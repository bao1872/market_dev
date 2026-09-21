"""[F2] Market Dashboard 只读 API 路由（用户侧）。

端点：
- GET /v1/market-dashboard/market   大盘页（cards + 250 日 long/short chart）
- GET /v1/market-dashboard/rankings 行业/概念 ranking（industry L1/L2/L3，concept 不分层次）
- GET /v1/market-dashboard/scopes   [R2] 行业/概念板块全集 explorer（projection-only，全局 T/T-5）
- GET /v1/market-dashboard/scopes/{board_id}  单板块详情
- GET /v1/market-dashboard/compare 重点板块半月 EW index 比较

权限：复用现有 ``market_data`` capability（与 /v1/boards 等行情产品一致），不新增权限。

不读 bars_daily / instruments / MarketBoardMembership / F1A compute service；
只读 market_dashboard_market_daily / market_dashboard_scope_daily / market_boards。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.models.user_capability import CAPABILITY_MARKET_DATA
from app.schemas.market_dashboard import (
    CompareResponse,
    MarketDashboardResponse,
    RankingsResponse,
    ScopeDetailResponse,
    ScopeExplorerResponse,
)
from app.services.access_control_service import AccessContext, require_capability
from app.services.market_dashboard_read_service import (
    compare_boards,
    get_market_dashboard,
    get_rankings,
    get_scope_detail,
)
from app.services.market_dashboard_scope_explorer_service import (
    get_scope_explorer,
    validate_explorer_params,
)

router = APIRouter(prefix="/v1/market-dashboard", tags=["market-dashboard"])


_MARKET_SCOPE_TYPES = ("industry", "concept")
_HIERARCHY_LEVELS = ("L1", "L2", "L3")
_RANKING_LOOKBACK = 5  # V1 固定 5 个真实交易日


def _ctx(_: AccessContext = Depends(require_capability(CAPABILITY_MARKET_DATA))) -> None:
    return None


@router.get("/market", response_model=MarketDashboardResponse)
async def market_dashboard(
    days: int = Query(250, ge=1, le=1000, description="回看交易日数（默认 250）"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_ctx),
) -> MarketDashboardResponse:
    """大盘页：最新 4 cards（MA5/MA20/MA50/全市场 EW index）+ 250 日 chart。

    未构建投影返回 404（unavailable，不伪造 0）。
    """
    resp = await get_market_dashboard(db, days)
    if resp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="market dashboard projection 尚未构建",
        )
    return resp


@router.get("/rankings", response_model=RankingsResponse)
async def market_dashboard_rankings(
    scope_type: str = Query("industry", description="板块类型：industry | concept"),
    hierarchy_level: str | None = Query(None, description="行业层级 L1/L2/L3（仅 industry 生效）"),
    lookback: int = Query(5, description="V1 固定 5 个真实交易日（仅允许 5）"),
    limit: int = Query(10, ge=1, le=50, description="Top/Bottom 各返回条数"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_ctx),
) -> RankingsResponse:
    """行业/概念 ranking：Top/Bottom（默认按 ma5_delta 排序）。

    V1 lookback 仅允许 5；concept 不分层（传入 hierarchy_level 无效）。
    """
    if scope_type not in _MARKET_SCOPE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid scope_type: {scope_type}; must be one of: industry, concept",
        )
    if hierarchy_level is not None and hierarchy_level not in _HIERARCHY_LEVELS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid hierarchy_level: {hierarchy_level}; must be one of: L1, L2, L3",
        )
    if lookback != _RANKING_LOOKBACK:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"V1 lookback 固定为 {_RANKING_LOOKBACK}，不允许 {lookback}",
        )
    return await get_rankings(db, scope_type, hierarchy_level, lookback, limit)


@router.get("/scopes", response_model=ScopeExplorerResponse)
async def market_dashboard_scope_explorer(
    scope_type: str = Query("industry", description="板块类型：industry | concept"),
    hierarchy_level: str | None = Query(
        None, description="行业层级 L1/L2/L3（仅 industry；concept 传入即 422）"
    ),
    q: str | None = Query(None, description="板块名 substring 搜索"),
    page: int = Query(1, ge=1, description="页码（从 1 开始）"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数（1-100）"),
    sort: str = Query(
        "ma5",
        description=(
            "排序字段：name | member_count | ma5 | ma10 | ma20 | ma50 | ma120 | "
            "ma5_delta | ma10_delta"
        ),
    ),
    direction: str = Query("desc", description="asc | desc"),
    ma5_min: float | None = Query(None, description="MA5 breadth ratio 下界"),
    ma5_max: float | None = Query(None, description="MA5 breadth ratio 上界"),
    ma10_min: float | None = Query(None, description="MA10 breadth ratio 下界"),
    ma10_max: float | None = Query(None, description="MA10 breadth ratio 上界"),
    ma5_delta_min: float | None = Query(None, description="MA5 delta 下界（ratio difference）"),
    ma5_delta_max: float | None = Query(None, description="MA5 delta 上界（ratio difference）"),
    ma10_delta_min: float | None = Query(None, description="MA10 delta 下界（ratio difference）"),
    ma10_delta_max: float | None = Query(None, description="MA10 delta 上界（ratio difference）"),
    member_count_min: int | None = Query(None, ge=0, description="成分数下界"),
    member_count_max: int | None = Query(None, ge=0, description="成分数上界"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_ctx),
) -> ScopeExplorerResponse:
    """行业/概念板块**全集** explorer（R2，projection-only）。

    全局 T / T-5 语义：T 与 PREV 取自全市场 projection 日历（最近 6 个投影交易日），
    所有 board 共用同一 T / PREV；board 缺 PREV row → previous_*/delta = None（不 fallback）。
    search / filter / sort / pagination 全部 SQL server-side；NULLS LAST + board_id 稳定排序。
    """
    ranges: dict[str, float | None] = {
        "ma5_min": ma5_min,
        "ma5_max": ma5_max,
        "ma10_min": ma10_min,
        "ma10_max": ma10_max,
        "ma5_delta_min": ma5_delta_min,
        "ma5_delta_max": ma5_delta_max,
        "ma10_delta_min": ma10_delta_min,
        "ma10_delta_max": ma10_delta_max,
        "member_count_min": None if member_count_min is None else float(member_count_min),
        "member_count_max": None if member_count_max is None else float(member_count_max),
    }
    error = validate_explorer_params(
        scope_type=scope_type,
        hierarchy_level=hierarchy_level,
        sort=sort,
        direction=direction,
        ranges=ranges,
    )
    if error is not None:
        # concept + hierarchy_level 等非法组合显式 422（不静默忽略，区别于 rankings）。
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error,
        )
    return await get_scope_explorer(
        db,
        scope_type=scope_type,
        hierarchy_level=hierarchy_level,
        q=q,
        page=page,
        page_size=page_size,
        sort=sort,
        direction=direction,
        ranges=ranges,
    )


@router.get("/scopes/{board_id}", response_model=ScopeDetailResponse)
async def market_dashboard_scope(
    board_id: UUID,
    days: int = Query(250, ge=1, le=1000, description="回看交易日数（默认 250）"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_ctx),
) -> ScopeDetailResponse:
    """单板块详情：250 日 MA + scope EW index + metadata。

    inactive / 不存在 / 未构建投影均返回 404（不伪造 0）。
    """
    resp = await get_scope_detail(db, board_id, days)
    if resp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"scope dashboard 不存在: board_id={board_id}",
        )
    return resp


@router.get("/compare", response_model=CompareResponse)
async def market_dashboard_compare(
    board_ids: str = Query(..., description="逗号分隔的 board_id 列表（1-20 个，行业/概念可混合）"),
    days: int = Query(10, ge=1, le=60, description="回看交易日数（默认 10，最大 60）"),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_ctx),
) -> CompareResponse:
    """重点板块半月比较：每个 scope 的 EW return 链独立归一到 first=100。

    任一 board 不存在 / 未激活返回 404；不重建历史 membership。
    """
    try:
        ids = [UUID(x.strip()) for x in board_ids.split(",") if x.strip()]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid board_ids: {board_ids}",
        ) from exc
    if not ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="board_ids 不能为空",
        )
    if len(ids) > 20:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="board_ids 数量不得超过 20",
        )

    resp = await compare_boards(db, ids, days)
    if resp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="compare 中存在不存在/未激活的 board",
        )
    return resp
