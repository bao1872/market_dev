"""Per-symbol incremental training state and LIVE_REFIT candidate generation.

Rules enforced here:
  * every symbol records its last processed trade date, last matured event,
    training-data SHA and model version;
  * a new bar only re-processes the affected candidates/features/labels, never
    the unchanged history;
  * research mode always keeps the shared 24/6/3 month isolation;
  * a live update produces a NEW candidate version (LIVE_REFIT). Published
    weights are never overwritten in place, and rollback is always available.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from . import personal_model as pm
from . import rolling_windows as rw

INCREMENTAL_SCHEMA = 'panji-strategy-incremental-state-v1'
LIVE_REFIT_KIND = 'LIVE_REFIT'

DEFAULT_STATE_DIR = Path(os.environ.get(
    'PANJI_STRATEGY_INCREMENTAL_STATE', '/tmp/panji_strategy_incremental'))
STATE_FILE = 'incremental_state.json'


class IncrementalError(RuntimeError):
    pass


def state_path(root: Path | str | None = None) -> Path:
    base = Path(root) if root else DEFAULT_STATE_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / STATE_FILE


def load_state(root: Path | str | None = None) -> dict:
    path = state_path(root)
    if not path.is_file():
        return {'schema': INCREMENTAL_SCHEMA, 'symbols': {}, 'runs': []}
    try:
        blob = json.loads(path.read_text(encoding='utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IncrementalError('INCREMENTAL_STATE_INVALID_JSON') from exc
    if not isinstance(blob, dict) or blob.get('schema') != INCREMENTAL_SCHEMA:
        raise IncrementalError('INCREMENTAL_STATE_SCHEMA_MISMATCH')
    blob.setdefault('symbols', {})
    blob.setdefault('runs', [])
    return blob


def save_state(state: dict, root: Path | str | None = None) -> Path:
    path = state_path(root)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True,
                              indent=2, allow_nan=False), encoding='utf-8')
    os.replace(tmp, path)
    return path


def data_sha256(frames: dict[str, pd.DataFrame]) -> str:
    """Content hash of the per-symbol training frames actually used."""
    h = hashlib.sha256()
    for symbol in sorted(frames):
        h.update(symbol.encode())
        h.update(str(len(frames[symbol])).encode())
        h.update(pd.util.hash_pandas_object(
            frames[symbol], index=False).to_numpy().tobytes())
    return h.hexdigest()


def plan_incremental(state: dict, symbol: str, available_dates: list) -> dict:
    """What must be re-processed for this symbol, without touching history."""
    record = state['symbols'].get(symbol)
    dates = sorted(set(available_dates))
    if not record or not record.get('last_trade_date'):
        return {'symbol': symbol, 'action': 'FULL_INITIAL_BUILD',
                'reprocess_from': dates[0].isoformat() if dates else None,
                'new_bars': len(dates), 'reason': 'NO_PRIOR_STATE'}
    last = pd.Timestamp(record['last_trade_date']).date()
    new_dates = [d for d in dates if d > last]
    if not new_dates:
        return {'symbol': symbol, 'action': 'NO_CHANGE', 'reprocess_from': None,
                'new_bars': 0, 'reason': 'NO_NEW_TRADING_DAYS'}
    # Only the newest bars can change a candidate's stage observations or make a
    # previously unresolved label mature; older history is reused as-is.
    return {'symbol': symbol, 'action': 'INCREMENTAL_UPDATE',
            'reprocess_from': last.isoformat(),
            'new_bars': len(new_dates),
            'new_date_range': f'{new_dates[0].isoformat()}..{new_dates[-1].isoformat()}',
            'reason': 'NEW_TRADING_DAYS'}


def update_symbol_state(state: dict, symbol: str, *, last_trade_date,
                        last_matured_event_date, training_data_sha256: str,
                        model_version: str, windows: dict,
                        kind: str = LIVE_REFIT_KIND) -> dict:
    entry = state['symbols'].setdefault(symbol, {})
    previous_version = entry.get('model_version')
    entry.update({
        'last_trade_date': str(last_trade_date),
        'last_matured_event_date': (str(last_matured_event_date)
                                    if last_matured_event_date else None),
        'training_data_sha256': training_data_sha256,
        'model_version': model_version,
        'previous_model_version': previous_version,
        'windows': windows['windows'],
        'cutoff_month': windows['cutoff_month'],
        'update_kind': kind,
        'rollback_available': bool(previous_version),
        'rollback_target': previous_version,
    })
    return entry


def record_run(state: dict, *, kind: str, as_of: str, symbols: list[str],
               detail: dict) -> dict:
    run = {'kind': kind, 'as_of': as_of, 'symbols': len(symbols),
           'detail': detail, 'run_id': hashlib.sha256(
               json.dumps([kind, as_of, symbols], sort_keys=True).encode()
           ).hexdigest()[:16]}
    state['runs'].append(run)
    return run


def build_live_refit_candidate(*, symbol: str, record: dict, windows: dict,
                               global_release_id: str, data_sha256: str,
                               test_assessment: dict) -> dict:
    """A NEW candidate version; never overwrites a published personal model."""
    if not record or record.get('mode') == pm.MODE_FALLBACK:
        raise IncrementalError('PERSONAL_FALLBACK_NOT_REFITTABLE ' + symbol)
    if test_assessment.get('status') != 'TEST_WINDOW_COMPLETE':
        # A live refit may still be produced, but it can never be presented as
        # having passed a complete three-month independent test.
        pass
    candidate = dict(record)
    candidate['status'] = pm.STATUS_CANDIDATE
    candidate['update_kind'] = LIVE_REFIT_KIND
    candidate['data_sha256'] = data_sha256
    candidate['global_release_id'] = global_release_id
    candidate['windows'] = rw.json_safe_windows(windows)
    candidate['independent_test_status'] = test_assessment.get('status')
    candidate['independent_test_claim'] = (
        'NOT_CLAIMED' if test_assessment.get('status') != 'TEST_WINDOW_COMPLETE'
        else 'THREE_COMPLETE_MONTHS')
    pm.set_identity(candidate)
    return candidate


def check_window_isolation(windows: dict) -> None:
    """Research mode must always keep the shared 24/6/3 month isolation."""
    for name in rw.SPLIT_NAMES:
        if name not in windows['windows']:
            raise rw.WindowError('MISSING_SPLIT_' + name.upper())
    if not (windows['windows']['train'][1] < windows['windows']['val'][0]
            and windows['windows']['val'][1] < windows['windows']['test'][0]):
        raise rw.WindowError('WINDOW_ISOLATION_VIOLATION')
