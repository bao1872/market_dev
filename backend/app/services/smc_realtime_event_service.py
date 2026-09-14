"""Realtime SMC 事件适配（F2 PART 8 / PART 11）。

PART 8 — ``prepare_transition_state``：从持久化 MonitorState 恢复 / bootstrap / re-initialize
可持久化 transition state（correctness authority），严格规则：

- namespace 不存在 → bootstrap（initial from C3）；
- namespace 存在 + payload corrupt → fail closed（绝不 bootstrap）；
- namespace 存在 + valid + same epoch → restore；
- namespace 存在 + valid + new epoch → re-initialize from new C3。

PART 11 — ``evaluate_realtime_smc_events``：纯函数 adapter，把 canonical transition 结果
映射为 ``StrategyEventDraft``。正确性 identity 由 ``event_key`` / ``episode key`` 保证，
因此 draft 的 ``apply_cooldown=False``（不走粗粒度冷却，避免重复漏发）。

本模块不做 I/O、不写数据库、不调用 provider。所有 fail-closed 门槛由
``smc_realtime_transition_service`` 持有，本模块只在 adapter 校验失败时整体返回 0 draft +
original state（绝不部分产事件）。

事件类型常量与 ``app.strategy.monitors.smc_monitor`` 保持一致（此处本地定义，避免
service 层反向依赖 strategy monitor）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.services.smc_monitor_target_service import SmcMonitorTargetSet
from app.services.smc_realtime_input_service import (
    RealtimeSmcInputBundle,
    resolve_daily_epoch,
)
from app.services.smc_realtime_transition_service import (
    SmcRealtimeTransitionState,
    deserialize_transition_state,
    evaluate_smc_realtime_transitions,
    initial_transition_state,
)
from app.strategy.runtime import StrategyEventDraft

__all__ = [
    "PreparedSmcTransitionState",
    "prepare_transition_state",
    "RealtimeSmcEventEvaluation",
    "evaluate_realtime_smc_events",
]

# 与 app.strategy.monitors.smc_monitor 保持一致
SMC_BOS_CROSS = "smc_bos_cross"
SMC_CHOCH_CROSS = "smc_choch_cross"
SMC_ORDER_BLOCK_FIRST_TOUCH = "smc_order_block_first_touch"


# ---------------------------------------------------------------------------
# PART 8 — transition state preparation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedSmcTransitionState:
    """transition state 准备结果。

    ``ok=False`` 表示 corrupt / 无法恢复 → 调用方必须置 ``smc_realtime_input=None``，
    绝不 fallback 到 fresh state（否则会重发已消费事件 / 重复 episode）。
    """

    state: SmcRealtimeTransitionState | None
    ok: bool
    degraded_reason: str = ""


def prepare_transition_state(
    *,
    instrument_id: UUID,
    target_set: SmcMonitorTargetSet,
    persisted_namespace_present: bool,
    persisted_payload: Any,
) -> PreparedSmcTransitionState:
    """从持久化 MonitorState 准备可持久化 transition state（correctness authority）。

    关键区别：``namespace 不存在`` ≠ ``namespace 存在但值为 None``。后者是 corrupt，
    必须 fail closed，不能 bootstrap。
    """
    epoch = resolve_daily_epoch(target_set)

    if not persisted_namespace_present:
        # bootstrap：全新标的 / 新 epoch 之前从未写过该 namespace。
        return PreparedSmcTransitionState(
            state=initial_transition_state(
                target_set,
                instrument_id=str(instrument_id),
                daily_epoch=epoch,
            ),
            ok=True,
        )

    # namespace 存在 → 必须能 strict deserialize；失败即 corrupt。
    try:
        restored = deserialize_transition_state(persisted_payload)
    except (TypeError, ValueError) as exc:
        return PreparedSmcTransitionState(
            state=None,
            ok=False,
            degraded_reason=f"persisted realtime SMC state corrupt: {exc}",
        )

    if restored.daily_epoch != epoch:
        # 同一标的进入新 daily-state epoch → 用新 C3 重新初始化 baseline。
        return PreparedSmcTransitionState(
            state=initial_transition_state(
                target_set,
                instrument_id=str(instrument_id),
                daily_epoch=epoch,
            ),
            ok=True,
        )

    return PreparedSmcTransitionState(state=restored, ok=True)


# ---------------------------------------------------------------------------
# PART 11 — pure event adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RealtimeSmcEventEvaluation:
    """canonical realtime SMC 事件评估（PART 11）。"""

    drafts: tuple[StrategyEventDraft, ...]
    next_state: SmcRealtimeTransitionState
    degraded_reason: str | None = None
    no_op: bool = False


def evaluate_realtime_smc_events(
    bundle: RealtimeSmcInputBundle,
) -> RealtimeSmcEventEvaluation:
    """把 canonical transition 结果映射为 ``StrategyEventDraft``（纯函数）。

    Fail-closed 门槛由 ``evaluate_smc_realtime_transitions`` 持有；本 adapter 仅在
    transition target identity 缺失等 adapter 级校验失败时整体 fail closed（0 draft +
    original state）。绝不部分产事件。
    """
    # no new completed bars 是正常 NO-OP，不是 degraded。
    if not bundle.qfq_minute_bars:
        return RealtimeSmcEventEvaluation(
            drafts=(),
            next_state=bundle.transition_state,
            degraded_reason=None,
            no_op=True,
        )

    result = evaluate_smc_realtime_transitions(
        instrument_id=str(bundle.instrument_id),
        target_set=bundle.runtime_target.target_set,
        completed_bars=bundle.qfq_minute_bars,
        context=bundle.context,
        state=bundle.transition_state,
    )
    if not result.ok:
        # proof 不通过 / 顺序异常 / params 不匹配 → 保持 original state，绝不推进。
        return RealtimeSmcEventEvaluation(
            drafts=(),
            next_state=bundle.transition_state,
            degraded_reason=result.fail_closed_reason,
            no_op=False,
        )

    structure_by_id = {
        t.target_id: t
        for t in bundle.runtime_target.target_set.active_structure_targets
    }
    ob_by_id = {
        t.target_id: t
        for t in bundle.runtime_target.target_set.active_order_block_targets
    }

    drafts: list[StrategyEventDraft] = []

    # --- structure adapter ---
    for transition in result.structure_transitions:
        target = structure_by_id.get(transition.target_id)
        if target is None:
            # adapter 找不到对应 target → 整体 fail closed，绝不部分产事件。
            return RealtimeSmcEventEvaluation(
                drafts=(),
                next_state=bundle.transition_state,
                degraded_reason=f"structure target missing: {transition.target_id}",
                no_op=False,
            )

        if transition.event_type == "BOS":
            event_type = SMC_BOS_CROSS
        elif transition.event_type == "CHoCH":
            event_type = SMC_CHOCH_CROSS
        else:
            return RealtimeSmcEventEvaluation(
                drafts=(),
                next_state=bundle.transition_state,
                degraded_reason=f"unknown structure event_type: {transition.event_type}",
                no_op=False,
            )

        try:
            event_time = datetime.fromisoformat(transition.confirmed_bar_time)
        except (ValueError, TypeError) as exc:
            return RealtimeSmcEventEvaluation(
                drafts=(),
                next_state=bundle.transition_state,
                degraded_reason=f"invalid structure event_time: {exc}",
                no_op=False,
            )
        if event_time.tzinfo is None:
            return RealtimeSmcEventEvaluation(
                drafts=(),
                next_state=bundle.transition_state,
                degraded_reason="structure event_time not timezone-aware",
                no_op=False,
            )

        # identity 不含 event_type：同一 pivot 一生只一次 False→True。
        dedupe_key = "smc_struct_transition:" + transition.transition_key
        logical_entity = "smc_structure:" + transition.transition_key
        payload = {
            "target_id": transition.target_id,
            "structure_type": transition.event_type,
            "lane": transition.lane,
            "kind": transition.kind,
            "level": target.level,
            "direction": "UP" if transition.kind == "high" else "DOWN",
            "anchor_time": transition.anchor_time,
            "confirmed_time": transition.confirmed_bar_time,
            "bias_before": transition.bias_before,
            "bias_after": transition.bias_after,
            "target_set_version": bundle.runtime_target.target_set.target_set_version,
            "indicator_view": "smc",
        }
        drafts.append(
            StrategyEventDraft(
                event_type=event_type,
                event_time=event_time,
                dedupe_key=dedupe_key,
                logical_entity=logical_entity,
                payload=payload,
                state_ttl_seconds=600,
                apply_cooldown=False,
            )
        )

    # --- OB adapter ---
    for ob_transition in result.ob_episode_transitions:
        ob_target = ob_by_id.get(ob_transition.target_id)
        if ob_target is None:
            return RealtimeSmcEventEvaluation(
                drafts=(),
                next_state=bundle.transition_state,
                degraded_reason=f"ob target missing: {ob_transition.target_id}",
                no_op=False,
            )

        try:
            event_time = datetime.fromisoformat(ob_transition.entry_bar_time)
        except (ValueError, TypeError) as exc:
            return RealtimeSmcEventEvaluation(
                drafts=(),
                next_state=bundle.transition_state,
                degraded_reason=f"invalid ob event_time: {exc}",
                no_op=False,
            )
        if event_time.tzinfo is None:
            return RealtimeSmcEventEvaluation(
                drafts=(),
                next_state=bundle.transition_state,
                degraded_reason="ob event_time not timezone-aware",
                no_op=False,
            )

        dedupe_key = "smc_ob_episode:" + ob_transition.episode_key
        logical_entity = "smc_ob:" + ob_transition.logical_ob_key
        payload = {
            "target_id": ob_transition.target_id,
            "internal": ob_transition.internal,
            "bias": ob_transition.bias,
            "bar_low": ob_target.bar_low,
            "bar_high": ob_target.bar_high,
            "anchor_time": ob_transition.anchor_time,
            "confirmed_time": ob_transition.confirmed_time,
            "entry_bar_time": ob_transition.entry_bar_time,
            "target_set_version": bundle.runtime_target.target_set.target_set_version,
            "indicator_view": "smc",
        }
        drafts.append(
            StrategyEventDraft(
                event_type=SMC_ORDER_BLOCK_FIRST_TOUCH,
                event_time=event_time,
                dedupe_key=dedupe_key,
                logical_entity=logical_entity,
                payload=payload,
                state_ttl_seconds=600,
                apply_cooldown=False,
            )
        )

    return RealtimeSmcEventEvaluation(
        drafts=tuple(drafts),
        next_state=result.state,
    )
