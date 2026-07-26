"""V2.1 Phase F 自选额度与权限测试 - POST /watchlist 额度边界。

验证 PRD §7.1 + §10.2 + §10.3：
- 额度=2，串行添加 3 只股票，第 3 只返回 409 WATCHLIST_LIMIT_REACHED
- admin 无额度限制（watchlist_stock_limit=None + is_admin_unlimited=True）
- 无 watchlist_management 能力 → 403 CAPABILITY_REQUIRED
- 已软删除记录恢复前校验额度（恢复后 active count +1）
- 恢复软删除记录后额度正确递减

测试策略：
- 使用 httpx AsyncClient + ASGITransport
- 串行调用 POST /watchlist，逐项断言 status_code 和最终 active count
- 并发 SELECT FOR UPDATE 行为由代码审查 + 数据库唯一约束保证，
  单元测试不模拟真并发（savepoint 模式下无效）

[注] 并发安全性来自代码 `select(User).with_for_update()`，
此测试覆盖额度边界逻辑和权限拒绝路径。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.capability_keys import MARKET_SCREENING, WATCHLIST_MANAGEMENT
from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models.capability_grant import UserCapabilityGrant
from app.models.instrument import Instrument
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.models.watchlist import UserWatchlistItem
from tests.conftest import make_asgi_transport


async def _ensure_role(db: AsyncSession, name: str) -> Role:
    """确保角色存在并返回。"""
    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _create_member_with_limit(
    db: AsyncSession, watchlist_limit: int
) -> tuple[User, Subscription]:
    """创建 member 用户 + subscription + capability grant（指定额度）。

    Args:
        watchlist_limit: watchlist_management 的 limit_value
    """
    user = User(
        id=uuid.uuid4(),
        email=f"limit_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("password-12345"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(user)
    member_role = await _ensure_role(db, "member")
    db.add(UserRole(user_id=user.id, role_id=member_role.id))

    now = datetime.now(UTC)
    starts_at = now - timedelta(days=1)
    expires_at = now + timedelta(days=30)

    sub = Subscription(
        id=uuid.uuid4(),
        user_id=user.id,
        plan_code="observe_20",
        status="active",
        starts_at=starts_at,
        expires_at=expires_at,
        entitlement_snapshot={"monitor_limit": watchlist_limit},
        source="invite",
        created_by=None,
    )
    db.add(sub)
    await db.flush()

    db.add(UserCapabilityGrant(
        user_id=user.id,
        capability_key=WATCHLIST_MANAGEMENT,
        limit_value=watchlist_limit,
        source_type="legacy_subscription",
        source_id=str(sub.id),
        starts_at=starts_at,
        expires_at=expires_at,
        revoked_at=None,
    ))
    db.add(UserCapabilityGrant(
        user_id=user.id,
        capability_key=MARKET_SCREENING,
        limit_value=None,
        source_type="legacy_subscription",
        source_id=str(sub.id),
        starts_at=starts_at,
        expires_at=expires_at,
        revoked_at=None,
    ))
    await db.flush()
    return user, sub


async def _create_admin(db: AsyncSession) -> User:
    """创建 admin 用户（无 grant，无 subscription）。"""
    user = User(
        id=uuid.uuid4(),
        email=f"admin_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("password-12345"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(user)
    admin_role = await _ensure_role(db, "admin")
    db.add(UserRole(user_id=user.id, role_id=admin_role.id))
    await db.flush()
    object.__setattr__(user, "_roles", ["admin"])
    return user


async def _create_member_without_grants(db: AsyncSession) -> User:
    """创建 member 用户但无任何 capability grant（无 watchlist_management 权限）。"""
    user = User(
        id=uuid.uuid4(),
        email=f"noperm_{uuid.uuid4().hex[:8]}@test.com",
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
    object.__setattr__(user, "_roles", ["member"])
    return user


async def _create_instruments(db: AsyncSession, count: int) -> list[Instrument]:
    """创建若干测试标的。"""
    instruments = []
    for _ in range(count):
        inst = Instrument(
            symbol=f"L{uuid.uuid4().hex[:5]}",
            name="额度测试标的",
            market="SZ",
            status="active",
        )
        db.add(inst)
        instruments.append(inst)
    await db.flush()
    return instruments


@pytest_asyncio.fixture
async def limit_client(
    db_session: AsyncSession,
) -> AsyncGenerator[tuple[AsyncClient, AsyncSession], None]:
    """提供 HTTP 客户端 + 测试 DB session，通过 dependency_overrides 注入。"""
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[deps_get_db] = get_test_db
    app.dependency_overrides[db_get_db] = get_test_db

    transport = make_asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, db_session

    app.dependency_overrides.clear()


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 额度边界测试（PRD §7.1 + §10.3）
# ============================================================


@pytest.mark.asyncio
async def test_member_reaches_limit_returns_409(
    limit_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """[F2] 额度=2，串行添加 3 只股票，第 3 只返回 409 WATCHLIST_LIMIT_REACHED。

    验证：
    - 第 1、2 个 POST 返回 201
    - 第 3 个 POST 返回 409，detail.reason_code=WATCHLIST_LIMIT_REACHED
    - 最终 active count = 2
    """
    client, db = limit_client
    user, _ = await _create_member_with_limit(db, watchlist_limit=2)
    instruments = await _create_instruments(db, 3)
    await db.flush()

    headers = _auth_headers(user.id)

    # 第 1、2 只：成功
    r1 = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[0].id), "source": "manual"},
        headers=headers,
    )
    assert r1.status_code == 201, f"第1只应成功: {r1.text}"

    r2 = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[1].id), "source": "manual"},
        headers=headers,
    )
    assert r2.status_code == 201, f"第2只应成功: {r2.text}"

    # 第 3 只：超额拒绝
    r3 = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[2].id), "source": "manual"},
        headers=headers,
    )
    assert r3.status_code == 409, f"第3只应409: {r3.text}"
    # FastAPI HTTPException 默认包装为 {"detail": <detail>}
    detail = r3.json()["detail"]
    assert detail["reason_code"] == "WATCHLIST_LIMIT_REACHED"
    assert detail["current_count"] == 2
    assert detail["limit"] == 2

    # 最终 active count = 2
    count_stmt = select(UserWatchlistItem).where(
        UserWatchlistItem.user_id == user.id,
        UserWatchlistItem.active.is_(True),
    )
    result = await db.execute(count_stmt)
    active_items = result.scalars().all()
    assert len(active_items) == 2


@pytest.mark.asyncio
async def test_admin_unlimited_no_limit(
    limit_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """[F2] admin 无额度限制，可添加多只股票。

    验证：
    - admin 添加 5 只股票全部 201
    - 无 WATCHLIST_LIMIT_REACHED
    - active count = 5
    """
    client, db = limit_client
    admin = await _create_admin(db)
    instruments = await _create_instruments(db, 5)
    await db.flush()

    headers = _auth_headers(admin.id)

    for i, inst in enumerate(instruments):
        r = await client.post(
            "/watchlist",
            json={"instrument_id": str(inst.id), "source": "manual"},
            headers=headers,
        )
        assert r.status_code == 201, f"admin 第{i+1}只应成功: {r.text}"

    count_stmt = select(UserWatchlistItem).where(
        UserWatchlistItem.user_id == admin.id,
        UserWatchlistItem.active.is_(True),
    )
    result = await db.execute(count_stmt)
    active_items = result.scalars().all()
    assert len(active_items) == 5


@pytest.mark.asyncio
async def test_no_watchlist_capability_returns_403(
    limit_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """[F4] 无 watchlist_management 能力的用户 POST /watchlist → 403。

    验证：
    - member 无 grant：403 CAPABILITY_REQUIRED
    """
    client, db = limit_client
    user = await _create_member_without_grants(db)
    instruments = await _create_instruments(db, 1)
    await db.flush()

    headers = _auth_headers(user.id)

    r = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[0].id), "source": "manual"},
        headers=headers,
    )
    assert r.status_code == 403, f"无权限应403: {r.text}"
    detail = r.json()["detail"]
    assert detail["reason_code"] == "CAPABILITY_REQUIRED"


@pytest.mark.asyncio
async def test_restore_soft_deleted_respects_limit(
    limit_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """[F2] 恢复软删除记录前校验额度（恢复后 active count +1）。

    场景：
    - 额度=1
    - 添加股票 A（201）
    - 删除股票 A（204）
    - 添加股票 B（201）→ 占用额度
    - 尝试恢复股票 A → 409（active count 已达额度）
    """
    client, db = limit_client
    user, _ = await _create_member_with_limit(db, watchlist_limit=1)
    instruments = await _create_instruments(db, 2)
    await db.flush()

    headers = _auth_headers(user.id)
    inst_a, inst_b = instruments[0], instruments[1]

    # 1. 添加 A
    r1 = await client.post(
        "/watchlist",
        json={"instrument_id": str(inst_a.id), "source": "manual"},
        headers=headers,
    )
    assert r1.status_code == 201

    # 2. 删除 A（软删除）
    r2 = await client.delete(f"/watchlist/{inst_a.id}", headers=headers)
    assert r2.status_code == 204

    # 3. 添加 B → 占用额度
    r3 = await client.post(
        "/watchlist",
        json={"instrument_id": str(inst_b.id), "source": "manual"},
        headers=headers,
    )
    assert r3.status_code == 201

    # 4. 尝试恢复 A → 409（额度=1，已占用）
    r4 = await client.post(
        "/watchlist",
        json={"instrument_id": str(inst_a.id), "source": "manual"},
        headers=headers,
    )
    assert r4.status_code == 409, f"恢复超额应409: {r4.text}"
    detail = r4.json()["detail"]
    assert detail["reason_code"] == "WATCHLIST_LIMIT_REACHED"


@pytest.mark.asyncio
async def test_duplicate_active_returns_409(
    limit_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """[F1] 同一股票重复添加 → 409 Conflict（非 WATCHLIST_LIMIT_REACHED）。

    验证 (user_id, instrument_id) 唯一约束：active 状态下重复添加返回 409，
    detail 为字符串而非 WATCHLIST_LIMIT_REACHED 字典。
    """
    client, db = limit_client
    user, _ = await _create_member_with_limit(db, watchlist_limit=20)
    instruments = await _create_instruments(db, 1)
    await db.flush()

    headers = _auth_headers(user.id)

    # 第一次：成功
    r1 = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[0].id), "source": "manual"},
        headers=headers,
    )
    assert r1.status_code == 201

    # 第二次：冲突
    r2 = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[0].id), "source": "manual"},
        headers=headers,
    )
    assert r2.status_code == 409
    # 重复添加返回字符串 detail，不是 WATCHLIST_LIMIT_REACHED 字典
    detail = r2.json()["detail"]
    assert isinstance(detail, str) or detail.get("reason_code") != "WATCHLIST_LIMIT_REACHED"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
