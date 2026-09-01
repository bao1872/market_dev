"""Tests for V3.2 cross-section, contribution and leadership.

Contains the four machine-checkable reconciliation identities required by
V3.2 §二十四, plus family isolation for the cross-section and the explicit
empty-set semantics for leadership migration.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest

from app.domain.auction.contribution import compute_contributions, reconcile
from app.domain.auction.cross_sectional import (
    AXES,
    compute_cross_sectional,
)
from app.domain.auction.leadership import compute_leadership
from app.domain.auction.member_observation import build_member_observation

_T = date(2026, 8, 14)
_A, _B, _C, _D = uuid4(), uuid4(), uuid4(), uuid4()


def _obs(iid: UUID, gap: float | None, amount: float | None):
    return build_member_observation(
        instrument_id=iid,
        trade_date=_T,
        final_price=None if gap is None else 1.0 + gap,
        prev_close=1.0,
        amount=amount,
        quality_status="ok",
        source="historical_backfill",
    )


# ---------------------------------------------------------------------------
# cross-section
# ---------------------------------------------------------------------------
def test_cross_section_ranks_within_family() -> None:
    rows = [
        {"scope_key": "s1", "equal_weight_gap": 0.010},
        {"scope_key": "s2", "equal_weight_gap": 0.020},
        {"scope_key": "s3", "equal_weight_gap": 0.030},
    ]
    res = compute_cross_sectional(rows)
    assert res["s1"].repricing["equal_weight_gap"] < res["s2"].repricing["equal_weight_gap"]
    assert res["s2"].repricing["equal_weight_gap"] < res["s3"].repricing["equal_weight_gap"]


def test_missing_metric_is_none_not_zero() -> None:
    rows = [
        {"scope_key": "s1", "equal_weight_gap": 0.010},
        {"scope_key": "s2", "equal_weight_gap": None},
    ]
    res = compute_cross_sectional(rows)
    assert res["s2"].repricing["equal_weight_gap"] is None


def test_four_axes_are_independent_and_unscored() -> None:
    assert set(AXES) == {"repricing", "breadth", "participation", "concentration"}
    rows = [{"scope_key": "s1", "equal_weight_gap": 0.01, "capital_tilt": 0.002}]
    res = compute_cross_sectional(rows)["s1"]
    # no composite score attribute exists
    for forbidden in ("score", "total_score", "composite"):
        assert not hasattr(res, forbidden)


def test_axis_keys_do_not_overlap() -> None:
    seen: set[str] = set()
    for keys in AXES.values():
        assert not (seen & set(keys)), f"metric reused across axes: {seen & set(keys)}"
        seen |= set(keys)


# ---------------------------------------------------------------------------
# contribution reconciliation (V3.2 §二十四)
# ---------------------------------------------------------------------------
def _members():
    return [_obs(_A, 0.020, 100.0), _obs(_B, -0.010, 50.0), _obs(_C, 0.000, 30.0)]


def test_ew_contribution_sum_equals_ew_gap() -> None:
    members = _members()
    ew = (0.020 - 0.010 + 0.0) / 3
    aw = (0.020 * 100 - 0.010 * 50 + 0.0 * 30) / 180
    res = compute_contributions(
        trade_date=_T, members=members, ew_gap=ew, aw_gap=aw, scope_total_amount=180.0
    )
    assert res.ew_sum == pytest.approx(ew)
    assert reconcile(res, ew_gap=ew, aw_gap=aw)["ew_sum_matches_ew_gap"] is True


def test_amount_share_sum_is_one() -> None:
    res = compute_contributions(
        trade_date=_T, members=_members(), ew_gap=0.0, aw_gap=0.0, scope_total_amount=180.0
    )
    assert res.amount_share_sum == pytest.approx(1.0)
    assert reconcile(res, ew_gap=0.0, aw_gap=0.0)["amount_share_sum_is_one"] is True


def test_aw_contribution_sum_equals_aw_gap() -> None:
    members = _members()
    aw = (0.020 * 100 - 0.010 * 50 + 0.0 * 30) / 180
    res = compute_contributions(
        trade_date=_T, members=members, ew_gap=0.0, aw_gap=aw, scope_total_amount=180.0
    )
    assert res.aw_sum == pytest.approx(aw)
    assert reconcile(res, ew_gap=0.0, aw_gap=aw)["aw_sum_matches_aw_gap"] is True


def test_positive_and_negative_contributions_are_both_preserved() -> None:
    res = compute_contributions(
        trade_date=_T, members=_members(), ew_gap=0.0, aw_gap=0.0, scope_total_amount=180.0
    )
    assert len(res.positive_ew) == 1
    assert len(res.negative_ew) == 1
    assert len(res.positive_aw) == 1
    assert len(res.negative_aw) == 1


def test_ineligible_members_do_not_break_identity() -> None:
    members = [_obs(_A, 0.020, 100.0), _obs(_D, None, None)]
    ew = 0.020  # only A is price-valid
    res = compute_contributions(
        trade_date=_T, members=members, ew_gap=ew, aw_gap=0.020, scope_total_amount=100.0
    )
    assert res.price_valid_count == 1
    assert res.ew_sum == pytest.approx(ew)
    assert res.amount_share_sum == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# leadership
# ---------------------------------------------------------------------------
def _contribs():
    res = compute_contributions(
        trade_date=_T, members=_members(), ew_gap=0.0, aw_gap=0.0, scope_total_amount=180.0
    )
    return res.members


def test_leader_set_is_minimal_prefix_explaining_50_percent() -> None:
    lead = compute_leadership(contributions=_contribs(), ew_gap=0.01)
    assert lead.direction == 1
    assert lead.leaders  # non-empty
    assert lead.explained_ratio is not None
    assert lead.explained_ratio >= 0.5


def test_direction_follows_ew_gap_sign() -> None:
    assert compute_leadership(contributions=_contribs(), ew_gap=-0.01).direction == -1
    assert compute_leadership(contributions=_contribs(), ew_gap=0.01).direction == 1


def test_direction_unavailable_yields_empty_set() -> None:
    lead = compute_leadership(contributions=_contribs(), ew_gap=None)
    assert lead.leaders == ()
    assert "DIRECTION_UNAVAILABLE" in lead.reason_codes


def test_empty_both_sides_jaccard_is_none_not_zero() -> None:
    lead = compute_leadership(contributions=_contribs(), ew_gap=0.0, previous_leaders=[])
    assert lead.jaccard is None
    assert lead.migration is None


def test_full_turnover_gives_jaccard_zero() -> None:
    lead = compute_leadership(
        contributions=_contribs(), ew_gap=0.01, previous_leaders=[_D]
    )
    assert lead.jaccard == 0.0
    assert lead.migration == 1.0


def test_stable_set_gives_jaccard_one() -> None:
    first = compute_leadership(contributions=_contribs(), ew_gap=0.01)
    second = compute_leadership(
        contributions=_contribs(), ew_gap=0.01, previous_leaders=first.leaders
    )
    assert second.jaccard == pytest.approx(1.0)
    assert second.migration == pytest.approx(0.0)
    assert second.retained == first.leaders
    assert second.entrants == ()
    assert second.exits == ()


def test_no_score_fields_are_produced() -> None:
    lead = compute_leadership(contributions=_contribs(), ew_gap=0.01)
    for forbidden in ("leader_score", "opportunity_score", "risk_score"):
        assert not hasattr(lead, forbidden)
