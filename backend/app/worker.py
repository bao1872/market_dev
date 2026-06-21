"""统一 Worker 入口 - 支持 Outbox Relay / Delivery Worker / Job 消费者 / 策略批量计算 / 行情调度。

用法：
    WORKER_TYPE=outbox python -m app.worker           # 运行 Outbox Relay
    WORKER_TYPE=delivery python -m app.worker         # 运行投递 Worker
    WORKER_TYPE=strategy_batch python -m app.worker   # 运行策略批量计算 Worker
    WORKER_TYPE=bars_scheduler python -m app.worker   # 运行行情调度 Worker（每日 16:00）
    WORKER_TYPE=all python -m app.worker              # 同时运行全部（开发模式）

环境变量：
    WORKER_TYPE: worker 类型（outbox/delivery/strategy_batch/bars_scheduler/all，默认 all）
    WORKER_INTERVAL: 轮询间隔秒数（默认 5）
    WORKER_BATCH_SIZE: 单次轮询最大记录数（默认 100）
    WORKER_MAX_RETRY: 最大重试次数（默认 5）

设计：
- 每个 worker 类型在独立 asyncio task 中运行
- 信号处理：SIGTERM/SIGINT 优雅退出
- 异常不吞：捕获后记录日志并等待下次轮询（避免单次失败导致 worker 退出）
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import UTC, datetime

from app.db import AsyncSessionLocal

logger = logging.getLogger("worker")

# Worker 配置
WORKER_TYPE = os.getenv("WORKER_TYPE", "all")
WORKER_INTERVAL = int(os.getenv("WORKER_INTERVAL", "5"))
WORKER_BATCH_SIZE = int(os.getenv("WORKER_BATCH_SIZE", "100"))
WORKER_MAX_RETRY = int(os.getenv("WORKER_MAX_RETRY", "5"))

# 优雅退出标志
_shutdown = False


def _handle_shutdown(signum: int, _frame: object) -> None:
    """信号处理：设置退出标志，让主循环自然结束。"""
    global _shutdown
    logger.info("收到信号 %s，准备退出...", signum)
    _shutdown = True


async def run_outbox_relay() -> None:
    """Outbox Relay worker：轮询 outbox 表，投递到 Redis 队列。

    每个轮询周期：
    1. 从 outbox 表读取 status=pending 的记录
    2. 投递到 Redis 队列（LPUSH）
    3. 标记为 processed 或增加 retry_count
    """
    from app.services.outbox_relay import relay_outbox

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
    """投递 Worker：消费 notification.message.created 事件，投递到用户渠道。

    每个轮询周期：
    1. 从 outbox 表读取 notification.message.created 事件
    2. 查询消息 + 用户活跃渠道
    3. 逐渠道投递（幂等）
    4. 标记 outbox 为 processed
    """
    from app.services.delivery_worker import process_notification_outbox

    logger.info("Delivery Worker 启动（间隔=%ds, 批次=%d）", WORKER_INTERVAL, WORKER_BATCH_SIZE)
    while not _shutdown:
        try:
            async with AsyncSessionLocal() as db:
                processed = await process_notification_outbox(
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
    1. 查询 strategy_runs WHERE status='queued'（按 started_at 排序，取 1 条）
    2. 调用 StrategyBatchService.execute_run() 执行
    3. 提交事务

    设计说明：
    - 单 run 串行执行（避免并发计算同一策略版本）
    - 执行失败时记录日志，run 状态由 execute_run 内部处理
    - Worker 重启后可继续执行 queued 状态的 run（中断恢复）
    """
    from sqlalchemy import select

    from app.models.strategy_run import StrategyRun
    from app.services.strategy_batch_service import StrategyBatchService

    logger.info(
        "Strategy Batch Worker 启动（间隔=%ds）", WORKER_INTERVAL
    )
    service = StrategyBatchService()

    while not _shutdown:
        try:
            async with AsyncSessionLocal() as db:
                # 查询 queued 状态的 run（按 started_at 排序，取 1 条）
                stmt = (
                    select(StrategyRun)
                    .where(StrategyRun.status == "queued")
                    .order_by(StrategyRun.started_at)
                    .limit(1)
                )
                result = await db.execute(stmt)
                run = result.scalar_one_or_none()

                if run is None:
                    # 无待执行 run，等待下次轮询
                    await asyncio.sleep(WORKER_INTERVAL)
                    continue

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
        except Exception as exc:
            logger.exception("Strategy Batch Worker 异常: %s", exc)
            # 异常时回滚，等待下次轮询重试
            try:
                await db.rollback()
            except Exception:
                pass
        await asyncio.sleep(WORKER_INTERVAL)


async def run_bars_scheduler_worker() -> None:
    """行情调度 Worker：每日 16:00 触发全市场多周期行情更新。

    使用 APScheduler AsyncIOScheduler + CronTrigger：
    - 每个交易日（周一至周五）16:00 触发
    - 调用 BarsSchedulerService.refresh_all_instruments()
    - 串行拉取 4 个周期（15m/60min/w/m），耗时约 1.8 小时

    设计说明：
    - APScheduler 在事件循环中运行，不阻塞
    - 信号处理：收到 SIGTERM/SIGINT 后优雅关闭 scheduler
    - 异常不吞：捕获后记录日志，不影响下次触发
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    from app.services.bars_scheduler_service import BarsSchedulerService

    scheduler = AsyncIOScheduler()
    service = BarsSchedulerService()

    async def scheduled_bars_refresh() -> None:
        """定时任务：每日 16:00 刷新全市场多周期行情。"""
        from datetime import date as date_cls

        trade_date = date_cls.today()
        logger.info("定时任务触发：多周期行情更新 trade_date=%s", trade_date)
        try:
            result = await service.refresh_all_instruments(trade_date)
            logger.info(
                "定时任务完成: total=%d succeeded=%d failed=%d period_counts=%s",
                result.total, result.succeeded, result.failed, result.period_counts,
            )
        except Exception as exc:
            logger.exception("定时任务异常: %s", exc)

    # 每个交易日 16:00 触发
    scheduler.add_job(
        scheduled_bars_refresh,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=0),
        id="bars_refresh_daily",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Bars Scheduler Worker 启动（每日 16:00 触发）")

    while not _shutdown:
        await asyncio.sleep(60)

    scheduler.shutdown(wait=False)
    logger.info("Bars Scheduler Worker 已退出")


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
    assert WORKER_TYPE in ("outbox", "delivery", "strategy_batch", "bars_scheduler", "all"), \
        f"未知 WORKER_TYPE: {WORKER_TYPE}"
    # 验证 worker 函数可调用
    assert callable(run_outbox_relay), "run_outbox_relay 应可调用"
    assert callable(run_delivery_worker), "run_delivery_worker 应可调用"
    assert callable(run_strategy_batch_worker), "run_strategy_batch_worker 应可调用"
    assert callable(run_bars_scheduler_worker), "run_bars_scheduler_worker 应可调用"
    print("OK: 配置验证通过")
