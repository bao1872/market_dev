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
- Review bootstrap 以**独立 co-process** 在同一进程内运行（复用 `run_review_bootstrap_worker`），
  拥有自己的执行 loop，绝不串行阻塞 mandatory orchestrator。
- Auction Scheduler 以独立 co-process 运行（`_run_auction_scheduler_co_process`）。
- 四个 loop 各自独立 `while not _shutdown`，共享 `_shutdown`，SIGTERM 时统一 drain 到当前
  业务 item terminal（禁止裸 Task.cancel 遗留 ownership 不清的 running job）。
- 禁止恢复"每轮 core → chip → bootstrap 串行 fallback"的旧结构 —— 那会让长时 chip /
  review bootstrap 任务占用 mandatory executor，造成 head-of-line blocking。
- **边界（勿过度声称）**：应用层 `_drain_co_process` 只保证 SIGTERM 到达 Python 后不裸取消
  活跃业务任务，它**不能**单独保证生产无界优雅停机——Docker `stop_grace_period`（默认 60s）
  到期仍可能 SIGKILL 进程。完整受控部署保证 = 部署前置活跃任务门禁（拒绝在 worker-after-close
  拥有 running 长任务时变更 backend runtime / 重启）+ 应用 drain + 既有 Docker stop 策略。
  该门禁由 `scripts/deploy/panji-deploy.sh` 的 `guard_active_after_close_jobs` 实现。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import uuid
from datetime import UTC, datetime, timedelta
from time import monotonic as _time_monotonic
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.notification_worker_runtime import (
    run_delivery_loop,
    run_outbox_relay_loop,
)
from app.services.scheduler_job_run_recovery_service import (
    auto_resume_interrupted_after_close_runs,
    recover_replaced_incarnation_runs,
    recover_stale_scheduler_job_runs,
)
from app.services.strategy_batch_worker_runtime import run_strategy_batch_loop

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

# [CRASH-RESUME-SLICE / P0-C] worker 实例标识 = "hostname:pid:incarnation_nonce"。
# 新增的 incarnation nonce 在每次进程启动时生成，用于区分「同一 PID slot 上的
# 不同代进程」——Docker restart 后 hostname 通常不变、PID 重新为 1，新旧 Python
# 进程会拿到同一个 slot（hostname:pid），但 nonce 不同，从而能证明上一代进程已被替换。
# 同时兼容 legacy 格式 "hostname:pid"（无 nonce）：解析时 incarnation 视为 None。
_WORKER_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def parse_worker_slot(instance_id: str) -> str:
    """返回 worker slot（hostname:pid），兼容 legacy "hostname:pid" 与
    新格式 "hostname:pid:nonce"。

    slot 相等意味着「同一台机器上的同一进程 PID 槽位」，是判断 incarnation
    替换的依据；incarnation（nonce）不同则说明该 slot 上的进程已更新。
    """
    if not instance_id:
        return instance_id
    parts = instance_id.split(":")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return instance_id


def parse_worker_incarnation(instance_id: str) -> str | None:
    """返回 incarnation nonce；legacy "hostname:pid" 返回 None。"""
    if not instance_id:
        return None
    parts = instance_id.split(":")
    if len(parts) == 3:
        return parts[2]
    return None


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
    """兼容 façade：装配 Outbox Relay 的进程级依赖。"""
    await run_outbox_relay_loop(
        session_factory=AsyncSessionLocal,
        heartbeat_loop=_heartbeat_loop,
        should_shutdown=lambda: _shutdown,
        interval=WORKER_INTERVAL,
        batch_size=WORKER_BATCH_SIZE,
        max_retry=WORKER_MAX_RETRY,
        logger=logger,
    )


async def run_delivery_worker() -> None:
    """兼容 façade：装配 Delivery Worker 的进程级依赖。"""
    await run_delivery_loop(
        session_factory=AsyncSessionLocal,
        heartbeat_loop=_heartbeat_loop,
        should_shutdown=lambda: _shutdown,
        interval=WORKER_INTERVAL,
        batch_size=WORKER_BATCH_SIZE,
        max_retry=WORKER_MAX_RETRY,
        logger=logger,
    )


