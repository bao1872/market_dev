"""Pure filesystem contracts; no database or real market task execution."""
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.strategy_integration import worker
from app.strategy_integration.job_store import enqueue, list_jobs, read_job
from app.strategy_integration.release_store import atomic_json, resolve_release_batch
from app.strategy_integration.runtime_status import read_runtime_status


def test_queue_deduplicates_live_payload_and_scheduled_identity(tmp_path):
    payload = {'trade_date': '2026-10-16'}
    first = enqueue(tmp_path, 'PREDICT', payload, identity='a'*32)
    assert enqueue(tmp_path, 'PREDICT', payload)['job_id'] == first['job_id']
    path = tmp_path/'queue'/('a'*32+'.json')
    record = json.loads(path.read_text())
    record['status'] = 'FAILED'
    atomic_json(tmp_path/'failed'/path.name, record)
    path.unlink()
    assert enqueue(tmp_path, 'PREDICT', payload, identity='a'*32)['status'] == 'FAILED'
    retry = enqueue(tmp_path, 'PREDICT', payload, retry_of=first['job_id'])
    assert read_job(tmp_path, retry['job_id'])['retry_of'] == first['job_id']
    assert len(list_jobs(tmp_path)) == 2
    with pytest.raises(ValueError, match='INVALID_JOB_ID'):
        read_job(tmp_path, '../untrusted')


def test_runtime_stale_heartbeat_disables_capabilities(tmp_path):
    assert read_runtime_status(tmp_path)['status'] == 'NOT_RUNNING'
    atomic_json(tmp_path/'runtime.json', {'status': 'AVAILABLE', 'can_train': True,
        'can_predict': True, 'updated_at': (datetime.now(UTC)-timedelta(minutes=3)).isoformat()})
    state = read_runtime_status(tmp_path)
    assert state['status'] == 'STALE_HEARTBEAT' and not state['can_train'] and not state['can_predict']


def test_crashed_job_recovered_once_and_empty_success_is_failure(tmp_path, monkeypatch):
    jobs, snapshots = tmp_path/'jobs', tmp_path/'snapshots'
    monkeypatch.setenv('PANJI_STRATEGY_WORKER_ENABLED', '1')
    monkeypatch.setenv('PANJI_STRATEGY_JOB_ROOT', str(jobs))
    monkeypatch.setenv('PANJI_STRATEGY_SNAPSHOT_ROOT', str(snapshots))
    monkeypatch.setattr('app.strategy_integration.runtime_status.publish_runtime_status', lambda *a: {})
    enqueue(jobs, 'PREDICT', {'trade_date': '2026-10-15'}, identity='a'*32)
    old = read_job(jobs, 'a'*32)
    old['status'] = 'RUNNING'
    atomic_json(jobs/'running'/('a'*32+'.json'), old)
    (jobs/'queue'/('a'*32+'.json')).unlink()
    job = enqueue(jobs, 'PREDICT', {'trade_date': '2026-10-16'})
    monkeypatch.setattr(worker, '_run_with_progress', lambda *a, **kw: (0, 'no result'))
    assert worker.process_one()['status'] == 'FAILED'
    assert read_job(jobs, job['job_id'])['error'] == 'JOB_RESULT_MISSING'
    assert read_job(jobs, 'a'*32)['error'] == 'INTERRUPTED_WORKER_RESTART'
    assert worker.process_one()['status'] == 'NO_QUEUED_JOBS'


def test_latest_same_model_batch_resolves_in_both_layouts(tmp_path):
    for name, day in [('20261015', '2026-10-15'), ('releases/frozen', '2026-10-16')]:
        atomic_json(tmp_path/name/'manifest.json', {'release_id': 'a'*64, 'as_of': day})
    assert resolve_release_batch(tmp_path, 'a'*64) == tmp_path/'releases/frozen'
