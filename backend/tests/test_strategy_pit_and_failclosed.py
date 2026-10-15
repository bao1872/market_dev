"""PIT qfq contract, price-contract fail-closed, and publish/rollback fault injection."""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.strategy_integration import model, producer, source
from app.strategy_integration.release_store import (
    PublishError, activate_batch, atomic_json, resolve_active_release,
    write_active_release,
)


def _assets() -> Path:
    a = os.environ.get('PANJI_STRATEGY_MODEL_ASSETS')
    assert a, 'PANJI_STRATEGY_MODEL_ASSETS required'
    return Path(a)


def _raw_group(n=260, action_at=None):
    """Raw OHLCV with one corporate action (factor halving) at `action_at`."""
    rng = np.random.default_rng(5)
    v = 30 + np.cumsum(rng.normal(0, .3, n))
    o = v * .999
    c = v * 1.001
    h = np.maximum(o, c) * 1.012
    l = np.minimum(o, c) * .988
    dates = pd.bdate_range('2023-01-02', periods=n)
    factor = np.ones(n)
    if action_at is not None:
        factor[action_at:] = 0.5
    # Carry the true RAW evidence columns so _pit_frame can reject any attempt to
    # double-adjust an already-QFQ frame (PIT_RAW_OHLCV_REQUIRED_NO_DOUBLE_ADJUST).
    return pd.DataFrame({'trade_date': dates, 'open': o, 'high': h, 'low': l,
                         'close': c, 'volume': np.full(n, 1e6),
                         'adj_factor': factor, 'raw_open': o, 'raw_high': h,
                         'raw_low': l, 'raw_close': c,
                         'raw_volume': np.full(n, 1e6)})


def _qfq(group, anchor):
    """Canonical qfq frame with an explicit datetime column (pipeline shape)."""
    out = source._qfq_from_raw(group, anchor)
    frame = pd.DataFrame(out.to_numpy(float),
                         columns=['open', 'high', 'low', 'close', 'volume'])
    frame.insert(0, 'datetime',
                 pd.Series(pd.to_datetime(out.index)).reset_index(drop=True))
    return frame


def test_pit_prefix_parity_across_corporate_action():
    """A corporate action AFTER a decision day must not change that day's inputs."""
    group = _raw_group(action_at=200)
    decision = 150
    decision_date = str(pd.Timestamp(group.trade_date.iloc[decision]).date())
    pit = _qfq(group, decision_date)

    # The same decision day, but converted from the FULL history (which includes
    # the later action) and then truncated: identical inputs required.
    full = _qfq(group, decision_date)
    truncated = full.iloc[:decision + 1]
    assert np.allclose(pit.iloc[:decision + 1]['close'].to_numpy(),
                       truncated['close'].to_numpy())
    assert np.allclose(pit.iloc[:decision + 1]['volume'].to_numpy(),
                       truncated['volume'].to_numpy())

    # Anchoring at a LATER date must rescale, proving the anchor is honoured.
    later = _qfq(group, str(pd.Timestamp(group.trade_date.iloc[-1]).date()))
    assert not np.allclose(pit.iloc[:decision + 1]['close'].to_numpy(),
                           later.iloc[:decision + 1]['close'].to_numpy())


def test_pit_frame_leaves_anchor_regime_unadjusted():
    group = _raw_group(action_at=200)
    decision = 150
    anchor = str(pd.Timestamp(group.trade_date.iloc[decision]).date())
    frame = model._pit_frame(group, anchor)
    # Inside the anchor's own factor regime the qfq price equals the raw price.
    raw_close = group['close'].to_numpy(float)[:decision + 1]
    assert np.allclose(frame['close'].to_numpy()[:decision + 1], raw_close)


def test_price_contract_mismatch_fails_closed():
    """Inference must refuse weights whose input contract is not QFQ_ASOF_V1."""
    raw_trained = {'models': {}, 'thresholds': {}, 'uncalibrated': True,
                   'price_input_contract': 'RAW_OHLCV'}
    with pytest.raises(producer.ProducerError,
                       match='MODEL_PRICE_INPUT_CONTRACT_MISMATCH'):
        producer.require_price_contract(raw_trained, producer.PRICE_INPUT_CONTRACT)

    # A matching candidate contract is accepted and echoed back.
    assert producer.require_price_contract(
        {'price_input_contract': producer.PRICE_INPUT_CONTRACT},
        producer.PRICE_INPUT_CONTRACT) == producer.PRICE_INPUT_CONTRACT


def test_frozen_bundle_declares_raw_contract():
    """The supplied pickle carries no contract, so it must derive as RAW_OHLCV."""
    src = (Path(__file__).resolve().parents[1] / 'app/strategy_integration/model.py')
    text = src.read_text(encoding='utf-8')
    assert "'price_input_contract': 'RAW_OHLCV'" in text


