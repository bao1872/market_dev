"""研究 Regime Discovery CLI — 分布审计 + 无监督候选状态发现。

与生产完全隔离：
- 只读 research_feature_matrix_rows + bars_daily（127.0.0.1:15432 隧道 + market_exp_ro 只读账户）
- 事务 READ ONLY + statement_timeout=120s
- 不改 API/前端/Worker/scheduler/migration/snapshot/watchlist/通知
- 不写原始矩阵、不写 parquet；输出 ≤ 50MB；保留最近 3 个 run

主流程：
1. 解析参数 + 设置单线程
2. 创建只读 engine + session
3. dry-run 打印计划退出
4. stratified_sample → matrix_df
5. fetch_close_prices → close_df
6. build_features → features_df（17 特征）
7. 分布审计（distribution/monthly_drift/stock_coverage/correlation/discrete）
8. 按 representation 分支：absolute / cross_sectional / both
   winsorize → scaler → correlation_prune → PCA → fit_kmeans × k_range → select_best_k
9. stability.check_stability（bootstrap ARI + centroid cosine）
10. 若 k 通过：全量 iter_chunks assignment → transition/prevalence/dwell
11. write_all → manifest + 7 CSV + report.md
12. enforce_output_size + enforce_max_runs

用法：
    cd /home/ubuntu/market_dev/backend && .venv/bin/python -m scripts.research_regime_discovery --dry-run
    cd /home/ubuntu/market_dev/backend && .venv/bin/python -m scripts.research_regime_discovery \\
        --start 2026-07-01 --end 2026-07-22 --sample-rows 100 --representation absolute
"""

# ruff: noqa: N802, N803, N806

from __future__ import annotations

import argparse
import gc
import logging
import os
import resource
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# 确保单线程（必须在 import sklearn 之前）
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

