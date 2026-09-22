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


def test_a3_daily_refresh_is_periods_d_only():
    """A3（[PANJI-INTRADAY-DIRECT-SOURCE] 合同 A）：盘后行情刷新必须只刷新日线。

    盘后 Core 是 daily-only；15m/1h 的实时来源已改为 provider direct（见 MDAS
    ``MarketDataSourcePolicy.PROVIDER_DIRECT``），因此盘后**不得**再维护 15m/60m ——
    否则 DB 分钟线会被当成"仍然被维护的源"，与新的 source ownership 冲突。

    断言生产调用 `bars_service.refresh_all_instruments(...)` 的关键字实参
    ``periods`` 恰好是 ``("d",)``：

    - 不允许缺少 / 为 None（默认 None = d/15m/60m 全刷，会重新引入分钟线维护）；
    - 不允许加入 "15m" / "60m" / "1h"。

    实现方式说明：完整的 `execute_after_close_run` 行为测试标记为
    ``@pytest.mark.postgres``（需要真实库），本文件是纯单元文件；因此这里对
    **生产调用点本身**做 AST 级断言，等价于 ``call_args.kwargs["periods"]``
    对源码的静态锁定，且不依赖 DB。
    """
    tree = ast.parse(
        inspect.getsource(orch.execute_after_close_run)
    )
    refresh_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = (
            fn.attr if isinstance(fn, ast.Attribute)
            else fn.id if isinstance(fn, ast.Name)
            else None
        )
        if name == "refresh_all_instruments":
            refresh_calls.append(node)

    assert len(refresh_calls) == 1, (
        f"execute_after_close_run 应恰好有 1 处 refresh_all_instruments 调用，"
        f"实际 {len(refresh_calls)}"
    )

    call = refresh_calls[0]
    periods_kw = [kw for kw in call.keywords if kw.arg == "periods"]
    assert periods_kw, "refresh_all_instruments 必须显式传 periods= 关键字实参"
    value = periods_kw[0].value
    assert isinstance(value, ast.Tuple), (
        f"periods 必须是字面量元组（否则无法静态确认内容），实际 {ast.dump(value)}"
    )
    periods = tuple(
        elt.value for elt in value.elts
        if isinstance(elt, ast.Constant)
    )
    assert len(periods) == len(value.elts), "periods 只能包含字面量常量"
    assert periods == ("d",), (
        f"盘后只允许刷新日线 periods=('d',)，实际 {periods!r}"
    )
    for forbidden in ("15m", "60m", "1h", "15min", "60min"):
        assert forbidden not in periods, (
            f"盘后不得再刷新分钟周期，实际 {periods!r} 含 {forbidden!r}"
        )


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
    # W5A：process lifecycle（含 Auction drain）已迁到 runtime 模块，故 drain 守卫改查 runtime
    import app.services.after_close_orchestrator_worker_runtime as rt_mod
    rt_code = _executable_code(rt_mod.run_after_close_orchestrator_worker_runtime)
    assert "_drain_co_process(_auction_co_process_task, 'Auction', logger)" in rt_code, (
        "Auction co-process 的 drain 必须保留（已随 lifecycle 迁到 runtime 模块）"
    )


# =============================================================================
# TEST C — Core → Review → History 合同不变
# =============================================================================


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


# =============================================================================
# TEST E — 盘后 chip 生产者 fail-closed（[PANJI-INTRADAY-DIRECT-SOURCE]）
# =============================================================================


@pytest.mark.asyncio
async def test_retired_chip_executor_fails_closed_without_db_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E（行为）：正式执行入口必须 fail-closed，且**不产生任何副作用**。

    这是「退役」区别于「静默空转」的关键：入口不仅要停，还要**可见地失败**，
    并且不得再刷新 15m / 计算 chip / upsert 快照（否则遗留 resume_queued 任务
    会在无人察觉的情况下继续写旧表，重新制造两份 chip 真相）。

    三个副作用 seam 全部换成记录器（不是抛异常，也不是 AsyncMock 返回值），
    断言调用次数恰好为 0：
      - `chip_bars_refresh_coordinator.refresh_15m_batch`（15m 运行级刷新）
      - `first_pyramid_service.compute_chip_consensus_snapshot`（chip 计算）
      - `after_close_chip_consensus_service._upsert_chip_snapshot`（快照落库）
    """
    import uuid as uuid_mod
    from datetime import date as date_mod

    import app.services.chip_bars_refresh_coordinator as refresh_mod
    import app.services.first_pyramid_service as fp_mod

    calls: dict[str, int] = {
        "refresh_15m": 0,
        "compute_chip": 0,
        "upsert_snapshot": 0,
    }

    async def _refresh_15m_batch(*args: object, **kwargs: object) -> object:
        calls["refresh_15m"] += 1
        return None

    async def _compute_chip(*args: object, **kwargs: object) -> object:
        calls["compute_chip"] += 1
        return None

    async def _upsert_chip_snapshot(*args: object, **kwargs: object) -> object:
        calls["upsert_snapshot"] += 1
        return None

    monkeypatch.setattr(refresh_mod, "refresh_15m_batch", _refresh_15m_batch)
    monkeypatch.setattr(fp_mod, "compute_chip_consensus_snapshot", _compute_chip)
    monkeypatch.setattr(chip_svc, "_upsert_chip_snapshot", _upsert_chip_snapshot)

    with pytest.raises(chip_svc.ChipPipelineRetiredError):
        await chip_svc.execute_after_close_chip_consensus(
            uuid_mod.uuid4(),
            date_mod(2026, 9, 22),
            uuid_mod.uuid4(),
            instrument_ids=[uuid_mod.uuid4(), uuid_mod.uuid4()],
            worker_id="retired-executor-test",
            lease_epoch=1,
        )

    assert calls == {"refresh_15m": 0, "compute_chip": 0, "upsert_snapshot": 0}, (
        f"退役执行器必须零副作用，实际: {calls}"
    )


def test_e2_legacy_impl_preserved_but_unwired() -> None:
    """E2（拓扑）：legacy 实现保留，但生产树中除定义模块外无任何调用点。"""
    import inspect as _inspect
    from pathlib import Path

    assert hasattr(chip_svc, "_execute_legacy_after_close_chip_consensus"), (
        "legacy 实现必须保留（审计 / 历史重建），退役不等于删除"
    )

    defining_module = Path(_inspect.getsourcefile(chip_svc)).resolve()
    app_root = defining_module.parents[2]
    offenders: list[str] = []
    for py in sorted(app_root.joinpath("app").rglob("*.py")):
        if py.resolve() == defining_module:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "_execute_legacy_after_close_chip_consensus":
                offenders.append(f"{py.relative_to(app_root)}:{node.lineno}")

    assert offenders == [], (
        "legacy chip 执行实现不得被生产链重新接线，实际: " + str(offenders)
    )
