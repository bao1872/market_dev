# PURE_UNIT_TEST=1
"""[2026-08-11 AFTER-CLOSE-ENHANCEMENT-HEAD-OF-LINE-BLOCKING] executor-level 调度合同测试。

针对 ROOT CAUSE 的真实调度行为测试（不连接数据库、不启动真实 Scheduler）。

当前架构（W5A 之后）：
- AfterClose Worker（唯一 mandatory executor）主循环只领取/执行 after_close_orchestrator；
- 进程内只有一个 in-process co-process：Auction Scheduler
  （由 worker._run_auction_scheduler_co_process 持有，通过 façade 注入 runtime 启动）；
- Chip 自动 consensus 已退役（CHIP-RETIRE 2026-09-01）：AfterClose 不再启动 Chip
  co-process；chip 专用 worker 入口 run_chip_consensus_worker 仍保留给
  WORKER_TYPE=chip_consensus 独立调试/调度；
- Review bootstrap co-process 已在前序 REVIEW-BACKEND-FINAL-CLOSURE Phase 5 物理删除
  （run_review_bootstrap_worker / _review_bootstrap_poll_once 不再存在）。

本文件用可控 asyncio.Event 驱动真实 worker 调度函数，验证：
  1. AUCTION IN-FLIGHT       → mandatory 主循环仍 poll/claim（不被 secondary work 阻塞）
  2. REVIEW BOOTSTRAP RETIRE → worker 不再暴露/执行 Review bootstrap 符号
  3. CHIP STANDALONE        → 无 mandatory 时 run_chip_consensus_worker 仍可独立执行
  4. NO CHIP DRIVEN BY AFTERCLOSE → AfterClose runtime 不驱动 chip poll / chip worker
  5. ACTIVE AUCTION + SHUTDOWN → 无裸取消，drain 到 terminal

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_worker_executor_isolation.py -v
"""
from __future__ import annotations

import asyncio
import inspect
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
# TEST 1 — Auction in-flight → mandatory main loop still polls/claims
# =============================================================================


@pytest.mark.asyncio
async def test_mandatory_loop_claims_while_chip_not_terminal() -> None:
    """TEST 1 — 核心合同①（当前化）：Auction co-process in-flight 时，mandatory 主循环仍 poll。

    驱动真实 `run_after_close_orchestrator_worker`：
    - Auction co-process 用可控 asyncio.Event 保持「开始执行但未完成」；
    - 断言：Auction 未 terminal 时 `_after_close_poll_once` 仍被调用（mandatory 不被
      secondary co-process 阻塞的 head-of-line-blocking 合同保持）；
    - 守护：当前 in-process secondary co-process 只剩 Auction，mandatory 主循环不直接
      调用 `_chip_consensus_poll_once` / `_review_bootstrap_poll_once`（均已退役）。
    """
    import app.worker as worker_mod
    from app.worker import run_after_close_orchestrator_worker

    auction_in_flight = asyncio.Event()
    auction_release = asyncio.Event()
    auction_finished = asyncio.Event()
    poll_calls: list[int] = []

    async def _fake_auction_co_process() -> None:
        # 模拟 Auction 任务：开始执行，但用可控 await 保持未完成
        auction_in_flight.set()
        await auction_release.wait()
        auction_finished.set()

    async def _fake_after_close_poll_once() -> bool:
        # mandatory 主循环每轮都会调用（即使 Auction 仍 in-flight）
        poll_calls.append(1)
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
             patch.object(worker_mod, "_after_close_poll_once", _fake_after_close_poll_once), \
             patch.object(worker_mod, "AsyncSessionLocal", fake_session), \
             patch("app.worker.recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.recover_replaced_incarnation_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.auto_resume_interrupted_after_close_runs", new=AsyncMock(return_value=0)):
            worker_task = asyncio.create_task(run_after_close_orchestrator_worker())

            await asyncio.wait_for(auction_in_flight.wait(), timeout=3)
            for _ in range(10):
                await asyncio.sleep(0.01)

            assert auction_finished.is_set() is False, "测试前提失效：Auction 不应已 terminal"
            assert poll_calls, (
                "FAIL: Auction 未 terminal 时 mandatory 主循环仍应 poll；"
                "secondary co-process 阻塞 mandatory 的 head-of-line-blocking 仍在"
            )
    finally:
        worker_mod._shutdown = True
        auction_release.set()
        await asyncio.sleep(0.05)
        if worker_task is not None:
            await asyncio.wait_for(worker_task, timeout=5)
        worker_mod._shutdown = saved_shutdown
        worker_mod.WORKER_INTERVAL = saved_interval


# =============================================================================
# TEST 2 — Review bootstrap fully retired from AfterClose executor wiring
# =============================================================================


