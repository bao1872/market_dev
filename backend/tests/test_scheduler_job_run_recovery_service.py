"""SchedulerJobRun 僵尸任务统一恢复服务测试 - 验证 recover_stale_scheduler_job_runs。

覆盖 5 个场景（spec Phase 3）：
1. 租约未过期且 heartbeat 正常的 running 任务不被恢复
2. 租约过期且 heartbeat 不健康的 running 任务被恢复为 interrupted + 写 recovery 事件
3. 同一任务不重复写 recovery 事件（幂等）
4. after_close_orchestrator 任务恢复时 metadata.orchestrator_status 改为 interrupted
5. heartbeat 超时 90s 但 lease 未过期的 running 任务不被恢复
6. lease 已过期但 heartbeat 健康的长任务不被恢复

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
设计要点：
- 使用固定 test_now 避免时间相关测试 flaky
- metadata_json 是 Text 类型，存 json.dumps(...) 字符串
- 通过传入 now 参数使测试确定性可重现
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models.job_run_event import JobRunEvent
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.scheduler_job_run_recovery_service import (
    recover_replaced_incarnation_runs,
    recover_stale_scheduler_job_runs,
)

_CI_ENV = (
    os.environ.get("GITHUB_ACTIONS", "").lower() in ("1", "true", "yes")
    or os.environ.get("PANJI_CI_DB_TEST", "").lower() in ("1", "true", "yes")
)
pytestmark = pytest.mark.skipif(
    not _CI_ENV,
    reason="scheduler recovery tests require the CI ephemeral PostgreSQL database",
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
        heartbeat_at=test_now - timedelta(seconds=100),
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
        heartbeat_at=test_now - timedelta(seconds=100),
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
async def test_heartbeat_timeout_lease_valid_not_recovered(db_session) -> None:
    """场景 5：heartbeat 超时但 lease 未过期时不越权回收。

    这是生产环境僵尸任务的典型场景：lease 设置较长（如 4h）但 Worker
    Watchdog 只在 lease 与 heartbeat 同时失效时回收，避免和较长租约冲突。
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

    assert recovered == 0
    await db_session.refresh(job_run)
    assert job_run.status == "running"
    assert job_run.error_code is None
    assert job_run.finished_at is None
    assert await _count_recovery_events(db_session, job_run_id) == 0


@pytest.mark.asyncio
async def test_lease_expired_heartbeat_fresh_not_recovered(db_session) -> None:
    """场景 6：lease 到点但 heartbeat 健康时不接管正常长任务。"""
    test_now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=_TZ)
    job_run = await _create_job_run(
        db_session,
        job_name="after_close_chip_consensus",
        status="running",
        lease_expires_at=test_now - timedelta(seconds=1),
        heartbeat_at=test_now - timedelta(seconds=10),
    )
    job_run_id = job_run.id

    recovered = await recover_stale_scheduler_job_runs(db_session, now=test_now)

    assert recovered == 0
    await db_session.refresh(job_run)
    assert job_run.status == "running"
    assert await _count_recovery_events(db_session, job_run_id) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])


# ============================================================================
# [CRASH-RESUME-SLICE / P0-C] incarnation 替换快速恢复行为测试
# 设计原则：仅在「同一 worker slot（hostname:pid）已出现新 incarnation」时，
# 才允许绕过长 lease 立即中断上一代进程遗留的 running 任务；
# 不同 slot 的 worker（即便 heartbeat stale + lease 仍有效）绝不抢占。
# ============================================================================


