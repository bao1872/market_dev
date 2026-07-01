"""邀请码套餐绑定测试（SubTask 4.12）。

验证邀请码注册/续期/升级/降级场景下 plan_code/monitor_limit/grant_months 的正确性：
- 生成邀请码：从 plans 表读取 monitor_limit 快照
- 注册：写入 plan_code + entitlement_snapshot，到期日按 grant_months 自然月计算
- 续期（升级）：observe_20 用户用 research_50 邀请码续期，套餐升级、到期日顺延
- 续期（降级）：research_50 用户用 observe_20 邀请码续期，套餐降级
- 续期（同级）：同套餐续期，到期日顺延

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库）
- 直接调用 subscription_service 函数，验证 ORM 字段与到期日计算
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from dateutil.relativedelta import relativedelta
from sqlalchemy import select

from app.core.security import get_password_hash
from app.models.user import Role, User, UserRole
from app.services.subscription_service import (
    generate_invite_codes,
    register_with_invite_code,
    renew_with_invite_code,
)
from app.services.plan_service import get_monitor_limit


async def _ensure_admin_role(db_session) -> Role:
    """确保 admin 角色存在并返回。"""
    result = await db_session.execute(select(Role).where(Role.name == "admin"))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name="admin", description="管理员")
        db_session.add(role)
        await db_session.flush()
    return role


async def _ensure_user_role(db_session) -> Role:
    """确保 user 角色存在并返回。"""
    result = await db_session.execute(select(Role).where(Role.name == "user"))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name="user", description="普通用户")
        db_session.add(role)
        await db_session.flush()
    return role


async def _create_admin(db_session) -> User:
    """创建管理员用户。"""
    admin = User(
        id=uuid.uuid4(),
        email=f"admin_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("admin-password-123"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db_session.add(admin)
    admin_role = await _ensure_admin_role(db_session)
    db_session.add(UserRole(user_id=admin.id, role_id=admin_role.id))
    await db_session.flush()
    return admin


@pytest.mark.asyncio
async def test_generate_invite_codes_observe_20(db_session):
    """生成 observe_20 邀请码：plan_code/monitor_limit/grant_months 正确写入。"""
    admin = await _create_admin(db_session)
    results = await generate_invite_codes(
        db=db_session,
        count=3,
        created_by=admin.id,
        plan_code="observe_20",
        grant_months=2,
        note="observe_20 batch",
    )
    await db_session.flush()

    assert len(results) == 3
    for invite, raw_code in results:
        assert invite.plan_code == "observe_20"
        # 验证邀请码 monitor_limit 快照与 plans 表 observe_20 一致
        assert invite.monitor_limit == await get_monitor_limit(db_session, "observe_20")
        assert invite.monitor_limit == 20
        assert invite.grant_months == 2
        assert isinstance(raw_code, str)
        assert len(raw_code) > 0


@pytest.mark.asyncio
async def test_generate_invite_codes_research_50(db_session):
    """生成 research_50 邀请码：monitor_limit=50。"""
    admin = await _create_admin(db_session)
    results = await generate_invite_codes(
        db=db_session,
        count=2,
        created_by=admin.id,
        plan_code="research_50",
        grant_months=1,
    )
    await db_session.flush()

    for invite, _ in results:
        assert invite.plan_code == "research_50"
        assert invite.monitor_limit == 50
        assert invite.grant_months == 1


@pytest.mark.asyncio
async def test_generate_invite_codes_invalid_plan_code_raises(db_session):
    """未知 plan_code 生成邀请码应抛 ValueError。"""
    admin = await _create_admin(db_session)
    with pytest.raises(ValueError, match="未知套餐代码"):
        await generate_invite_codes(
            db=db_session,
            count=1,
            created_by=admin.id,
            plan_code="unknown_plan",
            grant_months=1,
        )


@pytest.mark.asyncio
async def test_register_writes_plan_code_and_monitor_limit(db_session):
    """注册时写入 plan_code + entitlement_snapshot 到 subscription。"""
    admin = await _create_admin(db_session)
    results = await generate_invite_codes(
        db=db_session,
        count=1,
        created_by=admin.id,
        plan_code="research_50",
        grant_months=3,
    )
    await db_session.flush()
    raw_code = results[0][1]

    email = f"reg_{uuid.uuid4().hex[:8]}@test.com"
    user, subscription = await register_with_invite_code(
        db=db_session,
        email=email,
        password="password-12345",
        raw_invite_code=raw_code,
    )
    await db_session.flush()

    assert subscription.plan_code == "research_50"
    assert subscription.entitlement_snapshot["monitor_limit"] == 50


@pytest.mark.asyncio
async def test_register_expires_at_uses_natural_months(db_session):
    """注册到期日按 grant_months 自然月计算（非 30 天近似）。

    场景：grant_months=1，注册日 2026-01-31，到期日应为 2026-02-28（自然月末）。
    """
    admin = await _create_admin(db_session)
    results = await generate_invite_codes(
        db=db_session,
        count=1,
        created_by=admin.id,
        plan_code="observe_20",
        grant_months=1,
    )
    await db_session.flush()
    raw_code = results[0][1]

    # 固定 now 为 2026-01-31（月末），验证自然月计算
    fake_now = datetime(2026, 1, 31, 12, 0, 0, tzinfo=UTC)
    with patch("app.services.subscription_service.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        email = f"natural_{uuid.uuid4().hex[:8]}@test.com"
        user, subscription = await register_with_invite_code(
            db=db_session,
            email=email,
            password="password-12345",
            raw_invite_code=raw_code,
        )
        await db_session.flush()

    # 自然月：1月31日 + 1月 = 2月28日（2026 非闰年）
    expected = fake_now + relativedelta(months=1)
    assert subscription.expires_at == expected
    # 30 天近似会给 3月2日，自然月给 2月28日，二者不同
    assert subscription.expires_at.day == 28
    assert subscription.expires_at.month == 2


@pytest.mark.asyncio
async def test_renew_upgrade_plan_observe_to_research(db_session):
    """续期升级：observe_20 用户用 research_50 邀请码续期，套餐升级、到期日顺延。"""
    admin = await _create_admin(db_session)
    # 注册用 observe_20 邀请码
    reg_results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin.id,
        plan_code="observe_20", grant_months=1,
    )
    # 续期用 research_50 邀请码
    renew_results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin.id,
        plan_code="research_50", grant_months=2,
    )
    await db_session.flush()

    email = f"upgrade_{uuid.uuid4().hex[:8]}@test.com"
    user, subscription = await register_with_invite_code(
        db=db_session, email=email, password="password-12345",
        raw_invite_code=reg_results[0][1],
    )
    await db_session.flush()

    old_expires = subscription.expires_at
    assert subscription.plan_code == "observe_20"
    assert subscription.entitlement_snapshot["monitor_limit"] == 20

    # 续期升级
    subscription, old_expires_at, new_expires_at = await renew_with_invite_code(
        db=db_session, user_id=user.id,
        raw_invite_code=renew_results[0][1],
    )
    await db_session.flush()

    assert subscription.plan_code == "research_50"
    assert subscription.entitlement_snapshot["monitor_limit"] == 50
    # 未到期续期：从 old_expires 顺延 2 个自然月
    expected = old_expires + relativedelta(months=2)
    assert new_expires_at == expected


@pytest.mark.asyncio
async def test_renew_downgrade_plan_research_to_observe(db_session):
    """续期降级：research_50 用户用 observe_20 邀请码续期，套餐降级。"""
    admin = await _create_admin(db_session)
    reg_results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin.id,
        plan_code="research_50", grant_months=1,
    )
    renew_results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin.id,
        plan_code="observe_20", grant_months=1,
    )
    await db_session.flush()

    email = f"downgrade_{uuid.uuid4().hex[:8]}@test.com"
    user, subscription = await register_with_invite_code(
        db=db_session, email=email, password="password-12345",
        raw_invite_code=reg_results[0][1],
    )
    await db_session.flush()
    assert subscription.plan_code == "research_50"
    assert subscription.entitlement_snapshot["monitor_limit"] == 50

    subscription, old_expires_at, new_expires_at = await renew_with_invite_code(
        db=db_session, user_id=user.id,
        raw_invite_code=renew_results[0][1],
    )
    await db_session.flush()

    assert subscription.plan_code == "observe_20"
    assert subscription.entitlement_snapshot["monitor_limit"] == 20


@pytest.mark.asyncio
async def test_renew_expired_from_today(db_session):
    """已到期续期：从今天计算 grant_months 自然月。"""
    import datetime as dt_module

    admin = await _create_admin(db_session)
    reg_results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin.id,
        plan_code="observe_20", grant_months=1,
    )
    renew_results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin.id,
        plan_code="observe_20", grant_months=3,
    )
    await db_session.flush()

    email = f"expired_{uuid.uuid4().hex[:8]}@test.com"
    user, subscription = await register_with_invite_code(
        db=db_session, email=email, password="password-12345",
        raw_invite_code=reg_results[0][1],
    )
    await db_session.flush()

    # 手动将会员设为已到期
    subscription.expires_at = datetime.now(UTC) - dt_module.timedelta(days=5)
    subscription.status = "expired"
    await db_session.flush()

    renew_start = datetime.now(UTC)
    subscription, old_expires_at, new_expires_at = await renew_with_invite_code(
        db=db_session, user_id=user.id,
        raw_invite_code=renew_results[0][1],
    )
    await db_session.flush()
    renew_end = datetime.now(UTC)

    # 已到期：从 renew_start 到 renew_end 之间的某一刻计算 3 个自然月
    expected_lower = renew_start + relativedelta(months=3)
    expected_upper = renew_end + relativedelta(months=3)
    assert expected_lower <= new_expires_at <= expected_upper, (
        f"new_expires_at={new_expires_at} 不在 [{expected_lower}, {expected_upper}] 范围内"
    )
    assert subscription.status == "active"


@pytest.mark.asyncio
async def test_renew_same_plan_extends_expires(db_session):
    """同套餐续期：套餐不变，到期日顺延。"""
    admin = await _create_admin(db_session)
    reg_results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin.id,
        plan_code="observe_20", grant_months=1,
    )
    renew_results = await generate_invite_codes(
        db=db_session, count=1, created_by=admin.id,
        plan_code="observe_20", grant_months=1,
    )
    await db_session.flush()

    email = f"same_{uuid.uuid4().hex[:8]}@test.com"
    user, subscription = await register_with_invite_code(
        db=db_session, email=email, password="password-12345",
        raw_invite_code=reg_results[0][1],
    )
    await db_session.flush()

    old_expires = subscription.expires_at
    subscription, old_expires_at, new_expires_at = await renew_with_invite_code(
        db=db_session, user_id=user.id,
        raw_invite_code=renew_results[0][1],
    )
    await db_session.flush()

    assert subscription.plan_code == "observe_20"
    assert subscription.entitlement_snapshot["monitor_limit"] == 20
    expected = old_expires + relativedelta(months=1)
    assert new_expires_at == expected


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
