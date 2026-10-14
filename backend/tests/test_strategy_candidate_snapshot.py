"""Synthetic fixtures are tests only, NEVER published data."""
from __future__ import annotations
import copy
import importlib.util
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "app" / "services" / "strategy_candidate_snapshot.py"
_spec = importlib.util.spec_from_file_location("panji_candidate_snapshot_under_test", SRC)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)


def fixture():
    obs = {"observation_id": "obs1", "stage": "D1", "decision_date": "2026-06-03", "data_cutoff": "2026-06-03",
           "l1_score": .73, "l2_score": .64, "l1_threshold": .7, "l2_threshold": .6, "joint_alert": True,
           "reference_price": 10.5, "reference_confirmed_at": "2026-06-02", "first_breakout": True}
    item = {"observation_id": "obs1", "candidate_id": "cand1", "symbol": "SH:600000", "stage": "D1", "kind": "L", "l1_score": .73, "l2_score": .64, "joint_alert": True, "first_breakout": True}
    return {"schema": mod.SCHEMA,
            "metadata": {"strategy_id": "zigzag_two_stage", "release_id": "release1", "feature_sha": "hash", "audit_ref": "reference", "generated_at": "2026-06-04T10:00:00+08:00", "status": "AUDITED"},
            "stocks": {"SH:600000": {"bars": [
                {"date": "2026-06-01", "open": 10, "high": 11, "low": 9, "close": 10.5},
                {"date": "2026-06-02", "open": 10.5, "high": 10.7, "low": 9.9, "close": 10.3},
                {"date": "2026-06-03", "open": 10.3, "high": 11.4, "low": 10, "close": 11}],
                "candidates": [{"candidate_id": "cand1", "anchor_date": "2026-06-01", "visible_at": "2026-06-02", "kind": "L", "anchor_price": 9, "observations": [obs]}]}},
            "daily_batches": [{"trade_date": "2026-06-03", "release_id": "release1", "status": "PUBLISHED", "items": [item]}]}


def check_reject(patch):
    blob = fixture()
    patch(blob)
    try:
        mod.validate(blob)
    except mod.DatasetInvalid:
        return
    raise AssertionError("corrupt dataset accepted")


def test_valid_and_exact_crosslink():
    d = mod.validate(fixture())
    row = mod.get_today(d)["items"][0]
    assert row["candidate_id"] == mod.get_stock(d, "SH:600000")["candidates"][0]["candidate_id"]
    assert row["l1_score"] == .73


def test_pit_future_reference_rejected():
    check_reject(lambda d: d["stocks"]["SH:600000"]["candidates"][0]["observations"][0].update(reference_confirmed_at="2026-06-04"))


def test_no_false_joint_signal():
    check_reject(lambda d: d["stocks"]["SH:600000"]["candidates"][0]["observations"][0].update(l1_score=.3))


def test_daily_score_cannot_diverge():
    check_reject(lambda d: d["daily_batches"][0]["items"][0].update(l2_score=.999))


def test_breakout_requires_real_ohlc_crossing():
    check_reject(lambda d: d["stocks"]["SH:600000"]["candidates"][0]["observations"][0].update(reference_price=13))


def test_no_duplicate_first_breakout():
    def corrupt(d):
        cand = d["stocks"]["SH:600000"]["candidates"][0]
        item = copy.deepcopy(cand["observations"][0]); item.update(observation_id="obs2",stage="D2",decision_date="2026-06-04", data_cutoff="2026-06-03")
        cand["observations"].append(item)
        d["stocks"]["SH:600000"]["bars"].append({"date": "2026-06-04", "open":11,"high":12,"low":10.5,"close":11.8})
    check_reject(corrupt)


def test_unpublished_is_never_zero_signals():
    check_reject(lambda d: d["daily_batches"][0].update(status="FAILED"))
