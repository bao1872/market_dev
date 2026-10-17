"""Production fourteen-task snapshot build CLI; research commands live in panji_lab.

No worker, scheduler, HTTP handler or production DB write is started at import
or at startup. Every command writes only to operator-supplied paths OUTSIDE the
repository, and never activates a model.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

from . import model, producer, source
from . import rolling_windows as rw
from .release_store import resolve_active_release


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
    """Resolve only the approved active model; no production fallback."""
    active = resolve_active_release(root)
    if active and active.get('bundle_path'):
        path = Path(active['bundle_path'])
        bundle, facts = model.load_bundle_file(
            path, pinned_sha=active.get('bundle_sha256') or None)
        return bundle, facts, str(path), 'ACTIVE_RELEASE'
    raise producer.ProducerError('APPROVED_PRODUCTION_MODEL_REQUIRED')


def _canonical_source_report(report: dict) -> dict:
    """Translate the MDAS report into the source-report contract build/producer
    consume: a tamper-evident ``source_sha256`` plus coverage/authority fields.
    """
    provenance = report.get('provenance', {})
    source_sha256 = hashlib.sha256(json.dumps(
        {'bars': {s: {'b': h.get('source_bar_hash'), 'f': h.get('adj_factor_hash')}
                  for s, h in provenance.items()},
         'as_of': report.get('adjustment_as_of'),
         'expected_symbols': report.get('expected_symbols'),
         'excluded_symbols': report.get('excluded_symbols'),
         'total_instruments': report.get('total_instruments'),
         'covered_asof_count': report.get('covered_asof_count'),
         'price_adjustment': report.get('price_adjustment')}, sort_keys=True).encode()).hexdigest()
    return {
        'source_sha256': source_sha256,
        'eligible_stock_count': report['eligible_stock_count'],
        'coverage_ratio': report['coverage_ratio_of_eligible'],
        'pit_factor_series_available': report.get('pit_factor_series_available', False),
        'price_adjustment': report.get('price_adjustment', 'qfq'),
        'adjustment_mode': 'CANONICAL_MDAS_QFQ',
        'adjustment_as_of': report.get('adjustment_as_of'),
        'factor_provenance_authoritative': True,
        'connection_read_only_verified': report.get('connection_read_only_verified', False),
        'total_instruments': report.get('total_instruments'),
        'covered_asof_count': report.get('covered_asof_count'),
        'coverage_ratio_of_total': report.get('coverage_ratio_of_total'),
        'excluded': report.get('excluded', {}),
        'excluded_symbols': report.get('excluded_symbols', {}),
        'stale_symbols': report.get('stale_symbols', []),
        'expected_symbols': report.get('expected_symbols', []),
        'universe_owner': report.get('universe_owner'),
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
                date.fromisoformat(args.as_of), batch_size=80, max_symbols=args.max_symbols)
        stock_raw, mdas_report = asyncio.run(_read())
        if not mdas_report.get('pit_factor_series_available'):
            raise producer.ProducerError('CANONICAL_PIT_FACTOR_SERIES_UNAVAILABLE')
        pit = dict(stock_raw)
        frames = stock_raw
        return frames, pit, _canonical_source_report(mdas_report)
    frames, report = source.load_official_parquet_pit(
        args.bars, args.instruments, args.as_of, max_symbols=args.max_symbols)
    report['production_fallback_allowed'] = False
    pit = dict(frames)
    frames = {s: df.drop(columns=['adj_factor']) for s, df in frames.items()}
    return frames, pit, report


# --------------------------------------------------------------------------- build
def validate_source_coverage(report, max_symbols):
    if max_symbols == 0:
        total = report.get('total_instruments')
        covered = report.get('covered_asof_count')
        eligible = report.get('eligible_stock_count')
        if (not isinstance(total, int) or isinstance(total, bool) or total <= 0
                or not isinstance(covered, int) or isinstance(covered, bool)
                or not isinstance(eligible, int) or isinstance(eligible, bool)
                or not 0 <= covered <= eligible <= total):
            raise producer.ProducerError('FULL_UNIVERSE_DENOMINATOR_INVALID')
        if covered / total < 0.85:
            raise producer.ProducerError('FULL_UNIVERSE_TRADE_DAY_COVERAGE_BELOW_85_PERCENT')
        if report['eligible_stock_count'] < 1000:
            raise producer.ProducerError('FULL_UNIVERSE_COVERAGE_BELOW_1000')
        if report['coverage_ratio'] < 0.85:
            raise producer.ProducerError('SOURCE_TRADE_DAY_COVERAGE_BELOW_85_PERCENT')


def build(args):
    if args.chart_bars < 90:
        raise producer.ProducerError('CHART_REQUIRES_AT_LEAST_90_BARS')
    repo = os.environ.get('PANJI_STRATEGY_CODE_ROOT')
    out = _ensure_outside(args.output_dir, repo, 'OUTPUT_DIR')
    date.fromisoformat(args.as_of)
    windows = rw.rolling_windows(date.fromisoformat(args.as_of))

    from .fourteen_adapter import verify_package, verify_runtime_contract
    asset_manifest, asset_sha = verify_package(args.assets)
    verify_runtime_contract(asset_manifest)
    assets = {'manifest_sha256':asset_sha, 'models':14, 'feature_dim':len(asset_manifest['feature_columns'])}
    if getattr(args, 'candidate_bundle', None):
        if not getattr(args, 'candidate_sha', None):
            raise producer.ProducerError('CANDIDATE_BUNDLE_REQUIRES_PINNED_SHA')
        bundle, facts = model.load_bundle_file(args.candidate_bundle, pinned_sha=args.candidate_sha)
        bundle_path, bundle_origin = str(args.candidate_bundle), 'TRAINING_CANDIDATE'
    else:
        bundle, facts, bundle_path, bundle_origin = load_bundle_for_run(out, args.assets)
    delivery_id = getattr(args, 'lab_delivery_id', None)
    if delivery_id:
        from .lab_delivery import validate_delivery, verify_golden
        if delivery_id != facts['bundle_sha256'] or bundle_origin != 'TRAINING_CANDIDATE':
            raise producer.ProducerError('LAB_DELIVERY_IDENTITY_MISMATCH')
        _, approval, _ = validate_delivery(Path(bundle_path), delivery_id)
        from .job_progress import progress
        progress('VERIFY_LAB_GOLDEN', 0, 1)
        facts['lab_delivery'] = {**verify_golden(Path(bundle_path), delivery_id),
                                 'lab_revision': approval['lab_revision'],
                                 'approved_for_production':approval['approved_for_production']}
        progress('VERIFY_LAB_GOLDEN', 1, 1)
        bundle_origin = 'PANJI_LAB_DELIVERY'
    if 'fourteen_runtime' not in bundle:
        raise producer.ProducerError('PRODUCTION_REQUIRES_FOURTEEN_GLOBAL_MODEL')
    # The approved runtime owns this exact frozen feature engine; do not load
    # two additional base-model copies just to obtain the same engine module.
    fmod = bundle['fourteen_runtime'].engine
    producer.require_price_contract(bundle, producer.PRICE_INPUT_CONTRACT)
    expected_release = getattr(args, 'expected_active_release_id', None)
    expected_bundle = getattr(args, 'expected_active_bundle_sha', None)
    if ((expected_release and expected_release != producer.release_id_for(facts['bundle_sha256']))
            or (expected_bundle and expected_bundle != facts['bundle_sha256'])):
        raise producer.ProducerError('PREDICTION_PINNED_MODEL_CHANGED')
    frames, pit_source, source_report = _load_market(args, repo)

    expected_source = getattr(args, 'expected_source_sha', None)
    if expected_source and expected_source != source_report['source_sha256']:
        raise producer.ProducerError('RETRY_SOURCE_CHANGED_REVIEW_REQUIRED')
    validate_source_coverage(source_report, args.max_symbols)

    release_id = producer.release_id_for(facts['bundle_sha256'])

    if args.personal_registry:
        raise producer.ProducerError('FOURTEEN_REQUIRES_GLOBAL_ONLY')
    from .partitioned_build import build as build_partitioned
    return build_partitioned(args, out=out, frames=frames, pit_source=pit_source,
        source_report=source_report, fmod=fmod, bundle=bundle, facts=facts,
        bundle_path=bundle_path, bundle_origin=bundle_origin, assets=assets,
        release_id=release_id, windows=windows)


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
        sp.add_argument('--chart-bars', type=int, default=180)

    b = sub.add_parser('build')
    common(b)
    b.add_argument('--output-dir', type=Path, required=True)
    b.add_argument('--shard-bytes', type=int, default=12 * 1024 * 1024)
    b.add_argument('--candidate-bundle', type=Path)
    b.add_argument('--lab-delivery-id', default=None)
    b.add_argument('--candidate-sha', default=None)
    b.add_argument('--expected-source-sha', default=None)
    b.add_argument('--expected-active-release-id', default=None)
    b.add_argument('--expected-active-bundle-sha', default=None)
    b.add_argument('--personal-registry', type=Path, default=None)

    a = p.parse_args()
    try:
        if a.command == 'build':
            build(a)
    except Exception as ex:
        from .job_store import failure_details
        failure = {'status':'FAILED', **failure_details(ex)}
        print('STOP_INTEGRATION', failure['error'], file=sys.stderr)
        print(json.dumps(failure), flush=True)
        raise SystemExit(2) from ex



if __name__ == '__main__':
    main()
