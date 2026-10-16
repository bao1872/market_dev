"""B-stage exact frozen-engine parity tests; local pure unit only."""
from __future__ import annotations

import pytest
from app.services import strategy_structure_view as sv


def _bars(n=48):
    from datetime import date, timedelta
    start=date(2026,1,5)
    records=[]
    for i in range(n):
        # Reversal up/down with 8%-12% swings and daily variation
        middle=100+10*((i//8)%2) + 3*((i%8)/8)
        records.append({'date':(start+timedelta(days=i)).isoformat(),
                        'open':middle,'high':middle+1.3,'low':middle-1.5,'close':middle+.2})
    return {'symbol':'SH:600000','source_policy':'MDAS_DB_ONLY','price_adjustment':'qfq',
            'source_bar_hash':'real-hash-stub','adj_factor_hash':'factor-hash-stub',
            'as_of':records[-1]['date'],'bars':records,'count':len(records)}


def test_exact_frozen_engine_digest_and_parity():
    from app.strategy_integration.model import ENGINE_SHA
    assert sv.ENGINE_SHA == ENGINE_SHA
    result=sv.structure_from_verified_bars(_bars())
    assert result['algorithm_sha256']==ENGINE_SHA
    assert result['decision_pit_verified'] is False
    assert all(row['anchor_date']<=row['confirmed_at']<=result['as_of'] for row in result['confirmed_pivots'])
    assert all(row['visible_at']>row['anchor_date'] and not row['model_scored'] for row in result['early_candidates'])
    assert result['latest_mutable_count'] in (0,1)
    assert not result['hindsight_label_used_for_model_scoring']


def test_frozen_engine_prefix_invariance():
    full=sv.structure_from_verified_bars(_bars(48))
    partial=sv.structure_from_verified_bars(_bars(35))
    # Only pivots already CONFIRMED at the shorter decision cutoff can appear.
    cutoff=partial['as_of']
    expected=[p for p in full['confirmed_pivots'] if p['confirmed_at']<=cutoff]
    assert partial['confirmed_pivots']==expected


def test_no_future_bar_and_untrusted_source(monkeypatch):
    p=_bars()
    p['as_of']='2026-01-10'
    with pytest.raises(sv.StructureUnavailable,match='NON_MONOTONIC'):sv.structure_from_verified_bars(p)
    p=_bars();p['source_policy']='HYBRID'
    with pytest.raises(sv.StructureUnavailable,match='NOT_CANONICAL'):sv.structure_from_verified_bars(p)
    p=_bars();p['adj_factor_hash']=''
    with pytest.raises(sv.StructureUnavailable,match='QFQ_PROVENANCE'):sv.structure_from_verified_bars(p)


def test_warmup_insufficient_is_not_fabricated():
    with pytest.raises(sv.StructureUnavailable,match='TOO_SHORT'):
        sv.structure_from_verified_bars(_bars(10))


def test_frozen_engine_is_not_silent_fallback(monkeypatch):
    def unavailable():raise sv.StructureUnavailable('FROZEN_B55_ENGINE_NOT_VERIFIED')
    monkeypatch.setattr(sv,'_frozen_engine',unavailable)
    with pytest.raises(sv.StructureUnavailable,match='FROZEN_B55'):
        sv.structure_from_verified_bars(_bars())
