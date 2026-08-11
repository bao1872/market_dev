"""统一 Worker 入口 - 支持 Outbox Relay / Delivery Worker / Job 消费者 / 策略批量计算 / 行情调度 / 选股策略调度 / 日历调度 / 监控调度。

用法：
    WORKER_TYPE=outbox python -m app.worker           # 运行 Outbox Relay：将 Outbox 扩张为 MessageDelivery(pending)
    WORKER_TYPE=delivery python -m app.worker         # 运行投递 Worker：按渠道执行 MessageDelivery 状态机
    WORKER_TYPE=strategy_batch python -m app.worker   # 运行策略批量计算 Worker
    WORKER_TYPE=bars_scheduler python -m app.worker   # 运行行情调度 Worker（每日 16:00 行情刷新）
    WORKER_TYPE=strategy_scheduler python -m app.worker   # 运行选股策略调度 Worker（每日 18:30，兜底机制）
    WORKER_TYPE=calendar_scheduler python -m app.worker  # 运行日历调度 Worker（每日 02:00）
    WORKER_TYPE=monitor_scheduler python -m app.worker    # 运行监控调度 Worker（交易时段 9:30-15:00）
    WORKER_TYPE=after_close_orchestrator python -m app.worker  # 运行盘后编排 Worker（断点恢复 + 心跳租约）
    WORKER_TYPE=chip_consensus python -m app.worker   # 运行盘后筹码共识 Worker（[P0-3] 独立 poll + 断点续算）
    WORKER_TYPE=auction_scheduler python -m app.worker  # [P0-3] 运行竞价分析调度 Worker（09:25/10:00 触发）
    WORKER_TYPE=watchdog python -m app.worker          # 运行恢复看门狗（每 60s 清理僵尸任务）
    WORKER_TYPE=all python -m app.worker              # 同时运行全部（开发模式，含看门狗）

环境变量：
    WORKER_TYPE: worker 类型（outbox/delivery/strategy_batch/bars_scheduler/strategy_scheduler/calendar_scheduler/monitor_scheduler/after_close_orchestrator/chip_consensus/auction_scheduler/watchdog/all，默认 all）
    WORKER_INTERVAL: 轮询间隔秒数（默认 5）
    WORKER_BATCH_SIZE: 单次轮询最大记录数（默认 100）
    WORKER_MAX_RETRY: 最大重试次数（默认 5）

设计：
- 每个 worker 类型在独立 asyncio task 中运行
- 信号处理：SIGTERM/SIGINT 优雅退出
- 异常不吞：捕获后记录日志并等待下次轮询（避免单次失败导致 worker 退出）
- Outbox Relay 不再直接投递渠道，而是为每个渠道创建 MessageDelivery 记录
- Delivery Worker 负责实际渠道投递与失败重试
- [P0-3] chip_consensus 与 after_close_orchestrator 可在同一容器运行（独立 WORKER_TYPE 分支）

架构（2026-08-11 AFTER-CLOSE-ENHANCEMENT-HEAD-OF-LINE-BLOCKING 修复后）：
- `run_after_close_orchestrator_worker`（WORKER_TYPE=after_close_orchestrator，生产唯一入口）
  只负责 mandatory after-close orchestrator 主循环（`_after_close_poll_once`）。
- Chip consensus 以**独立 co-process** 在同一进程内运行（复用 `run_chip_consensus_worker`），
  拥有自己的执行 loop，绝不串行阻塞 mandatory orchestrator。
- Auction Scheduler 以独立 co-process 运行（`_run_auction_scheduler_co_process`）。
- 三个 loop 各自独立 `while not _shutdown`，共享 `_shutdown`，SIGTERM 时统一 drain。
- 禁止恢复"每轮 core → chip → bootstrap 串行 fallback"的旧结构 —— 那会让长时 chip
  任务占用 mandatory executor，造成 head-of-line blocking。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import uuid
from datetime import UTC, datetime, time, timedelta
from time import monotonic as _time_monotonic
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.scheduler_job_run_recovery_service import (
    auto_resume_interrupted_after_close_runs,
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

    # 退出时标记 stopped（Gate4: 写入 stopped_at，不再覆盖 heartbeat_at）
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
                hb.stopped_at = datetime.now(UTC)  # Gate4: 停止时间单独记录，heartbeat_at 保留最后一次心跳
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
                # [Phase8A] 删除 _maybe_trigger_after_close_orchestrator 自动触发路径：
                # 旧路径在 DSA completed 后才创建 after-close run（倒序），
                # Phase8A 改为 16:00/18:30 先创建 after-close run，orchestrator 内部创建 DSA。
                # manual DSA 和非 DSA selector 仍由 strategy_batch worker 正常执行。
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

    _hb_task = asyncio.create_task(_heartbeat_loop("bars_scheduler"))
    scheduler = AsyncIOScheduler()

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
        async with AsyncSessionLocal() as session:
            is_trading = await is_trading_day_async(session, trade_date)

        if not is_trading:
            logger.info("非交易日 %s，跳过盘后编排创建", trade_date)
            return

        logger.info("交易日 %s，15:05 创建/复用盘后编排任务", trade_date)
        try:
            async with AsyncSessionLocal() as db:
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
    """监控调度 Worker：交易时段内每 INTRADAY_MONITOR_POLL_SECONDS 秒执行一轮监控。

    [盘中监控1秒] - cycle_interval 由 config.intraday_monitor_poll_seconds 控制（默认1秒）
    DSA/SMC/Node 重算由 monitor_evaluations 表 exactly-once 去重保证：
    新 1m bar 完成才重算，否则跳过（return early）。
    上一周期未完成则跳过，不重入。

    使用 APScheduler AsyncIOScheduler + 交易时段判断：
    - 交易日 9:30-11:30：每 poll_seconds 秒执行一轮（同一 session 只创建一条 SchedulerJobRun）
    - 午休 11:30-13:00：暂停
    - 交易日 13:00-15:00：每 poll_seconds 秒执行一轮（同一 session 只创建一条 SchedulerJobRun）
    - 非交易日：不执行

    调用 MonitorBatchService.execute_monitor_cycle() 执行单轮监控。

    设计说明：
    - 不使用 CronTrigger（需要精确到秒级的循环控制）
    - 使用 while 循环 + asyncio.sleep(poll_seconds) 实现交易时段内循环
    - 交易日检查：复用 services/calendar_service.is_trading_day()
    - 午休暂停：11:30-13:00 期间 sleep 等待
    - session 聚合：每个上午/下午只创建一条 SchedulerJobRun，session 内更新
      last_cycle_at、succeeded_count、failed_count
    - 优雅退出：检查 _shutdown 标志
    """
    from datetime import time as time_cls

    from app.config import get_settings
    from app.services.monitor_batch_service import MonitorBatchService

    _hb_task = asyncio.create_task(_heartbeat_loop("monitor_scheduler"))
    service = MonitorBatchService()
    cycle_interval = get_settings().intraday_monitor_poll_seconds  # [盘中监控1秒] 默认1秒
    session_finish_margin = timedelta(seconds=cycle_interval + 5)
    _cycle_running = False  # 防重入标志

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
    await _notify_monitor_status(
        "监控服务已启动",
        f"交易时段 9:30-11:30 / 13:00-15:00\n每 {cycle_interval} 秒执行一轮监控",
    )

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
            # [盘中监控1秒] - 防重入：上一周期未完成则跳过
            if _cycle_running:
                logger.debug("monitor_scheduler 上一周期未完成，跳过本轮")
                await asyncio.sleep(cycle_interval)
                continue

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
                _cycle_running = True  # [盘中监控1秒] 设置防重入标志
                _cycle_start_ts = _time_monotonic()
                try:
                    result = await service.execute_monitor_cycle(db)
                    await db.commit()
                    cycle_succeeded = True
                    _cycle_latency = _time_monotonic() - _cycle_start_ts
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

    # [WorkerHeartbeat] - 原子 UPDATE：status running -> stopped（Gate4: 同步写入 stopped_at）
    # 使用 RETURNING + fetchall() + len() 计数（与 recover_stale_scheduler_job_runs 模式一致），
    # 避免 mypy 对 Result.rowcount 的 attr-defined 误报
    update_sql = text(
        """
        UPDATE worker_heartbeats
        SET status = 'stopped',
            stopped_at = :now
        WHERE status = 'running'
            AND heartbeat_at < :heartbeat_cutoff
        RETURNING worker_name
        """
    )
    result = await db.execute(update_sql, {"heartbeat_cutoff": heartbeat_cutoff, "now": now})
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
                # [PRD §4.3 JOB-01] 自动恢复 interrupted 的盘后任务 → resume_queued
                resumed = await auto_resume_interrupted_after_close_runs(db)
                stale_marked = await mark_stale_worker_heartbeats(db)
                await db.commit()
                if recovered > 0:
                    logger.info("[Recovery] 看门狗恢复: %d 个过期任务", recovered)
                if resumed > 0:
                    logger.info("[Recovery] 看门狗自动恢复: %d 个 interrupted 盘后任务", resumed)
                if stale_marked > 0:
                    logger.info("[Recovery] 看门狗清理: %d 个僵尸心跳", stale_marked)
        except Exception as exc:
            logger.exception("[Recovery] 看门狗异常: %s", exc)
        await asyncio.sleep(interval_seconds)


async def _after_close_poll_once() -> bool:
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

    async with AsyncSessionLocal() as db:
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
        job_run.worker_instance_id = _WORKER_INSTANCE_ID
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
    # [JOB-02] 传递 lease_epoch 使 execute_after_close_run 启用 fenced UPDATE
    try:
        await execute_after_close_run(
            job_run_id=job_run_id,
            trade_date=trade_date,
            worker_id=_WORKER_INSTANCE_ID,
            lease_epoch=current_lease_epoch,
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

    [P0-3 2026-07-31] Auction Scheduler 接入：
    - 在本 Worker 进程内启动同进程 Auction co-process 任务（_auction_co_process_task）
    - 不新建 Docker 容器，复用 after_close_orchestrator 容器
    - Auction 轮询独立于 core/chip 领取：每 AUCTION_SCHEDULER_POLL_INTERVAL（30s）一次
    - Auction 轮询异常隔离在 co-process 内，不影响主 Worker
    - SIGTERM 时 _shutdown=True，co-process 检查后退出，主 Worker await drain

    [2026-08-11 AFTER-CLOSE-ENHANCEMENT-HEAD-OF-LINE-BLOCKING] Chip 独立 co-process：
    - 本进程同时启动 Chip consensus co-process（_chip_co_process_task），复用已有
      run_chip_consensus_worker（其内部每轮调用 _chip_consensus_poll_once）。
    - mandatory 主循环**只**领取/执行 after_close_orchestrator，
      不再串行 fallback 到 _chip_consensus_poll_once。
    - 因此一个长时 chip 任务不得占用 mandatory after-close / Review 唯一 executor：
      当新的 after_close orchestrator 进入 queued / resume_queued 时，主循环可直接领取，
      无需等待 chip 任务自然完成（executor-level execution isolation）。
    - Chip co-process 独立 while 循环、共享 _shutdown、异常隔离在 co-process 内。
    - SIGTERM 时 mandatory 与两个 co-process（Auction / Chip）统一 drain。

    [SIGTERM drain] - 优雅退出（不强制中断当前 run）：
    - SIGTERM/SIGINT 由 _handle_shutdown 设置 _shutdown=True（全局标志）
    - 主循环在领取新任务前检查 _shutdown，若为 True 则不再领取新 item
    - 当前正在执行的 execute_after_close_run 完成后才退出（同步 await，不强制中断）；
      checkpoint（run status + heartbeat）由 execute_after_close_run 内部写入
    - Auction co-process 同步退出（_shutdown 标志共享）
    - 完成后立即退出（不再 sleep），退出码 0（main 自然退出）
    - 日志: "SIGTERM drain complete, finished current item"
    """
    _hb_task = asyncio.create_task(_heartbeat_loop("after_close_orchestrator"))
    logger.info(
        "[AfterCloseWorker] 启动（间隔=%ds）", WORKER_INTERVAL,
    )

    # [P0-3 2026-07-31] 启动 Auction Scheduler co-process（同进程，共享 _shutdown）
    # 不新建容器；独立轮询 09:25/10:00 触发窗口和 queued auction jobs
    _auction_co_process_task = asyncio.create_task(_run_auction_scheduler_co_process())
    logger.info(
        "[AfterCloseWorker] Auction Scheduler co-process 已启动（生产入口，无需单独 WORKER_TYPE=auction_scheduler）",
    )

    # [2026-08-11 AFTER-CLOSE-ENHANCEMENT-HEAD-OF-LINE-BLOCKING]
    # 启动 Chip consensus 独立 co-process（同进程，共享 _shutdown）。
    # 复用已有 run_chip_consensus_worker（内部独立 while 循环每轮 _chip_consensus_poll_once）。
    # mandatory 主循环不再串行 fallback 到 _chip_consensus_poll_once，
    # 因此长时 chip 任务不会阻塞 mandatory after-close / Review executor。
    _chip_co_process_task = asyncio.create_task(run_chip_consensus_worker())
    logger.info(
        "[AfterCloseWorker] Chip consensus co-process 已启动（独立执行 loop，不阻塞 mandatory orchestrator）",
    )

    # 启动恢复：清理上次崩溃残留的 running 任务 + 自动恢复 interrupted 任务
    try:
        async with AsyncSessionLocal() as db:
            recovered = await recover_stale_scheduler_job_runs(db)
            # [PRD §4.3 JOB-01] 自动将 interrupted 的盘后任务转为 resume_queued
            resumed = await auto_resume_interrupted_after_close_runs(db)
            await db.commit()
            if recovered > 0 or resumed > 0:
                logger.info(
                    "[AfterCloseWorker] 启动恢复: %d 个过期任务, %d 个自动恢复",
                    recovered, resumed,
                )
    except Exception as exc:
        logger.exception("[AfterCloseWorker] 启动恢复异常: %s", exc)

    try:
        while not _shutdown:
            try:
                # [2026-08-11 AFTER-CLOSE-ENHANCEMENT-HEAD-OF-LINE-BLOCKING]
                # mandatory 主循环只领取/执行 after_close_orchestrator。
                # Chip 已由独立 co-process 执行，不再在此串行 fallback。
                # Review bootstrap 仍作为最低优先级回填（不抢占盘后主链），
                # 其自身 executor isolation 另行登记（见 guide FIX C，本轮不改）。
                claimed = await _after_close_poll_once()
                if not claimed:
                    claimed = await _review_bootstrap_poll_once()
            except Exception as exc:
                # _after_close_poll_once / _review_bootstrap_poll_once
                # 内部已捕获执行异常，此处仅捕获领取阶段的意外异常
                logger.exception("[AfterCloseWorker] 轮询异常: %s", exc)
            if _shutdown:
                # [SIGTERM drain] 当前 run 已完成（或无任务），不再领取新 item
                # checkpoint（run status + heartbeat）已由 execute_after_close_run 内部写入
                logger.info("[AfterCloseWorker] SIGTERM drain: 不再领取新任务，准备退出")
                break
            await asyncio.sleep(WORKER_INTERVAL)
    finally:
        # [SIGTERM drain] 确保 Auction co-process 退出
        # co-process 检查 _shutdown 后自然退出，此处 await 确保不遗留悬空任务
        if not _auction_co_process_task.done():
            try:
                await asyncio.wait_for(_auction_co_process_task, timeout=35)
            except TimeoutError:
                _auction_co_process_task.cancel()
                try:
                    await _auction_co_process_task
                except asyncio.CancelledError:
                    pass
            except Exception as exc:
                logger.warning("[AfterCloseWorker] Auction co-process 退出异常: %s", exc)

        # [SIGTERM drain] 确保 Chip consensus co-process 退出（共享 _shutdown）
        if not _chip_co_process_task.done():
            try:
                await asyncio.wait_for(_chip_co_process_task, timeout=35)
            except TimeoutError:
                _chip_co_process_task.cancel()
                try:
                    await _chip_co_process_task
                except asyncio.CancelledError:
                    pass
            except Exception as exc:
                logger.warning("[AfterCloseWorker] Chip co-process 退出异常: %s", exc)

    # [SIGTERM drain complete] - 当前 item 已完成，worker 正常退出（退出码 0）
    logger.info("[AfterCloseWorker] SIGTERM drain complete, finished current item")


