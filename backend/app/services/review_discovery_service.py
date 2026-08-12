"""Review V2 Discovery service — canonical Signal lifecycle, unified read model."""

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
    sig_stmt = select(MarketReviewSignal).where(MarketReviewSignal.review_run_id == run.id)
    signals = list((await session.execute(sig_stmt)).scalars())

    signals_by_scope: dict[tuple[str, str], list[MarketReviewSignal]] = {}
    for sig in signals:
        signals_by_scope.setdefault((sig.scope_type, sig.scope_key), []).append(sig)

    trade_date_str = run.trade_date.isoformat() if isinstance(run.trade_date, date) else str(run.trade_date)
    discoveries: list[Discovery] = []

    for snap in snapshots:
        scope_signals = signals_by_scope.get((snap.scope_type, snap.scope_key), [])
        signal_ids = [str(s.id) for s in scope_signals]
        signal_types = [s.signal_type for s in scope_signals]
        signal_families = [s.filter_family for s in scope_signals]
        signal_statuses = [s.status for s in scope_signals]
        signal_first_seens = [
            s.first_seen_date.isoformat() if s.first_seen_date and hasattr(s.first_seen_date, 'isoformat')
            else str(s.first_seen_date) if s.first_seen_date else None
            for s in scope_signals
        ]

        discovery = build_discovery(
            run_id=str(run.id), trade_date=trade_date_str,
            scope_type=snap.scope_type, scope_key=snap.scope_key,
            scope_name=snap.scope_name or snap.scope_key,
            p_payload=snap.p_payload, q_payload=snap.q_payload,
            u_payload=snap.u_payload, c_payload=snap.c_payload, v_payload=snap.v_payload,
            signal_ids=signal_ids, signal_types=signal_types,
            signal_families=signal_families,
            signal_statuses=signal_statuses, signal_first_seens=signal_first_seens,
            coverage=float(snap.coverage_ratio) if snap.coverage_ratio else 0.0,
            ready_count=snap.ready_count or 0,
        )
        if discovery is None:
            continue
        discovery.representative_instruments = await _collect_representative_instruments(
            session, signal_ids)
        discoveries.append(discovery)

    return discoveries


async def build_scope_memberships(session, run, scope_keys) -> dict[str, set[str]]:
    if not scope_keys: return {}
    valid_keys = [uuid.UUID(k) for k in scope_keys if _is_valid_uuid(k)]
    if not valid_keys: return {}
    stmt = select(BoardMembershipHistory).where(
        BoardMembershipHistory.board_id.in_(valid_keys),
        BoardMembershipHistory.effective_from <= run.trade_date,
        (BoardMembershipHistory.effective_to.is_(None) | (BoardMembershipHistory.effective_to > run.trade_date)))
    result = await session.execute(stmt)
    memberships: dict[str, set[str]] = {}
    for m in result.scalars():
        memberships.setdefault(str(m.board_id), set()).add(str(m.instrument_id))
    return memberships

def _is_valid_uuid(s: str) -> bool:
    try: uuid.UUID(s); return True
    except ValueError: return False


async def compute_cross_scope_relations(
    discoveries, session=None, run=None,
) -> list[CrossScopeRelation]:
    discovery_dicts = [d.to_dict() for d in discoveries]
    scope_keys = {d.scope_key for d in discoveries}
    memberships = {}
    if session and run and scope_keys:
        memberships = await build_scope_memberships(session, run, scope_keys)
    return compute_relations(discovery_dicts, scope_memberships=memberships)


def rank_discoveries(discoveries, relations=None) -> list[tuple[Discovery, dict[str, float]]]:
    relation_map: dict[str, set[str]] = {}
    if relations:
        for r in relations:
            relation_map.setdefault(r.source_scope, set()).add(r.relation_type)
            relation_map.setdefault(r.target_scope, set()).add(r.relation_type)
    scored = []
    for d in discoveries:
        details = {}
        a = max((abs(v - 50) * 0.8 for v in d.anomaly.self_historical.values() if v is not None), default=0.0)
        details["anomaly"] = round(min(a, 40), 1)
        c = max((abs(m.delta1d) for m in d.change.metrics.values() if m.delta1d is not None), default=0.0)
        details["change"] = round(min(c * 2.5, 25), 1)
        details["evidenceConsistency"] = round(min(len(d.supporting_signal_ids) * 3, 15), 1)
        rt = relation_map.get(d.discovery_id, set())
        if "BROAD_CONFIRMATION" in rt: details["crossScopeConfirmation"] = 10.0
        elif any(x in rt for x in ("INDUSTRY_LED", "THEME_LED", "STYLE_LED")): details["crossScopeConfirmation"] = 5.0
        else: details["crossScopeConfirmation"] = 0.0
        details["coverage"] = round(min(d.coverage * 10, 10), 1)
        details["duration"] = min(d.duration * 1.0, 5.0)
        b = (d.state.internal_structure.momentum_breadth or 0) * 0.05
        details["breadth"] = round(min(b, 5), 1)
        total = sum(details.values())
        scored.append((d, total, details))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [(d, details) for d, _, details in scored]


