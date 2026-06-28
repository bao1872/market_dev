"""V1.6 会员与邀请码系统测试。

测试内容：
1. 邀请码注册（成功/邀请码无效/已使用/已作废/邮箱已注册）
2. 邀请码续期（未到期顺延/已到期从当天计算）
3. 会员状态查询（active/expired/无记录）
4. 登录会员到期拦截（到期返回 membership_expired=true）
5. 管理员邀请码管理（生成/作废/列表）
6. 管理员会员列表（含会员状态/到期时间/续期次数）
7. RBAC 越权访问（普通用户不能访问 admin 端点）

测试策略：
- 使用 sqlite 内存数据库 + 异步 SQLAlchemy
- 创建 users/roles/user_roles/memberships/invite_codes/invite_redemptions 表
- 通过 dependency_overrides 注入测试会话
- 覆盖主逻辑 + 边界条件
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models.membership import InviteCode, InviteRedemption, Membership
from app.models.user import Role, User, UserRole
from app.services.membership_service import (
    generate_invite_codes,
    hash_invite_code,
)


# SQLite 兼容的建表 DDL
_SQLITE_DDL_STATEMENTS = [
    """
CREATE TABLE IF NOT EXISTS users (
    id TEXT NOT NULL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
""",
    """
CREATE TABLE IF NOT EXISTS roles (
    id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
""",
    """
CREATE TABLE IF NOT EXISTS user_roles (
    user_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, role_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
)
""",
    """
CREATE TABLE IF NOT EXISTS memberships (
    id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active',
    started_at DATETIME NOT NULL,
    expires_at DATETIME NOT NULL,
    plan_code TEXT,
    monitor_limit INTEGER,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
)
""",
    """
CREATE TABLE IF NOT EXISTS invite_codes (
    id TEXT NOT NULL PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'unused',
    grant_days INTEGER NOT NULL DEFAULT 30,
    plan_code TEXT,
    monitor_limit INTEGER,
    grant_months INTEGER,
    note TEXT,
    created_by TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    used_by TEXT,
    used_at DATETIME,
    usage_type TEXT,
    FOREIGN KEY (created_by) REFERENCES users(id),
    FOREIGN KEY (used_by) REFERENCES users(id)
)
""",
    """
CREATE TABLE IF NOT EXISTS invite_redemptions (
    id TEXT NOT NULL PRIMARY KEY,
    invite_code_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    usage_type TEXT NOT NULL,
    old_expires_at DATETIME,
    new_expires_at DATETIME NOT NULL,
    redeemed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (invite_code_id) REFERENCES invite_codes(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
)
""",
]


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """创建内存 SQLite 异步会话，测试后销毁。

    创建 users/roles/user_roles/memberships/invite_codes/invite_redemptions 表，
    注入测试数据：admin 用户（admin 角色）+ normal 用户（user 角色）。
    """
    try:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    except Exception:
        pytest.skip("aiosqlite 不可用，跳过 DB 测试")

    async with engine.begin() as conn:
        for ddl_stmt in _SQLITE_DDL_STATEMENTS:
            await conn.execute(text(ddl_stmt))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        # 创建角色
        admin_role = Role(id=uuid.uuid4(), name="admin", description="管理员")
        user_role = Role(id=uuid.uuid4(), name="user", description="普通用户")
        session.add(admin_role)
        session.add(user_role)

        # 创建 admin 用户
        admin_user = User(
            id=uuid.uuid4(),
            email="admin@example.com",
            password_hash=get_password_hash("admin-password-123"),
            status="active",
            timezone="Asia/Shanghai",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(admin_user)
        session.add(UserRole(user_id=admin_user.id, role_id=admin_role.id))

        await session.commit()

        # 注入测试会话
        async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
            yield session

        from app.core.deps import get_db as deps_get_db
        from app.db import get_db as db_get_db
        app.dependency_overrides[deps_get_db] = get_test_db
        app.dependency_overrides[db_get_db] = get_test_db

        session._test_admin_user = admin_user  # type: ignore[attr-defined]
        session._test_admin_role = admin_role  # type: ignore[attr-defined]
        session._test_user_role = user_role  # type: ignore[attr-defined]

        yield session

        app.dependency_overrides.clear()

    await engine.dispose()


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 邀请码生成测试
# ============================================================


@pytest.mark.asyncio
async def test_generate_invite_codes_single(db_session: AsyncSession) -> None:
    """测试管理员生成单个邀请码。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    headers = _auth_headers(admin_user.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/invite-codes",
            headers=headers,
            json={"count": 1, "note": "test batch"},
        )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert "code" in data[0]
    assert len(data[0]["code"]) > 0
    assert data[0]["grant_days"] == 30
    assert data[0]["note"] == "test batch"


@pytest.mark.asyncio
async def test_generate_invite_codes_batch(db_session: AsyncSession) -> None:
    """测试管理员批量生成邀请码。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    headers = _auth_headers(admin_user.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/invite-codes",
            headers=headers,
            json={"count": 5, "note": "batch of 5"},
        )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 5
    # 验证每个邀请码不同
    codes = [item["code"] for item in data]
    assert len(set(codes)) == 5


@pytest.mark.asyncio
async def test_generate_invite_codes_normal_user_forbidden(db_session: AsyncSession) -> None:
    """测试普通用户不能生成邀请码。"""
    # 先注册一个普通用户
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin_user.id, note="for register"
    )
    await db_session.commit()
    raw_code = results[0][1]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 注册新用户
        reg_response = await client.post(
            "/auth/register",
            json={
                "email": "newuser@example.com",
                "password": "newuser-password-123",
                "invite_code": raw_code,
            },
        )
    assert reg_response.status_code == 200
    new_user_token = reg_response.json()["access_token"]

    # 新用户尝试生成邀请码（应被拒绝）
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/invite-codes",
            headers={"Authorization": f"Bearer {new_user_token}"},
            json={"count": 1},
        )
    assert response.status_code == 403


# ============================================================
# 邀请码注册测试
# ============================================================


@pytest.mark.asyncio
async def test_register_success(db_session: AsyncSession) -> None:
    """测试邀请码注册成功。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin_user.id, note="for register"
    )
    await db_session.commit()
    raw_code = results[0][1]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/register",
            json={
                "email": "newuser@example.com",
                "password": "newuser-password-123",
                "invite_code": raw_code,
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert "membership_started_at" in data
    assert "membership_expires_at" in data


@pytest.mark.asyncio
async def test_register_invalid_invite_code(db_session: AsyncSession) -> None:
    """测试无效邀请码注册失败。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/register",
            json={
                "email": "newuser@example.com",
                "password": "newuser-password-123",
                "invite_code": "INVALID-CODE-1234",
            },
        )
    assert response.status_code == 400
    assert "邀请码无效" in response.json()["detail"]


@pytest.mark.asyncio
async def test_register_used_invite_code(db_session: AsyncSession) -> None:
    """测试已使用邀请码注册失败。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin_user.id
    )
    await db_session.commit()
    raw_code = results[0][1]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 第一次注册成功
        await client.post(
            "/auth/register",
            json={
                "email": "user1@example.com",
                "password": "password-12345",
                "invite_code": raw_code,
            },
        )
        # 第二次使用同一邀请码注册失败
        response = await client.post(
            "/auth/register",
            json={
                "email": "user2@example.com",
                "password": "password-12345",
                "invite_code": raw_code,
            },
        )
    assert response.status_code == 400
    assert "已被使用" in response.json()["detail"]


@pytest.mark.asyncio
async def test_register_revoked_invite_code(db_session: AsyncSession) -> None:
    """测试已作废邀请码注册失败。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin_user.id
    )
    await db_session.commit()
    invite = results[0][0]
    raw_code = results[0][1]

    # 作废邀请码
    headers = _auth_headers(admin_user.id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        revoke_resp = await client.post(
            f"/admin/invite-codes/{invite.id}/revoke", headers=headers
        )
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["status"] == "revoked"

    # 使用已作废邀请码注册失败
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/register",
            json={
                "email": "newuser@example.com",
                "password": "newuser-password-123",
                "invite_code": raw_code,
            },
        )
    assert response.status_code == 400
    assert "已被作废" in response.json()["detail"]


@pytest.mark.asyncio
async def test_register_duplicate_email(db_session: AsyncSession) -> None:
    """测试邮箱已注册时注册失败。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=2, created_by=admin_user.id
    )
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 第一次注册成功
        await client.post(
            "/auth/register",
            json={
                "email": "dup@example.com",
                "password": "password-12345",
                "invite_code": results[0][1],
            },
        )
        # 第二次用同一邮箱注册失败
        response = await client.post(
            "/auth/register",
            json={
                "email": "dup@example.com",
                "password": "password-12345",
                "invite_code": results[1][1],
            },
        )
    assert response.status_code == 400
    assert "已被注册" in response.json()["detail"]


# ============================================================
# 登录会员到期拦截测试
# ============================================================


@pytest.mark.asyncio
async def test_login_membership_active(db_session: AsyncSession) -> None:
    """测试会员有效时登录返回 membership_expired=false。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin_user.id
    )
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 注册
        await client.post(
            "/auth/register",
            json={
                "email": "active@example.com",
                "password": "password-12345",
                "invite_code": results[0][1],
            },
        )
        # 登录
        response = await client.post(
            "/auth/login",
            json={"email": "active@example.com", "password": "password-12345"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["membership_expired"] is False


@pytest.mark.asyncio
async def test_login_membership_expired(db_session: AsyncSession) -> None:
    """测试会员到期后登录返回 membership_expired=true。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin_user.id
    )
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 注册
        reg_resp = await client.post(
            "/auth/register",
            json={
                "email": "expired@example.com",
                "password": "password-12345",
                "invite_code": results[0][1],
            },
        )
    assert reg_resp.status_code == 200

    # 手动将会员到期时间设为过去
    from sqlalchemy import select as sa_select

    # 通过 email 查找用户
    from app.models.user import User

    user_stmt = sa_select(User).where(User.email == "expired@example.com")
    user_result = await db_session.execute(user_stmt)
    user = user_result.scalar_one()

    membership_stmt = sa_select(Membership).where(Membership.user_id == user.id)
    membership_result = await db_session.execute(membership_stmt)
    membership = membership_result.scalar_one()
    membership.expires_at = datetime.now(UTC) - timedelta(days=1)
    membership.status = "expired"
    await db_session.commit()

    # 登录应返回 membership_expired=true
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "expired@example.com", "password": "password-12345"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["membership_expired"] is True


