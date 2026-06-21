"""MonitoringCorrelator - 监控组合事件关联状态机（C7）。

实现三种组合模式的事件关联状态机：
- INDEPENDENT: 每个成员独立触发，直接生成 CompositeEvent（member_count=1）
- ANY: 第一个符合事件确认即触发；VETO 在同一原子批次优先处理
- ALL: TRIGGER 打开窗口，累计 required CONFIRM 成员，VETO 取消窗口，超时过期

关键约束：
1. event_time 驱动：状态机不依赖墙钟，所有时间判断基于 event_time
2. watermark：处理乱序事件，watermark = max(event_time) - allowed_lateness
3. VETO 优先：VETO 事件在同一原子批次优先处理（取消窗口）
4. ordered：ALL 模式下 ordered=true 时按 order_index 顺序校验
5. 幂等：composite_event_key 唯一，重复事件不重复证据

状态机字段（MonitoringPlanState.status）：
- WAITING_TRIGGER: 等待 TRIGGER 事件（ALL 模式初始状态）
- WAITING_CONFIRM: 已收到 TRIGGER，等待 CONFIRM 事件
- CONFIRMED: 所有 required 成员已确认（终态）
- EXPIRED: 窗口超时未确认（终态）
- VETOED: 被 VETO 事件否决（终态）
- COOLDOWN: 冷却中（CONFIRMED 后进入）

Usage:
    from app.services.monitoring_correlator import (
        correlate_event, CorrelateResult, CompositeEventDraft,
    )

    result = correlate_event(
        event=strategy_event,
        plan_state=plan_state,
        revision=plan_revision,
        members=plan_members,
        watermark=event_time,
    )
    if result.composite_event:
        # 写入 CompositeMonitorEvent + Evidence
        ...

How to Run:
    python -m app.services.monitoring_correlator    # 自测：验证状态机逻辑
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from app.models.monitoring_plan import MonitoringPlanMember, MonitoringPlanRevision
from app.models.monitoring_plan_state import MonitoringPlanState
from app.models.strategy_event import StrategyEvent

logger = logging.getLogger("monitoring_correlator")

# 状态机常量
STATUS_WAITING_TRIGGER = "WAITING_TRIGGER"
STATUS_WAITING_CONFIRM = "WAITING_CONFIRM"
STATUS_CONFIRMED = "CONFIRMED"
STATUS_EXPIRED = "EXPIRED"
STATUS_VETOED = "VETOED"
STATUS_COOLDOWN = "COOLDOWN"

# 组合事件类型
EVENT_TYPE_INDEPENDENT = "composite_triggered_independent"
EVENT_TYPE_ANY = "composite_triggered_any"
EVENT_TYPE_ALL_CONFIRMED = "composite_confirmed"
EVENT_TYPE_VETOED = "composite_vetoed"


@dataclass
class EvidenceDraft:
    """证据草稿 - 引用原始 StrategyEvent 并冻结关键信息。

    即使原事件后续修改，证据不变（冻结策略版本/事件类型/事件时间/摘要）。
    """

    member_id: UUID
    strategy_event_id: UUID
    strategy_version_id: UUID
    event_type: str
    event_time: datetime
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompositeEventDraft:
    """组合事件草稿 - 状态机生成的组合事件，待写入 DB。

    composite_event_key 由 build_composite_event_key 计算，保证幂等。
    evidence 列表含每个成员的证据草稿。
    """

    user_id: UUID
    monitoring_plan_id: UUID
    revision_id: UUID
    instrument_id: UUID
    event_type: str
    event_time: datetime
    member_count: int
    state: dict[str, Any]
    evidence: list[EvidenceDraft] = field(default_factory=list)

    @property
    def composite_event_key(self) -> str:
        """计算组合事件唯一键（幂等去重）。"""
        return build_composite_event_key(
            revision_id=self.revision_id,
            instrument_id=self.instrument_id,
            event_type=self.event_type,
            event_time=self.event_time,
            member_ids=[e.member_id for e in self.evidence],
        )

    @property
    def payload(self) -> dict[str, Any]:
        """组合事件负载 JSONB（含 state/member_count/计算时间等）。"""
        return {
            "state": self.state,
            "member_count": self.member_count,
            "evidence_count": len(self.evidence),
        }


@dataclass
class CorrelateResult:
    """事件关联结果。

    - new_state: 更新后的状态字段（待写入 DB）
    - composite_event: 生成的组合事件草稿（None 表示未触发组合事件）
    - accepted: 事件是否被状态机接受（False 表示重复事件或被忽略）
    - reason: 接受/拒绝原因（用于日志与可解释性）
    """

    new_state: dict[str, Any]
    composite_event: CompositeEventDraft | None = None
    accepted: bool = False
    reason: str = ""


def build_composite_event_key(
    *,
    revision_id: UUID,
    instrument_id: UUID,
    event_type: str,
    event_time: datetime,
    member_ids: list[UUID],
) -> str:
    """构建组合事件唯一键（幂等去重）。

    格式: sha256({revision_id}|{instrument_id}|{event_type}|{event_time_iso}|{sorted_member_ids})

    相同 key 的组合事件不重复写入（UNIQUE 约束）。

    Args:
        revision_id: 方案版本 ID
        instrument_id: 股票 ID
        event_type: 组合事件类型
        event_time: 组合事件时间
        member_ids: 成员 ID 列表（排序后参与 hash）

    Returns:
        组合事件唯一键字符串（sha256 hex）
    """
    sorted_member_ids = sorted(str(m) for m in member_ids)
    raw = "|".join([
        str(revision_id),
        str(instrument_id),
        event_type,
        event_time.isoformat(),
        ",".join(sorted_member_ids),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_evidence_draft(
    event: StrategyEvent,
    member: MonitoringPlanMember,
) -> EvidenceDraft:
    """构建证据草稿（冻结策略版本/事件类型/事件时间/摘要）。"""
    summary = {
        "strategy_version_id": str(event.strategy_version_id),
        "event_type": event.event_type,
        "event_time": event.event_time.isoformat(),
        "payload_summary": dict(list(event.payload.items())[:5]),  # 取前 5 个字段作为摘要
    }
    return EvidenceDraft(
        member_id=member.id,
        strategy_event_id=event.id,
        strategy_version_id=event.strategy_version_id,
        event_type=event.event_type,
        event_time=event.event_time,
        summary=summary,
    )


def _find_member_by_event(
    event: StrategyEvent,
    members: list[MonitoringPlanMember],
) -> MonitoringPlanMember | None:
    """根据事件查找匹配的方案成员。

    匹配规则：
    - member.event_type == event.event_type
    - member.strategy_version_id == event.strategy_version_id（若 member 为 PINNED）
    - member.enabled == True

    Args:
        event: 原始策略事件
        members: 方案成员列表

    Returns:
        匹配的成员，或 None
    """
    for m in members:
        if not m.enabled:
            continue
        if m.event_type != event.event_type:
            continue
        # PINNED 模式必须版本匹配；STABLE_TRACK 模式接受任意版本
        if m.version_policy == "PINNED":
            if m.strategy_version_id is None:
                continue
            if m.strategy_version_id != event.strategy_version_id:
                continue
        return m
    return None


def _check_window_expired(
    state: MonitoringPlanState,
    watermark: datetime,
) -> bool:
    """检查窗口是否已超时（基于 watermark，非墙钟）。

    Args:
        state: 当前状态
        watermark: 当前 watermark（max(event_time) - allowed_lateness）

    Returns:
        True 表示窗口已超时
    """
    if state.window_deadline_at is None:
        return False
    return watermark > state.window_deadline_at


def _check_cooldown_expired(
    state: MonitoringPlanState,
    watermark: datetime,
) -> bool:
    """检查冷却是否已过期（基于 watermark，非墙钟）。

    Args:
        state: 当前状态
        watermark: 当前 watermark

    Returns:
        True 表示冷却已过期
    """
    if state.cooldown_until is None:
        return True
    return watermark > state.cooldown_until


def _check_ordered_position(
    member: MonitoringPlanMember,
    state: MonitoringPlanState,
    members: list[MonitoringPlanMember],
) -> bool:
    """检查 ordered 模式下成员顺序是否满足。

    ordered=true 时，CONFIRM 成员必须按 position 顺序确认：
    下一个确认的成员 position 应为已确认成员最大 position + 1。

    Args:
        member: 待确认成员
        state: 当前状态
        members: 方案成员列表

    Returns:
        True 表示顺序满足
    """
    confirmed_positions = {
        m.position for m in members
        if m.id in (state.confirmed_member_ids or [])
    }
    if not confirmed_positions:
        # 还没有确认任何成员，第一个确认的 position 应为最小（通常为 0）
        min_position = min(m.position for m in members if m.role in ("TRIGGER", "CONFIRM"))
        return member.position == min_position
    max_confirmed = max(confirmed_positions)
    return member.position == max_confirmed + 1


def correlate_independent(
    *,
    event: StrategyEvent,
    plan_state: MonitoringPlanState,
    revision: MonitoringPlanRevision,
    members: list[MonitoringPlanMember],
    watermark: datetime,
) -> CorrelateResult:
    """INDEPENDENT 模式事件关联。

    每个成员独立触发，直接生成 CompositeEvent（member_count=1）。
    不需要窗口/确认。

    Args:
        event: 原始策略事件
        plan_state: 当前状态
        revision: 方案版本
        members: 方案成员列表
        watermark: 当前 watermark

    Returns:
        关联结果（含组合事件草稿）
    """
    member = _find_member_by_event(event, members)
    if member is None:
        return CorrelateResult(
            new_state={},
            accepted=False,
            reason=f"INDEPENDENT: 事件 event_type={event.event_type} 无匹配成员",
        )

    # OBSERVE 角色不触发组合事件
    if member.role == "OBSERVE":
        return CorrelateResult(
            new_state={},
            accepted=True,
            reason=f"INDEPENDENT: OBSERVE 成员 position={member.position} 仅记录",
        )

    # 检查冷却（基于 watermark）
    if not _check_cooldown_expired(plan_state, watermark):
        return CorrelateResult(
            new_state={},
            accepted=False,
            reason=f"INDEPENDENT: 冷却中 cooldown_until={plan_state.cooldown_until}",
        )

    # 生成组合事件
    evidence = _build_evidence_draft(event, member)
    composite = CompositeEventDraft(
        user_id=plan_state.user_id,
        monitoring_plan_id=plan_state.monitoring_plan_id,
        revision_id=plan_state.revision_id,
        instrument_id=event.instrument_id,
        event_type=EVENT_TYPE_INDEPENDENT,
        event_time=event.event_time,
        member_count=1,
        state={
            "mode": "INDEPENDENT",
            "member_position": member.position,
            "member_role": member.role,
        },
        evidence=[evidence],
    )

    # 更新状态：进入冷却
    new_state = {
        "new_status": STATUS_COOLDOWN,
        "cooldown_until": event.event_time + timedelta(seconds=revision.cooldown_seconds),
        "state_payload": {
            "last_event_at": event.event_time.isoformat(),
            "last_composite_event_at": event.event_time.isoformat(),
        },
    }

    return CorrelateResult(
        new_state=new_state,
        composite_event=composite,
        accepted=True,
        reason=f"INDEPENDENT: 成员 position={member.position} 触发组合事件",
    )


def correlate_any(
    *,
    event: StrategyEvent,
    plan_state: MonitoringPlanState,
    revision: MonitoringPlanRevision,
    members: list[MonitoringPlanMember],
    watermark: datetime,
) -> CorrelateResult:
    """ANY 模式事件关联。

    第一个符合事件确认即触发；VETO 在同一原子批次优先处理。
    如 VETO 先到，取消等待。

    Args:
        event: 原始策略事件
        plan_state: 当前状态
        revision: 方案版本
        members: 方案成员列表
        watermark: 当前 watermark

    Returns:
        关联结果
    """
    member = _find_member_by_event(event, members)
    if member is None:
        return CorrelateResult(
            new_state={},
            accepted=False,
            reason=f"ANY: 事件 event_type={event.event_type} 无匹配成员",
        )

    # VETO 优先处理：取消等待
    if member.role == "VETO":
        evidence = _build_evidence_draft(event, member)
        composite = CompositeEventDraft(
            user_id=plan_state.user_id,
            monitoring_plan_id=plan_state.monitoring_plan_id,
            revision_id=plan_state.revision_id,
            instrument_id=event.instrument_id,
            event_type=EVENT_TYPE_VETOED,
            event_time=event.event_time,
            member_count=1,
            state={
                "mode": "ANY",
                "vetoed_by_member_id": str(member.id),
                "vetoed_by_position": member.position,
            },
            evidence=[evidence],
        )
        new_state = {
            "new_status": STATUS_VETOED,
            "vetoed_by_member_id": member.id,
            "clear_window": True,
            "state_payload": {
                "last_event_at": event.event_time.isoformat(),
                "vetoed_at": event.event_time.isoformat(),
            },
        }
        return CorrelateResult(
            new_state=new_state,
            composite_event=composite,
            accepted=True,
            reason=f"ANY: VETO 成员 position={member.position} 否决",
        )

    # OBSERVE 角色不触发
    if member.role == "OBSERVE":
        return CorrelateResult(
            new_state={},
            accepted=True,
            reason=f"ANY: OBSERVE 成员 position={member.position} 仅记录",
        )

    # 检查冷却
    if not _check_cooldown_expired(plan_state, watermark):
        return CorrelateResult(
            new_state={},
            accepted=False,
            reason=f"ANY: 冷却中 cooldown_until={plan_state.cooldown_until}",
        )

    # TRIGGER/CONFIRM 角色：第一个符合事件即触发
    evidence = _build_evidence_draft(event, member)
    composite = CompositeEventDraft(
        user_id=plan_state.user_id,
        monitoring_plan_id=plan_state.monitoring_plan_id,
        revision_id=plan_state.revision_id,
        instrument_id=event.instrument_id,
        event_type=EVENT_TYPE_ANY,
        event_time=event.event_time,
        member_count=1,
        state={
            "mode": "ANY",
            "triggered_by_member_id": str(member.id),
            "triggered_by_position": member.position,
            "triggered_by_role": member.role,
        },
        evidence=[evidence],
    )

    new_state = {
        "new_status": STATUS_COOLDOWN,
        "cooldown_until": event.event_time + timedelta(seconds=revision.cooldown_seconds),
        "state_payload": {
            "last_event_at": event.event_time.isoformat(),
            "last_composite_event_at": event.event_time.isoformat(),
        },
    }

    return CorrelateResult(
        new_state=new_state,
        composite_event=composite,
        accepted=True,
        reason=f"ANY: 成员 position={member.position} role={member.role} 触发组合事件",
    )


def correlate_all(
    *,
    event: StrategyEvent,
    plan_state: MonitoringPlanState,
    revision: MonitoringPlanRevision,
    members: list[MonitoringPlanMember],
    watermark: datetime,
) -> CorrelateResult:
    """ALL 模式事件关联状态机。

    状态机流程：
    1. TRIGGER 成员事件到达 → 打开窗口
       (window_started_at = event_time, window_deadline_at = event_time + confirmation_window_seconds)
    2. 等待 required CONFIRM 成员事件
    3. 全部确认后生成 CompositeEvent
    4. VETO 事件在同一原子批次优先处理（取消窗口）
    5. 超时未确认则状态转为 expired
    6. ordered=true 时按 order_index 顺序校验

    Args:
        event: 原始策略事件
        plan_state: 当前状态
        revision: 方案版本
        members: 方案成员列表
        watermark: 当前 watermark

    Returns:
        关联结果
    """
    member = _find_member_by_event(event, members)
    if member is None:
        return CorrelateResult(
            new_state={},
            accepted=False,
            reason=f"ALL: 事件 event_type={event.event_type} 无匹配成员",
        )

    # VETO 优先处理：取消窗口，进入 VETOED 终态
    if member.role == "VETO":
        evidence = _build_evidence_draft(event, member)
        composite = CompositeEventDraft(
            user_id=plan_state.user_id,
            monitoring_plan_id=plan_state.monitoring_plan_id,
            revision_id=plan_state.revision_id,
            instrument_id=event.instrument_id,
            event_type=EVENT_TYPE_VETOED,
            event_time=event.event_time,
            member_count=1,
            state={
                "mode": "ALL",
                "vetoed_by_member_id": str(member.id),
                "vetoed_by_position": member.position,
            },
            evidence=[evidence],
        )
        new_state = {
            "new_status": STATUS_VETOED,
            "vetoed_by_member_id": member.id,
            "clear_window": True,
            "state_payload": {
                "last_event_at": event.event_time.isoformat(),
                "vetoed_at": event.event_time.isoformat(),
            },
        }
        return CorrelateResult(
            new_state=new_state,
            composite_event=composite,
            accepted=True,
            reason=f"ALL: VETO 成员 position={member.position} 否决",
        )

    # 先检查窗口是否已超时（基于 watermark）
    if plan_state.status == STATUS_WAITING_CONFIRM and _check_window_expired(plan_state, watermark):
        # 窗口已超时，状态转为 EXPIRED（终态）
        new_state = {
            "new_status": STATUS_EXPIRED,
            "clear_window": True,
            "state_payload": {
                "last_event_at": event.event_time.isoformat(),
                "expired_at": watermark.isoformat(),
                "expire_reason": "window_timeout",
            },
        }
        return CorrelateResult(
            new_state=new_state,
            composite_event=None,
            accepted=False,
            reason=f"ALL: 窗口超时 watermark={watermark} deadline={plan_state.window_deadline_at}",
        )

    # OBSERVE 角色仅记录
    if member.role == "OBSERVE":
        return CorrelateResult(
            new_state={},
            accepted=True,
            reason=f"ALL: OBSERVE 成员 position={member.position} 仅记录",
        )

    # 检查冷却
    if plan_state.status == STATUS_COOLDOWN and not _check_cooldown_expired(plan_state, watermark):
        return CorrelateResult(
            new_state={},
            accepted=False,
            reason=f"ALL: 冷却中 cooldown_until={plan_state.cooldown_until}",
        )

    # 冷却已过期：重置为 WAITING_TRIGGER
    if plan_state.status == STATUS_COOLDOWN and _check_cooldown_expired(plan_state, watermark):
        new_state = {
            "new_status": STATUS_WAITING_TRIGGER,
            "clear_cooldown": True,
            "clear_window": True,
            "confirmed_member_ids": [],
            "state_payload": {
                "last_event_at": event.event_time.isoformat(),
                "reset_from_cooldown_at": watermark.isoformat(),
            },
        }
        # 重置后继续处理当前事件
        plan_state_status = STATUS_WAITING_TRIGGER
    else:
        plan_state_status = plan_state.status

    # TRIGGER 角色：打开窗口
    if member.role == "TRIGGER":
        if plan_state_status in (STATUS_WAITING_TRIGGER, STATUS_COOLDOWN):
            # 打开窗口
            window_started_at = event.event_time
            window_deadline_at = event.event_time + timedelta(
                seconds=revision.confirmation_window_seconds
            )
            new_confirmed = list(plan_state.confirmed_member_ids or []) + [member.id]

            new_state = {
                "new_status": STATUS_WAITING_CONFIRM,
                "window_started_at": window_started_at,
                "window_deadline_at": window_deadline_at,
                "confirmed_member_ids": new_confirmed,
                "state_payload": {
                    "last_event_at": event.event_time.isoformat(),
                    "window_opened_at": window_started_at.isoformat(),
                },
            }
            # 检查是否所有 required 成员已确认（单成员方案）
            required_members = [m for m in members if m.required and m.enabled and m.role != "OBSERVE"]
            if _all_required_confirmed(new_confirmed, required_members):
                return _build_confirmed_result(
                    event=event,
                    plan_state=plan_state,
                    revision=revision,
                    members=members,
                    confirmed_member_ids=new_confirmed,
                )
            return CorrelateResult(
                new_state=new_state,
                composite_event=None,
                accepted=True,
                reason=f"ALL: TRIGGER 成员 position={member.position} 打开窗口",
            )
        elif plan_state_status == STATUS_WAITING_CONFIRM:
            # 已在窗口中，重复 TRIGGER 事件：忽略（幂等）
            return CorrelateResult(
                new_state={},
                accepted=False,
                reason=f"ALL: 窗口已打开，重复 TRIGGER 事件 position={member.position}",
            )
        else:
            return CorrelateResult(
                new_state={},
                accepted=False,
                reason=f"ALL: TRIGGER 事件在终态 status={plan_state_status}",
            )

    # CONFIRM 角色：在窗口内确认
    if member.role == "CONFIRM":
        if plan_state_status != STATUS_WAITING_CONFIRM:
            return CorrelateResult(
                new_state={},
                accepted=False,
                reason=f"ALL: CONFIRM 事件在非 WAITING_CONFIRM 状态 status={plan_state_status}",
            )

        # 检查事件是否在窗口内
        if plan_state.window_started_at is not None and plan_state.window_deadline_at is not None:
            if event.event_time < plan_state.window_started_at:
                return CorrelateResult(
                    new_state={},
                    accepted=False,
                    reason=f"ALL: CONFIRM 事件早于窗口开始 event_time={event.event_time}",
                )
            if event.event_time > plan_state.window_deadline_at:
                return CorrelateResult(
                    new_state={
                        "new_status": STATUS_EXPIRED,
                        "clear_window": True,
                        "state_payload": {
                            "last_event_at": event.event_time.isoformat(),
                            "expired_at": event.event_time.isoformat(),
                            "expire_reason": "confirm_after_deadline",
                        },
                    },
                    composite_event=None,
                    accepted=False,
                    reason=f"ALL: CONFIRM 事件晚于窗口截止 event_time={event.event_time}",
                )

        # 检查 ordered 顺序
        if revision.ordered:
            if not _check_ordered_position(member, plan_state, members):
                return CorrelateResult(
                    new_state={},
                    accepted=False,
                    reason=f"ALL: ordered 模式下成员 position={member.position} 顺序不满足",
                )

        # 检查是否已确认（幂等）
        if member.id in (plan_state.confirmed_member_ids or []):
            return CorrelateResult(
                new_state={},
                accepted=False,
                reason=f"ALL: 成员 position={member.position} 已确认（幂等忽略）",
            )

        # 添加到已确认列表
        new_confirmed = list(plan_state.confirmed_member_ids or []) + [member.id]
        new_state = {
            "confirmed_member_ids": new_confirmed,
            "state_payload": {
                "last_event_at": event.event_time.isoformat(),
                "confirmed_at": event.event_time.isoformat(),
            },
        }

        # 检查是否所有 required 成员已确认
        required_members = [m for m in members if m.required and m.enabled and m.role != "OBSERVE"]
        if _all_required_confirmed(new_confirmed, required_members):
            return _build_confirmed_result(
                event=event,
                plan_state=plan_state,
                revision=revision,
                members=members,
                confirmed_member_ids=new_confirmed,
            )

        return CorrelateResult(
            new_state=new_state,
            composite_event=None,
            accepted=True,
            reason=f"ALL: CONFIRM 成员 position={member.position} 已确认，等待其他成员",
        )

    return CorrelateResult(
        new_state={},
        accepted=False,
        reason=f"ALL: 未知角色 role={member.role}",
    )


def _all_required_confirmed(
    confirmed_member_ids: list[UUID],
    required_members: list[MonitoringPlanMember],
) -> bool:
    """检查所有 required 成员是否已确认。

    Args:
        confirmed_member_ids: 已确认成员 ID 列表
        required_members: required 成员列表

    Returns:
        True 表示所有 required 成员已确认
    """
    if not required_members:
        return False
    confirmed_set = set(confirmed_member_ids)
    return all(m.id in confirmed_set for m in required_members)


def _build_confirmed_result(
    *,
    event: StrategyEvent,
    plan_state: MonitoringPlanState,
    revision: MonitoringPlanRevision,
    members: list[MonitoringPlanMember],
    confirmed_member_ids: list[UUID],
) -> CorrelateResult:
    """构建 CONFIRMED 结果（所有 required 成员已确认）。

    生成组合事件，状态进入冷却。
    """
    # 收集所有已确认成员的证据（使用当前事件作为最后确认的证据）
    # 注意：实际写入时需从 monitoring_state_evidence 表读取所有证据
    # 此处仅构建最后确认事件的证据，其余证据由调用方从 evidence 表补全
    confirmed_members = [
        m for m in members if m.id in confirmed_member_ids
    ]
    # 用当前事件构建最后一条证据
    last_member = _find_member_by_event(event, members)
    evidence_list: list[EvidenceDraft] = []
    if last_member is not None:
        evidence_list.append(_build_evidence_draft(event, last_member))

    composite = CompositeEventDraft(
        user_id=plan_state.user_id,
        monitoring_plan_id=plan_state.monitoring_plan_id,
        revision_id=plan_state.revision_id,
        instrument_id=event.instrument_id,
        event_type=EVENT_TYPE_ALL_CONFIRMED,
        event_time=event.event_time,
        member_count=len(confirmed_members),
        state={
            "mode": "ALL",
            "confirmed_member_ids": [str(m) for m in confirmed_member_ids],
            "confirmed_count": len(confirmed_members),
            "window_started_at": plan_state.window_started_at.isoformat() if plan_state.window_started_at else None,
            "window_deadline_at": plan_state.window_deadline_at.isoformat() if plan_state.window_deadline_at else None,
        },
        evidence=evidence_list,
    )

    new_state = {
        "new_status": STATUS_COOLDOWN,
        "cooldown_until": event.event_time + timedelta(seconds=revision.cooldown_seconds),
        "clear_window": True,
        "state_payload": {
            "last_event_at": event.event_time.isoformat(),
            "last_composite_event_at": event.event_time.isoformat(),
            "confirmed_at": event.event_time.isoformat(),
        },
    }

    return CorrelateResult(
        new_state=new_state,
        composite_event=composite,
        accepted=True,
        reason=f"ALL: 所有 required 成员已确认，触发组合事件 member_count={len(confirmed_members)}",
    )


def correlate_event(
    *,
    event: StrategyEvent,
    plan_state: MonitoringPlanState,
    revision: MonitoringPlanRevision,
    members: list[MonitoringPlanMember],
    watermark: datetime,
) -> CorrelateResult:
    """事件关联入口 - 根据 revision.mode 分发到对应模式的状态机。

    Args:
        event: 原始策略事件
        plan_state: 当前状态
        revision: 方案版本
        members: 方案成员列表
        watermark: 当前 watermark（max(event_time) - allowed_lateness）

    Returns:
        关联结果

    Raises:
        ValueError: 未知 mode
    """
    if revision.mode == "INDEPENDENT":
        return correlate_independent(
            event=event, plan_state=plan_state, revision=revision,
            members=members, watermark=watermark,
        )
    elif revision.mode == "ANY":
        return correlate_any(
            event=event, plan_state=plan_state, revision=revision,
            members=members, watermark=watermark,
        )
    elif revision.mode == "ALL":
        return correlate_all(
            event=event, plan_state=plan_state, revision=revision,
            members=members, watermark=watermark,
        )
    else:
        raise ValueError(f"未知 mode: {revision.mode}")


if __name__ == "__main__":
    # 自测入口：验证状态机逻辑（无副作用，不连 DB）
    import uuid as _uuid

    # 构造测试数据
    user_id = _uuid.uuid4()
    plan_id = _uuid.uuid4()
    revision_id = _uuid.uuid4()
    instrument_id = _uuid.uuid4()
    strategy_def_id = _uuid.uuid4()
    strategy_ver_id = _uuid.uuid4()

    # 1. INDEPENDENT 模式测试
    revision = MonitoringPlanRevision(
        id=revision_id, monitoring_plan_id=plan_id, revision=1,
        mode="INDEPENDENT", confirmation_window_seconds=0, ordered=False,
        cooldown_seconds=600, process_event_policy="IN_APP_ONLY",
        notification_config={}, created_by=user_id,
    )
    member = MonitoringPlanMember(
        id=_uuid.uuid4(), revision_id=revision_id,
        strategy_definition_id=strategy_def_id, strategy_version_id=strategy_ver_id,
        version_policy="PINNED", event_type="evt_test", role="TRIGGER",
        position=0, required=True, enabled=True, params={}, conditions=[],
    )
    state = MonitoringPlanState(
        id=_uuid.uuid4(), user_id=user_id, monitoring_plan_id=plan_id,
        revision_id=revision_id, instrument_id=instrument_id,
        status=STATUS_WAITING_TRIGGER, confirmed_member_ids=[],
        state_payload={}, lock_version=0,
    )
    event = StrategyEvent(
        id=_uuid.uuid4(), event_key="k1", strategy_version_id=strategy_ver_id,
        instrument_id=instrument_id, event_type="evt_test",
        event_time=datetime(2026, 6, 18, 10, 30, 0),
        schema_version=1, payload={"x": 1}, snapshot={},
    )
    result = correlate_event(
        event=event, plan_state=state, revision=revision,
        members=[member], watermark=event.event_time,
    )
    assert result.accepted, f"INDEPENDENT 应接受: {result.reason}"
    assert result.composite_event is not None
    assert result.composite_event.event_type == EVENT_TYPE_INDEPENDENT
    assert result.composite_event.member_count == 1
    print(f"INDEPENDENT 测试 ✓: {result.reason}")

    # 2. ANY 模式 + VETO 测试
    revision_any = MonitoringPlanRevision(
        id=revision_id, monitoring_plan_id=plan_id, revision=1,
        mode="ANY", confirmation_window_seconds=0, ordered=False,
        cooldown_seconds=600, process_event_policy="IN_APP_ONLY",
        notification_config={}, created_by=user_id,
    )
    veto_member = MonitoringPlanMember(
        id=_uuid.uuid4(), revision_id=revision_id,
        strategy_definition_id=strategy_def_id, strategy_version_id=strategy_ver_id,
        version_policy="PINNED", event_type="evt_veto", role="VETO",
        position=1, required=True, enabled=True, params={}, conditions=[],
    )
    veto_event = StrategyEvent(
        id=_uuid.uuid4(), event_key="k2", strategy_version_id=strategy_ver_id,
        instrument_id=instrument_id, event_type="evt_veto",
        event_time=datetime(2026, 6, 18, 10, 31, 0),
        schema_version=1, payload={"x": 1}, snapshot={},
    )
    result_veto = correlate_event(
        event=veto_event, plan_state=state, revision=revision_any,
        members=[member, veto_member], watermark=veto_event.event_time,
    )
    assert result_veto.accepted
    assert result_veto.composite_event.event_type == EVENT_TYPE_VETOED
    print(f"ANY + VETO 测试 ✓: {result_veto.reason}")

    # 3. ALL 模式测试
    revision_all = MonitoringPlanRevision(
        id=revision_id, monitoring_plan_id=plan_id, revision=1,
        mode="ALL", confirmation_window_seconds=900, ordered=False,
        cooldown_seconds=600, process_event_policy="IN_APP_ONLY",
        notification_config={}, created_by=user_id,
    )
    confirm_member = MonitoringPlanMember(
        id=_uuid.uuid4(), revision_id=revision_id,
        strategy_definition_id=strategy_def_id, strategy_version_id=strategy_ver_id,
        version_policy="PINNED", event_type="evt_confirm", role="CONFIRM",
        position=1, required=True, enabled=True, params={}, conditions=[],
    )
    trigger_event = StrategyEvent(
        id=_uuid.uuid4(), event_key="k3", strategy_version_id=strategy_ver_id,
        instrument_id=instrument_id, event_type="evt_test",
        event_time=datetime(2026, 6, 18, 10, 30, 0),
        schema_version=1, payload={"x": 1}, snapshot={},
    )
    # TRIGGER 打开窗口
    result_trigger = correlate_event(
        event=trigger_event, plan_state=state, revision=revision_all,
        members=[member, confirm_member], watermark=trigger_event.event_time,
    )
    assert result_trigger.accepted
    assert result_trigger.new_state["new_status"] == STATUS_WAITING_CONFIRM
    print(f"ALL TRIGGER 测试 ✓: {result_trigger.reason}")

    # 模拟状态更新后 CONFIRM
    state_after_trigger = MonitoringPlanState(
        id=state.id, user_id=user_id, monitoring_plan_id=plan_id,
        revision_id=revision_id, instrument_id=instrument_id,
        status=STATUS_WAITING_CONFIRM,
        window_started_at=trigger_event.event_time,
        window_deadline_at=datetime(2026, 6, 18, 10, 45, 0),
        confirmed_member_ids=[member.id],
        state_payload={}, lock_version=1,
    )
    confirm_event = StrategyEvent(
        id=_uuid.uuid4(), event_key="k4", strategy_version_id=strategy_ver_id,
        instrument_id=instrument_id, event_type="evt_confirm",
        event_time=datetime(2026, 6, 18, 10, 35, 0),
        schema_version=1, payload={"x": 1}, snapshot={},
    )
    result_confirm = correlate_event(
        event=confirm_event, plan_state=state_after_trigger, revision=revision_all,
        members=[member, confirm_member], watermark=confirm_event.event_time,
    )
    assert result_confirm.accepted
    assert result_confirm.composite_event is not None
    assert result_confirm.composite_event.event_type == EVENT_TYPE_ALL_CONFIRMED
    print(f"ALL CONFIRM 测试 ✓: {result_confirm.reason}")

    # 4. composite_event_key 幂等测试
    key1 = build_composite_event_key(
        revision_id=revision_id, instrument_id=instrument_id,
        event_type="test", event_time=datetime(2026, 6, 18, 10, 30, 0),
        member_ids=[member.id, confirm_member.id],
    )
    key2 = build_composite_event_key(
        revision_id=revision_id, instrument_id=instrument_id,
        event_type="test", event_time=datetime(2026, 6, 18, 10, 30, 0),
        member_ids=[confirm_member.id, member.id],  # 顺序不同
    )
    assert key1 == key2, "成员顺序不同应产生相同 key（排序后参与 hash）"
    print("composite_event_key 幂等测试 ✓")

    print("OK")
