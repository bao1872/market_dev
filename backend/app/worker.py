"""统一 Worker 入口 - 支持 Outbox Relay / Delivery Worker / Job 消费者 / 策略批量计算 / 行情调度 / 选股策略调度。

用法：
    WORKER_TYPE=outbox python -m app.worker           # 运行 Outbox Relay
    WORKER_TYPE=delivery python -m app.worker         # 运行投递 Worker
    WORKER_TYPE=strategy_batch python -m app.worker   # 运行策略批量计算 Worker
    WORKER_TYPE=bars_scheduler python -m app.worker   # 运行行情调度 Worker（每日 16:00）
    WORKER_TYPE=strategy_scheduler python -m app.worker   # 运行选股策略调度 Worker（每日 18:00）
    WORKER_TYPE=calendar_scheduler python -m app.worker  # 运行日历调度 Worker（每日 02:00）
    WORKER_TYPE=monitor_scheduler python -m app.worker    # 运行监控调度 Worker（交易时段 9:30-15:00）
    WORKER_TYPE=all python -m app.worker              # 同时运行全部（开发模式）

环境变量：
    WORKER_TYPE: worker 类型（outbox/delivery/strategy_batch/bars_scheduler/strategy_scheduler/calendar_scheduler/monitor_scheduler/all，默认 all）
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
from zoneinfo import ZoneInfo

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
    - 串行拉取 3 个周期（d/15m/60m），耗时约 1.8 小时

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

        from app.services.calendar_service import is_trading_day_async

        trade_date = date_cls.today()

        # 交易日历判断（替代简单的 weekday 判断）
        async with AsyncSessionLocal() as session:
            is_trading = await is_trading_day_async(session, trade_date)

        if not is_trading:
            logger.info("非交易日 %s，跳过行情刷新", trade_date)
            return

        logger.info("交易日 %s，开始行情刷新", trade_date)
        try:
            result = await service.refresh_all_instruments(trade_date)
            logger.info(
                "定时任务完成: total=%d succeeded=%d failed=%d period_counts=%s",
                result.total, result.succeeded, result.failed, result.period_counts,
            )
        except Exception as exc:
            logger.exception("定时任务异常: %s", exc)

    # 每日 16:00 触发（含非交易日，由内部交易日历判断是否执行）
    scheduler.add_job(
        scheduled_bars_refresh,
        CronTrigger(day_of_week="mon-sun", hour=16, minute=0, timezone=ZoneInfo("Asia/Shanghai")),
        id="bars_refresh_daily",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Bars Scheduler Worker 启动（每日 16:00 刷新行情）")

    while not _shutdown:
        await asyncio.sleep(60)

    scheduler.shutdown(wait=False)
    logger.info("Bars Scheduler Worker 已退出")


async def run_strategy_scheduler_worker() -> None:
    """选股策略调度 Worker：每日 18:00 触发所有 kind=selector 策略的批量计算。

    使用 APScheduler AsyncIOScheduler + CronTrigger：
    - 每个交易日（周一至周五）18:00 触发
    - 查询 strategy_definitions WHERE kind='selector' 的所有策略
    - 为每个策略调用 StrategyBatchService.create_batch_run(run_type="scheduled")
    - 创建的 queued run 由 strategy_batch worker 轮询执行

    设计说明：
    - 18:00 触发（bars_scheduler 16:00 刷新行情，约 1.8 小时完成后执行，避免竞态）
    - 数据就绪检查：check_data_readiness() 覆盖率 < 90% 时阻断 DSA 执行
    - 单个策略创建失败不阻塞其他策略，记录日志继续
    - 幂等：同一 strategy_key + trade_date + run_type=scheduled 只创建一次
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from sqlalchemy import exists, select

    from app.models.strategy import StrategyDefinition, StrategyVersion
    from app.services.strategy_batch_service import StrategyBatchService

    scheduler = AsyncIOScheduler()
    service = StrategyBatchService()

    async def scheduled_strategy_run() -> None:
        """定时任务：每日 18:00 为所有 selector 策略创建 queued run。"""
        from datetime import date as date_cls

        from app.services.calendar_service import is_trading_day_async

        trade_date = date_cls.today()

        # 交易日历判断（替代简单的 weekday 判断）
        async with AsyncSessionLocal() as session:
            is_trading = await is_trading_day_async(session, trade_date)

        if not is_trading:
            logger.info("非交易日 %s，跳过选股策略计算", trade_date)
            return

        logger.info("交易日 %s，开始选股策略计算", trade_date)
        try:
            async with AsyncSessionLocal() as db:
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
                    logger.warning("未找到 kind=selector 的策略，跳过")
                    return

                logger.info("待计算的 selector 策略: %s", strategy_keys)
                succeeded = 0
                failed = 0
                for strategy_key in strategy_keys:
                    try:
                        run = await service.create_batch_run(
                            db=db,
                            strategy_key=strategy_key,
                            trade_date=trade_date,
                            run_type="scheduled",
                        )
                        await db.commit()
                        logger.info(
                            "策略 %s 创建 run 成功: run_id=%s",
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

                logger.info(
                    "定时任务完成: total=%d succeeded=%d failed=%d",
                    len(strategy_keys), succeeded, failed,
                )
        except Exception as exc:
            logger.exception("选股策略调度任务异常: %s", exc)

    # 每日 18:00 触发（含非交易日，由内部交易日历判断是否执行）
    scheduler.add_job(
        scheduled_strategy_run,
        CronTrigger(day_of_week="mon-sun", hour=18, minute=0, timezone=ZoneInfo("Asia/Shanghai")),
        id="strategy_run_daily",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Strategy Scheduler Worker 启动（每日 18:00 触发）")

    while not _shutdown:
        await asyncio.sleep(60)

    scheduler.shutdown(wait=False)
    logger.info("Strategy Scheduler Worker 已退出")


async def run_calendar_scheduler_worker() -> None:
    """日历调度 Worker：每日 02:00 从 pytdx 拉取当年交易日历并更新 DB。

    使用 APScheduler AsyncIOScheduler + CronTrigger：
    - 每日 02:00 触发
    - 调用 seed_calendar_from_pytdx(session, year=当前年份)
    - 更新或插入交易日历记录

    设计说明：
    - APScheduler 在事件循环中运行，不阻塞
    - 信号处理：收到 SIGTERM/SIGINT 后优雅关闭 scheduler
    - 异常不吞：捕获后记录日志，不影响下次触发
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = AsyncIOScheduler()

    async def calendar_job() -> None:
        """每日凌晨刷新交易日历（从 pytdx 拉取当年日历并更新 DB）。"""
        from datetime import date as date_cls

        async with AsyncSessionLocal() as session:
            try:
                from app.services.calendar_seed import seed_calendar_from_pytdx
                year = date_cls.today().year
                count = await seed_calendar_from_pytdx(session, year=year)
                logger.info("日历刷新完成: year=%d, %d 条记录更新", year, count)
            except Exception as exc:
                logger.error("日历刷新失败: %s", exc)
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


async def run_monitor_scheduler_worker() -> None:
    """监控调度 Worker：交易时段内每 30 秒执行一轮监控。

    使用 APScheduler AsyncIOScheduler + 交易时段判断：
    - 交易日 9:30-11:30：每 30 秒执行一轮
    - 午休 11:30-13:00：暂停
    - 交易日 13:00-15:00：每 30 秒执行一轮
    - 非交易日：不执行

    调用 MonitorBatchService.execute_monitor_cycle() 执行单轮监控。

    设计说明：
    - 不使用 CronTrigger（需要精确到秒级的循环控制）
    - 使用 while 循环 + asyncio.sleep(30) 实现交易时段内循环
    - 交易日检查：复用 services/calendar_service.is_trading_day()
    - 午休暂停：11:30-13:00 期间 sleep 等待
    - 优雅退出：检查 _shutdown 标志
    """
    from datetime import time as time_cls

    from app.services.monitor_batch_service import MonitorBatchService

    service = MonitorBatchService()
    cycle_interval = 30  # 秒
    morning_start = time_cls(9, 30)
    morning_end = time_cls(11, 30)
    afternoon_start = time_cls(13, 0)
    afternoon_end = time_cls(15, 0)

    logger.info(
        "Monitor Scheduler Worker 启动（交易时段 %s-%s, %s-%s, 间隔=%ds）",
        morning_start, morning_end, afternoon_start, afternoon_end, cycle_interval,
    )

    # 启动成功飞书通知
    await _notify_monitor_status("监控服务已启动", "交易时段 9:30-11:30 / 13:00-15:00\n每 30 秒执行一轮监控")

    while not _shutdown:
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

            current_time = now.time()

            # 判断是否在交易时段
            in_morning = morning_start <= current_time < morning_end
            in_afternoon = afternoon_start <= current_time < afternoon_end

            if not (in_morning or in_afternoon):
                # 非交易时段，等待
                if current_time < morning_start:
                    # 开盘前，等待到 9:30
                    wait_seconds = (
                        datetime(now.year, now.month, now.day, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")) - now
                    ).total_seconds()
                    if wait_seconds > 0:
                        logger.info("等待开盘，还需 %d 秒", int(wait_seconds))
                        await asyncio.sleep(min(wait_seconds, 60))
                elif morning_end <= current_time < afternoon_start:
                    # 午休，等待到 13:00
                    wait_seconds = (
                        datetime(now.year, now.month, now.day, 13, 0, tzinfo=ZoneInfo("Asia/Shanghai")) - now
                    ).total_seconds()
                    if wait_seconds > 0:
                        logger.info("午休中，等待 %d 秒", int(wait_seconds))
                        await asyncio.sleep(min(wait_seconds, 60))
                elif current_time >= afternoon_end:
                    # 收盘后，等待到明天
                    await asyncio.sleep(300)
                continue

            # 交易时段内，执行监控周期
            async with AsyncSessionLocal() as db:
                result = await service.execute_monitor_cycle(db)
                await db.commit()
                if result.total_events_written > 0:
                    logger.info(
                        "监控周期完成: instruments=%d events=%d notifications=%d",
                        result.total_instruments,
                        result.total_events_written,
                        result.total_notifications_created,
                    )
                else:
                    logger.debug(
                        "监控周期完成: instruments=%d events=0",
                        result.total_instruments,
                    )

        except Exception as exc:
            logger.exception("Monitor Scheduler 异常: %s", exc)
            # 异常退出飞书通知
            await _notify_monitor_status("监控服务异常", str(exc), is_error=True)

        # 交易时段内每 30 秒一轮
        await asyncio.sleep(cycle_interval)

    logger.info("Monitor Scheduler Worker 已退出")


async def _notify_monitor_status(
    title: str, content: str, *, is_error: bool = False,
) -> None:
    """向所有配置了飞书渠道的用户发送监控状态通知。

    通知失败不影响主流程（仅记录警告）。

    TODO: [monitor_scheduler] 当前直接调用 adapter.send() 绕过 Outbox 管道。
    应改为 create_message → write_outbox(notification.message.created) → Delivery Worker 投递，
    与业务通知保持一致的投递语义（重试、幂等、静默时段）。风险：监控服务自身异常时
    Outbox/Delivery Worker 可能也不可用，需评估是否保留直接发送作为降级路径。
    """
    try:
        from sqlalchemy import select

        from app.models.notification import NotificationChannel
        from app.schemas.notification import NotificationMessageDTO
        from app.services.channel_adapter import get_adapter

        emoji = "❌" if is_error else "✅"
        text = f"{emoji} {title}\n\n{content}"

        async with AsyncSessionLocal() as db:
            # 查询所有活跃的飞书平台应用渠道
            stmt = select(NotificationChannel).where(
                NotificationChannel.adapter_type == "feishu_platform_app",
                NotificationChannel.status == "active",
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
                        body=text,
                        message_type="text",
                        template_key="monitor_status",
                        template_version="1.0.0",
                        summary=text[:100],
                        data_time=datetime.now(UTC).isoformat(),
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
    assert WORKER_TYPE in ("outbox", "delivery", "strategy_batch", "bars_scheduler", "strategy_scheduler", "calendar_scheduler", "monitor_scheduler", "all"), \
        f"未知 WORKER_TYPE: {WORKER_TYPE}"
    # 验证 worker 函数可调用
    assert callable(run_outbox_relay), "run_outbox_relay 应可调用"
    assert callable(run_delivery_worker), "run_delivery_worker 应可调用"
    assert callable(run_strategy_batch_worker), "run_strategy_batch_worker 应可调用"
    assert callable(run_bars_scheduler_worker), "run_bars_scheduler_worker 应可调用"
    assert callable(run_strategy_scheduler_worker), "run_strategy_scheduler_worker 应可调用"
    assert callable(run_calendar_scheduler_worker), "run_calendar_scheduler_worker 应可调用"
    assert callable(run_monitor_scheduler_worker), "run_monitor_scheduler_worker 应可调用"
    print("OK: 配置验证通过")
    asyncio.run(main())