async def _chip_consensus_poll_once() -> bool:
    """[ChipConsensusWorker] - 单次轮询：领取并执行一个 queued/resume_queued chip consensus 任务。

    使用 SELECT ... FOR UPDATE SKIP LOCKED 领取任务，多个 Worker 实例只有一个能领取。
    领取后更新 status='running' + worker_instance_id + heartbeat + lease + lease_epoch（fencing）。
    然后调用 execute_after_close_chip_consensus（含断点续算）。

    [P0-3 ref/instruction.md §二.3] chip 任务有执行者：
    - 在现有 after-close worker 容器内增加独立 poll 函数和 WORKER_TYPE 分支
    - 不新增常驻容器
    - 使用 FOR UPDATE SKIP LOCKED、lease_epoch、heartbeat、断点续算
    - chip 失败不反改 core（execute_after_close_chip_consensus 内部已隔离）

    断点续算：
    - get_pending_chip_instruments 过滤已 succeeded 的 instrument
    - resume_queued 任务只重试未成功项
    - 部分成功写 metadata.chip_status=partial，主 status=succeeded

    Returns:
        True 如果领取到任务（无论执行成功与否），False 如果无 queued/resume_queued 任务
    """
    from datetime import date as date_cls

    from app.schemas.first_pyramid import CHIP_CONSENSUS_ALGORITHM_VERSION
    from app.services.after_close_chip_consensus_service import (
        _CHIP_LEASE_SECONDS,
        CHIP_CONSENSUS_JOB_NAME,
        execute_after_close_chip_consensus,
        get_pending_chip_instruments,
    )
    from app.services.chip_consensus_run_lifecycle import (
        META_CHIP_RUN_ID,
        finalize_chip_run,
        resolve_or_create_chip_run,
    )
    from app.services.feature_snapshot_service import (
        get_active_a_share_instruments,
    )
    from app.services.fenced_job_run_service import (
        FencedJobHeartbeat,
        JobLeaseLostError,
        claim_next_job_run,
        finalize_job_run,
        merge_job_run_metadata,
    )

    async with AsyncSessionLocal() as db:
        claim = await claim_next_job_run(
            db,
            job_name=CHIP_CONSENSUS_JOB_NAME,
            worker_instance_id=_WORKER_INSTANCE_ID,
            lease_seconds=_CHIP_LEASE_SECONDS,
        )
        if claim is None:
            await db.rollback()
            return False
        await db.commit()

    lease_token = claim.token
    meta = claim.metadata
    trade_date_str = meta.get("trade_date")
    core_run_id_str = meta.get("core_run_id")
    job_run_id = lease_token.job_run_id
    current_lease_epoch = lease_token.lease_epoch
    prev_status = claim.previous_status
    is_resume = prev_status == "resume_queued"

    logger.info(
        "[ChipConsensusWorker] 领取任务: job_run_id=%s, prev_status=%s, "
        "lease_epoch=%s, is_resume=%s",
        job_run_id, prev_status, current_lease_epoch, is_resume,
    )

    heartbeat = FencedJobHeartbeat(lease_token, interval_seconds=30.0)
    await heartbeat.start()
    finalized = False
    trade_date = None
    chip_status = "failed"
    # [Corrective-3 §二.1] 领域 run id：retry/resume 必须复用 metadata 中已固定的 id，
    # 禁止每次重试新建 ChipConsensusRun。
    chip_run_id: uuid.UUID | None = None
    existing_chip_run_id_str = meta.get(META_CHIP_RUN_ID)
    existing_chip_run_id: uuid.UUID | None = None
    if existing_chip_run_id_str:
        try:
            existing_chip_run_id = uuid.UUID(str(existing_chip_run_id_str))
        except (ValueError, TypeError):
            logger.warning(
                "[ChipConsensusWorker] metadata.chip_run_id 非法，忽略: %s",
                existing_chip_run_id_str,
            )

    async def _finalize_failure(code: str, message: str) -> bool:
        return await finalize_job_run(
            lease_token,
            status="failed",
            metadata_updates={
                "chip_status": "failed",
                "chip_results_summary": {
                    "succeeded": 0,
                    "failed": 1,
                    "skipped": 0,
                    "total": 1,
                    "reason_codes": [code],
                },
            },
            total_count=1,
            succeeded_count=0,
            failed_count=1,
            error_code=code,
            error_message=message[:500],
        )

    try:
        if not trade_date_str or not core_run_id_str:
            logger.error(
                "[ChipConsensusWorker] 任务缺少 trade_date/core_run_id: job_run_id=%s",
                job_run_id,
            )
            finalized = await _finalize_failure(
                "CHIP_JOB_METADATA_MISSING",
                "任务缺少 trade_date/core_run_id，无法执行 chip consensus",
            )
            return True

        trade_date = date_cls.fromisoformat(trade_date_str)
        core_run_id = uuid.UUID(core_run_id_str)

        try:
            heartbeat.ensure_owned()
            async with AsyncSessionLocal() as db:
                all_instrument_ids = await get_active_a_share_instruments(db)
                pending_instrument_ids = await get_pending_chip_instruments(
                    db,
                    trade_date=trade_date,
                    core_run_id=core_run_id,
                    all_instrument_ids=all_instrument_ids,
                )
            heartbeat.ensure_owned()
        except JobLeaseLostError:
            raise
        except Exception as exc:
            logger.exception(
                "[ChipConsensusWorker] 获取 instrument 列表失败: job_run_id=%s, error=%s",
                job_run_id, exc,
            )
            finalized = await _finalize_failure(
                "CHIP_INSTRUMENT_LIST_FAILED",
                f"获取 instrument 列表失败: {exc}",
            )
            return True

        # [Corrective-3 §二.1] 建立 ChipConsensusRun 生命周期。
        # 修复前：没有任何生产路径写入 chip_consensus_runs，导致
        # publish_chip_consensus 的 session.get(ChipConsensusRun, ...) 永远为空。
        try:
            heartbeat.ensure_owned()
            async with AsyncSessionLocal() as run_db:
                chip_run = await resolve_or_create_chip_run(
                    run_db,
                    trade_date=trade_date,
                    source_core_run_id=core_run_id,
                    algorithm_version=CHIP_CONSENSUS_ALGORITHM_VERSION,
                    scheduler_job_run_id=job_run_id,
                    expected_count=len(all_instrument_ids),
                    worker_id=_WORKER_INSTANCE_ID,
                    lease_epoch=current_lease_epoch,
                    existing_run_id=existing_chip_run_id,
                )
                chip_run_id = chip_run.id
                await run_db.commit()
            # 把 chip_run_id 固定到 SchedulerJobRun metadata，恢复任务时复用同一 ID
            if existing_chip_run_id != chip_run_id:
                await merge_job_run_metadata(
                    job_run_id, {META_CHIP_RUN_ID: str(chip_run_id)},
                )
            heartbeat.ensure_owned()
        except JobLeaseLostError:
            raise
        except Exception as exc:
            logger.exception(
                "[ChipConsensusWorker] 创建/解析 ChipConsensusRun 失败: "
                "job_run_id=%s, error=%s",
                job_run_id, exc,
            )
            finalized = await _finalize_failure(
                "CHIP_DOMAIN_RUN_INIT_FAILED",
                f"创建/解析 ChipConsensusRun 失败: {exc}",
            )
            return True

        logger.info(
            "[ChipConsensusWorker] 开始执行: job_run_id=%s, trade_date=%s, "
            "core_run_id=%s, chip_run_id=%s, total_instruments=%d, pending=%d, "
            "is_resume=%s",
            job_run_id, trade_date, core_run_id, chip_run_id,
            len(all_instrument_ids), len(pending_instrument_ids), is_resume,
        )

        chip_result_summary = await execute_after_close_chip_consensus(
            job_run_id=job_run_id,
            trade_date=trade_date,
            core_run_id=core_run_id,
            instrument_ids=pending_instrument_ids,
            worker_id=_WORKER_INSTANCE_ID,
            lease_epoch=current_lease_epoch,
            ownership_check=heartbeat.ensure_owned,
        )
        heartbeat.ensure_owned()
        chip_status = str(chip_result_summary.get("status", "failed"))
        main_status = "failed" if chip_status == "failed" else "succeeded"
        failed_items = chip_result_summary.get("failed_instruments", [])
        skipped_items = chip_result_summary.get("skipped_instruments", [])
        reason_codes = sorted({
            str(item.get("reason") or item.get("error") or "UNKNOWN")[:120]
            for item in [*failed_items, *skipped_items]
        })[:20]
        metadata_updates = {
            "chip_status": chip_status,
            "succeeded_count": chip_result_summary.get("succeeded_count", 0),
            "failed_count": chip_result_summary.get("failed_count", 0),
            "skipped_count": chip_result_summary.get("skipped_count", 0),
            "total_count": chip_result_summary.get("total_count", 0),
            "chip_results_summary": {
                "succeeded": chip_result_summary.get("succeeded_count", 0),
                "failed": chip_result_summary.get("failed_count", 0),
                "skipped": chip_result_summary.get("skipped_count", 0),
                "total": chip_result_summary.get("total_count", 0),
                "reason_codes": reason_codes,
            },
        }

        # [Corrective-3 §二.1/§二.3] chip snapshots 完成 → ChipConsensusRun 终态。
        # 必须先于 publish_chip_consensus，因为发布函数校验
        # chip_run.status ∈ (succeeded, partial) 并读取 coverage_ratio。
        #
        # [Corrective-3.1 §P0-2] 领域 run 终态写入失败不得被静默吞掉：
        # 失败时记录 chip_domain_finalize_* 治理字段、禁止 publication、
        # 并把主任务降级为 degraded（不再无条件 succeeded）。
        domain_finalized = False
        if chip_run_id is not None:
            try:
                async with AsyncSessionLocal() as run_db:
                    await finalize_chip_run(
                        run_db,
                        chip_run_id=chip_run_id,
                        chip_status=chip_status,
                        succeeded_count=int(
                            chip_result_summary.get("succeeded_count", 0),
                        ),
                        failed_count=int(chip_result_summary.get("failed_count", 0)),
                        skipped_count=int(chip_result_summary.get("skipped_count", 0)),
                        total_count=int(chip_result_summary.get("total_count", 0)),
                        error_code=(
                            "CHIP_SYSTEMIC_FAILURE" if chip_status == "failed" else None
                        ),
                        error_message=(
                            "全部 chip instrument 处理失败"
                            if chip_status == "failed" else None
                        ),
                        diagnostics={"reason_codes": reason_codes},
                        fenced_token=heartbeat.token,
                    )
                    await run_db.commit()
                domain_finalized = True
                metadata_updates["chip_domain_finalize_status"] = "succeeded"
            except Exception as exc:
                logger.warning(
                    "[ChipConsensusWorker] ChipConsensusRun 终态写入失败: chip_run_id=%s",
                    chip_run_id, exc_info=True,
                )
                metadata_updates["chip_domain_finalize_status"] = "failed"
                metadata_updates["chip_domain_finalize_error_code"] = (
                    "CHIP_DOMAIN_FINALIZE_FAILED"
                )
                metadata_updates["chip_domain_finalize_error"] = str(exc)[:500]
                metadata_updates["chip_run_id"] = str(chip_run_id)
                # 领域 run 状态未知/不一致 → 主任务不得声称成功。
                # 不引入 SchedulerJobRun 状态机之外的新值（合法值仅
                # queued/running/succeeded/failed/skipped/interrupted/resume_queued），
                # 因此统一落 failed，由 metadata 区分"快照已算完但领域终态失败"。
                main_status = "failed"
        else:
            metadata_updates["chip_domain_finalize_status"] = "skipped_no_run"

        # [Corrective-3.1 §P0-1] publication 必须在 SchedulerJobRun 终态之前、
        # 且在租约仍然持有时执行，并向下传递 ownership_check 做写前 fencing。
        # 修复前 publication 位于 finally: heartbeat.stop() 之后，租约已释放，
        # helper 的 fencing 能力在生产路径上完全没有生效。
        publication_outcome = None
        if (
            trade_date is not None
            and chip_run_id is not None
            and domain_finalized
            and chip_status in {"succeeded", "partial"}
        ):
            from app.services.auction_anchor_service import (
                generate_and_publish_auction_anchors,
            )
            from app.services.chip_consensus_run_lifecycle import (
                publish_chip_and_upgrade_auction,
            )
            from app.services.factor_publication_service import publish_chip_consensus

            heartbeat.ensure_owned()
            publication_outcome = await publish_chip_and_upgrade_auction(
                trade_date=trade_date,
                chip_run_id=chip_run_id,
                algorithm_version=CHIP_CONSENSUS_ALGORITHM_VERSION,
                chip_status=chip_status,
                scheduler_job_run_id=job_run_id,
                worker_id=_WORKER_INSTANCE_ID,
                lease_epoch=current_lease_epoch,
                anchor_rebuild_required=bool(
                    chip_result_summary.get("anchor_rebuild_required", False),
                ),
                session_factory=AsyncSessionLocal,
                publish_fn=publish_chip_consensus,
                auction_fn=generate_and_publish_auction_anchors,
                ownership_check=heartbeat.ensure_owned,
                fenced_token=heartbeat.token,
            )
            # [Corrective-3 §二.4] 软失败必须可治理：并入主任务终态 metadata，
            # 使 ProductReadiness 能显示 chip run succeeded 但 publication missing。
            metadata_updates.update(publication_outcome.to_metadata())
        elif chip_run_id is not None and not domain_finalized:
            logger.error(
                "[ChipConsensusWorker] 领域 run 终态失败，已阻断 chip publication: "
                "chip_run_id=%s",
                chip_run_id,
            )

        # [Corrective-3.1 §P0-2] 区分两种 failed 原因，不得都报 CHIP_SYSTEMIC_FAILURE：
        #  - chip_status == "failed"：全部 instrument 处理失败
        #  - 领域 run 终态写入失败：快照已算完但 ChipConsensusRun 状态不一致
        if main_status != "failed":
            terminal_error_code = None
            terminal_error_message = None
        elif chip_status == "failed":
            terminal_error_code = "CHIP_SYSTEMIC_FAILURE"
            terminal_error_message = "全部 chip instrument 处理失败"
        else:
            terminal_error_code = "CHIP_DOMAIN_FINALIZE_FAILED"
            terminal_error_message = (
                "chip 快照已完成但 ChipConsensusRun 终态写入失败，"
                "publication 已阻断，需人工核对领域 run 状态"
            )

        finalized = await finalize_job_run(
            lease_token,
            status=main_status,
            metadata_updates=metadata_updates,
            total_count=int(chip_result_summary.get("total_count", 0)),
            succeeded_count=int(chip_result_summary.get("succeeded_count", 0)),
            failed_count=int(chip_result_summary.get("failed_count", 0)),
            error_code=terminal_error_code,
            error_message=terminal_error_message,
        )
        if not finalized:
            raise JobLeaseLostError(
                f"chip terminal update fenced: job_run_id={job_run_id}"
            )

        logger.info(
            "[ChipConsensusWorker] 执行完成: job_run_id=%s, status=%s, "
            "succeeded=%d, failed=%d, skipped=%d, total=%d",
            job_run_id, chip_status,
            chip_result_summary.get("succeeded_count", 0),
            chip_result_summary.get("failed_count", 0),
            chip_result_summary.get("skipped_count", 0),
            chip_result_summary.get("total_count", 0),
        )
    except JobLeaseLostError as exc:
        logger.warning(
            "[ChipConsensusWorker] 已失去租约，禁止终态或后续写入: job_run_id=%s, error=%s",
            job_run_id, exc,
        )
        return True
    except Exception as exc:
        logger.exception(
            "[ChipConsensusWorker] 执行异常: job_run_id=%s, error=%s", job_run_id, exc,
        )
        finalized = await _finalize_failure(
            "CHIP_JOB_EXECUTION_FAILED",
            f"chip consensus 执行异常: {exc}",
        )
        return True
    finally:
        await heartbeat.stop()

    # [Corrective-3.1 §P0-1] publication / auction 已上移至租约保护区内执行
    # （SchedulerJobRun 终态之前，并传入 ownership_check）。此处不再有终态后
    # 的无保护写入。
    return True


