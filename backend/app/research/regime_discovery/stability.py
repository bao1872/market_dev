"""稳定性检验 — bootstrap ARI / 时间切分 centroid cosine / 转移矩阵 / dwell time / 簇描述。

bootstrap：80% 样本 fit kmeans，与全量 labels 计算 ARI
时间切分：前 50% vs 后 50% 的 centroid cosine
簇描述：仅当某特征在 >=80% bootstrap 中方向一致且 |median z|>=0.5 才进入描述
"""

# ruff: noqa: N802, N803, N806

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd
from sklearn.cluster import MiniBatchKMeans
from sklearn.metrics import adjusted_rand_score
from sklearn.metrics.pairwise import cosine_similarity

from app.research.regime_discovery.models import (
    REJECTION_THRESHOLDS,
    evaluate_k,
    fit_kmeans,
)

logger = logging.getLogger("regime_discovery.stability")

# bootstrap 默认次数
BOOTSTRAP_N_DEFAULT = 20
# 簇描述 bootstrap 默认次数
DESCRIBE_BOOTSTRAP_N = 50
# 方向一致性门槛（80%）
DESCRIBE_DIRECTION_CONSISTENCY = 0.80
# |median z| 门槛
DESCRIBE_MEDIAN_Z_MIN = 0.5
# ARI 通过门槛
ARI_PASS_THRESHOLD = REJECTION_THRESHOLDS["bootstrap_ari_min"]
# centroid cosine 通过门槛
COSINE_PASS_THRESHOLD = REJECTION_THRESHOLDS["centroid_cosine_min"]


def bootstrap_ari(
    X: np.ndarray,
    k: int,
    n_boot: int = BOOTSTRAP_N_DEFAULT,
    seed: int = 42,
    sample_ratio: float = 0.8,
) -> dict[str, Any]:
    """bootstrap ARI：每次抽 80% 样本 fit kmeans，与全量 labels 计算 ARI。

    Args:
        X: 输入矩阵
        k: 簇数
        n_boot: bootstrap 次数
        seed: 随机种子
        sample_ratio: 抽样比例

    Returns:
        {mean_ari, median_ari, std_ari, min_ari, max_ari, pass_rate, n_boot}
    """
    rng = np.random.default_rng(seed)
    n = X.shape[0]
    n_sample = max(k * 2, int(n * sample_ratio))
    # 全量 labels
    full_model = fit_kmeans(X, k, seed=seed)
    full_labels = full_model.labels_
    aris: list[float] = []
    for i in range(n_boot):
        boot_seed = seed + i + 1
        idx = rng.choice(n, size=n_sample, replace=False)
        boot_model = fit_kmeans(X[idx], k, seed=boot_seed)
        _boot_labels = boot_model.labels_
        # 用 boot_model 预测全量 labels，与 full_labels 计算 ARI
        pred_labels = boot_model.predict(X)
        ari = float(adjusted_rand_score(full_labels, pred_labels))
        aris.append(ari)
    arr = np.array(aris)
    pass_rate = float(np.mean(np.array(aris) >= ARI_PASS_THRESHOLD))
    return {
        "mean_ari": float(arr.mean()),
        "median_ari": float(np.median(arr)),
        "std_ari": float(arr.std(ddof=1)) if len(arr) > 1 else 0.0,
        "min_ari": float(arr.min()),
        "max_ari": float(arr.max()),
        "pass_rate": pass_rate,
        "n_boot": n_boot,
        "threshold": ARI_PASS_THRESHOLD,
    }


