"""Panji research-evaluation read API (not real-time strategy inference)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from app.services.access_control_service import AccessContext, require_capability
from app.services.strategy_research_read_service import (
    SnapshotInvalid, SnapshotUnavailable, evaluate, read_snapshot,
)

router = APIRouter(prefix="/v1/strategy-research", tags=["strategy-research"])


def _market_access(_: AccessContext = Depends(require_capability("strategy_research"))) -> None:
    return None


@router.get("/zigzag/evaluation")
def zigzag_evaluation(
    side: str = Query("BUY", pattern="^(BUY|SELL)$"),
    symbol: str | None = Query(None, min_length=3, max_length=32, pattern=r"^[A-Za-z0-9:_]+$"),
    gain_pct: float = Query(5, gt=0, le=100),
    risk_pct: float = Query(5, gt=0, le=100),
    _: None = Depends(_market_access),
) -> dict:
    try:
        snap = read_snapshot()
        if snap["metadata"]["strategy_key"] != "zigzag_two_stage":
            raise SnapshotInvalid("unsupported strategy")
        return evaluate(snap, side, symbol, gain_pct, risk_pct)
    except SnapshotUnavailable as exc:
        # A missing research snapshot is not an empty evaluation result.
        # Match candidate snapshot availability semantics and the PRD fail-closed contract.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SnapshotInvalid as exc:
        raise HTTPException(status_code=503, detail="研究快照未通过完整性检查，已禁止展示") from exc
