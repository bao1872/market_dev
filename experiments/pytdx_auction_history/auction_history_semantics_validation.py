"""Auction Historical Validation Evidence Pipeline — Round 1D.

Final Runner Integrity Correction only.

目标：把 runner 修成「真实执行一次 --live 即可产生可信、完整、可审计 evidence」。

只解决 5 个已确认 blocker：
  1. Corporate Action 仍错误使用 unresolved UUID(0) sample；
  2. Corporate T-1 / T+1 当前实际上可能返回 T 本身；
  3. noncanonical 09:25 逻辑错误地把全天其他成交都纳入；
  4. raw evidence 没真正贯通 observation → writer；
  5. Volume unit 当前依赖不存在的 raw amount，真实 live 无法验证。

本轮禁止：
  - --live / SSH 真实实验 / production write / deploy / 120-day backfill
  - 重新设计 Auction 架构 / 修改 production code
  - 冻结 volume unit / 推导正式 auction_amount
  - 新建第二套 daily/calendar/universe/qfq/adjustment/mapping/pytdx 连接

权威 owner（只读复用，禁止第二套）：
  - Instrument Universe   → feature_snapshot_service.get_active_a_share_instruments
  - Instrument identity   → experiment-only read-only Instrument ORM resolution
  - Trading Calendar      → calendar_service.is_trading_day_async
  - Daily OHLCV           → MarketDataAggregationService (MDAS)
  - Previous Close        → MDAS
  - PIT QFQ               → MDAS adjustment_as_of=T
  - Adjustment discovery  → AdjustmentFactorService.get_factor_series (仅挑真实 event T)
  - Historical transaction→ PytdxAdapter managed connection
                            adapter.api.get_history_transaction_data(...)
"""

from __future__ import annotations

import csv
import json
import re
import statistics
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

# ---------------------------------------------------------------------------
# 权威 owner（禁止第二套 market logic / xdxr 重算）
# ---------------------------------------------------------------------------
from app.services.feature_snapshot_service import get_active_a_share_instruments
from app.services.calendar_service import is_trading_day_async
from app.services.market_data_aggregation_service import MarketDataAggregationService
from app.services.adjustment_factor_service import AdjustmentFactorService
from app.core.pytdx_adapter import PytdxAdapter, market_from_code
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

EXPERIMENT_DIR = Path(__file__).resolve().parent
SAMPLE_FILE = EXPERIMENT_DIR / "samples" / "auction_history_round1.csv"
OUTPUT_DIR = EXPERIMENT_DIR / "output" / "round1"

UUID_ZERO = UUID(int=0)

# 竞价时间规范
CANONICAL_AUCTION_TIMES = {"09:25", "09:25:00"}

# 分页参数（沿用 pytdx 探索脚本历史可工作约定）
PAGE_SIZE = 800
MAX_PAGES = 200
MAX_RECORDS = 200 * 800  # 单日逐笔上限保护

CORPORATE_LOOKBACK_DAYS = 180


# ===========================================================================
# DTO
# ===========================================================================
@dataclass
class SampleInstrument:
    symbol: str
    market: str
    instrument_id: UUID
    board: str
    coverage_tag: str
    cohort: str  # "routine" | "corporate"


@dataclass
class NormalizedAuctionTransaction:
    symbol: str
    market: str
    instrument_id: str
    trade_date: str
    source_time: str
    canonical_time: Optional[str]
    raw_price: float
    raw_volume_value: Optional[float]
    raw_amount_value: Optional[float]
    buy_sell_raw: Optional[int]
    source_record: dict
    source_schema_keys: list


@dataclass
class FullDayTransactionResult:
    status: str  # COMPLETE/EMPTY/SOURCE_ERROR/PAGINATION_STALLED/PAGINATION_LIMIT_REACHED
    records: list
    page_count: int = 0
    record_count: int = 0
    page_size: int = PAGE_SIZE
    source_first_time: Optional[str] = None
    source_last_time: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class AuctionExtractionResult:
    status: str  # FOUND/MULTIPLE_0925/NONCANONICAL_0925_TIME/MISSING_0925/SOURCE_ERROR
    records: list  # canonical 09:25 normalized records
    noncanonical_records: list  # 09:25:xx noncanonical normalized records
    all_records: list  # 完整 full-day records
    full_day_status: str  # 来自 FullDayTransactionResult.status


# ===========================================================================
# 样本加载
# ===========================================================================
def load_sample(path: Path = SAMPLE_FILE) -> list[SampleInstrument]:
    samples = []
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            samples.append(SampleInstrument(
                symbol=row["symbol"].strip(),
                market=row["market"].strip(),
                instrument_id=UUID_ZERO,  # 占位，resolve 前一律 UUID(0)
                board=row["board"].strip(),
                coverage_tag=row["coverage_tag"].strip(),
                cohort=row["cohort"].strip(),
            ))
    return samples


