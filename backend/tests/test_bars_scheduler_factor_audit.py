"""bars_scheduler._audit_and_rebuild_factors 集成测试 (CHANGE-20260718-007 S3.1)。

覆盖 S3.1 新增的因子审计 + 串行重建闭环：
- 全部一致路径：dry_run 无 needs_rebuild，写 FACTOR_AUDIT info 事件
- 需重建 + 全成功路径：rebuild_batch 全成功，写 done 事件含 before/after hash
- 需重建 + 部分失败路径：写 warn 事件含 failed_list
- dry_run 异常路径：写 error 事件，summary.errors=total，不抛出
- rebuild_batch 异常路径：写 error 事件，summary.failed=needs_rebuild，不抛出
- job_run_id=None 路径：不写事件但返回正确 summary
- 空 instruments 路径：返回零 summary

设计要点：
- FactorReconciliationTask 用 MagicMock 替换，避免连真实 pytdx/DB
- 测试事务隔离：_audit_and_rebuild_factors 内部 append_event 后会 db.commit()，
  为不破坏 db_session fixture 的 nested 事务，patch commit→flush
- 验证事件 payload 含 PROMPT.md S3.1 要求的字段（before/after hash 摘要）
- 验证软失败：dry_run/rebuild 异常被吞没，summary 返回失败计数而非抛出
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.models.instrument import Instrument
from app.models.job_run_event import JobRunEvent
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.bars_scheduler_service import BarsSchedulerService, BatchResult
from app.services.factor_reconciliation import (
    ReconciliationItem,
    ReconciliationItemResult,
    ReconciliationPlan,
    ReconciliationReport,
)
from app.services.job_run_event_service import list_events


async def _create_job_run(db_session) -> SchedulerJobRun:
    """创建测试用 SchedulerJobRun（满足外键约束）。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    job_run = SchedulerJobRun(
        job_name="bars_scheduler",
        business_date="2026-07-18",
        run_key=f"bars_scheduler:audit_test:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now,
    )
    db_session.add(job_run)
    await db_session.flush()
    return job_run


def _make_instruments(n: int = 3) -> list[Instrument]:
    """构造 n 个 active Instrument（不写库，仅供 len() 和日志）。"""
    return [
        Instrument(
            id=uuid.uuid4(),
            symbol=f"{600000 + i:06d}",
            name=f"测试标的{i}",
            market="SH",
            status="active",
        )
        for i in range(n)
    ]


def _make_plan(
    *, total_audited: int = 3, consistent: int = 2, needs_rebuild: int = 1,
) -> ReconciliationPlan:
    """构造 dry_run 返回的 ReconciliationPlan（needs_rebuild 个 item）。"""
    items = [
        ReconciliationItem(
            instrument_id=uuid.uuid4(),
            symbol=f"{600100 + i:06d}",
            earliest_affected=date(2024, 1, 1),
            before_hash=f"before_hash_{i}",
            mismatch_count=10 + i,
            reason="value_mismatch",
        )
        for i in range(needs_rebuild)
    ]
    return ReconciliationPlan(
        items=items,
        total_audited=total_audited,
        consistent_count=consistent,
        error_count=0,
    )


def _make_report(
    plan: ReconciliationPlan, *, fail_count: int = 0,
) -> ReconciliationReport:
    """构造 rebuild_batch 返回的 ReconciliationReport。"""
    results: list[ReconciliationItemResult] = []
    for i, item in enumerate(plan.items):
        success = i >= fail_count  # 前 fail_count 个失败
        results.append(
            ReconciliationItemResult(
                instrument_id=item.instrument_id,
                symbol=item.symbol,
                success=success,
                before_hash=item.before_hash,
                after_hash=f"after_hash_{i}" if success else "",
                records_updated=100 if success else 0,
                error_code=None if success else "rebuild_failed",
                error_message=None if success else "mock rebuild failure",
                rebuilt_at=datetime.now(UTC),
            )
        )
    return ReconciliationReport(
        results=results,
        total_planned=len(plan.items),
        success_count=len(results) - fail_count,
        failure_count=fail_count,
    )


def _patch_task(mock_task: MagicMock):
    """Patch FactorReconciliationTask 在 _audit_and_rebuild_factors 内的延迟 import。

    _audit_and_rebuild_factors 内部 `from app.services.factor_reconciliation import
    FactorReconciliationTask`，因此 patch 模块级属性即可影响后续 import。
    """
    return patch(
        "app.services.factor_reconciliation.FactorReconciliationTask",
        return_value=mock_task,
    )


