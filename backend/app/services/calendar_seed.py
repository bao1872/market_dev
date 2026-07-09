"""交易日历种子服务 - 从 Mootdx 拉取年度交易日历并写入 trading_calendar 表。

向量化处理：使用 pandas DataFrame 批量构建与去重，executemany 批量插入。
冲突处理：ON CONFLICT (trade_date, market) DO UPDATE SET ...；
    默认不覆盖 source='MANUAL_OVERRIDE' 的记录（force=True 除外）。

提供：
- build_full_year_calendar: 生成全年日历 DataFrame
- seed_calendar_from_mootdx: 拉取并写入数据库

用法：
    from app.services.calendar_seed import seed_calendar_from_mootdx

    # 异步执行（需在异步上下文中调用）
    count = await seed_calendar_from_mootdx(session, year=2026)

副作用：写入 trading_calendar 表（INSERT，冲突时更新 is_trading_day/source/status/verified_at）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import now_utc
from app.models.calendar import TradingCalendar
from app.services.mootdx_calendar_provider import (
    build_calendar_for_year,
    validate_historical_calendar,
)

logger = __import__("logging").getLogger(__name__)


def build_full_year_calendar(year: int) -> pd.DataFrame:
    """生成年度日历（含非交易日），标记 is_trading_day/source/status。

    完全基于 Mootdx Provider 的 build_calendar_for_year 结果，不再使用
    pytdx 指数 K 线或其他外部交易日历数据源。

    Args:
        year: 年份

    Returns:
        DataFrame，列：trade_date, is_trading_day, market, source, status
    """
    provider_df = build_calendar_for_year(year)
    df = provider_df.rename(columns={"date": "trade_date"}).copy()
    df["market"] = "A"
    df = df[["trade_date", "is_trading_day", "market", "source", "status"]]
    return df


async def seed_calendar_from_mootdx(
    session: AsyncSession,
    year: int,
    force: bool = False,
    commit: bool = True,
) -> int:
    """从 Mootdx 拉取年度交易日历并写入 trading_calendar 表。

    流程：
    1. validate_historical_calendar(year) 校验 holidays() 数据可用性
    2. build_full_year_calendar(year) 生成全年日历
    3. ON CONFLICT DO UPDATE 批量 upsert
       - force=False 时不覆盖 source='MANUAL_OVERRIDE' 的记录
       - force=True 时覆盖所有记录
    4. 写入 verified_at=now_utc()

    Args:
        session: 异步数据库会话
        year: 年份，如 2026
        force: 是否覆盖 MANUAL_OVERRIDE 记录，默认 False
        commit: 是否提交事务，默认 True；测试场景可传入 False 以避免污染测试库

    Returns:
        新插入或更新的记录数

    Raises:
        RuntimeError: Mootdx 数据校验失败
        Exception: 数据库写入失败（不吞没）
    """
    logger.info("开始从 Mootdx 拉取交易日历：year=%d, force=%s", year, force)

    validation = validate_historical_calendar(year)
    if not validation["ok"]:
        raise RuntimeError(
            f"Mootdx 历史交易日历校验失败：year={year}, message={validation['message']}"
        )

    df = build_full_year_calendar(year)
    if df.empty:
        logger.warning("Mootdx 生成 %d 年日历为空，跳过写入", year)
        return 0

    verified_at = now_utc()
    records: list[dict[str, Any]] = df.to_dict(orient="records")
    for record in records:
        record["verified_at"] = verified_at

    logger.info(
        "Mootdx 生成 %d 年日历：%d 条（其中交易日 %d 条），开始 upsert",
        year, len(df), int(df["is_trading_day"].sum()),
    )

    stmt = pg_insert(TradingCalendar).values(records)
    update_set = {
        "is_trading_day": stmt.excluded.is_trading_day,
        "source": stmt.excluded.source,
        "status": stmt.excluded.status,
        "verified_at": stmt.excluded.verified_at,
    }
    if force:
        stmt = stmt.on_conflict_do_update(
            index_elements=["trade_date", "market"],
            set_=update_set,
        )
    else:
        # [CalendarSeed] - 描述: force=False 时不覆盖 MANUAL_OVERRIDE 记录
        stmt = stmt.on_conflict_do_update(
            index_elements=["trade_date", "market"],
            set_=update_set,
            where=(TradingCalendar.source != "MANUAL_OVERRIDE"),
        )

    try:
        result = await session.execute(stmt)
        if commit:
            await session.commit()
    except Exception as exc:
        await session.rollback()
        raise RuntimeError(
            f"交易日历写入失败：year={year}, records={len(records)}"
        ) from exc

    if isinstance(result, CursorResult):
        affected = result.rowcount or 0
    else:
        affected = 0
    logger.info("交易日历写入完成：影响 %d 条", affected)
    return affected


if __name__ == "__main__":
    # 自测入口：验证 Mootdx 方式生成日历（不写库）
    print("=== calendar_seed 自测（不写库）===")

    df = build_full_year_calendar(2026)
    print(f"Mootdx 2026 年日历：{len(df)} 行")
    if not df.empty:
        print(df.head(5).to_string(index=False))
        print(f"交易日数量：{df['is_trading_day'].sum()}")
        print(f"source 分布：\n{df['source'].value_counts()}")
        print(f"status 分布：\n{df['status'].value_counts()}")

    print("\n--- 2026-06-29 检查 ---")
    row = df[df["trade_date"] == date(2026, 6, 29)]
    if not row.empty:
        print(row.to_string(index=False))
    else:
        print("未找到 2026-06-29")

    print("=== 自测结束 ===")
