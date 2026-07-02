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

import math
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.models.access_audit_log import AccessAuditLog
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.models.notification import MessageDelivery, NotificationChannel, NotificationMessage
from app.schemas.invitation import (
    InviteCodeCreate,
    InviteCodeListItem,
    InviteCodeResponse,
    InviteRedemptionResponse,
)
from app.schemas.notification import MessageDeliveryResponse
from app.schemas.subscription import (
    ChangePlanRequest,
    GrantSubscriptionRequest,
    MemberListItem,
    RenewSubscriptionRequest,
    SubscriptionRenewResponse,
    SubscriptionResponse,
)
from app.schemas.scheduler_job_run import (
    SchedulerJobRunItem,
    SchedulerJobRunListResponse,
)
from app.schemas.system_overview import SystemOverviewResponse
from app.schemas.user import UserResponse
from app.services.access_audit_service import query_audit_logs, write_audit_log
from app.services.subscription_service import (
    change_subscription_plan,
    generate_invite_codes,
    get_redemptions_by_user,
    grant_subscription_to_user,
    list_invite_codes,
    list_subscribers,
    renew_subscription,
    revoke_invite_code,
    revoke_subscription,
)
from app.services.notification_service import list_message_deliveries, retry_delivery
from app.services.system_overview_service import get_system_overview

router = APIRouter(
    prefix="/admin",
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
        results = await generate_invite_codes(
            db=db,
            count=payload.count,
            created_by=current_user.id,
            note=payload.note,
            plan_code=payload.plan_code,
            grant_months=payload.grant_months,
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
        before_data={"status": "unused"},
        after_data={
            "status": invite.status,
            "plan_code": invite.plan_code,
            "grant_months": invite.grant_months,
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
    members, total = await list_subscribers(db=db, limit=limit, offset=offset)
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
    return await get_system_overview(db)


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


@router.post("/users/{user_id}/reset-password", response_model=ResetPasswordResponse)
async def reset_user_password(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> ResetPasswordResponse:
    """管理员重置用户密码（当前仅记录审计日志，重置链路后续实现）。"""
    user = await _fetch_user_or_404(db, user_id)

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="user.reset_password",
        target_type="user",
        target_id=str(user.id),
        after_data={"initiated_by": str(current_user.id)},
    )
    await db.commit()

    return ResetPasswordResponse(user_id=user.id)


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
        items.append(
            UserResponse(
                id=user.id,
                email=user.email,
                status=user.status,
                timezone=user.timezone,
                roles=roles,
                created_at=user.created_at,
                updated_at=user.updated_at,
            )
        )

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="user.list",
        target_type="user",
        after_data={"count": len(items), "limit": limit, "offset": offset},
    )
    await db.commit()

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

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="user.read",
        target_type="user",
        target_id=str(user.id),
        after_data={
            "email": user.email,
            "status": user.status,
            "roles": roles,
        },
    )
    await db.commit()

    return UserResponse(
        id=user.id,
        email=user.email,
        status=user.status,
        timezone=user.timezone,
        roles=roles,
        created_at=user.created_at,
        updated_at=user.updated_at,
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


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert "/admin/invite-codes" in paths
    assert "/admin/members" in paths
    assert "/admin/system-overview" in paths
    print("OK")
