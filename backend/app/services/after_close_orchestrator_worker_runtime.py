"""AfterClose orchestrator main runtime (process lifecycle only).

Extracted verbatim from ``app.worker`` (PANJI-GOV-W5A).  This module owns the
AfterClose Worker *process lifecycle*:

* heartbeat task,
* Auction co-process startup (the task is still owned by
  ``worker._run_auction_scheduler_co_process`` — we only *call* it),
* startup recovery (stale -> replaced-incarnation -> auto-resume -> single commit),
* main poll loop (claim/poll exception isolation),
* SIGTERM drain (await co-process, no naked cancel),
* final exit log.

It does NOT own the task-claim / DB-fencing / auction logic; those remain in
``app.worker`` and are injected (``poll_once``, ``run_auction_co_process``).
The Auction callback is supplied by ``app.worker``; the automatic Chip co-process
and Review bootstrap execution are retired (no co-process is started for them).
This keeps the dangerous claim/fencing SQL out of scope for W5A.

Behavior is frozen: recovery order, commit point, exception isolation, SIGTERM
drain-without-cancel, and "no sleep after shutdown" are all preserved exactly.
No cron / fencing / transaction / state-update / retry semantics changed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

SessionFactory = Callable[[], Any]
HeartbeatLoop = Callable[[str], Coroutine[Any, Any, None]]
ShutdownProbe = Callable[[], bool]
WorkerInterval = Callable[[], int]
RecoverStaleJobRuns = Callable[[Any], Coroutine[Any, Any, int]]
RecoverReplacedRuns = Callable[[Any, str], Coroutine[Any, Any, int]]
AutoResumeRuns = Callable[[Any], Coroutine[Any, Any, int]]
PollOnce = Callable[[], Coroutine[Any, Any, bool]]
RunAuctionCoProcess = Callable[[], Coroutine[Any, Any, None]]


async def _drain_co_process(
    task: asyncio.Task[None] | None,
    name: str,
    logger: logging.Logger,
) -> None:
    """[SIGTERM drain] 等待一个 co-process 自然退出（drain 到当前业务 item terminal）。

    co-process 各自检查共享 should_shutdown，在当前业务 item 完成后退出循环。
    本函数**不**调用 Task.cancel()：long-running co-process 当前业务 item（当前仅 Auction）
    必须 drain 到 terminal，避免在 DB 中遗留 ownership 不清的 running job（naked orphan）。
    异常仅记录，不阻断其它 drain。
    """
    if task is None or task.done():
        return
    try:
        await task
    except Exception as exc:
        logger.warning("[AfterCloseWorker] %s co-process 退出异常: %s", name, exc)


async def run_after_close_orchestrator_worker_runtime(
    *,
    session_factory: SessionFactory,
    heartbeat_loop: HeartbeatLoop,
    should_shutdown: ShutdownProbe,
    worker_interval: WorkerInterval,
    worker_instance_id: str,
    recover_stale_job_runs: RecoverStaleJobRuns,
    recover_replaced_runs: RecoverReplacedRuns,
    auto_resume_runs: AutoResumeRuns,
    poll_once: PollOnce,
    run_auction_co_process: RunAuctionCoProcess,
    logger: logging.Logger,
) -> None:
    """[AfterCloseWorker] - 盘后编排独立 Worker：领取 queued 任务并执行。

    使用 FOR UPDATE SKIP LOCKED 领取任务，多个 Worker 实例只有一个能领取。
    每个轮询周期：
    1. 启动恢复（清理上次崩溃残留的 running 任务）
    2. poll_once 领取并执行一个 queued 任务
    3. sleep worker_interval() 后继续轮询

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

    [CHIP-RETIRE 2026-09-01] Chip 独立 co-process 已退役：
    - 本进程不再启动 Chip consensus co-process（原 _chip_co_process_task）。
    - 盘后主链不再创建 after_close_chip_consensus job，故不存在"长时 chip 任务占用
      mandatory executor"的对头阻塞问题；mandatory 主循环只领取/执行
      after_close_orchestrator。
    - SIGTERM 时仅 mandatory 与 Auction co-process 统一 drain。

    [REVIEW-BACKEND-FINAL-CLOSURE Phase 5] Review bootstrap 独立 co-process 已退休：
    - run_review_bootstrap_worker / _review_bootstrap_poll_once 已物理删除，本进程不再启动
      bootstrap co-process；历史回填由 canonical history replay（prepare_scope / canonical
      history run）接管，不再有独立的 bootstrap worker 入口。
    - mandatory 主循环只领取/执行 after_close_orchestrator（Chip co-process 已退役）。

    [SIGTERM drain] - 优雅退出（不强制中断当前 run，drain 到当前业务 item terminal）：
    - SIGTERM/SIGINT 由 _handle_shutdown 设置 _shutdown=True（全局标志）
    - 主循环在领取新任务前检查 _shutdown，若为 True 则不再领取新 item
    - 当前正在执行的 execute_after_close_run 完成后才退出（同步 await，不强制中断）；
      checkpoint（run status + heartbeat）由 execute_after_close_run 内部写入
    - 各 co-process（Auction）检查 _shutdown 后，在各自当前业务
      item 完成后退出（drain 到 terminal）；父进程对它们只 await，不裸 Task.cancel，
      避免在 DB 中遗留 ownership 不清的 running job（naked orphan）。
    - 完成后立即退出（不再 sleep），退出码 0（main 自然退出）
    - 日志: "SIGTERM drain complete, finished current item"
    """
    _hb_task = asyncio.create_task(heartbeat_loop("after_close_orchestrator"))
    logger.info(
        "[AfterCloseWorker] 启动（间隔=%ds）", worker_interval(),
    )

    # [P0-3 2026-07-31] 启动 Auction Scheduler co-process（同进程，共享 _shutdown）
    # 不新建容器；独立轮询 09:25/10:00 触发窗口和 queued auction jobs
    _auction_co_process_task = asyncio.create_task(run_auction_co_process())
    logger.info(
        "[AfterCloseWorker] Auction Scheduler co-process 已启动（生产入口，无需单独 WORKER_TYPE=auction_scheduler）",
    )

    # [CHIP-RETIRE 2026-09-01] 自动 chip consensus 已退役：本进程不再启动 Chip
    # co-process（原 _chip_co_process_task）。盘后主链不再创建 after_close_chip_consensus
    # job，故 after-close worker 无需 chip 执行器（executor isolation 问题随之消失）。
    # 历史 chip 数据 / 服务实现 / 独立 WORKER_TYPE=chip_consensus 调试入口均保留。

    # [REVIEW-BACKEND-FINAL-CLOSURE Phase 5] Review bootstrap co-process 已退休：
    # run_review_bootstrap_worker / _review_bootstrap_poll_once 物理删除，不再启动。

    # 启动恢复：清理上次崩溃残留的 running 任务 + 自动恢复 interrupted 任务
    try:
        async with session_factory() as db:
            recovered = await recover_stale_job_runs(db)
            # [CRASH-RESUME-SLICE / P0-C] 同 slot 新 incarnation 已启动 →
            # 立即中断上一代进程遗留的 running 任务（如本次 8/25 child 所属的旧进程），
            # 无需等待 4h lease 自然过期。
            replaced = await recover_replaced_runs(db, worker_instance_id)
            # [PRD §4.3 JOB-01] 自动将 interrupted 的盘后任务转为 resume_queued
            resumed = await auto_resume_runs(db)
            await db.commit()
            if recovered > 0 or resumed > 0 or replaced > 0:
                logger.info(
                    "[AfterCloseWorker] 启动恢复: %d 个过期任务, %d 个自动恢复, "
                    "%d 个 incarnation 替换恢复",
                    recovered, resumed, replaced,
                )
    except Exception as exc:
        logger.exception("[AfterCloseWorker] 启动恢复异常: %s", exc)

    try:
        while not should_shutdown():
            try:
                # [2026-08-11 AFTER-CLOSE-ENHANCEMENT-HEAD-OF-LINE-BLOCKING v2]
                # mandatory 主循环只领取/执行 after_close_orchestrator。
                # Chip / Review bootstrap co-process 均已退休（不在此串行 fallback），
                # 因此任何 long-running secondary job 都不占用 mandatory executor。
                await poll_once()
            except Exception as exc:
                # poll_once 内部已捕获执行异常，此处仅捕获领取阶段的意外异常
                logger.exception("[AfterCloseWorker] 轮询异常: %s", exc)
            if should_shutdown():
                # [SIGTERM drain] 当前 run 已完成（或无任务），不再领取新 item
                # checkpoint（run status + heartbeat）已由 execute_after_close_run 内部写入
                logger.info("[AfterCloseWorker] SIGTERM drain: 不再领取新任务，准备退出")
                break
            await asyncio.sleep(worker_interval())
    finally:
        # [SIGTERM drain] 逐个等待 co-process 自然退出（drain 到当前业务 item terminal）。
        # [CHIP-RETIRE 2026-09-01] chip / review bootstrap co-process 均已退役，此处仅剩 Auction。
        # 禁止裸 Task.cancel()：co-process 当前 item 必须到达 terminal，
        # 避免在 DB 中遗留 ownership 不清的 running job（naked orphan）。
        # 各 co-process 检查共享 _shutdown 后在当前 item 完成后退出循环。
        await _drain_co_process(_auction_co_process_task, "Auction", logger)

    # [SIGTERM drain complete] - 当前 item 已完成，worker 正常退出（退出码 0）
    logger.info("[AfterCloseWorker] SIGTERM drain complete, finished current item")
