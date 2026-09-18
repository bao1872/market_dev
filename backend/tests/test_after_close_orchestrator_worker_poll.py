# PURE_UNIT_TEST=1
"""[2026-08-11 W5B] AfterClose claim/poll owner 行为合同测试（纯单元测试，不连数据库）。

针对 `app.services.after_close_orchestrator_worker_poll.poll_after_close_once` 的
7 个行为合同 + 1 个 SQL 结构合同 + 1 个 worker 薄 façade 合同。

本文件用可控 FakeSession 驱动真实 owner 函数，验证：
  1. no-job            → rollback + False
  2. queued claim      → running / worker / heartbeat / lease(+_ORCHESTRATOR_LEASE_SECONDS) / epoch+1 / commit
  3. existing started_at → 不覆盖
  4. resume_queued     → 正常 claim，保持原状态语义
  5. missing trade_date → failed + finished + lease 释放 + ERROR 事件 + True
  6. execute args      → job_run_id / trade_date(date) / worker_id / 新 lease_epoch 精确
  7. execute raises    → 不传播，仍 True
  S. SQL 结构          → queued+resume_queued / order_by(created_at) / limit(1) / FOR UPDATE SKIP LOCKED
  F. worker façade     → _after_close_poll_once 非常薄（无领取逻辑符号）

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_after_close_orchestrator_worker_poll.py -v
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import postgresql

from app.services.after_close_orchestrator import _ORCHESTRATOR_LEASE_SECONDS
from app.services.after_close_orchestrator_worker_poll import poll_after_close_once


class _FakeJobRun:
    """最小 SchedulerJobRun 替身：只暴露 poll owner 读取/写入的字段。"""

    def __init__(self, **kw: object) -> None:
        self.job_name = "after_close_orchestrator"
        self.status = "queued"
        self.created_at = datetime(2026, 9, 17, 15, 0, 0)
        self.id = "job-1"
        self.started_at = None
        self.heartbeat_at = None
        self.lease_expires_at = None
        self.lease_epoch = 5
        self.attempt_no = 0
        self.metadata_json = '{"trade_date": "2026-09-17"}'
        self.worker_instance_id = None
        self.error_message = None
        self.finished_at = None
        self.__dict__.update(kw)


class _FakePollSession:
    """最小 AsyncSession 替身：execute 返回预置 job_run，get 返回预置 fetch。"""

    def __init__(self, *, claimed: _FakeJobRun | None = None, fetch: _FakeJobRun | None = None) -> None:
        self._claimed = claimed
        self._fetch = fetch
        self.commits = 0
        self.rollbacks = 0
        self.added: list[object] = []
        self.executed: list[object] = []

    async def __aenter__(self) -> _FakePollSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, stmt: object):
        self.executed.append(stmt)
        result = MagicMock()
        result.scalar_one_or_none = lambda: self._claimed
        return result

    async def get(self, *args: object, **kwargs: object):
        return self._fetch

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    def add(self, obj: object) -> None:
        self.added.append(obj)


class _FakeSessionFactory:
    def __init__(self, *, claimed: _FakeJobRun | None = None, fetch: _FakeJobRun | None = None) -> None:
        self.claimed = claimed
        self.fetch = fetch
        self.sessions: list[_FakePollSession] = []

    def __call__(self) -> _FakePollSession:
        s = _FakePollSession(claimed=self.claimed, fetch=self.fetch)
        self.sessions.append(s)
        return s


# =============================================================================
# 1. no-job → rollback + False
# =============================================================================


@pytest.mark.asyncio
async def test_poll_no_job_returns_false_with_rollback() -> None:
    factory = _FakeSessionFactory(claimed=None)
    execute = AsyncMock()
    with patch("app.services.after_close_orchestrator.execute_after_close_run", new=execute):
        result = await poll_after_close_once(
            session_factory=factory,
            worker_instance_id="w1",
            logger=MagicMock(),
        )
    assert result is False
    assert factory.sessions[0].rollbacks == 1
    assert factory.sessions[0].commits == 0
    execute.assert_not_called()


# =============================================================================
# 2. queued claim → running / worker / heartbeat / lease / epoch+1 / commit
# =============================================================================


@pytest.mark.asyncio
async def test_poll_queued_claim_updates_and_commits() -> None:
    job = _FakeJobRun(status="queued", lease_epoch=5, started_at=None)
    factory = _FakeSessionFactory(claimed=job)
    execute = AsyncMock()
    with patch("app.services.after_close_orchestrator.execute_after_close_run", new=execute):
        result = await poll_after_close_once(
            session_factory=factory, worker_instance_id="w1", logger=MagicMock(),
        )
    assert result is True
    assert job.status == "running"
    assert job.worker_instance_id == "w1"
    assert job.started_at is not None
    assert job.heartbeat_at is not None
    # lease 增量必须使用 _ORCHESTRATOR_LEASE_SECONDS
    assert job.lease_expires_at - job.heartbeat_at == timedelta(seconds=_ORCHESTRATOR_LEASE_SECONDS)
    assert job.lease_epoch == 6  # [JOB-02] fencing +1
    assert factory.sessions[0].commits >= 1
    execute.assert_awaited_once()


# =============================================================================
# 3. existing started_at → 不覆盖
# =============================================================================


@pytest.mark.asyncio
async def test_poll_existing_started_at_not_overwritten() -> None:
    existing = datetime(2026, 9, 16, 21, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    job = _FakeJobRun(status="queued", started_at=existing, lease_epoch=3)
    factory = _FakeSessionFactory(claimed=job)
    execute = AsyncMock()
    with patch("app.services.after_close_orchestrator.execute_after_close_run", new=execute):
        result = await poll_after_close_once(
            session_factory=factory, worker_instance_id="w1", logger=MagicMock(),
        )
    assert result is True
    assert job.started_at is existing  # 仅原值为 None 时才写
    assert job.status == "running"
    assert job.lease_epoch == 4


# =============================================================================
# 4. resume_queued → 正常 claim，保持原状态语义
# =============================================================================


@pytest.mark.asyncio
async def test_poll_resume_queued_claims() -> None:
    job = _FakeJobRun(status="resume_queued", attempt_no=2, lease_epoch=7)
    factory = _FakeSessionFactory(claimed=job)
    execute = AsyncMock()
    with patch("app.services.after_close_orchestrator.execute_after_close_run", new=execute):
        result = await poll_after_close_once(
            session_factory=factory, worker_instance_id="w1", logger=MagicMock(),
        )
    assert result is True
    assert job.status == "running"  # 领取后统一为 running
    assert job.lease_epoch == 8
    execute.assert_awaited_once()


# =============================================================================
# 5. missing trade_date → failed + finished + lease 释放 + ERROR 事件 + True
# =============================================================================


@pytest.mark.asyncio
async def test_poll_missing_trade_date_marks_failed_with_event() -> None:
    job = _FakeJobRun(status="queued", metadata_json="{}")  # 无 trade_date
    factory = _FakeSessionFactory(claimed=job, fetch=job)
    execute = AsyncMock()
    with patch("app.services.after_close_orchestrator.execute_after_close_run", new=execute):
        result = await poll_after_close_once(
            session_factory=factory, worker_instance_id="w1", logger=MagicMock(),
        )
    assert result is True
    # 第二 session 标记 failed + 写事件
    assert job.status == "failed"
    assert job.finished_at is not None
    assert job.lease_expires_at is not None  # 释放 run_key
    assert job.error_message == "任务缺少 trade_date，无法执行盘后流水线"
    assert factory.sessions[1].added, "应写 JobRunEvent"
    event = factory.sessions[1].added[0]
    assert event.step == "claim"
    assert event.level == "ERROR"
    assert event.message == "任务缺少 trade_date，无法执行盘后流水线"
    assert event.payload["reason"] == "missing_trade_date"
    assert event.payload["orchestrator_status"] == "failed"
    execute.assert_not_called()


# =============================================================================
# 6. execute args → job_run_id / trade_date(date) / worker_id / 新 lease_epoch 精确
# =============================================================================


@pytest.mark.asyncio
async def test_poll_execute_called_with_exact_args() -> None:
    job = _FakeJobRun(status="queued", lease_epoch=9, metadata_json='{"trade_date": "2026-09-17"}')
    factory = _FakeSessionFactory(claimed=job)
    execute = AsyncMock()
    with patch("app.services.after_close_orchestrator.execute_after_close_run", new=execute):
        result = await poll_after_close_once(
            session_factory=factory, worker_instance_id="w1", logger=MagicMock(),
        )
    assert result is True
    assert execute.await_count == 1
    kwargs = execute.call_args.kwargs
    assert kwargs["job_run_id"] == "job-1"
    assert kwargs["trade_date"] == date(2026, 9, 17)
    assert isinstance(kwargs["trade_date"], date)
    assert kwargs["worker_id"] == "w1"
    assert kwargs["lease_epoch"] == 10  # 领取后的新 epoch（不是领取前）


# =============================================================================
# 7. execute raises → 不传播，仍 True
# =============================================================================


@pytest.mark.asyncio
async def test_poll_execute_exception_not_propagated() -> None:
    job = _FakeJobRun(status="queued", lease_epoch=4, metadata_json='{"trade_date": "2026-09-17"}')
    factory = _FakeSessionFactory(claimed=job)
    execute = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.services.after_close_orchestrator.execute_after_close_run", new=execute):
        # poll owner 只记录不 re-raise，worker process 不因单个 job 崩掉
        result = await poll_after_close_once(
            session_factory=factory, worker_instance_id="w1", logger=MagicMock(),
        )
    assert result is True
    execute.assert_awaited_once()


# =============================================================================
# S. SQL 结构 → queued+resume_queued / order_by(created_at) / limit(1) / FOR UPDATE SKIP LOCKED
# =============================================================================


@pytest.mark.asyncio
async def test_poll_claim_sql_structure() -> None:
    job = _FakeJobRun(status="queued")
    factory = _FakeSessionFactory(claimed=job)
    execute = AsyncMock()
    with patch("app.services.after_close_orchestrator.execute_after_close_run", new=execute):
        await poll_after_close_once(
            session_factory=factory, worker_instance_id="w1", logger=MagicMock(),
        )
    stmt = factory.sessions[0].executed[0]
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "LIMIT 1" in sql or "limit 1" in sql
    assert "ORDER BY" in sql and "created_at" in sql
    assert "after_close_orchestrator" in sql
    assert "'queued'" in sql and "'resume_queued'" in sql
    # lease 使用 _ORCHESTRATOR_LEASE_SECONDS
    assert job.lease_expires_at - job.heartbeat_at == timedelta(seconds=_ORCHESTRATOR_LEASE_SECONDS)


# =============================================================================
# F. worker 薄 façade → 不含领取逻辑符号
# =============================================================================


def test_after_close_poll_facade_is_thin() -> None:
    import app.worker as worker_mod

    src = worker_mod._after_close_poll_once.__doc__ or ""
    banned_tokens = (
        "with_for_update",
        "skip_locked",
        "lease_epoch",
        "resume_queued",
        "metadata_json",
        "JobRunEvent",
        "execute_after_close_run",
        "select(",
    )
    # 仅检查文档字符串不会诱导读者认为 façade 仍含领取逻辑；
    # 真正的代码层检查在下方 inspect.getsource 中执行。
    for token in banned_tokens:
        assert token not in src, f"façade 文档不应枚举领取逻辑符号: {token}"

    import inspect

    full_src = inspect.getsource(worker_mod._after_close_poll_once)
    for token in banned_tokens:
        assert token not in full_src, f"façade 不得包含领取逻辑符号: {token}"
    assert "poll_after_close_once" in full_src
    assert "session_factory=AsyncSessionLocal" in full_src
    assert "worker_instance_id=_WORKER_INSTANCE_ID" in full_src
    assert "logger=logger" in full_src
