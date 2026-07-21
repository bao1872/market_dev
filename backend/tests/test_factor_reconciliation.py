"""复权因子批量一致性修复任务测试（CHANGE-20260718-005 Section 1）。

测试覆盖：
1. `_build_item` 从审计结果构建修复计划项（各种 mismatch 模式）
2. `ReconciliationPlan` / `ReconciliationReport` 数据类属性
3. 架构约束：dry_run 只读，不调用 rebuild/commit
4. `rebuild_batch` mock 测试：成功 / 失败 / partial_success 场景

约束：
- 纯单元测试（不连 DB/网络）
- rebuild_batch 使用 mock AdjustmentFactorService / FactorConsistencyAuditor
- 验证失败不写 1.0 伪装成功（error_code 非空）
"""
from __future__ import annotations

import ast
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.factor_consistency_audit import FactorAuditResult
from app.services.factor_reconciliation import (
    FactorReconciliationTask,
    ReconciliationItem,
    ReconciliationItemResult,
    ReconciliationPlan,
    ReconciliationReport,
)

_RECONCILIATION_FILE = (
    Path(__file__).parent.parent / "app" / "services" / "factor_reconciliation.py"
)


# =============================================================================
# 1. _build_item 测试
# =============================================================================


class TestBuildItem:
    """从审计结果构建修复计划项。"""

    def test_603538_bug_pattern(self):
        """603538 bug 模式：factor_all_unit_with_events。"""
        audit = FactorAuditResult(
            instrument_id=uuid.uuid4(), symbol="603538",
            is_consistent=False, stored_count=100, expected_count=100,
            missing_factor_count=0, mismatch_count=50,
            factor_all_unit_with_events=True,
            stored_factor_hash="abc123",
            earliest_mismatch=date(2024, 6, 15),
        )
        item = FactorReconciliationTask._build_item(audit)
        assert item.symbol == "603538"
        assert item.earliest_affected == date(2024, 6, 15)
        assert item.reason == "factor_all_unit_with_events"
        assert item.before_hash == "abc123"
        assert item.mismatch_count == 50

    def test_count_mismatch_no_earliest(self):
        """行数不匹配（earliest_mismatch=None）→ 保守全量重建。"""
        audit = FactorAuditResult(
            instrument_id=uuid.uuid4(), symbol="000001",
            is_consistent=False, stored_count=100, expected_count=99,
            missing_factor_count=0, mismatch_count=100,
            stored_factor_hash="def456",
            earliest_mismatch=None,
            error="count_mismatch: stored=100 expected=99",
        )
        item = FactorReconciliationTask._build_item(audit)
        assert item.earliest_affected == date(2000, 1, 1), (
            "earliest_mismatch=None 应保守全量重建（date(2000,1,1)）"
        )
        assert "count_mismatch" in item.reason

    def test_value_mismatch(self):
        """因子值 mismatch（有 earliest_mismatch）。"""
        audit = FactorAuditResult(
            instrument_id=uuid.uuid4(), symbol="000002",
            is_consistent=False, stored_count=100, expected_count=100,
            missing_factor_count=0, mismatch_count=3,
            stored_factor_hash="ghi789",
            earliest_mismatch=date(2025, 1, 10),
        )
        item = FactorReconciliationTask._build_item(audit)
        assert item.earliest_affected == date(2025, 1, 10)
        assert item.reason == "value_mismatch"

    def test_missing_factor(self):
        """NULL 因子 → reason 含 missing_factor。"""
        audit = FactorAuditResult(
            instrument_id=uuid.uuid4(), symbol="000003",
            is_consistent=False, stored_count=100, expected_count=100,
            missing_factor_count=5, mismatch_count=5,
            stored_factor_hash="jkl012",
            earliest_mismatch=date(2025, 3, 1),
        )
        item = FactorReconciliationTask._build_item(audit)
        assert "missing_factor:5" in item.reason


# =============================================================================
# 2. 数据类属性测试
# =============================================================================


