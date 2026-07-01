"""M1 监控 Universe 构建服务测试。

测试内容：
1. build_monitoring_universe: 多用户自选聚合去重（向量化 SQL DISTINCT）
2. build_monitoring_universe: 空自选情况（无任何 active 记录）
3. build_monitoring_universe: 软删除记录不参与聚合（active=false 排除）
4. get_universe_for_user: 单用户自选集合
5. get_universe_count: 去重后股票数量

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库）
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
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.watchlist import UserWatchlistItem
from app.services.universe_service import (
    build_monitoring_universe,
    get_universe_count,
    get_universe_for_user,
)


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


@pytest.fixture
async def universe_setup(db_session: AsyncSession, user_factory, instrument_factory):
    """预置 2 个用户 + 3 只标的。"""
    user_a = await user_factory(email="user_a@example.com", timezone="Asia/Shanghai")
    user_b = await user_factory(email="user_b@example.com", timezone="Asia/Shanghai")
    inst1 = await instrument_factory(symbol="600519", name="贵州茅台", market="SH")
    inst2 = await instrument_factory(symbol="000001", name="平安银行", market="SZ")
    inst3 = await instrument_factory(symbol="000858", name="五粮液", market="SZ")
    return user_a, user_b, inst1, inst2, inst3


@pytest.mark.asyncio
async def test_build_universe_multi_user_dedupe(db_session: AsyncSession, universe_setup) -> None:
    """测试多用户自选聚合去重。

    场景：
    - user_a 自选: inst1, inst2
    - user_b 自选: inst2, inst3
    - 期望 universe: {inst1, inst2, inst3}（去重并集，inst2 只出现一次）
    """
    user_a, user_b, inst1, inst2, inst3 = universe_setup

    await _add_watchlist_item(db_session, user_a.id, inst1.id)
    await _add_watchlist_item(db_session, user_a.id, inst2.id)
    await _add_watchlist_item(db_session, user_b.id, inst2.id)  # 与 user_a 重叠
    await _add_watchlist_item(db_session, user_b.id, inst3.id)

    universe = await build_monitoring_universe(db_session)

    # 验证去重并集：3 只股票（inst2 不重复）
    assert universe == {inst1.id, inst2.id, inst3.id}
    assert len(universe) == 3


@pytest.mark.asyncio
async def test_build_universe_empty(db_session: AsyncSession, universe_setup) -> None:
    """测试空自选情况（无任何 active 记录）。"""
    universe = await build_monitoring_universe(db_session)
    assert universe == set()
    assert len(universe) == 0


@pytest.mark.asyncio
async def test_build_universe_excludes_soft_deleted(db_session: AsyncSession, universe_setup) -> None:
    """测试软删除记录不参与聚合（active=false 排除）。

    场景：
    - user_a 自选 inst1 (active=true)
    - user_a 自选 inst2 (active=false，已软删除)
    - 期望 universe: {inst1}（inst2 被排除）
    """
    user_a, _, inst1, inst2, _ = universe_setup

    await _add_watchlist_item(db_session, user_a.id, inst1.id, active=True)
    await _add_watchlist_item(db_session, user_a.id, inst2.id, active=False)  # 软删除

    universe = await build_monitoring_universe(db_session)
    assert universe == {inst1.id}
    assert inst2.id not in universe


@pytest.mark.asyncio
async def test_build_universe_all_soft_deleted(db_session: AsyncSession, universe_setup) -> None:
    """测试全部软删除的边界情况。"""
    user_a, _, inst1, inst2, _ = universe_setup

    await _add_watchlist_item(db_session, user_a.id, inst1.id, active=False)
    await _add_watchlist_item(db_session, user_a.id, inst2.id, active=False)

    universe = await build_monitoring_universe(db_session)
    assert universe == set()


@pytest.mark.asyncio
async def test_get_universe_for_user(db_session: AsyncSession, universe_setup) -> None:
    """测试单用户自选集合（仅返回该用户的 active 记录）。"""
    user_a, user_b, inst1, inst2, inst3 = universe_setup

    await _add_watchlist_item(db_session, user_a.id, inst1.id)
    await _add_watchlist_item(db_session, user_a.id, inst2.id)
    await _add_watchlist_item(db_session, user_b.id, inst3.id)  # user_b 独有

    universe_a = await get_universe_for_user(db_session, user_a.id)
    universe_b = await get_universe_for_user(db_session, user_b.id)

    assert universe_a == {inst1.id, inst2.id}
    assert universe_b == {inst3.id}
    # user_a 的 universe 不应包含 user_b 的独有股票
    assert inst3.id not in universe_a


@pytest.mark.asyncio
async def test_get_universe_for_user_empty(db_session: AsyncSession, universe_setup) -> None:
    """测试用户无自选的边界情况。"""
    user_a, _, _, _, _ = universe_setup
    universe = await get_universe_for_user(db_session, user_a.id)
    assert universe == set()


@pytest.mark.asyncio
async def test_get_universe_count(db_session: AsyncSession, universe_setup) -> None:
    """测试去重后股票数量。"""
    user_a, user_b, inst1, inst2, inst3 = universe_setup

    # user_a: inst1, inst2
    await _add_watchlist_item(db_session, user_a.id, inst1.id)
    await _add_watchlist_item(db_session, user_a.id, inst2.id)
    # user_b: inst2 (重叠), inst3
    await _add_watchlist_item(db_session, user_b.id, inst2.id)
    await _add_watchlist_item(db_session, user_b.id, inst3.id)

    count = await get_universe_count(db_session)
    assert count == 3  # 去重后 3 只


@pytest.mark.asyncio
async def test_get_universe_count_empty(db_session: AsyncSession, universe_setup) -> None:
    """测试空自选的数量。"""
    count = await get_universe_count(db_session)
    assert count == 0


@pytest.mark.asyncio
async def test_universe_is_set_type(db_session: AsyncSession, universe_setup) -> None:
    """测试返回类型为 set（供监控调度器消费）。"""
    user_a, _, inst1, _, _ = universe_setup

    await _add_watchlist_item(db_session, user_a.id, inst1.id)

    universe = await build_monitoring_universe(db_session)
    assert isinstance(universe, set)
    # 验证元素类型为 UUID
    for item in universe:
        assert isinstance(item, uuid.UUID)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
