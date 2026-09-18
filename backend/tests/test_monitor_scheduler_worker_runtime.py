"""Tests for the Monitor Scheduler main-runtime (PANJI-GOV-W4A / W4B1 / C2C).

Layers mirror the Bars / Strategy / Calendar pattern:

* Structural contract tests — façade only delegates, public name unchanged, the
  runtime owns the trading-session loop / reentrancy guard / commit+rollback+finally
  ordering / startup & error notify timing, and now owns the two Monitor session
  helpers (_get_monitor_session / _find_or_create_monitor_session_job_run /
  _finalize_monitor_session) while injecting the canonical ``create_job_run`` (no
  second SchedulerJobRun owner, no GenericScheduler / MonitorManager).
* Behavioral tests (fake collaborators, no DB / external service) — these lock the
  W4A blockers plus the C2C exclusive-ownership + fenced session lifecycle:
  * eval-recovery exception must still propagate (not swallowed);
  * scheduler-job-recovery exception must still be swallowed and logged;
  * startup notification timing must be unchanged (after both recoveries, before loop);
  * cycle commit/rollback/finally ordering must be unchanged (happy cycle runs once);
  * _get_monitor_session boundary (09:30 included / 11:30 excluded, 13:00 included /
    15:00 excluded);
  * _find_or_create_monitor_session_job_run delegates verbatim to the injected
    create_job_run with the exact job_name / business_date / lease_seconds / metadata /
    run_key.
* C2C fenced-ownership behavioral contracts (the 7 core contracts the user approved):
  same session acquires once and runs multiple cycles; an already-owned-elsewhere
  session is skipped (no SELECT-reuse / no cross-process ownership theft); the session
  uses exactly one 30s fenced heartbeat; per-cycle progress is written through
  update_owned_job_run_progress carrying the token; a lost lease stops the session
  with no terminal; a session boundary transition finalizes the previous session once;
  and an unexpected runtime exception is swallowed, written as a fenced failed terminal,
  and never re-raised.
"""

import asyncio
import inspect
import logging
import uuid
from datetime import datetime
from datetime import time as tc
from zoneinfo import ZoneInfo

import pytest

from app.services import monitor_scheduler_worker_runtime as rt


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
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
        return _scalar_value["v"]


class FakeSessionCM:
    def __init__(self, rec: dict) -> None:  # noqa: ANN001
        self._rec = rec
        self.session = FakeSession(rec)

    async def __aenter__(self) -> FakeSession:
        return self.session

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeJobRun:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.status = "running"
        self.worker_instance_id = "monitor-owner"
        self.lease_epoch = 1


class _FakeToken:
    def __init__(self, *, job_run_id, worker_instance_id, lease_epoch, lease_seconds):  # noqa: ANN001
        self.job_run_id = job_run_id
        self.worker_instance_id = worker_instance_id
        self.lease_epoch = lease_epoch
        self.lease_seconds = lease_seconds


class _FakeJobLeaseLostError(RuntimeError):
    pass


# Shared state for the faked fenced primitives.
_finalize_calls: list[dict] = []
_finalize_return: list[bool] = [True]
_update_calls: list[dict] = []
_heartbeats: list = []
_unfenced_calls: list = []
_lose_lease: dict = {"v": False}
_scalar_value: dict = {"v": datetime(2026, 1, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))}
_update_raise: dict = {"v": False}


class FakeFencedHeartbeat:
    def __init__(self, token, *, interval_seconds: float = 30.0, refresh=None):  # noqa: ANN001
        self.token = token
        self.interval_seconds = interval_seconds
        self.started = False
        self.stopped = False
        self.lost = False
        _heartbeats.append(self)

    async def start(self) -> None:
        self.started = True

    def ensure_owned(self) -> None:
        if _lose_lease["v"]:
            raise rt.JobLeaseLostError("fake lost lease")

    async def stop(self) -> None:
        self.stopped = True


async def _fake_finalize(
    token,  # noqa: ANN001
    *,
    status: str,
    metadata_updates: dict,
    total_count: int,
    succeeded_count: int,
    failed_count: int,
    error_code: str | None = None,
    error_message: str | None = None,
) -> bool:
    # 模拟 lock_owned_job_run 失败：finalize_job_run 内部 catch JobLeaseLostError 后
    # 返回 False，不写 terminal（因此也不应被记录为一次成功写入）。
    if _lose_lease["v"]:
        return False
    _finalize_calls.append(
        {
            "token": token,
            "status": status,
            "metadata_updates": metadata_updates,
            "total_count": total_count,
            "succeeded_count": succeeded_count,
            "failed_count": failed_count,
            "error_code": error_code,
            "error_message": error_message,
        }
    )
    return _finalize_return[0]