# ===========================================================================
# 身份解析（experiment-only, read-only Instrument ORM）
# ===========================================================================
async def resolve_sample_instruments(
    session: AsyncSession,
    samples: list[SampleInstrument],
) -> tuple[list[SampleInstrument], list[dict]]:
    """按 (market, symbol) 解析真实 instrument_id，并校验 canonical universe 成员。

    权威 universe 成员 = get_active_a_share_instruments(session)。
    任何 UUID(0) 未解析即 fail-fast INTERNAL_IDENTITY_ERROR。
    同一 (market, symbol) 命中多个 active 行 → IDENTITY_AMBIGUOUS。
    解析到的 UUID 不在 canonical universe → SAMPLE_NOT_IN_CANONICAL_UNIVERSE。
    """
    resolved: list[SampleInstrument] = []
    skipped: list[dict] = []

    # 权威 universe 成员（唯一 membership authority）
    canonical_ids = set(await get_active_a_share_instruments(session))

    # 去重查询键（identity resolution 仅负责 (market, symbol) → UUID）
    keys = {(s.market, s.symbol) for s in samples}
    rows_by_key: dict[tuple[str, str], list[dict]] = {}
    for market, symbol in keys:
        stmt = text(
            "SELECT market, symbol, id FROM instruments "
            "WHERE market = :market AND symbol = :symbol"
        )
        result = await session.execute(stmt, {"market": market, "symbol": symbol})
        rows = [dict(r) for r in result.mappings().all()]
        rows_by_key[(market, symbol)] = rows

    for s in samples:
        rows = rows_by_key.get((s.market, s.symbol), [])
        if not rows:
            skipped.append({"symbol": s.symbol, "market": s.market,
                            "reason": "NO_ACTIVE_INSTRUMENT"})
            continue
        if len(rows) > 1:
            skipped.append({"symbol": s.symbol, "market": s.market,
                            "reason": "IDENTITY_AMBIGUOUS",
                            "candidates": [str(r["id"]) for r in rows]})
            continue
        rid = rows[0]["id"]
        if rid == UUID_ZERO:
            raise RuntimeError(
                f"INTERNAL_IDENTITY_ERROR: resolved id is UUID(0) for "
                f"{s.market}/{s.symbol}")
        if rid not in canonical_ids:
            skipped.append({"symbol": s.symbol, "market": s.market,
                            "instrument_id": str(rid),
                            "reason": "SAMPLE_NOT_IN_CANONICAL_UNIVERSE"})
            continue
        resolved.append(SampleInstrument(
            symbol=s.symbol, market=s.market, instrument_id=rid,
            board=s.board, coverage_tag=s.coverage_tag, cohort=s.cohort,
        ))
    return resolved, skipped


# ===========================================================================
# 时间规范化与分类（MOD3）
# ===========================================================================
def _normalize_auction_time(raw_time: str) -> Optional[str]:
    """仅 canonical exact form 返回 '09:25'，否则 None。"""
    if raw_time is None:
        return None
    t = str(raw_time).strip()
    if t in CANONICAL_AUCTION_TIMES:
        return "09:25"
    return None


class TransactionTimeClass:
    CANONICAL_0925 = "CANONICAL_0925"
    NONCANONICAL_0925 = "NONCANONICAL_0925"
    OTHER = "OTHER"


def classify_transaction_time(raw_time: str) -> str:
    """三类分类：
    - CANONICAL_0925   : 仅 '09:25' / '09:25:00'
    - NONCANONICAL_0925: 以 '09:25' 开头但非 exact form（如 09:25:01）
    - OTHER            : 其他一切（09:24:59 / 09:30 / 10:15 / 14:57 ...）
    """
    if raw_time is None:
        return TransactionTimeClass.OTHER
    t = str(raw_time).strip()
    if t in CANONICAL_AUCTION_TIMES:
        return TransactionTimeClass.CANONICAL_0925
    if t.startswith("09:25"):
        return TransactionTimeClass.NONCANONICAL_0925
    return TransactionTimeClass.OTHER


# ===========================================================================
# 原始交易记录归一化
# ===========================================================================
def _normalize_raw_transaction(
    symbol: str,
    market: str,
    instrument_id: str,
    trade_date: date,
    rec: dict,
) -> NormalizedAuctionTransaction:
    raw_time = str(rec.get("time", ""))
    raw_price = float(rec.get("price", 0.0) or 0.0)
    vol = rec.get("vol")
    raw_volume_value = float(vol) if vol is not None else None
    # 历史逐笔真实 source 无 amount 字段
    raw_amount_value = None
    bs = rec.get("buyorsell")
    buy_sell_raw = int(bs) if bs is not None else None
    canonical_time = _normalize_auction_time(raw_time)
    return NormalizedAuctionTransaction(
        symbol=symbol, market=market, instrument_id=instrument_id,
        trade_date=trade_date.isoformat(), source_time=raw_time,
        canonical_time=canonical_time, raw_price=raw_price,
        raw_volume_value=raw_volume_value, raw_amount_value=raw_amount_value,
        buy_sell_raw=buy_sell_raw, source_record=rec,
        source_schema_keys=sorted(rec.keys()),
    )


# ===========================================================================
# MOD4 / MOD5 / MOD15 — 分页完整日交易抓取（单一 source truth）
# ===========================================================================
def _page_fingerprint(page: list[dict]) -> str:
    if not page:
        return "EMPTY"
    first = page[0]
    last = page[-1]
    return f"{first.get('time')}|{last.get('time')}|{len(page)}"


def fetch_full_day_transactions_paginated(
    adapter: PytdxAdapter,
    symbol: str,
    market_int: int,
    trade_date: date,
) -> FullDayTransactionResult:
    """通过 PytdxAdapter managed connection 分页取得完整交易日逐笔。

    分页契约：
      - 仍调用 adapter.api.get_history_transaction_data(market, code, start, count, date_int)
      - 终止条件：page 为空 或 len(page) < page_size
      - 安全 guard：MAX_PAGES / MAX_RECORDS（达到 → PAGINATION_LIMIT_REACHED）
      - 重复页保护：相邻 page fingerprint 重复 / offset 前进但 source 不变
        → PAGINATION_STALLED
    """
    date_int = int(trade_date.strftime("%Y%m%d"))
    all_records: list[dict] = []
    offset = 0
    page_count = 0
    prev_fp = None
    source_first_time: Optional[str] = None
    source_last_time: Optional[str] = None

    try:
        while True:
            if page_count >= MAX_PAGES or len(all_records) >= MAX_RECORDS:
                return FullDayTransactionResult(
                    status="PAGINATION_LIMIT_REACHED", records=all_records,
                    page_count=page_count, record_count=len(all_records),
                    source_first_time=source_first_time,
                    source_last_time=source_last_time,
                )
            page = adapter.api.get_history_transaction_data(
                market=market_int, code=symbol, start=offset,
                count=PAGE_SIZE, date_int=date_int,
            ) or []
            page_count += 1

            if not page:
                break

            fp = _page_fingerprint(page)
            if prev_fp is not None and fp == prev_fp:
                # 重复页：source 不再前进
                return FullDayTransactionResult(
                    status="PAGINATION_STALLED", records=all_records,
                    page_count=page_count, record_count=len(all_records),
                    source_first_time=source_first_time,
                    source_last_time=source_last_time,
                    error_code="PAGINATION_STALLED",
                    error_message=f"repeated page fingerprint at offset={offset}",
                )
            prev_fp = fp

            if source_first_time is None:
                source_first_time = str(page[0].get("time", ""))
            source_last_time = str(page[-1].get("time", ""))

            all_records.extend(page)
            offset += len(page)

            if len(page) < PAGE_SIZE:
                break

        if not all_records:
            return FullDayTransactionResult(
                status="EMPTY", records=[], page_count=page_count,
                record_count=0, source_first_time=None, source_last_time=None,
            )
        return FullDayTransactionResult(
            status="COMPLETE", records=all_records, page_count=page_count,
            record_count=len(all_records), page_size=PAGE_SIZE,
            source_first_time=source_first_time,
            source_last_time=source_last_time,
        )
    except Exception as exc:  # noqa: BLE001 - source 异常必须结构化
        return FullDayTransactionResult(
            status="SOURCE_ERROR", records=[], page_count=page_count,
            record_count=len(all_records),
            source_first_time=source_first_time,
            source_last_time=source_last_time,
            error_code=type(exc).__name__,
            error_message=str(exc),
        )


