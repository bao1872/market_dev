"""管理员内测申请通知测试 - 复用管理员用户 Platform App 渠道。

TDD 红灯阶段：验证管理员通知通过专用 Outbox 事件扩张为 MessageDelivery，
最终由 delivery worker 调用 FeishuPlatformAppAdapter，不依赖独立管理员凭证。

测试覆盖：
1. active admin + active Platform App 渠道收到通知
2. admin 无 subscription 仍能收到
3. inactive admin 不接收
4. 普通 member 不接收管理员通知
5. 多管理员、多渠道正确扩张
6. 无管理员渠道不影响申请提交
7. 无管理员渠道不无限重试
8. 幂等键阻止重复投递
9. delivery worker 复用 Platform App Adapter
10. 运行时代码零 ADMIN_FEISHU_* / Webhook
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beta_application import BetaApplication
from app.models.notification import MessageDelivery, NotificationChannel, NotificationMessage
from app.models.outbox import Outbox
from app.models.user import Role, User, UserRole
from app.schemas.beta_application import BetaApplicationCreate
from app.schemas.notification import DeliveryResult
from app.services.beta_application_notifier import (
    BETA_APPLICATION_ADMIN_EVENT,
    build_beta_application_card,
    build_beta_application_dto,
)
from app.services.beta_application_service import create_application, retry_feishu
from app.services.delivery_worker import process_pending_deliveries
from app.services.notification_service import create_channel
from app.services.outbox_relay import relay_outbox

# ============================================================
# 测试 fixtures
# ============================================================


@pytest_asyncio.fixture
async def notifier_db_session() -> AsyncGenerator[AsyncSession, None]:
    """notifier 测试用 DB session（允许 commit，测试后清理）。"""
    from tests.conftest import TestAsyncSessionLocal

    session = TestAsyncSessionLocal()
    created_user_ids: list[uuid.UUID] = []
    created_channel_ids: list[uuid.UUID] = []

    session.created_user_ids = created_user_ids
    session.created_channel_ids = created_channel_ids
    try:
        yield session
    finally:
        try:
            await session.rollback()
        except Exception:
            pass
        # 清理顺序：deliveries -> messages -> outbox -> channels -> beta_applications -> user_roles -> users
        await session.execute(text("DELETE FROM message_deliveries"))
        await session.execute(text("DELETE FROM notification_messages"))
        await session.execute(
            text("DELETE FROM outbox WHERE event_type = :event_type"),
            {"event_type": BETA_APPLICATION_ADMIN_EVENT},
        )
        if created_channel_ids:
            for cid in created_channel_ids:
                await session.execute(
                    text("DELETE FROM notification_channels WHERE id = :cid"),
                    {"cid": str(cid)},
                )
        await session.execute(text("DELETE FROM beta_applications"))
        if created_user_ids:
            for uid in created_user_ids:
                await session.execute(
                    text("DELETE FROM user_roles WHERE user_id = :uid"),
                    {"uid": str(uid)},
                )
            for uid in created_user_ids:
                await session.execute(
                    text("DELETE FROM users WHERE id = :uid"),
                    {"uid": str(uid)},
                )
        await session.commit()
        await session.close()


@pytest_asyncio.fixture
async def admin_role(notifier_db_session: AsyncSession) -> Role:
    """创建或获取 admin 角色。"""
    stmt = select(Role).where(Role.name == "admin")
    role = (await notifier_db_session.execute(stmt)).scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name="admin", description="管理员")
        notifier_db_session.add(role)
        await notifier_db_session.commit()
    return role


@pytest_asyncio.fixture
async def member_role(notifier_db_session: AsyncSession) -> Role:
    """创建或获取 member 角色。"""
    stmt = select(Role).where(Role.name == "member")
    role = (await notifier_db_session.execute(stmt)).scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name="member", description="普通会员")
        notifier_db_session.add(role)
        await notifier_db_session.commit()
    return role


async def _create_user(
    db: AsyncSession,
    role: Role,
    status: str = "active",
    email_prefix: str = "user",
) -> User:
    """创建测试用户并分配角色。"""
    user = User(
        id=uuid.uuid4(),
        email=f"{email_prefix}_{uuid.uuid4().hex[:8]}@test.com",
        password_hash="$2b$12$dummyhash",
        status=status,
        timezone="Asia/Shanghai",
    )
    db.add(user)
    db.add(UserRole(user_id=user.id, role_id=role.id))
    await db.commit()
    await db.refresh(user)
    return user


async def _create_active_channel(
    db: AsyncSession,
    user_id: uuid.UUID,
    app_id: str = "cli_test_app_001",
) -> NotificationChannel:
    """创建 active feishu_platform_app 渠道。"""
    channel = await create_channel(
        db=db,
        user_id=user_id,
        adapter_type="feishu_platform_app",
        display_name="Admin Platform App",
        target_config={
            "app_id": app_id,
            "app_secret": "test_secret_value",
            "receive_id": "bg33237",
            "receive_id_type": "user_id",
        },
    )
    channel.status = "active"
    await db.commit()
    await db.refresh(channel)
    # [测试清理] - 追踪创建的渠道 ID，供 fixture finally 块清理
    ids = getattr(db, "created_channel_ids", None)
    if ids is not None:
        ids.append(channel.id)
    return channel


def _hash_ip(ip: str) -> str:
    """计算 IP 的 SHA256 哈希（与 API 层一致）。"""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


def _make_payload(
    *,
    wechat: str | None = "test_wechat",
    phone: str | None = None,
    watch_stock_count: int = 10,
    reason_code: str = "busy",
    reason_other: str | None = None,
    privacy_agreed: bool = True,
) -> dict:
    """构造合法的请求 payload（可覆盖个别字段）。"""
    payload: dict = {
        "watch_stock_count": watch_stock_count,
        "reason_code": reason_code,
        "privacy_agreed": privacy_agreed,
    }
    if wechat is not None:
        payload["wechat"] = wechat
    if phone is not None:
        payload["phone"] = phone
    if reason_other is not None:
        payload["reason_other"] = reason_other
    return payload


def _mock_feishu_send_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """mock FeishuPlatformAppAdapter.send 返回成功。"""
    from app.services.feishu_platform_app_adapter import FeishuPlatformAppAdapter

    async def mock_send(self, message_dto, channel_config):
        return DeliveryResult(success=True, provider_response={"code": 0})

    monkeypatch.setattr(FeishuPlatformAppAdapter, "send", mock_send)


# ============================================================
# 测试 1: active admin + active Platform App 渠道收到通知
# ============================================================


@pytest.mark.asyncio
async def test_create_application_notifies_active_admin_channel(
    notifier_db_session: AsyncSession,
    admin_role: Role,
):
    """create_application -> outbox 专用事件 -> relay -> MessageDelivery。"""
    admin = await _create_user(notifier_db_session, admin_role, email_prefix="admin")
    channel = await _create_active_channel(notifier_db_session, admin.id)

    payload = BetaApplicationCreate(**_make_payload(wechat="notifier_user_001"))
    app, is_new = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=_hash_ip("192.168.100.1")
    )
    assert is_new is True

    # 验证 outbox 事件
    result = await notifier_db_session.execute(
        select(Outbox)
        .where(Outbox.event_type == BETA_APPLICATION_ADMIN_EVENT)
        .where(Outbox.aggregate_id == app.id)
    )
    outbox_records = list(result.scalars().all())
    assert len(outbox_records) == 1
    assert outbox_records[0].payload == {"application_id": str(app.id)}

    # relay 扩张
    processed = await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
    await notifier_db_session.commit()
    assert processed >= 1

    # 验证 NotificationMessage + MessageDelivery
    msg_result = await notifier_db_session.execute(
        select(NotificationMessage).where(NotificationMessage.user_id == admin.id)
    )
    messages = list(msg_result.scalars().all())
    assert len(messages) == 1
    assert messages[0].source_type == "beta_application_admin"

    delivery_result = await notifier_db_session.execute(
        select(MessageDelivery).where(MessageDelivery.channel_id == channel.id)
    )
    deliveries = list(delivery_result.scalars().all())
    assert len(deliveries) == 1
    assert deliveries[0].status == "pending"
    assert deliveries[0].delivery_type == "card"

    # 幂等键包含 application_id + admin_user_id + channel_id
    expected_idem = hashlib.sha256(
        f"{app.id}|{admin.id}|{channel.id}".encode()
    ).hexdigest()
    assert deliveries[0].idempotency_key == expected_idem

    await notifier_db_session.refresh(app)
    assert app.feishu_delivery_status == "pending"


# ============================================================
# 测试 2: admin 无 subscription 仍能收到
# ============================================================


@pytest.mark.asyncio
async def test_admin_without_subscription_receives_notification(
    notifier_db_session: AsyncSession,
    admin_role: Role,
):
    """管理员通知不依赖 subscription，admin 无 subscription 仍创建 MessageDelivery。"""
    admin = await _create_user(notifier_db_session, admin_role, email_prefix="admin_nosub")
    channel = await _create_active_channel(notifier_db_session, admin.id)

    payload = BetaApplicationCreate(**_make_payload(wechat="no_sub_user"))
    app, _ = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=_hash_ip("192.168.100.2")
    )
    await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
    await notifier_db_session.commit()

    delivery_result = await notifier_db_session.execute(
        select(MessageDelivery).where(MessageDelivery.channel_id == channel.id)
    )
    assert len(list(delivery_result.scalars().all())) == 1


# ============================================================
# 测试 3: inactive admin 不接收
# ============================================================


@pytest.mark.asyncio
async def test_inactive_admin_does_not_receive_notification(
    notifier_db_session: AsyncSession,
    admin_role: Role,
):
    """User.status != active 的管理员不接收通知。"""
    admin = await _create_user(
        notifier_db_session, admin_role, status="disabled", email_prefix="admin_inactive"
    )
    await _create_active_channel(notifier_db_session, admin.id)

    payload = BetaApplicationCreate(**_make_payload(wechat="inactive_admin_user"))
    app, _ = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=_hash_ip("192.168.100.3")
    )
    await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
    await notifier_db_session.commit()

    msg_result = await notifier_db_session.execute(
        select(NotificationMessage).where(NotificationMessage.user_id == admin.id)
    )
    assert len(list(msg_result.scalars().all())) == 0

    await notifier_db_session.refresh(app)
    assert app.feishu_delivery_status == "failed"
    assert app.feishu_last_error == "ADMIN_PLATFORM_CHANNEL_NOT_CONFIGURED"


# ============================================================
# 测试 4: 普通 member 不接收管理员通知
# ============================================================


@pytest.mark.asyncio
async def test_member_does_not_receive_admin_notification(
    notifier_db_session: AsyncSession,
    admin_role: Role,
    member_role: Role,
):
    """普通 member 的 active channel 不应收到管理员内测申请通知。"""
    admin = await _create_user(notifier_db_session, admin_role, email_prefix="admin_only")
    await _create_active_channel(notifier_db_session, admin.id)

    member = await _create_user(notifier_db_session, member_role, email_prefix="member")
    member_channel = await _create_active_channel(
        notifier_db_session, member.id, app_id="cli_member_app"
    )

    payload = BetaApplicationCreate(**_make_payload(wechat="member_test_user"))
    app, _ = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=_hash_ip("192.168.100.4")
    )
    await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
    await notifier_db_session.commit()

    delivery_result = await notifier_db_session.execute(
        select(MessageDelivery).where(MessageDelivery.channel_id == member_channel.id)
    )
    assert len(list(delivery_result.scalars().all())) == 0


# ============================================================
# 测试 5: 多管理员、多渠道正确扩张
# ============================================================


@pytest.mark.asyncio
async def test_multiple_admins_multiple_channels(
    notifier_db_session: AsyncSession,
    admin_role: Role,
):
    """多个 active admin 配置渠道时，每个渠道创建独立 MessageDelivery。"""
    admin1 = await _create_user(notifier_db_session, admin_role, email_prefix="admin1")
    admin2 = await _create_user(notifier_db_session, admin_role, email_prefix="admin2")
    channel1 = await _create_active_channel(notifier_db_session, admin1.id, app_id="app1")
    channel2 = await _create_active_channel(notifier_db_session, admin2.id, app_id="app2")

    payload = BetaApplicationCreate(**_make_payload(wechat="multi_admin_user"))
    app, _ = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=_hash_ip("192.168.100.5")
    )
    await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
    await notifier_db_session.commit()

    delivery_result = await notifier_db_session.execute(
        select(MessageDelivery).where(
            MessageDelivery.channel_id.in_([channel1.id, channel2.id])
        )
    )
    deliveries = list(delivery_result.scalars().all())
    assert len(deliveries) == 2
    assert {d.channel_id for d in deliveries} == {channel1.id, channel2.id}


# ============================================================
# 测试 6: 无管理员渠道不影响申请提交
# ============================================================


@pytest.mark.asyncio
async def test_no_admin_channel_does_not_block_submission(
    notifier_db_session: AsyncSession,
    admin_role: Role,
):
    """无 active admin Platform App 渠道时，用户申请仍成功保存。"""
    await _create_user(notifier_db_session, admin_role, email_prefix="admin_no_channel")

    payload = BetaApplicationCreate(**_make_payload(wechat="no_channel_user"))
    app, is_new = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=_hash_ip("192.168.100.6")
    )
    assert is_new is True
    await notifier_db_session.refresh(app)
    assert app.wechat == "no_channel_user"


# ============================================================
# 测试 7: 无管理员渠道不无限重试
# ============================================================


@pytest.mark.asyncio
async def test_no_admin_channel_does_not_retry_infinitely(
    notifier_db_session: AsyncSession,
    admin_role: Role,
):
    """无渠道时 outbox 一次处理后标记 processed，beta_applications 标记 failed。"""
    await _create_user(notifier_db_session, admin_role, email_prefix="admin_no_channel_retry")

    payload = BetaApplicationCreate(**_make_payload(wechat="no_channel_retry_user"))
    app, _ = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=_hash_ip("192.168.100.7")
    )

    # 第一次 relay
    await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
    await notifier_db_session.commit()

    # 第二次 relay 不应再处理（已 processed）
    processed = await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
    await notifier_db_session.commit()
    assert processed == 0

    await notifier_db_session.refresh(app)
    assert app.feishu_delivery_status == "failed"
    assert app.feishu_last_error == "ADMIN_PLATFORM_CHANNEL_NOT_CONFIGURED"


# ============================================================
# 测试 8: 幂等键阻止重复投递
# ============================================================


@pytest.mark.asyncio
async def test_idempotency_prevents_duplicate_deliveries(
    notifier_db_session: AsyncSession,
    admin_role: Role,
):
    """同一 outbox 事件重复 relay 不会创建重复 MessageDelivery。"""
    admin = await _create_user(notifier_db_session, admin_role, email_prefix="admin_idem")
    channel = await _create_active_channel(notifier_db_session, admin.id)

    payload = BetaApplicationCreate(**_make_payload(wechat="idem_user"))
    app, _ = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=_hash_ip("192.168.100.8")
    )

    # 模拟 retry_feishu 重新入队同一事件
    await retry_feishu(db=notifier_db_session, app_id=app.id)
    await notifier_db_session.commit()

    # 两个 outbox 事件
    outbox_result = await notifier_db_session.execute(
        select(Outbox).where(Outbox.event_type == BETA_APPLICATION_ADMIN_EVENT)
    )
    assert len(list(outbox_result.scalars().all())) == 2

    # 两次 relay
    await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
    await notifier_db_session.commit()
    await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
    await notifier_db_session.commit()

    delivery_result = await notifier_db_session.execute(
        select(MessageDelivery).where(MessageDelivery.channel_id == channel.id)
    )
    deliveries = list(delivery_result.scalars().all())
    assert len(deliveries) == 1


# ============================================================
# 测试 9: delivery worker 复用 Platform App Adapter
# ============================================================


@pytest.mark.asyncio
async def test_delivery_worker_uses_platform_app_adapter(
    notifier_db_session: AsyncSession,
    admin_role: Role,
    monkeypatch: pytest.MonkeyPatch,
):
    """delivery worker 通过 FeishuPlatformAppAdapter 投递管理员通知。"""
    admin = await _create_user(notifier_db_session, admin_role, email_prefix="admin_delivery")
    channel = await _create_active_channel(notifier_db_session, admin.id)

    payload = BetaApplicationCreate(**_make_payload(wechat="delivery_user"))
    app, _ = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=_hash_ip("192.168.100.9")
    )
    await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
    await notifier_db_session.commit()

    _mock_feishu_send_success(monkeypatch)
    send_calls = []
    from app.services.feishu_platform_app_adapter import FeishuPlatformAppAdapter

    original_send = FeishuPlatformAppAdapter.send

    async def tracking_send(self, message_dto, channel_config):
        send_calls.append(channel_config)
        return await original_send(self, message_dto, channel_config)

    monkeypatch.setattr(FeishuPlatformAppAdapter, "send", tracking_send)

    success = await process_pending_deliveries(
        db=notifier_db_session, batch_size=10, max_retry=3
    )
    await notifier_db_session.commit()
    assert success == 1
    assert len(send_calls) == 1
    assert send_calls[0]["app_id"] == "cli_test_app_001"

    await notifier_db_session.refresh(app)
    assert app.feishu_delivery_status == "pending"  # delivery worker 不更新 beta_application 状态


# ============================================================
# 测试 10: 卡片构建
# ============================================================


def test_build_beta_application_card_contains_all_required_fields():
    """飞书卡片必须包含 spec 要求的所有字段。"""
    app = BetaApplication(
        id=uuid.uuid4(),
        wechat="test_wechat_id",
        phone="13800138000",
        watch_stock_count=15,
        reason_code="busy",
        reason_other=None,
        source="landing_page",
        submitted_at=datetime.now(UTC),
    )

    card = build_beta_application_card(app)

    assert "header" in card
    assert "elements" in card
    assert card["header"]["title"]["content"] == "新的内测申请"

    import json

    card_text = json.dumps(card, ensure_ascii=False)
    assert "申请编号" in card_text
    assert "提交时间" in card_text
    assert "微信号" in card_text
    assert "手机号" in card_text
    assert "盯盘" in card_text
    assert "理由" in card_text
    assert "后台" in card_text or "查看" in card_text


def test_build_beta_application_card_masks_contact_in_summary():
    """管理员飞书是内部通知，卡片含完整联系方式。"""
    app = BetaApplication(
        id=uuid.uuid4(),
        wechat="my_wechat_id",
        phone="13800138000",
        watch_stock_count=10,
        reason_code="quant",
        reason_other=None,
        source="landing_page",
        submitted_at=datetime.now(UTC),
    )

    card = build_beta_application_card(app)
    import json

    card_text = json.dumps(card, ensure_ascii=False)
    assert "13800138000" in card_text
    assert "my_wechat_id" in card_text


# ============================================================
# 测试 11: 日志脱敏
# ============================================================


@pytest.mark.asyncio
async def test_notifier_logs_do_not_leak_full_contact(
    notifier_db_session: AsyncSession,
    admin_role: Role,
    caplog: pytest.LogCaptureFixture,
):
    """notifier 日志中不输出完整手机号/微信号。"""
    await _create_user(notifier_db_session, admin_role, email_prefix="admin_log")
    await _create_active_channel(notifier_db_session, (await _create_user(
        notifier_db_session, admin_role, email_prefix="admin_log2"
    )).id)

    payload = BetaApplicationCreate(
        **_make_payload(wechat=None, phone="13900139000")
    )

    with caplog.at_level(logging.INFO):
        app, _ = await create_application(
            db=notifier_db_session, payload=payload, ip_hash=_hash_ip("192.168.100.10")
        )
        await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
        await notifier_db_session.commit()

    log_text = caplog.text
    assert "13900139000" not in log_text, (
        f"日志中出现了完整手机号: {log_text}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
