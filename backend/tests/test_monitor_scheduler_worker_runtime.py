"""Tests for the Monitor Scheduler main-runtime extraction (PANJI-GOV-W4A / W4B1).

Layers mirror the Bars / Strategy / Calendar pattern:

* Structural contract tests — façade only delegates, public name unchanged, the
  runtime owns the trading-session loop / reentrancy guard / commit+rollback+finally
  ordering / startup & error notify timing, and now owns the two Monitor session
  helpers (_get_monitor_session / _find_or_create_monitor_session_job_run) while
  injecting the canonical ``create_job_run`` (no second SchedulerJobRun owner, no
  GenericScheduler / MonitorManager).
* Behavioral tests (fake collaborators, no DB / external service) — these lock the
  four W4A blockers plus the W4B1 ownership move:
  * eval-recovery exception must still propagate (not swallowed);
  * scheduler-job-recovery exception must still be swallowed and logged;
  * startup notification timing must be unchanged (after both recoveries, before loop);
  * cycle commit/rollback/finally ordering must be unchanged (happy cycle runs once);
  * _get_monitor_session boundary (09:30 included / 11:30 excluded, 13:00 included /
    15:00 excluded);
  * _find_or_create_monitor_session_job_run delegates verbatim to the injected
    create_job_run with the exact job_name / business_date / lease_seconds / metadata /
    run_key.
"""

import asyncio
import inspect
import logging
from zoneinfo import ZoneInfo

import pytest

from app.services import monitor_scheduler_worker_runtime as rt


class FakeResult:
    def scalar_one_or_none(self):
        return None


class FakeSession:
    def __init__(self, rec: dict) -> None:  # noqa: ANN001
        self._rec = rec

    async def commit(self) -> None:
        self._rec["commits"] = self._rec.get("commits", 0) + 1

    async def rollback(self) -> None:
        self._rec["rollbacks"] = self._rec.get("rollbacks", 0) + 1

    async def execute(self, stmt):  # noqa: ANN001
        return FakeResult()

    async def scalar(self, stmt):  # noqa: ANN001
        return None


class FakeSessionCM:
    def __init__(self, rec: dict) -> None:  # noqa: ANN001
        self._rec = rec
        self.session = FakeSession(rec)

    async def __aenter__(self) -> FakeSession:
        return self.session

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeJobRun:
    id = "fake-mon-jr"
    last_cycle_at = None
    heartbeat_at = None
    lease_expires_at = None
    succeeded_count = 0
    failed_count = 0
    metadata_json = None


