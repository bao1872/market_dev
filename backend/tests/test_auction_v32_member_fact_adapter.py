"""Tests for Observation + HistoryEvidence -> AuctionMemberFact adaptation.

The end-to-end case matters most: it proves the V3.2 chain can feed the
EXISTING single scope calculator without rewriting or duplicating it.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest

from app.domain.auction.member_fact import AuctionMemberFactConfig
from app.domain.auction.member_fact_adapter import to_member_facts
from app.domain.auction.member_history import compute_member_history_evidence
from app.domain.auction.member_observation import build_member_observation
from app.domain.auction.scope_fact import compute_auction_l1_scope_facts

_T = date(2026, 8, 14)
_A, _B, _C = uuid4(), uuid4(), uuid4()

CFG = AuctionMemberFactConfig(
    positive_gap_percentile_threshold=90.0,
    negative_gap_percentile_threshold=10.0,
    volume_abnormal_percentile_threshold=90.0,
    amount_abnormal_percentile_threshold=90.0,
)


def _obs(iid: UUID, gap: float | None, amount: float | None, trade_date: date = _T):
    return build_member_observation(
        instrument_id=iid,
        trade_date=trade_date,
        final_price=None if gap is None else 1.0 + gap,
        prev_close=1.0,
        amount=amount,
        quality_status="ok",
        source="historical_backfill",
    )


def _evidence(iid: UUID, current, n: int = 80):
    hist = [_obs(iid, 0.01 + i * 1e-6, 1000.0 + i, _T - timedelta(days=i + 1)) for i in range(n)]
    return compute_member_history_evidence(
        instrument_id=iid, trade_date=_T, current=current, history=hist
    )


# ---------------------------------------------------------------------------
# gap scale passthrough
# ---------------------------------------------------------------------------
def test_gap_ratio_passed_through_without_rescaling() -> None:
    """+2.30% must arrive as 0.023; no /100 and no *100 anywhere."""
    obs = _obs(_A, 0.023, 1000.0)
    (fact,) = to_member_facts([obs], {})
    assert fact.gap_pct == pytest.approx(0.023)
    assert fact.gap_pct != pytest.approx(2.3)
    assert fact.gap_pct != pytest.approx(0.00023)


def test_unavailable_gap_becomes_nan_not_zero() -> None:
    (fact,) = to_member_facts([_obs(_A, None, 1000.0)], {})
    assert math.isnan(fact.gap_pct)
    assert fact.gap_pct != 0


# ---------------------------------------------------------------------------
# current vs history eligibility are independent
# ---------------------------------------------------------------------------
def test_current_eligibility_comes_from_observation_only() -> None:
    obs = _obs(_A, 0.02, 1000.0)
    (fact,) = to_member_facts([obs], {})
    assert fact.current_gap_eligible is True
    assert fact.current_amount_eligible is True
    # no evidence supplied -> history must NOT be assumed eligible
    assert fact.gap_history_eligible is False
    assert fact.amount_history_eligible is False


def test_history_eligibility_comes_from_evidence_only() -> None:
    current = _obs(_A, 0.02, 1000.0)
    (fact,) = to_member_facts([current], {_A: _evidence(_A, current)})
    assert fact.current_gap_eligible is True
    assert fact.gap_history_eligible is True
    assert fact.amount_history_eligible is True
    assert fact.gap_percentile is not None


def test_current_ready_but_history_not_ready_stays_split() -> None:
    current = _obs(_A, 0.02, 1000.0)
    short = compute_member_history_evidence(
        instrument_id=_A,
        trade_date=_T,
        current=current,
        history=[_obs(_A, 0.01, 1000.0, _T - timedelta(days=i + 1)) for i in range(5)],
    )
    (fact,) = to_member_facts([current], {_A: short})
    assert fact.current_gap_eligible is True      # today computable
    assert fact.gap_history_eligible is False     # history NOT usable


def test_joint_eligible_derived_from_history_not_current() -> None:
    current = _obs(_A, 0.02, 1000.0)
    (with_ev,) = to_member_facts([current], {_A: _evidence(_A, current)})
    (without_ev,) = to_member_facts([current], {})
    assert with_ev.joint_eligible is True
    assert without_ev.joint_eligible is False


# ---------------------------------------------------------------------------
# volume demoted
# ---------------------------------------------------------------------------
def test_volume_fields_present_but_unavailable() -> None:
    (fact,) = to_member_facts([_obs(_A, 0.02, 1000.0)], {})
    assert math.isnan(fact.auction_volume)
    assert math.isnan(fact.volume_percentile)
    assert fact.current_volume_eligible is False
    assert fact.volume_history_eligible is False


# ---------------------------------------------------------------------------
# end-to-end: feed the EXISTING single scope calculator
# ---------------------------------------------------------------------------
def test_adapted_facts_feed_existing_scope_calculator() -> None:
    """EW / AW / breadth must be produced by the one existing owner."""
    members = [
        _obs(_A, 0.020, 100.0),
        _obs(_B, -0.010, 50.0),
        _obs(_C, 0.000, 30.0),
    ]
    evidence = {m.instrument_id: _evidence(m.instrument_id, m) for m in members}
    facts = to_member_facts(members, evidence)

    scopes = [
        {"scope_id": "s0", "scope_family": "market", "member_indices": []},
        {"scope_id": "s1", "scope_family": "industry", "member_indices": [0, 1, 2]},
    ]
    res = compute_auction_l1_scope_facts(facts, scopes, CFG)[1]

    # EW = (0.020 + -0.010 + 0.000) / 3
    assert res.equal_weight_gap == pytest.approx((0.020 - 0.010 + 0.0) / 3)
    assert res.equal_weight_gap_den == 3
    # AW = (0.020*100 + -0.010*50 + 0*30) / 180
    assert res.amount_weighted_gap == pytest.approx((2.0 - 0.5 + 0.0) / 180.0)
    # breadth identity on the SAME current-gap denominator
    assert (
        res.positive_gap_breadth_num
        + res.negative_gap_breadth_num
        + res.unchanged_gap_breadth_num
    ) == res.equal_weight_gap_den
    assert res.total_auction_amount == pytest.approx(180.0)


def test_concept_overlap_reuses_the_same_adapted_facts() -> None:
    """One instrument in two scopes must be adapted once, not recomputed."""
    members = [_obs(_A, 0.020, 100.0), _obs(_B, -0.010, 50.0)]
    evidence = {m.instrument_id: _evidence(m.instrument_id, m) for m in members}
    facts = to_member_facts(members, evidence)

    scopes = [
        {"scope_id": "c1", "scope_family": "concept", "member_indices": [0, 1]},
        {"scope_id": "c2", "scope_family": "concept", "member_indices": [0]},
    ]
    res = compute_auction_l1_scope_facts(facts, scopes, CFG)
    # both concepts see the identical gap value for the shared instrument
    assert res[0].equal_weight_gap == pytest.approx(0.005)
    assert res[1].equal_weight_gap == pytest.approx(0.020)
