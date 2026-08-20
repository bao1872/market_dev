"""复盘模块管理员 API 路由（PRD §12.6）。

端点：
- POST /v1/admin/review/runs: 创建 review run（并执行计算）
- POST /v1/admin/review/runs/{id}/resume: 重启 run（处理 pending/failed）
- POST /v1/admin/review/runs/{id}/publish: 发布 run
- GET  /v1/admin/review/runs/{id}/status: 查询 run 状态（含 items + 发布门禁）

权限（PRD §3.2）：
- 所有端点需要 admin 身份（require_admin）

设计要点：
- 所有写操作要求 idempotency_key（PRD §12.6）
- 失败返回 4xx/5xx，错误信息含原因
- 不自动发布；publish 需要单独调用

遗留（REVIEW-BACKEND-FINAL-CLOSURE Phase 5 已退休）：
- 历史 bootstrap 可达 API 路径已物理删除（/v1/admin/review/bootstrap* 返回 404）。
- 底层 review_bootstrap_job_service / review_bootstrap_service 已删除。SchedulerJobRun 槽位保留不 DROP。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.models.market_review import MarketReviewRun, MarketReviewRunItem
from app.schemas.review import (
    ReviewChipCoverageDTO,
    ReviewRunCreateRequest,
    ReviewRunPublishRequest,
    ReviewRunResponse,
    ReviewRunResumeRequest,
    ReviewRunStatusItemDTO,
    ReviewRunStatusResponse,
)
from app.services.access_control_service import AccessContext, require_admin
from app.services.review_orchestrator_service import (
    DEFAULT_BASELINE_WINDOW,
    REVIEW_ALGORITHM_VERSION,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_CREATED,
    RUN_STATUS_PUBLISHED,
    RUN_STATUS_SIGNALS_READY,
    ReviewOrchestratorError,
    compute_run,
    create_run_with_result,
    get_run,
    get_run_status,
    resume_run,
)
from app.services.review_publication_service import (
    ReviewPublishBlockError,
    publish_review,
)

logger = logging.getLogger("api.admin_review")

router = APIRouter(prefix="/v1/admin/review", tags=["admin-review"])


def _extract_chip_coverage(run: Any) -> ReviewChipCoverageDTO | None:
    """从 run.metadata_json 提取 chip 覆盖率明细（历史 run 兼容读取）。

    [AUD-04/05 2026-08-07] Review 已与 chip 解耦：create_run 不再写入
    chip_coverage，新建 run 此处恒返回 None。保留本函数仅为兼容解耦前
    已落库的历史 run。
    """
    metadata = run.metadata_json if not isinstance(run, dict) else run.get("metadata_json")
    raw = (metadata or {}).get("chip_coverage") if isinstance(metadata, dict) else None
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


def _run_to_response(run: Any) -> ReviewRunResponse:
    """run ORM / dict → ReviewRunResponse。"""
    if isinstance(run, dict):
        return ReviewRunResponse(**run)
    return ReviewRunResponse(
        id=str(run.id),
        trade_date=run.trade_date.isoformat(),
        source_core_run_id=str(run.source_core_run_id),
        source_board_run_id=str(run.source_board_run_id),
        # [QM-63] chip 依赖溯源：None 明确表示 core-only 降级
        source_chip_run_id=(
            str(run.source_chip_run_id) if run.source_chip_run_id else None
        ),
        # [P0 2026-08-04] chip 真实覆盖率明细（以 expected_count 为分母）
        chip_coverage=_extract_chip_coverage(run),
        degraded_reasons=list(run.degraded_reasons or []),
        algorithm_version=run.algorithm_version,
        filter_version=run.filter_version,
        baseline_window=run.baseline_window,
        status=run.status,
        expected_scope_count=run.expected_scope_count,
        succeeded_scope_count=run.succeeded_scope_count,
        failed_scope_count=run.failed_scope_count,
        signal_count=run.signal_count,
        coverage_ratio=float(run.coverage_ratio) if run.coverage_ratio else None,
        started_at=run.started_at.isoformat() if run.started_at else None,
        completed_at=run.completed_at.isoformat() if run.completed_at else None,
        published_at=run.published_at.isoformat() if run.published_at else None,
        metadata=run.metadata_json or {},
        created_at=run.created_at.isoformat() if run.created_at else "",
        updated_at=run.updated_at.isoformat() if run.updated_at else "",
    )


@router.post(
    "/runs",
    response_model=ReviewRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_review_run(
    payload: ReviewRunCreateRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_admin),
) -> ReviewRunResponse:
    """[Admin] 创建 review run 并执行计算。

    流程：
    1. create_run（幂等：相同 trade_date+source_runs+版本复用）
    2. compute_run（编排完整 pipeline：metrics → signals → attribution → tracking）
    3. 返回 run 状态（不自动发布，需调用 publish 接口）

    dry_run=True 时只校验输入，不执行计算。
    """
    _ = ctx
    from datetime import date as date_cls

    try:
        trade_date = date_cls.fromisoformat(payload.trade_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"trade_date 格式错误（需 YYYY-MM-DD）: {payload.trade_date}",
        ) from exc

    # 解析可选 source_run_ids
    source_core_run_id = (
        uuid.UUID(payload.source_core_run_id) if payload.source_core_run_id else None
    )
    source_board_run_id = (
        uuid.UUID(payload.source_board_run_id) if payload.source_board_run_id else None
    )

    try:
        # 1. 创建或复用 run —— ReviewRunCreation(run, created) 显式暴露 created 语义
        creation = await create_run_with_result(
            db,
            trade_date=trade_date,
            source_core_run_id=source_core_run_id,
            source_board_run_id=source_board_run_id,
            algorithm_version=payload.algorithm_version,
            filter_version=payload.filter_version,
            baseline_window=payload.baseline_window,
            canary=payload.canary,
            symbols=payload.symbols,
            dry_run=payload.dry_run,
            idempotency_key=payload.idempotency_key,
        )
        run = creation.run
        created = creation.created

        if payload.dry_run:
            # dry-run：不写 DB，返回非持久化的 run 概要
            await db.rollback()
            return _run_to_response(run)

        # [Phase4.2 corrective] Admin Review reuse 状态机（显式 created/reused 语义）：
        # - created=True（本次新插入）：compute_run（canary/symbols 按请求）
        # - created=False 且 status==created（既有 created）：compute_run
        # - created=False 且 status in (computing/partial/failed)：
        #       resume_run(only_pending=True) —— 只处理 pending/可重试 failed，
        #       不重算已 succeeded item（禁止 only_pending=False 整段重算）
        # - created=False 且 status==signals_ready：原样返回（已就绪，不 recompute）
        # - created=False 且 status==published：原样返回 immutable（禁止原地重算）
        # - created=False 且 status==cancelled：409（已取消，不可复用）
        if created or run.status == RUN_STATUS_CREATED:
            compute_result = await compute_run(
                db,
                run,
                canary=payload.canary,
                symbols=payload.symbols,
            )
            await db.commit()
            await db.refresh(run)
            logger.info(
                "[Admin] review run 新建/created 并计算完成: run_id=%s created=%s result=%s",
                run.id, created, compute_result,
            )
        elif run.status == RUN_STATUS_PUBLISHED:
            # 已发布 run 不可变：不重算、不 commit 变更，原样返回
            await db.rollback()
            compute_result = {
                "reused": True,
                "immutable": True,
                "status": run.status,
                "message": "run 已发布，禁止原地重算，返回既有结果",
            }
            logger.info("[Admin] review run 复用已发布 run（不重算）: run_id=%s", run.id)
        elif run.status == RUN_STATUS_SIGNALS_READY:
            # 已就绪：原样返回，不 recompute
            await db.rollback()
            compute_result = {
                "reused": True,
                "immutable": True,
                "status": run.status,
                "message": "run 已 signals_ready，禁止重算，返回既有结果",
            }
            logger.info("[Admin] review run 复用 signals_ready run（不重算）: run_id=%s", run.id)
        elif run.status == RUN_STATUS_CANCELLED:
            # 已取消：不可复用，明确 409
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"run 已取消(cancelled)，不可复用或重算: run_id={run.id}",
            )
        else:
            # 未发布的既有非终态 run（computing/partial/failed）：
            # 以 resume 安全续跑（only_pending=True），而非整段重算
            compute_result = await resume_run(
                db, run, only_pending=True,
            )
            await db.commit()
            await db.refresh(run)
            logger.info(
                "[Admin] review run 复用未发布 run（resume only_pending=True）: "
                "run_id=%s status=%s result=%s",
                run.id, run.status, compute_result,
            )
    except HTTPException:
        # 已构造好的 HTTP 异常（如 cancelled → 409）必须原样向上传递，
        # 不得被下方通用 except Exception 二次包装成 500。
        await db.rollback()
        raise
    except ReviewOrchestratorError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception("[Admin] 创建 review run 失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建 review run 失败: {exc}",
        ) from exc

    return _run_to_response(run)


@router.post(
    "/runs/{run_id}/resume",
    response_model=ReviewRunResponse,
)
async def resume_review_run(
    run_id: uuid.UUID,
    payload: ReviewRunResumeRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_admin),
) -> ReviewRunResponse:
    """[Admin] 重启 run（处理 pending / 可重试 failed / 过期 running）。"""
    _ = ctx
    run = await get_run(db, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"run 不存在: run_id={run_id}",
        )

    try:
        result = await resume_run(db, run, only_pending=payload.only_pending)
        await db.commit()
        await db.refresh(run)
        logger.info("[Admin] review run resume 完成: run_id=%s result=%s", run_id, result)
    except ReviewOrchestratorError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception("[Admin] resume review run 失败: run_id=%s", run_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"resume 失败: {exc}",
        ) from exc

    return _run_to_response(run)


@router.post(
    "/runs/{run_id}/publish",
    response_model=ReviewRunResponse,
)
async def publish_review_run(
    run_id: uuid.UUID,
    payload: ReviewRunPublishRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_admin),
) -> ReviewRunResponse:
    """[Admin] 发布 review run（写入 factor_publications 正式指针）。

    [P0 安全收口 2026-08-01] force=True 语义变更：
    - 不再写正式 pointer，只生成 provisional 标记（run metadata 可审计）；
    - provisional run 不对普通用户可见，仅 admin 可通过 include_partial
      或显式 run_id 查看；
    - 撤销错误正式 pointer 使用 withdraw_review_publication（CLI），
      禁止用 force 覆盖。
    """
    run = await get_run(db, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"run 不存在: run_id={run_id}",
        )

    try:
        publication = await publish_review(
            db, run,
            force=payload.force,
            operator=ctx.user_id,
            idempotency_key=payload.idempotency_key,
        )
        await db.commit()
        await db.refresh(run)
        if publication is not None:
            logger.info(
                "[Admin] review run 发布成功: run_id=%s publication_id=%s",
                run_id, publication.id,
            )
        else:
            logger.warning(
                "[Admin] review run force=provisional（未写正式 pointer）: "
                "run_id=%s operator=%s idempotency_key=%s",
                run_id, ctx.user_id, payload.idempotency_key,
            )
    except ReviewPublishBlockError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"发布门禁失败: {'; '.join(exc.blockers)}",
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception("[Admin] publish review run 失败: run_id=%s", run_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"publish 失败: {exc}",
        ) from exc

    return _run_to_response(run)


@router.get(
    "/runs/{run_id}/status",
    response_model=ReviewRunStatusResponse,
)
async def get_review_run_status(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_admin),
) -> ReviewRunStatusResponse:
    """[Admin] 查询 run 状态（含 items + 发布门禁）。"""
    _ = ctx
    run = await get_run(db, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"run 不存在: run_id={run_id}",
        )

    status_data = await get_run_status(db, run)
    return ReviewRunStatusResponse(
        run=_run_to_response(run),
        items=[
            ReviewRunStatusItemDTO(**item) for item in status_data["items"]
        ],
        publishable=status_data["publishable"],
        publish_blockers=status_data["publish_blockers"],
    )


# =============================================================================
# Review 历史 bootstrap - 已退休（REVIEW-BACKEND-FINAL-CLOSURE Phase 5）
# bootstrap 可达代码路径已物理删除（review_bootstrap_job_service 删除），
# admin 端点不再存在（返回 404，非 202/410 deprecated）。SchedulerJobRun 槽位保留不 DROP。
# =============================================================================


class ReviewRunTimelineResponse(BaseModel):
    """review run 执行时间线摘要响应。

    用于中断恢复诊断：聚合 market_review_runs + market_review_run_items，
    返回最后心跳、当前 phase、item 计数、恢复次数、中断原因码。
    """

    run_id: str
    trade_date: str
    status: str
    last_heartbeat: str | None
    current_phase: str | None
    counts: dict[str, int]
    recovery_count: int
    interruption_reason_code: str | None


@router.get(
    "/runs/{run_id}/timeline",
    response_model=ReviewRunTimelineResponse,
)
async def get_review_run_timeline(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_admin),
) -> ReviewRunTimelineResponse:
    """[Admin] 查询 review run 执行时间线摘要（用于中断恢复诊断）。

    从 market_review_runs + market_review_run_items 聚合：
    - last_heartbeat: run 最后更新时间（market_review_runs.updated_at）
    - current_phase: 当前活跃 phase（优先取 status=running 的 phase，
      否则取最近 updated_at 的 phase）
    - counts: {completed, failed, pending} item 状态计数
      （completed=succeeded, pending=pending+running）
    - recovery_count: max(attempt_count) 聚合
    - interruption_reason_code: 从 run.metadata_json 中读取（如有）

    Raises:
        HTTPException 404: run 不存在
    """
    _ = ctx

    # 查询 run（不存在返回 404）
    run = await db.get(MarketReviewRun, run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"run 不存在: run_id={run_id}",
        )

    # 聚合 run_items: status 计数 + max(attempt_count)
    agg_stmt = (
        select(
            func.count()
                .filter(MarketReviewRunItem.status == "succeeded")
                .label("completed"),
            func.count()
                .filter(MarketReviewRunItem.status == "failed")
                .label("failed"),
            func.count()
                .filter(MarketReviewRunItem.status.in_(("pending", "running")))
                .label("pending"),
            func.max(MarketReviewRunItem.attempt_count).label("max_attempt"),
        )
        .where(MarketReviewRunItem.review_run_id == run_id)
    )
    agg_result = await db.execute(agg_stmt)
    agg_row = agg_result.one()

    # current_phase: 优先取 running 状态的 phase（按 updated_at 倒序）
    phase_stmt = (
        select(MarketReviewRunItem.phase)
        .where(
            MarketReviewRunItem.review_run_id == run_id,
            MarketReviewRunItem.status == "running",
        )
        .order_by(MarketReviewRunItem.updated_at.desc())
        .limit(1)
    )
    phase_result = await db.execute(phase_stmt)
    current_phase = phase_result.scalar_one_or_none()

    if current_phase is None:
        # 无 running item，取最近 updated_at 的 phase（任何状态）
        recent_phase_stmt = (
            select(MarketReviewRunItem.phase)
            .where(MarketReviewRunItem.review_run_id == run_id)
            .order_by(MarketReviewRunItem.updated_at.desc())
            .limit(1)
        )
        recent_phase_result = await db.execute(recent_phase_stmt)
        current_phase = recent_phase_result.scalar_one_or_none()

    # interruption_reason_code: 从 metadata_json 中读取
    meta = run.metadata_json or {}
    interruption_reason_code = meta.get("interruption_reason_code")

    return ReviewRunTimelineResponse(
        run_id=str(run.id),
        trade_date=run.trade_date.isoformat(),
        status=run.status,
        last_heartbeat=(
            run.updated_at.isoformat() if run.updated_at else None
        ),
        current_phase=current_phase,
        counts={
            "completed": agg_row.completed or 0,
            "failed": agg_row.failed or 0,
            "pending": agg_row.pending or 0,
        },
        recovery_count=agg_row.max_attempt or 0,
        interruption_reason_code=interruption_reason_code,
    )


if __name__ == "__main__":
    paths = [r.path for r in router.routes if isinstance(r, APIRoute)]
    print(f"router prefix: {router.prefix}")
    print(f"端点数: {len(paths)}")
    for p in paths:
        print(f"  {p}")
    # 验证常量导入
    print(f"REVIEW_ALGORITHM_VERSION = {REVIEW_ALGORITHM_VERSION}")
    print(f"DEFAULT_BASELINE_WINDOW = {DEFAULT_BASELINE_WINDOW}")

    # [REVIEW-BACKEND-FINAL-CLOSURE Phase 5] bootstrap 端点已退休（物理删除，
    # 返回 404），此处不再断言其存在。保留 admin review run 端点验证。
