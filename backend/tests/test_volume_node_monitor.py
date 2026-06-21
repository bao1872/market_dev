"""VolumeNodeMonitor 监控策略测试 - 验证功能正确性与性能预算。

测试内容：
1. 性能预算验证：单只标的 calculate_state + detect_events 总耗时 < 500ms（MONITOR_BUDGET_MS）
2. 状态输出验证：MonitorState.state 含 manifest.outputs 声明的 6 个字段
3. 事件检测验证：node_cluster_touch 事件 + touch_episode 去重
4. dedupe 验证：同一 episode 不重复触发，不同 episode 触发新事件

测试数据：
- 使用合成的 1m bars（不依赖真实数据库/网络）
- 生成 360+ 根 1m bars 满足 VP lookback 要求
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
            {"key": "poc_node", "type": "json"},
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
def minute_bars() -> pd.DataFrame:
    """400 根 1m bars（满足 VP lookback=360）。"""
    return _generate_minute_bars(n_bars=400)


@pytest.fixture
async def monitor() -> VolumeNodeMonitor:
    """已初始化的 VolumeNodeMonitor 实例。"""
    m = VolumeNodeMonitor()
    version = _make_mock_version()
    await m.initialize(version)
    return m


def _make_context(bars: pd.DataFrame, bar_time: datetime | None = None) -> MarketDataContext:
    """构建 MarketDataContext（1m bars）。"""
    return MarketDataContext(
        instrument_id=uuid.uuid4(),
        symbol="600519",
        bars_daily=pd.DataFrame(),  # monitor 不使用日线
        bars_minute=bars,
        trade_date=bars.index[0].date() if len(bars) > 0 else None,
        bar_time=bar_time or (bars.index[-1].to_pydatetime() if len(bars) > 0 else datetime.now(UTC)),
    )


class TestVolumeNodeMonitorPerformance:
    """性能预算测试（Task 16.6）。"""

    @pytest.mark.asyncio
    async def test_single_instrument_under_500ms(
        self, monitor: VolumeNodeMonitor, minute_bars: pd.DataFrame
    ) -> None:
        """验证单只标的 calculate_state + detect_events 总耗时 < 500ms。

        对照 volume_node_monitor.yaml resource_budget.target_ms_per_instrument=500。
        测量 3 次取最小值（减少噪声），任一次通过即视为达标。
        """
        context = _make_context(minute_bars)
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
        self, monitor: VolumeNodeMonitor, minute_bars: pd.DataFrame
    ) -> None:
        """验证 MonitorState.state 含 manifest.outputs 声明的 6 个字段。"""
        context = _make_context(minute_bars)
        state = await monitor.calculate_state(context)

        expected_fields = {
            "current_price", "upper_node", "lower_node",
            "position_0_1", "poc_node", "last_touched_node",
        }
        actual_fields = set(state.state.keys())
        assert actual_fields == expected_fields, (
            f"状态字段不匹配: 缺失={expected_fields - actual_fields}, "
            f"多余={actual_fields - expected_fields}"
        )

    @pytest.mark.asyncio
    async def test_current_price_is_number(
        self, monitor: VolumeNodeMonitor, minute_bars: pd.DataFrame
    ) -> None:
        """验证 current_price 为数值类型。"""
        context = _make_context(minute_bars)
        state = await monitor.calculate_state(context)
        assert isinstance(state.state["current_price"], (int, float))
        assert state.state["current_price"] > 0

    @pytest.mark.asyncio
    async def test_position_0_1_in_range(
        self, monitor: VolumeNodeMonitor, minute_bars: pd.DataFrame
    ) -> None:
        """验证 position_0_1 在 [0, 1] 区间内（ratio_0_1 语义）。"""
        context = _make_context(minute_bars)
        state = await monitor.calculate_state(context)
        position = state.state["position_0_1"]
        assert isinstance(position, (int, float))
        assert 0.0 <= position <= 1.0

    @pytest.mark.asyncio
    async def test_node_json_structure(
        self, monitor: VolumeNodeMonitor, minute_bars: pd.DataFrame
    ) -> None:
        """验证 node 输出为 json 结构（含 price_mid/price_low/price_high）或 None。"""
        context = _make_context(minute_bars)
        state = await monitor.calculate_state(context)

        for field in ("upper_node", "lower_node", "poc_node", "last_touched_node"):
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
    """事件检测与去重测试（Task 16.5）。"""

    @pytest.mark.asyncio
    async def test_no_event_when_no_touch(
        self, monitor: VolumeNodeMonitor, minute_bars: pd.DataFrame
    ) -> None:
        """无触碰时无事件（last_touched_node=None → 空事件列表）。"""
        context = _make_context(minute_bars)
        curr_state = await monitor.calculate_state(context)

        # 强制 curr_state 无触碰
        curr_state.state["last_touched_node"] = None

        events = await monitor.detect_events(context, None, curr_state)
        assert events == []

    @pytest.mark.asyncio
    async def test_event_on_new_touch_episode(
        self, monitor: VolumeNodeMonitor, minute_bars: pd.DataFrame
    ) -> None:
        """新触碰 episode 触发 node_cluster_touch 事件。

        场景：prev 无触碰，curr 有触碰 → 触发事件。
        """
        context = _make_context(minute_bars)
        curr_state = await monitor.calculate_state(context)

        # 强制 curr_state 有触碰
        curr_state.state["last_touched_node"] = {
            "price_mid": 10.5,
            "price_low": 10.45,
            "price_high": 10.55,
        }

        events = await monitor.detect_events(context, None, curr_state)
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, StrategyEventDraft)
        assert event.event_type == EVENT_TYPE_NODE_CLUSTER_TOUCH
        assert event.state_ttl_seconds == EVENT_STATE_TTL_SECONDS
        # 验证 dedupe_key 含 instrument_id 和 node_price_mid
        assert str(curr_state.instrument_id) in event.dedupe_key
        assert "10.5" in event.dedupe_key
        # 验证 payload 自包含
        assert event.payload["instrument_id"] == str(curr_state.instrument_id)
        assert event.payload["node"] == curr_state.state["last_touched_node"]

    @pytest.mark.asyncio
    async def test_dedupe_same_episode_no_event(
        self, monitor: VolumeNodeMonitor, minute_bars: pd.DataFrame
    ) -> None:
        """同一 episode（触碰同一 Node）不重复触发。

        场景：prev 触碰 Node A，curr 触碰同一 Node A → 不触发事件。
        """
        context = _make_context(minute_bars)
        curr_state = await monitor.calculate_state(context)

        touched_node = {
            "price_mid": 10.5,
            "price_low": 10.45,
            "price_high": 10.55,
        }
        curr_state.state["last_touched_node"] = touched_node

        # prev 触碰同一 Node（同一 episode）
        prev_state = MonitorState(
            instrument_id=curr_state.instrument_id,
            strategy_version_id=curr_state.strategy_version_id,
            state={"last_touched_node": touched_node.copy()},
        )

        events = await monitor.detect_events(context, prev_state, curr_state)
        assert events == [], "同一 episode 不应触发事件"

    @pytest.mark.asyncio
    async def test_new_episode_on_different_node(
        self, monitor: VolumeNodeMonitor, minute_bars: pd.DataFrame
    ) -> None:
        """触碰不同 Node 触发新 episode 事件。

        场景：prev 触碰 Node A，curr 触碰 Node B → 触发事件。
        """
        context = _make_context(minute_bars)
        curr_state = await monitor.calculate_state(context)

        curr_state.state["last_touched_node"] = {
            "price_mid": 10.8,
            "price_low": 10.75,
            "price_high": 10.85,
        }

        # prev 触碰不同 Node（不同 episode）
        prev_state = MonitorState(
            instrument_id=curr_state.instrument_id,
            strategy_version_id=curr_state.strategy_version_id,
            state={"last_touched_node": {
                "price_mid": 10.5,
                "price_low": 10.45,
                "price_high": 10.55,
            }},
        )

        events = await monitor.detect_events(context, prev_state, curr_state)
        assert len(events) == 1
        assert events[0].event_type == EVENT_TYPE_NODE_CLUSTER_TOUCH

    @pytest.mark.asyncio
    async def test_episode_end_no_event(
        self, monitor: VolumeNodeMonitor, minute_bars: pd.DataFrame
    ) -> None:
        """episode 结束（prev 有触碰，curr 无触碰）不触发事件。"""
        context = _make_context(minute_bars)
        curr_state = await monitor.calculate_state(context)
        curr_state.state["last_touched_node"] = None  # curr 无触碰

        # prev 有触碰
        prev_state = MonitorState(
            instrument_id=curr_state.instrument_id,
            strategy_version_id=curr_state.strategy_version_id,
            state={"last_touched_node": {
                "price_mid": 10.5,
                "price_low": 10.45,
                "price_high": 10.55,
            }},
        )

        events = await monitor.detect_events(context, prev_state, curr_state)
        assert events == [], "episode 结束不应触发事件"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-s"])
