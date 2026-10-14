"""Disk-based immutable version bundles and atomic *pair* snapshot pointer.

Requires POSIX filesystem: current/ is a symlink to a whole validated batch
folder. Both read API JSON paths traverse the same pointer. Existing good
snapshots survive any failed build, incomplete batch or process crash.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile


class PublishError(RuntimeError):pass


def atomic_bytes(path:Path,data:bytes):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.panji-')
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def atomic_json(path:Path,blob:dict):
    atomic_bytes(path,json.dumps(blob,ensure_ascii=False,sort_keys=True,allow_nan=False).encode())


def _validate_batch_for_activation(batch:Path):
    """Full reader validation against the staging batch, before any pointer swap.

    Candidate shards are hash-verified and re-validated individually and then
    globally; the evaluation keeps its own 96 MiB file bound. Raises PublishError
    so a corrupt batch never replaces a healthy published snapshot.
    """
    from app.services.strategy_candidate_shards import (
        MANIFEST_SCHEMA, ShardContractError, load_candidate_shards,
    )
    from app.services.strategy_candidate_snapshot import validate as validate_candidate
    from app.services.strategy_research_read_service import _check_snapshot
    descriptor = json.loads((batch/'candidate_snapshot.json').read_bytes())
    if isinstance(descriptor, dict) and descriptor.get('schema') == MANIFEST_SCHEMA:
        try:
            load_candidate_shards(batch/'candidate_snapshot.json', validate=validate_candidate)
        except ShardContractError as exc:
            raise PublishError('CANDIDATE_SHARD_CONTRACT '+str(exc)) from exc
    else:
        try:
            validate_candidate(descriptor)
        except Exception as exc:
            raise PublishError('CANDIDATE_VALIDATION_FAILED '+str(exc)) from exc
    try:
        _check_snapshot(json.loads((batch/'evaluation_snapshot.json').read_bytes()))
    except Exception as exc:
        raise PublishError('EVALUATION_VALIDATION_FAILED '+str(exc)) from exc


def activate_batch(root:Path,batch:Path):
    root=root.resolve();batch=batch.resolve()
    if not batch.is_dir() or not batch.is_relative_to(root):
        raise PublishError('BATCH_OUTSIDE_TRUSTED_ROOT')
    manifest=json.loads((batch/'manifest.json').read_text())
    if manifest['status']!='VALIDATED_STAGING_NOT_DEPLOYED':
        raise PublishError('BATCH_NOT_VALIDATED')
    for name,key in [('candidate_snapshot.json','candidate_sha256'),('evaluation_snapshot.json','evaluation_sha256')]:
        actual=hashlib.sha256((batch/name).read_bytes()).hexdigest()
        if actual!=manifest[key]:raise PublishError('BATCH_CONTENT_SHA_MISMATCH '+name)
    # Every shard must match its recorded digest before the pointer moves.
    shards=manifest.get('candidate_shards')
    if not isinstance(shards,list) or not shards:
        raise PublishError('MISSING_SHARD_MANIFEST')
    for entry in shards:
        name=entry['file'];shard=batch/name
        if shard.is_symlink() or not shard.is_file():
            raise PublishError('MISSING_OR_SYMLINKED_SHARD '+name)
        if shard.stat().st_size!=entry['bytes']:
            raise PublishError('SHARD_BYTES_MISMATCH '+name)
        if hashlib.sha256(shard.read_bytes()).hexdigest()!=entry['sha256']:
            raise PublishError('SHARD_SHA_MISMATCH '+name)
    # Both files belong to one immutable batch: validate them together.
    _validate_batch_for_activation(batch)
    # Single atomic directory symlink swap, never two independent JSON replaces.
    tmp=root/'.new-active-pointer'
    current=root/'current'
    if tmp.exists() or tmp.is_symlink():raise PublishError('CONCURRENT_PUBLISH_POINTER_LOCKED')
    tmp.symlink_to(batch.relative_to(root),target_is_directory=True)
    try:os.replace(tmp,current)
    finally:
        if tmp.is_symlink():tmp.unlink()
    return {'status':'ACTIVATED_AFTER_OPERATOR_APPROVAL','batch':str(batch)}
