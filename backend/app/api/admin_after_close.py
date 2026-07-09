"""管理员 API 路由 - 盘后编排管理 + 任务事件时间线查询。

端点：
- POST /admin/after-close-runs: 创建并异步执行盘后编排（日线刷新→DSA→质量门禁→发布）
- POST /admin/after-close-runs/dsa-only: [Phase6] 仅重算今日 DSA（要求当日日线覆盖率 ≥ 90%）
- GET /admin/after-close-runs/{run_id}: 查询盘后编排状态（含事件时间线）
- POST /admin/after-close-runs/{run_id}/retry: 重试失败的盘后编排
- POST /admin/after-close-runs/{run_id}/resume: [Phase6] 从失败步骤继续（保留断点检查点）
- POST /admin/after-close-runs/{run_id}/force: 强制重新执行盘后编排（非 failed 状态也可触发）
- GET /admin/job-runs/{run_id}/events: 查询任意任务的执行事件时间线

权限：
- 所有端点需要 admin 角色（RBAC）
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.core.route_utils import get_route_paths, iter_api_routes
from app.schemas.after_close_pipeline import (
    AfterClosePipelineResponse,
    AfterClosePipelineRunListResponse,
    AfterClosePipelineRunRequest,
    AfterClosePipelineRunResponse,
)
from app.schemas.scheduler_job_run import (
    AfterCloseRunCreateResponse,
    AfterCloseRunStatusResponse,
    JobRunEventItem,
    JobRunEventListResponse,
)
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    create_after_close_run,
    get_after_close_run_status,
    retry_after_close_run,
)
from app.services.after_close_pipeline_service import (
    create_pipeline_run,
    get_latest_pipeline,
    get_pipeline_by_trade_date,
    list_pipeline_runs,
)
from app.services.calendar_service import is_trading_day_async
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
    """创建盘后编排任务（仅创建 queued 任务，由独立 Worker 领取执行）。

    [Phase5] API 不再直接启动后台执行，仅创建 status=queued 任务。
    独立的 run_after_close_orchestrator_worker 会通过 FOR UPDATE SKIP LOCKED
    领取 queued 任务并执行，支持断点恢复 + 心跳租约。

    流程：
    1. 解析 trade_date
    2. create_after_close_run 创建 SchedulerJobRun（幂等，status=queued）
    3. 立即返回任务 ID（不等待执行完成）

    幂等：同 trade_date 已有 queued/running 任务时返回已有任务。

    Args:
        payload: 创建请求（含 trade_date）
        db: 异步数据库会话
        current_user: 当前管理员（由 require_roles 注入）

    Returns:
        创建响应（含 job_run_id 和初始状态）
    """
    trade_date = _parse_trade_date(payload.trade_date)

    # [AfterClose] - 非交易日拦截：避免创建空转的盘后编排任务（不创建 SchedulerJobRun 记录）
    if not await is_trading_day_async(db, trade_date):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "NON_TRADING_DAY",
                "reason": "非交易日无需执行盘后编排",
                "trade_date": trade_date.isoformat(),
                "weekday": trade_date.strftime("%A"),
                "message": f"{trade_date.isoformat()}（{trade_date.strftime('%A')}）非交易日，无需执行盘后编排",
            },
        )

    job_run, is_new = await create_after_close_run(db=db, trade_date=trade_date)

    # [Phase5] - API 仅创建 queued 任务，由独立 Worker 领取执行（不再 _kick_off_async_execution）
    from app.services.after_close_orchestrator import _parse_metadata
    meta = _parse_metadata(job_run)
    orchestrator_status = meta.get("orchestrator_status")

    # [Spec] 已有运行中任务时拒绝重复创建：返回 409 Conflict，body 含已有 after_close_run_id
    # [AfterClose] - detail 增强：透传 error_code/started_at/heartbeat_at/last_completed_step，
    # 供前端展示真实冲突原因（当前阶段 + 开始时间）并提供"查看任务"入口（job_run_id）
    if not is_new:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "DUPLICATE_RUN",
                "after_close_run_id": str(job_run.id),
                "status": job_run.status,
                "orchestrator_status": orchestrator_status or "unknown",
                "trade_date": trade_date.isoformat(),
                "started_at": (
                    job_run.started_at.isoformat() if job_run.started_at else None
                ),
                "heartbeat_at": (
                    job_run.heartbeat_at.isoformat() if job_run.heartbeat_at else None
                ),
                "last_completed_step": meta.get("last_completed_step"),
                "message": f"当天已有盘后任务正在运行: trade_date={trade_date}",
            },
        )

    return AfterCloseRunCreateResponse(
        job_run_id=str(job_run.id),
        status=job_run.status,
        orchestrator_status=orchestrator_status or "unknown",
        trade_date=trade_date.isoformat(),
        message=f"任务已加入队列: trade_date={trade_date}",
    )


# [Phase6] - dsa-only 覆盖率门槛：当日日线覆盖率 ≥ 90% 才允许跳过日线刷新
_DSA_ONLY_COVERAGE_THRESHOLD = 0.9


@router.post(
    "/after-close-runs/dsa-only",
    response_model=AfterCloseRunCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_dsa_only_run_endpoint(
    payload: AfterCloseRunCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AfterCloseRunCreateResponse:
    """[Phase6] 仅重算今日 DSA（要求当日日线覆盖率 ≥ 90%）。

    流程：
    1. 解析 trade_date
    2. 调用 compute_daily_coverage 查询当日日线覆盖率（口径与
       BarsSchedulerService._check_daily_coverage_and_trigger_dsa 对齐：
       bars_daily.trade_date + instruments.status='active'）
    3. 覆盖率 < 90% 返 409 + reason=DATA_COVERAGE_INSUFFICIENT
    4. 覆盖率达标：调用 create_after_close_run 创建 queued 任务
    5. 在 metadata 中设置 mode='dsa_only' + last_completed_step='daily_ready'
       （Worker 领取后跳过 refresh_daily，直接 create_batch_run）
    6. 返回 queued 任务

    [Phase6] - 描述: 与 create_after_close_run_endpoint 的区别：
    - 不重复拉行情（要求行情已就绪）
    - metadata 标记 mode='dsa_only' 供 Worker 识别
    - 覆盖率不足时返 409 而非创建任务

    Args:
        payload: 创建请求（含 trade_date）
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        创建响应（含 job_run_id 和初始状态）

    Raises:
        HTTPException 409: 当日日线覆盖率不足
    """
    from app.core.time import shanghai_business_date
    from app.services.after_close_orchestrator import (
        _parse_metadata,
        _update_orchestrator_status,
    )
    from app.services.bars_coverage_service import BarsCoverageService

    requested_date = _parse_trade_date(payload.trade_date)
    today = shanghai_business_date()

    # [AfterClose] - 描述: dsa-only 仅重算今日，拒绝未来日期
    if requested_date > today:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"dsa-only 拒绝未来日期: trade_date={requested_date.isoformat()}",
        )

    # [AfterClose] - 描述: dsa-only 仅重算今日，拒绝历史日期
    if requested_date < today:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"dsa-only 拒绝历史日期: trade_date={requested_date.isoformat()}",
        )

    # [Bugfix] - 描述: dsa-only 与系统概览覆盖率口径对齐
    # 原逻辑：用前端传入的 trade_date（=today）精确匹配，今日未回补时覆盖率 0%
    # 修复：若请求日期当日无数据，fallback 到最新已落盘交易日（与系统概览一致）
    latest_available = await BarsCoverageService.get_latest_trade_date(db)
    if latest_available is not None and requested_date > latest_available:
        # 请求日期 > 最新可用日（今日未回补），用最新可用日
        trade_date = latest_available
        logger.info(
            "[dsa-only] 请求日期 %s 当日无数据，fallback 到最新可用日 %s",
            requested_date, trade_date,
        )
    else:
        trade_date = requested_date

    # [Phase6] - 计算当日日线覆盖率（纯查询，不触发 DSA）
    coverage_result = await BarsCoverageService.compute_daily_coverage(db, trade_date)
    covered = coverage_result["covered"]
    total = coverage_result["total"]
    coverage = coverage_result["coverage"]
    coverage_raw = coverage_result["coverage_raw"]
    # 覆盖率门禁使用原始值，避免四舍五入边缘误判
    if coverage_raw < _DSA_ONLY_COVERAGE_THRESHOLD:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "DATA_COVERAGE_INSUFFICIENT",
                "trade_date": coverage_result["trade_date"],
                "requested_trade_date": requested_date.isoformat(),
                "daily_coverage": coverage,
                "daily_covered": covered,
                "daily_total": total,
                "threshold": _DSA_ONLY_COVERAGE_THRESHOLD,
                "source": coverage_result["source"],
                "message": f"当日日线覆盖率不足: {coverage:.1%} < {_DSA_ONLY_COVERAGE_THRESHOLD:.0%}",
            },
        )

    # [Phase6] - 覆盖率达标，创建 queued 任务
    job_run, is_new = await create_after_close_run(db=db, trade_date=trade_date)
    if not is_new:
        # 同日已有任务，复用 create 端点的 409 语义（含 error_code=DUPLICATE_RUN）
        meta = _parse_metadata(job_run)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "DUPLICATE_RUN",
                "after_close_run_id": str(job_run.id),
                "status": job_run.status,
                "orchestrator_status": meta.get("orchestrator_status", "unknown"),
                "trade_date": trade_date.isoformat(),
                "started_at": (
                    job_run.started_at.isoformat() if job_run.started_at else None
                ),
                "heartbeat_at": (
                    job_run.heartbeat_at.isoformat() if job_run.heartbeat_at else None
                ),
                "last_completed_step": meta.get("last_completed_step"),
                "message": f"当天已有盘后任务正在运行: trade_date={trade_date}",
            },
        )

    # [Phase6] - 在 metadata 中追加 mode='dsa_only' + last_completed_step='daily_ready'
    # Worker 领取后应识别 mode=dsa_only 跳过 refresh_daily 直接 create_batch_run
    await _update_orchestrator_status(
        db=db,
        job_run=job_run,
        status=AfterCloseRunStatus.QUEUED,
        message=(
            f"[dsa-only] 仅重算今日 DSA: trade_date={trade_date}, "
            f"coverage={coverage:.1%} ({covered}/{total})"
        ),
        payload={
            "mode": "dsa_only",
            "daily_coverage": coverage,
            "daily_covered": covered,
            "daily_total": total,
        },
        extra={
            "mode": "dsa_only",
            "last_completed_step": "daily_ready",
        },
    )
    await db.commit()

    logger.info(
        "[Phase6] dsa-only 任务已创建: run_id=%s, trade_date=%s, coverage=%.1f%%",
        job_run.id, trade_date, coverage * 100,
    )

    return AfterCloseRunCreateResponse(
        job_run_id=str(job_run.id),
        status=job_run.status,
        orchestrator_status=AfterCloseRunStatus.QUEUED.value,
        trade_date=trade_date.isoformat(),
        message=(
            f"[dsa-only] 仅重算今日 DSA 已创建: trade_date={trade_date}, "
            f"coverage={coverage:.1%}"
        ),
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
        # [Phase7] - 详情字段透传
        worker_instance_id=result["worker_instance_id"],
        heartbeat_at=result["heartbeat_at"],
        lease_expires_at=result["lease_expires_at"],
        last_completed_step=result["last_completed_step"],
        interrupt_reason=result["interrupt_reason"],
        is_retryable=result["is_retryable"],
        heartbeat_stale=result["heartbeat_stale"],
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

    [Phase5] 仅重置为 queued 状态，由独立 Worker 领取执行（不再直接启动后台任务）。

    仅 failed 状态的任务可重试。重置为 queued 后由 Worker 领取。

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

    # [Phase5] - 仅重置为 queued，由独立 Worker 领取执行（不再 _kick_off_async_execution）
    from app.services.after_close_orchestrator import _parse_metadata
    meta = _parse_metadata(job_run)
    trade_date_str = meta.get("trade_date", "")

    return AfterCloseRunCreateResponse(
        job_run_id=str(job_run.id),
        status=job_run.status,
        orchestrator_status=AfterCloseRunStatus.QUEUED.value,
        trade_date=trade_date_str,
        message=f"盘后编排已重试: job_run_id={job_run.id}",
    )


# [Phase6] - resume 允许的状态：failed/interrupted 都可恢复（retry 仅允许 failed）
_RESUMABLE_STATUSES = {"failed", "interrupted"}


@router.post(
    "/after-close-runs/{run_id}/resume",
    response_model=AfterCloseRunCreateResponse,
)
async def resume_after_close_run_endpoint(
    run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AfterCloseRunCreateResponse:
    """[Phase6] 从失败步骤继续（复用已成功阶段，不重复拉行情）。

    与 retry 的区别：
    - retry 仅允许 status=failed，且重置 last_completed_step（从头执行）
    - resume 允许 failed/interrupted，**保留 last_completed_step**（从断点继续）

    流程：
    1. 加载 job_run，校验为 after_close_orchestrator
    2. 校验 status in ('failed', 'interrupted')，否则返 400
    3. 重置 status='queued'，保留 metadata.last_completed_step
    4. 更新 orchestrator_status='queued'
    5. 返回 queued 任务（Worker 领取后从 last_completed_step 之后继续）

    Args:
        run_id: 编排任务 ID
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        恢复响应

    Raises:
        HTTPException 404: 任务不存在
        HTTPException 400: 任务非盘后编排或状态非 failed/interrupted
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
    if job_run.status not in _RESUMABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"仅 failed/interrupted 状态可恢复: "
                f"current_status={job_run.status}"
            ),
        )

    meta = _parse_metadata(job_run)
    trade_date_str = meta.get("trade_date", "")
    if not trade_date_str:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"metadata_json 中缺少 trade_date: job_run_id={run_id}",
        )

    # [Phase6] - 重置为 queued（保留 last_completed_step），由独立 Worker 领取执行
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    job_run.status = "queued"
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
        message=(
            f"[resume] 从失败步骤继续: job_run_id={run_id}, "
            f"last_completed_step={meta.get('last_completed_step')}"
        ),
    )
    await db.commit()

    logger.info(
        "[Phase6] resume 任务已重置为 queued: run_id=%s, last_completed_step=%s",
        run_id, meta.get("last_completed_step"),
    )

    return AfterCloseRunCreateResponse(
        job_run_id=str(job_run.id),
        status=job_run.status,
        orchestrator_status=AfterCloseRunStatus.QUEUED.value,
        trade_date=trade_date_str,
        message=f"[resume] 盘后编排已从断点恢复: job_run_id={job_run.id}",
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

    [Phase5] 仅重置为 queued 状态，由独立 Worker 领取执行（不再直接启动后台任务）。

    与 retry 的区别：force 不校验状态，任何状态都可强制重新执行。
    适用于任务卡在 running 状态但实际无 Worker 执行的场景。

    流程：
    1. 加载 job_run，校验为编排任务
    2. 重置 status=queued, error_message=None（由 Worker 领取）
    3. 更新 orchestrator_status=queued

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

    # [Phase5] - 重置为 queued（不是 running），由独立 Worker 领取执行
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    job_run.status = "queued"
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

    # [Phase5] - 不再 _kick_off_async_execution，由独立 Worker 领取 queued 任务

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


@router.get(
    "/after-close/pipeline/latest",
    response_model=AfterClosePipelineResponse,
)
async def get_after_close_pipeline_latest(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AfterClosePipelineResponse:
    """查询最近交易日（含今日）的盘后流水线聚合状态。"""
    data = await get_latest_pipeline(db)
    return AfterClosePipelineResponse(**data)


@router.get(
    "/after-close/pipeline",
    response_model=AfterClosePipelineResponse,
)
async def get_after_close_pipeline_by_date(
    trade_date: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AfterClosePipelineResponse:
    """查询指定交易日的盘后流水线聚合状态。"""
    date_obj = _parse_trade_date(trade_date)
    data = await get_pipeline_by_trade_date(db, date_obj)
    return AfterClosePipelineResponse(**data)


@router.get(
    "/after-close/pipeline/runs",
    response_model=AfterClosePipelineRunListResponse,
)
async def get_after_close_pipeline_runs(
    limit: int = Query(default=20, ge=1, le=100, description="最多返回运行数"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AfterClosePipelineRunListResponse:
    """查询最近 N 次 after_close_orchestrator 与 snapshot run 摘要。"""
    items = await list_pipeline_runs(db, limit=limit)
    return AfterClosePipelineRunListResponse(items=items, total=len(items))


@router.post(
    "/after-close/pipeline/run",
    response_model=AfterClosePipelineRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_after_close_pipeline_run(
    payload: AfterClosePipelineRunRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AfterClosePipelineRunResponse:
    """管理员触发指定交易日的 after_close 编排任务。

    同 trade_date 已有 queued/running/succeeded 时返回 existing，不重复创建。
    """
    trade_date = _parse_trade_date(payload.trade_date)
    job_run, is_new = await create_pipeline_run(db, trade_date)
    meta = _parse_metadata_for_new_endpoint(job_run)
    return AfterClosePipelineRunResponse(
        job_run_id=str(job_run.id),
        trade_date=meta.get("trade_date", trade_date.isoformat()),
        status=job_run.status,
        orchestrator_status=meta.get("orchestrator_status"),
        is_new=is_new,
    )


def _parse_metadata_for_new_endpoint(job_run: Any) -> dict[str, Any]:
    """解析新端点返回所需的 metadata_json（与 orchestrator 的 _parse_metadata 对齐）。"""
    import json

    if not job_run.metadata_json:
        return {}
    try:
        return json.loads(job_run.metadata_json)
    except (json.JSONDecodeError, TypeError):
        return {}


if __name__ == "__main__":
    # 自测入口：验证路由端点注册（不启动服务）
    routes = [(r.path, list(r.methods)) for r in iter_api_routes(router.routes) if r.methods]
    print(f"注册端点数: {len(routes)}")
    for path, methods in routes:
        print(f"  {methods} {path}")

    # 验证必要端点存在
    paths = set(get_route_paths(router.routes))
    assert "/admin/after-close-runs" in paths, "缺少 POST /admin/after-close-runs"
    assert "/admin/after-close-runs/dsa-only" in paths, "缺少 dsa-only 端点"
    assert "/admin/after-close-runs/{run_id}" in paths, "缺少 GET /admin/after-close-runs/{run_id}"
    assert "/admin/after-close-runs/{run_id}/retry" in paths, "缺少 retry 端点"
    assert "/admin/after-close-runs/{run_id}/resume" in paths, "缺少 resume 端点"
    assert "/admin/after-close-runs/{run_id}/force" in paths, "缺少 force 端点"
    assert "/admin/job-runs/{run_id}/events" in paths, "缺少 events 端点"
    assert "/admin/after-close/pipeline/latest" in paths, "缺少 pipeline latest 端点"
    assert "/admin/after-close/pipeline" in paths, "缺少 pipeline by date 端点"
    assert "/admin/after-close/pipeline/runs" in paths, "缺少 pipeline runs 端点"
    assert "/admin/after-close/pipeline/run" in paths, "缺少 pipeline run 端点"
    print("端点验证 ✓")

    # 验证 AfterCloseRunCreateRequest schema
    req = AfterCloseRunCreateRequest(trade_date="2026-06-25")
    assert req.trade_date == "2026-06-25"
    print("AfterCloseRunCreateRequest 验证 ✓")

    print("OK")
