"""趋势选股页全量 universe 展示测试。

测试目标：
- /strategy-runs/{run_id}/results 以 strategy_run_items 为主表
- 返回全量 items（含 succeeded/skipped/failed）
- skipped/failed 行 result 为 None，指标为空
- metric_filter 只筛有指标的行，source_total 仍为 total_instruments
- universe=watchlist 按 instrument 过滤（不论 item_status）

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
        pytest backend/tests/test_strategy_results_universe.py -q
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import (
    StrategyResult,
    StrategyResultMetric,
    StrategyRun,
    StrategyRunItem,
)
from app.models.watchlist import UserWatchlistItem
from app.services.selector_query_service import query_published_selector_results


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _setup_run_with_mixed_items(
    db_session: AsyncSession,
    *,
    strategy_key: str = "dsa_selector",
) -> dict[str, Any]:
    """创建 1 published run + 3 items (succeeded/skipped/failed) + 1 strategy_result。

    Returns:
        含 run_id/version_id/instruments/user_id 的字典（user_id 用于 watchlist 测试）
    """
    db = db_session
    now = datetime.now(UTC)
    trade_date = date(2026, 7, 3)

    # 1. 策略定义 + 版本
    definition = StrategyDefinition(
        strategy_key=strategy_key,
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
                {"key": "offset_mean", "type": "numeric", "filterable": True, "sortable": True},
            ],
        },
        build_hash=f"test_{uuid.uuid4().hex[:16]}",
        released_at=now,
    )
    db.add(version)
    await db.flush()

    # 2. 3 个标的
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

    # 3. published run，total_instruments=3
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
        succeeded_count=1,
        failed_count=1,
        skipped_count=1,
    )
    db.add(run)
    await db.flush()

    # 4. 1 个 strategy_result（succeeded 行）
    result = StrategyResult(
        run_id=run.id,
        strategy_version_id=version.id,
        instrument_id=instruments[0].id,
        trade_date=trade_date,
        payload={"dsa_dir_bars": 60, "offset_mean": 0.05},
    )
    db.add(result)
    await db.flush()

    for key, val in result.payload.items():
        metric = StrategyResultMetric(
            result_id=result.id,
            strategy_version_id=version.id,
            trade_date=trade_date,
            instrument_id=instruments[0].id,
            metric_key=key,
            numeric_value=float(val),
        )
        db.add(metric)

    # 5. 3 个 run_items
    items = [
        StrategyRunItem(
            run_id=run.id,
            instrument_id=instruments[0].id,
            status="succeeded",
            result_id=result.id,
            started_at=now,
            finished_at=now,
        ),
        StrategyRunItem(
            run_id=run.id,
            instrument_id=instruments[1].id,
            status="skipped",
            reason_code="insufficient_history",
            started_at=now,
            finished_at=now,
        ),
        StrategyRunItem(
            run_id=run.id,
            instrument_id=instruments[2].id,
            status="failed",
            reason_code="runtime_error",
            error_message="测试失败原因",
            started_at=now,
            finished_at=now,
        ),
    ]
    for item in items:
        db.add(item)
    await db.flush()

    return {
        "run_id": run.id,
        "version_id": version.id,
        "instruments": instruments,
        "trade_date": trade_date,
    }


async def test_query_returns_all_run_items_including_skipped_and_failed(
    db_session: AsyncSession,
) -> None:
    """RED 1: 查询返回全量 run_items（含 skipped/failed），source_total == total_instruments。"""
    test_data = await _setup_run_with_mixed_items(db_session)

    page = await query_published_selector_results(
        db_session,
        run_id=test_data["run_id"],
        page=1,
        page_size=50,
    )

    assert page.source_total == 3, (
        f"source_total 应为 total_instruments=3, 实际={page.source_total}"
    )
    assert page.filtered_total == 3, (
        f"filtered_total 应为 3（无筛选）, 实际={page.filtered_total}"
    )
    assert len(page.items) == 3, f"items 长度应为 3, 实际={len(page.items)}"

    item_statuses = {item.item_status for item in page.items}
    assert item_statuses == {"succeeded", "skipped", "failed"}, (
        f"应包含三种 item_status, 实际={item_statuses}"
    )


async def test_skipped_row_has_null_result_and_reason_code(
    db_session: AsyncSession,
) -> None:
    """RED 2: skipped 行 item_status/reason_code 正确，result 为 None。"""
    test_data = await _setup_run_with_mixed_items(db_session)

    page = await query_published_selector_results(
        db_session,
        run_id=test_data["run_id"],
        page=1,
        page_size=50,
    )

    skipped_items = [item for item in page.items if item.item_status == "skipped"]
    assert len(skipped_items) == 1, "应有 1 个 skipped 行"
    skipped = skipped_items[0]
    assert skipped.reason_code == "insufficient_history"
    assert skipped.result is None, "skipped 行 result 应为 None"

    failed_items = [item for item in page.items if item.item_status == "failed"]
    assert len(failed_items) == 1, "应有 1 个 failed 行"
    failed = failed_items[0]
    assert failed.reason_code == "runtime_error"
    assert failed.error_message == "测试失败原因"
    assert failed.result is None, "failed 行 result 应为 None"


async def test_metric_filter_only_filters_rows_with_metrics(
    db_session: AsyncSession,
) -> None:
    """RED 3: metric_filter 只筛有指标的行，source_total 仍为 total_instruments。"""
    from app.repositories.strategy_result_repository import MetricFilter

    test_data = await _setup_run_with_mixed_items(db_session)

    filters = [MetricFilter(metric_key="dsa_dir_bars", operator="gte", value=50)]
    page = await query_published_selector_results(
        db_session,
        run_id=test_data["run_id"],
        filters=filters,
        page=1,
        page_size=50,
    )

    assert page.source_total == 3, (
        f"source_total 应仍为 3（不因 filter 改变）, 实际={page.source_total}"
    )
    assert page.filtered_total == 1, (
        f"filtered_total 应为 1（只有 succeeded 行有指标且 >= 50）, 实际={page.filtered_total}"
    )
    assert len(page.items) == 1
    assert page.items[0].item_status == "succeeded"


async def test_universe_watchlist_filters_by_instrument_regardless_of_status(
    db_session: AsyncSession,
    user_factory: Any,
) -> None:
    """RED 4: universe=watchlist 按 instrument 过滤，不论 item_status。"""
    test_data = await _setup_run_with_mixed_items(db_session)

    # 创建用户 + 自选股（含 succeeded + skipped 两个 instrument）
    # user_factory(roles=["member"]) 内部已创建 UserRole，不再重复添加
    user = await user_factory(roles=["member"])

    watchlist_instrument_ids = [
        test_data["instruments"][0].id,  # succeeded
        test_data["instruments"][1].id,  # skipped
    ]
    for inst_id in watchlist_instrument_ids:
        db_session.add(
            UserWatchlistItem(
                user_id=user.id,
                instrument_id=inst_id,
                source="manual",
                active=True,
            )
        )
    await db_session.flush()

    page = await query_published_selector_results(
        db_session,
        run_id=test_data["run_id"],
        user_id=user.id,
        universe="watchlist",
        page=1,
        page_size=50,
    )

    assert page.source_total == 3, (
        f"source_total 应仍为 3（universe 不影响 source_total）, 实际={page.source_total}"
    )
    assert page.filtered_total == 2, (
        f"filtered_total 应为 2（watchlist 含 2 个 instrument）, 实际={page.filtered_total}"
    )
    assert len(page.items) == 2
    item_statuses = {item.item_status for item in page.items}
    assert item_statuses == {"succeeded", "skipped"}, (
        f"应包含 succeeded + skipped（watchlist 内不论 status）, 实际={item_statuses}"
    )


if __name__ == "__main__":
    # 自测入口：验证测试模块可导入
    import inspect

    assert inspect.iscoroutinefunction(test_query_returns_all_run_items_including_skipped_and_failed)
    assert inspect.iscoroutinefunction(test_skipped_row_has_null_result_and_reason_code)
    assert inspect.iscoroutinefunction(test_metric_filter_only_filters_rows_with_metrics)
    assert inspect.iscoroutinefunction(test_universe_watchlist_filters_by_instrument_regardless_of_status)
    print("test_strategy_results_universe 模块导入 ✓")
    print("OK")
