"""Outbox Relay - 事务性发件箱轮询投递。

设计（At-least-once 投递）：
1. 业务写入与 Outbox 记录在同一 DB 事务中（保证一致性）
2. Relay worker 轮询 outbox 表 status=pending 的记录
3. 投递到 Redis 队列（LPUSH）
4. 投递成功后标记 status=processed，记录 processed_at
5. 投递失败则 retry_count + 1，保持 pending 状态等待下次轮询

幂等保证：
- 下游消费者通过 idempotency_key 去重
- Outbox 记录的 id 作为幂等键的一部分

Redis 队列键：outbox:queue:{event_type}
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis_client import get_redis
from app.models.outbox import Outbox

logger = logging.getLogger("outbox_relay")

# Redis Outbox 队列前缀
_OUTBOX_QUEUE_PREFIX = "outbox:queue:"

# 单次轮询最大记录数
DEFAULT_BATCH_SIZE = 100

# 最大重试次数（超过则标记 failed）
DEFAULT_MAX_RETRY = 5


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


async def relay_outbox(
    db: AsyncSession,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_retry: int = DEFAULT_MAX_RETRY,
) -> int:
    """轮询 outbox 表，投递 pending 记录到 Redis 队列。

    At-least-once 投递：
    - 投递成功 -> status=processed
    - 投递失败 -> retry_count+1，超过 max_retry 则 status=failed

    Args:
        db: 异步会话
        batch_size: 单次轮询最大记录数
        max_retry: 最大重试次数

    Returns:
        本次成功投递的记录数
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

    delivered_count = 0
    for record in pending_records:
        try:
            # 2. 投递到 Redis 队列
            # 队列消息包含完整事件信息，消费者可幂等处理
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

            # 3. 标记为 processed
            record.status = "processed"
            record.processed_at = datetime.now(UTC)
            delivered_count += 1
        except Exception as e:
            # 投递失败：增加重试计数，超过阈值标记 failed
            # 补充上下文后继续（不 re-raise，因为单条失败不应阻塞其他记录）
            record.retry_count += 1
            if record.retry_count >= max_retry:
                record.status = "failed"
            logger.warning(
                "outbox 投递失败 outbox_id=%s event_type=%s retry=%s: %s",
                record.id, record.event_type, record.retry_count, e,
            )

    await db.flush()
    return delivered_count


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
