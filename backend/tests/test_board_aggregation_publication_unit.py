"""聚合（market aggregation）发布指针纯单元测试（Commit E 2026-08-05）。

[Commit E] Board Aggregation 合同校验：
- 基于精确 stock_core publication（exact lineage）
- 缺板块 / partial / reuse 路径的发布门禁
- industry L1/L2/L3 与 concept 分开的查询契约
- aggregation publication / pointer 原子切换

本测试为纯单元测试（PURE_UNIT_TEST），不连接数据库，使用 AsyncMock 模拟 session。
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.models.factor_publication import (
    PUBLICATION_KIND_MARKET_AGGREGATION,
    PUBLICATION_KIND_STOCK_CORE,
)
from app.services import (
    board_analysis_service,
    factor_publication_service,
)
from app.services.market_factor_aggregation_service import run_market_factor_aggregation

_TRADE_DATE = date(2026, 8, 4)


def _make_board_run(
    *,
    status: str = "succeeded",
    trade_date: date = _TRADE_DATE,
    source_core_run_id: uuid.UUID | None = None,
    expected_count: int = 10,
    succeeded_count: int = 10,
    failed_count: int = 0,
) -> Mock:
    run = Mock()
    run.id = uuid.uuid4()
    run.trade_date = trade_date
    run.source_core_run_id = source_core_run_id or uuid.uuid4()
    run.status = status
    run.expected_count = expected_count
    run.succeeded_count = succeeded_count
    run.failed_count = failed_count
    run.coverage_ratio = 1.0
    return run


def _make_core_pointer(data_run_id: uuid.UUID | None = None) -> Mock:
    pub = Mock()
    pub.data_run_id = data_run_id or uuid.uuid4()
    pub.published_at = None
    return pub


# -----------------------------------------------------------------------------
# run_market_factor_aggregation：既有 Board batch 发布路径
# -----------------------------------------------------------------------------


@patch("app.services.factor_publication_service.get_publication")
@patch("app.services.market_factor_aggregation_service.publish_market_aggregation")
async def test_run_aggregation_publishes_existing_batch_with_exact_lineage(
    mock_publish: AsyncMock,
    mock_get_publication: AsyncMock,
) -> None:
    """提供 aggregation_run_id → 校验既有 batch 并按 exact lineage 发布 pointer。"""
    source_core = uuid.uuid4()
    aggregation_run_id = uuid.uuid4()
    mock_get_publication.return_value = _make_core_pointer(data_run_id=source_core)
    mock_publish.return_value = _make_core_pointer(data_run_id=aggregation_run_id)

    session = AsyncMock()
    result = await run_market_factor_aggregation(
        session, _TRADE_DATE,
        algorithm_version="board-v1",
        aggregation_run_id=aggregation_run_id,
    )

    assert result["aggregation_run_id"] == str(aggregation_run_id)
    assert result["source_core_run_id"] == str(source_core)
    # stock_core kind 查询
    _, kwargs = mock_get_publication.call_args
    assert kwargs["publication_kind"] == PUBLICATION_KIND_STOCK_CORE
    # exact lineage 传递
    _, pub_kwargs = mock_publish.call_args
    assert pub_kwargs["source_core_run_id"] == source_core


@patch("app.services.factor_publication_service.get_publication")
async def test_run_aggregation_requires_published_core(
    mock_get_publication: AsyncMock,
) -> None:
    """无已发布 stock_core pointer → ValueError（聚合必须先于 core 发布）。"""
    mock_get_publication.return_value = None
    session = AsyncMock()

    with pytest.raises(ValueError, match="无已发布 stock_core"):
        await run_market_factor_aggregation(
            session, _TRADE_DATE,
            algorithm_version="board-v1",
            aggregation_run_id=uuid.uuid4(),
        )


@patch("app.services.board_analysis_service.compute_all_boards")
async def test_run_aggregation_full_batch_requires_publish(
    mock_compute: AsyncMock,
) -> None:
    """aggregation_run_id=None → 完整 Board batch；未通过门禁则拒绝。"""
    mock_compute.return_value = {"published": False, "status": "partial"}
    session = AsyncMock()

    with pytest.raises(ValueError, match="未通过完整性门禁"):
        await run_market_factor_aggregation(
            session, _TRADE_DATE, algorithm_version="board-v1",
        )


@patch("app.services.board_analysis_service.compute_all_boards")
async def test_run_aggregation_full_batch_published(
    mock_compute: AsyncMock,
) -> None:
    """完整 batch 发布成功 → 直接返回 compute_all_boards 结果。"""
    mock_compute.return_value = {
        "published": True,
        "board_analysis_run_id": str(uuid.uuid4()),
    }
    session = AsyncMock()
    result = await run_market_factor_aggregation(
        session, _TRADE_DATE, algorithm_version="board-v1",
    )
    assert result["published"] is True
    mock_compute.assert_awaited_once_with(
        session, _TRADE_DATE, publish=True, algorithm_version="board-v1",
    )


# -----------------------------------------------------------------------------
# publish_market_aggregation：严格 lineage 与 batch 门禁
# -----------------------------------------------------------------------------


@patch("app.services.factor_publication_service.get_publication")
@patch(
    "app.services.factor_publication_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_market_agg_lineage_mismatch_rejected(
    mock_published_core: AsyncMock,
    mock_get_publication: AsyncMock,
) -> None:
    """source_core_run_id 与已发布 stock_core pointer 不匹配 → ValueError。"""
    mock_published_core.return_value = uuid.uuid4()
    session = AsyncMock()
    board_run = _make_board_run(source_core_run_id=uuid.uuid4())
    session.get.return_value = board_run

    with pytest.raises(ValueError, match="不匹配"):
        await factor_publication_service.publish_market_aggregation(
            session, _TRADE_DATE,
            source_core_run_id=uuid.uuid4(),
            aggregation_run_id=board_run.id,
            algorithm_version="board-v1",
        )


@patch("app.services.factor_publication_service.get_publication")
@patch(
    "app.services.factor_publication_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_market_agg_succeeded_publishes(
    mock_published_core: AsyncMock,
    mock_get_publication: AsyncMock,
) -> None:
    """[CASE A] status=succeeded → publication PASS，保持原行为。"""
    source_core = uuid.uuid4()
    mock_published_core.return_value = source_core
    mock_get_publication.return_value = Mock()
    session = AsyncMock()
    board_run = _make_board_run(
        status="succeeded", source_core_run_id=source_core,
        expected_count=10, succeeded_count=10, failed_count=0,
    )
    session.get.return_value = board_run

    pub = await factor_publication_service.publish_market_aggregation(
        session, _TRADE_DATE,
        source_core_run_id=source_core,
        aggregation_run_id=board_run.id,
        algorithm_version="board-v1",
    )
    assert pub is not None
    # status 不被改写为 succeeded 之外的值（本例本来就是 succeeded）。
    assert board_run.status == "succeeded"


@patch("app.services.factor_publication_service.get_publication")
@patch(
    "app.services.factor_publication_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_market_agg_succeeded_with_failed_items_rejected(
    mock_published_core: AsyncMock,
    mock_get_publication: AsyncMock,
) -> None:
    """[CASE A2] status=succeeded 但 failed_count>0 → REJECT（state 不一致）。"""
    source_core = uuid.uuid4()
    mock_published_core.return_value = source_core
    session = AsyncMock()
    board_run = _make_board_run(
        status="succeeded", source_core_run_id=source_core,
        expected_count=10, succeeded_count=10, failed_count=1,
    )
    session.get.return_value = board_run

    with pytest.raises(ValueError, match="state 不一致"):
        await factor_publication_service.publish_market_aggregation(
            session, _TRADE_DATE,
            source_core_run_id=source_core,
            aggregation_run_id=board_run.id,
            algorithm_version="board-v1",
        )


@patch("app.services.factor_publication_service.get_publication")
@patch(
    "app.services.factor_publication_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_market_agg_succeeded_incomplete_count_rejected(
    mock_published_core: AsyncMock,
    mock_get_publication: AsyncMock,
) -> None:
    """[CASE A3] status=succeeded 但 succeeded_count != expected_count → REJECT。"""
    source_core = uuid.uuid4()
    mock_published_core.return_value = source_core
    session = AsyncMock()
    board_run = _make_board_run(
        status="succeeded", source_core_run_id=source_core,
        expected_count=10, succeeded_count=8, failed_count=0,
    )
    session.get.return_value = board_run

    with pytest.raises(ValueError, match="state 不一致"):
        await factor_publication_service.publish_market_aggregation(
            session, _TRADE_DATE,
            source_core_run_id=source_core,
            aggregation_run_id=board_run.id,
            algorithm_version="board-v1",
        )


@patch("app.services.factor_publication_service.get_publication")
@patch(
    "app.services.factor_publication_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_market_agg_partial_degraded_publishable_publishes(
    mock_published_core: AsyncMock,
    mock_get_publication: AsyncMock,
) -> None:
    """[CASE B] status=partial + degraded_publishable=True + degradation_only
    → publication PASS → pointer created → status 仍为 partial。"""
    source_core = uuid.uuid4()
    mock_published_core.return_value = source_core
    mock_get_publication.return_value = Mock()
    session = AsyncMock()
    board_run = _make_board_run(
        status="partial", source_core_run_id=source_core,
        expected_count=10, succeeded_count=9, failed_count=0,
    )
    session.get.return_value = board_run

    pub = await factor_publication_service.publish_market_aggregation(
        session, _TRADE_DATE,
        source_core_run_id=source_core,
        aggregation_run_id=board_run.id,
        algorithm_version="board-v1",
        degraded_publishable=True,
    )
    assert pub is not None
    # [PC-42] 合法 partial 被发布后，status 必须保留 partial，不得伪装 succeeded。
    assert board_run.status == "partial"


@patch("app.services.factor_publication_service.get_publication")
@patch(
    "app.services.factor_publication_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_market_agg_partial_not_degraded_rejected(
    mock_published_core: AsyncMock,
    mock_get_publication: AsyncMock,
) -> None:
    """[CASE C] status=partial + degraded_publishable=False → REJECT。"""
    source_core = uuid.uuid4()
    mock_published_core.return_value = source_core
    session = AsyncMock()
    board_run = _make_board_run(
        status="partial", source_core_run_id=source_core,
        expected_count=10, succeeded_count=5, failed_count=5,
    )
    session.get.return_value = board_run

    with pytest.raises(ValueError, match="不可发布"):
        await factor_publication_service.publish_market_aggregation(
            session, _TRADE_DATE,
            source_core_run_id=source_core,
            aggregation_run_id=board_run.id,
            algorithm_version="board-v1",
            degraded_publishable=False,
        )


@patch("app.services.factor_publication_service.get_publication")
@patch(
    "app.services.factor_publication_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_market_agg_partial_execution_failed_rejected(
    mock_published_core: AsyncMock,
    mock_get_publication: AsyncMock,
) -> None:
    """[CASE D] status=partial 但属 execution failure（无 canonical degraded
    证据）→ REJECT。"""
    source_core = uuid.uuid4()
    mock_published_core.return_value = source_core
    session = AsyncMock()
    # execution failure 的 partial：failed_count>0，但调用方不会把
    # degraded_publishable 置 True（_evaluate_degraded_publishable 已判为不可发布）。
    board_run = _make_board_run(
        status="partial", source_core_run_id=source_core,
        expected_count=10, succeeded_count=3, failed_count=7,
    )
    session.get.return_value = board_run

    with pytest.raises(ValueError, match="不可发布"):
        await factor_publication_service.publish_market_aggregation(
            session, _TRADE_DATE,
            source_core_run_id=source_core,
            aggregation_run_id=board_run.id,
            algorithm_version="board-v1",
            degraded_publishable=False,
        )


@patch("app.services.factor_publication_service.get_publication")
@patch(
    "app.services.factor_publication_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_market_agg_failed_rejected(
    mock_published_core: AsyncMock,
    mock_get_publication: AsyncMock,
) -> None:
    """[CASE E] status=failed → REJECT。"""
    source_core = uuid.uuid4()
    mock_published_core.return_value = source_core
    session = AsyncMock()
    board_run = _make_board_run(
        status="failed", source_core_run_id=source_core,
        expected_count=10, succeeded_count=0, failed_count=10,
    )
    session.get.return_value = board_run

    with pytest.raises(ValueError, match="不可发布"):
        await factor_publication_service.publish_market_aggregation(
            session, _TRADE_DATE,
            source_core_run_id=source_core,
            aggregation_run_id=board_run.id,
            algorithm_version="board-v1",
        )


# -----------------------------------------------------------------------------
# publish_board_analysis：单板块发布门禁
# -----------------------------------------------------------------------------


async def test_publish_board_analysis_requires_real_run() -> None:
    """snapshot 未绑定真实 board_analysis_run → ValueError。"""
    snapshot = Mock()
    snapshot.board_analysis_run_id = None
    session = AsyncMock()

    with pytest.raises(ValueError, match="真实 board_analysis_run"):
        await board_analysis_service.publish_board_analysis(session, snapshot)


async def test_publish_board_analysis_non_succeeded_returns_none() -> None:
    """snapshot 非 succeeded → 不发布，返回 None。"""
    snapshot = Mock()
    snapshot.board_analysis_run_id = uuid.uuid4()
    snapshot.status = "partial"
    snapshot.coverage_ratio = 0.6
    session = AsyncMock()

    result = await board_analysis_service.publish_board_analysis(session, snapshot)
    assert result is None
    session.execute.assert_not_awaited()


async def test_publish_board_analysis_coverage_below_threshold_returns_none() -> None:
    """coverage < 0.95 → 不发布，返回 None。"""
    snapshot = Mock()
    snapshot.board_analysis_run_id = uuid.uuid4()
    snapshot.status = "succeeded"
    snapshot.coverage_ratio = 0.90
    session = AsyncMock()

    result = await board_analysis_service.publish_board_analysis(session, snapshot)
    assert result is None


@patch("app.services.board_analysis_service.get_publication")
async def test_publish_board_analysis_success(mock_get_publication: AsyncMock) -> None:
    """succeeded 且 coverage 达标 → 原子 upsert 发布 pointer 并返回。"""
    pub = _make_core_pointer()
    mock_get_publication.return_value = pub
    snapshot = Mock()
    snapshot.board_id = uuid.uuid4()
    snapshot.board_analysis_run_id = uuid.uuid4()
    snapshot.trade_date = _TRADE_DATE
    snapshot.status = "succeeded"
    snapshot.coverage_ratio = 0.97
    snapshot.board_type = "industry"
    snapshot.board_name = "银行"
    snapshot.source_core_run_id = uuid.uuid4()
    snapshot.ready_count = 10
    snapshot.eligible_count = 10
    snapshot.algorithm_version = "board-v1"
    snapshot.taxonomy_version = "tax"
    snapshot.taxonomy_compatibility_key = "comp"
    snapshot.membership_version = "mem"
    session = AsyncMock()

    result = await board_analysis_service.publish_board_analysis(session, snapshot)

    assert result is pub
    session.execute.assert_awaited_once()
    session.flush.assert_awaited_once()
    _, kwargs = mock_get_publication.call_args
    assert kwargs["publication_kind"] == PUBLICATION_KIND_MARKET_AGGREGATION


# -----------------------------------------------------------------------------
# industry L1/L2/L3 与 concept 分开的查询契约
# -----------------------------------------------------------------------------


def test_board_analysis_service_exposes_industry_concept_separation() -> None:
    """list_board_analyses 支持按 board_type 分离 industry / concept。"""
    source = inspect.getsource(board_analysis_service.list_board_analyses)
    assert 'board_type in ("industry", "concept")' in source
    assert "BoardAnalysisSnapshot.board_type == board_type" in source


# -----------------------------------------------------------------------------
# [Phase 4.4.1 RB-01 收口] compute_all_boards 的 live pointer reconciliation
# 区分「historically published」(batch_run.published_at is not None) 与
# 「currently published pointer」(factor_publications market_aggregation pointer)。
# -----------------------------------------------------------------------------


def _make_session_with_batch(batch_run: Mock) -> AsyncMock:
    """构造一个 session，其 execute 按语句类型返回：
    - MarketBoard 查询 → 空列表（formal_batch=True，不触发 membership 解析）
    - run_stmt（BoardAnalysisRun）→ 给定 batch_run
    - 其它 select(func.count) → 0
    """
    session = AsyncMock()

    def _execute(stmt, *args, **kwargs):
        sql = str(stmt).lower()
        result = Mock()
        if "board_analysis_run" in sql:
            # run_stmt：热点指针对账查询，返回既有 batch_run
            result.scalar_one_or_none.return_value = batch_run
        elif "market_board" in sql:
            # MarketBoard 查询：返回空列表（formal_batch=True，不触发 membership 解析）
            result.scalars.return_value = _ScalarResult([])
        else:
            # select(func.count(...)) 等聚合 / population blocker / universe
            # 定义查询：需要既可迭代（list(...)）又具备 .first()/.all()
            result.scalars.return_value = _ScalarResult([])
            result.scalar_one.return_value = 0
            result.scalar.return_value = 0
        return result

    session.execute.side_effect = _execute
    return session


def _make_session_with_batch_none() -> AsyncMock:
    """构造一个 session：run_stmt 返回 None（没有既有 batch）→ 走正式 precompute。"""
    session = AsyncMock()

    def _execute(stmt, *args, **kwargs):  # type: ignore[no-untyped-def]
        sql = str(stmt).lower()
        result = Mock()
        if "board_analysis_run" in sql:
            # run_stmt：没有既有 batch
            result.scalar_one_or_none.return_value = None
        elif "market_board" in sql:
            result.scalars.return_value = _ScalarResult([])
        else:
            result.scalars.return_value = _ScalarResult([])
            result.scalar_one.return_value = 0
            result.scalar.return_value = 0
        return result

    session.execute.side_effect = _execute
    return session


class _ScalarResult:
    """可迭代且具备 .first()/.all() 的轻量 scalars 替身。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else 0

    def all(self):
        return list(self._rows)


