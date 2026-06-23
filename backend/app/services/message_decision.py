"""C9 消息决策服务 - 消费监控组合事件，转换为统一 NotificationMessage。

[LEGACY] 本模块已从主业务流程中移除，仅保留代码以备参考。

设计：
- decide_monitoring_message: 消费 CompositeMonitorEvent → MONITORING_PLAN_CONFIRMED / MONITOR_MEMBER_EVENT DTO
- decide_and_create: 决策 + 创建 NotificationMessage（幂等，含 Outbox 事件写入）

消费流程：
1. 监控组合事件确认 → decide_monitoring_message → MONITORING_PLAN_CONFIRMED
2. 监控单成员事件 → decide_monitoring_message → MONITOR_MEMBER_EVENT

注：选股组合消息决策（decide_selection_message）已随 SelectionPlan 架构弃用而移除。

幂等保证：
- NotificationMessage.idempotency_key 唯一，相同来源不重复创建
- Outbox 事件记录在同一事务中写入

Inputs:
    db: AsyncSession
    composite_event_id: UUID

How to Run:
    python -m app.services.message_decision    # 自测：验证函数签名（不连 DB）
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.composite_event import CompositeMonitorEvent
from app.models.instrument import Instrument
from app.models.monitoring_plan import MonitoringPlan, MonitoringPlanRevision
from app.repositories.composite_event_repository import (
    get_composite_event,
    list_evidence_by_composite_event,
)
from app.schemas.notification import NotificationMessageDTO
from app.services.message_builder import (
    build_monitor_member_event,
    build_monitoring_plan_confirmed,
)

logger = logging.getLogger("message_decision")


async def _get_instrument_name(db: AsyncSession, instrument_id: UUID) -> str:
    """查询股票名称（用于消息展示）。

    Args:
        db: 异步会话
        instrument_id: 股票 ID

    Returns:
        股票名称（如 "贵州茅台"），未找到返回 instrument_id 前 8 位
    """
    stmt = select(Instrument.symbol, Instrument.name).where(
        Instrument.id == instrument_id
    )
    result = await db.execute(stmt)
    row = result.first()
    if row is None:
        return str(instrument_id)[:8]
    return f"{row.name}({row.symbol})"



async def decide_monitoring_message(
    db: AsyncSession,
    composite_event_id: UUID,
) -> NotificationMessageDTO | None:
    """消费监控组合事件，构建 MONITORING_PLAN_CONFIRMED 或 MONITOR_MEMBER_EVENT 消息。

    流程：
    1. 查询 CompositeMonitorEvent
    2. 查询关联的 MonitoringPlan + MonitoringPlanRevision
    3. 查询证据链
    4. 查询股票名称
    5. 根据 event_type 构建对应消息 DTO

    event_type 映射：
    - composite_confirmed → MONITORING_PLAN_CONFIRMED（ALL 模式全部确认）
    - composite_triggered_any → MONITORING_PLAN_CONFIRMED（ANY 模式首事件触发）
    - composite_triggered_independent → MONITOR_MEMBER_EVENT（INDEPENDENT 单成员）
    - composite_vetoed → MONITOR_MEMBER_EVENT（VETO 否决）

    Args:
        db: 异步会话
        composite_event_id: 组合事件 ID

    Returns:
        NotificationMessageDTO 或 None（事件不存在）

    Raises:
        Exception: 查询失败时补充上下文后 re-raise
    """
    # 1. 查询组合事件
    event = await get_composite_event(db, composite_event_id)
    if event is None:
        logger.warning("组合事件不存在: composite_event_id=%s", composite_event_id)
        return None

    # 2. 查询关联方案与版本
    stmt_plan = (
        select(MonitoringPlan, MonitoringPlanRevision)
        .join(
            MonitoringPlanRevision,
            MonitoringPlanRevision.id == event.revision_id,
        )
        .where(MonitoringPlan.id == event.monitoring_plan_id)
    )
    result_plan = await db.execute(stmt_plan)
    plan_row = result_plan.first()
    if plan_row is None:
        logger.warning(
            "监控方案不存在: plan_id=%s revision_id=%s",
            event.monitoring_plan_id, event.revision_id,
        )
        return None
    plan, revision = plan_row[0], plan_row[1]

    # 3. 查询证据链
    evidence_list = await list_evidence_by_composite_event(db, composite_event_id)

    # 4. 查询股票名称
    stock_name = await _get_instrument_name(db, event.instrument_id)

    # 5. 从 payload 提取 member_count
    member_count = event.payload.get("member_count", len(evidence_list))

    # 6. 构建时间线（从证据链）
    timeline: list[dict] = []
    for ev in evidence_list:
        ev_summary = ev.summary or {}
        timeline.append({
            "time": ev_summary.get("event_time", event.event_time.isoformat()),
            "label": ev_summary.get("event_type", "unknown"),
            "strategy_version_id": ev_summary.get("strategy_version_id"),
        })

    # 7. 根据 event_type 构建对应消息
    resource_refs = {
        "instrument_id": str(event.instrument_id),
        "plan_id": str(plan.id),
        "event_id": str(event.id),
        "revision_id": str(revision.id),
    }

    data_time = event.event_time.isoformat()

    if event.event_type in ("composite_confirmed", "composite_triggered_any"):
        # 组合确认消息
        window_minutes = revision.confirmation_window_seconds // 60

        # current_price 暂从 payload 提取（若无则为 0.0）
        current_price = float(
            event.payload.get("state", {}).get("current_price", 0.0)
        )

        dto = build_monitoring_plan_confirmed(
            stock_name=stock_name,
            confirmed_count=member_count,
            total_count=member_count,  # confirmed 时全部已确认
            window_minutes=window_minutes,
            timeline=timeline,
            current_price=current_price,
            resource_refs=resource_refs,
            data_time=data_time,
        )
    else:
        # 单成员事件（composite_triggered_independent / composite_vetoed）
        # 取第一条证据作为成员信息
        first_ev = evidence_list[0] if evidence_list else None
        ev_summary = first_ev.summary if first_ev else {}

        dto = build_monitor_member_event(
            stock_name=stock_name,
            event_type=ev_summary.get("event_type", event.event_type),
            event_time=data_time,
            member_name=plan.name,
            role=event.event_type,  # 使用 event_type 作为角色标识
            summary_text=f"{plan.name} 触发 {event.event_type}",
            resource_refs=resource_refs,
        )

    logger.info(
        "监控消息决策完成: composite_event_id=%s event_type=%s stock=%s",
        composite_event_id, event.event_type, stock_name,
    )
    return dto



async def decide_and_create_monitoring(
    db: AsyncSession,
    composite_event_id: UUID,
    user_id: UUID,
) -> UUID | None:
    """决策监控消息并创建 NotificationMessage（含 Outbox 事件）。

    流程：
    1. decide_monitoring_message → DTO
    2. notification_service.create_message（幂等）
    3. write_outbox 事件（通知投递 worker 消费）

    Args:
        db: 异步会话
        composite_event_id: 组合事件 ID
        user_id: 用户 ID

    Returns:
        NotificationMessage ID 或 None（无消息可创建）
    """
    from app.services.notification_service import create_message
    from app.services.outbox_relay import write_outbox

    dto = await decide_monitoring_message(db, composite_event_id)
    if dto is None:
        return None

    message = await create_message(
        db=db,
        user_id=user_id,
        message_dto=dto,
        source_type="composite_monitor_event",
        source_id=composite_event_id,
    )

    # 写入 Outbox 事件（通知投递 worker 消费）
    await write_outbox(
        db=db,
        event_type="notification.message.created",
        payload={
            "message_id": str(message.id),
            "user_id": str(user_id),
            "message_type": dto.message_type,
            "source_type": "composite_monitor_event",
            "source_id": str(composite_event_id),
        },
        aggregate_type="notification_message",
        aggregate_id=message.id,
    )

    logger.info(
        "监控消息已创建: message_id=%s composite_event_id=%s",
        message.id, composite_event_id,
    )
    return message.id


if __name__ == "__main__":
    # 自测入口：验证函数签名（不连 DB，无副作用）
    import inspect

    for fn in (
        decide_monitoring_message,
        decide_and_create_monitoring,
        _get_instrument_name,
    ):
        assert inspect.iscoroutinefunction(fn), f"{fn.__name__} 应为协程函数"
        print(f"{fn.__name__} params={list(inspect.signature(fn).parameters.keys())}")
    print("OK")