# ===========================================================================
# MOD5 / MOD6 — 单日完整 dataset 复用于多个实验问题
# ===========================================================================
def extract_from_full_day(
    symbol: str,
    market: str,
    instrument_id: str,
    trade_date: date,
    full_day: FullDayTransactionResult,
) -> AuctionExtractionResult:
    """从同一个完整 dataset 提取：
      - canonical 09:25 records
      - noncanonical 09:25:xx records
      - 完整 full-day records（供 volume sum 使用）
    """
    if full_day.status == "SOURCE_ERROR":
        return AuctionExtractionResult(
            status="SOURCE_ERROR", records=[], noncanonical_records=[],
            all_records=[], full_day_status=full_day.status)
    if full_day.status != "COMPLETE":
        # 分页未完整：不能做 exact 09:25 uniqueness 判断
        return AuctionExtractionResult(
            status="SOURCE_PAGINATION_INCOMPLETE", records=[],
            noncanonical_records=[], all_records=[],
            full_day_status=full_day.status)

    canonical: list[NormalizedAuctionTransaction] = []
    noncanonical: list[NormalizedAuctionTransaction] = []
    normalized_all: list[NormalizedAuctionTransaction] = []

    for rec in full_day.records:
        n = _normalize_raw_transaction(symbol, market, instrument_id, trade_date, rec)
        normalized_all.append(n)
        cls = classify_transaction_time(n.source_time)
        if cls == TransactionTimeClass.CANONICAL_0925:
            canonical.append(n)
        elif cls == TransactionTimeClass.NONCANONICAL_0925:
            noncanonical.append(n)

    if len(canonical) == 1:
        status = "FOUND"
    elif len(canonical) > 1:
        status = "MULTIPLE_0925"
    elif len(noncanonical) >= 1:
        status = "NONCANONICAL_0925_TIME"
    else:
        status = "MISSING_0925"

    return AuctionExtractionResult(
        status=status, records=canonical, noncanonical_records=noncanonical,
        all_records=normalized_all, full_day_status=full_day.status)


# ===========================================================================
# MDAS daily bars 辅助
# ===========================================================================
def get_bar_for_date(bars_df, target: date):
    if bars_df is None or len(bars_df) == 0:
        return None
    ts = pd.Timestamp(target)
    if ts in bars_df.index:
        return bars_df.loc[ts]
    return None


def get_prev_bar_before(bars_df, target: date):
    if bars_df is None or len(bars_df) == 0:
        return None
    ts = pd.Timestamp(target)
    prior = [idx for idx in bars_df.index if idx < ts]
    if not prior:
        return None
    return bars_df.loc[max(prior)]


# ===========================================================================
# Lane A / Lane B
# ===========================================================================
def compute_lane_a(auction_price_raw, open_bar_T, mdas_data_source,
                   mdas_degraded, mdas_degraded_reason):
    if open_bar_T is None:
        return {
            "status": "LANE_A_MISSING_MDA_OPEN",
            "mdas_raw_open_T": None, "price_exact_match": None,
            "price_diff_abs": None, "price_diff_rel": None,
            "mdas_data_source": mdas_data_source, "mdas_degraded": mdas_degraded,
            "mdas_degraded_reason": mdas_degraded_reason,
        }
    open_T = float(open_bar_T["open"])
    diff_abs = auction_price_raw - open_T
    diff_rel = (diff_abs / open_T) if open_T != 0 else None
    return {
        "status": "COMPUTED",
        "mdas_raw_open_T": open_T,
        "price_exact_match": abs(diff_abs) < 1e-9,
        "price_diff_abs": diff_abs,
        "price_diff_rel": diff_rel,
        "mdas_data_source": mdas_data_source, "mdas_degraded": mdas_degraded,
        "mdas_degraded_reason": mdas_degraded_reason,
    }


def compute_lane_b(auction_price_raw, raw_Tm1, raw_T, qfq_Tm1, qfq_T,
                   target, adj_factor_hash, mdas_data_source,
                   mdas_degraded, mdas_degraded_reason):
    if raw_Tm1 is None or qfq_Tm1 is None:
        return {
            "status": "LANE_B_MISSING_PREV_BAR",
            "naive_raw_gap": None, "pit_gap": None,
            "raw_close_Tm1": None, "qfq_close_Tm1": None,
            "adj_factor_hash": adj_factor_hash,
            "mdas_data_source": mdas_data_source,
            "mdas_degraded": mdas_degraded,
            "mdas_degraded_reason": mdas_degraded_reason,
        }
    raw_close_Tm1 = float(raw_Tm1["close"])
    qfq_close_Tm1 = float(qfq_Tm1["close"])
    naive_raw_gap = (auction_price_raw / raw_close_Tm1 - 1) if raw_close_Tm1 else None
    pit_gap = (auction_price_raw / qfq_close_Tm1 - 1) if qfq_close_Tm1 else None
    return {
        "status": "COMPUTED",
        "naive_raw_gap": naive_raw_gap,
        "pit_gap": pit_gap,
        "raw_close_Tm1": raw_close_Tm1,
        "qfq_close_Tm1": qfq_close_Tm1,
        "adj_factor_hash": adj_factor_hash,
        "mdas_data_source": mdas_data_source,
        "mdas_degraded": mdas_degraded,
        "mdas_degraded_reason": mdas_degraded_reason,
    }