def _make_existing_batch(
    *,
    status: str = "succeeded",
    source_core_run_id: uuid.UUID,
) -> Mock:
    run = Mock()
    run.id = uuid.uuid4()
    run.trade_date = _TRADE_DATE
    run.source_core_run_id = source_core_run_id
    run.status = status
    run.succeeded_count = 10
    run.failed_count = 0
    run.expected_count = 10
    run.coverage_ratio = 1.0
    run.blockers = []
    run.published_at = datetime.now(UTC)  # historically published
    return run


@patch("app.services.board_analysis_service.publish_market_aggregation")
@patch("app.services.board_analysis_service.get_publication")
@patch(
    "app.services.board_analysis_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_cab_reuse_when_live_pointer_matches(
    mock_get_core: AsyncMock,
    mock_get_publication: AsyncMock,
    mock_publish: AsyncMock,
) -> None:
    """现有 batch + live pointer 正确指向本 run + lineage 一致 → 幂等复用，
    不重新 compute（publish_market_aggregation 不被调用）。"""
    source_core = uuid.uuid4()
    mock_get_core.return_value = source_core
    batch_run = _make_existing_batch(source_core_run_id=source_core)
    mock_get_publication.return_value = _make_core_pointer(data_run_id=batch_run.id)

    session = _make_session_with_batch(batch_run)
    result = await board_analysis_service.compute_all_boards(
        session, _TRADE_DATE, publish=True,
    )

    assert result["idempotent_reuse"] is True
    assert result["pointer_confirmed"] is True
    assert result["pointer_status"] == "confirmed"
    assert result["status"] == "succeeded"
    mock_publish.assert_not_awaited(), "live pointer 已正确 → 不得重新 publish"


