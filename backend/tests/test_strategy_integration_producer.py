"""Pure regression for causal scoring -> frozen reader DTO; no DB and no fitting."""
from __future__ import annotations
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.strategy_integration import model, producer


class StubBooster:
    def predict_proba(self, X):
        return np.column_stack([np.full(len(X), .1), np.full(len(X), .9)])


def sample():
    n=320
    rng=np.random.default_rng(26)
    v=25+np.sin(np.arange(n)/9)*2+np.cumsum(rng.normal(0,.25,n))
    o=v*.999; c=v*1.001; h=np.maximum(o,c)*1.018; l=np.minimum(o,c)*.981
    return pd.DataFrame({'datetime':pd.bdate_range('2024-01-01',periods=n),
                         'open':o,'high':h,'low':l,'close':c,'volume':np.full(n,2e6)})


@pytest.fixture(scope='module')
def setup_model():
    a=os.environ.get('PANJI_STRATEGY_MODEL_ASSETS')
    assert a and Path(a).is_dir(), 'PANJI_STRATEGY_MODEL_ASSETS required for frozen-engine parity tests'
    f=model.load_engine(Path(a))
    b={'models':{(layer,kind,k):StubBooster() for layer in ('L1','L2') for kind in ('H','L') for k in (1,2,3)},
       'thresholds':{(layer,kind,k):.5 for layer in ('L1','L2') for kind in ('H','L') for k in (1,2,3)}}
    return f,b


def test_two_sides_three_stages_are_real_asof(setup_model):
    f,b=setup_model;raw=sample()
    data=model.score_candidates(raw,'SZ:000001',f,b,min_anchor_bar=180)
    assert data['candidates']
    assert {c['kind'] for c in data['candidates']}=={'H','L'}
    for c in data['candidates']:
        for obs in c['observations']:
            assert obs['decision_date']==obs['data_cutoff']
            assert obs['l2_conditional_on_true_anchor']
            assert not obs['scores_calibrated']


def test_breakout_and_stable_identity(setup_model):
    f,b=setup_model;raw=sample()
    stock,daily=producer._candidate_and_bars(raw,'SZ:000001',f,b,chart_bars=80)
    assert stock['candidates']
    for c in stock['candidates']:
        assert sum(x['first_breakout'] for x in c['observations'])<=1
        for obs in c['observations']:
            assert obs['stage'] in ('D1','D2','D3')
            assert obs['decision_date'] in {p['date'] for p in stock['bars']}
    assert len({x['observation_id'] for x in daily})==len(daily)


def test_snapshot_evaluation_semantics(setup_model):
    f,b=setup_model;raw=sample()
    candidates,evaluation=producer.make_snapshots({'SZ:000001':raw},f,b,as_of=str(raw.datetime.iloc[-1].date()),source_sha='f'*64)
    assert candidates['metadata']['business_target']=={'L':'HH','H':'LL'}
    assert candidates['daily_batches'][-1]['trade_date']==str(raw.datetime.iloc[-1].date())
    assert evaluation['metadata']['cohort']=='ALL_EX_ANTE_ALERTS'
    assert all(x['model_release_id']==producer.RELEASE_ID for x in evaluation['events'])
    assert all(x['mfe_pct']>=0 and x['mae_pct']>=0 and x['mdd_pct']>=0 for x in evaluation['events'] if x['outcome']!='UNRESOLVED')


def test_pit_prefix_stability(setup_model):
    f,b=setup_model;raw=sample()
    checked=model.assert_prefix_invariance(raw,'SZ:000001',f,b,tail_cut=20)
    assert checked>0


def test_h_ll_and_l_hh_structural_target():
    # Direct deterministic structural outcome identity: no training/model score involved.
    p=lambda j,k,price,conf: {'bar':j,'type':k,'price':price,'confirmed_at':conf}
    raw=sample()
    ll=[p(30,'L',19,36),p(40,'H',28,46),p(50,'L',18,56)]
    hl=[p(30,'L',19,36),p(40,'H',28,46),p(50,'L',20,56)]
    assert producer._outcome(raw, ll, 40,'H',41,horizon=30)=='SUCCESS'
    assert producer._outcome(raw, hl, 40,'H',41,horizon=30)=='FAIL'
