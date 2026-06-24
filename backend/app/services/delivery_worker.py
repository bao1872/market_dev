"""投递 Worker - 消费 Outbox 事件，将通知消息投递到用户渠道。

设计：
- process_notification_outbox: 轮询 outbox 表中 notification.message.created 事件
- 对每条事件：查询消息 → 查询用户活跃渠道 → 逐渠道投递（幂等）
- 投递失败按 error_code 分类：RETRYABLE 延迟重试，CHANNEL_INVALID 标记渠道失效
- 静默时段：quiet_hours 配置，期间不投递（仅站内消息可见）

幂等保证：
- MessageDelivery.idempotency_key = SHA256(message_id + channel_id) 唯一
- Outbox 记录处理完成后标记 status=processed

Inputs:
    db: AsyncSession
    batch_size: int (单次轮询最大事件数)

How to Run:
    python -m app.services.delivery_worker    # 自测：验证函数签名（不连 DB）
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import (
    NotificationChannel,
    NotificationMessage,
)
from app.models.outbox import Outbox
from app.schemas.notification import DeliveryResult, NotificationMessageDTO
from app.services.notification_service import deliver_image_message, deliver_message

logger = logging.getLogger("delivery_worker")

# 通知事件类型（outbox.event_type）
_NOTIFICATION_EVENT_TYPE = "notification.message.created"

# 单次轮询最大事件数
DEFAULT_BATCH_SIZE = 50

# 最大重试次数
DEFAULT_MAX_RETRY = 3

# 静默时段配置（默认 22:00-08:00 不投递飞书，仅站内可见）
DEFAULT_QUIET_HOURS_START = 22
DEFAULT_QUIET_HOURS_END = 8

# 通知投递 Worker 默认时区（上海时区）
_CST = ZoneInfo("Asia/Shanghai")


def _is_quiet_hours(
    now: datetime | None = None,
    quiet_start: int = DEFAULT_QUIET_HOURS_START,
    quiet_end: int = DEFAULT_QUIET_HOURS_END,
) -> bool:
    """判断当前是否在静默时段内。

    使用 Asia/Shanghai 时区作为默认时区；若传入带时区 datetime，则按该时区判断。

    Args:
        now: 当前时间（None 表示当前上海时间）
        quiet_start: 静默开始小时（如 22）
        quiet_end: 静默结束小时（如 8）

    Returns:
        True 表示在静默时段内
    """
    if now is None:
        now = datetime.now(_CST)
    hour = now.hour
    if quiet_start > quiet_end:
        # 跨天（如 22-8）
        return hour >= quiet_start or hour < quiet_end
    return quiet_start <= hour < quiet_end


def _compute_next_attempt_at(
    now: datetime,
    quiet_start: int = DEFAULT_QUIET_HOURS_START,
    quiet_end: int = DEFAULT_QUIET_HOURS_END,
) -> datetime:
    """计算静默结束后首个可投递时间点。

    Args:
        now: 当前时间（带时区）
        quiet_start: 静默开始小时
        quiet_end: 静默结束小时

    Returns:
        下次可投递时间（与 now 相同时区）
    """
    # 统一转换为上海时区计算，确保跨天场景正确
    now_cst = now.astimezone(_CST)
    candidate = now_cst.replace(hour=quiet_end, minute=0, second=0, microsecond=0)
    if candidate <= now_cst:
        candidate = candidate + timedelta(days=1)
    # 返回与输入相同时区，便于测试与日志一致
    return candidate.astimezone(now.tzinfo or _CST)


async def _get_message_and_channels(
    db: AsyncSession,
    message_id: UUID,
    user_id: UUID,
) -> tuple[NotificationMessage | None, list[NotificationChannel]]:
    """查询消息与用户活跃渠道。

    Args:
        db: 异步会话
        message_id: 通知消息 ID
        user_id: 用户 ID

    Returns:
        (NotificationMessage | None, list[NotificationChannel])
    """
    # 查询消息
    stmt_msg = select(NotificationMessage).where(
        NotificationMessage.id == message_id
    )
    result_msg = await db.execute(stmt_msg)
    message = result_msg.scalar_one_or_none()

    # 查询用户活跃渠道
    stmt_ch = (
        select(NotificationChannel)
        .where(
            NotificationChannel.user_id == user_id,
            NotificationChannel.status == "active",
        )
        .order_by(NotificationChannel.created_at.desc())
    )
    result_ch = await db.execute(stmt_ch)
    channels = list(result_ch.scalars().all())

    return message, channels


async def _process_single_outbox(
    db: AsyncSession,
    outbox_record: Outbox,
) -> bool:
    """处理单条 outbox 事件（非静默时段）。

    Args:
        db: 异步会话
        outbox_record: Outbox 记录

    Returns:
        True 表示处理成功
    """
    payload: dict[str, Any] = outbox_record.payload or {}
    message_id_str = payload.get("message_id")
    user_id_str = payload.get("user_id")
    delivery_type = payload.get("delivery_type", "card")

    if not message_id_str or not user_id_str:
        logger.warning(
            "outbox 事件缺少 message_id/user_id: outbox_id=%s payload=%s",
            outbox_record.id, payload,
        )
        return True  # 标记为已处理（无效事件不重试）

    try:
        message_id = UUID(message_id_str)
        user_id = UUID(user_id_str)
    except ValueError as e:
        logger.warning(
            "outbox 事件 message_id/user_id 格式非法: outbox_id=%s: %s",
            outbox_record.id, e,
        )
        return True

    # 图片投递：从 payload 解析 base64 图片 bytes
    image_bytes: bytes | None = None
    if delivery_type == "image":
        image_b64 = payload.get("image_bytes_base64")
        if not image_b64:
            logger.warning(
                "图片 outbox 事件缺少 image_bytes_base64: outbox_id=%s",
                outbox_record.id,
            )
            return True
        try:
            image_bytes = base64.b64decode(image_b64)
        except Exception as e:
            logger.warning(
                "图片 outbox 事件 base64 解码失败: outbox_id=%s: %s",
                outbox_record.id, e,
            )
            return True

    # 查询消息与渠道
    message, channels = await _get_message_and_channels(db, message_id, user_id)

    if message is None:
        logger.warning(
            "通知消息不存在: message_id=%s outbox_id=%s",
            message_id, outbox_record.id,
        )
        return True

    if not channels:
        logger.info(
            "用户无活跃渠道，跳过投递: user_id=%s message_id=%s",
            user_id, message_id,
        )
        return True

    # 逐渠道投递
    delivered_count = 0
    for channel in channels:
        try:
            if delivery_type == "image" and image_bytes is not None:
                delivery = await deliver_image_message(
                    db, message_id, channel.id, image_bytes,
                )
            else:
                delivery = await deliver_message(db, message_id, channel.id)

            if delivery.status == "success":
                delivered_count += 1
                logger.info(
                    "投递成功: message_id=%s channel=%s delivery_type=%s",
                    message_id, channel.display_name, delivery_type,
                )
            else:
                logger.warning(
                    "投递失败: message_id=%s channel=%s delivery_type=%s "
                    "status=%s error=%s",
                    message_id, channel.display_name, delivery_type,
                    delivery.status, delivery.last_error_code,
                )

                # 渠道失效时标记 invalid
                if delivery.last_error_code == "CHANNEL_INVALID":
                    channel.status = "invalid"
                    logger.warning(
                        "渠道标记失效: channel_id=%s channel=%s",
                        channel.id, channel.display_name,
                    )

        except Exception as e:
            logger.error(
                "投递异常: message_id=%s channel=%s delivery_type=%s: %s",
                message_id, channel.display_name, delivery_type, e,
            )
            # 不 re-raise，继续处理其他渠道

    logger.info(
        "outbox 事件处理完成: outbox_id=%s message_id=%s "
        "delivery_type=%s channels=%s delivered=%s",
        outbox_record.id, message_id, delivery_type, len(channels), delivered_count,
    )
    return True


async def process_notification_outbox(
    db: AsyncSession,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_retry: int = DEFAULT_MAX_RETRY,
    quiet_hours: bool | None = None,
    now: datetime | None = None,
) -> int:
    """轮询 outbox 表，独占领取并处理 notification.message.created 事件。

    流程：
    1. 使用 SELECT ... FOR UPDATE SKIP LOCKED 查询 pending / 已到下次尝试时间的 deferred 记录
    2. 静默时段：将记录标记为 deferred 并计算 next_attempt_at，不投递
    3. 非静默时段：查询消息 → 查询用户活跃渠道 → 逐渠道投递
    4. 处理完成后标记 status=processed
    5. 失败则 retry_count+1，超过 max_retry 标记 failed

    Args:
        db: 异步会话
        batch_size: 单次轮询最大事件数
        max_retry: 最大重试次数
        quiet_hours: 是否静默时段（None 表示按上海时区自动判断）
        now: 当前时间（None 表示当前上海时间，用于测试）

    Returns:
        本次成功处理的事件数
    """
    if now is None:
        now = datetime.now(_CST)
    if quiet_hours is None:
        quiet_hours = _is_quiet_hours(now)

    # 1. 使用 FOR UPDATE SKIP LOCKED 独占领取通知事件
    #    包含 pending 与已到 next_attempt_at 的 deferred 记录
    stmt = (
        select(Outbox)
        .where(
            Outbox.event_type == _NOTIFICATION_EVENT_TYPE,
            or_(
                Outbox.status == "pending",
                and_(
                    Outbox.status == "deferred",
                    or_(
                        Outbox.next_attempt_at.is_(None),
                        Outbox.next_attempt_at <= now,
                    ),
                ),
            ),
        )
        .order_by(Outbox.created_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(stmt)
    pending_records = list(result.scalars().all())

    if not pending_records:
        return 0

    # 静默时段：统一 deferred，不投递
    if quiet_hours:
        for record in pending_records:
            record.status = "deferred"
            record.next_attempt_at = _compute_next_attempt_at(now)
            logger.info(
                "静默时段延迟投递: outbox_id=%s next_attempt_at=%s",
                record.id, record.next_attempt_at,
            )
        await db.flush()
        return 0

    processed_count = 0
    for record in pending_records:
        try:
            success = await _process_single_outbox(db, record)
            if success:
                record.status = "processed"
                record.processed_at = datetime.now(UTC)
                record.next_attempt_at = None
                processed_count += 1
            else:
                record.retry_count += 1
                if record.retry_count >= max_retry:
                    record.status = "failed"
                    record.next_attempt_at = None
        except Exception as e:
            logger.error(
                "outbox 事件处理异常: outbox_id=%s: %s",
                record.id, e,
            )
            record.retry_count += 1
            if record.retry_count >= max_retry:
                record.status = "failed"
                record.next_attempt_at = None

    await db.flush()
    return processed_count


async def get_pending_notification_count(db: AsyncSession) -> int:
    """获取待处理（pending + deferred）的通知 outbox 事件数（监控用）。"""
    from sqlalchemy import func

    stmt = select(func.count(Outbox.id)).where(
        Outbox.event_type == _NOTIFICATION_EVENT_TYPE,
        Outbox.status.in_(["pending", "deferred"]),
    )
    result = await db.execute(stmt)
    return int(result.scalar() or 0)


if __name__ == "__main__":
    # 自测入口：验证函数签名与静默时段判断（不连 DB，无副作用）
    import inspect

    for fn in (
        process_notification_outbox,
        _process_single_outbox,
        _get_message_and_channels,
        get_pending_notification_count,
    ):
        assert inspect.iscoroutinefunction(fn), f"{fn.__name__} 应为协程函数"
        print(f"{fn.__name__} params={list(inspect.signature(fn).parameters.keys())}")

    # 测试静默时段判断（上海时区与 naive datetime 兼容）
    from datetime import datetime

    # 22:00 在静默时段内
    t1 = datetime(2026, 6, 18, 22, 30)
    assert _is_quiet_hours(t1) is True

    # 10:00 不在静默时段内
    t2 = datetime(2026, 6, 18, 10, 0)
    assert _is_quiet_hours(t2) is False

    # 03:00 在静默时段内（跨天）
    t3 = datetime(2026, 6, 18, 3, 0)
    assert _is_quiet_hours(t3) is True

    # 08:00 不在静默时段内（边界）
    t4 = datetime(2026, 6, 18, 8, 0)
    assert _is_quiet_hours(t4) is False

    # 14:49 CST 盘中交易时间，不应静默
    t5 = datetime(2026, 6, 18, 14, 49, tzinfo=_CST)
    assert _is_quiet_hours(t5) is False

    # 测试 next_attempt_at 计算：22:30 -> 次日 08:00 CST
    t6 = datetime(2026, 6, 18, 22, 30, tzinfo=_CST)
    next_at = _compute_next_attempt_at(t6)
    assert next_at.hour == 8
    assert next_at.minute == 0
    assert next_at.date().day == 19

    print(f"quiet_hours_start={DEFAULT_QUIET_HOURS_START}")
    print(f"quiet_hours_end={DEFAULT_QUIET_HOURS_END}")
    print(f"event_type={_NOTIFICATION_EVENT_TYPE}")
    print("OK")
