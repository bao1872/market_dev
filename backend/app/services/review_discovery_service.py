"""Review V2 Discovery service.

Orchestrates Discovery aggregation from atomic Signal evidence,
State/Change/Anomaly projection, Cross-Scope Relation, and global ranking.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.cross_scope_relation import (
    CrossScopeRelation,
    compute_relations,
)
from app.domain.review.discovery import (
    Discovery,
    build_discovery,
)
from app.models.market_review import (
    MarketReviewRun,
    MarketReviewScopeSnapshot,
    MarketReviewSignal,
)


async def build_discoveries_for_run(
    session: AsyncSession,
    run: MarketReviewRun,
) -> list[Discovery]:
    """为指定 Review Run 构建全部 Discovery。

    消费所有 scope snapshot 和 signal，聚合为 Discovery。
    返回空列表表示无 scope 满足 eligibility 条件（合法状态）。
    """
    # 加载所有 scope snapshots
    snap_stmt = (
        select(MarketReviewScopeSnapshot)
        .where(MarketReviewScopeSnapshot.review_run_id == run.id)
    )
    snap_result = await session.execute(snap_stmt)
    snapshots = list(snap_result.scalars())

    # 加载所有 signals（atomic evidence）
    sig_stmt = (
        select(MarketReviewSignal)
        .where(MarketReviewSignal.review_run_id == run.id)
    )
    sig_result = await session.execute(sig_stmt)
    signals = list(sig_result.scalars())

    # 按 scope 分组 signals
    signals_by_scope: dict[tuple[str, str], list[str]] = {}
    for sig in signals:
        key = (sig.scope_type, sig.scope_key)
        signals_by_scope.setdefault(key, []).append(str(sig.id))

    # 为每个 scope 构建 Discovery
    discoveries: list[Discovery] = []
    trade_date_str = run.trade_date.isoformat() if isinstance(run.trade_date, date) else str(run.trade_date)

    for snap in snapshots:
        if snap.scope_type == "market":
            continue  # market 是全市场基准，不产生独立 Discovery

        discovery = build_discovery(
            run_id=str(run.id),
            trade_date=trade_date_str,
            scope_type=snap.scope_type,
            scope_key=snap.scope_key,
            scope_name=snap.scope_name or snap.scope_key,
            p_payload=snap.p_payload,
            q_payload=snap.q_payload,
            u_payload=snap.u_payload,
            c_payload=snap.c_payload,
            v_payload=snap.v_payload,
            signal_ids=signals_by_scope.get((snap.scope_type, snap.scope_key), []),
            coverage=snap.coverage_ratio or 0.0,
            ready_count=snap.ready_count or 0,
        )
        if discovery is not None:
            discoveries.append(discovery)

    return discoveries


async def compute_cross_scope_relations(
    discoveries: list[Discovery],
    session: AsyncSession | None = None,
) -> list[CrossScopeRelation]:
    """为 Discovery 列表计算 Cross-Scope Relation。"""
    discovery_dicts = [d.to_dict() for d in discoveries]

    # Membership overlap: simplified — use representative_instruments overlap
    membership_overlap: dict[tuple[str, str], float] = {}
    for i, d1 in enumerate(discoveries):
        for j, d2 in enumerate(discoveries):
            if i >= j:
                continue
            k1 = (d1.scope_type, d1.scope_key)
            k2 = (d2.scope_type, d2.scope_key)
            inst1 = {ri.get("instrumentId") for ri in d1.representative_instruments if ri.get("instrumentId")}
            inst2 = {ri.get("instrumentId") for ri in d2.representative_instruments if ri.get("instrumentId")}
            if inst1 and inst2:
                overlap = len(inst1 & inst2) / max(len(inst1 | inst2), 1)
                membership_overlap[(d1.scope_key, d2.scope_key)] = overlap

    return compute_relations(discovery_dicts, membership_overlap)


def rank_discoveries(
    discoveries: list[Discovery],
    relations: list[CrossScopeRelation] | None = None,
) -> list[Discovery]:
    """Global rank → sort。先全量 rank，再返回排序后列表。

    rank_key 可解释维度：anomaly, change, breadth, evidence, cross-scope confirmation。
    """
    scored: list[tuple[Discovery, float]] = []
    relation_set: dict[str, set[str]] = {}
    if relations:
        for r in relations:
            relation_set.setdefault(r.source_scope, set()).add(r.relation_type)
            relation_set.setdefault(r.target_scope, set()).add(r.relation_type)

    for d in discoveries:
        score = 0.0
        # Anomaly strength (0-40)
        max_anomaly = 0.0
        for v in d.anomaly.self_historical.values():
            if v is not None:
                max_anomaly = max(max_anomaly, abs(v - 50) * 0.8)
        score += min(max_anomaly, 40)

        # Change strength (0-25)
        max_delta = 0.0
        for m in d.change.metrics.values():
            if m.delta1d is not None:
                max_delta = max(max_delta, abs(m.delta1d))
        score += min(max_delta * 500, 25)

        # Breadth / evidence consistency (0-15)
        score += min(len(d.key_evidence) * 3, 15)

        # Cross-scope confirmation (0-10)
        rel_types = relation_set.get(d.discovery_id, set())
        if "BROAD_CONFIRMATION" in rel_types:
            score += 10
        elif "INDUSTRY_LED" in rel_types or "THEME_LED" in rel_types:
            score += 5

        # Coverage (0-10)
        score += min(d.coverage * 10, 10)

        scored.append((d, round(score, 1)))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [d for d, _ in scored]


async def get_discovery_by_id(
    session: AsyncSession,
    discovery_id: str,
    trade_date: date | None = None,
) -> Discovery | None:
    """按 discovery_id 查找 Discovery（从已发布的 Review Run 重建）。"""
    # Discovery identity 是 run_id:scope_type:scope_key 的 hash
    # 从 identity 反查需要 scan published runs + snapshots
    if trade_date:
        run_stmt = (
            select(MarketReviewRun)
            .where(
                MarketReviewRun.trade_date == trade_date,
                MarketReviewRun.status == "published",
            )
            .order_by(MarketReviewRun.created_at.desc())
            .limit(1)
        )
    else:
        run_stmt = (
            select(MarketReviewRun)
            .where(MarketReviewRun.status == "published")
            .order_by(MarketReviewRun.trade_date.desc())
            .limit(1)
        )
    run_result = await session.execute(run_stmt)
    run = run_result.scalar_one_or_none()
    if run is None:
        return None

    discoveries = await build_discoveries_for_run(session, run)
    for d in discoveries:
        if d.discovery_id == discovery_id:
            return d
    return None