@pytest.mark.asyncio
async def test_mandatory_loop_claims_while_bootstrap_not_terminal() -> None:
    """TEST 2 — 核心合同②（当前化）：Review bootstrap 已彻底退出 AfterClose executor wiring。

    - worker 不再暴露 `run_review_bootstrap_worker` / `_review_bootstrap_poll_once`；
    - AfterClose runtime 不得有上述符号的**执行调用**（注释里提及"已物理删除"不算）；
    - 真实跑一次 AfterClose worker：在无 bootstrap 符号的前提下正常启动 + drain 退出，
      证明 runtime 不再尝试启动 bootstrap co-process。
    """
    import app.services.after_close_orchestrator_worker_runtime as rt_mod
    import app.worker as worker_mod
    from app.worker import run_after_close_orchestrator_worker

    # 1. 生产代码不再暴露退役符号
    assert not hasattr(worker_mod, "run_review_bootstrap_worker"), (
        "Review bootstrap co-process 必须已退役（worker 不应再暴露 run_review_bootstrap_worker）"
    )
    assert not hasattr(worker_mod, "_review_bootstrap_poll_once"), (
        "Review bootstrap poll 必须已退役（worker 不应再暴露 _review_bootstrap_poll_once）"
    )

    # 2. runtime 不得执行退役符号（仅检查调用形态，注释不算）
    rt_src = inspect.getsource(rt_mod.run_after_close_orchestrator_worker_runtime)
    assert "run_review_bootstrap_worker(" not in rt_src, (
        "AfterClose runtime 不得执行 run_review_bootstrap_worker"
    )
    assert "_review_bootstrap_poll_once(" not in rt_src, (
        "AfterClose runtime 不得执行 _review_bootstrap_poll_once"
    )

    # 3. 真实跑一次：无 bootstrap 符号也能正常启动 + drain
    auction_in_flight = asyncio.Event()
    auction_release = asyncio.Event()

    async def _fake_auction_co_process() -> None:
        auction_in_flight.set()
        await auction_release.wait()

    async def _fake_poll() -> bool:
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
             patch.object(worker_mod, "_after_close_poll_once", _fake_poll), \
             patch.object(worker_mod, "AsyncSessionLocal", fake_session), \
             patch("app.worker.recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.recover_replaced_incarnation_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.auto_resume_interrupted_after_close_runs", new=AsyncMock(return_value=0)):
            worker_task = asyncio.create_task(run_after_close_orchestrator_worker())
            await asyncio.wait_for(auction_in_flight.wait(), timeout=3)
            # 启动成功且未尝试 bootstrap 启动（无 AttributeError）
    finally:
        worker_mod._shutdown = True
        auction_release.set()
        await asyncio.sleep(0.05)
        if worker_task is not None:
            await asyncio.wait_for(worker_task, timeout=5)
        worker_mod._shutdown = saved_shutdown
        worker_mod.WORKER_INTERVAL = saved_interval


# =============================================================================
# TEST 3 — chip worker entry still executes without mandatory (kept)
# =============================================================================


@pytest.mark.asyncio
async def test_chip_co_process_still_executes_without_mandatory() -> None:
    """TEST 3 — 无 mandatory run 时，chip worker 入口 run_chip_consensus_worker 仍可独立执行。

    历史兼容：CHIP-RETIRE 后 chip 自动 consensus 不再作为 AfterClose co-process 启动，
    但 WORKER_TYPE=chip_consensus 的独立 worker 入口仍保留（要求③证据之一）。
    """
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
                "FAIL: 无 mandatory run 时 chip worker 入口仍应执行 chip poll"
            )
    finally:
        worker_mod._shutdown = True
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
        worker_mod._shutdown = saved_shutdown
        worker_mod.WORKER_INTERVAL = saved_interval


# =============================================================================
# TEST 4 — AfterClose runtime does not drive chip poll / chip worker
# =============================================================================


