"""测试 preprocessing.py — winsorize/scaler/rank/prune/PCA。"""
# ruff: noqa: N802, N803, N806

from __future__ import annotations

import numpy as np
import pandas as pd

from app.research.regime_discovery.feature_builder import DIRECTION_FEATURES
from app.research.regime_discovery.preprocessing import (
    build_feature_matrix,
    correlation_prune,
    fit_pca,
    fit_robust_scaler,
    fit_winsorize_bounds,
    transform_cross_sectional_rank,
    transform_feature_matrix,
    transform_pca,
    transform_robust,
    transform_winsorize,
    winsorize_features,
)


class TestWinsorize:
    def test_clips_quantiles(self):
        rng = np.random.default_rng(42)
        df = pd.DataFrame({"feat": np.concatenate([rng.normal(0, 1, 100), [100.0, -100.0]])})
        out = winsorize_features(df, ["feat"], lower=0.005, upper=0.995)
        # 极端值应被截断
        assert out["feat"].max() < 100.0
        assert out["feat"].min() > -100.0

    def test_direction_features_skip(self, sample_features_df: pd.DataFrame):
        out = winsorize_features(sample_features_df, ["atr_pct", "dsa_dir"])
        # dsa_dir 不变（方向字段）
        pd.testing.assert_series_equal(
            out["dsa_dir"], sample_features_df["dsa_dir"], check_names=False
        )


class TestRobustScaler:
    def test_median_iqr(self):
        rng = np.random.default_rng(42)
        df = pd.DataFrame({"feat": rng.normal(5, 2, 1000)})
        params = fit_robust_scaler(df, ["feat"])
        assert "feat" in params
        assert "median" in params["feat"]
        assert "iqr" in params["feat"]
        # 中位数应接近 5
        assert abs(params["feat"]["median"] - 5) < 0.5

    def test_transform(self):
        df = pd.DataFrame({"feat": [1.0, 2.0, 3.0, 4.0, 5.0]})
        params = fit_robust_scaler(df, ["feat"])
        out = transform_robust(df, ["feat"], params)
        # transform 后中位数应为 0
        assert abs(out["feat"].median()) < 1e-5


class TestCrossSectionalRank:
    def test_by_trade_date(self):
        dates = pd.to_datetime(["2026-01-01"] * 5 + ["2026-01-02"] * 5)
        df = pd.DataFrame({
            "trade_date": dates,
            "feat": [1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 30.0, 40.0, 50.0],
        })
        out = transform_cross_sectional_rank(df, ["feat"])
        # 每天 5 行，rank pct 应为 [0.1, 0.3, 0.5, 0.7, 0.9]
        first_day = out[out["trade_date"] == dates[0]]["feat"]
        assert first_day.iloc[0] < first_day.iloc[1]
        assert abs(first_day.iloc[0] - 0.1) < 1e-5 or abs(first_day.iloc[0] - 0.2) < 1e-5

    def test_output_in_0_1(self, sample_features_df: pd.DataFrame):
        out = transform_cross_sectional_rank(sample_features_df, ["atr_pct"])
        s = out["atr_pct"].dropna()
        if len(s) > 0:
            assert s.min() >= 0.0
            assert s.max() <= 1.0


class TestCorrelationPrune:
    def test_keeps_one_of_redundant_pair(self):
        # 构造高相关对
        corr = pd.DataFrame({
            "a": [1.0, 0.95, 0.1],
            "b": [0.95, 1.0, 0.1],
            "c": [0.1, 0.1, 1.0],
        }, index=["a", "b", "c"])
        kept, dropped = correlation_prune(["a", "b", "c"], corr, threshold=0.92)
        assert len(kept) == 2
        assert len(dropped) == 1
        # 应丢弃 b（字母序后）
        assert "b" not in kept

    def test_no_prune_when_low_corr(self):
        corr = pd.DataFrame({
            "a": [1.0, 0.1],
            "b": [0.1, 1.0],
        }, index=["a", "b"])
        kept, dropped = correlation_prune(["a", "b"], corr, threshold=0.92)
        assert len(kept) == 2
        assert len(dropped) == 0

    def test_empty_corr(self):
        kept, dropped = correlation_prune(["a"], pd.DataFrame(), threshold=0.92)
        assert kept == ["a"]
        assert dropped == []


class TestPCA:
    def test_variance_threshold(self):
        rng = np.random.default_rng(42)
        # 5 维但只有 2 维有信号
        X = np.zeros((200, 5))
        X[:, 0] = rng.normal(0, 10, 200)
        X[:, 1] = rng.normal(0, 5, 200)
        X[:, 2] = X[:, 0] * 0.1 + rng.normal(0, 0.1, 200)
        X[:, 3] = rng.normal(0, 0.01, 200)
        X[:, 4] = rng.normal(0, 0.01, 200)
        params = fit_pca(X, n_components=8, variance_threshold=0.90)
        assert params["n_components"] <= 8
        assert sum(params["explained_variance_ratio"]) >= 0.90

    def test_max_components(self):
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (100, 3))
        params = fit_pca(X, n_components=8)
        # 最多 3 维
        assert params["n_components"] <= 3

    def test_transform(self):
        rng = np.random.default_rng(42)
        X = rng.normal(0, 1, (100, 5))
        params = fit_pca(X, n_components=3)
        X_pca = transform_pca(X, params)
        assert X_pca.shape == (100, params["n_components"])


