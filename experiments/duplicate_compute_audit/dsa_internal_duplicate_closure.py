#!/usr/bin/env python3
"""Phase 3.4B-1 — DSA Internal Duplicate Closure 门禁脚本（先捕获基线，再 verify）。

目标优化（common-subexpression elimination，不改任何算法语义）：
    compute_dsa_bundle 内部当前执行 dynamic_swing_anchored_vwap + _remove_dsa_lookahead
    各 2 次（第一次在 compute_dsa_history 里扔掉 pivot_labels/segments，第二次为了
    visual 字段再算一遍）。3.4B-A 实测 bundle=117.1ms、history=74.6ms，可省 ~42ms (36%)。

    重构 = 增加私有 _compute_dsa_history_artifact（一次 DSA + 一次 lookahead，多投影），
    compute_dsa_history / compute_dsa_bundle 都从它消费；输出 100% identical。

硬 Gate（本脚本验证）：
1. Artifact Parity：105 frozen 样本，bundle 输出（factor_per_bar 全保真 / visual_segments /
   pivot_labels / factor_time / anchor / last_row_metrics）sha256 与基线 100% identical。
2. Temporal Parity：daily_dsa_segment_duration_percentile 105/105 完全相同（用生产函数）。
3. Call-count：一次 compute_dsa_bundle 内顶层 dynamic_swing 2→1、_remove_dsa_lookahead 2→1
   （_remove_dsa_lookahead 内部必要的 prefix recomputation 不计数、保持不变）。
4. 性能：105 样本 bundle p50/p95 baseline vs optimized。

方法学：只读消费 frozen dataset；不连远程 DB；不改任何 DSA 参数/percentile/segment 语义。

Usage:
    cd backend
    PYTHONPATH=. .venv/bin/python ../experiments/duplicate_compute_audit/dsa_internal_duplicate_closure.py \
        --mode capture [--count 100 --include-boundary --reps 3]   # 重构前
    ... --mode verify  [--count 100 --include-boundary --reps 3]   # 重构后
    ... --mode callcount [--count 20]                              # 重构前后各一次
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from segment_artifact_feasibility import (
    MANIFEST_PATH,
    MIN_SEGMENTS_FOR_PERCENTILE,
    _extract_ages_from_bundle,
    _load_bars,
)

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "3.4B-1"


# ---------------------------------------------------------------------------
# bundle 全保真序列化（sha256）
# ---------------------------------------------------------------------------
def _bundle_hash(bundle: dict[str, Any]) -> str:
    fpb = bundle.get("factor_per_bar")
    if fpb is None or fpb.empty:
        fpb_part = {"empty": True}
    else:
        fpb_part = {
            "index": [ts.isoformat() for ts in fpb.index],
            "columns": list(fpb.columns),
            "dtypes": [str(d) for d in fpb.dtypes],
            # pandas 此版本 double_precision 上限 15；15 位有效数字对纯 CSE
            # （bit-identical 中间结果）的 parity 检测足够敏感，配合
            # dtypes/columns/index/duration_percentile 多重校验。
            "values": fpb.to_json(orient="split", date_format="iso", double_precision=15),
        }
    payload = {
        "factor_per_bar": fpb_part,
        "visual_segments": bundle.get("visual_segments"),
        "factor_time": [
            ts.isoformat() for ts in bundle.get("factor_time", [])
        ],
        "pivot_labels": bundle.get("pivot_labels"),
        "anchor": bundle.get("anchor"),
        "last_row_metrics": bundle.get("last_row_metrics"),
    }
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _temporal_percentile(bundle: dict[str, Any]) -> float | None:
    """复刻 temporal_feature_service 的 daily_dsa_segment_duration_percentile 算术。

    current_age = 当前 DSA 段 age（full-history bundle last_row_metrics.segment_bars，
    与 production 非 precomputed 路径同源）；hist_ages 用 _extract_ages_from_bundle
    （3.4B-0 已验证与生产 _collect_historical_segment_ages 105/105 一致）。
    """
    from app.services.structural_factor_service import percentile_rank

    current_age = (bundle.get("last_row_metrics") or {}).get("segment_bars")
    if current_age is None:
        return None
    hist_ages = _extract_ages_from_bundle(bundle)
    if len(hist_ages) < MIN_SEGMENTS_FOR_PERCENTILE:
        return None
    return percentile_rank(
        float(current_age), np.array(hist_ages, dtype=float), len(hist_ages)
    )


def _timed(fn: Callable[[], Any], warmup: int = 1, reps: int = 3) -> float:
    for _ in range(warmup):
        fn()
    ts: list[float] = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    return float(np.median(ts)) * 1000.0


def _run_instrument(df_1d: pd.DataFrame, reps: int) -> dict[str, Any]:
    from app.strategy.selectors.dsa_selector import compute_dsa_bundle

    bundle = compute_dsa_bundle(df_1d, {})
    bundle_hash = _bundle_hash(bundle)
    pct = _temporal_percentile(bundle)
    t_bundle = _timed(lambda: compute_dsa_bundle(df_1d, {}), warmup=1, reps=reps)
    return {
        "bars_count": int(len(df_1d)),
        "bundle_hash": bundle_hash,
        "duration_percentile": pct,
        "t_bundle_ms": t_bundle,
    }


def _select(count: int, include_boundary: bool) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in open(MANIFEST_PATH, encoding="utf-8")]
    main_rows = [r for r in rows if r["selection_reason"] == "main_ge250"]
    boundary_rows = [r for r in rows if r["selection_reason"] == "boundary_60_249"]
    selected = main_rows[:count]
    if include_boundary:
        selected = selected + boundary_rows
    return selected


def _load_sample_dfs(selected: list[dict[str, Any]]) -> list[tuple[dict[str, Any], pd.DataFrame]]:
    bars_by_id = _load_bars()
    out: list[tuple[dict[str, Any], pd.DataFrame]] = []
    for row in selected:
        iid = row["instrument_id"]
        if iid not in bars_by_id:
            continue
        from datetime import date

        df_all = bars_by_id[iid]
        trade_date = date.fromisoformat(row["max_trade_date"])
        out.append((row, df_all[df_all.index.date <= trade_date]))
    return out


# ---------------------------------------------------------------------------
# 模式 1/2：capture / verify
# ---------------------------------------------------------------------------
def _run_capture(selected: list[dict[str, Any]], reps: int) -> list[dict[str, Any]]:
    samples = _load_sample_dfs(selected)
    results: list[dict[str, Any]] = []
    for i, (row, df_1d) in enumerate(samples):
        rec = {
            "instrument_id": row["instrument_id"],
            "symbol": row.get("symbol"),
            "market": row.get("market"),
            "selection_reason": row["selection_reason"],
        }
        try:
            rec.update(_run_instrument(df_1d, reps))
        except Exception as exc:  # noqa: BLE001
            rec.update(
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "bundle_hash": None,
                    "duration_percentile": None,
                    "t_bundle_ms": None,
                }
            )
        results.append(rec)
        if (i + 1) % 25 == 0 or (i + 1) == len(results):
            print(f"  ... {i + 1}/{len(results)} done")
    return results


def _perf_stats(results: list[dict[str, Any]]) -> dict[str, float]:
    vals = [r["t_bundle_ms"] for r in results if r.get("t_bundle_ms") is not None]
    if not vals:
        return {"n": 0, "p50": 0.0, "p95": 0.0, "mean": 0.0}
    arr = np.array(vals)
    return {
        "n": len(arr),
        "p50": round(float(np.median(arr)), 2),
        "p95": round(float(np.percentile(arr, 95)), 2),
        "mean": round(float(arr.mean()), 2),
    }


def _mode_capture(count: int, include_boundary: bool, reps: int) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    selected = _select(count, include_boundary)
    print(f"capturing baseline: {len(selected)} instruments (reps={reps})")
    results = _run_capture(selected, reps)

    with open(OUTPUT_DIR / "baseline.jsonl", "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    perf = _perf_stats(results)
    with open(OUTPUT_DIR / "baseline_perf.json", "w", encoding="utf-8") as f:
        json.dump(perf, f, ensure_ascii=False, indent=2)
    print(f"baseline perf: {perf}")
    print(f"wrote {OUTPUT_DIR / 'baseline.jsonl'} and {OUTPUT_DIR / 'baseline_perf.json'}")


def _mode_verify(count: int, include_boundary: bool, reps: int) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ref_path = OUTPUT_DIR / "baseline.jsonl"
    if not ref_path.exists():
        sys.exit(f"基线不存在: {ref_path}（先运行 --mode capture）")
    refs = [json.loads(line) for line in open(ref_path, encoding="utf-8")]
    ref_map = {r["instrument_id"]: r for r in refs}

    selected = _select(count, include_boundary)
    print(f"verifying: {len(selected)} instruments (reps={reps})")
    results = _run_capture(selected, reps)

    n_hash = n_pct = n_total = 0
    diffs: list[dict[str, Any]] = []
    for r in results:
        ref = ref_map.get(r["instrument_id"])
        if ref is None:
            diffs.append({"instrument_id": r["instrument_id"], "reason": "missing_in_baseline"})
            continue
        n_total += 1
        hash_ok = r.get("bundle_hash") is not None and r["bundle_hash"] == ref.get("bundle_hash")
        pct_ok = r.get("duration_percentile") == ref.get("duration_percentile")
        if hash_ok:
            n_hash += 1
        if pct_ok:
            n_pct += 1
        if not (hash_ok and pct_ok):
            diffs.append(
                {
                    "instrument_id": r["instrument_id"],
                    "symbol": r.get("symbol"),
                    "selection_reason": r.get("selection_reason"),
                    "hash_equal": hash_ok,
                    "pct_equal": pct_ok,
                    "pct_before": ref.get("duration_percentile"),
                    "pct_after": r.get("duration_percentile"),
                    "error": r.get("error"),
                }
            )

    baseline_perf = json.load(open(OUTPUT_DIR / "baseline_perf.json", encoding="utf-8"))
    after_perf = _perf_stats(results)
    p50_delta = after_perf["p50"] - baseline_perf["p50"]

    summary = {
        "phase": "3.4B-1",
        "method": "CSE: compute_dsa_bundle 内第二次 dynamic_swing_anchored_vwap + "
                  "_remove_dsa_lookahead 移除，改消费 _compute_dsa_history_artifact 投影",
        "gate": "bundle sha256 100% identical + duration_percentile 105/105 identical",
        "n_total": n_total,
        "n_hash_identical": n_hash,
        "n_percentile_identical": n_pct,
        "hash_ratio": f"{n_hash}/{n_total}",
        "percentile_ratio": f"{n_pct}/{n_total}",
        "perf_baseline_ms": baseline_perf,
        "perf_after_ms": after_perf,
        "perf_p50_delta_ms": round(p50_delta, 2),
        "parity_pass": n_total > 0 and n_hash == n_total and n_pct == n_total,
        "diffs": diffs[:20],
    }

    with open(OUTPUT_DIR / "verify_result.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    with open(OUTPUT_DIR / "after.jsonl", "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    print(f"\nartifact hash identical: {n_hash}/{n_total}")
    print(f"percentile identical: {n_pct}/{n_total}")
    print(f"perf bundle p50: baseline={baseline_perf.get('p50')}ms -> after={after_perf.get('p50')}ms "
          f"(delta {p50_delta:+.2f}ms)")
    print(f"parity_pass: {summary['parity_pass']}")
    if diffs:
        print(f"{len(diffs)} diffs (first 10):")
        for d in diffs[:10]:
            print(f"  {d}")
    print(f"wrote {OUTPUT_DIR / 'verify_result.json'}")


# ---------------------------------------------------------------------------
# 模式 3：call-count gate
# ---------------------------------------------------------------------------
def _mode_callcount(count: int) -> None:
    selected = _select(count, include_boundary=False)
    samples = _load_sample_dfs(selected)[:count]
    print(f"call-count gate: {len(samples)} instruments (一次 compute_dsa_bundle each)")

    import app.strategy.selectors.dsa_selector as ds

    real_dsw = ds.dynamic_swing_anchored_vwap
    real_rla = ds._remove_dsa_lookahead
    counts = {"dsw_top": 0, "rla_top": 0, "n": 0}
    depth = 0

    def _counting_dsw(*a: Any, **k: Any) -> Any:
        if depth == 0:
            counts["dsw_top"] += 1
        return real_dsw(*a, **k)

    def _counting_rla(*a: Any, **k: Any) -> Any:
        nonlocal depth
        counts["rla_top"] += 1
        depth += 1
        try:
            return real_rla(*a, **k)
        finally:
            depth -= 1

    ds.dynamic_swing_anchored_vwap = _counting_dsw
    ds._remove_dsa_lookahead = _counting_rla
    per_sample: list[dict[str, Any]] = []
    try:
        for row, df_1d in samples:
            counts["dsw_top"] = counts["rla_top"] = 0
            from app.strategy.selectors.dsa_selector import compute_dsa_bundle

            compute_dsa_bundle(df_1d, {})
            per_sample.append(
                {
                    "instrument_id": row["instrument_id"],
                    "symbol": row.get("symbol"),
                    "dsw_top": counts["dsw_top"],
                    "rla_top": counts["rla_top"],
                }
            )
    finally:
        ds.dynamic_swing_anchored_vwap = real_dsw
        ds._remove_dsa_lookahead = real_rla

    n_ok = sum(1 for p in per_sample if p["dsw_top"] == 1 and p["rla_top"] == 1)
    out = {
        "gate": "一次 compute_dsa_bundle 内：顶层 dynamic_swing_anchored_vwap == 1 且 "
                "_remove_dsa_lookahead == 1（内部 prefix recomputation 不计数）",
        "n": len(per_sample),
        "n_pass": n_ok,
        "ratio": f"{n_ok}/{len(per_sample)}",
        "per_sample": per_sample[:10],
    }
    with open(OUTPUT_DIR / "callcount.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"call-count: {out['ratio']} pass (dsw_top==1 && rla_top==1)")
    for p in per_sample[:5]:
        print(f"  {p['symbol']}: dsw_top={p['dsw_top']} rla_top={p['rla_top']}")
    print(f"wrote {OUTPUT_DIR / 'callcount.json'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3.4B-1 DSA internal duplicate closure gates")
    ap.add_argument("--mode", choices=["capture", "verify", "callcount"], required=True)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--include-boundary", action="store_true")
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args()

    if args.mode == "capture":
        _mode_capture(args.count, args.include_boundary, args.reps)
    elif args.mode == "verify":
        _mode_verify(args.count, args.include_boundary, args.reps)
    elif args.mode == "callcount":
        _mode_callcount(args.count)


if __name__ == "__main__":
    main()
