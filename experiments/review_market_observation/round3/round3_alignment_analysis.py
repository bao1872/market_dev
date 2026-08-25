"""Round 3 §6-8 — Component→Primitive Alignment + Coverage Matrix + Primitive Coverage。

输入：
- round2_daily_observation.csv （Round 2 primitives）
- round3_current_review_daily.csv （Current Review daily values）
- round3_current_component_map.csv （component 事实映射）

输出：
- round3_alignment_matrix.csv （§7 matrix）
- round3_primitive_coverage.json （§8）
- round3_component_alignment_detail.json （§6 detail）
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np

OBS_DIMENSIONS = [
    "STATE",
    "TRANSITION",
    "BREADTH",
    "DIFFUSION",
    "CONCENTRATION",
    "PARTICIPATION",
    "PRICE",
    "CROSS_HORIZON_DIVERGENCE",
]

# Round 2 primitive columns → 语义维度映射（从 Round 2 事实列推断）
R2_PRIMITIVE_TO_DIM: dict[str, str] = {
    # State
    "regime_up_ratio": "STATE",
    "regime_down_ratio": "STATE",
    "regime_flat_ratio": "STATE",
    "swing_up_ratio": "STATE",
    "swing_down_ratio": "STATE",
    "internal_up_ratio": "STATE",
    "internal_down_ratio": "STATE",
    "momentum_expanding_ratio": "STATE",
    "momentum_contracting_ratio": "STATE",
    # Transition
    "t_regime_0_1_rate": "TRANSITION",
    "t_regime_0_neg1_rate": "TRANSITION",
    "t_regime_1_0_rate": "TRANSITION",
    "t_regime_neg1_0_rate": "TRANSITION",
    "t_swing_neg1_1_rate": "TRANSITION",
    "t_swing_1_neg1_rate": "TRANSITION",
    "t_internal_neg1_1_rate": "TRANSITION",
    "t_internal_1_neg1_rate": "TRANSITION",
    "t_momdir_contract_expand_rate": "TRANSITION",
    "t_momdir_expand_contract_rate": "TRANSITION",
    # Breadth
    "advance_ratio_actual": "BREADTH",  # 如果列名不同，下方代码会自动找匹配
    "advance_members": "BREADTH",
    "decline_members": "BREADTH",
    "up_down_ratio": "BREADTH",
    # Diffusion
    "structure_alignment_ratio": "DIFFUSION",
    "structure_divergence_ratio": "DIFFUSION",
    # Concentration
    "top5_price_contribution": "CONCENTRATION",
    "top5_amount_contribution": "CONCENTRATION",
    "member_change_hhi": "CONCENTRATION",
    "top5_price_return": "CONCENTRATION",
    "top5_amount_return": "CONCENTRATION",
    # Participation
    "volume_expansion_ratio_actual": "PARTICIPATION",
    "amount_expansion_ratio_actual": "PARTICIPATION",
    "median_volume_ratio20": "PARTICIPATION",
    "median_amount_ratio20": "PARTICIPATION",
    "active_members": "PARTICIPATION",
    # Cross-horizon
    "cross_horizon_coordinated_ratio": "CROSS_HORIZON_DIVERGENCE",
    "cross_horizon_divergent_ratio": "CROSS_HORIZON_DIVERGENCE",
}


def _spearman(a: pd.Series, b: pd.Series) -> float | None:
    """两序列 Spearman，自动 drop NA。"""
    mask = a.notna() & b.notna()
    if mask.sum() < 3:
        return None
    xa = a[mask].astype(float).rank(method="average")
    xb = b[mask].astype(float).rank(method="average")
    r = xa.corr(xb, method="pearson")
    return None if pd.isna(r) else float(r)


def _find_round2_counterpart(component_name: str,
                             candidate_dim: str,
                             df_r2: pd.DataFrame) -> str | None:
    """为一个 review component 在 Round 2 中找最相关的同维度 primitive。"""
    # 同维度的 R2 列
    same_dim_cols = [col for col, dim in R2_PRIMITIVE_TO_DIM.items()
                     if dim == candidate_dim and col in df_r2.columns]
    if not same_dim_cols:
        # 回退：模糊匹配列名
        same_dim_cols = [c for c in df_r2.columns
                         if c not in ("trade_date",) and not c.endswith(
                             ("_denom", "_count", "_members"))]
    return same_dim_cols  # 返回候选列表，调用方选 Spearman 最高的


def load_inputs(out_root: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    r2_csv = out_root / "round2_db_native" / "round2_daily_observation.csv"
    r3_review_csv = out_root / "round3" / "round3_current_review_daily.csv"
    r3_map_csv = out_root / "round3" / "round3_current_component_map.csv"
    df_r2 = pd.read_csv(r2_csv).set_index("trade_date")
    df_review = pd.read_csv(r3_review_csv).set_index("trade_date")
    df_map = pd.read_csv(r3_map_csv)
    # 对齐索引
    common_dates = df_r2.index.intersection(df_review.index)
    df_r2 = df_r2.loc[common_dates]
    df_review = df_review.loc[common_dates]
    return df_r2, df_review, df_map


def _same_family_spearman(comp_name: str, family: str,
                          df_map: pd.DataFrame,
                          df_review: pd.DataFrame) -> list[tuple[str, float]]:
    """同 family 其他 component 与该 comp 的 Spearman（检测冗余）。"""
    same_family = df_map[(df_map["family"] == family)
                         & (df_map["component_name"] != comp_name)]["component_name"].tolist()
    pairs: list[tuple[str, float]] = []
    for other in same_family:
        if other in df_review.columns:
            rho = _spearman(df_review[comp_name], df_review[other])
            if rho is not None and abs(rho) >= 0.7:
                pairs.append((other, round(rho, 4)))
    return pairs


def run_alignment(out_root: Path, r2_obs_csv: Path | None = None,
                  review_csv: Path | None = None,
                  comp_map_csv: Path | None = None) -> dict[str, Any]:
    # ---- 加载数据 ----
    if r2_obs_csv is None:
        df_r2 = pd.read_csv(out_root / "round2_db_native" /
                            "round2_daily_observation.csv").set_index("trade_date")
    else:
        df_r2 = pd.read_csv(r2_obs_csv).set_index("trade_date")
    if review_csv is None:
        df_review = pd.read_csv(out_root / "round3" /
                                "round3_current_review_daily.csv").set_index("trade_date")
    else:
        df_review = pd.read_csv(review_csv).set_index("trade_date")
    if comp_map_csv is None:
        df_map = pd.read_csv(out_root / "round3" /
                             "round3_current_component_map.csv")
    else:
        df_map = pd.read_csv(comp_map_csv)

    common_dates = df_r2.index.intersection(df_review.index)
    df_r2 = df_r2.loc[common_dates]
    df_review = df_review.loc[common_dates]

    # ---- §6 逐 component 对齐 ----
    matrix_rows: list[dict[str, Any]] = []
    detail: dict[str, dict[str, Any]] = {}

    for _, comp in df_map.iterrows():
        cname = comp["component_name"]
        family = comp["family"]
        dim = comp["candidate_observation_dimension"]
        if cname not in df_review.columns:
            continue

        # 找同维度 R2 primitive 中最相关的
        r2_candidates = _find_round2_counterpart(cname, dim, df_r2)
        best_r2_col = None
        best_rho = None
        for col in r2_candidates:
            if col not in df_r2.columns:
                continue
            rho = _spearman(df_review[cname], df_r2[col])
            if rho is None:
                continue
            if best_rho is None or abs(rho) > abs(best_rho):
                best_rho = rho
                best_r2_col = col

        # 检查是否混合多个 primitive（如果和多个不同维度 R2 列都高相关）
        multi_dim_hits: list[tuple[str, str, float]] = []
        for col in df_r2.columns:
            if col in ("trade_date",):
                continue
            # 跳过 count/denom 技术列
            if col.endswith(("_denom", "_count", "_members")) or col.startswith("n_"):
                continue
            rho2 = _spearman(df_review[cname], df_r2[col])
            if rho2 is None or abs(rho2) < 0.7:
                continue
            r2_dim = R2_PRIMITIVE_TO_DIM.get(col, "UNKNOWN")
            if (r2_dim != dim or dim in ("MIXED", "OTHER")) and r2_dim != "UNKNOWN":
                multi_dim_hits.append((col, r2_dim, round(rho2, 4)))

        # 同 family 冗余检查
        redundant_with = _same_family_spearman(cname, family, df_map, df_review)

        # finding 判定
        if best_rho is None:
            finding = "NO_DIRECT_COUNTERPART"
        elif abs(best_rho) < 0.5:
            finding = "PARTIAL"
        elif len(multi_dim_hits) >= 2:
            finding = "MIXED"
        elif len(redundant_with) >= 1:
            finding = "REDUNDANT_CANDIDATE"
        else:
            finding = "COVERED"

        evidence_parts = []
        if best_r2_col:
            evidence_parts.append(f"spearman_vs_{best_r2_col}={best_rho:.4f}")
        if multi_dim_hits:
            evidence_parts.append("multi_dim=" + ";".join(
                f"{c}({d}):{r}" for c, d, r in multi_dim_hits[:3]))
        if redundant_with:
            evidence_parts.append("same_fam_rho≥0.7=" + ";".join(
                f"{c}:{r}" for c, r in redundant_with))

        matrix_rows.append({
            "component": cname,
            "family": family,
            "semantic_dimension": dim,
            "round2_counterpart": best_r2_col or "",
            "spearman": f"{best_rho:.4f}" if best_rho is not None else "",
            "coverage": finding,
            "finding": finding,
            "evidence": " | ".join(evidence_parts),
        })
        detail[cname] = {
            "family": family,
            "semantic_dimension": dim,
            "best_r2_column": best_r2_col,
            "best_spearman": best_rho,
            "multi_dimension_correlations": multi_dim_hits,
            "same_family_redundant_pairs": redundant_with,
            "finding": finding,
        }

    # ---- §8 Primitive coverage ----
    dim_covered: dict[str, dict[str, Any]] = {}
    for dim in OBS_DIMENSIONS:
        # 找到 semantic_dimension = 该 dim 的全部 component 及其 finding
        dim_comps = [(r["component"], r["finding"], float(r["spearman"])
                      if r["spearman"] else None)
                     for r in matrix_rows if r["semantic_dimension"] == dim]
        # 也包括 MIXED 中混有该 dim 的
        mixed_hits = []
        for cname, d in detail.items():
            for (col, mdim, r) in d["multi_dimension_correlations"]:
                if mdim == dim and (cname, "MIXED") not in [(c, f) for c, f, _ in dim_comps]:
                    mixed_hits.append((cname, r))

        n_direct = len(dim_comps)
        n_covered = sum(1 for _, f, _ in dim_comps if f == "COVERED")
        n_partial = sum(1 for _, f, _ in dim_comps if f == "PARTIAL")

        if n_covered >= 1 and (n_covered + n_partial) >= n_direct * 0.6:
            verdict = "COVERED"
        elif (n_covered + n_partial) >= 1 or mixed_hits:
            verdict = "PARTIAL"
        else:
            verdict = "MISSING"

        dim_covered[dim] = {
            "verdict": verdict,
            "direct_components": [{"name": c, "finding": f, "spearman": s}
                                   for c, f, s in dim_comps],
            "mixed_components": [{"name": c, "spearman": s} for c, s in mixed_hits],
            "n_direct": n_direct,
            "n_covered": n_covered,
        }

    # 特别处理 CROSS_HORIZON_DIVERGENCE：目前 component 定义中没有直接表达跨周期
    # 看 trend_structure_momentum_alignment_ratio 是否相关
    # 以及是否有 R2 的 coordinated/divergent 列
    ch_cols = [c for c in df_r2.columns
               if "coordinated" in c.lower() or "divergent" in c.lower()
               or "cross" in c.lower() or "weak_trend" in c or "strong_trend" in c]
    if ch_cols:
        dim_covered["CROSS_HORIZON_DIVERGENCE"]["round2_columns"] = ch_cols
    else:
        # 从 Round2 archetype/correlations JSON 查
        dim_covered["CROSS_HORIZON_DIVERGENCE"]["note"] = (
            "no direct component; trend_structure_momentum_alignment_ratio touches DIFFUSION "
            "but not explicit cross-horizon divergence labels"
        )

    # 写输出
    out_round3 = out_root / "round3"
    out_round3.mkdir(parents=True, exist_ok=True)
    with open(out_round3 / "round3_alignment_matrix.csv", "w",
              newline="", encoding="utf-8") as f:
        fn = list(matrix_rows[0].keys()) if matrix_rows else []
        w = csv.DictWriter(f, fieldnames=fn)
        w.writeheader()
        w.writerows(matrix_rows)

    (out_round3 / "round3_primitive_coverage.json").write_text(
        json.dumps(dim_covered, indent=2, ensure_ascii=False, default=str)
    )
    (out_round3 / "round3_component_alignment_detail.json").write_text(
        json.dumps(detail, indent=2, ensure_ascii=False, default=str)
    )

    return {
        "n_components": len(matrix_rows),
        "findings_distribution": {
            k: sum(1 for r in matrix_rows if r["finding"] == k)
            for k in ("COVERED", "PARTIAL", "MIXED",
                      "REDUNDANT_CANDIDATE", "NO_DIRECT_COUNTERPART", "UNVERIFIED")
        },
        "primitive_verdicts": {d: v["verdict"] for d, v in dim_covered.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-root", type=Path,
                        default=Path(__file__).resolve().parents[1] / "out")
    args = parser.parse_args()
    summary = run_alignment(args.out_root)
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
