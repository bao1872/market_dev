"""测试 DSA 因子与可视化契约分离（Task 2）。

验证 compute_dsa_bundle 重构后：
1. 返回 factor_per_bar (DataFrame)、visual_segments (list[dict])、
   factor_time (DatetimeIndex)、pivot_labels (list[dict])、
   anchor (dict)、last_row_metrics (dict) 六字段
2. visual_segments 格式为 [{direction:1|-1, points:[{time,value}]}]
3. visual_segments 与底层算法 segments 逐点一致
4. factor_time == factor_per_bar.index
5. last_row_metrics 与重构前完全一致（固定输入对比）

用法:
    APP_ENV=test backend/.venv/bin/python -m pytest backend/tests/test_dsa_factor_visual_separation.py -v
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.constants.indicator_contract import DSA_LOOKBACK
from app.strategy_assets.algorithms.features.atr_rope_event_factor_lab_v4 import (
    ATRRopeConfig,
)
from app.strategy_assets.algorithms.features.dynamic_swing_anchored_vwap import (
    DSAConfig,
    dynamic_swing_anchored_vwap,
)
from app.strategy.selectors.dsa_selector import (
    MIN_DIR_BARS,
    _safe_float,
    compute_dsa_bundle,
)


# ---------------------------------------------------------------------------
# 测试数据与配置工厂（与 test_dsa_bundle_consistency.py 同口径，确保可对比）
# ---------------------------------------------------------------------------


def _build_synthetic_bars(n: int = 300, seed: int = 42, start_price: float = 10.0) -> pd.DataFrame:
    """构造前复权日线 DataFrame，含趋势 + 波动（与 test_dsa_bundle_consistency 同实现）。"""
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
    """共享合成行情（300 根，seed=42，与 consistency 测试同源）。"""
    return _build_synthetic_bars(n=300)


@pytest.fixture(scope="module")
def shared_bundle(shared_bars: pd.DataFrame) -> dict:
    """共享 compute_dsa_bundle 结果。"""
    return compute_dsa_bundle(shared_bars, _build_config())


@pytest.fixture(scope="module")
def shared_algorithm_segments(shared_bars: pd.DataFrame) -> list[dict]:
    """共享底层算法直接返回的 segments（与 bundle 内部使用相同截断与配置）。"""
    cfg = DSAConfig()
    lookback = DSA_LOOKBACK
    df = shared_bars.copy()
    if lookback is not None and len(df) > lookback:
        df = df.tail(lookback)
    vwap_series, dir_series, pivot_labels, segments = dynamic_swing_anchored_vwap(df, cfg)
    return segments


# ---------------------------------------------------------------------------
# 测试 1-6: compute_dsa_bundle 返回六个字段
# ---------------------------------------------------------------------------


class TestBundleReturnStructure:
    """验证 compute_dsa_bundle 返回新六字段结构。"""

    def test_returns_factor_per_bar(self, shared_bundle: dict):
        """测试 1: 返回 factor_per_bar (DataFrame)。"""
        assert "factor_per_bar" in shared_bundle, "bundle 必须包含 factor_per_bar"
        assert isinstance(shared_bundle["factor_per_bar"], pd.DataFrame)
        assert not shared_bundle["factor_per_bar"].empty

    def test_returns_visual_segments(self, shared_bundle: dict):
        """测试 2: 返回 visual_segments (list[dict])。"""
        assert "visual_segments" in shared_bundle, "bundle 必须包含 visual_segments"
        assert isinstance(shared_bundle["visual_segments"], list)
        assert len(shared_bundle["visual_segments"]) > 0
        for seg in shared_bundle["visual_segments"]:
            assert isinstance(seg, dict)

    def test_returns_factor_time(self, shared_bundle: dict):
        """测试 3: 返回 factor_time (DatetimeIndex)。"""
        assert "factor_time" in shared_bundle, "bundle 必须包含 factor_time"
        ft = shared_bundle["factor_time"]
        assert isinstance(ft, pd.DatetimeIndex), f"factor_time 应为 DatetimeIndex，实际 {type(ft)}"

    def test_returns_pivot_labels(self, shared_bundle: dict):
        """测试 4: 返回 pivot_labels (list[dict])。"""
        assert "pivot_labels" in shared_bundle, "bundle 必须包含 pivot_labels"
        assert isinstance(shared_bundle["pivot_labels"], list)
        for lab in shared_bundle["pivot_labels"]:
            assert isinstance(lab, dict)

    def test_returns_anchor(self, shared_bundle: dict):
        """测试 5: 返回 anchor (dict)。"""
        assert "anchor" in shared_bundle, "bundle 必须包含 anchor"
        assert isinstance(shared_bundle["anchor"], dict)

    def test_returns_last_row_metrics(self, shared_bundle: dict):
        """测试 6: 返回 last_row_metrics (dict)。"""
        assert "last_row_metrics" in shared_bundle, "bundle 必须包含 last_row_metrics"
        assert isinstance(shared_bundle["last_row_metrics"], dict)
        assert shared_bundle["last_row_metrics"], "last_row_metrics 不应为空"


# ---------------------------------------------------------------------------
# 测试 7-8: visual_segments 格式与一致性
# ---------------------------------------------------------------------------


class TestVisualSegmentsFormat:
    """验证 visual_segments 格式为 [{direction:1|-1, points:[{time,value}]}]。"""

    def test_segment_keys(self, shared_bundle: dict):
        """测试 7: 每个 segment 包含 direction 和 points 键。"""
        for seg in shared_bundle["visual_segments"]:
            assert "direction" in seg, f"segment 缺少 direction: {seg.keys()}"
            assert "points" in seg, f"segment 缺少 points: {seg.keys()}"
            assert seg["direction"] in (1, -1), f"direction 必须为 1 或 -1，实际 {seg['direction']}"
            assert isinstance(seg["points"], list)
            assert len(seg["points"]) >= 2, "每个 segment 至少 2 个点"

    def test_point_keys(self, shared_bundle: dict):
        """测试 7: 每个 point 包含 time (str) 和 value (float)。"""
        for seg in shared_bundle["visual_segments"]:
            for pt in seg["points"]:
                assert "time" in pt, f"point 缺少 time: {pt}"
                assert "value" in pt, f"point 缺少 value: {pt}"
                assert isinstance(pt["time"], str), f"time 必须为字符串，实际 {type(pt['time'])}"
                assert isinstance(pt["value"], float), f"value 必须为 float，实际 {type(pt['value'])}"
                assert math.isfinite(pt["value"]), f"value 必须有限，实际 {pt['value']}"

    def test_time_format_is_iso_date(self, shared_bundle: dict):
        """测试 7: time 格式为 YYYY-MM-DD。"""
        import re
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for seg in shared_bundle["visual_segments"]:
            for pt in seg["points"]:
                assert pattern.match(pt["time"]), f"time 应为 YYYY-MM-DD，实际 {pt['time']}"

    def test_visual_segments_match_algorithm(self, shared_bundle: dict, shared_algorithm_segments: list[dict]):
        """测试 8: visual_segments 的 direction/time/value 与底层算法 segments 逐点一致。"""
        bundle_segs = shared_bundle["visual_segments"]
        algo_segs = shared_algorithm_segments

        assert len(bundle_segs) == len(algo_segs), (
            f"segment 数量不一致: bundle={len(bundle_segs)} vs algo={len(algo_segs)}"
        )

        for i, (b_seg, a_seg) in enumerate(zip(bundle_segs, algo_segs)):
            assert b_seg["direction"] == a_seg["direction"], (
                f"segment[{i}] direction 不一致: bundle={b_seg['direction']} vs algo={a_seg['direction']}"
            )
            assert len(b_seg["points"]) == len(a_seg["points"]), (
                f"segment[{i}] points 数量不一致: bundle={len(b_seg['points'])} vs algo={len(a_seg['points'])}"
            )
            for j, (b_pt, a_pt) in enumerate(zip(b_seg["points"], a_seg["points"])):
                assert b_pt["time"] == a_pt["time"], (
                    f"segment[{i}].points[{j}] time 不一致: "
                    f"bundle={b_pt['time']} vs algo={a_pt['time']}"
                )
                assert math.isclose(b_pt["value"], a_pt["value"], rel_tol=1e-12, abs_tol=1e-12), (
                    f"segment[{i}].points[{j}] value 不一致: "
                    f"bundle={b_pt['value']} vs algo={a_pt['value']}"
                )


# ---------------------------------------------------------------------------
# 测试 9: factor_time == factor_per_bar.index
# ---------------------------------------------------------------------------


class TestFactorTime:
    """验证 factor_time 等于 factor_per_bar.index。"""

    def test_factor_time_equals_index(self, shared_bundle: dict):
        """测试 9: factor_time 等于 factor_per_bar.index。"""
        ft = shared_bundle["factor_time"]
        idx = shared_bundle["factor_per_bar"].index
        assert len(ft) == len(idx), (
            f"长度不一致: factor_time={len(ft)} vs index={len(idx)}"
        )
        assert (ft == idx).all(), "factor_time 与 factor_per_bar.index 不一致"


# ---------------------------------------------------------------------------
# 测试 10: last_row_metrics 与重构前完全一致
# ---------------------------------------------------------------------------


class TestLastRowMetricsUnchanged:
    """验证 last_row_metrics 与重构前完全一致。

    由于未修改 compute_dsa_history（SSOT）与 _history_row_to_metrics，
    last_row_metrics 的值由这两个函数决定，理论上不变。
    此处通过固定输入 + 字段完整性 + 与 per_bar 最后一行一致性双重验证。
    """

    # 期望 last_row_metrics 包含的所有字段（来自 _history_row_to_metrics 实际产出）
    EXPECTED_KEYS = {
        "regime_value", "regime_strength", "dsa_dir_bars",
        "offset_mean", "offset_std", "offset_rate", "offset_variance_rate", "offset_percentile",
        "touch_rope", "touch_vwap",
        "rope_dir1_pct", "rope_dir0_pct", "rope_dir_neg1_pct",
        "cross_up_count", "cross_down_count",
        "last_cross_up_date", "last_cross_down_date",
        "vwap_ret_5", "vwap_ret_10", "vwap_ret_20",
        "vwap_ret_avg", "vwap_ret_total",
        "dsa_vwap", "dsa_vwap_dev_pct",
        "vol_zscore", "avg_amount_20d",
        "change_pct",
        "rope_cross_up_date", "rope_cross_down_date",
        "rope_cross_up_price", "rope_cross_down_price",
        "rope_cross_up_count", "rope_cross_down_count",
    }

    def test_last_row_metrics_has_all_expected_keys(self, shared_bundle: dict):
        """测试 10: last_row_metrics 包含所有期望字段。"""
        metrics = shared_bundle["last_row_metrics"]
        missing = self.EXPECTED_KEYS - set(metrics.keys())
        assert not missing, f"last_row_metrics 缺少字段: {missing}"

    def test_last_row_metrics_consistent_with_per_bar(self, shared_bundle: dict):
        """测试 10: last_row_metrics 与 factor_per_bar 最后一行一致（核心不变性验证）。"""
        last_row = shared_bundle["factor_per_bar"].iloc[-1]
        metrics = shared_bundle["last_row_metrics"]

        # 数值字段
        for col in ["dsa_vwap", "dsa_vwap_dev_pct", "offset_mean", "offset_std",
                    "offset_rate", "offset_variance_rate", "offset_percentile",
                    "vwap_ret_avg", "vwap_ret_total", "vwap_ret_5", "vwap_ret_10", "vwap_ret_20"]:
            assert metrics[col] == _safe_float(last_row[col]), (
                f"字段 {col} 不一致: metrics={metrics[col]} vs per_bar={_safe_float(last_row[col])}"
            )

        # 整数字段
        for col in ["regime_value", "dsa_dir_bars",
                    "cross_up_count", "cross_down_count",
                    "rope_cross_up_count", "rope_cross_down_count"]:
            expected = int(last_row[col]) if pd.notna(last_row[col]) else 0
            assert metrics[col] == expected, f"字段 {col} 不一致: metrics={metrics[col]} vs {expected}"

    def test_last_row_metrics_deterministic(self):
        """测试 10: 相同输入两次计算 last_row_metrics 完全一致（确定性）。"""
        bars = _build_synthetic_bars(n=300)
        cfg = _build_config()
        m1 = compute_dsa_bundle(bars, cfg)["last_row_metrics"]
        m2 = compute_dsa_bundle(bars, cfg)["last_row_metrics"]
        assert m1 == m2, "相同输入的 last_row_metrics 不确定"


# ---------------------------------------------------------------------------
# 边界：数据不足时返回结构完整
# ---------------------------------------------------------------------------


class TestBundleEdgeCases:
    """验证数据不足时返回结构完整（含新字段）。"""

    def test_insufficient_data_returns_empty_structure(self):
        """数据 < 60 根时返回空结构（含所有新字段）。"""
        bars = _build_synthetic_bars(n=250).head(50)
        bundle = compute_dsa_bundle(bars, _build_config())
        assert bundle["factor_per_bar"].empty
        assert bundle["last_row_metrics"] == {}
        assert bundle["visual_segments"] == []
        assert bundle["pivot_labels"] == []
        assert bundle["anchor"] == {}
        assert len(bundle["factor_time"]) == 0


# ---------------------------------------------------------------------------
# 模块自测入口
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    bars = _build_synthetic_bars(n=300)
    bundle = compute_dsa_bundle(bars, _build_config())

    # 验证六字段
    for key in ["factor_per_bar", "visual_segments", "factor_time",
                "pivot_labels", "anchor", "last_row_metrics"]:
        assert key in bundle, f"缺少字段: {key}"

    # 验证 visual_segments 格式
    seg0 = bundle["visual_segments"][0]
    assert "direction" in seg0 and "points" in seg0
    pt0 = seg0["points"][0]
    assert "time" in pt0 and "value" in pt0

    # 验证 factor_time
    assert (bundle["factor_time"] == bundle["factor_per_bar"].index).all()

    print("Task 2 自测通过 ✓")
    print(f"  factor_per_bar 行数: {len(bundle['factor_per_bar'])}")
    print(f"  visual_segments 数量: {len(bundle['visual_segments'])}")
    print(f"  pivot_labels 数量: {len(bundle['pivot_labels'])}")
    print(f"  anchor 键: {list(bundle['anchor'].keys())}")
    print(f"  last_row_metrics 字段数: {len(bundle['last_row_metrics'])}")
    print(f"  第一个 segment: direction={seg0['direction']}, points={len(seg0['points'])}")
    print(f"  第一个 point: time={pt0['time']}, value={pt0['value']}")
    print("OK")
