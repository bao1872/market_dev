"""SchedulerJobRun 僵尸任务统一恢复服务测试 - 验证 recover_stale_scheduler_job_runs。

覆盖 5 个场景（spec Phase 3）：
1. 租约未过期且 heartbeat 正常的 running 任务不被恢复
2. 租约过期的 running 任务被恢复为 interrupted + 写 recovery 事件
3. 同一任务不重复写 recovery 事件（幂等）
4. after_close_orchestrator 任务恢复时 metadata.orchestrator_status 改为 interrupted
5. heartbeat 超时 90s 但 lease 未过期的 running 任务被恢复（关键边界场景）

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
设计要点：
- 使用固定 test_now 避免时间相关测试 flaky
- metadata_json 是 Text 类型，存 json.dumps(...) 字符串
- 通过传入 now 参数使测试确定性可重现
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models.job_run_event import JobRunEvent
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.scheduler_job_run_recovery_service import (
    recover_stale_scheduler_job_runs,
)

_TZ = ZoneInfo("Asia/Shanghai")


async def _create_job_run(
    db_session,
    *,
    job_name: str = "test_job",
    status: str = "running",
    lease_expires_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
    metadata: dict | None = None,
    run_key: str | None = None,
) -> SchedulerJobRun:
    """创建测试用 SchedulerJobRun（满足外键约束）。

    Args:
        db_session: 异步会话
        job_name: 任务名称
        status: 初始状态
        lease_expires_at: 租约过期时间
        heartbeat_at: 心跳时间
        metadata: 元数据 dict（将 json.dumps 到 metadata_json）
        run_key: 幂等键（默认随机生成）

    Returns:
        已 flush 的 SchedulerJobRun
    """
    job_run = SchedulerJobRun(
        job_name=job_name,
        business_date="2026-06-25",
        run_key=run_key or f"{job_name}:{uuid.uuid4().hex[:8]}",
        status=status,
        scheduled_at=datetime.now(_TZ),
        started_at=datetime.now(_TZ),
        heartbeat_at=heartbeat_at,
        lease_expires_at=lease_expires_at,
        metadata_json=json.dumps(metadata) if metadata else None,
    )
    db_session.add(job_run)
    await db_session.flush()
    return job_run


async def _count_recovery_events(db_session, job_run_id) -> int:
    """统计指定任务的 recovery 事件数量。"""
    stmt = select(JobRunEvent).where(
        JobRunEvent.job_run_id == job_run_id,
        JobRunEvent.step == "recovery",
    )
    result = await db_session.execute(stmt)
    return len(list(result.scalars().all()))


@pytest.mark.asyncio
async def test_lease_valid_heartbeat_fresh_not_recovered(db_session) -> None:
    """场景 1：租约未过期且 heartbeat 正常的 running 任务不被恢复。"""
    test_now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=_TZ)
    job_run = await _create_job_run(
        db_session,
        job_name="bars_scheduler",
        status="running",
        lease_expires_at=test_now + timedelta(minutes=5),
        heartbeat_at=test_now - timedelta(seconds=10),
    )
    job_run_id = job_run.id

    recovered = await recover_stale_scheduler_job_runs(db_session, now=test_now)

    assert recovered == 0
    await db_session.refresh(job_run)
    assert job_run.status == "running"
    assert job_run.error_code is None
    assert job_run.finished_at is None
    assert await _count_recovery_events(db_session, job_run_id) == 0


@pytest.mark.asyncio
async def test_lease_expired_recovered_to_interrupted(db_session) -> None:
    """场景 2：租约过期的 running 任务被恢复为 interrupted + 写 recovery 事件。"""
    test_now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=_TZ)
    job_run = await _create_job_run(
        db_session,
        job_name="bars_scheduler",
        status="running",
        lease_expires_at=test_now - timedelta(minutes=1),
        heartbeat_at=test_now - timedelta(seconds=10),
    )
    job_run_id = job_run.id

    recovered = await recover_stale_scheduler_job_runs(db_session, now=test_now)

    assert recovered == 1
    await db_session.refresh(job_run)
    assert job_run.status == "interrupted"
    assert job_run.error_code == "STALE_PROCESS_TERMINATED"
    assert job_run.finished_at is not None
    recovery_count = await _count_recovery_events(db_session, job_run_id)
    assert recovery_count == 1

    stmt = select(JobRunEvent).where(
        JobRunEvent.job_run_id == job_run_id,
        JobRunEvent.step == "recovery",
    )
    result = await db_session.execute(stmt)
    event = result.scalars().one()
    assert event.level == "error"
    assert event.payload is not None
    assert event.payload.get("original_status") == "running"
    assert "recovered_at" in event.payload
    assert "last_heartbeat" in event.payload


@pytest.mark.asyncio
async def test_idempotent_no_duplicate_recovery_event(db_session) -> None:
    """场景 3：同一任务不重复写 recovery 事件（幂等）。

    模拟：任务已被恢复（已有 recovery 事件），看门狗再次扫描时
    由于 status 已变为 interrupted，WHERE 不命中，不会再次更新；
    但即使有并发场景命中，事件插入也会因先 SELECT 判断而保持幂等。
    """
    test_now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=_TZ)
    # 创建一个已恢复（interrupted）的任务，并预写一条 recovery 事件
    job_run = await _create_job_run(
        db_session,
        job_name="bars_scheduler",
        status="interrupted",
        lease_expires_at=test_now - timedelta(minutes=1),
        heartbeat_at=test_now - timedelta(seconds=10),
    )
    job_run_id = job_run.id

    pre_event = JobRunEvent(
        job_run_id=job_run_id,
        step="recovery",
        level="error",
        message="预写入的恢复事件",
        payload={"original_status": "running", "recovered_at": test_now.isoformat()},
    )
    db_session.add(pre_event)
    await db_session.flush()

    recovered = await recover_stale_scheduler_job_runs(db_session, now=test_now)

    assert recovered == 0
    assert await _count_recovery_events(db_session, job_run_id) == 1


@pytest.mark.asyncio
async def test_after_close_orchestrator_metadata_updated(db_session) -> None:
    """场景 4：after_close_orchestrator 任务恢复时 metadata.orchestrator_status 改为 interrupted。"""
    test_now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=_TZ)
    job_run = await _create_job_run(
        db_session,
        job_name="after_close_orchestrator",
        status="running",
        lease_expires_at=test_now - timedelta(minutes=1),
        heartbeat_at=test_now - timedelta(seconds=10),
        metadata={"orchestrator_status": "refreshing_daily", "trade_date": "2026-06-25"},
    )
    job_run_id = job_run.id

    recovered = await recover_stale_scheduler_job_runs(db_session, now=test_now)

    assert recovered == 1
    await db_session.refresh(job_run)
    assert job_run.status == "interrupted"
    assert job_run.metadata_json is not None
    parsed = json.loads(job_run.metadata_json)
    assert parsed["orchestrator_status"] == "interrupted"
    assert parsed["trade_date"] == "2026-06-25"
    assert await _count_recovery_events(db_session, job_run_id) == 1


@pytest.mark.asyncio
async def test_heartbeat_timeout_lease_valid_recovered(db_session) -> None:
    """场景 5：heartbeat 超时 90s 但 lease 未过期的 running 任务被恢复（关键边界场景）。

    这是生产环境僵尸任务的典型场景：lease 设置较长（如 4h）但 Worker
    被 SIGKILL 后 heartbeat 停止更新，90s 后即应被识别为僵尸并恢复。
    """
    test_now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=_TZ)
    job_run = await _create_job_run(
        db_session,
        job_name="after_close_orchestrator",
        status="running",
        lease_expires_at=test_now + timedelta(hours=3),
        heartbeat_at=test_now - timedelta(seconds=100),
    )
    job_run_id = job_run.id

    recovered = await recover_stale_scheduler_job_runs(db_session, now=test_now)

    assert recovered == 1
    await db_session.refresh(job_run)
    assert job_run.status == "interrupted"
    assert job_run.error_code == "STALE_PROCESS_TERMINATED"
    assert job_run.finished_at is not None
    assert await _count_recovery_events(db_session, job_run_id) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
