"""bars_scheduler._audit_and_rebuild_factors 集成测试 (CHANGE-20260718-007 S3.1)。

覆盖 S3.1 新增的因子审计 + 串行重建闭环，以及 fail-closed 治理契约：
- 全部一致路径：dry_run 无 needs_rebuild，写 FACTOR_AUDIT info 事件
- 需重建 + 全成功路径：rebuild_batch 全成功，写 done 事件含 before/after hash
- 需重建 + 部分失败路径：failed > 0 = rebuild incomplete → 抛 FactorIntegrityBlockedError
- dry_run 异常路径：FACTOR_AUDIT_FAILED → 抛 FactorIntegrityBlockedError（fail-closed）
- rebuild_batch 异常路径：FACTOR_REBUILD_FAILED → 抛 FactorIntegrityBlockedError
- job_run_id=None 路径：不写事件但审计仍执行
- 空 instruments 路径：返回零 summary（含 degraded / degraded_symbols 字段）
- degraded > 0 路径：FACTOR_AUDIT_DEGRADED → 抛 FactorIntegrityBlockedError

设计要点：
- FactorReconciliationTask 用 MagicMock 替换，避免连真实 pytdx/DB
- 验证事件 payload 含 PROMPT.md S3.1 要求的字段（before/after hash 摘要）
- 软失败已被移除：任何 provider outage / audit failure / degraded / rebuild
  incomplete 必须 fail-closed（_run_post_daily_phase 对
  (FactorSourceUnavailableError, FactorIntegrityBlockedError) 统一 re-raise，
  保留 raw 日线但阻止 DSA/Core/Review 执行）。对应老测试（partial/dry_run/
  rebuild 的「不抛出」断言）已改写为断言 FactorIntegrityBlockedError。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.constants.factor_contract import (
    FACTOR_ALGORITHM_VERSION,
    FACTOR_RECONCILIATION_VERSION,
)
from app.models.instrument import Instrument
from app.models.job_run_event import JobRunEvent
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.bars_scheduler_service import (
    BarsSchedulerService,
    BatchResult,
    FactorIntegrityBlockedError,
    FactorSourceUnavailableError,
)
from app.services.factor_reconciliation import (
    ReconciliationItem,
    ReconciliationItemResult,
    ReconciliationPlan,
    ReconciliationReport,
    find_stale_version_instruments,
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
    degraded_count: int = 0, degraded_symbols: list[str] | None = None,
    error_count: int = 0,
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
        error_count=error_count,
        degraded_count=degraded_count,
        degraded_symbols=degraded_symbols or [],
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


def _seed_instrument(db_session, symbol: str, *, current: bool = True) -> None:
    """写入一只 active Instrument（current=True 表示已 stamp 当前版本，非 stale）。"""
    db_session.add(
        Instrument(
            id=uuid.uuid4(),
            symbol=symbol,
            name=symbol,
            market="SH",
            status="active",
            factor_algorithm_version=FACTOR_ALGORITHM_VERSION if current else None,
            factor_reconciliation_version=(
                FACTOR_RECONCILIATION_VERSION if current else None
            ),
            factor_reconciled_at=datetime.now(UTC) if current else None,
        )
    )


async def _get_id_by_symbol(db_session, symbol: str) -> uuid.UUID:
    from sqlalchemy import select as _select

    row = await db_session.execute(_select(Instrument.id).where(Instrument.symbol == symbol))
    return row.scalar_one()


async def _get_factor_version_fields(db_session, instrument_id: uuid.UUID) -> dict:
    """读取 instrument 的 3 个因子版本字段。"""
    row = await db_session.execute(
        text(
            "SELECT factor_algorithm_version, factor_reconciliation_version, "
            "factor_reconciled_at FROM instruments WHERE id = :id"
        ),
        {"id": instrument_id},
    )
    r = row.first()
    return {
        "factor_algorithm_version": r.factor_algorithm_version,
        "factor_reconciliation_version": r.factor_reconciliation_version,
        "factor_reconciled_at": r.factor_reconciled_at,
    }


def _patch_task(mock_task: MagicMock):
    """Patch FactorReconciliationTask 在 _audit_and_rebuild_factors 内的延迟 import。

    _audit_and_rebuild_factors 内部 `from app.services.factor_reconciliation import
    FactorReconciliationTask`，因此 patch 模块级属性即可影响后续 import。
    """
    return patch(
        "app.services.factor_reconciliation.FactorReconciliationTask",
        return_value=mock_task,
    )


def _patch_stamp(new: object | None = None):
    """Patch stamp_factor_reconciliation_versions_by_symbols（_stamp_verified_factor_scope
    在调用时延迟 import 该模块函数），便于验证 rollback / commit / 异常路径而不触达真实 DB。
    """
    if new is None:
        new = AsyncMock(return_value=0)
    return patch(
        "app.services.factor_reconciliation.stamp_factor_reconciliation_versions_by_symbols",
        new=new,
    )


def _mock_async_session() -> MagicMock:
    """构造 MagicMock AsyncSession：rollback/commit 为 AsyncMock（helper 内部 await）。"""
    s = MagicMock()
    s.rollback = AsyncMock()
    s.commit = AsyncMock()
    s.execute = AsyncMock(return_value=MagicMock())
    return s


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
    # [ROUND-5] degraded 字段必须存在且为 0（factor integrity 契约的一部分）
    assert summary["degraded"] == 0
    assert summary["degraded_symbols"] == []

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
    assert summary["audit_rebuilt"] == 1  # [PROMPT.md §5.4.2 V2]
    assert summary["failed"] == 0
    assert summary["failed_symbols"] == []  # [PROMPT.md §5.4.2 V2] 无失败
    assert summary["trade_date"] == "2026-07-18"  # [PROMPT.md §5.4.2 V2]
    # [ROUND-5] degraded 字段必须存在且为 0
    assert summary["degraded"] == 0
    assert summary["degraded_symbols"] == []

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
async def test_audit_needs_rebuild_partial_failure_blocks_core() -> None:
    """测试 3：重建部分失败（failed > 0 = rebuild incomplete）必须 fail-closed，
    抛 FactorIntegrityBlockedError(FACTOR_REBUILD_INCOMPLETE)，禁止静默继续 Core。

    用 MagicMock db_session + job_run_id=None 保持纯单元（不写事件、不连 DB）。
    rebuild_batch 会实际被调用（尝试重建），但只要有任一只失败，整体即 hard-fail。
    """
    instruments = _make_instruments(5)
    plan = _make_plan(total_audited=5, consistent=2, needs_rebuild=3)
    report = _make_report(plan, fail_count=1)  # 3 个里 1 个失败

    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)
    mock_task.rebuild_batch = AsyncMock(return_value=report)

    service = BarsSchedulerService()
    with _patch_task(mock_task):
        with pytest.raises(
            FactorIntegrityBlockedError, match="FACTOR_REBUILD_INCOMPLETE"
        ):
            await service._audit_and_rebuild_factors(
                trade_date=date(2026, 7, 18),
                instruments=instruments,
                db_session=MagicMock(),
                job_run_id=None,
            )

    # rebuild_batch 确实被调用（尝试重建），但因 incomplete 立即 hard-fail
    mock_task.rebuild_batch.assert_called_once()


# =============================================================================
# 4. dry_run 异常路径（软失败）
# =============================================================================


@pytest.mark.asyncio
async def test_audit_dry_run_failure_hard_fail() -> None:
    """测试 4：dry_run 抛异常必须 fail-closed，抛 FactorIntegrityBlockedError。

    关键约束：禁止软失败、禁止带着无法证明的 factor 继续 DSA/Core。
    用 MagicMock db_session + job_run_id=None 保持纯单元（不写事件、不连 DB），
    与生产 degraded 测试一致，可在 PURE_UNIT_TEST=1 下真正运行并验证，
    而非被 PostgreSQL skip 遮掩。
    """
    instruments = _make_instruments(3)

    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(side_effect=RuntimeError("pytdx 连接失败"))

    service = BarsSchedulerService()
    with _patch_task(mock_task):
        with pytest.raises(
            FactorIntegrityBlockedError, match="FACTOR_AUDIT_FAILED"
        ):
            await service._audit_and_rebuild_factors(
                trade_date=date(2026, 7, 18),
                instruments=instruments,
                db_session=MagicMock(),
                job_run_id=None,
            )

    # dry_run 抛异常时已 raise，rebuild_batch 不应被调用
    mock_task.rebuild_batch.assert_not_called()


# =============================================================================
# 5. rebuild_batch 异常路径（软失败）
# =============================================================================


@pytest.mark.asyncio
async def test_audit_rebuild_failure_hard_fail() -> None:
    """测试 5：rebuild_batch 抛异常必须 fail-closed，抛 FactorIntegrityBlockedError。"""
    instruments = _make_instruments(3)
    plan = _make_plan(total_audited=3, consistent=2, needs_rebuild=1)

    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)
    mock_task.rebuild_batch = AsyncMock(
        side_effect=RuntimeError("rebuild 内部错误"),
    )

    service = BarsSchedulerService()
    with _patch_task(mock_task):
        with pytest.raises(
            FactorIntegrityBlockedError, match="FACTOR_REBUILD_FAILED"
        ):
            await service._audit_and_rebuild_factors(
                trade_date=date(2026, 7, 18),
                instruments=instruments,
                db_session=MagicMock(),
                job_run_id=None,
            )


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
    # [ROUND-5] degraded 字段必须存在且为 0
    assert summary["degraded"] == 0
    assert summary["degraded_symbols"] == []
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
    # [PROMPT.md §5.4.2 V2] summary 必须包含 trade_date / audit_rebuilt / failed_symbols 字段
    # [G1B-3A] 新增 audit_mode / requested_symbols（空 instruments 走 full_market 分支）
    assert summary == {
        "trade_date": "2026-07-18",
        "total_audited": 0, "consistent": 0, "needs_rebuild": 0,
        "audit_rebuilt": 0, "rebuilt": 0, "failed": 0, "errors": 0,
        "failed_symbols": [],
        "degraded": 0, "degraded_symbols": [],
        "audit_mode": "full_market", "requested_symbols": 0,
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
        "trade_date": "2026-07-18",  # [PROMPT.md §5.4.2 V2]
        "total_audited": 3, "consistent": 2, "needs_rebuild": 1,
        "audit_rebuilt": 1, "rebuilt": 1, "failed": 0, "errors": 0,
        "failed_symbols": [],
        "degraded": 0, "degraded_symbols": [],  # [ROUND-5] 补齐 degraded 字段
    }
    assert result.factor_audit is not None
    assert result.factor_audit["total_audited"] == 3
    assert result.factor_audit["rebuilt"] == 1
    # [PROMPT.md §5.4.2 V2] 新增字段
    assert result.factor_audit["audit_rebuilt"] == 1
    assert result.factor_audit["trade_date"] == "2026-07-18"
    assert result.factor_audit["failed_symbols"] == []


# =============================================================================
# 9. [PROMPT.md §5.4.4 V2] AdjustmentFactorService._invalidate_capture_cache
#    Capture 缓存清理（filesystem per-event key，按 instrument_id 精确删除）
# =============================================================================


def test_invalidate_capture_cache_deletes_only_matching_files(tmp_path) -> None:
    """测试 9：_invalidate_capture_cache 只删除匹配 instrument_id 的缓存文件。

    场景：cache 目录下有 3 个文件
      - {event_a}_{target_iid}_v1_...  → 应删除
      - {event_b}_{target_iid}_v1_...  → 应删除（同 instrument_id 不同 event）
      - {event_a}_{other_iid}_v1_...   → 不应删除（不同 instrument_id）
      - random_other_file.png          → 不应删除（无 instrument_id token）
    """
    from app.services.adjustment_factor_service import AdjustmentFactorService

    target_iid = uuid.uuid4()
    other_iid = uuid.uuid4()
    event_a = uuid.uuid4()
    event_b = uuid.uuid4()

    cache_dir = tmp_path / "captures" / "cache"
    cache_dir.mkdir(parents=True)

    # 创建 4 个缓存文件
    target_file_a = cache_dir / f"{event_a}_{target_iid}_v1_tf=1d_dsf=1_iv=node_cluster"
    target_file_b = cache_dir / f"{event_b}_{target_iid}_v1_tf=1d_dsf=1_iv=smc"
    other_file = cache_dir / f"{event_a}_{other_iid}_v1_tf=1d_dsf=1_iv=node_cluster"
    unrelated_file = cache_dir / "random_other_file.png"

    for f in [target_file_a, target_file_b, other_file, unrelated_file]:
        f.write_bytes(b"\x89PNG\r\n\x1a\nfake_png_bytes")

    # patch _CACHE_DIR 到 tmp_path 下的目录
    with patch("app.services.stock_capture_service._CACHE_DIR", str(cache_dir)):
        service = AdjustmentFactorService()
        deleted = service._invalidate_capture_cache(target_iid)

    # 验证：删除 2 个（target_file_a + target_file_b），保留 2 个
    assert deleted == 2
    assert not target_file_a.exists()
    assert not target_file_b.exists()
    assert other_file.exists()  # 不同 instrument_id 保留
    assert unrelated_file.exists()  # 无关文件保留


def test_invalidate_capture_cache_missing_dir_returns_zero(tmp_path) -> None:
    """测试 10：cache 目录不存在时返回 0，不抛异常。"""
    from app.services.adjustment_factor_service import AdjustmentFactorService

    nonexistent_dir = tmp_path / "nonexistent"
    with patch("app.services.stock_capture_service._CACHE_DIR", str(nonexistent_dir)):
        service = AdjustmentFactorService()
        deleted = service._invalidate_capture_cache(uuid.uuid4())

    assert deleted == 0


def test_invalidate_capture_cache_empty_dir_returns_zero(tmp_path) -> None:
    """测试 11：cache 目录为空时返回 0。"""
    from app.services.adjustment_factor_service import AdjustmentFactorService

    empty_dir = tmp_path / "captures" / "cache"
    empty_dir.mkdir(parents=True)
    with patch("app.services.stock_capture_service._CACHE_DIR", str(empty_dir)):
        service = AdjustmentFactorService()
        deleted = service._invalidate_capture_cache(uuid.uuid4())

    assert deleted == 0


@pytest.mark.asyncio
async def test_invalidate_downstream_caches_includes_capture_field() -> None:
    """测试 12：_invalidate_downstream_caches 返回值包含 capture 字段。

    [PROMPT.md §5.4.4 V2] 因子变化后必须清理 Capture 缓存。
    验证返回 dict 含 4 个键：mdas / bars / indicator / capture
    """
    from app.services.adjustment_factor_service import AdjustmentFactorService

    service = AdjustmentFactorService()
    target_iid = uuid.uuid4()

    # mock 各缓存清理函数（不实际连 Redis / DB / filesystem）
    with patch.object(service, "_invalidate_mdas_cache", return_value=5), \
         patch("app.services.bars_cache.invalidate_bars_cache", new=AsyncMock(return_value=3)), \
         patch("app.services.indicator_cache.invalidate", new=AsyncMock(return_value=2)), \
         patch.object(service, "_invalidate_capture_cache", return_value=4):
        result = await service._invalidate_downstream_caches(target_iid)

    # 4 个字段全部存在
    assert set(result.keys()) == {"mdas", "bars", "indicator", "capture"}
    assert result["mdas"] == 5
    assert result["bars"] == 3
    assert result["indicator"] == 2
    assert result["capture"] == 4  # [PROMPT.md §5.4.4 V2] Capture 字段必须有值


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])


# =============================================================================
# 13. [ROUND-4] degraded_count 必须 hard-fail（不得静默继续 Core）
# =============================================================================


@pytest.mark.asyncio
async def test_audit_degraded_blocks_core() -> None:
    """degraded_count > 0 必须 hard-fail（FactorIntegrityBlockedError），
    禁止带着「无法证明 factor 正确性」的股票进入 DSA/Core/Review。

    用 job_run_id=None + MagicMock db_session 保持纯单元（不写事件、不连 DB）；
    degraded 检查在写 done 事件之前触发，因此 rebuild_batch 不应被调用。
    """
    instruments = _make_instruments(3)

    plan = _make_plan(
        total_audited=5293, consistent=5292, needs_rebuild=0,
        degraded_count=1, degraded_symbols=["600519"],
    )

    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)

    service = BarsSchedulerService()
    with _patch_task(mock_task):
        with pytest.raises(FactorIntegrityBlockedError, match="FACTOR_AUDIT_DEGRADED"):
            await service._audit_and_rebuild_factors(
                trade_date=date(2026, 7, 18),
                instruments=instruments,
                db_session=MagicMock(),  # job_run_id=None → 不写事件，dry_run 为 mock
                job_run_id=None,
            )

    # 在 degraded 处已 raise，rebuild_batch 不应被调用
    mock_task.rebuild_batch.assert_not_called()


# =============================================================================
# 14. [ROUND-4] AfterClose 首遍 detect 必须 force_refresh=True
# =============================================================================


@pytest.mark.asyncio
async def test_rebuild_detect_forces_fresh_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """AfterClose 首遍 detect 必须 force_refresh=True，绕过 24h XDXR 缓存。

    用 MagicMock session + job_run_id=None 保持纯单元，detect 全部返回 None
    （无变化）以跳过 rebuild、避免 DB 写。只验证 detect 调用确实带 force_refresh=True。
    """
    from app.services.bars_scheduler_service import BarsSchedulerService

    service = BarsSchedulerService()
    instruments = _make_instruments(3)

    captured: list[tuple[str, bool]] = []

    async def fake_detect(
        session: Any, instrument_id: Any, symbol: str, adapter: Any, *,
        force_refresh: bool = False, effective_as_of: Any = None,
    ) -> None:
        captured.append((symbol, force_refresh))
        return None  # 无变化，跳过重建

    mock_adj = MagicMock()
    mock_adj.detect_company_action_change = fake_detect
    mock_adj.rebuild_factor_series = AsyncMock()

    monkeypatch.setattr(
        "app.services.adjustment_factor_service.AdjustmentFactorService",
        lambda: mock_adj,
    )

    result = await service._rebuild_factors_if_needed(
        trade_date=date(2026, 9, 11),
        instruments=instruments,
        db_session=MagicMock(),
        job_run_id=None,
    )

    assert result["checked"] == 3
    assert len(captured) == 3
    assert all(fr is True for _, fr in captured), "所有 detect 必须 force_refresh=True"


# =============================================================================
# 15. [ROUND-4] rebuild 内 PytdxSourceError 参与熔断
# =============================================================================


@pytest.mark.asyncio
async def test_rebuild_pytdx_source_error_trips_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rebuild_factor_series 抛 PytdxSourceError → 单只 freshness 失败立即 fail-closed
    （FACTOR_SOURCE_SYMBOL_FRESHNESS_FAILED），并回滚该票 fingerprint；
    不等待连续 N 只、不继续后续标的。
    """
    from app.core.pytdx_adapter import PytdxSourceError
    from app.services.bars_scheduler_service import BarsSchedulerService

    service = BarsSchedulerService()
    instruments = _make_instruments(3)

    mock_adj = MagicMock()
    mock_adj.detect_company_action_change = AsyncMock(
        side_effect=lambda *a, **k: date(2024, 1, 1)  # 有变化，触发 rebuild
    )
    mock_adj.rebuild_factor_series = AsyncMock(
        side_effect=PytdxSourceError(
            operation="rebuild_factor_series", message="socket dropped mid rebuild"
        )
    )
    mock_adj._delete_fingerprint = MagicMock()

    monkeypatch.setattr(
        "app.services.adjustment_factor_service.AdjustmentFactorService",
        lambda: mock_adj,
    )
    monkeypatch.setattr(
        "app.services.bars_scheduler_service.get_pytdx_adapter", lambda: object()
    )

    db_session = MagicMock()
    db_session.rollback = AsyncMock()
    db_session.commit = AsyncMock()

    with pytest.raises(
        FactorSourceUnavailableError, match="FACTOR_SOURCE_SYMBOL_FRESHNESS_FAILED"
    ):
        await service._rebuild_factors_if_needed(
            trade_date=date(2026, 9, 11),
            instruments=instruments,
            db_session=db_session,
            job_run_id=None,
        )

    # 仅第一只触发 rebuild 失败即 fail-closed；不继续后续标的
    assert mock_adj.detect_company_action_change.call_count == 1
    assert mock_adj.rebuild_factor_series.call_count == 1
    # rebuild 阶段 provider 失败必须回滚 fingerprint，避免下轮误判「无变化」
    mock_adj._delete_fingerprint.assert_called_once_with(instruments[0].id)


