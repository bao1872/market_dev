"""Canonical qfq daily-bar adapter for the strategy integration.

Two input paths, one price owner:
  1. Production: `canonical_strategy_bars.load_official_qfq_stock_batch()`
     delegates to MarketDataAggregationService.get_bars_batch(adj="qfq").
  2. Offline historical parquet: converted ONLY by Panji's canonical pure
     function `app.services.adj_factor.apply_adj_factor_with_as_of`, and
     labelled OFFLINE_FACTOR_DIAGNOSTIC because the raw parquet is not
     authoritative factor provenance.

A valid corporate action / factor change is NEVER an exclusion reason.
No network, DB or mutation. Exclusion reasons are always counted and reported.
"""
from __future__ import annotations
from collections import Counter
from datetime import date
from pathlib import Path
import hashlib
import json
import re

import numpy as np
import pandas as pd

from .producer import ProducerError

SYMBOL = re.compile(r'^(SH|SZ|BJ):[0-9]{6}$')
ADJUSTMENT_OWNER = 'app.services.adj_factor.apply_adj_factor_with_as_of'
CANONICAL_OWNER = 'MarketDataAggregationService.get_bars_batch'
OFFLINE_DIAGNOSTIC = 'OFFLINE_FACTOR_DIAGNOSTIC'
EXPORT_BARS = 'bars_daily_qfq.parquet'
EXPORT_MANIFEST = 'input_manifest.json'


def load_canonical_qfq_export(output_dir: Path, as_of: str, *, max_symbols: int = 0,
                              min_bars: int = 150) -> tuple[dict[str, pd.DataFrame], dict]:
    """Production-ready path: accept ONLY a verified canonical qfq export.

    Refuses non-qfq payloads, a mismatched adjustment anchor, or an export whose
    factor provenance is absent. It never converts prices and never falls back
    to raw bars.
    """
    output_dir = Path(output_dir)
    manifest_path = output_dir / EXPORT_MANIFEST
    bars_path = output_dir / EXPORT_BARS
    if not manifest_path.is_file() or not bars_path.is_file():
        raise ProducerError('CANONICAL_EXPORT_NOT_FOUND')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('price_adjustment') != 'qfq':
        raise ProducerError('CANONICAL_EXPORT_NOT_QFQ')
    if manifest.get('adjustment_as_of') != as_of:
        raise ProducerError('CANONICAL_EXPORT_AS_OF_MISMATCH')
    if not manifest.get('factor_provenance_authoritative'):
        raise ProducerError('CANONICAL_EXPORT_FACTOR_PROVENANCE_MISSING')
    raw = pd.read_parquet(bars_path)
    if not {'symbol', 'datetime', 'open', 'high', 'low', 'close', 'volume'}.issubset(raw.columns):
        raise ProducerError('CANONICAL_EXPORT_SCHEMA_MISSING')
    raw['datetime'] = pd.to_datetime(raw.datetime)
    raw = raw[raw.datetime <= pd.Timestamp(as_of)]
    reasons = Counter()
    asof_covered = 0
    results = {}
    for symbol, group in raw.groupby('symbol', sort=True):
        group = group.sort_values('datetime')
        if group.datetime.duplicated().any():
            reasons['duplicate_bar'] += 1
            continue
        if len(group) < min_bars:
            reasons['too_short'] += 1
            continue
        arr = group[['open', 'high', 'low', 'close', 'volume']].apply(
            pd.to_numeric, errors='coerce').to_numpy(float)
        if not np.isfinite(arr).all() or (arr[:, :4] <= 0).any() or (arr[:, 4] < 0).any():
            reasons['invalid_ohlcv'] += 1
            continue
        if (arr[:, 1] < np.maximum.reduce([arr[:, 0], arr[:, 2], arr[:, 3]])).any() or (
                arr[:, 2] > np.minimum.reduce([arr[:, 0], arr[:, 1], arr[:, 3]])).any():
            reasons['invalid_candle'] += 1
            continue
        df = pd.DataFrame(arr, columns=['open', 'high', 'low', 'close', 'volume'])
        df.insert(0, 'datetime', pd.Series(pd.to_datetime(group.datetime)).reset_index(drop=True))
        results[str(symbol)] = df
        if df.datetime.iloc[-1].date() == pd.Timestamp(as_of).date():
            asof_covered += 1
    if max_symbols:
        results = dict(sorted(results.items())[:max_symbols])
        asof_covered = sum(
            df.datetime.iloc[-1].date() == pd.Timestamp(as_of).date() for df in results.values()
        )
    total_symbols = int(raw.symbol.nunique())
    facts = {
        'source_sha256': sha_file(bars_path), 'source_as_of': as_of, 'source_rows': len(raw),
        'raw_instrument_count': total_symbols, 'eligible_stock_count': len(results),
        'covered_asof_count': asof_covered,
        'coverage_ratio': round(asof_covered / max(1, len(results)), 6),
        'coverage_ratio_of_eligible': round(asof_covered / max(1, len(results)), 6),
        'coverage_ratio_of_total': round(asof_covered / max(1, total_symbols), 6),
        'excluded': dict(reasons), 'corporate_action_excluded': 0,
        'has_BJ': any(s.startswith('BJ:') for s in results),
        'price_adjustment': 'qfq', 'adjustment_as_of': as_of,
        'adjustment_mode': 'CANONICAL_MDAS_QFQ', 'adjustment_owner': CANONICAL_OWNER,
        'factor_provenance_authoritative': True, 'data_read_only': True,
        'canonical_manifest': manifest,
    }
    return results, facts


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1<<20),b''):
            h.update(chunk)
    return h.hexdigest()


