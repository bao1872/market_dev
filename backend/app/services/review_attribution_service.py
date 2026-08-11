"""复盘归因服务 - 子范围与个股归因持久化（PRD §9）。

职责：
- 对每个命中信号的父范围，扫描第二级下钻子范围（industry_l2/l3/concept）
- 计算子范围对父范围 P/Q/U/C/V 的贡献（attribution_engine）
- 计算个股对父范围的贡献（attribution_engine）
- upsert MarketReviewSignalAttribution / MarketReviewSignalInstrument 记录（幂等）

PRD §9 合同：
- 子范围贡献：保留正贡献和负贡献；按绝对贡献排序；保存前 N 项，API 支持分页
- 个股贡献：每只成员计算对 P/Q/U/C/V 的贡献 + 新鲜事件 + 与板块状态关系
- 归因不得仅按涨幅排序
- 角色分类与因子状态分开保存

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.review_attribution_service
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.attribution_engine import (
    aggregate_child_scope_attributions,
    aggregate_instrument_attributions,
)
from app.models.board_analysis_snapshot import BoardAnalysisSnapshot
from app.models.market_board import MarketBoard
from app.models.market_review import (
    MarketReviewSignal,
    MarketReviewSignalAttribution,
    MarketReviewSignalInstrument,
)
from app.services.board_membership_service import (
    PITMembershipUnavailableError,
    resolve_board_membership_at,
)
from app.services.review_scope_service import (
    SCOPE_PUBLISH_MIN_COVERAGE,
    fetch_member_flat_list,
    resolve_scope_members,
)

logger = logging.getLogger("review_attribution_service")

# 子范围归因默认保留前 N 项（PRD §9.1：保存前 N 项，但 API 支持分页读取全部）
DEFAULT_TOP_N_ATTRIBUTIONS = 20
# 个股归因默认保留前 N 项
DEFAULT_TOP_N_INSTRUMENTS = 30
MIN_CHILD_READY_COUNT = 3


@dataclass(frozen=True)
class ChildScopeCandidate:
    """A PIT child scope with the exact Board batch evidence used for attribution."""

    scope_type: str
    scope_key: str
    scope_name: str
    relation_type: str
    member_ids: tuple[uuid.UUID, ...]
    source_board_snapshot_id: uuid.UUID
    taxonomy_version: str
    taxonomy_compatibility_key: str
    membership_version: str


async def _member_flat_list(
    session: AsyncSession,
    member_ids: list[uuid.UUID],
    source_core_run_id: uuid.UUID,
    *,
    trade_date,
    day_fact_map: dict[uuid.UUID, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """[REVIEW-FACT-PARITY-02 §10] 统一成员 fact 获取入口。

    ``day_fact_map`` 非 None（正式 Review load-once 路径）时只做内存筛选并返回
    **共享引用**（下游只读，禁止 in-place mutation）；为 None 时（独立调试/
    非正式路径）回退旧的 per-scope loader。
    """
    if day_fact_map is None:
        return await fetch_member_flat_list(
            session, member_ids, source_core_run_id, trade_date=trade_date,
        )
    return [
        fact
        for fact in (day_fact_map.get(iid) for iid in member_ids)
        if fact is not None
    ]


# =============================================================================
# 子范围归因
# =============================================================================


async def compute_signal_attributions(
    session: AsyncSession,
    signal: MarketReviewSignal,
    *,
    parent_metrics: dict[str, dict[str, Any]],
    parent_ready_count: int,
    source_core_run_id: uuid.UUID,
    source_board_run_id: uuid.UUID,
    child_scope_types: tuple[str, ...] = ("industry_l2", "industry_l3", "concept"),
    top_n: int | None = None,
    day_fact_map: dict[uuid.UUID, dict[str, Any]] | None = None,
) -> list[MarketReviewSignalAttribution]:
    """Compute every eligible second-level attribution using PIT memberships.

    ``top_n`` is retained only for explicit administrative sampling. Normal runs persist
    every result so API pagination can retrieve both positive and negative contributors.

    [REVIEW-FACT-PARITY-02 §10] ``day_fact_map`` 由正式 ``compute_run`` 一次性加载
    并透传；非 None 时按 member ids 取内存引用，不再对每个 child scope 调用
    ``fetch_member_flat_list``（signal × child_scope 量级的重复读取）。
    """
    parent_ids, _ = await resolve_scope_members(
        session,
        signal.scope_type,
        signal.scope_key,
        trade_date=signal.trade_date,
    )
    if not parent_ids:
        await _delete_attributions(session, signal.id)
        return []

    child_scopes_data: list[dict[str, Any]] = []
    for child_scope_type in child_scope_types:
        candidates = await _list_child_scope_keys(
            session,
            signal.scope_type,
            signal.scope_key,
            child_scope_type,
            trade_date=signal.trade_date,
            source_board_run_id=source_board_run_id,
            parent_instrument_ids=parent_ids,
        )
        for child in candidates:
            eligible_count = len(child.member_ids)
            if eligible_count == 0:
                continue
            flat_list = await _member_flat_list(
                session,
                list(child.member_ids),
                source_core_run_id,
                trade_date=signal.trade_date,
                day_fact_map=day_fact_map,
            )
            ready_count = sum(
                1 for flat in flat_list
                if flat and flat.get("fp_trend_direction") is not None
            )
            coverage_ratio = ready_count / eligible_count
            data_quality = {
                "status": "ready",
                "eligible_count": eligible_count,
                "ready_count": ready_count,
                "coverage_ratio": coverage_ratio,
                "minimum_ready_count": MIN_CHILD_READY_COUNT,
                "minimum_coverage": SCOPE_PUBLISH_MIN_COVERAGE,
            }
            if child_scope_type == "concept" and (
                ready_count < MIN_CHILD_READY_COUNT
                or coverage_ratio < SCOPE_PUBLISH_MIN_COVERAGE
            ):
                continue
            child_scopes_data.append({
                "scope_type": child.scope_type,
                "scope_key": child.scope_key,
                "scope_name": child.scope_name,
                "relation_type": child.relation_type,
                "flat_list": flat_list,
                "eligible_count": eligible_count,
                "ready_count": ready_count,
                "coverage_ratio": coverage_ratio,
                "source_board_snapshot_id": child.source_board_snapshot_id,
                "taxonomy_version": child.taxonomy_version,
                "taxonomy_compatibility_key": child.taxonomy_compatibility_key,
                "membership_version": child.membership_version,
                "parent_scope_type": signal.scope_type,
                "parent_scope_key": signal.scope_key,
                "data_quality": data_quality,
            })

    attributions = aggregate_child_scope_attributions(
        parent_metrics, parent_ready_count, child_scopes_data,
    )
    if top_n is not None:
        attributions = attributions[:top_n]

    await _delete_attributions(session, signal.id)
    created: list[MarketReviewSignalAttribution] = []
    for attr in attributions:
        created.append(await _insert_attribution(session, signal.id, attr))

    logger.info(
        "[ReviewAttribution] signal=%s/%s child_scopes=%d attributions=%d",
        signal.scope_type,
        signal.signal_type,
        len(child_scopes_data),
        len(created),
    )
    return created


async def _insert_attribution(
    session: AsyncSession,
    signal_id: uuid.UUID,
    attr: dict[str, Any],
) -> MarketReviewSignalAttribution:
    """插入归因记录。"""
    record = MarketReviewSignalAttribution(
        signal_id=signal_id,
        child_scope_type=attr["child_scope_type"],
        child_scope_key=attr["child_scope_key"],
        child_scope_name=attr["child_scope_name"],
        relation_type=attr.get("relation_type"),
        contribution_value=(
            Decimal(str(attr["contribution_value"]))
            if attr.get("contribution_value") is not None else None
        ),
        contribution_rank=attr.get("contribution_rank"),
        metrics_payload=attr.get("metrics_payload"),
        evidence_payload=attr.get("evidence_payload"),
        coverage_ratio=(
            Decimal(str(attr["coverage_ratio"]))
            if attr.get("coverage_ratio") is not None else None
        ),
        source_board_snapshot_id=attr.get("source_board_snapshot_id"),
        taxonomy_version=attr.get("taxonomy_version"),
        taxonomy_compatibility_key=attr.get("taxonomy_compatibility_key"),
        membership_version=attr.get("membership_version"),
        eligible_count=attr.get("eligible_count"),
        ready_count=attr.get("ready_count"),
        data_quality_json=attr.get("data_quality"),
    )
    session.add(record)
    await session.flush()
    return record


async def _delete_attributions(
    session: AsyncSession,
    signal_id: uuid.UUID,
) -> None:
    """删除信号的所有归因记录（重算前清理）。"""
    from sqlalchemy import delete
    stmt = delete(MarketReviewSignalAttribution).where(
        MarketReviewSignalAttribution.signal_id == signal_id,
    )
    await session.execute(stmt)


async def _list_child_scope_keys(
    session: AsyncSession,
    parent_scope_type: str,
    parent_scope_key: str,
    child_scope_type: str,
    *,
    trade_date: Any,
    source_board_run_id: uuid.UUID,
    parent_instrument_ids: list[uuid.UUID],
) -> list[ChildScopeCandidate]:
    """List all relevant PIT child scopes without first-page truncation.

    Industry parents use the explicit hierarchy. Market/index/style parents use member
    intersection, and concepts always require a non-empty parent-member intersection.
    Every returned candidate is tied to the Board snapshot from the source batch.
    """
    if child_scope_type not in {"industry_l2", "industry_l3", "concept"}:
        return []

    stmt = (
        select(MarketBoard, BoardAnalysisSnapshot)
        .join(
            BoardAnalysisSnapshot,
            BoardAnalysisSnapshot.board_id == MarketBoard.id,
        )
        .where(
            MarketBoard.isActive.is_(True),
            BoardAnalysisSnapshot.trade_date == trade_date,
            BoardAnalysisSnapshot.board_analysis_run_id == source_board_run_id,
        )
    )
    if child_scope_type == "concept":
        stmt = stmt.where(MarketBoard.type == "concept")
    else:
        level = "L2" if child_scope_type == "industry_l2" else "L3"
        stmt = stmt.where(
            MarketBoard.type == "industry",
            MarketBoard.hierarchyLevel == level,
        )
        if parent_scope_type in {"industry_l1", "industry_l2"}:
            try:
                parent_id = uuid.UUID(parent_scope_key)
            except ValueError:
                return []
            if child_scope_type == "industry_l2":
                if parent_scope_type != "industry_l1":
                    return []
                stmt = stmt.where(MarketBoard.parentBoardId == parent_id)
            elif parent_scope_type == "industry_l2":
                stmt = stmt.where(MarketBoard.parentBoardId == parent_id)
            else:
                l2_ids = select(MarketBoard.id).where(
                    MarketBoard.parentBoardId == parent_id,
                    MarketBoard.type == "industry",
                    MarketBoard.hierarchyLevel == "L2",
                    MarketBoard.isActive.is_(True),
                )
                stmt = stmt.where(MarketBoard.parentBoardId.in_(l2_ids))

    stmt = stmt.order_by(MarketBoard.name.asc(), MarketBoard.id.asc())
    rows = list((await session.execute(stmt)).all())
    parent_set = set(parent_instrument_ids)
    candidates: list[ChildScopeCandidate] = []
    for board, snapshot in rows:
        try:
            membership = await resolve_board_membership_at(
                session, board.id, trade_date,
            )
        except PITMembershipUnavailableError:
            continue
        member_ids = tuple(
            instrument_id
            for instrument_id in membership.instrument_ids
            if instrument_id in parent_set
        )
        if not member_ids:
            continue
        candidates.append(
            ChildScopeCandidate(
                scope_type=child_scope_type,
                scope_key=str(board.id),
                scope_name=board.name,
                relation_type=_relation_type(parent_scope_type, child_scope_type),
                member_ids=member_ids,
                source_board_snapshot_id=snapshot.id,
                taxonomy_version=membership.taxonomy_version,
                taxonomy_compatibility_key=membership.compatibility_key,
                membership_version=membership.membership_version,
            ),
        )
    return candidates


def _relation_type(parent_type: str, child_type: str) -> str:
    """父子范围关系类型。"""
    if parent_type == "industry_l1" and child_type in {"industry_l2", "industry_l3"}:
        return "descendant_industry"
    if parent_type == "industry_l2" and child_type == "industry_l3":
        return "child_industry"
    if parent_type in ("industry_l1", "industry_l2") and child_type == "concept":
        return "related_concept"
    return "child_scope"


# =============================================================================
# 个股归因
# =============================================================================


async def compute_signal_instruments(
    session: AsyncSession,
    signal: MarketReviewSignal,
    *,
    parent_metrics: dict[str, dict[str, Any]],
    parent_ready_count: int,
    source_core_run_id: uuid.UUID,
    top_n: int | None = None,
    day_fact_map: dict[uuid.UUID, dict[str, Any]] | None = None,
) -> list[MarketReviewSignalInstrument]:
    """为信号计算个股归因并持久化。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        signal: MarketReviewSignal ORM 对象
        parent_metrics: 父范围 P/Q/U/C/V payload
        parent_ready_count: 父范围有效成员数
        source_core_run_id: stock_core run_id
        top_n: 保留前 N 项（按综合贡献绝对值排序）
        day_fact_map: [REVIEW-FACT-PARITY-02 §10] 正式路径一次性加载的当日 facts

    Returns:
        持久化的 MarketReviewSignalInstrument 列表
    """
    # 获取父范围成员
    instrument_ids, _ = await resolve_scope_members(
        session, signal.scope_type, signal.scope_key, trade_date=signal.trade_date,
    )
    if not instrument_ids:
        return []

    flat_list = await _member_flat_list(
        session,
        instrument_ids,
        source_core_run_id,
        trade_date=signal.trade_date,
        day_fact_map=day_fact_map,
    )
    if not flat_list:
        return []

    # 构建 instruments 输入（含 instrument_id / symbol / name）
    instruments_input: list[dict[str, Any]] = []
    for flat in flat_list:
        inst_id_str = flat.get("_instrument_id")
        if not inst_id_str:
            continue
        try:
            inst_id = uuid.UUID(inst_id_str)
        except ValueError:
            continue
        instruments_input.append({
            "instrument_id": inst_id,
            "symbol": flat.get("_instrument_symbol") or inst_id_str,
            "name": flat.get("_instrument_name") or inst_id_str,
            "flat": flat,
            "source_snapshot_id": flat.get("_snapshot_id"),
        })

    if not instruments_input:
        return []

    # 计算归因
    instruments = aggregate_instrument_attributions(
        parent_metrics, parent_ready_count, instruments_input,
    )

    if top_n is not None:
        instruments = instruments[:top_n]

    # 持久化（先删除旧归因，再插入；幂等）
    await _delete_instruments(session, signal.id)
    created: list[MarketReviewSignalInstrument] = []
    for inst in instruments:
        record = await _insert_instrument(session, signal.id, inst)
        created.append(record)

    logger.info(
        "[ReviewInstrument] signal=%s/%s instruments=%d",
        signal.scope_type, signal.signal_type, len(created),
    )
    return created


async def _insert_instrument(
    session: AsyncSession,
    signal_id: uuid.UUID,
    inst: dict[str, Any],
) -> MarketReviewSignalInstrument:
    """插入个股归因记录。"""
    record = MarketReviewSignalInstrument(
        signal_id=signal_id,
        instrument_id=inst["instrument_id"],
        symbol=inst["symbol"],
        name=inst["name"],
        board_role=inst.get("board_role"),
        relation_to_scope=inst.get("relation_to_scope"),
        contribution_value=(
            Decimal(str(inst["contribution_value"]))
            if inst.get("contribution_value") is not None else None
        ),
        contribution_rank=inst.get("contribution_rank"),
        first_pyramid_payload=inst.get("first_pyramid_payload"),
        fresh_events_payload=inst.get("fresh_events_payload"),
        contribution_payload=inst.get("contribution_payload"),
        role_evidence=inst.get("role_evidence"),
        source_snapshot_id=inst.get("source_snapshot_id"),
    )
    session.add(record)
    await session.flush()
    return record


async def _delete_instruments(
    session: AsyncSession,
    signal_id: uuid.UUID,
) -> None:
    """删除信号的所有个股归因记录（重算前清理）。"""
    from sqlalchemy import delete
    stmt = delete(MarketReviewSignalInstrument).where(
        MarketReviewSignalInstrument.signal_id == signal_id,
    )
    await session.execute(stmt)


# =============================================================================
# 查询（API 用）
# =============================================================================


async def list_attributions(
    session: AsyncSession,
    signal_id: uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[MarketReviewSignalAttribution], int]:
    """分页查询信号归因（按 contribution_rank 升序）。"""
    from sqlalchemy import func

    base = select(MarketReviewSignalAttribution).where(
        MarketReviewSignalAttribution.signal_id == signal_id,
    )
    count_stmt = select(func.count()).select_from(base.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    offset = (page - 1) * page_size
    stmt = (
        base
        .order_by(MarketReviewSignalAttribution.contribution_rank.asc())
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    return list(result.scalars()), total


async def list_instruments(
    session: AsyncSession,
    signal_id: uuid.UUID,
    *,
    board_role: str | None = None,
    relation_to_scope: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[MarketReviewSignalInstrument], int]:
    """分页查询信号个股归因（按 contribution_rank 升序）。"""
    from sqlalchemy import func

    base = select(MarketReviewSignalInstrument).where(
        MarketReviewSignalInstrument.signal_id == signal_id,
    )
    if board_role is not None:
        base = base.where(
            MarketReviewSignalInstrument.board_role == board_role,
        )
    if relation_to_scope is not None:
        base = base.where(
            MarketReviewSignalInstrument.relation_to_scope == relation_to_scope,
        )
    count_stmt = select(func.count()).select_from(base.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    offset = (page - 1) * page_size
    stmt = (
        base
        .order_by(MarketReviewSignalInstrument.contribution_rank.asc())
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    return list(result.scalars()), total


if __name__ == "__main__":
    print(f"DEFAULT_TOP_N_ATTRIBUTIONS = {DEFAULT_TOP_N_ATTRIBUTIONS}")
    print(f"DEFAULT_TOP_N_INSTRUMENTS = {DEFAULT_TOP_N_INSTRUMENTS}")
    print("OK: review_attribution_service imports verified")
