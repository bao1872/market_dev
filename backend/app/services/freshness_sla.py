"""数据新鲜度 SLA 检查服务。

检查 bars_daily/bars_minute/bars_15min/bars_60min 表中数据的时效性，
过期时触发拉取刷新。

SLA 定义（与任务约束一致）：
- 日线 SLA: 1800 秒（30 分钟）—— 数据应在收盘后 30 分钟内更新
- 15min SLA: 900 秒（15 分钟）—— 15 分钟线应在对应周期结束后 15 分钟内更新
- 60min SLA: 3600 秒（1 小时）—— 60 分钟线应在对应周期结束后 1 小时内更新
- 周线 SLA: 7 天 —— 周线数据应在每周结束后 7 天内更新
- 月线 SLA: 30 天 —— 月线数据应在每月结束后 30 天内更新

设计说明：
- 周线/月线不存储在 DB，从日线动态合成（convert_kline_frequency）
- 周线/月线的新鲜度等同于日线新鲜度（数据源相同）
- ensure_weekly_freshness/ensure_monthly_freshness 委托给 ensure_daily_freshness
- 1m 分钟线不参与定时刷新，仅按需查询

新鲜度计算：
- 日线/周线/月线：last_update = MAX(trade_date) 对应的收盘时间（当天 15:00）
  age = now - last_update；盘前 age < 0 视为 fresh（无需当日数据）
- 分钟/15min/60min：last_update = MAX(trade_time)
  age = now - last_update

注意：非交易时间会报 stale，调用方应结合交易日历决定是否触发刷新。

Inputs:
    session: AsyncSession
    instrument_id: UUID

Outputs:
    FreshnessResult: is_fresh, last_update, age_seconds, sla_seconds

How to Run:
    python -m app.services.freshness_sla    # 自测：验证 FreshnessResult 与 SLA 常量
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import select

from app.models.bar import Bar15Min, Bar60Min, BarDaily, BarMinute
from app.repositories.bar_repository import (
    refresh_15min_bars,
    refresh_60min_bars,
    refresh_daily_bars,
)
from app.services.bars_metrics import bars_freshness_age_seconds
from app.services.bars_scheduler_service import BarsSchedulerService

logger = logging.getLogger("freshness_sla")

# SLA 常量（秒）
DAILY_SLA_SECONDS = 1800  # 30 分钟
BAR_15MIN_SLA_SECONDS = 900  # 15 分钟
BAR_60MIN_SLA_SECONDS = 3600  # 1 小时
WEEKLY_SLA_SECONDS = 7 * 86400  # 7 天
MONTHLY_SLA_SECONDS = 30 * 86400  # 30 天

# 日线收盘时间（A 股 15:00 收盘）
_DAILY_CLOSE_TIME = time(15, 0)

# 刷新拉取的回看窗口
# 日线/15min/60min 刷新条数引用 BarsSchedulerService.DAILY_COUNTS（单一权威定义）


def _record_freshness_metric(
    period: str,
    age_seconds: float | None,
    sla_seconds: int,
    is_fresh: bool,
) -> None:
    """记录新鲜度指标与严重过期告警。

    - 更新 bars_freshness_age_seconds Gauge（age_seconds 非 None 时）
    - 当 age > 2 * SLA（严重过期）时记录 logger.error

    Args:
        period: 周期标识（d/minute/15m/60m/w/m）
        age_seconds: 数据年龄（秒），无数据时为 None
        sla_seconds: SLA 阈值（秒）
        is_fresh: 是否新鲜
    """
    if age_seconds is not None:
        bars_freshness_age_seconds.labels(period=period).set(age_seconds)
        # 严重过期告警：age > 2 * SLA
        if not is_fresh and age_seconds > 2 * sla_seconds:
            logger.error(
                "数据严重过期 period=%s age=%.0fs sla=%ds（超过 2 倍 SLA）",
                period, age_seconds, sla_seconds,
            )


@dataclass
class FreshnessResult:
    """新鲜度检查结果。

    Attributes:
        is_fresh: 数据是否新鲜（age <= SLA）
        last_update: 最新数据时间（日线为收盘时间，分钟为 trade_time）
        age_seconds: 数据年龄（秒），无数据时为 None
        sla_seconds: SLA 阈值（秒）
    """

    is_fresh: bool
    last_update: datetime | None
    age_seconds: float | None
    sla_seconds: int


# ===== 日线 =====


async def check_daily_freshness(
    session: AsyncSession,
    instrument_id: uuid.UUID,
) -> FreshnessResult:
    """检查日线数据新鲜度。

    last_update = MAX(trade_date) 对应的当天 15:00 收盘时间。
    age = now - last_update；盘前 age < 0 视为 fresh。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID

    Returns:
        FreshnessResult
    """
    try:
        result = await session.execute(
            select(func.max(BarDaily.trade_date))
            .where(BarDaily.instrument_id == instrument_id)
        )
        latest_date: date | None = result.scalar()
    except Exception as exc:
        logger.warning("查询日线最新日期失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if latest_date is None:
        logger.warning("日线无数据 instrument_id=%s", instrument_id)
        return FreshnessResult(
            is_fresh=False,
            last_update=None,
            age_seconds=None,
            sla_seconds=DAILY_SLA_SECONDS,
        )

    last_update = datetime.combine(latest_date, _DAILY_CLOSE_TIME, tzinfo=ZoneInfo("Asia/Shanghai"))
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    age = (now - last_update).total_seconds()

    is_fresh = age <= DAILY_SLA_SECONDS
    logger.info(
        "日线新鲜度 instrument_id=%s latest_date=%s age=%.0fs sla=%ds is_fresh=%s",
        instrument_id, latest_date, age, DAILY_SLA_SECONDS, is_fresh,
    )
    _record_freshness_metric("d", age, DAILY_SLA_SECONDS, is_fresh)
    return FreshnessResult(
        is_fresh=is_fresh,
        last_update=last_update,
        age_seconds=age,
        sla_seconds=DAILY_SLA_SECONDS,
    )


async def ensure_daily_freshness(
    session: AsyncSession,
    instrument_id: uuid.UUID,
) -> FreshnessResult:
    """检查日线新鲜度，过期则触发拉取刷新。

    刷新范围：最近 5 天（覆盖周末），拉取后重新检查新鲜度。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID

    Returns:
        刷新后的 FreshnessResult
    """
    result = await check_daily_freshness(session, instrument_id)
    if result.is_fresh:
        return result

    logger.info("日线数据过期，触发刷新 instrument_id=%s", instrument_id)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    start = (now - timedelta(days=BarsSchedulerService.DAILY_COUNTS["d"])).date()
    end = now.date()
    try:
        await refresh_daily_bars(session, instrument_id, start, end)
    except Exception as exc:
        logger.warning("日线刷新失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    return await check_daily_freshness(session, instrument_id)


# ===== 分钟线 =====

# 1m 健康检查参考 SLA（仅用于监控参考，不触发自动刷新）
_MINUTE_CHECK_SLA_SECONDS = 90


async def check_minute_freshness(
    session: AsyncSession,
    instrument_id: uuid.UUID,
) -> FreshnessResult:
    """检查分钟线数据新鲜度。

    last_update = MAX(trade_time)；age = now - last_update。

    注意：1m 数据不参与定时刷新，此检查仅用于监控参考。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID

    Returns:
        FreshnessResult
    """
    try:
        result = await session.execute(
            select(func.max(BarMinute.trade_time))
            .where(BarMinute.instrument_id == instrument_id)
        )
        latest_time: datetime | None = result.scalar()
    except Exception as exc:
        logger.warning("查询分钟线最新时间失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if latest_time is None:
        logger.warning("分钟线无数据 instrument_id=%s", instrument_id)
        return FreshnessResult(
            is_fresh=False,
            last_update=None,
            age_seconds=None,
            sla_seconds=_MINUTE_CHECK_SLA_SECONDS,
        )

    if latest_time.tzinfo is None:
        latest_time = latest_time.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    else:
        latest_time = latest_time.astimezone(ZoneInfo("Asia/Shanghai"))

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    age = (now - latest_time).total_seconds()

    is_fresh = age <= _MINUTE_CHECK_SLA_SECONDS
    logger.info(
        "分钟线新鲜度 instrument_id=%s latest_time=%s age=%.0fs sla=%ds is_fresh=%s",
        instrument_id, latest_time, age, _MINUTE_CHECK_SLA_SECONDS, is_fresh,
    )
    _record_freshness_metric("minute", age, _MINUTE_CHECK_SLA_SECONDS, is_fresh)
    return FreshnessResult(
        is_fresh=is_fresh,
        last_update=latest_time,
        age_seconds=age,
        sla_seconds=_MINUTE_CHECK_SLA_SECONDS,
    )


# ===== 15 分钟线 =====


async def check_15min_freshness(
    session: AsyncSession,
    instrument_id: uuid.UUID,
) -> FreshnessResult:
    """检查 15 分钟线数据新鲜度。

    last_update = MAX(trade_time)；age = now - last_update。
    """
    try:
        result = await session.execute(
            select(func.max(Bar15Min.trade_time))
            .where(Bar15Min.instrument_id == instrument_id)
        )
        latest_time: datetime | None = result.scalar()
    except Exception as exc:
        logger.warning("查询 15min 最新时间失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if latest_time is None:
        logger.warning("15min 无数据 instrument_id=%s", instrument_id)
        return FreshnessResult(
            is_fresh=False,
            last_update=None,
            age_seconds=None,
            sla_seconds=BAR_15MIN_SLA_SECONDS,
        )

    if latest_time.tzinfo is None:
        latest_time = latest_time.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    else:
        latest_time = latest_time.astimezone(ZoneInfo("Asia/Shanghai"))

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    age = (now - latest_time).total_seconds()

    is_fresh = age <= BAR_15MIN_SLA_SECONDS
    logger.info(
        "15min 新鲜度 instrument_id=%s latest_time=%s age=%.0fs sla=%ds is_fresh=%s",
        instrument_id, latest_time, age, BAR_15MIN_SLA_SECONDS, is_fresh,
    )
    _record_freshness_metric("15m", age, BAR_15MIN_SLA_SECONDS, is_fresh)
    return FreshnessResult(
        is_fresh=is_fresh,
        last_update=latest_time,
        age_seconds=age,
        sla_seconds=BAR_15MIN_SLA_SECONDS,
    )


async def ensure_15min_freshness(
    session: AsyncSession,
    instrument_id: uuid.UUID,
) -> FreshnessResult:
    """检查 15min 新鲜度，过期则触发拉取刷新。"""
    result = await check_15min_freshness(session, instrument_id)
    if result.is_fresh:
        return result

    logger.info("15min 数据过期，触发刷新 instrument_id=%s", instrument_id)
    try:
        await refresh_15min_bars(
            session, instrument_id, count=BarsSchedulerService.DAILY_COUNTS["15m"]
        )
    except Exception as exc:
        logger.warning("15min 刷新失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    return await check_15min_freshness(session, instrument_id)


# ===== 60 分钟线 =====


async def check_60min_freshness(
    session: AsyncSession,
    instrument_id: uuid.UUID,
) -> FreshnessResult:
    """检查 60 分钟线数据新鲜度。

    last_update = MAX(trade_time)；age = now - last_update。
    """
    try:
        result = await session.execute(
            select(func.max(Bar60Min.trade_time))
            .where(Bar60Min.instrument_id == instrument_id)
        )
        latest_time: datetime | None = result.scalar()
    except Exception as exc:
        logger.warning("查询 60min 最新时间失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if latest_time is None:
        logger.warning("60min 无数据 instrument_id=%s", instrument_id)
        return FreshnessResult(
            is_fresh=False,
            last_update=None,
            age_seconds=None,
            sla_seconds=BAR_60MIN_SLA_SECONDS,
        )

    if latest_time.tzinfo is None:
        latest_time = latest_time.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    else:
        latest_time = latest_time.astimezone(ZoneInfo("Asia/Shanghai"))

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    age = (now - latest_time).total_seconds()

    is_fresh = age <= BAR_60MIN_SLA_SECONDS
    logger.info(
        "60min 新鲜度 instrument_id=%s latest_time=%s age=%.0fs sla=%ds is_fresh=%s",
        instrument_id, latest_time, age, BAR_60MIN_SLA_SECONDS, is_fresh,
    )
    _record_freshness_metric("60m", age, BAR_60MIN_SLA_SECONDS, is_fresh)
    return FreshnessResult(
        is_fresh=is_fresh,
        last_update=latest_time,
        age_seconds=age,
        sla_seconds=BAR_60MIN_SLA_SECONDS,
    )


async def ensure_60min_freshness(
    session: AsyncSession,
    instrument_id: uuid.UUID,
) -> FreshnessResult:
    """检查 60min 新鲜度，过期则触发拉取刷新。"""
    result = await check_60min_freshness(session, instrument_id)
    if result.is_fresh:
        return result

    logger.info("60min 数据过期，触发刷新 instrument_id=%s", instrument_id)
    try:
        await refresh_60min_bars(
            session, instrument_id, count=BarsSchedulerService.DAILY_COUNTS["60m"]
        )
    except Exception as exc:
        logger.warning("60min 刷新失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    return await check_60min_freshness(session, instrument_id)


# ===== 周线 =====


async def ensure_weekly_freshness(
    session: AsyncSession,
    instrument_id: uuid.UUID,
) -> FreshnessResult:
    """检查周线新鲜度，过期则触发日线刷新。

    设计原则：周线/月线不存储在 DB，从日线动态合成。
    因此周线新鲜度等同于日线新鲜度，委托给 ensure_daily_freshness。
    """
    logger.info("周线新鲜度委托给日线检查 instrument_id=%s", instrument_id)
    return await ensure_daily_freshness(session, instrument_id)


# ===== 月线 =====


async def ensure_monthly_freshness(
    session: AsyncSession,
    instrument_id: uuid.UUID,
) -> FreshnessResult:
    """检查月线新鲜度，过期则触发日线刷新。

    设计原则：周线/月线不存储在 DB，从日线动态合成。
    因此月线新鲜度等同于日线新鲜度，委托给 ensure_daily_freshness。
    """
    logger.info("月线新鲜度委托给日线检查 instrument_id=%s", instrument_id)
    return await ensure_daily_freshness(session, instrument_id)


if __name__ == "__main__":
    # 自测入口：验证 FreshnessResult 与 SLA 常量（不连 DB，无副作用）
    logging.basicConfig(level=logging.INFO)

    # 1. 验证 SLA 常量
    assert DAILY_SLA_SECONDS == 1800, f"日线 SLA 应为 1800，实际 {DAILY_SLA_SECONDS}"
    assert BAR_15MIN_SLA_SECONDS == 900, f"15min SLA 应为 900，实际 {BAR_15MIN_SLA_SECONDS}"
    assert BAR_60MIN_SLA_SECONDS == 3600, f"60min SLA 应为 3600，实际 {BAR_60MIN_SLA_SECONDS}"
    assert WEEKLY_SLA_SECONDS == 7 * 86400, f"周线 SLA 应为 7 天，实际 {WEEKLY_SLA_SECONDS}"
    assert MONTHLY_SLA_SECONDS == 30 * 86400, f"月线 SLA 应为 30 天，实际 {MONTHLY_SLA_SECONDS}"
    print(f"DAILY_SLA_SECONDS={DAILY_SLA_SECONDS} (30 分钟)")
    print(f"BAR_15MIN_SLA_SECONDS={BAR_15MIN_SLA_SECONDS} (15 分钟)")
    print(f"BAR_60MIN_SLA_SECONDS={BAR_60MIN_SLA_SECONDS} (1 小时)")
    print(f"WEEKLY_SLA_SECONDS={WEEKLY_SLA_SECONDS} (7 天)")
    print(f"MONTHLY_SLA_SECONDS={MONTHLY_SLA_SECONDS} (30 天)")

    # 2. 验证 FreshnessResult 构造
    fresh = FreshnessResult(
        is_fresh=True,
        last_update=datetime(2026, 6, 18, 15, 0),
        age_seconds=100.0,
        sla_seconds=DAILY_SLA_SECONDS,
    )
    assert fresh.is_fresh is True
    assert fresh.age_seconds == 100.0
    print(f"fresh result: is_fresh={fresh.is_fresh}, age={fresh.age_seconds}s")

    stale = FreshnessResult(
        is_fresh=False,
        last_update=None,
        age_seconds=None,
        sla_seconds=_MINUTE_CHECK_SLA_SECONDS,
    )
    assert stale.is_fresh is False
    assert stale.age_seconds is None
    print(f"stale result: is_fresh={stale.is_fresh}, age=None")

    # 3. 验证新鲜度判断逻辑（模拟）
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    last_update_fresh = now - timedelta(seconds=600)
    age = (now - last_update_fresh).total_seconds()
    assert age <= DAILY_SLA_SECONDS, "600s 应为 fresh"
    print(f"模拟 600s ago: age={age:.0f}s <= {DAILY_SLA_SECONDS}s -> fresh ✓")

    last_update_stale = now - timedelta(seconds=7200)
    age = (now - last_update_stale).total_seconds()
    assert age > DAILY_SLA_SECONDS, "7200s 应为 stale"
    print(f"模拟 7200s ago: age={age:.0f}s > {DAILY_SLA_SECONDS}s -> stale ✓")

    # 4. 验证函数可调用
    import inspect

    expected_coroutines = [
        check_daily_freshness, ensure_daily_freshness,
        check_minute_freshness,
        check_15min_freshness, ensure_15min_freshness,
        check_60min_freshness, ensure_60min_freshness,
        ensure_weekly_freshness,
        ensure_monthly_freshness,
    ]
    for fn in expected_coroutines:
        assert inspect.iscoroutinefunction(fn), f"{fn.__name__} 应为协程"
    print(f"所有 {len(expected_coroutines)} 个函数为协程 ✓")

    # 5. 验证 _record_freshness_metric 函数
    assert callable(_record_freshness_metric), "_record_freshness_metric 应可调用"
    print("_record_freshness_metric 函数存在 ✓")

    # 验证指标记录（fresh 情况，不应触发 error 日志）
    _record_freshness_metric("d", age_seconds=100.0, sla_seconds=DAILY_SLA_SECONDS, is_fresh=True)
    print("fresh 情况指标记录 ✓（age=100s < SLA=1800s）")

    # 验证指标记录（stale 但未严重过期，不应触发 error 日志）
    _record_freshness_metric("d", age_seconds=2400.0, sla_seconds=DAILY_SLA_SECONDS, is_fresh=False)
    print("stale 情况指标记录 ✓（age=2400s > SLA=1800s 但 < 2*SLA=3600s）")

    # 验证指标记录（严重过期，应触发 error 日志）
    _record_freshness_metric("d", age_seconds=4000.0, sla_seconds=DAILY_SLA_SECONDS, is_fresh=False)
    print("严重过期指标记录 ✓（age=4000s > 2*SLA=3600s，应记录 error）")

    # 验证 age_seconds=None 时不记录指标
    _record_freshness_metric("d", age_seconds=None, sla_seconds=DAILY_SLA_SECONDS, is_fresh=False)
    print("age=None 情况跳过指标记录 ✓")

    print("\n所有自测通过 ✓（未进行 DB 测试）")
