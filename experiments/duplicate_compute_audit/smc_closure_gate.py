#!/usr/bin/env python3
"""Phase 3.4B-3A — SMC #2 Residual Duplicate Closure Gate（baseline/verify 双模式）。

目标：让 structural freshness 消费 `_shared_raw.smc_result`，消除第二次
`compute_smc_adapter`（即底层 `compute_smc_pine` 第二次调用）。

方法学（与 3.4B-1/3.4B-2A 同款 dual-run gate）：
- baseline 模式：在修改生产代码【前】运行，走当前生产路径
  （`_compute_all_factors_for_bars` 忽略 precomputed 的 smc_result → freshness 重跑 SMC），
  保存 baseline.json。
- verify 模式：在修改生产代码【后】运行，走新路径
  （`_compute_all_factors_for_bars` 消费 precomputed["smc_result"] → freshness 复用 DTO），
  与 baseline.json 逐样本对比。

两种模式都传同样的 precomputed（dsa_bundle/bb_df/sqz_result/smc_result），
唯一变量是 `_compute_smc_freshness_factors` 的 SMC 来源，精确隔离本刀。

硬 Gate：
1. SMC low-level call count：baseline=2（bundle 1 + freshness 1），verify=1（bundle 1）。
2. freshness payload 105/105 exact。
3. full structural result 105/105 exact（structural_hash 一致）。
4. raw immutability：smc_result hash 在 structural 消费前后一致（→ CoreArtifact /
   bundle / temporal / First Pyramid 输入不变）。

Usage:
    python smc_closure_gate.py --mode baseline --count 105
    python smc_closure_gate.py --mode verify --count 105
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
PARQUET_DIR = (
    REPO_ROOT / "backend" / ".perfdata" / "review" / "review-source-c5c686e-v1" / "parquet"
)
OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "3.4B-3A"
MANIFEST_PATH = (
    Path(__file__).resolve().parent / "output" / "3.4A-0" / "sample_manifest.jsonl"
)
BASELINE_PATH = OUTPUT_DIR / "baseline.json"

SMC_FRESHNESS_MIN_BARS = 250


def _stable_hash(obj: Any) -> str:
    """确定性 SHA256（前 16 字符），用于 payload / raw 一致性对比。"""
    s = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _load_bars() -> dict[str, pd.DataFrame]:
    bars = pd.read_parquet(PARQUET_DIR / "bars_daily.parquet")
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    out: dict[str, pd.DataFrame] = {}
    for iid, g in bars.groupby("instrument_id"):
        g = g.sort_values("trade_date").set_index("trade_date")
        g = g[["open", "high", "low", "close", "volume", "amount"]].astype(float)
        out[iid] = g
    return out


def _install_smc_spy() -> tuple[list[int], Any]:
    """包装 smc_pine_core.compute_smc_pine 在三个持有引用的模块上计数。

    bundle 路径（first_pyramid_service.compute_smc_pine）与 freshness 路径
    （smc_indicator.compute_smc_pine，经 compute_smc_adapter）都会落到同一个
    底层函数对象，三处同时包装才能统计完整调用次数。
    """
    from app.strategy_assets.algorithms.features import smc_indicator
    from app.strategy_assets.algorithms.features import smc_pine_core
    from app.services import first_pyramid_service

    counter: list[int] = [0]
    orig = smc_pine_core.compute_smc_pine

    def _wrapper(*a: Any, **kw: Any) -> Any:
        counter[0] += 1
        return orig(*a, **kw)

    smc_pine_core.compute_smc_pine = _wrapper
    first_pyramid_service.compute_smc_pine = _wrapper
    smc_indicator.compute_smc_pine = _wrapper
    return counter, orig


def _hash_raw_piece(name: str, piece: Any) -> str:
    """确定性 hash 单个 raw 片段（DataFrame 用 repr 的稳定排序形式）。"""
    if isinstance(piece, pd.DataFrame):
        s = piece.astype(str).to_json(orient="split", sort_keys=True, double_precision=15)
    else:
        s = json.dumps(piece, sort_keys=True, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _raw_piece_hashes(raw: Any) -> dict[str, str]:
    """对 CoreArtifact/bundle/temporal/FirstPyramid 消费的 raw 片段取 hash。

    证明 structural 消费 raw 后这些片段不被污染 → 下游各 artifact 输入不变。
    """
    out = {
        "smc_result": _hash_raw_piece("smc_result", raw.smc_result),
    }
    try:
        out["bb_df"] = _hash_raw_piece("bb_df", raw.bb_df)
    except Exception:
        pass
    try:
        out["sqzmom_result"] = _hash_raw_piece("sqzmom_result", raw.sqzmom_result)
    except Exception:
        pass
    try:
        out["dsa_bundle"] = _hash_raw_piece("dsa_bundle", raw.dsa_bundle)
    except Exception:
        pass
    return out


def _run_instrument(df_1d: pd.DataFrame) -> dict[str, Any]:
    """对单 instrument 执行完整 canonical 计算链并采集 gate 记录。"""
    from app.services.core_artifact_service import compute_core_kernel_bundle
    from app.services.structural_factor_service import _compute_all_factors_for_bars

    counter, _orig = _install_smc_spy()
    try:
        # SMC #1（bundle 唯一 kernel owner）
        raw = compute_core_kernel_bundle(df_1d, None)
        # 与 feature_snapshot_service._structural_precomputed 一致（structural 消费的子集）
        pre = {
            "dsa_bundle": raw.dsa_bundle,
            "bb_df": raw.bb_df,
            "sqz_result": raw.sqzmom_result,
            "smc_result": raw.smc_result,
        }
        hash_before = _raw_piece_hashes(raw)
        degraded: list[str] = []
        warmup: list[str] = []
        structural = _compute_all_factors_for_bars(
            df_1d, "1d", degraded, warmup, precomputed=pre
        )
        hash_after = _raw_piece_hashes(raw)
        smc_calls = counter[0]

        smc_freshness = structural.get("smc_freshness") or {}
        # full structural（排除 smc_freshness）单独 hash，隔离本刀影响面
        structural_other = {
            k: v for k, v in structural.items() if k != "smc_freshness"
        }
        raw_immutable_all = (
            hash_before == hash_after and hash_before.get("smc_result") is not None
        )
        return {
            "smc_calls": smc_calls,
            "smc_freshness": smc_freshness,
            "structural_hash": _stable_hash(structural),
            "structural_other_hash": _stable_hash(structural_other),
            "raw_smc_hash_before": hash_before.get("smc_result"),
            "raw_smc_hash_after": hash_after.get("smc_result"),
            "raw_immutable": raw_immutable_all,
            "raw_pieces_immutable": all(
                hash_before.get(k) == hash_after.get(k) for k in hash_before
            ),
            "eligible": len(df_1d) >= SMC_FRESHNESS_MIN_BARS,
            "degraded": degraded,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "smc_calls": counter[0],
            "smc_freshness": None,
            "structural_hash": None,
            "structural_other_hash": None,
            "raw_smc_hash_before": None,
            "raw_smc_hash_after": None,
            "raw_immutable": False,
            "raw_pieces_immutable": False,
            "eligible": len(df_1d) >= SMC_FRESHNESS_MIN_BARS,
            "degraded": [],
        }


def _select_samples(count: int, include_boundary: bool) -> list[dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        sys.exit(f"manifest 不存在: {MANIFEST_PATH}（先运行 build_manifest.py）")
    rows = [json.loads(line) for line in open(MANIFEST_PATH, encoding="utf-8")]
    main_rows = [r for r in rows if r["selection_reason"] == "main_ge250"]
    boundary_rows = [r for r in rows if r["selection_reason"] == "boundary_60_249"]
    selected = main_rows[:count]
    if include_boundary:
        selected = selected + boundary_rows
    return selected


def _run_all(bars_by_id: dict[str, pd.DataFrame], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in rows:
        iid = row["instrument_id"]
        if iid not in bars_by_id:
            continue
        df_all = bars_by_id[iid]
        trade_date = date.fromisoformat(row["max_trade_date"])
        df_1d = df_all[df_all.index.date <= trade_date]
        rec = _run_instrument(df_1d)
        out = {
            "instrument_id": iid,
            "symbol": row.get("symbol"),
            "market": row.get("market"),
            "bars_count": int(len(df_1d)),
        }
        out.update(rec)
        results.append(out)
    return results


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    ok = [r for r in results if not r.get("error")]
    return {
        "total": total,
        "errors": total - len(ok),
        "smc_calls_dist": sorted({r["smc_calls"] for r in ok}),
        "raw_immutable_ok": f"{sum(1 for r in ok if r['raw_immutable'])}/{total}",
    }


def _bench_isolated(bars_by_id: dict[str, pd.DataFrame], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """隔离测量：被消除的 SMC #2（compute_smc_adapter）vs 替代的 DTO 转换（adapt_smc_to_display_dto）。

    两者消费同一份 bars/raw，直接对比每 stock 耗时，即本刀的真实收益下限。
    """
    from app.services.canonical_adapters import compute_smc_adapter
    from app.services.core_artifact_service import compute_core_kernel_bundle
    from app.services.smc_view_adapter import adapt_smc_to_display_dto

    def _timeit(fn, *a, **kw) -> float:
        import time

        t0 = time.perf_counter()
        fn(*a, **kw)
        return (time.perf_counter() - t0) * 1000.0

    adapter_ms: list[float] = []
    dto_ms: list[float] = []
    for row in rows:
        iid = row["instrument_id"]
        if iid not in bars_by_id:
            continue
        df_all = bars_by_id[iid]
        trade_date = date.fromisoformat(row["max_trade_date"])
        df_1d = df_all[df_all.index.date <= trade_date]
        smc_raw = compute_core_kernel_bundle(df_1d, None).smc_result
        adapter_ms.append(_timeit(compute_smc_adapter, df_1d, display_bars=len(df_1d)))
        dto_ms.append(_timeit(adapt_smc_to_display_dto, smc_raw, len(df_1d)))

    def _p(xs: list[float], q: float) -> float:
        s = sorted(xs)
        return s[min(len(s) - 1, int(q / 100 * len(s)))]

    out = {
        "phase": "3.4B-3A",
        "mode": "bench",
        "note": "adapter=SMC #2（被消除）；dto=DTO 转换（替代）。净省 ≈ adapter - dto",
        "summary": {
            "samples": len(adapter_ms),
            "smc_adapter_ms_p50": round(_p(adapter_ms, 50), 3),
            "smc_adapter_ms_p95": round(_p(adapter_ms, 95), 3),
            "dto_ms_p50": round(_p(dto_ms, 50), 3),
            "dto_ms_p95": round(_p(dto_ms, 95), 3),
            "net_saving_ms_p50": round(_p(adapter_ms, 50) - _p(dto_ms, 50), 3),
        },
    }
    out_path = OUTPUT_DIR / "bench.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
    print(f"bench output: {out_path}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3.4B-3A SMC #2 closure gate")
    ap.add_argument("--mode", choices=["baseline", "verify", "bench"], required=True)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--include-boundary", action="store_true",
                    help="额外纳入 5 个 boundary_60_249 样本（合计 105）")
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = _select_samples(args.count, args.include_boundary)
    print(f"mode={args.mode} samples={len(rows)}")

    bars_by_id = _load_bars()
    if args.mode == "bench":
        _bench_isolated(bars_by_id, rows)
        return
    results = _run_all(bars_by_id, rows)
    summary = _summarize(results)

    if args.mode == "baseline":
        doc = {
            "phase": "3.4B-3A",
            "mode": "baseline",
            "note": "未修改生产代码（当前行为：freshness 重跑 SMC → smc_calls 应为 2）",
            "summary": summary,
            "results": results,
        }
        with open(BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"baseline saved: {BASELINE_PATH}")

    elif args.mode == "verify":
        if not BASELINE_PATH.exists():
            sys.exit("baseline.json 不存在，先跑 --mode baseline")
        baseline_doc = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        baseline = {r["instrument_id"]: r for r in baseline_doc["results"]}

        comp: list[dict[str, Any]] = []
        for r in results:
            b = baseline.get(r["instrument_id"])
            if b is None:
                comp.append({"instrument_id": r["instrument_id"], "match": False,
                             "reason": "no_baseline", "error": r.get("error")})
                continue
            if r.get("error") or b.get("error"):
                comp.append({"instrument_id": r["instrument_id"], "match": False,
                             "reason": "error", "error": r.get("error") or b.get("error"),
                             "smc_calls_b": b.get("smc_calls"), "smc_calls_v": r.get("smc_calls")})
                continue
            freshness_exact = (b["smc_freshness"] == r["smc_freshness"])
            structural_exact = (b["structural_hash"] == r["structural_hash"])
            other_exact = (b["structural_other_hash"] == r["structural_other_hash"])
            raw_immutable = r["raw_immutable"]
            raw_pieces_immutable = r["raw_pieces_immutable"]
            calls_b, calls_v = b["smc_calls"], r["smc_calls"]
            # main(>=250)：freshness 会跑 SMC，期望 2→1；boundary(<250)：freshness 不跑，期望 1→1
            if r["eligible"]:
                call_gate = calls_b == 2 and calls_v == 1
            else:
                call_gate = calls_b == 1 and calls_v == 1
            comp.append({
                "instrument_id": r["instrument_id"],
                "symbol": r["symbol"],
                "bars_count": r["bars_count"],
                "freshness_exact": freshness_exact,
                "structural_exact": structural_exact,
                "structural_other_exact": other_exact,
                "raw_immutable": raw_immutable,
                "raw_pieces_immutable": raw_pieces_immutable,
                "calls_baseline": calls_b,
                "calls_verify": calls_v,
                "call_gate": call_gate,
                "match": freshness_exact and structural_exact and other_exact
                         and raw_immutable and raw_pieces_immutable and call_gate,
            })

        matched = [c for c in comp if c.get("match")]
        freshness_n = sum(1 for c in comp if c.get("freshness_exact"))
        structural_n = sum(1 for c in comp if c.get("structural_exact"))
        other_n = sum(1 for c in comp if c.get("structural_other_exact"))
        immut_n = sum(1 for c in comp if c.get("raw_immutable"))
        pieces_immut_n = sum(1 for c in comp if c.get("raw_pieces_immutable"))
        call_gate_n = sum(1 for c in comp if c.get("call_gate"))
        total = len(comp)
        out = {
            "phase": "3.4B-3A",
            "mode": "verify",
            "summary": {
                "total": total,
                "all_gates_pass": f"{len(matched)}/{total}",
                "freshness_exact": f"{freshness_n}/{total}",
                "structural_exact": f"{structural_n}/{total}",
                "structural_other_exact": f"{other_n}/{total}",
                "raw_immutable": f"{immut_n}/{total}",
                "raw_pieces_immutable": f"{pieces_immut_n}/{total}",
                "smc_call_gate_2to1": f"{call_gate_n}/{total}",
                "errors": sum(1 for c in comp if c.get("reason") == "error"),
            },
            "results": comp,
        }
        out_path = OUTPUT_DIR / "verify.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
        print(f"verify output: {out_path}")

        failed = [c for c in comp if not c.get("match")]
        if failed:
            print("\nNON-PASS:")
            for c in failed[:20]:
                print(" ", c.get("symbol"), c.get("bars_count"), c.get("reason", ""),
                      {k: c.get(k) for k in ("freshness_exact", "structural_exact",
                                             "raw_immutable", "raw_pieces_immutable",
                                             "calls_baseline", "calls_verify", "call_gate")})
            sys.exit(1)
        print("\nALL GATES PASS — SMC #2 closure 105/105 exact，call count 2→1")


if __name__ == "__main__":
    main()
