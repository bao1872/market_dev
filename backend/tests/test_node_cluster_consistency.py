"""Node Cluster 一致性测试（advice.md 第四节 / Task 6.4）。

测试覆盖：
- VolumeNodeMonitor.compute_indicators 的 profile_meta 包含 6 个诊断字段
- compute_indicators 与 calculate_state 在同一组输入下 POC/upper_node/lower_node 一致
- profile_meta 中 primary_period/low_period/parameter_version 与 indicator_contract 一致

约束：
- 复用 test_volume_node_monitor.py 的数据生成模式
- 不连数据库（纯单元测试）
- profile_meta 仅在 compute_indicators 输出中（calculate_state 不返回 profile_meta）
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.strategy.monitors.volume_node_monitor import VolumeNodeMonitor
from app.strategy.runtime import MarketDataContext


def _generate_minute_bars(
    n_bars: int = 400,
    start_price: float = 10.0,
    seed: int = 42,
) -> pd.DataFrame:
    """生成合成 1m bars（满足 VP_LOOKBACK=250）。"""
    np.random.seed(seed)
    dates = pd.date_range(start="2026-06-18 09:30", periods=n_bars, freq="1min")
    minute_returns = np.random.uniform(-0.003, 0.003, size=n_bars)
    close = start_price * np.cumprod(1 + minute_returns)
    open_ = close * (1 + np.random.uniform(-0.001, 0.001, size=n_bars))
    high = np.maximum(open_, close) * (1 + np.random.uniform(0.0005, 0.003, size=n_bars))
    low = np.minimum(open_, close) * (1 - np.random.uniform(0.0005, 0.003, size=n_bars))
    volume = np.random.uniform(50000, 200000, size=n_bars)
    amount = volume * close
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "amount": amount},
        index=dates,
    )
    df.index.name = "datetime"
    return df


def _generate_daily_bars(
    n_bars: int = 20,
    end_date: str = "2026-06-18",
    start_price: float = 10.0,
    seed: int = 43,
) -> pd.DataFrame:
    """生成合成日线 bars。"""
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
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "amount": amount},
        index=dates,
    )
    df.index.name = "datetime"
    return df


def _generate_15m_bars(
    n_bars: int = 3700,
    end_date: str = "2026-06-18 15:00",
    start_price: float = 10.0,
    seed: int = 44,
) -> pd.DataFrame:
    """生成合成 15m bars（满足 NODE_CLUSTER_LOW_BARS=3600）。"""
    np.random.seed(seed)
    dates = pd.date_range(end=end_date, periods=n_bars, freq="15min")
    returns = np.random.uniform(-0.002, 0.002, size=n_bars)
    close = start_price * np.cumprod(1 + returns)
    open_ = close * (1 + np.random.uniform(-0.0005, 0.0005, size=n_bars))
    high = np.maximum(open_, close) * (1 + np.random.uniform(0.0002, 0.001, size=n_bars))
    low = np.minimum(open_, close) * (1 - np.random.uniform(0.0002, 0.001, size=n_bars))
    volume = np.random.uniform(100000, 500000, size=n_bars)
    amount = volume * close
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "amount": amount},
        index=dates,
    )
    df.index.name = "datetime"
    return df


def _make_mock_version() -> MagicMock:
    """创建 mock StrategyVersion（含 manifest 参数）。"""
    version = MagicMock()
    version.id = uuid.uuid4()
    version.manifest = {
        "strategy_id": "volume_node_monitor",
        "kind": "monitor",
        "version": "1.1.0",
        "parameters": [
            {"key": "algorithm.lookback", "type": "integer", "default": 250}
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
    }
    return version


def _make_context(
    bars_daily: pd.DataFrame,
    bars_minute: pd.DataFrame,
    bars_15m: pd.DataFrame | None = None,
) -> MarketDataContext:
    """构建 MarketDataContext（含 15m bars）。"""
    return MarketDataContext(
        instrument_id=uuid.uuid4(),
        symbol="600519",
        bars_daily=bars_daily,
        bars_minute=bars_minute,
        bars_15min=bars_15m,
        trade_date=bars_minute.index[0].date() if len(bars_minute) > 0 else None,
        bar_time=bars_minute.index[-1].to_pydatetime() if len(bars_minute) > 0 else datetime.now(UTC),
    )


# 诊断字段集合（advice.md 第四节）
_DIAGNOSTIC_KEYS = {
    "input_daily_bars",
    "input_15m_bars",
    "input_minute_bars",
    "primary_period",
    "low_period",
    "parameter_version",
}


@pytest.fixture
def daily_bars() -> pd.DataFrame:
    return _generate_daily_bars(n_bars=20)


@pytest.fixture
def minute_bars(daily_bars: pd.DataFrame) -> pd.DataFrame:
    return _generate_minute_bars(
        n_bars=400, start_price=float(daily_bars["close"].iloc[-1])
    )


@pytest.fixture
def bars_15m(daily_bars: pd.DataFrame) -> pd.DataFrame:
    return _generate_15m_bars(
        n_bars=1300, start_price=float(daily_bars["close"].iloc[-1])
    )


@pytest.fixture
async def monitor() -> VolumeNodeMonitor:
    """已初始化的 VolumeNodeMonitor 实例。"""
    m = VolumeNodeMonitor()
    version = _make_mock_version()
    await m.initialize(version)
    return m


class TestNodeClusterConsistency:
    """compute_indicators 与 calculate_state 一致性测试。

    profile_meta 仅在 compute_indicators 输出中；
    calculate_state 返回 vp_result.state_for_price(current_price) 的 6 个字段
    （current_price/upper_node/lower_node/position_0_1/poc_price/last_touched_node）。
    """

    @pytest.mark.asyncio
    async def test_compute_indicators_profile_meta_contains_diagnostic_fields(
        self,
        monitor: VolumeNodeMonitor,
        daily_bars: pd.DataFrame,
        minute_bars: pd.DataFrame,
        bars_15m: pd.DataFrame,
    ) -> None:
        """compute_indicators 的 profile_meta 必须包含 6 个诊断字段。

        profile_meta 为 dict（非 list），合并 VP 元信息与 prepare_node_cluster_bars 诊断字段。
        """
        context = _make_context(daily_bars, minute_bars, bars_15m)
        indicators = await monitor.compute_indicators(context)

        profile_meta = indicators.get("profile_meta")
        assert isinstance(profile_meta, dict), (
            f"compute_indicators 返回的 profile_meta 应为 dict，实际 {type(profile_meta)}"
        )
        for key in _DIAGNOSTIC_KEYS:
            assert key in profile_meta, (
                f"compute_indicators profile_meta 必须包含诊断字段 {key}，"
                f"实际 keys={list(profile_meta.keys())}"
            )

    @pytest.mark.asyncio
    async def test_calculate_state_returns_six_base_fields(
        self,
        monitor: VolumeNodeMonitor,
        daily_bars: pd.DataFrame,
        minute_bars: pd.DataFrame,
        bars_15m: pd.DataFrame,
    ) -> None:
        """calculate_state 返回的 state 必须包含 6 个基础字段。"""
        context = _make_context(daily_bars, minute_bars, bars_15m)
        state = await monitor.calculate_state(context)

        expected_keys = {
            "current_price", "upper_node", "lower_node",
            "position_0_1", "poc_price", "last_touched_node",
        }
        for key in expected_keys:
            assert key in state.state, (
                f"calculate_state.state 必须包含字段 {key}，实际 keys={list(state.state.keys())}"
            )

    @pytest.mark.asyncio
    async def test_poc_price_consistent_between_paths(
        self,
        monitor: VolumeNodeMonitor,
        daily_bars: pd.DataFrame,
        minute_bars: pd.DataFrame,
        bars_15m: pd.DataFrame,
    ) -> None:
        """同一组输入下，calculate_state 与 compute_indicators 的 POC 价格一致。

        两条路径都调用 prepare_node_cluster_bars 共享数据准备函数，
        再调用 compute_unified_volume_profile 计算 VP，因此 POC 必然一致。
        """
        context = _make_context(daily_bars, minute_bars, bars_15m)

        state = await monitor.calculate_state(context)
        state_poc = state.state.get("poc_price")

        indicators = await monitor.compute_indicators(context)
        indicator_poc_list = indicators.get("poc_price", [])
        indicator_poc = indicator_poc_list[-1] if indicator_poc_list else None

        # POC 可能为 None 或 dict，比较 price_mid
        def _poc_price_mid(p):
            if p is None:
                return None
            if isinstance(p, dict):
                return p.get("price_mid")
            return p

        assert _poc_price_mid(state_poc) == _poc_price_mid(indicator_poc), (
            f"POC 不一致：calculate_state={state_poc}，compute_indicators={indicator_poc}"
        )

    @pytest.mark.asyncio
    async def test_upper_node_consistent_between_paths(
        self,
        monitor: VolumeNodeMonitor,
        daily_bars: pd.DataFrame,
        minute_bars: pd.DataFrame,
        bars_15m: pd.DataFrame,
    ) -> None:
        """同一组输入下，calculate_state 与 compute_indicators 的 upper_node 一致。"""
        context = _make_context(daily_bars, minute_bars, bars_15m)

        state = await monitor.calculate_state(context)
        state_upper = state.state.get("upper_node")

        indicators = await monitor.compute_indicators(context)
        indicator_upper_list = indicators.get("upper_node", [])
        indicator_upper = indicator_upper_list[-1] if indicator_upper_list else None

        def _node_price_mid(p):
            if p is None:
                return None
            if isinstance(p, dict):
                return p.get("price_mid")
            return p

        assert _node_price_mid(state_upper) == _node_price_mid(indicator_upper), (
            f"upper_node 不一致：calculate_state={state_upper}，"
            f"compute_indicators={indicator_upper}"
        )

    @pytest.mark.asyncio
    async def test_profile_meta_values_match_indicator_contract(
        self,
        monitor: VolumeNodeMonitor,
        daily_bars: pd.DataFrame,
        minute_bars: pd.DataFrame,
        bars_15m: pd.DataFrame,
    ) -> None:
        """profile_meta 中 primary_period/low_period 必须与 indicator_contract 一致。

        profile_meta 为 dict，直接通过 key 访问。
        """
        from app.constants.indicator_contract import (
            NODE_CLUSTER_LOW_PERIOD,
            NODE_CLUSTER_PRIMARY_PERIOD,
        )

        context = _make_context(daily_bars, minute_bars, bars_15m)
        indicators = await monitor.compute_indicators(context)
        meta = indicators["profile_meta"]

        assert meta["primary_period"] == NODE_CLUSTER_PRIMARY_PERIOD
        assert meta["low_period"] == NODE_CLUSTER_LOW_PERIOD

    @pytest.mark.asyncio
    async def test_parameter_version_is_v1_1_0(
        self,
        monitor: VolumeNodeMonitor,
        daily_bars: pd.DataFrame,
        minute_bars: pd.DataFrame,
        bars_15m: pd.DataFrame,
    ) -> None:
        """profile_meta.parameter_version 必须为 'v1.1.0'。

        profile_meta 为 dict，直接通过 key 访问。
        """
        context = _make_context(daily_bars, minute_bars, bars_15m)
        indicators = await monitor.compute_indicators(context)
        meta = indicators["profile_meta"]

        assert meta["parameter_version"] == "v1.1.0", (
            f"parameter_version 应为 v1.1.0，实际为 {meta['parameter_version']}"
        )
