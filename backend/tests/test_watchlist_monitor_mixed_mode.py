"""[G canonical SMC cutover] WatchlistMonitor.detect_events SMC producer 契约测试。

历史沿革：本文件原覆盖「Node / 旧 SMC 各自独立决定 new-crossing vs legacy」的
mixed-mode 契约（P0 修正）。G cutover 后旧 SMC producer 已剪断，故重写为
canonical realtime SMC 接线契约；Node 新 crossing 与 VN legacy 的独立性仍成立。

锁定的生产合同：
A/B/C. canonical input → 三类 draft（smc_bos_cross / smc_choch_cross /
       smc_order_block_first_touch）全部进入事件列表；
D. canonical input unavailable → 0 SMC 事件、绝不调用 legacy producer、
   Node 事件仍正常，且既有 transition 命名空间原样保留；
E. 评估 fail closed → 上一轮 smc_realtime_transition 原样保留（不推进、不丢）；
F. 评估成功 → next_state 序列化写回 curr_state["smc_realtime_transition"]；
Negative. 生产路径不得产生 retest 事件类型；旧 producer 在模块级不可达。

纯单元测试（无 DB / 无网络）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services.node_monitor_target_service import (
    NodeMonitorTarget,
    NodeMonitorTargetSet,
)
from app.strategy.monitors import watchlist_monitor as wm
from app.strategy.monitors.volume_node_monitor import EVENT_TYPE_NODE_CLUSTER_TOUCH
from app.strategy.monitors.watchlist_monitor import WatchlistMonitor
from app.strategy.runtime import MarketDataContext, MonitorState, StrategyEventDraft

pytestmark = pytest.mark.pure_unit

_TZ = ZoneInfo("Asia/Shanghai")
_T = datetime(2026, 9, 14, 9, 40, tzinfo=_TZ)

_RETEST_TYPES = frozenset(
    {
        "smc_bos_retest",
        "smc_choch_retest",
        "smc_equal_highs_retest",
        "smc_equal_lows_retest",
    }
)


def _node_set() -> NodeMonitorTargetSet:
    """price=10.5 的单一筹码目标；配合 10.0 → 11.0 构成向上穿透。"""
    return NodeMonitorTargetSet(
        target_set_version="node_v1",
        targets=(
            NodeMonitorTarget(
                target_id="target_10_5",
                kind="peak",
                price=10.5,
                source_region_ids=("r1",),
            ),
        ),
        daily_source_hash="h_d",
        m15_source_hash="h_m",
        adjustment_context_hash="h_a",
        factor_hash="h_f",
        algorithm_version="1.0",
        output_schema_version=1,
        contract_fingerprint="fp1",
        profile_hash="prof1",
        node_regions_hash="reg1",
        updated_through=pd.Timestamp.now(),
        input_availability="available",
        input_degraded_reason=None,
    )


def _draft(event_type: str) -> StrategyEventDraft:
    return StrategyEventDraft(
        event_type=event_type,
        event_time=_T,
        dedupe_key=f"k:{event_type}",
        logical_entity=f"e:{event_type}",
        apply_cooldown=False,
    )


def _evaluation(
    *,
    drafts: Any = (),
    next_state: Any = "NEXT_STATE",
    degraded: str | None = None,
    no_op: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        drafts=tuple(drafts),
        next_state=next_state,
        degraded_reason=degraded,
        no_op=no_op,
    )


class _FakeSubMonitor:
    """假子 monitor：记录被调用次数并返回固定事件列表。"""

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.calls = 0
        self._events = events or []

    async def detect_events(self, context: Any, prev_state: Any, curr_state: Any):
        self.calls += 1
        return list(self._events)


def _build(
    *,
    node_target_set: Any = None,
    smc_realtime_input: Any = None,
    degraded_reason: str | None = None,
    vn_events: list[dict[str, Any]] | None = None,
) -> tuple[
    WatchlistMonitor,
    _FakeSubMonitor,
    _FakeSubMonitor,
    MarketDataContext,
    MonitorState,
]:
    inst_id, ver_id = uuid.uuid4(), uuid.uuid4()
    monitor = WatchlistMonitor()
    vn = _FakeSubMonitor(vn_events if vn_events is not None else [])
    smc = _FakeSubMonitor([{"source": "smc_legacy"}])
    monitor._vn = vn  # type: ignore[assignment]
    monitor._smc = smc  # type: ignore[assignment]

    context = MarketDataContext(
        instrument_id=inst_id,
        symbol="600519",
        bars_daily=pd.DataFrame(),
        bar_time=_T,
        node_target_set=node_target_set,
        smc_realtime_input=smc_realtime_input,
        smc_realtime_degraded_reason=degraded_reason,
        current_price=11.0,
        price_last=10.0,
    )
    curr_state = MonitorState(
        instrument_id=inst_id, strategy_version_id=ver_id, state={}
    )
    return monitor, vn, smc, context, curr_state


_UNSET = object()


def _prev_state_with_transition(payload: Any = _UNSET) -> MonitorState:
    """构造带 transition namespace 的 prev_state。

    使用 sentinel 而非 ``payload or default``：显式传入 ``None`` / ``{}`` 必须原样进入
    state，因为「key 存在但 value=None/corrupt」是与「key 不存在」完全不同的语义
    （前者必须持续 fail closed，后者才可 bootstrap）。
    """
    if payload is _UNSET:
        payload = {"epoch": "2026-09-11"}
    return MonitorState(
        instrument_id=uuid.uuid4(),
        strategy_version_id=uuid.uuid4(),
        state={"smc_realtime_transition": payload},
    )


# ── A/B/C：canonical input → 三类 draft 全部进入事件列表 ──────────────
async def test_canonical_drafts_all_three_types_are_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drafts = [
        _draft("smc_bos_cross"),
        _draft("smc_choch_cross"),
        _draft("smc_order_block_first_touch"),
    ]
    monkeypatch.setattr(
        wm, "evaluate_realtime_smc_events", lambda bundle: _evaluation(drafts=drafts)
    )
    monkeypatch.setattr(wm, "serialize_transition_state", lambda state: {"s": state})

    monitor, _vn, smc, context, curr_state = _build(smc_realtime_input=object())
    events = await monitor.detect_events(context, None, curr_state)

    assert [e.event_type for e in events] == [
        "smc_bos_cross",
        "smc_choch_cross",
        "smc_order_block_first_touch",
    ]
    # legacy SMC producer 不可达
    assert smc.calls == 0


# ── F：成功 → next_state 序列化写回 curr_state ───────────────────────
async def test_success_writes_back_serialized_next_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def _serialize(state: Any) -> dict[str, Any]:
        seen["state"] = state
        return {"serialized": state}

    monkeypatch.setattr(
        wm, "evaluate_realtime_smc_events", lambda bundle: _evaluation(next_state="NEXT")
    )
    monkeypatch.setattr(wm, "serialize_transition_state", _serialize)

    monitor, _vn, _smc, context, curr_state = _build(smc_realtime_input=object())
    await monitor.detect_events(context, None, curr_state)

    assert curr_state.state["smc_realtime_transition"] == {"serialized": "NEXT"}
    assert seen["state"] == "NEXT"
    # 成功时清除降级原因
    assert curr_state.state["smc_realtime_degraded_reason"] is None


# ── E：评估 fail closed → 上一轮 transition state 原样保留 ────────────
async def test_fail_closed_preserves_previous_transition_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wm,
        "evaluate_realtime_smc_events",
        lambda bundle: _evaluation(degraded="proof failed"),
    )
    serialize_calls: list[Any] = []
    monkeypatch.setattr(
        wm, "serialize_transition_state", lambda state: serialize_calls.append(state)
    )

    prev_state = _prev_state_with_transition()
    monitor, _vn, smc, context, curr_state = _build(smc_realtime_input=object())
    events = await monitor.detect_events(context, prev_state, curr_state)

    assert [e.event_type for e in events] == []  # 0 SMC 事件
    assert serialize_calls == []  # 不推进 state
    assert curr_state.state["smc_realtime_transition"] == {"epoch": "2026-09-11"}
    assert "proof failed" in (curr_state.state["smc_realtime_degraded_reason"] or "")
    assert smc.calls == 0


# ── E2：key 存在但 payload=None/{}/corrupt → 必须保留 key presence ────
@pytest.mark.parametrize("corrupt_payload", [None, {}])
async def test_fail_closed_preserves_present_corrupt_transition_namespace(
    monkeypatch: pytest.MonkeyPatch, corrupt_payload: Any
) -> None:
    """namespace 存在但 payload 为 None/{} → carry-forward 必须保留该 key。

    若丢掉 key，下一轮 prepare_transition_state 会误判「namespace 不存在 → bootstrap」，
    可能重发已消费的结构事件（fail-closed 状态被错误升级为 bootstrap）。
    """
    monkeypatch.setattr(
        wm,
        "evaluate_realtime_smc_events",
        lambda bundle: pytest.fail("canonical input unavailable 时不得评估"),
    )

    prev_state = _prev_state_with_transition(corrupt_payload)
    monitor, _vn, smc, context, curr_state = _build(
        node_target_set=_node_set(), smc_realtime_input=None
    )
    events = await monitor.detect_events(context, prev_state, curr_state)

    # key presence 必须保留，且 value 原样（不 deserialize / 不修复 / 不 bootstrap）
    assert "smc_realtime_transition" in curr_state.state
    assert curr_state.state["smc_realtime_transition"] == corrupt_payload
    # SMC 0 事件；legacy producer 不可达；Node 仍正常
    assert [e for e in events if e.event_type.startswith("smc_")] == []
    assert smc.calls == 0
    assert any(e.event_type == EVENT_TYPE_NODE_CLUSTER_TOUCH for e in events)


# ── D：canonical input unavailable → 0 SMC 事件 + Node 不受影响 ───────
async def test_unavailable_input_emits_no_smc_and_keeps_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wm,
        "evaluate_realtime_smc_events",
        lambda bundle: pytest.fail("canonical input unavailable 时不得评估"),
    )

    prev_state = _prev_state_with_transition()
    monitor, vn, smc, context, curr_state = _build(
        node_target_set=_node_set(), smc_realtime_input=None
    )
    events = await monitor.detect_events(context, prev_state, curr_state)

    # Node crossing 仍正常（10.0 → 11.0 穿透 10.5）
    assert len(events) == 1
    assert events[0].event_type == EVENT_TYPE_NODE_CLUSTER_TOUCH
    # SMC 0 事件；legacy producer 不可达；Node 已注入 → VN legacy 不跑
    assert smc.calls == 0
    assert vn.calls == 0
    # 既有 transition 命名空间原样保留（不得下一轮误判 bootstrap 重发）
    assert curr_state.state["smc_realtime_transition"] == {"epoch": "2026-09-11"}
    assert curr_state.state["smc_realtime_degraded_reason"]


# ── Negative：生产路径不得产生 retest 事件类型 ───────────────────────
async def test_no_retest_event_types_from_production_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wm, "evaluate_realtime_smc_events", lambda bundle: _evaluation())
    monkeypatch.setattr(wm, "serialize_transition_state", lambda state: {})

    monitor, _vn, _smc, context, curr_state = _build(
        node_target_set=_node_set(), smc_realtime_input=object()
    )
    events = await monitor.detect_events(context, None, curr_state)

    assert {e.event_type for e in events}.isdisjoint(_RETEST_TYPES)


# ── 旧 SMC producer 已从模块剪断（import / attribute 级）─────────────
def test_legacy_smc_producer_not_reachable_in_module() -> None:
    assert not hasattr(wm, "evaluate_smc_events")