# ============================================================
# 会员状态查询测试
# ============================================================


@pytest.mark.asyncio
async def test_get_membership_status(db_session: AsyncSession) -> None:
    """测试查询会员状态。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin_user.id
    )
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 注册
        reg_resp = await client.post(
            "/auth/register",
            json={
                "email": "status@example.com",
                "password": "password-12345",
                "invite_code": results[0][1],
            },
        )
    assert reg_resp.status_code == 200
    token = reg_resp.json()["access_token"]

    # 查询会员状态
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/me/membership", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "active"
    assert data["remaining_days"] > 0
    assert data["renewal_count"] == 0


@pytest.mark.asyncio
async def test_get_membership_no_record(db_session: AsyncSession) -> None:
    """测试无会员记录的用户查询会员状态返回 404。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    headers = _auth_headers(admin_user.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/me/membership", headers=headers)
    assert response.status_code == 404


# ============================================================
# 邀请码续期测试
# ============================================================


@pytest.mark.asyncio
async def test_renew_membership_active(db_session: AsyncSession) -> None:
    """测试未到期续期 - 从当前到期日顺延 30 天。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=2, created_by=admin_user.id
    )
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 注册
        reg_resp = await client.post(
            "/auth/register",
            json={
                "email": "renew@example.com",
                "password": "password-12345",
                "invite_code": results[0][1],
            },
        )
    assert reg_resp.status_code == 200
    token = reg_resp.json()["access_token"]

    # 查询续期前的到期时间
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        before_resp = await client.get(
            "/me/membership", headers={"Authorization": f"Bearer {token}"}
        )
    before_expires = before_resp.json()["expires_at"]

    # 续期
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        renew_resp = await client.post(
            "/auth/renew",
            headers={"Authorization": f"Bearer {token}"},
            json={"invite_code": results[1][1]},
        )
    assert renew_resp.status_code == 200
    renew_data = renew_resp.json()
    assert renew_data["membership_status"] == "active"
    assert renew_data["old_expires_at"] is not None

    # 验证续期后到期时间顺延
    from datetime import datetime as dt

    old_expires = dt.fromisoformat(renew_data["old_expires_at"].replace("Z", "+00:00"))
    new_expires = dt.fromisoformat(renew_data["new_expires_at"].replace("Z", "+00:00"))
    diff = new_expires - old_expires
    assert abs(diff.days - 30) <= 1  # 允许 1 天误差（时区）


@pytest.mark.asyncio
async def test_renew_membership_expired(db_session: AsyncSession) -> None:
    """测试已到期续期 - 从兑换当天重新计算 30 天。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=2, created_by=admin_user.id
    )
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 注册
        reg_resp = await client.post(
            "/auth/register",
            json={
                "email": "renew2@example.com",
                "password": "password-12345",
                "invite_code": results[0][1],
            },
        )
    assert reg_resp.status_code == 200
    token = reg_resp.json()["access_token"]

    # 手动将会员到期时间设为过去
    from sqlalchemy import select as sa_select

    from app.models.user import User

    user_stmt = sa_select(User).where(User.email == "renew2@example.com")
    user_result = await db_session.execute(user_stmt)
    user = user_result.scalar_one()

    membership_stmt = sa_select(Membership).where(Membership.user_id == user.id)
    membership_result = await db_session.execute(membership_stmt)
    membership = membership_result.scalar_one()
    membership.expires_at = datetime.now(UTC) - timedelta(days=5)
    membership.status = "expired"
    await db_session.commit()

    # 续期
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        renew_resp = await client.post(
            "/auth/renew",
            headers={"Authorization": f"Bearer {token}"},
            json={"invite_code": results[1][1]},
        )
    assert renew_resp.status_code == 200
    renew_data = renew_resp.json()
    assert renew_data["membership_status"] == "active"
    assert renew_data["remaining_days"] > 0


