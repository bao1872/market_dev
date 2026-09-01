"""Real HTTP endpoint contract tests for the Auction V3.2 API (Finding 4).

These issue actual HTTP requests through FastAPI (``TestClient``), which the
pure dependency-inspection tests cannot do.  They prove:

- the entitlement guard produces a real HTTP 403 without ``research_replay``;
- a published fixture produces HTTP 200 with the declared response schema;
- the detail endpoint resolves the CANONICAL scope_key and refuses a display
  name used as a key;
- meta/dates is guarded and shaped correctly.

Only the DB-loading helper is faked (explicitly "fake DB result"); the routing,
guard chain, read model, payload parsing and DTO mapping all run for real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import auction as auction_api
from app.domain.auction.scope_payload import build_scope_payload
from app.domain.auction.version import V32_ALGORITHM_VERSION
from app.main import app
from app.services.access_control_service import (
    AccessContext,
    require_authenticated,
)

_TRADE_DATE = date(2026, 8, 14)

# canonical identity: key and display name are deliberately DIFFERENT
_SCOPE_KEY = "CPT_ROBOT"
_SCOPE_NAME = "机器人"

_RUN_A = uuid4()
_RUN_B = uuid4()  # newer, UNPUBLISHED -> must stay invisible


@dataclass
class FakePublication:
    trade_date: date
    algorithm_version: str
    scan_run_id: UUID
    published_at: datetime


@dataclass
class FakeScopeResult:
    scan_run_id: UUID
    trade_date: date
    scope_type: str
    scope_id: UUID | None
    scope_name: str | None
    payload: dict[str, Any] = field(default_factory=dict)


def _payload() -> dict:
    return build_scope_payload(
        algorithm_version=V32_ALGORITHM_VERSION,
        identity={"scope_key": _SCOPE_KEY, "scope_name": _SCOPE_NAME},
        repricing={
            "equal_weight_gap": 0.0123,
            "amount_weighted_gap": 0.0140,
            "capital_tilt": 0.0017,
            "positive_gap_breadth": 0.55,
            "negative_gap_breadth": 0.30,
            "unchanged_gap_breadth": 0.15,
            "gap_dispersion": 0.011,
            "price_normalized_hhi": 0.28,
            "price_valid_count": 20,
        },
        historical_dynamics={
            "position": 72.5,
            "ema_fast": 70.0,
            "ema_slow": 66.0,
            "velocity": 4.0,
            "signal": 3.1,
            "acceleration": 0.9,
        },
        participation={
            "total_auction_amount": 5_000_000.0,
            "amount_position": 88.0,
            "amount_multiple": 1.75,
            "amount_abnormal_breadth": 0.35,
            "top1_amount_share": 0.18,
            "top3_amount_share": 0.46,
            "amount_normalized_hhi": 0.41,
        },
        # metric keys follow the cross-sectional AXES declaration, so the
        # per-axis primary position resolves as designed
        cross_sectional={
            "repricing": {"equal_weight_gap": 80.0},
            "breadth": {"positive_gap_breadth": 70.0},
            "participation": {"amount_historical_position": 88.0},
            "concentration": {"price_normalized_hhi": 60.0},
        },
        member_attribution={
            "members": [],
            "leadership_migration": 0.25,
            "retained": [],
            "entrants": [],
            "exits": [],
            "jaccard": 0.75,
        },
    )


def _published_fixture() -> tuple[list[Any], list[Any]]:
    """Only RUN_A is published; RUN_B is newer but must stay invisible."""
    publications = [
        FakePublication(
            _TRADE_DATE,
            V32_ALGORITHM_VERSION,
            _RUN_A,
            datetime(2026, 8, 14, 9, 40, tzinfo=UTC),
        )
    ]
    results = [
        FakeScopeResult(
            scan_run_id=_RUN_A,
            trade_date=_TRADE_DATE,
            scope_type="concept",
            scope_id=uuid4(),
            # the DB column holds the DISPLAY label; identity lives in payload
            scope_name=_SCOPE_NAME,
            payload=_payload(),
        ),
        FakeScopeResult(
            scan_run_id=_RUN_B,
            trade_date=_TRADE_DATE,
            scope_type="concept",
            scope_id=uuid4(),
            scope_name="未发布新结果",
            payload=_payload(),
        ),
    ]
    return publications, results


def _ctx(capabilities: dict | None, is_admin: bool = False) -> AccessContext:
    return AccessContext(
        user_id=str(uuid4()),
        email="tester@example.com",
        account_status="active",
        roles=["admin"] if is_admin else ["member"],
        is_admin=is_admin,
        is_member=not is_admin,
        subscription_active=True,
        subscription_expires_at=datetime.now(UTC) + timedelta(days=30),
        plan_code=None,
        plan_display_name=None,
        features=[],
        limits={},
        capabilities=capabilities if capabilities is not None else {},
        default_route="",
    )


async def _fake_published_loader(db: Any, trade_date: date) -> tuple[list[Any], list[Any]]:
    """Stand-in for the DB loader (endpoints await it)."""
    return _published_fixture()


@pytest.fixture()
def http_client():
    """TestClient wrapper.

    Deliberately NOT named ``http_client``: that name is reserved by conftest's DB
    fixture set and would auto-classify this file as postgres, silently
    skipping every HTTP contract test under PURE_UNIT_TEST=1.
    """
    yield TestClient(app)
    app.dependency_overrides.clear()


def _grant(capabilities: dict | None) -> None:
    ctx = _ctx(capabilities)
    app.dependency_overrides[require_authenticated] = lambda: ctx


# ---------------------------------------------------------------------------
# entitlement at the HTTP layer
# ---------------------------------------------------------------------------
def test_scopes_without_capability_is_403(http_client: TestClient) -> None:
    _grant({})  # logged in, missing research_replay
    response = http_client.get(
        "/v1/auction/scopes",
        params={"trade_date": _TRADE_DATE.isoformat(), "family": "concept"},
    )
    assert response.status_code == 403


def test_detail_without_capability_is_403(http_client: TestClient) -> None:
    _grant({})
    response = http_client.get(
        f"/v1/auction/scopes/{_SCOPE_KEY}",
        params={"trade_date": _TRADE_DATE.isoformat(), "family": "concept"},
    )
    assert response.status_code == 403


def test_meta_dates_without_capability_is_403(http_client: TestClient) -> None:
    _grant({})
    assert http_client.get("/v1/auction/meta/dates").status_code == 403


def test_admin_bypasses_the_capability(http_client: TestClient, monkeypatch) -> None:
    admin_ctx = _ctx({}, is_admin=True)
    app.dependency_overrides[require_authenticated] = lambda: admin_ctx
    monkeypatch.setattr(
        auction_api, "_load_publications_and_results", _fake_published_loader
    )
    response = http_client.get(
        "/v1/auction/scopes",
        params={"trade_date": _TRADE_DATE.isoformat(), "family": "concept"},
    )
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# list endpoint: 200 + schema + complete snapshot + unpublished invisible
# ---------------------------------------------------------------------------
def test_scopes_with_capability_returns_200_and_schema(
    http_client: TestClient, monkeypatch
) -> None:
    _grant({"research_replay": {"active": True}})
    monkeypatch.setattr(
        auction_api, "_load_publications_and_results", _fake_published_loader
    )
    response = http_client.get(
        "/v1/auction/scopes",
        params={"trade_date": _TRADE_DATE.isoformat(), "family": "concept"},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["trade_date"] == _TRADE_DATE.isoformat()
    assert body["family"] == "concept"
    assert body["algorithm_version"] == V32_ALGORITHM_VERSION
    assert body["schema_version"] == "auction-scope-v3.2"
    # only the PUBLISHED run is visible (RUN_B excluded)
    assert body["total_scopes"] == 1
    assert len(body["scopes"]) == 1

    row = body["scopes"][0]
    assert row["scope_key"] == _SCOPE_KEY
    assert row["scope_name"] == _SCOPE_NAME
    assert row["equal_weight_gap"] == pytest.approx(0.0123)
    assert row["ew_position"] == pytest.approx(72.5)
    assert row["amount_historical_position"] == pytest.approx(88.0)
    assert row["price_valid_count"] == 20
    # the list DTO exposes ONE cross-sectional position per axis (flat)
    # concentration has NO frozen representative, so it stays unavailable
    assert row["cross_sectional"] == {
        "repricing": pytest.approx(80.0),
        "breadth": pytest.approx(70.0),
        "participation": pytest.approx(88.0),
        "concentration": None,
    }


def test_unpublished_day_is_404_not_empty_list(
    http_client: TestClient, monkeypatch
) -> None:
    """No publication -> unavailable, never an empty ranking."""
    _grant({"research_replay": {"active": True}})
    async def _empty_loader(db: Any, trade_date: date) -> tuple[list[Any], list[Any]]:
        return [], []

    monkeypatch.setattr(auction_api, "_load_publications_and_results", _empty_loader)
    response = http_client.get(
        "/v1/auction/scopes",
        params={"trade_date": _TRADE_DATE.isoformat(), "family": "concept"},
    )
    assert response.status_code == 404


def test_invalid_family_is_rejected(http_client: TestClient) -> None:
    _grant({"research_replay": {"active": True}})
    response = http_client.get(
        "/v1/auction/scopes",
        params={"trade_date": _TRADE_DATE.isoformat(), "family": "sector"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Finding 1: canonical key vs display name
# ---------------------------------------------------------------------------
def test_detail_resolves_canonical_scope_key(
    http_client: TestClient, monkeypatch
) -> None:
    _grant({"research_replay": {"active": True}})
    monkeypatch.setattr(
        auction_api, "_load_publications_and_results", _fake_published_loader
    )
    response = http_client.get(
        f"/v1/auction/scopes/{_SCOPE_KEY}",
        params={"trade_date": _TRADE_DATE.isoformat(), "family": "concept"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["scope_key"] == _SCOPE_KEY
    assert body["scope_name"] == _SCOPE_NAME
    # all five canonical groups present
    for group in (
        "repricing",
        "historical_dynamics",
        "participation",
        "cross_sectional",
        "member_attribution",
        "diagnostics",
    ):
        assert group in body


def test_detail_rejects_display_name_used_as_key(
    http_client: TestClient, monkeypatch
) -> None:
    """The display name must never work as a product key."""
    _grant({"research_replay": {"active": True}})
    monkeypatch.setattr(
        auction_api, "_load_publications_and_results", _fake_published_loader
    )
    response = http_client.get(
        f"/v1/auction/scopes/{_SCOPE_NAME}",
        params={"trade_date": _TRADE_DATE.isoformat(), "family": "concept"},
    )
    assert response.status_code == 404


def test_detail_diagnostics_hold_the_technical_ids(
    http_client: TestClient, monkeypatch
) -> None:
    _grant({"research_replay": {"active": True}})
    monkeypatch.setattr(
        auction_api, "_load_publications_and_results", _fake_published_loader
    )
    body = http_client.get(
        f"/v1/auction/scopes/{_SCOPE_KEY}",
        params={"trade_date": _TRADE_DATE.isoformat(), "family": "concept"},
    ).json()
    assert "scan_run_id" in body["diagnostics"]
    assert "scope_id" in body["diagnostics"]
    # and not leaked into the business groups
    assert "scan_run_id" not in body["repricing"]


# ---------------------------------------------------------------------------
# meta/dates
# ---------------------------------------------------------------------------
def test_meta_dates_returns_published_dates_only(
    http_client: TestClient, monkeypatch
) -> None:
    _grant({"research_replay": {"active": True}})
    older = date(2026, 8, 13)

    async def _fake_loader(db, trade_date):
        publications = [
            FakePublication(
                _TRADE_DATE,
                V32_ALGORITHM_VERSION,
                _RUN_A,
                datetime(2026, 8, 14, 9, 40, tzinfo=UTC),
            ),
            # legacy algorithm -> must NOT be listed as a V3.2 published date
            FakePublication(
                older,
                "auction-legacy",
                uuid4(),
                datetime(2026, 8, 13, 9, 40, tzinfo=UTC),
            ),
        ]
        return publications, []

    monkeypatch.setattr(auction_api, "_load_publications_and_results", _fake_loader)

    async def _fake_all_pubs(db):
        return [
            FakePublication(
                _TRADE_DATE,
                V32_ALGORITHM_VERSION,
                _RUN_A,
                datetime(2026, 8, 14, 9, 40, tzinfo=UTC),
            ),
            FakePublication(
                older,
                "auction-legacy",
                uuid4(),
                datetime(2026, 8, 13, 9, 40, tzinfo=UTC),
            ),
        ]

    monkeypatch.setattr(auction_api, "_load_all_publications", _fake_all_pubs)

    response = http_client.get("/v1/auction/meta/dates")
    assert response.status_code == 200
    body = response.json()
    assert body["trade_dates"] == [_TRADE_DATE.isoformat()]
    assert body["latest"] == _TRADE_DATE.isoformat()
