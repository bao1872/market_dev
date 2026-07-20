"""Task 4.5 测试 - market_data_aggregation_service 作为行情聚合唯一事实源。

验证：
1. 日线 DB 命中且无缺口：返回 db 数据源、非 degraded、无 partial
2. DB 尾部缺失：调用 Pytdx 补齐并标记 hybrid
3. Pytdx 失败：降级到 DB，返回 degraded=true，不抛 502
4. 非交易时段：不调用实时源
5. 15m/1h 交易时段：拉 1m 聚合为 partial bar
6. Redis 缓存命中：直接返回缓存，不查 DB
7. 返回对象包含所有数据源诊断字段

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_market_data_aggregation_service.py -v
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services import market_data_aggregation_service as mdas

TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")


async def _async_return(value: Any) -> Any:
    """辅助：让同步值可被 await（用于 monkeypatch 异步函数）。"""
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


def _build_minute_bars(
    start: str,
    periods: int,
    freq: str = "1min",
) -> pd.DataFrame:
    """构造 mock 1 分钟线 DataFrame（naive DatetimeIndex）。"""
    times = pd.date_range(start, periods=periods, freq=freq)
    closes = [10.0 + i * 0.01 for i in range(len(times))]
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
    """返回空的 mock AsyncSession。"""
    return AsyncMock()


@pytest.fixture(autouse=True)
def _freeze_non_trading_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定 is_trading_day_async 返回 False，避免 1d partial daily 合成分支依赖 CI 运行时间。

    根因: MDAS get_bars 在 ``timeframe=="1d" and include_realtime`` 时（默认 True），
    会调用 ``is_trading_day_async(session, now.date())`` + ``compute_market_session(now, is_trading_day)``
    判断是否进入 partial daily 合成。该调用未被测试 mock，导致：
      - CI 在交易时段运行（北京时间 9:30-15:00）→ 进入合成分支 → mock session 不匹配 → degraded
      - CI 在非交易时段运行 → 不进入合成分支 → 测试通过（main CI 即此情况）

    本测试文件的所有 1d 用例均 mock 了 ``_expected_last_completed_daily_bar``，
    不依赖 ``is_trading_day_async`` 的真实行为；15m 用例走 ``_is_trading_hours`` 分支不受影响。
    固定为非交易日可让测试结果与 CI 运行时间解耦。
    """
    monkeypatch.setattr(
        mdas, "is_trading_day_async",
        lambda *a, **kw: _async_return(False),
    )


# ============================================================
# 基础返回结构
# ============================================================


async def test_get_bars_returns_bar_aggregation_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_bars 返回 BarAggregationResult，包含 bars DataFrame 与诊断字段。"""
    service = mdas.MarketDataAggregationService()
    db_df = _build_daily_bars(["2026-06-16", "2026-06-17", "2026-06-18"])

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_df.copy()),
    )
    monkeypatch.setattr(
        mdas, "_expected_last_completed_daily_bar",
        lambda session, now: date(2026, 6, 18),
    )
    monkeypatch.setattr(mdas, "_is_trading_hours", lambda now: False)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID, timeframe="1d", adj="none",
    )

    assert isinstance(result, mdas.BarAggregationResult)
    assert result.data_source == "db"
    assert not result.degraded
    assert result.degraded_reason is None
    assert result.last_persisted_bar_time == pd.Timestamp("2026-06-18")
    assert len(result.bars) == 3


# ============================================================
# 日线：DB 命中无缺口
# ============================================================


async def test_daily_db_hit_no_gap_returns_db_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 数据已覆盖最后一个已完成 bar，不请求 Pytdx。"""
    service = mdas.MarketDataAggregationService()
    db_df = _build_daily_bars(["2026-06-16", "2026-06-17", "2026-06-18"])

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_df.copy()),
    )
    monkeypatch.setattr(
        mdas, "_expected_last_completed_daily_bar",
        lambda session, now: date(2026, 6, 18),
    )
    monkeypatch.setattr(mdas, "_is_trading_hours", lambda now: False)
    pytdx_called = {"called": False}
    monkeypatch.setattr(
        mdas, "fetch_daily_bars",
        lambda *a, **kw: _async_return(pytdx_called.update(called=True) or pd.DataFrame()),
    )

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID, timeframe="1d", adj="none",
    )

    assert result.data_source == "db"
    assert not result.degraded
    assert not result.is_partial
    assert result.last_persisted_bar_time == pd.Timestamp("2026-06-18")
    assert result.last_live_bar_time is None
    assert not pytdx_called["called"], "DB 无缺口时不应请求 Pytdx"