def _run_runtime(
    monkeypatch,  # noqa: ANN001
    *,
    should_shutdown,
    recover_eval_side_effect=None,  # noqa: ANN001
    recover_job_side_effect=None,  # noqa: ANN001
    rec: dict | None = None,
):
    rec = rec if rec is not None else {}
    rec.setdefault("eval_recovery_calls", 0)
    rec.setdefault("job_recovery_calls", 0)
    rec.setdefault("startup_notify_calls", [])
    rec.setdefault("error_notify_calls", [])
    rec.setdefault("cycle_calls", 0)
    rec.setdefault("finish_calls", [])

    class _Settings:
        intraday_monitor_poll_seconds = 1

    monkeypatch.setattr(rt, "get_settings", lambda: _Settings())

    class _BatchService:
        async def recover_stale_evaluations(self, db):  # noqa: ANN001
            rec["eval_recovery_calls"] += 1
            if recover_eval_side_effect is not None:
                raise recover_eval_side_effect
            return 0

        async def execute_monitor_cycle(self, db):  # noqa: ANN001
            rec["cycle_calls"] += 1

            class _R:
                total_events_written = 0
                total_instruments = 0
                total_notifications_created = 0

            return _R()

    monkeypatch.setattr(rt, "MonitorBatchService", _BatchService)

    async def _fake_recover_job_runs(db):  # noqa: ANN001
        rec["job_recovery_calls"] += 1
        if recover_job_side_effect is not None:
            raise recover_job_side_effect
        return 0

    async def _fake_heartbeat(name):  # noqa: ANN001
        return None

    async def _fake_finish_job_run(db, job_run, status, **kw):  # noqa: ANN001
        rec["finish_calls"].append((status, kw))

    async def _fake_notify(title, content, *, is_error=False):  # noqa: ANN001
        if is_error:
            rec["error_notify_calls"].append((title, content))
        else:
            rec["startup_notify_calls"].append((title, content))

    async def _fake_create_job_run(
        db,  # noqa: ANN001
        job_name,  # noqa: ANN001
        business_date,  # noqa: ANN001
        *,
        lease_seconds=120,
        metadata=None,  # noqa: ANN001
        run_key=None,  # noqa: ANN001
        **kw,  # noqa: ANN001
    ):
        rec.setdefault("create_job_run_calls", 0)
        rec["create_job_run_calls"] += 1
        return _FakeJobRun()

    async def _coro():
        await rt.run_monitor_scheduler_worker_runtime(
            session_factory=lambda: FakeSessionCM(rec),
            heartbeat_loop=_fake_heartbeat,
            should_shutdown=should_shutdown,
            recover_stale_job_runs=_fake_recover_job_runs,
            create_job_run=_fake_create_job_run,
            finish_job_run=_fake_finish_job_run,
            notify_monitor_status=_fake_notify,
            monotonic_clock=lambda: 0.0,
            logger=logging.getLogger("test-mon-rt"),
        )

    return _coro(), rec


# --------------------------------------------------------------------------- #
# Structural contract tests
# --------------------------------------------------------------------------- #
def test_facade_delegates_only() -> None:
    from app.worker import run_monitor_scheduler_worker

    src = inspect.getsource(run_monitor_scheduler_worker)
    assert "run_monitor_scheduler_worker_runtime(" in src
    assert "await run_monitor_scheduler_worker_runtime(" in src
    # façade must no longer contain the main monitor loop / business logic
    # (the verbatim docstring may mention execute_monitor_cycle by name, so we
    #  check for the code call, not the bare word)
    assert "service.execute_monitor_cycle(" not in src
    assert "await asyncio.sleep(300)" not in src
    assert "监控服务已启动" not in src
    assert "recover_stale_evaluations" not in src
    # after W4B1 the session helpers live in the runtime; the façade must no longer
    # inject them, and must instead inject the canonical create_job_run
    assert "get_monitor_session=_get_monitor_session" not in src
    assert "find_or_create_session_job_run=" not in src
    assert "create_job_run=_create_job_run" in src
    # façade still injects the other canonical helpers + business owners + clock
    assert "session_factory=AsyncSessionLocal" in src
    assert "heartbeat_loop=_heartbeat_loop" in src
    assert "should_shutdown=lambda: _shutdown" in src
    assert "recover_stale_job_runs=recover_stale_scheduler_job_runs" in src
    assert "finish_job_run=_finish_job_run" in src
    assert "notify_monitor_status=_notify_monitor_status" in src
    assert "monotonic_clock=_time_monotonic" in src


def test_facade_public_name_preserved() -> None:
    import app.worker as worker

    assert hasattr(worker, "run_monitor_scheduler_worker")
    assert callable(worker.run_monitor_scheduler_worker)


