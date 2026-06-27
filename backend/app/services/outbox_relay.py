"""Outbox Relay - 事务性发件箱轮询投递。

设计（At-least-once 投递）：
1. 业务写入与 Outbox 记录在同一 DB 事务中（保证一致性）
2. Relay worker 轮询 outbox 表 status=pending 的记录
3. 普通事件：投递到 Redis 队列（LPUSH）
4. notification.message.created 事件：
   - 为每个活跃渠道创建 MessageDelivery(pending)
   - 不直接调用渠道 Adapter 投递
   - 创建完成后标记 Outbox 为 processed
5. 投递成功后标记 status=processed，记录 processed_at
6. 投递失败则 retry_count + 1，保持 pending 状态等待下次轮询

幂等保证：
- 下游消费者通过 idempotency_key 去重
- Outbox 记录的 id 作为幂等键的一部分
- MessageDelivery.idempotency_key 唯一，避免重复投递

Redis 队列键：outbox:queue:{event_type}
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis_client import get_redis
from app.models.notification import MessageDelivery, NotificationChannel
from app.models.outbox import Outbox

logger = logging.getLogger("outbox_relay")

# Redis Outbox 队列前缀
_OUTBOX_QUEUE_PREFIX = "outbox:queue:"

# 单次轮询最大记录数
DEFAULT_BATCH_SIZE = 100

# 最大重试次数（超过则标记 failed）
DEFAULT_MAX_RETRY = 5

# 通知事件类型，由本模块负责扩张为 MessageDelivery
_NOTIFICATION_EVENT_TYPE = "notification.message.created"


async def write_outbox(
    db: AsyncSession,
    event_type: str,
    payload: dict[str, Any],
    aggregate_type: str,
    aggregate_id: UUID | None = None,
    headers: dict[str, Any] | None = None,
) -> Outbox:
    """写入 outbox 记录（与业务写入同事务）。

    Args:
        db: 异步会话
        event_type: 事件类型（如 selector.run.completed）
        payload: 事件负载
        aggregate_type: 聚合根类型（如 strategy_run）
        aggregate_id: 聚合根 ID（可空）
        headers: 事件头（如 trace_id, tenant_id）

    Returns:
        Outbox 记录
    """
    if not event_type:
        raise ValueError("event_type 不能为空")
    if not aggregate_type:
        raise ValueError("aggregate_type 不能为空")

    outbox = Outbox(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
        headers=headers or {},
        status="pending",
        retry_count=0,
    )
    db.add(outbox)
    await db.flush()
    return outbox


async def _expand_notification_message_created(
    db: AsyncSession,
    record: Outbox,
) -> int:
    """将 notification.message.created 事件扩张为 MessageDelivery 记录。

    [飞书两段式投递] - 流程：
    1. 解析 payload 中的 message_id / user_id / delivery_type / image_url / message_group_id
    2. 若 payload 包含 target_channel_id，则只查询该目标渠道；否则查询用户全部活跃渠道
    3. 为每个匹配渠道创建 MessageDelivery(pending)
       - delivery_type 默认 'text'（飞书两段式投递默认文本）
       - message_group_id 从 payload 读取（关联同一事件的 text+image 两条投递）
    4. 幂等键基于 message_id + channel_id + delivery_type + image_url

    monitor_batch_service 写入 Outbox 时：
    - 文本 Outbox: delivery_type='text', message_group_id=<batch_group_id>
    - 图片 Outbox: delivery_type='image', message_group_id=<batch_group_id>, image_url=<capture_url>
    两条 Outbox 共享同一 message_group_id，outbox_relay 分别扩张为 text/image delivery.

    stock_detail_feishu_service 手动发送时：
    - 文本/图片 Outbox 均携带 target_channel_id，确保只创建一条目标投递。

    Args:
        db: 异步会话
        record: notification.message.created 的 Outbox 记录

    Returns:
        创建的 MessageDelivery 数量
    """
    payload: dict[str, Any] = record.payload or {}
    message_id_str = payload.get("message_id")
    user_id_str = payload.get("user_id")
    # [飞书两段式投递] - 默认 text（不再默认 card）
    delivery_type = payload.get("delivery_type", "text")
    image_url = payload.get("image_url")
    # 消息组 ID：关联同一事件的 text+image 两条投递记录
    message_group_id = payload.get("message_group_id")

    if not message_id_str or not user_id_str:
        logger.warning(
            "通知事件缺少 message_id/user_id: outbox_id=%s payload=%s",
            record.id, payload,
        )
        return 0

    try:
        message_id = UUID(message_id_str)
        user_id = UUID(user_id_str)
    except ValueError as e:
        logger.warning(
            "通知事件 message_id/user_id 格式非法: outbox_id=%s: %s",
            record.id, e,
        )
        return 0

    # 解析目标渠道 ID（手动发送场景）
    target_channel_id_str = payload.get("target_channel_id")
    target_channel_id: UUID | None = None
    if target_channel_id_str:
        try:
            target_channel_id = UUID(target_channel_id_str)
        except ValueError as e:
            logger.warning(
                "通知事件 target_channel_id 格式非法: outbox_id=%s: %s",
                record.id, e,
            )
            return 0

    # 查询用户活跃渠道：有 target_channel_id 时只查该渠道，否则查全部
    stmt = (
        select(NotificationChannel)
        .where(
            NotificationChannel.user_id == user_id,
            NotificationChannel.status == "active",
        )
        .order_by(NotificationChannel.created_at.desc())
    )
    if target_channel_id is not None:
        stmt = stmt.where(NotificationChannel.id == target_channel_id)
    result = await db.execute(stmt)
    channels = list(result.scalars().all())

    if not channels:
        logger.info(
            "无匹配活跃渠道，跳过扩张: user_id=%s message_id=%s target_channel_id=%s",
            user_id, message_id, target_channel_id,
        )
        return 0

    created = 0
    for channel in channels:
        # 幂等键：message_id|channel_id|delivery_type|image_url
        idem_parts = [str(message_id), str(channel.id), delivery_type]
        if image_url:
            idem_parts.append(image_url)
        idem_key = hashlib.sha256("|".join(idem_parts).encode()).hexdigest()

        delivery = MessageDelivery(
            id=uuid4(),
            notification_message_id=message_id,
            channel_id=channel.id,
            status="pending",
            delivery_type=delivery_type,
            attempt_count=0,
            image_url=image_url,
            message_group_id=message_group_id,
            idempotency_key=idem_key,
        )
        db.add(delivery)
        created += 1

    logger.info(
        "通知事件扩张完成: outbox_id=%s message_id=%s channels=%s "
        "delivery_type=%s message_group_id=%s",
        record.id, message_id, len(channels), delivery_type, message_group_id,
    )
    return created


async def relay_outbox(
    db: AsyncSession,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_retry: int = DEFAULT_MAX_RETRY,
) -> int:
    """轮询 outbox 表，投递 pending 记录。

    At-least-once 投递：
    - 普通事件 -> 投递到 Redis 队列，成功后 status=processed
    - notification.message.created -> 扩张为 MessageDelivery(pending)，然后 status=processed
    - 投递失败 -> retry_count+1，超过 max_retry 则 status=failed

    Args:
        db: 异步会话
        batch_size: 单次轮询最大记录数
        max_retry: 最大重试次数

    Returns:
        本次成功处理的记录数
    """
    if batch_size <= 0:
        raise ValueError("batch_size 必须大于 0")
    if max_retry <= 0:
        raise ValueError("max_retry 必须大于 0")

    redis = get_redis()

    # 1. 查询 pending 记录（按创建时间排序，FIFO）
    stmt = (
        select(Outbox)
        .where(Outbox.status == "pending")
        .order_by(Outbox.created_at)
        .limit(batch_size)
    )
    result = await db.execute(stmt)
    pending_records = list(result.scalars().all())

    if not pending_records:
        return 0

    processed_count = 0
    for record in pending_records:
        try:
            if record.event_type == _NOTIFICATION_EVENT_TYPE:
                # 通知事件：扩张为 MessageDelivery(pending)，不直接投递
                expanded = await _expand_notification_message_created(db, record)
                record.status = "processed"
                record.processed_at = datetime.now(UTC)
                processed_count += 1
                logger.info(
                    "outbox 通知事件已扩张: outbox_id=%s expanded=%s",
                    record.id, expanded,
                )
            else:
                # 普通事件：投递到 Redis 队列
                message = {
                    "outbox_id": str(record.id),
                    "event_type": record.event_type,
                    "aggregate_type": record.aggregate_type,
                    "aggregate_id": str(record.aggregate_id) if record.aggregate_id else None,
                    "payload": record.payload,
                    "headers": record.headers,
                    "created_at": record.created_at.isoformat(),
                }
                queue_key = f"{_OUTBOX_QUEUE_PREFIX}{record.event_type}"
                await redis.lpush(queue_key, json.dumps(message, ensure_ascii=False))

                record.status = "processed"
                record.processed_at = datetime.now(UTC)
                processed_count += 1
        except Exception as e:
            # 处理失败：增加重试计数，超过阈值标记 failed
            # 补充上下文后继续（不 re-raise，因为单条失败不应阻塞其他记录）
            record.retry_count += 1
            if record.retry_count >= max_retry:
                record.status = "failed"
            logger.warning(
                "outbox 处理失败 outbox_id=%s event_type=%s retry=%s: %s",
                record.id, record.event_type, record.retry_count, e,
            )

    await db.flush()
    return processed_count


async def get_pending_count(db: AsyncSession) -> int:
    """获取 pending 状态的 outbox 记录数（监控用）。"""
    from sqlalchemy import func

    stmt = select(func.count(Outbox.id)).where(Outbox.status == "pending")
    result = await db.execute(stmt)
    return int(result.scalar() or 0)


if __name__ == "__main__":
    # 自测入口：验证函数可导入（不连接 Redis/DB）
    print(f"write_outbox={write_outbox}")
    print(f"relay_outbox={relay_outbox}")
    print(f"get_pending_count={get_pending_count}")
    print(f"queue prefix={_OUTBOX_QUEUE_PREFIX}")
    print(f"batch_size={DEFAULT_BATCH_SIZE}")
    print(f"max_retry={DEFAULT_MAX_RETRY}")
    print("OK")
