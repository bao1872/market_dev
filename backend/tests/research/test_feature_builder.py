"""测试 feature_builder.py — 派生公式、无泄漏。"""
from __future__ import annotations

import pandas as pd

from app.research.regime_discovery.feature_builder import (
    CLUSTERING_FEATURE_WHITELIST,
    DIRECTION_FEATURES,
    FORBIDDEN_PREFIXES,
)


class TestBuildFeatures:
    def test_produces_17_columns(self, sample_features_df: pd.DataFrame):
        for feat in CLUSTERING_FEATURE_WHITELIST:
            assert feat in sample_features_df.columns, f"缺特征 {feat}"

    def test_feature_count(self):
        assert len(CLUSTERING_FEATURE_WHITELIST) == 17

    def test_no_label_or_hindsight_in_output(self, sample_features_df: pd.DataFrame):
        for col in sample_features_df.columns:
            for prefix in FORBIDDEN_PREFIXES:
                assert not col.startswith(prefix), f"输出含禁止列 {col}"

    def test_direction_features_skip_winsorize(self):
        assert "dsa_dir" in DIRECTION_FEATURES


class TestFormulas:
    def test_atr_pct_uses_close_denominator(self, sample_features_df: pd.DataFrame):
        # atr_pct = causal_atr / close
        df = sample_features_df.dropna(subset=["atr_pct", "close", "causal_atr"])
        if len(df) > 0:
            row = df.iloc[0]
            expected = row["causal_atr"] / row["close"]
            assert abs(row["atr_pct"] - expected) < 1e-5

    def test_bb_bandwidth_log_handles_negative(self):
        # 构造负值输入
        from app.research.regime_discovery.feature_builder import _compute_base_normalized
        df = pd.DataFrame({
            "close": [10.0],
            "causal_atr": [0.5],
            "causal_bb_percent_b": [0.5],
            "causal_bb_bandwidth_pct": [-0.1],  # 负值
            "causal_sqzmom_val": [0.01],
            "causal_sqzmom_delta_1": [0.005],
            "causal_volume_ratio_20": [1.0],
            "causal_volume_percentile_120": [0.5],
            "causal_active_swing_high": [11.0],
            "causal_active_swing_low": [9.0],
            "causal_developing_swing_high": [10.5],
            "causal_developing_swing_low": [9.5],
            "causal_dsa_confirmed_direction": ["1"],
            "causal_dsa_confirmed_age_bars": [5],
        })
        out = _compute_base_normalized(df)
        # log1p(max(-0.1, 0)) = log1p(0) = 0
        assert out["bb_bandwidth_log"].iloc[0] == 0.0

    def test_swing_position_clipped_0_1(self, sample_features_df: pd.DataFrame):
        asp = sample_features_df["active_swing_position"].dropna()
        dsp = sample_features_df["developing_swing_position"].dropna()
        if len(asp) > 0:
            assert asp.min() >= 0.0
            assert asp.max() <= 1.0
        if len(dsp) > 0:
            assert dsp.min() >= 0.0
            assert dsp.max() <= 1.0

    def test_dsa_dir_sign_mapping(self):
        from app.research.regime_discovery.feature_builder import _compute_base_normalized
        df = pd.DataFrame({
            "close": [10.0, 10.0, 10.0],
            "causal_atr": [0.5, 0.5, 0.5],
            "causal_bb_percent_b": [0.5, 0.5, 0.5],
            "causal_bb_bandwidth_pct": [0.1, 0.1, 0.1],
            "causal_sqzmom_val": [0.01, 0.01, 0.01],
            "causal_sqzmom_delta_1": [0.005, 0.005, 0.005],
            "causal_volume_ratio_20": [1.0, 1.0, 1.0],
            "causal_volume_percentile_120": [0.5, 0.5, 0.5],
            "causal_active_swing_high": [11.0, 11.0, 11.0],
            "causal_active_swing_low": [9.0, 9.0, 9.0],
            "causal_developing_swing_high": [10.5, 10.5, 10.5],
            "causal_developing_swing_low": [9.5, 9.5, 9.5],
            "causal_dsa_confirmed_direction": ["1", "-1", "0"],
            "causal_dsa_confirmed_age_bars": [5, 5, 5],
        })
        out = _compute_base_normalized(df)
        assert out["dsa_dir"].iloc[0] == 1.0
        assert out["dsa_dir"].iloc[1] == -1.0
        assert out["dsa_dir"].iloc[2] == 0.0

    def test_temporal_derivatives_per_instrument(self, sample_features_df: pd.DataFrame):
        # 前 5 行应为 NaN（shift(5)）
        df = sample_features_df.sort_values(["instrument_id", "trade_date"])
        for inst_id in df["instrument_id"].unique():
            sub = df[df["instrument_id"] == inst_id]
            # bb_percent_b_delta_5 前 5 行 NaN
            assert sub["bb_percent_b_delta_5"].iloc[:5].isna().all()

    def test_return_5d_formula(self, sample_features_df: pd.DataFrame):
        df = sample_features_df.sort_values(["instrument_id", "trade_date"])
        for inst_id in df["instrument_id"].unique():
            sub = df[df["instrument_id"] == inst_id].reset_index(drop=True)
            if len(sub) >= 6:
                expected = sub["close"].iloc[5] / sub["close"].iloc[0] - 1.0
                actual = sub["return_5d"].iloc[5]
                if pd.notna(actual):
                    assert abs(actual - expected) < 1e-5

    def test_realized_vol_10d_window(self, sample_features_df: pd.DataFrame):
        df = sample_features_df.sort_values(["instrument_id", "trade_date"])
        for inst_id in df["instrument_id"].unique():
            sub = df[df["instrument_id"] == inst_id].reset_index(drop=True)
            # 前 10 行应为 NaN（min_periods=10）
            assert sub["realized_vol_10d"].iloc[:10].isna().all()
            # 第 11 行应有值
            if len(sub) >= 11:
                assert pd.notna(sub["realized_vol_10d"].iloc[10])
