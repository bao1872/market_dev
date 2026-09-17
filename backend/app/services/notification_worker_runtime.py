"""Notification worker loops used by the unified worker composition root.

This module owns only the Outbox Relay and Delivery polling lifecycles.  Business
state machines remain in ``outbox_relay`` and ``delivery_worker``; the top-level
``app.worker`` module supplies process configuration, session construction,
heartbeat ownership and the shared shutdown signal.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

SessionFactory = Callable[[], Any]
HeartbeatLoop = Callable[[str], Coroutine[Any, Any, None]]
ShutdownProbe = Callable[[], bool]


async def run_outbox_relay_loop(
    *,
    session_factory: SessionFactory,
    heartbeat_loop: HeartbeatLoop,
    should_shutdown: ShutdownProbe,
    interval: int,
    batch_size: int,
    max_retry: int,
    logger: logging.Logger,
) -> None:
    """Poll pending Outbox rows and expand them into channel deliveries."""
    from app.services.outbox_relay import relay_outbox

    asyncio.create_task(heartbeat_loop("outbox"))
    logger.info("Outbox Relay worker 启动（间隔=%ds, 批次=%d）", interval, batch_size)
    while not should_shutdown():
        try:
            async with session_factory() as db:
                processed = await relay_outbox(
                    db=db,
                    batch_size=batch_size,
                    max_retry=max_retry,
                )
                await db.commit()
                if processed > 0:
                    logger.info("Outbox Relay 处理 %d 条", processed)
        except Exception as exc:
            logger.exception("Outbox Relay 异常: %s", exc)
        await asyncio.sleep(interval)


async def run_delivery_loop(
    *,
    session_factory: SessionFactory,
    heartbeat_loop: HeartbeatLoop,
    should_shutdown: ShutdownProbe,
    interval: int,
    batch_size: int,
    max_retry: int,
    logger: logging.Logger,
) -> None:
    """Poll pending/retrying deliveries and execute the delivery state machine."""
    from app.services.delivery_worker import process_pending_deliveries

    asyncio.create_task(heartbeat_loop("delivery"))
    logger.info("Delivery Worker 启动（间隔=%ds, 批次=%d）", interval, batch_size)
    while not should_shutdown():
        try:
            async with session_factory() as db:
                processed = await process_pending_deliveries(
                    db=db,
                    batch_size=batch_size,
                    max_retry=max_retry,
                )
                await db.commit()
                if processed > 0:
                    logger.info("Delivery Worker 处理 %d 条", processed)
        except Exception as exc:
            logger.exception("Delivery Worker 异常: %s", exc)
        await asyncio.sleep(interval)


__all__ = ["run_delivery_loop", "run_outbox_relay_loop"]
