"""投递 Worker - 轮询 MessageDelivery 表，将通知消息投递到用户渠道。

设计：
- process_pending_deliveries: 轮询 MessageDelivery 表
- 状态机：pending/sending/success/failed/retrying/dead
- 仅 status=success 时幂等快速返回
- failed/retrying 允许重发
- 使用 next_attempt_at / attempt_count 实现退避重试
- 超过最大重试次数后标记 dead
- 渠道失效（CHANNEL_INVALID）时标记渠道 invalid 并进入 dead

幂等保证：
- MessageDelivery.idempotency_key = SHA256(message_id + channel_id + delivery_type + image_url) 唯一
- 由 Outbox Relay 创建 MessageDelivery 时保证幂等

Inputs:
    db: AsyncSession
    batch_size: int (单次轮询最大记录数)

How to Run:
    python -m app.services.delivery_worker    # 自测：验证函数签名（不连 DB）
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.notification import MessageDelivery
from app.services.notification_service import _execute_delivery

logger = logging.getLogger("delivery_worker")

# 单次轮询最大记录数
DEFAULT_BATCH_SIZE = 50

# 最大重试次数（超过则 dead）
DEFAULT_MAX_RETRY = 3

# 重试退避基数（秒），第 n 次等待 2^n * _RETRY_BACKOFF_BASE
_RETRY_BACKOFF_BASE = 30

# 静默时段配置（默认 22:00-08:00 不投递，仅站内可见）
DEFAULT_QUIET_HOURS_START = 22
DEFAULT_QUIET_HOURS_END = 8

# [DeliveryWorker] - 用户主动触发的 source_type 集合：不受静默期限制，立即投递
# 监控自动触发的 source_type（如 monitor_event）仍受静默期保护，避免深夜打扰
_USER_TRIGGERED_SOURCE_TYPES = frozenset({"stock_detail_share"})

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


def _compute_retry_next_attempt_at(
    now: datetime,
    attempt_count: int,
    quiet_start: int = DEFAULT_QUIET_HOURS_START,
    quiet_end: int = DEFAULT_QUIET_HOURS_END,
) -> datetime:
    """计算失败后下次重试时间（指数退避 + 静默避让）。

    Args:
        now: 当前时间（带时区）
        attempt_count: 当前已尝试次数
        quiet_start: 静默开始小时
        quiet_end: 静默结束小时

    Returns:
        下次可重试时间
    """
    backoff = timedelta(seconds=(_RETRY_BACKOFF_BASE * (2 ** max(0, attempt_count - 1))))
    candidate = now + backoff

    # 若候选时间落在静默时段，顺延至静默结束后
    candidate_cst = candidate.astimezone(_CST)
    quiet_end_dt = candidate_cst.replace(hour=quiet_end, minute=0, second=0, microsecond=0)
    if quiet_start > quiet_end:
        # 跨天：当前在 quiet_start 之后或 quiet_end 之前都算静默
        if candidate_cst.hour >= quiet_start:
            quiet_end_dt = quiet_end_dt + timedelta(days=1)
        elif candidate_cst.hour < quiet_end:
            # 已在静默时段内，quiet_end_dt 就是今天结束时间
            pass
        else:
            return candidate
    else:
        if quiet_start <= candidate_cst.hour < quiet_end:
            pass
        else:
            return candidate

    if quiet_end_dt <= candidate_cst:
        quiet_end_dt = quiet_end_dt + timedelta(days=1)
    return quiet_end_dt.astimezone(now.tzinfo or _CST)


async def process_pending_deliveries(
    db: AsyncSession,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_retry: int = DEFAULT_MAX_RETRY,
    quiet_hours: bool | None = None,
    now: datetime | None = None,
) -> int:
    """轮询 MessageDelivery 表，执行待投递记录。

    流程：
    1. 使用 SELECT ... FOR UPDATE SKIP LOCKED 领取待投递记录
       条件：status=pending 或 (status=retrying 且 next_attempt_at <= now)
    2. 静默时段：将 pending 记录统一 deferred 为 retrying 并计算 next_attempt_at
    3. 非静默时段：调用 _execute_delivery 执行投递
       - 成功后 status=success
       - 失败后 status=retrying，增加 attempt_count，计算 next_attempt_at
       - 超过 max_retry 后 status=dead

    Args:
        db: 异步会话
        batch_size: 单次轮询最大记录数
        max_retry: 最大重试次数
        quiet_hours: 是否静默时段（None 表示按上海时区自动判断）
        now: 当前时间（None 表示当前上海时间，用于测试）

    Returns:
        本次成功投递的记录数
    """
    if now is None:
        now = datetime.now(_CST)
    if quiet_hours is None:
        quiet_hours = _is_quiet_hours(now)

    # 1. 使用 FOR UPDATE SKIP LOCKED 领取待投递记录
    stmt = (
        select(MessageDelivery)
        .where(
            or_(
                MessageDelivery.status == "pending",
                and_(
                    MessageDelivery.status == "retrying",
                    or_(
                        MessageDelivery.next_attempt_at.is_(None),
                        MessageDelivery.next_attempt_at <= now,
                    ),
                ),
            ),
        )
        .order_by(MessageDelivery.created_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
        .options(
            selectinload(MessageDelivery.channel),
            selectinload(MessageDelivery.message),
        )
    )
    result = await db.execute(stmt)
    pending_deliveries = list(result.scalars().all())

    if not pending_deliveries:
        return 0

    # [DeliveryWorker] - 静默时段处理：用户主动触发的投递立即执行，监控自动触发的延迟
    # 原因：用户点击"发送到飞书"按钮期望立即送达，不应被静默期延迟；
    #       监控通知才需要静默期保护，避免深夜打扰。
    if quiet_hours:
        deferred = []
        immediate = []
        for delivery in pending_deliveries:
            source_type = getattr(
                getattr(delivery, "message", None), "source_type", None,
            )
            if source_type in _USER_TRIGGERED_SOURCE_TYPES:
                immediate.append(delivery)
            else:
                deferred.append(delivery)

        # 延迟非用户主动触发的投递
        for delivery in deferred:
            delivery.status = "retrying"
            delivery.next_attempt_at = _compute_next_attempt_at(now)
            logger.info(
                "静默时段延迟投递: delivery_id=%s next_attempt_at=%s",
                delivery.id, delivery.next_attempt_at,
            )

        # 用户主动触发的投递立即执行（不受静默期限制）
        pending_deliveries = immediate
        await db.flush()
        if not pending_deliveries:
            return 0

    success_count = 0
    for delivery in pending_deliveries:
        try:
            await _execute_delivery(db, delivery)
            if delivery.status == "success":
                success_count += 1
            elif delivery.status in ("failed", "retrying"):
                # 失败后设置下次重试时间
                delivery.status = "retrying"
                if delivery.attempt_count >= max_retry:
                    delivery.status = "dead"
                    delivery.next_attempt_at = None
                else:
                    delivery.next_attempt_at = _compute_retry_next_attempt_at(
                        now, delivery.attempt_count,
                    )
                logger.warning(
                    "投递失败待重试: delivery_id=%s attempt=%s status=%s error=%s",
                    delivery.id, delivery.attempt_count, delivery.status,
                    delivery.last_error_code,
                )
            elif delivery.status == "dead":
                delivery.next_attempt_at = None
                logger.warning(
                    "投递进入 dead: delivery_id=%s attempt=%s error=%s",
                    delivery.id, delivery.attempt_count, delivery.last_error_code,
                )
        except Exception as e:
            logger.error(
                "投递执行异常: delivery_id=%s: %s",
                delivery.id, e,
            )
            delivery.status = "failed"
            delivery.attempt_count += 1
            delivery.last_error_code = "WORKER_EXCEPTION"
            delivery.provider_response = {"error": str(e)}
            if delivery.attempt_count >= max_retry:
                delivery.status = "dead"
                delivery.next_attempt_at = None
            else:
                delivery.next_attempt_at = _compute_retry_next_attempt_at(
                    now, delivery.attempt_count,
                )

    await db.flush()
    return success_count


async def get_pending_delivery_count(db: AsyncSession) -> int:
    """获取待处理（pending + retrying 且到时间）的投递记录数（监控用）。"""
    from sqlalchemy import func

    now = datetime.now(_CST)
    stmt = select(func.count(MessageDelivery.id)).where(
        or_(
            MessageDelivery.status == "pending",
            and_(
                MessageDelivery.status == "retrying",
                or_(
                    MessageDelivery.next_attempt_at.is_(None),
                    MessageDelivery.next_attempt_at <= now,
                ),
            ),
        ),
    )
    result = await db.execute(stmt)
    return int(result.scalar() or 0)


# 兼容旧名：process_notification_outbox 已废弃，保留别名供过渡
process_notification_outbox = process_pending_deliveries


async def get_pending_notification_count(db: AsyncSession) -> int:
    """兼容旧名的 pending 数量查询（监控用）。"""
    return await get_pending_delivery_count(db)


if __name__ == "__main__":
    # 自测入口：验证函数签名与静默时段判断（不连 DB，无副作用）
    import inspect

    for fn in (
        process_pending_deliveries,
        get_pending_delivery_count,
        process_notification_outbox,
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

    # 测试重试退避
    t7 = datetime(2026, 6, 18, 10, 0, tzinfo=_CST)
    retry_at = _compute_retry_next_attempt_at(t7, attempt_count=1)
    assert retry_at >= t7 + timedelta(seconds=_RETRY_BACKOFF_BASE)

    print(f"quiet_hours_start={DEFAULT_QUIET_HOURS_START}")
    print(f"quiet_hours_end={DEFAULT_QUIET_HOURS_END}")
    print("OK")
