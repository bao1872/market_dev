"""Strategy batch worker lifecycle, separate from the process composition root."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

SessionFactory = Callable[[], Any]
HeartbeatLoop = Callable[[str], Coroutine[Any, Any, None]]
ShutdownProbe = Callable[[], bool]


async def run_strategy_batch_loop(
    *,
    session_factory: SessionFactory,
    heartbeat_loop: HeartbeatLoop,
    should_shutdown: ShutdownProbe,
    interval: int,
    logger: logging.Logger,
) -> None:
    """Recover stale runs, then claim and execute queued strategy runs serially."""
    from app.services.strategy_batch_service import StrategyBatchService

    asyncio.create_task(heartbeat_loop("strategy_batch"))
    logger.info("Strategy Batch Worker 启动（间隔=%ds）", interval)
    service = StrategyBatchService()

    try:
        async with session_factory() as recovery_db:
            recovered = await service.recover_stale_runs(recovery_db)
            await recovery_db.commit()
            if recovered > 0:
                logger.info("Strategy Batch Worker 启动恢复: %d 个过期任务", recovered)
    except Exception as exc:
        logger.exception("Strategy Batch Worker 启动恢复异常: %s", exc)

    while not should_shutdown():
        db: Any | None = None
        try:
            async with session_factory() as db:
                run = await service.claim_next_run(db)
                if run is None:
                    await asyncio.sleep(interval)
                    continue

                # Claim visibility precedes execution, matching the existing
                # lease/ownership contract.
                await db.commit()
                logger.info(
                    "开始执行策略批量计算: run_id=%s, trade_date=%s",
                    run.id,
                    run.trade_date,
                )
                await service.execute_run(db, run.id)
                await db.commit()
                logger.info(
                    "策略批量计算完成: run_id=%s, status=%s",
                    run.id,
                    run.status,
                )
        except Exception as exc:
            logger.exception("Strategy Batch Worker 异常: %s", exc)
            try:
                if db is not None:
                    await db.rollback()
            except Exception:
                pass
        await asyncio.sleep(interval)


__all__ = ["run_strategy_batch_loop"]