def test_runtime_source_contract() -> None:
    # Inspect the whole module: the two session helpers are now module-level
    # functions owned by this runtime (not nested inside the main loop).
    src = inspect.getsource(rt)
    # recovery paths
    assert "recover_stale_evaluations" in src
    assert "recover_stale_job_runs" in src
    # non-trading / non-session cadence
    assert "sleep(300)" in src
    assert "min(wait_seconds, 60)" in src
    # reentrancy guard
    assert "_cycle_running" in src
    assert "_cycle_running = True" in src
    assert "finally:" in src
    assert "_cycle_running = False" in src
    # cycle execution + transaction boundaries
    assert "execute_monitor_cycle" in src
    assert "db.commit()" in src
    assert "db.rollback()" in src
    # lease + session finish
    assert "lease_expires_at = now + timedelta(seconds=120)" in src
    assert "session_finish_margin" in src
    # soft-fail source_bar_time query
    assert "查询 latest source_bar_time 失败" in src
    # notify timing
    assert "监控服务已启动" in src
    assert "监控服务异常" in src
    # one MonitorBatchService instance per runtime
    assert "service = MonitorBatchService()" in src
    # monotonic clock injected (not imported as _time_monotonic)
    assert "monotonic_clock()" in src
    assert "_time_monotonic" not in src
    # injected canonical create_job_run (W4B1) is called, not re-defined
    assert "create_job_run" in src
    assert "def _create_job_run" not in src
    # W4B1: the two Monitor session helpers are now OWNED by the runtime
    assert "def _get_monitor_session" in src
    assert "def _find_or_create_monitor_session_job_run" in src
    # runtime call sites use the internal helpers
    assert "get_monitor_session(now)" in src
    assert "_find_or_create_monitor_session_job_run(" in src
    # runtime does NOT re-implement the composition-root helpers / business owners
    assert "def notify_monitor_status" not in src
    assert "def finish_job_run" not in src
    assert "def recover_stale_job_runs" not in src
    # no generic abstraction
    assert "GenericScheduler" not in src
    assert "MonitorManager" not in src


# --------------------------------------------------------------------------- #
# Helper boundary tests (W4B1) — no PG, no DB
# --------------------------------------------------------------------------- #
def test_get_monitor_session_boundaries() -> None:
    from datetime import datetime
    from datetime import time as tc

    tz = ZoneInfo("Asia/Shanghai")
    cases = [
        (datetime(2026, 1, 1, 9, 29, 59, tzinfo=tz), None),
        (datetime(2026, 1, 1, 9, 30, 0, tzinfo=tz), ("morning", tc(9, 30), tc(11, 30))),
        (datetime(2026, 1, 1, 11, 29, 59, tzinfo=tz), ("morning", tc(9, 30), tc(11, 30))),
        (datetime(2026, 1, 1, 11, 30, 0, tzinfo=tz), None),
        (datetime(2026, 1, 1, 12, 59, 59, tzinfo=tz), None),
        (datetime(2026, 1, 1, 13, 0, 0, tzinfo=tz), ("afternoon", tc(13, 0), tc(15, 0))),
        (datetime(2026, 1, 1, 14, 59, 59, tzinfo=tz), ("afternoon", tc(13, 0), tc(15, 0))),
        (datetime(2026, 1, 1, 15, 0, 0, tzinfo=tz), None),
    ]
    for now, expected in cases:
        got = rt._get_monitor_session(now)
        assert got == expected, f"{now}: expected {expected!r}, got {got!r}"


def test_find_or_create_monitor_session_job_run_delegates_to_create_job_run() -> None:
    captured: dict = {}
    from datetime import datetime

    tz = ZoneInfo("Asia/Shanghai")

    async def _fake_create_job_run(
        db,  # noqa: ANN001
        job_name,  # noqa: ANN001
        business_date,  # noqa: ANN001
        *,
        lease_seconds=120,
        metadata=None,  # noqa: ANN001
        run_key=None,  # noqa: ANN001
        **kw,  # noqa: ANN001
    ):
        captured["job_name"] = job_name
        captured["business_date"] = business_date
        captured["lease_seconds"] = lease_seconds
        captured["metadata"] = metadata
        captured["run_key"] = run_key
        return _FakeJobRun()

    now = datetime(2026, 1, 1, 9, 45, tzinfo=tz)
    result = asyncio.run(
        rt._find_or_create_monitor_session_job_run(
            db=None,
            now_cst=now,
            business_date="2026-01-01",
            session_label="morning",
            create_job_run=_fake_create_job_run,
        )
    )
    assert result is not None
    assert captured["job_name"] == "monitor_scheduler"
    assert captured["business_date"] == "2026-01-01"
    assert captured["lease_seconds"] == 120
    assert captured["metadata"] == {"session_label": "morning"}
    assert captured["run_key"] == "monitor_scheduler:2026-01-01:morning"


