"""个股详情布局与行情唯一真源修复 — 定向回归测试。

验证 CHANGE-20260724-003 周期合同 + CHANGE-20260724-004 quote 解耦：
1. 15m 调用原生 15m 且不调用 1m 聚合
2. 60m 调用原生 60m
3. 1d 调用 Pytdx 原生日线
4. 1w/1mo 先合并今日 partial daily 再聚合
5. 同时间戳实时覆盖 DB
6. completed_only 强制 include_realtime=False
7. 实时空结果标记 stale（degraded + realtime_empty reason）
8. chart_snapshot quote 从 latest_daily_quote 派生（CHANGE-20260724-004）
9. PytdxAdapter 双线程串行且无死锁（RLock）
10. include_realtime=False 不调用任何 fetch_* 函数
11. 1d/1w/1mo quote 来自聚合前 daily_df
12. 1m/15m/1h quote 由已有目标周期 DataFrame 聚合
13. 各周期 quote 字段一致
14. spy 断言无额外 daily Pytdx/Repository 读取
15. include_realtime=False 无额外读取
16. 周/月 quote 不可用时不得使用周/月 OHLC 兜底
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services import market_data_aggregation_service as mdas

TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")
SHANGHAI = ZoneInfo("Asia/Shanghai")


async def _async_return(value: Any) -> Any:
    """辅助：让同步值可被 await。"""
    return value


def _build_daily_bars(
    dates: list[str],
    close_start: float = 10.0,
) -> pd.DataFrame:
    """构造 mock 日线 DataFrame（naive DatetimeIndex）。"""
    closes = [close_start + i * 0.1 for i in range(len(dates))]
    df = pd.DataFrame({
        "open": [c - 0.05 for c in closes],
        "high": [c + 0.1 for c in closes],
        "low": [c - 0.1 for c in closes],
        "close": closes,
        "volume": [100000.0 + i for i in range(len(dates))],
        "amount": [1000000.0 + i * 10 for i in range(len(dates))],
        "adj_factor": [1.0] * len(dates),
    }, index=pd.to_datetime(dates))
    df.index.name = "trade_date"
    return df


def _build_15min_bars(
    start: str,
    periods: int,
    close_start: float = 10.0,
) -> pd.DataFrame:
    """构造 mock 15 分钟线 DataFrame。"""
    times = pd.date_range(start, periods=periods, freq="15min")
    closes = [close_start + i * 0.01 for i in range(len(times))]
    df = pd.DataFrame({
        "open": [c - 0.01 for c in closes],
        "high": [c + 0.01 for c in closes],
        "low": [c - 0.01 for c in closes],
        "close": closes,
        "volume": [1000.0 + i for i in range(len(times))],
        "amount": [10000.0 + i * 10 for i in range(len(times))],
        "adj_factor": [1.0] * len(times),
    }, index=times)
    df.index.name = "trade_time"
    return df


def _build_60min_bars(
    start: str,
    periods: int,
    close_start: float = 10.0,
) -> pd.DataFrame:
    """构造 mock 60 分钟线 DataFrame。"""
    times = pd.date_range(start, periods=periods, freq="1h")
    closes = [close_start + i * 0.01 for i in range(len(times))]
    df = pd.DataFrame({
        "open": [c - 0.01 for c in closes],
        "high": [c + 0.01 for c in closes],
        "low": [c - 0.01 for c in closes],
        "close": closes,
        "volume": [1000.0 + i for i in range(len(times))],
        "amount": [10000.0 + i * 10 for i in range(len(times))],
        "adj_factor": [1.0] * len(times),
    }, index=times)
    df.index.name = "trade_time"
    return df


def _mock_session() -> AsyncMock:
    return AsyncMock()


def _mock_trading_hours(monkeypatch: pytest.MonkeyPatch, trading: bool = True) -> None:
    """Mock _is_trading_hours 和 is_trading_day_async。"""
    monkeypatch.setattr(mdas, "_is_trading_hours", lambda now: trading)
    monkeypatch.setattr(
        mdas, "is_trading_day_async",
        lambda *a, **kw: _async_return(trading),
    )


def _mock_redis_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock Redis 缓存未命中。"""
    monkeypatch.setattr(mdas, "_cache_get", lambda *a, **kw: None)
    monkeypatch.setattr(mdas, "_cache_set", lambda *a, **kw: None)


def _mock_symbol_and_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock _get_symbol 和 _get_listing_date 避免真实 DB 查询。"""
    monkeypatch.setattr(
        mdas, "_get_symbol",
        lambda session, instrument_id: _async_return("000001"),
    )
    monkeypatch.setattr(
        mdas, "_get_listing_date",
        lambda session, instrument_id: _async_return(date(2020, 1, 1)),
    )


def _mock_expected_daily(monkeypatch: pytest.MonkeyPatch, expected: date) -> None:
    """Mock _expected_last_completed_daily_bar 和 _call_expected_last_completed_daily_bar。"""
    monkeypatch.setattr(
        mdas, "_expected_last_completed_daily_bar",
        lambda session, now: expected,
    )
    monkeypatch.setattr(
        mdas, "_call_expected_last_completed_daily_bar",
        lambda session, now: _async_return(expected),
    )


def _mock_adj_factors_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock AdjustmentFactorService 返回空因子序列（跳过 qfq）。"""
    mock_service = MagicMock()
    mock_service.get_factor_series = lambda *a, **kw: _async_return(pd.DataFrame())
    mock_service.apply_qfq = lambda df, factor_df, **kw: df  # 直接返回原 df
    monkeypatch.setattr(mdas, "AdjustmentFactorService", lambda: mock_service)


