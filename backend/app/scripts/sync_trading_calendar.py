"""交易日历年度同步脚本。

用法：
    python -m app.scripts.sync_trading_calendar --year 2026 --dry-run
    python -m app.scripts.sync_trading_calendar --year 2026 --apply
    python -m app.scripts.sync_trading_calendar --apply  # 默认当前上海年份

功能：
- 基于 Mootdx Provider 生成指定年份全年日历
- --dry-run 输出差异报告（新增/修改/可疑日期/MANUAL_OVERRIDE 不覆盖数）
- --apply 将差异写入 trading_calendar 表（不覆盖 MANUAL_OVERRIDE）
- 扫描可疑日期：周一至周五、Mootdx 判定为交易日，但 DB 中 is_trading_day=False 或 status=CLOSED
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import shanghai_business_date
from app.db import AsyncSessionLocal
from app.models.calendar import TradingCalendar
from app.services.calendar_seed import build_full_year_calendar, seed_calendar_from_mootdx
from app.services.mootdx_calendar_provider import CALENDAR_STATUS_CLOSED

logger = logging.getLogger(__name__)


def _default_year() -> int:
    """返回当前上海业务年份。"""
    return shanghai_business_date().year


async def _load_existing_records(
    session: AsyncSession, year: int
) -> dict[date, TradingCalendar]:
    """加载指定年份已存在的 DB 记录。"""
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    result = await session.execute(
        select(TradingCalendar).where(
            TradingCalendar.trade_date >= start,
            TradingCalendar.trade_date <= end,
            TradingCalendar.market == "A",
        )
    )
    rows = result.scalars().all()
    return {row.trade_date: row for row in rows}


def _detect_suspicious_dates(
    mootdx_df: pd.DataFrame, existing: dict[date, TradingCalendar]
) -> list[date]:
    """扫描可疑日期。

    定义：周一至周五、Mootdx 判定为交易日，但 DB 中 is_trading_day=False 或 status=CLOSED。
    """
    suspicious: list[date] = []
    for _, row in mootdx_df.iterrows():
        d = row["trade_date"]
        if d.weekday() >= 5:
            continue
        if not row["is_trading_day"]:
            continue
        db_row = existing.get(d)
        if db_row is None:
            continue
        if db_row.is_trading_day is False or db_row.status == CALENDAR_STATUS_CLOSED:
            suspicious.append(d)
    return sorted(suspicious)


def _count_manual_override_blocks(
    mootdx_df: pd.DataFrame, existing: dict[date, TradingCalendar]
) -> int:
    """计算因 MANUAL_OVERRIDE 而不被覆盖的记录数。"""
    count = 0
    for _, row in mootdx_df.iterrows():
        d = row["trade_date"]
        db_row = existing.get(d)
        if db_row is not None and db_row.source == "MANUAL_OVERRIDE":
            # 只要 Mootdx 结果与 DB 不同，即视为不覆盖
            if (
                db_row.is_trading_day != row["is_trading_day"]
                or db_row.status != row["status"]
                or db_row.source != row["source"]
            ):
                count += 1
    return count


def _compute_diff(
    mootdx_df: pd.DataFrame, existing: dict[date, TradingCalendar]
) -> tuple[int, int]:
    """计算待新增数与待修改数。"""
    to_insert = 0
    to_update = 0
    for _, row in mootdx_df.iterrows():
        d = row["trade_date"]
        db_row = existing.get(d)
        if db_row is None:
            to_insert += 1
        elif (
            db_row.is_trading_day != row["is_trading_day"]
            or db_row.status != row["status"]
            or db_row.source != row["source"]
        ):
            to_update += 1
    return to_insert, to_update


async def _run_sync_with_session(
    session: AsyncSession,
    year: int,
    provider: str,
    dry_run: bool,
    apply: bool,
) -> dict[str, Any]:
    """在已打开的 session 上执行同步逻辑。"""
    if provider != "mootdx":
        raise ValueError(f"不支持的 provider: {provider}，仅支持 mootdx")

    mootdx_df = build_full_year_calendar(year)
    existing = await _load_existing_records(session, year)

    trading_days = int(mootdx_df["is_trading_day"].sum())
    closed_days = int((~mootdx_df["is_trading_day"]).sum())
    unknown_days = int((mootdx_df["status"] == "UNKNOWN").sum())
    to_insert, to_update = _compute_diff(mootdx_df, existing)
    suspicious = _detect_suspicious_dates(mootdx_df, existing)
    manual_override_blocks = _count_manual_override_blocks(mootdx_df, existing)

    report = {
        "year": year,
        "provider": provider,
        "trading_days": trading_days,
        "closed_days": closed_days,
        "unknown_days": unknown_days,
        "db_to_insert": to_insert,
        "db_to_update": to_update,
        "suspicious_dates": suspicious,
        "manual_override_blocks": manual_override_blocks,
    }

    if apply:
        affected = await seed_calendar_from_mootdx(session, year=year, force=False, commit=False)
        report["applied"] = True
        report["affected_rows"] = affected
    else:
        report["applied"] = False
        report["affected_rows"] = 0

    return report


async def run_sync(
    year: int | None = None,
    provider: str = "mootdx",
    dry_run: bool = False,
    apply: bool = False,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """执行同步（可被测试直接调用）。

    Args:
        year: 年份，None 表示当前上海年份
        provider: 仅支持 "mootdx"
        dry_run: 是否只输出报告不写入
        apply: 是否执行写入
        session: 外部传入的数据库会话（测试用），未传入时新建

    Returns:
        报告字典
    """
    if year is None:
        year = _default_year()

    if session is not None:
        return await _run_sync_with_session(session, year, provider, dry_run, apply)

    async with AsyncSessionLocal() as session:
        return await _run_sync_with_session(session, year, provider, dry_run, apply)


def _format_report(report: dict[str, Any]) -> str:
    """将报告格式化为人类可读文本。"""
    lines = [
        f"交易日历同步报告（year={report['year']}, provider={report['provider']}）",
        f"  交易日数: {report['trading_days']}",
        f"  CLOSED 数: {report['closed_days']}",
        f"  UNKNOWN 数: {report['unknown_days']}",
        f"  DB 待新增: {report['db_to_insert']}",
        f"  DB 待修改: {report['db_to_update']}",
        f"  MANUAL_OVERRIDE 不覆盖: {report['manual_override_blocks']}",
    ]
    if report["suspicious_dates"]:
        lines.append(f"  可疑日期 ({len(report['suspicious_dates'])} 条):")
        for d in report["suspicious_dates"]:
            lines.append(f"    - {d} ({d.strftime('%A')})")
    else:
        lines.append("  可疑日期: 无")

    if report["applied"]:
        lines.append(f"  已执行 apply，影响行数: {report['affected_rows']}")
    else:
        lines.append("  未执行 apply（使用 --apply 写入）")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="交易日历年度同步")
    parser.add_argument("--year", type=int, default=None, help="年份，默认当前上海年份")
    parser.add_argument("--provider", type=str, default="mootdx", help="数据源，默认 mootdx")
    parser.add_argument("--dry-run", action="store_true", help="仅输出报告，不写入")
    parser.add_argument("--apply", action="store_true", help="执行写入")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.dry_run and not args.apply:
        parser.error("请指定 --dry-run 或 --apply")

    report = asyncio.run(
        run_sync(year=args.year, provider=args.provider, dry_run=args.dry_run, apply=args.apply)
    )
    print(_format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
