"""One bounded durable queue owner shared by admin requests and the worker."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .release_store import atomic_json

FOLDERS = ('running', 'queue', 'finished', 'failed')


def read_job(root, job_id):
    if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
        raise ValueError('INVALID_JOB_ID')
    for folder in FOLDERS:
        path = Path(root)/folder/(job_id+'.json')
        if path.is_file():
            result = json.loads(path.read_text())
            progress = Path(root)/'progress'/(job_id+'.json')
            if progress.is_file():
                result['progress'] = json.loads(progress.read_text())
            return result
    return None


def job_identity(kind, payload):
    """One identity owner for scheduled, manual, and delivery evaluation requests."""
    return hashlib.sha256(f'{kind}:'.encode()+json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]


def enqueue(root, kind, payload, *, retry_of=None, identity=None):
    if kind not in ('TRAIN', 'PREDICT', 'EVALUATE'):
        raise ValueError('INVALID_JOB_KIND')
    root = Path(root)
    # All request writers share this small critical section; no network or ML.
    import fcntl
    root.mkdir(parents=True, exist_ok=True)
    with (root/'queue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if identity is None and kind in ('PREDICT', 'EVALUATE') and not retry_of:
            identity = job_identity(kind, payload)
        attempt = 0
        if retry_of:
            parent = read_job(root, retry_of)
            if not parent or parent['status'] != 'FAILED' or parent['kind'] != kind or parent['payload'] != payload:
                raise ValueError('RETRY_PARENT_MISMATCH')
            attempt = parent.get('attempt', 0) + 1
            if attempt > 3:
                raise ValueError('RETRY_LIMIT_REACHED')
            identity = hashlib.sha256(f'STRATEGY_RETRY:{retry_of}'.encode()).hexdigest()[:32]
        if identity:
            existing = read_job(root, identity)
            if existing:
                return {'job_id': identity, 'status': existing['status'], 'deduplicated': True}
        for folder in ('queue', 'running'):
            paths = sorted((root/folder).glob('*.json'))
            if len(paths) >= 100:
                raise ValueError('STRATEGY_QUEUE_LIMIT')
            for path in paths:
                old = json.loads(path.read_text())
                if old['kind'] == kind and old['payload'] == payload:
                    return {'job_id': old['job_id'], 'status': old['status'], 'deduplicated': True}
        job = {'job_id': identity or uuid.uuid4().hex, 'kind': kind, 'status': 'QUEUED',
            'created_at': datetime.now(UTC).isoformat(), 'payload': payload, 'retry_of': retry_of, 'attempt': attempt}
        atomic_json(root/'queue'/(job['job_id']+'.json'), job)
        return {'job_id': job['job_id'], 'status': 'QUEUED'}


def list_jobs(root, limit=30):
    paths = []
    for folder in FOLDERS:
        paths.extend((Path(root)/folder).glob('*.json'))
    paths.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
    return [read_job(root, p.stem) for p in paths[:min(limit, 50)]]


def recover_failed(root, identity, *, now=None):
    """Follow one bounded retry lineage. Only the worker's explicit transient flag qualifies."""
    now = now or datetime.now(UTC)
    job = read_job(root, identity)
    for _ in range(4):
        if not job or job['status'] != 'FAILED' or not job.get('retryable'):
            return job
        attempt = job.get('attempt', 0)
        if attempt >= 3:
            return job
        child_id = hashlib.sha256(f"STRATEGY_RETRY:{job['job_id']}".encode()).hexdigest()[:32]
        child = read_job(root, child_id)
        if child:
            job = child
            continue
        ended = datetime.fromisoformat(job['ended_at'])
        if now < ended + timedelta(seconds=60 * 2**attempt):
            return job
        result = enqueue(root, job['kind'], job['payload'], retry_of=job['job_id'])
        return read_job(root, result['job_id'])
    return job


def failure_details(exc):
    """Conservative shared classification for the worker and its child process."""
    import errno

    from sqlalchemy.exc import DBAPIError
    transient = isinstance(exc, TimeoutError) or (
        isinstance(exc, OSError) and exc.errno in
        (errno.ECONNRESET, errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EHOSTUNREACH))
    if isinstance(exc, DBAPIError):
        sqlstate = str(getattr(exc.orig, 'sqlstate', '') or getattr(exc.orig, 'pgcode', ''))
        transient = bool(exc.connection_invalidated or sqlstate.startswith('08') or sqlstate == '57P03')
    return {'error_class':'TRANSIENT_IO' if transient else 'CONTRACT_OR_RUNTIME',
            'retryable':transient, 'error_type':type(exc).__name__, 'error':str(exc)[:800]}


def retry_source_report(root, job):
    """Oldest acquired input report in this bounded lineage fixes retry inputs."""
    reports = []
    parent_id = job.get('retry_of')
    for _ in range(3):
        if not parent_id:
            break
        parent = read_job(root, parent_id)
        if not parent:
            raise ValueError('RETRY_PARENT_MISSING')
        path = Path(root)/'outputs'/parent_id/'source_report.json'
        if path.is_file():
            if path.is_symlink() or path.stat().st_size > 64*1024*1024:
                raise ValueError('RETRY_SOURCE_REPORT_INVALID')
            reports.append(json.loads(path.read_text()))
        parent_id = parent.get('retry_of')
    if parent_id:
        raise ValueError('RETRY_LINEAGE_LIMIT')
    return reports[-1] if reports else None