# ============================================================
# 测试 1: 15m 调用原生 15m 且不调用 1m 聚合
# ============================================================


async def test_15m_uses_native_pytdx_15m_not_1m_aggregation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[P0-4] 15m 实时尾部必须调用 fetch_15min_bars，禁止调用 fetch_minute_bars。"""
    service = mdas.MarketDataAggregationService()
    db_df = _build_15min_bars("2026-07-24 09:30", periods=250)

    # Mock 日内查询返回 DB 数据
    monkeypatch.setattr(
        mdas, "_query_15min_bars",
        lambda *a, **kw: _async_return(db_df.copy()),
    )
    # Mock _fetch_intraday_with_backfill 避免复杂回补逻辑
    monkeypatch.setattr(
        mdas, "_fetch_intraday_with_backfill",
        lambda *a, **kw: _async_return((db_df.copy(), 1, False, "no_limit")),
    )

    _mock_trading_hours(monkeypatch, trading=True)
    _mock_redis_miss(monkeypatch)
    _mock_symbol_and_listing(monkeypatch)
    _mock_expected_daily(monkeypatch, date(2026, 7, 23))

    # 跟踪 fetch_minute_bars 和 fetch_15min_bars 调用
    minute_called = {"called": False}
    fifteen_called = {"called": False}

    async def _mock_fetch_minute(*a, **kw):
        minute_called["called"] = True
        return pd.DataFrame()

    async def _mock_fetch_15min(*a, **kw):
        fifteen_called["called"] = True
        return _build_15min_bars("2026-07-24 14:45", periods=1)

    monkeypatch.setattr(mdas, "fetch_minute_bars", _mock_fetch_minute)
    monkeypatch.setattr(mdas, "fetch_15min_bars", _mock_fetch_15min)

    # Mock adjust factor 为空（跳过 qfq）
    _mock_adj_factors_empty(monkeypatch)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none", include_realtime=True,
    )

    assert fifteen_called["called"], "15m 必须调用 fetch_15min_bars"
    assert not minute_called["called"], "15m 禁止调用 fetch_minute_bars（1m 聚合已废弃）"
    assert result.is_partial, "实时尾部存在时 is_partial=True"
    assert result.data_source in ("hybrid", "degraded")


# ============================================================
# 测试 2: 60m 调用原生 60m
# ============================================================


async def test_60m_uses_native_pytdx_60m(monkeypatch: pytest.MonkeyPatch) -> None:
    """[P0-4] 1h 实时尾部必须调用 fetch_60min_bars。"""
    service = mdas.MarketDataAggregationService()
    db_df = _build_60min_bars("2026-07-24 09:30", periods=250)

    monkeypatch.setattr(
        mdas, "_fetch_intraday_with_backfill",
        lambda *a, **kw: _async_return((db_df.copy(), 1, False, "no_limit")),
    )

    _mock_trading_hours(monkeypatch, trading=True)
    _mock_redis_miss(monkeypatch)
    _mock_symbol_and_listing(monkeypatch)
    _mock_expected_daily(monkeypatch, date(2026, 7, 23))

    sixty_called = {"called": False}

    async def _mock_fetch_60min(*a, **kw):
        sixty_called["called"] = True
        return _build_60min_bars("2026-07-24 14:00", periods=1)

    monkeypatch.setattr(mdas, "fetch_60min_bars", _mock_fetch_60min)
    _mock_adj_factors_empty(monkeypatch)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="1h", adj="none", include_realtime=True,
    )

    assert sixty_called["called"], "1h 必须调用 fetch_60min_bars"
    assert result.is_partial


# ============================================================
# 测试 3: 1d 调用 Pytdx 原生日线
# ============================================================


async def test_1d_uses_pytdx_native_daily(monkeypatch: pytest.MonkeyPatch) -> None:
    """[P0-4] 1d 实时尾部必须调用 fetch_today_daily_bars，禁止从 1m 聚合。"""
    service = mdas.MarketDataAggregationService()
    db_df = _build_daily_bars(["2026-07-20", "2026-07-21", "2026-07-22"])

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_df.copy()),
    )
    _mock_expected_daily(monkeypatch, date(2026, 7, 22))
    _mock_trading_hours(monkeypatch, trading=True)
    _mock_redis_miss(monkeypatch)
    _mock_symbol_and_listing(monkeypatch)

    # Mock compute_market_session 返回交易时段
    from app.services.market_status_service import (
        MARKET_SESSION_AFTERNOON,
    )
    monkeypatch.setattr(
        mdas, "compute_market_session",
        lambda now, is_trading_day: MARKET_SESSION_AFTERNOON,
    )

    today_daily_called = {"called": False}
    minute_called = {"called": False}

    async def _mock_fetch_today_daily(*a, **kw):
        today_daily_called["called"] = True
        today_df = _build_daily_bars(["2026-07-24"])
        today_df.index = pd.DatetimeIndex(["2026-07-24"])
        today_df.index.name = "trade_date"
        return today_df

    async def _mock_fetch_minute(*a, **kw):
        minute_called["called"] = True
        return pd.DataFrame()

    monkeypatch.setattr(mdas, "fetch_today_daily_bars", _mock_fetch_today_daily)
    monkeypatch.setattr(mdas, "fetch_minute_bars", _mock_fetch_minute)
    _mock_adj_factors_empty(monkeypatch)
    monkeypatch.setattr(
        mdas, "fetch_daily_bars",
        lambda *a, **kw: _async_return(pd.DataFrame()),
    )

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="1d", adj="none", include_realtime=True,
    )

    assert today_daily_called["called"], "1d 必须调用 fetch_today_daily_bars"
    assert not minute_called["called"], "1d 禁止调用 fetch_minute_bars（1m 聚合已废弃）"
    assert result.is_partial


# ============================================================
# 测试 4: 1w/1mo 先合并今日 partial daily 再聚合
# ============================================================


async def test_1w_merges_today_daily_before_aggregation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[P0-2] 1w 必须先合并今日 partial daily 再聚合周线。

    构造场景：DB 有 2026-07-20~22 日线（上周数据），今日 2026-07-24（本周四）。
    如果先聚合再补今日，1w 最后一根 bar 不含 2026-07-24。
    如果先合并再聚合，1w 最后一根 bar 的 close 包含 2026-07-24 的 close。
    """
    service = mdas.MarketDataAggregationService()
    # DB 日线：2026-07-20(周一)~22(周三)，缺少今日 24(周四)
    db_df = _build_daily_bars(["2026-07-20", "2026-07-21", "2026-07-22"])

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_df.copy()),
    )
    _mock_expected_daily(monkeypatch, date(2026, 7, 22))
    _mock_trading_hours(monkeypatch, trading=True)
    _mock_redis_miss(monkeypatch)
    _mock_symbol_and_listing(monkeypatch)

    from app.services.market_status_service import (
        MARKET_SESSION_AFTERNOON,
    )
    monkeypatch.setattr(
        mdas, "compute_market_session",
        lambda now, is_trading_day: MARKET_SESSION_AFTERNOON,
    )

    today_close = 99.99  # 特殊值，便于断言

    async def _mock_fetch_today_daily(*a, **kw):
        today_df = pd.DataFrame({
            "open": [99.0], "high": [100.0], "low": [98.0],
            "close": [today_close], "volume": [500000.0],
            "amount": [5000000.0], "adj_factor": [1.0],
        }, index=pd.DatetimeIndex(["2026-07-24"]))
        today_df.index.name = "trade_date"
        return today_df

    monkeypatch.setattr(mdas, "fetch_today_daily_bars", _mock_fetch_today_daily)
    _mock_adj_factors_empty(monkeypatch)
    monkeypatch.setattr(
        mdas, "fetch_daily_bars",
        lambda *a, **kw: _async_return(pd.DataFrame()),
    )

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="1w", adj="none", include_realtime=True,
    )

    assert result.is_partial, "1w 含今日 partial daily 时 is_partial=True"
    # 1w 最后一根 bar 的 close 应该包含今日数据
    # aggregate_kline 对周线 close 取最后一根日线的 close
    assert not result.bars.empty, "1w bars 不应为空"
    last_weekly_close = float(result.bars.iloc[-1]["close"])
    assert last_weekly_close == today_close, (
        f"1w 最后一根 bar close={last_weekly_close} 应等于今日 close={today_close}"
        "（证明今日 partial daily 在聚合前已合并）"
    )


