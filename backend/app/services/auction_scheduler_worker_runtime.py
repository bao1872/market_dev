"""[AuctionScheduler] lifecycle runtime（W6B：从 worker.py 迁移，仅搬 lifecycle 不改契约）。

两个 public 入口：
- run_auction_scheduler_co_process_runtime(...)   Auction co-process（附 AfterClose 进程，无独立 heartbeat）
- run_auction_scheduler_worker_runtime(...)       Auction standalone debug worker（独立 heartbeat）

两者复用私有 core _run_auction_scheduler_loop（共享 startup recovery + poll loop + SIGTERM drain）。
差异仅由参数表达，不引入 mode 开关 / 通用基类 / logging framework：
- co-process: create_heartbeat=None（不创建 heartbeat，依附 AfterClose 进程 lifecycle）
- standalone: create_heartbeat=heartbeat_loop, hb_worker_name="auction_scheduler"

冻结合同（逐字保留 worker 原行为）：
- Poll interval 在每个 public 入口 lazy import AUCTION_SCHEDULER_POLL_INTERVAL，再传入 core（不在 worker 顶层 import）。
- standalone 顺序：heartbeat task 创建 → startup log → recovery → poll loop。
- recovery：new session → recover_stale_job_runs(db) → commit → if recovered>0 info（commit 不在 if 内）。
- recovery 异常非致命：except logger.exception → 仍进 poll loop（不 fail-fast）。
- loop entry：while not should_shutdown()。
- 每轮 poll exception 隔离：try poll except logger.exception → 仍做 shutdown check（不跳过）。
- poll 后第二次 shutdown check：poll → if should_shutdown: drain log → break → sleep（shutdown 后不 sleep）。
- 正常 sleep exact interval（asyncio.sleep(poll_interval)，无 jitter/backoff）。
- co-process 不得额外启动 heartbeat；standalone 恰好一次 heartbeat，参数 "auction_scheduler"。
- 日志文案区分 co-process / standalone，保留运行模式信息（排障用）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class _Messages:
    """同一套 loop 的 co-process / standalone 日志文案常量（明确两组，不揉合）。"""

    startup: str
    recovery: str
    recovery_error: str
    poll_error: str
    drain: str
    drain_complete: str


async def _run_auction_scheduler_loop(
    *,
    session_factory,
    recover_stale_job_runs,
    poll_once,
    should_shutdown,
    logger,
    poll_interval: int,
    messages: _Messages,
    create_heartbeat,
    hb_worker_name: str | None,
) -> None:
    # standalone：heartbeat task 在 startup log / recovery 之前创建（冻结合同 2）
    _hb_task = None
    if create_heartbeat is not None:
        _hb_task = asyncio.create_task(create_heartbeat(hb_worker_name))

    logger.info(messages.startup, poll_interval)

    # 启动恢复：清理上次崩溃残留的 running 任务
    try:
        async with session_factory() as db:
            recovered = await recover_stale_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info(messages.recovery, recovered)
    except Exception as exc:
        logger.exception(messages.recovery_error, exc)

    while not should_shutdown():
        try:
            await poll_once()
        except Exception as exc:
            # 异常隔离：单次 poll 异常仅记录日志，下一轮继续
            logger.exception(messages.poll_error, exc)
        if should_shutdown():
            logger.info(messages.drain)
            break
        await asyncio.sleep(poll_interval)

    logger.info(messages.drain_complete)


async def run_auction_scheduler_co_process_runtime(
    *,
    session_factory,
    recover_stale_job_runs,
    poll_once,
    should_shutdown,
    logger,
) -> None:
    """[P0-3] Auction co-process runtime（附 AfterClose 进程，无独立 heartbeat）。"""
    from app.services.auction_scheduler_service import AUCTION_SCHEDULER_POLL_INTERVAL

    messages = _Messages(
        startup="[AuctionScheduler] co-process 启动（间隔=%ds，触发窗口 09:25:05/10:00:00 Asia/Shanghai）",
        recovery="[AuctionScheduler] co-process 启动恢复: %d 个过期任务",
        recovery_error="[AuctionScheduler] co-process 启动恢复异常: %s",
        poll_error="[AuctionScheduler] co-process 轮询异常: %s",
        drain="[AuctionScheduler] co-process SIGTERM drain: 不再领取新任务，准备退出",
        drain_complete="[AuctionScheduler] co-process SIGTERM drain complete",
    )
    await _run_auction_scheduler_loop(
        session_factory=session_factory,
        recover_stale_job_runs=recover_stale_job_runs,
        poll_once=poll_once,
        should_shutdown=should_shutdown,
        logger=logger,
        poll_interval=AUCTION_SCHEDULER_POLL_INTERVAL,
        messages=messages,
        create_heartbeat=None,
        hb_worker_name=None,
    )


async def run_auction_scheduler_worker_runtime(
    *,
    session_factory,
    recover_stale_job_runs,
    poll_once,
    heartbeat_loop,
    should_shutdown,
    logger,
) -> None:
    """[P0-3] Auction standalone debug worker runtime（独立 heartbeat）。"""
    from app.services.auction_scheduler_service import AUCTION_SCHEDULER_POLL_INTERVAL

    messages = _Messages(
        startup="[AuctionScheduler] 启动（间隔=%ds，触发窗口 09:25:05/10:00:00 Asia/Shanghai）",
        recovery="[AuctionScheduler] 启动恢复: %d 个过期任务",
        recovery_error="[AuctionScheduler] 启动恢复异常: %s",
        poll_error="[AuctionScheduler] 轮询异常: %s",
        drain="[AuctionScheduler] SIGTERM drain: 不再领取新任务，准备退出",
        drain_complete="[AuctionScheduler] SIGTERM drain complete, finished current item",
    )
    await _run_auction_scheduler_loop(
        session_factory=session_factory,
        recover_stale_job_runs=recover_stale_job_runs,
        poll_once=poll_once,
        should_shutdown=should_shutdown,
        logger=logger,
        poll_interval=AUCTION_SCHEDULER_POLL_INTERVAL,
        messages=messages,
        create_heartbeat=heartbeat_loop,
        hb_worker_name="auction_scheduler",
    )
