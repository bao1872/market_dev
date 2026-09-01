"""Auction V3.2 core-member set and leadership migration (V3.2 §十三).

Objective evidence only — explicitly NO leader score, opportunity score or
risk score.

Selection:
- ``direction = sign(EW Gap)`` (1 / -1 / 0);
- ``aligned_i = AW Contribution_i * direction`` — how much a member's
  capital-weighted move actually confirms the board's direction;
- candidates are the members with ``aligned_i > 0``;
- ordering is ``aligned DESC`` then ``instrument_id ASC`` (deterministic);
- the leader set is the **minimal prefix** of that ordering whose cumulative
  aligned contribution explains at least 50% of the total positive aligned
  contribution.

Migration (today vs yesterday):
- ``retained`` / ``entrants`` / ``exits``;
- ``jaccard = |A n B| / |A u B|``;
- ``migration = 1 - jaccard``.

Empty-set semantics (must stay explicit, never a silent 0 or 1):
- both today's and yesterday's sets empty -> no comparison is possible:
  ``jaccard = None``, ``migration = None``, reason ``EMPTY_LEADER_SETS``;
- only one side empty -> ``jaccard = 0.0``, ``migration = 1.0`` (full turnover);
- ``direction == 0`` or EW Gap unavailable -> empty leader set with reason
  ``DIRECTION_UNAVAILABLE`` / ``NO_ALIGNED_CONTRIBUTION``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from app.domain.auction.contribution import MemberContribution

__all__ = [
    "LeadershipResult",
    "MIN_EXPLAINED_RATIO",
    "compute_leadership",
]

MIN_EXPLAINED_RATIO = 0.5

_REASON_DIRECTION_UNAVAILABLE = "DIRECTION_UNAVAILABLE"
_REASON_NO_ALIGNED = "NO_ALIGNED_CONTRIBUTION"
_REASON_EMPTY_BOTH = "EMPTY_LEADER_SETS"


@dataclass(frozen=True)
class LeadershipResult:
    direction: int
    leaders: tuple[UUID, ...]
    aligned_total: float | None
    explained_ratio: float | None
    retained: tuple[UUID, ...]
    entrants: tuple[UUID, ...]
    exits: tuple[UUID, ...]
    jaccard: float | None
    migration: float | None
    reason_codes: tuple[str, ...]


def _empty(direction: int, codes: tuple[str, ...], previous: Sequence[UUID]) -> LeadershipResult:
    return LeadershipResult(
        direction=direction,
        leaders=(),
        aligned_total=None,
        explained_ratio=None,
        retained=(),
        entrants=(),
        exits=tuple(sorted(set(previous), key=str)),
        jaccard=None if not previous else 0.0,
        migration=None if not previous else 1.0,
        reason_codes=codes,
    )


def compute_leadership(
    *,
    contributions: Sequence[MemberContribution],
    ew_gap: float | None,
    previous_leaders: Sequence[UUID] = (),
    min_explained_ratio: float = MIN_EXPLAINED_RATIO,
) -> LeadershipResult:
    """Select today's core members and measure migration vs yesterday."""
    prev = tuple(sorted(set(previous_leaders), key=str))

    if ew_gap is None or ew_gap == 0:
        return _empty(0, (_REASON_DIRECTION_UNAVAILABLE,), prev)

    direction = 1 if ew_gap > 0 else -1

    aligned: list[tuple[UUID, float]] = []
    for c in contributions:
        if c.aw_contribution is None:
            continue
        value = c.aw_contribution * direction
        if value > 0:
            aligned.append((c.instrument_id, value))

    if not aligned:
        return _empty(direction, (_REASON_NO_ALIGNED,), prev)

    aligned.sort(key=lambda item: (-item[1], str(item[0])))
    total = sum(v for _, v in aligned)
    target = total * min_explained_ratio

    cumulative = 0.0
    leaders: list[UUID] = []
    for instrument_id, value in aligned:
        leaders.append(instrument_id)
        cumulative += value
        if cumulative >= target:
            break

    leader_set = set(leaders)
    prev_set = set(prev)
    retained = tuple(sorted(leader_set & prev_set, key=str))
    entrants = tuple(sorted(leader_set - prev_set, key=str))
    exits = tuple(sorted(prev_set - leader_set, key=str))

    union_size = len(leader_set | prev_set)
    if union_size == 0:
        jaccard: float | None = None
        migration: float | None = None
        codes = (_REASON_EMPTY_BOTH,)
    else:
        jaccard = len(leader_set & prev_set) / union_size
        migration = 1.0 - jaccard
        codes = ()

    return LeadershipResult(
        direction=direction,
        leaders=tuple(sorted(leader_set, key=str)),
        aligned_total=total,
        explained_ratio=(cumulative / total) if total else None,
        retained=retained,
        entrants=entrants,
        exits=exits,
        jaccard=jaccard,
        migration=migration,
        reason_codes=codes,
    )
