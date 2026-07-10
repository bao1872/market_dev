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
    """直接构造 Subscription 记录（绕过 subscription_factory 对 plans 表的依赖）。"""
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


async def _make_watchlist(
    db: AsyncSession,
    user_id: uuid.UUID,
    instrument_id: uuid.UUID,
) -> UserWatchlistItem:
    """创建 active 自选记录。"""
    item = UserWatchlistItem(
        user_id=user_id,
        instrument_id=instrument_id,
        source="manual",
        active=True,
    )
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
    """同一 user_id 在 instrument_user_map 中只出现一次（防御重复 subscription）。

    说明：当前 subscriptions 表已对 user_id 加唯一约束，真实环境不会出现多条
    active subscription；本测试通过 monkeypatch 模拟 filter_eligible_recipients
    返回重复 user_id，验证 _resolve_watchlist_instruments 的去重结果。
    """
    instrument = await instrument_factory(symbol="600004", market="SH", name="白云机场")
    eligible_user = await user_factory(status="active", roles=["member"])
    await _make_subscription(db_session, eligible_user.id)
    await _make_watchlist(db_session, eligible_user.id, instrument.id)

    service = MonitorBatchService()

    with patch(
        "app.services.eligible_user_service.filter_eligible_recipients",
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
