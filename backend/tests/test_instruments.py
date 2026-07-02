"""R3 股票主数据 API 测试。

测试内容：
1. GET /instruments: 列表查询 + 关键词搜索 + 分页
2. GET /instruments/{id}: 按 ID 查询（含 404）
3. GET /instruments/by-symbol/{symbol}: 按 symbol 查询（含 404）

测试策略：
- 使用 conftest 的 db_session / client fixtures（PostgreSQL 测试库）
- 每个测试独立事务，测试后回滚
- Mock 数据通过 instrument_factory 注入
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def instruments_fixture(db_session: AsyncSession, instrument_factory):
    """预置 4 只测试标的（含 1 只指数，用于验证默认过滤）。"""
    instruments = [
        await instrument_factory(
            symbol="600519", name="贵州茅台", market="SH", status="active",
            listing_date=date(2001, 8, 27),
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        ),
        await instrument_factory(
            symbol="000001", name="平安银行", market="SZ", status="active",
            listing_date=date(1991, 4, 3),
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        ),
        await instrument_factory(
            symbol="300750", name="宁德时代", market="SZ", status="active",
            listing_date=date(2018, 6, 11),
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        ),
        await instrument_factory(
            symbol="600036", name="招商银行", market="SH", status="suspended",
            listing_date=date(2002, 4, 9),
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        ),
        await instrument_factory(
            symbol="000032", name="上证能源", market="SH", status="active",
            listing_date=date(2002, 4, 9),
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        ),
    ]
    return instruments


@pytest.mark.asyncio
async def test_list_instruments_default(client: AsyncClient, instruments_fixture) -> None:
    """测试默认列表查询：应排除指数/ETF/基金，只返回 A 股股票。"""
    response = await client.get("/instruments")
    assert response.status_code == 200
    data = response.json()
    # fixture 共 5 条，其中 000032 上证能源（SH 指数）应被过滤
    assert data["total"] == 4
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert len(data["items"]) == 4
    symbols = {item["symbol"] for item in data["items"]}
    assert "000032" not in symbols
    assert "600519" in symbols


@pytest.mark.asyncio
async def test_list_instruments_pagination(client: AsyncClient, instruments_fixture) -> None:
    """测试分页查询（指数已被默认过滤）。"""
    # 第一页，每页 2 条
    response = await client.get("/instruments", params={"page": 1, "page_size": 2})
    assert response.status_code == 200
    data = response.json()
    # 过滤后共 4 条 A 股股票
    assert data["total"] == 4
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) == 2
    assert data["pages"] == 2


@pytest.mark.asyncio
async def test_list_instruments_keyword_search(client: AsyncClient, instruments_fixture) -> None:
    """测试关键词搜索（symbol 模糊匹配）。"""
    response = await client.get("/instruments", params={"keyword": "600"})
    assert response.status_code == 200
    data = response.json()
    # 600519, 600036 包含 "600"
    assert data["total"] == 2
    symbols = [item["symbol"] for item in data["items"]]
    assert "600519" in symbols
    assert "600036" in symbols


@pytest.mark.asyncio
async def test_list_instruments_keyword_search_excludes_index(client: AsyncClient, instruments_fixture) -> None:
    """测试关键词搜索指数代码 000032 不应返回上证能源。"""
    response = await client.get("/instruments", params={"keyword": "000032"})
    assert response.status_code == 200
    data = response.json()
    # 000032 上证能源是 SH 指数，应被默认 A 股过滤排除
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_instruments_keyword_search_name(client: AsyncClient, instruments_fixture) -> None:
    """测试关键词搜索（name 模糊匹配）。"""
    response = await client.get("/instruments", params={"keyword": "银行"})
    assert response.status_code == 200
    data = response.json()
    # 平安银行、招商银行
    assert data["total"] == 2
    names = [item["name"] for item in data["items"]]
    assert "平安银行" in names
    assert "招商银行" in names


@pytest.mark.asyncio
async def test_list_instruments_market_filter(client: AsyncClient, instruments_fixture) -> None:
    """测试市场筛选。"""
    response = await client.get("/instruments", params={"market": "SH"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    for item in data["items"]:
        assert item["market"] == "SH"


@pytest.mark.asyncio
async def test_list_instruments_status_filter(client: AsyncClient, instruments_fixture) -> None:
    """测试状态筛选。"""
    response = await client.get("/instruments", params={"status": "suspended"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["symbol"] == "600036"
    assert data["items"][0]["status"] == "suspended"


@pytest.mark.asyncio
async def test_get_instrument_by_id(client: AsyncClient, instruments_fixture) -> None:
    """测试按 ID 查询。"""
    # 先获取一个 instrument 的 id
    list_response = await client.get("/instruments", params={"keyword": "600519"})
    instrument_id = list_response.json()["items"][0]["id"]

    # 按 ID 查询
    response = await client.get(f"/instruments/{instrument_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "600519"
    assert data["name"] == "贵州茅台"
    assert data["market"] == "SH"


@pytest.mark.asyncio
async def test_get_instrument_by_id_not_found(client: AsyncClient) -> None:
    """测试按 ID 查询不存在（404）。"""
    fake_id = uuid.uuid4()
    response = await client.get(f"/instruments/{fake_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_instrument_by_symbol(client: AsyncClient, instruments_fixture) -> None:
    """测试按 symbol 查询。"""
    response = await client.get("/instruments/by-symbol/000001")
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "000001"
    assert data["name"] == "平安银行"
    assert data["market"] == "SZ"


@pytest.mark.asyncio
async def test_get_instrument_by_symbol_not_found(client: AsyncClient) -> None:
    """测试按 symbol 查询不存在（404）。"""
    response = await client.get("/instruments/by-symbol/999999")
    assert response.status_code == 404


if __name__ == "__main__":
    # 自测入口：直接运行验证
    pytest.main([__file__, "-v", "--tb=short"])
