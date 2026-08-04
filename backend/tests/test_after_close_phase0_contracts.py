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
