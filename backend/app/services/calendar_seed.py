"""交易日历种子服务 - 从 Tushare 拉取年度交易日历并写入 trading_calendar 表。

向量化处理：使用 pandas DataFrame 批量构建与去重，executemany 批量插入。
冲突处理：ON CONFLICT (trade_date, market) DO UPDATE SET is_trading_day = EXCLUDED.is_trading_day。

Tushare token 来源：
- 优先从 R6 配置注册表读取（如已实现）
- 否则从环境变量 TUSHARE_TOKEN 读取

提供：
- get_tushare_token: 获取 Tushare token
- fetch_calendar_from_tushare: 从 Tushare 拉取年度日历（DataFrame）
- seed_calendar_from_tushare: 拉取并写入数据库

用法：
    from app.services.calendar_seed import seed_calendar_from_tushare

    # 同步执行（需在同步上下文中调用）
    count = await seed_calendar_from_tushare(session, year=2026)

副作用：写入 trading_calendar 表（INSERT，冲突时更新 is_trading_day）。
"""

from __future__ import annotations

from datetime import date
import logging
import os
from typing import Any

import pandas as pd
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.calendar import TradingCalendar

logger = logging.getLogger(__name__)

# Tushare trade_cal 默认市场标识
# SSE: 上海证券交易所，对应 A 股整体日历
TUSHARE_DEFAULT_EXCHANGE = "SSE"

# [CalendarSeed] - 描述: 已知公共假日近似日期（仅用于抑制工作日非交易日的 WARNING 噪声，非权威）
_KNOWN_HOLIDAYS_2026 = {
    date(2026, 1, 1),    # 元旦
    date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),  # 春节
    date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 23),  # 春节调休
    date(2026, 4, 6),    # 清明
    date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5),  # 劳动节
    date(2026, 6, 19), date(2026, 6, 22),  # 端午
    date(2026, 9, 25),   # 中秋+国庆前
    date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8),  # 国庆
}


def get_tushare_token() -> str | None:
    """获取 Tushare token。

    优先级：
    1. R6 配置注册表（如已实现，TODO: 接入后补充）
    2. 环境变量 TUSHARE_TOKEN

    Returns:
        token 字符串，未配置时返回 None
    """
    # TODO: R6 配置注册表实现后，从此读取 tushare_token
    # 当前仅从环境变量读取
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        logger.warning("TUSHARE_TOKEN 环境变量未设置，Tushare 相关功能不可用")
    return token


def transform_calendar_df(raw_df: pd.DataFrame, market: str = "A") -> pd.DataFrame:
    """将 Tushare trade_cal 原始 DataFrame 转换为 trading_calendar 表结构。

    转换步骤（向量化）：
    1. 列重命名：cal_date -> trade_date, is_open -> is_trading_day
    2. 日期字符串转 date 对象
    3. is_open int -> bool
    4. 补充 market 字段
    5. 去重（按 trade_date + market）

    Args:
        raw_df: Tushare trade_cal 返回的 DataFrame
        market: 市场标识（A 表示 A 股整体）

    Returns:
        转换后的 DataFrame，列：trade_date, is_trading_day, market
    """
    if raw_df.empty:
        return pd.DataFrame(columns=["trade_date", "is_trading_day", "market"])

    df = raw_df.copy()

    # 列重命名
    df = df.rename(columns={"cal_date": "trade_date", "is_open": "is_trading_day"})

    # 日期字符串（YYYYMMDD）转 date 对象（向量化）
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date

    # is_open: int(0/1) -> bool（向量化）
    df["is_trading_day"] = df["is_trading_day"].astype(bool)

    # 补充 market 字段
    df["market"] = market

    # 去重（按 trade_date + market）
    before_dedup = len(df)
    df = df.drop_duplicates(subset=["trade_date", "market"], keep="first")
    after_dedup = len(df)
    if before_dedup != after_dedup:
        logger.info(
            "trade_date+market 去重：%d -> %d（删除 %d 条）",
            before_dedup, after_dedup, before_dedup - after_dedup,
        )

    # 列顺序标准化
    df = df[["trade_date", "is_trading_day", "market"]]
    return df.reset_index(drop=True)


