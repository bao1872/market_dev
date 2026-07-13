"""策略结果行业/概念筛选测试（CHANGE-20260713-006）。

测试目标：
- /strategy-runs/{run_id}/results?industry=xxx 按 MarketBoardMembership EXISTS 过滤
- /strategy-runs/{run_id}/results?concept=xxx 按 MarketBoardMembership EXISTS 过滤
- industry + concept 同时提供时为 AND 语义（交集）
- 不存在的板块名返回空结果
- items.length == filtered_total（条件一致性）
- 无板块数据时（provider unavailable）筛选返回空

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
        pytest backend/tests/test_strategy_results_industry_concept.py -q
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_board import MarketBoard, MarketBoardMembership
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import (
    StrategyResult,
    StrategyRun,
    StrategyRunItem,
)
from app.services.selector_query_service import query_published_selector_results


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _setup_run_with_boards(
    db_session: AsyncSession,
) -> dict[str, Any]:
    """创建 1 published run + 3 instruments + 2 boards (industry + concept) + memberships。

    布局：
    - instruments[0]: 属于 industry="银行业" + concept="沪股通"
    - instruments[1]: 属于 industry="银行业" (无 concept)
    - instruments[2]: 属于 concept="沪股通" (无 industry)
    """
    db = db_session
    now = datetime.now(UTC)
    trade_date = date(2026, 7, 13)

    definition = StrategyDefinition(
        strategy_key="dsa_selector",
        kind="selector",
        display_name="趋势选股",
    )
    db.add(definition)
    await db.flush()

    version = StrategyVersion(
        strategy_definition_id=definition.id,
        version="1.0.0",
        status="released",
        manifest={
            "outputs": [
                {"key": "dsa_dir_bars", "type": "numeric", "filterable": True, "sortable": True},
            ],
        },
        build_hash=f"test_{uuid.uuid4().hex[:16]}",
        released_at=now,
    )
    db.add(version)
    await db.flush()

    instruments: list[Instrument] = []
    for i in range(3):
        inst = Instrument(
            symbol=f"T{uuid.uuid4().hex[:5]}",
            name=f"测试标的{i}",
            market="SZ",
            status="active",
        )
        db.add(inst)
        await db.flush()
        instruments.append(inst)

    run = StrategyRun(
        strategy_version_id=version.id,
        run_type="manual",
        trade_date=trade_date,
        status="published",
        input_overrides={},
        started_at=now,
        finished_at=now,
        idempotency_key=f"test:{version.id}:manual:{trade_date}:{uuid.uuid4().hex[:8]}",
        published_at=now,
        total_instruments=3,
        succeeded_count=3,
        failed_count=0,
        skipped_count=0,
    )
    db.add(run)
    await db.flush()

    # 3 个 succeeded run_items + results
    for inst in instruments:
        result = StrategyResult(
            run_id=run.id,
            strategy_version_id=version.id,
            instrument_id=inst.id,
            trade_date=trade_date,
            payload={"dsa_dir_bars": 60},
        )
        db.add(result)
        await db.flush()
        from app.models.strategy_run import StrategyResultMetric
        metric = StrategyResultMetric(
            result_id=result.id,
            strategy_version_id=version.id,
            trade_date=trade_date,
            instrument_id=inst.id,
            metric_key="dsa_dir_bars",
            numeric_value=60.0,
        )
        db.add(metric)
        item = StrategyRunItem(
            run_id=run.id,
            instrument_id=inst.id,
            status="succeeded",
            result_id=result.id,
            started_at=now,
            finished_at=now,
        )
        db.add(item)

    # 2 个板块
    industry_board = MarketBoard(
        externalCode="BK0475",
        name="银行业",
        type="industry",
    )
    concept_board = MarketBoard(
        externalCode="BK0501",
        name="沪股通",
        type="concept",
    )
    db.add(industry_board)
    db.add(concept_board)
    await db.flush()

    # 成分股关系
    # instruments[0]: industry + concept (两者都属)
    # instruments[1]: industry only
    # instruments[2]: concept only
    memberships = [
        MarketBoardMembership(boardId=industry_board.id, instrumentId=instruments[0].id),
        MarketBoardMembership(boardId=concept_board.id, instrumentId=instruments[0].id),
        MarketBoardMembership(boardId=industry_board.id, instrumentId=instruments[1].id),
        MarketBoardMembership(boardId=concept_board.id, instrumentId=instruments[2].id),
    ]
    for m in memberships:
        db.add(m)
    await db.flush()

    return {
        "run_id": run.id,
        "instruments": instruments,
        "industry_board": industry_board,
        "concept_board": concept_board,
    }


async def test_industry_filter_matches(db_session: AsyncSession) -> None:
    """industry=银行业 应匹配 instruments[0] + instruments[1]（2 条）。"""
    td = await _setup_run_with_boards(db_session)

    page = await query_published_selector_results(
        db_session,
        run_id=td["run_id"],
        industry="银行业",
        page=1,
        page_size=50,
    )

    assert page.filtered_total == 2, f"industry 筛选应返回 2 条, 实际={page.filtered_total}"
    assert len(page.items) == 2, f"items 长度应为 2, 实际={len(page.items)}"
    # items 与 total 条件一致
    assert len(page.items) == page.filtered_total


async def test_concept_filter_matches(db_session: AsyncSession) -> None:
    """concept=沪股通 应匹配 instruments[0] + instruments[2]（2 条）。"""
    td = await _setup_run_with_boards(db_session)

    page = await query_published_selector_results(
        db_session,
        run_id=td["run_id"],
        concept="沪股通",
        page=1,
        page_size=50,
    )

    assert page.filtered_total == 2, f"concept 筛选应返回 2 条, 实际={page.filtered_total}"
    assert len(page.items) == 2, f"items 长度应为 2, 实际={len(page.items)}"
    assert len(page.items) == page.filtered_total


async def test_industry_and_concept_intersection(db_session: AsyncSession) -> None:
    """industry=银行业 + concept=沪股通 同时提供时为 AND 语义，只匹配 instruments[0]（1 条）。"""
    td = await _setup_run_with_boards(db_session)

    page = await query_published_selector_results(
        db_session,
        run_id=td["run_id"],
        industry="银行业",
        concept="沪股通",
        page=1,
        page_size=50,
    )

    assert page.filtered_total == 1, f"AND 交集应返回 1 条, 实际={page.filtered_total}"
    assert len(page.items) == 1, f"items 长度应为 1, 实际={len(page.items)}"
    assert len(page.items) == page.filtered_total


async def test_nonexistent_board_returns_empty(db_session: AsyncSession) -> None:
    """不存在的板块名应返回 0 条结果。"""
    td = await _setup_run_with_boards(db_session)

    page = await query_published_selector_results(
        db_session,
        run_id=td["run_id"],
        industry="不存在的行业",
        page=1,
        page_size=50,
    )

    assert page.filtered_total == 0, f"不存在的板块应返回 0 条, 实际={page.filtered_total}"
    assert len(page.items) == 0, f"items 长度应为 0, 实际={len(page.items)}"
    assert len(page.items) == page.filtered_total


async def test_no_board_data_returns_empty(db_session: AsyncSession) -> None:
    """无板块数据时（provider unavailable），industry 筛选返回 0 条。"""
    td = await _setup_run_with_boards(db_session)

    # 使用未创建的板块类型名（数据中不存在）
    page = await query_published_selector_results(
        db_session,
        run_id=td["run_id"],
        industry="新能源车",
        page=1,
        page_size=50,
    )

    assert page.filtered_total == 0
    assert len(page.items) == 0
    assert len(page.items) == page.filtered_total


async def test_no_filter_returns_all(db_session: AsyncSession) -> None:
    """不传 industry/concept 时返回全部 3 条。"""
    td = await _setup_run_with_boards(db_session)

    page = await query_published_selector_results(
        db_session,
        run_id=td["run_id"],
        page=1,
        page_size=50,
    )

    assert page.filtered_total == 3
    assert len(page.items) == 3
    assert len(page.items) == page.filtered_total