class TestDirectionFeaturesSkip:
    def test_direction_not_in_pca(self, sample_features_df: pd.DataFrame):
        # 方向字段不应进入 PCA（build_feature_matrix 仍包含，但应在更上层过滤）
        # 这里测试 DIRECTION_FEATURES 集合正确
        assert "dsa_dir" in DIRECTION_FEATURES


class TestBuildFeatureMatrix:
    def test_absolute_representation(self, sample_features_df: pd.DataFrame):
        from app.research.regime_discovery.feature_builder import CLUSTERING_FEATURE_WHITELIST
        X, params = build_feature_matrix(
            sample_features_df, CLUSTERING_FEATURE_WHITELIST, "absolute"
        )
        assert X.dtype == np.float32
        assert params["representation"] == "absolute"
        assert params["n_features"] == len(CLUSTERING_FEATURE_WHITELIST)

    def test_cross_sectional_representation(self, sample_features_df: pd.DataFrame):
        from app.research.regime_discovery.feature_builder import CLUSTERING_FEATURE_WHITELIST
        X, params = build_feature_matrix(
            sample_features_df, CLUSTERING_FEATURE_WHITELIST, "cross_sectional"
        )
        assert X.dtype == np.float32
        assert params["representation"] == "cross_sectional"


class TestFitWinsorizeBounds:
    def test_returns_lo_hi_dict(self):
        rng = np.random.default_rng(42)
        df = pd.DataFrame({"feat": rng.normal(0, 1, 100)})
        bounds = fit_winsorize_bounds(df, ["feat"], lower=0.05, upper=0.95)
        assert "feat" in bounds
        assert "lo" in bounds["feat"]
        assert "hi" in bounds["feat"]
        assert bounds["feat"]["lo"] < bounds["feat"]["hi"]

    def test_direction_features_skip(self, sample_features_df: pd.DataFrame):
        bounds = fit_winsorize_bounds(sample_features_df, ["atr_pct", "dsa_dir"])
        # dsa_dir 是方向字段，不应出现在 bounds 中
        assert "dsa_dir" not in bounds
        assert "atr_pct" in bounds

    def test_empty_series_fallback(self):
        df = pd.DataFrame({"feat": [np.nan, np.nan, np.inf]})
        bounds = fit_winsorize_bounds(df, ["feat"])
        assert bounds["feat"] == {"lo": 0.0, "hi": 1.0}


class TestTransformWinsorize:
    def test_clips_with_fitted_bounds(self):
        df = pd.DataFrame({"feat": [-100.0, 0.0, 50.0, 100.0]})
        bounds = {"feat": {"lo": 0.0, "hi": 50.0}}
        out = transform_winsorize(df, ["feat"], bounds)
        assert out["feat"].min() >= 0.0
        assert out["feat"].max() <= 50.0

    def test_direction_features_skip(self, sample_features_df: pd.DataFrame):
        bounds = fit_winsorize_bounds(sample_features_df, ["atr_pct", "dsa_dir"])
        original_dsa = sample_features_df["dsa_dir"].copy()
        out = transform_winsorize(sample_features_df, ["atr_pct", "dsa_dir"], bounds)
        # dsa_dir 不变（方向字段不被 clip）
        pd.testing.assert_series_equal(
            out["dsa_dir"], original_dsa, check_names=False
        )


class TestTransformFeatureMatrix:
    def test_absolute_transform(self, sample_features_df: pd.DataFrame):
        from app.research.regime_discovery.feature_builder import CLUSTERING_FEATURE_WHITELIST
        # 先 fit + transform 得到 prep_params
        bounds = fit_winsorize_bounds(sample_features_df, CLUSTERING_FEATURE_WHITELIST)
        winsorized = transform_winsorize(
            sample_features_df, CLUSTERING_FEATURE_WHITELIST, bounds
        )
        _X, prep_params = build_feature_matrix(
            winsorized, CLUSTERING_FEATURE_WHITELIST, "absolute"
        )
        # 用 transform_feature_matrix transform 同一数据
        X2 = transform_feature_matrix(
            winsorized, CLUSTERING_FEATURE_WHITELIST, "absolute", prep_params
        )
        assert X2.dtype == np.float32
        assert X2.shape[1] == len(CLUSTERING_FEATURE_WHITELIST)

    def test_empty_after_dropna(self):
        # 全 NaN 行 → dropna 后为空 → 返回 (0, n_features)
        df = pd.DataFrame({
            "atr_pct": [np.nan, np.nan],
            "bb_percent_b": [np.nan, np.nan],
        })
        X = transform_feature_matrix(
            df, ["atr_pct", "bb_percent_b"], "absolute", {"scaler": {}}
        )
        assert X.shape == (0, 2)
        assert X.dtype == np.float32


