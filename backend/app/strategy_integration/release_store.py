"""Disk-based immutable version bundles and an atomic published release record.

`active_release.json` is the SINGLE TRUSTED published state: it binds the model
weights the worker must load to the exact snapshot batch that was activated, so
the model pointer and the snapshot pointer can never diverge. The `current/`
directory symlink is only a derived pointer kept in sync as a best-effort
refresh; readers resolve the batch via `resolve_release_batch()` (which ignores
`current`) or `resolve_active_release()` (the authoritative JSON), never the
symlink alone.

`activate_batch()` writes the authoritative JSON atomically FIRST; if that write
fails, nothing else changes, so the previously published state is left fully
intact (fail-closed). See `resolve_active_release()`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_log = logging.getLogger(__name__)


ACTIVE_RELEASE = 'active_release.json'


class PublishError(RuntimeError):
    pass


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.panji-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path: Path, blob: dict) -> None:
    atomic_bytes(path, json.dumps(
        blob, ensure_ascii=False, sort_keys=True, allow_nan=False).encode())


def digest_path(p: Path) -> str:
    """SHA256 of any (possibly large) file; used to verify pinned artifacts."""
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def resolve_release_batch(root: Path, release_id: str, *, evaluation_sha256: str | None = None) -> Path | None:
    """Locate the immutable batch directory carrying ``release_id`` under ``root``.

    Build names the batch ``<as_of>_<release_id[:16]>`` while the operator-facing
    layout keeps it under ``releases/<release_id>``; this scans one level deep for a
    manifest whose ``release_id`` matches, so the admin publish never has to guess
    the on-disk name and always points at the real batch.
    """
    root = root.resolve()
    manifests = list(root.glob('*/manifest.json')) + list(root.glob('releases/*/manifest.json'))
    matches = []
    for manifest in manifests:
        child = manifest.parent
        if child.is_symlink() or not child.resolve().is_relative_to(root):
            continue
        try:
            m = json.loads(manifest.read_text())
        except Exception:
            continue
        if m.get('release_id') == release_id and (evaluation_sha256 is None or m.get('evaluation_sha256') == evaluation_sha256):
            matches.append((m.get('as_of', ''), child.name, child))
    return max(matches)[2] if matches else None


def _validate_batch_for_activation(batch: Path):
    """Full reader validation against the staging batch, before any pointer swap.

    Candidate shards are hash-verified and re-validated individually and then
    globally; the evaluation keeps its own 96 MiB file bound. Raises PublishError
    so a corrupt batch never replaces a healthy published snapshot.
    """
    from app.services.strategy_candidate_shards import (
        MANIFEST_SCHEMA,
        ShardContractError,
        load_candidate_shards,
    )
    from app.services.strategy_candidate_snapshot import validate as validate_candidate
    from app.services.strategy_research_read_service import _check_snapshot

    descriptor = json.loads((batch / 'candidate_snapshot.json').read_bytes())
    from app.services.strategy_partitioned_snapshot import SCHEMA as PARTITIONED
    from app.services.strategy_partitioned_snapshot import index, validate_publication
    if isinstance(descriptor, dict) and descriptor.get('schema') == PARTITIONED:
        try:
            candidate = index(batch/'candidate_snapshot.json')
            evaluation = index(batch/'evaluation_snapshot.json')
            if {k:v for k,v in candidate.items() if k != 'kind'} != {k:v for k,v in evaluation.items() if k != 'kind'}:
                raise ValueError('PARTITION_DESCRIPTOR_LINEAGE_MISMATCH')
            validate_publication(batch/'candidate_snapshot.json')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise PublishError('PARTITIONED_VALIDATION_FAILED '+str(exc)) from exc
        return
    if isinstance(descriptor, dict) and descriptor.get('schema') == MANIFEST_SCHEMA:
        try:
            load_candidate_shards(batch / 'candidate_snapshot.json',
                                  validate=validate_candidate)
        except ShardContractError as exc:
            raise PublishError('CANDIDATE_SHARD_CONTRACT ' + str(exc)) from exc
    else:
        try:
            validate_candidate(descriptor)
        except Exception as exc:
            raise PublishError('CANDIDATE_VALIDATION_FAILED ' + str(exc)) from exc

    try:
        _check_snapshot(json.loads((batch / 'evaluation_snapshot.json').read_bytes()))
    except Exception as exc:
        raise PublishError('EVALUATION_VALIDATION_FAILED ' + str(exc)) from exc


def write_active_release(root: Path, batch: Path, release_id: str,
                         bundle_sha256: str, bundle_path: str) -> Path:
    """Record which weights the activated release pins (worker reads this).

    The pinned model artifact is file-verified: the path must exist and its SHA256
    must equal the recorded ``bundle_sha256``. A publish that cannot satisfy this
    must never write the record, so the model pointer can never point at a missing
    or tampered weight file.
    """
    root = root.resolve()
    if bundle_path:
        bp = Path(bundle_path)
        from .fourteen_adapter import is_package, verify_package
        if is_package(bp):
            try:
                manifest, _ = verify_package(bp, bundle_sha256)
                if manifest.get('price_input_contract') != 'QFQ_ASOF_V1':
                    raise ValueError('FOURTEEN_PRICE_INPUT_UNVERIFIED')
            except (ValueError, OSError) as exc:
                raise PublishError('ACTIVE_RELEASE_PACKAGE_INVALID') from exc
        elif not bp.is_file():
            raise PublishError('ACTIVE_RELEASE_BUNDLE_FILE_MISSING ' + str(bp))
        if bp.is_file() and bundle_sha256 and digest_path(bp) != bundle_sha256:
            raise PublishError('ACTIVE_RELEASE_BUNDLE_SHA_MISMATCH ' + str(bp))
    previous = resolve_active_release(root)
    now = datetime.now(UTC).isoformat()
    same_weights = previous and previous.get('release_id') == release_id and previous.get('bundle_sha256') == bundle_sha256
    model_activated_at = previous.get('model_activated_at', previous['activated_at']) if previous and same_weights else now
    payload = {
        'schema': 'panji-strategy-active-release-v1',
        'release_id': release_id,
        'batch': str(batch),
        'bundle_sha256': bundle_sha256,
        'bundle_path': bundle_path,
        'activated_at': now,
        'model_activated_at': model_activated_at,
    }
    target = root / ACTIVE_RELEASE
    atomic_json(target, payload)
    return target


def resolve_active_release(root: Path) -> dict | None:
    """Return the active release record, or None when nothing was activated."""
    pointer = root.resolve() / ACTIVE_RELEASE
    if not pointer.is_file():
        return None
    return json.loads(pointer.read_text())


def activate_batch(root: Path, batch: Path, *, bundle_sha256: str = '',
                   bundle_path: str = '', refresh_only: bool = False):
    """Serialize publication; daily refresh cannot replace weights or roll dates backward."""
    import fcntl
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root/'publication.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if refresh_only:
            active = resolve_active_release(root)
            manifest = json.loads((batch/'manifest.json').read_text())
            if (not active or active['release_id'] != manifest['release_id']
                    or active['bundle_sha256'] != manifest.get('runtime', {}).get('bundle_sha256')):
                raise PublishError('DAILY_REFRESH_MODEL_CHANGED')
            prior = json.loads((Path(active['batch'])/'manifest.json').read_text())
            if manifest['as_of'] < prior['as_of']:
                return {'status': 'HISTORY_VALIDATED_NOT_PUBLISHED', 'batch': str(batch)}
        return _activate_batch_locked(root, batch, bundle_sha256=bundle_sha256, bundle_path=bundle_path)


def _activate_batch_locked(root: Path, batch: Path, *, bundle_sha256: str = '',
                   bundle_path: str = ''):
    """Validate one immutable batch, then atomically move the pointer to it.

    The pointer swap is a single `os.replace` on a directory symlink: the candidate
    descriptor+shards and the evaluation file always switch together.
    """
    root = root.resolve()
    batch = batch.resolve()
    if not batch.is_dir() or not batch.is_relative_to(root):
        raise PublishError('BATCH_OUTSIDE_TRUSTED_ROOT')
    manifest = json.loads((batch / 'manifest.json').read_text())
    # Both the worker autopublish (built locally) and the admin publish (audited
    # operator artifact) are safe to activate; the audit prerequisite is an
    # independent gate enforced by the caller before this point.
    if manifest['status'] not in ('VALIDATED_STAGING_NOT_DEPLOYED', 'AUDITED'):
        raise PublishError('BATCH_NOT_VALIDATED')
    # Descriptor + every shard must match their recorded digests.
    for name, key in [('candidate_snapshot.json', 'candidate_sha256'),
                 ('evaluation_snapshot.json', 'evaluation_sha256')]:
        actual = hashlib.sha256((batch / name).read_bytes()).hexdigest()
        if actual != manifest[key]:
            raise PublishError('BATCH_CONTENT_SHA_MISMATCH ' + name)
    shards = manifest.get('candidate_shards')
    from app.services.strategy_partitioned_snapshot import SCHEMA as PARTITIONED
    if manifest.get('artifact_transport') != PARTITIONED and (not isinstance(shards, list) or not shards):
        raise PublishError('MISSING_SHARD_MANIFEST')
    for entry in shards or []:
        shard = batch / entry['file']
        if shard.is_symlink() or not shard.is_file():
            raise PublishError('MISSING_OR_SYMLINKED_SHARD ' + entry['file'])
        if shard.stat().st_size != entry['bytes']:
            raise PublishError('SHARD_BYTES_MISMATCH ' + entry['file'])
        if hashlib.sha256(shard.read_bytes()).hexdigest() != entry['sha256']:
            raise PublishError('SHARD_SHA_MISMATCH ' + entry['file'])
    # Both files belong to one immutable batch: validate them together.
    _validate_batch_for_activation(batch)
    release_id = manifest.get('release_id', '')
    pinned_bundle_sha = manifest.get('runtime', {}).get('bundle_sha256', bundle_sha256)
    pinned_bundle_path = manifest.get('bundle_path') or bundle_path
    # Authoritative publish state: active_release.json binds the model identity AND
    # the snapshot batch into a single trusted record. It is written atomically
    # FIRST. If this write fails (bundle file/SHA mismatch, disk error, injected
    # interruption), nothing else changes, so the previously published pointer is
    # left fully intact — fail-closed, the descriptor + shards never switch without
    # their pinned model weights. This is the "single trusted publish state": there
    # is no window where the snapshot pointer and the model pointer can diverge.
    write_active_release(root, batch, release_id, pinned_bundle_sha, pinned_bundle_path)
    # Best-effort refresh of the legacy `current` directory symlink so it mirrors
    # the authoritative record. This is a derived pointer only: readers resolve the
    # batch via resolve_release_batch (which ignores `current`) or the authoritative
    # active_release.json, so a failed symlink update cannot desync the published
    # state and must never block the publish.
    try:
        _refresh_current_symlink(root, batch)
    except OSError as exc:
        _log.warning(
            'current symlink refresh failed (non-fatal, JSON authoritative): %s', exc)
    return {'status': 'ACTIVATED_AFTER_OPERATOR_APPROVAL', 'batch': str(batch)}


def _refresh_current_symlink(root: Path, batch: Path) -> None:
    """Best-effort atomic refresh of the legacy `current` directory symlink.

    Kept as a separate unit so a failure here is isolatable (injection test) and
    non-fatal: the symlink is only a derived pointer; the authoritative published
    state lives in active_release.json (see activate_batch).
    """
    root = root.resolve()
    batch = batch.resolve()
    current = root / 'current'
    tmp = root / '.new-active-pointer'
    if tmp.exists() or tmp.is_symlink():
        raise PublishError('CONCURRENT_PUBLISH_POINTER_LOCKED')
    tmp.symlink_to(batch.relative_to(root), target_is_directory=True)
    try:
        os.replace(tmp, current)
    finally:
        if tmp.is_symlink():
            tmp.unlink()


def published_snapshot_path(name: str, fallback: str) -> Path:
    """Readers consume the same authoritative pointer as prediction jobs.

    An invalid active pointer is an error, never a fallback to old demo assets.
    """
    root = os.getenv('PANJI_STRATEGY_RELEASE_ROOT', '')
    if root:
        base = Path(root).resolve()
        active = resolve_active_release(base)
        if active is not None:
            batch = Path(active['batch']).resolve()
            if not batch.is_relative_to(base) or not (batch/name).is_file():
                raise PublishError('PUBLISHED_SNAPSHOT_POINTER_INVALID')
            return batch/name
    return Path(fallback)
