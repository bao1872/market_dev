"""DSA 历史回补 API 路由。

端点：
- POST /admin/strategies/dsa_selector/backfill: 创建并异步执行回补任务
- GET /admin/dsa-backfills/{backfill_job_id}: 查询任务摘要
- GET /admin/dsa-backfills/{backfill_job_id}/instruments: 查询单只股票进度
- GET /admin/dsa-backfills/{backfill_job_id}/date-runs: 查询每个交易日的 StrategyRun
- POST /admin/dsa-backfills/{backfill_job_id}/retry-failed: 重试失败股票
- POST /admin/dsa-backfills/{backfill_job_id}/cancel: 取消任务

说明：
- 所有端点均需 admin 角色
- 创建任务后立即返回 queued 状态，后台 asyncio task 执行实际回补
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import asc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.db import AsyncSessionLocal
from app.models.dsa_backfill import BackfillInstrumentProgress, DSABackfillJob
from app.models.instrument import Instrument
from app.models.strategy_run import StrategyRun
from app.models.user import User
from app.schemas.dsa_backfill import (
    CreateDSABackfillRequest,
    DSABackfillCancelResponse,
    DSABackfillDateRunListResponse,
    DSABackfillDateRunResponse,
    DSABackfillInstrumentProgressListResponse,
    DSABackfillInstrumentProgressResponse,
    DSABackfillJobResponse,
    DSABackfillRetryResponse,
    DSABackfillSummaryResponse,
)
from app.services.dsa_backfill_service import DSABackfillService

logger = logging.getLogger("api.dsa_backfill")

router = APIRouter(tags=["dsa-backfill"])

# 默认并发数
_DEFAULT_MAX_WORKERS = 4


async def _run_backfill_task(job_id: uuid.UUID, max_workers: int) -> None:
    """后台执行回补任务。

    使用独立 Session，异常仅记录日志，不影响 HTTP 响应。
    """
    service = DSABackfillService(max_workers=max_workers)
    try:
        async with AsyncSessionLocal() as db:
            await service.execute_backfill(db, job_id)
            await db.commit()
    except Exception as exc:
        logger.exception("后台执行 DSA backfill 失败 job_id=%s: %s", job_id, exc)


@router.post(
    "/admin/strategies/dsa_selector/backfill",
    response_model=DSABackfillJobResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_dsa_backfill(
    request: CreateDSABackfillRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles("admin")),
) -> DSABackfillJobResponse:
    """创建 DSA 历史回补任务并异步执行。

    仅创建任务记录并返回 queued 状态，实际回补在后台 asyncio task 中执行，
    避免 HTTP 长连接阻塞。
    """
    service = DSABackfillService(max_workers=request.max_workers)
    try:
        job = await service.create_backfill(
            db,
            strategy_key="dsa_selector",
            start_date=request.start_date,
            end_date=request.end_date,
            skip_published=request.skip_published,
            auto_publish=request.auto_publish,
            requested_by=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建回补任务失败: {exc}",
        ) from exc

    await db.commit()

    # 启动后台任务执行回补
    asyncio.create_task(_run_backfill_task(job.id, request.max_workers))

    return DSABackfillJobResponse(
        backfill_job_id=job.id,
        status=job.status,
        target_trade_dates=len(job.target_trade_dates),
        total_stocks=job.total_stocks,
        start_date=job.start_date,
        end_date=job.end_date,
        auto_publish=request.auto_publish,
        created_at=job.created_at,
    )


async def _get_job_or_404(db: AsyncSession, job_id: uuid.UUID) -> DSABackfillJob:
    """获取任务，不存在则 404。"""
    job = await db.get(DSABackfillJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"回补任务不存在: {job_id}",
        )
    return job


@router.get(
    "/admin/dsa-backfills/{backfill_job_id}",
    response_model=DSABackfillSummaryResponse,
)
async def get_backfill_summary(
    backfill_job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_roles("admin")),
) -> DSABackfillSummaryResponse:
    """查询回补任务摘要。"""
    job = await _get_job_or_404(db, backfill_job_id)
    return DSABackfillSummaryResponse(
        backfill_job_id=job.id,
        status=job.status,
        strategy_version_id=job.strategy_version_id,
        start_date=job.start_date,
        end_date=job.end_date,
        target_trade_dates=len(job.target_trade_dates),
        target_trade_dates_list=list(job.target_trade_dates),
        total_stocks=job.total_stocks,
        processed_stocks=job.processed_stocks,
        succeeded_stocks=job.succeeded_stocks,
        failed_stocks=job.failed_stocks,
        selected_result_count=job.selected_result_count,
        current_instrument_id=job.current_instrument_id,
        error_summary=job.error_summary,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
    )


@router.get(
    "/admin/dsa-backfills/{backfill_job_id}/instruments",
    response_model=DSABackfillInstrumentProgressListResponse,
)
async def list_backfill_instruments(
    backfill_job_id: uuid.UUID,
    status_filter: str | None = Query(None, alias="status", description="状态过滤"),
    limit: int = Query(50, ge=1, le=500, description="返回上限"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_roles("admin")),
) -> DSABackfillInstrumentProgressListResponse:
    """查询回补任务下单只股票进度（支持分页和状态过滤）。"""
    await _get_job_or_404(db, backfill_job_id)

    base_stmt = (
        select(
            BackfillInstrumentProgress,
            Instrument.symbol,
        )
        .join(
            Instrument,
            BackfillInstrumentProgress.instrument_id == Instrument.id,
        )
        .where(BackfillInstrumentProgress.backfill_job_id == backfill_job_id)
    )
    if status_filter:
        base_stmt = base_stmt.where(BackfillInstrumentProgress.status == status_filter)

    # 总数
    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total = int((await db.execute(count_stmt)).scalar() or 0)

    # 分页
    rows_stmt = (
        base_stmt.order_by(asc(BackfillInstrumentProgress.instrument_id))
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(rows_stmt)

    items: list[DSABackfillInstrumentProgressResponse] = []
    for progress, symbol in result.all():
        items.append(
            DSABackfillInstrumentProgressResponse(
                instrument_id=progress.instrument_id,
                symbol=symbol or "",
                status=progress.status,
                attempt_count=progress.attempt_count,
                result_count=progress.result_count,
                error_code=progress.error_code,
                error_message=progress.error_message,
                started_at=progress.started_at,
                finished_at=progress.finished_at,
            )
        )

    return DSABackfillInstrumentProgressListResponse(items=items, total=total)


@router.get(
    "/admin/dsa-backfills/{backfill_job_id}/date-runs",
    response_model=DSABackfillDateRunListResponse,
)
async def list_backfill_date_runs(
    backfill_job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_roles("admin")),
) -> DSABackfillDateRunListResponse:
    """查询回补任务下每个交易日对应的 StrategyRun。"""
    await _get_job_or_404(db, backfill_job_id)

    stmt = (
        select(StrategyRun)
        .where(StrategyRun.run_type == "backfill")
        .where(StrategyRun.input_overrides["backfill_job_id"].astext == str(backfill_job_id))
        .order_by(StrategyRun.trade_date)
    )
    result = await db.execute(stmt)
    runs = list(result.scalars().all())

    items = [
        DSABackfillDateRunResponse(
            run_id=run.id,
            trade_date=run.trade_date,
            status=run.status,
            total_instruments=run.total_instruments,
            succeeded_count=run.succeeded_count,
            failed_count=run.failed_count,
            skipped_count=run.skipped_count,
            published_at=run.published_at,
        )
        for run in runs
    ]
    return DSABackfillDateRunListResponse(items=items, total=len(items))


@router.post(
    "/admin/dsa-backfills/{backfill_job_id}/retry-failed",
    response_model=DSABackfillRetryResponse,
)
async def retry_failed_backfill_instruments(
    backfill_job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_roles("admin")),
) -> DSABackfillRetryResponse:
    """将失败股票重置为 pending，等待后台重新执行。"""
    await _get_job_or_404(db, backfill_job_id)
    service = DSABackfillService()
    try:
        retried = await service.retry_failed(db, backfill_job_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    await db.commit()

    # 重新触发后台执行
    asyncio.create_task(_run_backfill_task(backfill_job_id, _DEFAULT_MAX_WORKERS))

    job = await db.get(DSABackfillJob, backfill_job_id)
    return DSABackfillRetryResponse(
        backfill_job_id=backfill_job_id,
        retried_count=retried,
        status=job.status if job else "unknown",
    )


@router.post(
    "/admin/dsa-backfills/{backfill_job_id}/cancel",
    response_model=DSABackfillCancelResponse,
)
async def cancel_dsa_backfill(
    backfill_job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_roles("admin")),
) -> DSABackfillCancelResponse:
    """取消回补任务。"""
    await _get_job_or_404(db, backfill_job_id)
    service = DSABackfillService()
    try:
        job = await service.cancel_backfill(db, backfill_job_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    await db.commit()
    return DSABackfillCancelResponse(
        backfill_job_id=job.id,
        status=job.status,
    )


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]  # type: ignore[attr-defined]
    print(f"router.routes={paths}")
    assert "/admin/strategies/dsa_selector/backfill" in paths
    assert any("/admin/dsa-backfills/{backfill_job_id}" in p for p in paths)
    print("OK")