# ============================================================
# 邀请码作废测试
# ============================================================


@pytest.mark.asyncio
async def test_revoke_invite_code(db_session: AsyncSession) -> None:
    """测试作废未使用邀请码。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin_user.id
    )
    await db_session.commit()
    invite = results[0][0]

    headers = _auth_headers(admin_user.id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/admin/invite-codes/{invite.id}/revoke", headers=headers
        )
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"


@pytest.mark.asyncio
async def test_revoke_used_invite_code_fails(db_session: AsyncSession) -> None:
    """测试作废已使用邀请码失败。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin_user.id
    )
    await db_session.commit()
    invite = results[0][0]
    raw_code = results[0][1]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 先注册使用邀请码
        await client.post(
            "/auth/register",
            json={
                "email": "used@example.com",
                "password": "password-12345",
                "invite_code": raw_code,
            },
        )
        # 尝试作废已使用邀请码
        headers = _auth_headers(admin_user.id)
        response = await client.post(
            f"/admin/invite-codes/{invite.id}/revoke", headers=headers
        )
    assert response.status_code == 400
    assert "仅未使用" in response.json()["detail"]


# ============================================================
# 管理员列表查询测试
# ============================================================


@pytest.mark.asyncio
async def test_list_invite_codes(db_session: AsyncSession) -> None:
    """测试查询邀请码列表。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    await generate_invite_codes(db=db_session, count=3, created_by=admin_user.id)
    await db_session.commit()

    headers = _auth_headers(admin_user.id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/invite-codes", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 3
    assert len(data["items"]) >= 3


@pytest.mark.asyncio
async def test_list_invite_codes_by_status(db_session: AsyncSession) -> None:
    """测试按状态筛选邀请码列表。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    await generate_invite_codes(db=db_session, count=2, created_by=admin_user.id)
    await db_session.commit()

    headers = _auth_headers(admin_user.id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/admin/invite-codes?status=unused", headers=headers
        )
    assert response.status_code == 200
    data = response.json()
    assert all(item["status"] == "unused" for item in data["items"])


