"""当前用户权益 API 路由 - 门户收费模式套餐权限查询。

端点：
- GET /me/entitlements: 返回当前用户套餐、监控上限、已使用、剩余名额、到期日

套餐权限规则（plan_contract）：
- 管理员：返回 ADMIN_PLAN_CODE (research_50)，monitor_limit=50，绕过会员到期限制
- 普通用户：从 membership 读取 plan_code/monitor_limit，无会员记录返回 404
- used = 用户 active 自选股数量
- remaining = monitor_limit - used（不足时为 0，不返回负数）

设计说明：
- /me/entitlements 与 /me、/me/membership、/me/events/summary 并存（auth.py 中）
- 本模块仅承载权益查询端点，保持最小改动，不迁移现有 /me/* 端点
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.plan_contract import (
    ADMIN_PLAN_CODE,
    PLAN_CONTRACTS,
    get_monitor_limit,
    get_plan_name,
)
from app.core.deps import get_current_active_user, _get_user_roles
from app.db import get_db
from app.models.membership import Membership
from app.models.user import User
from app.models.watchlist import UserWatchlistItem

router = APIRouter(tags=["me"])


@router.get("/me/entitlements")
async def get_my_entitlements(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """获取当前用户套餐权益 - 套餐、监控上限、已使用、剩余名额、到期日。

    管理员：返回 ADMIN_PLAN_CODE (research_50)，绕过会员到期限制。
    普通用户：从 membership 读取套餐快照，无会员记录返回 404。

    Args:
        current_user: 当前用户（由 get_current_active_user 注入）
        db: 异步数据库会话

    Returns:
        {plan_code, plan_name, monitor_limit, used, remaining, expires_at}

    Raises:
        HTTPException 404: 普通用户无会员记录
    """
    # 查询用户 active 自选股数量
    used_stmt = (
        select(func.count(UserWatchlistItem.id))
        .where(
            UserWatchlistItem.user_id == current_user.id,
            UserWatchlistItem.active.is_(True),
        )
    )
    used_result = await db.execute(used_stmt)
    used = used_result.scalar_one()

    # 管理员：返回 ADMIN_PLAN_CODE，绕过会员到期限制
    user_roles = _get_user_roles(current_user)
    if "admin" in user_roles:
        plan_code = ADMIN_PLAN_CODE
        monitor_limit = get_monitor_limit(plan_code)
        remaining = max(0, monitor_limit - used)
        return {
            "plan_code": plan_code,
            "plan_name": get_plan_name(plan_code),
            "monitor_limit": monitor_limit,
            "used": used,
            "remaining": remaining,
            "expires_at": None,
        }

    # 普通用户：从 membership 读取套餐快照
    membership_stmt = select(Membership).where(Membership.user_id == current_user.id)
    membership_result = await db.execute(membership_stmt)
    membership = membership_result.scalar_one_or_none()

    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户无会员记录",
        )

    # 优先使用 membership 中的套餐快照；若为 NULL（旧数据），回退到 DEFAULT_PLAN_CODE
    plan_code = membership.plan_code or "observe_20"
    monitor_limit = membership.monitor_limit or get_monitor_limit(plan_code)
    remaining = max(0, monitor_limit - used)
    expires_at: datetime | None = membership.expires_at

    return {
        "plan_code": plan_code,
        "plan_name": get_plan_name(plan_code),
        "monitor_limit": monitor_limit,
        "used": used,
        "remaining": remaining,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert "/me/entitlements" in paths
    print("OK")
