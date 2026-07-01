"""交易日历 v9 重构回归测试（Tasks 1-6）。

覆盖核心场景：
1. Mootdx Provider 常量与 DataFrame 结构
2. 历史日期（2026 在 holidays() 覆盖内）语义：OPEN/CLOSED + MOOTDX_HISTORICAL
3. 未来/超出覆盖范围日期语义：MOOTDX_HOLIDAY
4. seed_calendar_from_mootdx 写入 source/status/verified_at
5. seed_calendar_from_mootdx 不覆盖 MANUAL_OVERRIDE（force=False）
6. seed_calendar_from_mootdx force=True 可覆盖 MANUAL_OVERRIDE
7. calendar_service DB OPEN -> True
8. calendar_service DB CLOSED -> False
9. calendar_service DB UNKNOWN -> 降级到 Mootdx
10. calendar_service 无 DB -> 降级到 Mootdx -> weekday
11. /market/status 响应包含诊断字段
12. /market/status DB UNKNOWN 时返回 "交易日历待确认" 且不显示休市
13. sync_trading_calendar --dry-run 输出计数
14. sync_trading_calendar 扫描可疑日期
15. 全仓无 Tushare 引用（此文件除外）
"""

from __future__ import annotations

from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.time import now_utc
from app.models.calendar import TradingCalendar
from app.services.mootdx_calendar_provider import (
    CALENDAR_STATUS_CLOSED,
    CALENDAR_STATUS_OPEN,
    CALENDAR_STATUS_UNKNOWN,
    MANUAL_OVERRIDE_SOURCE,
    MOOTDX_HISTORICAL_SOURCE,
    MOOTDX_HOLIDAY_SOURCE,
    build_calendar_for_year,
    is_trading_day_by_mootdx,
)

# ---------------------------------------------------------------------------
# Provider 层测试
# ---------------------------------------------------------------------------

def test_provider_constants_exist():
    """Provider 暴露新语义常量。"""
    assert CALENDAR_STATUS_OPEN == "OPEN"
    assert CALENDAR_STATUS_CLOSED == "CLOSED"
    assert CALENDAR_STATUS_UNKNOWN == "UNKNOWN"
    assert MOOTDX_HOLIDAY_SOURCE == "MOOTDX_HOLIDAY"
    assert MOOTDX_HISTORICAL_SOURCE == "MOOTDX_HISTORICAL"
    assert MANUAL_OVERRIDE_SOURCE == "MANUAL_OVERRIDE"


def test_build_calendar_for_year_columns():
    """build_calendar_for_year 返回正确列名与类型。"""
    df = build_calendar_for_year(2026)
    assert list(df.columns) == ["date", "is_trading_day", "status", "source"]
    assert len(df) == 365
    assert df["date"].iloc[0] == date(2026, 1, 1)
    assert df["date"].iloc[-1] == date(2026, 12, 31)


def test_historical_trading_day_2026_06_29():
    """2026-06-29 在历史覆盖内且为交易日 -> OPEN + MOOTDX_HISTORICAL。"""
    df = build_calendar_for_year(2026)
    row = df[df["date"] == date(2026, 6, 29)].iloc[0]
    assert row["is_trading_day"]
    assert row["status"] == CALENDAR_STATUS_OPEN
    assert row["source"] == MOOTDX_HISTORICAL_SOURCE


def test_historical_weekend_2026_06_27():
    """2026-06-27 周六 -> CLOSED + MOOTDX_HISTORICAL。"""
    df = build_calendar_for_year(2026)
    row = df[df["date"] == date(2026, 6, 27)].iloc[0]
    assert not row["is_trading_day"]
    assert row["status"] == CALENDAR_STATUS_CLOSED
    assert row["source"] == MOOTDX_HISTORICAL_SOURCE


def test_historical_holiday_2026_01_01():
    """2026-01-01 元旦 -> CLOSED + MOOTDX_HISTORICAL。"""
    df = build_calendar_for_year(2026)
    row = df[df["date"] == date(2026, 1, 1)].iloc[0]
    assert not row["is_trading_day"]
    assert row["status"] == CALENDAR_STATUS_CLOSED
    assert row["source"] == MOOTDX_HISTORICAL_SOURCE


def test_future_date_uses_mootdx_holiday_source():
    """超出 holidays() 覆盖的未来日期使用 MOOTDX_HOLIDAY 源。

    当 mootdx  holidays() 已覆盖 2026 全年时，用 2027-01-04（周一）测试。
    若未来某天 holidays() 已扩展覆盖，测试仍应通过（只是 source 会变为
    MOOTDX_HISTORICAL，此时跳过 source 断言）。
    """
    df = build_calendar_for_year(2027)
    row = df[df["date"] == date(2027, 1, 4)].iloc[0]
    assert row["source"] in (MOOTDX_HOLIDAY_SOURCE, MOOTDX_HISTORICAL_SOURCE)
    if row["source"] == MOOTDX_HOLIDAY_SOURCE:
        # 未来工作日，mootdx holiday() 返回 False 才标记为 OPEN
        assert row["is_trading_day"]
        assert row["status"] == CALENDAR_STATUS_OPEN