# ===========================================================================
# Volume / Amount evidence（MOD7 / MOD9）
# ===========================================================================
def compute_volume_evidence(price, volume, amount):
    """历史逐笔真实 source 无 raw amount，本函数保留接口但 caller 应传 None。"""
    if price and volume and amount:
        return {
            "implied_multiplier": amount / (price * volume),
            "reason": "COMPUTED_PRICE_VOLUME_AMOUNT",
        }
    return {"implied_multiplier": None, "reason": "RAW_AMOUNT_FIELD_ABSENT"}


def compute_amount_evidence():
    """MOD9：历史逐笔无 raw amount，Amount 必须保持 unresolved。"""
    return {
        "amount_source_type": "RAW_FIELD_ABSENT",
        "candidate_derived_amount": None,
        "evidence_reason": "VOLUME_UNIT_NOT_YET_CONFIRMED",
    }


# ===========================================================================
# 交易日历（MOD2：exclusive T-1 / T+1）
# ===========================================================================
async def previous_trading_day_before(session, T: date) -> Optional[date]:
    cur = T - timedelta(days=1)
    while cur > T - timedelta(days=30):
        if await is_trading_day_async(session, cur):
            return cur
        cur -= timedelta(days=1)
    return None


async def next_trading_day_after(session, T: date) -> Optional[date]:
    cur = T + timedelta(days=1)
    while cur < T + timedelta(days=30):
        if await is_trading_day_async(session, cur):
            return cur
        cur += timedelta(days=1)
    return None


async def previous_trading_dates(session, T: date, n: int) -> list[date]:
    """返回 T 及之前最多 n-1 个正式交易日。

    若 T 本身是交易日则包含 T；否则从最近 previous official trading day 开始。
    禁止假定 as_of 一定是交易日（MOD7）。
    """
    out: list[date] = []
    cur = T
    while len(out) < n and cur > T - timedelta(days=400):
        if await is_trading_day_async(session, cur):
            out.append(cur)
        cur -= timedelta(days=1)
    return out


# ===========================================================================
# Corporate action（MOD1 / MOD2 / MOD10）
# ===========================================================================
async def resolve_corporate_cases(
    adj_service,
    session,
    resolved_corporate: list[SampleInstrument],
    as_of: date,
    lookback_days: int,
) -> list[dict]:
    """MOD1：入参必须是已 resolve 的 corporate（instrument_id 非 UUID(0)）。

    RESOLVED case 必须包含真实 factor_before / factor_after（MOD3）。
    """
    out = []
    for inst in resolved_corporate:
        if inst.instrument_id == UUID_ZERO:
            raise ValueError(
                f"INTERNAL_IDENTITY_ERROR: corporate {inst.market}/{inst.symbol} "
                f"still has UUID(0) — must be resolved before corporate resolution")
        df = await adj_service.get_factor_series(
            session, inst.instrument_id, as_of=as_of)
        # 真实 corporate event T + factor_before/factor_after 发现（仅挑选）
        event = _discover_event_date(df, as_of, lookback_days)
        if event is None:
            out.append({
                "symbol": inst.symbol, "market": inst.market,
                "instrument_id": str(inst.instrument_id), "board": inst.board,
                "status": "NO_EVENT_IN_LOOKBACK", "event_date": None,
                "prev_trade_date": None, "next_trade_date": None,
                "factor_before": None, "factor_after": None,
            })
            continue
        prev_d = await previous_trading_day_before(session, event["event_date"])
        next_d = await next_trading_day_after(session, event["event_date"])
        # MOD10: hard assertion
        if prev_d is not None:
            assert prev_d < event["event_date"], (
                f"prev_d {prev_d} not < event {event['event_date']}")
        if next_d is not None:
            assert next_d > event["event_date"], (
                f"next_d {next_d} not > event {event['event_date']}")
        # MOD3: factor event 完整前后值才 RESOLVED
        if (event["factor_before"] is None or event["factor_after"] is None
                or event["factor_before"] == event["factor_after"]):
            out.append({
                "symbol": inst.symbol, "market": inst.market,
                "instrument_id": str(inst.instrument_id), "board": inst.board,
                "status": "FACTOR_EVENT_INCOMPLETE",
                "event_date": event["event_date"].isoformat(),
                "prev_trade_date": prev_d.isoformat() if prev_d else None,
                "next_trade_date": next_d.isoformat() if next_d else None,
                "factor_before": event["factor_before"],
                "factor_after": event["factor_after"],
            })
            continue
        out.append({
            "symbol": inst.symbol, "market": inst.market,
            "instrument_id": str(inst.instrument_id), "board": inst.board,
            "status": "RESOLVED", "event_date": event["event_date"].isoformat(),
            "prev_trade_date": prev_d.isoformat() if prev_d else None,
            "next_trade_date": next_d.isoformat() if next_d else None,
            "factor_before": event["factor_before"],
            "factor_after": event["factor_after"],
        })
    return out


