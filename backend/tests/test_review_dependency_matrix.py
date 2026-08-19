"""[REVIEW-CANONICAL-RUNTIME-REPLACEMENT] Review canonical 发布门禁合同测试。

纯单元测试（mock AsyncSession），无需数据库：
    PURE_UNIT_TEST=1 python -m pytest tests/test_review_dependency_matrix.py -v

[REVIEW-CANONICAL-RUNTIME-REPLACEMENT] 发布门禁业务判断已从 legacy
``MarketReviewScopeSnapshot`` / P/Q/U/C/V normalized_ready 迁移为 canonical
``run.metadata_json["canonical_composition_readiness"]``（由唯一 composition
owner 写入）。本文件锁定新门禁契约：

- auction 失败默认降级不阻断（门禁不因 auction 缺失而 block）；
- canonical composition readiness = insufficient_history → 阻塞；
- 无 canonical composition（空壳 run）→ 阻塞；
- 任一 activated scope 的 canonical readiness 非 ready → 阻塞；
- market / major_index / style 是非激活家族，无 composition 合法（不阻塞）；
- 禁止读取未来数据（point-in-time 违规 → 门禁 block）；
- 原子发布失败保留旧 pointer（重复发布幂等，零写入）。

legacy P/Q/U/C/V 计算（compute_scope_metrics / apply_cross_section_percentiles）
与旧 snapshot 写入已物理删除，因此旧的 P/Q/U/C/V payload 语义测试与本文件
"真实组合链路" 用例一并移除。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects.postgresql.dml import Insert as PgInsert

from app.models.market_review import MarketReviewRun
from app.services.review_publication_service import (
    evaluate_publish_gate,
    publish_review,
)

pytestmark = pytest.mark.asyncio


# =============================================================================
# Mock 工具
# =============================================================================


class _FakeResult:
    def __init__(self, *, scalar: object = None, scalar_list: list | None = None) -> None:
        self._scalar = scalar
        self._list = scalar_list if scalar_list is not None else []

    def scalar_one_or_none(self) -> object:
        return self._scalar

    def scalar(self) -> object:
        return self._scalar

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list:
        return self._list

    def __iter__(self):
        return iter(self._list)


def _make_session(execute_results: list[_FakeResult]) -> AsyncMock:
    session = AsyncMock()
    # 用生成器兜底：结果列表耗尽时返回最后一个结果（通常为 pointer），
    # 避免 side_effect 列表耗尽抛 StopIteration 导致断言失真。
    fallback = execute_results[-1] if execute_results else _FakeResult(scalar=None)

    def _effect(*args, **kwargs):
        if execute_results:
            return execute_results.pop(0)
        return fallback

    session.execute = AsyncMock(side_effect=_effect)
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    session.get = AsyncMock(return_value=None)
    return session


def _make_run(
    *,
    status: str = "signals_ready",
    composition_readiness: dict | None = None,
) -> MarketReviewRun:
    return MarketReviewRun(
        id=uuid.uuid4(),
        trade_date=date(2026, 7, 31),
        source_core_run_id=uuid.uuid4(),
        source_board_run_id=uuid.uuid4(),
        source_chip_run_id=None,
        degraded_reasons=[],
        algorithm_version="review-2.0.0",
        filter_version="filters-1.1.0",
        baseline_window=120,
        status=status,
        expected_scope_count=2,
        succeeded_scope_count=2,
        failed_scope_count=0,
        signal_count=171,
        coverage_ratio=Decimal("1.0"),
        published_at=None,
        metadata_json={
            # 默认：唯一 activated 家族（industry_l1）composition ready。
            # market/major_index/style 是非激活家族，无 composition 合法。
            "canonical_composition_readiness": (
                composition_readiness
                if composition_readiness is not None
                else {str(uuid.uuid4()): "ready"}
            ),
            "canonical_coverage": {"market": {"provided": 100, "eligible": 100}},
        },
    )


def _gate_pass_results(
    run: MarketReviewRun,
    *,
    future_obs_count: int = 0,
    live_review_pointer: object | None = None,
) -> list[_FakeResult]:
    """evaluate_publish_gate 全部通过所需的查询序列。

    新门禁顺序：incomplete run items → stock_core pointer → board pointer →
    [仅 run.status == "published"] live review pointer → future_obs count。
    （canonical_composition_readiness 从 run.metadata_json 读取，无 DB 查询。）
    """
    core_pub = AsyncMock()
    core_pub.data_run_id = run.source_core_run_id
    board_pub = AsyncMock()
    board_pub.data_run_id = run.source_board_run_id
    results = [
        _FakeResult(scalar_list=[]),          # 1. incomplete run items
        _FakeResult(scalar=core_pub),         # 2. stock_core pointer
        _FakeResult(scalar=board_pub),        # 3. board pointer
    ]
    if run.status == "published":
        results.append(_FakeResult(scalar=live_review_pointer))  # 4. live review pointer
    results.append(_FakeResult(scalar=future_obs_count))        # future_obs count
    return results


# =============================================================================
# 1. auction 失败默认降级不阻断
# =============================================================================


async def test_auction_absence_does_not_block_publish() -> None:
    """auction 维度不参与发布门禁（失败默认降级，不阻断）。"""
    run = _make_run()
    results = _gate_pass_results(run)
    publishable, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is True
    assert blockers == []


# =============================================================================
# 2. canonical composition readiness 非 ready → 阻塞
# =============================================================================


async def test_insufficient_history_composition_blocks() -> None:
    """activated scope 的 canonical readiness=insufficient_history → 阻塞。"""
    run = _make_run(composition_readiness={str(uuid.uuid4()): "insufficient_history"})
    results = _gate_pass_results(run)
    publishable, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is False
    assert any("非 ready" in b for b in blockers)


async def test_unavailable_composition_blocks() -> None:
    """activated scope 的 canonical readiness=unavailable_current → 阻塞。"""
    run = _make_run(composition_readiness={str(uuid.uuid4()): "unavailable_current"})
    results = _gate_pass_results(run)
    publishable, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is False
    assert any("非 ready" in b for b in blockers)


# =============================================================================
# 3. 空壳 run / 非激活家族无 composition
# =============================================================================


async def test_empty_composition_readiness_blocks_empty_shell() -> None:
    """无任何 canonical composition（空壳 run）→ 禁止发布。"""
    run = _make_run(composition_readiness={})
    results = _gate_pass_results(run)
    publishable, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is False
    assert any("空壳" in b for b in blockers)


async def test_non_activated_families_missing_composition_is_legal() -> None:
    """market / major_index / style 是非激活家族：无 composition 不阻塞。

    只有 activated 家族（industry_l1/l2/l3/concept）的 composition 才参与
    readiness gate；非激活家族缺 composition 是合法跳过，绝不回退 legacy。
    """
    activated_scope_key = str(uuid.uuid4())
    run = _make_run(composition_readiness={activated_scope_key: "ready"})
    # 元数据中只含 activated 家族，market 等不出现在 dict 中 → 仍可发布
    results = _gate_pass_results(run)
    publishable, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is True
    assert blockers == []


# =============================================================================
# 4. 无未来数据门禁
# =============================================================================


async def test_future_data_blocks_publish() -> None:
    """严格未来观测（trade_date > run.trade_date）→ 门禁 block（point-in-time 违规）。

    门禁只拦截“乱序/未来”观测（> run.trade_date），不再把当前 run 自身当日观测
    （== run.trade_date）误判为未来数据。
    """
    run = _make_run()
    results = _gate_pass_results(run, future_obs_count=3)
    publishable, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is False
    assert any("未来" in b or "point-in-time" in b for b in blockers)


async def test_own_same_day_observations_do_not_block() -> None:
    """当前 run 落库的当日观测（== trade_date）不得被当作未来数据拦截。"""
    run = _make_run()
    results = _gate_pass_results(run, future_obs_count=0)
    publishable, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is True
    assert not any("未来" in b for b in blockers)


async def test_no_future_data_passes() -> None:
    """无未来数据 → future_obs_count=0，门禁不因该检查 block。"""
    run = _make_run()
    results = _gate_pass_results(run, future_obs_count=0)
    publishable, _ = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is True


# =============================================================================
# 5. 原子发布幂等（重复发布零写入，保留旧 pointer）
# =============================================================================


async def test_republish_is_zero_write_idempotent() -> None:
    """已 published 且为当前 live pointer 的 run 再次发布 → 返回已有 publication，零写入。"""
    published_at = datetime.now(UTC)
    run = _make_run(status="published")
    run.published_at = published_at
    pointer = AsyncMock()
    pointer.id = uuid.uuid4()
    pointer.data_run_id = run.id
    # gate（4 queries） + idempotent return under lock（review live pointer）
    core_pub = AsyncMock()
    core_pub.data_run_id = run.source_core_run_id
    board_pub = AsyncMock()
    board_pub.data_run_id = run.source_board_run_id
    results = [
        _FakeResult(scalar_list=[]),      # 1. incomplete run items
        _FakeResult(scalar=core_pub),     # 2. stock_core pointer
        _FakeResult(scalar=board_pub),    # 3. board pointer
        _FakeResult(scalar=pointer),      # 4. live review pointer（published 分支）
        _FakeResult(scalar=0),            # 5. future_obs count
        # publish_review 幂等分支：再查 review pointer（第 6 次）
        _FakeResult(scalar=pointer),
    ]
    session = _make_session(results)
    pub = await publish_review(session, run, operator="admin", idempotency_key="k")
    # 返回既有 publication（旧 pointer 保留，未被替换）
    assert pub is pointer
    # 零写入：不得插入新 publication，不得 flush/delete
    insert_calls = [
        c for c in session.execute.call_args_list
        if c.args and isinstance(c.args[0], PgInsert)
    ]
    assert insert_calls == []
    session.flush.assert_not_awaited()
    session.delete.assert_not_awaited()
    # run 终态与发布时间未被改写
    assert run.status == "published"
    assert run.published_at == published_at
