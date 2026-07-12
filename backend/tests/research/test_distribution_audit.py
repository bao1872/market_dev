"""测试 distribution_audit.py — 分布、漂移、相关性、方向。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.research.regime_discovery.distribution_audit import (
    REDUNDANT_CORR_THRESHOLD,
    audit_correlation,
    audit_discrete,
    audit_distribution,
    audit_monthly_drift,
    audit_stock_coverage,
    summarize_redundant_pairs,
)


class TestAuditDistribution:
    def test_percentiles_present(self, sample_features_df: pd.DataFrame):
        features = ["atr_pct", "bb_percent_b", "volume_percentile_120"]
        df = audit_distribution(sample_features_df, features)
        assert len(df) == 3
        expected_cols = [
            "feature", "count", "null_rate", "finite_rate", "mean", "std",
            "min", "p01", "p05", "p25", "p50", "p75", "p95", "p99", "max",
            "extreme_low_rate", "extreme_high_rate",
        ]
        for col in expected_cols:
            assert col in df.columns, f"缺列 {col}"

    def test_skips_direction_features(self, sample_features_df: pd.DataFrame):
        # dsa_dir 是方向字段，不应出现在 distribution 输出
        df = audit_distribution(sample_features_df, ["atr_pct", "dsa_dir"])
        assert "dsa_dir" not in df["feature"].tolist()
        assert "atr_pct" in df["feature"].tolist()

    def test_count_correct(self, sample_features_df: pd.DataFrame):
        df = audit_distribution(sample_features_df, ["atr_pct"])
        assert df.iloc[0]["count"] == len(sample_features_df)


class TestAuditMonthlyDrift:
    def test_psi_present(self, sample_features_df: pd.DataFrame):
        df = audit_monthly_drift(sample_features_df, ["atr_pct"])
        assert "psi_vs_first" in df.columns
        assert "wasserstein_vs_first" in df.columns
        # 第一个月的 PSI 应为 0
        first_month_rows = df[df["month"] == df["month"].min()]
        if len(first_month_rows) > 0:
            assert abs(first_month_rows.iloc[0]["psi_vs_first"]) < 0.01


class TestAuditCorrelation:
    def test_redundant_pairs_detected(self):
        # 构造高相关数据
        rng = np.random.default_rng(42)
        df = pd.DataFrame({
            "feat_a": rng.normal(0, 1, 100),
            "feat_b": rng.normal(0, 1, 100),  # 与 a 不相关
        })
        df["feat_c"] = df["feat_a"] * 1.1 + rng.normal(0, 0.01, 100)  # 与 a 高相关
        corr, pairs = audit_correlation(df, ["feat_a", "feat_b", "feat_c"])
        assert len(pairs) >= 1
        # 应包含 (feat_a, feat_c) 或反之
        flat = [p[0] for p in pairs] + [p[1] for p in pairs]
        assert "feat_a" in flat and "feat_c" in flat

    def test_threshold_value(self):
        assert REDUNDANT_CORR_THRESHOLD == 0.92

    def test_no_redundant_pairs(self):
        rng = np.random.default_rng(42)
        df = pd.DataFrame({
            "feat_a": rng.normal(0, 1, 100),
            "feat_b": rng.normal(0, 1, 100),
        })
        _corr, pairs = audit_correlation(df, ["feat_a", "feat_b"])
        assert len(pairs) == 0

    def test_summarize_redundant_pairs(self):
        pairs = [("a", "b", 0.95)]
        summary = summarize_redundant_pairs(pairs)
        assert len(summary) == 1
        assert summary[0]["feature_a"] == "a"
        assert summary[0]["feature_b"] == "b"
        assert summary[0]["rho"] == 0.95


class TestAuditDiscrete:
    def test_direction_features(self, sample_features_df: pd.DataFrame):
        df = audit_discrete(sample_features_df, ["dsa_dir"])
        assert len(df) == 1
        assert "neg_rate" in df.columns
        assert "zero_rate" in df.columns
        assert "pos_rate" in df.columns
        assert "nan_rate" in df.columns

    def test_rates_sum(self, sample_features_df: pd.DataFrame):
        df = audit_discrete(sample_features_df, ["dsa_dir"])
        if len(df) > 0:
            row = df.iloc[0]
            total = row["neg_rate"] + row["zero_rate"] + row["pos_rate"] + row["nan_rate"]
            assert abs(total - 1.0) < 1e-5


class TestAuditStockCoverage:
    def test_returns_coverage(self, sample_features_df: pd.DataFrame):
        df = audit_stock_coverage(sample_features_df, ["atr_pct"])
        assert "instrument_count" in df.columns
        assert "mean_coverage_rate" in df.columns
        assert "min_coverage_rate" in df.columns
        assert "max_coverage_rate" in df.columns
