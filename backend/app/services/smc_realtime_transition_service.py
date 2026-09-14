"""Realtime SMC Transition Semantics — 纯语义 owner。

对应设计：`docs/changes/2026/CHANGE-20260914-001-realtime-smc-transition-semantics-design.md`
（DESIGN PASS @ `6100ecf8`）。

职责（严格限定）：
- 消费 C3 ``SmcMonitorTargetSet`` + **已完成 1m（qfq 坐标）** bar + effective params + 评估上下文；
- 产出 structure transition（BOS / CHoCH，一次性）与 OB episode transition（可重复 entry episode）；
- 维护 dynamic lane bias 与 OB episode state（可持久化 / 可 deterministic replay）。

本模块**不做**：发送通知、写 Outbox、调用 provider、发现 pivot / OB、重算 mitigation、
修改 C3 contract。纯函数、无 I/O、无副作用。

冻结语义要点（不得自行 redesign）：
- crossing 只认 completed 1m ``close``（``prev_close <= level < curr_close`` 等），禁止 wick；
- ``structure_transition_key`` **不含 event_type**（event_type 是 fire 时由 current lane bias 推出的属性）；
- lane bias 是**动态状态**（C3 值仅作 epoch 初始值）；
- internal gate：``level != counterpart level`` + ``bullish_bar / bearish_bar``；
  未形成 counterpart（C3 ``None``）时保持 core 语义（``finite != None`` → True）；
- qfq 坐标 / params hash 校验失败 → **fail closed（0 event）**。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.smc_monitor_target_service import (
    SmcMonitorTargetSet,
    _sha256_json,
)

__all__ = [
    "QfqMinuteBar",
    "RealtimeSmcEvaluationContext",
    "SessionAccumulator",
    "LaneState",
    "ObEpisodeState",
    "SmcRealtimeTransitionState",
    "StructureTransition",
    "ObEpisodeTransition",
    "SmcRealtimeTransitionResult",
    "structure_transition_key",
    "logical_ob_key",
    "initial_transition_state",
    "evaluate_smc_realtime_transitions",
]

_BULLISH_EVENT = "BOS"
_BEARISH_EVENT = "CHoCH"
_BULLISH_BIAS = 1
_BEARISH_BIAS = -1


# ---------------------------------------------------------------------------
# 输入 / 状态模型（全部 frozen，保证 snapshot 不可外部 mutation）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QfqMinuteBar:
    """**已完成** 1m bar，且已处于 qfq 价格坐标。

    ``session_key`` 由 market-data / session owner 提供（SMC 层不自己定义交易时钟）。
    """

    bar_time: str
    session_key: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class RealtimeSmcEvaluationContext:
    """评估上下文：effective params + qfq 坐标可证明性。

    ``effective_params`` 必须与 TargetSet 的 `params_hash` 一致（否则 fail closed）。
    ``qfq_coordinate_proven`` 由 adjustment-context owner 证明；
    未证明时禁止用 raw bar 与 qfq level 比较。
    """

    effective_params: Mapping[str, Any]
    qfq_coordinate_proven: bool = False
    qfq_coordinate_reason: str = ""


@dataclass(frozen=True)
class SessionAccumulator:
    """当日累计（partial-daily candle 滚动状态）。"""

    session_key: str
    session_open: float
    running_high: float
    running_low: float
    latest_close: float


@dataclass(frozen=True)
class LaneState:
    """单 lane 的动态状态：current bias + 已 FIRED/TERMINAL 的 transition key。"""

    bias: int
    fired_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObEpisodeState:
    logical_ob_key: str
    inside: bool = False
    current_episode_key: str | None = None


@dataclass(frozen=True)
class SmcRealtimeTransitionState:
    """可持久化 / 可 replay 的 realtime transition state。"""

    daily_epoch: str
    swing: LaneState
    internal: LaneState
    session: SessionAccumulator | None = None
    ob_states: tuple[ObEpisodeState, ...] = ()
    prev_completed_close: float | None = None


@dataclass(frozen=True)
class StructureTransition:
    """一次 structure crossing（BOS / CHoCH）。"""

    transition_key: str
    instrument_id: str
    lane: str
    kind: str
    anchor_time: str
    event_type: str
    bias_before: int
    bias_after: int
    confirmed_bar_time: str
    target_id: str


@dataclass(frozen=True)
class ObEpisodeTransition:
    """一次 OB entry episode（一次 entry = 一次通知候选）。"""

    episode_key: str
    logical_ob_key: str
    instrument_id: str
    internal: bool
    bias: int
    anchor_time: str
    confirmed_time: str
    entry_bar_time: str
    target_id: str


@dataclass(frozen=True)
class SmcRealtimeTransitionResult:
    state: SmcRealtimeTransitionState
    structure_transitions: tuple[StructureTransition, ...] = ()
    ob_episode_transitions: tuple[ObEpisodeTransition, ...] = ()
    ok: bool = True
    fail_closed_reason: str = ""


# ---------------------------------------------------------------------------
# identity（correctness dedupe key）
# ---------------------------------------------------------------------------


def _instrument_key(instrument_id: Any) -> str:
    return instrument_id if isinstance(instrument_id, str) else str(instrument_id)


def structure_transition_key(
    instrument_id: Any,
    lane: str,
    kind: str,
    anchor_time: str,
) -> str:
    """structure correctness identity —— **不含 event_type**。

    同一 pivot 一生只允许一次 False→True，故身份只能是 (instrument, lane, kind, anchor_time)。
    """
    return _sha256_json(
        {
            "namespace": "smc_structure_transition",
            "instrument_id": _instrument_key(instrument_id),
            "lane": lane,
            "kind": kind,
            "anchor_time": anchor_time,
        }
    )


def logical_ob_key(
    instrument_id: Any,
    internal: bool,
    bias: int,
    anchor_time: str,
    confirmed_time: str,
) -> str:
    return _sha256_json(
        {
            "namespace": "smc_ob_logical",
            "instrument_id": _instrument_key(instrument_id),
            "internal": internal,
            "bias": bias,
            "anchor_time": anchor_time,
            "confirmed_time": confirmed_time,
        }
    )


def ob_episode_key(logical_key: str, entry_completed_bar_time: str) -> str:
    return _sha256_json(
        {
            "namespace": "smc_ob_episode",
            "logical_ob_key": logical_key,
            "entry_completed_bar_time": entry_completed_bar_time,
        }
    )


# ---------------------------------------------------------------------------
# internal gate（core SSOT 语义）
# ---------------------------------------------------------------------------


def _is_bullish_bar(*, o: float, h: float, low: float, c: float) -> bool:
    """core 权威表达式（不得美化/纠正）：``(H - max(C, O)) > min(C, O - L)``。"""
    return (h - max(c, o)) > min(c, o - low)


def _is_bearish_bar(*, o: float, h: float, low: float, c: float) -> bool:
    return (h - max(c, o)) < min(c, o - low)


def _confluence_enabled(effective_params: Mapping[str, Any]) -> bool | None:
    """读取 internal_filter_confluence；缺失 → None（fail closed，禁用 DEFAULT_PARAMS）。"""
    value = effective_params.get("internal_filter_confluence")
    if isinstance(value, bool):
        return value
    return None


def _internal_gate_passes(
    *,
    kind: str,
    internal_level: float,
    counterpart_level: float | None,
    accum: SessionAccumulator,
    confluence: bool,
) -> bool:
    """internal lane 的额外 gate：level inequality + (可选) bullish/bearish_bar。

    counterpart (swing) 未形成时 C3 暴露 ``None``：``finite != None`` → True，
    与 core 的 ``finite != NaN`` 等价（**None 不得当 gate-false**）。
    """
    if internal_level == counterpart_level:
        return False
    if not confluence:
        return True
    if kind == "high":
        return _is_bullish_bar(
            o=accum.session_open, h=accum.running_high, low=accum.running_low, c=accum.latest_close
        )
    return _is_bearish_bar(
        o=accum.session_open, h=accum.running_high, low=accum.running_low, c=accum.latest_close
    )


def _slot_level(target_set: SmcMonitorTargetSet, name: str) -> float | None:
    slots = target_set.structure_context.get("slots") or {}
    slot = slots.get(name) or {}
    level = slot.get("level")
    return float(level) if isinstance(level, (int, float)) and not isinstance(level, bool) else None


def _classify(bias_before: int, kind: str) -> str:
    """core 规则：high 方向 crossing 且 bias==-1 → CHoCH，否则 BOS；low 反之（bias==+1 → CHoCH）。"""
    if kind == "high":
        return _BEARISH_EVENT if bias_before == _BEARISH_BIAS else _BULLISH_EVENT
    return _BEARISH_EVENT if bias_before == _BULLISH_BIAS else _BULLISH_EVENT


def _next_bias(kind: str) -> int:
    return _BULLISH_BIAS if kind == "high" else _BEARISH_BIAS


# ---------------------------------------------------------------------------
# 状态初始化
# ---------------------------------------------------------------------------


def initial_transition_state(
    target_set: SmcMonitorTargetSet,
    *,
    instrument_id: Any,
    daily_epoch: str,
) -> SmcRealtimeTransitionState:
    """TargetSet 激活时用 C3 ``structure_context`` 初始化 lane bias（仅初始值）。"""
    ctx = target_set.structure_context
    swing_bias = int(ctx.get("swing_bias") or 0)
    internal_bias = int(ctx.get("internal_bias") or 0)
    ob_states = tuple(
        ObEpisodeState(
            logical_ob_key=logical_ob_key(
                instrument_id,
                t.internal,
                t.bias,
                t.anchor_time,
                t.confirmed_time,
            )
        )
        for t in target_set.active_order_block_targets
    )
    return SmcRealtimeTransitionState(
        daily_epoch=daily_epoch,
        swing=LaneState(bias=swing_bias),
        internal=LaneState(bias=internal_bias),
        session=None,
        ob_states=ob_states,
        prev_completed_close=None,
    )


# ---------------------------------------------------------------------------
# 主入口（纯函数；deterministic replay：同样的 bars + 初始 state → 同样结果）
# ---------------------------------------------------------------------------


def _fail(state: SmcRealtimeTransitionState, reason: str) -> SmcRealtimeTransitionResult:
    return SmcRealtimeTransitionResult(state=state, ok=False, fail_closed_reason=reason)


def evaluate_smc_realtime_transitions(
    *,
    instrument_id: Any,
    target_set: SmcMonitorTargetSet,
    completed_bars: Sequence[QfqMinuteBar],
    context: RealtimeSmcEvaluationContext,
    state: SmcRealtimeTransitionState,
) -> SmcRealtimeTransitionResult:
    """按顺序消费 completed 1m bar，产出 structure / OB episode transitions。

    Fail-closed 门槛（任一不满足 → 0 event）：
    - qfq 坐标未被证明；
    - ``canonical_hash(effective_params) != target_set.params_hash``；
    - effective params 缺 ``internal_filter_confluence``。
    """
    # --- gate 1: qfq 坐标 ---
    if not context.qfq_coordinate_proven:
        return _fail(state, f"qfq coordinate not proven: {context.qfq_coordinate_reason or 'unspecified'}")

    # --- gate 2: effective params 必须与 TargetSet 绑定 ---
    expected = target_set.contract_identity.get("params_hash")
    if not isinstance(expected, str) or not expected:
        return _fail(state, "target_set missing params_hash")
    if _sha256_json(context.effective_params) != expected:
        return _fail(state, "effective_params hash != target_set.params_hash")

    confluence = _confluence_enabled(context.effective_params)
    if confluence is None:
        return _fail(state, "effective_params missing internal_filter_confluence")

    inst = _instrument_key(instrument_id)

    swing_keys = list(state.swing.fired_keys)
    internal_keys = list(state.internal.fired_keys)
    swing_bias = state.swing.bias
    internal_bias = state.internal.bias
    accum = state.session
    prev_close = state.prev_completed_close
    ob_states = {s.logical_ob_key: s for s in state.ob_states}

    structure_out: list[StructureTransition] = []
    ob_out: list[ObEpisodeTransition] = []

    # active structure targets 索引：(lane, kind) -> target
    struct_targets = {(t.lane, t.kind): t for t in target_set.active_structure_targets}
    ob_targets = list(target_set.active_order_block_targets)

    # 目标消失的 OB → TERMINAL（从 state 剪除）
    live_ob_keys = {
        logical_ob_key(inst, t.internal, t.bias, t.anchor_time, t.confirmed_time) for t in ob_targets
    }
    for key in list(ob_states):
        if key not in live_ob_keys:
            del ob_states[key]

    for bar in completed_bars:
        # --- session 累计（午休不 reset；跨 session_key 才新开）---
        if accum is None or accum.session_key != bar.session_key:
            accum = SessionAccumulator(
                session_key=bar.session_key,
                session_open=bar.open,
                running_high=bar.high,
                running_low=bar.low,
                latest_close=bar.close,
            )
        else:
            accum = SessionAccumulator(
                session_key=accum.session_key,
                session_open=accum.session_open,
                running_high=max(accum.running_high, bar.high),
                running_low=min(accum.running_low, bar.low),
                latest_close=bar.close,
            )

        # --- structure crossing（只认 completed close）---
        if prev_close is not None:
            for (lane, kind), target in struct_targets.items():
                key = structure_transition_key(inst, lane, kind, target.anchor_time)
                fired = swing_keys if lane == "swing" else internal_keys
                if key in fired:
                    continue  # 已 FIRED/TERMINAL

                level = target.level
                if kind == "high":
                    crossed = prev_close <= level < bar.close
                else:
                    crossed = prev_close >= level > bar.close
                if not crossed:
                    continue

                # internal lane 额外 gate
                if lane == "internal":
                    counterpart = _slot_level(
                        target_set, "swing_high" if kind == "high" else "swing_low"
                    )
                    if not _internal_gate_passes(
                        kind=kind,
                        internal_level=float(level),
                        counterpart_level=counterpart,
                        accum=accum,
                        confluence=bool(confluence),
                    ):
                        continue

                bias_before = swing_bias if lane == "swing" else internal_bias
                event_type = _classify(bias_before, kind)
                bias_after = _next_bias(kind)
                if lane == "swing":
                    swing_bias = bias_after
                    swing_keys.append(key)
                else:
                    internal_bias = bias_after
                    internal_keys.append(key)

                structure_out.append(
                    StructureTransition(
                        transition_key=key,
                        instrument_id=inst,
                        lane=lane,
                        kind=kind,
                        anchor_time=target.anchor_time,
                        event_type=event_type,
                        bias_before=bias_before,
                        bias_after=bias_after,
                        confirmed_bar_time=bar.bar_time,
                        target_id=target.target_id,
                    )
                )

        # --- OB episode（completed 1m 实际成交区间 vs zone）---
        for ob in ob_targets:
            lkey = logical_ob_key(inst, ob.internal, ob.bias, ob.anchor_time, ob.confirmed_time)
            st = ob_states.get(lkey, ObEpisodeState(logical_ob_key=lkey))
            touched = (bar.high >= ob.bar_low) and (bar.low <= ob.bar_high)
            if touched and not st.inside:
                ep_key = ob_episode_key(lkey, bar.bar_time)
                ob_states[lkey] = ObEpisodeState(
                    logical_ob_key=lkey, inside=True, current_episode_key=ep_key
                )
                ob_out.append(
                    ObEpisodeTransition(
                        episode_key=ep_key,
                        logical_ob_key=lkey,
                        instrument_id=inst,
                        internal=ob.internal,
                        bias=ob.bias,
                        anchor_time=ob.anchor_time,
                        confirmed_time=ob.confirmed_time,
                        entry_bar_time=bar.bar_time,
                        target_id=ob.target_id,
                    )
                )
            elif not touched and st.inside:
                ob_states[lkey] = ObEpisodeState(
                    logical_ob_key=lkey, inside=False, current_episode_key=None
                )
            else:
                ob_states[lkey] = st

        prev_close = bar.close

    new_state = SmcRealtimeTransitionState(
        daily_epoch=state.daily_epoch,
        swing=LaneState(bias=swing_bias, fired_keys=tuple(swing_keys)),
        internal=LaneState(bias=internal_bias, fired_keys=tuple(internal_keys)),
        session=accum,
        ob_states=tuple(ob_states[k] for k in sorted(ob_states)),
        prev_completed_close=prev_close,
    )
    return SmcRealtimeTransitionResult(
        state=new_state,
        structure_transitions=tuple(structure_out),
        ob_episode_transitions=tuple(ob_out),
        ok=True,
    )


def state_fingerprint(state: SmcRealtimeTransitionState) -> str:
    """state 的稳定指纹（审计/测试用），sha256 hex。"""
    payload = {
        "daily_epoch": state.daily_epoch,
        "swing_bias": state.swing.bias,
        "swing_fired": sorted(state.swing.fired_keys),
        "internal_bias": state.internal.bias,
        "internal_fired": sorted(state.internal.fired_keys),
        "session": None
        if state.session is None
        else {
            "session_key": state.session.session_key,
            "session_open": state.session.session_open,
            "running_high": state.session.running_high,
            "running_low": state.session.running_low,
            "latest_close": state.session.latest_close,
        },
        "ob_states": [
            {"key": s.logical_ob_key, "inside": s.inside, "episode": s.current_episode_key}
            for s in state.ob_states
        ],
        "prev_completed_close": state.prev_completed_close,
    }
    return _sha256_json(payload)