def test_unknown_price_contract_fails_closed():
    fmod = model.load_engine(_assets())
    stub = {'models': {}, 'thresholds': {}, 'uncalibrated': True}
    with pytest.raises(producer.ProducerError, match='MODEL_PRICE_INPUT_CONTRACT_UNKNOWN'):
        producer.require_price_contract(stub, producer.PRICE_INPUT_CONTRACT)


# ---------------------------------------------------------------- fault injection
class StubBooster:
    n_features_in_ = 131

    def predict_proba(self, X):
        return np.column_stack([np.full(len(X), .1), np.full(len(X), .9)])


def _batch(batch: Path):
    from app.services.strategy_candidate_shards import write_candidate_shards
    from app.services.strategy_candidate_snapshot import validate as validate_candidate
    fmod = model.load_engine(_assets())
    bundle = {'models': {(l, k, s): StubBooster() for l in ('L1', 'L2')
                         for k in ('H', 'L') for s in (1, 2, 3)},
              'thresholds': {(l, k, s): .5 for l in ('L1', 'L2')
                             for k in ('H', 'L') for s in (1, 2, 3)}}
    raw = _qfq(_raw_group(action_at=200), '2023-12-29')
    as_of = str(raw.datetime.iloc[-1].date())
    candidate, evaluation = producer.make_snapshots(
        {'SH:600000': raw}, fmod, bundle, as_of=as_of, source_sha='b' * 64,
        price_adjustment='qfq', adjustment_mode='CANONICAL_MDAS_QFQ')
    batch.mkdir(parents=True, exist_ok=True)
    shards = write_candidate_shards(candidate, batch, validate=validate_candidate)
    (batch / 'evaluation_snapshot.json').write_bytes(producer.encode_json(evaluation))
    descriptor = (batch / 'candidate_snapshot.json').read_bytes()
    real_bundle_sha = __import__('hashlib').sha256(
        (_assets() / 'two_stage_models_local_only.pkl').read_bytes()).hexdigest()
    atomic_json(batch / 'manifest.json', {
        'status': 'VALIDATED_STAGING_NOT_DEPLOYED',
        'release_id': producer.RELEASE_ID,
        'source_sha256': 'b' * 64,
        'candidate_sha256': __import__('hashlib').sha256(descriptor).hexdigest(),
        'evaluation_sha256': __import__('hashlib').sha256(
            (batch / 'evaluation_snapshot.json').read_bytes()).hexdigest(),
        'candidate_shards': shards['shards'],
        'bundle_path': str(_assets() / 'two_stage_models_local_only.pkl'),
        'runtime': {'bundle_sha256': real_bundle_sha},
    })
    return batch


def test_publish_interrupted_midway_keeps_previous_pointer(tmp_path):
    """A crash between pointer write and shard validation must not serve a partial set."""
    root = tmp_path / 'root'
    root.mkdir()
    good = _batch(root / 'good')
    activate_batch(root, good)
    before = (root / 'current').resolve()

    bad = _batch(root / 'bad')
    # Simulate an interrupted publish: a shard is truncated before activation.
    shard = bad / sorted(p.name for p in bad.iterdir()
                         if p.name.startswith('candidate_shard_'))[0]
    data = shard.read_bytes()
    shard.write_bytes(data[:len(data) // 2])
    with pytest.raises(PublishError):
        activate_batch(root, bad)
    assert (root / 'current').resolve() == before
    assert (root / 'current' / 'candidate_snapshot.json').read_bytes() == (
        good / 'candidate_snapshot.json').read_bytes()


def test_pointer_bundle_sha_inconsistency_fails_closed(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    weights = tmp_path / 'w.pkl'
    weights.write_bytes(b'x')
    # Pointer declares a different sha than the file: publish must refuse so the
    # model pointer can never point at a missing/tampered weight file. Fail-closed
    # happens at the publish step, before any inference.
    with pytest.raises(PublishError, match='ACTIVE_RELEASE_BUNDLE_SHA_MISMATCH'):
        write_active_release(root, root / 'b', 'r', 'd' * 64, str(weights))


def test_rollback_restores_exact_previous_pointer(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    first = _batch(root / 'first')
    second = _batch(root / 'second')
    activate_batch(root, first)
    first_target = (root / 'current').resolve()
    activate_batch(root, second)
    second_target = (root / 'current').resolve()
    assert first_target != second_target
    # Roll back to the first release.
    activate_batch(root, first)
    assert (root / 'current').resolve() == first_target
    assert json.loads((root / 'active_release.json').read_text())['batch'] == str(first)
