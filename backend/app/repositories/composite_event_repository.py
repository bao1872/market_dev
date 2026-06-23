"""CompositeMonitorEvent 仓储 - 组合监控事件与证据的幂等写入与查询（C8）。

[LEGACY] 本模块已从主业务流程中移除，仅保留代码以备参考。

提供：
- write_composite_event: 幂等写入组合事件（ON CONFLICT composite_event_key DO NOTHING）+ Evidence
- write_evidence: 写入证据（ON CONFLICT 复合主键 DO NOTHING）
- get_composite_event: 查询组合事件详情（含 evidence）
- list_composite_events_by_plan: 查询方案下的组合事件
- list_composite_events_by_instrument: 查询股票下的组合事件

设计说明：
- composite_event_key UNIQUE 约束保证幂等：相同 key 的组合事件不重复写入。
- Evidence 复合主键 (composite_event_id, member_id, strategy_event_id) 保证幂等。
- Evidence 冻结策略版本/事件类型/事件时间/摘要（即使原事件后续修改，证据不变）。
- 禁异常吞没：所有异常补充上下文后 re-raise。

Inputs:
    session: AsyncSession
    draft: CompositeEventDraft（来自 monitoring_correlator）
    evidence: EvidenceDraft 列表

How to Run:
    python -m app.repositories.composite_event_repository    # 自测：验证函数签名（不连 DB）
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.composite_event import (
    CompositeEventEvidence,
    CompositeMonitorEvent,
)
from app.services.monitoring_correlator import CompositeEventDraft, EvidenceDraft

logger = logging.getLogger("composite_event_repository")


async def write_composite_event(
    session: AsyncSession,
    *,
    draft: CompositeEventDraft,
    extra_evidence: list[EvidenceDraft] | None = None,
) -> CompositeMonitorEvent | None:
    """幂等写入组合事件 + Evidence。

    使用 ON CONFLICT (composite_event_key) DO NOTHING 实现幂等：
    - composite_event_key 不存在则插入，返回新对象
    - composite_event_key 已存在则跳过（幂等），返回 None

    Evidence 同步写入（ON CONFLICT 复合主键 DO NOTHING）。

    Args:
        session: 异步会话
        draft: 组合事件草稿（来自 monitoring_correlator）
        extra_evidence: 额外证据列表（如 ALL 模式下从 monitoring_state_evidence 表补全的证据）

    Returns:
        新写入的 CompositeMonitorEvent 对象；若 composite_event_key 已存在（跳过）则返回 None

    Raises:
        Exception: 写入失败时补充上下文后 re-raise
    """
    composite_event_key = draft.composite_event_key

    stmt = (
        pg_insert(CompositeMonitorEvent)
        .values(
            user_id=draft.user_id,
            monitoring_plan_id=draft.monitoring_plan_id,
            revision_id=draft.revision_id,
            instrument_id=draft.instrument_id,
            event_type=draft.event_type,
            event_time=draft.event_time,
            composite_event_key=composite_event_key,
            payload=draft.payload,
        )
        .on_conflict_do_nothing(index_elements=["composite_event_key"])
        .returning(CompositeMonitorEvent)
    )

    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "写入 composite_monitor_event 失败 key=%s event_type=%s: %s",
            composite_event_key, draft.event_type, exc,
        )
        raise

    row = result.scalar_one_or_none()
    if row is None:
        logger.debug(
            "composite_monitor_event 已存在（幂等跳过）: key=%s", composite_event_key,
        )
        return None

    logger.info(
        "写入 composite_monitor_event: key=%s event_type=%s event_time=%s member_count=%s",
        composite_event_key, draft.event_type, draft.event_time, draft.member_count,
    )

    # 写入 Evidence（合并 draft.evidence 与 extra_evidence）
    all_evidence = list(draft.evidence)
    if extra_evidence is not None:
        all_evidence.extend(extra_evidence)

    for ev in all_evidence:
        await write_evidence(session, composite_event_id=row.id, evidence=ev)

    return row


async def write_evidence(
    session: AsyncSession,
    *,
    composite_event_id: UUID,
    evidence: EvidenceDraft,
) -> CompositeEventEvidence | None:
    """幂等写入证据。

    使用 ON CONFLICT (composite_event_id, member_id, strategy_event_id) DO NOTHING 实现幂等。

    Evidence 冻结策略版本/事件类型/事件时间/摘要（即使原事件后续修改，证据不变）。

    Args:
        session: 异步会话
        composite_event_id: 组合事件 ID
        evidence: 证据草稿

    Returns:
        新写入的 CompositeEventEvidence 对象；若已存在（跳过）则返回 None

    Raises:
        Exception: 写入失败时补充上下文后 re-raise
    """
    stmt = (
        pg_insert(CompositeEventEvidence)
        .values(
            composite_event_id=composite_event_id,
            member_id=evidence.member_id,
            strategy_event_id=evidence.strategy_event_id,
            summary=evidence.summary,
        )
        .on_conflict_do_nothing(
            index_elements=["composite_event_id", "member_id", "strategy_event_id"]
        )
        .returning(CompositeEventEvidence)
    )

    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "写入 composite_event_evidence 失败 composite_event_id=%s member_id=%s: %s",
            composite_event_id, evidence.member_id, exc,
        )
        raise

    row = result.scalar_one_or_none()
    if row is not None:
        logger.info(
            "写入 composite_event_evidence: composite_event_id=%s member_id=%s strategy_event_id=%s",
            composite_event_id, evidence.member_id, evidence.strategy_event_id,
        )
    return row


async def get_composite_event(
    session: AsyncSession,
    event_id: UUID,
) -> CompositeMonitorEvent | None:
    """查询组合事件详情。

    Args:
        session: 异步会话
        event_id: 组合事件 ID

    Returns:
        CompositeMonitorEvent 对象或 None

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = select(CompositeMonitorEvent).where(CompositeMonitorEvent.id == event_id)
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning("查询 composite_monitor_event 失败 event_id=%s: %s", event_id, exc)
        raise
    return result.scalar_one_or_none()