def _discover_event_date(df, as_of: date, lookback_days: int) -> Optional[dict]:
    """返回 CorporateFactorEvent 结构（MOD3）：

    {
        "event_date": date,
        "factor_before": float | None,
        "factor_after": float | None,
    }

    仅从 authoritative factor series 中读取；不自行反推 / 重算 / 重建。
    找不到有效的 factor-change 则返回 None。
    """
    if df is None or len(df) == 0:
        return None
    col = "trade_date" if "trade_date" in df.columns else df.columns[0]
    if "adj_factor" not in df.columns:
        return None
    vals = df["adj_factor"].tolist()
    dates = df[col].tolist()
    for i in range(1, len(vals)):
        if vals[i] != vals[i - 1]:
            d = dates[i]
            if isinstance(d, (datetime, pd.Timestamp)):
                d = d.date()
            delta = (as_of - d).days if isinstance(d, date) else None
            if delta is not None and delta <= lookback_days:
                return {
                    "event_date": d,
                    "factor_before": float(vals[i - 1]),
                    "factor_after": float(vals[i]),
                }
    return None


async def run_corporate_observation(
    mdas, adapter, session, inst: SampleInstrument,
    trade_date: date, prev_trade_date: Optional[date],
    next_trade_date: Optional[date],
    factor_before: Optional[float] = None,
    factor_after: Optional[float] = None,
) -> dict:
    obs = _build_observation_base(inst, trade_date, "corporate")
    obs["prev_trade_date"] = prev_trade_date.isoformat() if prev_trade_date else None
    obs["next_trade_date"] = next_trade_date.isoformat() if next_trade_date else None
    if prev_trade_date is not None:
        assert prev_trade_date < trade_date
    if next_trade_date is not None:
        assert next_trade_date > trade_date

    # MOD4: factor evidence 真正贯通 observation
    naive_raw_gap = obs.get("lane_b", {}).get("naive_raw_gap") if obs.get("lane_b") else None
    pit_gap = obs.get("lane_b", {}).get("pit_gap") if obs.get("lane_b") else None
    obs["corporate"] = {
        "event_date": trade_date.isoformat(),
        "prev_trade_date": prev_trade_date.isoformat() if prev_trade_date else None,
        "next_trade_date": next_trade_date.isoformat() if next_trade_date else None,
        "factor_before": factor_before,
        "factor_after": factor_after,
        "naive_raw_gap": naive_raw_gap,
        "pit_gap": pit_gap,
        "gap_adjustment_effect": (
            (pit_gap - naive_raw_gap)
            if (pit_gap is not None and naive_raw_gap is not None) else None),
    }

    full_day = fetch_full_day_transactions_paginated(
        adapter, inst.symbol, market_from_code(inst.symbol), trade_date)
    extraction = extract_from_full_day(
        inst.symbol, inst.market, str(inst.instrument_id), trade_date, full_day)
    obs["full_day_status"] = full_day.status
    obs["extraction_status"] = extraction.status
    obs["raw_records"] = [_raw_evidence_dict(r) for r in extraction.records]
    obs["noncanonical_records"] = [_raw_evidence_dict(r) for r in extraction.noncanonical_records]

    # Lane A
    none_res = await mdas.get_bars(session, inst.instrument_id, adj="none",
                                   end_date=trade_date, limit=10)
    qfq_res = await mdas.get_bars(session, inst.instrument_id, adj="qfq",
                                  end_date=trade_date, adjustment_as_of=trade_date, limit=10)
    open_bar_T = get_bar_for_date(none_res.bars, trade_date)
    if extraction.status == "FOUND":
        auction_price = extraction.records[0].raw_price
        obs["lane_a"] = compute_lane_a(
            auction_price, open_bar_T, none_res.data_source,
            none_res.degraded, none_res.degraded_reason)
        raw_Tm1 = get_prev_bar_before(none_res.bars, trade_date)
        qfq_Tm1 = get_prev_bar_before(qfq_res.bars, trade_date)
        obs["lane_b"] = compute_lane_b(
            auction_price, raw_Tm1, open_bar_T, qfq_Tm1,
            get_bar_for_date(qfq_res.bars, trade_date), trade_date,
            qfq_res.adj_factor_hash, none_res.data_source,
            none_res.degraded, none_res.degraded_reason)
        # MOD4: 回填 corporate gap 字段（Lane B 可比才填）
        if obs.get("lane_b") and obs["lane_b"].get("status") == "COMPUTED":
            obs["corporate"]["naive_raw_gap"] = obs["lane_b"]["naive_raw_gap"]
            obs["corporate"]["pit_gap"] = obs["lane_b"]["pit_gap"]
            obs["corporate"]["gap_adjustment_effect"] = (
                obs["lane_b"]["pit_gap"] - obs["lane_b"]["naive_raw_gap"])
    else:
        obs["lane_a"] = None
        obs["lane_b"] = None

    # Volume + Amount evidence（MOD7 / MOD9）
    obs["volume_evidence"] = _compute_volume_from_full_day(
        inst, trade_date, full_day, none_res)
    obs["amount_evidence"] = compute_amount_evidence()
    obs["raw_amount_value"] = None
    return obs