def fetch_calendar_from_tushare(
    year: int,
    exchange: str = TUSHARE_DEFAULT_EXCHANGE,
    token: str | None = None,
) -> pd.DataFrame:
    """从 Tushare trade_cal 接口拉取年度交易日历。

    Args:
        year: 年份，如 2026
        exchange: 交易所标识（SSE 上证、SZSE 深证），默认 SSE
        token: Tushare token，None 表示从 get_tushare_token() 获取

    Returns:
        转换后的 DataFrame，列：trade_date, is_trading_day, market

    Raises:
        RuntimeError: token 未配置或 Tushare 调用失败
    """
    if token is None:
        token = get_tushare_token()
    if not token:
        raise RuntimeError(
            "Tushare token 未配置，请设置 TUSHARE_TOKEN 环境变量或实现 R6 配置注册表"
        )

    import tushare as ts

    start_date = f"{year}0101"
    end_date = f"{year}1231"

    logger.info("从 Tushare 拉取 %d 年交易日历：exchange=%s", year, exchange)
    try:
        pro = ts.pro_api(token)
        raw_df = pro.trade_cal(exchange=exchange, start_date=start_date, end_date=end_date)
    except Exception as exc:
        raise RuntimeError(
            f"Tushare trade_cal 调用失败：year={year}, exchange={exchange}, "
            f"start={start_date}, end={end_date}, error={exc}"
        ) from exc

    if raw_df is None or raw_df.empty:
        logger.warning("Tushare trade_cal 返回空数据：year=%d, exchange=%s", year, exchange)
        return pd.DataFrame(columns=["trade_date", "is_trading_day", "market"])

    logger.info("Tushare 拉取完成：%d 条原始记录", len(raw_df))
    return transform_calendar_df(raw_df, market="A")


async def seed_calendar_from_tushare(
    session: AsyncSession,
    year: int,
    exchange: str = TUSHARE_DEFAULT_EXCHANGE,
    token: str | None = None,
) -> int:
    """从 Tushare 拉取年度交易日历并写入 trading_calendar 表。

    冲突处理：ON CONFLICT (trade_date, market) DO UPDATE SET is_trading_day = EXCLUDED.is_trading_day。
    向量化：pandas 批量构建记录，executemany 批量插入。

    Args:
        session: 异步数据库会话
        year: 年份，如 2026
        exchange: 交易所标识，默认 SSE
        token: Tushare token，None 表示从 get_tushare_token() 获取

    Returns:
        新插入的记录数

    Raises:
        RuntimeError: token 未配置或 Tushare 调用失败
        Exception: 数据库写入失败（不吞没）
    """
    logger.info("开始从 Tushare 拉取交易日历：year=%d", year)

    df = fetch_calendar_from_tushare(year=year, exchange=exchange, token=token)

    if df.empty:
        logger.warning("Tushare 拉取交易日历为空，跳过写入")
        return 0

    logger.info("Tushare 拉取完成，共 %d 条日历记录，开始写入数据库", len(df))

    # 向量化构建插入记录
    records: list[dict[str, Any]] = df.to_dict(orient="records")

    # 使用 PostgreSQL ON CONFLICT DO UPDATE 批量插入（冲突时更新 is_trading_day）
    stmt = pg_insert(TradingCalendar).values(records)
    stmt = stmt.on_conflict_do_update(
        index_elements=["trade_date", "market"],
        set_={"is_trading_day": stmt.excluded.is_trading_day},
    )

    result = await session.execute(stmt)
    await session.commit()

    inserted = result.rowcount or 0
    logger.info("交易日历写入完成：新插入 %d 条，跳过 %d 条（已存在）", inserted, len(records) - inserted)
    return inserted


