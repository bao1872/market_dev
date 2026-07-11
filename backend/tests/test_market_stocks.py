"""行情列表 API 测试（PRD §8.1 / §12.1）。

测试内容：
1. GET /market/stocks?scope=market: 全市场列表 + 分页
2. GET /market/stocks?scope=watchlist: 仅返回用户自选
3. 搜索关键词 query
4. is_watchlisted 标记
5. 响应结构（items + page + page_size + total + as_of）
6. page_size 上限 100
7. 未认证返回 401

测试策略：
- 使用 conftest 的 db_session / client fixtures（PostgreSQL 测试库）
- 通过 dependency_overrides 注入认证用户
- 固定 SQL 数量，无逐行查询（通过响应结构间接验证）
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_active_user
from app.main import app
from app.models.instrument import Instrument
from app.models.user import User
from app.models.watchlist import UserWatchlistItem


@pytest_asyncio.fixture
async def market_stocks_client(
    client: AsyncClient,
    db_session: AsyncSession,
    user_factory,
    instrument_factory,
    subscription_factory,
) -> AsyncGenerator[tuple[AsyncClient, User, list[Instrument]], None]:
    """提供已认证 HTTP 客户端 + 测试用户 + 3 只测试标的 + 订阅。"""
    user = await user_factory(
        email="market_user@example.com",
        password_hash="fake-hash",
        timezone="Asia/Shanghai",
    )
    inst1 = await instrument_factory(
        symbol="600519", name="贵州茅台", market="SH", status="active",
    )
    inst2 = await instrument_factory(
        symbol="000001", name="平安银行", market="SZ", status="active",
    )
    inst3 = await instrument_factory(
        symbol="300750", name="宁德时代", market="SZ", status="active",
    )
    await subscription_factory(
        user_id=user.id,
        plan_code="observe_20",
        status="active",
        starts_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=30),
        source="invite",
    )

    # 直接创建一条自选记录（inst1）
    wl_item = UserWatchlistItem(
        user_id=user.id,
        instrument_id=inst1.id,
        source="manual",
        active=True,
    )
    db_session.add(wl_item)
    await db_session.flush()

    async def get_test_user() -> User:
        return user

    app.dependency_overrides[get_current_active_user] = get_test_user

    yield client, user, [inst1, inst2, inst3]

    app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.asyncio
async def test_market_scope_list(market_stocks_client) -> None:
    """测试 market scope 列表查询：返回全部活跃 A 股。"""
    client, _, instruments = market_stocks_client
    response = await client.get("/market/stocks", params={"scope": "market"})
    assert response.status_code == 200
    data = response.json()
    # 响应结构
    assert "items" in data
    assert "page" in data
    assert "page_size" in data
    assert "total" in data
    assert "as_of" in data
    # 至少包含测试标的
    assert data["total"] >= 3
    assert data["page"] == 1
    assert data["page_size"] == 50
    symbols = {item["symbol"] for item in data["items"]}
    assert "600519" in symbols
    assert "000001" in symbols


@pytest.mark.asyncio
async def test_market_scope_is_watchlisted(market_stocks_client) -> None:
    """测试 is_watchlisted 标记：inst1 在自选中，inst2 不在。"""
    client, _, instruments = market_stocks_client
    response = await client.get("/market/stocks", params={"scope": "market"})
    assert response.status_code == 200
    items = response.json()["items"]
    by_symbol = {item["symbol"]: item for item in items}
    assert by_symbol["600519"]["is_watchlisted"] is True
    assert by_symbol["000001"]["is_watchlisted"] is False


@pytest.mark.asyncio
async def test_watchlist_scope(market_stocks_client) -> None:
    """测试 watchlist scope：仅返回用户自选。"""
    client, _, instruments = market_stocks_client
    response = await client.get("/market/stocks", params={"scope": "watchlist"})
    assert response.status_code == 200
    data = response.json()
    # 仅 inst1 在自选中
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["symbol"] == "600519"
    assert data["items"][0]["is_watchlisted"] is True


@pytest.mark.asyncio
async def test_pagination(market_stocks_client) -> None:
    """测试分页查询。"""
    client, _, _ = market_stocks_client
    response = await client.get(
        "/market/stocks", params={"scope": "market", "page": 1, "page_size": 2}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) <= 2


@pytest.mark.asyncio
async def test_page_size_max_100(market_stocks_client) -> None:
    """测试 page_size 上限 100：超过 100 应返回 422。"""
    client, _, _ = market_stocks_client
    response = await client.get(
        "/market/stocks", params={"scope": "market", "page_size": 200}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_query(market_stocks_client) -> None:
    """测试搜索关键词（名称包含）。"""
    client, _, _ = market_stocks_client
    response = await client.get(
        "/market/stocks", params={"scope": "market", "query": "茅台"}
    )
    assert response.status_code == 200
    data = response.json()
    symbols = {item["symbol"] for item in data["items"]}
    assert "600519" in symbols
    # 不应包含不匹配的
    assert "000001" not in symbols


@pytest.mark.asyncio
async def test_row_fields(market_stocks_client) -> None:
    """测试每行返回页面所需全部字段。"""
    client, _, _ = market_stocks_client
    response = await client.get("/market/stocks", params={"scope": "market"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) > 0
    row = items[0]
    # 必需字段
    assert "instrument_id" in row
    assert "symbol" in row
    assert "name" in row
    assert "latest_price" in row
    assert "change_pct" in row
    assert "industry" in row
    assert "concepts" in row
    assert "dsa_state" in row
    assert "structure_state" in row
    assert "latest_event_title" in row
    assert "latest_event_time" in row
    assert "is_watchlisted" in row
    # industry/concepts 在 Phase 6 前为 null/空
    assert row["industry"] is None
    assert row["concepts"] == []


@pytest.mark.asyncio
async def test_unauthenticated_returns_401(client: AsyncClient) -> None:
    """测试未认证返回 401。"""
    response = await client.get("/market/stocks", params={"scope": "market"})
    assert response.status_code == 401


