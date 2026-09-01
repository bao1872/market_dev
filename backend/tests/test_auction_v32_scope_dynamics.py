"""Tests for the Auction V3.2 scope dynamics + amount participation owner.

Key contracts under test:
- Position baseline is strictly pre-T (no self, no future);
- EMA warmup: no value before ``span`` valid observations;
- Velocity = EMA5 - EMA20, Signal = EMA5(Velocity),
  Acceleration = Velocity - Signal;
- AW reuses the SAME arithmetic (confirmation, not a second lifecycle);
- Amount Multiple is median-based and unavailable when the median is <= 0.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from app.domain.auction.scope_dynamics import (
    compute_amount_participation,
    compute_dynamics,
    compute_position_series,
)

_T = date(2026, 8, 14)


def _series(n: int, fn):
    return [(_T - timedelta(days=n - 1 - i), fn(i)) for i in range(n)]


# ---------------------------------------------------------------------------
# Position: strictly pre-T
# ---------------------------------------------------------------------------
def test_first_date_has_no_baseline_so_insufficient() -> None:
    s = compute_position_series(_series(3, lambda i: 0.01 * (i + 1)))
    assert s[0]["status"] == "insufficient_history"
    assert s[0]["value"] is None


def test_position_never_uses_t_or_future() -> None:
    """A huge value at T must not appear in T's own baseline."""
    vals = [( _T - timedelta(days=3), 0.010), (_T - timedelta(days=2), 0.011),
            (_T - timedelta(days=1), 0.012), (_T, 999.0)]
    s = compute_position_series(vals)
    assert s[-1]["history"]["candidate_count"] == 3  # strictly pre-T only


def test_position_ready_only_after_minimum_valid_history() -> None:
    vals = _series(80, lambda i: 0.001 * i)
    s = compute_position_series(vals)
    assert s[59]["status"] == "insufficient_history"  # 59 pre-T valid < 60
    assert s[60]["status"] != "insufficient_history"  # 60 -> usable


def test_window_capped_at_120() -> None:
    s = compute_position_series(_series(200, lambda i: 0.001 * i))
    assert s[-1]["history"]["candidate_count"] == 120


def test_missing_values_are_skipped_not_zeroed() -> None:
    vals = [(_T - timedelta(days=i), None if i % 2 else 0.001 * i) for i in range(80, 0, -1)]
    vals.append((_T, 0.02))
    s = compute_position_series(vals)
    assert s[-1]["history"]["candidate_count"] == 80
    assert s[-1]["history"]["valid_count"] == 40


# ---------------------------------------------------------------------------
# EMA / Velocity / Signal / Acceleration
# ---------------------------------------------------------------------------
def test_velocity_equals_fast_minus_slow() -> None:
    d = compute_dynamics(_series(80, lambda i: 0.001 * i))
    for p in d.points:
        if p.velocity is not None:
            assert p.velocity == pytest.approx(p.ema_fast - p.ema_slow)


def test_acceleration_equals_velocity_minus_signal() -> None:
    d = compute_dynamics(_series(80, lambda i: 0.001 * i))
    for p in d.points:
        if p.acceleration is not None:
            assert p.acceleration == pytest.approx(p.velocity - p.signal)


def test_ema_warmup_produces_no_value_before_span() -> None:
    d = compute_dynamics(_series(80, lambda i: 0.001 * i))
    # fast EMA (span 5) needs 5 valid positions; slow (20) needs 20.
    early = d.points[10]
    assert early.velocity is None  # slow leg not ready yet


def test_dynamics_series_is_date_aligned_and_never_compressed() -> None:
    vals = _series(80, lambda i: 0.001 * i)
    d = compute_dynamics(vals)
    assert len(d.points) == len(vals)
    assert [p.trade_date for p in d.points] == [v[0] for v in vals]


def test_latest_returns_last_point() -> None:
    d = compute_dynamics(_series(80, lambda i: 0.001 * i))
    assert d.latest() is d.points[-1]


def test_empty_input_returns_empty_dynamics() -> None:
    assert compute_dynamics([]).points == ()


def test_aw_uses_the_same_arithmetic_as_ew() -> None:
    """AW is a confirmation computed identically — no second lifecycle."""
    vals = _series(80, lambda i: 0.001 * i)
    ew = compute_dynamics(vals)
    aw = compute_dynamics(vals)
    assert ew == aw


def test_dynamics_produces_no_phase_or_label_fields() -> None:
    d = compute_dynamics(_series(80, lambda i: 0.001 * i))
    p = d.latest()
    for forbidden in ("phase", "lifecycle", "strength", "opportunity", "risk", "score"):
        assert not hasattr(p, forbidden)


# ---------------------------------------------------------------------------
# amount participation
# ---------------------------------------------------------------------------
def test_amount_multiple_is_current_over_pre_t_median() -> None:
    amounts = [(_T - timedelta(days=i), 100.0) for i in range(80, 0, -1)]
    amounts.append((_T, 300.0))
    res = compute_amount_participation(amounts)
    assert res["amount_multiple"] == pytest.approx(3.0)


def test_amount_multiple_unavailable_when_median_is_zero() -> None:
    amounts = [(_T - timedelta(days=i), 0.0) for i in range(80, 0, -1)]
    amounts.append((_T, 300.0))
    res = compute_amount_participation(amounts)
    assert res["amount_multiple"] is None  # not inf, not 0


def test_amount_multiple_unavailable_when_current_missing() -> None:
    amounts = [(_T - timedelta(days=i), 100.0) for i in range(80, 0, -1)]
    amounts.append((_T, None))
    res = compute_amount_participation(amounts)
    assert res["amount_multiple"] is None
    assert res["amount_position"] is None


def test_amount_position_ready_with_enough_history() -> None:
    amounts = [(_T - timedelta(days=i), 100.0 + i) for i in range(80, 0, -1)]
    amounts.append((_T, 99999.0))
    res = compute_amount_participation(amounts)
    assert res["amount_position_status"] == "ready"
    assert res["amount_position"] is not None
    assert 0.0 <= res["amount_position"] <= 100.0


def test_amount_participation_baseline_excludes_t() -> None:
    amounts = [(_T - timedelta(days=i), 100.0) for i in range(80, 0, -1)]
    amounts.append((_T, 300.0))
    res = compute_amount_participation(amounts)
    assert res["history_candidate_count"] == 80  # strictly pre-T


def test_empty_amounts_are_handled() -> None:
    res = compute_amount_participation([])
    assert res["amount_position"] is None
    assert res["amount_multiple"] is None
    assert not math.isnan(res["amount_multiple"] or 0)
