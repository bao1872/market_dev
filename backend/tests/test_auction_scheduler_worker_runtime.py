# PURE_UNIT_TEST=1
"""[P0-3 / W6B] Auction Scheduler lifecycle runtime 单元测试（不连接数据库）。

直接覆盖 app.services.auction_scheduler_worker_runtime：
- run_auction_scheduler_co_process_runtime（附 AfterClose，无独立 heartbeat）
- run_auction_scheduler_worker_runtime（standalone，独立 heartbeat）
- 私有 core _run_auction_scheduler_loop（共享 recovery + poll loop + SIGTERM drain）

10 个用例对齐 W6B 冻结合同：

1. co-process 不创建 heartbeat
2. standalone 恰好创建一次 heartbeat，参数 "auction_scheduler"
3. heartbeat 创建发生在 recovery 前
4. recovery → commit
5. recovery 抛异常仍进入 poll
6. poll 抛异常，shutdown=False 时下一轮仍运行
7. poll 后 shutdown=True → 不 sleep
8. 初始 shutdown=True → poll=0
9. 正常 poll 后 sleep exact interval
10. 两个 worker façades 只是正确注入并 delegate

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_auction_scheduler_worker_runtime.py -v
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeSession:
    """模拟 AsyncSession（纯单元测试用，最小可用）。"""

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False
        self._objects: dict = {}

    async def __aenter__(self) -> _FakeSession:
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


def _build_fake_session_local(sessions: list[_FakeSession]):
    """构造 fake session_factory：每次调用取一个 session（超出返回空 session）。"""

    class _Factory:
        def __init__(self) -> None:
            self._sessions = sessions
            self._idx = 0

        def __call__(self) -> _FakeSession:
            if self._idx >= len(self._sessions):
                return _FakeSession()
            s = self._sessions[self._idx]
            self._idx += 1
            return s

    return _Factory()


# =============================================================================
# 1. co-process 不创建 heartbeat
# =============================================================================


@pytest.mark.asyncio
async def test_co_process_creates_no_heartbeat() -> None:
    from app.services.auction_scheduler_worker_runtime import (
        run_auction_scheduler_co_process_runtime,
    )

    hb_calls: list[str] = []

    def _hb(name: str):
        hb_calls.append(name)
        async def _c() -> None:
            return
        return _c()

    shutdown = [False]
    poll_calls = {"n": 0}

    async def _recover(db) -> int:
        return 0

    async def _poll() -> bool:
        poll_calls["n"] += 1
        shutdown[0] = True  # 第一次 poll 后即退出
        return False

    session_factory = _build_fake_session_local([_FakeSession()])
    logger = MagicMock()

    with patch("app.services.auction_scheduler_service.AUCTION_SCHEDULER_POLL_INTERVAL", 0):
        await run_auction_scheduler_co_process_runtime(
            session_factory=session_factory,
            recover_stale_job_runs=_recover,
            poll_once=_poll,
            should_shutdown=lambda: shutdown[0],
            logger=logger,
        )

    assert hb_calls == [], "co-process 不得创建 heartbeat"


# =============================================================================
# 2. standalone 恰好创建一次 heartbeat，参数 "auction_scheduler"
# =============================================================================


@pytest.mark.asyncio
async def test_standalone_creates_heartbeat_once() -> None:
    from app.services.auction_scheduler_worker_runtime import (
        run_auction_scheduler_worker_runtime,
    )

    hb_calls: list[str] = []

    def _hb(name: str):
        hb_calls.append(name)
        async def _c() -> None:
            return
        return _c()

    shutdown = [False]

    async def _recover(db) -> int:
        return 0

    async def _poll() -> bool:
        shutdown[0] = True
        return False

    session_factory = _build_fake_session_local([_FakeSession()])
    logger = MagicMock()

    with patch("app.services.auction_scheduler_service.AUCTION_SCHEDULER_POLL_INTERVAL", 0):
        await run_auction_scheduler_worker_runtime(
            session_factory=session_factory,
            recover_stale_job_runs=_recover,
            poll_once=_poll,
            heartbeat_loop=_hb,
            should_shutdown=lambda: shutdown[0],
            logger=logger,
        )

    assert hb_calls == ["auction_scheduler"], "standalone 恰好一次 heartbeat，参数 auction_scheduler"


# =============================================================================
# 3. heartbeat 创建发生在 recovery 前
# =============================================================================


@pytest.mark.asyncio
async def test_heartbeat_created_before_recovery() -> None:
    from app.services.auction_scheduler_worker_runtime import (
        run_auction_scheduler_worker_runtime,
    )

    events: list[str] = []

    def _hb(name: str):
        events.append("hb")
        async def _c() -> None:
            return
        return _c()

    shutdown = [False]

    async def _recover(db) -> int:
        events.append("recover")
        return 0

    async def _poll() -> bool:
        shutdown[0] = True
        return False

    session_factory = _build_fake_session_local([_FakeSession()])
    logger = MagicMock()

    with patch("app.services.auction_scheduler_service.AUCTION_SCHEDULER_POLL_INTERVAL", 0):
        await run_auction_scheduler_worker_runtime(
            session_factory=session_factory,
            recover_stale_job_runs=_recover,
            poll_once=_poll,
            heartbeat_loop=_hb,
            should_shutdown=lambda: shutdown[0],
            logger=logger,
        )

    assert events[0] == "hb", "heartbeat 应在 recovery 前创建"
    assert "recover" in events
    assert events.index("hb") < events.index("recover")


# =============================================================================
# 4. recovery → commit
# =============================================================================


@pytest.mark.asyncio
async def test_recovery_commits() -> None:
    from app.services.auction_scheduler_worker_runtime import (
        run_auction_scheduler_co_process_runtime,
    )

    session = _FakeSession()
    session_factory = _build_fake_session_local([session])
    logger = MagicMock()
    shutdown = [False]

    async def _recover(db) -> int:
        return 0

    async def _poll() -> bool:
        shutdown[0] = True
        return False

    with patch("app.services.auction_scheduler_service.AUCTION_SCHEDULER_POLL_INTERVAL", 0):
        await run_auction_scheduler_co_process_runtime(
            session_factory=session_factory,
            recover_stale_job_runs=_recover,
            poll_once=_poll,
            should_shutdown=lambda: shutdown[0],
            logger=logger,
        )

    assert session.committed is True


# =============================================================================
# 5. recovery 抛异常仍进入 poll
# =============================================================================


@pytest.mark.asyncio
async def test_recovery_failure_still_polls() -> None:
    from app.services.auction_scheduler_worker_runtime import (
        run_auction_scheduler_co_process_runtime,
    )

    shutdown = [False]
    poll_calls = {"n": 0}

    async def _recover_boom(db) -> int:
        raise RuntimeError("recovery down")

    async def _poll() -> bool:
        poll_calls["n"] += 1
        shutdown[0] = True
        return False

    session_factory = _build_fake_session_local([_FakeSession()])
    logger = MagicMock()

    with patch("app.services.auction_scheduler_service.AUCTION_SCHEDULER_POLL_INTERVAL", 0):
        await run_auction_scheduler_co_process_runtime(
            session_factory=session_factory,
            recover_stale_job_runs=_recover_boom,
            poll_once=_poll,
            should_shutdown=lambda: shutdown[0],
            logger=logger,
        )

    assert poll_calls["n"] >= 1, "recovery 异常后仍需进入 poll loop"
    logger.exception.assert_called()


# =============================================================================
# 6. poll 抛异常，shutdown=False 时下一轮仍运行
# =============================================================================


@pytest.mark.asyncio
async def test_poll_exception_continues_next_round() -> None:
    from app.services.auction_scheduler_worker_runtime import (
        run_auction_scheduler_co_process_runtime,
    )

    shutdown = [False]
    poll_calls = {"n": 0}

    async def _poll() -> bool:
        poll_calls["n"] += 1
        if poll_calls["n"] == 1:
            raise RuntimeError("poll boom")
        shutdown[0] = True
        return False

    async def _recover(db) -> int:
        return 0

    session_factory = _build_fake_session_local([_FakeSession()])
    logger = MagicMock()

    with patch("app.services.auction_scheduler_service.AUCTION_SCHEDULER_POLL_INTERVAL", 0):
        await run_auction_scheduler_co_process_runtime(
            session_factory=session_factory,
            recover_stale_job_runs=_recover,
            poll_once=_poll,
            should_shutdown=lambda: shutdown[0],
            logger=logger,
        )

    assert poll_calls["n"] >= 2, "poll 异常后下一轮仍应运行"
    logger.exception.assert_called()


# =============================================================================
# 7. poll 后 shutdown=True → 不 sleep
# =============================================================================


@pytest.mark.asyncio
async def test_shutdown_after_poll_skips_sleep() -> None:
    from app.services.auction_scheduler_worker_runtime import (
        run_auction_scheduler_co_process_runtime,
    )

    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    def _sleep(sec: float):
        sleep_calls.append(sec)
        return real_sleep(0)

    shutdown = [False]
    poll_calls = {"n": 0}

    async def _recover(db) -> int:
        return 0

    async def _poll() -> bool:
        poll_calls["n"] += 1
        shutdown[0] = True  # poll 内触发 shutdown
        return False

    session_factory = _build_fake_session_local([_FakeSession()])
    logger = MagicMock()

    with patch("asyncio.sleep", _sleep), \
         patch("app.services.auction_scheduler_service.AUCTION_SCHEDULER_POLL_INTERVAL", 0):
        await run_auction_scheduler_co_process_runtime(
            session_factory=session_factory,
            recover_stale_job_runs=_recover,
            poll_once=_poll,
            should_shutdown=lambda: shutdown[0],
            logger=logger,
        )

    assert poll_calls["n"] == 1
    assert sleep_calls == [], "shutdown 后不应 sleep"


# =============================================================================
# 8. 初始 shutdown=True → poll=0
# =============================================================================


@pytest.mark.asyncio
async def test_initial_shutdown_zero_poll() -> None:
    from app.services.auction_scheduler_worker_runtime import (
        run_auction_scheduler_co_process_runtime,
    )

    shutdown = [True]
    poll_calls = {"n": 0}

    async def _recover(db) -> int:
        return 0

    async def _poll() -> bool:
        poll_calls["n"] += 1
        return False

    session_factory = _build_fake_session_local([_FakeSession()])
    logger = MagicMock()

    with patch("app.services.auction_scheduler_service.AUCTION_SCHEDULER_POLL_INTERVAL", 0):
        await run_auction_scheduler_co_process_runtime(
            session_factory=session_factory,
            recover_stale_job_runs=_recover,
            poll_once=_poll,
            should_shutdown=lambda: shutdown[0],
            logger=logger,
        )

    assert poll_calls["n"] == 0, "初始 shutdown 时不应 poll"


# =============================================================================
# 9. 正常 poll 后 sleep exact interval
# =============================================================================


@pytest.mark.asyncio
async def test_normal_poll_sleeps_exact_interval() -> None:
    from app.services.auction_scheduler_worker_runtime import (
        run_auction_scheduler_co_process_runtime,
    )

    sleep_calls: list[int] = []
    real_sleep = asyncio.sleep

    def _sleep(sec: int):
        sleep_calls.append(sec)
        shutdown[0] = True  # sleep 后立即退出，避免无限循环
        return real_sleep(0)

    shutdown = [False]

    async def _recover(db) -> int:
        return 0

    async def _poll() -> bool:
        return False

    session_factory = _build_fake_session_local([_FakeSession()])
    logger = MagicMock()

    with patch("asyncio.sleep", _sleep), \
         patch("app.services.auction_scheduler_service.AUCTION_SCHEDULER_POLL_INTERVAL", 7):
        await run_auction_scheduler_co_process_runtime(
            session_factory=session_factory,
            recover_stale_job_runs=_recover,
            poll_once=_poll,
            should_shutdown=lambda: shutdown[0],
            logger=logger,
        )

    assert sleep_calls == [7], "正常 poll 后应按 exact interval sleep"


# =============================================================================
# 10. 两个 worker façades 只是正确注入并 delegate
# =============================================================================


@pytest.mark.asyncio
async def test_worker_facades_delegate_with_injection() -> None:
    import app.worker as worker_mod

    with patch(
        "app.services.auction_scheduler_worker_runtime.run_auction_scheduler_co_process_runtime",
        new=AsyncMock(return_value=None),
    ) as mock_co, \
         patch(
        "app.services.auction_scheduler_worker_runtime.run_auction_scheduler_worker_runtime",
        new=AsyncMock(return_value=None),
    ) as mock_standalone:
        await worker_mod._run_auction_scheduler_co_process()
        await worker_mod.run_auction_scheduler_worker()

    mock_co.assert_awaited_once()
    _, co_kwargs = mock_co.call_args
    assert co_kwargs["session_factory"] is worker_mod.AsyncSessionLocal
    assert co_kwargs["recover_stale_job_runs"] is worker_mod.recover_stale_scheduler_job_runs
    assert co_kwargs["poll_once"] is worker_mod._auction_scheduler_poll_once
    assert callable(co_kwargs["should_shutdown"])
    assert co_kwargs["logger"] is worker_mod.logger
    assert "heartbeat_loop" not in co_kwargs, "co-process 不应注入 heartbeat"

    mock_standalone.assert_awaited_once()
    _, st_kwargs = mock_standalone.call_args
    assert st_kwargs["session_factory"] is worker_mod.AsyncSessionLocal
    assert st_kwargs["recover_stale_job_runs"] is worker_mod.recover_stale_scheduler_job_runs
    assert st_kwargs["poll_once"] is worker_mod._auction_scheduler_poll_once
    assert st_kwargs["heartbeat_loop"] is worker_mod._heartbeat_loop
    assert callable(st_kwargs["should_shutdown"])
    assert st_kwargs["logger"] is worker_mod.logger
