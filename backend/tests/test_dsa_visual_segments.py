"""测试 DSA 指标返回时间与 visual_segments（Task 3）。

验证 DSASelector.compute_indicators 重构后：
1. 返回 time 字段（list[str]，ISO 日期字符串）
2. 返回 visual_segments 字段（list[dict]，Pine polyline 契约）
3. time 数组长度等于 factor_per_bar 行数
4. time 数组格式为 YYYY-MM-DD
5. visual_segments 的 direction 为 1 或 -1
6. visual_segments 的 points 包含 time 和 value
7. visual_segments 的 points.time 格式为 YYYY-MM-DD
8. visual_segments 的 points.value 为有限 float
9. time 数组与 source_bar_times 一致（长度和内容）
10. visual_segments 与底层算法 segments 逐点一致

用法:
    APP_ENV=test backend/.venv/bin/python -m pytest backend/tests/test_dsa_visual_segments.py -v
"""
from __future__ import annotations

import math
import re
import uuid
from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.constants.indicator_contract import DSA_LOOKBACK
from app.strategy.runtime import MarketDataContext
from app.strategy.selectors.dsa_selector import (
    MIN_DIR_BARS,
    DSASelector,
    compute_dsa_bundle,
)
from app.strategy_assets.algorithms.features.atr_rope_event_factor_lab_v4 import (
    ATRRopeConfig,
)
from app.strategy_assets.algorithms.features.dynamic_swing_anchored_vwap import (
    DSAConfig,
    dynamic_swing_anchored_vwap,
)

# ---------------------------------------------------------------------------
# 测试数据与配置工厂
# ---------------------------------------------------------------------------


def _build_synthetic_bars(n: int = 250, seed: int = 42, start_price: float = 10.0) -> pd.DataFrame:
    """构造前复权日线 DataFrame（250 根，匹配生产 load_chart_bars count=250）。

    与 test_dsa_factor_visual_separation._build_synthetic_bars 同实现，确保可对比。
    """
    assert n >= 60
    np.random.seed(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="B")

    daily_returns = np.random.uniform(0.003, 0.008, size=n)
    daily_returns[::7] = -0.002
    close = start_price * np.cumprod(1 + daily_returns)
    close = np.maximum(close, 1.0)

    open_ = close * (1 + np.random.uniform(-0.005, 0.005, size=n))
    high = np.maximum(open_, close) * (1 + np.random.uniform(0.001, 0.01, size=n))
    low = np.minimum(open_, close) * (1 - np.random.uniform(0.001, 0.01, size=n))
    volume = np.random.uniform(500000, 2000000, size=n)
    amount = volume * close

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        },
        index=idx,
    )


def _make_mock_version(lookback: int = DSA_LOOKBACK) -> MagicMock:
    """创建 mock StrategyVersion（lookback=DSA_LOOKBACK=250，与生产口径一致）。"""
    version = MagicMock()
    version.id = uuid.uuid4()
    version.manifest = {
        "strategy_id": "dsa_selector",
        "kind": "selector",
        "version": "1.1.0",
        "parameters": [{"key": "algorithm.lookback", "type": "integer", "default": lookback}],
        "resource_budget": {"target_ms_per_instrument": 5000},
    }
    return version


def _build_config(lookback: int | None = DSA_LOOKBACK) -> dict:
    """构建与 DSASelector._build_history_config 一致的测试配置。"""
    return {
        "dsa_config": DSAConfig(),
        "rope_config": ATRRopeConfig(regime_lookback=55),
        "min_dir_bars": MIN_DIR_BARS,
        "lookback": lookback,
    }


# ---------------------------------------------------------------------------
# 共享 fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def shared_bars() -> pd.DataFrame:
    """共享合成行情（250 根，seed=42，匹配生产 load_chart_bars count=250）。"""
    return _build_synthetic_bars(n=DSA_LOOKBACK)


