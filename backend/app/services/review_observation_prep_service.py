"""Canonical Observation Data Preparation — DB-aware layer (Round 1B).

Single responsibility: turn real canonical data into ``MemberObservation``
inputs plus the PIT(T) / PIT(T-1) member sets, feeding
``app.domain.review.scope_observation.compute_scope_observation``.

This layer owns the exact canonical T-1 resolution (from the trading calendar),
PIT membership resolution per scope family, and First Pyramid / bar loading.
The pure semantic mapping lives in ``app.services.observation_prep``; the Core
(``scope_observation.py``) stays untouched.

Shadow only: this service is never wired into Filter / Discovery / publication.
"""

from __future__ import annotations

import json
import logging
import uuid
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.member_fact import (
    DailyBarFact,
    previous_state_to_flat,
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
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION
from app.services import calendar_service, review_scope_service
from app.services.board_membership_service import PITMembershipUnavailableError
from app.services.observation_prep import (
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

# Historical market membership is unresolvable this round: ``resolve_scope_members
# ("market", ...)`` returns the CURRENT active universe and ignores trade_date.
# Market shadow is therefore skipped (see the guard in ``prepare_scope``); it is
# never computed from a current snapshot against a historical trade_date.
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
) -> dict[str, dict[str, object]]:
    """Load exact-T Current-only canonical facts per member.

    Returns ``{instrument_id: {MemberObservation attr: value}}`` for members that
    have a consumable exact-T snapshot.  Members without one are simply absent
    from the mapping, which downstream maps to ``None`` (unavailable) — never to a
    fallback snapshot from another trade date.
    """
    if not instrument_ids:
        return {}

    # Exact-T only, joined against the run gate (succeeded + published).
    stmt = (
        select(
            StockFeatureSnapshot.instrument_id,
            StockFeatureSnapshot.summary_payload,
        )
        .join(
            StockFeatureSnapshotRun,
            StockFeatureSnapshot.source_run_id == StockFeatureSnapshotRun.id,
        )
        .where(
            StockFeatureSnapshot.trade_date == trade_date,
            StockFeatureSnapshot.instrument_id.in_(instrument_ids),
            StockFeatureSnapshotRun.trade_date == trade_date,
            StockFeatureSnapshotRun.status == _SNAPSHOT_RUN_CONSUMABLE_STATUS,
            StockFeatureSnapshotRun.published_at.isnot(None),
        )
    )
    rows = (await session.execute(stmt)).all()

    out: dict[str, dict[str, object]] = {}
    for instrument_id, summary_payload in rows:
        flat = (summary_payload or {}).get("first_pyramid_flat")
        if not isinstance(flat, dict):
            continue
        facts: dict[str, object] = {}
        for attr, flat_key in _CURRENT_ONLY_SNAPSHOT_FIELDS.items():
            if flat_key in flat:
                facts[attr] = flat[flat_key]
        if facts:
            out[str(instrument_id)] = facts
    return out


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


async def _load_states(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_date: date | None,
) -> dict[uuid.UUID, dict]:
    """Canonical First Pyramid daily_state payloads at the exact ``trade_date``."""
    if not instrument_ids or trade_date is None:
        return {}
    stmt = select(FirstPyramidHistoryDailyState).where(
        FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
        FirstPyramidHistoryDailyState.trade_date == trade_date,
        FirstPyramidHistoryDailyState.algorithm_version == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
    )
    return {row.instrument_id: row.state_payload for row in (await session.execute(stmt)).scalars()}


async def _load_bar_facts(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_date: date,
) -> dict[uuid.UUID, list[DailyBarFact]]:
    """Ascending per-instrument daily bar facts up to ``trade_date`` (window).

    PERF-IO-2: column projection identical to ``_load_batch_bars`` — only the
    ``DailyBarFact`` columns are selected, never the full ``BarDaily`` ORM row.
    """
    if not instrument_ids:
        return {}
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
            BarDaily.trade_date <= trade_date,
            BarDaily.trade_date >= trade_date - timedelta(days=_BAR_LOOKBACK_DAYS),
        )
        .order_by(BarDaily.instrument_id.asc(), BarDaily.trade_date.asc())
    )
    by_instrument: dict[uuid.UUID, list[DailyBarFact]] = defaultdict(list)
    for bar in (await session.execute(stmt)).all():
        by_instrument[bar.instrument_id].append(DailyBarFact.from_row(bar))
    return dict(by_instrument)


