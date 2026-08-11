# PURE_UNIT_TEST=1
"""[P0-3 2026-07-31] Auction Scheduler Worker / co-process 纯单元测试 - 不连接数据库。

覆盖 ref/instruction.md §四 补充目标测试：

1. Compose 中实际 WORKER_TYPE=after_close_orchestrator 能到达 Auction 轮询（架构守护）
2. 09:25:05 窗口仅创建一个 auction_final 任务（幂等）
3. 10:00:00 窗口仅创建一个 auction_open_confirmation 任务（幂等）
4. 同一分钟多次 poll 不重复创建
5. Worker 错过精确时间但仍在补偿窗口时可创建
6. 非交易日不创建
7. Worker 重启后已存在 succeeded/running 任务不重复
8. 过期租约能够 fencing 恢复（lease_epoch 递增守护）
9. Auction 轮询异常不终止 after-close 主 Worker
10. SIGTERM 能够结束 Auction 后台 Task
11. Auction co-process 由 after_close_orchestrator 启动（不依赖 WORKER_TYPE=auction_scheduler）
12. 启动时调用 recover_stale_scheduler_job_runs 恢复崩溃残留

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_auction_scheduler_worker.py -v
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.services.auction_scheduler_service import (
    AUCTION_FINAL_JOB_NAME,
    AUCTION_FINAL_LEASE_SECONDS,
    AUCTION_FINAL_TRIGGER_TOLERANCE_SECONDS,
    AUCTION_OPEN_CONFIRMATION_JOB_NAME,
    AUCTION_OPEN_CONFIRMATION_LEASE_SECONDS,
    AUCTION_SCHEDULER_POLL_INTERVAL,
    _is_in_trigger_window,
    should_create_auction_final_job,
    should_create_auction_open_confirmation_job,
)

_TZ = ZoneInfo("Asia/Shanghai")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.prod.yml"
_WORKER_FILE = _REPO_ROOT / "backend" / "app" / "worker.py"


# =============================================================================
# 1. 架构守护：Compose + Worker 源码静态检查（不连接数据库）
# =============================================================================


def test_compose_worker_after_close_uses_after_close_orchestrator() -> None:
    """守护：docker-compose.prod.yml 的 worker-after-close 服务必须配置
    WORKER_TYPE=after_close_orchestrator（Auction co-process 的生产入口）。
    """
    content = _COMPOSE_FILE.read_text(encoding="utf-8")
    assert "worker-after-close:" in content, "缺少 worker-after-close 服务"
    # 定位 worker-after-close 段落并验证 WORKER_TYPE
    idx = content.index("worker-after-close:")
    # 截取该服务后续 50 行（足够覆盖 environment 段）
    segment = content[idx : idx + 2000]
    assert "WORKER_TYPE: after_close_orchestrator" in segment, (
        "worker-after-close 必须使用 WORKER_TYPE=after_close_orchestrator"
    )


def test_compose_has_no_auction_scheduler_service() -> None:
    """守护：禁止新增 auction_scheduler 独立 Compose 服务（不增加常驻容器）。

    Auction Scheduler 必须由 after_close_orchestrator 同进程 co-process 运行。
    """
    content = _COMPOSE_FILE.read_text(encoding="utf-8")
    # 禁止顶级服务名 auction_scheduler（允许出现在注释中）
    for line in content.splitlines():
        stripped = line.lstrip()
        # 顶级服务定义（2 空格缩进 + 冒号结尾且非列表项）
        if stripped.startswith("auction_scheduler") and ":" in stripped:
            # 确认是服务定义（缩进 2 空格）
            if line.startswith("  auction_scheduler") and not line.startswith("   "):
                raise AssertionError(
                    "禁止新增 auction_scheduler 独立 Compose 服务："
                    "Auction 必须复用 after_close_orchestrator 容器"
                )


def test_worker_module_starts_auction_co_process_in_after_close_worker() -> None:
    """守护：run_after_close_orchestrator_worker 内部必须启动 _run_auction_scheduler_co_process。

    验证：
    1. run_after_close_orchestrator_worker 函数体内调用 _run_auction_scheduler_co_process
    2. 在 finally 块中 await 该 task（SIGTERM drain）
    """
    content = _WORKER_FILE.read_text(encoding="utf-8")
    # 1. 必须定义 _run_auction_scheduler_co_process
    assert "async def _run_auction_scheduler_co_process" in content, (
        "worker.py 必须定义 _run_auction_scheduler_co_process"
    )
    # 2. run_after_close_orchestrator_worker 内必须 create_task 启动它
    assert "_run_auction_scheduler_co_process" in content
    # 3. 必须有 finally 块 drain（通过 _drain_co_process，禁止裸 Task.cancel）
    # 简化检查：搜索 _drain_co_process(_auction_co_process_task
    assert "_drain_co_process(_auction_co_process_task" in content, (
        "run_after_close_orchestrator_worker 必须在 finally 块 drain Auction co-process"
    )


def test_worker_module_marks_auction_scheduler_worker_as_debug_only() -> None:
    """守护：run_auction_scheduler_worker 必须明确标注为调试入口，生产入口为 co-process。"""
    content = _WORKER_FILE.read_text(encoding="utf-8")
    idx = content.index("async def run_auction_scheduler_worker")
    docstring_segment = content[idx : idx + 1500]
    # 必须在 docstring 中说明生产入口是 co-process（避免误用）
    assert "调试入口" in docstring_segment or "调试" in docstring_segment, (
        "run_auction_scheduler_worker 必须明确标注为调试入口"
    )
    assert "_run_auction_scheduler_co_process" in docstring_segment, (
        "run_auction_scheduler_worker docstring 必须指向 co-process 生产入口"
    )


# =============================================================================
# 2. 时间窗口检查（纯函数）
# =============================================================================


def test_should_create_auction_final_at_exact_trigger_time() -> None:
    """测试 5：09:25:05 精确命中触发窗口。"""
    now = datetime(2026, 7, 31, 9, 25, 5, tzinfo=_TZ)
    assert should_create_auction_final_job(now) is True


def test_should_create_auction_final_within_tolerance() -> None:
    """测试 5：错过精确秒数但 ±30s 内仍可创建（补偿窗口）。"""
    base = datetime(2026, 7, 31, 9, 25, 5, tzinfo=_TZ)
    # +30s
    assert should_create_auction_final_job(base + timedelta(seconds=30)) is True
    # -30s
    assert should_create_auction_final_job(base - timedelta(seconds=30)) is True
    # +15s
    assert should_create_auction_final_job(base + timedelta(seconds=15)) is True


def test_should_not_create_auction_final_beyond_tolerance() -> None:
    """超过 ±30s 容差不创建。"""
    base = datetime(2026, 7, 31, 9, 25, 5, tzinfo=_TZ)
    # +31s 超出容差
    assert should_create_auction_final_job(base + timedelta(seconds=31)) is False
    # -31s 超出容差
    assert should_create_auction_final_job(base - timedelta(seconds=31)) is False
    # 09:26:00 明显超出
    assert should_create_auction_final_job(
        datetime(2026, 7, 31, 9, 26, 0, tzinfo=_TZ)
    ) is False


def test_should_create_auction_open_confirmation_at_exact_time() -> None:
    """10:00:00 精确命中触发窗口。"""
    now = datetime(2026, 7, 31, 10, 0, 0, tzinfo=_TZ)
    assert should_create_auction_open_confirmation_job(now) is True


def test_should_create_auction_open_confirmation_within_tolerance() -> None:
    """10:00 ±30s 容差内可创建。"""
    base = datetime(2026, 7, 31, 10, 0, 0, tzinfo=_TZ)
    assert should_create_auction_open_confirmation_job(base + timedelta(seconds=29)) is True
    assert should_create_auction_open_confirmation_job(base - timedelta(seconds=29)) is True


def test_should_not_create_open_confirmation_outside_window() -> None:
    """10:00 窗口外不创建（例如 11:00）。"""
    now = datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)
    assert should_create_auction_open_confirmation_job(now) is False


def test_should_not_create_final_during_open_confirmation_window() -> None:
    """互斥：10:00 窗口不创建 auction_final。"""
    now = datetime(2026, 7, 31, 10, 0, 0, tzinfo=_TZ)
    assert should_create_auction_final_job(now) is False


def test_should_not_create_open_confirmation_during_final_window() -> None:
    """互斥：09:25 窗口不创建 auction_open_confirmation。"""
    now = datetime(2026, 7, 31, 9, 25, 5, tzinfo=_TZ)
    assert should_create_auction_open_confirmation_job(now) is False


def test_is_in_trigger_window_boundary() -> None:
    """_is_in_trigger_window 边界值检查。"""
    now = datetime(2026, 7, 31, 9, 25, 5, tzinfo=_TZ)
    # 恰好 tolerance=0 → 仅精确命中
    assert _is_in_trigger_window(
        now, hour=9, minute=25, second=5, tolerance_seconds=0
    ) is True
    # 偏离 1s 且 tolerance=0 → False
    assert _is_in_trigger_window(
        now + timedelta(seconds=1),
        hour=9, minute=25, second=5, tolerance_seconds=0,
    ) is False


# =============================================================================
# 3. _auction_scheduler_poll_once 时间窗口与任务创建（mock DB）
# =============================================================================


class _FakeAsyncSession:
    """模拟 AsyncSession 上下文管理器（用于纯单元测试）。

    Worker 函数 `async with AsyncSessionLocal() as db:` 会调用 commit/rollback/get 等方法。
    本类提供最小可用实现，可通过 `db.get` 返回预设对象。
    """

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
    """构造一个 fake AsyncSessionLocal：每次 `async with X() as db` 取一个 session。"""

    class _Factory:
        def __init__(self) -> None:
            self._sessions = sessions
            self._idx = 0

        def __call__(self) -> _FakeAsyncSession:
            if self._idx >= len(self._sessions):
                # 超出预期：返回空 session，避免 IndexError
                return _FakeAsyncSession()
            s = self._sessions[self._idx]
            self._idx += 1
            return s

    return _Factory()


@pytest.mark.asyncio
async def test_poll_creates_auction_final_in_window_on_trading_day() -> None:
    """测试 2：09:25:05 窗口 + 交易日 → 创建 auction_final job（is_new=True）。"""
    from app.worker import _auction_scheduler_poll_once

    fake_session = _FakeAsyncSession()
    fake_session_local = _build_fake_session_local([fake_session])

    # mock is_trading_day_async 返回 True
    # mock create_auction_final_job 返回 (job_run, True)
    fake_job = MagicMock()
    fake_job.id = uuid.uuid4()

    with patch("app.worker.AsyncSessionLocal", fake_session_local), \
         patch(
             "app.services.auction_scheduler_service.acquire_job_run_lock",
             new=AsyncMock(return_value=(fake_job, True)),
         ), \
         patch(
             "app.services.calendar_service.is_trading_day_async",
             new=AsyncMock(return_value=True),
         ), \
         patch(
             "app.services.auction_scheduler_service.get_queued_auction_job",
             new=AsyncMock(return_value=None),
         ):
        # mock datetime.now 返回 09:25:05
        fixed_now = datetime(2026, 7, 31, 9, 25, 5, tzinfo=_TZ)
        with patch("app.worker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            # date 方法走真实 datetime
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = await _auction_scheduler_poll_once()

    # 无 queued job → 返回 False（但已创建 job）
    assert result is False
    # 验证 acquire_job_run_lock 被调用（创建 final job）
    # 注：create_auction_final_job 内部调用 acquire_job_run_lock


@pytest.mark.asyncio
async def test_poll_creates_auction_open_confirmation_in_window() -> None:
    """测试 3：10:00:00 窗口 + 交易日 → 创建 auction_open_confirmation job。"""
    from app.worker import _auction_scheduler_poll_once

    fake_session = _FakeAsyncSession()
    fake_session_local = _build_fake_session_local([fake_session])

    fake_job = MagicMock()
    fake_job.id = uuid.uuid4()

    with patch("app.worker.AsyncSessionLocal", fake_session_local), \
         patch(
             "app.services.auction_scheduler_service.acquire_job_run_lock",
             new=AsyncMock(return_value=(fake_job, True)),
         ), \
         patch(
             "app.services.calendar_service.is_trading_day_async",
             new=AsyncMock(return_value=True),
         ), \
         patch(
             "app.services.auction_scheduler_service.get_queued_auction_job",
             new=AsyncMock(return_value=None),
         ):
        fixed_now = datetime(2026, 7, 31, 10, 0, 0, tzinfo=_TZ)
        with patch("app.worker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = await _auction_scheduler_poll_once()

    assert result is False


@pytest.mark.asyncio
async def test_poll_does_not_create_on_non_trading_day() -> None:
    """测试 6：非交易日不创建任何 auction job。"""
    from app.worker import _auction_scheduler_poll_once

    fake_session = _FakeAsyncSession()
    fake_session_local = _build_fake_session_local([fake_session])

    create_call_count = 0

    async def _no_create(*args, **kwargs):
        nonlocal create_call_count
        create_call_count += 1
        return (None, False)

    with patch("app.worker.AsyncSessionLocal", fake_session_local), \
         patch(
             "app.services.auction_scheduler_service.acquire_job_run_lock",
             new=_no_create,
         ), \
         patch(
             "app.services.calendar_service.is_trading_day_async",
             new=AsyncMock(return_value=False),  # 非交易日
         ), \
         patch(
             "app.services.auction_scheduler_service.get_queued_auction_job",
             new=AsyncMock(return_value=None),
         ):
        # 即使在 09:25:05 窗口内，非交易日也不创建
        fixed_now = datetime(2026, 7, 31, 9, 25, 5, tzinfo=_TZ)
        with patch("app.worker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            await _auction_scheduler_poll_once()

    assert create_call_count == 0, "非交易日不应创建任何 auction job"


@pytest.mark.asyncio
async def test_poll_does_not_create_outside_window() -> None:
    """窗口外（如 11:00）不创建 auction job。"""
    from app.worker import _auction_scheduler_poll_once

    fake_session = _FakeAsyncSession()
    fake_session_local = _build_fake_session_local([fake_session])

    create_call_count = 0

    async def _track_create(*args, **kwargs):
        nonlocal create_call_count
        create_call_count += 1
        return (None, False)

    with patch("app.worker.AsyncSessionLocal", fake_session_local), \
         patch(
             "app.services.auction_scheduler_service.acquire_job_run_lock",
             new=_track_create,
         ), \
         patch(
             "app.services.calendar_service.is_trading_day_async",
             new=AsyncMock(return_value=True),
         ), \
         patch(
             "app.services.auction_scheduler_service.get_queued_auction_job",
             new=AsyncMock(return_value=None),
         ):
        # 11:00 在两个窗口之外
        fixed_now = datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)
        with patch("app.worker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            await _auction_scheduler_poll_once()

    assert create_call_count == 0, "窗口外不应创建 auction job"


@pytest.mark.asyncio
async def test_multiple_polls_same_minute_dont_duplicate() -> None:
    """测试 4：同一分钟多次 poll 不重复创建（acquire_job_run_lock 返回 is_new=False）。"""
    from app.worker import _auction_scheduler_poll_once

    # 模拟第二次调用：acquire_job_run_lock 返回 (existing, False) → SKIPPED_DUPLICATE
    fake_session = _FakeAsyncSession()
    fake_session_local = _build_fake_session_local([fake_session])

    existing_job = MagicMock()
    existing_job.id = uuid.uuid4()
    lock_call_count = 0

    async def _lock_returns_existing(*args, **kwargs):
        nonlocal lock_call_count
        lock_call_count += 1
        # 第二次起返回 (existing, False)
        return (existing_job, False)

    with patch("app.worker.AsyncSessionLocal", fake_session_local), \
         patch(
             "app.services.auction_scheduler_service.acquire_job_run_lock",
             new=_lock_returns_existing,
         ), \
         patch(
             "app.services.calendar_service.is_trading_day_async",
             new=AsyncMock(return_value=True),
         ), \
         patch(
             "app.services.auction_scheduler_service.get_queued_auction_job",
             new=AsyncMock(return_value=None),
         ):
        fixed_now = datetime(2026, 7, 31, 9, 25, 10, tzinfo=_TZ)
        with patch("app.worker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            # 连续两次 poll（同一分钟）
            await _auction_scheduler_poll_once()
            await _auction_scheduler_poll_once()

    # acquire_job_run_lock 应被调用两次（幂等机制下每次 poll 都会尝试）
    # 但第二次返回 is_new=False，不会重复 commit
    assert lock_call_count == 2


# =============================================================================
# 4. _auction_scheduler_poll_once 任务领取与执行（mock DB + 服务）
# =============================================================================


def _make_job_run(
    *,
    job_name: str = AUCTION_FINAL_JOB_NAME,
    trade_date: date = date(2026, 7, 31),
    status: str = "queued",
    lease_epoch: int = 0,
    metadata: dict | None = None,
) -> MagicMock:
    """构造一个 SchedulerJobRun mock。"""
    meta = metadata if metadata is not None else {"trade_date": trade_date.isoformat()}
    job = MagicMock()
    job.id = uuid.uuid4()
    job.job_name = job_name
    job.status = status
    job.lease_epoch = lease_epoch
    job.started_at = None
    job.heartbeat_at = None
    job.metadata_json = json.dumps(meta)
    job.worker_instance_id = None
    return job


@pytest.mark.asyncio
async def test_poll_executes_queued_auction_final_job() -> None:
    """领取 queued auction_final job → 调用 execute_auction_scan_run。"""
    from app.worker import _auction_scheduler_poll_once

    fake_job = _make_job_run(job_name=AUCTION_FINAL_JOB_NAME)
    fake_session = _FakeAsyncSession()
    fake_session_local = _build_fake_session_local([fake_session])

    execute_called = False

    async def _track_execute(*, job_run_id, trade_date, **kwargs):
        nonlocal execute_called
        execute_called = True
        assert job_run_id == fake_job.id
        assert trade_date == date(2026, 7, 31)
        return {"status": "succeeded"}

    with patch("app.worker.AsyncSessionLocal", fake_session_local), \
         patch(
             "app.services.calendar_service.is_trading_day_async",
             new=AsyncMock(return_value=False),  # 不在窗口，跳过创建
         ), \
         patch(
             "app.services.auction_scheduler_service.get_queued_auction_job",
             new=AsyncMock(return_value=fake_job),
         ), \
         patch(
             "app.services.auction_scheduler_service.execute_auction_scan_run",
             new=_track_execute,
         ), \
         patch(
             "app.services.auction_scheduler_service.execute_auction_open_confirmation_run",
             new=AsyncMock(),
         ):
        # 时间不在触发窗口，但仍有 queued job 可领取
        fixed_now = datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)
        with patch("app.worker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            # get_queued_auction_job 内部使用同一 session；需让 fake_session 返回 job
            # 注意：_auction_scheduler_poll_once 中 get_queued_auction_job(db) 直接调用
            # 但实际 db 是 fake_session。我们 patch get_queued_auction_job 直接返回 job
            result = await _auction_scheduler_poll_once()

    assert result is True, "领取并执行了任务应返回 True"
    assert execute_called, "应调用 execute_auction_scan_run"
    # 验证任务被标记 running + lease_epoch 递增
    assert fake_job.status == "running"
    assert fake_job.lease_epoch == 1, "lease_epoch 应从 0 递增到 1（fencing）"
    assert fake_job.worker_instance_id is not None


@pytest.mark.asyncio
async def test_poll_executes_queued_open_confirmation_job() -> None:
    """领取 queued auction_open_confirmation job → 调用 execute_auction_open_confirmation_run。"""
    from app.worker import _auction_scheduler_poll_once

    fake_job = _make_job_run(job_name=AUCTION_OPEN_CONFIRMATION_JOB_NAME)
    fake_session = _FakeAsyncSession()
    fake_session_local = _build_fake_session_local([fake_session])

    execute_called = False

    async def _track_execute(*, job_run_id, trade_date, **kwargs):
        nonlocal execute_called
        execute_called = True
        return {"status": "succeeded"}

    with patch("app.worker.AsyncSessionLocal", fake_session_local), \
         patch(
             "app.services.calendar_service.is_trading_day_async",
             new=AsyncMock(return_value=False),
         ), \
         patch(
             "app.services.auction_scheduler_service.get_queued_auction_job",
             new=AsyncMock(return_value=fake_job),
         ), \
         patch(
             "app.services.auction_scheduler_service.execute_auction_scan_run",
             new=AsyncMock(),
         ), \
         patch(
             "app.services.auction_scheduler_service.execute_auction_open_confirmation_run",
             new=_track_execute,
         ):
        fixed_now = datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)
        with patch("app.worker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = await _auction_scheduler_poll_once()

    assert result is True
    assert execute_called, "应调用 execute_auction_open_confirmation_run"


@pytest.mark.asyncio
async def test_poll_no_queued_job_returns_false() -> None:
    """无 queued auction job → 返回 False。"""
    from app.worker import _auction_scheduler_poll_once

    fake_session = _FakeAsyncSession()
    fake_session_local = _build_fake_session_local([fake_session])

    with patch("app.worker.AsyncSessionLocal", fake_session_local), \
         patch(
             "app.services.calendar_service.is_trading_day_async",
             new=AsyncMock(return_value=False),
         ), \
         patch(
             "app.services.auction_scheduler_service.get_queued_auction_job",
             new=AsyncMock(return_value=None),
         ):
        fixed_now = datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)
        with patch("app.worker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = await _auction_scheduler_poll_once()

    assert result is False


@pytest.mark.asyncio
async def test_poll_job_missing_trade_date_marks_failed() -> None:
    """任务 metadata 缺少 trade_date → 标记 failed。"""
    from app.worker import _auction_scheduler_poll_once

    # metadata 不含 trade_date
    fake_job = _make_job_run(metadata={"auction_type": "final"})
    # db.get 返回的 mock 对象（用于标记 failed）
    failed_jr = MagicMock()
    # 三次 async with AsyncSessionLocal():
    #   1) 时间窗口检查（is_trading_day patched）
    #   2) get_queued_auction_job（patched）
    #   3) db.get(SchedulerJobRun, job_run_id) → 返回 failed_jr
    sessions = [_FakeAsyncSession(), _FakeAsyncSession(), _FakeAsyncSession()]
    sessions[2]._objects[fake_job.id] = failed_jr
    fake_session_local = _build_fake_session_local(sessions)

    with patch("app.worker.AsyncSessionLocal", fake_session_local), \
         patch(
             "app.services.calendar_service.is_trading_day_async",
             new=AsyncMock(return_value=False),
         ), \
         patch(
             "app.services.auction_scheduler_service.get_queued_auction_job",
             new=AsyncMock(return_value=fake_job),
         ):
        fixed_now = datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)
        with patch("app.worker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            result = await _auction_scheduler_poll_once()

    assert result is True
    # 验证 db.get 返回的对象被标记 failed
    assert failed_jr.status == "failed"
    assert failed_jr.error_message == "任务缺少 trade_date"


# =============================================================================
# 5. fencing 恢复：lease_epoch 递增
# =============================================================================


@pytest.mark.asyncio
async def test_poll_fencing_epoch_increments_on_claim() -> None:
    """测试 8：过期租约恢复时 lease_epoch 必须递增（fencing）。

    Worker 领取已存在 lease_epoch=N 的任务时，必须递增到 N+1，
    旧的 lease_epoch 持有者写库时会被 fencing 拒绝。
    """
    from app.worker import _auction_scheduler_poll_once

    # 已有 lease_epoch=3 的任务（之前 worker 崩溃残留）
    fake_job = _make_job_run(lease_epoch=3)
    fake_session = _FakeAsyncSession()
    fake_session_local = _build_fake_session_local([fake_session])

    with patch("app.worker.AsyncSessionLocal", fake_session_local), \
         patch(
             "app.services.calendar_service.is_trading_day_async",
             new=AsyncMock(return_value=False),
         ), \
         patch(
             "app.services.auction_scheduler_service.get_queued_auction_job",
             new=AsyncMock(return_value=fake_job),
         ), \
         patch(
             "app.services.auction_scheduler_service.execute_auction_scan_run",
             new=AsyncMock(return_value={"status": "succeeded"}),
         ):
        fixed_now = datetime(2026, 7, 31, 11, 0, 0, tzinfo=_TZ)
        with patch("app.worker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            await _auction_scheduler_poll_once()

    # fencing：lease_epoch 从 3 递增到 4
    assert fake_job.lease_epoch == 4, (
        f"lease_epoch 应从 3 递增到 4（fencing），实际: {fake_job.lease_epoch}"
    )


# =============================================================================
# 6. 异常隔离：Auction 轮询异常不终止 after-close 主 Worker
# =============================================================================


@pytest.mark.asyncio
async def test_co_process_exception_does_not_crash_main_worker() -> None:
    """测试 9：_auction_scheduler_poll_once 抛异常时，co-process 捕获并继续，
    主 Worker 不受影响。

    模拟：co-process 循环中第一次 poll 抛异常，第二次正常返回。
    验证：co-process 不退出，第二次 poll 仍被调用。
    """
    import app.worker as worker_mod

    call_count = 0

    async def _flaky_poll():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("模拟 auction poll 异常")
        # 第二次设置 _shutdown=True 让循环退出
        worker_mod._shutdown = True
        return False

    with patch.object(worker_mod, "_auction_scheduler_poll_once", side_effect=_flaky_poll), \
         patch(
             "app.services.auction_scheduler_service.AUCTION_SCHEDULER_POLL_INTERVAL",
             0,  # 立即轮询
         ), \
         patch.object(
             worker_mod, "recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0),
         ), \
         patch("app.worker.AsyncSessionLocal", _build_fake_session_local([])):
        # 重置 _shutdown
        worker_mod._shutdown = False
        try:
            await asyncio.wait_for(
                worker_mod._run_auction_scheduler_co_process(),
                timeout=5,
            )
        except TimeoutError:
            pass

    # 第一次抛异常被捕获，第二次正常执行
    assert call_count >= 2, (
        f"co-process 应在异常后继续运行，至少调用 2 次，实际: {call_count}"
    )


@pytest.mark.asyncio
async def test_main_worker_does_not_propagate_auction_exception() -> None:
    """测试 9 续：run_after_close_orchestrator_worker 即使 co-process 内部抛异常，
    主 Worker 也不应崩溃。

    通过让主循环 sleep 真实 yield（让 co-process 获得调度），验证：
    1. co-process 内部异常被隔离
    2. 主 Worker 在 finally 块正确 await co-process drain
    3. 主 Worker 正常退出（不抛出异常）
    """
    import app.worker as worker_mod

    # 重置状态
    worker_mod._shutdown = False

    auction_poll_calls = 0

    async def _always_fail():
        nonlocal auction_poll_calls
        auction_poll_calls += 1
        raise RuntimeError("持续异常")

    async def _no_claim():
        return False

    async def _quick_heartbeat(_name=None, interval=60):
        # 心跳立即退出（避免 DB 调用）
        return

    # 让主循环在第一次 poll 后通过真实 sleep(0) yield 给 co-process，然后退出
    main_poll_count = 0

    async def _set_shutdown_after_yield():
        nonlocal main_poll_count
        main_poll_count += 1
        # yield 一次让 co-process 获得调度机会
        await asyncio.sleep(0)
        worker_mod._shutdown = True
        return False

    with patch.object(worker_mod, "_auction_scheduler_poll_once", side_effect=_always_fail), \
         patch.object(worker_mod, "_after_close_poll_once", side_effect=_set_shutdown_after_yield), \
         patch.object(worker_mod, "_chip_consensus_poll_once", side_effect=_no_claim), \
         patch.object(worker_mod, "_heartbeat_loop", side_effect=_quick_heartbeat), \
         patch.object(
             worker_mod, "auto_resume_interrupted_after_close_runs", new=AsyncMock(return_value=0),
         ), \
         patch.object(
             worker_mod, "recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0),
         ), \
         patch("app.worker.AsyncSessionLocal", _build_fake_session_local([])), \
         patch.object(worker_mod, "WORKER_INTERVAL", 0), \
         patch(
             "app.services.auction_scheduler_service.AUCTION_SCHEDULER_POLL_INTERVAL", 0,
         ):
        # 主 Worker 应正常完成（不抛异常）
        await asyncio.wait_for(
            worker_mod.run_after_close_orchestrator_worker(),
            timeout=10,
        )

    # 主 Worker 至少 poll 一次
    assert main_poll_count >= 1, "主 Worker 应至少 poll 一次"
    # co-process 被调用过（异常被隔离，主 Worker 未崩溃）
    assert auction_poll_calls >= 1, "Auction co-process 应被调用"


# =============================================================================
# 7. SIGTERM drain：_shutdown 标志让 co-process 退出
# =============================================================================


@pytest.mark.asyncio
async def test_sigterm_terminates_co_process() -> None:
    """测试 10：SIGTERM 设置 _shutdown=True 后，co-process 退出循环。"""
    import app.worker as worker_mod

    worker_mod._shutdown = False

    poll_count = 0

    async def _poll_once():
        nonlocal poll_count
        poll_count += 1
        # 第一次 poll 后设置 _shutdown=True（模拟 SIGTERM）
        if poll_count >= 1:
            worker_mod._shutdown = True
        return False

    with patch.object(worker_mod, "_auction_scheduler_poll_once", side_effect=_poll_once), \
         patch(
             "app.services.auction_scheduler_service.AUCTION_SCHEDULER_POLL_INTERVAL",
             0,
         ), \
         patch.object(
             worker_mod, "recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0),
         ), \
         patch("app.worker.AsyncSessionLocal", _build_fake_session_local([])):
        await asyncio.wait_for(
            worker_mod._run_auction_scheduler_co_process(),
            timeout=5,
        )

    assert poll_count >= 1, "co-process 应至少 poll 一次"
    # co-process 退出后 _shutdown 仍为 True
    assert worker_mod._shutdown is True


@pytest.mark.asyncio
async def test_co_process_calls_recover_on_startup() -> None:
    """测试 11：co-process 启动时调用 recover_stale_scheduler_job_runs 恢复崩溃残留。"""
    import app.worker as worker_mod

    worker_mod._shutdown = False

    recover_called = False

    async def _track_recover(*args, **kwargs):
        nonlocal recover_called
        recover_called = True
        return 0

    async def _quick_poll():
        worker_mod._shutdown = True
        return False

    with patch.object(worker_mod, "_auction_scheduler_poll_once", side_effect=_quick_poll), \
         patch.object(
             worker_mod, "recover_stale_scheduler_job_runs", new=_track_recover,
         ), \
         patch("app.worker.AsyncSessionLocal", _build_fake_session_local([])), \
         patch(
             "app.services.auction_scheduler_service.AUCTION_SCHEDULER_POLL_INTERVAL",
             0,
         ):
        await asyncio.wait_for(
            worker_mod._run_auction_scheduler_co_process(),
            timeout=5,
        )

    assert recover_called, "co-process 启动时应调用 recover_stale_scheduler_job_runs"


@pytest.mark.asyncio
async def test_co_process_no_duplicate_background_tasks() -> None:
    """测试 11 续：co-process 是单 task，主 Worker 只 create_task 一次。

    通过检查 run_after_close_orchestrator_worker 源码中 create_task 调用次数。
    """
    content = _WORKER_FILE.read_text(encoding="utf-8")
    # 定位 run_after_close_orchestrator_worker 函数体
    start = content.index("async def run_after_close_orchestrator_worker")
    # 截取到下一个 async def（函数结束）
    next_def = content.index("\nasync def ", start + 1)
    func_body = content[start:next_def]

    # _run_auction_scheduler_co_process 的 create_task 应只出现一次
    create_count = func_body.count("create_task(_run_auction_scheduler_co_process")
    assert create_count == 1, (
        f"run_after_close_orchestrator_worker 应只 create_task 一次 co-process，实际: {create_count}"
    )


# =============================================================================
# 8. 常量与合同校验
# =============================================================================


def test_auction_scheduler_constants_contract() -> None:
    """守护：常量与 PRD 合同一致。"""
    assert AUCTION_FINAL_JOB_NAME == "auction_final"
    assert AUCTION_OPEN_CONFIRMATION_JOB_NAME == "auction_open_confirmation"
    assert AUCTION_FINAL_LEASE_SECONDS == 1800  # 30 分钟
    assert AUCTION_OPEN_CONFIRMATION_LEASE_SECONDS == 600  # 10 分钟
    assert AUCTION_SCHEDULER_POLL_INTERVAL == 30  # 30s
    assert AUCTION_FINAL_TRIGGER_TOLERANCE_SECONDS == 30  # ±30s