@pytest.mark.asyncio
async def test_chip_poll_driven_only_by_single_co_process() -> None:
    """TEST 4 — 无重复 chip 领取：AfterClose runtime 不驱动 chip poll / chip worker。

    当前"no duplicate chip claim"的正确定义：
    - AfterClose runtime（run_after_close_orchestrator_worker_runtime）不得调用
      run_chip_consensus_worker / _chip_consensus_poll_once / 创建 chip task；
    - 唯一的 chip 驱动源是独立 `run_chip_consensus_worker` worker（TEST 3）。
    """
    import app.services.after_close_orchestrator_worker_runtime as rt_mod
    import app.worker as worker_mod
    from app.worker import run_after_close_orchestrator_worker

    # 1. runtime 源码不得执行 chip 相关符号（注释提及不算）
    rt_src = inspect.getsource(rt_mod.run_after_close_orchestrator_worker_runtime)
    assert "run_chip_consensus_worker(" not in rt_src, (
        "AfterClose runtime 不得执行 run_chip_consensus_worker"
    )
    assert "_chip_consensus_poll_once(" not in rt_src, (
        "AfterClose runtime 不得执行 _chip_consensus_poll_once"
    )

    # 2. 真实跑 AfterClose worker：chip poll 不得被 AfterClose 主循环驱动
    chip_poll_total: list[int] = []
    auction_release = asyncio.Event()

    async def _fake_auction_co_process() -> None:
        await auction_release.wait()

    async def _real_guard_chip_poll() -> bool:
        chip_poll_total.append(1)
        return False

    async def _fake_poll() -> bool:
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
             patch.object(worker_mod, "_chip_consensus_poll_once", _real_guard_chip_poll), \
             patch.object(worker_mod, "_after_close_poll_once", _fake_poll), \
             patch.object(worker_mod, "AsyncSessionLocal", fake_session), \
             patch("app.worker.recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.recover_replaced_incarnation_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.auto_resume_interrupted_after_close_runs", new=AsyncMock(return_value=0)):
            worker_task = asyncio.create_task(run_after_close_orchestrator_worker())
            for _ in range(10):
                await asyncio.sleep(0.01)
            assert not chip_poll_total, (
                "FAIL: AfterClose 主循环不应驱动 chip poll（存在重复 chip 领取风险）"
            )
    finally:
        worker_mod._shutdown = True
        auction_release.set()
        await asyncio.sleep(0.05)
        if worker_task is not None:
            await asyncio.wait_for(worker_task, timeout=5)
        worker_mod._shutdown = saved_shutdown
        worker_mod.WORKER_INTERVAL = saved_interval


# =============================================================================
# TEST 5 — active Auction job + shutdown → no naked cancellation, drain to terminal
# =============================================================================


@pytest.mark.asyncio
async def test_shutdown_drains_active_long_jobs_without_naked_cancel() -> None:
    """TEST 5 — ACTIVE AUCTION JOB + SHUTDOWN → 无裸取消，drain 到 terminal。

    模拟 Auction 已 claim 一个 job，进入真实业务执行边界并保持 in-flight（未 terminal）。
    随后 `_shutdown = True`：
    - 父进程不得裸 Task.cancel() 在飞行中的 Auction 业务操作；
    - 父进程应持续 drain（等待 in-flight 业务到达 terminal），不因固定 timeout 直接取消；
    - 释放业务操作后，Auction 正常 drain 到 terminal（完成而非被取消）。
    """
    import app.worker as worker_mod
    from app.worker import run_after_close_orchestrator_worker

    auction_started = asyncio.Event()
    auction_release = asyncio.Event()
    auction_done = asyncio.Event()
    auction_cancelled = asyncio.Event()

    async def _fake_auction_co_process() -> None:
        # 模拟 Auction 已 claim 一个 job，进入真实业务执行边界并保持 in-flight
        auction_started.set()
        try:
            await auction_release.wait()
        except asyncio.CancelledError:
            auction_cancelled.set()
            raise
        auction_done.set()

    async def _fake_poll() -> bool:
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
             patch.object(worker_mod, "_after_close_poll_once", _fake_poll), \
             patch.object(worker_mod, "AsyncSessionLocal", fake_session), \
             patch("app.worker.recover_stale_scheduler_job_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.recover_replaced_incarnation_runs", new=AsyncMock(return_value=0)), \
             patch("app.worker.auto_resume_interrupted_after_close_runs", new=AsyncMock(return_value=0)):
            worker_task = asyncio.create_task(run_after_close_orchestrator_worker())

            # Auction 进入 in-flight 业务执行边界
            await asyncio.wait_for(auction_started.wait(), timeout=3)

            # 触发 shutdown
            worker_mod._shutdown = True

            # 给父进程 drain 时间：不应裸取消在飞行中的 Auction 业务操作
            await asyncio.sleep(0.1)
            assert not auction_cancelled.is_set(), (
                "FAIL: shutdown 不得裸 Task.cancel 在飞行中的 Auction 业务操作"
            )
            # 父进程应持续 drain（等待 in-flight 业务到达 terminal），不应提前返回
            assert not worker_task.done(), (
                "FAIL: 父进程应在 Auction 业务完成前持续 drain（不得提前退出）"
            )

            # 释放业务操作 → 应 drain 到 terminal（完成而非被取消）
            auction_release.set()
            await asyncio.wait_for(worker_task, timeout=5)

            assert auction_done.is_set(), "Auction 业务操作应 drain 到 terminal（完成而非被取消）"
            assert not auction_cancelled.is_set(), "Auction 业务操作不得被裸取消"
    finally:
        worker_mod._shutdown = saved_shutdown
        worker_mod.WORKER_INTERVAL = saved_interval
