"""[CHANGE-20260730-011] 板块分析 V1 API 路由。

端点：
- GET /v1/boards/analysis: 板块分析列表（分页，按 type/trade_date/sort 过滤）
- GET /v1/boards/{board_id}/analysis: 单板块分析详情
- POST /v1/admin/boards/{board_id}/analysis/compute: 已退役（410 Gone）
- POST /v1/admin/boards/analysis/compute-all: 已退役（410 Gone）

权限：
- GET 接口：require_authenticated（任何登录用户可读）
- POST 接口：require_roles("admin")（保留签名兼容，但统一 410）

设计：
- [Slice 4A7] 读接口（两个 GET）数据源已从 BoardAnalysisSnapshot 切换到
  published Unified Review canonical facts（ReviewScopeObservationFact）。
  使用当前已发布的 MarketReviewRun 作为 lineage；绝不回退到 BoardAnalysisSnapshot。
- [Slice 4A9] 写接口（admin）已退役：legacy Board compute 不再可触发，
  两个 POST 统一返回 HTTP 410 Gone；板块分析完全由 Unified Review 提供。
- 失败返回 4xx/5xx，不静默返回空数据

Board Analysis Runtime Status（[Slice 4A9 / 4A10]）：
- Legacy ``BoardAnalysisRun`` / ``BoardAnalysisSnapshot`` 计算已退役；禁止再实现或调用
  ``compute_board_analysis`` / ``compute_all_boards`` / ``publish_board_analysis``。
- 当前板块分析 owner 是 Unified Review；Board GET 仅为 published
  ``ReviewScopeObservationFact`` 的投影。
- ``board_analysis_service`` / ``market_factor_aggregation_service`` 已删除；``MarketBoard`` /
  taxonomy / PIT membership 仍是 Unified Review 做板块分析的正式基础设施。
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.models.bar import BarDaily
from app.models.market_review import MarketReviewRun, ReviewScopeObservationFact
from app.schemas.board_analysis import (
    BoardAnalysisDetailResponse,
    BoardAnalysisListResponse,
    BoardAnalysisSnapshotDTO,
)
from app.services.access_control_service import AccessContext, require_authenticated
from app.services.review_publication_service import (
    get_published_review_run_id,
    list_published_review_dates,
)

logger = logging.getLogger("api.board_analysis")

# 用户侧路由：/v1/boards
board_router = APIRouter(prefix="/v1/boards", tags=["board-analysis"])

# 管理员路由：/v1/admin/boards
admin_router = APIRouter(prefix="/v1/admin/boards", tags=["admin-board-analysis"])


_BOARD_SCOPE_TYPES = ("industry_l1", "industry_l2", "industry_l3")


async def compute_is_stale(
    session: AsyncSession,
    snapshot_trade_date: date,
) -> bool:
    """判断快照是否过期（snapshot.trade_date < MAX(bars_daily.trade_date)）。

    从 board_analysis_service 退役时迁入：板块读接口仍需此新鲜度守卫，
    但不再依赖 legacy Board producer。
    """
    max_date = await session.scalar(select(func.max(BarDaily.trade_date)))
    if max_date is None:
        return False
    return snapshot_trade_date < max_date


def _scope_types_for_board_type(board_type: str | None) -> tuple[str, ...]:
    """board_type（industry/concept）→ canonical Review scope_type 集合。"""
    if board_type == "concept":
        return ("concept",)
    if board_type == "industry":
        return _BOARD_SCOPE_TYPES
    return ("concept",) + _BOARD_SCOPE_TYPES


def _board_type_from_scope_type(scope_type: str) -> str:
    """canonical scope_type → board_type：concept->concept，其余 industry。"""
    return "concept" if scope_type == "concept" else "industry"


def _count(dist: dict[str, Any] | None, key: str) -> int:
    """从 canonical 分布 dict 取下整；缺失/非 dict 一律 0，不伪造。"""
    if not isinstance(dist, dict):
        return 0
    v = dist.get(key)
    return v if isinstance(v, int) else 0


def _mean_of(dist: dict[str, Any] | None) -> float | None:
    """取 canonical 分布 mean；缺失返回 None。"""
    if not isinstance(dist, dict):
        return None
    v = dist.get("mean")
    return v if isinstance(v, (int, float)) else None


def _state_counts(dist: dict[str, Any], *keys: str) -> dict[str, int]:
    """取 categorical 分布中若干 ``<label>_count`` 键为一个前端 key/value dict。"""
    return {label: _count(dist, f"{label}_count") for label in keys}


def _build_board_payload(observation: dict[str, Any]) -> dict[str, Any]:
    """把 canonical Review Scope observation 映射成 BoardAnalysisPage 所需 payload。

    仅迁移已存在的 canonical 字段；不新增公式。缺失的分布留 0/None。
    """
    if not isinstance(observation, dict):
        observation = {}
    trend = observation.get("trend", {}) if isinstance(observation.get("trend"), dict) else {}
    structure = observation.get("structure", {}) if isinstance(observation.get("structure"), dict) else {}
    momentum = observation.get("momentum", {}) if isinstance(observation.get("momentum"), dict) else {}
    participation = observation.get("participation", {}) if isinstance(observation.get("participation"), dict) else {}
    volume = participation.get("volume", {}) if isinstance(participation.get("volume"), dict) else {}

    trend_state = trend.get("state", {}) if isinstance(trend.get("state"), dict) else {}
    trend_strength_dist = trend.get("trend_strength_distribution", {}) if isinstance(trend.get("trend_strength_distribution"), dict) else {}
    vwap_dev_dist = trend.get("dsa_vwap_dev_pct_distribution", {}) if isinstance(trend.get("dsa_vwap_dev_pct_distribution"), dict) else {}

    swing_dist = structure.get("swing", {}).get("state", {}) if isinstance(structure.get("swing"), dict) else {}
    alignment_dist = structure.get("alignment", {}) if isinstance(structure.get("alignment"), dict) else {}
    current_state = structure.get("current_state", {}) if isinstance(structure.get("current_state"), dict) else {}
    latest_events = current_state.get("latest_events", {}) if isinstance(current_state.get("latest_events"), dict) else {}
    bos = latest_events.get("bos", {}) if isinstance(latest_events.get("bos"), dict) else {}
    choch = latest_events.get("choch", {}) if isinstance(latest_events.get("choch"), dict) else {}
    ob = latest_events.get("ob", {}) if isinstance(latest_events.get("ob"), dict) else {}

    momentum_state = momentum.get("state", {}) if isinstance(momentum.get("state"), dict) else {}
    squeeze_state = momentum.get("squeeze_state", {}) if isinstance(momentum.get("squeeze_state"), dict) else {}
    change = momentum.get("change", {}) if isinstance(momentum.get("change"), dict) else {}
    sqzmom = momentum.get("sqzmom", {}) if isinstance(momentum.get("sqzmom"), dict) else {}

    volume_badge = volume.get("badge", {}) if isinstance(volume.get("badge"), dict) else {}

    # Slice 4A8 — 恢复 Board 事件率（旧 Board 公式复现，非新公式）。
    # ready 分母来自已迁移的 canonical trend.board_ready_member_count，
    # bos/choch up/down 来自最新事件快照（已 parity）。
    board_ready = trend.get("board_ready_member_count", 0) or 0
    bos_up_n = _count(bos, "up")
    bos_down_n = _count(bos, "down")
    choch_up_n = _count(choch, "up")
    choch_down_n = _count(choch, "down")
    if board_ready > 0:
        bos_rate = round((bos_up_n + bos_down_n) / board_ready, 4)
        choch_rate = round((choch_up_n + choch_down_n) / board_ready, 4)
    else:
        bos_rate = 0.0
        choch_rate = 0.0

    # 动量 distribution：Expanding/Flat/Contracting → 正/中性/负 位置映射
    momentum_dir = {
        "positive": _count(momentum_state, "expanding_count"),
        "negative": _count(momentum_state, "contracting_count"),
        "neutral": _count(momentum_state, "flat_count"),
    }
    squeeze = {
        "squeeze": _count(squeeze_state, "squeeze_count"),
        "released": _count(squeeze_state, "squeeze_release_count"),
        "normal": _count(squeeze_state, "non_squeeze_count"),
    }
    change_row = {
        "enhancing": _count(change, "enhancing_count"),
        "fading": _count(change, "weakening_count"),
        "flat": _count(change, "flat_count"),
    }

    return {
        "trend_dist": _state_counts(trend_state, "up", "down", "neutral"),
        "trend_strength": {
            "avg": _mean_of(trend_strength_dist),
            "p25": trend_strength_dist.get("p25"),
            "p50": trend_strength_dist.get("p50"),
            "p75": trend_strength_dist.get("p75"),
        },
        "vwap_dev_pct": {
            "avg": _mean_of(vwap_dev_dist),
            "p25": vwap_dev_dist.get("p25"),
            "p50": vwap_dev_dist.get("p50"),
            "p75": vwap_dev_dist.get("p75"),
        },
        "structure": {
            **_state_counts(swing_dist, "up", "down", "neutral"),
            "swing_up": _count(swing_dist, "up_count"),
            "swing_down": _count(swing_dist, "down_count"),
            "swing_neutral": _count(swing_dist, "neutral_count"),
            "alignment_aligned": _count(alignment_dist, "aligned_count"),
            "alignment_misaligned": _count(alignment_dist, "divergent_count"),
            "avg_active_ob_count": current_state.get("mean_active_orderblock_count"),
        },
        "structure_events": {
            "bos_up": _count(bos, "up"),
            "bos_down": _count(bos, "down"),
            "choch_up": _count(choch, "up"),
            "choch_down": _count(choch, "down"),
            "ob_up": _count(ob, "up"),
            "ob_down": _count(ob, "down"),
            "eqh_present": latest_events.get("eqh", 0),
            "eql_present": latest_events.get("eql", 0),
            # Slice 4A8 — 旧 Board 公式：rate = (up+down)/board_ready，round 4。
            "bos_rate": bos_rate,
            "choch_rate": choch_rate,
        },
        "momentum": {
            **momentum_dir,
            **squeeze,
            **change_row,
            "avg_sqzmom": _mean_of(sqzmom),
        },
        "volume": {
            "high": _count(volume_badge, "high_count"),
            "low": _count(volume_badge, "low_count"),
            "normal": _count(volume_badge, "normal_count"),
            "unknown": _count(volume_badge, "unknown_count"),
            "avg_volume_ratio20": volume.get("ratio20_mean"),
            "avg_volume_ratio200": volume.get("ratio200_mean"),
        },
    }


def _fact_to_dto(
    fact: ReviewScopeObservationFact,
    run: MarketReviewRun,
    is_stale: bool,
) -> BoardAnalysisSnapshotDTO:
    """canonical Review fact → Board 读 DTO（truthful，legacy 字段为 None）。"""
    eligible = fact.pit_member_count
    ready = fact.provided_member_count or 0
    coverage = ready / eligible if eligible > 0 else 0
    return BoardAnalysisSnapshotDTO(
        id=str(fact.id),
        trade_date=fact.trade_date.isoformat(),
        board_id=fact.scope_key,
        board_type=_board_type_from_scope_type(fact.scope_type),
        board_name=fact.scope_name or fact.scope_key,
        source_core_run_id=str(run.source_core_run_id),
        board_analysis_run_id=None,
        taxonomy_version=None,
        taxonomy_compatibility_key=None,
        membership_version=None,
        algorithm_version=run.algorithm_version,
        parameter_hash=None,
        eligible_count=eligible,
        ready_count=ready,
        coverage_ratio=coverage,
        missing_count=eligible - ready,
        missing_reasons={},
        status=fact.readiness,
        payload=_build_board_payload(fact.observation_payload),
        error_message=None,
        started_at=None,
        finished_at=None,
        created_at=fact.created_at.isoformat() if fact.created_at else "",
        updated_at=fact.updated_at.isoformat() if fact.updated_at else "",
        is_stale=is_stale,
        is_published=True,
    )


async def _resolve_published_review(
    db: AsyncSession,
    trade_date: date | None,
) -> tuple[MarketReviewRun, date] | None:
    """定位当前已发布的 MarketReviewRun + 有效 trade_date。

    trade_date 未指定时取最近一个已发布复盘日。无发布 run 返回 None。
    """
    if trade_date is None:
        dates = await list_published_review_dates(db, limit=1)
        if not dates:
            return None
        trade_date = dates[0]
    run_id = await get_published_review_run_id(db, trade_date)
    if run_id is None:
        return None
    stmt = select(MarketReviewRun).where(MarketReviewRun.id == run_id)
    run = (await db.execute(stmt)).scalars().first()
    if run is None:
        return None
    return run, trade_date


def _board_fact_statement(
    run: MarketReviewRun,
    eff_trade_date: date,
    board_type: str | None,
):
    """构建版图 fact 查询（run lineage + trade_date + 板块 scope_type 过滤）。"""
    return (
        select(ReviewScopeObservationFact)
        .where(
            ReviewScopeObservationFact.review_run_id == run.id,
            ReviewScopeObservationFact.trade_date == eff_trade_date,
            ReviewScopeObservationFact.scope_type.in_(
                _scope_types_for_board_type(board_type)
            ),
        )
    )


async def _list_board_facts(
    db: AsyncSession,
    run: MarketReviewRun,
    eff_trade_date: date,
    board_type: str | None,
) -> list[ReviewScopeObservationFact]:
    stmt = _board_fact_statement(run, eff_trade_date, board_type)
    return list((await db.execute(stmt)).scalars())


async def _get_board_fact(
    db: AsyncSession,
    run: MarketReviewRun,
    eff_trade_date: date,
    board_id: str,
) -> ReviewScopeObservationFact | None:
    stmt = _board_fact_statement(run, eff_trade_date, None).where(
        ReviewScopeObservationFact.scope_key == board_id,
    )
    return (await db.execute(stmt)).scalars().first()


@board_router.get("/analysis", response_model=BoardAnalysisListResponse)
async def list_board_analysis(
    type: str | None = Query(
        None, description="板块类型过滤：industry | concept",
    ),
    trade_date: date | None = Query(
        None, description="业务交易日（不传取最新已发布复盘日）",
    ),
    sort: str = Query(
        "coverage_desc",
        description="排序：coverage_desc | coverage_asc | name_asc | ready_desc",
    ),
    page: int = Query(1, ge=1, description="页码（1-based）"),
    page_size: int = Query(20, ge=1, le=100, description="每页大小"),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_authenticated),
) -> BoardAnalysisListResponse:
    """板块分析列表（只读，需登录）。

    数据源为 published Unified Review canonical facts（ReviewScopeObservationFact）。
    每个 item 的 is_published 恒为 True（来源即已发布的 Review run）；
    is_stale 由最新行情交易日判断。
    """
    _ = ctx
    if type is not None and type not in ("industry", "concept"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid type: {type}; must be one of: industry, concept",
        )
    if sort not in ("coverage_desc", "coverage_asc", "name_asc", "ready_desc"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid sort: {sort}",
        )

    empty = BoardAnalysisListResponse(
        items=[], total=0, page=page, page_size=page_size, has_more=False,
    )
    resolved = await _resolve_published_review(db, trade_date)
    if resolved is None:
        return empty
    run, eff_trade_date = resolved

    facts = await _list_board_facts(db, run, eff_trade_date, type)
    if not facts:
        return empty

    is_stale = await compute_is_stale(db, eff_trade_date)
    rows = [_fact_to_dto(fact, run, is_stale) for fact in facts]

    if sort == "coverage_asc":
        rows.sort(key=lambda d: d.coverage_ratio)
    elif sort == "coverage_desc":
        rows.sort(key=lambda d: d.coverage_ratio, reverse=True)
    elif sort == "ready_desc":
        rows.sort(key=lambda d: d.ready_count, reverse=True)
    else:  # name_asc
        rows.sort(key=lambda d: d.board_name)

    total = len(rows)
    start = (page - 1) * page_size
    items = rows[start : start + page_size]
    return BoardAnalysisListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=start + len(items) < total,
    )


@board_router.get(
    "/{board_id}/analysis",
    response_model=BoardAnalysisDetailResponse,
)
async def get_board_analysis(
    board_id: uuid.UUID,
    trade_date: date | None = Query(
        None, description="业务交易日（不传取最新已发布复盘日）",
    ),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_authenticated),
) -> BoardAnalysisDetailResponse:
    """单板块分析详情（只读，需登录）。

    同一 canonical Review 数据源，按 scope_key == board_id 在已发布 run 内查找。
    无 canonical 板块 fact 时返回 404；不含 BoardAnalysisSnapshot fallback。
    """
    _ = ctx
    resolved = await _resolve_published_review(db, trade_date)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"板块分析不存在: board_id={board_id}",
        )
    run, eff_trade_date = resolved

    fact = await _get_board_fact(db, run, eff_trade_date, str(board_id))
    if fact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"板块分析不存在: board_id={board_id}",
        )
    is_stale = await compute_is_stale(db, eff_trade_date)
    dto = _fact_to_dto(fact, run, is_stale)
    return BoardAnalysisDetailResponse(snapshot=dto)


# =============================================================================
# 管理员接口
# =============================================================================


@admin_router.post(
    "/{board_id}/analysis/compute",
    status_code=status.HTTP_410_GONE,
)
async def retire_legacy_board_compute(
    board_id: uuid.UUID,
    trade_date: date | None = Query(
        None, description="业务交易日（不传取最新已发布 stock_core 日期）",
    ),
    publish: bool = Query(True, description="计算后是否发布"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> None:
    """[Admin] Legacy 板块计算已退役。

    [Slice 4A9] 板块分析已由 Unified Review 提供，不再触发 legacy Board compute。
    统一返回 HTTP 410 Gone：不做任何 DB 查询 / 计算 / market_aggregation 发布 /
    legacy 快照写入。
    """
    _ = current_user
    _ = db
    _ = board_id
    _ = trade_date
    _ = publish
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Legacy Board compute is retired; "
            "board analysis is served by Unified Review."
        ),
    )


@admin_router.post(
    "/analysis/compute-all",
    status_code=status.HTTP_410_GONE,
)
async def retire_legacy_board_compute_all(
    trade_date: date | None = Query(
        None, description="业务交易日（不传取最新已发布 stock_core 日期）",
    ),
    board_type: str | None = Query(
        None, description="限定类型：industry | concept（不传两个都计算）",
    ),
    limit: int | None = Query(
        None, ge=1, le=1000, description="限定每个类型的板块数（canary 用）",
    ),
    publish: bool = Query(True, description="计算后是否发布 coverage>=0.95 的结果"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> None:
    """[Admin] Legacy 批量板块计算已退役。

    [Slice 4A9] 板块分析已由 Unified Review 提供，不再触发 legacy Board compute。
    统一返回 HTTP 410 Gone：不做任何 DB 查询 / 计算 / market_aggregation 发布 /
    legacy 快照写入。
    """
    _ = current_user
    _ = db
    _ = trade_date
    _ = board_type
    _ = limit
    _ = publish
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Legacy Board compute is retired; "
            "board analysis is served by Unified Review."
        ),
    )
