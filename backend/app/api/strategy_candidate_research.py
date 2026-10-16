"""Strategy read endpoints — per-request entitlement, no background inference."""
from __future__ import annotations
import math
import re
import unicodedata
import pandas as pd
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.deps import get_db
from app.models.instrument import Instrument
from app.services.instrument_maintenance_service import stock_symbol_sql_filter
from app.services.market_data_aggregation_service import MarketDataAggregationService, MarketDataSourcePolicy
from app.services.calendar_service import get_most_recent_trading_day_async
from app.services.access_control_service import AccessContext, require_capability
from app.services.strategy_candidate_snapshot import DatasetInvalid, DatasetUnavailable, get_stock, get_today, read_dataset
from app.services.strategy_structure_view import StructureUnavailable, structure_from_verified_bars

router = APIRouter(prefix="/v1/strategy-research", tags=["strategy-research-candidates"])


def _visible(_: AccessContext = Depends(require_capability("strategy_research"))) -> None:
    return None


def _dataset() -> dict:
    try:
        return read_dataset()
    except DatasetUnavailable as ex:
        raise HTTPException(status_code=503, detail=str(ex)) from ex
    except DatasetInvalid as ex:
        raise HTTPException(status_code=503, detail="候选快照审计未通过，已禁止展示") from ex


@router.get("/catalog")
def catalog(_: None = Depends(_visible)) -> dict:
    d = _dataset()
    return {"metadata": d["metadata"], "stock_count": len(d["stocks"]), "batch_count": len(d["daily_batches"])}


async def _expected_latest_trade_day(db: AsyncSession, now: datetime | None = None) -> str:
    """PIT: before 15:30 Shanghai time, today's daily OHLC is not complete.

    Use the existing authoritative A-share trading-calendar table; if missing,
    fail closed instead of taking the latest available (possibly stale) batch.
    """
    local = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai"))
    calendar_cutoff = local.date() if local.time() >= time(15, 30) else local.date() - timedelta(days=1)
    trading = await get_most_recent_trading_day_async(db, calendar_cutoff)
    if trading is None:
        raise HTTPException(status_code=503, detail="缺少可信 A 股交易日历，无法判断今日预测是否新鲜")
    return trading.isoformat()


