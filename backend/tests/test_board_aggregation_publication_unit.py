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
async def test_market_agg_rejects_non_succeeded_batch(
    mock_published_core: AsyncMock,
    mock_get_publication: AsyncMock,
) -> None:
    """Board batch 非 succeeded → ValueError（partial 不能发布 market pointer）。"""
    source_core = uuid.uuid4()
    mock_published_core.return_value = source_core
    session = AsyncMock()
    board_run = _make_board_run(
        status="partial", source_core_run_id=source_core,
        expected_count=10, succeeded_count=5, failed_count=5,
    )
    session.get.return_value = board_run

    with pytest.raises(ValueError, match="非 succeeded"):
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
async def test_market_agg_rejects_failed_items(
    mock_published_core: AsyncMock,
    mock_get_publication: AsyncMock,
) -> None:
    """Board batch 存在 failed item → ValueError。"""
    source_core = uuid.uuid4()
    mock_published_core.return_value = source_core
    session = AsyncMock()
    board_run = _make_board_run(
        status="succeeded", source_core_run_id=source_core,
        expected_count=10, succeeded_count=9, failed_count=1,
    )
    session.get.return_value = board_run

    with pytest.raises(ValueError, match="failed item"):
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
async def test_cab_pointer_mismatch_not_false_green(
    mock_get_core: AsyncMock,
    mock_get_publication: AsyncMock,
    mock_publish: AsyncMock,
) -> None:
    """现有 batch（lineage 匹配当前 core）但 live pointer 指向其它旧 run →
    不得视为 current ready，必须走正式重算路径（publish 不被直接复用调用，
    返回结果不携带 pointer_confirmed=True）。"""
    source_core = uuid.uuid4()
    mock_get_core.return_value = source_core
    batch_run = _make_existing_batch(source_core_run_id=source_core)
    # live pointer 指向一个不同的、错误的 run
    mock_get_publication.return_value = _make_core_pointer(
        data_run_id=uuid.uuid4(),
    )

    session = _make_session_with_batch(batch_run)
    # 不重算分支会落入下方正式 precompute；此处仅验证不会被错误地当作
    # confirmed reuse（即不会在不重算的情况下声称 pointer_confirmed）。
    # 由于 precompute 需要 membership / board 数据，这里用 publish=False 隔离：
    result = await board_analysis_service.compute_all_boards(
        session, _TRADE_DATE, publish=False,
    )
    # 落入 precompute 分支：idempotent_reuse 应为 False，
    # 且不会在不重算的情况下声称 pointer_confirmed。
    assert result.get("idempotent_reuse") is not True
    assert result.get("pointer_confirmed") is not True