# =============================================================================
# 16. [G1B-3A] 影响集解析 _resolve_factor_audit_symbols
# =============================================================================


@pytest.mark.asyncio
async def test_resolve_impact_set_union(db_session) -> None:
    """[G1B-3A] changed ∪ failed ∪ stale 求并集，stale 来自 find_stale_version_instruments。

    changed=[A,B] + failed=[D] + stale=[C] → 影响集 = [A,B,C,D]（确定性排序）。
    """
    await _seed_instrument(db_session, "RSV_C", current=False)  # stale
    await _seed_instrument(db_session, "RSV_A", current=True)
    await _seed_instrument(db_session, "RSV_B", current=True)
    await _seed_instrument(db_session, "RSV_D", current=True)

    service = BarsSchedulerService()
    rebuild_result = {"changed_symbols": ["RSV_A", "RSV_B"], "failed_symbols": ["RSV_D"]}
    got = await service._resolve_factor_audit_symbols(db_session, rebuild_result)

    stale = await find_stale_version_instruments(db_session)
    expected = sorted({s for _, s in stale} | {"RSV_A", "RSV_B", "RSV_D"})
    assert got == expected


@pytest.mark.asyncio
async def test_resolve_impact_set_dedup_overlap(db_session) -> None:
    """[G1B-3A] changed 与 stale 重复 → 只审一次（集合去重，确定性排序）。"""
    await _seed_instrument(db_session, "RSV_C", current=False)  # stale

    service = BarsSchedulerService()
    # RSV_C 既在 changed 又在 stale
    rebuild_result = {"changed_symbols": ["RSV_C", "RSV_A"], "failed_symbols": ["RSV_B"]}
    got = await service._resolve_factor_audit_symbols(db_session, rebuild_result)

    stale = await find_stale_version_instruments(db_session)
    expected = sorted({s for _, s in stale} | {"RSV_C", "RSV_A", "RSV_B"})
    assert got == expected
    assert len(got) == len(set(got)), "影响集不应含重复"


