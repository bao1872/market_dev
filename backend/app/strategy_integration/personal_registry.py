"""Per-symbol personal model registry (persistent, outside the repository).

The registry is the single place that records, per symbol: training state,
parameters, data hash, version lineage and evaluation results. It never
activates a model implicitly and never mutates a published record in place.
"""
from __future__ import annotations

from pathlib import Path

from . import personal_model as _impl
from .personal_model import (  # re-exported public surface
    MODE_FALLBACK,
    MODE_REGULARIZED,
    MODE_STANDALONE,
    MODES,
    PERSONAL_SCHEMA,
    STATUS_ACTIVE,
    STATUS_CANDIDATE,
    STATUS_RETIRED,
    PersonalModelError,
    check_input_compatibility,
    describe,
    history_for,
    load_registry,
    new_record,
    record_identity,
    register,
    registry_path,
    resolve_active,
    rollback_to,
    save_registry,
    set_identity,
)

__all__ = [
    'MODE_FALLBACK', 'MODE_REGULARIZED', 'MODE_STANDALONE', 'MODES',
    'PERSONAL_SCHEMA', 'STATUS_ACTIVE', 'STATUS_CANDIDATE', 'STATUS_RETIRED',
    'PersonalModelError', 'check_input_compatibility', 'describe',
    'history_for', 'load_registry', 'new_record', 'record_identity',
    'register', 'registry_path', 'resolve_active', 'rollback_to',
    'save_registry', 'set_identity', 'activate', 'registry_summary',
]

DEFAULT_REGISTRY_DIR = _impl.DEFAULT_REGISTRY_DIR


def activate(registry: dict, symbol: str, model_version: str | None = None) -> dict:
    """Activate a symbol's candidate (or a specific historical version)."""
    record = registry['symbols'].get(symbol)
    if record is None:
        raise PersonalModelError('PERSONAL_MODEL_NOT_FOUND ' + symbol)
    if model_version and model_version != record.get('model_version'):
        return rollback_to(registry, symbol, model_version)
    if record.get('mode') == MODE_FALLBACK:
        raise PersonalModelError(
            'PERSONAL_FALLBACK_CANNOT_BE_ACTIVATED ' + symbol)
    record['status'] = STATUS_ACTIVE
    return record


def registry_summary(registry: dict) -> dict:
    """Counts by mode/status, for reporting and tests."""
    modes = {}
    statuses = {}
    for record in registry['symbols'].values():
        modes[record.get('mode')] = modes.get(record.get('mode'), 0) + 1
        statuses[record.get('status')] = statuses.get(record.get('status'), 0) + 1
    return {'symbols': len(registry['symbols']), 'by_mode': modes,
            'by_status': statuses}


def registry_dir(root: Path | str | None = None) -> Path:
    return Path(registry_path(root)).parent
