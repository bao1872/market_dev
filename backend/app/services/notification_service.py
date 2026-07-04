"""通知服务 - 消息创建、投递、渠道管理。

核心功能：
1. create_message(db, user_id, message_dto, source_type, source_id, idempotency_key):
   创建通知消息（幂等，idempotency_key 唯一）
2. deliver_message(db, message_id, channel_id):
   投递消息到指定渠道（调用 ChannelAdapter，幂等）
3. create_channel(db, user_id, adapter_type, display_name, target_config):
   创建通知渠道
4. update_channel(db, channel_id, user_id, display_name, target_config):
   更新通知渠道配置（敏感字段合并保留）
5. delete_channel(db, channel_id, user_id):
   删除通知渠道（软删除：status=inactive）
6. verify_channel(db, channel_id, user_id):
   验证渠道配置（调用 ChannelAdapter.verify，含所有权校验）

幂等保证：
- 消息创建：idempotency_key 唯一约束
- 投递：message_deliveries.idempotency_key 唯一约束
- 相同 idempotency_key 的操作不重复执行
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import app.services.feishu_platform_app_adapter  # noqa: F401
from app.core.time import format_shanghai_datetime
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

logger = logging.getLogger("notification_service")


class NotificationServiceError(ValueError):
    """通知服务业务错误基类。"""


class MessageNotFoundError(NotificationServiceError):
    """消息不存在。"""


class ChannelNotFoundError(NotificationServiceError):
    """渠道不存在。"""


class ChannelOwnershipError(NotificationServiceError):
    """渠道不属于当前用户（跨用户操作被拒绝）。"""


class DuplicateMessageError(NotificationServiceError):
    """重复消息（幂等键冲突）。"""


class DuplicateActiveChannelError(NotificationServiceError):
    """同一用户同一飞书适配器类型下已存在 active 渠道。"""


# [通知渠道] - 受 active 唯一约束限制的飞书适配器类型（仅 Platform App）
_FEISHU_ADAPTER_TYPES = {"feishu_platform_app"}


async def _ensure_no_active_feishu_conflict(
    db: AsyncSession,
    user_id: UUID,
    adapter_type: str,
    exclude_channel_id: UUID | None = None,
) -> None:
    """前置校验：同一用户下是否已存在 active 飞书渠道（Platform App）。

    用于 create_channel / update_channel / verify_channel / test_channel 等
    可能产生或保持 active 状态的入口，确保用户最多只有一条 active 飞书渠道。
    """
    if adapter_type not in _FEISHU_ADAPTER_TYPES:
        return

    # [通知渠道] - 单用户最多一条 active 飞书渠道（仅 Platform App）
    stmt = select(NotificationChannel.id, NotificationChannel.adapter_type).where(
        NotificationChannel.user_id == user_id,
        NotificationChannel.adapter_type.in_(_FEISHU_ADAPTER_TYPES),
        NotificationChannel.status == "active",
    )
    if exclude_channel_id is not None:
        stmt = stmt.where(NotificationChannel.id != exclude_channel_id)

    result = await db.execute(stmt)
    row = result.one_or_none()
    if row is not None:
        existing_id, existing_type = row
        raise DuplicateActiveChannelError(
            f"用户已存在 active {existing_type} 渠道（channel_id={existing_id}），"
            "同一用户下不能同时拥有多条 active 飞书渠道"
        )


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


def _delivery_idempotency_key(
    message_id: UUID,
    channel_id: UUID,
    delivery_type: str = "card",
    image_url: str | None = None,
) -> str:
    """生成投递幂等键。

    图片投递时包含 image_url，避免同消息不同截图被去重。
    """
    parts = [str(message_id), str(channel_id), delivery_type]
    if image_url:
        parts.append(image_url)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


async def _execute_delivery(
    db: AsyncSession,
    delivery: MessageDelivery,
    image_bytes: bytes | None = None,
) -> MessageDelivery:
    """执行单条 MessageDelivery 投递（状态机核心）。

    状态机：
    - status=success：幂等快速返回，不重复投递
    - status=failed/retrying/pending/sending：允许执行投递
    - 进入 sending -> 调用 adapter -> success / retrying / dead

    Args:
        db: 异步会话
        delivery: MessageDelivery 记录（需已关联 message 与 channel）
        image_bytes: 图片字节（delivery_type=image 且未提供 image_url 时使用）

    Returns:
        更新后的 MessageDelivery

    Raises:
        NotificationServiceError: 适配器抛异常且无法归集为 DeliveryResult
    """
    # [通知投递] - 幂等：只有 success 才快速返回；failed/retrying 允许重发
    if delivery.status == "success":
        return delivery

    channel = delivery.channel
    message = delivery.message
    if channel is None or message is None:
        delivery.status = "dead"
        delivery.last_error_code = "MISSING_CHANNEL_OR_MESSAGE"
        await db.flush()
        raise NotificationServiceError(
            f"投递记录缺少关联渠道或消息: delivery_id={delivery.id}"
        )

    adapter = get_adapter(channel.adapter_type)
    message_dto = NotificationMessageDTO(**message.body)

    # 进入 sending 状态
    delivery.status = "sending"
    delivery.last_error_code = None
    await db.flush()

    try:
        if delivery.delivery_type == "image":
            # [截图缓存] - 任务 6.3：优先复用 delivery.image_url（capture worker 静态 URL）
            # 通过 _fetch_image_bytes 拉取已缓存的 PNG，不重新调用 capture worker 截图
            actual_image_bytes = image_bytes
            if actual_image_bytes is None and delivery.image_url:
                actual_image_bytes = await _fetch_image_bytes(delivery.image_url)
            if actual_image_bytes is None:
                delivery.status = "failed"
                delivery.attempt_count += 1
                delivery.last_error_code = "IMAGE_BYTES_MISSING"
                await db.flush()
                return delivery
            result: DeliveryResult = await adapter.send_image_bytes(
                actual_image_bytes, channel.target_config
            )
        elif delivery.delivery_type == "text":
            # [飞书两段式投递] - 纯文本消息：调用 adapter.send_text_message
            result = await adapter.send_text_message(
                message_dto, channel.target_config
            )
        else:
            # card（兼容管理后台预览/历史投递）
            result = await adapter.send(message_dto, channel.target_config)
    except Exception as e:
        delivery.status = "failed"
        delivery.attempt_count += 1
        delivery.last_error_code = "ADAPTER_EXCEPTION"
        delivery.provider_response = {"error": str(e)}
        await db.flush()
        raise NotificationServiceError(
            f"渠道投递异常: adapter={channel.adapter_type}, "
            f"delivery_id={delivery.id}, message_id={message.id}: {e}"
        ) from e

    # 更新投递记录
    delivery.attempt_count += 1
    if result.success:
        delivery.status = "success"
        delivery.last_error_code = None
    else:
        # 渠道失效时标记渠道 invalid，同时本投递进入 dead
        if result.error_code == "CHANNEL_INVALID":
            channel.status = "invalid"
            delivery.status = "dead"
        else:
            delivery.status = "failed"
        delivery.last_error_code = result.error_code
    delivery.provider_response = result.provider_response

    # [ImageDelivery] - 分别记录图片上传状态与最终投递状态
    if delivery.delivery_type == "image":
        if result.image_upload_success is True:
            delivery.image_upload_status = "success"
            delivery.image_upload_error_code = None
        elif result.image_upload_success is False:
            delivery.image_upload_status = "failed"
            delivery.image_upload_error_code = (
                result.image_upload_error_code or result.error_code
            )
        delivery.image_upload_provider_response = result.image_upload_provider_response
        delivery.image_key = result.image_key

    await db.flush()

    return delivery


async def _fetch_image_bytes(image_url: str) -> bytes | None:
    """从 image_url 拉取图片 bytes。

    image_url 为 capture worker 返回的本地静态 URL，支持相对路径与绝对路径。
    相对路径（以 / 开头）自动拼接 capture_worker_url，避免 httpx.get("/static/...")
    因缺少 host 而失败。
    """
    import httpx

    from app.config import get_settings

    # [飞书投递] - 描述: 相对 URL 自动拼接 capture_worker_url
    if image_url.startswith("/"):
        base = get_settings().capture_worker_url.rstrip("/")
        image_url = f"{base}{image_url}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(image_url)
            resp.raise_for_status()
            return resp.content
    except httpx.HTTPError as e:
        # 记录 IMAGE_FETCH_FAILED 错误码，便于 delivery_worker 归集失败原因
        logger.error("IMAGE_FETCH_FAILED url=%s error=%s", image_url, e)
        return None


async def deliver_message(
    db: AsyncSession,
    message_id: UUID,
    channel_id: UUID,
) -> MessageDelivery:
    """投递消息到指定渠道（幂等同步入口）。

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
    idem_key = _delivery_idempotency_key(message_id, channel_id, "card")

    # 4. 检查是否已存在投递记录
    stmt_del = select(MessageDelivery).where(
        MessageDelivery.idempotency_key == idem_key
    )
    result_del = await db.execute(stmt_del)
    delivery = result_del.scalar_one_or_none()
    if delivery is None:
        # 5. 创建投递记录
        delivery = MessageDelivery(
            id=uuid4(),
            notification_message_id=message_id,
            channel_id=channel_id,
            status="pending",
            delivery_type="card",
            attempt_count=0,
            idempotency_key=idem_key,
        )
        db.add(delivery)
        await db.flush()

    # 6. 执行投递状态机
    return await _execute_delivery(db, delivery)


