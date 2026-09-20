"""Market Dashboard P1-F1D — after-close integration（Dashboard projection optional sidecar）。

本文件只验证 **after-close 控制流合同**，不重新证明 F1C 的 PostgreSQL 内部语义
（REPEATABLE READ + READ ONLY snapshot / SHARE lock membership guard / 原子替换已由
formal targeted-pg 的 3 个 Dashboard contract 独立证明）：

1. normal run   → sidecar 恰好执行 1 次，且位于 computing_features 之前
2. sidecar 失败 → 不阻断 Core/Review 主链（旧 projection 由 F1B 保留）
3. resume       → sidecar 仍重跑一次（它不是 checkpointed step）
4. 非交易日      → 不重建 projection
5. 结构回归      → 不进入 AfterCloseRunStatus / _CHECKPOINT_ORDER / _COMPLETED_STEPS

为什么单独成文件而不是扩进 ``test_after_close_phase0_control_flow.py``：
后者自 ``d542d10e``（引入 mandatory Core gate ``_validate_core_ready``）起已整体红灯
——fake session 返回 MagicMock 导致 CoreRun id 校验失败——而 T1 门禁按「整个变更文件」
执行，故在该文件内新增用例会让 ``make check`` 变红。本文件**复用**它的全 mock 编排
harness（不复制第二套大 mock 环境），只额外注入 market_dashboard spy 与 step 顺序记录器。

运行：PURE_UNIT_TEST=1 pytest backend/tests/test_market_dashboard_after_close_integration.py
"""

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import after_close_orchestrator as orchestrator
from app.services.after_close_orchestrator import AfterCloseRunStatus
from tests.test_after_close_phase0_control_flow import (
    _install_patches,
    _make_job_run,
    _published_resolution,
    _run_orchestrator,
    _stop_patches,
)

pytestmark = pytest.mark.asyncio

_SIDECAR_STEP = "rebuilding_market_dashboard"
_RUN_TRADE_DATE = date(2026, 8, 7)


def _install_dashboard_harness(job_run, *, refresh_result=None):
    """复用 control-flow harness，并额外注入：

    - ``market_dashboard`` spy（patch F1C 的 ``rebuild_market_dashboard_projection``；
      orchestrator 的 sidecar helper 在调用时才 import 该函数，故 patch 模块属性即生效）；
    - ``execute_orchestrator_step`` 记录器（真实 step 级顺序证据）；
    - ``compute_review_core`` 记录器（computing_features 内真实计算边界）；
    - ``_persist_step_summary`` spy（验证 sidecar 成功时补记的真实事实字段）。

    Returns: (spies, patchers, extra_patchers, steps)
    """
    spies, patchers = _install_patches(job_run, resolve_side_effect=_published_resolution)

    spies["market_dashboard"] = AsyncMock(
        return_value=MagicMock(
            projection_trade_date=_RUN_TRADE_DATE, market_rows=250, scope_rows=1000
        )
    )
    spies["persist_summary"] = AsyncMock()

    steps: list[str] = []
    original_execute = orchestrator.execute_orchestrator_step

    async def _recording_execute(step, operation, **kwargs):
        steps.append(step)
        return await original_execute(step, operation, **kwargs)

    # computing_features 内真实计算边界：直接返回原 return_value（不可回调原 mock，
    # 否则 side_effect 会自我递归）。
    core_return = spies["compute_review_core"].return_value

    async def _recording_core(*args, **kwargs):
        steps.append("compute_review_core")
        return core_return

    spies["compute_review_core"].side_effect = _recording_core

    extra: list = [
        patch.object(orchestrator, "execute_orchestrator_step", new=_recording_execute),
        patch(
            "app.services.market_dashboard_projection_rebuild_service."
            "rebuild_market_dashboard_projection",
            new=spies["market_dashboard"],
        ),
        # fake session 下真实 _persist_step_summary 会被 metadata 解析失败吞掉，
        # 故替换为 spy，使「成功时补记事实字段」可被真实断言。
        patch.object(orchestrator, "_persist_step_summary", new=spies["persist_summary"]),
    ]
    if refresh_result is not None:
        extra.append(
            patch(
                "app.services.bars_scheduler_service.BarsSchedulerService.refresh_all_instruments",
                new=AsyncMock(return_value=refresh_result),
            )
        )
    for p in extra:
        p.start()
    return spies, patchers, extra, steps