@pytest.fixture(scope="module")
def shared_bundle(shared_bars: pd.DataFrame) -> dict:
    """共享 compute_dsa_bundle 结果（用于对比 compute_indicators 输出）。"""
    return compute_dsa_bundle(shared_bars, _build_config())


@pytest.fixture(scope="module")
def shared_algorithm_segments(shared_bars: pd.DataFrame) -> list[dict]:
    """共享底层算法直接返回的 segments（与 bundle 内部使用相同截断与配置）。"""
    cfg = DSAConfig()
    lookback = DSA_LOOKBACK
    df = shared_bars.copy()
    if lookback is not None and len(df) > lookback:
        df = df.tail(lookback)
    _, _, _, segments = dynamic_swing_anchored_vwap(df, cfg)
    return segments


@pytest.fixture
async def dsa_selector() -> DSASelector:
    """已初始化的 DSASelector 实例（lookback=DSA_LOOKBACK=250）。"""
    selector = DSASelector()
    version = _make_mock_version(lookback=DSA_LOOKBACK)
    await selector.initialize(version)
    return selector


@pytest.fixture
def daily_context(shared_bars: pd.DataFrame) -> MarketDataContext:
    """日线 MarketDataContext（250 根，匹配生产 load_chart_bars count=250）。"""
    return MarketDataContext(
        instrument_id=uuid.uuid4(),
        symbol="600519",
        bars_daily=shared_bars,
        trade_date=date(2026, 6, 18),
    )


@pytest.fixture
async def indicators_result(
    dsa_selector: DSASelector, daily_context: MarketDataContext
) -> dict:
    """共享 compute_indicators 计算结果。"""
    return await dsa_selector.compute_indicators(daily_context)


# ---------------------------------------------------------------------------
# 测试 1-2: compute_indicators 返回 time 和 visual_segments 字段
# ---------------------------------------------------------------------------


class TestComputeIndicatorsReturnsNewFields:
    """验证 compute_indicators 返回 time 和 visual_segments 字段。"""

    @pytest.mark.asyncio
    async def test_returns_time_field(self, indicators_result: dict):
        """测试 1: compute_indicators 返回 time 字段（list[str]）。"""
        assert "time" in indicators_result, "compute_indicators 必须返回 time 字段"
        assert isinstance(indicators_result["time"], list)
        assert len(indicators_result["time"]) > 0
        for t in indicators_result["time"]:
            assert isinstance(t, str), f"time 元素必须为字符串，实际 {type(t)}"

    @pytest.mark.asyncio
    async def test_returns_visual_segments_field(self, indicators_result: dict):
        """测试 2: compute_indicators 返回 visual_segments 字段（list[dict]）。"""
        assert "visual_segments" in indicators_result, \
            "compute_indicators 必须返回 visual_segments 字段"
        assert isinstance(indicators_result["visual_segments"], list)
        assert len(indicators_result["visual_segments"]) > 0, \
            "visual_segments 不应为空（250 根行情应产生 segment）"
        for seg in indicators_result["visual_segments"]:
            assert isinstance(seg, dict)


# ---------------------------------------------------------------------------
# 测试 3-4: time 数组长度与格式
# ---------------------------------------------------------------------------


class TestTimeArrayFormat:
    """验证 time 数组长度与格式。"""

    @pytest.mark.asyncio
    async def test_time_length_equals_factor_per_bar(
        self, indicators_result: dict, shared_bundle: dict
    ):
        """测试 3: time 数组长度等于 factor_per_bar 行数。"""
        expected_len = len(shared_bundle["factor_per_bar"])
        assert len(indicators_result["time"]) == expected_len, (
            f"time 长度应等于 factor_per_bar 行数: "
            f"actual={len(indicators_result['time'])} vs expected={expected_len}"
        )

    @pytest.mark.asyncio
    async def test_time_format_is_iso_date(self, indicators_result: dict):
        """测试 4: time 数组格式为 ISO 日期字符串（YYYY-MM-DD）。"""
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for t in indicators_result["time"]:
            assert pattern.match(t), f"time 应为 YYYY-MM-DD 格式，实际 {t}"


