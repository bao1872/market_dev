"""Task 1 测试 - 图表行情契约 + Task 8 MACD timeframe 对齐。

验证：
1. compute_all_indicators 在不同 timeframe 下使用对应周期 bars 计算 MACD
2. 响应包含 source_bar_times 和 source_bar_hash（SubTask 1.4）
3. 策略返回 time 时不被覆盖（SubTask 1.3）
4. 日线行情来自 MarketDataAggregationService（不再通过 Exchange.klines）

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
    """构造指定周期的 mock bars（含 OHLCV + adj_factor，naive DatetimeIndex）。

    naive DatetimeIndex 与 MarketDataAggregationService 和 DB 查询的实际返回一致。
    """
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
    # 不再 tz_localize：MarketDataAggregationService 和 DB 查询返回 naive DatetimeIndex
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
def mock_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    """[图表行情契约] - mock MarketDataAggregationService + DB 查询函数。

    日线通过 MarketDataAggregationService（行情聚合 SSOT）获取；
    日内/周线/月线通过 DB 查询函数获取（与 indicator_service.py 当前实现一致）。
    所有 mock 数据使用 naive DatetimeIndex（与 DB 查询的实际行为一致）。
    """
    from datetime import datetime

    from app.services.market_data_aggregation_service import BarAggregationResult

    class _MockAggService:
        async def get_bars(self, session, instrument_id, timeframe="1d", adj="qfq", **kwargs):
            return BarAggregationResult(
                bars=_build_bars("1d"),
                data_source="db",
                as_of=datetime.now(),
                is_partial=False,
                last_persisted_bar_time=None,
                last_live_bar_time=None,
                freshness_seconds=0.0,
                degraded=False,
                degraded_reason=None,
            )

    monkeypatch.setattr(indicator_service, "MarketDataAggregationService", _MockAggService)

    async def mock_query_15min(session, instrument_id, start, end):
        return _build_bars("15m")

    async def mock_query_minute(session, instrument_id, start, end):
        # 监控策略仅需要 2 根 1 分钟线
        return _build_bars("15m", length=2)

    async def mock_query_60min(session, instrument_id, start, end):
        return _build_bars("1h")

    async def mock_fetch_weekly(session, instrument_id, start, end):
        return _build_bars("1w")

    async def mock_fetch_monthly(session, instrument_id, start, end):
        return _build_bars("1mo")

    monkeypatch.setattr(indicator_service, "_query_15min_bars", mock_query_15min)
    monkeypatch.setattr(indicator_service, "_query_minute_bars", mock_query_minute)
    monkeypatch.setattr(indicator_service, "_query_60min_bars", mock_query_60min)
    monkeypatch.setattr(indicator_service, "fetch_weekly_bars", mock_fetch_weekly)
    monkeypatch.setattr(indicator_service, "fetch_monthly_bars", mock_fetch_monthly)


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空策略注册表，避免测试中真实加载策略。"""
    monkeypatch.setattr(indicator_service.StrategyLoader, "_registry", {})