@pytest.mark.asyncio
async def test_list_members(db_session: AsyncSession) -> None:
    """测试查询会员账户列表。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin_user.id
    )
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 注册一个新用户
        await client.post(
            "/auth/register",
            json={
                "email": "member@example.com",
                "password": "password-12345",
                "invite_code": results[0][1],
            },
        )

    # 查询会员列表
    headers = _auth_headers(admin_user.id)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/admin/members", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 2  # admin + 新注册用户
    # 验证新注册用户有会员信息
    member_emails = [m["email"] for m in data["items"]]
    assert "member@example.com" in member_emails


@pytest.mark.asyncio
async def test_get_member_redemptions(db_session: AsyncSession) -> None:
    """测试查询用户兑换记录。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin_user.id
    )
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 注册
        reg_resp = await client.post(
            "/auth/register",
            json={
                "email": "redemption@example.com",
                "password": "password-12345",
                "invite_code": results[0][1],
            },
        )
    assert reg_resp.status_code == 200

    # 查找用户
    from sqlalchemy import select as sa_select

    from app.models.user import User

    user_stmt = sa_select(User).where(User.email == "redemption@example.com")
    user_result = await db_session.execute(user_stmt)
    user = user_result.scalar_one()

    # 查询兑换记录
    headers = _auth_headers(admin_user.id)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/admin/members/{user.id}/redemptions", headers=headers
        )
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert data[0]["usage_type"] == "registration"


# ============================================================
# 邀请码哈希一致性测试
# ============================================================


def test_hash_invite_code_consistency() -> None:
    """测试邀请码哈希一致性（忽略大小写和空格）。"""
    code = "ABCD-EFGH-IJKL-MNOP"
    h1 = hash_invite_code(code)
    h2 = hash_invite_code(code.lower())
    h3 = hash_invite_code(f" {code} ")
    assert h1 == h2 == h3


def test_hash_invite_code_different() -> None:
    """测试不同邀请码哈希不同。"""
    h1 = hash_invite_code("ABCD-EFGH-IJKL-MNOP")
    h2 = hash_invite_code("DCBA-HGFE-LKJI-PONM")
    assert h1 != h2


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
