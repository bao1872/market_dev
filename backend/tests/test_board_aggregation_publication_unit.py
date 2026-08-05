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
from datetime import date
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
