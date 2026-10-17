"""Per-stock transport for full-market snapshots, using existing domain validators.

Readers open only a selected stock/day. Evaluation iterates bounded stock files;
it never materializes all K-lines or all task records in the API process.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

SCHEMA = 'panji-strategy-partitioned-v1'
MAX_INDEX = 4*1024*1024
MAX_TOTAL = 16*1024*1024*1024
NAME = re.compile(r'(stock_[0-9]{5}_(candidate|evaluation)|daily_[0-9]{8})\.json\Z')


def index(path):
    path = Path(path).resolve()
    if path.stat().st_size > MAX_INDEX:
        raise ValueError('PARTITION_INDEX_TOO_LARGE')
    value = json.loads(path.read_bytes())
    if value.get('schema') != SCHEMA or value.get('kind') not in ('candidate','evaluation'):
        raise ValueError('PARTITION_SCHEMA_INVALID')
    stocks = value.get('stocks')
    if not isinstance(stocks, dict) or not 1 <= len(stocks) <= 8000:
        raise ValueError('PARTITION_STOCK_COUNT')
    from .strategy_candidate_snapshot import SYMBOL_RE
    if any(not SYMBOL_RE.fullmatch(s) for s in stocks):
        raise ValueError('PARTITION_SYMBOL_INVALID')
    entries = [entry[k] for entry in stocks.values() for k in ('candidate','evaluation')]
    entries += list(value.get('days', {}).values())
    if any(date.fromisoformat(day).isoformat() != day for day in value.get('days', {})):
        raise ValueError('PARTITION_DAY_INVALID')
    if len(value.get('days', {})) > 350 or len({e['file'] for e in entries}) != len(entries):
        raise ValueError('PARTITION_INDEX_DUPLICATES')
    total = 0
    for entry in entries:
        if not NAME.fullmatch(entry['file']) or not isinstance(entry['bytes'], int) or not 0 < entry['bytes'] <= 48*1024*1024 or not re.fullmatch('[0-9a-f]{64}', entry['sha256']):
            raise ValueError('PARTITION_FILE_CONTRACT')
        total += entry['bytes']
    if total > MAX_TOTAL or total != value.get('total_bytes'):
        raise ValueError('PARTITION_TOTAL_BYTES')
    from .strategy_candidate_snapshot import DatasetInvalid, validate
    from .strategy_research_read_service import SnapshotInvalid, _check_snapshot
    try:
        _check_snapshot({'schema': 'panji-strategy-evaluation-v1', 'metadata': value['evaluation_metadata'],
                         'models': value['models'], 'events': []})
        validate({'schema': 'panji-strategy-candidates-v3', 'metadata': value['metadata'], 'stocks': {}, 'daily_batches': []})
    except (SnapshotInvalid, DatasetInvalid) as exc:
        raise ValueError('PARTITION_METADATA_INVALID') from exc
    if any(value['metadata'].get(c) != value['evaluation_metadata'].get(e) for c, e in (
            ('release_id','release_id'), ('model_sha256','model_sha256'), ('model_version','model_version'), ('source_sha256','data_sha256'))):
        raise ValueError('PARTITION_CROSS_ASSET_LINEAGE')
    return value


def leaf(base, entry):
    path = Path(base)/entry['file']
    if path.is_symlink() or not path.is_file() or path.stat().st_size != entry['bytes']:
        raise ValueError('PARTITION_FILE_MISSING_OR_CHANGED')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry['sha256']:
        raise ValueError('PARTITION_SHA_MISMATCH')
    return json.loads(raw)


def candidate_leaf(base, manifest, symbol):
    from .strategy_candidate_snapshot import validate
    value = validate(leaf(base, manifest['stocks'][symbol]['candidate']))
    if list(value['stocks']) != [symbol]:
        raise ValueError('PARTITION_STOCK_IDENTITY')
    if value['metadata'] != manifest['metadata']:
        raise ValueError('PARTITION_CANDIDATE_LINEAGE')
    return value


def evaluation_leaf(base, manifest, symbol):
    from .strategy_research_read_service import _check_snapshot
    value = _check_snapshot(leaf(base, manifest['stocks'][symbol]['evaluation']))
    if value['metadata'] != manifest['evaluation_metadata']:
        raise ValueError('PARTITION_EVALUATION_LINEAGE')
    model_map = {m['id']: m for m in manifest['models']}
    if any(model_map.get(m['id']) != m for m in value['models']):
        raise ValueError('PARTITION_MODEL_IDENTITY')
    if any(r['symbol'] != symbol for r in value['events']) or any(r['symbol'] != symbol for r in value.get('task_samples', [])):
        raise ValueError('PARTITION_EVALUATION_SYMBOL')
    return value


def read_candidates(path, *, symbol=None, trade_date=None):
    manifest = index(path)
    base = Path(path).parent
    if symbol in manifest['stocks']:
        return candidate_leaf(base, manifest, symbol)
    stocks = dict.fromkeys(manifest['stocks'])
    days = [{'trade_date': day, 'release_id': manifest['metadata']['release_id'],
             'status': 'PUBLISHED', 'items': []} for day in manifest['days']]
    if trade_date in manifest['days']:
        selected = leaf(base, manifest['days'][trade_date])
        if selected['trade_date'] != trade_date or selected['metadata'] != manifest['metadata']:
            raise ValueError('PARTITION_DAILY_LINEAGE')
        days = [selected if d['trade_date'] == trade_date else d for d in days]
    return {'schema': 'panji-strategy-candidates-v3', 'metadata': manifest['metadata'],
            'stocks': stocks, 'daily_batches': days}


class EvaluationRows:
    def __init__(self, base, manifest, field):
        self.base, self.manifest, self.field = base, manifest, field

    def __iter__(self):
        return self.select_symbol(None)

    def select_symbol(self, selected):
        from .strategy_research_read_service import SnapshotInvalid
        try:
            for symbol in self.manifest['stocks']:
                if selected is not None and symbol != selected:
                    continue
                yield from evaluation_leaf(self.base, self.manifest, symbol).get(self.field, [])
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise SnapshotInvalid('partitioned evaluation leaf failed') from exc


def read_evaluation(path):
    manifest = index(path)
    return {'schema': 'panji-strategy-evaluation-v1', 'metadata': manifest['evaluation_metadata'],
            'models': manifest['models'], 'events': EvaluationRows(Path(path).parent, manifest, 'events'),
            'task_samples': EvaluationRows(Path(path).parent, manifest, 'task_samples')}


def validate_publication(path):
    """Sequential full-corpus validation, including the separate daily index."""
    manifest = index(path)
    expected = {day: [] for day in manifest['days']}
    n_events = n_samples = 0
    seen_events, seen_samples = set(), set()
    for symbol in manifest['stocks']:
        candidate = candidate_leaf(Path(path).parent, manifest, symbol)
        evaluation = evaluation_leaf(Path(path).parent, manifest, symbol)
        n_events += len(evaluation['events'])
        n_samples += len(evaluation.get('task_samples', []))
        for event in evaluation['events']:
            key = (event['model_id'], event['event_id'])
            if key in seen_events:
                raise ValueError('PARTITION_DUPLICATE_EVENT')
            seen_events.add(key)
        for sample in evaluation.get('task_samples', []):
            if sample['sample_id'] in seen_samples:
                raise ValueError('PARTITION_DUPLICATE_TASK_SAMPLE')
            seen_samples.add(sample['sample_id'])
        for batch in candidate['daily_batches']:
            if batch['trade_date'] not in expected:
                continue
            for row in batch['items']:
                expected[batch['trade_date']].append(hashlib.sha256(encode(row)).hexdigest())
    if n_events > 400_000 or n_samples > 2_000_000:
        raise ValueError('PARTITION_GLOBAL_EVALUATION_ROW_LIMIT')
    for day, entry in manifest['days'].items():
        daily = leaf(Path(path).parent, entry)
        hashes = [hashlib.sha256(encode(row)).hexdigest() for row in daily['items']]
        if daily['metadata'] != manifest['metadata'] or daily['trade_date'] != day or sorted(hashes) != sorted(expected[day]):
            raise ValueError('PARTITION_DAILY_OBSERVATION_MISMATCH')
    return {'evaluation_signal_rows': n_events, 'task_sample_rows': n_samples,
            'candidate_stocks': len(manifest['stocks']), 'daily_batches': len(manifest['days'])}


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()


def write_leaf(folder, name, value):
    from app.strategy_integration.release_store import atomic_bytes
    raw = encode(value)
    if len(raw) > 48*1024*1024:
        raise ValueError('PARTITION_LEAF_TOO_LARGE')
    atomic_bytes(Path(folder)/name, raw)
    return {'file': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