# ============================================================
# 测试 5: 同时间戳实时覆盖 DB
# ============================================================


async def test_realtime_overwrites_db_same_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[P0-4] 同时间戳的实时 bar 必须覆盖 DB bar（_merge_bars keep=last）。"""
    service = mdas.MarketDataAggregationService()
    # DB 有 14:30 的 bar，close=10.0
    db_df = _build_15min_bars("2026-07-24 09:30", periods=250)

    monkeypatch.setattr(
        mdas, "_fetch_intraday_with_backfill",
        lambda *a, **kw: _async_return((db_df.copy(), 1, False, "no_limit")),
    )
    _mock_trading_hours(monkeypatch, trading=True)
    _mock_redis_miss(monkeypatch)
    _mock_symbol_and_listing(monkeypatch)
    _mock_expected_daily(monkeypatch, date(2026, 7, 23))

    # 实时返回 14:45 的 bar（DB 已有），close=99.99（特殊值）
    realtime_close = 99.99

    async def _mock_fetch_15min(*a, **kw):
        # 返回与 DB 最后一根相同时间戳的 bar，但 close 不同
        last_time = db_df.index[-1]
        return pd.DataFrame({
            "open": [99.0], "high": [100.0], "low": [98.0],
            "close": [realtime_close], "volume": [5000.0],
            "amount": [50000.0], "adj_factor": [1.0],
        }, index=pd.DatetimeIndex([last_time]))

    monkeypatch.setattr(mdas, "fetch_15min_bars", _mock_fetch_15min)
    _mock_adj_factors_empty(monkeypatch)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none", include_realtime=True,
    )

    # 验证最后一根 bar 的 close 被实时数据覆盖
    assert not result.bars.empty
    last_close = float(result.bars.iloc[-1]["close"])
    assert last_close == realtime_close, (
        f"同时间戳实时应覆盖 DB：last_close={last_close} != realtime_close={realtime_close}"
    )


# ============================================================
# 测试 6: completed_only 强制 include_realtime=False
# ============================================================


async def test_completed_only_forces_no_realtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[P0-12] completed_only=True 时强制 include_realtime=False，不调用任何 fetch_* 函数。"""
    service = mdas.MarketDataAggregationService()
    db_df = _build_daily_bars(["2026-07-20", "2026-07-21", "2026-07-22"])

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_df.copy()),
    )
    _mock_expected_daily(monkeypatch, date(2026, 7, 22))
    _mock_trading_hours(monkeypatch, trading=True)  # 即使在交易时段
    _mock_redis_miss(monkeypatch)
    _mock_symbol_and_listing(monkeypatch)

    fetch_called = {"called": False}

    async def _mock_fetch_today_daily(*a, **kw):
        fetch_called["called"] = True
        return pd.DataFrame()

    monkeypatch.setattr(mdas, "fetch_today_daily_bars", _mock_fetch_today_daily)
    _mock_adj_factors_empty(monkeypatch)
    monkeypatch.setattr(
        mdas, "fetch_daily_bars",
        lambda *a, **kw: _async_return(pd.DataFrame()),
    )

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="1d", adj="none",
        include_realtime=True,  # 显式传 True
        completed_only=True,     # 但 completed_only 强制覆盖
    )

    assert not fetch_called["called"], (
        "completed_only=True 时不得调用 fetch_today_daily_bars"
    )
    assert not result.is_partial, "completed_only 时 is_partial=False"
    assert result.data_source == "db"


