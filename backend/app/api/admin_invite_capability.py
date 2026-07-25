"""V2.1 邀请码能力配置管理路由 - 管理员创建/列表/撤销。

PRD §6 邀请码能力配置 + §8.1 invite_codes 状态推导。

端点：
- POST /admin/v2/invite-codes: 创建邀请码（带能力配置）
- GET /admin/v2/invite-codes: 列表查询（含能力配置 + 状态推导）
- POST /admin/v2/invite-codes/{id}/revoke: 撤销邀请码

权限：所有端点需要 admin 角色（RBAC）

与 V1 /admin/invite-codes 的区别：
- 使用 duration_months（替代 grant_months）
- 使用 capabilities 列表（替代 plan_code + monitor_limit）
- 状态由 revoked_at/redeemed_at 推导（不依赖旧 status 字段）
- 不创建 Subscription，仅创建 InviteCodeCapability（兑换时创建 UserCapabilityGrant）
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.models.invitation import InviteCode
from app.schemas.invite_capability import (
    InviteCodeCapabilityItem,
    InviteCodeV2CreateRequest,
    InviteCodeV2ListItem,
    InviteCodeV2ListResponse,
    InviteCodeV2Response,
)
from app.services.access_audit_service import write_audit_log
from app.services.invite_capability_service import (
    InviteCodeCapabilityInput,
    create_invite_codes_with_capabilities,
    derive_invite_code_status_v2,
    list_invite_codes_with_capabilities,
    revoke_invite_code_v2,
)

router = APIRouter(
    prefix="/admin/v2",
    tags=["admin-invite-capability-v2"],
)


def _to_capability_input(item: InviteCodeCapabilityItem) -> InviteCodeCapabilityInput:
    """转换 schema → service 输入（schema 已校验，service 再校验一次防御）。"""
    return InviteCodeCapabilityInput(
        capability_key=item.capability_key,
        limit_value=item.limit_value,
    )


def _build_response(invite: InviteCode, raw_code: str, capabilities) -> InviteCodeV2Response:
    """构造创建响应（含明文 + 能力配置）。"""
    return InviteCodeV2Response(
        id=invite.id,
        code=raw_code,
        duration_months=invite.duration_months,  # type: ignore[arg-type]
        capabilities=[
            InviteCodeCapabilityItem(
                capability_key=c.capability_key,
                limit_value=c.limit_value,
            )
            for c in capabilities
        ],
        note=invite.note,
        created_at=invite.created_at,
    )


def _build_list_item(invite: InviteCode, capabilities) -> InviteCodeV2ListItem:
    """构造列表项（不含明文，含状态推导 + 能力配置）。"""
    return InviteCodeV2ListItem(
        id=invite.id,
        status=derive_invite_code_status_v2(invite),
        duration_months=invite.duration_months,  # type: ignore[arg-type]
        capabilities=[
            InviteCodeCapabilityItem(
                capability_key=c.capability_key,
                limit_value=c.limit_value,
            )
            for c in capabilities
        ],
        note=invite.note,
        created_by=invite.created_by,  # type: ignore[arg-type]
        created_at=invite.created_at,
        redeemed_by_user_id=invite.redeemed_by_user_id,  # type: ignore[arg-type]
        redeemed_at=invite.redeemed_at,
        revoked_at=invite.revoked_at,
    )


@router.post("/invite-codes", response_model=list[InviteCodeV2Response])
async def create_invite_codes_v2(
    payload: InviteCodeV2CreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> list[InviteCodeV2Response]:
    """创建邀请码（V2.1，带能力配置）。

    Args:
        payload: 创建请求（count + duration_months + capabilities + note）
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        邀请码列表（含明文 + 能力配置）

    Raises:
        HTTPException 400: 参数非法（capability_key 重复 / duration_months 超限 等）
    """
    try:
        capabilities_input = [_to_capability_input(c) for c in payload.capabilities]
        results = await create_invite_codes_with_capabilities(
            db=db,
            count=payload.count,
            created_by=current_user.id,
            duration_months=payload.duration_months,
            capabilities=capabilities_input,
            note=payload.note,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    # 写审计日志（不含明文 code）
    for invite, _raw_code in results:
        await write_audit_log(
            db=db,
            actor_user_id=current_user.id,
            action="invite_code_v2.create",
            target_type="invite_code",
            target_id=str(invite.id),
            after_data={
                "duration_months": invite.duration_months,
                "capabilities": [
                    {"capability_key": c.capability_key, "limit_value": c.limit_value}
                    for c in payload.capabilities
                ],
                "note": invite.note,
            },
        )

    await db.commit()

    # 查询能力配置用于响应（commit 后查询确保数据已落盘）
    from sqlalchemy import select

    from app.models.capability_grant import InviteCodeCapability

    responses: list[InviteCodeV2Response] = []
    for invite, raw_code in results:
        cap_result = await db.execute(
            select(InviteCodeCapability)
            .where(InviteCodeCapability.invite_code_id == invite.id)
            .order_by(InviteCodeCapability.capability_key)
        )
        caps = list(cap_result.scalars().all())
        responses.append(_build_response(invite, raw_code, caps))
    return responses


@router.get("/invite-codes", response_model=InviteCodeV2ListResponse)
async def list_invite_codes_v2(
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="状态筛选：available/redeemed/revoked",
    ),
    limit: int = Query(default=50, ge=1, le=200, description="分页大小"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> InviteCodeV2ListResponse:
    """查询邀请码列表（V2.1，含能力配置 + 状态推导）。

    Args:
        status_filter: 状态筛选
        limit: 分页大小
        offset: 分页偏移

    Returns:
        分页列表响应
    """
    if status_filter is not None and status_filter not in ("available", "redeemed", "revoked"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="status 必须是 available/redeemed/revoked",
        )

    items, total = await list_invite_codes_with_capabilities(
        db=db,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )
    return InviteCodeV2ListResponse(
        items=[_build_list_item(inv, caps) for inv, caps in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/invite-codes/{invite_code_id}/revoke", response_model=InviteCodeV2ListItem)
async def revoke_invite_code_v2_endpoint(
    invite_code_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> InviteCodeV2ListItem:
    """撤销邀请码（V2.1，仅 available 状态可撤销）。

    Args:
        invite_code_id: 邀请码 ID

    Returns:
        更新后的邀请码列表项

    Raises:
        HTTPException 400: 邀请码不存在或状态非 available
    """
    try:
        invite = await revoke_invite_code_v2(db=db, invite_code_id=invite_code_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await write_audit_log(
        db=db,
        actor_user_id=current_user.id,
        action="invite_code_v2.revoke",
        target_type="invite_code",
        target_id=str(invite.id),
        before_data={"status": "available"},
        after_data={
            "status": "revoked",
            "revoked_at": invite.revoked_at.isoformat() if invite.revoked_at else None,
        },
    )
    await db.commit()

    # 查询能力配置用于响应
    from sqlalchemy import select

    from app.models.capability_grant import InviteCodeCapability

    cap_result = await db.execute(
        select(InviteCodeCapability)
        .where(InviteCodeCapability.invite_code_id == invite.id)
        .order_by(InviteCodeCapability.capability_key)
    )
    caps = list(cap_result.scalars().all())
    return _build_list_item(invite, caps)


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths: list[str] = []
    for r in router.routes:
        path = getattr(r, "path", None)
        if isinstance(path, str):
            paths.append(path)
    print(f"router.routes={paths}")
    assert "/admin/v2/invite-codes" in paths
    assert "/admin/v2/invite-codes/{invite_code_id}/revoke" in paths
    print("OK")
