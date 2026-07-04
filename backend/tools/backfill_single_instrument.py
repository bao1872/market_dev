"""单标的行情历史回补工具。

用法：
    set -a && source /etc/market-dev/market.env && set +a
    export DATABASE_URL=postgresql+psycopg://bz:bz@localhost:5432/bz_stock
    export REDIS_URL=redis://localhost:6379/0
    python backend/tools/backfill_single_instrument.py <symbol> [--start-date YYYY-MM-DD]

示例：
    python backend/tools/backfill_single_instrument.py 000100 --start-date 2023-01-01
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.instrument import Instrument
from app.services.bars_scheduler_service import BarsSchedulerService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_single_instrument")


async def backfill(
    symbol: str,
    start_date: date,
) -> None:
    """按 symbol 查询 instrument_id 并串行回补 d/15m/60m 三个周期。"""
    service = BarsSchedulerService()

    async with AsyncSessionLocal() as session:
        instrument_id = await _lookup_instrument_id(session, symbol)
        if instrument_id is None:
            logger.error("未找到 symbol=%s 的 active 标的", symbol)
            sys.exit(1)

        logger.info("开始回补 symbol=%s instrument_id=%s start_date=%s", symbol, instrument_id, start_date)
        result = await service.refresh_one_instrument(
            instrument_id=instrument_id,
            symbol=symbol,
            counts=service.BACKFILL_COUNTS,
            start_date=start_date,
            db_session=session,
        )
        await session.commit()

    logger.info("回补完成 success=%s upsert_counts=%s error=%s", result.success, result.upsert_counts, result.error)
    if not result.success or any(v == 0 for v in result.upsert_counts.values()):
        sys.exit(2)


async def _lookup_instrument_id(session: AsyncSession, symbol: str) -> str | None:
    """根据 symbol 查询 active 标的 id。"""
    stmt = select(Instrument.id).where(Instrument.symbol == symbol, Instrument.status == "active")
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    return str(row) if row else None


def _parse_date(value: str) -> date:
    """命令行日期参数解析。"""
    return date.fromisoformat(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="单标的行情历史回补工具")
    parser.add_argument("symbol", help="股票代码，例如 000100")
    parser.add_argument(
        "--start-date",
        type=_parse_date,
        default=date(2023, 1, 1),
        help="日线回补起始日期（默认 2023-01-01）",
    )
    args = parser.parse_args()
    asyncio.run(backfill(args.symbol, args.start_date))


if __name__ == "__main__":
    main()
