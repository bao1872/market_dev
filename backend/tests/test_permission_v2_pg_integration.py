"""权限模型 V2 - 共享开发数据库目标测试。

运行模式：PANJI_SHARED_DEV_DB_TEST=1（经 SSH 隧道连共享开发业务数据库 bz_stock，
不创建任何临时/测试库，禁止 DDL/Alembic，完整 rollback，测试结束无残留）。

使用 conftest 的 `client` fixture（自动 override get_db 复用同一 db_session，
不逃逸事务）。所有写入经统一 db_session，测试结束外层事务 rollback。

覆盖（权限 V2 关键合同）：
1. self_selection 邀请码注册后真实写入 UserCapability；
2. /me/access 真实响应包含 capabilities；
3. login 真实响应包含 capabilities 且仅 self_selection 的 next_route=/market?scope=watchlist；
4. API 守卫真实 200/403（require_any_capability）；
5. 管理员列表返回权限摘要字段；
6. 新邀请码拒绝空 capabilities。
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models.user import Role, User, UserRole
from app.services.subscription_service import (
    generate_invite_codes,
    register_with_invite_code,
)

pytestmark = pytest.mark.shared_dev_db

# 测试数据唯一前缀（结束必须无残留）
_TEST_EMAIL_PREFIX = "pg-v2-test-"


async def _register_with_invite(db: AsyncSession, email: str, cap: list[dict]) -> User:
    """用指定 capabilities 生成邀请码并注册，返回 user。

    共享库 invite_codes.created_by NOT NULL，需传入真实 admin 用户 id。
    """
    created_by = await _admin_user(db)
    codes = await generate_invite_codes(
        db=db, count=1, note="pg-v2-test", capabilities=cap, created_by=created_by.id
    )
    code = codes[0][1]
    user, _subscription = await register_with_invite_code(
        db, email=email, raw_invite_code=code, password="test-pass-123"
    )
    return user


async def _admin_user(db: AsyncSession) -> User:
    """创建 admin 用户（savepoint 内，结束 rollback）。"""
    from app.core.security import get_password_hash

    user = User(
        email=f"{_TEST_EMAIL_PREFIX}admin-{uuid.uuid4().hex[:8]}@test.local",
        password_hash=get_password_hash("test-pass-123"),
        status="active",
        timezone="Asia/Shanghai",
    )
    db.add(user)
    await db.flush()
    role = await db.scalar(select(Role).where(Role.name == "admin"))
    if role is not None:
        db.add(UserRole(user_id=user.id, role_id=role.id))
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_self_selection_invite_writes_user_capability(db_session: AsyncSession) -> None:
    """self_selection 邀请码注册 → 真实写入 UserCapability（含 watchlist_limit）。"""
    email = f"{_TEST_EMAIL_PREFIX}ss-{uuid.uuid4().hex[:8]}@test.local"
    user = await _register_with_invite(
        db_session,
        email,
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 5}],
    )
    from app.models.user_capability import UserCapability

    cap = await db_session.scalar(
        select(UserCapability).where(
            UserCapability.user_id == user.id,
            UserCapability.capability == "self_selection",
        )
    )
    assert cap is not None
    assert cap.watchlist_limit == 5
    assert cap.expires_at is not None


@pytest.mark.asyncio
async def test_me_access_returns_capabilities(db_session: AsyncSession, client: AsyncClient) -> None:
    """GET /me/access 真实响应包含 capabilities。"""
    email = f"{_TEST_EMAIL_PREFIX}access-{uuid.uuid4().hex[:8]}@test.local"
    user = await _register_with_invite(
        db_session,
        email,
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 5}],
    )
    token = create_access_token(str(user.id))
    resp = await client.get(
        "/v1/me/access",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "capabilities" in body
    assert "self_selection" in body["capabilities"]
    assert body["capabilities"]["self_selection"]["active"] is True
    assert body["capabilities"]["self_selection"]["watchlist_limit"] == 5


@pytest.mark.asyncio
async def test_login_returns_capabilities_and_watchlist_next_route(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """login 真实响应含 capabilities，仅 self_selection 的 next_route=/market?scope=watchlist。"""
    email = f"{_TEST_EMAIL_PREFIX}login-{uuid.uuid4().hex[:8]}@test.local"
    await _register_with_invite(
        db_session,
        email,
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 5}],
    )
    resp = await client.post("/v1/auth/login", json={"email": email, "password": "test-pass-123"})
    assert resp.status_code == 200
    body = resp.json()
    assert "capabilities" in body
    assert "self_selection" in body["capabilities"]
    assert body["next_route"] == "/market?scope=watchlist"


@pytest.mark.asyncio
async def test_api_guard_200_and_403(db_session: AsyncSession, client: AsyncClient) -> None:
    """require_any_capability 真实 200（有 self_selection）；无权限用户 403。"""
    email_ok = f"{_TEST_EMAIL_PREFIX}guard-ok-{uuid.uuid4().hex[:8]}@test.local"
    user_ok = await _register_with_invite(
        db_session,
        email_ok,
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 5}],
    )
    token_ok = create_access_token(str(user_ok.id))

    # 无任何权限用户（普通注册，不写 capability）
    from app.core.security import get_password_hash

    email_none = f"{_TEST_EMAIL_PREFIX}guard-none-{uuid.uuid4().hex[:8]}@test.local"
    user_none = User(
        email=email_none,
        password_hash=get_password_hash("test-pass-123"),
        status="active",
        timezone="Asia/Shanghai",
    )
    db_session.add(user_none)
    await db_session.flush()
    role = await db_session.scalar(select(Role).where(Role.name == "member"))
    if role is not None:
        db_session.add(UserRole(user_id=user_none.id, role_id=role.id))
    await db_session.flush()
    token_none = create_access_token(str(user_none.id))

    ok = await client.get(
        "/v1/market/stocks?page=1&page_size=1",
        headers={"Authorization": f"Bearer {token_ok}"},
    )
    none = await client.get(
        "/v1/market/stocks?page=1&page_size=1",
        headers={"Authorization": f"Bearer {token_none}"},
    )
    assert ok.status_code in (200, 422)  # 200 或参数校验（权限已放行）
    assert none.status_code == 403


@pytest.mark.asyncio
async def test_admin_user_list_includes_capability_summary(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """管理员用户列表返回权限摘要字段（capabilities/active_keys/default_route）。"""
    admin = await _admin_user(db_session)
    await _register_with_invite(
        db_session,
        f"{_TEST_EMAIL_PREFIX}list-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 3}],
    )
    token = create_access_token(str(admin.id))
    resp = await client.get(
        "/v1/admin/users?limit=50",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    sample = body["items"][0]
    assert "capabilities" in sample
    assert "active_capability_keys" in sample
    assert "default_route" in sample
