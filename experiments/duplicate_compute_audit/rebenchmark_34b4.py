#!/usr/bin/env python3
"""Phase 3.4B-4 — Optimized Full-Chain Re-benchmark（MEASUREMENT ONLY）。

复用 3.4A-3 完全同口径的 perf harness（audit_closure._perf_run_instrument /
_perf_report），在全部 3.4B closure（DSA 内部闭包 + VWAP-return 向量化 / SMC#2 /
BB#2）落地后的当前代码上，重新测量 compute_review_core_for_trade_date 的
exclusive 成本分解，并生成与 3.4A-3 baseline 的 delta 表与当前热点重排。

冻结基准与 3.4A-3 完全一致：
- dataset: backend/.perfdata/review/review-source-c5c686e-v1
- sample: output/3.4A-0/sample_manifest.jsonl（100 main_ge250 + 5 boundary_60_249）
- 测量: warmup=1, reps=3 → n_runs = 105 * 3 = 315
- elapsed wall time（time.perf_counter），exclusive 组件，canonical hash/orchestration 差分

约束：
- PRODUCTION_CODE_DIFF = ZERO（本脚本只读消费 frozen dataset，不修改任何生产代码）
- 不连远程 DB；不做 PG migration / SQL 实验

Usage:
    cd backend && .venv/bin/python ../experiments/duplicate_compute_audit/rebenchmark_34b4.py
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from audit_closure import _load_bars, _perf_report, _perf_run_instrument

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = EXPERIMENT_DIR / "output" / "3.4A-0" / "sample_manifest.jsonl"
BASELINE_PATH = EXPERIMENT_DIR / "output" / "3.4A-3" / "cost_decomposition.json"
OUTPUT_DIR = EXPERIMENT_DIR / "output" / "3.4B-4"

# 与 3.4A-3 输出表完全一致的组件顺序
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


def _baseline_p50_map() -> dict[str, float]:
    if not BASELINE_PATH.exists():
        sys.exit(f"3.4A-3 baseline 不存在: {BASELINE_PATH}")
    base = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {row["component"]: float(row["elapsed_p50_ms"]) for row in base["table"]}


def _build_delta(current: list[dict[str, Any]], baseline_p50: dict[str, float]) -> list[dict[str, Any]]:
    """delta 表：component | baseline p50 | current p50 | saved ms | saved %（按 saved ms 降序）。"""
    delta: list[dict[str, Any]] = []
    for row in current:
        comp = row["component"]
        base = baseline_p50.get(comp)
        cur = float(row["elapsed_p50_ms"])
        if base is None:
            delta.append({
                "component": comp,
                "baseline_p50_ms": None,
                "current_p50_ms": cur,
                "saved_ms": None,
                "saved_pct": None,
                "note": "no 3.4A-3 baseline row",
            })
            continue
        saved_ms = base - cur
        saved_pct = (saved_ms / base * 100.0) if base else 0.0
        delta.append({
            "component": comp,
            "baseline_p50_ms": round(base, 3),
            "current_p50_ms": round(cur, 3),
            "saved_ms": round(saved_ms, 3),
            "saved_pct": round(saved_pct, 2),
        })
    # 按 saved_ms 降序（Total 单独放最前）
    delta.sort(key=lambda d: -d["saved_ms"] if d["saved_ms"] is not None else float("-inf"))
    return delta


def _hotspots_ranked(current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """仅按当前测量结果重排 top hotspots（不含 Total）。"""
    rows = [r for r in current if r["component"] != "Total per-stock"]
    rows = sorted(rows, key=lambda r: -float(r["elapsed_p50_ms"]))
    ranked = []
    for i, r in enumerate(rows, start=1):
        ranked.append({
            "rank": i,
            "component": r["component"],
            "elapsed_p50_ms": float(r["elapsed_p50_ms"]),
            "elapsed_p95_ms": float(r["elapsed_p95_ms"]),
            "median_share_pct": float(r["median_share_pct"]),
        })
    return ranked


def main() -> None:
    ap = argparse.ArgumentParser(description="3.4B-4 optimized full-chain re-benchmark")
    ap.add_argument("--count", type=int, default=100, help="主 sample 数量（默认 100）")
    ap.add_argument("--warmup", type=int, default=1, help="每 instrument warmup 次数（默认 1）")
    ap.add_argument("--reps", type=int, default=3, help="每 instrument timed reps 次数（默认 3）")
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
    print(f"3.4B-4 re-benchmark: {len(selected)} instruments "
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
        "phase": "3.4B-4",
        "method": "call real compute_review_core_for_trade_date; elapsed wall time (perf_counter); "
                  "exclusive components; canonical kernel/hash/orchestration differential; "
                  "SAME harness as 3.4A-3 (audit_closure._perf_run_instrument/_perf_report)",
        "reproducibility": {
            "dataset": "review-source-c5c686e-v1",
            "audit_code_sha": head,
            "baseline": {
                "phase": "3.4A-3",
                "path": str(BASELINE_PATH),
                "audit_code_sha": "28aad801fdf0d2d447bf53d5feece578c63ff24b",
            },
        },
    }
    out = _perf_report(runs_all, out)
    out["per_instrument"] = per_instrument

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "cost_decomposition.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # delta 表 + 当前热点重排
    baseline_p50 = _baseline_p50_map()
    delta = _build_delta(out["table"], baseline_p50)
    hotspots = _hotspots_ranked(out["table"])
    delta_out = {
        "phase": "3.4B-4",
        "baseline_phase": "3.4A-3",
        "baseline_p50_source": str(BASELINE_PATH),
        "units": "ms per stock (elapsed p50 wall time)",
        "delta": delta,
        "current_top_hotspots_by_current_p50": hotspots,
        "note": "hotspot ranking is based ONLY on current 3.4B-4 p50 measurements; "
                "not inherited from 3.4A-3 ordering",
    }
    delta_path = OUTPUT_DIR / "delta.json"
    delta_path.write_text(json.dumps(delta_out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "table": out["table"],
        "canonical_overhead": out["canonical_overhead"],
        "median_share_sum_pct": out["median_share_sum_pct"],
        "delta": delta,
        "current_top_hotspots": hotspots,
    }, ensure_ascii=False, indent=2))
    print(f"output: {out_path}")
    print(f"output: {delta_path}")
    print("NOTE: PRODUCTION_CODE_DIFF = ZERO（本脚本未修改任何生产代码）")


if __name__ == "__main__":
    main()
