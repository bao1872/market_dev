"""Frozen model -> audited, read-only Panji V2 snapshots.

This module has NO scheduler, HTTP, production DB write, or model fitting.
It creates deterministic candidate/evaluation JSON from historical OHLCV. A separate,
operator-authorized deployment must enable batch publication to live readers.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import model

CONTRACT = 'L_HH__H_LL__V1'
# Price input contract for the release identity. The frozen bundle itself was
# trained on unadjusted OHLCV; the served pipeline now feeds canonical qfq bars,
# so the release identity is versioned QFQ_ASOF_V1 and the original model SHA is
# retained separately as model.BUNDLE_SHA / bundle_sha256.
PRICE_INPUT_CONTRACT = 'QFQ_ASOF_V1'
EVAL_HORIZON = 20
RELEASE_ID = hashlib.sha256(f'{model.BUNDLE_SHA}|{model.ENGINE_SHA}|{CONTRACT}|{PRICE_INPUT_CONTRACT}'.encode()).hexdigest()

def release_id_for(bundle_sha: str, price_contract: str = PRICE_INPUT_CONTRACT) -> str:
    return hashlib.sha256(f'{bundle_sha}|{model.ENGINE_SHA}|{CONTRACT}|{price_contract}'.encode()).hexdigest()


def require_price_contract(bundle: dict, expected: str = PRICE_INPUT_CONTRACT) -> str:
    """Fail closed unless the weights declare a matching price-input contract.

    Returns the declared contract. Raises ProducerError when it is missing
    (unknown provenance) or does not match the pipeline contract, so inference
    can never silently fall back to raw-trained weights.
    """
    declared = (bundle.get('price_input_contract') or '').strip()
    if not declared:
        raise ProducerError('MODEL_PRICE_INPUT_CONTRACT_UNKNOWN')
    if declared != expected:
        raise ProducerError('MODEL_PRICE_INPUT_CONTRACT_MISMATCH')
    return declared


class ProducerError(ValueError):
    pass


def _day(df: pd.DataFrame, index: int) -> str:
    return str(pd.Timestamp(df.datetime.iloc[index]).date())


def _reference(raw: pd.DataFrame, snaps: Any, j: int, kind: str) -> tuple[float | None, str | None]:
    """Freeze the *opposite* reference confirmed no later than anchor close."""
    opposite = 'H' if kind == 'L' else 'L'
    previous = [p for p in snaps[j] if p['type'] == opposite and p['bar'] < j
                and p['confirmed_at'] is not None and p['confirmed_at'] <= j]
    if not previous:
        return None, None
    ref = previous[-1]
    return float(ref['price']), _day(raw, ref['confirmed_at'])


def _candidate_and_bars(raw: pd.DataFrame, symbol: str, fmod: Any, bundle: dict,
                        *, chart_bars: int = 85, release_id: str = RELEASE_ID,
                        pit_source: pd.DataFrame | None = None,
                        personal: dict | None = None) -> tuple[dict[str, Any], list[dict]]:
    if 'fourteen_runtime' in bundle:
        from .fourteen_adapter import build_stock
        if personal:
            raise ProducerError('FOURTEEN_REQUIRES_GLOBAL_ONLY')
        return build_stock(raw, symbol, bundle, release_id=release_id,
                           chart_bars=chart_bars, pit_source=pit_source)
    result = model.score_candidates(raw, symbol, fmod, bundle,
                                    min_anchor_bar=max(0, len(raw)-chart_bars),
                                    pit_source=pit_source, release_id=release_id,
                                    personal=personal)
    _, snapshots, _ = fmod.replay(raw)
    lookup = {d: i for i, d in enumerate(raw.datetime.dt.strftime('%Y-%m-%d'))}
    bar_start = max(0, len(raw) - chart_bars)
    bars = []
    for r in raw.iloc[bar_start:].itertuples(index=False):
        bars.append({'date': str(pd.Timestamp(r.datetime).date()), 'open': float(r.open),
                     'high': float(r.high), 'low': float(r.low), 'close': float(r.close)})
    candidates = []
    daily = []
    for candidate in result['candidates']:
        j = lookup.get(candidate['anchor_date'])
        if j is None or j < bar_start:
            continue
        kind = candidate['kind']
        reference, confirmed_at = _reference(raw, snapshots, j, kind)
        seen_first = False
        observations = []
        for obs in candidate['observations']:
            t = lookup[obs['decision_date']]
            # Anchor j+1 is D1, j+2 is D2, j+3 is D3 in *trading bars*.
            stage = int(obs['stage'][1:])
            if t != j + stage:
                raise ProducerError('STAGE_BAR_MISMATCH')
            if obs['data_cutoff'] != obs['decision_date']:
                raise ProducerError('FUTURE_DATA_CUTOFF')
            crossed = bool(reference is not None and (
                (kind == 'L' and float(raw.high.iloc[t]) > reference) or
                (kind == 'H' and float(raw.low.iloc[t]) < reference)))
            first = crossed and not seen_first
            if crossed:
                seen_first = True
            scored = bool(obs['scoreable'])
            l1 = float(obs['l1_score']) if scored else None
            l2 = float(obs['l2_score']) if scored else None
            o = {
                'observation_id': hashlib.sha256(f'{candidate["candidate_id"]}|{release_id}|{obs["stage"]}|{obs["decision_date"]}'.encode()).hexdigest()[:24],
                'symbol': symbol,
                'global_release_id': release_id,
                'personal_release_id': obs.get('personal_release_id'),
                'scoring_mode': obs.get('scoring_mode', 'GLOBAL_ONLY'),
                'stage': obs['stage'], 'decision_date': obs['decision_date'],
                'data_cutoff': obs['decision_date'],
                'l1_score': l1, 'l2_score': l2,
                'l1_threshold': float(obs['l1_threshold']), 'l2_threshold': float(obs['l2_threshold']),
                'joint_alert': bool(scored and l1 is not None and l2 is not None
                                    and l1 >= obs['l1_threshold'] and l2 >= obs['l2_threshold']),
                'reference_price': reference, 'reference_confirmed_at': confirmed_at,
                'first_breakout': bool(first),
            }
            observations.append(o)
            daily.append({'observation_id': o['observation_id'], 'candidate_id': candidate['candidate_id'],
                          'symbol': symbol, 'stage': o['stage'], 'kind': kind,
                          'global_release_id': release_id,
                          'personal_release_id': o['personal_release_id'],
                          'scoring_mode': o['scoring_mode'],
                          'l1_score': l1, 'l2_score': l2, 'joint_alert': o['joint_alert'],
                          'first_breakout': o['first_breakout'], 'trade_date': o['decision_date']})
        if observations:
            candidates.append({'candidate_id': candidate['candidate_id'], 'anchor_date': candidate['anchor_date'],
                               'visible_at': candidate['visible_at'], 'anchor_price': float(candidate['anchor_price']),
                               'kind': kind, 'observations': observations})
    return {'bars': bars, 'candidates': candidates}, daily


def _outcome(raw: pd.DataFrame, final: list, j: int, kind: str, t: int,
             horizon: int = EVAL_HORIZON) -> str:
    """All candidate alerts, including false anchors, kept in the denominator.

    Label is known only when the next true structure/negation has matured within
    the fixed observation horizon, never used as a prediction feature.
    """
    if t + horizon >= len(raw):
        return 'UNRESOLVED'
    true_idx = next((i for i, p in enumerate(final) if p['bar'] == j and p['type'] == kind), None)
    last_bar = t + horizon
    if true_idx is None:
        negating = [p for p in final if p['bar'] > j and p['type'] != kind
                    and p['confirmed_at'] <= last_bar]
        return 'FAIL' if negating else 'UNRESOLVED'
    i = true_idx
    if i < 1 or i + 1 >= len(final):
        return 'UNRESOLVED'
    prev, nextp = final[i - 1], final[i + 1]
    if prev['type'] != nextp['type'] or nextp['confirmed_at'] > last_bar or nextp['bar'] <= t:
        return 'UNRESOLVED'
    if kind == 'L':
        return 'SUCCESS' if float(nextp['price']) > float(prev['price']) else 'FAIL'
    return 'SUCCESS' if float(nextp['price']) < float(prev['price']) else 'FAIL'


def _path_metrics(raw: pd.DataFrame, kind: str, t: int,
                  horizon: int = EVAL_HORIZON) -> dict[str, Any]:
    """Next-session-open theoretical envelope. Never realized P&L.

    MDD is max drawdown of directional *close* returns relative to entry open;
    first passage within same OHLC bar remains unknown.
    """
    if t + horizon >= len(raw):
        raise ProducerError('UNMATURED_PATH')
    start = t + 1
    entry = float(raw.open.iloc[start])
    if entry <= 0:
        raise ProducerError('INVALID_ENTRY')
    path = []
    directional_closes = [0.0]
    for ix in range(start, start+horizon):
        hi, lo, cl = (float(raw[k].iloc[ix]) for k in ('high','low','close'))
        if kind == 'L':
            fav = max(0.,100 * (hi/entry-1))
            adv = max(0.,100 * (1-lo/entry))
            close_ret = 100 * (cl/entry-1)
        else:
            fav = max(0.,100 * (1-lo/entry))
            adv = max(0.,100 * (hi/entry-1))
            close_ret = 100 * (1-cl/entry)
        path.append({'favorable_pct': fav, 'adverse_pct': adv})
        directional_closes.append(close_ret)
    mfe = max(row['favorable_pct'] for row in path)
    mae = max(row['adverse_pct'] for row in path)
    peak = directional_closes[0]
    mdd = 0.
    for z in directional_closes:
        peak = max(peak, z)
        mdd = max(mdd, peak - z)
    return {'mfe_pct': round(mfe, 6), 'mae_pct': round(mae, 6),
            'mdd_pct': round(mdd, 6), 'endpoint_pct': round(directional_closes[-1], 6),
            'path': path}


def make_evaluation(stock_raw: dict[str, pd.DataFrame], stocks: dict[str, dict], fmod: Any,
                    *, as_of: str, source_sha: str, release_id: str = RELEASE_ID,
                    price_adjustment: str = 'qfq',
                    adjustment_mode: str | None = None,
                    bundle_sha256: str = '') -> dict:
    models = [{'id': f'joint_d{k}_{side.lower()}', 'name': f'{side} D{k} 联合', 'side': side}
              for side in ('BUY','SELL') for k in (1,2,3)]
    events = []
    for symbol, stock in stocks.items():
        raw = stock_raw[symbol]
        final, _, _ = fmod.replay(raw)
        idx = {str(x.date()): i for i, x in enumerate(pd.to_datetime(raw.datetime))}
        for candidate in stock['candidates']:
            j = idx[candidate['anchor_date']]
            kind = candidate['kind']
            side = 'BUY' if kind == 'L' else 'SELL'
            for o in candidate['observations']:
                if not o['joint_alert']:
                    continue
                t = idx[o['decision_date']]
                outcome = _outcome(raw, final, j, kind, t)
                record = {'event_id': candidate['candidate_id'],
                          'model_id': f'joint_d{o["stage"][1:]}_{side.lower()}', 'symbol': symbol,
                          'global_release_id': release_id,
                          'personal_release_id': o.get('personal_release_id'),
                          'scoring_mode': o.get('scoring_mode', 'GLOBAL_ONLY'),
                          'side': side, 'signal_time': o['decision_date'], 'outcome': outcome,
                          'candidate_id': candidate['candidate_id'], 'observation_id': o['observation_id'],
                          'stage': o['stage'], 'decision_date': o['decision_date'],
                          'model_release_id': release_id}
                if outcome != 'UNRESOLVED':
                    record.update(_path_metrics(raw, kind, t))
                events.append(record)
    if any(o.get('breakout') for stock in stocks.values() for c in stock['candidates'] for o in c['observations']):
        for trend in ('up', 'down', 'unknown'):
            for side in ('BUY', 'SELL'):
                models.append({'id': f'breakout_{trend}_{side.lower()}', 'side': side,
                    'name': f'{ {"up":"上升", "down":"下降", "unknown":"方向待确认"}[trend]} · {"上破前高" if side == "BUY" else "下破前低"}',
                    'family': 'BREAKOUT'})
        for symbol, stock in stocks.items():
            raw = stock_raw[symbol]
            idx = {str(x.date()): i for i, x in enumerate(pd.to_datetime(raw.datetime))}
            for candidate in stock['candidates']:
                for o in candidate['observations']:
                    fact = o.get('breakout')
                    if not fact or not o['first_breakout']:
                        continue
                    trend = (fact.get('trend_at_breakout') or {}).get('direction')
                    group = 'up' if trend == '向上' else 'down' if trend == '向下' else 'unknown'
                    side = 'BUY' if candidate['kind'] == 'L' else 'SELL'
                    t = idx[o['decision_date']]
                    record = {'event_id': fact['event_id'], 'model_id': f'breakout_{group}_{side.lower()}',
                        'family': 'BREAKOUT', 'side': side, 'symbol': symbol, 'signal_time': o['decision_date'],
                        'candidate_id': candidate['candidate_id'], 'observation_id': o['observation_id'],
                        'stage': o['stage'], 'decision_date': o['decision_date'], 'model_release_id': release_id,
                        'trend_at_event': trend, 'outcome': 'UNRESOLVED',
                        'outcome_definition': 'DIRECTIONAL_ENDPOINT_POSITIVE_NEXT_OPEN_20B'}
                    if t + EVAL_HORIZON < len(raw):
                        metrics = _path_metrics(raw, candidate['kind'], t)
                        record.update(metrics)
                        record['outcome'] = 'SUCCESS' if metrics['endpoint_pct'] > 0 else 'FAIL'
                    events.append(record)
    return {'schema': 'panji-strategy-evaluation-v1',
            'metadata': {'strategy_key': 'zigzag_two_stage', 'strategy_name': '两层 ZigZag 结构机会',
                         'version': release_id[:16], 'generated_at': as_of,
                         'evaluation_protocol': f'ALL_ALERTS_NEXT_OPEN_{EVAL_HORIZON}B_H_LL_L_HH_V1',
                         'model_sha256': bundle_sha256 or model.BUNDLE_SHA,
                         'data_sha256': source_sha,
                         'audit_ref': f'candidate_full_cohort_{release_id[:16]}',
                         'status': 'AUDITED', 'cohort': 'ALL_EX_ANTE_ALERTS',
                         'price_adjustment': price_adjustment,
                         'adjustment_mode': adjustment_mode},
            'models': models, 'events': events}


def make_snapshots(stock_raw: dict[str, pd.DataFrame], fmod: Any, bundle: dict,
                   *, as_of: str, source_sha: str, chart_bars: int = 85,
                   daily_days: int = 20, release_id: str = RELEASE_ID,
                   price_adjustment: str = 'qfq',
                   adjustment_mode: str | None = None,
                   bundle_sha256: str = '',
                   pit_source: dict[str, pd.DataFrame] | None = None,
                   personal_by_symbol: dict[str, dict] | None = None) -> tuple[dict, dict]:
    if not stock_raw:
        raise ProducerError('NO_STOCKS')
    stocks: dict[str, dict] = {}
    by_date: dict[str, list] = defaultdict(list)
    latest = date.fromisoformat(as_of)
    for position, (symbol, raw) in enumerate(sorted(stock_raw.items())):
        from .job_progress import progress
        progress("PREDICT_STOCKS", position, len(stock_raw), symbol=symbol)
        if len(raw) < 150 or pd.Timestamp(raw.datetime.iloc[-1]).date() > latest:
            raise ProducerError(f'INVALID_SOURCE_WINDOW {symbol}')
        one, daily = _candidate_and_bars(raw, symbol, fmod, bundle, chart_bars=chart_bars,
                                        release_id=release_id,
                                        pit_source=(pit_source or {}).get(symbol),
                                        personal=(personal_by_symbol or {}).get(symbol))
        stocks[symbol] = one
        for row in daily:
            by_date[row.pop('trade_date')].append(row)
    days = sorted([x for x in by_date if x <= as_of])[-daily_days:]
    if as_of not in days:
        days.append(as_of)
    batches = [{'trade_date': day, 'release_id': release_id, 'status': 'PUBLISHED',
                'items': by_date.get(day, [])} for day in sorted(set(days))]
    candidate = {'schema': 'panji-strategy-candidates-v2',
                 'metadata': {'strategy_id':'zigzag_two_stage', 'release_id':release_id,
                              'feature_sha':model.ENGINE_SHA, 'audit_ref':f'{source_sha[:16]}_{CONTRACT}',
                              'generated_at':datetime.now(timezone.utc).isoformat(), 'status':'AUDITED',
                              'score_calibrated':False, 'price_adjustment':price_adjustment,
                              'adjustment_mode':adjustment_mode,
                              'model_sha256': bundle_sha256 or model.BUNDLE_SHA,
                              'business_target': {'L':'HH','H':'LL'}, 'source_sha256':source_sha,
                              'scoring_architecture': 'GLOBAL_ROLLING_PLUS_OPTIONAL_PERSONAL',
                              'personal_symbols': sorted((personal_by_symbol or {}).keys()),
                              'global_release_id': release_id},
                 'stocks':stocks, 'daily_batches':batches}
    evaluation = make_evaluation(stock_raw, stocks, fmod, as_of=as_of, source_sha=source_sha,
                                  release_id=release_id, price_adjustment=price_adjustment,
                                  adjustment_mode=adjustment_mode,
                                  bundle_sha256=bundle_sha256)
    if 'fourteen_runtime' in bundle:
        from .fourteen_adapter import SCHEMA, task_samples
        candidate['schema'] = SCHEMA
        candidate['metadata'].update(model_version=bundle['model_version'],
            scoring_architecture='GLOBAL_FOURTEEN', personal_symbols=[])
        evaluation['metadata']['release_id'] = release_id
        evaluation['metadata']['model_version'] = bundle['model_version']
        evaluation['task_samples'] = [row for symbol, stock in stocks.items()
            for row in task_samples(stock_raw[symbol], stock, fmod, symbol,
                                    pit_source=(pit_source or {}).get(symbol),
                                    split_windows=bundle['fourteen_runtime'].manifest.get('split_windows'))]
    return candidate, evaluation


def verify_against_readers(candidate: dict, evaluation: dict) -> dict:
    """Execute the real market_dev validators; no write is performed."""
    from app.services.strategy_candidate_snapshot import validate as validate_candidate
    from app.services.strategy_research_read_service import _check_snapshot
    validate_candidate(candidate)
    _check_snapshot(evaluation)
    events = evaluation['events']
    by_side = Counter(e['side'] for e in events)
    return {'candidate_stocks':len(candidate['stocks']),
            'daily_batches':len(candidate['daily_batches']),
            'evaluation_signal_rows':len(events),
            'buy_alert_rows':by_side['BUY'], 'sell_alert_rows':by_side['SELL'],
            'unresolved_rows':sum(e['outcome']=='UNRESOLVED' for e in events)}


def encode_json(blob: dict) -> bytes:
    data = json.dumps(blob, ensure_ascii=False, allow_nan=False, separators=(',', ':'), sort_keys=True).encode('utf-8')
    return data
