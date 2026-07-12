"""分布审计 — 数值统计、缺失、尾部、月度漂移、覆盖率、Spearman 相关、方向字段。

输出仅为 summary DataFrame，不含原始行级数据。
PSI/Wasserstein 用于月度漂移；|rho|>0.92 标记冗余对。
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import wasserstein_distance

from app.research.regime_discovery.feature_builder import (
    DIRECTION_FEATURES,
    FORBIDDEN_PREFIXES,
)

logger = logging.getLogger("regime_discovery.distribution_audit")

# PSI 基准分箱数
PSI_BUCKETS = 10
# 冗余相关阈值
REDUNDANT_CORR_THRESHOLD = 0.92

# 数值分布审计的百分位
PERCENTILES = [0.01, 0.05, 0.25, 0.50, 0.75, 0.95, 0.99]
PERCENTILE_NAMES = ["p01", "p05", "p25", "p50", "p75", "p95", "p99"]


def _validate_features(df: pd.DataFrame, features: list[str]) -> list[str]:
    """校验特征列存在且无禁止前缀，返回可用特征列表。"""
    available: list[str] = []
    for feat in features:
        for prefix in FORBIDDEN_PREFIXES:
            if feat.startswith(prefix):
                raise ValueError(
                    f"禁止字段 '{feat}' 进入分布审计：{prefix} 类字段不得使用"
                )
        if feat in df.columns:
            available.append(feat)
        else:
            logger.warning("特征 %s 不在 DataFrame，跳过", feat)
    return available


def audit_distribution(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    """输出每个数值字段的 count/null_rate/finite_rate/percentiles/极端值比例。

    Args:
        df: 含特征列的 DataFrame
        features: 待审计的数值特征列表（方向字段请走 audit_discrete）

    Returns:
        DataFrame: 每行一个特征，列为统计指标
    """
    available = _validate_features(df, features)
    # 排除方向字段
    numeric_features = [f for f in available if f not in DIRECTION_FEATURES]
    rows: list[dict[str, Any]] = []
    for feat in numeric_features:
        s = df[feat]
        n_total = len(s)
        n_null = int(s.isna().sum())
        n_finite = int(np.isfinite(pd.to_numeric(s, errors="coerce")).sum())
        s_clean = pd.to_numeric(s, errors="coerce").replace(
            [np.inf, -np.inf], np.nan
        ).dropna()
        if len(s_clean) == 0:
            rows.append({
                "feature": feat, "count": 0, "null_rate": 1.0,
                "finite_rate": 0.0, "mean": np.nan, "std": np.nan,
                "min": np.nan, "max": np.nan,
                **dict.fromkeys(PERCENTILE_NAMES, np.nan),
                "extreme_low_rate": np.nan, "extreme_high_rate": np.nan,
            })
            continue
        # 百分位
        quantiles = s_clean.quantile(PERCENTILES).to_dict()
        # 重命名
        q_named = {PERCENTILE_NAMES[i]: quantiles[PERCENTILES[i]] for i in range(len(PERCENTILES))}
        p01 = q_named["p01"]
        p99 = q_named["p99"]
        # 极端值：超出 p01/p99（理论上约 2%，因 0.5%/99.5% winsor 之前）
        extreme_low = float((s_clean < p01).mean())
        extreme_high = float((s_clean > p99).mean())
        rows.append({
            "feature": feat,
            "count": n_total,
            "null_rate": n_null / n_total if n_total else 1.0,
            "finite_rate": n_finite / n_total if n_total else 0.0,
            "mean": float(s_clean.mean()),
            "std": float(s_clean.std(ddof=1)) if len(s_clean) > 1 else 0.0,
            "min": float(s_clean.min()),
            "max": float(s_clean.max()),
            **{name: float(q_named[name]) for name in PERCENTILE_NAMES},
            "extreme_low_rate": extreme_low,
            "extreme_high_rate": extreme_high,
        })
    return pd.DataFrame(rows)


def _compute_psi(baseline: np.ndarray, current: np.ndarray, buckets: int = PSI_BUCKETS) -> float:
    """计算 PSI：10 个等宽 bucket，基准为 baseline。

    Args:
        baseline: 基准样本（非空有限值）
        current: 当前样本（非空有限值）
        buckets: 分箱数

    Returns:
        PSI 值（float）；若无法计算返回 NaN
    """
    if len(baseline) == 0 or len(current) == 0:
        return float("nan")
    lo = float(min(baseline.min(), current.min()))
    hi = float(max(baseline.max(), current.max()))
    if hi <= lo:
        return 0.0
    edges = np.linspace(lo, hi, buckets + 1)
    # 计算占比，加 epsilon 避免除零
    eps = 1e-6
    base_counts, _ = np.histogram(baseline, bins=edges)
    curr_counts, _ = np.histogram(current, bins=edges)
    base_pct = base_counts / max(base_counts.sum(), 1) + eps
    curr_pct = curr_counts / max(curr_counts.sum(), 1) + eps
    psi = float(np.sum((curr_pct - base_pct) * np.log(curr_pct / base_pct)))
    return psi


def audit_monthly_drift(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    """按月统计每特征的中位数/IQR/mean/std + PSI + Wasserstein（基准为第一个月）。

    Args:
        df: 含 trade_date 和特征列的 DataFrame
        features: 待审计的数值特征列表

    Returns:
        DataFrame: 每行 (feature, month, median, iqr, mean, std, psi_vs_first, wasserstein_vs_first)
    """
    available = _validate_features(df, features)
    numeric_features = [f for f in available if f not in DIRECTION_FEATURES]

    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["month"] = df["trade_date"].dt.strftime("%Y-%m")
    months = sorted(df["month"].dropna().unique())
    if len(months) == 0:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for feat in numeric_features:
        # 基准：第一个月
        first_month_mask = df["month"] == months[0]
        baseline = pd.to_numeric(df.loc[first_month_mask, feat], errors="coerce").replace(
            [np.inf, -np.inf], np.nan
        ).dropna().to_numpy()
        for month in months:
            mask = df["month"] == month
            s = pd.to_numeric(df.loc[mask, feat], errors="coerce").replace(
                [np.inf, -np.inf], np.nan
            ).dropna()
            if len(s) == 0:
                rows.append({
                    "feature": feat, "month": month, "median": np.nan,
                    "iqr": np.nan, "mean": np.nan, "std": np.nan,
                    "psi_vs_first": np.nan, "wasserstein_vs_first": np.nan,
                })
                continue
            q25, q75 = s.quantile([0.25, 0.75])
            current = s.to_numpy()
            psi = _compute_psi(baseline, current)
            w_dist = float(wasserstein_distance(baseline, current)) if len(baseline) > 0 else np.nan
            rows.append({
                "feature": feat, "month": month,
                "median": float(s.median()), "iqr": float(q75 - q25),
                "mean": float(s.mean()),
                "std": float(s.std(ddof=1)) if len(s) > 1 else 0.0,
                "psi_vs_first": psi, "wasserstein_vs_first": w_dist,
            })
    return pd.DataFrame(rows)


def audit_stock_coverage(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    """按股票统计每特征的覆盖率（非空有限值 / 总行数）。

    Args:
        df: 含 instrument_id 和特征列的 DataFrame
        features: 待审计的数值特征列表

    Returns:
        DataFrame: 每行 (feature, instrument_count, mean_coverage_rate,
                   min_coverage_rate, max_coverage_rate)
    """
    available = _validate_features(df, features)
    numeric_features = [f for f in available if f not in DIRECTION_FEATURES]
    rows: list[dict[str, Any]] = []
    for feat in numeric_features:
        s = pd.to_numeric(df[feat], errors="coerce").replace([np.inf, -np.inf], np.nan)
        # 按 instrument_id 分组计算覆盖率
        grouped = (~s.isna()).groupby(df["instrument_id"]).mean()
        rows.append({
            "feature": feat,
            "instrument_count": int(len(grouped)),
            "mean_coverage_rate": float(grouped.mean()),
            "min_coverage_rate": float(grouped.min()),
            "max_coverage_rate": float(grouped.max()),
        })
    return pd.DataFrame(rows)


def audit_correlation(
    df: pd.DataFrame, features: list[str]
) -> tuple[pd.DataFrame, list[tuple[str, str, float]]]:
    """计算 Spearman 相关矩阵 + |rho|>0.92 冗余对。

    Args:
        df: 含特征列的 DataFrame
        features: 待审计的数值特征列表

    Returns:
        (corr_matrix_df, redundant_pairs) — 后者为 [(feat_a, feat_b, rho), ...]
    """
    available = _validate_features(df, features)
    numeric_features = [f for f in available if f not in DIRECTION_FEATURES]
    if len(numeric_features) < 2:
        return pd.DataFrame(), []
    sub = df[numeric_features].apply(pd.to_numeric, errors="coerce")
    corr = sub.corr(method="spearman")
    # 收集冗余对（上三角，避免重复）
    redundant: list[tuple[str, str, float]] = []
    for i in range(len(numeric_features)):
        for j in range(i + 1, len(numeric_features)):
            a = numeric_features[i]
            b = numeric_features[j]
            rho = corr.iloc[i, j]
            if pd.notna(rho) and abs(rho) > REDUNDANT_CORR_THRESHOLD:
                redundant.append((a, b, float(rho)))
    # 按绝对值降序
    redundant.sort(key=lambda x: abs(x[2]), reverse=True)
    return corr, redundant


def audit_discrete(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    """统计方向字段（如 dsa_dir）的 {-1, 0, 1} 占比、0 值占比、NaN 占比。

    Args:
        df: 含特征列的 DataFrame
        features: 待审计的方向特征列表

    Returns:
        DataFrame: 每行 (feature, count, neg_rate, zero_rate, pos_rate, nan_rate)
    """
    rows: list[dict[str, Any]] = []
    for feat in features:
        if feat not in df.columns:
            logger.warning("方向字段 %s 不在 DataFrame，跳过", feat)
            continue
        s = pd.to_numeric(df[feat], errors="coerce")
        n_total = len(s)
        n_nan = int(s.isna().sum())
        s_valid = s.dropna()
        n_neg = int((s_valid < 0).sum())
        n_zero = int((s_valid == 0).sum())
        n_pos = int((s_valid > 0).sum())
        n_valid = len(s_valid)
        rows.append({
            "feature": feat,
            "count": n_total,
            "neg_rate": n_neg / n_total if n_total else 0.0,
            "zero_rate": n_zero / n_total if n_total else 0.0,
            "pos_rate": n_pos / n_total if n_total else 0.0,
            "nan_rate": n_nan / n_total if n_total else 0.0,
            "valid_count": n_valid,
        })
    return pd.DataFrame(rows)


def summarize_redundant_pairs(
    pairs: list[tuple[str, str, float]],
) -> list[dict[str, Any]]:
    """将冗余对列表转换为可序列化的 dict 列表（用于 manifest）。"""
    return [{"feature_a": a, "feature_b": b, "rho": r} for a, b, r in pairs]