async def _fake_update_progress(
    token,  # noqa: ANN001
    *,
    last_cycle_at,  # noqa: ANN001
    succeeded_count: int,
    failed_count: int,
    metadata_updates: dict,
    session_factory=None,  # noqa: ANN001
) -> None:
    if _lose_lease["v"]:
        raise rt.JobLeaseLostError("fake lost lease during progress")
    if _update_raise["v"]:
        raise RuntimeError("fake progress boom")
    _update_calls.append(
        {
            "token": token,
            "succeeded_count": succeeded_count,
            "failed_count": failed_count,
            "metadata_updates": dict(metadata_updates),
        }
    )


async def _fast_sleep(*_args, **_kwargs) -> None:  # noqa: ANN001
    return None


def _reset_fakes() -> None:
    _finalize_calls.clear()
    _finalize_return[0] = True
    _update_calls.clear()
    _heartbeats.clear()
    _unfenced_calls.clear()
    _lose_lease["v"] = False
    _scalar_value["v"] = datetime(2026, 1, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    _update_raise["v"] = False


def _assert_no_unfenced() -> None:
    # Monitor must never fall back to the unfenced _finish_job_run helper.
    assert _unfenced_calls == [], f"unfenced finish_job_run was called: {_unfenced_calls}"


def _exit_after(n: int):
    """should_shutdown 返回 False 共 n 次后返回 True。"""
    state = {"i": 0}

    def _probe() -> bool:
        state["i"] += 1
        return state["i"] > n

    return _probe


def _default_monitor_session():
    """默认强制返回上午 session（测试驱动不依赖真实时间）。"""
    return ("morning", tc(9, 30), tc(11, 30))


# --------------------------------------------------------------------------- #
# Runtime driver
# --------------------------------------------------------------------------- #
def _run_runtime(
    monkeypatch,  # noqa: ANN001
    *,
    should_shutdown,
    create_returns_none: bool = False,
    get_session=None,  # noqa: ANN001
    is_trading: bool = True,
    rec: dict | None = None,
    batch_service=None,  # noqa: ANN001
    recover_job_runs_boom: bool = False,
    lose_lease: bool = False,
    progress_raise: bool = False,
):
    rec = rec if rec is not None else {}
    rec.setdefault("eval_recovery_calls", 0)
    rec.setdefault("job_recovery_calls", 0)
    rec.setdefault("startup_notify_calls", [])
    rec.setdefault("error_notify_calls", [])
    rec.setdefault("cycle_calls", 0)
    rec.setdefault("create_job_run_calls", 0)
    rec.setdefault("commits", 0)
    rec.setdefault("rollbacks", 0)

    _reset_fakes()
    # per-run lease/progress flags set AFTER reset.
    _lose_lease["v"] = lose_lease
    _update_raise["v"] = progress_raise
    # C2C fenced primitives are faked at the module level so the runtime consumes
    # them exactly as it would the production primitives.
    monkeypatch.setattr(rt, "FencedJobToken", _FakeToken)
    monkeypatch.setattr(rt, "FencedJobHeartbeat", FakeFencedHeartbeat)
    monkeypatch.setattr(rt, "JobLeaseLostError", _FakeJobLeaseLostError)
    monkeypatch.setattr(rt, "finalize_job_run", _fake_finalize)
    monkeypatch.setattr(rt, "update_owned_job_run_progress", _fake_update_progress)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    class _Settings:
        intraday_monitor_poll_seconds = 1

    monkeypatch.setattr(rt, "get_settings", lambda: _Settings())

    if batch_service is None:

        class _BatchService:
            async def recover_stale_evaluations(self, db):  # noqa: ANN001
                rec["eval_recovery_calls"] += 1
                return 0

            async def execute_monitor_cycle(self, db):  # noqa: ANN001
                rec["cycle_calls"] += 1

                class _R:
                    total_events_written = 0
                    total_instruments = 0
                    total_notifications_created = 0

                return _R()

        batch_service = _BatchService
    monkeypatch.setattr(rt, "MonitorBatchService", batch_service)

    async def _fake_is_trading(db, trade_date):  # noqa: ANN001
        return is_trading

    monkeypatch.setattr(
        "app.services.calendar_service.is_trading_day_async",
        _fake_is_trading,
    )

    async def _fake_recover_job_runs(db):  # noqa: ANN001
        rec["job_recovery_calls"] += 1
        if recover_job_runs_boom:
            raise RuntimeError("job-boom")
        return 0

    async def _fake_heartbeat(name):  # noqa: ANN001
        return None

    async def _fake_finish_job_run(db, job_run, status, **kw):  # noqa: ANN001
        _unfenced_calls.append((status, kw))

    async def _fake_notify(title, content, *, is_error=False, **kwargs):  # noqa: ANN001
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
        rec["create_job_run_calls"] += 1
        return None if create_returns_none else _FakeJobRun()

    # W4B2: notifier is a module import, not an injected param; monkeypatch on the
    # runtime module so the production API is not compromised for tests.
    monkeypatch.setattr(rt, "notify_monitor_status", _fake_notify)

    if get_session is None:
        get_session = _default_monitor_session
    monkeypatch.setattr(rt, "_get_monitor_session", lambda now: get_session())  # noqa: ANN001

    async def _coro():
        await rt.run_monitor_scheduler_worker_runtime(
            session_factory=lambda: FakeSessionCM(rec),
            heartbeat_loop=_fake_heartbeat,
            should_shutdown=should_shutdown,
            recover_stale_job_runs=_fake_recover_job_runs,
            create_job_run=_fake_create_job_run,
            finish_job_run=_fake_finish_job_run,
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
    # W4B2: the façade no longer injects the notifier (it is a dedicated module import)
    assert "notify_monitor_status=" not in src
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
    # soft-fail source_bar_time query (progress metadata, still fenced)
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
    # W4B1: the Monitor session helpers are now OWNED by the runtime
    assert "def _get_monitor_session" in src
    assert "def _find_or_create_monitor_session_job_run" in src
    assert "def _finalize_monitor_session" in src
    # runtime call sites use the internal helpers
    assert "get_monitor_session(now)" in src
    assert "_find_or_create_monitor_session_job_run(" in src
    # runtime does NOT re-implement the composition-root helpers / business owners
    assert "def notify_monitor_status" not in src
    assert "def finish_job_run" not in src
    assert "def recover_stale_job_runs" not in src
    # W4B2: the notifier is a dedicated module import, not an injected param
    assert "from app.services.monitor_status_notifier import notify_monitor_status" in src
    assert "notify_monitor_status=" not in src
    assert "async def notify_monitor_status" not in src
    # no generic abstraction
    assert "GenericScheduler" not in src
    assert "MonitorManager" not in src
    # C2C: exclusive owner + fenced session lifecycle (no SELECT-reuse, no
    # session_finish_margin, no manual lease/terminal writes)
    assert "FencedJobToken(" in src
    assert "FencedJobHeartbeat(" in src
    assert "interval_seconds=30.0" in src
    assert "finalize_job_run(" in src
    assert "update_owned_job_run_progress(" in src
    assert "ensure_owned()" in src
    assert "JobLeaseLostError" in src
    assert "SKIPPED_DUPLICATE" in src
    # removed over-design
    assert "session_finish_margin" not in src
    assert "lease_expires_at = now + timedelta(seconds=120)" not in src


# --------------------------------------------------------------------------- #
# Helper boundary tests (W4B1) — no PG, no DB
# --------------------------------------------------------------------------- #
def test_get_monitor_session_boundaries() -> None:
    from datetime import datetime

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

    class _BatchServiceBoom:
        async def recover_stale_evaluations(self, db):  # noqa: ANN001
            rec["eval_recovery_calls"] += 1
            raise RuntimeError("eval-boom")

        async def execute_monitor_cycle(self, db):  # noqa: ANN001
            raise AssertionError("must not reach cycle")

    coro, rec = _run_runtime(monkeypatch, should_shutdown=lambda: False, batch_service=_BatchServiceBoom, rec=rec)
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
        monkeypatch, should_shutdown=lambda: True, recover_job_runs_boom=True, rec=rec,
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
    rec: dict = {}
    coro, rec = _run_runtime(monkeypatch, should_shutdown=_exit_after(1), rec=rec)
    asyncio.run(coro)
    # full startup ran
    assert rec["eval_recovery_calls"] == 1
    assert rec["job_recovery_calls"] == 1
    assert rec["startup_notify_calls"][0][0] == "监控服务已启动"
    # exactly one cycle executed
    assert rec["cycle_calls"] == 1
    assert rec["commits"] >= 1
    assert rec.get("rollbacks", 0) == 0
    # W4B1: the runtime delegated session job-run creation to the injected create_job_run
    assert rec["create_job_run_calls"] == 1
    # C2C: exactly one fenced progress write, exactly one 30s heartbeat, no terminal
    assert len(_update_calls) == 1
    assert len(_heartbeats) == 1
    assert _heartbeats[0].interval_seconds == 30.0
    assert _finalize_calls == []
    _assert_no_unfenced()


# --------------------------------------------------------------------------- #
# C2C fenced-ownership core contracts (the 7 approved contracts)
# --------------------------------------------------------------------------- #
def test_same_session_acquires_once_and_runs_multiple_cycles(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    coro, rec = _run_runtime(monkeypatch, should_shutdown=_exit_after(3), rec=rec)
    asyncio.run(coro)
    # acquire exactly once (exclusive owner); 3 cycles reuse the same token
    assert rec["create_job_run_calls"] == 1
    assert rec["cycle_calls"] == 3
    assert len(_update_calls) == 3
    # single 30s heartbeat for the whole session; graceful shutdown stops it, no terminal
    assert len(_heartbeats) == 1
    assert _heartbeats[0].started is True
    assert _heartbeats[0].stopped is True
    assert _finalize_calls == []
    _assert_no_unfenced()


def test_active_session_owned_elsewhere_skips_business(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    # create_job_run returns None → another process owns the session
    coro, rec = _run_runtime(
        monkeypatch, should_shutdown=_exit_after(1), create_returns_none=True, rec=rec,
    )
    asyncio.run(coro)
    # attempt to acquire happened, but no business ran and no ownership theft
    assert rec["create_job_run_calls"] == 1
    assert rec["cycle_calls"] == 0
    assert len(_update_calls) == 0
    assert len(_heartbeats) == 0
    assert _finalize_calls == []
    _assert_no_unfenced()


def test_session_uses_30s_fenced_heartbeat(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    coro, rec = _run_runtime(monkeypatch, should_shutdown=_exit_after(1), rec=rec)
    asyncio.run(coro)
    assert len(_heartbeats) == 1
    assert _heartbeats[0].interval_seconds == 30.0
    assert _heartbeats[0].started is True
    assert _heartbeats[0].stopped is True
    # no terminal on graceful shutdown (heartbeat-only stop)
    assert _finalize_calls == []
    _assert_no_unfenced()


def test_cycle_progress_is_fenced(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    coro, rec = _run_runtime(monkeypatch, should_shutdown=_exit_after(1), rec=rec)
    asyncio.run(coro)
    assert len(_update_calls) == 1
    call = _update_calls[0]
    # token carries the created job_run ownership fields
    assert call["token"].job_run_id is not None
    assert call["token"].worker_instance_id == "monitor-owner"
    assert call["token"].lease_epoch == 1
    assert call["token"].lease_seconds == 120
    assert call["succeeded_count"] == 1
    assert call["failed_count"] == 0
    # progress metadata (source_bar_time) is merged through the fenced primitive
    assert call["metadata_updates"].get("last_bar_time") is not None


def test_lease_lost_stops_session_without_terminal(monkeypatch) -> None:  # noqa: ANN001
    # ownership lost (watchdog transferred) before/at the cycle → stop session, no terminal
    rec: dict = {}
    coro, rec = _run_runtime(monkeypatch, should_shutdown=_exit_after(1), lose_lease=True, rec=rec)
    asyncio.run(coro)
    assert _finalize_calls == []
    assert len(_update_calls) == 0
    assert rec["cycle_calls"] == 0
    # heartbeat still stopped
    assert len(_heartbeats) == 1
    assert _heartbeats[0].stopped is True
    _assert_no_unfenced()


def test_session_transition_finalizes_once(monkeypatch) -> None:  # noqa: ANN001
    # morning → afternoon: previous session finalized once (succeeded), new heartbeat started
    state = {"n": 0}

    def _session():
        state["n"] += 1
        if state["n"] == 1:
            return ("morning", tc(9, 30), tc(11, 30))
        return ("afternoon", tc(13, 0), tc(15, 0))

    rec: dict = {}
    # exit after 2 cycles (morning + afternoon each ran once)
    coro, rec = _run_runtime(
        monkeypatch,
        should_shutdown=lambda: rec["cycle_calls"] >= 2,
        get_session=_session,
        rec=rec,
    )
    asyncio.run(coro)
    # exactly one terminal (morning succeeded) at the boundary transition
    assert len(_finalize_calls) == 1
    assert _finalize_calls[0]["status"] == "succeeded"
    assert _finalize_calls[0]["total_count"] == 1
    assert _finalize_calls[0]["succeeded_count"] == 1
    # two sessions → two heartbeats (one per session), both stopped
    assert len(_heartbeats) == 2
    assert _heartbeats[0].stopped is True
    assert _heartbeats[1].stopped is True
    assert rec["cycle_calls"] == 2
    _assert_no_unfenced()


def test_runtime_exception_fenced_failed_and_swallowed(monkeypatch) -> None:  # noqa: ANN001
    # unexpected runtime exception (fenced progress write boom, after a successful cycle)
    # must be swallowed, written as a fenced failed terminal, and never re-raised.
    rec: dict = {}
    coro, rec = _run_runtime(monkeypatch, should_shutdown=_exit_after(1), progress_raise=True, rec=rec)
    # must NOT raise out of the runtime
    asyncio.run(coro)
    # exactly one fenced failed terminal
    assert len(_finalize_calls) == 1
    assert _finalize_calls[0]["status"] == "failed"
    assert _finalize_calls[0]["metadata_updates"].get("error") is not None
    # error notification sent
    assert len(rec["error_notify_calls"]) >= 1
    # never falls back to the unfenced helper
    _assert_no_unfenced()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