@pytest.mark.asyncio
async def test_resolve_impact_set_first_baseline_full_market(db_session) -> None:
    """[G1B-3A] 首次部署全市场 NULL → stale=全市场 → 影响集=全部 active。"""
    for s in ["BASE1", "BASE2", "BASE3"]:
        await _seed_instrument(db_session, s, current=False)

    service = BarsSchedulerService()
    rebuild_result = {"changed_symbols": [], "failed_symbols": []}
    got = await service._resolve_factor_audit_symbols(db_session, rebuild_result)

    stale = await find_stale_version_instruments(db_session)
    expected = sorted({s for _, s in stale})
    assert got == expected
    assert set(got) == {"BASE1", "BASE2", "BASE3"}


# =============================================================================
# 17. [G1B-3A] _run_post_daily_phase 使用影响集（无影响则跳过全市场审计）
# =============================================================================


@pytest.mark.asyncio
async def test_run_post_daily_skips_audit_when_no_impact(db_session) -> None:
    """[G1B-3A] 无 changed/failed/stale → 不调用 _audit_and_rebuild_factors，
    result.factor_audit 为 skipped_no_impact 零摘要。
    """
    from unittest.mock import AsyncMock as _AsyncMock

    service = BarsSchedulerService()

    health = MagicMock()
    health.available = True
    health.error = None
    health.latency_seconds = 0.1
    health.connected_server = "mock-host"
    health.uncached_xdxr_ok = True
    health.provider = "pytdx"

    with patch(
        "app.services.bars_scheduler_service.probe_factor_provider",
        new=_AsyncMock(return_value=health),
    ):
        service._rebuild_factors_if_needed = _AsyncMock(
            return_value={
                "trade_date": "2026-07-18",
                "checked": 0, "changed": 0, "rebuilt": 0, "failed": 0,
                "changed_symbols": [], "rebuilt_symbols": [], "failed_symbols": [],
            }
        )
        service._audit_and_rebuild_factors = _AsyncMock()
        service._check_daily_coverage_and_trigger_dsa = _AsyncMock(return_value=None)

        result = BatchResult(total=3)
        await service._run_post_daily_phase(
            date(2026, 7, 18),
            _make_instruments(3),
            db_session,
            None,
            result,
            trigger_dsa=False,
        )

    service._audit_and_rebuild_factors.assert_not_called()
    assert result.factor_audit is not None
    assert result.factor_audit["audit_mode"] == "skipped_no_impact"
    assert result.factor_audit["total_audited"] == 0


