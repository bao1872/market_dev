"""统一行情覆盖率服务测试。

覆盖：
- BarsCoverageService.compute_daily_coverage 返回结构与口径
- 分子/分母排除指数/ETF，仅统计 A 股股票
- trade_date 缺省时使用 shanghai_business_date()
- get_latest_trade_date 返回 <= 上海业务日期的最新交易日

测试策略：
- 使用 db_session fixture（PostgreSQL 测试库，事务回滚）
- 构造明确股票/指数/ETF，验证 A 股过滤规则
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.services.bars_coverage_service import BarsCoverageService

TEST_DATE = date(2026, 6, 24)


def _a_stock(symbol: str, market: str, name: str = "测试股票") -> Instrument:
    """构造一只 A 股股票标的。"""
    return Instrument(
        id=uuid.uuid4(),
        symbol=symbol,
        name=name,
        market=market,
        status="active",
    )


async def _add_bar_daily(
    db_session,
    instrument_id: uuid.UUID,
    trade_date: date,
) -> None:
    """为指定标的插入一条 BarDaily 记录。"""
    db_session.add(
        BarDaily(
            instrument_id=instrument_id,
            trade_date=trade_date,
            open=Decimal("10.0"),
            high=Decimal("11.0"),
            low=Decimal("9.0"),
            close=Decimal("10.5"),
            volume=Decimal("1000000"),
            amount=Decimal("10000000"),
            adj_factor=Decimal("1.0"),
        )
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_compute_daily_coverage_counts_only_a_stocks(db_session):
    """分子和分母只计 A 股股票，排除指数/ETF。"""
    stock_sh = _a_stock("600519", "SH", "贵州茅台")
    stock_sz = _a_stock("000001", "SZ", "平安银行")
    index_sh = Instrument(
        id=uuid.uuid4(), symbol="000016", name="上证50", market="SH", status="active"
    )
    etf_sh = Instrument(
        id=uuid.uuid4(), symbol="510050", name="上证50ETF", market="SH", status="active"
    )
    db_session.add_all([stock_sh, stock_sz, index_sh, etf_sh])
    await db_session.flush()

    # 股票 + 指数都有当日日线（模拟 bars_daily 残留指数数据）
    await _add_bar_daily(db_session, stock_sh.id, TEST_DATE)
    await _add_bar_daily(db_session, stock_sz.id, TEST_DATE)
    await _add_bar_daily(db_session, index_sh.id, TEST_DATE)

    result = await BarsCoverageService.compute_daily_coverage(db_session, TEST_DATE)

    assert set(result.keys()) == {"trade_date", "covered", "total", "coverage", "coverage_raw", "source"}
    assert result["trade_date"] == TEST_DATE.isoformat()
    # 分子应只含 2 只股票，不含指数
    assert result["covered"] == 2
    # 分母应只含 2 只股票，不含指数/ETF
    assert result["total"] == 2
    assert result["coverage"] == 1.0
    assert result["coverage_raw"] == 1.0
    assert result["source"] == "bars_daily"


@pytest.mark.asyncio
async def test_compute_daily_coverage_default_trade_date(db_session):
    """trade_date 为 None 时使用 shanghai_business_date()。"""
    stock = _a_stock("600000", "SH")
    db_session.add(stock)
    await db_session.flush()
    await _add_bar_daily(db_session, stock.id, TEST_DATE)

    with patch(
        "app.services.bars_coverage_service.shanghai_business_date",
        return_value=TEST_DATE,
    ):
        result = await BarsCoverageService.compute_daily_coverage(db_session, None)

    assert result["trade_date"] == TEST_DATE.isoformat()
    assert result["covered"] == 1
    assert result["total"] == 1
    assert result["coverage"] == 1.0
    assert result["coverage_raw"] == 1.0


@pytest.mark.asyncio
async def test_coverage_raw_used_for_threshold_not_rounded_value(db_session):
    """阈值判断应使用 coverage_raw，round 后的 coverage 仅用于展示。"""
    stock = _a_stock("600000", "SH")
    db_session.add(stock)
    await db_session.flush()

    # 清理旧 bar
    from sqlalchemy import delete
    await db_session.execute(delete(BarDaily).where(BarDaily.instrument_id == stock.id))
    await db_session.flush()

    # 只写入 1 根日线，total=1，covered=1，coverage_raw=1.0
    await _add_bar_daily(db_session, stock.id, TEST_DATE)
    result = await BarsCoverageService.compute_daily_coverage(db_session, TEST_DATE)
    assert result["coverage_raw"] == 1.0
    assert result["coverage"] == 1.0

    # 关键验证：coverage_raw 与 coverage 的关系——coverage 是 coverage_raw 的 round(..., 4)
    # 通过 mock covered/total 为 0.949999 验证 round 后可能改变阈值判断
    with patch.object(
        BarsCoverageService,
        "compute_daily_coverage",
        return_value={
            "trade_date": TEST_DATE.isoformat(),
            "covered": 95,
            "total": 100,
            "coverage": 0.95,  # round(0.94999, 4) 可能显示为 0.95
            "coverage_raw": 0.94999,
            "source": "bars_daily",
        },
    ):
        mocked = await BarsCoverageService.compute_daily_coverage(db_session, TEST_DATE)
        # 阈值判断应使用原始值，不应使用 round 后的 coverage
        assert mocked["coverage_raw"] < 0.95
        assert mocked["coverage"] == 0.95


@pytest.mark.asyncio
async def test_compute_daily_coverage_zero_total(db_session):
    """无活跃 A 股时覆盖率为 0.0，不抛异常。"""
    # 将现有活跃 A 股全部置为 inactive，确保 total=0
    from sqlalchemy import update

    await db_session.execute(
        update(Instrument).where(Instrument.status == "active").values(status="inactive")
    )
    await db_session.flush()

    result = await BarsCoverageService.compute_daily_coverage(db_session, TEST_DATE)

    assert result["covered"] == 0
    assert result["total"] == 0
    assert result["coverage"] == 0.0


@pytest.mark.asyncio
async def test_get_latest_trade_date_within_business_date(db_session):
    """get_latest_trade_date 返回 <= 上海业务日期的最大 trade_date。"""
    stock = _a_stock("600000", "SH")
    db_session.add(stock)
    await db_session.flush()

    await _add_bar_daily(db_session, stock.id, date(2026, 6, 22))
    await _add_bar_daily(db_session, stock.id, date(2026, 6, 24))
    # 未来日期应被过滤
    await _add_bar_daily(db_session, stock.id, date(2026, 6, 25))

    with patch(
        "app.services.bars_coverage_service.shanghai_business_date",
        return_value=date(2026, 6, 24),
    ):
        latest = await BarsCoverageService.get_latest_trade_date(db_session)

    assert latest == date(2026, 6, 24)


@pytest.mark.asyncio
async def test_get_latest_trade_date_no_data(db_session):
    """bars_daily 无数据时返回 None。"""
    latest = await BarsCoverageService.get_latest_trade_date(db_session)
    assert latest is None


@pytest.mark.asyncio
async def test_compute_daily_facts_detail_derives_v21_fields(db_session):
    """[R1.1c] compute_daily_facts_detail 仅用 bars_daily / instruments 无副作用派生
    target-date daily readiness 判定所需的最小事实：

    - eligible_count / daily_ready_count / coverage_ratio：直接来自 A 股覆盖
    - daily_missing_count = eligible - ready（可派生）
    - adj_factor_valid_count / adj_factor_total_count：当日 adj_factor 非空且>0 计数 / 总数
      （adjustment 合同 = target-date adj_factor 合法性，不建立 adjustment_as_of）

    [R1.1c-C3] max_bar_date / future_data_count / daily_invalid_count 不在合同内，
    不得返回（PRD10 MD-08 / PRD31 未要求，且不参与 readiness 判定）。
    """
    stock_a = _a_stock("600519", "SH", "贵州茅台")
    stock_b = _a_stock("000001", "SZ", "平安银行")
    db_session.add_all([stock_a, stock_b])
    await db_session.flush()

    # 两只股票当日均有日线；stock_a adj_factor 合法，stock_b adj_factor 缺失（非法）
    await _add_bar_daily(db_session, stock_a.id, TEST_DATE)
    await _add_bar_daily(db_session, stock_b.id, TEST_DATE)
    # 直接构造 adj_factor=None 的 bar（覆盖 helper 默认的 1.0）
    from sqlalchemy import delete
    await db_session.execute(delete(BarDaily).where(BarDaily.instrument_id == stock_b.id))
    await db_session.flush()
    db_session.add(
        BarDaily(
            instrument_id=stock_b.id,
            trade_date=TEST_DATE,
            open=Decimal("10.0"), high=Decimal("11.0"), low=Decimal("9.0"),
            close=Decimal("10.5"), volume=Decimal("1000000"), amount=Decimal("10000000"),
            adj_factor=None,
        )
    )
    # 非目标交易日 bar：不得计入 target-date readiness（合同只看 trade_date 当日）
    await _add_bar_daily(db_session, stock_a.id, date(2026, 6, 25))
    await db_session.flush()

    with patch(
        "app.services.bars_coverage_service.shanghai_business_date",
        return_value=TEST_DATE,
    ):
        detail = await BarsCoverageService.compute_daily_facts_detail(db_session, None)

    assert detail["eligible_count"] == 2
    assert detail["daily_ready_count"] == 2
    assert detail["daily_missing_count"] == 0
    assert detail["coverage_ratio"] == 1.0
    assert detail["coverage_raw"] == 1.0
    # adj_factor 合法性：stock_a 合法、stock_b 缺失 → valid=1 / total=2
    assert detail["adj_factor_total_count"] == 2
    assert detail["adj_factor_valid_count"] == 1
    assert detail["source"] == "bars_daily"
    # [R1.1c-C3] 合同外字段不得返回：未参与 readiness 判定即不查询、不上报
    for absent in ("max_bar_date", "future_data_count", "daily_invalid_count", "adjustment_as_of"):
        assert absent not in detail
