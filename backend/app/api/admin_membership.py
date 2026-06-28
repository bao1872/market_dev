"""管理员 API 路由 - 会员管理 + 系统概览。

端点：
- POST /admin/invite-codes: 生成邀请码（单个/批量，绑定 plan_code/grant_months）
- GET /admin/invite-codes: 查询邀请码列表（支持状态筛选 + 分页）
- POST /admin/invite-codes/{id}/revoke: 作废邀请码
- GET /admin/members: 查询会员账户列表（含会员状态/到期时间/剩余天数/续期次数）
- GET /admin/members/{user_id}/redemptions: 查询用户兑换记录
- GET /admin/system-overview: 系统概览（活跃用户/监控标的/评估统计/服务健康）

权限：
- 所有端点需要 admin 角色（RBAC）

套餐权限（plan_contract）：
- 生成邀请码时接收 plan_code/grant_months，从 PLAN_CONTRACTS 读取 monitor_limit 快照
- 默认 plan_code=observe_20、grant_months=1（保持向后兼容）
"""

from __future__ import annotations

import math
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.user import User
from app.models.notification import MessageDelivery, NotificationChannel, NotificationMessage
from app.schemas.membership import (
    InviteCodeCreate,
    InviteCodeListItem,
    InviteCodeResponse,
    InviteRedemptionResponse,
    MemberListItem,
)
from app.schemas.notification import MessageDeliveryResponse
from app.schemas.scheduler_job_run import (
    SchedulerJobRunItem,
    SchedulerJobRunListResponse,
)
from app.schemas.system_overview import SystemOverviewResponse
from app.services.membership_service import (
    generate_invite_codes,
    get_redemptions_by_user,
    list_invite_codes,
    list_members,
    revoke_invite_code,
)
from app.services.notification_service import list_message_deliveries, retry_delivery
from app.services.system_overview_service import get_system_overview

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
    """生成邀请码（单个/批量，绑定 plan_code/grant_months）。

    从 PLAN_CONTRACTS 读取 monitor_limit 快照写入邀请码。明文仅在生成时返回，后续不可获取。
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


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert "/admin/invite-codes" in paths
    assert "/admin/members" in paths
    assert "/admin/system-overview" in paths
    print("OK")