async def _load_structure_events(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_date: date | None,
) -> list[StructureEvent]:
    """Canonical immutable First Pyramid structure events for ``trade_date``.

    Source is ``FirstPyramidHistoryEvent`` (the immutable event stream), NOT
    ``fp_latest_*`` summaries and NOT a flattened array.  Each event is mapped to
    a :class:`StructureEvent`, carrying ``direction`` / ``level`` for leveled
    events (BOS / CHoCH / OB_*) and leaving them ``None`` for EQH/EQL extremes.
    ``release_volume_ratio`` is carried only for SQZ_RELEASE.
    """
    if not instrument_ids or trade_date is None:
        return []
    # Events carry event_time (ISO string) + history_contract_version, NOT a
    # trade_date column.  Filter by canonical algorithm version + history contract
    # version, and the T-day prefix on event_time (contract-aware, avoids v1/NULL
    # legacy events double-counting).
    date_prefix = trade_date.isoformat()
    stmt = select(FirstPyramidHistoryEvent).where(
        FirstPyramidHistoryEvent.instrument_id.in_(instrument_ids),
        FirstPyramidHistoryEvent.algorithm_version == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        FirstPyramidHistoryEvent.history_contract_version == HISTORY_CONTRACT_VERSION,
        FirstPyramidHistoryEvent.event_time.startswith(date_prefix),
    )
    events: list[StructureEvent] = []
    for row in (await session.execute(stmt)).scalars():
        payload = row.event_payload or {}
        # Shared immutable mapper: canonical boundary normalization (CHoCH casing
        # artifact), numeric level / release_volume_ratio coercion, and the bool-only
        # ``internal`` gate all live in ``_map_structure_event`` — the SAME helper
        # used by the Dataset Replay Adapter (Gate 2 mapping parity).
        events.append(
            _map_structure_event(
                instrument_id=str(row.instrument_id),
                event_type=row.event_type,
                direction=payload.get("direction"),
                level=payload.get("level"),
                internal=payload.get("internal"),
                release_volume_ratio=payload.get("release_volume_ratio"),
            )
        )
    return events


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
    """Build canonical ``MemberObservation`` inputs shared by the per-date and the
    batch replay path (single member-construction owner).

    ``counters`` / ``fallback_reasons`` are optional OUT parameters populated by the
    batch path for rules/25 §8.7 physical-cost instrumentation: ``counters["vec_hit"]``
    increments when a member resolves its VolumeContext from the precomputed vectorized
    series (no strict-prior window materialization); ``counters["vec_fallback"]``
    increments and ``fallback_reasons`` records the first reason when it falls back to
    the canonical per-date owner (which needs the strict-prior history window).  They
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
        raw = RawMemberFacts(
            member_id=str(inst_id),
            flat_t=previous_state_to_flat(state_t),
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
            continuous=state_to_continuous(state_t),
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


async def prepare_scope_from_member_ids(
    session: AsyncSession,
    scope_type: str,
    scope_key: str,
    scope_name: str,
    trade_date: date,
    member_ids: list[uuid.UUID],
    *,
    pit_member_ids_t1: list[uuid.UUID] | None = None,
    pit_status_t: str = "current_static",
    pit_status_t1: str = "current_static",
    t1_membership_available: bool = True,
    diagnostics: tuple[str, ...] = (),
    load_current_only: bool = True,
) -> PreparedScope:
    """Prepare canonical MemberObservation inputs from explicitly-given members.

    Used by the current-universe historical reconstruction: the caller fixes the
    membership once (CURRENT STATIC MEMBERSHIP) and reuses it for every
    historical trade date.  This function never resolves membership itself — it
    only prepares member facts at ``trade_date`` (T) and its exact canonical T-1
    via the single canonical loaders shared with ``prepare_scope``.

    ``pit_member_ids_t1`` defaults to the same current member set (the fixed
    universe is valid at T-1 too), so scope transitions stay inside the fixed
    current universe.  Historical facts are read strictly at T / exact T-1.

    ``load_current_only`` gates the Current-only snapshot loader.  The current
    path (``prepare_scope``) keeps it enabled (current-only facts served for the
    current day).  The historical reconstruction passes ``False``: the
    reconstruction is built ONLY from FP history + bars + FP events, never from
    the current-day snapshot store, so current-only facts stay ``None`` (PRD
    v2.3) and the large ``summary_payload`` JSONB is never transferred.
    """
    t1 = await calendar_service.get_previous_trading_day_async(session, trade_date)

    pit_ids_t = list(member_ids)
    t1_ids = list(pit_member_ids_t1) if pit_member_ids_t1 is not None else list(pit_ids_t)

    # ---- Facts ----
    states_t = await _load_states(session, pit_ids_t, trade_date)
    states_t1 = await _load_states(session, pit_ids_t, t1) if t1 else {}
    bar_facts = await _load_bar_facts(session, pit_ids_t, trade_date)
    t1_facts = await _load_bar_facts(session, pit_ids_t, t1) if t1 else {}
    # Wrap the per-replay-date bar lists into per-member ascending series so the
    # shared ``_build_member_observations`` owner can extract the T-row / T-1-row
    # bar (``exact_bar`` for the vec path) and the strict-prior window (only for the
    # window-bound fallback) identically to the batch path.  ``bar_facts`` holds
    # the T-day bar(s); ``t1_facts`` holds the T-1-day bar(s) — concat keeps the
    # ascending order the series owner expects (t1 < t).
    bars: dict[uuid.UUID, _InstrumentBarSeries] = {}
    for inst_id in bar_facts:
        # ``bar_facts`` holds the T-day bar(s); ``t1_facts`` holds the T-1-day bar(s)
        # (t1 < t, no overlap in normal loads).  Key by trade_date so a load that
        # returns the same bar for both dates (e.g. some test doubles) is not
        # double-counted, and keep ascending order for the series owner.
        merged: dict[date, DailyBarFact] = {}
        for b in t1_facts.get(inst_id, []):
            merged[b.trade_date] = b
        for b in bar_facts[inst_id]:
            merged[b.trade_date] = b
        ordered = [merged[d] for d in sorted(merged)]
        bars[inst_id] = _InstrumentBarSeries(
            facts=tuple(ordered), dates=tuple(b.trade_date for b in ordered)
        )
    # ROUND-2.2B: exact-T Event Coverage gate (single owner).  ``None`` = coverage
    # source unavailable -> structure-events unavailable (no fake denominator).
    # A set (possibly empty) = valid coverage -> only covered members' events load.
    coverage = await _load_backfill_event_coverage_member_ids(
        session, pit_ids_t, trade_date
    )
    if coverage is None:
        coverage_members = None
        structure_events: list[StructureEvent] = []
    else:
        coverage_members = tuple(str(i) for i in coverage)
        structure_events = await _load_structure_events(
            session, [i for i in pit_ids_t if uuid.UUID(str(i)) in coverage], trade_date
        )
    # Current-only canonical facts from the exact-T snapshot (see
    # ``_load_current_only_snapshot_facts``).  The historical reconstruction
    # passes ``load_current_only=False``: current-only facts have no FP-history
    # source and must stay None for historical T (PRD v2.3) — never fetched from
    # the snapshot store, never a current backfill.
    current_only_facts = (
        await _load_current_only_snapshot_facts(session, pit_ids_t, trade_date)
        if load_current_only
        else {}
    )

    members = _build_member_observations(
        pit_ids_t,
        trade_date=trade_date,
        t1=t1,
        states_t=states_t,
        states_t1=states_t1,
        bars=bars,
        current_only_facts=current_only_facts,
    )

    return PreparedScope(
        scope_type=scope_type,
        scope_key=scope_key,
        scope_name=scope_name,
        trade_date=trade_date,
        canonical_t1=t1,
        pit_member_ids=tuple(str(i) for i in pit_ids_t),
        pit_member_ids_t1=tuple(str(i) for i in t1_ids),
        members=tuple(members),
        t1_membership_available=t1_membership_available,
        pit_status_t=pit_status_t,
        pit_status_t1=pit_status_t1,
        diagnostics=tuple(diagnostics),
        event_coverage_member_ids=coverage_members,
        events=tuple(structure_events),
    )


# ----------------------------------------------------------------------------
# BATCH historical reconstruction: ONE bulk read of the whole member x date
# window, then replay per T (no per-date reload).  The per-date loaders above
# stay as the canonical SQL owners; the batch loaders read the SAME canonical
# tables once and the replay reproduces exactly the per-date windows so the
# resulting PreparedScope is byte-identical to the per-date path.
#
# Vectorized preprocessing: each member's VolumeContext series is computed ONCE
# across the whole window with the numpy owner (``compute_volume_context_vectorized``),
# then the per-T replay only indexes the precomputed row — the pandas rolling /
# percentile work that the per-date path repeats for every (member, T) is done a
# single time per member.  The canonical per-date owner remains the oracle and is
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
        ``_load_bar_facts`` for one replay date ``hi`` exactly."""
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


