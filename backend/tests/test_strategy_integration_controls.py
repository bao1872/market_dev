"""Pure tests: deployment flags and immutable staging pointer."""
from pathlib import Path
import json
import os
import pytest
from app.strategy_integration.worker import process_one
from app.strategy_integration.release_store import atomic_json, activate_batch, PublishError


def test_worker_is_off_by_default(monkeypatch):
    monkeypatch.delenv('PANJI_STRATEGY_WORKER_ENABLED',raising=False)
    with pytest.raises(RuntimeError,match='WORKER_DISABLED'):
        process_one()


def test_no_unvalidated_batch_activation(tmp_path):
    batch=tmp_path/'batch';batch.mkdir()
    atomic_json(batch/'manifest.json',{'status':'FAILED'})
    with pytest.raises(PublishError,match='BATCH_NOT_VALIDATED'):
        activate_batch(tmp_path,batch)
    assert not (tmp_path/'current').exists()


class StubBooster:
    """Deterministic stub; no real inference and no fitting."""
    def predict_proba(self, X):
        import numpy as np
        return np.column_stack([np.full(len(X), .1), np.full(len(X), .9)])


def _sample_bars(n=320):
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(26)
    v = 25 + np.sin(np.arange(n) / 9) * 2 + np.cumsum(rng.normal(0, .25, n))
    o = v * .999; c = v * 1.001
    h = np.maximum(o, c) * 1.018; l = np.minimum(o, c) * .981
    return pd.DataFrame({'datetime': pd.bdate_range('2024-01-01', periods=n),
                         'open': o, 'high': h, 'low': l, 'close': c,
                         'volume': np.full(n, 2e6)})


def _build_valid_batch(batch: Path) -> tuple[dict, bytes]:
    """Create a real, validator-accepted immutable staging batch (sharded)."""
    import hashlib
    import os
    from app.services.strategy_candidate_shards import write_candidate_shards
    from app.services.strategy_candidate_snapshot import validate as validate_candidate
    from app.strategy_integration import model, producer
    assets = os.environ.get('PANJI_STRATEGY_MODEL_ASSETS')
    assert assets, 'PANJI_STRATEGY_MODEL_ASSETS required'
    fmod = model.load_engine(Path(assets))
    bundle = {
        'models': {(layer, kind, k): StubBooster()
                   for layer in ('L1', 'L2') for kind in ('H', 'L') for k in (1, 2, 3)},
        'thresholds': {(layer, kind, k): .5
                       for layer in ('L1', 'L2') for kind in ('H', 'L') for k in (1, 2, 3)},
    }
    raw = _sample_bars()
    as_of = str(raw.datetime.iloc[-1].date())
    candidate, evaluation = producer.make_snapshots(
        {'SZ:000001': raw}, fmod, bundle, as_of=as_of, source_sha='f' * 64,
    )
    batch.mkdir(parents=True, exist_ok=True)
    shards = write_candidate_shards(candidate, batch, validate=validate_candidate)
    evaluation_data = producer.encode_json(evaluation)
    (batch / 'evaluation_snapshot.json').write_bytes(evaluation_data)
    descriptor = (batch / 'candidate_snapshot.json').read_bytes()
    atomic_json(batch / 'manifest.json', {
        'status': 'VALIDATED_STAGING_NOT_DEPLOYED',
        'release_id': producer.RELEASE_ID,
        'source_sha256': 'f' * 64,
        'candidate_sha256': hashlib.sha256(descriptor).hexdigest(),
        'evaluation_sha256': hashlib.sha256(evaluation_data).hexdigest(),
        'candidate_shards': shards['shards'],
        'candidate_total_bytes': shards['total_bytes'],
    })
    return shards, descriptor


def test_pair_pointer_atomic_and_fails_closed(tmp_path):
    import hashlib
    batch = tmp_path / 'batch'
    shards, descriptor = _build_valid_batch(batch)
    assert shards['shards'], 'expected at least one shard'
    assert activate_batch(tmp_path, batch)['status'] == 'ACTIVATED_AFTER_OPERATOR_APPROVAL'
    assert (tmp_path / 'current' / 'candidate_snapshot.json').read_bytes() == descriptor
    assert (tmp_path / 'current' / 'evaluation_snapshot.json').read_bytes() == (
        batch / 'evaluation_snapshot.json'
    ).read_bytes()


def test_activate_batch_rejects_corrupt_shard_and_keeps_pointer(tmp_path):
    """One altered shard must fail closed and leave the healthy pointer intact."""
    good = tmp_path / 'good'
    _build_valid_batch(good)
    assert activate_batch(tmp_path, good)['status'] == 'ACTIVATED_AFTER_OPERATOR_APPROVAL'
    pointer_before = (tmp_path / 'current').resolve()

    bad = tmp_path / 'bad'
    _build_valid_batch(bad)
    shard_name = next(p for p in sorted((bad).iterdir()) if p.name.startswith('candidate_shard_'))
    original = shard_name.read_bytes()
    shard_name.write_bytes(original.replace(b'"stocks"', b'"stocksX"', 1))
    with pytest.raises(PublishError):
        activate_batch(tmp_path, bad)
    # The previously published immutable release is untouched.
    assert (tmp_path / 'current').resolve() == pointer_before
    assert (tmp_path / 'current' / 'candidate_snapshot.json').read_bytes() == (
        good / 'candidate_snapshot.json'
    ).read_bytes()


def test_activate_batch_requires_shard_manifest(tmp_path):
    import hashlib
    batch = tmp_path / 'batch'
    _build_valid_batch(batch)
    manifest = json.loads((batch / 'manifest.json').read_text())
    manifest.pop('candidate_shards')
    atomic_json(batch / 'manifest.json', manifest)
    with pytest.raises(PublishError, match='MISSING_SHARD_MANIFEST'):
        activate_batch(tmp_path, batch)
    assert not (tmp_path / 'current').exists()
