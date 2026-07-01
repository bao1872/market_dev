"""W1 用户自选股 API 测试。

测试内容：
1. GET /watchlist: 查询当前用户自选列表
2. POST /watchlist: 加入自选（user_id 由上下文注入，不接受 body 传入）
3. DELETE /watchlist/{instrument_id}: 移除自选（软删除）
4. 重复加入返回 409 Conflict
5. 移除后重新加入（恢复 active=true）
6. user_id 注入安全约束：body 中传 user_id 应被忽略

测试策略：
- 使用 sqlite 内存数据库 + 异步 SQLAlchemy
- 通过 dependency_overrides 注入测试会话与认证用户
- 覆盖主逻辑 + 边界条件
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.deps import get_current_active_user
from app.db import get_db
from app.main import app
from app.models.instrument import Instrument
from app.models.user import User

# SQLite 兼容的建表 DDL（绕过 PostgreSQL 特有的 server_default）
_SQLITE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT NOT NULL PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS instruments (
        id TEXT NOT NULL PRIMARY KEY,
        symbol TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        pinyin_initials TEXT,
        market TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        listing_date DATE,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subscriptions (
        id TEXT NOT NULL PRIMARY KEY,
        user_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'active',
        starts_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at DATETIME NOT NULL,
        plan_code TEXT NOT NULL,
        entitlement_snapshot TEXT,
        source TEXT NOT NULL DEFAULT 'invite',
        created_by TEXT,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_watchlist_items (
        id TEXT NOT NULL PRIMARY KEY,
        user_id TEXT NOT NULL,
        instrument_id TEXT NOT NULL,
        source TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        removed_at DATETIME,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (instrument_id) REFERENCES instruments(id),
        UNIQUE (user_id, instrument_id)
    )
    """,
]


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """创建内存 SQLite 异步会话，测试后销毁。"""
    try:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    except Exception:
        pytest.skip("aiosqlite 不可用，跳过 DB 测试")

    async with engine.begin() as conn:
        for ddl in _SQLITE_DDL:
            await conn.execute(text(ddl))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        # 创建测试用户
        test_user = User(
            id=uuid.uuid4(),
            email="watcher@example.com",
            password_hash="fake-hash",
            status="active",
            timezone="Asia/Shanghai",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(test_user)

        # 创建测试股票
        inst1 = Instrument(
            id=uuid.uuid4(), symbol="600519", name="贵州茅台",
            market="SH", status="active",
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        )
        inst2 = Instrument(
            id=uuid.uuid4(), symbol="000001", name="平安银行",
            market="SZ", status="active",
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        )
        session.add(inst1)
        session.add(inst2)
        await session.flush()

        # 创建订阅记录（observe_20 套餐，entitlement_snapshot.monitor_limit=20，满足 POST /watchlist 额度校验）
        from app.models.subscription import Subscription

        subscription = Subscription(
            id=uuid.uuid4(),
            user_id=test_user.id,
            status="active",
            starts_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=30),
            plan_code="observe_20",
            entitlement_snapshot={"monitor_limit": 20},
            source="invite",
        )
        session.add(subscription)
        await session.commit()

        # 注入测试会话
        async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
            yield session

        # 注入认证用户（绕过 JWT，直接返回 test_user）
        async def get_test_user() -> User:
            return test_user

        app.dependency_overrides[get_db] = get_test_db
        app.dependency_overrides[get_current_active_user] = get_test_user

        # 暴露测试数据 ID 供测试使用
        session._test_user_id = test_user.id  # type: ignore[attr-defined]
        session._test_inst1_id = inst1.id  # type: ignore[attr-defined]
        session._test_inst2_id = inst2.id  # type: ignore[attr-defined]

        yield session

        app.dependency_overrides.clear()

    await engine.dispose()


