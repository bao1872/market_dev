"""C9 通知与消息测试 - 覆盖消息构建、飞书适配器、卡片构建、投递 worker。

测试策略：
- 纯函数测试（message_builder、feishu_card_builder、签名）：直接验证
- 适配器注册测试：验证 feishu_webhook 已注册
- API 端点测试：使用 FastAPI TestClient
- DB 依赖测试：使用 mock 模拟 AsyncSession

不依赖外部 Redis/PostgreSQL/飞书服务，确保测试可在 CI 环境运行。
"""

from __future__ import annotations

import hashlib
import hmac
import base64
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.notification import (
    NotificationMessageDTO,
    NotificationMessageResponse,
    DeliveryResult,
    NotificationPreviewRequest,
    NotificationPreviewResponse,
    NotificationChannelResponse,
    ChannelTestResponse,
)
from app.services.message_builder import (
    build_message,
    build_monitor_event,
    build_system_alert,
    build_channel_alert,
    MessageBuilderError,
)
from app.services.feishu_card_builder import (
    dto_to_feishu_card,
    mask_webhook_url,
)
from app.services.feishu_webhook_adapter import (
    _sign,
    _build_webhook_payload,
    FeishuWebhookAdapter,
)
from app.services.channel_adapter import (
    get_adapter,
    list_supported_adapters,
    MockChannelAdapter,
)
from app.services.delivery_worker import (
    _is_quiet_hours,
    process_notification_outbox,
    get_pending_notification_count,
)


# ==================== 消息构建器测试 ====================


class TestMessageBuilder:
    """消息构建器测试 - 覆盖全部消息类型。"""

    def test_build_monitor_event(self) -> None:
        """测试监控事件消息构建。"""
        dto = build_monitor_event(
            stock_name="贵州茅台",
            event_type="evt_dsa_dir_flip_up",
            event_time="2026-06-18T10:18:00+08:00",
            member_name="DSA选股",
            role="TRIGGER",
            summary_text="DSA 方向翻多",
            resource_refs={"instrument_id": "600519.SH"},
        )
        assert dto.message_type == "MONITOR_EVENT"
        assert dto.template_key == "monitor_event"
        assert dto.template_version == "1.1.0"
        assert "贵州茅台" in dto.title
        assert dto.data_time == "2026-06-18T10:18:00+08:00"
        assert len(dto.facts) == 3

    def test_build_system_alert(self) -> None:
        """测试系统异常消息构建。"""
        dto = build_system_alert(
            alert_type="DATA_STALE",
            message="日线行情数据已过期 30 分钟",
            resource_refs={"service": "bars_daily"},
        )
        assert dto.message_type == "SYSTEM_ALERT"
        assert "DATA_STALE" in dto.title
        assert "日线行情数据已过期" in dto.summary

    def test_build_channel_alert(self) -> None:
        """测试渠道异常消息构建。"""
        dto = build_channel_alert(
            channel_name="飞书Webhook",
            error_code="WEBHOOK_INVALID",
            error_message="Webhook URL 返回 404",
            resource_refs={"channel_id": "ch_001"},
        )
        assert dto.message_type == "CHANNEL_ALERT"
        assert "飞书Webhook" in dto.title
        assert "WEBHOOK_INVALID" in dto.facts[1]["value"]

    def test_build_message_invalid_type(self) -> None:
        """测试不支持的消息类型。"""
        with pytest.raises(MessageBuilderError, match="不支持的消息类型"):
            build_message("INVALID", {
                "title": "t", "summary": "s",
                "resource_refs": {}, "data_time": "2026-06-18",
            })

    def test_build_message_missing_required(self) -> None:
        """测试缺少必填字段。"""
        with pytest.raises(MessageBuilderError, match="缺少必填字段"):
            build_message("SYSTEM_ALERT", {"title": "t"})

    def test_dto_validate_message_type(self) -> None:
        """测试 DTO message_type 校验。"""
        dto = NotificationMessageDTO(
            message_type="INVALID",
            template_key="test",
            template_version="1.0.0",
            title="t",
            summary="s",
            resource_refs={},
            data_time="2026-06-18",
        )
        with pytest.raises(ValueError, match="message_type 必须为"):
            dto.validate_message_type()


# ==================== 飞书卡片构建器测试 ====================