from app.research.regime_discovery import (  # noqa: E402
    data_access,
    distribution_audit,
    feature_builder,
    models,
    preprocessing,
    reporting,
    stability,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("research_regime_discovery")

DEFAULT_OUTPUT_DIR = "/home/ubuntu/panji_research_outputs/regime_discovery"


def parse_args() -> argparse.Namespace:
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(
        description="研究 Regime Discovery — 分布审计 + 无监督候选状态发现",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只打印计划与估算，不查 DB，不写文件",
    )
    parser.add_argument("--start", type=str, default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--sample-rows", type=int, default=150000, help="抽样行数上限")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--k-min", type=int, default=3, help="k 最小值")
    parser.add_argument("--k-max", type=int, default=8, help="k 最大值")
    parser.add_argument("--chunk-size", type=int, default=25000, help="全量 assignment 分块大小")
    parser.add_argument("--max-rss-mb", type=int, default=1500, help="RSS 预算 MB")
    parser.add_argument(
        "--representation", choices=["absolute", "cross_sectional", "both"],
        default="both", help="特征表示方法",
    )
    parser.add_argument("--output-dir", type=str, default=DEFAULT_OUTPUT_DIR, help="输出根目录")
    args = parser.parse_args()
    if args.k_min > args.k_max:
        parser.error("--k-min 不能大于 --k-max")
    if args.k_min < 2:
        parser.error("--k-min 至少为 2")
    return args


def get_git_sha() -> str:
    """获取当前 git HEAD SHA。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def get_peak_rss_mb() -> float:
    """获取当前进程峰值 RSS（MB）。Linux 返回 KB。"""
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


def parse_date(s: str | None) -> date | None:
    if s is None:
        return None
    return date.fromisoformat(s)


def run_representation(
    features_df: pd.DataFrame,
    features: list[str],
    representation: str,
    k_range: tuple[int, int],
    seed: int,
    dates: pd.Series | None = None,
) -> dict[str, Any]:
    """跑单个 representation 的完整流程：preprocessing → clustering → stability。

    Returns:
        {representation, X, labels, k_selected, stable, stability_result,
         model_selection_df, cluster_profiles_df, described_features_df,
         preprocessing_params, winsorize_bounds, model, ...}
    """
    logger.info("=== representation=%s ===", representation)
    # 1. winsorize（fit + transform，保存 bounds 供 Phase E 复用）
    winsorize_bounds = preprocessing.fit_winsorize_bounds(features_df, features)
    winsorized = preprocessing.transform_winsorize(features_df, features, winsorize_bounds)
    # 2. 构建矩阵 X
    X, prep_params = preprocessing.build_feature_matrix(winsorized, features, representation)
    logger.info("X shape: %s, n_samples=%d, n_features=%d", X.shape, X.shape[0], X.shape[1])
    # 3. 相关性剪枝（基于 Spearman）
    corr_matrix, redundant = distribution_audit.audit_correlation(winsorized, features)
    pruned_features, dropped_pairs = preprocessing.correlation_prune(features, corr_matrix)
    if len(pruned_features) < len(features):
        logger.info("相关性剪枝: %d → %d（丢弃 %d）", len(features), len(pruned_features), len(dropped_pairs))
        # 重建 X 用剪枝后特征
        X, prep_params = preprocessing.build_feature_matrix(winsorized, pruned_features, representation)
    # 4. PCA
    pca_params = preprocessing.fit_pca(X)
    X_pca = preprocessing.transform_pca(X, pca_params)
    logger.info("PCA: %d → %d 维（解释方差 %.2f%%）",
                X.shape[1], pca_params["n_components"],
                sum(pca_params["explained_variance_ratio"]) * 100)
    # 5. k 选择
    best_k, selection_info = models.select_best_k(X_pca, k_range=k_range, seed=seed)
    # 6. k 选择结果 DataFrame
    model_sel_df = pd.DataFrame(selection_info["all_metrics"])
    # 7. 稳定性检验
    stable = False
    stability_result: dict[str, Any] = {}
    cluster_profiles_df = pd.DataFrame()
    described_features_df = pd.DataFrame()
    labels: np.ndarray | None = None
    model = None
    if best_k is not None:
        stability_result = stability.check_stability(
            X_pca, best_k, seed=seed,
            dates=dates.to_numpy() if dates is not None else None,
        )
        stable = stability_result["pass"]
        logger.info("稳定性检验 k=%d: pass=%s, reasons=%s",
                    best_k, stable, stability_result["reasons"])
        if stable:
            model = models.fit_kmeans(X_pca, best_k, seed=seed)
            labels = model.labels_
            # 过滤 winsorized 使其行数与 X/labels 一致（build_feature_matrix 内部
            # 会 dropna 掉含 NaN/inf 的行，原始 winsorized 行数多于 X）
            _feat_na = (
                winsorized[pruned_features]
                .replace([np.inf, -np.inf], np.nan)
                .isna()
                .any(axis=1)
            )
            filtered_df = winsorized[~_feat_na].reset_index(drop=True)
            # 簇画像
            cluster_profiles_df = models.get_cluster_profiles(
                filtered_df, pruned_features, labels
            )
            # 簇描述（bootstrap）— 在原始特征空间计算 z-score，使描述可解释
            # （labels 仍来自主模型 fit on X_pca，但 centroid z-score 用 X_pruned）
            described_features_df = stability.describe_clusters_bootstrap(
                X, labels, pruned_features, seed=seed,
            )
    # 8. 写稳定性结果到 DataFrame
    if stability_result:
        stability_df = pd.DataFrame([{
            "k": stability_result["k"],
            "silhouette": stability_result["silhouette"],
            "ari_mean": stability_result["ari_mean"],
            "cosine": stability_result["cosine"],
            "min_cluster_ratio": stability_result["min_cluster_ratio"],
            "max_cluster_ratio": stability_result["max_cluster_ratio"],
            "pass": stability_result["pass"],
            "reasons": "; ".join(stability_result["reasons"]) if stability_result["reasons"] else "",
        }])
    else:
        stability_df = pd.DataFrame([{
            "k": None, "silhouette": None, "ari_mean": None, "cosine": None,
            "min_cluster_ratio": None, "max_cluster_ratio": None,
            "pass": False, "reasons": "k 未通过初步门槛",
        }])
    return {
        "representation": representation,
        "X": X_pca,
        "labels": labels,
        "k_selected": best_k,
        "stable": stable,
        "stability_result": stability_result,
        "model_selection_df": model_sel_df,
        "cluster_profiles_df": cluster_profiles_df,
        "described_features_df": described_features_df,
        "stability_df": stability_df,
        "redundant_pairs": redundant,
        "pruned_features": pruned_features,
        "dropped_pairs": dropped_pairs,
        "pca_params": {
            "n_components": pca_params["n_components"],
            "explained_variance_ratio": pca_params["explained_variance_ratio"],
            "model": pca_params["model"],
        },
        "winsorize_bounds": winsorize_bounds,
        "scaler_params": prep_params.get("scaler", {}),
        "scaler": prep_params.get("scaler", {}),
        "model": model,
    }


def main() -> int:
    """主入口。"""
    args = parse_args()
    start = parse_date(args.start)
    end = parse_date(args.end)
    k_range = (args.k_min, args.k_max)

    logger.info("=" * 60)
    logger.info("Regime Discovery V1")
    logger.info("=" * 60)
    logger.info("参数: sample_rows=%d, seed=%d, k_range=%s, representation=%s",
                args.sample_rows, args.seed, k_range, args.representation)
    logger.info("日期: start=%s, end=%s", start, end)
    logger.info("输出目录: %s", args.output_dir)

    git_sha = get_git_sha()
    logger.info("Git SHA: %s", git_sha)

    # === dry-run ===
    if args.dry_run:
        logger.info("[dry-run] 计划：")
        logger.info("  1. 连接只读 DB（127.0.0.1:15432）")
        logger.info("  2. stratified_sample(%d rows, seed=%d)", args.sample_rows, args.seed)
        logger.info("  3. build_features → 17 特征")
        logger.info("  4. 分布审计（5 项）")
        logger.info("  5. representation=%s → winsorize → scaler → PCA → kmeans × %s",
                    args.representation, k_range)
        logger.info("  6. stability（bootstrap ARI + centroid cosine）")
        logger.info("  7. 若 k 通过：全量 assignment（get_all_matrix_rows 完整横截面 rank）")
        logger.info("  8. 写 manifest + 9 CSV + report.md")
        logger.info("  9. enforce_output_size(50MB) + enforce_max_runs(3)")
        logger.info("[dry-run] 退出，不查 DB 不写文件。")
        return 0

    # === 创建只读 session ===
    engine = data_access.create_readonly_engine()
    session = data_access.get_session(engine)
    try:
        # 查询元数据
        data_as_of = data_access.get_data_as_of(session)
        data_min, data_max = data_access.get_data_range(session)
        sql_row_count = data_access.get_total_row_count(session, start, end)
        logger.info("数据范围: %s ~ %s（as_of=%s, rows=%d）",
                    data_min, data_max, data_as_of, sql_row_count)

        # === 分层抽样 ===
        logger.info("分层抽样 sample_rows=%d seed=%d ...", args.sample_rows, args.seed)
        matrix_df = data_access.stratified_sample(
            session, sample_rows=args.sample_rows, seed=args.seed, start=start, end=end,
        )
        logger.info("抽样完成: %d 行", len(matrix_df))

        # === 获取 close 价格（用研究矩阵日期范围过滤，避免拉全历史） ===
        inst_ids = matrix_df["instrument_id"].unique().tolist()
        close_df = data_access.fetch_close_prices(
            session, instrument_ids=inst_ids, start=data_min, end=data_max,
        )
        logger.info("close 价格: %d 行", len(close_df))

        # === 构建特征 ===
        features_df = feature_builder.build_features(matrix_df, close_df)
        features = feature_builder.CLUSTERING_FEATURE_WHITELIST
        logger.info("特征构建完成: %d 特征 × %d 行", len(features), len(features_df))

        # 二次确认无泄漏
        feature_builder.validate_no_leakage(features)

        # === 分布审计 ===
        logger.info("分布审计 ...")
        distribution_df = distribution_audit.audit_distribution(features_df, features)
        drift_df = distribution_audit.audit_monthly_drift(features_df, features)
        coverage_df = distribution_audit.audit_stock_coverage(features_df, features)
        corr_matrix, redundant_pairs = distribution_audit.audit_correlation(features_df, features)
        discrete_df = distribution_audit.audit_discrete(
            features_df, list(feature_builder.DIRECTION_FEATURES)
        )
        logger.info(
            "分布审计完成: %d 字段, %d 月漂移, %d 冗余对",
            len(distribution_df), len(drift_df), len(redundant_pairs),
        )

        # === 按 representation 跑 ===
        dates_series = features_df["trade_date"] if "trade_date" in features_df.columns else None
        # dropna 后用于聚类的行索引
        results: dict[str, dict[str, Any]] = {}
        reps_to_run = ["absolute", "cross_sectional"] if args.representation == "both" else [args.representation]
        for rep in reps_to_run:
            results[rep] = run_representation(
                features_df, features, rep, k_range, args.seed, dates=dates_series,
            )

        # === Phase E: 全量 assignment（若任一 representation 通过） ===
        # 使用 get_all_matrix_rows 读取全量数据，确保横截面 rank 基于
        # 完整市场横截面计算（而非 chunk 子集）。严禁按行 chunk 分割
        # 同一交易日后分别 rank。
        transition_df = pd.DataFrame()
        prevalence_df = pd.DataFrame()
        dwell_df = pd.DataFrame()
        full_assignment_rows = 0
        any_stable = any(r["stable"] for r in results.values())
        chosen_rep = next((r for r in results.values() if r["stable"]), None)

        # 保存 sample 行数供 manifest 使用（matrix_df 可能即将被释放）
        sample_rows_count = len(matrix_df)

        if any_stable and chosen_rep is not None:
            logger.info("=== Phase E: 全量 assignment（完整横截面 rank） ===")
            logger.info("使用 representation=%s 做全量 assignment", chosen_rep["representation"])

            # 1. 释放样本数据腾出内存（features 列表后续 manifest 仍需使用）
            del matrix_df, close_df, features_df
            # 同时释放 results 中各 representation 的 X 矩阵（Phase E 会重新计算 X_full）
            for _rep, _r in results.items():
                _r["X"] = None
                _r["labels"] = None
            gc.collect()
            logger.info("已释放样本数据, RSS=%.0f MB", get_peak_rss_mb())

            # 2. 读取全量研究矩阵（不分块，保证横截面完整）
            full_matrix_df = data_access.get_all_matrix_rows(session, start, end)
            logger.info(
                "全量矩阵: %d 行 × %d 列, RSS=%.0f MB",
                len(full_matrix_df), full_matrix_df.shape[1], get_peak_rss_mb(),
            )

            # 3. 全量 close 价格（用 data_min/data_max 过滤日期范围）
            all_inst_ids = data_access.get_all_instrument_ids(session, start, end)
            logger.info("全量 instrument 数: %d", len(all_inst_ids))
            full_close_df = data_access.fetch_close_prices(
                session, instrument_ids=all_inst_ids,
                start=data_min, end=data_max,
            )
            logger.info("全量 close 价格: %d 行", len(full_close_df))

            # 4. build_features（全量数据，时序派生需要完整历史）
            full_features_df = feature_builder.build_features(full_matrix_df, full_close_df)
            del full_matrix_df, full_close_df
            gc.collect()
            logger.info(
                "全量特征构建完成: %d 行, RSS=%.0f MB",
                len(full_features_df), get_peak_rss_mb(),
            )

            # 5. winsorize with sample-fitted bounds
            full_winsorized = preprocessing.transform_winsorize(
                full_features_df, features, chosen_rep["winsorize_bounds"]
            )

            # 6. transform_feature_matrix：cross_sectional rank 基于完整市场横截面
            X_full = preprocessing.transform_feature_matrix(
                full_winsorized, chosen_rep["pruned_features"],
                chosen_rep["representation"], chosen_rep,
            )
            logger.info(
                "全量 X: shape=%s, RSS=%.0f MB",
                X_full.shape, get_peak_rss_mb(),
            )

            if X_full.shape[0] == 0:
                logger.warning("全量 assignment 无有效行（X 为空）")
            else:
                # 7. PCA transform
                X_full_pca = preprocessing.transform_pca(X_full, chosen_rep["pca_params"])
                # 8. predict
                full_labels = models.assign_clusters(chosen_rep["model"], X_full_pca)

                # 9. 对齐 trade_date/instrument_id 到有效行
                valid_mask = ~full_features_df[chosen_rep["pruned_features"]].replace(
                    [np.inf, -np.inf], np.nan
                ).isna().any(axis=1)
                valid_idx = valid_mask[valid_mask].index
                full_dates_arr = full_features_df.loc[valid_idx, "trade_date"].to_numpy()
                full_inst_arr = full_features_df.loc[valid_idx, "instrument_id"].to_numpy()
                full_assignment_rows = len(full_labels)
                logger.info(
                    "全量 assignment 完成: %d 有效行（原始 %d, SQL 总 %d）",
                    full_assignment_rows, len(full_features_df), sql_row_count,
                )

                # 10. 聚合
                transition_df = stability.compute_transition_matrix(
                    full_labels, full_inst_arr, full_dates_arr
                )
                prevalence_df = stability.monthly_prevalence(
                    full_labels, full_dates_arr
                )
                dwell_df = stability.compute_dwell_time(
                    full_labels, full_inst_arr, full_dates_arr
                )

                # 11. 释放全量矩阵
                del X_full, X_full_pca, full_winsorized, full_features_df
                gc.collect()
        else:
            logger.info("所有 representation 均未通过稳定性门槛，跳过全量 assignment")

        # === 选最终结果（优先 stable 的，否则取 absolute） ===
        final_rep = next((r for r in results.values() if r["stable"]), None)
        if final_rep is None:
            final_rep = results.get("absolute") or next(iter(results.values()))
        k_selected = final_rep["k_selected"]
        stable = final_rep["stable"]

        # === 准备输出 ===
        # model_selection_df：合并所有 representation
        model_sel_rows: list[dict[str, Any]] = []
        for rep, r in results.items():
            for m in r["model_selection_df"].to_dict("records"):
                m["representation"] = rep
                model_sel_rows.append(m)
        model_sel_df = pd.DataFrame(model_sel_rows)

        # cluster_stability_df：合并
        stability_rows: list[dict[str, Any]] = []
        for rep, r in results.items():
            for s in r["stability_df"].to_dict("records"):
                s["representation"] = rep
                stability_rows.append(s)
        stability_df = pd.DataFrame(stability_rows)

        # cluster_profiles_df
        profiles_df = final_rep["cluster_profiles_df"]
        # described_features_df
        described_df = final_rep["described_features_df"]

        # === 创建 run 目录 ===
        run_dir = reporting.create_run_dir(args.output_dir, seed=args.seed)
        # === 生成 report.md ===
        manifest_for_report = {
            "run_id": run_dir.name,
            "git_sha": git_sha,
            "data_as_of": str(data_as_of),
            "sample_rows": sample_rows_count,
            "seed": args.seed,
            "representation": args.representation,
            "k_range": list(k_range),
            "peak_rss_mb": get_peak_rss_mb(),
            "feature_list": features,
            "excluded_reasons": feature_builder.get_excluded_reasons(),
            "thresholds": models.get_thresholds(),
        }
        report_md = reporting.generate_report_md(
            manifest=manifest_for_report,
            distribution=distribution_df,
            drift=drift_df,
            model_selection=model_sel_df,
            cluster_profiles=profiles_df,
            cluster_stability=stability_df,
            transition=transition_df,
            k_selected=k_selected,
            stable=stable,
            redundant_pairs=redundant_pairs,
            described_features=described_df,
        )
        # === 写 manifest ===
        manifest_data: dict[str, Any] = {
            "run_id": run_dir.name,
            "git_sha": git_sha,
            "data_as_of": str(data_as_of),
            "sql_row_count": sql_row_count,
            "feature_list": features,
            "excluded_reasons": feature_builder.get_excluded_reasons(),
            "seed": args.seed,
            "thresholds": models.get_thresholds(),
            "model_params": models.get_model_params_summary(
                k_range, args.seed, args.representation
            ),
            "peak_rss_mb": get_peak_rss_mb(),
            "representation": args.representation,
            "sample_rows": sample_rows_count,
            "k_range": list(k_range),
            "created_at": pd.Timestamp.now().isoformat(),
            "pca_summary": {
                rep: {
                    "n_components": r["pca_params"]["n_components"],
                    "explained_variance_ratio": list(r["pca_params"]["explained_variance_ratio"]),
                }
                for rep, r in results.items()
            },
            "redundant_pairs": distribution_audit.summarize_redundant_pairs(redundant_pairs),
            "coverage_summary": coverage_df.to_dict("records") if not coverage_df.empty else [],
            "discrete_summary": discrete_df.to_dict("records") if not discrete_df.empty else [],
            "k_selected": k_selected,
            "stable": stable,
            "full_assignment": {
                "rows": full_assignment_rows,
                "representation": chosen_rep["representation"] if chosen_rep else None,
                "method": "get_all_matrix_rows",
                "cross_section_scope": "full_market_per_trade_date",
            },
        }
        # === 写所有文件 ===
        reporting.write_all(
            run_dir,
            manifest_data=manifest_data,
            distribution_df=distribution_df,
            drift_df=drift_df,
            model_sel_df=model_sel_df,
            profiles_df=profiles_df,
            stability_df=stability_df,
            transition_df=transition_df if not transition_df.empty else pd.DataFrame({"info": ["无 transition（k 未通过或样本不足）"]}),
            prevalence_df=prevalence_df if not prevalence_df.empty else None,
            dwell_df=dwell_df if not dwell_df.empty else None,
            report_md=report_md,
        )
        # === 3 run 保留 ===
        reporting.enforce_max_runs(Path(args.output_dir))
        # === 摘要 ===
        logger.info("=" * 60)
        logger.info("完成！输出目录: %s", run_dir)
        logger.info("输出文件: %s", reporting.list_output_files(run_dir))
        logger.info("k_selected=%s, stable=%s", k_selected, stable)
        logger.info("RSS 峰值: %.2f MB", get_peak_rss_mb())
        logger.info("=" * 60)
        return 0
    except Exception:
        logger.exception("Regime Discovery 失败")
        return 1
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
