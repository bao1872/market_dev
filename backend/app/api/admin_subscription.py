"""管理员 API 路由 - 订阅管理 + 系统概览。

端点：
- POST /admin/invite-codes: 生成邀请码（单个/批量，绑定 plan_code/grant_months）
- GET /admin/invite-codes: 查询邀请码列表（支持状态筛选 + 分页）
- POST /admin/invite-codes/{id}/revoke: 作废邀请码
- GET /admin/members: 查询订阅账户列表（含订阅状态/到期时间/剩余天数/续期次数）
- GET /admin/members/{user_id}/redemptions: 查询用户兑换记录
- GET /admin/system-overview: 系统概览（活跃用户/监控标的/评估统计/服务健康）

权限：
- 所有端点需要 admin 角色（RBAC）

套餐权限（plans 表）：
- 生成邀请码时接收 plan_code/grant_months，从 plans 表读取 monitor_limit 快照
- 默认 plan_code=observe_20、grant_months=1（保持向后兼容）
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.models.access_audit_log import AccessAuditLog
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.models.worker_heartbeat import WorkerHeartbeat
from app.schemas.access import (
    AdminAccessProfileResponse,
    AdminAccountInfo,
    EffectiveAccessInfo,
    ExplicitCapabilityRecord,
    SubscriptionSummaryInfo,
)
from app.schemas.invitation import (
    InviteCodeCreate,
    InviteCodeListItem,
    InviteCodeResponse,
    InviteRedemptionResponse,
)
from app.schemas.notification import MessageDeliveryResponse
from app.schemas.scheduler_job_run import (
    SchedulerJobRunItem,
    SchedulerJobRunListResponse,
)
from app.schemas.subscription import (
    ChangePlanRequest,
    ChangeSelfSelectionQuotaRequest,
    GrantCapabilityRequest,
    GrantSubscriptionRequest,
    MemberListItem,
    RenewSubscriptionRequest,
    SubscriptionRenewResponse,
    SubscriptionResponse,
    UserCapabilitiesResponse,
)
from app.schemas.system_overview import SystemOverviewResponse
from app.schemas.user import UserResponse
from app.schemas.worker_heartbeat import (
    WorkerHeartbeatItem,
    WorkerHeartbeatListResponse,
    classify_health_state,
)
from app.services.access_audit_service import query_audit_logs, write_audit_log
from app.services.notification_service import list_message_deliveries, retry_delivery
from app.services.subscription_service import (
    change_self_selection_quota,
    change_subscription_plan,
    generate_invite_codes,
    get_redemptions_by_user,
    get_user_capabilities,
    grant_capability_to_user,
    grant_subscription_to_user,
    list_invite_codes,
    list_subscribers_with_capabilities,
    renew_subscription,
    revoke_capability_from_user,
    revoke_invite_code,
    revoke_subscription,
)
from app.services.system_overview_service import get_system_overview

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin-subscription"],
)


class ChangeRoleRequest(BaseModel):
    """管理员修改用户角色请求。"""

    role: str = Field(..., description="目标角色：admin/member")


class ResetPasswordResponse(BaseModel):
    """管理员重置用户密码响应（当前仅记录审计日志，密码重置链路后续实现）。"""

    user_id: UUID = Field(..., description="用户 ID")
    message: str = Field(default="密码重置请求已记录", description="操作提示")


class AuditLogListItem(BaseModel):
    """审计日志列表项。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="日志 ID")
    actor_user_id: UUID = Field(..., description="操作者 user_id")
    action: str = Field(..., description="操作类型")
    target_type: str = Field(..., description="目标对象类型")
    target_id: str | None = Field(None, description="目标对象 ID")
    before_data: dict | None = Field(None, description="操作前快照")
    after_data: dict | None = Field(None, description="操作后快照")
    request_id: str | None = Field(None, description="请求追踪 ID")
    ip_hash: str | None = Field(None, description="IP 哈希")
    created_at: datetime = Field(..., description="操作时间")


class AuditLogListResponse(BaseModel):
    """审计日志列表响应。"""

    items: list[AuditLogListItem] = Field(default_factory=list, description="日志列表")
    total: int = Field(..., description="总数")
    limit: int = Field(..., description="分页大小")
    offset: int = Field(..., description="分页偏移")


class UserListResponse(BaseModel):
    """用户列表分页响应。"""

    items: list[UserResponse] = Field(default_factory=list, description="用户列表")
    total: int = Field(..., description="总数")
    limit: int = Field(..., description="分页大小")
    offset: int = Field(..., description="分页偏移")