# ============================================================
# 测试 7: 实时空结果标记 stale
# ============================================================


async def test_realtime_empty_marks_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[P0-6] 交易时段实时目标周期返回空时，必须标记 degraded + realtime_empty reason。"""
    service = mdas.MarketDataAggregationService()
    db_df = _build_15min_bars("2026-07-24 09:30", periods=250)

    monkeypatch.setattr(
        mdas, "_fetch_intraday_with_backfill",
        lambda *a, **kw: _async_return((db_df.copy(), 1, False, "no_limit")),
    )
    _mock_trading_hours(monkeypatch, trading=True)
    _mock_redis_miss(monkeypatch)
    _mock_symbol_and_listing(monkeypatch)
    _mock_expected_daily(monkeypatch, date(2026, 7, 23))

    # Pytdx 返回空 DataFrame
    async def _mock_fetch_15min_empty(*a, **kw):
        return pd.DataFrame()

    monkeypatch.setattr(mdas, "fetch_15min_bars", _mock_fetch_15min_empty)
    _mock_adj_factors_empty(monkeypatch)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none", include_realtime=True,
    )

    assert result.degraded, "实时返回空时必须 degraded=True"
    assert result.degraded_reason is not None
    assert "realtime_15m_empty_in_trading_hours" in result.degraded_reason, (
        f"degraded_reason 应含 realtime_15m_empty_in_trading_hours，实际：{result.degraded_reason}"
    )
    assert result.data_source == "degraded"


# ============================================================
# 测试 8: chart_snapshot quote 从 latest_daily_quote 派生（CHANGE-20260724-004）
# ============================================================


def test_quote_derived_from_latest_daily_quote() -> None:
    """[CHANGE-20260724-004] quote 唯一真源为 latest_daily_quote。

    - latest_daily_quote 存在时：所有周期返回完整 OHLC 字段
    - latest_daily_quote 缺失时：返回 None（禁止从 page_df 兜底）
    - 1w/1mo 不得使用聚合 bar 的 OHLC 作为当日行情
    """
    from app.api.chart_snapshot import _derive_quote_from_bars

    # 构造 1w bars（2 根周线）——这些不应影响 quote
    weekly_df = pd.DataFrame({
        "open": [10.0, 11.0], "high": [12.0, 13.0],
        "low": [9.0, 10.5], "close": [11.0, 12.5],
        "volume": [100000.0, 120000.0],
        "amount": [1000000.0, 1200000.0],
        "adj_factor": [1.0, 1.0],
    }, index=pd.DatetimeIndex(["2026-07-14", "2026-07-21"]))
    weekly_df.index.name = "trade_date"

    # 构造 mock bars_result，含 latest_daily_quote（当日日线事实）
    bars_result = MagicMock()
    bars_result.is_partial = True
    bars_result.last_live_bar_time = pd.Timestamp("2026-07-24 15:00")
    bars_result.last_persisted_bar_time = None
    bars_result.as_of = pd.Timestamp("2026-07-24 15:00")
    bars_result.latest_daily_quote = {
        "open": 9.8, "high": 10.5, "low": 9.6, "close": 10.2,
        "volume": 500000.0, "amount": 5000000.0,
        "prev_close": 10.0, "change_pct": 2.0,
    }

    # 1w quote：所有字段从 latest_daily_quote 派生，不从周线 bar 派生
    quote_weekly = _derive_quote_from_bars(weekly_df, bars_result, "1w")
    assert quote_weekly is not None
    assert quote_weekly["current_price"] == 10.2, "current_price 从 latest_daily_quote.close"
    assert quote_weekly["open"] == 9.8, "open 从 latest_daily_quote"
    assert quote_weekly["high"] == 10.5
    assert quote_weekly["low"] == 9.6
    assert quote_weekly["volume"] == 500000.0
    assert quote_weekly["amount"] == 5000000.0
    assert quote_weekly["prev_close"] == 10.0
    assert quote_weekly["change_pct"] == 2.0
    assert quote_weekly["is_realtime"] is True
    # 关键：不得使用周线 bar 的 OHLC（11.0/13.0 等）
    assert quote_weekly["open"] != 11.0, "禁止从 1w page_df 派生 open"
    assert quote_weekly["high"] != 13.0, "禁止从 1w page_df 派生 high"

    # 1mo quote：同 1w
    quote_monthly = _derive_quote_from_bars(weekly_df, bars_result, "1mo")
    assert quote_monthly["current_price"] == 10.2
    assert quote_monthly["open"] == 9.8

    # 1d quote：同样从 latest_daily_quote 派生
    quote_daily = _derive_quote_from_bars(weekly_df, bars_result, "1d")
    assert quote_daily["current_price"] == 10.2
    assert quote_daily["open"] == 9.8

    # latest_daily_quote 缺失时：返回 None（禁止兜底）
    bars_result_no_quote = MagicMock()
    bars_result_no_quote.latest_daily_quote = None
    bars_result_no_quote.is_partial = False
    bars_result_no_quote.last_live_bar_time = None
    bars_result_no_quote.last_persisted_bar_time = pd.Timestamp("2026-07-22")
    bars_result_no_quote.as_of = pd.Timestamp("2026-07-24")
    quote_none = _derive_quote_from_bars(weekly_df, bars_result_no_quote, "1w")
    assert quote_none is None, "latest_daily_quote 缺失时 quote 必须为 None"


# ============================================================
# 测试 9: PytdxAdapter 双线程串行且无死锁
# ============================================================


def test_pytdx_adapter_rlock_no_deadlock() -> None:
    """[P0-7] PytdxAdapter 使用 RLock，双线程并发调用不死锁。

    构造场景：两个线程同时调用 _fetch_with_retry，验证都能在超时内完成。
    RLock 保证同一线程内嵌套获取不自锁；跨线程串行执行。
    """
    from app.core.pytdx_adapter import PytdxAdapter

    adapter = PytdxAdapter()
    # 验证锁类型为 RLock
    assert isinstance(adapter._io_lock, type(threading.RLock())), (
        "_io_lock 必须为 threading.RLock（可重入），防止嵌套自锁"
    )

    # Mock _fetch_bars 模拟耗时操作
    call_order: list[str] = []
    call_lock = threading.Lock()

    def _mock_fetch_bars(symbol: str, period: str, count: int) -> pd.DataFrame:
        with call_lock:
            call_order.append(f"start:{symbol}")
        time.sleep(0.05)  # 模拟 I/O
        with call_lock:
            call_order.append(f"end:{symbol}")
        return pd.DataFrame()

    adapter._fetch_bars = _mock_fetch_bars  # type: ignore[assignment]

    # Mock connect 不做真实连接
    adapter._api = MagicMock()  # type: ignore[assignment]

    results: list[str] = []
    errors: list[Exception] = []

    def _worker(symbol: str) -> None:
        try:
            adapter._fetch_with_retry(symbol, "15min", 1)
            results.append(f"ok:{symbol}")
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_worker, args=("000001",))
    t2 = threading.Thread(target=_worker, args=("000002",))

    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert not t1.is_alive(), "线程 1 未死锁（5s 内完成）"
    assert not t2.is_alive(), "线程 2 未死锁（5s 内完成）"
    assert len(results) == 2, f"两个线程都应成功完成，实际：{results}"
    assert len(errors) == 0, f"不应有异常，实际：{errors}"

    # 验证串行执行：start:A 后必须先 end:A 再 start:B（或反之）
    # 两个线程的 start/end 不应交叉
    first_thread_calls = [c for c in call_order if "000001" in c]
    second_thread_calls = [c for c in call_order if "000002" in c]
    assert len(first_thread_calls) == 2, "线程1应有 start+end"
    assert len(second_thread_calls) == 2, "线程2应有 start+end"
    # 验证不交叉：第一个线程的 end 在第二个线程的 start 之前（或反之）
    first_end_idx = call_order.index(first_thread_calls[1])
    second_start_idx = call_order.index(second_thread_calls[0])
    second_end_idx = call_order.index(second_thread_calls[1])
    first_start_idx = call_order.index(first_thread_calls[0])
    # 两个区间 [first_start, first_end] 和 [second_start, second_end] 不应交叉
    if first_start_idx < second_start_idx:
        assert first_end_idx < second_start_idx, (
            f"串行执行：第一个线程 end 必须在第二个线程 start 之前，"
            f"call_order={call_order}"
        )
    else:
        assert second_end_idx < first_start_idx, (
            f"串行执行：第二个线程 end 必须在第一个线程 start 之前，"
            f"call_order={call_order}"
        )


# ============================================================
# 测试 10: include_realtime=False 不调用任何 fetch_* 函数
# ============================================================


async def test_include_realtime_false_no_fetch_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[P0-12] include_realtime=False 时不得调用任何 Pytdx fetch_* 函数（盘后 MFCS 回归）。"""
    service = mdas.MarketDataAggregationService()
    db_df = _build_daily_bars(["2026-07-20", "2026-07-21", "2026-07-22"])

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_df.copy()),
    )
    _mock_expected_daily(monkeypatch, date(2026, 7, 22))
    _mock_trading_hours(monkeypatch, trading=True)  # 即使在交易时段
    _mock_redis_miss(monkeypatch)
    _mock_symbol_and_listing(monkeypatch)

    fetch_calls: list[str] = []

    async def _track_fetch(name: str) -> Any:
        fetch_calls.append(name)
        return pd.DataFrame()

    monkeypatch.setattr(mdas, "fetch_today_daily_bars", lambda *a, **kw: _track_fetch("today_daily"))
    monkeypatch.setattr(mdas, "fetch_minute_bars", lambda *a, **kw: _track_fetch("minute"))
    monkeypatch.setattr(mdas, "fetch_15min_bars", lambda *a, **kw: _track_fetch("15min"))
    monkeypatch.setattr(mdas, "fetch_60min_bars", lambda *a, **kw: _track_fetch("60min"))
    _mock_adj_factors_empty(monkeypatch)
    monkeypatch.setattr(
        mdas, "fetch_daily_bars",
        lambda *a, **kw: _track_fetch("daily_backfill"),
    )

    # 1d include_realtime=False
    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="1d", adj="none", include_realtime=False,
    )

    assert not result.is_partial, "include_realtime=False 时 is_partial=False"
    assert result.data_source == "db"
    # fetch_daily_bars 可能被调用用于回补（非实时），但 fetch_today_daily_bars 不应被调用
    assert "today_daily" not in fetch_calls, (
        f"include_realtime=False 时不得调用 fetch_today_daily_bars，实际调用：{fetch_calls}"
    )
    assert "minute" not in fetch_calls
    assert "15min" not in fetch_calls
    assert "60min" not in fetch_calls