async def _load_backfill_event_coverage_member_ids(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_date: date,
) -> frozenset[uuid.UUID] | None:
    """Return the exact-T Event Coverage member set (or ``None`` = no trusted source).

    One SQL, DailyState JOIN Run JOIN RunItem, selecting only instrument_id (no ORM
    hydration, no JSONB).  ``None`` means the coverage source is unavailable for this
    scope/date (the caller must NOT fabricate it); ``frozenset()`` is a valid (possibly
    empty) coverage — a legal zero-event day still yields a real denominator.
    """
    if not instrument_ids or trade_date is None:
        return None
    stmt = (
        select(FirstPyramidHistoryDailyState.instrument_id)
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
            FirstPyramidHistoryDailyState.trade_date == trade_date,
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
        .distinct()
    )
    rows = (await session.execute(stmt)).scalars().all()
    # ROUND-2.2B AUDIT FIX (F1): 0 coverage rows == NO trusted canonical-backfill
    # coverage source -> return ``None`` (unavailable), NOT an empty frozenset.
    # An empty set would be misread by the Core as "valid empty coverage" and
    # produce a fake ``ready / denominator=0``.  ``None`` -> structure-events
    # unavailable / denominator=None.  This keeps per-date consistent with batch.
    if not rows:
        return None
    return frozenset(uuid.UUID(str(i)) for i in rows)


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


