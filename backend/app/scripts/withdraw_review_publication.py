"""撤销指定交易日的 Review 正式 publication pointer（可审计、幂等）。

用法：
    # dry-run（默认，只读，输出将影响的唯一 pointer）
    python -m app.scripts.withdraw_review_publication \
        --trade-date 2026-07-31 \
        --reason "force 发布的错误 run，门禁本应阻塞" \
        --operator admin@example.com \
        --idempotency-key withdraw-20260731-001

    # 实际执行（需显式 --apply）
    python -m app.scripts.withdraw_review_publication \
        --trade-date 2026-07-31 \
        --reason "..." --operator "..." --idempotency-key "..." --apply

安全合同（P0 安全收口 2026-08-01）：
- 只删除 (market/market/market_review/trade_date) 唯一 pointer；
- 保留 review run / scope / signal / attribution / instrument 全部数据；
- run.status、published_at 与全部子数据保持不变；
- after-close 通过当前正式 pointer 判定可否复用；
- 审计写入 run.metadata_json["publication_withdrawal"]；
- 幂等：pointer 不存在时退出码 0 并报告 already_withdrawn；
- 禁止裸 SQL、禁止删除 Review run。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date

from app.db import AsyncSessionLocal
from app.services.review_publication_service import withdraw_review_publication

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="撤销 Review 正式 publication pointer（默认 dry-run）",
    )
    parser.add_argument(
        "--trade-date", required=True,
        help="业务交易日，格式 YYYY-MM-DD",
    )
    parser.add_argument("--reason", required=True, help="撤销原因（审计）")
    parser.add_argument("--operator", required=True, help="操作者标识（审计）")
    parser.add_argument(
        "--idempotency-key", required=True, help="幂等键（审计）",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="实际执行撤销；缺省为 dry-run（只读）",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> dict:
    trade_date = date.fromisoformat(args.trade_date)
    async with AsyncSessionLocal() as session:
        summary = await withdraw_review_publication(
            session,
            trade_date,
            reason=args.reason,
            operator=args.operator,
            idempotency_key=args.idempotency_key,
            dry_run=not args.apply,
        )
        if args.apply:
            await session.commit()
        else:
            await session.rollback()
    return summary


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args()
    summary = asyncio.run(_run(args))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["already_withdrawn"]:
        print("RESULT: already_withdrawn（pointer 不存在，幂等空转）")
    elif summary["dry_run"]:
        print("RESULT: dry-run（未执行任何写入；加 --apply 实际撤销）")
    else:
        print("RESULT: withdrawn（正式 pointer 已撤销，run 数据保留）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
