"""复权因子一致性审计服务测试（CHANGE-20260718-005）。

测试覆盖：
1. `_compare_factors` 纯函数：各种 mismatch 场景分类
2. `_hash_factor_series` 确定性
3. `summarize_audit_results` 汇总
4. 架构约束：审计服务只读，不导入 rebuild/UPDATE 路径
5. 因子合同常量版本正确

约束：
- 纯单元测试（不连 DB/网络）
- 使用合成 DataFrame 模拟 stored / expected 因子序列
- 603538 美诺华、利通电子作为错误样本，600276 作为无事件对照
"""
from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pandas as pd
import pytest

from app.constants.factor_contract import (
    FACTOR_ALGORITHM_VERSION,
    FACTOR_COMPARISON_TOLERANCE,
    FACTOR_RECONCILIATION_VERSION,
)
from app.services.factor_consistency_audit import (
    FactorAuditResult,
    FactorConsistencyAuditor,
    summarize_audit_results,
)

_AUDIT_FILE = Path(__file__).parent.parent / "app" / "services" / "factor_consistency_audit.py"
_REPO_FILE = Path(__file__).parent.parent / "app" / "repositories" / "bar_repository.py"


# =============================================================================
# 合成数据 helpers
# =============================================================================


def _dates(n: int = 5, start: str = "2026-06-16") -> pd.DatetimeIndex:
    """生成 n 个连续交易日（从 start 起，freq='B' 业务日）。

    使用 start 而非 end，避免 end 落在非业务日时 date_range 返回少于 periods 的日期。
    2026-06-16 是周二，确保 periods 数量准确。
    """
    return pd.to_datetime(pd.date_range(start=start, periods=n, freq="B"))


def _stored_df(factors: list[float], dates: pd.Series | None = None) -> pd.DataFrame:
    """构造 stored 因子 DataFrame（含 NULL 支持）。"""
    if dates is None:
        dates = _dates(len(factors))
    return pd.DataFrame({"trade_date": dates, "adj_factor": factors})


def _expected_df(factors: list[float], dates: pd.Series | None = None) -> pd.DataFrame:
    """构造 expected 因子 DataFrame。"""
    if dates is None:
        dates = _dates(len(factors))
    return pd.DataFrame({"trade_date": dates, "expected_adj_factor": factors})


# =============================================================================
# 1. _compare_factors 纯函数测试
# =============================================================================


class TestCompareFactorsConsistent:
    """一致场景：stored == expected。"""

    def test_all_unit_consistent(self):
        """无事件股票（全 1.0）一致。600276 对照模式。"""
        dates = _dates(5)
        stored = _stored_df([1.0, 1.0, 1.0, 1.0, 1.0], dates)
        expected = _expected_df([1.0, 1.0, 1.0, 1.0, 1.0], dates)
        r = FactorConsistencyAuditor._compare_factors(
            uuid.uuid4(), "600276", stored, expected, max_mismatches=20,
        )
        assert r.is_consistent
        assert r.mismatch_count == 0
        assert r.missing_factor_count == 0
        assert not r.factor_all_unit_with_events
        assert not r.has_non_unit_expected

    def test_with_events_consistent(self):
        """有除权除息事件且 stored == expected 一致。"""
        dates = _dates(5)
        factors = [0.5, 0.5, 0.5, 0.8, 1.0]
        stored = _stored_df(factors, dates)
        expected = _expected_df(factors, dates)
        r = FactorConsistencyAuditor._compare_factors(
            uuid.uuid4(), "000001", stored, expected, max_mismatches=20,
        )
        assert r.is_consistent
        assert r.mismatch_count == 0
        assert r.has_non_unit_expected
        assert not r.stored_all_unit
        assert not r.factor_all_unit_with_events

    def test_both_empty_consistent(self):
        """双方都无数据（新上市）一致。"""
        stored = pd.DataFrame(columns=["trade_date", "adj_factor"])
        expected = pd.DataFrame(columns=["trade_date", "expected_adj_factor"])
        r = FactorConsistencyAuditor._compare_factors(
            uuid.uuid4(), "300001", stored, expected, max_mismatches=20,
        )
        assert r.is_consistent
        assert r.stored_count == 0
        assert r.expected_count == 0


