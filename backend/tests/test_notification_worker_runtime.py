"""Pure contract tests for notification worker lifecycle extraction."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from app.services.notification_worker_runtime import (
    run_delivery_loop,
    run_outbox_relay_loop,
)

pytestmark = pytest.mark.pure_unit


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class _SessionFactory:
    def __init__(self) -> None:
        self.sessions: list[_Session] = []

    def __call__(self) -> _Session:
        session = _Session()
        self.sessions.append(session)
        return session


def _one_iteration_shutdown_probe():
    calls = 0

    def _probe() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    return _probe


def _recording_heartbeat(names: list[str]):
    def _heartbeat(worker_name: str):
        names.append(worker_name)

        async def _noop() -> None:
            return None

        return _noop()

    return _heartbeat


@pytest.mark.asyncio
async def test_outbox_loop_preserves_batch_retry_commit_and_heartbeat() -> None:
    sessions = _SessionFactory()
    heartbeat_names: list[str] = []
    heartbeat = _recording_heartbeat(heartbeat_names)
    relay = AsyncMock(return_value=3)

    with (
        patch("app.services.outbox_relay.relay_outbox", relay),
        patch("app.services.notification_worker_runtime.asyncio.sleep", AsyncMock()),
    ):
        await run_outbox_relay_loop(
            session_factory=sessions,
            heartbeat_loop=heartbeat,
            should_shutdown=_one_iteration_shutdown_probe(),
            interval=7,
            batch_size=23,
            max_retry=4,
            logger=logging.getLogger("test.outbox"),
        )

    assert len(sessions.sessions) == 1
    assert sessions.sessions[0].commits == 1
    relay.assert_awaited_once_with(
        db=sessions.sessions[0],
        batch_size=23,
        max_retry=4,
    )
    assert heartbeat_names == ["outbox"]


@pytest.mark.asyncio
async def test_delivery_loop_preserves_batch_retry_commit_and_heartbeat() -> None:
    sessions = _SessionFactory()
    heartbeat_names: list[str] = []
    heartbeat = _recording_heartbeat(heartbeat_names)
    deliver = AsyncMock(return_value=2)

    with (
        patch("app.services.delivery_worker.process_pending_deliveries", deliver),
        patch("app.services.notification_worker_runtime.asyncio.sleep", AsyncMock()),
    ):
        await run_delivery_loop(
            session_factory=sessions,
            heartbeat_loop=heartbeat,
            should_shutdown=_one_iteration_shutdown_probe(),
            interval=5,
            batch_size=17,
            max_retry=6,
            logger=logging.getLogger("test.delivery"),
        )

    assert len(sessions.sessions) == 1
    assert sessions.sessions[0].commits == 1
    deliver.assert_awaited_once_with(
        db=sessions.sessions[0],
        batch_size=17,
        max_retry=6,
    )
    assert heartbeat_names == ["delivery"]


@pytest.mark.asyncio
async def test_worker_facades_supply_live_process_configuration(monkeypatch) -> None:
    import app.worker as worker

    outbox_loop = AsyncMock()
    delivery_loop = AsyncMock()
    monkeypatch.setattr(worker, "run_outbox_relay_loop", outbox_loop)
    monkeypatch.setattr(worker, "run_delivery_loop", delivery_loop)
    monkeypatch.setattr(worker, "WORKER_INTERVAL", 11)
    monkeypatch.setattr(worker, "WORKER_BATCH_SIZE", 29)
    monkeypatch.setattr(worker, "WORKER_MAX_RETRY", 8)
    monkeypatch.setattr(worker, "_shutdown", False)

    await worker.run_outbox_relay()
    await worker.run_delivery_worker()

    outbox_kwargs = outbox_loop.await_args.kwargs
    delivery_kwargs = delivery_loop.await_args.kwargs
    for kwargs in (outbox_kwargs, delivery_kwargs):
        assert kwargs["session_factory"] is worker.AsyncSessionLocal
        assert kwargs["heartbeat_loop"] is worker._heartbeat_loop
        assert kwargs["interval"] == 11
        assert kwargs["batch_size"] == 29
        assert kwargs["max_retry"] == 8
        assert kwargs["should_shutdown"]() is False

    worker._shutdown = True
    assert outbox_kwargs["should_shutdown"]() is True
    assert delivery_kwargs["should_shutdown"]() is True
