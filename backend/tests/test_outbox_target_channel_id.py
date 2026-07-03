# [治理测试] - 描述: outbox target_channel_id 回归测试（advice §9）
"""outbox target_channel_id 回归测试 - 直接测试 _expand_notification_message_created。

覆盖 advice §9 的 5 个隔离场景：
1. 无 target_channel_id：仍走 eligible_user_service（eligible 扩张，ineligible 不扩张）
2. 有 target_channel_id：跳过 eligible_user_service（即使 ineligible 也扩张）
3. 有 target_channel_id：只匹配指定渠道（不扩张到其他 active 渠道）
4. 非法 target_channel_id：不扩张（返回 0）
5. 无匹配渠道：不创建 delivery（返回 0）

设计要点：
- 直接调用 outbox_relay._expand_notification_message_created，隔离测试目标
- 不通过 relay_outbox，避免 Redis 依赖
- 不 mock is_user_eligible，让真实资格判定生效，验证 ddca659 的逻辑路径
- 与 test_stock_detail_feishu.py 的 HTTP 端到端测试互补

前置 commit：ddca659b8c9d64b6a414da0b4bbd6f80f704aef1
"fix(outbox): skip eligible_user_service for user-triggered notifications"

运行：
    cd backend && pytest tests/test_outbox_target_channel_id.py -q
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.notification import (
    MessageDelivery,
    NotificationChannel,
    NotificationMessage,
)
from app.models.outbox import Outbox
from app.services import outbox_relay

_EVENT_TYPE = "notification.message.created"


def _make_channel(
    user_id, adapter_type: str = "feishu_platform_app", status: str = "active"
) -> NotificationChannel:
    """构造 NotificationChannel 测试实例。"""
    return NotificationChannel(
        id=uuid.uuid4(),
        user_id=user_id,
        adapter_type=adapter_type,
        display_name=f"test-{adapter_type}",
        target_config={},
        status=status,
    )


def _make_message(user_id) -> NotificationMessage:
    """构造 NotificationMessage 测试实例。"""
    return NotificationMessage(
        id=uuid.uuid4(),
        user_id=user_id,
        message_type="MONITOR_EVENT",
        template_key="monitor_event",
        template_version="1.0.0",
        source_type="test",
        body={"text_content": "test"},
        idempotency_key=f"test-msg-{uuid.uuid4()}",
    )


def _make_outbox(
    message_id, user_id, target_channel_id=None
) -> Outbox:
    """构造 notification.message.created Outbox 记录。

    Args:
        message_id: NotificationMessage.id
        user_id: User.id
        target_channel_id: 可选，指定目标渠道 ID（UUID 或非法字符串）
    """
    payload = {
        "message_id": str(message_id),
        "user_id": str(user_id),
        "delivery_type": "text",
    }
    if target_channel_id is not None:
        payload["target_channel_id"] = (
            str(target_channel_id)
            if isinstance(target_channel_id, uuid.UUID)
            else target_channel_id
        )
    return Outbox(
        aggregate_type="notification",
        aggregate_id=None,
        event_type=_EVENT_TYPE,
        payload=payload,
        headers={},
        status="pending",
        retry_count=0,
    )


async def _count_deliveries(db_session, message_id) -> int:
    """统计指定 NotificationMessage 的 MessageDelivery 数量。"""
    result = await db_session.execute(
        select(MessageDelivery).where(
            MessageDelivery.notification_message_id == message_id
        )
    )
    return len(list(result.scalars().all()))


@pytest.mark.asyncio
async def test_no_target_channel_id_uses_eligible_user_service(
    db_session, user_factory, make_user_eligible,
) -> None:
    """无 target_channel_id：eligible 用户扩张，ineligible 用户不扩张。

    验证 ddca659 hotfix 保留的自动通知资格过滤路径。
    """
    # A: eligible 用户（active member + subscription）
    user_a = await user_factory()
    await make_user_eligible(user_a)
    msg_a = _make_message(user_a.id)
    channel_a = _make_channel(user_a.id)
    db_session.add_all([msg_a, channel_a])
    await db_session.flush()
    outbox_a = _make_outbox(msg_a.id, user_a.id, target_channel_id=None)
    db_session.add(outbox_a)
    await db_session.flush()

    # B: ineligible 用户（无角色、无 subscription）
    user_b = await user_factory()
    msg_b = _make_message(user_b.id)
    channel_b = _make_channel(user_b.id)
    db_session.add_all([msg_b, channel_b])
    await db_session.flush()
    outbox_b = _make_outbox(msg_b.id, user_b.id, target_channel_id=None)
    db_session.add(outbox_b)
    await db_session.flush()

    expanded_a = await outbox_relay._expand_notification_message_created(
        db_session, outbox_a
    )
    expanded_b = await outbox_relay._expand_notification_message_created(
        db_session, outbox_b
    )

    assert expanded_a == 1, "eligible 用户应扩张出 1 条 delivery"
    assert expanded_b == 0, "ineligible 用户应被 eligible_user_service 过滤，扩张 0 条"
    assert await _count_deliveries(db_session, msg_a.id) == 1
    assert await _count_deliveries(db_session, msg_b.id) == 0


@pytest.mark.asyncio
async def test_with_target_channel_id_skips_eligible_user_service(
    db_session, user_factory,
) -> None:
    """有 target_channel_id：跳过 eligible_user_service，ineligible 用户也扩张。

    验证 ddca659 hotfix 的核心修复：用户主动触发/手动指定渠道的通知不受订阅状态限制。
    """
    # ineligible 用户（无角色、无 subscription）
    user = await user_factory()
    msg = _make_message(user.id)
    channel = _make_channel(user.id)
    db_session.add_all([msg, channel])
    await db_session.flush()
    outbox = _make_outbox(msg.id, user.id, target_channel_id=channel.id)
    db_session.add(outbox)
    await db_session.flush()

    expanded = await outbox_relay._expand_notification_message_created(
        db_session, outbox
    )

    assert expanded == 1, (
        "有 target_channel_id 时应跳过 eligible_user_service，即使无资格也扩张"
    )
    assert await _count_deliveries(db_session, msg.id) == 1


@pytest.mark.asyncio
async def test_with_target_channel_id_only_matches_specified_channel(
    db_session, user_factory, make_user_eligible,
) -> None:
    """有 target_channel_id：只匹配指定渠道，不扩张到其他 active 渠道。

    注：uq_notification_channels_active_feishu 约束禁止同一用户拥有多个 active
    feishu_platform_app 渠道，因此用 1 条 feishu（target）+ 1 条 email（other active）
    验证：target_channel_id 指向 feishu 时，email 渠道不会被扩张。
    """
    user = await user_factory()
    await make_user_eligible(user)
    msg = _make_message(user.id)

    # 创建 2 条 active 渠道：feishu_platform_app（target）+ email（other active）
    target_channel = _make_channel(user.id, adapter_type="feishu_platform_app")
    other_channel = _make_channel(user.id, adapter_type="email")
    db_session.add_all([msg, target_channel, other_channel])
    await db_session.flush()

    # payload 指向 target_channel（feishu_platform_app）
    outbox = _make_outbox(msg.id, user.id, target_channel_id=target_channel.id)
    db_session.add(outbox)
    await db_session.flush()

    expanded = await outbox_relay._expand_notification_message_created(
        db_session, outbox
    )

    assert expanded == 1, "只应为指定 target_channel_id 创建 1 条 delivery"
    deliveries = await db_session.execute(
        select(MessageDelivery).where(
            MessageDelivery.notification_message_id == msg.id
        )
    )
    delivery_list = list(deliveries.scalars().all())
    assert len(delivery_list) == 1
    assert delivery_list[0].channel_id == target_channel.id, (
        "delivery 的 channel_id 必须等于 payload 指定的 target_channel_id，"
        "不应扩张到 email 渠道"
    )


@pytest.mark.asyncio
async def test_invalid_target_channel_id_does_not_expand(
    db_session, user_factory, make_user_eligible,
) -> None:
    """非法 target_channel_id（非 UUID）：不扩张，返回 0。"""
    user = await user_factory()
    await make_user_eligible(user)
    msg = _make_message(user.id)
    channel = _make_channel(user.id)
    db_session.add_all([msg, channel])
    await db_session.flush()

    # 非法 UUID 字符串
    outbox = _make_outbox(msg.id, user.id, target_channel_id="not-a-uuid")
    db_session.add(outbox)
    await db_session.flush()

    expanded = await outbox_relay._expand_notification_message_created(
        db_session, outbox
    )

    assert expanded == 0, "非法 target_channel_id 应返回 0，不扩张"
    assert await _count_deliveries(db_session, msg.id) == 0


@pytest.mark.asyncio
async def test_no_matching_channel_does_not_create_delivery(
    db_session, user_factory, make_user_eligible,
) -> None:
    """无匹配渠道：target_channel_id 指向不存在的渠道，不创建 delivery。"""
    user = await user_factory()
    await make_user_eligible(user)
    msg = _make_message(user.id)
    # 创建一个 disabled 渠道（status != active，不会被匹配）
    disabled_channel = _make_channel(user.id, status="disabled")
    db_session.add_all([msg, disabled_channel])
    await db_session.flush()

    # target_channel_id 指向一个不存在于数据库的 UUID
    nonexistent_channel_id = uuid.uuid4()
    outbox = _make_outbox(
        msg.id, user.id, target_channel_id=nonexistent_channel_id
    )
    db_session.add(outbox)
    await db_session.flush()

    expanded = await outbox_relay._expand_notification_message_created(
        db_session, outbox
    )

    assert expanded == 0, "无匹配 active 渠道应返回 0，不创建 delivery"
    assert await _count_deliveries(db_session, msg.id) == 0


if __name__ == "__main__":
    # 自测入口：验证模块可导入（不连接数据库）
    print(f"_EVENT_TYPE={_EVENT_TYPE}")
    print(f"outbox_relay._expand_notification_message_created="
          f"{outbox_relay._expand_notification_message_created}")
    print("OK")
