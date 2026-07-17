"""统一 Worker 入口 - 支持 Outbox Relay / Delivery Worker / Job 消费者 / 策略批量计算 / 行情调度 / 选股策略调度 / 日历调度 / 监控调度。

用法：
    WORKER_TYPE=outbox python -m app.worker           # 运行 Outbox Relay：将 Outbox 扩张为 MessageDelivery(pending)
    WORKER_TYPE=delivery python -m app.worker         # 运行投递 Worker：按渠道执行 MessageDelivery 状态机
    WORKER_TYPE=strategy_batch python -m app.worker   # 运行策略批量计算 Worker
    WORKER_TYPE=bars_scheduler python -m app.worker   # 运行行情调度 Worker（每日 16:00 行情刷新）
    WORKER_TYPE=strategy_scheduler python -m app.worker   # 运行选股策略调度 Worker（每日 18:30，兜底机制）
    WORKER_TYPE=calendar_scheduler python -m app.worker  # 运行日历调度 Worker（每日 02:00）
    WORKER_TYPE=monitor_scheduler python -m app.worker    # 运行监控调度 Worker（交易时段 9:30-15:00）
    WORKER_TYPE=watchdog python -m app.worker          # 运行恢复看门狗（每 60s 清理僵尸任务）
    WORKER_TYPE=all python -m app.worker              # 同时运行全部（开发模式，含看门狗）

环境变量：
    WORKER_TYPE: worker 类型（outbox/delivery/strategy_batch/bars_scheduler/strategy_scheduler/calendar_scheduler/monitor_scheduler/after_close_orchestrator/watchdog/all，默认 all）
    WORKER_INTERVAL: 轮询间隔秒数（默认 5）
    WORKER_BATCH_SIZE: 单次轮询最大记录数（默认 100）
    WORKER_MAX_RETRY: 最大重试次数（默认 5）

设计：
- 每个 worker 类型在独立 asyncio task 中运行
- 信号处理：SIGTERM/SIGINT 优雅退出
- 异常不吞：捕获后记录日志并等待下次轮询（避免单次失败导致 worker 退出）
- Outbox Relay 不再直接投递渠道，而是为每个渠道创建 MessageDelivery 记录
- Delivery Worker 负责实际渠道投递与失败重试
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy_run import StrategyRun
from app.services.scheduler_job_run_recovery_service import (
    recover_stale_scheduler_job_runs,
)

logger = logging.getLogger("worker")

# Worker 配置
WORKER_TYPE = os.getenv("WORKER_TYPE", "all")
WORKER_INTERVAL = int(os.getenv("WORKER_INTERVAL", "5"))
WORKER_BATCH_SIZE = int(os.getenv("WORKER_BATCH_SIZE", "100"))
WORKER_MAX_RETRY = int(os.getenv("WORKER_MAX_RETRY", "5"))

# [WorkerHeartbeat] - 僵尸心跳清理阈值（秒）：超过此值未刷新的 running 心跳视为僵尸
# 600s = 10 个心跳周期（默认心跳间隔 60s），远大于正常抖动，避免误杀活跃 worker
STALE_HEARTBEAT_THRESHOLD_SECONDS = int(os.getenv("STALE_HEARTBEAT_THRESHOLD_SECONDS", "600"))

# 优雅退出标志
_shutdown = False

# [WorkerHeartbeat] - 实例标识：hostname:pid
_WORKER_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}"


async def _heartbeat_loop(worker_name: str, interval: int = 60) -> None:
    """后台心跳任务，每 interval 秒更新一次 worker_heartbeats。

    启动时 INSERT（若不存在），运行中 UPDATE heartbeat_at，退出时标记 stopped。
    心跳失败仅记录警告，不中断 Worker 主流程。
    """
    from sqlalchemy import select

    from app.models.worker_heartbeat import WorkerHeartbeat

    # 启动时写入初始心跳
    try:
        async with AsyncSessionLocal() as db:
            now = datetime.now(UTC)
            stmt = select(WorkerHeartbeat).where(
                WorkerHeartbeat.worker_name == worker_name,
                WorkerHeartbeat.instance_id == _WORKER_INSTANCE_ID,
            )
            result = await db.execute(stmt)
            hb = result.scalar_one_or_none()
            if hb is None:
                hb = WorkerHeartbeat(
                    worker_name=worker_name,
                    instance_id=_WORKER_INSTANCE_ID,
                    started_at=now,
                    heartbeat_at=now,
                    status="running",
                    build_sha=os.environ.get("GIT_SHA", "unknown"),
                )
                db.add(hb)
            else:
                hb.heartbeat_at = now
                hb.status = "running"
            await db.commit()
    except Exception as e:
        logger.warning("心跳初始化失败 %s: %s", worker_name, e)

    # 定期更新心跳
    while not _shutdown:
        await asyncio.sleep(interval)
        if _shutdown:
            break
        try:
            async with AsyncSessionLocal() as db:
                now = datetime.now(UTC)
                stmt = select(WorkerHeartbeat).where(
                    WorkerHeartbeat.worker_name == worker_name,
                    WorkerHeartbeat.instance_id == _WORKER_INSTANCE_ID,
                )
                result = await db.execute(stmt)
                hb = result.scalar_one_or_none()
                if hb is not None:
                    hb.heartbeat_at = now
                    hb.status = "running"
                    await db.commit()
        except Exception as e:
            logger.warning("心跳更新失败 %s: %s", worker_name, e)

    # 退出时标记 stopped
    try:
        async with AsyncSessionLocal() as db:
            stmt = select(WorkerHeartbeat).where(
                WorkerHeartbeat.worker_name == worker_name,
                WorkerHeartbeat.instance_id == _WORKER_INSTANCE_ID,
            )
            result = await db.execute(stmt)
            hb = result.scalar_one_or_none()
            if hb is not None:
                hb.status = "stopped"
                hb.heartbeat_at = datetime.now(UTC)
                await db.commit()
    except Exception as e:
        logger.warning("心跳退出标记失败 %s: %s", worker_name, e)


def _handle_shutdown(signum: int, _frame: object) -> None:
    """信号处理：设置退出标志，让主循环自然结束。"""
    global _shutdown
    logger.info("收到信号 %s，准备退出...", signum)
    _shutdown = True


async def _create_job_run(
    db: AsyncSession,
    job_name: str,
    business_date: str,
    lease_seconds: int = 120,
    metadata: dict | None = None,
    scheduled_at: datetime | None = None,
    run_key: str | None = None,
) -> SchedulerJobRun | None:
    """创建 SchedulerJobRun 记录并返回（幂等版本）。

    如果提供 run_key，则调用 idempotency_service.acquire_job_run_lock() 双保险获取执行权：
    - pg_advisory_xact_lock 序列化并发
    - 唯一索引保证只有一条记录

    未抢到锁时返回 None，调用方应立即 return 不执行业务，并 logger.info("SKIPPED_DUPLICATE")。

    如果未提供 run_key（向后兼容），保持原行为直接 INSERT。

    Args:
        scheduled_at: CronTrigger 计划执行时间；None 时退化为 started_at（非 scheduler 场景）
        run_key: 业务幂等键；提供时启用幂等模式，None 时保持原行为
    """
    if run_key is not None:
        from app.services.idempotency_service import acquire_job_run_lock
        # [Idempotency] - acquire_job_run_lock 返回 (job_run, is_new)：
        # - is_new=True：新建任务，commit 并返回 job_run
        # - is_new=False：已有活跃任务(existing)或抢锁失败(None)，返回 None（调用方 SKIPPED_DUPLICATE）
        job_run, is_new = await acquire_job_run_lock(
            db=db,
            run_key=run_key,
            job_name=job_name,
            business_date=business_date,
            lease_seconds=lease_seconds,
            scheduled_at=scheduled_at,
            metadata=metadata,
            worker_instance_id=_WORKER_INSTANCE_ID,
        )
        if is_new and job_run is not None:
            await db.commit()
            await db.refresh(job_run)
            return job_run
        # is_new=False：已有活跃任务或抢锁失败，调用方应 SKIPPED_DUPLICATE
        # 注意：不 commit，acquire_job_run_lock 内部 recover_stale UPDATE 由抢到锁的事务统一 commit
        return None

    # 向后兼容：无 run_key 时保持原行为直接 INSERT
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    job_run = SchedulerJobRun(
        job_name=job_name,
        business_date=business_date,
        status="running",
        scheduled_at=scheduled_at if scheduled_at is not None else now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        worker_instance_id=_WORKER_INSTANCE_ID,
        metadata_json=json.dumps(metadata) if metadata else None,
    )
    db.add(job_run)
    await db.commit()
    await db.refresh(job_run)
    return job_run


async def _finish_job_run(
    db: AsyncSession,
    job_run: SchedulerJobRun,
    status: str,
    error_message: str | None = None,
    success_count: int | None = None,
    failure_count: int | None = None,
) -> None:
    """更新 SchedulerJobRun 记录为完成状态。

    通过 job_run.id 重新查询，兼容跨 session 的 detached 对象。
    调用后 status 变为 succeeded/failed/interrupted，并记录 finished_at。
    """
    from sqlalchemy import select

    stmt = select(SchedulerJobRun).where(SchedulerJobRun.id == job_run.id)
    result = await db.execute(stmt)
    attached = result.scalar_one_or_none()
    if attached is None:
        logger.warning("SchedulerJobRun id=%s 不存在，跳过更新", job_run.id)
        return
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    attached.status = status
    attached.finished_at = now
    attached.heartbeat_at = now
    attached.lease_expires_at = now  # 结束任务后租约立即过期
    attached.error_message = error_message
    if success_count is not None:
        attached.succeeded_count = success_count
    if failure_count is not None:
        attached.failed_count = failure_count
    await db.commit()


async def _update_job_heartbeat(
    db: AsyncSession,
    job_run: SchedulerJobRun,
    lease_seconds: int = 120,
) -> None:
    """长任务执行期间更新心跳与租约。

    每 30 秒调用一次，防止 Admin 页面误判为任务卡死或租约过期。
    """
    from sqlalchemy import select

    stmt = select(SchedulerJobRun).where(SchedulerJobRun.id == job_run.id)
    result = await db.execute(stmt)
    attached = result.scalar_one_or_none()
    if attached is None:
        logger.warning("SchedulerJobRun id=%s 不存在，跳过半程心跳", job_run.id)
        return
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    attached.heartbeat_at = now
    attached.lease_expires_at = now + timedelta(seconds=lease_seconds)
    await db.commit()


async def run_outbox_relay() -> None:
    """Outbox Relay worker：轮询 outbox 表，将通知扩张为每个渠道的 MessageDelivery。

    每个轮询周期：
    1. 从 outbox 表读取 status=pending 的记录
    2. 查询通知的目标渠道
    3. 为每个渠道创建 MessageDelivery(pending)
    4. 将 Outbox 记录标记为 processed
    """
    from app.services.outbox_relay import relay_outbox

    _hb_task = asyncio.create_task(_heartbeat_loop("outbox"))
    logger.info("Outbox Relay worker 启动（间隔=%ds, 批次=%d）", WORKER_INTERVAL, WORKER_BATCH_SIZE)
    while not _shutdown:
        try:
            async with AsyncSessionLocal() as db:
                processed = await relay_outbox(
                    db=db,
                    batch_size=WORKER_BATCH_SIZE,
                    max_retry=WORKER_MAX_RETRY,
                )
                await db.commit()
                if processed > 0:
                    logger.info("Outbox Relay 处理 %d 条", processed)
        except Exception as exc:
            logger.exception("Outbox Relay 异常: %s", exc)
        await asyncio.sleep(WORKER_INTERVAL)


async def run_delivery_worker() -> None:
    """投递 Worker：轮询 MessageDelivery 表，将通知消息投递到用户渠道。

    每个轮询周期：
    1. 从 message_deliveries 表读取 pending / 到期的 retrying 记录
    2. 调用 _execute_delivery 执行投递状态机
    3. 成功后 status=success；失败后 status=retrying/dead
    """
    from app.services.delivery_worker import process_pending_deliveries

    _hb_task = asyncio.create_task(_heartbeat_loop("delivery"))
    logger.info("Delivery Worker 启动（间隔=%ds, 批次=%d）", WORKER_INTERVAL, WORKER_BATCH_SIZE)
    while not _shutdown:
        try:
            async with AsyncSessionLocal() as db:
                processed = await process_pending_deliveries(
                    db=db,
                    batch_size=WORKER_BATCH_SIZE,
                    max_retry=WORKER_MAX_RETRY,
                )
                await db.commit()
                if processed > 0:
                    logger.info("Delivery Worker 处理 %d 条", processed)
        except Exception as exc:
            logger.exception("Delivery Worker 异常: %s", exc)
        await asyncio.sleep(WORKER_INTERVAL)


async def _maybe_trigger_after_close_orchestrator(
    db: AsyncSession,
    run: StrategyRun,
) -> None:
    """[AfterCloseAutoTrigger] - DSA scheduled 完成后自动触发盘后编排。

    仅当 strategy_key == 'dsa_selector' 时触发。
    create_after_close_run 是幂等的：同 trade_date 已有 queued/running 任务时返回已有任务。

    异常处理：触发失败仅记录日志，不传播异常，避免影响 worker 主流程
    （execute_run 已完成，auto-trigger 失败不应导致 worker 崩溃）。

    Args:
        db: 异步数据库会话（execute_run 已 commit，session 干净）
        run: 已完成的 StrategyRun
    """
    from sqlalchemy import select

    from app.constants.strategy_keys import DSA_SELECTOR
    from app.models.strategy import StrategyDefinition, StrategyVersion
    from app.services.after_close_orchestrator import create_after_close_run

    # 查询 strategy_key（通过 strategy_version_id 关联）
    stmt = (
        select(StrategyDefinition.strategy_key)
        .select_from(StrategyRun)
        .join(
            StrategyVersion,
            StrategyRun.strategy_version_id == StrategyVersion.id,
        )
        .join(
            StrategyDefinition,
            StrategyVersion.strategy_definition_id == StrategyDefinition.id,
        )
        .where(StrategyRun.id == run.id)
    )
    result = await db.execute(stmt)
    strategy_key = result.scalar_one_or_none()

    if strategy_key != DSA_SELECTOR:
        return

    trade_date = run.trade_date
    if trade_date is None:
        logger.warning(
            "[AfterCloseAutoTrigger] DSA run 缺少 trade_date，跳过: run_id=%s",
            run.id,
        )
        return

    try:
        job_run, is_new = await create_after_close_run(db=db, trade_date=trade_date)
        if is_new:
            logger.info(
                "[AfterCloseAutoTrigger] DSA 完成后自动触发盘后编排: "
                "dsa_run_id=%s, trade_date=%s, after_close_run_id=%s",
                run.id, trade_date, job_run.id,
            )
        else:
            logger.info(
                "[AfterCloseAutoTrigger] 盘后编排任务已存在（幂等）: "
                "dsa_run_id=%s, trade_date=%s, after_close_run_id=%s, status=%s",
                run.id, trade_date, job_run.id, job_run.status,
            )
    except Exception as exc:
        # 触发失败不传播异常：execute_run 已完成，auto-trigger 失败不应影响 worker
        logger.exception(
            "[AfterCloseAutoTrigger] 触发盘后编排失败（不影响 worker 主流程）: "
            "dsa_run_id=%s, trade_date=%s, error=%s",
            run.id, trade_date, exc,
        )


async def run_strategy_batch_worker() -> None:
    """策略批量计算 Worker：轮询 queued 状态的运行并执行。

    每个轮询周期：
    1. 查询 strategy_runs WHERE status='queued'（按 queued_at 排序，取 1 条）
    2. 调用 StrategyBatchService.execute_run() 执行
    3. 提交事务

    设计说明：
    - 单 run 串行执行（避免并发计算同一策略版本）
    - 执行失败时记录日志，run 状态由 execute_run 内部处理
    - Worker 重启后可继续执行 queued 状态的 run（中断恢复）
    - 启动时调用 recover_stale_runs() 恢复过期租约的 running 任务
    """
    from app.services.strategy_batch_service import StrategyBatchService

    _hb_task = asyncio.create_task(_heartbeat_loop("strategy_batch"))
    logger.info(
        "Strategy Batch Worker 启动（间隔=%ds）", WORKER_INTERVAL
    )
    service = StrategyBatchService()

    # 启动时恢复过期租约的 running 和 stale queued 任务
    try:
        async with AsyncSessionLocal() as db:
            recovered = await service.recover_stale_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info(
                    "Strategy Batch Worker 启动恢复: %d 个过期任务", recovered,
                )
    except Exception as exc:
        logger.exception("Strategy Batch Worker 启动恢复异常: %s", exc)

    while not _shutdown:
        try:
            async with AsyncSessionLocal() as db:
                # [StrategyBatchWorker] - 使用 claim_next_run 加锁领取任务，避免多 Worker 竞争
                run = await service.claim_next_run(db)
                if run is None:
                    # 无待执行 run，等待下次轮询
                    await asyncio.sleep(WORKER_INTERVAL)
                    continue

                await db.commit()
                logger.info(
                    "开始执行策略批量计算: run_id=%s, trade_date=%s",
                    run.id, run.trade_date,
                )
                await service.execute_run(db, run.id)
                await db.commit()
                logger.info(
                    "策略批量计算完成: run_id=%s, status=%s",
                    run.id, run.status,
                )
                # [AfterCloseAutoTrigger] - DSA scheduled 完成后自动触发盘后编排
                # 仅对 scheduled + completed 的 run 触发，manual run 或失败 run 不触发
                if run.status == "completed" and run.run_type == "scheduled":
                    await _maybe_trigger_after_close_orchestrator(db, run)
        except Exception as exc:
            logger.exception("Strategy Batch Worker 异常: %s", exc)
            # 异常时回滚，等待下次轮询重试
            try:
                await db.rollback()
            except Exception:
                pass
        await asyncio.sleep(WORKER_INTERVAL)


async def run_bars_scheduler_worker() -> None:
    """行情调度 Worker：每日 16:00 触发全市场多周期行情更新 + 17:00 板块同步。

    使用 APScheduler AsyncIOScheduler + CronTrigger：
    - 每个交易日（周一至周五）16:00 触发行情刷新
    - 每日 17:00 触发板块同步（qstock，独立 job_name/run_key，不阻塞行情主流水线）
    - qstock 同步调用通过 asyncio.to_thread 包装，不阻塞事件循环

    设计说明：
    - APScheduler 在事件循环中运行，不阻塞
    - 两个 job 独立调度，失败互不影响（board_sync 失败保留旧板块数据）
    - board_sync 使用 max_instances=1 实现单并发
    - 信号处理：收到 SIGTERM/SIGINT 后优雅关闭 scheduler
    - 异常不吞：捕获后记录日志，不影响下次触发
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    from app.services.bars_scheduler_service import BarsSchedulerService

    _hb_task = asyncio.create_task(_heartbeat_loop("bars_scheduler"))
    scheduler = AsyncIOScheduler()
    service = BarsSchedulerService()

    # 启动时恢复过期 running 任务
    try:
        async with AsyncSessionLocal() as db:
            recovered = await recover_stale_scheduler_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info("Bars Scheduler 启动恢复: %d 个过期任务", recovered)
    except Exception as exc:
        logger.exception("Bars Scheduler 启动恢复异常: %s", exc)

    async def scheduled_bars_refresh() -> None:
        """定时任务：每日 16:00 刷新全市场多周期行情。"""
        from datetime import date as date_cls

        from app.services.calendar_service import is_trading_day_async

        trade_date = date_cls.today()

        # 交易日历判断（替代简单的 weekday 判断）
        async with AsyncSessionLocal() as session:
            is_trading = await is_trading_day_async(session, trade_date)

        if not is_trading:
            logger.info("非交易日 %s，跳过行情刷新", trade_date)
            return

        logger.info("交易日 %s，开始行情刷新", trade_date)
        job_run = None
        heartbeat_task_ref: asyncio.Task | None = None
        try:
            async with AsyncSessionLocal() as db:
                # [BarsScheduler] - scheduled_at 为 CronTrigger 计划时间（16:00），不等于 started_at
                scheduled_at = datetime.combine(
                    trade_date, time(16, 0), tzinfo=ZoneInfo("Asia/Shanghai")
                )
                job_run = await _create_job_run(
                    db, "bars_scheduler", str(trade_date), scheduled_at=scheduled_at,
                    run_key=f"bars_scheduler:{trade_date}",
                )
                if job_run is None:
                    logger.info("bars_scheduler SKIPPED_DUPLICATE business_date=%s", trade_date)
                    return
                # [JobRunEvent] - 任务开始写入 START 事件
                from app.services.job_run_event_service import append_event
                await append_event(
                    db=db,
                    job_run_id=job_run.id,
                    step="START",
                    level="info",
                    message="开始更新日线",
                )
                await db.commit()

            # 行情刷新耗时约 1.8 小时，后台每 30 秒更新心跳与租约
            async def _bars_heartbeat_loop() -> None:
                while True:
                    await asyncio.sleep(30)
                    async with AsyncSessionLocal() as db:
                        if job_run is not None:
                            await _update_job_heartbeat(db, job_run)

            heartbeat_task_ref = asyncio.create_task(_bars_heartbeat_loop())
            # [JobRunEvent] - 传入 job_run_id 让 service 在日线阶段完成后写 DAILY_DONE/DSA_CREATED
            result = await service.refresh_all_instruments(
                trade_date, job_run_id=job_run.id,
            )
            if heartbeat_task_ref is not None:
                heartbeat_task_ref.cancel()
                try:
                    await heartbeat_task_ref
                except asyncio.CancelledError:
                    pass
            logger.info(
                "定时任务完成: total=%d succeeded=%d failed=%d period_counts=%s",
                result.total, result.succeeded, result.failed, result.period_counts,
            )
            if job_run is not None:
                async with AsyncSessionLocal() as db:
                    job_run = await db.get(SchedulerJobRun, job_run.id)
                    if job_run is not None:
                        # [BarsScheduler] - 记录 strategy_run_id 和 last_bar_time 到 metadata_json
                        meta: dict[str, object] = {}
                        if result.dsa_run_id is not None:
                            meta["strategy_run_id"] = str(result.dsa_run_id)
                        # 查询业务日最新 15min bar 的 trade_time 作为 last_bar_time
                        try:
                            from datetime import date as date_cls

                            from sqlalchemy import func as sa_func
                            from sqlalchemy import select as sa_select

                            from app.models.bar import Bar15Min

                            bd = job_run.business_date
                            if bd:
                                bd_date = date_cls.fromisoformat(bd)
                                latest_bt = await db.scalar(
                                    sa_select(sa_func.max(Bar15Min.trade_time)).where(
                                        Bar15Min.trade_time >= bd_date,
                                        Bar15Min.trade_time < bd_date + timedelta(days=1),
                                    )
                                )
                                if latest_bt is not None:
                                    meta["last_bar_time"] = latest_bt.isoformat()
                        except Exception as exc:
                            logger.debug("查询 latest bar trade_time 失败: %s", exc)
                        if meta:
                            job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
                        await _finish_job_run(
                            db, job_run, "succeeded",
                            success_count=result.succeeded,
                            failure_count=result.failed,
                        )
        except Exception as exc:
            logger.exception("定时任务异常: %s", exc)
            if heartbeat_task_ref is not None:
                heartbeat_task_ref.cancel()
                try:
                    await heartbeat_task_ref
                except asyncio.CancelledError:
                    pass
            if job_run is not None:
                async with AsyncSessionLocal() as db:
                    # [JobRunEvent] - 任务失败写入 ERROR 事件（含 traceback）
                    import traceback as tb_mod

                    from app.services.job_run_event_service import append_event
                    await append_event(
                        db=db,
                        job_run_id=job_run.id,
                        step="ERROR",
                        level="error",
                        message=str(exc)[:500],
                        payload={
                            "traceback": tb_mod.format_exc()[:4000],
                            "error_type": type(exc).__name__,
                        },
                    )
                    await _finish_job_run(db, job_run, "failed", error_message=str(exc)[:500])

    # 每日 16:00 触发（含非交易日，由内部交易日历判断是否执行）
    scheduler.add_job(
        scheduled_bars_refresh,
        CronTrigger(day_of_week="mon-sun", hour=16, minute=0, timezone=ZoneInfo("Asia/Shanghai")),
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

        async with AsyncSessionLocal() as session:
            is_trading = await is_trading_day_async(session, trade_date)

        if not is_trading:
            logger.info("非交易日 %s，跳过股本同步", trade_date)
            return

        logger.info("交易日 %s，开始股本同步", trade_date)
        job_run = None
        try:
            async with AsyncSessionLocal() as db:
                scheduled_at = datetime.combine(
                    trade_date, time(18, 0), tzinfo=ZoneInfo("Asia/Shanghai")
                )
                job_run = await _create_job_run(
                    db, "share_capital_sync", str(trade_date),
                    scheduled_at=scheduled_at,
                    run_key=f"share_capital_sync:{trade_date}",
                )
                if job_run is None:
                    logger.info("share_capital_sync SKIPPED_DUPLICATE business_date=%s", trade_date)
                    return
                await db.commit()

            async with AsyncSessionLocal() as db:
                result = await sync_share_capitals(db)

            logger.info(
                "股本同步完成: total=%d succeeded=%d failed=%d skipped_bj=%d",
                result["total"], result["succeeded"], result["failed"], result["skipped_bj"],
            )
            if job_run is not None:
                async with AsyncSessionLocal() as db:
                    await _finish_job_run(
                        db, job_run, "succeeded",
                        success_count=result["succeeded"],
                        failure_count=result["failed"],
                    )
        except Exception as exc:
            logger.exception("股本同步异常: %s", exc)
            if job_run is not None:
                async with AsyncSessionLocal() as db:
                    await _finish_job_run(db, job_run, "failed", error_message=str(exc)[:500])

    scheduler.add_job(
        scheduled_share_capital_sync,
        CronTrigger(day_of_week="mon-sun", hour=18, minute=0, timezone=ZoneInfo("Asia/Shanghai")),
        id="share_capital_sync_daily",
        replace_existing=True,
        max_instances=1,  # 单并发
    )

    scheduler.start()
    logger.info("Bars Scheduler Worker 启动（16:00 刷新行情 + 17:00 板块同步 + 18:00 股本同步）")

    while not _shutdown:
        await asyncio.sleep(60)

    scheduler.shutdown(wait=False)
    logger.info("Bars Scheduler Worker 已退出")


async def run_strategy_scheduler_worker() -> None:
    """选股策略调度 Worker（兜底机制）：每日 18:30 触发所有 kind=selector 策略的批量计算。

    使用 APScheduler AsyncIOScheduler + CronTrigger：
    - 每个交易日 18:30 触发（比 bars 16:00 晚 2.5 小时，作为兜底）
    - 查询 strategy_definitions WHERE kind='selector' 的所有策略
    - 调用 StrategyBatchService.create_batch_run(run_type="scheduled")
      创建或复用当日的 run（create_batch_run 内部统一去重/重试）
    - 创建/复用的 queued run 由 strategy_batch worker 轮询执行

    设计说明：
    - 18:30 触发（bars_scheduler 16:00 刷新行情，日线完成后自动触发 DSA，
      本调度器作为兜底，防止日线触发失败时遗漏）
    - 去重：create_batch_run 内部基于 (version, date, run_type) 与 attempt_no 幂等，
      本函数不再手动检查今日是否已有 run
    - 数据就绪检查：check_data_readiness() 覆盖率 < 90% 时阻断 DSA 执行
    - 单个策略创建失败不阻塞其他策略，记录日志继续
    - 完成状态：按 succeeded/failed 计数映射为 succeeded/partial_failed/failed
    - 幂等：create_batch_run 内部 idempotency_key 也保证去重
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from sqlalchemy import exists, select

    from app.models.strategy import StrategyDefinition, StrategyVersion
    from app.services.strategy_batch_service import StrategyBatchService

    _hb_task = asyncio.create_task(_heartbeat_loop("strategy_scheduler"))
    scheduler = AsyncIOScheduler()
    service = StrategyBatchService()

    # 启动时恢复过期 running 任务
    try:
        async with AsyncSessionLocal() as db:
            recovered = await recover_stale_scheduler_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info("Strategy Scheduler 启动恢复: %d 个过期任务", recovered)
    except Exception as exc:
        logger.exception("Strategy Scheduler 启动恢复异常: %s", exc)

    async def scheduled_strategy_run() -> None:
        """定时任务：每日 18:30 为所有 selector 策略创建/复用 queued run（兜底）。"""
        from datetime import date as date_cls

        from app.services.calendar_service import is_trading_day_async

        trade_date = date_cls.today()

        # 交易日历判断（替代简单的 weekday 判断）
        async with AsyncSessionLocal() as session:
            is_trading = await is_trading_day_async(session, trade_date)

        if not is_trading:
            logger.info("非交易日 %s，跳过选股策略计算", trade_date)
            return

        logger.info("交易日 %s，开始选股策略计算（兜底调度）", trade_date)
        job_run = None
        try:
            async with AsyncSessionLocal() as db:
                # [StrategyScheduler] - scheduled_at 为 CronTrigger 计划时间（18:30），不等于 started_at
                scheduled_at = datetime.combine(
                    trade_date, time(18, 30), tzinfo=ZoneInfo("Asia/Shanghai")
                )
                job_run = await _create_job_run(
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
                    await _finish_job_run(
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
                        # create_batch_run 内部统一处理新建/复用/重试
                        run = await service.create_batch_run(
                            db=db,
                            strategy_key=strategy_key,
                            trade_date=trade_date,
                            run_type="scheduled",
                        )
                        await db.commit()
                        strategy_run_ids.append(str(run.id))
                        # [StrategyScheduler] - 无论新建还是复用，均记录 strategy_run_id
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
                        await _update_job_heartbeat(db, job_run)

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
                await _finish_job_run(
                    db, job_run, final_status,
                    success_count=succeeded, failure_count=failed,
                )
        except Exception as exc:
            logger.exception("选股策略调度任务异常: %s", exc)
            if job_run is not None:
                async with AsyncSessionLocal() as db:
                    await _finish_job_run(db, job_run, "failed", error_message=str(exc)[:500])

    # 每日 18:30 触发（含非交易日，由内部交易日历判断是否执行；18:30 作为兜底，日线触发优先）
    scheduler.add_job(
        scheduled_strategy_run,
        CronTrigger(day_of_week="mon-sun", hour=18, minute=30, timezone=ZoneInfo("Asia/Shanghai")),
        id="strategy_run_daily",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Strategy Scheduler Worker 启动（每日 18:30 触发，兜底机制）")

    while not _shutdown:
        await asyncio.sleep(60)

    scheduler.shutdown(wait=False)
    logger.info("Strategy Scheduler Worker 已退出")


async def run_calendar_scheduler_worker() -> None:
    """日历调度 Worker：每日 02:00 从 Mootdx 拉取本年及下一年交易日历并更新 DB。

    使用 APScheduler AsyncIOScheduler + CronTrigger：
    - 每日 02:00 触发
    - 调用 seed_calendar_from_mootdx(session, year=当前年份) 与下一年
    - 更新或插入交易日历记录
    - Mootdx 失败时保留旧值并报警（异常上抛，不覆盖历史记录）

    设计说明：
    - APScheduler 在事件循环中运行，不阻塞
    - 信号处理：收到 SIGTERM/SIGINT 后优雅关闭 scheduler
    - 异常不吞：捕获后记录日志，不影响下次触发
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    _hb_task = asyncio.create_task(_heartbeat_loop("calendar_scheduler"))
    scheduler = AsyncIOScheduler()

    # 启动时恢复过期 running 任务
    try:
        async with AsyncSessionLocal() as db:
            recovered = await recover_stale_scheduler_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info("Calendar Scheduler 启动恢复: %d 个过期任务", recovered)
    except Exception as exc:
        logger.exception("Calendar Scheduler 启动恢复异常: %s", exc)

    async def calendar_job() -> None:
        """每日凌晨刷新交易日历（从 Mootdx 拉取当年及下一年日历并更新 DB）。"""
        from app.core.time import shanghai_business_date

        today = shanghai_business_date()
        job_run = None
        try:
            async with AsyncSessionLocal() as session:
                # [CalendarScheduler] - scheduled_at 为 CronTrigger 计划时间（02:00），不等于 started_at
                scheduled_at = datetime.combine(
                    today, time(2, 0), tzinfo=ZoneInfo("Asia/Shanghai")
                )
                job_run = await _create_job_run(
                    session, "calendar_scheduler", str(today), scheduled_at=scheduled_at,
                    run_key=f"calendar_scheduler:{today}",
                )
                if job_run is None:
                    logger.info("calendar_scheduler SKIPPED_DUPLICATE business_date=%s", today)
                    return
                from app.services.calendar_seed import seed_calendar_from_mootdx
                total_count = 0
                for year in (today.year, today.year + 1):
                    count = await seed_calendar_from_mootdx(session, year=year, force=False)
                    total_count += count
                    logger.info("日历刷新完成: year=%d, %d 条记录更新", year, count)
                await _finish_job_run(session, job_run, "succeeded", success_count=1)
        except Exception as exc:
            logger.error("日历刷新失败: %s", exc)
            if job_run is not None:
                async with AsyncSessionLocal() as db:
                    await _finish_job_run(db, job_run, "failed", error_message=str(exc)[:500])
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

    while not _shutdown:
        await asyncio.sleep(60)

    scheduler.shutdown(wait=False)
    logger.info("Calendar Scheduler Worker 已退出")


def _get_monitor_session(
    now_cst: datetime,
) -> tuple[str, time, time] | None:
    """根据当前上海时间返回盘中交易时段标签与起止时间。

    Returns:
        (label, start_time, end_time) 或 None（非交易时段）
    """
    from datetime import time as time_cls

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
    db: AsyncSession,
    now_cst: datetime,
    business_date: str,
    session_label: str,
) -> SchedulerJobRun | None:
    """查找或创建当前交易时段的 monitor_scheduler job_run（幂等版本）。

    基于 run_key=monitor_scheduler:{business_date}:{session_label} 唯一索引保证 session 幂等。
    返回 SchedulerJobRun 表示新建；返回 None 表示 session 已存在（调用方应按 run_key 查询复用）。
    """
    run_key = f"monitor_scheduler:{business_date}:{session_label}"
    return await _create_job_run(
        db,
        "monitor_scheduler",
        business_date,
        lease_seconds=120,
        metadata={"session_label": session_label},
        run_key=run_key,
    )