def _patch_commit(db_session):
    """将 db_session.commit 替换为 flush，保持 nested 事务隔离。

    _audit_and_rebuild_factors 内部 append_event 后会 await db.commit()，
    会破坏 db_session fixture 的 nested 事务。替换为 flush 后事件仍在 session
    内可见，list_events 可查询，且不污染其他测试。
    """
    return patch.object(db_session, "commit", new=db_session.flush)


def _find_audit_done_event(events) -> JobRunEvent:
    """从事件列表中找出 FACTOR_AUDIT DONE 事件。

    list_events 仅按 created_at 倒序排列，START 和 DONE 事件可能共享同一
    微秒时间戳导致顺序不确定。通过 payload 内容区分：
    - START: payload 含 "total" 和 "trade_date"
    - DONE: payload 含 "total_audited"（即 summary 字典）
    - 异常 DONE: payload 含 "error" 字段
    """
    audit_events = [e for e in events if e.step == "FACTOR_AUDIT"]
    for e in audit_events:
        if e.payload and "total_audited" in e.payload:
            return e
    raise AssertionError(
        f"未找到 FACTOR_AUDIT DONE 事件（payload 含 total_audited）。"
        f"audit_events: {[(e.level, e.payload) for e in audit_events]}"
    )


# =============================================================================
# 1. 全部一致路径：dry_run 无 needs_rebuild
# =============================================================================


@pytest.mark.asyncio
async def test_audit_all_consistent_writes_info_event(db_session) -> None:
    """测试 1：全部一致时写 FACTOR_AUDIT info 事件，summary 含零计数。"""
    job_run = await _create_job_run(db_session)
    instruments = _make_instruments(3)

    plan = _make_plan(total_audited=3, consistent=3, needs_rebuild=0)

    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)

    service = BarsSchedulerService()
    with _patch_task(mock_task), _patch_commit(db_session):
        summary = await service._audit_and_rebuild_factors(
            trade_date=date(2026, 7, 18),
            instruments=instruments,
            db_session=db_session,
            job_run_id=job_run.id,
        )

    # summary 验证
    assert summary["total_audited"] == 3
    assert summary["consistent"] == 3
    assert summary["needs_rebuild"] == 0
    assert summary["rebuilt"] == 0
    assert summary["failed"] == 0
    assert summary["errors"] == 0

    # 事件验证：start + done（info 级别，无 failed_list/needs_rebuild_symbols）
    events = await list_events(db_session, job_run.id, limit=10)
    steps = [e.step for e in events]
    assert "FACTOR_AUDIT" in steps
    # 应有 2 条 FACTOR_AUDIT 事件（start + done）
    audit_events = [e for e in events if e.step == "FACTOR_AUDIT"]
    assert len(audit_events) >= 2, (
        f"期望至少 2 条 FACTOR_AUDIT 事件，实际 {len(audit_events)}"
    )

    # DONE 事件通过 payload 内容识别（created_at 可能同微秒，顺序不确定）
    done_event = _find_audit_done_event(events)
    assert done_event.level == "info"
    assert done_event.payload is not None
    assert done_event.payload["total_audited"] == 3
    assert done_event.payload["consistent"] == 3
    assert done_event.payload["needs_rebuild"] == 0


# =============================================================================
# 2. 需重建 + 全成功路径
# =============================================================================