# ===========================================================================
# Routine observation（MOD4/5/6/7）
# ===========================================================================
async def run_single_observation(
    mdas, adapter, session, inst: SampleInstrument, trade_date: date,
) -> dict:
    obs = _build_observation_base(inst, trade_date, "routine")

    # 单一 source truth：分页完整日
    full_day = fetch_full_day_transactions_paginated(
        adapter, inst.symbol, market_from_code(inst.symbol), trade_date)
    extraction = extract_from_full_day(
        inst.symbol, inst.market, str(inst.instrument_id), trade_date, full_day)
    obs["full_day_status"] = full_day.status
    obs["extraction_status"] = extraction.status
    # MOD6：raw evidence 真正贯通
    obs["raw_records"] = [_raw_evidence_dict(r) for r in extraction.records]
    obs["noncanonical_records"] = [_raw_evidence_dict(r) for r in extraction.noncanonical_records]
    obs["raw_record_count"] = len(obs["raw_records"])
    obs["noncanonical_record_count"] = len(obs["noncanonical_records"])

    none_res = await mdas.get_bars(session, inst.instrument_id, adj="none",
                                   end_date=trade_date, limit=10)
    qfq_res = await mdas.get_bars(session, inst.instrument_id, adj="qfq",
                                  end_date=trade_date, adjustment_as_of=trade_date, limit=10)
    open_bar_T = get_bar_for_date(none_res.bars, trade_date)

    if extraction.status == "FOUND":
        auction_price = extraction.records[0].raw_price
        obs["lane_a"] = compute_lane_a(
            auction_price, open_bar_T, none_res.data_source,
            none_res.degraded, none_res.degraded_reason)
        raw_Tm1 = get_prev_bar_before(none_res.bars, trade_date)
        qfq_Tm1 = get_prev_bar_before(qfq_res.bars, trade_date)
        obs["lane_b"] = compute_lane_b(
            auction_price, raw_Tm1, open_bar_T, qfq_Tm1,
            get_bar_for_date(qfq_res.bars, trade_date), trade_date,
            qfq_res.adj_factor_hash, none_res.data_source,
            none_res.degraded, none_res.degraded_reason)
    else:
        obs["lane_a"] = None
        obs["lane_b"] = None

    # Volume + Amount evidence（MOD7 / MOD9）
    obs["volume_evidence"] = _compute_volume_from_full_day(
        inst, trade_date, full_day, none_res)
    obs["amount_evidence"] = compute_amount_evidence()
    obs["raw_amount_value"] = None
    return obs


def _compute_volume_from_full_day(inst, trade_date, full_day, none_res):
    """MOD7：source 侧 sum raw vol ↔ MDAS daily volume（仅 evidence，不下结论）。"""
    if full_day.status != "COMPLETE":
        return {
            "status": full_day.status,
            "sum_transaction_raw_vol": None,
            "valid_volume_record_count": None,
            "invalid_volume_record_count": None,
            "transaction_record_count": full_day.record_count,
            "mdas_daily_volume": None,
            "daily_volume_ratio": None,
            "pagination_status": full_day.status,
            "page_count": full_day.page_count,
            "mdas_data_source": none_res.data_source,
            "mdas_degraded": none_res.degraded,
            "mdas_degraded_reason": none_res.degraded_reason,
            "evidence_reason": "PAGINATION_INCOMPLETE_NO_VOLUME_INFERENCE",
        }
    valid = 0
    invalid = 0
    s = 0.0
    for rec in full_day.records:
        v = rec.get("vol")
        if v is not None:
            valid += 1
            s += float(v)
        else:
            invalid += 1
    open_bar_T = get_bar_for_date(none_res.bars, trade_date)
    mdas_vol = float(open_bar_T["volume"]) if open_bar_T is not None else None
    ratio = None
    if s > 0 and mdas_vol and mdas_vol > 0:
        ratio = mdas_vol / s
    return {
        "status": "COMPUTED",
        "sum_transaction_raw_vol": s,
        "valid_volume_record_count": valid,
        "invalid_volume_record_count": invalid,
        "transaction_record_count": full_day.record_count,
        "mdas_daily_volume": mdas_vol,
        "daily_volume_ratio": ratio,
        "pagination_status": full_day.status,
        "page_count": full_day.page_count,
        "mdas_data_source": none_res.data_source,
        "mdas_degraded": none_res.degraded,
        "mdas_degraded_reason": none_res.degraded_reason,
        "evidence_reason": "RATIO_DISTRIBUTION_ONLY_NO_UNIT_CONCLUSION",
    }


def _build_observation_base(inst: SampleInstrument, trade_date: date, cohort: str) -> dict:
    return {
        "symbol": inst.symbol, "market": inst.market,
        "instrument_id": str(inst.instrument_id), "board": inst.board,
        "cohort": cohort, "trade_date": trade_date.isoformat(),
        "coverage_tag": inst.coverage_tag,
    }


def _raw_evidence_dict(r: NormalizedAuctionTransaction) -> dict:
    return {
        "symbol": r.symbol, "market": r.market,
        "instrument_id": r.instrument_id, "trade_date": r.trade_date,
        "source_time": r.source_time, "canonical_time": r.canonical_time,
        "raw_price": r.raw_price, "raw_volume_value": r.raw_volume_value,
        "buy_sell_raw": r.buy_sell_raw, "source_record": r.source_record,
    }


# ===========================================================================
# Live status（MOD12：分维度）
# ===========================================================================
def derive_live_status(observations: list[dict], corporate_cases: list[dict]) -> dict:
    def dimension(field_filter, status_key):
        eligible = [o for o in observations if field_filter(o)]
        complete = [o for o in eligible
                    if o.get("full_day_status") == "COMPLETE"
                    and o.get("extraction_status") in (
                        "FOUND", "MULTIPLE_0925", "NONCANONICAL_0925_TIME", "MISSING_0925")]
        if not eligible:
            return "INSUFFICIENT"
        if len(complete) == len(eligible):
            return "COMPLETE"
        if len(complete) > 0:
            return "PARTIAL"
        return "INSUFFICIENT"

    auction_src = dimension(lambda o: True, "extraction_status")
    price_open = dimension(
        lambda o: o.get("extraction_status") == "FOUND", "lane_a")
    vol_unit = dimension(
        lambda o: o.get("volume_evidence", {}).get("daily_volume_ratio") is not None,
        "volume_evidence")

    corp = "INSUFFICIENT"
    if corporate_cases:
        resolved = [c for c in corporate_cases if c.get("status") == "RESOLVED"]
        corp = "COMPLETE" if len(resolved) == len(corporate_cases) else (
            "PARTIAL" if resolved else "INSUFFICIENT")

    overall = "COMPLETE" if all(
        x == "COMPLETE" for x in [auction_src, price_open, vol_unit, corp]) else (
        "PARTIAL" if any(x in ("COMPLETE", "PARTIAL") for x in
                         [auction_src, price_open, vol_unit, corp]) else "INSUFFICIENT")

    return {
        "LIVE_RUN_STATUS": "COMPLETED",
        "EVIDENCE_COMPLETENESS": overall,
        "auction_source_evidence": auction_src,
        "price_open_evidence": price_open,
        "pit_gap_evidence": "PARTIAL",  # corporate-only
        "volume_unit_evidence": vol_unit,
        "corporate_action_evidence": corp,
    }