async def deliver_image_message(
    db: AsyncSession,
    message_id: UUID,
    channel_id: UUID,
    image_bytes: bytes,
) -> MessageDelivery:
    """投递图片消息到指定渠道（幂等同步入口）。

    与 deliver_message 区别：
    - delivery_type = image
    - 调用 adapter.send_image_bytes 上传图片并发送

    幂等行为：
    - message_deliveries.idempotency_key = SHA256(message_id|channel_id|image)
    - 相同组合不重复投递

    Args:
        db: 异步会话
        message_id: 消息 ID
        channel_id: 渠道 ID
        image_bytes: PNG 图片 bytes

    Returns:
        MessageDelivery 投递记录

    Raises:
        MessageNotFoundError: 消息不存在
        ChannelNotFoundError: 渠道不存在
        NotificationServiceError: 适配器抛异常
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

    # 3. 生成投递幂等键（包含图片内容哈希，避免同消息不同图片被去重）
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    idem_key = hashlib.sha256(
        f"{message_id}|{channel_id}|{image_hash}".encode()
    ).hexdigest()

    # 4. 检查是否已存在投递记录
    stmt_del = select(MessageDelivery).where(
        MessageDelivery.idempotency_key == idem_key
    )
    result_del = await db.execute(stmt_del)
    delivery = result_del.scalar_one_or_none()
    if delivery is None:
        # 5. 创建图片投递记录
        delivery = MessageDelivery(
            id=uuid4(),
            notification_message_id=message_id,
            channel_id=channel_id,
            status="pending",
            delivery_type="image",
            attempt_count=0,
            idempotency_key=idem_key,
        )
        db.add(delivery)
        await db.flush()

    # 6. 执行投递状态机
    return await _execute_delivery(db, delivery, image_bytes=image_bytes)


async def create_channel(
    db: AsyncSession,
    user_id: UUID,
    adapter_type: str,
    display_name: str,
    target_config: dict[str, Any],
) -> NotificationChannel:
    """创建通知渠道。

    Args:
        db: 异步会话
        user_id: 用户 ID
        adapter_type: 渠道类型（feishu_platform_app/email/mock）
        display_name: 渠道名称
        target_config: 渠道配置

    Returns:
        NotificationChannel

    Raises:
        NotificationServiceError: adapter_type='feishu_webhook' 已废弃，被拒绝
    """
    # [通知渠道] - feishu_webhook 已废弃，统一为 feishu_platform_app
    if adapter_type == "feishu_webhook":
        raise NotificationServiceError(
            "feishu_webhook 渠道已废弃，请使用 feishu_platform_app（平台应用模式）"
        )
    # [通知渠道] - 前置校验：同一用户下最多一条 active 飞书渠道
    # create_channel 默认 status=pending，但若已有 active 飞书渠道，后续 verify/test
    # 将违反业务规则，因此创建时即拦截
    await _ensure_no_active_feishu_conflict(db, user_id, adapter_type)

    channel = NotificationChannel(
        id=uuid4(),
        user_id=user_id,
        adapter_type=adapter_type,
        display_name=display_name,
        target_config=target_config,
        status="pending",
    )
    db.add(channel)
    await db.flush()
    return channel


# 敏感字段列表：更新时若前端未传入，保留 DB 中的原值（仅 Platform App 的 app_secret）
_SENSITIVE_FIELDS = {"app_secret"}


async def update_channel(
    db: AsyncSession,
    channel_id: UUID,
    user_id: UUID,
    display_name: str | None = None,
    target_config: dict[str, Any] | None = None,
) -> NotificationChannel:
    """更新通知渠道配置。

    Args:
        db: 异步会话
        channel_id: 渠道 ID
        user_id: 用户 ID（权限校验）
        display_name: 新的渠道名称（None 表示不修改）
        target_config: 新的渠道配置（None 表示不修改）

    Returns:
        更新后的 NotificationChannel

    Raises:
        ValueError: 渠道不存在或不属于当前用户
    """
    channel = await db.get(NotificationChannel, channel_id)
    if channel is None or channel.user_id != user_id:
        raise ValueError("渠道不存在或无权操作")
    if channel.status == "inactive":
        raise ValueError("已删除的渠道无法修改")

    # [通知渠道] - 前置校验：飞书渠道更新时不允许与另一条 active 飞书渠道冲突
    # 更新后会将 status 置为 pending，若已存在其他 active 飞书渠道，后续 verify/test
    # 将违反业务规则，因此提前拦截
    await _ensure_no_active_feishu_conflict(
        db, user_id, channel.adapter_type, exclude_channel_id=channel.id
    )

    if display_name is not None:
        channel.display_name = display_name

    if target_config is not None:
        merged = dict(channel.target_config or {})
        for key, value in target_config.items():
            merged[key] = value
        # 保留前端未传入的敏感字段（脱敏接口省略了这些字段）
        for field in _SENSITIVE_FIELDS:
            if field not in target_config and field in merged:
                # 前端未传入，保留 DB 原值（merged 中已有）
                pass
            elif field not in target_config and field not in merged:
                # DB 中也没有，跳过
                pass
            # field in target_config: 前端显式传入新值，已在 merged 中更新
        channel.target_config = merged

    channel.status = "pending"
    await db.flush()
    return channel


async def delete_channel(
    db: AsyncSession,
    channel_id: UUID,
    user_id: UUID,
) -> NotificationChannel:
    """删除通知渠道（软删除：status=inactive）。

    Args:
        db: 异步会话
        channel_id: 渠道 ID
        user_id: 用户 ID（权限校验）

    Returns:
        更新后的 NotificationChannel

    Raises:
        ValueError: 渠道不存在或不属于当前用户
    """
    channel = await db.get(NotificationChannel, channel_id)
    if channel is None or channel.user_id != user_id:
        raise ValueError("渠道不存在或无权操作")

    channel.status = "inactive"
    await db.flush()
    return channel


async def verify_channel(
    db: AsyncSession,
    channel_id: UUID,
    user_id: UUID,
) -> NotificationChannel:
    """验证渠道配置（调用 ChannelAdapter.verify）。

    验证成功：status -> active，记录 last_verified_at
    验证失败：status -> invalid，记录 last_error_code

    Args:
        db: 异步会话
        channel_id: 渠道 ID
        user_id: 用户 ID（所有权校验，仅渠道所有者可操作）

    Returns:
        更新后的 NotificationChannel

    Raises:
        ChannelNotFoundError: 渠道不存在
        ChannelOwnershipError: 渠道不属于当前用户
    """
    stmt = select(NotificationChannel).where(NotificationChannel.id == channel_id)
    result = await db.execute(stmt)
    channel = result.scalar_one_or_none()
    if channel is None:
        raise ChannelNotFoundError(f"渠道不存在: channel_id={channel_id}")
    if channel.user_id != user_id:
        raise ChannelOwnershipError(f"无权操作渠道: channel_id={channel_id}")

    # [通知渠道] - 前置校验：verify 成功后会将 status 置为 active，需避免与另一条 active 飞书渠道冲突（不区分类型）
    await _ensure_no_active_feishu_conflict(
        db, channel.user_id, channel.adapter_type, exclude_channel_id=channel.id
    )

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


async def _patch_monitor_member_event_instruments(
    db: AsyncSession,
    messages: list[NotificationMessage],
) -> None:
    """补齐历史 MONITOR_MEMBER_EVENT 消息的股票信息。

    【仅历史消息读取，不生成新消息】MONITOR_MEMBER_EVENT 为旧单策略过程事件类型，
    新代码禁止生成（advice.md 第十一节遗留清理）。本函数仅用于读取历史消息时
    补齐缺失的股票信息，不创建任何新消息。

    若消息类型为 MONITOR_MEMBER_EVENT 且 body.resource_refs.instruments 为空，
    按 source_id 查询对应 StrategyEvent + Instrument，将股票信息写入 body。

    Args:
        db: 异步会话
        messages: 消息列表（原地修改 body）
    """
    from app.models.instrument import Instrument
    from app.models.strategy_event import StrategyEvent

    for message in messages:
        if message.message_type != "MONITOR_MEMBER_EVENT":
            continue

        body = message.body or {}
        resource_refs = body.get("resource_refs") or {}
        instruments = resource_refs.get("instruments")
        if instruments:
            continue

        source_id = message.source_id
        if source_id is None:
            continue

        event = await db.get(StrategyEvent, source_id)
        if event is None:
            continue

        instrument = await db.get(Instrument, event.instrument_id)
        if instrument is None:
            continue

        resource_refs["instruments"] = [
            {
                "instrument_id": str(instrument.id),
                "symbol": instrument.symbol,
                "name": instrument.name,
            },
        ]
        body["resource_refs"] = resource_refs
        message.body = body


async def list_user_messages(
    db: AsyncSession,
    user_id: UUID,
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
) -> list[NotificationMessage]:
    """列出用户消息。

    对每条消息 LEFT JOIN 其 message_deliveries 与关联 channel，
    返回真实投递状态；同时补齐历史 MONITOR_MEMBER_EVENT 的股票信息。

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
        .options(
            selectinload(NotificationMessage.deliveries)
            .selectinload(MessageDelivery.channel),
        )
        .order_by(NotificationMessage.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if unread_only:
        stmt = stmt.where(NotificationMessage.read_at.is_(None))
    result = await db.execute(stmt)
    messages = list(result.scalars().unique().all())

    # [历史兼容] 补齐 MONITOR_MEMBER_EVENT 股票信息
    await _patch_monitor_member_event_instruments(db, messages)

    return messages


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


# [Messages] - 描述: 未读消息计数，供角标使用（避免 list 接口 total 字段语义混淆）
async def count_unread_messages(db: AsyncSession, user_id: UUID) -> int:
    """统计用户未读消息数（read_at IS NULL）。

    Args:
        db: 异步会话
        user_id: 用户 ID

    Returns:
        未读消息总数
    """
    stmt = select(func.count()).select_from(NotificationMessage).where(
        NotificationMessage.user_id == user_id,
        NotificationMessage.read_at.is_(None),
    )
    result = await db.execute(stmt)
    return int(result.scalar_one())


# [Messages] - 描述: 批量标记当前用户所有未读消息为已读
async def mark_all_messages_read(db: AsyncSession, user_id: UUID) -> int:
    """批量标记用户所有未读消息为已读。

    Args:
        db: 异步会话
        user_id: 用户 ID

    Returns:
        受影响行数（被标记为已读的消息数）
    """
    stmt = (
        update(NotificationMessage)
        .where(
            NotificationMessage.user_id == user_id,
            NotificationMessage.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    result = await db.execute(stmt)
    return int(result.rowcount or 0)


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


async def list_message_deliveries(
    db: AsyncSession,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[MessageDelivery]:
    """查询消息投递记录（admin）。

    Args:
        db: 异步会话
        status: 状态筛选（pending/success/failed/retrying）
        limit: 分页大小
        offset: 分页偏移

    Returns:
        MessageDelivery 列表（按创建时间倒序）
    """
    stmt = (
        select(MessageDelivery)
        .options(
            selectinload(MessageDelivery.channel),
            selectinload(MessageDelivery.message),
        )
        .order_by(MessageDelivery.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if status:
        stmt = stmt.where(MessageDelivery.status == status)

    result = await db.execute(stmt)
    return list(result.scalars().unique().all())


async def retry_delivery(
    db: AsyncSession,
    delivery_id: UUID,
) -> MessageDelivery:
    """重试指定投递记录。

    直接更新已有的 MessageDelivery 记录并重新调用 adapter，
    不创建新记录，从而不破坏 deliver_message 的幂等语义。

    [截图缓存] - 任务 6.2/6.3：
    - 文本 delivery 重试：仅调用 adapter.send_text_message，不触发新截图
    - 图片 delivery 重试：复用 delivery.image_url（capture worker 返回的静态 URL），
      通过 _fetch_image_bytes 拉取已缓存的 PNG，不重新调用 capture worker
    - 截图缓存的 TTL（600s）由 stock_capture_service 保证，重试时通常仍在 TTL 内

    Args:
        db: 异步会话
        delivery_id: 投递记录 ID

    Returns:
        更新后的 MessageDelivery

    Raises:
        MessageNotFoundError: 投递记录不存在
        ChannelNotFoundError: 关联渠道不存在
    """
    stmt = (
        select(MessageDelivery)
        .where(MessageDelivery.id == delivery_id)
        .options(
            selectinload(MessageDelivery.channel),
            selectinload(MessageDelivery.message),
        )
    )
    result = await db.execute(stmt)
    delivery = result.scalar_one_or_none()
    if delivery is None:
        raise MessageNotFoundError(f"投递记录不存在: delivery_id={delivery_id}")

    channel = delivery.channel
    if channel is None:
        raise ChannelNotFoundError(f"投递记录关联渠道不存在: delivery_id={delivery_id}")

    message = delivery.message
    if message is None:
        raise MessageNotFoundError(f"投递记录关联消息不存在: delivery_id={delivery_id}")

    # 重置为重试中状态
    delivery.status = "retrying"
    delivery.last_error_code = None
    await db.flush()

    return await _execute_delivery(db, delivery)


async def retry_image_delivery(
    db: AsyncSession,
    message_group_id: str,
    user_id: UUID,
) -> list[MessageDelivery]:
    """按 message_group_id 重试失败的图片投递（不重复发送文字）。

    [StockDetailFeishu] - 描述: 仅重试 delivery_type='image' 且状态为 failed/dead 的投递，
    复用 retry_delivery，不创建新 MessageDelivery，从而不触发新的卡片段投递。

    Args:
        db: 异步会话
        message_group_id: 消息组 ID（关联 text+image 两条投递）
        user_id: 当前用户 ID（权限隔离）

    Returns:
        被重试的 MessageDelivery 列表（可能为空，表示无失败图片投递可重试）

    Raises:
        NotificationServiceError: 查询或重试过程中发生错误
    """
    stmt = (
        select(MessageDelivery)
        .join(NotificationChannel, MessageDelivery.channel_id == NotificationChannel.id)
        .where(
            MessageDelivery.message_group_id == message_group_id,
            MessageDelivery.delivery_type == "image",
            MessageDelivery.status.in_(["pending", "failed", "retrying", "dead"]),
            NotificationChannel.user_id == user_id,
        )
        .options(
            selectinload(MessageDelivery.channel),
            selectinload(MessageDelivery.message),
        )
        .order_by(MessageDelivery.created_at.asc())
    )
    result = await db.execute(stmt)
    image_deliveries = list(result.scalars().all())

    retried: list[MessageDelivery] = []
    for delivery in image_deliveries:
        retried_delivery = await retry_delivery(db, delivery.id)
        retried.append(retried_delivery)
    return retried


async def test_channel(
    db: AsyncSession,
    channel_id: UUID,
    user_id: UUID,
) -> tuple[NotificationChannel, DeliveryResult]:
    """测试渠道投递（发送测试消息）。

    与 verify_channel 的区别：
    - verify: 仅验证配置有效性（轻量）
    - test: 实际发送一条测试消息到渠道（完整投递验证）

    Args:
        db: 异步会话
        channel_id: 渠道 ID
        user_id: 用户 ID（所有权校验，仅渠道所有者可操作）

    Returns:
        (NotificationChannel, DeliveryResult) 渠道与投递结果

    Raises:
        ChannelNotFoundError: 渠道不存在
        ChannelOwnershipError: 渠道不属于当前用户
    """
    stmt = select(NotificationChannel).where(NotificationChannel.id == channel_id)
    result = await db.execute(stmt)
    channel = result.scalar_one_or_none()
    if channel is None:
        raise ChannelNotFoundError(f"渠道不存在: channel_id={channel_id}")
    if channel.user_id != user_id:
        raise ChannelOwnershipError(f"无权操作渠道: channel_id={channel_id}")

    adapter = get_adapter(channel.adapter_type)

    # 构建测试消息 DTO
    test_dto = NotificationMessageDTO(
        message_type="SYSTEM_ALERT",
        template_key="system_alert",
        template_version="1.1.0",
        title="渠道测试消息",
        summary=f"渠道「{channel.display_name}」测试投递，此消息无需关注。",
        resource_refs={"channel_id": str(channel_id), "test": True},
        data_time=format_shanghai_datetime(),
    )

    delivery_result = await adapter.send(test_dto, channel.target_config)

    # [通知渠道] - 前置校验：test 成功后会将 status 置为 active，需避免与另一条 active 飞书渠道冲突（不区分类型）
    if delivery_result.success:
        await _ensure_no_active_feishu_conflict(
            db, channel.user_id, channel.adapter_type, exclude_channel_id=channel.id
        )

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


class LatestEventNotFoundError(NotificationServiceError):
    """未找到可用于测试的最新事件。"""


async def test_channel_latest_event(
    db: AsyncSession,
    channel_id: UUID,
    frontend_base_url: str,
    capture_worker_url: str,
    capture_token_ttl_seconds: int = 300,
) -> tuple[NotificationChannel, NotificationMessage, dict[str, Any]]:
    """使用最新真实事件测试渠道图片投递链路。

    流程：
    1. 查询渠道与用户
    2. 取当前渠道用户 active watchlist 中股票的最新 StrategyEvent
    3. 生成 test_run_id 避免双击重复
    4. 生成短期 capture token
    5. 调用截图 Worker HTTP 服务获取图片本地静态 URL
    6. 创建通知消息并写入 Outbox（delivery_type=image, image_url=...）
    7. 由 Outbox Relay 扩张为 MessageDelivery(pending)，Delivery Worker 异步投递

    Args:
        db: 异步会话
        channel_id: 渠道 ID
        frontend_base_url: 前端 base URL
        capture_worker_url: 截图 Worker HTTP 服务地址
        capture_token_ttl_seconds: capture token 有效期（秒）

    Returns:
        (NotificationChannel, NotificationMessage, meta_info) 三元组

    Raises:
        ChannelNotFoundError: 渠道不存在
        LatestEventNotFoundError: 无可用事件或事件对应标的不存在
        NotificationServiceError: 截图服务调用失败
    """
    import httpx

    from app.core.security import create_capture_token
    from app.models.instrument import Instrument
    from app.models.strategy_event import StrategyEvent
    from app.models.watchlist import UserWatchlistItem
    from app.services.outbox_relay import write_outbox

    # 1. 查询渠道
    stmt = select(NotificationChannel).where(NotificationChannel.id == channel_id)
    result = await db.execute(stmt)
    channel = result.scalar_one_or_none()
    if channel is None:
        raise ChannelNotFoundError(f"渠道不存在: channel_id={channel_id}")

    # 2. 查询当前渠道用户 active watchlist 中的 instrument_id 列表
    stmt_watchlist = (
        select(UserWatchlistItem.instrument_id)
        .where(
            UserWatchlistItem.user_id == channel.user_id,
            UserWatchlistItem.active == True,  # noqa: E712
        )
    )
    result_watchlist = await db.execute(stmt_watchlist)
    watchlist_instrument_ids = [row[0] for row in result_watchlist.all()]

    if not watchlist_instrument_ids:
        raise LatestEventNotFoundError("当前渠道用户无活跃自选股，无可用事件")

    # 3. 取这些股票中的最新 StrategyEvent
    stmt_event = (
        select(StrategyEvent)
        .where(StrategyEvent.instrument_id.in_(watchlist_instrument_ids))
        .order_by(StrategyEvent.created_at.desc())
        .limit(1)
    )
    result_event = await db.execute(stmt_event)
    event = result_event.scalar_one_or_none()
    if event is None:
        raise LatestEventNotFoundError("当前渠道用户活跃自选股中无最新策略事件")

    # 4. 查询标的 symbol
    instrument = await db.get(Instrument, event.instrument_id)
    if instrument is None:
        raise LatestEventNotFoundError(
            f"事件对应标的不存在: instrument_id={event.instrument_id}"
        )
    symbol = instrument.symbol

    # 5. 生成 test_run_id 与 capture token
    test_run_id = uuid4()
    token = create_capture_token(
        subject=str(channel.user_id),
        event_id=str(event.id),
        expires_delta=timedelta(seconds=capture_token_ttl_seconds),
    )

    # 6. 调用截图 Worker 获取图片本地静态 URL
    # [screenshot-cache] - 传入 instrument_id 与 chart_version 启用缓存（任务 6.1）
    capture_payload = {
        "symbol": symbol,
        "event_id": str(event.id),
        "token": token,
        "frontend_base_url": frontend_base_url,
        "output_filename": f"test-{channel_id}-{test_run_id}",
        "instrument_id": str(event.instrument_id),
        "chart_version": "v1",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            capture_resp = await client.post(
                f"{capture_worker_url.rstrip('/')}/capture",
                json=capture_payload,
            )
            capture_resp.raise_for_status()
            capture_data = capture_resp.json()
    except Exception as e:
        raise NotificationServiceError(
            f"截图服务调用失败: capture_worker={capture_worker_url}, symbol={symbol}: {e}"
        ) from e

    image_url = capture_data.get("image_url")
    if not image_url:
        raise NotificationServiceError("截图服务未返回 image_url")

    # 7. 构建通知消息 DTO
    event_label = event.event_type
    dto = NotificationMessageDTO(
        message_type="MONITOR_EVENT",
        template_key="monitor_event",
        template_version="1.1.0",
        title=f"实测图片｜{instrument.name or symbol}",
        summary=f"事件 {event_label} · 股票 {symbol}",
        resource_refs={
            "event_id": str(event.id),
            "instrument_id": str(event.instrument_id),
            "symbol": symbol,
            "channel_id": str(channel_id),
            "test": True,
            "test_run_id": str(test_run_id),
        },
        data_time=event.event_time.isoformat() if event.event_time else "",
        primary_instrument={
            "instrument_id": str(event.instrument_id),
            "symbol": symbol,
            "name": instrument.name or "",
        },
        event_summary=event_label,
    )

    # 8. 创建消息（幂等，test_run_id 避免双击重复）
    message = await create_message(
        db=db,
        user_id=channel.user_id,
        message_dto=dto,
        source_type="strategy_event",
        source_id=event.id,
        idempotency_key=f"test-latest-event:{channel_id}:{event.id}:{test_run_id}",
    )

    # 9. 写入 Outbox，携带 image_url（不长期存 base64）
    await write_outbox(
        db=db,
        event_type="notification.message.created",
        payload={
            "message_id": str(message.id),
            "user_id": str(channel.user_id),
            "delivery_type": "image",
            "image_url": image_url,
        },
        aggregate_type="notification_message",
        aggregate_id=message.id,
    )

    meta = {
        "event_id": str(event.id),
        "symbol": symbol,
        "message_id": str(message.id),
        "delivery_status": "pending",
    }
    return channel, message, meta


if __name__ == "__main__":
    # 自测入口：验证服务函数可导入（不连接 DB）
    print(f"create_message={create_message}")
    print(f"deliver_message={deliver_message}")
    print(f"create_channel={create_channel}")
    print(f"update_channel={update_channel}")
    print(f"delete_channel={delete_channel}")
    print(f"verify_channel={verify_channel}")
    print(f"list_user_messages={list_user_messages}")
    print(f"mark_message_read={mark_message_read}")
    print(f"count_unread_messages={count_unread_messages}")
    print(f"mark_all_messages_read={mark_all_messages_read}")
    print(f"list_user_channels={list_user_channels}")
    print(f"test_channel={test_channel}")
    print(f"test_channel_latest_event={test_channel_latest_event}")
    print(f"deliver_image_message={deliver_image_message}")
    print(f"_generate_idempotency_key={_generate_idempotency_key}")

    # 验证飞书适配器已注册
    from app.services.channel_adapter import list_supported_adapters
    adapters = list_supported_adapters()
    print(f"registered_adapters={adapters}")
    assert "mock" in adapters
    assert "feishu_webhook" not in adapters
    assert "feishu_platform_app" in adapters

    print("OK")
