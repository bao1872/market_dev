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
    job = enqueue(jobs, 'PREDICT', {'trade_date': '2026-10-16', 'release_id':'a'*64, 'bundle_sha256':'b'*64})
    # INVALID_FIXTURE after production gate: supply the approved-model owner result
    # so this test still reaches its intended missing-result counterexample.
    monkeypatch.setattr('app.strategy_integration.release_store.resolve_active_release',
                        lambda root: {'release_id':'a'*64, 'bundle_sha256':'b'*64})
    monkeypatch.setattr(worker, '_run_with_progress', lambda *a, **kw: (0, 'no result'))
    assert worker.process_one()['status'] == 'FAILED'
    assert read_job(jobs, job['job_id'])['error'] == 'JOB_RESULT_MISSING'
    assert read_job(jobs, 'a'*32)['error'] == 'INTERRUPTED_WORKER_RESTART'
    assert worker.process_one()['status'] == 'NO_QUEUED_JOBS'


def test_latest_same_model_batch_resolves_in_both_layouts(tmp_path):
    for name, day in [('20261015', '2026-10-15'), ('releases/frozen', '2026-10-16')]:
        atomic_json(tmp_path/name/'manifest.json', {'release_id': 'a'*64, 'as_of': day})
    assert resolve_release_batch(tmp_path, 'a'*64) == tmp_path/'releases/frozen'


def test_recovery_backoff_exact_lineage_and_bounded_attempts(tmp_path):
    from app.strategy_integration.job_store import recover_failed
    now = datetime.now(UTC)
    original = enqueue(tmp_path, 'PREDICT', {'trade_date':'2026-10-16'}, identity='c'*32)
    job_id = original['job_id']
    for attempt in range(4):
        record = read_job(tmp_path, job_id)
        assert record['attempt'] == attempt
        record.update(status='FAILED', retryable=True, ended_at=now.isoformat())
        atomic_json(tmp_path/'failed'/(job_id+'.json'), record)
        (tmp_path/'queue'/(job_id+'.json')).unlink()
        assert recover_failed(tmp_path, original['job_id'], now=now)['job_id'] == job_id
        result = recover_failed(tmp_path, original['job_id'], now=now+timedelta(hours=1))
        if attempt == 3:
            assert result['status'] == 'FAILED' and result['job_id'] == job_id
        else:
            assert result['retry_of'] == job_id
            assert result['payload'] == record['payload']
            assert recover_failed(tmp_path, original['job_id'], now=now+timedelta(hours=1))['job_id'] == result['job_id']
            job_id = result['job_id']
    assert len(list_jobs(tmp_path)) == 4


def test_contract_failures_never_retry(tmp_path):
    from app.strategy_integration.job_store import recover_failed
    job = enqueue(tmp_path, 'PREDICT', {'trade_date':'2026-10-16'})
    record = read_job(tmp_path, job['job_id'])
    record.update(status='FAILED', error='COVERAGE_INVALID', retryable=False)
    atomic_json(tmp_path/'failed'/(record['job_id']+'.json'), record)
    (tmp_path/'queue'/(record['job_id']+'.json')).unlink()
    assert recover_failed(tmp_path, record['job_id'])['status'] == 'FAILED'
    assert len(list_jobs(tmp_path)) == 1


def test_audit_batch_selection_pins_evaluation_sha(tmp_path):
    for name, sha in [('a', '1'*64), ('b', '2'*64)]:
        atomic_json(tmp_path/name/'manifest.json', {'release_id':'f'*64, 'as_of':'2026-10-16', 'evaluation_sha256':sha})
    assert resolve_release_batch(tmp_path,'f'*64,evaluation_sha256='1'*64) == tmp_path/'a'
    assert resolve_release_batch(tmp_path,'f'*64,evaluation_sha256='3'*64) is None