class TestReconciliationDataClasses:
    """ReconciliationPlan / ReconciliationReport 数据类。"""

    def test_plan_needs_rebuild_count(self):
        """plan.needs_rebuild_count = items 长度。"""
        items = [
            ReconciliationItem(
                instrument_id=uuid.uuid4(), symbol="603538",
                earliest_affected=date(2024, 1, 1),
                before_hash="abc", mismatch_count=50,
                reason="factor_all_unit_with_events",
            ),
            ReconciliationItem(
                instrument_id=uuid.uuid4(), symbol="000001",
                earliest_affected=date(2025, 1, 1),
                before_hash="def", mismatch_count=3,
                reason="value_mismatch",
            ),
        ]
        plan = ReconciliationPlan(
            items=items, total_audited=100,
            consistent_count=97, error_count=1,
        )
        assert plan.needs_rebuild_count == 2
        assert plan.total_audited == 100
        assert plan.consistent_count == 97

    def test_report_success_rate(self):
        """report.success_rate = success_count / total_planned。"""
        report = ReconciliationReport(
            results=[], total_planned=10,
            success_count=8, failure_count=2,
        )
        assert report.success_rate == pytest.approx(0.8)
        assert not report.is_all_success

    def test_report_all_success(self):
        """全部成功 → is_all_success=True。"""
        report = ReconciliationReport(
            results=[], total_planned=5,
            success_count=5, failure_count=0,
        )
        assert report.is_all_success
        assert report.success_rate == 1.0

    def test_report_empty_plan(self):
        """空计划 → success_rate=1.0, is_all_success=False。"""
        report = ReconciliationReport(
            results=[], total_planned=0,
            success_count=0, failure_count=0,
        )
        assert report.success_rate == 1.0
        assert not report.is_all_success  # total_planned=0 不算 all_success

    def test_item_result_frozen(self):
        """ReconciliationItemResult 不可变。"""
        result = ReconciliationItemResult(
            instrument_id=uuid.uuid4(), symbol="603538",
            success=True, before_hash="abc", after_hash="def",
            records_updated=100, error_code=None, error_message=None,
            rebuilt_at=datetime.now(UTC),
        )
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore[misc]


# =============================================================================
# 3. 架构约束测试
# =============================================================================


class TestReconciliationArchitecture:
    """dry_run 只读约束。"""

    def test_dry_run_no_rebuild_call_in_source(self):
        """dry_run 方法源码不得直接调用 rebuild_factor_series。"""
        source = _RECONCILIATION_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "dry_run":
                    func_source = ast.get_source_segment(source, node)
                    assert func_source is not None
                    # dry_run 不得调用 rebuild（只读审计）
                    assert "rebuild_factor_series" not in func_source, (
                        "dry_run 禁止调用 rebuild_factor_series（只读约束）"
                    )
                    assert "rebuild_adj_factors" not in func_source, (
                        "dry_run 禁止调用 rebuild_adj_factors（只读约束）"
                    )

    def test_rebuild_batch_calls_rebuild(self):
        """rebuild_batch 必须通过 _rebuild_single → rebuild_factor_series 重建。"""
        source = _RECONCILIATION_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        found_rebuild_single = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "_rebuild_single":
                    found_rebuild_single = True
                    func_source = ast.get_source_segment(source, node)
                    assert func_source is not None
                    assert "rebuild_factor_series" in func_source, (
                        "_rebuild_single 必须调用 rebuild_factor_series"
                    )
        assert found_rebuild_single, "_rebuild_single 方法必须存在"


# =============================================================================
# 4. rebuild_batch mock 测试
# =============================================================================


