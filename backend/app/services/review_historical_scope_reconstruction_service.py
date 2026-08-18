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
import time
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
    prepare_scopes_from_union,
    prepare_union_fact_context,
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
        t1_membership_available=prepared.t1_membership_available,
        event_coverage_member_ids=prepared.event_coverage_member_ids,
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

    # rules/25 §8.7 physical-cost instrumentation: surface vectorized VolumeContext
    # hit/fallback counts from the batch prep owner into the Composition Owner.
    prep_counters: dict[str, int] = {}
    prep_fallback_reasons: list[str] = []
    t_bulk = time.perf_counter()
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
        prep_counters=prep_counters,
        prep_fallback_reasons=prep_fallback_reasons,
    )
    bulk_ms = (time.perf_counter() - t_bulk) * 1000.0
    series: list[dict[str, Any]] = []
    t_obs = time.perf_counter()
    for prepared in prepared_list:
        observation = compute_scope_observation(
            scope_type=scope_type,
            scope_key=scope_key,
            trade_date=prepared.trade_date,
            pit_member_ids=prepared.pit_member_ids,
            pit_member_ids_t1=prepared.pit_member_ids_t1,
            members=prepared.members,
            events=prepared.events,
            t1_membership_available=prepared.t1_membership_available,
            event_coverage_member_ids=prepared.event_coverage_member_ids,
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
    obs_ms = (time.perf_counter() - t_obs) * 1000.0
    logger.info(
        "[scope-reconstruction] scope_type=%s scope_key=%s member_count=%d "
        "trade_date_count=%d bulk_prep_ms=%.1f per_t_observation_ms=%.1f "
        "vec_hit=%d vec_fallback=%d fallback_reasons=%s",
        scope_type, scope_key, membership.member_count, len(trade_dates),
        bulk_ms, obs_ms,
        prep_counters.get("vec_hit", 0), prep_counters.get("vec_fallback", 0),
        ",".join(prep_fallback_reasons) or "-",
    )
    return {
        "scope": {"scope_type": scope_type, "scope_key": scope_key},
        "membership": {
            "mode": "current_static",
            "asof_date": asof_date.isoformat(),
            "member_count": membership.member_count,
        },
        "series": series,
        "prep_metrics": {
            "vec_hit": prep_counters.get("vec_hit", 0),
            "vec_fallback": prep_counters.get("vec_fallback", 0),
            "fallback_reasons": list(prep_fallback_reasons),
        },
    }


# PERF-2: bounded batch size for union-member sharing.
# Measurement (review_scope_dynamics_probe --mode measure-all-scopes, 2026-08-17):
#   concept: 389 boards, union 5285 members, avg 12.89 boards/member (max 67)
#   industry_l1/l2/l3: non-overlapping (avg 1.00 board/member), union ~5286 each
#   single member near-400d bars avg 263 rows
# Even processing ALL 767 boards at once the union member set is only ~5286, so a
# single batch is well within memory/transfer bounds. The constant below caps the
# per-batch union member count; when exceeded the caller chunks scope_keys.
_UNION_MEMBER_CAP = 4096


async def reconstruct_scope_series_batch(
    session: AsyncSession,
    scope_type: str,
    scope_keys: list[str],
    trade_dates: list[date],
    *,
    asof_date: date,
    current_only: bool = False,
    union_member_cap: int = _UNION_MEMBER_CAP,
) -> list[dict[str, Any]]:
    """Reconstruct observation series for a batch of scopes, loading each member's
    historical window exactly ONCE across all scopes that share it.

    Equivalent to calling :func:`reconstruct_scope_series` per scope_key, but the
    member x date bulk load is shared via a union of member_ids (PERF-2).  The
    business algorithm (:func:`compute_scope_observation`) is NEVER modified — the
    same ``PreparedScope`` per scope is produced, only the storage-layer load is
    deduplicated.  Returns one result dict per scope_key, in input order.
    """
    if not scope_keys:
        return []

    # 1) Resolve membership per scope (current-static semantic owner unchanged).
    scope_members: dict[str, tuple[list[uuid.UUID], str]] = {}
    for scope_key in scope_keys:
        membership = await resolve_current_membership(
            session, scope_type, scope_key, asof_date=asof_date
        )
        scope_members[scope_key] = (
            list(membership.member_ids),
            membership.scope_name,
        )

    # 2) Chunk scope_keys so each batch's union member count stays bounded.
    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_union: set[uuid.UUID] = set()
    for scope_key in scope_keys:
        mids = set(scope_members[scope_key][0])
        if current_batch and len(current_union | mids) > union_member_cap:
            batches.append(current_batch)
            current_batch = []
            current_union = set()
        current_batch.append(scope_key)
        current_union |= mids
    if current_batch:
        batches.append(current_batch)

    results: list[dict[str, Any]] | None = None
    for batch in batches:
        batch_result = await _reconstruct_batch_chunk(
            session,
            scope_type,
            batch,
            trade_dates,
            asof_date=asof_date,
            current_only=current_only,
            scope_members=scope_members,
        )
        if results is None:
            results = batch_result
        else:
            results.extend(batch_result)
    return results if results is not None else []


async def _reconstruct_batch_chunk(
    session: AsyncSession,
    scope_type: str,
    scope_keys: list[str],
    trade_dates: list[date],
    *,
    asof_date: date,
    current_only: bool,
    scope_members: dict[str, tuple[list[uuid.UUID], str]],
) -> list[dict[str, Any]]:
    """Shared-load one chunk of scope_keys and reconstruct per-scope series."""
    prep_counters: dict[str, int] = {}
    prep_fallback_reasons: list[str] = []

    # 3) Union member_ids across the chunk -> ONE bulk load.
    union_member_ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for scope_key in scope_keys:
        for mid in scope_members[scope_key][0]:
            if mid not in seen:
                seen.add(mid)
                union_member_ids.append(mid)

    if current_only:
        union_ctx = _UnionFactContext(t1_by_date={}, states_by_date={},
                                      bars={}, events_by_date={}, vec_volume={})
    else:
        union_ctx = await prepare_union_fact_context(
            session, trade_dates, union_member_ids,
            prep_counters=prep_counters, prep_fallback_reasons=prep_fallback_reasons,
        )

    # 4) Slice the shared context per scope (same _build_member_observations owner).
    prepared_map = await prepare_scopes_from_union(
        session, scope_type, trade_dates,
        {k: scope_members[k] for k in scope_keys}, union_ctx,
        prep_counters=prep_counters, prep_fallback_reasons=prep_fallback_reasons,
    )

    # 5) compute_scope_observation per scope per T — UNCHANGED algorithm.
    t_obs = time.perf_counter()
    results: list[dict[str, Any]] = []
    for scope_key in scope_keys:
        membership = scope_members[scope_key]
        series = []
        for prepared in prepared_map[scope_key]:
            observation = compute_scope_observation(
                scope_type=scope_type,
                scope_key=scope_key,
                trade_date=prepared.trade_date,
                pit_member_ids=prepared.pit_member_ids,
                pit_member_ids_t1=prepared.pit_member_ids_t1,
                members=prepared.members,
                events=prepared.events,
                t1_membership_available=prepared.t1_membership_available,
                event_coverage_member_ids=prepared.event_coverage_member_ids,
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
        results.append(
            {
                "scope": {
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                },
                "membership": {
                    "mode": "current_static",
                    "asof_date": asof_date.isoformat(),
                    "member_count": len(membership[0]),
                },
                "series": series,
                "prep_metrics": {
                    "vec_hit": prep_counters.get("vec_hit", 0),
                    "vec_fallback": prep_counters.get("vec_fallback", 0),
                    "fallback_reasons": list(prep_fallback_reasons),
                },
            }
        )
    obs_ms = (time.perf_counter() - t_obs) * 1000.0
    logger.info(
        "[scope-reconstruction-batch] scope_type=%s chunk_size=%d "
        "union_member_count=%d trade_date_count=%d per_scope_observation_ms=%.1f "
        "vec_hit=%d vec_fallback=%d",
        scope_type, len(scope_keys), len(union_member_ids), len(trade_dates),
        obs_ms,
        prep_counters.get("vec_hit", 0), prep_counters.get("vec_fallback", 0),
    )
    return results