def temporal_centroid_similarity(
    X: np.ndarray,
    labels: np.ndarray,
    dates: Sequence[Any],
    k: int,
) -> dict[str, Any]:
    """按时间切分（前 50% vs 后 50%）分别计算 centroid cosine 相似度。

    Args:
        X: 输入矩阵
        labels: 簇标签
        dates: 日期序列（用于排序切分）
        k: 簇数

    Returns:
        {cosine_similarity, pass: bool, threshold}
    """
    if len(X) != len(dates):
        raise ValueError(f"X 行数 {len(X)} != dates 长度 {len(dates)}")
    # 按日期排序
    dates_arr = np.asarray(dates)
    order = np.argsort(dates_arr)
    X_sorted = X[order]
    labels_sorted = np.asarray(labels)[order]
    n = len(X_sorted)
    mid = n // 2
    first_half = X_sorted[:mid]
    first_labels = labels_sorted[:mid]
    second_half = X_sorted[mid:]
    second_labels = labels_sorted[mid:]
    # 计算每半的 cluster centroid
    cosines: list[float] = []
    for c in range(k):
        first_mask = first_labels == c
        second_mask = second_labels == c
        if first_mask.sum() == 0 or second_mask.sum() == 0:
            continue
        c1 = first_half[first_mask].mean(axis=0).reshape(1, -1)
        c2 = second_half[second_mask].mean(axis=0).reshape(1, -1)
        cos = float(cosine_similarity(c1, c2)[0, 0])
        cosines.append(cos)
    if not cosines:
        return {
            "cosine_similarity": float("nan"),
            "pass": False,
            "threshold": COSINE_PASS_THRESHOLD,
            "reason": "某半段簇为空，无法计算",
        }
    mean_cos = float(np.mean(cosines))
    return {
        "cosine_similarity": mean_cos,
        "pass": mean_cos >= COSINE_PASS_THRESHOLD,
        "threshold": COSINE_PASS_THRESHOLD,
        "n_clusters_compared": len(cosines),
    }


def compute_transition_matrix(
    labels: np.ndarray,
    instrument_ids: Sequence[Any],
    dates: Sequence[Any],
) -> pd.DataFrame:
    """按 instrument 时间排序计算 R_i -> R_j 转移矩阵。

    Args:
        labels: 簇标签
        instrument_ids: 每行对应的 instrument_id
        dates: 每行对应的 trade_date

    Returns:
        DataFrame: k×k 转移概率矩阵，行列标签为 R1..Rk
    """
    if not (len(labels) == len(instrument_ids) == len(dates)):
        raise ValueError("labels/instrument_ids/dates 长度不一致")
    df = pd.DataFrame({
        "label": np.asarray(labels),
        "instrument_id": np.asarray(instrument_ids),
        "trade_date": pd.to_datetime(dates),
    })
    df = df.sort_values(["instrument_id", "trade_date"]).reset_index(drop=True)
    k = int(df["label"].max()) + 1
    # 计算每个 instrument 内的转移
    transitions = np.zeros((k, k), dtype=np.int64)
    for _inst, group in df.groupby("instrument_id"):
        labs = group["label"].to_numpy()
        for i in range(len(labs) - 1):
            transitions[labs[i], labs[i + 1]] += 1
    # 归一化为概率
    row_sums = transitions.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1
    prob = transitions / row_sums
    labels_str = [f"R{i + 1}" for i in range(k)]
    return pd.DataFrame(prob, index=labels_str, columns=labels_str)


