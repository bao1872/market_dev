"""登录接口专项测试 - 认证 + 会员状态 + 异常处理。

测试内容：
1. 正确账号密码登录成功（200）
2. 错误密码登录失败（401）
3. 不存在用户登录失败（401）
4. disabled 用户登录失败（401）
5. 无 membership 用户可登录且 membership_expired=true
6. expired membership 登录返回 membership_expired=true 且不修改 DB status
7. 数据库异常返回 500 且有日志
8. 密码 hash 格式异常返回 401

测试策略：
- 使用 sqlite 内存数据库 + 异步 SQLAlchemy（与 test_auth.py 一致，避免依赖外部 PG）
- 注册 JSONB 类型在 SQLite 上的编译回退
- 通过 dependency_overrides 注入测试会话
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
from app.models.membership import Membership
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
    """创建内存 SQLite 异步会话，测试后销毁。"""
    try:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    except Exception:
        pytest.skip("aiosqlite 不可用，跳过 DB 测试")

    async with engine.begin() as conn:
        for ddl_stmt in _SQLITE_DDL_STATEMENTS:
            await conn.execute(text(ddl_stmt))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        user_role = Role(id=uuid.uuid4(), name="user", description="普通用户")
        session.add(user_role)
        await session.commit()

        async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
            yield session

        from app.core.deps import get_db as deps_get_db
        from app.db import get_db as db_get_db
        app.dependency_overrides[deps_get_db] = get_test_db
        app.dependency_overrides[db_get_db] = get_test_db

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


async def _create_membership(
    session: AsyncSession,
    user_id: uuid.UUID,
    status: str,
    expires_at: datetime,
) -> Membership:
    """在测试会话中创建会员记录。"""
    now = datetime.now(UTC)
    membership = Membership(
        user_id=user_id,
        status=status,
        started_at=now - timedelta(days=30),
        expires_at=expires_at,
        plan_code="observe_20",
        monitor_limit=20,
        updated_at=now,
    )
    session.add(membership)
    await session.flush()
    return membership


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
async def test_login_without_membership_expired_true(db_session: AsyncSession) -> None:
    """无 membership 用户可登录，且 membership_expired=true。"""
    await _create_user(db_session, "no_member@example.com", "password123")
    await db_session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"email": "no_member@example.com", "password": "password123"},
        )
    assert response.status_code == 200
    assert response.json()["membership_expired"] is True


@pytest.mark.asyncio
async def test_login_expired_membership_not_modify_status(db_session: AsyncSession) -> None:
    """expired membership 登录返回 membership_expired=true，且不修改 DB status。"""
    user = await _create_user(db_session, "expired_member@example.com", "password123")
    membership = await _create_membership(
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
    assert response.json()["membership_expired"] is True

    # 刷新会话，重新查询 membership status，确认未被 login 改为 expired
    await db_session.refresh(membership)
    assert membership.status == "active"


@pytest.mark.asyncio
async def test_login_db_error_returns_500(db_session: AsyncSession, caplog) -> None:
    """数据库异常返回 500，并记录日志。"""
    await _create_user(db_session, "db_error@example.com", "password123")
    await db_session.commit()

    with patch("app.api.auth.get_effective_membership_status") as mock_status:
        mock_status.side_effect = SQLAlchemyError("simulated db failure")

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


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