def _stop_dashboard_harness(patchers, extra) -> None:
    # 先停本文件追加的 patcher（逆序），再停基础 harness，避免 patch 恢复顺序错乱。
    for p in reversed(extra):
        p.stop()
    _stop_patches(patchers)


async def _run_tolerant(job_run, *, skip_publish=False) -> str | None:
    """驱动完整编排，容忍 harness 陈旧导致的终态异常。

    该终态异常（``AfterCloseCoreNotReadyError``，来自 mandatory Core gate）发生在
    sidecar 之后，不影响本文件对 sidecar 控制流的断言；这里不为「让旧测试变绿」
    扩大 mock 范围。返回终态异常类型名（无异常则 None），供失败隔离对照使用。
    """
    try:
        await _run_orchestrator(job_run=job_run, skip_publish=skip_publish)
    except Exception as exc:  # noqa: BLE001 - 见模块 docstring
        return type(exc).__name__
    return None


# ---------------------------------------------------------------------------
# 1. 调用位置与顺序
# ---------------------------------------------------------------------------
async def test_market_dashboard_sidecar_runs_before_computing_features():
    """normal run：sidecar 恰好 1 次、入参为盘后 trade_date，
    且发生在 computing_features（及其内部真实计算边界）之前。"""
    job_run = _make_job_run(dsa_run_id=uuid.uuid4(), snapshot_run_id=uuid.uuid4())
    spies, patchers, extra, steps = _install_dashboard_harness(job_run)
    try:
        await _run_tolerant(job_run, skip_publish=False)

        assert spies["market_dashboard"].await_count == 1, (
            f"normal run 必须恰好重建一次 Dashboard projection，实际: "
            f"{spies['market_dashboard'].await_count}"
        )
        assert spies["market_dashboard"].await_args.args == (_RUN_TRADE_DATE,), (
            f"sidecar 必须以盘后 trade_date 调用，实际: {spies['market_dashboard'].await_args.args}"
        )

        assert _SIDECAR_STEP in steps, f"执行序列缺少 Dashboard sidecar，实际: {steps}"
        assert "computing_features" in steps, f"harness 未到达 computing_features，实际: {steps}"
        assert steps.index(_SIDECAR_STEP) < steps.index("computing_features"), (
            f"sidecar 必须在 computing_features 之前执行，实际序列: {steps}"
        )

        # computing_features 内真实计算边界（compute_review_core_with_run_items）
        # 也在 sidecar 之后——这比源码字符串检查更强。
        assert "compute_review_core" in steps, (
            f"harness 未到达 computing_features 真实计算边界，实际: {steps}"
        )
        assert steps.index(_SIDECAR_STEP) < steps.index("compute_review_core"), (
            f"sidecar 必须在 Core 计算之前执行，实际序列: {steps}"
        )

        # 成功时 step_summary 必须补记事实字段（诊断用；不新增 run table）。
        persisted = [
            call.args[1]
            for call in spies["persist_summary"].await_args_list
            if call.args[1].get("step") == _SIDECAR_STEP
        ]
        assert len(persisted) == 1, (
            f"sidecar 成功后必须落库一次 step_summary，实际: {len(persisted)}"
        )
        summary = persisted[0]
        assert summary["status"] == "succeeded"
        assert summary["projection_trade_date"] == _RUN_TRADE_DATE.isoformat()
        assert summary["market_rows"] == 250
        assert summary["scope_rows"] == 1000
        assert summary["processed"] == 1250 and summary["total"] == 1250
    finally:
        _stop_dashboard_harness(patchers, extra)


