"""Minimal V3.2 input loader — DB facts -> canonical loaded inputs.

This is deliberately narrow: it only produces what V3.2 needs today.  It is not
a general Auction data platform.

It READS and CONVERTS only.  It never computes EW/AW gap, HHI, position,
dynamics, contribution or leadership — those belong to the pure domain owners
(:mod:`app.domain.auction.analysis_preparation`).

Query shape: a FIXED number of bounded bulk reads (universe, current quotes,
historical quotes, calendar slots, membership edges).  No scope x member x day
loop, so no N+1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auction.member_observation import (
    AuctionMemberObservation,
    build_member_observation,
)
from app.domain.auction.membership_pit import (
    FAMILY_CONCEPT,
    FAMILY_INDUSTRY,
    MembershipEdge,
)
from app.models.auction import AuctionFinalQuote, AuctionQuoteCaptureRun
from app.models.board_taxonomy import (
    BoardDefinitionVersion,
    BoardMembershipHistory,
)
from app.models.calendar import TradingCalendar
from app.models.instrument import Instrument
from app.models.market_board import MarketBoard

__all__ = [
    "V32Inputs",
    "V32InputUnavailableError",
    "load_v32_inputs",
    "resolve_verified_consensus_capture",
]

#: canonical V3.2 history lane
HISTORICAL_SOURCE = "historical_backfill"
HISTORICAL_NAMESPACE = "historical_backfill"

#: the only capture source allowed as V3.2 "current T"
VERIFIED_CONSENSUS_SOURCE = "verified_consensus"

#: industry / concept are two separate peer universes, never merged
FAMILIES = (FAMILY_INDUSTRY, FAMILY_CONCEPT)

_MISSING_QUOTE = "missing_current_auction_quote"


class V32InputUnavailableError(RuntimeError):
    """V3.2 cannot run: a required production input is absent.

    Fail-closed by design — never fall back to a raw provider capture.
    """


@dataclass(frozen=True)
class V32Inputs:
    """Everything the pure preparation owner needs, already converted."""

    trade_date: date
    capture_run_id: UUID
    trade_slots: tuple[date, ...]
    current_observations: tuple[AuctionMemberObservation, ...]
    observations_by_date: dict[date, tuple[AuctionMemberObservation, ...]] = field(
        default_factory=dict
    )
    edges: tuple[MembershipEdge, ...] = ()
    expected_universe_ids: tuple[UUID, ...] = ()


# ---------------------------------------------------------------------------
# 3A / 2.1 current verified-consensus capture lineage
# ---------------------------------------------------------------------------
async def resolve_verified_consensus_capture(
    db: AsyncSession, trade_date: date, *, test_namespace: str = "production"
) -> UUID:
    """Resolve the exact verified-consensus capture run for ``trade_date``.

    Fails closed: a raw provider capture must never be substituted for the
    verified consensus result.
    """
    stmt = (
        select(AuctionQuoteCaptureRun.id)
        .where(
            AuctionQuoteCaptureRun.trade_date == trade_date,
            AuctionQuoteCaptureRun.source == VERIFIED_CONSENSUS_SOURCE,
            AuctionQuoteCaptureRun.status == "succeeded",
            AuctionQuoteCaptureRun.test_namespace == test_namespace,
        )
        .order_by(AuctionQuoteCaptureRun.finished_at.desc())
        .limit(1)
    )
    capture_id = (await db.execute(stmt)).scalar_one_or_none()
    if capture_id is None:
        raise V32InputUnavailableError(
            f"no succeeded {VERIFIED_CONSENSUS_SOURCE} capture for {trade_date} "
            f"(namespace={test_namespace}); refusing to fall back to a raw capture"
        )
    return capture_id


# ---------------------------------------------------------------------------
# 2.4 official trading slots (repository calendar owner)
# ---------------------------------------------------------------------------
async def load_official_trade_slots(
    db: AsyncSession,
    trade_date: date,
    *,
    window: int,
) -> tuple[date, ...]:
    """Return the official trading observation slots: ``D-window .. T``.

    Uses the repository trading-calendar owner.  A session with no quote keeps
    its slot with unavailable values — slots are never rebuilt from the dates
    that happen to have data.
    """
    stmt = (
        select(TradingCalendar.trade_date)
        .where(
            TradingCalendar.trade_date <= trade_date,
            TradingCalendar.is_trading_day.is_(True),
            TradingCalendar.market == "A",
        )
        .order_by(TradingCalendar.trade_date.desc())
        .limit(window + 1)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    if trade_date not in rows:
        raise V32InputUnavailableError(
            f"{trade_date} is not an official A-share trading day"
        )
    return tuple(sorted(rows))


# ---------------------------------------------------------------------------
# 2.2 expected universe
# ---------------------------------------------------------------------------
def expected_active_ashare_stmt():
    """Active A-share口径 — the single production owner, shared with the Auction
    capture / consensus path.

    ``status == active`` AND a 6-digit numeric symbol.  ``Instrument.market`` is
    ``SH`` / ``SZ`` / ``BJ``, never a single ``"A"`` value, so filtering on
    ``market == "A"`` would empty the universe and silently move every missing
    stock out of the coverage denominator.
    """
    return (
        select(Instrument.id)
        .where(
            Instrument.status == "active",
            Instrument.symbol.op("~")(r"^\d{6}$"),
        )
    )


async def load_expected_universe(db: AsyncSession) -> tuple[UUID, ...]:
    """The formal expected active A-share universe (coverage denominator).

    Consumes :func:`expected_active_ashare_stmt` — never ``Instrument.market``.
    """
    return tuple((await db.execute(expected_active_ashare_stmt())).scalars().all())


def _to_observation(row: AuctionFinalQuote) -> AuctionMemberObservation:
    return build_member_observation(
        instrument_id=row.instrument_id,
        trade_date=row.trade_date,
        final_price=row.final_price,
        prev_close=row.prev_close,
        amount=row.amount,
        quality_status=row.quality_status,
        source=row.source,
    )


def _missing_observation(instrument_id: UUID, trade_date: date) -> AuctionMemberObservation:
    """An expected instrument with no quote: present, but unavailable.

    Keeping it means coverage is ``valid / expected`` rather than
    ``returned / returned``.
    """
    return build_member_observation(
        instrument_id=instrument_id,
        trade_date=trade_date,
        final_price=None,
        prev_close=None,
        amount=None,
        quality_status=_MISSING_QUOTE,
        source=_MISSING_QUOTE,
    )


# ---------------------------------------------------------------------------
# 3A current T quotes
# ---------------------------------------------------------------------------
async def _load_current_quotes(
    db: AsyncSession, capture_run_id: UUID, trade_date: date
) -> list[AuctionFinalQuote]:
    stmt = select(AuctionFinalQuote).where(
        AuctionFinalQuote.capture_run_id == capture_run_id,
        AuctionFinalQuote.trade_date == trade_date,
    )
    return list((await db.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# 2.3 historical lane (historical_backfill only)
# ---------------------------------------------------------------------------
async def _load_historical_quotes(
    db: AsyncSession, trade_date: date, slots: tuple[date, ...]
) -> list[AuctionFinalQuote]:
    pre_t = [d for d in slots if d < trade_date]
    if not pre_t:
        return []
    stmt = select(AuctionFinalQuote).where(
        AuctionFinalQuote.trade_date.in_(pre_t),
        AuctionFinalQuote.source == HISTORICAL_SOURCE,
        AuctionFinalQuote.test_namespace == HISTORICAL_NAMESPACE,
    )
    return list((await db.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# 2.5 PIT membership -> MembershipEdge with MarketBoard.externalCode identity
# ---------------------------------------------------------------------------
async def _load_membership_edges(
    db: AsyncSession, trade_date: date, window_start: date
) -> tuple[MembershipEdge, ...]:
    """One bounded join; PIT validity is ``[effective_from, effective_to)``.

    Both the membership row AND its ``BoardDefinitionVersion`` must overlap the
    window ``[window_start, T]`` (half-open on both ends).  A membership row can
    still overlap while the board definition that created it has already ended,
    which would leak a stale board into the scope — so the definition version's
    own PIT window is enforced too.

    ``scope_key`` is ``MarketBoard.externalCode`` (the business identity); the
    board UUID never becomes a product key.
    """
    stmt = (
        select(
            MarketBoard.externalCode,
            MarketBoard.name,
            BoardDefinitionVersion.board_type,
            BoardMembershipHistory.instrument_id,
            BoardMembershipHistory.effective_from,
            BoardMembershipHistory.effective_to,
            BoardDefinitionVersion.effective_from,
            BoardDefinitionVersion.effective_to,
        )
        .join(
            BoardDefinitionVersion,
            BoardDefinitionVersion.id == BoardMembershipHistory.board_definition_version_id,
        )
        .join(MarketBoard, MarketBoard.id == BoardDefinitionVersion.board_id)
        .where(
            BoardDefinitionVersion.board_type.in_(FAMILIES),
            # membership row overlaps the whole window
            BoardMembershipHistory.effective_from <= trade_date,
            or_(
                BoardMembershipHistory.effective_to.is_(None),
                BoardMembershipHistory.effective_to > window_start,
            ),
            # BoardDefinitionVersion overlaps the window too
            BoardDefinitionVersion.effective_from <= trade_date,
            or_(
                BoardDefinitionVersion.effective_to.is_(None),
                BoardDefinitionVersion.effective_to > window_start,
            ),
        )
    )
    rows = (await db.execute(stmt)).all()

    edges: list[MembershipEdge] = []
    for (
        external_code,
        name,
        board_type,
        instrument_id,
        eff_from,
        eff_to,
        def_from,
        def_to,
    ) in rows:
        # The effective edge interval is the INTERSECTION of the membership row
        # and its BoardDefinitionVersion PIT intervals.  Keeping only the
        # membership interval (the previous bug) leaked future / stale definition
        # versions into historical Scope Facts — a point-in-time violation.
        edge_from = eff_from if eff_from >= def_from else def_from
        edge_to = _earliest_non_null(eff_to, def_to)
        if edge_to is not None and edge_to <= edge_from:
            # the two PIT intervals do not actually overlap
            continue
        edges.append(
            MembershipEdge(
                instrument_id=instrument_id,
                scope_key=external_code,
                scope_name=name,
                family=board_type,
                effective_from=edge_from,
                effective_to=edge_to,
            )
        )
    return tuple(edges)


def _earliest_non_null(a: date | None, b: date | None) -> date | None:
    """Return the earlier of two (possibly ``None``) exclusive end dates."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a < b else b


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
async def load_v32_inputs(
    db: AsyncSession,
    *,
    trade_date: date,
    capture_run_id: UUID,
    window: int = 120,
) -> V32Inputs:
    """Load every V3.2 input with a fixed number of bounded bulk reads."""
    slots = await load_official_trade_slots(db, trade_date, window=window)
    universe = await load_expected_universe(db)
    current_rows = await _load_current_quotes(db, capture_run_id, trade_date)
    history_rows = await _load_historical_quotes(db, trade_date, slots)
    edges = await _load_membership_edges(db, trade_date, slots[0])

    current_observations = [_to_observation(r) for r in current_rows]
    quoted_ids = {r.instrument_id for r in current_rows}

    # expected instruments with no quote still enter the coverage denominator
    for instrument_id in universe:
        if instrument_id not in quoted_ids:
            current_observations.append(_missing_observation(instrument_id, trade_date))

    by_date: dict[date, list[AuctionMemberObservation]] = {d: [] for d in slots}
    for row in history_rows:
        if row.trade_date in by_date:
            by_date[row.trade_date].append(_to_observation(row))
    by_date[trade_date] = current_observations

    return V32Inputs(
        trade_date=trade_date,
        capture_run_id=capture_run_id,
        trade_slots=slots,
        current_observations=tuple(current_observations),
        observations_by_date={d: tuple(v) for d, v in by_date.items()},
        edges=edges,
        expected_universe_ids=universe,
    )
