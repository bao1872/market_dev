"""main.py lifespan 启动恢复测试 - 验证 API Backend 启动时调用恢复函数。

覆盖 2 个场景（spec Phase 4 Task 4.6）：
1. lifespan 启动时调用 recover_stale_scheduler_job_runs 清理过期任务
2. 恢复函数抛异常时 lifespan 不阻塞启动（yield 正常执行）

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
设计要点：
- lifespan 内部用 AsyncSessionLocal() 创建独立 session，测试中 patch 为 test db_session
- patch check_strategy_assets / seed_strategies / seed_calendar_from_pytdx 为 no-op
- 用 patch.object 把 db.commit 替换为 flush，避免破坏 fixture 的 nested 事务
- 场景 1 用真实恢复函数验证 stale job 被恢复（端到端行为验证）
- 场景 2 patch 源模块函数抛异常，验证 yield 仍被到达
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models.scheduler_job_run import SchedulerJobRun

_TZ = ZoneInfo("Asia/Shanghai")


class _FakeSessionCtx:
    """模拟 AsyncSession 的异步上下文管理器，直接返回 test db_session。"""

    def __init__(self, session) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


class _FakeSessionLocal:
    """模拟 async_sessionmaker，__call__ 返回 _FakeSessionCtx。"""

    def __init__(self, session) -> None:
        self._session = session

    def __call__(self):
        return _FakeSessionCtx(self._session)


def _no_op_check_strategy_assets() -> None:
    """测试用 no-op 策略资产检查。"""
    return


async def _no_op_seed_strategies(db, release=False):
    """测试用 no-op 策略种子。"""
    return []


async def _no_op_seed_calendar(db, year=None):
    """测试用 no-op 日历种子。"""
    return 0


async def _create_stale_job(db_session, *, job_name: str = "test_lifespan_job") -> SchedulerJobRun:
    """创建一个 lease 过期的 running 任务（heartbeat 也超时）。"""
    test_now = datetime.now(_TZ)
    job_run = SchedulerJobRun(
        job_name=job_name,
        business_date="2026-06-25",
        run_key=f"{job_name}:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=test_now - timedelta(minutes=10),
        started_at=test_now - timedelta(minutes=10),
        heartbeat_at=test_now - timedelta(minutes=10),
        lease_expires_at=test_now - timedelta(minutes=5),
    )
    db_session.add(job_run)
    await db_session.flush()
    return job_run


@pytest.mark.asyncio
async def test_lifespan_calls_recovery_on_startup(db_session) -> None:
    """场景 1：lifespan 启动时调用 recover_stale_scheduler_job_runs 清理过期任务。

    验证方式：创建 stale job，运行 lifespan，检查 job 被恢复为 interrupted。
    （端到端行为验证，比 mock 调用计数更可靠）
    """
    from app.main import lifespan

    job_run = await _create_stale_job(db_session)
    job_run_id = job_run.id

    with patch("app.main.AsyncSessionLocal", _FakeSessionLocal(db_session)), \
         patch.object(db_session, "commit", new=db_session.flush), \
         patch("app.api.health.check_strategy_assets", _no_op_check_strategy_assets), \
         patch("app.services.strategy_seed.seed_strategies", _no_op_seed_strategies), \
         patch("app.services.calendar_seed.seed_calendar_from_pytdx", _no_op_seed_calendar):
        async with lifespan(None):
            pass  # yield 被到达，lifespan 正常运行

    # 验证 stale job 被恢复为 interrupted（证明 lifespan 调用了恢复函数）
    # populate_existing=True 强制从 DB 刷新（恢复函数用原始 SQL UPDATE 绕过 ORM identity map）
    stmt = (
        select(SchedulerJobRun)
        .where(SchedulerJobRun.id == job_run_id)
        .execution_options(populate_existing=True)
    )
    result = await db_session.execute(stmt)
    attached = result.scalar_one()
    assert attached.status == "interrupted"
    assert attached.error_code == "STALE_PROCESS_TERMINATED"


@pytest.mark.asyncio
async def test_lifespan_recovery_failure_does_not_block_startup(db_session) -> None:
    """场景 2：恢复函数抛异常时 lifespan 不阻塞启动（yield 正常执行）。

    验证方式：patch 恢复函数抛 RuntimeError，验证 lifespan 仍能进入 yield
    （async with 块的 body 能执行，未抛出异常）。
    patch 源模块属性：lifespan 内部 `from app.services... import` 会拿到 patched 对象。
    """
    from app.main import lifespan

    async def failing_recover(db, now=None):
        raise RuntimeError("模拟恢复失败")

    yield_reached = False
    with patch("app.main.AsyncSessionLocal", _FakeSessionLocal(db_session)), \
         patch.object(db_session, "commit", new=db_session.flush), \
         patch("app.api.health.check_strategy_assets", _no_op_check_strategy_assets), \
         patch("app.services.strategy_seed.seed_strategies", _no_op_seed_strategies), \
         patch("app.services.calendar_seed.seed_calendar_from_pytdx", _no_op_seed_calendar), \
         patch(
             "app.services.scheduler_job_run_recovery_service.recover_stale_scheduler_job_runs",
             failing_recover,
         ):
        async with lifespan(None):
            yield_reached = True

    assert yield_reached, (
        "恢复函数抛异常时 lifespan 应仍能到达 yield（不阻塞启动）"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
