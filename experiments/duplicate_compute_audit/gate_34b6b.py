#!/usr/bin/env python3
"""Phase 3.4B-6B — Exact Input Hash Assembly Optimization Gates。

本轮允许的**唯一** production 修改：`first_pyramid_service._compute_input_hash` 的
执行方式（pandas axis=1 → list/C-level join），hash contract 完全不变。

Gate 1 — Hash contract parity
  105 frozen samples + edge-contract cases，old_reference == production_new，字符串完全相等。
Gate 2 — Builder output parity
  raw kernels 每股只算一次（完整 chain 捕获）并复用；old/new `_compute_input_hash`
  分别消费下跑 `build_first_pyramid_core_snapshot`，比较 `FirstPyramidCoreSnapshot.model_dump()`
  105/105 exact。
Gate 3 — modified-scope PURE_UNIT regression（单独 pytest 调用，本脚本不跑）
Gate 4 — Performance
  同 6A harness（105 samples / warmup=1 / reps=3 / perf_counter）测 production-new 下
  input_hash in-situ 与 build_fp_total 的 p50/p95，对比 6A 的 old 基线。

HARD FREEZE：
  None/empty → sha256:empty；no OHLCV cols → sha256:no_ohlcv；exception → sha256:error；
  columns order → open/high/low/close/volume/amount；index.astype(str)；分隔符；
  hash 前缀；SHA256；hexdigest[:16]；logger 行为；全部保持不变。
  不改 structure dimension / SQZMOM / 不做任何顺手重构。

Usage:
    cd backend && .venv/bin/python ../experiments/duplicate_compute_audit/gate_34b6b.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from audit_closure import _load_bars

EXPERIMENT_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = EXPERIMENT_DIR / "output" / "3.4A-0" / "sample_manifest.jsonl"
OUTPUT_DIR = EXPERIMENT_DIR / "output" / "3.4B-6B"

HASH_COL_ORDER = ("open", "high", "low", "close", "volume", "amount")


def _compute_input_hash_old(bars: pd.DataFrame) -> str:
    """生产代码修改前的原实现（pandas axis=1），作为 parity old-reference。"""
    if bars is None or bars.empty:
        return "sha256:empty"
    cols = [c for c in HASH_COL_ORDER if c in bars.columns]
    if not cols:
        return "sha256:no_ohlcv"
    try:
        idx_str = pd.Series(bars.index.astype(str)).str.cat(sep=",")
        vals_str = bars[cols].astype(str).agg(",".join, axis=1).str.cat(sep="|")
        content = f"{idx_str}#{vals_str}"
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return "sha256:error"


def _p50_p95(a: np.ndarray) -> tuple[float, float]:
    if a.size == 0:
        return (0.0, 0.0)
    return (float(np.percentile(a, 50)), float(np.percentile(a, 95)))


def _edge_cases() -> list[dict[str, Any]]:
    """edge-contract cases：old/new 必须 exact 相等。"""
    idx_dup = pd.DatetimeIndex(
        ["2024-01-01", "2024-01-02", "2024-01-02", "2024-01-03", "2024-01-04"],
        name="trade_date",
    )
    frames: dict[str, pd.DataFrame] = {}
    frames["empty"] = pd.DataFrame(
        {"open": pd.Series(dtype=float), "high": pd.Series(dtype=float)}
    )
    frames["no_ohlcv"] = pd.DataFrame({"foo": [1.0, 2.0], "bar": [3.0, 4.0]})
    frames["missing_amount"] = pd.DataFrame(
        {
            "open": [1.0, 2.0, 3.0],
            "high": [4.0, 5.0, 6.0],
            "low": [0.5, 1.0, 2.0],
            "close": [3.0, 4.0, 5.0],
            "volume": [100.0, 200.0, 300.0],
        },
        index=pd.DatetimeIndex(["2024-01-01", "2024-01-02", "2024-01-03"]),
    )
    frames["nan_inf_zero_neg"] = pd.DataFrame(
        {
            "open": [np.nan, 0.0, -5.0, 1.0],
            "high": [np.inf, 1.0, 2.0, 3.0],
            "low": [-np.inf, 0.0, 1.0, 2.0],
            "close": [np.nan, 0.0, -1.0, 4.0],
            "volume": [0.0, np.nan, -9.0, 100.0],
            "amount": [np.nan, 3.0, -7.0, 999.0],
        },
        index=pd.DatetimeIndex(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]),
    )
    frames["scientific_float"] = pd.DataFrame(
        {
            "open": [1e-7, 1.23e15, 5.5e-10, 0.0],
            "high": [9.99e-8, 2.5e14, 1.0e-12, 1.0],
            "low": [3.3e-9, 1e-5, 7.7e-11, -0.0],
            "close": [4.44e-7, 6.0e13, 8.8e-13, 0.1],
            "volume": [0.0, 1e6, 1e-6, 2e7],
            "amount": [1.2e-5, 3.3e9, 1e-9, 4e2],
        },
        index=pd.DatetimeIndex(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]),
    )
    frames["duplicate_datetime_index"] = pd.DataFrame(
        {
            "open": [1.0, 2.0, 3.0, 4.0, 5.0],
            "high": [2.0, 3.0, 4.0, 5.0, 6.0],
            "low": [0.5, 1.0, 2.0, 3.0, 4.0],
            "close": [1.5, 2.5, 3.5, 4.5, 5.5],
            "volume": [100.0, 110.0, 120.0, 130.0, 140.0],
            "amount": [1e4, 1.1e4, 1.2e4, 1.3e4, 1.4e4],
        },
        index=idx_dup,
    )
    out = []
    for name, df in frames.items():
        out.append({"case": name, "n_rows": int(len(df)), "frame": df})
    return out


class SyncTimer:
    __slots__ = ("fn", "key", "elapsed", "calls")

    def __init__(self, fn: Any, key: str) -> None:
        self.fn = fn
        self.key = key
        self.elapsed = 0.0
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return self.fn(*args, **kwargs)
        finally:
            self.elapsed += time.perf_counter() - t0
            self.calls += 1


def _run_instrument_gates(
    df_1d: pd.DataFrame,
    instrument_id: uuid.UUID,
    trade_date: date,
    symbol: str,
    new_hash_fn: Any,
    old_hash_fn: Any,
    warmup: int,
    reps: int,
) -> dict[str, Any]:
    """单个 instrument：Gate2（builder parity）+ Gate4（in-situ timing）。

    用完整真实 chain 捕获 raw（warmup run），再离线用 old/new input_hash 各跑一次
    build_first_pyramid_core_snapshot 比较 model_dump。
    """
    import unittest.mock as mock

    from app.services.feature_snapshot_service import compute_review_core_for_trade_date
    import app.services.core_artifact_service as cas_module
    import app.services.first_pyramid_service as fp_module

    orig_build = cas_module.build_first_pyramid_core_snapshot
    captured: dict[str, Any] = {}

    build_total = SyncTimer(orig_build, "build_fp_total")
    input_hash_in_situ = SyncTimer(fp_module._compute_input_hash, "input_hash_in_situ")

    def _capture_build(**kw: Any) -> Any:
        if not captured:
            captured["raw"] = dict(kw)
        return build_total(**kw)

    chain_total = None
    last_build = 0.0
    last_hash = 0.0

    async def _chain() -> float:
        t0 = time.perf_counter()
        await compute_review_core_for_trade_date(
            None,
            instrument_id,
            trade_date,
            primary_timeframe="1d",
            adj="qfq",
            primary_bars=df_1d,
            primary_source_bar_hash="perf00",
            primary_adj_factor_hash="perf00",
            source_run_id=None,
            instrument_symbol=symbol,
        )
        return time.perf_counter() - t0

    patch_targets = [
        (cas_module, "build_first_pyramid_core_snapshot", _capture_build),
        (fp_module, "_compute_input_hash", input_hash_in_situ),
    ]
    with mock.patch.object(cas_module, "build_first_pyramid_core_snapshot", _capture_build), \
         mock.patch.object(fp_module, "_compute_input_hash", input_hash_in_situ):
        for _ in range(warmup):
            build_total.elapsed = 0.0
            input_hash_in_situ.elapsed = 0.0
            asyncio.run(_chain())
        for _ in range(reps):
            build_total.elapsed = 0.0
            input_hash_in_situ.elapsed = 0.0
            chain_total = asyncio.run(_chain())
            last_build = build_total.elapsed
            last_hash = input_hash_in_situ.elapsed

    # ---- Gate 2：离线段用同一份 raw，old/new input_hash 各 build 一次 ----
    raw = dict(captured["raw"])
    saved_new = fp_module._compute_input_hash
    try:
        fp_module._compute_input_hash = old_hash_fn
        dump_old = orig_build(**raw).model_dump()
        fp_module._compute_input_hash = new_hash_fn
        dump_new = orig_build(**raw).model_dump()
    finally:
        fp_module._compute_input_hash = saved_new

    return {
        "gate2_equal": dump_new == dump_old,
        "chain_total": chain_total,
        "build_fp_total": last_build,
        "input_hash_in_situ": last_hash,
    }


def main() -> None:
    import subprocess

    ap = argparse.ArgumentParser(description="3.4B-6B input-hash optimization gates")
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args()

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    import app.services.first_pyramid_service as fp_module
    new_hash_fn = fp_module._compute_input_hash

    # ---- selected samples（与 6A/FINAL 同口径：main_ge250[:100] + boundary_60_249）----
    rows = [json.loads(line) for line in open(MANIFEST_PATH, encoding="utf-8")]
    main_rows = [r for r in rows if r["selection_reason"] == "main_ge250"]
    boundary_rows = [r for r in rows if r["selection_reason"] == "boundary_60_249"]
    selected = main_rows[: args.count] + boundary_rows
    print(f"3.4B-6B gates: {len(selected)} instruments, "
          f"warmup={args.warmup} reps={args.reps} -> n_runs={len(selected) * args.reps}")

    bars_by_id = _load_bars()
    frames: list[pd.DataFrame] = []
    for row in selected:
        df_all = bars_by_id[row["instrument_id"]]
        td = date.fromisoformat(row["max_trade_date"])
        frames.append(df_all[df_all.index.date <= td])

    # ---- Gate 1：hash contract parity（105 frozen samples）----
    n_eq = 0
    mismatches: list[dict[str, Any]] = []
    for i, b in enumerate(frames):
        o = _compute_input_hash_old(b)
        n = new_hash_fn(b)
        if o == n:
            n_eq += 1
        else:
            mismatches.append({"sample": i, "old": o, "new": n})
    gate1_frozen = {
        "n_samples": len(frames),
        "n_equal": n_eq,
        "all_equal": n_eq == len(frames),
        "mismatches": mismatches[:20],
    }

    # Gate 1 edge cases
    edge_results = []
    for ec in _edge_cases():
        b = ec["frame"]
        o = _compute_input_hash_old(b)
        n = new_hash_fn(b)
        edge_results.append({
            "case": ec["case"],
            "n_rows": ec["n_rows"],
            "old": o,
            "new": n,
            "equal": o == n,
        })
    gate1_edges = {
        "n_cases": len(edge_results),
        "all_equal": all(r["equal"] for r in edge_results),
        "cases": edge_results,
    }

    # ---- Gate 2 + Gate 4 ----  (每 instrument 完整 chain + 离线 build)
    per_instrument = []
    gate2_equal_count = 0
    chain_totals: list[float] = []
    build_fp: list[float] = []
    hash_in_situ: list[float] = []
    for idx, (row, b) in enumerate(zip(selected, frames)):
        iid = row["instrument_id"]
        td = date.fromisoformat(row["max_trade_date"])
        try:
            res = _run_instrument_gates(
                b, uuid.UUID(iid), td, row.get("symbol") or "600000",
                new_hash_fn, _compute_input_hash_old, args.warmup, args.reps,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[gate error] {iid} {row.get('symbol')}: {type(exc).__name__}: {exc}")
            continue
        per_instrument.append({
            "instrument_id": iid,
            "symbol": row.get("symbol"),
            "bars_count": int(len(b)),
            "gate2_equal": res["gate2_equal"],
            "chain_total": res["chain_total"],
            "build_fp_total": res["build_fp_total"],
            "input_hash_in_situ": res["input_hash_in_situ"],
        })
        if res["gate2_equal"]:
            gate2_equal_count += 1
        chain_totals.append(res["chain_total"])
        build_fp.append(res["build_fp_total"])
        hash_in_situ.append(res["input_hash_in_situ"])
        if len(per_instrument) % 25 == 0:
            print(f"  ... {len(per_instrument)}/{len(selected)} done")

    gate2 = {
        "n_instruments": len(per_instrument),
        "n_equal": gate2_equal_count,
        "all_equal": gate2_equal_count == len(per_instrument),
    }

    bt50, bt95 = _p50_p95(np.array(build_fp) * 1000.0)
    hi50, hi95 = _p50_p95(np.array(hash_in_situ) * 1000.0)
    ct50, ct95 = _p50_p95(np.array(chain_totals) * 1000.0)
    # 6A 基线（production-old）：build_fp_total p50=60.0959, input_hash p50=20.15
    gate4 = {
        "baseline_6A_old": {"build_fp_p50_ms": 60.0959, "input_hash_p50_ms": 20.1485},
        "production_new": {
            "build_fp_p50_ms": round(bt50, 4),
            "build_fp_p95_ms": round(bt95, 4),
            "input_hash_in_situ_p50_ms": round(hi50, 4),
            "input_hash_in_situ_p95_ms": round(hi95, 4),
            "chain_total_p50_ms": round(ct50, 4),
            "chain_total_p95_ms": round(ct95, 4),
        },
    }

    summary = {
        "phase": "3.4B-6B",
        "audit_code_sha": head,
        "method": "call real compute_review_core_for_trade_date; perf_counter; "
                  "old-reference vs production-new parity; raw reuse offline build",
        "gate1_hash_contract_parity": {"frozen": gate1_frozen, "edge_cases": gate1_edges},
        "gate2_builder_output_parity": gate2,
        "gate4_performance": gate4,
    }
    (OUTPUT_DIR / "gate_34b6b_results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(OUTPUT_DIR / "per_instrument.jsonl", "w", encoding="utf-8") as f:
        for p in per_instrument:
            f.write(json.dumps(p) + "\n")

    print("\n=== Gate 1 frozen: all_equal =", gate1_frozen["all_equal"],
          f"({gate1_frozen['n_equal']}/{gate1_frozen['n_samples']})")
    print("=== Gate 1 edge:   all_equal =", gate1_edges["all_equal"])
    print("=== Gate 2: all_equal =", gate2["all_equal"],
          f"({gate2['n_equal']}/{gate2['n_instruments']})")
    print(f"=== Gate 4: build_fp p50={gate4['production_new']['build_fp_p50_ms']}ms "
          f"(6A old 60.0959ms); input_hash in-situ p50={hi50:.4f}ms "
          f"(6A old 20.1485ms); chain p50={ct50:.4f}ms")


if __name__ == "__main__":
    main()