@pytest.mark.asyncio
async def test_add_to_watchlist(db_session: AsyncSession) -> None:
    """测试加入自选。"""
    inst_id = db_session._test_inst1_id  # type: ignore[attr-defined]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/watchlist", json={"instrument_id": str(inst_id)})
    assert response.status_code == 201
    data = response.json()
    assert data["instrument_id"] == str(inst_id)
    assert data["active"] is True
    assert data["source"] == "manual"


@pytest.mark.asyncio
async def test_list_watchlist(db_session: AsyncSession) -> None:
    """测试查询自选列表。"""
    inst1 = db_session._test_inst1_id  # type: ignore[attr-defined]
    inst2 = db_session._test_inst2_id  # type: ignore[attr-defined]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/watchlist", json={"instrument_id": str(inst1)})
        await client.post("/watchlist", json={"instrument_id": str(inst2), "source": "monitor"})
        response = await client.get("/watchlist")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    symbols_added = {item["instrument_id"] for item in data["items"]}
    assert str(inst1) in symbols_added
    assert str(inst2) in symbols_added


@pytest.mark.asyncio
async def test_remove_from_watchlist(db_session: AsyncSession) -> None:
    """测试移除自选（软删除）。"""
    inst_id = db_session._test_inst1_id  # type: ignore[attr-defined]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/watchlist", json={"instrument_id": str(inst_id)})
        # 移除
        response = await client.delete(f"/watchlist/{inst_id}")
        assert response.status_code == 204
        # 查询列表应为空
        list_resp = await client.get("/watchlist")
    assert list_resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_remove_not_found(db_session: AsyncSession) -> None:
    """测试移除不存在的自选（404）。"""
    fake_id = uuid.uuid4()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(f"/watchlist/{fake_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_add_conflict(db_session: AsyncSession) -> None:
    """测试重复加入返回 409 Conflict。"""
    inst_id = db_session._test_inst1_id  # type: ignore[attr-defined]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp1 = await client.post("/watchlist", json={"instrument_id": str(inst_id)})
        assert resp1.status_code == 201
        resp2 = await client.post("/watchlist", json={"instrument_id": str(inst_id)})
    assert resp2.status_code == 409


@pytest.mark.asyncio
async def test_readd_after_remove(db_session: AsyncSession) -> None:
    """测试移除后重新加入（恢复 active=true）。"""
    inst_id = db_session._test_inst1_id  # type: ignore[attr-defined]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/watchlist", json={"instrument_id": str(inst_id)})
        await client.delete(f"/watchlist/{inst_id}")
        # 重新加入
        response = await client.post("/watchlist", json={"instrument_id": str(inst_id)})
    assert response.status_code == 201
    assert response.json()["active"] is True


@pytest.mark.asyncio
async def test_add_nonexistent_instrument(db_session: AsyncSession) -> None:
    """测试加入不存在的股票（404）。"""
    fake_id = uuid.uuid4()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/watchlist", json={"instrument_id": str(fake_id)})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_user_id_injection_ignored(db_session: AsyncSession) -> None:
    """测试 user_id 由上下文注入：body 中传 user_id 应被忽略（安全约束）。

    WatchlistAddRequest 不含 user_id 字段，Pydantic 默认忽略额外字段。
    即使 body 传入 user_id，也不会影响实际写入的 user_id（由认证上下文决定）。
    """
    inst_id = db_session._test_inst1_id  # type: ignore[attr-defined]
    test_user_id = db_session._test_user_id  # type: ignore[attr-defined]
    fake_user_id = uuid.uuid4()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # body 中传伪造的 user_id（应被忽略）
        response = await client.post("/watchlist", json={
            "instrument_id": str(inst_id),
            "user_id": str(fake_user_id),  # 应被忽略
        })
    assert response.status_code == 201
    data = response.json()
    # 实际写入的 user_id 应为认证上下文的用户，而非 body 中的伪造值
    assert data["user_id"] == str(test_user_id)
    assert data["user_id"] != str(fake_user_id)


@pytest.mark.asyncio
async def test_empty_watchlist(db_session: AsyncSession) -> None:
    """测试空自选列表。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/watchlist")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["items"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
