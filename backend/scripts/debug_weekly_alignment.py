"""调试：验证周线 close 与日线 close 的对齐关系。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.debug_weekly_alignment

验证假设：
- 周线 trade_date 是周期内第一个交易日（前对齐）
- 周线 close 是周期内最后一个交易日的 close
- 正确的验证应查询 [w_date, w_date + 7天) 范围内的日线，取最后一条
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.bar import BarDaily, BarWeekly

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("debug_align")

# 159318 港股通基
INSTRUMENT_ID = uuid.UUID("00b509c6-a573-4c67-bb3b-23323bd5bb2c")


async def main() -> None:
    """调试周线/日线 close 对齐关系。"""
    print("=" * 60)
    print("调试：周线 close 与日线 close 对齐关系")
    print("=" * 60)

    async with AsyncSessionLocal() as db:
        # 1. 取最近 5 条周线
        weekly_stmt = (
            select(BarWeekly.trade_date, BarWeekly.close, BarWeekly.open, BarWeekly.high, BarWeekly.low)
            .where(BarWeekly.instrument_id == INSTRUMENT_ID)
            .order_by(BarWeekly.trade_date.desc())
            .limit(5)
        )
        weekly_result = await db.execute(weekly_stmt)
        weekly_rows = list(weekly_result.all())

        print("\n--- 周线最近 5 条（trade_date 是周一/前对齐）---")
        for w_date, w_close, w_open, w_high, w_low in weekly_rows:
            print(f"  trade_date={w_date} open={w_open} high={w_high} low={w_low} close={w_close}")

        # 2. 对每条周线，查询该周范围内的日线 [w_date, w_date+7)
        print("\n--- 对应日线（[w_date, w_date+7) 范围内）---")
        for w_date, w_close, w_open, w_high, w_low in weekly_rows:
            week_end = w_date + timedelta(days=7)
            daily_stmt = (
                select(BarDaily.trade_date, BarDaily.close, BarDaily.open, BarDaily.high, BarDaily.low)
                .where(BarDaily.instrument_id == INSTRUMENT_ID)
                .where(BarDaily.trade_date >= w_date)
                .where(BarDaily.trade_date < week_end)
                .order_by(BarDaily.trade_date)
            )
            daily_result = await db.execute(daily_stmt)
            daily_rows = list(daily_result.all())

            print(f"\n  周线 trade_date={w_date} close={w_close}")
            print(f"  该周日线明细 ({len(daily_rows)} 条):")
            for d_date, d_close, d_open, d_high, d_low in daily_rows:
                print(f"    {d_date} open={d_open} high={d_high} low={d_low} close={d_close}")

            if daily_rows:
                first = daily_rows[0]
                last = daily_rows[-1]
                # 周线 open 应 = 日线 first open
                open_match = abs(float(w_open) - float(first[2])) < 1e-6 if w_open and first[2] else False
                # 周线 close 应 = 日线 last close
                close_match = abs(float(w_close) - float(last[1])) < 1e-6 if w_close and last[1] else False
                # 周线 high 应 = 日线 max high
                d_high_max = max(float(r[3]) for r in daily_rows if r[3] is not None)
                high_match = abs(float(w_high) - d_high_max) < 1e-6 if w_high else False
                # 周线 low 应 = 日线 min low
                d_low_min = min(float(r[4]) for r in daily_rows if r[4] is not None)
                low_match = abs(float(w_low) - d_low_min) < 1e-6 if w_low else False

                print("  对齐检查:")
                print(f"    open: weekly={w_open} vs daily_first={first[2]} [{'OK' if open_match else 'MISMATCH'}]")
                print(f"    close: weekly={w_close} vs daily_last={last[1]} [{'OK' if close_match else 'MISMATCH'}]")
                print(f"    high: weekly={w_high} vs daily_max={d_high_max} [{'OK' if high_match else 'MISMATCH'}]")
                print(f"    low: weekly={w_low} vs daily_min={d_low_min} [{'OK' if low_match else 'MISMATCH'}]")

    print("\n" + "=" * 60)
    print("调试完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
