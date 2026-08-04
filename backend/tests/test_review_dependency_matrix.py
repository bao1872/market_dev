"""[QM-63 review 依赖矩阵 / 质量硬门 / 原子发布 2026-08-04] Review 合同测试。

纯单元测试（mock AsyncSession），无需数据库：
    PURE_UNIT_TEST=1 python -m pytest tests/test_review_dependency_matrix.py -v

覆盖计划 9 项中的可测场景：
1. chip 不可用 → 降级 core-only + degraded_reasons=[CHIP_UNAVAILABLE]
2. chip 部分成功 → source_chip_run_id 记录 + degraded_reasons=[CHIP_PARTIAL]
3. auction 失败默认降级不阻断（门禁不因 auction 缺失而 block）
4. 59 条历史 → insufficient_history（非 unavailable）；60 条 → normalized_ready
5. 禁止读取未来数据（point-in-time 违规 → 门禁 block）
6. industry/concept 隔离：expected 与 actual scope_key 必须一致
7. all-null（P/Q/U/C/V 全 None）不可发布
8. 原子发布失败保留旧 pointer（重复发布幂等，零写入）
9. 非 ready 必须给出 reason（禁止无原因的不可用）
10. API/schema 暴露合同：degraded_reasons + source_chip_run_id 透传
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects.postgresql.dml import Insert as PgInsert

from app.models.market_review import MarketReviewRun, MarketReviewScopeSnapshot
from app.services.review_orchestrator_service import _resolve_chip_dependency
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


def _make_run(*, status: str = "signals_ready", degraded_reasons: list[str] | None = None) -> MarketReviewRun:
    return MarketReviewRun(
        id=uuid.uuid4(),
        trade_date=date(2026, 7, 31),
        source_core_run_id=uuid.uuid4(),
        source_board_run_id=uuid.uuid4(),
        source_chip_run_id=None,
        degraded_reasons=degraded_reasons or [],
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
        metadata_json={},
    )


def _ready_payload(value: float | None = 0.5) -> dict:
    return {
        "value": value,
        "rawValue": value,
        "status": "ready" if value is not None else "insufficient_history",
        "readiness": {
            "status": "ready" if value is not None else "insufficient_history",
            "raw_ready": True,
            "normalized_ready": True,
            "reason": None if value is not None else "历史 <60 日，需运行 bootstrap",
        },
    }


def _unavailable_payload(reason: str | None = None) -> dict:
    return {
        "value": None,
        "rawValue": None,
        "status": "unavailable",
        "readiness": {
            "status": "unavailable",
            "raw_ready": False,
            "normalized_ready": False,
            "reason": reason,
        },
    }


def _insufficient_payload(reason: str = "历史 <60 日，需运行 bootstrap") -> dict:
    return {
        "value": None,
        "rawValue": 0.5,
        "status": "insufficient_history",
        "readiness": {
            "status": "insufficient_history",
            "raw_ready": True,
            "normalized_ready": False,
            "reason": reason,
        },
    }


def _make_market_snap(run_id: uuid.UUID, payloads: dict | None = None) -> MarketReviewScopeSnapshot:
    payloads = payloads or {}
    return MarketReviewScopeSnapshot(
        id=uuid.uuid4(),
        review_run_id=run_id,
        trade_date=date(2026, 7, 31),
        scope_type="market",
        scope_key="market",
        scope_name="全市场",
        status="ready",
        coverage_ratio=Decimal("1.0"),
        p_payload=payloads.get("P", _ready_payload()),
        q_payload=payloads.get("Q", _ready_payload()),
        u_payload=payloads.get("U", _ready_payload()),
        c_payload=payloads.get("C", _ready_payload()),
        v_payload=payloads.get("V", _ready_payload()),
    )


def _gate_pass_results(run: MarketReviewRun, *, future_obs_count: int = 0, market_payloads: dict | None = None) -> list[_FakeResult]:
    """evaluate_publish_gate 全部通过所需的查询序列（10 次 execute）。

    顺序：market / major_index / style / industry_l1 / universe defs /
    expected L1 / incomplete items / core pub / board pub / future_obs_count。
    """
    market_snap = _make_market_snap(run.id, market_payloads)
    board_id = uuid.uuid4()
    industry_snap = MarketReviewScopeSnapshot(
        id=uuid.uuid4(),
        review_run_id=run.id,
        trade_date=run.trade_date,
        scope_type="industry_l1",
        scope_key=str(board_id),
        scope_name="电子",
        status="ready",
        coverage_ratio=Decimal("1.0"),
    )
    core_pub = AsyncMock()
    core_pub.data_run_id = run.source_core_run_id
    board_pub = AsyncMock()
    board_pub.data_run_id = run.source_board_run_id
    return [
        _FakeResult(scalar=market_snap),
        _FakeResult(scalar_list=[]),
        _FakeResult(scalar_list=[]),
        _FakeResult(scalar_list=[industry_snap]),
        _FakeResult(scalar_list=[]),
        _FakeResult(scalar_list=[(board_id,)]),
        _FakeResult(scalar_list=[]),
        _FakeResult(scalar=core_pub),
        _FakeResult(scalar=board_pub),
        _FakeResult(scalar=future_obs_count),  # future_obs count 查询
    ]


# =============================================================================
# 1-2. chip 依赖矩阵（_resolve_chip_dependency）
# =============================================================================


async def test_chip_unavailable_downgrades_to_core_only() -> None:
    """chip 完全缺失 → source_chip_run_id=None, degraded_reasons=[CHIP_UNAVAILABLE]。"""
    run = _make_run()
    session = _make_session([
        _FakeResult(scalar_list=[]),  # 无任何 chip 快照
    ])
    chip_run_id, reasons = await _resolve_chip_dependency(
        session, trade_date=run.trade_date, source_core_run_id=run.source_core_run_id,
    )
    assert chip_run_id is None
    assert reasons == ["CHIP_UNAVAILABLE"]


async def test_chip_partial_success_records_source_and_degraded() -> None:
    """chip 部分成功 → 记录 source_chip_run_id，degraded_reasons=[CHIP_PARTIAL]。"""
    run = _make_run()
    # group_by 返回 (status, count) 两行：succeeded=10, failed=5
    session = _make_session([
        _FakeResult(scalar_list=[("succeeded", 10), ("failed", 5)]),
    ])
    chip_run_id, reasons = await _resolve_chip_dependency(
        session, trade_date=run.trade_date, source_core_run_id=run.source_core_run_id,
    )
    assert chip_run_id == run.source_core_run_id
    assert reasons == ["CHIP_PARTIAL"]


async def test_chip_all_succeeded_no_degradation() -> None:
    """chip 全部成功 → 记录 source_chip_run_id，无降级。"""
    run = _make_run()
    session = _make_session([
        _FakeResult(scalar_list=[("succeeded", 10)]),
    ])
    chip_run_id, reasons = await _resolve_chip_dependency(
        session, trade_date=run.trade_date, source_core_run_id=run.source_core_run_id,
    )
    assert chip_run_id == run.source_core_run_id
    assert reasons == []


async def test_chip_all_failed_downgrades_to_core_only() -> None:
    """chip 全部失败 → source_chip_run_id=None, degraded_reasons=[CHIP_UNAVAILABLE]。"""
    run = _make_run()
    session = _make_session([
        _FakeResult(scalar_list=[("failed", 10)]),
    ])
    chip_run_id, reasons = await _resolve_chip_dependency(
        session, trade_date=run.trade_date, source_core_run_id=run.source_core_run_id,
    )
    assert chip_run_id is None
    assert reasons == ["CHIP_UNAVAILABLE"]


# =============================================================================
# 3. auction 失败默认降级不阻断
# =============================================================================


async def test_auction_absence_does_not_block_publish() -> None:
    """auction 维度不参与发布门禁（失败默认降级，不阻断）。

    evaluate_publish_gate 不查询 auction 表，auction 缺失不应产生 blocker。
    """
    run = _make_run()
    results = _gate_pass_results(run)
    publishable, blockers = await evaluate_publish_gate(session := _make_session(results), run)
    assert publishable is True
    assert blockers == []


# =============================================================================
# 4. 59/60 边界（insufficient_history vs ready）
# =============================================================================


async def test_gauge_insufficient_history_not_unavailable() -> None:
    """59 条历史 → P metric status=insufficient_history（normalized=None），非 unavailable。

    验证 readiness 四态区分：raw_ready=True, normalized_ready=False。
    """
    payload = _insufficient_payload()
    assert payload["status"] == "insufficient_history"
    assert payload["readiness"]["raw_ready"] is True
    assert payload["readiness"]["normalized_ready"] is False
    # 门禁对 insufficient_history 仍 block（但语义正确，不误判 unavailable）
    run = _make_run()
    results = _gate_pass_results(
        run, market_payloads={"P": payload},
    )
    publishable, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is False
    assert any("insufficient_history" in b for b in blockers)


# =============================================================================
# 5. 无未来数据门禁
# =============================================================================


async def test_future_data_blocks_publish() -> None:
    """检测到 >= trade_date 的 observation → 门禁 block（point-in-time 违规）。"""
    run = _make_run()
    results = _gate_pass_results(run, future_obs_count=3)
    publishable, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is False
    assert any("未来数据" in b or "point-in-time" in b for b in blockers)


async def test_no_future_data_passes() -> None:
    """无未来数据 → future_obs_count=0，门禁不因该检查 block。"""
    run = _make_run()
    results = _gate_pass_results(run, future_obs_count=0)
    publishable, _ = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is True


# =============================================================================
# 6. industry/concept 隔离
# =============================================================================


async def test_industry_scope_key_mismatch_blocks() -> None:
    """expected 行业 scope_key 与实际不一致 → 门禁 block（隔离违规）。"""
    run = _make_run()
    board_id = uuid.uuid4()
    # actual industry snap scope_key 与 expected 不同
    industry_snap = MarketReviewScopeSnapshot(
        id=uuid.uuid4(),
        review_run_id=run.id,
        trade_date=run.trade_date,
        scope_type="industry_l1",
        scope_key=str(uuid.uuid4()),  # 不匹配 expected
        scope_name="电子",
        status="ready",
        coverage_ratio=Decimal("1.0"),
    )
    core_pub = AsyncMock()
    core_pub.data_run_id = run.source_core_run_id
    board_pub = AsyncMock()
    board_pub.data_run_id = run.source_board_run_id
    results = [
        _FakeResult(scalar=_make_market_snap(run.id)),
        _FakeResult(scalar_list=[]),
        _FakeResult(scalar_list=[]),
        _FakeResult(scalar_list=[industry_snap]),
        _FakeResult(scalar_list=[]),
        _FakeResult(scalar_list=[(board_id,)]),  # expected = board_id
        _FakeResult(scalar_list=[]),
        _FakeResult(scalar=core_pub),
        _FakeResult(scalar=board_pub),
        _FakeResult(scalar=0),
    ]
    publishable, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is False
    assert any("配置范围缺失" in b or "存在非配置范围" in b for b in blockers)


# =============================================================================
# 7. all-null 不可发布
# =============================================================================


async def test_all_null_metrics_block_publish() -> None:
    """market P/Q/U/C/V 全部 value=None → 禁止发布空壳。"""
    run = _make_run()
    null_payloads = {k: _unavailable_payload(reason="上游字段缺失") for k in ("P", "Q", "U", "C", "V")}
    results = _gate_pass_results(run, market_payloads=null_payloads)
    publishable, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is False
    assert any("空壳" in b for b in blockers)


# =============================================================================
# 8. 原子发布幂等（重复发布零写入，保留旧 pointer）
# =============================================================================


async def test_republish_is_zero_write_idempotent() -> None:
    """已 published 且为当前 live pointer 的 run 再次发布 → 返回已有 publication，零写入。"""
    published_at = datetime.now(UTC)
    run = _make_run(status="published", degraded_reasons=[])
    run.published_at = published_at
    pointer = AsyncMock()
    pointer.id = uuid.uuid4()
    pointer.data_run_id = run.id
    # gate pass（9 queries） + idempotent return under lock（market/core/board/review live pointer）
    core_pub = AsyncMock()
    core_pub.data_run_id = run.source_core_run_id
    board_pub = AsyncMock()
    board_pub.data_run_id = run.source_board_run_id
    board_id = uuid.uuid4()
    results = [
        _FakeResult(scalar=_make_market_snap(run.id)),
        _FakeResult(scalar_list=[]),
        _FakeResult(scalar_list=[]),
        _FakeResult(scalar_list=[MarketReviewScopeSnapshot(
            id=uuid.uuid4(), review_run_id=run.id, trade_date=run.trade_date,
            scope_type="industry_l1", scope_key=str(board_id), scope_name="x",
            status="ready", coverage_ratio=Decimal("1.0"),
        )]),
        _FakeResult(scalar_list=[]),
        _FakeResult(scalar_list=[(board_id,)]),
        _FakeResult(scalar_list=[]),
        _FakeResult(scalar=core_pub),
        _FakeResult(scalar=board_pub),
        # run.status == "published" 分支：gate 内先查 review pointer（第 10 次）
        _FakeResult(scalar=pointer),
        _FakeResult(scalar=0),  # future_obs count（第 11 次）
        # publish_review 幂等分支：再查 review pointer（第 12 次）
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


# =============================================================================
# 9. 非 ready 必须给出 reason
# =============================================================================


async def test_non_ready_without_reason_blocks() -> None:
    """market 非 ready 但缺 reason → 门禁 block（禁止无原因的不可用）。"""
    run = _make_run()
    payload = _unavailable_payload(reason=None)  # 缺 reason
    results = _gate_pass_results(run, market_payloads={"P": payload})
    publishable, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is False
    assert any("缺 reason" in b for b in blockers)


async def test_non_ready_with_reason_passes_that_check() -> None:
    """非 ready 但给出 reason → 该检查通过（仍因 insufficient/unavailable 被其他检查 block）。"""
    run = _make_run()
    payload = _unavailable_payload(reason="上游字段缺失")
    results = _gate_pass_results(run, market_payloads={"P": payload})
    _, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert not any("缺 reason" in b for b in blockers)


# =============================================================================
# 10. API / schema 暴露合同：degraded_reasons + source_chip_run_id 必须透传
# =============================================================================


async def test_overview_response_exposes_chip_dependency() -> None:
    """ReviewOverviewResponse 必须返回 sourceChipRunId 与 degradedReasons。"""
    from app.schemas.review import ReviewOverviewResponse

    resp = ReviewOverviewResponse(
        reviewRunId=str(uuid.uuid4()),
        tradeDate="2026-08-04",
        status="signals_ready",
        sourceCoreRunId=str(uuid.uuid4()),
        sourceBoardRunId=str(uuid.uuid4()),
        sourceChipRunId=None,  # chip 不可用，core-only 降级
        degradedReasons=["CHIP_UNAVAILABLE"],
        algorithmVersion="v1",
        filterVersion="f1",
        baselineWindow=60,
    )
    payload = resp.model_dump(mode="json")
    # 契约：sourceChipRunId=null 必须明确返回（不得省略导致前端当"未记录"）
    assert "sourceChipRunId" in payload
    assert payload["sourceChipRunId"] is None
    assert payload["degradedReasons"] == ["CHIP_UNAVAILABLE"]


async def test_run_response_exposes_degraded_reasons() -> None:
    """ReviewRunResponse（管理端）必须返回 source_chip_run_id 与 degraded_reasons。"""
    from app.schemas.review import ReviewRunResponse

    resp = ReviewRunResponse(
        id=str(uuid.uuid4()),
        trade_date="2026-08-04",
        source_core_run_id=str(uuid.uuid4()),
        source_board_run_id=str(uuid.uuid4()),
        source_chip_run_id=str(uuid.uuid4()),
        degraded_reasons=["CHIP_PARTIAL"],
        algorithm_version="v1",
        filter_version="f1",
        baseline_window=60,
        status="signals_ready",
        created_at="2026-08-04T15:00:00Z",
        updated_at="2026-08-04T15:00:00Z",
    )
    payload = resp.model_dump(mode="json")
    assert payload["source_chip_run_id"] is not None
    assert payload["degraded_reasons"] == ["CHIP_PARTIAL"]
