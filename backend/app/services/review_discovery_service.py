"""Review V2 Discovery service."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.cross_scope_relation import CrossScopeRelation, compute_relations
from app.domain.review.discovery import Discovery, build_discovery
from app.models.market_review import (
    MarketReviewRun, MarketReviewScopeSnapshot, MarketReviewSignal,
    MarketReviewSignalInstrument, MarketReviewSignalAttribution,
)
from app.models.board_taxonomy import BoardMembershipHistory


async def build_discoveries_for_run(
    session: AsyncSession, run: MarketReviewRun,
) -> list[Discovery]:
    snap_stmt = select(MarketReviewScopeSnapshot).where(
        MarketReviewScopeSnapshot.review_run_id == run.id)
    snapshots = list((await session.execute(snap_stmt)).scalars())

    sig_stmt = select(MarketReviewSignal).where(
        MarketReviewSignal.review_run_id == run.id)
    signals = list((await session.execute(sig_stmt)).scalars())

    signals_by_scope: dict[tuple[str, str], list[MarketReviewSignal]] = {}
    for sig in signals:
        signals_by_scope.setdefault((sig.scope_type, sig.scope_key), []).append(sig)

    # Also load previous-day signals for lifecycle
    prev_sig_stmt = (
        select(MarketReviewSignal)
        .where(
            MarketReviewSignal.trade_date < run.trade_date,
            MarketReviewSignal.status.in_(
                ("new", "continuing", "confirmed", "weakened", "invalidated", "transformed")),
        )
        .order_by(MarketReviewSignal.trade_date.desc())
        .limit(500)
    )
    prev_signals = list((await session.execute(prev_sig_stmt)).scalars())
    prev_by_scope: dict[tuple[str, str], list[MarketReviewSignal]] = {}
    for ps in prev_signals:
        prev_by_scope.setdefault((ps.scope_type, ps.scope_key), []).append(ps)

    discoveries: list[Discovery] = []
    trade_date_str = run.trade_date.isoformat() if isinstance(run.trade_date, date) else str(run.trade_date)

    for snap in snapshots:
        scope_signals = signals_by_scope.get((snap.scope_type, snap.scope_key), [])
        signal_ids = [str(s.id) for s in scope_signals]
        prev_scope_signals = prev_by_scope.get((snap.scope_type, snap.scope_key), [])

        discovery = build_discovery(
            run_id=str(run.id), trade_date=trade_date_str,
            scope_type=snap.scope_type, scope_key=snap.scope_key,
            scope_name=snap.scope_name or snap.scope_key,
            p_payload=snap.p_payload, q_payload=snap.q_payload,
            u_payload=snap.u_payload, c_payload=snap.c_payload, v_payload=snap.v_payload,
            signal_ids=signal_ids,
            coverage=float(snap.coverage_ratio) if snap.coverage_ratio else 0.0,
            ready_count=snap.ready_count or 0,
        )
        if discovery is None:
            continue

        _apply_lifecycle(discovery, scope_signals, prev_scope_signals, run.trade_date)
        discovery.representative_instruments = await _collect_representative_instruments(
            session, signal_ids)
        discoveries.append(discovery)

    return discoveries


async def build_scope_memberships(
    session: AsyncSession, run: MarketReviewRun, scope_keys: set[str],
) -> dict[str, set[str]]:
    if not scope_keys:
        return {}
    valid_keys = []
    for k in scope_keys:
        try:
            valid_keys.append(uuid.UUID(k))
        except ValueError:
            pass
    if not valid_keys:
        return {}
    stmt = select(BoardMembershipHistory).where(
        BoardMembershipHistory.board_id.in_(valid_keys),
        BoardMembershipHistory.effective_from <= run.trade_date,
        (BoardMembershipHistory.effective_to.is_(None)
         | (BoardMembershipHistory.effective_to > run.trade_date)),
    )
    result = await session.execute(stmt)
    memberships: dict[str, set[str]] = {}
    for m in result.scalars():
        memberships.setdefault(str(m.board_id), set()).add(str(m.instrument_id))
    return memberships


async def compute_cross_scope_relations(
    discoveries: list[Discovery], session=None, run=None,
) -> list[CrossScopeRelation]:
    discovery_dicts = [d.to_dict() for d in discoveries]
    scope_keys = {d.scope_key for d in discoveries}
    memberships = {}
    if session and run and scope_keys:
        memberships = await build_scope_memberships(session, run, scope_keys)
    return compute_relations(discovery_dicts, scope_memberships=memberships)


def rank_discoveries(
    discoveries: list[Discovery], relations=None,
) -> list[tuple[Discovery, dict[str, float]]]:
    relation_map: dict[str, set[str]] = {}
    if relations:
        for r in relations:
            relation_map.setdefault(r.source_scope, set()).add(r.relation_type)
            relation_map.setdefault(r.target_scope, set()).add(r.relation_type)

    scored = []
    for d in discoveries:
        details: dict[str, float] = {}

        # anomaly (0-40)
        a = 0.0
        for v in d.anomaly.self_historical.values():
            if v is not None:
                a = max(a, abs(v - 50) * 0.8)
        details["anomaly"] = round(min(a, 40), 1)

        # change (0-25)
        c = 0.0
        for m in d.change.metrics.values():
            if m.delta1d is not None:
                c = max(c, abs(m.delta1d))
        details["change"] = round(min(c * 2.5, 25), 1)

        # evidenceConsistency (0-15)
        details["evidenceConsistency"] = round(min(len(d.supporting_signal_ids) * 3, 15), 1)

        # crossScopeConfirmation (0-10)
        rel_types = relation_map.get(d.discovery_id, set())
        if "BROAD_CONFIRMATION" in rel_types:
            details["crossScopeConfirmation"] = 10.0
        elif "INDUSTRY_LED" in rel_types or "THEME_LED" in rel_types:
            details["crossScopeConfirmation"] = 5.0
        elif "STYLE_LED" in rel_types:
            details["crossScopeConfirmation"] = 5.0
        else:
            details["crossScopeConfirmation"] = 0.0

        # coverage (0-10)
        details["coverage"] = round(min(d.coverage * 10, 10), 1)

        # duration (0-5)
        details["duration"] = min(d.duration * 1.0, 5.0)

        # breadth (0-5) — participation evidence from U
        b = 0.0
        if d.state.internal_structure.momentum_breadth is not None:
            b = d.state.internal_structure.momentum_breadth * 0.05
        details["breadth"] = round(min(b, 5), 1)

        total = sum(details.values())
        scored.append((d, total, details))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [(d, details) for d, _, details in scored]


async def get_discovery_by_id(session, discovery_id, trade_date=None) -> Discovery | None:
    if trade_date:
        run_stmt = select(MarketReviewRun).where(
            MarketReviewRun.trade_date == trade_date, MarketReviewRun.status == "published",
        ).order_by(MarketReviewRun.created_at.desc()).limit(1)
    else:
        run_stmt = select(MarketReviewRun).where(
            MarketReviewRun.status == "published",
        ).order_by(MarketReviewRun.trade_date.desc()).limit(1)
    run = (await session.execute(run_stmt)).scalar_one_or_none()
    if run is None:
        return None
    discoveries = await build_discoveries_for_run(session, run)
    for d in discoveries:
        if d.discovery_id == discovery_id:
            return d
    return None


async def list_discovery_attributions(
    session, signal_ids, page=1, page_size=20,
) -> tuple[list, int]:
    if not signal_ids:
        return [], 0
    try:
        ids = [uuid.UUID(sid) for sid in signal_ids]
    except ValueError:
        return [], 0
    subq = select(
        MarketReviewSignalAttribution.id,
        func.row_number().over(
            partition_by=MarketReviewSignalAttribution.child_scope_key,
            order_by=MarketReviewSignalAttribution.contribution_rank,
        ).label("rn"),
    ).where(MarketReviewSignalAttribution.signal_id.in_(ids)).subquery()
    stmt = select(MarketReviewSignalAttribution).join(
        subq, MarketReviewSignalAttribution.id == subq.c.id,
    ).where(subq.c.rn == 1).order_by(
        MarketReviewSignalAttribution.contribution_rank,
    ).offset((page - 1) * page_size).limit(page_size)
    items = list((await session.execute(stmt)).scalars())
    count_stmt = select(func.count(func.distinct(
        MarketReviewSignalAttribution.child_scope_key))).where(
        MarketReviewSignalAttribution.signal_id.in_(ids))
    total = (await session.execute(count_stmt)).scalar() or 0
    return items, total


async def list_discovery_instruments(
    session, signal_ids, page=1, page_size=20,
) -> tuple[list, int]:
    if not signal_ids:
        return [], 0
    try:
        ids = [uuid.UUID(sid) for sid in signal_ids]
    except ValueError:
        return [], 0
    subq = select(
        MarketReviewSignalInstrument.id,
        func.row_number().over(
            partition_by=MarketReviewSignalInstrument.instrument_id,
            order_by=MarketReviewSignalInstrument.contribution_rank,
        ).label("rn"),
    ).where(MarketReviewSignalInstrument.signal_id.in_(ids)).subquery()
    stmt = select(MarketReviewSignalInstrument).join(
        subq, MarketReviewSignalInstrument.id == subq.c.id,
    ).where(subq.c.rn == 1).order_by(
        MarketReviewSignalInstrument.contribution_rank,
    ).offset((page - 1) * page_size).limit(page_size)
    items = list((await session.execute(stmt)).scalars())
    count_stmt = select(func.count(func.distinct(
        MarketReviewSignalInstrument.instrument_id))).where(
        MarketReviewSignalInstrument.signal_id.in_(ids))
    total = (await session.execute(count_stmt)).scalar() or 0
    return items, total


# =============================================================================
# Lifecycle
# =============================================================================


def _apply_lifecycle(
    discovery: Discovery,
    scope_signals: list[MarketReviewSignal],
    prev_signals: list[MarketReviewSignal],
    current_trade_date: date,
) -> None:
    if not scope_signals:
        discovery.status = "new"
        return

    statuses = {s.status for s in scope_signals}
    if "confirmed" in statuses:
        discovery.status = "confirmed"
    elif "weakened" in statuses:
        discovery.status = "weakened"
    elif "invalidated" in statuses:
        discovery.status = "invalidated"
    elif "transformed" in statuses:
        discovery.status = "transformed"
    elif "continuing" in statuses:
        discovery.status = "continuing"
    else:
        discovery.status = "new"

    # Duration: count distinct trade_dates where signals exist for this scope
    all_signals = scope_signals + prev_signals
    trade_dates = set()
    for s in all_signals:
        if s.trade_date:
            trade_dates.add(s.trade_date)
    discovery.duration = len(trade_dates)

    # First seen: earliest trade_date across current + previous signals
    earliest = None
    for s in all_signals:
        if s.trade_date and (earliest is None or s.trade_date < earliest):
            earliest = s.trade_date
    if earliest:
        discovery.first_seen = earliest.isoformat() if hasattr(earliest, 'isoformat') else str(earliest)


async def _collect_representative_instruments(
    session, signal_ids, limit=10,
) -> list[dict[str, Any]]:
    if not signal_ids:
        return []
    try:
        ids = [uuid.UUID(sid) for sid in signal_ids]
    except ValueError:
        return []
    subq = select(
        MarketReviewSignalInstrument.id,
        func.row_number().over(
            partition_by=MarketReviewSignalInstrument.instrument_id,
            order_by=MarketReviewSignalInstrument.contribution_rank,
        ).label("rn"),
    ).where(MarketReviewSignalInstrument.signal_id.in_(ids)).subquery()
    stmt = select(MarketReviewSignalInstrument).join(
        subq, MarketReviewSignalInstrument.id == subq.c.id,
    ).where(subq.c.rn == 1).order_by(
        MarketReviewSignalInstrument.contribution_rank,
    ).limit(limit)
    instruments = list((await session.execute(stmt)).scalars())
    return [{
        "instrumentId": str(i.instrument_id),
        "boardRole": i.board_role,
        "relationToScope": i.relation_to_scope,
        "contributionValue": float(i.contribution_value) if i.contribution_value else None,
        "contributionRank": i.contribution_rank,
        "contributionPayload": i.contribution_payload,
        "roleEvidence": i.role_evidence,
    } for i in instruments]
