"""Round 2B modified-scope pure/unit tests: Experimental Filter.

Covers the PURE evaluation layer (``experimental_filter``): BREADTH_EXPANSION and
PARTICIPATION_CONFIRMATION archetypes, three-state missing semantics (MATCHED /
NOT_MATCHED / NOT_EVALUABLE), D1/D3 mandatory + D5 optional, historical /
peer not blocking, exact V0 threshold boundary, config-driven difference, input
not mutated, no banned output keys, no legacy P/Q/U/C/V dependency, Market/Index/
Style excluded, D5 uses delta explicitly, missing never coerced to zero, and
determinism.  No DB, no network, no CI.  (Prompt §19, §26.)
"""
from __future__ import annotations

import copy
from datetime import date

import app.domain.review.experimental_filter as ef
from app.domain.review.experimental_filter import ExperimentConfig

T = date(2026, 8, 11)
_D1 = date(2026, 8, 10)
_D3 = date(2026, 8, 6)
_D5 = date(2026, 8, 4)


def _ready_delta(delta: float) -> dict[str, object]:
    return {
        "status": "ready",
        "reference_date": None,
        "reference_value": None,
        "delta": delta,
    }


def _unavailable() -> dict[str, object]:
    return {
        "status": "unavailable",
        "reference_date": None,
        "reference_value": None,
        "delta": None,
    }


def _prim(delta_d1: float | None, delta_d3: float | None, delta_d5: float | None = None) -> dict:
    """Build a primitive node with explicit ready/unavailable delta contexts."""
    out: dict[str, object] = {
        "current": {"status": "ready", "value": 0.5},
        "d1": _ready_delta(delta_d1) if delta_d1 is not None else _unavailable(),
        "d3": _ready_delta(delta_d3) if delta_d3 is not None else _unavailable(),
        "historical": {"status": "insufficient_history", "percentile": None, "sample_count": 5},
        "peer": {"status": "unavailable", "percentile": None, "peer_count": 0},
    }
    if delta_d5 is not None:
        out["d5"] = _ready_delta(delta_d5)
    else:
        out["d5"] = _unavailable()
    return out


def _evidence(
    *,
    trend_d1: float | None = 0.07,
    trend_d3: float | None = 0.05,
    trend_d5: float | None = 0.03,
    advance_d1: float | None = 0.06,
    advance_d3: float | None = 0.04,
    advance_d5: float | None = 0.02,
    part_d1: float | None = 0.08,
    part_d3: float | None = 0.05,
    part_d5: float | None = 0.03,
    scope_type: str = "concept",
    scope_key: str = "A",
) -> dict:
    return {
        "scope": {
            "scope_type": scope_type,
            "scope_key": scope_key,
            "scope_name": "X",
            "pit_member_count": 10,
        },
        "trade_date": T.isoformat(),
        "primitives": {
            "trend_up_ratio": _prim(trend_d1, trend_d3, trend_d5),
            "price_advance_ratio": _prim(advance_d1, advance_d3, advance_d5),
            "participation_volume_p50": _prim(part_d1, part_d3, part_d5),
            "momentum_expanding_ratio": _prim(None, None, None),
            "price_return_mean": _prim(None, None, None),
            "price_raw_hhi": _prim(None, None, None),
        },
    }


def _find(result: dict, condition_id: str) -> dict:
    for c in result["conditions"]:
        if c["condition_id"] == condition_id:
            return c
    raise AssertionError(f"condition {condition_id} not found")


# ---------------------------------------------------------------------------
# A. BREADTH matched
# ---------------------------------------------------------------------------


def test_breadth_matched() -> None:
    ev = _evidence()
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
    assert r["evaluation_status"] == "evaluable"
    assert r["matched"] is True
    assert r["diagnostics"]["mandatory_missing"] == []
    assert _find(r, "trend_up_ratio_d1")["status"] == "matched"
    assert _find(r, "price_advance_ratio_d3")["status"] == "matched"


# ---------------------------------------------------------------------------
# B. BREADTH not_matched (mandatory available but fails V0)
# ---------------------------------------------------------------------------


def test_breadth_not_matched() -> None:
    ev = _evidence(trend_d1=-0.02)  # mandatory fails
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
    assert r["evaluation_status"] == "evaluable"
    assert r["matched"] is False
    assert _find(r, "trend_up_ratio_d1")["status"] == "not_matched"
    assert r["diagnostics"]["mandatory_missing"] == []


