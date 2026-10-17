"""Global fourteen-task training, called only by the existing training owner.

No auto activation. Portable output can be consumed by the existing build CLI.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from . import model, source
from . import rolling_windows as rw
from .fourteen_adapter import PACKAGE, extreme_resolution, load_package
from .producer import ProducerError


def build_dataset(stock_raw, runtime, windows, *, sink=None):
    from . import lab_parity_contract as labels
    data = defaultdict(list)
    for position, (symbol, raw) in enumerate(sorted(stock_raw.items())):
        from .job_progress import progress
        progress("BUILD_TRAINING_DATA", position, len(stock_raw), symbol=symbol)
        pit = raw if 'adj_factor' in raw.columns else None
        days = [pd.Timestamp(v).date() for v in raw.datetime]
        oracle_cache = {}
        for t in range(20, len(raw)):
            split = rw.split_of(days[t], windows)
            if split is None:
                continue
            prefix = model._pit_frame(pit.iloc[:t+1], days[t].isoformat()) if pit is not None else raw.iloc[:t+1]
            scores = runtime.score(prefix, days[t].isoformat())
            if not scores['signals']:
                continue
            if split not in oracle_cache:
                cutoff = max(i for i, d in enumerate(days) if d <= windows['dates'][split][1])
                oracle = model._pit_frame(pit.iloc[:cutoff+1], days[cutoff].isoformat()) if pit is not None else raw.iloc[:cutoff+1]
                final, _, _ = runtime.engine.replay(oracle)
                oracle_cache[split] = (oracle, final, cutoff)
            oracle, final, cutoff = oracle_cache[split]
            _, snap, _ = runtime.engine.replay(prefix)
            _, matrix = runtime.engine.features(prefix, snap, [t])
            for sig in scores['signals']:
                j, layer, kind, stage = sig['anchor_bar'], sig['layer'], sig['kind'], sig['stage']
                if stage == 0:
                    if rw.split_of(days[j], windows) != split:
                        continue
                elif j + 3 >= len(days) or rw.split_of(days[j+1], windows) != split or rw.split_of(days[j+3], windows) != split:
                    continue
                if layer == 'L2':
                    lab = labels.labels_for_stage(oracle, final, j, kind, t, cutoff)
                    y, maturity = lab.l2, lab.l2_available_bar
                else:
                    y, maturity = extreme_resolution(oracle, final, j, kind)
                if y is None or maturity is None or not t < maturity <= cutoff:
                    continue
                row = {
                    'features': np.asarray(matrix[0], dtype=np.float32), 'label': y,
                    'symbol': symbol, 'decision_date': days[t], 'anchor_date': days[j],
                    'label_available_at': days[maturity],
                    'sample_id': f'{symbol}|{days[j]}|{kind}|D{stage}|{layer}'}
                key = (split, layer, kind, stage)
                if sink is None:
                    data[key].append(row)
                else:
                    sink.append(key, row)
    return data if sink is None else sink.partitions


def train(args):
    from .training import _fit_submodel, _resolve_windows
    from .training_spool import DatasetSpool
    windows = _resolve_windows(args)
    output = Path(args.output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise ProducerError('CANDIDATE_MODEL_DIR_NOT_EMPTY')
    output.mkdir(parents=True, exist_ok=True)
    if args.max_symbols and not args.allow_partial_diagnostics:
        raise ProducerError('PARTIAL_TRAIN_REQUIRES_EXPLICIT_DIAGNOSTIC_FLAG')
    runtime = load_package()[0]['fourteen_runtime']
    if args.source_mode == 'canonical':
        import asyncio

        from .cli import _canonical_source_report, validate_source_coverage
        from .db_export import read_official_qfq_stock_batch
        frames, report = asyncio.run(read_official_qfq_stock_batch(
            date.fromisoformat(args.as_of), max_symbols=args.max_symbols))
        provenance = _canonical_source_report(report)
        if not report.get('pit_factor_series_available'):
            raise ProducerError('CANONICAL_PIT_FACTOR_SERIES_UNAVAILABLE')
        if args.max_symbols:
            frames = dict(sorted(frames.items())[:args.max_symbols])
        validate_source_coverage(provenance, args.max_symbols)
    else:
        frames, provenance = source.load_official_parquet_pit(args.bars, args.instruments, args.as_of, max_symbols=args.max_symbols)
    with tempfile.TemporaryDirectory(prefix='training-partitions-', dir=output) as temporary:
        spool = DatasetSpool(Path(temporary), len(runtime.engine.ALL_COLS))
        data = build_dataset(frames, runtime, windows, sink=spool)
        assessment = rw.assess_test_window(sorted({pd.Timestamp(d).date() for f in frames.values() for d in f.datetime}), windows)
        if assessment['status'] != 'TEST_WINDOW_COMPLETE':
            raise ProducerError('FOURTEEN_TEST_WINDOW_INCOMPLETE')
        fitted = {}
        for position, task in enumerate(runtime.manifest['models']):
            from .job_progress import progress
            progress('FIT_FOURTEEN_MODELS', position, 14, task=task)
            layer, kind, stage_text = task.split('_')
            stage = int(stage_text[1:])
            rows = {s: data.get((s, layer, kind, stage), []) for s in rw.SPLIT_NAMES}
            for split, items in rows.items():
                counts = items.label_counts if hasattr(items, 'label_counts') else [sum(r['label'] == y for r in items) for y in (0, 1)]
                if len(items) < args.min_samples or min(counts) < args.min_each_class:
                    raise ProducerError(f'FOURTEEN_INSUFFICIENT_SAMPLES:{task}:{split}')
            fitted[task] = _fit_submodel(rows, runtime.engine, trees=args.trees, threads=args.threads, seed=args.seed, progress_label=task)
    package = output / 'fourteen_candidate_UNPUBLISHED'
    package.mkdir()
    (package / 'models').mkdir()
    manifest = json.loads((PACKAGE / 'manifest.json').read_text())
    for name in ('runtime.py', 'feature_engine.py', '__init__.py', 'README.md', 'INTEGRATION.md', 'requirements.txt'):
        shutil.copyfile(PACKAGE / name, package / name)
    manifest.update(version='fourteen-global-' + args.as_of + '-' + hashlib.sha256(
        json.dumps({'source': provenance['source_sha256'], 'windows': windows['windows'], 'trees': args.trees,
                    'seed': args.seed}, sort_keys=True).encode()).hexdigest()[:16],
        data_end=windows['windows']['test'][1], price_input_contract=(
            'QFQ_ASOF_V1' if args.source_mode == 'canonical' and provenance.get('factor_provenance_authoritative') else 'OFFLINE_FACTOR_DIAGNOSTIC'),
        split_windows=windows['windows'])
    metrics = {}
    for task, (clf, threshold, results) in fitted.items():
        clf.booster_.save_model(str(package / manifest['models'][task]['file']))
        manifest['models'][task]['threshold'] = threshold
        metrics[task] = results
    manifest['files'] = {name: hashlib.sha256((package / name).read_bytes()).hexdigest()
                         for name in manifest['files']}
    (package / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    load_package(package)  # Execute the production integrity loader before reporting success.
    report = {'status': 'CANDIDATE_UNPUBLISHED', 'model_count': 14, 'global_only': True,
              'model_version': manifest['version'], 'bundle_path': str(package),
              'windows': rw.json_safe_windows(windows), 'metrics': metrics,
              'source_sha256': provenance['source_sha256'], 'production_published': False,
              'source_contract': {k: provenance.get(k) for k in ('price_adjustment', 'adjustment_as_of',
                  'factor_provenance_authoritative', 'connection_read_only_verified', 'eligible_stock_count',
                  'total_instruments', 'covered_asof_count', 'coverage_ratio', 'excluded', 'universe_owner')},
              'training_partitions': spool.manifest()}
    (output / 'train_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'bundle_path': str(package), 'models': 14}))
    return report
