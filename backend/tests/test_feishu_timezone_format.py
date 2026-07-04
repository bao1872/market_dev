"""飞书消息时区格式化测试。

覆盖：
- format_shanghai_datetime 将 UTC/naive datetime 正确转为 Asia/Shanghai 可读字符串
- build_monitor_event_text 对 UTC ISO 事件时间显示上海时区触发时间
- build_system_alert / build_channel_alert 默认 data_time 使用上海时区

测试策略：
- 纯单元测试，不依赖数据库
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.core.time import format_shanghai_datetime, now_shanghai
from app.services.message_builder import (
    build_channel_alert,
    build_monitor_event_text,
    build_system_alert,
)


@pytest.mark.parametrize(
    "input_dt, expected",
    [
        # UTC 06:48 = 上海 14:48
        (
            datetime(2026, 6, 18, 6, 48, 0, tzinfo=UTC),
            "2026-06-18 14:48:00 CST",
        ),
        # 无 tzinfo 的 naive datetime 视为 UTC
        (
            datetime(2026, 6, 18, 6, 48, 0),
            "2026-06-18 14:48:00 CST",
        ),
    ],
)
def test_format_shanghai_datetime_converts_to_shanghai(input_dt, expected):
    """UTC/naive datetime 应格式化为上海时区字符串。"""
    assert format_shanghai_datetime(input_dt) == expected


def test_format_shanghai_datetime_default_uses_now_shanghai():
    """value 为 None 时使用 now_shanghai() 并输出 CST 格式。"""
    fixed = datetime(2026, 6, 18, 14, 48, 0, tzinfo=UTC)
    with patch("app.core.time.now_shanghai", return_value=fixed.astimezone(now_shanghai().tzinfo)):
        result = format_shanghai_datetime()
    assert result.endswith(" CST")
    assert "2026-06-18" in result


def test_build_monitor_event_text_uses_shanghai_time():
    """event_time 为 UTC +00:00 时，文本中触发时间应显示上海时区 HH:MM。"""
    dto = build_monitor_event_text(
        stock_name="贵州茅台",
        symbol="600519",
        event_type="bb_upper_touch",
        event_time="2026-06-18T06:48:00+00:00",
        current_price=1500.0,
        bb_upper=1600.0,
        bb_mid=1500.0,
        bb_lower=1400.0,
        upper_node=1580.0,
        lower_node=1420.0,
        poc_price=1510.0,
        position_0_1=0.55,
    )

    assert dto.text_content is not None
    # 触发时间应转换为上海 14:48
    assert "触发时间：14:48" in dto.text_content
    # 文本中不应出现 UTC/+0 时区标识
    assert "+00:00" not in dto.text_content
    # data_time 字段也应是上海时区格式化
    assert dto.data_time.endswith(" CST")
    assert "2026-06-18 14:48:00 CST" == dto.data_time


def test_build_system_alert_default_data_time_shanghai():
    """build_system_alert 默认 data_time 使用上海时区。"""
    with patch("app.services.message_builder.format_shanghai_datetime", return_value="2026-06-18 14:48:00 CST"):
        dto = build_system_alert(
            alert_type="DATA_STALE",
            message="日线数据已过期",
            resource_refs={"service": "bars_daily"},
        )

    assert dto.data_time == "2026-06-18 14:48:00 CST"


def test_build_channel_alert_default_data_time_shanghai():
    """build_channel_alert 默认 data_time 使用上海时区。"""
    with patch("app.services.message_builder.format_shanghai_datetime", return_value="2026-06-18 14:48:00 CST"):
        dto = build_channel_alert(
            channel_name="飞书",
            error_code="RATE_LIMIT",
            error_message="请求过于频繁",
            resource_refs={"channel_id": "ch_001"},
        )

    assert dto.data_time == "2026-06-18 14:48:00 CST"
