"""管理员用户管理新端点测试（Phase 4.4）。

测试内容：
1. GET /admin/users — 用户列表（分页）
2. GET /admin/users/{user_id} — 用户详情
3. POST /admin/users/{user_id}/enable — 启用账户
4. POST /admin/users/{user_id}/disable — 停用账户
5. POST /admin/users/{user_id}/subscriptions/grant — 授予套餐
6. POST /admin/users/{user_id}/subscriptions/renew — 续期套餐
7. POST /admin/users/{user_id}/subscriptions/revoke — 撤销套餐
8. POST /admin/users/{user_id}/subscriptions/change-plan — 变更套餐
9. GET /admin/audit-logs?target_user_id={user_id} — 审计日志查询

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
from app.models.user import User


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


async def _db_session_flush(obj, db_session: AsyncSession) -> None:
    """辅助：将对象变更 flush 到当前事务。"""
    db_session.add(obj)
    await db_session.flush()


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
# 权限测试
# ============================================================


@pytest.mark.asyncio
async def test_non_admin_cannot_access_users_list(
    client: AsyncClient,
    member_user: User,
) -> None:
    """普通用户访问 /admin/users 返回 403。"""
    response = await client.get(
        "/admin/users",
        headers=_auth_headers(member_user.id),
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_non_admin_cannot_disable_user(
    client: AsyncClient,
    member_user: User,
) -> None:
    """普通用户调用 disable 返回 403。"""
    response = await client.post(
        f"/admin/users/{member_user.id}/disable",
        headers=_auth_headers(member_user.id),
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_non_admin_cannot_grant_subscription(
    client: AsyncClient,
    member_user: User,
) -> None:
    """普通用户调用 subscriptions/grant 返回 403。"""
    response = await client.post(
        f"/admin/users/{member_user.id}/subscriptions/grant",
        headers=_auth_headers(member_user.id),
        json={"plan_code": "observe_20", "grant_months": 1},
    )
    assert response.status_code == 403


# ============================================================
# GET /admin/users 与 /admin/users/{user_id}
# ============================================================


@pytest.mark.asyncio
async def test_admin_list_users(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
) -> None:
    """管理员可分页查询用户列表。"""
    response = await client.get(
        "/admin/users?limit=10&offset=0",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 2
    assert len(data["items"]) >= 2
    emails = {item["email"] for item in data["items"]}
    assert admin_user.email in emails
    assert member_user.email in emails


@pytest.mark.asyncio
async def test_admin_get_user_detail(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    db_session: AsyncSession,
) -> None:
    """管理员可查询用户详情，并写入 user.read 审计日志。"""
    response = await client.get(
        f"/admin/users/{member_user.id}",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(member_user.id)
    assert data["email"] == member_user.email
    assert "member" in data["roles"]

    logs = await db_session.execute(
        sa_select(AccessAuditLog)
        .where(
            AccessAuditLog.action == "user.read",
            AccessAuditLog.target_id == str(member_user.id),
        )
        .order_by(AccessAuditLog.created_at.desc())
    )
    assert logs.scalar_one_or_none() is not None


# ============================================================
# enable / disable
# ============================================================


@pytest.mark.asyncio
async def test_admin_disable_user(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    db_session: AsyncSession,
) -> None:
    """管理员可停用普通用户，状态变为 disabled 并记录审计日志。"""
    response = await client.post(
        f"/admin/users/{member_user.id}/disable",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "disabled"

    logs = await db_session.execute(
        sa_select(AccessAuditLog)
        .where(
            AccessAuditLog.action == "user.disable",
            AccessAuditLog.target_id == str(member_user.id),
        )
        .order_by(AccessAuditLog.created_at.desc())
    )
    log = logs.scalar_one_or_none()
    assert log is not None
    assert log.before_data == {"status": "active"}
    assert log.after_data == {"status": "disabled"}


@pytest.mark.asyncio
async def test_admin_enable_user(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    db_session: AsyncSession,
) -> None:
    """管理员可启用被停用用户，状态变为 active 并记录审计日志。"""
    member_user.status = "disabled"
    await _db_session_flush(member_user, db_session)

    response = await client.post(
        f"/admin/users/{member_user.id}/enable",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "active"

    logs = await db_session.execute(
        sa_select(AccessAuditLog)
        .where(
            AccessAuditLog.action == "user.enable",
            AccessAuditLog.target_id == str(member_user.id),
        )
        .order_by(AccessAuditLog.created_at.desc())
    )
    log = logs.scalar_one_or_none()
    assert log is not None
    assert log.before_data == {"status": "disabled"}
    assert log.after_data == {"status": "active"}


# ============================================================
# subscriptions/grant / renew / revoke / change-plan
# ============================================================


@pytest.mark.asyncio
async def test_admin_grant_subscription_endpoint(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    db_session: AsyncSession,
) -> None:
    """管理员可通过新端点授予 subscription。"""
    response = await client.post(
        f"/admin/users/{member_user.id}/subscriptions/grant",
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
        .order_by(AccessAuditLog.created_at.desc())
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
    """管理员可通过新端点续期 subscription。"""
    now = datetime.now(UTC)
    await subscription_factory(
        user_id=member_user.id,
        plan_code="observe_20",
        expires_at=now + timedelta(days=10),
    )

    response = await client.post(
        f"/admin/users/{member_user.id}/subscriptions/renew",
        headers=_auth_headers(admin_user.id),
        json={"grant_months": 2},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "active"
    assert data["old_expires_at"] is not None
    assert data["new_expires_at"] is not None

    logs = await db_session.execute(
        sa_select(AccessAuditLog)
        .where(
            AccessAuditLog.action == "subscription.renew",
            AccessAuditLog.target_id == str(member_user.id),
        )
        .order_by(AccessAuditLog.created_at.desc())
    )
    assert logs.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_admin_revoke_subscription_endpoint(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    subscription_factory: Callable[..., Subscription],
    db_session: AsyncSession,
) -> None:
    """管理员可通过新端点撤销 subscription。"""
    await subscription_factory(user_id=member_user.id)

    response = await client.post(
        f"/admin/users/{member_user.id}/subscriptions/revoke",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"

    logs = await db_session.execute(
        sa_select(AccessAuditLog)
        .where(
            AccessAuditLog.action == "subscription.revoke",
            AccessAuditLog.target_id == str(member_user.id),
        )
        .order_by(AccessAuditLog.created_at.desc())
    )
    assert logs.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_admin_change_plan_endpoint(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    subscription_factory: Callable[..., Subscription],
    db_session: AsyncSession,
) -> None:
    """管理员可通过新端点切换用户套餐。"""
    now = datetime.now(UTC)
    await subscription_factory(
        user_id=member_user.id,
        plan_code="observe_20",
        expires_at=now + timedelta(days=10),
    )

    response = await client.post(
        f"/admin/users/{member_user.id}/subscriptions/change-plan",
        headers=_auth_headers(admin_user.id),
        json={"plan_code": "research_50", "grant_months": 1},
    )
    assert response.status_code == 200
    assert response.json()["plan_code"] == "research_50"

    logs = await db_session.execute(
        sa_select(AccessAuditLog)
        .where(
            AccessAuditLog.action == "subscription.change_plan",
            AccessAuditLog.target_id == str(member_user.id),
        )
        .order_by(AccessAuditLog.created_at.desc())
    )
    assert logs.scalar_one_or_none() is not None


# ============================================================
# GET /admin/audit-logs
# ============================================================


@pytest.mark.asyncio
async def test_admin_audit_logs_filter_by_target_user(
    client: AsyncClient,
    admin_user: User,
    member_user: User,
    db_session: AsyncSession,
) -> None:
    """管理员可查询指定用户的审计日志。"""
    # 先产生一条目标用户的审计日志
    from app.services.access_audit_service import write_audit_log

    await write_audit_log(
        db=db_session,
        actor_user_id=admin_user.id,
        action="user.read",
        target_type="user",
        target_id=str(member_user.id),
        after_data={"email": member_user.email},
    )
    await db_session.flush()

    response = await client.get(
        f"/admin/audit-logs?target_user_id={member_user.id}",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1
    assert all(item["target_id"] == str(member_user.id) for item in data["items"])


if __name__ == "__main__":
    # 自测入口：验证测试模块可导入
    print("test_admin_users_api module loaded")
    print("OK")