# =============================================================================
# 18. [G1B-3A] _audit_and_rebuild_factors impact-set 完整性 + 成功 stamp
# =============================================================================


@pytest.mark.asyncio
async def test_audit_impact_set_scope_incomplete(db_session) -> None:
    """[G1B-3A] 请求 3 只但 dry_run 只审到 2 只 → FACTOR_AUDIT_SCOPE_INCOMPLETE。"""
    plan = _make_plan(total_audited=2, consistent=2, needs_rebuild=0)
    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)

    service = BarsSchedulerService()
    with _patch_task(mock_task):
        with pytest.raises(
            FactorIntegrityBlockedError, match="FACTOR_AUDIT_SCOPE_INCOMPLETE"
        ):
            await service._audit_and_rebuild_factors(
                trade_date=date(2026, 7, 18),
                instruments=_make_instruments(3),
                db_session=MagicMock(),
                job_run_id=None,
                symbols=["A", "B", "C"],
            )


@pytest.mark.asyncio
async def test_audit_impact_set_degraded_no_stamp(db_session) -> None:
    """[G1B-3A] degraded > 0 → FACTOR_AUDIT_DEGRADED，不 stamp（fail-closed）。"""
    plan = _make_plan(
        total_audited=3, consistent=2, needs_rebuild=0,
        degraded_count=1, degraded_symbols=["X1"],
    )
    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)

    service = BarsSchedulerService()
    with _patch_task(mock_task):
        with pytest.raises(
            FactorIntegrityBlockedError, match="FACTOR_AUDIT_DEGRADED"
        ):
            await service._audit_and_rebuild_factors(
                trade_date=date(2026, 7, 18),
                instruments=_make_instruments(3),
                db_session=MagicMock(),
                job_run_id=None,
                symbols=["A", "B", "C"],
            )