def fetch_trading_days_from_pytdx(year: int) -> set[date]:
    """从 pytdx 拉取上证指数日线，提取指定年份的交易日集合。

    替代 Tushare 方案：当 TUSHARE_TOKEN 未配置时，通过 pytdx 拉取上证指数
    （market=1/SH, code='999999'）的日线数据，从中提取交易日。
    指数不会停牌，能完整反映所有交易日。

    Args:
        year: 年份，如 2026

    Returns:
        该年份的交易日集合（date 对象）

    Raises:
        RuntimeError: pytdx 连接或拉取失败
    """
    from app.core.pytdx_adapter import connect_pytdx

    logger.info("从 pytdx 拉取上证指数日线，提取 %d 年交易日", year)

    trading_days: set[date] = set()
    with connect_pytdx() as adapter:
        # pytdx get_index_bars 拉取上证指数日线
        # 分页拉取，确保覆盖完整年份
        # 每页 800 条，约 3 年多的交易日，2 页足够覆盖 1 年
        all_dates: list[date] = []
        for page in range(4):  # 最多 4 页 = 3200 条，约 13 年
            start = page * 800
            try:
                data = adapter.api.get_index_bars(9, 1, "999999", start, 800)
            except Exception as exc:
                raise RuntimeError(
                    f"pytdx get_index_bars 拉取失败 page={page}, start={start}: {exc}"
                ) from exc
            if not data:
                break
            for item in data:
                dt_str = item.get("datetime")
                if dt_str:
                    dt = pd.to_datetime(dt_str)
                    all_dates.append(dt.date())
            # 检查是否已覆盖目标年份
            if all_dates and min(all_dates).year < year:
                break

    for d in all_dates:
        if d.year == year:
            trading_days.add(d)

    logger.info("pytdx 提取 %d 年交易日：%d 条", year, len(trading_days))
    return trading_days


def build_full_year_calendar(year: int, trading_days: set[date]) -> pd.DataFrame:
    """生成年度日历（含非交易日），标记 is_trading_day。

    生成到年底，过去日期仅 pytdx 确认的交易日标记为 True，
    未来工作日乐观标记为 True（后续 seed 用权威数据覆盖）。

    合理性校验：对过去工作日（周一至周五）未被 pytdx 标记为交易日的日期
    记 WARNING（保留 pytdx 原始结果，仅提示人工核对交易所公告）。
    已知公共假日（_KNOWN_HOLIDAYS_2026）不告警以避免噪声。

    向量化：使用 pandas date_range 生成日期，isin 判断交易日，
    DatetimeIndex.dayofweek 向量化判断工作日。

    Args:
        year: 年份
        trading_days: 交易日集合

    Returns:
        DataFrame，列：trade_date, is_trading_day, market
    """
    end_date = date(year, 12, 31)
    all_dates_idx = pd.date_range(start=f"{year}-01-01", end=end_date, freq="D")
    df = pd.DataFrame({"trade_date": all_dates_idx.date})
    today = date.today()
    # 过去/今天：仅 pytdx 确认的交易日标记为 True
    # 未来日期：工作日乐观标记为 True（后续 seed 用权威数据覆盖）
    df["is_trading_day"] = df.apply(
        lambda row: row["trade_date"] in trading_days if row["trade_date"] <= today
        else row["trade_date"].weekday() < 5,
        axis=1,
    )
    # [CalendarSeed] - 合理性校验：过去工作日未被 pytdx 标记为交易日时记 WARNING（保留原始结果，仅告警）
    suspect_mask = (
        (df["trade_date"] <= today)
        & (all_dates_idx.dayofweek < 5)
        & (~df["trade_date"].isin(trading_days))
        & (~df["trade_date"].isin(_KNOWN_HOLIDAYS_2026))
    )
    for d in df.loc[suspect_mask, "trade_date"]:
        logger.warning(
            "日历数据异常: %s 是工作日(%s)但 pytdx 未标记为交易日，请核对交易所公告",
            d, d.strftime("%A"),
        )
    df["market"] = "A"
    df = df[["trade_date", "is_trading_day", "market"]]
    return df


