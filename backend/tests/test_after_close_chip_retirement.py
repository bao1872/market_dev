# PURE_UNIT_TEST=1
"""[CHIP-RETIRE 2026-09-01] 自动 after_close_chip_consensus 退役行为合同测试。

背景
----
自动 chip 曾是 AfterClose 的 post-core enhancement：
`execute_after_close_run` 在 `if core_ready:` 下调用 `_enqueue_chip_job_step`
创建 `after_close_chip_consensus` job，由 after-close worker 进程内的
Chip co-process（`run_chip_consensus_worker`）领取执行。该架构带来两类复杂度：
executor isolation（长时 chip 占用 mandatory executor）与 SIGTERM 抢占
（`ChipPreemptedForShutdown` / `shutdown_check` / `requeue_owned_job_to_resume`）。

退役后 canonical chain = Core → Review → History → complete。

本文件锁定 4 条行为合同：

- TEST A：正常盘后主链成功后，新建 chip job 数 = 0
  （A1 行为证据：真实主链 spy 零调用；A2 拓扑证据：生产树无任何创建路径）。
- TEST B：after-close worker 不再启动 chip co-process
  （B1 行为证据：驱动真实 worker 后 chip 入口零调用；B2 源码守卫）。
- TEST C：Core → Review → History 合同不变（顺序与终态未被退役影响）。
- TEST D：历史 chip 兼容面未被删除，且抢占复杂度已精确回退
  （不波及其他 job 共用的 fencing helper）。

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_after_close_chip_retirement.py -v
"""
from __future__ import annotations

import ast
import asyncio
import inspect
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import app.services.after_close_chip_consensus_service as chip_svc
import app.services.after_close_orchestrator as orch
import app.worker as worker_mod
from tests.test_after_close_core_review_correction04 import T_DATE, build_harness

_APP_ROOT = Path(inspect.getsourcefile(orch)).resolve().parents[2]
_CHIP_CREATE = "create_after_close_chip_consensus_job"


def _executable_code(fn) -> str:
    """返回函数的**可执行代码**文本（剔除 docstring 与注释）。

    源码守卫必须只看真实执行语句：退役说明本身会在 docstring/注释里提到
    被移除的符号名，若直接对 `inspect.getsource` 做子串断言会自我误触。
    `ast.unparse` 天然丢弃注释，此处再显式剥离首个 docstring 表达式。
    """
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    func = tree.body[0]
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(node) for node in body)


# =============================================================================
# TEST A — 正常 AfterClose 成功 → 新建 chip job 数 = 0
# =============================================================================


@pytest.mark.asyncio
async def test_a1_successful_after_close_creates_zero_chip_jobs(monkeypatch):
    """A1（行为）：Core succeeded 的完整成功主链中，chip job 创建次数 = 0。

    证据强度说明：
    - `create_after_close_chip_consensus_job` 仍存在于服务模块（历史兼容），
      harness 在**真实服务模块**上安装录制替身，因此「零调用」是行为证据，
      而不是符号缺失造成的空断言。
    - patch 点充分性由 A2 保证：A2 已证明 app/ 下无任何模块 `from ... import`
      该符号，故服务模块属性是唯一可能的调用入口，拦截该点即可覆盖全部路径。
    - 极性已验证：本测试在退役前的生产代码上为 RED
      （`enqueue_chip_job` 步骤存在）。
    """
    h = build_harness(monkeypatch)

    with ExitStack() as st:
        for p in h["patches"]:
            st.enter_context(p)
        await orch.execute_after_close_run(h["run_id"], T_DATE)

    assert h["rec"]["chip"] == [], (
        f"退役后成功主链不得创建 chip job，实际调用: {h['rec']['chip']}"
    )
    assert "enqueue_chip_job" not in h["rec"]["steps"], (
        f"退役后主链不得出现 enqueue_chip_job 步骤，实际: {h['rec']['steps']}"
    )
    # 前提校验：该主链确实走完了 Core→Review→History（否则零 chip 无意义）
    assert "computing_review" in h["rec"]["steps"]
    assert "computing_history" in h["rec"]["steps"]


def test_a2_no_production_call_site_creates_chip_job():
    """A2（拓扑）：整个 app/ 生产树中，除定义模块自身外无任何 chip 创建调用点。

    A1 只能证明「被测那条链」零调用；A2 用 AST 扫描把结论升级为
    「不存在任何生产路径能自动创建 chip job」——这是「新建数 = 0」的充分条件。
    定义模块自身允许出现（def 定义 + `__main__` 自检的 inspect.signature）。
    """
    defining_module = Path(inspect.getsourcefile(chip_svc)).resolve()
    offenders: list[str] = []

    for py in sorted(_APP_ROOT.joinpath("app").rglob("*.py")):
        if py.resolve() == defining_module:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            # 直接调用：create_after_close_chip_consensus_job(...)
            if isinstance(node, ast.Call):
                fn = node.func
                name = (
                    fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute)
                    else None
                )
                if name == _CHIP_CREATE:
                    offenders.append(f"{py.relative_to(_APP_ROOT)}:{node.lineno} call")
            # 导入：from ... import create_after_close_chip_consensus_job
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == _CHIP_CREATE:
                        offenders.append(
                            f"{py.relative_to(_APP_ROOT)}:{node.lineno} import"
                        )

    assert offenders == [], (
        "自动 chip 已退役：app/ 下不得存在 chip job 创建路径，实际: " + str(offenders)
    )


