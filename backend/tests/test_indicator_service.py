"""Task 8 测试 - 后端按 timeframe 计算 MACD。

验证 compute_all_indicators 在 15m/1h/1d/1w/1mo 不同 timeframe 下，
均使用对应周期 bars 计算 MACD，且返回的 time 数组与该周期 bars 时间对齐。

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_indicator_service.py -v
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.services import indicator_service

TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")


def _build_bars(frequency: str, length: int = 60) -> pd.DataFrame:
    """构造指定周期的 mock bars（含 OHLCV + adj_factor）。"""
    if frequency == "1d":
        dates = pd.date_range("2026-03-01", periods=length, freq="B")
    elif frequency == "15m":
        dates = pd.date_range("2026-06-18 09:30", periods=length, freq="15min")
    elif frequency == "1h":
        dates = pd.date_range("2026-06-18 09:30", periods=length, freq="1h")
    elif frequency == "1w":
        dates = pd.date_range("2024-01-01", periods=length, freq="W-MON")
    elif frequency == "1mo":
        dates = pd.date_range("2021-01-01", periods=length, freq="MS")
    else:
        raise ValueError(f"不支持的 frequency: {frequency}")

    closes = [10.0 + i * 0.1 for i in range(length)]
    df = pd.DataFrame({
        "open": [c - 0.1 for c in closes],
        "high": [c + 0.2 for c in closes],
        "low": [c - 0.2 for c in closes],
        "close": closes,
        "volume": [100000 + i for i in range(length)],
        "amount": [1000000 + i * 10 for i in range(length)],
        "adj_factor": [1.0] * length,
    }, index=dates)
    df.index = df.index.tz_localize("Asia/Shanghai")
    return df


@pytest.fixture
def mock_session() -> AsyncMock:
    """mock AsyncSession，execute 返回固定 symbol。"""
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = ("000001",)
    session.execute.return_value = result
    return session


@pytest.fixture
def mock_exchange(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """mock Exchange.klines 返回不同 frequency 的 bars。"""
    exchange = AsyncMock()

    async def klines(symbol: str, frequency: str, **kwargs) -> pd.DataFrame:
        if frequency == "1m":
            # 监控策略仅需要 2 根 1 分钟线
            return _build_bars("15m", length=2)
        return _build_bars(frequency)

    exchange.klines = klines
    monkeypatch.setattr(indicator_service, "get_exchange", lambda market: exchange)
    return exchange


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空策略注册表，避免测试中真实加载策略。"""
    monkeypatch.setattr(indicator_service.StrategyLoader, "_registry", {})


@pytest.mark.parametrize("timeframe", ["15m", "1h", "1d", "1w", "1mo"])
async def test_macd_time_matches_timeframe(
    mock_session: AsyncMock,
    mock_exchange: AsyncMock,
    empty_registry: None,
    timeframe: str,
) -> None:
    """MACD time 数组必须与请求 timeframe 的 bars 时间对齐。"""
    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, timeframe, "none", bars=250,
    )

    macd = result["data"]["macd"]
    assert "time" in macd, "MACD 数据应包含 time 字段"
    assert len(macd["time"]) == len(macd["macd_dif"]), "time 长度应与 macd_dif 一致"
    assert len(macd["time"]) == len(macd["macd_dea"]), "time 长度应与 macd_dea 一致"
    assert len(macd["time"]) == len(macd["macd_hist"]), "time 长度应与 macd_hist 一致"

    # 检查 MACD time 与当前 timeframe 的 mock bars 最后一个时间戳一致
    expected_bars = _build_bars(timeframe)
    expected_last_time = expected_bars.index[-1]
    last_time = pd.Timestamp(macd["time"][-1])
    assert last_time == expected_last_time, (
        f"MACD 最后一个时间应与 {timeframe} bars 对齐: "
        f"expected={expected_last_time}, actual={last_time}"
    )


@pytest.mark.parametrize("timeframe", ["15m", "1h", "1d", "1w", "1mo"])
async def test_macd_time_values_are_iso_strings(
    mock_session: AsyncMock,
    mock_exchange: AsyncMock,
    empty_registry: None,
    timeframe: str,
) -> None:
    """MACD time 数组元素应为 ISO 格式时间字符串（JSON 可序列化）。"""
    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, timeframe, "none", bars=250,
    )

    macd = result["data"]["macd"]
    for t in macd["time"]:
        assert isinstance(t, str), "time 元素应为字符串"
        # 验证可解析为 Timestamp
        assert pd.Timestamp(t) is not None


