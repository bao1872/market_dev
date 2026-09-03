"""Modified-scope pure/unit tests for SLICE 4 / Price backend contracts.

No DB, no network. Two narrow owners are locked:

1. ``_compute_field_rolling`` variance20 (SLICE 4 §三)
2. ``_select_published_compositions`` + ``_build_price_projection`` (SLICE 4 §七/§八)

Locks (from the Slice 4 spec §十三):
 1. variance baseline excludes T
 2. variance population definition
 3. <2 finite baseline -> variance/std null
 4. null excluded, not zero
 5. variance/std mathematical consistency
 6. price Composition projection keeps date slots
 7. unpublished same-day Composition must not enter history
 8. missing published Composition -> null
 9. capital_tilt verbatim (never recomputed as AW - EW)
10. leadership ids/status/jaccard/migration verbatim
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.services.review_scope_diagnostics_service import (
    _build_price_projection,
    _compute_field_rolling,
    _select_published_compositions,
)


# ---------------------------------------------------------------------------
# tiny row stand-in: mirrors ReviewScopeCompositionSnapshot columns
# ---------------------------------------------------------------------------
class _Row:
    def __init__(self, trade_date: date, review_run_id: str, composition_payload: dict[str, Any] | None):
        self.trade_date = trade_date
        self.review_run_id = review_run_id
        self.composition_payload = composition_payload


def _d(y: int, m: int, day: int) -> date:
    return date(y, m, day)


# ---------------------------------------------------------------------------
# variance20
# ---------------------------------------------------------------------------


def test_pv1_variance_baseline_excludes_current_t():
    # baseline(T) = strictly before T -> variance20[0] must be null (no baseline)
    out = _compute_field_rolling([0.01, 0.02, 0.03], window=20)
    assert out["variance20"][0] is None
    # at index 1 the baseline holds only [0.01] -> 1 finite sample -> null
    assert out["variance20"][1] is None
    # at index 2 the baseline holds [0.01, 0.02] -> populated
    assert out["variance20"][2] is not None


def test_pv2_variance_is_population_not_sample():
    # baseline(2) = [0.0, 0.02] (index 2 excluded).
    # population var = 0.0001 ; sample var (n-1) would be 0.0002
    out = _compute_field_rolling([0.0, 0.02, 0.99], window=20)
    var = out["variance20"][2]
    assert var is not None
    assert abs(var - 0.0001) < 1e-12
    # population: divide by n (2), NOT n-1
    assert abs(var - 0.0002) > 1e-12


def test_pv3_less_than_two_finite_baseline_is_null():
    # baseline has exactly 1 finite value -> both variance and std null
    out = _compute_field_rolling([0.01, 0.02], window=20)
    assert out["variance20"][1] is None
    assert out["std20"][1] is None
    # empty baseline -> null
    out0 = _compute_field_rolling([0.01], window=20)
    assert out0["variance20"][0] is None
    assert out0["std20"][0] is None


def test_pv4_null_excluded_not_zero():
    # a null slot is EXCLUDED from the baseline, never coerced to 0.
    # series = [0.0, None, 0.02] -> baseline(2) = [0.0] (null dropped) -> 1 finite -> null
    with_null = _compute_field_rolling([0.0, None, 0.02], window=20)
    assert with_null["variance20"][2] is None
    # If null had been coerced to 0 the baseline would be [0.0, 0.0] -> variance 0.0
    assert with_null["variance20"][2] != 0.0


def test_pv5_variance_std_mathematically_consistent():
    series = [0.01, -0.02, 0.03, 0.005, -0.012, 0.021, 0.0]
    out = _compute_field_rolling(series, window=20)
    for i, (var, std) in enumerate(zip(out["variance20"], out["std20"], strict=True)):
        if var is None:
            assert std is None, f"index {i}: variance null but std present"
            continue
        assert std is not None, f"index {i}: variance present but std null"
        assert abs(std - (var**0.5)) < 1e-12, f"index {i}: std != sqrt(variance)"


# ---------------------------------------------------------------------------
# price Composition history projection
# ---------------------------------------------------------------------------


def _comp(tilt: float | None = None, lead: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"internal_structure_facts": {"capital_tilt": {"capital_tilt": tilt}}}
    if lead is not None:
        payload["leadership"] = lead
    return payload


def test_ph6_price_projection_keeps_date_slots():
    d1, d2, d3 = _d(2024, 1, 2), _d(2024, 1, 3), _d(2024, 1, 4)
    comps = {
        d1: _comp(tilt=0.004),
        d2: None,  # published date, Composition missing -> slot kept as null
        d3: _comp(tilt=-0.002),
    }
    out = _build_price_projection(comps, [d1, d2, d3])
    assert out["dates"] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert out["capital_tilt"] == [0.004, None, -0.002]
    assert len(out["leadership"]) == 3
    assert out["leadership"][1] is None


def test_ph7_unpublished_same_day_composition_never_enters_history():
    d = _d(2024, 1, 2)
    published_run = "run-A"
    later_unpublished_run = "run-B"
    rows = [
        _Row(d, later_unpublished_run, _comp(tilt=0.999)),  # unpublished -> must be dropped
        _Row(d, published_run, _comp(tilt=0.004)),  # published -> kept
    ]
    comps = _select_published_compositions(rows, {d: published_run})
    assert set(comps.keys()) == {d}
    assert comps[d]["internal_structure_facts"]["capital_tilt"]["capital_tilt"] == 0.004


def test_ph8_missing_published_composition_is_null():
    d = _d(2024, 1, 2)
    # no row at all for the published run -> date slot preserved as None
    comps = _select_published_compositions([], {d: "run-A"})
    assert comps == {}
    out = _build_price_projection(comps, [d])
    assert out["capital_tilt"] == [None]
    assert out["leadership"] == [None]


def test_ph9_capital_tilt_verbatim_not_aw_minus_ew():
    d = _d(2024, 1, 2)
    # persisted capital_tilt is 0.004 while AW - EW would be 0.006 -> must stay verbatim
    comp = _comp(tilt=0.004)
    comp["internal_structure_facts"]["capital_tilt"]["equal_weight_return"] = 0.010
    comp["internal_structure_facts"]["capital_tilt"]["amount_weighted_return"] = 0.016
    out = _build_price_projection({d: comp}, [d])
    assert out["capital_tilt"][0] == 0.004
    assert out["capital_tilt"][0] != 0.006


def test_ph10_leadership_verbatim_including_empty_vs_null_ids():
    d = _d(2024, 1, 2)
    lead = {
        "status": "unavailable",
        "reason": "CURRENT_LEADER_SET_UNAVAILABLE",
        "jaccard_stability": 0.42,
        "migration": 0.58,
        "current_leader_count": 0,
        "current_leader_ids": [],  # empty set is a REAL fact, must stay []
    }
    out = _build_price_projection({d: _comp(tilt=0.001, lead=lead)}, [d])
    got = out["leadership"][0]
    assert got is not None
    assert got["status"] == "unavailable"
    assert got["reason"] == "CURRENT_LEADER_SET_UNAVAILABLE"
    assert got["jaccard_stability"] == 0.42
    assert got["migration"] == 0.58
    assert got["current_leader_count"] == 0
    # [] must NOT collapse to None
    assert got["current_leader_ids"] == []
    assert got["current_leader_ids"] is not None

    # ids absent entirely -> null (distinct from [])
    out2 = _build_price_projection({d: _comp(tilt=None, lead={"status": "ready"})}, [d])
    got2 = out2["leadership"][0]
    assert got2 is not None
    assert got2["status"] == "ready"
    assert got2["reason"] is None
    assert got2["current_leader_ids"] is None
    assert got2["jaccard_stability"] is None
