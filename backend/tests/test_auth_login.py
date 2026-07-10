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
- 使用 PostgreSQL 测试库 + 共享 fixtures（client / user_factory / role_factory /
  subscription_factory）
- 通过 conftest.client 自动注入测试 session
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.exc import SQLAlchemyError

from app.core.security import get_password_hash
from app.models.subscription import Subscription
from app.models.user import Role, User
from tests.conftest import AsyncFactory


@pytest_asyncio.fixture
async def admin_user(
    user_factory: AsyncFactory[User],
    role_factory: AsyncFactory[Role],
) -> User:
    """创建 admin 角色与 admin@example.com 测试用户。"""
    await role_factory(name="admin", description="管理员")
    return await user_factory(
        email="admin@example.com",
        password_hash=get_password_hash("admin-password-123"),
        roles=["admin"],
    )


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
async def test_login_success(
    client: httpx.AsyncClient,
    user_factory: AsyncFactory[User],
) -> None:
    """正确账号密码登录成功，返回 token。"""
    await user_factory(
        email="login_ok@example.com",
        password_hash=get_password_hash("password123"),
        roles=["member"],
    )

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
async def test_login_wrong_password(
    client: httpx.AsyncClient,
    user_factory: AsyncFactory[User],
) -> None:
    """错误密码登录失败（401）。"""
    await user_factory(
        email="wrong_pwd@example.com",
        password_hash=get_password_hash("password123"),
        roles=["member"],
    )

    response = await client.post(
        "/auth/login",
        json={"email": "wrong_pwd@example.com", "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert "邮箱或密码错误" in response.json()["detail"]


@pytest.mark.asyncio
async def test_login_nonexistent_user(client: httpx.AsyncClient) -> None:
    """不存在用户登录失败（401，统一错误信息）。"""
    response = await client.post(
        "/auth/login",
        json={"email": "nobody@example.com", "password": "any-password-123"},
    )
    assert response.status_code == 401
    assert "邮箱或密码错误" in response.json()["detail"]


@pytest.mark.asyncio
async def test_login_disabled_user(
    client: httpx.AsyncClient,
    user_factory: AsyncFactory[User],
) -> None:
    """disabled 用户登录失败（401）。"""
    await user_factory(
        email="disabled_login@example.com",
        password_hash=get_password_hash("password123"),
        status="disabled",
        roles=["member"],
    )

    response = await client.post(
        "/auth/login",
        json={"email": "disabled_login@example.com", "password": "password123"},
    )
    assert response.status_code == 401
    assert "非 active" in response.json()["detail"]


@pytest.mark.asyncio
async def test_login_without_membership_subscription_active_false(
    client: httpx.AsyncClient,
    user_factory: AsyncFactory[User],
) -> None:
    """无 subscription 用户可登录，且 subscription_active=False（无订阅记录）。"""
    await user_factory(
        email="no_member@example.com",
        password_hash=get_password_hash("password123"),
        roles=["member"],
    )

    response = await client.post(
        "/auth/login",
        json={"email": "no_member@example.com", "password": "password123"},
    )
    assert response.status_code == 200
    data = response.json()
    # [Auth] - 描述: 无订阅记录的 member，subscription_active=False（替代旧 membership_expired=true）
    assert data["subscription_active"] is False
    assert data["next_route"] == "/subscription-expired"


@pytest.mark.asyncio
async def test_login_expired_subscription_not_modify_status(
    client: httpx.AsyncClient,
    db_session,
    user_factory: AsyncFactory[User],
    subscription_factory: AsyncFactory[Subscription],
) -> None:
    """过期订阅登录返回 subscription_active=False，且不修改 DB status。"""
    user = await user_factory(
        email="expired_member@example.com",
        password_hash=get_password_hash("password123"),
        roles=["member"],
    )
    subscription = await subscription_factory(
        user_id=user.id,
        status="active",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    await db_session.commit()

    response = await client.post(
        "/auth/login",
        json={"email": "expired_member@example.com", "password": "password123"},
    )
    assert response.status_code == 200
    data = response.json()
    # [Auth] - 描述: 过期订阅 subscription_active=False（替代旧 membership_expired=true）
    assert data["subscription_active"] is False
    assert data["next_route"] == "/subscription-expired"

    # 刷新会话，重新查询 subscription status，确认未被 login 改为 expired
    await db_session.refresh(subscription)
    assert subscription.status == "active"


@pytest.mark.asyncio
async def test_login_db_error_returns_500(
    client: httpx.AsyncClient,
    user_factory: AsyncFactory[User],
    caplog,
) -> None:
    """数据库异常返回 500，并记录日志。"""
    await user_factory(
        email="db_error@example.com",
        password_hash=get_password_hash("password123"),
        roles=["member"],
    )

    # [Auth] - 描述: login 调用 get_access_context，patch 该函数模拟 DB 异常
    with patch("app.api.auth.get_access_context") as mock_ctx:
        mock_ctx.side_effect = SQLAlchemyError("simulated db failure")

        response = await client.post(
            "/auth/login",
            json={"email": "db_error@example.com", "password": "password123"},
        )

    assert response.status_code == 500
    assert "登录服务暂不可用" in response.json()["detail"]
    assert any("登录失败" in record.message for record in caplog.records)
    assert any("db_error@example.com" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_login_invalid_password_hash_returns_401(
    client: httpx.AsyncClient,
    db_session,
    user_factory: AsyncFactory[User],
) -> None:
    """密码 hash 格式异常返回 401。"""
    user = await user_factory(
        email="bad_hash@example.com",
        roles=["member"],
    )
    user.password_hash = "not-a-valid-bcrypt-hash"
    await db_session.commit()

    response = await client.post(
        "/auth/login",
        json={"email": "bad_hash@example.com", "password": "any-password"},
    )
    assert response.status_code == 401


# ============================================================
# AccessProfile 登录响应测试（Phase 2 Task 2.4）
# 验证登录响应包含 4 个 token 字段 + 10 个 AccessProfile 字段
# next_route 逻辑：admin → /admin/overview；member active → /overview；
#                  member expired → /subscription-expired
# ============================================================


@pytest.mark.asyncio
async def test_login_response_contains_access_profile_fields(
    client: httpx.AsyncClient,
    admin_user: User,
) -> None:
    """登录成功后响应 JSON 包含全部 10 个 AccessProfile 字段 + 4 个 token 字段。"""
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
async def test_login_response_admin_next_route(
    client: httpx.AsyncClient,
    admin_user: User,
) -> None:
    """admin 登录 next_route='/admin/overview'。"""
    response = await client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "admin-password-123"},
    )
    assert response.status_code == 200
    assert response.json()["next_route"] == "/admin/overview"


@pytest.mark.asyncio
async def test_login_response_member_active_next_route(
    client: httpx.AsyncClient,
    user_factory: AsyncFactory[User],
    subscription_factory: AsyncFactory[Subscription],
    db_session,
) -> None:
    """member 有效订阅 next_route='/overview'。"""
    user = await user_factory(
        email="member_active@example.com",
        password_hash=get_password_hash("password123"),
        roles=["member"],
    )
    await subscription_factory(
        user_id=user.id,
        status="active",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    await db_session.commit()

    response = await client.post(
        "/auth/login",
        json={"email": "member_active@example.com", "password": "password123"},
    )
    assert response.status_code == 200
    assert response.json()["next_route"] == "/overview"


@pytest.mark.asyncio
async def test_login_response_member_expired_next_route(
    client: httpx.AsyncClient,
    user_factory: AsyncFactory[User],
    subscription_factory: AsyncFactory[Subscription],
    db_session,
) -> None:
    """member 订阅过期 next_route='/subscription-expired'。"""
    user = await user_factory(
        email="member_expired@example.com",
        password_hash=get_password_hash("password123"),
        roles=["member"],
    )
    await subscription_factory(
        user_id=user.id,
        status="active",  # DB 中仍为 active，但 expires_at 已过，get_access_context 实时计算为 expired
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    await db_session.commit()

    response = await client.post(
        "/auth/login",
        json={"email": "member_expired@example.com", "password": "password123"},
    )
    assert response.status_code == 200
    assert response.json()["next_route"] == "/subscription-expired"


@pytest.mark.asyncio
async def test_login_response_no_membership_expired_field(
    client: httpx.AsyncClient,
    admin_user: User,
) -> None:
    """响应不再包含 membership_expired 字段（已被 subscription_active 替代）。"""
    response = await client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "admin-password-123"},
    )
    assert response.status_code == 200
    assert "membership_expired" not in response.json()


@pytest.mark.asyncio
async def test_login_response_admin_subscription_required_false(
    client: httpx.AsyncClient,
    admin_user: User,
) -> None:
    """admin 的 subscription_required=False（admin 不需要订阅）。"""
    response = await client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "admin-password-123"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["subscription_required"] is False
    assert data["is_admin"] is True


@pytest.mark.asyncio
async def test_login_response_member_subscription_required_true(
    client: httpx.AsyncClient,
    user_factory: AsyncFactory[User],
    subscription_factory: AsyncFactory[Subscription],
    db_session,
) -> None:
    """member 的 subscription_required=True（member 需要订阅）。"""
    user = await user_factory(
        email="member_req@example.com",
        password_hash=get_password_hash("password123"),
        roles=["member"],
    )
    await subscription_factory(
        user_id=user.id,
        status="active",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    await db_session.commit()

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
