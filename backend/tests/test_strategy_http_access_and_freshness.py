"""Real FastAPI dependency-graph tests with synthetic files, no database/worker.

Run with PURE_UNIT_TEST=1. Do not use any production snapshot or live model.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategy_candidate_research as cand_api
from app.api import strategy_research as eval_api
from app.core.deps import get_db
from app.services.access_control_service import AccessContext, require_authenticated
from app.services.strategy_research_read_service import _load_cached


def _ctx(kind: str) -> AccessContext:
    admin = kind == "admin"
    active = kind in ("strategy", "admin")
    key = "market_data" if kind == "market" else "strategy_research"
    return AccessContext(
        user_id="fixture-user", account_status="active", roles=["admin" if admin else "member"],
        is_admin=admin, is_member=not admin, subscription_active=True, default_route="/strategy",
        capabilities={key: {"active": active}},
    )


def _candidate_data() -> dict:
    obs = {"observation_id": "obs-1", "stage": "D1", "decision_date": "2026-06-03", "data_cutoff": "2026-06-03",
           "l1_score": .73, "l2_score": .64, "l1_threshold": .7, "l2_threshold": .6,
           "joint_alert": True, "reference_price": 10.5, "reference_confirmed_at": "2026-06-02", "first_breakout": True}
    item = {"observation_id": "obs-1", "candidate_id": "candidate-1", "symbol": "SH:600000", "stage": "D1", "kind": "L",
            "l1_score": .73, "l2_score": .64, "joint_alert": True, "first_breakout": True}
    return {"schema": "panji-strategy-candidates-v2",
            "metadata": {"strategy_id": "zigzag_two_stage", "release_id": "release-1", "feature_sha": "test-only",
                         "audit_ref": "synthetic-test", "generated_at": "2026-06-04T10:00:00+08:00", "status": "AUDITED"},
            "stocks": {"SH:600000": {"bars": [
                {"date": "2026-06-01", "open": 10, "high": 11, "low": 9, "close": 10.5},
                {"date": "2026-06-02", "open": 10.5, "high": 10.7, "low": 9.9, "close": 10.3},
                {"date": "2026-06-03", "open": 10.3, "high": 11.4, "low": 10, "close": 11},
            ], "candidates": [{"candidate_id": "candidate-1", "anchor_date": "2026-06-01", "visible_at": "2026-06-02",
                              "kind": "L", "anchor_price": 9, "observations": [obs]}]}},
            "daily_batches": [{"trade_date": "2026-06-03", "release_id": "release-1", "status": "PUBLISHED", "items": [item]}]}


def _evaluation_data() -> dict:
    return {"schema": "panji-strategy-evaluation-v1",
            "metadata": {"strategy_key": "zigzag_two_stage", "strategy_name": "test-only", "version": "test-only", "generated_at": "2026-06-04",
                         "evaluation_protocol": "synthetic", "model_sha256": "test", "data_sha256": "test", "audit_ref": "test",
                         "status": "AUDITED", "cohort": "ALL_EX_ANTE_ALERTS"},
            "models": [{"id": "joint_d1", "name": "D1", "side": "BUY"}],
            "events": [{"event_id": "event-1", "model_id": "joint_d1", "side": "BUY", "symbol": "SH:600000",
                        "signal_time": "2026-06-03", "outcome": "SUCCESS", "mfe_pct": 11, "mae_pct": 2,
                        "candidate_id": "candidate-1", "observation_id": "obs-1", "stage": "D1",
                        "decision_date": "2026-06-03", "model_release_id": "release-1"}]}


@pytest.fixture
def app_factory(monkeypatch, tmp_path):
    candidate_file = tmp_path / "candidate.json"
    candidate_file.write_text(json.dumps(_candidate_data()), encoding="utf-8")
    evaluation_file = tmp_path / "evaluation.json"
    evaluation_file.write_text(json.dumps(_evaluation_data()), encoding="utf-8")
    monkeypatch.setenv("PANJI_STRATEGY_CANDIDATE_SNAPSHOT", str(candidate_file))
    monkeypatch.setenv("STRATEGY_RESEARCH_SNAPSHOT", str(evaluation_file))
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


@pytest.mark.parametrize("kind,expected", [("strategy",200),("market",403),("revoked",403),("expired",403),("admin",200)])
def test_http_entitlement_in_all_read_endpoints(app_factory, kind, expected):
    client = app_factory(kind)
    for path in ["/v1/strategy-research/catalog", "/v1/strategy-research/stocks/SH:600000/candidates",
                 "/v1/strategy-research/zigzag/evaluation"]:
        assert client.get(path).status_code == expected, (kind, path)


def test_release_mismatch_is_409_not_wrong_candidate(app_factory):
    client = app_factory("strategy")
    wrong = client.get("/v1/strategy-research/stocks/SH:600000/candidates", params={"release_id":"old-release"})
    assert wrong.status_code == 409
    good = client.get("/v1/strategy-research/stocks/SH:600000/candidates", params={"release_id":"release-1"})
    assert good.status_code == 200
    assert good.json()["candidates"][0]["candidate_id"] == "candidate-1"


def test_missing_research_snapshots_are_503_not_zero(app_factory, monkeypatch):
    client = app_factory("strategy")
    monkeypatch.delenv("PANJI_STRATEGY_CANDIDATE_SNAPSHOT")
    monkeypatch.delenv("STRATEGY_RESEARCH_SNAPSHOT")
    assert client.get("/v1/strategy-research/catalog").status_code == 503
    assert client.get("/v1/strategy-research/zigzag/evaluation").status_code == 503


@pytest.mark.asyncio
async def test_calendar_cutoff_uses_shanghai_time_and_existing_calendar(monkeypatch):
    fake = AsyncMock(return_value=date(2026, 10, 13))
    monkeypatch.setattr(cand_api, "get_most_recent_trading_day_async", fake)
    now = datetime(2026, 10, 14, 14, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert await cand_api._expected_latest_trade_day(object(), now) == "2026-10-13"
    assert fake.call_args.args[1] == date(2026, 10, 13)
    fake.return_value = date(2026, 10, 14)
    now = datetime(2026, 10, 14, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert await cand_api._expected_latest_trade_day(object(), now) == "2026-10-14"
    assert fake.call_args.args[1] == date(2026, 10, 14)


def test_latest_without_today_batch_fails_closed_but_history_still_available(app_factory, monkeypatch):
    # The frozen synthetic file contains 2026-06-03; 2026-06-04 should NOT
    # silently fall back to the 3rd as a 'today' recommendation.
    monkeypatch.setattr(cand_api, "get_most_recent_trading_day_async", AsyncMock(return_value=date(2026, 6, 4)))
    client = app_factory("strategy")
    stale = client.get("/v1/strategy-research/daily")
    assert stale.status_code == 503
    assert "未发布" in stale.json()["detail"]
    historical = client.get("/v1/strategy-research/daily", params={"trade_date": "2026-06-03"})
    assert historical.status_code == 200
    assert historical.json()["freshness"] == "HISTORICAL"
    assert len(historical.json()["items"]) == 1


def test_evaluation_payload_carries_complete_candidate_identity(app_factory):
    res = app_factory("strategy").get("/v1/strategy-research/zigzag/evaluation")
    assert res.status_code == 200
    record = res.json()["table"][0]
    assert (record["candidate_id"],record["observation_id"],record["stage"],record["model_release_id"]) == (
        "candidate-1", "obs-1", "D1", "release-1"
    )
