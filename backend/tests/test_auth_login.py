"""登录接口专项测试 - 认证 + AccessProfile 登录响应 + 异常处理。

测试内容：
1. 正确账号密码登录成功（200）
2. 错误密码登录失败（401）
3. 不存在用户登录失败（401）
4. disabled 用户登录失败（401）
5. 登录响应包含全部 AccessProfile 字段（10 字段 + 4 token 字段）
6. admin / member-active / member-expired 三种 next_route 路由
7. admin 的 subscription_required=False，member 的 subscription_required=True
8. 响应不再包含 membership_expired 字段（已被 subscription_active 替代）
9. 数据库异常返回 500 且有日志
10. 密码 hash 格式异常返回 401

测试策略：
- 使用 sqlite 内存数据库 + 异步 SQLAlchemy（与 test_auth.py 一致，避免依赖外部 PG）
- 注册 JSONB 类型在 SQLite 上的编译回退
- 通过 dependency_overrides 注入测试会话
- fixture 初始化 plans 表 + admin/user 双角色 + admin 用户
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.core.security import get_password_hash
from app.main import app
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole


# 注册 JSONB 在 SQLite 上的编译回退（测试环境兼容）
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element: JSONB, compiler: object, **kw: object) -> str:  # noqa: ARG001
    """JSONB 在 SQLite 上回退为 TEXT 类型。"""
    return "TEXT"


# SQLite 兼容的建表 DDL（绕过 PostgreSQL 特有的 server_default）
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
CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE,
    plan_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    starts_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL,
    entitlement_snapshot TEXT,
    source TEXT NOT NULL DEFAULT 'invite',
    created_by TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES users(id)
)
""",
    """
CREATE TABLE IF NOT EXISTS plans (
    id TEXT NOT NULL PRIMARY KEY,
    plan_code TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    monitor_limit INTEGER NOT NULL,
    notification_channel_limit INTEGER NOT NULL DEFAULT 1,
    message_retention_days INTEGER NOT NULL DEFAULT 30,
    features TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME
)
""",
]

# plans 表初始套餐数据（与 048_plans_table 迁移保持一致）
# SQLite 无 gen_random_uuid()，显式提供 id（测试用固定 UUID）
_PLANS_SEED_SQL = """
INSERT INTO plans (id, plan_code, display_name, monitor_limit,
    notification_channel_limit, message_retention_days, features, status)
VALUES
    ('00000000-0000-0000-0000-000000000001', 'observe_20', '观察版', 20, 1, 30,
     '["trend_selection","stock_detail","node_monitor","in_app_message","feishu_notification","stock_memo"]',
     'active'),
    ('00000000-0000-0000-0000-000000000002', 'research_50', '研究版', 50, 3, 180,
     '["trend_selection","stock_detail","node_monitor","in_app_message","feishu_notification","stock_memo","advanced_export"]',
     'active')
"""


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """创建内存 SQLite 异步会话，测试后销毁。"""
    try:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    except Exception:
        pytest.skip("aiosqlite 不可用，跳过 DB 测试")

    async with engine.begin() as conn:
        for ddl_stmt in _SQLITE_DDL_STATEMENTS:
            await conn.execute(text(ddl_stmt))
        # 插入 plans 表初始套餐数据（get_access_context 通过 plan_service.get_plan 查询 plans 表）
        await conn.execute(text(_PLANS_SEED_SQL))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        # [Test] - 描述: 初始化 admin/user 双角色 + admin 用户（用于 AccessProfile 测试）
        admin_role = Role(id=uuid.uuid4(), name="admin", description="管理员")
        user_role = Role(id=uuid.uuid4(), name="user", description="普通用户")
        session.add(admin_role)
        session.add(user_role)

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


async def _create_user(
    session: AsyncSession,
    email: str,
    password: str,
    status: str = "active",
) -> User:
    """在测试会话中创建用户。"""
    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=get_password_hash(password),
        status=status,
        timezone="Asia/Shanghai",
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    await session.flush()

    user_role = session._test_user_role  # type: ignore[attr-defined]
    session.add(UserRole(user_id=user.id, role_id=user_role.id))
    await session.flush()
    return user


