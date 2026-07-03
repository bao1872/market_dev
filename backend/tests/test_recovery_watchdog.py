"""Recovery 看门狗测试 - 验证 _recovery_watchdog_loop 行为。

覆盖 3 个场景（spec Phase 4 Task 4.5）：
1. 看门狗恢复 lease 过期的 running 任务为 interrupted
2. 默认间隔 60s（验证函数签名默认值）
3. 恢复函数抛异常时看门狗不退出（捕获异常后继续循环）

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
设计要点：
- 看门狗内部用 AsyncSessionLocal() 创建独立 session，测试中 patch 为 test db_session
- 用 patch.object 把 db.commit 替换为 flush，避免破坏 fixture 的 nested 事务
- 用 fake asyncio.sleep 控制循环退出（raise CancelledError）
- patch _heartbeat_loop 为 no-op，避免心跳 DB 写入干扰
- 保存 real_sleep 引用，fake_sleep 内 await real_sleep(0) 让事件循环有机会运行心跳 task
"""

from __future__ import annotations

import asyncio
import inspect
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


async def _no_op_heartbeat(worker_name: str, interval: int = 60) -> None:
    """测试用 no-op 心跳，避免真实心跳的 DB 写入。"""
    return


async def _create_stale_job(db_session, *, job_name: str = "test_watchdog_job") -> SchedulerJobRun:
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
async def test_watchdog_recovers_stale_job(db_session) -> None:
    """场景 1：看门狗恢复 lease 过期的 running 任务为 interrupted。

    流程：
    - 创建 stale running 任务
    - patch AsyncSessionLocal/_heartbeat_loop/asyncio.sleep
    - asyncio.sleep 第一次调用即 raise CancelledError 退出循环（已执行一次恢复）
    - 验证任务 status -> interrupted
    """
    from app import worker as worker_mod

    job_run = await _create_stale_job(db_session)
    job_run_id = job_run.id

    real_sleep = asyncio.sleep
    sleep_count = 0

    async def fake_sleep(seconds):
        nonlocal sleep_count
        sleep_count += 1
        # 让事件循环有机会运行 _hb_task（避免 task 未完成警告）
        await real_sleep(0)
        raise asyncio.CancelledError()

    with patch.object(worker_mod, "AsyncSessionLocal", _FakeSessionLocal(db_session)), \
         patch.object(db_session, "commit", new=db_session.flush), \
         patch.object(worker_mod, "_heartbeat_loop", _no_op_heartbeat), \
         patch.object(worker_mod, "_shutdown", False), \
         patch("asyncio.sleep", new=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await worker_mod._recovery_watchdog_loop(interval_seconds=1)

    assert sleep_count == 1, "看门狗应执行一次恢复后因 CancelledError 退出"

    # 验证任务被恢复为 interrupted
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
async def test_watchdog_interval_default_60s() -> None:
    """场景 2：默认间隔 60s（检查函数签名默认值）。"""
    from app.worker import _recovery_watchdog_loop

    sig = inspect.signature(_recovery_watchdog_loop)
    interval_param = sig.parameters.get("interval_seconds")
    assert interval_param is not None, "函数应有 interval_seconds 参数"
    assert interval_param.default == 60, (
        f"默认间隔应为 60s，实际: {interval_param.default}"
    )


@pytest.mark.asyncio
async def test_watchdog_handles_exception(db_session) -> None:
    """场景 3：恢复函数抛异常时看门狗不退出（捕获异常后继续循环）。

    流程：
    - 第 1 次调用 recover_stale_scheduler_job_runs 抛 RuntimeError
    - 看门狗 except Exception 捕获后继续循环
    - 第 2 次调用正常返回 0
    - 第 2 次 asyncio.sleep raise CancelledError 退出循环
    - 验证 recover 被调用 >= 2 次（证明异常未中断循环）
    """
    from app import worker as worker_mod

    real_sleep = asyncio.sleep
    sleep_count = 0
    recover_count = 0

    async def fake_sleep(seconds):
        nonlocal sleep_count
        sleep_count += 1
        await real_sleep(0)
        if sleep_count >= 2:
            raise asyncio.CancelledError()

    async def flaky_recover(db, now=None):
        nonlocal recover_count
        recover_count += 1
        if recover_count == 1:
            raise RuntimeError("模拟恢复失败")
        return 0

    with patch.object(worker_mod, "AsyncSessionLocal", _FakeSessionLocal(db_session)), \
         patch.object(db_session, "commit", new=db_session.flush), \
         patch.object(worker_mod, "_heartbeat_loop", _no_op_heartbeat), \
         patch.object(worker_mod, "_shutdown", False), \
         patch("asyncio.sleep", new=fake_sleep), \
         patch.object(worker_mod, "recover_stale_scheduler_job_runs", flaky_recover):
        with pytest.raises(asyncio.CancelledError):
            await worker_mod._recovery_watchdog_loop(interval_seconds=1)

    assert recover_count >= 2, (
        f"看门狗应在异常后继续循环，recover 调用次数: {recover_count}"
    )
    assert sleep_count == 2, (
        f"应执行 2 次 sleep 后退出，实际: {sleep_count}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
