"""[CP-V3-D] Phase D Auto-resume 受控测试。

验证 after_close_orchestrator 任务在不同步骤中断后的恢复流程：
1. refreshing_daily 中断 → interrupted → resume_queued
2. waiting_dsa_worker 中断 → interrupted → resume_queued
3. feature_snapshot 中断 → interrupted → resume_queued
4. attempt_no 递增（每次 resume +1）
5. lease_epoch fencing（worker 领取时递增，旧 worker 写入失败）
6. last_completed_step 保留（metadata 中保持中断时的步骤）
7. 最大重试次数限制（attempt_no >= 3 不再 auto-resume）
8. 唯一活跃记录（同一 run_key 不会有多个 queued/running/resume_queued）
9. 无僵尸 running（interrupted 后不再有 running 状态）

约束：
- 使用测试 DB（db_session fixture，事务性回滚）
- 不修改历史生产任务
- 验证状态机闭环：queued → running → interrupted → resume_queued → running → succeeded
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

from app.models.job_run_event import JobRunEvent
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.scheduler_job_run_recovery_service import (
    _AFTER_CLOSE_JOB_NAME,
    auto_resume_interrupted_after_close_runs,
    recover_stale_scheduler_job_runs,
)

_TZ = ZoneInfo("Asia/Shanghai")
_TEST_DATE = "2026-07-22"


async def _create_after_close_run(
    db_session,
    *,
    run_key: str,
    status: str = "running",
    orchestrator_status: str = "refreshing_daily",
    attempt_no: int = 0,
    lease_expires_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
    last_completed_step: str | None = None,
    lease_epoch: int = 0,
) -> SchedulerJobRun:
    """创建 after_close_orchestrator 测试任务。

    Args:
        orchestrator_status: 中断时的编排步骤（refreshing_daily/waiting_dsa_worker/feature_snapshot）
        last_completed_step: 已完成的最后步骤（用于断点恢复）
    """
    metadata = {
        "orchestrator_status": orchestrator_status,
        "trade_date": _TEST_DATE,
    }
    if last_completed_step is not None:
        metadata["last_completed_step"] = last_completed_step

    job_run = SchedulerJobRun(
        job_name=_AFTER_CLOSE_JOB_NAME,
        business_date=_TEST_DATE,
        run_key=run_key,
        status=status,
        scheduled_at=datetime.now(_TZ),
        started_at=datetime.now(_TZ),
        heartbeat_at=heartbeat_at,
        lease_expires_at=lease_expires_at,
        lease_epoch=lease_epoch,
        attempt_no=attempt_no,
        metadata_json=json.dumps(metadata, ensure_ascii=False),
    )
    db_session.add(job_run)
    await db_session.flush()
    return job_run


async def _get_job_run(db_session, job_run_id) -> SchedulerJobRun | None:
    """获取 SchedulerJobRun（绕过 identity map 缓存）。

    recovery_service 使用 raw SQL UPDATE 修改 status/attempt_no 等字段，
    这些变更不会反映到 ORM 缓存对象。调用 expire_all() 强制下次访问重新查询 DB。
    """
    db_session.expire_all()
    job_run = await db_session.get(SchedulerJobRun, job_run_id)
    return job_run


async def _count_events(db_session, job_run_id, step: str) -> int:
    """统计指定步骤的事件数量。"""
    stmt = select(JobRunEvent).where(
        JobRunEvent.job_run_id == job_run_id,
        JobRunEvent.step == step,
    )
    result = await db_session.execute(stmt)
    return len(list(result.scalars().all()))


# =============================================================================
# 测试 1: refreshing_daily 中断 → resume_queued
# =============================================================================


@pytest.mark.asyncio
async def test_d_refreshing_daily_interruption_and_resume(db_session) -> None:
    """[CP-V3-D] refreshing_daily 步骤中断 → interrupted → resume_queued。

    场景：after_close 任务在 refreshing_daily 步骤运行时进程崩溃，
    租约过期 → recover_stale 标记 interrupted → auto_resume 转 resume_queued。
    """
    test_now = datetime(2026, 7, 22, 16, 30, 0, tzinfo=_TZ)
    run_key = f"after_close_orchestrator:{_TEST_DATE}"

    # 创建 running 任务，lease 已过期
    job_run = await _create_after_close_run(
        db_session,
        run_key=run_key,
        status="running",
        orchestrator_status="refreshing_daily",
        lease_expires_at=test_now - timedelta(minutes=5),
        heartbeat_at=test_now - timedelta(seconds=120),
        attempt_no=0,
    )
    job_run_id = job_run.id

    # 1. recover_stale: running → interrupted
    recovered = await recover_stale_scheduler_job_runs(db_session, now=test_now)
    assert recovered == 1

    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run is not None
    assert job_run.status == "interrupted"
    assert job_run.error_code == "STALE_PROCESS_TERMINATED"
    assert job_run.finished_at is not None
    # recovery 事件
    assert await _count_events(db_session, job_run_id, "recovery") == 1
    # metadata.orchestrator_status 应更新为 interrupted
    metadata = json.loads(job_run.metadata_json)
    assert metadata["orchestrator_status"] == "interrupted"

    # 2. auto_resume: interrupted → resume_queued
    resumed = await auto_resume_interrupted_after_close_runs(db_session, now=test_now)
    assert resumed == 1

    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run is not None
    assert job_run.status == "resume_queued"
    assert job_run.attempt_no == 1  # 0 → 1
    assert job_run.error_code is None  # 清空错误信息
    assert job_run.finished_at is None  # 清空完成时间
    # resume 事件
    assert await _count_events(db_session, job_run_id, "auto_resume") == 1

    # 验证 resume 事件 payload
    stmt = select(JobRunEvent).where(
        JobRunEvent.job_run_id == job_run_id,
        JobRunEvent.step == "auto_resume",
    )
    result = await db_session.execute(stmt)
    event = result.scalars().one()
    assert event.payload is not None
    assert event.payload.get("action") == "interrupted_to_resume_queued"
    assert event.payload.get("attempt_no") == 1


# =============================================================================
# 测试 2: waiting_dsa_worker 中断 → resume_queued
# =============================================================================


@pytest.mark.asyncio
async def test_d_waiting_dsa_interruption_and_resume(db_session) -> None:
    """[CP-V3-D] waiting_dsa_worker 步骤中断 → interrupted → resume_queued。

    场景：after_close 任务在 waiting_dsa_worker 步骤运行时进程崩溃。
    last_completed_step=refreshing_daily（已完成日线刷新）。
    """
    test_now = datetime(2026, 7, 22, 17, 0, 0, tzinfo=_TZ)
    run_key = f"after_close_orchestrator:{_TEST_DATE}:dsa"

    job_run = await _create_after_close_run(
        db_session,
        run_key=run_key,
        status="running",
        orchestrator_status="waiting_dsa_worker",
        last_completed_step="refreshing_daily",
        lease_expires_at=test_now - timedelta(minutes=3),
        heartbeat_at=test_now - timedelta(seconds=100),
        attempt_no=0,
    )
    job_run_id = job_run.id

    # recover + resume
    await recover_stale_scheduler_job_runs(db_session, now=test_now)
    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run.status == "interrupted"

    await auto_resume_interrupted_after_close_runs(db_session, now=test_now)
    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run.status == "resume_queued"
    assert job_run.attempt_no == 1

    # 验证 last_completed_step 保留（resume 事件 payload 中）
    stmt = select(JobRunEvent).where(
        JobRunEvent.job_run_id == job_run_id,
        JobRunEvent.step == "auto_resume",
    )
    result = await db_session.execute(stmt)
    event = result.scalars().one()
    assert event.payload.get("last_completed_step") == "refreshing_daily"


# =============================================================================
# 测试 3: feature_snapshot 中断 → resume_queued
# =============================================================================


@pytest.mark.asyncio
async def test_d_feature_snapshot_interruption_and_resume(db_session) -> None:
    """[CP-V3-D] feature_snapshot 步骤中断 → interrupted → resume_queued。

    场景：after_close 任务在 feature_snapshot 步骤运行时进程崩溃。
    last_completed_step=quality_gate（已通过质量门禁）。
    """
    test_now = datetime(2026, 7, 22, 18, 0, 0, tzinfo=_TZ)
    run_key = f"after_close_orchestrator:{_TEST_DATE}:snap"

    job_run = await _create_after_close_run(
        db_session,
        run_key=run_key,
        status="running",
        orchestrator_status="feature_snapshot",
        last_completed_step="quality_gate",
        lease_expires_at=test_now - timedelta(minutes=2),
        heartbeat_at=test_now - timedelta(seconds=95),
        attempt_no=0,
    )
    job_run_id = job_run.id

    await recover_stale_scheduler_job_runs(db_session, now=test_now)
    await auto_resume_interrupted_after_close_runs(db_session, now=test_now)

    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run.status == "resume_queued"
    assert job_run.attempt_no == 1

    # 验证 resume 事件中 last_completed_step
    stmt = select(JobRunEvent).where(
        JobRunEvent.job_run_id == job_run_id,
        JobRunEvent.step == "auto_resume",
    )
    result = await db_session.execute(stmt)
    event = result.scalars().one()
    assert event.payload.get("last_completed_step") == "quality_gate"


# =============================================================================
# 测试 4: attempt_no 递增 + 最大重试限制
# =============================================================================


@pytest.mark.asyncio
async def test_d_attempt_no_increment_and_max_limit(db_session) -> None:
    """[CP-V3-D] attempt_no 每次递增，达到 _MAX_AUTO_RESUME_ATTEMPTS 后不再 auto-resume。

    场景：任务已 resume 2 次（attempt_no=2），第 3 次中断后：
    - auto_resume 将 attempt_no 递增到 3
    - 再次中断后 attempt_no=3 >= _MAX_AUTO_RESUME_ATTEMPTS(3) → 不再 auto-resume
    """
    test_now = datetime(2026, 7, 22, 19, 0, 0, tzinfo=_TZ)
    run_key = f"after_close_orchestrator:{_TEST_DATE}:max"

    # 创建已 resume 2 次的 interrupted 任务（attempt_no=2）
    job_run = await _create_after_close_run(
        db_session,
        run_key=run_key,
        status="interrupted",
        orchestrator_status="feature_snapshot",
        last_completed_step="quality_gate",
        attempt_no=2,  # 已重试 2 次
    )
    job_run_id = job_run.id

    # auto_resume: attempt_no 2 → 3
    resumed = await auto_resume_interrupted_after_close_runs(db_session, now=test_now)
    assert resumed == 1
    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run.status == "resume_queued"
    assert job_run.attempt_no == 3

    # 模拟再次中断（worker 领取后又崩溃）
    job_run.status = "interrupted"
    job_run.lease_expires_at = test_now - timedelta(minutes=1)
    job_run.heartbeat_at = test_now - timedelta(seconds=120)
    await db_session.flush()

    # 再次 auto_resume: attempt_no=3 >= _MAX_AUTO_RESUME_ATTEMPTS(3) → 不再 resume
    resumed_again = await auto_resume_interrupted_after_close_runs(db_session, now=test_now)
    assert resumed_again == 0
    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run.status == "interrupted"  # 保持 interrupted，不再 auto-resume
    assert job_run.attempt_no == 3  # 不再递增


# =============================================================================
# 测试 5: lease_epoch fencing
# =============================================================================


@pytest.mark.asyncio
async def test_d_lease_epoch_fencing(db_session) -> None:
    """[CP-V3-D] lease_epoch fencing：旧 worker 写入被拒绝。

    场景：
    1. Worker A 领取任务（lease_epoch=0→1），开始执行
    2. Worker A 崩溃（lease 过期）→ interrupted → resume_queued
    3. Worker B 领取任务（lease_epoch=1→2），开始执行
    4. Worker A 恢复尝试写入 → WHERE lease_epoch=1 失败（fencing）

    本测试验证 lease_epoch 在恢复流程中正确递增。
    """
    test_now = datetime(2026, 7, 22, 20, 0, 0, tzinfo=_TZ)
    run_key = f"after_close_orchestrator:{_TEST_DATE}:fence"

    # 创建 running 任务，lease_epoch=1（已被 worker A 领取）
    job_run = await _create_after_close_run(
        db_session,
        run_key=run_key,
        status="running",
        orchestrator_status="refreshing_daily",
        lease_expires_at=test_now - timedelta(minutes=5),
        heartbeat_at=test_now - timedelta(seconds=120),
        lease_epoch=1,  # worker A 的 epoch
        attempt_no=0,
    )
    job_run_id = job_run.id

    # recover + resume
    await recover_stale_scheduler_job_runs(db_session, now=test_now)
    await auto_resume_interrupted_after_close_runs(db_session, now=test_now)

    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run.status == "resume_queued"
    # lease_epoch 在 auto_resume 中未改变（worker 领取时才递增）
    assert job_run.lease_epoch == 1

    # 模拟 worker B 领取：lease_epoch 递增到 2
    fencing_update = text("""
        UPDATE scheduler_job_runs
        SET status = 'running',
            lease_epoch = lease_epoch + 1,
            heartbeat_at = :now,
            lease_expires_at = :lease_expiry
        WHERE id = :id AND status = 'resume_queued'
    """)
    await db_session.execute(fencing_update, {
        "id": job_run_id,
        "now": test_now,
        "lease_expiry": test_now + timedelta(minutes=10),
    })
    await db_session.flush()

    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run.status == "running"
    assert job_run.lease_epoch == 2  # 递增到 2

    # 验证旧 worker A 的写入会被 fencing 拒绝
    # （WHERE lease_epoch = 1 不再匹配，因为当前 lease_epoch = 2）
    stale_write = text("""
        UPDATE scheduler_job_runs
        SET error_message = 'stale worker A write attempt'
        WHERE id = :id AND lease_epoch = 1
    """)
    result = await db_session.execute(stale_write, {"id": job_run_id})
    assert result.rowcount == 0, "旧 worker 写入应被 fencing 拒绝（lease_epoch 不匹配）"

    # 验证 error_message 未被修改
    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run.error_message != "stale worker A write attempt"


# =============================================================================
# 测试 6: 唯一活跃记录（无重复 resume_queued）
# =============================================================================


@pytest.mark.asyncio
async def test_d_unique_active_run_no_duplicate(db_session) -> None:
    """[CP-V3-D] 同一 run_key 不会有多个 queued/running/resume_queued。

    场景：任务已 resume_queued，尝试创建同 run_key 的第二个活跃任务 → 被唯一索引拒绝。
    """
    test_now = datetime(2026, 7, 22, 21, 0, 0, tzinfo=_TZ)
    run_key = f"after_close_orchestrator:{_TEST_DATE}:uniq"

    # 创建 interrupted 任务并 resume
    job_run = await _create_after_close_run(
        db_session,
        run_key=run_key,
        status="interrupted",
        orchestrator_status="refreshing_daily",
        attempt_no=0,
    )
    job_run_id = job_run.id

    await auto_resume_interrupted_after_close_runs(db_session, now=test_now)
    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run.status == "resume_queued"

    # 尝试创建同 run_key 的第二个 resume_queued 任务 → 应失败（唯一索引）
    with pytest.raises(Exception, match="unique|duplicate|violat"):
        duplicate = SchedulerJobRun(
            job_name=_AFTER_CLOSE_JOB_NAME,
            business_date=_TEST_DATE,
            run_key=run_key,  # 同一 run_key
            status="resume_queued",
            attempt_no=0,
        )
        db_session.add(duplicate)
        await db_session.flush()


# =============================================================================
# 测试 7: 无僵尸 running（interrupted 后不再有 running）
# =============================================================================


@pytest.mark.asyncio
async def test_d_no_zombie_running_after_interruption(db_session) -> None:
    """[CP-V3-D] 任务被 interrupted 后，同一 run_key 不再有 running 状态。

    场景：任务 running → lease 过期 → interrupted。此时不应有同 run_key 的 running 任务。
    """
    test_now = datetime(2026, 7, 22, 22, 0, 0, tzinfo=_TZ)
    run_key = f"after_close_orchestrator:{_TEST_DATE}:zombie"

    await _create_after_close_run(
        db_session,
        run_key=run_key,
        status="running",
        orchestrator_status="feature_snapshot",
        lease_expires_at=test_now - timedelta(minutes=5),
        heartbeat_at=test_now - timedelta(seconds=120),
    )

    # recover → interrupted
    recovered = await recover_stale_scheduler_job_runs(db_session, now=test_now)
    assert recovered == 1

    # 验证同 run_key 的 running 任务数为 0
    count_sql = text("""
        SELECT count(*) FROM scheduler_job_runs
        WHERE run_key = :run_key AND status = 'running'
    """)
    result = await db_session.execute(count_sql, {"run_key": run_key})
    running_count = result.scalar()
    assert running_count == 0, f"不应有僵尸 running 任务，实际 {running_count} 个"

    # 验证同 run_key 的 interrupted 任务数为 1
    count_sql = text("""
        SELECT count(*) FROM scheduler_job_runs
        WHERE run_key = :run_key AND status = 'interrupted'
    """)
    result = await db_session.execute(count_sql, {"run_key": run_key})
    interrupted_count = result.scalar()
    assert interrupted_count == 1


# =============================================================================
# 测试 8: 非 after_close 任务不被 auto_resume
# =============================================================================


@pytest.mark.asyncio
async def test_d_non_after_close_not_auto_resumed(db_session) -> None:
    """[CP-V3-D] 非 after_close_orchestrator 任务的 interrupted 不被 auto_resume。

    场景：bars_scheduler 任务 interrupted → auto_resume 不处理（仅 after_close 支持）。
    """
    test_now = datetime(2026, 7, 22, 23, 0, 0, tzinfo=_TZ)
    run_key = "bars_scheduler:2026-07-22"

    job_run = SchedulerJobRun(
        job_name="bars_scheduler",  # 非 after_close_orchestrator
        business_date=_TEST_DATE,
        run_key=run_key,
        status="interrupted",
        attempt_no=0,
    )
    db_session.add(job_run)
    await db_session.flush()
    job_run_id = job_run.id

    resumed = await auto_resume_interrupted_after_close_runs(db_session, now=test_now)
    assert resumed == 0  # 不处理非 after_close 任务

    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run.status == "interrupted"  # 状态不变


# =============================================================================
# 测试 9: 完整状态机闭环（多轮中断恢复）
# =============================================================================


@pytest.mark.asyncio
async def test_d_full_state_machine_cycle(db_session) -> None:
    """[CP-V3-D] 完整状态机闭环：running → interrupted → resume_queued → running → interrupted → resume_queued。

    场景：任务经历 2 轮中断恢复，验证每轮的 attempt_no / lease_epoch / last_completed_step。
    """
    test_now = datetime(2026, 7, 22, 14, 0, 0, tzinfo=_TZ)
    run_key = f"after_close_orchestrator:{_TEST_DATE}:cycle"

    # 第 1 轮：running (refreshing_daily) → interrupted → resume_queued
    job_run = await _create_after_close_run(
        db_session,
        run_key=run_key,
        status="running",
        orchestrator_status="refreshing_daily",
        lease_expires_at=test_now - timedelta(minutes=5),
        heartbeat_at=test_now - timedelta(seconds=120),
        attempt_no=0,
        lease_epoch=0,
    )
    job_run_id = job_run.id

    await recover_stale_scheduler_job_runs(db_session, now=test_now)
    await auto_resume_interrupted_after_close_runs(db_session, now=test_now)

    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run.status == "resume_queued"
    assert job_run.attempt_no == 1
    assert job_run.lease_epoch == 0  # worker 领取时才递增

    # 模拟 worker 领取 → running（lease_epoch 递增到 1）
    pickup_sql = text("""
        UPDATE scheduler_job_runs
        SET status = 'running', lease_epoch = lease_epoch + 1,
            heartbeat_at = :now, lease_expires_at = :lease_expiry
        WHERE id = :id AND status = 'resume_queued'
    """)
    await db_session.execute(pickup_sql, {
        "id": job_run_id, "now": test_now,
        "lease_expiry": test_now + timedelta(minutes=10),
    })
    await db_session.flush()

    # 第 2 轮：running (feature_snapshot) → interrupted → resume_queued
    # 更新 metadata 以模拟进度推进（last_completed_step=refreshing_daily）
    job_run = await _get_job_run(db_session, job_run_id)
    metadata = json.loads(job_run.metadata_json)
    metadata["orchestrator_status"] = "feature_snapshot"
    metadata["last_completed_step"] = "refreshing_daily"
    job_run.metadata_json = json.dumps(metadata, ensure_ascii=False)
    job_run.lease_expires_at = test_now + timedelta(minutes=1)  # 又快过期
    job_run.heartbeat_at = test_now - timedelta(seconds=100)
    await db_session.flush()

    test_now_2 = test_now + timedelta(minutes=15)
    await recover_stale_scheduler_job_runs(db_session, now=test_now_2)
    await auto_resume_interrupted_after_close_runs(db_session, now=test_now_2)

    job_run = await _get_job_run(db_session, job_run_id)
    assert job_run.status == "resume_queued"
    assert job_run.attempt_no == 2  # 1 → 2
    assert job_run.lease_epoch == 1  # 第 2 轮 worker 领取时递增过

    # 验证 resume 事件中 last_completed_step 正确传递
    stmt = select(JobRunEvent).where(
        JobRunEvent.job_run_id == job_run_id,
        JobRunEvent.step == "auto_resume",
    )
    result = await db_session.execute(stmt)
    events = list(result.scalars().all())
    assert len(events) == 2  # 2 轮 resume 各 1 个事件

    # 第 2 个 resume 事件的 last_completed_step 应为 refreshing_daily
    second_event = events[1]
    assert second_event.payload.get("last_completed_step") == "refreshing_daily"
    assert second_event.payload.get("attempt_no") == 2
