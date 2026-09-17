"""Pure lifecycle contracts for the extracted strategy batch worker loop."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.services.strategy_batch_worker_runtime import run_strategy_batch_loop

pytestmark = pytest.mark.pure_unit


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


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
async def test_strategy_batch_recovery_claim_and_execution_commit_order() -> None:
    sessions = _SessionFactory()
    heartbeat_names: list[str] = []
    run = SimpleNamespace(id="run-1", trade_date="2026-09-17", status="running")
    service = SimpleNamespace(
        recover_stale_runs=AsyncMock(return_value=2),
        claim_next_run=AsyncMock(return_value=run),
        execute_run=AsyncMock(),
    )

    with (
        patch("app.services.strategy_batch_service.StrategyBatchService", return_value=service),
        patch("app.services.strategy_batch_worker_runtime.asyncio.sleep", AsyncMock()),
    ):
        await run_strategy_batch_loop(
            session_factory=sessions,
            heartbeat_loop=_recording_heartbeat(heartbeat_names),
            should_shutdown=_one_iteration_shutdown_probe(),
            interval=9,
            logger=logging.getLogger("test.strategy_batch"),
        )

    assert heartbeat_names == ["strategy_batch"]
    assert len(sessions.sessions) == 2
    assert sessions.sessions[0].commits == 1
    assert sessions.sessions[1].commits == 2
    service.recover_stale_runs.assert_awaited_once_with(sessions.sessions[0])
    service.claim_next_run.assert_awaited_once_with(sessions.sessions[1])
    service.execute_run.assert_awaited_once_with(sessions.sessions[1], "run-1")


@pytest.mark.asyncio
async def test_strategy_batch_execution_failure_rolls_back_and_keeps_loop_alive() -> None:
    sessions = _SessionFactory()
    run = SimpleNamespace(id="run-2", trade_date="2026-09-17", status="running")
    service = SimpleNamespace(
        recover_stale_runs=AsyncMock(return_value=0),
        claim_next_run=AsyncMock(return_value=run),
        execute_run=AsyncMock(side_effect=RuntimeError("boom")),
    )
    sleep = AsyncMock()

    with (
        patch("app.services.strategy_batch_service.StrategyBatchService", return_value=service),
        patch("app.services.strategy_batch_worker_runtime.asyncio.sleep", sleep),
    ):
        await run_strategy_batch_loop(
            session_factory=sessions,
            heartbeat_loop=_recording_heartbeat([]),
            should_shutdown=_one_iteration_shutdown_probe(),
            interval=4,
            logger=Mock(spec=logging.Logger),
        )

    assert len(sessions.sessions) == 2
    assert sessions.sessions[1].commits == 1
    assert sessions.sessions[1].rollbacks == 1
    sleep.assert_awaited_once_with(4)


@pytest.mark.asyncio
async def test_worker_strategy_batch_facade_supplies_live_configuration(monkeypatch) -> None:
    import app.worker as worker

    runtime_loop = AsyncMock()
    monkeypatch.setattr(worker, "run_strategy_batch_loop", runtime_loop)
    monkeypatch.setattr(worker, "WORKER_INTERVAL", 13)
    monkeypatch.setattr(worker, "_shutdown", False)

    await worker.run_strategy_batch_worker()

    kwargs = runtime_loop.await_args.kwargs
    assert kwargs["session_factory"] is worker.AsyncSessionLocal
    assert kwargs["heartbeat_loop"] is worker._heartbeat_loop
    assert kwargs["interval"] == 13
    assert kwargs["should_shutdown"]() is False
    worker._shutdown = True
    assert kwargs["should_shutdown"]() is True