# ============================================================
# 日线：DB 有历史但尾部缺一天，请求 Pytdx 补齐
# ============================================================


async def test_daily_db_missing_tail_calls_pytdx(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 最新 bar 早于最后一个已完成 bar，应调用 Pytdx 补齐。"""
    service = mdas.MarketDataAggregationService()
    db_df = _build_daily_bars(["2026-06-16", "2026-06-17"])  # 缺 06-18
    pytdx_tail = _build_daily_bars(["2026-06-18"], close_start=11.0)

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_df.copy()),
    )
    monkeypatch.setattr(
        mdas, "_expected_last_completed_daily_bar",
        lambda session, now: date(2026, 6, 18),
    )
    monkeypatch.setattr(mdas, "_is_trading_hours", lambda now: False)
    monkeypatch.setattr(
        mdas, "fetch_daily_bars",
        lambda *a, **kw: _async_return(pytdx_tail.copy()),
    )

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID, timeframe="1d", adj="none",
    )

    assert result.data_source == "hybrid"
    assert not result.degraded
    assert len(result.bars) == 3
    assert pd.Timestamp("2026-06-18") in result.bars.index
    assert result.last_persisted_bar_time == pd.Timestamp("2026-06-17")
    assert result.last_live_bar_time == pd.Timestamp("2026-06-18")


# ============================================================
# 日线去重：pytdx 15:00 与 DB 00:00 同日不产生重复 bar
# ============================================================


def _build_pytdx_daily_bars(
    dates: list[str],
    close_start: float = 10.0,
) -> pd.DataFrame:
    """构造模拟 pytdx 日线 DataFrame（datetime 列为 15:00 收盘时刻）。

    pytdx get_daily_bars 返回的 datetime 为收盘时刻 15:00，
    与 DB trade_date（午夜 00:00）不同，需由 fetch_daily_bars 规范化到午夜后才能去重。
    """
    closes = [close_start + i * 0.1 for i in range(len(dates))]
    dt_15h = [pd.Timestamp(d) + pd.Timedelta(hours=15) for d in dates]
    df = pd.DataFrame({
        "datetime": dt_15h,
        "open": [c - 0.05 for c in closes],
        "high": [c + 0.1 for c in closes],
        "low": [c - 0.1 for c in closes],
        "close": closes,
        "volume": [100000.0 + i for i in range(len(dates))],
        "amount": [1000000.0 + i * 10 for i in range(len(dates))],
    })
    return df


async def test_daily_pytdx_15h_dedup_with_db_00h(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytdx 日线（15:00）与 DB 日线（00:00）同日重叠时不得产生重复 bar。

    回归测试：fetch_daily_bars 必须将 pytdx 的 15:00 datetime 规范化到午夜，
    使 _merge_bars 的 index.duplicated(keep="last") 能按交易日去重。
    """
    service = mdas.MarketDataAggregationService()
    # DB 有 06-16、06-17（00:00 索引）
    db_df = _build_daily_bars(["2026-06-16", "2026-06-17"])
    # pytdx 返回 06-17、06-18（15:00 datetime 列，模拟真实 pytdx）
    # 06-17 与 DB 重叠 —— 必须去重，只保留 pytdx 版本（keep="last"）
    pytdx_raw = _build_pytdx_daily_bars(["2026-06-17", "2026-06-18"], close_start=12.0)

    async def fake_fetch(session, instrument_id, start, end):
        # 模拟真实 fetch_daily_bars 的处理：set_index + normalize
        df = pytdx_raw.copy()
        df = df.set_index("datetime")
        df.index = df.index.normalize()
        df.index.name = "trade_date"
        if "adj_factor" not in df.columns:
            df["adj_factor"] = 1.0
        return df

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_df.copy()),
    )
    monkeypatch.setattr(
        mdas, "_expected_last_completed_daily_bar",
        lambda session, now: date(2026, 6, 18),
    )
    monkeypatch.setattr(mdas, "_is_trading_hours", lambda now: False)
    monkeypatch.setattr(mdas, "fetch_daily_bars", fake_fetch)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID, timeframe="1d", adj="none",
    )

    assert result.data_source == "hybrid"
    assert not result.degraded
    # 必须只有 3 根 bar（06-16, 06-17, 06-18），06-17 不得重复
    assert len(result.bars) == 3
    # 验证索引无重复
    assert not result.bars.index.duplicated().any()
    # 06-17 应保留 pytdx 版本（close=12.0，keep="last"）
    bar_0617 = result.bars.loc[pd.Timestamp("2026-06-17")]
    assert bar_0617["close"] == 12.0


