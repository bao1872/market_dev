#!/usr/bin/env python3
"""Phase 3.4B-A — Canonical Historical Segment Artifact Feasibility Audit（只读，不实现）。

背景（3.4B-0 已证）：
- temporal `_collect_historical_segment_ages` 需要 full-history 已完成 DSA segments 的
  ages，用于当前 segment duration percentile；250-bar `_shared_raw.dsa_bundle` 不能替代
  （PARITY FAIL 5/105）。
- 现状：temporal 调 `compute_dsa_bundle(bars, {})`（full-history，约 122ms）然后从
  `visual_segments` + `_find_bar_index_by_time` 重建段边界。

本审计只回答三个问题（不改 production code）：
1. full-history DSA canonical 计算内部是否已产生 temporal 需要的 segment 信息？
   → `compute_dsa_history`（SSOT）已产出逐 bar segment contract
     （segment_id/segment_start_bar_index/segment_end_bar_index/segment_start_time/
     segment_end_time/segment_bars...），且 `compute_dsa_bundle.factor_per_bar` 直接携带。
2. 能否无第二 owner 暴露为 immutable artifact？
   → artifact 已存在于单一 canonical producer 输出；temporal 消费 segment 字段即符合
     dsa_selector.py Phase 5 契约注释 "Consumers must read these fields instead of
     rebuilding boundaries from visual_segments"。不新建第二套 DSA 实现。
3. 产生该 artifact 的成本与可省成本？
   → 测量 `compute_dsa_bundle(bars,{})` vs `compute_dsa_history(bars,{})` elapsed 差
     （= compute_dsa_bundle 内第二次 dynamic_swing_anchored_vwap + per_bar 视觉组装，
     temporal 不需要）。

Parity Gate（与 3.4B-0 同标准）：
- ages_visual（现状，经 3.4B-0 replication 105/105 验证） == ages_segment（canonical
  segment 字段直接提取）100% → parity PASS，temporal 可切换消费 canonical segment 字段
- 任意一只有差异 → parity FAIL：visual_segments（raw dir 翻转）与 segment_id
  （lookahead 修正后 dir 翻转）分段不一致，是产品语义问题而非纯性能问题，必须上报
Cost Gate：
- t_history 显著 < t_bundle → 存在可省成本（第二次 DSA-VWAP + 视觉组装）
- t_history ≈ t_bundle → 122ms 基本是 full-history DSA state 的合法成本 → 方案 A STOP

方法学：不修改任何生产代码（PRODUCTION_CODE_DIFF = ZERO）；只读消费 frozen dataset；
不连远程 DB；不做 PG migration / SQL 实验。只证明，不实现。

Usage:
    cd backend
    PYTHONPATH=. .venv/bin/python ../experiments/duplicate_compute_audit/segment_artifact_feasibility.py \
        --count 100 [--include-boundary] [--reps 3]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
PARQUET_DIR = (
    REPO_ROOT / "backend" / ".perfdata" / "review" / "review-source-c5c686e-v1" / "parquet"
)
OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "3.4B-A"
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


# ---------------------------------------------------------------------------
# 现状：temporal 从 visual_segments 重建段 ages（replication 已在 3.4B-0 验证 105/105）
# ---------------------------------------------------------------------------
def _extract_ages_from_bundle(dsa_bundle: dict[str, Any]) -> list[int]:
    """复刻 temporal_feature_service._collect_historical_segment_ages 的 bundle 消费部分。"""
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
            ages.append(int(end_idx - start_idx + 1))
    return ages


def _extract_boundaries_from_visual(dsa_bundle: dict[str, Any]) -> list[tuple[int, int]]:
    """现状：visual_segments 每段的 (start_bar_index, end_bar_index)。"""
    visual_segments = dsa_bundle.get("visual_segments", [])
    factor_per_bar = dsa_bundle.get("factor_per_bar")
    if factor_per_bar is None or factor_per_bar.empty or len(visual_segments) <= 1:
        return []

    from app.services.structural_factor_service import _find_bar_index_by_time

    out: list[tuple[int, int]] = []
    for seg in visual_segments[:-1]:
        points = seg.get("points", [])
        if len(points) < 2:
            continue
        s = _find_bar_index_by_time(factor_per_bar.index, points[0].get("time"))
        e = _find_bar_index_by_time(factor_per_bar.index, points[-1].get("time"))
        if s is not None and e is not None and e >= s:
            out.append((int(s), int(e)))
    return out


# ---------------------------------------------------------------------------
# 候选：从 canonical segment 字段直接提取（factor_per_bar / history 已携带）
# ---------------------------------------------------------------------------
def _extract_ages_from_segment_fields(per_bar: pd.DataFrame) -> list[int]:
    """从 canonical segment 字段提取历史已完成段 ages。

    语义：segment_id 分组，每段末 bar 的 segment_bars（= 段长），排除当前段（最后一段）；
    与 temporal 现状对齐：跳过段长 < 2 的段（visual_segments 只冻结 >=2 点的段）。
    仅消费 segment_direction != 0 的真实方向段：canonical 段字段由 dir_vals.fillna(0)
    驱动 group_id，首段 warmup（dir==0）会被编号为 segment，但它不是 DSA 段，
    visual_segments 也不包含它；忠实消费者切到 canonical 字段后同样必须过滤方向 0。
    """
    if per_bar is None or per_bar.empty or "segment_id" not in per_bar.columns:
        return []
    cols = ["segment_id", "segment_direction", "segment_bars"]
    df = per_bar[cols].dropna()
    if df.empty:
        return []
    df = df[df["segment_direction"] != 0]
    if df.empty:
        return []
    seg_ages = df.groupby("segment_id")["segment_bars"].last().astype(int)
    if len(seg_ages) <= 1:
        return []
    completed = seg_ages.iloc[:-1]
    return [int(a) for a in completed if a >= 2]


def _extract_boundaries_from_segment_fields(per_bar: pd.DataFrame) -> list[tuple[int, int]]:
    """候选：canonical segment 字段每段的 (start_bar_index, end_bar_index)。"""
    if per_bar is None or per_bar.empty or "segment_id" not in per_bar.columns:
        return []
    cols = ["segment_id", "segment_direction", "segment_start_bar_index", "segment_end_bar_index"]
    df = per_bar[cols].dropna()
    if df.empty:
        return []
    df = df[df["segment_direction"] != 0]
    if df.empty:
        return []
    out: list[tuple[int, int]] = []
    for _gid, grp in df.groupby("segment_id"):
        s = int(grp["segment_start_bar_index"].iloc[0])
        e = int(grp["segment_end_bar_index"].iloc[-1])
        if e >= s and (e - s + 1) >= 2:
            out.append((s, e))
    if len(out) <= 1:
        return []
    return out[:-1]  # 排除当前段


def _duration_percentile(
    current_age: int | None, ages: list[int]
) -> float | None:
    from app.services.structural_factor_service import percentile_rank

    if current_age is None or len(ages) < MIN_SEGMENTS_FOR_PERCENTILE:
        return None
    return percentile_rank(
        float(current_age), np.array(ages, dtype=float), len(ages)
    )


def _timed(fn: Callable[[], Any], warmup: int = 1, reps: int = 3) -> float:
    """elapsed wall time p50（ms）。"""
    for _ in range(warmup):
        fn()
    ts: list[float] = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    return float(np.median(ts)) * 1000.0


def _run_instrument(df_1d: pd.DataFrame, reps: int) -> dict[str, Any]:
    from app.services.first_pyramid_service import DSA_LOOKBACK, MIN_DIR_BARS
    from app.strategy.selectors.dsa_selector import compute_dsa_bundle, compute_dsa_history

    # 现状：temporal 完整 bundle（full-history）
    bundle_full = compute_dsa_bundle(df_1d, {})

    # 候选数据源：canonical segment 字段（factor_per_bar 携带；独立用 history 复核）
    per_bar = bundle_full.get("factor_per_bar")
    history_full = compute_dsa_history(df_1d, {})

    ages_visual = _extract_ages_from_bundle(bundle_full)
    boundaries_visual = _extract_boundaries_from_visual(bundle_full)

    ages_segment = _extract_ages_from_segment_fields(per_bar)
    boundaries_segment = _extract_boundaries_from_segment_fields(per_bar)
    # 一致性：从独立 history 调用提取必须与 factor_per_bar 相同（防实现 bug）
    ages_segment_hist = _extract_ages_from_segment_fields(history_full)
    hist_consistent = ages_segment == ages_segment_hist

    # current_age：与 3.4B-0 同源（250-bar contract last_row_metrics.segment_bars）
    dsa_config_250 = {"min_dir_bars": MIN_DIR_BARS, "lookback": DSA_LOOKBACK}
    bundle_250 = compute_dsa_bundle(df_1d, dsa_config_250)
    current_age = (bundle_250.get("last_row_metrics") or {}).get("segment_bars")
    current_age = int(current_age) if current_age is not None else None

    pct_visual = _duration_percentile(current_age, ages_visual)
    pct_segment = _duration_percentile(current_age, ages_segment)

    # 成本：bundle vs history（可省成本 = bundle - history，第二次 DSA-VWAP + 视觉组装）
    t_bundle = _timed(lambda: compute_dsa_bundle(df_1d, {}), warmup=1, reps=reps)
    t_history = _timed(lambda: compute_dsa_history(df_1d, {}), warmup=1, reps=reps)

    return {
        "bars_count": int(len(df_1d)),
        # parity
        "ages_equal": ages_visual == ages_segment,
        "pct_equal": pct_visual == pct_segment,
        "boundaries_equal": sorted(boundaries_visual) == sorted(boundaries_segment),
        "hist_consistent": hist_consistent,
        "n_ages_visual": len(ages_visual),
        "n_ages_segment": len(ages_segment),
        "ages_visual": ages_visual,
        "ages_segment": ages_segment,
        "current_age": current_age,
        "pct_visual": pct_visual,
        "pct_segment": pct_segment,
        # cost
        "t_bundle_ms": t_bundle,
        "t_history_ms": t_history,
        "t_save_ms": round(t_bundle - t_history, 3),
        "t_save_share": (
            round((t_bundle - t_history) / t_bundle, 4) if t_bundle > 0 else 0.0
        ),
        "parity_ok": (
            ages_visual == ages_segment
            and pct_visual == pct_segment
            and sorted(boundaries_visual) == sorted(boundaries_segment)
            and hist_consistent
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Phase 3.4B-A canonical historical segment artifact feasibility"
    )
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--include-boundary", action="store_true")
    ap.add_argument("--reps", type=int, default=3, help="timing reps（每样本）")
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
    print(f"running segment artifact feasibility on {len(selected)} instruments "
          f"(main {len(main_rows[:args.count])}, "
          f"boundary {len(boundary_rows) if args.include_boundary else 0}, reps={args.reps})")

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
            checks = _run_instrument(df_1d, args.reps)
        except Exception as exc:  # noqa: BLE001
            checks = {
                "bars_count": int(len(df_1d)),
                "error": f"{type(exc).__name__}: {exc}",
                "ages_equal": False,
                "pct_equal": False,
                "boundaries_equal": False,
                "hist_consistent": False,
                "n_ages_visual": 0,
                "n_ages_segment": 0,
                "ages_visual": [],
                "ages_segment": [],
                "current_age": None,
                "pct_visual": None,
                "pct_segment": None,
                "t_bundle_ms": None,
                "t_history_ms": None,
                "t_save_ms": None,
                "t_save_share": None,
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
    n_bnd = sum(1 for r in results if r.get("boundaries_equal"))
    n_hist = sum(1 for r in results if r.get("hist_consistent"))
    fails = [r for r in results if not r.get("parity_ok")]

    t_bundle_vals = [r["t_bundle_ms"] for r in results if r.get("t_bundle_ms") is not None]
    t_history_vals = [r["t_history_ms"] for r in results if r.get("t_history_ms") is not None]
    t_save_vals = [r["t_save_ms"] for r in results if r.get("t_save_ms") is not None]
    t_save_share_vals = [
        r["t_save_share"] for r in results if r.get("t_save_share") is not None
    ]

    def _p50(vals: list[float]) -> float:
        return float(np.median(vals)) if vals else 0.0

    summary = {
        "phase": "3.4B-A",
        "method": "compare temporal ages (visual_segments reconstruction) vs canonical "
                  "segment fields (segment_id/segment_bars) on full-history DSA; "
                  "time compute_dsa_bundle vs compute_dsa_history",
        "parity_gate": "100% ages + pct + boundaries parity required for switching "
                       "temporal to canonical segment fields",
        "cost_gate": "t_history << t_bundle => real avoidable cost; "
                     "t_history ~= t_bundle => 41% is legit full-history DSA cost (STOP)",
        "n_total": n_total,
        "n_parity_ok": n_parity,
        "n_ages_equal": n_ages,
        "n_pct_equal": n_pct,
        "n_boundaries_equal": n_bnd,
        "n_hist_consistent": n_hist,
        "parity_ratio": f"{n_parity}/{n_total}",
        "timing_p50_ms": {
            "compute_dsa_bundle": round(_p50(t_bundle_vals), 2),
            "compute_dsa_history": round(_p50(t_history_vals), 2),
            "avoidable (bundle - history)": round(_p50(t_save_vals), 2),
            "avoidable_share_p50": round(float(np.median(t_save_share_vals)), 4)
            if t_save_share_vals else 0.0,
        },
        "decision": (
            "PARITY PASS + real avoidable cost -> temporal can consume canonical "
            "segment fields (implementation phase); "
            if (n_total and n_parity == n_total and _p50(t_save_vals) > 5.0)
            else
            "PARITY FAIL -> visual_segments vs segment_id segmentation differ "
            "(product-semantics issue); or no avoidable cost -> 41% is legit "
            "full-history DSA cost, pivot to Artifact/summary + parallelism"
        ),
        "failures": [
            {
                "instrument_id": r.get("instrument_id"),
                "symbol": r.get("symbol"),
                "selection_reason": r.get("selection_reason"),
                "bars_count": r.get("bars_count"),
                "n_ages_visual": r.get("n_ages_visual"),
                "n_ages_segment": r.get("n_ages_segment"),
                "ages_equal": r.get("ages_equal"),
                "pct_equal": r.get("pct_equal"),
                "boundaries_equal": r.get("boundaries_equal"),
                "pct_visual": r.get("pct_visual"),
                "pct_segment": r.get("pct_segment"),
                "t_bundle_ms": r.get("t_bundle_ms"),
                "t_history_ms": r.get("t_history_ms"),
                "error": r.get("error"),
            }
            for r in fails
        ],
    }

    out_path = OUTPUT_DIR / "segment_artifact_feasibility.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    per_path = OUTPUT_DIR / "per_instrument.jsonl"
    with open(per_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    print(f"\nparity: {n_parity}/{n_total}  (ages {n_ages}, pct {n_pct}, "
          f"boundaries {n_bnd}, hist-consistent {n_hist})")
    print(f"timing p50: bundle={summary['timing_p50_ms']['compute_dsa_bundle']}ms "
          f"history={summary['timing_p50_ms']['compute_dsa_history']}ms "
          f"avoidable={summary['timing_p50_ms']['avoidable (bundle - history)']}ms "
          f"({summary['timing_p50_ms']['avoidable_share_p50']})")
    print(f"decision: {summary['decision']}")
    if fails:
        print(f"\n{len(fails)} parity failures:")
        for r in fails[:10]:
            print(f"  {r.get('symbol')}: n_visual={r.get('n_ages_visual')} "
                  f"n_segment={r.get('n_ages_segment')} pct_v={r.get('pct_visual')} "
                  f"pct_s={r.get('pct_segment')} {r.get('error') or ''}")
    print(f"output: {out_path}")


if __name__ == "__main__":
    main()