class TestRebuildBatchMock:
    """rebuild_batch 使用 mock 测试成功/失败/partial_success 场景。"""

    def _make_plan(self, symbols: list[str]) -> ReconciliationPlan:
        """构造测试用 plan。"""
        items = [
            ReconciliationItem(
                instrument_id=uuid.uuid4(), symbol=s,
                earliest_affected=date(2024, 1, 1),
                before_hash=f"before_{s}", mismatch_count=10,
                reason="factor_all_unit_with_events",
            )
            for s in symbols
        ]
        return ReconciliationPlan(
            items=items, total_audited=len(symbols) * 10,
            consistent_count=len(symbols) * 9, error_count=0,
        )

    @pytest.mark.asyncio
    async def test_rebuild_batch_all_success(self):
        """全部成功场景。"""
        plan = self._make_plan(["603538", "000001", "000002"])
        task = FactorReconciliationTask()

        # mock _rebuild_single 返回成功
        async def mock_rebuild(session, item):
            return ReconciliationItemResult(
                instrument_id=item.instrument_id, symbol=item.symbol,
                success=True, before_hash=item.before_hash,
                after_hash=f"after_{item.symbol}", records_updated=100,
                error_code=None, error_message=None,
                rebuilt_at=datetime.now(UTC),
            )

        task._rebuild_single = mock_rebuild  # type: ignore[assignment]
        report = await task.rebuild_batch(MagicMock(), plan, batch_size=2)

        assert report.total_planned == 3
        assert report.success_count == 3
        assert report.failure_count == 0
        assert report.is_all_success
        assert len(report.results) == 3
        assert all(r.after_hash.startswith("after_") for r in report.results)

    @pytest.mark.asyncio
    async def test_rebuild_batch_with_failure(self):
        """部分失败场景：失败不写 1.0 伪装成功。"""
        plan = self._make_plan(["603538", "000001", "000002"])
        task = FactorReconciliationTask()

        call_count = 0

        async def mock_rebuild(session, item):
            nonlocal call_count
            call_count += 1
            if item.symbol == "000001":
                # 模拟失败：xdxr 获取超时
                return ReconciliationItemResult(
                    instrument_id=item.instrument_id, symbol=item.symbol,
                    success=False, before_hash=item.before_hash,
                    after_hash="", records_updated=0,
                    error_code="TimeoutError",
                    error_message="xdxr_info timeout",
                    rebuilt_at=datetime.now(UTC),
                )
            return ReconciliationItemResult(
                instrument_id=item.instrument_id, symbol=item.symbol,
                success=True, before_hash=item.before_hash,
                after_hash=f"after_{item.symbol}", records_updated=100,
                error_code=None, error_message=None,
                rebuilt_at=datetime.now(UTC),
            )

        task._rebuild_single = mock_rebuild  # type: ignore[assignment]
        report = await task.rebuild_batch(MagicMock(), plan, batch_size=10)

        assert report.success_count == 2
        assert report.failure_count == 1
        assert not report.is_all_success

        # 失败项验证：不写 1.0 伪装成功
        failed = [r for r in report.results if not r.success][0]
        assert failed.symbol == "000001"
        assert failed.after_hash == ""  # 失败时 after_hash 为空
        assert failed.records_updated == 0
        assert failed.error_code == "TimeoutError"
        assert "xdxr" in failed.error_message  # type: ignore[operator]
        assert call_count == 3  # 全部 3 只都尝试了（失败不影响后续）

    @pytest.mark.asyncio
    async def test_rebuild_batch_partial_success(self):
        """partial_success：rebuild 后仍不一致。"""
        plan = self._make_plan(["603538"])
        task = FactorReconciliationTask()

        async def mock_rebuild(session, item):
            return ReconciliationItemResult(
                instrument_id=item.instrument_id, symbol=item.symbol,
                success=False, before_hash=item.before_hash,
                after_hash="after_partial", records_updated=100,
                error_code="partial_success_still_inconsistent",
                error_message="rebuild 后仍不一致: mismatch=5",
                rebuilt_at=datetime.now(UTC),
            )

        task._rebuild_single = mock_rebuild  # type: ignore[assignment]
        report = await task.rebuild_batch(MagicMock(), plan, batch_size=10)

        assert report.failure_count == 1
        assert report.success_count == 0
        result = report.results[0]
        assert result.error_code == "partial_success_still_inconsistent"
        assert result.records_updated == 100  # 确实更新了，但仍不一致

    @pytest.mark.asyncio
    async def test_rebuild_batch_empty_plan(self):
        """空计划 → success_rate=1.0, is_all_success=False。"""
        plan = ReconciliationPlan(
            items=[], total_audited=100,
            consistent_count=100, error_count=0,
        )
        task = FactorReconciliationTask()
        report = await task.rebuild_batch(MagicMock(), plan, batch_size=10)

        assert report.total_planned == 0
        assert report.success_count == 0
        assert report.failure_count == 0
        assert report.success_rate == 1.0
        assert not report.is_all_success

    @pytest.mark.asyncio
    async def test_rebuild_batch_serial_order(self):
        """rebuild_batch 必须串行（按 plan.items 顺序）。"""
        symbols = ["A", "B", "C", "D", "E"]
        plan = self._make_plan(symbols)
        task = FactorReconciliationTask()

        executed_order: list[str] = []

        async def mock_rebuild(session, item):
            executed_order.append(item.symbol)
            return ReconciliationItemResult(
                instrument_id=item.instrument_id, symbol=item.symbol,
                success=True, before_hash=item.before_hash,
                after_hash="ok", records_updated=1,
                error_code=None, error_message=None,
                rebuilt_at=datetime.now(UTC),
            )

        task._rebuild_single = mock_rebuild  # type: ignore[assignment]
        await task.rebuild_batch(MagicMock(), plan, batch_size=2)

        assert executed_order == symbols, "必须按 plan.items 顺序串行执行"