@pytest.mark.asyncio
async def test_audit_impact_set_rebuild_failed_no_stamp(db_session) -> None:
    """[G1B-3A] rebuild 失败 → FACTOR_REBUILD_INCOMPLETE，不 stamp（fail-closed）。"""
    plan = _make_plan(total_audited=3, consistent=2, needs_rebuild=1)
    report = _make_report(plan, fail_count=1)

    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)
    mock_task.rebuild_batch = AsyncMock(return_value=report)

    service = BarsSchedulerService()
    with _patch_task(mock_task):
        with pytest.raises(
            FactorIntegrityBlockedError, match="FACTOR_REBUILD_INCOMPLETE"
        ):
            await service._audit_and_rebuild_factors(
                trade_date=date(2026, 7, 18),
                instruments=_make_instruments(5),
                db_session=MagicMock(),
                job_run_id=None,
                symbols=["A", "B", "C"],
            )


@pytest.mark.asyncio
async def test_audit_impact_set_success_stamps_scope(db_session) -> None:
    """[G1B-3A] impact-set 全部一致 → 成功后批量 stamp 整个 scope 并 commit。

    验证 stamp 生效：3 只 instrument 的 factor_reconciled_at / 版本均被写入。
    """
    syms = ["STMP1", "STMP2", "STMP3"]
    for s in syms:
        await _seed_instrument(db_session, s, current=False)  # 先 NULL（stale）

    plan = _make_plan(total_audited=3, consistent=3, needs_rebuild=0)
    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)

    service = BarsSchedulerService()
    with _patch_task(mock_task), _patch_commit(db_session):
        summary = await service._audit_and_rebuild_factors(
            trade_date=date(2026, 7, 18),
            instruments=_make_instruments(3),
            db_session=db_session,
            job_run_id=None,
            symbols=syms,
        )

    assert summary["audit_mode"] == "impact_set"
    assert summary["requested_symbols"] == 3

    for s in syms:
        iid = await _get_id_by_symbol(db_session, s)
        fields = await _get_factor_version_fields(db_session, iid)
        assert fields["factor_reconciled_at"] is not None, f"{s} 应被 stamp"
        assert fields["factor_algorithm_version"] == FACTOR_ALGORITHM_VERSION
        assert fields["factor_reconciliation_version"] == FACTOR_RECONCILIATION_VERSION


