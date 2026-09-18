"""Tests for the Strategy Scheduler runtime extraction (PANJI-GOV-W3).

Layers mirror the Bars / Calendar pattern:

* Structural contract tests — façade only delegates, public name unchanged, the
  runtime keeps the 18:30 cron / ``strategy_run_daily`` / Asia/Shanghai, the
  verbatim selector SQL, the DSA-vs-non-DSA branches, the ValueError/Exception
  split, the heartbeat cadence, the final-status mapping, and injects the
  canonical ``SchedulerJobRun`` helpers + heartbeat updater (no second owner, no
  ``StrategySchedulerService`` / ``GenericScheduler``).
* Behavioral tests (fake scheduler + fake collaborators, no DB / external
  service) — non-trading-day short-circuit, duplicate short-circuit, and the
  final-status mapping (succeeded / partial_failed / failed).
* Heartbeat regression — 5 selectors must trigger ``update_job_heartbeat`` once.
"""

import asyncio
import inspect
import logging

from apscheduler.triggers.cron import CronTrigger

from app.services import strategy_scheduler_worker_runtime as rt


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


class _FakeResult:
    def __init__(self, rows) -> None:  # noqa: ANN001
        self._rows = rows

    def fetchall(self) -> list:
        return self._rows


class _FakeSession:
    def __init__(self, rec: dict) -> None:  # noqa: ANN001
        self._rec = rec

    async def commit(self) -> None:
        self._rec.setdefault("commits", 0)
        self._rec["commits"] += 1

    async def rollback(self) -> None:
        self._rec.setdefault("rollbacks", 0)
        self._rec["rollbacks"] += 1

    async def execute(self, stmt) -> _FakeResult:  # noqa: ANN001
        self._rec["execute_calls"] = self._rec.get("execute_calls", 0) + 1
        return _FakeResult(self._rec["selector_rows"])


class _FakeSessionCM:
    def __init__(self, rec: dict) -> None:  # noqa: ANN001
        self._rec = rec

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession(self._rec)

    async def __aexit__(self, *exc) -> bool:
        return False


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
    create_job_run_returns_none: bool = False,
    selector_keys=None,  # noqa: ANN001
    dsa_selector: str = "DSA_TEST",
    fail_keys=None,  # noqa: ANN001
    rec: dict | None = None,
):
    rec = rec if rec is not None else {}
    rec.setdefault("create_job_run_calls", [])
    rec.setdefault("create_after_close_calls", [])
    rec.setdefault("create_batch_calls", [])
    rec.setdefault("finish_job_run_calls", [])
    rec.setdefault("update_heartbeat_calls", 0)
    rec.setdefault("execute_calls", 0)
    rec.setdefault("selector_rows", [(k,) for k in (selector_keys or [])])

    fake = FakeScheduler()
    monkeypatch.setattr(rt, "AsyncIOScheduler", lambda: fake)

    async def _fake_is_trading(session, trade_date):  # noqa: ANN001
        return is_trading

    monkeypatch.setattr(
        "app.services.calendar_service.is_trading_day_async", _fake_is_trading
    )

    class _JR:
        id = "fake-jr"
        status = "running"
        metadata_json = None

    async def _fake_create_job_run(db, name, biz, *, scheduled_at, run_key):  # noqa: ANN001
        rec["create_job_run_calls"].append((name, biz, run_key))
        return None if create_job_run_returns_none else _JR()

    async def _fake_finish_job_run(db, job_run, status, **kw):  # noqa: ANN001
        rec["finish_job_run_calls"].append((status, kw))

    class _AfterCloseRun:
        id = "after-close-1"

    async def _fake_create_after_close(db=None, trade_date=None):  # noqa: ANN001
        rec["create_after_close_calls"].append(trade_date)
        return _AfterCloseRun(), True

    monkeypatch.setattr(
        "app.services.after_close_orchestrator.create_after_close_run",
        _fake_create_after_close,
    )

    class _BatchRun:
        def __init__(self, k: str) -> None:
            self.id = f"batch-{k}"

    class _FakeBatchService:
        def __init__(self) -> None:
            pass

        async def create_batch_run(self, db, strategy_key, trade_date, run_type):  # noqa: ANN001
            rec["create_batch_calls"].append((strategy_key, trade_date))
            if fail_keys and strategy_key in fail_keys:
                raise ValueError(f"simulated failure for {strategy_key}")
            return _BatchRun(str(strategy_key))

    monkeypatch.setattr(rt, "StrategyBatchService", _FakeBatchService)

    monkeypatch.setattr(
        "app.constants.strategy_keys.DSA_SELECTOR", dsa_selector
    )

    async def _fake_recover(db):  # noqa: ANN001
        return 0

    async def _fake_heartbeat(name):  # noqa: ANN001
        return None

    async def _fake_update_heartbeat(db, job_run, lease_seconds=120):  # noqa: ANN001
        rec["update_heartbeat_calls"] += 1
        return None

    await rt.run_strategy_scheduler_worker_runtime(
        session_factory=lambda: _FakeSessionCM(rec),
        heartbeat_loop=_fake_heartbeat,
        should_shutdown=lambda: True,
        create_job_run=_fake_create_job_run,
        finish_job_run=_fake_finish_job_run,
        update_job_heartbeat=_fake_update_heartbeat,
        recover_stale_job_runs=_fake_recover,
        logger=logging.getLogger("test-strat-rt"),
    )
    return fake, rec


