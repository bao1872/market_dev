"""复盘模块用户端 API 路由（PRD §12.1-12.5）。

端点：
- GET  /v1/review/dates: 已发布复盘交易日列表
- GET  /v1/review/latest: 最新已发布复盘 run 信息
- GET  /v1/review/{trade_date}/overview: 复盘总览（覆盖率）
- GET  /v1/review/{trade_date}/scopes: 市场扫描（Scope Observation 六键 summary）
- GET  /v1/review/{trade_date}/scopes/{scope_type}/{scope_key}: Scope Composition 详情

权限（PRD §3.2）：
- 所有读取接口：require_capability("research_replay")（admin 自动豁免）
- 普通用户只能看到已发布 run（published pointer）；
  admin 可通过 include_partial=true 查看 partial 结果。

设计要点：
- 用户侧路由不触发计算，只读 DB
- 失败返回 4xx/5xx，不静默返回空数据

遗留（REVIEW-BACKEND-FINAL-CLOSURE Phase 5 已退休）：
- legacy Signal / Discovery / Tracking 可达 API 路径已物理删除，返回 404。
- 底层 review_signal_service / review_discovery_service / review_tracking_service
  均已物理删除。历史 ORM 表（MarketReviewSignal 等）保留不 DROP。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.models.market_review import (
    MarketReviewRun,
    ReviewScopeObservationFact,
)
from app.schemas.review import (
    ReviewCanonicalScopeResponse,
    ReviewChipCoverageDTO,
    ReviewDatesResponse,
    ReviewLatestResponse,
    ReviewOverviewCoverageDTO,
    ReviewOverviewResponse,
    ReviewScopeCompositionDetailResponse,
    ReviewScopeListResponse,
)
from app.services.access_control_service import (
    AccessContext,
    require_admin,
    require_capability,
)
from app.services.review_observation_persistence_service import (
    get_scope_composition_snapshot,
    get_scope_observation_fact_by_run,
    list_scope_observation_facts,
    list_scope_observation_facts_by_run,
)
from app.services.review_publication_service import (
    get_published_review_run_id,
    list_published_review_dates,
)

logger = logging.getLogger("api.review")

router = APIRouter(prefix="/v1/review", tags=["review"])

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


async def _get_published_run_or_404(
    session: AsyncSession,
    trade_date: date,
) -> MarketReviewRun | None:
    """读取已发布 review run；无已发布 run 时返回 None（不抛 404）。

    供 Discovery 列表/详情端点使用：列表端点在无发布 run 时返回空列表
    （HTTP 200），详情端点随后显式抛 404。与 _get_published_run 的区别是
    本函数对“无已发布 run”返回 None 而非抛异常。
    """
    try:
        return await _get_published_run(session, trade_date)
    except HTTPException:
        return None


def _canonical_scope_fact_to_dto(
    fact: ReviewScopeObservationFact,
    *,
    composition_readiness: dict[str, str] | None = None,
    canonical_coverage: dict[str, dict[str, Any]] | None = None,
    signal_count: int = 0,
) -> ReviewCanonicalScopeResponse:
    """[REVIEW-CANONICAL-RUNTIME-REPLACEMENT] canonical fact → DTO。

    readiness 优先取 run metadata 中的 canonical composition readiness（唯一发布
    判断依据），缺失时回落 fact.readiness；coverage 取 metadata canonical_coverage
    的 provided/eligible（由唯一 composition owner 记录），缺失时用 fact 列派生。
    """
    coverage = (canonical_coverage or {}).get(fact.scope_key) or {}
    eligible = int(coverage.get("eligible", fact.pit_member_count) or 0)
    provided = int(coverage.get("provided", fact.provided_member_count) or 0)
    readiness = (composition_readiness or {}).get(fact.scope_key, fact.readiness)
    return ReviewCanonicalScopeResponse(
        scopeType=fact.scope_type,
        scopeKey=fact.scope_key,
        scopeName=fact.scope_name,
        readiness=readiness,
        status=fact.pit_status_t,
        eligibleCount=eligible,
        providedCount=provided,
        coverageRatio=float(provided / eligible) if eligible > 0 else None,
        observation=fact.observation_payload,
        signalCount=signal_count,
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


def _extract_chip_coverage(run: MarketReviewRun) -> ReviewChipCoverageDTO | None:
    """从 run.metadata_json 提取 chip 覆盖率明细（历史 run 兼容读取）。

    [AUD-04/05 2026-08-07] Review 已与 chip 解耦：create_run 不再写入
    chip_coverage，新建 run 此处恒返回 None（前端按“不可用”降级展示）。
    保留本函数仅为兼容解耦前已落库的历史 run。chip 就绪度应改由
    ProductReadiness / chip 域提供。
    """
    raw = (run.metadata_json or {}).get("chip_coverage")
    if not raw or not isinstance(raw, dict):
        return None
    return ReviewChipCoverageDTO(
        expectedCount=raw.get("expected_count"),
        succeededCount=int(raw.get("succeeded_count") or 0),
        failedCount=int(raw.get("failed_count") or 0),
        skippedCount=int(raw.get("skipped_count") or 0),
        missingCount=int(raw.get("missing_count") or 0),
        coverage=raw.get("coverage"),
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

    # [REVIEW-CANONICAL-RUNTIME-REPLACEMENT] coverage 明细从 legacy
    # MarketReviewScopeSnapshot 聚合切换为 canonical ReviewScopeObservationFact
    # （按 scope_type 聚合 provided/pit_member_count）。market/major_index/style 为
    # 未激活家族（ScopeCapability.persistence_activated=False），不产生 canonical
    # fact，其覆盖率为 None（合法跳过，绝不回退 legacy P/Q/U/C/V）。
    facts = await list_scope_observation_facts(
        db, from_date=run.trade_date, to_date=run.trade_date,
    )
    facts_by_type: dict[str, list[ReviewScopeObservationFact]] = {}
    for fact in facts:
        facts_by_type.setdefault(fact.scope_type, []).append(fact)

    def _family_ratio(scope_type: str) -> float | None:
        rows = facts_by_type.get(scope_type) or []
        if not rows:
            return None
        eligible = sum(f.pit_member_count for f in rows)
        provided = sum(f.provided_member_count or 0 for f in rows)
        if eligible <= 0:
            return None
        return provided / eligible

    coverage = ReviewOverviewCoverageDTO(
        market=None,
        indices=None,
        styles=None,
        industryL1=_family_ratio("industry_l1"),
    )

    # [REVIEW-BACKEND-FINAL-CLOSURE Phase 5] signal summary 已退休：
    # legacy Signal pipeline 不再计算，overview 不再含 signalSummary 字段。

    return ReviewOverviewResponse(
        reviewRunId=str(run.id),
        tradeDate=run.trade_date.isoformat(),
        status=run.status,
        sourceCoreRunId=str(run.source_core_run_id),
        sourceBoardRunId=str(run.source_board_run_id),
        # [QM-63] chip 依赖溯源：None 明确表示 core-only 降级，不得省略字段
        sourceChipRunId=(
            str(run.source_chip_run_id) if run.source_chip_run_id else None
        ),
        degradedReasons=list(run.degraded_reasons or []),
        # [P0 2026-08-04] chip 真实覆盖率明细（以 expected_count 为分母）
        chipCoverage=_extract_chip_coverage(run),
        algorithmVersion=run.algorithm_version,
        filterVersion=run.filter_version,
        baselineWindow=run.baseline_window,
        coverage=coverage,
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
    include_partial: bool = Query(False, description="admin 调试用"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewScopeListResponse:
    """市场扫描 - 列出指定交易日的 canonical Scope Observation facts。

    [REVIEW-CANONICAL-RUNTIME-REPLACEMENT] 不再返回 legacy P/Q/U/C/V
    （MarketReviewScopeSnapshot 已退役）；改读 canonical ReviewScopeObservationFact
    （仅 activated 家族 industry_l1/l2/l3/concept 有事实；market/major_index/style
    为未激活家族，无 canonical fact，其过滤结果为空是当前 capability 的如实呈现，
    绝不回退 legacy P/Q/U/C/V）。
    """
    if include_partial and not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="include_partial 仅管理员可用",
        )

    td = _parse_date_or_422(trade_date)
    run = await _get_published_run(db, td, include_partial=include_partial)
    metadata = run.metadata_json or {}
    readiness = metadata.get("canonical_composition_readiness") or {}
    canonical_coverage = metadata.get("canonical_coverage") or {}

    # [REVIEW-BACKEND-FINAL-CLOSURE Phase 4] 按 review_run_id 查询（grain 含
    # review_run_id），避免同日双 run 的 Observation lineage 污染（Phase 7 Gate A）。
    # scope_type 过滤在 router 内轻量完成（service 仅负责 run lineage 读取，
    # 不退回 global trade_date scan）。
    facts = await list_scope_observation_facts_by_run(
        db,
        review_run_id=run.id,
    )
    if scope_type:
        facts = [f for f in facts if f.scope_type == scope_type]

    total = len(facts)
    offset = (page - 1) * page_size
    paged = facts[offset:offset + page_size]
    items = [
        _canonical_scope_fact_to_dto(
            fact,
            composition_readiness=readiness,
            canonical_coverage=canonical_coverage,
        )
        for fact in paged
    ]

    return ReviewScopeListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=(offset + page_size) < total,
    )


@router.get(
    "/{trade_date}/scopes/{scope_type}/{scope_key}",
    response_model=ReviewScopeCompositionDetailResponse,
)
async def get_review_scope_composition(
    trade_date: str,
    scope_type: str,
    scope_key: str,
    include_partial: bool = Query(False, description="admin 调试用"),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewScopeCompositionDetailResponse:
    """市场扫描 - 单个范围完整 Canonical Composition。

    [REVIEW-BACKEND-FINAL-CLOSURE Phase 4] 返回单个 scope 的完整 Composition
    （Dynamics / Internal Structure / Leadership / Member Attribution / Objective
    Observation），数据来自 ReviewScopeCompositionSnapshot 薄表（grain =
    review_run_id + scope_type + scope_key）。按 published ReviewRun 的
    review_run_id 查询，避免同日双 run lineage 污染。

    端点路径与 scope list 路由分离，``{scope_type}`` 动态段不会捕获其它子路径。
    """
    if include_partial and not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="include_partial 仅管理员可用",
        )

    td = _parse_date_or_422(trade_date)
    run = await _get_published_run(db, td, include_partial=include_partial)

    snapshot = await get_scope_composition_snapshot(
        db,
        review_run_id=run.id,
        scope_type=scope_type,
        scope_key=scope_key,
    )
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到 Composition: scope_type={scope_type} scope_key={scope_key}",
        )

    # observation 与 fact 共享（fact 存客观事实，composition 存完整六键）
    # 必须显式传入 trade_date（run lineage grain = review_run_id + trade_date +
    # scope_type + scope_key），不退回 global scan。
    fact = await get_scope_observation_fact_by_run(
        db,
        run.id,
        td,
        scope_type,
        scope_key,
    )

    return ReviewScopeCompositionDetailResponse(
        reviewRunId=str(run.id),
        tradeDate=run.trade_date.isoformat(),
        scopeType=snapshot.scope_type,
        scopeKey=snapshot.scope_key,
        scopeName=(fact.scope_name if fact is not None else None),
        algorithmVersion=snapshot.algorithm_version,
        composition=snapshot.composition_payload,
        observation=(fact.observation_payload if fact is not None else None),
    )


# =============================================================================
# 12.3 信号 / 12.4 归因与个股 / 12.5 追踪 - 已退休
# (REVIEW-BACKEND-FINAL-CLOSURE Phase 5)
# Legacy Signal / Discovery / Tracking 可达 API 路径已物理删除，返回 404
# （非 410 deprecated）。底层 review_signal_service / review_discovery_service /
# review_tracking_service 均已物理删除。历史 ORM 表（MarketReviewSignal 等）保留不 DROP。
# =============================================================================





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
