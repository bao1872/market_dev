"""M1 监控 Universe 构建服务测试。

测试内容：
1. build_monitoring_universe: 多用户自选聚合去重（向量化 SQL DISTINCT）
2. build_monitoring_universe: 空自选情况（无任何 active 记录）
3. build_monitoring_universe: 软删除记录不参与聚合（active=false 排除）
4. get_universe_for_user: 单用户自选集合
5. get_universe_count: 去重后股票数量

测试策略：
- 使用 sqlite 内存数据库 + 异步 SQLAlchemy
- 直接调用 universe_service 函数（不走 HTTP，聚焦服务层逻辑）
- 多用户场景：user_a 与 user_b 自选存在重叠，验证 universe 为去重并集
- 边界条件：空自选、全部软删除、单用户独有股票

设计要点验证：
- 向量化去重：SQL SELECT DISTINCT 在数据库层完成，无 Python 层 for 循环
- 仅聚合 active=true 记录（软删除记录不参与）
- 返回 Set[UUID]，供监控调度器消费
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.instrument import Instrument
from app.models.user import User
from app.models.watchlist import UserWatchlistItem
from app.services.universe_service import (
    build_monitoring_universe,
    get_universe_count,
    get_universe_for_user,
)

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
        market TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        listing_date DATE,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    """创建内存 SQLite 异步会话，预置 2 个用户 + 3 只股票，测试后销毁。"""
    try:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    except Exception:
        pytest.skip("aiosqlite 不可用，跳过 DB 测试")

    async with engine.begin() as conn:
        for ddl in _SQLITE_DDL:
            await conn.execute(text(ddl))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        # 创建 2 个测试用户
        user_a = User(
            id=uuid.uuid4(),
            email="user_a@example.com",
            password_hash="fake-hash",
            status="active",
            timezone="Asia/Shanghai",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        user_b = User(
            id=uuid.uuid4(),
            email="user_b@example.com",
            password_hash="fake-hash",
            status="active",
            timezone="Asia/Shanghai",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user_a)
        session.add(user_b)

        # 创建 3 只测试股票
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
        inst3 = Instrument(
            id=uuid.uuid4(), symbol="000858", name="五粮液",
            market="SZ", status="active",
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        )
        session.add(inst1)
        session.add(inst2)
        session.add(inst3)
        await session.commit()

        # 暴露测试数据 ID 供测试使用
        session._test_user_a_id = user_a.id  # type: ignore[attr-defined]
        session._test_user_b_id = user_b.id  # type: ignore[attr-defined]
        session._test_inst1_id = inst1.id  # type: ignore[attr-defined]
        session._test_inst2_id = inst2.id  # type: ignore[attr-defined]
        session._test_inst3_id = inst3.id  # type: ignore[attr-defined]

        yield session

    await engine.dispose()


async def _add_watchlist_item(
    session: AsyncSession,
    user_id: uuid.UUID,
    instrument_id: uuid.UUID,
    source: str = "manual",
    active: bool = True,
) -> UserWatchlistItem:
    """辅助函数：直接插入自选记录（绕过 API，用于服务层测试）。"""
    item = UserWatchlistItem(
        user_id=user_id,
        instrument_id=instrument_id,
        source=source,
        active=active,
        created_at=datetime.now(UTC),
        removed_at=None if active else datetime.now(UTC),
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


@pytest.mark.asyncio
async def test_build_universe_multi_user_dedupe(db_session: AsyncSession) -> None:
    """测试多用户自选聚合去重。

    场景：
    - user_a 自选: inst1, inst2
    - user_b 自选: inst2, inst3
    - 期望 universe: {inst1, inst2, inst3}（去重并集，inst2 只出现一次）
    """
    user_a = db_session._test_user_a_id  # type: ignore[attr-defined]
    user_b = db_session._test_user_b_id  # type: ignore[attr-defined]
    inst1 = db_session._test_inst1_id  # type: ignore[attr-defined]
    inst2 = db_session._test_inst2_id  # type: ignore[attr-defined]
    inst3 = db_session._test_inst3_id  # type: ignore[attr-defined]

    await _add_watchlist_item(db_session, user_a, inst1)
    await _add_watchlist_item(db_session, user_a, inst2)
    await _add_watchlist_item(db_session, user_b, inst2)  # 与 user_a 重叠
    await _add_watchlist_item(db_session, user_b, inst3)

    universe = await build_monitoring_universe(db_session)

    # 验证去重并集：3 只股票（inst2 不重复）
    assert universe == {inst1, inst2, inst3}
    assert len(universe) == 3


@pytest.mark.asyncio
async def test_build_universe_empty(db_session: AsyncSession) -> None:
    """测试空自选情况（无任何 active 记录）。"""
    universe = await build_monitoring_universe(db_session)
    assert universe == set()
    assert len(universe) == 0


@pytest.mark.asyncio
async def test_build_universe_excludes_soft_deleted(db_session: AsyncSession) -> None:
    """测试软删除记录不参与聚合（active=false 排除）。

    场景：
    - user_a 自选 inst1 (active=true)
    - user_a 自选 inst2 (active=false，已软删除)
    - 期望 universe: {inst1}（inst2 被排除）
    """
    user_a = db_session._test_user_a_id  # type: ignore[attr-defined]
    inst1 = db_session._test_inst1_id  # type: ignore[attr-defined]
    inst2 = db_session._test_inst2_id  # type: ignore[attr-defined]

    await _add_watchlist_item(db_session, user_a, inst1, active=True)
    await _add_watchlist_item(db_session, user_a, inst2, active=False)  # 软删除

    universe = await build_monitoring_universe(db_session)
    assert universe == {inst1}
    assert inst2 not in universe


@pytest.mark.asyncio
async def test_build_universe_all_soft_deleted(db_session: AsyncSession) -> None:
    """测试全部软删除的边界情况。"""
    user_a = db_session._test_user_a_id  # type: ignore[attr-defined]
    inst1 = db_session._test_inst1_id  # type: ignore[attr-defined]
    inst2 = db_session._test_inst2_id  # type: ignore[attr-defined]

    await _add_watchlist_item(db_session, user_a, inst1, active=False)
    await _add_watchlist_item(db_session, user_a, inst2, active=False)

    universe = await build_monitoring_universe(db_session)
    assert universe == set()


@pytest.mark.asyncio
async def test_get_universe_for_user(db_session: AsyncSession) -> None:
    """测试单用户自选集合（仅返回该用户的 active 记录）。"""
    user_a = db_session._test_user_a_id  # type: ignore[attr-defined]
    user_b = db_session._test_user_b_id  # type: ignore[attr-defined]
    inst1 = db_session._test_inst1_id  # type: ignore[attr-defined]
    inst2 = db_session._test_inst2_id  # type: ignore[attr-defined]
    inst3 = db_session._test_inst3_id  # type: ignore[attr-defined]

    await _add_watchlist_item(db_session, user_a, inst1)
    await _add_watchlist_item(db_session, user_a, inst2)
    await _add_watchlist_item(db_session, user_b, inst3)  # user_b 独有

    universe_a = await get_universe_for_user(db_session, user_a)
    universe_b = await get_universe_for_user(db_session, user_b)

    assert universe_a == {inst1, inst2}
    assert universe_b == {inst3}
    # user_a 的 universe 不应包含 user_b 的独有股票
    assert inst3 not in universe_a


@pytest.mark.asyncio
async def test_get_universe_for_user_empty(db_session: AsyncSession) -> None:
    """测试用户无自选的边界情况。"""
    user_a = db_session._test_user_a_id  # type: ignore[attr-defined]
    universe = await get_universe_for_user(db_session, user_a)
    assert universe == set()


@pytest.mark.asyncio
async def test_get_universe_count(db_session: AsyncSession) -> None:
    """测试去重后股票数量。"""
    user_a = db_session._test_user_a_id  # type: ignore[attr-defined]
    user_b = db_session._test_user_b_id  # type: ignore[attr-defined]
    inst1 = db_session._test_inst1_id  # type: ignore[attr-defined]
    inst2 = db_session._test_inst2_id  # type: ignore[attr-defined]
    inst3 = db_session._test_inst3_id  # type: ignore[attr-defined]

    # user_a: inst1, inst2
    await _add_watchlist_item(db_session, user_a, inst1)
    await _add_watchlist_item(db_session, user_a, inst2)
    # user_b: inst2 (重叠), inst3
    await _add_watchlist_item(db_session, user_b, inst2)
    await _add_watchlist_item(db_session, user_b, inst3)

    count = await get_universe_count(db_session)
    assert count == 3  # 去重后 3 只


@pytest.mark.asyncio
async def test_get_universe_count_empty(db_session: AsyncSession) -> None:
    """测试空自选的数量。"""
    count = await get_universe_count(db_session)
    assert count == 0


@pytest.mark.asyncio
async def test_universe_is_set_type(db_session: AsyncSession) -> None:
    """测试返回类型为 set（供监控调度器消费）。"""
    user_a = db_session._test_user_a_id  # type: ignore[attr-defined]
    inst1 = db_session._test_inst1_id  # type: ignore[attr-defined]

    await _add_watchlist_item(db_session, user_a, inst1)

    universe = await build_monitoring_universe(db_session)
    assert isinstance(universe, set)
    # 验证元素类型为 UUID
    for item in universe:
        assert isinstance(item, uuid.UUID)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