# ---------------------------------------------------------------------------
# 测试 5-8: visual_segments 格式
# ---------------------------------------------------------------------------


class TestVisualSegmentsFormat:
    """验证 visual_segments 格式（direction/points/time/value）。"""

    @pytest.mark.asyncio
    async def test_segment_direction_is_valid(self, indicators_result: dict):
        """测试 5: visual_segments 的 direction 为 1 或 -1。"""
        for seg in indicators_result["visual_segments"]:
            assert "direction" in seg, f"segment 缺少 direction: {seg.keys()}"
            assert seg["direction"] in (1, -1), (
                f"direction 必须为 1 或 -1，实际 {seg['direction']}"
            )

    @pytest.mark.asyncio
    async def test_segment_points_have_time_and_value(self, indicators_result: dict):
        """测试 6: visual_segments 的 points 包含 time 和 value。"""
        for seg in indicators_result["visual_segments"]:
            assert "points" in seg, f"segment 缺少 points: {seg.keys()}"
            assert isinstance(seg["points"], list)
            assert len(seg["points"]) >= 2, "每个 segment 至少 2 个点"
            for pt in seg["points"]:
                assert "time" in pt, f"point 缺少 time: {pt}"
                assert "value" in pt, f"point 缺少 value: {pt}"

    @pytest.mark.asyncio
    async def test_segment_points_time_is_iso_date(self, indicators_result: dict):
        """测试 7: visual_segments 的 points.time 格式为 YYYY-MM-DD。"""
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for seg in indicators_result["visual_segments"]:
            for pt in seg["points"]:
                assert isinstance(pt["time"], str), \
                    f"points.time 必须为字符串，实际 {type(pt['time'])}"
                assert pattern.match(pt["time"]), \
                    f"points.time 应为 YYYY-MM-DD 格式，实际 {pt['time']}"

    @pytest.mark.asyncio
    async def test_segment_points_value_is_finite_float(self, indicators_result: dict):
        """测试 8: visual_segments 的 points.value 为有限 float。"""
        for seg in indicators_result["visual_segments"]:
            for pt in seg["points"]:
                assert isinstance(pt["value"], float), \
                    f"points.value 必须为 float，实际 {type(pt['value'])}"
                assert math.isfinite(pt["value"]), \
                    f"points.value 必须有限，实际 {pt['value']}"


# ---------------------------------------------------------------------------
# 测试 9: time 数组与 source_bar_times 一致
# ---------------------------------------------------------------------------


class TestTimeMatchesSourceBarTimes:
    """验证 time 数组与 source_bar_times 一致（长度和内容）。

    生产场景：load_chart_bars(count=250) 返回 250 根日线，
    indicator_service 计算 source_bar_times = [idx.strftime("%Y-%m-%d") for idx in daily_bars.index]，
    compute_indicators 接收同一份 daily_bars（250 根），lookback=250 不截断，
    factor_per_bar.index == daily_bars.index，time 应与 source_bar_times 完全一致。
    """

    @pytest.mark.asyncio
    async def test_time_matches_source_bar_times(
        self, indicators_result: dict, shared_bars: pd.DataFrame
    ):
        """测试 9: time 数组与 source_bar_times 一致（长度和内容）。"""
        # 模拟 indicator_service 计算的 source_bar_times
        source_bar_times = [idx.strftime("%Y-%m-%d") for idx in shared_bars.index]

        assert len(indicators_result["time"]) == len(source_bar_times), (
            f"time 长度应与 source_bar_times 一致: "
            f"actual={len(indicators_result['time'])} vs expected={len(source_bar_times)}"
        )
        assert indicators_result["time"] == source_bar_times, (
            "time 数组内容应与 source_bar_times 完全一致"
        )


# ---------------------------------------------------------------------------
# 测试 10: visual_segments 与底层算法 segments 逐点一致
# ---------------------------------------------------------------------------


