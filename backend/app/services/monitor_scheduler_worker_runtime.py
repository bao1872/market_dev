"""Monitor scheduler worker main lifecycle, separate from the process composition root.

This module owns ONLY the Monitor Scheduler main polling lifecycle: the trading-
session loop, intraday cycle execution, session job-run acquisition/reuse, startup
stale recovery, and graceful shutdown.  It does NOT re-implement any business rule:

* Trading-session classification (morning / afternoon / non-session) lives in the
  injected ``get_monitor_session`` (the composition root's
  :func:`app.worker._get_monitor_session`).
* Monitor session job-run acquisition/reuse lives in the injected
  ``find_or_create_session_job_run`` (the composition root's
  :func:`app.worker._find_or_create_monitor_session_job_run`).
* Startup / error Feishu notification lives in the injected ``notify_monitor_status``
  (the composition root's :func:`app.worker._notify_monitor_status`).
* The generic ``SchedulerJobRun`` state rules (finish / recover) are injected by the
  composition root (:mod:`app.worker`) and must not be duplicated here.
* Monitor cycle execution and stale-evaluation recovery live in
  :class:`app.services.monitor_batch_service.MonitorBatchService`.
* The monotonic clock used for cycle latency is injected (``monotonic_clock``) so the
  composition root keeps ownership of the time source.

The top-level :mod:`app.worker` module supplies the session factory, heartbeat
ownership, shared shutdown signal, the canonical helpers, and business owners.  This
is a pure structural extraction (PANJI-GOV-W4A); no cron / trading-session boundary /
reentrancy guard / transaction / exception / state-update / sleep-cadence / notify
timing semantics changed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from datetime import time as time_cls
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.monitor_batch_service import MonitorBatchService

SessionFactory = Callable[[], Any]
HeartbeatLoop = Callable[[str], Coroutine[Any, Any, None]]
ShutdownProbe = Callable[[], bool]
RecoverStaleJobRuns = Callable[[Any], Coroutine[Any, Any, int]]
GetMonitorSession = Callable[..., Any]
FindOrCreateSessionJobRun = Callable[..., Coroutine[Any, Any, SchedulerJobRun | None]]
FinishJobRun = Callable[..., Coroutine[Any, Any, None]]
NotifyMonitorStatus = Callable[..., Coroutine[Any, Any, None]]
MonotonicClock = Callable[[], float]


async def run_monitor_scheduler_worker_runtime(
    *,
    session_factory: SessionFactory,
    heartbeat_loop: HeartbeatLoop,
    should_shutdown: ShutdownProbe,
    recover_stale_job_runs: RecoverStaleJobRuns,
    get_monitor_session: GetMonitorSession,
    find_or_create_session_job_run: FindOrCreateSessionJobRun,
    finish_job_run: FinishJobRun,
    notify_monitor_status: NotifyMonitorStatus,
    monotonic_clock: MonotonicClock,
    logger: logging.Logger,
) -> None:
    """Run the Monitor Scheduler main lifecycle.

    Behavior is identical to the previous ``run_monitor_scheduler_worker`` body:
    start the heartbeat task, then inside the trading session run one
    ``MonitorBatchService.execute_monitor_cycle`` per ``cycle_interval`` second,
    acquiring/reusing a per-session ``SchedulerJobRun``, with startup stale recovery
    (evaluations + scheduler job-runs) and graceful shutdown.
    """
    _hb_task = asyncio.create_task(heartbeat_loop("monitor_scheduler"))
    service = MonitorBatchService()
    cycle_interval = get_settings().intraday_monitor_poll_seconds  # [盘中监控1秒] 默认1秒
    session_finish_margin = timedelta(seconds=cycle_interval + 5)
    _cycle_running = False  # 防重入标志

    # [eval_recovery] 启动时恢复过期租约的 PENDING 评估（无 try/except，异常向上传播）
    async with session_factory() as db:
        recovered = await service.recover_stale_evaluations(db)
        await db.commit()
        if recovered > 0:
            logger.info("Monitor Worker 启动恢复: %d 个过期评估", recovered)

    # 启动时恢复过期的 monitor_scheduler running 任务（异常吞掉并记录）
    try:
        async with session_factory() as db:
            recovered = await recover_stale_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info("Monitor Scheduler 启动恢复: %d 个过期任务", recovered)
    except Exception as exc:
        logger.exception("Monitor Scheduler 启动恢复异常: %s", exc)

    logger.info(
        "Monitor Scheduler Worker 启动（交易时段 9:30-11:30 / 13:00-15:00, 间隔=%ds）",
        cycle_interval,
    )

    # 启动成功飞书通知
    await notify_monitor_status(
        "监控服务已启动",
        f"交易时段 9:30-11:30 / 13:00-15:00\n每 {cycle_interval} 秒执行一轮监控",
    )

    while not should_shutdown():
        job_run = None
        try:
            from datetime import datetime

            now = datetime.now(ZoneInfo("Asia/Shanghai"))

            # 交易日检查（使用异步接口，避免在事件循环中降级到 weekday）
            from app.services.calendar_service import is_trading_day_async

            async with session_factory() as db:
                trading = await is_trading_day_async(db, now.date())
            if not trading:
                # 非交易日，等待到下一个工作日
                await asyncio.sleep(300)  # 5分钟检查一次
                continue

            session_info = get_monitor_session(now)
            if session_info is None:
                # 非交易时段，等待
                current_time = now.time()
                if current_time < time_cls(9, 30):
                    # 开盘前，等待到 9:30
                    wait_seconds = (
                        datetime(now.year, now.month, now.day, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")) - now
                    ).total_seconds()
                    if wait_seconds > 0:
                        logger.info("等待开盘，还需 %d 秒", int(wait_seconds))
                        await asyncio.sleep(min(wait_seconds, 60))
                elif time_cls(11, 30) <= current_time < time_cls(13, 0):
                    # 午休，等待到 13:00
                    wait_seconds = (
                        datetime(now.year, now.month, now.day, 13, 0, tzinfo=ZoneInfo("Asia/Shanghai")) - now
                    ).total_seconds()
                    if wait_seconds > 0:
                        logger.info("午休中，等待 %d 秒", int(wait_seconds))
                        await asyncio.sleep(min(wait_seconds, 60))
                elif current_time >= time_cls(15, 0):
                    # 收盘后，等待到明天
                    await asyncio.sleep(300)
                continue

            session_label, _start_time, end_time = session_info
            business_date = str(now.date())

            # 交易时段内，执行监控周期
            # [盘中监控1秒] - 防重入：上一周期未完成则跳过
            if _cycle_running:
                logger.debug("monitor_scheduler 上一周期未完成，跳过本轮")
                await asyncio.sleep(cycle_interval)
                continue

            async with session_factory() as db:
                job_run = await find_or_create_session_job_run(
                    db, now, business_date, session_label,
                )
                if job_run is None:
                    # session 已存在，按 run_key 查询复用（更新 last_cycle_at）
                    from sqlalchemy import select as sa_select

                    run_key = f"monitor_scheduler:{business_date}:{session_label}"
                    stmt = (
                        sa_select(SchedulerJobRun)
                        .where(SchedulerJobRun.run_key == run_key)
                        .limit(1)
                    )
                    result_q = await db.execute(stmt)
                    job_run = result_q.scalar_one_or_none()
                    if job_run is None:
                        # 极端情况：理论上不该发生，但容错跳过本轮
                        logger.warning(
                            "monitor_scheduler session_job_run not found for run_key=%s",
                            run_key,
                        )
                        await asyncio.sleep(cycle_interval)
                        continue
                    logger.debug(
                        "monitor_scheduler 复用 session job_run_id=%s", job_run.id,
                    )
                cycle_succeeded = False
                _cycle_running = True  # [盘中监控1秒] 设置防重入标志
                _cycle_start_ts = monotonic_clock()
                try:
                    result = await service.execute_monitor_cycle(db)
                    await db.commit()
                    cycle_succeeded = True
                    _cycle_latency = monotonic_clock() - _cycle_start_ts
                    if result.total_events_written > 0:
                        logger.info(
                            "监控周期完成: session=%s instruments=%d events=%d "
                            "notifications=%d latency=%.3fs skip=0",
                            session_label,
                            result.total_instruments,
                            result.total_events_written,
                            result.total_notifications_created,
                            _cycle_latency,
                        )
                    else:
                        logger.debug(
                            "监控周期完成: session=%s instruments=%d events=0 "
                            "latency=%.3fs",
                            session_label,
                            result.total_instruments,
                            _cycle_latency,
                        )
                except Exception as exc:
                    logger.exception("Monitor Scheduler 周期异常: %s", exc)
                    await db.rollback()
                finally:
                    _cycle_running = False  # [盘中监控1秒] 清除防重入标志

                # 更新 session 级统计与心跳
                now = datetime.now(ZoneInfo("Asia/Shanghai"))
                job_run.last_cycle_at = now
                job_run.heartbeat_at = now
                job_run.lease_expires_at = now + timedelta(seconds=120)
                if cycle_succeeded:
                    job_run.succeeded_count = (job_run.succeeded_count or 0) + 1
                else:
                    job_run.failed_count = (job_run.failed_count or 0) + 1
                # [monitor_scheduler] - 查询最新 source_bar_time 写入 metadata_json，供 Admin 页面展示
                try:
                    from sqlalchemy import func as sa_func
                    from sqlalchemy import select as sa_select

                    from app.models.monitor_evaluation import MonitorEvaluation

                    latest_bar_time = await db.scalar(
                        sa_select(sa_func.max(MonitorEvaluation.source_bar_time))
                    )
                    if latest_bar_time is not None:
                        existing_meta = (
                            json.loads(job_run.metadata_json)
                            if job_run.metadata_json
                            else {}
                        )
                        existing_meta["last_bar_time"] = latest_bar_time.isoformat()
                        job_run.metadata_json = json.dumps(
                            existing_meta, ensure_ascii=False
                        )
                except Exception as exc:
                    logger.debug("查询 latest source_bar_time 失败: %s", exc)
                await db.commit()

                # session 接近结束时标记完成
                session_end_dt = datetime.combine(now.date(), end_time)
                session_end_dt = session_end_dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
                if now + session_finish_margin >= session_end_dt:
                    await finish_job_run(
                        db, job_run, "succeeded",
                        success_count=job_run.succeeded_count,
                        failure_count=job_run.failed_count,
                    )

        except Exception as exc:
            logger.exception("Monitor Scheduler 异常: %s", exc)
            if job_run is not None:
                async with session_factory() as db:
                    await finish_job_run(db, job_run, "failed", error_message=str(exc)[:500])
            # 异常退出飞书通知
            await notify_monitor_status("监控服务异常", str(exc), is_error=True)

        # 交易时段内每 cycle_interval 秒一轮
        await asyncio.sleep(cycle_interval)

    logger.info("Monitor Scheduler Worker 已退出")


__all__ = ["run_monitor_scheduler_worker_runtime"]
