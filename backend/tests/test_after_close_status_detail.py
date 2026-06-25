"""盘后编排状态详情测试 - [Phase7] 验证 get_after_close_run_status 返回的扩展字段。

覆盖场景：
1. running 任务返回 worker_instance_id / heartbeat_at / lease_expires_at
2. metadata.last_completed_step 正确解析为 last_completed_step 字段
3. failed/interrupted 任务返回 interrupt_reason + is_retryable=true
4. running 任务心跳超时（heartbeat_at < now - 60s）返回 heartbeat_stale=true

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.models.scheduler_job_run import SchedulerJobRun
from app.services.after_close_orchestrator import get_after_close_run_status


async def _create_job_run(
    db_session,
    *,
    status: str = "running",
    orchestrator_status: str = "queued",
    trade_date: date = date(2026, 6, 25),
    worker_instance_id: str | None = None,
    heartbeat_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    last_completed_step: str | None = None,
    extra_meta: dict | None = None,
) -> SchedulerJobRun:
    """[Phase7] - 直接创建测试用 after_close SchedulerJobRun。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    meta: dict = {
        "orchestrator_status": orchestrator_status,
        "trade_date": trade_date.isoformat(),
    }
    if last_completed_step is not None:
        meta["last_completed_step"] = last_completed_step
    if extra_meta:
        meta.update(extra_meta)

    job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=trade_date.isoformat(),
        run_key=f"after_close_orchestrator:test:{uuid.uuid4().hex[:8]}",
        status=status,
        scheduled_at=now,
        started_at=now,
        heartbeat_at=heartbeat_at if heartbeat_at is not None else now,
        lease_expires_at=lease_expires_at if lease_expires_at is not None else now,
        worker_instance_id=worker_instance_id,
        error_code=error_code,
        error_message=error_message,
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    db_session.add(job_run)
    await db_session.flush()
    return job_run


# [Phase7] - 场景 1: running 任务返回 worker/heartbeat/lease
@pytest.mark.asyncio
async def test_status_includes_worker_and_heartbeat(db_session):
    worker_id = "worker-pod-abc123"
    hb = datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(seconds=10)
    lease = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(seconds=3600)

    job_run = await _create_job_run(
        db_session,
        status="running",
        orchestrator_status="refreshing_daily",
        worker_instance_id=worker_id,
        heartbeat_at=hb,
        lease_expires_at=lease,
    )

    result = await get_after_close_run_status(db_session, job_run.id)

    assert result["worker_instance_id"] == worker_id
    # ISO 格式校验（包含时区偏移）
    assert result["heartbeat_at"] is not None
    assert result["heartbeat_at"].startswith(hb.strftime("%Y-%m-%dT"))
    assert result["lease_expires_at"] is not None
    assert result["lease_expires_at"].startswith(lease.strftime("%Y-%m-%dT"))


# [Phase7] - 场景 2: metadata.last_completed_step 正确解析
@pytest.mark.asyncio
async def test_status_includes_last_completed_step(db_session):
    job_run = await _create_job_run(
        db_session,
        status="running",
        orchestrator_status="waiting_dsa_worker",
        last_completed_step="refreshing_daily",
    )

    result = await get_after_close_run_status(db_session, job_run.id)

    assert result["last_completed_step"] == "refreshing_daily"


# [Phase7] - 场景 3a: failed 任务返回 interrupt_reason + is_retryable=true
@pytest.mark.asyncio
async def test_status_failed_includes_interrupt_reason(db_session):
    job_run = await _create_job_run(
        db_session,
        status="failed",
        orchestrator_status="failed",
        error_code="DSA_TIMEOUT",
        error_message="DSA worker 等待超时",
    )

    result = await get_after_close_run_status(db_session, job_run.id)

    assert result["interrupt_reason"] == "DSA_TIMEOUT: DSA worker 等待超时"
    assert result["is_retryable"] is True
    assert result["heartbeat_stale"] is False  # failed 状态不判断心跳


# [Phase7] - 场景 3b: interrupted 任务同样允许重试
@pytest.mark.asyncio
async def test_status_interrupted_is_retryable(db_session):
    job_run = await _create_job_run(
        db_session,
        status="interrupted",
        orchestrator_status="failed",
        error_code="WORKER_LOST",
        error_message="Worker 进程崩溃",
    )

    result = await get_after_close_run_status(db_session, job_run.id)

    assert result["is_retryable"] is True
    assert result["interrupt_reason"] == "WORKER_LOST: Worker 进程崩溃"


# [Phase7] - 场景 3c: succeeded 任务不可重试
@pytest.mark.asyncio
async def test_status_succeeded_not_retryable(db_session):
    job_run = await _create_job_run(
        db_session,
        status="succeeded",
        orchestrator_status="succeeded",
        last_completed_step="succeeded",
    )

    result = await get_after_close_run_status(db_session, job_run.id)

    assert result["is_retryable"] is False
    assert result["interrupt_reason"] is None


# [Phase7] - 场景 4: running + heartbeat_at = now - 120s → heartbeat_stale=true
@pytest.mark.asyncio
async def test_status_running_heartbeat_stale(db_session):
    stale_hb = datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(seconds=120)
    job_run = await _create_job_run(
        db_session,
        status="running",
        orchestrator_status="refreshing_daily",
        heartbeat_at=stale_hb,
    )

    result = await get_after_close_run_status(db_session, job_run.id)

    assert result["heartbeat_stale"] is True


# [Phase7] - 场景 5: running + heartbeat_at = now - 10s → heartbeat_stale=false
@pytest.mark.asyncio
async def test_status_running_heartbeat_fresh(db_session):
    fresh_hb = datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(seconds=10)
    job_run = await _create_job_run(
        db_session,
        status="running",
        orchestrator_status="refreshing_daily",
        heartbeat_at=fresh_hb,
    )

    result = await get_after_close_run_status(db_session, job_run.id)

    assert result["heartbeat_stale"] is False


# [Phase7] - 场景 6: 没有 last_completed_step 时返回 None
@pytest.mark.asyncio
async def test_status_no_last_completed_step(db_session):
    job_run = await _create_job_run(
        db_session,
        status="running",
        orchestrator_status="queued",
        # 不设置 last_completed_step
    )

    result = await get_after_close_run_status(db_session, job_run.id)

    assert result["last_completed_step"] is None


# [Phase7] - 场景 7: 没有 worker_instance_id 时返回 None
@pytest.mark.asyncio
async def test_status_no_worker_instance_id(db_session):
    job_run = await _create_job_run(
        db_session,
        status="queued",
        orchestrator_status="queued",
        worker_instance_id=None,
    )

    result = await get_after_close_run_status(db_session, job_run.id)

    assert result["worker_instance_id"] is None
    assert result["heartbeat_at"] is not None  # 创建时已设置
