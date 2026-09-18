"""Strategy scheduler worker lifecycle, separate from the process composition root.

This module owns ONLY the Strategy Scheduler polling/scheduling lifecycle:
APScheduler setup, the daily 18:30 ``scheduled_strategy_run`` closure, startup
stale-run recovery, and graceful shutdown.  It does NOT re-implement any business
rule:

* Trading-day judgment lives in
  :func:`app.services.calendar_service.is_trading_day_async`.
* The selector strategy query (kind=selector / environment=production /
  is_scheduled / released version exists) is run verbatim against the production
  models and must not be rewritten here.
* DSA routing (``DSA_SELECTOR``) lives in
  :func:`app.services.after_close_orchestrator.create_after_close_run`.
* Non-DSA selector batch creation lives in
  :class:`app.services.strategy_batch_service.StrategyBatchService`.
* Mid-loop heartbeat/lease renewal lives in the injected
  ``update_job_heartbeat`` (the composition root's
  :func:`app.worker._update_job_heartbeat`).
* The generic ``SchedulerJobRun`` state rules (create / finish / recover) are
  injected by the composition root (:mod:`app.worker`) and must not be
  duplicated here.

The top-level :mod:`app.worker` module supplies the session factory, heartbeat
ownership, shared shutdown signal, the canonical job-run helpers, and the
heartbeat updater.  This is a pure structural extraction (PANJI-GOV-W3); no
cron / timezone / job identity / trading-day boundary / duplicate short-circuit /
selector SQL / DSA-vs-non-DSA routing / commit order / metadata content /
rollback / ValueError-vs-Exception / heartbeat cadence / final-status mapping /
transaction / shutdown / logging / exception semantics changed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import exists, select

from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.services.strategy_batch_service import StrategyBatchService

SessionFactory = Callable[[], Any]
HeartbeatLoop = Callable[[str], Coroutine[Any, Any, None]]
ShutdownProbe = Callable[[], bool]

# Generic SchedulerJobRun state-rule collaborators, injected by the composition
# root so this module never re-implements them.
CreateJobRun = Callable[..., Coroutine[Any, Any, SchedulerJobRun | None]]
FinishJobRun = Callable[..., Coroutine[Any, Any, None]]
RecoverStaleJobRuns = Callable[[Any], Coroutine[Any, Any, int]]
UpdateJobHeartbeat = Callable[..., Coroutine[Any, Any, None]]


async def run_strategy_scheduler_worker_runtime(
    *,
    session_factory: SessionFactory,
    heartbeat_loop: HeartbeatLoop,
    should_shutdown: ShutdownProbe,
    create_job_run: CreateJobRun,
    finish_job_run: FinishJobRun,
    update_job_heartbeat: UpdateJobHeartbeat,
    recover_stale_job_runs: RecoverStaleJobRuns,
    logger: logging.Logger,
) -> None:
    """Run the Strategy Scheduler lifecycle.

    Behavior is identical to the previous ``run_strategy_scheduler_worker`` body:
    the 18:30 fallback trigger that (re)creates the after-close run for
    ``DSA_SELECTOR`` and schedules a batch run for every other production selector
    strategy, gated by trading-day judgment, with startup stale-run recovery and
    graceful shutdown.
    """
    _hb_task = asyncio.create_task(heartbeat_loop("strategy_scheduler"))
    scheduler = AsyncIOScheduler()
    service = StrategyBatchService()

    # 启动时恢复过期 running 任务
    try:
        async with session_factory() as db:
            recovered = await recover_stale_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info("Strategy Scheduler 启动恢复: %d 个过期任务", recovered)
    except Exception as exc:
        logger.exception("Strategy Scheduler 启动恢复异常: %s", exc)

    async def scheduled_strategy_run() -> None:
        """[Phase8A] 定时任务：每日 18:30 兜底创建/复用 after-close run + 非 DSA selector run。

        Phase8A 变更：
        - DSA_SELECTOR: 创建/复用 after-close run（幂等，16:00 已创建则跳过）
          DSA 由 after-close orchestrator 内部创建和 inline claim，不再直接创建 DSA batch_run
        - 非 DSA selector: 仍走原 strategy_batch worker 路径创建 batch_run
        """
        from datetime import date as date_cls

        from app.constants.strategy_keys import DSA_SELECTOR
        from app.services.after_close_orchestrator import create_after_close_run
        from app.services.calendar_service import is_trading_day_async

        trade_date = date_cls.today()

        # 交易日历判断（替代简单的 weekday 判断）
        async with session_factory() as session:
            is_trading = await is_trading_day_async(session, trade_date)

        if not is_trading:
            logger.info("非交易日 %s，跳过选股策略计算", trade_date)
            return

        logger.info("交易日 %s，开始选股策略计算（兜底调度）", trade_date)
        job_run = None
        try:
            async with session_factory() as db:
                # [StrategyScheduler] - scheduled_at 为 CronTrigger 计划时间（18:30），不等于 started_at
                scheduled_at = datetime.combine(
                    trade_date, time(18, 30), tzinfo=ZoneInfo("Asia/Shanghai")
                )
                job_run = await create_job_run(
                    db, "strategy_scheduler", str(trade_date), scheduled_at=scheduled_at,
                    run_key=f"strategy_scheduler:{trade_date}",
                )
                if job_run is None:
                    logger.info("strategy_scheduler SKIPPED_DUPLICATE business_date=%s", trade_date)
                    return
                # 查询 production 环境 + 参与调度 + 有 released 版本的 selector 策略
                released_subq = (
                    select(StrategyVersion.id)
                    .where(
                        StrategyVersion.strategy_definition_id == StrategyDefinition.id,
                        StrategyVersion.status == "released",
                    )
                    .limit(1)
                    .correlate(StrategyDefinition)
                )
                stmt = select(StrategyDefinition.strategy_key).where(
                    StrategyDefinition.kind == "selector",
                    StrategyDefinition.environment == "production",
                    StrategyDefinition.is_scheduled == True,  # noqa: E712
                    exists(released_subq),
                )
                result = await db.execute(stmt)
                strategy_keys = [row[0] for row in result.fetchall()]

                if not strategy_keys:
                    logger.warning("未找到 kind=selector 的策略")
                    await finish_job_run(
                        db, job_run, "failed",
                        error_message="未找到 kind=selector 的策略",
                    )
                    return

                logger.info("待计算的 selector 策略: %s", strategy_keys)
                succeeded = 0
                failed = 0
                strategy_run_ids: list[str] = []
                for idx, strategy_key in enumerate(strategy_keys):
                    try:
                        if strategy_key == DSA_SELECTOR:
                            # [Phase8A] DSA 走 after-close orchestrator 路径
                            # 创建/复用 after-close run（幂等），DSA 由 orchestrator 内部创建
                            after_close_run, is_new = await create_after_close_run(
                                db=db, trade_date=trade_date,
                            )
                            await db.commit()
                            strategy_run_ids.append(str(after_close_run.id))
                            job_run.metadata_json = json.dumps({
                                "after_close_run_id": str(after_close_run.id),
                                "strategy_run_ids": strategy_run_ids,
                            })
                            await db.commit()
                            logger.info(
                                "[StrategyScheduler] DSA after-close run 创建/复用: "
                                "run_id=%s, is_new=%s, trade_date=%s",
                                after_close_run.id, is_new, trade_date,
                            )
                        else:
                            # 非 DSA selector: 仍走原 strategy_batch worker 路径
                            run = await service.create_batch_run(
                                db=db,
                                strategy_key=strategy_key,
                                trade_date=trade_date,
                                run_type="scheduled",
                            )
                            await db.commit()
                            strategy_run_ids.append(str(run.id))
                            job_run.metadata_json = json.dumps({
                                "strategy_run_id": str(run.id),
                                "strategy_run_ids": strategy_run_ids,
                            })
                            await db.commit()
                            logger.info(
                                "策略 %s 创建/复用 run 成功: run_id=%s",
                                strategy_key, run.id,
                            )
                        succeeded += 1
                    except ValueError as exc:
                        # 非交易日/数据未就绪/策略无可用版本
                        logger.warning(
                            "策略 %s 创建 run 跳过: %s", strategy_key, exc
                        )
                        await db.rollback()
                        failed += 1
                    except Exception as exc:
                        logger.exception(
                            "策略 %s 创建 run 异常: %s", strategy_key, exc
                        )
                        await db.rollback()
                        failed += 1

                    # 每 30 秒更新一次心跳与租约（兜底调度可能持续较长时间）
                    if idx % 5 == 4:
                        await update_job_heartbeat(db, job_run)

                logger.info(
                    "定时任务完成（兜底）: total=%d succeeded=%d failed=%d",
                    len(strategy_keys), succeeded, failed,
                )
                # [StrategyScheduler] - 按 succeeded/failed 计数映射最终状态
                if failed == 0:
                    final_status = "succeeded"
                elif succeeded > 0:
                    final_status = "partial_failed"
                else:
                    final_status = "failed"
                await finish_job_run(
                    db, job_run, final_status,
                    success_count=succeeded, failure_count=failed,
                )
        except Exception as exc:
            logger.exception("选股策略调度任务异常: %s", exc)
            if job_run is not None:
                async with session_factory() as db:
                    await finish_job_run(db, job_run, "failed", error_message=str(exc)[:500])

    # 每日 18:30 触发（含非交易日，由内部交易日历判断是否执行；18:30 作为兜底，日线触发优先）
    scheduler.add_job(
        scheduled_strategy_run,
        CronTrigger(day_of_week="mon-sun", hour=18, minute=30, timezone=ZoneInfo("Asia/Shanghai")),
        id="strategy_run_daily",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Strategy Scheduler Worker 启动（每日 18:30 触发，兜底机制）")

    while not should_shutdown():
        await asyncio.sleep(60)

    scheduler.shutdown(wait=False)
    logger.info("Strategy Scheduler Worker 已退出")


__all__ = ["run_strategy_scheduler_worker_runtime"]
