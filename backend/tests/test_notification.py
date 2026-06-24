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

from app.schemas.notification import (
    NotificationMessageDTO,
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


if __name__ == "__main__":
    # 自测入口：直接运行验证
    pytest.main([__file__, "-v", "--tb=short"])