async def _create_subscription(
    session: AsyncSession,
    user_id: uuid.UUID,
    status: str,
    expires_at: datetime,
) -> Subscription:
    """在测试会话中创建订阅记录。"""
    now = datetime.now(UTC)
    subscription = Subscription(
        user_id=user_id,
        plan_code="observe_20",
        status=status,
        starts_at=now - timedelta(days=30),
        expires_at=expires_at,
        entitlement_snapshot={"monitor_limit": 20},
        source="invite",
        created_by=None,
    )
    session.add(subscription)
    await session.flush()
    return subscription


@pytest.mark.asyncio
async def test_login_success(db_session: AsyncSession) -> None:
    """正确账号密码登录成功，返回 token。"""
    await _create_user(db_session, "login_ok@example.com", "password123")
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "login_ok@example.com", "password": "password123"},
        )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0


@pytest.mark.asyncio
async def test_login_wrong_password(db_session: AsyncSession) -> None:
    """错误密码登录失败（401）。"""
    await _create_user(db_session, "wrong_pwd@example.com", "password123")
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "wrong_pwd@example.com", "password": "wrong-password"},
        )
    assert response.status_code == 401
    assert "邮箱或密码错误" in response.json()["detail"]


@pytest.mark.asyncio
async def test_login_nonexistent_user(db_session: AsyncSession) -> None:
    """不存在用户登录失败（401，统一错误信息）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": "any-password-123"},
        )
    assert response.status_code == 401
    assert "邮箱或密码错误" in response.json()["detail"]


@pytest.mark.asyncio
async def test_login_disabled_user(db_session: AsyncSession) -> None:
    """disabled 用户登录失败（401）。"""
    await _create_user(db_session, "disabled_login@example.com", "password123", status="disabled")
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "disabled_login@example.com", "password": "password123"},
        )
    assert response.status_code == 401
    assert "非 active" in response.json()["detail"]


@pytest.mark.asyncio
async def test_login_without_membership_subscription_active_false(db_session: AsyncSession) -> None:
    """无 subscription 用户可登录，且 subscription_active=False（无订阅记录）。"""
    await _create_user(db_session, "no_member@example.com", "password123")
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "no_member@example.com", "password": "password123"},
        )
    assert response.status_code == 200
    data = response.json()
    # [Auth] - 描述: 无订阅记录的 member，subscription_active=False（替代旧 membership_expired=true）
    assert data["subscription_active"] is False
    assert data["next_route"] == "/membership-expired"


@pytest.mark.asyncio
async def test_login_expired_subscription_not_modify_status(db_session: AsyncSession) -> None:
    """expired subscription 登录返回 subscription_active=False，且不修改 DB status。"""
    user = await _create_user(db_session, "expired_member@example.com", "password123")
    subscription = await _create_subscription(
        db_session,
        user.id,
        status="active",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "expired_member@example.com", "password": "password123"},
        )
    assert response.status_code == 200
    data = response.json()
    # [Auth] - 描述: 过期订阅 subscription_active=False（替代旧 membership_expired=true）
    assert data["subscription_active"] is False
    assert data["next_route"] == "/membership-expired"

    # 刷新会话，重新查询 subscription status，确认未被 login 改为 expired
    await db_session.refresh(subscription)
    assert subscription.status == "active"


@pytest.mark.asyncio
async def test_login_db_error_returns_500(db_session: AsyncSession, caplog) -> None:
    """数据库异常返回 500，并记录日志。"""
    await _create_user(db_session, "db_error@example.com", "password123")
    await db_session.commit()

    # [Auth] - 描述: login 调用 get_access_context，patch 该函数模拟 DB 异常
    with patch("app.api.auth.get_access_context") as mock_ctx:
        mock_ctx.side_effect = SQLAlchemyError("simulated db failure")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/auth/login",
                json={"email": "db_error@example.com", "password": "password123"},
            )

    assert response.status_code == 500
    assert "登录服务暂不可用" in response.json()["detail"]
    assert any("登录失败" in record.message for record in caplog.records)
    assert any("db_error@example.com" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_login_invalid_password_hash_returns_401(db_session: AsyncSession) -> None:
    """密码 hash 格式异常返回 401。"""
    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),
        email="bad_hash@example.com",
        password_hash="not-a-valid-bcrypt-hash",
        status="active",
        timezone="Asia/Shanghai",
        created_at=now,
        updated_at=now,
    )
    db_session.add(user)
    await db_session.flush()

    user_role = db_session._test_user_role  # type: ignore[attr-defined]
    db_session.add(UserRole(user_id=user.id, role_id=user_role.id))
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "bad_hash@example.com", "password": "any-password"},
        )
    assert response.status_code == 401


# ============================================================
# AccessProfile 登录响应测试（Phase 2 Task 2.4）
# 验证登录响应包含 4 个 token 字段 + 10 个 AccessProfile 字段
# next_route 逻辑：admin → /admin/overview；member active → /overview；
#                  member expired → /membership-expired
# ============================================================

# 期望的 4 个 token 字段
_TOKEN_FIELDS = {"access_token", "refresh_token", "token_type", "expires_in"}

# 期望的 10 个 AccessProfile 字段
_ACCESS_PROFILE_FIELDS = {
    "is_admin",
    "roles",
    "subscription_required",
    "subscription_active",
    "plan_code",
    "plan_display_name",
    "expires_at",
    "features",
    "limits",
    "next_route",
}


@pytest.mark.asyncio
async def test_login_response_contains_access_profile_fields(db_session: AsyncSession) -> None:
    """登录成功后响应 JSON 包含全部 10 个 AccessProfile 字段 + 4 个 token 字段。"""
    # 使用 fixture 预创建的 admin 用户登录
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "admin-password-123"},
        )
    assert response.status_code == 200
    data = response.json()
    expected_fields = _TOKEN_FIELDS | _ACCESS_PROFILE_FIELDS
    assert set(data.keys()) == expected_fields, (
        f"响应字段不匹配，缺失: {expected_fields - set(data.keys())}，"
        f"多余: {set(data.keys()) - expected_fields}"
    )


@pytest.mark.asyncio
async def test_login_response_admin_next_route(db_session: AsyncSession) -> None:
    """admin 登录 next_route='/admin/overview'。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "admin-password-123"},
        )
    assert response.status_code == 200
    assert response.json()["next_route"] == "/admin/overview"


