"""策略 admin 端点认证测试（Phase 3 Task 3.4）。

安全约束：
- /admin/strategies 系列端点必须要求 admin 角色
- 未认证 → 401，认证但无 admin 角色 → 403

测试覆盖 3 个端点 × 2 种非法访问 = 6 个用例：
1. POST /admin/strategies（创建策略）
2. POST /admin/strategies/{key}/versions/{version}/release（发布版本）
3. POST /admin/strategies/{key}/versions/{version}/archive（归档版本）

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库 bz_stock_test）
- 通过 dependency_overrides 注入测试 session 到 app
- 使用 ASGITransport + AsyncClient 调用真实 HTTP 端点
- 复用 test_capture_token_isolation.py 的用户/角色创建模式
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.main import app
from app.models.user import Role, User, UserRole
from tests.conftest import make_asgi_transport


async def _ensure_role(db: AsyncSession, name: str) -> Role:
    """确保角色存在并返回（幂等）。"""
    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _create_normal_user(db: AsyncSession) -> User:
    """创建 active 状态的普通用户（仅 member 角色，无 admin 角色）。"""
    user = User(
        id=uuid.uuid4(),
        email=f"strat_auth_{uuid.uuid4().hex[:8]}@test.com",
        password_hash="$2b$12$dummyhash",
        status="active",
        timezone="Asia/Shanghai",
    )
    db.add(user)
    user_role = await _ensure_role(db, "member")
    db.add(UserRole(user_id=user.id, role_id=user_role.id))
    await db.flush()
    return user


@pytest_asyncio.fixture
async def strategies_auth_client(
    db_session: AsyncSession,
) -> AsyncGenerator[tuple[AsyncClient, User], None]:
    """提供 HTTP 客户端 + 普通用户，通过 dependency_overrides 注入测试 session。

    覆盖 app.core.deps.get_db 与 app.db.get_db 两个入口，确保路由拿到的 session
    与 fixture 中操作的是同一事务（测试后由 db_session fixture 回滚）。
    """
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[deps_get_db] = get_test_db
    app.dependency_overrides[db_get_db] = get_test_db

    normal_user = await _create_normal_user(db_session)
    await db_session.flush()

    transport = make_asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, normal_user

    app.dependency_overrides.clear()


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 access token 的 Bearer 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# POST /admin/strategies - 创建策略
# ============================================================


@pytest.mark.asyncio
async def test_create_strategy_requires_admin(
    strategies_auth_client: tuple[AsyncClient, User],
) -> None:
    """未认证 POST /admin/strategies 应返回 401。

    [Security] - 描述: 创建策略端点必须要求 admin 角色，未认证拒绝
    """
    client, _ = strategies_auth_client
    response = await client.post(
        "/admin/strategies",
        json={"manifest": {}},
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_create_strategy_rejects_non_admin(
    strategies_auth_client: tuple[AsyncClient, User],
) -> None:
    """普通用户 POST /admin/strategies 应返回 403。

    [Security] - 描述: 普通用户（user 角色）无 admin 权限，禁止创建策略
    """
    client, normal_user = strategies_auth_client
    response = await client.post(
        "/admin/strategies",
        json={"manifest": {}},
        headers=_auth_headers(normal_user.id),
    )
    assert response.status_code == 403, response.text


# ============================================================
# POST /admin/strategies/{key}/versions/{version}/release - 发布版本
# ============================================================


@pytest.mark.asyncio
async def test_release_strategy_version_requires_admin(
    strategies_auth_client: tuple[AsyncClient, User],
) -> None:
    """未认证 POST /admin/strategies/{key}/versions/{v}/release 应返回 401。

    [Security] - 描述: 发布策略版本端点必须要求 admin 角色，未认证拒绝
    """
    client, _ = strategies_auth_client
    response = await client.post(
        "/admin/strategies/test_key/versions/1.0.0/release",
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_release_strategy_version_rejects_non_admin(
    strategies_auth_client: tuple[AsyncClient, User],
) -> None:
    """普通用户 POST /admin/strategies/{key}/versions/{v}/release 应返回 403。

    [Security] - 描述: 普通用户无 admin 权限，禁止发布策略版本
    """
    client, normal_user = strategies_auth_client
    response = await client.post(
        "/admin/strategies/test_key/versions/1.0.0/release",
        headers=_auth_headers(normal_user.id),
    )
    assert response.status_code == 403, response.text


# ============================================================
# POST /admin/strategies/{key}/versions/{version}/archive - 归档版本
# ============================================================


@pytest.mark.asyncio
async def test_archive_strategy_version_requires_admin(
    strategies_auth_client: tuple[AsyncClient, User],
) -> None:
    """未认证 POST /admin/strategies/{key}/versions/{v}/archive 应返回 401。

    [Security] - 描述: 归档策略版本端点必须要求 admin 角色，未认证拒绝
    """
    client, _ = strategies_auth_client
    response = await client.post(
        "/admin/strategies/test_key/versions/1.0.0/archive",
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_archive_strategy_version_rejects_non_admin(
    strategies_auth_client: tuple[AsyncClient, User],
) -> None:
    """普通用户 POST /admin/strategies/{key}/versions/{v}/archive 应返回 403。

    [Security] - 描述: 普通用户无 admin 权限，禁止归档策略版本
    """
    client, normal_user = strategies_auth_client
    response = await client.post(
        "/admin/strategies/test_key/versions/1.0.0/archive",
        headers=_auth_headers(normal_user.id),
    )
    assert response.status_code == 403, response.text
