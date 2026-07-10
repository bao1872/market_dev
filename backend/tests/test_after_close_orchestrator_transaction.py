"""P0-6 盘后编排真实事务测试（真实测试库，不使用 FakeDB）。

验证目标：
1. feature_snapshot 进度回调（_build_feature_snapshot_progress_callback）在一次 commit 中
   同时写入 metadata_json（feature_snapshot_progress）与 job_run_events（progress 事件），
   换新 session 后二者必须“同时可见”（证明修复：进度事件不再因 append_event 在 commit
   之后执行而丢失）；
2. _update_orchestrator_status 在同一事务中 flush metadata + event 后，若事务 rollback，
   则 metadata 更新与新事件“都不可见”（证明原子性，无部分提交）。

这些测试使用真实 app.db.AsyncSessionLocal（conftest 已将 DATABASE_URL 指向测试库），
不使用 savepoint db_session fixture（因回调内部会开独立 session/连接），
故每个测试自行创建真实 SchedulerJobRun 并在 finally 中删除（级联清除事件）。

运行:
    APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
        backend/.venv/bin/python -m pytest \
        tests/test_after_close_orchestrator_transaction.py -q
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete

from app.db import AsyncSessionLocal
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    _build_feature_snapshot_progress_callback,
    _parse_metadata,
    _update_orchestrator_status,
)
from app.services.job_run_event_service import list_events

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_AFTER_CLOSE_JOB_NAME = "after_close_orchestrator"


async def _create_running_job_run(orchestrator_status: AfterCloseRunStatus) -> uuid.UUID:
    """在真实测试库创建一条 running 的盘后编排 job_run，返回 id（调用方负责清理）。"""
    now = datetime.now(_SHANGHAI_TZ)
    job_run_id = uuid.uuid4()
    metadata = {
        "orchestrator_status": orchestrator_status.value,
        "trade_date": "2026-07-10",
    }
    async with AsyncSessionLocal() as db:
        db.add(
            SchedulerJobRun(
                id=job_run_id,
                job_name=_AFTER_CLOSE_JOB_NAME,
                business_date="2026-07-10",
                # run_key 置空避免与活跃 run_key 部分唯一索引冲突
                run_key=None,
                status="running",
                started_at=now,
                heartbeat_at=now,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
        )
        await db.commit()
    return job_run_id


async def _delete_job_run(job_run_id: uuid.UUID) -> None:
    """清理测试创建的 job_run（外键 ON DELETE CASCADE 级联清除事件）。"""
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(SchedulerJobRun).where(SchedulerJobRun.id == job_run_id)
        )
        await db.commit()


@pytest.mark.asyncio
async def test_progress_callback_commits_metadata_and_event_atomically():
    """进度回调一次 commit 后，metadata 进度与 progress 事件在新 session 中同时可见。"""
    job_run_id = await _create_running_job_run(AfterCloseRunStatus.FEATURE_SNAPSHOT)
    try:
        callback = _build_feature_snapshot_progress_callback(job_run_id)
        # processed=500 达到 _FEATURE_SNAPSHOT_PROGRESS_EVENT_INTERVAL 阈值，触发事件写入
        await callback(
            phase="compute",
            processed=500, total=1000,
            computed_count=480, written_count=0, failed_count=20,
            started_at=None,
        )

        # 换新 session 验证 metadata_json 与 job_run_events 同时可见
        async with AsyncSessionLocal() as verify_db:
            job_run = await verify_db.get(SchedulerJobRun, job_run_id)
            assert job_run is not None
            meta = _parse_metadata(job_run)
            progress = meta.get("feature_snapshot_progress")
            assert progress is not None, "metadata 未写入 feature_snapshot_progress"
            assert progress["phase"] == "compute"
            assert progress["processed"] == 500
            assert progress["total"] == 1000
            assert progress["computed_count"] == 480
            assert progress["snapshot_count"] == 480
            assert progress["failed_count"] == 20

            events = await list_events(verify_db, job_run_id, limit=50)
            progress_events = [
                e for e in events
                if e.step == AfterCloseRunStatus.FEATURE_SNAPSHOT.value
                and (e.payload or {}).get("event_type") == "progress"
            ]
            assert progress_events, (
                "进度事件未持久化：metadata 可见但事件缺失，"
                "说明 append_event 未与 metadata 同一次 commit（回归修复失败）"
            )
            assert progress_events[0].payload["processed"] == 500
    finally:
        await _delete_job_run(job_run_id)


@pytest.mark.asyncio
async def test_progress_callback_below_threshold_writes_metadata_only():
    """低于事件阈值时：metadata 进度可见，但不写 progress 事件（避免事件表膨胀）。"""
    job_run_id = await _create_running_job_run(AfterCloseRunStatus.FEATURE_SNAPSHOT)
    try:
        callback = _build_feature_snapshot_progress_callback(job_run_id)
        # processed=100 < 500 阈值：只更新 metadata，不写事件
        await callback(
            phase="compute",
            processed=100, total=1000,
            computed_count=95, written_count=0, failed_count=5,
            started_at=None,
        )

        async with AsyncSessionLocal() as verify_db:
            job_run = await verify_db.get(SchedulerJobRun, job_run_id)
            assert job_run is not None
            meta = _parse_metadata(job_run)
            assert meta.get("feature_snapshot_progress", {}).get("processed") == 100

            events = await list_events(verify_db, job_run_id, limit=50)
            progress_events = [
                e for e in events
                if (e.payload or {}).get("event_type") == "progress"
            ]
            assert not progress_events, "低于阈值不应写入 progress 事件"
    finally:
        await _delete_job_run(job_run_id)


@pytest.mark.asyncio
async def test_orchestrator_status_update_rolls_back_atomically():
    """事务异常 rollback 后：状态切换 metadata 与新事件都不可见（原子性）。"""
    job_run_id = await _create_running_job_run(AfterCloseRunStatus.QUALITY_GATE)
    try:
        # 在一个真实 session 中执行状态切换（flush metadata + event），随后抛异常触发 rollback
        with pytest.raises(RuntimeError, match="模拟阶段失败"):
            async with AsyncSessionLocal() as db:
                job_run = await db.get(SchedulerJobRun, job_run_id)
                assert job_run is not None
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.FEATURE_SNAPSHOT,
                    message="切换到 feature_snapshot（将被 rollback）",
                )
                # 模拟阶段执行异常：未 commit 即抛出，async with 退出时 rollback
                raise RuntimeError("模拟阶段失败")

        # 换新 session 验证：metadata 仍为 quality_gate，且无 feature_snapshot 事件
        async with AsyncSessionLocal() as verify_db:
            job_run = await verify_db.get(SchedulerJobRun, job_run_id)
            assert job_run is not None
            meta = _parse_metadata(job_run)
            assert meta.get("orchestrator_status") == AfterCloseRunStatus.QUALITY_GATE.value, (
                "rollback 后 metadata 不应更新为 feature_snapshot"
            )

            events = await list_events(verify_db, job_run_id, limit=50)
            feature_snapshot_events = [
                e for e in events if e.step == AfterCloseRunStatus.FEATURE_SNAPSHOT.value
            ]
            assert not feature_snapshot_events, (
                "rollback 后不应存在 feature_snapshot 事件（部分提交）"
            )
    finally:
        await _delete_job_run(job_run_id)
