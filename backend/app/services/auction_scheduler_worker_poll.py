"""[AuctionScheduler] Claim/Poll 业务 owner（W6A：从 worker.py 迁移，仅搬 owner 不改状态机）。

函数：poll_auction_scheduler_once(*, session_factory, worker_instance_id, logger) -> bool

原始 worker._auction_scheduler_poll_once() 仅保留为 thin façade，将依赖注入后委托给本模块。
本模块不引入新的 claim 服务、不切换 repository、不改 SQL / 状态机 / 事务边界 /
fencing / missing-trade_date 分支 / execute 异常语义。

与 AfterClose 的关键差异（冻结，禁止“看起来合理”地照抄）：
- Auction claim 时【不重写】lease_expires_at（已在 create 时设置）；
  AfterClose claim 会重写 lease_expires_at。
- Auction lease_epoch 表达式 = (lease_epoch or 0) + 1；
  AfterClose = lease_epoch + 1。
- Auction missing trade_date / unknown job 失败分支【不写 JobRunEvent】，
  AfterClose 写 ERROR 事件。
- Auction 失败 error_message 为「任务缺少 trade_date」/「未知 job_name: {job_name}」。

SQL 语义（冻结，get_queued_auction_job 是正式 owner，不搬 SQL）：
    select(SchedulerJobRun)
    .where(job_name.in_(("auction_final", "auction_open_confirmation")),
           status == "queued")
    .order_by(created_at)
    .limit(1)
    .with_for_update(skip_locked=True)

事务边界（冻结）：
    - 时间窗口检查使用独立 session（仅 trading-day check + 可能的 job 创建），
      commit 无论 is_new 与否都执行（is_new 只控制“创建”日志）。
    - 时间窗口检查失败（异常）不阻止随后 claim 已存在的 queued job。
    - 非交易日仅跳过 timed job 创建，仍继续 claim queued job。
    - 领取 queued job：拿到 row lock → 写 running + worker + heartbeat + lease_epoch
      （不重写 lease_expires_at）→ commit（必须在 execute 之前）
      → 再解析 metadata / trade_date / id / epoch → 执行。
"""
from __future__ import annotations

import json
from datetime import date as date_cls
from datetime import datetime
from zoneinfo import ZoneInfo

from app.models.scheduler_job_run import SchedulerJobRun


async def poll_auction_scheduler_once(
    *,
    session_factory,
    worker_instance_id: str,
    logger,
) -> bool:
    """[P0-3] Auction Scheduler 单次轮询：

    1. 检查时间窗口：09:25:05 ± 30s → 创建 auction_final:{date}
                      10:00:00 ± 30s → 创建 auction_open_confirmation:{date}
    2. 领取一条 queued auction job 并执行（FOR UPDATE SKIP LOCKED）

    Returns:
        True 如果领取并执行了任务，False 如果无任务可执行
    """
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
        async with session_factory() as db:
            trading = await is_trading_day_async(db, now.date())

        if trading:
            # 09:25:05 ± 30s → 创建 auction_final job
            if should_create_auction_final_job(now):
                async with session_factory() as db:
                    job_run, is_new = await create_auction_final_job(
                        db, now.date(),
                        worker_instance_id=worker_instance_id,
                    )
                    if is_new:
                        logger.info(
                            "[AuctionScheduler] 创建 auction_final job: run_id=%s, trade_date=%s",
                            job_run.id if job_run else None, now.date(),
                        )
                    await db.commit()
            # 10:00:00 ± 30s → 创建 auction_open_confirmation job
            elif should_create_auction_open_confirmation_job(now):
                async with session_factory() as db:
                    job_run, is_new = await create_auction_open_confirmation_job(
                        db, now.date(),
                        worker_instance_id=worker_instance_id,
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
    async with session_factory() as db:
        job_run = await get_queued_auction_job(db)
        if job_run is None:
            await db.rollback()
            return False

        # 领取：更新 status='running' + worker + heartbeat + lease_epoch（fencing）
        now_claim = datetime.now(tz)
        job_run.status = "running"
        job_run.worker_instance_id = worker_instance_id
        if job_run.started_at is None:
            job_run.started_at = now_claim
        job_run.heartbeat_at = now_claim
        # lease_expires_at 已在 create 时设置；fencing epoch 递增（Auction 不重写）
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
        async with session_factory() as db:
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
                worker_id=worker_instance_id,
                lease_epoch=current_lease_epoch,
            )
        elif job_name == AUCTION_OPEN_CONFIRMATION_JOB_NAME:
            await execute_auction_open_confirmation_run(
                job_run_id=job_run_id,
                trade_date=trade_date,
                worker_id=worker_instance_id,
                lease_epoch=current_lease_epoch,
            )
        else:
            logger.error(
                "[AuctionScheduler] 未知 job_name=%s，标记 failed: job_run_id=%s",
                job_name, job_run_id,
            )
            async with session_factory() as db:
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