def _qfq_from_raw(group: pd.DataFrame, as_of: str) -> pd.DataFrame:
    """Qfq-convert one raw OHLCV group with the canonical Panji function.

    Never re-implements the adjustment formula; the factor column of the
    parquet is passed as the authoritative factor series.
    """
    from app.services.adj_factor import apply_adj_factor_with_as_of
    bars = group.set_index(pd.DatetimeIndex(pd.to_datetime(group['trade_date'])))
    bars = bars[['open', 'high', 'low', 'close', 'volume']].astype(float)
    factors = pd.DataFrame({
        'trade_date': pd.to_datetime(group['trade_date']).to_numpy(),
        'adj_factor': pd.to_numeric(group['adj_factor'], errors='coerce').astype(float).to_numpy(),
    })
    out = apply_adj_factor_with_as_of(bars, factors, date.fromisoformat(as_of))
    out = out[['open', 'high', 'low', 'close', 'volume']].astype(float)
    return out.sort_index()


def load_official_parquet(bars_file: Path, instruments_file: Path, as_of: str,
                          *, max_symbols: int = 0, min_bars: int = 150) -> tuple[dict[str,pd.DataFrame],dict]:
    required_bars = ['instrument_id','trade_date','open','high','low','close','volume','adj_factor']
    if not bars_file.is_file() or not instruments_file.is_file():
        raise ProducerError('SOURCE_FILES_NOT_FOUND')
    raw = pd.read_parquet(bars_file, columns=required_bars)
    instruments = pd.read_parquet(instruments_file)
    if not {'id','symbol'}.issubset(instruments.columns):
        raise ProducerError('INSTRUMENT_SCHEMA_MISSING')
    if 'market' not in instruments.columns:
        # The historical perfdata instrument map may contain id+bare symbol only.
        # Derive exchange from A-share code prefixes (never from ticker name).
        codes = instruments['symbol'].astype(str)
        instruments['market'] = np.select([codes.str.startswith('6'),codes.str.startswith(('0','3')),codes.str.startswith(('4','8','9'))],['SH','SZ','BJ'],default='UNKNOWN')
    m = instruments[['id','symbol','market']].drop_duplicates('id').copy()
    m['instrument_id'] = m.id.astype(str)
    m['panji_symbol'] = m.market.astype(str).str.upper() + ':' + m.symbol.astype(str).str.zfill(6)
    m = m[m.panji_symbol.str.fullmatch(SYMBOL.pattern)]
    raw['instrument_id'] = raw.instrument_id.astype(str)
    raw['trade_date'] = pd.to_datetime(raw.trade_date)
    raw = raw[raw.trade_date <= pd.Timestamp(as_of)]
    raw = raw.merge(m[['instrument_id','panji_symbol']], on='instrument_id', how='inner',validate='many_to_one')
    reasons = Counter()
    asof_covered = 0
    results = {}
    for symbol, group in raw.groupby('panji_symbol', sort=True):
        group = group.sort_values('trade_date')
        if group.trade_date.duplicated().any():
            reasons['duplicate_bar'] += 1
            continue
        if len(group) < min_bars:
            reasons['too_short'] += 1
            continue
        arr = group[['open','high','low','close','volume']].apply(pd.to_numeric,errors='coerce').to_numpy(float)
        if not np.isfinite(arr).all() or (arr[:,:4] <= 0).any() or (arr[:,4]<0).any():
            reasons['invalid_ohlcv'] += 1
            continue
        if (arr[:,1] < np.maximum.reduce([arr[:,0],arr[:,2],arr[:,3]])).any() or (arr[:,2] > np.minimum.reduce([arr[:,0],arr[:,1],arr[:,3]])).any():
            reasons['invalid_candle'] += 1
            continue
        # Factor data must exist and be usable, but a *change* of the factor is
        # a normal corporate action and is NEVER a reason to drop the stock.
        factor = pd.to_numeric(group.adj_factor,errors='coerce').to_numpy(float)
        if not np.isfinite(factor).all() or (factor<=0).any():
            reasons['invalid_adj_factor'] += 1
            continue
        try:
            adjusted = _qfq_from_raw(group, as_of)
        except (ValueError, KeyError, TypeError, AttributeError):
            reasons['adjustment_failed'] += 1
            continue
        if len(adjusted) < min_bars:
            reasons['too_short'] += 1
            continue
        arr2 = adjusted.to_numpy(float)
        if not np.isfinite(arr2).all() or (arr2[:,:4] <= 0).any() or (arr2[:,4]<0).any():
            reasons['invalid_ohlcv_after_adjustment'] += 1
            continue
        if (arr2[:,1] < np.maximum.reduce([arr2[:,0],arr2[:,2],arr2[:,3]])).any() or (
                arr2[:,2] > np.minimum.reduce([arr2[:,0],arr2[:,1],arr2[:,3]])).any():
            reasons['invalid_candle_after_adjustment'] += 1
            continue
        df = pd.DataFrame(arr2,columns=['open','high','low','close','volume'])
        df.insert(0,'datetime',pd.Series(pd.to_datetime(adjusted.index)).reset_index(drop=True))
        results[symbol] = df
        if df.datetime.iloc[-1].date() == pd.Timestamp(as_of).date():
            asof_covered += 1
    if max_symbols:
        by_market = {m:sorted(s for s in results if s.startswith(m+':')) for m in ('SH','SZ','BJ')}
        selected=[]
        while len(selected)<max_symbols and any(by_market.values()):
            for market in ('SH','SZ','BJ'):
                if by_market[market] and len(selected)<max_symbols:
                    selected.append(by_market[market].pop(0))
        results={s:results[s] for s in selected}
        asof_covered=sum(df.datetime.iloc[-1].date()==pd.Timestamp(as_of).date() for df in results.values())
    total_symbols = int(raw.panji_symbol.nunique())
    facts = {'source_sha256':hashlib.sha256((sha_file(bars_file)+'|'+sha_file(instruments_file)).encode()).hexdigest(),
             'source_as_of':as_of, 'source_rows':len(raw),
             'raw_instrument_count':total_symbols,
             'eligible_stock_count':len(results),'covered_asof_count':asof_covered,
             'coverage_ratio':round(asof_covered/max(1,len(results)),6),
             'coverage_ratio_of_eligible':round(asof_covered/max(1,len(results)),6),
             'coverage_ratio_of_total':round(asof_covered/max(1,total_symbols),6),
             'excluded':dict(reasons),
             'corporate_action_excluded':0,
             'has_BJ':any(s.startswith('BJ:') for s in results),
             'price_adjustment':'qfq',
             'adjustment_as_of':as_of,
             'adjustment_mode':OFFLINE_DIAGNOSTIC,
             'adjustment_owner':ADJUSTMENT_OWNER,
             'factor_provenance_authoritative':False,
             'data_read_only':True}
    return results,facts


