"""HISTORY PROGRESS CONCURRENT LOST-UPDATE — PRE-DEPLOY TARGETED PG CLOSURE (CORRECTION-05)。

真实 PostgreSQL 验证（PANJI_REMOTE_VERIFY_DB_TEST=1, plan=targeted-pg）：

证明 _make_history_executor_progress 与 _make_history_business_progress 两个独立 writer
在真实 asyncio 并发下，通过 SELECT ... FOR UPDATE 行锁串行化，对同一
SchedulerJobRun.metadata_json.computing_history 的 read-modify-write 不再发生 lost update。

LU-1  初始 processed=500 / heartbeat=H0；同时启动：
        - executor heartbeat → heartbeat=H1
        - business progress → processed=1000, total=5283
      最终必须**同时**具备 processed==1000（业务进度未被 executor 覆盖回 500）
      与 heartbeat==H1（executor 进度未被 business 覆盖回 H0）。

LU-2  反转方向：初始 processed=1000 / heartbeat=H0；同时启动：
        - business progress → processed=2000（业务继续推进）
        - executor heartbeat → heartbeat=H1
      最终 processed==2000 且 heartbeat==H1（双向都不丢）。

LU-3  高压并发：串行多次 gather 10 轮 executor+business，最终 processed/total 为最后一次
      business 值且 heartbeat 为最后一次 H1，无中间回退。

CREDENTIAL/SAFETY：本文件只在 PANJI_REMOTE_VERIFY_DB_TEST=1 的远程验证库
（bz_stock_verify_<sha>）运行，不连 production bz_stock，不手工 UPDATE，不部署。
每个 test 使用独立 committed session 准备数据，并各自清理。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.db import AsyncSessionLocal
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.after_close_orchestrator import (
    _make_history_business_progress,
    _make_history_executor_progress,
    _update_heartbeat_and_step,
)


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _create_job_run_with_history(
    *,
    processed: int,
    total: int,
    heartbeat: str,
) -> uuid.UUID:
    """用独立 committed session 创建带 computing_history step_summary 的 job_run。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    td = date(2026, 8, 25)
    meta = {
        "orchestrator_status": "computing_history",
        "trade_date": td.isoformat(),
        "step_summary": {
            "computing_history": {
                "step": "computing_history",
                "status": "running",
                "started_at": heartbeat,
                "last_progress_at": heartbeat,
                "heartbeat_at": heartbeat,
                "processed": processed,
                "total": total,
                "target_state_count": processed,
            }
        },
    }
    job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=td.isoformat(),
        run_key=f"after_close_orchestrator:lu:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now,
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    async with AsyncSessionLocal() as db:
        db.add(job_run)
        await db.flush()
        job_run_id = job_run.id
        await db.commit()
    return job_run_id


async def _read_computing_history(job_run_id: uuid.UUID) -> dict:
    async with AsyncSessionLocal() as db:
        job = await db.get(SchedulerJobRun, job_run_id)
        meta = json.loads(job.metadata_json)
        return meta.get("step_summary", {}).get("computing_history", {})


async def _read_full_meta(job_run_id: uuid.UUID) -> dict:
    async with AsyncSessionLocal() as db:
        job = await db.get(SchedulerJobRun, job_run_id)
        return json.loads(job.metadata_json)