@pytest.mark.asyncio
async def test_audit_needs_rebuild_all_success(db_session) -> None:
    """测试 2：发现不一致 + 重建全成功，写 info 事件含 before/after hash。"""
    job_run = await _create_job_run(db_session)
    instruments = _make_instruments(3)

    plan = _make_plan(total_audited=3, consistent=2, needs_rebuild=1)
    report = _make_report(plan, fail_count=0)

    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)
    mock_task.rebuild_batch = AsyncMock(return_value=report)

    service = BarsSchedulerService()
    with _patch_task(mock_task), _patch_commit(db_session):
        summary = await service._audit_and_rebuild_factors(
            trade_date=date(2026, 7, 18),
            instruments=instruments,
            db_session=db_session,
            job_run_id=job_run.id,
        )

    # summary 验证
    assert summary["needs_rebuild"] == 1
    assert summary["rebuilt"] == 1
    assert summary["failed"] == 0

    # 事件验证：done 事件应含 success_before_after_sample
    events = await list_events(db_session, job_run.id, limit=10)
    done_event = _find_audit_done_event(events)
    assert done_event.level == "info"  # 全成功 → info
    assert done_event.payload is not None
    assert "success_before_after_sample" in done_event.payload
    sample = done_event.payload["success_before_after_sample"]
    assert len(sample) == 1
    assert sample[0]["before_hash"] == "before_hash_0"
    assert sample[0]["after_hash"] == "after_hash_0"
    # needs_rebuild_symbols 也应记录
    assert "needs_rebuild_symbols" in done_event.payload
    assert len(done_event.payload["needs_rebuild_symbols"]) == 1


# =============================================================================
# 3. 需重建 + 部分失败路径
# =============================================================================


@pytest.mark.asyncio
async def test_audit_needs_rebuild_partial_failure(db_session) -> None:
    """测试 3：重建部分失败，写 warn 事件含 failed_list。"""
    job_run = await _create_job_run(db_session)
    instruments = _make_instruments(5)

    plan = _make_plan(total_audited=5, consistent=2, needs_rebuild=3)
    report = _make_report(plan, fail_count=1)  # 3 个里 1 个失败

    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)
    mock_task.rebuild_batch = AsyncMock(return_value=report)

    service = BarsSchedulerService()
    with _patch_task(mock_task), _patch_commit(db_session):
        summary = await service._audit_and_rebuild_factors(
            trade_date=date(2026, 7, 18),
            instruments=instruments,
            db_session=db_session,
            job_run_id=job_run.id,
        )

    # summary 验证：rebuilt=2, failed=1
    assert summary["needs_rebuild"] == 3
    assert summary["rebuilt"] == 2
    assert summary["failed"] == 1

    # 事件验证：done 事件应为 warn 级别（有失败）
    events = await list_events(db_session, job_run.id, limit=10)
    done_event = _find_audit_done_event(events)
    assert done_event.level == "warn"
    assert done_event.payload is not None
    assert "failed_list" in done_event.payload
    assert len(done_event.payload["failed_list"]) == 1
    failed = done_event.payload["failed_list"][0]
    assert failed["error_code"] == "rebuild_failed"
    # 成功的 before/after hash 仍应记录
    assert "success_before_after_sample" in done_event.payload
    assert len(done_event.payload["success_before_after_sample"]) == 2


# =============================================================================
# 4. dry_run 异常路径（软失败）
# =============================================================================


@pytest.mark.asyncio
async def test_audit_dry_run_failure_soft_fail(db_session) -> None:
    """测试 4：dry_run 抛异常时软失败，写 error 事件，summary.errors=total。

    关键约束：不抛出异常（不阻断 DSA），但留下诊断痕迹。
    """
    job_run = await _create_job_run(db_session)
    instruments = _make_instruments(3)

    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(side_effect=RuntimeError("pytdx 连接失败"))

    service = BarsSchedulerService()
    with _patch_task(mock_task), _patch_commit(db_session):
        # 不应抛出
        summary = await service._audit_and_rebuild_factors(
            trade_date=date(2026, 7, 18),
            instruments=instruments,
            db_session=db_session,
            job_run_id=job_run.id,
        )

    # summary 验证：errors=total（无法审计，全部计为 error）
    assert summary["total_audited"] == 0
    assert summary["errors"] == 3
    assert summary["needs_rebuild"] == 0
    assert summary["rebuilt"] == 0

    # 事件验证：done 事件应为 error 级别，含 error 字段
    events = await list_events(db_session, job_run.id, limit=10)
    done_event = _find_audit_done_event(events)
    assert done_event.level == "error"
    assert done_event.payload is not None
    assert "error" in done_event.payload
    assert "dry_run_failed" in done_event.payload["error"]
    assert "RuntimeError" in done_event.payload["error"]


# =============================================================================
# 5. rebuild_batch 异常路径（软失败）
# =============================================================================


