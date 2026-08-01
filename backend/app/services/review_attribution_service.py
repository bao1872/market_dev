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
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.attribution_engine import (
    aggregate_child_scope_attributions,
    aggregate_instrument_attributions,
)
from app.models.market_review import (
    MarketReviewSignal,
    MarketReviewSignalAttribution,
    MarketReviewSignalInstrument,
)
from app.services.review_scope_service import (
    fetch_member_flat_list,
    resolve_scope_members,
)

logger = logging.getLogger("review_attribution_service")

# 子范围归因默认保留前 N 项（PRD §9.1：保存前 N 项，但 API 支持分页读取全部）
DEFAULT_TOP_N_ATTRIBUTIONS = 20
# 个股归因默认保留前 N 项
DEFAULT_TOP_N_INSTRUMENTS = 30


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
    child_scope_types: tuple[str, ...] = ("industry_l2", "concept"),
    top_n: int = DEFAULT_TOP_N_ATTRIBUTIONS,
) -> list[MarketReviewSignalAttribution]:
    """为信号计算子范围归因并持久化。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        signal: MarketReviewSignal ORM 对象
        parent_metrics: 父范围 P/Q/U/C/V payload
        parent_ready_count: 父范围有效成员数
        source_core_run_id: stock_core run_id（用于读取成员 flat）
        child_scope_types: 下钻子范围类型（默认 industry_l2 + concept）
        top_n: 保留前 N 项（按绝对贡献排序）

    Returns:
        持久化的 MarketReviewSignalAttribution 列表
    """
    # 收集子范围
    child_scopes_data: list[dict[str, Any]] = []
    for child_scope_type in child_scope_types:
        # 简化：这里通过 board_analysis 表获取子范围列表
        # 实际实现需要根据 parent_scope_type 查询直接子范围和关联概念
        child_keys = await _list_child_scope_keys(
            session, signal.scope_type, signal.scope_key, child_scope_type,
        )
        for child_key, child_name in child_keys:
            instrument_ids, _ = await resolve_scope_members(
                session, child_scope_type, child_key, trade_date=signal.trade_date,
            )
            if not instrument_ids:
                continue
            flat_list = await fetch_member_flat_list(
                session, instrument_ids, source_core_run_id,
            )
            if not flat_list:
                continue
            child_scopes_data.append({
                "scope_type": child_scope_type,
                "scope_key": child_key,
                "scope_name": child_name,
                "relation_type": _relation_type(signal.scope_type, child_scope_type),
                "flat_list": flat_list,
                "ready_count": sum(
                    1 for f in flat_list
                    if f and f.get("fp_trend_direction") is not None
                ),
            })

    if not child_scopes_data:
        return []

    # 计算归因
    attributions = aggregate_child_scope_attributions(
        parent_metrics, parent_ready_count, child_scopes_data,
    )

    # 保留前 N 项（绝对贡献排序）
    attributions = attributions[:top_n]

    # 持久化（先删除旧归因，再插入；幂等）
    await _delete_attributions(session, signal.id)
    created: list[MarketReviewSignalAttribution] = []
    for attr in attributions:
        record = await _insert_attribution(session, signal.id, attr)
        created.append(record)

    logger.info(
        "[ReviewAttribution] signal=%s/%s child_scopes=%d attributions=%d",
        signal.scope_type, signal.signal_type,
        len(child_scopes_data), len(created),
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
) -> list[tuple[str, str]]:
    """列出父范围下的子范围 (key, name) 列表。

    简化实现：通过 market_boards 表查询。
    实际生产需要根据 parent_scope_type 路由（industry_l1 → industry_l2/l3）。
    """
    # 简化：返回空列表由调用方决定如何获取子范围
    # 生产环境需要从 board_analysis_snapshots 或专用映射表读取
    return []


def _relation_type(parent_type: str, child_type: str) -> str:
    """父子范围关系类型。"""
    if parent_type == "industry_l1" and child_type == "industry_l2":
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
    top_n: int = DEFAULT_TOP_N_INSTRUMENTS,
) -> list[MarketReviewSignalInstrument]:
    """为信号计算个股归因并持久化。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        signal: MarketReviewSignal ORM 对象
        parent_metrics: 父范围 P/Q/U/C/V payload
        parent_ready_count: 父范围有效成员数
        source_core_run_id: stock_core run_id
        top_n: 保留前 N 项（按综合贡献绝对值排序）

    Returns:
        持久化的 MarketReviewSignalInstrument 列表
    """
    # 获取父范围成员
    instrument_ids, _ = await resolve_scope_members(
        session, signal.scope_type, signal.scope_key, trade_date=signal.trade_date,
    )
    if not instrument_ids:
        return []

    flat_list = await fetch_member_flat_list(
        session, instrument_ids, source_core_run_id,
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
            "symbol": flat.get("fp_symbol") or inst_id_str,
            "name": flat.get("fp_name") or inst_id_str,
            "flat": flat,
            "source_snapshot_id": source_core_run_id,
        })

    if not instruments_input:
        return []

    # 计算归因
    instruments = aggregate_instrument_attributions(
        parent_metrics, parent_ready_count, instruments_input,
    )

    # 保留前 N 项
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