def load_official_parquet_pit(bars_file: Path, instruments_file: Path, as_of: str,
                              *, max_symbols: int = 0, min_bars: int = 150
                              ) -> tuple[dict[str, pd.DataFrame], dict]:
    """Like load_official_parquet, but every frame also carries `adj_factor`.

    Keeping the authoritative factor series lets the caller rebuild the
    point-in-time qfq frame for each decision date, so a corporate action that
    happens *after* a decision day can never rescale that day's inputs.
    """
    required = ['instrument_id', 'trade_date', 'open', 'high', 'low', 'close',
                'volume', 'adj_factor']
    if not bars_file.is_file() or not instruments_file.is_file():
        raise ProducerError('SOURCE_FILES_NOT_FOUND')
    raw = pd.read_parquet(bars_file, columns=required)
    instruments = pd.read_parquet(instruments_file)
    if not {'id', 'symbol'}.issubset(instruments.columns):
        raise ProducerError('INSTRUMENT_SCHEMA_MISSING')
    if 'market' not in instruments.columns:
        codes = instruments['symbol'].astype(str)
        instruments['market'] = np.select(
            [codes.str.startswith('6'), codes.str.startswith(('0', '3')),
             codes.str.startswith(('4', '8', '9'))], ['SH', 'SZ', 'BJ'],
            default='UNKNOWN')
    m = instruments[['id', 'symbol', 'market']].drop_duplicates('id').copy()
    m['instrument_id'] = m.id.astype(str)
    m['panji_symbol'] = (m.market.astype(str).str.upper() + ':'
                         + m.symbol.astype(str).str.zfill(6))
    m = m[m.panji_symbol.str.fullmatch(SYMBOL.pattern)]
    raw['instrument_id'] = raw.instrument_id.astype(str)
    raw['trade_date'] = pd.to_datetime(raw.trade_date)
    raw = raw[raw.trade_date <= pd.Timestamp(as_of)]
    raw = raw.merge(m[['instrument_id', 'panji_symbol']], on='instrument_id',
                    how='inner', validate='many_to_one')
    reasons = Counter()
    asof_covered = 0
    results: dict[str, pd.DataFrame] = {}
    for symbol, group in raw.groupby('panji_symbol', sort=True):
        group = group.sort_values('trade_date')
        if group.trade_date.duplicated().any():
            reasons['duplicate_bar'] += 1
            continue
        if len(group) < min_bars:
            reasons['too_short'] += 1
            continue
        arr = group[['open', 'high', 'low', 'close', 'volume']].apply(
            pd.to_numeric, errors='coerce').to_numpy(float)
        if not np.isfinite(arr).all() or (arr[:, :4] <= 0).any() or (arr[:, 4] < 0).any():
            reasons['invalid_ohlcv'] += 1
            continue
        if (arr[:, 1] < np.maximum.reduce([arr[:, 0], arr[:, 2], arr[:, 3]])).any() or (
                arr[:, 2] > np.minimum.reduce([arr[:, 0], arr[:, 1], arr[:, 3]])).any():
            reasons['invalid_candle'] += 1
            continue
        factor = pd.to_numeric(group.adj_factor, errors='coerce').to_numpy(float)
        if not np.isfinite(factor).all() or (factor <= 0).any():
            reasons['invalid_adj_factor'] += 1
            continue
        adjusted = _qfq_from_raw(group, as_of)
        arr2 = adjusted.to_numpy(float)
        if not np.isfinite(arr2).all() or (arr2[:, :4] <= 0).any() or (arr2[:, 4] < 0).any():
            reasons['invalid_ohlcv_after_adjustment'] += 1
            continue
        df = pd.DataFrame(arr2, columns=['open', 'high', 'low', 'close', 'volume'])
        df.insert(0, 'datetime',
                  pd.Series(pd.to_datetime(adjusted.index)).reset_index(drop=True))
        # Authoritative factor series, aligned row-for-row with the frame above.
        df['adj_factor'] = factor
        results[symbol] = df
        if df.datetime.iloc[-1].date() == pd.Timestamp(as_of).date():
            asof_covered += 1
    if max_symbols:
        by_market = {mk: sorted(s for s in results if s.startswith(mk + ':'))
                     for mk in ('SH', 'SZ', 'BJ')}
        selected = []
        while len(selected) < max_symbols and any(by_market.values()):
            for market in ('SH', 'SZ', 'BJ'):
                if by_market[market] and len(selected) < max_symbols:
                    selected.append(by_market[market].pop(0))
        results = {s: results[s] for s in selected}
        asof_covered = sum(
            df.datetime.iloc[-1].date() == pd.Timestamp(as_of).date()
            for df in results.values())
    total_symbols = int(raw.panji_symbol.nunique())
    facts = {'source_sha256': hashlib.sha256(
        (sha_file(bars_file) + '|' + sha_file(instruments_file)).encode()).hexdigest(),
        'source_as_of': as_of, 'source_rows': len(raw),
        'raw_instrument_count': total_symbols, 'eligible_stock_count': len(results),
        'covered_asof_count': asof_covered,
        'coverage_ratio': round(asof_covered / max(1, len(results)), 6),
        'coverage_ratio_of_eligible': round(asof_covered / max(1, len(results)), 6),
        'coverage_ratio_of_total': round(asof_covered / max(1, total_symbols), 6),
        'excluded': dict(reasons), 'corporate_action_excluded': 0,
        'has_BJ': any(s.startswith('BJ:') for s in results),
        'price_adjustment': 'qfq', 'adjustment_as_of': as_of,
        'adjustment_mode': OFFLINE_DIAGNOSTIC, 'adjustment_owner': ADJUSTMENT_OWNER,
        'factor_provenance_authoritative': False, 'data_read_only': True,
        'pit_factor_series_available': True}
    return results, facts
