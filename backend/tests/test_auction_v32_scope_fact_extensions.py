"""Tests for the AUCTION-V3.2 §4 extensions to the existing Auction L1 owner.

These tests deliberately do NOT create a second L1 owner: they only exercise
``compute_auction_l1_scope_facts`` (the single canonical owner) and, where a
cross-check is needed, compare against the SHARED math primitives — never
against a locally re-implemented formula.

Covered (§4.1 – §4.4):
- Capital Tilt = AW - EW (None in, None out)
- Unchanged Breadth (positive + negative + unchanged == denominator identity)
- Gap Dispersion (shared population-stdev semantics, n < 2 -> None)
- Price HHI (shared abs-value concentration; zero-gap counts toward N)
"""

from __future__ import annotations

import math

import pytest

from app.domain.auction.member_fact import (
    AuctionMemberFact,
    AuctionMemberFactConfig,
)
from app.domain.auction.scope_fact import compute_auction_l1_scope_facts
from app.domain.shared.concentration import abs_value_concentration
from app.domain.shared.stdev import population_stdev

_NAN = float("nan")

CFG = AuctionMemberFactConfig(
    positive_gap_percentile_threshold=90.0,
    negative_gap_percentile_threshold=10.0,
    volume_abnormal_percentile_threshold=90.0,
    amount_abnormal_percentile_threshold=90.0,
)


def _mk(
    instrument_id: str,
    gap_pct=_NAN,
    auction_amount=_NAN,
    gap_percentile=_NAN,
    amount_percentile=_NAN,
    current_gap_eligible=False,
    gap_history_eligible=False,
    current_amount_eligible=False,
    amount_history_eligible=False,
) -> AuctionMemberFact:
    return AuctionMemberFact(
        instrument_id=instrument_id,
        gap_pct=gap_pct,
        auction_volume=_NAN,
        auction_amount=auction_amount,
        gap_percentile=gap_percentile,
        volume_percentile=_NAN,
        amount_percentile=amount_percentile,
        current_gap_eligible=current_gap_eligible,
        gap_history_eligible=gap_history_eligible,
        current_volume_eligible=False,
        volume_history_eligible=False,
        current_amount_eligible=current_amount_eligible,
        amount_history_eligible=amount_history_eligible,
    )


def _run(members: list[AuctionMemberFact], member_indices: list[int]):
    scopes = [
        {"scope_id": "s0", "scope_family": "market", "member_indices": []},
        {
            "scope_id": "s1",
            "scope_family": "industry",
            "member_indices": member_indices,
        },
    ]
    res = compute_auction_l1_scope_facts(members, scopes, CFG)
    return res[1]


def _gap_member(mid: str, gap: float) -> AuctionMemberFact:
    return _mk(mid, gap_pct=gap, current_gap_eligible=True)


# ---------------------------------------------------------------------------
# §4.1 Capital Tilt
# ---------------------------------------------------------------------------
def test_capital_tilt_equals_aw_minus_ew() -> None:
    members = [
        _mk("A", gap_pct=2.0, auction_amount=100.0, current_gap_eligible=True,
            current_amount_eligible=True),
        _mk("B", gap_pct=-1.0, auction_amount=50.0, current_gap_eligible=True,
            current_amount_eligible=True),
    ]
    r = _run(members, [0, 1])
    # EW = (2 + -1) / 2 = 0.5 ; AW = (2*100 + -1*50) / 150 = 1.0
    assert r.equal_weight_gap == pytest.approx(0.5)
    assert r.amount_weighted_gap == pytest.approx(1.0)
    assert r.capital_tilt == pytest.approx(0.5)


def test_capital_tilt_is_derived_not_a_second_owner() -> None:
    """Tilt must equal AW - EW exactly, proving it consumes, not recomputes."""
    members = [
        _mk("A", gap_pct=3.0, auction_amount=10.0, current_gap_eligible=True,
            current_amount_eligible=True),
        _mk("B", gap_pct=-2.0, auction_amount=90.0, current_gap_eligible=True,
            current_amount_eligible=True),
    ]
    r = _run(members, [0, 1])
    assert r.capital_tilt == pytest.approx(
        r.amount_weighted_gap - r.equal_weight_gap
    )


def test_capital_tilt_none_when_ew_unavailable() -> None:
    # no current-gap-eligible member -> EW is None -> tilt is None, never 0
    members = [_mk("A", gap_pct=_NAN, auction_amount=100.0,
                   current_gap_eligible=False, current_amount_eligible=True)]
    r = _run(members, [0])
    assert r.equal_weight_gap is None
    assert r.capital_tilt is None


def test_capital_tilt_none_when_aw_unavailable() -> None:
    # zero amount total -> AW is None -> tilt is None, never 0
    members = [_mk("A", gap_pct=1.0, auction_amount=0.0, current_gap_eligible=True,
                   current_amount_eligible=True)]
    r = _run(members, [0])
    assert r.amount_weighted_gap is None
    assert r.equal_weight_gap == pytest.approx(1.0)
    assert r.capital_tilt is None


# ---------------------------------------------------------------------------
# §4.2 Unchanged Breadth
# ---------------------------------------------------------------------------
def test_unchanged_breadth_identity_holds() -> None:
    members = [_gap_member("A", 1.0), _gap_member("B", -1.0), _gap_member("C", 0.0)]
    r = _run(members, [0, 1, 2])
    assert r.positive_gap_breadth == pytest.approx(1 / 3)
    assert r.negative_gap_breadth == pytest.approx(1 / 3)
    assert r.unchanged_gap_breadth == pytest.approx(1 / 3)
    # machine-checkable identity
    assert (
        r.positive_gap_breadth_num
        + r.negative_gap_breadth_num
        + r.unchanged_gap_breadth_num
    ) == r.equal_weight_gap_den


