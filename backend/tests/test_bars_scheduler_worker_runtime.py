"""Tests for the Bars Scheduler runtime extraction (PANJI-GOV-W2).

Two layers:

* Structural contract tests — confirm the façade ``run_bars_scheduler_worker``
  only delegates, the public name is unchanged, the runtime still registers both
  job ids with the correct cron / timezone / ``max_instances`` and injects the
  canonical ``SchedulerJobRun`` helpers while importing the business owners
  in-closure (no second business owner, no generic scheduler abstraction).
* Behavioral tests — drive the real runtime with a fake APScheduler and fake
  collaborators so no database / external service is touched, and assert the two
  short-circuit paths (non-trading-day, duplicate job_run) actually gate the
  downstream work.
"""

import asyncio
import inspect
import logging

from apscheduler.triggers.cron import CronTrigger

from app.services import bars_scheduler_worker_runtime as rt


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.started = False
        self.shutdown_called = False
        self.shutdown_wait = None

    def add_job(self, func, trigger, **kwargs) -> None:  # noqa: ANN001
        self.jobs.append({"func": func, "trigger": trigger, "kwargs": kwargs})

    def start(self) -> None:
        self.started = True

    def shutdown(self, wait: bool = False) -> None:  # noqa: FBT001, FBT002
        self.shutdown_called = True
        self.shutdown_wait = wait


class FakeSession:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


class FakeSessionCM:
    def __init__(self) -> None:
        self.session = FakeSession()

    async def __aenter__(self) -> FakeSession:
        return self.session

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeJobRun:
    id = "fake-job-run"
    status = "running"


def _job(fake: FakeScheduler, job_id: str) -> dict:
    for j in fake.jobs:
        if j["kwargs"].get("id") == job_id:
            return j
    raise AssertionError(f"job id not registered: {job_id}")


def _assert_cron(
    trigger: CronTrigger,
    *,
    hour: int,
    minute: int,
    day_of_week: str = "mon-sun",
    tz: str = "Asia/Shanghai",
) -> None:
    assert isinstance(trigger, CronTrigger)
    r = repr(trigger)
    assert f"hour='{hour}'" in r
    assert f"minute='{minute}'" in r
    assert f"day_of_week='{day_of_week}'" in r
    assert f"timezone='{tz}'" in r


# --------------------------------------------------------------------------- #
# Runtime driver
# --------------------------------------------------------------------------- #
async def _run_runtime(
    monkeypatch,  # noqa: ANN001
    *,
    is_trading: bool = True,
    share_create_returns_none: bool = True,
    calls: dict | None = None,
):
    calls = calls if calls is not None else {}
    fake = FakeScheduler()
    monkeypatch.setattr(rt, "AsyncIOScheduler", lambda: fake)

    async def _fake_is_trading(session, trade_date):  # noqa: ANN001
        return is_trading

    monkeypatch.setattr(
        "app.services.calendar_service.is_trading_day_async", _fake_is_trading
    )

    async def _fake_create_after_close(db=None, trade_date=None):  # noqa: ANN001
        calls.setdefault("create_after_close", []).append(trade_date)
        return _FakeJobRun(), True

    monkeypatch.setattr(
        "app.services.after_close_orchestrator.create_after_close_run",
        _fake_create_after_close,
    )

    async def _fake_sync(db):  # noqa: ANN001
        calls["sync_share_capitals"] = calls.get("sync_share_capitals", 0) + 1
        return {"total": 0, "succeeded": 0, "failed": 0, "skipped_bj": 0}

    monkeypatch.setattr(
        "app.services.instrument_share_sync_service.sync_share_capitals", _fake_sync
    )

    async def _fake_create_job_run(db, name, biz, *, scheduled_at, run_key):  # noqa: ANN001
        calls.setdefault("create_job_run", []).append((name, biz, run_key))
        return None if share_create_returns_none else _FakeJobRun()

    async def _fake_finish_job_run(db, job_run, status, **kw):  # noqa: ANN001
        calls.setdefault("finish_job_run", []).append((status, kw))

    async def _fake_recover(db):  # noqa: ANN001
        return 0

    async def _fake_heartbeat(name):  # noqa: ANN001
        return None

    await rt.run_bars_scheduler_worker_runtime(
        session_factory=lambda: FakeSessionCM(),
        heartbeat_loop=_fake_heartbeat,
        should_shutdown=lambda: True,
        create_job_run=_fake_create_job_run,
        finish_job_run=_fake_finish_job_run,
        recover_stale_job_runs=_fake_recover,
        logger=logging.getLogger("test-bars-rt"),
    )
    return fake, calls


