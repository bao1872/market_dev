"""Tests for the V3.2 historical Scope series builder.

Focus: PIT membership really changes the historical series (no back-fill),
industry and concept stay isolated, and the series is produced by the SAME
single calculator the current-day path uses.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest

from app.domain.auction.member_fact import AuctionMemberFactConfig
from app.domain.auction.member_observation import build_member_observation
from app.domain.auction.membership_pit import (
    FAMILY_CONCEPT,
    FAMILY_INDUSTRY,
    MembershipEdge,
)
from app.domain.auction.scope_fact import compute_auction_l1_scope_facts
from app.domain.auction.scope_history import build_scope_history_series

_T = date(2026, 8, 14)
_A, _B, _C = uuid4(), uuid4(), uuid4()

CFG = AuctionMemberFactConfig(
    positive_gap_percentile_threshold=90.0,
    negative_gap_percentile_threshold=10.0,
    volume_abnormal_percentile_threshold=90.0,
    amount_abnormal_percentile_threshold=90.0,
)


def _obs(iid: UUID, gap: float, amount: float, trade_date: date):
    return build_member_observation(
        instrument_id=iid,
        trade_date=trade_date,
        final_price=1.0 + gap,
        prev_close=1.0,
        amount=amount,
        quality_status="ok",
        source="historical_backfill",
    )


def _dates(n: int = 5):
    return [_T - timedelta(days=n - 1 - i) for i in range(n)]


def test_series_covers_every_requested_date() -> None:
    dates = _dates(5)
    obs = {d: [_obs(_A, 0.01, 100.0, d)] for d in dates}
    edges = [MembershipEdge(_A, "IND_BANK", "IND_BANK", FAMILY_INDUSTRY, dates[0], None)]
    s = build_scope_history_series(
        trade_dates=dates, observations_by_date=obs, edges=edges, config=CFG
    )
    assert set(s.industry) == set(dates)
    assert all("IND_BANK" in s.industry[d] for d in dates)


def test_pit_membership_changes_the_historical_series() -> None:
    """B joins at D3: earlier days must contain only A."""
    dates = _dates(5)
    obs = {d: [_obs(_A, 0.02, 100.0, d), _obs(_B, -0.04, 100.0, d)] for d in dates}
    join_date = dates[2]
    edges = [
        MembershipEdge(_A, "IND_BANK", "IND_BANK", FAMILY_INDUSTRY, dates[0], None),
        MembershipEdge(_B, "IND_BANK", "IND_BANK", FAMILY_INDUSTRY, join_date, None),
    ]
    s = build_scope_history_series(
        trade_dates=dates, observations_by_date=obs, edges=edges, config=CFG
    )
    # before the join: only A -> EW = +0.02
    assert s.industry[dates[0]]["IND_BANK"].equal_weight_gap == pytest.approx(0.02)
    assert s.industry[dates[1]]["IND_BANK"].equal_weight_gap == pytest.approx(0.02)
    # from the join: A and B -> EW = (0.02 + -0.04) / 2 = -0.01
    assert s.industry[join_date]["IND_BANK"].equal_weight_gap == pytest.approx(-0.01)


def test_industry_and_concept_stay_isolated() -> None:
    dates = _dates(3)
    obs = {d: [_obs(_A, 0.01, 100.0, d), _obs(_B, 0.03, 100.0, d)] for d in dates}
    edges = [
        MembershipEdge(_A, "IND_BANK", "IND_BANK", FAMILY_INDUSTRY, dates[0], None),
        MembershipEdge(_B, "CPT_ROBOT", "CPT_ROBOT", FAMILY_CONCEPT, dates[0], None),
    ]
    s = build_scope_history_series(
        trade_dates=dates, observations_by_date=obs, edges=edges, config=CFG
    )
    assert set(s.industry[_T]) == {"IND_BANK"}
    assert set(s.concept[_T]) == {"CPT_ROBOT"}
    # no cross-family contamination
    assert "CPT_ROBOT" not in s.industry[_T]


def test_same_instrument_in_two_concepts_is_aggregated_from_one_fact_list() -> None:
    dates = _dates(2)
    obs = {d: [_obs(_A, 0.02, 100.0, d)] for d in dates}
    edges = [
        MembershipEdge(_A, "CPT_ROBOT", "CPT_ROBOT", FAMILY_CONCEPT, dates[0], None),
        MembershipEdge(_A, "CPT_AI", "CPT_AI", FAMILY_CONCEPT, dates[0], None),
    ]
    s = build_scope_history_series(
        trade_dates=dates, observations_by_date=obs, edges=edges, config=CFG
    )
    assert s.concept[_T]["CPT_ROBOT"].equal_weight_gap == pytest.approx(0.02)
    assert s.concept[_T]["CPT_AI"].equal_weight_gap == pytest.approx(0.02)


def test_series_matches_direct_single_date_calculation() -> None:
    """The series must equal the one calculator called directly."""
    d = _T
    obs = [_obs(_A, 0.02, 100.0, d), _obs(_B, -0.01, 50.0, d)]
    edges = [MembershipEdge(_A, "IND_BANK", "IND_BANK", FAMILY_INDUSTRY, d, None),
             MembershipEdge(_B, "IND_BANK", "IND_BANK", FAMILY_INDUSTRY, d, None)]

    s = build_scope_history_series(
        trade_dates=[d], observations_by_date={d: obs}, edges=edges, config=CFG
    )

    from app.domain.auction.member_fact_adapter import to_member_facts

    facts = to_member_facts(obs, {})
    direct = compute_auction_l1_scope_facts(
        facts,
        [{"scope_id": "IND_BANK", "scope_family": FAMILY_INDUSTRY, "member_indices": [0, 1]}],
        CFG,
    )[0]
    assert s.industry[d]["IND_BANK"].equal_weight_gap == direct.equal_weight_gap
    assert s.industry[d]["IND_BANK"].amount_weighted_gap == direct.amount_weighted_gap


def test_ew_gap_series_preserves_dates_in_order() -> None:
    dates = _dates(4)
    obs = {d: [_obs(_A, 0.01, 100.0, d)] for d in dates}
    edges = [MembershipEdge(_A, "IND_BANK", "IND_BANK", FAMILY_INDUSTRY, dates[0], None)]
    s = build_scope_history_series(
        trade_dates=dates, observations_by_date=obs, edges=edges, config=CFG
    )
    series = s.ew_gap_series(FAMILY_INDUSTRY, "IND_BANK")
    assert [d for d, _ in series] == dates
    assert all(v == pytest.approx(0.01) for _, v in series)


def test_date_without_observations_is_empty_not_fabricated() -> None:
    dates = _dates(3)
    obs = {dates[0]: [_obs(_A, 0.01, 100.0, dates[0])]}
    edges = [MembershipEdge(_A, "IND_BANK", "IND_BANK", FAMILY_INDUSTRY, dates[0], None)]
    s = build_scope_history_series(
        trade_dates=dates, observations_by_date=obs, edges=edges, config=CFG
    )
    assert s.industry[dates[1]] == {}
    assert s.industry[dates[2]] == {}