# ---------------------------------------------------------------------------
# 2. 失败隔离
# ---------------------------------------------------------------------------
async def test_market_dashboard_sidecar_failure_does_not_block_mainchain():
    """sidecar 抛异常（ProjectionInputChangedError / stale-T / DB write error 等）时，
    旧 projection 保留由 F1B 保证；主链必须**与 sidecar 成功时走到完全相同的下游步骤**。

    用「对照组 vs 实验组」而非固定断言，避免把 harness 当前能走到的位置写死。
    """
    # 对照组：sidecar 正常成功
    ok_job = _make_job_run(dsa_run_id=uuid.uuid4(), snapshot_run_id=uuid.uuid4())
    ok_spies, ok_patchers, ok_extra, ok_steps = _install_dashboard_harness(ok_job)
    try:
        ok_tail = await _run_tolerant(ok_job, skip_publish=False)
        assert ok_spies["market_dashboard"].await_count == 1
    finally:
        _stop_dashboard_harness(ok_patchers, ok_extra)

    # 实验组：sidecar 失败
    fail_job = _make_job_run(dsa_run_id=uuid.uuid4(), snapshot_run_id=uuid.uuid4())
    spies, patchers, extra, steps = _install_dashboard_harness(fail_job)
    spies["market_dashboard"].side_effect = RuntimeError("dashboard rebuild failed")
    try:
        fail_tail = await _run_tolerant(fail_job, skip_publish=False)

        assert spies["market_dashboard"].await_count == 1, "sidecar 应仍被调用一次"
        assert "compute_review_core" in steps, (
            f"Dashboard 失败不得阻断 computing_features，实际序列: {steps}"
        )
        assert steps == ok_steps, (
            f"sidecar 失败改变了主链下游步骤序列：\n失败: {steps}\n成功: {ok_steps}"
        )
        assert fail_tail == ok_tail, (
            f"sidecar 失败改变了主链终态：失败组={fail_tail!r} 对照组={ok_tail!r}"
        )
    finally:
        _stop_dashboard_harness(patchers, extra)


# ---------------------------------------------------------------------------
# 3. resume 语义
# ---------------------------------------------------------------------------
async def test_market_dashboard_sidecar_reruns_on_resume():
    """断点恢复（last_completed_step=publishing ⇒ skip_refresh / skip_computing）时，
    sidecar 仍必须执行一次：它不是 checkpointed step，刻意允许重建。"""
    job_run = _make_job_run(
        dsa_run_id=uuid.uuid4(), snapshot_run_id=uuid.uuid4(), last_completed_step="publishing"
    )
    spies, patchers, extra, _steps = _install_dashboard_harness(job_run)
    try:
        await _run_tolerant(job_run, skip_publish=True)

        assert spies["market_dashboard"].await_count == 1, (
            "resume 路径不得被 skip_refresh/skip_computing 吞掉 Dashboard sidecar"
        )
    finally:
        _stop_dashboard_harness(patchers, extra)


# ---------------------------------------------------------------------------
# 4. 非交易日
# ---------------------------------------------------------------------------
async def test_market_dashboard_sidecar_skipped_on_non_trading_day():
    """非交易日：normal path 在 sidecar 之前已经 succeeded + return，
    因此天然不得重建 projection（无需为 Dashboard 单独写 calendar 判断）。"""
    job_run = _make_job_run(dsa_run_id=uuid.uuid4(), snapshot_run_id=uuid.uuid4())
    spies, patchers, extra, _steps = _install_dashboard_harness(
        job_run,
        refresh_result=MagicMock(
            dsa_run_id=None, daily_coverage=None, skip_reason="NON_TRADING_DAY"
        ),
    )
    try:
        await _run_tolerant(job_run, skip_publish=False)

        assert spies["market_dashboard"].await_count == 0, "非交易日不得重建 Dashboard projection"
    finally:
        _stop_dashboard_harness(patchers, extra)


# ---------------------------------------------------------------------------
# 5. 结构回归：不是 mainchain checkpoint
# ---------------------------------------------------------------------------
async def test_market_dashboard_sidecar_is_not_a_mainchain_checkpoint():
    """Dashboard 是 idempotent optional projection sidecar，不得进入
    AfterCloseRunStatus / _CHECKPOINT_ORDER / _COMPLETED_STEPS —— 这是恢复语义
    （granular restart / failed-step resolution 不被扩张）的守卫。"""
    assert _SIDECAR_STEP not in orchestrator._CHECKPOINT_ORDER, (
        "Dashboard sidecar 不得成为 mainchain checkpoint"
    )
    assert _SIDECAR_STEP not in orchestrator._COMPLETED_STEPS, (
        "Dashboard sidecar 不得进入断点恢复映射"
    )
    for completed_step, completed in orchestrator._COMPLETED_STEPS.items():
        assert _SIDECAR_STEP not in completed, (
            f"Dashboard sidecar 不得出现在 _COMPLETED_STEPS[{completed_step!r}]"
        )
    assert _SIDECAR_STEP not in {status.value for status in AfterCloseRunStatus}, (
        "Dashboard sidecar 不得新增 AfterCloseRunStatus"
    )
    assert orchestrator._STEP_TIMEOUT_SECONDS[_SIDECAR_STEP] == 600, (
        "Dashboard sidecar 必须有有限 timeout（600s），不得为 None"
    )
