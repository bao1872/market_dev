"""No DB; full historical-link key must be either present or absent."""
from __future__ import annotations

import copy
import pytest
from app.services.strategy_research_read_service import SnapshotInvalid, _check_snapshot, evaluate


def _sample():
    return {
        "schema":"panji-strategy-evaluation-v1",
        "metadata":{"strategy_key":"zigzag_two_stage", "strategy_name":"test", "version":"test", "generated_at":"2026-06-04",
                    "evaluation_protocol":"synthetic", "model_sha256":"test", "data_sha256":"test", "audit_ref":"test",
                    "cohort":"ALL_EX_ANTE_ALERTS", "status":"AUDITED"},
        "models":[{"id":"d1", "name":"test", "side":"BUY"}],
        "events":[{"event_id":"event-1", "model_id":"d1", "symbol":"SH:600000", "side":"BUY", "signal_time":"2026-06-03",
                   "outcome":"SUCCESS", "mfe_pct":10, "mae_pct":3, "candidate_id":"c1", "observation_id":"o1",
                   "stage":"D1", "decision_date":"2026-06-03", "model_release_id":"r1"}],
    }


def test_crosslinked_event_projected_in_table_and_scatter():
    result = evaluate(_check_snapshot(_sample()), "BUY", None, 5, 5)
    for row in (result["table"][0], result["points"][0]):
        assert [row[k] for k in ("candidate_id", "observation_id", "stage", "decision_date", "model_release_id")] == [
            "c1", "o1", "D1", "2026-06-03", "r1"
        ]


def test_partial_identity_is_rejected():
    for missing in ("candidate_id", "observation_id", "stage", "decision_date", "model_release_id"):
        sample = _sample()
        del sample["events"][0][missing]
        with pytest.raises(SnapshotInvalid, match="link identity"):
            _check_snapshot(sample)


def test_wrong_decision_day_is_rejected():
    sample = _sample()
    sample["events"][0]["decision_date"] = "2026-06-02"
    with pytest.raises(SnapshotInvalid, match="stage/decision date"):
        _check_snapshot(sample)


def test_legacy_without_identity_remains_readable_but_has_no_deep_link():
    sample = _sample()
    for key in ("candidate_id", "observation_id", "stage", "decision_date", "model_release_id"):
        del sample["events"][0][key]
    result = evaluate(_check_snapshot(sample), "BUY", None, 5, 5)
    assert result["table"][0]["candidate_id"] is None
