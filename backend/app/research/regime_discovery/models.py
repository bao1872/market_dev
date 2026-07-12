"""无监督模型 — MiniBatchKMeans + diag GMM + k 选择 + 拒绝门槛。

主模型：MiniBatchKMeans (k=3..8)
辅助模型：diagonal-covariance GMM (max 60k rows)
拒绝门槛：silhouette>=0.08, min_cluster_ratio>=0.03, max_cluster_ratio<=0.60
（bootstrap ARI / centroid cosine 由 stability.py 计算）
"""

# ruff: noqa: N802, N803, N806

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd
from sklearn.cluster import MiniBatchKMeans
from sklearn.metrics import (
    calinski_harabasz_score,
    davies_bouldin_score,
    silhouette_score,
)
from sklearn.mixture import GaussianMixture

logger = logging.getLogger("regime_discovery.models")

# 拒绝门槛（与 stability.py 联动）
REJECTION_THRESHOLDS: dict[str, float] = {
    "silhouette_min": 0.08,
    "bootstrap_ari_min": 0.60,
    "centroid_cosine_min": 0.85,
    "min_cluster_ratio_min": 0.03,
    "max_cluster_ratio_max": 0.60,
}

# k 候选范围（inclusive）
K_RANGE_DEFAULT = (3, 8)
# GMM 最大样本数
GMM_MAX_ROWS = 60000

# MiniBatchKMeans 默认参数
KMEANS_PARAMS = {
    "batch_size": 256,
    "n_init": 10,
    "max_iter": 100,
    "reassignment_ratio": 0.1,
}


def fit_kmeans(
    X: np.ndarray,
    k: int,
    seed: int = 42,
    batch_size: int = 256,
) -> MiniBatchKMeans:
    """拟合 MiniBatchKMeans。

    Args:
        X: 输入矩阵 (n_samples, n_features)
        k: 簇数
        seed: 随机种子
        batch_size: mini-batch 大小

    Returns:
        拟合后的 MiniBatchKMeans
    """
    model = MiniBatchKMeans(
        n_clusters=k,
        random_state=seed,
        batch_size=min(batch_size, X.shape[0]),
        n_init=KMEANS_PARAMS["n_init"],
        max_iter=KMEANS_PARAMS["max_iter"],
        reassignment_ratio=KMEANS_PARAMS["reassignment_ratio"],
    )
    model.fit(X)
    return model


def fit_gmm_diag(
    X: np.ndarray, k: int, seed: int = 42
) -> GaussianMixture:
    """拟合 diagonal-covariance GMM。

    仅在 KMeans k 通过门槛后做对比，不作为主模型。
    若 X 行数 > GMM_MAX_ROWS，下采样。

    Args:
        X: 输入矩阵
        k: 簇数
        seed: 随机种子

    Returns:
        拟合后的 GaussianMixture
    """
    X_use = X
    if X.shape[0] > GMM_MAX_ROWS:
        rng = np.random.default_rng(seed)
        idx = rng.choice(X.shape[0], size=GMM_MAX_ROWS, replace=False)
        X_use = X[idx]
        logger.info(
            "GMM 输入 %d 行 > %d，下采样到 %d", X.shape[0], GMM_MAX_ROWS, GMM_MAX_ROWS
        )
    gmm = GaussianMixture(
        n_components=k,
        covariance_type="diag",
        random_state=seed,
        max_iter=100,
        n_init=5,
    )
    gmm.fit(X_use)
    return gmm


def evaluate_k(X: np.ndarray, k: int, seed: int = 42) -> dict[str, Any]:
    """评估单个 k 的指标：silhouette / Davies-Bouldin / Calinski-Harabasz / 簇占比。

    Args:
        X: 输入矩阵
        k: 簇数
        seed: 随机种子

    Returns:
        {k, silhouette, davies_bouldin, calinski_harabasz,
         min_cluster_ratio, max_cluster_ratio, inertia, pass初步}
    """
    if X.shape[0] < k * 2:
        return {
            "k": k, "silhouette": float("nan"), "davies_bouldin": float("nan"),
            "calinski_harabasz": float("nan"), "min_cluster_ratio": 0.0,
            "max_cluster_ratio": 1.0, "inertia": float("nan"),
            "pass_preliminary": False, "reason": "样本不足",
        }
    model = fit_kmeans(X, k, seed=seed)
    labels = model.labels_
    # 簇占比
    counts = np.bincount(labels, minlength=k)
    total = len(labels)
    ratios = counts / total
    min_ratio = float(ratios.min())
    max_ratio = float(ratios.max())
    # silhouette（若簇数 < 2 或样本太大，跳过）
    if k < 2 or len(labels) < 2:
        sil = float("nan")
    elif X.shape[0] > 50000:
        # 大样本下采样计算 silhouette
        rng = np.random.default_rng(seed)
        idx = rng.choice(X.shape[0], size=20000, replace=False)
        sil = float(silhouette_score(X[idx], labels[idx]))
    else:
        sil = float(silhouette_score(X, labels))
    # 其他指标
    db = float(davies_bouldin_score(X, labels)) if k > 1 else float("nan")
    ch = float(calinski_harabasz_score(X, labels)) if k > 1 else float("nan")
    # 初步门槛：silhouette + 簇占比（bootstrap ARI / cosine 由 stability.py 计算）
    pass_prelim = (
        pd.notna(sil) and sil >= REJECTION_THRESHOLDS["silhouette_min"]
        and min_ratio >= REJECTION_THRESHOLDS["min_cluster_ratio_min"]
        and max_ratio <= REJECTION_THRESHOLDS["max_cluster_ratio_max"]
    )
    return {
        "k": k, "silhouette": sil, "davies_bouldin": db,
        "calinski_harabasz": ch, "min_cluster_ratio": min_ratio,
        "max_cluster_ratio": max_ratio, "inertia": float(model.inertia_),
        "pass_preliminary": pass_prelim, "reason": "" if pass_prelim else "初步门槛未通过",
    }


