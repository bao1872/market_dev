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
    """模拟 sqlalchemy Result：支持 scalar_one_or_none / scalars().all()。"""

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


def _gate_pass_results(run: MarketReviewRun) -> list[_FakeResult]:
    """构造 evaluate_publish_gate 全部通过所需的 9 次查询结果。"""
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
    return [
        _FakeResult(scalar=market_snap),          # 1. market scope
        _FakeResult(scalar_list=[]),              # 2. major_index
        _FakeResult(scalar_list=[]),              # 3. style
        _FakeResult(scalar_list=[industry_snap]),  # 4. industry_l1
        _FakeResult(scalar_list=[]),              # 5. PIT universe definitions
        _FakeResult(scalar_list=[(board_id,)]),   # 6. expected L1 industries
        _FakeResult(scalar_list=[]),              # 7. incomplete run items
        _FakeResult(scalar=core_pub),             # 8. stock_core pointer
        _FakeResult(scalar=board_pub),            # 9. board pointer
    ]


def _gate_fail_results() -> list[_FakeResult]:
    """门禁失败的查询结果（market snap 缺失 + 无行业快照）。"""
    return [
        _FakeResult(scalar=None),       # market snap 缺失 → blocker
        _FakeResult(scalar_list=[]),    # major_index
        _FakeResult(scalar_list=[]),    # style
        _FakeResult(scalar_list=[]),    # industry → blocker
        _FakeResult(scalar_list=[]),    # PIT universe definitions
        _FakeResult(scalar_list=[]),    # expected L1 industries
        _FakeResult(scalar_list=[]),    # incomplete run items
        _FakeResult(scalar=None),       # stock_core pointer
        _FakeResult(scalar=None),       # board pointer
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

    async def test_configured_universe_missing_from_run_blocks_publish(self):
        run = _make_run()
        results = _gate_pass_results(run)
        results[4] = _FakeResult(scalar_list=[SimpleNamespace(
            universe_type="major_index",
            universe_key="csi300",
            population_status="blocked_external_population",
        )])
        publishable, blockers = await evaluate_publish_gate(
            _make_session(results), run,
        )
        assert publishable is False
        assert any("major_index 配置范围缺失" in item for item in blockers)
        assert any("population 非 ready" in item for item in blockers)

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
        results = _gate_pass_results(run)
        live_review = AsyncMock()
        live_review.data_run_id = uuid.uuid4()
        results.append(_FakeResult(scalar=live_review))
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
        results = _gate_pass_results(run)
        results.append(_FakeResult(scalar=pointer))  # gate live Review pointer
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