# ===========================================================================
# MOD8 / MOD11 — 聚合 volume distribution + pagination data quality
# ===========================================================================
def _quantile(values, q):
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 6)
    qv = statistics.quantiles(values, n=100, method="inclusive")
    return round(qv[max(0, min(99, int(q * 100) - 1))], 6)


def compute_volume_unit_distribution(observations: list[dict]) -> dict:
    eligible = [o for o in observations
                if o.get("full_day_status") == "COMPLETE"
                and o.get("volume_evidence", {}).get("daily_volume_ratio") is not None]
    out: dict[str, Any] = {}
    by_board: dict[str, list] = {}
    for o in eligible:
        r = o["volume_evidence"]["daily_volume_ratio"]
        by_board.setdefault(o["board"], []).append(r)

    def stats(vals):
        if not vals:
            return {"sample_count": 0, "median": None, "p10": None, "p25": None,
                    "p75": None, "p90": None, "min": None, "max": None}
        return {
            "sample_count": len(vals),
            "median": round(statistics.median(vals), 6),
            "p10": _quantile(vals, 0.10), "p25": _quantile(vals, 0.25),
            "p75": _quantile(vals, 0.75), "p90": _quantile(vals, 0.90),
            "min": round(min(vals), 6), "max": round(max(vals), 6),
        }

    out["ALL"] = stats([r for v in by_board.values() for r in v])
    for b in ["SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR"]:
        if b in by_board:
            out[b] = stats(by_board[b])
    return out


def compute_data_quality_summary(observations: list[dict]) -> dict:
    total = len(observations)
    pag = {"COMPLETE": 0, "EMPTY": 0, "SOURCE_ERROR": 0,
           "PAGINATION_STALLED": 0, "PAGINATION_LIMIT_REACHED": 0}
    for o in observations:
        st = o.get("full_day_status")
        if st in pag:
            pag[st] += 1
    auction_eligible = sum(1 for o in observations
                           if o.get("full_day_status") == "COMPLETE")
    vol_eligible = sum(1 for o in observations
                       if o.get("full_day_status") == "COMPLETE"
                       and o.get("volume_evidence", {}).get("daily_volume_ratio") is not None)
    return {
        "total_source_days_attempted": total,
        "pagination": pag,
        "auction_semantics_eligible_days": auction_eligible,
        "volume_unit_eligible_days": vol_eligible,
        "denominator_note": "pagination COMPLETE only enters auction semantics denominator; "
                             "pagination failure is SOURCE DAY INCOMPLETE, not MISSING_0925",
    }


# ===========================================================================
# Writer（MOD6 / MOD13）
# ===========================================================================
def write_evidence_outputs(output_dir, as_of, observations, corporate_cases,
                           live_status, data_quality, volume_dist,
                           pagination_rows, routine_count, corporate_count,
                           corporate_lookback_days):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    a = as_of.isoformat()

    # 00 manifest
    _write_json(output_dir / "00_manifest.json", {
        "as_of": a, "routine_count": routine_count,
        "corporate_count": corporate_count,
        "total_observations": len(observations),
        "corporate_lookback_days": corporate_lookback_days,
        "generated_at": datetime.now().isoformat(),
    })

    # 01 observations
    _write_json(output_dir / "01_observations.json", observations)

    # 02 corporate
    _write_csv(output_dir / "02_corporate_action_cases.csv", corporate_cases, [
        "symbol", "market", "instrument_id", "board", "status",
        "event_date", "prev_trade_date", "next_trade_date",
        "factor_before", "factor_after"])

    # 03 raw canonical auction records（仅 canonical 09:25）
    raw_rows = []
    for o in observations:
        for r in o.get("raw_records", []):
            raw_rows.append(r)
    _write_jsonl(output_dir / "03_raw_transaction_records.jsonl", raw_rows)

    # 04 noncanonical 09:25:xx records
    nc_rows = []
    for o in observations:
        for r in o.get("noncanonical_records", []):
            nc_rows.append(r)
    _write_jsonl(output_dir / "04_noncanonical_time_records.jsonl", nc_rows)

    # 05 source day pagination
    _write_csv(output_dir / "05_source_day_pagination.csv", pagination_rows, [
        "symbol", "market", "instrument_id", "trade_date",
        "pagination_status", "page_count", "record_count", "page_size",
        "source_first_time", "source_last_time",
        "sum_transaction_raw_vol", "valid_volume_record_count",
        "invalid_volume_record_count", "mdas_daily_volume",
        "daily_volume_ratio", "source_error_code", "source_error_message"])

    # 06 extraction status
    _write_csv(output_dir / "06_extraction_status.csv",
               [{"symbol": o["symbol"], "market": o["market"],
                 "trade_date": o["trade_date"],
                 "full_day_status": o.get("full_day_status"),
                 "extraction_status": o.get("extraction_status")}
                for o in observations],
               ["symbol", "market", "trade_date", "full_day_status", "extraction_status"])

    # 07 corporate cases（alias of 02, kept for compatibility）
    _write_csv(output_dir / "07_corporate_action_cases.csv", corporate_cases, [
        "symbol", "market", "instrument_id", "board", "status",
        "event_date", "prev_trade_date", "next_trade_date",
        "factor_before", "factor_after"])

    # 08 volume unit evidence
    vol_rows = []
    for o in observations:
        ve = o.get("volume_evidence", {})
        vol_rows.append({
            "symbol": o["symbol"], "market": o["market"], "board": o["board"],
            "trade_date": o["trade_date"],
            "transaction_record_count": ve.get("transaction_record_count"),
            "valid_volume_record_count": ve.get("valid_volume_record_count"),
            "sum_transaction_raw_vol": ve.get("sum_transaction_raw_vol"),
            "mdas_daily_volume": ve.get("mdas_daily_volume"),
            "daily_volume_ratio": ve.get("daily_volume_ratio"),
            "pagination_status": ve.get("pagination_status"),
            "page_count": ve.get("page_count"),
            "mdas_data_source": ve.get("mdas_data_source"),
            "mdas_degraded": ve.get("mdas_degraded"),
            "mdas_degraded_reason": ve.get("mdas_degraded_reason"),
            "evidence_reason": ve.get("evidence_reason"),
        })
    _write_csv(output_dir / "08_volume_unit_evidence.csv", vol_rows, [
        "symbol", "market", "board", "trade_date",
        "transaction_record_count", "valid_volume_record_count",
        "sum_transaction_raw_vol", "mdas_daily_volume", "daily_volume_ratio",
        "pagination_status", "page_count", "mdas_data_source",
        "mdas_degraded", "mdas_degraded_reason", "evidence_reason"])

    # 09 volume unit distribution
    _write_json(output_dir / "09_volume_unit_distribution.json", volume_dist)

    # 10 data quality summary
    _write_json(output_dir / "10_data_quality_summary.json", data_quality)

    # 11 validation summary
    _write_json(output_dir / "11_validation_summary.json", {
        "live_status": live_status,
        "data_quality": data_quality,
        "volume_unit_distribution": volume_dist,
        "amount_evidence": {
            "DIRECT_RAW_AMOUNT": "UNAVAILABLE",
            "DERIVED_AMOUNT": "PENDING_VOLUME_UNIT_CONFIRMATION",
        },
        "runner_conclusion": "NO_CONFIRMATION_IN_RUNNER",
    })


