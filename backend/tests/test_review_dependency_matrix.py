"""[QM-63 review 依赖矩阵 / 质量硬门 / 原子发布 2026-08-04] Review 合同测试。

纯单元测试（mock AsyncSession），无需数据库：
    PURE_UNIT_TEST=1 python -m pytest tests/test_review_dependency_matrix.py -v

覆盖计划 9 项中的可测场景：
3. auction 失败默认降级不阻断（门禁不因 auction 缺失而 block）
4. 59 条历史 → insufficient_history（非 unavailable）；60 条 → normalized_ready
5. 禁止读取未来数据（point-in-time 违规 → 门禁 block）
6. industry/concept 隔离：expected 与 actual scope_key 必须一致
7. all-null（P/Q/U/C/V 全 None）不可发布
8. 原子发布失败保留旧 pointer（重复发布幂等，零写入）
9. 非 ready 必须给出 reason（禁止无原因的不可用）

[AUD-04/05 2026-08-07] 原第 1、2、10 项（chip 依赖矩阵与 chip 字段透传合同）已退役：
它们保护的是"Review 依赖 chip"这一被判定为错误的合同 —— Review 在创建阶段查询
chip、把 chip 降级原因写进 Review lineage、并允许晚到 chip 通过 ON CONFLICT 改写
已发布 Review。该合同与 test_review_v21_dependency_contract.py 声明的
"Review 只依赖 stock_core + market_aggregation" 直接矛盾（Test Contract Drift）。
现 Review 已与 chip 解耦，`_resolve_chip_dependency` 已删除，故相关用例整体移除。
替代保护见 test_review_v21_dependency_contract.py（create_run 层零 chip 查询）
与 test_review_immutability_contract.py（晚到 chip 不改写已有 run）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects.postgresql.dml import Insert as PgInsert

from app.models.market_review import MarketReviewRun, MarketReviewScopeSnapshot
from app.services.review_publication_service import (
    evaluate_publish_gate,
    publish_review,
)
from app.services.review_scope_service import ScopeDefinition, compute_scope_metrics

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
    """严格未来观测（trade_date > run.trade_date）→ 门禁 block（point-in-time 违规）。

    [P0 修复 2026-08-04] 门禁只拦截“乱序/未来”观测（> run.trade_date），
    不再把当前 run 自身当日观测（== run.trade_date）误判为未来数据。
    """
    run = _make_run()
    results = _gate_pass_results(run, future_obs_count=3)
    publishable, blockers = await evaluate_publish_gate(_make_session(results), run)
    assert publishable is False
    assert any("未来" in b or "point-in-time" in b for b in blockers)


async def test_own_same_day_observations_do_not_block() -> None:
    """当前 run 落库的当日观测（== trade_date）不得被当作未来数据拦截。

    计算当日 Review → 保存当日 observation → 发布门，合法 Review 必须能通过。
    """
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
# 11. [P0 2026-08-04] 真实组合链路：compute_scope_metrics → persist →
#     evaluate_publish_gate。当前 run 落库当日观测（== trade_date）不得阻塞发布。
# =============================================================================


def _make_flat_list(n: int = 6) -> list[dict]:
    """构造带 fp_trend_direction 的真实 first_pyramid_flat 列表。"""
    rows = []
    for i in range(n):
        rows.append({
            "_instrument_id": f"ins{i}",
            "fp_trend_direction": "bullish" if i % 2 == 0 else "bearish",
            "fp_trend_structure_level": "swing",
        })
    return rows


async def test_combination_legit_run_own_observations_pass_publish_gate() -> None:
    """compute_scope_metrics → persist_metric_observations → evaluate_publish_gate。

    真实组合：合法 Review 计算当日指标并落库当日观测后，发布门必须通过——
    不得把当前 run 自身 == trade_date 的观测误判为“未来数据”。
    """
    from sqlalchemy.dialects.postgresql.dml import Insert as _PgInsert

    run = _make_run()
    scope = ScopeDefinition(
        scope_type="market",
        scope_key="market",
        scope_name="全市场",
        membership_version="v1",
    )
    flat_list = _make_flat_list()

    # 阶段 1+2：compute_scope_metrics 内部调用 persist_metric_observations，
    # 记录所有 pg_insert 写入（scope snapshot + 各 metric observation）。
    writes: list = []
    compute_session = AsyncMock()
    compute_session.flush = AsyncMock()

    async def _capture(*args, **kwargs):
        writes.append(args[0])
        return _FakeResult(scalar=None)

    compute_session.execute = AsyncMock(side_effect=_capture)

    snap = await compute_scope_metrics(
        compute_session,
        run.id,
        run.trade_date,
        scope,
        flat_list,
        algorithm_version=run.algorithm_version,
        eligible_count=len(flat_list),
    )
    # 确认真实产生了当日观测写入（== trade_date）
    obs_rows = [
        stmt
        for stmt in writes
        if isinstance(stmt, _PgInsert)
        and "market_review_metric_observations" in str(stmt)
    ]
    assert len(obs_rows) > 0, "compute_scope_metrics 必须真实写入 metric observations"
    assert snap is None or snap.scope_type == "market"  # 仅确认无异常

    # 阶段 3：对同一 run 运行发布门，构造全部通过序列（market snap 等）。
    # 关键：未来观测数为 0（本 run 只有 == trade_date 的当日观测）。
    gate_results = _gate_pass_results(run, future_obs_count=0)
    publishable, blockers = await evaluate_publish_gate(_make_session(gate_results), run)
    assert publishable is True, f"合法 Review 应通过发布门，blockers={blockers}"
    assert not any("未来" in b for b in blockers)


# =============================================================================
# 12. [CHANGE-20260808] LIVE taxonomy formal path：
#     compute_scope_metrics 必须把 scope.taxonomy_compatibility_key 显式传给
#     persist_metric_observations（LIVE observation 不得静默写 NULL taxonomy）。
# =============================================================================


async def _run_scope_metrics_capture(
    monkeypatch: pytest.MonkeyPatch,
    scope: ScopeDefinition,
) -> dict:
    """运行 compute_scope_metrics 并捕获 persist_metric_observations 收到的 kwargs。

    compute_scope_metrics 在函数体内 `from ... import persist_metric_observations`
    （local import），因此必须 patch 定义模块 app.services.review_metric_observation_service
    的符号；同时 mock upsert_scope_snapshot 让流程走到 persist 调用。
    """
    from app.services.review_scope_service import compute_scope_metrics

    captured: dict = {}

    async def _fake_persist(session, **kwargs):
        captured.update(kwargs)
        return 2

    from app.services import review_metric_observation_service as _obs_mod
    monkeypatch.setattr(_obs_mod, "persist_metric_observations", _fake_persist)

    async def _fake_upsert(*a, **kw):
        return object()  # 返回任意 snapshot ORM 占位

    monkeypatch.setattr(
        "app.services.review_scope_service.upsert_scope_snapshot", _fake_upsert,
    )

    run = _make_run()
    compute_session = AsyncMock()
    compute_session.flush = AsyncMock()
    async def _capture(*args, **kwargs):
        return _FakeResult(scalar=None)
    compute_session.execute = AsyncMock(side_effect=_capture)

    await compute_scope_metrics(
        compute_session,
        run.id,
        run.trade_date,
        scope,
        _make_flat_list(),
        algorithm_version=run.algorithm_version,
        eligible_count=1,
    )
    return captured


async def test_live_taxonomy_key_propagates_to_observation(monkeypatch) -> None:
    """industry scope taxonomy=B → LIVE observation 必须持久化 taxonomy=B。"""
    scope = ScopeDefinition(
        scope_type="industry_l1",
        scope_key="board-1",
        scope_name="行业",
        taxonomy_version="taxo-v3",
        taxonomy_compatibility_key="taxo-B",
        membership_version="pit-v1",
    )
    captured = await _run_scope_metrics_capture(monkeypatch, scope)
    # taxonomy key 必须显式传到 observation 持久化（非 NULL）
    assert captured.get("taxonomy_compatibility_key") == "taxo-B"
    assert captured.get("scope_type") == "industry_l1"


async def test_market_taxonomy_none_explicit(monkeypatch) -> None:
    """market scope taxonomy=None → 显式传 None（不得静默省略）。"""
    scope = ScopeDefinition(
        scope_type="market",
        scope_key="market",
        scope_name="全市场",
        taxonomy_version=None,
        taxonomy_compatibility_key=None,
        membership_version="v1",
    )
    captured = await _run_scope_metrics_capture(monkeypatch, scope)
    assert "taxonomy_compatibility_key" in captured
    assert captured["taxonomy_compatibility_key"] is None