class TestVisualSegmentsMatchAlgorithm:
    """验证 visual_segments 与底层算法 segments 逐点一致。"""

    @pytest.mark.asyncio
    async def test_visual_segments_match_algorithm(
        self, indicators_result: dict, shared_algorithm_segments: list[dict]
    ):
        """测试 10: visual_segments 与底层算法 segments 逐点一致。"""
        result_segs = indicators_result["visual_segments"]
        algo_segs = shared_algorithm_segments

        assert len(result_segs) == len(algo_segs), (
            f"segment 数量不一致: result={len(result_segs)} vs algo={len(algo_segs)}"
        )

        for i, (r_seg, a_seg) in enumerate(zip(result_segs, algo_segs)):
            assert r_seg["direction"] == a_seg["direction"], (
                f"segment[{i}] direction 不一致: "
                f"result={r_seg['direction']} vs algo={a_seg['direction']}"
            )
            assert len(r_seg["points"]) == len(a_seg["points"]), (
                f"segment[{i}] points 数量不一致: "
                f"result={len(r_seg['points'])} vs algo={len(a_seg['points'])}"
            )
            for j, (r_pt, a_pt) in enumerate(zip(r_seg["points"], a_seg["points"])):
                assert r_pt["time"] == a_pt["time"], (
                    f"segment[{i}].points[{j}] time 不一致: "
                    f"result={r_pt['time']} vs algo={a_pt['time']}"
                )
                assert math.isclose(
                    r_pt["value"], a_pt["value"], rel_tol=1e-12, abs_tol=1e-12
                ), (
                    f"segment[{i}].points[{j}] value 不一致: "
                    f"result={r_pt['value']} vs algo={a_pt['value']}"
                )


# ---------------------------------------------------------------------------
# 边界：数据不足时返回结构完整
# ---------------------------------------------------------------------------


class TestComputeIndicatorsEdgeCases:
    """验证数据不足时 compute_indicators 返回结构完整（含新字段）。"""

    @pytest.mark.asyncio
    async def test_insufficient_data_returns_empty_with_new_fields(self):
        """数据 < prd 时返回空结构（含 time 和 visual_segments 字段）。"""
        selector = DSASelector()
        await selector.initialize(_make_mock_version())

        # 只有 30 bars（不足 60 根最小要求）：取前 30 根避免触发 _build_synthetic_bars 的 n>=60 断言
        short_bars = _build_synthetic_bars(n=250).head(30)
        context = MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="600519",
            bars_daily=short_bars,
            trade_date=date(2026, 6, 18),
        )
        result = await selector.compute_indicators(context)

        # 空结构应包含 time 和 visual_segments 字段
        assert "time" in result, "空结构也必须包含 time 字段"
        assert "visual_segments" in result, "空结构也必须包含 visual_segments 字段"
        assert result["time"] == []
        assert result["visual_segments"] == []


# ---------------------------------------------------------------------------
# 模块自测入口
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import asyncio

    async def _self_test():
        bars = _build_synthetic_bars(n=DSA_LOOKBACK)
        selector = DSASelector()
        await selector.initialize(_make_mock_version())
        ctx = MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="600519",
            bars_daily=bars,
            trade_date=date(2026, 6, 18),
        )
        result = await selector.compute_indicators(ctx)

        # 验证新字段
        assert "time" in result, "缺少 time 字段"
        assert "visual_segments" in result, "缺少 visual_segments 字段"

        # 验证 time
        print(f"time 长度: {len(result['time'])}")
        print(f"time 前 5 个: {result['time'][:5]}")

        # 验证 visual_segments
        segs = result["visual_segments"]
        print(f"visual_segments 数量: {len(segs)}")
        if segs:
            seg0 = segs[0]
            print(f"第一个 segment: direction={seg0['direction']}, points={len(seg0['points'])}")
            if seg0["points"]:
                pt0 = seg0["points"][0]
                print(f"第一个 point: time={pt0['time']}, value={pt0['value']}")

        print("Task 3 自测通过 ✓")

    asyncio.run(_self_test())