# =============================================================================
# 18b. [G1B-3A.1] fail-closed 门禁 + 原子 stamp（pure-unit，不依赖 postgres）
# =============================================================================
# 说明：以下测试使用 MagicMock session + 延迟 import patch，属于 pure_unit，
# 在 PURE_UNIT_TEST=1 下实际执行（asyncio_mode = "auto"，无需 @pytest.mark.asyncio）。


async def test_audit_impact_set_one_error_hard_fails() -> None:
    """[G1B-3A.1] impact-set 100 只中 1 只 error（ratio 1%）→ FACTOR_AUDIT_IMPACT_ERRORS。

    旧 full_market 1% provider-health 阈值会放过，但 impact-set 是零容忍：
    stamp 前必须全部证明。rebuild_batch / stamp 均不得调用。
    """
    plan = _make_plan(total_audited=100, consistent=99, needs_rebuild=0, error_count=1)
    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)

    service = BarsSchedulerService()
    with _patch_task(mock_task), _patch_stamp() as stamp:
        with pytest.raises(
            FactorIntegrityBlockedError, match="FACTOR_AUDIT_IMPACT_ERRORS"
        ):
            await service._audit_and_rebuild_factors(
                trade_date=date(2026, 7, 18),
                instruments=_make_instruments(3),
                db_session=MagicMock(),
                job_run_id=None,
                symbols=[f"S{i:06d}" for i in range(100)],
            )
    mock_task.rebuild_batch.assert_not_called()
    stamp.assert_not_called()


