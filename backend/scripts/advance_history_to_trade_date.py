"""[HISTORY-CURRENT-DATE-LIFECYCLE-01 §13] canonical history 日推进运行入口。

把指定 canonical history run 的 daily-state 数据集推进到 target trade date。

语义（canonical dataset advancement，非重跑 backfill）：
- 同一 run id，不新建 run
- 只写 target-date state（1x 写放大，非 250x）
- 不 claim / 不修改 run item
- 不写 events 表
- PIT：MDAS end_date / adjustment_as_of = target

用法：
    python -m scripts.advance_history_to_trade_date \
        --run-id be56dcd2-... --trade-date 2026-08-10 [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import uuid
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("advance_history")


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--trade-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--output-bars", type=int, default=250)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印 participating set 规模与 readiness，不写任何数据",
    )
    args = parser.parse_args()

    run_id = uuid.UUID(args.run_id)
    target = datetime.strptime(args.trade_date, "%Y-%m-%d").date()

    from app.db import AsyncSessionLocal
    from app.services.first_pyramid_history_service import (
        advance_history_to_trade_date,
    )

    started = time.monotonic()
    last_log = {"t": started}

    async def progress(payload: dict) -> None:
        now = time.monotonic()
        if now - last_log["t"] < 30:
            return
        last_log["t"] = now
        elapsed = now - started
        done = payload["processed"]
        total = payload["total"] or 1
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        logger.info(
            "progress %s/%s target_states=%s elapsed=%.0fs rate=%.1f/s eta=%.0fs",
            done, total, payload["target_state_count"], elapsed, rate, eta,
        )

    async with AsyncSessionLocal() as session:
        if args.dry_run:
            from sqlalchemy import func, select

            from app.models.first_pyramid_history_run_item import (
                FirstPyramidHistoryRunItem,
            )

            n = (
                await session.execute(
                    select(func.count()).select_from(
                        select(FirstPyramidHistoryRunItem.instrument_id)
                        .where(
                            FirstPyramidHistoryRunItem.history_run_id == run_id,
                            FirstPyramidHistoryRunItem.status == "succeeded",
                        )
                        .subquery()
                    )
                )
            ).scalar_one()
            logger.info("DRY RUN participating succeeded run items = %s", n)
            return 0

        summary = await advance_history_to_trade_date(
            session,
            run_id,
            target,
            output_bars=args.output_bars,
            batch_size=args.batch_size,
            progress_callback=progress,
        )

    elapsed = time.monotonic() - started
    logger.info("=" * 60)
    logger.info("ADVANCE COMPLETE elapsed=%.1fs", elapsed)
    for k in (
        "run_id", "trade_date", "total", "processed",
        "target_state_count", "no_bar", "no_target_state", "failed",
    ):
        logger.info("  %s = %s", k, summary.get(k))
    for item in (summary.get("failed_instruments") or [])[:20]:
        logger.warning("  FAILED %s: %s", item["instrument_id"], item["error"])
    return 0 if summary.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
