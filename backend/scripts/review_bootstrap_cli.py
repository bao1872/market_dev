"""Review Bootstrap CLI - 从 stock_core 历史回填 scope snapshots（P0-6 正式入口）。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.review_bootstrap_cli \\
        --days-back 120 --dry-run

    # 实际写入（需管理员确认）
    .venv/bin/python -m scripts.review_bootstrap_cli --days-back 120 --no-dry-run

    # 指定截止日期
    .venv/bin/python -m scripts.review_bootstrap_cli --end-date 2026-07-25 --no-dry-run

参数：
    --days-back: 回溯天数（默认 120，最低 60）
    --dry-run: 只计算不写入（默认 True）
    --no-dry-run: 实际写入（需管理员确认）
    --end-date: 截止日期（YYYY-MM-DD，默认今天）

退出码：
    0: 成功
    1: 无可 bootstrap 的日期
    2: 参数错误

约束：
    - 默认 dry-run，不写生产数据
    - 幂等：相同 trade_date 已有 bootstrap snapshot 时跳过
    - 不修改 stock_core 数据（只读）
    - 不修改现有 review run（只创建 bootstrap run）
    - 不绕过 publish gate（bootstrap 只补历史，不 force publish）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date

logger = logging.getLogger("review_bootstrap_cli")


async def _run(args: argparse.Namespace) -> int:
    from app.db import AsyncSessionLocal
    from app.services.review_bootstrap_service import bootstrap_history

    end_date = None
    if args.end_date:
        try:
            end_date = date.fromisoformat(args.end_date)
        except ValueError:
            print(f"错误: --end-date 格式应为 YYYY-MM-DD: {args.end_date}", file=sys.stderr)
            return 2

    if args.days_back < 60:
        print(f"错误: --days-back 最低 60，当前 {args.days_back}", file=sys.stderr)
        return 2

    async with AsyncSessionLocal() as session:
        result = await bootstrap_history(
            session,
            end_date=end_date,
            days_back=args.days_back,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            await session.commit()

        print(json.dumps(result, indent=2, default=str))

        if result.get("eligible_dates", 0) == 0:
            print("WARNING: 无可 bootstrap 的日期", file=sys.stderr)
            return 1

        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Review Bootstrap CLI - 从 stock_core 历史回填 scope snapshots",
    )
    parser.add_argument(
        "--days-back", type=int, default=120,
        help="回溯天数（默认 120，最低 60）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="只计算不写入（默认 True）",
    )
    parser.add_argument(
        "--no-dry-run", dest="dry_run", action="store_false",
        help="实际写入（需管理员确认）",
    )
    parser.add_argument(
        "--end-date", type=str, default=None,
        help="截止日期（YYYY-MM-DD，默认今天）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
