"""Explicit offline build/publish; no worker is started at import or server startup.

Two *explicit* market-data entrances:

  canonical  (production): db_export.export_dataset() reads authoritative QFQ
              daily bars through MarketDataAggregationService.get_bars_batch().
  diagnostic (research):   existing raw parquet converted by the canonical pure
              function; labelled OFFLINE_FACTOR_DIAGNOSTIC and never used as an
              automatic production fallback.

The weights actually scored come from the ACTIVE release pointer when one exists,
so publishing/rolling back a release changes what the worker really loads.
"""
from __future__ import annotations
import argparse
import asyncio
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sys

from . import model, producer, source
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


def build(args):
    repo = os.environ.get('PANJI_STRATEGY_CODE_ROOT')
    out = _ensure_outside(args.output_dir, repo, 'OUTPUT_DIR')

    date.fromisoformat(args.as_of)
    assets = model.validate_source_assets(args.assets)
    fmod = model.load_engine(args.assets)
    # The activated release controls the weights actually used for inference.
    bundle, facts, bundle_path, bundle_origin = load_bundle_for_run(out, args.assets)

    if args.source_mode == 'canonical':
        # Production entrance: read-only DB export via the single price owner MDAS.
        from .db_export import export_dataset
        export_dir = _ensure_outside(args.export_dir, repo, 'EXPORT_DIR')
        export_dir.mkdir(parents=True, exist_ok=True)
        asyncio.run(export_dataset(export_dir, date.fromisoformat(args.as_of)))
        stock_raw, source_report = source.load_canonical_qfq_export(
            export_dir, args.as_of, max_symbols=args.max_symbols)
    else:
        # Research-only offline parquet; explicitly NOT a production fallback.
        stock_raw, source_report = source.load_official_parquet(
            args.bars, args.instruments, args.as_of, max_symbols=args.max_symbols)
        source_report['production_fallback_allowed'] = False

    if args.max_symbols == 0:
        if source_report['eligible_stock_count'] < 1000:
            raise producer.ProducerError('FULL_UNIVERSE_COVERAGE_BELOW_1000')
        if source_report['coverage_ratio'] < 0.85:
            raise producer.ProducerError('SOURCE_TRADE_DAY_COVERAGE_BELOW_85_PERCENT')

    release_id = producer.release_id_for(facts['bundle_sha256'])
    candidates, evaluation = producer.make_snapshots(
        stock_raw, fmod, bundle, as_of=args.as_of,
        source_sha=source_report['source_sha256'],
        chart_bars=args.chart_bars, release_id=release_id,
        price_adjustment=source_report.get('price_adjustment', 'qfq'),
        adjustment_mode=source_report.get('adjustment_mode'),
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
        print(json.dumps({'status': 'IDEMPOTENT_EXISTS', 'root': str(root)}, ensure_ascii=False))
        return None

    root.mkdir(parents=True, exist_ok=False)
    from app.services.strategy_candidate_shards import write_candidate_shards
    from app.services.strategy_candidate_snapshot import validate as validate_candidate

    shard_manifest = write_candidate_shards(
        candidates, root, validate=validate_candidate, target_bytes=args.shard_bytes)
    atomic_bytes(root / 'evaluation_snapshot.json', evaluation_data)
    descriptor = (root / 'candidate_snapshot.json').read_bytes()

    observation_total = sum(
        len(c['observations']) for st in candidates['stocks'].values() for c in st['candidates'])
    candidate_total = sum(len(st['candidates']) for st in candidates['stocks'].values())
    manifest = {
        'status': 'VALIDATED_STAGING_NOT_DEPLOYED',
        'release_id': release_id,
        'as_of': args.as_of,
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
        # Weights actually used, so activation can bind them to the pointer.
        'bundle_path': bundle_path,
        'bundle_origin': bundle_origin,
        'runtime': facts,
        'price_adjustment': source_report.get('price_adjustment'),
        'adjustment_mode': source_report.get('adjustment_mode'),
        'adjustment_as_of': source_report.get('adjustment_as_of'),
        'factor_provenance_authoritative': source_report.get(
            'factor_provenance_authoritative', False),
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
    }, ensure_ascii=False))
    return root


def main():
    p = argparse.ArgumentParser(
        description='Panji two-stage integration offline exporter, no deploy')
    sub = p.add_subparsers(dest='command', required=True)
    b = sub.add_parser('build')
    b.add_argument('--assets', type=Path, default=model.ASSET_DIR)
    b.add_argument('--bars', type=Path, required=True)
    b.add_argument('--instruments', type=Path, required=True)
    b.add_argument('--as-of', required=True)
    b.add_argument('--output-dir', type=Path, required=True)
    b.add_argument('--source-mode', choices=['canonical', 'diagnostic'], default='canonical')
    b.add_argument('--export-dir', type=Path, default=Path('/tmp/panji_strategy_export'))
    b.add_argument('--chart-bars', type=int, default=85)
    b.add_argument('--max-symbols', type=int, default=0)
    b.add_argument('--shard-bytes', type=int, default=12 * 1024 * 1024)
    a = p.parse_args()
    if a.command == 'build':
        build(a)


if __name__ == '__main__':
    try:
        main()
    except (producer.ProducerError, model.AssetError, ValueError) as ex:
        print('STOP_INTEGRATION', str(ex), file=sys.stderr)
        raise SystemExit(2)