async def run_chip_consensus_worker() -> None:
    """[ChipConsensusWorker] - 盘后筹码共识独立 Worker：领取 queued 任务并执行。

    [P0-3 ref/instruction.md §二.3] chip 任务有执行者：
    - 在现有 after-close worker 容器内增加独立 poll 函数和 WORKER_TYPE 分支
    - 不新增常驻容器
    - 使用 FOR UPDATE SKIP LOCKED、lease_epoch、heartbeat、断点续算
    - chip 失败不反改 core

    每个轮询周期：
    1. 启动恢复（清理上次崩溃残留的 running 任务，由 watchdog 转为 interrupted → resume_queued）
    2. _chip_consensus_poll_once 领取并执行一个 queued/resume_queued 任务
    3. sleep WORKER_INTERVAL 后继续轮询

    [SIGTERM drain] - 优雅退出（与 run_after_close_orchestrator_worker 一致）：
    - SIGTERM/SIGINT 设置 _shutdown=True
    - 主循环在领取新任务前检查 _shutdown
    - 当前正在执行的 chip consensus 完成后才退出
    """
    _hb_task = asyncio.create_task(_heartbeat_loop("chip_consensus"))
    logger.info(
        "[ChipConsensusWorker] 启动（间隔=%ds）", WORKER_INTERVAL,
    )

    # 启动恢复：清理上次崩溃残留的 running 任务（由 watchdog 转为 interrupted → resume_queued）
    try:
        async with AsyncSessionLocal() as db:
            recovered = await recover_stale_scheduler_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info(
                    "[ChipConsensusWorker] 启动恢复: %d 个过期任务", recovered,
                )
    except Exception as exc:
        logger.exception("[ChipConsensusWorker] 启动恢复异常: %s", exc)

    while not _shutdown:
        try:
            await _chip_consensus_poll_once()
        except Exception as exc:
            # _chip_consensus_poll_once 内部已捕获执行异常，
            # 此处仅捕获领取阶段的意外异常
            logger.exception("[ChipConsensusWorker] 轮询异常: %s", exc)
        if _shutdown:
            logger.info("[ChipConsensusWorker] SIGTERM drain: 不再领取新任务，准备退出")
            break
        await asyncio.sleep(WORKER_INTERVAL)

    logger.info("[ChipConsensusWorker] SIGTERM drain complete, finished current item")


