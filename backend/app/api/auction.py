"""竞价分析 API 路由 — 市场/板块/个股三级页面数据接口。

端点：
- GET  /auction: 市场级页面数据（market scope + top industry/concept + top events）
- GET  /auction/board/{board_id}: 板块级页面数据（scope + top instruments + events）
- GET  /auction/stock/{symbol}: 个股级页面数据（anchors + result + events）
- GET  /auction/anchors/{trade_date}: 锚点快照与发布状态
- POST /admin/auction/scan: 触发竞价扫描 + 聚合（admin）
- POST /admin/auction/anchors: 触发锚点生成 + 发布（admin）

权限（PRD §3.2）：
- GET 接口：require_authenticated（任何登录用户可读）
- POST 接口：require_admin

设计要点：
- 用户侧路由只读 DB，不触发计算
- 默认查询当日（上海业务日）数据，trade_date 可显式指定
- 接口返回 trade_date、algorithm_version、publication_id（如果有）、
  source run IDs、coverage 和 reason_codes
- admin 接口调用 service 层 run_auction_scan / compute_auction_aggregation /
  generate_auction_anchors / publish_auction_anchors，并由 API 层控制 commit
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.time import shanghai_business_date
from app.models.auction import (
    AuctionAnchorItem,
    AuctionAnchorPublication,
    AuctionAnchorSnapshot,
    AuctionEventTracking,
    AuctionInstrumentResult,
    AuctionScanRun,
    AuctionScopeResult,
)
from app.models.instrument import Instrument
from app.models.market_board import MarketBoardMembership
from app.schemas.auction import (
    AnchorItemOut,
    AnchorPublicationOut,
    AnchorSnapshotOut,
    AuctionBoardPageData,
    AuctionInstrumentPageData,
    AuctionMarketPageData,
    EventTrackingOut,
    InstrumentResultOut,
    ScopeResultOut,
)
from app.services.access_control_service import (
    AccessContext,
    require_admin,
    require_authenticated,
)
from app.services.auction_aggregation_service import compute_auction_aggregation
from app.services.auction_anchor_service import (
    AUCTION_ANCHOR_ALGORITHM_VERSION,
    AnchorCoverageLowError,
    AnchorSnapshotNotFoundError,
    AnchorSnapshotNotReadyError,
    AnchorVersionMismatchError,
    generate_auction_anchors,
    get_published_anchors,
    publish_auction_anchors,
)
from app.services.auction_scan_service import (
    AUCTION_SCAN_ALGORITHM_VERSION,
    AnchorExpiredError,
    AnchorNotPublishedError,
    run_auction_scan,
)

logger = logging.getLogger("api.auction")

# 用户侧路由
router = APIRouter(prefix="/auction", tags=["auction"])

# 管理员路由
admin_router = APIRouter(prefix="/admin/auction", tags=["admin-auction"])

# 默认 Top N
DEFAULT_TOP_BOARDS = 10
DEFAULT_TOP_EVENTS = 20
DEFAULT_TOP_INSTRUMENTS = 20

# scan run 对用户可见的状态（succeeded/partial）
_VISIBLE_SCAN_STATUSES = ("succeeded", "partial")


# =============================================================================
# 辅助函数
# =============================================================================


def _resolve_trade_date(trade_date: date | None) -> date:
    """解析 trade_date，None 时取上海当前业务日。"""
    return trade_date if trade_date is not None else shanghai_business_date()


async def _get_latest_scan_run(
    db: AsyncSession,
    trade_date: date,
    *,
    auction_type: str = "final",
) -> AuctionScanRun | None:
    """查询当日最新可见的 scan_run（succeeded/partial）。"""
    stmt = (
        select(AuctionScanRun)
        .where(
            AuctionScanRun.trade_date == trade_date,
            AuctionScanRun.auction_type == auction_type,
            AuctionScanRun.algorithm_version == AUCTION_SCAN_ALGORITHM_VERSION,
            AuctionScanRun.status.in_(_VISIBLE_SCAN_STATUSES),
        )
        .order_by(AuctionScanRun.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _get_market_scope(
    db: AsyncSession,
    scan_run_id: uuid.UUID,
) -> AuctionScopeResult | None:
    """查询 market scope result（scope_id=NULL）。"""
    stmt = select(AuctionScopeResult).where(
        AuctionScopeResult.scan_run_id == scan_run_id,
        AuctionScopeResult.scope_type == "market",
        AuctionScopeResult.scope_id.is_(None),
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _get_board_scope(
    db: AsyncSession,
    scan_run_id: uuid.UUID,
    board_id: uuid.UUID,
) -> AuctionScopeResult | None:
    """按 board_id 查询 scope result（不区分 industry/concept）。"""
    stmt = select(AuctionScopeResult).where(
        AuctionScopeResult.scan_run_id == scan_run_id,
        AuctionScopeResult.scope_id == board_id,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def _scope_to_dto(scope: AuctionScopeResult) -> ScopeResultOut:
    return ScopeResultOut.model_validate(scope, from_attributes=True)


def _instrument_to_dto(res: AuctionInstrumentResult) -> InstrumentResultOut:
    return InstrumentResultOut.model_validate(res, from_attributes=True)


def _event_to_dto(event: AuctionEventTracking) -> EventTrackingOut:
    return EventTrackingOut.model_validate(event, from_attributes=True)


def _anchor_to_dto(anchor: AuctionAnchorItem) -> AnchorItemOut:
    return AnchorItemOut.model_validate(anchor, from_attributes=True)


# =============================================================================
# 1. GET /auction — 市场级页面数据
# =============================================================================


@router.get("", response_model=AuctionMarketPageData)
async def get_market_page(
    trade_date: date | None = Query(None, description="业务交易日（默认当日）"),
    top_n: int = Query(
        DEFAULT_TOP_BOARDS, ge=1, le=50, description="行业/概念 Top N",
    ),
    top_events: int = Query(
        DEFAULT_TOP_EVENTS, ge=1, le=100, description="Top 事件数",
    ),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_authenticated),
) -> AuctionMarketPageData:
    """市场级页面数据。

    - 查询当日已发布的 scan_run（auction_type=final）
    - 查询 market scope result
    - 查询行业和概念 scope results（按 median_change_pct 排序，取前 N）
    - 查询 top events（按 event_type 分组，取最重要的）
    """
    _ = ctx
    resolved_date = _resolve_trade_date(trade_date)

    run = await _get_latest_scan_run(db, resolved_date, auction_type="final")
    if run is None:
        return AuctionMarketPageData(
            trade_date=resolved_date,
            algorithm_version=AUCTION_SCAN_ALGORITHM_VERSION,
            reason_codes=["scan_run_not_found"],
        )

    # 锚点发布信息（publication_id + source run IDs）
    anchors_info = await get_published_anchors(db, resolved_date)

    # market scope
    market_scope = await _get_market_scope(db, run.id)

    # industry scopes（按 median_change_pct 降序，NULLS LAST，取前 N）
    industry_stmt = (
        select(AuctionScopeResult)
        .where(
            AuctionScopeResult.scan_run_id == run.id,
            AuctionScopeResult.scope_type == "industry",
        )
        .order_by(
            AuctionScopeResult.median_change_pct.desc().nullslast(),
        )
        .limit(top_n)
    )
    industry_scopes = list((await db.execute(industry_stmt)).scalars().all())

    # concept scopes（按 median_change_pct 降序，NULLS LAST，取前 N）
    concept_stmt = (
        select(AuctionScopeResult)
        .where(
            AuctionScopeResult.scan_run_id == run.id,
            AuctionScopeResult.scope_type == "concept",
        )
        .order_by(
            AuctionScopeResult.median_change_pct.desc().nullslast(),
        )
        .limit(top_n)
    )
    concept_scopes = list((await db.execute(concept_stmt)).scalars().all())

    # top events（按 event_type 分组取最重要的：formed_at 最新优先）
    events_stmt = (
        select(AuctionEventTracking)
        .where(AuctionEventTracking.scan_run_id == run.id)
        .order_by(
            AuctionEventTracking.event_type.asc(),
            AuctionEventTracking.formed_at.desc().nullslast(),
        )
        .limit(top_events)
    )
    top_event_list = list((await db.execute(events_stmt)).scalars().all())

    reason_codes: list[str] = []
    if market_scope is None:
        reason_codes.append("market_scope_missing")

    return AuctionMarketPageData(
        trade_date=resolved_date,
        algorithm_version=run.algorithm_version,
        publication_id=anchors_info.get("publication_id"),
        scan_run_id=run.id,
        source_core_run_id=anchors_info.get("source_core_run_id"),
        source_chip_run_id=anchors_info.get("source_chip_run_id"),
        coverage=run.coverage_ratio,
        reason_codes=reason_codes,
        market_scope=_scope_to_dto(market_scope) if market_scope else None,
        industry_scopes=[_scope_to_dto(s) for s in industry_scopes],
        concept_scopes=[_scope_to_dto(s) for s in concept_scopes],
        top_events=[_event_to_dto(e) for e in top_event_list],
    )


# =============================================================================
# 2. GET /auction/board/{board_id} — 板块级页面数据
# =============================================================================


@router.get("/board/{board_id}", response_model=AuctionBoardPageData)
async def get_board_page(
    board_id: uuid.UUID,
    trade_date: date | None = Query(None, description="业务交易日（默认当日）"),
    top_n: int = Query(
        DEFAULT_TOP_INSTRUMENTS, ge=1, le=100, description="Top 个股数",
    ),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_authenticated),
) -> AuctionBoardPageData:
    """板块级页面数据。

    - 查询指定板块的 scope result
    - 查询该板块下的 top instruments（按 change_pct 排序）
    - 查询该板块相关的事件
    """
    _ = ctx
    resolved_date = _resolve_trade_date(trade_date)

    run = await _get_latest_scan_run(db, resolved_date, auction_type="final")
    if run is None:
        return AuctionBoardPageData(
            trade_date=resolved_date,
            algorithm_version=AUCTION_SCAN_ALGORITHM_VERSION,
            reason_codes=["scan_run_not_found"],
        )

    # 板块 scope result
    scope = await _get_board_scope(db, run.id, board_id)

    # 板块成员 instrument_ids
    members_stmt = select(MarketBoardMembership.instrumentId).where(
        MarketBoardMembership.boardId == board_id,
    )
    member_ids = list((await db.execute(members_stmt)).scalars().all())

    # top instruments（按 change_pct 降序）+ 板块相关事件
    top_instruments: list[AuctionInstrumentResult] = []
    events: list[AuctionEventTracking] = []
    if member_ids:
        inst_stmt = (
            select(AuctionInstrumentResult)
            .where(
                AuctionInstrumentResult.scan_run_id == run.id,
                AuctionInstrumentResult.instrument_id.in_(member_ids),
                AuctionInstrumentResult.change_pct.is_not(None),
            )
            .order_by(AuctionInstrumentResult.change_pct.desc())
            .limit(top_n)
        )
        top_instruments = list((await db.execute(inst_stmt)).scalars().all())

        evt_stmt = (
            select(AuctionEventTracking)
            .where(
                AuctionEventTracking.scan_run_id == run.id,
                AuctionEventTracking.instrument_id.in_(member_ids),
            )
            .order_by(AuctionEventTracking.formed_at.desc().nullslast())
            .limit(top_n)
        )
        events = list((await db.execute(evt_stmt)).scalars().all())

    reason_codes: list[str] = []
    if scope is None:
        reason_codes.append("board_scope_missing")
    if not member_ids:
        reason_codes.append("board_members_empty")

    return AuctionBoardPageData(
        trade_date=resolved_date,
        algorithm_version=run.algorithm_version,
        scan_run_id=run.id,
        scope=_scope_to_dto(scope) if scope else None,
        top_instruments=[_instrument_to_dto(i) for i in top_instruments],
        events=[_event_to_dto(e) for e in events],
        reason_codes=reason_codes,
    )


# =============================================================================
# 3. GET /auction/stock/{symbol} — 个股级页面数据
# =============================================================================


@router.get("/stock/{symbol}", response_model=AuctionInstrumentPageData)
async def get_stock_page(
    symbol: str,
    trade_date: date | None = Query(None, description="业务交易日（默认当日）"),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_authenticated),
) -> AuctionInstrumentPageData:
    """个股级页面数据。

    - 通过 symbol 查询 instrument_id
    - 查询该股票的锚点列表（active only）
    - 查询该股票的竞价结果
    - 查询该股票的事件追踪
    """
    _ = ctx
    resolved_date = _resolve_trade_date(trade_date)

    # 通过 symbol 查询 instrument
    inst_stmt = select(Instrument).where(Instrument.symbol == symbol).limit(1)
    instrument = (await db.execute(inst_stmt)).scalar_one_or_none()
    if instrument is None:
        return AuctionInstrumentPageData(
            trade_date=resolved_date,
            algorithm_version=AUCTION_SCAN_ALGORITHM_VERSION,
            reason_codes=["instrument_not_found"],
        )

    run = await _get_latest_scan_run(db, resolved_date, auction_type="final")

    # 锚点列表（active only）— 从已发布锚点快照查询
    anchors_info = await get_published_anchors(db, resolved_date)
    snapshot_id = anchors_info.get("snapshot_id")

    anchors: list[AuctionAnchorItem] = []
    if snapshot_id is not None:
        anchor_stmt = (
            select(AuctionAnchorItem)
            .where(
                AuctionAnchorItem.snapshot_id == snapshot_id,
                AuctionAnchorItem.instrument_id == instrument.id,
                AuctionAnchorItem.is_active.is_(True),
            )
            .order_by(AuctionAnchorItem.strength.desc())
        )
        anchors = list((await db.execute(anchor_stmt)).scalars().all())

    # 竞价结果 + 事件
    result: AuctionInstrumentResult | None = None
    events: list[AuctionEventTracking] = []
    if run is not None:
        res_stmt = (
            select(AuctionInstrumentResult)
            .where(
                AuctionInstrumentResult.scan_run_id == run.id,
                AuctionInstrumentResult.instrument_id == instrument.id,
            )
            .limit(1)
        )
        result = (await db.execute(res_stmt)).scalar_one_or_none()

        evt_stmt = (
            select(AuctionEventTracking)
            .where(
                AuctionEventTracking.scan_run_id == run.id,
                AuctionEventTracking.instrument_id == instrument.id,
            )
            .order_by(AuctionEventTracking.formed_at.desc().nullslast())
        )
        events = list((await db.execute(evt_stmt)).scalars().all())

    reason_codes: list[str] = []
    if snapshot_id is None:
        reason_codes.append("anchor_not_published")
    if run is None:
        reason_codes.append("scan_run_not_found")
    elif result is None:
        reason_codes.append("instrument_result_missing")

    return AuctionInstrumentPageData(
        trade_date=resolved_date,
        algorithm_version=(
            run.algorithm_version
            if run is not None
            else AUCTION_SCAN_ALGORITHM_VERSION
        ),
        scan_run_id=run.id if run is not None else None,
        instrument_id=instrument.id,
        anchors=[_anchor_to_dto(a) for a in anchors],
        result=_instrument_to_dto(result) if result else None,
        events=[_event_to_dto(e) for e in events],
        reason_codes=reason_codes,
    )


# =============================================================================
# 4. GET /auction/anchors/{trade_date} — 锚点快照和发布状态
# =============================================================================


class AnchorStatusResponse(BaseModel):
    """锚点快照 + 发布状态响应。"""

    snapshot: AnchorSnapshotOut | None = None
    publication: AnchorPublicationOut | None = None
    reason_codes: list[str] = Field(default_factory=list)


@router.get("/anchors/{trade_date}", response_model=AnchorStatusResponse)
async def get_anchor_status(
    trade_date: date,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_authenticated),
) -> AnchorStatusResponse:
    """查询指定日期的锚点快照和发布状态。

    返回 AnchorSnapshotOut + AnchorPublicationOut。
    """
    _ = ctx
    # 查询发布指针（最新一条）
    pub_stmt = (
        select(AuctionAnchorPublication)
        .where(AuctionAnchorPublication.trade_date == trade_date)
        .order_by(AuctionAnchorPublication.published_at.desc())
        .limit(1)
    )
    publication = (await db.execute(pub_stmt)).scalar_one_or_none()

    if publication is None:
        return AnchorStatusResponse(reason_codes=["publication_not_found"])

    # 查询快照
    snapshot = await db.get(AuctionAnchorSnapshot, publication.snapshot_id)

    reason_codes: list[str] = []
    if snapshot is None:
        reason_codes.append("snapshot_not_found")

    return AnchorStatusResponse(
        snapshot=(
            AnchorSnapshotOut.model_validate(snapshot, from_attributes=True)
            if snapshot is not None
            else None
        ),
        publication=AnchorPublicationOut.model_validate(
            publication, from_attributes=True,
        ),
        reason_codes=reason_codes,
    )


# =============================================================================
# 5. POST /admin/auction/scan — 触发竞价扫描（admin）
# =============================================================================


class AdminScanRequest(BaseModel):
    """触发竞价扫描请求。"""

    trade_date: date
    auction_type: str = Field(
        default="final", description="final（最终竞价）/ opening（开盘验证）",
    )


class AdminScanResponse(BaseModel):
    """触发竞价扫描响应（scan_run 概要）。"""

    trade_date: date
    auction_type: str
    algorithm_version: str
    scan_run_id: uuid.UUID | None = None
    status: str
    eligible_count: int = 0
    ready_count: int = 0
    coverage_ratio: float = 0.0
    result_count: int = 0
    event_count: int = 0
    aggregation_status: str | None = None
    reason_codes: list[str] = Field(default_factory=list)


@admin_router.post(
    "/scan",
    response_model=AdminScanResponse,
    status_code=status.HTTP_201_CREATED,
)
async def trigger_scan(
    payload: AdminScanRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_admin),
) -> AdminScanResponse:
    """[Admin] 触发竞价扫描 + 聚合计算。

    流程：
    1. run_auction_scan（基于已发布锚点扫描竞价数据）
    2. compute_auction_aggregation（计算板块/市场聚合）
    3. 返回 scan_run 概要

    Raises:
        HTTPException 409: 锚点未发布或已过期
        HTTPException 500: 其他内部错误
    """
    _ = ctx
    try:
        # 1. 扫描
        scan_result = await run_auction_scan(
            db,
            trade_date=payload.trade_date,
            auction_type=payload.auction_type,
        )
        await db.commit()

        run_id = scan_result.get("run_id")
        aggregation_status: str | None = None

        # 2. 聚合（仅在扫描成功/部分成功时）
        if run_id is not None and scan_result.get("status") in (
            "succeeded",
            "partial",
        ):
            try:
                agg_result = await compute_auction_aggregation(db, run_id)
                await db.commit()
                aggregation_status = "succeeded"
                _ = agg_result  # 概要不回传，前端可调 GET 接口查看
            except Exception as exc:
                await db.rollback()
                logger.exception(
                    "[Admin] 竞价聚合失败: run_id=%s: %s", run_id, exc,
                )
                aggregation_status = "failed"

        reason_codes: list[str] = []
        if aggregation_status is None:
            reason_codes.append("aggregation_skipped")
        elif aggregation_status == "failed":
            reason_codes.append("aggregation_failed")

        return AdminScanResponse(
            trade_date=payload.trade_date,
            auction_type=payload.auction_type,
            algorithm_version=AUCTION_SCAN_ALGORITHM_VERSION,
            scan_run_id=run_id,
            status=scan_result.get("status", "unknown"),
            eligible_count=scan_result.get("eligible_count", 0),
            ready_count=scan_result.get("ready_count", 0),
            coverage_ratio=scan_result.get("coverage_ratio", 0.0),
            result_count=scan_result.get("result_count", 0),
            event_count=scan_result.get("event_count", 0),
            aggregation_status=aggregation_status,
            reason_codes=reason_codes,
        )
    except AnchorNotPublishedError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"锚点未发布，禁止扫描: {exc}",
        ) from exc
    except AnchorExpiredError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"锚点快照已过期，禁止扫描: {exc}",
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception(
            "[Admin] 触发竞价扫描失败: trade_date=%s", payload.trade_date,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"触发竞价扫描失败: {exc}",
        ) from exc


# =============================================================================
# 6. POST /admin/auction/anchors — 触发锚点生成（admin）
# =============================================================================


class AdminAnchorsRequest(BaseModel):
    """触发锚点生成请求。"""

    trade_date: date


class AdminAnchorsResponse(BaseModel):
    """触发锚点生成响应（锚点概要）。"""

    trade_date: date
    algorithm_version: str
    snapshot_id: uuid.UUID | None = None
    status: str
    structure_count: int = 0
    chip_count: int = 0
    composite_count: int = 0
    eligible_count: int = 0
    coverage_ratio: float = 0.0
    publication_id: uuid.UUID | None = None
    reason_codes: list[str] = Field(default_factory=list)


@admin_router.post(
    "/anchors",
    response_model=AdminAnchorsResponse,
    status_code=status.HTTP_201_CREATED,
)
async def trigger_anchors(
    payload: AdminAnchorsRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_admin),
) -> AdminAnchorsResponse:
    """[Admin] 触发锚点生成 + 发布。

    流程：
    1. generate_auction_anchors（从 stock_core + chip_consensus 提取锚点）
    2. publish_auction_anchors（校验后写入发布指针）
    3. 返回锚点概要

    Raises:
        HTTPException 500: 内部错误
    """
    _ = ctx
    try:
        # 1. 生成锚点
        gen_result = await generate_auction_anchors(
            db,
            trade_date=payload.trade_date,
        )
        await db.commit()

        snapshot_id = gen_result.get("snapshot_id")
        publication_id: uuid.UUID | None = None
        reason_codes: list[str] = []

        # 2. 发布（仅在生成成功/structure_only 时）
        if snapshot_id is not None and gen_result.get("status") in (
            "succeeded",
            "structure_only",
        ):
            try:
                publication = await publish_auction_anchors(db, snapshot_id)
                await db.commit()
                publication_id = publication.id
            except (
                AnchorSnapshotNotFoundError,
                AnchorSnapshotNotReadyError,
                AnchorVersionMismatchError,
                AnchorCoverageLowError,
            ) as exc:
                await db.rollback()
                logger.warning(
                    "[Admin] 锚点发布失败: snapshot_id=%s: %s", snapshot_id, exc,
                )
                reason_codes.append(f"publish_failed: {exc}")
        else:
            reason_codes.append("publish_skipped")

        return AdminAnchorsResponse(
            trade_date=payload.trade_date,
            algorithm_version=AUCTION_ANCHOR_ALGORITHM_VERSION,
            snapshot_id=snapshot_id,
            status=gen_result.get("status", "unknown"),
            structure_count=gen_result.get("structure_count", 0),
            chip_count=gen_result.get("chip_count", 0),
            composite_count=gen_result.get("composite_count", 0),
            eligible_count=gen_result.get("eligible_count", 0),
            coverage_ratio=gen_result.get("coverage_ratio", 0.0),
            publication_id=publication_id,
            reason_codes=reason_codes,
        )
    except Exception as exc:
        await db.rollback()
        logger.exception(
            "[Admin] 触发锚点生成失败: trade_date=%s", payload.trade_date,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"触发锚点生成失败: {exc}",
        ) from exc


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    admin_paths = [r.path for r in admin_router.routes]
    print(f"router prefix: {router.prefix}")
    print(f"端点数: {len(paths)}")
    for p in paths:
        print(f"  {p}")
    print(f"admin_router prefix: {admin_router.prefix}")
    print(f"admin 端点数: {len(admin_paths)}")
    for p in admin_paths:
        print(f"  {p}")
    print("OK")