def test_map_daily_to_intraday_staircase() -> None:
    """日线 BB 值按日期映射到日内 bars 形成阶梯线。"""
    daily_times = [
        "2026-06-01T15:00:00+08:00",
        "2026-06-02T15:00:00+08:00",
        "2026-06-03T15:00:00+08:00",
        "2026-06-04T15:00:00+08:00",
        "2026-06-05T15:00:00+08:00",
    ]
    daily_values = [10.0, 11.0, 12.0, 13.0, 14.0]
    intraday_times = [
        "2026-06-03T09:30:00+08:00",
        "2026-06-03T09:45:00+08:00",
        "2026-06-04T09:30:00+08:00",
        "2026-06-04T09:45:00+08:00",
    ]
    result = indicator_service._map_daily_to_intraday(
        daily_values, daily_times, intraday_times,
    )
    # 06-03 盘中应取 06-02 收盘后的日线 BB；06-04 盘中应取 06-03 收盘后的日线 BB
    assert result == [11.0, 11.0, 12.0, 12.0], f"unexpected mapping: {result}"


def test_adapt_watchlist_bb_daily_preserved() -> None:
    """日线 timeframe 保留完整 BB 序列与 time 字段。"""
    indicators = {
        "bb_upper": [1.0, 2.0, 3.0, 4.0, 5.0],
        "bb_mid": [1.1, 2.1, 3.1, 4.1, 5.1],
        "bb_lower": [0.9, 1.9, 2.9, 3.9, 4.9],
        "upper_node": {"price_mid": 10.0},
    }
    daily_time_list = ["t1", "t2", "t3", "t4", "t5"]
    macd_bars = pd.DataFrame()
    macd_time_list: list[str] = []
    result = indicator_service._adapt_watchlist_bb(
        indicators, "1d", macd_bars, macd_time_list, daily_time_list,
    )
    assert result["bb_upper"] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert result["bb_mid"] == [1.1, 2.1, 3.1, 4.1, 5.1]
    assert result["upper_node"] == {"price_mid": 10.0}


def test_adapt_watchlist_bb_intraday_maps_to_macd_time() -> None:
    """15m/1h timeframe 将日线 BB 映射为日内阶梯线，time 用 macd_time_list。"""
    indicators = {
        "bb_upper": [10.0, 11.0, 12.0],
        "bb_mid": [9.0, 10.0, 11.0],
        "bb_lower": [8.0, 9.0, 10.0],
    }
    daily_time_list = [
        "2026-06-01T15:00:00+08:00",
        "2026-06-02T15:00:00+08:00",
        "2026-06-03T15:00:00+08:00",
    ]
    macd_times = [
        "2026-06-02T09:30:00+08:00",
        "2026-06-02T09:45:00+08:00",
        "2026-06-03T09:30:00+08:00",
    ]
    macd_bars = pd.DataFrame({"close": [1.0, 1.1, 1.2]}, index=pd.to_datetime(macd_times))
    result = indicator_service._adapt_watchlist_bb(
        indicators, "15m", macd_bars, macd_times, daily_time_list,
    )
    assert result["time"] == macd_times
    assert len(result["bb_upper"]) == len(macd_times)
    # 06-02 盘中取 06-01 收盘后的 BB；06-03 盘中取 06-02 收盘后的 BB
    assert result["bb_upper"] == [10.0, 10.0, 11.0]


def test_adapt_watchlist_bb_weekly_removes_bb() -> None:
    """周线/月线 timeframe 移除 BB 字段。"""
    indicators = {
        "bb_upper": [1.0, 2.0, 3.0],
        "bb_mid": [1.1, 2.1, 3.1],
        "bb_lower": [0.9, 1.9, 2.9],
        "upper_node": {"price_mid": 10.0},
    }
    result = indicator_service._adapt_watchlist_bb(
        indicators, "1w", pd.DataFrame(), [], [],
    )
    assert "bb_upper" not in result
    assert "bb_mid" not in result
    assert "bb_lower" not in result
    assert result["upper_node"] == {"price_mid": 10.0}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
