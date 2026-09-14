"""PART 7 — canonical realtime SMC input builder 测试（只消费 authoritative objects）。"""
from __future__ import annotations

import inspect
from uuid import UUID, uuid4

import pandas as pd
import pytest

from app.services.market_data_aggregation_service import (
    CanonicalCompletedQfqMinute,
    CanonicalCompletedQfqMinutes,
)
from app.services.smc_monitor_target_service import (
    SmcTargetContractError,
    build_smc_runtime_target_bundle,
)
from app.services.smc_realtime_input_service import (
    RealtimeSmcInputBundle,
    build_realtime_smc_input_bundle,
    resolve_daily_epoch,
)
from app.services.smc_realtime_transition_service import (
    evaluate_smc_realtime_transitions,
    initial_transition_state,
)

PARAMS = {"internal_filter_confluence": True}


def _slot(level, anchor_time="2026-01-04T00:00:00", crossed=False):
    return {"level": level, "anchor_index": 3, "anchor_time": anchor_time, "crossed": crossed}


def _empty_slot():
    return {"level": None, "anchor_index": None, "anchor_time": None, "crossed": False}


def _structure(swing_high=None):
    return {
        "swing_bias": 0,
        "internal_bias": 0,
        "slots": {
            "swing_high": swing_high or _empty_slot(),
            "swing_low": _empty_slot(),
            "internal_high": _empty_slot(),
            "internal_low": _empty_slot(),
        },
    }


def _runtime_target(close=10.5, params=None):
    bars = pd.DataFrame(
        {"open": [10.0], "high": [11.0], "low": [9.0], "close": [close]},
        index=pd.to_datetime(["2026-01-02"]),
    )
    smc = {
        "structure_target_state": _structure(swing_high=_slot(105.0)),
        "order_blocks": [],
        "params": dict(params if params is not None else PARAMS),
    }
    return build_smc_runtime_target_bundle(bars, smc)


def _row(hhmm, session="2026-09-14"):
    return CanonicalCompletedQfqMinute(
        bar_time=f"{session}T{hhmm}:00+08:00",
        session_key=session,
        open=104.0,
        high=106.0,
        low=103.0,
        close=105.0,
    )


def _minute_input(bars=(), qfq=True, sequence=True):
    rows = tuple(bars)
    return CanonicalCompletedQfqMinutes(
        bars=rows,
        qfq_proven=qfq,
        qfq_reason="" if qfq else "schedule stale",
        sequence_proven=sequence,
        sequence_reason="" if sequence else "missing 09:32",
        source_bar_hash="src-hash",
        adj_factor_hash="adj-hash",
        latest_completed_bar_time=rows[-1].bar_time if rows else None,
    )


def _state(runtime_target, epoch=None):
    return initial_transition_state(
        runtime_target.target_set,
        instrument_id=str(uuid4()),
        daily_epoch=epoch if epoch is not None else resolve_daily_epoch(runtime_target.target_set),
    )


class TestDailyEpoch:
    def test_epoch_is_updated_through(self):
        runtime_target = _runtime_target()
        assert resolve_daily_epoch(runtime_target.target_set) == (
            runtime_target.target_set.input_identity["updated_through"]
        )

    def test_hash_change_same_updated_through_keeps_epoch(self):
        a = _runtime_target(close=10.5)
        b = _runtime_target(close=10.9)
        assert a.target_set.input_identity["daily_bars_hash"] != (
            b.target_set.input_identity["daily_bars_hash"]
        )
        assert resolve_daily_epoch(a.target_set) == resolve_daily_epoch(b.target_set)


