"""Tests for the Auction V3.2 unified Member Auction Observation.

Covers the frozen gap contract, Missing != Zero, the independent
price/amount readiness flags, and — most importantly — that the historical
lane and the live lane collapse into the SAME business shape, so that every
downstream owner stops caring about provenance.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from app.domain.auction.member_observation import (
    HISTORICAL_SOURCE,
    build_member_observation,
    compute_gap_ratio,
    observation_from_quote_row,
)

_T = date(2026, 8, 14)
_IID = UUID("5e26a1fa-e013-4417-aad5-0cc7fcee9735")


def _obs(**kw):
    base = {
        "instrument_id": _IID,
        "trade_date": _T,
        "final_price": 10.23,
        "prev_close": 10.00,
        "amount": 1000.0,
        "quality_status": "ok",
        "reason_codes": [],
        "source": "mootdx",
    }
    base.update(kw)
    return build_member_observation(**base)


# ---------------------------------------------------------------------------
# gap ratio exact semantics
# ---------------------------------------------------------------------------
def test_gap_ratio_is_ratio_not_percent() -> None:
    # +2.30% MUST be 0.023, never 2.3
    assert compute_gap_ratio(10.23, 10.00) == pytest.approx(0.023)
    assert compute_gap_ratio(10.23, 10.00) != pytest.approx(2.3)


def test_gap_ratio_negative_and_zero() -> None:
    assert compute_gap_ratio(9.77, 10.00) == pytest.approx(-0.023)
    assert compute_gap_ratio(10.00, 10.00) == pytest.approx(0.0)


@pytest.mark.parametrize(
    "price,prev",
    [
        (None, 10.0),
        (10.0, None),
        (None, None),
        (float("nan"), 10.0),
        (10.0, float("nan")),
        (float("inf"), 10.0),
        (10.0, 0.0),  # zero base has no defined ratio
        (10.0, -1.0),  # negative base
    ],
)
def test_gap_ratio_unavailable_cases(price: float | None, prev: float | None) -> None:
    # Missing != Zero: every unusable input yields None, never 0.0
    assert compute_gap_ratio(price, prev) is None


def test_observation_gap_ratio_none_is_not_zero() -> None:
    o = _obs(final_price=None)
    assert o.gap_ratio is None
    assert o.price_ready is False
    assert not o.gap_ratio == 0


# ---------------------------------------------------------------------------
# Missing != Zero for amount
# ---------------------------------------------------------------------------
def test_zero_amount_is_still_ready() -> None:
    """A legitimate zero amount is a fact; only missing/negative is not."""
    o = _obs(amount=0.0)
    assert o.amount_ready is True
    assert o.auction_amount == pytest.approx(0.0)


def test_missing_amount_unavailable_with_reason() -> None:
    o = _obs(amount=None)
    assert o.amount_ready is False
    assert "AUCTION_AMOUNT_UNAVAILABLE" in o.reason_codes


def test_negative_amount_unavailable() -> None:
    o = _obs(amount=-5.0)
    assert o.amount_ready is False
    assert "AUCTION_AMOUNT_NEGATIVE" in o.reason_codes


def test_price_and_amount_readiness_are_independent() -> None:
    """No single global valid_count: the two flags must be able to differ."""
    price_ok_amount_missing = _obs(amount=None)
    assert price_ok_amount_missing.price_ready is True
    assert price_ok_amount_missing.amount_ready is False

    price_missing_amount_ok = _obs(final_price=10.23, prev_close=0.0)
    assert price_missing_amount_ok.price_ready is False
    assert price_missing_amount_ok.amount_ready is True


# ---------------------------------------------------------------------------
# quality gating
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status",
    ["suspended", "zero_volume", "missing_field", "api_error", "limit_up", "limit_down", None],
)
def test_unusable_quality_blocks_everything(status: str | None) -> None:
    o = _obs(quality_status=status)
    assert o.price_ready is False
    assert o.amount_ready is False
    assert o.gap_ratio is None
    assert o.auction_amount is None
    assert "QUALITY_STATUS_NOT_USABLE" in o.reason_codes


def test_reason_codes_are_deduplicated_preserving_order() -> None:
    o = _obs(final_price=None, prev_close=None, amount=None, reason_codes=["X", "X", "Y"])
    assert list(o.reason_codes).count("X") == 1
    assert o.reason_codes[0] == "X"


# ---------------------------------------------------------------------------
# historical vs live -> identical business shape
# ---------------------------------------------------------------------------
def _row(source: str) -> dict:
    return {
        "instrument_id": _IID,
        "trade_date": _T,
        "final_price": 9.14,
        "prev_close": 9.18,
        "amount": 4665970.0,
        "quality_status": "ok",
        "reason_codes": [],
        "source": source,
    }


def test_historical_and_live_rows_produce_identical_facts() -> None:
    hist = observation_from_quote_row(_row(HISTORICAL_SOURCE))
    live = observation_from_quote_row(_row("mootdx"))

    assert hist.gap_ratio == live.gap_ratio
    assert hist.auction_amount == live.auction_amount
    assert hist.price_ready == live.price_ready
    assert hist.amount_ready == live.amount_ready
    assert hist.reason_codes == live.reason_codes
    # only the provenance label differs, and it is carried for diagnostics
    assert hist.source == HISTORICAL_SOURCE
    assert live.source == "mootdx"


def test_real_historical_sample_row_matches_known_gap() -> None:
    """Real values from member_facts.jsonl (600519 / 2026-08-14)."""
    o = observation_from_quote_row(_row(HISTORICAL_SOURCE))
    assert o.gap_ratio is not None
    assert o.gap_ratio == pytest.approx(9.14 / 9.18 - 1.0)
    assert o.auction_amount == pytest.approx(4665970.0)


def test_observation_carries_no_board_or_score_semantics() -> None:
    """The member observation must stay free of board/position/score fields."""
    o = _obs()
    for forbidden in ("scope_id", "board", "industry", "concept", "position", "score"):
        assert not hasattr(o, forbidden)
