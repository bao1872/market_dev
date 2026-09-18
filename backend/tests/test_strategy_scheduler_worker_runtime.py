"""Tests for the Strategy Scheduler runtime (PANJI-GOV-W3 + C2B).

Layers mirror the Bars / Calendar pattern:

* Structural contract tests — façade only delegates, public name unchanged, the
  runtime keeps the 18:30 cron / ``strategy_run_daily`` / Asia/Shanghai, the
  verbatim selector SQL, the DSA-vs-non-DSA branches, the ValueError/Exception
  split, the final-status mapping (succeeded / partial_failed / failed), and
  injects the canonical ``SchedulerJobRun`` helpers (no second owner, no
  ``StrategySchedulerService`` / ``GenericScheduler``).
* Behavioral tests (fake scheduler + fake collaborators, no DB / external
  service) — the C2B fenced ownership lifecycle: a ``FencedJobToken`` is built
  from the created job's ownership fields, a 30s ``FencedJobHeartbeat`` keeps the
  lease alive, mid-loop metadata merges go through ``merge_owned_job_run_metadata``
  (never the ORM ``job_run.metadata_json =``), and the terminal state is written
  only through ``finalize_job_run`` carrying the token.  A lost lease is never
  allowed to write a terminal state, and ``partial_failed`` (a real
  first-class terminal) is accepted.
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
# Fakes for the C2B fenced primitives
# --------------------------------------------------------------------------- #
class _FakeToken:
    def __init__(self, *, job_run_id, worker_instance_id, lease_epoch, lease_seconds):  # noqa: ANN001
        self.job_run_id = job_run_id
        self.worker_instance_id = worker_instance_id
        self.lease_epoch = lease_epoch
        self.lease_seconds = lease_seconds


class _FakeJobLeaseLostError(RuntimeError):
    pass


_heartbeats: list = []
_finalize_calls: list[dict] = []
_finalize_return: list[bool] = [True]
_merge_calls: list[dict] = []
_unfenced_calls: list = []
_lose_lease: dict = {"v": False}
_merge_lost: dict = {"v": False}


class FakeFencedHeartbeat:
    def __init__(self, token, *, interval_seconds: float = 30.0, refresh=None):  # noqa: ANN001
        self.token = token
        self.interval_seconds = interval_seconds
        self.started = False
        self.stopped = False
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
    # 模拟 lock_owned_job_run 失败：finalize_job_run 内部 catch JobLeaseLostError
    # 后返回 False，不写 terminal（因此也不应被记录为一次成功写入）。
    if _lose_lease["v"] or _merge_lost["v"]:
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


async def _fake_merge(token, updates, *, session_factory=None) -> None:  # noqa: ANN001
    _merge_calls.append({"token": token, "updates": dict(updates)})
    if _merge_lost["v"]:
        raise rt.JobLeaseLostError("fake lost lease during merge")


class _JR:
    """Minimal SchedulerJobRun stand-in with the ownership fields C2B needs."""

    id = "fake-jr"
    status = "running"
    worker_instance_id = "strategy-owner"
    lease_epoch = 1
    lease_expires_at = None
    metadata_json = None


def _reset_fakes() -> None:
    _heartbeats.clear()
    _finalize_calls.clear()
    _finalize_return[0] = True
    _merge_calls.clear()
    _unfenced_calls.clear()
    _lose_lease["v"] = False
    _merge_lost["v"] = False


def _assert_no_unfenced() -> None:
    # Strategy must never fall back to the unfenced _finish_job_run helper.
    assert _unfenced_calls == [], f"unfenced finish_job_run was called: {_unfenced_calls}"


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

    _reset_fakes()
    # C2B fenced primitives are faked at the module level so the runtime consumes
    # them exactly as it would the production primitives.
    monkeypatch.setattr(rt, "FencedJobToken", _FakeToken)
    monkeypatch.setattr(rt, "FencedJobHeartbeat", FakeFencedHeartbeat)
    monkeypatch.setattr(rt, "JobLeaseLostError", _FakeJobLeaseLostError)
    monkeypatch.setattr(rt, "finalize_job_run", _fake_finalize)
    monkeypatch.setattr(rt, "merge_owned_job_run_metadata", _fake_merge)

    fake = FakeScheduler()
    monkeypatch.setattr(rt, "AsyncIOScheduler", lambda: fake)

    async def _fake_is_trading(session, trade_date):  # noqa: ANN001
        return is_trading

    monkeypatch.setattr(
        "app.services.calendar_service.is_trading_day_async", _fake_is_trading
    )

    async def _fake_create_job_run(db, name, biz, *, scheduled_at, run_key):  # noqa: ANN001
        rec["create_job_run_calls"].append((name, biz, run_key))
        return None if create_job_run_returns_none else _JR()

    async def _fake_finish_job_run(db, job_run, status, **kw):  # noqa: ANN001
        rec["finish_job_run_calls"].append((status, kw))
        _unfenced_calls.append((status, kw))

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
    # final-status mapping preserved (partial_failed is now a first-class terminal)
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
    # [C2B] terminal + metadata go through the fenced primitives, not _finish_job_run
    assert "FencedJobToken(" in src
    assert "FencedJobHeartbeat(" in src
    assert "interval_seconds=30.0" in src
    assert "merge_owned_job_run_metadata(" in src
    assert "finalize_job_run(" in src
    assert "heartbeat.ensure_owned()" in src
    assert "JobLeaseLostError" in src
    # the old selector-count heartbeat must be gone
    assert "if idx % 5 == 4:" not in src
    assert "update_job_heartbeat(db, job_run)" not in src


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


def test_strategy_fenced_success_terminal(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    fake, rec = asyncio.run(
        _run_runtime(monkeypatch, selector_keys=["OTHER_A", "OTHER_B", "OTHER_C"], rec=rec)
    )
    asyncio.run(_job(fake, "strategy_run_daily")["func"]())

    assert _finalize_calls  # reached terminal
    assert len(_finalize_calls) == 1
    call = _finalize_calls[0]
    assert call["status"] == "succeeded"
    assert call["total_count"] == 3
    assert call["succeeded_count"] == 3
    assert call["failed_count"] == 0
    # token carries the created job_run ownership fields
    token = call["token"]
    assert token.job_run_id == "fake-jr"
    assert token.worker_instance_id == "strategy-owner"
    assert token.lease_epoch == 1
    assert token.lease_seconds == 120
    # mid-loop metadata merges were fenced (one per non-DSA selector)
    assert len(_merge_calls) == 3
    # heartbeat lifecycle: 30s, started before business, stopped in finally
    assert len(_heartbeats) == 1
    assert _heartbeats[0].interval_seconds == 30.0
    assert _heartbeats[0].started is True
    assert _heartbeats[0].stopped is True
    _assert_no_unfenced()


def test_strategy_fenced_partial_failed_terminal(monkeypatch) -> None:  # noqa: ANN001
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

    assert len(_finalize_calls) == 1
    call = _finalize_calls[0]
    # 部分成功、部分失败 → partial_failed（一等终态，业务语义不变）
    assert call["status"] == "partial_failed"
    assert call["total_count"] == 3
    assert call["succeeded_count"] == 2
    assert call["failed_count"] == 1
    # DSA 分支仍 merge 一次；OTHER_B 成功 merge 一次；OTHER_A 失败不 merge
    assert len(_merge_calls) == 2
    assert _heartbeats[0].stopped is True
    _assert_no_unfenced()


def test_strategy_fenced_all_failed_terminal(monkeypatch) -> None:  # noqa: ANN001
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

    assert len(_finalize_calls) == 1
    call = _finalize_calls[0]
    assert call["status"] == "failed"
    assert call["succeeded_count"] == 0
    assert call["failed_count"] == 2
    # 全部失败 → 没有任何 fenced metadata merge
    assert _merge_calls == []
    assert _heartbeats[0].stopped is True
    _assert_no_unfenced()


def test_strategy_no_selectors_fenced_failed_terminal(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    fake, rec = asyncio.run(_run_runtime(monkeypatch, selector_keys=[], rec=rec))
    asyncio.run(_job(fake, "strategy_run_daily")["func"]())

    # 无 selector 分支也必须走 fenced failed terminal，不留最后一条 unfenced terminal
    assert len(_finalize_calls) == 1
    call = _finalize_calls[0]
    assert call["status"] == "failed"
    assert call["error_message"] is not None and "未找到 kind=selector" in call["error_message"]
    assert _merge_calls == []
    assert _heartbeats[0].stopped is True
    _assert_no_unfenced()


def test_strategy_uses_time_based_fenced_heartbeat(monkeypatch) -> None:  # noqa: ANN001
    rec: dict = {}
    fake, rec = asyncio.run(
        _run_runtime(monkeypatch, selector_keys=["K1", "K2", "K3", "K4", "K5"], rec=rec)
    )
    asyncio.run(_job(fake, "strategy_run_daily")["func"]())

    # C2B 退休「每 5 个 selector 才 heartbeat」的错设计：
    # 全程只有一个 30s FencedJobHeartbeat，且旧 update_job_heartbeat 不再被调用。
    assert len(_heartbeats) == 1
    assert _heartbeats[0].interval_seconds == 30.0
    assert _heartbeats[0].started is True
    assert _heartbeats[0].stopped is True
    assert rec["update_heartbeat_calls"] == 0
    assert _finalize_calls[-1]["status"] == "succeeded"
    _assert_no_unfenced()


def test_strategy_lease_lost_during_loop_no_terminal(monkeypatch) -> None:  # noqa: ANN001
    # 模拟循环内 ensure_owned 检测到 ownership 已转移：后续 selector 必须尽快停止，
    # 且不得写任何 terminal（fenced 或 unfenced）。
    fake, _ = asyncio.run(_run_runtime(monkeypatch, selector_keys=["OTHER_A", "OTHER_B", "OTHER_C"]))
    _lose_lease["v"] = True
    asyncio.run(_job(fake, "strategy_run_daily")["func"]())

    assert _finalize_calls == []
    assert _merge_calls == []
    _assert_no_unfenced()
    assert len(_heartbeats) == 1
    assert _heartbeats[0].stopped is True


def test_strategy_metadata_merge_lease_lost_no_terminal(monkeypatch) -> None:  # noqa: ANN001
    # 仅 metadata merge 阶段丢失 ownership：merge 抛 JobLeaseLostError → 停止循环，
    # 不得再用 stale worker 覆盖 terminal。
    fake, _ = asyncio.run(_run_runtime(monkeypatch, selector_keys=["OTHER_A", "OTHER_B"]))
    _merge_lost["v"] = True
    asyncio.run(_job(fake, "strategy_run_daily")["func"]())

    assert _finalize_calls == []
    # merge 被拒绝：要么未触达（ensure_owned 先失），要么触发了一次失败 merge 尝试；
    # 关键断言是「没有 stale terminal / 没有 unfenced terminal」。
    assert len(_heartbeats) == 1
    assert _heartbeats[0].stopped is True
    _assert_no_unfenced()


def test_strategy_finalize_race_lost_ownership_no_double_write(monkeypatch) -> None:  # noqa: ANN001
    # ensure_owned 成功但 finalize 返回 False（两次检查之间 ownership 被抢占）：
    # runtime 不得重试/双写。
    _finalize_return[0] = False
    fake, _ = asyncio.run(_run_runtime(monkeypatch, selector_keys=["OTHER_A", "OTHER_B"]))
    asyncio.run(_job(fake, "strategy_run_daily")["func"]())

    assert len(_finalize_calls) == 1
    assert _finalize_calls[0]["status"] == "succeeded"
    _assert_no_unfenced()
    assert _heartbeats[0].stopped is True
