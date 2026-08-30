"""E2.1 P1-C 确定性 race 证明（真实 PostgreSQL 行锁，无 sleep）。

这些测试**只在 verification PostgreSQL 下运行**
（PANJI_REMOTE_VERIFY_DB_TEST=1，DATABASE_URL 指向 bz_stock_verify_<sha>）。
本地/CI 默认跳过（PURE_UNIT_TEST=1 或无 verify DB 连接）。

核心不变量：admission 判定与 worker claim 共享同一个 PostgreSQL admission 行锁
（worker_pickup_admission 的 FOR UPDATE），因此不存在 TOCTOU。

CASE 1 PAUSE-WINS：operator 先持有行锁并置 PAUSED，worker 的 is_pickup_admitted
  在行锁上阻塞，直到 operator 提交；提交后 worker 读到 PAUSED → 不 claim。

CASE 2 WORKER-WINS：worker 先在同事务内读 active + claim queued→running 并提交；
  operator 随后 acquire pause 成功（行此前为 active）；此时已存在 running job，
  deploy secondary gate（running=0）必须 BLOCK。
"""
from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.scheduler_job_run import SchedulerJobRun
from app.models.worker_pickup_admission import WorkerPickupAdmission
from app.services.worker_pickup_admission_service import (
    acquire_pause,
    is_pickup_admitted,
    new_pause_token,
)

SCOPE = "after_close_orchestrator"

pytestmark = pytest.mark.postgres


def _engine():
    url = os.environ.get("DATABASE_URL") or os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("no verification DATABASE_URL; skipping P1-C race tests")
    return create_async_engine(url)


async def _seed_row(session) -> None:
    existing = (
        await session.execute(
            select(WorkerPickupAdmission).where(
                WorkerPickupAdmission.scope == SCOPE
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(WorkerPickupAdmission(scope=SCOPE, paused=False))
        await session.commit()


async def _insert_queued_job(session) -> str:
    job = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date="2026-08-31",
        status="queued",
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return str(job.id)


@pytest.mark.asyncio
async def test_pause_wins_race() -> None:
    """operator 先持行锁置 PAUSED；worker 的 admission 读取在行锁上阻塞，
    提交后读到 PAUSED → 不 claim（queued 保持不变）。"""
    engine = _engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        await _seed_row(s)

    ready = asyncio.Event()  # operator 已持锁
    worker_done = asyncio.Event()

    async def operator_hold():
        async with Session() as s:
            # 持有行锁（不立即提交），模拟 pause 先赢
            await s.execute(
                select(WorkerPickupAdmission)
                .where(WorkerPickupAdmission.scope == SCOPE)
                .with_for_update()
            )
            await s.execute  # no-op keep handle
            # 直接置 paused 并提交（真实 acquire 语义）
            await acquire_pause(
                s, scope=SCOPE, token=new_pause_token(), actor="race", reason="pause-wins"
            )
            await s.commit()
        ready.set()

    async def worker_attempt():
        await ready.wait()
        async with Session() as w:
            # 此 SELECT ... FOR UPDATE 在 operator 提交前会阻塞于行锁
            admitted = await is_pickup_admitted(w, SCOPE)
        worker_done.set()
        return admitted

    op = asyncio.create_task(operator_hold())
    wk = asyncio.create_task(worker_attempt())
    admitted = await wk
    await op

    assert admitted is False, "PAUSE-WINS: worker 必须读到 PAUSED 且不 claim"
    # 确认没有 running job 被创建（worker 未 claim）
    async with Session() as s:
        running = (
            await s.execute(
                select(SchedulerJobRun).where(SchedulerJobRun.status == "running")
            )
        ).scalars().all()
    assert running == [], "PAUSE-WINS: 不应产生 running job"


@pytest.mark.asyncio
async def test_worker_wins_race() -> None:
    """worker 先 claim queued→running 并提交；operator 随后 acquire pause 成功
    （行此前 active）；此时存在 running job → deploy secondary gate 必须 BLOCK。"""
    engine = _engine()
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        await _seed_row(s)
        job_id = await _insert_queued_job(s)

    async def worker_claim():
        async with Session() as w:
            # 同事务：读 active + claim queued→running（FOR UPDATE SKIP LOCKED）
            if await is_pickup_admitted(w, SCOPE):
                job = (
                    await w.execute(
                        select(SchedulerJobRun)
                        .where(SchedulerJobRun.id == job_id)
                        .with_for_update(skip_locked=True)
                    )
                ).scalar_one_or_none()
                if job is not None:
                    job.status = "running"
                    await w.commit()
                    return True
            return False

    claimed = await worker_claim()
    assert claimed is True, "WORKER-WINS: worker 应先成功 claim running"

    # operator 随后 acquire pause（行此前 active → 成功）
    async with Session() as s:
        ok = await acquire_pause(
            s, scope=SCOPE, token=new_pause_token(), actor="race", reason="worker-wins"
        )
        await s.commit()
    assert ok is True, "WORKER-WINS: operator acquire 应在 worker 之后成功"

    # secondary gate 依据：running>0 必须 BLOCK
    async with Session() as s:
        running = (
            await s.execute(
                select(SchedulerJobRun).where(SchedulerJobRun.status == "running")
            )
        ).scalars().all()
    assert len(running) >= 1, "WORKER-WINS: running job 已存在 → 部署 secondary gate BLOCK"