# --------------------------------------------------------------------------- #
# Structural contract tests
# --------------------------------------------------------------------------- #
def test_facade_delegates_only() -> None:
    from app.worker import run_strategy_scheduler_worker

    src = inspect.getsource(run_strategy_scheduler_worker)
    assert "run_strategy_scheduler_worker_runtime(" in src
    assert "await run_strategy_scheduler_worker_runtime(" in src
    # façade must no longer instantiate / define the lifecycle
    assert "AsyncIOScheduler(" not in src
    assert "CronTrigger(" not in src
    assert "async def scheduled_strategy_run" not in src
    assert "StrategyBatchService(" not in src
    # façade injects canonical helpers + heartbeat updater from worker.py
    assert "create_job_run=_create_job_run" in src
    assert "finish_job_run=_finish_job_run" in src
    assert "update_job_heartbeat=_update_job_heartbeat" in src
    assert "recover_stale_job_runs=recover_stale_scheduler_job_runs" in src


def test_facade_public_name_preserved() -> None:
    import app.worker as worker

    assert hasattr(worker, "run_strategy_scheduler_worker")
    assert callable(worker.run_strategy_scheduler_worker)


def test_runtime_source_contract() -> None:
    src = inspect.getsource(rt.run_strategy_scheduler_worker_runtime)
    # job id / cron / timezone / replace_existing
    assert '"strategy_run_daily"' in src
    assert "hour=18" in src and "minute=30" in src
    assert '"Asia/Shanghai"' in src
    assert "replace_existing=True" in src
    # one StrategyBatchService instance per runtime
    assert "service = StrategyBatchService()" in src
    # verbatim selector SQL
    assert 'StrategyDefinition.kind == "selector"' in src
    assert 'StrategyDefinition.environment == "production"' in src
    assert "StrategyDefinition.is_scheduled == True" in src
    assert "exists(released_subq)" in src
    assert ".correlate(StrategyDefinition)" in src
    # DSA vs non-DSA routing preserved
    assert "if strategy_key == DSA_SELECTOR:" in src
    assert "service.create_batch_run(" in src
    # ValueError vs Exception kept distinct
    assert "except ValueError as exc:" in src
    assert "except Exception as exc:" in src
    # heartbeat cadence preserved
    assert "if idx % 5 == 4:" in src
    assert "update_job_heartbeat(db, job_run)" in src
    # final-status mapping preserved
    assert 'final_status = "succeeded"' in src
    assert 'final_status = "partial_failed"' in src
    assert 'final_status = "failed"' in src
    # duplicate short-circuit preserved
    assert "SKIPPED_DUPLICATE" in src
    # runtime does NOT re-implement the canonical helpers
    assert "def create_job_run" not in src
    assert "def finish_job_run" not in src
    assert "def update_job_heartbeat" not in src
    assert "def recover_stale_job_runs" not in src
    # business owners imported in-closure (lazy, original positions)
    assert "from app.constants.strategy_keys import DSA_SELECTOR" in src
    assert (
        "from app.services.after_close_orchestrator import create_after_close_run" in src
    )
    assert "from app.services.calendar_service import is_trading_day_async" in src
    # no generic scheduler abstraction / second business owner
    assert "StrategySchedulerService" not in src
    assert "GenericScheduler" not in src