@router.post("/invite-codes", response_model=list[InviteCodeResponse])
async def create_invite_codes(
    payload: InviteCodeCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> list[InviteCodeResponse]:
    """生成邀请码（单个/批量，绑定 plan_code/grant_months）。

    从 plans 表读取 monitor_limit 快照写入邀请码。明文仅在生成时返回，后续不可获取。
    默认 plan_code=observe_20、grant_months=1（保持向后兼容）。

    Args:
        payload: 生成请求（count + note + plan_code + grant_months）
        db: 异步数据库会话
        current_user: 当前管理员用户（由 require_roles 注入）

    Returns:
        邀请码列表（含明文 + 套餐快照）

    Raises:
        HTTPException 400: plan_code 未知或 grant_months 非法
    """
    try:
        # [Phase 5B-2 PRD60 PA-20] capabilities 优先于 plan_code
        capabilities_json = None
        if payload.capabilities is not None:
            capabilities_json = [cap.model_dump() for cap in payload.capabilities]

        results = await generate_invite_codes(
            db=db,
            count=payload.count,
            created_by=current_user.id,
            note=payload.note,
            plan_code=payload.plan_code,
            grant_months=payload.grant_months,
            capabilities=capabilities_json,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    # [AuditLog] - 描述: 为每个生成的邀请码写审计日志（不含明文 code，仅记录套餐快照与状态）
    for invite, _raw_code in results:
        await write_audit_log(
            db=db,
            actor_user_id=current_user.id,
            action="invite_code.create",
            target_type="invite_code",
            target_id=str(invite.id),
            after_data={
                "status": invite.status,
                "plan_code": invite.plan_code,
                "monitor_limit": invite.monitor_limit,
                "grant_months": invite.grant_months,
                "grant_days": invite.grant_days,
                "note": invite.note,
                # [PRD60 PA-20] 记录实际授予的 capability 组合（旧模式为 None）
                # research_replay 在展示层为「复盘与竞价」，机器值保持 research_replay
                "capabilities": invite.capabilities,
            },
        )

    await db.commit()

    return [
        InviteCodeResponse(
            id=invite.id,
            code=raw_code,
            grant_days=invite.grant_days,
            plan_code=invite.plan_code,
            monitor_limit=invite.monitor_limit,
            grant_months=invite.grant_months,
            note=invite.note,
            created_at=invite.created_at,
            # [PRD60 PA-20] 回显 capability 组合，供前端展示实际权限（旧模式为 None）
            capabilities=invite.capabilities,
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
                plan_code=invite.plan_code,
                monitor_limit=invite.monitor_limit,
                grant_months=invite.grant_months,
                note=invite.note,
                created_by=invite.created_by,
                created_at=invite.created_at,
                used_by=invite.used_by,
                used_at=invite.used_at,
                usage_type=invite.usage_type,
                # [PRD60 PA-20] 回显 capability 组合（旧模式为 None）
                capabilities=invite.capabilities,
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

    # [AuditLog] - 描述: 记录邀请码作废操作（before=unused -> after=revoked）
    # revoke_invite_code 仅允许 unused 状态作废，故 before_data.status 必为 "unused"
    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="invite_code.revoke",
        target_type="invite_code",
        target_id=str(invite.id),
        # [PRD60 PA-20] before_data 保留被撤销邀请码原本授予的 capability 组合，
        # 使审计可还原「该邀请码本应授予哪些权限」（revoke 不修改 capabilities 列）
        before_data={"status": "unused", "capabilities": invite.capabilities},
        after_data={
            "status": invite.status,
            "plan_code": invite.plan_code,
            "grant_months": invite.grant_months,
            "capabilities": invite.capabilities,
        },
    )

    await db.commit()

    return InviteCodeListItem(
        id=invite.id,
        status=invite.status,
        grant_days=invite.grant_days,
        plan_code=invite.plan_code,
        monitor_limit=invite.monitor_limit,
        grant_months=invite.grant_months,
        note=invite.note,
        created_by=invite.created_by,
        created_at=invite.created_at,
        used_by=invite.used_by,
        used_at=invite.used_at,
        usage_type=invite.usage_type,
        # [PRD60 PA-20] 回显 capability 组合（旧模式为 None）
        capabilities=invite.capabilities,
    )


@router.get("/members")
async def get_members(
    limit: int = Query(default=50, ge=1, le=200, description="分页大小"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> dict:
    """查询订阅账户列表（含订阅状态/到期时间/剩余天数/续期次数/capabilities；MemberListItem 为 V1.6 遗留命名）。

    [Gate2 PRD60 PA-01] capabilities 字段包含三类独立权限状态（per-capability 独立 expires_at）。
    旧用户无 user_capabilities 行时为空 dict（fallback 到 plan_code 推断）。

    Args:
        limit: 分页大小
        offset: 分页偏移
        db: 异步数据库会话

    Returns:
        {items: MemberListItem[], total: int, limit: int, offset: int}
    """
    members, total = await list_subscribers_with_capabilities(db=db, limit=limit, offset=offset)
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


@router.get("/worker-heartbeats", response_model=WorkerHeartbeatListResponse)
async def get_worker_heartbeats(
    status: str | None = Query(default=None, description="状态筛选：running/idle/stopped"),
    worker_name: str | None = Query(default=None, description="Worker 名称筛选"),
    limit: int = Query(default=100, ge=1, le=200, description="分页大小"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> WorkerHeartbeatListResponse:
    """查询 Worker 心跳记录（admin 只读）。

    返回 worker_heartbeats 表的 raw 记录，附加后端计算的
    heartbeat_age_seconds 和 health_state。health_state 阈值：
    - fresh:   status=running 且 age < 120s
    - stale:   status=running 且 120s ≤ age < 600s
    - stopped: status=stopped 或 age ≥ 600s

    阈值常量定义在 app.schemas.worker_heartbeat，与
    system_overview_service.WORKER_HEALTH_WINDOW 和
    worker.STALE_HEARTBEAT_THRESHOLD_SECONDS 保持一致。
    """
    filters = []
    if status:
        filters.append(WorkerHeartbeat.status == status)
    if worker_name:
        filters.append(WorkerHeartbeat.worker_name == worker_name)

    count_stmt = select(func.count()).select_from(WorkerHeartbeat).where(*filters)
    total = await db.scalar(count_stmt) or 0

    stmt = (
        select(WorkerHeartbeat)
        .where(*filters)
        .order_by(WorkerHeartbeat.worker_name.asc(), WorkerHeartbeat.started_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = list(result.scalars().all())

    now = datetime.now(UTC)
    items: list[WorkerHeartbeatItem] = []
    for r in rows:
        age = int((now - r.heartbeat_at).total_seconds())
        health = classify_health_state(r.status, age)
        items.append(
            WorkerHeartbeatItem(
                worker_name=r.worker_name,
                instance_id=r.instance_id,
                started_at=r.started_at,
                heartbeat_at=r.heartbeat_at,
                status=r.status,
                stopped_at=getattr(r, "stopped_at", None),  # Gate4: 停止时间（None=运行中或历史无此字段）
                current_job_id=r.current_job_id,
                build_sha=r.build_sha,
                metadata_json=r.metadata_json,
                updated_at=r.updated_at,
                heartbeat_age_seconds=age,
                health_state=health,
            )
        )

    return WorkerHeartbeatListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/message-deliveries", response_model=list[MessageDeliveryResponse])
async def get_message_deliveries(
    status: str | None = Query(default=None, description="状态筛选：pending/success/failed/retrying"),
    limit: int = Query(default=50, ge=1, le=200, description="分页大小"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> list[MessageDeliveryResponse]:
    """查询消息投递记录（admin）。

    返回 message_deliveries 表记录，支持按状态筛选和分页。
    复用 MessageDeliveryResponse schema，包含渠道类型与展示名称。
    """
    rows = await list_message_deliveries(db=db, status=status, limit=limit, offset=offset)
    return [MessageDeliveryResponse.model_validate(r) for r in rows]


@router.post("/message-deliveries/{delivery_id}/retry", response_model=MessageDeliveryResponse)
async def retry_message_delivery(
    delivery_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> MessageDeliveryResponse:
    """立即重试指定消息投递记录。

    直接更新已有 MessageDelivery 记录并重新调用 adapter，
    不创建新记录，不破坏 deliver_message 的幂等语义。
    """
    try:
        delivery = await retry_delivery(db=db, delivery_id=delivery_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    return MessageDeliveryResponse.model_validate(delivery)


@router.get("/system-overview", response_model=SystemOverviewResponse)
async def get_system_overview_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> SystemOverviewResponse:
    """系统概览 - 管理员仪表盘数据。

    返回活跃用户、监控标的、评估统计、服务健康、市场阶段、监控运行时、盘后流水线等数据。
    业务逻辑由 system_overview_service.get_system_overview 提供，路由层仅做权限校验与转发。

    Args:
        db: 异步数据库会话
        current_user: 当前管理员用户（由 require_roles 注入）

    Returns:
        系统概览响应（17 个字段：12 基础 + 5 新增）
    """
    return SystemOverviewResponse(**await get_system_overview(db))


# ============================================================
# 用户账户管理端点
# ============================================================


async def _get_or_create_role(db: AsyncSession, name: str) -> Role:
    """按名称查询角色，不存在则创建。"""
    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _fetch_user_or_404(db: AsyncSession, user_id: UUID) -> User:
    """按 ID 查询用户，不存在则抛 404。"""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户不存在: {user_id}",
        )
    return user


async def _get_user_role_names(db: AsyncSession, user_id: UUID) -> list[str]:
    """查询用户的所有角色名。"""
    role_stmt = (
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    role_result = await db.execute(role_stmt)
    return [row[0] for row in role_result.all()]


@router.post("/users/{user_id}/disable", response_model=UserResponse)
async def disable_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> UserResponse:
    """禁用用户账户（status -> disabled）。"""
    user = await _fetch_user_or_404(db, user_id)
    old_status = user.status
    user.status = "disabled"
    user.updated_at = datetime.now(UTC)
    await db.flush()

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="user.disable",
        target_type="user",
        target_id=str(user.id),
        before_data={"status": old_status},
        after_data={"status": user.status},
    )
    await db.commit()

    roles = await _get_user_role_names(db, user.id)
    return UserResponse(
        id=user.id,
        email=user.email,
        status=user.status,
        timezone=user.timezone,
        roles=roles,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.post("/users/{user_id}/enable", response_model=UserResponse)
async def enable_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> UserResponse:
    """启用用户账户（status -> active）。"""
    user = await _fetch_user_or_404(db, user_id)
    old_status = user.status
    user.status = "active"
    user.updated_at = datetime.now(UTC)
    await db.flush()

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="user.enable",
        target_type="user",
        target_id=str(user.id),
        before_data={"status": old_status},
        after_data={"status": user.status},
    )
    await db.commit()

    roles = await _get_user_role_names(db, user.id)
    return UserResponse(
        id=user.id,
        email=user.email,
        status=user.status,
        timezone=user.timezone,
        roles=roles,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.post(
    "/users/{user_id}/reset-password",
    response_model=ResetPasswordResponse,
    deprecated=True,
    description="[DEPRECATED] 重置用户密码链路尚未实现。为避免“仅写审计却向管理员显示已重置”的误导，本端点 fail-closed 返回 501；前端不得展示重置入口。",
)
async def reset_user_password(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> ResetPasswordResponse:
    """管理员重置用户密码（当前仅记录审计日志，重置链路后续实现）。

    [PRD §8.4.8] 未真实实现前：
    - 前端必须隐藏该按钮；
    - 后端接口标记 deprecated；
    - 不得只写审计却向管理员显示“已重置”。
    因此本端点直接 fail-closed，不写“成功”审计，也不伪装已重置。
    """
    await _fetch_user_or_404(db, user_id)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="密码重置能力尚未实现，该入口已废弃，请勿使用",
    )


@router.post("/users/{user_id}/change-role", response_model=UserResponse)
async def change_user_role(
    user_id: UUID,
    payload: ChangeRoleRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> UserResponse:
    """修改用户角色。

    规则：
    - 目标角色为 admin 时，移除其他角色并添加 admin，同时撤销其 subscription
      （管理员无套餐无 subscription）
    - 目标角色为 member 时，移除 admin 角色并添加 member
    """
    if payload.role not in ("admin", "member"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="role 必须是 admin 或 member",
        )

    user = await _fetch_user_or_404(db, user_id)
    old_roles = await _get_user_role_names(db, user.id)

    # 删除现有角色关联
    await db.execute(
        delete(UserRole).where(UserRole.user_id == user.id)
    )
    # 重新按目标角色设置
    if payload.role == "admin":
        admin_role = await _get_or_create_role(db, "admin")
        db.add(UserRole(user_id=user.id, role_id=admin_role.id))
        # 管理员不绑定 subscription，撤销现有订阅
        sub_result = await db.execute(
            select(Subscription).where(Subscription.user_id == user.id)
        )
        sub = sub_result.scalar_one_or_none()
        if sub is not None:
            sub.status = "revoked"
            sub.updated_at = datetime.now(UTC)
    else:
        member_role = await _get_or_create_role(db, "member")
        db.add(UserRole(user_id=user.id, role_id=member_role.id))

    await db.flush()
    new_roles = await _get_user_role_names(db, user.id)

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="user.change_role",
        target_type="user",
        target_id=str(user.id),
        before_data={"roles": old_roles},
        after_data={"roles": new_roles},
    )
    await db.commit()

    return UserResponse(
        id=user.id,
        email=user.email,
        status=user.status,
        timezone=user.timezone,
        roles=new_roles,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


# ============================================================
# 用户订阅管理端点
# ============================================================


@router.post("/users/{user_id}/grant-subscription", response_model=SubscriptionResponse)
async def grant_subscription(
    user_id: UUID,
    payload: GrantSubscriptionRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> SubscriptionResponse:
    """管理员授予用户订阅。"""
    try:
        subscription = await grant_subscription_to_user(
            db=db,
            user_id=user_id,
            plan_code=payload.plan_code,
            grant_months=payload.grant_months,
            actor_user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="subscription.grant",
        target_type="subscription",
        target_id=str(subscription.user_id),
        after_data={
            "plan_code": subscription.plan_code,
            "grant_months": payload.grant_months,
            "expires_at": subscription.expires_at.isoformat(),
        },
    )
    await db.commit()

    return SubscriptionResponse.model_validate(subscription)


@router.post("/users/{user_id}/renew-subscription", response_model=SubscriptionRenewResponse)
async def renew_subscription_endpoint(
    user_id: UUID,
    payload: RenewSubscriptionRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> SubscriptionRenewResponse:
    """管理员为用户续期订阅。"""
    try:
        subscription, old_expires_at, new_expires_at = await renew_subscription(
            db=db,
            user_id=user_id,
            grant_months=payload.grant_months,
            actor_user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="subscription.renew",
        target_type="subscription",
        target_id=str(subscription.user_id),
        before_data={"expires_at": old_expires_at.isoformat()},
        after_data={"expires_at": new_expires_at.isoformat()},
    )
    await db.commit()

    return SubscriptionRenewResponse(
        id=subscription.id,
        user_id=subscription.user_id,
        plan_code=subscription.plan_code,
        status=subscription.status,
        starts_at=subscription.starts_at,
        expires_at=new_expires_at,
        old_expires_at=old_expires_at,
        new_expires_at=new_expires_at,
        entitlement_snapshot=subscription.entitlement_snapshot,
        source=subscription.source,
        created_by=subscription.created_by,
        created_at=subscription.created_at,
        updated_at=subscription.updated_at,
    )


@router.post("/users/{user_id}/revoke-subscription", response_model=SubscriptionResponse)
async def revoke_subscription_endpoint(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> SubscriptionResponse:
    """管理员撤销用户订阅。"""
    try:
        subscription = await revoke_subscription(
            db=db,
            user_id=user_id,
            actor_user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="subscription.revoke",
        target_type="subscription",
        target_id=str(subscription.user_id),
        before_data={"status": "active"},
        after_data={"status": subscription.status},
    )
    await db.commit()

    return SubscriptionResponse.model_validate(subscription)


@router.post("/users/{user_id}/change-plan", response_model=SubscriptionResponse)
async def change_subscription_plan_endpoint(
    user_id: UUID,
    payload: ChangePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> SubscriptionResponse:
    """管理员修改用户套餐（无 subscription 时创建，有时更新并续期）。"""
    try:
        subscription = await change_subscription_plan(
            db=db,
            user_id=user_id,
            plan_code=payload.plan_code,
            grant_months=payload.grant_months,
            actor_user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="subscription.change_plan",
        target_type="subscription",
        target_id=str(subscription.user_id),
        after_data={
            "plan_code": subscription.plan_code,
            "grant_months": payload.grant_months,
            "expires_at": subscription.expires_at.isoformat(),
        },
    )
    await db.commit()

    return SubscriptionResponse.model_validate(subscription)


# ============================================================
# [Gate2 PRD60 PA-20] Capability 管理端点（管理员直接授予/撤销/查看）
# ============================================================


@router.get(
    "/users/{user_id}/capabilities",
    response_model=UserCapabilitiesResponse,
)
async def get_user_capabilities_endpoint(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> UserCapabilitiesResponse:
    """[Gate2 PRD60] 查询用户 capabilities（三类独立权限状态）。

    返回 per-capability 独立的 active/expires_at/watchlist_limit。
    旧用户无 user_capabilities 行时返回空 dict（fallback 到 plan_code 推断）。
    """
    # 校验用户存在
    await _fetch_user_or_404(db, user_id)
    capabilities = await get_user_capabilities(db, user_id)
    return UserCapabilitiesResponse(user_id=user_id, capabilities=capabilities)


@router.post(
    "/users/{user_id}/capabilities",
    response_model=UserCapabilitiesResponse,
)
async def grant_capability_endpoint(
    user_id: UUID,
    payload: GrantCapabilityRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> UserCapabilitiesResponse:
    """[Gate2 PRD60 PA-20] 管理员直接授予/修改用户 capability。

    行为：
    - 已有该 capability：取较晚的 expires_at（不降权），如提供 watchlist_limit 则更新
    - 无该 capability：新建行，source='admin_grant'，granted_by=管理员 ID
    - self_selection 必须提供 watchlist_limit（PA-02）
    - expires_at 按 months × 30 天计算（PA-03，30 天周期）

    旧 plan_code fallback 仅兼容无 cap 行用户，不覆盖已有独立授权。
    """
    # 校验用户存在
    await _fetch_user_or_404(db, user_id)

    # [权限模型 V2 PV2-B09] 复用现有请求链 request_id（不伪造随机值）
    request_id = request.headers.get("x-request-id")

    try:
        mutation = await grant_capability_to_user(
            db=db,
            user_id=user_id,
            capability=payload.capability,
            months=payload.months,
            watchlist_limit=payload.watchlist_limit,
            actor_user_id=current_user.id,
            reason=payload.reason,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    # [权限模型 V2 PV2-B05] 同事务结构化审计：target_id="{user_id}:{capability}"，
    # action 依据真实 mutation_type 生成（capability.grant/extend/
    # extend_and_quota_change/regrant），不得把全部授权记成同一 action；
    # before/after 来自 mutation 快照，含 granted_by/reason/mutation_type；
    # 首次物化时 after 含 materialized_capabilities（未物化为空列表）
    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action=f"capability.{mutation.mutation_type}",
        target_type="user_capability",
        target_id=f"{user_id}:{payload.capability}",
        before_data=mutation.before,
        after_data={
            **mutation.after,
            "granted_by": str(current_user.id),
            "actor": str(current_user.id),
            "reason": mutation.after.get("reason") or "admin_manual_grant",
            "mutation_type": mutation.mutation_type,
            "materialized_capabilities": mutation.materialized_capabilities,
        },
        request_id=request_id,
    )

    # 返回该用户所有 capabilities（含刚授予的）
    capabilities = await get_user_capabilities(db, user_id)
    await db.commit()

    return UserCapabilitiesResponse(user_id=user_id, capabilities=capabilities)


@router.delete(
    "/users/{user_id}/capabilities/{capability}",
    response_model=UserCapabilitiesResponse,
)
async def revoke_capability_endpoint(
    user_id: UUID,
    capability: str,
    request: Request,
    reason: str | None = Query(
        default=None,
        max_length=500,
        description="撤销原因（审计用，可选；去空白，空转 None）",
    ),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> UserCapabilitiesResponse:
    """[Gate2 PRD60 PA-20] 管理员撤销用户 capability（tombstone，非硬删除）。

    行为：[权限模型 V2 PV2-B04] 不硬删除，采用 admin_revoke tombstone；
    保留原 granted_by；revoked_by 与 reason 仅写入审计。
    可选 reason query 参数：去空白、空字符串转 None、限长 500（PV2-B07）。
    """
    # 校验用户存在
    await _fetch_user_or_404(db, user_id)

    # 校验 capability 合法性
    valid_caps = {"self_selection", "market_data", "research_replay"}
    if capability not in valid_caps:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效 capability: {capability}，允许: {valid_caps}",
        )

    # [权限模型 V2 PV2-B07] query reason 规范化：去空白、空转 None（query 不走 Pydantic validator）
    if reason is not None:
        trimmed = reason.strip()
        reason = trimmed if trimmed else None

    # [权限模型 V2 PV2-B09] 复用现有请求链 request_id（不伪造随机值）
    request_id = request.headers.get("x-request-id")

    try:
        mutation = await revoke_capability_from_user(
            db=db,
            user_id=user_id,
            capability=capability,
            revoked_by=current_user.id,
            reason=reason,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    # [权限模型 V2 PV2-B04/B05] 同事务结构化审计：撤销不覆盖 granted_by，
    # revoked_by 仅写入 after 快照；action=capability.revoke；target_id="{user_id}:{capability}"；
    # 首次物化时 after 含 materialized_capabilities（未物化为空列表）
    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="capability.revoke",
        target_type="user_capability",
        target_id=f"{user_id}:{capability}",
        before_data=mutation.before,
        after_data={
            **mutation.after,
            "revoked_by": str(current_user.id),
            "actor": str(current_user.id),
            "reason": mutation.after.get("reason") or "admin_manual_revoke",
            "mutation_type": mutation.mutation_type,
            "materialized_capabilities": mutation.materialized_capabilities,
        },
        request_id=request_id,
    )

    # 返回该用户剩余 capabilities
    capabilities = await get_user_capabilities(db, user_id)
    await db.commit()

    return UserCapabilitiesResponse(user_id=user_id, capabilities=capabilities)


@router.patch(
    "/users/{user_id}/capabilities/self_selection/quota",
    response_model=UserCapabilitiesResponse,
)
async def change_self_selection_quota_endpoint(
    user_id: UUID,
    payload: ChangeSelfSelectionQuotaRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> UserCapabilitiesResponse:
    """[权限模型 V2 PV2-B05] 独立调整 self_selection 额度（mutation_type=quota_change）。

    只修改 watchlist_limit，不改变 expires_at；revoked 状态不得通过改额度恢复。
    审计 action 恒为 capability.quota_change。
    """
    await _fetch_user_or_404(db, user_id)

    request_id = request.headers.get("x-request-id")

    try:
        mutation = await change_self_selection_quota(
            db=db,
            user_id=user_id,
            new_watchlist_limit=payload.watchlist_limit,
            actor_user_id=current_user.id,
            reason=payload.reason,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="capability.quota_change",
        target_type="user_capability",
        target_id=f"{user_id}:self_selection",
        before_data=mutation.before,
        after_data={
            **mutation.after,
            "actor": str(current_user.id),
            "reason": mutation.after.get("reason") or "admin_manual_quota_change",
            "mutation_type": mutation.mutation_type,
            "materialized_capabilities": mutation.materialized_capabilities,
        },
        request_id=request_id,
    )

    capabilities = await get_user_capabilities(db, user_id)
    await db.commit()

    return UserCapabilitiesResponse(user_id=user_id, capabilities=capabilities)


# ============================================================
# /admin/users 用户管理端点（V1.6.4）
# ============================================================


@router.get("/users", response_model=UserListResponse)
async def list_users(
    limit: int = Query(default=50, ge=1, le=200, description="分页大小"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> UserListResponse:
    """查询用户列表（分页）。

    Args:
        limit: 分页大小
        offset: 分页偏移
        db: 异步数据库会话
        current_user: 当前管理员用户

    Returns:
        用户列表分页响应
    """
    count_stmt = select(func.count()).select_from(User)
    total = await db.scalar(count_stmt) or 0

    stmt = (
        select(User)
        .order_by(User.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(stmt)
    users = list(result.scalars().all())

    items: list[UserResponse] = []
    for user in users:
        roles = await _get_user_role_names(db, user.id)
        # [权限模型 V2] 复用 resolve_effective_access 唯一解析，不在管理员 service 重新推导
        from app.services.effective_access_service import (
            _ensure_aware,
            capabilities_to_serializable,
            resolve_effective_access,
        )
        user_with_roles = user
        # 挂载 _roles 供 _get_user_roles 读取
        user_with_roles._roles = roles  # type: ignore[attr-defined]
        profile = await resolve_effective_access(db, user_with_roles)

        active_expiries = [
            aware
            for cap in profile.capabilities.values()
            if cap.active and cap.expires_at is not None
            for aware in [_ensure_aware(cap.expires_at)]
            if aware is not None
        ]
        nearest_expires = min(active_expiries) if active_expiries else None

        items.append(
            UserResponse(
                id=user.id,
                email=user.email,
                status=user.status,
                timezone=user.timezone,
                roles=roles,
                created_at=user.created_at,
                updated_at=user.updated_at,
                capabilities=capabilities_to_serializable(profile.capabilities),
                active_capability_keys=profile.active_capability_keys,
                has_any_access=profile.has_any_access,
                default_route=profile.default_route,
                capability_source=profile.capability_source,
                diagnostics=profile.diagnostics,
                nearest_capability_expires_at=nearest_expires,
                legacy_fallback=profile.capability_source == "legacy_plan_fallback",
                subscription_summary=profile.subscription_summary,
            )
        )

    # [PRD §8.4] 普通读取不写审计：GET /users 列表查询仅只读，移除原 user.list 审计写入
    return UserListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> UserResponse:
    """查询用户详情。

    Args:
        user_id: 用户 ID
        db: 异步数据库会话
        current_user: 当前管理员用户

    Returns:
        用户信息响应
    """
    user = await _fetch_user_or_404(db, user_id)
    roles = await _get_user_role_names(db, user.id)

    # [PRD §8.4] 普通读取不写审计：GET /users/{user_id} 仅查询详情，移除原 user.read 审计写入，
    # 避免只读操作污染审计记录（审计仅记录真实的写操作：授予/撤销/禁用/启用/邀请码等）
    return UserResponse(
        id=user.id,
        email=user.email,
        status=user.status,
        timezone=user.timezone,
        roles=roles,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.get("/users/{user_id}/access-profile", response_model=AdminAccessProfileResponse)
async def get_user_access_profile(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AdminAccessProfileResponse:
    """[权限模型 V2 PV2-B06] 返回用户完整 access-profile（account / effective_access / subscription_summary / explicit_capability_records）。

    商业状态由 resolve_commercial_status 解析（受限 status + 诊断 reason，异常周期 fail-closed）。
    稳定错误：用户不存在 404；权限解析失败 500 + permission_resolution_failed（不暴露内部异常）。
    """
    from app.services.effective_access_service import (
        _ensure_aware,
        capabilities_to_serializable,
        resolve_effective_access,
    )

    user = await _fetch_user_or_404(db, user_id)
    roles = await _get_user_role_names(db, user.id)

    # effective_access
    user._roles = roles  # type: ignore[attr-defined]
    try:
        profile = await resolve_effective_access(db, user)
    except Exception:  # noqa: BLE001
        logger.exception("get_user_access_profile resolve failed user_id=%s", user.id)
        raise HTTPException(
            status_code=500,
            detail={"code": "permission_resolution_failed"},
        ) from None
    capabilities = capabilities_to_serializable(profile.capabilities)
    active_expiries = [
        _ensure_aware(cap.expires_at)
        for cap in profile.capabilities.values()
        if cap.active and cap.expires_at is not None
    ]
    nearest_expires = min(e for e in active_expiries if e is not None) if active_expiries else None

    # subscription_summary（商业展示，不参与判权）
    # [权限模型 V2 PV2-B06] 用 resolve_commercial_status 解析受限状态 + 诊断 reason
    from app.models.subscription import Subscription
    from app.services.subscription_service import resolve_commercial_status

    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalars().first()
    commercial = resolve_commercial_status(sub)
    plan_display = None
    if sub is not None and sub.plan_code:
        from app.services.plan_service import get_plan

        plan = await get_plan(db, sub.plan_code)
        plan_display = plan.display_name if plan else None

    sub_starts = _ensure_aware(sub.starts_at) if sub and sub.starts_at else None
    sub_expires = _ensure_aware(sub.expires_at) if sub and sub.expires_at else None

    # explicit_capability_records（规范化 state：active/expired/revoked）
    from app.models.user_capability import UserCapability

    cap_rows = (
        await db.execute(select(UserCapability).where(UserCapability.user_id == user.id))
    ).scalars().all()
    now = datetime.now(UTC)
    explicit_records = []
    for r in cap_rows:
        exp = _ensure_aware(r.expires_at)
        if r.source == "admin_revoke":
            state = "revoked"
        elif exp is not None and exp > now:
            state = "active"
        else:
            state = "expired"
        # [权限模型 V2 PV2-B06] 直接传 datetime/UUID，由 Pydantic 序列化为 ISO，
        # 禁止手工 isoformat
        explicit_records.append(
            ExplicitCapabilityRecord(
                capability=r.capability,
                state=state,
                granted_at=_ensure_aware(r.granted_at),
                expires_at=exp,
                watchlist_limit=r.watchlist_limit,
                source=r.source,
                granted_by=r.granted_by,
            )
        )

    return AdminAccessProfileResponse(
        account=AdminAccountInfo(
            id=user.id,
            email=user.email,
            account_status=user.status,
            roles=roles,
            created_at=_ensure_aware(user.created_at),
            last_login_at=_ensure_aware(getattr(user, "last_login_at", None)),
        ),
        effective_access=EffectiveAccessInfo(
            capabilities=capabilities,
            active_capability_keys=profile.active_capability_keys,
            has_any_access=profile.has_any_access,
            default_route=profile.default_route,
            capability_source=profile.capability_source,
            nearest_capability_expires_at=nearest_expires,
            legacy_fallback=profile.capability_source == "legacy_plan_fallback",
            diagnostics=profile.diagnostics,
        ),
        subscription_summary=SubscriptionSummaryInfo(
            status=commercial.status,
            reason=commercial.reason,
            plan_code=sub.plan_code if sub else None,
            plan_display_name=plan_display,
            starts_at=sub_starts,
            expires_at=sub_expires,
            source=getattr(sub, "source", None) if sub else None,
            entitlement_snapshot=getattr(sub, "entitlement_snapshot", None) if sub else None,
        ),
        explicit_capability_records=explicit_records,
    )


# ============================================================
# /admin/users/{user_id}/subscriptions 订阅管理端点（V1.6.4）
# ============================================================


@router.post("/users/{user_id}/subscriptions/grant", response_model=SubscriptionResponse)
async def grant_user_subscription(
    user_id: UUID,
    payload: GrantSubscriptionRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> SubscriptionResponse:
    """管理员授予用户订阅。"""
    try:
        subscription = await grant_subscription_to_user(
            db=db,
            user_id=user_id,
            plan_code=payload.plan_code,
            grant_months=payload.grant_months,
            actor_user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="subscription.grant",
        target_type="subscription",
        target_id=str(subscription.user_id),
        after_data={
            "plan_code": subscription.plan_code,
            "grant_months": payload.grant_months,
            "expires_at": subscription.expires_at.isoformat(),
        },
    )
    await db.commit()

    return SubscriptionResponse.model_validate(subscription)


@router.post("/users/{user_id}/subscriptions/renew", response_model=SubscriptionRenewResponse)
async def renew_user_subscription(
    user_id: UUID,
    payload: RenewSubscriptionRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> SubscriptionRenewResponse:
    """管理员为用户续期订阅。"""
    try:
        subscription, old_expires_at, new_expires_at = await renew_subscription(
            db=db,
            user_id=user_id,
            grant_months=payload.grant_months,
            actor_user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="subscription.renew",
        target_type="subscription",
        target_id=str(subscription.user_id),
        before_data={"expires_at": old_expires_at.isoformat()},
        after_data={"expires_at": new_expires_at.isoformat()},
    )
    await db.commit()

    return SubscriptionRenewResponse(
        id=subscription.id,
        user_id=subscription.user_id,
        plan_code=subscription.plan_code,
        status=subscription.status,
        starts_at=subscription.starts_at,
        expires_at=new_expires_at,
        old_expires_at=old_expires_at,
        new_expires_at=new_expires_at,
        entitlement_snapshot=subscription.entitlement_snapshot,
        source=subscription.source,
        created_by=subscription.created_by,
        created_at=subscription.created_at,
        updated_at=subscription.updated_at,
    )


@router.post("/users/{user_id}/subscriptions/revoke", response_model=SubscriptionResponse)
async def revoke_user_subscription(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> SubscriptionResponse:
    """管理员撤销用户订阅。"""
    try:
        subscription = await revoke_subscription(
            db=db,
            user_id=user_id,
            actor_user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="subscription.revoke",
        target_type="subscription",
        target_id=str(subscription.user_id),
        before_data={"status": "active"},
        after_data={"status": subscription.status},
    )
    await db.commit()

    return SubscriptionResponse.model_validate(subscription)


@router.post("/users/{user_id}/subscriptions/change-plan", response_model=SubscriptionResponse)
async def change_user_subscription_plan(
    user_id: UUID,
    payload: ChangePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> SubscriptionResponse:
    """管理员修改用户套餐（无 subscription 时创建，有时更新并续期）。"""
    try:
        subscription = await change_subscription_plan(
            db=db,
            user_id=user_id,
            plan_code=payload.plan_code,
            grant_months=payload.grant_months,
            actor_user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="subscription.change_plan",
        target_type="subscription",
        target_id=str(subscription.user_id),
        after_data={
            "plan_code": subscription.plan_code,
            "grant_months": payload.grant_months,
            "expires_at": subscription.expires_at.isoformat(),
        },
    )
    await db.commit()

    return SubscriptionResponse.model_validate(subscription)


# ============================================================
# 审计日志查询端点
# ============================================================


@router.get("/audit-logs", response_model=AuditLogListResponse)
async def get_audit_logs(
    target_user_id: UUID | None = Query(default=None, description="按目标用户 ID 筛选"),
    action: str | None = Query(default=None, description="按 action 筛选"),
    limit: int = Query(default=50, ge=1, le=200, description="分页大小"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> AuditLogListResponse:
    """查询管理员审计日志。

    支持按 target_user_id（target_id）和 action 筛选。
    """
    target_id = str(target_user_id) if target_user_id else None
    items = await query_audit_logs(
        db=db,
        target_id=target_id,
        action=action,
        limit=limit,
        offset=offset,
    )

    # 总数查询（复用相同筛选条件）
    count_stmt = select(func.count()).select_from(AccessAuditLog)
    filters = []
    if target_id is not None:
        filters.append(AccessAuditLog.target_id == target_id)
    if action is not None:
        filters.append(AccessAuditLog.action == action)
    if filters:
        count_stmt = count_stmt.where(*filters)
    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()

    return AuditLogListResponse(
        items=[AuditLogListItem.model_validate(log) for log in items],
        total=total,
        limit=limit,
        offset=offset,
    )


logger = logging.getLogger(__name__)


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths: list[str] = []
    for r in router.routes:
        path = getattr(r, "path", None)
        if isinstance(path, str):
            paths.append(path)
    print(f"router.routes={paths}")
    assert "/admin/invite-codes" in paths
    assert "/admin/members" in paths
    assert "/admin/system-overview" in paths
    print("OK")
