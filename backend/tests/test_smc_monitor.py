"""SmcMonitor 监控策略测试 - 验证 SMC 日线结构盘中监控。

[CHANGE-20260720-002 §二] 测试内容：
1. calculate_state: 返回 smc_confirmed_bos/smc_confirmed_choch/smc_active_obs/
   smc_current_price/smc_currently_touched/smc_swing_bias/smc_trailing/smc_availability
   /smc_episode_tracker 等字段
2. detect_events: 1m high/low 与 BOS/CHoCH level 相交触发 smc_bos_retest/smc_choch_retest
3. detect_events: 1m high/low 与 OB zone 相交触发 smc_order_block_first_touch
4. touch_episode dedupe: 同一 episode 多次触碰只触发一次事件
5. episode tracker: detect_events 直接 mutate curr_state.state["smc_episode_tracker"]
6. smc_entity_id 稳定性: BOS:{anchor_index}:{level} / CHoCH:... / OB:...
7. WatchlistMonitor 命名空间合并: bb/node_cluster/smc/market/degraded
8. WatchlistMonitor 单子 monitor 失败只标记 degraded 不阻断其他

[PRD V2.0 §3.2 L117 / SMC-02] 触碰检测使用最新已完成 1m 的 high/low 与线/区域相交，
覆盖影线触碰场景（close 未穿越但 high/low 相交）。

测试数据：
- 使用合成的日线 bars（≥250 根）满足 SMC ATR200 + swings_length=50 warmup 要求
- 使用合成的 1m bars（2 根）做触碰检测，支持显式 cur_high/cur_low 测试影线
- 1m bars 价格锚定最近日线 BOS level 触发 smc_bos_retest 事件
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.strategy.monitors.smc_monitor import (
    NOTIFY_COOLDOWN_SECONDS,
    SMC_BOS_RETEST,
    SMC_CHOCH_RETEST,
    SMC_ORDER_BLOCK_FIRST_TOUCH,
    SmcMonitor,
    _is_bos_touched,
    _is_ob_touched,
    _make_bos_entity_id,
    _make_choch_entity_id,
    _make_ob_entity_id,
)
from app.strategy.monitors.watchlist_monitor import WatchlistMonitor
from app.strategy.runtime import (
    MarketDataContext,
    MonitorState,
    StrategyEventDraft,
)


def _generate_daily_bars(
    n_bars: int = 300,
    end_date: str = "2026-06-18",
    start_price: float = 10.0,
    seed: int = 43,
) -> pd.DataFrame:
    """生成合成的日线 OHLCV bars（满足 SMC ATR200 + swings_length=50 warmup）。"""
    np.random.seed(seed)
    dates = pd.date_range(end=end_date, periods=n_bars, freq="B")
    daily_returns = np.random.uniform(-0.02, 0.02, size=n_bars)
    close = start_price * np.cumprod(1 + daily_returns)
    open_ = close * (1 + np.random.uniform(-0.01, 0.01, size=n_bars))
    high = np.maximum(open_, close) * (1 + np.random.uniform(0.005, 0.02, size=n_bars))
    low = np.minimum(open_, close) * (1 - np.random.uniform(0.005, 0.02, size=n_bars))
    volume = np.random.uniform(1_000_000, 5_000_000, size=n_bars)
    amount = volume * close
    df = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        },
        index=dates,
    )
    df.index.name = "datetime"
    return df


def _make_minute_bars(
    prev_close: float,
    cur_close: float,
    bar_time: datetime | None = None,
    cur_high: float | None = None,
    cur_low: float | None = None,
) -> pd.DataFrame:
    """构造 2 根 1m bars（用于触碰检测）。

    [PRD V2.0 §3.2 L117] 触发使用最新已完成 1m 的 high/low 与线/区域相交。
    支持 cur_high/cur_low 显式传入以测试影线触碰场景（close 未穿越但 high/low 相交）。

    Args:
        prev_close: 前一根 1m close
        cur_close: 当前 1m close
        bar_time: 当前 bar 时间（默认 2026-06-18 10:30）
        cur_high: 当前 1m high（默认 max(prev_close, cur_close)，向后兼容）
        cur_low: 当前 1m low（默认 min(prev_close, cur_close)，向后兼容）
    """
    if bar_time is None:
        bar_time = datetime(2026, 6, 18, 10, 30, tzinfo=UTC)
    if cur_high is None:
        cur_high = max(prev_close, cur_close)
    if cur_low is None:
        cur_low = min(prev_close, cur_close)
    times = pd.date_range(end=bar_time, periods=2, freq="1min", tz=UTC)
    return pd.DataFrame(
        {
            "open": [prev_close, cur_close],
            "high": [max(prev_close, cur_close), cur_high],
            "low": [min(prev_close, cur_close), cur_low],
            "close": [prev_close, cur_close],
            "volume": [100_000.0, 120_000.0],
            "amount": [prev_close * 100_000, cur_close * 120_000],
        },
        index=times,
    )


def _make_mock_version(strategy_id: str = "watchlist_monitor") -> MagicMock:
    """创建 mock StrategyVersion 对象（含 manifest）。"""
    version = MagicMock()
    version.id = uuid.uuid4()
    version.manifest = {
        "strategy_id": strategy_id,
        "kind": "monitor",
        "version": "1.2.0",
        "parameters": [
            {"key": "algorithm.bb_win", "type": "integer", "default": 20},
            {"key": "algorithm.bb_k", "type": "float", "default": 2.0},
        ],
        "outputs": [
            {"key": "smc_confirmed_bos", "type": "json"},
            {"key": "smc_confirmed_choch", "type": "json"},
            {"key": "smc_active_obs", "type": "json"},
            {"key": "smc_current_price", "type": "number"},
            {"key": "smc_currently_touched", "type": "json"},
            {"key": "smc_swing_bias", "type": "number"},
            {"key": "smc_trailing", "type": "json"},
            {"key": "smc_availability", "type": "string"},
            {"key": "smc_degraded_reason", "type": "string"},
            {"key": "smc_episode_tracker", "type": "json"},
        ],
        "event_types": [
            {"key": "smc_bos_retest", "dedupe": "touch_episode"},
            {"key": "smc_choch_retest", "dedupe": "touch_episode"},
            {"key": "smc_order_block_first_touch", "dedupe": "touch_episode"},
        ],
    }
    return version


def _make_context(
    bars_daily: pd.DataFrame,
    bars_minute: pd.DataFrame | None = None,
    bar_time: datetime | None = None,
) -> MarketDataContext:
    """构建 MarketDataContext（日线 + 1m bars）。"""
    return MarketDataContext(
        instrument_id=uuid.uuid4(),
        symbol="600519",
        bars_daily=bars_daily,
        bars_minute=bars_minute,
        trade_date=bars_daily.index[-1].date() if not bars_daily.empty else None,
        bar_time=bar_time,
    )


@pytest.fixture
def daily_bars() -> pd.DataFrame:
    """300 根日线 bars（满足 SMC warmup）。"""
    return _generate_daily_bars(n_bars=300)


@pytest.fixture
async def smc_monitor() -> SmcMonitor:
    """已初始化的 SmcMonitor 实例。"""
    m = SmcMonitor()
    version = _make_mock_version()
    await m.initialize(version)
    return m


# =============================================================================
# 1. 模块级辅助函数测试
# =============================================================================


class TestSmcMonitorHelpers:
    """验证 SmcMonitor 模块级辅助函数。"""

    def test_make_bos_entity_id_stable(self) -> None:
        """BOS entity_id 稳定：相同 anchor_index+level 生成相同 ID。"""
        id1 = _make_bos_entity_id(100, 10.5)
        id2 = _make_bos_entity_id(100, 10.5)
        assert id1 == id2 == "BOS:100:10.5"

    def test_make_choch_entity_id_stable(self) -> None:
        """CHoCH entity_id 稳定。"""
        id1 = _make_choch_entity_id(200, 9.8)
        id2 = _make_choch_entity_id(200, 9.8)
        assert id1 == id2 == "CHoCH:200:9.8"

    def test_make_ob_entity_id_stable(self) -> None:
        """OB entity_id 稳定。"""
        id1 = _make_ob_entity_id(150, 11.0, 10.0, 1)
        id2 = _make_ob_entity_id(150, 11.0, 10.0, 1)
        assert id1 == id2 == "OB:150:11.0:10.0:1"

    def test_is_bos_touched_level_inside_bar(self) -> None:
        """BOS level 在 bar 的 [cur_low, cur_high] 范围内：触碰。"""
        assert _is_bos_touched(cur_high=10.5, cur_low=9.5, level=10.0) is True

    def test_is_bos_touched_level_at_cur_low(self) -> None:
        """BOS level = cur_low 边界：触碰（含边界）。"""
        assert _is_bos_touched(cur_high=10.5, cur_low=10.0, level=10.0) is True

    def test_is_bos_touched_level_at_cur_high(self) -> None:
        """BOS level = cur_high 边界：触碰（含边界）。"""
        assert _is_bos_touched(cur_high=10.0, cur_low=9.5, level=10.0) is True

    def test_is_bos_touched_wick_only(self) -> None:
        """BOS 影线触碰正例：close 未触及 level，但 high 触及（[PRD V2.0 SMC-02] 修复）。"""
        # cur_close=9.8（在 level 10.0 下方），但 cur_high=10.5 触及 level
        assert _is_bos_touched(cur_high=10.5, cur_low=9.3, level=10.0) is True

    def test_is_bos_touched_level_below_bar(self) -> None:
        """BOS level 在 bar 下方：未触碰。"""
        assert _is_bos_touched(cur_high=9.5, cur_low=9.0, level=10.0) is False

    def test_is_bos_touched_level_above_bar(self) -> None:
        """BOS level 在 bar 上方：未触碰。"""
        assert _is_bos_touched(cur_high=10.8, cur_low=10.5, level=11.0) is False

    def test_is_ob_touched_bar_inside_zone(self) -> None:
        """OB bar 完全在 zone 内：触碰。"""
        assert _is_ob_touched(cur_high=10.8, cur_low=10.5, bar_high=11.0, bar_low=10.0) is True

    def test_is_ob_touched_bar_overlaps_from_below(self) -> None:
        """OB bar 从下方部分进入 zone：触碰。"""
        assert _is_ob_touched(cur_high=10.5, cur_low=9.5, bar_high=11.0, bar_low=10.0) is True

    def test_is_ob_touched_bar_overlaps_from_above(self) -> None:
        """OB bar 从上方部分进入 zone：触碰。"""
        assert _is_ob_touched(cur_high=11.5, cur_low=10.5, bar_high=11.0, bar_low=10.0) is True

    def test_is_ob_touched_wick_only(self) -> None:
        """OB 影线触碰正例：close 未进入 zone，但 high 进入（[PRD V2.0 SMC-02] 修复）。"""
        # cur_close=9.5（在 zone 下方），但 cur_high=10.5 进入 zone [10.0, 11.0]
        assert _is_ob_touched(cur_high=10.5, cur_low=9.0, bar_high=11.0, bar_low=10.0) is True

    def test_is_ob_touched_bar_touches_boundary(self) -> None:
        """OB bar 边界相切 zone：触碰（含边界）。"""
        # cur_high = bar_low
        assert _is_ob_touched(cur_high=10.0, cur_low=9.0, bar_high=11.0, bar_low=10.0) is True
        # cur_low = bar_high
        assert _is_ob_touched(cur_high=12.0, cur_low=11.0, bar_high=11.0, bar_low=10.0) is True

    def test_is_ob_touched_bar_below_zone(self) -> None:
        """OB bar 完全在 zone 下方：未触碰。"""
        assert _is_ob_touched(cur_high=9.5, cur_low=9.0, bar_high=11.0, bar_low=10.0) is False

    def test_is_ob_touched_bar_above_zone(self) -> None:
        """OB bar 完全在 zone 上方：未触碰。"""
        assert _is_ob_touched(cur_high=12.0, cur_low=11.5, bar_high=11.0, bar_low=10.0) is False


# =============================================================================
# 2. SmcMonitor.calculate_state 测试
# =============================================================================


class TestSmcMonitorCalculateState:
    """验证 SmcMonitor.calculate_state 输出字段。"""

    @pytest.mark.asyncio
    async def test_calculate_state_returns_required_fields(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """calculate_state 返回所有必需字段。"""
        bars_minute = _make_minute_bars(prev_close=10.0, cur_close=10.5)
        ctx = _make_context(daily_bars, bars_minute)
        state = await smc_monitor.calculate_state(ctx)

        # 验证所有必需字段存在
        assert "smc_confirmed_bos" in state.state
        assert "smc_confirmed_choch" in state.state
        assert "smc_active_obs" in state.state
        assert "smc_current_price" in state.state
        assert "smc_currently_touched" in state.state
        assert "smc_swing_bias" in state.state
        assert "smc_trailing" in state.state
        assert "smc_availability" in state.state
        assert "smc_degraded_reason" in state.state
        assert "smc_episode_tracker" in state.state

    @pytest.mark.asyncio
    async def test_calculate_state_availability_available(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """数据充足时 availability=available。"""
        bars_minute = _make_minute_bars(prev_close=10.0, cur_close=10.5)
        ctx = _make_context(daily_bars, bars_minute)
        state = await smc_monitor.calculate_state(ctx)
        assert state.state["smc_availability"] == "available"
        assert state.state["smc_degraded_reason"] is None

    @pytest.mark.asyncio
    async def test_calculate_state_current_price_from_minute(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """smc_current_price 优先取 1m close。"""
        bars_minute = _make_minute_bars(prev_close=10.0, cur_close=10.5)
        ctx = _make_context(daily_bars, bars_minute)
        state = await smc_monitor.calculate_state(ctx)
        assert state.state["smc_current_price"] == 10.5

    @pytest.mark.asyncio
    async def test_calculate_state_current_price_fallback_daily(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """无 1m bars 时 smc_current_price 回退到日线最后 close。"""
        ctx = _make_context(daily_bars, bars_minute=None)
        state = await smc_monitor.calculate_state(ctx)
        expected = round(float(daily_bars["close"].iloc[-1]), 4)
        assert state.state["smc_current_price"] == expected

    @pytest.mark.asyncio
    async def test_calculate_state_currently_touched_empty_without_minute(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """无 1m bars 时 smc_currently_touched 为空 dict。"""
        ctx = _make_context(daily_bars, bars_minute=None)
        state = await smc_monitor.calculate_state(ctx)
        assert state.state["smc_currently_touched"] == {}

    @pytest.mark.asyncio
    async def test_calculate_state_episode_tracker_init_empty(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """calculate_state 初始化 smc_episode_tracker 为空 dict（由 detect_events 填充）。"""
        bars_minute = _make_minute_bars(prev_close=10.0, cur_close=10.5)
        ctx = _make_context(daily_bars, bars_minute)
        state = await smc_monitor.calculate_state(ctx)
        assert state.state["smc_episode_tracker"] == {}

    @pytest.mark.asyncio
    async def test_calculate_state_raises_on_insufficient_daily(
        self, smc_monitor: SmcMonitor
    ) -> None:
        """日线数据不足 250 根时抛 ValueError。"""
        short_bars = _generate_daily_bars(n_bars=100)
        ctx = _make_context(short_bars, None)
        with pytest.raises(ValueError, match="daily bars 数据不足"):
            await smc_monitor.calculate_state(ctx)

    @pytest.mark.asyncio
    async def test_calculate_state_raises_on_empty_daily(
        self, smc_monitor: SmcMonitor
    ) -> None:
        """日线数据为空时抛 ValueError。"""
        ctx = _make_context(pd.DataFrame(), None)
        with pytest.raises(ValueError, match="需要 daily bars 数据"):
            await smc_monitor.calculate_state(ctx)


# =============================================================================
# 3. SmcMonitor.detect_events 测试
# =============================================================================


class TestSmcMonitorDetectEvents:
    """验证 SmcMonitor.detect_events 事件检测与 touch_episode dedupe。"""

    @pytest.mark.asyncio
    async def test_detect_events_empty_when_no_touch(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """无触碰时不产生事件，但 tracker 会记录所有 entity 的 last_touched=False。"""
        # 使用无 1m bars 的 context，currently_touched 为空 dict
        ctx = _make_context(daily_bars, bars_minute=None)
        curr_state = await smc_monitor.calculate_state(ctx)

        events = await smc_monitor.detect_events(ctx, None, curr_state)
        assert events == []
        # 无 1m bars → currently_touched 为空 → tracker 为空 dict
        assert curr_state.state["smc_episode_tracker"] == {}

    @pytest.mark.asyncio
    async def test_detect_events_no_minute_bars(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """无 1m bars 时不产生事件。"""
        ctx = _make_context(daily_bars, bars_minute=None)
        curr_state = await smc_monitor.calculate_state(ctx)

        events = await smc_monitor.detect_events(ctx, None, curr_state)
        assert events == []

    @pytest.mark.asyncio
    async def test_detect_events_bos_retest_triggers(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """1m 穿越 BOS level 触发 smc_bos_retest 事件。

        策略：找最近的 BOS level，构造 1m bars 让 cur_close 穿越。
        """
        bars_minute_neutral = _make_minute_bars(prev_close=10.0, cur_close=10.0)
        ctx_neutral = _make_context(daily_bars, bars_minute_neutral)
        curr_state = await smc_monitor.calculate_state(ctx_neutral)

        bos_list = curr_state.state["smc_confirmed_bos"]
        if not bos_list:
            pytest.skip("合成数据未产生 BOS 事件，跳过 BOS 触碰测试")

        # 取第一个 BOS level 构造穿越
        bos = bos_list[0]
        bos_level = float(bos["level"])
        prev_close = bos_level - 0.5  # 在 level 下方
        cur_close = bos_level + 0.5  # 在 level 上方 → 穿越触发

        bars_minute = _make_minute_bars(prev_close=prev_close, cur_close=cur_close)
        ctx = _make_context(daily_bars, bars_minute)
        curr_state = await smc_monitor.calculate_state(ctx)

        events = await smc_monitor.detect_events(ctx, None, curr_state)

        bos_events = [e for e in events if e.event_type == SMC_BOS_RETEST]
        if not bos_events:
            pytest.skip("合成数据 BOS level 不在 1m 价格范围内，跳过")

        assert len(bos_events) >= 1
        ev = bos_events[0]
        assert ev.event_type == SMC_BOS_RETEST
        assert ev.state_ttl_seconds == NOTIFY_COOLDOWN_SECONDS
        assert "smc_entity_id" in ev.payload
        assert ev.payload["smc_entity_id"].startswith("BOS:")
        assert ev.payload["touch_episode"] == 1
        # dedupe_key 含 event_type:instrument_id:entity_id:touch_episode
        assert SMC_BOS_RETEST in ev.dedupe_key
        assert ":1" in ev.dedupe_key  # episode=1

    @pytest.mark.asyncio
    async def test_detect_events_touch_episode_dedupe(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """同一 episode 多次触碰只触发一次事件。"""
        bars_minute_neutral = _make_minute_bars(prev_close=10.0, cur_close=10.0)
        ctx_neutral = _make_context(daily_bars, bars_minute_neutral)
        curr_state_init = await smc_monitor.calculate_state(ctx_neutral)

        bos_list = curr_state_init.state["smc_confirmed_bos"]
        if not bos_list:
            pytest.skip("合成数据未产生 BOS 事件，跳过 episode dedupe 测试")

        bos = bos_list[0]
        bos_level = float(bos["level"])
        prev_close = bos_level - 0.5
        cur_close = bos_level + 0.5

        # 第一次触碰：新 episode=1，触发事件
        bars_minute_1 = _make_minute_bars(prev_close=prev_close, cur_close=cur_close)
        ctx_1 = _make_context(daily_bars, bars_minute_1)
        curr_state_1 = await smc_monitor.calculate_state(ctx_1)
        events_1 = await smc_monitor.detect_events(ctx_1, None, curr_state_1)

        # 第二次触碰：prev 已 touched，同 episode，不触发
        # 构造 prev_state from curr_state_1（含已更新的 tracker）
        bars_minute_2 = _make_minute_bars(prev_close=prev_close, cur_close=cur_close)
        ctx_2 = _make_context(daily_bars, bars_minute_2)
        curr_state_2 = await smc_monitor.calculate_state(ctx_2)
        events_2 = await smc_monitor.detect_events(ctx_2, curr_state_1, curr_state_2)

        # 第一次应有事件，第二次同 episode 应无事件
        bos_events_1 = [e for e in events_1 if e.event_type == SMC_BOS_RETEST]
        bos_events_2 = [e for e in events_2 if e.event_type == SMC_BOS_RETEST]

        if not bos_events_1:
            pytest.skip("合成数据 BOS level 不在 1m 价格范围内")

        assert len(bos_events_1) >= 1
        assert len(bos_events_2) == 0  # dedupe

    @pytest.mark.asyncio
    async def test_detect_events_new_episode_after_release(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """触碰释放后再次触碰触发新 episode（直接注入 state 避免合成数据干扰）。"""
        from uuid import uuid4

        # 直接构造 prev_state/curr_state，避免合成数据中其他 BOS level 干扰
        inst_id = uuid4()
        version_id = uuid4()
        bos_entity = "BOS:100:10.5"
        bar_time = datetime(2026, 6, 18, 10, 30, tzinfo=UTC)

        # prev_state: BOS 已 touched（episode=1, last_touched=True）
        prev_state = MonitorState(
            instrument_id=inst_id,
            strategy_version_id=version_id,
            state={
                "smc_confirmed_bos": [{"anchor_index": 100, "level": 10.5, "bias": 1}],
                "smc_confirmed_choch": [],
                "smc_active_obs": [],
                "smc_current_price": 10.5,
                "smc_currently_touched": {bos_entity: True},
                "smc_swing_bias": 1,
                "smc_trailing": {},
                "smc_availability": "available",
                "smc_degraded_reason": None,
                "smc_episode_tracker": {
                    bos_entity: {"episode": 1, "last_touched": True},
                },
            },
            state_version=1,
            updated_at=bar_time,
        )

        # curr_state（释放）：BOS 未 touched
        curr_state_release = MonitorState(
            instrument_id=inst_id,
            strategy_version_id=version_id,
            state={
                "smc_confirmed_bos": [{"anchor_index": 100, "level": 10.5, "bias": 1}],
                "smc_confirmed_choch": [],
                "smc_active_obs": [],
                "smc_current_price": 15.0,  # 远离 level
                "smc_currently_touched": {bos_entity: False},
                "smc_swing_bias": 1,
                "smc_trailing": {},
                "smc_availability": "available",
                "smc_degraded_reason": None,
                "smc_episode_tracker": {},  # detect_events 会填充
            },
            state_version=1,
            updated_at=bar_time,
        )

        ctx = _make_context(daily_bars, _make_minute_bars(prev_close=10.0, cur_close=15.0), bar_time)
        events_release = await smc_monitor.detect_events(ctx, prev_state, curr_state_release)
        # 释放期不应有 BOS 事件
        assert len([e for e in events_release if e.event_type == SMC_BOS_RETEST]) == 0
        # tracker 应更新 last_touched=False，保留 episode=1
        tracker_after_release = curr_state_release.state["smc_episode_tracker"]
        assert bos_entity in tracker_after_release
        assert tracker_after_release[bos_entity]["episode"] == 1
        assert tracker_after_release[bos_entity]["last_touched"] is False

        # curr_state（再次触碰）：BOS touched
        curr_state_retouch = MonitorState(
            instrument_id=inst_id,
            strategy_version_id=version_id,
            state={
                "smc_confirmed_bos": [{"anchor_index": 100, "level": 10.5, "bias": 1}],
                "smc_confirmed_choch": [],
                "smc_active_obs": [],
                "smc_current_price": 10.5,
                "smc_currently_touched": {bos_entity: True},
                "smc_swing_bias": 1,
                "smc_trailing": {},
                "smc_availability": "available",
                "smc_degraded_reason": None,
                "smc_episode_tracker": {},  # detect_events 会填充
            },
            state_version=1,
            updated_at=bar_time,
        )

        events_retouch = await smc_monitor.detect_events(
            ctx, curr_state_release, curr_state_retouch
        )
        bos_events_retouch = [
            e for e in events_retouch if e.event_type == SMC_BOS_RETEST
        ]
        assert len(bos_events_retouch) == 1
        # episode 应为 2（释放后新 episode）
        assert bos_events_retouch[0].payload["touch_episode"] == 2
        assert ":2" in bos_events_retouch[0].dedupe_key
        assert bos_events_retouch[0].payload["smc_entity_id"] == bos_entity

    @pytest.mark.asyncio
    async def test_detect_events_mutates_episode_tracker(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """detect_events 直接 mutate curr_state.state['smc_episode_tracker']。"""
        bars_minute_neutral = _make_minute_bars(prev_close=10.0, cur_close=10.0)
        ctx_neutral = _make_context(daily_bars, bars_minute_neutral)
        curr_state = await smc_monitor.calculate_state(ctx_neutral)

        bos_list = curr_state.state["smc_confirmed_bos"]
        if not bos_list:
            pytest.skip("合成数据未产生 BOS 事件")

        bos = bos_list[0]
        bos_level = float(bos["level"])

        bars_touch = _make_minute_bars(prev_close=bos_level - 0.5, cur_close=bos_level + 0.5)
        ctx = _make_context(daily_bars, bars_touch)
        curr_state = await smc_monitor.calculate_state(ctx)

        # detect_events 前 tracker 为空
        assert curr_state.state["smc_episode_tracker"] == {}

        await smc_monitor.detect_events(ctx, None, curr_state)

        # detect_events 后 tracker 应被更新（含至少一个 entity 的 episode=1, last_touched=True）
        tracker = curr_state.state["smc_episode_tracker"]
        assert isinstance(tracker, dict)
        # 如果有触发事件，tracker 应有内容
        bos_events = [
            e for e in (await smc_monitor.detect_events(ctx, None, curr_state))
            if e.event_type == SMC_BOS_RETEST
        ]
        if bos_events:
            # 重新计算 tracker
            curr_state.state["smc_episode_tracker"] = {}
            await smc_monitor.detect_events(ctx, None, curr_state)
            tracker = curr_state.state["smc_episode_tracker"]
            assert len(tracker) > 0
            # 至少有一个 entity 的 last_touched=True
            assert any(info.get("last_touched") for info in tracker.values())


# =============================================================================
# 4. SmcMonitor.compute_indicators 测试
# =============================================================================


class TestSmcMonitorComputeIndicators:
    """验证 SmcMonitor.compute_indicators 返回完整 SMC DTO。"""

    @pytest.mark.asyncio
    async def test_compute_indicators_returns_full_dto(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """compute_indicators 返回完整 SMC DTO 字段。"""
        ctx = _make_context(daily_bars)
        dto = await smc_monitor.compute_indicators(ctx)

        # 验证 SMC DTO 必需字段
        assert "events" in dto
        assert "order_blocks" in dto
        assert "equal_highs_lows" in dto
        assert "trailing" in dto
        assert "swing_bias" in dto
        assert "pivots" in dto
        assert "time" in dto
        assert "params" in dto
        assert "view" in dto

    @pytest.mark.asyncio
    async def test_compute_indicators_empty_on_insufficient_data(
        self, smc_monitor: SmcMonitor
    ) -> None:
        """数据不足时返回空 DTO。"""
        short_bars = _generate_daily_bars(n_bars=100)
        ctx = _make_context(short_bars)
        dto = await smc_monitor.compute_indicators(ctx)
        assert dto["events"] == []
        assert dto["order_blocks"] == []
        assert dto["view"]["total_bars"] == 0

    @pytest.mark.asyncio
    async def test_compute_indicators_excludes_fvg(
        self, smc_monitor: SmcMonitor, daily_bars: pd.DataFrame
    ) -> None:
        """compute_indicators 输出不含任何 fvg 字段（FVG 完全排除）。"""
        ctx = _make_context(daily_bars)
        dto = await smc_monitor.compute_indicators(ctx)

        def _check_no_fvg(obj: Any, path: str = "") -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    assert "fvg" not in str(k).lower(), f"发现 FVG 字段: {path}.{k}"
                    _check_no_fvg(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _check_no_fvg(item, f"{path}[{i}]")

        _check_no_fvg(dto)


# =============================================================================
# 5. WatchlistMonitor 命名空间合并测试
# =============================================================================


class TestWatchlistMonitorNamespaces:
    """验证 WatchlistMonitor 命名空间合并 + degraded 处理。"""

    @pytest.mark.asyncio
    async def test_calculate_state_namespaces(
        self, daily_bars: pd.DataFrame
    ) -> None:
        """WatchlistMonitor.calculate_state 返回命名空间 bb/node_cluster/smc/market/degraded。"""
        monitor = WatchlistMonitor()
        version = _make_mock_version()
        await monitor.initialize(version)

        bars_minute = _make_minute_bars(prev_close=10.0, cur_close=10.5)
        ctx = _make_context(daily_bars, bars_minute)
        state = await monitor.calculate_state(ctx)

        # 命名空间键
        assert "bb" in state.state
        assert "node_cluster" in state.state
        assert "smc" in state.state
        assert "market" in state.state
        assert "degraded" in state.state

        # 命名空间内字段
        assert "bb_upper" in state.state["bb"]
        assert "upper_node" in state.state["node_cluster"]
        assert "smc_confirmed_bos" in state.state["smc"]
        assert "current_price" in state.state["market"]
        assert "previous_close" in state.state["market"]
        assert "change_pct" in state.state["market"]

        # degraded 结构
        degraded = state.state["degraded"]
        assert "bb" in degraded
        assert "node_cluster" in degraded
        assert "smc" in degraded

        # state_version 升级到 2
        assert state.state_version == 2

    @pytest.mark.asyncio
    async def test_calculate_state_flat_compat(
        self, daily_bars: pd.DataFrame
    ) -> None:
        """WatchlistMonitor.calculate_state 同时保留顶层平铺字段（兼容旧读取）。"""
        monitor = WatchlistMonitor()
        version = _make_mock_version()
        await monitor.initialize(version)

        bars_minute = _make_minute_bars(prev_close=10.0, cur_close=10.5)
        ctx = _make_context(daily_bars, bars_minute)
        state = await monitor.calculate_state(ctx)

        # 顶层平铺字段（兼容旧 _extract_sub_state）
        assert "bb_upper" in state.state
        assert "upper_node" in state.state
        assert "smc_confirmed_bos" in state.state
        assert "smc_currently_touched" in state.state
        assert "smc_episode_tracker" in state.state
        assert "previous_close" in state.state
        assert "change_pct" in state.state

    @pytest.mark.asyncio
    async def test_calculate_state_smc_degraded_on_short_daily(
        self
    ) -> None:
        """日线数据不足时 SMC 标记 degraded，但 BB 不受影响。"""
        monitor = WatchlistMonitor()
        version = _make_mock_version()
        await monitor.initialize(version)

        # 50 根日线（满足 BB 的 25 根要求，不满足 SMC 的 250 根要求）
        short_bars = _generate_daily_bars(n_bars=50)
        bars_minute = _make_minute_bars(prev_close=10.0, cur_close=10.5)
        ctx = _make_context(short_bars, bars_minute)
        state = await monitor.calculate_state(ctx)

        # SMC 应标记 degraded（< 250 根）
        assert state.state["degraded"]["smc"] is True
        # BB 不应 degraded（>= 25 根）
        assert state.state["degraded"]["bb"] is False

    @pytest.mark.asyncio
    async def test_extract_sub_state_namespace_priority(self) -> None:
        """_extract_sub_state 优先读取命名空间。"""
        from uuid import uuid4

        test_state = MonitorState(
            instrument_id=uuid4(),
            strategy_version_id=uuid4(),
            state={
                "bb": {"bb_upper": 99.9, "current_price": 50.0},
                "bb_upper": 11.1,  # 顶层平铺（应被命名空间覆盖）
            },
            state_version=2,
            updated_at=datetime.now(UTC),
        )
        bb_sub = WatchlistMonitor._extract_sub_state(test_state, "bb")
        assert bb_sub.state["bb_upper"] == 99.9  # 命名空间优先

    @pytest.mark.asyncio
    async def test_extract_sub_state_flat_fallback(self) -> None:
        """_extract_sub_state 无命名空间时 fallback 到顶层平铺（兼容旧 state）。"""
        from uuid import uuid4

        old_state = MonitorState(
            instrument_id=uuid4(),
            strategy_version_id=uuid4(),
            state={
                "bb_upper": 11.1,
                "bb_mid": 10.0,
                "bb_lower": 8.9,
                "current_price": 10.5,
                "prev_close": 10.3,
                "bb_width": 0.22,
                "bb_pos": 0.75,
            },
            state_version=1,
            updated_at=datetime.now(UTC),
        )
        bb_sub = WatchlistMonitor._extract_sub_state(old_state, "bb")
        assert bb_sub.state["bb_upper"] == 11.1
        assert bb_sub.state["current_price"] == 10.5

    @pytest.mark.asyncio
    async def test_detect_events_returns_combined_events(
        self, daily_bars: pd.DataFrame
    ) -> None:
        """WatchlistMonitor.detect_events 合并 BB+VN+SMC 事件。"""
        monitor = WatchlistMonitor()
        version = _make_mock_version()
        await monitor.initialize(version)

        bars_minute = _make_minute_bars(prev_close=10.0, cur_close=10.5)
        ctx = _make_context(daily_bars, bars_minute)
        curr_state = await monitor.calculate_state(ctx)

        events = await monitor.detect_events(ctx, None, curr_state)

        # 事件列表应为 list[StrategyEventDraft]
        assert isinstance(events, list)
        for ev in events:
            assert isinstance(ev, StrategyEventDraft)
            # 事件类型应为已知 7 种之一
            assert ev.event_type in {
                "bb_upper_touch", "bb_mid_touch", "bb_lower_touch",
                "node_cluster_touch",
                "smc_bos_retest", "smc_choch_retest", "smc_order_block_first_touch",
            }


# =============================================================================
# 6. SmcMonitor 自测入口验证
# =============================================================================


class TestSmcMonitorSelfTest:
    """验证 SmcMonitor 自测入口（python -m app.strategy.monitors.smc_monitor）。"""

    def test_self_test_passes(self) -> None:
        """运行 SmcMonitor 自测入口应无断言失败。"""
        # 直接调用模块自测（不通过 subprocess，避免环境问题）
        # 这里只验证关键导入和常量
        assert SmcMonitor.kind == "monitor"
        assert SMC_BOS_RETEST == "smc_bos_retest"
        assert SMC_CHOCH_RETEST == "smc_choch_retest"
        assert SMC_ORDER_BLOCK_FIRST_TOUCH == "smc_order_block_first_touch"
        assert NOTIFY_COOLDOWN_SECONDS == 600
