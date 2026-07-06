"""Quote 可信化测试 - /instruments/{id}/quote 必须暴露来源、实时性、新鲜度与降级状态。

测试目标：
1. pytdx 成功且处于交易时段 -> source="pytdx", is_realtime=true, degraded=false
2. pytdx 失败且处于交易时段 -> source="daily_fallback", is_realtime=false, degraded=true
3. 非交易时段 -> source="daily_fallback", is_realtime=false, degraded=false（不尝试 pytdx）
4. 无 DB fallback 数据 -> 404
5. Redis 缓存命中时不再调用 pytdx

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_quote_trustworthy.py -q
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import bars as bars_api
from app.core.time import now_shanghai
from app.models.bar import BarDaily


@pytest.fixture
def _reset_quote_state(monkeypatch):
    """每个测试前重置 quote 缓存与 pytdx 调用状态。"""
    monkeypatch.setattr(bars_api, "_quote_cache_get", AsyncMock(return_value=None))
    monkeypatch.setattr(bars_api, "_quote_cache_set", AsyncMock(return_value=None))
    monkeypatch.setattr(bars_api, "_fetch_pytdx_quote", AsyncMock(return_value=None))


async def _create_daily_bars(
    db_session: AsyncSession,
    instrument_id,
    trade_date: date,
    open_price: float = 10.0,
    high_price: float = 11.0,
    low_price: float = 9.5,
    close_price: float = 10.5,
    volume: float = 100000.0,
) -> BarDaily:
    bar = BarDaily(
        instrument_id=instrument_id,
        trade_date=trade_date,
        open=Decimal(str(open_price)),
        high=Decimal(str(high_price)),
        low=Decimal(str(low_price)),
        close=Decimal(str(close_price)),
        volume=Decimal(str(volume)),
        amount=Decimal(str(close_price * volume)),
    )
    db_session.add(bar)
    await db_session.flush()
    return bar


@pytest.mark.asyncio
async def test_quote_pytdx_success_during_trading_session(
    client,
    instrument_factory,
    monkeypatch,
):
    """交易时段 pytdx 成功：返回实时行情，不降级。"""
    instrument = await instrument_factory(symbol="000001", market="SH")
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
        "update_time": now.isoformat(),
        "is_realtime": True,
    }

    monkeypatch.setattr(bars_api, "_is_quote_realtime_session", AsyncMock(return_value=True))
    monkeypatch.setattr(bars_api, "_fetch_pytdx_quote", AsyncMock(return_value=pytdx_quote))

    response = await client.get(f"/api/v1/instruments/{instrument.id}/quote")
    assert response.status_code == 200, response.text

    data = response.json()
    assert data["source"] == "pytdx"
    assert data["is_realtime"] is True
    assert data["degraded"] is False
    assert data["degraded_reason"] is None
    assert "freshness_seconds" in data
    assert data["freshness_seconds"] >= 0
    assert data["freshness_seconds"] < 5
    assert data["update_time"] is not None
    assert data["symbol"] == instrument.symbol
    assert data["name"] == instrument.name


@pytest.mark.asyncio
async def test_quote_pytdx_failure_during_trading_session_fallback_daily(
    client,
    instrument_factory,
    db_session,
    monkeypatch,
):
    """交易时段 pytdx 失败：降级到日线 fallback，并标记 degraded。"""
    instrument = await instrument_factory(symbol="000002", market="SZ")
    today = now_shanghai().date()
    await _create_daily_bars(db_session, instrument.id, today)

    monkeypatch.setattr(bars_api, "_is_quote_realtime_session", AsyncMock(return_value=True))
    monkeypatch.setattr(bars_api, "_fetch_pytdx_quote", AsyncMock(return_value=None))

    response = await client.get(f"/api/v1/instruments/{instrument.id}/quote")
    assert response.status_code == 200, response.text

    data = response.json()
    assert data["source"] == "daily_fallback"
    assert data["is_realtime"] is False
    assert data["degraded"] is True
    assert data["degraded_reason"] is not None
    assert "pytdx" in data["degraded_reason"].lower() or "实时" in data["degraded_reason"]
    assert data["freshness_seconds"] >= 0
    assert data["update_time"] is not None


@pytest.mark.asyncio
async def test_quote_non_trading_session_skips_pytdx(
    client,
    instrument_factory,
    db_session,
    monkeypatch,
):
    """非交易时段直接读 DB，不调用 pytdx，也不标记 degraded。"""
    instrument = await instrument_factory(symbol="000003", market="SH")
    today = now_shanghai().date()
    await _create_daily_bars(db_session, instrument.id, today)

    pytdx_called = {"count": 0}

    async def _spy_fetch(symbol):
        pytdx_called["count"] += 1
        return None

    monkeypatch.setattr(bars_api, "_is_quote_realtime_session", AsyncMock(return_value=False))
    monkeypatch.setattr(bars_api, "_fetch_pytdx_quote", _spy_fetch)

    response = await client.get(f"/api/v1/instruments/{instrument.id}/quote")
    assert response.status_code == 200, response.text

    data = response.json()
    assert pytdx_called["count"] == 0
    assert data["source"] == "daily_fallback"
    assert data["is_realtime"] is False
    assert data["degraded"] is False
    assert data["degraded_reason"] is None


@pytest.mark.asyncio
async def test_quote_no_data_returns_404(
    client,
    instrument_factory,
    monkeypatch,
):
    """无 pytdx 且无 DB 数据时返回 404。"""
    instrument = await instrument_factory(symbol="000004", market="SZ")

    monkeypatch.setattr(bars_api, "_is_quote_realtime_session", AsyncMock(return_value=False))
    monkeypatch.setattr(bars_api, "_fetch_pytdx_quote", AsyncMock(return_value=None))

    response = await client.get(f"/api/v1/instruments/{instrument.id}/quote")
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_quote_redis_cache_hit_avoids_pytdx(
    client,
    instrument_factory,
    monkeypatch,
):
    """Redis 缓存命中时不再调用 pytdx，但仍返回 pytdx 来源与实时性。"""
    instrument = await instrument_factory(symbol="000005", market="SH")
    now = now_shanghai()
    cached_quote = {
        "instrument_id": str(instrument.id),
        "symbol": instrument.symbol,
        "name": instrument.name,
        "current_price": 20.0,
        "open": 19.5,
        "high": 20.5,
        "low": 19.0,
        "close": 20.0,
        "volume": 999999.0,
        "prev_close": 19.0,
        "change_pct": 5.26,
        "update_time": now.isoformat(),
        "is_realtime": True,
        "source": "pytdx",
        "freshness_seconds": 0.0,
        "degraded": False,
        "degraded_reason": None,
    }

    fetch_count = {"value": 0}

    async def _count_fetch(_symbol):
        fetch_count["value"] += 1
        return None

    monkeypatch.setattr(bars_api, "_is_quote_realtime_session", AsyncMock(return_value=True))
    monkeypatch.setattr(bars_api, "_fetch_pytdx_quote", _count_fetch)
    monkeypatch.setattr(bars_api, "_quote_cache_get", AsyncMock(return_value=cached_quote.copy()))

    response = await client.get(f"/api/v1/instruments/{instrument.id}/quote")
    assert response.status_code == 200, response.text

    data = response.json()
    assert fetch_count["value"] == 0
    assert data["source"] == "pytdx"
    assert data["is_realtime"] is True
    assert data["degraded"] is False
    assert data["current_price"] == 20.0
