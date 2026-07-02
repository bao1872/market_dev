"""管理员用户订阅管理端点测试。

测试内容：
1. subscription_service 4 个可复用函数（grant/renew/revoke/change-plan）
2. /admin/users/{user_id}/* 8 个端点
3. /admin/audit-logs 查询端点
4. 权限与业务规则（admin 无套餐、角色名 member、审计日志写入）

测试策略：
- PostgreSQL 测试库 + Alembic 迁移结构
- 使用 conftest.py 公共 db_session / client / factories
- 禁止手写 schema / SQLite
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_password_hash
from app.models.access_audit_log import AccessAuditLog
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.services.subscription_service import (
    change_subscription_plan,
    grant_subscription_to_user,
    renew_subscription,
    revoke_subscription,
)


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def admin_user(user_factory: Callable[..., User]) -> User:
    """创建管理员测试用户。"""
    return await user_factory(
        email="admin@example.com",
        password_hash=get_password_hash("admin-password-123"),
        roles=["admin"],
    )


@pytest_asyncio.fixture
async def member_user(user_factory: Callable[..., User]) -> User:
    """创建普通会员测试用户（无订阅）。"""
    return await user_factory(
        email="member@example.com",
        password_hash=get_password_hash("member-password-123"),
        roles=["member"],
    )


# ============================================================
# subscription_service 函数测试
# ============================================================


@pytest.mark.asyncio
async def test_grant_subscription_to_user_creates_subscription(
    db_session: AsyncSession,
    member_user: User,
) -> None:
    """grant_subscription_to_user 为无订阅用户创建 subscription。"""
    subscription = await grant_subscription_to_user(
        db=db_session,
        user_id=member_user.id,
        plan_code="observe_20",
        grant_months=3,
        actor_user_id=None,
    )

    assert subscription.user_id == member_user.id
    assert subscription.plan_code == "observe_20"
    assert subscription.status == "active"
    assert subscription.source == "admin_grant"
    assert subscription.expires_at > datetime.now(UTC) + timedelta(days=80)
    assert subscription.entitlement_snapshot["monitor_limit"] > 0


@pytest.mark.asyncio
async def test_grant_subscription_to_user_already_exists_fails(
    db_session: AsyncSession,
    member_user: User,
    subscription_factory: Callable[..., Subscription],
) -> None:
    """用户已存在 subscription 时再次 grant 应失败。"""
    await subscription_factory(user_id=member_user.id)

    with pytest.raises(ValueError, match="已存在"):
        await grant_subscription_to_user(
            db=db_session,
            user_id=member_user.id,
            plan_code="observe_20",
            grant_months=1,
        )


@pytest.mark.asyncio
async def test_grant_subscription_to_admin_fails(
    db_session: AsyncSession,
    admin_user: User,
) -> None:
    """管理员不绑定套餐，grant 应失败。"""
    with pytest.raises(ValueError, match="admin"):
        await grant_subscription_to_user(
            db=db_session,
            user_id=admin_user.id,
            plan_code="observe_20",
            grant_months=1,
        )


@pytest.mark.asyncio
async def test_renew_subscription_extends_expires_at(
    db_session: AsyncSession,
    member_user: User,
    subscription_factory: Callable[..., Subscription],
) -> None:
    """未到期续期从当前到期日顺延自然月。"""
    now = datetime.now(UTC)
    old_expires = now + timedelta(days=10)
    await subscription_factory(
        user_id=member_user.id,
        plan_code="observe_20",
        expires_at=old_expires,
    )

    subscription, old_at, new_at = await renew_subscription(
        db=db_session,
        user_id=member_user.id,
        grant_months=2,
    )

    assert subscription.status == "active"
    assert old_at == old_expires
    assert (new_at - old_at).days >= 58


@pytest.mark.asyncio
async def test_renew_subscription_after_expiry(
    db_session: AsyncSession,
    member_user: User,
    subscription_factory: Callable[..., Subscription],
) -> None:
    """已到期续期从当前时间重新计算。"""
    now = datetime.now(UTC)
    old_expires = now - timedelta(days=5)
    await subscription_factory(
        user_id=member_user.id,
        plan_code="observe_20",
        expires_at=old_expires,
    )

    subscription, old_at, new_at = await renew_subscription(
        db=db_session,
        user_id=member_user.id,
        grant_months=1,
    )

    assert subscription.status == "active"
    assert old_at == old_expires
    assert new_at > now + timedelta(days=25)


@pytest.mark.asyncio
async def test_renew_subscription_no_subscription_fails(
    db_session: AsyncSession,
    member_user: User,
) -> None:
    """无 subscription 时续期失败。"""
    with pytest.raises(ValueError, match="订阅记录不存在"):
        await renew_subscription(
            db=db_session,
            user_id=member_user.id,
            grant_months=1,
        )


@pytest.mark.asyncio
async def test_revoke_subscription_marks_revoked(
    db_session: AsyncSession,
    member_user: User,
    subscription_factory: Callable[..., Subscription],
) -> None:
    """revoke_subscription 将 subscription 标记为 revoked。"""
    await subscription_factory(user_id=member_user.id)

    subscription = await revoke_subscription(db=db_session, user_id=member_user.id)

    assert subscription.status == "revoked"


@pytest.mark.asyncio
async def test_change_subscription_plan_updates_plan_code(
    db_session: AsyncSession,
    member_user: User,
    subscription_factory: Callable[..., Subscription],
) -> None:
    """change_subscription_plan 更新套餐并顺延到期日。"""
    now = datetime.now(UTC)
    await subscription_factory(
        user_id=member_user.id,
        plan_code="observe_20",
        expires_at=now + timedelta(days=10),
    )

    subscription = await change_subscription_plan(
        db=db_session,
        user_id=member_user.id,
        plan_code="research_50",
        grant_months=2,
    )

    assert subscription.plan_code == "research_50"
    assert subscription.entitlement_snapshot["monitor_limit"] > 0
    assert subscription.expires_at > now + timedelta(days=60)


@pytest.mark.asyncio
async def test_change_subscription_plan_creates_when_missing(
    db_session: AsyncSession,
    member_user: User,
) -> None:
    """用户无 subscription 时 change_plan 创建新 subscription。"""
    subscription = await change_subscription_plan(
        db=db_session,
        user_id=member_user.id,
        plan_code="observe_20",
        grant_months=1,
    )

    assert subscription.plan_code == "observe_20"
    assert subscription.source == "admin_grant"


# ============================================================
# /admin/users/* 端点测试
# ============================================================


@pytest.mark.asyncio
async def test_admin_disable_user(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    db_session: AsyncSession,
) -> None:
    """管理员可以禁用普通用户。"""
    response = await client.post(
        f"/admin/users/{member_user.id}/disable",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "disabled"

    # 验证审计日志
    logs = await db_session.execute(
        sa_select(AccessAuditLog)
        .where(
            AccessAuditLog.action == "user.disable",
            AccessAuditLog.target_id == str(member_user.id),
        )
        .order_by(AccessAuditLog.created_at.desc())
    )
    assert logs.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_admin_enable_user(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    db_session: AsyncSession,
) -> None:
    """管理员可以启用被禁用的普通用户。"""
    member_user.status = "disabled"
    await db_session_flush(member_user, db_session)

    response = await client.post(
        f"/admin/users/{member_user.id}/enable",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "active"


@pytest.mark.asyncio
async def test_admin_grant_subscription_endpoint(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    db_session: AsyncSession,
) -> None:
    """管理员可通过端点为用户授予 subscription。"""
    response = await client.post(
        f"/admin/users/{member_user.id}/grant-subscription",
        headers=_auth_headers(admin_user.id),
        json={"plan_code": "observe_20", "grant_months": 3},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["plan_code"] == "observe_20"
    assert data["status"] == "active"

    logs = await db_session.execute(
        sa_select(AccessAuditLog)
        .where(
            AccessAuditLog.action == "subscription.grant",
            AccessAuditLog.target_id == str(member_user.id),
        )
    )
    assert logs.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_admin_renew_subscription_endpoint(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    subscription_factory: Callable[..., Subscription],
    db_session: AsyncSession,
) -> None:
    """管理员可通过端点为用户续期 subscription。"""
    now = datetime.now(UTC)
    await subscription_factory(
        user_id=member_user.id,
        plan_code="observe_20",
        expires_at=now + timedelta(days=10),
    )

    response = await client.post(
        f"/admin/users/{member_user.id}/renew-subscription",
        headers=_auth_headers(admin_user.id),
        json={"grant_months": 2},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "active"
    assert data["old_expires_at"] is not None
    assert data["new_expires_at"] is not None


@pytest.mark.asyncio
async def test_admin_revoke_subscription_endpoint(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    subscription_factory: Callable[..., Subscription],
    db_session: AsyncSession,
) -> None:
    """管理员可通过端点撤销用户 subscription。"""
    await subscription_factory(user_id=member_user.id)

    response = await client.post(
        f"/admin/users/{member_user.id}/revoke-subscription",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"


@pytest.mark.asyncio
async def test_admin_change_plan_endpoint(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    subscription_factory: Callable[..., Subscription],
) -> None:
    """管理员可通过端点切换用户套餐。"""
    now = datetime.now(UTC)
    await subscription_factory(
        user_id=member_user.id,
        plan_code="observe_20",
        expires_at=now + timedelta(days=10),
    )

    response = await client.post(
        f"/admin/users/{member_user.id}/change-plan",
        headers=_auth_headers(admin_user.id),
        json={"plan_code": "research_50", "grant_months": 1},
    )
    assert response.status_code == 200
    assert response.json()["plan_code"] == "research_50"


@pytest.mark.asyncio
async def test_admin_change_role_to_admin_revokes_subscription(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    subscription_factory: Callable[..., Subscription],
    db_session: AsyncSession,
) -> None:
    """将用户改为 admin 角色后应撤销其 subscription。"""
    await subscription_factory(user_id=member_user.id)

    response = await client.post(
        f"/admin/users/{member_user.id}/change-role",
        headers=_auth_headers(admin_user.id),
        json={"role": "admin"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "admin" in data["roles"]

    # 验证 subscription 被撤销
    sub_result = await db_session.execute(
        sa_select(Subscription).where(Subscription.user_id == member_user.id)
    )
    subscription = sub_result.scalar_one_or_none()
    assert subscription is None or subscription.status == "revoked"


@pytest.mark.asyncio
async def test_admin_change_role_to_member(
    client: AsyncClient,
    admin_user: User,
    user_factory: Callable[..., User],
) -> None:
    """将用户改为 member 角色。"""
    user = await user_factory(roles=["admin"])

    response = await client.post(
        f"/admin/users/{user.id}/change-role",
        headers=_auth_headers(admin_user.id),
        json={"role": "member"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "member" in data["roles"]
    assert "admin" not in data["roles"]


@pytest.mark.asyncio
async def test_admin_reset_password_endpoint(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    db_session: AsyncSession,
) -> None:
    """管理员重置用户密码请求端点记录审计日志。"""
    response = await client.post(
        f"/admin/users/{member_user.id}/reset-password",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    assert response.json()["user_id"] == str(member_user.id)

    logs = await db_session.execute(
        sa_select(AccessAuditLog)
        .where(
            AccessAuditLog.action == "user.reset_password",
            AccessAuditLog.target_id == str(member_user.id),
        )
    )
    assert logs.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_non_admin_cannot_access_user_endpoints(
    client: AsyncClient,
    member_user: User,
) -> None:
    """普通用户不能访问 admin 用户管理端点。"""
    token = create_access_token(str(member_user.id))
    response = await client.post(
        f"/admin/users/{member_user.id}/disable",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


# ============================================================
# /admin/audit-logs 端点测试
# ============================================================


@pytest.mark.asyncio
async def test_admin_audit_logs_endpoint(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    db_session: AsyncSession,
) -> None:
    """管理员可查询审计日志。"""
    # 先产生一条审计日志
    from app.services.access_audit_service import write_audit_log

    await write_audit_log(
        db=db_session,
        actor_user_id=admin_user.id,
        action="user.disable",
        target_type="user",
        target_id=str(member_user.id),
        after_data={"status": "disabled"},
    )
    await db_session.commit()

    response = await client.get(
        "/admin/audit-logs",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1
    assert any(item["target_id"] == str(member_user.id) for item in data["items"])


@pytest.mark.asyncio
async def test_admin_audit_logs_target_user_filter(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    user_factory: Callable[..., User],
    db_session: AsyncSession,
) -> None:
    """审计日志支持按 target_user_id 筛选。"""
    from app.services.access_audit_service import write_audit_log

    other_user = await user_factory()
    await write_audit_log(
        db=db_session,
        actor_user_id=admin_user.id,
        action="user.disable",
        target_type="user",
        target_id=str(member_user.id),
        after_data={"status": "disabled"},
    )
    await write_audit_log(
        db=db_session,
        actor_user_id=admin_user.id,
        action="user.disable",
        target_type="user",
        target_id=str(other_user.id),
        after_data={"status": "disabled"},
    )
    await db_session.commit()

    response = await client.get(
        f"/admin/audit-logs?target_user_id={member_user.id}",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    data = response.json()
    assert all(item["target_id"] == str(member_user.id) for item in data["items"])


async def db_session_flush(obj, db_session: AsyncSession) -> None:
    """辅助：将对象变更 flush 到当前事务。"""
    db_session.add(obj)
    await db_session.flush()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