# =============================================================================
# 5. _invalidate_downstream_caches 测试（FR-11 因子变化后精确失效下游缓存）
# =============================================================================


class TestInvalidateDownstreamCaches:
    """AdjustmentFactorService._invalidate_downstream_caches 单元测试。

    FR-11: 因子变化后精确失效 MDAS / bars / indicator 三层 Redis 缓存。
    单层失败不阻塞其他层（缓存 TTL 会自然过期）。
    """

    @pytest.mark.asyncio
    async def test_invalidates_all_three_cache_layers(self):
        """成功场景：三层缓存均被精确失效，返回各层删除键数。"""
        from app.services.adjustment_factor_service import AdjustmentFactorService

        service = AdjustmentFactorService()
        instrument_id = uuid.uuid4()

        # mock 三层失效函数
        service._invalidate_mdas_cache = MagicMock(return_value=5)  # type: ignore[assignment]

        bars_deleted = []
        indicator_deleted = []

        async def mock_bars_invalidate(inst_id):
            bars_deleted.append(inst_id)
            return 3

        async def mock_indicator_invalidate(inst_id):
            indicator_deleted.append(inst_id)
            return 2

        import app.services.bars_cache as bars_cache_mod
        import app.services.indicator_cache as indicator_cache_mod

        original_bars = bars_cache_mod.invalidate_bars_cache
        original_indicator = indicator_cache_mod.invalidate
        bars_cache_mod.invalidate_bars_cache = mock_bars_invalidate  # type: ignore[assignment]
        indicator_cache_mod.invalidate = mock_indicator_invalidate  # type: ignore[assignment]
        try:
            result = await service._invalidate_downstream_caches(instrument_id)
        finally:
            bars_cache_mod.invalidate_bars_cache = original_bars  # type: ignore[assignment]
            indicator_cache_mod.invalidate = original_indicator  # type: ignore[assignment]

        assert result == {"mdas": 5, "bars": 3, "indicator": 2, "capture": 0}
        service._invalidate_mdas_cache.assert_called_once_with(instrument_id)
        assert bars_deleted == [instrument_id]
        assert indicator_deleted == [instrument_id]

    @pytest.mark.asyncio
    async def test_bars_cache_failure_does_not_block_indicator(self):
        """bars 缓存失效异常不阻塞 indicator 缓存失效。"""
        from app.services.adjustment_factor_service import AdjustmentFactorService

        service = AdjustmentFactorService()
        instrument_id = uuid.uuid4()

        service._invalidate_mdas_cache = MagicMock(return_value=1)  # type: ignore[assignment]

        async def failing_bars_invalidate(inst_id):
            raise RuntimeError("redis connection refused")

        indicator_called = []

        async def mock_indicator_invalidate(inst_id):
            indicator_called.append(inst_id)
            return 4

        import app.services.bars_cache as bars_cache_mod
        import app.services.indicator_cache as indicator_cache_mod

        original_bars = bars_cache_mod.invalidate_bars_cache
        original_indicator = indicator_cache_mod.invalidate
        bars_cache_mod.invalidate_bars_cache = failing_bars_invalidate  # type: ignore[assignment]
        indicator_cache_mod.invalidate = mock_indicator_invalidate  # type: ignore[assignment]
        try:
            result = await service._invalidate_downstream_caches(instrument_id)
        finally:
            bars_cache_mod.invalidate_bars_cache = original_bars  # type: ignore[assignment]
            indicator_cache_mod.invalidate = original_indicator  # type: ignore[assignment]

        # MDAS 成功、bars 异常返回 0、indicator 成功
        assert result["mdas"] == 1
        assert result["bars"] == 0
        assert result["indicator"] == 4
        assert indicator_called == [instrument_id]

    @pytest.mark.asyncio
    async def test_indicator_cache_failure_does_not_block_bars(self):
        """indicator 缓存失效异常不阻塞 bars 缓存失效。"""
        from app.services.adjustment_factor_service import AdjustmentFactorService

        service = AdjustmentFactorService()
        instrument_id = uuid.uuid4()

        service._invalidate_mdas_cache = MagicMock(return_value=2)  # type: ignore[assignment]

        bars_called = []

        async def mock_bars_invalidate(inst_id):
            bars_called.append(inst_id)
            return 7

        async def failing_indicator_invalidate(inst_id):
            raise ConnectionError("redis timeout")

        import app.services.bars_cache as bars_cache_mod
        import app.services.indicator_cache as indicator_cache_mod

        original_bars = bars_cache_mod.invalidate_bars_cache
        original_indicator = indicator_cache_mod.invalidate
        bars_cache_mod.invalidate_bars_cache = mock_bars_invalidate  # type: ignore[assignment]
        indicator_cache_mod.invalidate = failing_indicator_invalidate  # type: ignore[assignment]
        try:
            result = await service._invalidate_downstream_caches(instrument_id)
        finally:
            bars_cache_mod.invalidate_bars_cache = original_bars  # type: ignore[assignment]
            indicator_cache_mod.invalidate = original_indicator  # type: ignore[assignment]

        # MDAS 成功、bars 成功、indicator 异常返回 0
        assert result["mdas"] == 2
        assert result["bars"] == 7
        assert result["indicator"] == 0
        assert bars_called == [instrument_id]

    @pytest.mark.asyncio
    async def test_rebuild_factor_series_calls_invalidate_downstream(self):
        """rebuild_factor_series 成功后必须调用 _invalidate_downstream_caches（FR-11）。"""
        from app.services import adjustment_factor_service as afs_mod
        from app.services.adjustment_factor_service import AdjustmentFactorService

        service = AdjustmentFactorService()
        instrument_id = uuid.uuid4()

        # mock rebuild_adj_factors 返回固定 records 数
        async def mock_rebuild(session, inst_id, symbol, earliest, adapter=None):
            return 42

        # mock _invalidate_downstream_caches 验证被调用
        invalidate_called = []

        async def mock_invalidate(inst_id):
            invalidate_called.append(inst_id)
            return {"mdas": 1, "bars": 0, "indicator": 2}

        original_rebuild = afs_mod.rebuild_adj_factors
        service._invalidate_downstream_caches = mock_invalidate  # type: ignore[assignment]
        afs_mod.rebuild_adj_factors = mock_rebuild  # type: ignore[assignment]
        try:
            count = await service.rebuild_factor_series(
                MagicMock(), instrument_id, "600519", date(2024, 1, 1),
            )
        finally:
            afs_mod.rebuild_adj_factors = original_rebuild  # type: ignore[assignment]

        assert count == 42
        assert invalidate_called == [instrument_id]