@pytest.mark.parametrize("timeframe", ["15m", "1h", "1d", "1w", "1mo"])
async def test_macd_time_matches_timeframe(
    mock_session: AsyncMock,
    mock_bars: None,
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
    mock_bars: None,
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


# ===== SubTask 1.3: 日线行情来自 MarketDataAggregationService（不再通过 Exchange） =====


async def test_daily_bars_from_load_chart_bars_not_exchange(
    mock_session: AsyncMock,
    empty_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日线行情来自 MarketDataAggregationService，不再调用 get_exchange/Exchange.klines。"""
    from datetime import datetime

    from app.services.market_data_aggregation_service import BarAggregationResult

    get_bars_called = False

    class _MockService:
        async def get_bars(self, session, instrument_id, timeframe="1d", adj="qfq", **kwargs):
            nonlocal get_bars_called
            get_bars_called = True
            return BarAggregationResult(
                bars=_build_bars("1d"),
                data_source="db",
                as_of=datetime.now(),
                is_partial=False,
                last_persisted_bar_time=None,
                last_live_bar_time=None,
                freshness_seconds=0.0,
                degraded=False,
                degraded_reason=None,
            )

    monkeypatch.setattr(indicator_service, "MarketDataAggregationService", _MockService)

    # mock DB 查询（避免连真实 DB）
    async def mock_query_15min(session, instrument_id, start, end):
        return _build_bars("15m")

    async def mock_query_minute(session, instrument_id, start, end):
        return _build_bars("15m", length=2)

    monkeypatch.setattr(indicator_service, "_query_15min_bars", mock_query_15min)
    monkeypatch.setattr(indicator_service, "_query_minute_bars", mock_query_minute)

    # [SubTask 1.3] 验证 Exchange 链路已删除：
    # 若模块已无 get_exchange 属性，说明 import 已被删除（理想状态）；
    # 若仍存在（例如未来误引入），用 fail_get_exchange 替换以捕获实际调用
    def fail_get_exchange(*args, **kwargs):
        raise AssertionError("不应再调用 get_exchange（SubTask 1.3 要求删除 Exchange 链路）")

    if hasattr(indicator_service, "get_exchange"):
        monkeypatch.setattr(indicator_service, "get_exchange", fail_get_exchange)
    else:
        # 模块未导入 get_exchange，Exchange 链路已从 import 层面删除
        pass

    await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "1d", "none", bars=250,
    )

    assert get_bars_called, "应调用 MarketDataAggregationService.get_bars 获取日线行情"
    # 双重验证：模块层面不应再依赖 get_exchange
    assert not hasattr(indicator_service, "get_exchange"), \
        "indicator_service 不应再 import get_exchange"


# ===== SubTask 1.4: source_bar_times / source_bar_hash =====


async def test_source_bar_times_in_response(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
) -> None:
    """响应包含 source_bar_times（与 daily_bars 长度一致，ISO 日期字符串）。"""
    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "1d", "none", bars=250,
    )
    assert "source_bar_times" in result, "响应应包含 source_bar_times"
    assert isinstance(result["source_bar_times"], list)
    # 长度与 daily_bars 一致
    expected_bars = _build_bars("1d")
    assert len(result["source_bar_times"]) == len(expected_bars)
    # 元素为 ISO 日期字符串（YYYY-MM-DD）
    for t in result["source_bar_times"]:
        assert isinstance(t, str)
        assert len(t) == 10, f"source_bar_times 元素应为 YYYY-MM-DD 格式: {t}"


async def test_source_bar_hash_in_response(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
) -> None:
    """响应包含 source_bar_hash（16 字符 hex 字符串）。"""
    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "1d", "none", bars=250,
    )
    assert "source_bar_hash" in result, "响应应包含 source_bar_hash"
    assert isinstance(result["source_bar_hash"], str)
    assert len(result["source_bar_hash"]) == 16, "source_bar_hash 应为 16 字符"
    int(result["source_bar_hash"], 16)  # 验证为 hex


async def test_source_bar_hash_consistent_with_chart_bars_service(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
) -> None:
    """indicator_service 的 source_bar_hash 与 chart_bars_service 计算一致。

    确保 /bars API 与 indicator_service 使用相同的 hash 计算逻辑（SSOT）。
    """
    from app.services.chart_bars_service import compute_source_bar_hash

    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "1d", "none", bars=250,
    )
    expected_bars = _build_bars("1d")
    expected_hash = compute_source_bar_hash(expected_bars)
    assert result["source_bar_hash"] == expected_hash, (
        f"source_bar_hash 应与 chart_bars_service 计算一致: "
        f"expected={expected_hash}, actual={result['source_bar_hash']}"
    )


async def test_source_bar_times_15m_includes_time(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
) -> None:
    """15m source_bar_times 元素含时间（YYYY-MM-DDTHH:MM:SS），与 /bars API trade_time 一致。

    修复根因：之前 source_bar_times 永远用 daily_bars，15m 图表上
    source_bar_times 是日线日期格式，与 15m K线时间不匹配，前端必然 mismatch。
    """
    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "15m", "none", bars=50,
    )
    assert "source_bar_times" in result
    times = result["source_bar_times"]
    assert len(times) > 0
    for t in times:
        # 15m 格式：YYYY-MM-DDTHH:MM:SS（19 字符）
        assert len(t) == 19, f"15m source_bar_times 应含时间: {t}"
        assert "T" in t, f"15m source_bar_times 应含 T 分隔符: {t}"


async def test_source_bar_hash_15m_consistent_with_chart_bars_service(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
) -> None:
    """15m source_bar_hash 与 chart_bars_service 用 macd_bars + timeframe='15m' 计算一致。"""
    from app.services.chart_bars_service import compute_source_bar_hash

    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "15m", "none", bars=50,
    )
    expected_bars = _build_bars("15m")
    expected_hash = compute_source_bar_hash(expected_bars, timeframe="15m")
    assert result["source_bar_hash"] == expected_hash, (
        f"15m source_bar_hash 应与 chart_bars_service(timeframe='15m') 一致: "
        f"expected={expected_hash}, actual={result['source_bar_hash']}"
    )


async def test_source_bar_times_1d_still_date_only(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
) -> None:
    """1d source_bar_times 仍为 YYYY-MM-DD 格式（向后兼容）。"""
    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "1d", "none", bars=250,
    )
    for t in result["source_bar_times"]:
        assert len(t) == 10, f"1d source_bar_times 应为 YYYY-MM-DD: {t}"


# ===== SubTask 1.3: 策略返回 time 时不被覆盖 =====


async def test_strategy_time_not_overridden(
    mock_session: AsyncMock,
    mock_bars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """策略返回 time 时，indicator_service 不再用 daily_time_list 覆盖。

    场景：DSA 策略将在 Task 3 返回自身精确 time 数组，
    indicator_service 必须保留策略返回的 time，不再强制覆盖。
    """
    custom_time = ["2026-01-01", "2026-01-02", "2026-01-03"]
    custom_indicators = {"time": custom_time, "value": [1.0, 2.0, 3.0]}

    # Mock StrategyLoader._registry 包含一个 mock 策略
    monkeypatch.setattr(
        indicator_service.StrategyLoader, "_registry", {"mock_strategy": None}
    )

    # Mock StrategyBatchService._get_latest_released_version
    mock_version = MagicMock()
    mock_version.manifest = {"chart_layers": [], "display_name": "Mock"}

    mock_batch_service = MagicMock()
    mock_batch_service._get_latest_released_version = AsyncMock(
        return_value=(None, mock_version)
    )
    monkeypatch.setattr(
        indicator_service, "StrategyBatchService", lambda: mock_batch_service
    )

    # Mock StrategyLoader.load 返回 mock runtime
    mock_runtime = MagicMock()
    mock_runtime.compute_indicators = AsyncMock(return_value=custom_indicators)
    monkeypatch.setattr(
        indicator_service.StrategyLoader, "load", AsyncMock(return_value=mock_runtime)
    )

    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "1d", "none", bars=250,
    )

    # 验证 mock_strategy 的 time 未被覆盖
    assert "mock_strategy" in result["data"], "mock_strategy 应在 data 中"
    assert result["data"]["mock_strategy"]["time"] == custom_time, (
        "策略返回 time 时不应被 daily_time_list 覆盖"
    )


# ===== 既有 helper 函数测试（不变） =====


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


# ===== SQZMOM_LB 副图集成测试 =====


@pytest.mark.parametrize("timeframe", ["1d", "15m", "1h", "1w", "1mo"])
async def test_sqzmom_layer_in_response(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
    timeframe: str,
) -> None:
    """[SQZMOM_LB 副图] - 响应应包含 sqzmom_lb layer 和 data。

    验证：
    1. layers 列表包含 layer_id == "sqzmom_lb" 的条目
    2. data["sqzmom_lb"] 包含 sqzmom_val/sqzmom_bcolor/sqzmom_scolor 等字段
    3. time 数组与请求 timeframe 的 bars 时间对齐
    4. 数据长度一致
    """
    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, timeframe, "none", bars=250,
    )

    # 1. 验证 layers 包含 sqzmom_lb
    sqzmom_layer = None
    for layer in result["layers"]:
        if layer.get("layer_id") == "sqzmom_lb":
            sqzmom_layer = layer
            break
    assert sqzmom_layer is not None, "layers 应包含 layer_id='sqzmom_lb'"
    assert sqzmom_layer["renderer"] == "sqzmom"
    assert sqzmom_layer["pane"] == "sqzmom"
    assert "sqzmom_val" in sqzmom_layer["fields"]
    assert "sqzmom_bcolor" in sqzmom_layer["fields"]
    assert "sqzmom_scolor" in sqzmom_layer["fields"]

    # 2. 验证 data["sqzmom_lb"] 存在且包含所有字段
    sqzmom_data = result["data"].get("sqzmom_lb")
    assert sqzmom_data is not None, "data 应包含 sqzmom_lb 键"
    expected_fields = [
        "sqzmom_val", "sqzmom_bcolor", "sqzmom_scolor",
        "sqzmom_sqz_on", "sqzmom_sqz_off", "sqzmom_no_sqz",
        "time",
    ]
    for field in expected_fields:
        assert field in sqzmom_data, f"sqzmom_lb data 应包含字段 {field}"

    # 3. 验证 time 与当前 timeframe bars 对齐
    expected_bars = _build_bars(timeframe)
    expected_last_time = expected_bars.index[-1]
    last_time = pd.Timestamp(sqzmom_data["time"][-1])
    assert last_time == expected_last_time, (
        f"SQZMOM time 最后一个应与 {timeframe} bars 对齐: "
        f"expected={expected_last_time}, actual={last_time}"
    )

    # 4. 验证所有数组长度一致
    n = len(sqzmom_data["time"])
    for field in ["sqzmom_val", "sqzmom_bcolor", "sqzmom_scolor",
                   "sqzmom_sqz_on", "sqzmom_sqz_off", "sqzmom_no_sqz"]:
        assert len(sqzmom_data[field]) == n, (
            f"sqzmom_lb.{field} 长度应与 time 一致: expected={n}, actual={len(sqzmom_data[field])}"
        )


async def test_sqzmom_val_has_valid_values_after_warmup(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
) -> None:
    """[SQZMOM_LB 副图] - warmup 后 sqzmom_val 应有有效数值（None 比例不超过一半）。"""
    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "1d", "none", bars=250,
    )

    sqzmom_data = result["data"]["sqzmom_lb"]
    val_arr = sqzmom_data["sqzmom_val"]
    n = len(val_arr)
    none_count = sum(1 for v in val_arr if v is None)
    # mock 数据有 60 根，warmup 需要 length+lengthKC=40 根
    # 实际返回 60 根（截取到 bars=250 但 mock 只有 60 根），所以至少 20 根应有效
    assert none_count < n, "应至少有一个有效 val 值"
    valid_count = n - none_count
    assert valid_count >= 20, f"warmup 后应有足够有效值，实际 {valid_count}/{n}"


async def test_sqzmom_bcolor_only_contains_valid_colors(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
) -> None:
    """[SQZMOM_LB 副图] - bcolor 只能是 lime/green/red/maroon。"""
    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "1d", "none", bars=250,
    )

    sqzmom_data = result["data"]["sqzmom_lb"]
    valid_colors = {"lime", "green", "red", "maroon"}
    for c in sqzmom_data["sqzmom_bcolor"]:
        assert c in valid_colors, f"bcolor 应是合法颜色，实际 {c}"


async def test_sqzmom_scolor_only_contains_valid_colors(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
) -> None:
    """[SQZMOM_LB 副图] - scolor 只能是 blue/black/gray。"""
    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "1d", "none", bars=250,
    )

    sqzmom_data = result["data"]["sqzmom_lb"]
    valid_colors = {"blue", "black", "gray"}
    for c in sqzmom_data["sqzmom_scolor"]:
        assert c in valid_colors, f"scolor 应是合法颜色，实际 {c}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
