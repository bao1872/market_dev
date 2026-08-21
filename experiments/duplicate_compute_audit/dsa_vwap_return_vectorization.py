#!/usr/bin/env python3
"""Phase 3.4B-2A — VWAP-return loop safe vectorization（dual-run + exact parity + loop bench）。

目标：只把 `compute_dsa_history` 内 VWAP 收益五列（vwap_ret_total / vwap_ret_avg /
vwap_ret_5 / vwap_ret_10 / vwap_ret_20）的 Python group loop 替换为数学等价的
pandas 向量化。指标定义 / 参数全部 HARD FREEZE（lookback / window / min_periods /
ddof / group_id / shift / NaN / 首值语义 / 排序 / dtype / 输出字段一律不动）。

关键等价性（禁止随手用 groupby.transform("first")）：
- 组内第一位置值 = strict positional first（== grp.iloc[0]），首值为 NaN/±inf/0 时
  整组五列维持 NaN，不是"第一个非空值"。
- shift(5/10/20) 必须是组内 shift（groupby(group_id).shift），禁止 vwap_vals.shift(5)
  跨 segment 污染。
- 组长 < 2 的组整组 NaN（旧 loop 直接 continue）。

硬 Gate（本脚本）：
- dualdiff：105 frozen 样本，旧 loop vs vectorized candidate，五列逐元素 exact parity
  （index / dtype / NaN mask / 值 100% identical，浮点要求 bit/equality exact，不设 tolerance）。
- benchloop：单独测量旧 loop vs vectorized 的 wall-time（不含 DSA 主体）。

方法学：不修改生产代码（dualdiff / benchloop 均为只读实验）；只读消费 frozen dataset；
不连远程 DB。候选通过后才进入生产替换 + bundle/temporal 门禁。

Usage:
    cd backend
    PYTHONPATH=. .venv/bin/python ../experiments/duplicate_compute_audit/dsa_vwap_return_vectorization.py \
        --mode dualdiff [--count 100 --include-boundary]
    ... --mode benchloop [--count 100 --include-boundary --reps 5]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from segment_artifact_feasibility import MANIFEST_PATH, _load_bars

OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "3.4B-2A"

VWAP_RET_COLS = ["vwap_ret_total", "vwap_ret_avg", "vwap_ret_5", "vwap_ret_10", "vwap_ret_20"]


# ---------------------------------------------------------------------------
# 两个实现：baseline（生产当前 loop 的原样复制） vs candidate（向量化）
# 二者接收完全相同的 (vwap_vals, group_id)，仅执行方式不同。
# ---------------------------------------------------------------------------
def _loop_impl(vwap_vals: pd.Series, group_id: pd.Series) -> dict[str, pd.Series]:
    """生产 dsa_selector.py 当前 loop 的原样复制（3.4B-1 状态，baseline）。"""
    vwap_ret_total = pd.Series(np.nan, index=vwap_vals.index, dtype=float)
    vwap_ret_avg = pd.Series(np.nan, index=vwap_vals.index, dtype=float)
    vwap_ret_5 = pd.Series(np.nan, index=vwap_vals.index, dtype=float)
    vwap_ret_10 = pd.Series(np.nan, index=vwap_vals.index, dtype=float)
    vwap_ret_20 = pd.Series(np.nan, index=vwap_vals.index, dtype=float)

    for _gid, grp in vwap_vals.groupby(group_id):
        if len(grp) < 2:
            continue
        start_val = grp.iloc[0]
        if not np.isfinite(start_val) or start_val == 0:
            continue
        idx = grp.index
        # 区间起点到当前的累计收益
        total = grp / start_val - 1.0
        vwap_ret_total.loc[idx] = total
        # 平均每 bar 收益
        vwap_ret_avg.loc[idx] = total / np.arange(1, len(grp) + 1)
        # N 期收益
        vwap_ret_5.loc[idx] = grp / grp.shift(5) - 1.0
        vwap_ret_10.loc[idx] = grp / grp.shift(10) - 1.0
        vwap_ret_20.loc[idx] = grp / grp.shift(20) - 1.0

    return {
        "vwap_ret_total": vwap_ret_total,
        "vwap_ret_avg": vwap_ret_avg,
        "vwap_ret_5": vwap_ret_5,
        "vwap_ret_10": vwap_ret_10,
        "vwap_ret_20": vwap_ret_20,
    }


def _vectorized_impl(vwap_vals: pd.Series, group_id: pd.Series) -> dict[str, pd.Series]:
    """candidate：数学等价的 pandas 向量化（严格保留 loop 语义，见模块 docstring）。"""
    vwap_ret_total = pd.Series(np.nan, index=vwap_vals.index, dtype=float)
    vwap_ret_avg = pd.Series(np.nan, index=vwap_vals.index, dtype=float)
    vwap_ret_5 = pd.Series(np.nan, index=vwap_vals.index, dtype=float)
    vwap_ret_10 = pd.Series(np.nan, index=vwap_vals.index, dtype=float)
    vwap_ret_20 = pd.Series(np.nan, index=vwap_vals.index, dtype=float)

    # 组内位置（positional 1..size）与组大小
    _pos = group_id.groupby(group_id).cumcount() + 1
    _grsize = group_id.groupby(group_id).transform("size")
    # 组内第一位置值：strict positional first（== grp.iloc[0]）。
    # 只保留 position==1 的原始值，其余置 NaN，再取组内 first（first 跳 NaN，
    # 故 position1 为 NaN 时整组 first=NaN，与 grp.iloc[0]=NaN 的 skip 语义一致）。
    _first = vwap_vals.where(_pos == 1).groupby(group_id).transform("first")
    _finite_first = pd.Series(np.isfinite(_first.to_numpy()), index=_first.index)
    _valid = (_grsize >= 2) & _finite_first & (_first != 0)

    if _valid.any():
        with np.errstate(divide="ignore", invalid="ignore"):
            _total = vwap_vals / _first - 1.0
            _avg = _total / _pos
            # 组内 shift（等价 grp.shift(5/10/20)，禁止跨 segment 污染）
            _r5 = vwap_vals / vwap_vals.groupby(group_id).shift(5) - 1.0
            _r10 = vwap_vals / vwap_vals.groupby(group_id).shift(10) - 1.0
            _r20 = vwap_vals / vwap_vals.groupby(group_id).shift(20) - 1.0
        vwap_ret_total.loc[_valid] = _total.loc[_valid]
        vwap_ret_avg.loc[_valid] = _avg.loc[_valid]
        vwap_ret_5.loc[_valid] = _r5.loc[_valid]
        vwap_ret_10.loc[_valid] = _r10.loc[_valid]
        vwap_ret_20.loc[_valid] = _r20.loc[_valid]

    return {
        "vwap_ret_total": vwap_ret_total,
        "vwap_ret_avg": vwap_ret_avg,
        "vwap_ret_5": vwap_ret_5,
        "vwap_ret_10": vwap_ret_10,
        "vwap_ret_20": vwap_ret_20,
    }


def _derive_vwap_group(bars: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """复刻 production `_compute_dsa_history_artifact` 的 vwap_vals + group_id 派生
    （config={}：dsa_config=DSAConfig()，lookback=None 不截断）。"""
    from app.strategy.selectors.dsa_selector import _remove_dsa_lookahead
    from app.strategy_assets.algorithms.features.dynamic_swing_anchored_vwap import (
        DSAConfig,
        dynamic_swing_anchored_vwap,
    )

    df = bars.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    dsa_config = DSAConfig()
    vwap_series, dir_series, _, _ = dynamic_swing_anchored_vwap(df, dsa_config)
    vwap_series, dir_series = _remove_dsa_lookahead(df, vwap_series, dir_series, dsa_config)
    dir_vals = dir_series.fillna(0).astype(int)
    change_mask = dir_vals != dir_vals.shift(1)
    change_mask.iloc[0] = True  # 第一根作为新区间起点
    group_id = change_mask.cumsum()
    return vwap_series.astype(float), group_id


# ---------------------------------------------------------------------------
# 样本加载（与 3.4B-1 相同 manifest 与 frozen dataset）
# ---------------------------------------------------------------------------
def _select(count: int, include_boundary: bool) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in open(MANIFEST_PATH, encoding="utf-8")]
    main_rows = [r for r in rows if r["selection_reason"] == "main_ge250"]
    boundary_rows = [r for r in rows if r["selection_reason"] == "boundary_60_249"]
    selected = main_rows[:count]
    if include_boundary:
        selected = selected + boundary_rows
    return selected


def _load_sample_dfs(selected: list[dict[str, Any]]) -> list[tuple[dict[str, Any], pd.DataFrame]]:
    bars_by_id = _load_bars()
    out: list[tuple[dict[str, Any], pd.DataFrame]] = []
    for row in selected:
        iid = row["instrument_id"]
        if iid not in bars_by_id:
            continue
        df_all = bars_by_id[iid]
        trade_date = date.fromisoformat(row["max_trade_date"])
        out.append((row, df_all[df_all.index.date <= trade_date]))
    return out


def _col_diff(label: str, a: pd.Series, b: pd.Series) -> dict[str, Any]:
    """逐元素 exact parity：index / dtype / NaN mask / 值（不设 tolerance）。"""
    index_ok = list(a.index) == list(b.index)
    dtype_ok = str(a.dtype) == str(b.dtype)
    a_np = a.to_numpy()
    b_np = b.to_numpy()
    nan_ok = np.array_equal(np.isnan(a_np), np.isnan(b_np))
    if index_ok and dtype_ok and nan_ok and a_np.shape == b_np.shape:
        a_clean = np.where(np.isnan(a_np), 0.0, a_np)
        b_clean = np.where(np.isnan(b_np), 0.0, b_np)
        value_ok = bool(np.array_equal(a_clean, b_clean))
        n_mismatch = int(np.count_nonzero(a_clean != b_clean)) if not value_ok else 0
        max_abs = (
            float(np.max(np.abs(a_clean - b_clean))) if not value_ok and a_np.size else 0.0
        )
    else:
        value_ok = False
        n_mismatch = -1
        max_abs = -1.0
    return {
        "column": label,
        "exact": bool(index_ok and dtype_ok and nan_ok and value_ok),
        "index_ok": index_ok,
        "dtype_ok": dtype_ok,
        "dtype_loop": str(a.dtype),
        "dtype_vec": str(b.dtype),
        "nan_mask_ok": nan_ok,
        "n_nan_loop": int(np.isnan(a_np).sum()) if a_np.size else 0,
        "n_nan_vec": int(np.isnan(b_np).sum()) if b_np.size else 0,
        "value_ok": value_ok,
        "n_value_mismatch": n_mismatch,
        "max_abs_diff": max_abs,
    }


def _dual_diff_sample(vw: pd.Series, gid: pd.Series) -> dict[str, Any]:
    loop = _loop_impl(vw, gid)
    vec = _vectorized_impl(vw, gid)
    cols = [_col_diff(c, loop[c], vec[c]) for c in VWAP_RET_COLS]
    return {
        "cols": cols,
        "exact": all(c["exact"] for c in cols),
        "n_bars": int(len(vw)),
        "n_groups": int(gid.nunique()),
    }


def _mode_dualdiff(count: int, include_boundary: bool) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    selected = _select(count, include_boundary)
    samples = _load_sample_dfs(selected)
    print(f"dualdiff: {len(samples)} instruments (loop vs vectorized)")

    results: list[dict[str, Any]] = []
    for i, (row, df_1d) in enumerate(samples):
        rec: dict[str, Any] = {
            "instrument_id": row["instrument_id"],
            "symbol": row.get("symbol"),
            "market": row.get("market"),
            "selection_reason": row["selection_reason"],
        }
        try:
            vw, gid = _derive_vwap_group(df_1d)
            rec.update(_dual_diff_sample(vw, gid))
        except Exception as exc:  # noqa: BLE001
            rec.update({"exact": False, "error": f"{type(exc).__name__}: {exc}"})
        results.append(rec)
        if (i + 1) % 25 == 0 or (i + 1) == len(results):
            print(f"  ... {i + 1}/{len(results)} done")

    n_total = len(results)
    n_pass = sum(1 for r in results if r.get("exact"))
    fails = [r for r in results if not r.get("exact")]

    # 全样本逐列 exact 统计（只统计无 error 样本）
    clean = [r for r in results if r.get("exact") is not None and not r.get("error")]
    col_stats: dict[str, dict[str, Any]] = {}
    for c in VWAP_RET_COLS:
        n_exact = sum(1 for r in clean if any(x["column"] == c and x["exact"] for x in r["cols"]))
        col_stats[c] = {"exact_ratio": f"{n_exact}/{n_total}", "n_exact": n_exact}

    summary = {
        "phase": "3.4B-2A",
        "method": "VWAP-return group loop -> pandas vectorization (dual-run, 5-col exact parity)",
        "gate": "vwap_ret_total/avg/5/10/20 五列逐元素 exact：index + dtype + NaN mask + 值 "
                "（浮点 bit/equality exact，无 tolerance）",
        "n_total": n_total,
        "n_exact": n_pass,
        "exact_ratio": f"{n_pass}/{n_total}",
        "col_stats": col_stats,
        "parity_pass": n_total > 0 and n_pass == n_total,
        "failures": [
            {
                "instrument_id": r.get("instrument_id"),
                "symbol": r.get("symbol"),
                "selection_reason": r.get("selection_reason"),
                "cols": r.get("cols"),
                "error": r.get("error"),
            }
            for r in fails
        ][:10],
    }

    with open(OUTPUT_DIR / "dualdiff.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    with open(OUTPUT_DIR / "dualdiff_per_instrument.jsonl", "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    print(f"\nexact parity: {n_pass}/{n_total}")
    for c, st in col_stats.items():
        print(f"  {c}: {st['exact_ratio']}")
    print(f"parity_pass: {summary['parity_pass']}")
    if fails:
        print(f"{len(fails)} failures (first 5):")
        for r in fails[:5]:
            print(f"  {r.get('symbol')}: {r.get('error') or r.get('cols')}")
    print(f"wrote {OUTPUT_DIR / 'dualdiff.json'}")


# ---------------------------------------------------------------------------
# 模式 2：单独 benchmark 旧 loop vs vectorized（不含 DSA 主体）
# ---------------------------------------------------------------------------
def _mode_benchloop(count: int, include_boundary: bool, reps: int) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    selected = _select(count, include_boundary)
    samples = _load_sample_dfs(selected)
    print(f"benchloop: {len(samples)} instruments (reps={reps}, loop vs vectorized)")

    t_loop: list[float] = []
    t_vec: list[float] = []
    per: list[dict[str, Any]] = []
    for i, (row, df_1d) in enumerate(samples):
        vw, gid = _derive_vwap_group(df_1d)
        for _ in range(1):
            _loop_impl(vw, gid)
            _vectorized_impl(vw, gid)
        tl: list[float] = []
        tv: list[float] = []
        for _ in range(reps):
            t0 = time.perf_counter()
            _loop_impl(vw, gid)
            tl.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            _vectorized_impl(vw, gid)
            tv.append(time.perf_counter() - t0)
        tl_ms = float(np.median(tl)) * 1000.0
        tv_ms = float(np.median(tv)) * 1000.0
        t_loop.append(tl_ms)
        t_vec.append(tv_ms)
        per.append(
            {
                "instrument_id": row["instrument_id"],
                "symbol": row.get("symbol"),
                "n_bars": int(len(vw)),
                "n_groups": int(gid.nunique()),
                "loop_ms": round(tl_ms, 4),
                "vec_ms": round(tv_ms, 4),
            }
        )
        if (i + 1) % 25 == 0 or (i + 1) == len(samples):
            print(f"  ... {i + 1}/{len(samples)} done")

    def _stats(vals: list[float]) -> dict[str, float]:
        a = np.array(vals)
        return {
            "p50": round(float(np.median(a)), 4),
            "p95": round(float(np.percentile(a, 95)), 4),
            "mean": round(float(a.mean()), 4),
            "total": round(float(a.sum()), 4),
        }

    loop_stats = _stats(t_loop)
    vec_stats = _stats(t_vec)
    delta_p50 = vec_stats["p50"] - loop_stats["p50"]
    summary = {
        "phase": "3.4B-2A",
        "method": "benchmark VWAP-return loop (baseline) vs vectorized (candidate), "
                  "isolated from DSA main body",
        "n": len(t_loop),
        "reps": reps,
        "loop_ms": loop_stats,
        "vec_ms": vec_stats,
        "delta_p50_ms": round(delta_p50, 4),
        "pct_p50_change": round(delta_p50 / loop_stats["p50"] * 100.0, 2)
        if loop_stats["p50"] else 0.0,
        "per_instrument": per[:10],
    }

    with open(OUTPUT_DIR / "benchloop.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\nloop p50: {loop_stats['p50']}ms | vec p50: {vec_stats['p50']}ms "
          f"(delta {delta_p50:+.4f}ms, {summary['pct_p50_change']}%)")
    print(f"wrote {OUTPUT_DIR / 'benchloop.json'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3.4B-2A VWAP-return loop safe vectorization")
    ap.add_argument("--mode", choices=["dualdiff", "benchloop"], required=True)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--include-boundary", action="store_true")
    ap.add_argument("--reps", type=int, default=5)
    args = ap.parse_args()

    if not MANIFEST_PATH.exists():
        sys.exit(f"manifest 不存在: {MANIFEST_PATH}")

    if args.mode == "dualdiff":
        _mode_dualdiff(args.count, args.include_boundary)
    elif args.mode == "benchloop":
        _mode_benchloop(args.count, args.include_boundary, args.reps)


if __name__ == "__main__":
    main()
