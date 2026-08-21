#!/usr/bin/env python3
"""Phase 3.4B-3B — BB #2 Residual Compute Closure Gate（dual / bench / postchange 三模式）。

BB #2 = EQUIVALENT_OUTPUT_SUBSET candidate（两个 producer 是不同 kernel）：
- Producer A：`_extract_extra_fields` canonical Bollinger
  （CanonicalComputationService → merged_dsa_atr_rope_bb_factors.compute_bollinger）
- Producer B：`_shared_raw.bb_df`
  （First Pyramid → bollinger_features_plotly.compute_features(bollinger)）

两个 Gate 全过才允许复用：
Gate 1 = 静态合同核对（本脚本假设已完成，详见对话审计）；
Gate 2 = 105 全序列 exact parity（index/dtype/NaN mask/float，禁 allclose/tolerance）。

模式：
- dual：对 105 样本 A vs B 三字段（bb_mid/bb_upper/bb_lower）全序列 exact。
- bench：隔离测量旧 canonical BB #2（compute_bollinger_adapter）耗时。
- postchange：修改生产代码后运行，验证 review-core extra 复用路径
  （precomputed_bb_df=raw.bb_df → 第二次 Bollinger execution 消失）与
  fallback 路径（precomputed_bb_df=None → 仍调用 canonical Bollinger）
  的 extra 三字段输出一致 + raw.bb_df immutable。

Usage:
    python bb_closure_gate.py --mode dual --count 100 --include-boundary
    python bb_closure_gate.py --mode bench --count 100 --include-boundary
    python bb_closure_gate.py --mode postchange --count 100 --include-boundary
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
PARQUET_DIR = (
    REPO_ROOT / "backend" / ".perfdata" / "review" / "review-source-c5c686e-v1" / "parquet"
)
OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "3.4B-3B"
MANIFEST_PATH = (
    Path(__file__).resolve().parent / "output" / "3.4A-0" / "sample_manifest.jsonl"
)
DUAL_RESULT_PATH = OUTPUT_DIR / "dual_result.json"

BB_FIELDS = ["bb_mid", "bb_upper", "bb_lower"]


def _stable_hash(obj: Any) -> str:
    s = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _df_hash(df: pd.DataFrame) -> str:
    # orient="split" 固定键序（index/columns/data），无需 sort_keys
    return _stable_hash(df.astype(str).to_json(orient="split", double_precision=15))


def _load_bars() -> dict[str, pd.DataFrame]:
    bars = pd.read_parquet(PARQUET_DIR / "bars_daily.parquet")
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    out: dict[str, pd.DataFrame] = {}
    for iid, g in bars.groupby("instrument_id"):
        g = g.sort_values("trade_date").set_index("trade_date")
        g = g[["open", "high", "low", "close", "volume", "amount"]].astype(float)
        out[iid] = g
    return out


def _select_samples(count: int, include_boundary: bool) -> list[dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        sys.exit(f"manifest 不存在: {MANIFEST_PATH}")
    rows = [json.loads(line) for line in open(MANIFEST_PATH, encoding="utf-8")]
    main_rows = [r for r in rows if r["selection_reason"] == "main_ge250"]
    boundary_rows = [r for r in rows if r["selection_reason"] == "boundary_60_249"]
    selected = main_rows[:count]
    if include_boundary:
        selected = selected + boundary_rows
    return selected


def _truncate(bars_all: pd.DataFrame, row: dict[str, Any]) -> pd.DataFrame:
    trade_date = date.fromisoformat(row["max_trade_date"])
    return bars_all[bars_all.index.date <= trade_date]


def _series_exact(a: pd.Series, b: pd.Series) -> dict[str, Any]:
    idx_exact = a.index.equals(b.index) and a.index.dtype == b.index.dtype
    dtype_exact = str(a.dtype) == str(b.dtype)
    av = a.to_numpy()
    bv = b.to_numpy()
    nan_a = pd.isna(av)
    nan_b = pd.isna(bv)
    nan_mask_exact = bool(np.array_equal(nan_a, nan_b))
    float_exact = False
    if nan_mask_exact:
        both_valid = ~nan_a
        float_exact = bool(np.array_equal(av[both_valid], bv[both_valid]))
    return {
        "index_exact": bool(idx_exact),
        "dtype_exact": bool(dtype_exact),
        "nan_mask_exact": nan_mask_exact,
        "float_exact": float_exact,
        "pass": bool(idx_exact) and bool(dtype_exact) and nan_mask_exact and float_exact,
    }


def _install_bb_spy() -> tuple[list[int], Any]:
    """计数 canonical Bollinger kernel（merged_dsa_atr_rope_bb_factors.compute_bollinger）。

    compute_bollinger_adapter 内部是惰性 import（函数内 from ... import compute_bollinger），
    运行时会读取模块属性，因此替换模块属性即可拦截，无需动 canonical_adapters。
    """
    from app.strategy_assets.algorithms.features import merged_dsa_atr_rope_bb_factors

    counter: list[int] = [0]
    orig = merged_dsa_atr_rope_bb_factors.compute_bollinger

    def _wrapper(*a: Any, **kw: Any) -> Any:
        counter[0] += 1
        return orig(*a, **kw)

    merged_dsa_atr_rope_bb_factors.compute_bollinger = _wrapper
    return counter, orig


# =============================================================================
# dual — A vs B 全序列 exact
# =============================================================================


def _run_dual(bars_by_id: dict[str, pd.DataFrame], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.services.canonical_adapters import compute_bollinger_adapter
    from app.services.core_artifact_service import compute_core_kernel_bundle

    results: list[dict[str, Any]] = []
    for row in rows:
        iid = row["instrument_id"]
        df_1d = _truncate(bars_by_id[iid], row)
        raw = compute_core_kernel_bundle(df_1d, None)
        bb_b = raw.bb_df
        bb_a = compute_bollinger_adapter(df_1d, length=20, mult=2.0)

        rec: dict[str, Any] = {
            "instrument_id": iid,
            "symbol": row.get("symbol"),
            "bars_count": int(len(df_1d)),
            "fields": {},
            "pass": True,
        }
        for f in BB_FIELDS:
            if f not in bb_a.columns or f not in bb_b.columns:
                rec["fields"][f] = {"pass": False, "reason": "missing_column"}
                rec["pass"] = False
                continue
            r = _series_exact(bb_a[f], bb_b[f])
            # 最后一根（iloc[-1]）一致 —— 即 _extract_extra_fields 实际消费的值
            a_last = bb_a[f].iloc[-1]
            b_last = bb_b[f].iloc[-1]
            last_nan_eq = bool(pd.isna(a_last) == pd.isna(b_last))
            last_val_eq = bool(
                (pd.isna(a_last) and pd.isna(b_last))
                or (not pd.isna(a_last) and not pd.isna(b_last) and float(a_last) == float(b_last))
            )
            r["last_nan_eq"] = last_nan_eq
            r["last_val_eq"] = last_val_eq
            r["pass"] = r["pass"] and last_nan_eq and last_val_eq
            rec["fields"][f] = r
            rec["pass"] = rec["pass"] and r["pass"]
        results.append(rec)
    return results


# =============================================================================
# bench — 隔离测量 canonical BB #2
# =============================================================================


def _run_bench(bars_by_id: dict[str, pd.DataFrame], rows: list[dict[str, Any]]) -> dict[str, Any]:
    import statistics
    import time

    from app.services.canonical_adapters import compute_bollinger_adapter

    ms: list[float] = []
    for row in rows:
        iid = row["instrument_id"]
        df_1d = _truncate(bars_by_id[iid], row)
        t0 = time.perf_counter()
        compute_bollinger_adapter(df_1d, length=20, mult=2.0)
        ms.append((time.perf_counter() - t0) * 1000.0)

    def _p(xs: list[float], q: float) -> float:
        s = sorted(xs)
        return s[min(len(s) - 1, int(q / 100 * len(s)))]

    out = {
        "phase": "3.4B-3B",
        "mode": "bench",
        "note": "canonical BB #2（compute_bollinger_adapter）每 stock 耗时；复用后这部分消失",
        "summary": {
            "samples": len(ms),
            "bb2_ms_p50": round(_p(ms, 50), 3),
            "bb2_ms_p95": round(_p(ms, 95), 3),
        },
    }
    (OUTPUT_DIR / "bench.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
    return out


# =============================================================================
# postchange — 修改后 review-core extra 复用 / fallback 验证
# =============================================================================


async def _run_postchange_single(
    df_1d: pd.DataFrame, iid: uuid.UUID, td: date, raw_bb_df: pd.DataFrame | None
) -> dict[str, Any]:
    from app.services.feature_snapshot_service import _extract_extra_fields

    counter, _orig = _install_bb_spy()

    extra_reuse = await _extract_extra_fields(
        df_1d, iid, td, source_bar_hash=None, adj_factor_hash=None,
        precomputed_bb_df=raw_bb_df,
    )
    calls_reuse = counter[0]

    extra_fallback = await _extract_extra_fields(
        df_1d, iid, td, source_bar_hash=None, adj_factor_hash=None,
        precomputed_bb_df=None,
    )
    calls_fallback = counter[0]

    same_bb = all(
        extra_reuse[k] == extra_fallback[k] for k in ("bb_upper", "bb_mid", "bb_lower")
    )
    return {
        "extra_reuse": {k: extra_reuse[k] for k in ("bb_upper", "bb_mid", "bb_lower")},
        "extra_fallback": {k: extra_fallback[k] for k in ("bb_upper", "bb_mid", "bb_lower")},
        "calls_reuse": calls_reuse,
        "calls_fallback": calls_fallback,
        "same_bb": same_bb,
        "pass": same_bb and calls_reuse == 0 and calls_fallback == 1,
    }


def _run_postchange(bars_by_id: dict[str, pd.DataFrame], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.services.core_artifact_service import compute_core_kernel_bundle

    results: list[dict[str, Any]] = []
    for row in rows:
        iid = row["instrument_id"]
        df_1d = _truncate(bars_by_id[iid], row)
        raw = compute_core_kernel_bundle(df_1d, None)
        hash_before = _df_hash(raw.bb_df)
        rec = asyncio.run(
            _run_postchange_single(df_1d, uuid.UUID(iid), date.fromisoformat(row["max_trade_date"]), raw.bb_df)
        )
        hash_after = _df_hash(raw.bb_df)
        rec["bb_df_immutable"] = hash_before == hash_after
        rec["instrument_id"] = iid
        rec["symbol"] = row.get("symbol")
        rec["bars_count"] = int(len(df_1d))
        rec["pass"] = rec["pass"] and rec["bb_df_immutable"]
        results.append(rec)
    return results


# =============================================================================
# main
# =============================================================================


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3.4B-3B BB #2 closure gate")
    ap.add_argument("--mode", choices=["dual", "bench", "postchange"], required=True)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--include-boundary", action="store_true")
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = _select_samples(args.count, args.include_boundary)
    print(f"mode={args.mode} samples={len(rows)}")
    bars_by_id = _load_bars()

    if args.mode == "bench":
        _run_bench(bars_by_id, rows)
        return

    if args.mode == "dual":
        results = _run_dual(bars_by_id, rows)
        n_pass = sum(1 for r in results if r["pass"])
        n = len(results)
        print(json.dumps({"total": n, "all_series_exact": f"{n_pass}/{n}"}, ensure_ascii=False, indent=2))
        (DUAL_RESULT_PATH).write_text(
            json.dumps({"mode": "dual", "total": n, "all_series_exact": f"{n_pass}/{n}",
                        "results": results}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if n_pass != n:
            for r in results:
                if not r["pass"]:
                    print("NON-PASS:", r.get("symbol"), r["fields"])
            sys.exit(1)
        print("ALL 105 FULL-SERIES EXACT — BB #2 EQUIVALENT_OUTPUT_SUBSET 确认，允许复用")
        return

    if args.mode == "postchange":
        results = _run_postchange(bars_by_id, rows)
        n_pass = sum(1 for r in results if r["pass"])
        n = len(results)
        reuse_zero = sum(1 for r in results if r["calls_reuse"] == 0)
        fallback_one = sum(1 for r in results if r["calls_fallback"] == 1)
        immut = sum(1 for r in results if r["bb_df_immutable"])
        same_bb = sum(1 for r in results if r["same_bb"])
        summary = {
            "total": n,
            "all_pass": f"{n_pass}/{n}",
            "extra_reuse_fallback_same": f"{same_bb}/{n}",
            "reuse_bb_exec_0": f"{reuse_zero}/{n}",
            "fallback_bb_exec_1": f"{fallback_one}/{n}",
            "bb_df_immutable": f"{immut}/{n}",
        }
        (OUTPUT_DIR / "postchange.json").write_text(
            json.dumps({"mode": "postchange", "summary": summary, "results": results},
                       ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if n_pass != n:
            for r in results:
                if not r["pass"]:
                    print("NON-PASS:", r.get("symbol"), {k: r.get(k) for k in
                          ("calls_reuse", "calls_fallback", "same_bb", "bb_df_immutable")})
            sys.exit(1)
        print("ALL PASS — review-core extra 复用 _shared_raw.bb_df，第二次 Bollinger execution 消失；fallback 仍调用 canonical")
        return


if __name__ == "__main__":
    main()
