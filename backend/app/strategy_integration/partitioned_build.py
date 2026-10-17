"""Streaming artifact assembly; computation remains in producer.make_snapshots."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from app.services import strategy_partitioned_snapshot as transport

from . import producer
from .job_progress import progress
from .release_store import atomic_bytes, atomic_json


def build(args, *, out, frames, pit_source, source_report, fmod, bundle, facts,
          bundle_path, bundle_origin, assets, release_id, windows):
    out.mkdir(parents=True, exist_ok=True)
    from app.services.strategy_tv_zigzag import identity as tv_identity
    computation_sha = hashlib.sha256(b''.join(
        (Path(__file__).parent/name).read_bytes() for name in
        ('producer.py', 'fourteen_adapter.py', 'history_inference.py', 'inference_cache.py', 'breakout.py', 'evaluation_contract.py')
    )).hexdigest()
    # Results, unlike weights, also depend on the evaluation and history contracts.
    batch_id = hashlib.sha256(json.dumps({
        'release_id': release_id, 'as_of': args.as_of,
        'source_sha256': source_report['source_sha256'],
        'evaluation_protocol': producer.EVALUATION_PROTOCOL,
        'history_bars': args.chart_bars, 'diagnostic_max_symbols':args.max_symbols, 'tv_sha256': tv_identity(),
        'computation_sha256': computation_sha, 'lab_delivery': facts.get('lab_delivery'),
    }, sort_keys=True).encode()).hexdigest()
    destination = out/(args.as_of+'_'+batch_id[:16])
    if destination.exists():
        manifest = json.loads((destination/'manifest.json').read_text())
        if manifest['source_sha256'] != source_report['source_sha256']:
            raise producer.ProducerError('SAME_RUN_NAME_CHANGED_INPUT_FORBIDDEN')
        transport.validate_publication(destination/'candidate_snapshot.json')
        result = {'status': 'IDEMPOTENT_EXISTS', 'output': str(destination)}
        print(json.dumps(result))
        return result
    checkpoint = out/'.strategy-checkpoints'/batch_id
    with tempfile.TemporaryDirectory(prefix='.strategy-build-', dir=out) as temporary:
        return _assemble(args, staging=Path(temporary), destination=destination, checkpoint=checkpoint, batch_id=batch_id,
            frames=frames, pit_source=pit_source, source_report=source_report, fmod=fmod,
            bundle=bundle, facts=facts, bundle_path=bundle_path, bundle_origin=bundle_origin,
            assets=assets, release_id=release_id, windows=windows)


def _assemble(args, *, staging, destination, checkpoint, batch_id, frames, pit_source, source_report, fmod,
              bundle, facts, bundle_path, bundle_origin, assets, release_id, windows):
    stocks, daily_files, models = {}, {}, {}
    metadata = evaluation_metadata = None
    no_current_score = []
    for number, (symbol, raw) in enumerate(sorted(frames.items())):
        progress('PREDICT_STOCKS', number, len(frames), symbol=symbol)
        key = hashlib.sha256(symbol.encode()).hexdigest()[:24]
        record = checkpoint/(key+'.json')
        if record.is_file():
            saved = json.loads(record.read_text())
            if saved.get('batch_id') != batch_id or saved.get('symbol') != symbol:
                raise producer.ProducerError('CHECKPOINT_IDENTITY_MISMATCH')
            candidates = transport.leaf(checkpoint, saved['candidate'])
            evaluation = transport.leaf(checkpoint, saved['evaluation'])
            if list(candidates['stocks']) != [symbol]:
                raise producer.ProducerError('CHECKPOINT_SYMBOL_MISMATCH')
        else:
            from .job_progress import parent_owns_progress
            with parent_owns_progress():
                candidates, evaluation = producer.make_snapshots({symbol: raw}, fmod, bundle,
                    as_of=args.as_of, source_sha=source_report['source_sha256'], chart_bars=args.chart_bars,
                    release_id=release_id, price_adjustment=source_report.get('price_adjustment', 'qfq'),
                    adjustment_mode=source_report.get('adjustment_mode'), bundle_sha256=facts['bundle_sha256'],
                    pit_source={symbol: pit_source[symbol]} if pit_source else None)
            producer.verify_against_readers(candidates, evaluation)
            atomic_json(record, {'batch_id': batch_id, 'symbol': symbol,
                'candidate': transport.write_leaf(checkpoint, key+'_candidate.json', candidates),
                'evaluation': transport.write_leaf(checkpoint, key+'_evaluation.json', evaluation)})
        if metadata is None:
            metadata = {**candidates['metadata'], 'history_bars': args.chart_bars}
            evaluation_metadata = {**evaluation['metadata'], 'history_bars': args.chart_bars}
        candidates['metadata'], evaluation['metadata'] = metadata, evaluation_metadata
        producer.verify_against_readers(candidates, evaluation)
        stocks[symbol] = {
            'candidate': transport.write_leaf(staging, f'stock_{number:05d}_candidate.json', candidates),
            'evaluation': transport.write_leaf(staging, f'stock_{number:05d}_evaluation.json', evaluation)}
        stock = candidates['stocks'][symbol]
        if stock['bars'][-1]['date'] == args.as_of and not any(
                o['decision_date'] == args.as_of and o.get('scoring_status') != 'CANDIDATE_INACTIVE'
                and any(o.get(k) is not None for k in ('d0_score','l1_score','l2_score'))
                for c in stock['candidates'] for o in c['observations']):
            no_current_score.append(symbol)
        models.update({m['id']: m for m in evaluation['models']})
        for batch in candidates['daily_batches']:
            path = staging/('daily_'+batch['trade_date'].replace('-', '')+'.jsonl')
            daily_files[batch['trade_date']] = path
            with path.open('ab') as writer:
                for row in batch['items']:
                    writer.write(transport.encode(row)+b'\n')
    days = {}
    for day in sorted(daily_files)[-20:]:
        items = [json.loads(line) for line in daily_files[day].read_bytes().splitlines()]
        value = {'metadata': metadata, 'trade_date': day, 'status': 'PUBLISHED',
                 'release_id': release_id, 'items': items}
        days[day] = transport.write_leaf(staging, 'daily_'+day.replace('-', '')+'.json', value)
    # These are this build's own assembly files, never published inputs.
    for path in daily_files.values():
        path.unlink()
    total = sum(e[k]['bytes'] for e in stocks.values() for k in ('candidate','evaluation'))+sum(e['bytes'] for e in days.values())
    descriptor = {'schema': transport.SCHEMA, 'kind': 'candidate', 'metadata': metadata,
        'evaluation_metadata': evaluation_metadata, 'stocks': stocks, 'days': days,
        'models': list(models.values()), 'total_bytes': total}
    atomic_bytes(staging/'candidate_snapshot.json', transport.encode(descriptor))
    descriptor['kind'] = 'evaluation'
    atomic_bytes(staging/'evaluation_snapshot.json', transport.encode(descriptor))
    validation = transport.validate_publication(staging/'candidate_snapshot.json')
    manifest = {'status': 'VALIDATED_STAGING_NOT_DEPLOYED', 'release_id': release_id, 'batch_id': batch_id,
        'as_of': args.as_of, 'diagnostic_max_symbols':args.max_symbols, 'evaluation_protocol': producer.EVALUATION_PROTOCOL,
        'source_sha256': source_report['source_sha256'],
        'candidate_sha256': hashlib.sha256((staging/'candidate_snapshot.json').read_bytes()).hexdigest(),
        'evaluation_sha256': hashlib.sha256((staging/'evaluation_snapshot.json').read_bytes()).hexdigest(),
        'artifact_transport': transport.SCHEMA, 'validation': validation, 'runtime': facts,
        'source': source_report, 'bundle_path': bundle_path, 'bundle_origin': bundle_origin,
        'scoring_coverage': {'expected_universe':source_report.get('total_instruments'),
            'accepted_inputs':len(frames), 'validated_stocks':validation['candidate_stocks'],
            'current_bar_count':source_report.get('covered_asof_count'),
            'no_current_model_score_count':len(no_current_score), 'no_current_model_score_symbols':no_current_score},
        'assets': assets, 'rolling_windows': windows['windows'], 'production_published': False}
    atomic_json(staging/'manifest.json', manifest)
    os.rename(staging, destination)
    # Only this successfully published build's owned checkpoints are removed.
    # Failed builds retain exact-input leaves for the existing retry owner.
    import shutil
    shutil.rmtree(checkpoint)
    progress('SNAPSHOT_VALIDATED', len(frames), len(frames))
    result = {'status': manifest['status'], 'output': str(destination), 'release_id': release_id,
              'validation': validation}
    print(json.dumps(result))
    return result
