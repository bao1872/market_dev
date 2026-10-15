"""Leakage-gated H/L × D1-D3 × L1/L2 training on a shared rolling window.

Outputs immutable *candidate* model artifacts. NEVER changes an active release,
never schedules itself, never updates a previously produced candidate bundle.

Contract:
  * windows come from `rolling_windows` (24/6/3 natural months, one shared dict);
  * a candidate event's D1..D3 must live entirely inside ONE split;
  * a label is only used once it has matured by that split's cutoff bar;
  * Train fits, Validation selects thresholds, Test is the final holdout;
  * personal-model base scores are time-ordered out-of-fold (never in-sample).
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import pickle
import sys

import numpy as np
import pandas as pd

from . import model, personal_model as pm, personal_trainer, producer, source
from . import lab_parity_contract as lab_contract
from . import rolling_windows as rw
from .producer import ProducerError

SUBKEYS = [(layer, kind, stage) for layer in ('L1', 'L2')
           for kind in ('H', 'L') for stage in (1, 2, 3)]
CANDIDATE_BUNDLE = 'two_stage_models_candidate_UNPUBLISHED.pkl'


def _resolve_windows(args) -> dict:
    """Rolling windows by default; explicit flags only for diagnostics."""
    explicit = all(getattr(args, name + '_' + suffix, None)
                   for name in ('train', 'val', 'test')
                   for suffix in ('start', 'end'))
    if getattr(args, 'use_rolling_window', True) or not explicit:
        return rw.rolling_windows(date.fromisoformat(args.as_of))
    windows = rw.rolling_windows(date.fromisoformat(args.as_of))
    dates = {}
    for name in rw.SPLIT_NAMES:
        lo = date.fromisoformat(getattr(args, name + '_start'))
        hi = date.fromisoformat(getattr(args, name + '_end'))
        if lo > hi:
            raise ProducerError('REVERSED_' + name.upper())
        dates[name] = (lo, hi)
    if not (dates['train'][1] < dates['val'][0] and dates['val'][1] < dates['test'][0]):
        raise ProducerError('TRAIN_VAL_TEST_OVERLAP')
    return {'as_of': args.as_of, 'cutoff_month': windows['cutoff_month'],
            'months': windows['months'],
            'windows': {k: (v[0].isoformat(), v[1].isoformat()) for k, v in dates.items()},
            'dates': dates}


def _labels(raw, final, j, kind, cutoff_bar, stage_bar):
    # Backwards-compatible helper. Do not reintroduce pivot-price proxy labels.
    result = lab_contract.labels_for_stage(raw, final, j, kind, stage_bar, cutoff_bar)
    return result.l1, result.l2


def build_dataset(stock_raw, fmod, windows, *, max_symbols: int = 0):
    """Point-in-time dataset: every decision day is featurised on its own
    as-of qfq frame, so a later corporate action cannot leak into training."""
    values: dict = defaultdict(list)
    counters: Counter = Counter()
    per_symbol_history: dict = {}
    seen_ids: set = set()
    for symbol, raw in sorted(stock_raw.items()):
        pit = raw if 'adj_factor' in raw.columns else None
        frame = raw.drop(columns=['adj_factor']) if pit is not None else raw
        days = [pd.Timestamp(x).date() for x in frame.datetime]
        per_symbol_history[symbol] = rw.stock_history_status(
            days[0], days[-1], windows)
        _, snap, _ = fmod.replay(frame)
        candidates = lab_contract.causal_candidates_pit(
            frame, pit, fmod, model._pit_frame)
        split_end_bars = {
            name: next((i for i in range(len(days) - 1, -1, -1)
                        if days[i] <= hi), -1)
            for name, lo, hi in ((n, windows['dates'][n][0], windows['dates'][n][1])
                                 for n in rw.SPLIT_NAMES)}
        oracle_by_split = {
            name: lab_contract.split_oracle_frame(
                frame, pit, fmod, model._pit_frame, end_bar)
            for name, end_bar in split_end_bars.items() if end_bar >= 0
        }
        stage_events = []
        for j, kind, _px in candidates:
            if j + 3 >= len(frame):
                continue
            d1, d3 = days[j + 1], days[j + 3]
            name = rw.split_of(d1, windows)
            # Event isolation: the whole D1..D3 span must sit inside ONE split.
            if name is None or not (windows['dates'][name][0] <= d3
                                    <= windows['dates'][name][1]):
                counters['boundary_dropped'] += 1
                continue
            cutoff = split_end_bars[name]
            if cutoff < 0:
                counters['split_without_bars'] += 1
                continue
            for k in (1, 2, 3):
                t = j + k
                oracle_raw, oracle_final = oracle_by_split[name]
                lab = lab_contract.labels_for_stage(
                    oracle_raw, oracle_final, j, kind, t, cutoff)
                l1, l2 = lab.l1, lab.l2
                counters['L1_REASON_' + lab.l1_reason] += 1
                counters['L2_REASON_' + lab.l2_reason] += 1
                if l1 is None and l2 is None:
                    counters['unresolved_both'] += 1
                    continue
                # Each event stores its OWN label maturity bars. Reusing a shared
                # `lab` from the outer loop would leak the last candidate's
                # maturity onto every sample and defeat the OOF anti-leak gate.
                stage_events.append((
                    j, kind, k, t, name,
                    l1, l2,
                    lab.l1_available_bar,
                    lab.l2_available_bar,
                ))
        if not stage_events:
            continue
        bars = sorted({s[3] for s in stage_events})
        if pit is not None:
            _, matrix = model._pit_features(frame, pit, fmod, snap, bars)
        else:
            _, matrix = fmod.features(frame, snap, bars)
        look = {bar: matrix[i] for i, bar in enumerate(bars)}
        for (j, kind, k, t, name,
             l1, l2, l1_bar, l2_bar) in stage_events:
            cutoff = split_end_bars[name]
            for layer, label, mat_bar in (('L1', l1, l1_bar),
                                          ('L2', l2, l2_bar)):
                if label is None:
                    continue
                if mat_bar is None:
                    raise ProducerError('MATURED_LABEL_WITHOUT_AVAILABLE_AT')
                # A label is only trustworthy once it has definitively matured
                # inside this split. A later maturity would let future information
                # into the OOF prior rows and is a contract violation.
                if mat_bar > cutoff:
                    raise ProducerError('MATURED_LABEL_AFTER_SPLIT_CUTOFF')
                sample_id = f'{symbol}|{days[j].isoformat()}|{kind}|D{k}|{layer}'
                if sample_id in seen_ids:
                    counters['duplicate_dropped'] += 1
                    continue
                seen_ids.add(sample_id)
                values[(name, layer, kind, k)].append({
                    'features': np.asarray(look[t], dtype=np.float32),
                    'label': int(label), 'sample_id': sample_id,
                    'symbol': symbol, 'decision_date': days[t],
                    'anchor_date': days[j],
                    'label_available_at': days[mat_bar]})
                counters[f'{name}_{layer}_{kind}_D{k}'] += 1
    return dict(values), dict(counters), per_symbol_history


def _fit_submodel(rows, fmod, *, trees, threads, seed):
    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_curve, roc_auc_score

    def unpack(items):
        return (np.stack([r['features'] for r in items]).astype(np.float32),
                np.asarray([r['label'] for r in items], dtype=int))

    xt, yt = unpack(rows['train'])
    xv, yv = unpack(rows['val'])
    xe, ye = unpack(rows['test'])
    clf = LGBMClassifier(n_estimators=trees, num_leaves=31, learning_rate=.045,
                         min_child_samples=80, max_depth=-1, reg_lambda=5.0,
                         verbosity=-1, n_jobs=threads, random_state=seed)
    clf.fit(pd.DataFrame(xt, columns=list(fmod.ALL_COLS)), yt)
    val = clf.predict_proba(pd.DataFrame(xv, columns=list(fmod.ALL_COLS)))[:, 1]
    fpr, tpr, thr = roc_curve(yv, val)
    finite = np.isfinite(thr)
    if not finite.any():
        raise ProducerError('INVALID_VAL_THRESHOLD')
    j = np.where(finite)[0][int(np.argmax((tpr - fpr)[finite]))]
    tau = float(np.clip(thr[j], 1e-6, 1 - 1e-6))
    if not np.isfinite(tau) or not (0 <= tau <= 1):
        raise ProducerError('INVALID_VAL_THRESHOLD')
    metrics = {}
    for name, X, y in (('TRAIN', xt, yt), ('VAL', xv, yv), ('TEST', xe, ye)):
        score = clf.predict_proba(pd.DataFrame(X, columns=list(fmod.ALL_COLS)))[:, 1]
        pred = score >= tau
        tp = int(((pred == 1) & (y == 1)).sum())
        tn = int(((pred == 0) & (y == 0)).sum())
        fp = int(((pred == 1) & (y == 0)).sum())
        fn = int(((pred == 0) & (y == 1)).sum())
        metrics[f'{name}'] = {
            'n': int(len(y)), 'positive': int(y.sum()), 'tp': tp, 'fp': fp,
            'tn': tn, 'fn': fn,
            'auc': float(roc_auc_score(y, score)) if len(set(y.tolist())) == 2 else None,
            'precision': (tp / (tp + fp)) if (tp + fp) else None,
            'recall': (tp / (tp + fn)) if (tp + fn) else None,
            'alerts': int(pred.sum()),
        }
    return clf, tau, metrics


def train(args):
    windows = _resolve_windows(args)
    if args.max_symbols and not args.allow_partial_diagnostics:
        raise ProducerError('PARTIAL_TRAIN_REQUIRES_EXPLICIT_DIAGNOSTIC_FLAG')
    assets = model.validate_source_assets(args.assets)
    fmod = model.load_engine(args.assets)

    if args.source_mode == 'canonical':
        from .db_export import export_dataset
        import asyncio
        export_dir = Path(args.export_dir).resolve()
        export_dir.mkdir(parents=True, exist_ok=True)
        asyncio.run(export_dataset(export_dir, date.fromisoformat(args.as_of)))
        stock_raw, src = source.load_canonical_qfq_export(
            export_dir, args.as_of, max_symbols=args.max_symbols)
    else:
        stock_raw, src = source.load_official_parquet_pit(
            args.bars, args.instruments, args.as_of, max_symbols=args.max_symbols)
        src['production_fallback_allowed'] = False

    all_dates = sorted({pd.Timestamp(x).date()
                        for df in stock_raw.values() for x in df.datetime})
    test_assessment = rw.assess_test_window(all_dates, windows)

    data, counters, per_symbol_history = build_dataset(
        stock_raw, fmod, windows, max_symbols=args.max_symbols)

    by_split: dict = defaultdict(dict)
    for (name, layer, kind, stage), rows in data.items():
        by_split[name][(layer, kind, stage)] = rows

    model_map, thresholds, metrics = {}, {}, {}
    insufficient = []
    for key in SUBKEYS:
        rows = {split: by_split.get(split, {}).get(key, []) for split in rw.SPLIT_NAMES}
        label = f'{key[0]}:{key[1]}:D{key[2]}'
        if min(len(rows[s]) for s in rw.SPLIT_NAMES) < args.min_samples:
            insufficient.append(f'{label}:{",".join(str(len(rows[s])) for s in rw.SPLIT_NAMES)}')
            continue
        if any(min(Counter(r['label'] for r in rows[s]).values()) < args.min_each_class
               for s in rw.SPLIT_NAMES):
            insufficient.append(f'{label}:CLASS')
            continue
        clf, tau, m = _fit_submodel(rows, fmod, trees=args.trees,
                                    threads=args.threads, seed=args.seed)
        model_map[key] = clf
        thresholds[key] = tau
        for split, vals in m.items():
            metrics[f'{label}:{split}'] = vals
    if len(model_map) != 12:
        raise ProducerError('INCOMPLETE_12_MODELS_NOT_RELEASABLE: ' + ';'.join(insufficient[:8]))

    if not args.skip_personal:
        # The raw frames are no longer needed once the dataset is built.
        stock_raw = {}
        import gc as _gc
        _gc.collect()
    oof_scores, oof_provenance = ({}, {}) if args.skip_personal else (
        personal_trainer.compute_oof_base_scores(
            by_split.get('train', {}), fmod, folds=args.oof_folds,
            trees=args.oof_trees, threads=args.threads, seed=args.seed))

    bundle = {
        'schema': 'PANJI_TWO_STAGE_FINAL_D123_V1',
        'version': EXPECTED_VERSION,
        'feature_sha': model.ENGINE_SHA, 'feature_dim': 131,
        'layers': ['L1', 'L2'], 'kinds': ['H', 'L'], 'stages': [1, 2, 3],
        'models': model_map, 'thresholds': thresholds,
        'uncalibrated': True, 'target': {'L': 'HH', 'H': 'LL'},
        'label_contract': 'L_HH__H_LL__V1',
        'price_input_contract': producer.PRICE_INPUT_CONTRACT,
        'training_kind': 'GLOBAL_ROLLING_WINDOW',
        'split_windows': [(n, windows['windows'][n][0], windows['windows'][n][1])
                          for n in rw.SPLIT_NAMES],
        'oof_base_scores': oof_provenance,
    }

    output = Path(args.output_dir).resolve()
    if output.exists() and list(output.iterdir()):
        raise ProducerError('CANDIDATE_MODEL_DIR_NOT_EMPTY')
    output.mkdir(parents=True, exist_ok=True)
    file = output / CANDIDATE_BUNDLE
    with file.open('xb') as f:
        pickle.dump(bundle, f, pickle.HIGHEST_PROTOCOL)
    sha = source.sha_file(file)

    global_release_id = producer.release_id_for(sha)
    data_sha = hashlib.sha256(json.dumps(
        {'source_sha256': src['source_sha256'],
         'windows': windows['windows'],
         'counts': counters}, sort_keys=True).encode()).hexdigest()

    personal_summary = {'skipped': True}
    if not args.skip_personal:
        personal_summary = personal_trainer.run_personal_training(
            by_split, oof_scores, fmod, windows=windows,
            global_release_id=global_release_id, global_bundle_sha256=sha,
            data_sha256=data_sha, price_contract=producer.PRICE_INPUT_CONTRACT,
            symbols=args.personal_symbols or None,
            global_models=model_map,
            registry_root=Path(args.personal_registry) if args.personal_registry else None,
            artifact_dir=(Path(args.personal_artifact_dir)
                          if getattr(args, 'personal_artifact_dir', None) else None),
            allow_standalone=args.personal_standalone,
            min_adjust_samples=args.personal_min_samples,
            min_each_class=getattr(args, 'personal_min_each_class',
                                   personal_trainer.DEFAULT_MIN_EACH_CLASS))

    report = {
        'status': 'CANDIDATE_TRAINED_NOT_PUBLISHED',
        'new_bundle_sha256': sha,
        'global_release_id': global_release_id,
        'training_source_sha256': src['source_sha256'],
        'training_data_sha256': data_sha,
        'rolling_windows': {'as_of': windows['as_of'],
                            'cutoff_month': windows['cutoff_month'],
                            'months': windows['months'],
                            'windows': windows['windows']},
        'test_window_assessment': test_assessment,
        'metrics': metrics,
        'insufficient_submodels': insufficient,
        'counts': counters,
        'symbol_history': {
            'complete_24m': sum(1 for v in per_symbol_history.values()
                                if v['status'] == 'COMPLETE_24M_HISTORY'),
            'partial': sum(1 for v in per_symbol_history.values()
                           if v['status'] == 'PARTIAL_HISTORY'),
            'insufficient': sum(1 for v in per_symbol_history.values()
                                if v['status'] == 'INSUFFICIENT_HISTORY'),
            'insufficient_symbols': [s for s, v in per_symbol_history.items()
                                     if v['status'] == 'INSUFFICIENT_HISTORY'],
        },
        'released': False, 'active': False,
        'feature_sha': model.ENGINE_SHA,
        'price_adjustment': src.get('price_adjustment'),
        'adjustment_mode': src.get('adjustment_mode'),
        'adjustment_as_of': src.get('adjustment_as_of'),
        'factor_provenance_authoritative': src.get('factor_provenance_authoritative', False),
        'pit_factor_series_available': src.get('pit_factor_series_available', False),
        'oof_base_scores': oof_provenance,
        'personal_models': {
            'trained': len(personal_summary.get('trained_symbols', [])),
            'regularized': sum(1 for r in personal_summary.get('records', [])
                               if r['mode'] == pm.MODE_REGULARIZED),
            'standalone': sum(1 for r in personal_summary.get('records', [])
                              if r['mode'] == pm.MODE_STANDALONE),
            'fallback': sum(1 for r in personal_summary.get('records', [])
                            if r['mode'] == pm.MODE_FALLBACK),
            'fallback_reasons': {r['symbol']: r['fallback_reason']
                                 for r in personal_summary.get('records', [])
                                 if r.get('mode') == pm.MODE_FALLBACK},
            'registry': personal_summary.get('registry'),
            'skipped': personal_summary.get('skipped', False),
        },
    }
    (output / 'train_report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'sha256': sha,
                      'global_release_id': global_release_id,
                      'dir': str(output),
                      'test_window_assessment': test_assessment['status'],
                      'personal_models': report['personal_models']['trained']},
                     ensure_ascii=False))


EXPECTED_VERSION = 'panji-two-stage-final-d123-1.0'


def main():
    p = argparse.ArgumentParser(description='Rolling-window strategy trainer (no deploy)')
    p.add_argument('--assets', type=Path, default=model.ASSET_DIR)
    p.add_argument('--bars', type=Path, required=True)
    p.add_argument('--instruments', type=Path, required=True)
    p.add_argument('--as-of', required=True, help='Run date; drives the rolling window')
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--source-mode', choices=['canonical', 'diagnostic'],
                   default='diagnostic')
    p.add_argument('--export-dir', type=Path, default=Path('/tmp/panji_strategy_train_export'))
    p.add_argument('--max-symbols', type=int, default=0)
    p.add_argument('--allow-partial-diagnostics', action='store_true')
    p.add_argument('--min-samples', type=int, default=200)
    p.add_argument('--min-each-class', type=int, default=40)
    p.add_argument('--trees', type=int, default=300)
    p.add_argument('--threads', type=int, default=4)
    p.add_argument('--seed', type=int, default=20261015)
    p.add_argument('--skip-personal', action='store_true')
    p.add_argument('--personal-standalone', action='store_true')
    p.add_argument('--personal-min-samples', type=int,
                   default=personal_trainer.DEFAULT_MIN_ADJUST_SAMPLES)
    p.add_argument('--personal-symbols', nargs='*', default=[])
    p.add_argument('--personal-registry', type=Path, default=None)
    p.add_argument('--oof-folds', type=int,
                   default=personal_trainer.DEFAULT_OOF_FOLDS)
    p.add_argument('--oof-trees', type=int,
                   default=personal_trainer.DEFAULT_OOF_TREES)
    p.add_argument('--use-rolling-window', action='store_true', default=True)
    for name in ('train', 'val', 'test'):
        for suffix in ('start', 'end'):
            p.add_argument('--' + name + '-' + suffix, default=None)
    args = p.parse_args()
    try:
        train(args)
    except (ProducerError, model.AssetError, ValueError) as exc:
        print('STOP_TRAIN', str(exc), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
