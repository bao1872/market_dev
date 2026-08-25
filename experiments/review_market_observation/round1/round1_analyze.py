"""Round 1 Dataset Integrity Audit + Primitive Audit + Transition Audit。

本文件**纯函数 + 文件 IO**，不连接 DB。输入是 round1_extract 生成的 frozen_dataset：
- frozen_dataset.csv.gz / frozen_dataset.parquet
- dataset_manifest.json

输出（写入 results/ 目录）：
- coverage_summary.json
- missingness_summary.json
- field_inventory.json
- state_frequency.json          (categorical 字段值域频率)
- transition_frequency.json     (T-1 → T 状态迁移矩阵)
- primitive_summary.json        (连续值 percentile/分布)
- data_quality_findings.json    (integrity blocker / warning 列表)
- round_1_summary.md            (人类可读 markdown，由 template 生成)

所有审计逻辑都是纯函数，便于单元测试（synthetic fixture）。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .dataset_schema import (
    CATEGORICAL_STATE_FIELDS,
    FROZEN_COLUMNS,
    TARGET_TRADE_DATE_COUNT,
    build_field_inventory,
)


def _normalize_categorical_value(value: Any) -> str:
    """归一化分类状态值的字符串表达（处理 float→int 等价的情况）。

    背景：state_payload 中 regime_value/swing_bias 等是 int，但读入 pandas 后可能
    变成 float（列中有 None 时 upcast）；直接 str(1.0) = "1.0"，但我们期望 "1"。
    规则：
    - 对 float，如果 float.is_integer()，则格式化为 str(int(x))
    - 否则 str(x)
    - None → "__NULL__"（由 caller 负责 fillna）
    """
    if value is None:
        return "__NULL__"
    if isinstance(value, float):
        if value != value:  # NaN
            return "__NULL__"
        if value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, bool):
        return str(value)
    return str(value)


# ============================================================================
# 加载 frozen dataset（自动选择 parquet/csv.gz，便于测试注入 pandas df）
# ============================================================================

def load_frozen_dataset(data_dir: Path) -> Any:
    """返回 pd.DataFrame。优先 parquet，次之 csv.gz。"""
    import pandas as pd
    parquet_path = data_dir / "frozen_dataset.parquet"
    if parquet_path.exists():
        return pd.read_parquet(parquet_path)
    csv_path = data_dir / "frozen_dataset.csv.gz"
    if csv_path.exists():
        return pd.read_csv(csv_path, compression="gzip")
    raise FileNotFoundError(
        f"No frozen dataset in {data_dir} (expect parquet or csv.gz)"
    )


def load_manifest(data_dir: Path) -> dict[str, Any]:
    return json.loads((data_dir / "dataset_manifest.json").read_text(encoding="utf-8"))


# ============================================================================
# A. Dataset Integrity Audit
# ============================================================================

@dataclass
class IntegrityFinding:
    severity: str            # "blocker" | "warning" | "info"
    check: str               # 稳定检查名
    evidence: dict[str, Any]  # 机器可读证据
    message: str             # 人类可读解释
    status: str = "UNVERIFIED"  # PASS / PARTIAL / INVALID / UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_trade_dates(df: Any) -> dict[str, Any]:
    """distinct dates / count / min-max / 120-proof / order。"""
    dates = sorted(df["trade_date"].dropna().unique().tolist())
    return {
        "distinct_count": len(dates),
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates else None,
        "is_exact_120": len(dates) == TARGET_TRADE_DATE_COUNT,
        "sorted_asc": dates == sorted(dates),
        # 相邻日期间隔的基本分布（不能有 nan）
    }


def check_rows(df: Any) -> dict[str, Any]:
    """total rows / distinct instruments / duplicates / per-day count 分布。"""
    import pandas as pd
    total_rows = len(df)
    distinct_instruments = df["instrument_id"].nunique()
    dup = df.duplicated(subset=["instrument_id", "trade_date"], keep=False)
    dup_rows = int(dup.sum())
    dup_keys = None
    if dup_rows > 0:
        dup_sample = df.loc[dup, ["instrument_id", "trade_date"]
                           ].drop_duplicates().head(10).to_dict("records")
        dup_keys = dup_sample

    daily_count = df.groupby("trade_date")["instrument_id"].nunique()
    return {
        "total_rows": total_rows,
        "distinct_instruments": distinct_instruments,
        "duplicate_pairs_count": dup_rows,
        "duplicate_keys_sample": dup_keys,
        "daily_instrument_count": {
            "min": int(daily_count.min()) if len(daily_count) else None,
            "max": int(daily_count.max()) if len(daily_count) else None,
            "mean": float(daily_count.mean()) if len(daily_count) else None,
            "median": float(daily_count.median()) if len(daily_count) else None,
        },
    }


def check_lineage(df: Any) -> dict[str, Any]:
    """algorithm_version / history_contract_version / source_history_run_id 分布。"""
    cols = ["algorithm_version", "hc_outer", "hc_payload", "source_history_run_id"]
    out: dict[str, Any] = {}
    for col in cols:
        if col not in df.columns:
            out[col] = {"__MISSING_COLUMN__": True}
            continue
        vc = df[col].fillna("__NULL__").value_counts().head(20)
        out[col] = {str(k): int(v) for k, v in vc.items()}
    # hc 一致性：同一行 hc_outer == hc_payload
    if "hc_outer" in df.columns and "hc_payload" in df.columns:
        mask_mismatch = df["hc_outer"].fillna("__X__") != df["hc_payload"].fillna("__X__")
        out["hc_mismatch_rows"] = int(mask_mismatch.sum())
        sample = df.loc[mask_mismatch, ["instrument_id", "trade_date", "hc_outer", "hc_payload"]
                        ].head(10).to_dict("records")
        out["hc_mismatch_sample"] = sample
    return out


def check_readiness(df: Any) -> dict[str, Any]:
    """history_sufficient / core_factor_ready / valid_for_market_aggregation / invalid_reason 分布。"""
    cols = [
        "history_sufficient", "core_factor_ready", "valid_for_market_aggregation",
        "invalid_reason",
    ]
    out: dict[str, Any] = {}
    for col in cols:
        if col not in df.columns:
            out[col] = {"__MISSING_COLUMN__": True}
            continue
        if col == "invalid_reason":
            vc = df[col].fillna("__VALID__").value_counts().head(20)
            out[col] = {str(k): int(v) for k, v in vc.items()}
        else:
            vc = df[col].fillna("__NULL__").value_counts()
            out[col] = {str(k): int(v) for k, v in vc.items()}

    # 每日 denominator: ready_count = valid_for_market_aggregation == True 的 instrument 数
    if "valid_for_market_aggregation" in df.columns:
        vma = df.groupby("trade_date")["valid_for_market_aggregation"].apply(
            lambda s: int((s == True).sum())  # noqa: E712
        )
        out["daily_ready_count"] = {
            "min": int(vma.min()) if len(vma) else None,
            "max": int(vma.max()) if len(vma) else None,
            "mean": float(vma.mean()) if len(vma) else None,
            "median": float(vma.median()) if len(vma) else None,
            "range_over_max_ratio": (
                float((vma.max() - vma.min()) / vma.max())
                if len(vma) and vma.max() > 0 else None
            ),
        }
    return out


def check_missingness(df: Any) -> dict[str, Any]:
    """每个核心字段的 missing rate（按列，null / NaN / '' 均视为 missing）。"""
    import pandas as pd
    result: dict[str, dict[str, Any]] = {}
    for col in FROZEN_COLUMNS:
        if col not in df.columns:
            result[col] = {"missing_rows": len(df), "missing_rate": 1.0, "note": "COLUMN_NOT_IN_DF"}
            continue
        series = df[col]
        if series.dtype == object:
            missing = series.isna() | (series.astype(str).str.strip() == "")
        else:
            missing = series.isna()
        n_miss = int(missing.sum())
        rate = n_miss / len(df) if len(df) else 1.0
        result[col] = {
            "present_rows": len(df) - n_miss,
            "missing_rows": n_miss,
            "missing_rate": round(rate, 6),
        }
    return result


def collect_integrity_findings(
    dates_info: dict[str, Any],
    rows_info: dict[str, Any],
    lineage_info: dict[str, Any],
    readiness_info: dict[str, Any],
    manifest: dict[str, Any],
) -> list[IntegrityFinding]:
    """把所有检查结果转换为可审计的 Finding 列表（含 blocker/warning 分类）。"""
    findings: list[IntegrityFinding] = []

    # F1: 120 dates proof
    if dates_info["is_exact_120"]:
        findings.append(IntegrityFinding(
            severity="info", check="TRADE_DATE_COUNT_120",
            evidence=dates_info,
            message=f"交易日严格 {TARGET_TRADE_DATE_COUNT} 个，范围 {dates_info['start']} ~ {dates_info['end']}",
            status="PASS",
        ))
    else:
        findings.append(IntegrityFinding(
            severity="blocker" if dates_info["distinct_count"] < 100 else "warning",
            check="TRADE_DATE_COUNT_NOT_120",
            evidence=dates_info,
            message=(
                f"交易日数 ≠ {TARGET_TRADE_DATE_COUNT}（实际={dates_info['distinct_count']}）；"
                "若历史不足则 Round 1 可能 INVALID"
            ),
            status="PARTIAL" if dates_info["distinct_count"] >= 100 else "INVALID",
        ))

    # F2: duplicates
    if rows_info["duplicate_pairs_count"] == 0:
        findings.append(IntegrityFinding(
            severity="info", check="NO_DUPLICATE_INSTR_DATE",
            evidence=rows_info,
            message="无 (instrument_id, trade_date) 重复对",
            status="PASS",
        ))
    else:
        findings.append(IntegrityFinding(
            severity="blocker",
            check="DUPLICATE_INSTR_DATE_FOUND",
            evidence=rows_info,
            message=f"存在 {rows_info['duplicate_pairs_count']} 行重复（UniqueConstraint 应已阻止）",
            status="INVALID",
        ))

    # F3: lineage — hc_outer / hc_payload 一致性
    hc_mm = lineage_info.get("hc_mismatch_rows", 0)
    if hc_mm == 0:
        findings.append(IntegrityFinding(
            severity="info", check="HC_OUTER_PAYLOAD_CONSISTENT",
            evidence={"hc_mismatch_rows": 0},
            message="DB 外层 hc_outer 与 payload 内 hc_payload 逐行一致",
            status="PASS",
        ))
    else:
        findings.append(IntegrityFinding(
            severity="blocker", check="HC_OUTER_PAYLOAD_MISMATCH",
            evidence=lineage_info,
            message=f"有 {hc_mm} 行 hc_outer ≠ hc_payload，lineage 不一致",
            status="INVALID",
        ))

    # F4: algorithm_version 不混杂（只允许 1 个主要版本，混杂需警告）
    av = lineage_info.get("algorithm_version", {})
    if len([k for k in av if k != "__NULL__"]) == 1:
        findings.append(IntegrityFinding(
            severity="info", check="SINGLE_ALGORITHM_VERSION",
            evidence=av, message=f"algorithm_version 唯一: {list(av.keys())}",
            status="PASS",
        ))
    else:
        findings.append(IntegrityFinding(
            severity="warning", check="ALGORITHM_VERSION_MIXED",
            evidence=av,
            message=f"发现 {len(av)} 种 algorithm_version（若无法解释则阻塞）",
            status="PARTIAL",
        ))

    # F5: denominator 稳定性（daily ready_count 的相对范围）
    drc = readiness_info.get("daily_ready_count", {})
    ratio = drc.get("range_over_max_ratio")
    if ratio is None:
        pass
    elif ratio <= 0.15:
        findings.append(IntegrityFinding(
            severity="info", check="DAILY_DENOMINATOR_STABLE",
            evidence=drc,
            message=f"每日 denominator 相对范围 {ratio:.3f}（≤0.15，稳定）",
            status="PASS",
        ))
    else:
        findings.append(IntegrityFinding(
            severity="warning", check="DAILY_DENOMINATOR_VOLATILE",
            evidence=drc,
            message=f"每日 denominator 相对范围 {ratio:.3f}（>0.15，需检查原因）",
            status="PARTIAL",
        ))

    return findings


# ============================================================================
# B. Primitive Audit（基础画像：值域、频率、缺失、分布）
# ============================================================================

def analyze_categorical_states(df: Any) -> dict[str, Any]:
    """分类状态字段：值域 + 总频率 + 每日横截面比例。"""
    import pandas as pd
    result: dict[str, Any] = {}
    for col in CATEGORICAL_STATE_FIELDS:
        if col not in df.columns:
            result[col] = {"error": "COLUMN_NOT_PRESENT"}
            continue
        series = df[col]
        # 全局频率（含 null 作为 __NULL__ 类别；值字符串统一归一为 int/float/str）
        fill_val = "__NULL__"
        normed = series.apply(lambda x: fill_val if (pd.isna(x) or str(x).strip() == "") else _normalize_categorical_value(x))
        freq = {str(k): int(v) for k, v in normed.value_counts().items()}

        # 每日横截面：各类别占比（只按 date 聚合，valid denominator = 非空）
        valid = df.loc[series.notna() & (series.astype(str) != "")]
        if len(valid) == 0:
            daily_cross = {}
        else:
            cross = (
                valid.groupby(["trade_date", col]).size()
                .unstack(fill_value=0)
            )
            cross_pct = cross.div(cross.sum(axis=1), axis=0).round(4)
            daily_summary = {}
            for cat in cross_pct.columns:
                s = cross_pct[cat]
                daily_summary[str(cat)] = {
                    "mean_ratio": round(float(s.mean()), 4),
                    "min_ratio": round(float(s.min()), 4),
                    "max_ratio": round(float(s.max()), 4),
                    "median_ratio": round(float(s.median()), 4),
                    "std": round(float(s.std(ddof=0)), 4),
                }
            daily_cross = daily_summary

        result[col] = {
            "global_frequency": freq,
            "n_categories": len(freq),
            "daily_cross_section_summary": daily_cross,
        }
    return result


def analyze_continuous_distribution(df: Any) -> dict[str, Any]:
    """连续数值字段：percentile / 基本分布。对每个维度关键连续指标做统计。"""
    import pandas as pd
    key_continuous = [
        # trend
        "regime_strength", "dsa_dir_bars", "dsa_vwap_dev_pct",
        "segment_change_pct", "segment_slope", "segment_bars",
        # momentum
        "sqzmom_val", "sqzmom_delta",
        # volume / price
        "volume_ratio_20", "volume_percentile_20",
        "review_volume_ratio20", "review_amount_ratio20",
        "review_volume_percentile20", "review_amount_percentile200",
        "price_position_120d",
        "fp_segment_volume_ratio",  # 这个在 flat 里？不，这个是 previous_state_to_flat 的 key，不存 frozen dataset
    ]
    # 修正：segment 量能字段名用 dataset_schema 中真实列
    key_continuous = [c for c in key_continuous if c != "fp_segment_volume_ratio"]
    for alt in ("current_vs_prev_volume_mean_ratio", "current_vs_prev_amount_mean_ratio"):
        if alt in df.columns:
            key_continuous.append(alt)

    percentiles = [0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99]
    result: dict[str, Any] = {}
    for col in key_continuous:
        if col not in df.columns:
            result[col] = {"error": "COLUMN_NOT_PRESENT"}
            continue
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if series.empty:
            result[col] = {"all_missing": True}
            continue
        q = series.quantile(percentiles)
        result[col] = {
            "count_present": int(series.shape[0]),
            "count_missing": int(df[col].isna().sum() + pd.to_numeric(df[col], errors="coerce").isna().sum() - df[col].isna().sum()),
            "missing_rate": round(float(1.0 - series.shape[0] / len(df)), 6),
            "min": round(float(series.min()), 6),
            "max": round(float(series.max()), 6),
            "mean": round(float(series.mean()), 6),
            "std": round(float(series.std(ddof=0)), 6),
            "median": round(float(series.median()), 6),
            "percentiles": {f"p{int(p*100):02d}": round(float(q[p]), 6) for p in percentiles},
        }
    return result


# ============================================================================
# C. Transition Audit（纯描述 T-1 state → T state，不建模）
# ============================================================================

def compute_transition_audit(df: Any, field: str) -> dict[str, Any]:
    """对一个分类字段计算真实交易日 T-1 → T 迁移矩阵。

    注意：T-1 = 前一**交易日**，不是自然日。利用 df 中 trade_date 的排序来定位
    前一日（因为 frozen dataset 只包含 120 个连续交易日，所以同一股票相邻 trade_date
    就是真实 T-1）。
    """
    import pandas as pd
    if field not in df.columns:
        return {"error": "COLUMN_NOT_PRESENT"}
    sub = df[["instrument_id", "trade_date", field]].copy()
    sub = sub.sort_values(["instrument_id", "trade_date"]).reset_index(drop=True)
    # 前一交易日状态（按 instrument 分组 shift(1)）
    sub[f"prev_{field}"] = sub.groupby("instrument_id")[field].shift(1)
    # 过滤掉没有前一交易日的行（每个 instrument 最早一天）
    has_prev = sub.dropna(subset=[f"prev_{field}"], how="all")
    # 对于该字段的迁移：prev & curr 都非空才计入迁移
    curr_norm = sub[field].apply(lambda x: None if (pd.isna(x) or str(x).strip() == "") else _normalize_categorical_value(x))
    prev_norm = sub[f"prev_{field}"].apply(lambda x: None if (pd.isna(x) or str(x).strip() == "") else _normalize_categorical_value(x))
    mask_valid = curr_norm.notna() & prev_norm.notna()
    # 用归一后的 Series 替换原值构建迁移
    valid_curr = curr_norm[mask_valid]
    valid_prev = prev_norm[mask_valid]
    valid = pd.DataFrame({
        "trade_date": sub.loc[mask_valid, "trade_date"],
        f"prev_{field}": valid_prev.values,
        field: valid_curr.values,
    })

    # 1. 迁移矩阵（全局频率 + 比例）
    if valid.empty:
        return {"no_transitions": True, "valid_pairs": 0}

    transition_key = valid[[f"prev_{field}", field]].astype(str)
    counts = transition_key.value_counts()
    matrix_count: dict[str, dict[str, int]] = defaultdict(dict)
    for (p, c), n in counts.items():
        matrix_count[p][c] = int(n)

    # row-wise ratio = 对每个 prev_state，curr 概率分布
    matrix_ratio: dict[str, dict[str, float]] = {}
    for p, curr_dict in matrix_count.items():
        total = sum(curr_dict.values())
        matrix_ratio[p] = {c: round(v / total, 4) for c, v in curr_dict.items()}

    # 2. 总体 transition rate（每日多少比例的股票发生了状态改变）
    changed_mask = valid[field].astype(str) != valid[f"prev_{field}"].astype(str)
    changed_per_date = (
        valid.assign(_changed=changed_mask.values)
        .groupby("trade_date")["_changed"].mean()
    )
    transition_rate_summary = {
        "mean": round(float(changed_per_date.mean()), 4) if len(changed_per_date) else None,
        "min": round(float(changed_per_date.min()), 4) if len(changed_per_date) else None,
        "max": round(float(changed_per_date.max()), 4) if len(changed_per_date) else None,
        "median": round(float(changed_per_date.median()), 4) if len(changed_per_date) else None,
    }

    # 3. 120 日最常见 transition（top 15）
    total_valid = len(valid)
    top_counts = counts.head(15)
    top_list = [
        {"prev": str(p), "curr": str(c), "count": int(n),
         "ratio_among_valid": round(n / total_valid, 4)}
        for (p, c), n in top_counts.items()
    ]

    return {
        "valid_pairs_count": total_valid,
        "overall_change_ratio": round(float(changed_mask.mean()), 4),
        "daily_transition_rate_summary": transition_rate_summary,
        "transition_matrix_count": dict(matrix_count),
        "transition_matrix_ratio": matrix_ratio,
        "top_15_transitions": top_list,
    }


def run_all_transition_audits(df: Any) -> dict[str, Any]:
    """对所有分类状态字段执行 transition audit。"""
    out: dict[str, Any] = {}
    for field in [
        "regime_value",           # -1/0/1 迁移（核心：趋势状态变化）
        "swing_bias",             # 主要结构方向变化
        "internal_bias",          # 短线结构方向变化
        "structure_alignment",    # 共振 ↔ 背离
        "volatility_phase",       # squeeze → released → normal
        "momentum_direction",     # expanding ↔ contracting
    ]:
        out[field] = compute_transition_audit(df, field)
    return out


# ============================================================================
# D. 综合：最终 Round 1 Verdict（按 prompt §11.5 Gate）
# ============================================================================

def derive_round1_verdict(findings: list[IntegrityFinding]) -> tuple[str, list[str]]:
    """从 findings 推导 PASS / PARTIAL / INVALID。"""
    blockers = [f for f in findings if f.severity == "blocker"]
    warnings = [f for f in findings if f.severity == "warning"]
    reasons: list[str] = []
    if blockers:
        for b in blockers:
            reasons.append(f"[BLOCKER] {b.check}: {b.message}")
        return "INVALID", reasons
    if warnings:
        for w in warnings:
            reasons.append(f"[WARNING] {w.check}: {w.message}")
        return "PARTIAL", reasons
    return "PASS", [f"[{f.status}] {f.check}: {f.message}" for f in findings]


# ============================================================================
# E. CLI / main
# ============================================================================

def run_all_audits(data_dir: Path, results_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """执行全部 Round 1 审计，写 JSON 结果到 results_dir。

    返回 summary dict（供 Markdown 模板消费）。
    """
    import pandas as pd
    results_dir.mkdir(parents=True, exist_ok=True)

    df = load_frozen_dataset(data_dir)

    # --- Integrity ---
    dates_info = check_trade_dates(df)
    rows_info = check_rows(df)
    lineage_info = check_lineage(df)
    readiness_info = check_readiness(df)
    missingness = check_missingness(df)
    findings = collect_integrity_findings(
        dates_info, rows_info, lineage_info, readiness_info, manifest,
    )

    # --- Primitive ---
    categorical = analyze_categorical_states(df)
    continuous = analyze_continuous_distribution(df)

    # --- Transition ---
    transitions = run_all_transition_audits(df)

    # --- Verdict ---
    verdict, verdict_reasons = derive_round1_verdict(findings)

    # --- 写出 ---
    def _write(name: str, obj: Any) -> None:
        (results_dir / name).write_text(
            json.dumps(obj, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    # field inventory（字段清单）
    field_inv = [
        {
            "name": f.name, "group": f.group, "expected_type": f.expected_type,
            "from_payload": f.from_payload, "nullable": f.nullable,
            "description": f.description,
            "missing_rate_overall": missingness.get(f.name, {}).get("missing_rate"),
        }
        for f in build_field_inventory()
    ]
    _write("field_inventory.json", field_inv)
    _write("coverage_summary.json", {
        "trade_dates": dates_info,
        "rows": rows_info,
        "readiness": readiness_info,
    })
    _write("missingness_summary.json", missingness)
    _write("state_frequency.json", categorical)
    _write("transition_frequency.json", transitions)
    _write("primitive_summary.json", {"categorical": categorical, "continuous": continuous})
    _write(
        "data_quality_findings.json",
        {
            "verdict": verdict,
            "verdict_reasons": verdict_reasons,
            "findings": [f.to_dict() for f in findings],
        },
    )

    return {
        "verdict": verdict,
        "verdict_reasons": verdict_reasons,
        "n_findings": len(findings),
        "n_blockers": sum(1 for f in findings if f.severity == "blocker"),
        "n_warnings": sum(1 for f in findings if f.severity == "warning"),
        "dates_info": dates_info,
        "rows_info": rows_info,
        "lineage_info": lineage_info,
        "readiness_info": readiness_info,
        "categorical": categorical,
        "continuous": continuous,
        "transitions": transitions,
    }


# ============================================================================
# §9 / §16：脱敏证据写出（用于 Git 入库）
# ============================================================================

def _slim_continuous(c: dict) -> dict:
    """把 full continuous stats 压缩为 p5/p50/p95 + min/max + count，适合 manifest。"""
    slim: dict = {}
    for k, v in c.items():
        if not isinstance(v, dict):
            continue
        pct = v.get("percentiles") or {}
        slim[k] = {
            "count": v.get("count"),
            "p01": pct.get("0.01"), "p05": pct.get("0.05"),
            "p50": pct.get("0.5"),
            "p95": pct.get("0.95"), "p99": pct.get("0.99"),
            "min": v.get("min"), "max": v.get("max"),
            "mean": v.get("mean"), "std": v.get("std"),
        }
    return slim


def _slim_categorical_top(categorical: dict, top_n: int = 3) -> dict:
    """仅保留每个 categorical 字段的全局 top_n 类别及其比例。"""
    out: dict = {}
    for field, meta in categorical.items():
        freq = meta.get("global_frequency") or {}
        total = sum(freq.values())
        items = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
        out[field] = {
            "n_categories": meta.get("n_categories"),
            "top_classes": [
                {"class": str(k), "count": int(v),
                 "ratio": round(v / total, 4) if total else None}
                for k, v in items
            ],
        }
    return out


def _slim_transitions_top(transitions: dict, top_n: int = 5) -> dict:
    out: dict = {}
    for field, meta in transitions.items():
        if not isinstance(meta, dict):
            continue
        out[field] = {
            "valid_pairs_count": meta.get("valid_pairs_count"),
            "overall_change_ratio": meta.get("overall_change_ratio"),
            "top_transitions": (meta.get("top_15_transitions") or [])[:top_n],
        }
    return out


def write_public_summary(
    audit_dir: Path,
    manifest_private: dict,
    audit_result: dict,
    *,
    public_manifest_path: Path,
    round1_summary_md_path: Path,
    unexpected_findings: list[str] | None = None,
) -> None:
    """§9 写脱敏 public manifest + §16 写 ROUND1_SUMMARY.md。

    不包含任何 DB 秘密；只写 SHA、行列数、日期范围、版本、覆盖率、
    压缩后的原语/过渡统计与 verdict。
    """
    from .round1_extract import build_public_manifest  # 本地导入避免 top-level 循环
    import pandas as pd  # noqa: F401  (already top-level? not necessarily)

    # 1) public manifest（脱敏小文件）
    extra_public = {
        "ROUND1_VERDICT": audit_result.get("verdict"),
        "N_FINDINGS_BLOCKER": audit_result.get("n_blockers"),
        "N_FINDINGS_WARNING": audit_result.get("n_warnings"),
        "N_FINDINGS_INFO": (
            (audit_result.get("n_findings") or 0)
            - (audit_result.get("n_blockers") or 0)
            - (audit_result.get("n_warnings") or 0)
        ),
        "categorical_top3": _slim_categorical_top(audit_result.get("categorical") or {}),
        "continuous_percentiles": _slim_continuous(audit_result.get("continuous") or {}),
        "transition_top5": _slim_transitions_top(audit_result.get("transitions") or {}),
        "daily_row_count_range": {
            "min": (audit_result.get("rows_info") or {}).get("min_per_date"),
            "max": (audit_result.get("rows_info") or {}).get("max_per_date"),
            "mean": (audit_result.get("rows_info") or {}).get("mean_per_date"),
        },
        "daily_ready_count_range": {
            "min": (audit_result.get("readiness_info") or {}).get("daily_ready_count", {}).get("min"),
            "max": (audit_result.get("readiness_info") or {}).get("daily_ready_count", {}).get("max"),
            "mean": (audit_result.get("readiness_info") or {}).get("daily_ready_count", {}).get("mean"),
        },
        "invalid_reason_top5": (audit_result.get("readiness_info") or {}).get("invalid_reason"),
        "unexpected_findings": unexpected_findings or [],
    }
    public = build_public_manifest(manifest_private, extra_public)
    public_manifest_path.write_text(
        json.dumps(public, indent=2, sort_keys=True, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    # 2) ROUND1_SUMMARY.md（§16 要求的分节格式）
    v = audit_result.get("verdict") or "UNKNOWN"
    reasons = audit_result.get("verdict_reasons") or []
    dates_info = audit_result.get("dates_info") or {}
    rows_info = audit_result.get("rows_info") or {}
    lineage_info = audit_result.get("lineage_info") or {}
    readiness = audit_result.get("readiness_info") or {}
    categorical = audit_result.get("categorical") or {}
    continuous = audit_result.get("continuous") or {}
    transitions = audit_result.get("transitions") or {}
    blockers = audit_result.get("n_blockers") or 0
    warnings = audit_result.get("n_warnings") or 0

    def pct_line(field: str) -> str:
        p = (continuous.get(field) or {}).get("percentiles") or {}
        return (
            f"| {field} "
            f"| {p.get('0.05')} | {p.get('0.5')} | {p.get('0.95')} |"
        )

    def top_cat_line(field: str) -> str:
        freq = (categorical.get(field) or {}).get("global_frequency") or {}
        if not freq:
            return f"| {field} | — | — |"
        total = sum(freq.values())
        k, n = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[0]
        return f"| {field} | {k} | {round(n/total, 4) if total else None} ({n}/{total}) |"

    def top_trans_line(field: str) -> str:
        top5 = (transitions.get(field) or {}).get("top_15_transitions") or []
        parts = []
        for t in top5[:3]:
            parts.append(f"{t['prev']}→{t['curr']} n={t['count']} ({t['ratio_among_valid']})")
        body = "；".join(parts) if parts else "—"
        return f"| {field} | {body} |"

    md = f"""# Round 1 Summary — Raw Data & Primitive Audit

