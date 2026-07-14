"""CHANGE-20260714-001: latest_change_pct 从 bars_daily 最新两根日线计算。

测试目标：
- published run 为 T-1、DB 最新日线为 T 时返回 T 涨跌幅
- 盘后编排未完成（仅 T-1 bar）也正确返回 T-1 涨跌幅
- 停牌/仅一根 bar/null close/prev_close=0 返回 None
- 红涨绿跌（正数=涨，负数=跌）
- sort by change_pct 使用 latest 字段
- filter by change_pct 使用 latest 字段
- filtered_total 与 items 条件一致
- 固定 SQL 数量，无 N+1

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql+psycopg://bz:bz@localhost:5433/bz_stock_test \
        pytest backend/tests/test_latest_change_pct.py -q
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import (
    StrategyResult,
    StrategyRun,
    StrategyRunItem,
)
from app.repositories.strategy_result_repository import (
    CHANGE_PCT_METRIC_KEY,
    MetricFilter,
    SortSpec,
    query_run_items_with_results,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _setup_run_with_bars(
    db_session: AsyncSession,
    *,
    bar_scenarios: list[dict[str, Any]],
) -> dict[str, Any]:
    """创建 1 published run + N instruments + 对应 bars_daily 记录。

    Args:
        bar_scenarios: 每个元素描述一个 instrument 的日线场景：
            - bars: [(trade_date, close), ...] 列表（close 可为 None）
            - payload: strategy_result payload（可为 None 表示 skipped）

    Returns:
        含 run_id/instruments/trade_date 的字典
    """
    db = db_session
    now = datetime.now(UTC)
    trade_date = date(2026, 7, 3)  # published run 交易日 T-1

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
    for i, scenario in enumerate(bar_scenarios):
        inst = Instrument(
            symbol=f"T{uuid.uuid4().hex[:5]}",
            name=f"测试标的{i}",
            market="SZ",
            status="active",
        )
        db.add(inst)
        await db.flush()
        instruments.append(inst)

        # 写入 bars_daily
        for bar_date, close in scenario["bars"]:
            bar = BarDaily(
                instrument_id=inst.id,
                trade_date=bar_date,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("1000"),
            )
            db.add(bar)

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
        total_instruments=len(instruments),
        succeeded_count=len(instruments),
    )
    db.add(run)
    await db.flush()

    for i, scenario in enumerate(bar_scenarios):
        payload = scenario.get("payload")
        if payload is not None:
            result = StrategyResult(
                run_id=run.id,
                strategy_version_id=version.id,
                instrument_id=instruments[i].id,
                trade_date=trade_date,
                payload=payload,
            )
            db.add(result)
            await db.flush()

        item = StrategyRunItem(
            run_id=run.id,
            instrument_id=instruments[i].id,
            status="succeeded" if payload is not None else "skipped",
            started_at=now,
            finished_at=now,
        )
        db.add(item)

    await db.flush()
    return {
        "run_id": run.id,
        "instruments": instruments,
        "trade_date": trade_date,
    }


async def test_published_run_t_minus_1_db_latest_t_returns_t_change_pct(
    db_session: AsyncSession,
) -> None:
    """published run 为 T-1、DB 最新日线为 T 时返回 T 涨跌幅。"""
    test_data = await _setup_run_with_bars(
        db_session,
        bar_scenarios=[
            {
                # T-1 close=10, T close=11 → +10%
                "bars": [(date(2026, 7, 3), Decimal("10")), (date(2026, 7, 4), Decimal("11"))],
                "payload": {"dsa_dir_bars": 5},
            },
        ],
    )

    page = await query_run_items_with_results(
        db_session,
        run_id=test_data["run_id"],
        limit=50,
    )

    assert len(page.items) == 1
    row = page.items[0]
    assert row.latest_change_pct is not None
    assert abs(row.latest_change_pct - 10.0) < 0.01, (
        f"应为 +10%, 实际={row.latest_change_pct}"
    )
    assert row.latest_change_trade_date == date(2026, 7, 4)


async def test_after_close_unpublished_returns_previous_pct(
    db_session: AsyncSession,
) -> None:
    """盘后编排未完成（仅 T-1 和 T-2 bar）也正确返回最新涨跌幅。"""
    test_data = await _setup_run_with_bars(
        db_session,
        bar_scenarios=[
            {
                # T-2 close=8, T-1 close=9 → +12.5%
                "bars": [(date(2026, 7, 2), Decimal("8")), (date(2026, 7, 3), Decimal("9"))],
                "payload": {"dsa_dir_bars": 5},
            },
        ],
    )

    page = await query_run_items_with_results(
        db_session,
        run_id=test_data["run_id"],
        limit=50,
    )

    assert len(page.items) == 1
    row = page.items[0]
    assert row.latest_change_pct is not None
    assert abs(row.latest_change_pct - 12.5) < 0.01


async def test_single_bar_returns_none(
    db_session: AsyncSession,
) -> None:
    """仅一根 bar（无 prev_close）返回 None。"""
    test_data = await _setup_run_with_bars(
        db_session,
        bar_scenarios=[
            {
                "bars": [(date(2026, 7, 4), Decimal("10"))],
                "payload": {"dsa_dir_bars": 5},
            },
        ],
    )

    page = await query_run_items_with_results(
        db_session,
        run_id=test_data["run_id"],
        limit=50,
    )

    assert len(page.items) == 1
    assert page.items[0].latest_change_pct is None
    assert page.items[0].latest_change_trade_date is None


async def test_null_close_returns_none(
    db_session: AsyncSession,
) -> None:
    """close 为 None 的 bar 不参与计算（视为无效）。"""
    test_data = await _setup_run_with_bars(
        db_session,
        bar_scenarios=[
            {
                # 最新 bar close=None → lag 后 prev_close=None → 被过滤
                "bars": [(date(2026, 7, 3), Decimal("10")), (date(2026, 7, 4), None)],
                "payload": {"dsa_dir_bars": 5},
            },
        ],
    )

    page = await query_run_items_with_results(
        db_session,
        run_id=test_data["run_id"],
        limit=50,
    )

    assert len(page.items) == 1
    # 最新 bar close=None，row_number=1 的行 close=None，prev_close=10
    # 但 close/prev_close = None/10 → NULL，所以 latest_change_pct 为 None
    assert page.items[0].latest_change_pct is None


async def test_prev_close_zero_returns_none(
    db_session: AsyncSession,
) -> None:
    """prev_close=0 时跳过（避免除零）。"""
    test_data = await _setup_run_with_bars(
        db_session,
        bar_scenarios=[
            {
                # T-1 close=0, T close=10 → prev_close=0 被过滤
                "bars": [(date(2026, 7, 3), Decimal("0")), (date(2026, 7, 4), Decimal("10"))],
                "payload": {"dsa_dir_bars": 5},
            },
        ],
    )

    page = await query_run_items_with_results(
        db_session,
        run_id=test_data["run_id"],
        limit=50,
    )

    assert len(page.items) == 1
    assert page.items[0].latest_change_pct is None


async def test_red_up_green_down(
    db_session: AsyncSession,
) -> None:
    """红涨绿跌：正数=涨，负数=跌。"""
    test_data = await _setup_run_with_bars(
        db_session,
        bar_scenarios=[
            {
                # +20% 涨
                "bars": [(date(2026, 7, 3), Decimal("10")), (date(2026, 7, 4), Decimal("12"))],
                "payload": {"dsa_dir_bars": 5},
            },
            {
                # -15% 跌
                "bars": [(date(2026, 7, 3), Decimal("20")), (date(2026, 7, 4), Decimal("17"))],
                "payload": {"dsa_dir_bars": -3},
            },
        ],
    )

    page = await query_run_items_with_results(
        db_session,
        run_id=test_data["run_id"],
        limit=50,
    )

    assert len(page.items) == 2
    pcts = {item.instrument_id: item.latest_change_pct for item in page.items}
    inst0 = test_data["instruments"][0].id
    inst1 = test_data["instruments"][1].id
    assert pcts[inst0] is not None and pcts[inst0] > 0, "标的0应为涨（正数）"
    assert pcts[inst1] is not None and pcts[inst1] < 0, "标的1应为跌（负数）"


async def test_sort_by_change_pct_desc(
    db_session: AsyncSession,
) -> None:
    """sort by change_pct desc 使用 latest 字段排序。"""
    test_data = await _setup_run_with_bars(
        db_session,
        bar_scenarios=[
            {
                # +5%
                "bars": [(date(2026, 7, 3), Decimal("100")), (date(2026, 7, 4), Decimal("105"))],
                "payload": {"dsa_dir_bars": 5},
            },
            {
                # +20%
                "bars": [(date(2026, 7, 3), Decimal("10")), (date(2026, 7, 4), Decimal("12"))],
                "payload": {"dsa_dir_bars": -3},
            },
            {
                # -10%
                "bars": [(date(2026, 7, 3), Decimal("20")), (date(2026, 7, 4), Decimal("18"))],
                "payload": {"dsa_dir_bars": 2},
            },
        ],
    )

    page = await query_run_items_with_results(
        db_session,
        run_id=test_data["run_id"],
        sort=SortSpec(field=CHANGE_PCT_METRIC_KEY, desc=True),
        limit=50,
    )

    assert len(page.items) == 3
    pcts = [item.latest_change_pct for item in page.items]
    assert pcts[0] is not None and pcts[1] is not None and pcts[2] is not None
    assert pcts[0] > pcts[1] > pcts[2], f"降序排列错误: {pcts}"
    assert abs(pcts[0] - 20.0) < 0.01
    assert abs(pcts[1] - 5.0) < 0.01
    assert abs(pcts[2] - (-10.0)) < 0.01


async def test_filter_by_change_pct_gt(
    db_session: AsyncSession,
) -> None:
    """filter by change_pct gt 使用 latest 字段筛选，filtered_total 一致。"""
    test_data = await _setup_run_with_bars(
        db_session,
        bar_scenarios=[
            {
                # +5%
                "bars": [(date(2026, 7, 3), Decimal("100")), (date(2026, 7, 4), Decimal("105"))],
                "payload": {"dsa_dir_bars": 5},
            },
            {
                # +20%
                "bars": [(date(2026, 7, 3), Decimal("10")), (date(2026, 7, 4), Decimal("12"))],
                "payload": {"dsa_dir_bars": -3},
            },
            {
                # -10%
                "bars": [(date(2026, 7, 3), Decimal("20")), (date(2026, 7, 4), Decimal("18"))],
                "payload": {"dsa_dir_bars": 2},
            },
        ],
    )

    page = await query_run_items_with_results(
        db_session,
        run_id=test_data["run_id"],
        filters=[MetricFilter(metric_key=CHANGE_PCT_METRIC_KEY, operator="gt", value=0.0)],
        limit=50,
    )

    # 只有 +5% 和 +20% 命中
    assert page.total == 2, f"total (filtered) 应为 2, 实际={page.total}"
    assert len(page.items) == 2, f"items 应为 2, 实际={len(page.items)}"
    for item in page.items:
        assert item.latest_change_pct is not None and item.latest_change_pct > 0


async def test_no_n_plus1_queries(
    db_session: AsyncSession,
) -> None:
    """固定 SQL 数量，无 N+1：批量加载 latest_change_pct 不随 instrument 数量增长。"""
    # 5 个 instrument，每个有 2 根日线
    scenarios = [
        {
            "bars": [
                (date(2026, 7, 3), Decimal(str(10 + i))),
                (date(2026, 7, 4), Decimal(str(11 + i))),
            ],
            "payload": {"dsa_dir_bars": 5},
        }
        for i in range(5)
    ]
    test_data = await _setup_run_with_bars(db_session, bar_scenarios=scenarios)

    query_count = 0
    sync_engine = db_session.sync_session.bind

    @event.listens_for(sync_engine, "after_cursor_execute")
    def _counter(*args: Any, **kwargs: Any) -> None:
        nonlocal query_count
        query_count += 1

    try:
        page = await query_run_items_with_results(
            db_session,
            run_id=test_data["run_id"],
            limit=50,
        )
    finally:
        event.remove(sync_engine, "after_cursor_execute", _counter)

    assert len(page.items) == 5
    # 预期 SQL 数量固定：
    #   1. 主查询（strategy_run_items + 过滤 + 排序 + 分页）
    #   2. count 查询
    #   3. 批量加载 strategy_results
    #   4. 批量加载 instruments
    #   5. 批量加载 latest_change_pct（_fetch_latest_change_pct_map）
    # 不随 instrument 数量线性增长（N+1 会产生 5+N 次）
    assert query_count <= 8, (
        f"SQL 查询次数过多，可能存在 N+1: {query_count}（5 个 instrument）"
    )