# =============================================================================
# Review Bootstrap Worker - Review 历史回填（admin API 提交，Worker 执行）
# =============================================================================


async def _review_bootstrap_poll_once() -> bool:
    """[ReviewBootstrapWorker] - 单次轮询：领取并执行一个 queued review bootstrap 任务。

    为什么需要独立 Worker：120 交易日 × 全 scope 的历史回填耗时远超 HTTP
    超时，admin API 只创建 status=queued 的 SchedulerJobRun，真正计算在这里。

    使用 FOR UPDATE SKIP LOCKED 领取 + lease_epoch fencing + heartbeat，
    与 chip consensus worker 同构：失去租约后禁止写终态，避免僵尸 Worker
    覆盖已被 watchdog 转交的任务。

    dry_run 任务在 service 层严格零业务写入（execute_bootstrap_job 显式 rollback）。

    Returns:
        True 如果领取到任务（无论执行成功与否），False 如果无可领取任务。
    """
    from app.services.fenced_job_run_service import (
        FencedJobHeartbeat,
        JobLeaseLostError,
        claim_next_job_run,
        finalize_job_run,
    )
    from app.services.review_bootstrap_job_service import (
        REVIEW_BOOTSTRAP_JOB_NAME,
        REVIEW_BOOTSTRAP_LEASE_SECONDS,
        build_job_metadata_updates,
        execute_bootstrap_job,
    )

    async with AsyncSessionLocal() as db:
        claim = await claim_next_job_run(
            db,
            job_name=REVIEW_BOOTSTRAP_JOB_NAME,
            worker_instance_id=_WORKER_INSTANCE_ID,
            lease_seconds=REVIEW_BOOTSTRAP_LEASE_SECONDS,
        )
        if claim is None:
            await db.rollback()
            return False
        await db.commit()

    lease_token = claim.token
    meta = claim.metadata
    job_run_id = lease_token.job_run_id
    dry_run = bool(meta.get("dry_run", True))

    logger.info(
        "[ReviewBootstrapWorker] 领取任务: job_run_id=%s prev_status=%s "
        "dry_run=%s end_date=%s days_back=%s operator=%s",
        job_run_id, claim.previous_status, dry_run,
        meta.get("end_date"), meta.get("days_back"), meta.get("operator"),
    )

    # bootstrap 单次执行时间长（全量可达数十分钟），心跳间隔沿用 30s
    heartbeat = FencedJobHeartbeat(lease_token, interval_seconds=30.0)
    await heartbeat.start()

    try:
        heartbeat.ensure_owned()
        async with AsyncSessionLocal() as db:
            result = await execute_bootstrap_job(db, job_metadata=meta)
        heartbeat.ensure_owned()

        summary_counts = result.get("scope_counts", {})
        failed_scopes = int(summary_counts.get("failed", 0))
        # 只有"全部日期都没算出来"才算任务级失败：
        # 单个 scope 的 unavailable 是 PIT 数据事实，不是任务故障。
        processed = int(result.get("processed", 0))
        main_status = "succeeded" if processed > 0 else "failed"

        finalized = await finalize_job_run(
            lease_token,
            status=main_status,
            metadata_updates=build_job_metadata_updates(result),
            total_count=int(result.get("eligible_dates", 0)),
            succeeded_count=int(result.get("written", 0)),
            failed_count=failed_scopes,
            error_code=None if main_status == "succeeded" else "BOOTSTRAP_NO_ELIGIBLE_DATES",
            error_message=(
                None if main_status == "succeeded"
                else f"无可回填交易日: status={result.get('status')}"
            ),
        )
        if not finalized:
            raise JobLeaseLostError(
                f"review bootstrap terminal update fenced: job_run_id={job_run_id}",
            )

        logger.info(
            "[ReviewBootstrapWorker] 执行完成: job_run_id=%s status=%s dry_run=%s "
            "eligible=%s processed=%s written=%s scope_counts=%s",
            job_run_id, main_status, dry_run,
            result.get("eligible_dates"), processed,
            result.get("written"), summary_counts,
        )
    except JobLeaseLostError as exc:
        logger.warning(
            "[ReviewBootstrapWorker] 已失去租约，禁止终态或后续写入: "
            "job_run_id=%s error=%s",
            job_run_id, exc,
        )
        return True
    except Exception as exc:
        logger.exception(
            "[ReviewBootstrapWorker] 执行异常: job_run_id=%s error=%s", job_run_id, exc,
        )
        await finalize_job_run(
            lease_token,
            status="failed",
            metadata_updates={"bootstrap_status": "failed"},
            total_count=0,
            succeeded_count=0,
            failed_count=1,
            error_code="BOOTSTRAP_EXECUTION_FAILED",
            error_message=f"review bootstrap 执行异常: {exc}"[:500],
        )
        return True
    finally:
        await heartbeat.stop()

    return True


