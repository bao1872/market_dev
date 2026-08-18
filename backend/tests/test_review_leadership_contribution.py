"""Stage 1 — Leadership Contribution member-level facts (PRD §14.4).

Tests the pure derived owner ``compute_member_leadership_contributions``:

    contribution_i = amount_share_i × return_1d_i

Key contracts locked here:

- ``amount_share`` comes ONLY from the single canonical owner
  ``compute_member_amount_contributions`` (no second denominator).
- ``return_1d`` missing / NaN / inf -> contribution unavailable (None), never 0.
- ``return_1d == 0`` -> real zero contribution (0.0), distinct from missing.
- amount missing / <= 0 -> amount_share None -> contribution None.
- Deterministic ordering (input order preserved; no hidden ranking here).
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.analysis.leadership_contribution import (
    compute_member_leadership_contributions,
)
from app.domain.review.scope_observation import MemberObservation

pytestmark = pytest.mark.pure_unit


def _m(
    mid: str,
    *,
    return_1d: float | None = None,
    amount: float | None = None,
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


def _contribs(members: list[MemberObservation]) -> dict[str, dict[str, float | None]]:
    facts = compute_member_leadership_contributions(members)
    return {c.member_id: {
        "amount_share": c.amount_share,
        "return_1d": c.return_1d,
        "contribution": c.contribution,
    } for c in facts.members}


# ---------------------------------------------------------------------------
# Core formula
# ---------------------------------------------------------------------------


def test_contribution_formula_amount_share_times_return() -> None:
    # amounts a=30, b=20, c=10 -> total 60; shares 0.5/0.3333/0.1667.
    # canonical return unit: +5% = 0.05, -4% = -0.04, +2% = 0.02.
    members = [
        _m("a", return_1d=0.05, amount=30.0),
        _m("b", return_1d=-0.04, amount=20.0),
        _m("c", return_1d=0.02, amount=10.0),
    ]
    out = _contribs(members)
    assert out["a"]["amount_share"] == pytest.approx(30 / 60)
    assert out["a"]["contribution"] == pytest.approx((30 / 60) * 0.05)
    assert out["b"]["amount_share"] == pytest.approx(20 / 60)
    assert out["b"]["contribution"] == pytest.approx((20 / 60) * -0.04)
    assert out["c"]["contribution"] == pytest.approx((10 / 60) * 0.02)


def test_contribution_negative_return() -> None:
    out = _contribs([_m("a", return_1d=-0.02, amount=100.0)])
    # single member -> share 1.0, contribution -0.02.
    assert out["a"]["amount_share"] == pytest.approx(1.0)
    assert out["a"]["contribution"] == pytest.approx(-0.02)


# ---------------------------------------------------------------------------
# Missing semantics
# ---------------------------------------------------------------------------


def test_return_zero_is_real_zero_not_missing() -> None:
    out = _contribs([_m("a", return_1d=0.0, amount=100.0)])
    assert out["a"]["return_1d"] == 0.0
    assert out["a"]["contribution"] == 0.0


def test_return_missing_is_unavailable_not_zero() -> None:
    out = _contribs([_m("a", return_1d=None, amount=100.0)])
    assert out["a"]["return_1d"] is None
    assert out["a"]["contribution"] is None
    assert out["a"]["contribution"] != 0


def test_return_nan_is_unavailable() -> None:
    out = _contribs([_m("a", return_1d=float("nan"), amount=100.0)])
    assert out["a"]["return_1d"] is None
    assert out["a"]["contribution"] is None


def test_amount_missing_makes_contribution_unavailable() -> None:
    out = _contribs([_m("a", return_1d=0.03, amount=None)])
    assert out["a"]["amount_share"] is None
    assert out["a"]["contribution"] is None


def test_amount_negative_makes_contribution_unavailable() -> None:
    out = _contribs([_m("a", return_1d=0.03, amount=-5.0)])
    assert out["a"]["amount_share"] is None
    assert out["a"]["contribution"] is None


def test_amount_zero_with_total_positive_is_real_zero() -> None:
    # a has 100 amount, b has 0 -> b share = 0.0, contribution = 0.0 (real).
    out = _contribs([_m("a", return_1d=0.02, amount=100.0), _m("b", return_1d=0.01, amount=0.0)])
    assert out["b"]["amount_share"] == pytest.approx(0.0)
    assert out["b"]["contribution"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Single / multi member + counts
# ---------------------------------------------------------------------------


def test_single_member_rankable() -> None:
    facts = compute_member_leadership_contributions([_m("a", return_1d=0.01, amount=10.0)])
    assert facts.rankable_count == 1
    assert facts.missing_count == 0
    assert facts.members[0].contribution == pytest.approx(0.01)


def test_rankable_vs_missing_counts() -> None:
    members = [
        _m("a", return_1d=0.01, amount=10.0),   # rankable
        _m("b", return_1d=None, amount=10.0),  # missing return
        _m("c", return_1d=0.01, amount=None),   # missing amount
    ]
    facts = compute_member_leadership_contributions(members)
    assert facts.rankable_count == 1
    assert facts.missing_count == 2


def test_missing_never_zero_when_mixed() -> None:
    out = _contribs([_m("a", return_1d=0.01, amount=10.0), _m("b", return_1d=None, amount=10.0)])
    assert out["a"]["contribution"] is not None
    assert out["b"]["contribution"] is None


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic_output_order_preserved() -> None:
    members = [
        _m("c", return_1d=0.03, amount=10.0),
        _m("a", return_1d=0.01, amount=20.0),
        _m("b", return_1d=-0.02, amount=30.0),
    ]
    first = compute_member_leadership_contributions(members)
    second = compute_member_leadership_contributions(members)
    assert [c.member_id for c in first.members] == ["c", "a", "b"]
    assert [c.member_id for c in second.members] == ["c", "a", "b"]
    assert first == second
