"""报告输出 — manifest + 7 CSV + report.md + 50MB 门禁 + 3 run 保留。

不写原始矩阵、不写 parquet、不写截图。
所有 CSV 不含 instrument_id / symbol 等可识别行级数据（transition_matrix 是聚合）。
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger("regime_discovery.reporting")

# 输出目录大小上限（MB）
OUTPUT_SIZE_LIMIT_MB = 50
# 保留最近 N 个 run
MAX_RUNS_KEPT = 3

# 必需输出文件
REQUIRED_OUTPUT_FILES = [
    "manifest.json",
    "distribution_summary.csv",
    "drift_summary.csv",
    "model_selection.csv",
    "cluster_profiles.csv",
    "cluster_stability.csv",
    "transition_matrix.csv",
    "report.md",
]


def create_run_dir(
    base_dir: str = "/home/ubuntu/panji_research_outputs/regime_discovery",
    seed: int = 42,
) -> Path:
    """创建 <run_id>/ 子目录，run_id = YYYYMMDD_HHMMSS_<seed>。

    Args:
        base_dir: 输出根目录
        seed: 随机种子（用于 run_id 命名）

    Returns:
        创建的 run 目录 Path
    """
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{seed}"
    run_dir = base / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    logger.info("创建 run 目录: %s", run_dir)
    return run_dir


def write_manifest(
    path: Path,
    *,
    git_sha: str,
    data_as_of: str,
    sql_row_count: int,
    feature_list: list[str],
    excluded_reasons: dict[str, str],
    seed: int,
    thresholds: dict[str, float],
    model_params: dict[str, Any],
    peak_rss_mb: float,
    representation: str,
    sample_rows: int,
    k_range: tuple[int, int],
    extras: dict[str, Any] | None = None,
) -> None:
    """写 manifest.json（含全部可复现元数据）。"""
    manifest: dict[str, Any] = {
        "run_id": path.parent.name,
        "git_sha": git_sha,
        "data_as_of": data_as_of,
        "sql_row_count": sql_row_count,
        "feature_list": feature_list,
        "excluded_reasons": excluded_reasons,
        "seed": seed,
        "thresholds": thresholds,
        "model_params": model_params,
        "peak_rss_mb": peak_rss_mb,
        "representation": representation,
        "sample_rows": sample_rows,
        "k_range": list(k_range),
        "created_at": datetime.now().isoformat(),
    }
    if extras:
        manifest["extras"] = extras
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("写 manifest: %s", path)


def write_distribution_summary(path: Path, df: pd.DataFrame) -> None:
    df.to_csv(path, index=False, encoding="utf-8")


def write_drift_summary(path: Path, df: pd.DataFrame) -> None:
    df.to_csv(path, index=False, encoding="utf-8")


def write_model_selection(path: Path, df: pd.DataFrame) -> None:
    df.to_csv(path, index=False, encoding="utf-8")


def write_cluster_profiles(path: Path, df: pd.DataFrame) -> None:
    df.to_csv(path, index=False, encoding="utf-8")


def write_cluster_stability(path: Path, df: pd.DataFrame) -> None:
    df.to_csv(path, index=False, encoding="utf-8")


def write_transition_matrix(path: Path, df: pd.DataFrame) -> None:
    df.to_csv(path, index=True, encoding="utf-8")  # transition matrix 保留行列名


def generate_report_md(
    *,
    manifest: dict[str, Any],
    distribution: pd.DataFrame,
    drift: pd.DataFrame,
    model_selection: pd.DataFrame,
    cluster_profiles: pd.DataFrame,
    cluster_stability: pd.DataFrame,
    transition: pd.DataFrame,
    k_selected: int | None,
    stable: bool,
    redundant_pairs: list[tuple[str, str, float]] | None = None,
    described_features: pd.DataFrame | None = None,
) -> str:
    """生成 Markdown 报告。

    Args:
        manifest: manifest dict
        distribution: 分布审计 DataFrame
        drift: 月度漂移 DataFrame
        model_selection: k 选择指标 DataFrame
        cluster_profiles: 簇画像 DataFrame
        cluster_stability: 稳定性检验 DataFrame
        transition: 转移矩阵
        k_selected: 选定的 k（None 表示未通过）
        stable: 是否通过稳定性检验
        redundant_pairs: 冗余特征对
        described_features: 簇描述特征（80% bootstrap + |median z|>=0.5）

    Returns:
        Markdown 字符串
    """
    lines: list[str] = []
    # 1. 概述
    lines.append(f"# Regime Discovery 报告 — {manifest.get('run_id', 'unknown')}")
    lines.append("")
    lines.append("## 1. 概述")
    lines.append("")
    lines.append(f"- **Git SHA**: `{manifest.get('git_sha', 'N/A')}`")
    lines.append(f"- **数据截止**: {manifest.get('data_as_of', 'N/A')}")
    lines.append(f"- **SQL 行数**: {manifest.get('sql_row_count', 'N/A')}")
    lines.append(f"- **样本行数**: {manifest.get('sample_rows', 'N/A')}")
    lines.append(f"- **随机种子**: {manifest.get('seed', 'N/A')}")
    lines.append(f"- **表示方法**: {manifest.get('representation', 'N/A')}")
    lines.append(f"- **k 范围**: {manifest.get('k_range', 'N/A')}")
    lines.append(f"- **资源峰值 RSS**: {manifest.get('peak_rss_mb', 'N/A')} MB")
    lines.append("")

    # 2. 特征清单
    lines.append("## 2. 特征清单与排除原因")
    lines.append("")
    feats = manifest.get("feature_list", [])
    lines.append(f"特征数: **{len(feats)}**")
    lines.append("")
    lines.append("特征: " + ", ".join(f"`{f}`" for f in feats))
    excluded = manifest.get("excluded_reasons", {})
    if excluded:
        lines.append("")
        lines.append("**排除原因**:")
        lines.append("")
        for k, v in excluded.items():
            lines.append(f"- `{k}`: {v}")
    lines.append("")

    # 3. 分布审计
    lines.append("## 3. 分布审计摘要")
    lines.append("")
    if distribution.empty:
        lines.append("（无分布审计数据）")
    else:
        lines.append("### 3.1 数值字段统计（前 10）")
        lines.append("")
        cols_show = ["feature", "count", "null_rate", "finite_rate", "mean", "std", "min", "p01", "p50", "p99", "max"]
        avail_cols = [c for c in cols_show if c in distribution.columns]
        lines.append(distribution[avail_cols].head(10).to_markdown(index=False))
        lines.append("")
        # 高缺失/低 finite top 5
        if "null_rate" in distribution.columns:
            high_null = distribution.nlargest(5, "null_rate")[["feature", "null_rate"]]
            lines.append("### 3.2 高缺失率 top 5")
            lines.append("")
            lines.append(high_null.to_markdown(index=False))
            lines.append("")
        if "finite_rate" in distribution.columns:
            low_finite = distribution.nsmallest(5, "finite_rate")[["feature", "finite_rate"]]
            lines.append("### 3.3 低 finite rate top 5")
            lines.append("")
            lines.append(low_finite.to_markdown(index=False))
            lines.append("")

    # 4. 月度漂移
    lines.append("## 4. 月度漂移")
    lines.append("")
    if drift.empty:
        lines.append("（无漂移数据）")
    else:
        if "psi_vs_first" in drift.columns:
            top_drift = drift.nlargest(10, "psi_vs_first")[
                ["feature", "month", "psi_vs_first", "wasserstein_vs_first"]
            ]
            lines.append("### 4.1 漂移 top 10（按 PSI）")
            lines.append("")
            lines.append(top_drift.to_markdown(index=False))
            lines.append("")

    # 5. 相关性冗余
    lines.append("## 5. 相关性冗余对（|rho|>0.92）")
    lines.append("")
    if redundant_pairs:
        lines.append("| feature_a | feature_b | rho |")
        lines.append("|---|---|---|")
        for a, b, rho in redundant_pairs:
            lines.append(f"| `{a}` | `{b}` | {rho:.4f} |")
    else:
        lines.append("（无冗余对）")
    lines.append("")

    # 6. 模型选择
    lines.append("## 6. 模型选择")
    lines.append("")
    if model_selection.empty:
        lines.append("（无模型选择数据）")
    else:
        cols = ["k", "silhouette", "davies_bouldin", "calinski_harabasz",
                "min_cluster_ratio", "max_cluster_ratio", "pass_preliminary"]
        avail = [c for c in cols if c in model_selection.columns]
        lines.append(model_selection[avail].to_markdown(index=False))
        lines.append("")
    if k_selected is None:
        lines.append("> **未发现稳定固定组合**：所有 k 均未通过初步门槛。")
    else:
        lines.append(f"> 选定 k = **{k_selected}**（稳定性 {'通过' if stable else '未通过'}）")
    lines.append("")

    # 7. 候选状态画像
    lines.append("## 7. 候选状态画像")
    lines.append("")
    if k_selected is not None and stable and not cluster_profiles.empty:
        lines.append("### 7.1 簇画像（count/ratio + 特征 mean/median/std）")
        lines.append("")
        # 只展示 count/ratio
        if {"cluster", "count", "ratio"}.issubset(cluster_profiles.columns):
            lines.append(cluster_profiles[["cluster", "count", "ratio"]].to_markdown(index=False))
            lines.append("")
        # 描述特征
        if described_features is not None and not described_features.empty:
            desc_only = described_features[described_features["described"]] if "described" in described_features.columns else described_features
            if not desc_only.empty:
                lines.append("### 7.2 描述特征（80% bootstrap 一致 + |median z|>=0.5）")
                lines.append("")
                show_cols = ["cluster", "feature", "median_z", "direction_consistency_rate"]
                avail = [c for c in show_cols if c in desc_only.columns]
                lines.append(desc_only[avail].to_markdown(index=False))
                lines.append("")
            else:
                lines.append("> 无特征满足 80% bootstrap 一致 + |median z|>=0.5 门槛。")
                lines.append("")
    else:
        lines.append("> **未发现稳定固定组合**，不输出簇画像。")
        lines.append("")

    # 8. 稳定性检验
    lines.append("## 8. 稳定性检验")
    lines.append("")
    if not cluster_stability.empty:
        lines.append(cluster_stability.to_markdown(index=False))
        lines.append("")

    # 9. 转移矩阵
    lines.append("## 9. 转移矩阵")
    lines.append("")
    if not transition.empty:
        lines.append(transition.to_markdown())
        lines.append("")

    # 10. 资源
    lines.append("## 10. 资源与输出")
    lines.append("")
    lines.append(f"- **RSS 峰值**: {manifest.get('peak_rss_mb', 'N/A')} MB")
    lines.append(f"- **阈值**: {manifest.get('thresholds', {})}")
    lines.append(f"- **输出文件**: {', '.join(REQUIRED_OUTPUT_FILES)}")
    lines.append("")
    return "\n".join(lines)


def enforce_output_size(run_dir: Path) -> None:
    """递归计算目录大小，超过 50MB 抛 RuntimeError。"""
    total = 0
    for f in run_dir.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    total_mb = total / (1024 * 1024)
    if total_mb > OUTPUT_SIZE_LIMIT_MB:
        raise RuntimeError(
            f"输出目录 {run_dir} 大小 {total_mb:.2f} MB 超过 {OUTPUT_SIZE_LIMIT_MB} MB 上限"
        )
    logger.info("输出目录大小: %.2f MB（上限 %d MB）", total_mb, OUTPUT_SIZE_LIMIT_MB)


def enforce_max_runs(base_dir: Path, keep: int = MAX_RUNS_KEPT) -> None:
    """删除最旧的 run 目录，保留最近 N 个。

    Args:
        base_dir: 输出根目录
        keep: 保留数量（默认 3）
    """
    if not base_dir.exists():
        return
    runs = sorted(
        [d for d in base_dir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
    )
    if len(runs) <= keep:
        return
    to_remove = runs[: len(runs) - keep]
    for d in to_remove:
        logger.info("清理旧 run: %s", d)
        shutil.rmtree(d, ignore_errors=True)


def write_all(
    run_dir: Path,
    *,
    manifest_data: dict[str, Any],
    distribution_df: pd.DataFrame,
    drift_df: pd.DataFrame,
    model_sel_df: pd.DataFrame,
    profiles_df: pd.DataFrame,
    stability_df: pd.DataFrame,
    transition_df: pd.DataFrame,
    report_md: str,
) -> None:
    """一次性写所有文件 + enforce_output_size。

    Args:
        run_dir: run 目录
        manifest_data: manifest 内容（已含 path 之外的元数据）
        distribution_df: 分布审计
        drift_df: 漂移
        model_sel_df: k 选择
        profiles_df: 簇画像
        stability_df: 稳定性
        transition_df: 转移矩阵
        report_md: Markdown 报告内容
    """
    # 写 manifest
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # 写 CSV
    distribution_df.to_csv(run_dir / "distribution_summary.csv", index=False, encoding="utf-8")
    drift_df.to_csv(run_dir / "drift_summary.csv", index=False, encoding="utf-8")
    model_sel_df.to_csv(run_dir / "model_selection.csv", index=False, encoding="utf-8")
    profiles_df.to_csv(run_dir / "cluster_profiles.csv", index=False, encoding="utf-8")
    stability_df.to_csv(run_dir / "cluster_stability.csv", index=False, encoding="utf-8")
    transition_df.to_csv(run_dir / "transition_matrix.csv", index=True, encoding="utf-8")
    # 写 report.md
    (run_dir / "report.md").write_text(report_md, encoding="utf-8")
    # 门禁
    enforce_output_size(run_dir)
    logger.info("所有输出已写入 %s", run_dir)


def list_output_files(run_dir: Path) -> list[str]:
    """列出 run 目录下的输出文件名。"""
    return [f.name for f in run_dir.iterdir() if f.is_file()]
