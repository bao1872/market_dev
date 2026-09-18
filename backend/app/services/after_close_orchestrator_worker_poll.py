"""[AfterCloseWorker] Claim/Poll 业务 owner（W5B：从 worker.py 迁移，仅搬 owner 不改状态机）。

函数：poll_after_close_once(*, session_factory, worker_instance_id, logger) -> bool

原始 worker._after_close_poll_once() 仅保留为 thin façade，将依赖注入后委托给本模块。
本模块不引入新的 claim 服务、不切换 repository、不改 SQL / 状态机 / 事务边界 /
fencing / missing-trade_date 分支 / execute 异常语义。

SQL 语义（冻结）：
    select(SchedulerJobRun)
    .where(job_name == "after_close_orchestrator",
           status.in_(("queued", "resume_queued")))
    .order_by(created_at)
    .limit(1)
    .with_for_update(skip_locked=True)

事务边界（冻结）：拿到 row lock → 写 running + worker + heartbeat + lease +
lease_epoch += 1 → commit（必须在 execute 之前）→ 再解析 metadata / trade_date /
id / epoch → 执行 orchestrator。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.scheduler_job_run import SchedulerJobRun


async def poll_after_close_once(
    *,
    session_factory,
    worker_instance_id: str,
    logger,
) -> bool:
    """[AfterCloseWorker] - 单次轮询：领取并执行一个 queued/resume_queued 盘后编排任务。

    使用 SELECT ... FOR UPDATE SKIP LOCKED 领取任务，多个 Worker 实例只有一个能领取。
    领取后更新 status='running' + worker_instance_id + heartbeat + lease，
    然后调用 execute_after_close_run（含断点恢复 + 心跳更新）。

    [PRD §4.3 JOB-01] 领取 queued（首次）或 resume_queued（自动恢复）任务：
    - queued：首次执行，attempt_no=0
    - resume_queued：自动恢复，attempt_no>=1，execute_after_close_run 按 last_completed_step 断点恢复

    [PRD §4.3 JOB-02] 领取时递增 lease_epoch（fencing）：
    - 旧 Worker（lease 已过期）的写操作会因 lease_epoch 不匹配被拒绝
    - 防止僵尸 Worker 继续写状态

    Returns:
        True 如果领取到任务（无论执行成功与否），False 如果无 queued/resume_queued 任务
    """
    from datetime import date as date_cls

    from sqlalchemy import select

    from app.services.after_close_orchestrator import (
        _ORCHESTRATOR_LEASE_SECONDS,
        execute_after_close_run,
    )

    async with session_factory() as db:
        # [AfterCloseWorker] - FOR UPDATE SKIP LOCKED 领取一个 queued 或 resume_queued 任务
        # [JOB-01] resume_queued 任务由 auto_resume_interrupted_after_close_runs 自动转换
        stmt = (
            select(SchedulerJobRun)
            .where(
                SchedulerJobRun.job_name == "after_close_orchestrator",
                SchedulerJobRun.status.in_(("queued", "resume_queued")),
            )
            .order_by(SchedulerJobRun.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        result = await db.execute(stmt)
        job_run = result.scalar_one_or_none()

        if job_run is None:
            # 无 queued/resume_queued 任务，释放锁（rollback 释放 FOR UPDATE 锁）
            await db.rollback()
            return False

        # [JOB-01] 记录领取前状态（queued=首次, resume_queued=自动恢复）
        prev_status = job_run.status
        is_resume = prev_status == "resume_queued"

        # 领取任务：更新 status='running' + worker + heartbeat + lease
        # [JOB-02] 递增 lease_epoch（fencing）：旧 Worker 写操作会因 lease_epoch 不匹配被拒绝
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        job_run.status = "running"
        job_run.worker_instance_id = worker_instance_id
        if job_run.started_at is None:
            job_run.started_at = now
        job_run.heartbeat_at = now
        job_run.lease_expires_at = now + timedelta(seconds=_ORCHESTRATOR_LEASE_SECONDS)
        job_run.lease_epoch = job_run.lease_epoch + 1  # [JOB-02] fencing
        await db.commit()

        # 提取 trade_date（expire_on_commit=False 让 commit 后属性仍可用）
        meta = json.loads(job_run.metadata_json) if job_run.metadata_json else {}
        trade_date_str = meta.get("trade_date")
        job_run_id = job_run.id
        current_lease_epoch = job_run.lease_epoch

        logger.info(
            "[AfterCloseWorker] 领取任务: job_run_id=%s, prev_status=%s, "
            "attempt_no=%s, lease_epoch=%s, is_resume=%s",
            job_run_id, prev_status, job_run.attempt_no, current_lease_epoch, is_resume,
        )

    if not trade_date_str:
        # advice.md: 任务缺 trade_date 必须立即写 ERROR 事件 + status=failed + finished_at + 释放 run_key
        # 禁止只记日志留 running 僵尸
        logger.error(
            "[AfterCloseWorker] 任务缺少 trade_date，标记 failed: job_run_id=%s", job_run_id,
        )
        async with session_factory() as db:
            jr = await db.get(SchedulerJobRun, job_run_id)
            if jr is not None:
                now_fail = datetime.now(ZoneInfo("Asia/Shanghai"))
                jr.status = "failed"
                jr.finished_at = now_fail
                jr.lease_expires_at = now_fail  # 释放 run_key
                jr.error_message = "任务缺少 trade_date，无法执行盘后流水线"
                # 写 ERROR 事件
                from app.models.job_run_event import JobRunEvent
                fail_meta = json.loads(jr.metadata_json) if jr.metadata_json else {}
                fail_meta["orchestrator_status"] = "failed"
                db.add(JobRunEvent(
                    job_run_id=jr.id,
                    step="claim",
                    level="ERROR",
                    message="任务缺少 trade_date，无法执行盘后流水线",
                    payload={"reason": "missing_trade_date", **fail_meta},
                ))
                await db.commit()
        return True  # 领取了但已标记 failed

    trade_date = date_cls.fromisoformat(trade_date_str)

    # 执行编排（异常由 execute_after_close_run 内部处理为 failed 后 re-raise）
    # Worker 捕获 re-raised 异常仅记录日志，不崩溃
    # [JOB-02] 传递 lease_epoch 使 execute_after_close_run 启用 fenced UPDATE
    try:
        await execute_after_close_run(
            job_run_id=job_run_id,
            trade_date=trade_date,
            worker_id=worker_instance_id,
            lease_epoch=current_lease_epoch,
        )
    except Exception as exc:
        logger.exception(
            "[AfterCloseWorker] 执行异常: job_run_id=%s, error=%s", job_run_id, exc,
        )
        # execute_after_close_run 内部已标记 failed，此处仅记录不 re-raise

    return True
