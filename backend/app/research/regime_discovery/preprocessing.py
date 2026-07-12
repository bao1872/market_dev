"""预处理 — winsorize、RobustScaler、横截面 rank、相关性剪枝、PCA。

方向类特征（DIRECTION_FEATURES）保留 -1/0/1，不参与 winsorize/scaler/PCA。
所有 fit 返回可序列化 dict，便于写入 manifest。
"""

# ruff: noqa: N802, N803, N806

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from sklearn.preprocessing import RobustScaler

from app.research.regime_discovery.feature_builder import DIRECTION_FEATURES

logger = logging.getLogger("regime_discovery.preprocessing")

# PCA 最大维度
PCA_MAX_COMPONENTS = 8
# PCA 累计方差阈值
PCA_VARIANCE_THRESHOLD = 0.90
# 相关性剪枝默认阈值
CORR_PRUNE_THRESHOLD = 0.92


def _split_direction_features(features: list[str]) -> tuple[list[str], list[str]]:
    """把 features 拆成 (numeric, direction) 两组。"""
    numeric = [f for f in features if f not in DIRECTION_FEATURES]
    direction = [f for f in features if f in DIRECTION_FEATURES]
    return numeric, direction


def winsorize_features(
    df: pd.DataFrame,
    features: list[str],
    lower: float = 0.005,
    upper: float = 0.995,
) -> pd.DataFrame:
    """对数值特征按分位截断（不改变分布形状），方向字段跳过。

    Args:
        df: 含特征列的 DataFrame
        features: 待处理的特征列表
        lower: 下分位（默认 0.5%）
        upper: 上分位（默认 99.5%）

    Returns:
        截断后的 DataFrame（副本）
    """
    numeric, _direction = _split_direction_features(features)
    out = df.copy()
    for feat in numeric:
        if feat not in out.columns:
            continue
        s = pd.to_numeric(out[feat], errors="coerce")
        if s.dropna().empty:
            continue
        lo = s.quantile(lower)
        hi = s.quantile(upper)
        if pd.notna(lo) and pd.notna(hi) and hi > lo:
            out[feat] = s.clip(lower=lo, upper=hi).astype(np.float32)
    return out


def fit_robust_scaler(
    df: pd.DataFrame, features: list[str]
) -> dict[str, dict[str, float]]:
    """拟合 RobustScaler（median + IQR），返回可序列化参数。

    Args:
        df: 含特征列的 DataFrame
        features: 待拟合的数值特征列表

    Returns:
        {feature: {"median": float, "iqr": float}, ...}
    """
    numeric, _direction = _split_direction_features(features)
    params: dict[str, dict[str, float]] = {}
    for feat in numeric:
        if feat not in df.columns:
            continue
        s = pd.to_numeric(df[feat], errors="coerce").replace(
            [np.inf, -np.inf], np.nan
        ).dropna()
        if len(s) == 0:
            params[feat] = {"median": 0.0, "iqr": 1.0}
            continue
        scaler = RobustScaler()
        scaler.fit(s.to_numpy().reshape(-1, 1))
        # center_ = median, scale_ = IQR
        params[feat] = {
            "median": float(scaler.center_[0]),
            "iqr": float(scaler.scale_[0]) if scaler.scale_[0] > 0 else 1.0,
        }
    return params


def transform_robust(
    df: pd.DataFrame,
    features: list[str],
    params: dict[str, dict[str, float]],
) -> pd.DataFrame:
    """应用 RobustScaler：(x - median) / IQR。

    Args:
        df: 含特征列的 DataFrame
        features: 待变换的特征列表
        params: fit_robust_scaler 返回的参数

    Returns:
        变换后的 DataFrame（副本）
    """
    numeric, _direction = _split_direction_features(features)
    out = df.copy()
    for feat in numeric:
        if feat not in out.columns or feat not in params:
            continue
        p = params[feat]
        median = p["median"]
        iqr = p["iqr"] if p["iqr"] > 0 else 1.0
        s = pd.to_numeric(out[feat], errors="coerce")
        out[feat] = ((s - median) / iqr).astype(np.float32)
    return out


