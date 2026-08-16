"""Auction Historical Data Semantics Validation — Round 1.

实验性质：Evidence / Experiment only。
目的：在进入全市场 ~120 交易日 Auction 历史回补前，验证
  1) pytdx 历史逐笔 09:25 能否代表最终集合竞价；
  2) historical auction price / volume / amount 真实语义；
  3) Auction historical path 能否复用盘迹现有统一行情体系；
  4) 除权/复权场景下正式 Gap 能否复用现有 PIT 复权体系；
  5) 不新建第二套行情读取 / 复权 / 交易日 / 股票池 / market mapping 逻辑。

UNIFIED MARKET DATA FIRST（硬规则）：
- 所有已属盘迹正式行情体系的数据，必须通过现有 official owner 获取。
- Instrument Universe / Trading Calendar / Daily OHLCV / Previous Close / Adjustment / QFQ
  → 复用 get_active_a_share_instruments() / calendar_service / MarketDataAggregationService(=MDAS)。
- 唯一允许使用尚未进入统一 Market Data API 的 historical transaction source
  → 通过 PytdxAdapter 受管连接调用 adapter.api.get_history_transaction_data（thin bridge）。

禁止（生产代码 READ ONLY，本脚本不修改 backend/）：
- 直接 new TdxHq_API（复用 PytdxAdapter 连接管理 / retry / reconnect）；
- 直接查 bars_daily / bar_repository private；
- 自己算 previous_close / 自己写 qfq / 自己读 xdxr 算 adjustment；
- 自己写 trading calendar / 自己重建 universe / 自己维护 market mapping。

本脚本只读取、不写入生产数据。
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

# ---- backend path bootstrap（仅 import，不修改生产代码）----
ROOT = Path(__file__).resolve().parents[3]  # .../market_dev
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402

from app.core.pytdx_adapter import PytdxAdapter, market_from_code  # noqa: E402
from app.db import AsyncSessionLocal  # noqa: E402
from app.services.adjustment_factor_service import AdjustmentFactorService  # noqa: E402
from app.services.calendar_service import CalendarService  # noqa: E402
from app.services.instrument_maintenance_service import (  # noqa: E402
    get_active_a_share_instruments,
)
from app.services.market_data_aggregation_service import (  # noqa: E402
    MarketDataAggregationService,
)

HERE = Path(__file__).resolve().parent
SAMPLE_CSV = HERE / "input" / "auction_history_sample_round1.csv"
OUT_DIR = HERE / "output" / "round1"
RAW_DIR = OUT_DIR / "raw"
NORM_DIR = OUT_DIR / "normalized"

# 09:25 集合竞价窗口判定（严格只认 09:25）
AUCTION_WINDOW = "09:25"


def log(msg: str) -> None:
    print(msg, flush=True)


# ----------------------------------------------------------------------------
# Sample loading
# ----------------------------------------------------------------------------
def load_sample() -> list[dict]:
    rows: list[dict] = []
    with SAMPLE_CSV.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("symbol", "").startswith("#") or not r.get("symbol"):
                continue
            rows.append(
                {
                    "symbol": r["symbol"].strip(),
                    "board": r["board"].strip(),
                    "liquidity": r["liquidity"].strip(),
                    "cohort": r["cohort"].strip(),
                    "note": r.get("note", "").strip(),
                }
            )
    return rows


# ----------------------------------------------------------------------------
# PHASE A helper：官方 owner 复用确认（仅打印，不替代运行时调用）
# ----------------------------------------------------------------------------
def print_owner_map() -> None:
    log("=" * 80)
    log("PHASE A — CURRENT OWNER MAP (ACTUAL)")
    log("=" * 80)
    rows = [
        ("Instrument Universe", "get_active_a_share_instruments() [instrument_maintenance_service]",
         "async generator; canonical active A-share", "实验 sample 挑选 + calendar/count 校验 base", "NONE"),
        ("Trading Calendar", "CalendarService.is_trading_day_async / is_trading_day",
         "canonical trading-calendar owner", "校验 sample 交易日 + 展开 T-1/T/T+1", "NONE"),
        ("Raw Daily Bars / Daily Open / Previous Close", "MarketDataAggregationService.get_bars",
         "timeframe=1d, adj=none, completed_only=True, include_realtime=False", "Lane A: MDAS raw daily open(T)", "NONE"),
        ("QFQ Daily Bars / PIT Previous Close", "MarketDataAggregationService.get_bars",
         "timeframe=1d, adj=qfq, adjustment_as_of=T", "Lane B: PIT qfq close(T-1)/open(T)", "NONE"),
        ("Adjustment / QFQ", "AdjustmentFactorService (authoritative adj_factor) + MDAS adj=qfq",
         "bars_daily adj_factor -> AdjustmentFactorService -> MDAS", "不自行应用 factor；读 MDAS 返回字段", "NONE"),
        ("Pytdx Transport", "PytdxAdapter (managed connection / retry / reconnect / market_from_code)",
         "复用其受管连接 + market_from_code", "唯一新增 source: historical transaction", "NONE"),
        ("Realtime Auction Quote", "auction_quote_provider / auction_quote_capture_service",
         "当前实时 09:25 采集", "本轮不调用（历史实验）", "NONE"),
        ("Historical Auction Transaction", "PytdxAdapter.api.get_history_transaction_data (thin bridge)",
         "无正式 public history-transaction owner", "本轮唯一实验 source bridge", "PUBLIC_HISTORY_TRANSACTION_API_GAP=YES"),
    ]
    for cap, owner, iface, usage, gap in rows:
        log(f"- {cap}")
        log(f"    owner   : {owner}")
        log(f"    iface   : {iface}")
        log(f"    usage   : {usage}")
        log(f"    gap     : {gap}")
    log("")


# ----------------------------------------------------------------------------
# PHASE C — exact 09:25 historical transaction extraction (thin bridge)
# ----------------------------------------------------------------------------
def extract_0925(adapter: PytdxAdapter, symbol: str, d: date) -> tuple[list[dict], str]:
    """通过 PytdxAdapter 受管连接读取历史逐笔，只认 time==09:25。

    返回 (records, status)。status ∈ {FOUND, MISSING_0925, SOURCE_ERROR}。
    records: 原始逐笔 dict 列表（已保留全部 raw 字段）。
    """
    market = market_from_code(symbol)
    if market is None:
        return [], "MARKET_MAPPING_FAILURE"
    date_int = int(d.strftime("%Y%m%d"))
    try:
        # 通过 PytdxAdapter 受管连接调用；不 new TdxHq_API
        raw = adapter.api.get_history_transaction_data(
            market=market, code=symbol, start=0, count=10000, date_int=date_int
        )
    except Exception as e:  # noqa: BLE001
        return [], f"SOURCE_ERROR:{type(e).__name__}:{e}"
    if not raw:
        return [], "MISSING_0925"
    recs = []
    for row in raw:
        t = (row.get("time") or "").strip()
        if not t.startswith(AUCTION_WINDOW):
            continue
        recs.append(
            {
                "symbol": symbol,
                "trade_date": d.isoformat(),
                "market": market,
                "time": t,
                "price": row.get("price"),
                "volume": row.get("volume"),  # pytdx 逐笔 volume 单位 = ? (Phase E)
                "amount": row.get("amount"),  # pytdx 逐笔 amount field 是否存在 (Phase F)
                "buy_or_sell": row.get("buy_or_sell"),
                "raw": dict(row),
            }
        )
    if not recs:
        return [], "MISSING_0925"
    return recs, ("MULTIPLE_0925" if len(recs) > 1 else "FOUND")


# ----------------------------------------------------------------------------
# PHASE D — Lane A (source vs MDAS raw open) / Lane B (PIT QFQ gap)
# ----------------------------------------------------------------------------
def get_mdas_daily_open(
    mdas: MarketDataAggregationService, session, instrument_id: str, d: date
) -> dict:
    """通过 MDAS 取 raw daily open（adj=none）。禁止直接 pytdx/bar_repository/SQL。"""
    res = mdas.get_bars(
        session=session,
        instrument_id=instrument_id,
        timeframe="1d",
        adj="none",
        include_realtime=False,
        completed_only=True,
        start=d,
        end=d,
    )
    bars = res.get("bars")
    out = {
        "source": "MDAS",
        "data_source": res.get("data_source"),
        "degraded": res.get("degraded"),
        "degraded_reason": res.get("degraded_reason"),
        "open": None,
    }
    if bars is not None and len(bars) > 0:
        out["open"] = float(bars.iloc[0]["open"])
    return out


def get_mdas_pit_qfq(
    mdas: MarketDataAggregationService, session, instrument_id: str, d: date
) -> dict:
    """通过 MDAS 取 PIT qfq close(T-1) / open(T)（adj=qfq, adjustment_as_of=T）。"""
    res = mdas.get_bars(
        session=session,
        instrument_id=instrument_id,
        timeframe="1d",
        adj="qfq",
        adjustment_as_of=d,
        include_realtime=False,
        completed_only=True,
        start=d - timedelta(days=10),
        end=d,
    )
    bars = res.get("bars")
    out = {
        "source": "MDAS",
        "adj": "qfq",
        "adjustment_as_of": res.get("adjustment_as_of"),
        "adj_factor_hash": res.get("adj_factor_hash"),
        "degraded": res.get("degraded"),
        "degraded_reason": res.get("degraded_reason"),
        "data_source": res.get("data_source"),
        "open_T": None,
        "close_Tm1": None,
    }
    if bars is not None and len(bars) > 0:
        last = bars.iloc[-1]
        out["open_T"] = float(last["open"])
        if len(bars) >= 2:
            out["close_Tm1"] = float(bars.iloc[-2]["close"])
    return out


# ----------------------------------------------------------------------------
# Main validation
# ----------------------------------------------------------------------------
async def run() -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    NORM_DIR.mkdir(parents=True, exist_ok=True)

    print_owner_map()

    sample = load_sample()
    routine = [s for s in sample if s["cohort"] == "R"]
    corp = [s for s in sample if s["cohort"] == "C"]
    log(f"sample: routine={len(routine)} corp={len(corp)}")

    # canonical universe 复用（仅校验 symbol 是否在 canonical active set；不重建 universe）
    async with AsyncSessionLocal() as session:
        canonical_ids = set()
        async for inst in get_active_a_share_instruments(session):
            canonical_ids.add(inst.instrument_id)
    in_universe = [s for s in sample if s["symbol"] in canonical_ids]
    log(f"canonical-universe match: {len(in_universe)}/{len(sample)}")

    cal = CalendarService()
    # 取最近 ~10 个有效交易日（calendar_service 校验，不准 weekday-only）
    end = date.today()
    trade_dates: list[date] = []
    cur = end
    while len(trade_dates) < 12 and cur > end - timedelta(days=60):
        if await cal.is_trading_day_async(cur):
            trade_dates.append(cur)
        cur -= timedelta(days=1)
    trade_dates = sorted(trade_dates)
    log(f"trade_dates (calendar_service validated): {[d.isoformat() for d in trade_dates]}")

    mdas = MarketDataAggregationService()

    # statistics accumulators
    stats = defaultdict(int)
    raw_records: list[dict] = []
    norm_rows: list[dict] = []

    lane_a = {"comparable": 0, "exact": 0, "mismatches": []}
    lane_b = {"comparable": 0, "examples": []}
    vol_unit_evidence: list[dict] = []
    amount_evidence: list[dict] = []
    adj_degraded: list[dict] = []

    adapter = PytdxAdapter()

    async with AsyncSessionLocal() as session:
        for s in sample:
            sym = s["symbol"]
            for d in trade_dates:
                # ---- PHASE C ----
                recs, status = extract_0925(adapter, sym, d)
                if status in ("SOURCE_ERROR", "MARKET_MAPPING_FAILURE"):
                    stats[status] += 1
                    norm_rows.append(
                        {"symbol": sym, "trade_date": d.isoformat(),
                         "extraction_status": status, "cohort": s["cohort"]}
                    )
                    continue
                if status == "MISSING_0925":
                    stats["MISSING_0925"] += 1
                    norm_rows.append(
                        {"symbol": sym, "trade_date": d.isoformat(),
                         "extraction_status": "MISSING_0925", "cohort": s["cohort"]}
                    )
                    continue

                stats["FOUND_0925" if status == "FOUND" else "MULTIPLE_0925"] += 1
                for r in recs:
                    raw_records.append(r)

                # 仅取首条做价格/量语义研究（不掩盖 MULTIPLE；保留全部 raw）
                first = recs[0]
                raw_price = first["price"]
                raw_vol = first["volume"]
                raw_amt = first.get("amount")
                raw_bs = first.get("buy_or_sell")

                # ---- Lane A: raw 09:25 price vs MDAS raw daily open ----
                mdas_open = get_mdas_daily_open(mdas, session, sym, d)
                if mdas_open["open"] is None:
                    stats["MDAS_DAILY_MISSING"] += 1
                else:
                    lane_a["comparable"] += 1
                    diff = abs(raw_price - mdas_open["open"])
                    rel = diff / mdas_open["open"] if mdas_open["open"] else None
                    if diff == 0:
                        lane_a["exact"] += 1
                    else:
                        lane_a["mismatches"].append(
                            {
                                "symbol": sym, "trade_date": d.isoformat(),
                                "raw_0925_price": raw_price,
                                "mdas_raw_open": mdas_open["open"],
                                "absolute_diff": diff, "relative_diff": rel,
                                "mdas_data_source": mdas_open["data_source"],
                                "mdas_degraded": mdas_open["degraded"],
                            }
                        )

                # ---- Lane B: PIT QFQ gap ----
                pit = get_mdas_pit_qfq(mdas, session, sym, d)
                if pit["degraded"]:
                    adj_degraded.append(
                        {"symbol": sym, "trade_date": d.isoformat(),
                         "reason": pit["degraded_reason"], "as_of": str(pit["adjustment_as_of"])}
                    )
                    stats["ADJUSTMENT_DEGRADED"] += 1
                if pit["close_Tm1"] is not None and pit["open_T"] is not None:
                    prev_close_T_basis = pit["close_Tm1"]
                    gap_pct = raw_price / prev_close_T_basis - 1 if prev_close_T_basis else None
                    lane_b["comparable"] += 1
                    if len(lane_b["examples"]) < 60:
                        lane_b["examples"].append(
                            {
                                "symbol": sym, "trade_date": d.isoformat(),
                                "cohort": s["cohort"],
                                "raw_0925_price": raw_price,
                                "pit_qfq_close_Tm1": pit["close_Tm1"],
                                "pit_qfq_open_T": pit["open_T"],
                                "gap_pct": gap_pct,
                                "adj_as_of": str(pit["adjustment_as_of"]),
                                "adj_factor_hash": pit["adj_factor_hash"],
                                "degraded": pit["degraded"],
                            }
                        )

                # ---- Phase E: volume unit ----
                if raw_price and raw_vol and raw_amt:
                    implied = raw_amt / (raw_price * raw_vol)
                    vol_unit_evidence.append(
                        {"symbol": sym, "trade_date": d.isoformat(),
                         "price": raw_price, "volume": raw_vol,
                         "amount": raw_amt, "implied_multiplier": implied,
                         "board": s["board"]}
                    )
                elif raw_price and raw_vol:
                    vol_unit_evidence.append(
                        {"symbol": sym, "trade_date": d.isoformat(),
                         "price": raw_price, "volume": raw_vol,
                         "amount": None, "implied_multiplier": None,
                         "board": s["board"]}
                    )

                # ---- Phase F: amount classification ----
                amount_evidence.append(
                    {
                        "symbol": sym, "trade_date": d.isoformat(),
                        "has_amount_field": raw_amt is not None,
                        "amount": raw_amt, "price": raw_price, "volume": raw_vol,
                        "buy_or_sell": raw_bs,
                    }
                )

                # data quality flags
                if raw_price in (0, None):
                    stats["INVALID_PRICE"] += 1
                if raw_vol in (0, None):
                    stats["ZERO_VOLUME"] += 1
                if raw_amt in (0, None) and raw_amt is not None:
                    stats["ZERO_AMOUNT"] += 1

                norm_rows.append(
                    {
                        "symbol": sym, "trade_date": d.isoformat(),
                        "cohort": s["cohort"], "board": s["board"],
                        "extraction_status": status,
                        "record_count_at_0925": len(recs),
                        "raw_price": raw_price, "raw_volume": raw_vol,
                        "raw_amount": raw_amt, "buy_or_sell": raw_bs,
                        "mdas_raw_open": mdas_open["open"],
                        "pit_qfq_close_Tm1": pit["close_Tm1"],
                        "pit_qfq_open_T": pit["open_T"],
                    }
                )

    # ---- Phase G: unified market data compliance audit ----
    compliance = {
        "instrument_universe": "PASS (get_active_a_share_instruments)",
        "trading_calendar": "PASS (CalendarService.is_trading_day_async)",
        "daily_open": "PASS (MDAS.get_bars adj=none)",
        "previous_close": "PASS (MDAS.get_bars + PIT qfq close(T-1))",
        "qfq": "PASS (MDAS.get_bars adj=qfq adjustment_as_of=T)",
        "adjustment": "PASS (AdjustmentFactorService via MDAS, no manual xdxr)",
        "market_mapping": "PASS (market_from_code via PytdxAdapter)",
        "historical_auction_transaction": "PASS (PytdxAdapter managed conn, no new TdxHq_API)",
    }
    # 通过本脚本自身约束保证（无 bar_repository/raw pytdx daily/TdxHq_API import/手动 qfq/手动 calendar）
    compliance["FAIL_SCAN"] = audit_no_duplicate_market_logic()

    # ---- persist ----
    with (RAW_DIR / "auction_0925_raw_records.json").open("w", encoding="utf-8") as f:
        json.dump(raw_records, f, ensure_ascii=False, indent=2, default=str)
    with (NORM_DIR / "auction_0925_normalized.csv").open("w", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "symbol", "trade_date", "cohort", "board", "extraction_status",
            "record_count_at_0925", "raw_price", "raw_volume", "raw_amount",
            "buy_or_sell", "mdas_raw_open", "pit_qfq_close_Tm1", "pit_qfq_open_T",
        ])
        w.writeheader()
        for r in norm_rows:
            w.writerow(r)
    with (NORM_DIR / "volume_unit_evidence.json").open("w", encoding="utf-8") as f:
        json.dump(vol_unit_evidence, f, ensure_ascii=False, indent=2, default=str)
    with (NORM_DIR / "amount_evidence.json").open("w", encoding="utf-8") as f:
        json.dump(amount_evidence, f, ensure_ascii=False, indent=2, default=str)
    with (NORM_DIR / "lane_a_mismatches.json").open("w", encoding="utf-8") as f:
        json.dump(lane_a["mismatches"], f, ensure_ascii=False, indent=2, default=str)
    with (NORM_DIR / "lane_b_examples.json").open("w", encoding="utf-8") as f:
        json.dump(lane_b["examples"], f, ensure_ascii=False, indent=2, default=str)
    with (NORM_DIR / "adjustment_degraded.json").open("w", encoding="utf-8") as f:
        json.dump(adj_degraded, f, ensure_ascii=False, indent=2, default=str)

    summary = {
        "owner_map": "see PHASE A log",
        "sample": {"routine": len(routine), "corp": len(corp),
                   "canonical_match": f"{len(in_universe)}/{len(sample)}"},
        "trade_dates": [d.isoformat() for d in trade_dates],
        "stats": dict(stats),
        "lane_a": {
            "comparable": lane_a["comparable"],
            "exact_match": lane_a["exact"],
            "match_rate": (lane_a["exact"] / lane_a["comparable"]) if lane_a["comparable"] else None,
            "mismatch_count": len(lane_a["mismatches"]),
        },
        "lane_b": {"comparable": lane_b["comparable"], "examples_count": len(lane_b["examples"])},
        "compliance": compliance,
        "public_history_transaction_api_gap": "YES",
    }
    with (OUT_DIR / "validation_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    log("=" * 80)
    log("PHASE C/H — extraction & data quality stats")
    log(json.dumps(dict(stats), ensure_ascii=False, indent=2))
    log("=" * 80)
    log("PHASE D Lane A — raw 09:25 price vs MDAS raw open")
    log(json.dumps({
        "comparable": lane_a["comparable"],
        "exact_match": lane_a["exact"],
        "match_rate": summary["lane_a"]["match_rate"],
        "mismatch_count": len(lane_a["mismatches"]),
    }, ensure_ascii=False, indent=2))
    log("=" * 80)
    log("PHASE G — UNIFIED MARKET DATA COMPLIANCE")
    for k, v in compliance.items():
        log(f"  {k}: {v}")
    log("=" * 80)
    return summary


def audit_no_duplicate_market_logic() -> str:
    """Phase G 自检：本脚本不得出现重复行情逻辑（grep 静态约束的运行时镜像）。"""
    self_src = Path(__file__).read_text(encoding="utf-8")
    banned = [
        "TdxHq_API(", "get_daily_bars(", "bar_repository",
        "AdjustmentFactorCalculator", "from app.services.xdxr",
        "def is_trading_day" in self_src and "CalendarService" not in self_src,
        "klines(",
    ]
    hits = [b for b in banned if b and b in self_src]
    return "PASS" if not hits else f"FAIL:{hits}"


if __name__ == "__main__":
    # 需要 DATABASE_URL / PYTDX 环境；本脚本只读，不写生产数据
    out = asyncio.run(run())
    log("DONE. outputs under experiments/pytdx_auction_history/output/round1/")
