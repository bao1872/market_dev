"""Round 3 §9-11 — P/Q/U/C/V Compression Audit + Archetype Replay + Cross-horizon Replay。

输入：
- round2_daily_observation.csv + round2_archetype_days.json
- round3_current_review_daily.csv + alignment_matrix + primitive_coverage

输出：
- round3_family_compression_audit.json （§9）
- round3_archetype_replay.json （§10）
- round3_cross_horizon_replay.json （§11）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np

ARCHETYPE_DAYS: list[str] = [
    "2026-04-01", "2026-03-24", "2026-04-17",
    "2026-03-23", "2026-03-31", "2026-06-05",
    "2026-05-13", "2026-03-02", "2026-03-03",
]

FAMILY_CODES = ["P", "Q", "U", "C", "V"]


def _spearman(a: pd.Series, b: pd.Series) -> float | None:
    mask = a.notna() & b.notna()
    if mask.sum() < 3:
        return None
    return float(a[mask].astype(float).rank(method="average")
                 .corr(b[mask].astype(float).rank(method="average")))


def _cos_sim(v1: list[float], v2: list[float]) -> float:
    n1 = np.linalg.norm(v1)
    n2 = np.linalg.norm(v2)
    if n1 == 0 or n2 == 0:
        return 0.0
    return float(np.dot(v1, v2) / (n1 * n2))


# ============================================================
# §9 Compression Audit
# ============================================================

def _family_internal_structure(df_map: pd.DataFrame, df_review: pd.DataFrame,
                               family: str) -> dict[str, Any]:
    """分析单个 family 的 component 结构。"""
    comps = df_map[df_map["family"] == family]["component_name"].tolist()
    dims = df_map[df_map["family"] == family][
        ["component_name", "candidate_observation_dimension"]
    ].set_index("component_name")["candidate_observation_dimension"].to_dict()

    # 检查 components 跨了多少 primitive dims
    unique_dims = set(dims.values())
    n_dims = len(unique_dims)

    # 同 family 内 component 间相关矩阵
    pair_corrs: list[tuple[str, str, float]] = []
    for i, c1 in enumerate(comps):
        for c2 in comps[i + 1:]:
            if c1 in df_review.columns and c2 in df_review.columns:
                rho = _spearman(df_review[c1], df_review[c2])
                if rho is not None:
                    pair_corrs.append((c1, c2, rho))
    high_corr_pairs = [(a, b, round(r, 4))
                        for a, b, r in pair_corrs if abs(r) >= 0.7]

    # family raw value vs components 的方差解释
    # 用 metric_X_rawValue 作为 Y，看 component 是否权重均匀
    raw_col = f"metric_{family}_rawValue"
    if raw_col in df_review.columns:
        y = df_review[raw_col].astype(float).fillna(0).tolist()
        # 看每个 component 与 family raw 的相关性
        comp_to_family: list[tuple[str, float]] = []
        for c in comps:
            if c in df_review.columns:
                rho = _spearman(df_review[c], df_review[raw_col])
                if rho is not None:
                    comp_to_family.append((c, round(rho, 4)))
    else:
        comp_to_family = []

    # Verdict
    if family == "C":
        # C 包含 price concentration + event concentration + amount concentration
        # 这些是否应该压成一个 C？——如果不同 component 间 rho 低，则不该压
        low_pairs = [(a, b, r) for a, b, r in pair_corrs if abs(r) < 0.4]
        if len(low_pairs) >= len(pair_corrs) * 0.5:
            verdict = "RESTRUCTURE_CANDIDATE"
        else:
            verdict = "RETAIN_AS_SUMMARY"
    elif family == "V":
        # V = Volume 还是广义 Participation？
        # 看 V 的 components 是否主要映射到 PARTICIPATION（事实在 component_map 已标）
        if "PARTICIPATION" in unique_dims and n_dims <= 2:
            verdict = "RETAIN"
        else:
            verdict = "INCONCLUSIVE"
    elif n_dims >= 3 or len(high_corr_pairs) == 0 and n_dims >= 2:
        # 多个独立维度混合到一个 family
        verdict = "RESTRUCTURE_CANDIDATE" if n_dims >= 3 else "PARTIAL"
    else:
        verdict = "RETAIN"

    if verdict == "PARTIAL":
        verdict = "RETAIN_AS_SUMMARY"

    return {
        "components": comps,
        "dimensions_spanned": sorted(unique_dims),
        "n_dimensions": n_dims,
        "component_pair_correlations": {
            "total_pairs": len(pair_corrs),
            "high_corr_pairs_ge_0_7": high_corr_pairs,
            "low_corr_pairs_lt_0_4": [(a, b, round(r, 4))
                                       for a, b, r in pair_corrs if abs(r) < 0.4],
        },
        "component_to_family_raw_corr": sorted(
            comp_to_family, key=lambda x: abs(x[1]), reverse=True
        ),
        "verdict": verdict,
    }


def compression_audit(df_map: pd.DataFrame, df_review: pd.DataFrame) -> dict[str, Any]:
    return {f: _family_internal_structure(df_map, df_review, f) for f in FAMILY_CODES}


# ============================================================
# §10 Archetype Day Replay
# ============================================================

def _r2_state(dr: pd.Series) -> dict[str, Any]:
    """从 Round 2 行提取核心 primitive。"""
    out = {}
    for key in ["regime_up_ratio", "regime_down_ratio", "swing_up_ratio",
                "momentum_expanding_ratio"]:
        if key in dr.index:
            out[key] = None if pd.isna(dr[key]) else float(dr[key])
    return {"state_ratios": out}


def _r2_transition(dr: pd.Series) -> dict[str, Any]:
    rates = {}
    for key in ["t_regime_0_1_rate", "t_regime_0_neg1_rate",
                "t_swing_neg1_1_rate", "t_momdir_contract_expand_rate"]:
        if key in dr.index:
            rates[key] = None if pd.isna(dr[key]) else float(dr[key])
    return {"transition_rates": rates}


def _r2_diffusion(dr: pd.Series) -> dict[str, Any]:
    # 找扩散相关列
    cols = [c for c in dr.index if any(k in c.lower()
            for k in ["align", "diver", "diffus", "breadth"])]
    out = {c: (None if pd.isna(dr[c]) else float(dr[c])) for c in cols}
    # breadth 也放这里便于对比
    for c in dr.index:
        if "advance_ratio" in c.lower() or c in ("advance_ratio",):
            out["advance_ratio_R2"] = None if pd.isna(dr[c]) else float(dr[c])
    return out


def _r2_concentration(dr: pd.Series) -> dict[str, Any]:
    return {
        k: (None if pd.isna(dr[k]) else float(dr[k]))
        for k in ["top5_price_contribution", "member_change_hhi",
                  "top5_amount_contribution"]
        if k in dr.index
    }


def _r2_participation(dr: pd.Series) -> dict[str, Any]:
    cols = [c for c in dr.index if any(k in c.lower()
            for k in ["volume_expansion", "amount_expansion", "volume_ratio",
                      "amount_ratio", "active"])]
    return {c: (None if pd.isna(dr[c]) else float(dr[c])) for c in cols}


def _review_family_vals(dr: pd.Series) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in FAMILY_CODES:
        for suffix in ["_value", "_rawValue", "_status"]:
            k = f"metric_{f}{suffix}"
            if k in dr.index:
                out[k] = None if pd.isna(dr[k]) else (
                    float(dr[k]) if suffix != "_status" else str(dr[k])
                )
    return out


def _review_key_components(dr: pd.Series, df_map: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """对每个 family 选出该日值最高/最低的 component。"""
    out: dict[str, dict[str, Any]] = {}
    for f in FAMILY_CODES:
        comps = df_map[df_map["family"] == f]["component_name"].tolist()
        vals: list[tuple[str, float]] = []
        for c in comps:
            if c in dr.index and not pd.isna(dr[c]):
                try:
                    vals.append((c, float(dr[c])))
                except (TypeError, ValueError):
                    pass
        vals.sort(key=lambda x: x[1], reverse=True)
        out[f] = {
            "top3": [{"name": n, "raw": round(v, 4)} for n, v in vals[:3]],
            "bottom3": [{"name": n, "raw": round(v, 4)} for n, v in vals[-3:]],
        }
    return out


def _clarify_verdict(r2_side: dict[str, Any], review_vals: dict[str, Any]) -> tuple[str, str]:
    """判断 Current Review 是否能清楚表达这一天。"""
    reasons: list[str] = []
    # P 的 value 是否和 R2 regime 同向
    p_val = review_vals.get("metric_P_value")
    ru = r2_side.get("state_ratios", {}).get("regime_up_ratio")
    rd = r2_side.get("state_ratios", {}).get("regime_down_ratio")
    if p_val is not None and ru is not None and rd is not None:
        expected_p_high = ru > rd  # 上行情景 P 应该高
        actual_p_high = p_val > 50
        if expected_p_high != actual_p_high:
            reasons.append("P与state方向矛盾")
    # Q 是否能表达 Transition
    trans = r2_side.get("transition_rates", {})
    t_regime_up = trans.get("t_regime_0_1_rate")
    q_val = review_vals.get("metric_Q_value")
    if t_regime_up is not None and q_val is not None:
        if t_regime_up > 0.15 and q_val < 40:
            reasons.append("强transition但Q未同步反映")
    # U 是否表达 Participation
    if len(reasons) == 0:
        return "CLEAR", "P/Q/U/C/V与R2 primitive 无明显矛盾"
    elif len(reasons) <= 2:
        return "PARTIAL", "; ".join(reasons)
    else:
        return "OBSCURED", "; ".join(reasons)


def archetype_replay(out_root: Path,
                     df_r2: pd.DataFrame,
                     df_review: pd.DataFrame,
                     df_map: pd.DataFrame) -> dict[str, Any]:
    # 也尝试从 JSON 加载 archetype
    archetype_path = out_root / "round2_db_native" / "round2_archetype_days.json"
    if archetype_path.exists():
        try:
            data = json.loads(archetype_path.read_text())
            extra_days = []
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, str) and "202" in v:
                        extra_days.append(v[:10])
                    elif isinstance(v, dict) and "trade_date" in v:
                        extra_days.append(str(v["trade_date"])[:10])
                    elif isinstance(v, list):
                        for item in v:
                            if isinstance(item, str) and "202" in item:
                                extra_days.append(item[:10])
                            elif isinstance(item, dict) and "trade_date" in item:
                                extra_days.append(str(item["trade_date"])[:10])
            # 去重合并
            all_days = list(dict.fromkeys(ARCHETYPE_DAYS + extra_days))
        except Exception:
            all_days = list(ARCHETYPE_DAYS)
    else:
        all_days = list(ARCHETYPE_DAYS)

    results: dict[str, Any] = {}
    for d in all_days:
        if d not in df_r2.index or d not in df_review.index:
            results[d] = {"status": "DATE_NOT_FOUND"}
            continue
        r2_row = df_r2.loc[d]
        rvw_row = df_review.loc[d]
        r2_side = {
            **_r2_state(r2_row),
            "Transition": _r2_transition(r2_row),
            "Diffusion": _r2_diffusion(r2_row),
            "Concentration": _r2_concentration(r2_row),
            "Participation": _r2_participation(r2_row),
        }
        review_vals = _review_family_vals(rvw_row)
        key_comps = _review_key_components(rvw_row, df_map)
        verdict, reason = _clarify_verdict(r2_side, review_vals)
        results[d] = {
            "Round2": r2_side,
            "CurrentReview": {
                "P_Q_U_C_V": review_vals,
                "key_components": key_comps,
            },
            "clarity_verdict": verdict,
            "clarity_reason": reason,
        }
    return results


# ============================================================
# §11 Cross-horizon divergence replay
# ============================================================

def _find_cross_horizon_dates(df_r2: pd.DataFrame) -> list[str]:
    """从 Round 2 列找 weak-trend-improving / strong-trend-weakening 日期。"""
    candidates: list[str] = []
    # 方法1：明确列
    for col in df_r2.columns:
        if any(k in col.lower() for k in ["weak_trend", "strong_trend",
                                            "internal_momentum_improving",
                                            "internal_momentum_weakening"]):
            dates = df_r2.index[df_r2[col].fillna(False).astype(bool)].tolist()
            candidates.extend(str(d) for d in dates)
    # 方法2：推断 - trend 与 momentum/mom_change 背离
    if not candidates and "regime_up_ratio" in df_r2.columns:
        # simple heuristic 跳过
        pass
    return list(dict.fromkeys(candidates))


def cross_horizon_replay(df_r2: pd.DataFrame,
                         df_review: pd.DataFrame,
                         df_map: pd.DataFrame) -> dict[str, Any]:
    ch_dates = _find_cross_horizon_dates(df_r2)
    results: dict[str, Any] = {}
    for d in ch_dates:
        if d not in df_review.index:
            continue
        rvw_row = df_review.loc[d]
        # Q = 内部结构质量；U = 参与范围；P = 价格
        # 若 P 高但 Q 低（或反向）→ 跨周期压缩
        p_v = rvw_row.get("metric_P_value")
        q_v = rvw_row.get("metric_Q_value")
        u_v = rvw_row.get("metric_U_value")
        spread = None
        if p_v is not None and q_v is not None:
            spread = abs(float(p_v) - float(q_v))
        compressed = spread is not None and spread < 15  # P、Q 被压到接近
        results[str(d)] = {
            "P_value": None if pd.isna(p_v) else float(p_v),
            "Q_value": None if pd.isna(q_v) else float(q_v),
            "U_value": None if pd.isna(u_v) else float(u_v),
            "P_Q_abs_spread": spread,
            "directions_compressed_to_single_value": compressed,
            "note": (
                "若P与Q差距小，Review会把不同周期方向压成一个模糊结果"
                if compressed else "P/Q 仍保留一定差距"
            ),
        }
    if not results:
        results["_NOTE"] = (
            "Round 2 cross-horizon divergence dates 未找到列；"
            "请参考 Round 2 的 correlations JSON 和 archetype replay 标签推断"
        )
    return results


# ============================================================
# CLI
# ============================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="round3_archetype_analysis")
    p.add_argument("--out-root", type=Path,
                   default=Path(__file__).resolve().parents[1] / "out")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    out_root = args.out_root
    out_r3 = out_root / "round3"
    out_r3.mkdir(parents=True, exist_ok=True)

    df_r2 = pd.read_csv(out_root / "round2_db_native" /
                        "round2_daily_observation.csv").set_index("trade_date")
    df_review = pd.read_csv(out_r3 / "round3_current_review_daily.csv").set_index("trade_date")
    df_map = pd.read_csv(out_r3 / "round3_current_component_map.csv")

    common = df_r2.index.intersection(df_review.index)
    df_r2 = df_r2.loc[common]
    df_review = df_review.loc[common]

    comp = compression_audit(df_map, df_review)
    (out_r3 / "round3_family_compression_audit.json").write_text(
        json.dumps(comp, indent=2, ensure_ascii=False, default=str)
    )

    arche = archetype_replay(out_root, df_r2, df_review, df_map)
    (out_r3 / "round3_archetype_replay.json").write_text(
        json.dumps(arche, indent=2, ensure_ascii=False, default=str)
    )

    ch = cross_horizon_replay(df_r2, df_review, df_map)
    (out_r3 / "round3_cross_horizon_replay.json").write_text(
        json.dumps(ch, indent=2, ensure_ascii=False, default=str)
    )

    summary = {
        "compression_verdicts": {
            f: v["verdict"] for f, v in comp.items()
        },
        "archetype_dates": len(arche),
        "cross_horizon_dates_analyzed": len(ch),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
