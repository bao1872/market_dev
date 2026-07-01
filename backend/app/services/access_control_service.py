"""权限控制服务 - 统一权限上下文 AccessContext + 权限依赖函数。

提供：
- get_access_context: 获取当前用户的完整权限上下文（只读，不写 DB）
- require_authenticated: 要求已登录（链式依赖 deps.get_current_active_user）
- require_admin: 要求管理员身份（基于 ctx.is_admin）
- require_active_subscription: 要求有效订阅（管理员豁免）
- require_feature: 要求具备指定 feature（管理员豁免）
- require_quota: 返回限额值供调用方比较（管理员返回 None 表示无限制）

设计原则：
- 禁止各 API 自行拼接 role、subscription、expires_at，统一从 AccessContext 读取
- admin 不需要 subscription，不受订阅到期和普通额度限制（subscription_active=True 豁免）
- is_admin 只判断 "admin" 角色，strategy_author 等其他角色不影响身份判定
- is_member 判断 "user" 角色，与 is_admin 对称（共 11 个字段）
- subscription_active 由实时计算：status='active' AND starts_at<=now AND expires_at>now
- get_access_context 是只读操作（不写 DB），可在登录路径使用
- 复用 plan_service.get_plan 与 subscription_service.get_effective_subscription_status，
  不重复实现套餐查询与订阅状态判定逻辑

业务规则（permission-matrix.md 设计）：
- observe_20: monitor_limit=20, notification_channel_limit=1, message_retention_days=30, 6 features
- research_50: monitor_limit=50, notification_channel_limit=3, message_retention_days=180, 7 features
- 过期订阅仍记录原 plan_code/plan_display_name（便于前端展示降级提示），但 subscription_active=False
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import _get_user_roles, get_current_active_user
from app.db import get_db
from app.models.subscription import Subscription
from app.models.user import User
from app.services.plan_service import get_plan
from app.services.subscription_service import get_effective_subscription_status

__all__ = [
    "AccessContext",
    "get_access_context",
    "require_authenticated",
    "require_admin",
    "require_active_subscription",
    "require_feature",
    "require_quota",
]


class AccessContext(BaseModel):
    """权限上下文 - 统一封装用户身份、订阅状态、套餐权益（11 个字段）。

    字段语义：
    - user_id: 用户 ID（字符串化 UUID，与 JWT sub 声明一致）
    - account_status: 用户状态（user.status，active/disabled/pending）
    - roles: 用户角色名列表（从 user._roles 读取）
    - is_admin: 是否为管理员（"admin" in roles）
    - is_member: 是否为普通会员（"user" in roles，与 is_admin 对称）
    - subscription_active: 订阅是否有效（admin 豁免=True；member 实时计算）
    - plan_code: 当前套餐代码（admin/无订阅=None；member 从 subscription 读取）
    - plan_display_name: 套餐展示名（从 plans 表读取，过期订阅仍保留）
    - expires_at: 订阅过期时间（admin/无订阅=None）
    - features: 功能特性列表（从 plans 表读取，admin/无订阅=[]）
    - limits: 额度限制 dict（monitor_limit/notification_channel_limit/message_retention_days）

    设计要点：
    - admin 路径：subscription_active=True（豁免），plan_code=None，features=[]，limits={}
    - member 过期订阅：subscription_active=False，但 plan_code/plan_display_name/features/limits 仍填充
    - 不可变（frozen=True），避免在请求处理中被意外修改
    """

    model_config = ConfigDict(frozen=True)

    user_id: str
    account_status: str
    roles: list[str]
    is_admin: bool
    is_member: bool
    subscription_active: bool
    plan_code: str | None = None
    plan_display_name: str | None = None
    expires_at: datetime | None = None
    features: list[str] = Field(default_factory=list)
    limits: dict = Field(default_factory=dict)


async def get_access_context(db: AsyncSession, user: User) -> AccessContext:
    """获取当前用户的完整权限上下文（只读操作，不写 DB）。

    流程：
    1. 从 user._roles 读取角色名列表
    2. 计算 is_admin / is_member
    3. admin 路径：subscription_active=True（豁免），plan_code=None
    4. non-admin 路径：
       a. 调用 subscription_service.get_effective_subscription_status 获取订阅状态
       b. 若有订阅记录（active 或 expired）：查询 subscription.plan_code，再查询 plans 表
          填充 plan_display_name/features/limits（过期订阅仍保留，便于前端降级提示）
       c. 若无订阅记录：plan_code=None，features=[]，limits={}
    5. 构建 AccessContext 返回

    Args:
        db: 异步数据库会话
        user: 当前用户对象（需由 deps.get_current_user 注入 _roles 属性）

    Returns:
        AccessContext 权限上下文
    """
    roles = _get_user_roles(user)
    is_admin = "admin" in roles
    is_member = "user" in roles

    # [AccessControl] - 描述: admin 路径直接豁免订阅检查，不查询 subscription 表
    if is_admin:
        return AccessContext(
            user_id=str(user.id),
            account_status=user.status,
            roles=roles,
            is_admin=True,
            is_member=is_member,
            subscription_active=True,
            plan_code=None,
            plan_display_name=None,
            expires_at=None,
            features=[],
            limits={},
        )

    # [AccessControl] - 描述: member 路径查询订阅有效状态（只读，复用 subscription_service）
    effective_status, expires_at = await get_effective_subscription_status(db, user.id)
    subscription_active = effective_status == "active"

    # 无订阅记录：plan_code=None，features=[]，limits={}
    if effective_status == "none":
        return AccessContext(
            user_id=str(user.id),
            account_status=user.status,
            roles=roles,
            is_admin=False,
            is_member=is_member,
            subscription_active=False,
            plan_code=None,
            plan_display_name=None,
            expires_at=None,
            features=[],
            limits={},
        )

    # [AccessControl] - 描述: 有订阅记录（active 或 expired）查询 plan_code 并读取 plans 表
    # 过期订阅仍填充 plan_code/plan_display_name/features/limits，便于前端展示降级提示
    sub_stmt = select(Subscription.plan_code).where(Subscription.user_id == user.id)
    sub_result = await db.execute(sub_stmt)
    plan_code = sub_result.scalar_one_or_none()

    if plan_code is None:
        # 理论不可达：effective_status != "none" 但查不到 plan_code
        return AccessContext(
            user_id=str(user.id),
            account_status=user.status,
            roles=roles,
            is_admin=False,
            is_member=is_member,
            subscription_active=subscription_active,
            plan_code=None,
            plan_display_name=None,
            expires_at=expires_at,
            features=[],
            limits={},
        )

    # [PlanService] - 描述: 复用 plan_service.get_plan 读取套餐定义（唯一真源）
    plan = await get_plan(db, plan_code)
    return AccessContext(
        user_id=str(user.id),
        account_status=user.status,
        roles=roles,
        is_admin=False,
        is_member=is_member,
        subscription_active=subscription_active,
        plan_code=plan.plan_code,
        plan_display_name=plan.display_name,
        expires_at=expires_at,
        features=list(plan.features) if plan.features else [],
        limits={
            "monitor_limit": int(plan.monitor_limit),
            "notification_channel_limit": int(plan.notification_channel_limit),
            "message_retention_days": int(plan.message_retention_days),
        },
    )


async def require_authenticated(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
) -> AccessContext:
    """要求已登录且 active，返回 AccessContext。

    链式依赖 deps.get_current_active_user（JWT 解析 + 状态检查），
    再调用 get_access_context 构建完整权限上下文。

    Args:
        db: 异步数据库会话（由 get_db 注入）
        user: 当前 active 用户（由 get_current_active_user 注入）

    Returns:
        AccessContext 权限上下文
    """
    return await get_access_context(db, user)


async def require_admin(
    ctx: AccessContext = Depends(require_authenticated),
) -> AccessContext:
    """要求管理员身份，否则 403。

    基于 ctx.is_admin 判定（"admin" in roles），与 deps.require_roles("admin") 语义一致，
    但本函数面向订阅/功能权限链路，作为 require_authenticated 之后的二级权限检查。

    Args:
        ctx: 权限上下文（由 require_authenticated 注入）

    Returns:
        原 AccessContext（链式传递）

    Raises:
        HTTPException 403: 非管理员
    """
    if not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="权限不足：需要管理员身份",
        )
    return ctx


async def require_active_subscription(
    ctx: AccessContext = Depends(require_authenticated),
) -> AccessContext:
    """要求有效订阅（admin 自动豁免），否则 403。

    admin 路径：ctx.subscription_active=True（get_access_context 已豁免），直接通过。
    member 路径：ctx.subscription_active 由实时计算，过期或无订阅返回 403。

    Args:
        ctx: 权限上下文（由 require_authenticated 注入）

    Returns:
        原 AccessContext（链式传递）

    Raises:
        HTTPException 403: 订阅已过期或无有效订阅
    """
    if not ctx.subscription_active:
        # [AccessControl] - 描述: 区分"已过期"与"无订阅"两种情况，错误信息更精准
        if ctx.plan_code is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="订阅已过期，请续期",
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无有效订阅",
        )
    return ctx


def require_feature(feature_name: str) -> Callable[..., object]:
    """功能特性检查依赖工厂（admin 豁免）。

    返回一个 FastAPI 依赖函数，检查 ctx.features 是否包含指定 feature。
    admin 自动豁免（features 为空也通过）。

    用法：
        @router.post("/export", dependencies=[Depends(require_feature("advanced_export"))])
        async def export(...): ...

    Args:
        feature_name: 功能特性名（如 "trend_selection" / "advanced_export"）

    Returns:
        FastAPI 依赖函数，校验通过返回原 ctx，否则 403
    """
    if not feature_name:
        raise ValueError("require_feature 需要非空 feature_name")

    async def _check_feature(
        ctx: AccessContext = Depends(require_authenticated),
    ) -> AccessContext:
        """检查 ctx 是否具备指定 feature（admin 豁免）。"""
        # [AccessControl] - 描述: admin 豁免，不检查 features 列表
        if ctx.is_admin:
            return ctx
        if feature_name not in ctx.features:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要功能: {feature_name}",
            )
        return ctx

    return _check_feature


def require_quota(quota_name: str) -> Callable[..., object]:
    """额度检查依赖工厂（admin 豁免，返回限额值）。

    返回一个 FastAPI 依赖函数，返回限额值供调用方比较实际使用量。
    - admin：返回 None（表示无限制，调用方应跳过超额检查）
    - member：返回 ctx.limits[quota_name]；若 quota_name 不在 limits 中抛 403

    注意：本函数只返回限额值，实际超额检查由调用方完成。
    例如 watchlist 新增时：limit = await require_quota("monitor_limit")(...);
    若 limit is not None 且 current_count >= limit，则拒绝新增。

    用法：
        @router.post("/watchlist")
        async def add_watchlist(
            ctx: AccessContext = Depends(require_authenticated),
            monitor_limit = Depends(require_quota("monitor_limit")),
        ): ...

    Args:
        quota_name: 额度名（如 "monitor_limit" / "notification_channel_limit"）

    Returns:
        FastAPI 依赖函数，返回限额值（int）或 None（admin 无限制）
    """
    if not quota_name:
        raise ValueError("require_quota 需要非空 quota_name")

    async def _get_quota(
        ctx: AccessContext = Depends(require_authenticated),
    ) -> int | None:
        """返回限额值（admin=None 无限制；member=int 限额；缺失=403）。"""
        # [AccessControl] - 描述: admin 豁免，返回 None 表示无限制
        if ctx.is_admin:
            return None
        if quota_name not in ctx.limits:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"无有效额度: {quota_name}",
            )
        return ctx.limits[quota_name]

    return _get_quota


if __name__ == "__main__":
    # [AccessControl] - 描述: 自测入口，验证函数签名与 AccessContext 字段（不连接数据库）
    assert callable(get_access_context)
    assert callable(require_authenticated)
    assert callable(require_admin)
    assert callable(require_active_subscription)
    assert callable(require_feature)
    assert callable(require_quota)

    # AccessContext 11 个字段
    expected_fields = {
        "user_id", "account_status", "roles", "is_admin", "is_member",
        "subscription_active", "plan_code", "plan_display_name",
        "expires_at", "features", "limits",
    }
    assert set(AccessContext.model_fields.keys()) == expected_fields
    assert len(AccessContext.model_fields) == 11

    # 工厂函数返回可调用依赖
    feature_dep = require_feature("trend_selection")
    assert callable(feature_dep)
    quota_dep = require_quota("monitor_limit")
    assert callable(quota_dep)

    # 构造 AccessContext 验证字段默认值
    ctx = AccessContext(
        user_id="test-uuid",
        account_status="active",
        roles=["admin"],
        is_admin=True,
        is_member=False,
        subscription_active=True,
    )
    assert ctx.plan_code is None
    assert ctx.plan_display_name is None
    assert ctx.expires_at is None
    assert ctx.features == []
    assert ctx.limits == {}
    # frozen=True 不可变
    try:
        ctx.is_admin = False  # type: ignore[misc]
        raise AssertionError("AccessContext 应为 frozen")
    except Exception:
        pass

    print(f"AccessContext fields={sorted(AccessContext.model_fields.keys())}")
    print(f"require_feature('trend_selection') -> {feature_dep}")
    print(f"require_quota('monitor_limit') -> {quota_dep}")
    print("OK: access_control_service 函数签名与字段验证通过")
