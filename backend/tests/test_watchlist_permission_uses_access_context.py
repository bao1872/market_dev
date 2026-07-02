"""watchlist 权限统一测试（Phase 2 Task 2.6 / 2.7）。

验证 watchlist 4 个端点均使用 AccessContext 统一权限模型，而非自行查询 Subscription：
- GET /watchlist
- POST /watchlist
- DELETE /watchlist/{instrument_id}
- GET /watchlist/monitor-status

权限矩阵（advice.md Phase 2）：
- admin → 200/201/204（require_active_subscription 豁免，POST 时 require_quota 返回 None 跳过额度检查）
- active member + 未达 limit → 200/201/204
- active member + 达到 monitor_limit → POST 409，其他端点 200/204
- expired member → 403（require_active_subscription 拒绝，subscription_active=False）
- 无 subscription member → 403（require_active_subscription 拒绝）

关键行为变化（与旧 _check_watchlist_limit 对比）：
- 旧代码：过期订阅 member 仍可访问自选股（GET/DELETE/monitor-status 仅检查登录）
- 新代码：过期订阅 member 访问全部 4 个端点均被拒绝（require_active_subscription 检查 subscription_active）

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库 bz_stock_test）
- 通过 dependency_overrides 注入测试 session 到 app
- 使用 ASGITransport + AsyncClient + JWT token 调用真实 HTTP 端点
- 复用 test_watchlist_limit.py 的用户/订阅/标的创建模式
- 角色名统一使用 member（不是 user），符合 AGENTS.md 规则 7
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
from app.services.subscription_service import (
    generate_invite_codes,
    register_with_invite_code,
)

# ============================================================
# 测试辅助函数
# ============================================================


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
    """创建管理员用户（admin 角色，无 subscription，符合 AGENTS.md 规则 8）。"""
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


async def _create_member_with_plan(
    db: AsyncSession, plan_code: str, grant_months: int = 1
) -> tuple[User, Subscription]:
    """通过邀请码注册创建 member 用户 + 订阅记录。"""
    admin = await _create_admin(db)
    results = await generate_invite_codes(
        db=db,
        count=1,
        created_by=admin.id,
        plan_code=plan_code,
        grant_months=grant_months,
    )
    await db.flush()
    email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    user, subscription = await register_with_invite_code(
        db=db,
        email=email,
        password="password-12345",
        raw_invite_code=results[0][1],
    )
    await db.flush()
    return user, subscription


async def _create_member_without_subscription(db: AsyncSession) -> User:
    """创建无订阅记录的 member 用户。"""
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
    member_role = await _ensure_role(db, "member")
    db.add(UserRole(user_id=user.id, role_id=member_role.id))
    await db.flush()
    return user


async def _create_instruments(db: AsyncSession, count: int) -> list[Instrument]:
    """创建若干测试标的。"""
    instruments = []
    for _ in range(count):
        inst = Instrument(
            symbol=f"T{uuid.uuid4().hex[:5]}",
            name="测试标的",
            market="SZ",
            status="active",
        )
        db.add(inst)
        instruments.append(inst)
    await db.flush()
    return instruments


async def _add_active_watchlist(
    db: AsyncSession, user: User, instruments: list[Instrument], count: int
) -> None:
    """为用户添加 count 个 active 自选股。"""
    for i in range(count):
        item = UserWatchlistItem(
            user_id=user.id,
            instrument_id=instruments[i].id,
            source="manual",
            active=True,
        )
        db.add(item)
    await db.flush()


# ============================================================
# fixtures
# ============================================================


@pytest_asyncio.fixture
async def watchlist_perm_client(
    db_session: AsyncSession,
) -> AsyncGenerator[tuple[AsyncClient, AsyncSession], None]:
    """提供 HTTP 客户端 + 测试 DB session，通过 dependency_overrides 注入。

    Mock session.commit 为 flush，避免关闭 conftest 的嵌套事务（SAVEPOINT）。
    watchlist POST 端点会调用 db.commit()，若直接提交会破坏测试隔离。
    """
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    original_commit = db_session.commit

    async def mock_commit():
        await db_session.flush()

    db_session.commit = mock_commit  # type: ignore[method-assign]

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[deps_get_db] = get_test_db
    app.dependency_overrides[db_get_db] = get_test_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, db_session

    db_session.commit = original_commit  # type: ignore[method-assign]
    app.dependency_overrides.clear()


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 权限测试场景
# ============================================================


@pytest.mark.asyncio
async def test_expired_member_blocked(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """过期订阅 member 调 POST /watchlist → 403。

    [AccessControl] - 描述: require_active_subscription 检查 subscription_active=False，
    拒绝过期订阅用户新增自选股（旧代码仅检查 subscription 记录是否存在，不检查过期，这是要修复的行为）
    """
    client, db = watchlist_perm_client
    user, subscription = await _create_member_with_plan(db, "observe_20")
    # 手动设置过期
    subscription.expires_at = datetime.now(UTC) - timedelta(days=1)
    instruments = await _create_instruments(db, 1)
    await db.flush()

    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[0].id), "source": "manual"},
        headers=_auth_headers(user.id),
    )

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_no_subscription_member_blocked(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """无 subscription member 调 POST /watchlist → 403。

    [AccessControl] - 描述: require_active_subscription 检查无订阅记录，返回 403
    """
    client, db = watchlist_perm_client
    user = await _create_member_without_subscription(db)
    instruments = await _create_instruments(db, 1)
    await db.flush()

    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[0].id), "source": "manual"},
        headers=_auth_headers(user.id),
    )

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_admin_bypasses_limit(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """admin 调 POST /watchlist → 201（绕过额度检查）。

    [AccessControl] - 描述: admin 经 require_active_subscription 豁免，
    require_quota 返回 None，跳过额度检查
    """
    client, db = watchlist_perm_client
    admin = await _create_admin(db)
    instruments = await _create_instruments(db, 51)
    # admin 已有 50 只 active，加第 51 只应成功
    await _add_active_watchlist(db, admin, instruments, count=50)
    await db.flush()

    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[50].id), "source": "manual"},
        headers=_auth_headers(admin.id),
    )

    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_active_member_at_limit_blocked(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """active member 达到 monitor_limit → 409。

    [AccessControl] - 描述: require_quota 返回 monitor_limit=20，
    当前 active count=20，新增超过限额返回 409
    """
    client, db = watchlist_perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    instruments = await _create_instruments(db, 21)
    await _add_active_watchlist(db, user, instruments, count=20)
    await db.flush()

    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[20].id), "source": "manual"},
        headers=_auth_headers(user.id),
    )

    assert resp.status_code == 409, resp.text
    assert "监控数量已达上限" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_active_member_under_limit_allowed(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """active member 未达 limit → 201。

    [AccessControl] - 描述: require_quota 返回 monitor_limit=20，
    当前 active count=19，新增未超限额返回 201
    """
    client, db = watchlist_perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    instruments = await _create_instruments(db, 20)
    await _add_active_watchlist(db, user, instruments, count=19)
    await db.flush()

    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[19].id), "source": "manual"},
        headers=_auth_headers(user.id),
    )

    assert resp.status_code == 201, resp.text


# ============================================================
# GET /watchlist 权限测试
# ============================================================


@pytest.mark.asyncio
async def test_get_watchlist_expired_member_blocked(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """过期订阅 member 调 GET /watchlist → 403。"""
    client, db = watchlist_perm_client
    user, subscription = await _create_member_with_plan(db, "observe_20")
    subscription.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db.flush()

    resp = await client.get("/watchlist", headers=_auth_headers(user.id))
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_get_watchlist_no_subscription_member_blocked(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """无 subscription member 调 GET /watchlist → 403。"""
    client, db = watchlist_perm_client
    user = await _create_member_without_subscription(db)
    await db.flush()

    resp = await client.get("/watchlist", headers=_auth_headers(user.id))
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_get_watchlist_active_member_allowed(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """active member 调 GET /watchlist → 200。"""
    client, db = watchlist_perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    resp = await client.get("/watchlist", headers=_auth_headers(user.id))
    assert resp.status_code == 200, resp.text


# ============================================================
# DELETE /watchlist/{instrument_id} 权限测试
# ============================================================


@pytest.mark.asyncio
async def test_delete_watchlist_expired_member_blocked(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """过期订阅 member 调 DELETE /watchlist/{id} → 403。"""
    client, db = watchlist_perm_client
    user, subscription = await _create_member_with_plan(db, "observe_20")
    instruments = await _create_instruments(db, 1)
    await _add_active_watchlist(db, user, instruments, count=1)
    subscription.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db.flush()

    resp = await client.delete(
        f"/watchlist/{instruments[0].id}",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_delete_watchlist_no_subscription_member_blocked(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """无 subscription member 调 DELETE /watchlist/{id} → 403。"""
    client, db = watchlist_perm_client
    user = await _create_member_without_subscription(db)
    instruments = await _create_instruments(db, 1)
    await db.flush()

    resp = await client.delete(
        f"/watchlist/{instruments[0].id}",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_delete_watchlist_active_member_allowed(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """active member 调 DELETE /watchlist/{id} → 204。"""
    client, db = watchlist_perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    instruments = await _create_instruments(db, 1)
    await _add_active_watchlist(db, user, instruments, count=1)
    await db.flush()

    resp = await client.delete(
        f"/watchlist/{instruments[0].id}",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 204, resp.text


# ============================================================
# GET /watchlist/monitor-status 权限测试
# ============================================================


@pytest.mark.asyncio
async def test_monitor_status_expired_member_blocked(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """过期订阅 member 调 GET /watchlist/monitor-status → 403。"""
    client, db = watchlist_perm_client
    user, subscription = await _create_member_with_plan(db, "observe_20")
    subscription.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db.flush()

    resp = await client.get("/watchlist/monitor-status", headers=_auth_headers(user.id))
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_monitor_status_no_subscription_member_blocked(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """无 subscription member 调 GET /watchlist/monitor-status → 403。"""
    client, db = watchlist_perm_client
    user = await _create_member_without_subscription(db)
    await db.flush()

    resp = await client.get("/watchlist/monitor-status", headers=_auth_headers(user.id))
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_monitor_status_active_member_allowed(
    watchlist_perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """active member 调 GET /watchlist/monitor-status → 200。"""
    client, db = watchlist_perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    resp = await client.get("/watchlist/monitor-status", headers=_auth_headers(user.id))
    assert resp.status_code == 200, resp.text


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
