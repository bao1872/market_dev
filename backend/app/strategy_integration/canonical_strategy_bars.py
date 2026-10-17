"""Read-only bridge to Panji's existing *single-owner* MDAS qfq pipeline.

This is for a DB-backed, explicitly authorized off-line export, not an HTTP
handler. It never calculates an adjustment factor or calls external providers.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

import numpy as np
import pandas as pd


class CanonicalMarketDataError(ValueError):
    pass


async def load_factor_series_for_pit(
    session: Any, instrument_id: Any, as_of: date
) -> pd.DataFrame | None:
    """Authoritative factor series for one instrument, as of `as_of`.

    MDAS only exposes a factor *hash*, so point-in-time feature rebuilding needs
    the series itself. It is fetched through the single factor owner
    (AdjustmentFactorService) and is read-only.
    """
    from app.services.adjustment_factor_service import AdjustmentFactorService
    try:
        series = await AdjustmentFactorService().get_factor_series(
            session, instrument_id, as_of=as_of)
    except Exception:
        return None
    if series is None or series.empty:
        return None
    if not {'trade_date', 'adj_factor'}.issubset(series.columns):
        return None
    return series


def _align_factor_series(frame_dates: pd.DatetimeIndex, series: pd.DataFrame) -> np.ndarray:
    """Attach only factors effective on or before each daily bar (fail closed).

    Never infer an early factor by backward-filling a later corporate-action
    factor. Missing initial coverage is an unavailable PIT input, not a reason
    to synthesize historical factors.
    """
    bars = pd.DatetimeIndex(frame_dates)
    if bars.empty or bars.has_duplicates or not bars.is_monotonic_increasing or bars.hasnans:
        raise CanonicalMarketDataError('INVALID_PIT_BAR_DATES')
    if bars.tz is not None:
        raise CanonicalMarketDataError('PIT_BAR_TIMEZONE_UNSUPPORTED')
    if not {'trade_date', 'adj_factor'}.issubset(series.columns) or series.empty:
        raise CanonicalMarketDataError('INVALID_PIT_FACTOR_SERIES')
    dates = pd.to_datetime(series['trade_date'], errors='coerce')
    factors = pd.to_numeric(series['adj_factor'], errors='coerce')
    if (dates.isna().any() or dates.duplicated().any() or
            getattr(dates.dt, 'tz', None) is not None or
            not np.isfinite(factors.to_numpy(dtype=float)).all() or
            (factors <= 0).any()):
        raise CanonicalMarketDataError('INVALID_PIT_FACTOR_SERIES')
    order = np.argsort(dates.to_numpy())
    dates = dates.iloc[order].reset_index(drop=True)
    factors = factors.iloc[order].reset_index(drop=True)
    merged = pd.merge_asof(
        pd.DataFrame({'bar_date': bars.normalize().as_unit('ns')}),
        pd.DataFrame({'factor_date': dates.dt.normalize().dt.as_unit('ns'), 'adj_factor': factors}),
        left_on='bar_date', right_on='factor_date', direction='backward',
    )
    values = merged['adj_factor'].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise CanonicalMarketDataError('PIT_FACTOR_INITIAL_COVERAGE_MISSING')
    return values


async def load_official_qfq_stock_batch(
    session: Any,
    instrument_by_id: dict[Any, str],
    as_of: date,
    *,
    start_date: date = date(2012, 1, 1),
    batch_size: int = 80,
    min_bars: int = 150,
    with_factor_series: bool = True,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """Return schema-ready canonical OHLCV with provenance and exclusion reasons.

    IMPORTANT: use before generating ML features; never qfq-adjust an already
    qfq-adjusted bar. No stock is excluded merely for a valid corporate action.
    """
    from app.services.market_data_aggregation_service import (
        MarketDataAggregationService,
        MarketDataSourcePolicy,
    )
    if start_date > as_of or batch_size < 1 or min_bars < 1:
        raise CanonicalMarketDataError("BAD_CANONICAL_READ_PARAMETERS")
    ids = sorted(instrument_by_id, key=lambda obj: str(instrument_by_id[obj]))
    service = MarketDataAggregationService()
    accepted: dict[str, pd.DataFrame] = {}
    hashes: dict[str, dict] = {}
    excluded: Counter[str] = Counter()
    excluded_symbols: dict[str, str] = {}
    stale_symbols: list[str] = []
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
        # Read both QFQ chart bars and MDAS-owned RAW bars. The latter are
        # required to reconstruct an historical QFQ frame exactly ONCE.
        raw_result = await service.get_bars_batch(
            session, chunk, timeframe='1d', adj='none', completed_only=True,
            source_policy=MarketDataSourcePolicy.DB_ONLY,
            start_date=start_date, end_date=as_of, allow_backfill=False, limit=None,
        )
        for iid in chunk:
            symbol = instrument_by_id[iid]
            stock = result.get(iid)
            if isinstance(stock, Exception) or stock is None:
                excluded["mdas_error"] += 1
                excluded_symbols[symbol] = "mdas_error"
                continue
            bars = stock.bars
            if bars is None or bars.empty or len(bars) < min_bars:
                excluded["too_short_or_missing"] += 1
                excluded_symbols[symbol] = "too_short_or_missing"
                continue
            # In MDAS qfq mode, a missing factor can yield unchanged raw bars;
            # a missing provenance hash MUST NOT be treated as valid qfq.
            if stock.degraded or not stock.adj_factor_hash:
                excluded["missing_or_degraded_factor"] += 1
                excluded_symbols[symbol] = "missing_or_degraded_factor"
                continue
            dates = pd.to_datetime(bars.index)
            if dates.has_duplicates or not dates.is_monotonic_increasing:
                excluded["duplicate_or_unsorted_dates"] += 1
                excluded_symbols[symbol] = "duplicate_or_unsorted_dates"
                continue
            if dates[-1].date() > as_of:
                excluded["future_bar"] += 1
                excluded_symbols[symbol] = "future_bar"
                continue
            arr = bars[["open","high","low","close","volume"]].apply(
                pd.to_numeric, errors="coerce",
            ).to_numpy(dtype=float)
            if not np.isfinite(arr).all() or not (arr[:,:4] > 0).all() or (arr[:,4] < 0).any():
                excluded["invalid_ohlcv"] += 1
                excluded_symbols[symbol] = "invalid_ohlcv"
                continue
            if (arr[:,1] < np.maximum.reduce([arr[:,0],arr[:,2],arr[:,3]])).any() or (
                arr[:,2] > np.minimum.reduce([arr[:,0],arr[:,1],arr[:,3]])
            ).any():
                excluded["invalid_candle"] += 1
                excluded_symbols[symbol] = "invalid_candle"
                continue
            native = raw_result.get(iid)
            if isinstance(native, Exception) or native is None or native.bars is None:
                excluded['missing_mdas_raw_bars'] += 1
                excluded_symbols[symbol] = 'missing_mdas_raw_bars'
                continue
            raw_bars = native.bars
            if (len(raw_bars) != len(bars) or not pd.DatetimeIndex(raw_bars.index).as_unit('ns').equals(dates.as_unit('ns'))):
                excluded['mdas_raw_qfq_date_mismatch'] += 1
                excluded_symbols[symbol] = 'mdas_raw_qfq_date_mismatch'
                continue
            native_values = raw_bars[['open','high','low','close','volume']].apply(
                pd.to_numeric, errors='coerce').to_numpy(dtype=float)
            if not np.isfinite(native_values).all() or (native_values[:, :4] <= 0).any() or (native_values[:,4] < 0).any():
                excluded['invalid_mdas_raw_ohlcv'] += 1
                excluded_symbols[symbol] = 'invalid_mdas_raw_ohlcv'
                continue
            # Only one market-data owner: current QFQ remains unchanged.
            raw = pd.DataFrame(arr, columns=['open','high','low','close','volume'])
            for ci, c in enumerate(('open','high','low','close','volume')):
                raw['raw_' + c] = native_values[:, ci]
            raw.insert(0,"datetime",dates.to_numpy())
            factor_series = None
            factor_note = 'not_requested'
            if with_factor_series:
                factor_series = await load_factor_series_for_pit(session, iid, as_of)
                if factor_series is None:
                    # Without the series, as-of PIT rebuilding is impossible; the
                    # frame is still valid qfq but must be flagged so callers can
                    # refuse to pretend PIT was applied.
                    factor_note = 'factor_series_unavailable'
                else:
                    try:
                        aligned = _align_factor_series(dates, factor_series)
                    except CanonicalMarketDataError:
                        # Exclude this instrument: never turn a gap into a
                        # silently backward-filled, look-ahead PIT series.
                        factor_series = None
                        factor_note = 'factor_series_invalid'
                    else:
                        raw['adj_factor'] = aligned
                        factor_note = 'authoritative_factor_series_attached'
            if with_factor_series and factor_series is None:
                excluded['missing_authoritative_factor_series'] += 1
                excluded_symbols[symbol] = 'missing_authoritative_factor_series'
                continue
            accepted[symbol] = raw
            hashes[symbol] = {
                "source_bar_hash": stock.source_bar_hash,
                "adj_factor_hash": stock.adj_factor_hash,
                "adjustment_as_of": as_of.isoformat(),
                "market_data_contract_version": stock.market_data_contract_version,
                "data_source": stock.data_source,
                "factor_series_for_pit": factor_note,
                "factor_series_rows": 0 if factor_series is None else int(len(factor_series)),
            }
            if dates[-1].date() == as_of:
                covered += 1
            else:
                stale_symbols.append(symbol)
        from .job_progress import progress
        progress("READ_MDAS_BATCHES", min(off+batch_size, len(ids)), len(ids))
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
        "excluded_symbols": excluded_symbols,
        "stale_symbols": stale_symbols,
        "expected_symbols": sorted(instrument_by_id.values()),
        "provenance": hashes,
        "pit_factor_series_available": any(
            v.get("factor_series_for_pit") == "authoritative_factor_series_attached"
            for v in hashes.values()),
        "pit_factor_series_symbols": sum(
            1 for v in hashes.values()
            if v.get("factor_series_for_pit") == "authoritative_factor_series_attached"),
        "external_fetch_allowed": False,
        "db_write_allowed": False,
    }
