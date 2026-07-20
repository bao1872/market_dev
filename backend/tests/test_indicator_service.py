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
    """[图表行情契约] - mock MarketDataAggregationService（行情聚合 SSOT）。

    [CHANGE-20260717-002 SSOT] 全部周期（1d/15m/1m/1h/1w/1mo）通过 MDAS 获取；
    MDAS 内部完成 DB 查询 + Pytdx 兜底 + 复权一次 + 周月聚合。
    所有 mock 数据使用 naive DatetimeIndex（与 DB 查询的实际行为一致）。
    """
    monkeypatch.setattr(
        indicator_service,
        "MarketDataAggregationService",
        _make_mock_mdas(),
    )


def _make_mock_mdas() -> type:
    """构造 mock MarketDataAggregationService，get_bars 按 timeframe 返回对应周期 bars。"""
    from datetime import datetime

    from app.services.market_data_aggregation_service import BarAggregationResult

    class _MockAggService:
        async def get_bars(self, session, instrument_id, timeframe="1d", adj="qfq", **kwargs):
            if timeframe == "1m":
                # minute: 监控策略仅需要 2 根 1 分钟线（用 15m format 构造）
                bars = _build_bars("15m", length=2)
            else:
                bars = _build_bars(timeframe)
            return BarAggregationResult(
                bars=bars,
                data_source="db",
                as_of=datetime.now(),
                is_partial=False,
                last_persisted_bar_time=None,
                last_live_bar_time=None,
                freshness_seconds=0.0,
                degraded=False,
                degraded_reason=None,
            )

    return _MockAggService