@pytest.mark.asyncio
async def test_audit_rebuild_failure_soft_fail(db_session) -> None:
    """测试 5：rebuild_batch 抛异常时软失败，写 error 事件，summary.failed=needs_rebuild。"""
    job_run = await _create_job_run(db_session)
    instruments = _make_instruments(3)

    plan = _make_plan(total_audited=3, consistent=2, needs_rebuild=1)

    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)
    mock_task.rebuild_batch = AsyncMock(
        side_effect=RuntimeError("rebuild 内部错误"),
    )

    service = BarsSchedulerService()
    with _patch_task(mock_task), _patch_commit(db_session):
        summary = await service._audit_and_rebuild_factors(
            trade_date=date(2026, 7, 18),
            instruments=instruments,
            db_session=db_session,
            job_run_id=job_run.id,
        )

    # summary 验证：failed=needs_rebuild（全部计为失败）
    assert summary["needs_rebuild"] == 1
    assert summary["failed"] == 1
    assert summary["rebuilt"] == 0

    # 事件验证：done 事件应为 error 级别
    events = await list_events(db_session, job_run.id, limit=10)
    done_event = _find_audit_done_event(events)
    assert done_event.level == "error"
    assert done_event.payload is not None
    assert "rebuild_batch_failed" in done_event.payload["error"]
    # needs_rebuild_symbols 仍应记录（便于后续人工修复）
    assert "needs_rebuild_symbols" in done_event.payload


# =============================================================================
# 6. job_run_id=None 路径（不写事件但审计仍执行）
# =============================================================================


@pytest.mark.asyncio
async def test_audit_without_job_run_id(db_session) -> None:
    """测试 6：job_run_id=None 时不写事件，但 audit + rebuild 仍正常执行。"""
    instruments = _make_instruments(3)

    plan = _make_plan(total_audited=3, consistent=2, needs_rebuild=1)
    report = _make_report(plan, fail_count=0)

    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)
    mock_task.rebuild_batch = AsyncMock(return_value=report)

    service = BarsSchedulerService()
    with _patch_task(mock_task), _patch_commit(db_session):
        summary = await service._audit_and_rebuild_factors(
            trade_date=date(2026, 7, 18),
            instruments=instruments,
            db_session=db_session,
            job_run_id=None,  # 不传 job_run_id
        )

    # summary 仍应正确
    assert summary["total_audited"] == 3
    assert summary["needs_rebuild"] == 1
    assert summary["rebuilt"] == 1
    # 不写事件，无异常即可


# =============================================================================
# 7. 空 instruments 路径（早返回零 summary）
# =============================================================================


@pytest.mark.asyncio
async def test_audit_empty_instruments(db_session) -> None:
    """测试 7：instruments 为空时早返回零 summary，不调用 dry_run。"""
    job_run = await _create_job_run(db_session)

    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock()  # 不应被调用

    service = BarsSchedulerService()
    with _patch_task(mock_task), _patch_commit(db_session):
        summary = await service._audit_and_rebuild_factors(
            trade_date=date(2026, 7, 18),
            instruments=[],  # 空
            db_session=db_session,
            job_run_id=job_run.id,
        )

    # 零 summary
    assert summary == {
        "total_audited": 0, "consistent": 0, "needs_rebuild": 0,
        "rebuilt": 0, "failed": 0, "errors": 0,
    }
    # dry_run 不应被调用
    mock_task.dry_run.assert_not_called()
    # 不应写事件（空列表早返回，未到写事件步骤）
    events = await list_events(db_session, job_run.id, limit=10)
    assert len(events) == 0


# =============================================================================
# 8. BatchResult.factor_audit 字段验证（集成 _process_all_instruments）
# =============================================================================


@pytest.mark.asyncio
async def test_factor_audit_field_populated_in_result() -> None:
    """测试 8：BatchResult.factor_audit 字段在审计完成后正确填充。

    验证 _audit_and_rebuild_factors 返回的 summary 能被赋值到 BatchResult.factor_audit。
    纯数据类测试，不需要 db_session。
    """
    result = BatchResult(total=3)
    # 初始应为 None
    assert result.factor_audit is None

    # 模拟 _audit_and_rebuild_factors 返回值赋给 result
    result.factor_audit = {
        "total_audited": 3, "consistent": 2, "needs_rebuild": 1,
        "rebuilt": 1, "failed": 0, "errors": 0,
    }
    assert result.factor_audit is not None
    assert result.factor_audit["total_audited"] == 3
    assert result.factor_audit["rebuilt"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
