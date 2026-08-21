#!/usr/bin/env python3
"""Phase 3.4B-0 — DSA #2 Contract Equivalence Proof（只证明，不实现）。

背景（第四轮审查修正）：
- Core DSA #1 = `compute_dsa_bundle(bars, {"min_dir_bars": MIN_DIR_BARS, "lookback": DSA_LOOKBACK})`
  其中 DSA_LOOKBACK = 250（indicator_contract.py）→ 截断最近 250 bar 再计算。
- Temporal DSA #2 = `_collect_historical_segment_ages(bars)` 内部
  `compute_dsa_bundle(bars, {})` → `lookback=None`，不截断，全历史（415 bar）。
- 因此两者是 **SAME_KERNEL_DIFFERENT_CONTRACT**，不是 EXACT_DUPLICATE。
- `_collect_historical_segment_ages` 的目标是收集**全历史已完成 DSA segments** 的
  持续时长（`visual_segments[:-1]`），用于当前 segment duration percentile。

本实验对 fixed sample_manifest 每只股票比较：
    A = compute_dsa_bundle(bars, {"min_dir_bars": MIN_DIR_BARS, "lookback": 250})   # 250-bar contract
    B = compute_dsa_bundle(bars, {})                                               # full-history contract
比较：
    1. `_collect_historical_segment_ages` 最终消费的 ages 数组：ages_250 vs ages_full
       （复刻生产提取逻辑，并用 `_collect_historical_segment_ages(bars)` 做 replication self-check，
        避免自证）
    2. 最终 `daily_dsa_segment_duration_percentile_250` vs `_full`
       （current_age 取自生产同源：250-bar structural dsa_segment 的 current_dsa_segment_age_bars）

Gate（用户第四轮审查明确）：
    100% parity → 才允许 raw reuse
    任意一只有差异 → 禁止直接复用（不接受 99.9% / 误差很小 / 大部分一样）

方法学：
- 不修改任何生产代码（PRODUCTION_CODE_DIFF = ZERO）
- 只读消费 frozen dataset；不连远程 DB；不做 PG migration / SQL 实验
- 只证明，不实现复用

Usage:
    cd backend
    PYTHONPATH=. .venv/bin/python ../experiments/duplicate_compute_audit/dsa_contract_parity.py \
        --count 100 [--include-boundary]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
PARQUET_DIR = (
    REPO_ROOT / "backend" / ".perfdata" / "review" / "review-source-c5c686e-v1" / "parquet"
)
OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "3.4B-0"
MANIFEST_PATH = (
    Path(__file__).resolve().parent / "output" / "3.4A-0" / "sample_manifest.jsonl"
)

MIN_SEGMENTS_FOR_PERCENTILE = 5  # temporal_feature_service._MIN_SEGMENTS_FOR_PERCENTILE


def _load_bars() -> dict[str, pd.DataFrame]:
    bars = pd.read_parquet(PARQUET_DIR / "bars_daily.parquet")
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    out: dict[str, pd.DataFrame] = {}
    for iid, g in bars.groupby("instrument_id"):
        g = g.sort_values("trade_date").set_index("trade_date")
        g = g[["open", "high", "low", "close", "volume", "amount"]].astype(float)
        out[iid] = g
    return out


def _extract_ages_from_bundle(dsa_bundle: dict[str, Any]) -> list[int]:
    """从 dsa_bundle 提取历史已完成 segments 的 ages（复刻
    temporal_feature_service._collect_historical_segment_ages 的 bundle 消费部分）。
    """
    visual_segments = dsa_bundle.get("visual_segments", [])
    factor_per_bar = dsa_bundle.get("factor_per_bar")
    if factor_per_bar is None or factor_per_bar.empty or len(visual_segments) <= 1:
        return []

    from app.services.structural_factor_service import _find_bar_index_by_time

    ages: list[int] = []
    for seg in visual_segments[:-1]:
        points = seg.get("points", [])
        if len(points) < 2:
            continue
        start_idx = _find_bar_index_by_time(
            factor_per_bar.index, points[0].get("time")
        )
        end_idx = _find_bar_index_by_time(
            factor_per_bar.index, points[-1].get("time")
        )
        if start_idx is not None and end_idx is not None and end_idx >= start_idx:
            age = end_idx - start_idx + 1
            ages.append(int(age))
    return ages


def _duration_percentile(
    current_age: int | None, ages: list[int]
) -> float | None:
    """复刻 _compute_daily_context 的 percentile 计算（同 current_age 同 ages）。"""
    from app.services.structural_factor_service import percentile_rank

    if current_age is None or len(ages) < MIN_SEGMENTS_FOR_PERCENTILE:
        return None
    return percentile_rank(
        float(current_age), np.array(ages, dtype=float), len(ages)
    )


def _run_instrument(df_1d: pd.DataFrame) -> dict[str, Any]:
    """对单 instrument 执行 DSA 250/full contract parity。"""
    from app.services.first_pyramid_service import DSA_LOOKBACK, MIN_DIR_BARS
    from app.strategy.selectors.dsa_selector import compute_dsa_bundle
    from app.services.temporal_feature_service import _collect_historical_segment_ages

    # Core DSA #1 contract（250-bar 截断）
    dsa_config_250 = {
        "min_dir_bars": MIN_DIR_BARS,
        "lookback": DSA_LOOKBACK,
    }
    bundle_250 = compute_dsa_bundle(df_1d, dsa_config_250)

    # Temporal DSA #2 contract（full-history，lookback=None）
    bundle_full = compute_dsa_bundle(df_1d, {})

    # 生产路径 replication self-check：直接从 bundle_full 提取 == 生产函数输出
    ages_full_extracted = _extract_ages_from_bundle(bundle_full)
    ages_full_prod = _collect_historical_segment_ages(df_1d)
    replication_ok = ages_full_extracted == ages_full_prod

    # candidate：若复用 _shared_raw.dsa_bundle（250-bar contract）
    ages_250 = _extract_ages_from_bundle(bundle_250)

    # current_age 取自生产同源：250-bar structural dsa_segment 的 current_dsa_segment_age_bars
    current_age = (
        bundle_250.get("last_row_metrics") or {}
    ).get("segment_bars")
    current_age = int(current_age) if current_age is not None else None

    pct_250 = _duration_percentile(current_age, ages_250)
    pct_full = _duration_percentile(current_age, ages_full_extracted)

    ages_equal = ages_250 == ages_full_extracted
    pct_equal = pct_250 == pct_full

    return {
        "bars_count": int(len(df_1d)),
        "ages_equal": ages_equal,
        "pct_equal": pct_equal,
        "replication_self_check": replication_ok,
        "n_ages_250": len(ages_250),
        "n_ages_full": len(ages_full_extracted),
        "ages_250": ages_250,
        "ages_full": ages_full_extracted,
        "current_age": current_age,
        "pct_250": pct_250,
        "pct_full": pct_full,
        "parity_ok": ages_equal and pct_equal and replication_ok,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Phase 3.4B-0 DSA #2 contract equivalence proof"
    )
    ap.add_argument("--count", type=int, default=100, help="主 sample 运行数量")
    ap.add_argument("--include-boundary", action="store_true",
                    help="同时运行 boundary sample（60-249 bars）")
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
    print(f"running DSA contract parity on {len(selected)} instruments "
          f"(main {len(main_rows[:args.count])}, "
          f"boundary {len(boundary_rows) if args.include_boundary else 0})")

    bars_by_id = _load_bars()

    results: list[dict[str, Any]] = []
    for i, row in enumerate(selected):
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
                "bars_count": int(len(df_1d)),
                "error": f"{type(exc).__name__}: {exc}",
                "ages_equal": False,
                "pct_equal": False,
                "replication_self_check": False,
                "n_ages_250": 0,
                "n_ages_full": 0,
                "current_age": None,
                "pct_250": None,
                "pct_full": None,
                "parity_ok": False,
            }
        row_out = {
            "instrument_id": iid,
            "symbol": row.get("symbol"),
            "market": row.get("market"),
            "selection_reason": row["selection_reason"],
        }
        row_out.update(checks)
        results.append(row_out)
        if (i + 1) % 25 == 0 or (i + 1) == len(selected):
            print(f"  ... {i + 1}/{len(selected)} done")

    n_total = len(results)
    n_parity = sum(1 for r in results if r.get("parity_ok"))
    n_ages = sum(1 for r in results if r.get("ages_equal"))
    n_pct = sum(1 for r in results if r.get("pct_equal"))
    n_repl = sum(1 for r in results if r.get("replication_self_check"))
    fails = [r for r in results if not r.get("parity_ok")]

    summary = {
        "phase": "3.4B-0",
        "method": "compute_dsa_bundle(250-contract) vs compute_dsa_bundle({}) on same bars; "
                  "compare extracted historical segment ages + duration percentile",
        "gate": "100% parity required for raw reuse; any difference -> forbid direct reuse",
        "n_total": n_total,
        "n_parity_ok": n_parity,
        "n_ages_equal": n_ages,
        "n_pct_equal": n_pct,
        "n_replication_ok": n_repl,
        "parity_ratio": f"{n_parity}/{n_total}",
        "decision": (
            "PARITY PASS -> raw reuse allowed"
            if n_total and n_parity == n_total
            else "PARITY FAIL -> direct reuse FORBIDDEN; investigate historical segment artifact"
        ),
        "failures": [
            {
                "instrument_id": r.get("instrument_id"),
                "symbol": r.get("symbol"),
                "selection_reason": r.get("selection_reason"),
                "bars_count": r.get("bars_count"),
                "n_ages_250": r.get("n_ages_250"),
                "n_ages_full": r.get("n_ages_full"),
                "ages_equal": r.get("ages_equal"),
                "pct_equal": r.get("pct_equal"),
                "current_age": r.get("current_age"),
                "pct_250": r.get("pct_250"),
                "pct_full": r.get("pct_full"),
                "error": r.get("error"),
            }
            for r in fails
        ],
    }

    out_path = OUTPUT_DIR / "dsa_contract_parity.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    per_path = OUTPUT_DIR / "per_instrument.jsonl"
    with open(per_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    print(f"\nparity: {n_parity}/{n_total}  (ages {n_ages}/{n_total}, "
          f"pct {n_pct}/{n_total}, replication {n_repl}/{n_total})")
    print(f"decision: {summary['decision']}")
    if fails:
        print(f"\n{len(fails)} parity failures:")
        for r in fails[:10]:
            print(f"  {r.get('symbol')}: n_ages 250={r.get('n_ages_250')} "
                  f"full={r.get('n_ages_full')} pct 250={r.get('pct_250')} "
                  f"full={r.get('pct_full')} {r.get('error') or ''}")
    print(f"output: {out_path}")


if __name__ == "__main__":
    main()
