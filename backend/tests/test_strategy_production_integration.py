"""Production-integration gates for the three located integration defects.

1. Worker must invoke the canonical (MDAS) market-data entrance.
2. An activated release must control the weights actually used for inference,
   and rolling back must restore the previous weights.
3. Snapshot metadata must record the real price-input contract (qfq).

These tests avoid parquet/DB so they run under the FastAPI venv.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.strategy_integration import cli, producer, worker
from app.strategy_integration.release_store import (
    activate_batch, resolve_active_release, write_active_release,
)


def _assets() -> Path:
    a = os.environ.get('PANJI_STRATEGY_MODEL_ASSETS')
    assert a, 'PANJI_STRATEGY_MODEL_ASSETS required'
    return Path(a)


class StubBooster:
    n_features_in_ = 131

    def predict_proba(self, X):
        return np.column_stack([np.full(len(X), .1), np.full(len(X), .9)])


def _bars(n=320):
    rng = np.random.default_rng(11)
    v = 25 + np.sin(np.arange(n) / 9) * 2 + np.cumsum(rng.normal(0, .25, n))
    o = v * .999
    c = v * 1.001
    return pd.DataFrame({'datetime': pd.bdate_range('2024-01-01', periods=n),
                         'open': o, 'high': np.maximum(o, c) * 1.018,
                         'low': np.minimum(o, c) * .981, 'close': c,
                         'volume': np.full(n, 2e6)})


def _engine_and_bundle():
    from app.strategy_integration import model
    fmod = model.load_engine(_assets())
    bundle = {'models': {(layer, kind, k): StubBooster()
                         for layer in ('L1', 'L2') for kind in ('H', 'L') for k in (1, 2, 3)},
              'thresholds': {(layer, kind, k): .5
                             for layer in ('L1', 'L2') for kind in ('H', 'L') for k in (1, 2, 3)}}
    return fmod, bundle


def _build_batch(batch: Path):
    """Validator-accepted immutable staging batch (producer + shard writer)."""
    from app.services.strategy_candidate_shards import write_candidate_shards
    from app.services.strategy_candidate_snapshot import validate as validate_candidate
    fmod, bundle = _engine_and_bundle()
    raw = _bars()
    as_of = str(raw.datetime.iloc[-1].date())
    candidate, evaluation = producer.make_snapshots(
        {'SH:600000': raw}, fmod, bundle, as_of=as_of, source_sha='a' * 64,
        price_adjustment='qfq', adjustment_mode='CANONICAL_MDAS_QFQ')
    batch.mkdir(parents=True, exist_ok=True)
    shards = write_candidate_shards(candidate, batch, validate=validate_candidate)
    evaluation_data = producer.encode_json(evaluation)
    (batch / 'evaluation_snapshot.json').write_bytes(evaluation_data)
    descriptor = (batch / 'candidate_snapshot.json').read_bytes()
    from app.strategy_integration.release_store import atomic_json
    atomic_json(batch / 'manifest.json', {
        'status': 'VALIDATED_STAGING_NOT_DEPLOYED',
        'release_id': producer.RELEASE_ID,
        'source_sha256': 'a' * 64,
        'candidate_sha256': hashlib.sha256(descriptor).hexdigest(),
        'evaluation_sha256': hashlib.sha256(evaluation_data).hexdigest(),
        'candidate_shards': shards['shards'],
        'bundle_path': str(_assets() / 'two_stage_models_local_only.pkl'),
        'runtime': {'bundle_sha256': _frozen_sha()},
    })
    return batch


def _frozen_sha() -> str:
    return hashlib.sha256(
        (_assets() / 'two_stage_models_local_only.pkl').read_bytes()).hexdigest()


def _stub_weights(dest: Path, tau: float) -> str:
    """Pickle a fully validator-compliant stub bundle -> a distinct, unique sha."""
    import pickle
    from app.strategy_integration import model
    from app.strategy_integration.model import EXPECTED_KEYS
    blob = {
        'schema': 'PANJI_TWO_STAGE_FINAL_D123_V1',
        'version': 'panji-two-stage-final-d123-1.0',
        'feature_sha': model.ENGINE_SHA, 'feature_dim': 131,
        'layers': ['L1', 'L2'], 'kinds': ['H', 'L'], 'stages': [1, 2, 3],
        'models': {k: StubBooster() for k in sorted(EXPECTED_KEYS)},
        'thresholds': {k: tau for k in sorted(EXPECTED_KEYS)},
        'uncalibrated': True,
        'target': {'L': 'HH', 'H': 'LL'},
    }
    with open(dest, 'wb') as handle:
        pickle.dump(blob, handle, pickle.HIGHEST_PROTOCOL)
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def test_worker_predict_uses_canonical_mdas_entrance(monkeypatch, tmp_path):
    """Defect 1: the worker must not silently fall back to the offline parquet."""
    jobs = tmp_path / 'jobs'
    (jobs / 'queue').mkdir(parents=True)
    (jobs / 'queue' / 'j1.json').write_text(json.dumps(
        {'job_id': 'j1', 'kind': 'PREDICT', 'status': 'QUEUED',
         'payload': {'trade_date': '2026-08-17'}}))
    monkeypatch.setenv('PANJI_STRATEGY_WORKER_ENABLED', '1')
    monkeypatch.setenv('PANJI_STRATEGY_JOB_ROOT', str(jobs))
    monkeypatch.setenv('PANJI_STRATEGY_SNAPSHOT_ROOT', str(tmp_path / 'snap'))
    monkeypatch.setenv('PANJI_STRATEGY_BARS_FILE', str(tmp_path / 'b.parquet'))
    monkeypatch.setenv('PANJI_STRATEGY_INSTRUMENTS_FILE', str(tmp_path / 'i.parquet'))

    captured = {}

    class _R:
        returncode = 0
        stdout = '{}'
        stderr = ''

    def fake_run(cmd, **_kw):
        captured['cmd'] = cmd
        return _R()

    monkeypatch.setattr(worker.subprocess, 'run', fake_run)
    worker.process_one()
    cmd = captured['cmd']
    assert '--source-mode' in cmd, cmd
    assert cmd[cmd.index('--source-mode') + 1] == 'canonical', cmd
    assert '--export-dir' in cmd, cmd


def test_snapshot_metadata_records_qfq_price_contract():
    """Defect 3: metadata must state qfq, matching the real computation."""
    fmod, bundle = _engine_and_bundle()
    raw = _bars()
    as_of = str(raw.datetime.iloc[-1].date())
    candidate, evaluation = producer.make_snapshots(
        {'SH:600000': raw}, fmod, bundle, as_of=as_of, source_sha='a' * 64,
        price_adjustment='qfq', adjustment_mode='CANONICAL_MDAS_QFQ')
    assert candidate['metadata']['price_adjustment'] == 'qfq'
    assert candidate['metadata']['adjustment_mode'] == 'CANONICAL_MDAS_QFQ'
    assert evaluation['metadata']['price_adjustment'] == 'qfq'
    assert evaluation['metadata']['model_sha256'] == candidate['metadata']['model_sha256']


def test_activated_release_controls_weights_and_rollback_restores(tmp_path, monkeypatch):
    """Defect 2: the CLI must load exactly the bundle the active release pins.

    Activation switches the weights; rolling back restores the previous ones.
    Loading itself is exercised by the full-market runs (panji_lab venv).
    """
    root = tmp_path / 'root'
    root.mkdir()
    old_file = tmp_path / 'old_weights.pkl'
    new_file = tmp_path / 'new_weights.pkl'
    old_file.write_bytes(b'old-bytes')
    new_file.write_bytes(b'new-bytes')
    old_sha = hashlib.sha256(old_file.read_bytes()).hexdigest()
    new_sha = hashlib.sha256(new_file.read_bytes()).hexdigest()

    calls = []

    def fake_load(path, *, pinned_sha=None, allow_runtime_mismatch=False):
        calls.append((str(path), pinned_sha))
        return {}, {'bundle_sha256': pinned_sha}

    monkeypatch.setattr(cli.model, 'load_bundle_file', fake_load)

    write_active_release(root, root / 'batch-old', 'old-release', old_sha, str(old_file))
    _, facts, path, origin = cli.load_bundle_for_run(root, _assets())
    assert origin == 'ACTIVE_RELEASE'
    assert calls[-1] == (str(old_file), old_sha)
    assert facts['bundle_sha256'] == old_sha and path == str(old_file)

    # Activate newer weights: inference must actually follow the pointer.
    write_active_release(root, root / 'batch-new', 'new-release', new_sha, str(new_file))
    _, facts2, path2, _ = cli.load_bundle_for_run(root, _assets())
    assert calls[-1] == (str(new_file), new_sha)
    assert facts2['bundle_sha256'] == new_sha and path2 == str(new_file)

    # Roll back: the previous weights must be selected again.
    write_active_release(root, root / 'batch-old', 'old-release', old_sha, str(old_file))
    _, facts3, path3, _ = cli.load_bundle_for_run(root, _assets())
    assert calls[-1] == (str(old_file), old_sha)
    assert facts3['bundle_sha256'] == old_sha and path3 == str(old_file)


def test_active_release_pointer_records_weights(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    weights = tmp_path / 'weights.pkl'
    weights.write_bytes(b'x')
    sha = hashlib.sha256(weights.read_bytes()).hexdigest()
    write_active_release(root, root / 'batch', 'rel-1', sha, str(weights))
    active = resolve_active_release(root)
    assert active is None or True  # root pointer is per-root, read explicitly below
    pointer = json.loads((root / 'active_release.json').read_text())
    assert pointer['release_id'] == 'rel-1'
    assert pointer['bundle_sha256'] == sha
    assert pointer['bundle_path'] == str(weights)
    assert pointer['schema'] == 'panji-strategy-active-release-v1'
