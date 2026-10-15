"""Per-symbol personal model records, registry and input-compatibility gate.

A personal model NEVER replaces the full-market model: it is combined with it.
Every record carries its own training state, windows, data hash, sample counts,
evaluation results and version lineage, so activation and rollback are explicit.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from . import model as engine_model
from . import rolling_windows as _rw

PERSONAL_SCHEMA = 'panji-strategy-personal-model-v1'
MODE_REGULARIZED = 'REGULARIZED_ADJUSTMENT'
MODE_STANDALONE = 'STANDALONE_LIGHT'
MODE_FALLBACK = 'FALLBACK_GLOBAL'
MODES = (MODE_REGULARIZED, MODE_STANDALONE, MODE_FALLBACK)

STATUS_CANDIDATE = 'CANDIDATE_TRAINED_NOT_PUBLISHED'
STATUS_ACTIVE = 'ACTIVE'
STATUS_RETIRED = 'RETIRED'

DEFAULT_REGISTRY_DIR = Path(os.environ.get(
    'PANJI_STRATEGY_PERSONAL_REGISTRY', '/tmp/panji_strategy_personal_models'))
REGISTRY_FILE = 'personal_registry.json'


class PersonalModelError(RuntimeError):
    pass


def canonical_bytes(blob: Any) -> bytes:
    return json.dumps(blob, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(',', ':')).encode('utf-8')


def record_identity(record: dict) -> str:
    """Stable identity of a personal record (content addressed)."""
    return hashlib.sha256(canonical_bytes(record)).hexdigest()


def registry_path(root: Path | str | None = None) -> Path:
    base = Path(root) if root else DEFAULT_REGISTRY_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / REGISTRY_FILE


def load_registry(root: Path | str | None = None) -> dict:
    """Load the registry; a missing registry is an empty one, never an error."""
    path = registry_path(root)
    if not path.is_file():
        return {'schema': PERSONAL_SCHEMA, 'symbols': {}, 'history': {}}
    try:
        blob = json.loads(path.read_text(encoding='utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PersonalModelError('PERSONAL_REGISTRY_INVALID_JSON') from exc
    if not isinstance(blob, dict) or blob.get('schema') != PERSONAL_SCHEMA:
        raise PersonalModelError('PERSONAL_REGISTRY_SCHEMA_MISMATCH')
    blob.setdefault('symbols', {})
    blob.setdefault('history', {})
    return blob


def save_registry(registry: dict, root: Path | str | None = None) -> Path:
    path = registry_path(root)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(registry, ensure_ascii=False, sort_keys=True,
                              indent=2, allow_nan=False), encoding='utf-8')
    os.replace(tmp, path)
    return path


def new_record(symbol: str, *, windows: dict, global_release_id: str,
               global_bundle_sha256: str, data_sha256: str,
               history: dict) -> dict:
    return {
        'schema': PERSONAL_SCHEMA,
        'symbol': symbol,
        'model_version': None,
        'mode': MODE_FALLBACK,
        'fallback_reason': 'NOT_TRAINED',
        'status': STATUS_CANDIDATE,
        'global_release_id': global_release_id,
        'global_bundle_sha256': global_bundle_sha256,
        'personal_bundle_sha256': None,
        'release_id': None,
        'price_input_contract': None,
        'feature_sha': engine_model.ENGINE_SHA,
        'feature_dim': 131,
        'windows': _rw.json_safe_windows(windows),
        'history': history,
        'samples': {},
        'metrics': {},
        'submodels': {},
        'blend_weight': 0.0,
        'data_sha256': data_sha256,
        'created_at': None,
        'last_trade_date_processed': None,
        'last_matured_event_date': None,
        'rollback_parent': None,
        'artifact_path': None,
    }


def set_identity(record: dict) -> dict:
    """Fill model_version / release_id / identity for a finished record."""
    record['model_version'] = hashlib.sha256(canonical_bytes({
        'symbol': record['symbol'],
        'mode': record['mode'],
        'windows': record['windows'],
        'global_release_id': record['global_release_id'],
        'submodels': record['submodels'],
        'data_sha256': record['data_sha256'],
    })).hexdigest()
    record['release_id'] = hashlib.sha256(
        f"{record['model_version']}|{record['global_release_id']}|"
        f"{record['price_input_contract'] or 'UNKNOWN'}".encode()).hexdigest()
    return record


def register(registry: dict, record: dict, *, activate: bool = False) -> dict:
    """Store a candidate record; never activates unless explicitly requested."""
    set_identity(record)
    symbol = record['symbol']
    previous = registry['symbols'].get(symbol)
    if previous is not None:
        record['rollback_parent'] = previous.get('model_version')
    registry['symbols'][symbol] = record
    registry['history'].setdefault(symbol, []).append({
        'model_version': record['model_version'],
        'mode': record['mode'],
        'created_at': record.get('created_at'),
        'personal_bundle_sha256': record.get('personal_bundle_sha256'),
        'status': record['status'],
    })
    if activate:
        record['status'] = STATUS_ACTIVE
    return record


def resolve_active(registry: dict, symbol: str) -> dict | None:
    record = registry['symbols'].get(symbol)
    if record is None or record.get('status') != STATUS_ACTIVE:
        return None
    return record


def history_for(registry: dict, symbol: str) -> list[dict]:
    return list(registry['history'].get(symbol, []))


def rollback_to(registry: dict, symbol: str, model_version: str) -> dict:
    """Re-activate a previously registered version; never rewrites history."""
    record = registry['symbols'].get(symbol)
    if record is None:
        raise PersonalModelError('PERSONAL_MODEL_NOT_FOUND ' + symbol)
    target = next((r for r in registry['history'].get(symbol, [])
                   if r.get('model_version') == model_version), None)
    if target is None:
        raise PersonalModelError('PERSONAL_MODEL_VERSION_NOT_FOUND ' + model_version)
    if model_version == record.get('model_version'):
        record['status'] = STATUS_ACTIVE
        return record
    raise PersonalModelError('PERSONAL_ROLLBACK_REQUIRES_STORED_ARTIFACT ' + model_version)


def check_input_compatibility(record: dict, *, expected_price_contract: str,
                              global_release_id: str) -> None:
    """Fail closed unless the record can legitimately be combined with the
    current global model. Any mismatch is a hard error with a precise reason."""
    if record.get('price_input_contract') != expected_price_contract:
        raise PersonalModelError(
            'PERSONAL_PRICE_CONTRACT_MISMATCH '
            f"{record.get('price_input_contract')}!={expected_price_contract}")
    if record.get('feature_sha') != engine_model.ENGINE_SHA:
        raise PersonalModelError('PERSONAL_FEATURE_SHA_MISMATCH')
    if int(record.get('feature_dim') or 0) != 131:
        raise PersonalModelError('PERSONAL_FEATURE_DIM_MISMATCH')
    if record.get('global_release_id') != global_release_id:
        raise PersonalModelError(
            'PERSONAL_GLOBAL_RELEASE_MISMATCH '
            f"{record.get('global_release_id')}!={global_release_id}")
    if record.get('mode') not in MODES:
        raise PersonalModelError('PERSONAL_MODE_UNKNOWN ' + str(record.get('mode')))
    if record.get('mode') == MODE_FALLBACK and record.get('status') == STATUS_ACTIVE:
        raise PersonalModelError('PERSONAL_FALLBACK_CANNOT_BE_ACTIVE')


def describe(record: dict | None, symbol: str) -> dict:
    """Machine-readable scoring provenance for one symbol."""
    if record is None:
        return {'symbol': symbol, 'scoring_mode': 'GLOBAL_ONLY',
                'personal_release_id': None, 'personal_mode': None,
                'reason': 'NO_ACTIVE_PERSONAL_MODEL'}
    return {'symbol': symbol, 'scoring_mode': 'GLOBAL_PLUS_PERSONAL',
            'personal_release_id': record.get('release_id'),
            'personal_mode': record.get('mode'),
            'reason': record.get('fallback_reason')}
