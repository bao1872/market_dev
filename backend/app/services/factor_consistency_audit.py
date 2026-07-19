"""复权因子全市场一致性审计服务（CHANGE-20260718-005）。

只读审计：对所有 active 股票的 bars_daily.adj_factor 存量序列与基于当前 raw 日线
+ 公司行为重新计算的 expected 序列逐日比较，输出 mismatch 分类和数量。

为什么需要本服务（系统缺口）：
- `AdjustmentFactorService.detect_company_action_change` 仅检测 xdxr fingerprint 变化。
  若 fingerprint 未变但历史序列已错误（过去 bug、部分更新、1.0 伪装成功），
  系统无法发现存量错误。
- 本服务通过逐日比对 stored vs expected 发现存量错误，不受 fingerprint 限制。
- `FACTOR_ALGORITHM_VERSION` / `FACTOR_RECONCILIATION_VERSION` 变化时必须重跑本审计。

审计范围由审计结果决定（非硬编码 603538/利通电子）：
- 603538 美诺华、利通电子作为错误样本验证审计能发现 mismatch
- 600276 作为无事件对照验证审计不误报
- 修复范围 = 审计发现的所有不一致股票

零副作用约束：
- 本服务只读（SELECT），不写库、不失效缓存、不调用 rebuild
- 修复由 `FactorReconciliationTask`（独立模块）按 dry-run → 小批次串行重建执行
- 架构守护测试强制本服务不导入 rebuild/UPDATE 路径

用法：
    from app.services.factor_consistency_audit import FactorConsistencyAuditor
    auditor = FactorConsistencyAuditor()
    result = await auditor.audit_single_stock(session, instrument_id, symbol)
    async for result in auditor.audit_active_stocks(session, batch_size=50):
        ...

模块自测：
    python -m app.services.factor_consistency_audit
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.factor_contract import (
    FACTOR_ALGORITHM_VERSION,
    FACTOR_ALL_UNIT_EVENT_THRESHOLD,
    FACTOR_COMPARISON_TOLERANCE,
    FACTOR_RECONCILIATION_VERSION,
)
from app.core.pytdx_adapter import PytdxAdapter, get_pytdx_adapter
from app.models.instrument import Instrument
from app.repositories.bar_repository import (
    compute_expected_adj_factors,
    get_adj_factor_series,
)
from app.services.adjustment_factor_calculator import AdjustmentFactorDataError

logger = logging.getLogger("services.factor_consistency_audit")


# =============================================================================
# 不可变审计结果数据类
# =============================================================================


@dataclass(frozen=True)
class FactorMismatchDetail:
    """单日因子 mismatch 明细（不可变）。"""

    trade_date: date
    stored_factor: float | None  # None = stored 为 NULL
    expected_factor: float
    diff: float | None  # stored - expected；stored 为 None 时为 None


@dataclass(frozen=True)
class FactorAuditResult:
    """单股票因子一致性审计结果（不可变）。

    所有字段在审计完成时确定，禁止运行时修改。

    Attributes:
        instrument_id: 标的 UUID
        symbol: 股票代码
        is_consistent: 存量因子是否与 expected 一致（无 mismatch 且无 missing）
        stored_count: bars_daily 中该股票的日线根数
        expected_count: expected 因子序列根数（应与 stored_count 相同）
        missing_factor_count: stored adj_factor 为 NULL 的行数
        mismatch_count: stored != expected（超出容差）的行数
        mismatches: 前 N 条 mismatch 明细（供人工排查，默认 20）
        has_non_unit_expected: expected 是否含非 1.0 因子（即有除权除息事件）
        stored_all_unit: stored 是否全为 1.0（可能 1.0 伪装）
        factor_all_unit_with_events: 603538 bug 模式（stored 全 1.0 但 expected 有非 1.0）
        stored_factor_hash: stored 因子序列内容 hash
        expected_factor_hash: expected 因子序列内容 hash
        earliest_mismatch: 最早 mismatch 日期（用于 rebuild 的 earliest_affected）
        algorithm_version: 审计时使用的算法版本
        reconciliation_version: 审计逻辑版本
        error: 审计失败原因（xdxr 获取失败等）；None 表示审计成功完成
        degraded_reason: 数据缺失原因（CHANGE-20260719-001 §1.2）；
            非 None 表示 bars_daily 缺口导致无法计算 expected，
            不得归类为算法不一致（mismatch）或审计失败（error）。
            典型值："bars_daily_missing_data"
    """

    instrument_id: uuid.UUID
    symbol: str
    is_consistent: bool
    stored_count: int
    expected_count: int
    missing_factor_count: int
    mismatch_count: int
    mismatches: list[FactorMismatchDetail] = field(default_factory=list)
    has_non_unit_expected: bool = False
    stored_all_unit: bool = False
    factor_all_unit_with_events: bool = False
    stored_factor_hash: str = ""
    expected_factor_hash: str = ""
    earliest_mismatch: date | None = None
    algorithm_version: str = FACTOR_ALGORITHM_VERSION
    reconciliation_version: int = FACTOR_RECONCILIATION_VERSION
    error: str | None = None
    degraded_reason: str | None = None


# =============================================================================
# 审计服务
# =============================================================================


class FactorConsistencyAuditor:
    """全市场复权因子一致性审计服务（只读）。

    对每只 active 股票：加载 stored 因子序列 → 重算 expected → 逐日比对 → 分类 mismatch。
    零副作用：不写库、不失效缓存、不调用 rebuild。
    """

    def __init__(self, adapter: PytdxAdapter | None = None) -> None:
        self._adapter = adapter

    async def audit_single_stock(
        self,
        session: AsyncSession,
        instrument_id: uuid.UUID,
        symbol: str,
        *,
        max_mismatches: int = 20,
    ) -> FactorAuditResult:
        """审计单只股票的因子一致性（只读）。

        Args:
            session: 异步 DB 会话
            instrument_id: 标的 UUID
            symbol: 股票代码（用于 xdxr_info）
            max_mismatches: 返回的 mismatch 明细最大条数（避免大结果集）
            adapter: pytdx 适配器（None 用模块单例）

        Returns:
            FactorAuditResult（不可变）
        """
        adapter = self._adapter or get_pytdx_adapter()

        # 1. 加载 stored 因子序列
        try:
            stored_df = await get_adj_factor_series(session, instrument_id, as_of=None)
        except Exception as exc:
            logger.warning(
                "audit_single_stock 加载 stored 因子失败 instrument_id=%s: %s",
                instrument_id, exc,
            )
            return FactorAuditResult(
                instrument_id=instrument_id, symbol=symbol,
                is_consistent=False, stored_count=0, expected_count=0,
                missing_factor_count=0, mismatch_count=0,
                error=f"load_stored_failed: {exc}",
            )

        stored_count = len(stored_df)

        # 2. 重算 expected 因子序列（只读，不写库）
        # [CHANGE-20260719-001 §1.2] compute_expected_adj_factors 已删除 min_date 参数，
        # 内部调用 calculate_adjustment_factor_series 纯函数（不补齐 supplement_df）。
        # 数据缺失时抛 AdjustmentFactorDataError，auditor 捕获后标记 degraded_reason，
        # 不得归类为算法不一致（mismatch）或审计失败（error）。
        try:
            expected_df = await compute_expected_adj_factors(
                session, instrument_id, symbol, adapter=adapter,
            )
        except AdjustmentFactorDataError as exc:
            # 数据缺失（如 000688 bars_daily 缺口）：标记 degraded，不是 mismatch/error
            logger.warning(
                "audit_single_stock 数据缺失 symbol=%s: %s（标记 degraded）",
                symbol, exc,
            )
            return FactorAuditResult(
                instrument_id=instrument_id, symbol=symbol,
                is_consistent=False, stored_count=stored_count, expected_count=0,
                missing_factor_count=0, mismatch_count=0,
                degraded_reason=exc.degraded_reason,
            )
        except Exception as exc:
            logger.warning(
                "audit_single_stock 重算 expected 失败 symbol=%s: %s", symbol, exc,
            )
            return FactorAuditResult(
                instrument_id=instrument_id, symbol=symbol,
                is_consistent=False, stored_count=stored_count, expected_count=0,
                missing_factor_count=0, mismatch_count=0,
                error=f"compute_expected_failed: {exc}",
            )

        # 3. 逐日比对（_compare_factors 内部计算 stored_count/expected_count）
        return self._compare_factors(
            instrument_id, symbol, stored_df, expected_df, max_mismatches,
        )

    async def audit_active_stocks(
        self,
        session: AsyncSession,
        *,
        batch_size: int = 50,
        max_mismatches: int = 20,
    ) -> AsyncIterator[FactorAuditResult]:
        """审计所有 active 股票的因子一致性（分批只读）。

        按 batch_size 分批查询 active 股票，逐只审计，yield 结果。
        调用方可按 is_consistent 过滤，汇总 mismatch 统计。

        Args:
            session: 异步 DB 会话
            batch_size: 每批查询的股票数量（控制内存）
            max_mismatches: 每只股票返回的 mismatch 明细最大条数

        Yields:
            FactorAuditResult（每只股票一条）
        """
        offset = 0
        while True:
            result = await session.execute(
                select(Instrument.id, Instrument.symbol)
                .where(Instrument.status == "active")
                .order_by(Instrument.symbol)
                .limit(batch_size)
                .offset(offset)
            )
            rows = result.all()
            if not rows:
                break
            for instrument_id, symbol in rows:
                yield await self.audit_single_stock(
                    session, instrument_id, symbol, max_mismatches=max_mismatches,
                )
            offset += batch_size

    # =========================================================================
    # 内部：逐日比对逻辑（纯函数，可单元测试）
    # =========================================================================

    @staticmethod
    def _compare_factors(
        instrument_id: uuid.UUID,
        symbol: str,
        stored_df: pd.DataFrame,
        expected_df: pd.DataFrame,
        max_mismatches: int,
    ) -> FactorAuditResult:
        """纯函数比对 stored vs expected 因子序列。

        不连 DB，可独立单元测试。
        """
        stored_count = len(stored_df)
        expected_count = len(expected_df)

        # 计算 stored / expected 因子序列 hash（内容指纹）
        stored_hash = FactorConsistencyAuditor._hash_factor_series(stored_df)
        expected_hash = FactorConsistencyAuditor._hash_factor_series(expected_df)

        # 检测 expected 是否含非 1.0 因子（即有除权除息事件影响）
        has_non_unit_expected = False
        if expected_count > 0:
            expected_factors = expected_df["expected_adj_factor"].astype(float)
            has_non_unit_expected = bool(
                (expected_factors.sub(1.0).abs() > FACTOR_ALL_UNIT_EVENT_THRESHOLD).any()
            )

        # 检测 stored 是否全为 1.0
        stored_all_unit = False
        missing_factor_count = 0
        if stored_count > 0:
            stored_factors = stored_df["adj_factor"]
            # NULL/NaN 计入 missing
            missing_mask = stored_factors.isna()
            missing_factor_count = int(missing_mask.sum())
            non_null = stored_factors.dropna()
            if len(non_null) > 0:
                stored_all_unit = bool(
                    (non_null.astype(float).sub(1.0).abs()
                     <= FACTOR_ALL_UNIT_EVENT_THRESHOLD).all()
                )
            else:
                stored_all_unit = False  # 全 NULL 不算 all_unit

        # 603538 bug 模式：stored 全 1.0 但 expected 有非 1.0
        factor_all_unit_with_events = stored_all_unit and has_non_unit_expected

        # 行数不匹配：无法逐日比对，直接标记不一致
        if stored_count != expected_count:
            return FactorAuditResult(
                instrument_id=instrument_id, symbol=symbol,
                is_consistent=False,
                stored_count=stored_count, expected_count=expected_count,
                missing_factor_count=missing_factor_count,
                mismatch_count=max(stored_count, expected_count),
                has_non_unit_expected=has_non_unit_expected,
                stored_all_unit=stored_all_unit,
                factor_all_unit_with_events=factor_all_unit_with_events,
                stored_factor_hash=stored_hash,
                expected_factor_hash=expected_hash,
                earliest_mismatch=None,
                error=f"count_mismatch: stored={stored_count} expected={expected_count}",
            )

        if stored_count == 0:
            # 双方都无数据，视为一致（无事件对照或新上市）
            return FactorAuditResult(
                instrument_id=instrument_id, symbol=symbol,
                is_consistent=True,
                stored_count=0, expected_count=0,
                missing_factor_count=0, mismatch_count=0,
                has_non_unit_expected=False,
                stored_all_unit=False,
                factor_all_unit_with_events=False,
                stored_factor_hash=stored_hash,
                expected_factor_hash=expected_hash,
            )

        # 逐日比对（stored 和 expected 行数相同，按 trade_date 对齐）
        stored_sorted = stored_df.sort_values("trade_date").reset_index(drop=True)
        expected_sorted = expected_df.sort_values("trade_date").reset_index(drop=True)

        # [Bugfix CHANGE-20260718-007] - 归一化 trade_date 类型后再比较。
        # stored 的 trade_date 来自 DB，可能是 datetime64[s]（pandas.Timestamp）；
        # expected 的 trade_date 来自 compute_expected_adj_factors，可能是 object
        #（datetime.date）。类型不同时 == 比较恒为 False，导致全部股票误报
        # date_sequence_mismatch（审计从未真正工作过）。统一转 date 对象后再比较。
        stored_dates = pd.to_datetime(stored_sorted["trade_date"]).dt.date.tolist()
        expected_dates = pd.to_datetime(expected_sorted["trade_date"]).dt.date.tolist()
        dates_match = stored_dates == expected_dates

        mismatches: list[FactorMismatchDetail] = []
        mismatch_count = 0
        earliest_mismatch: date | None = None

        if not dates_match:
            # 日期序列不同，无法逐日比对
            return FactorAuditResult(
                instrument_id=instrument_id, symbol=symbol,
                is_consistent=False,
                stored_count=stored_count, expected_count=expected_count,
                missing_factor_count=missing_factor_count,
                mismatch_count=stored_count,
                has_non_unit_expected=has_non_unit_expected,
                stored_all_unit=stored_all_unit,
                factor_all_unit_with_events=factor_all_unit_with_events,
                stored_factor_hash=stored_hash,
                expected_factor_hash=expected_hash,
                error="date_sequence_mismatch",
            )

        # 逐日比对因子值（stored_dates 已归一化为 date 对象）
        for i in range(stored_count):
            td_date = stored_dates[i]
            stored_val = stored_sorted["adj_factor"].iloc[i]
            expected_val = float(expected_sorted["expected_adj_factor"].iloc[i])

            # stored 为 NULL/NaN
            if pd.isna(stored_val):
                mismatch_count += 1
                if earliest_mismatch is None:
                    earliest_mismatch = td_date
                if len(mismatches) < max_mismatches:
                    mismatches.append(FactorMismatchDetail(
                        trade_date=td_date,
                        stored_factor=None,
                        expected_factor=expected_val,
                        diff=None,
                    ))
                continue

            stored_f = float(stored_val)
            diff = abs(stored_f - expected_val)
            if diff > FACTOR_COMPARISON_TOLERANCE:
                mismatch_count += 1
                if earliest_mismatch is None:
                    earliest_mismatch = td_date
                if len(mismatches) < max_mismatches:
                    mismatches.append(FactorMismatchDetail(
                        trade_date=td_date,
                        stored_factor=stored_f,
                        expected_factor=expected_val,
                        diff=stored_f - expected_val,
                    ))

        is_consistent = (
            mismatch_count == 0
            and missing_factor_count == 0
            and not factor_all_unit_with_events
        )

        return FactorAuditResult(
            instrument_id=instrument_id, symbol=symbol,
            is_consistent=is_consistent,
            stored_count=stored_count, expected_count=expected_count,
            missing_factor_count=missing_factor_count,
            mismatch_count=mismatch_count,
            mismatches=mismatches,
            has_non_unit_expected=has_non_unit_expected,
            stored_all_unit=stored_all_unit,
            factor_all_unit_with_events=factor_all_unit_with_events,
            stored_factor_hash=stored_hash,
            expected_factor_hash=expected_hash,
            earliest_mismatch=earliest_mismatch,
        )

    @staticmethod
    def _hash_factor_series(df: pd.DataFrame, factor_col: str | None = None) -> str:
        """计算因子序列内容 hash（用于 before/after 比对和缓存键）。

        Args:
            df: 含 trade_date + 因子列的 DataFrame
            factor_col: 因子列名（stored='adj_factor', expected='expected_adj_factor'）；
                None 时自动检测

        Returns:
            16 字符 hex hash（SHA256 前 16 字符）；空 DataFrame 返回 'empty'
        """
        if df is None or df.empty:
            return "empty"
        col = factor_col or (
            "expected_adj_factor" if "expected_adj_factor" in df.columns else "adj_factor"
        )
        if col not in df.columns:
            return "no_factor_col"
        try:
            sorted_df = df.sort_values("trade_date")
            parts = []
            for _, row in sorted_df.iterrows():
                td = row["trade_date"]
                td_str = td.isoformat() if hasattr(td, "isoformat") else str(td)
                val = row[col]
                val_str = "null" if pd.isna(val) else f"{float(val):.10f}"
                parts.append(f"{td_str}|{val_str}")
            content = "\n".join(parts)
            return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        except Exception as exc:
            logger.warning("因子序列 hash 计算失败: %s", exc)
            return "hash_error"


# =============================================================================
# 审计结果汇总
# =============================================================================


@dataclass(frozen=True)
class FactorAuditSummary:
    """全市场因子审计汇总（不可变）。

    分类（CHANGE-20260719-001 §1.2 引入 degraded）：
    - consistent: 因子一致（is_consistent=True）
    - inconsistent: 因子不一致/mismatch（is_consistent=False, error=None, degraded_reason=None）
    - degraded: 数据缺失无法判断（degraded_reason != None，如 bars_daily 缺口）
    - error: 审计失败（error != None，如 xdxr 获取失败）
    四类互斥，total_audited = consistent + inconsistent + degraded + error
    """

    total_audited: int
    consistent_count: int
    inconsistent_count: int
    error_count: int
    factor_all_unit_with_events_count: int
    total_mismatches: int
    algorithm_version: str
    reconciliation_version: int
    degraded_count: int = 0
    inconsistent_symbols: list[str] = field(default_factory=list)
    error_symbols: list[str] = field(default_factory=list)
    degraded_symbols: list[str] = field(default_factory=list)

    @property
    def consistency_rate(self) -> float:
        """一致率（0.0-1.0，分母排除 degraded/error）。"""
        # degraded 和 error 不计入分母（无法判断一致性）
        denominator = self.consistent_count + self.inconsistent_count
        if denominator == 0:
            return 0.0
        return self.consistent_count / denominator


async def summarize_audit_results(
    results: list[FactorAuditResult],
) -> FactorAuditSummary:
    """汇总审计结果列表为 FactorAuditSummary。"""
    consistent = [r for r in results if r.is_consistent]
    # inconsistent 排除 degraded（degraded_reason != None）和 error（error != None）
    inconsistent = [
        r for r in results
        if not r.is_consistent and r.error is None and r.degraded_reason is None
    ]
    errors = [r for r in results if r.error is not None]
    degraded = [r for r in results if r.degraded_reason is not None]
    all_unit_events = [r for r in results if r.factor_all_unit_with_events]
    total_mismatches = sum(r.mismatch_count for r in results)

    return FactorAuditSummary(
        total_audited=len(results),
        consistent_count=len(consistent),
        inconsistent_count=len(inconsistent),
        error_count=len(errors),
        factor_all_unit_with_events_count=len(all_unit_events),
        total_mismatches=total_mismatches,
        algorithm_version=FACTOR_ALGORITHM_VERSION,
        reconciliation_version=FACTOR_RECONCILIATION_VERSION,
        degraded_count=len(degraded),
        inconsistent_symbols=[r.symbol for r in inconsistent],
        error_symbols=[r.symbol for r in errors],
        degraded_symbols=[r.symbol for r in degraded],
    )


if __name__ == "__main__":
    # 自测：验证 _compare_factors 纯函数逻辑（不连 DB）
    import uuid as _uuid

    logging.basicConfig(level=logging.INFO)
    test_iid = _uuid.uuid4()

    # Case 1: 一致（stored == expected，无事件，全 1.0）
    dates1 = pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"])
    stored1 = pd.DataFrame({
        "trade_date": dates1, "adj_factor": [1.0, 1.0, 1.0],
    })
    expected1 = pd.DataFrame({
        "trade_date": dates1, "expected_adj_factor": [1.0, 1.0, 1.0],
    })
    r1 = FactorConsistencyAuditor._compare_factors(
        test_iid, "600276", stored1, expected1, max_mismatches=20,
    )
    assert r1.is_consistent, f"Case1 应一致: {r1}"
    assert r1.stored_count == 3
    assert r1.mismatch_count == 0
    print("Case1 无事件一致 ✓")

    # Case 2: 603538 bug 模式（stored 全 1.0，expected 有非 1.0）
    stored2 = pd.DataFrame({
        "trade_date": dates1, "adj_factor": [1.0, 1.0, 1.0],
    })
    expected2 = pd.DataFrame({
        "trade_date": dates1, "expected_adj_factor": [0.5, 0.5, 1.0],
    })
    r2 = FactorConsistencyAuditor._compare_factors(
        test_iid, "603538", stored2, expected2, max_mismatches=20,
    )
    assert not r2.is_consistent, "Case2 应不一致"
    assert r2.factor_all_unit_with_events, "Case2 应检测到 all_unit_with_events"
    assert r2.mismatch_count == 2, f"Case2 应有 2 个 mismatch，实际 {r2.mismatch_count}"
    assert r2.earliest_mismatch == date(2026, 6, 16)
    print("Case2 603538 bug 模式检测 ✓")

    # Case 3: 因子值 mismatch（stored 有非 1.0 但与 expected 不符）
    stored3 = pd.DataFrame({
        "trade_date": dates1, "adj_factor": [0.48, 0.5, 1.0],
    })
    expected3 = pd.DataFrame({
        "trade_date": dates1, "expected_adj_factor": [0.5, 0.5, 1.0],
    })
    r3 = FactorConsistencyAuditor._compare_factors(
        test_iid, "000001", stored3, expected3, max_mismatches=20,
    )
    assert not r3.is_consistent, "Case3 应不一致"
    assert r3.mismatch_count == 1, f"Case3 应有 1 个 mismatch，实际 {r3.mismatch_count}"
    assert r3.mismatches[0].trade_date == date(2026, 6, 16)
    print("Case3 因子值 mismatch ✓")

    # Case 4: stored 含 NULL
    stored4 = pd.DataFrame({
        "trade_date": dates1, "adj_factor": [0.5, None, 1.0],
    })
    expected4 = pd.DataFrame({
        "trade_date": dates1, "expected_adj_factor": [0.5, 0.5, 1.0],
    })
    r4 = FactorConsistencyAuditor._compare_factors(
        test_iid, "000002", stored4, expected4, max_mismatches=20,
    )
    assert not r4.is_consistent, "Case4 应不一致"
    assert r4.missing_factor_count == 1
    assert r4.mismatch_count == 1
    print("Case4 stored NULL ✓")

    # Case 5: 行数不匹配
    stored5 = pd.DataFrame({
        "trade_date": dates1[:2], "adj_factor": [1.0, 1.0],
    })
    expected5 = pd.DataFrame({
        "trade_date": dates1, "expected_adj_factor": [1.0, 1.0, 1.0],
    })
    r5 = FactorConsistencyAuditor._compare_factors(
        test_iid, "000003", stored5, expected5, max_mismatches=20,
    )
    assert not r5.is_consistent, "Case5 应不一致"
    assert r5.error and "count_mismatch" in r5.error
    print("Case5 行数不匹配 ✓")

    # Case 6: hash 确定性
    # 注意：stored1 和 stored2 的因子值均为 [1.0, 1.0, 1.0]，hash 应相同；
    # stored3 = [0.48, 0.5, 1.0] 与 stored1 不同，hash 应不同。
    h1 = FactorConsistencyAuditor._hash_factor_series(stored1)
    h2 = FactorConsistencyAuditor._hash_factor_series(stored1)
    assert h1 == h2, "相同输入 hash 应一致"
    assert h1 == FactorConsistencyAuditor._hash_factor_series(stored2), (
        "stored1/stored2 因子值相同，hash 应一致"
    )
    h3 = FactorConsistencyAuditor._hash_factor_series(stored3)
    assert h1 != h3, "不同因子序列 hash 应不同"
    print("Case6 hash 确定性 ✓")

    # Case 7: [Bugfix CHANGE-20260718-007] 日期类型不一致（stored=Timestamp, expected=date）
    # 生产场景：stored 来自 DB（datetime64[s] → Timestamp），expected 来自
    # compute_expected_adj_factors（object → datetime.date）。日期相同但类型不同，
    # 修复前 == 比较恒 False，误报 date_sequence_mismatch。修复后应正确识别为一致。
    dates7_stored = pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"])  # Timestamp
    dates7_expected = pd.Series([date(2026, 6, 16), date(2026, 6, 17), date(2026, 6, 18)])  # date
    stored7 = pd.DataFrame({
        "trade_date": dates7_stored, "adj_factor": [1.0, 1.0, 1.0],
    })
    expected7 = pd.DataFrame({
        "trade_date": dates7_expected, "expected_adj_factor": [1.0, 1.0, 1.0],
    })
    r7 = FactorConsistencyAuditor._compare_factors(
        test_iid, "CASE7_TYPE_MISMATCH", stored7, expected7, max_mismatches=20,
    )
    assert r7.is_consistent, (
        f"Case7 日期类型不一致但值相同应一致: error={r7.error}, mismatch={r7.mismatch_count}"
    )
    assert r7.error is None, f"Case7 不应有 error: {r7.error}"
    print("Case7 日期类型不一致归一化 ✓")

    print("OK")
