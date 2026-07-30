"""板块分析 V1 CLI - canary + 全量计算入口（CHANGE-20260730-011）。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.board_analysis_cli \\
        --canary --publish

    # 指定类型（industry 或 concept）
    .venv/bin/python -m scripts.board_analysis_cli --canary --type industry

    # 全量计算（行业+概念）
    .venv/bin/python -m scripts.board_analysis_cli --all --publish

    # 指定 trade_date
    .venv/bin/python -m scripts.board_analysis_cli --all --trade-date 2026-07-29

参数：
    --canary: canary 模式（每个类型 5 个板块）
    --all: 全量计算
    --type: 限定类型（industry | concept）
    --limit: 限定板块数（与 --all 配合）
    --trade-date: 指定交易日（默认从最新 stock_core pointer 推断）
    --publish: 计算后发布 coverage>=0.95 的结果（默认 True）
    --no-publish: 不发布，只计算
    --dry-run: 只打印计划，不执行写入

约束：
    - 只读已发布 stock_core pointer 指向的 run
    - 单板块失败不阻塞其他板块
    - coverage < 0.95 时保存 partial 结果但不发布
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="板块分析 V1 CLI（canary + 全量计算入口）",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--canary", action="store_true",
                      help="canary 模式（每个类型 5 个板块）")
    mode.add_argument("--all", action="store_true",
                      help="全量计算（行业+概念）")
    p.add_argument("--type", choices=["industry", "concept"], default=None,
                   help="限定类型（不传两个都计算）")
    p.add_argument("--limit", type=int, default=None,
                   help="限定每个类型的板块数（canary 默认 5）")
    p.add_argument("--trade-date", type=str, default=None,
                   help="业务交易日 ISO（默认从最新 stock_core pointer 推断）")
    p.add_argument("--publish", action="store_true", default=True,
                   help="计算后发布 coverage>=0.95 的结果（默认启用）")
    p.add_argument("--no-publish", dest="publish", action="store_false",
                   help="不发布，只计算")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印计划，不执行写入")
    return p.parse_args()


async def _resolve_trade_date(db, trade_date_str: str | None) -> date:
    """从参数或最新 stock_core pointer 解析 trade_date。"""
    if trade_date_str:
        return date.fromisoformat(trade_date_str)

    from sqlalchemy import select

    from app.models.factor_publication import FactorPublication

    stmt = (
        select(FactorPublication)
        .where(FactorPublication.publication_kind == "stock_core")
        .order_by(FactorPublication.published_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    pub = result.scalar_one_or_none()
    if pub is None:
        raise RuntimeError(
            "无已发布 stock_core pointer，必须先完成盘后核心计算并发布 stock_core"
        )
    return pub.trade_date


async def _run(args: argparse.Namespace) -> int:
    from app.db import AsyncSessionLocal
    from app.services.board_analysis_service import (
        BOARD_ANALYSIS_ALGORITHM_VERSION,
        BOARD_ANALYSIS_MIN_COVERAGE,
        compute_all_boards,
    )

    async with AsyncSessionLocal() as db:
        try:
            trade_date = await _resolve_trade_date(db, args.trade_date)
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 2

        # canary 默认每类型 5 个
        limit = args.limit
        if args.canary and limit is None:
            limit = 5

        # 全量不限制
        if args.all:
            limit = None

        board_type = args.type
        if not (args.canary or args.all):
            logger.error("必须指定 --canary 或 --all")
            return 2

        logger.info(
            "[BoardAnalysis CLI] trade_date=%s, type=%s, limit=%s, publish=%s",
            trade_date, board_type, limit, args.publish,
        )
        logger.info(
            "[BoardAnalysis CLI] 算法版本: %s, 发布门禁: %.2f",
            BOARD_ANALYSIS_ALGORITHM_VERSION, BOARD_ANALYSIS_MIN_COVERAGE,
        )

        try:
            if args.dry_run:
                # 打印板块列表
                from sqlalchemy import select as sa_select

                from app.models.market_board import MarketBoard

                stmt = sa_select(MarketBoard).order_by(MarketBoard.name.asc())
                if board_type:
                    stmt = stmt.where(MarketBoard.type == board_type)
                if limit:
                    stmt = stmt.limit(limit)
                result = await db.execute(stmt)
                boards = result.scalars().all()
                print(f"[DRY-RUN] 计划计算 {len(boards)} 个板块:")
                for b in boards:
                    print(f"  - {b.type:9s}  {b.name}")
                return 0

            result = await compute_all_boards(
                db,
                trade_date,
                board_type=board_type,
                limit=limit,
                publish=args.publish,
            )
            await db.commit()

            # 输出结果
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0 if result["failed"] == 0 else 1
        except Exception:
            logger.exception("[BoardAnalysis CLI] 失败")
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
