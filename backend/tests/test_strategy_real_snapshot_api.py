"""Full-pipeline API acceptance against a REAL shard-descriptor snapshot.

The snapshot is produced by the actual producer + shard writer (validator
accepted), never a hand-written fixture. No database, no worker, no worker
enablement; PURE_UNIT_TEST=1.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategy_candidate_research as cand_api
from app.api import strategy_research as eval_api
from app.core.deps import get_db
from app.services.access_control_service import AccessContext, require_authenticated
from app.services.strategy_research_read_service import _load_cached


class StubBooster:
    def predict_proba(self, X):
        return np.column_stack([np.full(len(X), .1), np.full(len(X), .9)])


def _ctx(kind: str) -> AccessContext:
    admin = kind == "admin"
    active = kind in ("strategy", "admin")
    key = "market_data" if kind == "market" else "strategy_research"
    return AccessContext(
        user_id="fixture-user", account_status="active", roles=["admin" if admin else "member"],
        is_admin=admin, is_member=not admin, subscription_active=True, default_route="/strategy",
        capabilities={key: {"active": active}},
    )


def _bars(n=320):
    rng = np.random.default_rng(7)
    v = 25 + np.sin(np.arange(n) / 9) * 2 + np.cumsum(rng.normal(0, .25, n))
    o = v * .999
    c = v * 1.001
    h = np.maximum(o, c) * 1.018
    l = np.minimum(o, c) * .981
    return pd.DataFrame({'datetime': pd.bdate_range('2024-01-01', periods=n),
                         'open': o, 'high': h, 'low': l, 'close': c,
                         'volume': np.full(n, 2e6)})


def _make_batch(tmp_path: Path, *, sharded: bool):
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
    raw = _bars()
    as_of = str(raw.datetime.iloc[-1].date())
    candidate, evaluation = producer.make_snapshots(
        {'SH:600000': raw}, fmod, bundle, as_of=as_of, source_sha='a' * 64,
    )
    batch = tmp_path / 'batch'
    batch.mkdir(parents=True, exist_ok=True)
    if sharded:
        write_candidate_shards(candidate, batch, validate=validate_candidate)
    else:
        from app.services.strategy_candidate_shards import encode
        (batch / 'candidate_snapshot.json').write_bytes(encode(candidate))
    (batch / 'evaluation_snapshot.json').write_bytes(producer.encode_json(evaluation))
    return batch, candidate


@pytest.fixture
def app_factory(monkeypatch, tmp_path):
    batch, _ = _make_batch(tmp_path, sharded=True)
    monkeypatch.setenv('PANJI_STRATEGY_CANDIDATE_SNAPSHOT', str(batch / 'candidate_snapshot.json'))
    monkeypatch.setenv('STRATEGY_RESEARCH_SNAPSHOT', str(batch / 'evaluation_snapshot.json'))
    _load_cached.cache_clear()

    def make(kind: str):
        app = FastAPI()
        app.include_router(cand_api.router)
        app.include_router(eval_api.router)
        app.dependency_overrides[require_authenticated] = lambda: _ctx(kind)

        async def fake_db():
            yield object()
        app.dependency_overrides[get_db] = fake_db
        return TestClient(app, raise_server_exceptions=False)

    yield make
    _load_cached.cache_clear()


ENDPOINTS = ['/v1/strategy-research/catalog',
             '/v1/strategy-research/stocks/SH:600000/candidates',
             '/v1/strategy-research/zigzag/evaluation']


@pytest.mark.parametrize('kind,expected', [('strategy', 200), ('market', 403), ('admin', 200)])
def test_entitlements_over_real_sharded_snapshot(app_factory, kind, expected):
    client = app_factory(kind)
    for path in ENDPOINTS:
        assert client.get(path).status_code == expected, (kind, path)


def test_sharded_snapshot_serves_same_stock_payload(app_factory, tmp_path):
    """Readers must return the identical in-memory shape as a legacy file."""
    from app.services.strategy_candidate_snapshot import get_stock, read_dataset
    dataset = read_dataset()
    payload = get_stock(dataset, 'SH:600000')
    assert payload['candidates'], 'expected candidates from the sharded snapshot'
    for cand in payload['candidates']:
        assert cand['candidate_id'].startswith('SH:600000|')
        for obs in cand['observations']:
            assert obs['decision_date'] == obs['data_cutoff']
            assert obs['stage'] in ('D1', 'D2', 'D3')


def test_legacy_single_file_route_still_serves(app_factory, tmp_path, monkeypatch):
    batch, _ = _make_batch(tmp_path / 'legacy', sharded=False)
    monkeypatch.setenv('PANJI_STRATEGY_CANDIDATE_SNAPSHOT', str(batch / 'candidate_snapshot.json'))
    monkeypatch.setenv('STRATEGY_RESEARCH_SNAPSHOT', str(batch / 'evaluation_snapshot.json'))
    _load_cached.cache_clear()
    client = app_factory('strategy')
    for path in ENDPOINTS:
        assert client.get(path).status_code == 200, path


def test_missing_snapshots_are_503(app_factory, monkeypatch):
    client = app_factory('strategy')
    monkeypatch.delenv('PANJI_STRATEGY_CANDIDATE_SNAPSHOT')
    monkeypatch.delenv('STRATEGY_RESEARCH_SNAPSHOT')
    assert client.get('/v1/strategy-research/catalog').status_code == 503
    assert client.get('/v1/strategy-research/zigzag/evaluation').status_code == 503


def test_corrupt_shard_fails_closed_without_500(app_factory, tmp_path):
    """An altered candidate shard must make candidate reads unavailable (never 500).

    The evaluation endpoint reads its own separate snapshot, so it is expected
    to keep serving while the candidate side fails closed.
    """
    shard = next(p for p in sorted((tmp_path / 'batch').iterdir())
                 if p.name.startswith('candidate_shard_'))
    original = shard.read_bytes()
    shard.write_bytes(original.replace(b'"stocks"', b'"stocksX"', 1))
    try:
        client = app_factory('strategy')
        for path in ['/v1/strategy-research/catalog',
                     '/v1/strategy-research/stocks/SH:600000/candidates']:
            code = client.get(path).status_code
            assert code in (503, 409), (path, code)
    finally:
        shard.write_bytes(original)
