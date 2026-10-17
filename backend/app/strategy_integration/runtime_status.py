"""Honest capability advertisement from the isolated strategy process."""
from __future__ import annotations

import importlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from .release_store import atomic_json, resolve_active_release


def publish_runtime_status(jobs, snapshots):
    from .fourteen_adapter import PACKAGE, is_package, verify_package
    reason = None
    try:
        importlib.import_module('lightgbm')
        verify_package(PACKAGE)
        from app.services.strategy_tv_zigzag import identity
        identity()
        if not shutil.which('node'):
            raise ValueError('TV_ZIGZAG_NODE_RUNTIME_MISSING')
        available = True
    except (ImportError, OSError, ValueError) as exc:
        available, reason = False, type(exc).__name__
    active, predictable = None, False
    try:
        active = resolve_active_release(Path(snapshots))
        path = Path((active or {}).get('bundle_path') or '.')
        if available and active and is_package(path):
            manifest, _ = verify_package(path, active.get('bundle_sha256'))
            predictable = manifest.get('price_input_contract') == 'QFQ_ASOF_V1'
    except (OSError, ValueError, KeyError) as exc:
        reason = type(exc).__name__
    status = {'status': 'AVAILABLE' if available else 'MISSING_ML_RUNTIME',
        'updated_at': datetime.now(UTC).isoformat(), 'can_train': available,
        'can_predict': predictable, 'reason': reason,
        'active_release_id': (active or {}).get('release_id'), 'model_count': 14}
    atomic_json(Path(jobs)/'runtime.json', status)
    return status


def read_runtime_status(root):
    path = Path(root)/'runtime.json'
    if not path.is_file():
        return {'status': 'NOT_RUNNING', 'can_train': False, 'can_predict': False}
    try:
        result = json.loads(path.read_text())
        age = (datetime.now(UTC)-datetime.fromisoformat(result['updated_at'])).total_seconds()
    except (ValueError, KeyError, TypeError, OSError):
        return {'status': 'INVALID_HEARTBEAT', 'can_train': False, 'can_predict': False}
    if age > 120 or age < -5:
        return {**result, 'status': 'STALE_HEARTBEAT', 'can_train': False, 'can_predict': False}
    return result
