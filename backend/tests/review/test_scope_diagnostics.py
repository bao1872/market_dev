"""Targeted unit tests for Review scope diagnostics (R3 History / Cross-sectional).

Pure, no DB, no PG. Covers the contract-critical logic:
- 20D rolling math (lagged baseline, null != 0, std==0 -> None);
- published-run lineage selection (_select_published_facts);
- field rolling alignment (_compute_field_rolling).

DB-backed end-to-end history run-selection (published run resolution across a
date window) is a targeted-PG claim and is NOT RUN here (see task report).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.domain.review.analysis.observation_stats import (
    empirical_percentile,
    safe_mean,
    safe_std,
    zscore,
)
from app.services.review_scope_diagnostics_service import (
    _compute_field_rolling,
    _select_published_facts,
)


@dataclass
class _StubFact:
    trade_date: date
    review_run_id: Any
    observation_payload: Any


# ---------------------------------------------------------------------------
# observation_stats
# ---------------------------------------------------------------------------


def test_safe_mean_excludes_none_and_never_coerces_zero():
    assert safe_mean([1.0, 2.0, None, 3.0]) == 2.0
    assert safe_mean([None, None]) is None
    # null is not treated as 0
    assert safe_mean([None, 10.0]) == 10.0


def test_safe_std_requires_two_finite():
    assert safe_std([5.0]) is None
    assert safe_std([1.0, 3.0]) == 1.0  # population std of [1,3]


def test_zscore_std_zero_is_none_not_zero():
    # baseline std == 0 must yield None, never a fake z = 0
    assert zscore(5.0, 5.0, 0.0) is None
    assert zscore(5.0, 5.0, None) is None
    assert zscore(None, 5.0, 1.0) is None
    assert zscore(7.0, 5.0, 1.0) == 2.0


def test_empirical_percentile_self_inclusive():
    # value == median of [1,2,3,4,5] -> rank (2 + 0.5*1)/5*100 = 50
    assert empirical_percentile(3.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == 50.0
    assert empirical_percentile(None, [1.0, 2.0]) is None


# ---------------------------------------------------------------------------
# lineage selection
# ---------------------------------------------------------------------------


def test_select_published_facts_drops_non_published_run():
    d = date(2026, 3, 1)
    pub = "run-pub"
    other = "run-other"
    rows = [
        _StubFact(d, pub, {"price": {"equal_weight_return": 0.01}}),
        _StubFact(d, other, {"price": {"equal_weight_return": 0.99}}),  # later same-day run
    ]
    out = _select_published_facts(rows, {d: pub})
    assert list(out.keys()) == [d]
    assert out[d]["price"]["equal_weight_return"] == 0.01  # published only


def test_select_published_facts_no_pointer_yields_empty():
    d = date(2026, 3, 1)
    rows = [_StubFact(d, "run-x", {"price": {}})]
    assert _select_published_facts(rows, {d: None}) == {}


# ---------------------------------------------------------------------------
# field rolling (lagged baseline)
# ---------------------------------------------------------------------------


def test_compute_field_rolling_lagged_and_aligned():
    # series length 5; baseline(i) = values strictly before i
    series = [1.0, 2.0, 3.0, 4.0, 5.0]
    r = _compute_field_rolling(series, window=20)
    # first point: no baseline -> mean/std/z/pct all None, baselineCount 0
    assert r["mean20"][0] is None
    assert r["zscore20"][0] is None
    assert r["baselineCount"][0] == 0
    # point i=4: baseline = [1,2,3,4] mean=2.5 std=population(1.25)=~1.118
    # value 5 -> z = (5-2.5)/1.1180... ~ 2.236
    assert r["mean20"][4] == 2.5
    assert abs(r["zscore20"][4] - 2.23606797749979) < 1e-9
    assert r["baselineCount"][4] == 4
    # all arrays aligned to series length
    assert len(r["zscore20"]) == 5


def test_compute_field_rolling_missing_values_excluded_from_baseline():
    series = [1.0, None, 3.0, None, 5.0]
    r = _compute_field_rolling(series, window=20)
    # i=4 baseline = finite values before i = [1.0, 3.0] (None excluded, not 0)
    assert r["baselineCount"][4] == 2
    assert r["mean20"][4] == 2.0
    assert r["zscore20"][4] == (5.0 - 2.0) / 1.0