async def test_audit_impact_set_baseline_errors_hard_fails() -> None:
    """[G1B-3A.1] 首次 baseline 8000 只中 40 只 error（ratio 0.5%）< 1%。

    但 impact-set 零容忍：40/8000 不能 stamp 全市场。必须 FACTOR_AUDIT_IMPACT_ERRORS。
    不构造真实对象，仅 mock plan count。
    """
    plan = _make_plan(total_audited=8000, consistent=7960, needs_rebuild=0, error_count=40)
    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)

    service = BarsSchedulerService()
    with _patch_task(mock_task), _patch_stamp() as stamp:
        with pytest.raises(
            FactorIntegrityBlockedError, match="FACTOR_AUDIT_IMPACT_ERRORS"
        ):
            await service._audit_and_rebuild_factors(
                trade_date=date(2026, 7, 18),
                instruments=_make_instruments(1),
                db_session=MagicMock(),
                job_run_id=None,
                symbols=[f"S{i:06d}" for i in range(8000)],
            )
    stamp.assert_not_called()


async def test_audit_full_market_keeps_legacy_health_policy() -> None:
    """[G1B-3A.1] symbols=None（legacy full_market）保留原 1% provider-health 阈值。

    1000 只中 5 只 error（ratio 0.5%）< 1% → 不触发 FACTOR_AUDIT_IMPACT_ERRORS，
    且全市场模式不 stamp。本轮不改动旧生产语义。
    """
    plan = _make_plan(total_audited=1000, consistent=995, needs_rebuild=0, error_count=5)
    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)

    service = BarsSchedulerService()
    with _patch_task(mock_task), _patch_stamp() as stamp:
        summary = await service._audit_and_rebuild_factors(
            trade_date=date(2026, 7, 18),
            instruments=_make_instruments(1000),
            db_session=MagicMock(),
            job_run_id=None,
            symbols=None,  # full_market
        )
    assert summary["audit_mode"] == "full_market"
    assert summary["errors"] == 5
    # 旧 full_market 阈值未改变：本函数正常返回，不抛 IMPACT_ERRORS
    stamp.assert_not_called()


