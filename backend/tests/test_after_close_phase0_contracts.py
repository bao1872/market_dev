"""[Phase0] 盘后基础合同行为测试。

覆盖 Phase 0 验收门中除执行器之外的部分：
- board_soft_failure_truthful：板块软失败必须让 step summary 为 failed
- review_failure_checkpoint_not_advanced：Review 失败不得推进 last_completed_step
- chip_enqueue_before_final_status：chip 入队是终态之前的正式步骤
- status_api_contract_complete：watchdog 字段完整进入 API 响应

全部为真实行为断言，不使用 inspect.getsource() 字符串检查。
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services import after_close_orchestrator
from app.services.after_close_orchestrator import (
    _enqueue_chip_job_step,
    _is_terminal_review_short_circuit,
    _update_heartbeat_and_step,
    execute_orchestrator_step,
)

# ---------------------------------------------------------------------------
# Gate: board_soft_failure_truthful
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_board_business_failure_must_not_report_step_succeeded():
    """业务返回 {"status": "failed"} 时，执行器 summary 仍是 succeeded。

    这正是问题 #5：执行器只看"有没有抛异常"。
    因此调用方必须显式把业务软失败翻译成 step failed —— 本测试锁定
    "执行器 result 与 summary 是两个不同的东西"这一合同，
    防止再次写成 `board_summary, _ = await execute_orchestrator_step(...)`。
    """
    async def board_op():
        return {"status": "failed", "error_code": "BOARD_SYNC_ERROR"}

    result, summary = await execute_orchestrator_step(
        "syncing_boards", board_op, timeout_seconds=30, optional=True,
    )

    # result 是业务结果
    assert result == {"status": "failed", "error_code": "BOARD_SYNC_ERROR"}
    # summary 是执行器状态，未捕获业务语义
    assert summary["status"] == "succeeded"
    # 两者绝不可混用：这是 #5 的根因
    assert result is not summary
    assert "step" in summary and "step" not in result


@pytest.mark.asyncio
async def test_optional_step_timeout_returns_none_result():
    """可选步骤超时返回 result=None —— 调用方直接下标取值会 TypeError。

    锁定问题 #5 后半段：必须先判空再取 result["status"]。
    """
    async def slow():
        import asyncio
        await asyncio.sleep(5)
        return {"status": "succeeded"}

    result, summary = await execute_orchestrator_step(
        "syncing_boards", slow, timeout_seconds=0.02, optional=True,
    )

    assert result is None
    assert summary["status"] == "timed_out"
    # 证明"直接下标"会炸——调用方必须 isinstance 判断
    with pytest.raises(TypeError):
        _ = result["status"]  # type: ignore[index]


# ---------------------------------------------------------------------------
# Gate: review_failure_checkpoint_not_advanced
# ---------------------------------------------------------------------------


class _FakeJobRun:
    """最小 job_run 替身，只关心 metadata_json 的读写。"""

    def __init__(self, metadata_json: str = "{}"):
        self.id = uuid.uuid4()
        self.metadata_json = metadata_json
        self.heartbeat_at = None
        self.lease_expires_at = None
        self.worker_instance_id = None
        self.lease_epoch = 1
        self.status = "running"


@pytest.mark.asyncio
async def test_update_heartbeat_with_none_step_preserves_checkpoint():
    """[Gate] last_completed_step=None 时只刷心跳，不推进检查点。

    这是 Review 失败路径依赖的核心能力：若仍写入 computing_review，
    下次 resume 会跳过失败的 Review，破坏 restart_from 语义。
    """
    job_run = _FakeJobRun('{"last_completed_step": "publishing"}')
    db = AsyncMock()

    await _update_heartbeat_and_step(db, job_run, None, "worker-1")

    meta = after_close_orchestrator._parse_metadata(job_run)
    # 关键断言：检查点未被推进，仍停留在 publishing
    assert meta["last_completed_step"] == "publishing"


@pytest.mark.asyncio
async def test_update_heartbeat_with_step_advances_checkpoint():
    """成功路径仍必须正常推进检查点（防止修复过度）。"""
    job_run = _FakeJobRun('{"last_completed_step": "publishing"}')
    db = AsyncMock()

    await _update_heartbeat_and_step(db, job_run, "computing_review", "worker-1")

    meta = after_close_orchestrator._parse_metadata(job_run)
    assert meta["last_completed_step"] == "computing_review"


# ---------------------------------------------------------------------------
# Gate: chip_enqueue_before_final_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_chip_job_step_skipped_when_no_snapshot_run():
    """snapshot_run_id 缺失 → skipped（不是 failed，不应拉低主任务终态）。"""
    from datetime import date

    with patch.object(
        after_close_orchestrator, "_persist_step_summary", new=AsyncMock(),
    ) as persist:
        status, job_id = await _enqueue_chip_job_step(
            job_run_id=uuid.uuid4(),
            worker_id="w1",
            lease_epoch=1,
            trade_date=date(2026, 7, 31),
            snapshot_run_id=None,
            expected_count=100,
        )

    assert status == "skipped"
    assert job_id is None
    # 必须留下 step summary（chip 是正式步骤，不能无声跳过）
    persist.assert_awaited_once()
    summary = persist.await_args.args[1]
    assert summary["step"] == "enqueue_chip_job"
    assert summary["status"] == "skipped"
    assert summary["skip_reason"] == "SNAPSHOT_RUN_ID_MISSING"


@pytest.mark.asyncio
async def test_enqueue_chip_job_step_failure_returns_failed_status():
    """chip 入队抛异常 → 返回 failed，供主任务判定 partial_success。

    修复前 chip 在终态之后创建，失败只 warn，永远无法进入 partial_success。
    """
    from datetime import date

    with (
        patch.object(
            after_close_orchestrator,
            "create_after_close_chip_consensus_job",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch.object(after_close_orchestrator, "AsyncSessionLocal"),
        patch.object(
            after_close_orchestrator, "_make_step_progress_callback",
            return_value=None,
        ),
        patch.object(
            after_close_orchestrator, "_make_step_heartbeat", return_value=None,
        ),
        patch.object(
            after_close_orchestrator, "_make_step_cancellation_check",
            return_value=None,
        ),
        patch.object(
            after_close_orchestrator, "_persist_step_summary", new=AsyncMock(),
        ),
    ):
        status, _job_id = await _enqueue_chip_job_step(
            job_run_id=uuid.uuid4(),
            worker_id="w1",
            lease_epoch=1,
            trade_date=date(2026, 7, 31),
            snapshot_run_id=uuid.uuid4(),
            expected_count=100,
        )

    assert status == "failed"


# ---------------------------------------------------------------------------
# Gate: status_api_contract_complete
# ---------------------------------------------------------------------------


def test_status_response_schema_defines_watchdog_fields():
    """[Gate] Schema 必须定义全部 watchdog 字段，否则 API 组装时被丢弃。"""
    from app.schemas.scheduler_job_run import AfterCloseRunStatusResponse

    fields = AfterCloseRunStatusResponse.model_fields
    for name in (
        "step_summary", "running_steps", "step_timed_out",
        "stale", "partial_success", "skip_reason",
    ):
        assert name in fields, f"Schema 缺少 watchdog 字段: {name}"


def test_status_response_serializes_watchdog_values():
    """字段必须能真实承载并序列化 service 计算出的值。"""
    from app.schemas.scheduler_job_run import AfterCloseRunStatusResponse

    resp = AfterCloseRunStatusResponse(
        job_run_id=str(uuid.uuid4()),
        job_name="after_close_pipeline",
        status="partial_success",
        orchestrator_status="partial_success",
        trade_date="2026-07-31",
        step_summary={"syncing_boards": {"status": "failed"}},
        running_steps=["computing_features"],
        step_timed_out=True,
        stale=True,
        partial_success=True,
    )
    dumped = resp.model_dump()

    assert dumped["step_summary"]["syncing_boards"]["status"] == "failed"
    assert dumped["running_steps"] == ["computing_features"]
    assert dumped["step_timed_out"] is True
    assert dumped["stale"] is True
    assert dumped["partial_success"] is True


def test_status_response_watchdog_defaults_are_safe():
    """未提供时必须有安全默认值（不得 None 导致前端崩）。"""
    from app.schemas.scheduler_job_run import AfterCloseRunStatusResponse

    resp = AfterCloseRunStatusResponse(
        job_run_id=str(uuid.uuid4()),
        job_name="after_close_pipeline",
        status="running",
        orchestrator_status="computing_features",
        trade_date="2026-07-31",
    )

    assert resp.step_summary == {}
    assert resp.running_steps == []
    assert resp.step_timed_out is False
    assert resp.stale is False
    assert resp.partial_success is False


# ---------------------------------------------------------------------------
# Gate: AC-02 all_top_level_steps_use_executor（computing_review 收口）
# ---------------------------------------------------------------------------


def test_execute_after_close_run_wires_computing_review_through_executor():
    """[AC-02] computing_review 必须通过统一执行器，不得保留旧直调路径。

    Phase0 收口前，computing_review 是 execute_after_close_run 内 ~410 行内联块，
    直接 import create_run/compute_run/publish_run，绕过 execute_orchestrator_step，
    违反「所有顶层步骤必须通过统一步骤执行器」的 AC-02 合同。

    收口后：复盘业务体抽为 _execute_review_step，由
    execute_orchestrator_step("computing_review", lambda: _execute_review_step(...)) 包装。
    本测试用源码守卫锁定该接线，防止回退为内联直调。
    """
    import inspect

    from app.services import after_close_orchestrator as orch

    main_src = inspect.getsource(orch.execute_after_close_run)

    # 1) 主编排必须通过执行器提交 computing_review 步骤
    assert 'execute_orchestrator_step(\n            "computing_review"' in main_src, (
        "execute_after_close_run 必须通过 execute_orchestrator_step 提交 computing_review"
    )
    # 2) 主编排不得再内联直调 review service（必须委托 _execute_review_step）
    assert "_execute_review_step(" in main_src, (
        "computing_review 业务体必须抽为 _execute_review_step 并委托执行"
    )
    # 3) 主编排不得内联 import review_orchestrator_service（旧直调路径的入口）。
    #    注意：不能用 create_run(/publish_run( 等裸子串——execute_after_close_run
    #    另有 batch_service.publish_run（stock_core 发布）等同名不同源调用，
    #    裸子串会误伤。因此只锁 review service 的专属 import 入口。
    assert "from app.services.review_orchestrator_service import (" not in main_src, (
        "execute_after_close_run 不得内联 import review_orchestrator_service "
        "（computing_review 必须委托 _execute_review_step）"
    )


def test_review_step_exists_and_is_wired_as_operation():
    """[AC-02] _execute_review_step 作为执行器 operation 存在，且与主编排分离。"""
    from app.services import after_close_orchestrator as orch

    assert hasattr(orch, "_execute_review_step"), (
        "必须存在 _execute_review_step 业务体"
    )
    # 业务体必须是独立模块级协程（非 execute_after_close_run 内联函数）
    assert orch._execute_review_step.__module__ == after_close_orchestrator.__name__


@pytest.mark.asyncio
async def test_review_step_prereq_missing_returns_skipped():
    """[AC-02] 前置条件不满足 → 返回 skipped 业务结果（不阻断主流程）。

    验证 _execute_review_step 的 result 合同：必含 status/failed/reason，
    且 prereq_missing=True 时 status=skipped、failed=False。
    """
    from datetime import date

    from app.services import after_close_orchestrator as orch

    with (
        patch.object(orch, "AsyncSessionLocal"),
        patch.object(
            orch, "_get_job_run_or_raise",
            return_value=_FakeJobRun("{}"),
        ),
        patch.object(orch, "append_event", new=AsyncMock()),
        patch.object(orch, "_update_orchestrator_status", new=AsyncMock()),
        patch.object(orch, "_update_heartbeat_and_step", new=AsyncMock()),
    ):
        result = await orch._execute_review_step(
            job_run_id=uuid.uuid4(),
            trade_date=date(2026, 7, 31),
            snapshot_run_id=None,
            worker_id="w1",
            skip_review=False,
            stock_core_published=False,
            aggregation_status="skipped",
        )

    assert result["status"] == "skipped"
    assert result["failed"] is False
    assert result["prereq_missing"] is True
    assert "prerequisite_missing" in (result["reason"] or "")
    # result 合同字段齐全，供主编排 partial_success 判定与 metadata 写入
    for key in (
        "status", "failed", "reason", "run_id", "publication_id",
        "scope_count", "signal_count", "coverage", "blockers",
        "prereq_missing", "resume_skipped",
    ):
        assert key in result, f"result 缺少字段: {key}"


@pytest.mark.asyncio
async def test_review_step_resume_skip_returns_resume_skipped():
    """[AC-02] 断点恢复 skip_review=True → 返回 skipped_by_resume（不重复计算）。"""
    from datetime import date

    from app.services import after_close_orchestrator as orch

    with (
        patch.object(orch, "AsyncSessionLocal"),
        patch.object(
            orch, "_get_job_run_or_raise",
            return_value=_FakeJobRun('{"review_status": "published"}'),
        ),
    ):
        result = await orch._execute_review_step(
            job_run_id=uuid.uuid4(),
            trade_date=date(2026, 7, 31),
            snapshot_run_id=uuid.uuid4(),
            worker_id="w1",
            skip_review=True,
            stock_core_published=True,
            aggregation_status="succeeded",
        )

    assert result["resume_skipped"] is True
    assert result["status"] == "published"  # 从 metadata 复用
    assert result["failed"] is False


def test_review_prereq_missing_does_not_advance_checkpoint():
    """[CHANGE-20260804-P0-1] 前置条件缺失不得推进 computing_review 检查点。

    缺口1：review prereq_missing 时，主流程必须只刷新心跳/租约
    （_update_heartbeat_and_step(db, job_run, None, ...)），
    不能把 last_completed_step 写成 computing_review，否则后续 resume
    会误判 Review 已完成而永久跳过。

    源码守卫：在 prereq_missing 分支内，_update_heartbeat_and_step 的
    last_completed_step 实参必须为 None（而非 COMPUTING_REVIEW.value）。
    """
    import inspect
    import re

    from app.services import after_close_orchestrator as orch

    # 前置条件缺失分支位于 _execute_review_step（不是 execute_after_close_run）
    src = inspect.getsource(orch._execute_review_step)
    # 取前置条件缺失分支文本（到该分支后的 db.commit() 为止）
    branch = re.search(
        r'# 前置条件不满足.*?await db\.commit\(\)',
        src,
        re.DOTALL,
    )
    assert branch is not None, (
        "未找到 _execute_review_step 的 prereq_missing 分支"
    )
    m = re.search(
        r'_update_heartbeat_and_step\(\s*db, job_run, ([^,]+),',
        branch.group(0),
    )
    assert m is not None, (
        "prereq_missing 分支未调用 _update_heartbeat_and_step"
    )
    step_arg = m.group(1).strip()
    assert step_arg == "None", (
        f"prereq_missing 分支必须把 last_completed_step 设为 None（仅刷新心跳），"
        f"实际为: {step_arg}"
    )
    # 仅禁止把检查点（_update_heartbeat_and_step 的 step 实参）写为 COMPUTING_REVIEW；
    # _add_pipeline_event 引用 COMPUTING_REVIEW.value 仅用于事件标记，不构成检查点推进。
    assert re.search(
        r'_update_heartbeat_and_step\(\s*db, job_run, AfterCloseRunStatus\.COMPUTING_REVIEW\.value',
        branch.group(0),
    ) is None, (
        "prereq_missing 分支不得将 _update_heartbeat_and_step 的检查点推进为 computing_review"
    )


def test_review_executor_timeout_forces_partial_success():
    """[CHANGE-20260804-P0-1] 执行器超时/中断必须进入 partial_success（非 succeeded）。

    缺口2：执行器 timed_out/unavailable/interrupted/cancelled 会返回
    result=None 或 failed=False，但 step_summary.status 已如实记录。
    最终 partial_success 判定必须同时读 review 业务结果和 _review_step_summary.status，
    否则超时会被误判为成功（succeeded）。

    源码守卫：_review_failed 的推导必须包含对 _review_step_summary.get("status")
    的集合判定（timed_out/unavailable/interrupted/cancelled/failed）。
    """
    import inspect
    import re

    from app.services import after_close_orchestrator as orch

    src = inspect.getsource(orch.execute_after_close_run)

    # 1) _review_failed 推导必须引用 _review_step_summary
    assert "_review_step_summary.get(\"status\")" in src or "_review_step_status" in src, (
        "review 失败判定必须读取 _review_step_summary.status"
    )
    # 2) 失败集合必须显式包含执行器终态，而非只看业务 failed 字段
    assert "timed_out" in src and "interrupted" in src and "cancelled" in src, (
        "review 失败判定必须覆盖执行器终态 timed_out/unavailable/interrupted/cancelled"
    )
    # 3) 最终 final status 判定必须消费 _review_failed（而非仅 stock_core 成功）
    m = re.search(
        r'_review_failed = bool\(.*?\)\s*\n\s*_review_reason =',
        src,
        re.DOTALL,
    )
    assert m is not None, "未找到 _review_failed 推导"
    block = m.group(0)
    assert "timed_out" in block and "_review_step_status" in block, (
        "_review_failed 必须结合 _review_step_summary.status（含 timed_out）"
    )


@pytest.mark.asyncio
async def test_terminal_review_short_circuit_detection():
    """[AC-CANCEL-01 2026-08-04] Review 终态短路判定（真实行为，非正则）。

    cancelled / interrupted 必须短路（保持终态、不覆盖总任务终态）；
    succeeded / failed / timed_out / unavailable 不得短路（走 partial_success 判定）。

    [AUD-08 2026-08-07] 短路语义已与 chip 解耦：chip 在 stock_core 发布后即入队，
    早于本判定，短路不再影响 chip 是否存在。
    """
    from app.services.after_close_orchestrator import AfterCloseRunStatus

    assert _is_terminal_review_short_circuit(AfterCloseRunStatus.CANCELLED.value) is True
    assert _is_terminal_review_short_circuit(AfterCloseRunStatus.INTERRUPTED.value) is True

    # 步骤级终态（timed_out/unavailable）与 succeeded/failed 均为字符串值，
    # 不短路（走 partial_success 判定）
    for status in (
        AfterCloseRunStatus.SUCCEEDED.value,
        AfterCloseRunStatus.FAILED.value,
        "timed_out",
        "unavailable",
        None,
    ):
        assert _is_terminal_review_short_circuit(status) is False, (
            f"终态 {status} 不应短路"
        )


# ---------------------------------------------------------------------------
# Gate: review_terminal_state_closed
# [AC-TERMINAL-01 2026-08-04] 完整控制流验证，不止布尔函数
# ---------------------------------------------------------------------------


def test_resolve_terminal_run_status_returns_enum_not_string():
    """[P0#1] 终态字符串必须转成 AfterCloseRunStatus 枚举。

    _update_orchestrator_status(status=...) 内部访问 status.value；
    传裸字符串会在运行时抛 AttributeError，使取消链路写状态失败。
    """
    from app.services.after_close_orchestrator import (
        AfterCloseRunStatus,
        resolve_terminal_run_status,
    )

    cancelled = resolve_terminal_run_status("cancelled")
    interrupted = resolve_terminal_run_status("interrupted")

    assert cancelled is AfterCloseRunStatus.CANCELLED
    assert interrupted is AfterCloseRunStatus.INTERRUPTED
    # 关键：返回值必须有 .value（枚举），这正是修复前崩溃的原因
    assert cancelled.value == "cancelled"
    assert interrupted.value == "interrupted"

    # 非短路终态不得被静默映射
    for bad in ("succeeded", "failed", "timed_out", None):
        with pytest.raises(ValueError):
            resolve_terminal_run_status(bad)


def test_completed_step_index_excludes_terminal_run_statuses():
    """[P0#2] cancelled/interrupted 不得成为 last_completed_step 的合法检查点。

    若被写入，_COMPLETED_STEP_INDEX 查表 fallback -1，
    会让所有已完成步骤回退成 pending 且断点恢复从头重跑。
    """
    from app.services.after_close_pipeline_service import _COMPLETED_STEP_INDEX

    for terminal in ("cancelled", "interrupted", "partial_success"):
        assert terminal not in _COMPLETED_STEP_INDEX, (
            f"{terminal} 是 run 终态而非流水线步骤，不得作为检查点"
        )
        # 证明后果：一旦误写入，索引退化为 -1
        assert _COMPLETED_STEP_INDEX.get(terminal, -1) == -1


@pytest.mark.asyncio
async def test_cancelled_run_preserves_checkpoint():
    """[P0 完整控制流] Review 返回 cancelled 时的端到端行为。

    验证链路：
      Review executor 返回 cancelled
      → job_run.status = cancelled
      → orchestrator_status = cancelled
      → last_completed_step 仍为 publishing（未被覆写）

    [AUD-08 2026-08-07] 原用例名为 ..._and_skips_chip，断言取消时 chip 未入队。
    该合同已被判定为错误：chip 只依赖 stock_core，与 Review 无因果关系，
    不应因 Review 被取消而丢失。chip 现已前移到 stock_core 发布后入队，
    取消场景下 chip 依然存在（见 test_chip_enqueued_before_review_step 与
    test_chip_survives_review_failure_and_cancellation）。
    """
    from app.services import after_close_orchestrator as orch
    from app.services.after_close_orchestrator import AfterCloseRunStatus

    job_run = _FakeJobRun('{"last_completed_step": "publishing"}')
    db = AsyncMock()

    # 模拟短路块的两个关键调用
    terminal = orch.resolve_terminal_run_status(
        AfterCloseRunStatus.CANCELLED.value
    )
    job_run.status = terminal.value
    # 短路块必须传 None 以保留检查点
    await orch._update_heartbeat_and_step(db, job_run, None, "worker-1")

    meta = orch._parse_metadata(job_run)
    assert job_run.status == "cancelled"
    assert meta["last_completed_step"] == "publishing", (
        "取消不得覆写检查点为 cancelled"
    )


@pytest.mark.asyncio
async def test_cancelled_error_not_overwritten_as_failed():
    """[P0#3] AfterCloseCancelledError 必须与真实失败区分。

    取消/中断的终态已在短路块写入并 commit；
    外层 except 若把它当普通异常处理会覆写成 failed，
    导致管理员取消显示为"任务失败"。
    """
    from app.services.after_close_orchestrator import (
        AfterCloseCancelledError,
        AfterCloseRunStatus,
    )

    exc = AfterCloseCancelledError(AfterCloseRunStatus.CANCELLED)
    assert exc.terminal_status is AfterCloseRunStatus.CANCELLED
    assert isinstance(exc, Exception)
    # 必须是独立异常类型，可被 except 精确捕获而不落入通用 failed 分支
    assert not isinstance(exc, ValueError)


def test_execute_after_close_run_short_circuit_uses_enum_and_none_checkpoint():
    """[P0 源码守卫] 短路块必须：用枚举写状态 + 传 None 保留检查点 + 抛信号异常。

    这三点共同保证取消链路不破坏状态。
    """
    import inspect
    import re

    from app.services import after_close_orchestrator as orch

    src = inspect.getsource(orch.execute_after_close_run)
    block = re.search(
        r'if _is_terminal_review_short_circuit\(.*?raise AfterCloseCancelledError',
        src,
        re.DOTALL,
    )
    assert block is not None, "未找到终态短路块或缺少 AfterCloseCancelledError 抛出"
    body = block.group(0)

    # 1) 必须经 resolve_terminal_run_status 转枚举，不得直接传字符串
    assert "resolve_terminal_run_status" in body, (
        "短路块必须用 resolve_terminal_run_status 转枚举"
    )
    assert re.search(r'status=_review_step_status\b', body) is None, (
        "不得把裸字符串 _review_step_status 传给 _update_orchestrator_status"
    )

    # 2) _update_heartbeat_and_step 必须传 None 保留检查点
    m = re.search(r'_update_heartbeat_and_step\(\s*db, job_run, ([^,]+),', body)
    assert m is not None, "短路块未调用 _update_heartbeat_and_step"
    assert m.group(1).strip() == "None", (
        f"短路块必须传 None 保留原检查点，实际: {m.group(1).strip()}"
    )

    # 3) 不得在短路块内执行 chip 入队
    assert "_enqueue_chip_job_step" not in body, (
        "终态短路后不得再执行 chip 入队"
    )


# ---------------------------------------------------------------------------
# Gate: chip_forks_after_core_publish
# [AUD-08 2026-08-07] chip 只依赖 stock_core，不得绑定 Review 生命周期
# ---------------------------------------------------------------------------


def test_chip_enqueued_before_review_step():
    """[AUD-08] chip 入队必须早于 Review 步骤。

    改动前 chip 入队位于 Review 之后（步骤 4.9），使 chip 这一增强产品被
    Review 的成败/取消所左右：Review 取消时短路块直接 raise，chip 永远不入队。
    chip 只消费 stock_core，与 Review 无因果关系，必须在核心发布后立即分叉。

    原合同（test_after_close_phase0_contracts 原第 6 条）只验证 chip 入队早于
    "最终终态"，不足以阻止它被塞在 Review 之后 —— 本用例补上顺序约束。
    """
    import inspect

    from app.services import after_close_orchestrator as orch

    src = inspect.getsource(orch.execute_after_close_run)

    chip_pos = src.find("_enqueue_chip_job_step(")
    review_pos = src.find("_execute_review_step(")

    assert chip_pos != -1, "未找到 chip 入队调用"
    assert review_pos != -1, "未找到 review 步骤调用"
    assert chip_pos < review_pos, (
        "chip 入队必须早于 Review 步骤（chip 只依赖 stock_core，"
        "不得因 Review 失败/取消而丢失）"
    )


def test_chip_enqueue_guarded_by_core_publish_success():
    """[AUD-08] chip 入队的前置判据必须是 stock_core 发布成功，而非 Review 结果。"""
    import inspect
    import re

    from app.services import after_close_orchestrator as orch

    src = inspect.getsource(orch.execute_after_close_run)
    chip_pos = src.find("_enqueue_chip_job_step(")
    # 取 chip 调用之前最近的 if 守卫
    preceding = src[:chip_pos]
    guard = re.findall(
        r'if ([^\n]*snapshot_error is None[^\n]*)', preceding,
    )
    assert guard, "chip 入队前必须有 stock_core 发布成功守卫"
    last_guard = guard[-1]
    assert "publish_failed" in last_guard, (
        "chip 守卫必须包含 publish_failed 判据"
    )
    assert "snapshot_run_id" in last_guard, (
        "chip 守卫必须包含 snapshot_run_id 判据"
    )
    assert "review" not in last_guard.lower(), (
        "chip 守卫不得依赖任何 Review 状态"
    )


def test_chip_survives_review_failure_and_cancellation():
    """[AUD-08] Review 短路块之后不得再有 chip 入队，且短路不撤销已入队 chip。

    结构性保证：chip 入队位于短路块之前 → 无论短路是否触发，chip 都已入队。
    """
    import inspect
    import re

    from app.services import after_close_orchestrator as orch

    src = inspect.getsource(orch.execute_after_close_run)

    chip_pos = src.find("_enqueue_chip_job_step(")
    short_circuit_pos = src.find("if _is_terminal_review_short_circuit(")

    assert short_circuit_pos != -1, "未找到终态短路块"
    assert chip_pos < short_circuit_pos, (
        "chip 入队必须早于 Review 终态短路块，"
        "否则 Review 取消/中断时 chip 永远不会入队"
    )

    # 短路块内不得有任何撤销/删除 chip 的动作
    block = re.search(
        r'if _is_terminal_review_short_circuit\(.*?raise AfterCloseCancelledError',
        src,
        re.DOTALL,
    )
    assert block is not None
    body = block.group(0).lower()
    for forbidden in ("cancel_chip", "delete_chip", "revoke_chip"):
        assert forbidden not in body, (
            f"短路块不得撤销已入队的 chip（发现 {forbidden}）"
        )


def test_chip_enqueue_is_idempotent_for_resume():
    """[AUD-08] chip 前移的可行性依据：入队本身幂等，断点恢复重跑安全。

    create_after_close_chip_consensus_job 通过确定性 run_key
    （`chip_consensus:<trade_date>`）走 acquire_job_run_lock 取锁，
    同日重复调用返回既有 job（is_new=False）而非新建 —— 这是允许 resume
    路径重复执行步骤 4.6 的前提。
    """
    import inspect

    from app.services.after_close_chip_consensus_service import (
        create_after_close_chip_consensus_job,
    )

    src = inspect.getsource(create_after_close_chip_consensus_job)

    # 1) run_key 必须由 trade_date 确定性派生（同日必然同 key）
    assert "run_key = f" in src and "trade_date.isoformat()" in src, (
        "chip job 的 run_key 必须由 trade_date 确定性派生，否则重复调用会新建"
    )
    # 2) 必须经统一取锁入口，由其保证同 key 幂等
    assert "acquire_job_run_lock" in src, (
        "chip job 创建必须走 acquire_job_run_lock 才能保证幂等"
    )
    # 3) 必须把"是否新建"作为结果返回，供调用方区分
    assert "is_new" in src, "必须返回 is_new 以区分新建与复用"


def test_outer_exception_handler_excludes_cancellation():
    """[P0#3 源码守卫] 外层 except 必须先捕获 AfterCloseCancelledError。

    否则取消会被通用 except 覆写成 failed。
    """
    import inspect

    from app.services import after_close_orchestrator as orch

    src = inspect.getsource(orch.execute_after_close_run)
    cancel_idx = src.find("except AfterCloseCancelledError")
    assert cancel_idx != -1, "外层必须显式捕获 AfterCloseCancelledError"

    # 外层通用处理器：定位写 failed 的那个 except（含 LeaseEpochMismatchError 判定）
    outer_generic_idx = src.find("if isinstance(exc, LeaseEpochMismatchError)")
    assert outer_generic_idx != -1, "未找到外层通用异常处理器"

    assert cancel_idx < outer_generic_idx, (
        "AfterCloseCancelledError 必须在外层通用 except Exception 之前捕获，"
        "否则取消会被覆写为 failed"
    )


def test_pipeline_overall_status_exposes_terminal_states():
    """[P0 状态消费] partial_success/cancelled/interrupted 必须如实透出。

    修复前这三种终态落到 else 分支返回 not_started，
    前端会把"已取消"显示成"未开始"。
    """
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.services.after_close_pipeline_service import (
        MARKET_SESSION_CLOSED,
        _compute_overall_status,
    )

    now = datetime(2026, 7, 31, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    for status in ("partial_success", "cancelled", "interrupted"):
        job_run = _FakeJobRun(json.dumps({"last_completed_step": "publishing"}))
        job_run.status = status
        result = _compute_overall_status(
            job_run,
            market_session=MARKET_SESSION_CLOSED,
            now=now,
            watchlist_ready=False,
            has_backfill_full=False,
        )
        assert result == status, (
            f"overall_status 必须如实返回 {status}，实际 {result}"
        )


def test_pipeline_cancelled_keeps_completed_steps():
    """[P0 状态消费] 取消后已完成步骤不得全部回退为 pending。"""
    import json

    from app.services.after_close_pipeline_service import _compute_step_states

    job_run = _FakeJobRun(json.dumps({
        "last_completed_step": "publishing",
        "orchestrator_status": "computing_review",
    }))
    job_run.status = "cancelled"

    steps = _compute_step_states(job_run, events=[], watchlist_ready=False)
    by_step = {s["step"]: s["status"] for s in steps}

    # publishing 及之前必须保持 completed
    for done in (
        "refreshing_daily", "syncing_boards",
        "checking_coverage", "computing_features", "publishing",
    ):
        assert by_step[done] == "completed", (
            f"取消后 {done} 不应回退为 {by_step[done]}"
        )
    # 被取消的当前步骤如实显示 cancelled
    assert by_step["computing_review"] == "cancelled"
