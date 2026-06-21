"""MonitorState 仓储 - 监控状态的幂等写入与查询（M3）。

提供：
- upsert_state: 幂等写入监控状态（ON CONFLICT 复合主键 DO UPDATE）
- get_state: 查询单条监控状态
- list_states_by_instrument: 查询某股票的所有监控策略状态
- list_states_by_strategy_version: 查询某策略版本的所有股票状态

设计说明：
- upsert 通过 PostgreSQL ON CONFLICT (strategy_version_id, instrument_id) DO UPDATE 实现幂等。
- 查询使用 selectin 策略避免 N+1。
- 禁异常吞没：所有异常补充上下文后 re-raise。

Inputs:
    session: AsyncSession
    instrument_id / strategy_version_id: UUID
    payload: 状态字典
    bar_time / calculation_id / state_schema_version: 状态元数据

How to Run:
    python -m app.repositories.monitor_state_repository    # 自测：验证函数签名（不连 DB）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import select

from app.models.monitor_state import MonitorState

logger = logging.getLogger("monitor_state_repository")


async def upsert_state(
    session: AsyncSession,
    instrument_id: UUID,
    strategy_version_id: UUID,
    payload: dict[str, Any],
    bar_time: datetime,
    calculation_id: str,
    state_schema_version: int,
) -> MonitorState:
    """幂等写入监控状态。

    使用 ON CONFLICT (strategy_version_id, instrument_id) DO UPDATE：
    - 不存在则插入
    - 已存在则更新 payload/bar_time/calculation_id/state_schema_version/updated_at

    Args:
        session: 异步会话
        instrument_id: 股票 ID
        strategy_version_id: 策略版本 ID
        payload: 监控状态字典
        bar_time: 触发该状态的 bar 时间
        calculation_id: 计算批次 ID（幂等标识）
        state_schema_version: 状态 schema 版本

    Returns:
        写入后的 MonitorState 对象

    Raises:
        Exception: 写入失败时补充上下文后 re-raise
    """
    stmt = pg_insert(MonitorState).values(
        strategy_version_id=strategy_version_id,
        instrument_id=instrument_id,
        bar_time=bar_time,
        calculation_id=calculation_id,
        state_schema_version=state_schema_version,
        payload=payload,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["strategy_version_id", "instrument_id"],
        set_={
            "bar_time": stmt.excluded.bar_time,
            "calculation_id": stmt.excluded.calculation_id,
            "state_schema_version": stmt.excluded.state_schema_version,
            "payload": stmt.excluded.payload,
            "updated_at": stmt.excluded.updated_at,
        },
    ).returning(MonitorState)

    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "upsert monitor_state 失败 instrument_id=%s strategy_version_id=%s: %s",
            instrument_id, strategy_version_id, exc,
        )
        raise

    row = result.scalar_one()
    logger.info(
        "upsert monitor_state: instrument_id=%s strategy_version_id=%s bar_time=%s",
        instrument_id, strategy_version_id, bar_time,
    )
    return row


async def get_state(
    session: AsyncSession,
    instrument_id: UUID,
    strategy_version_id: UUID,
) -> MonitorState | None:
    """查询单条监控状态。

    Args:
        session: 异步会话
        instrument_id: 股票 ID
        strategy_version_id: 策略版本 ID

    Returns:
        MonitorState 对象或 None（不存在时）

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = select(MonitorState).where(
        MonitorState.instrument_id == instrument_id,
        MonitorState.strategy_version_id == strategy_version_id,
    )
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "查询 monitor_state 失败 instrument_id=%s strategy_version_id=%s: %s",
            instrument_id, strategy_version_id, exc,
        )
        raise
    return result.scalar_one_or_none()


async def list_states_by_instrument(
    session: AsyncSession,
    instrument_id: UUID,
) -> list[MonitorState]:
    """查询某股票的所有监控策略状态。

    Args:
        session: 异步会话
        instrument_id: 股票 ID

    Returns:
        MonitorState 列表（按 updated_at 倒序）

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = (
        select(MonitorState)
        .where(MonitorState.instrument_id == instrument_id)
        .order_by(MonitorState.updated_at.desc())
    )
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "查询 monitor_state 列表失败 instrument_id=%s: %s",
            instrument_id, exc,
        )
        raise
    return list(result.scalars().all())


async def list_states_by_strategy_version(
    session: AsyncSession,
    strategy_version_id: UUID,
) -> list[MonitorState]:
    """查询某策略版本的所有股票状态。

    Args:
        session: 异步会话
        strategy_version_id: 策略版本 ID

    Returns:
        MonitorState 列表（按 updated_at 倒序）

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = (
        select(MonitorState)
        .where(MonitorState.strategy_version_id == strategy_version_id)
        .order_by(MonitorState.updated_at.desc())
    )
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "查询 monitor_state 列表失败 strategy_version_id=%s: %s",
            strategy_version_id, exc,
        )
        raise
    return list(result.scalars().all())


if __name__ == "__main__":
    # 自测入口：验证函数签名与可调用性（不连 DB，无副作用）
    import inspect

    for fn in (upsert_state, get_state, list_states_by_instrument, list_states_by_strategy_version):
        assert inspect.iscoroutinefunction(fn), f"{fn.__name__} 应为协程函数"
        print(f"{fn.__name__} params={list(inspect.signature(fn).parameters.keys())}")
    print("OK")