def test_is_trading_day_by_mootdx_for_known_dates():
    """单日期判断函数对已知日期返回预期结果。"""
    assert is_trading_day_by_mootdx(date(2026, 6, 29)) is True
    assert is_trading_day_by_mootdx(date(2026, 6, 27)) is False
    assert is_trading_day_by_mootdx(date(2026, 1, 1)) is False


# ---------------------------------------------------------------------------
# Seed 层测试
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def seeded_2026(db_session):
    """向测试库写入 2026 全年日历，测试结束后由 db_session 回滚。"""
    from app.services.calendar_seed import seed_calendar_from_mootdx

    count = await seed_calendar_from_mootdx(db_session, year=2026, force=False, commit=False)
    assert count > 0
    return count


@pytest.mark.asyncio
async def test_seed_creates_records_with_mootdx_historical(db_session, seeded_2026):
    """seed 后 DB 记录包含正确 source/status/verified_at。"""
    result = await db_session.execute(
        select(TradingCalendar).where(
            TradingCalendar.trade_date == date(2026, 6, 29),
            TradingCalendar.market == "A",
        )
    )
    row = result.scalar_one()
    assert row.is_trading_day is True
    assert row.status == CALENDAR_STATUS_OPEN
    assert row.source == MOOTDX_HISTORICAL_SOURCE
    assert row.verified_at is not None


@pytest.mark.asyncio
async def test_seed_does_not_overwrite_manual_override(db_session):
    """force=False 时不覆盖 MANUAL_OVERRIDE 记录。"""
    from app.services.calendar_seed import seed_calendar_from_mootdx

    manual = TradingCalendar(
        trade_date=date(2026, 6, 29),
        is_trading_day=False,
        market="A",
        source=MANUAL_OVERRIDE_SOURCE,
        status=CALENDAR_STATUS_CLOSED,
        verified_at=now_utc(),
    )
    db_session.add(manual)
    await db_session.flush()

    await seed_calendar_from_mootdx(db_session, year=2026, force=False, commit=False)

    result = await db_session.execute(
        select(TradingCalendar).where(
            TradingCalendar.trade_date == date(2026, 6, 29),
            TradingCalendar.market == "A",
        )
    )
    row = result.scalar_one()
    assert row.source == MANUAL_OVERRIDE_SOURCE
    assert row.status == CALENDAR_STATUS_CLOSED
    assert row.is_trading_day is False


@pytest.mark.asyncio
async def test_seed_force_overwrites_manual_override(db_session):
    """force=True 时可覆盖 MANUAL_OVERRIDE 记录。"""
    from app.services.calendar_seed import seed_calendar_from_mootdx

    manual = TradingCalendar(
        trade_date=date(2026, 6, 29),
        is_trading_day=False,
        market="A",
        source=MANUAL_OVERRIDE_SOURCE,
        status=CALENDAR_STATUS_CLOSED,
        verified_at=now_utc(),
    )
    db_session.add(manual)
    await db_session.flush()

    await seed_calendar_from_mootdx(db_session, year=2026, force=True, commit=False)

    result = await db_session.execute(
        select(TradingCalendar).where(
            TradingCalendar.trade_date == date(2026, 6, 29),
            TradingCalendar.market == "A",
        )
    )
    row = result.scalar_one()
    await db_session.refresh(row)
    assert row.source == MOOTDX_HISTORICAL_SOURCE
    assert row.status == CALENDAR_STATUS_OPEN
    assert row.is_trading_day is True


# ---------------------------------------------------------------------------
# Service 层测试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_service_db_open_returns_true(db_session):
    """DB status=OPEN 时直接返回 True。"""
    from app.services.calendar_service import is_trading_day_async

    db_session.add(
        TradingCalendar(
            trade_date=date(2026, 6, 29),
            is_trading_day=True,
            market="A",
            source=MOOTDX_HISTORICAL_SOURCE,
            status=CALENDAR_STATUS_OPEN,
            verified_at=now_utc(),
        )
    )
    await db_session.flush()

    result = await is_trading_day_async(db_session, date(2026, 6, 29))
    assert result is True


@pytest.mark.asyncio
async def test_service_db_closed_returns_false(db_session):
    """DB status=CLOSED 时直接返回 False。"""
    from app.services.calendar_service import is_trading_day_async

    db_session.add(
        TradingCalendar(
            trade_date=date(2026, 1, 1),
            is_trading_day=False,
            market="A",
            source=MOOTDX_HISTORICAL_SOURCE,
            status=CALENDAR_STATUS_CLOSED,
            verified_at=now_utc(),
        )
    )
    await db_session.flush()

    result = await is_trading_day_async(db_session, date(2026, 1, 1))
    assert result is False


@pytest.mark.asyncio
async def test_service_db_unknown_falls_back_to_mootdx(db_session):
    """DB status=UNKNOWN 时降级到 Mootdx。"""
    from app.services.calendar_service import is_trading_day_async

    db_session.add(
        TradingCalendar(
            trade_date=date(2026, 6, 29),
            is_trading_day=False,
            market="A",
            source=MOOTDX_HOLIDAY_SOURCE,
            status=CALENDAR_STATUS_UNKNOWN,
            verified_at=now_utc(),
        )
    )
    await db_session.flush()

    result = await is_trading_day_async(db_session, date(2026, 6, 29))
    assert result is True  # Mootdx 判定为交易日


