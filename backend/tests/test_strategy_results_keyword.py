"""策略结果 keyword 搜索测试（CHANGE-20260713-005）。

测试目标：
- /strategy-runs/{run_id}/results 的 keyword 参数同时匹配
  Instrument.symbol / Instrument.name / Instrument.pinyin_initials
- items 与 filtered_total 条件一致（同一 keyword 下 items.length == filtered_total）
- total 字段为该 keyword 下的真实总数（不是 items.length）

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5433/bz_stock_test \
        pytest backend/tests/test_strategy_results_keyword.py -q
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
    StrategyRun,
    StrategyRunItem,
)
from app.services.selector_query_service import query_published_selector_results


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _setup_run_with_named_instruments(
    db_session: AsyncSession,
    *,
    instruments: list[dict[str, str]],
) -> dict[str, Any]:
    """创建 1 published run + N 个 instruments（每个 instrument 含 symbol/name/pinyin_initials）。

    instruments: [{"symbol": "600519", "name": "贵州茅台", "pinyin_initials": "gzmt"}, ...]
    """
    db = db_session
    now = datetime.now(UTC)
    trade_date = date(2026, 7, 3)

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

    inst_models: list[Instrument] = []
    for spec in instruments:
        inst = Instrument(
            symbol=spec["symbol"],
            name=spec["name"],
            pinyin_initials=spec.get("pinyin_initials"),
            market="SH",
            status="active",
        )
        db.add(inst)
        await db.flush()
        inst_models.append(inst)

    n = len(inst_models)
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
        total_instruments=n,
        succeeded_count=n,
        failed_count=0,
        skipped_count=0,
    )
    db.add(run)
    await db.flush()

    for inst in inst_models:
        result = StrategyResult(
            run_id=run.id,
            strategy_version_id=version.id,
            instrument_id=inst.id,
            trade_date=trade_date,
            payload={"dsa_dir_bars": 30},
        )
        db.add(result)
        await db.flush()
        item = StrategyRunItem(
            run_id=run.id,
            instrument_id=inst.id,
            status="succeeded",
            result_id=result.id,
            started_at=now,
            finished_at=now,
        )
        db.add(item)

    await db.flush()
    return {
        "run_id": run.id,
        "version_id": version.id,
        "instruments": inst_models,
        "trade_date": trade_date,
    }


async def test_keyword_matches_symbol(db_session: AsyncSession) -> None:
    """keyword 按股票代码 ILIKE 匹配，items 与 total 条件一致。"""
    test_data = await _setup_run_with_named_instruments(
        db_session,
        instruments=[
            {"symbol": "600519", "name": "贵州茅台", "pinyin_initials": "gzmt"},
            {"symbol": "000001", "name": "平安银行", "pinyin_initials": "payh"},
            {"symbol": "000002", "name": "万科A", "pinyin_initials": "wka"},
        ],
    )

    page = await query_published_selector_results(
        db_session,
        run_id=test_data["run_id"],
        keyword="600519",
        page=1,
        page_size=50,
    )

    assert page.filtered_total == 1, f"filtered_total 应为 1（匹配 600519），实际={page.filtered_total}"
    assert len(page.items) == 1, f"items 长度应为 1，实际={len(page.items)}"
    assert page.items[0].instrument.symbol == "600519"
    # items.length 与 filtered_total 条件一致
    assert len(page.items) == page.filtered_total


async def test_keyword_matches_name(db_session: AsyncSession) -> None:
    """keyword 按中文名称 ILIKE 匹配，items 与 total 条件一致。"""
    test_data = await _setup_run_with_named_instruments(
        db_session,
        instruments=[
            {"symbol": "600519", "name": "贵州茅台", "pinyin_initials": "gzmt"},
            {"symbol": "600518", "name": "康美药业", "pinyin_initials": "kmyy"},
            {"symbol": "000858", "name": "五粮液", "pinyin_initials": "wly"},
        ],
    )

    page = await query_published_selector_results(
        db_session,
        run_id=test_data["run_id"],
        keyword="茅台",
        page=1,
        page_size=50,
    )

    assert page.filtered_total == 1, f"filtered_total 应为 1（匹配 茅台），实际={page.filtered_total}"
    assert len(page.items) == 1
    assert page.items[0].instrument.name == "贵州茅台"
    assert len(page.items) == page.filtered_total


async def test_keyword_matches_pinyin_initials(db_session: AsyncSession) -> None:
    """keyword 按拼音首字母 ILIKE 匹配，items 与 total 条件一致。"""
    test_data = await _setup_run_with_named_instruments(
        db_session,
        instruments=[
            {"symbol": "600519", "name": "贵州茅台", "pinyin_initials": "gzmt"},
            {"symbol": "600518", "name": "康美药业", "pinyin_initials": "kmyy"},
            {"symbol": "000858", "name": "五粮液", "pinyin_initials": "wly"},
        ],
    )

    page = await query_published_selector_results(
        db_session,
        run_id=test_data["run_id"],
        keyword="gzmt",
        page=1,
        page_size=50,
    )

    assert page.filtered_total == 1, (
        f"filtered_total 应为 1（匹配 pinyin_initials=gzmt），实际={page.filtered_total}"
    )
    assert len(page.items) == 1
    assert page.items[0].instrument.symbol == "600519"
    assert page.items[0].instrument.pinyin_initials == "gzmt"
    assert len(page.items) == page.filtered_total


if __name__ == "__main__":
    import inspect

    assert inspect.iscoroutinefunction(test_keyword_matches_symbol)
    assert inspect.iscoroutinefunction(test_keyword_matches_name)
    assert inspect.iscoroutinefunction(test_keyword_matches_pinyin_initials)
    print("test_strategy_results_keyword 模块导入 ✓")
    print("OK")