def test_unchanged_breadth_uses_same_denominator_as_current_gap() -> None:
    members = [_gap_member("A", 1.0), _gap_member("B", -1.0), _gap_member("C", 0.0)]
    r = _run(members, [0, 1, 2])
    assert r.unchanged_gap_breadth_den == r.equal_weight_gap_den
    assert r.unchanged_gap_breadth_den == r.positive_gap_breadth_den


def test_unchanged_breadth_all_flat() -> None:
    members = [_gap_member("A", 0.0), _gap_member("B", 0.0)]
    r = _run(members, [0, 1])
    assert r.unchanged_gap_breadth == pytest.approx(1.0)
    assert r.positive_gap_breadth == pytest.approx(0.0)
    assert r.negative_gap_breadth == pytest.approx(0.0)


def test_unchanged_breadth_excludes_ineligible_members() -> None:
    # D is current-gap-ineligible -> must not enter the denominator
    members = [
        _gap_member("A", 1.0),
        _gap_member("B", -1.0),
        _mk("D", gap_pct=_NAN, current_gap_eligible=False),
    ]
    r = _run(members, [0, 1, 2])
    assert r.equal_weight_gap_den == 2
    assert r.unchanged_gap_breadth_den == 2
    assert r.unchanged_gap_breadth == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# §4.3 Gap Dispersion
# ---------------------------------------------------------------------------
def test_gap_dispersion_matches_shared_population_stdev() -> None:
    gaps = [1.0, -1.0, 0.0]
    r = _run([_gap_member(m, g) for m, g in zip("ABC", gaps, strict=False)], [0, 1, 2])
    assert r.gap_dispersion == pytest.approx(math.sqrt(2.0 / 3.0))
    assert r.gap_dispersion == pytest.approx(population_stdev(gaps))


def test_gap_dispersion_identical_values_is_zero() -> None:
    r = _run([_gap_member("A", 2.0), _gap_member("B", 2.0)], [0, 1])
    assert r.gap_dispersion == pytest.approx(0.0)


def test_gap_dispersion_single_member_is_none() -> None:
    # n < 2 -> unavailable, never 0
    r = _run([_gap_member("A", 2.0)], [0])
    assert r.gap_dispersion is None


def test_gap_dispersion_excludes_ineligible_members() -> None:
    members = [
        _gap_member("A", 1.0),
        _gap_member("B", 3.0),
        _mk("C", gap_pct=999.0, current_gap_eligible=False),
    ]
    r = _run(members, [0, 1, 2])
    assert r.gap_dispersion_den == 2
    assert r.gap_dispersion == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# §4.4 Price HHI
# ---------------------------------------------------------------------------
def test_price_hhi_abs_share_semantics() -> None:
    # abs = [1, 1, 0] -> total 2 -> shares [0.5, 0.5, 0]
    # raw = 0.5 ; N = 3 -> floor 1/3, denom 2/3 -> norm = 0.25
    members = [_gap_member("A", 1.0), _gap_member("B", -1.0), _gap_member("C", 0.0)]
    r = _run(members, [0, 1, 2])
    assert r.price_raw_hhi == pytest.approx(0.5)
    assert r.price_normalized_hhi == pytest.approx(0.25)
    assert r.price_concentration_status == "ready"


def test_price_hhi_zero_gap_member_counts_toward_n() -> None:
    """A zero-gap member is still a concentration universe member (Review contract)."""
    members = [_gap_member("A", 1.0), _gap_member("B", -1.0), _gap_member("C", 0.0)]
    r = _run(members, [0, 1, 2])
    assert r.price_concentration_member_count == 3


def test_price_hhi_matches_shared_primitive_exactly() -> None:
    gaps = [2.0, -1.5, 0.0, 0.75]
    r = _run([_gap_member(m, g) for m, g in zip("ABCD", gaps, strict=False)], [0, 1, 2, 3])
    expected = abs_value_concentration(gaps)
    assert r.price_raw_hhi == pytest.approx(expected["raw_hhi"])
    assert r.price_normalized_hhi == pytest.approx(expected["normalized_hhi"])
    assert r.price_concentration_member_count == expected["member_count"]
    assert r.price_concentration_status == expected["status"]


def test_price_hhi_all_zero_gaps_unavailable() -> None:
    members = [_gap_member("A", 0.0), _gap_member("B", 0.0)]
    r = _run(members, [0, 1])
    assert r.price_raw_hhi is None
    assert r.price_normalized_hhi is None
    assert r.price_concentration_status == "zero_abs_return"


def test_price_hhi_single_member_normalized_is_none() -> None:
    r = _run([_gap_member("A", 2.0)], [0])
    assert r.price_raw_hhi == pytest.approx(1.0)
    assert r.price_normalized_hhi is None
    assert r.price_concentration_status == "insufficient_member_count"


def test_price_hhi_dominant_single_direction() -> None:
    # abs = [9, 1] -> shares [0.9, 0.1] -> raw 0.82 -> norm (0.82-0.5)/0.5 = 0.64
    members = [_gap_member("A", 9.0), _gap_member("B", -1.0)]
    r = _run(members, [0, 1])
    assert r.price_raw_hhi == pytest.approx(0.82)
    assert r.price_normalized_hhi == pytest.approx(0.64)
