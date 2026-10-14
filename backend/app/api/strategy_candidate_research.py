"""Strategy read endpoints — per-request entitlement, no background inference."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Query
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


@router.get("/daily")
def daily(trade_date: str | None = Query(None), _: None = Depends(_visible)) -> dict:
    try:
        return get_today(_dataset(), trade_date)
    except DatasetUnavailable as ex:
        raise HTTPException(status_code=404, detail=str(ex)) from ex
    except DatasetInvalid as ex:
        raise HTTPException(status_code=422, detail=str(ex)) from ex


@router.get("/stocks/{symbol}/candidates")
def candidates(symbol: str, _: None = Depends(_visible)) -> dict:
    try:
        return get_stock(_dataset(), symbol)
    except DatasetUnavailable as ex:
        raise HTTPException(status_code=404, detail=str(ex)) from ex
    except DatasetInvalid as ex:
        raise HTTPException(status_code=422, detail=str(ex)) from ex
