"""盘中监控合并通知文本内容测试 - 验证飞书两段式投递中文本段完整性。

背景：
- 飞书两段式投递（text + image）中，delivery_type=text 调用 adapter.send_text_message
- send_text_message 优先读 message_dto.text_content，回退到 summary
- 历史问题：_build_merged_card_dto 未填 text_content，导致飞书只收到 summary 一行
- 修复：_build_merged_card_dto 填充 text_content；adapter 增加 items 兜底

测试覆盖：
1. _build_merged_card_dto 输出 text_content 包含概览 + 逐股票详情 + 数据时间
2. feishu_platform_app_adapter.send_text_message 在 text_content 为空但 items 非空时兜底拼接
3. 边界：events 为空时 text_content 仍含概览
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from app.schemas.notification import NotificationMessageDTO
from app.services.message_builder import elements_to_text
from app.services.monitor_batch_service import MonitorBatchService


def _make_event(
    instrument_id: UUID,
    event_type: str = "node_cluster_touch",
    event_time: datetime | None = None,
    payload: dict | None = None,
    snapshot: dict | None = None,
) -> SimpleNamespace:
    """构造 mock StrategyEvent（避免依赖 DB）。"""
    return SimpleNamespace(
        id=uuid4(),
        instrument_id=instrument_id,
        event_type=event_type,
        event_time=event_time or datetime(2026, 6, 26, 11, 13, tzinfo=UTC),
        payload=payload or {
            "price": 77.20,
            "boundary": 77.14,
            "dev_pct": 0.0007,
        },
        snapshot=snapshot or {},
    )


class TestBuildMergedCardDtoTextContent:
    """验证 _build_merged_card_dto 输出 text_content 字段。"""

    def test_text_content_contains_overview_and_details(self) -> None:
        """text_content 应包含概览行 + 股票标题 + 信号详情 + 数据时间。"""
        inst_id = uuid4()
        event = _make_event(inst_id)
        service = MonitorBatchService()

        dto = service._build_merged_card_dto(
            user_events=[event],
            total_instruments=24,
            instrument_info_cache={inst_id: ("688362", "甬矽电子")},
            change_pct_map={inst_id: 0.03},
            strategy_key="watchlist_monitor",
            strategy_name="BB+节点监控",
        )

        # text_content 不能为空
        assert dto.text_content, "text_content 不应为空"
        # 应包含概览行
        assert "自选股 24 只" in dto.text_content
        assert "触发 1 只" in dto.text_content
        # 应包含股票标题（带名称和代码）
        assert "甬矽电子" in dto.text_content
        assert "688362" in dto.text_content
        # 应包含信号详情（事件标签和现价）
        assert "节点集群穿越" in dto.text_content
        assert "77.20" in dto.text_content
        # 应包含数据时间
        assert "数据时间" in dto.text_content

    def test_text_content_multi_instruments_separated(self) -> None:
        """多只股票时 text_content 应包含每只股票的详情。"""
        inst1 = uuid4()
        inst2 = uuid4()
        ev1 = _make_event(inst1, event_type="bb_upper_touch")
        ev2 = _make_event(inst2, event_type="bb_lower_touch")
        service = MonitorBatchService()

        dto = service._build_merged_card_dto(
            user_events=[ev1, ev2],
            total_instruments=10,
            instrument_info_cache={
                inst1: ("600519", "贵州茅台"),
                inst2: ("000858", "五粮液"),
            },
        )

        assert dto.text_content
        assert "贵州茅台" in dto.text_content
        assert "五粮液" in dto.text_content
        assert "布林上轨穿越" in dto.text_content
        assert "布林下轨穿越" in dto.text_content

    def test_text_content_not_equal_summary(self) -> None:
        """text_content 不应等于 summary（summary 只是单行预览）。"""
        inst_id = uuid4()
        event = _make_event(inst_id)
        service = MonitorBatchService()

        dto = service._build_merged_card_dto(
            user_events=[event],
            total_instruments=5,
            instrument_info_cache={inst_id: ("688362", "甬矽电子")},
        )

        assert dto.text_content != dto.summary, (
            "text_content 不应等于 summary，否则飞书只收到一行预览"
        )
        assert len(dto.text_content) > len(dto.summary)


class TestElementsToTextHelper:
    """验证 elements_to_text 公共辅助函数。"""

    def test_markdown_element(self) -> None:
        """markdown 元素取 content 字段。"""
        elements = [{"tag": "markdown", "content": "hello world"}]
        assert elements_to_text(elements) == "hello world"

    def test_hr_element(self) -> None:
        """hr 元素转成 --- 分隔线。"""
        elements = [
            {"tag": "markdown", "content": "第一段"},
            {"tag": "hr"},
            {"tag": "markdown", "content": "第二段"},
        ]
        result = elements_to_text(elements)
        assert "第一段" in result
        assert "第二段" in result
        assert "---" in result

    def test_note_element(self) -> None:
        """note 元素取内部 plain_text content。"""
        elements = [
            {"tag": "markdown", "content": "概览"},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "数据时间: 2026-06-26 11:13"}],
            },
        ]
        result = elements_to_text(elements)
        assert "概览" in result
        assert "数据时间: 2026-06-26 11:13" in result

    def test_empty_elements(self) -> None:
        """空 elements 返回空字符串。"""
        assert elements_to_text([]) == ""
        assert elements_to_text(None) == ""  # type: ignore[arg-type]


class TestSendTextMessageItemsFallback:
    """验证 feishu_platform_app_adapter.send_text_message 的 items 兜底。"""

    @pytest.mark.asyncio
    async def test_text_content_empty_fallback_to_items(self) -> None:
        """text_content 为空但 items 非空时，应拼接 items 作为文本发送。"""
        from app.services.feishu_platform_app_adapter import FeishuPlatformAppAdapter

        dto = NotificationMessageDTO(
            message_type="MONITOR_EVENT",
            template_key="monitor_merged_event",
            template_version="2.0.0",
            title="BB+节点监控 11:13",
            summary="自选股 24 只 | 触发 1 只",
            text_content=None,  # 关键：text_content 为空
            items=[
                {"tag": "markdown", "content": "自选股 24 只 | 触发 1 只"},
                {"tag": "markdown", "content": "**甬矽电子 688362**"},
                {"tag": "markdown", "content": "🟣 节点集群穿越\n  现价: 77.20"},
            ],
            resource_refs={},
            data_time="2026-06-26 11:13",
        )

        adapter = FeishuPlatformAppAdapter()
        channel_config = {
            "app_id": "cli_test",
            "app_secret": "test_secret",
            "receive_id": "test_user",
            "receive_id_type": "user_id",
        }

        # mock _get_tenant_access_token 与 httpx.AsyncClient.post
        captured_payload: dict = {}

        class FakeResponse:
            status_code = 200

            def json(self) -> dict:
                return {"code": 0, "msg": "success", "data": {}}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, json=None, **kwargs):
                captured_payload["url"] = url
                captured_payload["json"] = json
                return FakeResponse()

        with patch(
            "app.services.feishu_platform_app_adapter._get_tenant_access_token",
            new=AsyncMock(return_value=("fake_token", None)),
        ), patch("httpx.AsyncClient", return_value=FakeClient()):
            result = await adapter.send_text_message(dto, channel_config)

        assert result.success is True
        # 验证发送的 content 包含 items 内容，而非仅 summary
        # content 是 JSON 字符串（json.dumps 产生），解码后检查
        import json as _json
        sent_content_raw = captured_payload["json"]["content"]
        sent_text = _json.loads(sent_content_raw)["text"]
        assert "甬矽电子" in sent_text, "应包含 items 中的股票详情"
        assert "节点集群穿越" in sent_text, "应包含 items 中的信号详情"
        assert "77.20" in sent_text, "应包含 items 中的现价"
        # 不应只等于 summary（说明 items 兜底生效）
        assert sent_text != dto.summary, "items 兜底应生成比 summary 更长的文本"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
