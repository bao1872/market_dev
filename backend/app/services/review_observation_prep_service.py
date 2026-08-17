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

import logging
import uuid
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.member_fact import (
    DailyBarFact,
    previous_state_to_flat,
    state_to_continuous,
)
from app.domain.review.scope_observation import MemberObservation, StructureEvent
from app.models.bar import BarDaily
from app.models.calendar import TradingCalendar
from app.models.first_pyramid_history import (
    FirstPyramidHistoryDailyState,
    FirstPyramidHistoryEvent,
)
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
    """Ascending per-instrument daily bar facts up to ``trade_date`` (window)."""
    if not instrument_ids:
        return {}
    stmt = (
        select(BarDaily)
        .where(
            BarDaily.instrument_id.in_(instrument_ids),
            BarDaily.trade_date <= trade_date,
            BarDaily.trade_date >= trade_date - timedelta(days=_BAR_LOOKBACK_DAYS),
        )
        .order_by(BarDaily.instrument_id.asc(), BarDaily.trade_date.asc())
    )
    by_instrument: dict[uuid.UUID, list[DailyBarFact]] = defaultdict(list)
    for bar in (await session.execute(stmt)).scalars():
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
        # Canonical boundary normalization: CHoCH casing is a storage artifact, NOT a
        # product distinction.  Normalize case-insensitively so Scope Core never
        # perceives "CHoCH" vs "CHOCH".  Additive-only; other known types unaffected.
        etype = _normalize_event_type(row.event_type)
        direction = payload.get("direction")
        level = payload.get("level")
        # ``internal`` is the canonical Structure Level dimension (False=Swing,
        # True=Internal).  Stored in the event payload; NOT re-derived by Scope.
        internal_raw = payload.get("internal")
        internal = bool(internal_raw) if isinstance(internal_raw, bool) else None
        release_ratio = payload.get("release_volume_ratio")
        events.append(
            StructureEvent(
                member_id=str(row.instrument_id),
                event_type=etype,
                direction=direction,
                level=(float(level) if isinstance(level, (int, float)) else None),
                internal=internal,
                release_volume_ratio=(
                    float(release_ratio) if isinstance(release_ratio, (int, float)) else None
                ),
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
        state_t = states_t.get(inst_id)
        if state_t is None:
            # Not a valid canonical First Pyramid member/fact at T -> not provided.
            continue
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
    # Canonical immutable structure events for T (PRD §7.4 D).
    structure_events = await _load_structure_events(session, pit_ids_t, trade_date)
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
    (one query), grouped by trade_date -> {instrument_id: state_payload}."""
    if not instrument_ids or not trade_dates:
        return {}
    dates = set(trade_dates)
    dates.update(d for d in t1_by_date.values() if d is not None)
    stmt = select(FirstPyramidHistoryDailyState).where(
        FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
        FirstPyramidHistoryDailyState.trade_date.in_(sorted(dates)),
        FirstPyramidHistoryDailyState.algorithm_version == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
    )
    out: dict[date, dict[uuid.UUID, dict]] = defaultdict(dict)
    for row in (await session.execute(stmt)).scalars():
        out[row.trade_date][row.instrument_id] = row.state_payload
    return dict(out)


async def _load_batch_bars(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    trade_dates: list[date],
) -> dict[uuid.UUID, _InstrumentBarSeries]:
    """All daily bars across ``[first-400d, last]`` once (one query), grouped per
    instrument ascending.  Per-date windows are sliced in memory at replay."""
    if not instrument_ids or not trade_dates:
        return {}
    first, last = trade_dates[0], trade_dates[-1]
    stmt = (
        select(BarDaily)
        .where(
            BarDaily.instrument_id.in_(instrument_ids),
            BarDaily.trade_date >= first - timedelta(days=_BAR_LOOKBACK_DAYS),
            BarDaily.trade_date <= last,
        )
        .order_by(BarDaily.instrument_id.asc(), BarDaily.trade_date.asc())
    )
    by_instrument: dict[uuid.UUID, list[DailyBarFact]] = defaultdict(list)
    for bar in (await session.execute(stmt)).scalars():
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
        # Canonical boundary normalization (identical to ``_load_structure_events``).
        etype = _normalize_event_type(row.event_type)
        direction = payload.get("direction")
        level = payload.get("level")
        internal_raw = payload.get("internal")
        internal = bool(internal_raw) if isinstance(internal_raw, bool) else None
        release_ratio = payload.get("release_volume_ratio")
        event_time = row.event_time
        if not event_time:
            # The query bounds ``event_time`` to [first, last+1d), so a NULL here
            # would be a data anomaly; skip rather than crash or mis-group.
            continue
        grouped[event_time[:10]].append(
            StructureEvent(
                member_id=str(row.instrument_id),
                event_type=etype,
                direction=direction,
                level=(float(level) if isinstance(level, (int, float)) else None),
                internal=internal,
                release_volume_ratio=(
                    float(release_ratio) if isinstance(release_ratio, (int, float)) else None
                ),
            )
        )
    return {
        date.fromisoformat(prefix): events
        for prefix, events in grouped.items()
        if date.fromisoformat(prefix) in set(trade_dates)
    }


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
        structure_events = events_by_date.get(t, [])
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
    """Build per-scope ``PreparedScope`` series by slicing a shared union fact context.

    ``scope_members`` maps ``scope_key -> (member_ids, scope_name)``.  The loop
    order is **date -> union member -> scope slice** (VEC-1): for every
    trade_date the canonical ``_build_member_observations`` owner runs ONCE over
    the union of member_ids, then each scope SELECTS the resulting immutable
    ``MemberObservation`` by reference in its own membership order.  A member
    shared by N scopes (e.g. one stock in many concept boards) is therefore
    constructed once per date instead of N times, while the result for a single
    scope stays byte-identical to calling
    :func:`prepare_scope_series_from_member_ids` for that scope alone.
    """
    if not trade_dates or not scope_members:
        return {}
    import time

    t0 = time.perf_counter()

    # ---- Precompute stable scope context ONCE (out of the date loop) ----
    # Scope member order / name / string tuples never change across the replay;
    # building them here instead of per-T removes repeated tuple/list/set
    # construction on every trade_date.  ``member_set`` (VEC-1-CORRECTION) is the
    # scope-specific membership used to filter the union's structure events so
    # ``PreparedScope.events`` stays strictly scope-local.
    scope_meta: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], set[str]]] = {}
    union_ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    scope_member_day_count = 0
    for scope_key, (member_ids, scope_name) in scope_members.items():
        ids = tuple(member_ids)
        scope_member_day_count += len(ids)
        str_ids = tuple(str(i) for i in ids)
        scope_meta[scope_key] = (
            scope_name,
            str_ids,
            # current-static: the T-1 membership equals the T membership.
            str_ids,
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

    out: dict[str, list[PreparedScope]] = {k: [] for k in scope_members}
    t_loop = time.perf_counter()
    for t in trade_dates:
        t1 = union_ctx.t1_by_date.get(t)
        states_t = union_ctx.states_by_date.get(t, {})
        states_t1 = union_ctx.states_by_date.get(t1, {}) if t1 else {}
        structure_events = union_ctx.events_by_date.get(t, [])

        # VEC-1: ONE canonical member build for the whole union per trade_date.
        # A member shared by N scopes is constructed exactly once, not N times.
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
            current_only_facts={},
            vec_volume=union_ctx.vec_volume,
            counters=batch_counters,
            fallback_reasons=batch_fallback_reasons,
        )
        member_by_id = {m.member_id: m for m in union_members}

        for scope_key, (scope_name, str_ids, str_ids_t1, member_set) in scope_meta.items():
            members = tuple(
                member_by_id[sid] for sid in str_ids if sid in member_by_id
            )
            # VEC-1-CORRECTION: the union's events are filtered to this scope's
            # membership so PreparedScope.events stays strictly scope-local (the
            # Scope Core would drop out-of-scope events anyway, but the contract
            # is that a PreparedScope carries ONLY its own members' events).
            scope_events = tuple(
                e for e in structure_events if e.member_id in member_set
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
        len(scope_members), len(union_ids), len(trade_dates),
        scope_member_day_count, unique_member_day_count, duplication_factor,
        len(trade_dates),
        batch_counters.get("vec_hit", 0), batch_counters.get("vec_fallback", 0),
        ",".join(batch_fallback_reasons) or "-",
        loop_ms, total_ms,
    )
    return out


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
