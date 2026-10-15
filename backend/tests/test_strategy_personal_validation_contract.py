"""Independent validation governs personal thresholds; TEST cannot tune models."""
from __future__ import annotations

import numpy as np
import pytest
from datetime import date

from app.strategy_integration import personal_validation as pv
from app.strategy_integration import personal_model as pm
from app.strategy_integration import rolling_windows as rw


class FakeGlobal:
    def predict_proba(self, x):
        z = np.asarray(x).reshape(-1)
        p = np.clip(0.15 + 0.7 * z, 0.02, 0.98)
        return np.column_stack([1-p, p])


class FakeMod:
    ALL_COLS = ['score']


def rows(symbol, n, split):
    ans=[]
    for i in range(n):
        score = 0.15 + 0.7 * ((i * 37) % 101) / 101
        ans.append({'sample_id': f'{symbol}:{split}:{i}', 'symbol':symbol,
                    'features': np.array([score],np.float32),
                    'label': int(score < 0.52),
                    'decision_date': date(2024 if split=='train' else 2026, 3 if split=='val' else 8, 1)})
    return ans


def sample_windows():
    return rw.rolling_windows(date(2026, 10, 15))


@pytest.mark.parametrize('symbol', ['SH:600000','SZ:000001'])
def test_changing_only_test_labels_never_changes_selection(symbol):
    pytest.importorskip('sklearn')
    k=('L1','L',1)
    tr=rows(symbol,80,'train')
    va=rows(symbol,34,'val')
    te=rows(symbol,30,'test')
    oof={r['sample_id']: float(r['features'][0]) for r in tr}
    args=dict(train_samples={k:tr},val_samples={k:va},
              oof_scores=oof,global_models={k:FakeGlobal()}, fmod=FakeMod(),
              windows=sample_windows(),global_release_id='1'*64,
              global_bundle_sha256='2'*64,data_sha256='3'*64,
              price_contract='QFQ_ASOF_V1')
    a=pv.strict_personal_record(symbol,test_samples={k:te},**args)
    alt=[dict(r,label=1-r['label']) for r in te]
    b=pv.strict_personal_record(symbol,test_samples={k:alt},**args)
    ka=a['submodels'][str(k)]; kb=b['submodels'][str(k)]
    assert ka['selection_source']=='VAL_ONLY'
    assert {n:ka[n] for n in ('mode','coef','intercept','threshold','blend_weight')} == {
        n:kb[n] for n in ('mode','coef','intercept','threshold','blend_weight')}
    assert a['status']==pm.STATUS_CANDIDATE


def test_insufficient_validation_is_explicit_fallback():
    pytest.importorskip('sklearn')
    k=('L2','H',2); s='SH:600000'
    tr=rows(s,80,'train'); va=rows(s,5,'val')
    a=pv.strict_personal_record(s,train_samples={k:tr},val_samples={k:va},
        test_samples={k:rows(s,20,'test')},
        oof_scores={r['sample_id']:float(r['features'][0]) for r in tr},
        global_models={k:FakeGlobal()},fmod=FakeMod(),windows=sample_windows(),
        global_release_id='1'*64,global_bundle_sha256='2'*64,data_sha256='3'*64,
        price_contract='QFQ_ASOF_V1')
    assert a['submodels'][str(k)]['reason']=='INSUFFICIENT_INDEPENDENT_VALIDATION'
    assert a['mode']==pm.MODE_FALLBACK
