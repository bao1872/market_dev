# PURE_UNIT_TEST=1
"""[ChipConsensusWorker / W7A] Chip lifecycle runtime 单元测试（不连接数据库）。

直接覆盖 app.services.chip_consensus_worker_runtime.run_chip_consensus_worker_runtime：
- standalone debug Worker，自带 chip_consensus heartbeat（非 co-process）
- 冻结合同：heartbeat 顺序 / recovery / poll 异常隔离 / shutdown check / 动态 interval

10 个用例对齐 W7A 冻结合同：

1. heartbeat 恰好创建一次，name="chip_consensus"
2. heartbeat 创建发生在 recovery 前
3. recovery → commit
4. recovered=0 仍 commit
5. recovery exception 非致命，仍 poll
6. initial shutdown=True → poll=0
7. poll exception 后 shutdown=False → 下一轮继续
8. poll 后 shutdown=True → sleep=0
9. 正常 sleep 使用 worker_interval() 当前值（动态 7→11）
10. façade 注入全部正确

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_chip_consensus_worker_runtime.py -v
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
# 1. heartbeat 恰好创建一次，name="chip_consensus"
# =============================================================================


@pytest.mark.asyncio
async def test_heartbeat_created_once_with_name() -> None:
    from app.services.chip_consensus_worker_runtime import (
        run_chip_consensus_worker_runtime,
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

    await run_chip_consensus_worker_runtime(
        session_factory=session_factory,
        heartbeat_loop=_hb,
        recover_stale_job_runs=_recover,
        poll_once=_poll,
        should_shutdown=lambda: shutdown[0],
        worker_interval=lambda: 5,
        logger=logger,
    )

    assert hb_calls == ["chip_consensus"], "heartbeat 恰好一次，参数 chip_consensus"


# =============================================================================
# 2. heartbeat 创建发生在 recovery 前
# =============================================================================


@pytest.mark.asyncio
async def test_heartbeat_before_recovery() -> None:
    from app.services.chip_consensus_worker_runtime import (
        run_chip_consensus_worker_runtime,
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

    await run_chip_consensus_worker_runtime(
        session_factory=session_factory,
        heartbeat_loop=_hb,
        recover_stale_job_runs=_recover,
        poll_once=_poll,
        should_shutdown=lambda: shutdown[0],
        worker_interval=lambda: 5,
        logger=logger,
    )

    assert events[0] == "hb", "heartbeat 应在 recovery 前创建"
    assert "recover" in events
    assert events.index("hb") < events.index("recover")


# =============================================================================
# 3. recovery → commit
# =============================================================================


@pytest.mark.asyncio
async def test_recovery_commits() -> None:
    from app.services.chip_consensus_worker_runtime import (
        run_chip_consensus_worker_runtime,
    )

    session = _FakeSession()
    session_factory = _build_fake_session_local([session])
    logger = MagicMock()
    shutdown = [False]

    async def _recover(db) -> int:
        return 3

    async def _poll() -> bool:
        shutdown[0] = True
        return False

    await run_chip_consensus_worker_runtime(
        session_factory=session_factory,
        heartbeat_loop=_fake_heartbeat,
        recover_stale_job_runs=_recover,
        poll_once=_poll,
        should_shutdown=lambda: shutdown[0],
        worker_interval=lambda: 5,
        logger=logger,
    )

    assert session.committed is True


async def _fake_heartbeat(name: str):
    return None


# =============================================================================
# 4. recovered=0 仍 commit
# =============================================================================


@pytest.mark.asyncio
async def test_recovery_commits_even_when_zero() -> None:
    from app.services.chip_consensus_worker_runtime import (
        run_chip_consensus_worker_runtime,
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

    await run_chip_consensus_worker_runtime(
        session_factory=session_factory,
        heartbeat_loop=_fake_heartbeat,
        recover_stale_job_runs=_recover,
        poll_once=_poll,
        should_shutdown=lambda: shutdown[0],
        worker_interval=lambda: 5,
        logger=logger,
    )

    assert session.committed is True, "recovered=0 时 commit 仍在 if 外执行"


# =============================================================================
# 5. recovery exception 非致命，仍 poll
# =============================================================================


@pytest.mark.asyncio
async def test_recovery_failure_still_polls() -> None:
    from app.services.chip_consensus_worker_runtime import (
        run_chip_consensus_worker_runtime,
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

    await run_chip_consensus_worker_runtime(
        session_factory=session_factory,
        heartbeat_loop=_fake_heartbeat,
        recover_stale_job_runs=_recover_boom,
        poll_once=_poll,
        should_shutdown=lambda: shutdown[0],
        worker_interval=lambda: 5,
        logger=logger,
    )

    assert poll_calls["n"] >= 1, "recovery 异常后仍需进入 worker loop"
    logger.exception.assert_called()


# =============================================================================
# 6. initial shutdown=True → poll=0
# =============================================================================


@pytest.mark.asyncio
async def test_initial_shutdown_zero_poll() -> None:
    from app.services.chip_consensus_worker_runtime import (
        run_chip_consensus_worker_runtime,
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

    await run_chip_consensus_worker_runtime(
        session_factory=session_factory,
        heartbeat_loop=_fake_heartbeat,
        recover_stale_job_runs=_recover,
        poll_once=_poll,
        should_shutdown=lambda: shutdown[0],
        worker_interval=lambda: 5,
        logger=logger,
    )

    assert poll_calls["n"] == 0, "初始 shutdown 时不应 poll"


# =============================================================================
# 7. poll exception 后 shutdown=False → 下一轮继续
# =============================================================================


@pytest.mark.asyncio
async def test_poll_exception_continues_next_round() -> None:
    from app.services.chip_consensus_worker_runtime import (
        run_chip_consensus_worker_runtime,
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

    await run_chip_consensus_worker_runtime(
        session_factory=session_factory,
        heartbeat_loop=_fake_heartbeat,
        recover_stale_job_runs=_recover,
        poll_once=_poll,
        should_shutdown=lambda: shutdown[0],
        worker_interval=lambda: 0,
        logger=logger,
    )

    assert poll_calls["n"] >= 2, "poll 异常后下一轮仍应运行"
    logger.exception.assert_called()


# =============================================================================
# 8. poll 后 shutdown=True → sleep=0
# =============================================================================


@pytest.mark.asyncio
async def test_shutdown_after_poll_skips_sleep() -> None:
    from app.services.chip_consensus_worker_runtime import (
        run_chip_consensus_worker_runtime,
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

    with patch("asyncio.sleep", _sleep):
        await run_chip_consensus_worker_runtime(
            session_factory=session_factory,
            heartbeat_loop=_fake_heartbeat,
            recover_stale_job_runs=_recover,
            poll_once=_poll,
            should_shutdown=lambda: shutdown[0],
            worker_interval=lambda: 5,
            logger=logger,
        )

    assert poll_calls["n"] == 1
    assert sleep_calls == [], "shutdown 后不应 sleep"


# =============================================================================
# 9. 正常 sleep 使用 worker_interval() 当前值（动态 7→11）
# =============================================================================


@pytest.mark.asyncio
async def test_sleep_uses_current_worker_interval_dynamically() -> None:
    from app.services.chip_consensus_worker_runtime import (
        run_chip_consensus_worker_runtime,
    )

    sleep_calls: list[int] = []
    real_sleep = asyncio.sleep
    interval_holder = {"v": 7}

    def _sleep(sec: int):
        sleep_calls.append(sec)
        if len(sleep_calls) >= 2:
            shutdown[0] = True
        else:
            interval_holder["v"] = 11  # 第一次 sleep 后改为 11
        return real_sleep(0)

    shutdown = [False]

    async def _recover(db) -> int:
        return 0

    async def _poll() -> bool:
        return False

    session_factory = _build_fake_session_local([_FakeSession()])
    logger = MagicMock()

    with patch("asyncio.sleep", _sleep):
        await run_chip_consensus_worker_runtime(
            session_factory=session_factory,
            heartbeat_loop=_fake_heartbeat,
            recover_stale_job_runs=_recover,
            poll_once=_poll,
            should_shutdown=lambda: shutdown[0],
            worker_interval=lambda: interval_holder["v"],
            logger=logger,
        )

    assert sleep_calls == [7, 11], "sleep 应使用 worker_interval() 当前值（动态）"


# =============================================================================
# 10. façade 注入全部正确
# =============================================================================


@pytest.mark.asyncio
async def test_worker_facade_delegates_with_injection() -> None:
    import app.worker as worker_mod

    with patch(
        "app.services.chip_consensus_worker_runtime.run_chip_consensus_worker_runtime",
        new=AsyncMock(return_value=None),
    ) as mock_runtime:
        await worker_mod.run_chip_consensus_worker()

    mock_runtime.assert_awaited_once()
    _, kwargs = mock_runtime.call_args
    assert kwargs["session_factory"] is worker_mod.AsyncSessionLocal
    assert kwargs["heartbeat_loop"] is worker_mod._heartbeat_loop
    assert kwargs["recover_stale_job_runs"] is worker_mod.recover_stale_scheduler_job_runs
    assert kwargs["poll_once"] is worker_mod._chip_consensus_poll_once
    assert callable(kwargs["should_shutdown"])
    assert callable(kwargs["worker_interval"])
    assert kwargs["worker_interval"]() == worker_mod.WORKER_INTERVAL
    assert kwargs["logger"] is worker_mod.logger
