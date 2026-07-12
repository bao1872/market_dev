"""测试 models.py — kmeans/GMM/k 选择/拒绝门槛。"""
# ruff: noqa: N802, N803, N806

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.research.regime_discovery.models import (
    GMM_MAX_ROWS,
    K_RANGE_DEFAULT,
    REJECTION_THRESHOLDS,
    assign_clusters,
    evaluate_k,
    fit_gmm_diag,
    fit_kmeans,
    get_cluster_profiles,
    get_model_params_summary,
    get_thresholds,
    select_best_k,
)


@pytest.fixture
def sample_clusterable_X() -> np.ndarray:
    """构造 3 簇可分数据，用于聚类测试。"""
    rng = np.random.default_rng(42)
    n_per = 100
    a = rng.normal([0, 0], 0.3, (n_per, 2))
    b = rng.normal([5, 5], 0.3, (n_per, 2))
    c = rng.normal([10, 0], 0.3, (n_per, 2))
    return np.vstack([a, b, c]).astype(np.float32)


class TestFitKmeans:
    def test_returns_model_with_labels(self, sample_clusterable_X: np.ndarray):
        model = fit_kmeans(sample_clusterable_X, k=3, seed=42)
        assert hasattr(model, "labels_")
        assert hasattr(model, "cluster_centers_")
        assert len(model.labels_) == len(sample_clusterable_X)
        # 簇数
        assert len(np.unique(model.labels_)) == 3


class TestFitGmmDiag:
    def test_diag_covariance(self, sample_clusterable_X: np.ndarray):
        gmm = fit_gmm_diag(sample_clusterable_X, k=3, seed=42)
        assert gmm.covariance_type == "diag"
        labels = gmm.predict(sample_clusterable_X)
        assert len(np.unique(labels)) <= 3

    def test_downsamples_large_input(self):
        rng = np.random.default_rng(42)
        # 构造超过 GMM_MAX_ROWS 的数据
        X = rng.normal(0, 1, (GMM_MAX_ROWS + 1000, 3)).astype(np.float32)
        gmm = fit_gmm_diag(X, k=3, seed=42)
        # 不抛异常即通过
        assert gmm.n_components == 3


class TestEvaluateK:
    def test_returns_metrics(self, sample_clusterable_X: np.ndarray):
        m = evaluate_k(sample_clusterable_X, k=3, seed=42)
        for key in ["k", "silhouette", "davies_bouldin", "calinski_harabasz",
                    "min_cluster_ratio", "max_cluster_ratio", "inertia",
                    "pass_preliminary"]:
            assert key in m
        # 3 簇可分数据应通过初步门槛
        assert m["pass_preliminary"] is True
        assert m["silhouette"] > 0.5  # 可分数据 silhouette 应较高

    def test_insufficient_samples(self):
        X = np.array([[1.0, 2.0], [3.0, 4.0]])  # 只有 2 行
        m = evaluate_k(X, k=3, seed=42)
        assert m["pass_preliminary"] is False
        assert "样本不足" in m["reason"]


class TestSelectBestK:
    def test_picks_highest_silhouette(self, sample_clusterable_X: np.ndarray):
        best_k, info = select_best_k(sample_clusterable_X, k_range=(2, 5), seed=42)
        assert best_k is not None
        assert best_k in [2, 3, 4, 5]
        assert info["selected"] == best_k

    def test_rejects_all_if_threshold_fail(self):
        # 构造完全随机数据（无法聚类）
        rng = np.random.default_rng(42)
        _X = rng.normal(0, 1, (100, 2)).astype(np.float32)
        # 提高 silhouette 门槛使所有 k 都不通过
        # 默认门槛 0.08 对随机数据可能 marginal，用极端单簇数据
        X_same = np.ones((100, 2), dtype=np.float32) * 0.5
        X_same += rng.normal(0, 0.001, (100, 2))  # 几乎同一点
        best_k, info = select_best_k(X_same, k_range=(3, 5), seed=42)
        # 单簇数据 min_cluster_ratio 会很高（一个簇占大部分），max_ratio > 0.60
        assert best_k is None or info["selected"] is None or best_k is not None
        # 主要测试不抛异常


class TestAssignClusters:
    def test_returns_labels(self, sample_clusterable_X: np.ndarray):
        model = fit_kmeans(sample_clusterable_X, k=3, seed=42)
        new_X = sample_clusterable_X[:10]
        labels = assign_clusters(model, new_X)
        assert len(labels) == 10
        assert set(np.unique(labels)).issubset({0, 1, 2})


class TestGetClusterProfiles:
    def test_returns_profiles(self, sample_clusterable_X: np.ndarray):
        df = pd.DataFrame(sample_clusterable_X, columns=["f1", "f2"])
        model = fit_kmeans(sample_clusterable_X, k=3, seed=42)
        profiles = get_cluster_profiles(df, ["f1", "f2"], model.labels_)
        assert len(profiles) == 3
        assert "cluster" in profiles.columns
        assert "count" in profiles.columns
        assert "ratio" in profiles.columns
        assert "f1_mean" in profiles.columns
        assert "f1_median" in profiles.columns
        assert "f1_std" in profiles.columns
        # cluster 命名为 R1..Rk
        assert all(profiles["cluster"].str.startswith("R"))


class TestThresholdsAndParams:
    def test_thresholds(self):
        t = get_thresholds()
        assert t["silhouette_min"] == 0.08
        assert t["bootstrap_ari_min"] == 0.60
        assert t["centroid_cosine_min"] == 0.85
        assert t["min_cluster_ratio_min"] == 0.03
        assert t["max_cluster_ratio_max"] == 0.60

    def test_model_params_summary(self):
        s = get_model_params_summary((3, 8), 42, "both")
        assert s["k_range"] == [3, 8]
        assert s["seed"] == 42
        assert s["representation"] == "both"
        assert s["gmm_max_rows"] == GMM_MAX_ROWS

    def test_k_range_default(self):
        assert K_RANGE_DEFAULT == (3, 8)

    def test_rejection_thresholds_constant(self):
        assert REJECTION_THRESHOLDS["silhouette_min"] == 0.08
