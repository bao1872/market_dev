"""delivery_worker 监控资格测试 - monitor_event 必须使用 monitor 专用资格。

覆盖：
1. active admin 的 monitor_event delivery 不被标记 USER_INELIGIBLE，能走到 _execute_delivery
2. active member + subscription 的 monitor_event delivery 能走到 _execute_delivery
3. disabled admin 的 monitor_event delivery 被标记 dead/USER_INELIGIBLE
4. 无订阅普通用户的 monitor_event delivery 被标记 dead/USER_INELIGIBLE

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_delivery_worker_monitor_eligible.py -q
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import MessageDelivery, NotificationChannel, NotificationMessage
from app.services import delivery_worker


async def _create_monitor_delivery(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    label: str,
) -> MessageDelivery:
    """为指定用户创建一条 monitor_event 来源的 MessageDelivery。"""
    channel = NotificationChannel(
        user_id=user_id,
        adapter_type="feishu_platform_app",
        display_name=f"channel_{label}",
        target_config={"receive_id": "x"},
        status="active",
    )
    db_session.add(channel)
    await db_session.flush()

    message = NotificationMessage(
        user_id=user_id,
        message_type="MONITOR_EVENT",
        template_key="monitor_event",
        template_version="1.0.0",
        source_type="monitor_event",
        source_id=user_id,
        body={
            "title": "test",
            "summary": "test",
            "data_time": "2026-07-06T12:00:00+08:00",
        },
        idempotency_key=f"test-delivery-worker-monitor:{label}:{user_id}",
    )
    db_session.add(message)
    await db_session.flush()

    delivery = MessageDelivery(
        notification_message_id=message.id,
        channel_id=channel.id,
        status="pending",
        delivery_type="text",
        idempotency_key=f"test-delivery-worker-monitor-delivery:{label}:{user_id}",
    )
    db_session.add(delivery)
    await db_session.flush()
    return delivery


@pytest.mark.asyncio
async def test_delivery_worker_monitor_event_uses_monitor_eligibility(
    db_session: AsyncSession,
    user_factory,
    subscription_factory,
    monkeypatch,
):
    """delivery_worker 对 monitor_event 使用 is_user_eligible_for_monitor，放行 active admin。"""
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

    deliveries: dict[str, MessageDelivery] = {}
    for label, user in users.items():
        delivery = await _create_monitor_delivery(db_session, user.id, label)
        deliveries[label] = delivery

    # mock _execute_delivery：只把状态改成 success，不真发飞书
    async def _fake_execute(db, delivery):
        delivery.status = "success"

    monkeypatch.setattr(delivery_worker, "_execute_delivery", _fake_execute)

    processed = await delivery_worker.process_pending_deliveries(
        db_session, batch_size=10, quiet_hours=False,
    )
    assert processed == 2  # active_admin + member

    # 重新加载 delivery 状态
    async def _refresh(delivery_id: uuid.UUID) -> MessageDelivery | None:
        from sqlalchemy import select

        return (await db_session.execute(
            select(MessageDelivery).where(MessageDelivery.id == delivery_id)
        )).scalar_one_or_none()

    refreshed = {label: await _refresh(d.id) for label, d in deliveries.items()}
    assert refreshed["active_admin"] is not None
    assert refreshed["active_admin"].status == "success"
    assert refreshed["active_admin"].last_error_code is None

    assert refreshed["member"] is not None
    assert refreshed["member"].status == "success"
    assert refreshed["member"].last_error_code is None

    assert refreshed["disabled_admin"] is not None
    assert refreshed["disabled_admin"].status == "dead"
    assert refreshed["disabled_admin"].last_error_code == "USER_INELIGIBLE"

    assert refreshed["plain"] is not None
    assert refreshed["plain"].status == "dead"
    assert refreshed["plain"].last_error_code == "USER_INELIGIBLE"
