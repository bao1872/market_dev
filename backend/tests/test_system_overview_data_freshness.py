"""test_system_overview_data_freshness.py - data_freshness 子结构测试。

覆盖 Phase 9 spec 2 个核心场景：
1. 行情落后最近交易日时 is_behind_latest_trade_date=true
2. 选股已发布时 latest_published_trade_date 正确

测试策略：
- 复用 test_system_overview_service.py 的 fixture 与辅助函数模式
- mock is_trading_day_async 控制 market_session（不影响 data_freshness 直接查 trading_calendar）
- 使用 db_session fixture 创建测试数据，事务自动回滚

用法：
    cd backend && APP_ENV=test pytest tests/test_system_overview_data_freshness.py -v
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.models.bar import BarDaily
from app.models.calendar import TradingCalendar
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy_run import StrategyRun
from app.services.system_overview_service import get_system_overview

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 测试用固定日期（周二，交易日）
TEST_DATE = date(2026, 6, 24)
TEST_DATE_STR = "2026-06-24"
# 落后 2 天的日期（用于行情落后场景）
OLD_DATE = date(2026, 6, 22)


def _mock_trading_day(is_trading: bool = True):
    """创建 is_trading_day_async 的 mock 上下文（控制 market_session 计算）。"""
    return patch(
        "app.services.calendar_service.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=is_trading,
    )


async def _create_active_instruments(db_session, count: int = 5):
    """创建指定数量的 active A 股标的（满足 FK 约束与 stock_symbol_sql_filter 规则）。"""
    from app.models.instrument import Instrument

    instruments = []
    for i in range(count):
        # SZ 主板代码 00xxxx，确保被 stock_symbol_sql_filter 识别为股票
        inst = Instrument(
            symbol=f"00{1000 + i:04d}",
            name=f"测试标的{i}",
            market="SZ",
            status="active",
        )
        db_session.add(inst)
        instruments.append(inst)
    await db_session.flush()
    return instruments


async def _create_bars_daily_for_all_active(db_session, trade_date: date) -> int:
    """为 DB 中所有 active 标的创建当日 BarDaily（确保覆盖率 100%）。

    测试库存在 seed active 标的，若仅覆盖测试创建的标的会导致覆盖率 < 90%。
    本辅助函数补全所有 active 标的的 BarDaily（幂等，跳过已存在）。
    """
    from sqlalchemy import select

    from app.models.instrument import Instrument

    all_active = (await db_session.execute(
        select(Instrument.id).where(Instrument.status == "active")
    )).scalars().all()

    existing_ids = set((await db_session.execute(
        select(BarDaily.instrument_id).where(BarDaily.trade_date == trade_date)
    )).scalars().all())

    created = 0
    for inst_id in all_active:
        if inst_id in existing_ids:
            continue
        db_session.add(BarDaily(
            instrument_id=inst_id,
            trade_date=trade_date,
            open=Decimal("10.0"),
            high=Decimal("11.0"),
            low=Decimal("9.0"),
            close=Decimal("10.5"),
            volume=Decimal("1000000"),
        ))
        created += 1
    await db_session.flush()
    return created


async def _create_trading_calendar_entries(db_session, entries: list[tuple[date, bool]]):
    """创建 trading_calendar 条目（trade_date, is_trading_day）。"""
    for trade_date, is_trading in entries:
        db_session.add(TradingCalendar(
            trade_date=trade_date,
            is_trading_day=is_trading,
            market="A",
        ))
    await db_session.flush()


async def _create_bars_succeeded_job(db_session, business_date_str: str = TEST_DATE_STR):
    """创建当日 bars_scheduler succeeded job（盘后流水线前置条件）。"""
    job = SchedulerJobRun(
        job_name="bars_scheduler",
        business_date=business_date_str,
        status="succeeded",
        started_at=datetime(2026, 6, 24, 16, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 24, 16, 20, tzinfo=SHANGHAI),
    )
    db_session.add(job)
    await db_session.flush()
    return job


# ==================== data_freshness 测试（2 个核心场景）====================


@pytest.mark.asyncio
async def test_data_freshness_bars_behind_latest_trade_date(db_session):
    """场景 1: 行情落后最近交易日时 is_behind_latest_trade_date=true。

    Setup:
    - bars_daily 最新日期 = 2026-06-22（落后）
    - trading_calendar 最近交易日 = 2026-06-24
    - bars_scheduler succeeded job for 2026-06-24

    Assert:
    - data_freshness.bars.is_behind_latest_trade_date == True
    - data_freshness.bars.latest_daily_trade_date == "2026-06-22"
    - data_freshness.bars.last_success_job_id is not None
    - data_freshness.bars.daily_coverage 在 [0, 1] 区间
    """
    now = datetime(2026, 6, 24, 17, 0, tzinfo=SHANGHAI)

    # 创建 trading_calendar：6/22、6/23、6/24 均为交易日（最近交易日=6/24）
    await _create_trading_calendar_entries(db_session, [
        (date(2026, 6, 22), True),
        (date(2026, 6, 23), True),
        (date(2026, 6, 24), True),
    ])

    # 创建 active 标的 + 旧日期 BarDaily（2026-06-22，落后最近交易日 2 天）
    await _create_active_instruments(db_session, count=5)
    await _create_bars_daily_for_all_active(db_session, OLD_DATE)

    # 创建当日 bars_scheduler succeeded job
    await _create_bars_succeeded_job(db_session, TEST_DATE_STR)

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert "data_freshness" in pipeline, "data_freshness 子结构应存在"

    bars = pipeline["data_freshness"]["bars"]
    assert bars["latest_daily_trade_date"] == "2026-06-22"
    assert bars["is_behind_latest_trade_date"] is True
    assert bars["last_success_job_id"] is not None
    # daily_coverage 应为 0.0~1.0 之间
    assert 0.0 <= bars["daily_coverage"] <= 1.0


@pytest.mark.asyncio
async def test_data_freshness_strategy_published(db_session, dsa_selector_strategy):
    """场景 2: 选股已发布时 latest_published_trade_date 正确。

    Setup:
    - bars_scheduler succeeded
    - strategy_runs 插入两条 published: 6/23 和 6/24（最新）

    Assert:
    - data_freshness.strategy.latest_published_trade_date == "2026-06-24"
    - data_freshness.strategy.latest_compute_trade_date == "2026-06-24"
    - data_freshness.strategy.status == "published"
    - data_freshness.strategy.strategy_run_id is not None
    - data_freshness.strategy.total_instruments == 100
    - data_freshness.strategy.failed_count == 2
    - data_freshness.strategy.published_at is not None
    """
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)
    version_id = dsa_selector_strategy["version"].id

    # bars_scheduler succeeded
    await _create_bars_succeeded_job(db_session, TEST_DATE_STR)

    # 创建 trading_calendar 确保 is_behind_latest_trade_date 可计算
    await _create_trading_calendar_entries(db_session, [
        (date(2026, 6, 24), True),
    ])

    # 创建 active 标的 + BarDaily 覆盖（避免 coverage=0）
    await _create_active_instruments(db_session, count=5)
    await _create_bars_daily_for_all_active(db_session, TEST_DATE)

    # strategy_runs: 6/23 published + 6/24 published（最新）
    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=date(2026, 6, 23),
        status="published",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}:1",
        attempt_no=1,
        failed_count=0,
        succeeded_count=100,
        total_instruments=100,
        published_at=datetime(2026, 6, 23, 19, 0, tzinfo=SHANGHAI),
    ))
    # 最新一条：6/24 published，failed_count=2
    latest_run = StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="published",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}:2",
        attempt_no=1,
        failed_count=2,
        succeeded_count=98,
        total_instruments=100,
        published_at=datetime(2026, 6, 24, 19, 0, tzinfo=SHANGHAI),
    )
    db_session.add(latest_run)
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    strategy = pipeline["data_freshness"]["strategy"]

    assert strategy["latest_published_trade_date"] == "2026-06-24"
    assert strategy["latest_compute_trade_date"] == "2026-06-24"
    assert strategy["status"] == "published"
    assert strategy["strategy_run_id"] is not None
    assert strategy["total_instruments"] == 100
    assert strategy["failed_count"] == 2
    assert strategy["published_at"] is not None