# --------------------------------------------------------------------------- #
# Structural contract tests
# --------------------------------------------------------------------------- #
def test_facade_delegates_only() -> None:
    from app.worker import run_bars_scheduler_worker

    src = inspect.getsource(run_bars_scheduler_worker)
    # façade must delegate to the runtime
    assert "run_bars_scheduler_worker_runtime(" in src
    assert "await run_bars_scheduler_worker_runtime(" in src
    # façade must no longer instantiate / define the lifecycle
    # (the docstring may still mention the words; only the code must be gone)
    assert "AsyncIOScheduler()" not in src
    assert "CronTrigger(" not in src
    assert "async def scheduled_bars_refresh" not in src
    assert "async def scheduled_share_capital_sync" not in src
    # façade injects the canonical SchedulerJobRun helpers from worker.py
    assert "create_job_run=_create_job_run" in src
    assert "finish_job_run=_finish_job_run" in src
    assert "recover_stale_job_runs=recover_stale_scheduler_job_runs" in src


def test_facade_public_name_preserved() -> None:
    import app.worker as worker

    assert hasattr(worker, "run_bars_scheduler_worker")
    assert callable(worker.run_bars_scheduler_worker)


def test_runtime_source_contract() -> None:
    src = inspect.getsource(rt.run_bars_scheduler_worker_runtime)
    # both job ids present
    assert '"bars_refresh_daily"' in src
    assert '"share_capital_sync_daily"' in src
    # 15:05 and 18:00 Asia/Shanghai
    assert "hour=15" in src and "minute=5" in src
    assert "hour=18" in src and "minute=0" in src
    assert '"Asia/Shanghai"' in src
    # single-concurrency contract preserved
    assert "max_instances=1" in src
    # runtime does NOT re-implement the canonical SchedulerJobRun helpers
    assert "def create_job_run" not in src
    assert "def finish_job_run" not in src
    assert "def recover_stale_job_runs" not in src
    # runtime consumes the injected helpers
    assert "create_job_run(" in src
    assert "finish_job_run(" in src
    assert "recover_stale_job_runs(" in src
    # business owners are imported in-closure (lazy, original positions)
    assert (
        "from app.services.after_close_orchestrator import create_after_close_run" in src
    )
    assert "from app.services.calendar_service import is_trading_day_async" in src
    assert (
        "from app.services.instrument_share_sync_service import sync_share_capitals"
        in src
    )
    # no generic scheduler abstraction / second business owner
    assert "SchedulerManager" not in src
    assert "GenericScheduler" not in src


# --------------------------------------------------------------------------- #
# Behavioral tests (fake scheduler, no DB / external service)
# --------------------------------------------------------------------------- #
def test_runtime_registers_two_jobs_with_triggers(monkeypatch) -> None:  # noqa: ANN001
    fake, _ = asyncio.run(_run_runtime(monkeypatch))
    ids = [j["kwargs"].get("id") for j in fake.jobs]
    assert "bars_refresh_daily" in ids
    assert "share_capital_sync_daily" in ids

    bars = _job(fake, "bars_refresh_daily")
    share = _job(fake, "share_capital_sync_daily")
    _assert_cron(bars["trigger"], hour=15, minute=5)
    _assert_cron(share["trigger"], hour=18, minute=0)
    assert share["kwargs"].get("max_instances") == 1

    # startup + graceful shutdown behavior preserved
    assert fake.started is True
    assert fake.shutdown_called is True
    assert fake.shutdown_wait is False


def test_bars_refresh_skips_on_non_trading_day(monkeypatch) -> None:  # noqa: ANN001
    calls: dict = {}
    fake, calls = asyncio.run(
        _run_runtime(monkeypatch, is_trading=False, calls=calls)
    )
    bars = _job(fake, "bars_refresh_daily")
    asyncio.run(bars["func"]())
    # non-trading day must short-circuit before creating the after-close run
    assert "create_after_close" not in calls


def test_share_capital_skips_sync_on_duplicate(monkeypatch) -> None:  # noqa: ANN001
    calls: dict = {}
    fake, calls = asyncio.run(
        _run_runtime(monkeypatch, share_create_returns_none=True, calls=calls)
    )
    share = _job(fake, "share_capital_sync_daily")
    asyncio.run(share["func"]())
    # duplicate job_run must short-circuit before syncing share capitals
    assert calls.get("sync_share_capitals", 0) == 0
    # but the idempotency check itself ran
    assert len(calls.get("create_job_run", [])) == 1


def test_share_capital_runs_sync_on_non_duplicate(monkeypatch) -> None:  # noqa: ANN001
    calls: dict = {}
    fake, calls = asyncio.run(
        _run_runtime(monkeypatch, share_create_returns_none=False, calls=calls)
    )
    share = _job(fake, "share_capital_sync_daily")
    asyncio.run(share["func"]())
    # non-duplicate proceeds to sync and finishes succeeded
    assert calls.get("sync_share_capitals", 0) == 1
    assert any(status == "succeeded" for status, _ in calls.get("finish_job_run", []))
