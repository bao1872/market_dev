"""复盘模块用户端 API 路由（PRD §12.1-12.5）。

端点：
- GET  /v1/review/dates: 已发布复盘交易日列表
- GET  /v1/review/latest: 最新已发布复盘 run 信息
- GET  /v1/review/{trade_date}/overview: 复盘总览（覆盖率）
- GET  /v1/review/{trade_date}/scopes: 市场扫描（canonical Scope 薄列表：Fact + Composition 投影）
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
import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.domain.review.member_fact import (
    collect_composition_member_ids,
    is_uuid,
)
from app.domain.review.observation_groups import build_l2_observation_groups
from app.models.instrument import Instrument
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
    ReviewScopeCompareFactsDTO,
    ReviewScopeCompositionDetailResponse,
    ReviewScopeListResponse,
    ReviewScopeObservationSummaryDTO,
    ReviewScopeSummaryDTO,
)
from app.services.access_control_service import (
    AccessContext,
    require_admin,
    require_capability,
)
from app.services.review_cross_sectional_service import get_cross_sectional
from app.services.review_observation_persistence_service import (
    ReviewScopeSummaryRow,
    get_scope_composition_snapshot,
    get_scope_observation_fact_by_run,
    list_review_scope_summaries_by_run,
    list_scope_observation_facts,
)
from app.services.review_publication_service import (
    get_published_review_run_id,
    is_formally_published_review_run,
    list_formally_published_review_dates,
    list_published_review_dates,
)
from app.services.review_scope_diagnostics_service import get_scope_diagnostics
from app.services.review_scope_explorer_service import list_review_scope_compare

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
        HTTPException 404: 无 live pointer（无已发布复盘）
        HTTPException 500: live pointer 指向缺失 run，或该 run 不满足
            FORMAL_REVIEW_READ_OWNER（status != published / published_at IS NULL /
            run.trade_date != trade_date）—— data-integrity fail-closed
    """
    run_id = await get_published_review_run_id(session, trade_date)
    if run_id is not None:
        run = await session.get(MarketReviewRun, run_id)
        if run is not None:
            # §4 formal publication guard（用户正式路径）：live pointer 指向的 run
            # 必须已正式发布（status=published 且 published_at IS NOT NULL）。
            # [C1 FINAL-IDENTITY §4/§5] guard 由 is_formally_published_review_run
            # **单一拥有**（status + published_at + pointer identity + trade_date），
            # 此处不另设第二套判定；下方 detail 只做精确诊断，不改变 gate 语义。
            if include_partial or is_formally_published_review_run(
                run, run_id, expected_trade_date=trade_date,
            ):
                return run
            # §4 case C: live pointer 指向 run，但不满足 FORMAL_REVIEW_READ_OWNER
            # （status!=published / published_at IS NULL / run.trade_date != T）
            # → data-integrity fail-closed，不得作为正式 Review 返回。
            # 禁止 404 / fallback / 回退 latest / 跳过到其它日期。
            mismatch = ""
            if run.trade_date != trade_date:
                mismatch = (
                    f"pointer trade_date={trade_date} 与 ReviewRun trade_date="
                    f"{run.trade_date} 不一致（cross-date pointer）；"
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"{mismatch}"
                    f"trade_date={trade_date} 的正式 Review pointer 指向 run={run_id}，"
                    f"但该 run 不满足正式发布合同（status={run.status}, "
                    f"published_at={run.published_at}, run.trade_date={run.trade_date}），"
                    "数据一致性异常，拒绝作为正式 Review 返回"
                ),
            )
        # §4 case B: live pointer 指向不存在的 run
        # → data-integrity fail-closed，绝不回退 latest run。
        if not include_partial:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"trade_date={trade_date} 的正式 Review pointer 指向不存在的 run={run_id}，"
                    "数据一致性异常，拒绝回退到 latest run"
                ),
            )

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

    与 _get_published_run 的区别是本函数对“无已发布 run”返回 None 而非抛异常。

    [PHASE C1 FINAL §7] P2 DEFERRED（记录，不在本轮修改）：
    - 本函数当前**无任何 production caller**（legacy Discovery 服务与用户端点
      已随 REVIEW-BACKEND-FINAL-CLOSURE Phase 5 退休）。全仓检索只剩本定义处，
      因此不存在“用户正式 caller 吞掉 500”的实际风险，本轮不做清理性删除
      （避免为清洁代码扩大范围）。
    - 潜在缺陷（若未来被复用必须先行修复）：``except HTTPException: return None``
      会把 `_get_published_run` 抛出的 **500 data-integrity error（§4 case B/C：
      live pointer 指向缺失 run / 未正式发布 run）一并吞成 None**。用户正式
      caller 复用本函数时，必须改为只把真正的 **404 no publication** 转为 None，
      500 必须继续向上抛。
    """
    try:
        return await _get_published_run(session, trade_date)
    except HTTPException:
        return None


def _summary_row_to_dto(
    row: ReviewScopeSummaryRow,
    *,
    composition_readiness: dict[str, str] | None = None,
    canonical_coverage: dict[str, dict[str, Any]] | None = None,
    compare_facts: dict[str, Any] | None = None,
) -> ReviewCanonicalScopeResponse:
    """[REVIEW-CANONICAL-SLICE-B] projected summary row → canonical list DTO。

    readiness 优先取 run metadata 的 canonical composition readiness（唯一发布
    判断依据），缺失时回落 fact.readiness；coverage 取 metadata canonical_coverage
    的 provided/eligible，缺失时用 fact 列派生。summary 仅当 Composition 存在
    （LEFT JOIN 命中）时投影，否则 ``None``（partial / missing composition）。
    所有 analysis 字段原样透传 Optional，不把 unavailable 转 0。
    """
    coverage = (canonical_coverage or {}).get(row.scope_key) or {}
    eligible = int(coverage.get("eligible", row.pit_member_count) or 0)
    provided = int(coverage.get("provided", row.provided_member_count) or 0)
    readiness = (composition_readiness or {}).get(row.scope_key, row.fact_readiness)
    summary: ReviewScopeSummaryDTO | None = None
    if row.composition_present:
        summary = ReviewScopeSummaryDTO(
            dynamicsStatus=row.dynamics_status,
            phase=row.phase,
            position=row.position,
            velocity=row.velocity,
            acceleration=row.acceleration,
            upperOccupancy=row.upper_occupancy,
            lowerOccupancy=row.lower_occupancy,
            equalWeightReturn=row.equal_weight_return,
            amountWeightedReturn=row.amount_weighted_return,
            capitalTilt=row.capital_tilt,
            advanceRatio=row.advance_ratio,
            declineRatio=row.decline_ratio,
            unchangedRatio=row.unchanged_ratio,
            returnDispersion=row.return_dispersion,
            priceNormalizedHhi=row.price_normalized_hhi,
            amountNormalizedHhi=row.amount_normalized_hhi,
            leadershipStatus=row.leadership_status,
            jaccardStability=row.jaccard_stability,
            migration=row.migration,
        )
    # R2B Observation Fact thin projection — SEPARATE owner from summary.
    # Not gated on composition_present; Fact-derived scalars pass through
    # verbatim (None stays None, 0 stays 0 — no `or 0` coercion).
    observation_summary = ReviewScopeObservationSummaryDTO(
        freshnessTodayCount=row.freshness_today_count,
        freshnessDecayWeightedDensity=row.freshness_decay_weighted_density,
        technicalHhi=row.technical_hhi,
        technicalTop5Numerator=row.technical_top5_numerator,
        technicalTop5Denominator=row.technical_top5_denominator,
        technicalLeaderMedianGap=row.technical_leader_median_gap,
        technicalLeaderSymbol=row.technical_leader_symbol,
        technicalMemberCount=row.technical_member_count,
    )
    return ReviewCanonicalScopeResponse(
        scopeType=row.scope_type,
        scopeKey=row.scope_key,
        scopeName=row.scope_name,
        readiness=readiness,
        status=row.pit_status_t,
        eligibleCount=eligible,
        providedCount=provided,
        coverageRatio=float(provided / eligible) if eligible > 0 else None,
        summary=summary,
        observationSummary=observation_summary,
        compareFacts=(
            ReviewScopeCompareFactsDTO(**compare_facts) if compare_facts else None
        ),
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
    """已**正式发布**复盘交易日列表（降序）。

    [PHASE C1 FINAL §4] 名义语义 = "已正式发布 Review 的交易日"，因此不得只验证
    live pointer：pointer 指向缺失 / ``status != published`` / ``published_at IS NULL``
    的 ``MarketReviewRun`` 时，该 T 不得列为正式已发布日期。全部条件由
    ``list_formally_published_review_dates`` 在 DB 层（pointer JOIN run）完成。
    """
    _ = ctx
    dates = await list_formally_published_review_dates(db, limit=200)
    return ReviewDatesResponse(
        trade_dates=[d.isoformat() for d in dates],
        latest_trade_date=dates[0].isoformat() if dates else None,
    )


@router.get("/latest", response_model=ReviewLatestResponse)
async def get_latest_review(
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_capability(REVIEW_CAPABILITY)),
) -> ReviewLatestResponse:
    """最新已**正式发布**复盘 run 基本信息。

    [PHASE C1 FINAL §3] 禁止自行 ``pointer → db.get → return``。本端点与
    overview/scopes/detail 共享同一个 FORMAL_REVIEW_READ_OWNER：

    1. 先用 ``list_published_review_dates``（LIVE POINTER OWNER）取候选最新
       交易日；
    2. 再经 ``_get_published_run``（统一 formal guard）校验
       ``ReviewRun.status == published`` 且 ``published_at IS NOT NULL``。

    因此：broken pointer（pointer → 缺失 run）= fail-closed 500；
    ``status != published`` = fail-closed 500；``published_at IS NULL`` =
    fail-closed 500。**绝不**回退到 latest ReviewRun，也**不**跳过到更早的
    交易日（与 §8 CASE A 一致）。
    """
    _ = ctx
    dates = await list_published_review_dates(db, limit=1)
    if not dates:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="无已发布复盘",
        )
    trade_date = dates[0]
    # 复用统一正式 read-owner guard：404/500 语义与 overview/scopes/detail 完全一致。
    run = await _get_published_run(db, trade_date)
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
        db, review_run_id=run.id, from_date=run.trade_date, to_date=run.trade_date,
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
        # [Slice 3 core-only] nullable Board lineage：DB NULL → JSON null，
        # 历史 UUID → UUID string（禁止 str(None) 序列化为 "None"）
        sourceBoardRunId=(
            str(run.source_board_run_id)
            if run.source_board_run_id is not None
            else None
        ),
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

    # [REVIEW-CANONICAL-SLICE-B] 单一读取 owner：DB 级分页 + JSONB 标量投影
    # （Fact LEFT OUTER JOIN Composition）。不加载完整 composition_payload，
    # 不退回 global trade_date scan，不 per-scope 查询，不做 canonical 重算。
    # scope_type 过滤下推到 SQL（与 count 同条件，保证 total 一致）。
    offset = (page - 1) * page_size
    total, summaries = await list_review_scope_summaries_by_run(
        db,
        review_run_id=run.id,
        trade_date=td,
        scope_type=scope_type,
        offset=offset,
        limit=page_size,
    )

    # [SLICE 5 / Explorer] compare facts：ONE extra batch query（不是 N+1）。
    # 同一 formally published run + 同一 family 过滤；cross-sectional peer percentile
    # 在该查询的结果集上以 canonical math owner 批量计算，绝不 per-scope 调用
    # get_cross_sectional()。
    compare_map: dict[tuple[str, str], dict] = {}
    if summaries:
        compare_map = await list_review_scope_compare(
            db,
            review_run_id=run.id,
            trade_date=td,
            scope_type=scope_type,
            scope_keys={(row.scope_type, row.scope_key) for row in summaries},
        )

    items = [
        _summary_row_to_dto(
            row,
            composition_readiness=readiness,
            canonical_coverage=canonical_coverage,
            compare_facts=compare_map.get((row.scope_type, row.scope_key)),
        )
        for row in summaries
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
    """市场扫描 - 单个范围 Canonical Observation Detail。

    [R3A Canonical Observation Detail Contract] Scope Detail 以 Observation
    Fact 为第一归属（最小存在 owner）；Composition 为可选 enrichment。

    读取顺序（Fact-first，published-run lineage）：
      1. 解析 published ReviewRun；
      2. 用 run-lineage grain（review_run_id + trade_date + scope_type +
         scope_key）查 Fact via ``get_scope_observation_fact_by_run``；
         Fact 缺失 → 404（Fact owns detail existence）；
      3. 再查 Composition（可选）；缺失 → composition=null，仍返回 200；
      4. observationGroups 由已加载 Fact.observation_payload 直接投影
         ``build_l2_observation_groups``，不二次查库、不调 global
         ``get_scope_observation_fact``、不调 review_observation_group_service。

    同日多 run 不污染 published run：用户详情路径严禁 global trade_date scan。
    端点路径与 scope list 路由分离，``{scope_type}`` 动态段不会捕获其它子路径。
    """
    if include_partial and not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="include_partial 仅管理员可用",
        )

    td = _parse_date_or_422(trade_date)
    run = await _get_published_run(db, td, include_partial=include_partial)

    # [R3A] Fact-first：observation 是 detail 存在的最小 owner，必须按
    # published run lineage grain 查（review_run_id + trade_date + scope_type +
    # scope_key），不退回 global get_scope_observation_fact(trade_date, ...)。
    fact = await get_scope_observation_fact_by_run(
        db,
        run.id,
        td,
        scope_type,
        scope_key,
    )
    if fact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到 Observation Fact: scope_type={scope_type} scope_key={scope_key}",
        )

    # [R3A] Composition 为可选 enrichment：缺失不 404，composition=null。
    snapshot = await get_scope_composition_snapshot(
        db,
        review_run_id=run.id,
        scope_type=scope_type,
        scope_key=scope_key,
    )

    # [R3A] observation payload 原样透传；非 dict 视为内部数据完整性错误，
    # 不得静默制造 {} / 全 null 8 组假 observation。
    if not isinstance(fact.observation_payload, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="内部数据完整性错误：Observation Fact.observation_payload 非合法 dict",
        )
    observation = fact.observation_payload

    # [R3A] L2 Observation Groups：由已加载 Fact 直接投影，单一 owner。
    observation_groups = build_l2_observation_groups(observation)

    # [R3A] algorithmVersion 元数据回退（仅 metadata 选择，不业务计算）：
    # snapshot > fact > run。
    if snapshot is not None:
        algorithm_version = snapshot.algorithm_version
    elif fact.algorithm_version is not None:
        algorithm_version = fact.algorithm_version
    else:
        algorithm_version = run.algorithm_version

    # [REVIEW-PRODUCT-CLOSURE-01 Phase C] Member identity directory — ADDITIVE
    # display metadata.  ONE bulk Instrument query for every member id referenced
    # by the Composition (leadership id arrays + attribution member_id); zero
    # N+1.  Missing ids simply stay out of the mapping (frontend falls back to
    # the short/internal id).  Nothing here rewrites the persisted Composition.
    member_directory: dict[str, dict[str, str]] = {}
    # [R3A-BE2] snapshot may be None (composition missing) -> keep empty directory,
    # do NOT touch snapshot.composition_payload. composition stays null, endpoint 200.
    composition_ok = snapshot is not None and isinstance(snapshot.composition_payload, dict)
    # [DSA correction] memberDirectory ref IDs =
    #   Composition 引用成员  UNION  observation.trend.transition.changed_members 成员。
    # 仍是 ONE bulk Instrument query（去重后一次性查）。composition=null 时只要
    # Observation 有 changed-member UUID，目录也必须能生成（不依赖 Composition）。
    # [R3 History] query-time 20D rolling diagnostics from published-run safe series.
    # 提前到 memberDirectory 之前：Price 历史 leader id 需要并入同一次 bulk 查询。
    history = await get_scope_diagnostics(
        db, trade_date=td, scope_type=scope_type, scope_key=scope_key,
    )
    ref_ids: list[str] = []
    if composition_ok:
        ref_ids.extend(
            mid for mid in collect_composition_member_ids(snapshot.composition_payload)
            if is_uuid(mid)
        )
    ref_ids.extend(_collect_changed_member_ids(observation))
    # [SLICE 4 / Price] UNION price history current_leader_ids。
    # 仍是 ONE bulk Instrument query（去重后一次性查），绝不逐个成员发请求。
    ref_ids.extend(_collect_price_history_leader_ids(history))
    ref_ids = list(dict.fromkeys(ref_ids))  # 稳定去重
    if ref_ids:
        id_uuids = [uuid.UUID(mid) for mid in ref_ids]
        inst_rows = (
            await db.execute(
                select(Instrument.id, Instrument.symbol, Instrument.name).where(
                    Instrument.id.in_(id_uuids)
                )
            )
        ).all()
        member_directory = {
            str(inst_id): {"symbol": symbol, "name": name}
            for inst_id, symbol, name in inst_rows
        }

    # [R3 Cross-sectional P0] published-run lineage cross-sectional position evidence.
    cross_section = await get_cross_sectional(db, td, scope_type, scope_key)

    return ReviewScopeCompositionDetailResponse(
        reviewRunId=str(run.id),
        tradeDate=fact.trade_date.isoformat(),
        scopeType=fact.scope_type,
        scopeKey=fact.scope_key,
        scopeName=fact.scope_name,
        algorithmVersion=algorithm_version,
        observation=observation,
        observationGroups=observation_groups,
        composition=(snapshot.composition_payload if snapshot is not None else None),
        memberDirectory=member_directory,
        history=history,
        crossSection=cross_section,
    )


def _collect_price_history_leader_ids(history: Any) -> list[str]:
    """[SLICE 4 / Price] 从 history.price.leadership 收集 current_leader_ids。

    并入 memberDirectory 的同一批 ref IDs —— 仍是 ONE bulk Instrument query
    （去重后一次性查），绝不逐个成员发请求。非 dict / 缺字段 / 非 UUID 一律跳过，
    绝不抛错；history 为 None（非 activated scope_type）时返回空。
    """
    ids: list[str] = []
    # get_scope_diagnostics() 明确返回普通 dict（不是 pydantic 模型）。
    # getattr(dict, "price", None) 恒为 None —— 必须按真实 dict shape 读取，
    # 否则历史 leader id 一个都收不到、前端只能 fallback 到裸 ID。
    if isinstance(history, dict):
        price = history.get("price")
    else:  # 防御：万一上游改为对象，仍保持可用（不抛错）
        price = getattr(history, "price", None)
    if price is None:
        return ids
    leadership = price.get("leadership") if isinstance(price, dict) else getattr(price, "leadership", None)
    if not isinstance(leadership, list):
        return ids
    for item in leadership:
        if not isinstance(item, dict):
            continue
        raw_ids = item.get("current_leader_ids")
        if not isinstance(raw_ids, list):
            continue
        for mid in raw_ids:
            if isinstance(mid, str) and is_uuid(mid):
                ids.append(mid)
    return ids


def _collect_changed_member_ids(observation: Any) -> list[str]:
    """从 canonical Observation 收集 T-1→T 变化成员 ID（用于 memberDirectory 批量解析）。

    ref IDs = Composition leadership/attribution 引用 UNION
    trend.transition.changed_members UNION structure.swing.transition.changed_members
    UNION structure.internal.transition.changed_members（ONE bulk Instrument query，去重）。
    非 dict / 缺字段 / 非 UUID 一律跳过，绝不抛错。
    """
    ids: list[str] = []
    try:
        trend_changed = (
            observation.get("trend", {}).get("transition", {}).get("changed_members", [])
        )
        swing_changed = (
            observation.get("structure", {}).get("swing", {})
            .get("transition", {}).get("changed_members", [])
        )
        internal_changed = (
            observation.get("structure", {}).get("internal", {})
            .get("transition", {}).get("changed_members", [])
        )
    except AttributeError:
        return ids
    for changed in (trend_changed, swing_changed, internal_changed):
        if not isinstance(changed, list):
            continue
        for m in changed:
            if isinstance(m, dict):
                mid = m.get("member_id")
                if isinstance(mid, str) and is_uuid(mid):
                    ids.append(mid)
    return ids


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