@pytest.mark.asyncio
async def test_service_no_db_uses_mootdx(db_session):
    """DB 无记录时降级到 Mootdx。"""
    from app.services.calendar_service import is_trading_day_async

    result = await is_trading_day_async(db_session, date(2026, 6, 29))
    assert result is True


# ---------------------------------------------------------------------------
# API 层测试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_status_response_has_diagnostic_fields(db_session, monkeypatch):
    """market/status 返回诊断字段。"""
    from app.api.market import get_market_status

    db_session.add(
        TradingCalendar(
            trade_date=date(2026, 6, 29),
            is_trading_day=True,
            market="A",
            source=MOOTDX_HISTORICAL_SOURCE,
            status=CALENDAR_STATUS_OPEN,
            verified_at=now_utc(),
        )
    )
    await db_session.flush()

    # [CalendarTest] - 描述: 固定 market/status 使用的当前日期，避免硬编码日期随运行日失效
    monkeypatch.setattr(
        "app.api.market.shanghai_business_date", lambda: date(2026, 6, 29)
    )

    resp = await get_market_status(db_session)
    assert resp.calendar_date == date(2026, 6, 29)
    assert resp.calendar_status == CALENDAR_STATUS_OPEN
    assert resp.calendar_source == MOOTDX_HISTORICAL_SOURCE
    assert resp.calendar_verified_at is not None
    assert resp.degraded is False
    assert resp.degraded_reason is None


@pytest.mark.asyncio
async def test_market_status_unknown_shows_waiting_text(db_session, monkeypatch):
    """DB UNKNOWN 时不显示休市，状态文案为交易日历待确认。"""
    from app.api.market import get_market_status

    db_session.add(
        TradingCalendar(
            trade_date=date(2026, 6, 29),
            is_trading_day=False,
            market="A",
            source=MOOTDX_HOLIDAY_SOURCE,
            status=CALENDAR_STATUS_UNKNOWN,
            verified_at=now_utc(),
        )
    )
    await db_session.flush()

    # [CalendarTest] - 描述: 固定 market/status 使用的当前日期，确保命中已种子的 UNKNOWN 记录
    monkeypatch.setattr(
        "app.api.market.shanghai_business_date", lambda: date(2026, 6, 29)
    )

    resp = await get_market_status(db_session)
    assert resp.degraded is True
    assert resp.degraded_reason == "calendar status UNKNOWN"
    assert resp.status_text == "交易日历待确认"
    assert resp.market_session != "NON_TRADING_DAY"


# ---------------------------------------------------------------------------
# Sync 脚本测试
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_dry_run_counts(db_session):
    """sync_trading_calendar --dry-run 输出预期计数。"""
    from app.scripts.sync_trading_calendar import run_sync

    report = await run_sync(year=2026, dry_run=True, apply=False, session=db_session)
    assert report["trading_days"] > 0
    assert report["closed_days"] > 0
    assert report["unknown_days"] == 0  # 2026 在 holidays 覆盖内
    assert report["db_to_insert"] >= 0
    assert report["db_to_update"] >= 0


@pytest.mark.asyncio
async def test_sync_identifies_suspicious_dates(db_session):
    """sync 脚本能发现 DB 中周一至周五但被标记为 CLOSED 的可疑日期。"""
    from app.scripts.sync_trading_calendar import run_sync

    # 2026-06-29 是周一交易日，故意标记为 CLOSED
    db_session.add(
        TradingCalendar(
            trade_date=date(2026, 6, 29),
            is_trading_day=False,
            market="A",
            source=MOOTDX_HOLIDAY_SOURCE,
            status=CALENDAR_STATUS_CLOSED,
            verified_at=now_utc(),
        )
    )
    await db_session.flush()

    report = await run_sync(year=2026, dry_run=True, apply=False, session=db_session)
    suspicious = [d for d in report["suspicious_dates"] if d == date(2026, 6, 29)]
    assert len(suspicious) == 1


# ---------------------------------------------------------------------------
# 全仓 Tushare 引用检查
# ---------------------------------------------------------------------------

def test_no_tushare_references_in_backend():
    """backend/app、tests、pyproject.toml 中无 Tushare 引用（历史迁移文件除外）。"""
    import subprocess

    result = subprocess.run(
        [
            "grep", "-Rin",
            "tushare|TUSHARE|pro_api|trade_cal|get_tushare_token|fetch_calendar_from_tushare",
            "backend/app", "backend/pyproject.toml", "backend/tests",
        ],
        cwd="/root/web_dev",
        capture_output=True,
        text=True,
    )
    # 仅允许本测试文件自身与历史迁移文件出现关键词
    allowed = ["test_calendar_v9_regression.py"]
    lines = [line for line in result.stdout.splitlines() if not any(a in line for a in allowed)]
    assert not lines, f"发现未清理的 Tushare 引用：\n{chr(10).join(lines)}"