# --------------------------------------------------------------------------- #
# Behavioral tests (fake scheduler, no DB / external service)
# --------------------------------------------------------------------------- #
def test_runtime_registers_job_with_cron(monkeypatch) -> None:  # noqa: ANN001
    fake, _ = asyncio.run(_run_runtime(monkeypatch))
    ids = [j["kwargs"].get("id") for j in fake.jobs]
    assert "strategy_run_daily" in ids
    job = _job(fake, "strategy_run_daily")
    _assert_cron(job["trigger"], hour=18, minute=30)
    assert fake.started is True
    assert fake.shutdown_called is True
    assert fake.shutdown_wait is False


def test_non_trading_day_skips_everything(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    fake, rec = asyncio.run(
        _run_runtime(monkeypatch, is_trading=False, rec=rec)
    )
    asyncio.run(_job(fake, "strategy_run_daily")["func"]())
    # non-trading day must short-circuit before create_job_run / selector SQL
    assert rec["create_job_run_calls"] == []
    assert rec["execute_calls"] == 0


def test_duplicate_skips_selector_and_owners(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    fake, rec = asyncio.run(
        _run_runtime(monkeypatch, create_job_run_returns_none=True, rec=rec)
    )
    asyncio.run(_job(fake, "strategy_run_daily")["func"]())
    # duplicate job_run must short-circuit before selector SQL / after-close / batch
    assert len(rec["create_job_run_calls"]) == 1
    assert rec["execute_calls"] == 0
    assert rec["create_after_close_calls"] == []
    assert rec["create_batch_calls"] == []


def test_final_status_succeeded(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    fake, rec = asyncio.run(
        _run_runtime(monkeypatch, selector_keys=["OTHER_A", "OTHER_B", "OTHER_C"], rec=rec)
    )
    asyncio.run(_job(fake, "strategy_run_daily")["func"]())
    assert rec["finish_job_run_calls"][-1][0] == "succeeded"
    assert rec["create_after_close_calls"] == []
    assert len(rec["create_batch_calls"]) == 3


def test_final_status_partial_failed(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    fake, rec = asyncio.run(
        _run_runtime(
            monkeypatch,
            selector_keys=["DSA_TEST", "OTHER_A", "OTHER_B"],
            fail_keys=["OTHER_A"],
            rec=rec,
        )
    )
    asyncio.run(_job(fake, "strategy_run_daily")["func"]())
    assert rec["finish_job_run_calls"][-1][0] == "partial_failed"
    # DSA branch still ran (1 after-close), one non-DSA failed, one succeeded
    assert len(rec["create_after_close_calls"]) == 1
    assert len(rec["create_batch_calls"]) == 2


def test_final_status_failed(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    fake, rec = asyncio.run(
        _run_runtime(
            monkeypatch,
            selector_keys=["OTHER_A", "OTHER_B"],
            fail_keys=["OTHER_A", "OTHER_B"],
            rec=rec,
        )
    )
    asyncio.run(_job(fake, "strategy_run_daily")["func"]())
    assert rec["finish_job_run_calls"][-1][0] == "failed"


def test_heartbeat_called_once_for_five_selectors(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    fake, rec = asyncio.run(
        _run_runtime(monkeypatch, selector_keys=["K1", "K2", "K3", "K4", "K5"], rec=rec)
    )
    asyncio.run(_job(fake, "strategy_run_daily")["func"]())
    # idx % 5 == 4 -> only the 5th (idx=4) selector triggers a heartbeat
    assert rec["update_heartbeat_calls"] == 1
