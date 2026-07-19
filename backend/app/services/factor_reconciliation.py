"""复权因子批量一致性修复任务（CHANGE-20260718-005 Section 1）。

基于 FactorConsistencyAuditor 的审计结果，按小批次串行重建不一致股票的因子序列。

执行流程：
1. dry_run: 审计全市场（或指定股票），收集不一致股票 → ReconciliationPlan
2. rebuild_batch: 按 plan 串行重建，每只股票独立事务，记录 before/after hash + 成功/失败
3. 失败处理：re-raise，不吞没，不写 1.0 伪装成功，记录 error_code
4. 成功后：精确失效该股票 MDAS 缓存（AdjustmentFactorService.rebuild_factor_series 已处理）

安全约束：
- 全程串行（禁止并发 rebuild）
- 每只股票独立事务（失败回滚不影响其他股票）
- 失败不得写 1.0 伪装成功（_calculate_adj_factor 的兜底 1.0 仅在 xdxr 获取失败时返回，
  rebuild_factor_series 会 re-raise 而非吞没）
- dry-run 零副作用（不写库、不失效缓存）
- 不做无边界全市场重跑（只重建审计发现的不一致股票）

缓存失效范围：
- MDAS: rebuild_factor_series 已调用 _invalidate_mdas_cache(instrument_id)
- indicator/snapshot/DSA/monitor: 由调用方按受影响 trade_date 精确重算
  （本任务只负责因子重建，不触发 snapshot 重算——避免无边界全市场重跑）

用法：
    from app.services.factor_reconciliation import FactorReconciliationTask
    task = FactorReconciliationTask()
    plan = await task.dry_run(session)  # 只读审计
    report = await task.rebuild_batch(session, plan, batch_size=10)  # 串行重建
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.factor_contract import (
    FACTOR_ALGORITHM_VERSION,
    FACTOR_RECONCILIATION_VERSION,
)
from app.core.pytdx_adapter import PytdxAdapter
from app.services.adjustment_factor_service import AdjustmentFactorService
from app.services.factor_consistency_audit import (
    FactorAuditResult,
    FactorConsistencyAuditor,
)

logger = logging.getLogger("services.factor_reconciliation")


# =============================================================================
# 不可变数据类
# =============================================================================


@dataclass(frozen=True)
class ReconciliationItem:
    """单只股票的修复计划项（不可变）。

    Attributes:
        instrument_id: 标的 UUID
        symbol: 股票代码
        earliest_affected: 最早受影响日期（rebuild 起点）
        before_hash: 修复前 stored 因子序列 hash
        mismatch_count: 审计发现的 mismatch 行数
        reason: 不一致原因（factor_all_unit_with_events / value_mismatch / count_mismatch 等）
    """

    instrument_id: uuid.UUID
    symbol: str
    earliest_affected: date
    before_hash: str
    mismatch_count: int
    reason: str


@dataclass(frozen=True)
class ReconciliationPlan:
    """修复计划（不可变）。

    分类（CHANGE-20260719-001 §1.3 引入 degraded）：
    - items: 需要修复的股票（mismatch，可重建）
    - degraded: 数据缺失无法判断（如 bars_daily 缺口），不在 items 中，
      需要先回补数据再重新审计
    - error: 审计失败（如 xdxr 获取失败）
    四类互斥，total_audited = consistent + needs_rebuild + degraded + error

    Attributes:
        items: 需要修复的股票列表（mismatch，可重建）
        total_audited: 审计的总股票数
        consistent_count: 一致股票数
        error_count: 审计失败股票数
        degraded_count: 数据缺失股票数（不在 items 中，需先回补数据）
        degraded_symbols: 数据缺失股票代码列表
        algorithm_version: 审计时使用的算法版本
        reconciliation_version: 对账版本
        dry_run_at: dry-run 执行时间（UTC）
    """

    items: list[ReconciliationItem]
    total_audited: int
    consistent_count: int
    error_count: int
    degraded_count: int = 0
    degraded_symbols: list[str] = field(default_factory=list)
    algorithm_version: str = FACTOR_ALGORITHM_VERSION
    reconciliation_version: int = FACTOR_RECONCILIATION_VERSION
    dry_run_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def needs_rebuild_count(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class ReconciliationItemResult:
    """单只股票的修复结果（不可变）。

    Attributes:
        instrument_id: 标的 UUID
        symbol: 股票代码
        success: 是否成功
        before_hash: 修复前 stored 因子序列 hash
        after_hash: 修复后 stored 因子序列 hash（失败时为空串）
        records_updated: 更新的记录数（失败时为 0）
        error_code: 失败错误码（成功时为 None）
        error_message: 失败错误信息（成功时为 None）
        rebuilt_at: 修复执行时间（UTC）
    """

    instrument_id: uuid.UUID
    symbol: str
    success: bool
    before_hash: str
    after_hash: str
    records_updated: int
    error_code: str | None
    error_message: str | None
    rebuilt_at: datetime


@dataclass(frozen=True)
class ReconciliationReport:
    """批量修复报告（不可变）。

    Attributes:
        results: 每只股票的修复结果
        total_planned: 计划修复股票数
        success_count: 成功数
        failure_count: 失败数
        algorithm_version: 算法版本
        reconciliation_version: 对账版本
        started_at: 批次开始时间
        finished_at: 批次结束时间
    """

    results: list[ReconciliationItemResult]
    total_planned: int
    success_count: int
    failure_count: int
    algorithm_version: str = FACTOR_ALGORITHM_VERSION
    reconciliation_version: int = FACTOR_RECONCILIATION_VERSION
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_all_success(self) -> bool:
        return self.failure_count == 0 and self.total_planned > 0

    @property
    def success_rate(self) -> float:
        if self.total_planned == 0:
            return 1.0
        return self.success_count / self.total_planned


# =============================================================================
# 修复任务服务
# =============================================================================


class FactorReconciliationTask:
    """复权因子批量一致性修复任务。

    dry_run 只读审计 → rebuild_batch 串行重建。
    全程串行，每只股票独立事务，失败不伪装。
    """

    def __init__(
        self,
        auditor: FactorConsistencyAuditor | None = None,
        adj_service: AdjustmentFactorService | None = None,
        adapter: PytdxAdapter | None = None,
    ) -> None:
        self._auditor = auditor or FactorConsistencyAuditor()
        self._adj_service = adj_service or AdjustmentFactorService()
        self._adapter = adapter

    async def dry_run(
        self,
        session: AsyncSession,
        *,
        symbols: list[str] | None = None,
        batch_size: int = 50,
        max_mismatches: int = 20,
    ) -> ReconciliationPlan:
        """只读审计全市场（或指定股票），生成修复计划。

        零副作用：不写库、不失效缓存、不调用 rebuild。

        Args:
            session: 异步 DB 会话
            symbols: 指定股票代码列表（None=全市场 active 股票）
            batch_size: 审计分批大小
            max_mismatches: 每只股票 mismatch 明细最大条数

        Returns:
            ReconciliationPlan（含需要修复的股票列表）
        """
        items: list[ReconciliationItem] = []
        total_audited = 0
        consistent_count = 0
        error_count = 0
        degraded_count = 0
        degraded_symbols: list[str] = []

        if symbols:
            # 指定股票：逐只审计
            from sqlalchemy import select

            from app.models.instrument import Instrument

            for symbol in symbols:
                result_row = await session.execute(
                    select(Instrument.id, Instrument.symbol)
                    .where(Instrument.symbol == symbol)
                    .where(Instrument.status == "active")
                )
                row = result_row.first()
                if row is None:
                    logger.warning("dry_run 股票未找到或非 active: %s", symbol)
                    continue
                instrument_id, sym = row
                audit_result = await self._auditor.audit_single_stock(
                    session, instrument_id, sym, max_mismatches=max_mismatches,
                )
                total_audited += 1
                if audit_result.error:
                    error_count += 1
                elif audit_result.degraded_reason is not None:
                    # [CHANGE-20260719-001 §1.3] 数据缺失（如 bars_daily 缺口）
                    # 不归类为算法不一致（mismatch），不加入 items（无法重建），
                    # 需先回补数据再重新审计
                    degraded_count += 1
                    degraded_symbols.append(sym)
                elif audit_result.is_consistent:
                    consistent_count += 1
                else:
                    items.append(self._build_item(audit_result))
        else:
            # 全市场：分批审计
            async for audit_result in self._auditor.audit_active_stocks(
                session, batch_size=batch_size, max_mismatches=max_mismatches,
            ):
                total_audited += 1
                if audit_result.error:
                    error_count += 1
                elif audit_result.degraded_reason is not None:
                    # [CHANGE-20260719-001 §1.3] 数据缺失不归类为 mismatch
                    degraded_count += 1
                    degraded_symbols.append(audit_result.symbol)
                elif audit_result.is_consistent:
                    consistent_count += 1
                else:
                    items.append(self._build_item(audit_result))

        logger.info(
            "dry_run 完成: audited=%d consistent=%d needs_rebuild=%d "
            "degraded=%d errors=%d",
            total_audited, consistent_count, len(items),
            degraded_count, error_count,
        )
        return ReconciliationPlan(
            items=items,
            total_audited=total_audited,
            consistent_count=consistent_count,
            error_count=error_count,
            degraded_count=degraded_count,
            degraded_symbols=degraded_symbols,
        )

    async def rebuild_batch(
        self,
        session: AsyncSession,
        plan: ReconciliationPlan,
        *,
        batch_size: int = 10,
    ) -> ReconciliationReport:
        """按 plan 串行重建不一致股票的因子序列。

        每只股票独立事务，失败回滚不影响其他股票。
        失败不写 1.0 伪装成功（rebuild_factor_series 失败时 re-raise）。

        Args:
            session: 异步 DB 会话
            plan: dry_run 生成的修复计划
            batch_size: 每批处理的股票数（进度记录粒度）

        Returns:
            ReconciliationReport（含每只股票的修复结果）
        """
        results: list[ReconciliationItemResult] = []
        started_at = datetime.now(UTC)
        success_count = 0
        failure_count = 0

        for i, item in enumerate(plan.items):
            logger.info(
                "rebuild_batch [%d/%d] symbol=%s earliest=%s",
                i + 1, len(plan.items), item.symbol, item.earliest_affected,
            )
            result = await self._rebuild_single(session, item)
            results.append(result)
            if result.success:
                success_count += 1
            else:
                failure_count += 1

            # 每 batch_size 只记录进度日志（不写库，避免长事务）
            if (i + 1) % batch_size == 0:
                logger.info(
                    "rebuild_batch 进度 [%d/%d] success=%d failure=%d",
                    i + 1, len(plan.items), success_count, failure_count,
                )

        finished_at = datetime.now(UTC)
        logger.info(
            "rebuild_batch 完成: total=%d success=%d failure=%d",
            len(plan.items), success_count, failure_count,
        )
        return ReconciliationReport(
            results=results,
            total_planned=len(plan.items),
            success_count=success_count,
            failure_count=failure_count,
            started_at=started_at,
            finished_at=finished_at,
        )

    # =========================================================================
    # 内部方法
    # =========================================================================

    @staticmethod
    def _build_item(audit_result: FactorAuditResult) -> ReconciliationItem:
        """从审计结果构建修复计划项。"""
        # earliest_affected: 使用审计发现的 earliest_mismatch，
        # 若为 None（行数/日期不匹配），从 expected 因子序列第一个非 1.0 的日期开始
        earliest = audit_result.earliest_mismatch
        if earliest is None:
            # 行数/日期不匹配：无法精确定位，从最早日期重建（保守策略）
            # audit_result 不含完整日期序列，调用方 rebuild_factor_series 会从
            # earliest_affected 起查询所有 bars_daily，所以用 date.min 确保全量重建
            earliest = date(2000, 1, 1)

        reason = audit_result.error or "value_mismatch"
        if audit_result.factor_all_unit_with_events:
            reason = "factor_all_unit_with_events"
        elif audit_result.missing_factor_count > 0:
            reason = f"missing_factor:{audit_result.missing_factor_count}"

        return ReconciliationItem(
            instrument_id=audit_result.instrument_id,
            symbol=audit_result.symbol,
            earliest_affected=earliest,
            before_hash=audit_result.stored_factor_hash,
            mismatch_count=audit_result.mismatch_count,
            reason=reason,
        )

    async def _rebuild_single(
        self,
        session: AsyncSession,
        item: ReconciliationItem,
    ) -> ReconciliationItemResult:
        """重建单只股票的因子序列（独立事务）。

        成功：记录 after_hash + records_updated
        失败：记录 error_code + error_message，不写 1.0 伪装
        """
        rebuilt_at = datetime.now(UTC)
        try:
            # rebuild_factor_series 内部会 commit 事务并失效 MDAS 缓存
            records = await self._adj_service.rebuild_factor_series(
                session,
                item.instrument_id,
                item.symbol,
                item.earliest_affected,
                adapter=self._adapter,
            )

            # 重新审计获取 after_hash（只读，验证修复成功）
            after_audit = await self._auditor.audit_single_stock(
                session, item.instrument_id, item.symbol, max_mismatches=5,
            )
            after_hash = after_audit.stored_factor_hash

            # 验证修复后是否一致（若仍不一致，标记为 partial_success）
            if not after_audit.is_consistent and after_audit.error is None:
                logger.warning(
                    "_rebuild_single 修复后仍不一致 symbol=%s mismatch=%d",
                    item.symbol, after_audit.mismatch_count,
                )
                return ReconciliationItemResult(
                    instrument_id=item.instrument_id,
                    symbol=item.symbol,
                    success=False,
                    before_hash=item.before_hash,
                    after_hash=after_hash,
                    records_updated=records,
                    error_code="partial_success_still_inconsistent",
                    error_message=(
                        f"rebuild 后仍不一致: mismatch={after_audit.mismatch_count}"
                    ),
                    rebuilt_at=rebuilt_at,
                )

            logger.info(
                "_rebuild_single 成功 symbol=%s records=%d before=%s after=%s",
                item.symbol, records, item.before_hash, after_hash,
            )
            return ReconciliationItemResult(
                instrument_id=item.instrument_id,
                symbol=item.symbol,
                success=True,
                before_hash=item.before_hash,
                after_hash=after_hash,
                records_updated=records,
                error_code=None,
                error_message=None,
                rebuilt_at=rebuilt_at,
            )

        except Exception as exc:
            # 失败：不写 1.0 伪装成功，记录错误
            # rebuild_factor_series 失败时已 rollback，不会留下部分更新
            logger.error(
                "_rebuild_single 失败 symbol=%s: %s", item.symbol, exc,
            )
            return ReconciliationItemResult(
                instrument_id=item.instrument_id,
                symbol=item.symbol,
                success=False,
                before_hash=item.before_hash,
                after_hash="",
                records_updated=0,
                error_code=type(exc).__name__,
                error_message=str(exc),
                rebuilt_at=rebuilt_at,
            )


if __name__ == "__main__":
    # 自测：验证数据类和 _build_item 逻辑（不连 DB）
    import uuid as _uuid

    logging.basicConfig(level=logging.INFO)

    from app.services.factor_consistency_audit import FactorAuditResult

    # Case 1: _build_item 从 603538 bug 模式构建计划项
    audit_603538 = FactorAuditResult(
        instrument_id=_uuid.uuid4(), symbol="603538",
        is_consistent=False, stored_count=100, expected_count=100,
        missing_factor_count=0, mismatch_count=50,
        factor_all_unit_with_events=True,
        stored_factor_hash="abc123",
        earliest_mismatch=date(2024, 6, 15),
    )
    item = FactorReconciliationTask._build_item(audit_603538)
    assert item.symbol == "603538"
    assert item.earliest_affected == date(2024, 6, 15)
    assert item.reason == "factor_all_unit_with_events"
    assert item.before_hash == "abc123"
    assert item.mismatch_count == 50
    print("Case1 _build_item 603538 ✓")

    # Case 2: earliest_mismatch=None → 保守全量重建
    audit_count = FactorAuditResult(
        instrument_id=_uuid.uuid4(), symbol="000001",
        is_consistent=False, stored_count=100, expected_count=99,
        missing_factor_count=0, mismatch_count=100,
        stored_factor_hash="def456",
        earliest_mismatch=None,
        error="count_mismatch: stored=100 expected=99",
    )
    item2 = FactorReconciliationTask._build_item(audit_count)
    assert item2.earliest_affected == date(2000, 1, 1), (
        "earliest_mismatch=None 应保守全量重建"
    )
    assert "count_mismatch" in item2.reason
    print("Case2 _build_item count_mismatch ✓")

    # Case 3: ReconciliationReport 成功率
    report = ReconciliationReport(
        results=[], total_planned=10, success_count=8, failure_count=2,
    )
    assert report.success_rate == 0.8
    assert not report.is_all_success
    report_all = ReconciliationReport(
        results=[], total_planned=5, success_count=5, failure_count=0,
    )
    assert report_all.is_all_success
    print("Case3 ReconciliationReport ✓")

