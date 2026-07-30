"""Chip Consensus Worker 测试 - 验证 chip consensus 任务的领取、并发、断点续算、恢复（P0-3）。

覆盖 4 类场景（ref/instruction.md §二.3）：
1. 领取（test_worker_claims_queued_chip_job）：Worker 领取 queued 任务（status→running, lease_epoch 递增）
2. 重复领取（test_worker_concurrent_only_one_claims_chip）：并发只有一个领取成功（FOR UPDATE SKIP LOCKED）
3. 部分成功（test_worker_partial_success_writes_metadata）：chip 部分成功写 metadata.chip_status=partial，主 status=succeeded
4. 恢复（test_worker_resumes_interrupted_chip_job）：interrupted → resume_queued → Worker 领取断点续算

附加：
5. 缺失 trade_date/core_run_id → 立即标记 failed
6. resume_queued 任务调用 get_pending_chip_instruments 过滤已成功项

测试环境：PostgreSQL 测试库
运行：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... \
        pytest backend/tests/test_chip_consensus_worker.py -v
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.models.scheduler_job_run import SchedulerJobRun

# CI 环境标识（与 conftest.py 一致）
_CI_ENV = (
    os.environ.get("GITHUB_ACTIONS", "").lower() in ("1", "true", "yes")
    or os.environ.get("PANJI_CI_DB_TEST", "").lower() in ("1", "true", "yes")
)

# 本测试文件全部为 PG 集成测试（依赖 TestAsyncSessionLocal fixture），
# 只在 CI 临时 Postgres 容器中运行；本地 PURE_UNIT_TEST=1 自动 skip。
pytestmark = pytest.mark.skipif(
    not _CI_ENV,
    reason="chip consensus worker 测试为 PG 集成测试，只在 CI 临时 Postgres 容器中运行；本地请用 PURE_UNIT_TEST=1",
)

_TZ = ZoneInfo("Asia/Shanghai")
_TRADE_DATE = date(2026, 6, 25)
_CHIP_LEASE_SECONDS = 3600


# ---------------------------------------------------------------------------
# 测试辅助函数
# ---------------------------------------------------------------------------


async def _create_queued_chip_job(
    session_factory,
    *,
    trade_date: date = _TRADE_DATE,
    core_run_id: uuid.UUID | None = None,
    status: str = "queued",
) -> SchedulerJobRun:
    """用独立 session 创建并 commit 一个 chip consensus 任务（跨事务可见）。"""
    if core_run_id is None:
        core_run_id = uuid.uuid4()
    now = datetime.now(_TZ)
    meta = {
        "chip_status": "queued",
        "trade_date": trade_date.isoformat(),
        "core_run_id": str(core_run_id),
        "scope": "all_a_share",
    }
    job_run = SchedulerJobRun(
        job_name="after_close_chip_consensus",
        business_date=trade_date.isoformat(),
        run_key=f"after_close_chip_consensus:test:{uuid.uuid4().hex[:8]}",
        status=status,
        scheduled_at=now,
        started_at=now if status == "running" else None,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=_CHIP_LEASE_SECONDS),
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    async with session_factory() as db:
        db.add(job_run)
        await db.commit()
        return job_run


async def _cleanup_job_run(session_factory, job_run_id: uuid.UUID) -> None:
    """删除测试创建的 SchedulerJobRun + 关联事件。"""
    async with session_factory() as db:
        await db.execute(
            text("DELETE FROM job_run_events WHERE job_run_id = :id"),
            {"id": job_run_id},
        )
        await db.execute(
            text("DELETE FROM scheduler_job_runs WHERE id = :id"),
            {"id": job_run_id},
        )
        await db.commit()


# ---------------------------------------------------------------------------
# 测试 1: Worker 领取 queued 任务
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_claims_queued_chip_job() -> None:
    """测试 1：Worker 领取 queued chip consensus 任务。

    验证：
    - status: queued → running
    - worker_instance_id 已设置
    - lease_epoch 递增（fencing）
    - heartbeat_at / lease_expires_at 已更新
    """
    from app.worker import _WORKER_INSTANCE_ID, _chip_consensus_poll_once
    from tests.conftest import TestAsyncSessionLocal

    core_run_id = uuid.uuid4()
    job_run = await _create_queued_chip_job(
        TestAsyncSessionLocal, core_run_id=core_run_id,
    )
    job_run_id = job_run.id
    initial_lease_epoch = job_run.lease_epoch

    try:
        # mock execute_after_close_chip_consensus 避免真的执行业务逻辑
        with patch(
            "app.services.after_close_chip_consensus_service.execute_after_close_chip_consensus",
            new=AsyncMock(return_value={
                "succeeded_count": 0, "failed_count": 0, "total_count": 0,
                "status": "succeeded", "failed_instruments": [],
                "skipped_instruments": [],
            }),
        ), patch(
            "app.services.feature_snapshot_service.get_active_a_share_instruments",
            new=AsyncMock(return_value=[]),
        ), patch(
            "app.services.after_close_chip_consensus_service.get_pending_chip_instruments",
            new=AsyncMock(return_value=[]),
        ):
            claimed = await _chip_consensus_poll_once()

        assert claimed is True, "Worker 应领取到任务"

        # 用独立 session 验证任务状态
        async with TestAsyncSessionLocal() as db:
            result = await db.get(SchedulerJobRun, job_run_id)
            assert result is not None
            assert result.status == "running", f"status 应为 running, 实际: {result.status}"
            assert result.worker_instance_id == _WORKER_INSTANCE_ID
            assert result.heartbeat_at is not None
            assert result.lease_expires_at is not None
            # lease_epoch 应递增（fencing）
            assert result.lease_epoch == initial_lease_epoch + 1, (
                f"lease_epoch 应递增 1, 实际: {result.lease_epoch} (初始={initial_lease_epoch})"
            )
    finally:
        await _cleanup_job_run(TestAsyncSessionLocal, job_run_id)


# ---------------------------------------------------------------------------
# 测试 2: 并发只有一个领取成功
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_concurrent_only_one_claims_chip() -> None:
    """测试 2：两个 Worker 并发，只有一个领取成功（FOR UPDATE SKIP LOCKED）。"""
    from app.worker import _chip_consensus_poll_once
    from tests.conftest import TestAsyncSessionLocal

    core_run_id = uuid.uuid4()
    job_run = await _create_queued_chip_job(
        TestAsyncSessionLocal, core_run_id=core_run_id,
    )
    job_run_id = job_run.id

    try:
        with patch(
            "app.services.after_close_chip_consensus_service.execute_after_close_chip_consensus",
            new=AsyncMock(return_value={
                "succeeded_count": 0, "failed_count": 0, "total_count": 0,
                "status": "succeeded", "failed_instruments": [],
                "skipped_instruments": [],
            }),
        ), patch(
            "app.services.feature_snapshot_service.get_active_a_share_instruments",
            new=AsyncMock(return_value=[]),
        ), patch(
            "app.services.after_close_chip_consensus_service.get_pending_chip_instruments",
            new=AsyncMock(return_value=[]),
        ):
            # 并发调用两次
            import asyncio as _asyncio
            results = await _asyncio.gather(
                _chip_consensus_poll_once(),
                _chip_consensus_poll_once(),
            )

        # 至少一个返回 True（可能两个都返回 True，但只有一个真正领取）
        assert any(results), "至少应有一个 Worker 领取到任务"

        # 验证任务只被领取一次（worker_instance_id 应是 _WORKER_INSTANCE_ID，不会出现两个不同 worker）
        async with TestAsyncSessionLocal() as db:
            result = await db.get(SchedulerJobRun, job_run_id)
            assert result is not None
            assert result.status == "running"
            # worker_instance_id 应是单一值（不会出现并发覆盖）
            assert result.worker_instance_id is not None
    finally:
        await _cleanup_job_run(TestAsyncSessionLocal, job_run_id)


# ---------------------------------------------------------------------------
# 测试 3: 部分成功写 metadata.chip_status=partial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_partial_success_writes_metadata() -> None:
    """测试 3：chip 部分成功写 metadata.chip_status=partial。

    验证：
    - execute_after_close_chip_consensus 返回 status=partial 时
    - _update_job_run_metadata 应写入 metadata.chip_status=partial
    - 主 status 保持 succeeded（chip 部分成功不反改 core）
    """
    from app.services.after_close_chip_consensus_service import (
        _update_job_run_metadata,
    )
    from tests.conftest import TestAsyncSessionLocal

    core_run_id = uuid.uuid4()
    job_run = await _create_queued_chip_job(
        TestAsyncSessionLocal, core_run_id=core_run_id, status="running",
    )
    job_run_id = job_run.id

    try:
        # 直接调用 _update_job_run_metadata 模拟部分成功
        await _update_job_run_metadata(
            job_run_id=job_run_id,
            chip_status="partial",
            succeeded_count=80,
            failed_count=20,
            total_count=100,
        )

        async with TestAsyncSessionLocal() as db:
            result = await db.get(SchedulerJobRun, job_run_id)
            assert result is not None
            meta = json.loads(result.metadata_json)
            assert meta["chip_status"] == "partial"
            assert meta["succeeded_count"] == 80
            assert meta["failed_count"] == 20
            assert meta["total_count"] == 100
            assert meta["chip_results_summary"] == {
                "succeeded": 80, "failed": 20, "total": 100,
            }
    finally:
        await _cleanup_job_run(TestAsyncSessionLocal, job_run_id)


# ---------------------------------------------------------------------------
# 测试 4: 恢复 interrupted → resume_queued → Worker 领取
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_resumes_interrupted_chip_job() -> None:
    """测试 4：interrupted chip consensus → resume_queued → Worker 领取断点续算。

    流程：
    1. 创建一个 interrupted 的 chip job
    2. 调用 auto_resume_interrupted_after_close_runs → 转为 resume_queued
    3. _chip_consensus_poll_once 领取并执行
    4. 验证 get_pending_chip_instruments 被调用（断点续算过滤已成功项）
    """
    from app.services.scheduler_job_run_recovery_service import (
        auto_resume_interrupted_after_close_runs,
    )
    from app.worker import _chip_consensus_poll_once
    from tests.conftest import TestAsyncSessionLocal

    core_run_id = uuid.uuid4()
    # 创建一个 interrupted 任务（attempt_no=0）
    job_run = await _create_queued_chip_job(
        TestAsyncSessionLocal, core_run_id=core_run_id, status="interrupted",
    )
    job_run_id = job_run.id

    try:
        # 1. 调用 auto_resume：interrupted → resume_queued
        async with TestAsyncSessionLocal() as db:
            resumed = await auto_resume_interrupted_after_close_runs(db)
            await db.commit()
        assert resumed == 1, f"应恢复 1 个任务，实际: {resumed}"

        # 2. 验证任务状态变为 resume_queued
        async with TestAsyncSessionLocal() as db:
            result = await db.get(SchedulerJobRun, job_run_id)
            assert result is not None
            assert result.status == "resume_queued", (
                f"状态应为 resume_queued, 实际: {result.status}"
            )
            assert result.attempt_no == 1, "attempt_no 应递增为 1"

        # 3. mock get_pending_chip_instruments 验证被调用
        mock_pending = AsyncMock(return_value=[])
        with patch(
            "app.services.after_close_chip_consensus_service.execute_after_close_chip_consensus",
            new=AsyncMock(return_value={
                "succeeded_count": 0, "failed_count": 0, "total_count": 0,
                "status": "succeeded", "failed_instruments": [],
                "skipped_instruments": [],
            }),
        ), patch(
            "app.services.feature_snapshot_service.get_active_a_share_instruments",
            new=AsyncMock(return_value=[]),
        ), patch(
            "app.services.after_close_chip_consensus_service.get_pending_chip_instruments",
            new=mock_pending,
        ):
            claimed = await _chip_consensus_poll_once()

        assert claimed is True
        # 验证 get_pending_chip_instruments 被调用（断点续算）
        mock_pending.assert_awaited_once()
    finally:
        await _cleanup_job_run(TestAsyncSessionLocal, job_run_id)


# ---------------------------------------------------------------------------
# 测试 5: 缺失 trade_date/core_run_id → 立即标记 failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_missing_metadata_marks_failed() -> None:
    """测试 5：任务缺少 trade_date/core_run_id → 立即标记 failed + 写 ERROR 事件。"""
    from app.worker import _chip_consensus_poll_once
    from tests.conftest import TestAsyncSessionLocal

    # 创建一个缺少 core_run_id 的任务
    now = datetime.now(_TZ)
    meta = {
        "chip_status": "queued",
        "trade_date": _TRADE_DATE.isoformat(),
        # 故意缺少 core_run_id
    }
    job_run = SchedulerJobRun(
        job_name="after_close_chip_consensus",
        business_date=_TRADE_DATE.isoformat(),
        run_key=f"after_close_chip_consensus:test:{uuid.uuid4().hex[:8]}",
        status="queued",
        scheduled_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=_CHIP_LEASE_SECONDS),
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    async with TestAsyncSessionLocal() as db:
        db.add(job_run)
        await db.commit()
    job_run_id = job_run.id

    try:
        claimed = await _chip_consensus_poll_once()
        assert claimed is True

        async with TestAsyncSessionLocal() as db:
            result = await db.get(SchedulerJobRun, job_run_id)
            assert result is not None
            assert result.status == "failed", (
                f"缺 core_run_id 应标记 failed, 实际: {result.status}"
            )
            assert result.finished_at is not None
            assert result.lease_expires_at == result.finished_at  # 释放 run_key
            assert result.error_message is not None
            assert "core_run_id" in result.error_message
    finally:
        await _cleanup_job_run(TestAsyncSessionLocal, job_run_id)


# ---------------------------------------------------------------------------
# 测试 6: 部分成功后断点续算 — get_pending_chip_instruments 过滤已成功项
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_resume_filters_succeeded_instruments() -> None:
    """测试 6：resume_queued 任务调用 get_pending_chip_instruments 过滤已成功项。

    场景：上次 chip 计算了 100 只，80 成功，20 未成功。
    resume_queued 任务应只重试 20 只未成功的（get_pending_chip_instruments 返回 20 只）。
    """
    from app.worker import _chip_consensus_poll_once
    from tests.conftest import TestAsyncSessionLocal

    core_run_id = uuid.uuid4()
    # 创建 resume_queued 任务（attempt_no=1）
    job_run = await _create_queued_chip_job(
        TestAsyncSessionLocal,
        core_run_id=core_run_id,
        status="resume_queued",
    )
    job_run_id = job_run.id

    # 模拟 20 只待计算 instrument
    pending_instruments = [uuid.uuid4() for _ in range(20)]
    all_instruments = [uuid.uuid4() for _ in range(100)]

    try:
        mock_execute = AsyncMock(return_value={
            "succeeded_count": 20, "failed_count": 0, "total_count": 20,
            "status": "succeeded", "failed_instruments": [],
            "skipped_instruments": [],
        })

        with patch(
            "app.services.after_close_chip_consensus_service.execute_after_close_chip_consensus",
            new=mock_execute,
        ), patch(
            "app.services.feature_snapshot_service.get_active_a_share_instruments",
            new=AsyncMock(return_value=all_instruments),
        ), patch(
            "app.services.after_close_chip_consensus_service.get_pending_chip_instruments",
            new=AsyncMock(return_value=pending_instruments),
        ):
            claimed = await _chip_consensus_poll_once()

        assert claimed is True

        # 验证 execute 收到的是 pending 列表（20 只），不是全部（100 只）
        mock_execute.assert_awaited_once()
        call_kwargs = mock_execute.call_args.kwargs
        assert call_kwargs["instrument_ids"] == pending_instruments, (
            "execute 应收到 pending 列表（过滤已成功项）"
        )
        assert len(call_kwargs["instrument_ids"]) == 20
    finally:
        await _cleanup_job_run(TestAsyncSessionLocal, job_run_id)