# ============================================================
# CHANGE-20260724-004 定向测试 11-16: latest_daily_quote 合同
# ============================================================


async def test_11_1d_1w_1mo_quote_from_pre_aggregation_daily_df(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[测试1] 1d/1w/1mo 的 latest_daily_quote 来自聚合前的 daily_df。

    1d: bars_df_full 即合并 partial 后的日线
    1w/1mo: daily_df 已合并今日 partial（聚合前）
    """
    service = mdas.MarketDataAggregationService()
    # DB 日线含今日
    db_df = _build_daily_bars(["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-24"])
    today_close = float(db_df.iloc[-1]["close"])

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_df.copy()),
    )
    _mock_expected_daily(monkeypatch, date(2026, 7, 23))
    _mock_trading_hours(monkeypatch, trading=False)  # 盘后，不触发 partial 合并
    _mock_redis_miss(monkeypatch)
    _mock_symbol_and_listing(monkeypatch)
    _mock_adj_factors_empty(monkeypatch)
    monkeypatch.setattr(
        mdas, "fetch_daily_bars",
        lambda *a, **kw: _async_return(pd.DataFrame()),
    )
    monkeypatch.setattr(
        mdas, "fetch_today_daily_bars",
        lambda *a, **kw: _async_return(pd.DataFrame()),
    )

    for tf in ("1d", "1w", "1mo"):
        result = await service.get_bars(
            _mock_session(), TEST_INSTRUMENT_ID,
            timeframe=tf, adj="none", include_realtime=False,
        )
        assert result.latest_daily_quote is not None, f"{tf}: latest_daily_quote 不应为 None"
        ldq = result.latest_daily_quote
        assert ldq["close"] == today_close, (
            f"{tf}: latest_daily_quote.close={ldq['close']} 应等于日线末根 close={today_close}"
        )
        assert "open" in ldq and ldq["open"] is not None
        assert "high" in ldq and ldq["high"] is not None
        assert "low" in ldq and ldq["low"] is not None
        assert "volume" in ldq
        assert "amount" in ldq
        assert "prev_close" in ldq
        assert "change_pct" in ldq


async def test_12_1m_15m_1h_quote_aggregated_from_target_period_df(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[测试2] 1m/15m/1h 的 latest_daily_quote 从已加载目标周期 DataFrame 聚合。

    构造 15m bars 跨两日，验证 latest_daily_quote 反映最后交易日的 OHLC 聚合。
    """
    service = mdas.MarketDataAggregationService()
    # 15m bars：2026-07-23 下午 + 2026-07-24 全天
    bars_day1 = _build_15min_bars("2026-07-23 13:00", periods=8)  # 4h
    bars_day2 = _build_15min_bars("2026-07-24 09:30", periods=16)  # 4h
    all_bars = pd.concat([bars_day1, bars_day2])

    monkeypatch.setattr(
        mdas, "_query_15min_bars",
        lambda *a, **kw: _async_return(all_bars.copy()),
    )
    monkeypatch.setattr(
        mdas, "_fetch_intraday_with_backfill",
        lambda *a, **kw: _async_return((all_bars.copy(), 1, False, "no_limit")),
    )
    _mock_trading_hours(monkeypatch, trading=False)  # 盘后
    _mock_redis_miss(monkeypatch)
    _mock_symbol_and_listing(monkeypatch)
    _mock_expected_daily(monkeypatch, date(2026, 7, 23))
    _mock_adj_factors_empty(monkeypatch)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none", include_realtime=False,
    )

    assert result.latest_daily_quote is not None
    ldq = result.latest_daily_quote
    day2_bars = all_bars[all_bars.index.date == pd.Timestamp("2026-07-24").date()]
    assert ldq["open"] == float(day2_bars.iloc[0]["open"]), "open = 当日首根 open"
    assert ldq["high"] == float(day2_bars["high"].max()), "high = 当日最高"
    assert ldq["low"] == float(day2_bars["low"].min()), "low = 当日最低"
    assert ldq["close"] == float(day2_bars.iloc[-1]["close"]), "close = 当日末根 close"
    assert ldq["volume"] == float(day2_bars["volume"].sum()), "volume = 当日成交量之和"
    # prev_close = 上一交易日末根 close
    day1_bars = all_bars[all_bars.index.date == pd.Timestamp("2026-07-23").date()]
    assert ldq["prev_close"] == float(day1_bars.iloc[-1]["close"]), "prev_close = 上一交易日末根 close"


