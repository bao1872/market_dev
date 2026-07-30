"""复盘模块用户端 API 路由（PRD §12.1-12.5）。

端点：
- GET  /api/v1/review/dates: 已发布复盘交易日列表
- GET  /api/v1/review/latest: 最新已发布复盘 run 信息
- GET  /api/v1/review/{trade_date}/overview: 复盘总览（覆盖率+信号汇总）
- GET  /api/v1/review/{trade_date}/scopes: 市场扫描（P/Q/U/C/V）
- GET  /api/v1/review/{trade_date}/signals: 信号列表
- GET  /api/v1/review/signals/{signal_id}: 信号详情
- GET  /api/v1/review/signals/{signal_id}/attributions: 子范围归因
- GET  /api/v1/review/signals/{signal_id}/instruments: 个股归因
- GET  /api/v1/review/trackings: 用户追踪列表
- POST /api/v1/review/trackings: 创建追踪（幂等）
- PATCH /api/v1/review/trackings/{id}: 更新追踪（幂等）
- DELETE /api/v1/review/trackings/{id}: 关闭追踪（幂等，不物理删除）
- GET  /api/v1/review/trackings/{id}/evaluations: 追踪逐日评估

权限（PRD §3.2）：
- 所有读取接口：require_capability("research_replay")（admin 自动豁免）
- 追踪写接口：require_capability("research_replay")
- 普通用户只能看到已发布 run（published pointer）；
  admin 可通过 include_partial=true 查看 partial 结果。

设计要点：
- 用户侧路由不触发计算，只读 DB
- 写操作（追踪）要求 idempotency_key（PRD §12.6）
- 失败返回 4xx/5xx，不静默返回空数据
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.models.market_review import (
    MarketReviewRun,
    MarketReviewScopeSnapshot,
    MarketReviewSignal,
    MarketReviewSignalAttribution,
    MarketReviewSignalInstrument,
    MarketReviewTracking,
    MarketReviewTrackingEvaluation,
)
from app.schemas.review import (
    ReviewAttributionListResponse,
    ReviewAttributionResponse,
    ReviewDatesResponse,
    ReviewInstrumentListResponse,
    ReviewInstrumentResponse,
    ReviewLatestResponse,
    ReviewOverviewCoverageDTO,
    ReviewOverviewResponse,
    ReviewOverviewSignalSummaryDTO,
    ReviewScopeListResponse,
    ReviewScopeMetricsResponse,
    ReviewSignalListResponse,
    ReviewSignalResponse,
    ReviewTrackingCreateRequest,
    ReviewTrackingEvaluationListResponse,
    ReviewTrackingEvaluationResponse,
    ReviewTrackingListResponse,
    ReviewTrackingPatchRequest,
    ReviewTrackingResponse,
)
from app.services.access_control_service import (
    AccessContext,
    require_admin,
    require_capability,
)
from app.services.review_attribution_service import (
    list_attributions,
    list_instruments,
)
from app.services.review_publication_service import (
    get_published_review_run_id,
    list_published_review_dates,
)
from app.services.review_scope_service import list_scope_snapshots
from app.services.review_signal_service import (
    count_signals_by_status,
    get_signal,
    list_signals,
)
from app.services.review_tracking_service import (
    TrackingError,
    close_tracking,
    create_tracking,
    get_tracking_for_user,
    list_evaluations,
    list_trackings,
    update_tracking,
)

logger = logging.getLogger("api.review")

router = APIRouter(prefix="/api/v1/review", tags=["review"])

# research_replay capability 是复盘模块的权限门禁（PRD60 PA-12）
REVIEW_CAPABILITY = "research_replay"


# =============================================================================
# 内部工具
# =============================================================================


async def _get_published_run(
    session: AsyncSession,
    trade_date: date,
    *,
    include_partial: bool = False,
) -> MarketReviewRun:
    """读取已发布的 review run；include_partial=True 时回退到任意 run。

    Args:
        session: 异步 DB 会话
        trade_date: 业务交易日
        include_partial: True 时允许返回 partial/failed run（仅 admin 用）

    Returns:
        MarketReviewRun ORM 对象

    Raises:
        HTTPException 404: 无已发布 run
    """
    run_id = await get_published_review_run_id(session, trade_date)
    if run_id is not None:
        run = await session.get(MarketReviewRun, run_id)
        if run is not None:
            return run

    if not include_partial:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"trade_date={trade_date} 无已发布复盘 run",
        )

    # include_partial=True: 回退到任意 run（admin 调试用）
    stmt = (
        select(MarketReviewRun)
        .where(MarketReviewRun.trade_date == trade_date)
        .order_by(MarketReviewRun.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"trade_date={trade_date} 无任何 review run（含 partial）",
        )
    return run


async def _load_signal_or_404(
    session: AsyncSession,
    signal_id: uuid.UUID,
) -> MarketReviewSignal:
    """加载信号，不存在抛 404。"""
    signal = await get_signal(session, signal_id)
    if signal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"信号不存在: signal_id={signal_id}",
        )
    return signal


def _scope_snapshot_to_dto(
    snap: MarketReviewScopeSnapshot,
    *,
    signal_count: int = 0,
) -> ReviewScopeMetricsResponse:
    """scope snapshot ORM → DTO。"""
    return ReviewScopeMetricsResponse(
        id=str(snap.id),
        reviewRunId=str(snap.review_run_id),
        tradeDate=snap.trade_date.isoformat(),
        scopeType=snap.scope_type,
        scopeKey=snap.scope_key,
        scopeName=snap.scope_name,
        parentScopeType=snap.parent_scope_type,
        parentScopeKey=snap.parent_scope_key,
        eligibleCount=snap.eligible_count,
        readyCount=snap.ready_count,
        coverageRatio=float(snap.coverage_ratio),
        status=snap.status,
        p=snap.p_payload,
        q=snap.q_payload,
        u=snap.u_payload,
        c=snap.c_payload,
        v=snap.v_payload,
        dataQuality=snap.data_quality_json,
        signalCount=signal_count,
    )


def _signal_to_dto(sig: MarketReviewSignal) -> ReviewSignalResponse:
    """signal ORM → DTO（含 durationDays 注入）。"""
    duration_days = 0
    if sig.first_seen_date and sig.trade_date:
        duration_days = (sig.trade_date - sig.first_seen_date).days
    return ReviewSignalResponse(
        id=str(sig.id),
        reviewRunId=str(sig.review_run_id),
        tradeDate=sig.trade_date.isoformat(),
        filterFamily=sig.filter_family,
        signalType=sig.signal_type,
        scopeType=sig.scope_type,
        scopeKey=sig.scope_key,
        scopeName=sig.scope_name,
        status=sig.status,
        firstSeenDate=sig.first_seen_date.isoformat(),
        previousSignalId=str(sig.previous_signal_id) if sig.previous_signal_id else None,
        transformedToSignalId=(
            str(sig.transformed_to_signal_id)
            if sig.transformed_to_signal_id else None
        ),
        triggerPayload=sig.trigger_payload or {},
        baselinePayload=sig.baseline_payload or {},
        evidencePayload=sig.evidence_payload or {},
        confirmationRule=sig.confirmation_rule or {},
        invalidationRule=sig.invalidation_rule or {},
        coverageRatio=float(sig.coverage_ratio) if sig.coverage_ratio else None,
        rankKey=sig.rank_key or {},
        durationDays=duration_days,
        createdAt=sig.created_at.isoformat() if sig.created_at else None,
    )


def _attribution_to_dto(attr: MarketReviewSignalAttribution) -> ReviewAttributionResponse:
    """attribution ORM → DTO。"""
    return ReviewAttributionResponse(
        id=str(attr.id),
        signalId=str(attr.signal_id),
        childScopeType=attr.child_scope_type,
        childScopeKey=attr.child_scope_key,
        childScopeName=attr.child_scope_name,
        relationType=attr.relation_type,
        contributionValue=(
            float(attr.contribution_value) if attr.contribution_value is not None else None
        ),
        contributionRank=attr.contribution_rank,
        metricsPayload=attr.metrics_payload or {},
        evidencePayload=attr.evidence_payload or {},
        coverageRatio=float(attr.coverage_ratio) if attr.coverage_ratio else None,
        createdAt=attr.created_at.isoformat() if attr.created_at else None,
    )


def _instrument_to_dto(inst: MarketReviewSignalInstrument) -> ReviewInstrumentResponse:
    """instrument ORM → DTO。"""
    return ReviewInstrumentResponse(
        id=str(inst.id),
        signalId=str(inst.signal_id),
        instrumentId=str(inst.instrument_id),
        symbol=inst.symbol,
        name=inst.name,
        boardRole=inst.board_role,
        relationToScope=inst.relation_to_scope,
        contributionValue=(
            float(inst.contribution_value)
            if inst.contribution_value is not None else None
        ),
        contributionRank=inst.contribution_rank,
        firstPyramidPayload=inst.first_pyramid_payload or {},
        freshEventsPayload=inst.fresh_events_payload or {},
        sourceSnapshotId=(
            str(inst.source_snapshot_id) if inst.source_snapshot_id else None
        ),
        createdAt=inst.created_at.isoformat() if inst.created_at else None,
    )


def _tracking_to_dto(tracking: MarketReviewTracking) -> ReviewTrackingResponse:
    """tracking ORM → DTO。"""
    return ReviewTrackingResponse(
        id=str(tracking.id),
        userId=str(tracking.user_id),
        sourceSignalId=(
            str(tracking.source_signal_id) if tracking.source_signal_id else None
        ),
        trackingType=tracking.tracking_type,
        scopeType=tracking.scope_type,
        scopeKey=tracking.scope_key,
        instrumentId=(
            str(tracking.instrument_id) if tracking.instrument_id else None
        ),
        status=tracking.status,
        confirmationConditions=tracking.confirmation_conditions or {},
        invalidationConditions=tracking.invalidation_conditions or {},
        note=tracking.note,
        createdAt=tracking.created_at.isoformat() if tracking.created_at else "",
        closedAt=tracking.closed_at.isoformat() if tracking.closed_at else None,
    )


def _evaluation_to_dto(
    evaluation: MarketReviewTrackingEvaluation,
) -> ReviewTrackingEvaluationResponse:
    """evaluation ORM → DTO。"""
    return ReviewTrackingEvaluationResponse(
        id=str(evaluation.id),
        trackingId=str(evaluation.tracking_id),
        reviewRunId=str(evaluation.review_run_id),
        tradeDate=evaluation.trade_date.isoformat(),
        previousState=evaluation.previous_state,
        currentState=evaluation.current_state,
        evaluationPayload=evaluation.evaluation_payload or {},
        createdAt=evaluation.created_at.isoformat() if evaluation.created_at else "",
    )


def _parse_date_or_422(date_str: str) -> date:
    """解析 YYYY-MM-DD 字符串为 date，失败抛 422。"""
    try:
        return date.fromisoformat(date_str)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"trade_date 格式错误（需 YYYY-MM-DD）: {date_str}, error={exc}",
        ) from exc


# =============================================================================
# 12.1 日期与总览
# =============================================================================


@router.get("/dates", response_model=ReviewDatesResponse)
async def get_review_dates(
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewDatesResponse:
    """已发布复盘交易日列表（降序）。"""
    _ = ctx
    dates = await list_published_review_dates(db, limit=200)
    return ReviewDatesResponse(
        trade_dates=[d.isoformat() for d in dates],
        latest_trade_date=dates[0].isoformat() if dates else None,
    )


@router.get("/latest", response_model=ReviewLatestResponse)
async def get_latest_review(
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewLatestResponse:
    """最新已发布复盘 run 基本信息。"""
    _ = ctx
    dates = await list_published_review_dates(db, limit=1)
    if not dates:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="无已发布复盘",
        )
    trade_date = dates[0]
    run_id = await get_published_review_run_id(db, trade_date)
    if run_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"trade_date={trade_date} 缺少 publication pointer",
        )
    run = await db.get(MarketReviewRun, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"run_id={run_id} 不存在",
        )
    return ReviewLatestResponse(
        review_run_id=str(run.id),
        trade_date=run.trade_date.isoformat(),
        status=run.status,
        algorithm_version=run.algorithm_version,
        filter_version=run.filter_version,
    )


@router.get("/{trade_date}/overview", response_model=ReviewOverviewResponse)
async def get_review_overview(
    trade_date: str,
    include_partial: bool = Query(
        False, description="admin 调试用：允许查看 partial/failed run",
    ),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewOverviewResponse:
    """指定交易日的复盘总览（覆盖率+信号汇总）。"""
    # include_partial=True 仅 admin 可用
    if include_partial and not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="include_partial 仅管理员可用",
        )

    td = _parse_date_or_422(trade_date)
    run = await _get_published_run(
        db, td, include_partial=include_partial,
    )

    # 计算 coverage 明细
    coverage = ReviewOverviewCoverageDTO()
    scopes = await list_scope_snapshots(db, run.id)
    market_snaps = [s for s in scopes if s.scope_type == "market"]
    index_snaps = [s for s in scopes if s.scope_type == "major_index"]
    style_snaps = [s for s in scopes if s.scope_type == "style"]
    industry_snaps = [s for s in scopes if s.scope_type == "industry_l1"]

    if market_snaps:
        coverage.market = float(market_snaps[0].coverage_ratio)
    if index_snaps:
        ready = sum(1 for s in index_snaps if s.status == "ready")
        coverage.indices = ready / len(index_snaps) if index_snaps else None
    if style_snaps:
        ready = sum(1 for s in style_snaps if s.status == "ready")
        coverage.styles = ready / len(style_snaps) if style_snaps else None
    if industry_snaps:
        ready = sum(1 for s in industry_snaps if s.status == "ready")
        coverage.industryL1 = ready / len(industry_snaps) if industry_snaps else None

    # 信号汇总
    status_counts = await count_signals_by_status(db, run.id)
    signal_summary = ReviewOverviewSignalSummaryDTO(
        new=status_counts.get("new", 0),
        continuing=status_counts.get("continuing", 0),
        confirmed=status_counts.get("confirmed", 0),
        weakened=status_counts.get("weakened", 0),
        invalidated=status_counts.get("invalidated", 0),
        transformed=status_counts.get("transformed", 0),
    )

    return ReviewOverviewResponse(
        reviewRunId=str(run.id),
        tradeDate=run.trade_date.isoformat(),
        status=run.status,
        sourceCoreRunId=str(run.source_core_run_id),
        sourceBoardRunId=str(run.source_board_run_id),
        algorithmVersion=run.algorithm_version,
        filterVersion=run.filter_version,
        baselineWindow=run.baseline_window,
        coverage=coverage,
        signalSummary=signal_summary,
        coverageRatio=float(run.coverage_ratio) if run.coverage_ratio else None,
        expectedScopeCount=run.expected_scope_count,
        succeededScopeCount=run.succeeded_scope_count,
        failedScopeCount=run.failed_scope_count,
        signalCount=run.signal_count,
        startedAt=run.started_at.isoformat() if run.started_at else None,
        completedAt=run.completed_at.isoformat() if run.completed_at else None,
        publishedAt=run.published_at.isoformat() if run.published_at else None,
    )


# =============================================================================
# 12.2 市场扫描
# =============================================================================


@router.get("/{trade_date}/scopes", response_model=ReviewScopeListResponse)
async def list_review_scopes(
    trade_date: str,
    scope_type: str | None = Query(None, description="范围类型过滤"),
    parent_scope_type: str | None = Query(None, description="父范围类型过滤"),
    parent_scope_key: str | None = Query(None, description="父范围标识过滤"),
    include_partial: bool = Query(False, description="admin 调试用"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewScopeListResponse:
    """市场扫描 - 列出指定交易日的所有范围 P/Q/U/C/V。"""
    if include_partial and not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="include_partial 仅管理员可用",
        )

    td = _parse_date_or_422(trade_date)
    run = await _get_published_run(db, td, include_partial=include_partial)

    scopes = await list_scope_snapshots(
        db,
        run.id,
        scope_type=scope_type,
        parent_scope_type=parent_scope_type,
        parent_scope_key=parent_scope_key,
    )

    total = len(scopes)
    offset = (page - 1) * page_size
    paged = scopes[offset:offset + page_size]
    items = [_scope_snapshot_to_dto(s) for s in paged]

    return ReviewScopeListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(offset + page_size) < total,
    )


# =============================================================================
# 12.3 信号
# =============================================================================


@router.get("/{trade_date}/signals", response_model=ReviewSignalListResponse)
async def list_review_signals(
    trade_date: str,
    filter_family: str | None = Query(None, description="筛选器族 A/B/C"),
    signal_type: str | None = Query(None, description="信号类型"),
    signal_status: str | None = Query(
        None, alias="status", description="生命周期状态",
    ),
    scope_type: str | None = Query(None, description="范围类型"),
    scope_key: str | None = Query(None, description="范围标识"),
    include_partial: bool = Query(False, description="admin 调试用"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewSignalListResponse:
    """信号列表 - 指定交易日的命中信号（可过滤）。"""
    if include_partial and not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="include_partial 仅管理员可用",
        )

    td = _parse_date_or_422(trade_date)
    run = await _get_published_run(db, td, include_partial=include_partial)

    # 校验 filter_family
    if filter_family is not None and filter_family not in ("A", "B", "C"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid filter_family: {filter_family}; must be A/B/C",
        )

    signals, total = await list_signals(
        db,
        run.id,
        filter_family=filter_family,
        signal_type=signal_type,
        status=signal_status,
        scope_type=scope_type,
        scope_key=scope_key,
        page=page,
        page_size=page_size,
    )
    items = [_signal_to_dto(s) for s in signals]
    return ReviewSignalListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@router.get("/signals/{signal_id}", response_model=ReviewSignalResponse)
async def get_review_signal(
    signal_id: uuid.UUID,
    include_partial: bool = Query(False, description="admin 调试用"),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewSignalResponse:
    """信号详情。"""
    if include_partial and not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="include_partial 仅管理员可用",
        )

    signal = await _load_signal_or_404(db, signal_id)

    # 非 include_partial 模式下，校验信号所在 run 已发布
    if not include_partial:
        run_id = await get_published_review_run_id(db, signal.trade_date)
        if run_id is None or run_id != signal.review_run_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"信号 {signal_id} 所在 run 未发布",
            )

    return _signal_to_dto(signal)


# =============================================================================
# 12.4 归因与个股
# =============================================================================


@router.get(
    "/signals/{signal_id}/attributions",
    response_model=ReviewAttributionListResponse,
)
async def list_signal_attributions(
    signal_id: uuid.UUID,
    include_partial: bool = Query(False, description="admin 调试用"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewAttributionListResponse:
    """子范围归因列表（按 contribution_rank 升序）。"""
    if include_partial and not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="include_partial 仅管理员可用",
        )

    signal = await _load_signal_or_404(db, signal_id)
    if not include_partial:
        run_id = await get_published_review_run_id(db, signal.trade_date)
        if run_id is None or run_id != signal.review_run_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"信号 {signal_id} 所在 run 未发布",
            )

    attrs, total = await list_attributions(
        db, signal_id, page=page, page_size=page_size,
    )
    items = [_attribution_to_dto(a) for a in attrs]
    return ReviewAttributionListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@router.get(
    "/signals/{signal_id}/instruments",
    response_model=ReviewInstrumentListResponse,
)
async def list_signal_instruments(
    signal_id: uuid.UUID,
    board_role: str | None = Query(None, description="板块角色过滤"),
    relation_to_scope: str | None = Query(None, description="与板块关系过滤"),
    include_partial: bool = Query(False, description="admin 调试用"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewInstrumentListResponse:
    """个股归因列表（按 contribution_rank 升序）。"""
    if include_partial and not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="include_partial 仅管理员可用",
        )

    signal = await _load_signal_or_404(db, signal_id)
    if not include_partial:
        run_id = await get_published_review_run_id(db, signal.trade_date)
        if run_id is None or run_id != signal.review_run_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"信号 {signal_id} 所在 run 未发布",
            )

    instruments, total = await list_instruments(
        db,
        signal_id,
        board_role=board_role,
        relation_to_scope=relation_to_scope,
        page=page,
        page_size=page_size,
    )
    items = [_instrument_to_dto(i) for i in instruments]
    return ReviewInstrumentListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


# =============================================================================
# 12.5 追踪
# =============================================================================


@router.get("/trackings", response_model=ReviewTrackingListResponse)
async def list_my_trackings(
    tracking_type: str | None = Query(None, description="追踪类型过滤"),
    tracking_status: str | None = Query(
        None, alias="status", description="状态过滤",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewTrackingListResponse:
    """当前用户的追踪列表。"""
    user_id = uuid.UUID(ctx.user_id)
    trackings, total = await list_trackings(
        db,
        user_id,
        status=tracking_status,
        tracking_type=tracking_type,
        page=page,
        page_size=page_size,
    )
    items = [_tracking_to_dto(t) for t in trackings]
    return ReviewTrackingListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@router.post(
    "/trackings",
    response_model=ReviewTrackingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_my_tracking(
    payload: ReviewTrackingCreateRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewTrackingResponse:
    """创建追踪（幂等：相同 idempotency_key 不重复创建）。"""
    user_id = uuid.UUID(ctx.user_id)

    # 解析可选字段
    source_signal_id = (
        uuid.UUID(payload.source_signal_id) if payload.source_signal_id else None
    )
    instrument_id = (
        uuid.UUID(payload.instrument_id) if payload.instrument_id else None
    )

    try:
        tracking = await create_tracking(
            db,
            user_id=user_id,
            tracking_type=payload.tracking_type,
            source_signal_id=source_signal_id,
            scope_type=payload.scope_type,
            scope_key=payload.scope_key,
            instrument_id=instrument_id,
            confirmation_conditions=payload.confirmation_conditions or None,
            invalidation_conditions=payload.invalidation_conditions or None,
            note=payload.note,
            idempotency_key=payload.idempotency_key,
        )
        await db.commit()
        await db.refresh(tracking)
    except TrackingError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception("[Review] 创建追踪失败: user=%s", ctx.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建追踪失败: {exc}",
        ) from exc

    return _tracking_to_dto(tracking)


@router.patch(
    "/trackings/{tracking_id}",
    response_model=ReviewTrackingResponse,
)
async def update_my_tracking(
    tracking_id: uuid.UUID,
    payload: ReviewTrackingPatchRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewTrackingResponse:
    """更新追踪字段（幂等键必须提供）。"""
    user_id = uuid.UUID(ctx.user_id)
    tracking = await get_tracking_for_user(db, tracking_id, user_id)
    if tracking is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"追踪不存在或无权访问: tracking_id={tracking_id}",
        )

    try:
        tracking = await update_tracking(
            db,
            tracking,
            status=payload.status,
            confirmation_conditions=payload.confirmation_conditions,
            invalidation_conditions=payload.invalidation_conditions,
            note=payload.note,
        )
        await db.commit()
        await db.refresh(tracking)
    except TrackingError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception("[Review] 更新追踪失败: id=%s", tracking_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新追踪失败: {exc}",
        ) from exc

    return _tracking_to_dto(tracking)


@router.delete(
    "/trackings/{tracking_id}",
    response_model=ReviewTrackingResponse,
)
async def close_my_tracking(
    tracking_id: uuid.UUID,
    idempotency_key: str = Query(..., description="幂等键"),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewTrackingResponse:
    """关闭追踪（status=closed，不物理删除）。"""
    _ = idempotency_key  # 幂等键记录在请求日志，由调用方保证
    user_id = uuid.UUID(ctx.user_id)
    tracking = await get_tracking_for_user(db, tracking_id, user_id)
    if tracking is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"追踪不存在或无权访问: tracking_id={tracking_id}",
        )

    try:
        tracking = await close_tracking(db, tracking)
        await db.commit()
        await db.refresh(tracking)
    except TrackingError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception("[Review] 关闭追踪失败: id=%s", tracking_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"关闭追踪失败: {exc}",
        ) from exc

    return _tracking_to_dto(tracking)


@router.get(
    "/trackings/{tracking_id}/evaluations",
    response_model=ReviewTrackingEvaluationListResponse,
)
async def list_tracking_evaluations(
    tracking_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewTrackingEvaluationListResponse:
    """追踪的逐日评估记录（按 trade_date 降序）。"""
    user_id = uuid.UUID(ctx.user_id)
    # 权限校验：追踪必须属于当前用户
    tracking = await get_tracking_for_user(db, tracking_id, user_id)
    if tracking is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"追踪不存在或无权访问: tracking_id={tracking_id}",
        )

    evaluations, total = await list_evaluations(
        db, tracking_id, page=page, page_size=page_size,
    )
    items = [_evaluation_to_dto(e) for e in evaluations]
    return ReviewTrackingEvaluationListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


# =============================================================================
# 管理员辅助接口（include_partial 用，路径在 admin_review.py 中定义主流程）
# =============================================================================


# 注意：admin 路由统一在 admin_review.py 中定义；
# 此处仅暴露 require_admin 依赖（供 admin_review.py 复用）
__all__ = [
    "router",
    "require_admin",
]


if __name__ == "__main__":
    # 自测：路由前缀与端点数量
    paths = [r.path for r in router.routes]
    print(f"router prefix: {router.prefix}")
    print(f"端点数: {len(paths)}")
    for p in paths:
        print(f"  {p}")
