"""Per-symbol personal model training.

Preferred mode is a REGULARIZED ADJUSTMENT model trained on the full-market
model's *time-ordered out-of-fold* predictions plus the symbol's own matured
events. In-sample fitted scores are never used as training input, so the
full-market model's training-set fit cannot contaminate personal training.

A standalone lightweight classifier is allowed only when the symbol has enough
samples; otherwise the record falls back to the global model with an explicit
reason. Nothing here activates or publishes a model.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import model as engine_model
from . import personal_model as pm
from . import rolling_windows as rw
from .producer import ProducerError

SUBKEYS = [(layer, kind, stage) for layer in ('L1', 'L2')
           for kind in ('H', 'L') for stage in (1, 2, 3)]

DEFAULT_MIN_ADJUST_SAMPLES = 60
DEFAULT_MIN_EACH_CLASS = 15
DEFAULT_MIN_STANDALONE_SAMPLES = 300
DEFAULT_OOF_FOLDS = 3
DEFAULT_OOF_TREES = 60
DEFAULT_LIGHT_TREES = 80


def time_ordered_folds(days: list[date], n_folds: int) -> list[tuple[date, date]]:
    """Contiguous, time-ordered fold boundaries (no shuffling, no leakage)."""
    ordered = sorted(set(days))
    if n_folds < 2 or len(ordered) < n_folds:
        return []
    edges = [ordered[int(round(i * (len(ordered) - 1) / (n_folds - 1)))]
             for i in range(n_folds)]
    folds = []
    for i in range(1, n_folds):
        folds.append((edges[i - 1], edges[i]))
    return folds


def compute_oof_base_scores(train_samples: dict, fmod, *, folds: int,
                            trees: int, threads: int, seed: int) -> tuple[dict, dict]:
    """Out-of-fold full-market base scores for every training sample.

    Fold i is scored by a model fitted only on folds < i, so a sample never sees
    a model that was trained on it. Returns (scores, provenance).
    """
    from lightgbm import LGBMClassifier

    scores: dict[str, float] = {}
    provenance = {'folds': folds, 'trees': trees, 'unscored_samples': 0,
                  'scored_samples': 0, 'submodels': {}}
    for key in SUBKEYS:
        rows = train_samples.get(key, [])
        if not rows:
            provenance['submodels'][str(key)] = {'n': 0, 'scored': 0, 'unscored': 0}
            continue
        ordered = sorted(rows, key=lambda r: (r['decision_date'], r['sample_id']))
        days = [r['decision_date'] for r in ordered]
        bounds = time_ordered_folds(days, folds)
        if not bounds:
            provenance['unscored_samples'] += len(ordered)
            provenance['submodels'][str(key)] = {
                'n': len(ordered), 'scored': 0, 'unscored': len(ordered)}
            continue
        starts = [b[0] for b in bounds]
        scored = 0
        for i, (lo, hi) in enumerate(bounds):
            prior = [r for r in ordered if r['decision_date'] < lo]
            block = [r for r in ordered if lo <= r['decision_date'] <= hi]
            if not block:
                continue
            if i == 0 or len({r['label'] for r in prior}) < 2:
                provenance['unscored_samples'] += len(block)
                continue
            xt = np.stack([r['features'] for r in prior]).astype(np.float32)
            yt = np.asarray([r['label'] for r in prior], dtype=int)
            clf = LGBMClassifier(n_estimators=trees, num_leaves=15,
                                 learning_rate=.08, min_child_samples=40,
                                 reg_lambda=5.0, verbosity=-1, n_jobs=threads,
                                 random_state=seed)
            clf.fit(pd.DataFrame(xt, columns=list(fmod.ALL_COLS)), yt)
            xb = np.stack([r['features'] for r in block]).astype(np.float32)
            vals = clf.predict_proba(pd.DataFrame(xb, columns=list(fmod.ALL_COLS)))[:, 1]
            if len(vals) != len(block) or not np.isfinite(vals).all():
                raise ProducerError('NON_FINITE_OOF_BASE_SCORE ' + str(key))
            for r, v in zip(block, vals):
                scores[r['sample_id']] = float(v)
                scored += 1
        provenance['scored_samples'] += scored
        provenance['submodels'][str(key)] = {
            'n': len(ordered), 'scored': scored, 'unscored': len(ordered) - scored}
    return scores, provenance


def _youden_threshold(y, s) -> float | None:
    from sklearn.metrics import roc_curve
    if len(set(y.tolist())) < 2:
        return None
    fpr, tpr, thr = roc_curve(y, s)
    finite = np.isfinite(thr)
    if not finite.any():
        return None
    idx = np.where(finite)[0][int(np.argmax((tpr - fpr)[finite]))]
    tau = float(np.clip(thr[idx], 1e-6, 1 - 1e-6))
    return tau if np.isfinite(tau) else None


def _auc(y, s):
    from sklearn.metrics import roc_auc_score
    if len(set(np.asarray(y).tolist())) < 2:
        return None
    return float(roc_auc_score(np.asarray(y), np.asarray(s)))


def _confusion(y, pred) -> dict:
    y = np.asarray(y, dtype=int)
    pred = np.asarray(pred, dtype=bool)
    return {'tp': int(((pred == 1) & (y == 1)).sum()),
            'fp': int(((pred == 1) & (y == 0)).sum()),
            'tn': int(((pred == 0) & (y == 0)).sum()),
            'fn': int(((pred == 0) & (y == 1)).sum())}


def train_personal(symbol: str, *, samples_by_key: dict, oof_scores: dict,
                   fmod, windows: dict, global_release_id: str,
                   global_bundle_sha256: str, data_sha256: str,
                   price_contract: str,
                   min_adjust_samples: int = DEFAULT_MIN_ADJUST_SAMPLES,
                   min_each_class: int = DEFAULT_MIN_EACH_CLASS,
                   min_standalone_samples: int = DEFAULT_MIN_STANDALONE_SAMPLES,
                   allow_standalone: bool = False,
                   light_trees: int = DEFAULT_LIGHT_TREES,
                   threads: int = 4, seed: int = 20261015,
                   artifact_dir: Path | None = None) -> dict:
    """Train (or explicitly fall back for) one symbol's personal adjustment."""
    history = rw.stock_history_status(
        min(r['decision_date'] for rows in samples_by_key.values() for r in rows),
        max(r['decision_date'] for rows in samples_by_key.values() for r in rows),
        windows) if samples_by_key else {'status': 'INSUFFICIENT_HISTORY',
                                         'reason': 'NO_SAMPLES'}
    record = pm.new_record(symbol, windows=windows,
                           global_release_id=global_release_id,
                           global_bundle_sha256=global_bundle_sha256,
                           data_sha256=data_sha256, history=history)
    record['price_input_contract'] = price_contract
    record['created_at'] = pd.Timestamp.now('UTC').isoformat()

    counts = Counter()
    positive = Counter()
    for key, rows in samples_by_key.items():
        counts[str(key)] = len(rows)
        positive[str(key)] = int(sum(r['label'] for r in rows))
    record['samples'] = {'total': int(sum(counts.values())),
                         'by_submodel': dict(counts),
                         'positive_by_submodel': dict(positive)}

    artifacts: dict = {}
    standalone_models: dict = {}
    modes: dict = {}
    blend_weights: list[float] = []
    insufficient: list[str] = []

    for key in SUBKEYS:
        rows = samples_by_key.get(key, [])
        usable = [r for r in rows if r['sample_id'] in oof_scores]
        if len(usable) < min_adjust_samples:
            modes[str(key)] = {'mode': pm.MODE_FALLBACK,
                               'reason': 'INSUFFICIENT_OOF_SAMPLES',
                               'n': len(rows), 'usable_n': len(usable)}
            insufficient.append(f'{key}:n={len(usable)}')
            continue
        labels = [r['label'] for r in usable]
        if min(Counter(labels).values()) < min_each_class:
            modes[str(key)] = {'mode': pm.MODE_FALLBACK,
                               'reason': 'SINGLE_OR_RARE_CLASS',
                               'n': len(usable),
                               'positive': int(sum(labels))}
            insufficient.append(f'{key}:class')
            continue
        base = np.asarray([oof_scores[r['sample_id']] for r in usable], dtype=float)
        y = np.asarray(labels, dtype=int)

        chosen = pm.MODE_REGULARIZED
        personal_prob = None
        if allow_standalone and len(usable) >= min_standalone_samples:
            from lightgbm import LGBMClassifier
            xt = np.stack([r['features'] for r in usable]).astype(np.float32)
            clf = LGBMClassifier(n_estimators=light_trees, num_leaves=15,
                                 learning_rate=.08, min_child_samples=40,
                                 reg_lambda=5.0, verbosity=-1, n_jobs=threads,
                                 random_state=seed)
            clf.fit(pd.DataFrame(xt, columns=list(fmod.ALL_COLS)), y)
            personal_prob = clf.predict_proba(
                pd.DataFrame(xt, columns=list(fmod.ALL_COLS)))[:, 1]
            chosen = pm.MODE_STANDALONE
            standalone_models[f'{key[0]}:{key[1]}:D{key[2]}'] = clf
        if personal_prob is None:
            from sklearn.linear_model import LogisticRegression
            lr = LogisticRegression(C=1.0, max_iter=2000)
            lr.fit(base.reshape(-1, 1), y)
            personal_prob = lr.predict_proba(base.reshape(-1, 1))[:, 1]
            artifacts[f'{key[0]}:{key[1]}:D{key[2]}'] = {
                'kind': 'logistic', 'coef': float(lr.coef_[0][0]),
                'intercept': float(lr.intercept_[0])}

        # Blend weight chosen on the symbol's own data, bounded so the global
        # model always contributes (no silent full replacement).
        best_w, best_j = 0.0, -1.0
        for w in (0.0, 0.15, 0.3, 0.45, 0.6):
            blended = w * personal_prob + (1 - w) * base
            tau = _youden_threshold(y, blended)
            if tau is None:
                continue
            j = _youden_j(y, blended, tau)
            if j > best_j:
                best_j, best_w = j, w
        blended = best_w * personal_prob + (1 - best_w) * base
        tau = _youden_threshold(y, blended)
        if tau is None:
            modes[str(key)] = {'mode': pm.MODE_FALLBACK,
                               'reason': 'NO_VALID_THRESHOLD', 'n': len(usable)}
            insufficient.append(f'{key}:threshold')
            continue
        blend_weights.append(best_w)
        pred = blended >= tau
        entry = {
            'mode': chosen, 'blend_weight': best_w, 'threshold': tau,
            'n': len(usable), 'positive': int(y.sum()),
            'train_auc': _auc(y, blended), 'train': _confusion(y, pred),
            'reason': None}
        if chosen == pm.MODE_REGULARIZED:
            # The adjustment coefficients travel with the record so inference can
            # rebuild the personal score without any extra artifact.
            entry['coef'] = float(lr.coef_[0][0])
            entry['intercept'] = float(lr.intercept_[0])
        modes[str(key)] = entry

    record['submodels'] = modes
    record['blend_weight'] = (round(float(np.mean(blend_weights)), 6)
                              if blend_weights else 0.0)
    active_modes = {v['mode'] for v in modes.values()}
    if pm.MODE_REGULARIZED in active_modes or pm.MODE_STANDALONE in active_modes:
        record['mode'] = (pm.MODE_STANDALONE if pm.MODE_STANDALONE in active_modes
                          and pm.MODE_REGULARIZED not in active_modes
                          else pm.MODE_REGULARIZED)
        record['fallback_reason'] = None
    else:
        record['mode'] = pm.MODE_FALLBACK
        record['fallback_reason'] = 'INSUFFICIENT_PERSONAL_SAMPLES:' + ';'.join(
            insufficient[:6]) if insufficient else 'INSUFFICIENT_PERSONAL_SAMPLES'
    record['artifact_path'] = None
    if standalone_models and artifact_dir is not None:
        import pickle as _pickle
        artifact_dir = Path(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        # The artifact file is named after the content hash written below, so a
        # record and its weights can never silently drift apart.
        payload = _pickle.dumps(standalone_models, protocol=_pickle.HIGHEST_PROTOCOL)
        record['personal_bundle_sha256'] = hashlib.sha256(payload).hexdigest()
        target = artifact_dir / f"personal_{symbol.replace(':', '_')}_{record['personal_bundle_sha256'][:16]}.pkl"
        target.write_bytes(payload)
        record['artifact_path'] = str(target)
    else:
        record['personal_bundle_sha256'] = (
            hashlib.sha256(json.dumps(artifacts, sort_keys=True,
                                      ensure_ascii=False).encode()).hexdigest()
            if artifacts else None)
    record['metrics'] = {'train': {k: v for k, v in modes.items()
                                   if v.get('mode') != pm.MODE_FALLBACK}}
    record['last_trade_date_processed'] = windows['windows']['test'][1]
    record['last_matured_event_date'] = windows['windows']['test'][1]
    return record


def _youden_j(y, s, tau) -> float:
    pred = np.asarray(s) >= tau
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    tn = int(((pred == 0) & (y == 0)).sum())
    tpr = tp / (tp + fn) if (tp + fn) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    return tpr - fpr


def collect_symbol_samples(all_samples: dict, symbol: str) -> dict:
    """Per-symbol view of the shared training samples."""
    out: dict = defaultdict(list)
    for key, rows in all_samples.items():
        for r in rows:
            if r['symbol'] == symbol:
                out[key].append(r)
    return dict(out)


def run_personal_training(samples_by_split: dict, oof_scores: dict, fmod, *,
                          global_models: dict | None = None,
                          windows: dict, global_release_id: str,
                          global_bundle_sha256: str, data_sha256: str,
                          price_contract: str, symbols: list[str] | None = None,
                          registry_root: Path | None = None,
                          artifact_dir: Path | None = None,
                          allow_standalone: bool = False, **kwargs) -> dict:
    """Train adjustments on TRAIN and select blend/threshold on VAL."""
    if not global_models:
        raise pm.PersonalModelError('PERSONAL_VALIDATION_GLOBAL_MODELS_REQUIRED')
    from .personal_validation import strict_personal_record
    train_samples = samples_by_split.get('train', {})
    available = sorted({r['symbol'] for rows in train_samples.values() for r in rows})
    targets = sorted(symbols) if symbols else available
    registry = pm.load_registry(registry_root)
    results = []
    for symbol in targets:
        own = collect_symbol_samples(train_samples, symbol)
        val = collect_symbol_samples(samples_by_split.get('val', {}), symbol)
        test = collect_symbol_samples(samples_by_split.get('test', {}), symbol)
        record = strict_personal_record(
            symbol, train_samples=own, val_samples=val, test_samples=test,
            global_models=global_models, oof_scores=oof_scores, fmod=fmod,
            windows=windows, global_release_id=global_release_id,
            global_bundle_sha256=global_bundle_sha256, data_sha256=data_sha256,
            price_contract=price_contract,
            allow_standalone=allow_standalone,
            artifact_dir=artifact_dir, **kwargs)
        pm.register(registry, record)
        results.append(record)
    pm.save_registry(registry, registry_root)
    return {'trained_symbols': [r['symbol'] for r in results],
            'records': results, 'registry': str(pm.registry_path(registry_root))}
