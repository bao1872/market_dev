"""管理员飞书异步通知测试 (Task 3, SubTask 3.6).

TDD 红灯阶段：先写失败测试，再实现业务代码。

测试内容（对应 spec 第四节"提交后推送管理员飞书"）：
1. Outbox 写入测试（create_application 后 outbox 表有 beta_application_admin 事件，含申请数据）
2. 飞书失败不影响提交（mock 飞书发送失败，create_application 仍返回 is_new=True）
3. 投递状态更新（Outbox 投递成功后 beta_applications.feishu_delivery_status='success'）
4. Outbox 重试（投递失败后 feishu_delivery_status='failed'，retry_feishu 重新入队）

测试策略：
- service 层测试使用真实 PostgreSQL 测试库（允许 commit），测试后清理 beta_applications + outbox
- 飞书发送通过 monkeypatch mock FeishuWebhookAdapter.send，避免真实网络调用
- relay_outbox 直接调用，验证 beta_application_admin 事件处理分支
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beta_application import BetaApplication
from app.models.outbox import Outbox
from app.schemas.beta_application import BetaApplicationCreate
from app.schemas.notification import DeliveryResult
from app.services.beta_application_service import create_application, retry_feishu
from app.services.outbox_relay import relay_outbox


# ============================================================
# 测试 fixtures
# ============================================================


@pytest_asyncio.fixture
async def notifier_db_session() -> AsyncGenerator[AsyncSession, None]:
    """notifier 测试用 DB session（允许 commit，测试后清理）。

    使用真实 PostgreSQL 测试库，测试后清理 beta_applications 与对应 outbox 记录，
    保证测试隔离。不使用 conftest.db_session 的 nested transaction 模式，因为
    create_application 内部需要 db.commit()。
    """
    from tests.conftest import TestAsyncSessionLocal

    session = TestAsyncSessionLocal()
    try:
        yield session
    finally:
        try:
            await session.rollback()
        except Exception:
            pass
        await session.execute(
            text("DELETE FROM outbox WHERE event_type = 'beta_application_admin'")
        )
        await session.execute(text("DELETE FROM beta_applications"))
        await session.commit()
        await session.close()


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


def _set_admin_feishu_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """设置管理员飞书环境变量（测试用）。"""
    monkeypatch.setenv(
        "ADMIN_FEISHU_WEBHOOK_URL",
        "https://open.feishu.cn/open-apis/bot/v2/hook/test-admin",
    )
    monkeypatch.setenv("ADMIN_FEISHU_SIGN_SECRET", "test_sign_secret")


def _mock_feishu_send_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """mock FeishuWebhookAdapter.send 返回成功。"""
    from app.services.feishu_webhook_adapter import FeishuWebhookAdapter

    async def mock_send(self, message_dto, channel_config):
        return DeliveryResult(success=True, provider_response={"code": 0})

    monkeypatch.setattr(FeishuWebhookAdapter, "send", mock_send)


def _mock_feishu_send_failure(monkeypatch: pytest.MonkeyPatch, error_msg: str = "mock failure") -> None:
    """mock FeishuWebhookAdapter.send 返回失败。"""
    from app.services.feishu_webhook_adapter import FeishuWebhookAdapter

    async def mock_send(self, message_dto, channel_config):
        return DeliveryResult(
            success=False,
            error_code="NETWORK_ERROR",
            error_message=error_msg,
        )

    monkeypatch.setattr(FeishuWebhookAdapter, "send", mock_send)


# ============================================================
# SubTask 3.6 测试 1: Outbox 写入测试
# ============================================================


@pytest.mark.asyncio
async def test_create_application_writes_beta_application_admin_outbox(
    notifier_db_session: AsyncSession,
):
    """create_application 后 outbox 表有 beta_application_admin 事件，payload 含申请数据。"""
    payload = BetaApplicationCreate(**_make_payload(wechat="notifier_user_001"))
    ip_hash = _hash_ip("192.168.100.1")

    app, is_new = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=ip_hash
    )
    assert is_new is True

    result = await notifier_db_session.execute(
        select(Outbox)
        .where(Outbox.event_type == "beta_application_admin")
        .where(Outbox.aggregate_id == app.id)
    )
    outbox_records = list(result.scalars().all())
    assert len(outbox_records) >= 1, "未写入 beta_application_admin Outbox 事件"

    outbox = outbox_records[0]
    assert outbox.aggregate_type == "beta_application"
    assert outbox.status == "pending"
    # payload 包含申请数据（不含 webhook 配置，安全考虑）
    assert outbox.payload["application_id"] == str(app.id)
    assert outbox.payload["wechat"] == "notifier_user_001"
    assert outbox.payload["watch_stock_count"] == 10
    # payload 不应包含 webhook_url 或 sign_secret
    assert "webhook_url" not in outbox.payload
    assert "sign_secret" not in outbox.payload


@pytest.mark.asyncio
async def test_create_application_sets_feishu_status_pending(
    notifier_db_session: AsyncSession,
):
    """create_application 后 beta_applications.feishu_delivery_status='pending'。"""
    payload = BetaApplicationCreate(**_make_payload(wechat="notifier_user_002"))
    ip_hash = _hash_ip("192.168.100.2")

    app, is_new = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=ip_hash
    )
    assert is_new is True

    await notifier_db_session.refresh(app)
    assert app.feishu_delivery_status == "pending"


# ============================================================
# SubTask 3.6 测试 2: 飞书失败不影响提交
# ============================================================


@pytest.mark.asyncio
async def test_feishu_failure_does_not_affect_submission(
    notifier_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """飞书发送失败不影响用户提交（create_application 仍返回 is_new=True）。

    场景：system_channel 未配置（返回 None），create_application 仍成功。
    """
    # 不设置环境变量，system_channel 返回 None
    monkeypatch.delenv("ADMIN_FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ADMIN_FEISHU_SIGN_SECRET", raising=False)

    payload = BetaApplicationCreate(**_make_payload(wechat="feishu_fail_user"))
    ip_hash = _hash_ip("192.168.100.3")

    # create_application 应成功（飞书是异步投递，不影响提交）
    app, is_new = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=ip_hash
    )
    assert is_new is True
    assert app.id is not None

    # 申请已写入数据库
    await notifier_db_session.refresh(app)
    assert app.wechat == "feishu_fail_user"


@pytest.mark.asyncio
async def test_relay_without_admin_config_marks_failed(
    notifier_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """system_channel 未配置时，relay_outbox 标记 feishu_delivery_status='failed'。"""
    # 不设置环境变量，system_channel 返回 None
    monkeypatch.delenv("ADMIN_FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ADMIN_FEISHU_SIGN_SECRET", raising=False)

    payload = BetaApplicationCreate(**_make_payload(wechat="no_config_user"))
    ip_hash = _hash_ip("192.168.100.4")
    app, _ = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=ip_hash
    )

    # relay_outbox 处理事件（system_channel 未配置应标记 failed）
    await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
    await notifier_db_session.commit()

    await notifier_db_session.refresh(app)
    assert app.feishu_delivery_status == "failed"
    assert app.feishu_last_error is not None
    assert "not configured" in app.feishu_last_error or "未配置" in app.feishu_last_error


# ============================================================
# SubTask 3.6 测试 3: 投递状态更新（成功）
# ============================================================


@pytest.mark.asyncio
async def test_relay_outbox_success_updates_feishu_status(
    notifier_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """Outbox 投递成功后 beta_applications.feishu_delivery_status='success'。"""
    _set_admin_feishu_env(monkeypatch)
    _mock_feishu_send_success(monkeypatch)

    payload = BetaApplicationCreate(**_make_payload(wechat="relay_success_user"))
    ip_hash = _hash_ip("192.168.100.5")
    app, is_new = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=ip_hash
    )
    assert is_new is True

    # 调用 relay_outbox 处理事件
    processed = await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
    await notifier_db_session.commit()

    assert processed >= 1, "relay_outbox 应处理至少 1 条记录"

    # 验证 beta_applications.feishu_delivery_status='success'
    await notifier_db_session.refresh(app)
    assert app.feishu_delivery_status == "success"
    assert app.feishu_delivered_at is not None
    assert app.feishu_last_error is None

    # 验证 outbox 记录已 processed
    result = await notifier_db_session.execute(
        select(Outbox)
        .where(Outbox.event_type == "beta_application_admin")
        .where(Outbox.aggregate_id == app.id)
    )
    outbox_records = list(result.scalars().all())
    assert any(r.status == "processed" for r in outbox_records), (
        "outbox 记录应标记为 processed"
    )


# ============================================================
# SubTask 3.6 测试 4: Outbox 重试（失败 + retry_feishu）
# ============================================================


@pytest.mark.asyncio
async def test_relay_outbox_failure_marks_failed_and_retry_requeues(
    notifier_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """投递失败后 feishu_delivery_status='failed'，retry_feishu 重新入队。"""
    _set_admin_feishu_env(monkeypatch)
    _mock_feishu_send_failure(monkeypatch, error_msg="mock failure for retry test")

    payload = BetaApplicationCreate(**_make_payload(wechat="relay_fail_user"))
    ip_hash = _hash_ip("192.168.100.6")
    app, is_new = await create_application(
        db=notifier_db_session, payload=payload, ip_hash=ip_hash
    )
    assert is_new is True

    # 调用 relay_outbox 处理事件（max_retry=1 确保第一次失败后标记 failed）
    await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=1)
    await notifier_db_session.commit()

    # 验证 beta_applications.feishu_delivery_status='failed'
    await notifier_db_session.refresh(app)
    assert app.feishu_delivery_status == "failed"
    assert app.feishu_last_error is not None
    assert "mock failure for retry test" in app.feishu_last_error

    # retry_feishu 重新入队
    await notifier_db_session.refresh(app)
    new_outbox = await retry_feishu(db=notifier_db_session, app_id=app.id)
    assert new_outbox.id is not None
    assert new_outbox.event_type == "beta_application_admin"
    assert new_outbox.status == "pending"

    # retry_feishu 后 feishu_delivery_status 应恢复为 pending
    await notifier_db_session.refresh(app)
    assert app.feishu_delivery_status == "pending"


# ============================================================
# SubTask 3.2 测试: 卡片构建
# ============================================================


def test_build_beta_application_card_contains_all_required_fields():
    """飞书卡片必须包含 spec 要求的所有字段。"""
    from app.services.beta_application_notifier import build_beta_application_card

    app = BetaApplication(
        id=__import__("uuid").uuid4(),
        wechat="test_wechat_id",
        phone="13800138000",
        watch_stock_count=15,
        reason_code="busy",
        reason_other=None,
        source="landing_page",
        submitted_at=datetime.now(UTC),
    )

    card = build_beta_application_card(app)

    # 验证卡片结构
    assert "header" in card
    assert "elements" in card
    assert card["header"]["title"]["content"] == "新的内测申请"

    # 验证卡片内容包含所有必需字段（转为 JSON 字符串检查）
    import json
    card_text = json.dumps(card, ensure_ascii=False)
    assert "申请编号" in card_text
    assert "提交时间" in card_text
    assert "微信号" in card_text
    assert "手机号" in card_text
    assert "盯盘" in card_text
    assert "理由" in card_text
    assert "后台" in card_text or "查看" in card_text  # 后台入口


def test_build_beta_application_card_masks_contact_in_summary():
    """卡片正文可包含完整联系方式（管理员飞书是内部通知，需完整信息）。

    注意：管理员飞书是系统级内部通知，发送给管理员，需完整联系方式以便联系用户。
    日志脱敏是针对 logger 的，不是针对管理员飞书内容。
    """
    from app.services.beta_application_notifier import build_beta_application_card

    app = BetaApplication(
        id=__import__("uuid").uuid4(),
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
    # 管理员飞书应包含完整联系方式（便于管理员联系用户）
    assert "13800138000" in card_text
    assert "my_wechat_id" in card_text


# ============================================================
# SubTask 3.1 测试: system_channel
# ============================================================


def test_get_admin_feishu_config_returns_none_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    """环境变量未设置时返回 None。"""
    monkeypatch.delenv("ADMIN_FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ADMIN_FEISHU_SIGN_SECRET", raising=False)

    from app.constants.system_channel import get_admin_feishu_config

    config = get_admin_feishu_config()
    assert config is None


def test_get_admin_feishu_config_returns_dict_when_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    """环境变量设置时返回 dict。"""
    monkeypatch.setenv(
        "ADMIN_FEISHU_WEBHOOK_URL",
        "https://open.feishu.cn/open-apis/bot/v2/hook/test",
    )
    monkeypatch.setenv("ADMIN_FEISHU_SIGN_SECRET", "secret123")

    from app.constants.system_channel import get_admin_feishu_config

    config = get_admin_feishu_config()
    assert config is not None
    assert config["webhook_url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/test"
    assert config["sign_secret"] == "secret123"


def test_get_admin_feishu_config_without_sign_secret(
    monkeypatch: pytest.MonkeyPatch,
):
    """仅设置 webhook_url（无 sign_secret）时返回 dict 不含 sign_secret。"""
    monkeypatch.setenv(
        "ADMIN_FEISHU_WEBHOOK_URL",
        "https://open.feishu.cn/open-apis/bot/v2/hook/test",
    )
    monkeypatch.delenv("ADMIN_FEISHU_SIGN_SECRET", raising=False)

    from app.constants.system_channel import get_admin_feishu_config

    config = get_admin_feishu_config()
    assert config is not None
    assert config["webhook_url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/test"
    assert "sign_secret" not in config


# ============================================================
# 日志脱敏测试
# ============================================================


@pytest.mark.asyncio
async def test_notifier_logs_do_not_leak_full_contact(
    notifier_db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    """notifier 日志中不输出完整手机号/微信号。"""
    _set_admin_feishu_env(monkeypatch)
    _mock_feishu_send_success(monkeypatch)

    payload = BetaApplicationCreate(
        **_make_payload(wechat=None, phone="13900139000")
    )
    ip_hash = _hash_ip("192.168.100.7")

    with caplog.at_level(logging.INFO):
        await create_application(
            db=notifier_db_session, payload=payload, ip_hash=ip_hash
        )
        await relay_outbox(db=notifier_db_session, batch_size=10, max_retry=3)
        await notifier_db_session.commit()

    log_text = caplog.text
    # 完整手机号不得出现在日志中
    assert "13900139000" not in log_text, (
        f"日志中出现了完整手机号: {log_text}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