async def test_13_all_timeframes_quote_fields_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[测试3] 各周期 latest_daily_quote 字段一致（同一组键）。

    所有周期返回相同的键集合：open/high/low/close/volume/amount/prev_close/change_pct
    """
    service = mdas.MarketDataAggregationService()
    db_daily = _build_daily_bars(["2026-07-20", "2026-07-21", "2026-07-22"])
    db_15m = _build_15min_bars("2026-07-22 09:30", periods=16)

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_daily.copy()),
    )
    monkeypatch.setattr(
        mdas, "_query_15min_bars",
        lambda *a, **kw: _async_return(db_15m.copy()),
    )
    monkeypatch.setattr(
        mdas, "_fetch_intraday_with_backfill",
        lambda *a, **kw: _async_return((db_15m.copy(), 1, False, "no_limit")),
    )
    _mock_trading_hours(monkeypatch, trading=False)
    _mock_redis_miss(monkeypatch)
    _mock_symbol_and_listing(monkeypatch)
    _mock_expected_daily(monkeypatch, date(2026, 7, 22))
    _mock_adj_factors_empty(monkeypatch)
    monkeypatch.setattr(
        mdas, "fetch_daily_bars",
        lambda *a, **kw: _async_return(pd.DataFrame()),
    )

    expected_keys = {"open", "high", "low", "close", "volume", "amount", "prev_close", "change_pct"}
    for tf in ("1d", "1w", "1mo", "15m"):
        result = await service.get_bars(
            _mock_session(), TEST_INSTRUMENT_ID,
            timeframe=tf, adj="none", include_realtime=False,
        )
        assert result.latest_daily_quote is not None, f"{tf}: latest_daily_quote 不应为 None"
        actual_keys = set(result.latest_daily_quote.keys())
        assert actual_keys == expected_keys, (
            f"{tf}: latest_daily_quote 键不一致，期望={expected_keys}，实际={actual_keys}"
        )


async def test_14_no_extra_daily_reads_for_intraday_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[测试4] 15m/1h/1m quote 不得触发额外 daily Pytdx/Repository 读取。

    spy fetch_today_daily_bars 和 _query_daily_bars，断言 15m 路径不调用它们用于 quote。
    """
    service = mdas.MarketDataAggregationService()
    db_15m = _build_15min_bars("2026-07-24 09:30", periods=250)

    monkeypatch.setattr(
        mdas, "_fetch_intraday_with_backfill",
        lambda *a, **kw: _async_return((db_15m.copy(), 1, False, "no_limit")),
    )
    _mock_trading_hours(monkeypatch, trading=False)
    _mock_redis_miss(monkeypatch)
    _mock_symbol_and_listing(monkeypatch)
    _mock_expected_daily(monkeypatch, date(2026, 7, 23))
    _mock_adj_factors_empty(monkeypatch)

    daily_call_count = {"query_daily": 0, "today_daily": 0, "fetch_daily": 0}

    async def _spy_query_daily(*a, **kw):
        daily_call_count["query_daily"] += 1
        return pd.DataFrame()

    async def _spy_today_daily(*a, **kw):
        daily_call_count["today_daily"] += 1
        return pd.DataFrame()

    async def _spy_fetch_daily(*a, **kw):
        daily_call_count["fetch_daily"] += 1
        return pd.DataFrame()

    monkeypatch.setattr(mdas, "_query_daily_bars", _spy_query_daily)
    monkeypatch.setattr(mdas, "fetch_today_daily_bars", _spy_today_daily)
    monkeypatch.setattr(mdas, "fetch_daily_bars", _spy_fetch_daily)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none", include_realtime=False,
    )

    # 15m 盘后路径不应调用任何 daily 查询（quote 从 15m bars 聚合）
    assert daily_call_count["query_daily"] == 0, (
        f"15m 不得调用 _query_daily_bars 用于 quote，实际调用 {daily_call_count['query_daily']} 次"
    )
    assert daily_call_count["today_daily"] == 0, (
        f"15m 不得调用 fetch_today_daily_bars 用于 quote，实际调用 {daily_call_count['today_daily']} 次"
    )
    assert daily_call_count["fetch_daily"] == 0, (
        f"15m 不得调用 fetch_daily_bars 用于 quote，实际调用 {daily_call_count['fetch_daily']} 次"
    )
    # latest_daily_quote 仍应有值（从 15m bars 聚合）
    assert result.latest_daily_quote is not None, "15m 应从 bars 聚合出 latest_daily_quote"


