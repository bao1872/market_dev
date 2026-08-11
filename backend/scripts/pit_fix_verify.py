"""HISTORY-BACKFILL-PIT-01 Round 2 远程 targeted verification。

比较三路：
  A. TRUE PIT recompute (adjustment_as_of=t)
  B. GLOBAL one-pass raw (adjustment_as_of=None)
  C. FIXED normalization output (B + normalize_history_result_to_pit)

要求 A vs C: EXACT parity on all business/persisted fields + event payload。
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, "/app")

from app.db import AsyncSessionLocal
from app.services.first_pyramid_service import (
    compute_first_pyramid_history,
    normalize_history_result_to_pit,
)
from app.services.market_data_aggregation_service import MarketDataAggregationService

TEST_CASES = [
    ("300592", date(2026, 5, 15)),
    ("300592", date(2026, 3, 15)),
    ("000032", date(2026, 5, 15)),
    ("000032", date(2026, 3, 15)),
    ("603379", date(2026, 5, 15)),
    ("603379", date(2026, 3, 15)),
    ("600519", date(2026, 5, 15)),   # control
    ("000001", date(2026, 5, 15)),   # control
]

OUTPUT_BARS = 250
TOL = 1e-10


async def _get_instrument_id(session, symbol: str) -> str:
    from sqlalchemy import text
    row = await session.execute(
        text("SELECT id FROM instruments WHERE symbol = :s"), {"s": symbol},
    )
    result = row.scalar()
    return str(result) if result else ""


async def _fetch_bars(session, symbol: str, target_date: date, adj_as_of: date | None) -> pd.DataFrame | None:
    import uuid
    iid_str = await _get_instrument_id(session, symbol)
    if not iid_str:
        return None
    mdas = MarketDataAggregationService()
    kwargs: dict[str, Any] = {
        "timeframe": "1d", "adj": "qfq",
        "include_realtime": False, "completed_only": True,
        "allow_backfill": False,
        "end_date": target_date,
        "limit": 500,
    }
    if adj_as_of is not None:
        kwargs["adjustment_as_of"] = adj_as_of
    result = await mdas.get_bars(session, uuid.UUID(iid_str), **kwargs)
    return result.bars if result.bars is not None else None


def _compare_deep(a: Any, b: Any, path: str = "$") -> list[str]:
    """递归比较两个值，返回差异列表。"""
    diffs: list[str] = []
    if type(a) != type(b):
        diffs.append(f"{path}: TYPE {type(a).__name__} vs {type(b).__name__}")
        return diffs
    if isinstance(a, dict):
        all_keys = set(a.keys()) | set(b.keys())
        for k in sorted(all_keys):
            va = a.get(k)
            vb = b.get(k)
            if va is None and vb is None:
                continue
            diffs.extend(_compare_deep(va, vb, f"{path}.{k}"))
    elif isinstance(a, list):
        if len(a) != len(b):
            diffs.append(f"{path}: LEN {len(a)} vs {len(b)}")
        else:
            for i, (va, vb) in enumerate(zip(a, b)):
                diffs.extend(_compare_deep(va, vb, f"{path}[{i}]"))
    elif isinstance(a, float) and isinstance(b, float):
        if np.isnan(a) and np.isnan(b):
            return diffs
        if a == 0 and b == 0:
            return diffs
        abs_err = abs(a - b)
        rel_err = abs(a - b) / max(abs(a), abs(b), TOL)
        if abs_err > TOL and rel_err > TOL:
            diffs.append(f"{path}: {a:.12f} vs {b:.12f} (abs={abs_err:.2e}, rel={rel_err:.2e})")
    elif a != b:
        diffs.append(f"{path}: {a!r} vs {b!r}")
    return diffs


def _count_event_types(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        t = e.get("type", "UNKNOWN")
        counts[t] = counts.get(t, 0) + 1
    return counts


async def run_case(session, symbol: str, target_date: date) -> dict:
    # A: TRUE PIT
    bars_pit = await _fetch_bars(session, symbol, target_date, adj_as_of=target_date)
    # B: GLOBAL one-pass
    bars_global = await _fetch_bars(session, symbol, target_date, adj_as_of=None)

    result: dict[str, Any] = {"symbol": symbol, "target_date": target_date.isoformat()}

    if bars_pit is None or bars_pit.empty or bars_global is None or bars_global.empty:
        result["error"] = "no_bars"
        return result

    n_pit = len(bars_pit)
    n_global = len(bars_global)

    if n_pit < 60 or n_global < 60:
        result["error"] = "insufficient_bars"
        return result

    result["n_bars"] = n_pit
    pit_dates = set(bars_pit.index.date)
    global_dates = set(bars_global.index.date)
    result["same_date_set"] = pit_dates == global_dates

    # A: TRUE PIT compute
    hist_pit = compute_first_pyramid_history(bars_pit, symbol=symbol, output_bars=OUTPUT_BARS, include_chip=False)

    # B: GLOBAL one-pass compute
    hist_global = compute_first_pyramid_history(bars_global, symbol=symbol, output_bars=OUTPUT_BARS, include_chip=False)

    # C: FIXED = B + normalize_history_result_to_pit
    if "adj_factor" in bars_global.columns:
        factor_series = bars_global["adj_factor"].dropna()
        anchor_factor = float(factor_series.iloc[-1]) if not factor_series.empty else 1.0
        hist_fixed = normalize_history_result_to_pit(
            dict(hist_global),  # shallow copy to avoid mutating original
            factor_series=factor_series,
            anchor_factor=anchor_factor,
        )
    else:
        hist_fixed = dict(hist_global)

    # 比较 A vs C
    # 排除 input_hash（A 和 B 的 bars 不同 → hash 不同）
    pit_ds = hist_pit.get("daily_state") or []
    fixed_ds = hist_fixed.get("daily_state") or []
    pit_ev = hist_pit.get("events") or []
    fixed_ev = hist_fixed.get("events") or []

    # 只比较 output 内的 state（按 time 配对）
    pit_by_time = {s.get("time", ""): s for s in pit_ds}
    fixed_by_time = {s.get("time", ""): s for s in fixed_ds}
    common_times = set(pit_by_time.keys()) & set(fixed_by_time.keys())

    # 比较 daily_state 的 business fields（排除 input_hash, meta）
    skip_keys = {"input_hash", "parameter_hash_core", "_debug"}
    daily_diffs = []
    for t in sorted(common_times):
        ps = pit_by_time[t]
        fs = fixed_by_time[t]
        for k in sorted(ps.keys()):
            if k in skip_keys:
                continue
            va = ps.get(k)
            vb = fs.get(k)
            diffs_k = _compare_deep(va, vb, f"daily[{t}].{k}")
            daily_diffs.extend(diffs_k)

    # 比较 events
    pit_evt_map = {(e.get("type"), e.get("bar_index"), e.get("time", "")): e for e in pit_ev}
    fixed_evt_map = {(e.get("type"), e.get("bar_index"), e.get("time", "")): e for e in fixed_ev}
    common_evt = set(pit_evt_map.keys()) & set(fixed_evt_map.keys())

    event_diffs = []
    for key in common_evt:
        pe = pit_evt_map[key]
        fe = fixed_evt_map[key]
        for k in sorted(pe.keys()):
            if k in skip_keys:
                continue
            va = pe.get(k)
            vb = fe.get(k)
            diffs_k = _compare_deep(va, vb, f"event[{key}].{k}")
            event_diffs.extend(diffs_k)

    # 事件 identity 对比
    pit_evt_ids = set(pit_evt_map.keys())
    fixed_evt_ids = set(fixed_evt_map.keys())
    only_pit = pit_evt_ids - fixed_evt_ids
    only_fixed = fixed_evt_ids - pit_evt_ids

    result["daily_state_count"] = len(common_times)
    result["event_count_pit"] = len(pit_ev)
    result["event_count_fixed"] = len(fixed_ev)
    result["daily_diffs"] = len(daily_diffs)
    result["event_diffs"] = len(event_diffs)
    result["only_pit_events"] = len(only_pit)
    result["only_fixed_events"] = len(only_fixed)
    result["event_types_pit"] = _count_event_types(pit_ev)
    result["FULL_PARITY"] = (len(daily_diffs) == 0 and len(event_diffs) == 0
                              and len(only_pit) == 0 and len(only_fixed) == 0)

    if daily_diffs:
        result["daily_diff_samples"] = daily_diffs[:10]
    if event_diffs:
        result["event_diff_samples"] = event_diffs[:10]

    return result


async def main():
    async with AsyncSessionLocal() as session:
        summary = []
        for sym, td in TEST_CASES:
            result = await run_case(session, sym, td)
            summary.append({
                k: v for k, v in result.items()
                if k in ("symbol", "target_date", "n_bars", "same_date_set",
                         "daily_state_count", "event_count_pit", "event_count_fixed",
                         "daily_diffs", "event_diffs", "only_pit_events", "only_fixed_events",
                         "event_types_pit", "FULL_PARITY", "error",
                         "daily_diff_samples", "event_diff_samples")
            })
        print(json.dumps(summary, default=str, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
