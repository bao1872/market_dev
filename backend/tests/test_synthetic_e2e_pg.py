"""PostgreSQL synthetic E2E（P0-04/P0-06/P0-07）— 仅编写/可收集，**不在本轮执行**。

[CHANGE-20260805-CP4A-CP3 / Step 7]
本文件覆盖 PG 事务语义，需在隔离验证库（bz_stock_verify_<sha>）执行 Migration 087 +
Seed 后运行：
    snapshot persistence
    → 按 source_core_run_id 分页 decode artifact
    → persist_precomputed_dsa_results（StrategyResult/Run/Item）
    → stock_core 原子 publication（同事务 quality/fencing/pointer/run/supersede/audit）
    → 断言 pointer/run 状态原子一致、失败全回滚旧 pointer 保留

由于当前未授权执行 Migration / 未建验证库，本文件默认 **skip**（仅收集、静态审查）。
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

# 未授权 PG 时跳过；收集通过即可
pytestmark = pytest.mark.skip(
    reason="PG synthetic E2E 需隔离验证库 + Migration 087 授权后执行"
)


@pytest.mark.asyncio
async def test_pg_snapshot_to_atomic_publication(session) -> None:  # pragma: no cover
    """占位：daily bars → context → artifact → snapshot → 原子 stock_core 发布。"""
    from app.services.stock_core_publication_service import publish_stock_core_atomically

    dsa_run_id = uuid.uuid4()
    # 1. 从 source_core_run_id 分页 decode artifact（CoreArtifactRepository）
    # 2. persist DSA projection（persist_precomputed_dsa_results）
    # 3. 原子发布
    await publish_stock_core_atomically(
        session,
        scope_key="market",
        trade_date=date(2026, 8, 6),
        publication_kind="stock_core",
        algorithm_version="dsa-v3",
        snapshot_run_id=uuid.uuid4(),
        coverage_ratio=1.0,
        worker_id="w1",
        lease_epoch=1,
        eligible_count=10,
    )
    assert dsa_run_id is not None


@pytest.mark.asyncio
async def test_pg_atomic_publication_rollback(session) -> None:  # pragma: no cover
    """占位：失败时旧 pointer 保留、run 状态不漂移。"""
    assert True
