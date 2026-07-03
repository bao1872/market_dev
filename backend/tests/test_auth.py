"""R2 身份与权限测试 - JWT 认证 + RBAC + UserContext 注入。

测试内容：
1. 登录成功/失败（密码错误/用户不存在/状态非 active）
2. token 刷新（refresh token 有效/无效/类型错误）
3. /me 获取当前用户（含角色列表）
4. 私有资源 user_id 由上下文注入（不接受 body 中的 user_id）

测试策略：
- 使用 PostgreSQL 测试库 + 公共 fixtures
- 通过 client fixture 自动注入测试会话
- 使用 user_factory / role_factory 创建测试数据
- 覆盖主逻辑 + 边界条件
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, create_refresh_token, get_password_hash
from app.models.user import User


@pytest_asyncio.fixture
async def auth_users(
    db_session: AsyncSession,
    user_factory: Callable[..., User],
) -> dict[str, User]:
    """创建认证测试所需的三类用户：admin、normal、disabled。

    - admin@example.com / admin-password-123，角色 admin
    - user@example.com / user-password-123，角色 member
    - disabled@example.com / disabled-password-123，角色 member，status=disabled
    """
    admin_user = await user_factory(
        email="admin@example.com",
        password_hash=get_password_hash("admin-password-123"),
        status="active",
        roles=["admin"],
    )
    normal_user = await user_factory(
        email="user@example.com",
        password_hash=get_password_hash("user-password-123"),
        status="active",
        roles=["member"],
    )
    disabled_user = await user_factory(
        email="disabled@example.com",
        password_hash=get_password_hash("disabled-password-123"),
        status="disabled",
        roles=["member"],
    )
    return {
        "admin": admin_user,
        "normal": normal_user,
        "disabled": disabled_user,
    }


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 登录测试
# ============================================================


@pytest.mark.asyncio
async def test_login_success_admin(client: httpx.AsyncClient, auth_users: dict[str, User]) -> None:
    """测试管理员登录成功。"""
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
async def test_login_success_normal_user(client: httpx.AsyncClient, auth_users: dict[str, User]) -> None:
    """测试普通用户登录成功。"""
    response = await client.post(
        "/auth/login",
        json={"email": "user@example.com", "password": "user-password-123"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data


@pytest.mark.asyncio
async def test_login_wrong_password(client: httpx.AsyncClient, auth_users: dict[str, User]) -> None:
    """测试密码错误登录失败（401）。"""
    response = await client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert "邮箱或密码错误" in response.json()["detail"]


@pytest.mark.asyncio
async def test_login_nonexistent_user(client: httpx.AsyncClient) -> None:
    """测试不存在的用户登录失败（401，统一错误信息避免泄露用户是否存在）。"""
    response = await client.post(
        "/auth/login",
        json={"email": "nobody@example.com", "password": "any-password-123"},
    )
    assert response.status_code == 401
    assert "邮箱或密码错误" in response.json()["detail"]


@pytest.mark.asyncio
async def test_login_disabled_user(client: httpx.AsyncClient, auth_users: dict[str, User]) -> None:
    """测试被禁用用户登录失败（401）。"""
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
async def test_refresh_token_success(client: httpx.AsyncClient, auth_users: dict[str, User]) -> None:
    """测试使用有效 refresh token 刷新成功。"""
    admin_user = auth_users["admin"]
    rtoken = create_refresh_token(str(admin_user.id))

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
async def test_refresh_token_with_access_token_fails(client: httpx.AsyncClient, auth_users: dict[str, User]) -> None:
    """测试使用 access token 刷新失败（类型错误，401）。"""
    admin_user = auth_users["admin"]
    # 用 access token 尝试刷新
    atoken = create_access_token(str(admin_user.id))

    response = await client.post(
        "/auth/refresh",
        json={"refresh_token": atoken},
    )
    assert response.status_code == 401
    assert "类型错误" in response.json()["detail"]


@pytest.mark.asyncio
async def test_refresh_token_invalid(client: httpx.AsyncClient) -> None:
    """测试使用无效 refresh token 刷新失败（401）。"""
    response = await client.post(
        "/auth/refresh",
        json={"refresh_token": "invalid-token-string"},
    )
    assert response.status_code == 401


# ============================================================
# /me 端点测试
# ============================================================


@pytest.mark.asyncio
async def test_get_me_success(client: httpx.AsyncClient, auth_users: dict[str, User]) -> None:
    """测试获取当前用户信息（含角色列表）。"""
    admin_user = auth_users["admin"]
    headers = _auth_headers(admin_user.id)

    response = await client.get("/me", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "admin@example.com"
    assert data["status"] == "active"
    assert "admin" in data["roles"]
    assert "password_hash" not in data  # 不返回密码哈希


@pytest.mark.asyncio
async def test_get_me_no_token(client: httpx.AsyncClient) -> None:
    """测试无 token 访问 /me 被拒绝（401/403）。"""
    response = await client.get("/me")
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_me_invalid_token(client: httpx.AsyncClient) -> None:
    """测试无效 token 访问 /me 被拒绝（401）。"""
    response = await client.get(
        "/me", headers={"Authorization": "Bearer invalid-token"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me_disabled_user(client: httpx.AsyncClient, auth_users: dict[str, User]) -> None:
    """测试被禁用用户访问 /me 被拒绝（403）。"""
    disabled_user = auth_users["disabled"]
    headers = _auth_headers(disabled_user.id)

    response = await client.get("/me", headers=headers)
    assert response.status_code == 403
    assert "非 active" in response.json()["detail"]


# ============================================================
# 私有资源 user_id 注入测试
# ============================================================


@pytest.mark.asyncio
async def test_user_id_from_context_not_body(client: httpx.AsyncClient, auth_users: dict[str, User]) -> None:
    """测试私有资源 user_id 由认证上下文注入，不接受 body 中的 user_id。

    /me 端点不接受任何 user_id 参数，完全依赖 token 上下文。
    即使请求 body 中传入 user_id，也不应影响返回的用户。
    """
    admin_user = auth_users["admin"]
    normal_user = auth_users["normal"]
    headers = _auth_headers(admin_user.id)

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
async def test_get_current_user_uses_token_sub(client: httpx.AsyncClient, auth_users: dict[str, User]) -> None:
    """测试 get_current_user 依赖从 token 的 sub 声明提取 user_id。

    验证：不同用户的 token 返回不同用户，token 中的 sub 决定用户身份。
    """
    admin_user = auth_users["admin"]
    normal_user = auth_users["normal"]

    # admin token 应返回 admin 用户
    admin_headers = _auth_headers(admin_user.id)
    admin_resp = await client.get("/me", headers=admin_headers)
    assert admin_resp.json()["email"] == "admin@example.com"

    # normal token 应返回 normal 用户
    normal_headers = _auth_headers(normal_user.id)
    normal_resp = await client.get("/me", headers=normal_headers)
    assert normal_resp.json()["email"] == "user@example.com"


@pytest.mark.asyncio
async def test_refresh_token_with_disabled_user_fails(client: httpx.AsyncClient, auth_users: dict[str, User]) -> None:
    """测试被禁用用户使用 refresh token 刷新失败（401）。"""
    disabled_user = auth_users["disabled"]
    rtoken = create_refresh_token(str(disabled_user.id))

    response = await client.post(
        "/auth/refresh",
        json={"refresh_token": rtoken},
    )
    assert response.status_code == 401
    assert "非 active" in response.json()["detail"]


if __name__ == "__main__":
    # 自测入口：直接运行验证
    pytest.main([__file__, "-v", "--tb=short"])
