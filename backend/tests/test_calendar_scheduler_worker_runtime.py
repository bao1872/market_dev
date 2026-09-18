"""PANJI-GOV-W1 + C2A: Calendar Scheduler runtime verification.

Structural contract tests verify the mechanical extraction (behavior unchanged)
and the C2A fenced-ownership migration (terminal state now flows through
``finalize_job_run`` carrying a ``FencedJobToken`` built from the created job's
ownership fields, with a 30s ``FencedJobHeartbeat`` kept alive across the two
year seeds).  Behavioral tests drive the real runtime with fakes and touch no
database.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid

import pytest

from app.services import calendar_scheduler_worker_runtime as rt
from app.services.fenced_job_run_service import JobLeaseLostError


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


class FakeJobRun:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.status = "running"
        self.worker_instance_id = "test-instance-id"
        self.lease_epoch = 7


_finalize_calls: list[dict] = []
_heartbeats: list[FakeFencedHeartbeat] = []
_unfenced_calls: list[tuple] = []
_lose_lease: dict[str, bool] = {"v": False}


class FakeFencedHeartbeat:
    def __init__(self, token, *, interval_seconds: float = 30.0, refresh=None) -> None:  # noqa: ANN001
        self.token = token
        self.interval_seconds = interval_seconds
        self.started = False
        self.stopped = False
        self._ensure_calls = 0
        self.lose_after = None
        _heartbeats.append(self)

    async def start(self) -> None:
        self.started = True

    def ensure_owned(self) -> None:
        self._ensure_calls += 1
        if _lose_lease["v"]:
            raise JobLeaseLostError("fake lost lease")
        if self.lose_after is not None and self._ensure_calls > self.lose_after:
            raise JobLeaseLostError("fake lost lease after threshold")

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
    return True


def _reset_fakes() -> None:
    _finalize_calls.clear()
    _heartbeats.clear()
    _unfenced_calls.clear()
    _lose_lease["v"] = False


def _assert_no_unfenced() -> None:
    assert _unfenced_calls == [], f"unfenced finish_job_run was called: {_unfenced_calls}"


def _job(fake: FakeScheduler, job_id: str) -> dict:
    for j in fake.jobs:
        if j["kwargs"].get("id") == job_id:
            return j
    raise AssertionError(f"job id not registered: {job_id}")


# --------------------------------------------------------------------------- #
# Runtime driver
# --------------------------------------------------------------------------- #
async def _run_runtime(
    monkeypatch,  # noqa: ANN001
    *,
    create_returns_none: bool = True,
    create_raises: bool = False,
    seed_raises: bool = False,
    calls: dict | None = None,
):
    calls = calls if calls is not None else {}
    _reset_fakes()
    monkeypatch.setattr(rt, "FencedJobHeartbeat", FakeFencedHeartbeat)
    monkeypatch.setattr(rt, "finalize_job_run", _fake_finalize)

    fake = FakeScheduler()
    monkeypatch.setattr(rt, "AsyncIOScheduler", lambda: fake)

    async def _fake_create_job_run(db, name, biz, *, scheduled_at, run_key):  # noqa: ANN001
        calls.setdefault("create_job_run", []).append((name, biz, run_key))
        if create_raises:
            raise RuntimeError("create boom")
        return None if create_returns_none else FakeJobRun()

    async def _fake_seed(session, year, force=False):  # noqa: ANN001
        calls.setdefault("seed_years", []).append(year)
        if seed_raises:
            raise RuntimeError("seed boom")
        return 5

    monkeypatch.setattr(
        "app.services.calendar_seed.seed_calendar_from_mootdx", _fake_seed
    )

    async def _fake_finish_job_run(db, job_run, status, **kw):  # noqa: ANN001
        _unfenced_calls.append((status, kw))

    async def _fake_recover(db):  # noqa: ANN001
        return 0

    async def _fake_heartbeat(name):  # noqa: ANN001
        return None

    await rt.run_calendar_scheduler_worker_runtime(
        session_factory=lambda: FakeSessionCM(),
        heartbeat_loop=_fake_heartbeat,
        should_shutdown=lambda: True,
        create_job_run=_fake_create_job_run,
        finish_job_run=_fake_finish_job_run,
        recover_stale_job_runs=_fake_recover,
        logger=logging.getLogger("test-calendar-rt"),
    )
    return fake, calls


# --------------------------------------------------------------------------- #
# Structural contract tests
# --------------------------------------------------------------------------- #
def test_runtime_function_importable() -> None:
    from app.services.calendar_scheduler_worker_runtime import (
        run_calendar_scheduler_worker_runtime,
    )

    assert callable(run_calendar_scheduler_worker_runtime)
    assert inspect.iscoroutinefunction(run_calendar_scheduler_worker_runtime)


def test_facade_delegates_to_runtime() -> None:
    from app.worker import run_calendar_scheduler_worker

    source = inspect.getsource(run_calendar_scheduler_worker)
    # 兼容 façade 不重新实现 lifecycle，只注入依赖并委托给 runtime。
    assert "run_calendar_scheduler_worker_runtime" in source
    assert "AsyncIOScheduler" not in source
    assert "CronTrigger" not in source


def test_runtime_preserves_calendar_contract() -> None:
    from app.services.calendar_scheduler_worker_runtime import (
        run_calendar_scheduler_worker_runtime,
    )

    source = inspect.getsource(run_calendar_scheduler_worker_runtime)
    # cron: 每日 02:00 Asia/Shanghai
    assert 'CronTrigger(hour=2, minute=0, timezone=ZoneInfo("Asia/Shanghai"))' in source
    # job identity 不变
    assert 'id="calendar_scheduler"' in source
    assert 'name="calendar_scheduler"' in source
    assert 'run_key=f"calendar_scheduler:{today}"' in source
    # 复用既有 job-run 状态规则（不重新实现第二套）
    assert "create_job_run(" in source
    assert "recover_stale_job_runs(" in source
    # [C2A] terminal 走 fenced primitive，不再调用 unfenced _finish_job_run
    assert "FencedJobToken(" in source
    assert "FencedJobHeartbeat(" in source
    assert "finalize_job_run(" in source
    assert "heartbeat.ensure_owned()" in source
    assert "heartbeat.start()" in source
    assert "heartbeat.stop()" in source
    # 调用既有业务 owner
    assert "seed_calendar_from_mootdx" in source
    assert "shanghai_business_date" in source


def test_facade_injects_canonical_helpers() -> None:
    from app.worker import run_calendar_scheduler_worker

    source = inspect.getsource(run_calendar_scheduler_worker)
    # 注入既有 canonical 协作者，不产生第二套状态 owner / service locator。
    assert "AsyncSessionLocal" in source
    assert "_heartbeat_loop" in source
    assert "_create_job_run" in source
    assert "_finish_job_run" in source
    assert "recover_stale_scheduler_job_runs" in source


def _line_index_of(source: str, token: str) -> int:
    for i, ln in enumerate(source.splitlines()):
        if token in ln:
            return i
    raise AssertionError(f"token not found in source: {token!r}")


def _indent_of(source: str, token: str) -> int:
    ln = source.splitlines()[_line_index_of(source, token)]
    return len(ln) - len(ln.lstrip())


def test_calendar_seed_import_after_duplicate_check() -> None:
    """PANJI-GOV-W1-R1 回归：calendar_seed import 必须晚于 duplicate 短路。

    duplicate 路径（job_run is None -> return）不得在加载 calendar_seed 依赖树
    之后才命中；且 calendar_seed import 必须位于 try/with 内，使其 import 异常仍由
    原有 except 路径处理。
    """
    from app.services.calendar_scheduler_worker_runtime import (
        run_calendar_scheduler_worker_runtime,
    )

    source = inspect.getsource(run_calendar_scheduler_worker_runtime)
    seed_token = "from app.services.calendar_seed import seed_calendar_from_mootdx"
    dup_token = "if job_run is None:"
    try_token = "try:"

    seed_idx = _line_index_of(source, seed_token)
    dup_idx = _line_index_of(source, dup_token)

    # 1) import 必须发生在 duplicate 检查之后（源码顺序）。
    assert seed_idx > dup_idx, (
        "calendar_seed import 必须位于 duplicate 检查之后，"
        "否则 duplicate short-circuit 会先于该 import 命中。"
    )
    # 2) import 必须嵌套在 try/with 内（缩进深于 try:），import 异常仍由 except 处理。
    assert _indent_of(source, seed_token) > _indent_of(source, try_token), (
        "calendar_seed import 必须位于 try 内，否则其 import 异常不在原有 except 路径中。"
    )


# --------------------------------------------------------------------------- #
# C2A fenced-ownership behavioral tests
# --------------------------------------------------------------------------- #
def test_calendar_fenced_success_terminal(monkeypatch) -> None:  # noqa: ANN001
    fake, calls = asyncio.run(_run_runtime(monkeypatch, create_returns_none=False))
    job = _job(fake, "calendar_scheduler")
    asyncio.run(job["func"]())

    # 两个年度都已 seed
    assert len(calls.get("seed_years", [])) == 2

    # 恰好一次 fenced succeeded terminal，token 携带 ownership fields
    assert len(_finalize_calls) == 1
    call = _finalize_calls[0]
    assert call["status"] == "succeeded"
    assert call["total_count"] == 1
    assert call["succeeded_count"] == 1
    assert call["failed_count"] == 0
    token = call["token"]
    assert token.worker_instance_id == "test-instance-id"
    assert token.lease_epoch == 7
    assert token.lease_seconds == 120
    # heartbeat: 启动于 seed 前，finally 中停止
    assert len(_heartbeats) == 1
    assert _heartbeats[0].started is True
    assert _heartbeats[0].interval_seconds == 30.0
    assert _heartbeats[0].stopped is True
    _assert_no_unfenced()


def test_calendar_fenced_failure_terminal(monkeypatch) -> None:  # noqa: ANN001
    fake, _ = asyncio.run(
        _run_runtime(monkeypatch, create_returns_none=False, seed_raises=True)
    )
    job = _job(fake, "calendar_scheduler")
    # seed 异常必须继续向 APScheduler 传播（恢复 W1 exception semantics）。
    with pytest.raises(RuntimeError, match="seed boom"):
        asyncio.run(job["func"]())

    # seed 异常 → 恰好一次 fenced failed terminal，带 error_message；且异常继续传播
    assert len(_finalize_calls) == 1
    call = _finalize_calls[0]
    assert call["status"] == "failed"
    assert call["error_message"] is not None and "seed boom" in call["error_message"]
    assert _heartbeats[0].stopped is True
    _assert_no_unfenced()


def test_calendar_create_failure_propagates(monkeypatch) -> None:  # noqa: ANN001
    # create_job_run 抛异常：尚未建立 owner token，不得安全 fenced terminal；
    # 原异常必须继续向 APScheduler 传播。
    fake, _ = asyncio.run(_run_runtime(monkeypatch, create_raises=True))
    job = _job(fake, "calendar_scheduler")
    with pytest.raises(RuntimeError, match="create boom"):
        asyncio.run(job["func"]())

    assert _finalize_calls == []
    assert _heartbeats == []
    _assert_no_unfenced()


def test_calendar_lease_lost_after_first_year_skips_terminal(monkeypatch) -> None:  # noqa: ANN001
    # 第一年 seed 后 lease 丢失：第二年不应继续，且不得写 terminal。
    fake, calls = asyncio.run(_run_runtime(monkeypatch, create_returns_none=False))
    _lose_lease["v"] = True
    job = _job(fake, "calendar_scheduler")
    asyncio.run(job["func"]())

    # 仅第一年 seed；第二年因 ownership 校验失败未继续
    assert calls.get("seed_years") == [_seed_first_year(calls)]
    assert _finalize_calls == []
    _assert_no_unfenced()
    assert _heartbeats[0].stopped is True


def _seed_first_year(calls: dict) -> int:
    years = calls.get("seed_years", [])
    assert len(years) == 1
    return years[0]


def test_calendar_finalize_lost_ownership_skips_double_write(monkeypatch) -> None:  # noqa: ANN001
    # ownership 在两年度之间校验通过，但 finalize 报告丢失（ensure 与 finalize 间
    # 的 race）：runtime 不得重试 / 双写。
    fake, _ = asyncio.run(_run_runtime(monkeypatch, create_returns_none=False))
    # 让 heartbeat 在两年度之间仍持有，但强制 finalize 返回 False
    job = _job(fake, "calendar_scheduler")
    monkeypatch.setattr(rt, "finalize_job_run", _fake_finalize_returning(False))
    asyncio.run(job["func"]())

    assert len(_finalize_calls) == 1
    assert _finalize_calls[0]["status"] == "succeeded"
    _assert_no_unfenced()
    assert _heartbeats[0].stopped is True


def _fake_finalize_returning(value: bool):  # noqa: ANN001
    async def _impl(
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
        return value

    return _impl
