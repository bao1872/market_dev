"""复盘范围扫描服务 - 一级与二级范围 scope snapshot（PRD §6、§7）。

PRD §6 范围定义：
- 第一级扫描：market / major_index / style / industry_l1
- 第二级下钻（仅对命中信号的父范围扫描）：
  industry_l2 / industry_l3 / concept / instrument
- 下钻必须保留父子路径：market → style/index/industry_l1 → industry_l2/l3 or related concept → instrument

输入：
- 已发布 stock_core pointer（factor_publications.data_run_id）
- 已发布 board_analysis pointer（用于行业/概念范围）
- 第一金字塔历史基线（默认 120 个交易日，最低 60 日）

输出：
- MarketReviewScopeSnapshot ORM 记录（每个范围一条，含 P/Q/U/C/V payload）

幂等：
- 相同 (review_run_id, scope_type, scope_key) 不重算
- coverage_ratio = ready_count / eligible_count

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.review_scope_service
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.member_fact import DailyBarFact, ReviewMemberFact
from app.domain.review.metric_engine import _cross_section_percentile, compute_all_metrics
from app.domain.review.metric_registry import DEFAULT_REGISTRY
from app.models.bar import BarDaily
from app.models.first_pyramid_history import FirstPyramidHistoryDailyState
from app.models.instrument import Instrument
from app.models.market_review import MarketReviewScopeSnapshot
from app.services.board_membership_service import (
    PITMembershipUnavailableError,
    resolve_board_membership_at,
    resolve_universe_membership_at,
)

logger = logging.getLogger("review_scope_service")

# 默认基线窗口（PRD §0、§7.1）
DEFAULT_BASELINE_WINDOW = 120
MIN_BASELINE_WINDOW = 60

# 单 scope 发布门禁（PRD §11.1）
SCOPE_PUBLISH_MIN_COVERAGE = 0.95

# 第一级范围类型
LEVEL1_SCOPE_TYPES: tuple[str, ...] = (
    "market", "major_index", "style", "industry_l1",
)
# 第二级范围类型
LEVEL2_SCOPE_TYPES: tuple[str, ...] = (
    "industry_l2", "industry_l3", "concept", "instrument",
)


class ScopeSnapshotError(Exception):
    """范围快照计算失败。"""

    pass


# =============================================================================
# Scope 定义
# =============================================================================


class ScopeDefinition:
    """单个范围定义（scope_type + scope_key + scope_name + 成员来源）。

    成员来源由 service 层根据 scope_type 查询：
    - market: 全部 active A 股
    - major_index: 复用指数成分服务
    - style: 复用风格股票池定义
    - industry_l1/l2/l3: 复用行业板块成员关系
    - concept: 复用概念板块成员关系
    - instrument: 单只股票（下钻用）
    """

    def __init__(
        self,
        scope_type: str,
        scope_key: str,
        scope_name: str,
        *,
        parent_scope_type: str | None = None,
        parent_scope_key: str | None = None,
        source_board_snapshot_id: uuid.UUID | None = None,
        taxonomy_version: str | None = None,
        taxonomy_compatibility_key: str | None = None,
        membership_version: str | None = None,
    ) -> None:
        self.scope_type = scope_type
        self.scope_key = scope_key
        self.scope_name = scope_name
        self.parent_scope_type = parent_scope_type
        self.parent_scope_key = parent_scope_key
        self.source_board_snapshot_id = source_board_snapshot_id
        self.taxonomy_version = taxonomy_version
        self.taxonomy_compatibility_key = taxonomy_compatibility_key
        self.membership_version = membership_version

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_type": self.scope_type,
            "scope_key": self.scope_key,
            "scope_name": self.scope_name,
            "parent_scope_type": self.parent_scope_type,
            "parent_scope_key": self.parent_scope_key,
            "source_board_snapshot_id": (
                str(self.source_board_snapshot_id)
                if self.source_board_snapshot_id else None
            ),
            "taxonomy_version": self.taxonomy_version,
            "taxonomy_compatibility_key": self.taxonomy_compatibility_key,
            "membership_version": self.membership_version,
        }


# =============================================================================
# Scope snapshot upsert（幂等）
# =============================================================================


async def upsert_scope_snapshot(
    session: AsyncSession,
    review_run_id: uuid.UUID,
    trade_date: date,
    scope: ScopeDefinition,
    *,
    eligible_count: int,
    ready_count: int,
    coverage_ratio: float,
    status: str,
    p_payload: dict[str, Any] | None,
    q_payload: dict[str, Any] | None,
    u_payload: dict[str, Any] | None,
    c_payload: dict[str, Any] | None,
    v_payload: dict[str, Any] | None,
    data_quality_json: dict[str, Any] | None = None,
) -> MarketReviewScopeSnapshot:
    """upsert scope snapshot 记录（幂等，唯一键 review_run_id+scope_type+scope_key）。"""
    values = {
        "review_run_id": review_run_id,
        "trade_date": trade_date,
        "scope_type": scope.scope_type,
        "scope_key": scope.scope_key,
        "scope_name": scope.scope_name,
        "parent_scope_type": scope.parent_scope_type,
        "parent_scope_key": scope.parent_scope_key,
        "source_board_snapshot_id": scope.source_board_snapshot_id,
        "taxonomy_version": scope.taxonomy_version,
        "taxonomy_compatibility_key": scope.taxonomy_compatibility_key,
        "membership_version": scope.membership_version,
        "eligible_count": eligible_count,
        "ready_count": ready_count,
        "coverage_ratio": Decimal(str(coverage_ratio)),
        "status": status,
        "p_payload": p_payload,
        "q_payload": q_payload,
        "u_payload": u_payload,
        "c_payload": c_payload,
        "v_payload": v_payload,
        "data_quality_json": data_quality_json,
    }

    stmt = pg_insert(MarketReviewScopeSnapshot).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_review_scope_snapshots_run_scope",
        set_={
            "trade_date": stmt.excluded.trade_date,
            "scope_name": stmt.excluded.scope_name,
            "parent_scope_type": stmt.excluded.parent_scope_type,
            "parent_scope_key": stmt.excluded.parent_scope_key,
            "source_board_snapshot_id": stmt.excluded.source_board_snapshot_id,
            "eligible_count": stmt.excluded.eligible_count,
            "ready_count": stmt.excluded.ready_count,
            "coverage_ratio": stmt.excluded.coverage_ratio,
            "status": stmt.excluded.status,
            "p_payload": stmt.excluded.p_payload,
            "q_payload": stmt.excluded.q_payload,
            "u_payload": stmt.excluded.u_payload,
            "c_payload": stmt.excluded.c_payload,
            "v_payload": stmt.excluded.v_payload,
            "data_quality_json": stmt.excluded.data_quality_json,
        },
    )
    await session.execute(stmt)
    await session.flush()

    # 读取 upsert 后的记录
    return await get_scope_snapshot(
        session, review_run_id, scope.scope_type, scope.scope_key,
    )  # type: ignore[return-value]


async def get_scope_snapshot(
    session: AsyncSession,
    review_run_id: uuid.UUID,
    scope_type: str,
    scope_key: str,
) -> MarketReviewScopeSnapshot | None:
    """读取单个 scope snapshot。"""
    stmt = (
        select(MarketReviewScopeSnapshot)
        .where(
            MarketReviewScopeSnapshot.review_run_id == review_run_id,
            MarketReviewScopeSnapshot.scope_type == scope_type,
            MarketReviewScopeSnapshot.scope_key == scope_key,
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_scope_snapshots(
    session: AsyncSession,
    review_run_id: uuid.UUID,
    *,
    scope_type: str | None = None,
    parent_scope_type: str | None = None,
    parent_scope_key: str | None = None,
) -> list[MarketReviewScopeSnapshot]:
    """列出 review run 的 scope snapshot（可按 scope_type / parent 过滤）。"""
    stmt = select(MarketReviewScopeSnapshot).where(
        MarketReviewScopeSnapshot.review_run_id == review_run_id,
    )
    if scope_type is not None:
        stmt = stmt.where(MarketReviewScopeSnapshot.scope_type == scope_type)
    if parent_scope_type is not None:
        stmt = stmt.where(
            MarketReviewScopeSnapshot.parent_scope_type == parent_scope_type,
        )
    if parent_scope_key is not None:
        stmt = stmt.where(
            MarketReviewScopeSnapshot.parent_scope_key == parent_scope_key,
        )
    result = await session.execute(stmt)
    return list(result.scalars())


# =============================================================================
# Scope 指标计算
# =============================================================================


async def compute_scope_metrics(
    session: AsyncSession,
    review_run_id: uuid.UUID,
    trade_date: date,
    scope: ScopeDefinition,
    flat_list: list[dict[str, Any]],
    *,
    eligible_count: int | None = None,
    history_maps: dict[str, dict[str, list[float]]] | None = None,
    prev_values: dict[str, float] | None = None,
    prev5d_values: dict[str, float] | None = None,
    cross_section_values: dict[str, list[float]] | None = None,
    pyramid_v2_payload: dict[str, Any] | None = None,
) -> MarketReviewScopeSnapshot:
    """计算单个范围的 P/Q/U/C/V 并 upsert scope snapshot。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        review_run_id: 复盘 run ID
        trade_date: 业务交易日
        scope: 范围定义
        flat_list: 成员 first_pyramid_flat 列表
        eligible_count: 范围成员总数（None=取 len(flat_list)）
        history_maps: 每个 metric 的历史序列 map（用于历史分位归一化）
        prev_values: 前一交易日各 metric 的 value（用于 delta1d）
        prev5d_values: 前 5 交易日各 metric 的 value（用于 delta5d）
        cross_section_values: 当日所有 scope 的 value 序列（用于横截面分位）
        pyramid_v2_payload: 第二金字塔维度数据（PRD §24，来自
            board_analysis_snapshots.payload["pyramid_v2"]）；
            industry/concept scope 由 orchestrator 注入，其他 scope 为 None

    Returns:
        MarketReviewScopeSnapshot ORM 对象
    """
    if eligible_count is None:
        eligible_count = len(flat_list)

    # ready_count = 有 fp_trend_direction 的成员数
    ready_count = sum(
        1 for f in flat_list if f and f.get("fp_trend_direction") is not None
    )

    coverage_ratio = (
        ready_count / eligible_count if eligible_count > 0 else 0.0
    )

    # 计算 P/Q/U/C/V
    payloads = compute_all_metrics(
        flat_list,
        ready_count=ready_count,
        history_maps=history_maps,
        prev_values=prev_values,
        prev5d_values=prev5d_values,
        cross_section_values=cross_section_values,
        registry=DEFAULT_REGISTRY,
    )

    # 状态判定（PRD §7.1：历史少于 60 日 status=insufficient_history）
    statuses = [p.get("status") for p in payloads.values()]
    if not flat_list:
        status = "unavailable"
    elif all(s == "ready" for s in statuses):
        status = "ready"
    elif any(s == "insufficient_history" for s in statuses):
        status = "insufficient_history"
    else:
        status = "partial"

    data_quality = {
        "eligible_count": eligible_count,
        "ready_count": ready_count,
        "missing_count": eligible_count - ready_count,
        "missing_reasons": _classify_missing_reasons(flat_list, eligible_count),
    }
    # [P0-7 2026-07-30] 注入 pyramid_v2 维度数据（PRD §24 D 族筛选器）
    # industry/concept scope 由 orchestrator 从 board_analysis_snapshots 读取并注入；
    # 其他 scope pyramid_v2_payload=None，不写入 data_quality_json
    if pyramid_v2_payload is not None:
        data_quality["pyramid_v2"] = pyramid_v2_payload

    snapshot = await upsert_scope_snapshot(
        session,
        review_run_id,
        trade_date,
        scope,
        eligible_count=eligible_count,
        ready_count=ready_count,
        coverage_ratio=coverage_ratio,
        status=status,
        p_payload=payloads.get("P"),
        q_payload=payloads.get("Q"),
        u_payload=payloads.get("U"),
        c_payload=payloads.get("C"),
        v_payload=payloads.get("V"),
        data_quality_json=data_quality,
    )

    logger.info(
        "[ReviewScope] %s/%s eligible=%d ready=%d coverage=%.4f status=%s",
        scope.scope_type, scope.scope_name, eligible_count, ready_count,
        coverage_ratio, status,
    )
    return snapshot


def _scope_family(scope_type: str) -> str:
    if scope_type.startswith("industry_"):
        return "industry"
    return scope_type


async def apply_cross_section_percentiles(
    session: AsyncSession,
    review_run_id: uuid.UUID,
) -> int:
    """Second pass: rank normalized values among same-day scope-family peers."""
    stmt = (
        select(MarketReviewScopeSnapshot)
        .where(MarketReviewScopeSnapshot.review_run_id == review_run_id)
        .order_by(
            MarketReviewScopeSnapshot.scope_type.asc(),
            MarketReviewScopeSnapshot.scope_key.asc(),
        )
    )
    snapshots = list((await session.execute(stmt)).scalars())
    fields = {
        "P": "p_payload",
        "Q": "q_payload",
        "U": "u_payload",
        "C": "c_payload",
        "V": "v_payload",
    }
    peers: dict[tuple[str, str], list[float]] = defaultdict(list)
    for snapshot in snapshots:
        family = _scope_family(snapshot.scope_type)
        for code, field in fields.items():
            payload = getattr(snapshot, field)
            value = payload.get("value") if isinstance(payload, dict) else None
            if isinstance(value, (int, float)):
                peers[(family, code)].append(float(value))

    updated = 0
    for snapshot in snapshots:
        family = _scope_family(snapshot.scope_type)
        for code, field in fields.items():
            original = getattr(snapshot, field)
            if not isinstance(original, dict):
                continue
            payload = dict(original)
            value = payload.get("value")
            peer_values = peers.get((family, code), [])
            percentile = (
                _cross_section_percentile(float(value), peer_values)
                if isinstance(value, (int, float)) and peer_values
                else None
            )
            payload["crossSectionPercentile"] = percentile
            setattr(snapshot, field, payload)
        updated += 1
    await session.flush()
    return updated


def _classify_missing_reasons(
    flat_list: list[dict[str, Any]], eligible_count: int,
) -> dict[str, int]:
    """分类成员缺失原因。"""
    reasons: dict[str, int] = {}
    ready = sum(
        1 for f in flat_list if f and f.get("fp_trend_direction") is not None
    )
    missing = eligible_count - ready
    if missing > 0:
        # 简化：所有缺失归为 SNAPSHOT_MISSING 或 FP_TREND_MISSING
        no_trend = sum(
            1 for f in flat_list
            if f and f.get("fp_trend_direction") is None
        )
        no_snapshot = eligible_count - len(flat_list)
        if no_snapshot > 0:
            reasons["SNAPSHOT_MISSING"] = no_snapshot
        if no_trend > 0:
            reasons["FP_TREND_MISSING"] = no_trend
    return reasons


# =============================================================================
# 范围查询（service 层根据 scope_type 查询成员）
# =============================================================================


async def resolve_scope_members(
    session: AsyncSession,
    scope_type: str,
    scope_key: str,
    *,
    trade_date: date,
) -> tuple[list[uuid.UUID], str]:
    """Resolve members from the source valid on ``trade_date``.

    Board and configured universe scopes are point-in-time only. Historical
    requests never fall back to the latest-state membership projection.
    """
    if scope_type == "market":
        from app.models.instrument import Instrument

        stmt = select(Instrument.id).where(Instrument.status == "active")
        result = await session.execute(stmt)
        return [row[0] for row in result], "全市场"

    if scope_type == "instrument":
        try:
            inst_id = uuid.UUID(scope_key)
        except ValueError as exc:
            raise ScopeSnapshotError(
                f"instrument scope_key 非合法 UUID: {scope_key}",
            ) from exc
        return [inst_id], scope_key

    if scope_type in ("major_index", "style"):
        expected_type = "major_index" if scope_type == "major_index" else "style"
        try:
            definition, membership = await resolve_universe_membership_at(
                session, scope_key, trade_date,
            )
        except PITMembershipUnavailableError as exc:
            raise ScopeSnapshotError(str(exc)) from exc
        if definition.universe_type != expected_type:
            raise ScopeSnapshotError(
                f"scope_type mismatch: {scope_type} key={scope_key} "
                f"universe_type={definition.universe_type}",
            )
        if membership.population_status != "ready":
            raise ScopeSnapshotError(
                f"{membership.population_status}: {scope_type}={scope_key} "
                f"trade_date={trade_date}",
            )
        return list(membership.instrument_ids), definition.name

    if scope_type in ("industry_l1", "industry_l2", "industry_l3", "concept"):
        from app.models.market_board import MarketBoard

        try:
            board_uuid = uuid.UUID(scope_key)
        except ValueError as exc:
            raise ScopeSnapshotError(
                f"{scope_type} scope_key 非合法 UUID: {scope_key}",
            ) from exc
        board = (
            await session.execute(
                select(MarketBoard).where(MarketBoard.id == board_uuid).limit(1),
            )
        ).scalar_one_or_none()
        if board is None:
            raise ScopeSnapshotError(f"board_not_found: {scope_key}")
        expected_board_type = _board_type_from_scope(scope_type)
        if board.type != expected_board_type:
            raise ScopeSnapshotError(
                f"scope_type mismatch: {scope_type} board_type={board.type}",
            )
        expected_level = _hierarchy_level_from_scope(scope_type)
        if expected_level is not None and board.hierarchyLevel != expected_level:
            raise ScopeSnapshotError(
                f"scope hierarchy mismatch: {scope_type} "
                f"hierarchy={board.hierarchyLevel}",
            )
        try:
            membership = await resolve_board_membership_at(
                session, board_uuid, trade_date,
            )
        except PITMembershipUnavailableError as exc:
            raise ScopeSnapshotError(str(exc)) from exc
        if membership.population_status != "ready":
            raise ScopeSnapshotError(
                f"{membership.population_status}: board={scope_key} "
                f"trade_date={trade_date}",
            )
        return list(membership.instrument_ids), board.name

    return [], scope_key


def _board_type_from_scope(scope_type: str) -> str:
    """scope_type → market_boards.type 映射。"""
    if scope_type in ("industry_l1", "industry_l2", "industry_l3"):
        return "industry"
    if scope_type == "concept":
        return "concept"
    return scope_type


def _hierarchy_level_from_scope(scope_type: str) -> str | None:
    if scope_type == "industry_l1":
        return "L1"
    if scope_type == "industry_l2":
        return "L2"
    if scope_type == "industry_l3":
        return "L3"
    return None


# =============================================================================
# 批量获取成员 first_pyramid_flat
# =============================================================================


async def fetch_member_flat_list(
    session: AsyncSession,
    instrument_ids: list[uuid.UUID],
    source_core_run_id: uuid.UUID,
    *,
    trade_date: date | None = None,
) -> list[dict[str, Any]]:
    """Build typed Review member facts from one core run and PIT daily data.

    Returns:
        Metric input dictionaries. When ``trade_date`` is omitted this preserves the
        legacy identity-only behavior for non-Review callers and unit fixtures.
    """
    if not instrument_ids:
        return []
    from app.models.stock_feature_snapshot import StockFeatureSnapshot

    stmt = (
        select(
            StockFeatureSnapshot.id,
            StockFeatureSnapshot.instrument_id,
            Instrument.symbol,
            Instrument.name,
            StockFeatureSnapshot.summary_payload,
        )
        .join(Instrument, Instrument.id == StockFeatureSnapshot.instrument_id)
        .where(
            StockFeatureSnapshot.instrument_id.in_(instrument_ids),
            StockFeatureSnapshot.source_run_id == source_core_run_id,
        )
    )
    result = await session.execute(stmt)
    source_rows: list[tuple[uuid.UUID, uuid.UUID, str, str, dict[str, Any]]] = []
    for row in result:
        snapshot_id = row[0]
        instrument_id = row[1]
        symbol = row[2]
        name = row[3]
        summary = row[4] or {}
        if not isinstance(summary, dict):
            continue
        flat = summary.get("first_pyramid_flat")
        if isinstance(flat, dict):
            source_rows.append((snapshot_id, instrument_id, symbol, name, flat))

    if trade_date is None:
        return [
            {
                **flat,
                "_instrument_id": str(instrument_id),
                "_instrument_symbol": symbol,
                "_instrument_name": name,
                "_snapshot_id": snapshot_id,
            }
            for snapshot_id, instrument_id, symbol, name, flat in source_rows
        ]

    bar_stmt = (
        select(BarDaily)
        .where(
            BarDaily.instrument_id.in_(instrument_ids),
            BarDaily.trade_date <= trade_date,
            BarDaily.trade_date >= trade_date - timedelta(days=400),
        )
        .order_by(BarDaily.instrument_id.asc(), BarDaily.trade_date.asc())
    )
    bars_by_instrument: dict[uuid.UUID, list[DailyBarFact]] = defaultdict(list)
    for bar in (await session.execute(bar_stmt)).scalars():
        bars_by_instrument[bar.instrument_id].append(DailyBarFact.from_row(bar))

    previous_stmt = (
        select(FirstPyramidHistoryDailyState)
        .where(
            FirstPyramidHistoryDailyState.instrument_id.in_(instrument_ids),
            FirstPyramidHistoryDailyState.trade_date < trade_date,
            FirstPyramidHistoryDailyState.trade_date >= trade_date - timedelta(days=45),
        )
        .order_by(
            FirstPyramidHistoryDailyState.instrument_id.asc(),
            FirstPyramidHistoryDailyState.trade_date.desc(),
        )
    )
    previous_by_instrument: dict[uuid.UUID, dict[str, Any]] = {}
    for state in (await session.execute(previous_stmt)).scalars():
        previous_by_instrument.setdefault(state.instrument_id, state.state_payload)

    return [
        ReviewMemberFact.build(
            instrument_id=instrument_id,
            symbol=symbol,
            name=name,
            snapshot_id=snapshot_id,
            trade_date=trade_date,
            first_pyramid=flat,
            bars=bars_by_instrument[instrument_id],
            previous_state=previous_by_instrument.get(instrument_id),
        ).to_metric_input()
        for snapshot_id, instrument_id, symbol, name, flat in source_rows
    ]


if __name__ == "__main__":
    print(f"LEVEL1_SCOPE_TYPES = {LEVEL1_SCOPE_TYPES}")
    print(f"LEVEL2_SCOPE_TYPES = {LEVEL2_SCOPE_TYPES}")
    print(f"SCOPE_PUBLISH_MIN_COVERAGE = {SCOPE_PUBLISH_MIN_COVERAGE}")
    print("OK: review_scope_service imports verified")
