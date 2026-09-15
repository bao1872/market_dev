"""Stage 2B CORRECTION-2 — Leadership mapping direction-semantics Golden tests.

These lock the R3 / Candidate A / Candidate B direction semantics that the static
NO-MIGRATION checks cannot prove.  They exercise the single production owner
``build_leadership_snapshot`` (frozen Leadership Migration Numerical Contract,
PRD §7.10) with synthetic ``MemberObservation`` inputs and verify:

  1. up-day   : EW>0, positive contribution -> direction leader
  2. down-day : EW<0, negative contribution -> direction leader   (KEY regression)
  3. contrarian: EW<0, positive contribution -> aligned_score<0 -> excluded from B
  4. price_candidate=False must NOT change canonical EW direction (owned elsewhere)
  5. EW unavailable (None) -> snapshot unavailable (not 0, not pseudo-ranking)
  6. EW exactly 0 -> no prevailing direction -> snapshot unavailable

The production owner consumes ONLY ``aligned_score`` for R3/concentration/Candidate
B — never the raw contribution sign.  The minimal-prefix 50% coverage and the
direction alignment are both enforced by ``build_leadership_snapshot``; tests
assert on its ``leader_set`` / ``leader_ids`` / ``direction`` / ``status``.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.analysis.leadership_contribution import (
    compute_member_leadership_contributions,
)
from app.domain.review.analysis.leadership_migration import (
    build_leadership_snapshot,
)
from app.domain.review.scope_observation import (
    MemberObservation,
    compute_scope_observation,
)

pytestmark = pytest.mark.pure_unit


def _m(
    mid: str,
    *,
    return_1d: float | None = None,
    amount: float | None = 100.0,
) -> MemberObservation:
    return MemberObservation(
        member_id=mid,
        price_candidate=True,
        return_1d=return_1d,
        amount=amount,
        trend=Direction.UP,
        swing=Direction.UP,
        internal=Direction.UP,
        momentum=MomentumDirection.EXPANDING,
    )


def _snap(members: list[MemberObservation], *, ew_return: float | None):
    """Build a ``LeadershipSnapshot`` for the given members + canonical EW."""
    contribution_facts = compute_member_leadership_contributions(list(members))
    return build_leadership_snapshot(
        trade_date="2026-01-05",
        ew_return=ew_return,
        contribution_facts=contribution_facts,
    )


# ---------------------------------------------------------------------------
# EW direction boundary (unavailable vs zero vs nonzero)
# ---------------------------------------------------------------------------


def test_ew_direction_boundaries() -> None:
    cf = compute_member_leadership_contributions([_m("a", return_1d=0.05, amount=60.0)])
    # unavailable (no EW) -> direction None
    assert (
        build_leadership_snapshot(
            trade_date="2026-01-05", ew_return=None, contribution_facts=cf
        ).direction
        is None
    )
    # no prevailing direction (EW == 0) -> unavailable, direction None
    zero = build_leadership_snapshot(
        trade_date="2026-01-05", ew_return=0.0, contribution_facts=cf
    )
    assert zero.status == "unavailable" and zero.direction is None
    # up
    assert (
        build_leadership_snapshot(
            trade_date="2026-01-05", ew_return=0.001, contribution_facts=cf
        ).direction
        == 1
    )
    # down
    assert (
        build_leadership_snapshot(
            trade_date="2026-01-05", ew_return=-0.001, contribution_facts=cf
        ).direction
        == -1
    )


# ---------------------------------------------------------------------------
# R3 direction semantics
# ---------------------------------------------------------------------------


def test_r3_up_day_positive_contribution_leads() -> None:
    # EW>0 (scope up).  A has positive contribution -> aligned_score>0 -> leader.
    members = [
        _m("a", return_1d=0.05, amount=60.0),  # +, leader
        _m("b", return_1d=0.02, amount=40.0),  # +, follower
    ]
    snap = _snap(members, ew_return=0.04)
    assert snap.direction == 1
    assert snap.leader_ids == ("a",)
    assert snap.leader_set[0].aligned_score > 0.0


def test_r3_down_day_negative_contribution_leads() -> None:
    # KEY regression: EW<0 (scope down).  A has the LARGEST NEGATIVE contribution,
    # which is the true down-day leader after direction alignment.
    members = [
        _m("a", return_1d=-0.06, amount=50.0),  # biggest negative push
        _m("b", return_1d=-0.03, amount=50.0),  # negative, smaller
        _m("c", return_1d=0.02, amount=50.0),  # contrarian (positive)
    ]
    snap = _snap(members, ew_return=-0.04)
    assert snap.direction == -1
    # amounts equal -> share 1/3; contributions a=-0.02 b=-0.01 c=+0.0067;
    # aligned = contribution * (-1): a=+0.02 b=+0.01 c=-0.0067.  Only a reaches the
    # 50% minimal prefix -> single down-day leader.
    assert snap.leader_ids == ("a",)
    assert snap.leader_set[0].aligned_score == pytest.approx(0.02, abs=1e-9)
    assert snap.leader_set[0].aligned_score > 0.0


def test_r3_down_day_contrarian_excluded_from_candidate_b() -> None:
    # EW<0, contrarian member with positive raw contribution must NOT enter the
    # direction-aligned leader set.
    members = [
        _m("a", return_1d=-0.06, amount=50.0),
        _m("c", return_1d=0.02, amount=50.0),  # contrarian
    ]
    snap = _snap(members, ew_return=-0.03)
    assert snap.direction == -1
    assert "a" in snap.leader_ids  # down-driver is a leader
    assert "c" not in snap.leader_ids  # contrarian excluded (aligned_score<0)


# ---------------------------------------------------------------------------
# Candidate B minimal-prefix coverage (uses aligned_score only)
# ---------------------------------------------------------------------------


def test_candidate_b_minimal_prefix_coverage() -> None:
    members = [
        _m("a", return_1d=0.06, amount=30.0),
        _m("b", return_1d=0.04, amount=30.0),
        _m("c", return_1d=0.02, amount=30.0),
        _m("d", return_1d=0.01, amount=10.0),
    ]
    snap = _snap(members, ew_return=0.05)
    # amounts a=30,b=30,c=30,d=10 -> total 100 -> shares .3/.3/.3/.1
    # contributions: a=.018 b=.012 c=.006 d=.001; total positive=.037; 50%->.0185
    # -> {a, b} (a alone .018 < .0185).
    assert set(snap.leader_ids) == {"a", "b"}
    assert all(ls.aligned_score > 0.0 for ls in snap.leader_set)


# ---------------------------------------------------------------------------
# price_candidate=False must not change canonical EW direction
# ---------------------------------------------------------------------------


def test_price_candidate_false_does_not_change_ew_direction() -> None:
    # The canonical equal_weight_return is owned by compute_scope_observation
    # (price-candidate + finite exact-T1 filter).  The production owner consumes the
    # ALREADY-computed ew_return; a non-price-candidate member's return must never
    # be fed in as if it were the scope EW.  Here we verify that with ew_return=-0.01
    # (scope down), a positive member is ranked as contrarian (aligned_score<0), NOT
    # leader.
    members = [_m("a", return_1d=0.02, amount=100.0)]
    snap = _snap(members, ew_return=-0.01)
    assert snap.direction == -1  # scope down
    assert snap.status == "ready"  # EW is valid (nonzero)
    # Positive return against a down scope -> negative aligned -> no leader.
    assert snap.leader_ids == ()


def test_price_candidate_false_excluded_from_canonical_ew() -> None:
    # REAL synthetic case through the formal owner: member A is price_candidate=True
    # with +2%; member B is price_candidate=False with -80%.  The canonical
    # equal_weight_return must be +2% — B's -80% must NOT enter the EW universe.
    b_member = MemberObservation(
        member_id="b",
        price_candidate=False,  # NOT in the price universe
        return_1d=-0.80,  # extreme, but must be excluded from EW
        amount=100.0,
        trend=Direction.UP,
        swing=Direction.UP,
        internal=Direction.UP,
        momentum=MomentumDirection.EXPANDING,
    )
    obs = compute_scope_observation(
        scope_type="industry",
        scope_key="electronics",
        trade_date=date(2026, 1, 5),
        pit_member_ids=["a", "b"],
        members=[_m("a", return_1d=0.02, amount=100.0), b_member],
        event_coverage_member_ids=None,
    )
    # Canonical EW = mean over price-valid returns ONLY = +0.02 (a), not -0.39
    # (which would result if b's -0.80 entered the mean).
    ew = obs["price"]["equal_weight_return"]
    assert ew == pytest.approx(0.02, abs=1e-12)

    # Feed the canonical EW into the production owner: scope is UP, so a (+2%) is
    # the direction-aligned leader; the extreme contrarian b is NOT a leader.
    snap = build_leadership_snapshot(
        trade_date="2026-01-05",
        ew_return=ew,
        contribution_facts=compute_member_leadership_contributions(
            [_m("a", return_1d=0.02, amount=100.0), b_member]
        ),
    )
    assert snap.direction == 1
    assert snap.leader_ids == ("a",)
    assert snap.leader_set[0].aligned_score > 0.0


# ---------------------------------------------------------------------------
# EW unavailable / zero
# ---------------------------------------------------------------------------


def test_ew_unavailable_r3_not_computed() -> None:
    members = [_m("a", return_1d=0.02, amount=100.0)]
    snap = _snap(members, ew_return=None)
    assert snap.status == "unavailable"  # R3 unavailable, NOT a pseudo-ranking
    assert snap.direction is None
    assert snap.leader_set is None


def test_ew_zero_no_prevailing_direction() -> None:
    members = [_m("a", return_1d=0.0, amount=100.0), _m("b", return_1d=0.0, amount=100.0)]
    snap = _snap(members, ew_return=0.0)
    assert snap.status == "unavailable"  # no prevailing direction -> no R3
    assert snap.direction is None
    assert snap.leader_set is None


def test_unavailable_vs_legitimate_empty_leader_set_distinct() -> None:
    # EW unavailable -> leader set None (unavailable).
    unavailable = _snap([_m("a", return_1d=0.02, amount=100.0)], ew_return=None)
    assert unavailable.leader_set is None
    # EW valid but NO member has aligned_score>0 (all oppose the direction) ->
    # leader set is a legitimate EMPTY list, distinct from unavailable.
    all_contrarian = _snap(
        [_m("a", return_1d=0.02, amount=50.0), _m("b", return_1d=0.01, amount=50.0)],
        ew_return=-0.03,  # down scope -> both members contrarian
    )
    assert all_contrarian.leader_set == ()  # legitimate empty, ready
    assert all_contrarian.leader_set is not None  # distinct from unavailable