async def run_strategy_batch_worker() -> None:
    """兼容 façade：装配 Strategy Batch Worker 的进程级依赖。"""
    await run_strategy_batch_loop(
        session_factory=AsyncSessionLocal,
        heartbeat_loop=_heartbeat_loop,
        should_shutdown=lambda: _shutdown,
        interval=WORKER_INTERVAL,
        logger=logger,
    )


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
    from app.services.bars_scheduler_worker_runtime import (
        run_bars_scheduler_worker_runtime,
    )

    await run_bars_scheduler_worker_runtime(
        session_factory=AsyncSessionLocal,
        heartbeat_loop=_heartbeat_loop,
        should_shutdown=lambda: _shutdown,
        create_job_run=_create_job_run,
        finish_job_run=_finish_job_run,
        recover_stale_job_runs=recover_stale_scheduler_job_runs,
        logger=logger,
    )


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
    from app.services.strategy_scheduler_worker_runtime import (
        run_strategy_scheduler_worker_runtime,
    )

    await run_strategy_scheduler_worker_runtime(
        session_factory=AsyncSessionLocal,
        heartbeat_loop=_heartbeat_loop,
        should_shutdown=lambda: _shutdown,
        create_job_run=_create_job_run,
        finish_job_run=_finish_job_run,
        update_job_heartbeat=_update_job_heartbeat,
        recover_stale_job_runs=recover_stale_scheduler_job_runs,
        logger=logger,
    )


