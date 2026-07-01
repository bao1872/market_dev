"""当前用户权益 API 路由 - 门户收费模式套餐权限查询。

端点：
- GET /me/entitlements: 返回当前用户套餐、监控上限、已使用、剩余名额、到期日

套餐权限规则（plans 表）：
- 管理员：返回 ADMIN_PLAN_CODE (research_50)，monitor_limit=50，绕过会员到期限制
- 普通用户：从 membership 读取 plan_code/monitor_limit，无会员记录返回 404
- used = 用户 active 自选股数量
- remaining = monitor_limit - used（不足时为 0，不返回负数）

设计说明：
- /me/entitlements 与 /me、/me/membership、/me/events/summary 并存（auth.py 中）
- 本模块仅承载权益查询端点，保持最小改动，不迁移现有 /me/* 端点
- 套餐字段从 plans 表查询（plan_service.get_plan），不再使用 plan_contract.py 字典
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.plan_codes import ADMIN_PLAN_CODE
from app.core.deps import _get_user_roles, get_current_active_user
from app.db import get_db
from app.models.subscription import Subscription
from app.models.user import User
from app.models.watchlist import UserWatchlistItem
from app.services.plan_service import get_monitor_limit as get_monitor_limit_async
from app.services.plan_service import get_plan

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
        # [PlanService] - 描述: 从 plans 表查询管理员套餐（research_50）
        admin_plan = await get_plan(db, plan_code)
        monitor_limit = admin_plan.monitor_limit
        remaining = max(0, monitor_limit - used)
        return {
            "plan_code": plan_code,
            "plan_name": admin_plan.display_name,
            "monitor_limit": monitor_limit,
            "used": used,
            "remaining": remaining,
            "expires_at": None,
        }

    # 普通用户：从 subscription 读取套餐快照
    subscription_stmt = select(Subscription).where(Subscription.user_id == current_user.id)
    subscription_result = await db.execute(subscription_stmt)
    subscription = subscription_result.scalar_one_or_none()

    if subscription is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户无会员记录",
        )

    # 优先使用 subscription 中的套餐快照；若为 NULL（旧数据），回退到 DEFAULT_PLAN_CODE
    plan_code = subscription.plan_code or "observe_20"
    # [PlanService] - 描述: monitor_limit 优先用 entitlement_snapshot，否则查 plans 表
    snapshot = subscription.entitlement_snapshot or {}
    if snapshot.get("monitor_limit") is not None:
        monitor_limit = int(snapshot["monitor_limit"])
    else:
        monitor_limit = await get_monitor_limit_async(db, plan_code)
    remaining = max(0, monitor_limit - used)
    expires_at: datetime | None = subscription.expires_at

    # [PlanService] - 描述: plan_name 从 plans 表 display_name 查询
    plan = await get_plan(db, plan_code)
    return {
        "plan_code": plan_code,
        "plan_name": plan.display_name,
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