async def run_review_bootstrap_worker() -> None:
    """[ReviewBootstrapWorker] - Review 历史回填 Worker：领取 queued 任务并执行。

    不新增常驻容器：与 chip consensus 一致，生产由
    run_after_close_orchestrator_worker 在 after-close 容器内轮询
    （_review_bootstrap_poll_once，最低优先级）。

    本函数仅在 WORKER_TYPE=review_bootstrap 时作为独立 worker 启动，
    用于调试或独立部署；WORKER_TYPE=all 不启动，避免与 after-close 循环
    重复领取同一批任务。

    每个轮询周期：
    1. _review_bootstrap_poll_once 领取并执行一个 queued/resume_queued 任务
    2. sleep WORKER_INTERVAL 后继续轮询

    [SIGTERM drain] 与其它 Worker 一致：当前任务执行完才退出。
    """
    logger.info("[ReviewBootstrapWorker] 启动（间隔=%ds）", WORKER_INTERVAL)

    while not _shutdown:
        try:
            await _review_bootstrap_poll_once()
        except Exception as exc:
            # poll_once 内部已捕获执行异常，此处仅捕获领取阶段的意外异常
            logger.exception("[ReviewBootstrapWorker] 轮询异常: %s", exc)
        if _shutdown:
            logger.info("[ReviewBootstrapWorker] SIGTERM drain: 不再领取新任务，准备退出")
            break
        await asyncio.sleep(WORKER_INTERVAL)

    logger.info("[ReviewBootstrapWorker] SIGTERM drain complete, finished current item")