@patch("app.services.board_analysis_service.publish_market_aggregation")
@patch("app.services.board_analysis_service.get_publication")
@patch(
    "app.services.board_analysis_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_cab_recover_pointer_when_missing(
    mock_get_core: AsyncMock,
    mock_get_publication: AsyncMock,
    mock_publish: AsyncMock,
) -> None:
    """现有 succeeded batch + live pointer 缺失 → 只恢复 pointer，
    不重算 Board 数据（publish_market_aggregation 被调用，但不重新 compute）。"""
    source_core = uuid.uuid4()
    mock_get_core.return_value = source_core
    batch_run = _make_existing_batch(source_core_run_id=source_core)
    mock_get_publication.return_value = None  # live pointer 缺失
    # publish_market_aggregation 成功返回指向本 batch 的 publication（事实确认）
    mock_publish.return_value = _make_core_pointer(data_run_id=batch_run.id)

    session = _make_session_with_batch(batch_run)
    result = await board_analysis_service.compute_all_boards(
        session, _TRADE_DATE, publish=True,
    )

    assert result["idempotent_reuse"] is True
    assert result["pointer_recovered"] is True
    assert result["pointer_confirmed"] is True
    assert result["status"] == "succeeded"
    mock_publish.assert_awaited_once(), (
        "pointer 缺失时必须恢复 publication pointer"
    )


