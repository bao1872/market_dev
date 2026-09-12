"""multi-day 日线缺口历史恢复 CLI（R2）。

用法（容器内 / 远程开发运行环境）：
    python -m scripts.recover_daily_gaps --through 2026-09-11
    python -m scripts.recover_daily_gaps --through 2026-09-11 --dry-run
    python -m scripts.recover_daily_gaps --through 2026-09-11 --lookback 10

本地（经注册只读调试隧道，backend/.env）：
    cd backend && .venv/bin/python -m scripts.recover_daily_gaps --through 2026-09-11

语义：
    - 只修 trading_calendar 认定的交易日，升序修复，用前一个完整交易日证明 source。
    - market_wide 缺口走 bulk repair，残余走 pytdx-first 逐股。
    - 默认真实写入（--dry-run 关闭写入，只扫描/拉取）。
    - 无 --force / --skip-validation 这类绕过安全门禁的参数（门禁在 service 内 fail-closed）。

退出码：
    0 = 全部缺口已消除（continuity 通过）
    1 = 仍有未消除的缺口交易日
    2 = 参数错误 / 被 fail-closed 门禁阻断
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

# 本地执行时载入 backend/.env（容器内无该文件 → no-op）。
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:  # noqa: BLE001 - 缺 dotenv/文件时静默（容器依赖环境变量）
    pass

from app.core.pytdx_adapter import PytdxAdapter  # noqa: E402
from app.db import AsyncSessionLocal  # noqa: E402
from app.services.daily_gap_recovery_service import (  # noqa: E402
    DailyGapRecoveryDayResult,
    recover_recent_daily_gaps,
)

logger = logging.getLogger("recover_daily_gaps")


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date: {value!r} (expect YYYY-MM-DD)"
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "恢复最近 N 个交易日的日线缺口"
            "（market-wide bulk repair + pytdx-first residual）"
        )
    )
    parser.add_argument(
        "--through",
        type=_parse_iso_date,
        required=True,
        help="恢复上界交易日（含），格式 YYYY-MM-DD",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=10,
        help="向前回看的交易日数（默认 10）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只扫描/拉取，不写任何表（默认关闭 = 真实写入）",
    )
    return parser


def _day_to_dict(day: DailyGapRecoveryDayResult) -> dict[str, Any]:
    out = asdict(day)
    out["trade_date"] = out["trade_date"].isoformat()
    if out.get("reference_trade_date") is not None:
        out["reference_trade_date"] = out["reference_trade_date"].isoformat()
    return out


async def _run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as session:
        # [G1 缺口修复主源回正] 注入 pytdx adapter：repair_market_wide_daily_gap 与
        # 一致性门禁以 pytdx 为首选主源（失败/无 adapter 时回退同花顺）。
        # adapter 懒连接（get_daily_bars 内按需 connect），单次运行复用同一实例。
        result = await recover_recent_daily_gaps(
            session,
            through=args.through,
            lookback_trade_days=args.lookback,
            dry_run=args.dry_run,
            adapter=PytdxAdapter(),
        )

    report = {
        "through": result.through.isoformat(),
        "dry_run": args.dry_run,
        "gaps_before": [g.trade_date.isoformat() for g in result.gaps_before],
        "gaps_after": [g.trade_date.isoformat() for g in result.gaps_after],
        "unresolved_dates": [d.isoformat() for d in result.unresolved_dates],
        "is_complete": result.is_complete,
        "days": [_day_to_dict(d) for d in result.days],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result.is_complete else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
