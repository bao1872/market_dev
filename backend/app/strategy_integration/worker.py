"""Operator-enabled durable job worker and optional isolated after-close loop.

Not called from FastAPI startup, existing DSA scheduler or process import.
This may run only after production deploy authorization and readiness checks.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from .release_store import activate_batch, atomic_json


def enabled() -> bool:
    return os.getenv('PANJI_STRATEGY_WORKER_ENABLED') == '1'


def _predict_cmd(runtime, bars, inst, trade_date, export_dir, snap_root,
                 registry: str = '') -> list[str]:
    """Build the PREDICT subprocess command.

    The operator must point ``registry`` at the activated per-symbol model registry
    (``PANJI_STRATEGY_PERSONAL_REGISTRY``); when set, the build reads it so symbols
    with an active personal model are scored GLOBAL_PLUS_PERSONAL and the rest are
    explicitly GLOBAL_ONLY (never impersonated).
    """
    cmd = [runtime, '-m', 'app.strategy_integration.cli', 'build',
           '--source-mode', 'canonical',
           '--bars', str(bars), '--instruments', str(inst),
           '--as-of', trade_date,
           '--export-dir', str(export_dir),
           '--output-dir', str(snap_root)]
    if registry and Path(registry).is_absolute():
        cmd += ['--personal-registry', registry]
    return cmd


def _train_cmd(runtime: str, bars: Path, instruments: Path, cfg: dict,
               output_dir: Path, export_dir: Path) -> list[str]:
    """Build ONLY a canonical PIT training command with frozen 24/6/3 windows.

    Never trust user-configured split dates independently from rolling_windows.
    """
    from datetime import date as _date

    from . import rolling_windows as rw
    as_of = _date.fromisoformat(cfg['as_of'])
    expected = rw.rolling_windows(as_of)['windows']
    for key in ('train','val','test'):
        if cfg[key] != {'start':expected[key][0], 'end':expected[key][1]}:
            raise RuntimeError('TRAIN_WINDOW_NOT_ROLLING_24_6_3')
    if not 50 <= int(cfg['trees']) <= 2000:
        raise RuntimeError('TRAIN_TREES_OUT_OF_RANGE')
    return [runtime, '-m','app.strategy_integration.training',
            '--source-mode','canonical',
            '--as-of',as_of.isoformat(),
            '--bars',str(bars),'--instruments',str(instruments),
            '--export-dir',str(export_dir),
            '--output-dir',str(output_dir),
            '--trees',str(cfg['trees']), '--threads', str(max(1, min(4, int(os.getenv('PANJI_STRATEGY_THREADS', '1'))))),
            '--model-family', cfg.get('model_family', 'fourteen')]



def _roots() -> tuple[Path,Path,Path,Path]:
    if not enabled():
        raise RuntimeError('WORKER_DISABLED_PENDING_DEPLOY_APPROVAL')
    r=os.getenv('PANJI_STRATEGY_JOB_ROOT','')
    o=os.getenv('PANJI_STRATEGY_SNAPSHOT_ROOT','')
    bars=os.getenv('PANJI_STRATEGY_BARS_FILE',str(Path(r)/'exports'/'bars.parquet'))
    inst=os.getenv('PANJI_STRATEGY_INSTRUMENTS_FILE',str(Path(r)/'exports'/'instruments.parquet'))
    if not all(x and Path(x).is_absolute() for x in (r,o,bars,inst)):
        raise RuntimeError('WORKER_PATHS_NOT_CONFIGURED')
    return Path(r),Path(o),Path(bars),Path(inst)


def _run_with_progress(cmd, job, jobs, *, lock_fd):
    import signal
    import time

    from .job_store import read_job
    logs = jobs/'logs'
    logs.mkdir(parents=True, exist_ok=True)
    path = logs/(job['job_id']+'.log')
    env = dict(os.environ)
    env['PANJI_STRATEGY_PROGRESS_FILE'] = str(jobs/'progress'/(job['job_id']+'.json'))
    env['PANJI_STRATEGY_CACHE_ROOT'] = str(jobs/'prefix-cache')
    env['PANJI_STRATEGY_SOURCE_REPORT_FILE'] = str(jobs/'outputs'/job['job_id']/'source_report.json')
    stall = max(600, int(os.getenv('PANJI_STRATEGY_STALL_SECONDS', '1800')))
    changed, previous = time.monotonic(), None
    heartbeat = time.monotonic()
    with path.open('ab') as log:
        process = subprocess.Popen(cmd, stdout=log, stderr=log, env=env,
            cwd=os.environ.get('PANJI_STRATEGY_CODE_ROOT') or None,
            start_new_session=True, pass_fds=(lock_fd,))
        try:
            while process.poll() is None:
                if time.monotonic() - heartbeat >= 30:
                    from .runtime_status import publish_runtime_status
                    publish_runtime_status(jobs, Path(os.environ['PANJI_STRATEGY_SNAPSHOT_ROOT']))
                    heartbeat = time.monotonic()
                state = read_job(jobs, job['job_id']) or {}
                value = state.get('progress', {})
                token = (value.get('stage'), value.get('completed'))
                if token != previous and token != (None, None):
                    previous, changed = token, time.monotonic()
                if time.monotonic()-changed > stall:
                    raise RuntimeError('STRATEGY_JOB_STALLED_NO_REAL_PROGRESS')
                time.sleep(1)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    with path.open('rb') as log:
        log.seek(max(0, path.stat().st_size-6000))
        tail = log.read().decode('utf-8', errors='replace')
    return process.returncode, tail


def process_one() -> dict:
    import fcntl
    jobs, snap_root, bars, inst = _roots()
    jobs.mkdir(parents=True, exist_ok=True)
    from .runtime_status import publish_runtime_status
    publish_runtime_status(jobs, snap_root)
    with (jobs/'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'WORKER_BUSY'}
        # The child inherits this lock. A restarted worker cannot recover or
        # claim work while an orphaned fitting/scoring process is still alive.
        for running in sorted((jobs/'running').glob('*.json')):
            old = json.loads(running.read_text())
            old.update(status='FAILED', error='INTERRUPTED_WORKER_RESTART', retryable=True,
                       ended_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat())
            atomic_json(jobs/'failed'/running.name, old)
            running.unlink()
        for req in sorted((jobs/'queue').glob('*.json')):
            running = jobs/'running'/req.name
            running.parent.mkdir(parents=True, exist_ok=True)
            os.replace(req, running)
            job = json.loads(running.read_text())
            if job.get('status') != 'QUEUED':
                raise RuntimeError('INVALID_JOB_STATE')
            job.update(status='RUNNING', started_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat())
            atomic_json(running, job)
            payload, kind = job['payload'], job['kind']
            runtime = os.getenv('PANJI_STRATEGY_PYTHON', sys.executable)
            try:
                if kind == 'PREDICT':
                    from .release_store import resolve_active_release
                    active = resolve_active_release(snap_root)
                    if not active:
                        raise RuntimeError('APPROVED_PRODUCTION_MODEL_REQUIRED')
                    for key in ('release_id', 'bundle_sha256'):
                        if not payload.get(key) or payload[key] != active.get(key):
                            raise RuntimeError('PREDICTION_PINNED_MODEL_CHANGED')
                    cmd = _predict_cmd(runtime, bars, inst, payload['trade_date'], jobs/'exports'/job['job_id'], snap_root)
                    cmd += ['--expected-active-release-id', payload['release_id'],
                            '--expected-active-bundle-sha', payload['bundle_sha256']]
                elif kind == 'EVALUATE':
                    from .fourteen_adapter import verify_package
                    from .job_store import read_job
                    if payload.get('delivery_id'):
                        from .lab_delivery import SHA, validate_delivery
                        sha = payload['delivery_id']
                        if not isinstance(sha, str) or not SHA.fullmatch(sha):
                            raise RuntimeError('INVALID_DELIVERY_ID')
                        package = jobs/'deliveries'/sha
                        # Verification belongs to this isolated ML runtime, never the API.
                        validate_delivery(package, sha)
                    else:
                        # Transitional read/verification path for the already-running TRAIN.
                        training = read_job(jobs, payload['training_job_id'])
                        if not training or training['kind'] != 'TRAIN' or training['status'] != 'SUCCEEDED':
                            raise RuntimeError('TRAINING_CANDIDATE_NOT_READY')
                        package = jobs/'outputs'/training['job_id']/'fourteen_candidate_UNPUBLISHED'
                        _meta, sha = verify_package(package)
                    cmd = _predict_cmd(runtime, bars, inst, payload['trade_date'], jobs/'exports'/job['job_id'], snap_root)
                    cmd += ['--candidate-bundle', str(package), '--candidate-sha', sha]
                    if payload.get('delivery_id'):
                        cmd += ['--lab-delivery-id', sha]
                    diagnostic = payload.get('diagnostic_max_symbols', 0)
                    if not isinstance(diagnostic, int) or isinstance(diagnostic, bool) or not 0 <= diagnostic <= 20:
                        raise RuntimeError('INVALID_DIAGNOSTIC_SYMBOL_LIMIT')
                    if diagnostic:
                        cmd += ['--max-symbols', str(diagnostic)]
                elif kind == 'TRAIN':
                    raise RuntimeError('TRAINING_MOVED_TO_PANJI_LAB')
                else:
                    raise RuntimeError('UNKNOWN_JOB_KIND')
                if job.get('retry_of'):
                    from .cli import _canonical_source_report
                    from .job_store import retry_source_report
                    report = retry_source_report(jobs, job)
                    if report is not None:
                        cmd += ['--expected-source-sha', _canonical_source_report(report)['source_sha256']]
                code, tail = _run_with_progress(cmd, job, jobs, lock_fd=lock.fileno())
                job.update(status='SUCCEEDED' if code == 0 else 'FAILED', return_code=code, stdout_tail=tail)
                if code != 0:
                    job['error'] = f'PROCESS_EXIT_{code}'
                    job['retryable'] = False
                    lines = [line for line in tail.splitlines() if line.startswith('{')]
                    if lines:
                        failure = json.loads(lines[-1])
                        if failure.get('status') == 'FAILED':
                            job['error'] = str(failure.get('error', job['error']))[:1000]
                            job['error_class'] = failure.get('error_class')
                            job['retryable'] = failure.get('error_class') == 'TRANSIENT_IO' and failure.get('retryable') is True
                if code == 0:
                    lines = [line for line in tail.splitlines() if line.startswith('{')]
                    if not lines:
                        raise RuntimeError('JOB_RESULT_MISSING')
                    result = json.loads(lines[-1])
                    if not isinstance(result, dict) or not result:
                        raise RuntimeError('JOB_RESULT_INVALID')
                    allowed = ('CANDIDATE_UNPUBLISHED', 'CANDIDATE_TRAINED_NOT_PUBLISHED') if kind == 'TRAIN' else ('VALIDATED_STAGING_NOT_DEPLOYED', 'IDEMPOTENT_EXISTS')
                    if result.get('status') not in allowed:
                        raise RuntimeError('JOB_RESULT_NOT_VALIDATED')
                    job['result'] = result
                    if kind == 'PREDICT' and os.getenv('PANJI_STRATEGY_AUTOPUBLISH_ENABLED') == '1':
                        batch = Path(result.get('output', ''))
                        if not batch.is_dir():
                            raise RuntimeError('NO_VALIDATED_SNAPSHOT_OUTPUT')
                        publication = activate_batch(snap_root, batch, refresh_only=True)
                        job['publication'] = publication
                        job['snapshot_pointer_activated'] = publication['status'] == 'ACTIVATED_AFTER_OPERATOR_APPROVAL'
            except Exception as exc:
                from .job_store import failure_details
                job.update(status='FAILED', **failure_details(exc))
            job['ended_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
            atomic_json(jobs/('finished' if job['status'] == 'SUCCEEDED' else 'failed')/req.name, job)
            running.unlink(missing_ok=True)
            return {'job_id': job['job_id'], 'status': job['status']}
    return {'status': 'NO_QUEUED_JOBS'}


async def scheduled_trade_day(now):
    days, _ = await scheduled_trade_days(now, limit=1)
    return days[-1] if days else None


async def scheduled_trade_days(now, *, since=None, limit=31):
    """Calendar-owned close dates, including bounded missed-day compensation."""
    from datetime import timedelta

    from app.db import AsyncSessionLocal
    from app.services.calendar_service import (
        get_most_recent_trading_day_async,
        get_previous_trading_day_async,
    )

    from .readonly_runtime import assert_readonly_session, configure_readonly_connections
    configure_readonly_connections()
    cutoff = now.date() if now.time() >= time(18, 30) else now.date()-timedelta(days=1)
    days = []
    async with AsyncSessionLocal() as db:
        if not await assert_readonly_session(db):
            raise RuntimeError('STRATEGY_DATABASE_READ_ONLY_PROOF_REQUIRED')
        day = await get_most_recent_trading_day_async(db, cutoff)
        while day is not None and (since is None or day >= since) and len(days) < limit:
            days.append(day.isoformat())
            day = await get_previous_trading_day_async(db, day)
    return list(reversed(days)), bool(day and since and day >= since)



def run_daily_loop():
    import asyncio
    asyncio.run(_daily_loop())


async def _daily_loop():
    import asyncio
    from time import monotonic

    from .job_store import enqueue, failure_details, job_identity, recover_failed
    from .producer import EVALUATION_PROTOCOL
    from .release_store import resolve_active_release
    from .runtime_status import publish_runtime_status
    jobs, root, bars, inst = _roots()
    next_scan = 0.0
    while True:
        if os.getenv('PANJI_STRATEGY_DAILY_SCHEDULER_ENABLED') == '1' and monotonic() >= next_scan:
            next_scan = monotonic()+60
            checked_at = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
            try:
                status = publish_runtime_status(jobs, root)
                active = resolve_active_release(root)
                if status['can_predict'] and active:
                    since = datetime.fromisoformat(active.get('model_activated_at', active['activated_at'])).astimezone(ZoneInfo('Asia/Shanghai')).date()
                    days, truncated = await scheduled_trade_days(datetime.now(ZoneInfo('Asia/Shanghai')), since=since)
                    for day in days:
                        payload = {k:active[k] for k in ('release_id', 'bundle_sha256')}
                        payload.update(trade_date=day, evaluation_protocol=EVALUATION_PROTOCOL)
                        identity = job_identity('PREDICT', payload)
                        enqueue(jobs, 'PREDICT', payload, identity=identity)
                        recover_failed(jobs, identity)
                    state = {'status':'CHECKED', 'checked_at':checked_at, 'trade_dates':days,
                        'model_release_id':active['release_id'], 'backlog_exceeds_31_sessions':truncated}
                else:
                    state = {'status':'NOT_READY', 'checked_at':checked_at,
                             'reason':'APPROVED_MODEL_OR_RUNTIME_REQUIRED'}
            except Exception as exc:
                state = {'status':'FAILED', 'checked_at':checked_at, **failure_details(exc)}
            atomic_json(jobs/'scheduler.json', state)
        process_one()
        await asyncio.sleep(10)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['once','daily-loop'])
    args=p.parse_args()
    if args.command=='once':
        print(json.dumps(process_one()))
    else:
        run_daily_loop()

if __name__=='__main__':
    main()