def transform_cross_sectional_rank(
    df: pd.DataFrame,
    features: list[str],
    _params: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """按 trade_date 横截面 rank（pct=True），输出 [0, 1]。

    方向字段跳过（保留 -1/0/1）。

    Args:
        df: 含 trade_date 和特征列的 DataFrame
        features: 待变换的特征列表
        _params: 占位参数（横截面 rank 无需 fit）

    Returns:
        变换后的 DataFrame（副本）
    """
    numeric, _direction = _split_direction_features(features)
    out = df.copy()
    if "trade_date" not in out.columns:
        raise ValueError("DataFrame 缺少 trade_date 列，无法做横截面 rank")
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    for feat in numeric:
        if feat not in out.columns:
            continue
        s = pd.to_numeric(out[feat], errors="coerce")
        # 按 trade_date 分组 rank
        out[feat] = s.groupby(out["trade_date"]).rank(pct=True, method="average").astype(np.float32)
    return out


def correlation_prune(
    features: list[str],
    corr_matrix: pd.DataFrame,
    threshold: float = CORR_PRUNE_THRESHOLD,
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """相关性剪枝：对 |rho|>threshold 的对，丢弃一方。

    保留策略：保留方差较大的一方（corr_matrix 对角线外部无方差信息时，按字母序保留）。
    实际实现：遍历冗余对，逐对从 features 中移除后者（保持稳定顺序）。

    Args:
        features: 原始特征列表
        corr_matrix: Spearman 相关矩阵
        threshold: |rho| 阈值，默认 0.92

    Returns:
        (保留特征列表, 丢弃对列表 [(kept, dropped, rho_str), ...])
    """
    kept = list(features)
    dropped: list[tuple[str, str, str]] = []
    if corr_matrix.empty:
        return kept, dropped
    # 收集所有冗余对
    pairs: list[tuple[str, str, float]] = []
    seen: set[tuple[str, ...]] = set()
    for i in range(len(features)):
        for j in range(i + 1, len(features)):
            a, b = features[i], features[j]
            if a not in corr_matrix.index or b not in corr_matrix.columns:
                continue
            rho = corr_matrix.loc[a, b]
            if pd.notna(rho) and abs(rho) > threshold:
                # 标准化 key（按字母序）
                key = tuple(sorted([a, b]))
                if key in seen:
                    continue
                seen.add(key)
                pairs.append((a, b, float(rho)))
    # 按绝对值降序
    pairs.sort(key=lambda x: abs(x[2]), reverse=True)
    # 逐对移除后者（若两者都还在 kept 中）
    for a, b, rho in pairs:
        if a in kept and b in kept:
            # 保留字母序靠前的，丢弃另一方（稳定可复现）
            to_drop = b if a < b else a
            to_keep = a if a < b else b
            kept.remove(to_drop)
            dropped.append((to_keep, to_drop, f"{rho:.4f}"))
    return kept, dropped


def fit_pca(
    X: np.ndarray,
    n_components: int = PCA_MAX_COMPONENTS,
    variance_threshold: float = PCA_VARIANCE_THRESHOLD,
) -> dict[str, Any]:
    """拟合 PCA，保留 90% 方差且最多 8 维。

    Args:
        X: 输入矩阵 (n_samples, n_features)
        n_components: 最大维度，默认 8
        variance_threshold: 累计方差阈值，默认 0.90

    Returns:
        {"model": PCA, "n_components": int, "explained_variance_ratio": list,
         "n_features_in": int}
    """
    if X.shape[0] == 0 or X.shape[1] == 0:
        raise ValueError("PCA 输入矩阵为空")
    # 先用所有主成分计算累计方差
    max_comp = min(n_components, X.shape[1], X.shape[0])
    pca_full = PCA(n_components=min(X.shape[1], X.shape[0]))
    pca_full.fit(X)
    cum_var = np.cumsum(pca_full.explained_variance_ratio_)
    # 找到 >= variance_threshold 的最小维度
    n_needed = int(np.searchsorted(cum_var, variance_threshold) + 1)
    n_final = min(n_needed, max_comp)
    n_final = max(1, n_final)
    # 重新拟合到目标维度
    pca = PCA(n_components=n_final)
    pca.fit(X)
    return {
        "model": pca,
        "n_components": n_final,
        "explained_variance_ratio": pca.explained_variance_ratio_.tolist(),
        "n_features_in": int(X.shape[1]),
    }


def transform_pca(X: np.ndarray, pca_params: dict[str, Any]) -> np.ndarray:
    """应用 PCA 降维。"""
    return pca_params["model"].transform(X)


def build_feature_matrix(
    df: pd.DataFrame,
    features: list[str],
    representation: str = "absolute",
) -> tuple[np.ndarray, dict[str, Any]]:
    """构建聚类输入矩阵 X。

    Args:
        df: 含特征列的 DataFrame（已 winsorize）
        features: 待使用的特征列表
        representation: "absolute"（RobustScaler）或 "cross_sectional"（横截面 rank）

    Returns:
        (X, params) — X 为 float32 矩阵，params 为可序列化 dict
    """
    numeric, direction = _split_direction_features(features)
    all_cols = numeric + direction
    missing = [c for c in all_cols if c not in df.columns]
    if missing:
        raise ValueError(f"特征列缺失: {missing}")

    # 提取数值矩阵：cross_sectional 需要 trade_date 列做分组
    keep_cols = list(all_cols)
    if representation == "cross_sectional" and "trade_date" in df.columns:
        keep_cols.append("trade_date")
    sub = df[keep_cols].copy()
    for c in all_cols:
        sub[c] = pd.to_numeric(sub[c], errors="coerce")
    if "trade_date" in sub.columns:
        # dropna 只对特征列，保留 trade_date
        feat_na = sub[all_cols].replace([np.inf, -np.inf], np.nan).isna().any(axis=1)
        sub = sub[~feat_na].reset_index(drop=True)
    else:
        sub = sub.replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)
    if len(sub) == 0:
        raise RuntimeError("特征矩阵 dropna 后为空")

    params: dict[str, Any] = {"representation": representation, "features_used": all_cols}

    if representation == "absolute":
        scaler_params = fit_robust_scaler(sub, numeric)
        sub = transform_robust(sub, numeric, scaler_params)
        params["scaler"] = scaler_params
    elif representation == "cross_sectional":
        sub = transform_cross_sectional_rank(sub, numeric)
        params["scaler"] = "cross_sectional_rank"
    else:
        raise ValueError(f"未知 representation: {representation}")

    X = sub[all_cols].to_numpy(dtype=np.float32)
    params["n_samples"] = int(X.shape[0])
    params["n_features"] = int(X.shape[1])
    return X, params