def test_daily_refresh_fences_model_changes_and_history_regression(tmp_path, monkeypatch):
    from app.strategy_integration import release_store as store
    bundle = tmp_path/'model.txt'
    bundle.write_bytes(b'unit-fixture')
    prior = tmp_path/'newer'
    atomic_json(prior/'manifest.json', {'as_of':'2026-10-16'})
    store.write_active_release(tmp_path, prior, 'f'*64, store.digest_path(bundle), str(bundle))
    batch = tmp_path/'older'
    atomic_json(batch/'manifest.json', {'as_of':'2026-10-15','release_id':'f'*64,
                                       'runtime':{'bundle_sha256':store.digest_path(bundle)}})
    monkeypatch.setattr(store,'_activate_batch_locked',lambda *a,**kw:pytest.fail('must not swap'))
    assert store.activate_batch(tmp_path,batch,refresh_only=True)['status'] == 'HISTORY_VALIDATED_NOT_PUBLISHED'
    atomic_json(batch/'manifest.json', {'as_of':'2026-10-16','release_id':'e'*64,'runtime':{'bundle_sha256':store.digest_path(bundle)}})
    with pytest.raises(store.PublishError,match='DAILY_REFRESH_MODEL_CHANGED'):
        store.activate_batch(tmp_path,batch,refresh_only=True)


def test_manual_and_scheduler_requests_share_terminal_identity(tmp_path):
    from app.strategy_integration.job_store import job_identity
    payload = {'trade_date':'2026-10-16','release_id':'a'*64,'bundle_sha256':'b'*64,'evaluation_protocol':'v2'}
    job = enqueue(tmp_path,'PREDICT',payload)
    assert job['job_id'] == job_identity('PREDICT',payload)
    record = read_job(tmp_path,job['job_id'])
    record['status'] = 'SUCCEEDED'
    atomic_json(tmp_path/'finished'/(job['job_id']+'.json'),record)
    (tmp_path/'queue'/(job['job_id']+'.json')).unlink()
    assert enqueue(tmp_path,'PREDICT',payload)['deduplicated']
    assert enqueue(tmp_path,'PREDICT',payload,identity=job_identity('PREDICT',payload))['status'] == 'SUCCEEDED'


def test_missing_day_compensation_uses_calendar_owner(monkeypatch):
    import asyncio
    from datetime import date
    from types import SimpleNamespace
    from app.services import calendar_service as calendar
    calls = []
    dates = [date(2026,10,13),date(2026,10,15),date(2026,10,16)]  # deliberately irregular authoritative calendar
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self,*a): pass
        async def execute(self, sql):
            assert str(sql) == 'SHOW transaction_read_only'
            return SimpleNamespace(scalar_one=lambda:'on')
    monkeypatch.setattr('app.db.AsyncSessionLocal',Session)
    monkeypatch.setenv('PANJI_STRATEGY_READ_ONLY_DATABASE','1')
    # This test has no DB; connection configuration is already owned/covered separately.
    monkeypatch.setattr('app.strategy_integration.readonly_runtime._installed',True)
    async def recent(db, cutoff):
        calls.append(cutoff)
        return max((d for d in dates if d<=cutoff),default=None)
    async def previous(db, cutoff):
        return max((d for d in dates if d<cutoff),default=None)
    monkeypatch.setattr(calendar,'get_most_recent_trading_day_async',recent)
    monkeypatch.setattr(calendar,'get_previous_trading_day_async',previous)
    now = datetime.fromisoformat('2026-10-17T10:00:00+08:00')
    days,truncated = asyncio.run(worker.scheduled_trade_days(now,since=date(2026,10,15)))
    assert days == ['2026-10-15','2026-10-16'] and not truncated
    assert calls == [date(2026,10,16)]
    days,truncated = asyncio.run(worker.scheduled_trade_days(now,since=date(2026,10,13),limit=1))
    assert days == ['2026-10-16'] and truncated