def test_a2b_orchestrator_holds_no_chip_symbols():
    """A2b：orchestrator 既不持有 chip create 函数，也不再定义 chip 入队步骤，
    且 step timeout 预算中不再登记 `enqueue_chip_job`。"""
    assert not hasattr(orch, _CHIP_CREATE)
    assert not hasattr(orch, "_enqueue_chip_job_step")
    assert "enqueue_chip_job" not in orch._STEP_TIMEOUT_SECONDS

    main_src = inspect.getsource(orch.execute_after_close_run)
    assert _CHIP_CREATE not in main_src
    assert "_enqueue_chip_job_step" not in main_src


# =============================================================================
# TEST B — after-close worker 不再启动 chip co-process
# =============================================================================


@pytest.mark.asyncio
async def test_b1_after_close_worker_starts_no_chip_co_process():
    """B1（行为）：驱动真实 `run_after_close_orchestrator_worker`，
    chip co-process 入口与 chip poll 均零调用。

    退役前：worker 启动时 `asyncio.create_task(run_chip_consensus_worker())`，
    因此 `run_chip_consensus_worker` 必被调用。
    退役后：两者都不得被本 worker 触达（函数本身仍保留，见 TEST D）。
    """
    chip_worker_calls: list[int] = []
    chip_poll_calls: list[int] = []
    after_close_polls: list[int] = []

    async def _record_chip_worker() -> None:
        chip_worker_calls.append(1)

    async def _record_chip_poll() -> bool:
        chip_poll_calls.append(1)
        return False

    async def _fake_after_close_poll_once() -> bool:
        after_close_polls.append(1)
        return False

    async def _idle_auction_co_process() -> None:
        while not worker_mod._shutdown:
            await asyncio.sleep(0.005)

    async def _noop_heartbeat(_name: str) -> None:
        return None

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    saved_shutdown = worker_mod._shutdown
    saved_interval = worker_mod.WORKER_INTERVAL
    worker_mod._shutdown = False
    worker_mod.WORKER_INTERVAL = 0

    task: asyncio.Task[None] | None = None
    try:
        with (
            patch.object(worker_mod, "_heartbeat_loop", _noop_heartbeat),
            patch.object(
                worker_mod, "_run_auction_scheduler_co_process",
                _idle_auction_co_process,
            ),
            patch.object(
                worker_mod, "run_chip_consensus_worker", _record_chip_worker,
            ),
            patch.object(
                worker_mod, "_chip_consensus_poll_once", _record_chip_poll,
            ),
            patch.object(
                worker_mod, "_after_close_poll_once", _fake_after_close_poll_once,
            ),
            patch.object(worker_mod, "AsyncSessionLocal", _FakeSession),
            patch.object(
                worker_mod, "recover_stale_scheduler_job_runs",
                AsyncMock(return_value=0),
            ),
            patch.object(
                worker_mod, "recover_replaced_incarnation_runs",
                AsyncMock(return_value=0),
            ),
            patch.object(
                worker_mod, "auto_resume_interrupted_after_close_runs",
                AsyncMock(return_value=0),
            ),
        ):
            task = asyncio.create_task(worker_mod.run_after_close_orchestrator_worker())
            # 让主循环真实转若干轮（前提：mandatory poll 确实被驱动）
            for _ in range(10):
                await asyncio.sleep(0.01)
                if after_close_polls:
                    break
            assert after_close_polls, "测试前提失效：mandatory 主循环未被驱动"
            assert chip_worker_calls == [], (
                "退役后 after-close worker 不得启动 chip co-process"
            )
            assert chip_poll_calls == [], (
                "退役后 mandatory 主循环不得驱动 chip poll"
            )
    finally:
        worker_mod._shutdown = True
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
        worker_mod._shutdown = saved_shutdown
        worker_mod.WORKER_INTERVAL = saved_interval


def test_b2_after_close_worker_source_has_no_chip_wiring():
    """B2（源码守卫，非唯一证据）：after-close worker 体内不再引用 chip 执行入口，
    drain 段也不再 drain chip co-process。"""
    code = _executable_code(worker_mod.run_after_close_orchestrator_worker)
    assert "run_chip_consensus_worker" not in code, (
        "after-close worker 不得再启动 chip co-process"
    )
    assert "_chip_consensus_poll_once" not in code, (
        "mandatory 主循环不得再 fallback 到 chip poll"
    )
    assert "_chip_co_process_task" not in code, (
        "chip co-process task 变量应随退役一并移除"
    )
    # 注：ast.unparse 会把字符串字面量统一为单引号，故此处按 unparse 形态断言
    assert "_drain_co_process(_auction_co_process_task, 'Auction')" in code, (
        "Auction co-process 的 drain 必须保留（退役不得波及其他 co-process）"
    )


