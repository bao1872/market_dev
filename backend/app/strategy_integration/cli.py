"""Explicit offline build / train / evaluate CLI; nothing is deployed here.

No worker, scheduler, HTTP handler or production DB write is started at import
or at startup. Every command writes only to operator-supplied paths OUTSIDE the
repository, and never activates a model.
"""
from __future__ import annotations
from collections import Counter

import argparse
import asyncio
import hashlib
import json
import os
from datetime import date
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from . import model, personal_model as pm, personal_registry, producer, source
from . import rolling_windows as rw
from .release_store import resolve_active_release


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    fd, name = tempfile.mkstemp(prefix='.panji-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def _inside(path: Path, repo: str | None) -> bool:
    if not repo:
        return False
    root = Path(repo).resolve()
    return path == root or (root in path.parents)


def _ensure_outside(path: Path, repo: str | None, label: str) -> Path:
    resolved = path.resolve()
    if _inside(resolved, repo):
        raise producer.ProducerError(f'{label}_INSIDE_REPOSITORY_FORBIDDEN')
    if resolved == Path('/') or resolved == Path('/tmp'):
        raise producer.ProducerError(f'{label}_TOO_BROAD')
    return resolved


def load_bundle_for_run(root: Path, assets: Path) -> tuple[dict, dict, str, str]:
    """Resolve the weights to score with: active release wins, else frozen assets."""
    active = resolve_active_release(root)
    if active and active.get('bundle_path'):
        path = Path(active['bundle_path'])
        bundle, facts = model.load_bundle_file(
            path, pinned_sha=active.get('bundle_sha256') or None)
        return bundle, facts, str(path), 'ACTIVE_RELEASE'
    bundle, facts = model.load_bundle(assets)
    return bundle, facts, str(Path(assets) / 'two_stage_models_local_only.pkl'), 'FROZEN_ASSETS'


def _canonical_source_report(report: dict) -> dict:
    """Translate the MDAS report into the source-report contract build/producer
    consume: a tamper-evident ``source_sha256`` plus coverage/authority fields.
    """
    provenance = report.get('provenance', {})
    source_sha256 = hashlib.sha256(json.dumps(
        {s: {'b': h.get('source_bar_hash'), 'f': h.get('adj_factor_hash')}
         for s, h in provenance.items()}, sort_keys=True).encode()).hexdigest()
    return {
        'source_sha256': source_sha256,
        'eligible_stock_count': report['eligible_stock_count'],
        'coverage_ratio': report['coverage_ratio_of_eligible'],
        'pit_factor_series_available': report.get('pit_factor_series_available', False),
        'price_adjustment': report.get('price_adjustment', 'qfq'),
        'adjustment_mode': 'CANONICAL_MDAS_QFQ',
        'adjustment_as_of': report.get('adjustment_as_of'),
        'factor_provenance_authoritative': True,
    }


def _load_market(args, repo: str | None):
    """Return (frames, pit_source, source_report) for the requested entrance."""
    if args.source_mode == 'canonical':
        # Authoritative path: read QFQ bars + factor series straight from MDAS.
        # No intermediate Parquet export (bars_daily_qfq.parquet / input_manifest)
        # or OFFLINE_FACTOR_DIAGNOSTIC fallback is required or allowed; the MDAS
        # ownership contract is unchanged.
        from .db_export import read_official_qfq_stock_batch
        async def _read():
            return await read_official_qfq_stock_batch(
                date.fromisoformat(args.as_of), batch_size=args.max_symbols or 80)
        stock_raw, mdas_report = asyncio.run(_read())
        if not mdas_report.get('pit_factor_series_available'):
            raise producer.ProducerError('CANONICAL_PIT_FACTOR_SERIES_UNAVAILABLE')
        pit = dict(stock_raw)
        frames = {s: df.drop(columns=['adj_factor']) for s, df in stock_raw.items()}
        return frames, pit, _canonical_source_report(mdas_report)
    frames, report = source.load_official_parquet_pit(
        args.bars, args.instruments, args.as_of, max_symbols=args.max_symbols)
    report['production_fallback_allowed'] = False
    pit = {s: df for s, df in frames.items()}
    frames = {s: df.drop(columns=['adj_factor']) for s, df in frames.items()}
    return frames, pit, report


def _resolve_personal(registry: dict, symbols: list[str], *, global_release_id: str,
                      price_contract: str, required: bool) -> tuple[dict, list]:
    """Combine each symbol's ACTIVE personal model with the global model."""
    combined, problems = {}, []
    for symbol in symbols:
        record = pm.resolve_active(registry, symbol)
        if record is None:
            if required:
                problems.append({'symbol': symbol, 'reason': 'NO_ACTIVE_PERSONAL_MODEL'})
            continue
        try:
            pm.check_input_compatibility(
                record, expected_price_contract=price_contract,
                global_release_id=global_release_id)
        except pm.PersonalModelError as exc:
            problems.append({'symbol': symbol, 'reason': str(exc)})
            continue
        entry = {'record': record}
        if record.get('artifact_path'):
            entry['artifacts'] = model.load_personal_artifacts(record)
        combined[symbol] = entry
    return combined, problems


# --------------------------------------------------------------------------- build
def build(args):
    repo = os.environ.get('PANJI_STRATEGY_CODE_ROOT')
    out = _ensure_outside(args.output_dir, repo, 'OUTPUT_DIR')
    date.fromisoformat(args.as_of)
    windows = rw.rolling_windows(date.fromisoformat(args.as_of))

    assets = model.validate_source_assets(args.assets)
    fmod = model.load_engine(args.assets)
    bundle, facts, bundle_path, bundle_origin = load_bundle_for_run(out, args.assets)
    frames, pit_source, source_report = _load_market(args, repo)

    if args.max_symbols == 0:
        if source_report['eligible_stock_count'] < 1000:
            raise producer.ProducerError('FULL_UNIVERSE_COVERAGE_BELOW_1000')
        if source_report['coverage_ratio'] < 0.85:
            raise producer.ProducerError('SOURCE_TRADE_DAY_COVERAGE_BELOW_85_PERCENT')

    release_id = producer.release_id_for(facts['bundle_sha256'])
    bundle_contract = producer.require_price_contract(
        bundle, producer.PRICE_INPUT_CONTRACT)

    personal_by_symbol: dict = {}
    personal_problems: list = []
    if args.personal_registry:
        registry = pm.load_registry(args.personal_registry)
        personal_by_symbol, personal_problems = _resolve_personal(
            registry, sorted(frames), global_release_id=release_id,
            price_contract=bundle_contract, required=False)

    candidates, evaluation = producer.make_snapshots(
        frames, fmod, bundle, as_of=args.as_of,
        source_sha=source_report['source_sha256'],
        chart_bars=args.chart_bars, release_id=release_id,
        price_adjustment=source_report.get('price_adjustment', 'qfq'),
        adjustment_mode=source_report.get('adjustment_mode'),
        bundle_sha256=facts['bundle_sha256'],
        pit_source=pit_source, personal_by_symbol=personal_by_symbol,
    )
    validation = producer.verify_against_readers(candidates, evaluation)
    evaluation_data = producer.encode_json(evaluation)
    if len(evaluation_data) > 96 * 1024 * 1024:
        raise producer.ProducerError('EVALUATION_EXCEEDS_EXISTING_READER_LIMIT')

    root = out / (args.as_of + '_' + release_id[:16])
    if root.exists():
        old = root / 'manifest.json'
        if not old.is_file():
            raise producer.ProducerError('STAGING_DEST_EXISTS_WITHOUT_MANIFEST')
        prior = json.loads(old.read_text())
        if prior.get('source_sha256') != source_report['source_sha256']:
            raise producer.ProducerError('SAME_RUN_NAME_CHANGED_INPUT_FORBIDDEN')
        print(json.dumps({'status': 'IDEMPOTENT_EXISTS', 'root': str(root)},
                         ensure_ascii=False))
        return None

    root.mkdir(parents=True, exist_ok=False)
    from app.services.strategy_candidate_shards import write_candidate_shards
    from app.services.strategy_candidate_snapshot import validate as validate_candidate
    shard_manifest = write_candidate_shards(
        candidates, root, validate=validate_candidate, target_bytes=args.shard_bytes)
    atomic_bytes(root / 'evaluation_snapshot.json', evaluation_data)
    descriptor = (root / 'candidate_snapshot.json').read_bytes()

    observation_total = sum(
        len(c['observations']) for st in candidates['stocks'].values()
        for c in st['candidates'])
    candidate_total = sum(len(st['candidates']) for st in candidates['stocks'].values())
    manifest = {
        'status': 'VALIDATED_STAGING_NOT_DEPLOYED',
        'release_id': release_id,
        'as_of': args.as_of,
        'rolling_windows': {'as_of': windows['as_of'],
                            'cutoff_month': windows['cutoff_month'],
                            'windows': windows['windows']},
        'source_sha256': source_report['source_sha256'],
        'candidate_sha256': hashlib.sha256(descriptor).hexdigest(),
        'evaluation_sha256': hashlib.sha256(evaluation_data).hexdigest(),
        'candidate_shards': shard_manifest['shards'],
        'candidate_total_bytes': shard_manifest['total_bytes'],
        'candidate_stocks': shard_manifest['stock_count'],
        'candidate_count': candidate_total,
        'observation_count': observation_total,
        'source': source_report,
        'validation': validation,
        'score_calibrated': False,
        'training_performed': False,
        'production_published': False,
        'assets': assets,
        'bundle_path': bundle_path,
        'bundle_origin': bundle_origin,
        'runtime': facts,
        'price_adjustment': source_report.get('price_adjustment'),
        'adjustment_mode': source_report.get('adjustment_mode'),
        'adjustment_as_of': source_report.get('adjustment_as_of'),
        'factor_provenance_authoritative': source_report.get(
            'factor_provenance_authoritative', False),
        'personal_models': {
            'symbols_with_active_personal': sorted(personal_by_symbol),
            'count': len(personal_by_symbol),
            'problems': personal_problems,
        },
    }
    atomic_bytes(root / 'manifest.json', producer.encode_json(manifest))
    print(json.dumps({
        'status': 'VALIDATED_STAGING_NOT_DEPLOYED', 'output': str(root),
        'validation': validation, 'source': source_report,
        'shards': len(shard_manifest['shards']),
        'candidate_total_bytes': shard_manifest['total_bytes'],
        'source_mode': args.source_mode,
        'bundle_path': bundle_path, 'bundle_origin': bundle_origin,
        'bundle_sha256': facts['bundle_sha256'],
        'personal_models': len(personal_by_symbol),
        'rolling_windows': windows['windows'],
    }, ensure_ascii=False))
    return root


# ------------------------------------------------------------------ train-global
def train_global(args):
    from . import training
    args.min_samples = args.min_samples
    args.use_rolling_window = True
    training.train(args)


# --------------------------------------------------------------- train-personal
def train_personal_cmd(args):
    """Train personal models for the requested symbols from a trained global."""
    from . import personal_trainer, training
    windows = rw.rolling_windows(date.fromisoformat(args.as_of))
    fmod = model.load_engine(args.assets)
    frames, pit, report = _load_market(args, os.environ.get('PANJI_STRATEGY_CODE_ROOT'))
    data, _counters, _hist = training.build_dataset(pit, fmod, windows)
    by_split: dict = {}
    for (name, layer, kind, stage), rows in data.items():
        by_split.setdefault(name, {})[(layer, kind, stage)] = rows
    oof, oof_prov = personal_trainer.compute_oof_base_scores(
        by_split.get('train', {}), fmod, folds=args.oof_folds,
        trees=args.oof_trees, threads=args.threads, seed=args.seed)
    if args.global_bundle is None:
        raise pm.PersonalModelError('PERSONAL_REQUIRES_PINNED_GLOBAL_BUNDLE')
    global_bundle, global_facts = model.load_bundle_file(
        args.global_bundle, pinned_sha=args.global_bundle_sha256)
    if producer.release_id_for(global_facts['bundle_sha256']) != args.global_release_id:
        raise pm.PersonalModelError('PERSONAL_GLOBAL_RELEASE_ID_MISMATCH')
    summary = personal_trainer.run_personal_training(
        by_split, oof, fmod, global_models=global_bundle['models'], windows=windows,
        global_release_id=args.global_release_id,
        global_bundle_sha256=args.global_bundle_sha256,
        data_sha256=report['source_sha256'],
        price_contract=producer.PRICE_INPUT_CONTRACT,
        symbols=args.personal_symbols or None,
        registry_root=args.personal_registry,
        artifact_dir=args.personal_artifact_dir,
        allow_standalone=args.personal_standalone,
        min_adjust_samples=args.personal_min_samples,
        min_each_class=args.personal_min_each_class)
    print(json.dumps({
        'status': 'PERSONAL_CANDIDATES_TRAINED_NOT_PUBLISHED',
        'trained': len(summary['trained_symbols']),
        'regularized': sum(1 for r in summary['records']
                           if r['mode'] == pm.MODE_REGULARIZED),
        'standalone': sum(1 for r in summary['records']
                          if r['mode'] == pm.MODE_STANDALONE),
        'fallback': sum(1 for r in summary['records']
                        if r['mode'] == pm.MODE_FALLBACK),
        'registry': summary['registry'],
        'oof_base_scores': oof_prov['scored_samples'],
        'oof_unscored': oof_prov['unscored_samples'],
    }, ensure_ascii=False))


# ------------------------------------------------------------------- incremental
def incremental_cmd(args):
    """Record per-symbol incremental state and emit LIVE_REFIT candidates."""
    from . import incremental_training as inc
    from . import training
    windows = rw.rolling_windows(date.fromisoformat(args.as_of))
    inc.check_window_isolation(windows)
    fmod = model.load_engine(args.assets)
    frames, pit, report = _load_market(args, os.environ.get('PANJI_STRATEGY_CODE_ROOT'))
    state = inc.load_state(args.incremental_state)
    registry = pm.load_registry(args.personal_registry)
    all_dates = sorted({pd.Timestamp(x).date()
                        for df in frames.values() for x in df.datetime})
    plans, refits, skipped = [], [], []
    for symbol in sorted(frames):
        dates = [pd.Timestamp(x).date() for x in frames[symbol].datetime]
        plan = inc.plan_incremental(state, symbol, dates)
        plans.append(plan)
        if plan['action'] == 'NO_CHANGE':
            skipped.append(symbol)
            continue
        own = {symbol: pit[symbol]}
        data, _c, _h = training.build_dataset(own, fmod, windows)
        record = pm.resolve_active(registry, symbol)
        if record is None or record.get('mode') == pm.MODE_FALLBACK:
            # No refittable personal model: record why and move on, never invent.
            skipped.append({'symbol': symbol,
                            'reason': ('NO_ACTIVE_PERSONAL_MODEL' if record is None
                                       else 'PERSONAL_FALLBACK_NOT_REFITTABLE:' +
                                       str(record.get('fallback_reason')))})
            continue
        refit = inc.build_live_refit_candidate(
            symbol=symbol, record=record, windows=windows,
            global_release_id=args.global_release_id,
            data_sha256=report['source_sha256'],
            test_assessment=rw.assess_test_window(all_dates, windows))
        inc.update_symbol_state(
            state, symbol, last_trade_date=dates[-1],
            last_matured_event_date=dates[-1],
            training_data_sha256=report['source_sha256'],
            model_version=refit['model_version'], windows=windows)
        refits.append({'symbol': symbol, 'model_version': refit['model_version'],
                       'independent_test_status': refit['independent_test_status'],
                       'independent_test_claim': refit['independent_test_claim']})
    inc.record_run(state, kind='INCREMENTAL', as_of=args.as_of,
                   symbols=[p['symbol'] for p in plans],
                   detail={'refits': len(refits), 'skipped': len(skipped)})
    inc.save_state(state, args.incremental_state)
    print(json.dumps({
        'status': 'INCREMENTAL_STATE_UPDATED_NOT_PUBLISHED',
        'symbols': len(plans), 'refit_candidates': len(refits),
        'unchanged_skipped': len(skipped),
        'fallback_skipped': skipped,
        'live_refit_kind': inc.LIVE_REFIT_KIND,
        'plans': plans[:5], 'refits': refits[:5],
        'test_window_assessment': rw.assess_test_window(all_dates, windows)['status'],
    }, ensure_ascii=False))


# ---------------------------------------------------------------------- evaluate
def _metrics_block(y, s, tau) -> dict:
    y = np.asarray(y, dtype=int)
    s = np.asarray(s, dtype=float)
    pred = s >= tau
    tp = int(((pred == 1) & (y == 1)).sum()); fp = int(((pred == 1) & (y == 0)).sum())
    tn = int(((pred == 0) & (y == 0)).sum()); fn = int(((pred == 0) & (y == 1)).sum())
    auc = None
    reasons = []
    if len(set(y.tolist())) < 2:
        reasons.append('AUC_NULL_SINGLE_CLASS_IN_TEST_SAMPLES')
    else:
        from sklearn.metrics import roc_auc_score
        auc = float(roc_auc_score(y, s))
    if not (tp + fp):
        reasons.append('PRECISION_NULL_NO_PREDICTED_POSITIVE')
    if not (tp + fn):
        reasons.append('RECALL_NULL_NO_ACTUAL_POSITIVE')
    return {'n': int(len(y)), 'positive': int(y.sum()), 'negative': int((y == 0).sum()),
            'auc': auc,
            'precision': (tp / (tp + fp)) if (tp + fp) else None,
            'recall': (tp / (tp + fn)) if (tp + fn) else None,
            'tp': tp, 'fp': fp, 'tn': tn, 'fn': fn,
            'alerts': int(pred.sum()), 'threshold': float(tau),
            'null_reasons': reasons}


def evaluate_cmd(args):
    """Independent test-window evaluation of global / personal / combined."""
    from . import training
    windows = rw.rolling_windows(date.fromisoformat(args.as_of))
    fmod = model.load_engine(args.assets)
    frames, pit, report = _load_market(args, os.environ.get('PANJI_STRATEGY_CODE_ROOT'))
    all_dates = sorted({pd.Timestamp(x).date()
                        for df in frames.values() for x in df.datetime})
    assessment = rw.assess_test_window(all_dates, windows)

    bundle, facts = model.load_bundle_file(Path(args.bundle))
    global_release_id = producer.release_id_for(facts['bundle_sha256'])
    registry = pm.load_registry(args.personal_registry) if args.personal_registry \
        else {'symbols': {}, 'history': {}}

    data, counters, history = training.build_dataset(pit, fmod, windows)
    by_split: dict = {}
    for (name, layer, kind, stage), rows in data.items():
        by_split.setdefault(name, {})[(layer, kind, stage)] = rows

    symbols = sorted(frames) if not args.symbols else list(args.symbols)
    per_symbol: dict = {}
    aggregate = {'GLOBAL': [], 'PERSONAL': [], 'COMBINED': []}
    unresolved = 0
    path_stats = []
    for symbol in symbols:
        raw = frames[symbol]
        own = {symbol: pit[symbol]}
        # Rebuild this symbol's test-window samples with true as-of PIT bars.
        sym_data, _c, _h = training.build_dataset(own, fmod, windows)
        test_rows = {key[1:]: rows for key, rows in sym_data.items()
                     if key[0] == 'test'}
        record = pm.resolve_active(registry, symbol)
        entry = {'symbol': symbol,
                 'history_status': history.get(symbol, {}).get('status'),
                 'personal_mode': (record or {}).get('mode'),
                 'submodels': {}}
        for key, rows in sorted(test_rows.items()):
            if not rows:
                continue
            X = np.stack([r['features'] for r in rows]).astype(np.float32)
            y = np.asarray([r['label'] for r in rows], dtype=int)
            clf = bundle['models'].get(key)
            if clf is None:
                continue
            g = clf.predict_proba(pd.DataFrame(X, columns=list(fmod.ALL_COLS)))[:, 1]
            tau_g = float(bundle['thresholds'][key])
            gm = _metrics_block(y, g, tau_g)
            entry['submodels'][f'{key[0]}:{key[1]}:D{key[2]}'] = {'GLOBAL': gm}
            aggregate['GLOBAL'].append((y, g))
            if record is not None:
                adj, tau_p, _mode = model.personal_adjusted_score(
                    key, 0.0, record, None)
                sub = (record.get('submodels') or {}).get(str(key))
                if sub and sub.get('mode') == pm.MODE_REGULARIZED:
                    coef, intercept = sub['coef'], sub['intercept']
                    p = 1.0 / (1.0 + np.exp(-(coef * g + intercept)))
                    w = float(sub.get('blend_weight', 0.0))
                    blended = w * p + (1 - w) * g
                    pm_block = _metrics_block(y, blended, float(sub['threshold']))
                    entry['submodels'][f'{key[0]}:{key[1]}:D{key[2]}']['PERSONAL'] = pm_block
                    entry['submodels'][f'{key[0]}:{key[1]}:D{key[2]}']['COMBINED'] = pm_block
                    aggregate['PERSONAL'].append((y, blended))
                    aggregate['COMBINED'].append((y, blended))
                    delta = ((pm_block['auc'] - gm['auc'])
                             if (pm_block['auc'] is not None and gm['auc'] is not None)
                             else None)
                    entry['submodels'][f'{key[0]}:{key[1]}:D{key[2]}']['auc_delta_vs_global'] = delta
        # Alert path metrics on the full causal candidate set inside TEST.
        try:
            final, _snap, _ = fmod.replay(raw)
            idx = {str(pd.Timestamp(x).date()): i
                   for i, x in enumerate(pd.to_datetime(raw.datetime))}
            test_lo, test_hi = windows['dates']['test']
            outcomes = Counter()
            mfe, mae, mdd = [], [], []
            for key, rows in test_rows.items():
                clf = bundle['models'].get(key)
                if clf is None:
                    continue
                X = np.stack([r['features'] for r in rows]).astype(np.float32)
                g = clf.predict_proba(
                    pd.DataFrame(X, columns=list(fmod.ALL_COLS)))[:, 1]
                sub = (record.get('submodels') or {}).get(str(key))
                if sub and sub.get('mode') == pm.MODE_REGULARIZED:
                    z = sub['coef'] * g + sub['intercept']
                    pv = 1.0 / (1.0 + np.exp(-z))
                    w = float(sub.get('blend_weight', 0.0))
                    g = w * pv + (1 - w) * g
                    tau = float(sub['threshold'])
                else:
                    tau = float(bundle['thresholds'][key])
                kind = key[1]
                for r, score, alert in zip(rows, g, g >= tau):
                    if not (test_lo <= r['decision_date'] <= test_hi):
                        continue
                    if not alert:
                        continue
                    j = idx[r['anchor_date'].isoformat()]
                    t = idx[r['decision_date'].isoformat()]
                    outcome = producer._outcome(raw, final, j, kind, t)
                    outcomes[outcome] += 1
                    if outcome != 'UNRESOLVED':
                        m = producer._path_metrics(raw, kind, t)
                        mfe.append(m['mfe_pct']); mae.append(m['mae_pct'])
                        mdd.append(m['mdd_pct'])
            entry['alerts'] = {
                'total': int(sum(outcomes.values())),
                'success': int(outcomes.get('SUCCESS', 0)),
                'fail': int(outcomes.get('FAIL', 0)),
                'unresolved': int(outcomes.get('UNRESOLVED', 0)),
                'mfe_pct_mean': round(float(np.mean(mfe)), 6) if mfe else None,
                'mae_pct_mean': round(float(np.mean(mae)), 6) if mae else None,
                'mdd_pct_mean': round(float(np.mean(mdd)), 6) if mdd else None,
                'mfe_pct_max': round(float(np.max(mfe)), 6) if mfe else None,
                'mae_pct_max': round(float(np.max(mae)), 6) if mae else None,
                'mdd_pct_max': round(float(np.max(mdd)), 6) if mdd else None,
                'path_metric_note': ('THEORETICAL_NEXT_OPEN_ENVELOPE_NOT_REALIZED_PNL'
                                     if mfe else 'NO_MATURED_ALERTS_IN_TEST_WINDOW'),
            }
        except Exception as exc:  # never fabricate path metrics
            entry['alerts'] = {'error': repr(exc)[:200]}
        per_symbol[symbol] = entry

    def pooled(pairs):
        if not pairs:
            return None
        y = np.concatenate([p[0] for p in pairs])
        s = np.concatenate([p[1] for p in pairs])
        if len(set(y.tolist())) < 2:
            return {'n': int(len(y)), 'auc': None,
                    'reason': 'SINGLE_CLASS_IN_POOLED_TEST_SAMPLES'}
        from sklearn.metrics import roc_auc_score
        return {'n': int(len(y)), 'positive': int(y.sum()),
                'auc': float(roc_auc_score(y, s))}

    out = {
        'status': 'EVALUATION_DONE_NOT_PUBLISHED',
        'as_of': args.as_of,
        'rolling_windows': windows['windows'],
        'test_window_assessment': assessment,
        'independent_test_claim': ('NOT_CLAIMED'
                                   if assessment['status'] != 'TEST_WINDOW_COMPLETE'
                                   else 'THREE_COMPLETE_MONTHS'),
        'global_release_id': global_release_id,
        'global_bundle_sha256': facts['bundle_sha256'],
        'symbols_evaluated': len(per_symbol),
        'pooled': {k: pooled(v) for k, v in aggregate.items()},
        'personal_symbols': sum(1 for v in per_symbol.values()
                                if v.get('personal_mode') not in (None, pm.MODE_FALLBACK)),
        'fallback_symbols': sum(1 for v in per_symbol.values()
                                if v.get('personal_mode') == pm.MODE_FALLBACK),
        'insufficient_history_symbols': sum(
            1 for v in per_symbol.values()
            if v.get('history_status') == 'INSUFFICIENT_HISTORY'),
        'unresolved_events': int(sum(counters.get('unresolved_both', 0) for _ in [0])),
        'per_symbol': per_symbol,
        'can_publish': False,
        'production_publish_allowed': False,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2,
                                        allow_nan=False), encoding='utf-8')
    print(json.dumps({'status': out['status'], 'out': str(args.out),
                      'symbols_evaluated': out['symbols_evaluated'],
                      'test_window_assessment': assessment['status'],
                      'pooled_auc': {k: (v or {}).get('auc') for k, v in out['pooled'].items()}},
                     ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(
        description='Panji two-stage integration offline CLI (no deploy)')
    sub = p.add_subparsers(dest='command', required=True)

    def common(sp):
        sp.add_argument('--assets', type=Path, default=model.ASSET_DIR)
        sp.add_argument('--bars', type=Path, required=True)
        sp.add_argument('--instruments', type=Path, required=True)
        sp.add_argument('--as-of', required=True)
        sp.add_argument('--source-mode', choices=['canonical', 'diagnostic'],
                        default='diagnostic')
        sp.add_argument('--export-dir', type=Path,
                        default=Path('/tmp/panji_strategy_export'))
        sp.add_argument('--max-symbols', type=int, default=0)
        sp.add_argument('--chart-bars', type=int, default=85)

    b = sub.add_parser('build')
    common(b)
    b.add_argument('--output-dir', type=Path, required=True)
    b.add_argument('--shard-bytes', type=int, default=12 * 1024 * 1024)
    b.add_argument('--personal-registry', type=Path, default=None)

    t = sub.add_parser('train-global')
    common(t)
    t.add_argument('--output-dir', type=Path, required=True)
    t.add_argument('--allow-partial-diagnostics', action='store_true')
    t.add_argument('--min-samples', type=int, default=200)
    t.add_argument('--min-each-class', type=int, default=40)
    t.add_argument('--trees', type=int, default=300)
    t.add_argument('--threads', type=int, default=4)
    t.add_argument('--seed', type=int, default=20261015)
    t.add_argument('--skip-personal', action='store_true')
    t.add_argument('--personal-standalone', action='store_true')
    t.add_argument('--personal-min-samples', type=int, default=60)
    t.add_argument('--personal-min-each-class', type=int, default=15)
    t.add_argument('--personal-artifact-dir', type=Path, default=None)
    t.add_argument('--personal-symbols', nargs='*', default=[])
    t.add_argument('--personal-registry', type=Path, default=None)
    t.add_argument('--oof-folds', type=int, default=3)
    t.add_argument('--oof-trees', type=int, default=60)

    tp = sub.add_parser('train-personal')
    common(tp)
    tp.add_argument('--global-bundle', type=Path, required=True)
    tp.add_argument('--global-release-id', required=True)
    tp.add_argument('--global-bundle-sha256', required=True)
    tp.add_argument('--personal-symbols', nargs='*', default=[])
    tp.add_argument('--personal-registry', type=Path, default=None)
    tp.add_argument('--personal-artifact-dir', type=Path, default=None)
    tp.add_argument('--personal-standalone', action='store_true')
    tp.add_argument('--personal-min-samples', type=int, default=60)
    tp.add_argument('--personal-min-each-class', type=int, default=15)
    tp.add_argument('--oof-folds', type=int, default=3)
    tp.add_argument('--oof-trees', type=int, default=60)
    tp.add_argument('--threads', type=int, default=4)
    tp.add_argument('--seed', type=int, default=20261015)

    inc = sub.add_parser('incremental')
    common(inc)
    inc.add_argument('--incremental-state', type=Path, default=None)
    inc.add_argument('--personal-registry', type=Path, default=None)
    inc.add_argument('--global-release-id', required=True)
    inc.add_argument('--global-bundle-sha256', required=True)

    ev = sub.add_parser('evaluate')
    common(ev)
    ev.add_argument('--bundle', type=Path, required=True)
    ev.add_argument('--personal-registry', type=Path, default=None)
    ev.add_argument('--symbols', nargs='*', default=[])
    ev.add_argument('--out', type=Path, required=True)

    a = p.parse_args()
    try:
        if a.command == 'build':
            build(a)
        elif a.command == 'train-global':
            train_global(a)
        elif a.command == 'train-personal':
            train_personal_cmd(a)
        elif a.command == 'incremental':
            incremental_cmd(a)
        elif a.command == 'evaluate':
            evaluate_cmd(a)
    except (producer.ProducerError, model.AssetError, pm.PersonalModelError,
            ValueError) as ex:
        print('STOP_INTEGRATION', str(ex), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
