"""Honest capability advertisement from the isolated strategy process."""
from __future__ import annotations

import importlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from .release_store import atomic_json, resolve_active_release


def publish_runtime_status(jobs, snapshots):
    from .fourteen_adapter import PACKAGE, is_package, verify_package, verify_production_package
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
            manifest, _ = verify_production_package(path, active.get('bundle_sha256'))
            predictable = manifest.get('price_input_contract') == 'QFQ_ASOF_V1'
    except (OSError, ValueError, KeyError) as exc:
        reason = type(exc).__name__
    status = {'status': 'AVAILABLE' if available else 'MISSING_ML_RUNTIME',
        'updated_at': datetime.now(UTC).isoformat(), 'can_train': False,
        'can_validate_delivery': available,
        'can_predict': predictable, 'reason': reason,
        'active_release_id': (active or {}).get('release_id'), 'model_count': 14}
    atomic_json(Path(jobs)/'runtime.json', status)
    return status


def read_runtime_status(root):
    path = Path(root)/'runtime.json'
    if not path.is_file():
        return {'status': 'NOT_RUNNING', 'can_train': False, 'can_predict': False, 'can_validate_delivery': False}
    try:
        result = json.loads(path.read_text())
        age = (datetime.now(UTC)-datetime.fromisoformat(result['updated_at'])).total_seconds()
    except (ValueError, KeyError, TypeError, OSError):
        return {'status': 'INVALID_HEARTBEAT', 'can_train': False, 'can_predict': False, 'can_validate_delivery': False}
    if age > 120 or age < -5:
        return {**result, 'status': 'STALE_HEARTBEAT', 'can_train': False, 'can_predict': False, 'can_validate_delivery': False}
    scheduler = Path(root)/'scheduler.json'
    if scheduler.is_file():
        try:
            if scheduler.stat().st_size > 16384:
                raise ValueError('SCHEDULER_STATE_TOO_LARGE')
            state = json.loads(scheduler.read_text())
            if (not isinstance(state, dict) or state.get('status') not in ('CHECKED','NOT_READY','FAILED')
                    or any(state.get(k) is not None and not isinstance(state[k],str) for k in ('checked_at','error','reason'))):
                raise ValueError('INVALID_SCHEDULER_STATE')
            result['after_close'] = state
        except (ValueError, OSError):
            result['after_close'] = {'status':'INVALID_STATE'}
    return result
