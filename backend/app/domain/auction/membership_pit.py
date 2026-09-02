"""Auction V3.2 PIT Scope membership owner.

Source of truth: ``board_membership_history`` (models/board_taxonomy.py), which
carries ``effective_from`` / ``effective_to`` and therefore supports real
point-in-time membership.

Interval convention — NOT invented here, taken from the repo's existing PIT
query (``backend/app/services/review_scope_service.py:441-444``)::

    effective_from <= trade_date
    AND (effective_to IS NULL OR effective_to > trade_date)

i.e. a **half-open** interval ``[effective_from, effective_to)``: the end date
is EXCLUSIVE.  An open interval (``effective_to IS NULL``) is still current.

Why this matters (V3.2 §八): a historical Scope Fact must be built from the
membership that was真实 on that day.  Back-filling yesterday's scope with
today's member list would answer "how did today's constituents behave" instead
of "how did this board itself evolve" — a different product question.
This module makes the PIT choice explicit and testable.

The module is pure: no DB access.  Callers supply the already-loaded edges.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

__all__ = [
    "MembershipEdge",
    "is_effective_at",
    "resolve_scope_members",
    "resolve_scope_members_bulk",
]

FAMILY_INDUSTRY = "industry"
FAMILY_CONCEPT = "concept"


@dataclass(frozen=True)
class MembershipEdge:
    """One instrument's membership in one board over a validity interval."""

    instrument_id: UUID
    #: business scope identity (``MarketBoard.externalCode``) — never a raw UUID
    scope_key: str
    scope_name: str
    family: str  # "industry" | "concept"
    effective_from: date
    #: exclusive end; ``None`` means still effective
    effective_to: date | None


def is_effective_at(edge: MembershipEdge, trade_date: date) -> bool:
    """Half-open ``[effective_from, effective_to)`` validity test."""
    if edge.effective_from > trade_date:
        return False
    if edge.effective_to is None:
        return True
    return edge.effective_to > trade_date


def definition_version_effective_in_window(
    effective_from: date | None,
    effective_to: date | None,
    trade_date: date,
    window_start: date,
) -> bool:
    """A ``BoardDefinitionVersion`` is usable for the window iff it overlaps
    ``[window_start, T]`` (half-open on both ends, mirroring the membership rule).

    A membership row can still overlap the window while the board definition that
    created it has already ended; without this check the stale board would leak
    into the scope.  Used by the V3.2 loader in addition to the equivalent SQL
    predicate so the rule is unit-testable without a database.
    """
    if effective_from is None or effective_from > trade_date:
        return False
    if effective_to is not None and effective_to <= window_start:
        return False
    return True


def resolve_scope_members(
    edges: Sequence[MembershipEdge],
    trade_date: date,
    *,
    family: str | None = None,
) -> dict[str, tuple[UUID, ...]]:
    """Return ``scope_key -> sorted member instrument ids`` valid at T.

    ``family`` filters to ``industry`` or ``concept`` so the two peer cohorts
    are never mixed into one ranking universe.
    """
    out: dict[str, set[UUID]] = {}
    for edge in edges:
        if family is not None and edge.family != family:
            continue
        if not is_effective_at(edge, trade_date):
            continue
        out.setdefault(edge.scope_key, set()).add(edge.instrument_id)
    return {key: tuple(sorted(members)) for key, members in out.items()}


def resolve_scope_members_bulk(
    edges: Sequence[MembershipEdge],
    trade_dates: Sequence[date],
    *,
    family: str | None = None,
) -> dict[date, dict[str, tuple[UUID, ...]]]:
    """Resolve PIT membership for MANY dates in ONE pass over the edges.

    Performance contract: this is O(len(edges) * len(trade_dates)) in memory
    with no I/O — it must never be replaced by a per-scope/per-day query.
    One instrument belonging to several concepts is still visited once per
    (edge, date) pair and reused for every scope it appears in.
    """
    dates = sorted(set(trade_dates))
    buckets: dict[date, dict[str, set[UUID]]] = {d: {} for d in dates}
    for edge in edges:
        if family is not None and edge.family != family:
            continue
        for d in dates:
            if not is_effective_at(edge, d):
                continue
            buckets[d].setdefault(edge.scope_key, set()).add(edge.instrument_id)
    return {
        d: {k: tuple(sorted(v)) for k, v in scopes.items()} for d, scopes in buckets.items()
    }


def edges_from_rows(
    rows: Iterable[Any],
    *,
    board_meta: dict[UUID, tuple[str, str, str]],
) -> list[MembershipEdge]:
    """Adapt raw ``board_membership_history`` rows into edges.

    ``board_meta`` maps ``board_definition_version_id -> (scope_key, name, family)``
    and is supplied by the caller (the board/definition read side), keeping this
    module free of persistence concerns.
    """
    out: list[MembershipEdge] = []
    for row in rows:
        meta = board_meta.get(row.board_definition_version_id)
        if meta is None:
            continue
        scope_key, scope_name, family = meta
        out.append(
            MembershipEdge(
                instrument_id=row.instrument_id,
                scope_key=scope_key,
                scope_name=scope_name,
                family=family,
                effective_from=row.effective_from,
                effective_to=row.effective_to,
            )
        )
    return out