@patch("app.services.board_analysis_service.publish_market_aggregation")
@patch("app.services.board_analysis_service.get_publication")
@patch(
    "app.services.board_analysis_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_cab_pointer_mismatch_does_pointer_only_reconciliation(
    mock_get_core: AsyncMock,
    mock_get_publication: AsyncMock,
    mock_publish: AsyncMock,
) -> None:
    """[Phase 4.4.2 Fix 1] 现有 succeeded current-lineage batch 但 live pointer
    指向其它旧 run → **只做 pointer-only reconciliation**（publish_market_aggregation
    被调用以本 batch.id 重指 pointer），**绝不重算/修改已发布 artifact**（snapshot
    upsert 不被调用；idempotent_reuse=True 表示复用已发布 batch，不重新 compute）。"""
    source_core = uuid.uuid4()
    mock_get_core.return_value = source_core
    batch_run = _make_existing_batch(source_core_run_id=source_core)
    # live pointer 指向一个不同的、错误的 run（情况 C）
    mock_get_publication.return_value = _make_core_pointer(
        data_run_id=uuid.uuid4(),
    )
    # publish_market_aggregation 返回指向本 batch 的 publication（事实确认成功）
    mock_publish.return_value = _make_core_pointer(data_run_id=batch_run.id)

    session = _make_session_with_batch(batch_run)
    result = await board_analysis_service.compute_all_boards(
        session, _TRADE_DATE, publish=True,
    )

    # 复用已发布 batch，不重新 compute（不修改 immutable artifact）
    assert result["idempotent_reuse"] is True
    assert result["pointer_recovered"] is True
    # 只恢复 pointer 到本 batch，不重算
    mock_publish.assert_awaited_once()
    _, pub_kwargs = mock_publish.call_args
    assert pub_kwargs["aggregation_run_id"] == batch_run.id
    assert result["pointer_confirmed"] is True
    assert result["pointer_status"] == "recovered"
    assert result["status"] == "succeeded"


