"""MarketDataAggregationService 1d partial daily bar 测试。

覆盖：
1. 交易时段 include_realtime=True 时，1d 返回 data_source=hybrid、is_partial=True、last_live_bar_time 非空。
2. 非交易时段 include_realtime=True 不追加 partial bar。
3. partial daily bar 的 open/high/low/close/volume/amount 由当日已完成 1m 聚合而来。

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_market_data_aggregation_partial_daily.py -q
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import market_data_aggregation_service as mdas
from app.services.market_data_aggregation_service import MarketDataAggregationService


def _build_minute_df(trade_date: date, count: int = 5) -> pd.DataFrame:
    """构造 9:31 起的已完成 1m bars。"""
    rows = []
    base_price = 10.0
    for i in range(count):
        minute = 31 + i
        dt = datetime.combine(trade_date, datetime.min.time().replace(hour=9, minute=minute))
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        price = base_price + i * 0.1
        rows.append({
            "open": price,
            "high": price + 0.05,
            "low": price - 0.05,
            "close": price + 0.02,
            "volume": 1000.0 + i * 100,
            "amount": (1000.0 + i * 100) * price,
            "adj_factor": 1.0,
        })
    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex([
        datetime.combine(trade_date, datetime.min.time().replace(hour=9, minute=31 + i)).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        for i in range(count)
    ])
    df.index.name = "trade_time"
    return df


def _build_daily_df(trade_date: date, count: int = 3) -> pd.DataFrame:
    """构造历史日线（index 为 naive Timestamp，与真实 DB 返回一致）。"""
    rows = []
    dates = []
    for i in range(count, 0, -1):
        d = trade_date - pd.Timedelta(days=i)
        price = 10.0 + i * 0.1
        rows.append({
            "open": price - 0.05,
            "high": price + 0.05,
            "low": price - 0.08,
            "close": price,
            "volume": 10000.0,
            "amount": 10000.0 * price,
            "adj_factor": 1.0,
        })
        dates.append(pd.Timestamp(d))
    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex(dates)
    df.index.name = "trade_date"
    return df


@pytest.fixture
def _reset_cache(monkeypatch):
    """禁用 Redis 缓存，避免测试间互相影响。"""
    monkeypatch.setattr(mdas, "_cache_get", lambda _key: None)
    monkeypatch.setattr(mdas, "_cache_set", lambda _key, _value: None)


@pytest.mark.asyncio
async def test_partial_daily_bar_during_trading_session(
    db_session: AsyncSession,
    instrument_factory,
    monkeypatch,
    _reset_cache,
):
    """交易时段 1d bars 含 partial daily bar（Pytdx 原生日线）。

    [CHANGE-20260724-003] 1d 实时尾部使用 Pytdx 原生日线，禁止从 1m 聚合。
    """
    instrument = await instrument_factory(symbol="600519", market="SH", status="active")
    today = date(2026, 7, 6)

    daily_df = _build_daily_df(today, count=3)
    # 构造今日 partial daily bar（Pytdx 原生日线返回值）
    today_daily_df = pd.DataFrame({
        "open": [9.95],
        "high": [10.3],
        "low": [9.9],
        "close": [10.15],
        "volume": [5000.0],
        "amount": [50000.0],
        "adj_factor": [1.0],
    }, index=pd.DatetimeIndex([pd.Timestamp(today)]))
    today_daily_df.index.name = "trade_date"

    monkeypatch.setattr(mdas, "_query_daily_bars", AsyncMock(return_value=daily_df))
    monkeypatch.setattr(mdas, "fetch_daily_bars", AsyncMock(return_value=pd.DataFrame()))
    monkeypatch.setattr(mdas, "is_trading_day_async", AsyncMock(return_value=True))
    monkeypatch.setattr(
        mdas,
        "compute_market_session",
        lambda _now, _is_trading: mdas.MARKET_SESSION_MORNING,
    )
    monkeypatch.setattr(mdas, "fetch_today_daily_bars", AsyncMock(return_value=today_daily_df))

    # 固定 now 为交易时段内 10:00
    fixed_now = datetime.combine(today, datetime.min.time().replace(hour=10, minute=0))
    fixed_now = fixed_now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(mdas, "now_shanghai", lambda: fixed_now)

    result = await MarketDataAggregationService().get_bars(
        session=db_session,
        instrument_id=instrument.id,
        timeframe="1d",
        adj="none",
        include_realtime=True,
    )

    assert result.data_source == "hybrid"
    assert result.is_partial is True
    assert result.last_live_bar_time is not None
    assert not result.bars.empty
    last_bar = result.bars.iloc[-1]
    # [CHANGE-20260724-003] partial daily 直接来自 Pytdx 原生日线，不经过 1m 聚合
    assert float(last_bar["open"]) == pytest.approx(9.95)
    assert float(last_bar["close"]) == pytest.approx(10.15)
    assert float(last_bar["high"]) == pytest.approx(10.3)
    assert float(last_bar["low"]) == pytest.approx(9.9)
    assert float(last_bar["volume"]) == pytest.approx(5000.0)


@pytest.mark.asyncio
async def test_no_partial_daily_bar_outside_trading_session(
    db_session: AsyncSession,
    instrument_factory,
    monkeypatch,
    _reset_cache,
):
    """非交易时段 1d bars 不含 partial daily bar。"""
    instrument = await instrument_factory(symbol="600519", market="SH", status="active")
    today = date(2026, 7, 6)

    daily_df = _build_daily_df(today, count=3)

    monkeypatch.setattr(mdas, "_query_daily_bars", AsyncMock(return_value=daily_df))
    monkeypatch.setattr(mdas, "fetch_daily_bars", AsyncMock(return_value=pd.DataFrame()))
    monkeypatch.setattr(mdas, "is_trading_day_async", AsyncMock(return_value=True))
    monkeypatch.setattr(
        mdas,
        "compute_market_session",
        lambda _now, _is_trading: "closed",
    )

    fixed_now = datetime.combine(today, datetime.min.time().replace(hour=20, minute=0))
    fixed_now = fixed_now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(mdas, "now_shanghai", lambda: fixed_now)

    result = await MarketDataAggregationService().get_bars(
        session=db_session,
        instrument_id=instrument.id,
        timeframe="1d",
        adj="none",
        include_realtime=True,
    )

    assert result.is_partial is False
    assert result.last_live_bar_time is None


@pytest.mark.asyncio
async def test_partial_daily_fetch_today_daily_uses_correct_date(
    db_session: AsyncSession,
    instrument_factory,
    monkeypatch,
    _reset_cache,
):
    """1d partial daily 拉取今日日线时，today 参数必须为正确的上海交易日 date。

    [CHANGE-20260724-003] 1d 实时尾部使用 fetch_today_daily_bars(session, instrument_id, today)，
    禁止从 1m 聚合。today 参数由 now_shanghai().date() 派生，确保上海时区对齐。
    """
    instrument = await instrument_factory(symbol="600519", market="SH", status="active")
    today = date(2026, 7, 6)

    daily_df = _build_daily_df(today, count=3)

    monkeypatch.setattr(mdas, "_query_daily_bars", AsyncMock(return_value=daily_df))
    monkeypatch.setattr(mdas, "fetch_daily_bars", AsyncMock(return_value=pd.DataFrame()))
    monkeypatch.setattr(mdas, "is_trading_day_async", AsyncMock(return_value=True))
    monkeypatch.setattr(
        mdas,
        "compute_market_session",
        lambda _now, _is_trading: mdas.MARKET_SESSION_MORNING,
    )

    fixed_now = datetime.combine(today, datetime.min.time().replace(hour=10, minute=0))
    fixed_now = fixed_now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(mdas, "now_shanghai", lambda: fixed_now)

    captured: dict[str, Any] = {}

    async def _capture_fetch_today_daily(
        _session: AsyncSession,
        _instrument_id: uuid.UUID,
        today_param: date,
    ) -> pd.DataFrame:
        captured["today"] = today_param
        # 返回今日 partial daily bar
        today_df = pd.DataFrame({
            "open": [9.95], "high": [10.3], "low": [9.9],
            "close": [10.15], "volume": [5000.0],
            "amount": [50000.0], "adj_factor": [1.0],
        }, index=pd.DatetimeIndex([pd.Timestamp(today_param)]))
        today_df.index.name = "trade_date"
        return today_df

    monkeypatch.setattr(mdas, "fetch_today_daily_bars", _capture_fetch_today_daily)

    await MarketDataAggregationService().get_bars(
        session=db_session,
        instrument_id=instrument.id,
        timeframe="1d",
        adj="none",
        include_realtime=True,
    )

    assert "today" in captured, f"未捕获到 today 参数，captured={captured}"
    assert captured["today"] == today, (
        f"today 参数应为 {today}，实际：{captured['today']}"
    )
    assert isinstance(captured["today"], date), "today 参数必须为 date 类型"


@pytest.mark.asyncio
async def test_intraday_1m_fetch_minute_bars_uses_aware_datetime(
    db_session: AsyncSession,
    instrument_factory,
    monkeypatch,
    _reset_cache,
):
    """timeframe=1m 拉取实时 1m 时，start/end 必须同为 aware datetime。"""
    instrument = await instrument_factory(symbol="600519", market="SH", status="active")
    today = date(2026, 7, 6)

    monkeypatch.setattr(mdas, "_query_minute_bars", AsyncMock(return_value=pd.DataFrame()))
    monkeypatch.setattr(mdas, "_is_trading_hours", lambda _now: True)

    fixed_now = datetime.combine(today, datetime.min.time().replace(hour=10, minute=0))
    fixed_now = fixed_now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(mdas, "now_shanghai", lambda: fixed_now)

    captured: dict[str, Any] = {}

    async def _capture_fetch_minute_bars(
        _session: AsyncSession,
        _instrument_id: uuid.UUID,
        start_time: datetime,
        end_time: datetime,
    ) -> pd.DataFrame:
        captured["start_time"] = start_time
        captured["end_time"] = end_time
        return _build_minute_df(today, count=5)

    monkeypatch.setattr(mdas, "fetch_minute_bars", _capture_fetch_minute_bars)

    await MarketDataAggregationService().get_bars(
        session=db_session,
        instrument_id=instrument.id,
        timeframe="1m",
        adj="none",
        include_realtime=True,
    )

    assert "start_time" in captured, f"未捕获到 start_time，captured={captured}"
    assert "end_time" in captured, f"未捕获到 end_time，captured={captured}"
    assert captured["start_time"].tzinfo is not None
    assert captured["end_time"].tzinfo is not None
    # [mdas-timezone] - 必须能正常相减，不触发 offset-naive/offset-aware 异常
    delta = captured["end_time"] - captured["start_time"]
    assert delta.total_seconds() > 0
