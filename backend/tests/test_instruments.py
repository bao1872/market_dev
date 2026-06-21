"""R3 股票主数据 API 测试。

测试内容：
1. GET /instruments: 列表查询 + 关键词搜索 + 分页
2. GET /instruments/{id}: 按 ID 查询（含 404）
3. GET /instruments/by-symbol/{symbol}: 按 symbol 查询（含 404）

测试策略：
- 使用 sqlite 内存数据库 + 异步 SQLAlchemy
- 每个测试独立事务，测试后回滚
- Mock 数据通过 fixture 注入
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.main import app
from app.models.instrument import Instrument


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """创建内存 SQLite 异步会话，测试后销毁。

    使用 aiosqlite 驱动（需安装 pytest-asyncio + aiosqlite）。
    仅创建 instruments 表（避免其他模型的外键依赖导致创建失败）。
    """
    try:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    except Exception:
        pytest.skip("aiosqlite 不可用，跳过 DB 测试")

    async with engine.begin() as conn:
        # 仅创建 instruments 表（避免 notification 等模型引用 users 表导致失败）
        await conn.run_sync(
            lambda sync_conn: Instrument.__table__.create(sync_conn, checkfirst=True)
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        # 注入测试数据
        test_instruments = [
            Instrument(
                id=uuid.uuid4(),
                symbol="600519",
                name="贵州茅台",
                market="SH",
                status="active",
                listing_date=date(2001, 8, 27),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            Instrument(
                id=uuid.uuid4(),
                symbol="000001",
                name="平安银行",
                market="SZ",
                status="active",
                listing_date=date(1991, 4, 3),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            Instrument(
                id=uuid.uuid4(),
                symbol="300750",
                name="宁德时代",
                market="SZ",
                status="active",
                listing_date=date(2018, 6, 11),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            Instrument(
                id=uuid.uuid4(),
                symbol="600036",
                name="招商银行",
                market="SH",
                status="suspended",
                listing_date=date(2002, 4, 9),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
        ]
        for inst in test_instruments:
            session.add(inst)
        await session.commit()

        # 将会话注入到 app 依赖
        async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
            yield session

        app.dependency_overrides[__import__("app.core.deps", fromlist=["get_db"]).get_db] = get_test_db

        yield session

        app.dependency_overrides.clear()

    await engine.dispose()


@pytest.mark.asyncio
async def test_list_instruments_default(db_session: AsyncSession) -> None:
    """测试默认列表查询（无筛选，分页）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 4
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert len(data["items"]) == 4


@pytest.mark.asyncio
async def test_list_instruments_pagination(db_session: AsyncSession) -> None:
    """测试分页查询。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 第一页，每页 2 条
        response = await client.get("/instruments", params={"page": 1, "page_size": 2})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 4
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) == 2
    assert data["pages"] == 2


@pytest.mark.asyncio
async def test_list_instruments_keyword_search(db_session: AsyncSession) -> None:
    """测试关键词搜索（symbol 模糊匹配）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments", params={"keyword": "600"})
    assert response.status_code == 200
    data = response.json()
    # 600519, 600036 包含 "600"
    assert data["total"] == 2
    symbols = [item["symbol"] for item in data["items"]]
    assert "600519" in symbols
    assert "600036" in symbols


@pytest.mark.asyncio
async def test_list_instruments_keyword_search_name(db_session: AsyncSession) -> None:
    """测试关键词搜索（name 模糊匹配）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments", params={"keyword": "银行"})
    assert response.status_code == 200
    data = response.json()
    # 平安银行、招商银行
    assert data["total"] == 2
    names = [item["name"] for item in data["items"]]
    assert "平安银行" in names
    assert "招商银行" in names


@pytest.mark.asyncio
async def test_list_instruments_market_filter(db_session: AsyncSession) -> None:
    """测试市场筛选。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments", params={"market": "SH"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    for item in data["items"]:
        assert item["market"] == "SH"


@pytest.mark.asyncio
async def test_list_instruments_status_filter(db_session: AsyncSession) -> None:
    """测试状态筛选。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments", params={"status": "suspended"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["symbol"] == "600036"
    assert data["items"][0]["status"] == "suspended"


@pytest.mark.asyncio
async def test_get_instrument_by_id(db_session: AsyncSession) -> None:
    """测试按 ID 查询。"""
    # 先获取一个 instrument 的 id
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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
async def test_get_instrument_by_id_not_found(db_session: AsyncSession) -> None:
    """测试按 ID 查询不存在（404）。"""
    fake_id = uuid.uuid4()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/instruments/{fake_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_instrument_by_symbol(db_session: AsyncSession) -> None:
    """测试按 symbol 查询。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments/by-symbol/000001")
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "000001"
    assert data["name"] == "平安银行"
    assert data["market"] == "SZ"


@pytest.mark.asyncio
async def test_get_instrument_by_symbol_not_found(db_session: AsyncSession) -> None:
    """测试按 symbol 查询不存在（404）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments/by-symbol/999999")
    assert response.status_code == 404


if __name__ == "__main__":
    # 自测入口：直接运行验证
    pytest.main([__file__, "-v", "--tb=short"])