# --------------------------------------------------------------------------- #
# Behavioral tests (fake collaborators, no DB / external service)
# --------------------------------------------------------------------------- #
def test_startup_sequence_then_shutdown(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    coro, rec = _run_runtime(monkeypatch, should_shutdown=lambda: True, rec=rec)
    asyncio.run(coro)
    # eval recovery executed
    assert rec["eval_recovery_calls"] == 1
    # scheduler-job recovery executed (after eval recovery)
    assert rec["job_recovery_calls"] == 1
    # startup notify executed (after both recoveries)
    assert rec["startup_notify_calls"][0][0] == "监控服务已启动"
    # loop never entered
    assert rec["cycle_calls"] == 0


def test_eval_recovery_exception_propagates(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    coro, rec = _run_runtime(
        monkeypatch,
        should_shutdown=lambda: False,
        recover_eval_side_effect=RuntimeError("eval-boom"),
        rec=rec,
    )
    with pytest.raises(RuntimeError):
        asyncio.run(coro)
    # eval recovery attempted
    assert rec["eval_recovery_calls"] == 1
    # scheduler-job recovery must NOT run (eval recovery raised before it)
    assert rec["job_recovery_calls"] == 0
    # startup notify must NOT run (it is after job recovery)
    assert rec["startup_notify_calls"] == []


def test_job_recovery_exception_caught_startup_notify_runs(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    coro, rec = _run_runtime(
        monkeypatch,
        should_shutdown=lambda: True,
        recover_job_side_effect=RuntimeError("job-boom"),
        rec=rec,
    )
    asyncio.run(coro)
    # eval recovery ran
    assert rec["eval_recovery_calls"] == 1
    # job recovery attempted (raised) but swallowed
    assert rec["job_recovery_calls"] == 1
    # startup notify still happened (after the swallowed job-recovery)
    assert rec["startup_notify_calls"][0][0] == "监控服务已启动"
    # loop not entered
    assert rec["cycle_calls"] == 0


def test_one_monitor_cycle_executes_on_trading_session(monkeypatch) -> None:  # noqa: ANN001
    from datetime import time as tc

    async def _fake_is_trading(db, trade_date):  # noqa: ANN001
        return True

    monkeypatch.setattr(
        "app.services.calendar_service.is_trading_day_async", _fake_is_trading
    )
    # W4B1: the session helper is now owned by the runtime; force a morning session
    # via monkeypatch (no time faking in the production API).
    monkeypatch.setattr(
        rt,
        "_get_monitor_session",
        lambda now: ("morning", tc(9, 30), tc(11, 30)),  # noqa: ANN001
    )

    state = {"n": 0}

    def _should_shutdown() -> bool:
        state["n"] += 1
        # enter the loop once, then exit after the first cycle's tail sleep
        return state["n"] > 1

    rec: dict = {}
    coro, rec = _run_runtime(monkeypatch, should_shutdown=_should_shutdown, rec=rec)
    asyncio.run(coro)
    # full startup ran
    assert rec["eval_recovery_calls"] == 1
    assert rec["job_recovery_calls"] == 1
    assert rec["startup_notify_calls"][0][0] == "监控服务已启动"
    # exactly one cycle executed (commit happened, no rollback in the happy path)
    assert rec["cycle_calls"] == 1
    assert rec["commits"] >= 1
    assert rec.get("rollbacks", 0) == 0
    # W4B1: the runtime delegated session job-run creation to the injected create_job_run
    assert rec["create_job_run_calls"] == 1
