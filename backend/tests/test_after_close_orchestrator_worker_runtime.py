"""Tests for the AfterClose orchestrator main runtime extraction (PANJI-GOV-W5A).

These lock the seven W5A audit questions without a real Postgres / Auction / Feishu:

* ``run_after_close_orchestrator_worker`` is now a thin façade in ``app.worker``
  (only delegates); the real process lifecycle lives in
  ``after_close_orchestrator_worker_runtime``.
* ``_after_close_poll_once`` and ``_run_auction_scheduler_co_process`` are NOT moved
  (still owned by worker, injected into the runtime).
* startup recovery order is frozen: stale -> replaced-incarnation -> auto-resume ->
  single commit.
* a recovery exception is swallowed and the worker still polls.
* after SIGTERM (shutdown) the worker stops without an extra ``asyncio.sleep``.
* the Auction co-process is drained (awaited), never ``Task.cancel``-ed.
* the final exit log fires only after drain completes.

The runtime injects every collaborator (session_factory / heartbeat_loop /
should_shutdown / worker_interval / worker_instance_id / recover_* / poll_once /
run_auction_co_process / logger), so the tests drive it directly with fakes.
"""

import asyncio
import inspect
import logging

import pytest

import app.services.after_close_orchestrator_worker_runtime as rt
from app.services.after_close_orchestrator_worker_runtime import (
    run_after_close_orchestrator_worker_runtime,
)


class _FakeSession:
    def __init__(self, on_commit=None):  # noqa: ANN001
        self._on_commit = on_commit

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        if self._on_commit:
            self._on_commit()


async def _noop_hb(_name: str) -> None:
    return None


async def _quick_auction() -> None:
    return None


async def _noop_poll() -> bool:
    return False


# --------------------------------------------------------------------------- #
# Structural contracts
# --------------------------------------------------------------------------- #
def test_facade_delegates_only() -> None:
    import app.worker as worker

    src = inspect.getsource(worker.run_after_close_orchestrator_worker)
    # thin façade: only injects worker-owned deps + delegates
    assert "run_after_close_orchestrator_worker_runtime(" in src
    assert "session_factory=AsyncSessionLocal" in src
    assert "should_shutdown=lambda: _shutdown" in src
    assert "worker_interval=lambda: WORKER_INTERVAL" in src
    # lifecycle must NOT live in worker anymore
    assert "while not _shutdown" not in src
    assert "create_task(_run_auction_scheduler_co_process" not in src
    assert "recover_stale_scheduler_job_runs(db)" not in src
    assert "recover_replaced_incarnation_runs(db" not in src
    assert "auto_resume_interrupted_after_close_runs(db)" not in src
    assert "_drain_co_process(" not in src
    assert "async def _drain_co_process" not in src
    # the real lifecycle + drain helper now live in the runtime module
    assert hasattr(rt, "run_after_close_orchestrator_worker_runtime")
    assert hasattr(rt, "_drain_co_process")


def test_runtime_source_contract() -> None:
    src = inspect.getsource(run_after_close_orchestrator_worker_runtime)
    # every collaborator is an injected parameter (no worker globals captured)
    for name in (
        "session_factory",
        "heartbeat_loop",
        "should_shutdown",
        "worker_interval",
        "worker_instance_id",
        "recover_stale_job_runs",
        "recover_replaced_runs",
        "auto_resume_runs",
        "poll_once",
        "run_auction_co_process",
        "logger",
    ):
        assert name in src
    # frozen startup recovery order + single commit
    assert "recovered = await recover_stale_job_runs(db)" in src
    assert "replaced = await recover_replaced_runs(db, worker_instance_id)" in src
    assert "resumed = await auto_resume_runs(db)" in src
    assert "await db.commit()" in src
    # Auction co-process started exactly once, never cancelled
    assert "asyncio.create_task(run_auction_co_process())" in src
    # no naked cancel: real code never calls .cancel() on the auction task
    assert "_auction_co_process_task.cancel" not in src
    assert "task.cancel" not in src
    # main loop + no extra sleep after shutdown
    assert "while not should_shutdown()" in src
    assert "await asyncio.sleep(worker_interval())" in src
    # drain lives in finally and uses the runtime's own _drain_co_process
    assert "finally:" in src
    assert '_drain_co_process(_auction_co_process_task, "Auction", logger)' in src
    # final exit log fires after drain
    assert (
        'logger.info("[AfterCloseWorker] SIGTERM drain complete, finished current item")'
        in src
    )


def test_injected_functions_not_moved() -> None:
    # ownership check: the dangerous claim/fencing logic stays in worker
    import app.worker as worker

    assert hasattr(worker, "_after_close_poll_once")
    assert hasattr(worker, "_run_auction_scheduler_co_process")