async def build_ranked_read_model(
    session: AsyncSession, run: MarketReviewRun,
) -> tuple[list[Discovery], list[CrossScopeRelation], dict[str, Discovery]]:
    """Unified read model assembly: discoveries → relations → rank → attach.

    Returns (ranked_discoveries, relations, discovery_by_id).
    """
    discoveries = await build_discoveries_for_run(session, run)
    relations = await compute_cross_scope_relations(discoveries, session=session, run=run)
    ranked = rank_discoveries(discoveries, relations)

    # Attach rankKey and relatedScopes
    relation_map: dict[str, list[dict]] = {}
    for r in relations:
        rd = r.to_dict()
        relation_map.setdefault(r.source_scope, []).append(rd)
        relation_map.setdefault(r.target_scope, []).append(rd)

    discovery_by_id: dict[str, Discovery] = {}
    for d, details in ranked:
        d.rank_key = details
        d.related_scopes = relation_map.get(d.discovery_id, [])
        discovery_by_id[d.discovery_id] = d

    ranked_discoveries = [d for d, _ in ranked]
    return ranked_discoveries, relations, discovery_by_id


async def get_discovery_by_id(
    session: AsyncSession, discovery_id: str, trade_date: date | None = None,
) -> Discovery | None:
    """Resolve Discovery by ID. Uses run-scoped identity for deterministic lookup.

    Discovery identity includes run_id, so we scan published runs to find the match.
    For the common case (latest run), try latest first.
    """
    # Try latest published run first
    run_stmt = select(MarketReviewRun).where(
        MarketReviewRun.status == "published"
    ).order_by(MarketReviewRun.trade_date.desc()).limit(1)
    run = (await session.execute(run_stmt)).scalar_one_or_none()
    if run is None:
        return None

    # Try the run matching trade_date if provided
    if trade_date and run.trade_date != trade_date:
        alt_stmt = select(MarketReviewRun).where(
            MarketReviewRun.trade_date == trade_date, MarketReviewRun.status == "published",
        ).limit(1)
        alt_run = (await session.execute(alt_stmt)).scalar_one_or_none()
        if alt_run:
            run = alt_run

    discoveries = await build_discoveries_for_run(session, run)
    for d in discoveries:
        if d.discovery_id == discovery_id:
            return d
    return None


async def list_discovery_attributions(session, signal_ids, page=1, page_size=20):
    if not signal_ids: return [], 0
    try: ids = [uuid.UUID(sid) for sid in signal_ids]
    except ValueError: return [], 0
    subq = select(MarketReviewSignalAttribution.id,
        func.row_number().over(partition_by=MarketReviewSignalAttribution.child_scope_key,
            order_by=MarketReviewSignalAttribution.contribution_rank).label("rn"),
    ).where(MarketReviewSignalAttribution.signal_id.in_(ids)).subquery()
    stmt = select(MarketReviewSignalAttribution).join(
        subq, MarketReviewSignalAttribution.id == subq.c.id,
    ).where(subq.c.rn == 1).order_by(MarketReviewSignalAttribution.contribution_rank,
    ).offset((page - 1) * page_size).limit(page_size)
    items = list((await session.execute(stmt)).scalars())
    count_stmt = select(func.count(func.distinct(MarketReviewSignalAttribution.child_scope_key))).where(
        MarketReviewSignalAttribution.signal_id.in_(ids))
    total = (await session.execute(count_stmt)).scalar() or 0
    return items, total


async def list_discovery_instruments(session, signal_ids, page=1, page_size=20):
    if not signal_ids: return [], 0
    try: ids = [uuid.UUID(sid) for sid in signal_ids]
    except ValueError: return [], 0
    subq = select(MarketReviewSignalInstrument.id,
        func.row_number().over(partition_by=MarketReviewSignalInstrument.instrument_id,
            order_by=MarketReviewSignalInstrument.contribution_rank).label("rn"),
    ).where(MarketReviewSignalInstrument.signal_id.in_(ids)).subquery()
    stmt = select(MarketReviewSignalInstrument).join(
        subq, MarketReviewSignalInstrument.id == subq.c.id,
    ).where(subq.c.rn == 1).order_by(MarketReviewSignalInstrument.contribution_rank,
    ).offset((page - 1) * page_size).limit(page_size)
    items = list((await session.execute(stmt)).scalars())
    count_stmt = select(func.count(func.distinct(MarketReviewSignalInstrument.instrument_id))).where(
        MarketReviewSignalInstrument.signal_id.in_(ids))
    total = (await session.execute(count_stmt)).scalar() or 0
    return items, total


async def _collect_representative_instruments(session, signal_ids, limit=10):
    if not signal_ids: return []
    try: ids = [uuid.UUID(sid) for sid in signal_ids]
    except ValueError: return []
    subq = select(MarketReviewSignalInstrument.id,
        func.row_number().over(partition_by=MarketReviewSignalInstrument.instrument_id,
            order_by=MarketReviewSignalInstrument.contribution_rank).label("rn"),
    ).where(MarketReviewSignalInstrument.signal_id.in_(ids)).subquery()
    stmt = select(MarketReviewSignalInstrument).join(
        subq, MarketReviewSignalInstrument.id == subq.c.id,
    ).where(subq.c.rn == 1).order_by(MarketReviewSignalInstrument.contribution_rank).limit(limit)
    instruments = list((await session.execute(stmt)).scalars())
    return [{"instrumentId": str(i.instrument_id), "symbol": i.symbol, "name": i.name,
             "boardRole": i.board_role,
             "relationToScope": i.relation_to_scope,
             "contributionValue": float(i.contribution_value) if i.contribution_value else None,
             "contributionRank": i.contribution_rank,
             "contributionPayload": i.contribution_payload, "roleEvidence": i.role_evidence}
            for i in instruments]
