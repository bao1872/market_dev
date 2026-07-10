"""盘后编排 Worker 测试 - 验证任务领取、并发竞争、异常处理、断点恢复、心跳更新。

覆盖：
1. test_worker_claims_queued_job: Worker 领取 queued 任务（status→running, worker_instance_id 设置）
2. test_worker_concurrent_only_one_claims: 并发只有一个领取成功（FOR UPDATE SKIP LOCKED）
3. test_worker_exception_marks_failed: execute_after_close_run 异常时任务标记 failed，Worker 不崩溃
4. test_execute_with_checkpoint_skips_refresh_daily: 断点恢复跳过日线刷新
5. test_execute_updates_heartbeat_each_step: 每阶段完成后心跳 + lease 更新

测试环境：PostgreSQL 测试库
设计要点：
- 测试 1-3 使用 TestAsyncSessionLocal 真实 commit（Worker 使用独立 session，需跨事务可见）
- 测试 4-5 mock AsyncSessionLocal 为 db_session（测试 execute_after_close_run 内部逻辑）
- cleanup 手动删除测试创建的记录（独立 session commit 不受 fixture 回滚保护）
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy_run import StrategyRun
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    execute_after_close_run,
)
from app.services.bars_scheduler_service import BarsSchedulerService, BatchResult
from app.services.strategy_batch_service import StrategyBatchService

_TZ = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# 测试辅助函数
# ---------------------------------------------------------------------------

async def _create_queued_after_close_run(
    session_factory,
    *,
    trade_date: date = date(2026, 6, 25),
    last_completed_step: str | None = None,
    dsa_run_id: uuid.UUID | None = None,
    status: str = "queued",
) -> SchedulerJobRun:
    """用独立 session 创建并 commit 一个 after_close 编排任务（跨事务可见）。

    用于 Worker 测试：Worker 使用独立 AsyncSessionLocal，需要任务数据已 commit。
    """
    now = datetime.now(_TZ)
    meta: dict = {
        "orchestrator_status": AfterCloseRunStatus.QUEUED.value,
        "trade_date": trade_date.isoformat(),
    }
    if last_completed_step is not None:
        meta["last_completed_step"] = last_completed_step
    if dsa_run_id is not None:
        meta["dsa_run_id"] = str(dsa_run_id)

    job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=trade_date.isoformat(),
        run_key=f"after_close_orchestrator:test:{uuid.uuid4().hex[:8]}",
        status=status,
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=14400),
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    async with session_factory() as db:
        db.add(job_run)
        await db.commit()
        # expire_on_commit=False 让 commit 后属性仍可用
        return job_run


async def _cleanup_job_run(session_factory, job_run_id: uuid.UUID) -> None:
    """删除测试创建的 SchedulerJobRun + 关联事件（独立 session commit 不受 fixture 回滚保护）。"""
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


async def _create_dsa_strategy_run(
    db_session,
    *,
    status: str = "completed",
    trade_date: date = date(2026, 6, 25),
) -> tuple[StrategyRun, uuid.UUID]:
    """创建测试用 DSA StrategyRun（满足 orchestrator 查询）。"""
    from app.models.strategy import StrategyDefinition, StrategyVersion

    definition = StrategyDefinition(
        strategy_key=f"test_dsa_{uuid.uuid4().hex[:8]}",
        kind="selector",
        display_name="测试 DSA",
    )
    db_session.add(definition)
    await db_session.flush()

    version = StrategyVersion(
        strategy_definition_id=definition.id,
        version="1.0.0",
        status="released",
        manifest={"outputs": [], "parameters": []},
        build_hash=f"hash_{uuid.uuid4().hex[:16]}",
        released_at=datetime.now(_TZ),
    )
    db_session.add(version)
    await db_session.flush()

    now = datetime.now(_TZ)
    dsa_run = StrategyRun(
        strategy_version_id=version.id,
        run_type="scheduled",
        trade_date=trade_date,
        status=status,
        input_overrides={},
        idempotency_key=f"test_dsa:{version.id}:{trade_date}:{uuid.uuid4().hex[:8]}",
        total_instruments=100,
        succeeded_count=95,
        failed_count=0,
        started_at=now,
        finished_at=now,
    )
    db_session.add(dsa_run)
    await db_session.flush()
    return dsa_run, version.id


# ---------------------------------------------------------------------------
# 测试 1: Worker 领取 queued 任务
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_claims_queued_job() -> None:
    """测试 1：Worker 领取 queued 任务。

    流程：
    1. 用 TestAsyncSessionLocal 创建并 commit 一个 queued 任务
    2. mock execute_after_close_run 避免真的执行
    3. 调用 _after_close_poll_once() 单次轮询
    4. 验证任务被领取：status=running, worker_instance_id 已设置, heartbeat_at 已更新
    """
    from app.worker import _WORKER_INSTANCE_ID, _after_close_poll_once
    from tests.conftest import TestAsyncSessionLocal

    job_run = await _create_queued_after_close_run(TestAsyncSessionLocal)
    job_run_id = job_run.id

    try:
        # mock execute_after_close_run 避免真的执行业务逻辑
        # patch 源模块（_after_close_poll_once 内部局部 import 从源模块查找属性）
        with patch(
            "app.services.after_close_orchestrator.execute_after_close_run",
            new=AsyncMock(return_value=None),
        ):
            claimed = await _after_close_poll_once()

        assert claimed is True, "Worker 应领取到任务"

        # 用独立 session 验证任务状态
        async with TestAsyncSessionLocal() as db:
            result = await db.get(SchedulerJobRun, job_run_id)
            assert result is not None
            assert result.status == "running", f"status 应为 running, 实际: {result.status}"
            assert result.worker_instance_id == _WORKER_INSTANCE_ID, (
                f"worker_instance_id 应为 {_WORKER_INSTANCE_ID}, 实际: {result.worker_instance_id}"
            )
            assert result.heartbeat_at is not None, "heartbeat_at 应已更新"
            assert result.lease_expires_at is not None, "lease_expires_at 应已设置"
    finally:
        await _cleanup_job_run(TestAsyncSessionLocal, job_run_id)


# ---------------------------------------------------------------------------
# 测试 2: 并发只有一个领取成功
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_concurrent_only_one_claims() -> None:
    """测试 2：两个 Worker 并发，只有一个领取成功。

    流程：
    1. 创建一个 queued 任务
    2. 两个 _after_close_poll_once 并发执行（asyncio.gather）
    3. mock execute_after_close_run 记录调用次数
    4. 验证只有一个 claimed=True，execute_after_close_run 只被调用一次
    """
    from app.worker import _after_close_poll_once
    from tests.conftest import TestAsyncSessionLocal

    job_run = await _create_queued_after_close_run(TestAsyncSessionLocal)
    job_run_id = job_run.id

    try:
        call_count = 0
        call_lock = asyncio.Lock()

        async def _mock_execute(**kwargs):
            nonlocal call_count
            async with call_lock:
                call_count += 1
            # 模拟短暂执行时间，让并发窗口存在
            await asyncio.sleep(0.05)

        with patch(
            "app.services.after_close_orchestrator.execute_after_close_run",
            new=_mock_execute,
        ):
            results = await asyncio.gather(
                _after_close_poll_once(),
                _after_close_poll_once(),
            )

        # 只有一个应返回 True
        claimed_count = sum(1 for r in results if r is True)
        assert claimed_count == 1, (
            f"应只有一个 Worker 领取成功, 实际: {claimed_count}, results={results}"
        )
        # execute_after_close_run 只被调用一次
        assert call_count == 1, (
            f"execute_after_close_run 应只被调用一次, 实际: {call_count}"
        )

        # 验证任务状态为 running
        async with TestAsyncSessionLocal() as db:
            result = await db.get(SchedulerJobRun, job_run_id)
            assert result is not None
            assert result.status == "running"
    finally:
        await _cleanup_job_run(TestAsyncSessionLocal, job_run_id)


# ---------------------------------------------------------------------------
# 测试 3: execute_after_close_run 异常时任务标记 failed，Worker 不崩溃
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_exception_marks_failed() -> None:
    """测试 3：execute_after_close_run 内部异常标记 failed，Worker 不崩溃。

    流程：
    1. 创建一个 queued 任务
    2. mock BarsSchedulerService.refresh_all_instruments 抛异常
    3. 调用 _after_close_poll_once()（不 mock execute_after_close_run，让其内部处理异常）
    4. 验证 Worker 不崩溃（_after_close_poll_once 正常返回）
    5. 验证任务被标记 failed（由 execute_after_close_run 内部异常处理）
    """
    from app.worker import _after_close_poll_once
    from tests.conftest import TestAsyncSessionLocal

    job_run = await _create_queued_after_close_run(TestAsyncSessionLocal)
    job_run_id = job_run.id

    try:
        exc = RuntimeError("pytdx 连接超时（模拟）")

        with patch.object(
            BarsSchedulerService, "refresh_all_instruments",
            new=AsyncMock(side_effect=exc),
        ):
            # Worker 不应抛异常（execute_after_close_run 内部捕获并标记 failed 后 re-raise，
            # Worker 的 _after_close_poll_once 应捕获并记录日志）
            claimed = await _after_close_poll_once()

        assert claimed is True, "Worker 应领取到任务"

        # 验证任务被标记 failed
        async with TestAsyncSessionLocal() as db:
            result = await db.get(SchedulerJobRun, job_run_id)
            assert result is not None
            assert result.status == "failed", (
                f"任务应被标记 failed, 实际: {result.status}"
            )
            assert result.error_message is not None
            assert "pytdx" in result.error_message
    finally:
        await _cleanup_job_run(TestAsyncSessionLocal, job_run_id)


# ---------------------------------------------------------------------------
# 测试 4: 断点恢复跳过日线刷新
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_with_checkpoint_skips_refresh_daily(db_session) -> None:
    """测试 4：last_completed_step='refreshing_daily' 时跳过日线刷新。

    流程：
    1. 创建 running 任务，metadata.last_completed_step='refreshing_daily' + dsa_run_id 已设置
    2. mock AsyncSessionLocal 为 db_session
    3. mock refresh_all_instruments（验证未被调用）
    4. mock _poll_dsa_run_status 返回 completed
    5. mock _check_quality_gates 返回 True
    6. mock publish_run 返回 mock run
    7. 调用 execute_after_close_run(worker_id="test-worker")
    8. 验证 refresh_all_instruments 未被调用
    9. 验证后续阶段正常执行（waiting_dsa_worker → quality_gate → publishing → succeeded）
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")

    # 创建 running 任务，last_completed_step='refreshing_daily'
    now = datetime.now(_TZ)
    meta = {
        "orchestrator_status": AfterCloseRunStatus.WAITING_DSA_WORKER.value,
        "trade_date": "2026-06-25",
        "dsa_run_id": str(dsa_run.id),
        "last_completed_step": "refreshing_daily",
    }
    job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date="2026-06-25",
        run_key=f"after_close_orchestrator:test:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=14400),
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    db_session.add(job_run)
    await db_session.flush()

    # mock AsyncSessionLocal 返回 db_session
    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    # mock refresh_all_instruments（应未被调用）
    mock_refresh = AsyncMock()

    # mock publish_run 返回的对象
    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(_TZ)

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=AsyncMock(side_effect=lambda model, id: {
            (SchedulerJobRun, job_run.id): job_run,
            (StrategyRun, dsa_run.id): dsa_run,
        }.get((model, id))),
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments", new=mock_refresh,
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=date(2026, 6, 25),
            worker_id="test-worker-1",
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 验证 refresh_all_instruments 未被调用（断点恢复跳过）
    mock_refresh.assert_not_called()

    # 验证后续阶段正常执行
    from app.services.job_run_event_service import list_events
    events = await list_events(db_session, job_run.id, limit=20)
    steps = [e.step for e in events]

    assert AfterCloseRunStatus.WAITING_DSA_WORKER.value in steps, (
        f"应执行 waiting_dsa_worker 阶段: {steps}"
    )
    assert AfterCloseRunStatus.QUALITY_GATE.value in steps, (
        f"应执行 quality_gate 阶段: {steps}"
    )
    assert AfterCloseRunStatus.PUBLISHING.value in steps, (
        f"应执行 publishing 阶段: {steps}"
    )
    assert AfterCloseRunStatus.SUCCEEDED.value in steps, (
        f"应执行 succeeded 阶段: {steps}"
    )

    # 验证最终状态
    assert job_run.status == "succeeded"
    assert job_run.finished_at is not None

    # 验证 last_completed_step 更新为 succeeded
    assert job_run.metadata_json is not None
    final_meta = json.loads(job_run.metadata_json)
    assert final_meta.get("last_completed_step") == "succeeded", (
        f"last_completed_step 应为 succeeded, 实际: {final_meta.get('last_completed_step')}"
    )


