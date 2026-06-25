"""盘后编排服务测试 - 验证 create/execute/get_status/retry 的事件写入。

覆盖：
- create_after_close_run 创建任务并写入 queued 事件
- get_after_close_run_status 返回编排状态 + 事件时间线
- retry_after_close_run 重置 failed 任务并写入事件
- execute_after_close_run 成功路径写入各步骤事件
- execute_after_close_run 失败路径写入 failed 事件

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
注意：
- create/retry 内部调用 db.commit()，测试中用 patch.object 替换为 flush（不破坏 nested 事务）
- execute 使用独立 AsyncSessionLocal，测试中 mock 为返回 db_session 的假 context manager
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy_run import StrategyRun
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    create_after_close_run,
    execute_after_close_run,
    get_after_close_run_status,
    retry_after_close_run,
)
from app.services.bars_scheduler_service import BarsSchedulerService, BatchResult
from app.services.job_run_event_service import append_event
from app.services.strategy_batch_service import StrategyBatchService


async def _create_after_close_job_run(
    db_session,
    *,
    status: str = "running",
    orchestrator_status: str = "queued",
    trade_date: date = date(2026, 6, 25),
    dsa_run_id: uuid.UUID | None = None,
) -> SchedulerJobRun:
    """直接创建测试用 after_close SchedulerJobRun（不经过 create_after_close_run）。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    meta = {
        "orchestrator_status": orchestrator_status,
        "trade_date": trade_date.isoformat(),
    }
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
        lease_expires_at=now,
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    db_session.add(job_run)
    await db_session.flush()
    return job_run


async def _create_dsa_strategy_run(
    db_session,
    *,
    status: str = "completed",
    trade_date: date = date(2026, 6, 25),
) -> tuple[StrategyRun, uuid.UUID]:
    """创建测试用 DSA StrategyRun（满足 orchestrator 查询）。

    需要先创建 StrategyDefinition + StrategyVersion 满足外键约束。
    """
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
        released_at=datetime.now(ZoneInfo("Asia/Shanghai")),
    )
    db_session.add(version)
    await db_session.flush()

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
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


@pytest.mark.asyncio
async def test_create_after_close_run_writes_queued_event(db_session) -> None:
    """测试 1：create_after_close_run 创建任务并写入 queued 事件。

    mock acquire_job_run_lock 避免真实 DB 锁；
    mock db.commit 替换为 flush 避免破坏 fixture nested 事务。
    """
    trade_date = date(2026, 6, 25)
    fake_job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=trade_date.isoformat(),
        run_key=f"after_close_orchestrator:{trade_date.isoformat()}",
        status="running",
        scheduled_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        heartbeat_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        lease_expires_at=datetime.now(ZoneInfo("Asia/Shanghai")),
    )
    db_session.add(fake_job_run)
    await db_session.flush()

    async def _fake_acquire(db, **kwargs):
        return fake_job_run

    with patch(
        "app.services.after_close_orchestrator.acquire_job_run_lock",
        new=_fake_acquire,
    ), patch.object(db_session, "commit", new=db_session.flush):
        result, is_new = await create_after_close_run(db=db_session, trade_date=trade_date)

    assert is_new is True
    assert result.id == fake_job_run.id
    assert result.status == "running"

    # 验证 metadata_json 含 orchestrator_status=queued
    meta = json.loads(result.metadata_json)
    assert meta["orchestrator_status"] == AfterCloseRunStatus.QUEUED.value
    assert meta["trade_date"] == "2026-06-25"

    # 验证事件写入
    from app.services.job_run_event_service import list_events
    events = await list_events(db_session, result.id, limit=10)
    assert len(events) >= 1
    queued_events = [e for e in events if e.step == AfterCloseRunStatus.QUEUED.value]
    assert len(queued_events) >= 1
    assert queued_events[0].level == "info"
    assert "盘后编排" in queued_events[0].message


