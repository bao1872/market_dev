#!/usr/bin/env python3
"""Phase 3.4B-5 — Residual Hotspot Attribution（MEASUREMENT ONLY）。

在 3.4B-4 全链复测基础上，把剩余两个大桶拆到函数级 leaf：

Part A — Temporal daily-context bucket（3.4B-4 的 "MACD / daily_context / derived"）
  _compute_macd_state total / compute_macd_adapter kernel / _compute_daily_context total
  _collect_historical_segment_ages total
  temporal_feature_service.compute_dsa_bundle（full-history DSA #2，patch temporal 模块级引用，
    不 patch dsa_selector / first_pyramid_service，避免误捕获 DSA #1）
  _compute_sqzmom_at_bar / _compute_volume_percentile_at_bar / _find_bar_index_by_time
  percentile_rank / _compute_derived_relation

Part B — Artifact / summary assembly bucket
  compute_core_artifact total + 内部 build_first_pyramid_core_snapshot / _extract_dsa_metrics /
    _extract_dsa_visual / _extract_state_events / _json_safe_value aggregate + residual
  build_summary_payload total + 内部 build_persisted_afc_payload / flatten_first_pyramid /
    assemble_first_pyramid_read_model + other
  encode_core_artifact_to_summary / _extract_extra_fields

Part C — full-universe serial projection（从 frozen dataset 实际统计 review-core eligible，
  bars>=60 门槛，不用总 instrument 数硬套）。

约束（与 3.4B-4 一致）：
- MEASUREMENT_ONLY / PRODUCTION_CODE_DIFF = ZERO / 不改公式 / 不改参数 / 不改 lookback /
  不改 window / 不实施向量化
- 同一 frozen dataset + 105 samples + warmup=1 + reps=3 + perf_counter
- 只 patch/timer，不重写 builder / serializer / 数据结构

Usage:
    cd backend && .venv/bin/python ../experiments/duplicate_compute_audit/attribute_34b5.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from collections import defaultdict
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
OUTPUT_DIR = EXPERIMENT_DIR / "output" / "3.4B-5"

# 与 3.4B-4 输出表完全一致的组件顺序（保持跨阶段口径）
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

# review-core eligibility 门槛（对应 compute_review_core_for_trade_date 的 len(df_1d) < 60 检查）
ELIGIBLE_MIN_BARS = 60


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


class AsyncTimer:
    __slots__ = ("fn", "key", "elapsed", "calls")

    def __init__(self, fn: Callable[..., Any], key: str) -> None:
        self.fn = fn
        self.key = key
        self.elapsed = 0.0
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return await self.fn(*args, **kwargs)
        finally:
            self.elapsed += time.perf_counter() - t0
            self.calls += 1


class CanonicalTimer:
    __slots__ = ("fn", "total", "calls")

    def __init__(self, fn: Callable[..., Any]) -> None:
        self.fn = fn
        self.total: dict[str, float] = defaultdict(float)
        self.calls: dict[str, int] = defaultdict(int)

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        aid = kwargs.get("algorithm_id") or (args[1] if len(args) > 1 else None)
        t0 = time.perf_counter()
        try:
            return await self.fn(*args, **kwargs)
        finally:
            self.total[str(aid)] += time.perf_counter() - t0
            self.calls[str(aid)] += 1


class HashTimer:
    __slots__ = ("fn", "total", "calls")

    def __init__(self, fn: Callable[..., Any]) -> None:
        self.fn = fn
        self.total: dict[str, float] = defaultdict(float)
        self.calls: dict[str, int] = defaultdict(int)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        aid = kwargs.get("algorithm_id") or (args[0] if args else None)
        t0 = time.perf_counter()
        try:
            return self.fn(*args, **kwargs)
        finally:
            self.total[str(aid)] += time.perf_counter() - t0
            self.calls[str(aid)] += 1


def _perf_run_instrument_34b5(
    df_1d: pd.DataFrame,
    instrument_id: uuid.UUID,
    trade_date: date,
    symbol: str,
    warmup: int,
    reps: int,
) -> list[dict[str, Any]]:
    """调用真实 compute_review_core_for_trade_date，3.4B-4 patch 集 + 3.4B-5 leaf timers。

    返回 warmup 后每个 timed rep 的：chain_total / 3.4B-4 兼容 rows / temporal leaf /
    artifact leaf / per-run reconciliation。
    """
    import unittest.mock as mock

    from app.services.canonical_computation_service import CanonicalComputationService
    from app.services.feature_snapshot_service import (
        _compute_daily_context,
        _compute_derived_relation,
        _compute_macd_state,
        _extract_extra_fields,
        build_summary_payload,
        compute_review_core_for_trade_date,
    )
    import app.services.canonical_adapters as cca_module
    import app.services.core_artifact_codec as cac_module
    import app.services.core_artifact_service as cas_module
    import app.services.feature_snapshot_service as fss_module
    import app.services.first_pyramid_service as fp_module
    import app.services.structural_factor_service as sfs_module
    import app.services.temporal_feature_service as tfs_module

    _orig_compute = CanonicalComputationService.compute
    _orig_hash = CanonicalComputationService._compute_result_hash

    bundle_timers = {
        "bundle_total": SyncTimer(cas_module.compute_core_kernel_bundle, "bundle_total"),
        "dsa": SyncTimer(fp_module.compute_dsa_bundle, "dsa"),
        "smc1": SyncTimer(fp_module.compute_smc_pine, "smc1"),
        "bb1": SyncTimer(fp_module.compute_bollinger_features, "bb1"),
        "sqzmom": SyncTimer(fp_module.compute_sqzmom_lb, "sqzmom"),
        "vc": SyncTimer(fp_module.compute_volume_context_series, "vc"),
    }
    structural_timers = {
        "atr": SyncTimer(sfs_module.compute_atr, "atr"),
        "dsa_segment": SyncTimer(sfs_module._compute_dsa_segment_factors, "dsa_segment"),
        "swing": SyncTimer(sfs_module._compute_swing_factors, "swing"),
        "cost_vp": SyncTimer(sfs_module._compute_cost_position_factors, "cost_vp"),
        "momentum": SyncTimer(sfs_module._compute_volatility_momentum_factors, "momentum"),
        "participation": SyncTimer(sfs_module._compute_participation_factors, "participation"),
        "smc2": SyncTimer(sfs_module._compute_smc_freshness_factors, "smc2"),
    }
    segment_timers = {
        "macd": AsyncTimer(fss_module._compute_macd_state, "macd"),
        "daily_context": SyncTimer(fss_module._compute_daily_context, "daily_context"),
        "derived_relation": SyncTimer(fss_module._compute_derived_relation, "derived_relation"),
        "extra": AsyncTimer(fss_module._extract_extra_fields, "extra"),
        "core_artifact": SyncTimer(cas_module.compute_core_artifact, "core_artifact"),
        "summary": SyncTimer(fss_module.build_summary_payload, "summary"),
        "codec": SyncTimer(cac_module.encode_core_artifact_to_summary, "codec"),
    }
    kernel_timers = {
        "structural_kernel": SyncTimer(cca_module.compute_structural_features_adapter, "structural_kernel"),
        "bb2_kernel": SyncTimer(cca_module.compute_bollinger_adapter, "bb2_kernel"),
        "macd_kernel": SyncTimer(cca_module.compute_macd_adapter, "macd_kernel"),
    }
    # ---- 3.4B-5 Part A：Temporal leaf timers（temporal 模块级引用）----
    temporal_timers = {
        "hist_ages": SyncTimer(tfs_module._collect_historical_segment_ages, "hist_ages"),
        "dsa2_full": SyncTimer(tfs_module.compute_dsa_bundle, "dsa2_full"),
        "sqzmom_at_bar": SyncTimer(tfs_module._compute_sqzmom_at_bar, "sqzmom_at_bar"),
        "volpct_at_bar": SyncTimer(tfs_module._compute_volume_percentile_at_bar, "volpct_at_bar"),
        "find_bar_index": SyncTimer(tfs_module._find_bar_index_by_time, "find_bar_index"),
        "percentile_rank": SyncTimer(tfs_module.percentile_rank, "percentile_rank"),
    }
    # ---- 3.4B-5 Part B：Artifact leaf timers ----
    artifact_timers = {
        "build_fp_core": SyncTimer(fp_module.build_first_pyramid_core_snapshot, "build_fp_core"),
        "dsa_metrics": SyncTimer(cas_module._extract_dsa_metrics, "dsa_metrics"),
        "dsa_visual": SyncTimer(cas_module._extract_dsa_visual, "dsa_visual"),
        "state_events": SyncTimer(cas_module._extract_state_events, "state_events"),
        "json_safe": SyncTimer(cas_module._json_safe_value, "json_safe"),
        "build_afc": SyncTimer(fss_module.build_persisted_afc_payload, "build_afc"),
        "flatten": SyncTimer(fss_module.flatten_first_pyramid, "flatten"),
        "assemble_read": SyncTimer(fss_module.assemble_first_pyramid_read_model, "assemble_read"),
    }

    all_timers = {**bundle_timers, **structural_timers, **segment_timers,
                  **kernel_timers, **temporal_timers, **artifact_timers}
    canonical_timer = CanonicalTimer(_orig_compute)
    hash_timer = HashTimer(_orig_hash)

    patch_targets: list[tuple[Any, str, Any]] = [
        # 3.4B-4 兼容层
        (cas_module, "compute_core_kernel_bundle", bundle_timers["bundle_total"]),
        (fp_module, "compute_dsa_bundle", bundle_timers["dsa"]),
        (fp_module, "compute_smc_pine", bundle_timers["smc1"]),
        (fp_module, "compute_bollinger_features", bundle_timers["bb1"]),
        (fp_module, "compute_sqzmom_lb", bundle_timers["sqzmom"]),
        (fp_module, "compute_volume_context_series", bundle_timers["vc"]),
        (sfs_module, "compute_atr", structural_timers["atr"]),
        (sfs_module, "_compute_dsa_segment_factors", structural_timers["dsa_segment"]),
        (sfs_module, "_compute_swing_factors", structural_timers["swing"]),
        (sfs_module, "_compute_cost_position_factors", structural_timers["cost_vp"]),
        (sfs_module, "_compute_volatility_momentum_factors", structural_timers["momentum"]),
        (sfs_module, "_compute_participation_factors", structural_timers["participation"]),
        (sfs_module, "_compute_smc_freshness_factors", structural_timers["smc2"]),
        (fss_module, "_compute_macd_state", segment_timers["macd"]),
        (fss_module, "_compute_daily_context", segment_timers["daily_context"]),
        (fss_module, "_compute_derived_relation", segment_timers["derived_relation"]),
        (fss_module, "_extract_extra_fields", segment_timers["extra"]),
        (cas_module, "compute_core_artifact", segment_timers["core_artifact"]),
        (fss_module, "build_summary_payload", segment_timers["summary"]),
        (cac_module, "encode_core_artifact_to_summary", segment_timers["codec"]),
        (cca_module, "compute_structural_features_adapter", kernel_timers["structural_kernel"]),
        (cca_module, "compute_bollinger_adapter", kernel_timers["bb2_kernel"]),
        (cca_module, "compute_macd_adapter", kernel_timers["macd_kernel"]),
        (CanonicalComputationService, "compute", canonical_timer),
        (CanonicalComputationService, "_compute_result_hash", hash_timer),
        # 3.4B-5 Part A：temporal leaf
        (tfs_module, "_collect_historical_segment_ages", temporal_timers["hist_ages"]),
        (tfs_module, "compute_dsa_bundle", temporal_timers["dsa2_full"]),
        (tfs_module, "_compute_sqzmom_at_bar", temporal_timers["sqzmom_at_bar"]),
        (tfs_module, "_compute_volume_percentile_at_bar", temporal_timers["volpct_at_bar"]),
        (tfs_module, "_find_bar_index_by_time", temporal_timers["find_bar_index"]),
        (tfs_module, "percentile_rank", temporal_timers["percentile_rank"]),
        # 3.4B-5 Part B：artifact leaf
        # 注意：compute_core_artifact 经 core_artifact_service 模块级引用调用
        # build_first_pyramid_core_snapshot（cas L46 import），必须 patch cas_module 而非 fp_module。
        (cas_module, "build_first_pyramid_core_snapshot", artifact_timers["build_fp_core"]),
        (cas_module, "_extract_dsa_metrics", artifact_timers["dsa_metrics"]),
        (cas_module, "_extract_dsa_visual", artifact_timers["dsa_visual"]),
        (cas_module, "_extract_state_events", artifact_timers["state_events"]),
        (cas_module, "_json_safe_value", artifact_timers["json_safe"]),
        (fss_module, "build_persisted_afc_payload", artifact_timers["build_afc"]),
        (fss_module, "flatten_first_pyramid", artifact_timers["flatten"]),
        (fss_module, "assemble_first_pyramid_read_model", artifact_timers["assemble_read"]),
    ]

    def _reset() -> None:
        for t in all_timers.values():
            t.elapsed = 0.0
            t.calls = 0
        canonical_timer.total.clear()
        canonical_timer.calls.clear()
        hash_timer.total.clear()
        hash_timer.calls.clear()

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

    def _t(t: dict[str, SyncTimer], k: str) -> float:
        return t[k].elapsed

    def _collect(chain_total: float) -> dict[str, Any]:
        bt = {k: bundle_timers[k].elapsed for k in ("dsa", "smc1", "bb1", "sqzmom", "vc")}
        st = {k: structural_timers[k].elapsed
              for k in ("atr", "dsa_segment", "swing", "cost_vp", "momentum", "participation", "smc2")}

        bundle_total = bundle_timers["bundle_total"].elapsed
        structural_total = canonical_timer.total.get("structural_features", 0.0)
        macd_total = canonical_timer.total.get("macd", 0.0)
        bollinger_total = canonical_timer.total.get("bollinger", 0.0)
        daily_derived = (
            segment_timers["daily_context"].elapsed
            + segment_timers["derived_relation"].elapsed
        )
        extra_total = segment_timers["extra"].elapsed
        core_artifact = segment_timers["core_artifact"].elapsed
        summary = segment_timers["summary"].elapsed
        codec = segment_timers["codec"].elapsed
        bb2_kernel = kernel_timers["bb2_kernel"].elapsed
        macd_kernel = kernel_timers["macd_kernel"].elapsed

        rows: dict[str, float] = {
            "DSA": bt["dsa"],
            "SMC #1": bt["smc1"],
            "SMC #2 (freshness)": st["smc2"],
            "Bollinger #1": bt["bb1"],
            "Bollinger #2 (extra)": bb2_kernel,
            "SQZMOM": bt["sqzmom"],
            "VolumeContext": bt["vc"],
            "ATR / swing / participation": st["atr"] + st["swing"] + st["participation"],
            "structural derived (dsa_segment / momentum)": st["dsa_segment"] + st["momentum"],
            "single-period VP (cost_position)": st["cost_vp"],
            "MACD / daily_context / derived": macd_kernel + daily_derived,
            "Canonical hash / orchestration overhead": (
                (structural_total - kernel_timers["structural_kernel"].elapsed
                 - hash_timer.total.get("structural_features", 0.0))
                + (bollinger_total - bb2_kernel - hash_timer.total.get("bollinger", 0.0))
                + (macd_total - macd_kernel - hash_timer.total.get("macd", 0.0))
                + (bundle_total - sum(bt.values()))
            ),
            "Artifact / summary assembly": (
                core_artifact + summary + codec + extra_total - bb2_kernel - (
                    bollinger_total - bb2_kernel - hash_timer.total.get("bollinger", 0.0)
                )
            ),
        }
        rows["other / unmeasured"] = chain_total - sum(rows.values())

        # ---- Part A：Temporal leaf（exclusive reconciliation per-run）----
        daily_context_total = segment_timers["daily_context"].elapsed
        hist_ages = temporal_timers["hist_ages"].elapsed
        sqzmom = temporal_timers["sqzmom_at_bar"].elapsed
        volpct = temporal_timers["volpct_at_bar"].elapsed
        other_daily_context = daily_context_total - hist_ages - sqzmom - volpct
        dsa2_full = temporal_timers["dsa2_full"].elapsed
        seg_age_projection = hist_ages - dsa2_full

        # ---- Part B：Artifact leaf（exclusive reconciliation per-run）----
        build_fp = artifact_timers["build_fp_core"].elapsed
        dsa_metrics = artifact_timers["dsa_metrics"].elapsed
        dsa_visual = artifact_timers["dsa_visual"].elapsed
        state_events = artifact_timers["state_events"].elapsed
        artifact_residual = core_artifact - build_fp - dsa_metrics - dsa_visual - state_events
        afc = artifact_timers["build_afc"].elapsed
        flatten = artifact_timers["flatten"].elapsed
        assemble_read = artifact_timers["assemble_read"].elapsed
        other_summary = summary - afc - flatten - assemble_read

        temporal_leaf = {
            "macd_state": segment_timers["macd"].elapsed,
            "macd_kernel": macd_kernel,
            "daily_context": daily_context_total,
            "hist_ages": hist_ages,
            "dsa2_full": dsa2_full,
            "seg_age_projection": seg_age_projection,
            "other_daily_context": other_daily_context,
            "sqzmom_at_bar": sqzmom,
            "volpct_at_bar": volpct,
            "find_bar_index": temporal_timers["find_bar_index"].elapsed,
            "percentile_rank": temporal_timers["percentile_rank"].elapsed,
            "derived_relation": segment_timers["derived_relation"].elapsed,
        }
        artifact_leaf = {
            "core_artifact": core_artifact,
            "build_fp_core": build_fp,
            "dsa_metrics": dsa_metrics,
            "dsa_visual": dsa_visual,
            "state_events": state_events,
            "json_safe_aggregate": artifact_timers["json_safe"].elapsed,
            "artifact_residual": artifact_residual,
            "summary": summary,
            "build_afc_payload": afc,
            "flatten_first_pyramid": flatten,
            "assemble_read_model": assemble_read,
            "other_summary_assembly": other_summary,
            "codec": codec,
            "extra_fields": extra_total,
        }
        recon = {
            "daily_context_parent": daily_context_total,
            "daily_context_sum": hist_ages + sqzmom + volpct + other_daily_context,
            "hist_ages_parent": hist_ages,
            "hist_ages_sum": dsa2_full + seg_age_projection,
            "core_artifact_parent": core_artifact,
            "core_artifact_sum": build_fp + dsa_metrics + dsa_visual + state_events + artifact_residual,
            "summary_parent": summary,
            "summary_sum": afc + flatten + assemble_read + other_summary,
        }
        # 3.4B-4 兼容非桶 leaf（单个函数，非聚合桶）
        leaf_34b4 = {
            "dsa1": bt["dsa"],
            "smc1": bt["smc1"],
            "smc2": st["smc2"],
            "bb1": bt["bb1"],
            "sqzmom": bt["sqzmom"],
            "vc": bt["vc"],
            "atr": st["atr"],
            "dsa_segment": st["dsa_segment"],
            "swing": st["swing"],
            "cost_vp": st["cost_vp"],
            "momentum": st["momentum"],
            "participation": st["participation"],
        }
        return {
            "chain_total": chain_total,
            "rows": rows,
            "leaf_34b4": leaf_34b4,
            "temporal_leaf": temporal_leaf,
            "artifact_leaf": artifact_leaf,
            "recon": recon,
            "calls": {
                "canonical": dict(canonical_timer.calls),
                "hash": dict(hash_timer.calls),
                "dsa2_full_calls": temporal_timers["dsa2_full"].calls,
                "dsa1_calls": bundle_timers["dsa"].calls,
            },
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


def _p50_p95(a: np.ndarray) -> tuple[float, float]:
    if a.size == 0:
        return (0.0, 0.0)
    return (float(np.percentile(a, 50)), float(np.percentile(a, 95)))


def _report_leaf(tag: str, runs_all: list[dict[str, Any]], key: str,
                 totals: np.ndarray) -> dict[str, Any]:
    vals = np.array([r[tag][key] for r in runs_all], dtype=float) * 1000.0
    p50, p95 = _p50_p95(vals)
    return {
        "leaf": key,
        "elapsed_p50_ms": round(p50, 4),
        "elapsed_p95_ms": round(p95, 4),
        "median_share_of_chain_pct": round(float(np.median(vals / (totals * 1000.0))) * 100.0, 3),
        "n_calls_median": None,
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
    ap = argparse.ArgumentParser(description="3.4B-5 residual hotspot attribution")
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
    print(f"3.4B-5 attribution: {len(selected)} instruments "
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
            runs = _perf_run_instrument_34b5(df_1d, uuid.UUID(iid), trade_date, symbol,
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
    t50, t95 = _p50_p95(totals * 1000.0)

    # ---- 3.4B-4 兼容表 ----
    table: list[dict[str, Any]] = []
    for n in ROW_NAMES:
        vals = np.array([r["rows"].get(n, 0.0) for r in runs_all], dtype=float) * 1000.0
        p50, p95 = _p50_p95(vals)
        table.append({
            "component": n,
            "elapsed_p50_ms": round(p50, 3),
            "elapsed_p95_ms": round(p95, 3),
            "median_share_pct": round(float(np.median(vals / (totals * 1000.0))) * 100.0, 3),
        })
    table.append({
        "component": "Total per-stock",
        "elapsed_p50_ms": round(t50, 3),
        "elapsed_p95_ms": round(t95, 3),
        "median_share_pct": 100.0,
    })
    med_share_sum = sum(r["median_share_pct"] for r in table[:-1])

    # ---- Part A/B leaf 报告 ----
    temporal_leaves = ["macd_state", "macd_kernel", "daily_context", "hist_ages", "dsa2_full",
                       "seg_age_projection", "other_daily_context", "sqzmom_at_bar", "volpct_at_bar",
                       "find_bar_index", "percentile_rank", "derived_relation"]
    artifact_leaves = ["core_artifact", "build_fp_core", "dsa_metrics", "dsa_visual", "state_events",
                       "json_safe_aggregate", "artifact_residual", "summary", "build_afc_payload",
                       "flatten_first_pyramid", "assemble_read_model", "other_summary_assembly",
                       "codec", "extra_fields"]

    temporal_report = [_report_leaf("temporal_leaf", runs_all, k, totals) for k in temporal_leaves]
    artifact_report = [_report_leaf("artifact_leaf", runs_all, k, totals) for k in artifact_leaves]

    # ---- reconciliation ----
    reconciliation = {
        "phase": "3.4B-5",
        "method": "per-run exclusive reconciliation; error = |(child_sum - parent)/parent|",
        "temporal": {
            "daily_context": {
                "hist_ages + sqzmom + volpct + other": _recon_error(
                    runs_all, "daily_context_parent", "daily_context_sum"),
            },
            "hist_ages": {
                "dsa2_full + seg_age_projection": _recon_error(
                    runs_all, "hist_ages_parent", "hist_ages_sum"),
            },
        },
        "artifact": {
            "core_artifact": {
                "build_fp + dsa_metrics + dsa_visual + state_events + residual": _recon_error(
                    runs_all, "core_artifact_parent", "core_artifact_sum"),
            },
            "summary": {
                "afc + flatten + assemble + other": _recon_error(
                    runs_all, "summary_parent", "summary_sum"),
            },
        },
    }

    # ---- Part C：serial projection ----
    eligible_info = _eligible_count_from_parquet()
    n_elig = eligible_info["eligible_instruments"]
    serial_projection = {
        "eligible_instruments": n_elig,
        "eligibility_rule": eligible_info["eligibility_rule"],
        "total_instruments_in_dataset": eligible_info["total_instruments_in_dataset"],
        "p50_serial_projection_seconds": round(t50 * n_elig / 1000.0, 1),
        "p50_serial_projection_minutes": round(t50 * n_elig / 1000.0 / 60.0, 1),
        "p95_serial_projection_seconds": round(t95 * n_elig / 1000.0, 1),
        "p95_serial_projection_minutes": round(t95 * n_elig / 1000.0 / 60.0, 1),
        "caveat": "serial compute-only projection != production AfterClose wall-clock "
                  "(不含 DB 读/写、并发重叠、调度与运行环境开销)",
    }

    # ---- top 10 leaf hotspots（仅按当前测量排序；排除复合桶，避免与子叶子重叠）----
    # 复合桶（bucket，不作为 leaf 参与排名）：daily_context / hist_ages / core_artifact / summary
    temporal_leaf_names = ["macd_state", "macd_kernel", "dsa2_full", "seg_age_projection",
                           "other_daily_context", "sqzmom_at_bar", "volpct_at_bar",
                           "find_bar_index", "percentile_rank", "derived_relation"]
    # 注意：json_safe_aggregate 是 _json_safe_value 在 build_fp_core 内部递归调用的嵌套计数
    # （与 build_fp_core 时间重叠），保留原始测量但不参与排名，避免重叠 timer 双重计数。
    artifact_leaf_names = ["build_fp_core", "dsa_metrics", "dsa_visual", "state_events",
                           "artifact_residual", "build_afc_payload",
                           "flatten_first_pyramid", "assemble_read_model", "other_summary_assembly",
                           "codec", "extra_fields"]
    leaf_rows = [
        dict(r) for r in temporal_report
        if r["leaf"] in temporal_leaf_names
    ] + [
        dict(r) for r in artifact_report
        if r["leaf"] in artifact_leaf_names
    ]
    for lr in leaf_rows:
        lr.pop("n_calls_median", None)
    # 3.4B-4 兼容非桶 leaf（单个函数）
    leaf_34b4_labels = {
        "dsa1": "DSA #1 (250-bar bundle)",
        "smc1": "SMC #1 (bundle)",
        "smc2": "SMC #2 (freshness)",
        "bb1": "Bollinger #1 (bundle)",
        "sqzmom": "SQZMOM",
        "vc": "VolumeContext",
        "atr": "ATR",
        "dsa_segment": "dsa_segment factors",
        "swing": "swing factors",
        "cost_vp": "cost_position (single-period VP)",
        "momentum": "volatility_momentum",
        "participation": "participation",
    }
    for k, label in leaf_34b4_labels.items():
        vals = np.array([r["leaf_34b4"][k] for r in runs_all], dtype=float) * 1000.0
        p50, p95 = _p50_p95(vals)
        leaf_rows.append({
            "leaf": label,
            "elapsed_p50_ms": round(p50, 4),
            "elapsed_p95_ms": round(p95, 4),
            "median_share_of_chain_pct": round(
                float(np.median(vals / (totals * 1000.0))) * 100.0, 3),
        })
    leaf_rows.sort(key=lambda d: -d["elapsed_p50_ms"])
    top10_leaves = leaf_rows[:10]

    # ---- Decision Gate 分类（静态证据 + 本次测量）----
    classification = _classify_leaves(top10_leaves)

    residual = {
        "phase": "3.4B-5",
        "method": "call real compute_review_core_for_trade_date; perf_counter; "
                  "3.4B-4 patch 集 + 3.4B-5 leaf timers; per-run exclusive reconciliation",
        "reproducibility": {
            "dataset": "review-source-c5c686e-v1",
            "audit_code_sha": head,
            "baseline": {"phase": "3.4B-4", "audit_code_sha": "79cf519db5e408f04a7ac378db36571f296c8b2e"},
        },
        "n_runs": len(runs_all),
        "total_per_stock": {"p50_ms": round(t50, 3), "p95_ms": round(t95, 3)},
        "table_34b4_compatible": table,
        "median_share_sum_pct": round(med_share_sum, 3),
        "temporal_leaf": temporal_report,
        "artifact_leaf": artifact_report,
        "top10_leaves": top10_leaves,
        "decision_gate": classification,
        "measurement_notes": {
            "json_safe_aggregate": (
                "_json_safe_value 在 build_fp_core 内部被递归调用，时间与 build_fp_core 重叠；"
                "仅保留原始测量（见 artifact_leaf），不参与 top10 排名避免双重计数；"
                "其真实 exclusive 序列化开销上界为 artifact_residual（top10 #10）"
            ),
            "nested_counters_not_ranked": [
                "daily_context / hist_ages / core_artifact / summary 为聚合桶，不参与排名",
                "find_bar_index / percentile_rank 为 hist_ages 内部子计数器，数值 <0.5ms，未进入 top10",
            ],
        },
        "full_universe_serial_projection": serial_projection,
        "calls_summary": {
            "dsa1_calls": sorted({r["calls"]["dsa1_calls"] for r in runs_all}),
            "dsa2_full_calls": sorted({r["calls"]["dsa2_full_calls"] for r in runs_all}),
        },
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "residual_attribution.json").write_text(
        json.dumps(residual, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(OUTPUT_DIR / "per_instrument.jsonl", "w", encoding="utf-8") as f:
        for row, runs in zip(per_instrument, per_instrument_runs):
            f.write(json.dumps({**row, "runs": runs}, ensure_ascii=False) + "\n")
    recon_out = {"reconciliation": reconciliation, "eligible_projection": serial_projection}
    (OUTPUT_DIR / "reconciliation.json").write_text(
        json.dumps(recon_out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "total_per_stock": residual["total_per_stock"],
        "temporal_leaf": temporal_report,
        "artifact_leaf": artifact_report,
        "reconciliation": reconciliation,
        "top10_leaves": top10_leaves,
        "decision_gate": classification,
        "full_universe_serial_projection": serial_projection,
    }, ensure_ascii=False, indent=2))
    print(f"output: {OUTPUT_DIR}")
    print("NOTE: PRODUCTION_CODE_DIFF = ZERO（本脚本未修改任何生产代码）")


def _classify_leaves(top10: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对 top leaf 按 Decision Gate A/B/C/D 分类（静态代码证据 + 测量 share）。"""
    gate: dict[str, str] = {
        # A. exact duplicate
        # （本轮 top leaf 中无 exact duplicate：SMC#2/BB#2 已在 3.4B-3 关闭）
        # B. safe vectorizable pure loop
        "find_bar_index": "B",
        "seg_age_projection": "B",
        # C. legitimate stateful computation（kernel / 真实算法）
        "dsa2_full": "C",
        "dsa_metrics": "C",
        "build_fp_core": "C",
        "hist_ages": "C",
        "dsa_segment factors": "C",
        "DSA #1 (250-bar bundle)": "C",
        "SMC #1 (bundle)": "C",
        "SMC #2 (freshness)": "C",
        "Bollinger #1 (bundle)": "C",
        "VolumeContext": "C",
        "cost_position (single-period VP)": "C",
        "SQZMOM": "C",
        "swing factors": "C",
        "participation": "C",
        "volatility_momentum": "C",
        "ATR": "C",
        # D. serialization / copying / projection overhead
        "build_afc_payload": "D",
        "flatten_first_pyramid": "D",
        "assemble_read_model": "D",
        "json_safe_aggregate": "D",
        "codec": "D",
        "artifact_residual": "D",
        "other_summary_assembly": "D",
        "extra_fields": "D",
        "percentile_rank": "D",
    }
    evidence: dict[str, str] = {
        "find_bar_index": "per-segment pd.Timestamp + 全 index 布尔 mask（O(n)）纯 Python 循环；可一次性预建 time->idx 索引，数学等价",
        "seg_age_projection": "hist_ages 内 segment 循环 + age 列表拼装，纯 Python；可向量化/预计算",
        "dsa2_full": "full-history compute_dsa_bundle（DSA #2，DIFFERENT_CONTRACT 250 无 lookback）；真实内核计算，禁止改 lookback/改算法",
        "build_fp_core": "FirstPyramidCoreSnapshot 纯 builder（P0-03 收敛），组装大 dict；真实组装成本",
        "dsa_metrics": "从 raw dsa_bundle.last_row_metrics 提取投影标量，含 _json_safe_value 递归",
        "hist_ages": "_collect_historical_segment_ages 整体（DSA#2 + 段投影循环），与 dsa2_full/seg_age_projection 部分重叠",
        "dsa_segment factors": "structural dsa_segment 因子（消费 precomputed dsa_bundle），真实算法",
        "DSA #1 (250-bar bundle)": "compute_core_kernel_bundle 内 250-bar compute_dsa_bundle，真实内核",
        "SMC #1 (bundle)": "bundle 内 compute_smc_pine，真实内核",
        "SMC #2 (freshness)": "freshness 复用 _shared_raw.smc_result（3.4B-3A 已闭合），剩余为适配/包装开销",
        "Bollinger #1 (bundle)": "bundle 内 compute_bollinger_features，真实内核",
        "VolumeContext": "compute_volume_context_series，真实内核",
        "cost_position (single-period VP)": "单周期 VP 计算，真实算法",
        "SQZMOM": "bundle 内 compute_sqzmom_lb，真实内核",
        "swing factors": "structural swing 因子，真实算法",
        "participation": "structural participation 因子，真实算法",
        "volatility_momentum": "structural momentum 因子，真实算法",
        "ATR": "compute_atr，真实内核",
        "build_afc_payload": "Atomic Fact Contract V1 摘要 dict 组装（copy/serialize 开销）",
        "flatten_first_pyramid": "99 字段扁平化 dict 投影",
        "assemble_read_model": "read model 组装 + 元数据 dict 拷贝",
        "json_safe_aggregate": "_json_safe_value 递归（numpy/Timestamp → native），序列化开销",
        "codec": "encode_core_artifact_to_summary dict 组装",
        "artifact_residual": "CoreComputationArtifact 构造 + 顶层 _json_safe_value + model_dump",
        "other_summary_assembly": "summary dict 直接字段拼装",
        "extra_fields": "_extract_extra_fields 剩余（current_price/change_pct + BB 复用后无 kernel）",
        "percentile_rank": "numpy 窗口 + mask + percentile，单次 O(lookback)；daily 路径调用次数少",
    }
    out = []
    for leaf in top10:
        name = leaf["leaf"]
        out.append({
            "leaf": name,
            "classification": gate.get(name, "C"),
            "elapsed_p50_ms": leaf["elapsed_p50_ms"],
            "median_share_of_chain_pct": leaf["median_share_of_chain_pct"],
            "evidence": evidence.get(name, "待补充静态证据"),
        })
    return out


if __name__ == "__main__":
    main()
