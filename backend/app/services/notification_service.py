"""通知服务 - 消息创建、投递、渠道管理。

核心功能：
1. create_message(db, user_id, message_dto, source_type, source_id, idempotency_key):
   创建通知消息（幂等，idempotency_key 唯一）
2. deliver_message(db, message_id, channel_id):
   投递消息到指定渠道（调用 ChannelAdapter，幂等）
3. create_channel(db, user_id, adapter_type, display_name, target_config):
   创建通知渠道
4. verify_channel(db, channel_id):
   验证渠道配置（调用 ChannelAdapter.verify）

幂等保证：
- 消息创建：idempotency_key 唯一约束
- 投递：message_deliveries.idempotency_key 唯一约束
- 相同 idempotency_key 的操作不重复执行
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.feishu_platform_app_adapter  # noqa: F401

# 导入飞书 Webhook 适配器以触发注册（@register_adapter 在导入时执行）
import app.services.feishu_webhook_adapter  # noqa: F401
from app.models.notification import (
    MessageDelivery,
    NotificationChannel,
    NotificationMessage,
)
from app.schemas.notification import (
    DeliveryResult,
    NotificationMessageDTO,
)
from app.services.channel_adapter import get_adapter


class NotificationServiceError(ValueError):
    """通知服务业务错误基类。"""


class MessageNotFoundError(NotificationServiceError):
    """消息不存在。"""


class ChannelNotFoundError(NotificationServiceError):
    """渠道不存在。"""


class DuplicateMessageError(NotificationServiceError):
    """重复消息（幂等键冲突）。"""


def _generate_idempotency_key(
    user_id: UUID,
    message_dto: NotificationMessageDTO,
    source_type: str,
    source_id: UUID | None,
) -> str:
    """根据消息内容生成幂等键（若未显式提供）。

    幂等键 = SHA256(user_id + message_type + template_key + template_version + source_type + source_id + data_time)
    """
    parts = [
        str(user_id),
        message_dto.message_type,
        message_dto.template_key,
        message_dto.template_version,
        source_type,
        str(source_id) if source_id else "",
        message_dto.data_time,
    ]
    payload = "|".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def create_message(
    db: AsyncSession,
    user_id: UUID,
    message_dto: NotificationMessageDTO,
    source_type: str,
    source_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> NotificationMessage:
    """创建通知消息（幂等）。

    幂等行为：
    - idempotency_key 唯一约束，相同 key 不重复创建
    - 若未提供 idempotency_key，根据消息内容自动生成

    Args:
        db: 异步会话
        user_id: 用户 ID
        message_dto: 统一消息 DTO
        source_type: 来源类型（如 strategy_run/selection_plan_run）
        source_id: 来源聚合 ID
        idempotency_key: 幂等键（可选，未提供则自动生成）

    Returns:
        NotificationMessage

    Raises:
        DuplicateMessageError: 幂等键冲突（消息已存在）
    """
    if idempotency_key is None:
        idempotency_key = _generate_idempotency_key(
            user_id, message_dto, source_type, source_id
        )

    # 先检查是否已存在（幂等快速路径）
    stmt = select(NotificationMessage).where(
        NotificationMessage.idempotency_key == idempotency_key
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    # 创建新消息
    message = NotificationMessage(
        id=uuid4(),
        user_id=user_id,
        message_type=message_dto.message_type,
        template_key=message_dto.template_key,
        template_version=message_dto.template_version,
        source_type=source_type,
        source_id=source_id,
        body=message_dto.model_dump(),
        idempotency_key=idempotency_key,
    )
    db.add(message)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        # 幂等键冲突：查询已存在的返回
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing
        raise DuplicateMessageError(
            f"创建消息失败（幂等键冲突）: {e}"
        ) from e

    return message


async def deliver_message(
    db: AsyncSession,
    message_id: UUID,
    channel_id: UUID,
) -> MessageDelivery:
    """投递消息到指定渠道（幂等）。

    幂等行为：
    - message_deliveries.idempotency_key = SHA256(message_id + channel_id)
    - 相同组合不重复投递

    Args:
        db: 异步会话
        message_id: 消息 ID
        channel_id: 渠道 ID

    Returns:
        MessageDelivery 投递记录

    Raises:
        MessageNotFoundError: 消息不存在
        ChannelNotFoundError: 渠道不存在
    """
    # 1. 查询消息
    stmt_msg = select(NotificationMessage).where(NotificationMessage.id == message_id)
    result_msg = await db.execute(stmt_msg)
    message = result_msg.scalar_one_or_none()
    if message is None:
        raise MessageNotFoundError(f"消息不存在: message_id={message_id}")

    # 2. 查询渠道
    stmt_ch = select(NotificationChannel).where(NotificationChannel.id == channel_id)
    result_ch = await db.execute(stmt_ch)
    channel = result_ch.scalar_one_or_none()
    if channel is None:
        raise ChannelNotFoundError(f"渠道不存在: channel_id={channel_id}")

    # 3. 生成投递幂等键
    idem_key = hashlib.sha256(
        f"{message_id}|{channel_id}".encode()
    ).hexdigest()

    # 4. 检查是否已投递（幂等）
    stmt_del = select(MessageDelivery).where(
        MessageDelivery.idempotency_key == idem_key
    )
    result_del = await db.execute(stmt_del)
    existing_delivery = result_del.scalar_one_or_none()
    if existing_delivery is not None:
        return existing_delivery

    # 5. 创建投递记录
    delivery = MessageDelivery(
        id=uuid4(),
        notification_message_id=message_id,
        channel_id=channel_id,
        status="pending",
        attempt_count=0,
        idempotency_key=idem_key,
    )
    db.add(delivery)
    await db.flush()

    # 6. 调用 ChannelAdapter 投递
    adapter = get_adapter(channel.adapter_type)
    message_dto = NotificationMessageDTO(**message.body)

    try:
        result: DeliveryResult = await adapter.send(message_dto, channel.target_config)
    except Exception as e:
        # Adapter 抛异常：记录失败，不吞没上下文
        delivery.status = "failed"
        delivery.attempt_count += 1
        delivery.last_error_code = "ADAPTER_EXCEPTION"
        delivery.provider_response = {"error": str(e)}
        await db.flush()
        raise NotificationServiceError(
            f"渠道投递异常: adapter={channel.adapter_type}, "
            f"message_id={message_id}, channel_id={channel_id}: {e}"
        ) from e

    # 7. 更新投递记录
    delivery.attempt_count += 1
    if result.success:
        delivery.status = "success"
    else:
        delivery.status = "failed"
        delivery.last_error_code = result.error_code
    delivery.provider_response = result.provider_response
    await db.flush()

    return delivery


async def create_channel(
    db: AsyncSession,
    user_id: UUID,
    adapter_type: str,
    display_name: str,
    target_config: dict[str, Any],
    secret_ref: UUID | None = None,
) -> NotificationChannel:
    """创建通知渠道。

    Args:
        db: 异步会话
        user_id: 用户 ID
        adapter_type: 渠道类型（feishu_webhook/email/mock）
        display_name: 渠道名称
        target_config: 渠道配置
        secret_ref: 敏感字段引用

    Returns:
        NotificationChannel
    """
    channel = NotificationChannel(
        id=uuid4(),
        user_id=user_id,
        adapter_type=adapter_type,
        display_name=display_name,
        target_config=target_config,
        secret_ref=secret_ref,
        status="pending",
    )
    db.add(channel)
    await db.flush()
    return channel


async def verify_channel(
    db: AsyncSession,
    channel_id: UUID,
) -> NotificationChannel:
    """验证渠道配置（调用 ChannelAdapter.verify）。

    验证成功：status -> active，记录 last_verified_at
    验证失败：status -> invalid，记录 last_error_code

    Args:
        db: 异步会话
        channel_id: 渠道 ID

    Returns:
        更新后的 NotificationChannel

    Raises:
        ChannelNotFoundError: 渠道不存在
    """
    stmt = select(NotificationChannel).where(NotificationChannel.id == channel_id)
    result = await db.execute(stmt)
    channel = result.scalar_one_or_none()
    if channel is None:
        raise ChannelNotFoundError(f"渠道不存在: channel_id={channel_id}")

    adapter = get_adapter(channel.adapter_type)
    try:
        verified = await adapter.verify(channel.target_config)
    except Exception as e:
        channel.status = "invalid"
        channel.last_error_code = "VERIFY_EXCEPTION"
        channel.last_verified_at = datetime.now(UTC)
        await db.flush()
        raise NotificationServiceError(
            f"渠道验证异常: channel_id={channel_id}, adapter={channel.adapter_type}: {e}"
        ) from e

    if verified:
        channel.status = "active"
        channel.last_error_code = None
    else:
        channel.status = "invalid"
        channel.last_error_code = "VERIFY_FAILED"
    channel.last_verified_at = datetime.now(UTC)
    await db.flush()
    return channel


async def list_user_messages(
    db: AsyncSession,
    user_id: UUID,
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
) -> list[NotificationMessage]:
    """列出用户消息。

    Args:
        db: 异步会话
        user_id: 用户 ID
        limit: 返回条数
        offset: 偏移量
        unread_only: 仅返回未读消息

    Returns:
        消息列表（按创建时间倒序）
    """
    stmt = (
        select(NotificationMessage)
        .where(NotificationMessage.user_id == user_id)
        .order_by(NotificationMessage.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if unread_only:
        stmt = stmt.where(NotificationMessage.read_at.is_(None))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def mark_message_read(
    db: AsyncSession,
    message_id: UUID,
    user_id: UUID,
) -> NotificationMessage:
    """标记消息已读。

    Args:
        db: 异步会话
        message_id: 消息 ID
        user_id: 用户 ID（权限校验）

    Returns:
        更新后的 NotificationMessage

    Raises:
        MessageNotFoundError: 消息不存在或不属于该用户
    """
    stmt = select(NotificationMessage).where(
        NotificationMessage.id == message_id,
        NotificationMessage.user_id == user_id,
    )
    result = await db.execute(stmt)
    message = result.scalar_one_or_none()
    if message is None:
        raise MessageNotFoundError(
            f"消息不存在或不属于用户: message_id={message_id}, user_id={user_id}"
        )

    if message.read_at is None:
        message.read_at = datetime.now(UTC)
        await db.flush()
    return message


async def list_user_channels(
    db: AsyncSession,
    user_id: UUID,
) -> list[NotificationChannel]:
    """列出用户的通知渠道。

    Args:
        db: 异步会话
        user_id: 用户 ID

    Returns:
        NotificationChannel 列表（按创建时间倒序）
    """
    stmt = (
        select(NotificationChannel)
        .where(NotificationChannel.user_id == user_id)
        .order_by(NotificationChannel.created_at.desc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def test_channel(
    db: AsyncSession,
    channel_id: UUID,
) -> tuple[NotificationChannel, DeliveryResult]:
    """测试渠道投递（发送测试消息）。

    与 verify_channel 的区别：
    - verify: 仅验证配置有效性（轻量）
    - test: 实际发送一条测试消息到渠道（完整投递验证）

    Args:
        db: 异步会话
        channel_id: 渠道 ID

    Returns:
        (NotificationChannel, DeliveryResult) 渠道与投递结果

    Raises:
        ChannelNotFoundError: 渠道不存在
    """
    stmt = select(NotificationChannel).where(NotificationChannel.id == channel_id)
    result = await db.execute(stmt)
    channel = result.scalar_one_or_none()
    if channel is None:
        raise ChannelNotFoundError(f"渠道不存在: channel_id={channel_id}")

    adapter = get_adapter(channel.adapter_type)

    # 构建测试消息 DTO
    test_dto = NotificationMessageDTO(
        message_type="SYSTEM_ALERT",
        template_key="system_alert",
        template_version="1.1.0",
        title="渠道测试消息",
        summary=f"渠道「{channel.display_name}」测试投递，此消息无需关注。",
        resource_refs={"channel_id": str(channel_id), "test": True},
        data_time=datetime.now(UTC).isoformat(),
    )

    delivery_result = await adapter.send(test_dto, channel.target_config)

    # 更新渠道状态
    channel.last_verified_at = datetime.now(UTC)
    if delivery_result.success:
        channel.status = "active"
        channel.last_error_code = None
    else:
        if delivery_result.error_code == "CHANNEL_INVALID":
            channel.status = "invalid"
        elif delivery_result.error_code and "RETRYABLE" in delivery_result.error_code:
            channel.status = "degraded"
        else:
            channel.status = "invalid"
        channel.last_error_code = delivery_result.error_code

    await db.flush()
    return channel, delivery_result


if __name__ == "__main__":
    # 自测入口：验证服务函数可导入（不连接 DB）
    print(f"create_message={create_message}")
    print(f"deliver_message={deliver_message}")
    print(f"create_channel={create_channel}")
    print(f"verify_channel={verify_channel}")
    print(f"list_user_messages={list_user_messages}")
    print(f"mark_message_read={mark_message_read}")
    print(f"list_user_channels={list_user_channels}")
    print(f"test_channel={test_channel}")
    print(f"_generate_idempotency_key={_generate_idempotency_key}")

    # 验证飞书适配器已注册
    from app.services.channel_adapter import list_supported_adapters
    adapters = list_supported_adapters()
    print(f"registered_adapters={adapters}")
    assert "mock" in adapters
    assert "feishu_webhook" in adapters

    print("OK")
