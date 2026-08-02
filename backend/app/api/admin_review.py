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
    ReviewOrchestratorError,
    compute_run,
    create_run,
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


def _run_to_response(run: Any) -> ReviewRunResponse:
    """run ORM / dict → ReviewRunResponse。"""
    if isinstance(run, dict):
        return ReviewRunResponse(**run)
    return ReviewRunResponse(
        id=str(run.id),
        trade_date=run.trade_date.isoformat(),
        source_core_run_id=str(run.source_core_run_id),
        source_board_run_id=str(run.source_board_run_id),
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
        # 1. 创建或复用 run
        run = await create_run(
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

        if payload.dry_run:
            # dry-run：不写 DB，返回非持久化的 run 概要
            await db.rollback()
            return _run_to_response(run)

        await db.commit()
        await db.refresh(run)

        # 2. 执行计算
        compute_result = await compute_run(
            db,
            run,
            canary=payload.canary,
            symbols=payload.symbols,
        )
        await db.commit()
        await db.refresh(run)

        logger.info(
            "[Admin] review run 计算完成: run_id=%s result=%s",
            run.id, compute_result,
        )
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
