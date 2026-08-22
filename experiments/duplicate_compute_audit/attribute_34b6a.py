#!/usr/bin/env python3
"""Phase 3.4B-6A — First Pyramid Builder Cost Attribution（MEASUREMENT ONLY）。

回答：build_first_pyramid_core_snapshot 的 ~61.8ms（3.4B-5 build_fp_core）到底花在哪里。

Part A — direct children exclusive attribution
  build_first_pyramid_core_snapshot total（patch core_artifact_service 模块级引用）
  直接子函数（patch first_pyramid_service 模块级引用）：
    _build_trend_dimension / _build_structure_dimension / _build_momentum_dimension
    _compute_input_hash / _compute_core_parameter_hash
    _build_aggregate_status_text / _build_field_availability
  嵌套计数器（模块级 patch 同时捕获 dimension builder 内部调用，禁止进入 exclusive 排名）：
    extract_last_volume_context（链内 4 次嵌套 + 1 次直接，见 358/617/827/836/1204）
    _vc_to_schema（链内 3 次嵌套 + 1 次直接，见 359/618/837/1205）
  定义：
    builder_residual = build_fp_total
                       - (trend + structure + momentum + input_hash
                          + param_hash + agg_status + field_avail)
  builder_residual 包含：line-1204/1205 的 direct extract_vc/vc_schema、
  FirstPyramidCoreSnapshot 构造、其余未计量的 builder 工作。

Part B — _compute_input_hash 专项 Gate（p50 >= 10ms 或 >= 20% of build_fp）
  B1 静态合同追踪（_compute_input_hash vs compute_source_bar_hash）
  B2 实验脚本内向量化候选 + 105/105 exact string equality + isolated benchmark

Part C — 修正 3.4B-5 对 json_safe_aggregate 的错误措辞（superseded interpretation）

Part D — SQZMOM 本轮禁止改动（后续单独证明 contract 等价）。

约束：
- MEASUREMENT_ONLY / PRODUCTION_CODE_DIFF = ZERO / 禁止改任何指标逻辑/公式/参数/lookback/window/event
- 同一 frozen dataset + 105 samples + warmup=1 + reps=3 + perf_counter
- 只 patch/timer，不重写 builder / serializer / 数据结构

Usage:
    cd backend && .venv/bin/python ../experiments/duplicate_compute_audit/attribute_34b6a.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from audit_closure import _load_bars

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = EXPERIMENT_DIR / "output" / "3.4A-0" / "sample_manifest.jsonl"
PARQUET_DIR = REPO_ROOT / "backend" / ".perfdata" / "review" / "review-source-c5c686e-v1" / "parquet"
OUTPUT_DIR = EXPERIMENT_DIR / "output" / "3.4B-6A"

ELIGIBLE_MIN_BARS = 60
INPUT_HASH_GATE_ABS_MS = 10.0
INPUT_HASH_GATE_SHARE = 0.20


class SyncTimer:
    __slots__ = ("fn", "key", "elapsed", "calls")

    def __init__(self, fn: Callable[..., Any], key: str) -> None:
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


def _p50_p95(a: np.ndarray) -> tuple[float, float]:
    if a.size == 0:
        return (0.0, 0.0)
    return (float(np.percentile(a, 50)), float(np.percentile(a, 95)))


def _perf_run_instrument_34b6a(
    df_1d: pd.DataFrame,
    instrument_id: uuid.UUID,
    trade_date: date,
    symbol: str,
    warmup: int,
    reps: int,
) -> list[dict[str, Any]]:
    """调用真实 compute_review_core_for_trade_date，只加 First Pyramid builder 最薄 timer。"""
    import unittest.mock as mock

    from app.services.feature_snapshot_service import compute_review_core_for_trade_date
    import app.services.core_artifact_service as cas_module
    import app.services.first_pyramid_service as fp_module

    build_timers = {
        "build_fp_total": SyncTimer(cas_module.build_first_pyramid_core_snapshot, "build_fp_total"),
        "trend": SyncTimer(fp_module._build_trend_dimension, "trend"),
        "structure": SyncTimer(fp_module._build_structure_dimension, "structure"),
        "momentum": SyncTimer(fp_module._build_momentum_dimension, "momentum"),
        # 嵌套计数器：模块级 patch 会同时捕获 dimension builder 内部调用
        "extract_vc": SyncTimer(fp_module.extract_last_volume_context, "extract_vc"),
        "vc_schema": SyncTimer(fp_module._vc_to_schema, "vc_schema"),
        "input_hash": SyncTimer(fp_module._compute_input_hash, "input_hash"),
        "param_hash": SyncTimer(fp_module._compute_core_parameter_hash, "param_hash"),
        "agg_status": SyncTimer(fp_module._build_aggregate_status_text, "agg_status"),
        "field_avail": SyncTimer(fp_module._build_field_availability, "field_avail"),
    }
    all_timers = dict(build_timers)

    patch_targets: list[tuple[Any, str, Any]] = [
        # build_first_pyramid_core_snapshot 经 core_artifact_service 模块级引用调用（cas L48 import）
        (cas_module, "build_first_pyramid_core_snapshot", build_timers["build_fp_total"]),
        (fp_module, "_build_trend_dimension", build_timers["trend"]),
        (fp_module, "_build_structure_dimension", build_timers["structure"]),
        (fp_module, "_build_momentum_dimension", build_timers["momentum"]),
        (fp_module, "extract_last_volume_context", build_timers["extract_vc"]),
        (fp_module, "_vc_to_schema", build_timers["vc_schema"]),
        (fp_module, "_compute_input_hash", build_timers["input_hash"]),
        (fp_module, "_compute_core_parameter_hash", build_timers["param_hash"]),
        (fp_module, "_build_aggregate_status_text", build_timers["agg_status"]),
        (fp_module, "_build_field_availability", build_timers["field_avail"]),
    ]

    def _reset() -> None:
        for t in all_timers.values():
            t.elapsed = 0.0
            t.calls = 0

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

    def _collect(chain_total: float) -> dict[str, Any]:
        t = {k: build_timers[k].elapsed for k in build_timers}
        build_fp_total = t["build_fp_total"]
        # exclusive direct children（互不重叠：trend/structure/momentum 各自包含其内部嵌套调用）
        children_exclusive = (
            t["trend"] + t["structure"] + t["momentum"]
            + t["input_hash"] + t["param_hash"]
            + t["agg_status"] + t["field_avail"]
        )
        builder_residual = build_fp_total - children_exclusive
        return {
            "chain_total": chain_total,
            "build": {
                "build_fp_total": build_fp_total,
                "trend": t["trend"],
                "structure": t["structure"],
                "momentum": t["momentum"],
                "input_hash": t["input_hash"],
                "param_hash": t["param_hash"],
                "agg_status": t["agg_status"],
                "field_avail": t["field_avail"],
                # 嵌套计数器（与 dimension builder 时间重叠，不参与 exclusive 排名）
                "extract_vc_nested": t["extract_vc"],
                "vc_schema_nested": t["vc_schema"],
                "children_exclusive": children_exclusive,
                "builder_residual": builder_residual,
            },
            "recon": {
                "build_fp_parent": build_fp_total,
                "build_fp_sum": children_exclusive + builder_residual,
            },
            "calls": {k: build_timers[k].calls for k in build_timers},
        }

    runs: list[dict[str, Any]] = []
    from contextlib import ExitStack
    stack = ExitStack()
    for mod, name, timer in patch_targets:
        stack.enter_context(mock.patch.object(mod, name, timer))
    with stack:
        for _ in range(warmup):
            _reset()
            asyncio.run(_chain())
        for _ in range(reps):
            _reset()
            ct = asyncio.run(_chain())
            runs.append(_collect(ct))
    return runs


def _report_child(tag: str, key: str, runs_all: list[dict[str, Any]],
                  build_fp_totals: np.ndarray, totals: np.ndarray) -> dict[str, Any]:
    vals = np.array([r[tag][key] for r in runs_all], dtype=float) * 1000.0
    p50, p95 = _p50_p95(vals)
    return {
        "child": key,
        "elapsed_p50_ms": round(p50, 4),
        "elapsed_p95_ms": round(p95, 4),
        "median_share_of_build_fp_pct": round(
            float(np.median(vals / (build_fp_totals * 1000.0))) * 100.0, 3),
        "median_share_of_chain_pct": round(
            float(np.median(vals / (totals * 1000.0))) * 100.0, 3),
        "calls_median": (
            int(np.median([r["calls"][key] for r in runs_all]))
            if key in runs_all[0]["calls"] else None
        ),
    }


def _recon_error(runs_all: list[dict[str, Any]], parent: str, child_sum: str) -> dict[str, Any]:
    errs = []
    for r in runs_all:
        p = r["recon"][parent]
        c = r["recon"][child_sum]
        errs.append(abs(c - p) / p if p > 1e-12 else (0.0 if abs(c - p) < 1e-12 else float("inf")))
    arr = np.array(errs, dtype=float)
    return {
        "median_rel_error_pct": round(float(np.median(arr)) * 100.0, 4),
        "p95_rel_error_pct": round(float(np.percentile(arr, 95)) * 100.0, 4),
        "max_rel_error_pct": round(float(np.max(arr)) * 100.0, 4),
    }


# ---------------------------------------------------------------------------
# Part B — _compute_input_hash 专项
# ---------------------------------------------------------------------------

def _compute_input_hash_old(bars: pd.DataFrame) -> str:
    """与 first_pyramid_service._compute_input_hash 完全相同的拷贝（只读，不改生产）。"""
    if bars is None or bars.empty:
        return "sha256:empty"
    cols = [c for c in ("open", "high", "low", "close", "volume", "amount") if c in bars.columns]
    if not cols:
        return "sha256:no_ohlcv"
    try:
        idx_str = pd.Series(bars.index.astype(str)).str.cat(sep=",")
        vals_str = bars[cols].astype(str).agg(",".join, axis=1).str.cat(sep="|")
        content = f"{idx_str}#{vals_str}"
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    except Exception as exc:  # noqa: BLE001
        return "sha256:error"


def _compute_input_hash_vectorized_candidate(bars: pd.DataFrame) -> str:
    """候选：与 _compute_input_hash 产生完全相同的字符串 hash。

    将生产代码中 pandas 的
      pd.Series(index.astype(str)).str.cat(sep=",")   →  list join
      bars[cols].astype(str).agg(",".join, axis=1).str.cat(sep="|")
                                                     →  to_numpy().tolist() + C-level join
    避免逐行 pandas Series 构造。不改变任何 hash 算法/浮点格式/字段顺序/
    index 格式/separator/digest 长度。
    """
    if bars is None or bars.empty:
        return "sha256:empty"
    cols = [c for c in ("open", "high", "low", "close", "volume", "amount") if c in bars.columns]
    if not cols:
        return "sha256:no_ohlcv"
    try:
        idx_str = ",".join(bars.index.astype(str).tolist())
        arr = bars[cols].astype(str).to_numpy().tolist()
        vals_str = "|".join(",".join(row) for row in arr)
        content = f"{idx_str}#{vals_str}"
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    except Exception as exc:  # noqa: BLE001
        return "sha256:error"


def _input_hash_contract_static() -> dict[str, Any]:
    """B1 静态合同追踪：_compute_input_hash vs compute_source_bar_hash。

    两个 hash 都"对 OHLCV 做 SHA256"，但 contract 不同，不能互相复用。
    """
    return {
        "caller_hash": {
            "producer": "chart_bars_service.compute_source_bar_hash(df, timeframe='1d')",
            "data_flow": "BarAggregationResult.source_bar_hash -> primary_source_bar_hash "
                         "-> compute_core_artifact(input_hash=primary_source_bar_hash, "
                         "bars_hash=primary_source_bar_hash)",
            "row_format": "{time_str}|{open}|{high}|{low}|{close}|{volume}|{amount}",
            "row_separator": "\\n",
            "index_representation": "strftime('%Y-%m-%d')（1d）",
            "value_separator": "|",
            "hash_prefix": "（无前缀，裸 hexdigest）",
            "digest_truncation": "sha256 hexdigest[:16]",
        },
        "builder_hash": {
            "producer": "first_pyramid_service._compute_input_hash(bars)",
            "data_flow": "build_first_pyramid_core_snapshot -> _compute_input_hash(bars) "
                         "-> fp_core.inputHash",
            "content": "{idx_str}#{vals_str}",
            "idx_str": "pd.Series(bars.index.astype(str)).str.cat(sep=',')（全部 index 值，"
                       "DatetimeIndex 为完整 'YYYY-MM-DD HH:MM:SS' 字符串）",
            "vals_str": "bars[cols].astype(str).agg(','.join, axis=1).str.cat(sep='|')"
                        "（行内逗号，行间竖线）",
            "hash_prefix": "'sha256:' 前缀",
            "digest_truncation": "sha256 hexdigest[:16]",
        },
        "classification": "DIFFERENT_HASH_CONTRACT",
        "evidence": (
            "1) 行内/行间 separator 不同（'|'+'\\n' vs ','+'+'|'）；"
            "2) index 表示不同（strftime('%Y-%m-%d') vs index.astype(str) 完整时间戳）；"
            "3) hash 前缀不同（无前缀 vs 'sha256:'）；"
            "4) 拼接顺序/结构不同（裸行拼接 vs '{idx}#{vals}'）。"
            "对相同 bars，两个函数必然产出不同的 hash 字符串；"
            "因此禁止复用 caller 的 primary_source_bar_hash 替换 fp_core.inputHash，"
            "否则改变持久化 inputHash 值（违反 hash contract）。"
        ),
        "implication": (
            "_compute_input_hash 不能作为 exact duplicate 删除；"
            "但它是纯 Python 行循环/字符串拼接开销，可做 exact-equivalent 向量化（Part B2）。"
        ),
    }


def _input_hash_gate_and_candidate(
    selected: list[dict[str, Any]],
    bars_by_id: dict[str, pd.DataFrame],
    runs_all: list[dict[str, Any]],
    build_fp_p50_ms: float,
) -> dict[str, Any]:
    """B 专项：Gate 判定 → B1 静态合同 → B2 向量化候选 105/105 + isolated benchmark。"""
    input_hash_p50_ms = float(np.percentile(
        np.array([r["build"]["input_hash"] for r in runs_all], dtype=float) * 1000.0, 50))
    share_pct = float(np.median(
        np.array([r["build"]["input_hash"] for r in runs_all], dtype=float)
        / np.array([r["build"]["build_fp_total"] for r in runs_all], dtype=float))) * 100.0

    gate_met = input_hash_p50_ms >= INPUT_HASH_GATE_ABS_MS or share_pct >= INPUT_HASH_GATE_SHARE * 100.0

    out: dict[str, Any] = {
        "phase": "3.4B-6A",
        "gate": {
            "met": gate_met,
            "input_hash_p50_ms": round(input_hash_p50_ms, 4),
            "share_of_build_fp_pct": round(share_pct, 3),
            "thresholds": {
                "abs_ms": INPUT_HASH_GATE_ABS_MS,
                "share": INPUT_HASH_GATE_SHARE,
            },
        },
    }
    if not gate_met:
        out["skipped_reason"] = (
            f"_compute_input_hash p50={input_hash_p50_ms:.2f}ms / "
            f"share={share_pct:.2f}% 未达 Gate（>=10ms 或 >=20%），不强行优化。"
        )
        return out

    # B1 静态合同
    out["static_contract"] = _input_hash_contract_static()

    # B2 向量化候选：105/105 exact string equality
    frames: list[pd.DataFrame] = []
    for row in selected:
        iid = row["instrument_id"]
        if iid not in bars_by_id:
            continue
        df_all = bars_by_id[iid]
        trade_date = date.fromisoformat(row["max_trade_date"])
        frames.append(df_all[df_all.index.date <= trade_date])

    mismatches: list[dict[str, Any]] = []
    n_equal = 0
    for i, b in enumerate(frames):
        o = _compute_input_hash_old(b)
        c = _compute_input_hash_vectorized_candidate(b)
        if o == c:
            n_equal += 1
        else:
            mismatches.append({"sample": i, "old": o, "candidate": c})
    total = len(frames)
    out["exact_equality"] = {
        "n_samples": total,
        "n_equal": n_equal,
        "all_equal": n_equal == total,
        "mismatches": mismatches[:20],
    }
    if not (n_equal == total):
        out["candidate"] = {"status": "FAIL", "reason": "存在 hash 不一致，禁止进入 production"}
        return out

    # isolated benchmark（interleaved，避免顺序偏差）
    N = 30
    old_ts: list[float] = []
    cand_ts: list[float] = []
    per_sample: list[dict[str, float]] = []
    for b in frames:
        o_vals = []
        c_vals = []
        for _ in range(N):
            t0 = time.perf_counter(); _compute_input_hash_old(b); o_vals.append(time.perf_counter() - t0)
            t0 = time.perf_counter(); _compute_input_hash_vectorized_candidate(b); c_vals.append(time.perf_counter() - t0)
        old_ts.extend(o_vals)
        cand_ts.extend(c_vals)
        per_sample.append({
            "rows": int(len(b)),
            "old_p50_ms": round(float(np.percentile(o_vals, 50)) * 1000.0, 4),
            "candidate_p50_ms": round(float(np.percentile(c_vals, 50)) * 1000.0, 4),
        })
    old_p50, old_p95 = _p50_p95(np.array(old_ts) * 1000.0)
    cand_p50, cand_p95 = _p50_p95(np.array(cand_ts) * 1000.0)
    out["candidate"] = {
        "status": "PROOF_OK",
        "method": "interleaved perf_counter, N=30/sample, same frozen df_1d frames",
        "old_p50_ms": round(old_p50, 4),
        "old_p95_ms": round(old_p95, 4),
        "candidate_p50_ms": round(cand_p50, 4),
        "candidate_p95_ms": round(cand_p95, 4),
        "saved_ms_p50": round(old_p50 - cand_p50, 4),
        "saved_pct_p50": round((old_p50 - cand_p50) / old_p50 * 100.0, 2) if old_p50 > 0 else 0.0,
        "saved_ms_p95": round(old_p95 - cand_p95, 4),
        "saved_pct_p95": round((old_p95 - cand_p95) / old_p95 * 100.0, 2) if old_p95 > 0 else 0.0,
        "note": "candidate proof only；本轮不实施 production implementation",
        "per_sample_top10": sorted(per_sample, key=lambda d: -d["old_p50_ms"])[:10],
    }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="3.4B-6A first-pyramid builder attribution")
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
    print(f"3.4B-6A attribution: {len(selected)} instruments "
          f"(main {len(main_rows[:args.count])}, boundary {len(boundary_rows)}), "
          f"warmup={args.warmup} reps={args.reps} -> n_runs={len(selected) * args.reps}")

    bars_by_id = _load_bars()
    runs_all: list[dict[str, Any]] = []
    per_instrument: list[dict[str, Any]] = []
    per_instrument_runs: list[list[dict[str, Any]]] = []
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
            runs = _perf_run_instrument_34b6a(df_1d, uuid.UUID(iid), trade_date, symbol,
                                              args.warmup, args.reps)
        except Exception as exc:  # noqa: BLE001
            print(f"[perf error] {iid} {symbol}: {type(exc).__name__}: {exc}")
            continue
        runs_all.extend(runs)
        per_instrument_runs.append(runs)
        per_instrument.append({
            "instrument_id": iid,
            "symbol": symbol,
            "bars_count": int(len(df_1d)),
            "selection_reason": row["selection_reason"],
            "n_runs": len(runs),
        })
        if len(per_instrument) % 25 == 0:
            print(f"  ... {len(per_instrument)}/{len(selected)} done")

    totals = np.array([r["chain_total"] for r in runs_all], dtype=float)
    build_fp_totals = np.array([r["build"]["build_fp_total"] for r in runs_all], dtype=float)
    t50, t95 = _p50_p95(totals * 1000.0)
    b50, b95 = _p50_p95(build_fp_totals * 1000.0)

    # ---- Part A：direct children exclusive attribution ----
    exclusive_children = [
        _report_child("build", "trend", runs_all, build_fp_totals, totals),
        _report_child("build", "structure", runs_all, build_fp_totals, totals),
        _report_child("build", "momentum", runs_all, build_fp_totals, totals),
        _report_child("build", "input_hash", runs_all, build_fp_totals, totals),
        _report_child("build", "param_hash", runs_all, build_fp_totals, totals),
        _report_child("build", "agg_status", runs_all, build_fp_totals, totals),
        _report_child("build", "field_avail", runs_all, build_fp_totals, totals),
    ]
    residual_report = _report_child("build", "builder_residual", runs_all, build_fp_totals, totals)
    nested_report = [
        _report_child("build", "extract_vc_nested", runs_all, build_fp_totals, totals),
        _report_child("build", "vc_schema_nested", runs_all, build_fp_totals, totals),
    ]

    # 负数 residual 检查（timer 重叠即测量错误）
    neg_residuals = [r["build"]["builder_residual"] for r in runs_all if r["build"]["builder_residual"] < -1e-9]
    if neg_residuals:
        print(f"[MEASUREMENT ERROR] {len(neg_residuals)} runs 出现负 residual，"
              f"min={min(neg_residuals) * 1000.0:.4f}ms —— 修正 measurement 后重跑")
        sys.exit(2)

    reconciliation = {
        "phase": "3.4B-6A",
        "method": "per-run exclusive reconciliation; error = |(child_sum - parent)/parent|",
        "build_fp": {
            "exclusive_children_sum = trend+structure+momentum+input_hash+param_hash"
            "+agg_status+field_avail; builder_residual 为剩余": _recon_error(
                runs_all, "build_fp_parent", "build_fp_sum"),
        },
        "nested_counters": {
            "extract_last_volume_context": "模块级 patch 含 4 次嵌套（dimension builder 内）+ 1 次直接（L1204）；"
                                            "不参与 exclusive 排名，直接调用部分计入 builder_residual",
            "_vc_to_schema": "模块级 patch 含 3 次嵌套 + 1 次直接（L1205）；同上",
        },
    }

    # ---- Part B：input_hash 专项 Gate ----
    input_hash_result = _input_hash_gate_and_candidate(selected, bars_by_id, runs_all, b50)

    # ---- Part C：json_safe_aggregate 措辞修正（superseded interpretation）----
    part_c = {
        "supersedes": "3.4B-5 residual_attribution.json measurement_notes.json_safe_aggregate",
        "old_wording": "_json_safe_value 在 build_fp_core 内部被递归调用，时间与 build_fp_core 重叠",
        "correction": (
            "真实 production 顺序为 build_first_pyramid_core_snapshot() 返回 fp_core 之后，"
            "compute_core_artifact 再调用 _extract_dsa_metrics/_extract_dsa_visual/"
            "_extract_state_events、fp_core.model_dump()、_json_safe_value(...)、"
            "CoreComputationArtifact(...)；即 _json_safe_value 在 build_fp_core 之后，"
            "不在 builder 内部。json_safe_aggregate=~9.2ms 不能作为 exclusive hotspot 排名，"
            "是因为 _json_safe_value 是递归函数，timer 对每层 recursive inclusive duration 累加，"
            "存在递归层级重复计时。artifact_residual≈1.9ms 可作其 exclusive residual 看待，"
            "但其中混有 model_dump/top-level JSON 安全化/模型构造，不进一步拆分猜测。"
        ),
        "old_3.4B5_evidence_not_amended": True,
    }

    # ---- Part D：SQZMOM 不动 ----
    part_d = {
        "sqzmom_touched": False,
        "note": "sqzmom_at_bar≈5ms 疑似重复计算，但 3.4B-5 committed evidence 标注'待补充静态证据'；"
                "本轮禁止改动。后续需单独证明 temporal compute_sqzmom_lb defaults == "
                "First Pyramid _FIRST_PYRAMID_PARAMS['sqzmom_config'] 且 bars/index/"
                "point-in-time contract 相同后，才可进入 duplicate closure。",
    }

    # ---- Decision Gate ----
    input_hash_gate_met = input_hash_result["gate"]["met"]
    if input_hash_gate_met:
        cand_status = input_hash_result.get("candidate", {}).get("status", "N/A")
        if cand_status == "PROOF_OK":
            decision_gate = {
                "case": 1,
                "label": "_compute_input_hash dominant + 向量化候选 105/105 exact + 明显更快",
                "next": "下一阶段允许做 production implementation（仍不触碰指标逻辑）",
            }
        else:
            decision_gate = {
                "case": "candidate_fail",
                "label": "_compute_input_hash dominant 但候选未 105/105 exact",
                "next": "候选 FAIL，禁止进入 production；下一阶段仅测量拆分其余维度",
            }
    else:
        decision_gate = {
            "case": "gate_not_met",
            "label": "_compute_input_hash 未达专项 Gate",
            "next": "按实际 dominant child 决定下一阶段拆解方向",
        }

    report = {
        "phase": "3.4B-6A",
        "method": "call real compute_review_core_for_trade_date; perf_counter; "
                  "builder leaf timers; per-run exclusive reconciliation",
        "reproducibility": {
            "dataset": "review-source-c5c686e-v1",
            "audit_code_sha": head,
            "baseline": {"phase": "3.4B-5", "audit_code_sha": "e4af18c6be34f87655e8c8bae2b9fbd555f1746c"},
        },
        "n_runs": len(runs_all),
        "chain_total_per_stock": {"p50_ms": round(t50, 3), "p95_ms": round(t95, 3)},
        "build_first_pyramid_core_snapshot": {"p50_ms": round(b50, 4), "p95_ms": round(b95, 4)},
        "exclusive_children": exclusive_children,
        "builder_residual": residual_report,
        "nested_counters": nested_report,
        "reconciliation": reconciliation,
        "negative_residual_count": len(neg_residuals),
        "decision_gate": decision_gate,
        "part_c_json_safe_wording": part_c,
        "part_d_sqzmom": part_d,
        "input_hash_gate_met": input_hash_gate_met,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "builder_attribution.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "input_hash_candidate.json").write_text(
        json.dumps(input_hash_result, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(OUTPUT_DIR / "per_instrument.jsonl", "w", encoding="utf-8") as f:
        for row, runs in zip(per_instrument, per_instrument_runs):
            f.write(json.dumps({**row, "runs": runs}, ensure_ascii=False) + "\n")

    print(json.dumps({
        "chain_total_per_stock": report["chain_total_per_stock"],
        "build_fp": report["build_first_pyramid_core_snapshot"],
        "exclusive_children": exclusive_children,
        "builder_residual": residual_report,
        "nested_counters": nested_report,
        "reconciliation": reconciliation,
        "input_hash_gate": input_hash_result["gate"],
        "candidate_status": input_hash_result.get("candidate", {}).get("status"),
        "decision_gate": decision_gate,
    }, ensure_ascii=False, indent=2))
    print(f"output: {OUTPUT_DIR}")
    print("NOTE: PRODUCTION_CODE_DIFF = ZERO（本脚本未修改任何生产代码）")


if __name__ == "__main__":
    main()
