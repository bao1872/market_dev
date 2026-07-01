"""Subscription 模型测试 - Phase 2 Task 2.2 RED 阶段。

测试内容：
1. subscriptions 表存在
2. 创建 Subscription 对象，所有字段正确
3. user_id 唯一约束
4. get_effective_subscription_status 三态返回（active/expired/none）
5. 返回值类型为 Literal["active","expired","none"]

测试策略：
- 使用 conftest.py 的 db_session fixture（PostgreSQL 测试库 bz_stock_test）
- 直接调用 subscription_service.get_effective_subscription_status（GREEN 阶段实现）
- 表结构由 Alembic 049_subscriptions_table 迁移创建
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.services.subscription_service import get_effective_subscription_status


def _list_table_names_sync(sync_session) -> set[str]:
    """同步上下文中获取表名集合（供 run_sync 调用）。

    run_sync 回调接收的是 SyncSession，通过 connection() 拿到底层 connection
    再用 inspect 获取表名。
    """
    conn = sync_session.connection()
    return set(inspect(conn).get_table_names())


async def _ensure_user_role(db: AsyncSession) -> Role:
    """确保 user 角色存在并返回。"""
    result = await db.execute(select(Role).where(Role.name == "user"))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name="user", description="普通用户")
        db.add(role)
        await db.flush()
    return role


async def _create_user(db: AsyncSession, email: str | None = None) -> User:
    """创建普通用户（user 角色）。"""
    email = email or f"sub_{uuid.uuid4().hex[:8]}@test.com"
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash="fake-hash",
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(user)
    role = await _ensure_user_role(db)
    db.add(UserRole(user_id=user.id, role_id=role.id))
    await db.flush()
    return user


def _build_subscription(
    user_id: uuid.UUID,
    *,
    status: str = "active",
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    plan_code: str = "observe_20",
    source: str = "invite",
) -> Subscription:
    """构造 Subscription 对象（不写库，由调用方 add + flush）。"""
    now = datetime.now(UTC)
    return Subscription(
        id=uuid.uuid4(),
        user_id=user_id,
        plan_code=plan_code,
        status=status,
        starts_at=starts_at or now,
        expires_at=expires_at or (now + timedelta(days=30)),
        entitlement_snapshot=None,
        source=source,
        created_by=None,
    )


# ============================================================
# 表存在性测试
# ============================================================


@pytest.mark.asyncio
async def test_subscriptions_table_exists(db_session: AsyncSession) -> None:
    """subscriptions 表存在（由 049_subscriptions_table 迁移创建）。"""
    table_names = await db_session.run_sync(_list_table_names_sync)
    assert "subscriptions" in table_names, "subscriptions 表应存在"


# ============================================================
# 创建/字段测试
# ============================================================


@pytest.mark.asyncio
async def test_subscription_create(db_session: AsyncSession) -> None:
    """创建 Subscription 对象，所有字段正确写入并读取。"""
    user = await _create_user(db_session)
    now = datetime.now(UTC)
    starts_at = now - timedelta(days=1)
    expires_at = now + timedelta(days=30)
    snapshot = {"monitor_limit": 20, "features": ["trend_selection"]}

    sub = Subscription(
        id=uuid.uuid4(),
        user_id=user.id,
        plan_code="observe_20",
        status="active",
        starts_at=starts_at,
        expires_at=expires_at,
        entitlement_snapshot=snapshot,
        source="invite",
        created_by=None,
    )
    db_session.add(sub)
    await db_session.flush()

    # 重新查询验证字段
    stmt = select(Subscription).where(Subscription.user_id == user.id)
    result = await db_session.execute(stmt)
    fetched = result.scalar_one()
    assert fetched.id == sub.id
    assert fetched.user_id == user.id
    assert fetched.plan_code == "observe_20"
    assert fetched.status == "active"
    assert fetched.starts_at == starts_at
    assert fetched.expires_at == expires_at
    assert fetched.entitlement_snapshot == snapshot
    assert fetched.source == "invite"
    assert fetched.created_by is None
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


# ============================================================
# 唯一约束测试
# ============================================================


@pytest.mark.asyncio
async def test_subscription_user_id_unique(db_session: AsyncSession) -> None:
    """user_id 唯一约束：同一用户插入两条订阅应抛 IntegrityError。"""
    user = await _create_user(db_session)
    sub1 = _build_subscription(user.id)
    sub2 = _build_subscription(user.id)
    db_session.add(sub1)
    db_session.add(sub2)
    with pytest.raises(IntegrityError):
        await db_session.flush()


# ============================================================
# get_effective_subscription_status 三态测试
# ============================================================


@pytest.mark.asyncio
async def test_effective_subscription_active(db_session: AsyncSession) -> None:
    """status='active' + starts_at<=now + expires_at>now -> 返回 'active'。"""
    user = await _create_user(db_session)
    now = datetime.now(UTC)
    sub = _build_subscription(
        user.id,
        status="active",
        starts_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=10),
    )
    db_session.add(sub)
    await db_session.flush()

    status, expires_at = await get_effective_subscription_status(db_session, user.id)
    assert status == "active"
    assert expires_at is not None


@pytest.mark.asyncio
async def test_effective_subscription_expired(db_session: AsyncSession) -> None:
    """status='active' 但 expires_at<now -> 返回 'expired'。"""
    user = await _create_user(db_session)
    now = datetime.now(UTC)
    sub = _build_subscription(
        user.id,
        status="active",
        starts_at=now - timedelta(days=10),
        expires_at=now - timedelta(days=1),
    )
    db_session.add(sub)
    await db_session.flush()

    status, expires_at = await get_effective_subscription_status(db_session, user.id)
    assert status == "expired"
    assert expires_at is not None


@pytest.mark.asyncio
async def test_effective_subscription_none(db_session: AsyncSession) -> None:
    """无订阅记录 -> 返回 'none'。"""
    user = await _create_user(db_session)
    status, expires_at = await get_effective_subscription_status(db_session, user.id)
    assert status == "none"
    assert expires_at is None


@pytest.mark.asyncio
async def test_subscription_status_literal(db_session: AsyncSession) -> None:
    """get_effective_subscription_status 返回值 status 必须为 active/expired/none 之一。"""
    user = await _create_user(db_session)
    status, _ = await get_effective_subscription_status(db_session, user.id)
    assert status in ("active", "expired", "none")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
