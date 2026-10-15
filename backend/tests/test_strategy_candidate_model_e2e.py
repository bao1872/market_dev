"""Candidate-model end-to-end gates: real LightGBM weights, 12 models, 131 dims,
prefix invariance under PIT, shard integrity, metadata identity, and the
publish -> infer -> rollback chain plus the raw-trained fail-closed guard.

Runs only when a candidate bundle exists at PANJI_CANDIDATE_BUNDLE and the
frozen assets are present; otherwise it skips (never fabricates a pass).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.services.strategy_candidate_shards import load_candidate_shards
from app.services.strategy_candidate_snapshot import validate as validate_candidate
from app.strategy_integration import model, producer, source
from app.strategy_integration.release_store import write_active_release
from app.strategy_integration.cli import load_bundle_for_run

ASSETS = Path('/tmp/panji_frozen_assets')
CANDIDATE = Path('/tmp/panji_train_full/two_stage_models_candidate_UNPUBLISHED.pkl')
FULL_BATCH = Path('/tmp/panji_predfull_new')

pytestmark = pytest.mark.skipif(
    not (ASSETS / 'two_stage_models_local_only.pkl').is_file() or not CANDIDATE.is_file(),
    reason='candidate bundle / frozen assets not present in this environment',
)


def _lightgbm_available() -> bool:
    try:
        import lightgbm  # noqa: F401
        import sklearn  # noqa: F401
        return True
    except Exception:
        return False


needs_runtime = pytest.mark.skipif(
    not _lightgbm_available(),
    reason='lightgbm/sklearn runtime not in this venv; real-weight checks run under panji_lab',
)


@needs_runtime
def test_real_lightgbm_weights_12_models_131_dims():
    bundle, facts = model.load_bundle_file(CANDIDATE)
    assert len(bundle['models']) == 12 and len(bundle['thresholds']) == 12
    for key, clf in bundle['models'].items():
        assert clf.__class__.__module__.startswith('lightgbm'), key
        assert getattr(clf, 'n_features_in_', None) == 131, key
    assert bundle['feature_dim'] == 131
    assert bundle['feature_sha'] == model.ENGINE_SHA
    assert bundle['target'] == {'L': 'HH', 'H': 'LL'}
    assert bundle['price_input_contract'] == producer.PRICE_INPUT_CONTRACT
    assert facts['bundle_sha256'] == hashlib.sha256(CANDIDATE.read_bytes()).hexdigest()


@needs_runtime
def test_prefix_invariance_under_pit():
    fmod = model.load_engine(ASSETS)
    bundle, _ = model.load_bundle_file(CANDIDATE)
    be = Path('/Users/zhenbao/Desktop/coding/market_dev/backend')
    market, _ = source.load_official_parquet_pit(
        be / '.perfdata/afterclose/afterclose-20260817-v1/bars_daily_raw.parquet',
        be / '.perfdata/afterclose/afterclose-20260817-v1/instruments.parquet',
        '2026-08-17', max_symbols=3)
    sym = sorted(market)[0]
    group = market[sym]
    raw = group.drop(columns=['adj_factor'])
    assert model.assert_prefix_invariance(
        raw, sym, fmod, bundle, tail_cut=20, pit_source=group) > 0


def test_full_market_shards_and_metadata_identity():
    if not FULL_BATCH.is_dir():
        pytest.skip('full-market candidate run not present')
    batch = sorted(p for p in FULL_BATCH.iterdir() if p.is_dir())[0]
    manifest = json.loads((batch / 'manifest.json').read_text())
    assert len(manifest['candidate_shards']) >= 1
    for entry in manifest['candidate_shards']:
        shard = batch / entry['file']
        assert shard.is_file() and not shard.is_symlink()
        assert shard.stat().st_size == entry['bytes']
        assert hashlib.sha256(shard.read_bytes()).hexdigest() == entry['sha256']
    merged = load_candidate_shards(batch / 'candidate_snapshot.json',
                                   validate=validate_candidate)
    assert merged['metadata']['price_adjustment'] == 'qfq'
    assert merged['metadata']['model_sha256'] == manifest['runtime']['bundle_sha256']
    assert merged['metadata']['release_id'] == manifest['release_id']
    assert len(merged['daily_batches']) == 20


@needs_runtime
def test_publish_infer_rollback_identity(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    import pickle
    with open(CANDIDATE, 'rb') as fh:
        blob = pickle.load(fh)
    blob['thresholds'][('L1', 'H', 1)] = 0.31
    old = tmp_path / 'old.pkl'
    with open(old, 'wb') as fh:
        pickle.dump(blob, fh, pickle.HIGHEST_PROTOCOL)
    old_sha = hashlib.sha256(old.read_bytes()).hexdigest()
    new_sha = hashlib.sha256(CANDIDATE.read_bytes()).hexdigest()
    assert old_sha != new_sha

    write_active_release(root, root / 'b-new', 'new', new_sha, str(CANDIDATE))
    _, facts_new, path_new, origin_new = load_bundle_for_run(root, ASSETS)
    assert origin_new == 'ACTIVE_RELEASE'
    assert facts_new['bundle_sha256'] == new_sha
    assert path_new == str(CANDIDATE)

    write_active_release(root, root / 'b-old', 'old', old_sha, str(old))
    _, facts_old, path_old, _ = load_bundle_for_run(root, ASSETS)
    assert facts_old['bundle_sha256'] == old_sha
    assert path_old == str(old)


@needs_runtime
def test_raw_trained_weights_are_refused():
    frozen, facts = model.load_bundle(ASSETS)
    assert facts['price_input_contract'] == 'RAW_OHLCV'
    with pytest.raises(producer.ProducerError):
        producer.require_price_contract(frozen, producer.PRICE_INPUT_CONTRACT)
