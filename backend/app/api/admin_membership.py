"""管理员 API 路由 - 会员管理 + 系统概览。

端点：
- POST /admin/invite-codes: 生成邀请码（单个/批量）
- GET /admin/invite-codes: 查询邀请码列表（支持状态筛选 + 分页）
- POST /admin/invite-codes/{id}/revoke: 作废邀请码
- GET /admin/members: 查询会员账户列表（含会员状态/到期时间/剩余天数/续期次数）
- GET /admin/members/{user_id}/redemptions: 查询用户兑换记录
- GET /admin/system-overview: 系统概览（活跃用户/监控标的/评估统计/服务健康）

权限：
- 所有端点需要 admin 角色（RBAC）
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.strategy_keys import DSA_SELECTOR
from app.core.deps import get_db, require_roles
from app.models.monitor_evaluation import MonitorEvaluation
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy import StrategyDefinition
from app.models.strategy_run import StrategyRun
from app.models.user import User
from app.models.watchlist import UserWatchlistItem
from app.models.worker_heartbeat import WorkerHeartbeat
from app.schemas.membership import (
    InviteCodeCreate,
    InviteCodeListItem,
    InviteCodeResponse,
    InviteRedemptionResponse,
    MemberListItem,
)
from app.schemas.scheduler_job_run import (
    RecentSchedulerJobSummary,
    SchedulerJobRunItem,
    SchedulerJobRunListResponse,
)
from app.services.membership_service import (
    generate_invite_codes,
    get_redemptions_by_user,
    list_invite_codes,
    list_members,
    revoke_invite_code,
)

router = APIRouter(
    prefix="/admin",
    tags=["admin-membership"],
)


@router.post("/invite-codes", response_model=list[InviteCodeResponse])
async def create_invite_codes(
    payload: InviteCodeCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> list[InviteCodeResponse]:
    """生成邀请码（单个/批量）。

    固定权益为"会员 +30 天"。明文仅在生成时返回，后续不可获取。

    Args:
        payload: 生成请求（count + note）
        db: 异步数据库会话
        current_user: 当前管理员用户（由 require_roles 注入）

    Returns:
        邀请码列表（含明文）
    """
    results = await generate_invite_codes(
        db=db,
        count=payload.count,
        created_by=current_user.id,
        note=payload.note,
    )
    await db.commit()

    return [
        InviteCodeResponse(
            id=invite.id,
            code=raw_code,
            grant_days=invite.grant_days,
            note=invite.note,
            created_at=invite.created_at,
        )
        for invite, raw_code in results
    ]


@router.get("/invite-codes")
async def get_invite_codes(
    status_filter: str | None = Query(default=None, alias="status", description="状态筛选：unused/used/revoked"),
    limit: int = Query(default=50, ge=1, le=200, description="分页大小"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> dict:
    """查询邀请码列表（支持状态筛选 + 分页）。

    Args:
        status_filter: 状态筛选
        limit: 分页大小
        offset: 分页偏移
        db: 异步数据库会话

    Returns:
        {items: InviteCodeListItem[], total: int, limit: int, offset: int}
    """
    items, total = await list_invite_codes(
        db=db,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [
            InviteCodeListItem(
                id=invite.id,
                status=invite.status,
                grant_days=invite.grant_days,
                note=invite.note,
                created_by=invite.created_by,
                created_at=invite.created_at,
                used_by=invite.used_by,
                used_at=invite.used_at,
                usage_type=invite.usage_type,
            )
            for invite in items
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/invite-codes/{invite_code_id}/revoke", response_model=InviteCodeListItem)
async def revoke_code(
    invite_code_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> InviteCodeListItem:
    """作废邀请码（仅 unused 状态可作废）。

    Args:
        invite_code_id: 邀请码 ID
        db: 异步数据库会话

    Returns:
        更新后的 InviteCodeListItem

    Raises:
        HTTPException 400: 邀请码不存在或状态非 unused
    """
    try:
        invite = await revoke_invite_code(db=db, invite_code_id=invite_code_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await db.commit()

    return InviteCodeListItem(
        id=invite.id,
        status=invite.status,
        grant_days=invite.grant_days,
        note=invite.note,
        created_by=invite.created_by,
        created_at=invite.created_at,
        used_by=invite.used_by,
        used_at=invite.used_at,
        usage_type=invite.usage_type,
    )


@router.get("/members")
async def get_members(
    limit: int = Query(default=50, ge=1, le=200, description="分页大小"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> dict:
    """查询会员账户列表（含会员状态/到期时间/剩余天数/续期次数）。

    Args:
        limit: 分页大小
        offset: 分页偏移
        db: 异步数据库会话

    Returns:
        {items: MemberListItem[], total: int, limit: int, offset: int}
    """
    members, total = await list_members(db=db, limit=limit, offset=offset)
    return {
        "items": [MemberListItem(**m) for m in members],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/members/{user_id}/redemptions",
    response_model=list[InviteRedemptionResponse],
)
async def get_member_redemptions(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> list[InviteRedemptionResponse]:
    """查询用户兑换记录。

    Args:
        user_id: 用户 ID
        db: 异步数据库会话

    Returns:
        兑换记录列表
    """
    redemptions = await get_redemptions_by_user(db=db, user_id=user_id)
    return [
        InviteRedemptionResponse(
            id=r.id,
            invite_code_id=r.invite_code_id,
            user_id=r.user_id,
            usage_type=r.usage_type,
            old_expires_at=r.old_expires_at,
            new_expires_at=r.new_expires_at,
            redeemed_at=r.redeemed_at,
        )
        for r in redemptions
    ]


@router.get("/scheduler-job-runs", response_model=SchedulerJobRunListResponse)
async def get_scheduler_job_runs(
    job_name: str | None = Query(default=None, description="任务名称筛选"),
    business_date: str | None = Query(default=None, description="业务日期 YYYY-MM-DD"),
    status: str | None = Query(default=None, description="状态：running/succeeded/failed"),
    limit: int = Query(default=50, ge=1, le=200, description="分页大小"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> SchedulerJobRunListResponse:
    """查询定时任务运行记录（SchedulerJobRun）。

    返回最近创建的定时任务执行记录，支持按任务名、业务日期、状态筛选。
    """
    # 构建筛选条件
    filters = []
    if job_name:
        filters.append(SchedulerJobRun.job_name == job_name)
    if business_date:
        filters.append(SchedulerJobRun.business_date == business_date)
    if status:
        filters.append(SchedulerJobRun.status == status)

    # 总数
    count_stmt = select(func.count(SchedulerJobRun.id)).where(*filters)
    total = await db.scalar(count_stmt) or 0

    # 分页查询
    stmt = (
        select(SchedulerJobRun)
        .where(*filters)
        .order_by(SchedulerJobRun.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = list(result.scalars().all())

    return SchedulerJobRunListResponse(
        items=[SchedulerJobRunItem.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/system-overview")
async def get_system_overview(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> dict:
    """系统概览 - 管理员仪表盘数据。

    返回活跃用户、监控标的、评估统计、服务健康等数据。
    已实现的数据源：active_users, distinct_monitored_instruments, latest_selector_run,
    evaluations_success_rate, failed_retry_count。
    尚无数据源的字段返回 0/unknown。

    Args:
        db: 异步数据库会话
        current_user: 当前管理员用户（由 require_roles 注入）

    Returns:
        系统概览字典
    """
    # 1. active_users: 有活跃自选股的去重用户数
    active_users_stmt = select(func.count(func.distinct(UserWatchlistItem.user_id))).where(
        UserWatchlistItem.active.is_(True),
    )
    active_users = await db.scalar(active_users_stmt) or 0

    # 2. distinct_monitored_instruments: 活跃自选股去重标的数
    distinct_instruments_stmt = select(
        func.count(func.distinct(UserWatchlistItem.instrument_id)),
    ).where(
        UserWatchlistItem.active.is_(True),
    )
    distinct_monitored_instruments = await db.scalar(distinct_instruments_stmt) or 0

    # 3. evaluations_last_minute: 最近 1 分钟完成的评估数
    one_minute_ago = datetime.now(UTC) - timedelta(minutes=1)
    eval_last_min_stmt = select(func.count()).select_from(MonitorEvaluation).where(
        MonitorEvaluation.calculated_at >= one_minute_ago,
        MonitorEvaluation.status.in_(["SUCCEEDED", "FAILED"]),
    )
    evaluations_last_minute = await db.scalar(eval_last_min_stmt) or 0

    # 4. evaluations_success_rate: 已完成评估的成功率
    total_completed_stmt = select(func.count()).select_from(MonitorEvaluation).where(
        MonitorEvaluation.status.in_(["SUCCEEDED", "FAILED", "DEAD"]),
    )
    total_completed = await db.scalar(total_completed_stmt) or 0
    succeeded_stmt = select(func.count()).select_from(MonitorEvaluation).where(
        MonitorEvaluation.status == "SUCCEEDED",
    )
    succeeded_count = await db.scalar(succeeded_stmt) or 0
    evaluations_success_rate = round(succeeded_count / total_completed, 4) if total_completed > 0 else 0.0

    # 5. failed_retry_count: 当前 FAILED 状态且可重试的评估数
    failed_retry_stmt = select(func.count()).select_from(MonitorEvaluation).where(
        MonitorEvaluation.status == "FAILED",
    )
    failed_retry_count = await db.scalar(failed_retry_stmt) or 0

    # 6. latest_selector_run: dsa_selector 最近一次运行
    selector_def_stmt = select(StrategyDefinition.id).where(
        StrategyDefinition.strategy_key == DSA_SELECTOR,
    )
    selector_def_id = await db.scalar(selector_def_stmt)
    latest_selector_run = None
    if selector_def_id is not None:
        from app.models.strategy import StrategyVersion
        version_ids_stmt = select(StrategyVersion.id).where(
            StrategyVersion.strategy_definition_id == selector_def_id,
        )
        version_ids_result = await db.execute(version_ids_stmt)
        version_ids = [row[0] for row in version_ids_result.all()]
        if version_ids:
            run_stmt = (
                select(StrategyRun)
                .where(StrategyRun.strategy_version_id.in_(version_ids))
                .order_by(StrategyRun.started_at.desc())
                .limit(1)
            )
            run_result = await db.execute(run_stmt)
            run = run_result.scalar_one_or_none()
            if run is not None:
                latest_selector_run = {
                    "id": str(run.id),
                    "status": run.status,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                    "total_instruments": run.total_instruments,
                    "succeeded_count": run.succeeded_count,
                    "failed_count": run.failed_count,
                }

    # 7. queue_backlog: queued 状态的 StrategyRun 数量
    queued_count_stmt = select(func.count(StrategyRun.id)).where(
        StrategyRun.status == "queued",
    )
    queue_backlog = await db.scalar(queued_count_stmt) or 0

    # 8. worker_health / scheduler_health: 基于 worker_heartbeats 实时查询
    heartbeat_stmt = select(WorkerHeartbeat)
    heartbeats_result = await db.execute(heartbeat_stmt)
    hb_list = heartbeats_result.scalars().all()

    now = datetime.now(UTC)
    active_workers = [
        hb for hb in hb_list
        if hb.status == "running" and (now - hb.heartbeat_at).total_seconds() < 120
    ]
    all_running_workers = [hb for hb in hb_list if hb.status == "running"]

    scheduler_names = {hb.worker_name for hb in active_workers if "scheduler" in hb.worker_name}

    worker_health = "healthy" if active_workers else ("degraded" if all_running_workers else "unknown")
    scheduler_health = "healthy" if scheduler_names else ("degraded" if all_running_workers else "unknown")

    # 9. recent_scheduler_jobs: 最近 24 小时内各 job_name 最新一条记录
    one_day_ago = datetime.now(UTC) - timedelta(days=1)
    recent_jobs_subq = (
        select(
            SchedulerJobRun,
            func.row_number().over(
                partition_by=SchedulerJobRun.job_name,
                order_by=SchedulerJobRun.created_at.desc(),
            ).label("rn"),
        )
        .where(SchedulerJobRun.created_at >= one_day_ago)
        .subquery()
    )
    recent_jobs_stmt = select(recent_jobs_subq).where(recent_jobs_subq.c.rn == 1)
    recent_jobs_result = await db.execute(recent_jobs_stmt)
    recent_scheduler_jobs = [
        RecentSchedulerJobSummary(
            job_name=row.job_name,
            status=row.status,
            business_date=row.business_date,
            started_at=row.started_at,
            finished_at=row.finished_at,
            progress=row.progress,
            succeeded_count=row.succeeded_count,
            failed_count=row.failed_count,
            error_message=row.error_message,
        ).model_dump()
        for row in recent_jobs_result
    ]

    return {
        "active_users": active_users,
        "distinct_monitored_instruments": distinct_monitored_instruments,
        "evaluations_last_minute": evaluations_last_minute,
        "evaluations_success_rate": evaluations_success_rate,
        "notification_delivery_rate": 0.0,
        "queue_backlog": queue_backlog,
        "failed_retry_count": failed_retry_count,
        "latest_selector_run": latest_selector_run,
        "worker_health": worker_health,
        "scheduler_health": scheduler_health,
        "recent_scheduler_jobs": recent_scheduler_jobs,
        "recent_anomalies": [],
    }


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert "/admin/invite-codes" in paths
    assert "/admin/members" in paths
    assert "/admin/system-overview" in paths
    print("OK")
