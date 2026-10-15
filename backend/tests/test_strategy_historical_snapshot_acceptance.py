"""Req 4/5/6 acceptance: real FastAPI HTTP acceptance over the generated historical
full-market snapshot (2026-08-17) and the pre-audited evaluation snapshot.

This exercises the exact endpoints the four frontend pages consume:
  - 个股 (individual stock candidates): /v1/strategy-research/stocks/{symbol}/candidates
  - 历史评价 (historical evaluation):   /v1/strategy-research/zigzag/evaluation
  - 历史日期机会查询 (historical date):  /v1/strategy-research/daily?trade_date=...
  - 模型管理 (model management):         /v1/admin/strategy-research/zigzag/model-releases

It also verifies Req 5 fail-closed: with no batch for the *expected latest* trade
day, /daily returns 503 and never substitutes an older batch as "today".

No production DB, model weights, or release mutation is touched. The historical
sharded snapshot + evaluation snapshot are read-only server-side files.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin_strategy_integration as admin_api
from app.api import strategy_candidate_research as cand_api
from app.api import strategy_research as eval_api
from app.core.deps import get_db
from app.services.access_control_service import AccessContext, require_admin, require_authenticated

HIST_BATCH = '/tmp/panji_14_snapshot/2026-08-17_89a5be0b28d1dac9'
CANDIDATE_SNAPSHOT = HIST_BATCH + '/candidate_snapshot.json'
EVALUATION_SNAPSHOT = HIST_BATCH + '/evaluation_snapshot.json'
RELEASE_ROOT = '/tmp/panji_14_snapshot'
RELEASE_ID = '89a5be0b28d1dac904ca07c7fd3bb75cd127f6b16663b694e145c5bc05395cdb'
HIST_TRADE_DATE = '2026-08-17'
SAMPLE_SYMBOL = 'SH:600000'
BUNDLE_SHA = 'aded1a1fed6a571abb371acd620da8a37cbe8e378904767b4c96e6022508c0ae'
BUNDLE_PATH = '/tmp/panji_14_global/two_stage_models_candidate_UNPUBLISHED.pkl'

PRESENT = all(__import__('pathlib').Path(p).is_file() for p in (CANDIDATE_SNAPSHOT, EVALUATION_SNAPSHOT))


def _ctx(kind: str) -> AccessContext:
    admin = kind == 'admin'
    active = kind in ('strategy', 'admin')
    key = 'market_data' if kind == 'market' else 'strategy_research'
    return AccessContext(
        user_id='acceptance-user', account_status='active',
        roles=['admin' if admin else 'member'], is_admin=admin, is_member=not admin,
        subscription_active=True, default_route='/strategy',
        capabilities={key: {'active': active}},
    )


async def _fake_db():
    yield object()


@pytest.fixture
def api_client(monkeypatch):
    if not PRESENT:
        pytest.skip('historical 14-stock snapshot not present')
    monkeypatch.setenv('PANJI_STRATEGY_CANDIDATE_SNAPSHOT', CANDIDATE_SNAPSHOT)
    monkeypatch.setenv('STRATEGY_RESEARCH_SNAPSHOT', EVALUATION_SNAPSHOT)
    monkeypatch.setenv('PANJI_STRATEGY_RELEASE_ROOT', RELEASE_ROOT)
    monkeypatch.setenv('PANJI_STRATEGY_ADMIN_WRITES_ENABLED', '0')

    app = FastAPI()
    app.include_router(cand_api.router)
    app.include_router(eval_api.router)
    app.include_router(admin_api.router)
    app.dependency_overrides[require_authenticated] = lambda: _ctx('strategy')
    app.dependency_overrides[require_admin] = lambda: _ctx('admin')
    app.dependency_overrides[get_db] = _fake_db
    return TestClient(app, raise_server_exceptions=False)


def test_catalog_serves_historical_full_market_metadata(api_client):
    # Req 4: the catalog page reads the frozen 2026-08-17 snapshot.
    res = api_client.get('/v1/strategy-research/catalog')
    assert res.status_code == 200, res.text
    body = res.json()
    assert body['stock_count'] == 14
    assert body['batch_count'] == 20
    assert body['metadata']['release_id'] == RELEASE_ID
    assert body['metadata']['status'] == 'AUDITED'


def test_stock_candidates_reads_sharded_snapshot(api_client):
    # Req 4 + Req 6: per-symbol candidates come from the sharded snapshot, not a
    # legacy single-file path, and the historical as-of date is never relabelled.
    res = api_client.get(f'/v1/strategy-research/stocks/{SAMPLE_SYMBOL}/candidates')
    assert res.status_code == 200, res.text
    body = res.json()
    assert body['symbol'] == SAMPLE_SYMBOL
    assert body['release_id'] == RELEASE_ID
    assert len(body['candidates']) > 0
    assert len(body['bars']) > 0
    # No symbol outside the published snapshot may be impersonated.
    missing = api_client.get('/v1/strategy-research/stocks/SH:999999/candidates')
    assert missing.status_code == 404


def test_historical_date_opportunity_query_is_explicitly_historical(api_client):
    # Req 4: an explicit trade_date is a historical replay; it must never be shown
    # as CURRENT market data.
    res = api_client.get('/v1/strategy-research/daily', params={'trade_date': HIST_TRADE_DATE})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body['freshness'] == 'HISTORICAL'
    assert body['trade_date'] == HIST_TRADE_DATE
    assert body['expected_trade_date'] == HIST_TRADE_DATE
    assert body['release_id'] == RELEASE_ID
    assert len(body['items']) > 0


def test_today_missing_batch_fails_closed_not_old_substitute(api_client, monkeypatch):
    # Req 5: with no batch for the expected latest trade day, /daily must return a
    # clear 503 "unavailable" state, never an empty list or a stale older batch.
    monkeypatch.setattr(
        cand_api, 'get_most_recent_trading_day_async',
        AsyncMock(return_value=date(2026, 10, 14)))
    res = api_client.get('/v1/strategy-research/daily')
    assert res.status_code == 503, res.text
    detail = res.json()['detail']
    assert '未发布' in detail or '禁止使用旧批次' in detail
    # And the historical query for the same real snapshot still works.
    hist = api_client.get('/v1/strategy-research/daily', params={'trade_date': HIST_TRADE_DATE})
    assert hist.status_code == 200 and hist.json()['freshness'] == 'HISTORICAL'


def test_historical_evaluation_page_payload(api_client):
    # Req 4: the historical evaluation page reads the pre-audited evaluation file.
    res = api_client.get('/v1/strategy-research/zigzag/evaluation', params={'side': 'BUY', 'gain_pct': 5, 'risk_pct': 3})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body['metadata']['strategy_key'] == 'zigzag_two_stage'
    assert body['metadata']['status'] == 'AUDITED'
    # side=BUY selects the 3 BUY models (6 total = 3 BUY + 3 SELL).
    assert len(body['models']) == 3
    for row in body['table']:
        assert row['candidate_id'] and row['observation_id'] and row['stage']


def test_model_management_exposes_activated_release(api_client):
    # Req 4: model management reads the activated release record (model pointer).
    res = api_client.get('/v1/admin/strategy-research/zigzag/model-releases')
    assert res.status_code == 200, res.text
    body = res.json()
    assert body['active_release'] is not None
    active = body['active_release']
    assert active['schema'] == 'panji-strategy-active-release-v1'
    assert active['release_id'] == RELEASE_ID
    # The model bundle the active release pins is the historical global bundle.
    assert active['bundle_sha256'] == BUNDLE_SHA
    assert active['bundle_path'] == BUNDLE_PATH


def test_permission_gates_read_endpoints():
    # Req 6: without the strategy_research capability, read endpoints are 403.
    app = FastAPI()
    app.include_router(cand_api.router)
    app.include_router(eval_api.router)
    app.dependency_overrides[require_authenticated] = lambda: _ctx('market')
    app.dependency_overrides[get_db] = _fake_db
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get('/v1/strategy-research/catalog').status_code == 403
    assert c.get(f'/v1/strategy-research/stocks/{SAMPLE_SYMBOL}/candidates').status_code == 403


def test_release_version_mismatch_is_409(api_client):
    # Req 4/6: a stale release_id must not map old candidates to a different version.
    res = api_client.get(
        f'/v1/strategy-research/stocks/{SAMPLE_SYMBOL}/candidates',
        params={'release_id': 'old-release-id-not-matching'})
    assert res.status_code == 409, res.text
