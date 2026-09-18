"""Monitor scheduler worker main lifecycle, separate from the process composition root.

This module owns the Monitor Scheduler main polling lifecycle AND the Monitor-specific
session helpers (moved from the composition root in PANJI-GOV-W4A / W4B1):

* Trading-session classification (morning / afternoon / non-session) is owned by this
  module's :func:`_get_monitor_session`; the boundary rules are preserved verbatim.
* Monitor session job-run acquisition/reuse is owned by this module's
  :func:`_find_or_create_monitor_session_job_run`, a thin wrapper that delegates canonical
  ``SchedulerJobRun`` creation to the injected ``create_job_run`` (the composition root's
  :func:`app.worker._create_job_run`).  It does NOT insert, commit, or recover on its own.
* Startup / error Feishu notification lives in the dedicated
  :mod:`app.services.monitor_status_notifier` module, imported at module top; the runtime
  only *calls* it and does NOT own the Redis idempotency / channel-query / DTO / adapter
  logic.
* The generic ``SchedulerJobRun`` state rules (finish / recover) are injected by the
  composition root (:mod:`app.worker`) and must not be duplicated here.
* Monitor cycle execution and stale-evaluation recovery live in
  :class:`app.services.monitor_batch_service.MonitorBatchService`.
* The monotonic clock used for cycle latency is injected (``monotonic_clock``) so the
  composition root keeps ownership of the time source.

The top-level :mod:`app.worker` module supplies the session factory, heartbeat
ownership, shared shutdown signal, the canonical helpers, and business owners.  This is a
pure structural extraction (PANJI-GOV-W4A, W4B1, and W4B2); no cron / trading-session
boundary / reentrancy guard / transaction / exception / state-update / sleep-cadence /
notify timing semantics changed.  In W4B1 the Monitor session helpers moved here and
``create_job_run`` became an injected dependency; in W4B2 the status notifier moved to its
own module and is imported (not injected).
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
from app.services.monitor_status_notifier import notify_monitor_status

SessionFactory = Callable[[], Any]
HeartbeatLoop = Callable[[str], Coroutine[Any, Any, None]]
ShutdownProbe = Callable[[], bool]
RecoverStaleJobRuns = Callable[[Any], Coroutine[Any, Any, int]]
CreateJobRun = Callable[..., Coroutine[Any, Any, SchedulerJobRun | None]]
FinishJobRun = Callable[..., Coroutine[Any, Any, None]]
MonotonicClock = Callable[[], float]


def _get_monitor_session(
    now_cst: Any,
) -> tuple[str, time_cls, time_cls] | None:
    """根据当前上海时间返回盘中交易时段标签与起止时间。

    Returns:
        (label, start_time, end_time) 或 None（非交易时段）

    边界（逐字保持）：
        - 09:30 包含，11:30 不包含
        - 13:00 包含，15:00 不包含
    """
    current_time = now_cst.time()
    morning_start = time_cls(9, 30)
    morning_end = time_cls(11, 30)
    afternoon_start = time_cls(13, 0)
    afternoon_end = time_cls(15, 0)

    if morning_start <= current_time < morning_end:
        return ("morning", morning_start, morning_end)
    if afternoon_start <= current_time < afternoon_end:
        return ("afternoon", afternoon_start, afternoon_end)
    return None


async def _find_or_create_monitor_session_job_run(
    db: Any,
    now_cst: Any,
    business_date: str,
    session_label: str,
    *,
    create_job_run: CreateJobRun,
) -> SchedulerJobRun | None:
    """查找或创建当前交易时段的 monitor_scheduler job_run（幂等版本）。

    基于 run_key=monitor_scheduler:{business_date}:{session_label} 唯一索引保证 session 幂等。
    返回 SchedulerJobRun 表示新建；返回 None 表示 session 已存在（调用方应按 run_key 查询复用）。

    仅作为 canonical _create_job_run 的 Monitor-specific wrapper；不直接 INSERT、
    不调用 idempotency_service、不自行 commit/recover。
    """
    run_key = f"monitor_scheduler:{business_date}:{session_label}"
    return await create_job_run(
        db,
        "monitor_scheduler",
        business_date,
        lease_seconds=120,
        metadata={"session_label": session_label},
        run_key=run_key,
    )


async def run_monitor_scheduler_worker_runtime(
    *,
    session_factory: SessionFactory,
    heartbeat_loop: HeartbeatLoop,
    should_shutdown: ShutdownProbe,
    recover_stale_job_runs: RecoverStaleJobRuns,
    create_job_run: CreateJobRun,
    finish_job_run: FinishJobRun,
    monotonic_clock: MonotonicClock,
    logger: logging.Logger,
) -> None:
    """Run the Monitor Scheduler main lifecycle.

    Behavior is identical to the previous ``run_monitor_scheduler_worker`` body:
    start the heartbeat task, then inside the trading session run one
    ``MonitorBatchService.execute_monitor_cycle`` per ``cycle_interval`` second,
    acquiring/reusing a per-session ``SchedulerJobRun`` (via the owned
    ``_find_or_create_monitor_session_job_run``), with startup stale recovery
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
        session_factory=session_factory,
        logger=logger,
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

            session_info = _get_monitor_session(now)
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
                job_run = await _find_or_create_monitor_session_job_run(
                    db, now, business_date, session_label,
                    create_job_run=create_job_run,
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
            await notify_monitor_status(
                "监控服务异常", str(exc), is_error=True,
                session_factory=session_factory, logger=logger,
            )

        # 交易时段内每 cycle_interval 秒一轮
        await asyncio.sleep(cycle_interval)

    logger.info("Monitor Scheduler Worker 已退出")


__all__ = [
    "run_monitor_scheduler_worker_runtime",
    "_get_monitor_session",
    "_find_or_create_monitor_session_job_run",
]
