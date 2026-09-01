"""Tests for the Auction V3.2 member historical evidence owner.

The most important cases are the ones that are easy to get wrong and expensive
to notice later:

- ``current ready != history ready``: a computable today-gap says NOTHING
  about whether the historical window is usable;
- T itself and any future observation must never enter the baseline;
- insufficient history is ``insufficient_history``, never a fake 0 / 50.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from app.domain.auction.member_history import (
    compute_member_history_evidence,
    filter_strictly_pre_t,
)
from app.domain.auction.member_observation import build_member_observation

_IID = UUID("5e26a1fa-e013-4417-aad5-0cc7fcee9735")
_T = date(2026, 8, 14)


def _obs(trade_date: date, gap: float | None, amount: float | None):
    return build_member_observation(
        instrument_id=_IID,
        trade_date=trade_date,
        # final_price/prev_close chosen so gap_ratio == gap exactly
        final_price=None if gap is None else 1.0 + gap,
        prev_close=1.0,
        amount=amount,
        quality_status="ok",
        source="historical_backfill",
    )


def _history(n: int, *, gap: float = 0.01, amount: float = 1000.0):
    """n consecutive observations strictly before T."""
    return [_obs(_T - timedelta(days=i + 1), gap + i * 1e-6, amount + i) for i in range(n)]


def _evidence(current_gap, current_amount, history, n_hist=0):
    hist = history if n_hist == 0 else _history(n_hist)
    return compute_member_history_evidence(
        instrument_id=_IID,
        trade_date=_T,
        current=_obs(_T, current_gap, current_amount),
        history=hist,
    )


# ---------------------------------------------------------------------------
# current ready  !=  history ready  (the error this module must prevent)
# ---------------------------------------------------------------------------
def test_current_gap_ready_does_not_imply_history_ready() -> None:
    """Today's gap is computable but there is no history -> not eligible."""
    ev = _evidence(0.02, 1000.0, history=[])
    assert ev.gap_percentile is None
    assert ev.gap_position_status == "insufficient_history"
    assert ev.gap_history_eligible is False


def test_current_amount_ready_does_not_imply_amount_history_ready() -> None:
    ev = _evidence(0.02, 1000.0, history=[])
    assert ev.amount_percentile is None
    assert ev.amount_position_status == "insufficient_history"
    assert ev.amount_history_eligible is False


def test_current_unavailable_is_unavailable_current_not_insufficient() -> None:
    """A missing today-value is a different status from a missing history."""
    ev = _evidence(None, None, history=_history(80))
    assert ev.gap_position_status == "unavailable_current"
    assert ev.amount_position_status == "unavailable_current"
    assert ev.gap_history_eligible is False
    assert ev.amount_history_eligible is False
    assert "GAP_CURRENT_UNAVAILABLE" in ev.reason_codes


def test_price_and_amount_history_eligibility_are_independent() -> None:
    """Gap history is long enough, amount history is not -> flags must differ."""
    hist = [
        _obs(_T - timedelta(days=i + 1), gap=0.01 + i * 1e-6, amount=None)
        for i in range(80)
    ]
    ev = _evidence(0.02, 1000.0, history=hist)
    assert ev.gap_history_eligible is True
    assert ev.amount_history_eligible is False


# ---------------------------------------------------------------------------
# no future leakage / T excluded
# ---------------------------------------------------------------------------
def test_t_and_future_observations_are_dropped() -> None:
    hist = _history(80)
    polluted = hist + [
        _obs(_T, 999.0, 999999.0),  # T itself
        _obs(_T + timedelta(days=1), 999.0, 999999.0),  # future
    ]
    ev = _evidence(0.02, 1000.0, history=polluted)
    assert ev.dropped_future_or_same_day == 2
    assert "FUTURE_OR_SAME_DAY_OBSERVATION_DROPPED" in ev.reason_codes
    # candidate_count reflects only the strictly-pre-T window
    assert ev.gap_candidate_count == 80


def test_filter_strictly_pre_t_sorts_and_counts() -> None:
    kept, dropped = filter_strictly_pre_t(
        [_obs(_T, 1.0, 1.0), _obs(_T - timedelta(days=2), 1.0, 1.0), _obs(_T - timedelta(days=1), 1.0, 1.0)],
        _T,
    )
    assert dropped == 1
    assert [o.trade_date for o in kept] == [_T - timedelta(days=2), _T - timedelta(days=1)]


# ---------------------------------------------------------------------------
# window cap and minimum-valid gate
# ---------------------------------------------------------------------------
def test_window_is_capped_at_120_candidates() -> None:
    ev = _evidence(0.02, 1000.0, history=_history(300))
    assert ev.gap_candidate_count == 120


def test_below_minimum_valid_history_is_insufficient() -> None:
    ev = _evidence(0.02, 1000.0, history=_history(59))
    assert ev.gap_valid_count == 59
    assert ev.gap_history_eligible is False


def test_at_minimum_valid_history_is_ready() -> None:
    ev = _evidence(0.02, 1000.0, history=_history(60))
    assert ev.gap_valid_count == 60
    assert ev.gap_history_eligible is True
    assert ev.gap_percentile is not None


def test_insufficient_history_never_fabricates_a_position() -> None:
    """No fake 0.0 / 50.0 percentile when the window is too short."""
    ev = _evidence(0.02, 1000.0, history=_history(10))
    assert ev.gap_percentile is None  # not 0.0, not 50.0


# ---------------------------------------------------------------------------
# missing slots inside the window are skipped, not treated as zero
# ---------------------------------------------------------------------------
def test_missing_slots_reduce_valid_count_not_filled_with_zero() -> None:
    hist = _history(80)
    for i in range(0, 80, 2):
        hist[i] = _obs(hist[i].trade_date, None, None)
    ev = _evidence(0.02, 1000.0, history=hist)
    assert ev.gap_candidate_count == 80
    assert ev.gap_valid_count == 40  # missing slots are NOT valid observations
    assert ev.gap_history_eligible is False


# ---------------------------------------------------------------------------
# percentile direction sanity
# ---------------------------------------------------------------------------
def test_percentile_reflects_rank_within_own_history() -> None:
    hist = _history(80)
    low = _evidence(-1.0, 1000.0, history=hist)
    high = _evidence(1.0, 1000.0, history=hist)
    assert low.gap_percentile is not None and high.gap_percentile is not None
    assert low.gap_percentile < high.gap_percentile
    assert 0.0 <= low.gap_percentile <= 100.0
    assert 0.0 <= high.gap_percentile <= 100.0


def test_evidence_is_deterministic() -> None:
    a = _evidence(0.02, 1000.0, history=_history(80))
    b = _evidence(0.02, 1000.0, history=_history(80))
    assert a == b