class TestCompareFactors603538BugPattern:
    """603538 bug 模式：stored 全 1.0 但 expected 有非 1.0。"""

    def test_all_unit_with_events_detected(self):
        """stored 全 1.0 + expected 有非 1.0 → factor_all_unit_with_events=True。"""
        dates = _dates(3)
        stored = _stored_df([1.0, 1.0, 1.0], dates)
        expected = _expected_df([0.5, 0.5, 1.0], dates)
        r = FactorConsistencyAuditor._compare_factors(
            uuid.uuid4(), "603538", stored, expected, max_mismatches=20,
        )
        assert not r.is_consistent
        assert r.factor_all_unit_with_events
        assert r.stored_all_unit
        assert r.has_non_unit_expected
        assert r.mismatch_count == 2  # 前两行 1.0 vs 0.5

    def test_all_unit_with_events_earliest_mismatch(self):
        """earliest_mismatch 应为第一个不一致日期。"""
        dates = _dates(3)
        stored = _stored_df([1.0, 1.0, 1.0], dates)
        expected = _expected_df([0.5, 0.5, 1.0], dates)
        r = FactorConsistencyAuditor._compare_factors(
            uuid.uuid4(), "603538", stored, expected, max_mismatches=20,
        )
        assert r.earliest_mismatch == dates[0].date()


class TestCompareFactorsValueMismatch:
    """因子值 mismatch（stored 有非 1.0 但与 expected 不符）。"""

    def test_single_mismatch(self):
        """单行因子值不匹配。"""
        dates = _dates(3)
        stored = _stored_df([0.48, 0.5, 1.0], dates)
        expected = _expected_df([0.5, 0.5, 1.0], dates)
        r = FactorConsistencyAuditor._compare_factors(
            uuid.uuid4(), "000001", stored, expected, max_mismatches=20,
        )
        assert not r.is_consistent
        assert r.mismatch_count == 1
        assert r.mismatches[0].trade_date == dates[0].date()
        assert r.mismatches[0].stored_factor == pytest.approx(0.48)
        assert r.mismatches[0].expected_factor == pytest.approx(0.5)
        assert r.mismatches[0].diff == pytest.approx(-0.02)

    def test_tolerance_no_false_positive(self):
        """容差内的差异不算 mismatch。"""
        dates = _dates(2)
        # 差异 = 1e-9 < FACTOR_COMPARISON_TOLERANCE (1e-6)
        stored = _stored_df([0.5, 1.0], dates)
        expected = _expected_df([0.5 + 1e-9, 1.0], dates)
        r = FactorConsistencyAuditor._compare_factors(
            uuid.uuid4(), "000002", stored, expected, max_mismatches=20,
        )
        assert r.is_consistent
        assert r.mismatch_count == 0

    def test_max_mismatches_limit(self):
        """mismatches 列表受 max_mismatches 限制。"""
        n = 50
        dates = _dates(n)
        stored = _stored_df([1.0] * n, dates)
        expected = _expected_df([0.5] * n, dates)
        r = FactorConsistencyAuditor._compare_factors(
            uuid.uuid4(), "000003", stored, expected, max_mismatches=10,
        )
        assert r.mismatch_count == n
        assert len(r.mismatches) == 10  # 受 max_mismatches 限制


class TestCompareFactorsNullAndCount:
    """NULL 因子和行数不匹配。"""

    def test_stored_null_factor(self):
        """stored adj_factor 为 NULL 计入 missing。"""
        dates = _dates(3)
        stored = _stored_df([0.5, None, 1.0], dates)
        expected = _expected_df([0.5, 0.5, 1.0], dates)
        r = FactorConsistencyAuditor._compare_factors(
            uuid.uuid4(), "000004", stored, expected, max_mismatches=20,
        )
        assert not r.is_consistent
        assert r.missing_factor_count == 1
        assert r.mismatch_count == 1  # NULL 行也计入 mismatch
        assert r.mismatches[0].stored_factor is None
        assert r.mismatches[0].diff is None

    def test_count_mismatch(self):
        """行数不匹配 → count_mismatch error。"""
        dates = _dates(3)
        stored = _stored_df([1.0, 1.0], dates[:2])
        expected = _expected_df([1.0, 1.0, 1.0], dates)
        r = FactorConsistencyAuditor._compare_factors(
            uuid.uuid4(), "000005", stored, expected, max_mismatches=20,
        )
        assert not r.is_consistent
        assert r.error is not None
        assert "count_mismatch" in r.error

    def test_date_sequence_mismatch(self):
        """行数相同但日期不同 → date_sequence_mismatch。"""
        dates_a = pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"])
        dates_b = pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-19"])
        stored = _stored_df([1.0, 1.0, 1.0], dates_a)
        expected = _expected_df([1.0, 1.0, 1.0], dates_b)
        r = FactorConsistencyAuditor._compare_factors(
            uuid.uuid4(), "000006", stored, expected, max_mismatches=20,
        )
        assert not r.is_consistent
        assert r.error == "date_sequence_mismatch"