@pytest.mark.asyncio
async def test_create_after_close_run_returns_existing_on_duplicate(db_session) -> None:
    """测试 1.1：create_after_close_run 在 acquire_job_run_lock 返回 None 时返回已有任务 + is_new=False。

    模拟同日已有运行中任务的幂等场景：acquire 返回 None → 函数应查询已有记录返回 (existing, False)。
    """
    trade_date = date(2026, 6, 25)
    existing_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=trade_date.isoformat(),
        run_key=f"after_close_orchestrator:{trade_date.isoformat()}",
        status="running",
        scheduled_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        heartbeat_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        lease_expires_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        metadata_json=json.dumps({"orchestrator_status": "refreshing_daily"}),
    )
    db_session.add(existing_run)
    await db_session.flush()

    async def _fake_acquire_returns_none(db, **kwargs):
        return None

    with patch(
        "app.services.after_close_orchestrator.acquire_job_run_lock",
        new=_fake_acquire_returns_none,
    ):
        result, is_new = await create_after_close_run(db=db_session, trade_date=trade_date)

    assert is_new is False
    assert result.id == existing_run.id
    assert result.status == "running"


@pytest.mark.asyncio
async def test_get_after_close_run_status_returns_events(db_session) -> None:
    """测试 2：get_after_close_run_status 返回编排状态 + 事件时间线。

    直接创建 job_run + 写入事件，验证查询结果。
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(
        db_session,
        orchestrator_status=AfterCloseRunStatus.WAITING_DSA_WORKER.value,
        dsa_run_id=dsa_run.id,
    )

    # 写入多条事件（设置递增 created_at 确保倒序可预测）
    e1 = await append_event(
        db=db_session, job_run_id=job_run.id,
        step=AfterCloseRunStatus.QUEUED.value,
        message="盘后编排已创建",
    )
    await db_session.flush()
    e1.created_at = datetime(2026, 6, 25, 16, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    e2 = await append_event(
        db=db_session, job_run_id=job_run.id,
        step=AfterCloseRunStatus.REFRESHING_DAILY.value,
        message="开始刷新日线",
    )
    await db_session.flush()
    e2.created_at = datetime(2026, 6, 25, 17, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    e3 = await append_event(
        db=db_session, job_run_id=job_run.id,
        step=AfterCloseRunStatus.WAITING_DSA_WORKER.value,
        message=f"等待 DSA: dsa_run_id={dsa_run.id}",
        payload={"dsa_run_id": str(dsa_run.id)},
    )
    await db_session.flush()
    e3.created_at = datetime(2026, 6, 25, 18, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    await db_session.flush()

    result = await get_after_close_run_status(db=db_session, job_run_id=job_run.id)

    assert result["job_run_id"] == str(job_run.id)
    assert result["status"] == "running"
    assert result["orchestrator_status"] == AfterCloseRunStatus.WAITING_DSA_WORKER.value
    assert result["trade_date"] == "2026-06-25"
    assert result["dsa_run_id"] == str(dsa_run.id)
    assert result["dsa_run_status"] == "completed"
    assert len(result["events"]) == 3
    # 倒序：最新事件在前（18:00 -> 17:00 -> 16:00）
    assert result["events"][0]["step"] == AfterCloseRunStatus.WAITING_DSA_WORKER.value
    assert result["events"][1]["step"] == AfterCloseRunStatus.REFRESHING_DAILY.value
    assert result["events"][2]["step"] == AfterCloseRunStatus.QUEUED.value


@pytest.mark.asyncio
async def test_retry_after_close_run_writes_event(db_session) -> None:
    """测试 3：retry_after_close_run 重置 failed 任务并写入 queued 事件。

    mock db.commit 替换为 flush 避免破坏 fixture nested 事务。
    """
    job_run = await _create_after_close_job_run(
        db_session,
        status="failed",
        orchestrator_status=AfterCloseRunStatus.FAILED.value,
    )
    job_run.error_message = "模拟失败"
    await db_session.flush()

    with patch.object(db_session, "commit", new=db_session.flush):
        result = await retry_after_close_run(db=db_session, job_run_id=job_run.id)

    assert result.status == "running"
    assert result.error_message is None
    assert result.finished_at is None

    meta = json.loads(result.metadata_json)
    assert meta["orchestrator_status"] == AfterCloseRunStatus.QUEUED.value

    from app.services.job_run_event_service import list_events
    events = await list_events(db_session, job_run.id, limit=10)
    queued_events = [e for e in events if e.step == AfterCloseRunStatus.QUEUED.value]
    assert len(queued_events) >= 1
    assert "重试" in queued_events[-1].message


@pytest.mark.asyncio
async def test_execute_writes_status_events(db_session) -> None:
    """测试 4：execute_after_close_run 成功路径写入各步骤事件。

    mock AsyncSessionLocal 返回 db_session（不真正关闭）；
    mock BarsSchedulerService.refresh_all_instruments 返回含 dsa_run_id 的 BatchResult；
    mock _poll_dsa_run_status 返回 completed；
    mock StrategyBatchService._check_quality_gates 返回 True；
    mock StrategyBatchService.publish_run 返回 mock run。
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(db_session)

    # 构造 mock AsyncSessionLocal（async with 返回 db_session，不关闭）
    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    # mock refresh_all_instruments 返回含 dsa_run_id 的 BatchResult
    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    # mock publish_run 返回的对象
    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

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
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 验证事件序列：应包含 refreshing_daily → waiting_dsa_worker → quality_gate → publishing → succeeded
    from app.services.job_run_event_service import list_events
    events = await list_events(db_session, job_run.id, limit=20)
    steps = [e.step for e in events]

    assert AfterCloseRunStatus.REFRESHING_DAILY.value in steps, f"缺少 refreshing_daily 事件: {steps}"
    assert AfterCloseRunStatus.WAITING_DSA_WORKER.value in steps, f"缺少 waiting_dsa_worker 事件: {steps}"
    assert AfterCloseRunStatus.QUALITY_GATE.value in steps, f"缺少 quality_gate 事件: {steps}"
    assert AfterCloseRunStatus.PUBLISHING.value in steps, f"缺少 publishing 事件: {steps}"
    assert AfterCloseRunStatus.SUCCEEDED.value in steps, f"缺少 succeeded 事件: {steps}"

    # [AfterClose] - 不断言事件顺序：同一事务内 created_at 可能相同，
    # list_events 的倒序仅保证 created_at 不同时正确排序。
    # 验证 job_run 状态更新为 succeeded（最终状态）
    assert job_run.status == "succeeded"
    assert job_run.finished_at is not None