async def prepare_scope_series_from_member_ids(
    session: AsyncSession,
    scope_type: str,
    scope_key: str,
    scope_name: str,
    trade_dates: list[date],
    member_ids: list[uuid.UUID],
    *,
    pit_member_ids_t1: list[uuid.UUID] | None = None,
    pit_status_t: str = "current_static",
    pit_status_t1: str = "current_static",
    t1_membership_available: bool = True,
    diagnostics: tuple[str, ...] = (),
    load_current_only: bool = False,
    prep_counters: dict[str, int] | None = None,
    prep_fallback_reasons: list[str] | None = None,
) -> list[PreparedScope]:
    """Batch prepare one historical Scope Observation per date in one bulk read.

    The whole member x date window is loaded ONCE (calendar, FP states, bars,
    FP events) and replayed per ``trade_date`` — reproducing exactly the
    per-date ``prepare_scope_from_member_ids`` windows, so each returned
    :class:`PreparedScope` is identical to the per-date path (membership is
    caller-fixed CURRENT STATIC, facts come from exact T / exact canonical T-1,
    never current-day backfill).

    ``load_current_only`` defaults to ``False`` (this is the historical-series
    path: current-only snapshot facts have no FP-history source and stay None
    per PRD v2.3).
    """
    if not trade_dates:
        return []
    import time

    t0 = time.perf_counter()
    t1_by_date = await _load_batch_calendar(session, trade_dates)
    cal_ms = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter()
    states_by_date = await _load_batch_states(session, member_ids, trade_dates, t1_by_date)
    states_ms = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter()
    bars = await _load_batch_bars(session, member_ids, trade_dates)
    bars_ms = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter()
    events_by_date = await _load_batch_events(session, member_ids, trade_dates)
    events_ms = (time.perf_counter() - t0) * 1000.0
    # ROUND-2.2B: bulk exact-T Event Coverage in ONE query (no per-date SQL).
    coverage_by_date = await _load_batch_backfill_event_coverage(
        session, member_ids, trade_dates
    )
    # Vectorized preprocessing: per-member VolumeContext series computed once,
    # indexed by the per-T replay (see ``_build_member_observations``).
    t_vec = time.perf_counter()
    vec_volume = _precompute_vectorized_volume(bars)
    vec_ms = (time.perf_counter() - t_vec) * 1000.0

    pit_ids_t = list(member_ids)
    t1_ids = list(pit_member_ids_t1) if pit_member_ids_t1 is not None else list(pit_ids_t)

    out: list[PreparedScope] = []
    t_loop = time.perf_counter()
    # rules/25 §8.7 physical-cost instrumentation: accumulate vectorized VolumeContext
    # hit/fallback counts once for the whole replay.  Pure counters — no effect on the
    # constructed MemberObservations or any business branch.  When ``prep_counters`` is
    # provided (Composition Owner), the same counts are surfaced to it for unified
    # reporting; otherwise the local counters feed only the log line.
    batch_counters: dict[str, int] = prep_counters if prep_counters is not None else {}
    batch_fallback_reasons: list[str] = (
        prep_fallback_reasons if prep_fallback_reasons is not None else []
    )
    for t in trade_dates:
        t1 = t1_by_date.get(t)
        states_t = states_by_date.get(t, {})
        states_t1 = states_by_date.get(t1, {}) if t1 else {}
        # NOTE: the full 400-bar window is NOT materialized here. ``_build_member_observations``
        # extracts the T-row bar via ``series.exact_bar(t)`` (O(log n)) for the
        # vectorized fast path, and only calls ``series.window(t)`` inside the
        # window-bound fallback for the few members that need strict-prior history.
        # This removes the O(dates x members x ~400) list-copy hotspot.
        # ROUND-2.2B: per-T coverage from the bulk map.  ``None`` (date absent)
        # -> coverage source unavailable -> no events, no fake denominator.
        coverage_t = coverage_by_date.get(t)
        if coverage_t is None:
            coverage_members = None
            structure_events: list[StructureEvent] = []
        else:
            coverage_members = tuple(str(i) for i in coverage_t)
            covered = {str(i) for i in coverage_t}
            structure_events = [
                e for e in events_by_date.get(t, []) if e.member_id in covered
            ]
        current_only_facts = (
            await _load_current_only_snapshot_facts(session, pit_ids_t, t)
            if load_current_only
            else {}
        )
        members = _build_member_observations(
            pit_ids_t,
            trade_date=t,
            t1=t1,
            states_t=states_t,
            states_t1=states_t1,
            bars=bars,
            current_only_facts=current_only_facts,
            vec_volume=vec_volume,
            counters=batch_counters,
            fallback_reasons=batch_fallback_reasons,
        )
        out.append(
            PreparedScope(
                scope_type=scope_type,
                scope_key=scope_key,
                scope_name=scope_name,
                trade_date=t,
                canonical_t1=t1,
                pit_member_ids=tuple(str(i) for i in pit_ids_t),
                pit_member_ids_t1=tuple(str(i) for i in t1_ids),
                members=tuple(members),
                t1_membership_available=t1_membership_available,
                pit_status_t=pit_status_t,
                pit_status_t1=pit_status_t1,
                diagnostics=tuple(diagnostics),
                event_coverage_member_ids=coverage_members,
                events=tuple(structure_events),
            )
        )
    loop_ms = (time.perf_counter() - t_loop) * 1000.0
    logger.info(
        "[scope-prep-batch] scope_type=%s scope_key=%s member_count=%d "
        "trade_date_count=%d vec_hit=%d vec_fallback=%d fallback_reasons=%s "
        "cal_ms=%.1f states_ms=%.1f bars_ms=%.1f events_ms=%.1f "
        "vec_precompute_ms=%.1f replay_loop_ms=%.1f",
        scope_type, scope_key, len(member_ids), len(trade_dates),
        batch_counters.get("vec_hit", 0), batch_counters.get("vec_fallback", 0),
        ",".join(batch_fallback_reasons) or "-",
        cal_ms, states_ms, bars_ms, events_ms, vec_ms, loop_ms,
    )
    return out


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


