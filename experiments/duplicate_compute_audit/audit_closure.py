#!/usr/bin/env python3
"""Phase 3.4A-1 — Compute-Once Closure Audit（Pass 1：call-count + targeted spy）。

静态 artifact-flow 审计结论来自对当前 dev 代码的逐段核验（见输出 static_consumer_map.json），
本脚本执行 **runtime targeted spy**：对 sample_manifest 每个 instrument 重放生产子链

    compute_core_kernel_bundle(df_1d, None)          # shared raw（DSA/SMC#1/BB#1/SQZMOM/VC）
    → compute_structural_features_adapter(1d, precomputed=_structural_precomputed)  # SMC#2
    → _extract_extra_fields(df_1d)                   # BB#2（canonical bollinger）

并对底层 kernel 做计数包装（spy），量化真实低层 invocation count。

方法学（关键约束，见计划 v3.1 §四）：
- **不把 diagnostics 传入 structural**（structural 先 bump 再复用 precomputed，
  会导致 DSA diagnostics=2 假计数）。真实低层 invocation 只由 targeted spy 得出。
- 不修改任何生产代码（PRODUCTION_CODE_DIFF = ZERO），实验代码仅存在于本目录。
- 只读消费 frozen dataset；不连远程 DB；不做 PG migration / SQL 实验。

Usage:
    python audit_closure.py --count 10
    python audit_closure.py --count 500 --include-boundary
    python audit_closure.py --static-only
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

REPO_ROOT = Path(__file__).resolve().parents[2]
PARQUET_DIR = (
    REPO_ROOT / "backend" / ".perfdata" / "review" / "review-source-c5c686e-v1" / "parquet"
)
OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "3.4A-1"
MANIFEST_PATH = (
    Path(__file__).resolve().parent / "output" / "3.4A-0" / "sample_manifest.jsonl"
)

# 与生产对齐的 eligibility 门槛
SMC_FRESHNESS_MIN_BARS = 250
BB_EXTRA_MIN_BARS = 21

# ---------------------------------------------------------------------------
# 静态 artifact-flow 审计结论（代码核验所得，作为 runtime spy 的对照基线）
# ---------------------------------------------------------------------------
STATIC_CONSUMER_MAP: list[dict[str, Any]] = [
    {
        "business_fact": "DSA",
        "canonical_producer": "compute_core_kernel_bundle -> compute_dsa_bundle",
        "artifact": "_shared_raw.dsa_bundle",
        "consumers": ["First Pyramid core builder", "structural dsa_segment (precomputed['dsa_bundle'])"],
        "shared_artifact_available": True,
        "consumer_actually_reuses": True,
        "extra_kernel_execution": 0,
        "classification": "reuse-ok",
    },
    {
        "business_fact": "SMC",
        "canonical_producer": "compute_core_kernel_bundle -> compute_smc_pine(params=None)",
        "artifact": "_shared_raw.smc_result",
        "consumers": ["First Pyramid core builder", "structural SMC freshness (_compute_smc_freshness_factors)"],
        "shared_artifact_available": True,
        "consumer_actually_reuses": False,
        "extra_kernel_execution": "+1（>=250 bars）",
        "classification": "EXACT_KERNEL_DUPLICATE candidate",
        "evidence": "structural_factor_service.L1541 compute_smc_adapter(bars, display_bars=len(bars)) 未消费 precomputed['smc_result']；同 kernel compute_smc_pine、相同 bars、params=None",
    },
    {
        "business_fact": "Bollinger",
        "canonical_producer": "compute_core_kernel_bundle -> compute_bollinger_features(BBcfg(20,2.0))",
        "artifact": "_shared_raw.bb_df",
        "consumers": ["structural momentum (precomputed['bb_df'])"],
        "shared_artifact_available": True,
        "consumer_actually_reuses": True,
        "extra_kernel_execution": 0,
        "classification": "reuse-ok",
    },
    {
        "business_fact": "Bollinger",
        "canonical_producer": "CanonicalComputationService.compute(algorithm_id='bollinger') -> compute_bollinger_adapter -> merged_dsa_atr_rope_bb_factors.compute_bollinger",
        "artifact": "_extract_extra_fields bb_upper/mid/lower",
        "consumers": ["summary_payload bb_upper/bb_mid/bb_lower"],
        "shared_artifact_available": True,
        "consumer_actually_reuses": False,
        "extra_kernel_execution": "+1（>=21 bars）",
        "classification": "EQUIVALENT_OUTPUT_SUBSET candidate",
        "evidence": "feature_snapshot_service.L1687-L1708 独立 canonical Bollinger；与 _shared_raw.bb_df 的 bb_mid/upper/lower 公式等价（SMA20+2σ，std ddof=0）但 producer 不同",
    },
    {
        "business_fact": "SQZMOM",
        "canonical_producer": "compute_core_kernel_bundle -> compute_sqzmom_lb",
        "artifact": "_shared_raw.sqzmom_result",
        "consumers": ["structural momentum (precomputed['sqz_result'])"],
        "shared_artifact_available": True,
        "consumer_actually_reuses": True,
        "extra_kernel_execution": 0,
        "classification": "reuse-ok",
    },
    {
        "business_fact": "VolumeContext",
        "canonical_producer": "compute_core_kernel_bundle -> compute_volume_context_series",
        "artifact": "_shared_raw.vc_series",
        "consumers": ["First Pyramid core builder"],
        "shared_artifact_available": True,
        "consumer_actually_reuses": None,
        "extra_kernel_execution": 0,
        "classification": "dead injection（structural 注入 precomputed['vc_series'] 但未消费，无害，不改造）",
        "evidence": "structural _compute_volatility_momentum_factors 仅消费 bb_df + sqz_result，未读 vc_series",
    },
]

STATIC_DIAGNOSTICS_BOUNDARY = {
    "gate_nature": "canonical owner scope 的 compute-once Gate（六类计数 run-scoped + enforce_compute_once_gate）",
    "not_a": "整个 process 内所有低层 kernel invocation 的全局 profiler",
    "blind_spots": [
        "structural SMC freshness 调用未传 diagnostics（freshness 在 owner 之外）",
        "_extract_extra_fields canonical bollinger 未计入 Gate",
        "MACD canonical compute 未计入 Gate",
    ],
    "method_rule": "禁止把 diagnostics 传进 structural 来测真实调用次数（先 bump 再复用 precomputed 会造成假计数）；真实低层 invocation 只由 targeted spy / wrapper 得出。",
}


# ---------------------------------------------------------------------------
# spy 工具
# ---------------------------------------------------------------------------


class CallCounter:
    """计数包装：统计真实低层 kernel invocation，委托原函数保证行为不变。"""

    def __init__(self, fn: Callable[..., Any], key: str) -> None:
        self.fn = fn
        self.key = key
        self.count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.count += 1
        return self.fn(*args, **kwargs)


def _load_bars() -> dict[str, pd.DataFrame]:
    """从 frozen parquet 一次性加载全部 instrument 的日线（DatetimeIndex + OHLCV）。"""
    bars = pd.read_parquet(PARQUET_DIR / "bars_daily.parquet")
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    out: dict[str, pd.DataFrame] = {}
    for iid, g in bars.groupby("instrument_id"):
        g = g.sort_values("trade_date").set_index("trade_date")
        g = g[["open", "high", "low", "close", "volume", "amount"]].astype(float)
        out[iid] = g
    return out


def _expected_counts(bars_count: int) -> dict[str, int]:
    """按 eligibility 计算期望的真实低层 invocation（与计划 §七 对齐）。"""
    smc_total = 1 + (1 if bars_count >= SMC_FRESHNESS_MIN_BARS else 0)
    return {
        "dsa": 1,
        "smc_total": smc_total,  # SMC#1 + (SMC#2 if >=250)
        "bollinger_total": 2,  # BB#1 bundle + BB#2 extra
        "sqzmom": 1,
        "volume_context": 1,
    }


def _run_instrument(
    df_1d: pd.DataFrame,
    instrument_id: uuid.UUID,
    trade_date: date,
    spy: dict[str, CallCounter],
    canonical_counts: dict[str, int],
    hash_counts: dict[str, int],
) -> dict[str, Any]:
    """镜像生产子链重放（bundle → structural(precomputed) → _extract_extra_fields）。"""
    from app.services.core_artifact_service import compute_core_kernel_bundle
    from app.services.canonical_adapters import compute_structural_features_adapter
    from app.services.feature_snapshot_service import _extract_extra_fields

    # 1. shared raw bundle
    _shared_raw = compute_core_kernel_bundle(df_1d, None)

    # 2. structural（复用 precomputed，不重算 kernel）
    _structural_precomputed = {
        "dsa_bundle": _shared_raw.dsa_bundle,
        "bb_df": _shared_raw.bb_df,
        "sqz_result": _shared_raw.sqzmom_result,
        **(
            {"smc_result": _shared_raw.smc_result}
            if getattr(_shared_raw, "smc_result", None) is not None
            else {}
        ),
        **(
            {"vc_series": _shared_raw.vc_series}
            if getattr(_shared_raw, "vc_series", None) is not None
            else {}
        ),
    }
    compute_structural_features_adapter(
        df_1d,
        "1d",
        precomputed_node_cluster=None,
        diagnostics=None,  # 禁止传 diagnostics：先 bump 再复用会造成假计数
        precomputed=_structural_precomputed,
    )

    # 3. extra fields（BB#2）
    asyncio.run(_extract_extra_fields(df_1d, instrument_id, trade_date, None, None))

    return {
        "smc_pine_first_pyramid": spy["smc_pine_first_pyramid"].count,  # SMC#1
        "smc_pine_smc_indicator": spy["smc_pine_smc_indicator"].count,  # SMC#2
        "bollinger_features_plotly": spy["bollinger_features_plotly"].count,  # BB#1
        "compute_bollinger_merged": spy["compute_bollinger_merged"].count,  # BB#2
        "dsa_bundle": spy["dsa_bundle"].count,
        "sqzmom_lb": spy["sqzmom_lb"].count,
        "volume_context": spy["volume_context_series"].count,
        "smc_adapter": spy["smc_adapter"].count,
        "canonical_dispatch": dict(canonical_counts),
        "canonical_hash": dict(hash_counts),
    }


# ---------------------------------------------------------------------------
# Pass 2 — 3.4A-3 Compute-Only Cost Decomposition（elapsed wall time, exclusive）
# ---------------------------------------------------------------------------


class SyncTimer:
    """最薄计时包装：time.perf_counter()（elapsed wall time），委托原函数行为不变。"""

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
    """异步版计时包装（被 `await` 的 async 函数挂点）。"""

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
    """CanonicalComputationService.compute 计时：按 algorithm_id 累计总耗时。"""

    __slots__ = ("fn", "total", "calls")

    def __init__(self, fn: Callable[..., Any]) -> None:
        self.fn = fn  # 原始 bound classmethod
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
    """_compute_result_hash 计时（serialize DataFrame/dict + SHA256），按 algorithm_id。"""

    __slots__ = ("fn", "total", "calls")

    def __init__(self, fn: Callable[..., Any]) -> None:
        self.fn = fn  # 原始 bound classmethod
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


def _perf_run_instrument(
    df_1d: pd.DataFrame,
    instrument_id: uuid.UUID,
    trade_date: date,
    symbol: str,
    warmup: int,
    reps: int,
) -> list[dict[str, Any]]:
    """调用真实 compute_review_core_for_trade_date（传 primary_bars + symbol，session 未用），
    用最薄计时包装按分解层级分段计时。返回 warmup 后每个 timed rep 的分解记录。

    - 不修改生产代码（PRODUCTION_CODE_DIFF = ZERO）
    - 计时区间内不做额外 hash/日志/diagnostics 构造（仅 perf_counter + 委托）
    - 组件为 exclusive（层层相减 / other = total - 显式组件和）
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

    _orig_compute = CanonicalComputationService.compute  # bound classmethod
    _orig_hash = CanonicalComputationService._compute_result_hash

    # ---- 计时挂点（key → (module, attr, timer)）----
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
    all_timers = {**bundle_timers, **structural_timers, **segment_timers, **kernel_timers}

    canonical_timer = CanonicalTimer(_orig_compute)
    hash_timer = HashTimer(_orig_hash)

    patch_targets: list[tuple[Any, str, Any]] = [
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
            None,  # session：primary_bars + instrument_symbol 提供时不被使用
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

        # 组件（exclusive，除 other 外相加即总耗时）
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

        diag = {
            "structural_features": {
                "kernel": kernel_timers["structural_kernel"].elapsed,
                "canonical_total": structural_total,
                "hash": hash_timer.total.get("structural_features", 0.0),
                "orchestration": structural_total - kernel_timers["structural_kernel"].elapsed
                - hash_timer.total.get("structural_features", 0.0),
            },
            "bollinger": {
                "kernel": bb2_kernel,
                "canonical_total": bollinger_total,
                "hash": hash_timer.total.get("bollinger", 0.0),
                "orchestration": bollinger_total - bb2_kernel
                - hash_timer.total.get("bollinger", 0.0),
            },
            "macd": {
                "kernel": macd_kernel,
                "canonical_total": macd_total,
                "hash": hash_timer.total.get("macd", 0.0),
                "orchestration": macd_total - macd_kernel - hash_timer.total.get("macd", 0.0),
            },
        }
        return {
            "chain_total": chain_total,
            "rows": rows,
            "canonical_diag": diag,
            "calls": {
                "canonical": dict(canonical_timer.calls),
                "hash": dict(hash_timer.calls),
            },
        }

    runs: list[dict[str, Any]] = []
    from contextlib import ExitStack
    stack = ExitStack()
    for mod, name, timer in patch_targets:
        stack.enter_context(mock.patch.object(mod, name, timer))
    with stack:
        # warmup（不采样）
        for _ in range(warmup):
            _reset()
            asyncio.run(_chain())
        # timed reps
        for _ in range(reps):
            _reset()
            ct = asyncio.run(_chain())
            runs.append(_collect(ct))
    return runs


def _perf_report(runs_all: list[dict[str, Any]], out: dict[str, Any]) -> dict[str, Any]:
    """汇总 p50/p95 elapsed + median exclusive share。rows 顺序固定。"""
    row_names = [
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
    totals = np.array([r["chain_total"] for r in runs_all], dtype=float)
    comp_vals = {n: np.array([r["rows"].get(n, 0.0) for r in runs_all], dtype=float) for n in row_names}
    shares = {n: (comp_vals[n] / totals) for n in row_names}

    def _p50_p95(a: np.ndarray) -> tuple[float, float]:
        if a.size == 0:
            return (0.0, 0.0)
        return (float(np.percentile(a, 50)), float(np.percentile(a, 95)))

    table: list[dict[str, Any]] = []
    for n in row_names:
        p50, p95 = _p50_p95(comp_vals[n] * 1000.0)
        table.append({
            "component": n,
            "elapsed_p50_ms": round(p50, 3),
            "elapsed_p95_ms": round(p95, 3),
            "median_share_pct": round(float(np.median(shares[n])) * 100.0, 3),
        })
    t50, t95 = _p50_p95(totals * 1000.0)
    table.append({
        "component": "Total per-stock",
        "elapsed_p50_ms": round(t50, 3),
        "elapsed_p95_ms": round(t95, 3),
        "median_share_pct": 100.0,
    })

    # canonical 诊断（差值 = orchestration/hash overhead）
    diag_algos = ["structural_features", "bollinger", "macd"]
    diag_rows: list[dict[str, Any]] = []
    for aid in diag_algos:
        kern = np.array([r["canonical_diag"][aid]["kernel"] for r in runs_all], dtype=float) * 1000.0
        ct = np.array([r["canonical_diag"][aid]["canonical_total"] for r in runs_all], dtype=float) * 1000.0
        hs = np.array([r["canonical_diag"][aid]["hash"] for r in runs_all], dtype=float) * 1000.0
        orch = np.array([r["canonical_diag"][aid]["orchestration"] for r in runs_all], dtype=float) * 1000.0
        diag_rows.append({
            "algorithm": aid,
            "kernel_p50_ms": round(float(np.median(kern)), 3),
            "canonical_total_p50_ms": round(float(np.median(ct)), 3),
            "hash_p50_ms": round(float(np.median(hs)), 3),
            "orchestration_p50_ms": round(float(np.median(orch)), 3),
            "hash_share_of_canonical_pct": round(float(np.median(hs / np.maximum(ct, 1e-9))) * 100.0, 2),
            "orchestration_share_of_canonical_pct": round(
                float(np.median(orch / np.maximum(ct, 1e-9))) * 100.0, 2),
        })

    # 校验 exclusive：median share 求和 ≈ 100
    med_share_sum = sum(float(np.median(shares[n])) for n in row_names) * 100.0
    out["table"] = table
    out["canonical_overhead"] = diag_rows
    out["median_share_sum_pct"] = round(med_share_sum, 3)
    out["n_runs"] = len(runs_all)
    return out


def perf_main(args: argparse.Namespace) -> None:
    """3.4A-3 主入口：对 sample 每个 instrument 跑 warmup + timed reps，输出 exclusive 分解。"""
    import json as _json
    from datetime import date as _date

    OUTPUT_PERF = Path(__file__).resolve().parent / "output" / "3.4A-3"
    OUTPUT_PERF.mkdir(parents=True, exist_ok=True)

    if not MANIFEST_PATH.exists():
        sys.exit(f"manifest 不存在: {MANIFEST_PATH}（先运行 build_manifest.py）")
    rows = [json.loads(line) for line in open(MANIFEST_PATH, encoding="utf-8")]
    main_rows = [r for r in rows if r["selection_reason"] == "main_ge250"]
    boundary_rows = [r for r in rows if r["selection_reason"] == "boundary_60_249"]
    selected = main_rows[: args.count]
    if args.include_boundary:
        selected = selected + boundary_rows
    print(f"perf decomposition: {len(selected)} instruments, "
          f"warmup={args.warmup} reps={args.reps} "
          f"(main {len(main_rows[:args.count])}, boundary {len(boundary_rows) if args.include_boundary else 0})")

    bars_by_id = _load_bars()
    runs_all: list[dict[str, Any]] = []
    per_instrument: list[dict[str, Any]] = []
    for row in selected:
        iid = row["instrument_id"]
        if iid not in bars_by_id:
            continue
        df_all = bars_by_id[iid]
        trade_date = _date.fromisoformat(row["max_trade_date"])
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
        "phase": "3.4A-3",
        "method": "call real compute_review_core_for_trade_date; elapsed wall time (perf_counter); "
                  "exclusive components; canonical kernel/hash/orchestration differential",
        "reproducibility": {
            "dataset": "review-source-c5c686e-v1",
            "audit_code_sha": args.audit_code_sha,
        },
    }
    out = _perf_report(runs_all, out)
    out["per_instrument"] = per_instrument
    out_path = OUTPUT_PERF / "cost_decomposition.json"
    with open(out_path, "w", encoding="utf-8") as f:
        _json.dump(out, f, ensure_ascii=False, indent=2)
    print(_json.dumps({"table": out["table"], "canonical_overhead": out["canonical_overhead"],
                       "median_share_sum_pct": out["median_share_sum_pct"]},
                      ensure_ascii=False, indent=2))
    print(f"output: {out_path}")
    print("NOTE: PRODUCTION_CODE_DIFF = ZERO（本脚本未修改任何生产代码）")


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3.4A-1 closure audit / 3.4A-3 cost decomposition")
    ap.add_argument("--mode", choices=["counts", "static", "perf"], default="counts",
                    help="counts=runtime targeted spy；static=只输出静态结论；perf=3.4A-3 成本分解")
    ap.add_argument("--count", type=int, default=10, help="主 sample 运行数量")
    ap.add_argument("--include-boundary", action="store_true", help="同时运行 boundary sample")
    ap.add_argument("--static-only", action="store_true", help="等价于 --mode static")
    ap.add_argument("--warmup", type=int, default=1, help="perf：每 instrument warmup 次数（不计入采样）")
    ap.add_argument("--reps", type=int, default=3, help="perf：每 instrument timed reps 次数")
    ap.add_argument("--audit-code-sha", default="", help="perf：git rev-parse HEAD 快照（可复现）")
    args = ap.parse_args()

    if args.mode == "perf":
        perf_main(args)
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 静态结论落盘
    static = {
        "static_consumer_map": STATIC_CONSUMER_MAP,
        "diagnostics_coverage_boundary": STATIC_DIAGNOSTICS_BOUNDARY,
    }
    with open(OUTPUT_DIR / "static_consumer_map.json", "w", encoding="utf-8") as f:
        json.dump(static, f, ensure_ascii=False, indent=2)

    if args.static_only or args.mode == "static":
        print(json.dumps(static, ensure_ascii=False, indent=2))
        print(f"\nstatic map written: {OUTPUT_DIR / 'static_consumer_map.json'}")
        return

    # 加载 manifest + bars
    if not MANIFEST_PATH.exists():
        sys.exit(f"manifest 不存在: {MANIFEST_PATH}（先运行 build_manifest.py）")
    rows = [json.loads(line) for line in open(MANIFEST_PATH, encoding="utf-8")]
    main_rows = [r for r in rows if r["selection_reason"] == "main_ge250"]
    boundary_rows = [r for r in rows if r["selection_reason"] == "boundary_60_249"]
    selected = main_rows[: args.count]
    if args.include_boundary:
        selected = selected + boundary_rows
    print(f"running spy on {len(selected)} instruments "
          f"(main {len(main_rows[:args.count])}, boundary {len(boundary_rows) if args.include_boundary else 0})")

    bars_by_id = _load_bars()

    # 导入生产模块（仅 spy 挂点，不修改）
    import app.services.canonical_adapters as cca_module
    import app.services.first_pyramid_service as fp_module
    import app.strategy_assets.algorithms.features.merged_dsa_atr_rope_bb_factors as merged_bb_module
    import app.strategy_assets.algorithms.features.smc_indicator as smc_ind_module
    from app.services.canonical_computation_service import CanonicalComputationService

    # 原始 kernel 引用（spy 委托目标，行为不变）
    _fp_smc = fp_module.compute_smc_pine
    _fp_bb = fp_module.compute_bollinger_features
    _fp_dsa = fp_module.compute_dsa_bundle
    _fp_sqz = fp_module.compute_sqzmom_lb
    _fp_vc = fp_module.compute_volume_context_series
    _ind_smc = smc_ind_module.compute_smc_pine
    _merged_bb = merged_bb_module.compute_bollinger
    _cca_smc = cca_module.compute_smc_adapter

    # 构造 spy 实例（一次，跨 instrument 复用计数清零逻辑由脚本层处理）
    import unittest.mock as mock

    results: list[dict[str, Any]] = []
    for row in selected:
        iid = row["instrument_id"]
        if iid not in bars_by_id:
            continue
        df_all = bars_by_id[iid]
        trade_date = date.fromisoformat(row["max_trade_date"])
        df_1d = df_all[df_all.index.date <= trade_date]

        spy: dict[str, CallCounter] = {}
        canonical_counts: dict[str, int] = defaultdict(int)
        hash_counts: dict[str, int] = defaultdict(int)

        # canonical compute / hash 均为 @classmethod：patch 后保留原绑定 classmethod 作 side_effect，
        # 避免 plain wrapper 丢失 cls 导致 TypeError。
        _orig_compute = CanonicalComputationService.compute  # bound classmethod
        _orig_hash = CanonicalComputationService._compute_result_hash

        async def _canonical_side_effect(*a, **kw):
            # side_effect 必须是 async def：Python 3.11 unittest.mock 对「同步函数返回
            # coroutine」会包一层 wrapper coroutine，await 拿到内层 coroutine 而非结果。
            aid = kw.get("algorithm_id") or (a[1] if len(a) > 1 else None)
            canonical_counts[str(aid)] += 1
            return await _orig_compute(*a, **kw)

        def _hash_side_effect(*a, **kw):
            hash_counts["result_hash"] += 1
            return _orig_hash(*a, **kw)

        with (
            mock.patch.object(CanonicalComputationService, "compute") as m_compute,
            mock.patch.object(CanonicalComputationService, "_compute_result_hash") as m_hash,
            mock.patch.object(fp_module, "compute_smc_pine",
                              CallCounter(_fp_smc, "smc_pine_first_pyramid")) as m_fp_smc,
            mock.patch.object(fp_module, "compute_bollinger_features",
                              CallCounter(_fp_bb, "bollinger_features_plotly")) as m_fp_bb,
            mock.patch.object(fp_module, "compute_dsa_bundle",
                              CallCounter(_fp_dsa, "dsa_bundle")) as m_fp_dsa,
            mock.patch.object(fp_module, "compute_sqzmom_lb",
                              CallCounter(_fp_sqz, "sqzmom_lb")) as m_fp_sqz,
            mock.patch.object(fp_module, "compute_volume_context_series",
                              CallCounter(_fp_vc, "volume_context_series")) as m_fp_vc,
            mock.patch.object(smc_ind_module, "compute_smc_pine",
                              CallCounter(_ind_smc, "smc_pine_smc_indicator")) as m_ind_smc,
            mock.patch.object(merged_bb_module, "compute_bollinger",
                              CallCounter(_merged_bb, "compute_bollinger_merged")) as m_merged_bb,
            mock.patch.object(cca_module, "compute_smc_adapter",
                              CallCounter(_cca_smc, "smc_adapter")) as m_cca_smc,
        ):
            m_compute.side_effect = _canonical_side_effect
            m_hash.side_effect = _hash_side_effect
            spy = {
                "smc_pine_first_pyramid": m_fp_smc,
                "smc_pine_smc_indicator": m_ind_smc,
                "bollinger_features_plotly": m_fp_bb,
                "compute_bollinger_merged": m_merged_bb,
                "dsa_bundle": m_fp_dsa,
                "sqzmom_lb": m_fp_sqz,
                "volume_context_series": m_fp_vc,
                "smc_adapter": m_cca_smc,
            }
            actual = _run_instrument(df_1d, uuid.UUID(iid), trade_date, spy, canonical_counts, hash_counts)

        exp = _expected_counts(len(df_1d))
        actual_counts = {
            "dsa": actual["dsa_bundle"],
            "smc_total": actual["smc_pine_first_pyramid"] + actual["smc_pine_smc_indicator"],
            "bollinger_total": actual["bollinger_features_plotly"] + actual["compute_bollinger_merged"],
            "sqzmom": actual["sqzmom_lb"],
            "volume_context": actual["volume_context"],
        }
        ok = all(exp[k] == actual_counts[k] for k in exp)
        results.append(
            {
                "instrument_id": iid,
                "symbol": row.get("symbol"),
                "market": row.get("market"),
                "bars_count": int(len(df_1d)),
                "selection_reason": row["selection_reason"],
                "smc_freshness_eligible": len(df_1d) >= SMC_FRESHNESS_MIN_BARS,
                "expected": exp,
                "actual": actual_counts,
                "breakdown": {
                    "smc_first_pyramid": actual["smc_pine_first_pyramid"],
                    "smc_smc_indicator": actual["smc_pine_smc_indicator"],
                    "smc_adapter": actual["smc_adapter"],
                    "bb_first_pyramid": actual["bollinger_features_plotly"],
                    "bb_merged": actual["compute_bollinger_merged"],
                    "canonical_dispatch": actual["canonical_dispatch"],
                    "canonical_hash": actual["canonical_hash"],
                },
                "match": ok,
            }
        )
        if not ok:
            print(f"[MISMATCH] {iid} {row.get('symbol')} bars={len(df_1d)} exp={exp} act={actual_counts}")

    passed = sum(1 for r in results if r["match"])
    out = {
        "phase": "3.4A-1",
        "method": "runtime targeted spy（不传 diagnostics；底层 kernel 计数包装）",
        "spy_points": [
            "smc_pine_first_pyramid (SMC#1, first_pyramid_service)",
            "smc_pine_smc_indicator (SMC#2 via compute_smc_adapter)",
            "bollinger_features_plotly (BB#1, first_pyramid_service)",
            "compute_bollinger_merged (BB#2 via canonical bollinger adapter)",
            "dsa_bundle / sqzmom_lb / volume_context_series",
            "CanonicalComputationService.compute (dispatch count)",
            "CanonicalComputationService._compute_result_hash (hash count)",
        ],
        "expected_formula": {
            "smc_total": "N_core_eligible + N_bars_ge_250",
            "bollinger_total": "N_core_eligible + N_bars_ge_21（本样本均 >=60）",
            "dsa/sqzmom/volume_context": "N_core_eligible（复用正常，无重复）",
        },
        "results": results,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
        },
    }
    out_path = OUTPUT_DIR / "closure_audit_counts.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\nclosure audit: {passed}/{len(results)} matched expected call-count")
    print(f"output: {out_path}")
    if results:
        print("sample of results:")
        for r in results[:3]:
            print(" ", r["instrument_id"], r["symbol"], r["bars_count"], r["expected"], r["actual"], r["match"])
    print("\nNOTE: PRODUCTION_CODE_DIFF = ZERO（本脚本未修改任何生产代码）")


if __name__ == "__main__":
    main()
