"""GET /me/access 端点测试（Phase 2 Task 2.5）。

验证返回完整 AccessContext（11 个字段）：
{user_id, account_status, roles, is_admin, is_member,
 subscription_active, plan_code, plan_display_name, expires_at, features, limits}

测试用例：
- admin 用户：is_admin=True, subscription_active=True（豁免），plan_code=None
- member 有效订阅（observe_20）：is_admin=False, subscription_active=True, plan_code="observe_20"
- member 订阅过期：subscription_active=False（plan_code 仍保留便于前端降级提示）
- member 无订阅：subscription_active=False, plan_code=None
- 响应包含全部 11 个字段（字段集合精确匹配）
- 无 token 调用返回 401

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库 bz_stock_test）
- 通过 dependency_overrides 注入测试 session 到 app
- 使用 ASGITransport + AsyncClient 调用 HTTP 端点
- 复用 test_me_entitlements.py 的辅助函数模式（admin/会员用户创建）
- 端点只读：不写 DB，复用 get_access_context 唯一真源
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.services.subscription_service import (
    generate_invite_codes,
    register_with_invite_code,
)

# AccessContext 11 个字段（与 access_control_service.AccessContext 对齐）
_EXPECTED_ACCESS_FIELDS = {
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


async def _ensure_role(db: AsyncSession, name: str) -> Role:
    """确保角色存在并返回。"""
    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _create_admin(db: AsyncSession) -> User:
    """创建管理员用户（admin 角色）。"""
    admin = User(
        id=uuid.uuid4(),
        email=f"admin_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("admin-password-123"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(admin)
    admin_role = await _ensure_role(db, "admin")
    db.add(UserRole(user_id=admin.id, role_id=admin_role.id))
    await db.flush()
    return admin


async def _create_normal_user_with_membership(
    db: AsyncSession, plan_code: str, grant_months: int = 1
) -> tuple[User, Subscription]:
    """通过邀请码注册创建普通用户 + 订阅记录（复用 subscription_service 唯一真源）。"""
    admin = await _create_admin(db)
    results = await generate_invite_codes(
        db=db,
        count=1,
        created_by=admin.id,
        plan_code=plan_code,
        grant_months=grant_months,
    )
    await db.flush()
    email = f"user_{uuid.uuid4().hex[:8]}@test.com"
    user, subscription = await register_with_invite_code(
        db=db,
        email=email,
        password="password-12345",
        raw_invite_code=results[0][1],
    )
    await db.flush()
    return user, subscription


async def _create_member_without_subscription(db: AsyncSession) -> User:
    """创建无订阅记录的普通用户（仅 user 角色，无 subscription）。"""
    user = User(
        id=uuid.uuid4(),
        email=f"nomember_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("password-12345"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(user)
    user_role = await _ensure_role(db, "user")
    db.add(UserRole(user_id=user.id, role_id=user_role.id))
    await db.flush()
    return user


@pytest_asyncio.fixture
async def access_client(
    db_session: AsyncSession,
) -> AsyncGenerator[tuple[AsyncClient, AsyncSession], None]:
    """提供 HTTP 客户端 + 测试 DB session，通过 dependency_overrides 注入。

    覆盖 app.core.deps.get_db 与 app.db.get_db 两个入口，确保路由拿到的 session
    与 fixture 中操作的是同一事务（测试后由 db_session fixture 回滚）。
    """
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[deps_get_db] = get_test_db
    app.dependency_overrides[db_get_db] = get_test_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, db_session

    app.dependency_overrides.clear()


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# /me/access 端点测试
# ============================================================


@pytest.mark.asyncio
async def test_me_access_admin(access_client: tuple[AsyncClient, AsyncSession]) -> None:
    """admin 用户：is_admin=True, subscription_active=True（豁免），plan_code=None。"""
    client, db = access_client
    admin = await _create_admin(db)
    await db.flush()

    resp = await client.get("/me/access", headers=_auth_headers(admin.id))

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_admin"] is True
    assert data["is_member"] is False
    assert data["subscription_active"] is True
    assert data["plan_code"] is None
    assert data["plan_display_name"] is None
    assert data["expires_at"] is None
    assert data["features"] == []
    assert data["limits"] == {}
    assert data["account_status"] == "active"
    assert "admin" in data["roles"]
    assert data["user_id"] == str(admin.id)


@pytest.mark.asyncio
async def test_me_access_member_active(
    access_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """member 有效订阅（observe_20）：is_admin=False, subscription_active=True, plan_code="observe_20"。"""
    client, db = access_client
    user, _subscription = await _create_normal_user_with_membership(db, "observe_20", grant_months=1)
    await db.flush()

    resp = await client.get("/me/access", headers=_auth_headers(user.id))

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_admin"] is False
    assert data["is_member"] is True
    assert data["subscription_active"] is True
    assert data["plan_code"] == "observe_20"
    assert data["plan_display_name"] == "观察版"
    assert data["expires_at"] is not None
    # observe_20 套餐 6 个 features（与 plans 表迁移一致）
    assert isinstance(data["features"], list)
    assert len(data["features"]) == 6
    # limits 含 monitor_limit / notification_channel_limit / message_retention_days
    assert data["limits"]["monitor_limit"] == 20
    assert data["limits"]["notification_channel_limit"] == 1
    assert data["limits"]["message_retention_days"] == 30
    assert "user" in data["roles"]
    assert data["user_id"] == str(user.id)


@pytest.mark.asyncio
async def test_me_access_member_expired(
    access_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """member 订阅过期：subscription_active=False，plan_code 仍保留（前端降级提示）。"""
    client, db = access_client
    user, subscription = await _create_normal_user_with_membership(db, "observe_20", grant_months=1)
    # [Test] - 描述: 手动设为已到期，验证 get_access_context 实时计算为 expired
    subscription.expires_at = datetime.now(UTC) - timedelta(days=1)
    subscription.status = "expired"
    await db.flush()

    resp = await client.get("/me/access", headers=_auth_headers(user.id))

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_admin"] is False
    assert data["is_member"] is True
    assert data["subscription_active"] is False
    # [AccessControl] - 描述: 过期订阅仍保留 plan_code/plan_display_name/features/limits
    assert data["plan_code"] == "observe_20"
    assert data["plan_display_name"] == "观察版"
    assert data["expires_at"] is not None
    assert data["limits"]["monitor_limit"] == 20


@pytest.mark.asyncio
async def test_me_access_member_no_subscription(
    access_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """member 无订阅：subscription_active=False, plan_code=None。"""
    client, db = access_client
    user = await _create_member_without_subscription(db)
    await db.flush()

    resp = await client.get("/me/access", headers=_auth_headers(user.id))

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_admin"] is False
    assert data["is_member"] is True
    assert data["subscription_active"] is False
    assert data["plan_code"] is None
    assert data["plan_display_name"] is None
    assert data["expires_at"] is None
    assert data["features"] == []
    assert data["limits"] == {}


@pytest.mark.asyncio
async def test_me_access_returns_all_11_fields(
    access_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """响应 JSON 包含全部 11 个 AccessContext 字段（字段集合精确匹配）。"""
    client, db = access_client
    admin = await _create_admin(db)
    await db.flush()

    resp = await client.get("/me/access", headers=_auth_headers(admin.id))

    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == _EXPECTED_ACCESS_FIELDS, (
        f"字段集合不匹配，缺失: {_EXPECTED_ACCESS_FIELDS - set(data.keys())}，"
        f"多余: {set(data.keys()) - _EXPECTED_ACCESS_FIELDS}"
    )
    assert len(data.keys()) == 11


@pytest.mark.asyncio
async def test_me_access_requires_auth(
    access_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """无 token 调用 /me/access 返回 401。"""
    client, _db = access_client

    resp = await client.get("/me/access")  # 不带 Authorization 头

    assert resp.status_code == 401


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
