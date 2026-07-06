"""Worker auto-trigger 测试 - 验证 DSA 完成后自动触发盘后编排。

覆盖：
- DSA scheduled + completed → 调用 create_after_close_run
- 非 DSA strategy → 不调用 create_after_close_run
- DSA 但 trade_date=None → 不调用 create_after_close_run
- create_after_close_run 抛异常 → 不传播异常

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
注意：
- create_after_close_run 内部调用 acquire_job_run_lock + db.commit()
- 用 patch.object 替换 commit 为 flush 避免破坏 fixture nested 事务
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import StrategyRun
from app.worker import _maybe_trigger_after_close_orchestrator


async def _create_strategy_records(
    db_session,
    strategy_key: str,
    trade_date: date | None,
    run_type: str = "scheduled",
    status: str = "completed",
) -> StrategyRun:
    """创建 strategy_definition + version + run 记录（辅助函数）。"""
    definition = StrategyDefinition(
        strategy_key=strategy_key,
        kind="selector",
        display_name=f"测试策略-{strategy_key}",
    )
    db_session.add(definition)
    await db_session.flush()

    version = StrategyVersion(
        strategy_definition_id=definition.id,
        version="1.0.0",
        status="released",
        manifest={"outputs": []},
        build_hash=f"test_hash_{uuid.uuid4().hex[:16]}",
        released_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()

    run = StrategyRun(
        strategy_version_id=version.id,
        run_type=run_type,
        trade_date=trade_date,
        status=status,
        input_overrides={},
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        idempotency_key=f"test:{version.id}:{run_type}:{trade_date}:{uuid.uuid4().hex[:8]}",
    )
    db_session.add(run)
    await db_session.flush()

    return run


@pytest.mark.asyncio
async def test_auto_trigger_dsa_scheduled_completed(db_session) -> None:
    """DSA scheduled + completed → 调用 create_after_close_run 创建 after_close 任务。"""
    trade_date = date(2026, 7, 6)
    run = await _create_strategy_records(
        db_session, "dsa_selector", trade_date, run_type="scheduled", status="completed"
    )

    # patch commit 为 flush（create_after_close_run 内部会 commit）
    with patch.object(db_session, "commit", new=db_session.flush):
        await _maybe_trigger_after_close_orchestrator(db_session, run)

    # 验证 after_close 任务已创建
    stmt = select(SchedulerJobRun).where(
        SchedulerJobRun.job_name == "after_close_orchestrator",
        SchedulerJobRun.business_date == trade_date.isoformat(),
    )
    result = await db_session.execute(stmt)
    job_run = result.scalar_one_or_none()

    assert job_run is not None
    assert job_run.status == "queued"
    assert job_run.business_date == trade_date.isoformat()


@pytest.mark.asyncio
async def test_auto_trigger_non_dsa_strategy(db_session) -> None:
    """非 DSA strategy → 不调用 create_after_close_run。"""
    trade_date = date(2026, 7, 7)
    run = await _create_strategy_records(
        db_session,
        "watchlist_monitor",  # 非 dsa_selector
        trade_date,
        run_type="scheduled",
        status="completed",
    )

    with patch.object(db_session, "commit", new=db_session.flush):
        await _maybe_trigger_after_close_orchestrator(db_session, run)

    # 验证没有创建 after_close 任务
    stmt = select(SchedulerJobRun).where(
        SchedulerJobRun.job_name == "after_close_orchestrator",
        SchedulerJobRun.business_date == trade_date.isoformat(),
    )
    result = await db_session.execute(stmt)
    job_run = result.scalar_one_or_none()

    assert job_run is None


@pytest.mark.asyncio
async def test_auto_trigger_dsa_missing_trade_date(db_session) -> None:
    """DSA 但 trade_date=None → 不调用 create_after_close_run。"""
    run = await _create_strategy_records(
        db_session, "dsa_selector", None, run_type="scheduled", status="completed"
    )

    # 即使 trade_date=None，函数也不应抛异常
    with patch.object(db_session, "commit", new=db_session.flush):
        await _maybe_trigger_after_close_orchestrator(db_session, run)

    # 验证没有创建 after_close 任务
    stmt = select(SchedulerJobRun).where(
        SchedulerJobRun.job_name == "after_close_orchestrator",
    )
    result = await db_session.execute(stmt)
    job_runs = result.scalars().all()

    # 可能有其他测试创建的任务，但不应有 trade_date=None 的
    for jr in job_runs:
        assert jr.business_date is not None


@pytest.mark.asyncio
async def test_auto_trigger_create_failure_no_propagation(db_session) -> None:
    """create_after_close_run 抛异常 → 不传播异常（不影响 worker 主流程）。"""
    trade_date = date(2026, 7, 8)
    run = await _create_strategy_records(
        db_session, "dsa_selector", trade_date, run_type="scheduled", status="completed"
    )

    # mock create_after_close_run 抛异常（局部导入，patch 源模块）
    with patch(
        "app.services.after_close_orchestrator.create_after_close_run",
        new_callable=AsyncMock,
        side_effect=RuntimeError("模拟触发失败"),
    ):
        # 不应抛异常
        await _maybe_trigger_after_close_orchestrator(db_session, run)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
