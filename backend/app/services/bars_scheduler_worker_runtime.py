"""Bars scheduler worker lifecycle, separate from the process composition root.

This module owns ONLY the Bars Scheduler polling/scheduling lifecycle:
APScheduler setup, the two daily job closures (``scheduled_bars_refresh`` at
15:05 and ``scheduled_share_capital_sync`` at 18:00), startup stale-run
recovery, and graceful shutdown.  It does NOT re-implement any business rule:

* Trading-day judgment lives in
  :func:`app.services.calendar_service.is_trading_day_async`.
* After-close orchestration (15:05) lives in
  :func:`app.services.after_close_orchestrator.create_after_close_run`.
* Share-capital sync (18:00) lives in
  :func:`app.services.instrument_share_sync_service.sync_share_capitals`.
* The generic ``SchedulerJobRun`` state rules (create / finish / recover) are
  injected by the composition root (:mod:`app.worker`) and must not be
  duplicated here.

The top-level :mod:`app.worker` module supplies the session factory, heartbeat
ownership, shared shutdown signal, and the canonical job-run helpers.  This is a
pure structural extraction (PANJI-GOV-W2); no cron / timezone / job identity /
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
from app.services.fenced_job_run_service import (
    FencedJobHeartbeat,
    FencedJobToken,
    JobLeaseLostError,
    finalize_job_run,
)

SessionFactory = Callable[[], Any]
HeartbeatLoop = Callable[[str], Coroutine[Any, Any, None]]
ShutdownProbe = Callable[[], bool]

# Generic SchedulerJobRun state-rule collaborators, injected by the composition
# root so this module never re-implements them.
CreateJobRun = Callable[..., Coroutine[Any, Any, SchedulerJobRun | None]]
FinishJobRun = Callable[..., Coroutine[Any, Any, None]]
RecoverStaleJobRuns = Callable[[Any], Coroutine[Any, Any, int]]


async def run_bars_scheduler_worker_runtime(
    *,
    session_factory: SessionFactory,
    heartbeat_loop: HeartbeatLoop,
    should_shutdown: ShutdownProbe,
    create_job_run: CreateJobRun,
    finish_job_run: FinishJobRun,
    recover_stale_job_runs: RecoverStaleJobRuns,
    logger: logging.Logger,
) -> None:
    """Run the Bars Scheduler lifecycle.

    Behavior is identical to the previous ``run_bars_scheduler_worker`` body:
    the 15:05 after-close trigger and the 18:00 share-capital sync, both gated
    by trading-day judgment, with startup stale-run recovery and graceful
    shutdown.
    """
    _hb_task = asyncio.create_task(heartbeat_loop("bars_scheduler"))
    scheduler = AsyncIOScheduler()

    # 启动时恢复过期 running 任务
    try:
        async with session_factory() as db:
            recovered = await recover_stale_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info("Bars Scheduler 启动恢复: %d 个过期任务", recovered)
    except Exception as exc:
        logger.exception("Bars Scheduler 启动恢复异常: %s", exc)

    async def scheduled_bars_refresh() -> None:
        """[Phase8A] 定时任务：每日 15:05 创建/复用盘后编排任务。

        Phase8A 变更：15:05 不再直接刷新行情和触发 DSA，而是创建 after-close run。
        行情刷新、DSA 创建、特征计算、发布等全部由 after-close orchestrator 统一编排，
        避免双路径（bars_scheduler 直接触发 DSA vs orchestrator 内部创建 DSA）造成的
        重复执行和 race condition。

        [Gate3] 触发时间从 16:00 改为 15:05 Asia/Shanghai（收盘后 5 分钟）。
        幂等：create_after_close_run 内部基于 run_key 去重，18:30 兜底重复调用安全。
        """
        from datetime import date as date_cls

        from app.services.after_close_orchestrator import create_after_close_run
        from app.services.calendar_service import is_trading_day_async

        trade_date = date_cls.today()

        # 交易日历判断（替代简单的 weekday 判断）
        async with session_factory() as session:
            is_trading = await is_trading_day_async(session, trade_date)

        if not is_trading:
            logger.info("非交易日 %s，跳过盘后编排创建", trade_date)
            return

        logger.info("交易日 %s，15:05 创建/复用盘后编排任务", trade_date)
        try:
            async with session_factory() as db:
                job_run, is_new = await create_after_close_run(db=db, trade_date=trade_date)
                if is_new:
                    logger.info(
                        "[BarsScheduler] 15:05 已创建盘后编排任务: run_id=%s, trade_date=%s",
                        job_run.id, trade_date,
                    )
                else:
                    logger.info(
                        "[BarsScheduler] 15:05 盘后编排任务已存在（幂等）: "
                        "run_id=%s, trade_date=%s, status=%s",
                        job_run.id, trade_date, job_run.status,
                    )
        except Exception as exc:
            logger.exception(
                "[BarsScheduler] 15:05 创建盘后编排任务失败: trade_date=%s, error=%s",
                trade_date, exc,
            )

    # [Gate3] 每日 15:05 Asia/Shanghai 触发（收盘后 5 分钟；含非交易日，由内部交易日历判断是否执行）
    scheduler.add_job(
        scheduled_bars_refresh,
        CronTrigger(day_of_week="mon-sun", hour=15, minute=5, timezone=ZoneInfo("Asia/Shanghai")),
        id="bars_refresh_daily",
        replace_existing=True,
    )

    # [BoardSync] - 板块同步已迁移至 after_close_orchestrator 的 syncing_boards 步骤
    # （refreshing_daily → syncing_boards → waiting_dsa_worker）
    # 不再需要独立的 17:00 qstock 定时任务。BOARD_SYNC_ENABLED 开关由 orchestrator 读取，
    # false 时 syncing_boards 步骤标记为 skipped（不访问问财）。
    # 板块同步是软失败：失败不覆盖旧数据、不阻断 DSA/快照/发布。

    # ===== 股本同步 job（pytdx get_finance_info，每日 18:00，独立 job_name/run_key） =====
    async def scheduled_share_capital_sync() -> None:
        """定时任务：每日 18:00 同步全市场 SH/SZ 股票总股本/流通股本。

        CHANGE-20260713-010: 用于 quote 端点市值计算。
        - pytdx get_finance_info 获取 zongguben/liutongguben/updated_date
        - 写入 instruments 表 total_share/float_share/share_as_of
        - 独立于 bars_refresh，使用独立 pytdx 连接
        - 失败只记录 SchedulerJobRun，不影响下次触发
        """
        from datetime import date as date_cls

        from app.services.calendar_service import is_trading_day_async
        from app.services.instrument_share_sync_service import sync_share_capitals

        trade_date = date_cls.today()

        async with session_factory() as session:
            is_trading = await is_trading_day_async(session, trade_date)

        if not is_trading:
            logger.info("非交易日 %s，跳过股本同步", trade_date)
            return

        logger.info("交易日 %s，开始股本同步", trade_date)
        job_run = None
        try:
            async with session_factory() as db:
                scheduled_at = datetime.combine(
                    trade_date, time(18, 0), tzinfo=ZoneInfo("Asia/Shanghai")
                )
                job_run = await create_job_run(
                    db, "share_capital_sync", str(trade_date),
                    scheduled_at=scheduled_at,
                    run_key=f"share_capital_sync:{trade_date}",
                )
                if job_run is None:
                    logger.info("share_capital_sync SKIPPED_DUPLICATE business_date=%s", trade_date)
                    return
                await db.commit()

            # [C2A] 建立 fenced ownership token，脱离 unfenced 的 _finish_job_run：
            # 健康 owner 持续刷新 lease（watchdog 不会误 recovery），ownership 丢失时
            # stale owner 的 terminal 写被拒。
            if not job_run.worker_instance_id:
                raise RuntimeError(
                    f"share_capital_sync create_job_run 未设置 worker_instance_id: "
                    f"job_run_id={job_run.id}"
                )
            token = FencedJobToken(
                job_run_id=job_run.id,
                worker_instance_id=job_run.worker_instance_id,
                lease_epoch=job_run.lease_epoch,
                lease_seconds=120,
            )
            heartbeat = FencedJobHeartbeat(token, interval_seconds=30.0)
            await heartbeat.start()
            try:
                async with session_factory() as db:
                    result = await sync_share_capitals(db)

                logger.info(
                    "股本同步完成: total=%d succeeded=%d failed=%d skipped_bj=%d",
                    result["total"], result["succeeded"], result["failed"], result["skipped_bj"],
                )
                # ownership 仍持有才允许 terminal
                heartbeat.ensure_owned()
                finalized = await finalize_job_run(
                    token,
                    status="succeeded",
                    metadata_updates={},
                    total_count=result["total"],
                    succeeded_count=result["succeeded"],
                    failed_count=result["failed"],
                )
                if not finalized:
                    logger.warning(
                        "share_capital_sync 已失去 ownership，跳过 succeeded terminal 写入: "
                        "business_date=%s",
                        trade_date,
                    )
            except JobLeaseLostError:
                logger.warning(
                    "share_capital_sync lease 已丢失，跳过 terminal 写入: business_date=%s",
                    trade_date,
                )
            except Exception as exc:
                logger.exception("股本同步异常: %s", exc)
                # 异常路径：仍持有 token 才 fenced failed terminal；已丢失则仅告警，
                # 不做第二次 unfenced terminal 写。
                try:
                    heartbeat.ensure_owned()
                except JobLeaseLostError:
                    logger.warning(
                        "share_capital_sync lease 已丢失，跳过 failed terminal 写入: "
                        "business_date=%s",
                        trade_date,
                    )
                else:
                    finalized = await finalize_job_run(
                        token,
                        status="failed",
                        metadata_updates={},
                        total_count=0,
                        succeeded_count=0,
                        failed_count=0,
                        error_message=str(exc)[:500],
                    )
                    if not finalized:
                        logger.warning(
                            "share_capital_sync 已失去 ownership，跳过 failed terminal 写入: "
                            "business_date=%s",
                            trade_date,
                        )
            finally:
                await heartbeat.stop()
        except Exception as exc:
            # 建立 fenced token / 启动 heartbeat 之前的创建阶段异常：无法安全 fenced
            # terminal，仅记录（由 watchdog 合法 recovery），不再回退到 unfenced finish。
            logger.exception("股本同步创建/启动异常: %s", exc)

    scheduler.add_job(
        scheduled_share_capital_sync,
        CronTrigger(day_of_week="mon-sun", hour=18, minute=0, timezone=ZoneInfo("Asia/Shanghai")),
        id="share_capital_sync_daily",
        replace_existing=True,
        max_instances=1,  # 单并发
    )

    scheduler.start()
    logger.info("Bars Scheduler Worker 启动（16:00 刷新行情 + 17:00 板块同步 + 18:00 股本同步）")

    while not should_shutdown():
        await asyncio.sleep(60)

    scheduler.shutdown(wait=False)
    logger.info("Bars Scheduler Worker 已退出")


__all__ = ["run_bars_scheduler_worker_runtime"]