> 本文件由 `round1_analyze.write_public_summary()` 真实执行后生成；
> 不包含任何数据库凭据，仅保留小型可复核的聚合证据。

## 1. Baseline

```text
DEV_BASE_SHA = {manifest_private.get("DEV_BASE_SHA")}
EXP_SHA      = {manifest_private.get("EXP_SHA")}
```

## 2. Frozen Dataset

```text
DATASET_ID                   = {manifest_private.get("DATASET_ID")}
TRADE_DATE_START             = {manifest_private.get("TRADE_DATE_START")}
TRADE_DATE_END               = {manifest_private.get("TRADE_DATE_END")}
TRADE_DATE_COUNT             = {manifest_private.get("TRADE_DATE_COUNT")}
TRADE_DATE_IS_EXACT_TARGET   = {manifest_private.get("TRADE_DATE_IS_EXACT_TARGET")}
ROW_COUNT                    = {manifest_private.get("ROW_COUNT")}
INSTRUMENT_COUNT             = {manifest_private.get("INSTRUMENT_COUNT")}
ALGORITHM_VERSION            = {manifest_private.get("ALGORITHM_VERSION")}
HISTORY_CONTRACT_VERSION     = {manifest_private.get("HISTORY_CONTRACT_VERSION")}
```

## 3. Integrity

