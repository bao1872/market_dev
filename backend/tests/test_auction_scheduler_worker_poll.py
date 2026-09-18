# PURE_UNIT_TEST=1
"""[P0-3 / W6A] Auction Scheduler poll owner 单元测试（不连接数据库）。

直接覆盖 app.services.auction_scheduler_worker_poll.poll_auction_scheduler_once。
14 个用例对齐 W6A 冻结合同：

1. 非交易日仍继续 claim queued job（冻结合同 6）
2. trading-day 检查异常仍继续 claim（冻结合同 2）
3. final 触发 → create auction_final + commit（冻结合同 4/5）
4. open 触发 → create auction_open_confirmation + commit（冻结合同 5）
5. 重复 is_new=False 仍 commit 但不打印“创建”日志（冻结合同 4）
6. 无 queued job → rollback + False（冻结合同 8）
7. claim → running/worker/started_at/heartbeat/(epoch or 0)+1/commit（冻结合同 7）
8. claim 不修改 lease_expires_at（冻结合同 9，关键）
9. missing trade_date → failed / lease release / 精确错误 / True（冻结合同 12）
10. final dispatch 精确 args（冻结合同 14）
11. open-confirmation dispatch 精确 args（冻结合同 14）
12. unknown job → failed / 精确错误 / True（冻结合同 15）
13. executor 异常 → 吞掉 + True（冻结合同 16）
14. worker façade 薄（仅依赖注入后委托）

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_auction_scheduler_worker_poll.py -v
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.services.auction_scheduler_service import (
    AUCTION_FINAL_JOB_NAME,
    AUCTION_OPEN_CONFIRMATION_JOB_NAME,
)

_TZ = ZoneInfo("Asia/Shanghai")


class _FakeAsyncSession:
    """模拟 AsyncSession 上下文管理器（纯单元测试用，最小可用）。"""

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False
        self._objects: dict = {}

    async def __aenter__(self) -> _FakeAsyncSession:
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def get(self, model, id_):
        return self._objects.get(id_)

    def add(self, obj) -> None:
        pass


def _build_fake_session_local(sessions: list[_FakeAsyncSession]):
    """构造 fake session_factory：每次调用取一个 session（超出返回空 session）。"""

    class _Factory:
        def __init__(self) -> None:
            self._sessions = sessions
            self._idx = 0

        def __call__(self) -> _FakeAsyncSession:
            if self._idx >= len(self._sessions):
                return _FakeAsyncSession()
            s = self._sessions[self._idx]
            self._idx += 1
            return s

    return _Factory()


def _make_job_run(
    *,
    job_name: str = AUCTION_FINAL_JOB_NAME,
    trade_date: date = date(2026, 7, 31),
    status: str = "queued",
    lease_epoch: int = 0,
    lease_expires_at=None,
    metadata: dict | None = None,
) -> MagicMock:
    """构造 SchedulerJobRun mock。"""
    meta = metadata if metadata is not None else {"trade_date": trade_date.isoformat()}
    job = MagicMock()
    job.id = __import__("uuid").uuid4()
    job.job_name = job_name
    job.status = status
    job.lease_epoch = lease_epoch
    job.lease_expires_at = lease_expires_at
    job.started_at = None
    job.heartbeat_at = None
    job.metadata_json = __import__("json").dumps(meta)
    job.worker_instance_id = None
    return job


@contextmanager
def _fixed_now(fixed_now: datetime):
    """patch 新 owner 模块的 datetime，使 now() 返回固定时间。"""
    with patch("app.services.auction_scheduler_worker_poll.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        # 允许 owner 内 datetime(...) 构造（date_cls.fromisoformat 走真实 date）
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        yield mock_dt


# =============================================================================
# 1. 非交易日仍继续 claim queued job（冻结合同 6）
# =============================================================================


@pytest.mark.asyncio
async def test_non_trading_day_still_claims_queued_job() -> None:
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    fake_job = _make_job_run()
    check_session = _FakeAsyncSession()
    claim_session = _FakeAsyncSession()
    session_factory = _build_fake_session_local([check_session, claim_session])
    logger = MagicMock()

    with _fixed_now(datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)), \
         patch("app.services.calendar_service.is_trading_day_async", new=AsyncMock(return_value=False)), \
         patch("app.services.auction_scheduler_service.get_queued_auction_job", new=AsyncMock(return_value=fake_job)), \
         patch("app.services.auction_scheduler_service.execute_auction_scan_run", new=AsyncMock(return_value={"status": "succeeded"})):
        result = await poll_auction_scheduler_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is True
    assert fake_job.status == "running"
    assert fake_job.worker_instance_id == "w1"
    assert claim_session.committed is True


# =============================================================================
# 2. trading-day 检查异常仍继续 claim（冻结合同 2）
# =============================================================================


@pytest.mark.asyncio
async def test_trading_day_check_exception_still_claims() -> None:
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    fake_job = _make_job_run()
    claim_session = _FakeAsyncSession()
    session_factory = _build_fake_session_local([_FakeAsyncSession(), claim_session])
    logger = MagicMock()

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    with _fixed_now(datetime(2026, 7, 31, 9, 25, 5, tzinfo=_TZ)), \
         patch("app.services.calendar_service.is_trading_day_async", new=_boom), \
         patch("app.services.auction_scheduler_service.get_queued_auction_job", new=AsyncMock(return_value=fake_job)), \
         patch("app.services.auction_scheduler_service.execute_auction_scan_run", new=AsyncMock(return_value={"status": "succeeded"})):
        result = await poll_auction_scheduler_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is True
    assert fake_job.status == "running"
    # 异常被记录但不上抛
    logger.exception.assert_called()


# =============================================================================
# 3. final 触发 → create auction_final + commit（冻结合同 4/5）
# =============================================================================


@pytest.mark.asyncio
async def test_final_window_creates_final_job_and_commits() -> None:
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    created: dict = {}

    async def _track_create(*a, **k):
        created["called"] = True
        created["kwargs"] = k
        return (MagicMock(), True)

    create_session = _FakeAsyncSession()
    session_factory = _build_fake_session_local([_FakeAsyncSession(), create_session])
    logger = MagicMock()

    with _fixed_now(datetime(2026, 7, 31, 9, 25, 5, tzinfo=_TZ)), \
         patch("app.services.calendar_service.is_trading_day_async", new=AsyncMock(return_value=True)), \
         patch("app.services.auction_scheduler_service.create_auction_final_job", new=_track_create), \
         patch("app.services.auction_scheduler_service.get_queued_auction_job", new=AsyncMock(return_value=None)):
        result = await poll_auction_scheduler_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is False  # 无 queued job
    assert created.get("called") is True
    assert create_session.committed is True
    assert created["kwargs"].get("worker_instance_id") == "w1"


# =============================================================================
# 4. open 触发 → create auction_open_confirmation + commit（冻结合同 5）
# =============================================================================


@pytest.mark.asyncio
async def test_open_window_creates_open_confirmation_job_and_commits() -> None:
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    created: dict = {}

    async def _track_create(*a, **k):
        created["called"] = True
        return (MagicMock(), True)

    create_session = _FakeAsyncSession()
    session_factory = _build_fake_session_local([_FakeAsyncSession(), create_session])
    logger = MagicMock()

    with _fixed_now(datetime(2026, 7, 31, 10, 0, 0, tzinfo=_TZ)), \
         patch("app.services.calendar_service.is_trading_day_async", new=AsyncMock(return_value=True)), \
         patch("app.services.auction_scheduler_service.create_auction_open_confirmation_job", new=_track_create), \
         patch("app.services.auction_scheduler_service.get_queued_auction_job", new=AsyncMock(return_value=None)):
        result = await poll_auction_scheduler_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is False
    assert created.get("called") is True
    assert create_session.committed is True


# =============================================================================
# 5. 重复 is_new=False 仍 commit 但不打印“创建”日志（冻结合同 4）
# =============================================================================


@pytest.mark.asyncio
async def test_duplicate_is_new_false_commits_without_create_log() -> None:
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    created: dict = {}

    async def _track_create(*a, **k):
        created["called"] = True
        return (MagicMock(), False)  # is_new=False

    create_session = _FakeAsyncSession()
    session_factory = _build_fake_session_local([_FakeAsyncSession(), create_session])
    logger = MagicMock()

    with _fixed_now(datetime(2026, 7, 31, 9, 25, 10, tzinfo=_TZ)), \
         patch("app.services.calendar_service.is_trading_day_async", new=AsyncMock(return_value=True)), \
         patch("app.services.auction_scheduler_service.create_auction_final_job", new=_track_create), \
         patch("app.services.auction_scheduler_service.get_queued_auction_job", new=AsyncMock(return_value=None)):
        result = await poll_auction_scheduler_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is False
    assert created.get("called") is True
    assert create_session.committed is True
    # is_new=False → 不应打印“创建 auction_final job”日志
    create_logs = [
        c.args[0]
        for c in logger.info.call_args_list
        if c.args and "创建 auction_final job" in c.args[0]
    ]
    assert create_logs == [], "is_new=False 不应打印创建日志"


# =============================================================================
# 6. 无 queued job → rollback + False（冻结合同 8）
# =============================================================================


@pytest.mark.asyncio
async def test_no_queued_job_rolls_back_and_returns_false() -> None:
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    claim_session = _FakeAsyncSession()
    session_factory = _build_fake_session_local([_FakeAsyncSession(), claim_session])
    logger = MagicMock()

    with _fixed_now(datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)), \
         patch("app.services.calendar_service.is_trading_day_async", new=AsyncMock(return_value=False)), \
         patch("app.services.auction_scheduler_service.get_queued_auction_job", new=AsyncMock(return_value=None)):
        result = await poll_auction_scheduler_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is False
    assert claim_session.rolled_back is True
    assert claim_session.committed is False


# =============================================================================
# 7. claim → running/worker/started_at/heartbeat/(epoch or 0)+1/commit（冻结合同 7）
# =============================================================================


@pytest.mark.asyncio
async def test_claim_sets_running_worker_started_heartbeat_epoch() -> None:
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    fake_job = _make_job_run(lease_epoch=0)
    claim_session = _FakeAsyncSession()
    session_factory = _build_fake_session_local([_FakeAsyncSession(), claim_session])
    logger = MagicMock()

    with _fixed_now(datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)), \
         patch("app.services.calendar_service.is_trading_day_async", new=AsyncMock(return_value=False)), \
         patch("app.services.auction_scheduler_service.get_queued_auction_job", new=AsyncMock(return_value=fake_job)), \
         patch("app.services.auction_scheduler_service.execute_auction_scan_run", new=AsyncMock(return_value={"status": "succeeded"})):
        result = await poll_auction_scheduler_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is True
    assert fake_job.status == "running"
    assert fake_job.worker_instance_id == "w1"
    assert fake_job.started_at is not None
    assert fake_job.heartbeat_at is not None
    assert fake_job.lease_epoch == 1  # (0 or 0) + 1
    assert claim_session.committed is True


# =============================================================================
# 8. claim 不修改 lease_expires_at（冻结合同 9，关键）
# =============================================================================


@pytest.mark.asyncio
async def test_claim_does_not_touch_lease_expires_at() -> None:
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    sentinel = object()
    fake_job = _make_job_run(lease_epoch=2, lease_expires_at=sentinel)
    claim_session = _FakeAsyncSession()
    session_factory = _build_fake_session_local([_FakeAsyncSession(), claim_session])
    logger = MagicMock()

    with _fixed_now(datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)), \
         patch("app.services.calendar_service.is_trading_day_async", new=AsyncMock(return_value=False)), \
         patch("app.services.auction_scheduler_service.get_queued_auction_job", new=AsyncMock(return_value=fake_job)), \
         patch("app.services.auction_scheduler_service.execute_auction_scan_run", new=AsyncMock(return_value={"status": "succeeded"})):
        result = await poll_auction_scheduler_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is True
    assert fake_job.lease_expires_at is sentinel, "claim 不应修改 lease_expires_at"
    assert fake_job.lease_epoch == 3  # (2 or 0) + 1


# =============================================================================
# 9. missing trade_date → failed / lease release / 精确错误 / True（冻结合同 12）
# =============================================================================


@pytest.mark.asyncio
async def test_missing_trade_date_marks_failed() -> None:
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    fake_job = _make_job_run(metadata={"auction_type": "final"})  # 无 trade_date
    failed_jr = MagicMock()
    fail_session = _FakeAsyncSession()
    fail_session._objects[fake_job.id] = failed_jr
    session_factory = _build_fake_session_local(
        [_FakeAsyncSession(), _FakeAsyncSession(), fail_session]
    )
    logger = MagicMock()

    with _fixed_now(datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)), \
         patch("app.services.calendar_service.is_trading_day_async", new=AsyncMock(return_value=False)), \
         patch("app.services.auction_scheduler_service.get_queued_auction_job", new=AsyncMock(return_value=fake_job)):
        result = await poll_auction_scheduler_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is True
    assert failed_jr.status == "failed"
    assert failed_jr.error_message == "任务缺少 trade_date"
    assert failed_jr.lease_expires_at is not None


# =============================================================================
# 10. final dispatch 精确 args（冻结合同 14）
# =============================================================================


@pytest.mark.asyncio
async def test_final_dispatch_exact_args() -> None:
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    fake_job = _make_job_run(job_name=AUCTION_FINAL_JOB_NAME, lease_epoch=4)
    captured: dict = {}

    async def _track(*, job_run_id, trade_date, worker_id, lease_epoch, **kwargs):
        captured["job_run_id"] = job_run_id
        captured["trade_date"] = trade_date
        captured["worker_id"] = worker_id
        captured["lease_epoch"] = lease_epoch
        return {"status": "succeeded"}

    claim_session = _FakeAsyncSession()
    session_factory = _build_fake_session_local([_FakeAsyncSession(), claim_session])
    logger = MagicMock()

    with _fixed_now(datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)), \
         patch("app.services.calendar_service.is_trading_day_async", new=AsyncMock(return_value=False)), \
         patch("app.services.auction_scheduler_service.get_queued_auction_job", new=AsyncMock(return_value=fake_job)), \
         patch("app.services.auction_scheduler_service.execute_auction_scan_run", new=_track), \
         patch("app.services.auction_scheduler_service.execute_auction_open_confirmation_run", new=AsyncMock()):
        result = await poll_auction_scheduler_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is True
    assert captured["job_run_id"] == fake_job.id
    assert captured["trade_date"] == date(2026, 7, 31)
    assert captured["worker_id"] == "w1"
    assert captured["lease_epoch"] == 5  # (4 or 0) + 1


# =============================================================================
# 11. open-confirmation dispatch 精确 args（冻结合同 14）
# =============================================================================


@pytest.mark.asyncio
async def test_open_confirmation_dispatch_exact_args() -> None:
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    fake_job = _make_job_run(
        job_name=AUCTION_OPEN_CONFIRMATION_JOB_NAME, lease_epoch=4
    )
    captured: dict = {}

    async def _track(*, job_run_id, trade_date, worker_id, lease_epoch, **kwargs):
        captured["job_run_id"] = job_run_id
        captured["trade_date"] = trade_date
        captured["worker_id"] = worker_id
        captured["lease_epoch"] = lease_epoch
        return {"status": "succeeded"}

    claim_session = _FakeAsyncSession()
    session_factory = _build_fake_session_local([_FakeAsyncSession(), claim_session])
    logger = MagicMock()

    with _fixed_now(datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)), \
         patch("app.services.calendar_service.is_trading_day_async", new=AsyncMock(return_value=False)), \
         patch("app.services.auction_scheduler_service.get_queued_auction_job", new=AsyncMock(return_value=fake_job)), \
         patch("app.services.auction_scheduler_service.execute_auction_open_confirmation_run", new=_track), \
         patch("app.services.auction_scheduler_service.execute_auction_scan_run", new=AsyncMock()):
        result = await poll_auction_scheduler_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is True
    assert captured["job_run_id"] == fake_job.id
    assert captured["trade_date"] == date(2026, 7, 31)
    assert captured["worker_id"] == "w1"
    assert captured["lease_epoch"] == 5  # (4 or 0) + 1


# =============================================================================
# 12. unknown job → failed / 精确错误 / True（冻结合同 15）
# =============================================================================


@pytest.mark.asyncio
async def test_unknown_job_marks_failed() -> None:
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    fake_job = _make_job_run(job_name="weird_job")
    failed_jr = MagicMock()
    fail_session = _FakeAsyncSession()
    fail_session._objects[fake_job.id] = failed_jr
    session_factory = _build_fake_session_local(
        [_FakeAsyncSession(), _FakeAsyncSession(), fail_session]
    )
    logger = MagicMock()

    with _fixed_now(datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)), \
         patch("app.services.calendar_service.is_trading_day_async", new=AsyncMock(return_value=False)), \
         patch("app.services.auction_scheduler_service.get_queued_auction_job", new=AsyncMock(return_value=fake_job)):
        result = await poll_auction_scheduler_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is True
    assert failed_jr.status == "failed"
    assert failed_jr.error_message == "未知 job_name: weird_job"


# =============================================================================
# 13. executor 异常 → 吞掉 + True（冻结合同 16）
# =============================================================================


@pytest.mark.asyncio
async def test_executor_exception_swallowed() -> None:
    from app.services.auction_scheduler_worker_poll import poll_auction_scheduler_once

    fake_job = _make_job_run()
    claim_session = _FakeAsyncSession()
    session_factory = _build_fake_session_local([_FakeAsyncSession(), claim_session])
    logger = MagicMock()

    async def _boom(**kwargs):
        raise RuntimeError("scan failed")

    with _fixed_now(datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)), \
         patch("app.services.calendar_service.is_trading_day_async", new=AsyncMock(return_value=False)), \
         patch("app.services.auction_scheduler_service.get_queued_auction_job", new=AsyncMock(return_value=fake_job)), \
         patch("app.services.auction_scheduler_service.execute_auction_scan_run", new=_boom):
        result = await poll_auction_scheduler_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is True  # 不被异常中断
    logger.exception.assert_called()


# =============================================================================
# 14. worker façade 薄（仅依赖注入后委托）
# =============================================================================


@pytest.mark.asyncio
async def test_worker_facade_delegates_to_owner() -> None:
    import app.worker as worker_mod

    with patch(
        "app.services.auction_scheduler_worker_poll.poll_auction_scheduler_once",
        new=AsyncMock(return_value=True),
    ) as mock_owner:
        result = await worker_mod._auction_scheduler_poll_once()

    assert result is True
    mock_owner.assert_awaited_once()
    _, kwargs = mock_owner.call_args
    assert kwargs["session_factory"] is worker_mod.AsyncSessionLocal
    assert kwargs["worker_instance_id"] == worker_mod._WORKER_INSTANCE_ID
    assert kwargs["logger"] is worker_mod.logger
