"""Observable strict first-break facts, independent of model score thresholds."""
from __future__ import annotations

import hashlib

from app.services.strategy_tv_zigzag import current_segment


def observe_breakout(raw, snapshots, anchor, kind, decision, *, segments_by_bar=None):
    from .producer import _reference
    reference, confirmed = _reference(raw, snapshots, anchor, kind)
    if reference is None:
        return None
    # Exclude a reference already breached on the anchor session. Daily OHLC
    # cannot reconstruct intraday order, so it cannot be a later first-hit fact.
    key = 'high' if kind == 'L' else 'low'
    def crosses(value):
        return value > reference if kind == 'L' else value < reference
    if crosses(float(raw[key].iloc[anchor])):
        return None
    first = next((i for i in range(anchor+1, min(anchor+3, decision)+1)
                  if crosses(float(raw[key].iloc[i]))), None)
    if first is None:
        return None
    return {'first_date': str(raw.datetime.iloc[first].date()), 'first_stage': f'D{first-anchor}',
            'direction': 'HIGH' if kind == 'L' else 'LOW', 'reference_price': reference,
            'reference_confirmed_at': confirmed, 'first_bar': first,
            'trend_at_breakout': segments_by_bar[first] if segments_by_bar is not None else current_segment(raw.iloc[:first+1]),
            'is_first_today': first == decision,
            'holding_beyond_reference': crosses(float(raw.close.iloc[decision]))}


def event_identity(symbol, anchor_date, kind, release_id):
    return hashlib.sha256(f'{symbol}|{anchor_date}|{kind}|{release_id}|FIRST_BREAK'.encode()).hexdigest()[:24]
