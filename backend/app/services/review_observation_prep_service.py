"""Canonical Observation Data Preparation — DB-aware layer (Round 1B).

Single responsibility: turn real canonical data into ``MemberObservation``
inputs plus the PIT(T) / PIT(T-1) member sets, feeding
``app.domain.review.scope_observation.compute_scope_observation``.

This layer owns the exact canonical T-1 resolution (from the trading calendar),
PIT membership resolution per scope family, and First Pyramid / bar loading.
The pure semantic mapping lives in ``app.services.observation_prep``; the Core
(``scope_observation.py``) stays untouched.

CANONICAL: this is the single preparation owner consumed by the orchestrator
(``prepare_current_scope_observations_batch``) and by the historical reconstruction
(``prepare_scopes_from_union``).  It is NOT a shadow path.
"""

from __future__ import annotations

import gc
import json
import logging
import resource
import uuid
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.member_fact import (
    DailyBarFact,
    previous_state_to_flat,
    snapshot_flat_to_continuous,
    snapshot_flat_to_flat_t,
    state_to_continuous,
)
from app.domain.review.member_fact import (
    number as coerce_number,
)
from app.domain.review.scope_observation import MemberObservation, StructureEvent
from app.models.bar import BarDaily
from app.models.calendar import TradingCalendar
from app.models.first_pyramid_history import (
    FirstPyramidHistoryDailyState,
    FirstPyramidHistoryEvent,
)
from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
from app.models.first_pyramid_history_run_item import FirstPyramidHistoryRunItem
from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION
from app.services import calendar_service, review_scope_service
from app.services.board_membership_service import PITMembershipUnavailableError
from app.services.observation_prep import (
    _BOARD_CURRENT_ELIGIBLE_KEY,
    _BOARD_CURRENT_FLAT_KEY,
    _BOARD_CURRENT_SYMBOL_KEY,
    RawMemberFacts,
    build_member_observation,
    build_member_observation_from_facts,
)
from app.services.volume_context import (
    LONG_WINDOW,
    SHORT_WINDOW,
    VectorizedVolumeContext,
    compute_volume_context_vectorized,
    vectorized_context_at,
)

# History payload contract version for canonical immutable events (M2 isolation).
HISTORY_CONTRACT_VERSION = "review-history-v2"

logger = logging.getLogger("review_observation_prep_service")

# Bar history window for the shared vol/amt ratio SSOT (same as Review pipeline).
_RATIO_WINDOW = 20
_BAR_LOOKBACK_DAYS = 400

# P1 instrumentation (PERF-OOM-V2): report BOTH current and peak RSS.
# Preferred Linux source is /proc/self/status (VmRSS = current resident,
# VmHWM = high-water peak).  ru_maxrss from resource.getrusage is only a
# high-water mark (NOT current), so we never label it as current RSS; it is
# used only as a fallback when /proc is unavailable (macOS/CI).  No tracemalloc.
_PROC_SELF_STATUS = "/proc/self/status"


def _read_proc_rss() -> tuple[int | None, int | None]:
    """Return (current_rss_bytes, peak_rss_bytes) from /proc/self/status.

    VmRSS is the live resident set size; VmHWM is the high-water mark.
    Returns (None, None) if /proc is unavailable. No tracemalloc, no side effects.
    """
    try:
        with open(_PROC_SELF_STATUS, "r", encoding="utf-8") as fh:
            cur = peak = None
            for line in fh:
                if cur is not None and peak is not None:
                    break
                if line.startswith("VmRSS:"):
                    # e.g. "VmRSS:   123456 kB"
                    cur = int(line.split()[1]) * 1024
                elif line.startswith("VmHWM:"):
                    peak = int(line.split()[1]) * 1024
            return cur, peak
    except OSError:
        return None, None


def _log_rss(
    stage: str,
    union_member_count: int = 0,
    trade_date_count: int = 0,
    *,
    bar_rows: int | None = None,
    state_rows: int | None = None,
    event_rows: int | None = None,
    vector_member_count: int | None = None,
    scope_count: int = 0,
    member_refs_t: int = 0,
    member_refs_t1: int = 0,
    current_only_member_count: int = 0,
    coverage_member_count: int = 0,
    scope_type: str = "",
    batch_index: int | None = None,
    result_count: int = 0,
    reconstruction_count: int = 0,
    processed_scope_count: int = 0,
    session_identity_map_count: int | None = None,
    session_new_count: int | None = None,
    session_dirty_count: int | None = None,
) -> None:
    """Log current + peak RSS (MiB) at a named preparation stage.

    Reports both current_rss_mb (live resident) and peak_rss_mb (high-water),
    sourced from /proc/self/status on Linux. On platforms without /proc, falls
    back to resource.getrusage(ru_maxrss) for the peak value only — and is
    explicitly NOT labelled as current RSS. Pure read, no tracemalloc (which
    must never run in production).

    The extra counters (scope_count / member_refs_t / member_refs_t1 /
    current_only_member_count / coverage_member_count) are emitted only by the
    early ``current-prep-*`` attribution markers in
    ``prepare_current_scope_observations_batch``; they default to 0 so the
    existing ``union-prep-*`` call sites are unchanged.

    M4 post-prep attribution markers reuse the same helper with additional
    observability fields (scope_type / batch_index / result_count /
    reconstruction_count / processed_scope_count / session_*_count). These are
    pure logging only — never read back into any business computation.
    """
    cur_bytes, peak_bytes = _read_proc_rss()
    if peak_bytes is None:
        # Fallback: ru_maxrss is high-water peak only; current unknown.
        peak_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    current_mb = (cur_bytes / (1024 * 1024)) if cur_bytes is not None else float("nan")
    peak_mb = peak_bytes / (1024 * 1024)
    extra = ""
    if bar_rows is not None:
        extra += f" bar_rows={bar_rows}"
    if state_rows is not None:
        extra += f" state_rows={state_rows}"
    if event_rows is not None:
        extra += f" event_rows={event_rows}"
    if vector_member_count is not None:
        extra += f" vec_members={vector_member_count}"
    if scope_count:
        extra += f" scope_count={scope_count}"
    if member_refs_t:
        extra += f" member_refs_t={member_refs_t}"
    if member_refs_t1:
        extra += f" member_refs_t1={member_refs_t1}"
    if current_only_member_count:
        extra += f" current_only_members={current_only_member_count}"
    if coverage_member_count:
        extra += f" coverage_members={coverage_member_count}"
    if scope_type:
        extra += f" scope_type={scope_type}"
    if batch_index is not None:
        extra += f" batch_index={batch_index}"
    if result_count:
        extra += f" result_count={result_count}"
    if reconstruction_count:
        extra += f" reconstruction_count={reconstruction_count}"
    if processed_scope_count:
        extra += f" processed_scope_count={processed_scope_count}"
    if session_identity_map_count is not None:
        extra += f" session_identity_map_count={session_identity_map_count}"
    if session_new_count is not None:
        extra += f" session_new_count={session_new_count}"
    if session_dirty_count is not None:
        extra += f" session_dirty_count={session_dirty_count}"
    logger.info(
        "[RSS] stage=%s current_rss_mb=%.1f peak_rss_mb=%.1f union_member_count=%d trade_date_count=%d%s",
        stage,
        current_mb,
        peak_mb,
        union_member_count,
        trade_date_count,
        extra,
    )

# Historical market membership is unresolvable this round: ``resolve_scope_members
# ("market", ...)`` returns the CURRENT active universe and ignores trade_date.
# Market shadow is therefore skipped (see the guard in
# ``prepare_current_scope_observations_batch``); it is never computed from a
# current snapshot against a historical trade_date.
MARKET_SKIP_DIAGNOSTIC = (
    "historical_market_membership_unresolved: "
    "market membership is current active universe, not historical PIT; "
    "Market observation skipped this round"
)


# Canonical event-type casing.  CHoCH/CHOCH/... are storage-case artifacts, NOT a
# product distinction.  Scope Core must not perceive "CHoCH" vs "CHOCH".  This is
# the single normalization boundary; producer storage values are left untouched.
_EVENT_TYPE_NORMALIZATION: dict[str, str] = {
    "choch": "CHoCH",
    "bos": "BOS",
    "ob_created": "OB_CREATED",
    "ob_entered": "OB_ENTERED",
    "ob_mitigated": "OB_MITIGATED",
    "eqh": "EQH",
    "eql": "EQL",
    "sqz_release": "SQZ_RELEASE",
}


def _normalize_event_type(raw: str | None) -> str:
    """Normalize canonical event_type at the loader boundary (case-insensitive)."""
    if not raw:
        return ""
    token = raw.strip().lower()
    return _EVENT_TYPE_NORMALIZATION.get(token, raw.strip().upper())


# ----------------------------------------------------------------------------
# SHARED Source-Fact Mappers (REVIEW-V23-PHASE1-R0-A)
# ----------------------------------------------------------------------------
# These pure helpers are the SINGLE mapping owner for translating source facts
# (a SQLAlchemy row, a PG JSONB dict, or a Dataset parquet/JSON row) into the
# internal representations consumed by ``_build_member_observations`` /
# ``compute_scope_observation``.  Both the DB loader path and the Dataset Replay
# Adapter path call the SAME helpers (Gate 2 Adapter contract parity): we never
# duplicate a mapping in two places, so a semantic drift cannot appear between
# "DB path" and "Dataset path".
#
# ``_decode_jsonb`` is deliberately loose on input type: PostgreSQL JSONB arrives
# as a Python ``dict`` while a Dataset parquet row carries the same JSON as a
# JSON ``str`` (the parquet converter writes dict/list columns as ``pa.string()``
# + ``json.dumps``).  It accepts ``dict`` / ``str`` / ``None`` and rejects any
# other type or invalid JSON with ``ValueError`` (fail fast, never silent
# fallback).


