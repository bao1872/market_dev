"""Endpoint-level contract tests for the Auction V3.2 API (KPI-6).

These complement ``test_auction_v32_api_contract.py``: that file proves the
READ-MODEL behaviour, this one proves the ENDPOINTS themselves carry the formal
entitlement guard and that the guard really returns 403 without the capability.

Pattern is reused from ``test_auction_replay_entitlement.py`` (dependency-tree
static assertion + direct dependency invocation).  No new test framework and no
new auction capability: the machine value stays ``research_replay``.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.api import auction as auction_api
from app.services.access_control_service import (
    AccessContext,
    require_capability,
)

V32_ENDPOINTS = [
    auction_api.list_auction_scopes,
    auction_api.get_auction_scope_detail,
    auction_api.list_auction_scope_dates,
]

_EXPIRES = datetime.now(UTC) + timedelta(days=30)


def _ctx(
    *,
    is_admin: bool = False,
    capabilities: dict | None = None,
) -> AccessContext:
    return AccessContext(
        user_id=str(uuid.uuid4()),
        email="tester@example.com",
        account_status="active",
        roles=["admin"] if is_admin else ["member"],
        is_admin=is_admin,
        is_member=not is_admin,
        subscription_active=True,
        subscription_expires_at=_EXPIRES,
        plan_code=None,
        plan_display_name=None,
        features=[],
        limits={},
        capabilities=capabilities if capabilities is not None else {},
        default_route="",
    )


def _ctx_dependency_call(endpoint) -> object:
    """Return the Depends(...) callable bound to the endpoint's ``ctx`` param."""
    sig = inspect.signature(endpoint)
    param = sig.parameters["ctx"]
    return param.default.dependency


# ---------------------------------------------------------------------------
# guard presence
# ---------------------------------------------------------------------------
def test_capability_machine_value_is_research_replay() -> None:
    """No dedicated auction capability may be invented."""
    assert auction_api.AUCTION_CAPABILITY == "research_replay"


@pytest.mark.parametrize("endpoint", V32_ENDPOINTS)
def test_v32_endpoint_declares_the_guard(endpoint) -> None:
    """Each V3.2 endpoint must expose the injected ``ctx`` parameter."""
    assert "ctx" in inspect.signature(endpoint).parameters


@pytest.mark.parametrize("endpoint", V32_ENDPOINTS)
def test_v32_endpoint_binds_research_replay(endpoint) -> None:
    dep = _ctx_dependency_call(endpoint)
    assert dep.__closure__ is not None
    captured = [c.cell_contents for c in dep.__closure__]
    assert "research_replay" in captured, f"{endpoint.__name__} 未绑定 research_replay"


@pytest.mark.parametrize("endpoint", V32_ENDPOINTS)
def test_v32_endpoint_guard_is_not_require_authenticated(endpoint) -> None:
    dep = _ctx_dependency_call(endpoint)
    assert getattr(dep, "__name__", "") != "require_authenticated"


# ---------------------------------------------------------------------------
# guard behaviour: 403 vs pass
# ---------------------------------------------------------------------------
async def test_without_capability_is_403() -> None:
    dep = require_capability("research_replay")
    with pytest.raises(HTTPException) as exc:
        await dep(ctx=_ctx(capabilities={}))
    assert exc.value.status_code == 403


def test_with_capability_passes() -> None:
    dep = require_capability("research_replay")
    ctx = dep(
        ctx=_ctx(capabilities={"research_replay": {"active": True}}),
    )
    assert ctx is not None


def test_admin_is_exempt() -> None:
    dep = require_capability("research_replay")
    ctx = dep(ctx=_ctx(is_admin=True, capabilities={}))
    assert ctx is not None


async def test_inactive_capability_is_403() -> None:
    dep = require_capability("research_replay")
    with pytest.raises(HTTPException) as exc:
        await dep(ctx=_ctx(capabilities={"research_replay": {"active": False}}))
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# endpoint request-shape contracts (behaviour, not source text)
# ---------------------------------------------------------------------------
def test_list_endpoint_requests_family_and_trade_date() -> None:
    params = inspect.signature(auction_api.list_auction_scopes).parameters
    assert "family" in params
    assert "trade_date" in params


def test_detail_endpoint_requests_scope_key_and_family() -> None:
    params = inspect.signature(auction_api.get_auction_scope_detail).parameters
    assert "scope_key" in params
    assert "family" in params


def test_family_default_is_industry() -> None:
    """The list endpoint defaults to the industry family (product contract)."""
    param = inspect.signature(auction_api.list_auction_scopes).parameters["family"]
    assert param.default.default == "industry"


def test_routes_are_registered_under_v1_auction() -> None:
    paths = {r.path for r in auction_api.router.routes}
    assert {
        "/v1/auction/scopes",
        "/v1/auction/scopes/{scope_key}",
        "/v1/auction/meta/dates",
    } <= paths
