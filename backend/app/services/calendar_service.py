"""交易日历服务 - is_trading_day 三级降级判断。

降级策略（与 ref/交易/datasource/trade_calendar.py 保持一致，但调整优先级）：
    1. 查 DB trading_calendar 表（权威来源，含节假日信息）
    2. 如 DB 无数据，查 Tushare trade_cal API（在线兜底）
    3. 如 Tushare 失败，按周末判断（周六日非交易日，最后降级）

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

import logging
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.calendar import TradingCalendar
from app.services.calendar_seed import get_tushare_token

logger = logging.getLogger(__name__)

# 日期输入类型
DateLike = str | date | datetime | None


def _parse_date(target_date: DateLike = None) -> date:
    """将输入解析为 date 对象。

    Args:
        target_date: 日期对象、字符串(YYYY-MM-DD)、datetime 或 None（默认今天）

    Returns:
        date 对象

    Raises:
        ValueError: 不支持的日期类型或格式错误
    """
    if target_date is None:
        return date.today()
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
    注意：此方法可能将节假日误判为交易日，仅在 DB 与 Tushare 均不可用时使用。
    """
    is_weekday = target.weekday() < 5
    logger.warning(
        "交易日判断降级为 weekday 模式：%s -> %s（可能将节假日误判为交易日）",
        target, "交易日" if is_weekday else "非交易日",
    )
    return is_weekday


def _check_tushare(target: date) -> bool | None:
    """通过 Tushare trade_cal API 检查是否为交易日。

    Returns:
        True/False: 已判断
        None: 查询失败或无数据，需降级

    注意：失败时记录 WARNING 并返回 None，不抛异常（降级策略，非异常吞没）。
    """
    token = get_tushare_token()
    if not token:
        logger.warning("Tushare token 未配置，降级到 weekday 判断")
        return None

    try:
        import tushare as ts

        pro = ts.pro_api(token)
        # 查询目标日期所在月，避免拉取全年数据
        date_str = target.strftime("%Y%m%d")
        month_start = target.strftime("%Y%m") + "01"
        df = pro.trade_cal(exchange="SSE", start_date=month_start, end_date=date_str)

        if df is None or df.empty:
            logger.warning("Tushare trade_cal 返回空数据，降级到 weekday 判断：%s", target)
            return None

        row = df[df["cal_date"] == date_str]
        if row.empty:
            logger.warning("Tushare trade_cal 未找到目标日期记录，降级到 weekday 判断：%s", target)
            return None

        return bool(row.iloc[0]["is_open"])
    except Exception as exc:
        # 降级策略：记录 WARNING 并返回 None，由上层降级到 weekday
        # 这不是异常吞没：降级是文档化的业务策略，日志保留了失败原因
        logger.warning("Tushare trade_cal 查询失败，降级到 weekday 判断：%s, error=%s", target, exc)
        return None


async def _check_database_async(session: AsyncSession, target: date) -> bool | None:
    """通过 DB trading_calendar 表检查是否为交易日（异步）。

    Returns:
        True/False: 已判断
        None: 表中无该日期记录，需降级

    注意：查询异常时记录 WARNING 并返回 None（降级策略，非异常吞没）。
    """
    try:
        stmt = select(TradingCalendar.is_trading_day).where(
            TradingCalendar.trade_date == target,
            TradingCalendar.market == "A",
        )
        result = await session.execute(stmt)
        row = result.first()
        if row is None:
            logger.info("DB trading_calendar 无 %s 记录，降级到 Tushare 查询", target)
            return None
        return bool(row[0])
    except Exception as exc:
        # 降级策略：记录 WARNING 并返回 None，由上层降级到 Tushare
        logger.warning("DB trading_calendar 查询失败，降级到 Tushare 查询：%s, error=%s", target, exc)
        return None


def _check_database_sync(target: date) -> bool | None:
    """通过 DB trading_calendar 表检查是否为交易日（同步，内部使用 asyncio.run）。

    Returns:
        True/False: 已判断
        None: 表中无该日期记录或查询失败，需降级
    """
    import asyncio

    async def _query() -> bool | None:
        async with AsyncSessionLocal() as session:
            return await _check_database_async(session, target)

    try:
        return asyncio.run(_query())
    except RuntimeError as exc:
        # 可能已在事件循环中（如 FastAPI 上下文），记录并降级
        logger.warning("DB 同步查询失败（可能已在事件循环中），降级到 Tushare：%s, error=%s", target, exc)
        return None
    except Exception as exc:
        logger.warning("DB 同步查询异常，降级到 Tushare：%s, error=%s", target, exc)
        return None


async def is_trading_day_async(
    session: AsyncSession,
    target_date: DateLike = None,
) -> bool:
    """异步判断指定日期是否为交易日（三级降级）。

    降级顺序：
    1. DB trading_calendar 表（使用传入的 session）
    2. Tushare trade_cal API
    3. weekday 判断（周六日非交易日）

    Args:
        session: 异步数据库会话
        target_date: 日期对象、字符串(YYYY-MM-DD)、datetime 或 None（默认今天）

    Returns:
        True 表示交易日，False 表示非交易日（始终返回 bool，不抛异常）
    """
    target = _parse_date(target_date)

    # 第一级：DB 查询
    result = await _check_database_async(session, target)
    if result is not None:
        return result

    # 第二级：Tushare 查询
    result = _check_tushare(target)
    if result is not None:
        return result

    # 第三级：weekday 判断
    return _check_weekday(target)


def is_trading_day(target_date: DateLike = None) -> bool:
    """同步判断指定日期是否为交易日（三级降级）。

    降级顺序：
    1. DB trading_calendar 表
    2. Tushare trade_cal API
    3. weekday 判断（周六日非交易日）

    注意：在异步上下文（如 FastAPI 请求处理）中应使用 is_trading_day_async，
    避免 asyncio.run 与现有事件循环冲突。

    Args:
        target_date: 日期对象、字符串(YYYY-MM-DD)、datetime 或 None（默认今天）

    Returns:
        True 表示交易日，False 表示非交易日（始终返回 bool，不抛异常）
    """
    target = _parse_date(target_date)

    # 第一级：DB 查询
    result = _check_database_sync(target)
    if result is not None:
        return result

    # 第二级：Tushare 查询
    result = _check_tushare(target)
    if result is not None:
        return result

    # 第三级：weekday 判断
    return _check_weekday(target)


if __name__ == "__main__":
    # 自测入口：验证日期解析与 weekday 降级（不写库表，不依赖外部服务）
    print("=== calendar_service 自测 ===")

    # 测试日期解析
    test_cases = [None, "2026-04-04", date(2026, 5, 1), datetime(2026, 10, 1)]
    for tc in test_cases:
        parsed = _parse_date(tc)
        print(f"_parse_date({tc!r}) = {parsed}")

    # 测试 weekday 降级（不依赖 DB/Tushare）
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

    # 测试完整 is_trading_day（将降级到 weekday，因 DB/Tushare 不可用）
    print("\nis_trading_day 完整测试（预期降级到 weekday）：")
    for d in ["2026-04-03", "2026-04-04", "2026-04-05", "2026-04-06"]:
        result = is_trading_day(d)
        print(f"  {d}: {'交易日' if result else '非交易日'}")

    print("=== 自测结束 ===")