def compute_dwell_time(
    labels: np.ndarray,
    instrument_ids: Sequence[Any],
    dates: Sequence[Any],
) -> pd.DataFrame:
    """每次进入某 cluster 后停留多少 bar 才离开。

    Args:
        labels: 簇标签
        instrument_ids: 每行对应的 instrument_id
        dates: 每行对应的 trade_date

    Returns:
        DataFrame: 每行 (cluster, mean_dwell, median_dwell, max_dwell, n_episodes)
    """
    if not (len(labels) == len(instrument_ids) == len(dates)):
        raise ValueError("labels/instrument_ids/dates 长度不一致")
    df = pd.DataFrame({
        "label": np.asarray(labels),
        "instrument_id": np.asarray(instrument_ids),
        "trade_date": pd.to_datetime(dates),
    })
    df = df.sort_values(["instrument_id", "trade_date"]).reset_index(drop=True)
    # 按 instrument 计算连续段
    dwell_records: list[int] = []
    cluster_records: list[int] = []
    for _inst, group in df.groupby("instrument_id"):
        labs = group["label"].to_numpy()
        if len(labs) == 0:
            continue
        current = labs[0]
        run_len = 1
        for lab in labs[1:]:
            if lab == current:
                run_len += 1
            else:
                dwell_records.append(run_len)
                cluster_records.append(int(current))
                current = lab
                run_len = 1
        # 最后一段
        dwell_records.append(run_len)
        cluster_records.append(int(current))
    if not dwell_records:
        return pd.DataFrame(columns=[
            "cluster", "mean_dwell", "median_dwell", "max_dwell", "n_episodes"
        ])
    rec_df = pd.DataFrame({"cluster": cluster_records, "dwell": dwell_records})
    rows: list[dict[str, Any]] = []
    for c in sorted(rec_df["cluster"].unique()):
        sub = rec_df[rec_df["cluster"] == c]["dwell"]
        rows.append({
            "cluster": f"R{c + 1}",
            "cluster_id": int(c),
            "mean_dwell": float(sub.mean()),
            "median_dwell": float(sub.median()),
            "max_dwell": int(sub.max()),
            "n_episodes": int(len(sub)),
        })
    return pd.DataFrame(rows)


def monthly_prevalence(
    labels: np.ndarray,
    dates: Sequence[Any],
) -> pd.DataFrame:
    """按月统计每 cluster 占比。

    Args:
        labels: 簇标签
        dates: 日期序列

    Returns:
        DataFrame: 每行 (month, cluster, count, ratio)
    """
    if len(labels) != len(dates):
        raise ValueError("labels/dates 长度不一致")
    df = pd.DataFrame({
        "label": np.asarray(labels),
        "trade_date": pd.to_datetime(dates),
    })
    df["month"] = df["trade_date"].dt.strftime("%Y-%m")
    rows: list[dict[str, Any]] = []
    for month in sorted(df["month"].dropna().unique()):
        sub = df[df["month"] == month]
        total = len(sub)
        for c in sorted(sub["label"].unique()):
            n = int((sub["label"] == c).sum())
            rows.append({
                "month": month,
                "cluster": f"R{c + 1}",
                "cluster_id": int(c),
                "count": n,
                "ratio": float(n / total) if total else 0.0,
            })
    return pd.DataFrame(rows)


def describe_clusters_bootstrap(
    X: np.ndarray,
    labels: np.ndarray,
    features: list[str],
    n_boot: int = DESCRIBE_BOOTSTRAP_N,
    seed: int = 42,
) -> pd.DataFrame:
    """50 次 bootstrap，每次 fit kmeans + 计算 cluster 特征 z-score。

    仅当某特征在 >=80% bootstrap 中方向一致且 |median z|>=0.5 才进入描述。

    Args:
        X: 输入矩阵
        labels: 全量 labels
        features: 特征名列表
        n_boot: bootstrap 次数（默认 50）
        seed: 随机种子

    Returns:
        DataFrame: 每行 (cluster, feature, median_z, direction_consistency_rate,
                   described: bool)
    """
    rng = np.random.default_rng(seed)
    n, n_feat = X.shape
    k = int(labels.max()) + 1
    # 全局 z-score（基于全量）
    global_mean = X.mean(axis=0)
    global_std = X.std(axis=0)
    global_std[global_std == 0] = 1.0
    # 全量 cluster centroid z-score
    full_centroids_z = np.zeros((k, n_feat))
    for c in range(k):
        mask = labels == c
        if mask.sum() > 0:
            full_centroids_z[c] = (X[mask].mean(axis=0) - global_mean) / global_std
    # bootstrap
    boot_centroids_z = np.zeros((n_boot, k, n_feat))
    for i in range(n_boot):
        boot_seed = seed + i + 1
        idx = rng.choice(n, size=max(k * 2, int(n * 0.8)), replace=False)
        boot_model = MiniBatchKMeans(
            n_clusters=k, random_state=boot_seed,
            batch_size=min(256, len(idx)),
            n_init=3, max_iter=50,
        )
        boot_model.fit(X[idx])
        boot_labels = boot_model.predict(X)
        for c in range(k):
            mask = boot_labels == c
            if mask.sum() > 0:
                boot_centroids_z[i, c] = (X[mask].mean(axis=0) - global_mean) / global_std
    # 汇总
    rows: list[dict[str, Any]] = []
    for c in range(k):
        for j, feat in enumerate(features):
            full_z = float(full_centroids_z[c, j])
            boot_zs = boot_centroids_z[:, c, j]
            median_z = float(np.median(boot_zs))
            # 方向一致性：bootstrap 中与全量 z 方向一致的比例
            if full_z == 0:
                consistency = 0.0
            else:
                same_dir = np.sum(np.sign(boot_zs) == np.sign(full_z))
                consistency = float(same_dir / n_boot)
            described = (
                consistency >= DESCRIBE_DIRECTION_CONSISTENCY
                and abs(median_z) >= DESCRIBE_MEDIAN_Z_MIN
            )
            rows.append({
                "cluster": f"R{c + 1}",
                "cluster_id": c,
                "feature": feat,
                "full_z": full_z,
                "median_z": median_z,
                "direction_consistency_rate": consistency,
                "described": bool(described),
            })
    return pd.DataFrame(rows)


