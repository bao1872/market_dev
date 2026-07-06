"""Quote 时区测试 - /quote 返回的 update_time 必须带 +08:00。

覆盖：
1. pytdx 返回 naive datetime -> update_time 带 +08:00。
2. pytdx 返回 +00:00 字符串 -> update_time 带 +08:00。
3. freshness_seconds 基于上海时间计算。

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_quote_timezone.py -q
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.api import bars as bars_api
from app.core.time import now_shanghai


@pytest.mark.asyncio
async def test_quote_update_time_naive_datetime(
    client,
    instrument_factory,
    monkeypatch,
):
    """pytdx 返回 naive datetime 时，/quote update_time 必须带 +08:00。"""
    instrument = await instrument_factory(symbol="600519", market="SH")
    now = now_shanghai()
    pytdx_quote = {
        "current_price": 10.5,
        "open": 10.1,
        "high": 10.8,
        "low": 9.9,
        "close": 10.5,
        "volume": 123456.0,
        "prev_close": 10.0,
        "change_pct": 5.0,
        "update_time": now.replace(tzinfo=None),
        "is_realtime": True,
    }

    monkeypatch.setattr(bars_api, "_is_quote_realtime_session", AsyncMock(return_value=True))
    monkeypatch.setattr(bars_api, "_fetch_pytdx_quote", AsyncMock(return_value=pytdx_quote))

    response = await client.get(f"/api/v1/instruments/{instrument.id}/quote")
    assert response.status_code == 200, response.text

    data = response.json()
    assert data["update_time"].endswith("+08:00")
    assert data["source"] == "pytdx"
    assert data["is_realtime"] is True
    assert data["freshness_seconds"] >= 0
    assert data["freshness_seconds"] < 5


@pytest.mark.asyncio
async def test_quote_update_time_utc_string(
    client,
    instrument_factory,
    monkeypatch,
):
    """pytdx 返回 +00:00 字符串时，/quote update_time 必须带 +08:00。"""
    instrument = await instrument_factory(symbol="600519", market="SH")
    pytdx_quote = {
        "current_price": 10.5,
        "open": 10.1,
        "high": 10.8,
        "low": 9.9,
        "close": 10.5,
        "volume": 123456.0,
        "prev_close": 10.0,
        "change_pct": 5.0,
        "update_time": "2026-07-06T14:18:00+00:00",
        "is_realtime": True,
    }

    monkeypatch.setattr(bars_api, "_is_quote_realtime_session", AsyncMock(return_value=True))
    monkeypatch.setattr(bars_api, "_fetch_pytdx_quote", AsyncMock(return_value=pytdx_quote))

    response = await client.get(f"/api/v1/instruments/{instrument.id}/quote")
    assert response.status_code == 200, response.text

    data = response.json()
    assert data["update_time"].endswith("+08:00")
    assert "+00:00" not in data["update_time"]
    assert data["source"] == "pytdx"