@pytest.mark.asyncio
async def test_execute_failure_writes_failed_event(db_session) -> None:
    """测试 5：execute_after_close_run 失败路径写入 failed 事件。

    mock refresh_all_instruments 抛异常，验证 failed 事件写入 + job_run.status=failed。
    """
    job_run = await _create_after_close_job_run(db_session)

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    exc = RuntimeError("pytdx 连接超时（模拟）")

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=AsyncMock(side_effect=lambda model, id: job_run if id == job_run.id else None),
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(side_effect=exc),
    ):
        with pytest.raises(RuntimeError, match="pytdx 连接超时"):
            await execute_after_close_run(
                job_run_id=job_run.id,
                trade_date=date(2026, 6, 25),
                dsa_poll_interval=0,
                dsa_poll_timeout=1,
            )

    # 验证 failed 事件写入
    from app.services.job_run_event_service import list_events
    events = await list_events(db_session, job_run.id, limit=20)
    failed_events = [e for e in events if e.step == AfterCloseRunStatus.FAILED.value]
    assert len(failed_events) >= 1, f"缺少 failed 事件: {[e.step for e in events]}"
    assert failed_events[0].level == "error"
    assert "pytdx 连接超时" in failed_events[0].message
    assert failed_events[0].payload["error_type"] == "RuntimeError"
    assert "traceback" in failed_events[0].payload

    # 验证 job_run 状态更新为 failed
    assert job_run.status == "failed"
    assert job_run.error_message is not None
    assert "pytdx" in job_run.error_message
    assert job_run.finished_at is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