@patch("app.services.board_analysis_service.compute_board_analysis")
@patch("app.services.board_analysis_service.publish_market_aggregation")
@patch("app.services.board_analysis_service.get_publication")
@patch(
    "app.services.board_analysis_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_cab_wrong_pointer_never_recomputes_artifact(
    mock_get_core: AsyncMock,
    mock_get_publication: AsyncMock,
    mock_publish: AsyncMock,
    mock_compute: AsyncMock,
) -> None:
    """[Phase 4.4.3 强化] 现有 succeeded published batch + live pointer 指向错误 run →
    必须只做 pointer-only reconciliation，**直接锁定历史 published artifact 不重算**：
    patch compute_board_analysis 并断言其 **从未被调用**。不依赖空 boards fixture 间接证明。"""
    source_core = uuid.uuid4()
    mock_get_core.return_value = source_core
    batch_run = _make_existing_batch(source_core_run_id=source_core)
    mock_get_publication.return_value = _make_core_pointer(
        data_run_id=uuid.uuid4(),  # 错误的旧 run（情况 C）
    )
    mock_publish.return_value = _make_core_pointer(data_run_id=batch_run.id)

    session = _make_session_with_batch(batch_run)
    result = await board_analysis_service.compute_all_boards(
        session, _TRADE_DATE, publish=True,
    )

    # 直接断言：历史 published artifact 不被重算
    mock_compute.assert_not_awaited(), (
        "pointer-only reconciliation 不得重算历史 published artifact"
    )
    mock_publish.assert_awaited_once()
    assert result["idempotent_reuse"] is True
    assert result["pointer_confirmed"] is True


