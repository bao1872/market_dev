"""全市场行情质量扫描与修复 CLI（Stage 5 P0）。

用法：
    # canary 模式（5 只已知退市/canary 股票，dry-run）
    cd /root/web_dev/backend && .venv/bin/python -m scripts.market_data_quality_cli \\
        --canary --timeframe 1d --dry-run

    # canary 扫描 + 实际写入
    .venv/bin/python -m scripts.market_data_quality_cli --canary --timeframe 1d

    # 全量扫描（1d，所有活跃 A 股）
    .venv/bin/python -m scripts.market_data_quality_cli --scan --timeframe 1d --all

    # 扫描 + 修复（仅 DB_MISSING 触发修复）
    .venv/bin/python -m scripts.market_data_quality_cli --scan-and-repair \\
        --timeframe 1d --all

    # 指定日期范围
    .venv/bin/python -m scripts.market_data_quality_cli --scan --timeframe 1d \\
        --all --start 2026-01-01 --end 2026-07-30

    # 指定股票
    .venv/bin/python -m scripts.market_data_quality_cli --scan --timeframe 1d \\
        --symbols 000021,000004,002808

    # resume 已有 run
    .venv/bin/python -m scripts.market_data_quality_cli --scan --timeframe 1d \\
        --all --resume

参数：
    --scan / --repair / --scan-and-repair: 操作模式（互斥，至少一个）
    --timeframe: 1d 或 15m
    --symbols: 逗号分隔的股票代码列表（默认全部活跃 A 股）
    --all: 全量扫描（与 --symbols 互斥）
    --start / --end: 日期范围（ISO 格式）
    --batch-size: 批次大小（scan 默认 50，repair 默认 10）
    --dry-run: 只打印计划不写入（默认 True，用 --no-dry-run 实际写入）
    --resume: 继续已有 run（不存在则新建）
    --limit: 最大标的数（canary 模式默认 5）
    --canary: 快捷模式（--limit 5 --symbols 000021,000004,002808,002898,300029）

约束（AGENTS.md §8）：
- 本 CLI 设计在服务器上对 bz_stock 执行扫描/修复
- 本地只允许 --dry-run 运行
- 修复只写 raw OHLCV，不写 qfq 价格
- 不修改 074 等已有迁移

[P0-CLI语义 2026-07-30] 三种模式语义：
- --dry-run：零持久写（不创建 run/items，只解析 symbols 打印计划）
- --scan：允许写审计 run/items（记录扫描结果），但不改 bars
- --repair：才修改行情数据（写 raw OHLCV，幂等 upsert）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# canary 默认 5 只已知退市/canary 股票
_CANARY_SYMBOLS = "000021,000004,002808,002898,300029"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="全市场行情质量扫描与修复 CLI（Stage 5 P0）",
    )

    # 操作模式（互斥）
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan", action="store_true",
                      help="扫描模式：检测行情质量问题")
    mode.add_argument("--repair", action="store_true",
                      help="修复模式：仅修复 DB_MISSING 的 item")
    mode.add_argument("--scan-and-repair", action="store_true",
                      help="扫描 + 修复：先扫描再修复 DB_MISSING")

    # 标的范围（互斥）
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--symbols", type=str, default=None,
                       help="逗号分隔的股票代码列表（默认全部活跃 A 股）")
    scope.add_argument("--all", action="store_true",
                       help="全量扫描所有活跃 A 股")
    scope.add_argument("--canary", action="store_true",
                       help="canary 模式（5 只已知退市/canary 股票）")

    p.add_argument("--timeframe", choices=["1d", "15m"], required=True,
                   help="周期：1d 或 15m")
    p.add_argument("--start", type=str, default=None,
                   help="起始日期 ISO（默认：1d=2020-01-01，15m=90 天前）")
    p.add_argument("--end", type=str, default=None,
                   help="结束日期 ISO（默认：今天）")
    p.add_argument("--batch-size", type=int, default=None,
                   help="批次大小（scan 默认 50，repair 默认 10）")
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="只打印计划不写入（默认启用）")
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                   help="实际执行写入（默认禁用，需显式开启）")
    p.add_argument("--resume", action="store_true",
                   help="继续已有 run（不存在则新建）")
    p.add_argument("--limit", type=int, default=None,
                   help="最大标的数（canary 默认 5）")

    return p.parse_args()


def _resolve_dates(args: argparse.Namespace) -> tuple[date, date]:
    """解析起始/结束日期。"""
    today = date.today()
    if args.end:
        end_date = date.fromisoformat(args.end)
    else:
        end_date = today

    if args.start:
        start_date = date.fromisoformat(args.start)
    else:
        # 默认：1d 从 2020-01-01，15m 从 90 天前
        if args.timeframe == "1d":
            start_date = date(2020, 1, 1)
        else:
            start_date = end_date - timedelta(days=90)

    if start_date > end_date:
        raise ValueError(
            f"start_date {start_date} 不能晚于 end_date {end_date}"
        )
    return start_date, end_date


async def _run(args: argparse.Namespace) -> int:
    from app.db import AsyncSessionLocal
    from app.services.market_data_quality_service import (
        MarketDataQualityService,
    )

    start_date, end_date = _resolve_dates(args)

    # canary 快捷模式
    if args.canary:
        if args.symbols:
            logger.error("--canary 与 --symbols 互斥")
            return 2
        symbols = _CANARY_SYMBOLS
        limit = args.limit or 5
    else:
        symbols = args.symbols
        limit = args.limit

    # 批次大小默认值
    if args.batch_size is not None:
        scan_batch = args.batch_size
        repair_batch = args.batch_size
    else:
        scan_batch = 50
        repair_batch = 10

    logger.info(
        "[MDQ CLI] mode=%s timeframe=%s range=%s~%s symbols=%s limit=%s "
        "dry_run=%s resume=%s batch(scan=%d repair=%d)",
        "scan-and-repair" if args.scan_and_repair
        else "scan" if args.scan else "repair",
        args.timeframe, start_date, end_date,
        symbols or "ALL", limit, args.dry_run, args.resume,
        scan_batch, repair_batch,
    )

    async with AsyncSessionLocal() as db:
        try:
            results: dict = {}

            # [P0-CLI语义 2026-07-30] dry-run 零持久写：不创建 run/items
            if args.dry_run:
                # 解析 symbols 列表
                if args.canary:
                    resolved_symbols = _CANARY_SYMBOLS.split(",")
                elif symbols:
                    resolved_symbols = [s.strip() for s in symbols.split(",") if s.strip()]
                else:
                    # 查询全部活跃 A 股 symbols（只读）
                    from sqlalchemy import text as sa_text
                    sym_result = await db.execute(
                        sa_text("SELECT symbol FROM instruments WHERE status='active' AND symbol ~ '^[036]' ORDER BY symbol")
                    )
                    resolved_symbols = [r[0] for r in sym_result]
                if limit:
                    resolved_symbols = resolved_symbols[:limit]
                print(
                    f"[DRY-RUN] 计划扫描 {len(resolved_symbols)} 只股票 "
                    f"(timeframe={args.timeframe}, range={start_date}~{end_date}):"
                )
                for s in resolved_symbols[:20]:
                    print(f"  - {s}")
                if len(resolved_symbols) > 20:
                    print(f"  ... 共 {len(resolved_symbols)} 只")
                results["dry_run"] = {
                    "dry_run": True,
                    "timeframe": args.timeframe,
                    "range": [start_date.isoformat(), end_date.isoformat()],
                    "symbols_count": len(resolved_symbols),
                    "symbols_sample": resolved_symbols[:5],
                }
                print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
                return 0

            # 扫描阶段（非 dry-run）
            if args.scan or args.scan_and_repair:
                # 创建或解析 run
                if args.resume:
                    run = await MarketDataQualityService.resolve_run(
                        db,
                        timeframe=args.timeframe,
                        start_date=start_date,
                        end_date=end_date,
                        repair_mode=args.scan_and_repair,
                    )
                    logger.info(
                        "[MDQ CLI] resume run_id=%s status=%s total=%d "
                        "succeeded=%d failed=%d",
                        run.id, run.status, run.total_instruments,
                        run.succeeded_count, run.failed_count,
                    )
                else:
                    run = await MarketDataQualityService.create_run(
                        db,
                        timeframe=args.timeframe,
                        start_date=start_date,
                        end_date=end_date,
                        repair_mode=args.scan_and_repair,
                    )

                # 实际执行扫描（--scan 允许写审计 run/items，不改 bars）
                scan_summary = await MarketDataQualityService.execute_scan(
                    db,
                    run_id=run.id,
                    batch_size=scan_batch,
                    dry_run=False,
                )
                await db.commit()
                results["scan"] = scan_summary

            # 修复阶段（非 dry-run；dry-run 已在前面统一返回）
            if args.repair or args.scan_and_repair:
                # 修复需要先有 run
                if args.scan_and_repair:
                    run_id = run.id
                else:
                    # --repair 单独使用，需要 resume 已有 run
                    if not args.resume:
                        logger.error("--repair 单独使用必须配合 --resume")
                        return 2
                    run = await MarketDataQualityService.resolve_run(
                        db,
                        timeframe=args.timeframe,
                        start_date=start_date,
                        end_date=end_date,
                        repair_mode=True,
                    )
                    run_id = run.id

                repair_summary = (
                    await MarketDataQualityService.execute_repair(
                        db,
                        run_id=run_id,
                        batch_size=repair_batch,
                        dry_run=False,
                    )
                )
                await db.commit()
                results["repair"] = repair_summary

            # 输出结果
            print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
            return 0

        except Exception:
            logger.exception("[MDQ CLI] 失败")
            await db.rollback()
            return 1


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
