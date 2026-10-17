"""One bounded durable queue owner shared by admin requests and the worker."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
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


def enqueue(root, kind, payload, *, retry_of=None, identity=None):
    if kind not in ('TRAIN', 'PREDICT', 'EVALUATE'):
        raise ValueError('INVALID_JOB_KIND')
    root = Path(root)
    # All request writers share this small critical section; no network or ML.
    import fcntl
    root.mkdir(parents=True, exist_ok=True)
    with (root/'queue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
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
            'created_at': datetime.now(UTC).isoformat(), 'payload': payload, 'retry_of': retry_of}
        atomic_json(root/'queue'/(job['job_id']+'.json'), job)
        return {'job_id': job['job_id'], 'status': 'QUEUED'}


def list_jobs(root, limit=30):
    paths = []
    for folder in FOLDERS:
        paths.extend((Path(root)/folder).glob('*.json'))
    paths.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
    return [read_job(root, p.stem) for p in paths[:min(limit, 50)]]