# =============================================================================
# [P0-3 修复 2026-07-31] Auction Scheduler Worker - 竞价分析调度
# =============================================================================


async def _auction_scheduler_poll_once() -> bool:
    """[P0-3] Auction Scheduler 单次轮询：
    1. 检查时间窗口：09:25:05 ± 30s → 创建 auction_final:{date}
                      10:00:00 ± 30s → 创建 auction_open_confirmation:{date}
    2. 领取一条 queued auction job 并执行（FOR UPDATE SKIP LOCKED）

    Returns:
        True 如果领取并执行了任务，False 如果无任务可执行
    """
    from datetime import date as date_cls

    from app.services.auction_scheduler_service import (
        AUCTION_FINAL_JOB_NAME,
        AUCTION_OPEN_CONFIRMATION_JOB_NAME,
        create_auction_final_job,
        create_auction_open_confirmation_job,
        execute_auction_open_confirmation_run,
        execute_auction_scan_run,
        get_queued_auction_job,
        should_create_auction_final_job,
        should_create_auction_open_confirmation_job,
    )
    from app.services.calendar_service import is_trading_day_async

    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)

    # 1. 时间窗口检查 - 仅在交易日创建任务
    try:
        async with AsyncSessionLocal() as db:
            trading = await is_trading_day_async(db, now.date())

        if trading:
            # 09:25:05 ± 30s → 创建 auction_final job
            if should_create_auction_final_job(now):
                async with AsyncSessionLocal() as db:
                    job_run, is_new = await create_auction_final_job(
                        db, now.date(),
                        worker_instance_id=_WORKER_INSTANCE_ID,
                    )
                    if is_new:
                        logger.info(
                            "[AuctionScheduler] 创建 auction_final job: run_id=%s, trade_date=%s",
                            job_run.id if job_run else None, now.date(),
                        )
                    await db.commit()
            # 10:00:00 ± 30s → 创建 auction_open_confirmation job
            elif should_create_auction_open_confirmation_job(now):
                async with AsyncSessionLocal() as db:
                    job_run, is_new = await create_auction_open_confirmation_job(
                        db, now.date(),
                        worker_instance_id=_WORKER_INSTANCE_ID,
                    )
                    if is_new:
                        logger.info(
                            "[AuctionScheduler] 创建 auction_open_confirmation job: run_id=%s, trade_date=%s",
                            job_run.id if job_run else None, now.date(),
                        )
                    await db.commit()
    except Exception as exc:
        logger.exception("[AuctionScheduler] 时间窗口检查/任务创建异常: %s", exc)

    # 2. 领取一条 queued auction job
    async with AsyncSessionLocal() as db:
        job_run = await get_queued_auction_job(db)
        if job_run is None:
            await db.rollback()
            return False

        # 领取：更新 status='running' + worker + heartbeat + lease_epoch（fencing）
        now_claim = datetime.now(tz)
        job_run.status = "running"
        job_run.worker_instance_id = _WORKER_INSTANCE_ID
        if job_run.started_at is None:
            job_run.started_at = now_claim
        job_run.heartbeat_at = now_claim
        # lease_expires_at 已在 create 时设置；fencing epoch 递增
        job_run.lease_epoch = (job_run.lease_epoch or 0) + 1
        await db.commit()

        job_run_id = job_run.id
        current_lease_epoch = job_run.lease_epoch
        job_name = job_run.job_name
        # 提取 metadata
        meta = json.loads(job_run.metadata_json) if job_run.metadata_json else {}
        trade_date_str = meta.get("trade_date")

    if not trade_date_str:
        # 缺关键 metadata，立即标记 failed
        logger.error(
            "[AuctionScheduler] 任务缺少 trade_date，标记 failed: job_run_id=%s",
            job_run_id,
        )
        async with AsyncSessionLocal() as db:
            jr = await db.get(SchedulerJobRun, job_run_id)
            if jr is not None:
                now_fail = datetime.now(tz)
                jr.status = "failed"
                jr.finished_at = now_fail
                jr.lease_expires_at = now_fail
                jr.error_message = "任务缺少 trade_date"
                await db.commit()
        return True

    trade_date = date_cls.fromisoformat(trade_date_str)

    logger.info(
        "[AuctionScheduler] 领取任务: job_run_id=%s, job_name=%s, "
        "trade_date=%s, lease_epoch=%s",
        job_run_id, job_name, trade_date, current_lease_epoch,
    )

    # 执行任务
    try:
        if job_name == AUCTION_FINAL_JOB_NAME:
            await execute_auction_scan_run(
                job_run_id=job_run_id,
                trade_date=trade_date,
                worker_id=_WORKER_INSTANCE_ID,
                lease_epoch=current_lease_epoch,
            )
        elif job_name == AUCTION_OPEN_CONFIRMATION_JOB_NAME:
            await execute_auction_open_confirmation_run(
                job_run_id=job_run_id,
                trade_date=trade_date,
                worker_id=_WORKER_INSTANCE_ID,
                lease_epoch=current_lease_epoch,
            )
        else:
            logger.error(
                "[AuctionScheduler] 未知 job_name=%s，标记 failed: job_run_id=%s",
                job_name, job_run_id,
            )
            async with AsyncSessionLocal() as db:
                jr = await db.get(SchedulerJobRun, job_run_id)
                if jr is not None:
                    now_fail = datetime.now(tz)
                    jr.status = "failed"
                    jr.finished_at = now_fail
                    jr.lease_expires_at = now_fail
                    jr.error_message = f"未知 job_name: {job_name}"
                    await db.commit()
    except Exception as exc:
        logger.exception(
            "[AuctionScheduler] 执行异常: job_run_id=%s, error=%s", job_run_id, exc,
        )
        # execute_*_run 内部已标记 failed，此处仅记录

    return True


