#!/usr/bin/env python3
"""Phase 3.4B-FINAL — Review-Core Compute Optimization Closure。

从 3.4B-6B（input_hash 最后一刀）已推送 SHA 开始，使用与 3.4A-3 / 3.4B-4
完全同口径 harness（audit_closure._perf_run_instrument / _perf_report）重新执行
真实 compute_review_core_for_trade_date，输出最终验收数字。

冻结基准不变：
- dataset: backend/.perfdata/review/review-source-c5c686e-v1
- sample: output/3.4A-0/sample_manifest.jsonl（100 main_ge250 + 5 boundary_60_249）
- 测量: warmup=1, reps=3 → n_runs = 105 * 3 = 315
- elapsed wall time（time.perf_counter），exclusive 组件

输出：
- final cost decomposition + delta vs 3.4A-3 baseline
- Total p50/p95 / saved ms / saved %
- eligible instruments = 5286（从 frozen parquet 实际统计）
- final serial compute-only projection
- Phase 3 关闭掉的浪费清单 + 留下的合法计算成本清单（静态说明）

约束：
- PRODUCTION_CODE_DIFF = ZERO（本脚本只读消费 frozen dataset）
- 不连远程 DB；不做 PG migration / SQL 实验
- 不再优化留下的合法成本（full-history DSA#2 / DSA#1 / _build_structure_dimension /
  VolumeContext / SQZMOM / SMC#1）

Usage:
    cd backend && .venv/bin/python ../experiments/duplicate_compute_audit/final_34bfinal.py
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from audit_closure import _load_bars, _perf_report, _perf_run_instrument

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = EXPERIMENT_DIR / "output" / "3.4A-0" / "sample_manifest.jsonl"
BASELINE_PATH = EXPERIMENT_DIR / "output" / "3.4A-3" / "cost_decomposition.json"
PARQUET_DIR = REPO_ROOT / "backend" / ".perfdata" / "review" / "review-source-c5c686e-v1" / "parquet"
OUTPUT_DIR = EXPERIMENT_DIR / "output" / "3.4B-FINAL"

ELIGIBLE_MIN_BARS = 60

ROW_NAMES = [
    "DSA",
    "SMC #1",
    "SMC #2 (freshness)",
    "Bollinger #1",
    "Bollinger #2 (extra)",
    "SQZMOM",
    "VolumeContext",
    "ATR / swing / participation",
    "structural derived (dsa_segment / momentum)",
    "single-period VP (cost_position)",
    "MACD / daily_context / derived",
    "Canonical hash / orchestration overhead",
    "Artifact / summary assembly",
    "other / unmeasured",
]

# ---- Phase 3 关闭掉的浪费（历史 commits 已落地，此处为最终清单）----
CLOSED_WASTE = [
    {
        "item": "MDAS N+1 / batch read",
        "phase": "3.4B-0 之前",
        "note": "行情/因子读取 N+1 改 batch；reduce DB round-trips",
    },
    {
        "item": "DSA internal duplicate pipeline",
        "phase": "3.4B-1",
        "note": "compute_dsa_bundle 重复 dynamic_swing_anchored_vwap + _remove_dsa_lookahead 消除；"
                "_DSAHistoryComputation artifact 复用；call 2→1；p50 116.9→83.8ms（-28.3%）",
    },
    {
        "item": "DSA VWAP-return Python group loop",
        "phase": "3.4B-2",
        "note": "VWAP return 组循环改向量化执行，公式不变",
    },
    {
        "item": "SMC #2 duplicate",
        "phase": "3.4B-3A",
        "note": "_compute_smc_freshness_factors 复用 _shared_raw.smc_result；SMC low-level 2→1；"
                "isolated p50 4.713→0.204ms",
    },
    {
        "item": "Bollinger #2 duplicate",
        "phase": "3.4B-3B",
        "note": "_extract_extra_fields 复用 _shared_raw.bb_df 替代 CanonicalComputationService.compute('bollinger')",
    },
    {
        "item": "First Pyramid input_hash row-wise pandas assembly",
        "phase": "3.4B-6B",
        "note": "_compute_input_hash 逐行 pandas axis=1 改 list/C-level join；hash contract 不变；"
                "105/105 exact；input_hash in-situ p50 20.15→2.08ms（-89.7%）",
    },
]

# ---- 留下的合法计算成本（不再继续优化）----
REMAINING_LEGITIMATE = [
    {
        "item": "full-history DSA #2",
        "note": "不同 contract（lookback=None）的合法 kernel；约 76.6ms（3.4B-5）；复用被禁止",
    },
    {
        "item": "DSA #1",
        "note": "250-bar 合法 kernel；约 52.6ms（3.4B-5）",
    },
    {
        "item": "_build_structure_dimension",
        "note": "First Pyramid structure dimension；约 38ms（3.4B-6A）；本轮不拆不向量化，"
                "未来 AfterClose wall-clock 不满足 SLA 再凭 production profile 回查",
    },
    {
        "item": "VolumeContext",
        "note": "合法序列计算",
    },
    {
        "item": "SQZMOM",
        "note": "疑似重复但未获合同证明；后续需单独证明 temporal defaults == "
                "_FIRST_PYRAMID_PARAMS['sqzmom_config'] 且 bars/index/point-in-time contract 相同",
    },
    {
        "item": "SMC #1",
        "note": "合法 kernel",
    },
]


def _eligible_count_from_parquet() -> dict[str, Any]:
    df = pd.read_parquet(PARQUET_DIR / "bars_daily.parquet")
    g = df.groupby("instrument_id").size()
    eligible = int((g >= ELIGIBLE_MIN_BARS).sum())
    return {
        "total_instruments_in_dataset": int(len(g)),
        "eligibility_rule": f"review-core: len(df_1d) >= {ELIGIBLE_MIN_BARS} "
                            "(compute_review_core_for_trade_date degraded check)",
        "eligible_instruments": eligible,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="3.4B-FINAL review-core compute optimization closure")
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args()

    import subprocess
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()

    if not MANIFEST_PATH.exists():
        sys.exit(f"manifest 不存在: {MANIFEST_PATH}")
    rows = [json.loads(line) for line in open(MANIFEST_PATH, encoding="utf-8")]
    main_rows = [r for r in rows if r["selection_reason"] == "main_ge250"]
    boundary_rows = [r for r in rows if r["selection_reason"] == "boundary_60_249"]
    selected = main_rows[: args.count] + boundary_rows
    print(f"3.4B-FINAL closure benchmark: {len(selected)} instruments "
          f"(main {len(main_rows[:args.count])}, boundary {len(boundary_rows)}), "
          f"warmup={args.warmup} reps={args.reps} -> n_runs={len(selected) * args.reps}")

    bars_by_id = _load_bars()
    runs_all: list[dict[str, Any]] = []
    per_instrument: list[dict[str, Any]] = []
    for row in selected:
        iid = row["instrument_id"]
        if iid not in bars_by_id:
            print(f"[skip] {iid} not in bars")
            continue
        df_all = bars_by_id[iid]
        trade_date = date.fromisoformat(row["max_trade_date"])
        df_1d = df_all[df_all.index.date <= trade_date]
        symbol = row.get("symbol") or "600000"
        try:
            runs = _perf_run_instrument(df_1d, uuid.UUID(iid), trade_date, symbol,
                                        args.warmup, args.reps)
        except Exception as exc:  # noqa: BLE001
            print(f"[perf error] {iid} {symbol}: {type(exc).__name__}: {exc}")
            continue
        runs_all.extend(runs)
        per_instrument.append({
            "instrument_id": iid,
            "symbol": symbol,
            "bars_count": int(len(df_1d)),
            "selection_reason": row["selection_reason"],
            "n_runs": len(runs),
        })
        if len(per_instrument) % 25 == 0:
            print(f"  ... {len(per_instrument)}/{len(selected)} done")

    out: dict[str, Any] = {
        "phase": "3.4B-FINAL",
        "method": "call real compute_review_core_for_trade_date; elapsed wall time (perf_counter); "
                  "exclusive components; SAME harness as 3.4A-3 / 3.4B-4 "
                  "(audit_closure._perf_run_instrument/_perf_report)",
        "reproducibility": {
            "dataset": "review-source-c5c686e-v1",
            "audit_code_sha": head,
            "baseline": {
                "phase": "3.4A-3",
                "path": str(BASELINE_PATH),
                "audit_code_sha": "28aad801fdf0d2d447bf53d5feece578c63ff24b",
            },
            "last_optimization": {
                "phase": "3.4B-6B",
                "audit_code_sha": "0cc7e864",
                "item": "input_hash assembly optimization",
            },
        },
    }
    out = _perf_report(runs_all, out)
    out["per_instrument"] = per_instrument

    # ---- baseline / final / saved ----
    base = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    base_total = next(r for r in base["table"] if r["component"] == "Total per-stock")
    final_total = next(r for r in out["table"] if r["component"] == "Total per-stock")
    b_p50, b_p95 = float(base_total["elapsed_p50_ms"]), float(base_total["elapsed_p95_ms"])
    f_p50, f_p95 = float(final_total["elapsed_p50_ms"]), float(final_total["elapsed_p95_ms"])
    summary = {
        "baseline_3A3_total_ms": {"p50": b_p50, "p95": b_p95},
        "final_total_ms": {"p50": f_p50, "p95": f_p95},
        "saved_ms": {"p50": round(b_p50 - f_p50, 3), "p95": round(b_p95 - f_p95, 3)},
        "saved_pct": {
            "p50": round((b_p50 - f_p50) / b_p50 * 100.0, 2),
            "p95": round((b_p95 - f_p95) / b_p95 * 100.0, 2),
        },
    }

    # ---- delta 表（component 级，baseline 3.4A-3 → final）----
    baseline_p50 = {r["component"]: float(r["elapsed_p50_ms"]) for r in base["table"]}
    delta = []
    for r in out["table"]:
        comp = r["component"]
        cur = float(r["elapsed_p50_ms"])
        if comp == "Total per-stock":
            continue
        b = baseline_p50.get(comp)
        if b is None:
            delta.append({"component": comp, "baseline_p50_ms": None, "final_p50_ms": cur,
                          "saved_ms": None, "saved_pct": None})
            continue
        delta.append({"component": comp, "baseline_p50_ms": round(b, 3), "final_p50_ms": round(cur, 3),
                      "saved_ms": round(b - cur, 3),
                      "saved_pct": round((b - cur) / b * 100.0, 2) if b else 0.0})
    delta.sort(key=lambda d: -(d["saved_ms"] if d["saved_ms"] is not None else float("-inf")))

    # ---- serial projection ----
    eligible_info = _eligible_count_from_parquet()
    n_elig = eligible_info["eligible_instruments"]
    serial_projection = {
        "eligible_instruments": n_elig,
        "eligibility_rule": eligible_info["eligibility_rule"],
        "total_instruments_in_dataset": eligible_info["total_instruments_in_dataset"],
        "p50_serial_projection_seconds": round(f_p50 * n_elig / 1000.0, 1),
        "p50_serial_projection_minutes": round(f_p50 * n_elig / 1000.0 / 60.0, 1),
        "p95_serial_projection_seconds": round(f_p95 * n_elig / 1000.0, 1),
        "p95_serial_projection_minutes": round(f_p95 * n_elig / 1000.0 / 60.0, 1),
        "caveat": "serial compute-only projection != production AfterClose wall-clock "
                  "(不含 DB read、并发、IO、调度、持久化与序列化等真实运行成本)",
    }

    final_report = {
        "phase": "3.4B-FINAL",
        "summary": summary,
        "closed_waste_phase3": CLOSED_WASTE,
        "remaining_legitimate_costs": REMAINING_LEGITIMATE,
        "delta_vs_3A3": delta,
        "serial_projection": serial_projection,
        "note_structure_builder": (
            "_build_structure_dimension ≈38ms 本轮不拆/不向量化/不重构；"
            "当前收尾优先级高于再抠几十毫秒；未来真实 AfterClose wall-clock 不满足 SLA "
            "再凭 production profile 回查。"
        ),
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "cost_decomposition.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "closure_report.json").write_text(
        json.dumps(final_report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 3.4B-FINAL closure ===")
    print(f"baseline 3.4A-3: p50 {b_p50}ms  p95 {b_p95}ms")
    print(f"final          : p50 {f_p50}ms  p95 {f_p95}ms")
    print(f"saved          : p50 {b_p50 - f_p50:.1f}ms ({(b_p50 - f_p50) / b_p50 * 100:.1f}%)  "
          f"p95 {b_p95 - f_p95:.1f}ms ({(b_p95 - f_p95) / b_p95 * 100:.1f}%)")
    print(f"eligible instruments = {n_elig}")
    print(f"serial projection: p50 {f_p50 * n_elig / 1000 / 60:.1f}min  "
          f"p95 {f_p95 * n_elig / 1000 / 60:.1f}min")
    print("PRODUCTION_CODE_DIFF = ZERO（本脚本未修改任何生产代码）")


if __name__ == "__main__":
    main()