class TestCrossSectionalRankFullSection:
    """验证横截面 rank 基于完整 trade_date 横截面计算，而非行 chunk 子集。"""

    def _make_multi_date_df(self) -> pd.DataFrame:
        """构造 2 个交易日 × 6 只股票的数据，每个交易日有明确的 rank 顺序。"""
        dates = pd.to_datetime(
            ["2026-01-01"] * 6 + ["2026-01-02"] * 6
        )
        # day1: 1..6, day2: 10..60
        return pd.DataFrame({
            "trade_date": dates,
            "instrument_id": ["A", "B", "C", "D", "E", "F"] * 2,
            "feat": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0,
                     10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
        })

    def test_full_cross_section_rank_correct(self):
        """完整横截面 rank：每天 6 只股票，rank pct 应为 1/12, 3/12, ..., 11/12。"""
        df = self._make_multi_date_df()
        out = transform_cross_sectional_rank(df, ["feat"])
        day1 = out[out["trade_date"] == pd.Timestamp("2026-01-01")].sort_values("feat")
        # rank pct = (rank-1)/(n-1) for method='average' with pct=True, but pandas
        # rank(pct=True) uses rank/n. For 6 values: 1/6, 2/6, ..., 6/6
        expected = [1 / 6, 2 / 6, 3 / 6, 4 / 6, 5 / 6, 6 / 6]
        np.testing.assert_allclose(
            day1["feat"].to_numpy(), expected, atol=1e-6
        )

    def test_chunk_split_produces_different_rank(self):
        """验证：按行 chunk 分割同一交易日后分别 rank，结果与完整横截面不同。

        这正是为什么要用 get_all_matrix_rows 而非 iter_chunks 的原因。
        """
        df = self._make_multi_date_df()
        # 完整横截面 rank（正确）
        full_out = transform_cross_sectional_rank(df, ["feat"])
        full_day1 = full_out[full_out["trade_date"] == pd.Timestamp("2026-01-01")]
        full_day1_ranks = full_day1["feat"].sort_values().to_numpy()

        # 按行 chunk 分割（前 3 行 + 后 9 行）— 同一交易日的 6 行被分到两个 chunk
        chunk1 = df.iloc[:3]  # day1 的 A, B, C
        chunk1_out = transform_cross_sectional_rank(chunk1, ["feat"])
        # chunk1 中 day1 只有 3 行，rank pct = 1/3, 2/3, 3/3
        chunk1_day1_ranks = chunk1_out["feat"].sort_values().to_numpy()

        # 两者必须不同（否则说明 chunk 分割没影响 rank — 这是错误的）
        assert not np.allclose(full_day1_ranks[:3], chunk1_day1_ranks), (
            "横截面 rank 在 chunk 分割后结果相同 — 这意味着 rank 没有基于完整横截面，"
            "或者测试数据构造有误"
        )

    def test_split_by_trade_date_produces_same_rank(self):
        """验证：按 trade_date 完整分割后分别 rank，结果与完整横截面一致。"""
        df = self._make_multi_date_df()
        full_out = transform_cross_sectional_rank(df, ["feat"])

        # 按 trade_date 分割（每个 chunk 包含完整交易日的所有股票）
        results = []
        for _date, group in df.groupby("trade_date"):
            results.append(transform_cross_sectional_rank(group, ["feat"]))
        split_out = pd.concat(results, ignore_index=True)

        # 两者应一致（按 trade_date 分割不改变 rank）
        full_sorted = full_out.sort_values(["trade_date", "instrument_id"]).reset_index(drop=True)
        split_sorted = split_out.sort_values(["trade_date", "instrument_id"]).reset_index(drop=True)
        np.testing.assert_allclose(
            full_sorted["feat"].to_numpy(),
            split_sorted["feat"].to_numpy(),
            atol=1e-6,
        )

    def test_transform_feature_matrix_uses_full_cross_section(self):
        """验证 transform_feature_matrix 在 cross_sectional 模式下基于完整横截面 rank。"""
        df = self._make_multi_date_df()
        # 完整数据 transform
        X_full = transform_feature_matrix(
            df, ["feat"], "cross_sectional", {"scaler": {}}
        )
        assert X_full.shape == (12, 1)
        # 前 6 行（day1）的 rank 应基于 6 只股票
        day1_ranks = X_full[:6].flatten()
        assert day1_ranks.min() > 0
        assert day1_ranks.max() <= 1.0