def build_union_fact_context_from_loaded_facts(
    *,
    t1_by_date: dict[date, date | None],
    states_by_date: dict[date, dict[uuid.UUID, dict]],
    bars: dict[uuid.UUID, _InstrumentBarSeries],
    events_by_date: dict[date, list[StructureEvent]],
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

    Identical bulk-loading owner as :func:`prepare_scope_series_from_member_ids`
    (calendar, FP states, bars, FP events, vectorized volume), but the union of
    members is supplied by the caller so the cost is incurred exactly once even
    when many scopes share the same members.  Slicing per scope happens in
    :func:`prepare_scopes_from_union`.
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
    t_vec = time.perf_counter()
    vec_volume = _precompute_vectorized_volume(bars)
    vec_ms = (time.perf_counter() - t_vec) * 1000.0
    batch_counters: dict[str, int] = (
        prep_counters if prep_counters is not None else {}
    )
    batch_fallback_reasons: list[str] = (
        prep_fallback_reasons if prep_fallback_reasons is not None else []
    )
    # VEC-1: counters are now populated by the per-date union build in
    # ``prepare_scopes_from_union``, not per-scope.  Feed-through is implicit
    # via the shared dict reference.
    logger.info(
        "[union-fact-context] union_member_count=%d trade_date_count=%d "
        "cal_ms=%.1f states_ms=%.1f bars_ms=%.1f events_ms=%.1f vec_precompute_ms=%.1f",
        len(union_member_ids), len(trade_dates),
        cal_ms, states_ms, bars_ms, events_ms, vec_ms,
    )
    return _UnionFactContext(
        t1_by_date=t1_by_date,
        states_by_date=states_by_date,
        bars=bars,
        events_by_date=events_by_date,
        vec_volume=vec_volume,
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
    prep_counters: dict[str, int] | None = None,
    prep_fallback_reasons: list[str] | None = None,
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
    byte-identical to calling ``prepare_scope_series_from_member_ids`` for that
    scope alone.

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
        current_only_facts = (
            current_only_facts_by_date.get(t, {})
            if current_only_facts_by_date is not None
            else {}
        )
        # ROUND-2.2B: per-T coverage for this union membership.  ``None`` coverage
        # source (no DB) or a date absent from the map -> coverage unavailable.
        coverage_t = None if coverage_by_date is None else coverage_by_date.get(t)

        # VEC-1: ONE canonical member build for the whole union per trade_date.
        # ``_build_member_observations`` remains the single member-construction
        # owner; the scopes below only SELECT the resulting immutable
        # MemberObservation by reference (scope membership order preserved).
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
            scope_type, scope_name, str_ids, str_ids_t1, member_set,
        ) in scope_meta.items():
            members = tuple(
                member_by_id[sid] for sid in str_ids if sid in member_by_id
            )
            # VEC-1-CORRECTION: the union's events are filtered to this scope's
            # membership so PreparedScope.events stays strictly scope-local (the
            # Scope Core would drop out-of-scope events anyway, but the contract
            # is that a PreparedScope carries ONLY its own members' events).
            # ROUND-2.2B: coverage-unavailable -> no events + coverage=None.
            if coverage_t is None:
                scope_coverage = None
                scope_events: tuple[StructureEvent, ...] = ()
            else:
                covered = {str(i) for i in coverage_t}
                scope_coverage = tuple(str(i) for i in coverage_t)
                scope_events = tuple(
                    e for e in structure_events
                    if e.member_id in member_set and e.member_id in covered
                )
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
                    t1_membership_available=t1_membership_available,
                    pit_status_t=pit_status_t,
                    pit_status_t1=pit_status_t1,
                    diagnostics=(),
                    event_coverage_member_ids=scope_coverage,
                    events=scope_events,
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


async def prepare_scope(
    session: AsyncSession,
    scope_type: str,
    scope_key: str,
    trade_date: date,
) -> PreparedScope:
    """Prepare canonical MemberObservation inputs for one scope on ``trade_date``."""
    diagnostics: list[str] = []
    t1 = await calendar_service.get_previous_trading_day_async(session, trade_date)

    # ---- Market historical guard (Round 1B closure) ----
    # ``resolve_scope_members("market")`` returns the current active universe and
    # ignores trade_date.  Using current universe x historical trade_date for a
    # Market Observation is a semantic error (current-snapshot applied to
    # history).  Market historical membership is unresolvable this round, so the
    # shadow is skipped with an explicit diagnostic — never current_snapshot.
    if scope_type == "market":
        return PreparedScope(
            scope_type=scope_type,
            scope_key=scope_key,
            scope_name=scope_key,
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

    # ---- PIT(T) ----
    pit_ids_t: list[uuid.UUID] = []
    scope_name = scope_key
    pit_status_t = "ready"
    try:
        pit_ids_t, scope_name = await review_scope_service.resolve_scope_members(
            session,
            scope_type,
            scope_key,
            trade_date=trade_date,
        )
        pit_status_t = "historical_pit"
    except (
        PITMembershipUnavailableError,
        review_scope_service.OptionalScopeUnavailableError,
    ) as exc:
        pit_status_t = "unavailable"
        diagnostics.append(f"pit_unavailable_T:{scope_type}/{scope_key} {exc}")
    except review_scope_service.ScopeSnapshotError as exc:
        pit_status_t = "unavailable"
        diagnostics.append(f"scope_error_T:{scope_type}/{scope_key} {exc}")

    # ---- PIT(T-1) ----
    pit_ids_t1: list[uuid.UUID] = []
    t1_membership_available = False
    pit_status_t1 = "unavailable"
    if t1 is not None and pit_status_t != "unavailable":
        try:
            pit_ids_t1, _ = await review_scope_service.resolve_scope_members(
                session,
                scope_type,
                scope_key,
                trade_date=t1,
            )
            pit_status_t1 = "historical_pit"
            t1_membership_available = True
        except (
            PITMembershipUnavailableError,
            review_scope_service.OptionalScopeUnavailableError,
        ) as exc:
            pit_status_t1 = "unavailable"
            diagnostics.append(f"pit_unavailable_T1:{scope_type}/{scope_key} {exc}")
        except review_scope_service.ScopeSnapshotError as exc:
            pit_status_t1 = "unavailable"
            diagnostics.append(f"scope_error_T1:{scope_type}/{scope_key} {exc}")
    elif t1 is None:
        diagnostics.append("canonical_t1_unavailable: no previous trading day")

    if pit_status_t == "unavailable":
        return PreparedScope(
            scope_type=scope_type,
            scope_key=scope_key,
            scope_name=scope_name,
            trade_date=trade_date,
            canonical_t1=t1,
            pit_member_ids=(),
            pit_member_ids_t1=tuple(str(i) for i in pit_ids_t1),
            members=(),
            t1_membership_available=t1_membership_available,
            pit_status_t=pit_status_t,
            pit_status_t1=pit_status_t1,
            diagnostics=tuple(diagnostics),
            event_coverage_member_ids=None,
            events=(),
        )

    # ---- Facts (single canonical path, shared with current-universe
    #      reconstruction via ``prepare_scope_from_member_ids``) ----
    return await prepare_scope_from_member_ids(
        session,
        scope_type,
        scope_key,
        scope_name,
        trade_date,
        pit_ids_t,
        pit_member_ids_t1=pit_ids_t1,
        pit_status_t=pit_status_t,
        pit_status_t1=pit_status_t1,
        t1_membership_available=t1_membership_available,
        diagnostics=tuple(diagnostics),
    )