class TestFeishuCardBuilder:
    """飞书卡片构建器测试。"""

    def _make_dto(self, message_type: str = "MONITOR_EVENT") -> NotificationMessageDTO:
        """构建测试用 DTO。"""
        return NotificationMessageDTO(
            message_type=message_type,
            template_key="test",
            template_version="1.1.0",
            title="测试标题",
            summary="测试摘要",
            facts=[{"key": "k", "label": "标签", "value": "值"}],
            timeline=[{"time": "2026-06-18T10:18:00+08:00", "label": "事件"}],
            items=[{"name": "贵州茅台(600519)", "rank_value": 0.95}],
            actions=[{"label": "查看", "url": "/detail"}],
            resource_refs={"test": True},
            data_time="2026-06-18T10:28:00+08:00",
            disclaimer="免责声明",
        )

    def test_dto_to_card_structure(self) -> None:
        """测试卡片基本结构。"""
        dto = self._make_dto()
        card = dto_to_feishu_card(dto)
        assert card["config"]["wide_screen_mode"] is True
        assert card["header"]["title"]["content"] == "测试标题"
        assert card["header"]["template"] == "turquoise"  # MONITOR_EVENT → turquoise

    def test_card_header_color_mapping(self) -> None:
        """测试消息类型到颜色映射。"""
        color_map = {
            "MONITOR_EVENT": "turquoise",
            "MONITOR_MEMBER_EVENT": "turquoise",
            "SYSTEM_ALERT": "red",
            "CHANNEL_ALERT": "orange",
        }
        for msg_type, expected_color in color_map.items():
            dto = self._make_dto(msg_type)
            card = dto_to_feishu_card(dto)
            assert card["header"]["template"] == expected_color

    def test_card_elements_content(self) -> None:
        """测试卡片元素内容。"""
        dto = self._make_dto()
        card = dto_to_feishu_card(dto)
        elements = card["elements"]
        # 应包含：摘要、hr、事实、hr、时间线、hr、条目、hr、操作、hr、免责、note
        assert len(elements) > 5
        # 第一个元素是摘要 markdown
        assert elements[0]["tag"] == "markdown"
        assert elements[0]["content"] == "测试摘要"

    def test_card_empty_facts_timeline(self) -> None:
        """测试空 facts/timeline 时的卡片。"""
        dto = NotificationMessageDTO(
            message_type="SYSTEM_ALERT",
            template_key="test",
            template_version="1.1.0",
            title="标题",
            summary="摘要",
            resource_refs={},
            data_time="2026-06-18",
        )
        card = dto_to_feishu_card(dto)
        elements = card["elements"]
        # 空列表不应生成对应 section
        markdown_contents = [e.get("content", "") for e in elements if e.get("tag") == "markdown"]
        assert not any("关键事实" in c for c in markdown_contents)
        assert not any("时间线" in c for c in markdown_contents)

    def test_mask_webhook_url(self) -> None:
        """测试 Webhook URL 脱敏。"""
        url = "https://open.feishu.cn/open-apis/bot/v2/hook/abc123"
        masked = mask_webhook_url(url)
        assert masked == "https://open.feishu.cn/***"

    def test_mask_webhook_url_empty(self) -> None:
        """测试空 URL 脱敏。"""
        assert mask_webhook_url("") == ""


# ==================== 飞书 Webhook 适配器测试 ====================


