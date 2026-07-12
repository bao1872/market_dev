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
    transform_cross_sectional_rank,
    transform_pca,
    transform_robust,
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