async def test_15_include_realtime_false_no_extra_daily_for_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[测试5] include_realtime=False 时 latest_daily_quote 不得产生额外日线查询。

    1d 盘后路径：daily_df 已有数据，latest_daily_quote 从 daily_df 派生，不额外查询。
    """
    service = mdas.MarketDataAggregationService()
    db_daily = _build_daily_bars(["2026-07-20", "2026-07-21", "2026-07-22"])

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_daily.copy()),
    )
    _mock_expected_daily(monkeypatch, date(2026, 7, 22))
    _mock_trading_hours(monkeypatch, trading=False)
    _mock_redis_miss(monkeypatch)
    _mock_symbol_and_listing(monkeypatch)
    _mock_adj_factors_empty(monkeypatch)

    extra_calls = {"today_daily": 0, "fetch_daily": 0}

    async def _spy_today(*a, **kw):
        extra_calls["today_daily"] += 1
        return pd.DataFrame()

    async def _spy_fetch(*a, **kw):
        extra_calls["fetch_daily"] += 1
        return pd.DataFrame()

    monkeypatch.setattr(mdas, "fetch_today_daily_bars", _spy_today)
    monkeypatch.setattr(mdas, "fetch_daily_bars", _spy_fetch)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="1d", adj="none", include_realtime=False,
    )

    assert extra_calls["today_daily"] == 0, "include_realtime=False 不得调用 fetch_today_daily_bars"
    assert extra_calls["fetch_daily"] == 0, "include_realtime=False 不得调用 fetch_daily_bars"
    assert result.latest_daily_quote is not None, "1d 盘后应从 daily_df 派生 latest_daily_quote"
    assert result.latest_daily_quote["close"] == float(db_daily.iloc[-1]["close"])


async def test_16_weekly_quote_unavailable_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[测试6] 1w quote 不可用时不得使用 1w OHLC 兜底。

    构造 daily_df 为空 + 1w bars 有数据，验证 latest_daily_quote=None
    且 _derive_quote_from_bars 返回 None（禁止从 1w page_df 派生）。
    """
    from app.api.chart_snapshot import _compute_freshness_state, _derive_quote_from_bars

    # 1w bars 有数据，但 latest_daily_quote 为 None
    weekly_df = pd.DataFrame({
        "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
        "volume": [100000.0], "amount": [1000000.0],
    }, index=pd.DatetimeIndex(["2026-07-21"]))

    bars_result = MagicMock()
    bars_result.latest_daily_quote = None
    bars_result.is_partial = False
    bars_result.degraded = False
    bars_result.degraded_reason = None
    bars_result.last_live_bar_time = None
    bars_result.last_persisted_bar_time = pd.Timestamp("2026-07-21")
    bars_result.as_of = pd.Timestamp("2026-07-24")

    # quote 必须为 None
    quote = _derive_quote_from_bars(weekly_df, bars_result, "1w")
    assert quote is None, "latest_daily_quote 缺失时 1w quote 必须为 None（禁止从 1w page_df 兜底）"

    # freshness 必须为 unavailable
    freshness = _compute_freshness_state(bars_result, weekly_df)
    assert freshness == "unavailable", (
        f"latest_daily_quote 缺失时 freshness 应为 unavailable，实际={freshness}"
    )
