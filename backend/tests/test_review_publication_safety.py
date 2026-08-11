"""[P0 安全收口 2026-08-01] Review 发布安全合同测试。

验证以下不变量（PRD §23.5 / §23.5A）：
1. force=True 只生成 provisional 标记：不写 factor_publications、
   run 不进入 published、published_at 不写入、metadata 记录审计字段；
2. provisional run 无正式 pointer，普通用户读取入口（dates/latest/
   overview/scopes 共用的 _get_published_run）返回 404；
3. admin 通过 include_partial=true 可读取同一 run；
4. 门禁通过的正式 run 仍可正常发布（写 pointer、status=published）；
5. withdrawal 幂等且不删除 run：只删 pointer，run 状态和发布时间保持不变；
6. after-close 只按当前正式 pointer 复用 published run。

测试环境：纯单元测试（mock AsyncSession），无需数据库：
    PURE_UNIT_TEST=1 python -m pytest tests/test_review_publication_safety.py -v
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects.postgresql.dml import Insert as PgInsert

from app.models.market_review import MarketReviewRun, MarketReviewScopeSnapshot
from app.services.review_publication_service import (
    ReviewWithdrawalBlockError,
    evaluate_publish_gate,
    get_published_review_run_id,
    is_formally_published_review_run,
    publish_review,
    withdraw_review_publication,
)

pytestmark = pytest.mark.asyncio


# =============================================================================
# Mock 工具
# =============================================================================


class _FakeResult:
    """模拟 sqlalchemy Result：支持 scalar / scalar_one_or_none / scalars().all()。"""

    def __init__(
        self,
        *,
        scalar: object = None,
        scalar_list: list | None = None,
    ) -> None:
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
    """构造 mock AsyncSession，execute 按序返回预置结果。"""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute_results)
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    session.get = AsyncMock(return_value=None)
    return session


def _executed_statements(session: AsyncMock) -> list:
    return [c.args[0] for c in session.execute.call_args_list]


def _make_run(
    *,
    status: str = "signals_ready",
    published_at: datetime | None = None,
) -> MarketReviewRun:
    return MarketReviewRun(
        id=uuid.uuid4(),
        trade_date=date(2026, 7, 31),
        source_core_run_id=uuid.uuid4(),
        source_board_run_id=uuid.uuid4(),
        algorithm_version="review-2.0.0",
        filter_version="filters-1.1.0",
        baseline_window=120,
        status=status,
        expected_scope_count=2,
        succeeded_scope_count=2,
        failed_scope_count=0,
        signal_count=171,
        coverage_ratio=Decimal("1.0"),
        published_at=published_at,
        metadata_json={},
    )


def _ready_payload() -> dict:
    return {
        "value": 0.5,
        "rawValue": 0.5,
        "status": "ready",
        "readiness": {
            "status": "ready",
            "raw_ready": True,
            "normalized_ready": True,
            "reason": None,
        },
    }


def _make_market_snap(run_id: uuid.UUID) -> MarketReviewScopeSnapshot:
    return MarketReviewScopeSnapshot(
        id=uuid.uuid4(),
        review_run_id=run_id,
        trade_date=date(2026, 7, 31),
        scope_type="market",
        scope_key="market",
        scope_name="全市场",
        status="ready",
        coverage_ratio=Decimal("1.0"),
        p_payload=_ready_payload(),
        q_payload=_ready_payload(),
        u_payload=_ready_payload(),
        c_payload=_ready_payload(),
        v_payload=_ready_payload(),
    )


def _gate_pass_results(
    run: MarketReviewRun,
    *,
    live_review_pointer: object | None = None,
    future_obs_count: int = 0,
) -> list[_FakeResult]:
    """构造 evaluate_publish_gate 全部通过所需的查询结果。

    查询顺序（与 evaluate_publish_gate 实现严格一致）：
        1 market / 2 major_index / 3 style / 4 industry_l1 /
        5 PIT universe defs / 6 expected L1 / 7 incomplete items /
        8 stock_core pointer / 9 board pointer /
        [仅 run.status == "published"] 10 live review pointer /
        末位 future_obs count（[QM-63] 无未来数据硬门）。
    """
    market_snap = _make_market_snap(run.id)
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
    results = [
        _FakeResult(scalar=market_snap),                        # 1. market scope
        _FakeResult(scalar_list=[industry_snap]),                # 2. all non-market scopes [V2]
        _FakeResult(scalar_list=[]),                            # 3. PIT universe definitions
        _FakeResult(scalar_list=[]),                            # 4. incomplete run items
        _FakeResult(scalar=core_pub),                           # 5. stock_core pointer
        _FakeResult(scalar=board_pub),                          # 6. board pointer
    ]
    if run.status == "published":
        results.append(_FakeResult(scalar=live_review_pointer))
    results.append(_FakeResult(scalar=future_obs_count))        # future_obs count
    return results


def _gate_fail_results() -> list[_FakeResult]:
    """门禁失败的查询结果（market snap 缺失 + 无行业快照）。"""
    return [
        _FakeResult(scalar=None),       # market snap 缺失 → blocker
        _FakeResult(scalar_list=[]),    # all non-market scopes [V2]
        _FakeResult(scalar_list=[]),    # PIT universe definitions
        _FakeResult(scalar_list=[]),    # incomplete run items
        _FakeResult(scalar=None),       # stock_core pointer
        _FakeResult(scalar=None),       # board pointer
        _FakeResult(scalar=0),          # future_obs count
    ]


# =============================================================================
# 1. force=True → provisional：不写 pointer、不置 published、metadata 可审计
# =============================================================================


class TestForceIsProvisional:
    async def test_force_does_not_write_pointer(self):
        run = _make_run()
        session = _make_session(_gate_fail_results())

        result = await publish_review(
            session, run,
            force=True, operator="admin-1", idempotency_key="k-1",
        )

        assert result is None
        # 不得执行任何 INSERT（pointer upsert）
        assert not any(
            isinstance(stmt, PgInsert) for stmt in _executed_statements(session)
        ), "force 路径不得写入 factor_publications"

    async def test_force_does_not_mark_published(self):
        run = _make_run(status="signals_ready", published_at=None)
        session = _make_session(_gate_fail_results())

        await publish_review(
            session, run,
            force=True, operator="admin-1", idempotency_key="k-1",
        )

        assert run.status == "signals_ready"
        assert run.published_at is None

    async def test_force_records_audit_metadata(self):
        run = _make_run()
        session = _make_session(_gate_fail_results())

        await publish_review(
            session, run,
            force=True, operator="admin-1", idempotency_key="k-1",
        )

        record = run.metadata_json.get("provisional_publication")
        assert record is not None
        assert record["force_requested"] is True
        assert record["is_provisional"] is True
        assert record["operator"] == "admin-1"
        assert record["idempotency_key"] == "k-1"
        assert record["requested_at"]
        # 门禁 blockers 必须记录（本用例 market snap 缺失 + 行业缺失）
        assert len(record["gate_blockers"]) >= 2


class TestFormalGateCompleteness:
    async def test_canary_and_provisional_are_never_formally_publishable(self):
        run = _make_run()
        run.metadata_json = {
            "canary": True,
            "provisional_publication": {"is_provisional": True},
        }
        publishable, blockers = await evaluate_publish_gate(
            _make_session(_gate_pass_results(run)), run,
        )
        assert publishable is False
        assert "canary run 不可正式发布" in blockers
        assert "provisional run 不可正式发布" in blockers

    async def test_configured_universe_missing_from_run_is_optional_diagnostic_not_blocker(
        self,
    ):
        """[Phase4C §6.3.8] major_index/style PIT 不可用（blocked_external_population）
        仅记为诊断，不阻塞整个 Market Review MVP 发布。market ready → 门禁 OPEN。
        """
        run = _make_run()
        results = _gate_pass_results(run)
        results[2] = _FakeResult(scalar_list=[SimpleNamespace(
            universe_type="major_index",
            universe_key="csi300",
            population_status="blocked_external_population",
        )])
        publishable, blockers = await evaluate_publish_gate(
            _make_session(results), run,
        )
        assert publishable is True
        assert not any("population 非 ready" in item for item in blockers)
        diag = run.metadata_json.get("optional_scope_diagnostics", [])
        assert any("population 非 ready" in item for item in diag)

    async def test_both_source_pointers_are_required(self):
        run = _make_run()
        publishable, blockers = await evaluate_publish_gate(
            _make_session(_gate_fail_results()), run,
        )
        assert publishable is False
        assert "正式 stock_core pointer 缺失" in blockers
        assert "正式 board pointer 缺失" in blockers

    async def test_superseded_published_run_cannot_overwrite_live_pointer(self):
        run = _make_run(status="published", published_at=datetime.now(UTC))
        live_review = AsyncMock()
        live_review.data_run_id = uuid.uuid4()
        results = _gate_pass_results(run, live_review_pointer=live_review)
        publishable, blockers = await evaluate_publish_gate(
            _make_session(results), run,
        )
        assert publishable is False
        assert any("禁止原地重发" in item for item in blockers)


# =============================================================================
# 2/3. provisional 不出现在普通用户入口；admin include_partial 可读
# =============================================================================


class TestProvisionalVisibility:
    async def test_user_entry_404_without_pointer(self):
        """无正式 pointer 时普通用户入口 404（provisional 不可见）。"""
        from fastapi import HTTPException

        from app.api.review import _get_published_run

        session = _make_session([_FakeResult(scalar=None)])  # 无 pointer
        with pytest.raises(HTTPException) as exc_info:
            await _get_published_run(
                session, date(2026, 7, 31), include_partial=False,
            )
        assert exc_info.value.status_code == 404

    async def test_admin_include_partial_reads_provisional(self):
        """admin include_partial=true 回退读取任意 run（含 provisional）。"""
        from app.api.review import _get_published_run

        run = _make_run()
        session = _make_session([
            _FakeResult(scalar=None),     # 无正式 pointer
            _FakeResult(scalar=run),      # include_partial 回退查询
        ])
        got = await _get_published_run(
            session, date(2026, 7, 31), include_partial=True,
        )
        assert got is run


# =============================================================================
# 4. 正式合格 run 正常发布
# =============================================================================


class TestFormalPublish:
    async def test_qualified_run_publishes_pointer(self):
        run = _make_run()
        pub = AsyncMock()
        pub.id = uuid.uuid4()
        session = _make_session([
            *_gate_pass_results(run),
            _FakeResult(),              # pointer upsert
            _FakeResult(scalar=pub),    # 发布成功后回读 pointer
        ])

        result = await publish_review(session, run, force=False)

        assert result is pub
        inserts = [
            s for s in _executed_statements(session)
            if isinstance(s, PgInsert)
        ]
        assert len(inserts) == 1, "正式发布必须写且只写一次 pointer"
        statements = _executed_statements(session)
        assert statements[7]._for_update_arg is not None
        assert statements[8]._for_update_arg is not None
        assert run.status == "published"
        assert run.published_at is not None
        assert "provisional_publication" not in (run.metadata_json or {})

    async def test_current_published_run_is_a_zero_write_idempotent_replay(self):
        published_at = datetime.now(UTC)
        run = _make_run(status="published", published_at=published_at)
        pointer = _make_pointer(run.id)
        results = _gate_pass_results(run, live_review_pointer=pointer)
        results.append(_FakeResult(scalar=pointer))  # idempotent return under lock
        session = _make_session(results)

        result = await publish_review(session, run, force=False)

        assert result is pointer
        assert run.published_at == published_at
        assert not any(
            isinstance(stmt, PgInsert) for stmt in _executed_statements(session)
        )


# =============================================================================
# 5/6. withdrawal：幂等、只删 pointer、历史 run 不变、复用依赖 live pointer
# =============================================================================


def _make_pointer(run_id: uuid.UUID) -> AsyncMock:
    pub = AsyncMock()
    pub.id = uuid.uuid4()
    pub.scope_type = "market"
    pub.scope_key = "market"
    pub.publication_kind = "market_review"
    pub.trade_date = date(2026, 7, 31)
    pub.algorithm_version = "1.0.0-core-split"
    pub.data_run_id = run_id
    pub.coverage_ratio = 1.0
    pub.published_at = datetime(2026, 8, 1, 10, 22, 3, tzinfo=UTC)
    return pub


def _withdraw_kwargs(run: MarketReviewRun, pub: AsyncMock) -> dict:
    return {
        "expected_run_id": run.id,
        "expected_publication_id": pub.id,
        "reason": "测试撤销",
        "operator": "test-operator",
        "idempotency_key": f"withdraw-{pub.id}",
    }


class TestWithdrawal:
    async def test_withdraw_deletes_only_pointer_and_keeps_run(self):
        run = _make_run(
            status="published",
            published_at=datetime(2026, 8, 1, 10, 22, 3, tzinfo=UTC),
        )
        pub = _make_pointer(run.id)
        session = _make_session([_FakeResult(scalar=pub)])
        session.get = AsyncMock(return_value=run)

        kwargs = _withdraw_kwargs(run, pub)
        kwargs.update({
            "reason": "force 发布的错误 run",
            "operator": "admin-1",
            "idempotency_key": "withdraw-1",
        })
        summary = await withdraw_review_publication(
            session, date(2026, 7, 31),
            **kwargs,
        )

        assert summary["withdrawn"] is True
        assert summary["pointer"]["data_run_id"] == str(run.id)
        # 只删除 pointer，不得删除 run
        assert session.delete.call_count == 1
        assert session.delete.call_args.args[0] is pub
        # run 的历史发布事实与数据完整保留
        assert run.status == "published"
        assert run.published_at == datetime(2026, 8, 1, 10, 22, 3, tzinfo=UTC)
        assert summary["run_status_reset"] is False
        assert summary["run_preserved"] is True
        # 审计字段
        audit = run.metadata_json.get("publication_withdrawal")
        assert audit is not None
        assert audit["reason"] == "force 发布的错误 run"
        assert audit["operator"] == "admin-1"
        assert audit["idempotency_key"] == "withdraw-1"
        assert audit["withdrawn_at"]
        assert audit["previous_pointer"]["id"] == str(pub.id)

    async def test_withdraw_idempotent_when_pointer_absent(self):
        session = _make_session([_FakeResult(scalar=None)])

        summary = await withdraw_review_publication(
            session, date(2026, 7, 31),
            expected_run_id=uuid.uuid4(),
            expected_publication_id=uuid.uuid4(),
            reason="重复执行", operator="admin-1",
            idempotency_key="withdraw-1",
        )

        assert summary["already_withdrawn"] is True
        assert summary["withdrawn"] is False
        session.delete.assert_not_called()

    async def test_withdraw_dry_run_writes_nothing(self):
        run = _make_run(
            status="published",
            published_at=datetime(2026, 8, 1, 10, 22, 3, tzinfo=UTC),
        )
        pub = _make_pointer(run.id)
        session = _make_session([_FakeResult(scalar=pub)])
        session.get = AsyncMock(return_value=run)

        kwargs = _withdraw_kwargs(run, pub)
        kwargs["reason"] = "演练"
        kwargs["operator"] = "admin-1"
        kwargs["idempotency_key"] = "withdraw-dry"
        summary = await withdraw_review_publication(
            session, date(2026, 7, 31),
            **kwargs, dry_run=True,
        )

        assert summary["dry_run"] is True
        assert summary["withdrawn"] is False
        assert summary["pointer_found"] is True
        assert summary["run_status_reset"] is False
        assert summary["run_preserved"] is True
        session.delete.assert_not_called()
        session.flush.assert_not_called()
        assert run.status == "published"  # 未改动

    async def test_withdrawn_run_not_reused_without_live_pointer(self):
        """历史 published 状态保留，但 live pointer 缺失时不可正式复用。"""
        run = _make_run(
            status="published",
            published_at=datetime(2026, 8, 1, 10, 22, 3, tzinfo=UTC),
        )
        pub = _make_pointer(run.id)
        session = _make_session([_FakeResult(scalar=pub)])
        session.get = AsyncMock(return_value=run)

        await withdraw_review_publication(
            session, date(2026, 7, 31),
            **_withdraw_kwargs(run, pub),
        )

        assert run.status == "published"
        assert run.published_at is not None

        pointer_session = _make_session([_FakeResult(scalar=None)])
        live_run_id = await get_published_review_run_id(
            pointer_session, date(2026, 7, 31),
        )
        assert is_formally_published_review_run(run, live_run_id) is False
        assert is_formally_published_review_run(run, run.id) is True

    async def test_dry_run_pointer_switch_is_rejected(self):
        run = _make_run(status="published", published_at=datetime.now(UTC))
        old_pub = _make_pointer(run.id)
        session = _make_session([_FakeResult(scalar=old_pub)])
        session.get = AsyncMock(return_value=run)

        dry_summary = await withdraw_review_publication(
            session, date(2026, 7, 31),
            **_withdraw_kwargs(run, old_pub), dry_run=True,
        )
        assert dry_summary["pointer"]["id"] == str(old_pub.id)

        new_pub = _make_pointer(uuid.uuid4())
        # Simulate the live pointer changing after the dry-run.
        apply_session = _make_session([_FakeResult(scalar=new_pub)])
        apply_session.get = AsyncMock(return_value=_make_run(status="published"))
        with pytest.raises(ReviewWithdrawalBlockError, match="expected_publication_id"):
            await withdraw_review_publication(
                apply_session, date(2026, 7, 31),
                **_withdraw_kwargs(run, old_pub),
            )
        apply_session.delete.assert_not_called()
        apply_session.flush.assert_not_called()


class TestProgressiveScopeReadiness:
    """[Phase4C §6.3.8] market=HARD GATE；industry/index/style=OPTIONAL 渐进 scope。

    可选 scope 不可用（bootstrap_unavailable / insufficient_history /
    blocked_external_population / PIT unavailable）仅记为诊断，不阻塞整个
    Market Review MVP 发布。
    """

    def _make_market_ready_run(self) -> MarketReviewRun:
        run = _make_run()
        # [Phase4C P0-A 修正] 真实全量 level-1 counters（不伪造 expected=1）：
        # 4 个 level-1 scope（market + industry_l1 + major_index + style），
        # 仅 market ready，3 个 optional 全部 unavailable（走 skipped/diagnostic）。
        run.expected_scope_count = 4
        run.succeeded_scope_count = 1
        run.failed_scope_count = 0
        return run

    def _market_snap(self, run: MarketReviewRun) -> MarketReviewScopeSnapshot:
        return MarketReviewScopeSnapshot(
            id=uuid.uuid4(),
            review_run_id=run.id,
            trade_date=run.trade_date,
            scope_type="market",
            scope_key="market",
            scope_name="全市场",
            status="ready",
            coverage_ratio=Decimal("1.0"),
            p_payload=_ready_payload(),
            q_payload=_ready_payload(),
            u_payload=_ready_payload(),
            c_payload=_ready_payload(),
            v_payload=_ready_payload(),
        )

    def _results_market_ready_optional_unavailable(
        self, run: MarketReviewRun,
    ) -> list[_FakeResult]:
        # 查询顺序与 evaluate_publish_gate 严格一致：
        # 1 market / 2 major_index / 3 style / 4 industry / 5 universe defs /
        # 6 expected L1 / 7 incomplete items / 8 stock_core pointer /
        # 9 board pointer / [published] live / future_obs count
        core_pub = SimpleNamespace(data_run_id=run.source_core_run_id)
        board_pub = SimpleNamespace(data_run_id=run.source_board_run_id)
        return [
            _FakeResult(scalar=self._market_snap(run)),   # 1 market ready
            _FakeResult(scalar_list=[]),                   # 2 all non-market scopes [V2]
            _FakeResult(scalar_list=[]),                   # 3 universe defs
            _FakeResult(scalar_list=[]),                   # 4 incomplete items
            _FakeResult(scalar=core_pub),                  # 5 stock_core pointer
            _FakeResult(scalar=board_pub),                 # 6 board pointer
            _FakeResult(scalar=0),                         # future_obs count
        ]

    def _opt_snap(self, scope_type: str, scope_key: str, status: str) -> MarketReviewScopeSnapshot:
        return MarketReviewScopeSnapshot(
            id=uuid.uuid4(),
            review_run_id=uuid.uuid4(),
            trade_date=date(2026, 7, 31),
            scope_type=scope_type,
            scope_key=scope_key,
            scope_name=scope_key,
            status=status,
            coverage_ratio=Decimal("1.0"),
        )

    def _results_market_ready_optional_ready(self, run: MarketReviewRun) -> list[_FakeResult]:
        core_pub = SimpleNamespace(data_run_id=run.source_core_run_id)
        board_pub = SimpleNamespace(data_run_id=run.source_board_run_id)
        return [
            _FakeResult(scalar=self._market_snap(run)),
            _FakeResult(scalar_list=[
                self._opt_snap("major_index", "csi300", "ready"),
                self._opt_snap("style", "large_cap_style", "ready"),
                self._opt_snap("industry_l1", "board-1", "ready"),
            ]),                                     # 2 all non-market scopes [V2]
            _FakeResult(scalar_list=[]),             # 3 universe defs
            _FakeResult(scalar_list=[]),             # 4 incomplete items
            _FakeResult(scalar=core_pub),
            _FakeResult(scalar=board_pub),
            _FakeResult(scalar=0),
        ]

    async def test_market_ready_optional_unavailable_gate_open(self):
        """A: market ready + 可选 scope 不可用 → 门禁 OPEN。"""
        run = self._make_market_ready_run()
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results_market_ready_optional_unavailable(run)), run,
        )
        assert publishable is True
        assert not any("population 非 ready" in b for b in blockers)
        diag = run.metadata_json.get("optional_scope_diagnostics", [])
        assert isinstance(diag, list)

    async def test_market_missing_gate_closed(self):
        """B: market 缺失 → CLOSED。"""
        run = self._make_market_ready_run()
        run.expected_scope_count = 0
        results = self._results_market_ready_optional_unavailable(run)
        results[0] = _FakeResult(scalar=None)  # no market snapshot
        publishable, blockers = await evaluate_publish_gate(
            _make_session(results), run,
        )
        assert publishable is False
        assert any("market 范围快照缺失" in b for b in blockers)

    async def test_market_not_ready_gate_closed(self):
        """C: market 存在但未 ready → CLOSED。"""
        run = self._make_market_ready_run()
        results = self._results_market_ready_optional_unavailable(run)
        # 真实 MarketReviewScopeSnapshot，status 非 ready（coverage 兜底达标以免干扰）
        not_ready_snap = MarketReviewScopeSnapshot(
            id=uuid.uuid4(),
            review_run_id=run.id,
            trade_date=run.trade_date,
            scope_type="market",
            scope_key="market",
            scope_name="全市场",
            status="insufficient_history",
            coverage_ratio=Decimal("1.0"),
            p_payload=_ready_payload(),
            q_payload=_ready_payload(),
            u_payload=_ready_payload(),
            c_payload=_ready_payload(),
            v_payload=_ready_payload(),
        )
        results[0] = _FakeResult(scalar=not_ready_snap)
        publishable, blockers = await evaluate_publish_gate(
            _make_session(results), run,
        )
        assert publishable is False
        assert any("market" in b for b in blockers)

    async def test_market_coverage_below_threshold_closed(self):
        """D: market coverage 低于强制门槛 → CLOSED。"""
        run = self._make_market_ready_run()
        results = self._results_market_ready_optional_unavailable(run)
        low_cov_snap = MarketReviewScopeSnapshot(
            id=uuid.uuid4(),
            review_run_id=run.id,
            trade_date=run.trade_date,
            scope_type="market",
            scope_key="market",
            scope_name="全市场",
            status="ready",
            coverage_ratio=Decimal("0.5"),
            p_payload=_ready_payload(),
            q_payload=_ready_payload(),
            u_payload=_ready_payload(),
            c_payload=_ready_payload(),
            v_payload=_ready_payload(),
        )
        results[0] = _FakeResult(scalar=low_cov_snap)
        publishable, blockers = await evaluate_publish_gate(
            _make_session(results), run,
        )
        assert publishable is False
        assert any("coverage" in b for b in blockers)

    async def test_market_ready_optional_ready_open(self):
        """E: market ready + 可选 scope 也 ready → OPEN（无 market blocker）。"""
        run = self._make_market_ready_run()
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results_market_ready_optional_ready(run)), run,
        )
        assert publishable is True
        # 可选 scope 即使 ready 也不应产生 market 阻塞
        assert not any("market" in b for b in blockers)

    async def test_market_ready_optional_error_keeps_diagnostic_open(self):
        """F: market ready + 可选 scope 错误/unavailable → OPEN 且诊断保留。"""
        run = self._make_market_ready_run()
        results = self._results_market_ready_optional_unavailable(run)
        # industry 快照存在但 status 非 ready（error 态）
        results[1] = _FakeResult(
            scalar_list=[self._opt_snap("industry_l1", "board-1", "error")],
        )
        publishable, blockers = await evaluate_publish_gate(
            _make_session(results), run,
        )
        assert publishable is True
        diag = run.metadata_json.get("optional_scope_diagnostics", [])
        # optional scope 错误态（status=error）作为诊断保留，不影响发布（非 blocker）
        assert any("industry_l1" in d for d in diag)

    # =========================================================================
    # [R6 直接证据 Phase4C 2026-08-09]
    # OPTIONAL_UNAVAILABLE vs UNEXPECTED_EXECUTION_FAILURE 是两个不同合同：
    #   - optional scope 数据源不可用（skipped）→ diagnostic only → OPEN
    #   - 任何 run item 处于 failed / pending / running（真实执行异常或非终态）
    #     → whole Review publication CLOSED
    # 注意：以下测试全部保持 market ready + 真实全量 counter
    # （expected_scope_count=4），不伪造 expected=1 来迎合 market-only gate。
    # =========================================================================

    def _run_item(self, run: MarketReviewRun, status: str, last_error: str | None = None):
        """构造 MarketReviewRunItem（真实 ORM 对象，非 mock）。"""
        from app.models.market_review import MarketReviewRunItem

        return MarketReviewRunItem(
            id=uuid.uuid4(),
            review_run_id=run.id,
            scope_type="industry_l1",
            scope_key="board-1",
            phase="metrics",
            status=status,
            attempt_count=1,
            last_error=last_error,
        )

    def _results_with_run_items(
        self, run: MarketReviewRun, items: list,
    ) -> list[_FakeResult]:
        """market ready + optional unavailable，第 7 个查询返回给定 incomplete items。

        evaluate_publish_gate 的第 7 个 execute 是
        `MarketReviewRunItem.status.in_(("failed","pending","running"))`，
        因此这里注入的就是 DB 真实会返回的未成功终态项集合。
        skipped 状态不在该查询条件内，故 skipped item 场景注入空列表。
        """
        results = self._results_market_ready_optional_unavailable(run)
        results[3] = _FakeResult(scalar_list=items)
        return results

    async def test_optional_skipped_item_is_diagnostic_only_gate_open(self):
        """R6-1: market ready + optional scope item = skipped → OPEN。

        skipped 是 optional scope 数据源不可用的诊断性终态，
        不属于 failed/pending/running 查询范围，不阻塞 whole Review。
        """
        run = self._make_market_ready_run()
        # skipped item 不会出现在 incomplete-items 查询结果里（真实 DB 语义）
        results = self._results_with_run_items(run, [])
        assert run.expected_scope_count == 4  # 真实全量 counter，未伪造
        publishable, blockers = await evaluate_publish_gate(
            _make_session(results), run,
        )
        assert publishable is True
        assert not any("未成功终态项" in b for b in blockers)

    async def test_optional_failed_item_unexpected_execution_failure_gate_closed(self):
        """R6-2: market ready + optional scope item = failed（非预期执行异常）→ CLOSED。

        这是与 R6-1 的关键区分：optional 语义只豁免"数据源不可用"，
        不豁免"执行异常"。failed item 必须阻塞整套 Review 发布。
        """
        run = self._make_market_ready_run()
        failed_item = self._run_item(
            run, "failed",
            last_error="TypeError: unexpected execution error in scope metrics",
        )
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results_with_run_items(run, [failed_item])), run,
        )
        assert publishable is False
        assert any("未成功终态项" in b for b in blockers)
        # 必须是 execution-failure blocker，而不是 market mandatory blocker
        assert not any("market 范围快照缺失" in b for b in blockers)

    async def test_pending_item_non_terminal_state_gate_closed(self):
        """R6-3: market ready + 存在 pending item（非终态）→ CLOSED。"""
        run = self._make_market_ready_run()
        pending_item = self._run_item(run, "pending")
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results_with_run_items(run, [pending_item])), run,
        )
        assert publishable is False
        assert any("未成功终态项" in b for b in blockers)
        assert not any("market 范围快照缺失" in b for b in blockers)

    async def test_running_item_non_terminal_state_gate_closed(self):
        """R6-4: market ready + 存在 running item（非终态）→ CLOSED。"""
        run = self._make_market_ready_run()
        running_item = self._run_item(run, "running")
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results_with_run_items(run, [running_item])), run,
        )
        assert publishable is False
        assert any("未成功终态项" in b for b in blockers)
        assert not any("market 范围快照缺失" in b for b in blockers)

    async def test_incomplete_items_query_targets_only_execution_failure_states(self):
        """R6-5: 门禁的 execution-failure 查询条件必须精确为 failed/pending/running。

        直接断言 SQL 语义，防止未来把 skipped 误并入 execution failure，
        或把 failed 误移出 blocker 集合（回归保护）。
        """
        run = self._make_market_ready_run()
        session = _make_session(self._results_with_run_items(run, []))
        await evaluate_publish_gate(session, run)
        # 第 7 个 execute 即 incomplete run items 查询
        sql = str(_executed_statements(session)[6].compile(
            compile_kwargs={"literal_binds": True},
        ))
        assert "failed" in sql
        assert "pending" in sql
        assert "running" in sql
        assert "skipped" not in sql

    async def test_forbidden_current_pit_fallback_contract_unchanged(self):
        """G: 既有禁止 current/PIT fallback 的 contract 不变（market 仍强制当前日可计算、
        可选 scope 状态不被伪装为 ready）。market coverage 低于门槛 → CLOSED。"""
        run = self._make_market_ready_run()
        results = self._results_market_ready_optional_unavailable(run)
        # market coverage 低于强制门槛，必须阻塞（禁止伪装 ready）
        m = results[0].scalar()
        m.coverage_ratio = Decimal("0.5")
        publishable, blockers = await evaluate_publish_gate(
            _make_session(results), run,
        )
        assert publishable is False
        assert any("coverage" in b for b in blockers)

    async def test_expected_run_id_mismatch_is_zero_write(self):
        run = _make_run(status="published", published_at=datetime.now(UTC))
        pub = _make_pointer(run.id)
        session = _make_session([_FakeResult(scalar=pub)])
        session.get = AsyncMock(return_value=run)
        with pytest.raises(ReviewWithdrawalBlockError, match="expected_run_id"):
            await withdraw_review_publication(
                session, date(2026, 7, 31),
                expected_run_id=uuid.uuid4(),
                expected_publication_id=pub.id,
                reason="mismatch", operator="op", idempotency_key="k",
            )
        session.delete.assert_not_called()
        session.flush.assert_not_called()
        assert "publication_withdrawal" not in run.metadata_json

    async def test_expected_publication_id_mismatch_is_zero_write(self):
        run = _make_run(status="published", published_at=datetime.now(UTC))
        pub = _make_pointer(run.id)
        session = _make_session([_FakeResult(scalar=pub)])
        session.get = AsyncMock(return_value=run)
        with pytest.raises(ReviewWithdrawalBlockError, match="expected_publication_id"):
            await withdraw_review_publication(
                session, date(2026, 7, 31),
                expected_run_id=run.id,
                expected_publication_id=uuid.uuid4(),
                reason="mismatch", operator="op", idempotency_key="k",
            )
        session.delete.assert_not_called()
        session.flush.assert_not_called()
        assert "publication_withdrawal" not in run.metadata_json

    async def test_missing_run_is_zero_write(self):
        pub = _make_pointer(uuid.uuid4())
        session = _make_session([_FakeResult(scalar=pub)])
        session.get = AsyncMock(return_value=None)
        with pytest.raises(ReviewWithdrawalBlockError, match="MarketReviewRun"):
            await withdraw_review_publication(
                session, date(2026, 7, 31),
                expected_run_id=pub.data_run_id,
                expected_publication_id=pub.id,
                reason="missing run", operator="op", idempotency_key="k",
            )
        session.delete.assert_not_called()
        session.flush.assert_not_called()

    async def test_wrong_scope_or_kind_is_zero_write(self):
        run = _make_run(status="published", published_at=datetime.now(UTC))
        pub = _make_pointer(run.id)
        pub.scope_key = "instrument"
        pub.publication_kind = "stock_core"
        session = _make_session([_FakeResult(scalar=pub)])
        session.get = AsyncMock(return_value=run)
        with pytest.raises(ReviewWithdrawalBlockError) as exc_info:
            await withdraw_review_publication(
                session, date(2026, 7, 31),
                **_withdraw_kwargs(run, pub),
            )
        assert "scope_key" in str(exc_info.value)
        assert "publication_kind" in str(exc_info.value)
        session.delete.assert_not_called()
        session.flush.assert_not_called()

    async def test_concurrent_withdrawal_second_call_is_idempotent(self):
        run = _make_run(status="published", published_at=datetime.now(UTC))
        pub = _make_pointer(run.id)
        first = _make_session([_FakeResult(scalar=pub)])
        first.get = AsyncMock(return_value=run)
        first_result = await withdraw_review_publication(
            first, date(2026, 7, 31), **_withdraw_kwargs(run, pub),
        )
        assert first_result["withdrawn"] is True

        second = _make_session([_FakeResult(scalar=None)])
        second_result = await withdraw_review_publication(
            second, date(2026, 7, 31), **_withdraw_kwargs(run, pub),
        )
        assert second_result["already_withdrawn"] is True
        assert second.delete.call_count == 0

    async def test_withdraw_requires_audit_fields(self):
        session = _make_session([])
        with pytest.raises(ValueError):
            await withdraw_review_publication(
                session, date(2026, 7, 31),
                expected_run_id=uuid.uuid4(),
                expected_publication_id=uuid.uuid4(),
                reason="", operator="op", idempotency_key="k",
            )
