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
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.member_fact import DailyBarFact, previous_state_to_flat
from app.domain.review.scope_observation import MemberObservation
from app.models.bar import BarDaily
from app.models.first_pyramid_history import FirstPyramidHistoryDailyState
from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION
from app.services import calendar_service, review_scope_service
from app.services.board_membership_service import PITMembershipUnavailableError
from app.services.observation_prep import RawMemberFacts, build_member_observation

logger = logging.getLogger("review_observation_prep_service")

# Bar history window for the shared vol/amt ratio SSOT (same as Review pipeline).
_RATIO_WINDOW = 20
_BAR_LOOKBACK_DAYS = 400

# Market has no historical PIT membership table (universe_memberships empty);
# T-1 market membership is unavailable by contract — never substitute current.
HISTORICAL_PIT_UNAVAILABLE_SCOPE_TYPES: frozenset[str] = frozenset({"market"})


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
    stmt = (
        select(FirstPyramidHistoryDailyState)
        .where(
            FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
            FirstPyramidHistoryDailyState.trade_date == trade_date,
            FirstPyramidHistoryDailyState.algorithm_version
            == FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        )
    )
    return {
        row.instrument_id: row.state_payload
        for row in (await session.execute(stmt)).scalars()
    }


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


async def prepare_scope(
    session: AsyncSession,
    scope_type: str,
    scope_key: str,
    trade_date: date,
) -> PreparedScope:
    """Prepare canonical MemberObservation inputs for one scope on ``trade_date``."""
    diagnostics: list[str] = []
    t1 = await calendar_service.get_previous_trading_day_async(session, trade_date)

    # ---- PIT(T) ----
    pit_ids_t: list[uuid.UUID] = []
    scope_name = scope_key
    pit_status_t = "ready"
    try:
        pit_ids_t, scope_name = await review_scope_service.resolve_scope_members(
            session, scope_type, scope_key, trade_date=trade_date,
        )
        if scope_type in HISTORICAL_PIT_UNAVAILABLE_SCOPE_TYPES:
            # market PIT(T) = current active universe (canonical production source),
            # but it is a current snapshot, not true historical PIT.
            pit_status_t = "current_snapshot"
        else:
            pit_status_t = "historical_pit"
    except (PITMembershipUnavailableError, review_scope_service.OptionalScopeUnavailableError) as exc:
        pit_status_t = "unavailable"
        diagnostics.append(f"pit_unavailable_T:{scope_type}/{scope_key} {exc}")
    except review_scope_service.ScopeSnapshotError as exc:
        pit_status_t = "unavailable"
        diagnostics.append(f"scope_error_T:{scope_type}/{scope_key} {exc}")

    # ---- PIT(T-1) ----
    pit_ids_t1: list[uuid.UUID] = []
    t1_membership_available = False
    pit_status_t1 = "unavailable"
    if scope_type in HISTORICAL_PIT_UNAVAILABLE_SCOPE_TYPES:
        pit_status_t1 = "unavailable"
        diagnostics.append(
            "market_historical_pit_t1_unavailable: "
            "no historical market membership source; current not substituted"
        )
    elif t1 is not None and pit_status_t != "unavailable":
        try:
            pit_ids_t1, _ = await review_scope_service.resolve_scope_members(
                session, scope_type, scope_key, trade_date=t1,
            )
            pit_status_t1 = "historical_pit"
            t1_membership_available = True
        except (PITMembershipUnavailableError, review_scope_service.OptionalScopeUnavailableError) as exc:
            pit_status_t1 = "unavailable"
            diagnostics.append(f"pit_unavailable_T1:{scope_type}/{scope_key} {exc}")
        except review_scope_service.ScopeSnapshotError as exc:
            pit_status_t1 = "unavailable"
            diagnostics.append(f"scope_error_T1:{scope_type}/{scope_key} {exc}")
    elif t1 is None:
        diagnostics.append("canonical_t1_unavailable: no previous trading day")

    if pit_status_t == "unavailable":
        return PreparedScope(
            scope_type=scope_type, scope_key=scope_key, scope_name=scope_name,
            trade_date=trade_date, canonical_t1=t1,
            pit_member_ids=(), pit_member_ids_t1=tuple(str(i) for i in pit_ids_t1),
            members=(), t1_membership_available=t1_membership_available,
            pit_status_t=pit_status_t, pit_status_t1=pit_status_t1,
            diagnostics=tuple(diagnostics),
        )

    # ---- Facts ----
    states_t = await _load_states(session, pit_ids_t, trade_date)
    states_t1 = await _load_states(session, pit_ids_t, t1) if t1 else {}
    bar_facts = await _load_bar_facts(session, pit_ids_t, trade_date)
    t1_facts = await _load_bar_facts(session, pit_ids_t, t1) if t1 else {}

    members: list[MemberObservation] = []
    for inst_id in pit_ids_t:
        state_t = states_t.get(inst_id)
        if state_t is None:
            # Not a valid canonical First Pyramid member/fact at T -> not provided.
            continue
        facts = bar_facts.get(inst_id, [])
        current = facts[-1] if facts and facts[-1].trade_date == trade_date else None
        volumes = [b.volume for b in facts if b.volume is not None]
        amounts = [b.amount for b in facts if b.amount is not None]
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
                        previous_state_to_flat(states_t1[inst_id])
                        if inst_id in states_t1 else None
                    ),
                    close_t1=t1_bar.close if t1_bar else None,
                )
            )
        )

    return PreparedScope(
        scope_type=scope_type, scope_key=scope_key, scope_name=scope_name,
        trade_date=trade_date, canonical_t1=t1,
        pit_member_ids=tuple(str(i) for i in pit_ids_t),
        pit_member_ids_t1=tuple(str(i) for i in pit_ids_t1),
        members=tuple(members),
        t1_membership_available=t1_membership_available,
        pit_status_t=pit_status_t, pit_status_t1=pit_status_t1,
        diagnostics=tuple(diagnostics),
    )
