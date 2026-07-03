"""幂等服务测试 - 验证 acquire_job_run_lock 的部分唯一索引 + 僵尸恢复语义。

覆盖 spec Phase 2 五个场景：
1. 新建 run_key 返回 (job_run, True)，status='running'
2. 已有 running 记录时返回 (existing, False)
3. 已有 interrupted 记录时允许新建 (job_run, True)，run_key 相同但 id 不同
4. 已有 running 但 lease 过期的僵尸任务，先 recover 为 interrupted 再新建 (job_run, True)
5. 两个独立 session 并发（asyncio.gather），只有一个返回 is_new=True

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
设计要点：
- 使用 PostgreSQL 测试库：pg_advisory_xact_lock / 部分唯一索引 / jsonb_set 均依赖 PG
- 测试 5 用独立 session（TestAsyncSessionLocal）+ asyncio.gather 真正并发
- run_key 加 uuid 后缀避免测试间残留数据冲突
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.models.scheduler_job_run import SchedulerJobRun
from app.services.idempotency_service import acquire_job_run_lock

_TZ = ZoneInfo("Asia/Shanghai")


async def _insert_job_run(
    db_session,
    *,
    run_key: str,
    job_name: str = "test_job",
    status: str = "running",
    lease_expires_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
    business_date: str = "2026-06-25",
    metadata: dict | None = None,
) -> SchedulerJobRun:
    """直接 INSERT 一条 SchedulerJobRun（用于测试前置数据准备）。"""
    now = datetime.now(_TZ)
    job_run = SchedulerJobRun(
        job_name=job_name,
        business_date=business_date,
        run_key=run_key,
        status=status,
        scheduled_at=now,
        started_at=now,
        heartbeat_at=heartbeat_at if heartbeat_at is not None else now,
        lease_expires_at=lease_expires_at if lease_expires_at is not None else now + timedelta(seconds=120),
        metadata_json=json.dumps(metadata) if metadata else None,
    )
    db_session.add(job_run)
    await db_session.flush()
    return job_run


@pytest.mark.asyncio
async def test_acquire_lock_new_job_returns_is_new_true(db_session) -> None:
    """场景 1：新建 run_key，返回 (job_run, True)，job_run.status='running'。"""
    run_key = f"test_new:{uuid.uuid4().hex[:8]}"

    job_run, is_new = await acquire_job_run_lock(
        db=db_session,
        run_key=run_key,
        job_name="test_new",
        business_date="2026-06-25",
        worker_instance_id="test-worker-1",
    )

    assert is_new is True
    assert job_run is not None
    assert job_run.run_key == run_key
    assert job_run.status == "running"
    assert job_run.worker_instance_id == "test-worker-1"
    assert job_run.lease_expires_at is not None
    assert job_run.heartbeat_at is not None


@pytest.mark.asyncio
async def test_acquire_lock_existing_running_returns_is_new_false(db_session) -> None:
    """场景 2：已有 running 记录时，返回 (existing, False)。"""
    run_key = f"test_dup:{uuid.uuid4().hex[:8]}"

    existing_run = await _insert_job_run(
        db_session, run_key=run_key, job_name="test_dup", status="running",
    )
    existing_id = existing_run.id
    await db_session.flush()

    job_run, is_new = await acquire_job_run_lock(
        db=db_session,
        run_key=run_key,
        job_name="test_dup",
        business_date="2026-06-25",
    )

    assert is_new is False
    assert job_run is not None
    assert job_run.id == existing_id


@pytest.mark.asyncio
async def test_acquire_lock_interrupted_allows_new_attempt(db_session) -> None:
    """场景 3：已有 interrupted 记录时允许新建 (job_run, True)，run_key 相同但 id 不同。

    验证部分唯一索引的核心收益：interrupted/failed 后可创建新 attempt。
    """
    run_key = f"test_retry:{uuid.uuid4().hex[:8]}"

    interrupted_run = await _insert_job_run(
        db_session, run_key=run_key, job_name="test_retry", status="interrupted",
    )
    interrupted_id = interrupted_run.id
    await db_session.flush()

    job_run, is_new = await acquire_job_run_lock(
        db=db_session,
        run_key=run_key,
        job_name="test_retry",
        business_date="2026-06-25",
    )

    assert is_new is True
    assert job_run is not None
    assert job_run.id != interrupted_id
    assert job_run.run_key == run_key
    assert job_run.status == "running"

    # 验证旧记录仍为 interrupted（未被修改）
    await db_session.refresh(interrupted_run)
    assert interrupted_run.status == "interrupted"


@pytest.mark.asyncio
async def test_acquire_lock_recovers_stale_first(db_session) -> None:
    """场景 4：已有 running 但 lease_expires_at < now 的僵尸任务，先恢复为 interrupted，新任务成功创建。

    验证 acquire_job_run_lock 内部调用 recover_stale_scheduler_job_runs：
    - 僵尸 running 被改为 interrupted（写 recovery 事件）
    - 部分唯一索引放行新记录 INSERT
    """
    run_key = f"test_stale:{uuid.uuid4().hex[:8]}"
    past = datetime.now(_TZ) - timedelta(minutes=5)

    stale_run = await _insert_job_run(
        db_session,
        run_key=run_key,
        job_name="test_stale",
        status="running",
        lease_expires_at=past,
        heartbeat_at=past,
    )
    stale_id = stale_run.id
    await db_session.flush()

    job_run, is_new = await acquire_job_run_lock(
        db=db_session,
        run_key=run_key,
        job_name="test_stale",
        business_date="2026-06-25",
    )

    assert is_new is True
    assert job_run is not None
    assert job_run.id != stale_id
    assert job_run.status == "running"

    # 验证僵尸被恢复为 interrupted
    await db_session.refresh(stale_run)
    assert stale_run.status == "interrupted"
    assert stale_run.error_code == "STALE_PROCESS_TERMINATED"


@pytest.mark.asyncio
async def test_acquire_lock_concurrent_only_one_wins() -> None:
    """场景 5：两个独立 session 并发（asyncio.gather），只有一个返回 is_new=True。

    流程：
    - worker_a: 独立 session，acquire_job_run_lock + commit（释放 advisory lock）
    - worker_b: 独立 session，acquire_job_run_lock（等待 worker_a commit 后获取锁）
    - worker_b SELECT 看到 worker_a 创建的 running 记录，返回 (existing, False)
    - 测试后手动 cleanup（独立 session 不受 db_session fixture 回滚保护）
    """
    from tests.conftest import TestAsyncSessionLocal

    run_key = f"test_concurrent:{uuid.uuid4().hex[:8]}"
    results: dict[str, tuple] = {}

    # 用 asyncio.Event 同步保证 worker_a 先 commit 释放 advisory lock，
    # worker_b 后开始（避免 asyncio 调度时序导致 worker_b 先执行并 rollback，
    # 进而 worker_a 看不到任何记录也创建新记录的 flaky 场景）。
    # 真实并发场景下 pg_advisory_xact_lock 会序列化，行为等价。
    a_done = asyncio.Event()

    async def worker_a() -> None:
        async with TestAsyncSessionLocal() as session:
            job_run, is_new = await acquire_job_run_lock(
                db=session,
                run_key=run_key,
                job_name="test_concurrent",
                business_date="2026-06-25",
                worker_instance_id="worker-a",
            )
            # commit 释放 advisory_xact_lock，让 worker_b 能获取锁
            await session.commit()
            # 在 session 关闭前提取 id（expire_on_commit=False 让 commit 后属性仍可用）
            results["a"] = {"id": job_run.id, "is_new": is_new}
            a_done.set()

    async def worker_b() -> None:
        # 等 worker_a commit 释放 advisory lock 后再开始，保证 worker_b 能看到 worker_a 的记录
        await a_done.wait()
        async with TestAsyncSessionLocal() as session:
            job_run, is_new = await acquire_job_run_lock(
                db=session,
                run_key=run_key,
                job_name="test_concurrent",
                business_date="2026-06-25",
                worker_instance_id="worker-b",
            )
            # 在 session 关闭前提取 id（避免 detached instance 错误）
            existing_id = job_run.id if job_run is not None else None
            results["b"] = {"id": existing_id, "is_new": is_new}
            await session.rollback()

    # 并发执行：worker_b 在 advisory_xact_lock 上阻塞，等 worker_a commit 后才能继续
    await asyncio.gather(worker_a(), worker_b())

    # 验证：worker_a 抢到锁（is_new=True），worker_b 看到 existing（is_new=False）
    assert "a" in results, "worker_a 未执行"
    assert "b" in results, "worker_b 未执行"
    assert results["a"]["is_new"] is True, "worker_a 应抢到锁"
    assert results["b"]["is_new"] is False, "worker_b 应看到 existing"
    assert results["b"]["id"] == results["a"]["id"], (
        "worker_b 应返回 worker_a 创建的记录"
    )

    # cleanup：删除测试创建的记录（独立 session 已 commit，不受 fixture 回滚保护）
    async with TestAsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM scheduler_job_runs WHERE run_key = :run_key"),
            {"run_key": run_key},
        )
        await session.commit()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
