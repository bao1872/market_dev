"""VolumeNodeMonitor 监控策略测试 - 验证功能正确性与性能预算。

测试内容：
1. 性能预算验证：单只标的 calculate_state + detect_events 总耗时 < 500ms（MONITOR_BUDGET_MS）
2. 状态输出验证：MonitorState.state 含 manifest.outputs 声明的 6 个字段
3. 事件检测验证：1m bar 收盘价 crossover 穿越 peak_price 触发 node_cluster_touch 事件
4. dedupe 验证：dedupe_key / logical_entity 随 boundary 与 bar_time 变化，用于外层 touch_episode 去重

测试数据：
- 使用合成的日线 bars（≥10 根）作为 Volume Profile 主数据
- 使用合成的 1m bars（360+ 根）满足 VP lookback 要求，并用于 crossover 检测
- 1m bars 价格锚定最近日线收盘价，保持数据语义一致
- 固定随机种子确保测试可复现

参考文档：
- doc/trading_platform_development_docs_v1.1/examples/volume_node_monitor.yaml
- 05_STRATEGY_EXTENSION_SPEC.md
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.strategy.budget import MONITOR_BUDGET_MS
from app.strategy.monitors.volume_node_monitor import (
    EVENT_STATE_TTL_SECONDS,
    EVENT_TYPE_NODE_CLUSTER_TOUCH,
    VolumeNodeMonitor,
)
from app.strategy.runtime import (
    MarketDataContext,
    MonitorState,
    StrategyEventDraft,
)

# compute_indicators 返回的 profile_rows 每行必需字段（与 advice.md 一致）
_PROFILE_ROW_KEYS = {
    "price_low", "price_high", "price_mid",
    "bullish_volume", "bearish_volume", "total_volume",
    "is_peak", "is_poc", "is_value_area",
}
# profile_meta 必需字段（5 个核心 VP 字段 + 6 个 prepare_node_cluster_bars 诊断字段）
_PROFILE_META_KEYS = {
    "row_count", "price_step", "poc_price", "vah_price", "val_price",
    "input_daily_bars", "input_15m_bars", "input_minute_bars",
    "primary_period", "low_period", "parameter_version",
}


def _generate_minute_bars(
    n_bars: int = 400,
    start_price: float = 10.0,
    seed: int = 42,
) -> pd.DataFrame:
    """生成合成的 1m OHLCV bars（满足 VP lookback=360 要求）。

    生成逻辑：
    - 收盘价小幅波动（模拟盘中震荡）
    - 成交量随机但保持合理范围
    - index 为 DatetimeIndex（1 分钟频率）

    Args:
        n_bars: 生成的 bar 数（默认 400，满足 lookback=360）
        start_price: 起始价格
        seed: 随机种子（确保可复现）

    Returns:
        DataFrame: index=DatetimeIndex, columns=open/high/low/close/volume/amount
    """
    np.random.seed(seed)
    # 1m 频率，从 09:30 开始
    dates = pd.date_range(start="2026-06-18 09:30", periods=n_bars, freq="1min")

    # 小幅波动（±0.3%），模拟盘中震荡
    minute_returns = np.random.uniform(-0.003, 0.003, size=n_bars)
    close = start_price * np.cumprod(1 + minute_returns)
    open_ = close * (1 + np.random.uniform(-0.001, 0.001, size=n_bars))
    high = np.maximum(open_, close) * (1 + np.random.uniform(0.0005, 0.003, size=n_bars))
    low = np.minimum(open_, close) * (1 - np.random.uniform(0.0005, 0.003, size=n_bars))
    volume = np.random.uniform(50000, 200000, size=n_bars)
    amount = volume * close

    df = pd.DataFrame({
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": amount,
    }, index=dates)
    df.index.name = "datetime"
    return df


def _generate_daily_bars(
    n_bars: int = 20,
    end_date: str = "2026-06-18",
    start_price: float = 10.0,
    seed: int = 43,
) -> pd.DataFrame:
    """生成合成的日线 OHLCV bars（满足 VolumeNodeMonitor 日线数据要求）。

    VolumeNodeMonitor 以日线 bars 作为主数据计算 Volume Profile，
    需要至少 10 根日线。本 helper 生成 20 根可复现的日线数据，
    覆盖 monitor 的 lookback=360 所需的主数据周期。

    Args:
        n_bars: 生成的日线 bar 数（默认 20）
        end_date: 最后一个交易日的日期字符串
        start_price: 起始价格
        seed: 随机种子（确保可复现）

    Returns:
        DataFrame: index=DatetimeIndex（交易日）, columns=open/high/low/close/volume/amount
    """
    np.random.seed(seed)
    # 使用工作日频率，确保交易日语义
    dates = pd.date_range(end=end_date, periods=n_bars, freq="B")

    # 日线波动（±2%），模拟正常股价波动
    daily_returns = np.random.uniform(-0.02, 0.02, size=n_bars)
    close = start_price * np.cumprod(1 + daily_returns)
    open_ = close * (1 + np.random.uniform(-0.01, 0.01, size=n_bars))
    high = np.maximum(open_, close) * (1 + np.random.uniform(0.005, 0.02, size=n_bars))
    low = np.minimum(open_, close) * (1 - np.random.uniform(0.005, 0.02, size=n_bars))
    volume = np.random.uniform(1_000_000, 5_000_000, size=n_bars)
    amount = volume * close

    df = pd.DataFrame({
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "amount": amount,
    }, index=dates)
    df.index.name = "datetime"
    return df


def _make_mock_version(strategy_id: str = "volume_node_monitor") -> MagicMock:
    """创建 mock StrategyVersion 对象（含 manifest 参数）。"""
    version = MagicMock()
    version.id = uuid.uuid4()
    version.manifest = {
        "strategy_id": strategy_id,
        "kind": "monitor",
        "version": "1.1.0",
        "parameters": [
            {"key": "algorithm.lookback", "type": "integer", "default": 360}
        ],
        "outputs": [
            {"key": "current_price", "type": "number"},
            {"key": "upper_node", "type": "json"},
            {"key": "lower_node", "type": "json"},
            {"key": "position_0_1", "type": "number", "semantic": "ratio_0_1"},
            {"key": "poc_price", "type": "json"},
            {"key": "last_touched_node", "type": "json"},
        ],
        "event_types": [
            {
                "key": "node_cluster_touch",
                "dedupe": "touch_episode",
                "state_ttl_seconds": 120,
            }
        ],
        "resource_budget": {
            "target_ms_per_instrument": MONITOR_BUDGET_MS
        },
    }
    return version


@pytest.fixture
def minute_bars(daily_bars: pd.DataFrame) -> pd.DataFrame:
    """400 根 1m bars（满足 VP lookback=360），价格锚定最近日线收盘价。"""
    start_price = float(daily_bars["close"].iloc[-1])
    return _generate_minute_bars(n_bars=400, start_price=start_price)


@pytest.fixture
def daily_bars() -> pd.DataFrame:
    """20 根日线 bars（满足 VolumeNodeMonitor 日线数据要求）。"""
    return _generate_daily_bars(n_bars=20)


@pytest.fixture
async def monitor() -> VolumeNodeMonitor:
    """已初始化的 VolumeNodeMonitor 实例。"""
    m = VolumeNodeMonitor()
    version = _make_mock_version()
    await m.initialize(version)
    return m


def _make_context(
    bars_daily: pd.DataFrame,
    bars_minute: pd.DataFrame,
    bar_time: datetime | None = None,
) -> MarketDataContext:
    """构建 MarketDataContext（日线 + 1m bars）。"""
    return MarketDataContext(
        instrument_id=uuid.uuid4(),
        symbol="600519",
        bars_daily=bars_daily,
        bars_minute=bars_minute,
        trade_date=bars_minute.index[0].date() if len(bars_minute) > 0 else None,
        bar_time=bar_time or (bars_minute.index[-1].to_pydatetime() if len(bars_minute) > 0 else datetime.now(UTC)),
    )


def _make_minute_bars_crossing_price(
    base_bars: pd.DataFrame,
    crossover_price: float,
    cross_step: float = 1e-4,
) -> pd.DataFrame:
    """复制 1m bars 并修改最后两根 close，使其穿越指定价格。

    修改后倒数第二根 close = crossover_price - cross_step，
    最后一根 close = crossover_price + cross_step，
    从而满足 crossover 条件：prev_close <= crossover_price < cur_close。
    同时同步 high/low 保持 OHLC 合理性。

    Args:
        base_bars: 原始 1m bars
        crossover_price: 需要被穿越的价格
        cross_step: 穿越步长（默认 1e-4，避免同时穿越多个 peak_price）

    Returns:
        修改后的 1m bars 副本
    """
    bars = base_bars.copy()
    loc_close = bars.columns.get_loc("close")
    loc_open = bars.columns.get_loc("open")
    loc_high = bars.columns.get_loc("high")
    loc_low = bars.columns.get_loc("low")

    # 倒数第二根：close 在 crossover_price 下方
    idx_prev = -2
    prev_close = crossover_price - cross_step
    bars.iloc[idx_prev, loc_close] = prev_close
    prev_open = bars.iloc[idx_prev, loc_open]
    bars.iloc[idx_prev, loc_high] = max(prev_open, prev_close) * (1 + cross_step)
    bars.iloc[idx_prev, loc_low] = min(prev_open, prev_close) * (1 - cross_step)

    # 最后一根：close 在 crossover_price 上方
    idx_cur = -1
    cur_close = crossover_price + cross_step
    bars.iloc[idx_cur, loc_close] = cur_close
    cur_open = bars.iloc[idx_cur, loc_open]
    bars.iloc[idx_cur, loc_high] = max(cur_open, cur_close) * (1 + cross_step)
    bars.iloc[idx_cur, loc_low] = min(cur_open, cur_close) * (1 - cross_step)

    return bars


def _make_minute_bars_no_crossover(base_bars: pd.DataFrame) -> pd.DataFrame:
    """复制 1m bars 并将最后一根 close 设为与倒数第二根相同，确保无穿越。

    当 prev_close == cur_close 时，不可能存在 peak_price cp 使得
    prev_close <= cp < cur_close 或 cur_close <= cp < prev_close，
    因此不会产生 crossover 事件。

    Args:
        base_bars: 原始 1m bars

    Returns:
        修改后的 1m bars 副本
    """
    bars = base_bars.copy()
    loc_close = bars.columns.get_loc("close")
    loc_open = bars.columns.get_loc("open")
    loc_high = bars.columns.get_loc("high")
    loc_low = bars.columns.get_loc("low")

    idx_cur = -1
    idx_prev = -2
    cur_close = bars.iloc[idx_prev, loc_close]
    bars.iloc[idx_cur, loc_close] = cur_close
    cur_open = bars.iloc[idx_cur, loc_open]
    bars.iloc[idx_cur, loc_high] = max(cur_open, cur_close) * 1.0001
    bars.iloc[idx_cur, loc_low] = min(cur_open, cur_close) * 0.9999

    return bars


class TestVolumeNodeMonitorPerformance:
    """性能预算测试（Task 16.6）。"""

    @pytest.mark.asyncio
    async def test_single_instrument_under_500ms(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame, minute_bars: pd.DataFrame
    ) -> None:
        """验证单只标的 calculate_state + detect_events 总耗时 < 500ms。

        对照 volume_node_monitor.yaml resource_budget.target_ms_per_instrument=500。
        测量 3 次取最小值（减少噪声），任一次通过即视为达标。
        """
        context = _make_context(daily_bars, minute_bars)
        prev_state: MonitorState | None = None

        # 预热一次（避免首次加载 features 模块的冷启动开销影响判断）
        _ = await monitor.calculate_state(context)

        # 正式测量：3 次取最小值
        elapsed_ms_list: list[float] = []
        for _ in range(3):
            start = time.perf_counter()
            curr_state = await monitor.calculate_state(context)
            _ = await monitor.detect_events(context, prev_state, curr_state)
            elapsed_ms = (time.perf_counter() - start) * 1000
            elapsed_ms_list.append(elapsed_ms)

        min_elapsed_ms = min(elapsed_ms_list)
        print(
            f"单只标的耗时（3 次）: {elapsed_ms_list} ms, "
            f"最小={min_elapsed_ms:.2f}ms, 预算={MONITOR_BUDGET_MS}ms"
        )

        # 验证最小耗时 < 500ms（MONITOR_BUDGET_MS）
        assert min_elapsed_ms < MONITOR_BUDGET_MS, (
            f"单只标的处理耗时 {min_elapsed_ms:.2f}ms 超过预算 {MONITOR_BUDGET_MS}ms"
        )


class TestVolumeNodeMonitorState:
    """状态输出测试（Task 16.4 字段对齐）。"""

    @pytest.mark.asyncio
    async def test_state_contains_all_output_fields(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame, minute_bars: pd.DataFrame
    ) -> None:
        """验证 MonitorState.state 含 manifest.outputs 声明的 6 个字段。"""
        context = _make_context(daily_bars, minute_bars)
        state = await monitor.calculate_state(context)

        expected_fields = {
            "current_price", "upper_node", "lower_node",
            "position_0_1", "poc_price", "last_touched_node",
        }
        actual_fields = set(state.state.keys())
        assert actual_fields == expected_fields, (
            f"状态字段不匹配: 缺失={expected_fields - actual_fields}, "
            f"多余={actual_fields - expected_fields}"
        )

    @pytest.mark.asyncio
    async def test_current_price_is_number(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame, minute_bars: pd.DataFrame
    ) -> None:
        """验证 current_price 为数值类型。"""
        context = _make_context(daily_bars, minute_bars)
        state = await monitor.calculate_state(context)
        assert isinstance(state.state["current_price"], (int, float))
        assert state.state["current_price"] > 0

    @pytest.mark.asyncio
    async def test_position_0_1_in_range(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame, minute_bars: pd.DataFrame
    ) -> None:
        """验证 position_0_1 在 [0, 1] 区间内（ratio_0_1 语义）。"""
        context = _make_context(daily_bars, minute_bars)
        state = await monitor.calculate_state(context)
        position = state.state["position_0_1"]
        assert isinstance(position, (int, float))
        assert 0.0 <= position <= 1.0

    @pytest.mark.asyncio
    async def test_node_json_structure(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame, minute_bars: pd.DataFrame
    ) -> None:
        """验证 node 输出为 json 结构（含 price_mid/price_low/price_high）或 None。"""
        context = _make_context(daily_bars, minute_bars)
        state = await monitor.calculate_state(context)

        for field in ("upper_node", "lower_node", "poc_price", "last_touched_node"):
            node = state.state[field]
            if node is not None:
                assert isinstance(node, dict), f"{field} 应为 dict 或 None，实际 {type(node)}"
                assert "price_mid" in node
                assert "price_low" in node
                assert "price_high" in node
                assert isinstance(node["price_mid"], (int, float))
                assert isinstance(node["price_low"], (int, float))
                assert isinstance(node["price_high"], (int, float))


class TestVolumeNodeMonitorEvents:
    """事件检测与去重测试（Task 16.5）——与 crossover 实现语义一致。"""

    @pytest.mark.asyncio
    async def test_no_event_when_no_crossover(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame, minute_bars: pd.DataFrame
    ) -> None:
        """最后两根 1m bar 无价格穿越时无事件。"""
        no_cross_bars = _make_minute_bars_no_crossover(minute_bars)
        context = _make_context(daily_bars, no_cross_bars)
        curr_state = await monitor.calculate_state(context)

        events = await monitor.detect_events(context, None, curr_state)
        assert events == []

    @pytest.mark.asyncio
    async def test_event_on_price_crossover(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame, minute_bars: pd.DataFrame
    ) -> None:
        """1m bar 收盘价向上穿越 peak_price 时触发 node_cluster_touch 事件。"""
        context = _make_context(daily_bars, minute_bars)
        curr_state = await monitor.calculate_state(context)

        assert monitor._last_vp_result is not None
        peak_prices = monitor._last_vp_result.all_peak_prices
        assert peak_prices, "需要至少一个 peak_price 才能构造穿越"
        crossover_price = float(peak_prices[len(peak_prices) // 2])

        cross_bars = _make_minute_bars_crossing_price(minute_bars, crossover_price)
        cross_context = _make_context(daily_bars, cross_bars)
        cross_state = await monitor.calculate_state(cross_context)

        events = await monitor.detect_events(cross_context, None, cross_state)
        assert len(events) >= 1, "穿越 peak_price 时应至少产生一个事件"
        event = events[0]
        assert isinstance(event, StrategyEventDraft)
        assert event.event_type == EVENT_TYPE_NODE_CLUSTER_TOUCH
        assert event.state_ttl_seconds == EVENT_STATE_TTL_SECONDS
        assert str(cross_state.instrument_id) in event.dedupe_key
        assert event.payload["instrument_id"] == str(cross_state.instrument_id)
        assert event.payload["boundary"] == crossover_price
        assert "cluster_price" in event.payload
        assert "dev_pct" in event.payload

    @pytest.mark.asyncio
    async def test_same_crossover_same_dedupe_key(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame, minute_bars: pd.DataFrame
    ) -> None:
        """同一 boundary + 同一 bar_time 调用两次产生同一 dedupe_key / logical_entity。"""
        context = _make_context(daily_bars, minute_bars)
        curr_state = await monitor.calculate_state(context)

        assert monitor._last_vp_result is not None
        peak_prices = monitor._last_vp_result.all_peak_prices
        assert peak_prices
        crossover_price = float(peak_prices[len(peak_prices) // 2])

        cross_bars = _make_minute_bars_crossing_price(minute_bars, crossover_price)
        cross_context = _make_context(daily_bars, cross_bars)
        cross_state = await monitor.calculate_state(cross_context)

        events1 = await monitor.detect_events(cross_context, None, cross_state)
        events2 = await monitor.detect_events(cross_context, None, cross_state)
        assert len(events1) >= 1
        assert len(events2) >= 1
        assert events1[0].dedupe_key == events2[0].dedupe_key
        assert events1[0].logical_entity == events2[0].logical_entity

    @pytest.mark.asyncio
    async def test_different_boundary_different_event(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame, minute_bars: pd.DataFrame
    ) -> None:
        """穿越不同 peak_price 产生不同事件（dedupe_key / logical_entity 不同）。"""
        context = _make_context(daily_bars, minute_bars)
        curr_state = await monitor.calculate_state(context)

        assert monitor._last_vp_result is not None
        peak_prices = monitor._last_vp_result.all_peak_prices
        assert len(peak_prices) >= 2, "需要至少 2 个 peak_price"
        price_a = float(peak_prices[0])
        price_b = float(peak_prices[1])

        cross_bars_a = _make_minute_bars_crossing_price(minute_bars, price_a)
        context_a = _make_context(daily_bars, cross_bars_a)
        state_a = await monitor.calculate_state(context_a)

        cross_bars_b = _make_minute_bars_crossing_price(minute_bars, price_b)
        context_b = _make_context(daily_bars, cross_bars_b)
        state_b = await monitor.calculate_state(context_b)

        events_a = await monitor.detect_events(context_a, None, state_a)
        events_b = await monitor.detect_events(context_b, None, state_b)
        assert len(events_a) >= 1
        assert len(events_b) >= 1
        assert events_a[0].dedupe_key != events_b[0].dedupe_key
        assert events_a[0].logical_entity != events_b[0].logical_entity
        assert events_a[0].payload["boundary"] == price_a
        assert events_b[0].payload["boundary"] == price_b

    @pytest.mark.asyncio
    async def test_no_event_when_minute_bars_too_short(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame, minute_bars: pd.DataFrame
    ) -> None:
        """1m bars 不足 2 根时无法做 crossover 检测，返回空事件列表。"""
        short_bars = minute_bars.iloc[-1:].copy()
        context = _make_context(daily_bars, short_bars)
        curr_state = await monitor.calculate_state(context)

        events = await monitor.detect_events(context, None, curr_state)
        assert events == []


class TestVolumeNodeMonitorComputeIndicators:
    """compute_indicators SSOT 测试 - 验证 profile_rows/profile_meta 完整透传。"""

    def _make_daily_context(self, daily_bars: pd.DataFrame) -> MarketDataContext:
        """构建仅含日线 bars 的 MarketDataContext（compute_indicators 不依赖 1m bars）。"""
        return MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="600519",
            bars_daily=daily_bars,
            bars_minute=None,
            trade_date=daily_bars.index[-1].date() if len(daily_bars) > 0 else None,
            bar_time=daily_bars.index[-1].to_pydatetime() if len(daily_bars) > 0 else datetime.now(UTC),
        )

    @pytest.mark.asyncio
    async def test_profile_rows_has_100_rows(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """profile_rows 长度恒为 100（VP_ROWS），与 VP SSOT 一致。"""
        context = self._make_daily_context(daily_bars)
        indicators = await monitor.compute_indicators(context)

        assert "profile_rows" in indicators
        profile_rows = indicators["profile_rows"]
        assert isinstance(profile_rows, list)
        assert len(profile_rows) == 100, (
            f"profile_rows 应为 100 行，实际 {len(profile_rows)}"
        )

    @pytest.mark.asyncio
    async def test_profile_row_fields_complete(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """每行包含全部 9 个必需字段且类型正确。"""
        context = self._make_daily_context(daily_bars)
        indicators = await monitor.compute_indicators(context)

        for i, row in enumerate(indicators["profile_rows"]):
            assert set(row.keys()) == _PROFILE_ROW_KEYS, (
                f"第 {i} 行字段不匹配: {set(row.keys())} != {_PROFILE_ROW_KEYS}"
            )
            # 价格字段为 float
            for k in ("price_low", "price_high", "price_mid",
                      "bullish_volume", "bearish_volume", "total_volume"):
                assert isinstance(row[k], float), f"第 {i} 行 {k} 应为 float"
            # 布尔字段为 bool
            for k in ("is_peak", "is_poc", "is_value_area"):
                assert isinstance(row[k], bool), f"第 {i} 行 {k} 应为 bool"
            # 价格区间单调性：price_low <= price_mid <= price_high
            assert row["price_low"] <= row["price_mid"] <= row["price_high"], (
                f"第 {i} 行价格区间非单调: low={row['price_low']} mid={row['price_mid']} high={row['price_high']}"
            )
            # total_volume = bullish + bearish（VP 多空拆分守恒）
            assert abs(row["total_volume"] - (row["bullish_volume"] + row["bearish_volume"])) < 1e-6, (
                f"第 {i} 行 total_volume != bullish+bearish"
            )

    @pytest.mark.asyncio
    async def test_profile_meta_fields_complete(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """profile_meta 包含全部 5 个必需字段。"""
        context = self._make_daily_context(daily_bars)
        indicators = await monitor.compute_indicators(context)

        assert "profile_meta" in indicators
        meta = indicators["profile_meta"]
        assert isinstance(meta, dict)
        assert set(meta.keys()) == _PROFILE_META_KEYS, (
            f"profile_meta 字段不匹配: {set(meta.keys())}"
        )
        assert meta["row_count"] == 100
        assert isinstance(meta["price_step"], float)
        assert meta["price_step"] > 0
        # POC/VAH/VAL 价格应落在 profile 价格范围内（非 None 时）
        prices_low = [r["price_low"] for r in indicators["profile_rows"]]
        prices_high = [r["price_high"] for r in indicators["profile_rows"]]
        price_min, price_max = min(prices_low), max(prices_high)
        for k in ("poc_price", "vah_price", "val_price"):
            v = meta[k]
            assert v is None or isinstance(v, float), f"{k} 应为 None 或 float"
            if v is not None:
                assert price_min <= v <= price_max, f"{k}={v} 不在价格范围 [{price_min}, {price_max}]"
        # prepare_node_cluster_bars 注入的诊断字段类型校验
        assert isinstance(meta["input_daily_bars"], int)
        assert isinstance(meta["input_15m_bars"], int)
        assert isinstance(meta["input_minute_bars"], int)
        assert meta["primary_period"] == "1d"
        assert meta["low_period"] == "15m"
        assert isinstance(meta["parameter_version"], str)

    @pytest.mark.asyncio
    async def test_is_poc_exactly_one_row(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """is_poc 在 POC 行为 True，其余行为 False（有且仅有一个 POC）。"""
        context = self._make_daily_context(daily_bars)
        indicators = await monitor.compute_indicators(context)

        poc_rows = [r for r in indicators["profile_rows"] if r["is_poc"]]
        assert len(poc_rows) == 1, f"应恰有 1 行 is_poc=True，实际 {len(poc_rows)}"

        # POC 行 price_mid 应与 profile_meta.poc_price 一致
        poc_row = poc_rows[0]
        poc_meta_price = indicators["profile_meta"]["poc_price"]
        if poc_meta_price is not None:
            assert abs(poc_row["price_mid"] - poc_meta_price) < 1e-3, (
                f"POC 行 price_mid={poc_row['price_mid']} 与 meta.poc_price={poc_meta_price} 不一致"
            )

    @pytest.mark.asyncio
    async def test_is_value_area_range(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """is_value_area 在 [VAL, VAH] 区间内为 True，且为连续区间。

        SSOT 语义（luxalgo_volume_profile_pytdx_15m_aligned.py）：
        - is_value_area 对行索引 val_level <= k <= vah_level 为 True
        - vah_price = 最后一个 VA 行的 price_high（上边界）
        - val_price = 第一个 VA 行的 price_low（下边界）
        """
        context = self._make_daily_context(daily_bars)
        indicators = await monitor.compute_indicators(context)

        rows = indicators["profile_rows"]
        va_flags = [r["is_value_area"] for r in rows]
        va_count = sum(va_flags)
        assert va_count > 0, "value area 应至少有 1 行"

        # value area 为连续区间：True 之间不应夹杂 False
        first_true = va_flags.index(True)
        last_true = len(va_flags) - 1 - va_flags[::-1].index(True)
        va_segment = va_flags[first_true:last_true + 1]
        assert all(va_segment), "value area 应为连续区间，内部不应有 False"

        # SSOT 边界语义：VAH=最后 VA 行 price_high，VAL=首 VA 行 price_low
        # 容差 1e-3 容纳 price 字段 round(4) 与 meta 未 round 的浮点差异
        meta = indicators["profile_meta"]
        first_va_row = rows[first_true]
        last_va_row = rows[last_true]
        if meta["vah_price"] is not None:
            assert abs(last_va_row["price_high"] - meta["vah_price"]) < 1e-3, (
                f"VAH={meta['vah_price']} 应≈最后 VA 行 price_high={last_va_row['price_high']}"
            )
        if meta["val_price"] is not None:
            assert abs(first_va_row["price_low"] - meta["val_price"]) < 1e-3, (
                f"VAL={meta['val_price']} 应≈首 VA 行 price_low={first_va_row['price_low']}"
            )

    @pytest.mark.asyncio
    async def test_peak_rows_backward_compatible(
        self, monitor: VolumeNodeMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """peak_rows 字段保留（向后兼容），结构不变。"""
        context = self._make_daily_context(daily_bars)
        indicators = await monitor.compute_indicators(context)

        assert "peak_rows" in indicators
        peak_rows = indicators["peak_rows"]
        assert isinstance(peak_rows, list)
        # peak_rows 可能为空（合成数据无 peak），但非空时字段必须完整
        expected_peak_keys = {"price_mid", "bullish_volume", "bearish_volume", "total_volume", "is_peak"}
        for row in peak_rows:
            assert set(row.keys()) == expected_peak_keys
            assert row["is_peak"] is True

        # profile_rows 中 is_peak=True 的行数应 >= peak_rows 行数
        # （profile_rows 的 is_peak 覆盖更广，包含重复档位）
        profile_peak_count = sum(1 for r in indicators["profile_rows"] if r["is_peak"])
        assert profile_peak_count >= len(peak_rows)

    @pytest.mark.asyncio
    async def test_short_bars_returns_empty_profile(
        self, monitor: VolumeNodeMonitor
    ) -> None:
        """日线 bars 不足 10 根时返回空 profile_rows 和空 profile_meta。"""
        short_bars = _generate_daily_bars(n_bars=5)
        context = self._make_daily_context(short_bars)
        indicators = await monitor.compute_indicators(context)

        assert indicators["profile_rows"] == []
        assert indicators["profile_meta"] == {}
        assert indicators["peak_rows"] == []


class TestIndicatorServiceSnapshotPassthrough:
    """_truncate_lists 快照字段透传测试 - 保证 SSOT 完整性。"""

    def test_snapshot_keys_not_truncated(self) -> None:
        """profile_rows/profile_meta/peak_rows 不受 bars 截断。"""
        from app.services.indicator_service import _truncate_lists

        indicators = {
            "upper_node": list(range(200)),  # 时间序列，应被截断
            "profile_rows": [{"price_mid": i} for i in range(100)],  # 快照，不截断
            "profile_meta": {"row_count": 100, "price_step": 0.01},
            "peak_rows": [{"price_mid": 10.0}],
        }
        result = _truncate_lists(indicators, bars=50)
        # 时间序列截断为最后 50 个
        assert len(result["upper_node"]) == 50
        # 快照字段保持完整
        assert len(result["profile_rows"]) == 100
        assert result["profile_meta"] == {"row_count": 100, "price_step": 0.01}
        assert result["peak_rows"] == [{"price_mid": 10.0}]

    def test_normal_keys_still_truncate(self) -> None:
        """非快照字段仍按 bars 截断（回归保护）。"""
        from app.services.indicator_service import _truncate_lists

        result = _truncate_lists({"a": [1, 2, 3, 4, 5]}, 3)
        assert result == {"a": [3, 4, 5]}


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-s"])