def _make_spy_mdas(called_timeframes: list[str]) -> type:
    """构造 spy MarketDataAggregationService，记录 get_bars 调用的 timeframe。

    用于验证特定 timeframe 的 get_bars 是否被调用（替代旧的私有函数 spy）。
    """
    from datetime import datetime

    from app.services.market_data_aggregation_service import BarAggregationResult

    class _SpyMDAS:
        async def get_bars(self, session, instrument_id, timeframe="1d", adj="qfq", **kwargs):
            called_timeframes.append(timeframe)
            if timeframe == "1m":
                bars = _build_bars("15m", length=2)
            else:
                bars = _build_bars(timeframe)
            return BarAggregationResult(
                bars=bars,
                data_source="db",
                as_of=datetime.now(),
                is_partial=False,
                last_persisted_bar_time=None,
                last_live_bar_time=None,
                freshness_seconds=0.0,
                degraded=False,
                degraded_reason=None,
            )

    return _SpyMDAS


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
            if timeframe == "1m":
                bars = _build_bars("15m", length=2)
            else:
                bars = _build_bars(timeframe)
            return BarAggregationResult(
                bars=bars,
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
    # [CHANGE-20260717-002 SSOT] 全部周期通过 MDAS 获取，不再需要 mock 私有查询函数

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


async def test_timeframe_echoed_in_response(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
) -> None:
    """[CHANGE-20260719-003 §四] 响应应 echo timeframe 字段，供前端周期切换乱序丢弃检查。

    前端 useStockResearchData 比对 response.timeframe vs 当前 timeframe，
    不匹配则丢弃旧响应（PROMPT.md §4 "generation 不一致响应丢弃"）。
    """
    for tf in ("1d", "15m", "1h", "1w", "1mo"):
        result = await indicator_service.compute_all_indicators(
            mock_session, TEST_INSTRUMENT_ID, tf, "none", bars=100,
        )
        assert result.get("timeframe") == tf, (
            f"响应应 echo timeframe={tf}, 实际={result.get('timeframe')!r}"
        )


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
    # Mock _get_available_strategy_keys（避免查询数据库）
    monkeypatch.setattr(
        indicator_service,
        "_get_available_strategy_keys",
        AsyncMock(return_value=set()),
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
    # Mock _get_available_strategy_keys（避免查询数据库）
    monkeypatch.setattr(
        indicator_service,
        "_get_available_strategy_keys",
        AsyncMock(return_value=set()),
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


async def test_dsa_context_bars_display_uses_macd_bars_and_bars_daily_uses_daily_in_15m(
    mock_session: AsyncMock,
    mock_bars: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[CHANGE-20260720-001] - 15m 下 context.bars_display 是 macd_bars（当前 tf），bars_daily 是真正日线。

    [PR #32] 之前 bars_daily=macd_bars（15m bars），导致 Node/BB 日线结构算法收到非日线数据。
    修复后严格分离：
    - bars_display = macd_bars（15m），供 DSA/MACD/SQZMOM 等当前周期图层使用；
    - bars_daily = daily_bars（真正日线），供 Node/BB/SMC 日线结构算法使用；
    - display_timeframe = "15m"。
    """
    monkeypatch.setattr(
        indicator_service.StrategyLoader, "_registry", {"mock_strategy": None}
    )
    # Mock _get_available_strategy_keys（避免查询数据库）
    monkeypatch.setattr(
        indicator_service,
        "_get_available_strategy_keys",
        AsyncMock(return_value=set()),
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
    # display_timeframe 应为 15m
    assert ctx.display_timeframe == "15m", (
        f"15m 请求下 display_timeframe 应为 '15m'，实际: {ctx.display_timeframe!r}"
    )
    # bars_display 应是 15m bars（macd_bars），长度与 15m bars 一致
    expected_15m_bars = _build_bars("15m")
    assert ctx.bars_display is not None, "15m 下 context.bars_display 不应为 None"
    assert len(ctx.bars_display) == len(expected_15m_bars), (
        f"15m 下 context.bars_display 长度应等于 15m bars({len(expected_15m_bars)}), "
        f"实际={len(ctx.bars_display)}"
    )
    # bars_display 时间应为 15m 格式（含 HH:MM:SS）
    display_first_time = ctx.bars_display.index[0]
    assert hasattr(display_first_time, 'hour'), (
        f"15m bars_display 应是 DatetimeIndex 含时间: {display_first_time}"
    )
    # 15m 第一个 bar 是 09:30（hour=9）
    assert display_first_time.hour == 9, (
        f"15m bars_display 第一个时间应为 09:30（hour=9），实际: {display_first_time}（hour={display_first_time.hour}）"
    )
    # bars_daily 应是真正日线（hour=0），长度等于 daily bars
    expected_daily_bars = _build_bars("1d")
    assert len(ctx.bars_daily) == len(expected_daily_bars), (
        f"15m 下 context.bars_daily 长度应等于 daily bars({len(expected_daily_bars)}), "
        f"实际={len(ctx.bars_daily)}"
    )
    daily_first_time = ctx.bars_daily.index[0]
    assert hasattr(daily_first_time, 'hour'), (
        f"daily bars_daily 应是 DatetimeIndex: {daily_first_time}"
    )
    assert daily_first_time.hour == 0, (
        f"15m 下 context.bars_daily 第一个时间应为日线 00:00（hour=0），"
        f"实际: {daily_first_time}（hour={daily_first_time.hour}）"
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
    # CHANGE-20260715-007: 新增 swing_bias + view（adapter 元信息）
    required_fields = {
        "events", "order_blocks", "equal_highs_lows", "trailing",
        "swing_bias", "pivots", "time", "view",
    }
    assert required_fields.issubset(set(smc_data.keys())), (
        f"smc data 缺少字段: {required_fields - set(smc_data.keys())}"
    )

    # CHANGE-20260715-007: view 必须包含窗口元信息
    view = smc_data["view"]
    for view_key in ("total_bars", "display_bars", "offset", "window_start", "window_end"):
        assert view_key in view, f"smc view 缺少 {view_key}"

    # CHANGE-20260715-007: swing_bias 必须为合法值（1/-1/0）
    valid_biases = {1, -1, 0}
    assert smc_data["swing_bias"] in valid_biases, (
        f"swing_bias 应为 {valid_biases} 之一，实得: {smc_data['swing_bias']}"
    )
    assert isinstance(smc_data["swing_bias"], int), (
        f"swing_bias 必须为 int 类型，实得 {type(smc_data['swing_bias']).__name__}"
    )

    # CHANGE-20260715-007: time 数组长度必须 <= display_bars（响应大小与 bars 同阶）
    assert len(smc_data["time"]) <= smc_data["view"]["display_bars"], (
        f"SMC time 数组长度 {len(smc_data['time'])} 不得超过 display_bars "
        f"{smc_data['view']['display_bars']}"
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


# ============================================================
# [CHANGE-20260716-001 required_inputs] 基于实际可用策略加载日内数据
# 修复根因：静态 _registry 包含 volume_node_monitor/bb_monitor，但数据库无定义，
# 导致旧逻辑错误地纳入 15min/minute，1d 请求仍执行 2 条不必要的查询。
# ============================================================


def test_determine_required_bars_only_daily_when_empty() -> None:
    """空策略集合只返回 daily（MACD/DSA 等基础指标始终需要日线）。"""
    result = indicator_service._determine_required_bars(set())
    assert result == frozenset({"daily"}), f"空策略应只返回 daily，实得 {result}"


def test_determine_required_bars_includes_15min_minute_when_vp_available() -> None:
    """VP 策略可用时包含 15min/minute（VP profile + crossover）。"""
    result = indicator_service._determine_required_bars({"volume_node_monitor"})
    assert "15min" in result, "VP 可用时应包含 15min"
    assert "minute" in result, "VP 可用时应包含 minute"
    assert "daily" in result, "VP 可用时应包含 daily"


def test_determine_required_bars_includes_15min_excludes_minute_when_vp_unavailable() -> None:
    """[CHANGE-20260720-001] WATCHLIST_MONITOR 现在声明 15min（内部含 VolumeNodeMonitor）。

    VP（volume_node_monitor）不可用时（只有 dsa_selector + watchlist_monitor）：
    - daily 始终需要（MACD/DSA 基础指标）
    - 15min 需要（WATCHLIST_MONITOR 内部 VolumeNodeMonitor 用 15m 做 VP profile）
    - minute 不需要（VP 不可用，无 crossover 检测）
    """
    result = indicator_service._determine_required_bars({"dsa_selector", "watchlist_monitor"})
    assert "daily" in result, "应包含 daily"
    assert "15min" in result, (
        "WATCHLIST_MONITOR 声明 15min（内部含 VolumeNodeMonitor），应包含 15min"
    )
    assert "minute" not in result, "VP 不可用时应不包含 minute"
    assert result == frozenset({"daily", "15min"}), (
        f"应返回 daily+15min（WATCHLIST_MONITOR 声明），实得 {result}"
    )


async def test_1d_skips_15min_minute_queries_when_vp_unavailable(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[CHANGE-20260716-001] 1d 请求在 VP 策略不可用时不查询 15min/minute。

    修复根因：静态 _registry 包含 volume_node_monitor，但数据库无定义，
    导致旧逻辑错误地加载 15min/minute 数据。
    新逻辑：基于数据库实际可用策略（有 released version）计算 required_bars。
    """
    # Mock _get_available_strategy_keys 返回空集合（模拟 VP 不可用）
    monkeypatch.setattr(
        indicator_service,
        "_get_available_strategy_keys",
        AsyncMock(return_value=set()),
    )

    # [CHANGE-20260717-002 SSOT] 全部周期通过 MDAS 获取，spy MDAS.get_bars 调用的 timeframe
    called_timeframes: list[str] = []
    monkeypatch.setattr(
        indicator_service,
        "MarketDataAggregationService",
        _make_spy_mdas(called_timeframes),
    )

    await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "1d", "none", bars=250,
    )

    assert "15m" not in called_timeframes, (
        "1d 请求在 VP 不可用时应跳过 15min 查询"
    )
    assert "1m" not in called_timeframes, (
        "1d 请求在 VP 不可用时应跳过 minute 查询"
    )


async def test_1d_loads_15min_minute_queries_when_vp_available(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[CHANGE-20260716-001] 1d 请求在 VP 策略可用时查询 15min/minute。"""
    # Mock _get_available_strategy_keys 返回 volume_node_monitor（模拟 VP 可用）
    monkeypatch.setattr(
        indicator_service,
        "_get_available_strategy_keys",
        AsyncMock(return_value={"volume_node_monitor"}),
    )

    # [CHANGE-20260717-002 SSOT] 全部周期通过 MDAS 获取，spy MDAS.get_bars 调用的 timeframe
    called_timeframes: list[str] = []
    monkeypatch.setattr(
        indicator_service,
        "MarketDataAggregationService",
        _make_spy_mdas(called_timeframes),
    )

    await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "1d", "none", bars=250,
    )

    assert "15m" in called_timeframes, (
        "1d 请求在 VP 可用时应加载 15min 数据（VP profile）"
    )
    assert "1m" in called_timeframes, (
        "1d 请求在 VP 可用时应加载 minute 数据（VP crossover）"
    )


async def test_15m_always_loads_15min_regardless_of_vp(
    mock_session: AsyncMock,
    mock_bars: None,
    empty_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[CHANGE-20260716-001] 15m 请求始终加载 15min 数据（macd_bars 用途），独立于策略 registry。"""
    # Mock _get_available_strategy_keys 返回空集合（模拟 VP 不可用）
    monkeypatch.setattr(
        indicator_service,
        "_get_available_strategy_keys",
        AsyncMock(return_value=set()),
    )

    # [CHANGE-20260717-002 SSOT] 全部周期通过 MDAS 获取，spy MDAS.get_bars 调用的 timeframe
    called_timeframes: list[str] = []
    monkeypatch.setattr(
        indicator_service,
        "MarketDataAggregationService",
        _make_spy_mdas(called_timeframes),
    )

    await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, "15m", "none", bars=50,
    )

    assert "15m" in called_timeframes, (
        "15m 请求应始终加载 15min 数据（macd_bars 用途），独立于策略 registry"
    )


# ============================================================
# [CHANGE-20260720-001] 五周期 Node profile_hash 一致性测试
# Node Cluster 固定使用 daily_bars + bars_15min（不依赖显示周期），
# 五周期（1d/15m/1h/1w/1mo）切换时 profile_hash 必须一致。
# ============================================================


def _make_overlapping_mock_mdas() -> type:
    """构造 mock MDAS，daily/15m bars 使用重叠日期范围（满足 Node Cluster 覆盖率约束）。

    - daily: 260 根日线，end_date=2026-06-18
    - 15m: 4100 根 15m bars，end_date=2026-06-18 15:00（覆盖 daily 最后若干日）
    - 1m/1h/1w/1mo: 复用 _build_bars 默认构造（仅用于显示周期，Node 不读取）
    """
    from datetime import datetime

    import numpy as np

    from app.services.market_data_aggregation_service import BarAggregationResult

    # 复用 test_node_cluster_engine.py 的构造方式，确保 daily/15m 时间范围重叠
    def _make_daily_bars(n: int = 260, end_date: str = "2026-06-18") -> pd.DataFrame:
        np.random.seed(43)
        dates = pd.date_range(end=end_date, periods=n, freq="B")
        price_low, price_high = 9.0, 15.0
        span = price_high - price_low
        mid = (price_low + price_high) / 2
        returns = np.random.uniform(-0.01, 0.01, size=n)
        close = mid * np.cumprod(1 + returns)
        close = np.clip(close, price_low + span * 0.1, price_high - span * 0.1)
        open_ = close * (1 + np.random.uniform(-0.005, 0.005, size=n))
        high = np.maximum(open_, close) * (1 + np.random.uniform(0.002, 0.01, size=n))
        low = np.minimum(open_, close) * (1 - np.random.uniform(0.002, 0.01, size=n))
        high = np.maximum(high, price_high - span * 0.05)
        low = np.minimum(low, price_low + span * 0.05)
        volume = np.random.uniform(1_000_000, 5_000_000, size=n)
        amount = volume * close
        df = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close,
             "volume": volume, "amount": amount, "adj_factor": [1.0] * n},
            index=dates,
        )
        df.index.name = "datetime"
        return df

    def _make_15m_bars(n_total: int = 4100, end_date: str = "2026-06-18 15:00") -> pd.DataFrame:
        np.random.seed(7)
        dates = pd.date_range(end=end_date, periods=n_total, freq="15min")
        # 簇：3 个价格簇 + 低量填充
        clusters = [(10.0, 200_000.0, 0.3), (12.0, 200_000.0, 0.3), (14.0, 200_000.0, 0.3)]
        parts: list[pd.DataFrame] = []
        consumed = 0
        for price, vol, frac in clusters:
            n_cluster = int(n_total * frac)
            idx = dates[consumed:consumed + n_cluster]
            consumed += n_cluster
            jitter = price * 0.0005
            close = price + np.random.uniform(-jitter, jitter, size=n_cluster)
            parts.append(pd.DataFrame(
                {"open": close, "high": close + jitter, "low": close - jitter,
                 "close": close, "volume": np.full(n_cluster, vol, dtype=float),
                 "amount": close * vol, "adj_factor": [1.0] * n_cluster},
                index=idx,
            ))
        remaining = n_total - consumed
        if remaining > 0:
            idx = dates[consumed:]
            close = np.full(remaining, clusters[0][0], dtype=float)
            parts.append(pd.DataFrame(
                {"open": close, "high": close, "low": close, "close": close,
                 "volume": np.full(remaining, 1000.0), "amount": close * 1000.0,
                 "adj_factor": [1.0] * remaining},
                index=idx,
            ))
        df = pd.concat(parts, ignore_index=False)
        df.index.name = "datetime"
        return df

    daily_cache = _make_daily_bars()
    bars_15m_cache = _make_15m_bars()

    class _MockAggService:
        async def get_bars(self, session, instrument_id, timeframe="1d", adj="qfq", **kwargs):
            if timeframe == "1d":
                bars = daily_cache
            elif timeframe == "15m":
                bars = bars_15m_cache
            elif timeframe == "1m":
                # minute: 监控策略仅需要 2 根 1 分钟线（用 15m format 构造）
                bars = _build_bars("15m", length=2)
            else:
                # 1h/1w/1mo: 显示周期用 _build_bars（Node 不读取，仅 macd_bars 使用）
                bars = _build_bars(timeframe)
            return BarAggregationResult(
                bars=bars,
                data_source="db",
                as_of=datetime.now(),
                is_partial=False,
                last_persisted_bar_time=None,
                last_live_bar_time=None,
                freshness_seconds=0.0,
                degraded=False,
                degraded_reason=None,
            )

    return _MockAggService


@pytest.mark.parametrize("timeframe", ["1d", "15m", "1h", "1w", "1mo"])
async def test_node_cluster_profile_hash_consistent_across_timeframes(
    mock_session: AsyncMock,
    empty_registry: None,
    monkeypatch: pytest.MonkeyPatch,
    timeframe: str,
) -> None:
    """[CHANGE-20260720-001] 五周期 Node profile_hash 一致性。

    Node Cluster 固定使用 completed qfq 1d×250 + 15m×4000，不随页面周期变化。
    五周期切换时 data["node_cluster"]["profile_meta"]["profile_hash"] 必须一致。

    实现要点：
    - bars_daily/bars_15min 与显示周期分离（修复前 bars_daily=macd_bars 导致非 1d 周期 Node 不可用）；
    - _compute_independent_node_cluster 只读取 daily_bars + bars_15min，不读取 macd_bars。

    测试环境：
    - 使用 _make_overlapping_mock_mdas 提供 daily(260根) + 15m(4100根) 重叠 bars；
    - WATCHLIST_MONITOR 可用（_get_available_strategy_keys 返回 {"watchlist_monitor"}），
      确保所有 timeframe 下都加载 15min bars。
    """
    # 使用重叠日期的 mock MDAS
    monkeypatch.setattr(
        indicator_service,
        "MarketDataAggregationService",
        _make_overlapping_mock_mdas(),
    )
    # Mock _get_available_strategy_keys 返回 WATCHLIST_MONITOR（确保 15min 被加载）
    monkeypatch.setattr(
        indicator_service,
        "_get_available_strategy_keys",
        AsyncMock(return_value={"watchlist_monitor"}),
    )

    result = await indicator_service.compute_all_indicators(
        mock_session, TEST_INSTRUMENT_ID, timeframe, "none", bars=250,
    )

    assert "node_cluster" in result["data"], (
        f"{timeframe} 下应独立输出 data['node_cluster']"
    )
    node_cluster = result["data"]["node_cluster"]
    assert isinstance(node_cluster, dict), (
        f"{timeframe} 下 node_cluster 应为 dict，实际: {type(node_cluster)}"
    )
    # availability 应为 available（daily_bars 和 15m_bars 都已 mock 提供）
    assert node_cluster["availability"] == "available", (
        f"{timeframe} 下 node_cluster.availability 应为 'available'，"
        f"实际: {node_cluster.get('availability')!r}, "
        f"degraded_reason={node_cluster.get('degraded_reason')!r}"
    )
    assert node_cluster["degraded_reason"] is None, (
        f"{timeframe} 下 node_cluster.degraded_reason 应为 None，"
        f"实际: {node_cluster.get('degraded_reason')!r}"
    )
    profile_meta = node_cluster["profile_meta"]
    assert isinstance(profile_meta, dict), "profile_meta 应为 dict"
    assert "profile_hash" in profile_meta, "profile_meta 应包含 profile_hash"
    assert profile_meta["profile_hash"], (
        f"{timeframe} 下 profile_hash 不应为空: {profile_meta.get('profile_hash')!r}"
    )
    # daily_source_hash 和 bars_15m_source_hash 应存在（验证输入确定性）
    assert profile_meta.get("daily_source_hash"), (
        f"{timeframe} 下 daily_source_hash 不应为空"
    )
    assert profile_meta.get("bars_15m_source_hash"), (
        f"{timeframe} 下 bars_15m_source_hash 不应为空"
    )
    # row_count 应为 100（完整 VP profile）
    assert profile_meta.get("row_count") == 100, (
        f"{timeframe} 下 row_count 应为 100，实际: {profile_meta.get('row_count')}"
    )


async def test_node_cluster_profile_hash_identical_across_all_five_timeframes(
    mock_session: AsyncMock,
    empty_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[CHANGE-20260720-001] 五周期 Node profile_hash 必须完全一致。

    Node Cluster 输入固定为 daily_bars + bars_15min，不依赖显示周期。
    五周期（1d/15m/1h/1w/1mo）下 profile_hash / daily_source_hash / bars_15m_source_hash
    必须完全一致，证明 Node Cluster 独立于显示周期。
    """
    # 使用重叠日期的 mock MDAS
    monkeypatch.setattr(
        indicator_service,
        "MarketDataAggregationService",
        _make_overlapping_mock_mdas(),
    )
    # Mock _get_available_strategy_keys
    monkeypatch.setattr(
        indicator_service,
        "_get_available_strategy_keys",
        AsyncMock(return_value={"watchlist_monitor"}),
    )

    hashes_by_tf: dict[str, dict[str, str]] = {}
    for tf in ("1d", "15m", "1h", "1w", "1mo"):
        result = await indicator_service.compute_all_indicators(
            mock_session, TEST_INSTRUMENT_ID, tf, "none", bars=250,
        )
        node_cluster = result["data"]["node_cluster"]
        meta = node_cluster["profile_meta"]
        hashes_by_tf[tf] = {
            "profile_hash": meta["profile_hash"],
            "daily_source_hash": meta["daily_source_hash"],
            "bars_15m_source_hash": meta["bars_15m_source_hash"],
        }

    # 五周期 profile_hash 必须完全一致
    profile_hashes = {h["profile_hash"] for h in hashes_by_tf.values()}
    assert len(profile_hashes) == 1, (
        f"五周期 profile_hash 必须完全一致，实际: {hashes_by_tf}"
    )
    # 五周期 daily_source_hash 必须完全一致
    daily_hashes = {h["daily_source_hash"] for h in hashes_by_tf.values()}
    assert len(daily_hashes) == 1, (
        f"五周期 daily_source_hash 必须完全一致，实际: {hashes_by_tf}"
    )
    # 五周期 bars_15m_source_hash 必须完全一致
    bars_15m_hashes = {h["bars_15m_source_hash"] for h in hashes_by_tf.values()}
    assert len(bars_15m_hashes) == 1, (
        f"五周期 bars_15m_source_hash 必须完全一致，实际: {hashes_by_tf}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