async def seed_calendar_from_pytdx(
    session: AsyncSession,
    year: int,
) -> int:
    """从 pytdx 拉取交易日并写入 trading_calendar 表（完整日历，含非交易日）。

    替代 Tushare 方案：当 TUSHARE_TOKEN 未配置时使用。
    冲突处理：ON CONFLICT (trade_date, market) DO UPDATE SET is_trading_day = EXCLUDED.is_trading_day。

    Args:
        session: 异步数据库会话
        year: 年份，如 2026

    Returns:
        新插入的记录数

    Raises:
        RuntimeError: pytdx 连接或拉取失败
        Exception: 数据库写入失败（不吞没）
    """
    logger.info("开始从 pytdx 拉取交易日历：year=%d", year)

    trading_days = fetch_trading_days_from_pytdx(year)
    if not trading_days:
        logger.warning("pytdx 拉取 %d 年交易日为空，跳过写入", year)
        return 0

    df = build_full_year_calendar(year, trading_days)
    logger.info("生成 %d 年完整日历：%d 条（其中交易日 %d 条）",
                year, len(df), int(df["is_trading_day"].sum()))

    records: list[dict[str, Any]] = df.to_dict(orient="records")

    stmt = pg_insert(TradingCalendar).values(records)
    stmt = stmt.on_conflict_do_update(
        index_elements=["trade_date", "market"],
        set_={"is_trading_day": stmt.excluded.is_trading_day},
    )

    result = await session.execute(stmt)
    await session.commit()

    inserted = result.rowcount or 0
    logger.info("交易日历写入完成：新插入 %d 条，跳过 %d 条（已存在）", inserted, len(records) - inserted)
    return inserted


if __name__ == "__main__":
    # 自测入口：小批量验证（不写库表，仅拉取并转换）
    # 优先使用 Tushare（需 TUSHARE_TOKEN），不可用时降级到 pytdx
    print("=== calendar_seed 自测（不写库）===")
    token = get_tushare_token()
    if token:
        try:
            df = fetch_calendar_from_tushare(year=2026, token=token)
            print(f"Tushare 2026 年日历拉取结果：{len(df)} 行")
            if not df.empty:
                print(df.head(5).to_string(index=False))
                print(f"交易日数量：{df['is_trading_day'].sum()}")
        except RuntimeError as e:
            print(f"Tushare 自测失败：{e}")
    else:
        print("TUSHARE_TOKEN 未设置，使用 pytdx 方式自测")
        try:
            trading_days = fetch_trading_days_from_pytdx(2026)
            print(f"pytdx 2026 年交易日：{len(trading_days)} 条")
            if trading_days:
                sorted_days = sorted(trading_days)
                print(f"范围：{sorted_days[0]} ~ {sorted_days[-1]}")
                print(f"含 2026-06-16：{date(2026, 6, 16) in trading_days}")
                df = build_full_year_calendar(2026, trading_days)
                print(f"完整日历：{len(df)} 条，交易日 {int(df['is_trading_day'].sum())} 条")
        except RuntimeError as e:
            print(f"pytdx 自测失败：{e}")

    # build_full_year_calendar 合理性校验自测：构造工作日不在 trading_days 的场景
    # 故意剔除 2026-06-26（周五），验证 WARNING 输出（不验证日志捕获）
    print("--- build_full_year_calendar 合理性校验自测 ---")
    _all_weekdays_2026 = pd.date_range("2026-01-01", "2026-12-31", freq="B").date
    _td_missing = set(_all_weekdays_2026) - {date(2026, 6, 26)}
    _df_test = build_full_year_calendar(2026, _td_missing)
    _row_0626 = _df_test[_df_test["trade_date"] == date(2026, 6, 26)]
    print(f"2026-06-26 标记结果：\n{_row_0626.to_string(index=False)}")
    print("（应见 WARNING: 日历数据异常: 2026-06-26 是工作日(Friday)但 pytdx 未标记为交易日）")
    print("=== 自测结束 ===")