@patch("app.services.board_analysis_service.publish_market_aggregation")
@patch("app.services.board_analysis_service.get_publication")
@patch(
    "app.services.board_analysis_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_cab_pointer_reconcile_keeps_original_published_at(
    mock_get_core: AsyncMock,
    mock_get_publication: AsyncMock,
    mock_publish: AsyncMock,
) -> None:
    """[Phase 4.4.3 Fix 1] pointer-only reconciliation 不得修改历史
    batch_run.published_at：live pointer missing 与 wrong pointer 两种情况下，
    batch_run.published_at 必须保持 original_published_at 不变。"""
    source_core = uuid.uuid4()
    mock_get_core.return_value = source_core
    batch_run = _make_existing_batch(source_core_run_id=source_core)
    original_published_at = batch_run.published_at
    mock_publish.return_value = _make_core_pointer(data_run_id=batch_run.id)

    # 情形 B：live pointer 缺失
    mock_get_publication.return_value = None
    session = _make_session_with_batch(batch_run)
    await board_analysis_service.compute_all_boards(
        session, _TRADE_DATE, publish=True,
    )
    assert batch_run.published_at == original_published_at, (
        "live pointer missing reconcile 后 published_at 不应改变"
    )

    # 情形 C：live pointer 指向错误 run
    mock_get_publication.return_value = _make_core_pointer(
        data_run_id=uuid.uuid4(),
    )
    session = _make_session_with_batch(batch_run)
    await board_analysis_service.compute_all_boards(
        session, _TRADE_DATE, publish=True,
    )
    assert batch_run.published_at == original_published_at, (
        "wrong pointer reconcile 后 published_at 不应改变"
    )


