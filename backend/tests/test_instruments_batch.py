"""POST /instruments/batch 批量查询股票 API 测试。

测试内容：
1. 批量查询返回正确数量（传入已存在的 ID 列表）
2. 空 ids 列表返回 422（InstrumentBatchRequest schema min_length=1 校验拦截）
3. 不存在的 ID 列表返回空数组（total=0，items=[]）

测试策略：
- 使用 conftest 的 db_session / client fixtures（PostgreSQL 测试库）
- 每个测试独立事务，测试后回滚
- Mock 数据通过 instrument_factory 注入固定 UUID

注意：
- InstrumentBatchRequest schema 强制 ids: list[UUID] min_length=1，空列表会被 Pydantic 拒绝（422）
- 不存在的 ID 不会触发 404，只是不出现在返回结果中（IN 查询的天然行为）
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def batch_instruments_fixture(db_session: AsyncSession, instrument_factory):
    """预置 3 只固定 UUID 的测试标的。"""
    test_id_1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
    test_id_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
    test_id_3 = uuid.UUID("00000000-0000-0000-0000-000000000003")
    instruments = [
        await instrument_factory(
            id=test_id_1, symbol="600519", name="贵州茅台", market="SH", status="active",
            listing_date=date(2001, 8, 27),
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        ),
        await instrument_factory(
            id=test_id_2, symbol="000001", name="平安银行", market="SZ", status="active",
            listing_date=date(1991, 4, 3),
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        ),
        await instrument_factory(
            id=test_id_3, symbol="300750", name="宁德时代", market="SZ", status="active",
            listing_date=date(2018, 6, 11),
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        ),
    ]
    return instruments


@pytest.mark.asyncio
async def test_batch_returns_correct_count(client: AsyncClient, batch_instruments_fixture) -> None:
    """测试 1：批量查询返回正确数量。

    传入 3 个已存在的 ID，验证返回 3 条记录，且 symbol 集合正确。
    """
    test_ids = [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
    ]
    response = await client.post("/instruments/batch", json={"ids": test_ids})

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["items"]) == 3
    symbols = {item["symbol"] for item in data["items"]}
    assert symbols == {"600519", "000001", "300750"}


@pytest.mark.asyncio
async def test_batch_empty_ids_returns_422(client: AsyncClient) -> None:
    """测试 2：空 ids 列表返回 422。

    InstrumentBatchRequest schema 强制 ids min_length=1，空列表会被 Pydantic 拒绝。
    验证后端输入校验生效，避免空 IN 查询的语义歧义。
    """
    response = await client.post("/instruments/batch", json={"ids": []})

    assert response.status_code == 422
    # Pydantic 校验错误应包含 ids 字段
    detail = response.json().get("detail", [])
    assert any("ids" in str(item.get("loc", [])) for item in detail), (
        f"422 错误应指向 ids 字段，实际 detail={detail}"
    )


@pytest.mark.asyncio
async def test_batch_nonexistent_ids_returns_empty(client: AsyncClient) -> None:
    """测试 3：不存在的 ID 列表返回空数组。

    传入 2 个随机 UUID（数据库中不存在），验证返回 total=0、items=[]。
    IN 查询对不存在的 ID 不报错，只是不返回记录。
    """
    nonexistent_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    response = await client.post("/instruments/batch", json={"ids": nonexistent_ids})

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["items"] == []


if __name__ == "__main__":
    # 自测入口：直接运行验证
    pytest.main([__file__, "-v", "--tb=short"])