| 维度 | 结果 |
|---|---|
| row_count | {rows_info.get("row_count")} （daily min={rows_info.get("min_per_date")} max={rows_info.get("max_per_date")} mean={rows_info.get("mean_per_date")}） |
| (instrument_id, trade_date) 主键唯一 | duplicate_pairs_count = {rows_info.get("duplicate_pairs_count")} |
| 交易日期数 | {dates_info.get("count")} (sorted_asc={dates_info.get("sorted_asc")} is_exact_target={dates_info.get("is_exact_target")}) |
| core_factor_ready 覆盖率 | {readiness.get("summary_core_factor_ready", {}).get("ratio")} ({readiness.get("summary_core_factor_ready", {}).get("count")}/{readiness.get("summary_core_factor_ready", {}).get("total")}) |
| valid_for_market_aggregation 覆盖率 | {readiness.get("summary_valid_for_market_aggregation", {}).get("ratio")} ({readiness.get("summary_valid_for_market_aggregation", {}).get("count")}/{readiness.get("summary_valid_for_market_aggregation", {}).get("total")}) |
| hc_outer == hc_payload | {lineage_info.get("hc_match_ratio")} |
| algorithm_version x hc_outer distinct combos | {lineage_info.get("outer_distinct_pairs")} |
| invalid_reason Top-5 | {(readiness.get("invalid_reason") or {})} |
| findings 数量 | blocker={blockers} / warning={warnings} |

