"""Panji research-evaluation read API (not real-time strategy inference)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.services.access_control_service import AccessContext, require_capability
from app.services.strategy_preview import select_preview
from app.services.strategy_research_read_service import (
    SnapshotInvalid,
    SnapshotUnavailable,
    evaluate,
    read_snapshot,
)

router = APIRouter(dependencies=[Depends(select_preview)], prefix="/v1/strategy-research", tags=["strategy-research"])


def _market_access(_: AccessContext = Depends(require_capability("strategy_research"))) -> None:
    return None


@router.get("/zigzag/evaluation")
def zigzag_evaluation(
    side: str = Query("BUY", pattern="^(BUY|SELL)$"),
    symbol: str | None = Query(None, min_length=3, max_length=32, pattern=r"^[A-Za-z0-9:_]+$"),
    gain_pct: float = Query(5, gt=0, le=100),
    risk_pct: float = Query(5, gt=0, le=100),
    period: str = Query("ALL", pattern="^(ALL|TRAIN|VAL|TEST|POST_TEST|UNASSIGNED)$"),
    family: str = Query("JOINT", pattern="^(JOINT|BREAKOUT)$"),
    trend: str | None = Query(None, pattern="^(向上|向下|待确认)$"),
    release_id: str | None = Query(None, min_length=1, max_length=128),
    _: None = Depends(_market_access),
) -> dict:
    try:
        snap = read_snapshot()
        if snap["metadata"]["strategy_key"] != "zigzag_two_stage":
            raise SnapshotInvalid("unsupported strategy")
        if release_id and snap["metadata"].get("release_id") != release_id:
            raise HTTPException(status_code=409, detail="评价与候选模型版本不一致")
        return evaluate(snap, side, symbol, gain_pct, risk_pct, family=family, trend=trend, period=period)
    except SnapshotUnavailable as exc:
        # A missing research snapshot is not an empty evaluation result.
        # Match candidate snapshot availability semantics and the PRD fail-closed contract.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SnapshotInvalid as exc:
        raise HTTPException(status_code=503, detail="研究快照未通过完整性检查，已禁止展示") from exc


@router.get("/zigzag/task-performance")
def task_performance(
    symbol: str | None = Query(None, pattern=r"^(SH|SZ|BJ):[0-9]{6}$"),
    period: str = Query("TEST", pattern="^(TRAIN|VAL|TEST|POST_TEST|UNASSIGNED|ALL)$"),
    release_id: str | None = Query(None, min_length=1, max_length=128),
    _: None = Depends(_market_access),
) -> dict:
    from app.services.strategy_task_performance import evaluate_tasks
    try:
        snapshot = read_snapshot()
        if snapshot["metadata"]["strategy_key"] != "zigzag_two_stage":
            raise SnapshotInvalid("unsupported strategy")
        return evaluate_tasks(snapshot, symbol=symbol, period=period, release_id=release_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SnapshotUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SnapshotInvalid as exc:
        raise HTTPException(status_code=503, detail="任务评价快照未通过完整性检查") from exc
