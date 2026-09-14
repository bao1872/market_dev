"""Canonical realtime SMC evaluation input（F1R / PART 7）。

builder **只消费 authoritative objects**：

```text
SmcRuntimeTargetBundle（TargetSet + 同源 effective params）
CanonicalCompletedQfqMinutes（MDAS 权威产出 + qfq / sequence proof）
SmcRealtimeTransitionState（已恢复的 transition state）
```

明确禁止：
- caller 提供 ``qfq_proven`` / ``sequence_proven`` 之类的 proof bool；
- caller override ``daily_epoch``；
- caller 提供独立 ``target_set`` / ``effective_params`` / ``qfq_minute_bars``；
- ``bool()`` coercion、``dict()`` 重建 mutable、``str(instrument_id)`` 宽松转换。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.services.market_data_aggregation_service import CanonicalCompletedQfqMinutes
from app.services.smc_monitor_target_service import (
    SmcMonitorTargetSet,
    SmcRuntimeTargetBundle,
    SmcTargetContractError,
)
from app.services.smc_realtime_transition_service import (
    QfqMinuteBar,
    RealtimeSmcEvaluationContext,
    SmcRealtimeTransitionState,
)

__all__ = [
    "RealtimeSmcInputBundle",
    "resolve_daily_epoch",
    "build_realtime_smc_input_bundle",
]


@dataclass(frozen=True)
class RealtimeSmcInputBundle:
    """realtime transition owner 的唯一输入（不可变）。

    ``target_set`` / ``effective_params`` **不在此重复保存**，统一从 ``runtime_target``
    访问，避免未来出现两份来源分叉。
    """

    instrument_id: UUID
    runtime_target: SmcRuntimeTargetBundle
    qfq_minute_bars: tuple[QfqMinuteBar, ...]
    context: RealtimeSmcEvaluationContext
    daily_epoch: str
    transition_state: SmcRealtimeTransitionState


def resolve_daily_epoch(target_set: SmcMonitorTargetSet) -> str:
    """epoch 主边界 = authoritative completed daily ``updated_through``。

    不使用 ``target_set_version``（XDXR / rebuild 也会改变它），
    也不单独使用 ``daily_bars_hash``。
    """
    input_identity = target_set.input_identity
    if not isinstance(input_identity, Mapping):
        raise SmcTargetContractError("target_set.input_identity 缺失或非 mapping")
    updated_through = input_identity.get("updated_through")
    if not isinstance(updated_through, str) or not updated_through:
        raise SmcTargetContractError(
            f"target_set.input_identity.updated_through 必须是非空 str，实际={updated_through!r}"
        )
    return updated_through


def build_realtime_smc_input_bundle(
    *,
    instrument_id: UUID,
    runtime_target: SmcRuntimeTargetBundle,
    minute_input: CanonicalCompletedQfqMinutes,
    transition_state: SmcRealtimeTransitionState,
) -> RealtimeSmcInputBundle:
    """装配 canonical input bundle（proof 全部来自 owner，禁止 caller override）。

    Raises:
        SmcTargetContractError: 任一输入类型不符，或 transition state 的 epoch
            与 TargetSet 推导出的 epoch 不一致。
    """
    if not isinstance(instrument_id, UUID):
        raise SmcTargetContractError("instrument_id must be UUID")
    if not isinstance(runtime_target, SmcRuntimeTargetBundle):
        raise SmcTargetContractError("runtime_target must be SmcRuntimeTargetBundle")
    if not isinstance(minute_input, CanonicalCompletedQfqMinutes):
        raise SmcTargetContractError("minute_input must be CanonicalCompletedQfqMinutes")
    if not isinstance(transition_state, SmcRealtimeTransitionState):
        raise SmcTargetContractError("transition_state must be SmcRealtimeTransitionState")

    daily_epoch = resolve_daily_epoch(runtime_target.target_set)
    if transition_state.daily_epoch != daily_epoch:
        raise SmcTargetContractError(
            "transition state epoch mismatch: "
            f"state={transition_state.daily_epoch!r} != target={daily_epoch!r}"
        )

    bars = tuple(
        QfqMinuteBar(
            bar_time=row.bar_time,
            session_key=row.session_key,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
        )
        for row in minute_input.bars
    )

    context = RealtimeSmcEvaluationContext(
        effective_params=runtime_target.effective_params,
        qfq_coordinate_proven=minute_input.qfq_proven,
        qfq_coordinate_reason=minute_input.qfq_reason,
        completed_bar_sequence_proven=minute_input.sequence_proven,
        completed_bar_sequence_reason=minute_input.sequence_reason,
    )

    return RealtimeSmcInputBundle(
        instrument_id=instrument_id,
        runtime_target=runtime_target,
        qfq_minute_bars=bars,
        context=context,
        daily_epoch=daily_epoch,
        transition_state=transition_state,
    )
