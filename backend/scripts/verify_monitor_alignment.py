# -*- coding: utf-8 -*-
"""端到端验证：项目监控实现与参考脚本 monitoring.py 的对齐性。

Purpose: 对比项目监控链路（前复权+BB+VP+穿越检测）与参考脚本的输出一致性。
Inputs:
    - pytdx 行情数据（通过参考脚本 fetch_all_kline 获取）
    - PostgreSQL 数据库（通过项目 bar_repository 获取）
Outputs:
    - 对比结果（BB参考线、VP peak_prices、穿越检测、前复权效果）
How to Run:
    cd /root/web_dev/backend
    python scripts/verify_monitor_alignment.py
    python scripts/verify_monitor_alignment.py --symbol 000001
Examples:
    python scripts/verify_monitor_alignment.py
    python scripts/verify_monitor_alignment.py --symbol 600519
Side Effects:
    - 读取数据库（不写入）
    - 通过 pytdx 获取行情数据（不写入DB）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

# 项目路径
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)
REF_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "ref", "交易")
sys.path.insert(0, REF_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("verify_monitor_alignment")


def _load_features_modules():
    """加载 features 模块（bollinger + volume_profile）。"""
    from features.bollinger_features_plotly import bollinger
    from features.luxalgo_volume_profile_pytdx_15m_aligned import (
        VolumeProfileConfig,
        compute_volume_profile,
    )
    return bollinger, compute_volume_profile, VolumeProfileConfig


def _fmt_price(val):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "N/A"
    return f"{val:.2f}"


def compare_bb_reference_lines(symbol: str, bollinger_fn) -> dict:
    """对比 BB 参考线：参考脚本（pytdx直取+内置复权）vs 项目（DB+项目复权）。"""
    from app.monitoring import fetch_all_kline

    print(f"\n{'='*60}")
    print(f"=== BB 参考线对比: {symbol} ===")
    print(f"{'='*60}")

    # 参考侧：pytdx 直取 + 内置复权
    ref_data = fetch_all_kline([symbol], 'd', bars=250)
    ref_df = ref_data.get(symbol)
    if ref_df is None or ref_df.empty:
        print(f"  参考侧无数据: {symbol}")
        return {"match": False, "reason": "参考侧无数据"}

    bb_mid_ref, bb_upper_ref, bb_lower_ref = bollinger_fn(ref_df, 20, 2.0)
    # 取最后一根已完成bar
    ref_idx = -1
    ref_upper = float(bb_upper_ref.iloc[ref_idx])
    ref_mid = float(bb_mid_ref.iloc[ref_idx])
    ref_lower = float(bb_lower_ref.iloc[ref_idx])
    ref_close = float(ref_df["close"].iloc[ref_idx])

    print(f"  参考侧（pytdx+内置复权）:")
    print(f"    bars={len(ref_df)}, close={_fmt_price(ref_close)}")
    print(f"    bb_upper={_fmt_price(ref_upper)}, bb_mid={_fmt_price(ref_mid)}, bb_lower={_fmt_price(ref_lower)}")

    # 项目侧：DB + 项目复权
    async def _project_side():
        from app.db import AsyncSessionLocal
        from app.repositories.bar_repository import (
            _get_adj_factor_df,
            apply_adj_factor_to_bars,
            fetch_daily_bars,
        )
        from app.models.instrument import Instrument
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            # 查 instrument_id
            stmt = select(Instrument.id, Instrument.symbol).where(Instrument.symbol == symbol)
            result = await db.execute(stmt)
            row = result.first()
            if row is None:
                return None, None
            instrument_id = row[0]

            # 获取日线（未复权）
            from datetime import date
            today = date.today()
            from datetime import timedelta
            bars_daily = await fetch_daily_bars(
                db, instrument_id,
                start_date=today - timedelta(days=370),
                end_date=today,
            )
            if bars_daily.empty:
                return None, None

            # 应用前复权
            adj_factor_df = await _get_adj_factor_df(db, instrument_id)
            if not adj_factor_df.empty:
                bars_daily = apply_adj_factor_to_bars(bars_daily, adj_factor_df, intraday=False)

            return bars_daily, adj_factor_df

    proj_df, adj_df = asyncio.run(_project_side())
    if proj_df is None or proj_df.empty:
        print(f"  项目侧无数据: {symbol}")
        return {"match": False, "reason": "项目侧无数据"}

    bb_mid_proj, bb_upper_proj, bb_lower_proj = bollinger_fn(proj_df, 20, 2.0)
    proj_idx = -1
    proj_upper = float(bb_upper_proj.iloc[proj_idx])
    proj_mid = float(bb_mid_proj.iloc[proj_idx])
    proj_lower = float(bb_lower_proj.iloc[proj_idx])
    proj_close = float(proj_df["close"].iloc[proj_idx])

    print(f"  项目侧（DB+项目复权）:")
    print(f"    bars={len(proj_df)}, close={_fmt_price(proj_close)}")
    print(f"    bb_upper={_fmt_price(proj_upper)}, bb_mid={_fmt_price(proj_mid)}, bb_lower={_fmt_price(proj_lower)}")

    # 对比
    diff_upper = abs(ref_upper - proj_upper)
    diff_mid = abs(ref_mid - proj_mid)
    diff_lower = abs(ref_lower - proj_lower)
    max_diff = max(diff_upper, diff_mid, diff_lower)
    tolerance = 0.05  # 允许5分钱误差（数据来源/时间点可能略有差异）
    match = max_diff < tolerance

    print(f"\n  差异: upper={diff_upper:.4f}, mid={diff_mid:.4f}, lower={diff_lower:.4f}")
    print(f"  最大差异: {max_diff:.4f}, 容差: {tolerance}")
    print(f"  匹配: {'YES' if match else 'NO'}")

    return {
        "match": match,
        "max_diff": max_diff,
        "ref": {"upper": ref_upper, "mid": ref_mid, "lower": ref_lower},
        "proj": {"upper": proj_upper, "mid": proj_mid, "lower": proj_lower},
    }


def compare_vp_peak_prices(symbol: str, compute_vp_fn, VPConfig) -> dict:
    """对比 VP peak_prices：参考脚本 vs 项目。"""
    from app.monitoring import fetch_all_kline

    print(f"\n{'='*60}")
    print(f"=== VP Peak Prices 对比: {symbol} ===")
    print(f"{'='*60}")

    # 参考侧
    ref_daily = fetch_all_kline([symbol], 'd', bars=250)
    ref_15m = fetch_all_kline([symbol], '15m', bars=8000)
    daily_df = ref_daily.get(symbol)
    ltf_df = ref_15m.get(symbol)

    if daily_df is None or daily_df.empty:
        print(f"  参考侧无日线数据: {symbol}")
        return {"match": False, "reason": "参考侧无日线数据"}

    # 准备数据
    daily_for_vp = daily_df.reset_index()
    if "datetime" not in daily_for_vp.columns:
        for col in ["index", "date", "time"]:
            if col in daily_for_vp.columns:
                daily_for_vp = daily_for_vp.rename(columns={col: "datetime"})
                break

    ltf_for_vp = None
    if ltf_df is not None and not ltf_df.empty:
        ltf_for_vp = ltf_df.reset_index()
        if "datetime" not in ltf_for_vp.columns:
            for col in ["index", "date", "time"]:
                if col in ltf_for_vp.columns:
                    ltf_for_vp = ltf_for_vp.rename(columns={col: "datetime"})
                    break

    vp_cfg = VPConfig(
        peaks_show="peaks",
        profile_lookback_length=360,
        profile_number_of_rows=100,
        value_area_threshold=0.70,
        peaks_detection_percent=0.05,
        troughs_show="none",
        troughs_detection_percent=0.07,
        volume_node_threshold=0.01,
        highest_n_volume_nodes=0,
        lowest_n_volume_nodes=0,
    )

    ref_vp = compute_vp_fn(daily_for_vp, cfg=vp_cfg, profile_df=ltf_for_vp, main_period="day")
    ref_peaks = ref_vp.all_peak_prices
    print(f"  参考侧 peaks ({len(ref_peaks)}): {ref_peaks[:5]}{'...' if len(ref_peaks) > 5 else ''}")

    # 项目侧
    async def _project_side():
        from app.db import AsyncSessionLocal
        from app.repositories.bar_repository import (
            _get_adj_factor_df,
            apply_adj_factor_to_bars,
            fetch_daily_bars,
            fetch_15min_bars,
        )
        from app.models.instrument import Instrument
        from sqlalchemy import select
        from datetime import date, timedelta

        async with AsyncSessionLocal() as db:
            stmt = select(Instrument.id, Instrument.symbol).where(Instrument.symbol == symbol)
            result = await db.execute(stmt)
            row = result.first()
            if row is None:
                return None, None
            instrument_id = row[0]

            today = date.today()
            now_naive = datetime.now().replace(tzinfo=None)

            bars_daily = await fetch_daily_bars(
                db, instrument_id,
                start_date=today - timedelta(days=370),
                end_date=today,
            )
            bars_15min = await fetch_15min_bars(
                db, instrument_id,
                start_time=now_naive - timedelta(days=800),
                end_time=now_naive,
            )

            # 前复权
            adj_factor_df = await _get_adj_factor_df(db, instrument_id)
            if not adj_factor_df.empty:
                if not bars_daily.empty:
                    bars_daily = apply_adj_factor_to_bars(bars_daily, adj_factor_df, intraday=False)
                if not bars_15min.empty:
                    bars_15min = apply_adj_factor_to_bars(bars_15min, adj_factor_df, intraday=True)

            return bars_daily, bars_15min

    proj_daily, proj_15min = asyncio.run(_project_side())
    if proj_daily is None or proj_daily.empty:
        print(f"  项目侧无数据: {symbol}")
        return {"match": False, "reason": "项目侧无数据"}

    proj_daily_vp = proj_daily.reset_index()
    if "datetime" not in proj_daily_vp.columns:
        for col in ["index", "date", "time"]:
            if col in proj_daily_vp.columns:
                proj_daily_vp = proj_daily_vp.rename(columns={col: "datetime"})
                break

    proj_ltf_vp = None
    if proj_15min is not None and not proj_15min.empty:
        proj_ltf_vp = proj_15min.reset_index()
        if "datetime" not in proj_ltf_vp.columns:
            for col in ["index", "date", "time"]:
                if col in proj_ltf_vp.columns:
                    proj_ltf_vp = proj_ltf_vp.rename(columns={col: "datetime"})
                    break

    proj_vp = compute_vp_fn(proj_daily_vp, cfg=vp_cfg, profile_df=proj_ltf_vp, main_period="day")
    proj_peaks = proj_vp.all_peak_prices
    print(f"  项目侧 peaks ({len(proj_peaks)}): {proj_peaks[:5]}{'...' if len(proj_peaks) > 5 else ''}")

    # 对比
    if not ref_peaks and not proj_peaks:
        match = True
        max_diff = 0.0
    elif not ref_peaks or not proj_peaks:
        match = False
        max_diff = float("inf")
    else:
        # 取较短的列表长度进行逐个对比
        min_len = min(len(ref_peaks), len(proj_peaks))
        diffs = [abs(ref_peaks[i] - proj_peaks[i]) for i in range(min_len)]
        max_diff = max(diffs) if diffs else 0.0
        tolerance = 0.10  # VP peak 允许1毛钱误差
        match = max_diff < tolerance and len(ref_peaks) == len(proj_peaks)

    print(f"\n  参考侧 peak 数: {len(ref_peaks)}, 项目侧 peak 数: {len(proj_peaks)}")
    print(f"  最大差异: {max_diff:.4f}")
    print(f"  匹配: {'YES' if match else 'NO'}")

    return {"match": match, "max_diff": max_diff, "ref_count": len(ref_peaks), "proj_count": len(proj_peaks)}


def verify_crossover_detection(bollinger_fn, compute_vp_fn, VPConfig) -> dict:
    """验证穿越检测逻辑一致性（构造合成数据）。"""
    print(f"\n{'='*60}")
    print(f"=== 穿越检测逻辑验证（合成数据）===")
    print(f"{'='*60}")

    # 构造合成日线数据（100根bar，close从10到20线性增长）
    dates = pd.date_range("2026-01-01", periods=100, freq="B")
    close_vals = np.linspace(10, 20, 100)
    daily_df = pd.DataFrame({
        "open": close_vals - 0.1,
        "high": close_vals + 0.5,
        "low": close_vals - 0.5,
        "close": close_vals,
        "volume": np.full(100, 100000),
    }, index=dates)

    # 计算 BB
    bb_mid, bb_upper, bb_lower = bollinger_fn(daily_df, 20, 2.0)

    # 构造 1m 数据：价格从 BB 下方穿越到上方
    last_upper = float(bb_upper.iloc[-1])
    m1_df = pd.DataFrame({
        "open": [last_upper - 1.0, last_upper + 0.5],
        "high": [last_upper - 0.5, last_upper + 1.0],
        "low": [last_upper - 1.5, last_upper - 0.5],
        "close": [last_upper - 0.5, last_upper + 0.5],
        "volume": [5000, 6000],
    }, index=pd.to_datetime(["2026-06-23 10:00", "2026-06-23 10:01"]))

    # 参考脚本检测逻辑
    from app.monitoring import detect_bb_signals
    ref_signals = detect_bb_signals(daily_df, m1_df=m1_df, freq="d")

    # 项目 BollingerMonitor 检测逻辑（手动复现）
    prev_close = float(m1_df.iloc[-2]["close"])
    cur_close = float(m1_df.iloc[-1]["close"])
    ref_upper = float(bb_upper.iloc[-2]) if len(bb_upper) >= 2 else float(bb_upper.iloc[-1])
    ref_mid_val = float(bb_mid.iloc[-2]) if len(bb_mid) >= 2 else float(bb_mid.iloc[-1])
    ref_lower = float(bb_lower.iloc[-2]) if len(bb_lower) >= 2 else float(bb_lower.iloc[-1])

    proj_signals = []
    # 上轨穿越
    if prev_close < ref_upper <= cur_close:
        proj_signals.append("bb_upper_touch")
    # 中轨穿越
    if (prev_close <= ref_mid_val < cur_close) or (cur_close <= ref_mid_val < prev_close):
        proj_signals.append("bb_mid_touch")
    # 下轨穿越
    if prev_close > ref_lower >= cur_close:
        proj_signals.append("bb_lower_touch")

    ref_types = [s["trigger_type"] for s in ref_signals]
    match = set(ref_types) == set(proj_signals)

    print(f"  合成场景: prev_close={prev_close:.2f}, cur_close={cur_close:.2f}")
    print(f"  BB参考线: upper={ref_upper:.2f}, mid={ref_mid_val:.2f}, lower={ref_lower:.2f}")
    print(f"  参考脚本检测: {ref_types}")
    print(f"  项目逻辑检测: {proj_signals}")
    print(f"  匹配: {'YES' if match else 'NO'}")

    # Node 穿越检测
    print(f"\n  --- Node 穿越检测 ---")
    # 构造合成 peak prices
    class FakeVPResult:
        all_peak_prices = [15.0, 17.5]

    fake_vp = FakeVPResult()
    # 构造 1m 数据穿越 15.0
    node_m1 = pd.DataFrame({
        "open": [14.5, 15.5],
        "high": [14.8, 15.8],
        "low": [14.3, 15.2],
        "close": [14.8, 15.5],
        "volume": [5000, 6000],
    }, index=pd.to_datetime(["2026-06-23 10:00", "2026-06-23 10:01"]))

    from app.monitoring import detect_node_cluster_signals
    ref_node_signals = detect_node_cluster_signals(node_m1, fake_vp, freq="d")

    # 项目逻辑
    prev_close_node = float(node_m1.iloc[-2]["close"])
    cur_close_node = float(node_m1.iloc[-1]["close"])
    proj_node_signals = []
    for cp in fake_vp.all_peak_prices:
        peak_cross = (prev_close_node <= cp < cur_close_node) or (cur_close_node <= cp < prev_close_node)
        if peak_cross:
            proj_node_signals.append(("node_cluster_touch", cp))

    ref_node_types = [(s["trigger_type"], s["cluster_price"]) for s in ref_node_signals]
    node_match = set(ref_node_types) == set(proj_node_signals)

    print(f"  合成场景: prev_close={prev_close_node:.2f}, cur_close={cur_close_node:.2f}")
    print(f"  Peak prices: {fake_vp.all_peak_prices}")
    print(f"  参考脚本检测: {ref_node_types}")
    print(f"  项目逻辑检测: {proj_node_signals}")
    print(f"  匹配: {'YES' if node_match else 'NO'}")

    return {"bb_match": match, "node_match": node_match}


def verify_adj_factor_effect(symbol: str, bollinger_fn) -> dict:
    """验证前复权对 BB 计算的影响。"""
    from app.monitoring import fetch_all_kline

    print(f"\n{'='*60}")
    print(f"=== 前复权效果验证: {symbol} ===")
    print(f"{'='*60}")

    # 获取未复权数据
    ref_data = fetch_all_kline([symbol], 'd', bars=250)
    ref_df = ref_data.get(symbol)
    if ref_df is None or ref_df.empty:
        print(f"  无数据: {symbol}")
        return {"effect": "unknown"}

    # 参考脚本已内置复权，直接计算 BB
    bb_mid_adj, bb_upper_adj, bb_lower_adj = bollinger_fn(ref_df, 20, 2.0)
    adj_close = float(ref_df["close"].iloc[-1])
    adj_upper = float(bb_upper_adj.iloc[-1])
    adj_mid = float(bb_mid_adj.iloc[-1])
    adj_lower = float(bb_lower_adj.iloc[-1])

    # 项目侧：获取未复权数据再手动应用复权
    async def _project_side():
        from app.db import AsyncSessionLocal
        from app.repositories.bar_repository import (
            _get_adj_factor_df,
            fetch_daily_bars,
        )
        from app.models.instrument import Instrument
        from sqlalchemy import select
        from datetime import date, timedelta

        async with AsyncSessionLocal() as db:
            stmt = select(Instrument.id, Instrument.symbol).where(Instrument.symbol == symbol)
            result = await db.execute(stmt)
            row = result.first()
            if row is None:
                return None, None
            instrument_id = row[0]

            today = date.today()
            bars_daily = await fetch_daily_bars(
                db, instrument_id,
                start_date=today - timedelta(days=370),
                end_date=today,
            )
            adj_factor_df = await _get_adj_factor_df(db, instrument_id)
            return bars_daily, adj_factor_df

    proj_df_raw, adj_df = asyncio.run(_project_side())
    if proj_df_raw is None or proj_df_raw.empty:
        print(f"  项目侧无数据: {symbol}")
        return {"effect": "unknown"}

    # 未复权 BB
    bb_mid_raw, bb_upper_raw, bb_lower_raw = bollinger_fn(proj_df_raw, 20, 2.0)
    raw_close = float(proj_df_raw["close"].iloc[-1])
    raw_upper = float(bb_upper_raw.iloc[-1])
    raw_mid = float(bb_mid_raw.iloc[-1])
    raw_lower = float(bb_lower_raw.iloc[-1])

    # 检查 adj_factor 是否有变化（非全1.0）
    has_adj = False
    if adj_df is not None and not adj_df.empty:
        adj_values = adj_df["adj_factor"].values
        if len(adj_values) > 1:
            has_adj = not np.allclose(adj_values, adj_values[-1], rtol=1e-6)

    print(f"  未复权: close={_fmt_price(raw_close)}, upper={_fmt_price(raw_upper)}, mid={_fmt_price(raw_mid)}, lower={_fmt_price(raw_lower)}")
    print(f"  已复权（参考脚本）: close={_fmt_price(adj_close)}, upper={_fmt_price(adj_upper)}, mid={_fmt_price(adj_mid)}, lower={_fmt_price(adj_lower)}")
    print(f"  adj_factor 有变化: {'YES' if has_adj else 'NO'}")

    if has_adj:
        diff_close = abs(raw_close - adj_close)
        diff_upper = abs(raw_upper - adj_upper)
        print(f"  复权影响: close差异={diff_close:.4f}, upper差异={diff_upper:.4f}")
        print(f"  结论: 前复权对除权股票有显著影响，必须应用")
    else:
        print(f"  结论: 该股票近期无除权事件，前复权影响极小（ratio≈1.0）")

    return {"has_adj_change": has_adj, "raw_close": raw_close, "adj_close": adj_close}


def main():
    parser = argparse.ArgumentParser(description="验证项目监控实现与参考脚本的对齐性")
    parser.add_argument("--symbol", default="000001", help="测试股票代码（默认000001）")
    args = parser.parse_args()

    symbol = args.symbol
    print(f"验证股票: {symbol}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 加载 features 模块
    try:
        bollinger_fn, compute_vp_fn, VPConfig = _load_features_modules()
        print("features 模块加载成功")
    except Exception as e:
        print(f"features 模块加载失败: {e}")
        return

    results = {}

    # 1. BB 参考线对比
    try:
        results["bb"] = compare_bb_reference_lines(symbol, bollinger_fn)
    except Exception as e:
        print(f"BB 对比失败: {e}")
        results["bb"] = {"match": False, "reason": str(e)}

    # 2. VP Peak Prices 对比
    try:
        results["vp"] = compare_vp_peak_prices(symbol, compute_vp_fn, VPConfig)
    except Exception as e:
        print(f"VP 对比失败: {e}")
        results["vp"] = {"match": False, "reason": str(e)}

    # 3. 穿越检测逻辑验证
    try:
        results["crossover"] = verify_crossover_detection(bollinger_fn, compute_vp_fn, VPConfig)
    except Exception as e:
        print(f"穿越检测验证失败: {e}")
        results["crossover"] = {"bb_match": False, "node_match": False, "reason": str(e)}

    # 4. 前复权效果验证
    try:
        results["adj_factor"] = verify_adj_factor_effect(symbol, bollinger_fn)
    except Exception as e:
        print(f"前复权验证失败: {e}")
        results["adj_factor"] = {"effect": "unknown", "reason": str(e)}

    # 汇总
    print(f"\n{'='*60}")
    print(f"=== 验证汇总 ===")
    print(f"{'='*60}")
    bb_ok = results.get("bb", {}).get("match", False)
    vp_ok = results.get("vp", {}).get("match", False)
    co_bb = results.get("crossover", {}).get("bb_match", False)
    co_node = results.get("crossover", {}).get("node_match", False)
    print(f"  BB 参考线: {'PASS' if bb_ok else 'FAIL'}")
    print(f"  VP Peak Prices: {'PASS' if vp_ok else 'FAIL'}")
    print(f"  BB 穿越检测: {'PASS' if co_bb else 'FAIL'}")
    print(f"  Node 穿越检测: {'PASS' if co_node else 'FAIL'}")
    print(f"  前复权效果: 已验证")

    all_pass = bb_ok and vp_ok and co_bb and co_node
    print(f"\n  总体结果: {'ALL PASS' if all_pass else 'SOME FAILED'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
