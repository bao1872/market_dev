"""Canonical realtime SMC evaluation input（F1）。

职责（严格限定）：把 **真实 owner** 产出的原料装配成 realtime transition owner 的唯一输入：

```text
SmcMonitorTargetSet
+ effective SMC params（与 TargetSet 同源，不得从 DEFAULT_PARAMS 另造）
+ completed 1m qfq bars
+ qfq / adjustment proof（来自 AdjustmentFactorService / business-date context owner）
+ completed-bar sequence proof（来自 market-data/session owner）
+ daily epoch（authoritative completed daily updated_through）
```

本模块**不**做：
- 复权计算（复用 ``AdjustmentFactorService`` / ``BusinessDateAdjustmentService.apply_context_qfq``）；
- 交易时段判定（复用 market-data/session owner）；
- 任何 proof 的“伪造”（禁止硬编码 True）；
- DB / provider / 通知。

**epoch 冻结**：``daily_epoch`` 主边界是 ``target_set.input_identity["updated_through"]``。
``daily_bars_hash`` 变化（XDXR / rebuild）**不单独**定义 epoch，避免重置 lane bias。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.smc_monitor_target_service import (
    SmcMonitorTargetSet,
    SmcTargetContractError,
    _sha256_json,
)
from app.services.smc_realtime_transition_service import (
    QfqMinuteBar,
    RealtimeSmcEvaluationContext,
)

__all__ = [
    "CompletedMinuteSequenceProof",
    "RealtimeSmcInputBundle",
    "resolve_daily_epoch",
    "build_realtime_smc_input_bundle",
]


@dataclass(frozen=True)
class CompletedMinuteSequenceProof:
    """canonical completed 1m 序列连续性证明（由 market-data/session owner 产出）。

    SMC 层不自己硬编码交易时段；只消费上游 proof。
    ``proven=False`` → transition owner fail closed（0 event，state 不推进）。
    """

    proven: bool
    reason: str = ""
    source: str = ""


@dataclass(frozen=True)
class RealtimeSmcInputBundle:
    """realtime transition owner 的唯一输入（不可变）。"""

    instrument_id: str
    target_set: SmcMonitorTargetSet
    effective_params: Mapping[str, Any]
    qfq_minute_bars: tuple[QfqMinuteBar, ...]
    context: RealtimeSmcEvaluationContext
    daily_epoch: str


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
    instrument_id: Any,
    target_set: SmcMonitorTargetSet,
    effective_params: Mapping[str, Any],
    qfq_minute_bars: Sequence[QfqMinuteBar],
    qfq_coordinate_proven: bool,
    sequence_proof: CompletedMinuteSequenceProof,
    qfq_coordinate_reason: str = "",
    daily_epoch: str | None = None,
) -> RealtimeSmcInputBundle:
    """装配并校验 canonical input bundle（fail closed，不伪造 proof）。

    Raises:
        SmcTargetContractError: effective params 与 TargetSet 的 ``params_hash`` 不一致
            （禁止悄悄改用 DEFAULT_PARAMS）；或 TargetSet 缺少 epoch 依据。
    """
    if not isinstance(target_set, SmcMonitorTargetSet):
        raise SmcTargetContractError("target_set 必须是 SmcMonitorTargetSet")
    if not isinstance(effective_params, Mapping):
        raise SmcTargetContractError("effective_params 必须是 mapping")

    expected = target_set.contract_identity.get("params_hash")
    if not isinstance(expected, str) or not expected:
        raise SmcTargetContractError("target_set 缺少 params_hash")
    if _sha256_json(effective_params) != expected:
        raise SmcTargetContractError(
            "effective_params 与 target_set.params_hash 不一致（禁止使用 DEFAULT_PARAMS 兜底）"
        )

    if not isinstance(sequence_proof, CompletedMinuteSequenceProof):
        raise SmcTargetContractError("sequence_proof 必须是 CompletedMinuteSequenceProof")

    context = RealtimeSmcEvaluationContext(
        effective_params=dict(effective_params),
        qfq_coordinate_proven=bool(qfq_coordinate_proven),
        qfq_coordinate_reason=qfq_coordinate_reason,
        completed_bar_sequence_proven=bool(sequence_proof.proven),
        completed_bar_sequence_reason=sequence_proof.reason,
    )

    return RealtimeSmcInputBundle(
        instrument_id=str(instrument_id),
        target_set=target_set,
        effective_params=dict(effective_params),
        qfq_minute_bars=tuple(qfq_minute_bars),
        context=context,
        daily_epoch=daily_epoch if daily_epoch is not None else resolve_daily_epoch(target_set),
    )
