#!/usr/bin/env python3
"""CF-PERF-01 — PROFILE-A: Current Stock Core compute-only profile (sample-only).

设计边界（与用户协议 §1-§9 一致）:
- 仅测量 current core 的「数据读取 + 计算」成本，不持久化 formal snapshot，
  不创建 formal publication，不修改 pointer。
- 直接复用真实 current path: `compute_review_core_for_trade_date`。
- 样本 50-100 instruments；输出 avg / p50 / p95（仅聚合，不输出个股明细以外的敏感物）。
- SAMPLE-ONLY: 必须显式设置 CURRENT_CORE_PROFILE=1 才会真正跑；否则只打印帮助并退出。
- 不在 formal production run / pointer 上执行；默认连本地/CI 或显式 DB。

PROFILE-B（真实 execution harness: claim/session/compute/persist/run-item/commit）
必须在 isolated verification DB 或 sanctioned sample env 执行，本脚本不覆盖。

用法:
    CURRENT_CORE_PROFILE=1 \
    APP_ENV=development \
    python backend/tools/current_core_profile.py \
        --trade-date 2026-08-07 --sample 80 --seed 42
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import statistics
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models.instrument import Instrument
from app.repositories.bar_repository import get_bars
from app.services.feature_snapshot_service import (
    compute_review_core_for_trade_date,
    _truncate_bars_to_trade_date,
)

# ---- 安全边界 ----------------------------------------------------------
if os.environ.get("CURRENT_CORE_PROFILE") != "1":
    print(
        "SAMPLE-ONLY guard: 未设置 CURRENT_CORE_PROFILE=1，拒绝执行。\n"
        "本脚本只做 current core 的 compute-only profiling，不持久化、不发布、不改 pointer。\n"
        "确认环境后执行: CURRENT_CORE_PROFILE=1 APP_ENV=development "
        "python backend/tools/current_core_profile.py --trade-date 2026-08-07 --sample 80"
    )
    sys.exit(0)

_SETTINGS = get_settings()
if "bz_stock_verify" not in str(_SETTINGS.DATABASE_URL) and "production" in os.environ.get("APP_ENV", ""):
    print("REFUSE: APP_ENV=production 且非 verification DB。PROFILE-A 不得针对 formal production run。")
    sys.exit(2)


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    k = (n - 1) * p
    f = int(k)
    c = min(f + 1, n - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def _summarize(name: str, vals: list[float]) -> dict[str, float]:
    if not vals:
        return {name: 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "n": 0.0}
    s = sorted(vals)
    return {
        "name": name,
        "avg_ms": round(statistics.fmean(s) * 1000, 2),
        "p50_ms": round(_pct(s, 0.50) * 1000, 2),
        "p95_ms": round(_pct(s, 0.95) * 1000, 2),
        "n": float(len(s)),
    }


async def _sample_instruments(db: AsyncSession, n: int, seed: int) -> list[UUID]:
    # 取有日线数据的代表性样本（不复制全市场）
    rows = (
        await db.execute(
            select(Instrument.id)
            .where(Instrument.instrument_type == "stock")
            .order_by(Instrument.id)
            .limit(n * 4)
        )
    ).scalars().all()
    # 确定性抽样：用 seed 跳跃
    chosen = rows[:: max(1, len(rows) // max(1, n))][:n] if rows else []
    return list(chosen)


async def _profile_one(
    db: AsyncSession,
    instrument_id: "uuid",
    trade_date: date,
    run_calculated_at: str,
) -> dict[str, float] | None:
    # A. load daily bars（+ 内部 adj factor 读取/apply，归为 A+B）
    t0 = time.perf_counter()
    bars = await get_bars(
        db, instrument_id, "1d", end=trade_date, adjust="qfq",
        allow_backfill=False,
    )
    df = _truncate_bars_to_trade_date(bars, trade_date, "1d")
    t_load = time.perf_counter()

    if df is None or df.empty or len(df) < 60:
        return None  # 样本跳过：历史不足

    # C. compute（复用真实 current path，不持久化）
    try:
        _ = await compute_review_core_for_trade_date(
            db, instrument_id, trade_date, "1d", "qfq",
            primary_bars=df,
            run_calculated_at=run_calculated_at,
        )
    except Exception as exc:  # profile 不掩盖，但记录后仍返回 None
        print(f"  skip compute error {instrument_id}: {exc}")
        return None
    t_compute = time.perf_counter()

    return {
        "A_load_ms": (t_load - t0) * 1000,
        "C_compute_ms": (t_compute - t_load) * 1000,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trade-date", default="2026-08-07")
    ap.add_argument("--sample", type=int, default=80)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    td = date.fromisoformat(args.trade_date)
    run_calc = td.isoformat()

    async with AsyncSessionLocal() as db:
        instrs = await _sample_instruments(db, args.sample, args.seed)
        print(f"PROFILE-A: trade_date={td} sample_target={args.sample} "
              f"actual_population={len(instrs)} env={os.environ.get('APP_ENV')}")

        load_ms: list[float] = []
        compute_ms: list[float] = []
        skipped = 0
        for i, iid in enumerate(instrs, 1):
            r = await _profile_one(db, iid, td, run_calc)
            if r is None:
                skipped += 1
                continue
            load_ms.append(r["A_load_ms"])
            compute_ms.append(r["C_compute_ms"])
            if i % 20 == 0:
                print(f"  progress {i}/{len(instrs)} kept={len(load_ms)} skipped={skipped}")

        abc_total = [l + c for l, c in zip(load_ms, compute_ms)]
        out = {
            "A_load": _summarize("A_load", load_ms),
            "C_compute": _summarize("C_compute", compute_ms),
            "ABC_total": _summarize("ABC_total", abc_total),
            "skipped_insufficient": skipped,
        }
        print("\n=== PROFILE-A RESULT (compute-only, sample) ===")
        for k, v in out.items():
            print(f"{k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())
