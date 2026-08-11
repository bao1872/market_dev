"""管理员 API 路由 - 盘后编排管理 + 任务事件时间线查询。

端点：
- POST /admin/after-close-runs: 创建并异步执行盘后编排（日线刷新→DSA→质量门禁→发布）
- GET /admin/after-close-runs/{run_id}: 查询盘后编排状态（含事件时间线）
- POST /admin/after-close-runs/{run_id}/retry: 重试失败的盘后编排
- POST /admin/after-close-runs/{run_id}/resume: [Phase6] 从失败步骤继续（保留断点检查点）
- POST /admin/after-close-runs/{run_id}/force: 强制重新执行盘后编排（非 failed 状态也可触发）
  支持 restart_from="daily_ready" 参数：从 DSA 阶段重算（跳过日线刷新，需覆盖率≥90%）
- GET /admin/job-runs/{run_id}/events: 查询任意任务的执行事件时间线

权限：
- 所有端点需要 admin 角色（RBAC）
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin_errors import (
    admin_bad_request,
    admin_conflict,
    admin_error,
    admin_not_found,
)
from app.core.deps import get_db, require_roles
from app.core.route_utils import get_route_paths, iter_api_routes
from app.schemas.after_close_pipeline import (
    AfterClosePipelineResponse,
    AfterClosePipelineRunListResponse,
    AfterClosePipelineRunRequest,
    AfterClosePipelineRunResponse,
)
from app.schemas.scheduler_job_run import (
    AfterCloseRunActionRequest,
    AfterCloseRunCreateResponse,
    AfterCloseRunStatusResponse,
    JobRunEventItem,
    JobRunEventListResponse,
)
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    cancel_after_close_run,
    create_after_close_run,
    get_after_close_run_status,
    reconcile_after_close_run,
    retry_after_close_run,
)
from app.services.after_close_pipeline_service import (
    create_pipeline_run,
    get_latest_pipeline,
    get_pipeline_by_trade_date,
    list_pipeline_runs,
)
from app.services.calendar_service import is_trading_day_async

# [CP3] restart boundary 枚举单一真源，避免 API 层与 service 层枚举漂移。
from app.services.granular_restart_service import (
    ALL_BOUNDARIES as _ALL_RESTART_BOUNDARIES,
)
from app.services.job_run_event_service import list_events

logger = logging.getLogger("admin_after_close")

router = APIRouter(
    prefix="/v1/admin",
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
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "after_close_invalid_trade_date",
            f"trade_date 格式错误（需 YYYY-MM-DD）: {trade_date_str}, error={e}",
            retryable=False,
            resumable=False,
            recommended_action="重新提交 YYYY-MM-DD 格式的交易日",
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
        # [PRD §8.4.9] 统一错误：stable_error_code=after_close_non_trading_day，error_code/reason 保留旧码 NON_TRADING_DAY
        raise admin_conflict(
            "after_close_non_trading_day",
            f"{trade_date.isoformat()}（{trade_date.strftime('%A')}）非交易日，无需执行盘后编排",
            legacy_error_code="NON_TRADING_DAY",
            severity="info",
            retryable=False,
            resumable=False,
            recommended_action="无需处理，非交易日不执行盘后编排",
            trade_date=trade_date.isoformat(),
            weekday=trade_date.strftime("%A"),
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
        # [PRD §8.4.9] 统一错误：stable_error_code=after_close_conflict，error_code/reason 保留旧码 DUPLICATE_RUN
        raise admin_conflict(
            "after_close_conflict",
            f"当天已有盘后任务正在运行: trade_date={trade_date}",
            legacy_error_code="DUPLICATE_RUN",
            severity="warning",
            retryable=False,
            resumable=True,
            recommended_action="查看任务详情或等待其进入终态",
            after_close_run_id=str(job_run.id),
            status=job_run.status,
            orchestrator_status=orchestrator_status or "unknown",
            trade_date=trade_date.isoformat(),
            started_at=(
                job_run.started_at.isoformat() if job_run.started_at else None
            ),
            heartbeat_at=(
                job_run.heartbeat_at.isoformat() if job_run.heartbeat_at else None
            ),
            last_completed_step=meta.get("last_completed_step"),
        )

    return AfterCloseRunCreateResponse(
        job_run_id=str(job_run.id),
        status=job_run.status,
        orchestrator_status=orchestrator_status or "unknown",
        trade_date=trade_date.isoformat(),
        message=f"任务已加入队列: trade_date={trade_date}",
    )


# [CHANGE-20260728-008] 原 /admin/after-close-runs/dsa-only 独立端点已删除，
# 功能并入 /admin/after-close-runs/{run_id}/force?restart_from=daily_ready。
# 系统只允许 job_name=after_close_orchestrator、run_type=full，
# 正常任务从 refreshing_daily 开始，不创建 dsa_only 类型。

# [PRD Alignment Pass P1-4] force?restart_from 合法取值 = PRD 31 §6 全枚举。
# 不同 restart 不应重算不相关上游：
#   review 不重算 core/board；board_aggregation 不重算 core；
#   chip 不重算 core；dsa_projection 不执行 DSA；
#   auction 不重算 core；state_events 不重算 core。
#
# [CP3 修正] 本集合此前只有 9 项，漏了 `board_aggregation`，导致该 boundary
# 虽有真实 handler 却被 API 层判为非法值（400）。现与
# granular_restart_service.ALL_BOUNDARIES 单一真源对齐，避免两处枚举漂移。
_RESTART_FROM_VALID_VALUES = set(_ALL_RESTART_BOUNDARIES)
# [PRD 31 §6 / V2.1] 所有 10 个 boundary 均已实现（granular_restart_service.dispatch_restart），
# 不再有 not_implemented 分支。
_RESTART_FROM_COVERAGE_THRESHOLD = 0.9


def _action_response(job_run, message: str) -> AfterCloseRunCreateResponse:
    from app.services.after_close_orchestrator import _parse_metadata

    meta = _parse_metadata(job_run)
    return AfterCloseRunCreateResponse(
        job_run_id=str(job_run.id),
        status=job_run.status,
        orchestrator_status=meta.get("orchestrator_status", job_run.status),
        trade_date=str(job_run.scheduled_for),
        message=message,
        parent_job_run_id=meta.get("parent_job_run_id"),
        restart_from=meta.get("restart_from"),
    )


@router.post("/after-close-runs/{run_id}/cancel", response_model=AfterCloseRunCreateResponse)
async def cancel_after_close_run_endpoint(
    run_id: str,
    request: Request,
    payload: AfterCloseRunActionRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AfterCloseRunCreateResponse:
    try:
        _request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        job_run = await cancel_after_close_run(
            db,
            job_run_id=run_id,
            reason=payload.reason if payload else None,
            actor=getattr(current_user, "username", None) or str(current_user),
            request_id=_request_id,
        )
        await db.commit()
        return _action_response(job_run, "盘后任务已取消或已处于终态")
    except ValueError as exc:
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_not_found(
            "after_close_run_not_found",
            str(exc),
            request_id=_request_id,
        ) from exc


@router.post("/after-close-runs/{run_id}/reconcile", response_model=AfterCloseRunCreateResponse)
async def reconcile_after_close_run_endpoint(
    run_id: str,
    request: Request,
    payload: AfterCloseRunActionRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AfterCloseRunCreateResponse:
    try:
        _request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        job_run = await reconcile_after_close_run(
            db,
            job_run_id=run_id,
            reason=payload.reason if payload else None,
            actor=getattr(current_user, "username", None) or str(current_user),
            # [Phase0-Fix#6] 此前生成了 _request_id 却未传入 service（审计断链）
            request_id=_request_id,
        )
        await db.commit()
        return _action_response(job_run, "盘后任务状态已校准")
    except ValueError as exc:
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_not_found(
            "after_close_run_not_found",
            str(exc),
            request_id=_request_id,
        ) from exc


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
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_not_found("after_close_run_not_found", str(e)) from e

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
        # [Phase0-Fix#4] 统一步骤合同 / watchdog 字段完整透传，
        # 修复「service 已计算 → API 组装丢弃 → 管理后台看不到」的合同断链。
        skip_reason=result.get("skip_reason"),
        step_summary=result.get("step_summary") or {},
        running_steps=result.get("running_steps") or [],
        step_timed_out=bool(result.get("step_timed_out")),
        stale=bool(result.get("stale")),
        partial_success=bool(result.get("partial_success")),
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
            # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
            raise admin_not_found(
                "after_close_run_not_found", error_msg,
            ) from e
        raise admin_bad_request(
            "after_close_not_retryable", error_msg,
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

    幂等：重复调用返回同一任务，不新建 SchedulerJobRun/StrategyRun/SnapshotRun。
    同日互斥：同 trade_date 已有 queued/running 任务时返 409。

    流程：
    1. SELECT FOR UPDATE 锁定 job_run（PostgreSQL 行级锁，SQLite 忽略）
    2. 校验为 after_close_orchestrator
    3. 幂等：status=queued 直接返回同一任务
    4. 校验 status in ('failed', 'interrupted')，否则返 400
    5. 同日 queued/running 互斥校验（排除自身）
    6. 保留 run_key/trade_date/dsa_run_id/feature_snapshot_run_id/
       last_started_step/last_completed_step/snapshot_progress
    7. 状态改 queued，清 finished_at/error_message/error_code/worker_instance_id
    8. heartbeat_at=None, lease_expires_at=None（由 Worker 领取后设置）
    9. metadata 写 resume_requested_at + orchestrator_status=queued
    10. 写唯一 manual_resume 事件
    11. commit 并返回

    Args:
        run_id: 编排任务 ID
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        恢复响应

    Raises:
        HTTPException 404: 任务不存在
        HTTPException 400: 任务非盘后编排或状态非 failed/interrupted
        HTTPException 409: 同日已有 queued/running 任务
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from sqlalchemy import select

    from app.models.scheduler_job_run import SchedulerJobRun
    from app.services.after_close_orchestrator import (
        _AFTER_CLOSE_JOB_NAME,
        _parse_metadata,
        _update_orchestrator_status,
    )
    from app.services.job_run_event_service import append_event

    # 1. SELECT FOR UPDATE 锁定 job_run（PostgreSQL 行级锁，SQLite 忽略 with_for_update）
    stmt = (
        select(SchedulerJobRun)
        .where(SchedulerJobRun.id == run_id)
        .with_for_update()
    )
    result = await db.execute(stmt)
    job_run = result.scalar_one_or_none()

    if job_run is None:
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_not_found(
            "after_close_run_not_found", f"编排任务不存在: job_run_id={run_id}",
        )
    if job_run.job_name != _AFTER_CLOSE_JOB_NAME:
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_bad_request(
            "after_close_wrong_job_type",
            f"任务非盘后编排: job_name={job_run.job_name}",
        )

    meta = _parse_metadata(job_run)
    trade_date_str = meta.get("trade_date", "")
    if not trade_date_str:
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_bad_request(
            "after_close_missing_trade_date",
            f"metadata_json 中缺少 trade_date: job_run_id={run_id}",
        )

    # 3. 幂等：已 queued（前一次 resume 已提交，Worker 尚未领取）直接返回同一任务
    if job_run.status == "queued":
        logger.info(
            "[resume] 幂等返回已 queued 任务: run_id=%s, last_completed_step=%s",
            run_id, meta.get("last_completed_step"),
        )
        return AfterCloseRunCreateResponse(
            job_run_id=str(job_run.id),
            status=job_run.status,
            orchestrator_status=AfterCloseRunStatus.QUEUED.value,
            trade_date=trade_date_str,
            message=f"[resume] 任务已处于 queued（幂等返回）: job_run_id={job_run.id}",
        )

    # 4. 校验状态
    if job_run.status not in _RESUMABLE_STATUSES:
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_bad_request(
            "after_close_not_resumable",
            (
                f"仅 failed/interrupted 状态可恢复: "
                f"current_status={job_run.status}"
            ),
        )

    # 5. 同日 queued/running 互斥校验（排除自身）
    conflict_stmt = (
        select(SchedulerJobRun)
        .where(
            SchedulerJobRun.job_name == _AFTER_CLOSE_JOB_NAME,
            SchedulerJobRun.business_date == trade_date_str,
            SchedulerJobRun.status.in_(["queued", "running"]),
            SchedulerJobRun.id != run_id,
        )
    )
    conflict_result = await db.execute(conflict_stmt)
    conflict_run = conflict_result.scalar_one_or_none()
    if conflict_run is not None:
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_conflict(
            "SAME_DAY_ACTIVE_RUN",
            (
                f"同日已有 {conflict_run.status} 任务: "
                f"trade_date={trade_date_str}, run_id={conflict_run.id}"
            ),
            severity="warning",
            retryable=True,
            resumable=True,
            recommended_action="等待同日活跃任务进入终态后再恢复",
            conflicting_run_id=str(conflict_run.id),
            trade_date=trade_date_str,
            status=conflict_run.status,
        )

    # 6-8. 重置为 queued（保留 last_completed_step / dsa_run_id /
    #      feature_snapshot_run_id / snapshot_progress 等已有 metadata）
    #      清 finished_at/error_message/error_code/worker_instance_id
    #      heartbeat_at/lease_expires_at 置 None（由 Worker 领取后设置）
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    job_run.status = "queued"
    job_run.error_message = None
    job_run.error_code = None
    job_run.finished_at = None
    job_run.worker_instance_id = None
    job_run.heartbeat_at = None
    job_run.lease_expires_at = None

    # 9. metadata 写 resume_requested_at + orchestrator_status=queued
    #    _update_orchestrator_status 保留已有 metadata 字段，仅更新 orchestrator_status
    await _update_orchestrator_status(
        db=db,
        job_run=job_run,
        status=AfterCloseRunStatus.QUEUED,
        message=(
            f"[resume] 从失败步骤继续: job_run_id={run_id}, "
            f"last_completed_step={meta.get('last_completed_step')}"
        ),
        extra={
            "resume_requested_at": now.isoformat(),
        },
    )

    # 10. 写唯一 manual_resume 事件
    await append_event(
        db=db,
        job_run_id=job_run.id,
        step="manual_resume",
        level="info",
        message=(
            f"管理员手动恢复: job_run_id={run_id}, "
            f"trade_date={trade_date_str}, "
            f"last_completed_step={meta.get('last_completed_step')}"
        ),
        payload={
            "resume_requested_at": now.isoformat(),
            "last_completed_step": meta.get("last_completed_step"),
            "last_started_step": meta.get("last_started_step"),
            "trade_date": trade_date_str,
        },
    )

    # 11. commit 并返回
    await db.commit()

    logger.info(
        "[resume] 任务已重置为 queued: run_id=%s, last_completed_step=%s",
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
    restart_from: str | None = Query(
        default=None,
        description="从指定步骤重新执行（目前仅支持 'daily_ready'，跳过日线刷新，需覆盖率≥90%）",
    ),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AfterCloseRunCreateResponse:
    """强制重新执行盘后编排（非 failed 状态也可触发）。

    [Phase5] 仅重置为 queued 状态，由独立 Worker 领取执行（不再直接启动后台任务）。

    与 retry 的区别：force 不校验状态，任何状态都可强制重新执行。
    适用于任务卡在 running 状态但实际无 Worker 执行的场景。

    restart_from 参数（统一替代原 dsa-only 独立端点）：
    - restart_from="daily_ready"：跳过日线刷新，从 DSA 阶段开始重算。
      要求当日日线覆盖率 ≥ 90%，否则返 409。
      仍执行 syncing_boards → computing_dsa → computing_features → publishing 全链路，
      不跳过特征/快照/发布。
    - 不传 restart_from：从头执行（默认行为）。

    流程：
    1. 加载 job_run，校验为编排任务
    2. 若 restart_from="daily_ready"：校验覆盖率 ≥ 90%
    3. 重置 status=queued, error_message=None（由 Worker 领取）
    4. 更新 orchestrator_status=queued
    5. [CP3] 交由 granular_restart_service.dispatch_restart 按 boundary 显式分派；
       主链 boundary 写 child metadata.mainchain_stage（worker 起始阶段），
       **不再写 last_completed_step**（该字段语义为"已完成"，且 orchestrator
       的 _completed_steps 不包含 checking_coverage，写入会导致语义反转）。

    Args:
        run_id: 编排任务 ID
        restart_from: 从指定步骤重新执行（"daily_ready" 跳过日线刷新）
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        强制执行响应

    Raises:
        HTTPException 404: 任务不存在
        HTTPException 400: 任务非盘后编排或 restart_from 值非法
        HTTPException 409: restart_from="daily_ready" 但覆盖率不足
    """

    from app.models.scheduler_job_run import SchedulerJobRun
    from app.services.after_close_orchestrator import (
        _parse_metadata,
    )

    # 校验 restart_from 取值
    if restart_from is not None and restart_from not in _RESTART_FROM_VALID_VALUES:
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_bad_request(
            "after_close_invalid_restart_from",
            (
                f"restart_from 仅支持 {_RESTART_FROM_VALID_VALUES}，"
                f"当前值: {restart_from}"
            ),
        )

    # [V2.1 PRD 31 §6] 所有 10 个 boundary 均已实现（granular_restart_service.dispatch_restart），
    # 仅校验值是否在正式枚举内，不再返回 not_implemented。
    if restart_from is not None and restart_from not in _RESTART_FROM_VALID_VALUES:
        raise admin_error(
            "invalid_restart_from",
            f"restart_from={restart_from} 不在正式枚举 {sorted(_RESTART_FROM_VALID_VALUES)} 内",
        )

    job_run = await db.get(SchedulerJobRun, run_id)
    if job_run is None:
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_not_found(
            "after_close_run_not_found", f"编排任务不存在: job_run_id={run_id}",
        )
    if job_run.job_name != "after_close_orchestrator":
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_bad_request(
            "after_close_wrong_job_type",
            f"任务非盘后编排: job_name={job_run.job_name}",
        )

    meta = _parse_metadata(job_run)
    trade_date_str = meta.get("trade_date", "")
    if not trade_date_str:
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_bad_request(
            "after_close_missing_trade_date",
            f"metadata_json 中缺少 trade_date: job_run_id={run_id}",
        )

    # restart_from="daily_ready"：校验覆盖率 ≥ 90%
    if restart_from == "daily_ready":
        from datetime import date as date_cls

        from app.services.bars_coverage_service import BarsCoverageService

        trade_date = date_cls.fromisoformat(trade_date_str)
        coverage_result = await BarsCoverageService.compute_daily_coverage(
            db, trade_date,
        )
        coverage_raw = coverage_result["coverage_raw"]
        if coverage_raw < _RESTART_FROM_COVERAGE_THRESHOLD:
            # [PRD §8.4.9] 统一错误：stable_error_code=after_close_coverage_insufficient，
            # error_code/reason 保留旧码 DATA_COVERAGE_INSUFFICIENT
            raise admin_conflict(
                "after_close_coverage_insufficient",
                (
                    f"restart_from=daily_ready 覆盖率不足: "
                    f"{coverage_result['coverage']:.1%} < "
                    f"{_RESTART_FROM_COVERAGE_THRESHOLD:.0%}"
                ),
                legacy_error_code="DATA_COVERAGE_INSUFFICIENT",
                severity="warning",
                retryable=True,
                resumable=False,
                recommended_action="重新同步日线数据后再重试",
                trade_date=coverage_result["trade_date"],
                daily_coverage=coverage_result["coverage"],
                daily_covered=coverage_result["covered"],
                daily_total=coverage_result["total"],
                threshold=_RESTART_FROM_COVERAGE_THRESHOLD,
            )

    # [V2.1 / CP3] 所有 10 个 boundary 经 granular_restart_service 真实调度，不返回 501。
    # 主链四 boundary：创建 child SchedulerJobRun 并写 metadata.mainchain_stage（worker 从
    #   该阶段开始执行），child 保持 queued —— **不写 last_completed_step**
    #   （orchestrator 的 _completed_steps 不认识 checking_coverage，且语义为"已完成"，相反）。
    # 子产品六 boundary：本请求内同步执行真实重建 + 发布，成功 succeeded / 失败 failed
    #   并记 level=error 事件（记录真实异常，不伪造成功）。
    from app.services.granular_restart_service import dispatch_restart

    try:
        handled = await dispatch_restart(
            db=db,
            job_run=job_run,
            restart_from=restart_from,
            actor=current_user.username if hasattr(current_user, "username") else str(current_user),
            request_id=str(uuid.uuid4()),
        )
    except NotImplementedError as exc:
        # 无真实领域级 handler（如 state_events 尚未实现重建入口）：明确报错，不伪造成功、不 501。
        raise admin_error(
            "restart_boundary_not_implemented",
            f"{exc}（该 boundary 已纳入 PRD §6 合同，但后端真实 handler 未实现，禁止伪造成功）",
        ) from exc
    except ValueError as exc:
        raise admin_bad_request("invalid_restart_request", str(exc)) from exc

    # restart_from="daily_ready"：清除 child 上可能继承的旧 dsa_run_id（worker 会新建 DSA run）
    if restart_from == "daily_ready":
        meta_after = _parse_metadata(handled)
        if "dsa_run_id" in meta_after:
            meta_after.pop("dsa_run_id", None)
            handled.metadata_json = json.dumps(meta_after, ensure_ascii=False)
            await db.commit()

    # [Phase5] - 不再 _kick_off_async_execution，由独立 Worker 领取 queued 任务
    final_status = handled.status
    return AfterCloseRunCreateResponse(
        job_run_id=str(handled.id),
        status=final_status,
        orchestrator_status=(
            _parse_metadata(handled).get("orchestrator_status", final_status)
            if handled.job_name == "after_close_orchestrator"
            else final_status
        ),
        trade_date=trade_date_str,
        message=(
            f"granular restart 已调度: boundary={restart_from}, "
            f"job_run_id={handled.id}, status={final_status}"
        ),
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
        # [PRD §8.4.9] 统一错误字段（管理 API 唯一构造器）
        raise admin_not_found(
            "after_close_run_not_found", f"任务不存在: job_run_id={run_id}",
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
    assert "/admin/after-close-runs/dsa-only" not in paths, "dsa-only 端点应已删除"
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
