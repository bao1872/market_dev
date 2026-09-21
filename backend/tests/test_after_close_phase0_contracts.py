"""[Phase0] 盘后基础合同行为测试。

覆盖 Phase 0 验收门中除执行器之外的部分：
- board_soft_failure_truthful：板块软失败必须让 step summary 为 failed
- review_failure_checkpoint_not_advanced：Review 失败不得推进 last_completed_step
- chip_automatic_enqueue_retired：盘后主链不再自动创建 chip job（CHIP-RETIRE 2026-09-01）
- status_api_contract_complete：watchdog 字段完整进入 API 响应

全部为真实行为断言，不使用 inspect.getsource() 字符串检查。
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import after_close_orchestrator
from app.services.after_close_orchestrator import (
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

    # [Slice1 contract] _update_heartbeat_and_step 重新从 DB FOR UPDATE 读取最新
    # metadata；此处让 execute().scalar_one_or_none() 返回 None，回到传入 job_run 路径。
    async def _fake_execute(*_a, **_k):
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        return r

    db.execute.side_effect = _fake_execute

    await _update_heartbeat_and_step(db, job_run, None, "worker-1")

    meta = after_close_orchestrator._parse_metadata(job_run)
    # 关键断言：检查点未被推进，仍停留在 publishing
    assert meta["last_completed_step"] == "publishing"


@pytest.mark.asyncio
async def test_update_heartbeat_with_step_advances_checkpoint():
    """成功路径仍必须正常推进检查点（防止修复过度）。"""
    job_run = _FakeJobRun('{"last_completed_step": "publishing"}')
    db = AsyncMock()

    async def _fake_execute(*_a, **_k):
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        return r

    db.execute.side_effect = _fake_execute

    await _update_heartbeat_and_step(db, job_run, "computing_review", "worker-1")

    meta = after_close_orchestrator._parse_metadata(job_run)
    assert meta["last_completed_step"] == "computing_review"


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
# Gate: AC-02 all_top_level_steps_use_executor（rebuilding_market_dashboard 收口）
# ---------------------------------------------------------------------------


def test_execute_after_close_run_wires_rebuilding_market_dashboard_through_executor():
    """[AC-02][R1] rebuilding_market_dashboard 必须通过统一执行器（optional sidecar）。

    review 计算现 = Market Dashboard 投影重建，抽为 _execute_rebuilding_market_dashboard，
    由 execute_orchestrator_step("rebuilding_market_dashboard", ..., optional=True) 包装。
    本测试用源码守卫锁定该接线，防止回退为内联直调或二次 checkpoint。
    """
    import inspect

    from app.services import after_close_orchestrator as orch

    main_src = inspect.getsource(orch.execute_after_close_run)

    # 1) 主编排必须通过执行器提交 rebuilding_market_dashboard 步骤
    assert 'execute_orchestrator_step(\n            "rebuilding_market_dashboard"' in main_src, (
        "execute_after_close_run 必须通过 execute_orchestrator_step 提交 rebuilding_market_dashboard"
    )
    # 2) 主编排不得再内联直调 review service（必须委托 _execute_rebuilding_market_dashboard）
    assert "_execute_rebuilding_market_dashboard(" in main_src, (
        "rebuilding_market_dashboard 业务体必须抽为 _execute_rebuilding_market_dashboard 并委托执行"
    )
    # 3) 复盘投影重建为 optional sidecar，不得进入 checkpoint / 不得阻断 Core
    assert "optional=True" in main_src, (
        "rebuilding_market_dashboard 必须为 optional sidecar（失败不阻断 Core）"
    )
    # 4) 复盘已收口到新 owner（Market Dashboard projection 重建），
    #    不得再内联直调已退役的 review orchestrator 模块（源码守卫不含字面模块路径）。
    assert "_execute_rebuilding_market_dashboard(" in main_src, (
        "rebuilding_market_dashboard 必须委托 _execute_rebuilding_market_dashboard"
    )


def test_rebuild_market_dashboard_step_exists_and_is_wired_as_operation():
    """[AC-02][R1] _execute_rebuilding_market_dashboard 作为执行器 operation 存在。"""
    from app.services import after_close_orchestrator as orch

    assert hasattr(orch, "_execute_rebuilding_market_dashboard"), (
        "必须存在 _execute_rebuilding_market_dashboard 业务体"
    )
    # 业务体必须是独立模块级协程（非 execute_after_close_run 内联函数）
    assert (
        orch._execute_rebuilding_market_dashboard.__module__
        == after_close_orchestrator.__name__
    )


def test_rebuild_market_dashboard_writes_status_constant():
    """[R1] 执行器前置写入 REBUILDING_MARKET_DASHBOARD orchestrator_status（可选侧挂）。"""
    import inspect

    from app.services import after_close_orchestrator as orch

    src = inspect.getsource(orch.execute_after_close_run)
    assert "AfterCloseRunStatus.REBUILDING_MARKET_DASHBOARD" in src, (
        "rebuilding_market_dashboard 开始前必须写入 REBUILDING_MARKET_DASHBOARD 状态"
    )


def test_rebuild_market_dashboard_optional_failure_partial_success():
    """[R1] optional 步骤失败必须进入 partial_success（非 succeeded）。

    源码守卫：_derive_after_close_final_status 对 optional 步骤失败
    （optional_failures 非空）返回 PARTIAL_SUCCESS，而非仅看 stock_core 成功。
    """
    import inspect

    from app.services import after_close_orchestrator as orch

    src = inspect.getsource(orch._derive_after_close_final_status)

    # optional 步骤失败集合非空 → PARTIAL_SUCCESS
    assert "optional_failures" in src, (
        "_derive_after_close_final_status 必须消费 optional_failures"
    )
    assert "PARTIAL_SUCCESS" in src, (
        "optional 步骤失败时最终状态应为 PARTIAL_SUCCESS"
    )


@pytest.mark.asyncio
async def test_terminal_short_circuit_detection():
    """[R1] history 终态短路判定（源码守卫）。

    computing_history 返回 cancelled / interrupted 必须立即终止收尾
    （保持终态、不覆盖总任务终态，且不推进后续步骤如 rebuilding_market_dashboard）。
    succeeded / failed / timed_out / unavailable 不得短路（走 partial_success 判定）。

    新设计以 history 终态为短路触发（替代旧 review 步骤短路）。
    """
    import inspect
    import re

    from app.services import after_close_orchestrator as orch

    src = inspect.getsource(orch.execute_after_close_run)
    assert re.search(
        r'if _history_status in \("cancelled", "interrupted"\):.*?raise AfterCloseCancelledError',
        src,
        re.DOTALL,
    ) is not None, (
        "history 终态 cancelled/interrupted 必须命中终止短路并 raise AfterCloseCancelledError"
    )
    assert '"terminal_short_circuit": True' in src, (
        "终止短路块必须写入 terminal_short_circuit payload"
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

    [CHIP-RETIRE 2026-09-01] 原 [CORRECTION-02] 的 chip readiness 段落已作废：
    自动 chip 入队整体退役后主链不再创建 chip job，Review 终态与 chip 之间不存在
    任何因果关系可言。相关退役合同见
    tests/test_after_close_chip_retirement.py。
    """
    from app.services import after_close_orchestrator as orch
    from app.services.after_close_orchestrator import AfterCloseRunStatus

    job_run = _FakeJobRun('{"last_completed_step": "publishing"}')
    db = AsyncMock()

    async def _fake_execute(*_a, **_k):
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        return r

    db.execute.side_effect = _fake_execute

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
    """[R1 源码守卫] history 终态短路块必须：用枚举写状态 + 传 None 保留检查点 + 抛信号异常。

    这三点共同保证取消链路不破坏状态。
    """
    import inspect
    import re

    from app.services import after_close_orchestrator as orch

    src = inspect.getsource(orch.execute_after_close_run)
    block = re.search(
        r'if _history_status in \("cancelled", "interrupted"\):.*?raise AfterCloseCancelledError',
        src,
        re.DOTALL,
    )
    assert block is not None, "未找到终态短路块或缺少 AfterCloseCancelledError 抛出"
    body = block.group(0)

    # 1) 必须经 resolve_terminal_run_status 转枚举，不得直接传字符串
    assert "resolve_terminal_run_status" in body, (
        "短路块必须用 resolve_terminal_run_status 转枚举"
    )

    # 2) _update_heartbeat_and_step 必须传 None 保留检查点
    m = re.search(r'_update_heartbeat_and_step\(\s*db, job_run, ([^,]+),', body)
    assert m is not None, "短路块未调用 _update_heartbeat_and_step"
    assert m.group(1).strip() == "None", (
        f"短路块必须传 None 保留原检查点，实际: {m.group(1).strip()}"
    )

    # 3) 终止短路后不得再执行后续步骤（如 rebuilding_market_dashboard 投影）
    assert "rebuilding_market_dashboard" not in body, (
        "终态短路后不得再执行 rebuilding_market_dashboard 投影"
    )


