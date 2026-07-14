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


# ===== PR #32: DSA 全周期 + BB 全周期 =====


async def test_indicator_time_injected_from_macd_bars_in_15m(
    mock_session: AsyncMock,
    mock_bars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[PR #32] - 15m 下策略未返回 time 时，注入的 time 应来自 macd_bars（当前 tf），非 daily_bars。

    修复根因：之前 bars_daily=daily_bars 且 daily_time_list 用 daily_bars.index，
    导致 DSA 在 15m 下收到日线 bars 且 time 是日线格式，与 15m K线不对齐。
    新行为：bars_daily=macd_bars，daily_time_list 用 macd_bars.index，
    DSA 在 15m 下用 15m bars 计算且 time 与 15m K线对齐。
    """
    # Mock StrategyLoader._registry 包含一个不返回 time 的 mock 策略
    monkeypatch.setattr(
        indicator_service.StrategyLoader, "_registry", {"mock_strategy": None}
    )

    mock_version = MagicMock()
    mock_version.manifest = {"chart_layers": [], "display_name": "Mock"}

    mock_batch_service = MagicMock()
    mock_batch_service._get_latest_released_version = AsyncMock(
        return_value=(None, mock_version)
    )
    monkeypatch.setattr(
        indicator_service, "StrategyBatchService", lambda: mock_batch_service
    )

    # mock runtime 返回不带 time 的 indicators
    mock_runtime = MagicMock()
    mock_runtime.compute_indicators = AsyncMock(
        return_value={"value": [1.0, 2.0, 3.0]}  # 无 time 字段
    )
    monkeypatch.setattr(
        indicator_service.StrategyLoader, "load", AsyncMock(return_value=mock_runtime)
    )

    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "15m", "none", bars=50,
    )

    # 验证注入的 time 来自 macd_bars（15m 格式，含时间，非日线 YYYY-MM-DD）
    injected_time = result["data"]["mock_strategy"]["time"]
    assert len(injected_time) > 0, "应注入非空 time 数组"
    assert "T" in injected_time[0], (
        f"15m time 应含 T 分隔符（来自 macd_bars）: {injected_time[0]}"
    )
    # 不应是日线格式（YYYY-MM-DD，长度 10）
    assert len(injected_time[0]) > 10, (
        f"15m time 应含时间（非日线 YYYY-MM-DD）: {injected_time[0]}"
    )


async def test_dsa_context_bars_daily_uses_macd_bars_in_15m(
    mock_session: AsyncMock,
    mock_bars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[PR #32] - 15m 下传给策略的 context.bars_daily 应是 macd_bars（当前 tf），非 daily_bars。

    验证 DSA 在 15m 下用 15m bars 计算（通过捕获 MarketDataContext）。
    """
    monkeypatch.setattr(
        indicator_service.StrategyLoader, "_registry", {"mock_strategy": None}
    )

    mock_version = MagicMock()
    mock_version.manifest = {"chart_layers": [], "display_name": "Mock"}

    mock_batch_service = MagicMock()
    mock_batch_service._get_latest_released_version = AsyncMock(
        return_value=(None, mock_version)
    )
    monkeypatch.setattr(
        indicator_service, "StrategyBatchService", lambda: mock_batch_service
    )

    # 捕获传给策略的 context
    captured_contexts: list = []
    mock_runtime = MagicMock()
    async def _capture(context):
        captured_contexts.append(context)
        return {"value": [1.0]}
    mock_runtime.compute_indicators = _capture
    monkeypatch.setattr(
        indicator_service.StrategyLoader, "load", AsyncMock(return_value=mock_runtime)
    )

    await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "15m", "none", bars=50,
    )

    assert len(captured_contexts) > 0, "应至少调用一次策略"
    ctx = captured_contexts[0]
    # bars_daily 应是 15m bars（macd_bars），长度与 15m bars 一致
    expected_15m_bars = _build_bars("15m")
    assert len(ctx.bars_daily) == len(expected_15m_bars), (
        f"15m 下 context.bars_daily 长度应等于 15m bars({len(expected_15m_bars)}), "
        f"实际={len(ctx.bars_daily)}"
    )
    # bars_daily 时间应为 15m 格式（含 HH:MM:SS），非日线日期 00:00:00
    first_time = ctx.bars_daily.index[0]
    assert hasattr(first_time, 'hour'), (
        f"15m bars_daily 应是 DatetimeIndex 含时间: {first_time}"
    )
    # 15m 第一个 bar 是 09:30（hour=9），daily 第一个 bar 是 00:00（hour=0）
    assert first_time.hour == 9, (
        f"15m bars_daily 第一个时间应为 09:30（hour=9），实际: {first_time}（hour={first_time.hour}）"
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


def test_adapt_watchlist_bb_15m_uses_macd_bars_not_daily_staircase() -> None:
    """[PR #31] - 15m BB 必须用 macd_bars 计算，不再映射日线阶梯线。

    修复根因：之前 _adapt_watchlist_bb 在 15m 路径下调用 _map_daily_to_intraday，
    把日线 BB 映射到 15m 时间轴，导致 15m BB 全部相同（阶梯线），
    不是真正的 15m 周期 BB。

    新行为：15m/1h 用 macd_bars 重新计算 BB（length=20, mult=2.0），
    bb_upper/bb_mid/bb_lower 反映 15m close 的波动，而非日线 BB 阶梯线。
    """
    # 日线 BB（watchlist_monitor 返回，长度=3）
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
    # 25 根 15m bars（close 逐根变化，足够计算 BB length=20）
    macd_times = pd.date_range("2026-06-02 09:30", periods=25, freq="15min").astype(str).tolist()
    closes = [10.0 + i * 0.05 for i in range(25)]  # 10.00 → 11.20 递增
    macd_bars = pd.DataFrame({
        "open": closes,
        "high": [c + 0.1 for c in closes],
        "low": [c - 0.1 for c in closes],
        "close": closes,
        "volume": [100000] * 25,
    }, index=pd.to_datetime(macd_times))

    result = indicator_service._adapt_watchlist_bb(
        indicators, "15m", macd_bars, macd_times, daily_time_list,
    )

    # 1. time 用 macd_time_list（与 15m bars 对齐）
    assert result["time"] == macd_times, "15m BB time 应与 macd_time_list 一致"

    # 2. bb_upper 长度 == macd_bars 长度（25）
    assert len(result["bb_upper"]) == len(macd_times), (
        f"15m bb_upper 长度应等于 macd_bars 长度({len(macd_times)}), "
        f"实际={len(result['bb_upper'])}"
    )

    # 3. bb_upper 不是全部相同（非日线阶梯线）
    bbu = result["bb_upper"]
    last_5 = bbu[-5:]
    unique_last_5 = set(last_5)
    assert len(unique_last_5) > 1, (
        f"15m bb_upper 最后 5 根不应全部相同（非日线阶梯线）: {last_5}"
    )

    # 4. bb_upper 最后一个值 != 日线 BB 最后一个值（12.0）
    #    应该是基于 15m close 计算的 BB upper
    assert bbu[-1] != 12.0, (
        f"15m bb_upper 最后值不应等于日线 BB 最后值(12.0): {bbu[-1]}"
    )

    # 5. bb_mid / bb_lower 也应基于 15m bars 计算
    assert len(result["bb_mid"]) == len(macd_times)
    assert len(result["bb_lower"]) == len(macd_times)


def test_adapt_watchlist_bb_1h_uses_macd_bars_not_daily_staircase() -> None:
    """[PR #31] - 1h BB 同样用 macd_bars 计算，不映射日线阶梯线。"""
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
    # 25 根 1h bars
    macd_times = pd.date_range("2026-06-02 10:00", periods=25, freq="1h").astype(str).tolist()
    closes = [20.0 + i * 0.1 for i in range(25)]
    macd_bars = pd.DataFrame({
        "open": closes,
        "high": [c + 0.2 for c in closes],
        "low": [c - 0.2 for c in closes],
        "close": closes,
        "volume": [100000] * 25,
    }, index=pd.to_datetime(macd_times))

    result = indicator_service._adapt_watchlist_bb(
        indicators, "1h", macd_bars, macd_times, daily_time_list,
    )

    assert result["time"] == macd_times
    assert len(result["bb_upper"]) == len(macd_times)
    # 1h bb_upper 最后值不应等于日线 BB 最后值（12.0）
    assert result["bb_upper"][-1] != 12.0, (
        f"1h bb_upper 最后值不应等于日线 BB 最后值(12.0): {result['bb_upper'][-1]}"
    )


def test_adapt_watchlist_bb_15m_bb_matches_compute_bollinger() -> None:
    """[PR #31] - 15m BB 与 compute_bollinger(macd_bars) 计算结果一致。

    验证 _adapt_watchlist_bb 内部调用了 compute_bollinger(macd_bars)，
    而不是 _map_daily_to_intraday(daily_bb)。
    """
    from app.strategy_assets.algorithms.features.merged_dsa_atr_rope_bb_factors import (
        compute_bollinger,
    )

    indicators = {
        "bb_upper": [10.0, 11.0, 12.0],
        "bb_mid": [9.0, 10.0, 11.0],
        "bb_lower": [8.0, 9.0, 10.0],
    }
    daily_time_list = ["2026-06-01", "2026-06-02", "2026-06-03"]
    macd_times = pd.date_range("2026-06-02 09:30", periods=25, freq="15min").astype(str).tolist()
    closes = [10.0 + i * 0.05 for i in range(25)]
    macd_bars = pd.DataFrame({
        "open": closes,
        "high": [c + 0.1 for c in closes],
        "low": [c - 0.1 for c in closes],
        "close": closes,
        "volume": [100000] * 25,
    }, index=pd.to_datetime(macd_times))

    result = indicator_service._adapt_watchlist_bb(
        indicators, "15m", macd_bars, macd_times, daily_time_list,
    )

    # 用 compute_bollinger 直接计算期望值
    expected = compute_bollinger(macd_bars, length=20, mult=2.0)
    expected_upper = expected["bb_upper"].tolist()

    # 最后一个非 NaN 值应与 _adapt_watchlist_bb 返回的最后值一致
    last_valid_pos = expected["bb_upper"].reset_index(drop=True).last_valid_index()
    expected_last = expected_upper[last_valid_pos]
    actual_last = result["bb_upper"][-1]
    assert abs(actual_last - expected_last) < 1e-6, (
        f"15m bb_upper 最后值应与 compute_bollinger 一致: "
        f"expected={expected_last}, actual={actual_last}"
    )


def test_adapt_watchlist_bb_1w_uses_macd_bars_not_removed() -> None:
    """[PR #32] - 1w BB 必须用 macd_bars 计算，不再移除 BB 字段。

    修复根因：之前 _adapt_watchlist_bb 在 1w/1mo 路径直接 pop BB 字段，
    导致前端 1w/1mo 图表无 BB overlay。新行为：1w/1mo 用 compute_bollinger(macd_bars)
    重新计算 BB，与 15m/1h 路径一致。
    """
    indicators = {
        "bb_upper": [10.0, 11.0, 12.0],
        "bb_mid": [9.0, 10.0, 11.0],
        "bb_lower": [8.0, 9.0, 10.0],
    }
    # 25 根 1w bars（close 逐根变化，足够计算 BB length=20）
    macd_times = pd.date_range("2026-01-05", periods=25, freq="W-MON").astype(str).tolist()
    closes = [10.0 + i * 0.2 for i in range(25)]
    macd_bars = pd.DataFrame({
        "open": closes,
        "high": [c + 0.3 for c in closes],
        "low": [c - 0.3 for c in closes],
        "close": closes,
        "volume": [1000000] * 25,
    }, index=pd.to_datetime(macd_times))

    result = indicator_service._adapt_watchlist_bb(
        indicators, "1w", macd_bars, macd_times, [],
    )

    # 1. BB 字段不被移除
    assert "bb_upper" in result, "1w bb_upper 不应被移除"
    assert "bb_mid" in result, "1w bb_mid 不应被移除"
    assert "bb_lower" in result, "1w bb_lower 不应被移除"

    # 2. time 用 macd_time_list（与 1w bars 对齐）
    assert result["time"] == macd_times, "1w BB time 应与 macd_time_list 一致"

    # 3. bb_upper 长度 == macd_bars 长度（25）
    assert len(result["bb_upper"]) == len(macd_times), (
        f"1w bb_upper 长度应等于 macd_bars 长度({len(macd_times)}), "
        f"实际={len(result['bb_upper'])}"
    )

    # 4. bb_upper 最后值 != 日线 BB 最后值（12.0），应该是基于 1w close 计算
    assert result["bb_upper"][-1] != 12.0, (
        f"1w bb_upper 最后值不应等于日线 BB 最后值(12.0): {result['bb_upper'][-1]}"
    )


def test_adapt_watchlist_bb_1mo_uses_macd_bars_not_removed() -> None:
    """[PR #32] - 1mo BB 同样用 macd_bars 计算，不移除。"""
    indicators = {
        "bb_upper": [10.0, 11.0, 12.0],
        "bb_mid": [9.0, 10.0, 11.0],
        "bb_lower": [8.0, 9.0, 10.0],
    }
    macd_times = pd.date_range("2024-01-01", periods=25, freq="MS").astype(str).tolist()
    closes = [10.0 + i * 0.5 for i in range(25)]
    macd_bars = pd.DataFrame({
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [5000000] * 25,
    }, index=pd.to_datetime(macd_times))

    result = indicator_service._adapt_watchlist_bb(
        indicators, "1mo", macd_bars, macd_times, [],
    )

    assert "bb_upper" in result, "1mo bb_upper 不应被移除"
    assert "bb_mid" in result, "1mo bb_mid 不应被移除"
    assert "bb_lower" in result, "1mo bb_lower 不应被移除"
    assert result["time"] == macd_times
    assert len(result["bb_upper"]) == len(macd_times)
    assert result["bb_upper"][-1] != 12.0, (
        f"1mo bb_upper 最后值不应等于日线 BB 最后值(12.0): {result['bb_upper'][-1]}"
    )


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


# ============================================================
# [CHANGE-011 SMC] include_smc 按需计算测试
# ============================================================


async def test_smc_not_calculated_when_include_smc_false(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[CHANGE-011] include_smc=False（默认）时不计算 SMC，响应无 smc layer。

    SMC 默认关闭，不消耗 CPU；前端通过 IndicatorToolbar 显式开启。
    """
    # spy: 记录 compute_smc_indicators 是否被调用
    smc_called = False
    original_compute = indicator_service.compute_smc_indicators

    def spy_compute(*args, **kwargs):
        nonlocal smc_called
        smc_called = True
        return original_compute(*args, **kwargs)

    monkeypatch.setattr(indicator_service, "compute_smc_indicators", spy_compute)

    # 默认调用（不传 include_smc → 默认 False）
    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "1d", "none", bars=250,
    )

    # SMC 未被调用
    assert not smc_called, "include_smc=False 时不应调用 compute_smc_indicators"

    # 响应中无 smc layer
    layer_ids = [layer["layer_id"] for layer in result["layers"]]
    assert "smc" not in layer_ids, "include_smc=False 时不应有 smc layer"

    # data 中无 smc 键
    assert "smc" not in result["data"], "include_smc=False 时 data 不应有 smc 键"


async def test_smc_calculated_when_include_smc_true(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
) -> None:
    """[CHANGE-011] include_smc=True 时计算 SMC，响应包含 smc layer。

    SMC 按需计算，输出 BOS/CHoCH/OB/EQH/EQL/trailing。
    """
    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "1d", "none", bars=250,
        include_smc=True,
    )

    # 响应中有 smc layer
    layer_ids = [layer["layer_id"] for layer in result["layers"]]
    assert "smc" in layer_ids, "include_smc=True 时应有 smc layer"

    # smc layer 结构正确
    smc_layer = next(layer for layer in result["layers"] if layer["layer_id"] == "smc")
    assert smc_layer["renderer"] == "smc"
    assert smc_layer["direction_colored"] is True
    assert smc_layer["direction_up_color"] == "#FF4D4F"  # A 股红涨
    assert smc_layer["direction_down_color"] == "#22C55E"  # A 股绿跌

    # data 中有 smc 键，包含必需字段
    assert "smc" in result["data"]
    smc_data = result["data"]["smc"]
    required_fields = {"events", "order_blocks", "equal_highs_lows", "trailing", "pivots", "time"}
    assert required_fields.issubset(set(smc_data.keys())), (
        f"smc data 缺少字段: {required_fields - set(smc_data.keys())}"
    )

    # FVG 不存在于 smc 输出
    for key in smc_data:
        assert "fvg" not in str(key).lower(), f"smc data 不得包含 FVG 键: {key}"


async def test_smc_default_param_is_false(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
) -> None:
    """[CHANGE-011] compute_all_indicators 的 include_smc 参数默认为 False。"""
    import inspect as _inspect

    sig = _inspect.signature(indicator_service.compute_all_indicators)
    param = sig.parameters.get("include_smc")
    assert param is not None, "compute_all_indicators 应有 include_smc 参数"
    assert param.default is False, (
        f"include_smc 默认值应为 False（SMC 默认关闭），实际为 {param.default}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