async def list_evidence_by_composite_event(
    session: AsyncSession,
    composite_event_id: UUID,
) -> list[CompositeEventEvidence]:
    """查询组合事件的所有证据。

    Args:
        session: 异步会话
        composite_event_id: 组合事件 ID

    Returns:
        CompositeEventEvidence 列表

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = select(CompositeEventEvidence).where(
        CompositeEventEvidence.composite_event_id == composite_event_id
    )
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "查询 composite_event_evidence 失败 composite_event_id=%s: %s",
            composite_event_id, exc,
        )
        raise
    return list(result.scalars().all())


async def list_composite_events_by_plan(
    session: AsyncSession,
    *,
    monitoring_plan_id: UUID,
    user_id: UUID | None = None,
    event_type: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = 100,
) -> list[CompositeMonitorEvent]:
    """查询方案下的组合事件。

    Args:
        session: 异步会话
        monitoring_plan_id: 方案 ID
        user_id: 可选用户过滤
        event_type: 可选事件类型过滤
        start_time: 事件时间 >= start_time
        end_time: 事件时间 <= end_time
        limit: 最大返回数

    Returns:
        CompositeMonitorEvent 列表（按 event_time 倒序）

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = select(CompositeMonitorEvent).where(
        CompositeMonitorEvent.monitoring_plan_id == monitoring_plan_id
    )
    if user_id is not None:
        stmt = stmt.where(CompositeMonitorEvent.user_id == user_id)
    if event_type is not None:
        stmt = stmt.where(CompositeMonitorEvent.event_type == event_type)
    if start_time is not None:
        stmt = stmt.where(CompositeMonitorEvent.event_time >= start_time)
    if end_time is not None:
        stmt = stmt.where(CompositeMonitorEvent.event_time <= end_time)
    stmt = stmt.order_by(CompositeMonitorEvent.event_time.desc()).limit(limit)
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "查询 composite_monitor_event 列表失败 plan_id=%s: %s",
            monitoring_plan_id, exc,
        )
        raise
    return list(result.scalars().all())


async def list_composite_events_by_instrument(
    session: AsyncSession,
    *,
    instrument_id: UUID,
    user_id: UUID | None = None,
    monitoring_plan_id: UUID | None = None,
    event_type: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = 100,
) -> list[CompositeMonitorEvent]:
    """查询股票下的组合事件。

    Args:
        session: 异步会话
        instrument_id: 股票 ID
        user_id: 可选用户过滤
        monitoring_plan_id: 可选方案过滤
        event_type: 可选事件类型过滤
        start_time: 事件时间 >= start_time
        end_time: 事件时间 <= end_time
        limit: 最大返回数

    Returns:
        CompositeMonitorEvent 列表（按 event_time 倒序）

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    stmt = select(CompositeMonitorEvent).where(
        CompositeMonitorEvent.instrument_id == instrument_id
    )
    if user_id is not None:
        stmt = stmt.where(CompositeMonitorEvent.user_id == user_id)
    if monitoring_plan_id is not None:
        stmt = stmt.where(CompositeMonitorEvent.monitoring_plan_id == monitoring_plan_id)
    if event_type is not None:
        stmt = stmt.where(CompositeMonitorEvent.event_type == event_type)
    if start_time is not None:
        stmt = stmt.where(CompositeMonitorEvent.event_time >= start_time)
    if end_time is not None:
        stmt = stmt.where(CompositeMonitorEvent.event_time <= end_time)
    stmt = stmt.order_by(CompositeMonitorEvent.event_time.desc()).limit(limit)
    try:
        result = await session.execute(stmt)
    except Exception as exc:
        logger.warning(
            "查询 composite_monitor_event 列表失败 instrument_id=%s: %s",
            instrument_id, exc,
        )
        raise
    return list(result.scalars().all())


if __name__ == "__main__":
    # 自测入口：验证函数签名与可调用性（不连 DB，无副作用）
    import inspect

    for fn in (
        write_composite_event, write_evidence, get_composite_event,
        list_evidence_by_composite_event, list_composite_events_by_plan,
        list_composite_events_by_instrument,
    ):
        assert inspect.iscoroutinefunction(fn), f"{fn.__name__} 应为协程函数"
        print(f"{fn.__name__} params={list(inspect.signature(fn).parameters.keys())}")
    print("OK")