# ---------------------------------------------------------------------------
# C. BREADTH mandatory missing -> not_evaluable (NOT not_matched)
# ---------------------------------------------------------------------------


def test_breadth_mandatory_missing_not_evaluable() -> None:
    ev = _evidence(trend_d1=None)  # mandatory d1 unavailable
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
    assert r["evaluation_status"] == "not_evaluable"
    assert r["matched"] is False
    assert _find(r, "trend_up_ratio_d1")["status"] == "unavailable"
    assert "trend_up_ratio_d1" in r["diagnostics"]["mandatory_missing"]


# ---------------------------------------------------------------------------
# D. BREADTH D5 missing -> still evaluable
# ---------------------------------------------------------------------------


def test_breadth_d5_missing_still_evaluable() -> None:
    ev = _evidence(trend_d5=None, advance_d5=None)  # optional d5 unavailable
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
    assert r["evaluation_status"] == "evaluable"
    assert r["matched"] is True
    assert r["diagnostics"]["mandatory_missing"] == []
    assert r["diagnostics"]["optional_missing"] == [
        "trend_up_ratio_d5",
        "price_advance_ratio_d5",
    ]


# ---------------------------------------------------------------------------
# E. PARTICIPATION matched
# ---------------------------------------------------------------------------


def test_participation_matched() -> None:
    ev = _evidence()
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_PARTICIPATION_CONFIRMATION)
    assert r["evaluation_status"] == "evaluable"
    assert r["matched"] is True
    assert _find(r, "participation_volume_p50_d1")["status"] == "matched"
    assert _find(r, "price_advance_ratio_d3")["status"] == "matched"


# ---------------------------------------------------------------------------
# F. PARTICIPATION not_matched
# ---------------------------------------------------------------------------


def test_participation_not_matched() -> None:
    ev = _evidence(part_d3=-0.03)
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_PARTICIPATION_CONFIRMATION)
    assert r["evaluation_status"] == "evaluable"
    assert r["matched"] is False
    assert _find(r, "participation_volume_p50_d3")["status"] == "not_matched"


# ---------------------------------------------------------------------------
# G. PARTICIPATION mandatory missing -> not_evaluable
# ---------------------------------------------------------------------------


def test_participation_mandatory_missing_not_evaluable() -> None:
    ev = _evidence(part_d1=None)
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_PARTICIPATION_CONFIRMATION)
    assert r["evaluation_status"] == "not_evaluable"
    assert "participation_volume_p50_d1" in r["diagnostics"]["mandatory_missing"]


# ---------------------------------------------------------------------------
# H. optional trend missing -> still evaluable
# ---------------------------------------------------------------------------


def test_participation_optional_trend_missing_evaluable() -> None:
    ev = _evidence(trend_d1=None, trend_d3=None)  # optional trend unavailable
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_PARTICIPATION_CONFIRMATION)
    assert r["evaluation_status"] == "evaluable"
    assert r["matched"] is True
    assert r["diagnostics"]["optional_missing"] == [
        "trend_up_ratio_d1",
        "trend_up_ratio_d3",
    ]


# ---------------------------------------------------------------------------
# I. historical insufficient_history -> does not block
# ---------------------------------------------------------------------------


def test_historical_insufficient_does_not_block() -> None:
    ev = _evidence()
    # historical already insufficient in helper; evaluation must still be valid
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
    assert r["evaluation_status"] == "evaluable"
    assert r["matched"] is True


# ---------------------------------------------------------------------------
# J. threshold exact boundary (delta == 0 must NOT match under gt)
# ---------------------------------------------------------------------------


def test_threshold_exact_boundary_not_matched() -> None:
    ev = _evidence(trend_d1=0.0, trend_d3=0.0, advance_d1=0.0, advance_d3=0.0)
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
    assert r["matched"] is False
    assert _find(r, "trend_up_ratio_d1")["status"] == "not_matched"


def test_threshold_exact_boundary_tiny_positive_matched() -> None:
    ev = _evidence(trend_d1=1e-9, trend_d3=1e-9, advance_d1=1e-9, advance_d3=1e-9)
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
    assert r["matched"] is True


