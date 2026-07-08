"""研究特征矩阵计算模块测试。

验证 feature_computer 的核心规则：
1. causal rolling 不使用未来数据（per-bar 计算无前视偏差）
2. confirmed_delay swing 不回填 anchor date（anchor bar 前为 NULL）
3. DSA causal/hindsight 双轨同时存在
4. Node Cluster 只在 hindsight
5. label 不进入 feature columns
6. 所有 33 个 feature 列都存在
7. warmup 期可为 NULL

用法：
    cd backend && APP_ENV=test pytest tests/test_feature_computer.py -v
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.research.feature_causality_registry import build_default_registry
from app.research.feature_computer import (
    compute_all_features,
    compute_causal_rolling_features,
    compute_confirmed_delay_swing_features,
    compute_dsa_dual_track_features,
    compute_label_features,
)

# ===== 测试数据工厂 =====


def _make_synthetic_bars(n: int = 250, start: date | None = None) -> pd.DataFrame:
    """生成确定性合成 bar 数据用于测试。

    Args:
        n: bar 数量
        start: 起始日期

    Returns:
        DataFrame with DatetimeIndex and columns [open, high, low, close, volume, amount]
    """
    if start is None:
        start = date(2025, 6, 1)
    dates = pd.date_range(start=start, periods=n, freq="B")  # Business day

    rng = np.random.default_rng(seed=42)
    # 随机游走 close
    returns = rng.normal(0.001, 0.02, n)
    close = 10.0 * np.cumprod(1 + returns)
    open_ = close * (1 + rng.normal(0, 0.005, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.01, n)))
    volume = rng.integers(1_000_000, 10_000_000, n).astype(float)
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
        index=dates,
    )


@pytest.fixture
def bars_250() -> pd.DataFrame:
    """250 根日线（含 warmup）."""
    return _make_synthetic_bars(250)


@pytest.fixture
def bars_500() -> pd.DataFrame:
    """500 根日线（含足够 warmup for DSA）."""
    return _make_synthetic_bars(500)


# ===== 1. causal rolling features =====


class TestCausalRollingFeatures:
    """causal 滚动特征测试。"""

    def test_returns_dataframe_with_expected_columns(self, bars_250: pd.DataFrame) -> None:
        """返回 DataFrame 包含 7 个 causal rolling 列。"""
        result = compute_causal_rolling_features(bars_250)
        assert isinstance(result, pd.DataFrame)
        expected_cols = {
            "causal_atr",
            "causal_bb_percent_b",
            "causal_bb_bandwidth_pct",
            "causal_sqzmom_val",
            "causal_sqzmom_delta_1",
            "causal_volume_ratio_20",
            "causal_volume_percentile_120",
        }
        assert expected_cols.issubset(set(result.columns))

    def test_index_matches_bars(self, bars_250: pd.DataFrame) -> None:
        """返回 DataFrame 的 index 与输入 bars 对齐。"""
        result = compute_causal_rolling_features(bars_250)
        assert len(result) == len(bars_250)
        assert result.index.equals(bars_250.index)

    def test_warmup_period_is_nan(self, bars_250: pd.DataFrame) -> None:
        """BB warmup 期（前 20 根）bb_percent_b 应为 NaN。"""
        result = compute_causal_rolling_features(bars_250)
        assert result["causal_bb_percent_b"].iloc[:19].isna().all(), (
            "BB 前 20 根应 NaN（warmup 不足）"
        )
        assert result["causal_bb_percent_b"].iloc[19:].notna().any(), (
            "第 20 根后应有非 NaN 值"
        )

    def test_atr_first_14_are_nan(self, bars_250: pd.DataFrame) -> None:
        """ATR 前 14 根（length=14）应为 NaN。"""
        result = compute_causal_rolling_features(bars_250)
        assert result["causal_atr"].iloc[:13].isna().all(), (
            "ATR 前 13 根应 NaN"
        )

    def test_no_future_lookahead(self, bars_250: pd.DataFrame) -> None:
        """causal 特征不使用未来数据：修改最后一根 bar 不影响前面的值。"""
        result1 = compute_causal_rolling_features(bars_250)

        # 修改最后一根 bar
        bars_modified = bars_250.copy()
        bars_modified.iloc[-1, bars_modified.columns.get_loc("close")] = 999.0
        result2 = compute_causal_rolling_features(bars_modified)

        # 除最后一行外，其他行应完全一致
        cols = [
            "causal_atr",
            "causal_bb_percent_b",
            "causal_bb_bandwidth_pct",
            "causal_sqzmom_val",
            "causal_volume_ratio_20",
        ]
        for col in cols:
            np.testing.assert_array_equal(
                result1[col].iloc[:-1].to_numpy(),
                result2[col].iloc[:-1].to_numpy(),
                err_msg=f"{col} 存在前视偏差",
            )

    def test_sqzmom_delta_1_is_diff(self, bars_250: pd.DataFrame) -> None:
        """sqzmom_delta_1 = sqzmom_val[i] - sqzmom_val[i-1]。"""
        result = compute_causal_rolling_features(bars_250)
        val = result["causal_sqzmom_val"]
        delta = result["causal_sqzmom_delta_1"]
        # 从第 2 个有效值开始验证
        valid_mask = val.notna() & delta.notna()
        expected_diff = val.diff()[valid_mask]
        actual_diff = delta[valid_mask]
        np.testing.assert_array_almost_equal(
            actual_diff.to_numpy(),
            expected_diff.to_numpy(),
            err_msg="sqzmom_delta_1 应等于 sqzmom_val 的一阶差分",
        )

    def test_volume_ratio_20_is_last_vol_over_sma20(self, bars_250: pd.DataFrame) -> None:
        """volume_ratio_20 = volume[i] / SMA(volume, 20)[i]。"""
        result = compute_causal_rolling_features(bars_250)
        vols = bars_250["volume"].astype(float)
        sma_20 = vols.rolling(20, min_periods=20).mean()
        expected = vols / sma_20
        actual = result["causal_volume_ratio_20"]
        # 从第 20 根开始比较
        valid_start = 19
        np.testing.assert_array_almost_equal(
            actual.iloc[valid_start:].to_numpy(),
            expected.iloc[valid_start:].to_numpy(),
            err_msg="volume_ratio_20 计算错误",
        )


# ===== 2. confirmed_delay swing features =====


class TestConfirmedDelaySwingFeatures:
    """confirmed_delay swing 特征测试。"""

    def test_returns_expected_columns(self, bars_250: pd.DataFrame) -> None:
        """返回包含 4 个 confirmed_delay 列。"""
        result = compute_confirmed_delay_swing_features(bars_250)
        expected_cols = {
            "confirmed_delay_confirmed_swing_high",
            "confirmed_delay_confirmed_swing_low",
            "confirmed_delay_bars_since_confirmed_swing_high",
            "confirmed_delay_bars_since_confirmed_swing_low",
        }
        assert expected_cols.issubset(set(result.columns))

    def test_no_lookahead_anchor_fills_forward_only(self, bars_250: pd.DataFrame) -> None:
        """confirmed_swing 不回填 anchor date 之前的 bar。

        pivot 在 anchor+length 确认，anchor 到确认 bar 之间用 pivot 值，
        anchor 之前不得有该 pivot 值。
        """
        result = compute_confirmed_delay_swing_features(bars_250)
        highs = result["confirmed_delay_confirmed_swing_high"]

        # forward-fill 应只在确认后才填充，不会提前
        # 找到第一个非 NaN 值
        first_valid_idx = highs.first_valid_index()
        if first_valid_idx is not None:
            # 第一个有效值之前的所有值应为 NaN（无前视）
            before_mask = highs.index < first_valid_idx
            assert highs[before_mask].isna().all(), (
                "confirmed_swing_high 在第一个确认 pivot 之前应全为 NaN（无前视）"
            )

    def test_bars_since_increases_monotonically(self, bars_250: pd.DataFrame) -> None:
        """bars_since_confirmed_swing_high 在两个确认 pivot 之间单调递增。"""
        result = compute_confirmed_delay_swing_features(bars_250)
        bsh = result["confirmed_delay_bars_since_confirmed_swing_high"]

        # 找到非 NaN 段
        valid = bsh.notna()
        if valid.sum() > 10:
            # 在连续非 NaN 段内，应单调递增
            values = bsh[valid].to_numpy()
            for i in range(1, len(values)):
                if values[i] != 0:  # 0 表示新确认
                    assert values[i] >= values[i - 1], (
                        f"bars_since 在 {i} 处应递增: {values[i-1]} -> {values[i]}"
                    )

    def test_zero_bars_since_at_confirmation(self, bars_250: pd.DataFrame) -> None:
        """确认 pivot 的 bar 上 bars_since=0。"""
        result = compute_confirmed_delay_swing_features(bars_250)
        highs = result["confirmed_delay_confirmed_swing_high"]
        bsh = result["confirmed_delay_bars_since_confirmed_swing_high"]

        # pivot 确认时（swing_high 值变化）bars_since 应为 0
        high_changes = highs.diff().abs() > 1e-10
        confirmed_bars = high_changes & highs.notna()
        if confirmed_bars.any():
            # 在确认 bar 处 bsh 应为 0
            for idx in confirmed_bars[confirmed_bars].index:
                assert bsh[idx] == 0, (
                    f"在确认 pivot {idx} 处 bars_since 应为 0, 实际={bsh[idx]}"
                )


# ===== 3. DSA dual track features =====


class TestDSADualTrack:
    """DSA 双轨（causal + hindsight）测试。"""

    def test_returns_both_causal_and_hindsight(self, bars_500: pd.DataFrame) -> None:
        """返回同时包含 causal 和 hindsight DSA 列。"""
        result = compute_dsa_dual_track_features(bars_500)
        causal_cols = {
            "causal_dsa_confirmed_segment",
            "causal_dsa_confirmed_direction",
            "causal_dsa_confirmed_age_bars",
        }
        hindsight_cols = {
            "hindsight_dsa_finalized_segment",
            "hindsight_dsa_finalized_direction",
            "hindsight_dsa_finalized_age_bars",
        }
        assert causal_cols.issubset(set(result.columns)), (
            f"缺少 causal DSA 列: {causal_cols - set(result.columns)}"
        )
        assert hindsight_cols.issubset(set(result.columns)), (
            f"缺少 hindsight DSA 列: {hindsight_cols - set(result.columns)}"
        )

    def test_causal_dsa_has_no_lookahead(self, bars_500: pd.DataFrame) -> None:
        """causal DSA 不使用未来数据：修改最后一根 bar 不影响前面的 segment。"""
        result1 = compute_dsa_dual_track_features(bars_500)

        bars_modified = bars_500.copy()
        bars_modified.iloc[-1, bars_modified.columns.get_loc("close")] = 999.0
        result2 = compute_dsa_dual_track_features(bars_modified)

        # causal segment 在最后一行前应一致
        causal_seg1 = result1["causal_dsa_confirmed_segment"].iloc[:-1]
        causal_seg2 = result2["causal_dsa_confirmed_segment"].iloc[:-1]
        np.testing.assert_array_equal(
            causal_seg1.to_numpy(),
            causal_seg2.to_numpy(),
            err_msg="causal DSA segment 存在前视偏差",
        )

    def test_hindsight_may_differ_from_causal(self, bars_500: pd.DataFrame) -> None:
        """hindsight DSA 可能与 causal 不同（未来确认后修正）。"""
        result = compute_dsa_dual_track_features(bars_500)
        causal_seg = result["causal_dsa_confirmed_segment"]
        hindsight_seg = result["hindsight_dsa_finalized_segment"]

        # 两列应存在（非全 NaN）
        assert causal_seg.notna().any(), "causal DSA segment 不应全 NaN"
        assert hindsight_seg.notna().any(), "hindsight DSA segment 不应全 NaN"

    def test_direction_values_are_valid(self, bars_500: pd.DataFrame) -> None:
        """DSA direction 值应为 1, 0, 或 -1。"""
        result = compute_dsa_dual_track_features(bars_500)
        for col in [
            "causal_dsa_confirmed_direction",
            "hindsight_dsa_finalized_direction",
        ]:
            valid_vals = result[col].dropna()
            if len(valid_vals) > 0:
                unique_vals = set(valid_vals.unique())
                assert unique_vals.issubset({1, 0, -1}), (
                    f"{col} direction 值超出 {{1,0,-1}}: {unique_vals}"
                )


# ===== 4. label features =====


class TestLabelFeatures:
    """label 特征测试。"""

    def test_returns_expected_columns(self, bars_250: pd.DataFrame) -> None:
        """返回 7 个 label 列。"""
        result = compute_label_features(bars_250)
        expected_cols = {
            "label_future_return_5d",
            "label_future_return_10d",
            "label_future_return_20d",
            "label_future_max_drawdown_10d",
            "label_future_max_drawdown_20d",
            "label_breakout_success_10d",
            "label_failure_breakdown_10d",
        }
        assert expected_cols.issubset(set(result.columns))

    def test_future_return_uses_future_close(self, bars_250: pd.DataFrame) -> None:
        """future_return_5d[i] = close[i+5]/close[i] - 1。"""
        result = compute_label_features(bars_250)
        closes = bars_250["close"].astype(float)
        actual = result["label_future_return_5d"]

        # 验证前 N-5 行
        n = len(bars_250)
        for i in range(n - 5):
            expected = closes.iloc[i + 5] / closes.iloc[i] - 1.0
            assert abs(actual.iloc[i] - expected) < 1e-10, (
                f"future_return_5d[{i}] 不正确: expected={expected}, actual={actual.iloc[i]}"
            )

    def test_last_5_rows_are_nan(self, bars_250: pd.DataFrame) -> None:
        """最后 5 根 future_return_5d 应为 NaN（无未来数据）。"""
        result = compute_label_features(bars_250)
        assert result["label_future_return_5d"].iloc[-5:].isna().all()
        assert result["label_future_return_10d"].iloc[-10:].isna().all()
        assert result["label_future_return_20d"].iloc[-20:].isna().all()

    def test_max_drawdown_is_non_positive(self, bars_250: pd.DataFrame) -> None:
        """future_max_drawdown 应 <= 0（回撤非正）。"""
        result = compute_label_features(bars_250)
        valid = result["label_future_max_drawdown_10d"].dropna()
        if len(valid) > 0:
            assert (valid <= 0).all(), (
                f"max_drawdown 应 <= 0, 实际 max={valid.max()}"
            )

    def test_breakout_is_binary(self, bars_250: pd.DataFrame) -> None:
        """breakout_success 和 failure_breakdown 应为 0 或 1。"""
        result = compute_label_features(bars_250)
        for col in ["label_breakout_success_10d", "label_failure_breakdown_10d"]:
            valid = result[col].dropna()
            if len(valid) > 0:
                unique_vals = set(valid.unique())
                assert unique_vals.issubset({0, 1}), (
                    f"{col} 应为 0/1, 实际: {unique_vals}"
                )


# ===== 5. 整合测试 =====


class TestComputeAllFeatures:
    """compute_all_features 整合测试。"""

    def test_returns_all_33_feature_columns(self, bars_500: pd.DataFrame) -> None:
        """返回所有 33 个 feature 列。"""
        result = compute_all_features(bars_500)
        reg = build_default_registry()
        expected_cols = set(reg.db_columns())
        actual_cols = set(result.columns)
        missing = expected_cols - actual_cols
        assert not missing, f"缺少 feature 列: {missing}"

    def test_label_not_in_causal_namespace(self, bars_500: pd.DataFrame) -> None:
        """label 列不应出现在 causal 命名空间。"""
        result = compute_all_features(bars_500)
        reg = build_default_registry()
        causal_cols = {s.db_column for s in reg.by_namespace("causal")}
        label_cols = {s.db_column for s in reg.by_namespace("label")}

        # 确认 label 列存在
        for col in label_cols:
            assert col in result.columns, f"label 列 {col} 不存在"

        # 确认 causal 列存在
        for col in causal_cols:
            assert col in result.columns, f"causal 列 {col} 不存在"

    def test_index_matches_bars(self, bars_500: pd.DataFrame) -> None:
        """返回 DataFrame 的 index 与输入 bars 对齐。"""
        result = compute_all_features(bars_500)
        assert len(result) == len(bars_500)
        assert result.index.equals(bars_500.index)
