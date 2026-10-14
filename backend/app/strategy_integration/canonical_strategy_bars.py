"""Read-only bridge to Panji's existing *single-owner* MDAS qfq pipeline.

This is for a DB-backed, explicitly authorized off-line export, not an HTTP
handler. It never calculates an adjustment factor or calls external providers.
"""
from __future__ import annotations
from collections import Counter
from datetime import date
from typing import Any, Iterable

import numpy as np
import pandas as pd


class CanonicalMarketDataError(ValueError):
    pass


async def load_official_qfq_stock_batch(
    session: Any,
    instrument_by_id: dict[Any, str],
    as_of: date,
    *,
    start_date: date = date(2012, 1, 1),
    batch_size: int = 80,
    min_bars: int = 150,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """Return schema-ready canonical OHLCV with provenance and exclusion reasons.

    IMPORTANT: use before generating ML features; never qfq-adjust an already
    qfq-adjusted bar. No stock is excluded merely for a valid corporate action.
    """
    from app.services.market_data_aggregation_service import (
        MarketDataAggregationService, MarketDataSourcePolicy,
    )
    if start_date > as_of or batch_size < 1 or min_bars < 1:
        raise CanonicalMarketDataError("BAD_CANONICAL_READ_PARAMETERS")
    ids = sorted(instrument_by_id, key=lambda obj: str(instrument_by_id[obj]))
    service = MarketDataAggregationService()
    accepted: dict[str, pd.DataFrame] = {}
    hashes: dict[str, dict] = {}
    excluded: Counter[str] = Counter()
    covered = 0
    for off in range(0, len(ids), batch_size):
        chunk = ids[off:off+batch_size]
        result = await service.get_bars_batch(
            session, chunk,
            timeframe="1d",
            adj="qfq",
            completed_only=True,
            source_policy=MarketDataSourcePolicy.DB_ONLY,
            adjustment_as_of=as_of,
            start_date=start_date,
            end_date=as_of,
            allow_backfill=False,
            limit=None,
        )
        for iid in chunk:
            symbol = instrument_by_id[iid]
            stock = result.get(iid)
            if isinstance(stock, Exception) or stock is None:
                excluded["mdas_error"] += 1
                continue
            bars = stock.bars
            if bars is None or bars.empty or len(bars) < min_bars:
                excluded["too_short_or_missing"] += 1
                continue
            # In MDAS qfq mode, a missing factor can yield unchanged raw bars;
            # a missing provenance hash MUST NOT be treated as valid qfq.
            if stock.degraded or not stock.adj_factor_hash:
                excluded["missing_or_degraded_factor"] += 1
                continue
            dates = pd.to_datetime(bars.index)
            if dates.has_duplicates or not dates.is_monotonic_increasing:
                excluded["duplicate_or_unsorted_dates"] += 1
                continue
            if dates[-1].date() > as_of:
                excluded["future_bar"] += 1
                continue
            arr = bars[["open","high","low","close","volume"]].apply(
                pd.to_numeric, errors="coerce",
            ).to_numpy(dtype=float)
            if not np.isfinite(arr).all() or not (arr[:,:4] > 0).all() or (arr[:,4] < 0).any():
                excluded["invalid_ohlcv"] += 1
                continue
            if (arr[:,1] < np.maximum.reduce([arr[:,0],arr[:,2],arr[:,3]])).any() or (
                arr[:,2] > np.minimum.reduce([arr[:,0],arr[:,1],arr[:,3]])
            ).any():
                excluded["invalid_candle"] += 1
                continue
            # Only one market-data owner: do not locally re-adjust prices.
            raw = pd.DataFrame(arr, columns=["open","high","low","close","volume"])
            raw.insert(0,"datetime",dates.to_numpy())
            accepted[symbol] = raw
            hashes[symbol] = {
                "source_bar_hash": stock.source_bar_hash,
                "adj_factor_hash": stock.adj_factor_hash,
                "adjustment_as_of": as_of.isoformat(),
                "market_data_contract_version": stock.market_data_contract_version,
                "data_source": stock.data_source,
            }
            if dates[-1].date() == as_of:
                covered += 1
    return accepted, {
        "price_adjustment": "qfq",
        "adjustment_as_of": as_of.isoformat(),
        "source_policy": "DB_ONLY",
        "completed_only": True,
        "source_owner": "MarketDataAggregationService.get_bars_batch",
        "total_instruments": len(ids),
        "eligible_stock_count": len(accepted),
        "covered_asof_count": covered,
        "coverage_ratio_of_eligible": round(covered/max(1,len(accepted)),6),
        "coverage_ratio_of_total": round(covered/max(1,len(ids)),6),
        "excluded": dict(excluded),
        "provenance": hashes,
        "external_fetch_allowed": False,
        "db_write_allowed": False,
    }
