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
from app.services.subscription_service import (
    get_effective_subscription_status,
    get_subscription_status,
    list_subscribers,
)

# [Subscription] - 描述: 测试用默认权益快照（满足 entitlement_snapshot NOT NULL 约束）
_DEFAULT_SNAPSHOT = {
    "monitor_limit": 20,
    "notification_channel_limit": 1,
    "message_retention_days": 30,
    "features": [],
}


def _list_table_names_sync(sync_session) -> set[str]:
    """同步上下文中获取表名集合（供 run_sync 调用）。

    run_sync 回调接收的是 SyncSession，通过 connection() 拿到底层 connection
    再用 inspect 获取表名。
    """
    conn = sync_session.connection()
    return set(inspect(conn).get_table_names())


async def _ensure_member_role(db: AsyncSession) -> Role:
    """确保 member 角色存在并返回（Phase 10：roles 统一为 admin/member）。"""
    result = await db.execute(select(Role).where(Role.name == "member"))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name="member", description="普通会员")
        db.add(role)
        await db.flush()
    return role


async def _create_user(db: AsyncSession, email: str | None = None) -> User:
    """创建普通用户（member 角色，Phase 10 统一）。"""
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
    role = await _ensure_member_role(db)
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
    entitlement_snapshot: dict | None = _DEFAULT_SNAPSHOT,
) -> Subscription:
    """构造 Subscription 对象（不写库，由调用方 add + flush）。

    entitlement_snapshot 默认提供有效快照（满足 NOT NULL 约束）；
    测试 NOT NULL 拒绝时显式传入 None。
    """
    now = datetime.now(UTC)
    return Subscription(
        id=uuid.uuid4(),
        user_id=user_id,
        plan_code=plan_code,
        status=status,
        starts_at=starts_at or now,
        expires_at=expires_at or (now + timedelta(days=30)),
        entitlement_snapshot=entitlement_snapshot,
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


# ============================================================
# Phase 8 RED: status 枚举 / entitlement_snapshot NOT NULL / source 枚举
# 设计要点：expired 不持久化（实时计算），DB CheckConstraint 应拒绝 'expired'；
# status 允许 active/revoked/cancelled；source 允许 invite/admin_grant/migration；
# entitlement_snapshot 必须非空。
# ============================================================


@pytest.mark.asyncio
async def test_subscription_status_revoked_accepted(db_session: AsyncSession) -> None:
    """status='revoked' 应可写入（管理员撤销订阅）。"""
    user = await _create_user(db_session)
    sub = _build_subscription(user.id, status="revoked")
    db_session.add(sub)
    await db_session.flush()  # 不抛异常即通过

    stmt = select(Subscription).where(Subscription.user_id == user.id)
    result = await db_session.execute(stmt)
    fetched = result.scalar_one()
    assert fetched.status == "revoked"


@pytest.mark.asyncio
async def test_subscription_status_cancelled_accepted(db_session: AsyncSession) -> None:
    """status='cancelled' 应可写入（用户主动取消）。"""
    user = await _create_user(db_session)
    sub = _build_subscription(user.id, status="cancelled")
    db_session.add(sub)
    await db_session.flush()

    stmt = select(Subscription).where(Subscription.user_id == user.id)
    result = await db_session.execute(stmt)
    fetched = result.scalar_one()
    assert fetched.status == "cancelled"


@pytest.mark.asyncio
async def test_subscription_status_expired_rejected(db_session: AsyncSession) -> None:
    """status='expired' 应被 DB CheckConstraint 拒绝（expired 实时计算，不持久化）。"""
    user = await _create_user(db_session)
    sub = _build_subscription(user.id, status="expired")
    db_session.add(sub)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_subscription_entitlement_snapshot_not_null(db_session: AsyncSession) -> None:
    """entitlement_snapshot=None 应被 DB NOT NULL 约束拒绝。"""
    user = await _create_user(db_session)
    sub = _build_subscription(user.id, entitlement_snapshot=None)
    db_session.add(sub)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_subscription_source_migration_accepted(db_session: AsyncSession) -> None:
    """source='migration' 应可写入（旧 memberships 数据迁移来源）。"""
    user = await _create_user(db_session)
    sub = _build_subscription(user.id, source="migration")
    db_session.add(sub)
    await db_session.flush()

    stmt = select(Subscription).where(Subscription.user_id == user.id)
    result = await db_session.execute(stmt)
    fetched = result.scalar_one()
    assert fetched.source == "migration"


# ============================================================
# Phase 8 RED: get_subscription_status / list_subscribers 无写副作用
# 设计要点：expired 实时计算，不持久化到 status 字段。
# ============================================================


@pytest.mark.asyncio
async def test_get_subscription_status_no_side_effect(db_session: AsyncSession) -> None:
    """调用 get_subscription_status 后，过期订阅的 status 仍为 'active'（不写 expired）。"""
    user = await _create_user(db_session)
    now = datetime.now(UTC)
    sub = _build_subscription(
        user.id,
        status="active",
        starts_at=now - timedelta(days=10),
        expires_at=now - timedelta(days=1),  # 已过期
    )
    db_session.add(sub)
    await db_session.flush()

    await get_subscription_status(db_session, user.id)

    # 重新查询验证 status 未被写为 'expired'
    stmt = select(Subscription).where(Subscription.user_id == user.id)
    result = await db_session.execute(stmt)
    fetched = result.scalar_one()
    assert fetched.status == "active"


@pytest.mark.asyncio
async def test_list_subscribers_no_expired_write(db_session: AsyncSession) -> None:
    """调用 list_subscribers 后，过期订阅的 status 仍为 'active'（不写 expired）。"""
    user = await _create_user(db_session)
    now = datetime.now(UTC)
    sub = _build_subscription(
        user.id,
        status="active",
        starts_at=now - timedelta(days=10),
        expires_at=now - timedelta(days=1),  # 已过期
    )
    db_session.add(sub)
    await db_session.flush()

    await list_subscribers(db_session)

    # 重新查询验证 status 未被写为 'expired'
    stmt = select(Subscription).where(Subscription.user_id == user.id)
    result = await db_session.execute(stmt)
    fetched = result.scalar_one()
    assert fetched.status == "active"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
