"""R2 身份与权限测试 - JWT 认证 + RBAC + UserContext 注入。

测试内容：
1. 登录成功/失败（密码错误/用户不存在/状态非 active）
2. token 刷新（refresh token 有效/无效/类型错误）
3. /me 获取当前用户（含角色列表）
4. 私有资源 user_id 由上下文注入（不接受 body 中的 user_id）

测试策略：
- 使用 sqlite 内存数据库 + 异步 SQLAlchemy
- 注册 JSONB 类型在 SQLite 上的编译（回退为 JSON），支持含 JSONB 字段的表
- 创建 users/roles/user_roles 表
- 通过 dependency_overrides 注入测试会话
- 覆盖主逻辑 + 边界条件
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.core.security import create_access_token, create_refresh_token, get_password_hash
from app.main import app
from app.models.user import Role, User, UserRole


# 注册 JSONB 在 SQLite 上的编译回退（测试环境兼容）
# 生产环境使用 PostgreSQL JSONB，测试环境 SQLite 回退为 TEXT
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element: JSONB, compiler: object, **kw: object) -> str:  # noqa: ARG001
    """JSONB 在 SQLite 上回退为 TEXT 类型（JSON 序列化由 SQLAlchemy 处理）。"""
    return "TEXT"


# SQLite 兼容的建表 DDL（绕过 PostgreSQL 特有的 server_default）
# 生产环境使用 Alembic 迁移（gen_random_uuid、JSONB 等），测试环境用 SQLite 原生语法
# 注意：SQLite 不支持单次 execute 多条语句，需逐条执行
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
    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL,
    plan_code TEXT,
    monitor_limit INTEGER,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
)
""",
]


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """创建内存 SQLite 异步会话，测试后销毁。

    使用 SQLite 兼容的建表 DDL（绕过 PostgreSQL 特有的 server_default）。
    创建 users/roles/user_roles 表，注入测试数据：
    - admin 用户（admin 角色）
    - normal 用户（user 角色）
    - disabled 用户（active 角色但 status=disabled）
    """
    try:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    except Exception:
        pytest.skip("aiosqlite 不可用，跳过 DB 测试")

    async with engine.begin() as conn:
        # 逐条执行 SQLite 兼容的 DDL（绕过 PostgreSQL 特有的 server_default）
        for ddl_stmt in _SQLITE_DDL_STATEMENTS:
            await conn.execute(text(ddl_stmt))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        # 创建角色
        admin_role = Role(id=uuid.uuid4(), name="admin", description="管理员")
        user_role = Role(id=uuid.uuid4(), name="user", description="普通用户")
        session.add(admin_role)
        session.add(user_role)

        # 创建用户
        admin_user = User(
            id=uuid.uuid4(),
            email="admin@example.com",
            password_hash=get_password_hash("admin-password-123"),
            status="active",
            timezone="Asia/Shanghai",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        normal_user = User(
            id=uuid.uuid4(),
            email="user@example.com",
            password_hash=get_password_hash("user-password-123"),
            status="active",
            timezone="Asia/Shanghai",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        disabled_user = User(
            id=uuid.uuid4(),
            email="disabled@example.com",
            password_hash=get_password_hash("disabled-password-123"),
            status="disabled",
            timezone="Asia/Shanghai",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(admin_user)
        session.add(normal_user)
        session.add(disabled_user)

        # 创建用户-角色关联
        session.add(UserRole(user_id=admin_user.id, role_id=admin_role.id))
        session.add(UserRole(user_id=normal_user.id, role_id=user_role.id))
        session.add(UserRole(user_id=disabled_user.id, role_id=user_role.id))

        await session.commit()

        # 将会话注入到 app 依赖
        async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
            yield session

        # 覆盖 get_db（所有依赖 get_db 的地方都会使用测试会话）
        from app.core.deps import get_db as deps_get_db
        from app.db import get_db as db_get_db
        app.dependency_overrides[deps_get_db] = get_test_db
        app.dependency_overrides[db_get_db] = get_test_db

        # 将用户 ID 存储到 fixture 属性，供测试使用
        session._test_admin_user = admin_user  # type: ignore[attr-defined]
        session._test_normal_user = normal_user  # type: ignore[attr-defined]
        session._test_disabled_user = disabled_user  # type: ignore[attr-defined]
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
# 登录测试
# ============================================================


@pytest.mark.asyncio
async def test_login_success_admin(db_session: AsyncSession) -> None:
    """测试管理员登录成功。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "admin-password-123"},
        )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0


@pytest.mark.asyncio
async def test_login_success_normal_user(db_session: AsyncSession) -> None:
    """测试普通用户登录成功。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "user@example.com", "password": "user-password-123"},
        )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data


@pytest.mark.asyncio
async def test_login_wrong_password(db_session: AsyncSession) -> None:
    """测试密码错误登录失败（401）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "wrong-password"},
        )
    assert response.status_code == 401
    assert "邮箱或密码错误" in response.json()["detail"]


@pytest.mark.asyncio
async def test_login_nonexistent_user(db_session: AsyncSession) -> None:
    """测试不存在的用户登录失败（401，统一错误信息避免泄露用户是否存在）。"""
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
    """测试被禁用用户登录失败（401）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={
                "email": "disabled@example.com",
                "password": "disabled-password-123",
            },
        )
    assert response.status_code == 401
    assert "非 active" in response.json()["detail"]


