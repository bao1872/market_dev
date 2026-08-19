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

SINGLE entry point: :func:`reconstruct_scope_series_batch`.  Membership is
resolved for ALL scopes in two queries (:func:`resolve_current_memberships_batch`)
and the member x date window is loaded ONCE through the shared union prep owner
(``prepare_union_fact_context`` + ``prepare_scopes_from_union``).  A single scope
routes through this SAME batch owner with a batch size of one — there is no
second single-scope implementation.

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
    build_union_fact_context_from_loaded_facts,
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


def _validate_current_board(scope_type: str, scope_key: str, board: Any) -> None:
    """Shared board-validation owner for current-static membership.

    PERF-FIX-STRUCTURAL-1 (P0-A): the single resolver and the batch resolver share
    ONE validation contract (scope_type -> board_type / hierarchy), never a copy.
    Raises :class:`HistoricalReconstructionError` on any mismatch.
    """
    if scope_type not in _SUPPORTED_SCOPE_TYPES:
        raise HistoricalReconstructionError(f"unsupported scope_type={scope_type}")
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


async def resolve_current_memberships_batch(
    session: AsyncSession,
    scope_type: str,
    scope_keys: list[str],
    *,
    asof_date: date,
) -> dict[str, CurrentStaticMembership]:
    """Resolve CURRENT STATIC MEMBERSHIP for many scopes in exactly TWO queries.

    PERF-FIX-STRUCTURAL-1 (P0-A): replaces the N+1 (one scope -> two sequential
    SQL round-trips) with a bulk resolver:

        SQL 1: SELECT all boards WHERE id IN (scope_keys)
        SQL 2: SELECT all memberships WHERE board_id IN (those boards)

    ``market_board_memberships`` PK is ``(board_id, instrument_id)`` so the
    ``board_id`` prefix is index-friendly.  Membership semantics are IDENTICAL to
    the former :func:`resolve_current_membership` (same ``_validate_current_board``
    owner), so a single scope routes through this SAME batch owner (batch size 1).
    """
    if scope_type not in _SUPPORTED_SCOPE_TYPES:
        raise HistoricalReconstructionError(f"unsupported scope_type={scope_type}")

    board_uuids: list[uuid.UUID] = []
    for scope_key in scope_keys:
        try:
            board_uuids.append(uuid.UUID(scope_key))
        except ValueError as exc:
            raise HistoricalReconstructionError(
                f"scope_key 非合法 UUID: {scope_key}"
            ) from exc

    # SQL 1: all boards for the requested scope keys.
    boards = {
        b.id: b
        for b in (
            await session.execute(
                select(MarketBoard).where(MarketBoard.id.in_(board_uuids))
            )
        ).scalars()
    }

    # SQL 2: all memberships for those boards (one query).
    members_by_board: dict[uuid.UUID, list[uuid.UUID]] = {}
    if boards:
        rows = (
            await session.execute(
                select(
                    MarketBoardMembership.boardId,
                    MarketBoardMembership.instrumentId,
                ).where(MarketBoardMembership.boardId.in_(boards.keys()))
            )
        ).all()
        for bid, iid in rows:
            members_by_board.setdefault(bid, []).append(iid)

    out: dict[str, CurrentStaticMembership] = {}
    for scope_key in scope_keys:
        buuid = uuid.UUID(scope_key)
        board = boards.get(buuid)
        _validate_current_board(scope_type, scope_key, board)
        assert board is not None  # _validate_current_board raises otherwise
        member_ids = tuple(dict.fromkeys(members_by_board.get(buuid, [])))
        out[scope_key] = CurrentStaticMembership(
            member_ids=member_ids,
            scope_name=board.name,
            asof_date=asof_date,
            member_count=len(member_ids),
        )
    return out


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

    Equivalent to running the former single-scope series reconstruction per
    scope_key, but the member x date bulk load is shared via a union of
    member_ids (PERF-2).  The business algorithm (:func:`compute_scope_observation`)
    is NEVER modified — the same ``PreparedScope`` per scope is produced, only the
    storage-layer load is deduplicated.  Returns one result dict per scope_key, in
    input order.
    """
    if not scope_keys:
        return []

    # 1) Resolve membership for ALL scopes in TWO queries (PERF-FIX-STRUCTURAL-1
    #    P0-A: replaces the former N+1 of one scope -> two sequential SQL round-trips
    #    with a single bulk board query + a single bulk membership query).  The
    #    current-static semantic owner is unchanged.
    memberships = await resolve_current_memberships_batch(
        session, scope_type, scope_keys, asof_date=asof_date
    )
    scope_members: dict[str, tuple[list[uuid.UUID], str]] = {}
    for scope_key in scope_keys:
        m = memberships[scope_key]
        scope_members[scope_key] = (
            list(m.member_ids),
            m.scope_name,
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
        # Empty shared context: no DB facts loaded — Current-only is served via
        # the exact-T snapshot facts only (built through the same pure core).
        union_ctx = build_union_fact_context_from_loaded_facts(
            t1_by_date={}, states_by_date={}, bars={}, events_by_date={},
        )
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