class TestFeishuWebhookAdapter:
    """飞书 Webhook 适配器测试。"""

    def test_adapter_registered(self) -> None:
        """测试适配器已注册。"""
        adapters = list_supported_adapters()
        assert "feishu_webhook" in adapters
        assert "mock" in adapters

    def test_get_adapter(self) -> None:
        """测试获取适配器实例。"""
        adapter = get_adapter("feishu_webhook")
        assert adapter.adapter_type == "feishu_webhook"
        assert isinstance(adapter, FeishuWebhookAdapter)

    def test_sign_algorithm(self) -> None:
        """测试飞书签名算法。"""
        timestamp = "1597362936"
        secret = "test_secret"
        sign = _sign(timestamp, secret)

        # 验证签名可重复
        assert sign == _sign(timestamp, secret)

        # 验证签名格式（Base64）
        decoded = base64.b64decode(sign)
        assert len(decoded) == 32  # SHA256 输出 32 字节

        # 验证签名内容
        expected = hmac.new(
            f"{timestamp}\n{secret}".encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        assert sign == base64.b64encode(expected).decode("utf-8")

    def test_build_payload_with_sign(self) -> None:
        """测试带签名的 payload 构建。"""
        dto = NotificationMessageDTO(
            message_type="SYSTEM_ALERT",
            template_key="test",
            template_version="1.1.0",
            title="测试",
            summary="摘要",
            resource_refs={},
            data_time="2026-06-18",
        )
        payload = _build_webhook_payload(dto, "secret")
        assert payload["msg_type"] == "interactive"
        assert "timestamp" in payload
        assert "sign" in payload
        assert "card" in payload
        assert payload["card"]["header"]["title"]["content"] == "测试"

    def test_build_payload_without_sign(self) -> None:
        """测试无签名的 payload 构建。"""
        dto = NotificationMessageDTO(
            message_type="SYSTEM_ALERT",
            template_key="test",
            template_version="1.1.0",
            title="测试",
            summary="摘要",
            resource_refs={},
            data_time="2026-06-18",
        )
        payload = _build_webhook_payload(dto, None)
        assert "timestamp" not in payload
        assert "sign" not in payload
        assert payload["msg_type"] == "interactive"

    @pytest.mark.asyncio
    async def test_send_missing_webhook_url(self) -> None:
        """测试缺少 webhook_url 时的错误处理。"""
        adapter = FeishuWebhookAdapter()
        dto = NotificationMessageDTO(
            message_type="SYSTEM_ALERT",
            template_key="test",
            template_version="1.1.0",
            title="测试",
            summary="摘要",
            resource_refs={},
            data_time="2026-06-18",
        )
        result = await adapter.send(dto, {})
        assert result.success is False
        assert result.error_code == "CONFIG_MISSING"

    @pytest.mark.asyncio
    async def test_send_network_error(self) -> None:
        """测试网络错误处理。"""
        adapter = FeishuWebhookAdapter()
        dto = NotificationMessageDTO(
            message_type="SYSTEM_ALERT",
            template_key="test",
            template_version="1.1.0",
            title="测试",
            summary="摘要",
            resource_refs={},
            data_time="2026-06-18",
        )
        # 使用不可达的 URL 触发网络错误
        result = await adapter.send(dto, {
            "webhook_url": "http://127.0.0.1:19999/hook",  # 不可达端口
            "sign_secret": "secret",
        })
        assert result.success is False
        assert result.error_code in ("NETWORK_ERROR", "NETWORK_TIMEOUT", "RETRYABLE")


# ==================== 投递 Worker 测试 ====================


class TestDeliveryWorker:
    """投递 Worker 测试。"""

    def test_is_quiet_hours_overnight(self) -> None:
        """测试跨天静默时段（22-8）。"""
        # 22:30 在静默时段
        assert _is_quiet_hours(datetime(2026, 6, 18, 22, 30)) is True
        # 03:00 在静默时段
        assert _is_quiet_hours(datetime(2026, 6, 18, 3, 0)) is True
        # 10:00 不在静默时段
        assert _is_quiet_hours(datetime(2026, 6, 18, 10, 0)) is False
        # 08:00 不在静默时段（边界）
        assert _is_quiet_hours(datetime(2026, 6, 18, 8, 0)) is False
        # 21:59 不在静默时段
        assert _is_quiet_hours(datetime(2026, 6, 18, 21, 59)) is False

    def test_is_quiet_hours_same_day(self) -> None:
        """测试同天静默时段（如 12-14）。"""
        assert _is_quiet_hours(datetime(2026, 6, 18, 13, 0), 12, 14) is True
        assert _is_quiet_hours(datetime(2026, 6, 18, 11, 0), 12, 14) is False
        assert _is_quiet_hours(datetime(2026, 6, 18, 15, 0), 12, 14) is False

    @pytest.mark.asyncio
    async def test_process_empty_outbox(self) -> None:
        """测试空 outbox 处理。"""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        count = await process_notification_outbox(mock_db)
        assert count == 0

    @pytest.mark.asyncio
    async def test_get_pending_count(self) -> None:
        """测试获取 pending 事件数。"""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = 5
        mock_db.execute = AsyncMock(return_value=mock_result)

        count = await get_pending_notification_count(mock_db)
        assert count == 5


# ==================== Mock 适配器测试 ====================


class TestMockChannelAdapter:
    """Mock 渠道适配器测试（验证 ClassVar 修复后仍正常工作）。"""

    @pytest.mark.asyncio
    async def test_mock_send(self) -> None:
        """测试 Mock 适配器投递。"""
        adapter = MockChannelAdapter()
        assert adapter.adapter_type == "mock"

        dto = NotificationMessageDTO(
            message_type="SYSTEM_ALERT",
            template_key="test",
            template_version="1.1.0",
            title="测试",
            summary="摘要",
            resource_refs={},
            data_time="2026-06-18",
        )
        result = await adapter.send(dto, {})
        assert result.success is True
        assert result.provider_response["mock"] is True

    @pytest.mark.asyncio
    async def test_mock_verify(self) -> None:
        """测试 Mock 适配器验证。"""
        adapter = MockChannelAdapter()
        verified = await adapter.verify({})
        assert verified is True

    def test_get_mock_adapter(self) -> None:
        """测试通过注册表获取 Mock 适配器。"""
        adapter = get_adapter("mock")
        assert adapter.adapter_type == "mock"


# ==================== API 端点测试 ====================


class TestNotificationAPI:
    """通知 API 端点测试。"""

    def test_preview_endpoint(self) -> None:
        """测试消息预览端点。"""
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        response = client.post("/notification-previews", json={
            "message_type": "MONITOR_EVENT",
            "context": {
                "title": "监控事件｜贵州茅台",
                "summary": "3/3 个策略在 15 分钟内完成确认",
                "facts": [{"key": "price", "label": "当前价格", "value": 1502.3}],
                "timeline": [{"time": "2026-06-18T10:18:00+08:00", "label": "Node 碰触"}],
                "resource_refs": {"instrument_id": "600519.SH"},
                "data_time": "2026-06-18T10:28:00+08:00",
            },
        })
        assert response.status_code == 200
        data = response.json()
        assert "dto" in data
        assert "in_app" in data
        assert "feishu_card" in data
        assert data["dto"]["message_type"] == "MONITOR_EVENT"
        assert data["feishu_card"]["header"]["template"] == "turquoise"
        assert data["feishu_card"]["header"]["title"]["content"] == "监控事件｜贵州茅台"

    def test_preview_invalid_message_type(self) -> None:
        """测试预览端点无效消息类型。"""
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        response = client.post("/notification-previews", json={
            "message_type": "INVALID",
            "context": {
                "title": "t", "summary": "s",
                "resource_refs": {}, "data_time": "2026-06-18",
            },
        })
        assert response.status_code == 400

    def test_preview_missing_required(self) -> None:
        """测试预览端点缺少必填字段。"""
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        response = client.post("/notification-previews", json={
            "message_type": "SYSTEM_ALERT",
            "context": {"title": "t"},  # 缺少 summary 等
        })
        assert response.status_code == 400

    def test_messages_endpoint_requires_auth(self) -> None:
        """测试消息列表端点需要认证。"""
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        response = client.get("/messages")
        assert response.status_code == 401  # 缺少 X-User-Id

    def test_channels_endpoint_requires_auth(self) -> None:
        """测试渠道列表端点需要认证。"""
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        response = client.get("/notification-channels")
        assert response.status_code == 401  # 缺少 X-User-Id


# ==================== Outbox 投递链修复测试 ====================


class TestOutboxDeliveryPipeline:
    """Outbox 投递链修复测试（Task 1-3）。"""

    def test_is_quiet_hours_shanghai_trading_time(self) -> None:
        """北京时间 14:49 不在默认静默时段内。"""
        from zoneinfo import ZoneInfo
        from app.services.delivery_worker import _is_quiet_hours

        # 14:49 CST 是盘中交易时间，不应静默
        cst = ZoneInfo("Asia/Shanghai")
        t = datetime(2026, 6, 24, 14, 49, tzinfo=cst)
        assert _is_quiet_hours(t) is False

        # 22:30 CST 在静默时段
        t2 = datetime(2026, 6, 24, 22, 30, tzinfo=cst)
        assert _is_quiet_hours(t2) is True

    def test_next_attempt_at_computed_from_shanghai_quiet_end(self) -> None:
        """22:30 CST 触发时，next_attempt_at 应为次日 08:00 CST。"""
        from zoneinfo import ZoneInfo
        from app.services.delivery_worker import _compute_next_attempt_at

        cst = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 6, 24, 22, 30, tzinfo=cst)
        next_at = _compute_next_attempt_at(now)

        assert next_at is not None
        assert next_at.tzinfo is not None
        assert next_at.hour == 8
        assert next_at.minute == 0
        # 跨天
        assert next_at.date() == datetime(2026, 6, 25, 8, 0, tzinfo=cst).date()

    @pytest.mark.asyncio
    async def test_process_notification_outbox_deferred_in_quiet_hours(self) -> None:
        """静默时段处理通知 Outbox 应标记为 deferred 并设置 next_attempt_at。"""
        from zoneinfo import ZoneInfo
        from app.models.outbox import Outbox
        from app.services.delivery_worker import process_notification_outbox

        cst = ZoneInfo("Asia/Shanghai")
        outbox = Outbox(
            id=uuid4(),
            aggregate_type="notification_message",
            aggregate_id=uuid4(),
            event_type="notification.message.created",
            payload={"message_id": str(uuid4()), "user_id": str(uuid4())},
            headers={},
            status="pending",
            retry_count=0,
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [outbox]
        mock_db.execute = AsyncMock(return_value=mock_result)

        # 22:30 CST 显式传入静默
        quiet_now = datetime(2026, 6, 24, 22, 30, tzinfo=cst)
        count = await process_notification_outbox(
            mock_db, batch_size=10, quiet_hours=True, now=quiet_now,
        )

        # 静默时不算“成功处理”，返回 0
        assert count == 0
        assert outbox.status == "deferred"
        assert outbox.next_attempt_at is not None
        assert outbox.next_attempt_at.hour == 8

    @pytest.mark.asyncio
    async def test_process_notification_outbox_respects_next_attempt_at(self) -> None:
        """deferred 记录未到 next_attempt_at 时不应被处理。"""
        from zoneinfo import ZoneInfo
        from app.models.outbox import Outbox
        from app.services.delivery_worker import process_notification_outbox

        cst = ZoneInfo("Asia/Shanghai")
        future = datetime(2026, 6, 25, 7, 0, tzinfo=cst)
        outbox = Outbox(
            id=uuid4(),
            aggregate_type="notification_message",
            aggregate_id=uuid4(),
            event_type="notification.message.created",
            payload={"message_id": str(uuid4()), "user_id": str(uuid4())},
            headers={},
            status="deferred",
            retry_count=0,
            next_attempt_at=future,
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [outbox]
        mock_db.execute = AsyncMock(return_value=mock_result)

        # 当前时间早于 next_attempt_at，且非静默
        now = datetime(2026, 6, 25, 6, 0, tzinfo=cst)
        count = await process_notification_outbox(
            mock_db, batch_size=10, quiet_hours=False, now=now,
        )

        # 未到时间，不处理
        assert count == 0
        assert outbox.status == "deferred"

    @pytest.mark.asyncio
    async def test_relay_outbox_excludes_notification_events(self) -> None:
        """通用 Outbox Relay 应排除 notification.message.created。"""
        from app.services.outbox_relay import relay_outbox

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("app.services.outbox_relay.get_redis") as mock_redis:
            mock_redis.return_value = AsyncMock()
            await relay_outbox(mock_db, batch_size=10)

        # 验证查询条件中排除了通知事件类型
        call_args = mock_db.execute.call_args
        stmt = call_args[0][0]
        # 将 compiled SQL 转为小写检查 WHERE 条件
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        assert "event_type" in compiled
        assert "notification.message.created" in compiled
        assert "not in" in compiled or "!=" in compiled


    @pytest.mark.asyncio
    async def test_list_user_messages_includes_deliveries(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """GET /messages 应返回关联的 deliveries 数组。"""
        from app.models.notification import NotificationChannel, NotificationMessage, MessageDelivery
        from app.schemas.notification import NotificationMessageDTO
        from app.services.notification_service import list_user_messages

        dto = NotificationMessageDTO(
            message_type="MONITOR_EVENT",
            template_key="monitor_event",
            template_version="1.1.0",
            title="测试",
            summary="摘要",
            resource_refs={
                "instrument_id": str(test_instrument.id),
                "instruments": [{"instrument_id": str(test_instrument.id)}],
            },
            data_time="2026-06-24T10:00:00+08:00",
        )
        message = NotificationMessage(
            user_id=test_user.id,
            message_type=dto.message_type,
            template_key=dto.template_key,
            template_version=dto.template_version,
            source_type="strategy_event",
            source_id=None,
            body=dto.model_dump(),
            idempotency_key="test:msg:1",
        )
        channel = NotificationChannel(
            user_id=test_user.id,
            adapter_type="feishu_webhook",
            display_name="测试Webhook",
            target_config={"webhook_url": "http://example.com/hook"},
            status="active",
        )
        db_session.add_all([message, channel])
        await db_session.flush()

        delivery = MessageDelivery(
            notification_message_id=message.id,
            channel_id=channel.id,
            status="failed",
            attempt_count=2,
            last_error_code="NETWORK_ERROR",
            idempotency_key="test:delivery:1",
        )
        db_session.add(delivery)
        await db_session.flush()

        messages = await list_user_messages(db_session, test_user.id, limit=10)
        found = next((m for m in messages if m.id == message.id), None)
        assert found is not None
        assert len(found.deliveries) == 1
        delivery = found.deliveries[0]
        assert delivery.status == "failed"
        assert delivery.channel.adapter_type == "feishu_webhook"
        assert delivery.channel.display_name == "测试Webhook"
        assert delivery.last_error_code == "NETWORK_ERROR"

    @pytest.mark.asyncio
    async def test_list_user_messages_patches_monitor_member_event(
        self, db_session, test_user, test_instrument, test_selector_strategy,
    ) -> None:
        """历史 MONITOR_MEMBER_EVENT 消息应自动补齐股票信息。"""
        from app.models.notification import NotificationMessage
        from app.models.strategy_event import StrategyEvent
        from app.schemas.notification import NotificationMessageDTO
        from app.services.notification_service import list_user_messages

        version = test_selector_strategy["version"]
        event = StrategyEvent(
            event_key="test:member:event:1",
            strategy_version_id=version.id,
            instrument_id=test_instrument.id,
            event_type="bb_upper_touch",
            event_time=datetime.now(UTC),
            schema_version=1,
            payload={"price": 100.0},
        )
        db_session.add(event)
        await db_session.flush()

        dto = NotificationMessageDTO(
            message_type="MONITOR_MEMBER_EVENT",
            template_key="monitor_member_event",
            template_version="1.0.0",
            title="测试",
            summary="摘要",
            resource_refs={},
            data_time="2026-06-24T10:00:00+08:00",
        )
        message = NotificationMessage(
            user_id=test_user.id,
            message_type=dto.message_type,
            template_key=dto.template_key,
            template_version=dto.template_version,
            source_type="strategy_event",
            source_id=event.id,
            body=dto.model_dump(),
            idempotency_key="test:msg:member:1",
        )
        db_session.add(message)
        await db_session.flush()

        messages = await list_user_messages(db_session, test_user.id, limit=10)
        found = next((m for m in messages if m.id == message.id), None)
        assert found is not None
        resource_refs = found.body.get("resource_refs", {})
        instruments = resource_refs.get("instruments", [])
        assert len(instruments) == 1
        assert instruments[0]["instrument_id"] == str(test_instrument.id)
        assert instruments[0]["symbol"] == test_instrument.symbol
        assert instruments[0]["name"] == test_instrument.name


# ==================== 消息 DTO 结构化字段测试（Task 12） ====================


class TestMessageStructuredFields:
    """消息响应新增 strategy_key / strategy_name / instrument_count / primary_instrument / event_summary 字段测试。"""

    def test_notification_message_response_has_structured_fields(self) -> None:
        """NotificationMessageResponse 应包含新增结构化字段。"""
        response = NotificationMessageResponse(
            id=uuid4(),
            user_id=uuid4(),
            message_type="MONITOR_EVENT",
            template_key="monitor_event",
            template_version="1.1.0",
            source_type="strategy_event",
            source_id=uuid4(),
            body={
                "strategy_key": "watchlist_monitor",
                "strategy_name": "BB+节点监控",
                "instrument_count": 1,
                "primary_instrument": {"instrument_id": "i1", "symbol": "600519", "name": "贵州茅台"},
                "event_summary": "布林上轨穿越 · 边界 24.80",
            },
            created_at=datetime.now(UTC),
        )
        assert response.strategy_key == "watchlist_monitor"
        assert response.strategy_name == "BB+节点监控"
        assert response.instrument_count == 1
        assert response.primary_instrument["symbol"] == "600519"
        assert response.event_summary == "布林上轨穿越 · 边界 24.80"

    def test_dto_has_structured_fields(self) -> None:
        """NotificationMessageDTO 应允许设置新增结构化字段。"""
        dto = NotificationMessageDTO(
            message_type="MONITOR_EVENT",
            template_key="monitor_event",
            template_version="1.1.0",
            title="测试",
            summary="摘要",
            resource_refs={"instruments": [{"instrument_id": "i1"}]},
            data_time="2026-06-24T10:00:00+08:00",
            strategy_key="watchlist_monitor",
            strategy_name="BB+节点监控",
            instrument_count=1,
            primary_instrument={"instrument_id": "i1", "symbol": "600519", "name": "贵州茅台"},
            event_summary="布林上轨穿越",
        )
        assert dto.strategy_key == "watchlist_monitor"
        assert dto.strategy_name == "BB+节点监控"
        assert dto.instrument_count == 1
        assert dto.primary_instrument["symbol"] == "600519"
        assert dto.event_summary == "布林上轨穿越"

    def test_build_merged_card_dto_sets_structured_fields(self) -> None:
        """monitor_batch_service._build_merged_card_dto 应填充新增字段。"""
        from app.services.monitor_batch_service import MonitorBatchService

        service = MonitorBatchService()
        inst_id_1 = uuid4()
        inst_id_2 = uuid4()

        class _FakeEvent:
            def __init__(self, event_type, instrument_id, payload, event_time, snapshot=None):
                self.id = uuid4()
                self.event_type = event_type
                self.instrument_id = instrument_id
                self.payload = payload
                self.event_time = event_time
                self.snapshot = snapshot or {}

        events = [
            _FakeEvent(
                "bb_upper_touch", inst_id_1,
                {"price": 25.50, "boundary": 24.80, "dev_pct": 2.82,
                 "bb_upper": 24.80, "bb_mid": 22.00, "bb_lower": 19.20},
                datetime(2026, 6, 24, 10, 30, 0, tzinfo=UTC),
            ),
            _FakeEvent(
                "bb_lower_touch", inst_id_2,
                {"price": 15.30, "boundary": 15.00, "dev_pct": 2.00},
                datetime(2026, 6, 24, 10, 30, 0, tzinfo=UTC),
            ),
        ]
        info_cache = {
            inst_id_1: ("000001", "平安银行"),
            inst_id_2: ("600519", "贵州茅台"),
        }
        dto = service._build_merged_card_dto(events, 5, info_cache)
        assert dto.strategy_key == "watchlist_monitor"
        assert dto.strategy_name is not None
        assert dto.instrument_count == 2
        assert dto.primary_instrument is not None
        assert dto.primary_instrument["symbol"] in ("000001", "600519")
        assert dto.event_summary is not None
        assert "触发" in dto.event_summary

    @pytest.mark.asyncio
    async def test_create_notification_from_event_sets_structured_fields(
        self, db_session, test_user, test_instrument, test_selector_strategy,
    ) -> None:
        """event_recipient_service.create_notification_from_event 应填充新增字段。"""
        from app.models.strategy_event import StrategyEvent
        from app.services.event_recipient_service import create_notification_from_event
        from app.services.notification_service import list_user_messages

        version = test_selector_strategy["version"]
        event = StrategyEvent(
            event_key="test:structured:event:1",
            strategy_version_id=version.id,
            instrument_id=test_instrument.id,
            event_type="bb_upper_touch",
            event_time=datetime.now(UTC),
            schema_version=1,
            payload={"price": 100.0, "boundary": 120.5, "dev_pct": 1.2},
        )
        db_session.add(event)
        await db_session.flush()

        # 创建有效的自选股记录（满足 FK 约束）
        from app.models.watchlist import UserWatchlistItem
        watchlist_item = UserWatchlistItem(
            user_id=test_user.id,
            instrument_id=test_instrument.id,
            source="test",
            active=True,
        )
        db_session.add(watchlist_item)
        await db_session.flush()

        # 创建接收人记录
        from app.models.event_recipient import StrategyEventRecipient
        recipient = StrategyEventRecipient(
            event_id=event.id,
            user_id=test_user.id,
            watchlist_item_id=watchlist_item.id,
        )
        db_session.add(recipient)
        await db_session.flush()

        result = await create_notification_from_event(db_session, event.id, test_user.id)
        assert result is not None

        messages = await list_user_messages(db_session, test_user.id, limit=10)
        found = next((m for m in messages if m.source_id == event.id), None)
        assert found is not None
        body = found.body
        assert body.get("strategy_key") is not None
        assert body.get("strategy_name") is not None
        assert body.get("instrument_count") == 1
        assert body.get("primary_instrument", {}).get("symbol") == test_instrument.symbol
        assert body.get("event_summary") is not None

    @pytest.mark.asyncio
    async def test_messages_endpoint_returns_structured_fields(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """GET /messages 响应应包含新增结构化字段。"""
        from collections.abc import AsyncGenerator

        from httpx import ASGITransport, AsyncClient
        from app.main import app
        from app.models.notification import NotificationMessage
        from app.schemas.notification import NotificationMessageDTO
        from app.core.deps import get_db as deps_get_db
        from app.db import get_db as db_get_db

        dto = NotificationMessageDTO(
            message_type="MONITOR_EVENT",
            template_key="monitor_event",
            template_version="1.1.0",
            title="测试",
            summary="摘要",
            resource_refs={"instruments": [{"instrument_id": str(test_instrument.id)}]},
            data_time="2026-06-24T10:00:00+08:00",
            strategy_key="watchlist_monitor",
            strategy_name="BB+节点监控",
            instrument_count=1,
            primary_instrument={"instrument_id": str(test_instrument.id), "symbol": test_instrument.symbol, "name": test_instrument.name},
            event_summary="布林上轨穿越",
        )
        message = NotificationMessage(
            user_id=test_user.id,
            message_type=dto.message_type,
            template_key=dto.template_key,
            template_version=dto.template_version,
            source_type="strategy_event",
            source_id=None,
            body=dto.model_dump(),
            idempotency_key="test:structured:msg:1",
        )
        db_session.add(message)
        await db_session.flush()

        async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
            yield db_session

        app.dependency_overrides[deps_get_db] = get_test_db
        app.dependency_overrides[db_get_db] = get_test_db
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/messages", headers={"X-User-Id": str(test_user.id)})
            assert response.status_code == 200
            data = response.json()
            items = data["items"]
            found = next((m for m in items if m["id"] == str(message.id)), None)
            assert found is not None
            assert found["strategy_key"] == "watchlist_monitor"
            assert found["strategy_name"] == "BB+节点监控"
            assert found["instrument_count"] == 1
            assert found["primary_instrument"]["symbol"] == test_instrument.symbol
            assert found["event_summary"] == "布林上轨穿越"
        finally:
            app.dependency_overrides.clear()


# ==================== 失败投递管理接口测试（Task 14） ====================


class TestMessageDeliveryAdminService:
    """message-deliveries 服务层：查询与重试。"""

    @pytest.mark.asyncio
    async def test_list_message_deliveries_returns_records(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """list_message_deliveries 应返回 message_deliveries 记录。"""
        from app.models.notification import NotificationChannel, NotificationMessage, MessageDelivery
        from app.schemas.notification import NotificationMessageDTO
        from app.services.notification_service import list_message_deliveries

        dto = NotificationMessageDTO(
            message_type="MONITOR_EVENT",
            template_key="monitor_event",
            template_version="1.1.0",
            title="测试",
            summary="摘要",
            resource_refs={},
            data_time="2026-06-24T10:00:00+08:00",
        )
        message = NotificationMessage(
            user_id=test_user.id,
            message_type=dto.message_type,
            template_key=dto.template_key,
            template_version=dto.template_version,
            source_type="strategy_event",
            source_id=None,
            body=dto.model_dump(),
            idempotency_key="test:delivery:msg:1",
        )
        channel = NotificationChannel(
            user_id=test_user.id,
            adapter_type="feishu_webhook",
            display_name="测试渠道",
            target_config={"webhook_url": "http://example.com/hook"},
            status="active",
        )
        db_session.add_all([message, channel])
        await db_session.flush()

        delivery = MessageDelivery(
            notification_message_id=message.id,
            channel_id=channel.id,
            status="failed",
            attempt_count=2,
            last_error_code="NETWORK_ERROR",
            idempotency_key="test:delivery:1",
        )
        db_session.add(delivery)
        await db_session.flush()

        rows = await list_message_deliveries(db_session, status="failed", limit=10)
        assert len(rows) >= 1
        found = next((d for d in rows if d.id == delivery.id), None)
        assert found is not None
        assert found.status == "failed"
        assert found.channel_id == channel.id

    @pytest.mark.asyncio
    async def test_retry_delivery_updates_existing_record(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """retry_delivery 应复用现有记录并重新调用 adapter。"""
        from app.models.notification import NotificationChannel, NotificationMessage, MessageDelivery
        from app.schemas.notification import NotificationMessageDTO
        from app.services.notification_service import retry_delivery
        from app.services.channel_adapter import ChannelAdapter

        dto = NotificationMessageDTO(
            message_type="MONITOR_EVENT",
            template_key="monitor_event",
            template_version="1.1.0",
            title="测试",
            summary="摘要",
            resource_refs={},
            data_time="2026-06-24T10:00:00+08:00",
        )
        message = NotificationMessage(
            user_id=test_user.id,
            message_type=dto.message_type,
            template_key=dto.template_key,
            template_version=dto.template_version,
            source_type="strategy_event",
            source_id=None,
            body=dto.model_dump(),
            idempotency_key="test:retry:msg:1",
        )
        channel = NotificationChannel(
            user_id=test_user.id,
            adapter_type="mock",
            display_name="Mock渠道",
            target_config={},
            status="active",
        )
        db_session.add_all([message, channel])
        await db_session.flush()

        delivery = MessageDelivery(
            notification_message_id=message.id,
            channel_id=channel.id,
            status="failed",
            attempt_count=1,
            last_error_code="NETWORK_ERROR",
            idempotency_key="test:retry:1",
        )
        db_session.add(delivery)
        await db_session.flush()

        class _FakeAdapter(ChannelAdapter):
            adapter_type = "mock"

            async def send(self, message_dto, target_config):
                from app.schemas.notification import DeliveryResult
                return DeliveryResult(success=True, provider_response={"retried": True})

            async def verify(self, target_config):
                return True

        with patch("app.services.notification_service.get_adapter") as mock_get_adapter:
            mock_get_adapter.return_value = _FakeAdapter()
            retried = await retry_delivery(db_session, delivery.id)

        assert retried.id == delivery.id
        assert retried.attempt_count >= 2
        assert retried.status == "success"
        assert retried.last_error_code is None or retried.last_error_code == ""


# ==================== 图片投递链路测试 ====================


class TestImageDeliveryPipeline:
    """图片投递链路测试：Outbox -> delivery_worker -> deliver_image_message -> adapter。"""

    @pytest.mark.asyncio
    async def test_deliver_image_message_creates_image_delivery(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """deliver_image_message 应创建 delivery_type=image 的投递记录。"""
        from app.models.notification import NotificationChannel, NotificationMessage, MessageDelivery
        from app.schemas.notification import NotificationMessageDTO
        from app.services.notification_service import deliver_image_message

        dto = NotificationMessageDTO(
            message_type="MONITOR_EVENT",
            template_key="monitor_event",
            template_version="1.1.0",
            title="测试",
            summary="摘要",
            resource_refs={"instrument_id": str(test_instrument.id)},
            data_time="2026-06-24T10:00:00+08:00",
        )
        message = NotificationMessage(
            user_id=test_user.id,
            message_type=dto.message_type,
            template_key=dto.template_key,
            template_version=dto.template_version,
            source_type="strategy_event",
            source_id=None,
            body=dto.model_dump(),
            idempotency_key="test:image:msg:1",
        )
        channel = NotificationChannel(
            user_id=test_user.id,
            adapter_type="mock",
            display_name="Mock渠道",
            target_config={},
            status="active",
        )
        db_session.add_all([message, channel])
        await db_session.flush()

        image_bytes = b"fake-png-bytes"
        delivery = await deliver_image_message(
            db_session, message.id, channel.id, image_bytes,
        )

        assert delivery.delivery_type == "image"
        assert delivery.status == "success"
        assert delivery.notification_message_id == message.id
        assert delivery.channel_id == channel.id

    @pytest.mark.asyncio
    async def test_delivery_worker_processes_image_outbox(
        self, db_session, test_user, test_instrument,
    ) -> None:
        """delivery_worker 应能处理 delivery_type=image 的 Outbox 事件。"""
        from app.models.notification import NotificationChannel, NotificationMessage, MessageDelivery
        from app.models.outbox import Outbox
        from app.schemas.notification import NotificationMessageDTO
        from app.services.delivery_worker import _process_single_outbox

        dto = NotificationMessageDTO(
            message_type="MONITOR_EVENT",
            template_key="monitor_event",
            template_version="1.1.0",
            title="测试",
            summary="摘要",
            resource_refs={"instrument_id": str(test_instrument.id)},
            data_time="2026-06-24T10:00:00+08:00",
        )
        message = NotificationMessage(
            user_id=test_user.id,
            message_type=dto.message_type,
            template_key=dto.template_key,
            template_version=dto.template_version,
            source_type="strategy_event",
            source_id=None,
            body=dto.model_dump(),
            idempotency_key="test:image:msg:2",
        )
        channel = NotificationChannel(
            user_id=test_user.id,
            adapter_type="mock",
            display_name="Mock渠道",
            target_config={},
            status="active",
        )
        db_session.add_all([message, channel])
        await db_session.flush()

        outbox = Outbox(
            id=uuid4(),
            aggregate_type="notification_message",
            aggregate_id=message.id,
            event_type="notification.message.created",
            payload={
                "message_id": str(message.id),
                "user_id": str(test_user.id),
                "delivery_type": "image",
                "image_bytes_base64": base64.b64encode(b"fake-png-bytes").decode("utf-8"),
            },
            headers={},
            status="pending",
            retry_count=0,
        )

        success = await _process_single_outbox(db_session, outbox)
        assert success is True

        # 验证创建了 image 投递记录
        stmt = select(MessageDelivery).where(
            MessageDelivery.notification_message_id == message.id,
            MessageDelivery.channel_id == channel.id,
        )
        result = await db_session.execute(stmt)
        delivery = result.scalar_one_or_none()
        assert delivery is not None
        assert delivery.delivery_type == "image"
        assert delivery.status == "success"


class TestCaptureToken:
    """截图模式短期 token 测试。"""

    def test_create_capture_token_has_capture_type(self) -> None:
        """create_capture_token 应生成 type=capture 的 JWT。"""
        from app.core.security import create_capture_token, decode_token

        token = create_capture_token(subject="test-user", event_id="evt-1")
        payload = decode_token(token)
        assert payload["type"] == "capture"
        assert payload["sub"] == "test-user"
        assert payload["event_id"] == "evt-1"

    def test_get_current_user_accepts_capture_token(self) -> None:
        """get_current_user 应接受 capture token 并返回用户。"""
        from datetime import timedelta
        from uuid import uuid4

        from fastapi import HTTPException

        from app.core.security import create_capture_token
        from app.core.deps import get_current_user

        # 该测试验证 token 类型被接受；使用 AsyncMock 模拟 DB，使其返回用户不存在
        # 预期抛出 401（用户不存在），错误信息中不应包含 "token 类型错误"
        fake_user_id = uuid4()
        token = create_capture_token(
            subject=str(fake_user_id),
            event_id="evt-1",
            expires_delta=timedelta(minutes=5),
        )

        class FakeCreds:
            credentials = token

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        # 使用同步调用包装（实际 get_current_user 是 async，此处仅做类型校验）
        import asyncio
        async def _call():
            return await get_current_user(FakeCreds(), mock_db)  # type: ignore[arg-type]

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(_call())
        assert exc_info.value.status_code == 401
        assert "token 类型错误" not in str(exc_info.value.detail)


class TestLatestEventEndpoint:
    """真实事件图片测试端点测试。"""

    def test_test_latest_event_requires_admin(self) -> None:
        """test-latest-event 端点需要 admin 角色。"""
        from httpx import ASGITransport, AsyncClient
        from app.main import app
        from uuid import uuid4

        async def _call():
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                # 无 token 访问应 401
                return await client.post(f"/notification-channels/{uuid4()}/test-latest-event")

        import asyncio
        response = asyncio.run(_call())
        assert response.status_code == 401


if __name__ == "__main__":
    # 自测入口：直接运行验证
    pytest.main([__file__, "-v", "--tb=short"])