## 4. Primitive findings

### 4.1 分类状态 Top 1（全局频率）

| 原语 | Top 1 | 占比（比例/计数） |
|---|---|---|
{chr(10).join(top_cat_line(f) for f in ["regime_value","swing_bias","internal_bias","structure_alignment","volatility_phase","momentum_direction"])}

### 4.2 连续原语（p5 / p50 / p95）

| 原语 | p5 | p50 | p95 |
|---|---|---|---|
{chr(10).join(pct_line(f) for f in ["regime_strength","dsa_dir_bars","dsa_vwap_dev_pct","sqzmom_val","sqzmom_delta","review_volume_ratio20","review_amount_ratio20","price_position_120d"])}

## 5. Transition findings（Top 3）

| 原语 | Top Transitions（prev→curr, n, ratio ） |
|---|---|
{chr(10).join(top_trans_line(f) for f in ["regime_value","swing_bias","internal_bias","structure_alignment","volatility_phase","momentum_direction"])}

## 6. Unexpected Findings

{(unexpected_findings or ["(none explicitly flagged)"])}

## 7. Verdict

```text
ROUND 1 VERDICT = {v}
```

Findings 摘要：

{chr(10).join("- " + r for r in reasons[:30]) or "- (no reasons reported)"}

---

_生成于 {manifest_private.get("EXTRACTED_AT")}；SCHEMA_HASH={manifest_private.get("SCHEMA_HASH")}；DATA_HASH={manifest_private.get("DATA_HASH")[:16]}…_
"""
    round1_summary_md_path.write_text(md, encoding="utf-8")


# ============================================================================
# CLI（§2.1 用 python -m 调用）
# ============================================================================

def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Round 1 integrity + primitive + transition audit")
    ap.add_argument("--data-dir", required=True, type=Path, help="Frozen dataset 所在目录（含 parquet+manifest）")
    ap.add_argument("--audit-dir", required=True, type=Path, help="结果 JSON/MD 输出目录")
    ap.add_argument("--write-public", action="store_true",
                    help="同时生成 dataset_manifest_public.json + ROUND1_SUMMARY.md（§9/§16，默认写入 audit-dir 父目录下的 public/）")
    ap.add_argument("--public-dir", type=Path, default=None,
                    help="若 --write-public，public 输出位置；默认 <exp-root>/review_market_observation/")
    ap.add_argument("--unexpected-finding", action="append", default=[],
                    help="可重复：追加人类观察到的 unexpected finding（会写入 public manifest）")
    args = ap.parse_args(argv)

    args.audit_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(args.data_dir)
    result = run_all_audits(args.data_dir, args.audit_dir, manifest)

    # §16：也把 round1_summary.json 落到 audit-dir（便于 shell 读取）
    summary_for_shell = {
        "round1_verdict": result.get("verdict"),
        "round1_reasons": result.get("verdict_reasons"),
        "manifest_file": str((args.data_dir / "extracted_manifest.json").resolve()),
        "frozen_dataset_file": str(next(
            (args.data_dir / f for f in ("frozen_dataset.parquet", "frozen_dataset.csv.gz")
             if (args.data_dir / f).exists()),
            args.data_dir / "frozen_dataset.parquet",
        ).resolve()),
        "coverage_summary_file": str((args.audit_dir / "coverage_summary.json").resolve()),
        "state_frequency_file": str((args.audit_dir / "state_frequency.json").resolve()),
        "continuous_stats_file": str((args.audit_dir / "primitive_summary.json").resolve()),
        "transition_audit_file": str((args.audit_dir / "transition_frequency.json").resolve()),
        "integrity_findings_file": str((args.audit_dir / "data_quality_findings.json").resolve()),
        "n_blockers": result.get("n_blockers"),
        "n_warnings": result.get("n_warnings"),
        "n_info": (result.get("n_findings") or 0)
                  - (result.get("n_blockers") or 0)
                  - (result.get("n_warnings") or 0),
    }
    (args.audit_dir / "round1_summary.json").write_text(
        json.dumps(summary_for_shell, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    # §9/§16 脱敏公开证据
    if args.write_public:
        public_dir = args.public_dir
        if public_dir is None:
            # 默认写 review_market_observation 根目录（可 git add）
            public_dir = Path(__file__).resolve().parents[1]
        public_dir.mkdir(parents=True, exist_ok=True)
        write_public_summary(
            audit_dir=args.audit_dir,
            manifest_private=manifest,
            audit_result=result,
            public_manifest_path=public_dir / "dataset_manifest_public.json",
            round1_summary_md_path=public_dir / "ROUND1_SUMMARY.md",
            unexpected_findings=args.unexpected_finding or None,
        )

    print(f"Round 1 Verdict: {result['verdict']}")
    for r in result["verdict_reasons"][:15]:
        print("  - " + r[:240])
    print(
        f"Blockers: {result['n_blockers']}  "
        f"Warnings: {result['n_warnings']}  "
        f"Info: {result['n_findings'] - result['n_blockers'] - result['n_warnings']}"
    )
    return 0 if result["verdict"] != "INVALID" else 1


def main(argv: list[str] | None = None) -> int:
    return _cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