def select_best_k(
    X: np.ndarray,
    k_range: tuple[int, int] = K_RANGE_DEFAULT,
    seed: int = 42,
) -> tuple[int | None, dict[str, Any]]:
    """综合指标选最佳 k。

    初步筛选：silhouette>=0.08 AND min_ratio>=0.03 AND max_ratio<=0.60
    在通过者中选 silhouette 最高的 k。
    若全部不通过，返回 (None, all_metrics)。

    Args:
        X: 输入矩阵
        k_range: (k_min, k_max) inclusive
        seed: 随机种子

    Returns:
        (best_k or None, {"all_metrics": [...], "selected": best_k or None,
                          "reason": str})
    """
    k_min, k_max = k_range
    all_metrics: list[dict[str, Any]] = []
    passed: list[dict[str, Any]] = []
    for k in range(k_min, k_max + 1):
        m = evaluate_k(X, k, seed=seed)
        all_metrics.append(m)
        if m["pass_preliminary"]:
            passed.append(m)
    if not passed:
        return None, {
            "all_metrics": all_metrics, "selected": None,
            "reason": "所有 k 均未通过初步门槛（silhouette/min_ratio/max_ratio）",
        }
    # 选 silhouette 最高的
    best = max(passed, key=lambda x: x["silhouette"] if pd.notna(x["silhouette"]) else -1)
    return best["k"], {
        "all_metrics": all_metrics, "selected": best["k"],
        "reason": f"通过初步门槛中 silhouette 最高（{best['silhouette']:.4f}）",
    }


def assign_clusters(model: MiniBatchKMeans | GaussianMixture, X: np.ndarray) -> np.ndarray:
    """用已拟合模型对新数据赋簇标签。"""
    return model.predict(X)


def get_cluster_profiles(
    df: pd.DataFrame,
    features: list[str],
    labels: np.ndarray,
) -> pd.DataFrame:
    """按簇分组输出每簇的 count/ratio/每特征 mean/median/std。

    Args:
        df: 含特征列的 DataFrame
        features: 待统计的特征列表
        labels: 簇标签（长度等于 df 行数）

    Returns:
        DataFrame: 每行 (cluster, count, ratio, feat_mean, feat_median, feat_std, ...)
    """
    if len(df) != len(labels):
        raise ValueError(
            f"df 行数 {len(df)} != labels 长度 {len(labels)}"
        )
    df = df.copy()
    df["_cluster"] = labels
    rows: list[dict[str, Any]] = []
    total = len(df)
    clusters = sorted(df["_cluster"].unique())
    for c in clusters:
        sub = df[df["_cluster"] == c]
        row: dict[str, Any] = {
            "cluster": f"R{c + 1}",  # R1..Rk
            "cluster_id": int(c),
            "count": int(len(sub)),
            "ratio": float(len(sub) / total) if total else 0.0,
        }
        for feat in features:
            if feat not in sub.columns:
                continue
            s = pd.to_numeric(sub[feat], errors="coerce").replace(
                [np.inf, -np.inf], np.nan
            ).dropna()
            row[f"{feat}_mean"] = float(s.mean()) if len(s) else float("nan")
            row[f"{feat}_median"] = float(s.median()) if len(s) else float("nan")
            row[f"{feat}_std"] = float(s.std(ddof=1)) if len(s) > 1 else 0.0
        rows.append(row)
    return pd.DataFrame(rows)


def get_thresholds() -> dict[str, float]:
    """返回拒绝门槛（供 manifest 使用）。"""
    return dict(REJECTION_THRESHOLDS)


def get_model_params_summary(
    k_range: tuple[int, int], seed: int, representation: str
) -> dict[str, Any]:
    """返回模型参数摘要（供 manifest 使用）。"""
    return {
        "k_range": list(k_range),
        "seed": seed,
        "representation": representation,
        "kmeans_params": KMEANS_PARAMS,
        "gmm_max_rows": GMM_MAX_ROWS,
        "kmeans_implementation": "sklearn.cluster.MiniBatchKMeans",
        "gmm_implementation": "sklearn.mixture.GaussianMixture(diag)",
    }
