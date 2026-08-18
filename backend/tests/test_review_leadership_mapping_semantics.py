"""Stage 2B CORRECTION-2 — Leadership mapping direction-semantics Golden tests.

These lock the R3 / Candidate A / Candidate B direction semantics that the static
NO-MIGRATION checks cannot prove.  They exercise the pure research helpers
(``_three_rankings`` / ``_coverage_leader_set`` / ``_ew_return_direction``) with
synthetic MemberObservations and verify:

  1. up-day   : EW>0, positive contribution -> direction leader
  2. down-day : EW<0, negative contribution -> direction leader   (KEY regression)
  3. contrarian: EW<0, positive contribution -> aligned_score<0 -> excluded from B
  4. price_candidate=False must NOT change canonical EW direction (owned elsewhere)
  5. EW unavailable (None) -> R3 unavailable (not 0, not pseudo-rank)
  6. EW exactly 0 -> no prevailing direction -> Candidate A/B unavailable

The research helpers consume ONLY ``aligned_score`` for R3/concentration/Candidate B
— never the raw contribution sign.
"""

from __future__ import annotations

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.scope_observation import MemberObservation
from scripts.review_scope_dynamics_probe import (
    _AlignedLeadership,
    _coverage_leader_set,
    _ew_return_direction,
    _three_rankings,
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


# ---------------------------------------------------------------------------
# _ew_return_direction boundary (unavailable vs zero vs nonzero)
# ---------------------------------------------------------------------------


def test_ew_direction_boundaries() -> None:
    assert _ew_return_direction(None) == (False, 0)    # unavailable
    assert _ew_return_direction(0.0) == (True, 0)      # no prevailing direction
    assert _ew_return_direction(0.001) == (True, 1)    # up
    assert _ew_return_direction(-0.001) == (True, -1)  # down


# ---------------------------------------------------------------------------
# R3 direction semantics
# ---------------------------------------------------------------------------


def test_r3_up_day_positive_contribution_leads() -> None:
    # EW>0 (scope up).  A has positive contribution -> aligned_score>0 -> leader.
    members = [
        _m("a", return_1d=0.05, amount=60.0),   # +, leader
        _m("b", return_1d=0.02, amount=40.0),   # +, follower
    ]
    r1, r2, r3, direction = _three_rankings(members, ew_return=0.04)
    assert direction == 1
    assert r3 is not None
    assert r3[0].member_id == "a"
    assert r3[0].aligned_score > 0.0


def test_r3_down_day_negative_contribution_leads() -> None:
    # KEY regression: EW<0 (scope down).  A has the LARGEST NEGATIVE contribution,
    # which is the true down-day leader after direction alignment.
    members = [
        _m("a", return_1d=-0.06, amount=50.0),   # biggest negative push
        _m("b", return_1d=-0.03, amount=50.0),   # negative, smaller
        _m("c", return_1d=0.02, amount=50.0),    # contrarian (positive)
    ]
    r1, r2, r3, direction = _three_rankings(members, ew_return=-0.04)
    assert direction == -1
    assert r3 is not None
    # Direction-aligned leaders are the DOWN drivers first.  Each member has
    # amount=50 -> share=1/3, so contribution = (1/3)*return:
    #   a: (1/3)(-0.06)=-0.02 -> aligned +0.02
    #   b: (1/3)(-0.03)=-0.01 -> aligned +0.01
    #   c: (1/3)(+0.02)=+0.0067 -> aligned -0.0067 (contrarian, last)
    assert [x.member_id for x in r3] == ["a", "b", "c"]
    assert r3[0].aligned_score == pytest.approx(0.02, abs=1e-9)
    assert r3[0].aligned_score > 0.0
    assert r3[1].aligned_score == pytest.approx(0.01, abs=1e-9)
    # Contrarian member c has negative aligned_score and is ranked last in R3.
    assert r3[2].member_id == "c"
    assert r3[2].aligned_score < 0.0
    # R1 (signed contribution DESC) is a DIFFERENT ranking: it ranks the biggest
    # POSITIVE contribution first — here c (+0.0067) — proving R1 and R3 are
    # distinct and raw contribution is preserved (not mutated by alignment).
    assert r1[0].member_id == "c"
    assert r1[0].contribution > 0.0


def test_r3_down_day_contrarian_excluded_from_candidate_b() -> None:
    # EW<0, contrarian member with positive raw contribution must NOT enter B.
    members = [
        _m("a", return_1d=-0.06, amount=50.0),
        _m("c", return_1d=0.02, amount=50.0),  # contrarian
    ]
    r1, r2, r3, direction = _three_rankings(members, ew_return=-0.03)
    assert direction == -1
    assert r3 is not None
    ls = _coverage_leader_set(r3, coverage=0.5)
    ids = {x.member_id for x in ls}
    assert "a" in ids          # down-driver is a leader
    assert "c" not in ids      # contrarian excluded (aligned_score<0)


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
    r1, r2, r3, direction = _three_rankings(members, ew_return=0.05)
    assert r3 is not None
    # aligned positive scores: a=0.06? no - contribution=amount_share*return.
    # amounts a=30,b=30,c=30,d=10 -> total 100 -> shares .3/.3/.3/.1
    # contributions: a=.3*.06=.018 b=.3*.04=.012 c=.3*.02=.006 d=.1*.01=.001
    # total positive = .037.  50% -> .0185 -> {a, b} (a=.018 alone < .0185).
    ls = _coverage_leader_set(r3, coverage=0.5)
    ids = {x.member_id for x in ls}
    assert ids == {"a", "b"}
    assert all(x.aligned_score > 0.0 for x in ls)


# ---------------------------------------------------------------------------
# price_candidate=False must not change canonical EW direction
# ---------------------------------------------------------------------------


def test_price_candidate_false_does_not_change_ew_direction() -> None:
    # The canonical equal_weight_return is owned by compute_scope_observation
    # (price-candidate + finite exact-T1 filter).  The research helper consumes the
    # ALREADY-computed ew_return; a non-price-candidate member's return must never
    # be fed in as if it were the scope EW.  Here we verify the helper uses the
    # canonical ew_return argument, and that a contrarian flag does not flip it.
    members = [_m("a", return_1d=0.02, amount=100.0)]
    # Even if a member has an extreme return, the direction is whatever the formal
    # owner reported.  With ew_return=-0.01 (scope down), a positive member must be
    # ranked as contrarian (aligned_score<0), NOT leader.
    r1, r2, r3, direction = _three_rankings(members, ew_return=-0.01)
    assert direction == -1
    assert r3 is not None
    assert r3[0].aligned_score < 0.0  # positive return against a down scope


def test_price_candidate_false_excluded_from_canonical_ew() -> None:
    # REAL synthetic case through the formal owner: member A is price_candidate=True
    # with +2%; member B is price_candidate=False with -80%.  The canonical
    # equal_weight_return must be +2% — B's -80% must NOT enter the EW universe.
    from datetime import date

    from app.domain.review.scope_observation import compute_scope_observation

    b_member = MemberObservation(
        member_id="b",
        price_candidate=False,      # NOT in the price universe
        return_1d=-0.80,            # extreme, but must be excluded from EW
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
    assert obs["price"]["equal_weight_return"] == pytest.approx(0.02, abs=1e-12)

    # Feed the canonical EW into the research helper: scope is UP, so a (+2%) is
    # the direction-aligned leader; the extreme contrarian b is NOT a leader.
    r1, r2, r3, direction = _three_rankings(
        [_m("a", return_1d=0.02, amount=100.0), b_member],
        ew_return=obs["price"]["equal_weight_return"],
    )
    assert direction == 1
    assert r3 is not None
    # Both a and b are rankable for contribution, but only a is a direction leader.
    assert r3[0].member_id == "a"
    assert r3[0].aligned_score > 0.0


# ---------------------------------------------------------------------------
# EW unavailable / zero
# ---------------------------------------------------------------------------


def test_ew_unavailable_r3_not_computed() -> None:
    members = [_m("a", return_1d=0.02, amount=100.0)]
    r1, r2, r3, direction = _three_rankings(members, ew_return=None)
    assert direction == 0
    assert r3 is None                     # R3 unavailable, NOT a pseudo-ranking
    assert len(r1) == 1                   # R1/R2 still available
    assert len(r2) == 1
    # Candidate B over an unavailable R3 is UNAVAILABLE (None), NOT an empty set.
    assert _coverage_leader_set(r3, 0.5) is None


def test_ew_zero_no_prevailing_direction() -> None:
    members = [_m("a", return_1d=0.0, amount=100.0), _m("b", return_1d=0.0, amount=100.0)]
    r1, r2, r3, direction = _three_rankings(members, ew_return=0.0)
    assert direction == 0
    assert r3 is None                     # no prevailing direction -> no R3
    # Candidate A/B unavailable; must NOT select a member_id pseudo Top5.
    assert _coverage_leader_set(r3, 0.5) is None


def test_unavailable_vs_legitimate_empty_leader_set_distinct() -> None:
    # R3 unavailable (None) -> leader set None (unavailable).
    assert _coverage_leader_set(None, 0.5) is None
    # R3 valid but NO member has aligned_score>0 (all oppose the direction) ->
    # leader set is a legitimate EMPTY list, distinct from unavailable.
    # Construct a valid R3 where every member has negative aligned_score.
    r3_all_negative = [
        _AlignedLeadership(member_id="a", contribution=-0.02, aligned_score=-0.02),
        _AlignedLeadership(member_id="b", contribution=-0.01, aligned_score=-0.01),
    ]
    empty_set = _coverage_leader_set(r3_all_negative, 0.5)
    assert empty_set == []
    assert empty_set is not None  # distinct from unavailable