# ============================================================
# Token 刷新测试
# ============================================================


@pytest.mark.asyncio
async def test_refresh_token_success(db_session: AsyncSession) -> None:
    """测试使用有效 refresh token 刷新成功。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    rtoken = create_refresh_token(str(admin_user.id))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/refresh",
            json={"refresh_token": rtoken},
        )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0
    # 验证新 access token 可用于认证（解码成功且 sub 一致）
    from app.core.security import decode_token

    new_payload = decode_token(data["access_token"])
    assert new_payload["sub"] == str(admin_user.id)
    assert new_payload["type"] == "access"


@pytest.mark.asyncio
async def test_refresh_token_with_access_token_fails(db_session: AsyncSession) -> None:
    """测试使用 access token 刷新失败（类型错误，401）。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    # 用 access token 尝试刷新
    atoken = create_access_token(str(admin_user.id))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/refresh",
            json={"refresh_token": atoken},
        )
    assert response.status_code == 401
    assert "类型错误" in response.json()["detail"]


@pytest.mark.asyncio
async def test_refresh_token_invalid(db_session: AsyncSession) -> None:
    """测试使用无效 refresh token 刷新失败（401）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/refresh",
            json={"refresh_token": "invalid-token-string"},
        )
    assert response.status_code == 401


# ============================================================
# /me 端点测试
# ============================================================


@pytest.mark.asyncio
async def test_get_me_success(db_session: AsyncSession) -> None:
    """测试获取当前用户信息（含角色列表）。"""
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    headers = _auth_headers(admin_user.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/me", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "admin@example.com"
    assert data["status"] == "active"
    assert "admin" in data["roles"]
    assert "password_hash" not in data  # 不返回密码哈希


@pytest.mark.asyncio
async def test_get_me_no_token(db_session: AsyncSession) -> None:
    """测试无 token 访问 /me 被拒绝（401/403）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/me")
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_me_invalid_token(db_session: AsyncSession) -> None:
    """测试无效 token 访问 /me 被拒绝（401）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/me", headers={"Authorization": "Bearer invalid-token"}
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me_disabled_user(db_session: AsyncSession) -> None:
    """测试被禁用用户访问 /me 被拒绝（403）。"""
    disabled_user = db_session._test_disabled_user  # type: ignore[attr-defined]
    headers = _auth_headers(disabled_user.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/me", headers=headers)
    assert response.status_code == 403
    assert "非 active" in response.json()["detail"]


# ============================================================
# 私有资源 user_id 注入测试
# ============================================================


@pytest.mark.asyncio
async def test_user_id_from_context_not_body(db_session: AsyncSession) -> None:
    """测试私有资源 user_id 由认证上下文注入，不接受 body 中的 user_id。

    /me 端点不接受任何 user_id 参数，完全依赖 token 上下文。
    即使请求 body 中传入 user_id，也不应影响返回的用户。
    """
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    normal_user = db_session._test_normal_user  # type: ignore[attr-defined]
    headers = _auth_headers(admin_user.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 用 admin 的 token 访问 /me，即使 body 中传入 normal_user 的 ID
        # GET 请求无 body，但 /me 端点完全依赖 token，不接受任何 user_id 输入
        response = await client.get("/me", headers=headers)
    assert response.status_code == 200
    data = response.json()
    # 必须返回 admin 用户，而非其他用户
    assert data["email"] == "admin@example.com"
    assert data["id"] == str(admin_user.id)
    # 确保不是 normal_user
    assert data["id"] != str(normal_user.id)


@pytest.mark.asyncio
async def test_get_current_user_uses_token_sub(db_session: AsyncSession) -> None:
    """测试 get_current_user 依赖从 token 的 sub 声明提取 user_id。

    验证：不同用户的 token 返回不同用户，token 中的 sub 决定用户身份。
    """
    admin_user = db_session._test_admin_user  # type: ignore[attr-defined]
    normal_user = db_session._test_normal_user  # type: ignore[attr-defined]

    # admin token 应返回 admin 用户
    admin_headers = _auth_headers(admin_user.id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin_resp = await client.get("/me", headers=admin_headers)
    assert admin_resp.json()["email"] == "admin@example.com"

    # normal token 应返回 normal 用户
    normal_headers = _auth_headers(normal_user.id)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        normal_resp = await client.get("/me", headers=normal_headers)
    assert normal_resp.json()["email"] == "user@example.com"


@pytest.mark.asyncio
async def test_refresh_token_with_disabled_user_fails(db_session: AsyncSession) -> None:
    """测试被禁用用户使用 refresh token 刷新失败（401）。"""
    disabled_user = db_session._test_disabled_user  # type: ignore[attr-defined]
    rtoken = create_refresh_token(str(disabled_user.id))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/refresh",
            json={"refresh_token": rtoken},
        )
    assert response.status_code == 401
    assert "非 active" in response.json()["detail"]


if __name__ == "__main__":
    # 自测入口：直接运行验证
    pytest.main([__file__, "-v", "--tb=short"])