def check_stability(
    X: np.ndarray,
    k: int,
    seed: int = 42,
    dates: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """综合稳定性检验：bootstrap ARI + centroid cosine + evaluate_k 指标。

    Args:
        X: 输入矩阵
        k: 簇数
        seed: 随机种子
        dates: 日期序列（用于时间切分）

    Returns:
        {k, silhouette, ari_mean, cosine, min_ratio, max_ratio,
         pass: bool, reasons: [str]}
    """
    reasons: list[str] = []
    # 1. evaluate_k 指标
    metrics = evaluate_k(X, k, seed=seed)
    sil = metrics["silhouette"]
    min_ratio = metrics["min_cluster_ratio"]
    max_ratio = metrics["max_cluster_ratio"]
    if not (pd.notna(sil) and sil >= REJECTION_THRESHOLDS["silhouette_min"]):
        reasons.append(f"silhouette={sil:.4f} < {REJECTION_THRESHOLDS['silhouette_min']}")
    if min_ratio < REJECTION_THRESHOLDS["min_cluster_ratio_min"]:
        reasons.append(f"min_cluster_ratio={min_ratio:.4f} < {REJECTION_THRESHOLDS['min_cluster_ratio_min']}")
    if max_ratio > REJECTION_THRESHOLDS["max_cluster_ratio_max"]:
        reasons.append(f"max_cluster_ratio={max_ratio:.4f} > {REJECTION_THRESHOLDS['max_cluster_ratio_max']}")
    # 2. bootstrap ARI
    ari_result = bootstrap_ari(X, k, seed=seed)
    ari_mean = ari_result["mean_ari"]
    if ari_mean < REJECTION_THRESHOLDS["bootstrap_ari_min"]:
        reasons.append(f"bootstrap_ari={ari_mean:.4f} < {REJECTION_THRESHOLDS['bootstrap_ari_min']}")
    # 3. centroid cosine（若有 dates）
    cosine_val = float("nan")
    cosine_pass = True
    if dates is not None and len(dates) == X.shape[0]:
        model = fit_kmeans(X, k, seed=seed)
        cosine_result = temporal_centroid_similarity(X, model.labels_, dates, k)
        cosine_val = cosine_result["cosine_similarity"]
        cosine_pass = cosine_result["pass"]
        if not cosine_pass and pd.notna(cosine_val):
            reasons.append(f"centroid_cosine={cosine_val:.4f} < {REJECTION_THRESHOLDS['centroid_cosine_min']}")
    pass_all = len(reasons) == 0
    return {
        "k": k,
        "silhouette": sil,
        "ari_mean": ari_mean,
        "cosine": cosine_val,
        "min_cluster_ratio": min_ratio,
        "max_cluster_ratio": max_ratio,
        "pass": pass_all,
        "reasons": reasons,
        "bootstrap_detail": ari_result,
    }
