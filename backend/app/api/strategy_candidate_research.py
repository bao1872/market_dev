"""Strategy read endpoints — per-request entitlement, no background inference."""
from __future__ import annotations
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.deps import get_db
from app.services.calendar_service import get_most_recent_trading_day_async
from app.services.access_control_service import AccessContext, require_capability
from app.services.strategy_candidate_snapshot import DatasetInvalid, DatasetUnavailable, get_stock, get_today, read_dataset

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