# =============================================================================
# 2. _hash_factor_series 确定性测试
# =============================================================================


class TestHashFactorSeries:
    """因子序列 hash 确定性。"""

    def test_same_input_same_hash(self):
        """相同输入 → 相同 hash。"""
        dates = _dates(3)
        df = _stored_df([0.5, 0.5, 1.0], dates)
        h1 = FactorConsistencyAuditor._hash_factor_series(df)
        h2 = FactorConsistencyAuditor._hash_factor_series(df)
        assert h1 == h2

    def test_different_values_different_hash(self):
        """不同因子值 → 不同 hash。"""
        dates = _dates(3)
        df1 = _stored_df([0.5, 0.5, 1.0], dates)
        df2 = _stored_df([0.6, 0.6, 1.0], dates)
        h1 = FactorConsistencyAuditor._hash_factor_series(df1)
        h2 = FactorConsistencyAuditor._hash_factor_series(df2)
        assert h1 != h2

    def test_empty_df_hash(self):
        """空 DataFrame → 'empty'。"""
        df = pd.DataFrame(columns=["trade_date", "adj_factor"])
        assert FactorConsistencyAuditor._hash_factor_series(df) == "empty"

    def test_null_vs_value_different_hash(self):
        """NULL 与具体值 hash 不同。"""
        dates = _dates(2)
        df_null = _stored_df([None, 1.0], dates)
        df_val = _stored_df([0.5, 1.0], dates)
        h_null = FactorConsistencyAuditor._hash_factor_series(df_null)
        h_val = FactorConsistencyAuditor._hash_factor_series(df_val)
        assert h_null != h_val

    def test_order_independent_hash(self):
        """行顺序不影响 hash（内部排序）。"""
        dates = pd.to_datetime(["2026-06-18", "2026-06-16", "2026-06-17"])
        df_unsorted = pd.DataFrame({
            "trade_date": dates, "adj_factor": [1.0, 0.5, 0.5],
        })
        dates_sorted = pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"])
        df_sorted = pd.DataFrame({
            "trade_date": dates_sorted, "adj_factor": [0.5, 0.5, 1.0],
        })
        h1 = FactorConsistencyAuditor._hash_factor_series(df_unsorted)
        h2 = FactorConsistencyAuditor._hash_factor_series(df_sorted)
        assert h1 == h2, "排序后内容相同应产生相同 hash"


# =============================================================================
# 3. summarize_audit_results 测试
# =============================================================================


class TestSummarizeAuditResults:
    """审计结果汇总。"""

    def test_summary_counts(self):
        """汇总 consistent/inconsistent/error 计数。"""
        results = [
            FactorAuditResult(
                instrument_id=uuid.uuid4(), symbol="600276",
                is_consistent=True, stored_count=100, expected_count=100,
                missing_factor_count=0, mismatch_count=0,
            ),
            FactorAuditResult(
                instrument_id=uuid.uuid4(), symbol="603538",
                is_consistent=False, stored_count=100, expected_count=100,
                missing_factor_count=0, mismatch_count=50,
                factor_all_unit_with_events=True,
            ),
            FactorAuditResult(
                instrument_id=uuid.uuid4(), symbol="000001",
                is_consistent=False, stored_count=100, expected_count=100,
                missing_factor_count=0, mismatch_count=3,
                error="compute_expected_failed: timeout",
            ),
        ]
        summary = asyncio_run(summarize_audit_results(results))
        assert summary.total_audited == 3
        assert summary.consistent_count == 1
        assert summary.inconsistent_count == 1  # 603538（无 error）
        assert summary.error_count == 1  # 000001（有 error）
        assert summary.factor_all_unit_with_events_count == 1
        assert summary.total_mismatches == 53
        assert "603538" in summary.inconsistent_symbols
        assert "000001" in summary.error_symbols
        assert summary.consistency_rate == pytest.approx(1 / 3)

    def test_empty_summary(self):
        """空结果汇总。"""
        summary = asyncio_run(summarize_audit_results([]))
        assert summary.total_audited == 0
        assert summary.consistency_rate == 0.0


