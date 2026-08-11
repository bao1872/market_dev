# PURE_UNIT_TEST=1
"""[2026-08-11 AFTER-CLOSE-ENHANCEMENT-HEAD-OF-LINE-BLOCKING] executor-level 调度合同测试。

针对 ROOT CAUSE 的真实调度行为测试（不连接数据库、不启动真实 Scheduler）。

背景：
- 旧 worker.py 的 `run_after_close_orchestrator_worker` 主循环里：
  `_after_close_poll_once()` 之后 fallback 到 `_chip_consensus_poll_once()`，
  领取 chip 后串行 `await execute_after_close_chip_consensus(...)` 直到 terminal。
  这造成 head-of-line blocking：chip 长任务占用 mandatory after-close / Review
  的唯一 executor，导致新的 after_close orchestrator（queued/resume_queued）
  无法被领取，computing_review 被 chip 阻塞（Runtime 已真实复现）。

- 修复后：chip consensus 以**独立 co-process**（复用 `run_chip_consensus_worker`）
  在同一进程运行，mandatory 主循环只领取/执行 after_close_orchestrator，
  不再串行 fallback chip。因此 chip 未 terminal 时 mandatory run 仍可被领取。

本文件用可控 asyncio.Event 驱动真实 worker 调度函数，
验证「CHIP TASK NOT TERMINAL 时 MANDATORY AFTER-CLOSE 仍可被 CLAIM/START」。

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_worker_executor_isolation.py -v
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


class _FakeSession:
    """最小 AsyncSession 替代：只提供 worker 启动恢复所需的 commit/rollback。"""

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _FakeSessionFactory:
    """每次 `AsyncSessionLocal()` 返回一个 fake session（供 worker 启动恢复）。"""

    def __call__(self) -> _FakeSession:
        return _FakeSession()


async def _noop_heartbeat(_name: str) -> None:
    """替换 _heartbeat_loop，避免在纯单元测试里连库/长循环。"""
    return None


# =============================================================================
# TEST 1 — mandatory loop does not serially await chip（本轮最重要测试）
# =============================================================================


@pytest.mark.asyncio
async def test_mandatory_loop_claims_while_chip_not_terminal() -> None:
    """TEST 1 — 核心合同：Chip 任务开始但未 terminal 时，mandatory after-close 仍可被领取。

    驱动真实 `run_after_close_orchestrator_worker` 调度 harness：
    - chip co-process 用可控 asyncio.Event 保持「开始执行但未完成」；
    - 模拟新的 after_close orchestrator queued；
    - 断言：在 chip 未 terminal 时，`_after_close_poll_once` 仍再次运行并可领取 mandatory run。
    - 守护：mandatory 主循环不得直接调用 `_chip_consensus_poll_once`（避免重复 chip 领取）。
    """
    import app.worker as worker_mod
    from app.worker import run_after_close_orchestrator_worker

    chip_in_flight = asyncio.Event()
    chip_release = asyncio.Event()
    chip_finished = asyncio.Event()
    chip_poll_calls: list[int] = []
    mandatory_claims: list[bool] = []

    async def _fake_chip_co_process() -> None:
        # 模拟 chip 任务：开始执行，但用可控 await 保持未完成
        chip_in_flight.set()
        await chip_release.wait()
        chip_finished.set()

    async def _fake_chip_poll_once() -> bool:
        # 若 mandatory 主循环仍直接调用 chip poll，则此处会被记录（应始终为空）
        chip_poll_calls.append(1)
        return False

    async def _fake_auction_co_process() -> None:
        while not worker_mod._shutdown:
            await asyncio.sleep(0.005)

    async def _fake_after_close_poll_once() -> bool:
        # 仅当 chip 已 in-flight 且尚未完成时，模拟新的 mandatory run 可被领取
        if chip_in_flight.is_set() and not chip_finished.is_set():
            mandatory_claims.append(True)
            return True
        return False

    async def _fake_review_bootstrap_poll_once() -> bool:
        return False

    fake_session = _FakeSessionFactory()

    saved_shutdown = worker_mod._shutdown
    saved_interval = worker_mod.WORKER_INTERVAL
    worker_mod._shutdown = False
    worker_mod.WORKER_INTERVAL = 0

    worker_task: asyncio.Task[None] | None = None
    try:
        with patch.object(worker_mod, "_heartbeat_loop", _noop_heartbeat), \
             patch.object(worker_mod, "_run_auction_scheduler_co_process", _fake_auction_co_process), \
             patch.object(worker_mod, "run_chip_consensus_worker", _fake_chip_co_process), \
             patch.object(worker_mod, "_chip_consensus_poll_once", _fake_chip_poll_once), \
             patch.object(worker_mod, "_after_close_poll_once", _fake_after_close_poll_once), \
             patch.object(worker_mod, "_review_bootstrap_poll_once", _fake_review_bootstrap_poll_once), \
             patch.object(worker_mod, "AsyncSessionLocal", fake_session), \
             patch("app.worker.recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.auto_resume_interrupted_after_close_runs", new=AsyncMock(return_value=0)):
            worker_task = asyncio.create_task(run_after_close_orchestrator_worker())

            # 等待 chip co-process 进入 in-flight
            await asyncio.wait_for(chip_in_flight.wait(), timeout=3)

            # 让 mandatory loop 再轮询若干次（期间 chip 保持未 terminal）
            for _ in range(10):
                await asyncio.sleep(0.01)

            # 前提：chip 必须仍未完成
            assert chip_finished.is_set() is False, "测试前提失效：chip 不应已 terminal"

            # 核心断言：Chip 未 terminal 时 mandatory run 必须仍可被领取
            assert mandatory_claims, (
                "FAIL: Chip 未 terminal 时 mandatory run 未能被 _after_close_poll_once 领取，"
                "head-of-line blocking 仍在"
            )

            # 守护：mandatory 主循环不得直接调用 chip poll（否则可能造成重复 chip 领取）
            assert not chip_poll_calls, (
                "FAIL: mandatory 主循环仍直接调用 _chip_consensus_poll_once，"
                "应只在独立 chip co-process 内调用"
            )
    finally:
        worker_mod._shutdown = True
        chip_release.set()  # 释放 chip co-process，允许 drain
        await asyncio.sleep(0.05)
        if worker_task is not None:
            await asyncio.wait_for(worker_task, timeout=5)
        worker_mod._shutdown = saved_shutdown
        worker_mod.WORKER_INTERVAL = saved_interval


# =============================================================================
# TEST 2 — Chip still executes when no mandatory run
# =============================================================================


@pytest.mark.asyncio
async def test_chip_co_process_still_executes_without_mandatory() -> None:
    """TEST 2 — 无 mandatory run 时，chip co-process 仍可领取/轮询 chip job。

    运行真实 `run_chip_consensus_worker`（作为 co-process 的复用实现），
    其独立 loop 每轮调用 `_chip_consensus_poll_once`，与 mandatory 主循环无关。
    """
    import app.worker as worker_mod
    from app.worker import run_chip_consensus_worker

    chip_polled = asyncio.Event()
    chip_claim_attempts: list[bool] = []

    async def _fake_chip_poll_once() -> bool:
        chip_claim_attempts.append(True)
        chip_polled.set()
        return False  # 无可领取 chip job，但 poll 已执行（co-process 仍在工作）

    fake_session = _FakeSessionFactory()

    saved_shutdown = worker_mod._shutdown
    saved_interval = worker_mod.WORKER_INTERVAL
    worker_mod._shutdown = False
    worker_mod.WORKER_INTERVAL = 0

    task: asyncio.Task[None] | None = None
    try:
        with patch.object(worker_mod, "_heartbeat_loop", _noop_heartbeat), \
             patch.object(worker_mod, "_chip_consensus_poll_once", _fake_chip_poll_once), \
             patch.object(worker_mod, "AsyncSessionLocal", fake_session), \
             patch("app.worker.recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0)):
            task = asyncio.create_task(run_chip_consensus_worker())
            await asyncio.wait_for(chip_polled.wait(), timeout=3)
            assert chip_claim_attempts, (
                "FAIL: 无 mandatory run 时 chip co-process 仍应执行 chip poll"
            )
    finally:
        worker_mod._shutdown = True
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
        worker_mod._shutdown = saved_shutdown
        worker_mod.WORKER_INTERVAL = saved_interval


# =============================================================================
# TEST 3 — no duplicate Chip claim (single co-process drives chip poll)
# =============================================================================


@pytest.mark.asyncio
async def test_chip_poll_driven_only_by_single_co_process() -> None:
    """TEST 3 — 无重复 chip 领取：chip poll 仅由唯一 co-process 驱动，mandatory 主循环不驱动。

    全 worker harness 下，`_chip_consensus_poll_once` 只应被 `run_chip_consensus_worker`
    这一个 co-process 调用。mandatory 主循环不得触发它，避免 co-process 改造后
    同一 chip job 被双重领取/执行。
    """
    import app.worker as worker_mod
    from app.worker import run_after_close_orchestrator_worker

    chip_poll_total: list[int] = []
    chip_release = asyncio.Event()

    async def _fake_chip_co_process() -> None:
        # 单 co-process 是 chip poll 的唯一驱动者
        await chip_release.wait()

    async def _real_guard_chip_poll() -> bool:
        # 若 mandatory 主循环仍直接调用它，将在此被计数
        chip_poll_total.append(1)
        return False

    async def _fake_after_close_poll_once() -> bool:
        return False

    async def _fake_review_bootstrap_poll_once() -> bool:
        return False

    async def _fake_auction_co_process() -> None:
        while not worker_mod._shutdown:
            await asyncio.sleep(0.005)

    fake_session = _FakeSessionFactory()

    saved_shutdown = worker_mod._shutdown
    saved_interval = worker_mod.WORKER_INTERVAL
    worker_mod._shutdown = False
    worker_mod.WORKER_INTERVAL = 0

    worker_task: asyncio.Task[None] | None = None
    try:
        with patch.object(worker_mod, "_heartbeat_loop", _noop_heartbeat), \
             patch.object(worker_mod, "_run_auction_scheduler_co_process", _fake_auction_co_process), \
             patch.object(worker_mod, "run_chip_consensus_worker", _fake_chip_co_process), \
             patch.object(worker_mod, "_chip_consensus_poll_once", _real_guard_chip_poll), \
             patch.object(worker_mod, "_after_close_poll_once", _fake_after_close_poll_once), \
             patch.object(worker_mod, "_review_bootstrap_poll_once", _fake_review_bootstrap_poll_once), \
             patch.object(worker_mod, "AsyncSessionLocal", fake_session), \
             patch("app.worker.recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.auto_resume_interrupted_after_close_runs", new=AsyncMock(return_value=0)):
            worker_task = asyncio.create_task(run_after_close_orchestrator_worker())
            # 让 mandatory loop 轮询若干次
            for _ in range(10):
                await asyncio.sleep(0.01)
            assert not chip_poll_total, (
                "FAIL: mandatory 主循环不应驱动 chip poll（存在重复 chip 领取风险）"
            )
    finally:
        worker_mod._shutdown = True
        chip_release.set()
        await asyncio.sleep(0.05)
        if worker_task is not None:
            await asyncio.wait_for(worker_task, timeout=5)
        worker_mod._shutdown = saved_shutdown
        worker_mod.WORKER_INTERVAL = saved_interval


# =============================================================================
# TEST 4 — shutdown / drain
# =============================================================================


@pytest.mark.asyncio
async def test_shutdown_drains_mandatory_chip_auction() -> None:
    """TEST 4 — shutdown / drain：SIGTERM/_shutdown 时 mandatory、chip、auction 都能进入 drain。

    验证 co-process（chip / auction）随 `_shutdown` 进入 drain 并自然退出，
    不强制杀掉正在执行中的业务任务。
    """
    import app.worker as worker_mod
    from app.worker import run_after_close_orchestrator_worker

    chip_started = asyncio.Event()
    chip_finished = asyncio.Event()
    auction_finished = asyncio.Event()

    async def _fake_chip_co_process() -> None:
        chip_started.set()
        try:
            while not worker_mod._shutdown:
                await asyncio.sleep(0.005)
        finally:
            chip_finished.set()

    async def _fake_auction_co_process() -> None:
        try:
            while not worker_mod._shutdown:
                await asyncio.sleep(0.005)
        finally:
            auction_finished.set()

    async def _fake_after_close_poll_once() -> bool:
        return False

    async def _fake_review_bootstrap_poll_once() -> bool:
        return False

    fake_session = _FakeSessionFactory()

    saved_shutdown = worker_mod._shutdown
    saved_interval = worker_mod.WORKER_INTERVAL
    worker_mod._shutdown = False
    worker_mod.WORKER_INTERVAL = 0

    worker_task: asyncio.Task[None] | None = None
    try:
        with patch.object(worker_mod, "_heartbeat_loop", _noop_heartbeat), \
             patch.object(worker_mod, "_run_auction_scheduler_co_process", _fake_auction_co_process), \
             patch.object(worker_mod, "run_chip_consensus_worker", _fake_chip_co_process), \
             patch.object(worker_mod, "_after_close_poll_once", _fake_after_close_poll_once), \
             patch.object(worker_mod, "_review_bootstrap_poll_once", _fake_review_bootstrap_poll_once), \
             patch.object(worker_mod, "AsyncSessionLocal", fake_session), \
             patch("app.worker.recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.auto_resume_interrupted_after_close_runs", new=AsyncMock(return_value=0)):
            worker_task = asyncio.create_task(run_after_close_orchestrator_worker())
            await asyncio.wait_for(chip_started.wait(), timeout=3)

            # 触发 shutdown：mandatory 与两个 co-process 均应进入 drain
            worker_mod._shutdown = True
            await asyncio.wait_for(worker_task, timeout=5)

            assert chip_finished.is_set(), "chip co-process 应随 _shutdown 进入 drain 并退出"
            assert auction_finished.is_set(), "auction co-process 应随 _shutdown 进入 drain 并退出"
    finally:
        worker_mod._shutdown = saved_shutdown
        worker_mod.WORKER_INTERVAL = saved_interval