# ---------------------------------------------------------------------------
# K. same Evidence + different V0 config -> different result
# ---------------------------------------------------------------------------


def test_same_evidence_different_config_different_result() -> None:
    ev = _evidence(trend_d1=0.0, trend_d3=0.0, advance_d1=0.0, advance_d3=0.0)
    ge_config = ExperimentConfig(operator="ge", boundary=0.0)
    r_gt = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION, ExperimentConfig(operator="gt", boundary=0.0))
    r_ge = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION, ge_config)
    assert r_gt["matched"] is False
    assert r_ge["matched"] is True  # delta == 0 satisfies >= but not >


# ---------------------------------------------------------------------------
# L. input Evidence not mutated
# ---------------------------------------------------------------------------


def test_input_evidence_not_mutated() -> None:
    ev = _evidence()
    original = copy.deepcopy(ev)
    ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
    ef.evaluate_experiment(ev, ef.ARCHETYPE_PARTICIPATION_CONFIRMATION)
    assert ev == original


# ---------------------------------------------------------------------------
# M. output contains no banned keys
# ---------------------------------------------------------------------------


def test_output_no_banned_keys() -> None:
    ev = _evidence()
    for r in ef.evaluate_scope(ev):
        assert ef.candidate_result_has_banned_keys(r) is False


# ---------------------------------------------------------------------------
# N. no legacy P/Q/U/C/V dependency (module imports only scope_evidence-free paths)
# ---------------------------------------------------------------------------


def test_no_legacy_pqucv_import() -> None:
    import importlib

    mod = importlib.import_module("app.domain.review.experimental_filter")
    # The pure module must NOT import legacy filter definitions / engine.
    assert "app.domain.review.filter_definitions" not in mod.__dict__
    assert "app.domain.review.filter_engine" not in mod.__dict__


# ---------------------------------------------------------------------------
# O. Market / Index / Style excluded
# ---------------------------------------------------------------------------


def test_excluded_scope_types_not_evaluable() -> None:
    for excluded in ("market", "major_index", "style"):
        ev = _evidence(scope_type=excluded)
        r = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
        assert r["evaluation_status"] == "not_evaluable"
        assert r["matched"] is False
        assert r["diagnostics"]["mandatory_missing"] == ["scope_type_not_activated"]


def test_activated_scope_types_evaluable() -> None:
    for activated in ("concept", "industry_l1", "industry_l2", "industry_l3"):
        ev = _evidence(scope_type=activated)
        r = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
        assert r["evaluation_status"] == "evaluable"
        assert r["matched"] is True


# ---------------------------------------------------------------------------
# P. D5 uses delta explicitly (never reference_value / whole dict)
# ---------------------------------------------------------------------------


def test_d5_reads_explicit_delta() -> None:
    ev = _evidence(trend_d5=0.03, advance_d5=0.02)
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
    c = _find(r, "trend_up_ratio_d5")
    assert c["status"] == "matched"
    assert c["evidence"]["delta"] == 0.03


def test_d5_unavailable_does_not_break_mandatory() -> None:
    # D5 available but d1/d3 mandatory failing -> NOT_MATCHED (not not_evaluable)
    ev = _evidence(trend_d1=-0.01, trend_d3=-0.01, trend_d5=0.05)
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
    assert r["evaluation_status"] == "evaluable"
    assert r["matched"] is False


# ---------------------------------------------------------------------------
# Q. missing never coerced to zero
# ---------------------------------------------------------------------------


def test_missing_not_coerced_to_zero() -> None:
    # mandatory d1 missing entirely from primitives -> unavailable, NOT matched by 0
    ev = _evidence(trend_d1=None)
    del ev["primitives"]["trend_up_ratio"]["d1"]
    r = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
    assert r["evaluation_status"] == "not_evaluable"
    assert _find(r, "trend_up_ratio_d1")["status"] == "unavailable"
    assert _find(r, "trend_up_ratio_d1")["evidence"]["delta"] is None


# ---------------------------------------------------------------------------
# R. same input/config deterministic
# ---------------------------------------------------------------------------


def test_deterministic() -> None:
    ev = _evidence()
    r1 = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
    r2 = ef.evaluate_experiment(ev, ef.ARCHETYPE_BREADTH_EXPANSION)
    assert r1 == r2
