"""Monitor eligibility 集成测试 - watchlist → eligible user 过滤。

验证 MonitorBatchService._resolve_watchlist_instruments 正确过滤：
- active member + active subscription 进入监控 universe
- expired / disabled / no-subscription 用户被排除
- 同一用户在 instrument_user_map 中只出现一次（去重）

业务验证范围限定为资格过滤与进程心跳，不验证通知投递
（outbox/delivery/capture 保持关闭）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.subscription import Subscription
from app.models.user import User
from app.models.watchlist import UserWatchlistItem
from app.services.monitor_batch_service import MonitorBatchService
from tests.conftest import AsyncFactory

# 测试用默认权益快照（满足 entitlement_snapshot NOT NULL 约束）
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

    [V2.1] 同时创建 capability grants（模拟 Phase I backfill），
    使监控资格检查（filter_users_with_capability）能通过。
    """
    from app.constants.capability_keys import MARKET_SCREENING, WATCHLIST_MANAGEMENT
    from app.models.capability_grant import UserCapabilityGrant

    now = datetime.now(UTC)
    # 确保 starts_at < expires_at（避免 ck_grant_expires_after_starts 违约）
    if starts_at is None and expires_at is not None:
        starts_at = expires_at - timedelta(days=1)
    else:
        starts_at = starts_at or (now - timedelta(days=1))
        expires_at = expires_at or (now + timedelta(days=30))

    sub = Subscription(
        id=uuid.uuid4(),
        user_id=user_id,
        plan_code=plan_code,
        status=status,
        starts_at=starts_at,
        expires_at=expires_at,
        entitlement_snapshot=_DEFAULT_SNAPSHOT,
        source="invite",
        created_by=None,
    )
    db.add(sub)
    await db.flush()

    # [V2.1] 创建 capability grants（有效期与 subscription 对齐）
    monitor_limit = _DEFAULT_SNAPSHOT["monitor_limit"]
    db.add(UserCapabilityGrant(
        user_id=user_id,
        capability_key=WATCHLIST_MANAGEMENT,
        limit_value=int(monitor_limit),
        source_type="legacy_subscription",
        source_id=str(sub.id),
        starts_at=starts_at,
        expires_at=expires_at,
        revoked_at=None,
    ))
    db.add(UserCapabilityGrant(
        user_id=user_id,
        capability_key=MARKET_SCREENING,
        limit_value=None,
        source_type="legacy_subscription",
        source_id=str(sub.id),
        starts_at=starts_at,
        expires_at=expires_at,
        revoked_at=None,
    ))
    await db.flush()
    return sub


async def _make_watchlist(
    db: AsyncSession,
    user_id: uuid.UUID,
    instrument_id: uuid.UUID,
    *,
    created_at: datetime | None = None,
) -> UserWatchlistItem:
    """创建 active 自选记录。

    Args:
        created_at: 显式设置 created_at（用于测试排序）；None 用 DB 默认 now()
    """
    item = UserWatchlistItem(
        user_id=user_id,
        instrument_id=instrument_id,
        source="manual",
        active=True,
    )
    if created_at is not None:
        item.created_at = created_at
    db.add(item)
    await db.flush()
    return item


@pytest.mark.asyncio
async def test_resolve_watchlist_instruments_eligibility_filter(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
    instrument_factory: AsyncFactory[Instrument],
) -> None:
    """4 类用户添加同一只股票，仅 active member + active subscription 进入 universe。"""
    instrument = await instrument_factory(symbol="600000", market="SH", name="浦发银行")

    # 1. active member + active subscription → eligible
    active_member = await user_factory(status="active", roles=["member"])
    await _make_subscription(db_session, active_member.id)
    await _make_watchlist(db_session, active_member.id, instrument.id)

    # 2. expired subscription → not eligible
    expired_user = await user_factory(status="active", roles=["member"])
    now = datetime.now(UTC)
    await _make_subscription(
        db_session, expired_user.id,
        starts_at=now - timedelta(days=10),
        expires_at=now - timedelta(days=1),
    )
    await _make_watchlist(db_session, expired_user.id, instrument.id)

    # 3. disabled user → not eligible
    disabled_user = await user_factory(status="disabled", roles=["member"])
    await _make_subscription(db_session, disabled_user.id)
    await _make_watchlist(db_session, disabled_user.id, instrument.id)

    # 4. no subscription → not eligible
    no_sub_user = await user_factory(status="active", roles=["member"])
    await _make_watchlist(db_session, no_sub_user.id, instrument.id)

    service = MonitorBatchService()
    instrument_ids, instrument_user_map, _ = await service._resolve_watchlist_instruments(db_session)

    assert instrument.id in instrument_ids
    assert instrument_user_map[instrument.id] == [active_member.id]
    assert expired_user.id not in instrument_user_map.get(instrument.id, [])
    assert disabled_user.id not in instrument_user_map.get(instrument.id, [])
    assert no_sub_user.id not in instrument_user_map.get(instrument.id, [])