@patch(
    "app.services.board_analysis_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_cab_published_but_non_succeeded_fails_closed(
    mock_get_core: AsyncMock,
) -> None:
    """[Phase 4.4.3 Fix 2 fail-closed] batch_run.published_at is not None 但
    status != 'succeeded' → 不得 fall-through 用同一 batch_run.id 重算/upsert 历史
    snapshots；必须抛出领域错误（fail-closed，留待人工治理）。"""
    source_core = uuid.uuid4()
    mock_get_core.return_value = source_core
    # 历史 partial/failed 却 published_at 非空（异常状态）
    batch_run = _make_existing_batch(
        status="partial", source_core_run_id=source_core,
    )
    session = _make_session_with_batch(batch_run)
    with pytest.raises(ValueError):
        await board_analysis_service.compute_all_boards(
            session, _TRADE_DATE, publish=True,
        )


@patch("app.services.board_analysis_service.publish_market_aggregation")
@patch("app.services.board_analysis_service.get_publication")
@patch(
    "app.services.board_analysis_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_cab_pointer_missing_publish_false_not_confirmed(
    mock_get_core: AsyncMock,
    mock_get_publication: AsyncMock,
    mock_publish: AsyncMock,
) -> None:
    """[Phase 4.4.2 Fix 2] 现有 succeeded batch + live pointer 缺失 + publish=False →
    不调用 publish_market_aggregation；pointer_recovered=False /
    pointer_confirmed=False / pointer_status="missing"。"""
    source_core = uuid.uuid4()
    mock_get_core.return_value = source_core
    batch_run = _make_existing_batch(source_core_run_id=source_core)
    mock_get_publication.return_value = None  # live pointer 缺失

    session = _make_session_with_batch(batch_run)
    result = await board_analysis_service.compute_all_boards(
        session, _TRADE_DATE, publish=False,
    )

    mock_publish.assert_not_awaited(), "publish=False 时不得调用 publish"
    assert result["idempotent_reuse"] is True
    assert result["pointer_recovered"] is False
    assert result["pointer_confirmed"] is False
    assert result["pointer_status"] == "missing"
    assert result["status"] == "succeeded"