class TestBuilderRejectsCallerAuthority:
    def test_signature_has_no_proof_or_epoch_params(self):
        params = set(inspect.signature(build_realtime_smc_input_bundle).parameters)
        assert params == {"instrument_id", "runtime_target", "minute_input", "transition_state"}

    def test_no_duplicate_target_set_or_params_on_bundle(self):
        runtime_target = _runtime_target()
        bundle = build_realtime_smc_input_bundle(
            instrument_id=uuid4(),
            runtime_target=runtime_target,
            minute_input=_minute_input([_row("09:31")]),
            transition_state=_state(runtime_target),
        )
        assert isinstance(bundle, RealtimeSmcInputBundle)
        assert not hasattr(bundle, "effective_params")
        assert not hasattr(bundle, "target_set")

    def test_non_uuid_instrument_id_rejected(self):
        runtime_target = _runtime_target()
        with pytest.raises(SmcTargetContractError):
            build_realtime_smc_input_bundle(
                instrument_id=str(uuid4()),  # 宽松 str 不被接受
                runtime_target=runtime_target,
                minute_input=_minute_input([_row("09:31")]),
                transition_state=_state(runtime_target),
            )

    def test_non_runtime_target_rejected(self):
        runtime_target = _runtime_target()
        with pytest.raises(SmcTargetContractError):
            build_realtime_smc_input_bundle(
                instrument_id=uuid4(),
                runtime_target={"not": "runtime bundle"},  # type: ignore[arg-type]
                minute_input=_minute_input([_row("09:31")]),
                transition_state=_state(runtime_target),
            )

    def test_non_minute_input_rejected(self):
        runtime_target = _runtime_target()
        with pytest.raises(SmcTargetContractError):
            build_realtime_smc_input_bundle(
                instrument_id=uuid4(),
                runtime_target=runtime_target,
                minute_input=object(),  # type: ignore[arg-type]
                transition_state=_state(runtime_target),
            )

    def test_epoch_mismatch_rejected(self):
        runtime_target = _runtime_target()
        with pytest.raises(SmcTargetContractError):
            build_realtime_smc_input_bundle(
                instrument_id=uuid4(),
                runtime_target=runtime_target,
                minute_input=_minute_input([_row("09:31")]),
                transition_state=_state(runtime_target, epoch="2026-01-01T00:00:00+08:00"),
            )


class TestBuilderConsumesOwnerProof:
    def test_proof_false_survives_into_context(self):
        runtime_target = _runtime_target()
        bundle = build_realtime_smc_input_bundle(
            instrument_id=uuid4(),
            runtime_target=runtime_target,
            minute_input=_minute_input([_row("09:31")], qfq=False, sequence=False),
            transition_state=_state(runtime_target),
        )
        assert bundle.context.qfq_coordinate_proven is False
        assert bundle.context.completed_bar_sequence_proven is False
        assert bundle.context.qfq_coordinate_reason == "schedule stale"
        assert bundle.context.completed_bar_sequence_reason == "missing 09:32"
        assert bundle.context.effective_params is runtime_target.effective_params

    def test_valid_proofs_drive_transition_owner(self):
        runtime_target = _runtime_target()
        instrument_id = uuid4()
        state = initial_transition_state(
            runtime_target.target_set,
            instrument_id=str(instrument_id),
            daily_epoch=resolve_daily_epoch(runtime_target.target_set),
        )
        bundle = build_realtime_smc_input_bundle(
            instrument_id=instrument_id,
            runtime_target=runtime_target,
            minute_input=_minute_input(
                [_row("09:31"), CanonicalCompletedQfqMinute(
                    bar_time="2026-09-14T09:32:00+08:00",
                    session_key="2026-09-14",
                    open=105.0,
                    high=106.5,
                    low=104.0,
                    close=106.0,
                )]
            ),
            transition_state=state,
        )
        assert bundle.context.qfq_coordinate_proven is True
        assert bundle.context.completed_bar_sequence_proven is True
        assert len(bundle.qfq_minute_bars) == 2

        res = evaluate_smc_realtime_transitions(
            instrument_id=str(bundle.instrument_id),
            target_set=bundle.runtime_target.target_set,
            completed_bars=bundle.qfq_minute_bars,
            context=bundle.context,
            state=bundle.transition_state,
        )
        assert res.ok is True
        assert len(res.structure_transitions) == 1