@pytest.mark.asyncio
async def test_resolve_watchlist_instruments_dedups_user_id(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
    instrument_factory: AsyncFactory[Instrument],
) -> None:
    """同一 user_id 在 instrument_user_map 中只出现一次（防御重复 eligibility）。

    [V2.1] 监控资格通过 capability_service.filter_users_with_capability 判定。
    本测试通过 monkeypatch 模拟返回重复 user_id，验证 _resolve_watchlist_instruments
    的去重结果（eligible_user_ids 使用 set 去重）。
    """
    instrument = await instrument_factory(symbol="600004", market="SH", name="白云机场")
    eligible_user = await user_factory(status="active", roles=["member"])
    await _make_subscription(db_session, eligible_user.id)
    await _make_watchlist(db_session, eligible_user.id, instrument.id)

    service = MonitorBatchService()

    # [V2.1] patch capability_service.filter_users_with_capability 返回重复 user_id
    with patch(
        "app.services.capability_service.filter_users_with_capability",
        return_value=[eligible_user.id, eligible_user.id],
    ):
        instrument_ids, instrument_user_map, _ = await service._resolve_watchlist_instruments(db_session)

    assert instrument.id in instrument_ids
    user_ids = instrument_user_map.get(instrument.id, [])
    assert user_ids == [eligible_user.id]
    assert len(user_ids) == len(set(user_ids))


@pytest.mark.asyncio
async def test_eligible_user_service_distinct_user_id(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
) -> None:
    """filter_eligible_recipients 返回的用户 ID 列表元素唯一。

    subscriptions 表 user_id 唯一约束保证当前 schema 不会真实出现多条 active
    subscription；此测试直接验证 DISTINCT 防御逻辑生效。
    """
    from app.services.eligible_user_service import (
        filter_eligible_recipients,
        list_eligible_user_ids,
    )

    eligible_user = await user_factory(status="active", roles=["member"])
    await _make_subscription(db_session, eligible_user.id)

    all_ids = await list_eligible_user_ids(db_session)
    filtered_ids = await filter_eligible_recipients(db_session, [eligible_user.id, eligible_user.id])

    assert len(all_ids) == len(set(all_ids))
    assert filtered_ids == [eligible_user.id]


@pytest.mark.asyncio
async def test_resolve_watchlist_instruments_respects_per_user_limit(
    db_session: AsyncSession,
    user_factory: AsyncFactory[User],
    instrument_factory: AsyncFactory[Instrument],
) -> None:
    """[V2.1 F3] 监控 universe 只覆盖每用户前 N 只（by created_at ASC, id ASC）。

    场景：
    - observe_20 用户（limit=20）添加 3 只自选股
    - 修改 capability grant 的 limit_value=2，模拟降级
    - 验证只有前 2 只进入监控 universe（按 created_at ASC 排序）
    """
    from sqlalchemy import select as sa_select

    from app.constants.capability_keys import WATCHLIST_MANAGEMENT
    from app.models.capability_grant import UserCapabilityGrant

    # 创建 3 只不同股票
    inst1 = await instrument_factory(symbol="600001", market="SH", name="A1")
    inst2 = await instrument_factory(symbol="600002", market="SH", name="A2")
    inst3 = await instrument_factory(symbol="600003", market="SH", name="A3")

    # 创建用户 + subscription + grants（limit=20）
    user = await user_factory(status="active", roles=["member"])
    await _make_subscription(db_session, user.id)

    # 添加 3 只自选股（显式 created_at，确保按时间 ASC 排序时 inst1 < inst2 < inst3）
    base_time = datetime.now(UTC)
    await _make_watchlist(db_session, user.id, inst1.id, created_at=base_time - timedelta(minutes=3))
    await _make_watchlist(db_session, user.id, inst2.id, created_at=base_time - timedelta(minutes=2))
    await _make_watchlist(db_session, user.id, inst3.id, created_at=base_time - timedelta(minutes=1))

    # 修改 capability grant 的 limit_value=2，模拟降级
    grant_result = await db_session.execute(
        sa_select(UserCapabilityGrant).where(
            UserCapabilityGrant.user_id == user.id,
            UserCapabilityGrant.capability_key == WATCHLIST_MANAGEMENT,
            UserCapabilityGrant.revoked_at.is_(None),
        )
    )
    for grant in grant_result.scalars():
        grant.limit_value = 2
    await db_session.flush()

    service = MonitorBatchService()
    instrument_ids, instrument_user_map, _ = await service._resolve_watchlist_instruments(db_session)

    # 验证只有前 2 只进入监控 universe（inst1 和 inst2，按 created_at ASC）
    assert inst1.id in instrument_ids, "第 1 只（created_at 最早）应在监控范围内"
    assert inst2.id in instrument_ids, "第 2 只应在监控范围内"
    assert inst3.id not in instrument_ids, "第 3 只超出额度，不应进入监控 universe"
