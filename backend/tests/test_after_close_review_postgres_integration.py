"""真实 PostgreSQL Review 发布链路集成合同。

该模块与 ``test_after_close_orchestrator.py`` 的主编排隔离测试分开：
不 mock ``create_run`` 或 ``publish_run``，而是使用真实临时 PostgreSQL
持久化 stock_core/Board pointer、Review run、scope snapshot 和 Review pointer。

本地按仓库规则只允许纯单元测试；因此 ``PURE_UNIT_TEST=1`` 时整个模块跳过。
CI PostgreSQL Integration job 会在临时库中执行本模块。
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.board_analysis_snapshot import BoardAnalysisRun
from app.models.factor_publication import (
    PUBLICATION_KIND_MARKET_AGGREGATION,
    PUBLICATION_KIND_STOCK_CORE,
    FactorPublication,
)
from app.models.market_review import (
    MarketReviewRun,
    MarketReviewRunItem,
    MarketReviewScopeSnapshot,
)
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.services.review_orchestrator_service import (
    ITEM_FAILED,
    PHASE_METRICS,
    REVIEW_FILTER_VERSION,
    compute_run,
    create_run,
    publish_run,
    resume_run,
)
from app.services.review_publication_service import (
    PUBLICATION_KIND_MARKET_REVIEW,
    ReviewPublishBlockError,
    ReviewWithdrawalBlockError,
    get_published_review_run_id,
    is_formally_published_review_run,
    withdraw_review_publication,
)
from app.services.review_scope_service import ScopeDefinition

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("PURE_UNIT_TEST", "").lower() in {"1", "true", "yes"},
        reason="Review PostgreSQL integration only runs in CI temporary Postgres",
    ),
]

TRADE_DATE = date(2026, 7, 31)
OPERATOR = "integration-test:review"


def _ready_payload(value: float = 1.0) -> dict[str, object]:
    return {
        "value": value,
        "rawValue": value,
        "readiness": {
            "raw_ready": True,
            "normalized_ready": True,
            "reason": None,
        },
        "components": [],
    }


def _blocked_payload() -> dict[str, object]:
    return {
        "value": 1.0,
        "rawValue": 1.0,
        "readiness": {
            "raw_ready": True,
            "normalized_ready": False,
            "reason": "integration_insufficient_history",
        },
        "components": [],
    }


async def _seed_published_inputs(
    session: AsyncSession,
    *,
    trade_date: date,
) -> tuple[uuid.UUID, uuid.UUID, FactorPublication, FactorPublication]:
    """创建真实 stock_core 与 Board batch，并切换其正式 pointer。"""
    now = datetime.now(UTC)
    core_id = uuid.uuid4()
    board_id = uuid.uuid4()

    core_run = StockFeatureSnapshotRun(
        id=core_id,
        trade_date=trade_date,
        run_type="after_close",
        status="succeeded",
        expected_count=1,
        snapshot_count=1,
        failed_count=0,
        skipped_count=0,
        failure_rate=0.0,
        started_at=now,
        finished_at=now,
        published_at=now,
    )
    board_run = BoardAnalysisRun(
        id=board_id,
        trade_date=trade_date,
        source_core_run_id=core_id,
        taxonomy_version="integration-taxonomy-v1",
        taxonomy_compatibility_key="integration-taxonomy-compatible-v1",
        membership_version="integration-membership-v1",
        algorithm_version="board-integration-v1",
        expected_count=1,
        succeeded_count=1,
        failed_count=0,
        coverage_ratio=1.0,
        status="succeeded",
        published_at=now,
    )
    session.add_all([core_run, board_run])
    await session.flush()

    core_pointer = FactorPublication(
        scope_type="market",
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_STOCK_CORE,
        algorithm_version="core-integration-v1",
        data_run_id=core_id,
        coverage_ratio=1.0,
        published_at=now,
        metadata_json=json.dumps({"source": "integration"}),
    )
    board_pointer = FactorPublication(
        scope_type="market",
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_MARKET_AGGREGATION,
        algorithm_version="board-integration-v1",
        data_run_id=board_id,
        coverage_ratio=1.0,
        published_at=now,
        metadata_json=json.dumps({"board_analysis_run_id": str(board_id)}),
    )
    session.add_all([core_pointer, board_pointer])
    await session.flush()
    return core_id, board_id, core_pointer, board_pointer


async def _add_scope_snapshot(
    session: AsyncSession,
    run: MarketReviewRun,
    *,
    blocked: bool = False,
) -> None:
    payload_factory = _blocked_payload if blocked else _ready_payload
    payloads = {key: payload_factory() for key in ("p", "q", "u", "c", "v")}
    status = "insufficient_history" if blocked else "ready"
    session.add_all(
        [
            MarketReviewScopeSnapshot(
                review_run_id=run.id,
                trade_date=run.trade_date,
                scope_type="market",
                scope_key="market",
                scope_name="全市场",
                eligible_count=1,
                ready_count=1,
                coverage_ratio=Decimal("1"),
                status=status,
                p_payload=payloads["p"],
                q_payload=payloads["q"],
                u_payload=payloads["u"],
                c_payload=payloads["c"],
                v_payload=payloads["v"],
            ),
            # Gate 要求至少一个真实 industry_l1 scope；集成 fixture 不需要
            # 依赖 market_boards/member population，只保存该层的 ready 事实。
            MarketReviewScopeSnapshot(
                review_run_id=run.id,
                trade_date=run.trade_date,
                scope_type="industry_l1",
                scope_key="integration-industry-l1",
                scope_name="集成行业",
                eligible_count=1,
                ready_count=1,
                coverage_ratio=Decimal("1"),
                status=status,
                p_payload=payloads["p"],
                q_payload=payloads["q"],
                u_payload=payloads["u"],
                c_payload=payloads["c"],
                v_payload=payloads["v"],
            ),
        ],
    )
    run.expected_scope_count = 2
    run.succeeded_scope_count = 2
    run.failed_scope_count = 0
    run.coverage_ratio = Decimal("1")
    run.status = "signals_ready"
    await session.flush()


async def _make_review_run(
    session: AsyncSession,
    *,
    trade_date: date = TRADE_DATE,
    algorithm_version: str | None = None,
) -> MarketReviewRun:
    return await create_run(
        session,
        trade_date=trade_date,
        algorithm_version=algorithm_version,
        filter_version=REVIEW_FILTER_VERSION,
        idempotency_key=f"integration:{trade_date}:{algorithm_version or 'default'}",
    )


async def test_real_after_close_review_flow_gate_publish_reuse_withdraw_and_force(
    db_session: AsyncSession,
) -> None:
    """真实 DB 覆盖 after-close 使用的 Review create/compute/publish 合同。

    compute/resume 只替换最底层成员解析为合法空范围，避免集成测试依赖外部
    行情成员 population；create_run、compute_run、resume_run、publish_run、
    evaluate gate 和 withdrawal 均使用真实实现并真实写入临时 PostgreSQL。
    """
    core_id, board_id, core_pointer, board_pointer = await _seed_published_inputs(
        db_session,
        trade_date=TRADE_DATE,
    )
    run = await _make_review_run(db_session)
    assert run.source_core_run_id == core_id
    assert run.source_board_run_id == board_id

    # 真实 compute/resume：只把成员查询替换为 empty population，仍让
    # orchestrator 写入 run item、更新状态和 coverage。
    with patch(
        "app.services.review_orchestrator_service._resolve_level1_scopes",
        new=AsyncMock(return_value=[ScopeDefinition("market", "market", "全市场")]),
    ), patch(
        "app.services.review_orchestrator_service.resolve_scope_members",
        new=AsyncMock(return_value=([], "全市场")),
    ):
        compute_result = await compute_run(db_session, run)
        assert compute_result["status"] == "signals_ready"
        assert compute_result["expected_scope_count"] == 1

        metrics_item = await db_session.scalar(
            select(MarketReviewRunItem).where(
                MarketReviewRunItem.review_run_id == run.id,
                MarketReviewRunItem.phase == PHASE_METRICS,
            ),
        )
        assert metrics_item is not None
        metrics_item.status = ITEM_FAILED
        await db_session.flush()

        resume_result = await resume_run(db_session, run)
        assert resume_result["resumed_scopes"] == 1
        assert resume_result["status"] == "signals_ready"

    # 先验证 gate_blocked 路径，不写 Review pointer。
    await _add_scope_snapshot(db_session, run, blocked=True)
    with pytest.raises(ReviewPublishBlockError):
        await publish_run(db_session, run, force=False)
    blocked_pointer = await db_session.scalar(
        select(FactorPublication).where(
            FactorPublication.trade_date == TRADE_DATE,
            FactorPublication.publication_kind == PUBLICATION_KIND_MARKET_REVIEW,
        ),
    )
    assert blocked_pointer is None

    # 更新为同一 run 的 normalized-ready 数据后正式发布。
    await db_session.execute(
        select(MarketReviewScopeSnapshot).where(
            MarketReviewScopeSnapshot.review_run_id == run.id,
        ),
    )
    await db_session.delete(
        await db_session.scalar(
            select(MarketReviewScopeSnapshot).where(
                MarketReviewScopeSnapshot.review_run_id == run.id,
                MarketReviewScopeSnapshot.scope_type == "market",
            ),
        ),
    )
    await db_session.delete(
        await db_session.scalar(
            select(MarketReviewScopeSnapshot).where(
                MarketReviewScopeSnapshot.review_run_id == run.id,
                MarketReviewScopeSnapshot.scope_type == "industry_l1",
            ),
        ),
    )
    await db_session.flush()
    await _add_scope_snapshot(db_session, run, blocked=False)

    publication, blockers = await publish_run(db_session, run, force=False)
    assert publication is not None
    assert blockers == []
    assert publication.data_run_id == run.id
    assert run.status == "published"
    assert run.published_at is not None

    live_run_id = await get_published_review_run_id(db_session, TRADE_DATE)
    assert live_run_id == run.id
    assert is_formally_published_review_run(run, live_run_id)

    # 真实 guarded withdrawal：pointer 删除、run/子数据保留，随后旧 run
    # 不再满足 pointer-aware reuse 条件。
    withdrawal = await withdraw_review_publication(
        db_session,
        TRADE_DATE,
        expected_run_id=run.id,
        expected_publication_id=publication.id,
        reason="integration withdrawal",
        operator=OPERATOR,
        idempotency_key=f"integration-withdraw:{run.id}",
        dry_run=False,
    )
    assert withdrawal["withdrawn"] is True
    assert withdrawal["run_preserved"] is True
    assert await get_published_review_run_id(db_session, TRADE_DATE) is None
    assert not is_formally_published_review_run(run, None)
    preserved_run = await db_session.get(MarketReviewRun, run.id)
    assert preserved_run is not None
    assert preserved_run.status == "published"
    assert preserved_run.published_at is not None
    assert preserved_run.metadata_json["publication_withdrawal"]["operator"] == OPERATOR

    repeated = await withdraw_review_publication(
        db_session,
        TRADE_DATE,
        expected_run_id=run.id,
        expected_publication_id=publication.id,
        reason="integration repeated withdrawal",
        operator=OPERATOR,
        idempotency_key=f"integration-withdraw:{run.id}",
        dry_run=False,
    )
    assert repeated["already_withdrawn"] is True
    assert repeated["withdrawn"] is False

    # 生产约束之外的其他日期/kind pointer 不受影响。
    other_pointer = FactorPublication(
        scope_type="market",
        scope_key="market",
        trade_date=date(2026, 8, 1),
        publication_kind=PUBLICATION_KIND_STOCK_CORE,
        algorithm_version="other",
        data_run_id=core_id,
        coverage_ratio=1.0,
        published_at=datetime.now(UTC),
    )
    db_session.add(other_pointer)
    await db_session.flush()
    assert await db_session.get(FactorPublication, other_pointer.id) is not None
    assert await db_session.get(FactorPublication, core_pointer.id) is not None
    assert await db_session.get(FactorPublication, board_pointer.id) is not None

    # force 只落 provisional metadata，不写正式 Review pointer。
    force_run = await _make_review_run(
        db_session,
        trade_date=TRADE_DATE,
        algorithm_version="review-integration-force-v1",
    )
    force_publication, _ = await publish_run(
        db_session,
        force_run,
        force=True,
        operator=OPERATOR,
        idempotency_key=f"integration-force:{force_run.id}",
    )
    assert force_publication is None
    assert force_run.published_at is None
    assert force_run.status == "created"
    assert force_run.metadata_json["provisional_publication"]["is_provisional"] is True
    assert await get_published_review_run_id(db_session, TRADE_DATE) is None


async def test_real_review_guard_rejects_expected_pointer_mismatch_and_missing_run(
    db_session: AsyncSession,
) -> None:
    """真实 PostgreSQL 验证 withdrawal guard 失败时零写入。"""
    _core_id, _board_id, _core_pointer, _board_pointer = await _seed_published_inputs(
        db_session,
        trade_date=TRADE_DATE,
    )
    run = await _make_review_run(db_session)
    await _add_scope_snapshot(db_session, run, blocked=False)
    publication, _ = await publish_run(db_session, run)
    assert publication is not None

    before_meta = dict(run.metadata_json or {})
    with pytest.raises(ReviewWithdrawalBlockError):
        await withdraw_review_publication(
            db_session,
            TRADE_DATE,
            expected_run_id=uuid.uuid4(),
            expected_publication_id=publication.id,
            reason="wrong run",
            operator=OPERATOR,
            idempotency_key="integration-wrong-run",
        )
    assert await db_session.get(FactorPublication, publication.id) is not None
    assert run.metadata_json == before_meta

    with pytest.raises(ReviewWithdrawalBlockError):
        await withdraw_review_publication(
            db_session,
            TRADE_DATE,
            expected_run_id=run.id,
            expected_publication_id=uuid.uuid4(),
            reason="wrong publication",
            operator=OPERATOR,
            idempotency_key="integration-wrong-publication",
        )
    assert await db_session.get(FactorPublication, publication.id) is not None
    assert run.metadata_json == before_meta

    # pointer 指向不存在的 run 时也必须拒绝，不能先删 pointer。
    ghost_run = uuid.uuid4()
    publication.data_run_id = ghost_run
    await db_session.flush()
    with pytest.raises(ReviewWithdrawalBlockError):
        await withdraw_review_publication(
            db_session,
            TRADE_DATE,
            expected_run_id=ghost_run,
            expected_publication_id=publication.id,
            reason="missing run",
            operator=OPERATOR,
            idempotency_key="integration-missing-run",
        )
    assert await db_session.get(FactorPublication, publication.id) is not None


async def test_real_review_transaction_rolls_back_pointer_and_audit(
    db_session: AsyncSession,
) -> None:
    """真实 PostgreSQL 验证事务异常不会留下 pointer 或审计写入。"""
    _core_id, _board_id, _core_pointer, _board_pointer = await _seed_published_inputs(
        db_session,
        trade_date=TRADE_DATE,
    )
    run = await _make_review_run(db_session)
    await _add_scope_snapshot(db_session, run, blocked=False)
    publication, _ = await publish_run(db_session, run)
    assert publication is not None

    with pytest.raises(RuntimeError):
        async with db_session.begin_nested():
            await withdraw_review_publication(
                db_session,
                TRADE_DATE,
                expected_run_id=run.id,
                expected_publication_id=publication.id,
                reason="rollback test",
                operator=OPERATOR,
                idempotency_key="integration-rollback",
            )
            raise RuntimeError("force rollback")

    assert await db_session.get(FactorPublication, publication.id) is not None
    persisted_run = await db_session.get(MarketReviewRun, run.id)
    assert persisted_run is not None
    assert "publication_withdrawal" not in (persisted_run.metadata_json or {})


# ``asyncio`` is intentionally imported above so this file keeps a direct
# dependency on the event-loop contract used by the CI integration fixture.
assert asyncio.iscoroutinefunction(test_real_after_close_review_flow_gate_publish_reuse_withdraw_and_force)