def test_transient_classification_does_not_retry_contract_or_resource_errors():
    import errno
    from app.strategy_integration.job_store import failure_details
    assert failure_details(TimeoutError('read timed out'))['retryable']
    assert failure_details(OSError(errno.ECONNRESET,'read reset'))['retryable']
    assert not failure_details(ValueError('COVERAGE_INVALID'))['retryable']
    assert not failure_details(OSError(errno.ENOSPC,'disk full'))['retryable']
    assert not failure_details(MemoryError('budget exceeded'))['retryable']


def test_worker_consumes_structured_child_failure(tmp_path, monkeypatch):
    jobs, snapshots = tmp_path/'jobs', tmp_path/'snapshots'
    monkeypatch.setenv('PANJI_STRATEGY_WORKER_ENABLED','1')
    monkeypatch.setenv('PANJI_STRATEGY_JOB_ROOT',str(jobs))
    monkeypatch.setenv('PANJI_STRATEGY_SNAPSHOT_ROOT',str(snapshots))
    monkeypatch.setattr('app.strategy_integration.runtime_status.publish_runtime_status',lambda *a:{})
    monkeypatch.setattr('app.strategy_integration.release_store.resolve_active_release',
                        lambda root:{'release_id':'a'*64,'bundle_sha256':'b'*64})
    from app.strategy_integration.job_store import failure_details
    result = enqueue(jobs,'PREDICT',{'trade_date':'2026-10-16','release_id':'a'*64,'bundle_sha256':'b'*64})
    terminal = {'status':'FAILED', **failure_details(TimeoutError('transient read timeout'))}
    monkeypatch.setattr(worker,'_run_with_progress',lambda *a,**kw:(2,json.dumps(terminal)))
    assert worker.process_one()['status'] == 'FAILED'
    job = read_job(jobs,result['job_id'])
    assert job['retryable'] and job['error_class'] == 'TRANSIENT_IO'


def test_retry_uses_first_acquired_input_identity(tmp_path):
    from app.strategy_integration.job_store import retry_source_report
    first = enqueue(tmp_path,'PREDICT',{'trade_date':'2026-10-16'})
    original = read_job(tmp_path,first['job_id'])
    original.update(status='FAILED',retryable=True,ended_at=datetime.now(UTC).isoformat())
    atomic_json(tmp_path/'failed'/(original['job_id']+'.json'),original)
    (tmp_path/'queue'/(original['job_id']+'.json')).unlink()
    atomic_json(tmp_path/'outputs'/original['job_id']/'source_report.json',{'source_marker':'original'})
    retry = enqueue(tmp_path,'PREDICT',original['payload'],retry_of=original['job_id'])
    child = read_job(tmp_path,retry['job_id'])
    assert retry_source_report(tmp_path,child) == {'source_marker':'original'}
    child.update(status='FAILED',retryable=True,ended_at=datetime.now(UTC).isoformat())
    atomic_json(tmp_path/'failed'/(child['job_id']+'.json'),child)
    (tmp_path/'queue'/(child['job_id']+'.json')).unlink()
    atomic_json(tmp_path/'outputs'/child['job_id']/'source_report.json',{'source_marker':'later'})
    retry2 = enqueue(tmp_path,'PREDICT',original['payload'],retry_of=child['job_id'])
    assert retry_source_report(tmp_path,read_job(tmp_path,retry2['job_id'])) == {'source_marker':'original'}


def test_calendar_failure_is_visible_separately_from_model_capability(tmp_path):
    atomic_json(tmp_path/'runtime.json', {'status':'AVAILABLE','can_predict':True,
        'can_validate_delivery':True,'can_train':False,'updated_at':datetime.now(UTC).isoformat()})
    atomic_json(tmp_path/'scheduler.json', {'status':'FAILED','checked_at':datetime.now(UTC).isoformat(),'error':'calendar unavailable'})
    state = read_runtime_status(tmp_path)
    assert state['after_close']['status'] == 'FAILED'
    assert state['can_predict'] is True  # capability is not a claim that the daily batch succeeded
    atomic_json(tmp_path/'scheduler.json', {'status':'FAILED','error':{'invalid':'object'}})
    assert read_runtime_status(tmp_path)['after_close']['status'] == 'INVALID_STATE'
