"""Calendar scheduler worker lifecycle, separate from the process composition root.

This module owns ONLY the Calendar Scheduler polling/scheduling lifecycle:
APScheduler setup, the daily ``calendar_job`` closure, startup stale-run
recovery, and graceful shutdown.  It does NOT re-implement any business rule:

* Trading-calendar judgment lives in :mod:`app.core.time`
  (``shanghai_business_date``).
* The actual calendar refresh lives in
  :func:`app.services.calendar_seed.seed_calendar_from_mootdx`.
* The generic ``SchedulerJobRun`` state rules (create / finish / recover) are
  injected by the composition root (:mod:`app.worker`) and must not be
  duplicated here.

The top-level :mod:`app.worker` module supplies the session factory, heartbeat
ownership, shared shutdown signal, and the canonical job-run helpers.  This is a
pure structural extraction (PANJI-GOV-W1); no cron / timezone / job identity /
retry / transaction / heartbeat / shutdown / logging / exception semantics
changed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.models.scheduler_job_run import SchedulerJobRun

SessionFactory = Callable[[], Any]
HeartbeatLoop = Callable[[str], Coroutine[Any, Any, None]]
ShutdownProbe = Callable[[], bool]

# Generic SchedulerJobRun state-rule collaborators, injected by the composition
# root so this module never re-implements them.
CreateJobRun = Callable[..., Coroutine[Any, Any, SchedulerJobRun | None]]
FinishJobRun = Callable[..., Coroutine[Any, Any, None]]
RecoverStaleJobRuns = Callable[[Any], Coroutine[Any, Any, int]]


async def run_calendar_scheduler_worker_runtime(
    *,
    session_factory: SessionFactory,
    heartbeat_loop: HeartbeatLoop,
    should_shutdown: ShutdownProbe,
    create_job_run: CreateJobRun,
    finish_job_run: FinishJobRun,
    recover_stale_job_runs: RecoverStaleJobRuns,
    logger: logging.Logger,
) -> None:
    """Run the Calendar Scheduler lifecycle.

    Behavior is identical to the previous ``run_calendar_scheduler_worker`` body:
    daily 02:00 Asia/Shanghai cron, idempotent job-run, Mootdx seed of the
    current and next year, startup stale-run recovery, and graceful shutdown.
    """
    _hb_task = asyncio.create_task(heartbeat_loop("calendar_scheduler"))
    scheduler = AsyncIOScheduler()

    # 启动时恢复过期 running 任务
    try:
        async with session_factory() as db:
            recovered = await recover_stale_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info("Calendar Scheduler 启动恢复: %d 个过期任务", recovered)
    except Exception as exc:
        logger.exception("Calendar Scheduler 启动恢复异常: %s", exc)

    async def calendar_job() -> None:
        """每日凌晨刷新交易日历（从 Mootdx 拉取当年及下一年日历并更新 DB）。"""
        from app.core.time import shanghai_business_date
        from app.services.calendar_seed import seed_calendar_from_mootdx

        today = shanghai_business_date()
        job_run = None
        try:
            async with session_factory() as session:
                # [CalendarScheduler] - scheduled_at 为 CronTrigger 计划时间（02:00），不等于 started_at
                scheduled_at = datetime.combine(
                    today, time(2, 0), tzinfo=ZoneInfo("Asia/Shanghai")
                )
                job_run = await create_job_run(
                    session, "calendar_scheduler", str(today), scheduled_at=scheduled_at,
                    run_key=f"calendar_scheduler:{today}",
                )
                if job_run is None:
                    logger.info("calendar_scheduler SKIPPED_DUPLICATE business_date=%s", today)
                    return
                total_count = 0
                for year in (today.year, today.year + 1):
                    count = await seed_calendar_from_mootdx(session, year=year, force=False)
                    total_count += count
                    logger.info("日历刷新完成: year=%d, %d 条记录更新", year, count)
                await finish_job_run(session, job_run, "succeeded", success_count=1)
        except Exception as exc:
            logger.error("日历刷新失败: %s", exc)
            if job_run is not None:
                async with session_factory() as db:
                    await finish_job_run(db, job_run, "failed", error_message=str(exc)[:500])
            raise

    scheduler.add_job(
        calendar_job,
        CronTrigger(hour=2, minute=0, timezone=ZoneInfo("Asia/Shanghai")),
        id="calendar_scheduler",
        name="calendar_scheduler",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Calendar Scheduler Worker 启动（每日 02:00 刷新交易日历）")

    while not should_shutdown():
        await asyncio.sleep(60)

    scheduler.shutdown(wait=False)
    logger.info("Calendar Scheduler Worker 已退出")


__all__ = ["run_calendar_scheduler_worker_runtime"]