# =============================================================================
# TEST C — Core → Review → History 合同不变
# =============================================================================


@pytest.mark.asyncio
async def test_c_core_review_history_contract_unchanged(monkeypatch):
    """C：退役不改变 canonical chain —— Core gate 通过后
    Review → History 顺序执行，post-core OPTIONAL DSA 仍在其后，父任务进入终态。"""
    h = build_harness(monkeypatch)

    with ExitStack() as st:
        for p in h["patches"]:
            st.enter_context(p)
        await orch.execute_after_close_run(h["run_id"], T_DATE)

    steps = h["rec"]["steps"]
    assert "computing_review" in steps and "computing_history" in steps
    assert steps.index("computing_review") < steps.index("computing_history"), (
        f"Review 必须先于 History，实际顺序: {steps}"
    )
    assert "dsa_compatibility" in steps, "post-core OPTIONAL DSA 步骤不得被退役波及"
    assert steps.index("computing_history") < steps.index("dsa_compatibility"), (
        f"DSA 兼容性投影仍应在 History 之后，实际顺序: {steps}"
    )
    assert h["job_row"].status in ("succeeded", "partial_success"), (
        f"父任务应进入成功类终态，实际: {h['job_row'].status}"
    )
    # state_events 仍以 Core X 执行（chip 退役不得连带影响同门控下的其他副作用）
    assert h["rec"]["events"], "state_events 不得被 chip 退役波及"


@pytest.mark.asyncio
async def test_c2_core_not_ready_still_fail_closed(monkeypatch):
    """C2：Core 未就绪时仍 fail-closed —— Review/History 零调用、父任务 failed。"""
    h = build_harness(monkeypatch, core_status="failed")

    with ExitStack() as st:
        for p in h["patches"]:
            st.enter_context(p)
        with pytest.raises((orch.AfterCloseCoreNotReadyError, RuntimeError)):
            await orch.execute_after_close_run(h["run_id"], T_DATE)

    assert "computing_review" not in h["rec"]["steps"]
    assert "computing_history" not in h["rec"]["steps"]
    assert h["rec"]["chip"] == []
    assert h["job_row"].status == "failed"


# =============================================================================
# TEST D — 历史 chip 兼容面保留 + 抢占复杂度精确回退
# =============================================================================


def test_d1_chip_service_and_models_preserved():
    """D1：历史 chip 代码/模型未被删除（退役 ≠ 删除）。"""
    assert hasattr(chip_svc, _CHIP_CREATE), "chip create 服务实现必须保留（历史兼容）"
    assert hasattr(chip_svc, "execute_after_close_chip_consensus"), (
        "chip 执行实现必须保留"
    )
    assert chip_svc.CHIP_CONSENSUS_JOB_NAME == "after_close_chip_consensus", (
        "历史 job_type 字面量不得改动，否则历史 SchedulerJobRun 行无法被识别"
    )
    # 快照模型仍可导入（未做 schema cleanup / migration）
    from app.models.stock_chip_consensus_snapshot import (  # noqa: F401
        StockChipConsensusSnapshot,
    )


def test_d2_dedicated_chip_worker_entrypoint_preserved():
    """D2：chip 专用 worker 入口保留 —— 退役的是「自动启动」，不是「可执行性」。

    历史/手工重算仍可通过 WORKER_TYPE=chip_consensus 走
    `run_chip_consensus_worker` → `_chip_consensus_poll_once` 执行。
    该路径的真实调度行为由
    tests/test_worker_executor_isolation.py::test_chip_co_process_still_executes_without_mandatory
    覆盖。
    """
    assert hasattr(worker_mod, "run_chip_consensus_worker")
    assert hasattr(worker_mod, "_chip_consensus_poll_once")


def test_d3_preemption_complexity_reverted_without_touching_shared_fencing():
    """D3：仅回退「为自动 chip 部署抢占」新增的复杂度，不波及共用 fencing helper。

    回退面（必须消失）：
      - chip service 的 `ChipPreemptedForShutdown` 异常类
      - `execute_after_close_chip_consensus` 的 `shutdown_check` 参数
      - fenced_job_run_service 的 `requeue_owned_job_to_resume`
    保留面（必须存在）：其他 job 共用的 claim/lock/finalize fencing 原语。
    """
    import app.services.fenced_job_run_service as fenced

    assert not hasattr(chip_svc, "ChipPreemptedForShutdown"), (
        "chip SIGTERM 抢占异常应随自动部署一并回退"
    )
    sig = inspect.signature(chip_svc.execute_after_close_chip_consensus)
    assert "shutdown_check" not in sig.parameters, (
        "chip 执行器不应再接受 shutdown_check 抢占钩子"
    )
    assert not hasattr(fenced, "requeue_owned_job_to_resume"), (
        "抢占专用的 requeue helper 应随回退移除"
    )

    for kept in (
        "claim_next_job_run",
        "lock_owned_job_run",
        "finalize_job_run",
        "FencedJobToken",
    ):
        assert hasattr(fenced, kept), (
            f"共用 fencing 原语 {kept} 不得被 chip 回退波及"
        )