async def _cleanup(job_run_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(SchedulerJobRun, job_run_id)
        if job is not None:
            await db.delete(job)
            await db.commit()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_lu1_concurrent_executor_and_business_no_lost_update() -> None:
    """LU-1: 同时启动 executor heartbeat 与 business progress，不得 lost update。"""
    h0 = "2026-08-25T12:00:00+00:00"
    h1 = "2026-08-25T12:00:10+00:00"
    job_run_id = await _create_job_run_with_history(
        processed=500, total=5283, heartbeat=h0
    )
    try:
        biz = _make_history_business_progress(job_run_id, "w1")
        exec_cb = _make_history_executor_progress(job_run_id, "w1")

        async def _executor():
            await exec_cb({
                "step": "computing_history", "status": "running",
                "started_at": h0, "heartbeat_at": h1,
            })

        async def _business():
            await biz({"processed": 1000, "total": 5283, "target_state_count": 1000})

        # 真正并发：两个 writer 各自独立 AsyncSession，不人为顺序 await
        await asyncio.gather(_executor(), _business())

        after = await _read_computing_history(job_run_id)
        assert after["processed"] == 1000, (
            f"lost update: business processed 被 executor 覆盖回 {after.get('processed')}"
        )
        assert after["total"] == 5283
        assert after["target_state_count"] == 1000
        assert after["heartbeat_at"] == h1, (
            f"lost update: executor heartbeat 被 business 覆盖回 {after.get('heartbeat_at')}"
        )
        assert after["status"] == "running"
    finally:
        await _cleanup(job_run_id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_lu2_concurrent_business_advance_and_executor_no_lost_update() -> None:
    """LU-2: 反转方向，business 继续推进 + executor heartbeat，双向都不丢。"""
    h0 = "2026-08-25T12:00:00+00:00"
    h1 = "2026-08-25T12:00:10+00:00"
    job_run_id = await _create_job_run_with_history(
        processed=1000, total=5283, heartbeat=h0
    )
    try:
        biz = _make_history_business_progress(job_run_id, "w1")
        exec_cb = _make_history_executor_progress(job_run_id, "w1")

        async def _executor():
            await exec_cb({
                "step": "computing_history", "status": "running",
                "started_at": h0, "heartbeat_at": h1,
            })

        async def _business():
            await biz({"processed": 2000, "total": 5283, "target_state_count": 2000})

        await asyncio.gather(_business(), _executor())

        after = await _read_computing_history(job_run_id)
        assert after["processed"] == 2000, (
            f"lost update: business 推进被覆盖回 {after.get('processed')}"
        )
        assert after["heartbeat_at"] == h1, (
            f"lost update: executor heartbeat 被覆盖回 {after.get('heartbeat_at')}"
        )
    finally:
        await _cleanup(job_run_id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_lu3_high_contention_no_intermediate_rollback() -> None:
    """LU-3: 高压并发 10 轮 gather，最终 state 为最后一次 business/executor 值，无中间回退。"""
    h0 = "2026-08-25T12:00:00+00:00"
    job_run_id = await _create_job_run_with_history(
        processed=0, total=5283, heartbeat=h0
    )
    try:
        biz = _make_history_business_progress(job_run_id, "w1")
        exec_cb = _make_history_executor_progress(job_run_id, "w1")
        for i in range(1, 11):
            h_i = f"2026-08-25T12:00:{i:02d}+00:00"
            async def _executor(h=h_i):
                await exec_cb({
                    "step": "computing_history", "status": "running",
                    "started_at": h0, "heartbeat_at": h,
                })
            async def _business(p=i):
                await biz({"processed": p * 100, "total": 5283, "target_state_count": p * 100})
            await asyncio.gather(_executor(), _business())

        after = await _read_computing_history(job_run_id)
        assert after["processed"] == 1000, (
            f"高压并发后 processed 应为最后一次 1000，实际 {after.get('processed')}"
        )
        assert after["total"] == 5283
        # 最后一次 heartbeat 为 12:00:10
        assert after["heartbeat_at"] == "2026-08-25T12:00:10+00:00", (
            f"heartbeat 应为最后一次 H1，实际 {after.get('heartbeat_at')}"
        )
    finally:
        await _cleanup(job_run_id)


async def _load_job_run(db, job_run_id: uuid.UUID) -> SchedulerJobRun:
    return await db.get(SchedulerJobRun, job_run_id)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_lu4_checkpoint_sees_fresh_metadata_after_independent_commit() -> None:
    """LU-4: session A 先加载 SchedulerJobRun；独立 session B 更新并提交
    computing_history.processed；session A 再执行 _update_heartbeat_and_step；
    最终必须同时保留新的 processed 与新的 checkpoint。

    验证 CORRECTION-05 的修复：_update_heartbeat_and_step 内部以 FOR UPDATE 重新从 DB 读取
    最新 metadata（不信任 session A 内存中旧快照），否则 identity-map stale 会覆盖业务进度。
    """
    h0 = "2026-08-25T12:00:00+00:00"
    job_run_id = await _create_job_run_with_history(
        processed=500, total=5283, heartbeat=h0
    )
    try:
        # session A 先加载 job_run（模拟旧快照已驻留内存）
        async with AsyncSessionLocal() as db_a:
            await _load_job_run(db_a, job_run_id)

        # 独立 session B 更新并提交 computing_history.processed
        biz = _make_history_business_progress(job_run_id, "w1")
        await biz({"processed": 1000, "total": 5283, "target_state_count": 1000})

        # session A 再次加载后执行 checkpoint（last_completed_step=computing_history）
        async with AsyncSessionLocal() as db_a2:
            job_a2 = await _load_job_run(db_a2, job_run_id)
            await _update_heartbeat_and_step(db_a2, job_a2, "computing_history", "w1")

        after = await _read_computing_history(job_run_id)
        # 新的业务进度必须保留
        assert after["processed"] == 1000, (
            f"checkpoint 覆盖了业务进度，processed={after.get('processed')}"
        )
        assert after["target_state_count"] == 1000
        # 新的 checkpoint 必须写入
        assert after["status"] == "completed", (
            f"checkpoint 未生效，status={after.get('status')}"
        )
        meta = await _read_full_meta(job_run_id)
        assert meta.get("last_completed_step") == "computing_history", (
            f"last_completed_step={meta.get('last_completed_step')}"
        )
    finally:
        await _cleanup(job_run_id)