# --------------------------------------------------------------------------- #
# Behavioral tests
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_startup_recovery_order() -> None:
    order: list[str] = []

    async def _stale(db):  # noqa: ANN001
        order.append("stale")
        return 1

    async def _replaced(db, wid):  # noqa: ANN001
        order.append("replaced")
        return 2

    async def _resume(db):  # noqa: ANN001
        order.append("resume")
        return 3

    probe = {"shutdown": True}  # skip the main loop; only verify recovery

    await run_after_close_orchestrator_worker_runtime(
        session_factory=lambda: _FakeSession(on_commit=lambda: order.append("commit")),
        heartbeat_loop=_noop_hb,
        should_shutdown=lambda: probe["shutdown"],
        worker_interval=lambda: 0,
        worker_instance_id="host:pid:nonce",
        recover_stale_job_runs=_stale,
        recover_replaced_runs=_replaced,
        auto_resume_runs=_resume,
        poll_once=_noop_poll,  # never reached
        run_auction_co_process=_quick_auction,
        logger=logging.getLogger("test-rt"),
    )

    # stale -> replaced -> resume -> single commit
    assert order == ["stale", "replaced", "resume", "commit"]


@pytest.mark.asyncio
async def test_recovery_exception_keeps_polling() -> None:
    calls = {"poll": 0, "recover_raised": False}
    probe = {"shutdown": False}

    async def _boom(db):  # noqa: ANN001
        calls["recover_raised"] = True
        raise RuntimeError("recovery boom")

    async def _poll():
        calls["poll"] += 1
        probe["shutdown"] = True  # simulate SIGTERM right after first poll
        return False

    await run_after_close_orchestrator_worker_runtime(
        session_factory=_FakeSession,
        heartbeat_loop=_noop_hb,
        should_shutdown=lambda: probe["shutdown"],
        worker_interval=lambda: 0,
        worker_instance_id="host:pid:nonce",
        recover_stale_job_runs=_boom,
        recover_replaced_runs=_zero_wid,
        auto_resume_runs=_zero,
        poll_once=_poll,
        run_auction_co_process=_quick_auction,
        logger=logging.getLogger("test-rt"),
    )

    # recovery exception swallowed -> worker still polled (did not crash)
    assert calls["recover_raised"] is True
    assert calls["poll"] >= 1


@pytest.mark.asyncio
async def test_shutdown_no_extra_sleep(monkeypatch) -> None:  # noqa: ANN001
    calls = {"poll": 0, "sleep": 0}
    probe = {"shutdown": False}

    async def _poll():
        calls["poll"] += 1
        probe["shutdown"] = True  # SIGTERM during first poll
        return False

    class _FakeAsyncio:
        @staticmethod
        def create_task(coro):
            return asyncio.create_task(coro)

        @staticmethod
        async def sleep(interval):  # noqa: ANN001
            calls["sleep"] += 1
            await asyncio.sleep(0)

    monkeypatch.setattr(rt, "asyncio", _FakeAsyncio)

    await run_after_close_orchestrator_worker_runtime(
        session_factory=_FakeSession,
        heartbeat_loop=_noop_hb,
        should_shutdown=lambda: probe["shutdown"],
        worker_interval=lambda: 1,
        worker_instance_id="host:pid:nonce",
        recover_stale_job_runs=_zero,
        recover_replaced_runs=_zero_wid,
        auto_resume_runs=_zero,
        poll_once=_poll,
        run_auction_co_process=_quick_auction,
        logger=logging.getLogger("test-rt"),
    )

    # shutdown detected after the poll -> break before sleep (no extra interval wait)
    assert calls["poll"] == 1
    assert calls["sleep"] == 0


@pytest.mark.asyncio
async def test_auction_drain_no_cancel() -> None:
    probe = {"shutdown": False}
    started = asyncio.Event()
    release = asyncio.Event()
    finished = {"v": False}
    cancelled = {"v": False}

    async def _auction():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled["v"] = True
            raise
        finished["v"] = True

    async def _poll():
        probe["shutdown"] = True  # SIGTERM after first poll
        return False

    task = asyncio.create_task(
        run_after_close_orchestrator_worker_runtime(
            session_factory=_FakeSession,
            heartbeat_loop=_noop_hb,
            should_shutdown=lambda: probe["shutdown"],
            worker_interval=lambda: 0,
            worker_instance_id="host:pid:nonce",
            recover_stale_job_runs=_zero,
            recover_replaced_runs=_zero_wid,
            auto_resume_runs=_zero,
            poll_once=_poll,
            run_auction_co_process=_auction,
            logger=logging.getLogger("test-rt"),
        )
    )

    await started.wait()  # Auction co-process has started
    await asyncio.sleep(0.02)  # let the runtime reach the finally drain
    # Auction must NOT be cancelled and must still be pending
    assert cancelled["v"] is False
    assert finished["v"] is False

    release.set()  # allow Auction to finish naturally
    await asyncio.wait_for(task, timeout=5)
    assert finished["v"] is True
    assert cancelled["v"] is False
    assert task.done()


async def _zero(db):  # noqa: ANN001
    return 0


async def _zero_wid(db, wid):  # noqa: ANN001
    return 0