async def _set_worker_instance_id(
    db_session, job_run_id, instance_id: str
) -> None:
    """直接更新任务占有的 worker 实例标识（绕过 ORM 事件）。"""
    from sqlalchemy import text

    await db_session.execute(
        text("UPDATE scheduler_job_runs SET worker_instance_id = :wid WHERE id = :id"),
        {"wid": instance_id, "id": job_run_id},
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_same_slot_old_incarnation_fast_recover(db_session) -> None:
    """D: 同 slot 旧 incarnation → 新 incarnation，旧 running 任务可快速恢复。

    当前 worker = 6230ac1ea028:1:bbbb（新 incarnation）。
    旧任务 owner = 6230ac1ea028:1:aaaa（同 slot 旧 incarnation），
    heartbeat stale 且 lease 仍有效（4h 未到期）。
    recover_replaced_incarnation_runs 应不等待 lease 立即中断该任务。
    """
    test_now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=_TZ)
    current_worker = "6230ac1ea028:1:bbbb"
    job_run = await _create_job_run(
        db_session,
        job_name="after_close_orchestrator",
        status="running",
        lease_expires_at=test_now + timedelta(hours=3),  # 4h lease 仍有效
        heartbeat_at=test_now - timedelta(minutes=30),     # heartbeat 已 stale
    )
    job_run_id = job_run.id
    await _set_worker_instance_id(db_session, job_run_id, "6230ac1ea028:1:aaaa")

    recovered = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id=current_worker, now=test_now
    )

    assert recovered == 1
    await db_session.refresh(job_run)
    assert job_run.status == "interrupted"
    assert job_run.error_code == "WORKER_INCARNATION_REPLACED"
    assert job_run.finished_at is not None
    assert await _count_recovery_events(db_session, job_run_id) == 1


@pytest.mark.asyncio
async def test_different_slot_stale_lease_valid_no_steal(db_session) -> None:
    """E: 不同 slot 的 worker，即便 heartbeat stale 且 lease 仍有效，也不能抢占。

    当前 worker = 9999ffff:1:cccc（另一个 slot）。
    旧任务 owner = 6230ac1ea028:1（不同 slot），
    heartbeat stale 且 lease 有效。recover_replaced_incarnation_runs 必须不动它。
    """
    test_now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=_TZ)
    current_worker = "9999ffff:1:cccc"
    job_run = await _create_job_run(
        db_session,
        job_name="after_close_orchestrator",
        status="running",
        lease_expires_at=test_now + timedelta(hours=3),
        heartbeat_at=test_now - timedelta(minutes=30),
    )
    job_run_id = job_run.id
    await _set_worker_instance_id(db_session, job_run_id, "6230ac1ea028:1:aaaa")

    recovered = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id=current_worker, now=test_now
    )

    assert recovered == 0
    await db_session.refresh(job_run)
    assert job_run.status == "running"
    assert job_run.error_code is None
    assert job_run.finished_at is None
    assert await _count_recovery_events(db_session, job_run_id) == 0


@pytest.mark.asyncio
async def test_legacy_worker_id_recognized_same_slot(db_session) -> None:
    """F: legacy worker id（hostname:pid）可被新 hostname:pid:nonce 识别为同 slot 前代。

    当前 worker = 6230ac1ea028:1:xxxx（新格式，带 nonce）。
    旧任务 owner = 6230ac1ea028:1（legacy 格式，无 nonce）。
    应识别为同 slot 前代 → 快速恢复（这正是修复当前 8/25 child 的关键）。
    """
    test_now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=_TZ)
    current_worker = "6230ac1ea028:1:xxxx"
    job_run = await _create_job_run(
        db_session,
        job_name="after_close_orchestrator",
        status="running",
        lease_expires_at=test_now + timedelta(hours=3),
        heartbeat_at=test_now - timedelta(minutes=30),
    )
    job_run_id = job_run.id
    await _set_worker_instance_id(db_session, job_run_id, "6230ac1ea028:1")

    recovered = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id=current_worker, now=test_now
    )

    assert recovered == 1
    await db_session.refresh(job_run)
    assert job_run.status == "interrupted"
    assert job_run.error_code == "WORKER_INCARNATION_REPLACED"


@pytest.mark.asyncio
async def test_same_incarnation_not_reinterrupted(db_session) -> None:
    """安全护栏：当前 worker 与 owner 是同一 incarnation（同一进程），绝不重复中断自己。"""
    test_now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=_TZ)
    current_worker = "6230ac1ea028:1:bbbb"
    job_run = await _create_job_run(
        db_session,
        job_name="after_close_orchestrator",
        status="running",
        lease_expires_at=test_now + timedelta(hours=3),
        heartbeat_at=test_now - timedelta(seconds=10),
    )
    job_run_id = job_run.id
    await _set_worker_instance_id(db_session, job_run_id, "6230ac1ea028:1:bbbb")

    recovered = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id=current_worker, now=test_now
    )

    assert recovered == 0
    await db_session.refresh(job_run)
    assert job_run.status == "running"
