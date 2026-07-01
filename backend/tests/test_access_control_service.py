"""权限控制服务测试 - Phase 2 Task 2.3。

测试 AccessContext 数据类与权限依赖函数：
- get_access_context: 获取完整权限上下文（admin 豁免 / member 查询订阅+套餐）
- require_authenticated: 要求已登录（链式依赖 deps.get_current_active_user）
- require_admin: 要求管理员（基于 ctx.is_admin）
- require_active_subscription: 要求有效订阅（admin 豁免）
- require_feature: 要求功能特性（admin 豁免）
- require_quota: 返回限额值（admin 返回 None 表示无限制）

测试策略：
- 使用 conftest.py 的 db_session fixture（PostgreSQL 测试库 bz_stock_test）
- 直接创建 User + Role + UserRole + Subscription 测试数据
- 调用 get_access_context 验证 AccessContext 11 个字段
- 直接调用依赖函数（require_admin 等）验证权限逻辑与 403 异常

业务规则（来自 permission-matrix.md 设计）：
- admin 不需要 subscription，subscription_active=True（豁免），plan_code=None
- member 有效订阅：从 subscription.plan_code 查询 plans 表填充 features/limits
- member 已过期订阅：subscription_active=False，但仍记录原 plan_code/plan_display_name
- member 无订阅：subscription_active=False，plan_code=None
- is_admin 仅判断 "admin" 角色，strategy_author 等其他角色不影响
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.services.access_control_service import (
    AccessContext,
    get_access_context,
    require_active_subscription,
    require_admin,
    require_feature,
    require_quota,
)

# ============================================================
# 测试辅助函数
# ============================================================


async def _ensure_role(db: AsyncSession, name: str) -> Role:
    """确保角色存在并返回（幂等）。"""
    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _create_user_with_roles(
    db: AsyncSession, role_names: list[str], email: str | None = None
) -> User:
    """创建用户并分配指定角色，挂载 _roles 属性模拟 deps 注入行为。"""
    email = email or f"user_{uuid.uuid4().hex[:8]}@test.com"
    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash="fake-hash",
        status="active",
        timezone="Asia/Shanghai",
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    await db.flush()
    for name in role_names:
        role = await _ensure_role(db, name)
        db.add(UserRole(user_id=user.id, role_id=role.id))
    await db.flush()
    # [AccessControl] - 描述: 挂载 _roles 属性，模拟 deps._fetch_user_with_roles 的行为
    object.__setattr__(user, "_roles", list(role_names))
    return user


async def _create_subscription(
    db: AsyncSession,
    user_id: uuid.UUID,
    plan_code: str = "observe_20",
    expired: bool = False,
) -> Subscription:
    """创建订阅记录（expired=True 时 expires_at < now）。

    [Subscription] - 描述: entitlement_snapshot 必须为非空 dict（Phase 8 NOT NULL 约束），
    从 plans 表的套餐定义构造，与生产 subscription_service._build_entitlement_snapshot 语义一致。
    """
    # [Plan] - 描述: 套餐 entitlement_snapshot 预定义（与 048_plans_table.py 数据对齐）
    _plan_entitlements = {
        "observe_20": {
            "monitor_limit": 20,
            "notification_channel_limit": 1,
            "message_retention_days": 30,
            "features": [
                "trend_selection",
                "stock_detail",
                "node_monitor",
                "in_app_message",
                "feishu_notification",
                "stock_memo",
            ],
        },
        "research_50": {
            "monitor_limit": 50,
            "notification_channel_limit": 3,
            "message_retention_days": 180,
            "features": [
                "trend_selection",
                "stock_detail",
                "node_monitor",
                "in_app_message",
                "feishu_notification",
                "stock_memo",
                "advanced_export",
            ],
        },
    }
    now = datetime.now(UTC)
    if expired:
        starts_at = now - timedelta(days=40)
        expires_at = now - timedelta(days=10)
    else:
        starts_at = now - timedelta(days=1)
        expires_at = now + timedelta(days=30)
    sub = Subscription(
        id=uuid.uuid4(),
        user_id=user_id,
        plan_code=plan_code,
        status="active",
        starts_at=starts_at,
        expires_at=expires_at,
        # [Subscription] - 描述: 必填字段，从预定义套餐构造（与 plans 表对齐）
        entitlement_snapshot=_plan_entitlements.get(plan_code, _plan_entitlements["observe_20"]),
        source="invite",
        created_by=None,
    )
    db.add(sub)
    await db.flush()
    return sub


# ============================================================
# 1-6: get_access_context 字段填充测试
# ============================================================


@pytest.mark.asyncio
async def test_admin_without_subscription(db_session: AsyncSession) -> None:
    """admin 用户无 subscription → is_admin=True, subscription_active=True, plan_code=None, features=[], limits={}。"""
    admin = await _create_user_with_roles(db_session, ["admin"])
    ctx = await get_access_context(db_session, admin)

    assert ctx.is_admin is True
    assert ctx.subscription_active is True
    assert ctx.plan_code is None
    assert ctx.plan_display_name is None
    assert ctx.expires_at is None
    assert ctx.features == []
    assert ctx.limits == {}


@pytest.mark.asyncio
async def test_member_with_observe_20_active(db_session: AsyncSession) -> None:
    """member 用户有 observe_20 有效订阅 → is_admin=False, subscription_active=True, plan_code="observe_20"。"""
    member = await _create_user_with_roles(db_session, ["member"])
    await _create_subscription(db_session, member.id, plan_code="observe_20", expired=False)
    ctx = await get_access_context(db_session, member)

    assert ctx.is_admin is False
    assert ctx.is_member is True
    assert ctx.subscription_active is True
    assert ctx.plan_code == "observe_20"
    assert ctx.plan_display_name == "观察版"
    assert ctx.expires_at is not None
    # features 含 6 项
    assert len(ctx.features) == 6
    assert "trend_selection" in ctx.features
    assert "stock_detail" in ctx.features
    assert "node_monitor" in ctx.features
    assert "in_app_message" in ctx.features
    assert "feishu_notification" in ctx.features
    assert "stock_memo" in ctx.features
    assert "advanced_export" not in ctx.features
    # limits
    assert ctx.limits == {
        "monitor_limit": 20,
        "notification_channel_limit": 1,
        "message_retention_days": 30,
    }


@pytest.mark.asyncio
async def test_member_with_research_50_active(db_session: AsyncSession) -> None:
    """member 用户有 research_50 有效订阅 → plan_code="research_50", features 含 7 项, limits monitor_limit=50。"""
    member = await _create_user_with_roles(db_session, ["member"])
    await _create_subscription(db_session, member.id, plan_code="research_50", expired=False)
    ctx = await get_access_context(db_session, member)

    assert ctx.is_admin is False
    assert ctx.subscription_active is True
    assert ctx.plan_code == "research_50"
    assert ctx.plan_display_name == "研究版"
    assert len(ctx.features) == 7
    assert "advanced_export" in ctx.features
    assert ctx.limits == {
        "monitor_limit": 50,
        "notification_channel_limit": 3,
        "message_retention_days": 180,
    }


@pytest.mark.asyncio
async def test_member_with_expired_subscription(db_session: AsyncSession) -> None:
    """member 用户订阅已过期 → subscription_active=False, plan_code 仍记录原套餐, plan_display_name 保留。"""
    member = await _create_user_with_roles(db_session, ["member"])
    await _create_subscription(db_session, member.id, plan_code="observe_20", expired=True)
    ctx = await get_access_context(db_session, member)

    assert ctx.is_admin is False
    assert ctx.subscription_active is False
    # 过期但仍记录原套餐代码与展示名
    assert ctx.plan_code == "observe_20"
    assert ctx.plan_display_name == "观察版"
    assert ctx.expires_at is not None
    # features/limits 仍填充（便于前端展示降级提示）
    assert len(ctx.features) == 6
    assert ctx.limits["monitor_limit"] == 20


@pytest.mark.asyncio
async def test_member_without_subscription(db_session: AsyncSession) -> None:
    """member 用户无订阅记录 → subscription_active=False, plan_code=None, features=[], limits={}。"""
    member = await _create_user_with_roles(db_session, ["member"])
    ctx = await get_access_context(db_session, member)

    assert ctx.is_admin is False
    assert ctx.is_member is True
    assert ctx.subscription_active is False
    assert ctx.plan_code is None
    assert ctx.plan_display_name is None
    assert ctx.expires_at is None
    assert ctx.features == []
    assert ctx.limits == {}


@pytest.mark.asyncio
async def test_strategy_author_role(db_session: AsyncSession) -> None:
    """用户有 strategy_author 角色但无 admin → is_admin=False, is_member 判定不影响。"""
    user = await _create_user_with_roles(db_session, ["strategy_author"])
    ctx = await get_access_context(db_session, user)

    # strategy_author 不是 admin，也不是 user
    assert ctx.is_admin is False
    assert ctx.is_member is False
    assert "strategy_author" in ctx.roles
    # 无 admin 角色且无订阅 → subscription_active=False
    assert ctx.subscription_active is False
    assert ctx.plan_code is None


# ============================================================
# 7-8: require_admin 依赖测试
# ============================================================


@pytest.mark.asyncio
async def test_require_admin_passes_for_admin(db_session: AsyncSession) -> None:
    """require_admin 依赖对 admin 用户通过，返回原 ctx。"""
    admin = await _create_user_with_roles(db_session, ["admin"])
    ctx = await get_access_context(db_session, admin)

    result = await require_admin(ctx=ctx)
    assert result is ctx


@pytest.mark.asyncio
async def test_require_admin_fails_for_member(db_session: AsyncSession) -> None:
    """require_admin 依赖对 member 用户返回 403。"""
    member = await _create_user_with_roles(db_session, ["member"])
    ctx = await get_access_context(db_session, member)

    with pytest.raises(HTTPException) as exc_info:
        await require_admin(ctx=ctx)
    assert exc_info.value.status_code == 403


# ============================================================
# 9-10: require_active_subscription 依赖测试
# ============================================================


@pytest.mark.asyncio
async def test_require_active_subscription_passes_for_admin(db_session: AsyncSession) -> None:
    """require_active_subscription 对 admin 通过（豁免，无需订阅）。"""
    admin = await _create_user_with_roles(db_session, ["admin"])
    ctx = await get_access_context(db_session, admin)

    result = await require_active_subscription(ctx=ctx)
    assert result is ctx


@pytest.mark.asyncio
async def test_require_active_subscription_fails_for_expired(db_session: AsyncSession) -> None:
    """require_active_subscription 对过期订阅返回 403。"""
    member = await _create_user_with_roles(db_session, ["member"])
    await _create_subscription(db_session, member.id, plan_code="observe_20", expired=True)
    ctx = await get_access_context(db_session, member)

    with pytest.raises(HTTPException) as exc_info:
        await require_active_subscription(ctx=ctx)
    assert exc_info.value.status_code == 403
    assert "订阅" in exc_info.value.detail


# ============================================================
# 11-13: require_feature 依赖测试
# ============================================================


@pytest.mark.asyncio
async def test_require_feature_passes(db_session: AsyncSession) -> None:
    """require_feature("trend_selection") 对 observe_20 用户通过。"""
    member = await _create_user_with_roles(db_session, ["member"])
    await _create_subscription(db_session, member.id, plan_code="observe_20", expired=False)
    ctx = await get_access_context(db_session, member)

    dep = require_feature("trend_selection")
    result = await dep(ctx=ctx)
    assert result is ctx


@pytest.mark.asyncio
async def test_require_feature_fails(db_session: AsyncSession) -> None:
    """require_feature("advanced_export") 对 observe_20 用户返回 403。"""
    member = await _create_user_with_roles(db_session, ["member"])
    await _create_subscription(db_session, member.id, plan_code="observe_20", expired=False)
    ctx = await get_access_context(db_session, member)

    dep = require_feature("advanced_export")
    with pytest.raises(HTTPException) as exc_info:
        await dep(ctx=ctx)
    assert exc_info.value.status_code == 403
    assert "advanced_export" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_feature_admin_exempt(db_session: AsyncSession) -> None:
    """require_feature("advanced_export") 对 admin 通过（豁免，无需 features 含该项）。"""
    admin = await _create_user_with_roles(db_session, ["admin"])
    ctx = await get_access_context(db_session, admin)
    # admin 的 features 为空，但应豁免
    assert "advanced_export" not in ctx.features

    dep = require_feature("advanced_export")
    result = await dep(ctx=ctx)
    assert result is ctx


# ============================================================
# 14: AccessContext 字段完整性测试
# ============================================================


def test_access_context_has_all_11_fields() -> None:
    """AccessContext 必须包含全部 11 个字段。"""
    expected_fields = {
        "user_id",
        "account_status",
        "roles",
        "is_admin",
        "is_member",
        "subscription_active",
        "plan_code",
        "plan_display_name",
        "expires_at",
        "features",
        "limits",
    }
    actual_fields = set(AccessContext.model_fields.keys())
    assert actual_fields == expected_fields, (
        f"AccessContext 字段不匹配，缺失: {expected_fields - actual_fields}，"
        f"多余: {actual_fields - expected_fields}"
    )
    assert len(actual_fields) == 11


# ============================================================
# 补充: require_quota 依赖测试
# ============================================================


@pytest.mark.asyncio
async def test_require_quota_returns_limit_for_member(db_session: AsyncSession) -> None:
    """require_quota("monitor_limit") 对 observe_20 用户返回 20。"""
    member = await _create_user_with_roles(db_session, ["member"])
    await _create_subscription(db_session, member.id, plan_code="observe_20", expired=False)
    ctx = await get_access_context(db_session, member)

    dep = require_quota("monitor_limit")
    result = await dep(ctx=ctx)
    assert result == 20


@pytest.mark.asyncio
async def test_require_quota_admin_returns_none(db_session: AsyncSession) -> None:
    """require_quota 对 admin 返回 None（无限制）。"""
    admin = await _create_user_with_roles(db_session, ["admin"])
    ctx = await get_access_context(db_session, admin)

    dep = require_quota("monitor_limit")
    result = await dep(ctx=ctx)
    assert result is None


@pytest.mark.asyncio
async def test_require_quota_missing_for_member_without_subscription(
    db_session: AsyncSession,
) -> None:
    """require_quota 对无订阅 member（quota 不在 limits）返回 403。"""
    member = await _create_user_with_roles(db_session, ["member"])
    ctx = await get_access_context(db_session, member)
    assert ctx.limits == {}

    dep = require_quota("monitor_limit")
    with pytest.raises(HTTPException) as exc_info:
        await dep(ctx=ctx)
    assert exc_info.value.status_code == 403


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