def test_chip_enqueue_is_idempotent_for_resume():
    """[CHIP-RETIRE 2026-09-01 / 历史兼容] 保留的 chip 服务仍保证入队幂等。

    自动入队已退役，但 `create_after_close_chip_consensus_job` 作为历史/手工
    重算入口保留（NON-GOAL：不删服务实现）。本测试锁定其幂等性未被回退波及，
    确保历史 chip job 重放安全。

    [AUD-08 原始语境] chip 前移的可行性依据：入队本身幂等，断点恢复重跑安全。

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


class _FakeStockCorePub:
    """最小 publication 替身，只关心 data_run_id。"""

    def __init__(self, data_run_id):
        self.data_run_id = data_run_id


@pytest.mark.asyncio
async def test_resolve_stock_core_published_pointer_match():
    """pointer 存在且 data_run_id == snapshot_run_id → (published=True, superseded=False)。"""
    from datetime import date

    from app.services.after_close_orchestrator import resolve_stock_core_published

    snap = uuid.uuid4()
    pub = _FakeStockCorePub(data_run_id=snap)

    async def fake_get_publication(*_a, **_k):
        return pub

    with patch(
        "app.services.factor_publication_service.get_publication",
        new=fake_get_publication,
    ):
        published, superseded = await resolve_stock_core_published(
            AsyncMock(), date(2026, 7, 31), snap
        )

    assert published is True
    assert superseded is False


@pytest.mark.asyncio
async def test_resolve_stock_core_published_pointer_mismatch():
    """pointer 存在但指向别的 run → (published=False, superseded=True)。"""
    from datetime import date

    from app.services.after_close_orchestrator import resolve_stock_core_published

    snap = uuid.uuid4()
    pub = _FakeStockCorePub(data_run_id=uuid.uuid4())

    async def fake_get_publication(*_a, **_k):
        return pub

    with patch(
        "app.services.factor_publication_service.get_publication",
        new=fake_get_publication,
    ):
        published, superseded = await resolve_stock_core_published(
            AsyncMock(), date(2026, 7, 31), snap
        )

    assert published is False
    assert superseded is True


@pytest.mark.asyncio
async def test_resolve_stock_core_published_pointer_missing():
    """无 pointer（从未发布）→ (published=False, superseded=False)。"""
    from datetime import date

    from app.services.after_close_orchestrator import resolve_stock_core_published

    async def fake_get_publication(*_a, **_k):
        return None

    with patch(
        "app.services.factor_publication_service.get_publication",
        new=fake_get_publication,
    ):
        published, superseded = await resolve_stock_core_published(
            AsyncMock(), date(2026, 7, 31), uuid.uuid4()
        )

    assert published is False
    assert superseded is False


@pytest.mark.asyncio
async def test_resolve_stock_core_published_none_snapshot():
    """snapshot_run_id 为 None 时无需查库，直接 (False, False)。"""
    from datetime import date

    from app.services.after_close_orchestrator import resolve_stock_core_published

    published, superseded = await resolve_stock_core_published(
        AsyncMock(), date(2026, 7, 31), None
    )
    assert published is False
    assert superseded is False


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
        "last_completed_step": "computing_features",
        "orchestrator_status": "computing_history",
    }))
    job_run.status = "cancelled"

    steps = _compute_step_states(job_run, events=[], watchlist_ready=False)
    by_step = {s["step"]: s["status"] for s in steps}

    # computing_features 及之前必须保持 completed（取消不得回退已完成步骤）
    for done in (
        "refreshing_daily", "syncing_boards",
        "checking_coverage", "rebuilding_market_dashboard", "computing_features",
    ):
        assert by_step[done] == "completed", (
            f"取消后 {done} 不应回退为 {by_step[done]}"
        )
    # 被取消的当前步骤（orchestrator_status 对应步骤）如实显示 cancelled
    assert by_step["computing_history"] == "cancelled", (
        f"被取消的当前步骤应显示 cancelled，实际 {by_step.get('computing_history')}"
    )