@patch("app.services.board_analysis_service.publish_market_aggregation")
@patch("app.services.board_analysis_service.get_publication")
@patch(
    "app.services.board_analysis_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_cab_fresh_compute_publish_false_not_confirmed(
    mock_get_core: AsyncMock,
    mock_get_publication: AsyncMock,
    mock_publish: AsyncMock,
) -> None:
    """[Phase 4.4.2 Fix 2] 全新 precompute succeeded + publish=False →
    pointer_confirmed=False，且 pointer_status 不得为 "published"。"""
    source_core = uuid.uuid4()
    mock_get_core.return_value = source_core
    # 没有既有 batch：run_stmt 返回 None → 走正式 precompute
    session = _make_session_with_batch_none()
    result = await board_analysis_service.compute_all_boards(
        session, _TRADE_DATE, publish=False,
    )

    mock_publish.assert_not_awaited()
    assert result["pointer_confirmed"] is False
    assert result["pointer_status"] != "published"


@patch("app.services.board_analysis_service.publish_market_aggregation")
@patch("app.services.board_analysis_service.get_publication")
@patch(
    "app.services.board_analysis_service.get_published_snapshot_run_id",
    new_callable=AsyncMock,
)
async def test_cab_publish_pointer_data_run_mismatch_not_false_green(
    mock_get_core: AsyncMock,
    mock_get_publication: AsyncMock,
    mock_publish: AsyncMock,
) -> None:
    """[Phase 4.4.2 Fix 2] publish=True 但 publish_market_aggregation 返回的
    publication.data_run_id != batch_run.id → 事实确认失败，pointer_confirmed=False，
    不得假绿。"""
    source_core = uuid.uuid4()
    mock_get_core.return_value = source_core
    # 没有既有 batch：走正式 precompute 后发布
    session = _make_session_with_batch_none()
    # 返回的 publication 指向一个错误的 run
    mock_publish.return_value = _make_core_pointer(data_run_id=uuid.uuid4())

    result = await board_analysis_service.compute_all_boards(
        session, _TRADE_DATE, publish=True,
    )

    # precompute 成功后才发布；验证事实确认拒绝假绿
    assert result["pointer_confirmed"] is False
    assert result["pointer_status"] == "missing"
    # 不应声称 published
    assert result["pointer_status"] != "published"
