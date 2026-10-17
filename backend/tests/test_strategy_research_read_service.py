"""S1 pure-service contract tests; no DB or live model required."""
import json
import pytest
from app.services.strategy_research_read_service import (
    SCHEMA, SnapshotInvalid, SnapshotUnavailable, _check_snapshot,
    _touch_order, evaluate, read_snapshot,
)


def fixture():
    return {
        "schema": SCHEMA,
        "metadata": {
            "strategy_key": "zigzag_two_stage", "strategy_name": "两层 ZigZag", "version": "test-fixture",
            "generated_at": "2026-09-30", "evaluation_protocol": "structure-endpoint-v1",
            "model_sha256": "test-bundle-sha", "data_sha256": "test-data-sha",
            "audit_ref": "test-only-do-not-publish", "status": "AUDITED", "cohort": "ALL_EX_ANTE_ALERTS",
        },
        "models": [{"id": "joint_d1", "name": "D1 联合", "side": "BUY"}, {"id": "joint_d2", "name": "D2 联合", "side": "BUY"}],
        "events": [
            {"event_id": "SH:600000|10|L", "model_id": "joint_d1", "side": "BUY", "symbol": "SH:600000", "signal_time": "2026-06-02", "outcome": "SUCCESS", "mfe_pct": 12., "mae_pct": 3., "mdd_pct": 4., "endpoint_pct": 8., "path": [{"favorable_pct": 4., "adverse_pct": 1.}, {"favorable_pct": 10., "adverse_pct": 3.}]},
            {"event_id": "SZ:000001|20|L", "model_id": "joint_d1", "side": "BUY", "symbol": "SZ:000001", "signal_time": "2026-06-03", "outcome": "FAIL", "mfe_pct": 2., "mae_pct": 8., "mdd_pct": 11., "endpoint_pct": -6., "path": [{"favorable_pct": 2., "adverse_pct": 6.}]},
            {"event_id": "SH:600000|30|L", "model_id": "joint_d1", "side": "BUY", "symbol": "SH:600000", "signal_time": "2026-06-04", "outcome": "UNRESOLVED", "mfe_pct": None, "mae_pct": None, "mdd_pct": None, "endpoint_pct": None},
            {"event_id": "SH:600000|10|L", "model_id": "joint_d2", "side": "BUY", "symbol": "SH:600000", "signal_time": "2026-06-03", "outcome": "SUCCESS", "mfe_pct": 10., "mae_pct": 4., "mdd_pct": 5., "endpoint_pct": 7.},
        ],
    }


def test_complete_signal_denominator_and_joint():
    r = evaluate(_check_snapshot(fixture()), "BUY", None, 5, 5)
    a = r["models"][0]
    assert (a["n_signals"], a["n_resolved"], a["n_unresolved"], a["n_success"]) == (3, 2, 1, 1)
    assert a["success_rate"] == .5
    assert a["joint_count"] == 1 and a["joint_rate"] == .5
    assert a["touch_order"]["TARGET_FIRST"] == 1
    assert a["touch_order"]["RISK_FIRST"] == 1
    assert r["models"][1]["joint_rate"] == 1
    assert len(r["points"]) == 3


def test_symbol_filter_and_unresolved():
    r = evaluate(_check_snapshot(fixture()), "BUY", "SH:600000", 5, 5)
    assert r["models"][0]["n_signals"] == 2
    assert r["models"][0]["success_rate"] == 1
    assert r["models"][0]["n_unresolved"] == 1


def test_same_day_unknown_and_unavailable():
    assert _touch_order([{"favorable_pct": 6, "adverse_pct": 6}], 5, 5) == "SAME_BAR_UNKNOWN"
    assert _touch_order(None, 5, 5) == "UNAVAILABLE"
    assert _touch_order([{"favorable_pct": 1, "adverse_pct": 1}], 5, 5) == "NEITHER"


def test_bad_event_fails_closed():
    p = fixture()
    p["events"][0]["outcome"] = "UNRESOLVED"
    with pytest.raises(SnapshotInvalid, match="unresolved events"):
        _check_snapshot(p)


def test_duplicate_model_event_fails_closed():
    p = fixture()
    p["events"].append(dict(p["events"][0]))
    with pytest.raises(SnapshotInvalid, match="duplicate model event"):
        _check_snapshot(p)


def test_unavailable_is_not_zero(monkeypatch):
    monkeypatch.delenv("STRATEGY_RESEARCH_SNAPSHOT", raising=False)
    with pytest.raises(SnapshotUnavailable):
        read_snapshot()


def test_json_snapshot_roundtrip(monkeypatch, tmp_path):
    f = tmp_path / "snapshot.json"
    f.write_text(json.dumps(fixture()), encoding="utf-8")
    monkeypatch.setenv("STRATEGY_RESEARCH_SNAPSHOT", str(f))
    assert read_snapshot()["metadata"]["version"] == "test-fixture"


def independent_fixture():
    """Reuse the wire fixture, explicitly select the new production contract."""
    p = fixture()
    p['metadata'].update(maturity_contract='INDEPENDENT_STRUCTURE_AND_PATH_V1', path_horizon=20)
    for row in p['events']:
        row['structure_outcome'] = row['outcome']
        row['path_status'] = 'UNRESOLVED' if row['outcome'] == 'UNRESOLVED' else 'MATURED'
        if row['path_status'] == 'MATURED':
            row['path'] = [row.get('path', [{'favorable_pct': 1., 'adverse_pct': 1.}])[0].copy() for _ in range(20)]
    return p


def test_path_maturity_is_independent_of_structure_resolution():
    p = independent_fixture()
    # Enough next-open bars, while the structural label is still unresolved.
    row = p['events'][2]
    row.update(path_status='MATURED', endpoint_pct=9., mfe_pct=11., mae_pct=2., mdd_pct=3.,
               path=[{'favorable_pct': 11., 'adverse_pct': 2.} for _ in range(20)])
    # Conversely, structure can resolve before the 20-bar path matures.
    p['events'][0].update(path_status='UNRESOLVED', endpoint_pct=None, mfe_pct=None, mae_pct=None, mdd_pct=None, path=None)
    a = evaluate(_check_snapshot(p), 'BUY', None, 5, 5)['models'][0]
    assert (a['n_structure_matured'], a['n_unresolved'], a['n_path'], a['n_path_unresolved']) == (2, 1, 2, 1)
    assert a['endpoint_p50'] == 1.5  # -6 and +9, including unresolved structure
    assert a['joint_count'] == 1


@pytest.mark.parametrize('mutation', ['short_path', 'unmatured_metrics', 'missing_status'])
def test_independent_maturity_contract_fails_closed(mutation):
    p = independent_fixture()
    row = p['events'][0]
    if mutation == 'short_path':
        row['path'].pop()
    elif mutation == 'unmatured_metrics':
        row['path_status'] = 'UNRESOLVED'
    else:
        row.pop('structure_outcome')
    with pytest.raises(SnapshotInvalid):
        _check_snapshot(p)