async def test_fetch_daily_bars_normalizes_pytdx_15h_to_midnight(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_daily_bars 直接单测：pytdx 15:00 datetime 必须规范化到午夜 00:00。"""
    pytdx_raw = _build_pytdx_daily_bars(["2026-07-08", "2026-07-09"], close_start=28.0)

    monkeypatch.setattr(
        mdas, "_get_symbol",
        lambda session, instrument_id: _async_return("603538"),
    )
    monkeypatch.setattr(
        mdas, "get_pytdx_adapter",
        lambda: type("FakeAdapter", (), {
            "get_daily_bars": lambda self, symbol, start, end: pytdx_raw.copy(),
        })(),
    )

    result = await mdas.fetch_daily_bars(
        _mock_session(), TEST_INSTRUMENT_ID, date(2026, 7, 1), date(2026, 7, 10),
    )

    assert len(result) == 2
    # 索引必须为午夜 00:00:00，不得保留 15:00
    for ts in result.index:
        assert ts.hour == 0, f"索引 {ts} 未规范化到午夜，hour={ts.hour}"
        assert ts.minute == 0
    assert result.index.name == "trade_date"


# ============================================================
# Pytdx 失败降级
# ============================================================


async def test_pytdx_failure_returns_degraded_with_db_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pytdx 补尾失败时返回 DB 数据，并标记 degraded=true。"""
    service = mdas.MarketDataAggregationService()
    db_df = _build_daily_bars(["2026-06-16", "2026-06-17"])

    async def failing_fetch(*args, **kwargs):
        raise RuntimeError("pytdx timeout")

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_df.copy()),
    )
    monkeypatch.setattr(
        mdas, "_expected_last_completed_daily_bar",
        lambda session, now: date(2026, 6, 18),
    )
    monkeypatch.setattr(mdas, "_is_trading_hours", lambda now: False)
    monkeypatch.setattr(mdas, "fetch_daily_bars", failing_fetch)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID, timeframe="1d", adj="none",
    )

    assert result.data_source == "degraded"
    assert result.degraded is True
    assert result.degraded_reason is not None
    assert "pytdx" in result.degraded_reason.lower() or "timeout" in result.degraded_reason.lower()
    assert len(result.bars) == 2


# ============================================================
# 非交易时段不调用 Pytdx 实时源
# ============================================================


async def test_non_trading_hours_skips_realtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """非交易时段 include_realtime=True 也不调用实时 1m 源。"""
    service = mdas.MarketDataAggregationService()
    db_df = _build_daily_bars(["2026-06-16", "2026-06-17", "2026-06-18"])

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_df.copy()),
    )
    monkeypatch.setattr(
        mdas, "_expected_last_completed_daily_bar",
        lambda session, now: date(2026, 6, 18),
    )
    monkeypatch.setattr(mdas, "_is_trading_hours", lambda now: False)
    realtime_called = {"called": False}
    monkeypatch.setattr(
        mdas, "fetch_minute_bars",
        lambda *a, **kw: _async_return(realtime_called.update(called=True) or pd.DataFrame()),
    )

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID, timeframe="1d", adj="none",
        include_realtime=True,
    )

    assert not realtime_called["called"]
    assert not result.is_partial


