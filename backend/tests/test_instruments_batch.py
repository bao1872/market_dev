"""POST /instruments/batch 批量查询股票 API 测试。

测试内容：
1. 批量查询返回正确数量（传入已存在的 ID 列表）
2. 空 ids 列表返回 422（InstrumentBatchRequest schema min_length=1 校验拦截）
3. 不存在的 ID 列表返回空数组（total=0，items=[]）

测试策略：
- 使用 sqlite 内存数据库 + 异步 SQLAlchemy（与 test_instruments.py 同模式，自包含不依赖 PostgreSQL）
- 每个测试独立事务，测试后销毁
- Mock 数据通过 fixture 注入

注意：
- InstrumentBatchRequest schema 强制 ids: list[UUID] min_length=1，空列表会被 Pydantic 拒绝（422）
- 不存在的 ID 不会触发 404，只是不出现在返回结果中（IN 查询的天然行为）
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


@pytest_asyncio.fixture(scope="session", autouse=True)
async def init_test_db():
    """覆盖 conftest.py 的 init_test_db（本测试使用 sqlite 内存数据库，不需要 Alembic 迁移）。

    conftest.py 的 init_test_db 会执行 Alembic 迁移到 TEST_DATABASE_URL 指向的 PostgreSQL 测试库，
    但本测试完全自包含（sqlite 内存数据库），覆盖为 no-op 避免对 PostgreSQL 测试库的依赖。
    """
    yield


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """创建内存 SQLite 异步会话，测试后销毁。

    使用 aiosqlite 驱动（与 test_instruments.py 同模式）。
    仅创建 instruments 表（避免其他模型的外键依赖导致创建失败）。
    """
    try:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    except Exception:
        pytest.skip("aiosqlite 不可用，跳过 DB 测试")

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Instrument.__table__.create(sync_conn, checkfirst=True)
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        # 注入测试数据（3 条，使用固定 UUID 便于批量查询测试）
        test_id_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
        test_id_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
        test_id_3 = uuid.UUID("00000000-0000-0000-0000-000000000003")
        test_instruments = [
            Instrument(
                id=test_id_1,
                symbol="600519",
                name="贵州茅台",
                market="SH",
                status="active",
                listing_date=date(2001, 8, 27),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            Instrument(
                id=test_id_2,
                symbol="000001",
                name="平安银行",
                market="SZ",
                status="active",
                listing_date=date(1991, 4, 3),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ),
            Instrument(
                id=test_id_3,
                symbol="300750",
                name="宁德时代",
                market="SZ",
                status="active",
                listing_date=date(2018, 6, 11),
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
async def test_batch_returns_correct_count(db_session: AsyncSession) -> None:
    """测试 1：批量查询返回正确数量。

    传入 3 个已存在的 ID，验证返回 3 条记录，且 symbol 集合正确。
    """
    test_ids = [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
    ]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/instruments/batch", json={"ids": test_ids})

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["items"]) == 3
    symbols = {item["symbol"] for item in data["items"]}
    assert symbols == {"600519", "000001", "300750"}


@pytest.mark.asyncio
async def test_batch_empty_ids_returns_422(db_session: AsyncSession) -> None:
    """测试 2：空 ids 列表返回 422。

    InstrumentBatchRequest schema 强制 ids min_length=1，空列表会被 Pydantic 拒绝。
    验证后端输入校验生效，避免空 IN 查询的语义歧义。
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/instruments/batch", json={"ids": []})

    assert response.status_code == 422
    # Pydantic 校验错误应包含 ids 字段
    detail = response.json().get("detail", [])
    assert any("ids" in str(item.get("loc", [])) for item in detail), (
        f"422 错误应指向 ids 字段，实际 detail={detail}"
    )


@pytest.mark.asyncio
async def test_batch_nonexistent_ids_returns_empty(db_session: AsyncSession) -> None:
    """测试 3：不存在的 ID 列表返回空数组。

    传入 2 个随机 UUID（数据库中不存在），验证返回 total=0、items=[]。
    IN 查询对不存在的 ID 不报错，只是不返回记录。
    """
    nonexistent_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/instruments/batch", json={"ids": nonexistent_ids})

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["items"] == []


if __name__ == "__main__":
    # 自测入口：直接运行验证
    pytest.main([__file__, "-v", "--tb=short"])
