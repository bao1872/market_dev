"""交易日历服务 - is_trading_day 三级降级判断。

降级策略：
    1. 查 DB trading_calendar 表（权威来源，含节假日信息）
       - status=OPEN -> True
       - status=CLOSED -> False
       - status=UNKNOWN -> 继续降级到 Mootdx
    2. 如 DB 无数据或 UNKNOWN，查 Mootdx Provider 在线判断
    3. 如 Mootdx 失败，按周末判断（周六日非交易日，最后降级）

异常处理说明（非异常吞没）：
- 每级降级记录 WARNING 日志，包含失败原因与降级目标
- 降级是文档化的业务策略，不是静默兜底
- 顶层 is_trading_day 始终返回 bool，不抛异常（保证调用方可用）
- 内部各降级函数返回 None 表示"无法判断，请降级"，返回 bool 表示"已判断"

提供：
- is_trading_day: 同步接口，三级降级判断
- is_trading_day_async: 异步接口（优先使用，避免阻塞事件循环）

用法：
    from app.services.calendar_service import is_trading_day

    if is_trading_day("2026-04-04"):
        print("清明节是交易日")

副作用：无（只读查询，不写库表/不改文件）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import shanghai_business_date
from app.db import AsyncSessionLocal
from app.models.calendar import TradingCalendar
from app.services.mootdx_calendar_provider import (
    CALENDAR_STATUS_CLOSED,
    CALENDAR_STATUS_OPEN,
    is_trading_day_by_mootdx,
)

logger = logging.getLogger(__name__)

# 日期输入类型
DateLike = str | date | datetime | None


def _parse_date(target_date: DateLike = None) -> date:
    """将输入解析为 date 对象。

    Args:
        target_date: 日期对象、字符串(YYYY-MM-DD)、datetime 或 None（默认上海业务日期）

    Returns:
        date 对象

    Raises:
        ValueError: 不支持的日期类型或格式错误
    """
    if target_date is None:
        return shanghai_business_date()
    if isinstance(target_date, datetime):
        return target_date.date()
    if isinstance(target_date, date):
        return target_date
    if isinstance(target_date, str):
        return datetime.strptime(target_date, "%Y-%m-%d").date()
    raise ValueError(f"不支持的日期类型：{type(target_date)}")


def _check_weekday(target: date) -> bool:
    """仅通过 weekday 判断（不考虑节假日）。

    最后降级方案：周六日非交易日，周一至周五视为交易日。
    注意：此方法可能将节假日误判为交易日，仅在 DB 与 Mootdx 均不可用时使用。
    """
    is_weekday = target.weekday() < 5
    logger.warning(
        "交易日判断降级为 weekday 模式：%s -> %s（可能将节假日误判为交易日）",
        target, "交易日" if is_weekday else "非交易日",
    )
    return is_weekday


def _check_mootdx_online(target: date) -> bool | None:
    """通过 Mootdx Provider 在线判断是否为交易日（同步）。

    Returns:
        True/False: 已判断
        None: 查询失败，需降级
    """
    try:
        return is_trading_day_by_mootdx(target)
    except Exception as exc:
        logger.warning(
            "Mootdx 在线交易日判断失败，降级到 weekday：%s, error=%s", target, exc
        )
        return None


async def _check_mootdx_online_async(target: date) -> bool | None:
    """通过 Mootdx Provider 在线判断是否为交易日（异步包装）。

    使用 asyncio.to_thread 避免阻塞事件循环。
    """
    try:
        return await asyncio.to_thread(_check_mootdx_online, target)
    except Exception as exc:
        logger.warning(
            "Mootdx 异步在线交易日判断失败，降级到 weekday：%s, error=%s", target, exc
        )
        return None


async def _check_database_async(session: AsyncSession, target: date) -> tuple[bool | None, str, str | None]:
    """通过 DB trading_calendar 表检查是否为交易日（异步）。

    Returns:
        (is_trading_day, status, source):
        - is_trading_day: True/False 表示已判断；None 表示需降级
        - status: DB 中的 status 字段值，无记录时为 None
        - source: DB 中的 source 字段值，无记录时为 None

    注意：查询异常时记录 WARNING 并返回 (None, None, None)（降级策略，非异常吞没）。
    """
    try:
        stmt = select(
            TradingCalendar.is_trading_day,
            TradingCalendar.status,
            TradingCalendar.source,
        ).where(
            TradingCalendar.trade_date == target,
            TradingCalendar.market == "A",
        )
        result = await session.execute(stmt)
        row = result.first()
        if row is None:
            logger.info("DB trading_calendar 无 %s 记录，降级到 Mootdx 查询", target)
            return None, None, None

        is_trading, status, source = row
        if status == CALENDAR_STATUS_OPEN:
            return bool(is_trading), status, source
        if status == CALENDAR_STATUS_CLOSED:
            return bool(is_trading), status, source
        # [CalendarService] - 描述: status=UNKNOWN 时不信任 DB，降级到 Mootdx
        logger.warning(
            "DB trading_calendar %s status=%s，降级到 Mootdx 查询", target, status
        )
        return None, status, source
    except Exception as exc:
        # 降级策略：记录 WARNING 并返回 None，由上层降级到 Mootdx
        logger.warning("DB trading_calendar 查询失败，降级到 Mootdx 查询：%s, error=%s", target, exc)
        return None, None, None


def _check_database_sync(target: date) -> tuple[bool | None, str | None, str | None]:
    """通过 DB trading_calendar 表检查是否为交易日（同步，内部使用 asyncio.run）。

    Returns:
        (is_trading_day, status, source):
        - is_trading_day: True/False 表示已判断；None 表示需降级
        - status: DB 中的 status 字段值，无记录时为 None
        - source: DB 中的 source 字段值，无记录时为 None
    """
    async def _query() -> tuple[bool | None, str | None, str | None]:
        async with AsyncSessionLocal() as session:
            return await _check_database_async(session, target)

    try:
        return asyncio.run(_query())
    except RuntimeError as exc:
        # 可能已在事件循环中（如 FastAPI 上下文），记录并降级
        logger.warning("DB 同步查询失败（可能已在事件循环中），降级到 Mootdx：%s, error=%s", target, exc)
        return None, None, None
    except Exception as exc:
        logger.warning("DB 同步查询异常，降级到 Mootdx：%s, error=%s", target, exc)
        return None, None, None


async def is_trading_day_async(
    session: AsyncSession,
    target_date: DateLike = None,
) -> bool:
    """异步判断指定日期是否为交易日（三级降级）。

    降级顺序：
    1. DB trading_calendar 表（使用传入的 session）
    2. Mootdx Provider 在线判断
    3. weekday 判断（周六日非交易日）

    Args:
        session: 异步数据库会话
        target_date: 日期对象、字符串(YYYY-MM-DD)、datetime 或 None（默认上海业务日期）

    Returns:
        True 表示交易日，False 表示非交易日（始终返回 bool，不抛异常）
    """
    target = _parse_date(target_date)

    # 第一级：DB 查询
    result, _, _ = await _check_database_async(session, target)
    if result is not None:
        return result

    # 第二级：Mootdx 在线查询
    result = await _check_mootdx_online_async(target)
    if result is not None:
        return result

    # 第三级：weekday 判断
    return _check_weekday(target)


def is_trading_day(target_date: DateLike = None) -> bool:
    """同步判断指定日期是否为交易日（三级降级）。

    降级顺序：
    1. DB trading_calendar 表
    2. Mootdx Provider 在线判断
    3. weekday 判断（周六日非交易日）

    注意：在异步上下文（如 FastAPI 请求处理）中应使用 is_trading_day_async，
    避免 asyncio.run 与现有事件循环冲突。

    Args:
        target_date: 日期对象、字符串(YYYY-MM-DD)、datetime 或 None（默认上海业务日期）

    Returns:
        True 表示交易日，False 表示非交易日（始终返回 bool，不抛异常）
    """
    target = _parse_date(target_date)

    # 第一级：DB 查询
    result, _, _ = _check_database_sync(target)
    if result is not None:
        return result

    # 第二级：Mootdx 在线查询
    result = _check_mootdx_online(target)
    if result is not None:
        return result

    # 第三级：weekday 判断
    return _check_weekday(target)


if __name__ == "__main__":
    # 自测入口：验证日期解析与降级链（不写库表）
    print("=== calendar_service 自测 ===")

    # 测试日期解析
    test_cases = [None, "2026-04-04", date(2026, 5, 1), datetime(2026, 10, 1)]
    for tc in test_cases:
        parsed = _parse_date(tc)
        print(f"_parse_date({tc!r}) = {parsed}")

    # 测试 weekday 降级（不依赖 DB/Mootdx）
    weekday_tests = [
        (date(2026, 4, 3), True),   # 周五
        (date(2026, 4, 4), False),  # 周六
        (date(2026, 4, 5), False),  # 周日
        (date(2026, 4, 6), True),   # 周一
    ]
    print("\nweekday 降级测试：")
    for d, expected in weekday_tests:
        result = _check_weekday(d)
        status = "PASS" if result == expected else "FAIL"
        print(f"  {d} ({d.strftime('%a')}): {result} (expected {expected}) [{status}]")

    # 测试 Mootdx 在线判断
    print("\nMootdx 在线判断测试：")
    for d in [date(2026, 6, 29), date(2026, 6, 27), date(2026, 1, 1)]:
        result = _check_mootdx_online(d)
        print(f"  {d}: {'交易日' if result else '非交易日'}")

    # 测试完整 is_trading_day（DB 不可用，将降级到 Mootdx -> weekday）
    print("\nis_trading_day 完整测试（预期使用 Mootdx）：")
    for d in ["2026-04-03", "2026-04-04", "2026-04-05", "2026-04-06"]:
        result = is_trading_day(d)
        print(f"  {d}: {'交易日' if result else '非交易日'}")

    print("=== 自测结束 ===")
