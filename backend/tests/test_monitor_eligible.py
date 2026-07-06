"""监控资格边界测试 - admin 与 member 进入监控 universe，disabled/plain 被排除。

覆盖：
1. 旧 filter_eligible_recipients 保持向后兼容（admin 被排除）
2. filter_monitor_eligible_recipients 放行 active admin + active member/subscription
3. is_user_eligible_for_monitor 单条判定
4. MonitorBatchService._resolve_watchlist_instruments 口径一致

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_monitor_eligible.py -q
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import MessageDelivery, NotificationChannel
from app.models.outbox import Outbox
from app.models.watchlist import UserWatchlistItem
from app.schemas.notification import NotificationMessageDTO
from app.services.eligible_user_service import (
    filter_eligible_recipients,
    filter_monitor_eligible_recipients,
    is_user_eligible_for_monitor,
)
from app.services.monitor_batch_service import MonitorBatchService
from app.services.notification_service import create_message
from app.services.outbox_relay import relay_outbox


@pytest.mark.asyncio
async def test_legacy_filter_excludes_admin(
    db_session: AsyncSession,
    user_factory,
    subscription_factory,
):
    """旧资格过滤：admin 被排除，member+subscription 被保留。"""
    admin = await user_factory(roles=["admin"], status="active")
    member = await user_factory(roles=["member"], status="active")
    await subscription_factory(user_id=member.id)
    plain = await user_factory(status="active")

    eligible = set(await filter_eligible_recipients(db_session, [admin.id, member.id, plain.id]))

    assert member.id in eligible
    assert admin.id not in eligible
    assert plain.id not in eligible


@pytest.mark.asyncio
async def test_monitor_filter_boundary(
    db_session: AsyncSession,
    user_factory,
    subscription_factory,
):
    """监控资格过滤：active admin 与 member+subscription 保留；disabled admin、plain 排除。"""
    active_admin = await user_factory(roles=["admin"], status="active")
    disabled_admin = await user_factory(roles=["admin"], status="disabled")
    member = await user_factory(roles=["member"], status="active")
    await subscription_factory(user_id=member.id)
    plain = await user_factory(status="active")

    user_ids = [active_admin.id, disabled_admin.id, member.id, plain.id]
    eligible = set(await filter_monitor_eligible_recipients(db_session, user_ids))

    assert active_admin.id in eligible
    assert member.id in eligible
    assert disabled_admin.id not in eligible
    assert plain.id not in eligible


@pytest.mark.asyncio
async def test_is_user_eligible_for_monitor_single(
    db_session: AsyncSession,
    user_factory,
    subscription_factory,
):
    """单条监控资格判定边界。"""
    active_admin = await user_factory(roles=["admin"], status="active")
    disabled_admin = await user_factory(roles=["admin"], status="disabled")
    member = await user_factory(roles=["member"], status="active")
    await subscription_factory(user_id=member.id)
    from datetime import UTC, datetime, timedelta

    expired_member = await user_factory(roles=["member"], status="active")
    # subscription.status 不允许 'expired'，通过 expires_at 过期来表达失效订阅
    await subscription_factory(
        user_id=expired_member.id,
        status="active",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )

    assert await is_user_eligible_for_monitor(db_session, active_admin.id) is True
    assert await is_user_eligible_for_monitor(db_session, disabled_admin.id) is False
    assert await is_user_eligible_for_monitor(db_session, member.id) is True
    assert await is_user_eligible_for_monitor(db_session, expired_member.id) is False


@pytest.mark.asyncio
async def test_monitor_batch_includes_admin_watchlist(
    db_session: AsyncSession,
    user_factory,
    subscription_factory,
    instrument_factory,
):
    """MonitorBatchService 把 active admin 的自选股纳入监控 universe。"""
    active_admin = await user_factory(roles=["admin"], status="active")
    disabled_admin = await user_factory(roles=["admin"], status="disabled")
    member = await user_factory(roles=["member"], status="active")
    await subscription_factory(user_id=member.id)
    plain = await user_factory(status="active")

    # 600519 是上交所股票；000001+SH 会被 is_index_symbol 识别为指数而被过滤
    instrument = await instrument_factory(symbol="600519", market="SH", status="active")

    db_session.add_all([
        UserWatchlistItem(user_id=active_admin.id, instrument_id=instrument.id, active=True, source="manual"),
        UserWatchlistItem(user_id=disabled_admin.id, instrument_id=instrument.id, active=True, source="manual"),
        UserWatchlistItem(user_id=member.id, instrument_id=instrument.id, active=True, source="manual"),
        UserWatchlistItem(user_id=plain.id, instrument_id=instrument.id, active=True, source="manual"),
    ])
    await db_session.flush()

    service = MonitorBatchService()
    instrument_ids, instrument_user_map, _ = await service._resolve_watchlist_instruments(db_session)

    assert instrument.id in instrument_ids
    users_for_instrument = set(instrument_user_map.get(instrument.id, []))
    assert active_admin.id in users_for_instrument
    assert member.id in users_for_instrument
    assert disabled_admin.id not in users_for_instrument
    assert plain.id not in users_for_instrument


@pytest.mark.asyncio
async def test_outbox_relay_monitor_eligibility_consistency(
    db_session: AsyncSession,
    user_factory,
    subscription_factory,
):
    """outbox_relay 对监控通知使用 monitor 资格过滤，与 batch/event_recipient 口径一致。"""
    active_admin = await user_factory(roles=["admin"], status="active")
    disabled_admin = await user_factory(roles=["admin"], status="disabled")
    member = await user_factory(roles=["member"], status="active")
    await subscription_factory(user_id=member.id)
    plain = await user_factory(status="active")

    users = {
        "active_admin": active_admin,
        "disabled_admin": disabled_admin,
        "member": member,
        "plain": plain,
    }

    # 为每个用户创建 active 渠道与一条监控来源消息
    outbox_records: list[Outbox] = []
    for label, user in users.items():
        channel = NotificationChannel(
            user_id=user.id,
            adapter_type="feishu_platform_app",
            display_name=f"channel_{label}",
            target_config={"receive_id": "x"},
            status="active",
        )
        db_session.add(channel)
        await db_session.flush()

        dto = NotificationMessageDTO(
            message_type="MONITOR_EVENT",
            template_key="monitor_event",
            template_version="1.0.0",
            title="test",
            summary="test",
            resource_refs={},
            data_time="2026-07-05T12:00:00+08:00",
        )
        message = await create_message(
            db=db_session,
            user_id=user.id,
            message_dto=dto,
            source_type="monitor_event",
            source_id=user.id,
            idempotency_key=f"test-monitor-eligibility:{label}:{user.id}",
        )

        outbox = Outbox(
            aggregate_type="notification_message",
            aggregate_id=message.id,
            event_type="notification.message.created",
            payload={
                "message_id": str(message.id),
                "user_id": str(user.id),
                "delivery_type": "text",
            },
            status="pending",
        )
        db_session.add(outbox)
        outbox_records.append(outbox)

    await db_session.flush()

    processed = await relay_outbox(db_session, batch_size=10)
    assert processed == 4

    # 统计每个用户生成的 MessageDelivery 数量
    delivery_counts: dict[str, int] = {}
    for label, user in users.items():
        stmt = select(func.count()).select_from(MessageDelivery).where(
            MessageDelivery.channel_id.in_(
                select(NotificationChannel.id).where(NotificationChannel.user_id == user.id)
            ),
        )
        delivery_counts[label] = (await db_session.scalar(stmt)) or 0

    assert delivery_counts["active_admin"] == 1
    assert delivery_counts["member"] == 1
    assert delivery_counts["disabled_admin"] == 0
    assert delivery_counts["plain"] == 0