# [P0-3] Auction Scheduler 轮询间隔（从 auction_scheduler_service 导入，避免硬编码）
_AUCTION_SCHEDULER_POLL_INTERVAL_VAL = 30  # 默认 30s；正式部署时由环境变量覆盖


async def _run_auction_scheduler_co_process() -> None:
    """[P0-3 2026-07-31] Auction Scheduler co-process - 在 after_close_orchestrator Worker 进程内运行。

    生产入口：`docker-compose.prod.yml` 的 `worker-after-close`（WORKER_TYPE=after_close_orchestrator）
    自动启动本 co-process，无需单独 WORKER_TYPE=auction_scheduler。

    职责：
    1. 检查时间窗口：09:25:05 ± 30s → 创建 auction_final:{date}（仅交易日）
                     10:00:00 ± 30s → 创建 auction_open_confirmation:{date}（仅交易日）
    2. 领取一条 queued auction job 并执行（FOR UPDATE SKIP LOCKED）
    3. 每 AUCTION_SCHEDULER_POLL_INTERVAL（30s）轮询一次

    异常隔离：
    - 所有异常在循环内捕获，不影响 after_close_orchestrator 主 Worker
    - 单次 poll 异常仅记录日志，下一轮继续

    SIGTERM drain：
    - 共享全局 `_shutdown` 标志
    - 检查 _shutdown 后退出循环；当前正在执行的 auction job 完成后才退出
    - 由 run_after_close_orchestrator_worker 在 finally 块中 await drain
    """
    from app.services.auction_scheduler_service import (
        AUCTION_SCHEDULER_POLL_INTERVAL,
    )

    logger.info(
        "[AuctionScheduler] co-process 启动（间隔=%ds，触发窗口 09:25:05/10:00:00 Asia/Shanghai）",
        AUCTION_SCHEDULER_POLL_INTERVAL,
    )

    # 启动恢复：清理上次崩溃残留的 running auction 任务
    try:
        async with AsyncSessionLocal() as db:
            recovered = await recover_stale_scheduler_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info(
                    "[AuctionScheduler] co-process 启动恢复: %d 个过期任务", recovered,
                )
    except Exception as exc:
        logger.exception("[AuctionScheduler] co-process 启动恢复异常: %s", exc)

    while not _shutdown:
        try:
            await _auction_scheduler_poll_once()
        except Exception as exc:
            # 异常隔离：不影响 after_close_orchestrator 主 Worker
            logger.exception("[AuctionScheduler] co-process 轮询异常: %s", exc)
        if _shutdown:
            logger.info("[AuctionScheduler] co-process SIGTERM drain: 不再领取新任务，准备退出")
            break
        await asyncio.sleep(AUCTION_SCHEDULER_POLL_INTERVAL)

    logger.info("[AuctionScheduler] co-process SIGTERM drain complete")


