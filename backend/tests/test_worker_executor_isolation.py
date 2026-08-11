# PURE_UNIT_TEST=1
"""[2026-08-11 AFTER-CLOSE-ENHANCEMENT-HEAD-OF-LINE-BLOCKING] executor-level 调度合同测试。

针对 ROOT CAUSE 的真实调度行为测试（不连接数据库、不启动真实 Scheduler）。

背景：
- 旧 worker.py 的 `run_after_close_orchestrator_worker` 主循环里串行 fallback 到
  `_chip_consensus_poll_once()` / `_review_bootstrap_poll_once()`，领取后 await 到
  terminal，造成 head-of-line blocking：long-running secondary job 占用 mandatory
  after-close / Review 唯一 executor，导致新的 after_close orchestrator 无法被领取。

- 修复后：chip consensus 与 review bootstrap 均以**独立 co-process**（复用
  `run_chip_consensus_worker` / `run_review_bootstrap_worker`）在同一进程运行，
  mandatory 主循环只领取/执行 after_close_orchestrator。shutdown 时父进程对
  co-process 只 await（drain 到当前业务 item terminal），**禁止裸 Task.cancel()**
  遗留 ownership 不清的 running job。

本文件用可控 asyncio.Event 驱动真实 worker 调度函数，验证：
  1. CHIP NOT TERMINAL       → mandatory can claim
  2. BOOTSTRAP NOT TERMINAL  → mandatory can claim
  3. chip 独立 co-process 在无 mandatory 时仍执行
  4. chip poll 仅由唯一 co-process 驱动（无重复领取）
  5. ACTIVE LONG JOB + SHUTDOWN → 无裸取消，drain 到 terminal

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


def _idle_co_process_until_shutdown():
    """构造一个 idle co-process：循环直到 _shutdown（用于不关心其 drain 时机的测试）。"""

    async def _run() -> None:
        import app.worker as worker_mod

        while not worker_mod._shutdown:
            await asyncio.sleep(0.005)

    return _run


# =============================================================================
# TEST 1 — chip not terminal → mandatory can claim
# =============================================================================


@pytest.mark.asyncio
async def test_mandatory_loop_claims_while_chip_not_terminal() -> None:
    """TEST 1 — 核心合同①：Chip 未 terminal 时，mandatory after-close 仍可被领取。

    驱动真实 `run_after_close_orchestrator_worker`：
    - chip co-process 用可控 asyncio.Event 保持「开始执行但未完成」；
    - 断言：chip 未 terminal 时 `_after_close_poll_once` 仍运行并可领取 mandatory run；
    - 守护：mandatory 主循环不得直接调用 `_chip_consensus_poll_once`。
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

    async def _fake_bootstrap_poll_once() -> bool:
        return False

    async def _fake_after_close_poll_once() -> bool:
        # 仅当 chip 已 in-flight 且尚未完成时，模拟新的 mandatory run 可被领取
        if chip_in_flight.is_set() and not chip_finished.is_set():
            mandatory_claims.append(True)
            return True
        return False

    fake_session = _FakeSessionFactory()

    saved_shutdown = worker_mod._shutdown
    saved_interval = worker_mod.WORKER_INTERVAL
    worker_mod._shutdown = False
    worker_mod.WORKER_INTERVAL = 0

    worker_task: asyncio.Task[None] | None = None
    try:
        with patch.object(worker_mod, "_heartbeat_loop", _noop_heartbeat), \
             patch.object(worker_mod, "_run_auction_scheduler_co_process", _idle_co_process_until_shutdown()), \
             patch.object(worker_mod, "run_chip_consensus_worker", _fake_chip_co_process), \
             patch.object(worker_mod, "run_review_bootstrap_worker", _idle_co_process_until_shutdown()), \
             patch.object(worker_mod, "_chip_consensus_poll_once", _fake_chip_poll_once), \
             patch.object(worker_mod, "_review_bootstrap_poll_once", _fake_bootstrap_poll_once), \
             patch.object(worker_mod, "_after_close_poll_once", _fake_after_close_poll_once), \
             patch.object(worker_mod, "AsyncSessionLocal", fake_session), \
             patch("app.worker.recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.auto_resume_interrupted_after_close_runs", new=AsyncMock(return_value=0)):
            worker_task = asyncio.create_task(run_after_close_orchestrator_worker())

            await asyncio.wait_for(chip_in_flight.wait(), timeout=3)
            for _ in range(10):
                await asyncio.sleep(0.01)

            assert chip_finished.is_set() is False, "测试前提失效：chip 不应已 terminal"
            assert mandatory_claims, (
                "FAIL: Chip 未 terminal 时 mandatory run 未能被 _after_close_poll_once 领取，"
                "head-of-line blocking 仍在"
            )
            assert not chip_poll_calls, (
                "FAIL: mandatory 主循环仍直接调用 _chip_consensus_poll_once"
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
# TEST 2 — bootstrap not terminal → mandatory can claim
# =============================================================================


@pytest.mark.asyncio
async def test_mandatory_loop_claims_while_bootstrap_not_terminal() -> None:
    """TEST 2 — 核心合同②：Review Bootstrap 未 terminal 时，mandatory 仍可被领取。

    bootstrap co-process 用可控 asyncio.Event 保持 in-flight（未 terminal），
    断言 `_after_close_poll_once` 仍执行并可领取 mandatory run，
    且 mandatory 主循环不直接调用 `_review_bootstrap_poll_once`。
    """
    import app.worker as worker_mod
    from app.worker import run_after_close_orchestrator_worker

    bootstrap_in_flight = asyncio.Event()
    bootstrap_release = asyncio.Event()
    bootstrap_finished = asyncio.Event()
    bootstrap_poll_calls: list[int] = []
    mandatory_claims: list[bool] = []

    async def _fake_bootstrap_co_process() -> None:
        # 模拟 bootstrap 任务：开始执行，但用可控 await 保持未完成
        bootstrap_in_flight.set()
        await bootstrap_release.wait()
        bootstrap_finished.set()

    async def _fake_bootstrap_poll_once() -> bool:
        # 若 mandatory 主循环仍直接调用 bootstrap poll，则此处会被记录（应仅由
        # run_review_bootstrap_worker co-process 调用，但本测试 patch 掉 co-process，
        # 因此 mandatory 若直接调用它会被计数）
        bootstrap_poll_calls.append(1)
        return False

    async def _fake_chip_co_process() -> None:
        # idle chip co-process
        while not worker_mod._shutdown:
            await asyncio.sleep(0.005)

    async def _fake_after_close_poll_once() -> bool:
        # 仅当 bootstrap 已 in-flight 且尚未完成时，模拟新的 mandatory run 可被领取
        if bootstrap_in_flight.is_set() and not bootstrap_finished.is_set():
            mandatory_claims.append(True)
            return True
        return False

    fake_session = _FakeSessionFactory()

    saved_shutdown = worker_mod._shutdown
    saved_interval = worker_mod.WORKER_INTERVAL
    worker_mod._shutdown = False
    worker_mod.WORKER_INTERVAL = 0

    worker_task: asyncio.Task[None] | None = None
    try:
        with patch.object(worker_mod, "_heartbeat_loop", _noop_heartbeat), \
             patch.object(worker_mod, "_run_auction_scheduler_co_process", _idle_co_process_until_shutdown()), \
             patch.object(worker_mod, "run_chip_consensus_worker", _fake_chip_co_process), \
             patch.object(worker_mod, "run_review_bootstrap_worker", _fake_bootstrap_co_process), \
             patch.object(worker_mod, "_chip_consensus_poll_once", lambda: _unused_never()), \
             patch.object(worker_mod, "_review_bootstrap_poll_once", _fake_bootstrap_poll_once), \
             patch.object(worker_mod, "_after_close_poll_once", _fake_after_close_poll_once), \
             patch.object(worker_mod, "AsyncSessionLocal", fake_session), \
             patch("app.worker.recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.auto_resume_interrupted_after_close_runs", new=AsyncMock(return_value=0)):
            worker_task = asyncio.create_task(run_after_close_orchestrator_worker())

            await asyncio.wait_for(bootstrap_in_flight.wait(), timeout=3)
            for _ in range(10):
                await asyncio.sleep(0.01)

            assert bootstrap_finished.is_set() is False, "测试前提失效：bootstrap 不应已 terminal"
            assert mandatory_claims, (
                "FAIL: Bootstrap 未 terminal 时 mandatory run 未能被 _after_close_poll_once 领取，"
                "head-of-line blocking 仍在"
            )
            assert not bootstrap_poll_calls, (
                "FAIL: mandatory 主循环仍直接调用 _review_bootstrap_poll_once"
            )
    finally:
        worker_mod._shutdown = True
        bootstrap_release.set()
        await asyncio.sleep(0.05)
        if worker_task is not None:
            await asyncio.wait_for(worker_task, timeout=5)
        worker_mod._shutdown = saved_shutdown
        worker_mod.WORKER_INTERVAL = saved_interval


async def _unused_never() -> bool:
    """占位：TEST 2 中 chip poll 不应被任何路径调用（co-process 也被 patch 掉）。"""
    raise AssertionError("TEST 2 中不应调用 chip poll")


# =============================================================================
# TEST 3 — chip co-process still executes without mandatory
# =============================================================================


@pytest.mark.asyncio
async def test_chip_co_process_still_executes_without_mandatory() -> None:
    """TEST 3 — 无 mandatory run 时，chip co-process 仍可领取/轮询 chip job。"""
    import app.worker as worker_mod
    from app.worker import run_chip_consensus_worker

    chip_polled = asyncio.Event()
    chip_claim_attempts: list[bool] = []

    async def _fake_chip_poll_once() -> bool:
        chip_claim_attempts.append(True)
        chip_polled.set()
        return False

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
# TEST 4 — no duplicate chip claim
# =============================================================================


@pytest.mark.asyncio
async def test_chip_poll_driven_only_by_single_co_process() -> None:
    """TEST 4 — 无重复 chip 领取：chip poll 仅由唯一 co-process 驱动，mandatory 不驱动。"""
    import app.worker as worker_mod
    from app.worker import run_after_close_orchestrator_worker

    chip_poll_total: list[int] = []
    chip_release = asyncio.Event()

    async def _fake_chip_co_process() -> None:
        await chip_release.wait()

    async def _real_guard_chip_poll() -> bool:
        chip_poll_total.append(1)
        return False

    async def _fake_after_close_poll_once() -> bool:
        return False

    async def _fake_bootstrap_poll_once() -> bool:
        return False

    fake_session = _FakeSessionFactory()

    saved_shutdown = worker_mod._shutdown
    saved_interval = worker_mod.WORKER_INTERVAL
    worker_mod._shutdown = False
    worker_mod.WORKER_INTERVAL = 0

    worker_task: asyncio.Task[None] | None = None
    try:
        with patch.object(worker_mod, "_heartbeat_loop", _noop_heartbeat), \
             patch.object(worker_mod, "_run_auction_scheduler_co_process", _idle_co_process_until_shutdown()), \
             patch.object(worker_mod, "run_chip_consensus_worker", _fake_chip_co_process), \
             patch.object(worker_mod, "run_review_bootstrap_worker", _idle_co_process_until_shutdown()), \
             patch.object(worker_mod, "_chip_consensus_poll_once", _real_guard_chip_poll), \
             patch.object(worker_mod, "_review_bootstrap_poll_once", _fake_bootstrap_poll_once), \
             patch.object(worker_mod, "_after_close_poll_once", _fake_after_close_poll_once), \
             patch.object(worker_mod, "AsyncSessionLocal", fake_session), \
             patch("app.worker.recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.auto_resume_interrupted_after_close_runs", new=AsyncMock(return_value=0)):
            worker_task = asyncio.create_task(run_after_close_orchestrator_worker())
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
# TEST 5 — active long job + shutdown → no naked cancellation, drain to terminal
# =============================================================================


@pytest.mark.asyncio
async def test_shutdown_drains_active_long_jobs_without_naked_cancel() -> None:
    """TEST 5 — ACTIVE LONG JOB + SHUTDOWN → 无裸取消，drain 到 terminal。

    模拟 chip 与 review bootstrap 都已进入真实 poll execution boundary，
    业务 operation 保持 in-flight（未 terminal）。随后 `_shutdown = True`：
    - 父进程不得裸 Task.cancel() 在飞行中的 chip / bootstrap 业务操作；
    - 父进程应持续 drain（等待 in-flight 业务到达 terminal），不因固定 timeout 直接取消；
    - 释放业务操作后，chip / bootstrap 均正常 drain 到 terminal（完成而非被取消）。
    """
    import app.worker as worker_mod
    from app.worker import run_after_close_orchestrator_worker

    chip_started = asyncio.Event()
    chip_release = asyncio.Event()
    chip_done = asyncio.Event()
    chip_cancelled = asyncio.Event()

    bootstrap_started = asyncio.Event()
    bootstrap_release = asyncio.Event()
    bootstrap_done = asyncio.Event()
    bootstrap_cancelled = asyncio.Event()

    async def _fake_chip_co_process() -> None:
        # 模拟 chip 已 claim 一个 job，进入真实业务执行边界并保持 in-flight
        chip_started.set()
        try:
            await chip_release.wait()
        except asyncio.CancelledError:
            chip_cancelled.set()
            raise
        chip_done.set()

    async def _fake_bootstrap_co_process() -> None:
        # 模拟 bootstrap 已 claim 一个 job，进入真实业务执行边界并保持 in-flight
        bootstrap_started.set()
        try:
            await bootstrap_release.wait()
        except asyncio.CancelledError:
            bootstrap_cancelled.set()
            raise
        bootstrap_done.set()

    async def _fake_auction_co_process() -> None:
        while not worker_mod._shutdown:
            await asyncio.sleep(0.005)

    async def _fake_after_close_poll_once() -> bool:
        return False

    async def _fake_chip_poll_once() -> bool:
        return False

    async def _fake_bootstrap_poll_once() -> bool:
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
             patch.object(worker_mod, "run_review_bootstrap_worker", _fake_bootstrap_co_process), \
             patch.object(worker_mod, "_chip_consensus_poll_once", _fake_chip_poll_once), \
             patch.object(worker_mod, "_review_bootstrap_poll_once", _fake_bootstrap_poll_once), \
             patch.object(worker_mod, "_after_close_poll_once", _fake_after_close_poll_once), \
             patch.object(worker_mod, "AsyncSessionLocal", fake_session), \
             patch("app.worker.recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.auto_resume_interrupted_after_close_runs", new=AsyncMock(return_value=0)):
            worker_task = asyncio.create_task(run_after_close_orchestrator_worker())

            # chip 与 bootstrap 均进入 in-flight 业务执行边界
            await asyncio.wait_for(chip_started.wait(), timeout=3)
            await asyncio.wait_for(bootstrap_started.wait(), timeout=3)

            # 触发 shutdown
            worker_mod._shutdown = True

            # 给父进程 drain 时间：不应裸取消在飞行中的 chip / bootstrap 业务操作
            await asyncio.sleep(0.1)
            assert not chip_cancelled.is_set(), (
                "FAIL: shutdown 不得裸 Task.cancel 在飞行中的 chip 业务操作"
            )
            assert not bootstrap_cancelled.is_set(), (
                "FAIL: shutdown 不得裸 Task.cancel 在飞行中的 bootstrap 业务操作"
            )
            # 父进程应持续 drain（等待 in-flight 业务到达 terminal），不应提前返回
            assert not worker_task.done(), (
                "FAIL: 父进程应在 chip / bootstrap 业务完成前持续 drain（不得提前退出）"
            )

            # 释放业务操作 → 应 drain 到 terminal（完成而非被取消）
            chip_release.set()
            bootstrap_release.set()
            await asyncio.wait_for(worker_task, timeout=5)

            assert chip_done.is_set(), "chip 业务操作应 drain 到 terminal（完成而非被取消）"
            assert bootstrap_done.is_set(), "bootstrap 业务操作应 drain 到 terminal（完成而非被取消）"
            assert not chip_cancelled.is_set(), "chip 业务操作不得被裸取消"
            assert not bootstrap_cancelled.is_set(), "bootstrap 业务操作不得被裸取消"
    finally:
        worker_mod._shutdown = saved_shutdown
        worker_mod.WORKER_INTERVAL = saved_interval