def _decode_jsonb(value: Any) -> dict:
    """Decode a JSONB-ish value to a dict, supporting both DB dict and str.

    - ``dict``     -> returned as-is (defensive shallow copy).
    - ``str``      -> ``json.loads``; MUST decode to a dict else ValueError.
    - ``None``     -> ``{}``.
    - other / invalid JSON / non-dict JSON -> ``ValueError``.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"_decode_jsonb: invalid JSON string: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(
                f"_decode_jsonb: JSON decoded to {type(loaded).__name__}, expected dict"
            )
        return loaded
    raise ValueError(f"_decode_jsonb: unsupported type {type(value).__name__}")


def _map_daily_bar_fact(
    *,
    trade_date: date,
    open: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
    volume: float | None,
    amount: float | None,
) -> DailyBarFact:
    """Shared DailyBarFact mapper (exact numeric coercion, no NaN injection)."""
    return DailyBarFact(
        trade_date=trade_date,
        open=coerce_number(open),
        high=coerce_number(high),
        low=coerce_number(low),
        close=coerce_number(close),
        volume=coerce_number(volume),
        amount=coerce_number(amount),
    )


def _map_structure_event(
    *,
    instrument_id: str,
    event_type: str,
    direction: Any,
    level: Any,
    internal: Any,
    release_volume_ratio: Any,
) -> StructureEvent:
    """Shared immutable StructureEvent mapper (identical to the DB loaders).

    Canonical boundary normalization (event_type casing), numeric level /
    release_volume_ratio coercion, and the bool-only ``internal`` gate are all
    applied here so the DB path and the Dataset path produce byte-identical
    events.
    """
    internal_flag: bool | None = bool(internal) if isinstance(internal, bool) else None
    return StructureEvent(
        member_id=instrument_id,
        event_type=_normalize_event_type(event_type),
        direction=direction,
        level=(float(level) if isinstance(level, (int, float)) else None),
        internal=internal_flag,
        release_volume_ratio=(
            float(release_volume_ratio)
            if isinstance(release_volume_ratio, (int, float))
            else None
        ),
    )


# ----------------------------------------------------------------------------
# CURRENT-ONLY canonical source (REVIEW-V23-A-CORRECTION-3)
# ----------------------------------------------------------------------------
# Source ownership split enforced by this module:
#   * Historical-capable facts -> ``FirstPyramidHistoryDailyState`` exact T.
#   * Current-only facts       -> ``StockFeatureSnapshot`` exact T, via
#     ``summary_payload.first_pyramid_flat``.
#
# The Current-only facts below have NO point-in-time member-day history series in
# the First Pyramid history contract.  Per PRD v2.3 a missing historical series
# MUST NOT suppress the Current fact, so they are read from the exact-T snapshot.
#
# Hard invariant: exact ``trade_date == T`` only.  There is NO fallback to a
# "latest" snapshot, and no fallback to T+1 — that would be a time-key violation
# (future leakage) under AGENTS.md §8.  A member whose exact-T snapshot is absent
# or not consumable simply yields ``None`` for these facts.
_CURRENT_ONLY_SNAPSHOT_FIELDS: dict[str, str] = {
    # MemberObservation attribute  ->  first_pyramid_flat key
    "release_volume_ratio": "fp_release_volume_ratio",
    "momentum_volume_relation": "fp_momentum_volume_relation",
    "bb_position": "fp_bb_position",
    "bb_width": "fp_bb_width",
    "vwap_ret_total": "fp_vwap_ret_total",
    "trailing_top_pct": "fp_distance_to_trailing_top_pct",
    "trailing_bottom_pct": "fp_distance_to_trailing_bottom_pct",
}

# Snapshot run gate: only a published, succeeded run may be consumed.  This
# mirrors the existing snapshot consumption gate (feature_snapshot_service /
# review_scope_service) rather than inventing a second selection policy.
_SNAPSHOT_RUN_CONSUMABLE_STATUS = "succeeded"


async def _load_current_only_snapshot_facts(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_date: date,
    *,
    source_core_run_id: uuid.UUID,
) -> dict[str, dict[str, object]]:
    """Load exact-T Current-only canonical facts per member.

    Returns ``{instrument_id: {MemberObservation attr: value}}`` for members that
    have a consumable exact-T snapshot.  Members without one are simply absent
    from the mapping, which downstream maps to ``None`` (unavailable) — never to a
    fallback snapshot from another trade date.

    Slice 4A1R2 — Core lineage lock:
    The query is additionally locked to the ReviewRun's immutable
    ``source_core_run_id`` identity
    (``StockFeatureSnapshot.source_run_id == source_core_run_id`` and
    ``StockFeatureSnapshotRun.id == source_core_run_id``).  This makes the Review
    snapshot source lineage-identical to the legacy Board path
    (``StockFeatureSnapshot.source_run_id == source_run_id``).  Multiple
    succeeded/published runs may exist for the same ``trade_date``; Review must not
    silently consume another core run on rerun / resume.  If the row for the
    specified ``source_core_run_id`` is absent, this returns empty (fail-safe) —
    never a fallback to another same-day run.
    """
    if not instrument_ids:
        return {}

    # Exact-T only, joined against the run gate (succeeded + published), AND locked
    # to the immutable source_core_run_id identity (Slice 4A1R2).
    #
    # M3-B (OOM closure): the SELECT projects ONLY the JSONB subtree
    # ``summary_payload -> 'first_pyramid_flat'`` (via the ``->`` operator),
    # NOT the full ``summary_payload`` (~57 KB avg / ~1.6 GB decompressed across
    # the whole union).  PostgreSQL therefore transfers and the application
    # hydrates only the ~19-20 MB flat — the primary pre-chunk memory owner.
    # ``first_pyramid_flat`` is kept WHOLE (all fp_* keys): key-level projection
    # is intentionally deferred so the migrated Board capabilities consume the
    # identical flat payload they did before (no semantic drift).  The JSONB
    # ``->`` expression carries type JSONB, so the driver decodes the subtree
    # directly into a Python dict (missing key / JSON null -> None).
    stmt = (
        select(
            StockFeatureSnapshot.instrument_id,
            StockFeatureSnapshot.summary_payload.op("->")("first_pyramid_flat"),
        )
        .join(
            StockFeatureSnapshotRun,
            StockFeatureSnapshot.source_run_id == StockFeatureSnapshotRun.id,
        )
        .where(
            StockFeatureSnapshot.trade_date == trade_date,
            StockFeatureSnapshot.instrument_id.in_(instrument_ids),
            StockFeatureSnapshot.source_run_id == source_core_run_id,
            StockFeatureSnapshotRun.id == source_core_run_id,
            StockFeatureSnapshotRun.trade_date == trade_date,
            StockFeatureSnapshotRun.status == _SNAPSHOT_RUN_CONSUMABLE_STATUS,
            StockFeatureSnapshotRun.published_at.isnot(None),
        )
    )
    rows = (await session.execute(stmt)).all()

    out: dict[str, dict[str, object]] = {}
    for instrument_id, flat in rows:
        if not isinstance(flat, dict):
            continue
        facts: dict[str, object] = {}
        for attr, flat_key in _CURRENT_ONLY_SNAPSHOT_FIELDS.items():
            if flat_key in flat:
                facts[attr] = flat[flat_key]
        # Slice 4A1R — Board current-state capability runtime source (SINGLE OWNER).
        #
        # The 9 migrated Board current-state capabilities MUST consume the exact-T
        # ``first_pyramid_flat`` that the Board producer consumes, NOT the History
        # ``previous_state_to_flat`` output (which emits only a partial ``fp_*``
        # subset and would silently degrade the migrated facts to None/0).
        #
        # This loader already holds that exact payload, so we carry it whole here
        # instead of adding a second DB query.  ``build_member_observation_from_facts``
        # is the only consumer; there is NO fallback to ``raw.flat_t``.
        facts[_BOARD_CURRENT_FLAT_KEY] = flat
        if facts:
            out[str(instrument_id)] = facts
    return out


async def _load_batch_instrument_board_meta(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
) -> dict[str, dict[str, Any]]:
    """Slice 4A2 union-level Instrument metadata loader (single batch query).

    Replicates the legacy Board producer universe gate
    ``_is_instrument_valid_for_aggregation`` == ``Instrument.status == "active"``
    EXACTLY — no extra rules (no `.ST` / `.退` / delisted-suffix checks) — and
    additionally carries the ``symbol`` so the migrated Board capability can emit
    ``leader_symbol`` (which must be the instrument symbol, NOT the member_id
    UUID).  A single ``SELECT Instrument.id, Instrument.status, Instrument.symbol``
    over the union member set (no N+1, no second query).

    The result gates the migrated Board current-state capability universe ONLY;
    it does NOT change the existing Review price / transition / historical /
    participation universes.  ``build_member_observation_from_facts`` reads it as
    the current-only facts ``_BOARD_CURRENT_ELIGIBLE_KEY`` and
    ``_BOARD_CURRENT_SYMBOL_KEY``.

    Returns ``{str(instrument_id): {"eligible": bool, "symbol": str}}`` for all
    requested ids that exist in the table.
    """
    if not instrument_ids:
        return {}
    stmt = select(
        Instrument.id, Instrument.status, Instrument.symbol
    ).where(Instrument.id.in_(instrument_ids))
    rows = (await session.execute(stmt)).all()
    return {
        str(iid): {"eligible": (status == "active"), "symbol": symbol}
        for iid, status, symbol in rows
    }


@dataclass(frozen=True)
class PreparedScope:
    """Prepared canonical inputs for one scope/date observation."""

    scope_type: str
    scope_key: str
    scope_name: str
    trade_date: date
    canonical_t1: date | None
    pit_member_ids: tuple[str, ...]
    pit_member_ids_t1: tuple[str, ...]
    members: tuple[MemberObservation, ...]
    t1_membership_available: bool
    pit_status_t: str
    pit_status_t1: str
    diagnostics: tuple[str, ...]
    # ROUND-2.2B: REQUIRED (no default) — exact-T Event Coverage members for this
    # scope/date.  ``None`` = coverage source unavailable (structure-events
    # unavailable); ``tuple(...)`` = valid coverage (possibly empty = legal
    # zero-event day).  Every PreparedScope constructor MUST decide explicitly.
    event_coverage_member_ids: tuple[str, ...] | None
    events: tuple[StructureEvent, ...] = ()
    # Slice 4A3 — Board Event Freshness migration.  The board-ready universe's
    # immutable FP event history over ``[T-20, T]`` as an ordered stream of
    # ``(event_type, event_time_iso)`` pairs, sliced per scope by board-ready
    # member IDs in the union core (NO per-scope SQL).  Empty when the freshness
    # history is not carried (replay / historical path).
    freshness_events: tuple[tuple[str, str], ...] = ()


async def list_recent_trading_days(
    session: AsyncSession,
    end_date: date,
    n: int,
) -> list[date]:
    """Return the ``n`` most recent complete A-share trading days <= ``end_date``."""
    from app.models.calendar import TradingCalendar

    rows = (
        await session.execute(
            select(TradingCalendar.trade_date)
            .where(
                TradingCalendar.trade_date <= end_date,
                TradingCalendar.is_trading_day.is_(True),
                TradingCalendar.market == "A",
            )
            .order_by(TradingCalendar.trade_date.desc())
            .limit(n)
        )
    ).scalars()
    return list(rows)


def _build_member_observations(
    pit_ids_t: list[uuid.UUID],
    *,
    trade_date: date,
    t1: date | None,
    states_t: dict[uuid.UUID, dict],
    states_t1: dict[uuid.UUID, dict],
    bars: dict[uuid.UUID, _InstrumentBarSeries],
    current_only_facts: dict[str, dict[str, object]],
    vec_volume: dict[uuid.UUID, _VectorizedMemberVolume] | None = None,
    counters: dict[str, int] | None = None,
    fallback_reasons: list[str] | None = None,
) -> list[MemberObservation]:
    """Build canonical ``MemberObservation`` inputs — the single member-construction
    owner shared by every batch / union replay path.

    ``counters`` / ``fallback_reasons`` are optional OUT parameters populated by the
    batch path for rules/25 §8.7 physical-cost instrumentation: ``counters["vec_hit"]``
    increments when a member resolves its VolumeContext from the precomputed vectorized
    series (no strict-prior window materialization); ``counters["vec_fallback"]``
    increments and ``fallback_reasons`` records the first reason when it falls back to
    the canonical window owner (which needs the strict-prior history window).  They
    never affect the constructed ``MemberObservation`` — this is pure observability.
    """
    members: list[MemberObservation] = []
    for inst_id in pit_ids_t:
        # ROUND-2 GAP-L1-MEMBER-GATE FIX: a PIT member EXISTS by virtue of being
        # in the PIT membership — NOT because it has a daily_state.  When the
        # daily_state is missing (state_t is None), the member is still built; the
        # state-derived fact families (Trend / Structure State / Momentum /
        # state-driven Volume) become unavailable, while bars-driven Price and
        # snapshot-driven Current-only facts stay available (they have their own
        # independent sources).  Previously this ``continue`` dropped the whole
        # member, so bars/snapshots that existed could never form Price /
        # Current-only on a state-less day (proven on 2026-08-17).
        state_t = states_t.get(inst_id)
        series = bars.get(inst_id)
        current = series.exact_bar(trade_date) if series else None
        t1_bar = series.exact_bar(t1) if (series and t1) else None
        # [CHANGE-20260826-001 Slice 1 CORRECTION] Current(T) First Pyramid facts
        # are owned by published Core(T), NOT History(T).  When the Core snapshot
        # flat is present (daily current path), build flat_t / continuous from it;
        # otherwise fall back to History(T) state (historical replay / union paths
        # where Core(T) is not the observation target).  This makes Review(T)
        # Current result independent of whether History(T) exists (KPI-1/KPI-2).
        current_only = current_only_facts.get(str(inst_id))
        core_flat = (
            current_only.get(_BOARD_CURRENT_FLAT_KEY)
            if current_only is not None
            else None
        )
        if core_flat is not None:
            flat_t = snapshot_flat_to_flat_t(core_flat)
            continuous = snapshot_flat_to_continuous(core_flat)
        else:
            flat_t = previous_state_to_flat(state_t)
            continuous = state_to_continuous(state_t)
        raw = RawMemberFacts(
            member_id=str(inst_id),
            flat_t=flat_t,
            close_t=current.close if current else None,
            amount_t=current.amount if current else None,
            volume_t=current.volume if current else None,
            flat_t1=(
                previous_state_to_flat(states_t1[inst_id]) if inst_id in states_t1 else None
            ),
            close_t1=t1_bar.close if t1_bar else None,
            # Continuous Trend/Structure/Momentum/Volume facts (PRD §7.3-§7.6)
            # that the history state_payload carries but previous_state_to_flat
            # does not surface. Additive passthrough; missing -> None.
            continuous=continuous,
            # Current-only canonical facts from the exact-T snapshot.  These
            # have no member-day history series; Historical Dynamics stays
            # unavailable while Current is served (PRD v2.3).
            current_only=current_only_facts.get(str(inst_id)),
        )
        vv = vec_volume.get(inst_id) if vec_volume else None
        if vv is not None:
            # Index the precomputed series at the member's last finite-volume bar
            # <= T.  The row is canonical-equivalent to the per-date compact-array
            # row for T per-window, not per full-200 gate:
            #   * 20D windows (MA20/ratio20/zscore20/percentile20, INCLUDE current
            #     bar) are contained in [T-400d, T] iff ``w >= SHORT_WINDOW``;
            #   * 200D windows are contained iff ``w >= LONG_WINDOW``.  When
            #     ``w < LONG_WINDOW`` the canonical owner yields unavailable 200D
            #     facts (MA200 min_periods not met), and the vectorized series
            #     yields the SAME unavailable row as long as the member has fewer
            #     than LONG_WINDOW finite-volume bars up to ``hi`` in the batch
            #     window (``hi < LONG_WINDOW - 1``) — i.e. neither path ever has a
            #     200-bar window to drift on.  This keeps the vectorized path
            #     engaged for real data whose history is shorter than 200 bars.
            hi = bisect_right(vv.dates, trade_date) - 1
            if hi >= 0:
                lo = bisect_left(vv.dates, trade_date - timedelta(days=_BAR_LOOKBACK_DAYS))
                w = hi - lo + 1
                if w >= SHORT_WINDOW and (w >= LONG_WINDOW or hi < LONG_WINDOW - 1):
                    if counters is not None:
                        counters["vec_hit"] = counters.get("vec_hit", 0) + 1
                    members.append(
                        build_member_observation_from_facts(
                            raw, vectorized_context_at(vv.context, hi)
                        )
                    )
                    continue
        # Canonical per-date owner (oracle) — window-bound edge case (fewer than
        # LONG_WINDOW finite-volume bars in the 400d lookback, or no finite-volume
        # bar <= T).  It needs the strict-prior history, so build it now.
        if counters is not None:
            counters["vec_fallback"] = counters.get("vec_fallback", 0) + 1
            if fallback_reasons is not None:
                if vv is None:
                    reason = "no_finite_volume" if series is not None else "no_bars"
                else:
                    reason = "w_insufficient"
                if reason not in fallback_reasons:
                    fallback_reasons.append(reason)
        prior_bars = (
            [b for b in series.window(trade_date) if b.trade_date != trade_date]
            if series
            else []
        )
        raw = replace(
            raw,
            volume_history=tuple(b.volume for b in prior_bars),
            amount_history=tuple(b.amount for b in prior_bars),
        )
        members.append(build_member_observation(raw))
    return members


# ----------------------------------------------------------------------------
# BATCH historical reconstruction: ONE bulk read of the whole member x date
# window, then replay per T (no per-date reload).  The batch loaders are the
# canonical SQL owners; the replay reproduces exactly the per-date windows so
# the resulting PreparedScope is byte-identical to a hypothetical per-date path.
#
# Vectorized preprocessing: each member's VolumeContext series is computed ONCE
# across the whole window with the numpy owner (``compute_volume_context_vectorized``),
# then the per-T replay only indexes the precomputed row — the pandas rolling /
# percentile work that a per-date path would repeat for every (member, T) is done
# a single time per member.  The canonical batch window remains the oracle and is
# used as fallback for the window-bound edge case (see ``_build_member_observations``).
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _VectorizedMemberVolume:
    """Precomputed per-member VolumeContext over the whole batch bar window.

    ``dates`` / ``volumes`` are the member's finite-volume daily bars across
    ``[first-400d, last]`` ascending (the batch window); ``context`` is the
    vectorized VolumeContext series with one row per volume (row ``i`` == the
    canonical series row for the bar at ``dates[i]``).  The per-T row is resolved
    by index; a T whose ``[T-400d, T]`` window holds fewer than ``LONG_WINDOW``
    finite-volume bars falls back to the canonical per-date owner (window-bound
    equivalence, no semantic drift).
    """

    dates: tuple[date, ...]
    volumes: np.ndarray
    context: VectorizedVolumeContext


def _precompute_vectorized_volume(
    bars: dict[uuid.UUID, _InstrumentBarSeries],
) -> dict[uuid.UUID, _VectorizedMemberVolume]:
    """Compute the whole-member vectorized VolumeContext series once per member.

    Members with no finite-volume bar are simply absent (their facts stay
    unavailable, matching the canonical owner).  Missing / non-finite volume is
    filtered (mirroring the canonical ``_finite`` compact-array construction),
    so ``dates`` / ``volumes`` reproduce the canonical finite-only array exactly.
    """
    out: dict[uuid.UUID, _VectorizedMemberVolume] = {}
    for inst_id, series in bars.items():
        vols: list[float] = []
        dates: list[date] = []
        for fact in series.facts:
            if fact.volume is None:
                continue
            value = float(fact.volume)
            if not np.isfinite(value):
                # Mirror the canonical finite-only compact array: non-finite
                # volume is unavailable, not a real bar.
                continue
            vols.append(value)
            dates.append(fact.trade_date)
        if not vols:
            continue
        arr = np.asarray(vols, dtype=float)
        context = compute_volume_context_vectorized(arr)
        out[inst_id] = _VectorizedMemberVolume(
            dates=tuple(dates),
            volumes=arr,
            context=context,
        )
    return out


@dataclass(frozen=True)
class _InstrumentBarSeries:
    """Ascending per-instrument daily bar facts for the whole batch window."""

    facts: tuple[DailyBarFact, ...]
    dates: tuple[date, ...]

    def window(self, hi: date, lo_days: int = _BAR_LOOKBACK_DAYS) -> list[DailyBarFact]:
        """Facts with ``hi - lo_days <= trade_date <= hi`` (ascending), reproducing
        the canonical ``[hi-400d, hi]`` replay window for ``hi`` exactly."""
        lo = hi - timedelta(days=lo_days)
        start = bisect_left(self.dates, lo)
        end = bisect_right(self.dates, hi)
        return list(self.facts[start:end])

    def last_bar(self, hi: date, lo_days: int = _BAR_LOOKBACK_DAYS) -> DailyBarFact | None:
        """The single last fact with ``trade_date <= hi`` (or None), O(log n).

        Used by the vectorized fast path so the 400-bar window is NOT materialized
        per member per replay date — only the T-row bar is needed there.
        """
        lo = hi - timedelta(days=lo_days)
        start = bisect_left(self.dates, lo)
        end = bisect_right(self.dates, hi)
        return self.facts[end - 1] if end > start else None

    def exact_bar(self, target: date) -> DailyBarFact | None:
        """The single fact whose ``trade_date`` exactly equals ``target``, or None.

        EXACT canonical match only: if ``target`` has no bar (e.g. the instrument is
        suspended), None is returned and callers MUST NOT fall back to an earlier bar
        (T-2/T-3).  This preserves the canonical exact-T / exact-T1 contract of
        ``close_t1`` while staying O(log n) and lazy (no window materialization).
        """
        idx = bisect_left(self.dates, target)
        if idx < len(self.dates) and self.dates[idx] == target:
            return self.facts[idx]
        return None


def _build_t1_map(
    trade_dates: Sequence[date],
    trading_days: Sequence[date],
) -> dict[date, date | None]:
    """Map each requested date to its strictly-lower trading-day predecessor.

    Same predicates as ``calendar_service.get_previous_trading_day_async``
    (strictly < ref_date, is_trading_day, market A) over the caller-supplied
    ``trading_days`` window.
    """
    days = sorted(trading_days)
    out: dict[date, date | None] = {}
    for t in trade_dates:
        idx = bisect_left(days, t)
        out[t] = days[idx - 1] if idx > 0 else None
    return out


async def _load_batch_calendar(
    session: AsyncSession,
    trade_dates: list[date],
) -> dict[date, date | None]:
    """All A-market trading days across ``[first-400d, last]`` once; map each
    requested date to its exact canonical T-1 (single query for the whole series)."""
    if not trade_dates:
        return {}
    first, last = trade_dates[0], trade_dates[-1]
    rows = (
        await session.execute(
            select(TradingCalendar.trade_date)
            .where(
                TradingCalendar.trade_date >= first - timedelta(days=_BAR_LOOKBACK_DAYS),
                TradingCalendar.trade_date <= last,
                TradingCalendar.is_trading_day.is_(True),
                TradingCalendar.market == "A",
            )
            .order_by(TradingCalendar.trade_date.asc())
        )
    ).scalars()
    return _build_t1_map(trade_dates, list(rows))


async def _load_batch_states(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_dates: list[date],
    t1_by_date: dict[date, date | None],
) -> dict[date, dict[uuid.UUID, dict]]:
    """First Pyramid daily_state payloads for every requested T and its exact T-1
    (one query), grouped by trade_date -> {instrument_id: state_payload}.

    PERF-IO-1-REVERT: the SQL-side ``jsonb_build_object`` key projection is
    removed.  On the server (where Review actually runs) there is no network
    transfer to save, and per-row JSONB construction in SQL costs more than the
    full payload that is already local (measured ~2.4x slower).  The loader keeps
    a plain 3-column projection (``instrument_id`` / ``trade_date`` /
    ``state_payload``) so ORM hydration of the full row is still avoided, while
    the canonical semantic mapping (``previous_state_to_flat`` /
    ``state_to_continuous``) reads the full ``state_payload`` exactly as before.
    """
    if not instrument_ids or not trade_dates:
        return {}
    dates = set(trade_dates)
    dates.update(d for d in t1_by_date.values() if d is not None)
    stmt = select(
        FirstPyramidHistoryDailyState.instrument_id,
        FirstPyramidHistoryDailyState.trade_date,
        FirstPyramidHistoryDailyState.state_payload,
    ).where(
        FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
        FirstPyramidHistoryDailyState.trade_date.in_(sorted(dates)),
        FirstPyramidHistoryDailyState.algorithm_version == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
    )
    out: dict[date, dict[uuid.UUID, dict]] = defaultdict(dict)
    for row in (await session.execute(stmt)).all():
        out[row.trade_date][row.instrument_id] = row.state_payload
    return dict(out)


async def _load_batch_bars(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_dates: list[date],
) -> dict[uuid.UUID, _InstrumentBarSeries]:
    """All daily bars across ``[first-400d, last]`` once (one query), grouped per
    instrument ascending.  Per-date windows are sliced in memory at replay.

    PERF-IO-2: only the columns ``DailyBarFact`` consumes (OHLCV + amount) are
    selected — the full ``BarDaily`` ORM row (incl. ``adj_factor``) is never
    hydrated, which is the largest measured I/O cost on the server.  Pure physical
    projection: ``DailyBarFact.from_row`` reads the same fields from the projected
    Row as from the full model.
    """
    if not instrument_ids or not trade_dates:
        return {}
    first, last = trade_dates[0], trade_dates[-1]
    stmt = (
        select(
            BarDaily.instrument_id,
            BarDaily.trade_date,
            BarDaily.open,
            BarDaily.high,
            BarDaily.low,
            BarDaily.close,
            BarDaily.volume,
            BarDaily.amount,
        )
        .where(
            BarDaily.instrument_id.in_(instrument_ids),
            BarDaily.trade_date >= first - timedelta(days=_BAR_LOOKBACK_DAYS),
            BarDaily.trade_date <= last,
        )
        .order_by(BarDaily.instrument_id.asc(), BarDaily.trade_date.asc())
    )
    by_instrument: dict[uuid.UUID, list[DailyBarFact]] = defaultdict(list)
    for bar in (await session.execute(stmt)).all():
        by_instrument[bar.instrument_id].append(DailyBarFact.from_row(bar))
    out: dict[uuid.UUID, _InstrumentBarSeries] = {}
    for inst, facts in by_instrument.items():
        out[inst] = _InstrumentBarSeries(
            facts=tuple(facts),
            dates=tuple(f.trade_date for f in facts),
        )
    return out


async def _load_batch_events(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_dates: list[date],
) -> dict[date, list[StructureEvent]]:
    """Canonical immutable FP structure events across ``[first, last+1d)`` once
    (one query), grouped by the T prefix of ``event_time`` — identical membership
    to the per-date ``event_time.startswith(T.isoformat())`` filter."""
    if not instrument_ids or not trade_dates:
        return {}
    first, last = trade_dates[0], trade_dates[-1]
    after_last = last + timedelta(days=1)
    stmt = select(FirstPyramidHistoryEvent).where(
        FirstPyramidHistoryEvent.instrument_id.in_(instrument_ids),
        FirstPyramidHistoryEvent.algorithm_version == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        FirstPyramidHistoryEvent.history_contract_version == HISTORY_CONTRACT_VERSION,
        FirstPyramidHistoryEvent.event_time >= first.isoformat(),
        FirstPyramidHistoryEvent.event_time < after_last.isoformat(),
    )
    grouped: dict[str, list[StructureEvent]] = defaultdict(list)
    for row in (await session.execute(stmt)).scalars():
        payload = row.event_payload or {}
        event_time = row.event_time
        if not event_time:
            # The query bounds ``event_time`` to [first, last+1d), so a NULL here
            # would be a data anomaly; skip rather than crash or mis-group.
            continue
        grouped[event_time[:10]].append(
            _map_structure_event(
                instrument_id=str(row.instrument_id),
                event_type=row.event_type,
                direction=payload.get("direction"),
                level=payload.get("level"),
                internal=payload.get("internal"),
                release_volume_ratio=payload.get("release_volume_ratio"),
            )
        )
    return {
        date.fromisoformat(prefix): events
        for prefix, events in grouped.items()
        if date.fromisoformat(prefix) in set(trade_dates)
    }


async def _load_batch_freshness_events(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_date: date,
) -> dict[str, list[tuple[str, str]]]:
    """Union-level immutable FP event history over ``[T-20, T]`` — ONE query.

    Exact replication of the Board ``_compute_freshness_density`` query bounds:
    ``event_time >= (T - 20d).isoformat()`` with the canonical history contract
    (``HISTORY_CONTRACT_VERSION``) and NO upper bound — the aggregation Core
    skips future events via ``days_ago < 0``, exactly like the Board loop.
    Grouped per member (string UUID to match ``MemberObservation.member_id``) so
    the union core slices per-scope by board-ready IDs with NO per-scope SQL.

    Filters match the Board query EXACTLY: ONLY ``history_contract_version``
    (per the migration spec "只使用 FirstPyramidHistoryEvent 并且
    history_contract_version == review-history-v2").  Do NOT add an
    ``algorithm_version`` filter here — the Board freshness producer has none,
    and adding one would silently drop events the Board counts.

    Event types are kept RAW (no ``_normalize_event_type`` casing normalization):
    the Board freshness producer feeds ``row.event_type`` straight into
    ``_event_dimension``, so normalizing here would break field-level parity.
    """
    if not instrument_ids:
        return {}
    start_iso = (trade_date - timedelta(days=20)).isoformat()
    stmt = select(
        FirstPyramidHistoryEvent.instrument_id,
        FirstPyramidHistoryEvent.event_type,
        FirstPyramidHistoryEvent.event_time,
    ).where(
        FirstPyramidHistoryEvent.instrument_id.in_(instrument_ids),
        FirstPyramidHistoryEvent.history_contract_version == HISTORY_CONTRACT_VERSION,
        FirstPyramidHistoryEvent.event_time.isnot(None),
        FirstPyramidHistoryEvent.event_time >= start_iso,
    )
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in (await session.execute(stmt)).all():
        # ``compute_freshness`` handles ``""`` exactly like ``None`` (falls
        # through to structure), so NULL event_type is safe here.
        out[str(row.instrument_id)].append(
            (row.event_type or "", row.event_time)
        )
    # Deterministic per-member order (the Board has no ORDER BY; we sort by
    # time then type so production output is reproducible).
    for key in out:
        out[key].sort(key=lambda ev: (ev[1], ev[0]))
    return dict(out)


# ---------------------------------------------------------------------------
# ROUND-2.2B — Conservative canonical-backfill Event Coverage (single owner)
# ---------------------------------------------------------------------------
# An exact-T DailyState proves Event lifecycle coverage iff (12-condition contract):
#   1. exact-T FirstPyramidHistoryDailyState exists
#   2. DailyState.algorithm_version == FIRST_PYRAMID_CORE_ALGORITHM_VERSION
#   3. DailyState.history_contract_version == HISTORY_CONTRACT_VERSION
#   4. DailyState.source_history_run_id IS NOT NULL
#   5. matching FirstPyramidHistoryRun exists (source_history_run_id)
#   6. Run.algorithm_version == FIRST_PYRAMID_CORE_ALGORITHM_VERSION
#   7. Run.status IN ('partial','succeeded')
#   8. Run.completed_at IS NOT NULL
#   9. matching FirstPyramidHistoryRunItem (run_id + instrument_id) exists
#  10. RunItem.status == 'succeeded'
#  11. RunItem.completed_at IS NOT NULL
#  12. DailyState.updated_at <= Run.completed_at   (excludes post-backfill state-only
#                                                  advancement, e.g. 08-10)
# This is the CURRENT Review conservative canonical-backfill proof; it does NOT claim
# to support future lifecycle modes.  NO date hardcode anywhere.


async def _load_batch_backfill_event_coverage(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_dates: list[date],
) -> dict[date, frozenset[uuid.UUID]]:
    """Batch coverage for all ``trade_dates`` in ONE query (no per-date SQL).

    Returns a dict {trade_date: frozenset[UUID]}.  A date absent from the dict has
    NO coverage entry -> its structure-events source is unavailable (caller must not
    fabricate it).  This avoids an N-date query explosion in the replay path.
    """
    if not instrument_ids or not trade_dates:
        return {}
    stmt = (
        select(
            FirstPyramidHistoryDailyState.trade_date,
            FirstPyramidHistoryDailyState.instrument_id,
        )
        .join(
            FirstPyramidHistoryRun,
            FirstPyramidHistoryDailyState.source_history_run_id == FirstPyramidHistoryRun.id,
        )
        .join(
            FirstPyramidHistoryRunItem,
            (FirstPyramidHistoryRunItem.history_run_id == FirstPyramidHistoryRun.id)
            & (FirstPyramidHistoryRunItem.instrument_id == FirstPyramidHistoryDailyState.instrument_id),
        )
        .where(
            FirstPyramidHistoryDailyState.trade_date.in_(trade_dates),
            FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
            FirstPyramidHistoryDailyState.algorithm_version == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            FirstPyramidHistoryDailyState.history_contract_version == HISTORY_CONTRACT_VERSION,
            FirstPyramidHistoryDailyState.source_history_run_id.isnot(None),
            FirstPyramidHistoryRun.algorithm_version == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            FirstPyramidHistoryRun.status.in_(["partial", "succeeded"]),
            FirstPyramidHistoryRun.completed_at.isnot(None),
            FirstPyramidHistoryRunItem.status == "succeeded",
            FirstPyramidHistoryRunItem.completed_at.isnot(None),
            FirstPyramidHistoryDailyState.updated_at <= FirstPyramidHistoryRun.completed_at,
        )
    )
    by_date: dict[date, set[uuid.UUID]] = defaultdict(set)
    for d, instrument_id in (await session.execute(stmt)).all():
        by_date[d].add(uuid.UUID(str(instrument_id)))
    return {d: frozenset(s) for d, s in by_date.items()}


@dataclass(frozen=True)
class _UnionFactContext:
    """Shared, loaded-once fact context for a union of member_ids.

    Built by :func:`prepare_union_fact_context` and sliced per-scope by
    :func:`prepare_scopes_from_union`.  This is the storage layer behind
    PERF-2: the same ``bars`` / ``states`` / ``events`` / ``vec_volume`` are
    loaded once for a union of members and reused across N scopes that share
    members (e.g. one stock belonging to many concept boards), instead of
    re-loading the whole member x date window per scope.
    """

    t1_by_date: dict[date, date | None]
    states_by_date: dict[date, dict[uuid.UUID, dict]]
    bars: dict[uuid.UUID, _InstrumentBarSeries]
    events_by_date: dict[date, list[StructureEvent]]
    vec_volume: dict[uuid.UUID, _VectorizedMemberVolume]
    # Slice 4A3 — Board Event Freshness migration.  Immutable FP event history
    # over ``[T-20, T]`` keyed by string member UUID, loaded ONCE at union level
    # (one batch read) and sliced per scope by board-ready IDs in the union core.
    freshness_events_by_member: dict[str, list[tuple[str, str]]] = field(
        default_factory=dict
    )
    # PERF-OOM (2026-08-24 closure): when the union is prepared chunked, the heavy
    # ``bars`` / ``vec_volume`` are released after each chunk and the already-built
    # compact ``MemberObservation`` list is carried here instead.  ``None`` => the
    # non-chunked path still owns ``bars`` / ``vec_volume`` (oracle, unchanged).
    prebuilt_members_by_date: dict[date, list[MemberObservation]] | None = field(
        default=None
    )


def build_union_fact_context_from_loaded_facts(
    *,
    t1_by_date: dict[date, date | None],
    states_by_date: dict[date, dict[uuid.UUID, dict]],
    bars: dict[uuid.UUID, _InstrumentBarSeries],
    events_by_date: dict[date, list[StructureEvent]],
    freshness_events_by_member: dict[str, list[tuple[str, str]]] | None = None,
) -> _UnionFactContext:
    """Dataset loaded-facts -> production ``_UnionFactContext`` (R0 Replay Adapter).

    This is the SINGLE entry point that turns already-loaded source facts
    (calendar T-1 map, FP daily states, bars, FP structure events) into the same
    ``_UnionFactContext`` consumed by :func:`prepare_scopes_from_union`.  The DB
    path builds the same context via :func:`prepare_union_fact_context` (which
    loads from PostgreSQL); the Dataset path builds it here from frozen source
    facts.  Both then feed the SAME production preparation core.

    The only computation performed here is the shared vectorized VolumeContext
    precompute (``_precompute_vectorized_volume``) — the same owner the DB path
    calls.  There is NO second set of business formulas: bars / states / events
    are passed in already mapped by the shared source-fact mappers.
    """
    return _UnionFactContext(
        t1_by_date=t1_by_date,
        states_by_date=states_by_date,
        bars=bars,
        events_by_date=events_by_date,
        vec_volume=_precompute_vectorized_volume(bars),
        freshness_events_by_member=freshness_events_by_member or {},
    )


async def prepare_union_fact_context(
    session: AsyncSession,
    trade_dates: list[date],
    union_member_ids: list[uuid.UUID],
    *,
    prep_counters: dict[str, int] | None = None,
    prep_fallback_reasons: list[str] | None = None,
) -> _UnionFactContext:
    """Load the whole member x date window ONCE for a union of member_ids.

    Bulk-loading owner (calendar, FP states, bars, FP events, vectorized volume);
    the union of members is supplied by the caller so the cost is incurred
    exactly once even when many scopes share the same members.  Slicing per scope
    happens in :func:`prepare_scopes_from_union`.
    """
    if not trade_dates or not union_member_ids:
        return _UnionFactContext(
            t1_by_date={},
            states_by_date={},
            bars={},
            events_by_date={},
            vec_volume={},
        )
    import time

    t0 = time.perf_counter()
    t1_by_date = await _load_batch_calendar(session, trade_dates)
    cal_ms = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter()
    states_by_date = await _load_batch_states(
        session, union_member_ids, trade_dates, t1_by_date
    )
    states_ms = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter()
    bars = await _load_batch_bars(session, union_member_ids, trade_dates)
    bars_ms = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter()
    events_by_date = await _load_batch_events(session, union_member_ids, trade_dates)
    events_ms = (time.perf_counter() - t0) * 1000.0
    # Slice 4A3 — Board Event Freshness: ONE union-level batch read of the
    # immutable ``[T-20, T]`` event history (never per-scope SQL).  T is the last
    # date of the window, matching the Board's ``trade_date`` argument.
    t0 = time.perf_counter()
    freshness_events_by_member = await _load_batch_freshness_events(
        session, union_member_ids, trade_dates[-1],
    )
    freshness_ms = (time.perf_counter() - t0) * 1000.0
    t_vec = time.perf_counter()
    vec_volume = _precompute_vectorized_volume(bars)
    vec_ms = (time.perf_counter() - t_vec) * 1000.0
    # VEC-1: counters are populated by the per-date union build in
    # ``prepare_scopes_from_union``, not per-scope.  Feed-through is implicit
    # via the shared dict reference.
    logger.info(
        "[union-fact-context] union_member_count=%d trade_date_count=%d "
        "cal_ms=%.1f states_ms=%.1f bars_ms=%.1f events_ms=%.1f "
        "freshness_ms=%.1f vec_precompute_ms=%.1f",
        len(union_member_ids), len(trade_dates),
        cal_ms, states_ms, bars_ms, events_ms, freshness_ms, vec_ms,
    )
    return _UnionFactContext(
        t1_by_date=t1_by_date,
        states_by_date=states_by_date,
        bars=bars,
        events_by_date=events_by_date,
        vec_volume=vec_volume,
        freshness_events_by_member=freshness_events_by_member,
    )


# PERF-OOM (2026-08-24 closure): initial chunk size for the bar/vector path.
# The non-chunked loader held the ENTIRE union (~5286 members x 400-day bars) AND
# the full ``VectorizedVolumeContext`` simultaneously, peaking ~3.95 GiB and being
# OOM-killed at the 4 GiB container ceiling.  Chunking bounds peak memory to
# ~one chunk of bars + vectors while the canonical union-first architecture and
# every business formula are preserved (``_load_batch_bars`` / ``_precompute_
# vectorized_volume`` / ``_build_member_observations`` are the SAME owners; only
# their lifetime is bounded per chunk).  Output is semantically identical.
_REVIEW_PREP_CHUNK_SIZE = 500


async def prepare_union_fact_context_chunked(
    session: AsyncSession,
    trade_dates: list[date],
    union_member_ids: list[uuid.UUID],
    *,
    chunk_size: int = _REVIEW_PREP_CHUNK_SIZE,
    current_only_facts_by_date: dict[date, dict] | None = None,
    prep_counters: dict[str, int] | None = None,
    prep_fallback_reasons: list[str] | None = None,
) -> _UnionFactContext:
    """Memory-bounded union fact context — chunked bar/vector load (PERF-OOM).

    Loads the compact maps (calendar T-1, FP states, FP events, board freshness)
    ONCE (unchanged), then loads bars + precomputes the vectorized volume in
    chunks of ``chunk_size`` members.  For each chunk the canonical
    ``MemberObservation`` list is built per trade_date and appended to
    ``prebuilt_members_by_date``; the chunk's heavy ``bars`` / ``vec_volume`` are
    then released before the next chunk.  Peak memory is thus bounded by one
    chunk rather than the whole union.

    The returned ``_UnionFactContext`` carries ``prebuilt_members_by_date`` and
    empty ``bars`` / ``vec_volume``; :func:`build_prepared_scopes_from_union`
    reuses the prebuilt members verbatim, producing output identical to the
    non-chunked oracle.
    """
    import time

    _log_rss(
        "union-prep-start",
        union_member_count=len(union_member_ids),
        trade_date_count=len(trade_dates) if trade_dates else 0,
    )
    # Compact maps: loaded once, held for the whole prep (small footprint).
    t0 = time.perf_counter()
    t1_by_date = await _load_batch_calendar(session, trade_dates)
    cal_ms = (time.perf_counter() - t0) * 1000.0
    _log_rss("union-prep-calendar", union_member_count=len(union_member_ids),
             trade_date_count=len(trade_dates) if trade_dates else 0)

    t0 = time.perf_counter()
    states_by_date = await _load_batch_states(
        session, union_member_ids, trade_dates, t1_by_date
    )
    states_ms = (time.perf_counter() - t0) * 1000.0
    state_rows = sum(len(v) for v in states_by_date.values())
    _log_rss("union-prep-states", union_member_count=len(union_member_ids),
             trade_date_count=len(trade_dates) if trade_dates else 0,
             state_rows=state_rows)

    t0 = time.perf_counter()
    events_by_date = await _load_batch_events(session, union_member_ids, trade_dates)
    events_ms = (time.perf_counter() - t0) * 1000.0
    event_rows = sum(len(v) for v in events_by_date.values())
    _log_rss("union-prep-events", union_member_count=len(union_member_ids),
             trade_date_count=len(trade_dates) if trade_dates else 0,
             event_rows=event_rows)

    t0 = time.perf_counter()
    freshness_events_by_member = await _load_batch_freshness_events(
        session, union_member_ids, trade_dates[-1],
    )
    freshness_ms = (time.perf_counter() - t0) * 1000.0
    _log_rss("union-prep-freshness", union_member_count=len(union_member_ids),
             trade_date_count=len(trade_dates) if trade_dates else 0)

    # Chunked bar + vector precompute.  ``states_by_date`` is keyed by ALL union
    # members, so per-chunk state masks are sliced from it (same lookup the
    # non-chunked path does per member).
    prebuilt: dict[date, list[MemberObservation]] = {t: [] for t in trade_dates}
    total_chunks = (len(union_member_ids) + chunk_size - 1) // chunk_size if union_member_ids else 0
    for chunk_idx in range(total_chunks):
        chunk = union_member_ids[chunk_idx * chunk_size:(chunk_idx + 1) * chunk_size]
        t0 = time.perf_counter()
        bars = await _load_batch_bars(session, chunk, trade_dates)
        bars_ms = (time.perf_counter() - t0) * 1000.0
        bar_rows = sum(len(s.facts) for s in bars.values())
        _log_rss(
            "union-prep-bars-chunk",
            union_member_count=len(chunk),
            trade_date_count=len(trade_dates) if trade_dates else 0,
            bar_rows=bar_rows,
        )
        t_vec = time.perf_counter()
        vec_volume = _precompute_vectorized_volume(bars)
        vec_ms = (time.perf_counter() - t_vec) * 1000.0
        _log_rss(
            "union-prep-vector-chunk",
            union_member_count=len(chunk),
            trade_date_count=len(trade_dates) if trade_dates else 0,
            vector_member_count=len(vec_volume),
        )
        for t in trade_dates:
            states_t = states_by_date.get(t, {})
            t1 = t1_by_date.get(t)
            states_t1 = states_by_date.get(t1, {}) if t1 is not None else {}
            chunk_members = _build_member_observations(
                chunk,
                trade_date=t,
                t1=t1,
                states_t={mid: states_t.get(mid) for mid in chunk},
                states_t1={mid: states_t1.get(mid) for mid in chunk},
                bars=bars,
                current_only_facts=(
                    current_only_facts_by_date.get(t, {})
                    if current_only_facts_by_date
                    else {}
                ),
                vec_volume=vec_volume,
                counters=prep_counters,
                fallback_reasons=prep_fallback_reasons,
            )
            prebuilt[t].extend(chunk_members)
        # Release the chunk's heavy objects before loading the next chunk.
        del bars
        del vec_volume
        gc.collect()
        _log_rss(
            "union-prep-chunk-released",
            union_member_count=len(chunk),
            trade_date_count=len(trade_dates) if trade_dates else 0,
        )
    _log_rss(
        "union-prep-done",
        union_member_count=len(union_member_ids),
        trade_date_count=len(trade_dates) if trade_dates else 0,
    )
    logger.info(
        "[union-fact-context-chunked] union_member_count=%d trade_date_count=%d "
        "chunks=%d cal_ms=%.1f states_ms=%.1f events_ms=%.1f freshness_ms=%.1f",
        len(union_member_ids), len(trade_dates) if trade_dates else 0,
        total_chunks, cal_ms, states_ms, events_ms, freshness_ms,
    )
    return _UnionFactContext(
        t1_by_date=t1_by_date,
        states_by_date=states_by_date,
        bars={},
        events_by_date=events_by_date,
        vec_volume={},
        freshness_events_by_member=freshness_events_by_member,
        prebuilt_members_by_date=prebuilt,
    )


@dataclass(frozen=True)
class ScopeReplaySpec:
    """One scope to replay from a shared union fact context (R0-B).

    Carries its OWN ``scope_type`` so a single union build can serve a
    mixed-family view (e.g. concept + industry_l1/l2/l3 in dev_500 /
    capacity_4096), unlike the DB wrapper which is single-``scope_type``.
    """

    scope_type: str
    scope_key: str
    scope_name: str
    member_ids: tuple[uuid.UUID, ...]


def build_prepared_scopes_from_union(
    *,
    trade_dates: list[date],
    scope_specs: Sequence[ScopeReplaySpec],
    union_ctx: _UnionFactContext,
    membership_t1_by_scope: dict[str, tuple[uuid.UUID, ...]] | None = None,
    current_only_facts_by_date: dict[date, dict[str, dict[str, object]]] | None = None,
    coverage_by_date: dict[date, frozenset[uuid.UUID]] | None = None,
    pit_status_t: str = "current_static",
    pit_status_t1: str = "current_static",
    t1_membership_available: bool = True,
    pit_status_by_scope: dict[str, str] | None = None,
    pit_status_t1_by_scope: dict[str, str] | None = None,
    t1_membership_available_by_scope: dict[str, bool] | None = None,
    diagnostics_by_scope: dict[str, tuple[str, ...]] | None = None,
    prep_counters: dict[str, int] | None = None,
    prep_fallback_reasons: list[str] | None = None,
    prebuilt_members_by_date: dict[date, list[MemberObservation]] | None = None,
) -> dict[str, list[PreparedScope]]:
    """PURE union preparation core: slice a shared ``_UnionFactContext`` into
    per-scope ``PreparedScope`` series (R0-B single production calculation owner).

    This is the ONE owner shared by the DB path and the Dataset Replay Adapter.
    The loop order is **date -> union member -> scope slice** (VEC-1): for every
    trade_date the canonical ``_build_member_observations`` owner runs ONCE over
    the union of member_ids, then each scope SELECTS the resulting immutable
    ``MemberObservation`` by reference in its own membership order.  A member
    shared by N scopes (e.g. one stock in many concept boards) is constructed
    once per date instead of N times, while the result for a single scope stays
    byte-identical to preparing that scope alone.

    Unlike the DB wrapper (which hard-codes current-static semantics), this core
    explicitly accepts:

    - ``membership_t1_by_scope``: per-scope T-1 membership (``scope_key ->
      member UUID tuple``).  When ``None``, T-1 == T (current-static, the
      Historical Dynamics exception).  For Current L1 PIT(T) the caller supplies
      the real PIT(T-1) set; T-1/Transition facts are only meaningful when this
      is not forged.
    - ``current_only_facts_by_date``: per-date Current-only snapshot facts
      (``{trade_date: {member_id: {attr: value}}}``).  When ``None``, all
      Current-only facts stay ``None`` (Historical Dynamics path).  For Current
      L1 the caller supplies them so Release Volume Ratio / BB / VWAP / Distance
      / Momentum-Volume Relation are populated.
    """
    if not trade_dates or not scope_specs:
        return {}
    import time

    t0 = time.perf_counter()

    # ---- Precompute stable scope context ONCE (out of the date loop) ----
    scope_meta: dict[str, tuple[str, str, tuple[str, ...], tuple[str, ...], set[str]]] = {}
    union_ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    scope_member_day_count = 0
    t1_by_scope: dict[str, tuple[str, ...]] = {}
    for spec in scope_specs:
        ids = tuple(spec.member_ids)
        scope_member_day_count += len(ids)
        str_ids = tuple(str(i) for i in ids)
        # T-1 membership: explicit PIT(T-1) if supplied, else current-static T==T1.
        if membership_t1_by_scope is not None:
            t1_ids = tuple(
                str(i) for i in membership_t1_by_scope.get(spec.scope_key, ())
            )
        else:
            t1_ids = str_ids
        t1_by_scope[spec.scope_key] = t1_ids
        scope_meta[spec.scope_key] = (
            spec.scope_type,
            spec.scope_name,
            str_ids,
            t1_ids,
            set(str_ids),
        )
        for mid in ids:
            if mid not in seen:
                seen.add(mid)
                union_ids.append(mid)
    scope_member_day_count *= len(trade_dates)
    unique_member_day_count = len(union_ids) * len(trade_dates)
    duplication_factor = (
        scope_member_day_count / unique_member_day_count
        if unique_member_day_count
        else 0.0
    )

    batch_counters: dict[str, int] = (
        prep_counters if prep_counters is not None else {}
    )
    batch_fallback_reasons: list[str] = (
        prep_fallback_reasons if prep_fallback_reasons is not None else []
    )

    out: dict[str, list[PreparedScope]] = {s.scope_key: [] for s in scope_specs}
    t_loop = time.perf_counter()
    for t in trade_dates:
        t1 = union_ctx.t1_by_date.get(t)
        states_t = union_ctx.states_by_date.get(t, {})
        states_t1 = union_ctx.states_by_date.get(t1, {}) if t1 else {}
        structure_events = union_ctx.events_by_date.get(t, [])
        # PERF-FIX-STRUCTURAL-1 (P0-C): index the union events once per date as
        # member_id -> events, so each scope gathers ONLY its own members' events
        # instead of scanning the whole union event stream per scope.
        events_by_member: dict[str, list[StructureEvent]] = {}
        for _e in structure_events:
            events_by_member.setdefault(_e.member_id, []).append(_e)
        current_only_facts = (
            current_only_facts_by_date.get(t, {})
            if current_only_facts_by_date is not None
            else {}
        )
        # ROUND-2.2B: per-T coverage for this union membership.  ``None`` coverage
        # source (no DB) or a date absent from the map -> coverage unavailable.
        # PERF-FIX-STRUCTURAL-1 (P0-B): convert the union coverage UUIDs to strings
        # EXACTLY ONCE per date (not once per scope), then each scope takes only its
        # own PIT(T) ∩ coverage intersection (scope-local) — never copies the whole
        # union coverage into every PreparedScope (which was O(D×S×U) string/alloc
        # + a 4096-element set rebuild per scope in the Core).
        coverage_t = None if coverage_by_date is None else coverage_by_date.get(t)
        covered_str = (
            None if coverage_t is None else frozenset(str(i) for i in coverage_t)
        )

        # VEC-1: ONE canonical member build for the whole union per trade_date.
        # ``_build_member_observations`` remains the single member-construction
        # owner; the scopes below only SELECT the resulting immutable
        # MemberObservation by reference (scope membership order preserved).
        # PERF-OOM: when the union was prepared chunked, the already-built compact
        # ``MemberObservation`` list is carried on the context (heavy bars/vectors
        # already released) — reuse it verbatim so output is identical to the
        # non-chunked oracle.  Otherwise build from the live bars/vec_volume.
        if union_ctx.prebuilt_members_by_date is not None:
            union_members = union_ctx.prebuilt_members_by_date.get(t, [])
        else:
            union_members = _build_member_observations(
                union_ids,
                trade_date=t,
                t1=t1,
                states_t=states_t,
                states_t1=states_t1,
                bars=union_ctx.bars,
                current_only_facts=current_only_facts,
                vec_volume=union_ctx.vec_volume,
                counters=batch_counters,
                fallback_reasons=batch_fallback_reasons,
            )
        member_by_id = {m.member_id: m for m in union_members}

        for scope_key, (
            scope_type, scope_name, str_ids, str_ids_t1, _member_set,
        ) in scope_meta.items():
            members = tuple(
                member_by_id[sid] for sid in str_ids if sid in member_by_id
            )
            # Slice 4A3 — Board Event Freshness: the scope universe is the
            # board-ready members ONLY (Board ``ready_ids`` parity: active ∩
            # snapshot-exists ∩ trend-ready).  Events come from the union-level
            # ``[T-20, T]`` batch read (ONE query, never per-scope SQL), sliced
            # here by board-ready member IDs into the ``(event_type,
            # event_time_iso)`` stream consumed by ``compute_freshness``.
            freshness_events = tuple(
                (etype, etime)
                for m in members
                if m.board_current_ready
                for (etype, etime) in union_ctx.freshness_events_by_member.get(
                    m.member_id, ()
                )
            )
            # VEC-1-CORRECTION: the union's events are filtered to this scope's
            # membership so PreparedScope.events stays strictly scope-local (the
            # Scope Core would drop out-of-scope events anyway, but the contract
            # is that a PreparedScope carries ONLY its own members' events).
            # ROUND-2.2B: coverage-unavailable -> no events + coverage=None.
            # PERF-FIX-STRUCTURAL-1 (P0-B): scope-local coverage = PIT(scope) ∩
            # Coverage(union) only — never the whole union coverage.  This keeps
            # ``event_coverage_member_ids`` bounded by the scope's own membership
            # and lets the Core's ``set(...)`` intersect a few hundred IDs, not 4096.
            if covered_str is None:
                scope_coverage = None
                scope_events: tuple[StructureEvent, ...] = ()
            else:
                scope_coverage = tuple(sid for sid in str_ids if sid in covered_str)
                # PERF-FIX-STRUCTURAL-1 (P0-C): gather via the per-date member index,
                # then filter to this scope's covered members.  No full union scan.
                scope_events = tuple(
                    e
                    for sid in str_ids
                    if sid in covered_str
                    for e in events_by_member.get(sid, ())
                )
            # Per-scope status / diagnostics win when supplied (Current L1 PIT
            # batch owner); otherwise the caller-level defaults apply (Historical
            # current-static wrapper passes "current_static").
            out[scope_key].append(
                PreparedScope(
                    scope_type=scope_type,
                    scope_key=scope_key,
                    scope_name=scope_name,
                    trade_date=t,
                    canonical_t1=t1,
                    pit_member_ids=str_ids,
                    pit_member_ids_t1=str_ids_t1,
                    members=members,
                    t1_membership_available=(
                        t1_membership_available_by_scope.get(scope_key, t1_membership_available)
                        if t1_membership_available_by_scope is not None
                        else t1_membership_available
                    ),
                    pit_status_t=(
                        pit_status_by_scope.get(scope_key, pit_status_t)
                        if pit_status_by_scope is not None
                        else pit_status_t
                    ),
                    pit_status_t1=(
                        pit_status_t1_by_scope.get(scope_key, pit_status_t1)
                        if pit_status_t1_by_scope is not None
                        else pit_status_t1
                    ),
                    diagnostics=(
                        diagnostics_by_scope.get(scope_key, ())
                        if diagnostics_by_scope is not None
                        else ()
                    ),
                    event_coverage_member_ids=scope_coverage,
                    events=scope_events,
                    freshness_events=freshness_events,
                )
            )
    loop_ms = (time.perf_counter() - t_loop) * 1000.0
    total_ms = (time.perf_counter() - t0) * 1000.0
    # rules/25 §8.7 physical-cost instrumentation (pure observability — never
    # affects a business branch).  VEC-1 makes the vec_hit/vec_fallback counters
    # report unique-member-day builds instead of scope-member-day builds.
    logger.info(
        "[scope-prep-union-vec1] scope_count=%d union_member_count=%d "
        "trade_date_count=%d scope_member_day_count=%d unique_member_day_count=%d "
        "duplication_factor=%.2f member_build_calls=%d vec_hit=%d vec_fallback=%d "
        "fallback_reasons=%s replay_loop_ms=%.1f total_ms=%.1f",
        len(scope_specs), len(union_ids), len(trade_dates),
        scope_member_day_count, unique_member_day_count, duplication_factor,
        len(trade_dates),
        batch_counters.get("vec_hit", 0), batch_counters.get("vec_fallback", 0),
        ",".join(batch_fallback_reasons) or "-",
        loop_ms, total_ms,
    )
    return out


async def prepare_scopes_from_union(
    session: AsyncSession,
    scope_type: str,
    trade_dates: list[date],
    scope_members: dict[str, tuple[list[uuid.UUID], str]],
    union_ctx: _UnionFactContext,
    *,
    pit_status_t: str = "current_static",
    pit_status_t1: str = "current_static",
    t1_membership_available: bool = True,
    prep_counters: dict[str, int] | None = None,
    prep_fallback_reasons: list[str] | None = None,
) -> dict[str, list[PreparedScope]]:
    """DB-aware thin wrapper over the pure union preparation core.

    Builds per-scope ``PreparedScope`` series by slicing a shared union fact
    context.  This wrapper hard-codes the **Historical Dynamics current-static
    semantics** (T-1 membership == T membership, Current-only facts unavailable)
    that the previous inline implementation baked in; it delegates the entire
    calculation to :func:`build_prepared_scopes_from_union` so the Dataset
    Replay Adapter and the DB path share the SAME production calculation owner.

    For Current L1 (PIT(T), Current-only snapshot facts) use
    :func:`build_prepared_scopes_from_union` directly — do NOT route Current L1
    through this wrapper, or the current-static exception would be smuggled in.
    """
    specs = [
        ScopeReplaySpec(
            scope_type=scope_type,
            scope_key=scope_key,
            scope_name=scope_name,
            member_ids=tuple(member_ids),
        )
        for scope_key, (member_ids, scope_name) in scope_members.items()
    ]
    # ROUND-2.2B: bulk exact-T Event Coverage (one query for all dates), passed
    # into the pure core.  A date absent from the map -> coverage unavailable.
    coverage_by_date = await _load_batch_backfill_event_coverage(
        session, list(union_ctx.bars.keys()), trade_dates
    )
    return build_prepared_scopes_from_union(
        trade_dates=trade_dates,
        scope_specs=specs,
        union_ctx=union_ctx,
        membership_t1_by_scope=None,          # current-static T==T1
        current_only_facts_by_date=None,      # Current-only unavailable
        coverage_by_date=coverage_by_date,
        pit_status_t=pit_status_t,
        pit_status_t1=pit_status_t1,
        t1_membership_available=t1_membership_available,
        prep_counters=prep_counters,
        prep_fallback_reasons=prep_fallback_reasons,
    )


async def _load_current_needs(
    session: AsyncSession,
    union_members: list[uuid.UUID],
    trade_date: date,
    *,
    source_core_run_id: uuid.UUID,
) -> dict[str, dict[str, object]]:
    """Load + enrich ALL Current-only canonical facts needed by
    ``_build_member_observations`` BEFORE any union prep (PERF-OOM-V2 P0-2).

    The chunked union prep prebuilds ``MemberObservation`` objects and releases
    the heavy bars/vectors per chunk, so every Current-only fact consumed by the
    member builder MUST be present here — enrichment done after prep would never
    re-enter ``_build_member_observations``.

    Steps (fixed order):
      1. exact-T ``StockFeatureSnapshot`` first_pyramid_flat facts (release_volume
         ratio, momentum volume relation, BB position/width, VWAP return, trailing
         distance) via ``_load_current_only_snapshot_facts``;
      2. union-level ``Instrument`` metadata (active eligibility + symbol) via
         ``_load_batch_instrument_board_meta``;
      3. inject ``_BOARD_CURRENT_ELIGIBLE_KEY`` / ``_BOARD_CURRENT_SYMBOL_KEY``
         into the per-member fact dict (matches the legacy Board flat_list
         pre-filter, scoped to migrated Board capabilities only).
    """
    facts = await _load_current_only_snapshot_facts(
        session,
        union_members,
        trade_date,
        source_core_run_id=source_core_run_id,
    )
    # Slice 4A2 — Board union-level Instrument metadata (single batch query):
    # ``Instrument.status == "active"`` eligibility (4A1R2) AND the symbol needed
    # for ``leader_symbol`` (4A2).  Carried into the current-only facts so
    # ``build_member_observation_from_facts`` can gate the migrated Board
    # capability universe and emit the leader symbol exactly like the legacy Board
    # flat_list pre-filter.  Affects ONLY the migrated Board capabilities, not
    # other Review universes.
    board_meta = await _load_batch_instrument_board_meta(session, union_members)
    for iid, member_facts in facts.items():
        meta = board_meta.get(iid)
        if meta is None:
            member_facts[_BOARD_CURRENT_ELIGIBLE_KEY] = False
            member_facts[_BOARD_CURRENT_SYMBOL_KEY] = None
        else:
            member_facts[_BOARD_CURRENT_ELIGIBLE_KEY] = bool(meta["eligible"])
            member_facts[_BOARD_CURRENT_SYMBOL_KEY] = meta["symbol"]
    return facts


async def prepare_current_scope_observations_batch(
    session: AsyncSession,
    trade_date: date,
    scope_specs: Sequence[ScopeReplaySpec],
    *,
    trade_dates: list[date] | None = None,
    source_core_run_id: uuid.UUID,
    chunk_members: bool = False,
) -> dict[str, PreparedScope] | dict[str, list[PreparedScope]]:
    """Batch-prepare current-day (L1 PIT) Canonical Scope Observations — the
    SINGLE current-day preparation owner.

    When ``trade_dates`` is provided (e.g. ``[T-1, T]``), the union fact load and
    ``build_prepared_scopes_from_union`` operate on that multi-date axis and the
    function returns the per-scope SERIES (``dict[str, list[PreparedScope]]``),
    keyed/ordered by ``trade_dates``. This is the family-batch path used by
    Leadership T-1→T reconstruction (no per-scope N+1, no second algorithm).
    With the default (``None``) the behaviour is unchanged: single ``trade_date``,
    returned unwrapped as ``dict[str, PreparedScope]``.

    The one DB entry point for the orchestrator's current-day canonical
    observation double-write.  It:

      1. resolves PIT(T) / PIT(T-1) membership per scope through the existing
         ``review_scope_service.resolve_scope_members`` owner (identical
         error / status / diagnostics semantics to the former per-scope path);
      2. loads the union of member facts ONCE (calendar, FP states, bars, FP
         events, backfill event coverage, current-only snapshot facts) via the
         shared batch loaders;
      3. slices per-scope ``PreparedScope`` via
         :func:`build_prepared_scopes_from_union` — the single preparation
         calculation owner.

    ``source_core_run_id`` (Slice 4A1R2) is the immutable ReviewRun input identity.
    It is threaded into ``_load_current_only_snapshot_facts`` so the Review exact-T
    snapshot source is lineage-locked to the same core run the Board producer used.
    The orchestrator passes ``run.source_core_run_id`` from BOTH ``compute_run`` and
    ``resume_run`` — this layer never re-resolves "the latest publication pointer".

    Every input scope yields exactly one ``PreparedScope`` keyed by ``scope_key``.
    A scope whose PIT(T) is unavailable gets the same terminal ``unavailable``
    The legacy path produced (empty members, ``pit_status_t`` /
    ``pit_status_t1`` / diagnostics preserved) — never an exception, never a fake
    empty payload.  ``member_ids`` on the input ``ScopeReplaySpec`` is ignored:
    PIT membership is always resolved here (the caller cannot pre-fix it).
    """
    if not scope_specs:
        return {}
    effective_dates = trade_dates if trade_dates is not None else [trade_date]
    # M3-B early RSS attribution: capture the baseline BEFORE any current-day
    # membership resolution / snapshot loading.  The pre-chunk memory owner is
    # the full summary_payload hydration that used to happen inside
    # ``_load_current_only_snapshot_facts`` — this marker plus
    # ``current-prep-current-needs-loaded`` bracket the exact jump the OOM
    # acceptance must prove no longer reaches ~4 GiB.
    _log_rss("current-prep-start", scope_count=len(scope_specs))
    t1 = await calendar_service.get_previous_trading_day_async(session, trade_date)

    # ---- Resolve PIT(T) / PIT(T-1) per scope (single owner: resolve_scope_members).
    # ---- Unavailable scopes are terminal-ized directly; available scopes are
    # ---- replayed through the shared union preparation core.
    resolved_specs: list[ScopeReplaySpec] = []
    terminal: dict[str, PreparedScope] = {}
    pit_status_by_scope: dict[str, str] = {}
    pit_status_t1_by_scope: dict[str, str] = {}
    t1_membership_available_by_scope: dict[str, bool] = {}
    diagnostics_by_scope: dict[str, tuple[str, ...]] = {}
    membership_t1_by_scope: dict[str, tuple[uuid.UUID, ...]] = {}

    for spec in scope_specs:
        # Market historical guard (Round 1B closure): ``resolve_scope_members
        # ("market")`` returns the CURRENT active universe and ignores trade_date.
        # Using current universe x historical trade_date for a Market Observation
        # is a semantic error (current-snapshot applied to history), so market is
        # skipped with an explicit diagnostic — never current_snapshot.
        if spec.scope_type == "market":
            terminal[spec.scope_key] = PreparedScope(
                scope_type=spec.scope_type,
                scope_key=spec.scope_key,
                scope_name=spec.scope_name,
                trade_date=trade_date,
                canonical_t1=t1,
                pit_member_ids=(),
                pit_member_ids_t1=(),
                members=(),
                t1_membership_available=False,
                pit_status_t="unavailable",
                pit_status_t1="unavailable",
                diagnostics=(MARKET_SKIP_DIAGNOSTIC,),
                event_coverage_member_ids=None,
            )
            continue

        # ---- PIT(T) ----
        pit_ids_t: list[uuid.UUID] = []
        scope_name = spec.scope_name
        pit_status_t = "ready"
        diagnostics: list[str] = []
        try:
            pit_ids_t, resolved_name = await review_scope_service.resolve_scope_members(
                session,
                spec.scope_type,
                spec.scope_key,
                trade_date=trade_date,
            )
            scope_name = resolved_name or spec.scope_name
            pit_status_t = "historical_pit"
        except (
            PITMembershipUnavailableError,
            review_scope_service.OptionalScopeUnavailableError,
        ) as exc:
            pit_status_t = "unavailable"
            diagnostics.append(f"pit_unavailable_T:{spec.scope_type}/{spec.scope_key} {exc}")
        except review_scope_service.ScopeSnapshotError as exc:
            pit_status_t = "unavailable"
            diagnostics.append(f"scope_error_T:{spec.scope_type}/{spec.scope_key} {exc}")

        # ---- PIT(T-1) ----
        pit_ids_t1: list[uuid.UUID] = []
        t1_membership_available = False
        pit_status_t1 = "unavailable"
        if t1 is not None and pit_status_t != "unavailable":
            try:
                pit_ids_t1, _ = await review_scope_service.resolve_scope_members(
                    session,
                    spec.scope_type,
                    spec.scope_key,
                    trade_date=t1,
                )
                pit_status_t1 = "historical_pit"
                t1_membership_available = True
            except (
                PITMembershipUnavailableError,
                review_scope_service.OptionalScopeUnavailableError,
            ) as exc:
                pit_status_t1 = "unavailable"
                diagnostics.append(
                    f"pit_unavailable_T1:{spec.scope_type}/{spec.scope_key} {exc}"
                )
            except review_scope_service.ScopeSnapshotError as exc:
                pit_status_t1 = "unavailable"
                diagnostics.append(
                    f"scope_error_T1:{spec.scope_type}/{spec.scope_key} {exc}"
                )
        elif t1 is None:
            diagnostics.append("canonical_t1_unavailable: no previous trading day")

        diagnostics_tuple = tuple(diagnostics)
        if pit_status_t == "unavailable":
            terminal[spec.scope_key] = PreparedScope(
                scope_type=spec.scope_type,
                scope_key=spec.scope_key,
                scope_name=scope_name,
                trade_date=trade_date,
                canonical_t1=t1,
                pit_member_ids=(),
                pit_member_ids_t1=tuple(str(i) for i in pit_ids_t1),
                members=(),
                t1_membership_available=t1_membership_available,
                pit_status_t=pit_status_t,
                pit_status_t1=pit_status_t1,
                diagnostics=diagnostics_tuple,
                event_coverage_member_ids=None,
                events=(),
            )
            continue

        resolved_specs.append(
            ScopeReplaySpec(
                scope_type=spec.scope_type,
                scope_key=spec.scope_key,
                scope_name=scope_name,
                member_ids=tuple(pit_ids_t),
            )
        )
        pit_status_by_scope[spec.scope_key] = pit_status_t
        pit_status_t1_by_scope[spec.scope_key] = pit_status_t1
        t1_membership_available_by_scope[spec.scope_key] = t1_membership_available
        diagnostics_by_scope[spec.scope_key] = diagnostics_tuple
        membership_t1_by_scope[spec.scope_key] = tuple(pit_ids_t1)

    if not resolved_specs:
        return terminal

    # M3-B early RSS attribution: membership resolution is complete.  Reports the
    # PIT(T) / PIT(T-1) member-reference totals (the 83989-ish reference count on
    # the real scale) without building any additional duplicate structures —
    # these totals are derived from the already-resolved spec tuples.
    _log_rss(
        "current-prep-memberships-resolved",
        scope_count=len(resolved_specs),
        member_refs_t=sum(len(spec.member_ids) for spec in resolved_specs),
        member_refs_t1=sum(len(m) for m in membership_t1_by_scope.values()),
    )

    # ---- Union member facts: load the whole member set ONCE, then slice. ----
    union_members: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for spec in resolved_specs:
        for mid in spec.member_ids:
            if mid not in seen:
                seen.add(mid)
                union_members.append(mid)

    # M3-B early RSS attribution: the deduplicated union is now bounded (the
    # unique PIT(T) member set feeding every loader below).
    _log_rss("current-prep-union-built", union_member_count=len(union_members))

    # ---- Current-only facts MUST be fully resolved BEFORE the chunked union
    # prep (PERF-OOM-V2 P0-1/P0-2).  The chunked builder prebuilds
    # ``MemberObservation`` objects inside each chunk and releases the heavy
    # bars/vectors immediately; any Current-only fact not present at that point
    # is permanently frozen out of the prebuilt members.  Order is therefore
    # fixed: resolve union members -> load current-only snapshot facts -> load
    # board_meta -> enrich current_only_facts with eligibility + symbol -> ONLY
    # THEN call either union prep (chunked or oracle).  Coverage is a
    # PreparedScope-level concern and is loaded after prep (unchanged).

    # Current-only snapshot facts at exact T (str-keyed, same shape as the
    # current-only loader consumes). C1a fix: the loader's contract is
    # exact-T only (scalar ``trade_date``), NOT ``effective_dates`` — passing the
    # multi-element list would feed a list into a scalar ``Column == date``
    # comparison and fail at SQL compile/execute time under the real PG adapter.
    current_only_facts = await _load_current_needs(
        session, union_members, trade_date, source_core_run_id=source_core_run_id
    )
    # M3-B early RSS attribution: the current-only snapshot facts (the M3-B
    # projection target) have been loaded.  On the real scale this marker vs
    # ``current-prep-start`` is the decisive proof that pre-chunk RSS no longer
    # jumps to ~4 GiB — the full summary_payload is no longer hydrated here.
    _log_rss(
        "current-prep-current-needs-loaded",
        union_member_count=len(union_members),
        current_only_member_count=len(current_only_facts),
    )
    coverage_by_date = await _load_batch_backfill_event_coverage(
        session, union_members, effective_dates
    )
    _log_rss(
        "current-prep-coverage-loaded",
        union_member_count=len(union_members),
        coverage_member_count=sum(
            len(s) for s in coverage_by_date.values()
        ),
    )

    # PERF-OOM (2026-08-24 closure): the production current-day path uses the
    # chunked, memory-bounded union preparation (heavy bars/vectors released per
    # chunk, Members prebuilt).  The non-chunked path remains the oracle used by
    # tests and the Dataset Replay Adapter — identical outputs.
    _log_rss(
        "current-prep-before-union-context",
        union_member_count=len(union_members),
        trade_date_count=len(effective_dates),
    )
    if chunk_members:
        union_ctx = await prepare_union_fact_context_chunked(
            session,
            effective_dates,
            union_members,
            current_only_facts_by_date={trade_date: current_only_facts},
        )
    else:
        union_ctx = await prepare_union_fact_context(
            session, effective_dates, union_members
        )

    prepared_map = build_prepared_scopes_from_union(
        trade_dates=effective_dates,
        scope_specs=resolved_specs,
        union_ctx=union_ctx,
        membership_t1_by_scope=membership_t1_by_scope,
        current_only_facts_by_date={trade_date: current_only_facts},
        coverage_by_date=coverage_by_date,
        pit_status_by_scope=pit_status_by_scope,
        pit_status_t1_by_scope=pit_status_t1_by_scope,
        t1_membership_available_by_scope=t1_membership_available_by_scope,
        diagnostics_by_scope=diagnostics_by_scope,
        prebuilt_members_by_date=union_ctx.prebuilt_members_by_date,
    )
    if len(effective_dates) == 1:
        # Single-date (current-day) owner contract: unwrap the per-scope series
        # to ``dict[str, PreparedScope]`` consumed by the orchestrator.
        prepared_single = {key: scopes[0] for key, scopes in prepared_map.items()}
        return {**terminal, **prepared_single}
    # Multi-date axis (e.g. [T-1, T] for Leadership T-1→T): return the per-scope
    # SERIES keyed/ordered by ``effective_dates``. No unwrap — the caller slices
    # the dates it needs (Leadership reads [T-1] and [T]).
    return {**terminal, **prepared_map}
