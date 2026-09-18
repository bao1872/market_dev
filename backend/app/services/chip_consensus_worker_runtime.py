"""[ChipConsensusWorker] lifecycle runtime（W7A：从 worker.py 迁移，仅搬 lifecycle 不改契约）。

单一 public 入口 run_chip_consensus_worker_runtime：
- Chip 为独立 standalone debug Worker（WORKER_TYPE=chip_consensus），自带 chip_consensus heartbeat；
- 不是 co-process，无 co-process 变体；
- 不引入 GenericSchedulerRuntime / WorkerRuntimeBase / mode switch。

冻结合同（逐字保留 worker 原行为）：
- heartbeat 在 startup log / recovery 前创建，参数 "chip_consensus"，不 cancel / 不额外 await。
- startup log：logger.info("[ChipConsensusWorker] 启动（间隔=%ds）", worker_interval())。
- recovery：new session → recover_stale_job_runs(db) → commit（在 if 外）→ if recovered>0 info。
- recovery 异常非致命：except logger.exception → 仍进 worker loop（不 fail-fast）。
- loop entry：while not should_shutdown()。
- 每轮 poll exception 隔离：try poll except logger.exception → 仍做 shutdown check（不跳过）。
- poll 后 shutdown check：poll → if should_shutdown: drain log → break → sleep（shutdown 后不 sleep）。
- 正常 sleep 使用 worker_interval() 当前值（lambda 每轮重读 WORKER_INTERVAL，支持动态 interval）。
- initial shutdown=True → poll=0，不 sleep。
- 正常 sleep exact worker_interval()，无 jitter/backoff。
- final log：SIGTERM drain complete, finished current item（不 naked cancel 当前 Chip item）。
"""
from __future__ import annotations

import asyncio


async def run_chip_consensus_worker_runtime(
    *,
    session_factory,
    heartbeat_loop,
    recover_stale_job_runs,
    poll_once,
    should_shutdown,
    worker_interval,
    logger,
) -> None:
    """[P0-3] Chip consensus standalone Worker runtime（独立 heartbeat，依附 WORKER_TYPE=chip_consensus）。"""
    # heartbeat 必须在 startup log / recovery 之前创建（冻结合同）
    # local strong reference，不 cancel / 不额外 await（保持原 worker 生命周期语义）
    _hb_task = asyncio.create_task(heartbeat_loop("chip_consensus"))

    logger.info(
        "[ChipConsensusWorker] 启动（间隔=%ds）", worker_interval(),
    )

    # 启动恢复：清理上次崩溃残留的 running 任务（由 watchdog 转为 interrupted → resume_queued）
    try:
        async with session_factory() as db:
            recovered = await recover_stale_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info(
                    "[ChipConsensusWorker] 启动恢复: %d 个过期任务", recovered,
                )
    except Exception as exc:
        logger.exception("[ChipConsensusWorker] 启动恢复异常: %s", exc)

    while not should_shutdown():
        try:
            await poll_once()
        except Exception as exc:
            # _chip_consensus_poll_once 内部已捕获执行异常，此处仅捕获领取阶段的意外异常
            logger.exception("[ChipConsensusWorker] 轮询异常: %s", exc)
        if should_shutdown():
            logger.info("[ChipConsensusWorker] SIGTERM drain: 不再领取新任务，准备退出")
            break
        await asyncio.sleep(worker_interval())

    logger.info("[ChipConsensusWorker] SIGTERM drain complete, finished current item")
