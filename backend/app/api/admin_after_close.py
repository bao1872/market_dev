"""管理员 API 路由 - 盘后编排管理 + 任务事件时间线查询。

端点：
- POST /admin/after-close-runs: 创建并异步执行盘后编排（日线刷新→DSA→质量门禁→发布）
- GET /admin/after-close-runs/{run_id}: 查询盘后编排状态（含事件时间线）
- POST /admin/after-close-runs/{run_id}/retry: 重试失败的盘后编排
- POST /admin/after-close-runs/{run_id}/force: 强制重新执行盘后编排（非 failed 状态也可触发）
- GET /admin/job-runs/{run_id}/events: 查询任意任务的执行事件时间线

权限：
- 所有端点需要 admin 角色（RBAC）
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.schemas.scheduler_job_run import (
    AfterCloseRunCreateResponse,
    AfterCloseRunStatusResponse,
    JobRunEventItem,
    JobRunEventListResponse,
)
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    create_after_close_run,
    execute_after_close_run,
    get_after_close_run_status,
    retry_after_close_run,
)
from app.services.job_run_event_service import list_events

logger = logging.getLogger("admin_after_close")

router = APIRouter(
    prefix="/admin",
    tags=["admin-after-close"],
)


class AfterCloseRunCreateRequest(BaseModel):
    """盘后编排创建请求。"""

    trade_date: str  # YYYY-MM-DD


def _parse_trade_date(trade_date_str: str):
    """[AfterClose] - 解析交易日期字符串为 date 对象。"""
    from datetime import date as date_cls
    try:
        return date_cls.fromisoformat(trade_date_str)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"trade_date 格式错误（需 YYYY-MM-DD）: {trade_date_str}, error={e}",
        ) from e


def _kick_off_async_execution(job_run_id: UUID, trade_date) -> None:
    """[AfterClose] - 后台异步启动盘后编排执行（fire and forget）。

    使用 asyncio.create_task 在当前事件循环中启动，不阻塞 HTTP 响应。
    execute_after_close_run 内部使用独立 AsyncSession，不依赖请求 session。
    """
    task = asyncio.create_task(
        execute_after_close_run(
            job_run_id=job_run_id,
            trade_date=trade_date,
        )
    )
    # 添加回调记录任务异常（不吞没异常，仅记录日志）
    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            logger.info("[AfterClose] 后台执行任务已取消: job_run_id=%s", job_run_id)
            return
        exc = t.exception()
        if exc is not None:
            logger.error(
                "[AfterClose] 后台执行任务异常: job_run_id=%s, error=%s",
                job_run_id, exc,
                exc_info=exc,
            )

    task.add_done_callback(_on_done)


@router.post(
    "/after-close-runs",
    response_model=AfterCloseRunCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_after_close_run_endpoint(
    payload: AfterCloseRunCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AfterCloseRunCreateResponse:
    """创建并异步执行盘后编排。

    流程：
    1. 解析 trade_date
    2. create_after_close_run 创建 SchedulerJobRun（幂等）
    3. 后台异步启动 execute_after_close_run
    4. 立即返回任务 ID（不等待执行完成）

    幂等：同 trade_date 已有 running/succeeded 任务时返回已有任务。

    Args:
        payload: 创建请求（含 trade_date）
        db: 异步数据库会话
        current_user: 当前管理员（由 require_roles 注入）

    Returns:
        创建响应（含 job_run_id 和初始状态）
    """
    trade_date = _parse_trade_date(payload.trade_date)

    job_run, is_new = await create_after_close_run(db=db, trade_date=trade_date)

    # 仅对新创建的任务（status=running 且 orchestrator_status=queued）启动后台执行
    # 已存在的不重复启动
    from app.services.after_close_orchestrator import _parse_metadata
    meta = _parse_metadata(job_run)
    orchestrator_status = meta.get("orchestrator_status")
    if is_new and orchestrator_status == AfterCloseRunStatus.QUEUED.value:
        _kick_off_async_execution(job_run.id, trade_date)

    # [Spec] 已有运行中任务时拒绝重复创建：返回 409 Conflict，body 含已有 after_close_run_id
    if not is_new:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "after_close_run_id": str(job_run.id),
                "status": job_run.status,
                "orchestrator_status": orchestrator_status or "unknown",
                "trade_date": trade_date.isoformat(),
                "message": f"同日已有盘后编排任务: trade_date={trade_date}",
            },
        )

    return AfterCloseRunCreateResponse(
        job_run_id=str(job_run.id),
        status=job_run.status,
        orchestrator_status=orchestrator_status or "unknown",
        trade_date=trade_date.isoformat(),
        message=f"盘后编排已创建并启动: trade_date={trade_date}",
    )


@router.get(
    "/after-close-runs/{run_id}",
    response_model=AfterCloseRunStatusResponse,
)
async def get_after_close_run_endpoint(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AfterCloseRunStatusResponse:
    """查询盘后编排状态（含事件时间线 + DSA run 状态）。

    Args:
        run_id: 编排任务 ID
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        编排状态响应（含 orchestrator_status, dsa_run_status, events）

    Raises:
        HTTPException 404: 任务不存在
        HTTPException 400: 任务非盘后编排
    """
    try:
        result = await get_after_close_run_status(db=db, job_run_id=run_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e

    return AfterCloseRunStatusResponse(
        job_run_id=result["job_run_id"],
        job_name=result["job_name"],
        business_date=result["business_date"],
        status=result["status"],
        orchestrator_status=result["orchestrator_status"],
        trade_date=result["trade_date"],
        dsa_run_id=result["dsa_run_id"],
        dsa_run_status=result["dsa_run_status"],
        started_at=result["started_at"],
        finished_at=result["finished_at"],
        error_message=result["error_message"],
        events=[
            JobRunEventItem(
                id=e["id"],
                job_run_id=run_id,
                step=e["step"],
                level=e["level"],
                message=e["message"],
                payload=e["payload"],
                created_at=e["created_at"],
            )
            for e in result["events"]
        ],
    )


@router.post(
    "/after-close-runs/{run_id}/retry",
    response_model=AfterCloseRunCreateResponse,
)
async def retry_after_close_run_endpoint(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AfterCloseRunCreateResponse:
    """重试失败的盘后编排任务。

    仅 failed 状态的任务可重试。重置为 queued 后重新启动后台执行。

    Args:
        run_id: 编排任务 ID
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        重试响应

    Raises:
        HTTPException 404: 任务不存在
        HTTPException 400: 任务非盘后编排或状态非 failed
    """
    try:
        job_run = await retry_after_close_run(db=db, job_run_id=run_id)
    except ValueError as e:
        error_msg = str(e)
        if "不存在" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_msg,
            ) from e
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg,
        ) from e

    # 解析 trade_date 并启动后台执行
    from app.services.after_close_orchestrator import _parse_metadata
    meta = _parse_metadata(job_run)
    trade_date_str = meta.get("trade_date", "")
    trade_date = _parse_trade_date(trade_date_str) if trade_date_str else None

    if trade_date is not None:
        _kick_off_async_execution(job_run.id, trade_date)

    return AfterCloseRunCreateResponse(
        job_run_id=str(job_run.id),
        status=job_run.status,
        orchestrator_status=AfterCloseRunStatus.QUEUED.value,
        trade_date=trade_date_str,
        message=f"盘后编排已重试: job_run_id={job_run.id}",
    )


@router.post(
    "/after-close-runs/{run_id}/force",
    response_model=AfterCloseRunCreateResponse,
)
async def force_advance_after_close_endpoint(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AfterCloseRunCreateResponse:
    """强制重新执行盘后编排（非 failed 状态也可触发）。

    与 retry 的区别：force 不校验状态，任何状态都可强制重新执行。
    适用于任务卡在 running 状态但实际无 Worker 执行的场景。

    流程：
    1. 加载 job_run，校验为编排任务
    2. 重置 status=running, error_message=None
    3. 更新 orchestrator_status=queued
    4. 启动后台执行

    Args:
        run_id: 编排任务 ID
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        强制执行响应

    Raises:
        HTTPException 404: 任务不存在
        HTTPException 400: 任务非盘后编排
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from app.models.scheduler_job_run import SchedulerJobRun
    from app.services.after_close_orchestrator import (
        _ORCHESTRATOR_LEASE_SECONDS,
        _parse_metadata,
        _update_orchestrator_status,
    )

    job_run = await db.get(SchedulerJobRun, run_id)
    if job_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"编排任务不存在: job_run_id={run_id}",
        )
    if job_run.job_name != "after_close_orchestrator":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"任务非盘后编排: job_name={job_run.job_name}",
        )

    meta = _parse_metadata(job_run)
    trade_date_str = meta.get("trade_date", "")
    if not trade_date_str:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"metadata_json 中缺少 trade_date: job_run_id={run_id}",
        )
    trade_date = _parse_trade_date(trade_date_str)

    # 重置任务状态（不校验原状态，允许强制推进）
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    job_run.status = "running"
    job_run.error_message = None
    job_run.error_code = None
    job_run.finished_at = None
    job_run.started_at = now
    job_run.heartbeat_at = now
    job_run.lease_expires_at = now + timedelta(seconds=_ORCHESTRATOR_LEASE_SECONDS)

    await _update_orchestrator_status(
        db=db,
        job_run=job_run,
        status=AfterCloseRunStatus.QUEUED,
        message=f"管理员强制重新执行: job_run_id={run_id}",
    )
    await db.commit()

    _kick_off_async_execution(job_run.id, trade_date)

    return AfterCloseRunCreateResponse(
        job_run_id=str(job_run.id),
        status=job_run.status,
        orchestrator_status=AfterCloseRunStatus.QUEUED.value,
        trade_date=trade_date_str,
        message=f"盘后编排已强制重新执行: job_run_id={job_run.id}",
    )


