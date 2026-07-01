"""W1 用户自选股 API 测试。

测试内容：
1. GET /watchlist: 查询当前用户自选列表
2. POST /watchlist: 加入自选（user_id 由上下文注入，不接受 body 传入）
3. DELETE /watchlist/{instrument_id}: 移除自选（软删除）
4. 重复加入返回 409 Conflict
5. 移除后重新加入（恢复 active=true）
6. user_id 注入安全约束：body 中传 user_id 应被忽略

测试策略：
- 使用 conftest 的 db_session / client fixtures（PostgreSQL 测试库）
- 通过 dependency_overrides 注入认证用户
- 覆盖主逻辑 + 边界条件
"""

from __future__ import annotations

import uuid
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


@pytest_asyncio.fixture
async def watchlist_client(
    client: AsyncClient,
    db_session: AsyncSession,
    user_factory,
    instrument_factory,
    subscription_factory,
) -> AsyncGenerator[tuple[AsyncClient, User, Instrument, Instrument], None]:
    """提供已认证 HTTP 客户端 + 测试用户/标的/订阅。"""
    user = await user_factory(
        email="watcher@example.com",
        password_hash="fake-hash",
        timezone="Asia/Shanghai",
    )
    inst1 = await instrument_factory(
        symbol="600519", name="贵州茅台", market="SH", status="active",
    )
    inst2 = await instrument_factory(
        symbol="000001", name="平安银行", market="SZ", status="active",
    )
    await subscription_factory(
        user_id=user.id,
        plan_code="observe_20",
        status="active",
        starts_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=30),
        source="invite",
    )

    async def get_test_user() -> User:
        return user

    app.dependency_overrides[get_current_active_user] = get_test_user

    yield client, user, inst1, inst2

    app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.asyncio
async def test_add_to_watchlist(watchlist_client) -> None:
    """测试加入自选。"""
    client, _, inst1, _ = watchlist_client
    response = await client.post("/watchlist", json={"instrument_id": str(inst1.id)})
    assert response.status_code == 201
    data = response.json()
    assert data["instrument_id"] == str(inst1.id)
    assert data["active"] is True
    assert data["source"] == "manual"


@pytest.mark.asyncio
async def test_list_watchlist(watchlist_client) -> None:
    """测试查询自选列表。"""
    client, _, inst1, inst2 = watchlist_client
    await client.post("/watchlist", json={"instrument_id": str(inst1.id)})
    await client.post("/watchlist", json={"instrument_id": str(inst2.id), "source": "monitor"})
    response = await client.get("/watchlist")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    symbols_added = {item["instrument_id"] for item in data["items"]}
    assert str(inst1.id) in symbols_added
    assert str(inst2.id) in symbols_added


@pytest.mark.asyncio
async def test_remove_from_watchlist(watchlist_client) -> None:
    """测试移除自选（软删除）。"""
    client, _, inst1, _ = watchlist_client
    await client.post("/watchlist", json={"instrument_id": str(inst1.id)})
    # 移除
    response = await client.delete(f"/watchlist/{inst1.id}")
    assert response.status_code == 204
    # 查询列表应为空
    list_resp = await client.get("/watchlist")
    assert list_resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_remove_not_found(watchlist_client) -> None:
    """测试移除不存在的自选（404）。"""
    client, _, _, _ = watchlist_client
    fake_id = uuid.uuid4()
    response = await client.delete(f"/watchlist/{fake_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_add_conflict(watchlist_client) -> None:
    """测试重复加入返回 409 Conflict。"""
    client, _, inst1, _ = watchlist_client
    resp1 = await client.post("/watchlist", json={"instrument_id": str(inst1.id)})
    assert resp1.status_code == 201
    resp2 = await client.post("/watchlist", json={"instrument_id": str(inst1.id)})
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_readd_after_remove(watchlist_client) -> None:
    """测试移除后重新加入（恢复 active=true）。"""
    client, _, inst1, _ = watchlist_client
    await client.post("/watchlist", json={"instrument_id": str(inst1.id)})
    await client.delete(f"/watchlist/{inst1.id}")
    # 重新加入
    response = await client.post("/watchlist", json={"instrument_id": str(inst1.id)})
    assert response.status_code == 201
    assert response.json()["active"] is True


@pytest.mark.asyncio
async def test_add_nonexistent_instrument(watchlist_client) -> None:
    """测试加入不存在的股票（404）。"""
    client, _, _, _ = watchlist_client
    fake_id = uuid.uuid4()
    response = await client.post("/watchlist", json={"instrument_id": str(fake_id)})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_user_id_injection_ignored(watchlist_client) -> None:
    """测试 user_id 由上下文注入：body 中传 user_id 应被忽略（安全约束）。

    WatchlistAddRequest 不含 user_id 字段，Pydantic 默认忽略额外字段。
    即使 body 传入 user_id，也不会影响实际写入的 user_id（由认证上下文决定）。
    """
    client, user, inst1, _ = watchlist_client
    fake_user_id = uuid.uuid4()
    # body 中传伪造的 user_id（应被忽略）
    response = await client.post("/watchlist", json={
        "instrument_id": str(inst1.id),
        "user_id": str(fake_user_id),  # 应被忽略
    })
    assert response.status_code == 201
    data = response.json()
    # 实际写入的 user_id 应为认证上下文的用户，而非 body 中的伪造值
    assert data["user_id"] == str(user.id)
    assert data["user_id"] != str(fake_user_id)


@pytest.mark.asyncio
async def test_empty_watchlist(watchlist_client) -> None:
    """测试空自选列表。"""
    client, _, _, _ = watchlist_client
    response = await client.get("/watchlist")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["items"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
