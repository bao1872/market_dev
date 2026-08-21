"""Leadership Contribution — member-level derived fact (Internal Structure, PRD §14.4).

Stage 1 of the Internal Structure Dynamics closure.  For each member of a scope
on a given trade date:

    contribution_i = amount_share_i × return_1d_i

It answers: "which members contributed today's scope price move?"

Ownership boundary (single-owner principle):

- ``amount_share`` is NOT recomputed here.  It is consumed from the SINGLE
  canonical owner ``compute_member_amount_contributions`` (PRD §7.2), so the
  amount-share denominator has exactly one definition in the codebase.
- ``return_1d`` is read from the already-prepared ``MemberObservation``.
- ``contribution`` is ``None`` (unavailable) whenever EITHER ``amount_share`` or
  ``return_1d`` is missing — never coerced to 0.

Missing semantics (canonical ``MemberObservation``: ``amount >= 0`` valid,
zero amount valid, negative invalid):

- ``return_1d = 0``   -> legal member, contribution = 0.0 (a real zero move).
- ``return_1d = None`` / NaN / inf -> contribution unavailable (None).
- ``amount`` missing / non-finite / negative -> ``amount_share`` is None (from the
  shared owner), so contribution unavailable (None).
- ``amount == 0`` (total > 0)       -> legal member, amount_share = 0.0, so if
  return_1d is finite, contribution = 0.0.

Pure + deterministic + non-mutating.  No DB, no T-1, no ranking, no TopN, no
migration decision — those belong to later stages.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from app.domain.review.scope_observation import (
    MemberObservation,
    compute_member_amount_contributions,
)


@dataclass(frozen=True)
class MemberLeadershipContribution:
    """One member's leadership contribution evidence for a scope/date."""

    member_id: str
    amount_share: float | None
    return_1d: float | None
    contribution: float | None


@dataclass(frozen=True)
class LeadershipContributionFacts:
    """Scope aggregate of member leadership contributions.

    ``rankable_count`` = members with a computable contribution (both amount_share
    and return_1d present).  ``missing_count`` = members excluded because amount
    and/or return_1d is unavailable (NOT the same as contribution == 0).
    """

    rankable_count: int
    missing_count: int
    members: tuple[MemberLeadershipContribution, ...]


def _finite_return_1d(member: MemberObservation) -> float | None:
    """Return ``member.return_1d`` iff it is finite, else None (unavailable)."""
    value = member.return_1d
    if value is None or not math.isfinite(value):
        return None
    return value


def compute_member_leadership_contributions(
    members: Sequence[MemberObservation],
) -> LeadershipContributionFacts:
    """Compute per-member leadership contribution from prepared members.

    Reuses the single amount-share owner (``compute_member_amount_contributions``)
    so the denominator has one definition.  Contribution is amount_share ×
    return_1d; missing amount_share or missing/non-finite return_1d yields
    contribution = None (never 0).

    Deterministic: member order follows the input ``members`` order; no sorting is
    introduced here (ranking is a later-stage concern).
    """
    amount_facts = compute_member_amount_contributions(members)

    amount_share_by_id: dict[str, float | None] = {}
    for item in amount_facts.members:
        amount_share_by_id[item.member_id] = item.amount_share

    contributions: list[MemberLeadershipContribution] = []
    rankable = 0
    missing = 0
    for member in members:
        amount_share = amount_share_by_id.get(member.member_id)
        return_1d = _finite_return_1d(member)
        if amount_share is not None and return_1d is not None:
            contribution = amount_share * return_1d
            rankable += 1
        else:
            contribution = None
            missing += 1
        contributions.append(
            MemberLeadershipContribution(
                member_id=member.member_id,
                amount_share=amount_share,
                return_1d=return_1d,
                contribution=contribution,
            )
        )

    return LeadershipContributionFacts(
        rankable_count=rankable,
        missing_count=missing,
        members=tuple(contributions),
    )


__all__ = [
    "MemberLeadershipContribution",
    "LeadershipContributionFacts",
    "compute_member_leadership_contributions",
]
