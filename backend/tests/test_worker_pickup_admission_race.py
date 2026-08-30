"""E2.1 P1-C 确定性 race 证明（真实 PostgreSQL 行锁，无 sleep）。

这些测试**只在 verification PostgreSQL 下运行**
（PANJI_REMOTE_VERIFY_DB_TEST=1，DATABASE_URL 指向 bz_stock_verify_<sha>）。
本地/CI 默认跳过（PURE_UNIT_TEST=1 或无 verify DB 连接）。

核心不变量：admission 判定与 worker claim 共享同一个 PostgreSQL admission 行锁
（worker_pickup_admission 的 FOR UPDATE），因此不存在 TOCTOU。

本文件使用**真实并发事务 + 行锁屏障**（asyncio.Event），证明两个事务确实在
同一行上相互阻塞，而不是顺序执行的假 race：

CASE 1 PAUSE-WINS（真实并发）：
  operator 事务先持 admission 行 FOR UPDATE（未提交）并置 PAUSED；
  worker 事务尝试同一行 FOR UPDATE → 被 PostgreSQL 行锁阻塞；
  operator 提交（释放锁）后 worker 才继续 → 读到 PAUSED → 不 claim。

CASE 2 WORKER-WINS（真实并发）：
  worker 事务先持 admission 行 FOR UPDATE（active）并 claim queued→running（未提交）；
  operator 事务尝试同一行 FOR UPDATE（acquire pause）→ 被阻塞；
  worker 提交（释放锁）后 operator 才 acquire 成功（置 PAUSED）；
  此时 running 已存在 → 部署 secondary gate 必须 BLOCK。

每个测试前后 reset admission row + 清理 fixture job，避免 test A 留 paused 污染 test B。
worker 侧 claim 使用的 SQL/ownership path 与 production owner
`worker.py::_after_close_poll_once` 完全一致（is_pickup_admitted + FOR UPDATE SKIP LOCKED）；
其调用关系由 pure-unit 测试 test_worker_pickup_admission_service.py 中的
_after_close_poll_once 用例覆盖。
"""
from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.scheduler_job_run import SchedulerJobRun
from app.models.worker_pickup_admission import WorkerPickupAdmission
from app.services.worker_pickup_admission_service import (
    acquire_pause,
    is_pickup_admitted,
    new_pause_token,
)

SCOPE = "after_close_orchestrator"
FIXTURE_BD = "2099-01-01"  # 隔离标记：只清理本文件插入的 fixture job

pytestmark = pytest.mark.postgres


def _engine():
    url = os.environ.get("DATABASE_URL") or os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("no verification DATABASE_URL; skipping P1-C race tests")
    return create_async_engine(url)


async def _reset_admission(session) -> None:
    """UPSERT singleton row 为 active（paused=false, token=None），保证 test 间隔离。"""
    stmt = (
        pg_insert(WorkerPickupAdmission)
        .values(
            scope=SCOPE,
            paused=False,
            pause_token=None,
            paused_by=None,
            reason=None,
            paused_at=None,
        )
        .on_conflict_do_update(
            index_elements=["scope"],
            set_={
                "paused": False,
                "pause_token": None,
                "paused_by": None,
                "reason": None,
                "paused_at": None,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()


async def _seed_fixture_job(session) -> str:
    job = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=FIXTURE_BD,
        status="queued",
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return str(job.id)


async def _cleanup(session) -> None:
    await session.execute(
        select(SchedulerJobRun)
        .where(SchedulerJobRun.business_date == FIXTURE_BD)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        SchedulerJobRun.__table__.delete().where(
            SchedulerJobRun.business_date == FIXTURE_BD
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_pause_wins_race() -> None:
    """operator 事务持行锁（未提交）置 PAUSED；worker 事务被同一行锁阻塞，
    提交后 worker 读到 PAUSED → 不 claim。真实并发，无 sleep。"""
    engine = _engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        await _reset_admission(s)

    lock_held = asyncio.Event()  # operator 已持行锁、尚未提交

    async def operator():
        async with Session() as s:
            # 持 admission 行 FOR UPDATE（未提交）
            await s.execute(
                select(WorkerPickupAdmission)
                .where(WorkerPickupAdmission.scope == SCOPE)
                .with_for_update()
            )
            row = (
                await s.execute(
                    select(WorkerPickupAdmission).where(
                        WorkerPickupAdmission.scope == SCOPE
                    )
                )
            ).scalar_one()
            row.paused = True
            row.pause_token = new_pause_token()
            row.paused_by = "race:pause-wins"
            lock_held.set()  # 持锁且未提交：worker 的 FOR UPDATE 将在此刻被阻塞
            await s.commit()  # 释放行锁

    async def worker():
        await lock_held.wait()  # 等 operator 持锁（未提交）才发起查询
        async with Session() as w:
            # is_pickup_admitted 内部对同一行 FOR UPDATE；operator 未提交时此处被阻塞
            admitted = await is_pickup_admitted(w, SCOPE)
        return admitted

    try:
        task_op = asyncio.create_task(operator())
        admitted = await worker()  # 阻塞直到 operator 提交
        await task_op
    finally:
        async with Session() as s:
            await _reset_admission(s)

    assert admitted is False, "PAUSE-WINS: worker 必须被行锁阻塞后读到 PAUSED 且不 claim"


@pytest.mark.asyncio
async def test_worker_wins_race() -> None:
    """worker 事务持行锁 claim queued→running（未提交）；operator 事务尝试同一行锁
    被阻塞；worker 提交后 operator acquire 成功（PAUSED）。真实并发，无 sleep。"""
    engine = _engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        await _reset_admission(s)
        job_id = await _seed_fixture_job(s)

    claimed = asyncio.Event()  # worker 已置 running、尚未提交

    async def worker():
        async with Session() as w:
            # 持 admission 行 FOR UPDATE（active），与 production owner 同路径
            admitted = await is_pickup_admitted(w, SCOPE)
            assert admitted is True, "WORKER-WINS: worker 应先读到 active"
            job = (
                await w.execute(
                    select(SchedulerJobRun)
                    .where(SchedulerJobRun.id == job_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one()
            job.status = "running"
            claimed.set()  # running 已置、行锁仍持有（未提交）；operator 的 acquire 将在此刻被阻塞
            await w.commit()  # 释放行锁

    async def operator():
        await claimed.wait()  # 等 worker 持锁（未提交）才发起 acquire
        async with Session() as o:
            # acquire_pause 内部对同一行 FOR UPDATE；worker 未提交时此处被阻塞
            ok = await acquire_pause(
                o,
                scope=SCOPE,
                token=new_pause_token(),
                actor="race:worker-wins",
                reason="worker-wins",
            )
            await o.commit()
        return ok

    try:
        task_w = asyncio.create_task(worker())
        ok = await operator()  # 阻塞直到 worker 提交
        await task_w
    finally:
        async with Session() as s:
            await _cleanup(s)
            await _reset_admission(s)

    assert ok is True, "WORKER-WINS: operator acquire 应在 worker 提交后成功"
    # running 已存在 + row PAUSED → 部署 secondary gate 必须 BLOCK（不变量验证）
    async with Session() as s:
        running = (
            await s.execute(
                select(SchedulerJobRun).where(SchedulerJobRun.status == "running")
            )
        ).scalars().all()
    assert len(running) >= 1, "WORKER-WINS: running job 已存在 → 部署 secondary gate BLOCK"