def _write_json(path, obj):
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


def _write_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def _write_csv(path, rows, columns):
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c) for c in columns})


# ===========================================================================
# Top-level runner（仅本地 dry / offline；禁止 --live）
# ===========================================================================
async def run_validation(
    session, mdas, adapter, as_of: date,
    corporate_lookback_days: int = CORPORATE_LOOKBACK_DAYS,
    trade_date: Optional[date] = None,
    adj_service: Optional[AdjustmentFactorService] = None,
    output_dir=None,
):
    """MOD9：adj_service 显式依赖；None → 真实 AdjustmentFactorService() instance。

    MOD6：routine 跑最近 10 个正式交易日（每个 inst × 10 dates）。
    MOD2：corporate 仅在各自真实 event_T 运行一次。
    """
    if adj_service is None:
        adj_service = AdjustmentFactorService()

    samples = load_sample()
    resolved, _skipped = await resolve_sample_instruments(session, samples)

    resolved_routine = [x for x in resolved if x.cohort == "routine"]
    resolved_corporate = [x for x in resolved if x.cohort == "corporate"]

    # MOD6：最近 10 个正式交易日（official calendar）
    routine_dates = await previous_trading_dates(session, as_of, 10)

    observations = []
    pagination_rows = []
    for inst in resolved_routine:
        for T in routine_dates:
            obs = await run_single_observation(mdas, adapter, session, inst, T)
            observations.append(obs)
            pagination_rows.append(_pagination_row(inst, T, obs))

    corporate_cases = await resolve_corporate_cases(
        adj_service, session, resolved_corporate,
        as_of, corporate_lookback_days)
    for inst in resolved_corporate:
        cc = next((c for c in corporate_cases
                   if c["instrument_id"] == str(inst.instrument_id)), None)
        if cc is None or cc.get("event_date") is None:
            continue
        # MOD2：corporate observation 必须在 event_T 上运行，不得用 as_of / routine T
        event_T = date.fromisoformat(cc["event_date"])
        prev_d = date.fromisoformat(cc["prev_trade_date"]) if cc.get("prev_trade_date") else None
        next_d = date.fromisoformat(cc["next_trade_date"]) if cc.get("next_trade_date") else None
        cobs = await run_corporate_observation(
            mdas, adapter, session, inst, event_T, prev_d, next_d,
            factor_before=cc.get("factor_before"),
            factor_after=cc.get("factor_after"))
        assert cobs["trade_date"] == cc["event_date"], (
            f"corporate observation trade_date {cobs['trade_date']} "
            f"!= event_date {cc['event_date']}")
        observations.append(cobs)
        pagination_rows.append(_pagination_row(inst, event_T, cobs))

    live_status = derive_live_status(observations, corporate_cases)
    data_quality = compute_data_quality_summary(observations)
    volume_dist = compute_volume_unit_distribution(observations)

    out_dir = Path(output_dir) if output_dir else (OUTPUT_DIR / as_of.isoformat())
    write_evidence_outputs(
        out_dir, as_of, observations, corporate_cases,
        live_status, data_quality, volume_dist, pagination_rows,
        len(resolved_routine), len(resolved_corporate), corporate_lookback_days)
    return {
        "observations": observations, "corporate_cases": corporate_cases,
        "live_status": live_status, "data_quality": data_quality,
        "volume_dist": volume_dist,
    }


def _pagination_row(inst, trade_date, obs) -> dict:
    ve = obs.get("volume_evidence", {})
    fd = obs.get("full_day_status")
    return {
        "symbol": inst.symbol, "market": inst.market,
        "instrument_id": str(inst.instrument_id),
        "trade_date": trade_date.isoformat(),
        "pagination_status": fd,
        "page_count": ve.get("page_count"),
        "record_count": ve.get("transaction_record_count"),
        "page_size": PAGE_SIZE,
        "source_first_time": None, "source_last_time": None,
        "sum_transaction_raw_vol": ve.get("sum_transaction_raw_vol"),
        "valid_volume_record_count": ve.get("valid_volume_record_count"),
        "invalid_volume_record_count": ve.get("invalid_volume_record_count"),
        "mdas_daily_volume": ve.get("mdas_daily_volume"),
        "daily_volume_ratio": ve.get("daily_volume_ratio"),
        "source_error_code": None, "source_error_message": None,
    }


# pandas 延迟 import（MDAS 返回 DataFrame）
try:
    import pandas as pd  # noqa: F401
except Exception:  # noqa: BLE001
    pd = None  # type: ignore
