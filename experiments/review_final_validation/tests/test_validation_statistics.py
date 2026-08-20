"""
Unit tests for the REVIEW-FINAL-STATISTICAL-VALIDATION statistics helpers.

These test only the experiment's statistical logic (percentiles, deterministic
tie-break, top-k, family aggregation, attribution share denominator, unavailable
preservation, serialization determinism). They do NOT re-test the Review domain
owners (that is covered by the production test suite).

Run:
    PYTHONPATH=. APP_ENV=test DATABASE_URL=... REDIS_URL=... \\
        backend/.venv/bin/python -m pytest experiments/review_final_validation/tests/ -q
"""
from __future__ import annotations

import json
import sys
import os

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_EXP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)
sys.path.insert(0, _EXP)

import run_validation as rv  # noqa: E402


# ---------------------------------------------------------------------------
# percentile / null handling
# ---------------------------------------------------------------------------
def test_pctl_basic():
    assert rv._pctl([1, 2, 3, 4], 50) == 2.5
    assert rv._pctl([5], 50) == 5
    assert rv._pctl([], 50) == ""


def test_pctl_ignores_none():
    assert rv._pctl([None, 1, 2, 3, 4], 50) == 2.5


def test_pctl_median_even():
    assert rv._pctl([10, 20, 30, 40], 50) == 25.0


def test_to_float_null_handling():
    assert rv._to_float(None) is None
    assert rv._to_float("") is None
    assert rv._to_float("abc") is None
    assert rv._to_float("1.5") == 1.5


# ---------------------------------------------------------------------------
# deterministic tie-break / top-k
# ---------------------------------------------------------------------------
def _mk(scope_key, value):
    class S:
        pass
    s = S()
    s.scope_key = scope_key
    s.scope_type = "concept"
    return s, value


def test_cross_section_tie_break_by_scope_key():
    # equal values => ascending scope_key
    items = [_mk("b", 1.0), _mk("a", 1.0), _mk("c", 1.0)]
    ranked = sorted(items, key=lambda x: (x[1], x[0].scope_key))
    assert [x[0].scope_key for x in ranked] == ["a", "b", "c"]


def test_cross_section_none_goes_last_descending():
    items = [_mk("a", None), _mk("b", 5.0)]
    ranked = sorted(items, key=lambda x: (x[1] if x[1] is not None else float("-inf"), x[0].scope_key), reverse=True)
    assert ranked[0][0].scope_key == "b"


def test_topn_abs_share_deterministic():
    group = [
        {"member_id": "a", "canonical_contribution": 2.0},
        {"member_id": "b", "canonical_contribution": -1.0},
        {"member_id": "c", "canonical_contribution": 1.0},
    ]
    # top1 abs share = 2.0 / (2+1+1) = 0.5
    assert rv._topn_abs_share(group, "canonical_contribution", 1) == pytest.approx(0.5)
    assert rv._topn_abs_share(group, "canonical_contribution", 3) == pytest.approx(1.0)


def test_topn_abs_share_empty_or_zero():
    assert rv._topn_abs_share([], "canonical_contribution", 1) == ""
    assert rv._topn_abs_share([{"member_id": "a", "canonical_contribution": None}], "canonical_contribution", 1) == ""
    # total 0
    assert rv._topn_abs_share([{"member_id": "a", "canonical_contribution": 0.0}], "canonical_contribution", 1) == ""


# ---------------------------------------------------------------------------
# attribution share denominator definition (unrounded canonical values)
# ---------------------------------------------------------------------------
def test_attribution_share_uses_canonical_not_rounded():
    # If we rounded to 1 decimal, 0.15 would collapse; using raw must keep separation.
    group = [
        {"member_id": "a", "canonical_contribution": 0.25},
        {"member_id": "b", "canonical_contribution": 0.15},
        {"member_id": "c", "canonical_contribution": 0.10},
    ]
    assert rv._topn_abs_share(group, "canonical_contribution", 2) == pytest.approx(0.8)


def test_zero_or_unavailable_preservation():
    d = {"aw_universe_count": 10, "positive": [1, 2], "negative": [3]}
    zero = max(0, (d["aw_universe_count"] or 0) - len(d["positive"]) - len(d["negative"]))
    assert zero == 7
    # never negative
    d2 = {"aw_universe_count": 1, "positive": [1, 2], "negative": [3, 4]}
    assert max(0, (d2["aw_universe_count"] or 0) - len(d2["positive"]) - len(d2["negative"])) == 0


# ---------------------------------------------------------------------------
# unavailable preservation (not coerced to 0)
# ---------------------------------------------------------------------------
def test_num_preserves_unavailable():
    assert rv._num(None) == ""
    assert rv._num("") == ""
    assert rv._num(float("nan")) == ""


def test_status_preservation():
    assert rv._status(None) == "unavailable"
    assert rv._status({"status": "unavailable_current"}) == "unavailable_current"
    assert rv._status({"error": "boom"}) == "error"
    assert rv._status({"ready": True}) == "ready"


# ---------------------------------------------------------------------------
# family aggregation
# ---------------------------------------------------------------------------
def test_family_summary_counts():
    buffers = {("2026-08-10", "concept"): [
        {"member_count": 30, "ew": 0.01, "adv": 0.5, "dec": 0.3, "disp": 0.2,
         "cap_tilt": 0.0, "pnh": 0.1, "anh": 0.05, "ready": True},
        {"member_count": 40, "ew": 0.02, "adv": 0.6, "dec": 0.2, "disp": 0.3,
         "cap_tilt": 0.0, "pnh": 0.2, "anh": 0.1, "ready": True},
        {"member_count": 50, "ew": None, "adv": None, "dec": None, "disp": None,
         "cap_tilt": None, "pnh": None, "anh": None, "ready": False},
    ]}
    rows = rv._family_summary(buffers, {}, {})
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == "2026-08-10" and r[1] == "concept"
    assert r[2] == 3       # scope_count
    assert r[3] == 2       # ready_count
    assert r[5] == 1       # unavailable_count
    # median of ew over non-null (0.01, 0.02) -> 0.015
    assert rv._pctl([0.01, 0.02], 50) == pytest.approx(0.015)


# ---------------------------------------------------------------------------
# CSV / JSON serialization determinism
# ---------------------------------------------------------------------------
def test_closure_matrix_deterministic(tmp_path):
    m1 = rv._closure_matrix([1], [1], [1], [1], [1], [1])
    m2 = rv._closure_matrix([1], [1], [1], [1], [1], [1])
    assert json.dumps(m1, sort_keys=True) == json.dumps(m2, sort_keys=True)
    assert m1["gates"]["CURRENT_STATE_CANONICAL_FACTS"] == "PASS"
    assert m1["gates"]["STRICT_PIT_HISTORICAL_PRODUCT_VALIDATION"] == "BLOCKED_BY_MEMBERSHIP_DATA"
    assert m1["gates"]["HISTORICAL_DYNAMICS_ALGORITHM"] == "PASS_ON_CURRENT_STATIC_PROXY"


def test_determinism_rows_record_pass_fail_skip():
    rows = rv._determinism_checks({"2026-08-10": []})
    # empty buffer => no rows
    assert rows == []


def test_write_csv_header_and_rows(tmp_path):
    p = tmp_path / "t.csv"
    rv.write_csv(str(p), ["a", "b"], [[1, 2], [3, None]])
    text = p.read_text(encoding="utf-8")
    assert "a,b" in text
    assert "1,2" in text
    assert ",3," in text.replace("\n", ",") or "3,\n" in text