async def run_auction_scheduler_worker() -> None:
    """[P0-3] Auction Scheduler Worker - 竞价分析调度独立 Worker（调试入口）。

    生产环境由 `run_after_close_orchestrator_worker` 自动启动 `_run_auction_scheduler_co_process`，
    无需单独 WORKER_TYPE=auction_scheduler。本入口仅用于独立调试。

    [P0-3 ref/instruction.md §三.3] 接入现有 Scheduler/Worker，不新建容器：
    - 使用 SchedulerJobRun、run_key、heartbeat、lease、fencing、retry 和恢复
    - 不新增常驻容器（与 bars_scheduler/calendar_scheduler 同级）

    每个轮询周期：
    1. 检查时间窗口：09:25:05 ± 30s → 创建 auction_final:{date}
                     10:00:00 ± 30s → 创建 auction_open_confirmation:{date}
    2. _auction_scheduler_poll_once 领取并执行一条 queued auction job
    3. sleep 后继续轮询

    [SIGTERM drain] - 优雅退出（与 run_after_close_orchestrator_worker 一致）：
    - SIGTERM/SIGINT 设置 _shutdown=True
    - 主循环在领取新任务前检查 _shutdown
    - 当前正在执行的 auction job 完成后才退出
    """
    from app.services.auction_scheduler_service import (
        AUCTION_SCHEDULER_POLL_INTERVAL,
    )

    _hb_task = asyncio.create_task(_heartbeat_loop("auction_scheduler"))
    logger.info(
        "[AuctionScheduler] 启动（间隔=%ds，触发窗口 09:25:05/10:00:00 Asia/Shanghai）",
        AUCTION_SCHEDULER_POLL_INTERVAL,
    )

    # 启动恢复：清理上次崩溃残留的 running 任务（由 watchdog 转为 interrupted）
    try:
        async with AsyncSessionLocal() as db:
            recovered = await recover_stale_scheduler_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info(
                    "[AuctionScheduler] 启动恢复: %d 个过期任务", recovered,
                )
    except Exception as exc:
        logger.exception("[AuctionScheduler] 启动恢复异常: %s", exc)

    while not _shutdown:
        try:
            await _auction_scheduler_poll_once()
        except Exception as exc:
            logger.exception("[AuctionScheduler] 轮询异常: %s", exc)
        if _shutdown:
            logger.info("[AuctionScheduler] SIGTERM drain: 不再领取新任务，准备退出")
            break
        await asyncio.sleep(AUCTION_SCHEDULER_POLL_INTERVAL)

    logger.info("[AuctionScheduler] SIGTERM drain complete, finished current item")


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

    # [2026-08-11 AFTER-CLOSE-ENHANCEMENT-HEAD-OF-LINE-BLOCKING]
    # WORKER_TYPE=after_close_orchestrator 在 run_after_close_orchestrator_worker 内
    # 同时启动 Chip 独立 co-process（复用 run_chip_consensus_worker），不再串行 fallback。
    # WORKER_TYPE=chip_consensus 仅用于调试/独立部署，避免与 after-close 重复领取。
    if WORKER_TYPE == "chip_consensus":
        tasks.append(asyncio.create_task(run_chip_consensus_worker()))

    # [P0-3 2026-07-31] Auction Scheduler Worker - 竞价分析调度（09:25/10:00 触发）
    if WORKER_TYPE in ("auction_scheduler", "all"):
        tasks.append(asyncio.create_task(run_auction_scheduler_worker()))

    # [2026-08-02] review bootstrap 已接入 run_after_close_orchestrator_worker
    # （mandatory 主循环中作为最低优先级回填，不抢占盘后主链；其独立 executor
    # isolation 另行登记，见 guide FIX C）。
    # 生产没有 WORKER_TYPE=all 容器，after-close 容器跑 after_close_orchestrator，
    # 因此这里只在 WORKER_TYPE=review_bootstrap 时启动独立 worker（调试/独立部署），
    # 避免 all 模式下与 after-close 循环重复领取同一批任务。
    if WORKER_TYPE == "review_bootstrap":
        tasks.append(asyncio.create_task(run_review_bootstrap_worker()))

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
    assert WORKER_TYPE in ("outbox", "delivery", "strategy_batch", "bars_scheduler", "strategy_scheduler", "calendar_scheduler", "monitor_scheduler", "after_close_orchestrator", "chip_consensus", "auction_scheduler", "review_bootstrap", "watchdog", "all"), \
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
    assert callable(run_chip_consensus_worker), "run_chip_consensus_worker 应可调用"
    assert callable(run_auction_scheduler_worker), "run_auction_scheduler_worker 应可调用"
    assert callable(run_review_bootstrap_worker), "run_review_bootstrap_worker 应可调用"
    assert callable(_review_bootstrap_poll_once), "_review_bootstrap_poll_once 应可调用"
    assert callable(_recovery_watchdog_loop), "_recovery_watchdog_loop 应可调用"
    print("OK: 配置验证通过")
    asyncio.run(main())
