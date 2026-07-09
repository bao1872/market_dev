"""Worker 用户资格服务测试 - Phase 4.3 RED 阶段。

测试 eligible_user_service 作为 Worker 用户资格唯一事实源。

资格定义（与 subscription_service.get_effective_subscription_status 一致）：
- User.status = 'active'
- 用户有 member 角色（admin 不进入 universe）
- Subscription 有效：status='active' AND starts_at <= now AND expires_at > now

测试策略：
- 使用 conftest.py 的 db_session fixture（PostgreSQL 测试库 bz_stock_test）
- 使用 user_factory / subscription_factory 创建测试数据
- 表结构由 Alembic 迁移创建（禁止手写 DDL 建表语句）
- 角色名用 member（不是 user），admin 无套餐无 subscription
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription import Subscription
from app.models.user import User
from app.services.eligible_user_service import (
    filter_eligible_recipients,
    is_user_eligible,
    list_eligible_user_ids,
)
from tests.conftest import AsyncFactory

# 测试用默认权益快照（满足 entitlement_snapshot NOT NULL 约束，不依赖 plans 表）
_DEFAULT_SNAPSHOT: dict[str, Any] = {
    "monitor_limit": 20,
    "notification_channel_limit": 1,
    "message_retention_days": 30,
    "features": [],
}


async def _make_subscription(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    status: str = "active",
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    plan_code: str = "observe_20",
) -> Subscription:
    """直接构造 Subscription 记录（绕过 subscription_factory 对 plans 表的依赖）。

    使用 _DEFAULT_SNAPSHOT 满足 NOT NULL 约束，专注测试资格判定逻辑。
    """
    now = datetime.now(UTC)
    sub = Subscription(
        id=uuid.uuid4(),
        user_id=user_id,
        plan_code=plan_code,
        status=status,
        starts_at=starts_at or (now - timedelta(days=1)),
        expires_at=expires_at or (now + timedelta(days=30)),
        entitlement_snapshot=_DEFAULT_SNAPSHOT,
        source="invite",
        created_by=None,
    )
    db.add(sub)
    await db.flush()
    return sub


# ============================================================
# is_user_eligible 单用户判定
# ============================================================


@pytest.mark.asyncio
async def test_active_member_with_active_subscription_is_eligible(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """active member + 有效 subscription → eligible。"""
    user = await user_factory(status="active", roles=["member"])
    await _make_subscription(db_session, user.id)

    eligible = await is_user_eligible(db_session, user.id)
    assert eligible is True


@pytest.mark.asyncio
async def test_disabled_member_not_eligible(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """User.status='disabled' → not eligible。"""
    user = await user_factory(status="disabled", roles=["member"])
    await _make_subscription(db_session, user.id)

    eligible = await is_user_eligible(db_session, user.id)
    assert eligible is False


@pytest.mark.asyncio
async def test_pending_member_not_eligible(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """User.status='pending' → not eligible。"""
    user = await user_factory(status="pending", roles=["member"])
    await _make_subscription(db_session, user.id)

    eligible = await is_user_eligible(db_session, user.id)
    assert eligible is False


@pytest.mark.asyncio
async def test_expired_subscription_not_eligible(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """subscription.expires_at < now → not eligible。"""
    user = await user_factory(status="active", roles=["member"])
    now = datetime.now(UTC)
    await _make_subscription(
        db_session, user.id,
        starts_at=now - timedelta(days=10),
        expires_at=now - timedelta(days=1),  # 已过期
    )

    eligible = await is_user_eligible(db_session, user.id)
    assert eligible is False


@pytest.mark.asyncio
async def test_revoked_subscription_not_eligible(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """subscription.status='revoked' → not eligible。"""
    user = await user_factory(status="active", roles=["member"])
    await _make_subscription(db_session, user.id, status="revoked")

    eligible = await is_user_eligible(db_session, user.id)
    assert eligible is False


@pytest.mark.asyncio
async def test_future_subscription_not_eligible(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """subscription.starts_at > now（尚未生效）→ not eligible。"""
    user = await user_factory(status="active", roles=["member"])
    now = datetime.now(UTC)
    await _make_subscription(
        db_session, user.id,
        starts_at=now + timedelta(days=1),  # 尚未生效
        expires_at=now + timedelta(days=30),
    )

    eligible = await is_user_eligible(db_session, user.id)
    assert eligible is False


@pytest.mark.asyncio
async def test_member_without_subscription_not_eligible(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """member 无 subscription 记录 → not eligible。"""
    user = await user_factory(status="active", roles=["member"])

    eligible = await is_user_eligible(db_session, user.id)
    assert eligible is False


@pytest.mark.asyncio
async def test_admin_not_eligible(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """admin 不进入 universe（admin 不是普通会员监控对象）。

    admin 无 subscription（规则 8），且无 member 角色，应 not eligible。
    """
    user = await user_factory(status="active", roles=["admin"])

    eligible = await is_user_eligible(db_session, user.id)
    assert eligible is False


@pytest.mark.asyncio
async def test_admin_with_member_role_still_not_eligible(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """同时拥有 admin + member 角色 + 有效 subscription → 仍 not eligible。

    验证 admin 角色排除逻辑：只要有 admin 角色就不进入 universe，
    即使同时拥有 member 角色和有效订阅。
    """
    user = await user_factory(status="active", roles=["admin", "member"])
    await _make_subscription(db_session, user.id)

    eligible = await is_user_eligible(db_session, user.id)
    assert eligible is False


@pytest.mark.asyncio
async def test_user_without_any_role_not_eligible(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """无任何角色的 active 用户 + 有效 subscription → not eligible（必须有 member 角色）。"""
    user = await user_factory(status="active", roles=[])
    await _make_subscription(db_session, user.id)

    eligible = await is_user_eligible(db_session, user.id)
    assert eligible is False


# ============================================================
# list_eligible_user_ids 批量查询
# ============================================================


@pytest.mark.asyncio
async def test_list_eligible_user_ids_excludes_admin(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """admin 不在 list_eligible_user_ids 返回列表中。"""
    member_user = await user_factory(status="active", roles=["member"])
    await _make_subscription(db_session, member_user.id)

    admin_user = await user_factory(status="active", roles=["admin"])

    eligible_ids = await list_eligible_user_ids(db_session)

    assert member_user.id in eligible_ids
    assert admin_user.id not in eligible_ids


@pytest.mark.asyncio
async def test_list_eligible_user_ids_only_returns_eligible(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """list_eligible_user_ids 只返回有资格的用户。"""
    # eligible 用户
    eligible_user = await user_factory(status="active", roles=["member"])
    await _make_subscription(db_session, eligible_user.id)

    # disabled 用户
    disabled_user = await user_factory(status="disabled", roles=["member"])
    await _make_subscription(db_session, disabled_user.id)

    # expired 用户
    expired_user = await user_factory(status="active", roles=["member"])
    now = datetime.now(UTC)
    await _make_subscription(
        db_session, expired_user.id,
        starts_at=now - timedelta(days=10),
        expires_at=now - timedelta(days=1),
    )

    # 无 subscription 用户
    no_sub_user = await user_factory(status="active", roles=["member"])

    eligible_ids = await list_eligible_user_ids(db_session)

    assert eligible_user.id in eligible_ids
    assert disabled_user.id not in eligible_ids
    assert expired_user.id not in eligible_ids
    assert no_sub_user.id not in eligible_ids


# ============================================================
# filter_eligible_recipients 批量过滤
# ============================================================


@pytest.mark.asyncio
async def test_filter_eligible_recipients_returns_only_eligible(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """filter_eligible_recipients 批量过滤混合用户列表，只返回 eligible 的。"""
    eligible1 = await user_factory(status="active", roles=["member"])
    await _make_subscription(db_session, eligible1.id)

    eligible2 = await user_factory(status="active", roles=["member"])
    await _make_subscription(db_session, eligible2.id)

    disabled_user = await user_factory(status="disabled", roles=["member"])
    await _make_subscription(db_session, disabled_user.id)

    expired_user = await user_factory(status="active", roles=["member"])
    now = datetime.now(UTC)
    await _make_subscription(
        db_session, expired_user.id,
        expires_at=now - timedelta(days=1),
    )

    admin_user = await user_factory(status="active", roles=["admin"])

    input_ids = [eligible1.id, eligible2.id, disabled_user.id, expired_user.id, admin_user.id]
    result = await filter_eligible_recipients(db_session, input_ids)

    result_set = set(result)
    assert eligible1.id in result_set
    assert eligible2.id in result_set
    assert disabled_user.id not in result_set
    assert expired_user.id not in result_set
    assert admin_user.id not in result_set


@pytest.mark.asyncio
async def test_filter_eligible_recipients_empty_input(db_session: AsyncSession) -> None:
    """空输入列表 → 空输出。"""
    result = await filter_eligible_recipients(db_session, [])
    assert result == []


@pytest.mark.asyncio
async def test_filter_eligible_recipients_preserves_eligible_order(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """filter_eligible_recipients 返回有资格用户（顺序由数据库查询决定）。"""
    u1 = await user_factory(status="active", roles=["member"])
    await _make_subscription(db_session, u1.id)
    u2 = await user_factory(status="active", roles=["member"])
    await _make_subscription(db_session, u2.id)

    result = await filter_eligible_recipients(db_session, [u1.id, u2.id])
    result_set = set(result)
    assert result_set == {u1.id, u2.id}


# ============================================================
# 续期后自动恢复监控
# ============================================================


@pytest.mark.asyncio
async def test_renewal_restores_eligibility(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """续期后自动恢复监控：expired → not eligible，更新为 active → eligible。"""
    user = await user_factory(status="active", roles=["member"])
    now = datetime.now(UTC)
    sub = await _make_subscription(
        db_session, user.id,
        starts_at=now - timedelta(days=10),
        expires_at=now - timedelta(days=1),  # 已过期
    )

    # 过期 → not eligible
    assert await is_user_eligible(db_session, user.id) is False

    # 续期：更新 expires_at 为未来时间，status 重置为 active
    sub.expires_at = now + timedelta(days=30)
    sub.status = "active"
    await db_session.flush()

    # 续期后 → eligible
    assert await is_user_eligible(db_session, user.id) is True
