"""StrategyEvent 仓储 - 策略事件的幂等写入与查询（M4）。

提供：
- write_event: 幂等写入策略事件（ON CONFLICT event_key DO NOTHING）+ 快照冻结
- query_events: 按多条件查询事件
- get_event: 查询事件详情（含 snapshot）

设计说明：
- write_event 通过 ON CONFLICT (event_key) DO NOTHING 实现幂等：相同 event_key 不重复写入。
- snapshot 冻结事件发生时的完整上下文（bars/state/metrics）为 JSONB，用于证据回溯。
- 禁异常吞没：所有异常补充上下文后 re-raise。
- 查询使用索引 ix_strategy_event_symbol_time（instrument_id + event_time DESC）。

Inputs:
    session: AsyncSession
    event: StrategyEventDraft 或等价字典（含 event_key/event_type/payload/snapshot 等）

How to Run:
    python -m app.repositories.strategy_event_repository    # 自测：验证函数签名（不连 DB）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import select

from app.models.strategy_event import StrategyEvent

logger = logging.getLogger("strategy_event_repository")

# 事件 envelope schema 版本（对齐 strategy_event.schema.json）
EVENT_SCHEMA_VERSION = 1


async def write_event(
    session: AsyncSession,
    *,
    event_key: str,
    strategy_version_id: UUID,
    instrument_id: UUID,
    event_type: str,
    event_time: datetime,
    payload: dict[str, Any],
    snapshot: dict[str, Any] | None = None,
    logical_entity_id: str | None = None,
) -> StrategyEvent | None:
    """幂等写入策略事件。

    使用 ON CONFLICT (event_key) DO NOTHING：
    - event_key 不存在则插入，返回新对象
    - event_key 已存在则跳过（幂等），返回 None

    Args:
        session: 异步会话
        event_key: 事件唯一键（幂等去重）
        strategy_version_id: 策略版本 ID
        instrument_id: 股票 ID
        event_type: 事件类型
        event_time: 事件发生时间（bar 时间）
        payload: 事件负载 JSONB（自包含，不依赖外部状态）
        snapshot: 事件发生时上下文快照 JSONB（bars/state/metrics 冻结），None 则为空 dict
        logical_entity_id: 逻辑实体（如 instrument_id 字符串），可空

    Returns:
        新写入的 StrategyEvent 对象；若 event_key 已存在（跳过）则返回 None

    Raises:
        Exception: 写入失败时补充上下文后 re-raise
    """
    snapshot_data = snapshot if snapshot is not None else {}

    stmt = (
        pg_insert(StrategyEvent)
        .values(
            event_key=event_key,
            strategy_version_id=strategy_version_id,
            instrument_id=instrument_id,
            event_type=event_type,
            event_time=event_time,
            logical_entity_id=logical_entity_id,
            schema_version=EVENT_SCHEMA_VERSION,
            payload=payload,
            snapshot=snapshot_data,
        )
        .on_conflict_do_nothing(index_elements=["event_key"])
        .returning(StrategyEvent)
    )

    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "写入 strategy_event 失败 event_key=%s event_type=%s: %s",
            event_key, event_type, exc,
        )
        raise

    row = result.scalar_one_or_none()
    if row is not None:
        logger.info(
            "写入 strategy_event: event_key=%s event_type=%s event_time=%s",
            event_key, event_type, event_time,
        )
    else:
        logger.debug(
            "strategy_event 已存在（幂等跳过）: event_key=%s", event_key,
        )
    return row


async def query_events(
    session: AsyncSession,
    *,
    instrument_id: UUID | None = None,
    strategy_version_id: UUID | None = None,
    event_type: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = 100,
) -> list[StrategyEvent]:
    """按多条件查询策略事件。

    所有条件均为可选，按 AND 组合。结果按 event_time 倒序。

    Args:
        session: 异步会话
        instrument_id: 按股票过滤
        strategy_version_id: 按策略版本过滤
        event_type: 按事件类型过滤
        start_time: 事件时间 >= start_time
        end_time: 事件时间 <= end_time
        limit: 最大返回数（默认 100）

    Returns:
        StrategyEvent 列表（按 event_time 倒序）

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = select(StrategyEvent)
    if instrument_id is not None:
        stmt = stmt.where(StrategyEvent.instrument_id == instrument_id)
    if strategy_version_id is not None:
        stmt = stmt.where(StrategyEvent.strategy_version_id == strategy_version_id)
    if event_type is not None:
        stmt = stmt.where(StrategyEvent.event_type == event_type)
    if start_time is not None:
        stmt = stmt.where(StrategyEvent.event_time >= start_time)
    if end_time is not None:
        stmt = stmt.where(StrategyEvent.event_time <= end_time)
    stmt = stmt.order_by(StrategyEvent.event_time.desc()).limit(limit)

    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning("查询 strategy_event 列表失败: %s", exc)
        raise
    return list(result.scalars().all())


async def get_event(
    session: AsyncSession,
    event_id: UUID,
) -> StrategyEvent | None:
    """查询事件详情（含 snapshot）。

    Args:
        session: 异步会话
        event_id: 事件 ID

    Returns:
        StrategyEvent 对象或 None（不存在时）

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = select(StrategyEvent).where(StrategyEvent.id == event_id)
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning("查询 strategy_event 详情失败 event_id=%s: %s", event_id, exc)
        raise
    return result.scalar_one_or_none()


if __name__ == "__main__":
    # 自测入口：验证函数签名与可调用性（不连 DB，无副作用）
    import inspect

    for fn in (write_event, query_events, get_event):
        assert inspect.iscoroutinefunction(fn), f"{fn.__name__} 应为协程函数"
        print(f"{fn.__name__} params={list(inspect.signature(fn).parameters.keys())}")
    print(f"EVENT_SCHEMA_VERSION={EVENT_SCHEMA_VERSION}")
    print("OK")
