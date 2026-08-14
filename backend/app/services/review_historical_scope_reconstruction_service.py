"""Current-universe historical Scope Observation reconstruction (Review v2.3).

Contract: **CURRENT STATIC MEMBERSHIP x historical First Pyramid facts**.

- ``members(S)`` = the current latest canonical membership of Scope S
  (``market_boards`` + ``market_board_memberships`` current projection).  This
  set is fixed for the whole historical reconstruction series.
- For every historical trade date ``T``, each current member's First Pyramid
  historical facts at ``T`` are read (exact ``T`` and exact canonical ``T-1``),
  a ``MemberObservation`` is built per member, and the Scope Observation is
  produced by ``compute_scope_observation`` (single canonical owner).
- Historical / PIT / ASOF membership is NEVER consulted.  Current facts are
  NEVER backfilled into a historical ``T``.
- Provenance lives OUTSIDE the canonical payload — never a top-level L1 section.

Shadow only: not wired into Filter / Discovery / publication / orchestrator.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.scope_observation import compute_scope_observation
from app.models.market_board import MarketBoard, MarketBoardMembership
from app.services.review_observation_persistence_service import (
    validate_scope_observation_payload,
)
from app.services.review_observation_prep_service import (
    PreparedScope,
    prepare_scope_from_member_ids,
    prepare_scope_series_from_member_ids,
)

logger = logging.getLogger("review_historical_scope_reconstruction")

# Board validation mirrors ``review_scope_service`` (the same current-state owner),
# NOT a copy of its PIT membership query logic.
_BOARD_TYPE_FROM_SCOPE: dict[str, str] = {
    "industry_l1": "industry",
    "industry_l2": "industry",
    "industry_l3": "industry",
    "concept": "concept",
}
_HIERARCHY_FROM_SCOPE: dict[str, str | None] = {
    "industry_l1": "L1",
    "industry_l2": "L2",
    "industry_l3": "L3",
    "concept": None,
}
_SUPPORTED_SCOPE_TYPES = frozenset(_BOARD_TYPE_FROM_SCOPE)


class HistoricalReconstructionError(RuntimeError):
    """Raised for invalid scope keys, board reference errors or type mismatches."""


@dataclass(frozen=True)
class CurrentStaticMembership:
    """Current latest canonical membership, fixed for the whole series."""

    member_ids: tuple[uuid.UUID, ...]
    scope_name: str
    asof_date: date
    member_count: int


async def resolve_current_membership(
    session: AsyncSession,
    scope_type: str,
    scope_key: str,
    *,
    asof_date: date,
) -> CurrentStaticMembership:
    """Resolve CURRENT STATIC MEMBERSHIP from the current projection owner.

    Owner: ``market_boards`` (current hierarchy L1/L2/L3 + concept) joined to
    ``market_board_memberships`` (current per-board instrument rows).  No
    historical / PIT / ASOF resolution is ever consulted; the board's current
    members are fixed for the whole reconstruction series.
    """
    if scope_type not in _SUPPORTED_SCOPE_TYPES:
        raise HistoricalReconstructionError(f"unsupported scope_type={scope_type}")
    try:
        board_uuid = uuid.UUID(scope_key)
    except ValueError as exc:
        raise HistoricalReconstructionError(f"scope_key 非合法 UUID: {scope_key}") from exc
    board = (
        await session.execute(
            select(MarketBoard).where(MarketBoard.id == board_uuid).limit(1),
        )
    ).scalar_one_or_none()
    if board is None:
        raise HistoricalReconstructionError(f"board_not_found: {scope_key}")
    expected_type = _BOARD_TYPE_FROM_SCOPE[scope_type]
    if board.type != expected_type:
        raise HistoricalReconstructionError(
            f"scope_type mismatch: {scope_type} board_type={board.type}"
        )
    expected_level = _HIERARCHY_FROM_SCOPE[scope_type]
    if expected_level is not None and board.hierarchyLevel != expected_level:
        raise HistoricalReconstructionError(
            f"scope hierarchy mismatch: {scope_type} hierarchy={board.hierarchyLevel}"
        )
    rows = (
        await session.execute(
            select(MarketBoardMembership.instrumentId).where(
                MarketBoardMembership.boardId == board.id,
            )
        )
    ).scalars()
    member_ids = tuple(dict.fromkeys(rows))  # dedupe, preserve order
    return CurrentStaticMembership(
        member_ids=member_ids,
        scope_name=board.name,
        asof_date=asof_date,
        member_count=len(member_ids),
    )


@dataclass(frozen=True)
class ReconstructedObservation:
    """One historical Scope Observation plus its prep metadata."""

    trade_date: date
    prepared: PreparedScope
    observation: dict[str, Any]
    provided_member_count: int


async def reconstruct_scope_observation(
    session: AsyncSession,
    scope_type: str,
    scope_key: str,
    trade_date: date,
    membership: CurrentStaticMembership,
) -> ReconstructedObservation:
    """Rebuild one historical Scope Observation with the FIXED current universe.

    Member facts come strictly from ``trade_date`` (T) and its exact canonical
    T-1 — never from the current day.  The canonical payload is produced by
    ``compute_scope_observation`` and contract-validated; provenance is NOT
    injected into the payload.
    """
    prepared = await prepare_scope_from_member_ids(
        session,
        scope_type,
        scope_key,
        membership.scope_name,
        trade_date,
        list(membership.member_ids),
        # Historical reconstruction is built ONLY from FP history + bars + FP
        # events.  Current-only snapshot facts stay None for historical T (PRD
        # v2.3) and the large summary_payload JSONB is never transferred.
        load_current_only=False,
    )
    observation = compute_scope_observation(
        scope_type=scope_type,
        scope_key=scope_key,
        trade_date=trade_date,
        pit_member_ids=prepared.pit_member_ids,
        pit_member_ids_t1=prepared.pit_member_ids_t1,
        members=prepared.members,
        events=prepared.events,
    )
    validate_scope_observation_payload(
        observation,
        scope_type=scope_type,
        scope_key=scope_key,
        trade_date=trade_date,
    )
    return ReconstructedObservation(
        trade_date=trade_date,
        prepared=prepared,
        observation=observation,
        provided_member_count=observation["scope"]["provided_member_count"],
    )


async def reconstruct_scope_series(
    session: AsyncSession,
    scope_type: str,
    scope_key: str,
    trade_dates: list[date],
    *,
    asof_date: date,
) -> dict[str, Any]:
    """Rebuild an ordered historical Scope Observation series (BATCH path).

    Membership is resolved once (CURRENT STATIC).  The whole member x date
    window is read in ONE bulk pass (``prepare_scope_series_from_member_ids``)
    and replayed per T, then each observation is produced by the single
    canonical owner ``compute_scope_observation`` and contract-validated.
    Provenance lives OUTSIDE the canonical payloads.
    """
    membership = await resolve_current_membership(
        session, scope_type, scope_key, asof_date=asof_date
    )
    prepared_list = await prepare_scope_series_from_member_ids(
        session,
        scope_type,
        scope_key,
        membership.scope_name,
        trade_dates,
        list(membership.member_ids),
        # Historical reconstruction is built ONLY from FP history + bars + FP
        # events.  Current-only snapshot facts stay None for historical T (PRD
        # v2.3) and the large summary_payload JSONB is never transferred.
        load_current_only=False,
    )
    series: list[dict[str, Any]] = []
    for prepared in prepared_list:
        observation = compute_scope_observation(
            scope_type=scope_type,
            scope_key=scope_key,
            trade_date=prepared.trade_date,
            pit_member_ids=prepared.pit_member_ids,
            pit_member_ids_t1=prepared.pit_member_ids_t1,
            members=prepared.members,
            events=prepared.events,
        )
        validate_scope_observation_payload(
            observation,
            scope_type=scope_type,
            scope_key=scope_key,
            trade_date=prepared.trade_date,
        )
        series.append(
            {
                "trade_date": prepared.trade_date.isoformat(),
                "provided_member_count": observation["scope"]["provided_member_count"],
                "observation": observation,
            }
        )
    return {
        "scope": {"scope_type": scope_type, "scope_key": scope_key},
        "membership": {
            "mode": "current_static",
            "asof_date": asof_date.isoformat(),
            "member_count": membership.member_count,
        },
        "series": series,
    }