async def run_calendar_scheduler_worker() -> None:
    """日历调度 Worker 入口（兼容 façade）。

    实际 lifecycle（APScheduler 调度、calendar_job、启动恢复、shutdown）已抽到
    :mod:`app.services.calendar_scheduler_worker_runtime`。本函数只负责从
    composition root 注入依赖并保持 public 名称兼容。

    行为契约（cron / timezone / job identity / retry / transaction / heartbeat /
    shutdown / logging / exception）完全不变；详见 PANJI-GOV-W1。
    """
    from app.services.calendar_scheduler_worker_runtime import (
        run_calendar_scheduler_worker_runtime,
    )

    await run_calendar_scheduler_worker_runtime(
        session_factory=AsyncSessionLocal,
        heartbeat_loop=_heartbeat_loop,
        should_shutdown=lambda: _shutdown,
        create_job_run=_create_job_run,
        finish_job_run=_finish_job_run,
        recover_stale_job_runs=recover_stale_scheduler_job_runs,
        logger=logger,
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
    from app.services.monitor_scheduler_worker_runtime import (
        run_monitor_scheduler_worker_runtime,
    )

    await run_monitor_scheduler_worker_runtime(
        session_factory=AsyncSessionLocal,
        heartbeat_loop=_heartbeat_loop,
        should_shutdown=lambda: _shutdown,
        recover_stale_job_runs=recover_stale_scheduler_job_runs,
        create_job_run=_create_job_run,
        finish_job_run=_finish_job_run,
        monotonic_clock=_time_monotonic,
        logger=logger,
    )


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
                # [CRASH-RESUME-SLICE / P0-C] 同 slot 新 incarnation 已启动 →
                # 立即中断上一代进程遗留的 running 任务，无需等待 4h lease。
                replaced = await recover_replaced_incarnation_runs(db, _WORKER_INSTANCE_ID)
                # [PRD §4.3 JOB-01] 自动恢复 interrupted 的盘后任务 → resume_queued
                resumed = await auto_resume_interrupted_after_close_runs(db)
                stale_marked = await mark_stale_worker_heartbeats(db)
                await db.commit()
                if replaced > 0:
                    logger.info("[Recovery] 看门狗 incarnation 替换恢复: %d 个旧进程任务", replaced)
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
    """[AfterCloseWorker] - 单次轮询（thin façade）。

    领取/执行实现已迁移到 app.services.after_close_orchestrator_worker_poll.poll_after_close_once；
    本函数仅做依赖注入（session_factory / worker_instance_id / logger）后委托，
    不再包含任何领取 / fencing / 执行逻辑。
    """
    from app.services.after_close_orchestrator_worker_poll import poll_after_close_once

    return await poll_after_close_once(
        session_factory=AsyncSessionLocal,
        worker_instance_id=_WORKER_INSTANCE_ID,
        logger=logger,
    )


async def run_after_close_orchestrator_worker() -> None:
    """[AfterCloseWorker] - 盘后编排独立 Worker 入口（thin façade）。

    实际 process lifecycle 已抽到 ``app.services.after_close_orchestrator_worker_runtime``；
    这里只注入 worker 拥有的 process/canonical 依赖，不持有 lifecycle 逻辑。
    """
    from app.services.after_close_orchestrator_worker_runtime import (
        run_after_close_orchestrator_worker_runtime,
    )

    await run_after_close_orchestrator_worker_runtime(
        session_factory=AsyncSessionLocal,
        heartbeat_loop=_heartbeat_loop,
        should_shutdown=lambda: _shutdown,
        worker_interval=lambda: WORKER_INTERVAL,
        worker_instance_id=_WORKER_INSTANCE_ID,
        recover_stale_job_runs=recover_stale_scheduler_job_runs,
        recover_replaced_runs=recover_replaced_incarnation_runs,
        auto_resume_runs=auto_resume_interrupted_after_close_runs,
        poll_once=_after_close_poll_once,
        run_auction_co_process=_run_auction_scheduler_co_process,
        logger=logger,
    )


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
    """[ChipConsensusWorker] - 盘后筹码共识独立 Worker（thin façade）。

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

    lifecycle 实现已迁移到 app.services.chip_consensus_worker_runtime.run_chip_consensus_worker_runtime；
    本函数仅做依赖注入后委托，独立创建 chip_consensus heartbeat。
    """
    from app.services.chip_consensus_worker_runtime import (
        run_chip_consensus_worker_runtime,
    )

    await run_chip_consensus_worker_runtime(
        session_factory=AsyncSessionLocal,
        heartbeat_loop=_heartbeat_loop,
        recover_stale_job_runs=recover_stale_scheduler_job_runs,
        poll_once=_chip_consensus_poll_once,
        should_shutdown=lambda: _shutdown,
        worker_interval=lambda: WORKER_INTERVAL,
        logger=logger,
    )


# =============================================================================
# Review Bootstrap Worker - 已退休（REVIEW-BACKEND-FINAL-CLOSURE Phase 5）
# 历史回填 bootstrap 的可达代码路径已物理删除；SchedulerJobRun 槽位保留为
# 历史兼容（不 DROP），但不再有 Worker 领取或执行 review bootstrap 任务。
# =============================================================================


# [REVIEW-BACKEND-FINAL-CLOSURE Phase 5] Review bootstrap Worker 已退休。
# run_review_bootstrap_worker / _review_bootstrap_poll_once 物理删除，
# 不再有 Worker 领取或执行 review bootstrap 任务（SchedulerJobRun 槽位保留不 DROP）。


# =============================================================================
# [P0-3 修复 2026-07-31] Auction Scheduler Worker - 竞价分析调度
# =============================================================================


async def _auction_scheduler_poll_once() -> bool:
    """[P0-3] Auction Scheduler 单次轮询（thin façade）。

    领取/执行实现已迁移到 app.services.auction_scheduler_worker_poll.poll_auction_scheduler_once；
    本函数仅做依赖注入（session_factory / worker_instance_id / logger）后委托，
    不再包含任何时间窗口检查 / 任务创建 / 领取 / fencing / 执行逻辑。
    """
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    return await poll_auction_scheduler_once(
        session_factory=AsyncSessionLocal,
        worker_instance_id=_WORKER_INSTANCE_ID,
        logger=logger,
    )


async def _run_auction_scheduler_co_process() -> None:
    """[P0-3] Auction co-process（thin façade，依附 AfterClose 进程 lifecycle）。

    lifecycle 实现已迁移到 app.services.auction_scheduler_worker_runtime.run_auction_scheduler_co_process_runtime；
    本函数仅做依赖注入后委托，不创建独立 heartbeat（由 AfterClose 进程统一承载）。
    """
    from app.services.auction_scheduler_worker_runtime import (
        run_auction_scheduler_co_process_runtime,
    )

    await run_auction_scheduler_co_process_runtime(
        session_factory=AsyncSessionLocal,
        recover_stale_job_runs=recover_stale_scheduler_job_runs,
        poll_once=_auction_scheduler_poll_once,
        should_shutdown=lambda: _shutdown,
        logger=logger,
    )


async def run_auction_scheduler_worker() -> None:
    """[P0-3] Auction standalone debug worker（thin façade，调试入口；生产入口为 _run_auction_scheduler_co_process）。

    lifecycle 实现已迁移到 app.services.auction_scheduler_worker_runtime.run_auction_scheduler_worker_runtime；
    本函数仅做依赖注入后委托，独立创建 auction_scheduler heartbeat。
    """
    from app.services.auction_scheduler_worker_runtime import (
        run_auction_scheduler_worker_runtime,
    )

    await run_auction_scheduler_worker_runtime(
        session_factory=AsyncSessionLocal,
        recover_stale_job_runs=recover_stale_scheduler_job_runs,
        poll_once=_auction_scheduler_poll_once,
        heartbeat_loop=_heartbeat_loop,
        should_shutdown=lambda: _shutdown,
        logger=logger,
    )


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

    # [2026-08-11 AFTER-CLOSE-ENHANCEMENT-HEAD-OF-LINE-BLOCKING v2]
    # WORKER_TYPE=after_close_orchestrator 在 run_after_close_orchestrator_worker 内
    # 同时启动 Chip / Review bootstrap 独立 co-process（复用各自 worker loop），
    # [REVIEW-BACKEND-FINAL-CLOSURE Phase 5] WORKER_TYPE=review_bootstrap 已退休：
    # Review bootstrap Worker 物理删除，不再注册或领取任务。

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
    assert callable(_recovery_watchdog_loop), "_recovery_watchdog_loop 应可调用"
    print("OK: 配置验证通过")
    asyncio.run(main())