async def test_audit_impact_set_runs_with_empty_instruments() -> None:
    """[G1B-3A.1] instruments=[] 但 impact symbols 非空 → 不得 early return，dry_run 必须执行。

    DB 自身验证 symbols 是否真实存在（scope completeness gate），不信调用方 cache。
    """
    plan = _make_plan(total_audited=1, consistent=1, needs_rebuild=0, error_count=0)
    mock_task = MagicMock()
    mock_task.dry_run = AsyncMock(return_value=plan)

    service = BarsSchedulerService()
    with _patch_task(mock_task), _patch_stamp(new=AsyncMock(return_value=1)):
        summary = await service._audit_and_rebuild_factors(
            trade_date=date(2026, 7, 18),
            instruments=[],  # 空缓存
            db_session=_mock_async_session(),
            job_run_id=None,
            symbols=["A"],
        )
    mock_task.dry_run.assert_called_once()  # 证明未 early return
    assert summary["audit_mode"] == "impact_set"
    assert summary["requested_symbols"] == 1


async def test_stamp_verified_scope_incomplete_rolls_back() -> None:
    """[G1B-3A.1] stamp rowcount 不足（expected=3, stamped=2）→ rollback + INCOMPLETE。

    不依赖上层 session 生命周期碰巧回滚。commit 不得调用。
    """
    session = _mock_async_session()
    service = BarsSchedulerService()
    with patch(
        "app.services.factor_reconciliation.stamp_factor_reconciliation_versions_by_symbols",
        new=AsyncMock(return_value=2),
    ):
        with pytest.raises(
            FactorIntegrityBlockedError, match="FACTOR_VERSION_STAMP_INCOMPLETE"
        ):
            await service._stamp_verified_factor_scope(session, ["A", "B", "C"])
    session.rollback.assert_called_once()
    session.commit.assert_not_called()


async def test_stamp_verified_scope_exception_rolls_back() -> None:
    """[G1B-3A.1] stamp 底层异常 → rollback + FACTOR_VERSION_STAMP_FAILED。commit 不得调用。"""
    session = _mock_async_session()
    service = BarsSchedulerService()
    with patch(
        "app.services.factor_reconciliation.stamp_factor_reconciliation_versions_by_symbols",
        new=AsyncMock(side_effect=RuntimeError("db failure")),
    ):
        with pytest.raises(
            FactorIntegrityBlockedError, match="FACTOR_VERSION_STAMP_FAILED"
        ):
            await service._stamp_verified_factor_scope(session, ["A", "B", "C"])
    session.rollback.assert_called_once()
    session.commit.assert_not_called()


async def test_stamp_verified_scope_success_commits() -> None:
    """[G1B-3A.1] stamp 全部成功（expected=3, stamped=3）→ commit，不 rollback。"""
    session = _mock_async_session()
    service = BarsSchedulerService()
    with patch(
        "app.services.factor_reconciliation.stamp_factor_reconciliation_versions_by_symbols",
        new=AsyncMock(return_value=3),
    ):
        await service._stamp_verified_factor_scope(session, ["A", "B", "C"])
    session.commit.assert_called_once()
    session.rollback.assert_not_called()