# ============================================================
# 交易时段 15m 拉 1m 聚合为 partial bar
# ============================================================


async def test_intraday_15m_aggregates_live_partial_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    """交易时段请求 15m，服务从 1m 聚合出当前 partial bar。"""
    service = mdas.MarketDataAggregationService()
    # DB 已有一个历史 15m bar（09:30）
    db_15m = pd.DataFrame({
        "open": [10.0],
        "high": [10.1],
        "low": [9.9],
        "close": [10.05],
        "volume": [10000.0],
        "amount": [100000.0],
        "adj_factor": [1.0],
    }, index=pd.to_datetime(["2026-06-18 09:30:00"]))
    db_15m.index.name = "trade_time"
    # 1m 数据覆盖 09:45 新周期（未完成）
    live_1m = _build_minute_bars("2026-06-18 09:45:00", periods=5)

    monkeypatch.setattr(mdas, "_query_15min_bars", lambda *a, **kw: _async_return(db_15m.copy()))
    monkeypatch.setattr(mdas, "_is_trading_hours", lambda now: True)
    monkeypatch.setattr(
        mdas, "fetch_minute_bars",
        lambda *a, **kw: _async_return(live_1m.copy()),
    )

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID, timeframe="15m", adj="none",
        include_realtime=True,
    )

    assert result.data_source == "hybrid"
    assert result.is_partial is True
    assert result.last_live_bar_time is not None
    assert pd.Timestamp("2026-06-18 09:45:00") in result.bars.index


# ============================================================
# Redis 缓存命中
# ============================================================


async def test_cache_hit_returns_cached_result_without_db_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis 缓存命中时直接返回，不查 DB。"""
    service = mdas.MarketDataAggregationService()
    db_df = _build_daily_bars(["2026-06-16", "2026-06-17", "2026-06-18"])
    cached = mdas.BarAggregationResult(
        bars=db_df.copy(),
        data_source="db",
        as_of=datetime.now(ZoneInfo("Asia/Shanghai")),
        is_partial=False,
        last_persisted_bar_time=pd.Timestamp("2026-06-18"),
        last_live_bar_time=None,
        freshness_seconds=2.0,
        degraded=False,
        degraded_reason=None,
    )

    db_called = {"called": False}
    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_called.update(called=True) or db_df.copy()),
    )
    monkeypatch.setattr(
        mdas, "_cache_get", lambda *a, **kw: cached,
    )

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID, timeframe="1d", adj="none",
    )

    assert not db_called["called"], "缓存命中时不应查询 DB"
    assert result.data_source == "db"
    assert result.last_persisted_bar_time == pd.Timestamp("2026-06-18")


# ============================================================
# 诊断字段完整性
# ============================================================


async def test_diagnostic_fields_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """BarAggregationResult 包含 spec 要求的所有诊断字段。"""
    service = mdas.MarketDataAggregationService()
    db_df = _build_daily_bars(["2026-06-18"])

    monkeypatch.setattr(
        mdas, "_query_daily_bars",
        lambda *a, **kw: _async_return(db_df.copy()),
    )
    monkeypatch.setattr(
        mdas, "_expected_last_completed_daily_bar",
        lambda session, now: date(2026, 6, 18),
    )
    monkeypatch.setattr(mdas, "_is_trading_hours", lambda now: False)

    result = await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID, timeframe="1d", adj="none",
    )

    assert hasattr(result, "data_source")
    assert hasattr(result, "as_of")
    assert hasattr(result, "is_partial")
    assert hasattr(result, "last_persisted_bar_time")
    assert hasattr(result, "last_live_bar_time")
    assert hasattr(result, "freshness_seconds")
    assert hasattr(result, "degraded")
    assert hasattr(result, "degraded_reason")
    assert isinstance(result.freshness_seconds, (int, float))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
