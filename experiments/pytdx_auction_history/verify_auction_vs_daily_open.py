"""竞价数据正确性验证 — 随机抽样 K 只股票，核对 auction price vs 日线 open 等三项合同。

[CHANGE-20260817-001] Phase 1 Historical Source Validation。

对 temporal120 run1 观测（29 stocks × 120 trade_dates）做可复现随机抽样，逐 stock-day 验证：

  1. Price contract：auction_price_raw == DB bars_daily.open（lane_a 内嵌对比，mdas_data_source=db）
  2. Volume unit contract：auction_volume_shares == auction_volume_raw_lots × 100（LOT → shares）
  3. Amount derivation contract：auction_amount == auction_price_raw × auction_volume_shares

可选 `--db-recheck`：独立重读 bars_daily，对抽样股票做一次当前 DB 交叉核对（不只依赖生成期证据）。

用法：
    python experiments/pytdx_auction_history/verify_auction_vs_daily_open.py            # K=10，纯证据核对
    python .../verify_auction_vs_daily_open.py --count 10 --seed 42                    # 指定抽样数与 seed
    python .../verify_auction_vs_daily_open.py --db-recheck                            # 追加 DB 交叉核对
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date
from pathlib import Path
from typing import Any

_EXP_DIR = Path(__file__).resolve().parent
_OBS_PATH = _EXP_DIR / "output" / "temporal120" / "2026-08-14" / "run1" / "01_observations.json"

# 权威 owner 常量（复用 auction_history_semantics_validation，不重定义第二套）
AUCTION_LOT_MULTIPLIER = 100
PRICE_EXACT_TOL = 1e-9

# canonicalization status 中 volume-bearing 的记录才参与 Volume unit 检查
CANON_VOLUME_BEARING_STATUSES = {"CANONICAL"}


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_observations(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"observations 不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} 顶层应为 list，got {type(data)}")
    return data


def group_by_stock(observations: list[dict]) -> dict[str, list[dict]]:
    by_stock: dict[str, list[dict]] = {}
    for obs in observations:
        sym = str(obs.get("symbol") or obs.get("stock_id") or "?")
        by_stock.setdefault(sym, []).append(obs)
    return by_stock


def sample_stocks(by_stock: dict[str, list[dict]], k: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    keys = sorted(by_stock.keys())
    if len(keys) < k:
        print(f"[warn] 样本股票 {len(keys)} < 抽样数 {k}，按全部抽样", file=sys.stderr)
        k = len(keys)
    return rng.sample(keys, k)


def verify_observation(obs: dict) -> dict:
    """对单个 stock-day 返回三项检查结果。"""
    result: dict[str, Any] = {
        "trade_date": obs.get("trade_date"),
        "symbol": obs.get("symbol"),
        "auction_price_raw": obs.get("auction_price_raw"),
    }

    # --- 1. Price vs daily open（lane_a 内嵌 DB bars_daily 对比）---
    lane_a = obs.get("lane_a") or {}
    if lane_a.get("status") == "COMPUTED":
        price = _as_float(obs.get("auction_price_raw"))
        db_open = _as_float(lane_a.get("mdas_raw_open_T"))
        if price is None or db_open is None:
            result["price_check"] = "unavailable"
        else:
            result["price_check"] = "match" if abs(price - db_open) < PRICE_EXACT_TOL else "mismatch"
            result["db_open"] = db_open
    else:
        # LANE_A_MISSING_MDA_OPEN 等：当日无 DB open，无法比对，记为 unavailable（非 mismatch）
        result["price_check"] = "unavailable"
        result["lane_a_status"] = lane_a.get("status")

    # --- 2. Volume unit contract：shares == lots × 100 ---
    lots = _as_float(obs.get("auction_volume_raw_lots"))
    shares = _as_float(obs.get("auction_volume_shares"))
    canon_status = obs.get("canonicalization_status")
    if lots is not None and lots > 0 and canon_status in CANON_VOLUME_BEARING_STATUSES:
        expected_shares = round(lots * AUCTION_LOT_MULTIPLIER)
        result["volume_check"] = "match" if shares == expected_shares else "mismatch"
        result["expected_shares"] = expected_shares
    else:
        result["volume_check"] = "unavailable"

    # --- 3. Amount derivation contract：amount == price × shares ---
    amt = _as_float(obs.get("auction_amount"))
    price = _as_float(obs.get("auction_price_raw"))
    amt_source = obs.get("auction_amount_source_type") or ""
    if amt is not None and price is not None and shares is not None \
            and "DERIVED" in amt_source:
        expected_amt = price * shares
        result["amount_check"] = "match" if abs(amt - expected_amt) < 1e-6 else "mismatch"
        result["expected_amount"] = expected_amt
    else:
        result["amount_check"] = "unavailable"

    return result


def verify_stock(observations: list[dict]) -> dict:
    checks = [verify_observation(obs) for obs in observations]
    def _count(key: str) -> dict:
        return {
            "total": sum(1 for c in checks if c.get(key) in ("match", "mismatch")),
            "match": sum(1 for c in checks if c.get(key) == "match"),
            "mismatch": sum(1 for c in checks if c.get(key) == "mismatch"),
            "unavailable": sum(1 for c in checks if c.get(key) == "unavailable"),
        }
    return {
        "symbol": observations[0].get("symbol"),
        "stock_days": len(observations),
        "price": _count("price_check"),
        "volume_unit": _count("volume_check"),
        "amount": _count("amount_check"),
    }


async def db_recheck_price(
    session: Any,
    by_stock: dict[str, list[dict]],
    symbols: list[str],
    obs_by_key: dict[tuple[str, str], dict],
) -> dict:
    """独立重读 bars_daily.open，与 auction_price_raw 直接比对（当前 DB 交叉核对）。"""
    from sqlalchemy import select
    from app.models.bar import BarDaily

    rows = []
    for sym in symbols:
        for obs in by_stock[sym]:
            rows.append(obs)
    checked = matched = mismatched = 0
    mismatches: list[dict] = []
    # 分页批量查询 bars_daily
    from sqlalchemy import or_
    from uuid import UUID

    missing_instruments: dict[str, str] = {}
    for obs in rows:
        iid = obs.get("instrument_id")
        if iid and str(iid) not in missing_instruments:
            missing_instruments[str(iid)] = str(obs.get("symbol"))

    # 按 instrument_id 批量取该股票 120 个交易日的 open
    for iid_str, sym in missing_instruments.items():
        try:
            iid_uuid = UUID(iid_str)
        except ValueError:
            continue
        dates = [date.fromisoformat(obs["trade_date"]) for obs in by_stock[sym]]
        stmt = select(BarDaily).where(
            BarDaily.instrument_id == iid_uuid,
            BarDaily.trade_date.in_(dates),
        )
        res = await session.execute(stmt)
        open_by_date: dict[date, float] = {}
        for bar in res.scalars():
            open_by_date[bar.trade_date] = float(bar.open) if bar.open is not None else None
        for obs in by_stock[sym]:
            t = date.fromisoformat(obs["trade_date"])
            db_open = open_by_date.get(t)
            price = _as_float(obs.get("auction_price_raw"))
            if db_open is None or price is None:
                continue
            checked += 1
            if abs(price - db_open) < PRICE_EXACT_TOL:
                matched += 1
            else:
                mismatched += 1
                mismatches.append({
                    "symbol": sym, "trade_date": t.isoformat(),
                    "auction_price_raw": price, "db_open": db_open,
                })
    return {
        "checked": checked, "matched": matched, "mismatched": mismatched,
        "mismatch_details": mismatches[:20],
    }


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="竞价正确性抽样验证")
    parser.add_argument("--observations", type=Path, default=_OBS_PATH)
    parser.add_argument("--count", type=int, default=10, help="随机抽样股票数（默认 10）")
    parser.add_argument("--seed", type=int, default=42, help="随机 seed（可复现）")
    parser.add_argument("--db-recheck", action="store_true", help="追加 DB bars_daily 交叉核对")
    parser.add_argument("--output", type=Path, default=None, help="结果 JSON 输出路径")
    args = parser.parse_args(argv)

    observations = load_observations(args.observations)
    by_stock = group_by_stock(observations)
    symbols = sample_stocks(by_stock, args.count, args.seed)
    print(f"observations total stock-days: {len(observations)}  stocks: {len(by_stock)}")
    print(f"sampled {len(symbols)} stocks (seed={args.seed}): {', '.join(symbols)}")

    stock_reports = [verify_stock(by_stock[sym]) for sym in symbols]

    agg = {
        "seed": args.seed,
        "sampled_stocks": symbols,
        "stock_reports": stock_reports,
        "aggregate": {
            "price": {"total": 0, "match": 0, "mismatch": 0, "unavailable": 0},
            "volume_unit": {"total": 0, "match": 0, "mismatch": 0, "unavailable": 0},
            "amount": {"total": 0, "match": 0, "mismatch": 0, "unavailable": 0},
        },
    }
    for rep in stock_reports:
        for k in ("price", "volume_unit", "amount"):
            for field in ("total", "match", "mismatch", "unavailable"):
                agg["aggregate"][k][field] += rep[k][field]

    db_result = None
    if args.db_recheck:
        from app.db import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            db_result = await db_recheck_price(session, by_stock, symbols, {})
        agg["db_recheck"] = db_result

    for rep in stock_reports:
        print(json.dumps(rep, ensure_ascii=False, default=str))
    print("=== aggregate ===")
    print(json.dumps(agg["aggregate"], ensure_ascii=False, default=str))
    if db_result is not None:
        print("=== db_recheck ===")
        print(json.dumps({k: v for k, v in db_result.items() if k != "mismatch_details"},
                         ensure_ascii=False))

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(agg, f, ensure_ascii=False, default=str, indent=2)
        print(f"wrote {args.output}")

    # 结论断言：三项检查 mismatch 均为 0
    price_mm = agg["aggregate"]["price"]["mismatch"]
    vol_mm = agg["aggregate"]["volume_unit"]["mismatch"]
    amt_mm = agg["aggregate"]["amount"]["mismatch"]
    db_mm = (db_result or {}).get("mismatched", 0)
    ok = price_mm == 0 and vol_mm == 0 and amt_mm == 0 and db_mm == 0
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'} "
          f"(price_mismatch={price_mm} volume_mismatch={vol_mm} "
          f"amount_mismatch={amt_mm} db_mismatch={db_mm})")
    return 0 if ok else 1


if __name__ == "__main__":
    import asyncio
    raise SystemExit(asyncio.run(main()))
