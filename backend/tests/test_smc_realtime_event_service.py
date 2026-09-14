"""F2 PART 8 (prepare_transition_state) + PART 11 (evaluate_realtime_smc_events) 单元测试。

覆盖：
- state preparation：namespace absent / present+valid+same epoch / present+valid+new epoch /
  present+None / present+malformed（corrupt fail-closed）。
- event adapter：empty bars no_op / qfq proof false / sequence proof false /
  BOS structure / CHoCH same transition_key / OB episode / adapter target missing fail closed。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pandas as pd

from app.services.market_data_aggregation_service import (
    CanonicalCompletedQfqMinute,
    CanonicalCompletedQfqMinutes,
)
from app.services.smc_monitor_target_service import (
    SmcRuntimeTargetBundle,
    build_smc_monitor_target_set,
)
from app.services.smc_realtime_event_service import (
    SMC_BOS_CROSS,
    SMC_CHOCH_CROSS,
    SMC_ORDER_BLOCK_FIRST_TOUCH,
    evaluate_realtime_smc_events,
    prepare_transition_state,
)
from app.services.smc_realtime_input_service import (
    build_realtime_smc_input_bundle,
    resolve_daily_epoch,
)
from app.services.smc_realtime_transition_service import (
    SmcRealtimeTransitionResult,
    StructureTransition,
    initial_transition_state,
    serialize_transition_state,
    structure_transition_key,
)

PARAMS = {"internal_filter_confluence": True}
INSTR_ID: UUID = uuid4()
INSTR_STR = str(INSTR_ID)
SESSION = "2026-09-14"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _iso(session, hhmm):
    return f"{session}T{hhmm}:00+08:00"


def _slot(level=None, anchor_index=None, anchor_time=None, crossed=False):
    return {"level": level, "anchor_index": anchor_index, "anchor_time": anchor_time, "crossed": crossed}


def _structure(swing_high=None, swing_low=None, internal_high=None, internal_low=None,
               swing_bias=0, internal_bias=0):
    return {
        "swing_bias": swing_bias,
        "internal_bias": internal_bias,
        "slots": {
            "swing_high": swing_high or _slot(),
            "swing_low": swing_low or _slot(),
            "internal_high": internal_high or _slot(),
            "internal_low": internal_low or _slot(),
        },
    }


def _ob(internal=False, bias=1, bar_low=10.0, bar_high=11.0,
        anchor_index=1, anchor_time="2026-01-02T00:00:00",
        confirmed_index=2, confirmed_time="2026-01-03T00:00:00"):
    return {
        "internal": internal, "bias": bias, "bar_low": bar_low, "bar_high": bar_high,
        "anchor_index": anchor_index, "anchor_time": anchor_time,
        "confirmed_index": confirmed_index, "confirmed_time": confirmed_time,
        "mitigated_index": None, "entered": False,
    }


def _sh(level=105.0):
    return _slot(level=level, anchor_index=3, anchor_time="2026-01-04T00:00:00", crossed=False)


def _target_set(structure, obs=None, params=None):
    bars = pd.DataFrame(
        {"open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5]},
        index=pd.to_datetime(["2026-01-01"]),
    )
    return build_smc_monitor_target_set(
        bars,
        {
            "structure_target_state": structure,
            "order_blocks": obs or [],
            "params": dict(params if params is not None else PARAMS),
        },
    )


def _rt(ts, params=None):
    return SmcRuntimeTargetBundle(
        target_set=ts, effective_params=dict(params if params is not None else PARAMS)
    )


def _cqm(t, o, h, low, c, session=SESSION):
    return CanonicalCompletedQfqMinute(
        bar_time=_iso(session, t), session_key=session, open=o, high=h, low=low, close=c
    )


def _minute(bars, *, qfq_proven=True, sequence_proven=True, qfq_reason="", sequence_reason="",
            adj_factor_hash="adj1"):
    if bars:
        latest = bars[-1].bar_time
        qreason = qfq_reason or "ok"
        sreason = sequence_reason or "ok"
    else:
        latest = None
        qreason = qfq_reason or "no new completed bars"
        sreason = sequence_reason or "no new completed bars"
    return CanonicalCompletedQfqMinutes(
        bars=tuple(bars),
        qfq_proven=qfq_proven,
        qfq_reason=qreason,
        sequence_proven=sequence_proven,
        sequence_reason=sreason,
        source_bar_hash="src1",
        adj_factor_hash=adj_factor_hash,
        latest_completed_bar_time=latest,
    )


def _bundle(rt, minute, state):
    return build_realtime_smc_input_bundle(
        instrument_id=INSTR_ID, runtime_target=rt, minute_input=minute, transition_state=state
    )


def _initial_state(ts):
    return initial_transition_state(ts, instrument_id=INSTR_STR, daily_epoch=resolve_daily_epoch(ts))


# ---------------------------------------------------------------------------
# PART 8 — state preparation
# ---------------------------------------------------------------------------


def test_namespace_absent_bootstrap():
    ts = _target_set(_structure(swing_high=_sh(level=105.0)))
    epoch = resolve_daily_epoch(ts)
    r = prepare_transition_state(
        instrument_id=INSTR_ID, target_set=ts,
        persisted_namespace_present=False, persisted_payload=None,
    )
    assert r.ok is True
    assert r.state is not None
    assert r.state.daily_epoch == epoch
    assert r.degraded_reason == ""


def test_namespace_present_valid_same_epoch_restore():
    ts = _target_set(_structure(swing_high=_sh(level=105.0)))
    epoch = resolve_daily_epoch(ts)
    state = initial_transition_state(ts, instrument_id=INSTR_STR, daily_epoch=epoch)
    payload = serialize_transition_state(state)
    r = prepare_transition_state(
        instrument_id=INSTR_ID, target_set=ts,
        persisted_namespace_present=True, persisted_payload=payload,
    )
    assert r.ok is True
    assert r.state == state  # restored exactly


def test_namespace_present_valid_new_epoch_reinit():
    ts = _target_set(_structure(swing_high=_sh(level=105.0)))
    epoch = resolve_daily_epoch(ts)
    old_state = initial_transition_state(ts, instrument_id=INSTR_STR, daily_epoch="1999-01-01")
    payload = serialize_transition_state(old_state)
    r = prepare_transition_state(
        instrument_id=INSTR_ID, target_set=ts,
        persisted_namespace_present=True, persisted_payload=payload,
    )
    assert r.ok is True
    assert r.state is not None
    assert r.state.daily_epoch == epoch  # re-initialized to new C3 epoch
    assert r.state.daily_epoch != "1999-01-01"


def test_namespace_present_none_corrupt():
    ts = _target_set(_structure(swing_high=_sh(level=105.0)))
    r = prepare_transition_state(
        instrument_id=INSTR_ID, target_set=ts,
        persisted_namespace_present=True, persisted_payload=None,
    )
    assert r.ok is False
    assert r.state is None
    assert "corrupt" in r.degraded_reason.lower()


def test_namespace_present_malformed_corrupt():
    ts = _target_set(_structure(swing_high=_sh(level=105.0)))
    r = prepare_transition_state(
        instrument_id=INSTR_ID, target_set=ts,
        persisted_namespace_present=True, persisted_payload={"daily_epoch": "x"},
    )
    assert r.ok is False
    assert "corrupt" in r.degraded_reason.lower()


# ---------------------------------------------------------------------------
# PART 11 — event adapter
# ---------------------------------------------------------------------------


def test_empty_bars_noop():
    ts = _target_set(_structure(swing_high=_sh(level=105.0), swing_bias=1))
    rt = _rt(ts)
    state = _initial_state(ts)
    minute = _minute([], qfq_proven=False, sequence_proven=False)
    bundle = _bundle(rt, minute, state)
    res = evaluate_realtime_smc_events(bundle)
    assert res.no_op is True
    assert res.drafts == ()
    assert res.next_state is state
    assert res.degraded_reason is None


def test_qfq_proof_false():
    ts = _target_set(_structure(swing_high=_sh(level=105.0), swing_bias=1))
    rt = _rt(ts)
    state = _initial_state(ts)
    bars = [_cqm("09:31", 100, 100, 99, 100), _cqm("09:32", 100, 106, 100, 106)]
    minute = _minute(bars, qfq_proven=False, sequence_proven=True)
    bundle = _bundle(rt, minute, state)
    res = evaluate_realtime_smc_events(bundle)
    assert res.drafts == ()
    assert res.next_state is state  # original state unchanged
    assert res.degraded_reason is not None
    assert res.no_op is False


def test_sequence_proof_false():
    ts = _target_set(_structure(swing_high=_sh(level=105.0), swing_bias=1))
    rt = _rt(ts)
    state = _initial_state(ts)
    bars = [_cqm("09:31", 100, 100, 99, 100), _cqm("09:32", 100, 106, 100, 106)]
    minute = _minute(bars, qfq_proven=True, sequence_proven=False)
    bundle = _bundle(rt, minute, state)
    res = evaluate_realtime_smc_events(bundle)
    assert res.drafts == ()
    assert res.next_state is state
    assert res.degraded_reason is not None
    assert res.no_op is False


def test_bos_structure():
    ts = _target_set(_structure(swing_high=_sh(level=105.0), swing_bias=1))
    rt = _rt(ts)
    state = _initial_state(ts)
    bars = [_cqm("09:31", 100, 100, 99, 100), _cqm("09:32", 100, 106, 100, 106)]
    minute = _minute(bars)
    bundle = _bundle(rt, minute, state)
    res = evaluate_realtime_smc_events(bundle)
    assert len(res.drafts) == 1
    d = res.drafts[0]
    assert d.event_type == SMC_BOS_CROSS
    assert d.apply_cooldown is False
    expected_key = "smc_struct_transition:" + structure_transition_key(
        INSTR_STR, "swing", "high", "2026-01-04T00:00:00"
    )
    assert d.dedupe_key == expected_key
    assert d.logical_entity == "smc_structure:" + structure_transition_key(
        INSTR_STR, "swing", "high", "2026-01-04T00:00:00"
    )
    assert res.next_state is not state  # state advanced


def test_choch_same_transition_key():
    # 同一 pivot（transition_key 不含 event_type）只因 bias 不同产生 CHoCH，dedupe identity 与 BOS 一致。
    ts = _target_set(_structure(swing_high=_sh(level=105.0), swing_bias=-1))
    rt = _rt(ts)
    state = _initial_state(ts)
    bars = [_cqm("09:31", 100, 100, 99, 100), _cqm("09:32", 100, 106, 100, 106)]
    minute = _minute(bars)
    bundle = _bundle(rt, minute, state)
    res = evaluate_realtime_smc_events(bundle)
    assert len(res.drafts) == 1
    d = res.drafts[0]
    assert d.event_type == SMC_CHOCH_CROSS
    assert d.dedupe_key == "smc_struct_transition:" + structure_transition_key(
        INSTR_STR, "swing", "high", "2026-01-04T00:00:00"
    )


def test_ob_episode():
    ts = _target_set(_structure(swing_high=_sh(level=9999.0)), obs=[_ob()])
    rt = _rt(ts)
    state = _initial_state(ts)
    bars = [_cqm("09:31", 10.5, 10.5, 10.0, 10.5)]  # 触碰 OB zone 10-11
    minute = _minute(bars)
    bundle = _bundle(rt, minute, state)
    res = evaluate_realtime_smc_events(bundle)
    assert len(res.drafts) == 1
    d = res.drafts[0]
    assert d.event_type == SMC_ORDER_BLOCK_FIRST_TOUCH
    assert d.apply_cooldown is False
    assert d.dedupe_key.startswith("smc_ob_episode:")
    assert d.logical_entity.startswith("smc_ob:")
    assert res.next_state is not state


def test_adapter_target_missing_fail_closed():
    ts = _target_set(_structure(swing_high=_sh(level=105.0), swing_bias=1))
    rt = _rt(ts)
    state = _initial_state(ts)
    bars = [_cqm("09:31", 100, 100, 99, 100), _cqm("09:32", 100, 106, 100, 106)]
    minute = _minute(bars)
    bundle = _bundle(rt, minute, state)

    fake_transition = StructureTransition(
        transition_key="x", instrument_id=INSTR_STR, lane="swing", kind="high",
        anchor_time="2026-01-04T00:00:00", event_type="BOS", bias_before=1,
        bias_after=-1, confirmed_bar_time=_iso(SESSION, "09:32"), target_id="MISSING",
    )
    fake_result = SmcRealtimeTransitionResult(
        state=state, structure_transitions=(fake_transition,), ok=True
    )
    from unittest.mock import patch

    with patch(
        "app.services.smc_realtime_event_service.evaluate_smc_realtime_transitions",
        return_value=fake_result,
    ):
        res = evaluate_realtime_smc_events(bundle)
    assert res.drafts == ()
    assert res.next_state is state  # original state unchanged
    assert "missing" in (res.degraded_reason or "").lower()
