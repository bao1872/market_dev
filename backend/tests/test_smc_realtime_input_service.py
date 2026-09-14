"""F1 canonical realtime SMC input bundle 单元测试。"""
from __future__ import annotations

import pandas as pd
import pytest

from app.services.smc_monitor_target_service import (
    SmcTargetContractError,
    build_smc_monitor_target_set,
)
from app.services.smc_realtime_input_service import (
    CompletedMinuteSequenceProof,
    build_realtime_smc_input_bundle,
    resolve_daily_epoch,
)
from app.services.smc_realtime_transition_service import (
    QfqMinuteBar,
    evaluate_smc_realtime_transitions,
    initial_transition_state,
)

PARAMS = {"internal_filter_confluence": True}
INSTR = "INST-1"


def _slot(level, anchor_time="2026-01-04T00:00:00", crossed=False):
    return {"level": level, "anchor_index": 3, "anchor_time": anchor_time, "crossed": crossed}


def _structure(swing_high=None, swing_bias=0):
    return {
        "swing_bias": swing_bias,
        "internal_bias": 0,
        "slots": {
            "swing_high": swing_high or {"level": None, "anchor_index": None, "anchor_time": None, "crossed": False},
            "swing_low": {"level": None, "anchor_index": None, "anchor_time": None, "crossed": False},
            "internal_high": {"level": None, "anchor_index": None, "anchor_time": None, "crossed": False},
            "internal_low": {"level": None, "anchor_index": None, "anchor_time": None, "crossed": False},
        },
    }


def _target_set(close=10.5, structure=None, params=None):
    bars = pd.DataFrame(
        {"open": [10.0], "high": [11.0], "low": [9.0], "close": [close]},
        index=pd.to_datetime(["2026-01-02"]),
    )
    return build_smc_monitor_target_set(
        bars,
        {
            "structure_target_state": structure or _structure(swing_high=_slot(105.0)),
            "order_blocks": [],
            "params": dict(params if params is not None else PARAMS),
        },
    )


def _bar(t, c, session="2026-09-14"):
    return QfqMinuteBar(bar_time=t, session_key=session, open=c, high=c + 0.5, low=c - 0.5, close=c)


def _proof(proven=True, reason=""):
    return CompletedMinuteSequenceProof(proven=proven, reason=reason, source="market_data")


class TestDailyEpoch:
    def test_epoch_is_updated_through(self):
        ts = _target_set()
        assert resolve_daily_epoch(ts) == ts.input_identity["updated_through"]

    def test_hash_change_same_updated_through_keeps_epoch(self):
        ts_a = _target_set(close=10.5)
        ts_b = _target_set(close=10.9)  # 同最后一根日期，仅价格不同
        assert ts_a.input_identity["updated_through"] == ts_b.input_identity["updated_through"]
        assert ts_a.input_identity["daily_bars_hash"] != ts_b.input_identity["daily_bars_hash"]
        assert resolve_daily_epoch(ts_a) == resolve_daily_epoch(ts_b)

    def test_missing_updated_through_raises(self):
        ts = _target_set()
        object.__setattr__(ts, "input_identity", {})  # frozen dataclass 强制构造非法态
        with pytest.raises(SmcTargetContractError):
            resolve_daily_epoch(ts)


class TestBundle:
    def test_bundle_carries_proofs_not_hardcoded(self):
        ts = _target_set()
        bundle = build_realtime_smc_input_bundle(
            instrument_id=INSTR,
            target_set=ts,
            effective_params=PARAMS,
            qfq_minute_bars=[_bar("09:31", 104.0)],
            qfq_coordinate_proven=False,
            qfq_coordinate_reason="context stale",
            sequence_proof=_proof(proven=False, reason="missing 09:30"),
        )
        assert bundle.context.qfq_coordinate_proven is False
        assert bundle.context.completed_bar_sequence_proven is False
        assert bundle.daily_epoch == resolve_daily_epoch(ts)

    def test_params_hash_mismatch_raises(self):
        ts = _target_set()
        with pytest.raises(SmcTargetContractError):
            build_realtime_smc_input_bundle(
                instrument_id=INSTR,
                target_set=ts,
                effective_params={"internal_filter_confluence": True, "extra": 1},
                qfq_minute_bars=[],
                qfq_coordinate_proven=True,
                sequence_proof=_proof(),
            )

    def test_bundle_feeds_transition_owner_fail_closed(self):
        ts = _target_set()
        bundle = build_realtime_smc_input_bundle(
            instrument_id=INSTR,
            target_set=ts,
            effective_params=PARAMS,
            qfq_minute_bars=[_bar("09:31", 104.0), _bar("09:32", 106.0)],
            qfq_coordinate_proven=False,
            sequence_proof=_proof(),
        )
        state = initial_transition_state(ts, instrument_id=INSTR, daily_epoch=bundle.daily_epoch)
        res = evaluate_smc_realtime_transitions(
            instrument_id=bundle.instrument_id,
            target_set=bundle.target_set,
            completed_bars=bundle.qfq_minute_bars,
            context=bundle.context,
            state=state,
        )
        assert res.ok is False
        assert res.structure_transitions == ()

    def test_bundle_valid_proofs_allow_transition(self):
        ts = _target_set()
        bundle = build_realtime_smc_input_bundle(
            instrument_id=INSTR,
            target_set=ts,
            effective_params=PARAMS,
            qfq_minute_bars=[_bar("09:31", 104.0), _bar("09:32", 106.0)],
            qfq_coordinate_proven=True,
            sequence_proof=_proof(),
        )
        state = initial_transition_state(ts, instrument_id=INSTR, daily_epoch=bundle.daily_epoch)
        res = evaluate_smc_realtime_transitions(
            instrument_id=bundle.instrument_id,
            target_set=bundle.target_set,
            completed_bars=bundle.qfq_minute_bars,
            context=bundle.context,
            state=state,
        )
        assert res.ok is True
        assert len(res.structure_transitions) == 1