@router.get(
    "/job-runs/{run_id}/events",
    response_model=JobRunEventListResponse,
)
async def list_job_run_events_endpoint(
    run_id: UUID,
    limit: int = Query(default=100, ge=1, le=500, description="最多返回事件数"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> JobRunEventListResponse:
    """查询任意任务的执行事件时间线（按 created_at 倒序）。

    通用端点，适用于所有 SchedulerJobRun（bars_scheduler /
    after_close_orchestrator / strategy_batch_worker 等）。

    Args:
        run_id: 任务 ID
        limit: 最多返回事件数
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        事件列表响应

    Raises:
        HTTPException 404: 任务不存在
    """
    from app.models.scheduler_job_run import SchedulerJobRun

    # 校验任务存在（不限制 job_name，通用端点）
    job_run = await db.get(SchedulerJobRun, run_id)
    if job_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"任务不存在: job_run_id={run_id}",
        )

    events = await list_events(db=db, job_run_id=run_id, limit=limit)
    return JobRunEventListResponse(
        items=[
            JobRunEventItem(
                id=e.id,
                job_run_id=e.job_run_id,
                step=e.step,
                level=e.level,
                message=e.message,
                payload=e.payload,
                created_at=e.created_at,
            )
            for e in events
        ],
        total=len(events),
    )


if __name__ == "__main__":
    # 自测入口：验证路由端点注册（不启动服务）
    routes = [(r.path, list(r.methods)) for r in router.routes if hasattr(r, "methods")]
    print(f"注册端点数: {len(routes)}")
    for path, methods in routes:
        print(f"  {methods} {path}")

    # 验证必要端点存在
    paths = {r.path for r in router.routes}
    assert "/admin/after-close-runs" in paths, "缺少 POST /admin/after-close-runs"
    assert "/admin/after-close-runs/{run_id}" in paths, "缺少 GET /admin/after-close-runs/{run_id}"
    assert "/admin/after-close-runs/{run_id}/retry" in paths, "缺少 retry 端点"
    assert "/admin/after-close-runs/{run_id}/force" in paths, "缺少 force 端点"
    assert "/admin/job-runs/{run_id}/events" in paths, "缺少 events 端点"
    print("端点验证 ✓")

    # 验证 AfterCloseRunCreateRequest schema
    req = AfterCloseRunCreateRequest(trade_date="2026-06-25")
    assert req.trade_date == "2026-06-25"
    print("AfterCloseRunCreateRequest 验证 ✓")

    print("OK")
