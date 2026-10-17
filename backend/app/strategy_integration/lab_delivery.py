"""Bounded data-only lab handoff. Never execute Python from an uploaded archive.

The existing queue executes golden-case verification in the isolated ML runtime;
the existing release store remains the only activation owner.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .fourteen_adapter import verify_package, verify_runtime_contract

MAX_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 512 * 1024
SHA = re.compile(r'^[0-9a-f]{64}$')


def receive_chunk(root: Path, data: bytes, pinned_sha: str, index: int, total_bytes: int):
    """Keep each HTTP request below the existing proxy limit; resume immutable chunks.

    At most four incomplete archives exist. Completed model assets still belong
    to receive(), and only that validated asset is submitted to the queue.
    """
    import fcntl
    import shutil
    import time

    from .release_store import atomic_bytes, atomic_json

    if not SHA.fullmatch(pinned_sha) or not 0 < total_bytes <= MAX_BYTES:
        raise ValueError('INVALID_DELIVERY_UPLOAD')
    count = (total_bytes + CHUNK_BYTES - 1) // CHUNK_BYTES
    if not 0 <= index < count or len(data) != min(CHUNK_BYTES, total_bytes-index*CHUNK_BYTES):
        raise ValueError('DELIVERY_CHUNK_SIZE_MISMATCH')
    digest = hashlib.sha256(data).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    with (root/'.upload.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = root/'.upload-receipts'/(pinned_sha+'.json')
        if receipt.is_file():
            saved = json.loads(receipt.read_text())
            if saved['total_bytes'] != total_bytes or saved['chunks'][index] != digest:
                raise ValueError('IMMUTABLE_DELIVERY_ARCHIVE_CHANGED')
            return saved['result'] if index == count-1 else {'status':'PART_ACCEPTED'}
        pending = root/'.uploads'
        pending.mkdir(exist_ok=True)
        folder = pending/pinned_sha
        if not folder.exists():
            # Only expired, explicitly owned incomplete upload chunks are disposable.
            for old in pending.iterdir():
                marker = old/'upload.json'
                if SHA.fullmatch(old.name) and not old.is_symlink() and marker.is_file():
                    metadata = json.loads(marker.read_text())
                    if metadata.get('schema') == 'PANJI_LAB_UPLOAD_V1' and time.time()-marker.stat().st_mtime > 86400:
                        shutil.rmtree(old)
            if len(list(pending.iterdir())) >= 4:
                raise ValueError('DELIVERY_PENDING_UPLOAD_LIMIT_RESUME_EXISTING')
            atomic_json(folder/'upload.json', {'schema':'PANJI_LAB_UPLOAD_V1','total_bytes':total_bytes})
        marker = json.loads((folder/'upload.json').read_text())
        if folder.is_symlink() or marker['total_bytes'] != total_bytes:
            raise ValueError('DELIVERY_UPLOAD_IDENTITY_MISMATCH')
        part = folder/f'part_{index:04d}.bin'
        if part.exists() and hashlib.sha256(part.read_bytes()).hexdigest() != digest:
            raise ValueError('IMMUTABLE_DELIVERY_CHUNK_CHANGED')
        atomic_bytes(part, data)
        parts = [folder/f'part_{i:04d}.bin' for i in range(count)]
        if not all(p.is_file() for p in parts):
            return {'status':'PART_ACCEPTED'}
        chunks = [p.read_bytes() for p in parts]
        result = receive(root, b''.join(chunks), pinned_sha)
        atomic_json(receipt, {'total_bytes':total_bytes,
            'chunks':[hashlib.sha256(chunk).hexdigest() for chunk in chunks], 'result':result})
        shutil.rmtree(folder)
        return result


def validate_delivery(root: Path, pinned_sha: str):
    if not SHA.fullmatch(pinned_sha):
        raise ValueError('INVALID_DELIVERY_SHA')
    manifest, sha = verify_package(root, pinned_sha)
    verify_runtime_contract(manifest)
    if manifest.get('price_input_contract') != 'QFQ_ASOF_V1':
        raise ValueError('DELIVERY_PRICE_CONTRACT_UNVERIFIED')
    delivery = json.loads((root/'delivery.json').read_text())
    if (delivery.get('schema') != 'PANJI_LAB_DELIVERY_V1'
            or delivery.get('manifest_sha256') != sha
            or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', delivery.get('lab_revision', ''))
            or not isinstance(delivery.get('approved_for_production'), bool)):
        raise ValueError('LAB_APPROVAL_IDENTITY_REQUIRED')
    for name in ('validation_report.json', 'golden_cases.json'):
        if hashlib.sha256((root/name).read_bytes()).hexdigest() != delivery.get('files', {}).get(name):
            raise ValueError('DELIVERY_EVIDENCE_HASH_MISMATCH:' + name)
    report = json.loads((root/'validation_report.json').read_text())
    if set(report.get('tasks', {})) != set(manifest['models']):
        raise ValueError('DELIVERY_TASK_REPORT_INCOMPLETE')
    previous = None
    from datetime import date
    for split in ('train', 'val', 'test'):
        bounds = report.get('windows', {}).get(split, {})
        start, end = date.fromisoformat(bounds['start']), date.fromisoformat(bounds['end'])
        if start > end or (previous and previous >= start):
            raise ValueError('DELIVERY_SPLIT_OVERLAP')
        previous = end
        if manifest.get('split_windows', {}).get(split) != [start.isoformat(), end.isoformat()]:
            raise ValueError('DELIVERY_SPLIT_CONTRACT_MISMATCH')
        for task in manifest['models']:
            metrics = report['tasks'][task][split]
            if not isinstance(metrics.get('n'), int) or metrics['n'] < 0:
                raise ValueError('DELIVERY_SAMPLE_COUNT_INVALID')
            for key in ('precision', 'recall', 'auc'):
                if key not in metrics:
                    raise ValueError('DELIVERY_METRIC_MISSING')
                value = metrics[key]
                if value is None and not metrics.get('unavailable_reason'):
                    raise ValueError('DELIVERY_METRIC_UNAVAILABLE_WITHOUT_REASON')
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                          or not math.isfinite(value) or not 0 <= value <= 1):
                    raise ValueError('DELIVERY_METRIC_INVALID')
    if previous is None or previous.isoformat() > manifest['data_end']:
        raise ValueError('DELIVERY_TEST_AFTER_DATA_END')
    cases = json.loads((root/'golden_cases.json').read_text())
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError('DELIVERY_GOLDEN_CASES_REQUIRED')
    if sum(len(c.get('bars', [])) for c in cases) > 100000:
        raise ValueError('DELIVERY_GOLDEN_TOTAL_HISTORY_LIMIT')
    if not set(manifest['models']).issubset({s['task'] for c in cases for s in c['expected']['signals']}):
        raise ValueError('DELIVERY_GOLDEN_TASK_COVERAGE_INCOMPLETE')
    return manifest, delivery, cases


def receive(root: Path, archive: bytes, pinned_sha: str):
    if len(archive) > MAX_BYTES:
        raise ValueError('DELIVERY_ARCHIVE_TOO_LARGE')
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.receive-', dir=root) as temporary:
        staging = Path(temporary)
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            entries = zipped.infolist()
            if len(entries) > 100 or sum(e.file_size for e in entries) > MAX_BYTES:
                raise ValueError('DELIVERY_EXPANDED_LIMIT')
            seen = set()
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                if (not name or '\\' in name or path.is_absolute() or '..' in path.parts
                        or str(path) != name.rstrip('/') or name in seen
                        or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1):
                    raise ValueError('UNSAFE_DELIVERY_PATH')
                seen.add(name)
                if not entry.is_dir():
                    target = staging.joinpath(*path.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zipped.read(entry))
        manifest, delivery, _ = validate_delivery(staging, pinned_sha)
        allowed = set(manifest['files']) | {'manifest.json', 'delivery.json', 'validation_report.json', 'golden_cases.json'}
        if {str(p.relative_to(staging)) for p in staging.rglob('*') if p.is_file()} != allowed:
            raise ValueError('UNDECLARED_DELIVERY_FILE')
        destination = root/pinned_sha
        if destination.exists():
            validate_delivery(destination, pinned_sha)
            if any((destination/name).read_bytes() != (staging/name).read_bytes() for name in allowed):
                raise ValueError('DELIVERY_IDENTITY_CHANGED_EVIDENCE')
        else:
            os.rename(staging, destination)
        return {'delivery_id': pinned_sha, 'status': 'RECEIVED_NOT_ACTIVATED',
                'model_version': manifest['version'], 'lab_revision': delivery['lab_revision']}


def verify_golden(root: Path, pinned_sha: str):
    """Runs only inside the isolated ML worker, using trusted production runtime."""
    from importlib.metadata import version

    import numpy as np
    import pandas as pd

    from .fourteen_adapter import load_package
    manifest, _, cases = validate_delivery(root, pinned_sha)
    versions = {name:version(name) for name in ('numpy','pandas','lightgbm','pyarrow')}
    if any(actual != manifest['runtime_versions'].get(name) for name, actual in versions.items()):
        raise ValueError('DELIVERY_RUNTIME_DEPENDENCY_MISMATCH')
    bundle, _ = load_package(root, pinned_sha)
    runtime = bundle['fourteen_runtime']
    for case in cases:
        raw = runtime.prepare(pd.DataFrame(case['bars']), case['as_of'])
        if not 21 <= len(raw) <= 20000:
            raise ValueError('GOLDEN_HISTORY_INVALID')
        _, snapshot, _ = runtime.engine.replay(raw)
        _, features = runtime.engine.features(raw, snapshot, [len(raw)-1])
        np.testing.assert_allclose(features, np.asarray(case['features'], dtype=float), rtol=1e-10, atol=1e-10, equal_nan=True)
        actual = runtime.score(raw, case['as_of'])
        expected = case['expected']
        if actual['status'] != expected['status'] or len(actual['signals']) != len(expected['signals']):
            raise ValueError('GOLDEN_SIGNAL_SET_MISMATCH')
        for a, b in zip(sorted(actual['signals'], key=lambda s:s['task']), sorted(expected['signals'], key=lambda s:s['task']), strict=True):
            for key in ('task', 'anchor_dt', 'stage', 'above_threshold', 'target', 'condition'):
                if a[key] != b[key]:
                    raise ValueError('GOLDEN_SIGNAL_IDENTITY_MISMATCH:' + key)
            np.testing.assert_allclose([a['score'], a['threshold']], [b['score'], b['threshold']], rtol=1e-10, atol=1e-10)
    return {'status': 'GOLDEN_VERIFIED', 'cases': len(cases), 'manifest_sha256': pinned_sha, 'runtime_versions':versions}
