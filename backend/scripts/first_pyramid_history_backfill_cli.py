"""第一金字塔历史回补 CLI - Run/Item 接入版（CHANGE-20260729-008）。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.first_pyramid_history_backfill_cli \\
        --symbols 000001,000021 --output-bars 250 --dry-run

    # 全市场 canary（5 只）
    .venv/bin/python -m scripts.first_pyramid_history_backfill_cli --canary

    # 扩大 25 只
    .venv/bin/python -m scripts.first_pyramid_history_backfill_cli --limit 25

    # 全市场
    .venv/bin/python -m scripts.first_pyramid_history_backfill_cli --all

    # resume 未完成的 run
    .venv/bin/python -m scripts.first_pyramid_history_backfill_cli --resume --history-run-id <uuid>

参数：
    --symbols: 只处理指定股票代码（逗号分隔）
    --limit: 限制处理 instrument 数量（用于 canary/扩大）
    --all: 处理全部 A 股
    --canary: 默认 5 只含深科技（000021）
    --output-bars: 输出最近 N 日（默认 250）
    --batch-size: claim 批次大小（默认 25）
    --algorithm-version: 算法版本（默认 v1，由 FIRST_PYRAMID_CORE_ALGORITHM_VERSION 决定）
    --dry-run: 只打印计划，不执行写入
    --resume: 续跑指定 history_run_id（必须配合 --history-run-id）
    --history-run-id: 指定 resume 的 run ID

约束：
    - DB-only 取数（禁止自动 pytdx 拉取）
    - include_chip=false（chip 由独立 job 异步处理）
    - 单股失败不阻塞其他股票
    - state 幂等更新、events 零重复
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="第一金字塔历史回补 CLI（Run/Item 接入版，DB-only）",
    )
    p.add_argument("--symbols", type=str, default=None,
                   help="只处理指定股票代码（逗号分隔，如 000001,000021）")
    p.add_argument("--limit", type=int, default=None,
                   help="限制处理 instrument 数量（按 symbol ASC）")
    p.add_argument("--all", action="store_true",
                   help="处理全部 A 股")
    p.add_argument("--canary", action="store_true",
                   help="canary 模式（5 只含深科技 000021）")
    p.add_argument("--output-bars", type=int, default=250,
                   help="输出最近 N 日（默认 250）")
    p.add_argument("--batch-size", type=int, default=25,
                   help="claim 批次大小（默认 25）")
    p.add_argument("--algorithm-version", type=str, default=None,
                   help="算法版本（默认使用 FIRST_PYRAMID_CORE_ALGORITHM_VERSION）")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印计划，不执行写入")
    p.add_argument("--resume", action="store_true",
                   help="续跑指定 history_run_id")
    p.add_argument("--history-run-id", type=str, default=None,
                   help="指定 resume 的 history_run_id")
    return p.parse_args()


async def _resolve_instrument_ids(
    db,
    *,
    symbols: str | None,
    limit: int | None,
    all_a_share: bool,
    canary: bool,
) -> list[uuid.UUID]:
    """根据参数解析 instrument_ids。"""
    from sqlalchemy import select

    from app.models.instrument import Instrument

    stmt = select(Instrument.id, Instrument.symbol).where(Instrument.status == "active")
    # 只取 A 股（沪深股票代码，排除指数/ETF/基金）
    stmt = stmt.where(
        Instrument.symbol.like("0%") | Instrument.symbol.like("3%")
         | Instrument.symbol.like("6%")
    )

    if symbols:
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
        stmt = stmt.where(Instrument.symbol.in_(symbol_list))
    elif canary:
        # 默认 5 只含深科技 000021
        canary_symbols = ["000001", "000021", "600000", "600519", "000002"]
        stmt = stmt.where(Instrument.symbol.in_(canary_symbols))
    elif all_a_share:
        pass  # 不加额外过滤
    elif limit is not None:
        pass  # 加 limit

    stmt = stmt.order_by(Instrument.symbol.asc())
    if limit is not None and not all_a_share:
        stmt = stmt.limit(limit)

    result = await db.execute(stmt)
    rows = result.all()
    return [row[0] for row in rows]


async def _amain() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 验证参数互斥
    sel_count = sum([
        bool(args.symbols), args.all, args.canary, args.limit is not None,
    ])
    if sel_count == 0 and not args.resume:
        logger.error("必须指定 --symbols / --limit / --all / --canary / --resume 之一")
        return 2
    if sel_count > 1 and not args.resume:
        logger.error("--symbols / --limit / --all / --canary 互斥")
        return 2

    # 决定算法版本
    from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION
    algorithm_version = args.algorithm_version or FIRST_PYRAMID_CORE_ALGORITHM_VERSION

    from app.db import AsyncSessionLocal
    from app.services.first_pyramid_history_service import (
        backfill_history_with_run_items,
        create_history_run,
        create_history_run_items,
        get_history_run_progress,
    )

    # Resume 模式：跳过 create_run，直接调 backfill
    if args.resume:
        if not args.history_run_id:
            logger.error("--resume 必须配合 --history-run-id")
            return 2
        history_run_id = uuid.UUID(args.history_run_id)
        logger.info("[CLI] Resume history_run_id=%s", history_run_id)
        async with AsyncSessionLocal() as db:
            progress = await get_history_run_progress(db, history_run_id)
        logger.info("[CLI] 当前进度: %s", progress)
    else:
        # 新建 run
        async with AsyncSessionLocal() as db:
            instrument_ids = await _resolve_instrument_ids(
                db,
                symbols=args.symbols,
                limit=args.limit,
                all_a_share=args.all,
                canary=args.canary,
            )

        if not instrument_ids:
            logger.warning("[CLI] 未匹配到任何 instrument，退出")
            return 1

        logger.info(
            "[CLI] 匹配 %d 只 instrument，algorithm_version=%s, output_bars=%d",
            len(instrument_ids), algorithm_version, args.output_bars,
        )

        if args.dry_run:
            logger.info("[CLI] --dry-run 模式，不执行写入")
            return 0

        # 1. 创建 history run
        scope = (
            "symbols" if args.symbols
            else "canary" if args.canary
            else "limit" if args.limit is not None
            else "all_a_share"
        )
        async with AsyncSessionLocal() as db:
            run, is_new = await create_history_run(
                db,
                algorithm_version=algorithm_version,
                output_bars=args.output_bars,
                scope=scope,
                instrument_ids=instrument_ids,
            )
            if is_new:
                await create_history_run_items(db, run.id, instrument_ids)
            await db.commit()
            history_run_id = run.id

        logger.info(
            "[CLI] history_run_id=%s, is_new=%s, expected=%d",
            history_run_id, is_new, len(instrument_ids),
        )

    # 2. 执行回补
    result = await backfill_history_with_run_items(
        history_run_id=history_run_id,
        algorithm_version=algorithm_version,
        output_bars=args.output_bars,
        batch_size=args.batch_size,
    )

    logger.info("[CLI] 完成: %s", result)
    return 0 if result["status"] != "failed" else 1


def main() -> int:
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.warning("中断，run item 状态保留在 DB，可 --resume 继续")
        return 130
    except Exception as exc:
        logger.error("[CLI] 失败: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
