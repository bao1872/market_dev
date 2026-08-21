#!/usr/bin/env python3
"""Phase 3.4A-2 — Reuse Safety Proof（只证明，不实现）。

验证「future 让 SMC freshness / BB extra 复用 `_shared_raw` 不会改变结果」，
为 3.4B Residual Duplicate Closure 提供安全依据。

三个子证明（对 sample_manifest 每个 instrument 执行）：

1. SMC freshness parity：
   - Baseline A = 生产路径 `_compute_smc_freshness_factors(bars)`（当前实际执行）
   - Replication self-check：`_freshness_from_dto(bars, compute_smc_adapter(bars, display_bars=len(bars))) == A`
     （证明本脚本的 DTO→因子 复刻与生产一致，避免"自证"）
   - Candidate B = `_freshness_from_dto(bars, adapt_smc_to_display_dto(_shared_raw.smc_result, len(bars)))`
   - 要求 A == B 100%；任何不一致即记录为 3.4B blocker

2. raw immutability：
   - `_stable_hash(_shared_raw.smc_result)` before vs after candidate consumer 必须一致
   - 意义：`_shared_raw` 在 structural 消费后还会被 First Pyramid / CoreArtifact 消费，
     禁止结构污染（structural 先于 CoreArtifact 消费 raw）。

3. BB common-field parity：
   - `_shared_raw.bb_df` vs canonical bollinger#2 payload 的 `bb_mid/bb_upper/bb_lower`
   - 两个 producer 公式相同（SMA20 + 2σ，std ddof=0），判 EQUIVALENT_OUTPUT_SUBSET

方法学（计划 v3.1）：
- 不修改任何生产代码（PRODUCTION_CODE_DIFF = ZERO）
- 只读消费 frozen dataset；不连远程 DB
- 只证明，不实现复用

Usage:
    python reuse_safety_proof.py --count 100
    python reuse_safety_proof.py --count 500
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

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
PARQUET_DIR = (
    REPO_ROOT / "backend" / ".perfdata" / "review" / "review-source-c5c686e-v1" / "parquet"
)
OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "3.4A-2"
MANIFEST_PATH = (
    Path(__file__).resolve().parent / "output" / "3.4A-0" / "sample_manifest.jsonl"
)

SMC_FRESHNESS_MIN_BARS = 250
BB_WIN = 20
BB_K = 2.0
COMMON_BB_FIELDS = ["bb_mid", "bb_upper", "bb_lower"]

_FRESHNESS_KEYS = [
    "smc_bos_bullish_internal_freshness_bars",
    "smc_bos_bullish_swing_freshness_bars",
    "smc_bos_bearish_internal_freshness_bars",
    "smc_bos_bearish_swing_freshness_bars",
    "smc_choch_bullish_internal_freshness_bars",
    "smc_choch_bullish_swing_freshness_bars",
    "smc_choch_bearish_internal_freshness_bars",
    "smc_choch_bearish_swing_freshness_bars",
    "smc_order_block_touch_bullish_internal_freshness_bars",
    "smc_order_block_touch_bullish_swing_freshness_bars",
    "smc_order_block_touch_bearish_internal_freshness_bars",
    "smc_order_block_touch_bearish_swing_freshness_bars",
    "smc_eqh_freshness_bars",
    "smc_eql_freshness_bars",
]


def _stable_hash(obj: Any) -> str:
    """确定性 SHA256（前 16 字符），用于 raw immutability 前后对比。"""
    s = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _freshness_from_dto(bars: pd.DataFrame, smc_dto: dict[str, Any]) -> dict[str, Any]:
    """DTO → 14 个 freshness 因子（精确复刻 structural_factor_service
    `_compute_smc_freshness_factors` 的 DTO→因子 部分；≥250 gate 与生产一致）。

    生产函数（structural_factor_service.L1516-L1621）结构：
        result = {14 个 key: None}
        if len(bars) < 250: return result          # gate
        smc_dto = compute_smc_adapter(bars, display_bars=len(bars))
        ... 消费 smc_dto.events / order_blocks / equal_highs_lows ...
    本函数接收已构造好的 smc_dto，其余逻辑逐行等价。
    """
    result: dict[str, Any] = {k: None for k in _FRESHNESS_KEYS}

    if bars is None or bars.empty or len(bars) < SMC_FRESHNESS_MIN_BARS:
        return result

    current_index = len(bars) - 1

    # --- BOS/CHoCH: 按 bullish/bearish × internal/swing 拆分 ---
    bos_choch_subtypes: dict[str, int] = {}
    for e in smc_dto.get("events", []):
        etype = e.get("type")
        if etype not in ("BOS", "CHoCH"):
            continue
        bullish = e.get("bullish")
        internal = e.get("internal")
        confirmed_idx = e.get("confirmed_index")
        if bullish is None or internal is None or confirmed_idx is None:
            continue
        direction = "bullish" if bullish else "bearish"
        level = "internal" if internal else "swing"
        key = f"{etype.lower()}_{direction}_{level}"
        idx = int(confirmed_idx)
        if key not in bos_choch_subtypes or idx > bos_choch_subtypes[key]:
            bos_choch_subtypes[key] = idx

    for key, best_idx in bos_choch_subtypes.items():
        factor_key = f"smc_{key}_freshness_bars"
        if factor_key in result:
            result[factor_key] = current_index - best_idx

    # --- OB touch: 按 bullish/bearish × internal/swing 拆分 ---
    bars_high = bars["high"].to_numpy(dtype=float)
    bars_low = bars["low"].to_numpy(dtype=float)
    ob_touch_subtypes: dict[str, int] = {}
    for ob in smc_dto.get("order_blocks", []):
        ob_high = ob.get("bar_high")
        ob_low = ob.get("bar_low")
        confirmed_idx = ob.get("confirmed_index")
        bias = ob.get("bias")
        internal = ob.get("internal")
        if ob_high is None or ob_low is None or confirmed_idx is None:
            continue
        if bias is None or internal is None:
            continue
        ob_high_f = float(ob_high)
        ob_low_f = float(ob_low)
        start_idx = int(confirmed_idx) + 1
        direction = "bullish" if bias == 1 else "bearish"
        level = "internal" if internal else "swing"
        key = f"order_block_touch_{direction}_{level}"
        first_touch = -1
        for i in range(start_idx, len(bars)):
            if bars_high[i] >= ob_low_f and bars_low[i] <= ob_high_f:
                first_touch = i
                break
        if first_touch >= 0:
            if key not in ob_touch_subtypes or first_touch > ob_touch_subtypes[key]:
                ob_touch_subtypes[key] = first_touch

    for key, best_idx in ob_touch_subtypes.items():
        factor_key = f"smc_{key}_freshness_bars"
        if factor_key in result:
            result[factor_key] = current_index - best_idx

    # --- EQH/EQL: 无 internal/swing 字段，单因子 ---
    for eqhl in smc_dto.get("equal_highs_lows", []):
        etype = eqhl.get("type")
        confirmed_idx = eqhl.get("confirmed_index")
        if etype is None or confirmed_idx is None:
            continue
        factor_key = f"smc_{etype.lower()}_freshness_bars"
        if factor_key not in result:
            continue
        idx = int(confirmed_idx)
        if result[factor_key] is None or idx > (current_index - result[factor_key]):
            result[factor_key] = current_index - idx

    return result


def _load_bars() -> dict[str, pd.DataFrame]:
    bars = pd.read_parquet(PARQUET_DIR / "bars_daily.parquet")
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    out: dict[str, pd.DataFrame] = {}
    for iid, g in bars.groupby("instrument_id"):
        g = g.sort_values("trade_date").set_index("trade_date")
        g = g[["open", "high", "low", "close", "volume", "amount"]].astype(float)
        out[iid] = g
    return out


def _run_instrument(df_1d: pd.DataFrame) -> dict[str, Any]:
    """对单 instrument 执行三个子证明，返回逐项结论。"""
    from app.services.core_artifact_service import compute_core_kernel_bundle
    from app.services.structural_factor_service import _compute_smc_freshness_factors
    from app.services.canonical_adapters import compute_smc_adapter
    from app.services.smc_view_adapter import adapt_smc_to_display_dto
    from app.services.canonical_computation_service import CanonicalComputationService
    from app.strategy_assets.algorithms.features.merged_dsa_atr_rope_bb_factors import (
        compute_bollinger,
    )

    _shared_raw = compute_core_kernel_bundle(df_1d, None)
    smc_raw = _shared_raw.smc_result

    # ================= 1. SMC freshness parity =================
    A = _compute_smc_freshness_factors(df_1d)  # production baseline
    a_all_none = all(v is None for v in A.values())

    dto_baseline = compute_smc_adapter(df_1d, display_bars=len(df_1d))
    b0 = _freshness_from_dto(df_1d, dto_baseline)  # replication self-check
    repl_ok = A == b0

    # candidate：从 shared raw 构造同一 DTO（display_bars=len(bars) → offset=0）
    hash_before = _stable_hash(smc_raw)
    dto_candidate = adapt_smc_to_display_dto(smc_raw, len(df_1d))
    b1 = _freshness_from_dto(df_1d, dto_candidate)
    hash_after = _stable_hash(smc_raw)

    parity_ok = A == b1
    immut_ok = hash_before == hash_after
    dto_identical = dto_baseline == dto_candidate

    # ================= 3. BB common-field parity =================
    # canonical #2 生产路径：CanonicalComputationService.compute(algorithm_id="bollinger")
    bb1 = _shared_raw.bb_df

    async def _bb2_payload():
        res = await CanonicalComputationService.compute(
            algorithm_id="bollinger",
            instrument_id=uuid.UUID(int=0),
            as_of=df_1d.index[-1].date().isoformat(),
            source_bar_hash=None,
            adj_factor_hash=None,
            bars=df_1d,
            length=BB_WIN,
            mult=BB_K,
        )
        return res.payload

    bb2 = asyncio.run(_bb2_payload())

    bb_checks: dict[str, dict[str, Any]] = {}
    bb_all_exact = True
    for field in COMMON_BB_FIELDS:
        s1 = bb1[field]
        s2 = bb2[field]
        mask = s1.notna() & s2.notna()
        max_diff = float((s1[mask] - s2[mask]).abs().max()) if mask.any() else 0.0
        nan_mismatch = int(s1.isna().ne(s2.isna()).sum())
        exact = max_diff == 0.0 and nan_mismatch == 0
        bb_all_exact = bb_all_exact and exact
        bb_checks[field] = {"max_abs_diff": max_diff, "nan_mismatch": nan_mismatch, "exact": exact}

    return {
        "smc_eligible": len(df_1d) >= SMC_FRESHNESS_MIN_BARS,
        "a_all_none": a_all_none,
        "replication_self_check": repl_ok,
        "smc_parity": parity_ok,
        "smc_dto_identical": dto_identical,
        "raw_immutability": immut_ok,
        "raw_hash_before": hash_before,
        "raw_hash_after": hash_after,
        "bb_common_field_parity": bb_all_exact,
        "bb_field_checks": bb_checks,
        "freshness_baseline": {k: A[k] for k in _FRESHNESS_KEYS if A[k] is not None},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3.4A-2 reuse safety proof")
    ap.add_argument("--count", type=int, default=100, help="主 sample 运行数量")
    ap.add_argument("--include-boundary", action="store_true", help="同时运行 boundary sample")
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not MANIFEST_PATH.exists():
        sys.exit(f"manifest 不存在: {MANIFEST_PATH}（先运行 build_manifest.py）")
    rows = [json.loads(line) for line in open(MANIFEST_PATH, encoding="utf-8")]
    main_rows = [r for r in rows if r["selection_reason"] == "main_ge250"]
    boundary_rows = [r for r in rows if r["selection_reason"] == "boundary_60_249"]
    selected = main_rows[: args.count]
    if args.include_boundary:
        selected = selected + boundary_rows
    print(f"running reuse safety proof on {len(selected)} instruments "
          f"(main {len(main_rows[:args.count])}, boundary {len(boundary_rows) if args.include_boundary else 0})")

    bars_by_id = _load_bars()

    results: list[dict[str, Any]] = []
    for row in selected:
        iid = row["instrument_id"]
        if iid not in bars_by_id:
            continue
        df_all = bars_by_id[iid]
        trade_date = date.fromisoformat(row["max_trade_date"])
        df_1d = df_all[df_all.index.date <= trade_date]
        try:
            checks = _run_instrument(df_1d)
        except Exception as exc:  # noqa: BLE001
            checks = {
                "error": f"{type(exc).__name__}: {exc}",
                "smc_eligible": len(df_1d) >= SMC_FRESHNESS_MIN_BARS,
                "replication_self_check": False,
                "smc_parity": False,
                "smc_dto_identical": False,
                "raw_immutability": False,
                "bb_common_field_parity": False,
                "bb_field_checks": {},
                "freshness_baseline": {},
            }
        row_out = {
            "instrument_id": iid,
            "symbol": row.get("symbol"),
            "market": row.get("market"),
            "bars_count": int(len(df_1d)),
            "selection_reason": row["selection_reason"],
        }
        row_out.update(checks)
        results.append(row_out)

    # 汇总（只对 smc_eligible 计 parity；raw immutability 全量计）
    total = len(results)
    repl_ok_n = sum(1 for r in results if r.get("replication_self_check") and not r.get("error"))
    parity_n = sum(1 for r in results if r.get("smc_eligible") and r.get("smc_parity"))
    parity_den = sum(1 for r in results if r.get("smc_eligible"))
    immut_ok_n = sum(1 for r in results if r.get("raw_immutability"))
    bb_ok_n = sum(1 for r in results if r.get("bb_common_field_parity"))
    dto_iden_n = sum(1 for r in results if r.get("smc_dto_identical"))

    out = {
        "phase": "3.4A-2",
        "method": "reuse safety proof（baseline vs raw-based；hash before/after；BB common-field equivalence）",
        "summary": {
            "total": total,
            "replication_self_check_ok": f"{repl_ok_n}/{total}",
            "smc_parity_ok": f"{parity_n}/{parity_den}（仅 eligible）",
            "smc_dto_identical": f"{dto_iden_n}/{total}",
            "raw_immutability_ok": f"{immut_ok_n}/{total}",
            "bb_common_field_parity_ok": f"{bb_ok_n}/{total}",
            "errors": sum(1 for r in results if r.get("error")),
        },
        "results": results,
    }
    out_path = OUTPUT_DIR / "reuse_safety_proof.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
    print(f"output: {out_path}")
    failed = [r for r in results if not (r.get("replication_self_check") and r.get("smc_parity")
                                         and r.get("raw_immutability") and r.get("bb_common_field_parity"))
              and not r.get("error")]
    if failed:
        print("NON-PASS instruments:")
        for r in failed:
            print(" ", r.get("symbol"), r.get("selection_reason"), r.get("bars_count"), r.get("error", ""))
    print("\nNOTE: PRODUCTION_CODE_DIFF = ZERO（本脚本未修改任何生产代码）")


if __name__ == "__main__":
    main()
