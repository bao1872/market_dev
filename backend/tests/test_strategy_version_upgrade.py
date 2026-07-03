"""策略版本升级测试（advice.md 第五节 / Task 5.4）。

测试覆盖：
- seed_strategies 在空库上创建 watchlist_monitor 1.1.0 / dsa_selector 1.4.0 released 版本
- manifest outputs 包含新增字段（previous_close, change_pct）
- 旧版本被人工创建后，再次 seed_strategies 不会修改旧版本的 released_at
- 升级后同一策略存在多个 released 版本，seed_strategies 不抛 MultipleResultsFound

约束：
- 使用测试库（db_session fixture）
- patch db.commit 为 db.flush，避免破坏 nested 事务（与 test_main_lifespan.py 一致）
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.strategy import StrategyDefinition, StrategyVersion
from app.services.strategy_seed import seed_strategies


@pytest.mark.asyncio
async def test_seed_creates_new_released_versions(db_session):
    """seed_strategies 在空库上创建 1.1.0/1.4.1 released 版本。"""
    # patch commit 为 flush，避免破坏 db_session 的 nested 事务
    with patch.object(db_session, "commit", new=db_session.flush):
        results = await seed_strategies(db_session, release=True)

    result_map = {(k, v): s for k, v, s in results}
    assert ("watchlist_monitor", "1.1.0") in result_map, (
        "watchlist_monitor v1.1.0 必须被 seed_strategies 创建"
    )
    assert ("dsa_selector", "1.4.1") in result_map, (
        "dsa_selector v1.4.1 必须被 seed_strategies 创建"
    )
    assert result_map[("watchlist_monitor", "1.1.0")] == "released"
    assert result_map[("dsa_selector", "1.4.1")] == "released"


@pytest.mark.asyncio
async def test_watchlist_monitor_manifest_contains_new_fields(db_session):
    """watchlist_monitor v1.1.0 manifest outputs 必须包含 previous_close 和 change_pct。"""
    with patch.object(db_session, "commit", new=db_session.flush):
        await seed_strategies(db_session, release=True)

    stmt = (
        select(StrategyVersion)
        .join(StrategyDefinition, StrategyDefinition.id == StrategyVersion.strategy_definition_id)
        .where(
            StrategyDefinition.strategy_key == "watchlist_monitor",
            StrategyVersion.version == "1.1.0",
        )
    )
    result = await db_session.execute(stmt)
    version = result.scalar_one_or_none()
    assert version is not None, "watchlist_monitor v1.1.0 必须存在"
    assert version.status == "released"

    manifest = version.manifest
    output_keys = [o["key"] for o in manifest.get("outputs", [])]
    assert "previous_close" in output_keys, (
        "watchlist_monitor v1.1.0 manifest.outputs 必须包含 previous_close 字段"
    )
    assert "change_pct" in output_keys, (
        "watchlist_monitor v1.1.0 manifest.outputs 必须包含 change_pct 字段"
    )


@pytest.mark.asyncio
async def test_dsa_selector_version_is_1_4_1(db_session):
    """dsa_selector 最新 released 版本号必须是 1.4.1。"""
    with patch.object(db_session, "commit", new=db_session.flush):
        await seed_strategies(db_session, release=True)

    stmt = (
        select(StrategyVersion)
        .join(StrategyDefinition, StrategyDefinition.id == StrategyVersion.strategy_definition_id)
        .where(
            StrategyDefinition.strategy_key == "dsa_selector",
            StrategyVersion.status == "released",
        )
        .order_by(StrategyVersion.released_at.desc())
        .limit(1)
    )
    result = await db_session.execute(stmt)
    latest_version = result.scalar_one_or_none()
    assert latest_version is not None, "dsa_selector 必须有 released 版本"
    assert latest_version.version == "1.4.1", (
        f"dsa_selector 最新 released 版本应为 1.4.1，实际为 {latest_version.version}"
    )


@pytest.mark.asyncio
async def test_seed_idempotent_does_not_raise_multiple_results(db_session):
    """seed_strategies 二次调用幂等：不抛 MultipleResultsFound。

    [策略种子] - 描述: 旧实现 strategy_seed.py:165 用 scalar_one_or_none()
    在多个 released 版本时会抛 MultipleResultsFound；修复后用 scalars().first()。
    """
    with patch.object(db_session, "commit", new=db_session.flush):
        await seed_strategies(db_session, release=True)
        # 二次调用不应抛异常
        await seed_strategies(db_session, release=True)


@pytest.mark.asyncio
async def test_seed_does_not_modify_old_released_version(db_session):
    """seed_strategies 不会修改已存在的 released 版本的 released_at。

    场景：人工创建一个旧 released 版本（1.0.0），运行 seed_strategies，
    验证旧版本的 released_at 不变。
    """
    from app.constants.strategy_keys import WATCHLIST_MONITOR

    # 1. 第一次 seed，创建 1.1.0
    with patch.object(db_session, "commit", new=db_session.flush):
        await seed_strategies(db_session, release=True)

    # 2. 查 watchlist_monitor 定义
    def_stmt = select(StrategyDefinition).where(
        StrategyDefinition.strategy_key == WATCHLIST_MONITOR
    )
    definition = (await db_session.execute(def_stmt)).scalar_one()

    # 3. 人工创建一个旧版本 1.0.0（released，时间戳固定）
    old_released_at = datetime(2026, 1, 1, tzinfo=UTC)
    old_version = StrategyVersion(
        strategy_definition_id=definition.id,
        version="1.0.0",
        status="released",
        manifest={
            "strategy_id": "watchlist_monitor",
            "version": "1.0.0",
            "outputs": [{"key": "current_price", "type": "number"}],
        },
        build_hash="old_test_hash",
        released_at=old_released_at,
    )
    db_session.add(old_version)
    await db_session.flush()

    # 4. 再次 seed_strategies，不应修改 1.0.0 的 released_at
    with patch.object(db_session, "commit", new=db_session.flush):
        await seed_strategies(db_session, release=True)

    # 5. 验证 1.0.0 的 released_at 未变
    stmt = (
        select(StrategyVersion)
        .where(
            StrategyVersion.strategy_definition_id == definition.id,
            StrategyVersion.version == "1.0.0",
        )
    )
    result = await db_session.execute(stmt)
    retrieved_old = result.scalar_one()
    assert retrieved_old.released_at == old_released_at, (
        f"旧版本 1.0.0 的 released_at 不应被修改："
        f"期望 {old_released_at}，实际 {retrieved_old.released_at}"
    )

    # 6. 同时验证 1.1.0 仍然存在
    stmt_new = (
        select(StrategyVersion)
        .where(
            StrategyVersion.strategy_definition_id == definition.id,
            StrategyVersion.version == "1.1.0",
        )
    )
    new_version = (await db_session.execute(stmt_new)).scalar_one_or_none()
    assert new_version is not None, "1.1.0 版本必须仍然存在"
    assert new_version.status == "released"