async def run_monitor_scheduler_worker() -> None:
    """监控调度 Worker：交易时段内每 30 秒执行一轮监控。

    使用 APScheduler AsyncIOScheduler + 交易时段判断：
    - 交易日 9:30-11:30：每 30 秒执行一轮（同一 session 只创建一条 SchedulerJobRun）
    - 午休 11:30-13:00：暂停
    - 交易日 13:00-15:00：每 30 秒执行一轮（同一 session 只创建一条 SchedulerJobRun）
    - 非交易日：不执行

    调用 MonitorBatchService.execute_monitor_cycle() 执行单轮监控。

    设计说明：
    - 不使用 CronTrigger（需要精确到秒级的循环控制）
    - 使用 while 循环 + asyncio.sleep(30) 实现交易时段内循环
    - 交易日检查：复用 services/calendar_service.is_trading_day()
    - 午休暂停：11:30-13:00 期间 sleep 等待
    - session 聚合：每个上午/下午只创建一条 SchedulerJobRun，session 内更新
      last_cycle_at、succeeded_count、failed_count
    - 优雅退出：检查 _shutdown 标志
    """
    from datetime import time as time_cls

    from app.services.monitor_batch_service import MonitorBatchService

    _hb_task = asyncio.create_task(_heartbeat_loop("monitor_scheduler"))
    service = MonitorBatchService()
    cycle_interval = 30  # 秒
    session_finish_margin = timedelta(seconds=cycle_interval + 5)

    # [eval_recovery] 启动时恢复过期租约的 PENDING 评估
    async with AsyncSessionLocal() as db:
        recovered = await service.recover_stale_evaluations(db)
        await db.commit()
        if recovered > 0:
            logger.info("Monitor Worker 启动恢复: %d 个过期评估", recovered)

    # 启动时恢复过期的 monitor_scheduler running 任务
    try:
        async with AsyncSessionLocal() as db:
            recovered = await recover_stale_scheduler_job_runs(db)
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
    await _notify_monitor_status("监控服务已启动", "交易时段 9:30-11:30 / 13:00-15:00\n每 30 秒执行一轮监控")

    while not _shutdown:
        job_run = None
        try:
            from datetime import datetime

            now = datetime.now(ZoneInfo("Asia/Shanghai"))

            # 交易日检查（使用异步接口，避免在事件循环中降级到 weekday）
            from app.services.calendar_service import is_trading_day_async

            async with AsyncSessionLocal() as db:
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
            async with AsyncSessionLocal() as db:
                job_run = await _find_or_create_monitor_session_job_run(
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
                try:
                    result = await service.execute_monitor_cycle(db)
                    await db.commit()
                    cycle_succeeded = True
                    if result.total_events_written > 0:
                        logger.info(
                            "监控周期完成: session=%s instruments=%d events=%d notifications=%d",
                            session_label,
                            result.total_instruments,
                            result.total_events_written,
                            result.total_notifications_created,
                        )
                    else:
                        logger.debug(
                            "监控周期完成: session=%s instruments=%d events=0",
                            session_label,
                            result.total_instruments,
                        )
                except Exception as exc:
                    logger.exception("Monitor Scheduler 周期异常: %s", exc)
                    await db.rollback()

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
                    await _finish_job_run(
                        db, job_run, "succeeded",
                        success_count=job_run.succeeded_count,
                        failure_count=job_run.failed_count,
                    )

        except Exception as exc:
            logger.exception("Monitor Scheduler 异常: %s", exc)
            if job_run is not None:
                async with AsyncSessionLocal() as db:
                    await _finish_job_run(db, job_run, "failed", error_message=str(exc)[:500])
            # 异常退出飞书通知
            await _notify_monitor_status("监控服务异常", str(exc), is_error=True)

        # 交易时段内每 30 秒一轮
        await asyncio.sleep(cycle_interval)

    logger.info("Monitor Scheduler Worker 已退出")


# [monitor_scheduler] - 启动通知幂等降级缓存：Redis 不可用时使用进程内 set
_monitor_start_notified: set[str] = set()


async def _notify_monitor_status(
    title: str, content: str, *, is_error: bool = False,
) -> None:
    """发送监控状态通知（启动/异常）。

    启动通知（is_error=False）：
    - 幂等：基于 monitor-start:{git_sha} 键（Redis SET NX EX 7天，降级进程内 set）
      避免每次 Worker 重启都给管理员发重复启动通知
    - 仅发送给 admin 角色用户的渠道（运维事件不混淆为交易信号）

    异常通知（is_error=True）：
    - 发送给所有活跃飞书渠道（监控异常影响所有用户信号生成）
    - 不做幂等（每次异常都应通知）

    通知失败不影响主流程（仅记录警告）。
    使用 message_type="SYSTEM_ALERT" + template_key="system_alert" 构造通知。

    TODO: [monitor_scheduler] 当前直接调用 adapter.send() 绕过 Outbox 管道。
    应改为 create_message → write_outbox(notification.message.created) → Delivery Worker 投递，
    与业务通知保持一致的投递语义（重试、幂等、静默时段）。风险：监控服务自身异常时
    Outbox/Delivery Worker 可能也不可用，需评估是否保留直接发送作为降级路径。
    """
    try:
        from sqlalchemy import select

        from app.core.time import now_shanghai
        from app.models.notification import NotificationChannel
        from app.models.user import Role, UserRole
        from app.schemas.notification import NotificationMessageDTO
        from app.services.channel_adapter import get_adapter

        emoji = "❌" if is_error else "✅"

        # 启动通知幂等检查：monitor-start:{git_sha}（7 天 TTL）
        if not is_error:
            git_sha = os.environ.get("GIT_SHA", "unknown")
            idem_key = f"monitor-start:{git_sha}"
            try:
                from app.core.redis_client import get_redis

                redis = get_redis()
                acquired = await redis.set(idem_key, "1", nx=True, ex=7 * 86400)
                if not acquired:
                    logger.info(
                        "monitor startup notification already sent for %s", git_sha
                    )
                    return
            except Exception as e:
                logger.warning("Redis 幂等检查失败，降级为进程内幂等: %s", e)
                if git_sha in _monitor_start_notified:
                    logger.info(
                        "monitor startup notification already sent for %s (in-process)",
                        git_sha,
                    )
                    return
                _monitor_start_notified.add(git_sha)

        async with AsyncSessionLocal() as db:
            # 查询活跃的飞书平台应用渠道
            # 启动通知仅发送给 admin 角色用户；异常通知发送给所有用户
            stmt = select(NotificationChannel).where(
                NotificationChannel.adapter_type == "feishu_platform_app",
                NotificationChannel.status == "active",
            )
            if not is_error:
                admin_user_ids_subq = (
                    select(UserRole.user_id)
                    .join(Role, Role.id == UserRole.role_id)
                    .where(Role.name == "admin")
                )
                stmt = stmt.where(
                    NotificationChannel.user_id.in_(admin_user_ids_subq)
                )
            result = await db.execute(stmt)
            channels = list(result.scalars().all())

            if not channels:
                logger.debug("无活跃飞书渠道，跳过监控状态通知")
                return

            for channel in channels:
                try:
                    adapter = get_adapter(channel.adapter_type)
                    dto = NotificationMessageDTO(
                        title=f"{emoji} {title}",
                        message_type="SYSTEM_ALERT",
                        template_key="system_alert",
                        template_version="1.1.0",
                        summary=content[:200],
                        data_time=now_shanghai().isoformat(),
                        resource_refs={},
                    )
                    delivery = await adapter.send(dto, channel.target_config)
                    if delivery.success:
                        logger.info("监控状态通知已发送: %s -> user=%s", title, channel.user_id)
                    else:
                        logger.warning(
                            "监控状态通知发送失败: %s -> user=%s: %s",
                            title, channel.user_id, delivery.error_message,
                        )
                except Exception as e:
                    logger.warning("监控状态通知发送异常: user=%s: %s", channel.user_id, e)

    except Exception as e:
        logger.warning("监控状态通知整体失败: %s", e)


async def mark_stale_worker_heartbeats(
    db: AsyncSession,
    now: datetime | None = None,
    threshold_seconds: int = STALE_HEARTBEAT_THRESHOLD_SECONDS,
) -> int:
    """[WorkerHeartbeat] - 将 status='running' 但 heartbeat_at 过旧的僵尸心跳标记为 stopped。

    覆盖场景：容器被 SIGKILL（无 SIGTERM graceful shutdown）时，_heartbeat_loop
    无法执行退出清理，worker_heartbeats 表残留 status='running' 记录，导致
    管理员看到的 Worker 状态不可信。

    设计说明：
    - 只 UPDATE status='running' AND heartbeat_at < now - threshold 的记录为 'stopped'
    - 不删除历史记录，保留 started_at/heartbeat_at/build_sha 供审计
    - 不 commit（由调用方控制事务，与 recover_stale_scheduler_job_runs 模式一致）
    - 不吞异常：数据库异常向上传播
    - 使用 timezone-aware UTC（与 _heartbeat_loop 一致）
    - 幂等：status 已是 stopped 的记录不会被重复处理（WHERE status='running'）

    Args:
        db: 异步数据库会话（不 commit，由调用方控制事务）
        now: 当前时间（默认 UTC now），可注入用于测试
        threshold_seconds: 僵尸判定阈值（秒），默认 STALE_HEARTBEAT_THRESHOLD_SECONDS=600

    Returns:
        被标记为 stopped 的记录数量

    Raises:
        Exception: 数据库执行异常向上传播（不吞异常）
    """
    from sqlalchemy import text

    if now is None:
        now = datetime.now(UTC)

    heartbeat_cutoff = now - timedelta(seconds=threshold_seconds)

    # [WorkerHeartbeat] - 原子 UPDATE：status running -> stopped
    # 使用 RETURNING + fetchall() + len() 计数（与 recover_stale_scheduler_job_runs 模式一致），
    # 避免 mypy 对 Result.rowcount 的 attr-defined 误报
    update_sql = text(
        """
        UPDATE worker_heartbeats
        SET status = 'stopped'
        WHERE status = 'running'
            AND heartbeat_at < :heartbeat_cutoff
        RETURNING worker_name
        """
    )
    result = await db.execute(update_sql, {"heartbeat_cutoff": heartbeat_cutoff})
    marked_rows = result.fetchall()
    marked_count = len(marked_rows)

    if marked_count > 0:
        logger.info(
            "[WorkerHeartbeat] 标记 %d 个僵尸心跳为 stopped（阈值=%ds）",
            marked_count, threshold_seconds,
        )

    return marked_count


async def _recovery_watchdog_loop(interval_seconds: int = 60) -> None:
    """[Recovery] - 后台看门狗：每 interval_seconds 调用 recover_stale_scheduler_job_runs 和 mark_stale_worker_heartbeats。

    覆盖场景：API 不重启但任务租约自然过期、Worker 被杀后无容器重启。
    与各 Worker 启动恢复互补：启动恢复只在上次崩溃残留时执行一次，
    看门狗持续运行，捕获运行期间产生的僵尸任务。

    设计说明：
    - 默认 60s 间隔，覆盖 lease 过期（120s）与 heartbeat 超时（90s）两种场景
    - recover_stale_scheduler_job_runs 不 commit，本函数调用后立即 commit
    - mark_stale_worker_heartbeats 同事务内执行，清理 worker_heartbeats 僵尸记录（阈值 600s）
    - 异常不退出：recover/heartbeat/commit 失败仅记录日志，下个周期继续重试
    - _shutdown 为 True 时退出循环（由信号处理设置）
    """
    _hb_task = asyncio.create_task(_heartbeat_loop("recovery_watchdog"))
    logger.info("[Recovery] 看门狗启动（间隔=%ds）", interval_seconds)
    while not _shutdown:
        try:
            async with AsyncSessionLocal() as db:
                recovered = await recover_stale_scheduler_job_runs(db)
                stale_marked = await mark_stale_worker_heartbeats(db)
                await db.commit()
                if recovered > 0:
                    logger.info("[Recovery] 看门狗恢复: %d 个过期任务", recovered)
                if stale_marked > 0:
                    logger.info("[Recovery] 看门狗清理: %d 个僵尸心跳", stale_marked)
        except Exception as exc:
            logger.exception("[Recovery] 看门狗异常: %s", exc)
        await asyncio.sleep(interval_seconds)


async def _after_close_poll_once() -> bool:
    """[AfterCloseWorker] - 单次轮询：领取并执行一个 queued 盘后编排任务。

    使用 SELECT ... FOR UPDATE SKIP LOCKED 领取任务，多个 Worker 实例只有一个能领取。
    领取后更新 status='running' + worker_instance_id + heartbeat + lease，
    然后调用 execute_after_close_run（含断点恢复 + 心跳更新）。

    Returns:
        True 如果领取到任务（无论执行成功与否），False 如果无 queued 任务
    """
    from datetime import date as date_cls

    from sqlalchemy import select

    from app.services.after_close_orchestrator import (
        _ORCHESTRATOR_LEASE_SECONDS,
        execute_after_close_run,
    )

    async with AsyncSessionLocal() as db:
        # [AfterCloseWorker] - FOR UPDATE SKIP LOCKED 领取一个 queued 任务
        stmt = (
            select(SchedulerJobRun)
            .where(
                SchedulerJobRun.job_name == "after_close_orchestrator",
                SchedulerJobRun.status == "queued",
            )
            .order_by(SchedulerJobRun.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        result = await db.execute(stmt)
        job_run = result.scalar_one_or_none()

        if job_run is None:
            # 无 queued 任务，释放锁（rollback 释放 FOR UPDATE 锁）
            await db.rollback()
            return False

        # 领取任务：更新 status='running' + worker + heartbeat + lease
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        job_run.status = "running"
        job_run.worker_instance_id = _WORKER_INSTANCE_ID
        if job_run.started_at is None:
            job_run.started_at = now
        job_run.heartbeat_at = now
        job_run.lease_expires_at = now + timedelta(seconds=_ORCHESTRATOR_LEASE_SECONDS)
        await db.commit()

        # 提取 trade_date（expire_on_commit=False 让 commit 后属性仍可用）
        meta = json.loads(job_run.metadata_json) if job_run.metadata_json else {}
        trade_date_str = meta.get("trade_date")
        job_run_id = job_run.id

    if not trade_date_str:
        # advice.md: 任务缺 trade_date 必须立即写 ERROR 事件 + status=failed + finished_at + 释放 run_key
        # 禁止只记日志留 running 僵尸
        logger.error(
            "[AfterCloseWorker] 任务缺少 trade_date，标记 failed: job_run_id=%s", job_run_id,
        )
        async with AsyncSessionLocal() as db:
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
    try:
        await execute_after_close_run(
            job_run_id=job_run_id,
            trade_date=trade_date,
            worker_id=_WORKER_INSTANCE_ID,
        )
    except Exception as exc:
        logger.exception(
            "[AfterCloseWorker] 执行异常: job_run_id=%s, error=%s", job_run_id, exc,
        )
        # execute_after_close_run 内部已标记 failed，此处仅记录不 re-raise

    return True


async def run_after_close_orchestrator_worker() -> None:
    """[AfterCloseWorker] - 盘后编排独立 Worker：领取 queued 任务并执行。

    使用 FOR UPDATE SKIP LOCKED 领取任务，多个 Worker 实例只有一个能领取。
    每个轮询周期：
    1. 启动恢复（清理上次崩溃残留的 running 任务）
    2. _after_close_poll_once 领取并执行一个 queued 任务
    3. sleep WORKER_INTERVAL 后继续轮询

    设计说明：
    - execute_after_close_run 内部含断点恢复 + 心跳更新，Worker 仅负责领取和调度
    - 异常不退出：execute_after_close_run 内部标记 failed 后 re-raise，
      Worker 捕获仅记录日志，等待下次轮询
    """
    _hb_task = asyncio.create_task(_heartbeat_loop("after_close_orchestrator"))
    logger.info(
        "[AfterCloseWorker] 启动（间隔=%ds）", WORKER_INTERVAL,
    )

    # 启动恢复：清理上次崩溃残留的 running 任务
    try:
        async with AsyncSessionLocal() as db:
            recovered = await recover_stale_scheduler_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info(
                    "[AfterCloseWorker] 启动恢复: %d 个过期任务", recovered,
                )
    except Exception as exc:
        logger.exception("[AfterCloseWorker] 启动恢复异常: %s", exc)

    while not _shutdown:
        try:
            await _after_close_poll_once()
        except Exception as exc:
            # _after_close_poll_once 内部已捕获 execute_after_close_run 异常，
            # 此处仅捕获领取阶段的意外异常
            logger.exception("[AfterCloseWorker] 轮询异常: %s", exc)
        await asyncio.sleep(WORKER_INTERVAL)


async def main() -> None:
    """主入口：根据 WORKER_TYPE 启动对应的 worker。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info("Worker 启动: type=%s, interval=%ds", WORKER_TYPE, WORKER_INTERVAL)

    # 注册信号处理
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    tasks: list[asyncio.Task] = []

    if WORKER_TYPE in ("outbox", "all"):
        tasks.append(asyncio.create_task(run_outbox_relay()))

    if WORKER_TYPE in ("delivery", "all"):
        tasks.append(asyncio.create_task(run_delivery_worker()))

    if WORKER_TYPE in ("strategy_batch", "all"):
        tasks.append(asyncio.create_task(run_strategy_batch_worker()))

    if WORKER_TYPE in ("bars_scheduler", "all"):
        tasks.append(asyncio.create_task(run_bars_scheduler_worker()))

    if WORKER_TYPE in ("strategy_scheduler", "all"):
        tasks.append(asyncio.create_task(run_strategy_scheduler_worker()))

    if WORKER_TYPE in ("calendar_scheduler", "all"):
        tasks.append(asyncio.create_task(run_calendar_scheduler_worker()))

    if WORKER_TYPE in ("monitor_scheduler", "all"):
        tasks.append(asyncio.create_task(run_monitor_scheduler_worker()))

    # [Phase5] - 盘后编排独立 Worker：领取 queued 任务并执行（断点恢复 + 心跳租约）
    if WORKER_TYPE in ("after_close_orchestrator", "all"):
        tasks.append(asyncio.create_task(run_after_close_orchestrator_worker()))

    # [Recovery] - 看门狗：all 模式自动启动，或 WORKER_TYPE=watchdog 单独启动
    if WORKER_TYPE in ("watchdog", "all"):
        tasks.append(asyncio.create_task(_recovery_watchdog_loop()))

    if not tasks:
        logger.error("未知 WORKER_TYPE: %s", WORKER_TYPE)
        return

    # 等待所有 worker 退出
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Worker 已退出")


if __name__ == "__main__":
    # 自测入口：验证模块导入与配置（不连接 DB/Redis）
    print(f"WORKER_TYPE={WORKER_TYPE}")
    print(f"WORKER_INTERVAL={WORKER_INTERVAL}")
    print(f"WORKER_BATCH_SIZE={WORKER_BATCH_SIZE}")
    print(f"WORKER_MAX_RETRY={WORKER_MAX_RETRY}")
    assert WORKER_TYPE in ("outbox", "delivery", "strategy_batch", "bars_scheduler", "strategy_scheduler", "calendar_scheduler", "monitor_scheduler", "after_close_orchestrator", "watchdog", "all"), \
        f"未知 WORKER_TYPE: {WORKER_TYPE}"
    # 验证 worker 函数可调用
    assert callable(run_outbox_relay), "run_outbox_relay 应可调用"
    assert callable(run_delivery_worker), "run_delivery_worker 应可调用"
    assert callable(run_strategy_batch_worker), "run_strategy_batch_worker 应可调用"
    assert callable(run_bars_scheduler_worker), "run_bars_scheduler_worker 应可调用"
    assert callable(run_strategy_scheduler_worker), "run_strategy_scheduler_worker 应可调用"
    assert callable(run_calendar_scheduler_worker), "run_calendar_scheduler_worker 应可调用"
    assert callable(run_monitor_scheduler_worker), "run_monitor_scheduler_worker 应可调用"
    assert callable(run_after_close_orchestrator_worker), "run_after_close_orchestrator_worker 应可调用"
    assert callable(_recovery_watchdog_loop), "_recovery_watchdog_loop 应可调用"
    print("OK: 配置验证通过")
    asyncio.run(main())
