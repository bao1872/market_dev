"""Train personal adjustments on TRAIN; choose blend and threshold on VAL only.

Never use TEST for fitting/selection. Standalone LightGBM personal mode is
fail-closed until a separately reviewed feature-row scorer is integrated.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from . import personal_model as pm
from . import personal_trainer as pt


def strict_personal_record(symbol, *, train_samples, val_samples, test_samples,
                           oof_scores, global_models, fmod, windows,
                           global_release_id, global_bundle_sha256,
                           data_sha256, price_contract,
                           min_adjust_samples=60, min_each_class=15,
                           min_val_samples=10, allow_standalone=False, **unused):
    if allow_standalone:
        raise pm.PersonalModelError('PERSONAL_STANDALONE_SCORING_NOT_IMPLEMENTED_FAIL_CLOSED')
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score

    train_dates = [r['decision_date'] for vals in train_samples.values() for r in vals]
    history = (pt.rw.stock_history_status(min(train_dates), max(train_dates), windows)
               if train_dates else {'status': 'INSUFFICIENT_HISTORY', 'reason': 'NO_TRAIN_EVENTS'})
    record = pm.new_record(symbol, windows=windows,
                           global_release_id=global_release_id,
                           global_bundle_sha256=global_bundle_sha256,
                           data_sha256=data_sha256, history=history)
    record['price_input_contract'] = price_contract
    record['created_at'] = pd.Timestamp.now('UTC').isoformat()
    record['samples'] = {'total': sum(len(x) for x in train_samples.values()),
                         'by_submodel': {str(k): len(v) for k, v in train_samples.items()},
                         'validation_by_submodel': {str(k): len(v) for k, v in val_samples.items()},
                         'test_by_submodel': {str(k): len(v) for k, v in test_samples.items()}}
    modes = {}
    reasons = []
    for key in pt.SUBKEYS:
        name = str(key)
        train = [r for r in train_samples.get(key, []) if r['sample_id'] in oof_scores]
        valid = val_samples.get(key, [])
        test = test_samples.get(key, [])
        def fallback(reason):
            modes[name] = {'mode': pm.MODE_FALLBACK, 'reason': reason,
                           'train_n': len(train), 'val_n': len(valid), 'test_n': len(test)}
            reasons.append(f'{name}:{reason}')
        if (len(train) < min_adjust_samples or
                len(set(r['label'] for r in train)) < 2 or
                min(Counter(r['label'] for r in train).values()) < min_each_class):
            fallback('INSUFFICIENT_TRAIN_OOF_OR_CLASS')
            continue
        if len(valid) < min_val_samples or len(set(r['label'] for r in valid)) < 2:
            fallback('INSUFFICIENT_INDEPENDENT_VALIDATION')
            continue
        if key not in global_models:
            fallback('GLOBAL_MODEL_FOR_SUBKEY_MISSING')
            continue
        x_train = np.array([oof_scores[r['sample_id']] for r in train]).reshape(-1, 1)
        y_train = np.array([r['label'] for r in train], dtype=int)
        lr = LogisticRegression(C=1.0, max_iter=2000)
        lr.fit(x_train, y_train)
        def base_scores(rows):
            x = np.stack([r['features'] for r in rows]).astype(np.float32)
            return global_models[key].predict_proba(pd.DataFrame(x, columns=fmod.ALL_COLS))[:, 1]
        # Critical: validation scores come from the global model trained on
        # TRAIN only. In particular the OOF train predictions are never used to
        # fit thresholds or choose the personal mixing coefficient.
        base_val = base_scores(valid)
        y_val = np.array([r['label'] for r in valid], dtype=int)
        p_val = lr.predict_proba(base_val.reshape(-1,1))[:, 1]
        best = None
        for w in (0.0, 0.15, 0.3, 0.45, 0.6):
            mixed = w * p_val + (1 - w) * base_val
            tau = pt._youden_threshold(y_val, mixed)
            if tau is None:
                continue
            j = pt._youden_j(y_val, mixed, tau)
            if best is None or (j, -w) > (best[0], -best[1]):
                best = (j, w, tau)
        if best is None:
            fallback('VALIDATION_THRESHOLD_UNDEFINED')
            continue
        _, w, tau = best
        if w == 0.0:
            fallback('NO_VALIDATED_PERSONAL_IMPROVEMENT')
            continue
        val_sc = w * p_val + (1-w) * base_val
        val_pred = val_sc >= tau
        entry = {'mode': pm.MODE_REGULARIZED,
                 'coef': float(lr.coef_[0][0]),
                 'intercept': float(lr.intercept_[0]),
                 'blend_weight': w, 'threshold': tau,
                 'selection_source': 'VAL_ONLY',
                 'train_n': len(train), 'val_n': len(valid), 'test_n': len(test),
                 'validation': {'n': len(valid), 'positive': int(y_val.sum()),
                                'auc': float(roc_auc_score(y_val, val_sc)),
                                **pt._confusion(y_val, val_pred)},
                 'test': {'n': len(test), 'status': 'UNSCORABLE_NO_TEST_SAMPLES'}
                 }
        if test:
            base_te = base_scores(test)
            y_te = np.array([r['label'] for r in test], dtype=int)
            te_sc = w * lr.predict_proba(base_te.reshape(-1,1))[:,1] + (1-w) * base_te
            entry['test'] = {'n': len(test), 'positive': int(y_te.sum()),
                             'auc': (float(roc_auc_score(y_te, te_sc))
                                     if len(set(y_te.tolist())) == 2 else None),
                             **pt._confusion(y_te, te_sc >= tau),
                             'selection_source': 'VAL_ONLY_TEST_UNUSED'}
        modes[name] = entry
    record['submodels'] = modes
    active = [v for v in modes.values() if v.get('mode') == pm.MODE_REGULARIZED]
    record['mode'] = pm.MODE_REGULARIZED if active else pm.MODE_FALLBACK
    record['fallback_reason'] = (None if active else
                                 'INSUFFICIENT_PERSONAL_SAMPLES:' + ';'.join(reasons[:6]))
    record['blend_weight'] = float(np.mean([e['blend_weight'] for e in active])) if active else 0.0
    record['metrics'] = {'submodel_val_test': {k: {n: v.get(n) for n in ('validation','test')}
                                              for k,v in modes.items() if v.get('mode') == pm.MODE_REGULARIZED}}
    record['last_trade_date_processed'] = windows['windows']['train'][1]
    record['last_matured_event_date'] = str(max(train_dates)) if train_dates else None
    record['artifact_path'] = None
    return record