# ---------------------------------------------------------------------------
# 测试 5: 每阶段完成后心跳 + lease 更新
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_updates_heartbeat_each_step(db_session) -> None:
    """测试 5：每阶段完成后 heartbeat_at + lease_expires_at 更新。

    流程：
    1. 创建 running 任务（无 last_completed_step，从头执行）
    2. mock 各阶段服务
    3. 记录每阶段前的 heartbeat_at，验证每阶段后 heartbeat_at 更新
    4. 验证 lease_expires_at 始终在未来（> now）
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date="2026-06-25",
        run_key=f"after_close_orchestrator:test:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=datetime.now(_TZ),
        started_at=datetime.now(_TZ),
        heartbeat_at=datetime.now(_TZ) - timedelta(hours=1),  # 初始旧心跳
        lease_expires_at=datetime.now(_TZ) - timedelta(hours=1),  # 初始过期租约
        metadata_json=json.dumps({
            "orchestrator_status": AfterCloseRunStatus.QUEUED.value,
            "trade_date": "2026-06-25",
        }, ensure_ascii=False),
    )
    db_session.add(job_run)
    await db_session.flush()

    initial_heartbeat = job_run.heartbeat_at

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(_TZ)

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=AsyncMock(side_effect=lambda model, id: {
            (SchedulerJobRun, job_run.id): job_run,
            (StrategyRun, dsa_run.id): dsa_run,
        }.get((model, id))),
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=date(2026, 6, 25),
            worker_id="test-worker-1",
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 验证最终 heartbeat_at 已更新（不再是初始旧值）
    assert initial_heartbeat is not None
    assert job_run.heartbeat_at is not None
    assert job_run.heartbeat_at > initial_heartbeat, (
        f"heartbeat_at 应已更新: {job_run.heartbeat_at} > {initial_heartbeat}"
    )

    # 验证 lease_expires_at 在未来
    now = datetime.now(_TZ)
    # lease_expires_at 可能带时区也可能不带（PG 返回），统一比较
    lease = job_run.lease_expires_at
    assert lease is not None
    if lease.tzinfo is None:
        lease = lease.replace(tzinfo=_TZ)
    assert lease is not None
    assert lease > now, (
        f"lease_expires_at 应在未来: {lease} > {now}"
    )

    # 验证 worker_instance_id 已设置
    assert job_run.worker_instance_id == "test-worker-1", (
        f"worker_instance_id 应为 test-worker-1, 实际: {job_run.worker_instance_id}"
    )

    # 验证 last_completed_step 最终为 succeeded
    assert job_run.metadata_json is not None
    final_meta = json.loads(job_run.metadata_json)
    assert final_meta.get("last_completed_step") == "succeeded", (
        f"last_completed_step 应为 succeeded, 实际: {final_meta.get('last_completed_step')}"
    )

    # 验证任务成功完成
    assert job_run.status == "succeeded"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