@pytest.mark.asyncio
async def test_login_response_member_active_next_route(db_session: AsyncSession) -> None:
    """member 有效订阅 next_route='/overview'。"""
    # _create_user 显式设置 id=uuid.uuid4() 并返回 user 对象，可直接使用 user.id
    user = await _create_user(db_session, "member_active@example.com", "password123")
    await _create_subscription(
        db_session,
        user_id=user.id,
        status="active",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "member_active@example.com", "password": "password123"},
        )
    assert response.status_code == 200
    assert response.json()["next_route"] == "/overview"


@pytest.mark.asyncio
async def test_login_response_member_expired_next_route(db_session: AsyncSession) -> None:
    """member 订阅过期 next_route='/membership-expired'。"""
    user = await _create_user(db_session, "member_expired@example.com", "password123")
    await _create_subscription(
        db_session,
        user_id=user.id,
        status="active",  # DB 中仍为 active，但 expires_at 已过，get_access_context 实时计算为 expired
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "member_expired@example.com", "password": "password123"},
        )
    assert response.status_code == 200
    assert response.json()["next_route"] == "/membership-expired"


@pytest.mark.asyncio
async def test_login_response_no_membership_expired_field(db_session: AsyncSession) -> None:
    """响应不再包含 membership_expired 字段（已被 subscription_active 替代）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "admin-password-123"},
        )
    assert response.status_code == 200
    assert "membership_expired" not in response.json()


@pytest.mark.asyncio
async def test_login_response_admin_subscription_required_false(db_session: AsyncSession) -> None:
    """admin 的 subscription_required=False（admin 不需要订阅）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "admin-password-123"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["subscription_required"] is False
    assert data["is_admin"] is True


@pytest.mark.asyncio
async def test_login_response_member_subscription_required_true(db_session: AsyncSession) -> None:
    """member 的 subscription_required=True（member 需要订阅）。"""
    user = await _create_user(db_session, "member_req@example.com", "password123")
    await _create_subscription(
        db_session,
        user_id=user.id,
        status="active",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "member_req@example.com", "password": "password123"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["subscription_required"] is True
    assert data["is_admin"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
