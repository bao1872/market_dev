"""[P0 安全收口 2026-08-01] Review 发布安全合同测试。

[REVIEW-CANONICAL-RUNTIME-REPLACEMENT] 发布门禁已迁移为**只消费 canonical
composition readiness**（``run.metadata_json["canonical_composition_readiness"]``），
不再读取 legacy ``MarketReviewScopeSnapshot`` / P/Q/U/C/V ``normalized_ready``。

本文件验证以下不变量（PRD §23.5 / §23.5A / REVIEW-CANONICAL-RUNTIME-REPLACEMENT）：
1. force=True 只生成 provisional 标记：不写 factor_publications、
   run 不进入 published、published_at 不写入、metadata 记录审计字段；
2. provisional run 无正式 pointer，普通用户读取入口（dates/latest/
   overview/scopes 共用的 _get_published_run）返回 404；
3. admin 通过 include_partial=true 可读取同一 run；
4. 门禁通过的正式 run 仍可正常发布（写 pointer、status=published）；
5. withdrawal 幂等且不删除 run：只删 pointer，run 状态和发布时间保持不变；
6. after-close 只按当前正式 pointer 复用 published run；
7. [canonical gate] 空壳 run（无 canonical composition readiness）禁止发布；
   任一 activated scope readiness 非 ready → canonical 数据缺口，禁止发布；
   market/major_index/style 非激活家族合法跳过（不出现在 readiness），不阻塞；
   UNEXPECTED_EXECUTION_FAILURE（failed/pending/running item）仍阻塞；
   [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] 发布门禁直接校验 Review 显式绑定的
   CoreRun（StockFeatureSnapshotRun）完整性（run 存在 + trade_date 一致 +
   status==succeeded），**不查 stock_core FactorPublication pointer**；
   [QM-63] 未来 observation 硬门仍生效。

测试环境：纯单元测试（mock AsyncSession），无需数据库：
    PURE_UNIT_TEST=1 python -m pytest tests/test_review_publication_safety.py -v
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects.postgresql.dml import Insert as PgInsert

from app.domain.review.versions import REVIEW_ALGORITHM_VERSION
from app.models.market_review import MarketReviewRun
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


def _make_session(
    execute_results: list[_FakeResult] | tuple[list[_FakeResult], object | None],
    *,
    core_run: object | None = None,
) -> AsyncMock:
    """构造 mock AsyncSession，execute 按序返回预置结果。

    [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] 第一个参数可直接传
    ``_gate_pass_results`` / ``_gate_fail_results`` 返回的
    ``(execute_results, core_run)`` tuple，自动解包；也可显式传
    ``execute_results`` + ``core_run=``。

    evaluate_publish_gate 直接通过 ``session.get(StockFeatureSnapshotRun,
    run.source_core_run_id)`` 校验 CoreRun 完整性（不查 stock_core
    FactorPublication pointer）。mock 在此对 StockFeatureSnapshotRun 返回
    ``core_run``，其余 model 返回 None。
    """
    if isinstance(execute_results, tuple):
        execute_results, core_run = execute_results
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _get(model, ident):
        if model is StockFeatureSnapshotRun:
            return core_run
        return None

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=execute_results)
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    session.get = _get
    return session


def _executed_statements(session: AsyncMock) -> list:
    return [c.args[0] for c in session.execute.call_args_list]


# [Slice 4A5R] 哨兵：区分「调用方未传 source_board_run_id」（应退回到随机的
# legacy lineage）与「调用方显式传 None」（真 JSON NULL canonical lineage）。
_UNSET_BOARD_RUN_ID = object()


def _make_run(
    *,
    status: str = "signals_ready",
    published_at: datetime | None = None,
    composition_readiness: dict | None = None,
    source_board_run_id: uuid.UUID | None | object = _UNSET_BOARD_RUN_ID,
) -> MarketReviewRun:
    # [Slice 4A5R] 只有哨兵（未传）才退回到随机 legacy UUID；显式 None 保持 NULL。
    resolved_board_run_id = uuid.uuid4() if source_board_run_id is _UNSET_BOARD_RUN_ID else source_board_run_id
    run = MarketReviewRun(
        id=uuid.uuid4(),
        trade_date=date(2026, 7, 31),
        source_core_run_id=uuid.uuid4(),
        source_board_run_id=resolved_board_run_id,
        algorithm_version=REVIEW_ALGORITHM_VERSION,
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
    # [REVIEW-CANONICAL-RUNTIME-REPLACEMENT] canonical gate 的唯一 readiness
    # 数据源。None → 空 dict（空壳 run，禁止发布）；传值则写全 ready 或指定状态。
    if composition_readiness:
        run.metadata_json["canonical_composition_readiness"] = composition_readiness
    return run


def _ready_readiness() -> dict:
    """单一 activated scope 的 ready composition readiness。"""
    return {str(uuid.uuid4()): "ready"}


def _make_core_run(
    *, trade_date: date | None = None, status: str = "succeeded",
) -> AsyncMock:
    """构造 evaluate_publish_gate 校验通过的 CoreRun 行（StockFeatureSnapshotRun）。

    [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] 发布门禁直接校验 CoreRun 完整性：
    run 存在 + trade_date 一致 + status==succeeded（compute-complete），
    **不查 stock_core FactorPublication pointer**。
    """
    core = AsyncMock()
    core.trade_date = trade_date
    core.status = status
    return core


def _gate_pass_results(
    run: MarketReviewRun,
    *,
    live_review_pointer: object | None = None,
    future_obs_count: int = 0,
) -> tuple[list[_FakeResult], AsyncMock]:
    """构造 evaluate_publish_gate 全部通过所需的查询结果 + CoreRun 行。

    [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] 新门禁查询顺序（与实现严格一致）：
        1 incomplete run items（failed/pending/running）
        2 session.get(StockFeatureSnapshotRun, source_core_run_id) → 合法 CoreRun
        [仅 run.status == "published"] 3 live review pointer
        末位 future_obs count（[QM-63] 无未来数据硬门）。
    """
    results = [
        _FakeResult(scalar_list=[]),            # 1. incomplete run items
    ]
    core_run = _make_core_run(trade_date=run.trade_date, status="succeeded")
    if run.status == "published":
        results.append(_FakeResult(scalar=live_review_pointer))
    results.append(_FakeResult(scalar=future_obs_count))    # future_obs count
    return results, core_run


def _gate_fail_results() -> tuple[list[_FakeResult], None]:
    """门禁失败的查询（CoreRun 不存在 + 无 future data）。

    与 `_gate_pass_results` 保持相同查询顺序；CoreRun 返回 None →
    触发「CoreRun 不存在」blocker。
    """
    results = [
        _FakeResult(scalar_list=[]),    # incomplete run items
        _FakeResult(scalar=0),          # future_obs count
    ]
    return results, None


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
        # 门禁 blockers 必须记录（本用例：空壳 run 无 canonical readiness +
        # core/board pointer 缺失）
        assert len(record["gate_blockers"]) >= 2


class TestFormalGateCompleteness:
    async def test_empty_shell_run_without_readiness_is_closed(self):
        """[REVIEW-CANONICAL-RUNTIME-REPLACEMENT] 空壳 run（无任何 canonical
        composition readiness）禁止发布（fail-closed，禁止空壳上市，绝不回退
        legacy P/Q/U/C/V）。"""
        run = _make_run(composition_readiness={})  # 空 dict → 未写入任何 canonical fact
        publishable, blockers = await evaluate_publish_gate(
            _make_session(_gate_pass_results(run)), run,
        )
        assert publishable is False
        assert any("canonical composition readiness" in b for b in blockers)

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

    async def test_non_activated_families_absent_from_readiness_are_legal_skip(
        self,
    ):
        """[REVIEW-CANONICAL-RUNTIME-REPLACEMENT] market/major_index/style 是
        ScopeCapability 非激活家族（不出现在 composition_readiness，也绝不回退
        legacy P/Q/U/C/V）。activated 家族（industry_l1）全 ready → 门禁 OPEN。
        """
        run = _make_run(composition_readiness={
            "industry_l1:board-a": "ready",
        })
        publishable, blockers = await evaluate_publish_gate(
            _make_session(_gate_pass_results(run)), run,
        )
        assert publishable is True
        # 非激活家族不产生任何 blocker（implementation gap 不是保留 legacy 的理由）
        assert not any("market" in b for b in blockers)
        assert not any("P/Q/U/C/V" in b for b in blockers)

    async def test_core_run_required_not_stock_core_pointer(self):
        """[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] 发布门禁直接校验 CoreRun 完整性，
        不再要求正式 stock_core FactorPublication pointer；board / market_aggregation
        pointer 更不参与门禁。CoreRun 不存在 → blocker。"""
        run = _make_run()
        publishable, blockers = await evaluate_publish_gate(
            _make_session(_gate_fail_results()), run,
        )
        assert publishable is False
        assert any("CoreRun 不存在" in b for b in blockers)
        assert not any("正式 stock_core pointer" in b for b in blockers)
        assert not any("board pointer" in b for b in blockers)

    async def test_superseded_published_run_cannot_overwrite_live_pointer(self):
        run = _make_run(
            status="published",
            published_at=datetime.now(UTC),
            composition_readiness=_ready_readiness(),
        )
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
        run = _make_run(composition_readiness=_ready_readiness())
        pub = AsyncMock()
        pub.id = uuid.uuid4()
        gate_results, core_run = _gate_pass_results(run)
        session = _make_session(
            [
                *gate_results,
                _FakeResult(),              # pointer upsert
                _FakeResult(scalar=pub),    # 发布成功后回读 pointer
            ],
            core_run=core_run,
        )

        result = await publish_review(session, run, force=False)

        assert result is pub
        inserts = [
            s for s in _executed_statements(session)
            if isinstance(s, PgInsert)
        ]
        assert len(inserts) == 1, "正式发布必须写且只写一次 pointer"
        # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] 发布只写 market_review pointer，
        # stock_core publication 已从 Core→Review 主链旁路，故不再产生 stock_core
        # pointer 写入。下方仅校验写入数量，for-update 锁语义由集成测试覆盖。
        assert run.status == "published"
        assert run.published_at is not None
        assert "provisional_publication" not in (run.metadata_json or {})

    async def test_current_published_run_is_a_zero_write_idempotent_replay(self):
        published_at = datetime.now(UTC)
        run = _make_run(
            status="published", published_at=published_at,
            composition_readiness=_ready_readiness(),
        )
        pointer = _make_pointer(run.id)
        gate_results, core_run = _gate_pass_results(run, live_review_pointer=pointer)
        gate_results.append(_FakeResult(scalar=pointer))  # idempotent return under lock
        session = _make_session(gate_results, core_run=core_run)

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


class TestProgressiveScopeReadiness:
    """[REVIEW-CANONICAL-RUNTIME-REPLACEMENT] Progressive scope 语义（canonical gate）。

    新 Review 权威链把 market / major_index / style / industry_l1 / industry_l2 /
    industry_l3 / concept 全部视为平行 Scope Family。发布门禁只消费 canonical
    composition readiness（``run.metadata_json["canonical_composition_readiness"]``）：

    - activated 家族（industry_l1/l2/l3/concept）：每个已产生 canonical fact 的
      scope 的 readiness 必须为 ready；任一非 ready（unavailable_current /
      insufficient_history）→ canonical 数据缺口 → CLOSED（fail-closed，绝不
      回退 legacy P/Q/U/C/V）。
    - 非激活家族（market / major_index / style）：ScopeCapability 非激活，
      不出现在 composition_readiness；缺失/不可用是 implementation capability
      的合法跳过，不阻塞，也绝不回退 P/Q/U/C/V（market 历史 PIT 缺口是
      implementation gap，不是保留 legacy 的理由）。
    - UNEXPECTED_EXECUTION_FAILURE 仍阻塞：任何 run item 处于 failed/pending/running。
    - skipped（PIT unavailable / 空成员）是诊断性终态，不阻塞。
    """

    def _results(
        self,
        run: MarketReviewRun,
        *,
        items: list | None = None,
        future_obs: int = 0,
    ) -> tuple[list[_FakeResult], AsyncMock]:
        """canonical gate 查询结果（非 published run 共 2 次 execute + 1 次 CoreRun get）。

        [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] 新门禁顺序（与实现严格一致）：
            execute: incomplete items / future_obs count
            session.get(StockFeatureSnapshotRun): CoreRun 行（直接校验完整性，
            不查 stock_core FactorPublication pointer）。
        """
        core_run = _make_core_run(trade_date=run.trade_date, status="succeeded")
        return (
            [
                _FakeResult(scalar_list=items if items is not None else []),
                _FakeResult(scalar=future_obs),
            ],
            core_run,
        )

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

    # -------------------------------------------------------------------------
    # activated 家族：readiness 必须 ready（canonical 数据缺口 fail-closed）
    # -------------------------------------------------------------------------

    @pytest.mark.parametrize(
        "activated",
        ["industry_l1", "industry_l2", "industry_l3", "concept"],
    )
    async def test_activated_family_present_not_ready_closes_gate(
        self, activated: str,
    ):
        """activated 家族任一 readiness 非 ready（unavailable_current）→ CLOSED。

        覆盖 industry_l1/l2/l3/concept 全家族测试矩阵。
        """
        run = _make_run(composition_readiness={
            f"{activated}:scope-1": "unavailable_current",
        })
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results(run)), run,
        )
        assert publishable is False
        assert any("非 ready" in b for b in blockers)
        # fail-closed：是 canonical 数据缺口 blocker，不是回退 legacy
        assert not any("P/Q/U/C/V" in b for b in blockers)

    @pytest.mark.parametrize(
        "activated",
        ["industry_l1", "industry_l2", "industry_l3", "concept"],
    )
    async def test_activated_family_insufficient_history_closes_gate(
        self, activated: str,
    ):
        """activated 家族 readiness = insufficient_history → CLOSED。"""
        run = _make_run(composition_readiness={
            f"{activated}:scope-1": "insufficient_history",
        })
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results(run)), run,
        )
        assert publishable is False
        assert any("非 ready" in b for b in blockers)

    async def test_activated_scopes_all_ready_opens_gate(self):
        """activated 家族全部 ready → OPEN。"""
        run = _make_run(composition_readiness={
            "board-a": "ready",
            "board-b": "ready",
            "concept-c": "ready",
        })
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results(run)), run,
        )
        assert publishable is True
        assert not any("canonical" in b for b in blockers)

    # -------------------------------------------------------------------------
    # 非激活家族：market / major_index / style 合法跳过（implementation gap ≠ BLOCK）
    # -------------------------------------------------------------------------

    @pytest.mark.parametrize("non_activated", ["market", "major_index", "style"])
    async def test_non_activated_family_absent_is_legal_skip(
        self, non_activated: str,
    ):
        """非激活家族不出现在 composition_readiness → 合法跳过，不阻塞发布。

        覆盖 market / major_index / style 全家族测试矩阵。market 历史 PIT 缺口
        是 implementation gap，不是允许 legacy P/Q/U/C/V 永久保留的理由。
        """
        run = _make_run(composition_readiness={"industry_l1:board-a": "ready"})
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results(run)), run,
        )
        assert publishable is True
        assert not any(non_activated in b for b in blockers)
        assert not any("P/Q/U/C/V" in b for b in blockers)

    # -------------------------------------------------------------------------
    # UNEXPECTED_EXECUTION_FAILURE vs 诊断性 skipped
    # -------------------------------------------------------------------------

    async def test_skipped_item_is_diagnostic_only_gate_open(self):
        """skipped（PIT unavailable / 空成员）不在 failed/pending/running 查询范围
        → 诊断性终态，不阻塞发布。"""
        run = _make_run(composition_readiness={"industry_l1:board-a": "ready"})
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results(run)), run,
        )
        assert publishable is True
        assert not any("未成功终态项" in b for b in blockers)

    async def test_failed_item_unexpected_execution_failure_gate_closed(self):
        """failed item（真实执行异常）→ CLOSED；blocker 是 execution-failure，
        而非 market mandatory（market 已无强制 gate）。"""
        run = _make_run(composition_readiness={"industry_l1:board-a": "ready"})
        failed_item = self._run_item(
            run, "failed",
            last_error="TypeError: unexpected execution error in scope metrics",
        )
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results(run, items=[failed_item])), run,
        )
        assert publishable is False
        assert any("未成功终态项" in b for b in blockers)
        assert not any("market" in b for b in blockers)

    async def test_pending_item_non_terminal_state_gate_closed(self):
        """pending item（非终态）→ CLOSED。"""
        run = _make_run(composition_readiness={"industry_l1:board-a": "ready"})
        pending_item = self._run_item(run, "pending")
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results(run, items=[pending_item])), run,
        )
        assert publishable is False
        assert any("未成功终态项" in b for b in blockers)
        assert not any("market" in b for b in blockers)

    async def test_running_item_non_terminal_state_gate_closed(self):
        """running item（非终态）→ CLOSED。"""
        run = _make_run(composition_readiness={"industry_l1:board-a": "ready"})
        running_item = self._run_item(run, "running")
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results(run, items=[running_item])), run,
        )
        assert publishable is False
        assert any("未成功终态项" in b for b in blockers)
        assert not any("market" in b for b in blockers)

    async def test_incomplete_items_query_targets_only_execution_failure_states(self):
        """门禁的 execution-failure 查询条件必须精确为 failed/pending/running。

        直接断言 SQL 语义，防止未来把 skipped 误并入 execution failure，
        或把 failed 误移出 blocker 集合（回归保护）。
        """
        run = _make_run(composition_readiness={"industry_l1:board-a": "ready"})
        session = _make_session(self._results(run))
        await evaluate_publish_gate(session, run)
        # 第 1 个 execute 即 incomplete run items 查询
        sql = str(_executed_statements(session)[0].compile(
            compile_kwargs={"literal_binds": True},
        ))
        assert "failed" in sql
        assert "pending" in sql
        assert "running" in sql
        assert "skipped" not in sql

    async def test_forbidden_legacy_fallback_contract_unchanged(self):
        """既有禁止 legacy/PIT fallback 的 contract 不变：activated scope 的非 ready
        状态不能被伪装为 ready，必须作为 canonical 数据缺口 blocker 呈现，绝不回退
        legacy P/Q/U/C/V。"""
        run = _make_run(composition_readiness={
            "industry_l1:board-a": "unavailable_current",
        })
        publishable, blockers = await evaluate_publish_gate(
            _make_session(self._results(run)), run,
        )
        assert publishable is False
        # 必须是 canonical 缺口 blocker，不得伪装 ready 或回退 legacy
        assert any("非 ready" in b for b in blockers)
        assert not any("P/Q/U/C/V" in b for b in blockers)


# =============================================================================
# Slice 4A5 Board-independent：Review 发布只依赖 stock_core pointer + canonical
# readiness，不再依赖 market_aggregation / BoardAnalysis / source_board_run_id。
# =============================================================================


class Test4A5BoardIndependentPublication:
    """[Slice 4A5 Board-independent]

    canonical Review（``source_board_run_id=None``）及其 legacy 兄弟
    （``source_board_run_id=任意 UUID``）都以同一套 CoreRun-only gate 判断发布。

    [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] 新 gate 直接通过
    ``session.get(StockFeatureSnapshotRun, source_core_run_id)`` 校验 CoreRun
    完整性（run 存在 + trade_date 一致 + status==succeeded），**不查询 stock_core
    FactorPublication pointer，也不查询 board / market_aggregation pointer**；
    因此 `_gate_pass_results` 的 execute 结果序列中不存在任何 board pointer 槽位。
    若生产实现恢复 board 查询，本组测试会因 execute 结果耗尽（mock 越界）而失败——
    这正是"不查 board"的直接证据。
    """

    async def test_canonical_run_source_board_none_publishes_without_board(
        self,
    ):
        """[4A5R-A/D] canonical run（source_board_run_id=NULL）+ 无任何 board /
        market_aggregation pointer → 门禁 OPEN。

        [4A5R] 必须先证明前置条件：显式 ``source_board_run_id=None`` 得到真
        JSON NULL lineage（哨兵保证不会退回到随机 UUID）。
        """
        run = _make_run(
            source_board_run_id=None,  # 显式 NULL → canonical 设计态
            composition_readiness={"industry_l1:board-a": "ready"},
        )
        # 前置条件：确认真 NULL，而非被 helper 换成 random UUID。
        assert run.source_board_run_id is None
        publishable, blockers = await evaluate_publish_gate(
            _make_session(_gate_pass_results(run)), run,
        )
        assert publishable is True
        assert blockers == []
        assert not any("board" in b for b in blockers)
        assert not any("market_aggregation" in b for b in blockers)

    async def test_legacy_run_arbitrary_source_board_id_publishes(self):
        """[4A5R-E] legacy Review run 只保留任意 Board lineage UUID；core pointer
        匹配 → 门禁 OPEN，Board lineage 不影响发布。

        [4A5R] 只覆盖 UUID lineage（legacy 语义），不在此参数化 None——
        NULL canonical lineage 由 `test_canonical_run_source_board_none...`
        独立证明，避免隐藏不同前置条件。
        """
        legacy_board_id = uuid.uuid4()
        run = _make_run(
            source_board_run_id=legacy_board_id,
            composition_readiness={"concept:scope-x": "ready"},
        )
        assert run.source_board_run_id == legacy_board_id
        publishable, blockers = await evaluate_publish_gate(
            _make_session(_gate_pass_results(run)), run,
        )
        assert publishable is True
        assert blockers == []
        assert not any("board" in b for b in blockers)

    async def test_board_pointer_presence_or_staleness_never_matters(self):
        """[4A5-D] Board pointer 是否存在、是否 stale，都不改变 gate 判定。

        无论 source_board_run_id 是 NULL、任取 UUID，还是与任何 board 记录不一致，
        gate 结果只由 stock_core pointer + canonical readiness 决定。这里显式
        用一个不等于任何 board UUID 的 source_board_run_id 复核不触发任何 board 比较。
        """
        run = _make_run(
            source_board_run_id=uuid.uuid4(),
            composition_readiness={"industry_l1:board-a": "ready"},
        )
        # 提供与 _gate_pass_results 完全一致的 execute 序列（不含 board 查询）。
        publishable, blockers = await evaluate_publish_gate(
            _make_session(_gate_pass_results(run)), run,
        )
        assert publishable is True
        assert not any("board" in b for b in blockers)

    async def test_core_run_not_succeeded_blocks(self):
        """[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] CoreRun 行存在但 status 非
        succeeded（compute-complete 合同未满足）→ BLOCK。

        旧合同比较 stock_core pointer.data_run_id == source_core_run_id；新合同
        直接校验 CoreRun（StockFeatureSnapshotRun）行的 compute-complete 状态。
        """
        run = _make_run(composition_readiness={"industry_l1:board-a": "ready"})
        # CoreRun 存在，但 status 非 succeeded（例如仍 running / failed）
        stale_core = _make_core_run(trade_date=run.trade_date, status="running")
        results = [
            _FakeResult(scalar_list=[]),                 # incomplete run items
            _FakeResult(scalar=0),                       # future_obs count
        ]
        publishable, blockers = await evaluate_publish_gate(
            _make_session(results, core_run=stale_core), run,
        )
        assert publishable is False
        assert any("CoreRun status" in b and "非 succeeded" in b for b in blockers)

    async def test_source_is_free_of_market_aggregation_ref_and_board_read(self):
        """[4A5-I] 生产源码契约门：

        1. review_publication_service.py 不引用 PUBLICATION_KIND_MARKET_AGGREGATION_REF
           （不存在该常量名）；
        2. evaluate_publish_gate 函数体（AST，排除 docstring）不读取
           run.source_board_run_id。
        """
        import ast
        import inspect

        import app.services.review_publication_service as svc

        module_src = inspect.getsource(svc)
        assert "PUBLICATION_KIND_MARKET_AGGREGATION_REF" not in module_src

        # 用 AST 收集函数底层真实读取到的属性名，避免误伤注释/docstring 文本。
        func_src = inspect.getsource(svc.evaluate_publish_gate)
        reads = set()

        def _walk(node):
            if isinstance(node, ast.Attribute) and node.attr == "source_board_run_id":
                reads.add("source_board_run_id")
            for child in ast.iter_child_nodes(node):
                _walk(child)

        _walk(ast.parse(func_src))
        assert "source_board_run_id" not in reads