# =============================================================================
# 4. 架构约束测试（只读审计）
# =============================================================================


class TestAuditReadOnlyArchitecture:
    """审计服务必须只读：不导入 rebuild/UPDATE 路径。"""

    def test_audit_service_no_rebuild_import(self):
        """factor_consistency_audit.py 不得导入 rebuild_adj_factors。"""
        source = _AUDIT_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    assert alias.name != "rebuild_adj_factors", (
                        "审计服务禁止导入 rebuild_adj_factors（只读约束）"
                    )

    def test_audit_service_no_session_commit(self):
        """审计服务不得调用 session.commit/execute(UPDATE/INSERT)。"""
        source = _AUDIT_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            # 禁止 session.commit() / session.add() / session.delete()
            if isinstance(node, ast.Attribute) and node.attr in (
                "commit", "add", "delete", "flush", "merge",
            ):
                # 允许在注释中出现，但禁止实际调用（ast.Attribute + Call）
                pass
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in (
                    "commit", "add", "delete", "flush", "merge",
                ):
                    pytest.fail(
                        f"审计服务禁止调用 session.{func.attr}()（只读约束）"
                    )

    def test_compute_expected_is_readonly(self):
        """compute_expected_adj_factors 不得执行写操作（UPDATE/INSERT/DELETE/commit）。

        使用 AST 检查而非字符串匹配，避免 docstring/注释中的写操作关键词误报。
        """
        source = _REPO_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        found_func = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "compute_expected_adj_factors":
                    found_func = True
                    # 遍历函数体 AST 节点，检查写操作
                    for child in ast.walk(node):
                        # 禁止 session.commit() / session.add() 等写方法调用
                        if isinstance(child, ast.Call):
                            func = child.func
                            if isinstance(func, ast.Attribute) and func.attr in (
                                "commit", "add", "delete", "flush", "merge",
                            ):
                                pytest.fail(
                                    f"compute_expected_adj_factors 禁止调用 "
                                    f"{func.attr}()（只读约束）"
                                )
                        # 禁止 text("UPDATE/INSERT/DELETE...") 调用
                        if isinstance(child, ast.Call):
                            func = child.func
                            # text(...) 调用
                            if isinstance(func, ast.Name) and func.id == "text":
                                for arg in child.args:
                                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                                        sql = arg.value.upper()
                                        if any(kw in sql for kw in (
                                            "UPDATE", "INSERT", "DELETE", "DROP",
                                        )):
                                            pytest.fail(
                                                f"compute_expected_adj_factors 禁止含 "
                                                f"写 SQL: {arg.value}"
                                            )
        assert found_func, "compute_expected_adj_factors 函数必须存在"


# =============================================================================
# 5. 因子合同常量测试
# =============================================================================


class TestFactorContractConstants:
    """因子合同常量版本正确。"""

    def test_algorithm_version_is_fq_v1(self):
        assert FACTOR_ALGORITHM_VERSION == "fq-v1"

    def test_reconciliation_version_is_1(self):
        assert FACTOR_RECONCILIATION_VERSION == 1

    def test_tolerance_positive(self):
        assert FACTOR_COMPARISON_TOLERANCE > 0
        assert FACTOR_COMPARISON_TOLERANCE < 0.001  # 不会误报真实因子差异


# =============================================================================
# helpers
# =============================================================================


def asyncio_run(coro):
    """同步运行 coroutine（测试 helper）。

    summarize_audit_results 是 async 函数，调用后返回 coroutine 对象。
    使用 asyncio.run 执行（Python 3.12 推荐，避免已弃用的 get_event_loop）。
    """
    import asyncio
    return asyncio.run(coro)
