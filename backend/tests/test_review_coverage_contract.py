"""[AUD-06 2026-08-07] Review coverage 语义合同测试。

纯单元测试（mock AsyncSession），无需数据库：
    PURE_UNIT_TEST=1 python -m pytest tests/test_review_coverage_contract.py -v

锁定的不变量：**run.coverage_ratio 表达真实有效样本覆盖率，不是 scope 执行成功率。**

背景（审计 AUD-06）：改动前 `run.coverage_ratio = succeeded_scope_count /
expected_scope_count`，即"有多少个 scope 跑完了"。这个数被当作数据质量指标对外
透出，并被 review 发布门禁按 >= 0.95 判定。其失真在于：10 个 scope 全部执行成功
（执行率 1.0）但每个 scope 底层只有 80/100 只票有有效样本时，真实数据覆盖只有
0.8，系统却报告 1.0 —— 一个"全绿"的复盘可能建立在两成缺失的数据上。

现在两个语义被分开表达：
- `run.coverage_ratio`  = SUM(scope.ready_count) / SUM(scope.eligible_count)（数据）
- `scope_execution_rate` = succeeded_scope_count / expected_scope_count（执行）
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.market_review import MarketReviewRun
from app.services.review_orchestrator_service import (
    _aggregate_run_data_coverage,
    _scope_execution_rate,
)

pytestmark = pytest.mark.asyncio


class _SumResult:
    """模拟 SELECT SUM(ready_count), SUM(eligible_count) 的返回。"""

    def __init__(self, row: tuple[int, int] | None) -> None:
        self._row = row

    def one_or_none(self) -> tuple[int, int] | None:
        return self._row


class _AggSession:
    def __init__(self, row: tuple[int, int] | None) -> None:
        self._row = row
        self.executed = 0

    async def execute(self, stmt):
        self.executed += 1
        return _SumResult(self._row)


def _make_run(*, expected: int, succeeded: int) -> MarketReviewRun:
    return MarketReviewRun(
        id=uuid.uuid4(),
        expected_scope_count=expected,
        succeeded_scope_count=succeeded,
        failed_scope_count=expected - succeeded,
    )


# =============================================================================
# 核心对照：执行率 1.0 与数据覆盖 0.8 必须可区分
# =============================================================================


async def test_full_execution_with_partial_data_coverage() -> None:
    """[AUD-06 核心用例] 10/10 scope 成功，但底层 800/1000 有效样本。

    执行率必须是 1.0，数据覆盖必须是 0.8 —— 两者不得互相冒充。
    """
    run = _make_run(expected=10, succeeded=10)
    session = _AggSession((800, 1000))

    data_coverage = await _aggregate_run_data_coverage(
        session, run.id,  # type: ignore[arg-type]
    )
    execution_rate = _scope_execution_rate(run)

    assert execution_rate == 1.0, "10/10 scope 成功，执行率应为 1.0"
    assert data_coverage == Decimal("0.8"), (
        "800/1000 有效样本，真实数据覆盖应为 0.8"
    )
    assert float(data_coverage) != execution_rate, (
        "两个语义必须可区分：这正是 AUD-06 要修的失真"
    )


async def test_coverage_is_not_execution_rate_when_scopes_fail() -> None:
    """反向：8/10 scope 成功但成功 scope 数据完整 → 执行率 0.8、数据覆盖 1.0。"""
    run = _make_run(expected=10, succeeded=8)
    session = _AggSession((800, 800))

    data_coverage = await _aggregate_run_data_coverage(
        session, run.id,  # type: ignore[arg-type]
    )

    assert _scope_execution_rate(run) == 0.8
    assert data_coverage == Decimal("1")


# =============================================================================
# 边界：分母为 0 不得除零，不得回落成执行率
# =============================================================================


async def test_zero_eligible_returns_zero_not_execution_rate() -> None:
    """eligible 总和为 0 → 返回 0，绝不回落成执行率（否则又变成冒充）。"""
    run = _make_run(expected=10, succeeded=10)
    session = _AggSession((0, 0))

    data_coverage = await _aggregate_run_data_coverage(
        session, run.id,  # type: ignore[arg-type]
    )

    assert data_coverage == Decimal("0")
    assert _scope_execution_rate(run) == 1.0
    assert float(data_coverage) != _scope_execution_rate(run), (
        "无有效样本时不得借执行率粉饰成 1.0"
    )


async def test_no_scope_snapshot_returns_zero() -> None:
    """无 scope 快照（聚合返回 None）→ 0，不抛异常。"""
    run = _make_run(expected=0, succeeded=0)
    session = _AggSession(None)

    assert await _aggregate_run_data_coverage(
        session, run.id,  # type: ignore[arg-type]
    ) == Decimal("0")


async def test_execution_rate_zero_expected_is_zero() -> None:
    """expected_scope_count=0 → 执行率 0，不得除零。"""
    assert _scope_execution_rate(_make_run(expected=0, succeeded=0)) == 0.0


async def test_partial_data_coverage_precision() -> None:
    """非整除比例保持精度（不得被四舍五入成 1.0 掩盖缺口）。"""
    run = _make_run(expected=3, succeeded=3)
    session = _AggSession((997, 1000))

    data_coverage = await _aggregate_run_data_coverage(
        session, run.id,  # type: ignore[arg-type]
    )

    assert data_coverage == Decimal("0.997")
    assert data_coverage < Decimal("1")
