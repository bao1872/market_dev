"""测试 stability.py — bootstrap ARI / cosine / transition / dwell / 80% 描述。"""
# ruff: noqa: N802, N803, N806

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import numpy as np
import pytest

from app.research.regime_discovery.stability import (
    DESCRIBE_DIRECTION_CONSISTENCY,
    DESCRIBE_MEDIAN_Z_MIN,
    bootstrap_ari,
    compute_dwell_time,
    compute_transition_matrix,
    describe_clusters_bootstrap,
    monthly_prevalence,
    temporal_centroid_similarity,
)


@pytest.fixture
def clusterable_X_with_meta():
    """构造 3 簇可分数据 + 元数据（dates, instrument_ids）。"""
    rng = np.random.default_rng(42)
    n_per = 60
    a = rng.normal([0, 0], 0.2, (n_per, 2))
    b = rng.normal([5, 5], 0.2, (n_per, 2))
    c = rng.normal([10, 0], 0.2, (n_per, 2))
    X = np.vstack([a, b, c]).astype(np.float32)
    # labels: 0..n_per-1=0, n_per..2n_per-1=1, 2n_per..=2
    labels = np.array([0] * n_per + [1] * n_per + [2] * n_per)
    # dates：连续 60 天，3 簇按时间排列
    base = date(2026, 1, 1)
    dates = []
    cur = base
    n_days = n_per
    while len(dates) < n_days:
        if cur.weekday() < 5:
            dates.append(cur)
        cur += timedelta(days=1)
    dates_all = dates * 3  # 三段各 60 天
    # instrument_ids：3 个股票
    inst_ids = [uuid4()] * n_per + [uuid4()] * n_per + [uuid4()] * n_per
    return X, labels, dates_all, inst_ids


class TestBootstrapAri:
    def test_deterministic_with_seed(self, sample_clusterable_X):
        # 用相同 seed 跑两次，结果应一致
        r1 = bootstrap_ari(sample_clusterable_X, k=3, n_boot=5, seed=42)
        r2 = bootstrap_ari(sample_clusterable_X, k=3, n_boot=5, seed=42)
        assert r1["mean_ari"] == r2["mean_ari"]
        assert r1["n_boot"] == 5

    def test_returns_all_fields(self, sample_clusterable_X):
        r = bootstrap_ari(sample_clusterable_X, k=3, n_boot=3, seed=42)
        for key in ["mean_ari", "median_ari", "std_ari", "min_ari", "max_ari",
                    "pass_rate", "n_boot"]:
            assert key in r

    def test_high_ari_for_separable_data(self, sample_clusterable_X):
        r = bootstrap_ari(sample_clusterable_X, k=3, n_boot=5, seed=42)
        # 可分数据 ARI 应较高
        assert r["mean_ari"] > 0.5


class TestTemporalCentroidSimilarity:
    def test_cosine(self, clusterable_X_with_meta):
        X, labels, dates, _inst = clusterable_X_with_meta
        result = temporal_centroid_similarity(X, labels, dates, k=3)
        assert "cosine_similarity" in result
        assert "pass" in result
        # 3 簇按时间排列，前 50% 和后 50% 的簇可能不同
        # 主要测试不抛异常

    def test_returns_threshold(self, clusterable_X_with_meta):
        X, labels, dates, _inst = clusterable_X_with_meta
        result = temporal_centroid_similarity(X, labels, dates, k=3)
        assert result["threshold"] == 0.85


class TestTransitionMatrix:
    def test_shape(self, clusterable_X_with_meta):
        X, labels, dates, inst_ids = clusterable_X_with_meta
        df = compute_transition_matrix(labels, inst_ids, dates)
        # k=3 → 3x3 矩阵
        assert df.shape == (3, 3)
        # 行列名为 R1..R3
        assert list(df.index) == ["R1", "R2", "R3"]
        assert list(df.columns) == ["R1", "R2", "R3"]

    def test_row_sums_to_one(self, clusterable_X_with_meta):
        X, labels, dates, inst_ids = clusterable_X_with_meta
        df = compute_transition_matrix(labels, inst_ids, dates)
        # 每行之和应接近 1（若有转移）
        for i in range(len(df)):
            row_sum = df.iloc[i].sum()
            if row_sum > 0:
                assert abs(row_sum - 1.0) < 1e-5

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            compute_transition_matrix([0, 1], ["a"], [date(2026, 1, 1)])


class TestDwellTime:
    def test_per_cluster(self, clusterable_X_with_meta):
        X, labels, dates, inst_ids = clusterable_X_with_meta
        df = compute_dwell_time(labels, inst_ids, dates)
        assert "cluster" in df.columns
        assert "mean_dwell" in df.columns
        assert "median_dwell" in df.columns
        assert "max_dwell" in df.columns
        assert "n_episodes" in df.columns
        # 每个 cluster 至少有一个 episode
        assert all(df["n_episodes"] >= 1)

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            compute_dwell_time([0, 1], ["a"], [date(2026, 1, 1)])


class TestMonthlyPrevalence:
    def test_returns_records(self, clusterable_X_with_meta):
        X, labels, dates, inst_ids = clusterable_X_with_meta
        df = monthly_prevalence(labels, dates)
        assert "month" in df.columns
        assert "cluster" in df.columns
        assert "count" in df.columns
        assert "ratio" in df.columns
        # 每月每簇占比之和应为 1
        for month in df["month"].unique():
            month_sum = df[df["month"] == month]["ratio"].sum()
            assert abs(month_sum - 1.0) < 1e-5


class TestDescribeClustersBootstrap:
    def test_80pct_rule(self, clusterable_X_with_meta):
        X, labels, dates, inst_ids = clusterable_X_with_meta
        features = ["f1", "f2"]
        df = describe_clusters_bootstrap(
            X, labels, features, n_boot=10, seed=42
        )
        assert "described" in df.columns
        assert "direction_consistency_rate" in df.columns
        assert "median_z" in df.columns
        # 可分数据的特征应被描述
        described = df[df["described"]]
        if len(described) > 0:
            # 满足描述的特征应满足 80% 一致性
            assert all(described["direction_consistency_rate"] >= DESCRIBE_DIRECTION_CONSISTENCY)
            assert all(described["median_z"].abs() >= DESCRIBE_MEDIAN_Z_MIN)

    def test_thresholds(self):
        assert DESCRIBE_DIRECTION_CONSISTENCY == 0.80
        assert DESCRIBE_MEDIAN_Z_MIN == 0.5
