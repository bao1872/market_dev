"""F2 Market Dashboard API 合同（HTTP 层，复用 market_data permission）。

- 各端点 200 且返回派生 DTO（不碰 bars_daily / F1A）
- scope 不存在 → 404
- 无 token → 401（market_data 授权边界生效）
- lookback != 5 → 422（V1 产品合同锁死）
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.core.deps import get_db
from app.core.security import create_access_token
from app.main import app
from app.models.user import User
from tests.conftest import TestAsyncSessionLocal
from tests.test_market_dashboard_read_pg import _seed


def _auth_header(user: User) -> dict:
    token = create_access_token(user)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def api_client():
    async with TestAsyncSessionLocal() as s:
        async with s.begin():
            user = User(
                id=uuid4(),
                username="dashuser",
                email="dash@x.com",
                password_hash="x",
                status="active",
                is_admin=True,  # admin → 拥有 market_data 等全部 capability
            )
            s.add(user)

        def _override():
            ses = TestAsyncSessionLocal()

            async def _commit(*a, **k):
                await ses.flush()
                await ses.commit()

            ses.commit = _commit
            return ses

        app.dependency_overrides[get_db] = _override
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, user
        app.dependency_overrides.clear()


async def test_market_dashboard_api_endpoints_ok():
    async with TestAsyncSessionLocal() as s:
        async with s.begin():
            boards = await _seed(s)

    ac, user = api_client
    hdr = _auth_header(user)

    r_market = await ac.get("/v1/market-dashboard/market", headers=hdr)
    assert r_market.status_code == 200, r_market.text
    body = r_market.json()
    assert body["projection_trade_date"] == "2026-09-03"
    assert body["cards"]["ma20"] is None  # 最新一行 valid_count=0
    assert len(body["series"]) == 3

    r_rank = await ac.get(
        "/v1/market-dashboard/rankings?scope_type=industry&hierarchy_level=L1",
        headers=hdr,
    )
    assert r_rank.status_code == 200, r_rank.text
    assert r_rank.json()["top"][0]["board_id"] == str(boards["a"].id)

    r_scope = await ac.get(f"/v1/market-dashboard/scopes/{boards['a'].id}", headers=hdr)
    assert r_scope.status_code == 200, r_scope.text
    assert r_scope.json()["metadata"]["board_id"] == str(boards["a"].id)

    r_cmp = await ac.get(
        f"/v1/market-dashboard/compare?board_ids={boards['e'].id},{boards['f'].id}",
        headers=hdr,
    )
    assert r_cmp.status_code == 200, r_cmp.text
    assert len(r_cmp.json()["boards"]) == 2
    assert r_cmp.json()["boards"][0]["points"][0]["ew_index"] == pytest.approx(100.0)


async def test_market_dashboard_api_scope_404():
    async with TestAsyncSessionLocal() as s:
        async with s.begin():
            boards = await _seed(s)

    ac, user = api_client
    hdr = _auth_header(user)
    r = await ac.get(f"/v1/market-dashboard/scopes/{uuid4()}", headers=hdr)
    assert r.status_code == 404
    # 未激活 board → 404
    r2 = await ac.get(f"/v1/market-dashboard/scopes/{boards['d'].id}", headers=hdr)
    assert r2.status_code == 404


async def test_market_dashboard_api_unauth_401():
    ac, _ = api_client
    r = await ac.get("/v1/market-dashboard/market")
    assert r.status_code == 401


async def test_market_dashboard_api_invalid_lookback_422():
    async with TestAsyncSessionLocal() as s:
        async with s.begin():
            await _seed(s)

    ac, user = api_client
    hdr = _auth_header(user)
    r = await ac.get(
        "/v1/market-dashboard/rankings?scope_type=industry&hierarchy_level=L1&lookback=10",
        headers=hdr,
    )
    assert r.status_code == 422