@router.get("/daily")
async def daily(
    trade_date: str | None = Query(None),
    _: None = Depends(_visible),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Explicit trade_date is historical replay; unspecified is the latest
    # complete A-share trade day, NOT max(snapshot.daily_batches).
    expected = trade_date if trade_date is not None else await _expected_latest_trade_day(db)
    dataset = _dataset()
    try:
        result = get_today(dataset, expected)
        return {**result, "freshness": "HISTORICAL" if trade_date is not None else "CURRENT", "expected_trade_date": expected}
    except DatasetUnavailable as ex:
        latest = max((b["trade_date"] for b in dataset["daily_batches"]), default="无")
        if trade_date is None:
            raise HTTPException(status_code=503, detail=f"{expected} 盘后预测未发布（最近发布：{latest}）；禁止使用旧批次冒充今日机会") from ex
        raise HTTPException(status_code=404, detail=str(ex)) from ex
    except DatasetInvalid as ex:
        raise HTTPException(status_code=422, detail=str(ex)) from ex



def _strategy_full_symbol(market: str, raw_symbol: str) -> str:
    """Convert the instrument SSOT (market + bare six digits) to snapshot ID."""
    market = str(market).strip().upper()
    bare = str(raw_symbol).strip()
    if market not in {"SH", "SZ", "BJ"} or len(bare) != 6 or any(
        digit not in "0123456789" for digit in bare
    ):
        raise ValueError("INVALID_STRATEGY_INSTRUMENT_ID")
    return f"{market}:{bare}"


@router.get("/stocks/search")
async def strategy_stock_search(
    keyword: str = Query(..., min_length=1, max_length=50),
    _: None = Depends(_visible),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Instrument discovery scoped to strategy_research; no market_data grant."""
    key = unicodedata.normalize("NFKC", keyword).strip()
    empty = {"items": [], "scope": "STRATEGY_RESEARCH", "available_count": 0}
    if not key:
        return empty

    low = key.lower()
    if ":" in key:
        # Snapshot IDs are prefixed; instruments.symbol is unprefixed in DB.
        parts = key.upper().split(":")
        if (
            len(parts) != 2 or parts[0] not in {"SH", "SZ", "BJ"}
            or not 1 <= len(parts[1]) <= 6
            or any(ch not in "0123456789" for ch in parts[1])
        ):
            return empty
        market, bare_prefix = parts
        match_code = (Instrument.symbol == bare_prefix) if len(bare_prefix) == 6 else (
            Instrument.symbol.ilike(f"{bare_prefix}%")
        )
        hit = (Instrument.market == market) & match_code
        rank = case((Instrument.symbol == bare_prefix, 0), else_=1)
    else:
        hit = or_(
            Instrument.symbol.ilike(f"%{key}%"),
            Instrument.name.ilike(f"%{key}%"),
            Instrument.pinyin_initials.ilike(f"{low}%"),
        )
        rank = case(
            (Instrument.symbol == key, 0),
            (Instrument.symbol.ilike(f"{key}%"), 1),
            (Instrument.pinyin_initials.ilike(f"{low}%"), 2),
            (Instrument.name.ilike(f"%{key}%"), 3),
            else_=4,
        )
    stmt = (
        select(Instrument)
        .where(stock_symbol_sql_filter(Instrument), hit)
        .order_by(rank, Instrument.symbol)
        .limit(12)
    )
    result = await db.execute(stmt)
    allowed = set(_dataset()["stocks"])
    items = []
    for row in result.scalars().all():
        try:
            symbol = _strategy_full_symbol(row.market, row.symbol)
        except ValueError as exc:
            # A corrupted instrument identity cannot be surfaced as a tradeable stock.
            raise HTTPException(status_code=503, detail="股票主数据标识不完整，拒绝返回") from exc
        items.append({
            "symbol": symbol,
            "name": row.name,
            "in_snapshot": symbol in allowed,
        })
    return {
        "items": items,
        "scope": "STRATEGY_RESEARCH",
        "available_count": sum(bool(row["in_snapshot"]) for row in items),
    }


# V2 historical chart source. This is independent from model prediction snapshots.
# No factor recalculation or provider fallback is allowed in this read path.
@router.get('/stocks/{symbol}/bars')
async def strategy_history_bars(
    symbol: str,
    limit: int = Query(500, ge=85, le=800),
    _: None = Depends(_visible),
    db: AsyncSession = Depends(get_db),
    include_structure: bool = Query(False),
) -> dict:
    if not re.fullmatch(r'(SH|SZ|BJ):[0-9]{6}', symbol):
        raise HTTPException(status_code=422, detail='invalid strategy stock symbol')
    market, bare = symbol.split(':')
    found = await db.execute(select(Instrument).where(
        Instrument.symbol == bare, Instrument.market == market,
        stock_symbol_sql_filter(Instrument),
    ))
    instrument = found.scalar_one_or_none()
    if instrument is None:
        raise HTTPException(status_code=404, detail='A股主数据中没有此股票')

    # Canonical snapshot is deliberately NOT used as a long-term price database.
    # Historical requests are completed 1d/QFQ/DB_ONLY, with a PIT as-of date.
    as_of = date.fromisoformat(await _expected_latest_trade_day(db))
    service = MarketDataAggregationService()
    stock = await service.get_bars(
        db, instrument.id, timeframe='1d', adj='qfq',
        include_realtime=False, completed_only=True,
        adjustment_as_of=as_of, end_date=as_of,
        start_date=as_of - timedelta(days=1600), limit=limit,
        source_policy=MarketDataSourcePolicy.DB_ONLY,
        allow_backfill=False,
    )
    frame = stock.bars
    if stock.degraded or not stock.adj_factor_hash or frame is None or frame.empty:
        raise HTTPException(status_code=503, detail='权威QFQ行情或复权因子未就绪，不允许回退模拟数据')
    if not {'open','high','low','close'}.issubset(frame.columns):
        raise HTTPException(status_code=503, detail='权威K线字段不完整')
    index = pd.DatetimeIndex(frame.index)
    if index.has_duplicates or not index.is_monotonic_increasing or index[-1].date() > as_of:
        raise HTTPException(status_code=503, detail='权威行情日期序列不合法')
    records = []
    for when, row in frame.iterrows():
        values = [float(row[k]) for k in ('open','high','low','close')]
        if not all(math.isfinite(x) and x > 0 for x in values):
            raise HTTPException(status_code=503, detail='权威行情包含非法价格')
        op, hi, lo, close = values
        if hi < max(op, lo, close) or lo > min(op, hi, close):
            raise HTTPException(status_code=503, detail='权威行情OHLC关系不合法')
        records.append({'date':pd.Timestamp(when).strftime('%Y-%m-%d'),
                        'open':op,'high':hi,'low':lo,'close':close})
    if len(records) > limit:
        raise HTTPException(status_code=503, detail='权威行情数量超过请求上限')
    response = {
        'symbol':symbol,'bars':records,'count':len(records),
        'requested_count':limit,'actual_start':records[0]['date'],
        'actual_end':records[-1]['date'],
        'as_of':as_of.isoformat(),
        'price_adjustment':'qfq',
        'source_policy':'MDAS_DB_ONLY',
        'source_bar_hash':stock.source_bar_hash,
        'adj_factor_hash':stock.adj_factor_hash,
        'history_exhausted':bool(stock.history_exhausted),
        # Explicitly independent from the OLD diagnostic model price contract.
        'candidate_overlay_supported':False,
        'candidate_overlay_reason':'历史模型快照为独立版本；尚未完成与此QFQ序列逐日价格同源核验',
    }
    if include_structure is True:
        try:
            response['structure'] = structure_from_verified_bars(response)
        except StructureUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return response


@router.get("/stocks/{symbol}/candidates")
def candidates(
    symbol: str,
    release_id: str | None = Query(None, min_length=1, max_length=128),
    _: None = Depends(_visible),
) -> dict:
    dataset = _dataset()
    if release_id is not None and dataset["metadata"]["release_id"] != release_id:
        raise HTTPException(status_code=409, detail="模型发布版本不匹配，拒绝将旧候选映射到其他版本")
    try:
        return get_stock(dataset, symbol)
    except DatasetUnavailable as ex:
        raise HTTPException(status_code=404, detail=str(ex)) from ex
    except DatasetInvalid as ex:
        raise HTTPException(status_code=422, detail=str(ex)) from ex
