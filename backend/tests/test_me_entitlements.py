"""GET /me/entitlements 端点测试（SubTask 4.13）。

验证返回字段：{plan_code, plan_name, monitor_limit, used, remaining, expires_at}
- 普通用户 observe_20：monitor_limit=20，used=自选股 active 数，remaining=20-used
- 普通用户 research_50：monitor_limit=50
- 管理员：返回 ADMIN_PLAN_CODE (research_50)，monitor_limit=50
- 无会员记录：404
- 已到期会员：仍返回套餐信息（status=expired）

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库）
- 通过 dependency_overrides 注入测试 session 到 app
- 使用 ASGITransport + AsyncClient 调用 HTTP 端点
- 创建用户/会员/自选股，验证 used/remaining 计算
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
from app.models.instrument import Instrument
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.models.watchlist import UserWatchlistItem
from app.services.subscription_service import generate_invite_codes, register_with_invite_code


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
    """通过邀请码注册创建普通用户 + 订阅记录。"""
    admin = await _create_admin(db)
    results = await generate_invite_codes(
        db=db, count=1, created_by=admin.id,
        plan_code=plan_code, grant_months=grant_months,
    )
    await db.flush()
    email = f"user_{uuid.uuid4().hex[:8]}@test.com"
    user, subscription = await register_with_invite_code(
        db=db, email=email, password="password-12345",
        raw_invite_code=results[0][1],
    )
    await db.flush()
    return user, subscription


async def _create_instruments(db: AsyncSession, count: int) -> list[Instrument]:
    """创建若干测试标的。"""
    instruments = []
    for i in range(count):
        inst = Instrument(
            symbol=f"T{uuid.uuid4().hex[:5]}",
            name=f"测试标的{i}",
            market="SZ",
            status="active",
        )
        db.add(inst)
        instruments.append(inst)
    await db.flush()
    return instruments


async def _add_watchlist_items(
    db: AsyncSession, user: User, instruments: list[Instrument], active_count: int
) -> None:
    """为用户添加自选股，前 active_count 个为 active。"""
    for i, inst in enumerate(instruments):
        item = UserWatchlistItem(
            user_id=user.id,
            instrument_id=inst.id,
            source="manual",
            active=i < active_count,
        )
        db.add(item)
    await db.flush()


@pytest_asyncio.fixture
async def entitlements_client(db_session: AsyncSession) -> AsyncGenerator[tuple[AsyncClient, AsyncSession], None]:
    """提供 HTTP 客户端 + 测试 DB session，通过 dependency_overrides 注入。"""
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
# /me/entitlements 端点测试
# ============================================================


@pytest.mark.asyncio
async def test_entitlements_observe_20_user(entitlements_client):
    """observe_20 用户：返回 monitor_limit=20，used/remaining 正确。"""
    client, db = entitlements_client
    user, membership = await _create_normal_user_with_membership(db, "observe_20", grant_months=1)
    # 添加 5 个 active 自选股
    instruments = await _create_instruments(db, 5)
    await _add_watchlist_items(db, user, instruments, active_count=5)
    await db.flush()

    resp = await client.get("/me/entitlements", headers=_auth_headers(user.id))

    assert resp.status_code == 200
    data = resp.json()
    assert data["plan_code"] == "observe_20"
    assert data["plan_name"] == "观察版"
    assert data["monitor_limit"] == 20
    assert data["used"] == 5
    assert data["remaining"] == 15
    assert data["expires_at"] is not None


@pytest.mark.asyncio
async def test_entitlements_research_50_user(entitlements_client):
    """research_50 用户：返回 monitor_limit=50。"""
    client, db = entitlements_client
    user, membership = await _create_normal_user_with_membership(db, "research_50", grant_months=1)
    # 添加 10 个 active 自选股
    instruments = await _create_instruments(db, 10)
    await _add_watchlist_items(db, user, instruments, active_count=10)
    await db.flush()

    resp = await client.get("/me/entitlements", headers=_auth_headers(user.id))

    assert resp.status_code == 200
    data = resp.json()
    assert data["plan_code"] == "research_50"
    assert data["plan_name"] == "研究版"
    assert data["monitor_limit"] == 50
    assert data["used"] == 10
    assert data["remaining"] == 40


@pytest.mark.asyncio
async def test_entitlements_admin_returns_research_50(entitlements_client):
    """管理员：返回 ADMIN_PLAN_CODE (research_50)，monitor_limit=50。"""
    client, db = entitlements_client
    admin = await _create_admin(db)
    await db.flush()

    resp = await client.get("/me/entitlements", headers=_auth_headers(admin.id))

    assert resp.status_code == 200
    data = resp.json()
    assert data["plan_code"] == "research_50"
    assert data["plan_name"] == "研究版"
    assert data["monitor_limit"] == 50


@pytest.mark.asyncio
async def test_entitlements_no_membership_returns_404(entitlements_client):
    """无会员记录用户：返回 404。"""
    client, db = entitlements_client
    # 创建无会员记录的普通用户
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

    resp = await client.get("/me/entitlements", headers=_auth_headers(user.id))

    assert resp.status_code == 404
    assert "会员记录" in resp.json()["detail"] or "无会员" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_entitlements_used_excludes_inactive_watchlist(entitlements_client):
    """used 字段只统计 active=true 的自选股。"""
    client, db = entitlements_client
    user, membership = await _create_normal_user_with_membership(db, "observe_20", grant_months=1)
    # 8 个 active + 3 个 inactive
    instruments = await _create_instruments(db, 11)
    await _add_watchlist_items(db, user, instruments, active_count=8)
    await db.flush()

    resp = await client.get("/me/entitlements", headers=_auth_headers(user.id))

    assert resp.status_code == 200
    data = resp.json()
    assert data["used"] == 8
    assert data["remaining"] == 12  # 20 - 8


@pytest.mark.asyncio
async def test_entitlements_expired_membership_still_returns_plan(entitlements_client):
    """已到期订阅：仍返回套餐信息（status=expired，monitor_limit 不变）。"""
    client, db = entitlements_client
    user, subscription = await _create_normal_user_with_membership(db, "observe_20", grant_months=1)
    # 手动设为已到期
    subscription.expires_at = datetime.now(UTC) - timedelta(days=1)
    subscription.status = "expired"
    await db.flush()

    resp = await client.get("/me/entitlements", headers=_auth_headers(user.id))

    assert resp.status_code == 200
    data = resp.json()
    assert data["plan_code"] == "observe_20"
    assert data["monitor_limit"] == 20
    assert data["expires_at"] is not None


@pytest.mark.asyncio
async def test_entitlements_zero_used_when_no_watchlist(entitlements_client):
    """无自选股时 used=0，remaining=monitor_limit。"""
    client, db = entitlements_client
    user, membership = await _create_normal_user_with_membership(db, "observe_20", grant_months=1)
    await db.flush()

    resp = await client.get("/me/entitlements", headers=_auth_headers(user.id))

    assert resp.status_code == 200
    data = resp.json()
    assert data["used"] == 0
    assert data["remaining"] == 20


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
