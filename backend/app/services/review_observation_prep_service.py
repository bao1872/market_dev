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
from dataclasses import dataclass
from datetime import date, timedelta

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
from app.services.observation_prep import RawMemberFacts, build_member_observation

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
    bar_facts: dict[uuid.UUID, list[DailyBarFact]],
    t1_facts: dict[uuid.UUID, list[DailyBarFact]],
    current_only_facts: dict[str, dict[str, object]],
) -> list[MemberObservation]:
    """Build canonical ``MemberObservation`` inputs shared by the per-date and the
    batch replay path (single member-construction owner).

    ``bar_facts`` / ``t1_facts`` are the bar-aligned per-member lists for the
    date's ``[T-400d, T]`` / ``[t1-400d, t1]`` windows (ascending).  STRICT-PRIOR
    history: ``facts`` includes T (window ``<= T``), so T is excluded here —
    ``volume_t`` / ``amount_t`` carry T separately and the canonical volume owner
    appends it exactly once.  Both series are built from the SAME prior bars so
    index i stays aligned to one trade_date across volume and amount.
    """
    members: list[MemberObservation] = []
    for inst_id in pit_ids_t:
        state_t = states_t.get(inst_id)
        if state_t is None:
            # Not a valid canonical First Pyramid member/fact at T -> not provided.
            continue
        facts = bar_facts.get(inst_id, [])
        current = facts[-1] if facts and facts[-1].trade_date == trade_date else None
        prior_bars = [b for b in facts if b.trade_date != trade_date]
        volumes = [b.volume for b in prior_bars]
        amounts = [b.amount for b in prior_bars]
        t1_bars = t1_facts.get(inst_id, [])
        t1_bar = t1_bars[-1] if t1_bars and t1_bars[-1].trade_date == t1 else None
        members.append(
            build_member_observation(
                RawMemberFacts(
                    member_id=str(inst_id),
                    flat_t=previous_state_to_flat(state_t),
                    close_t=current.close if current else None,
                    amount_t=current.amount if current else None,
                    volume_t=current.volume if current else None,
                    volume_history=tuple(volumes),
                    amount_history=tuple(amounts),
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
            )
        )
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
        bar_facts=bar_facts,
        t1_facts=t1_facts,
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
# ----------------------------------------------------------------------------


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
    t1_by_date = await _load_batch_calendar(session, trade_dates)
    states_by_date = await _load_batch_states(session, member_ids, trade_dates, t1_by_date)
    bars = await _load_batch_bars(session, member_ids, trade_dates)
    events_by_date = await _load_batch_events(session, member_ids, trade_dates)

    pit_ids_t = list(member_ids)
    t1_ids = list(pit_member_ids_t1) if pit_member_ids_t1 is not None else list(pit_ids_t)

    out: list[PreparedScope] = []
    for t in trade_dates:
        t1 = t1_by_date.get(t)
        states_t = states_by_date.get(t, {})
        states_t1 = states_by_date.get(t1, {}) if t1 else {}
        bar_facts = {inst: series.window(t) for inst, series in bars.items()}
        t1_facts = {inst: series.window(t1) for inst, series in bars.items()} if t1 else {}
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
            bar_facts=bar_facts,
            t1_facts=t1_facts,
            current_only_facts=current_only_facts,
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
