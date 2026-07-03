"""一次性数据修复脚本：重新同步 A 股股票，并将未被覆盖的指数/ETF/基金标记为 inactive。

用法：
    docker exec trading-backend python -m app.scripts.fix_instruments_remove_indices [--dry-run]

注意：
- 本脚本为一次性修复脚本，执行后检查无误即可删除，不必提交到 git。
- instruments 表被 bars_daily 等表外键引用，不能物理删除指数记录；
  因此改为：同步 A 股时通过 upsert(symbol) 覆盖 symbol 冲突的指数，
  剩余未被覆盖的指数/ETF/基金标记为 inactive。
- 同步过程会复用 instrument_seed.seed_instruments_from_pytdx，从 pytdx 拉取最新 A 股列表。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.instrument import Instrument
from app.services.instrument_seed import seed_instruments_from_pytdx


async def _count_non_active_stocks(db: AsyncSession) -> int:
    """统计待标记为 inactive 的非 A 股股票记录数。"""
    stmt = select(func.count(Instrument.id)).where(
        (Instrument.status == "active")
        & ~(
            (Instrument.market == "SH") & (Instrument.symbol.op("~")(r"^[6][0-9]{5}$"))
            | (Instrument.market == "SZ") & (Instrument.symbol.op("~")(r"^(00|02|30)[0-9]{4}$"))
            | (Instrument.market == "BJ")
            & (
                Instrument.symbol.op("~")(
                    r"^(920[0-9]{3}|83[0-9]{4}|87[0-9]{4}|88[0-9]{4}|43[0-9]{4})$"
                )
            )
        )
    )
    result = await db.execute(stmt)
    return result.scalar() or 0


async def _deactivate_non_stocks(db: AsyncSession) -> int:
    """将非 A 股股票记录标记为 inactive，返回更新行数。

    物理删除会违反 bars_daily 等表的外键约束，因此只改状态。
    """
    from sqlalchemy import update

    stmt = (
        update(Instrument)
        .where(
            (Instrument.status == "active")
            & ~(
                (Instrument.market == "SH") & (Instrument.symbol.op("~")(r"^[6][0-9]{5}$"))
                | (Instrument.market == "SZ")
                & (Instrument.symbol.op("~")(r"^(00|02|30)[0-9]{4}$"))
                | (Instrument.market == "BJ")
                & (
                    Instrument.symbol.op("~")(
                        r"^(920[0-9]{3}|83[0-9]{4}|87[0-9]{4}|88[0-9]{4}|43[0-9]{4})$"
                    )
                )
            )
        )
        .values(status="inactive")
    )
    result = await db.execute(stmt)
    return result.rowcount or 0  # type: ignore[attr-defined]


async def _run(dry_run: bool) -> int:
    """执行修复流程。

    Args:
        dry_run: True 时只统计不删除/不同步。

    Returns:
        进程退出码（0 成功，1 失败）
    """
    async with AsyncSessionLocal() as db:
        try:
            total_before = (
                await db.execute(select(func.count(Instrument.id)))
            ).scalar() or 0
            non_stock_count = await _count_non_active_stocks(db)
            print(f"instruments 总数: {total_before}")
            print(f"待标记为 inactive 的非 A 股 active 记录: {non_stock_count}")

            if dry_run:
                print("[DRY-RUN] 不执行同步和状态更新")
                await db.rollback()
                return 0

            # 先同步 A 股：upsert(symbol) 会自动覆盖 symbol 冲突的指数记录（如 000032）
            synced = await seed_instruments_from_pytdx(db)
            print(f"同步完成: {synced} 条被插入或更新")

            # 将未被覆盖的非 A 股 active 记录标记为 inactive
            if non_stock_count > 0:
                deactivated = await _deactivate_non_stocks(db)
                print(f"已标记为 inactive 的非 A 股记录: {deactivated}")

            await db.commit()

            total_after = (
                await db.execute(select(func.count(Instrument.id)))
            ).scalar() or 0
            active_count = (
                await db.execute(
                    select(func.count(Instrument.id)).where(Instrument.status == "active")
                )
            ).scalar() or 0
            print(f"instruments 总数: {total_before} -> {total_after}")
            print(f"active A 股数量: {active_count}")

            # 验证 000032
            stmt = select(Instrument.symbol, Instrument.name, Instrument.market, Instrument.status).where(
                Instrument.symbol == "000032"
            )
            result = await db.execute(stmt)
            row = result.fetchone()
            if row:
                print(f"000032 当前记录: {row.symbol} {row.name} {row.market} {row.status}")
            else:
                print("000032 当前无记录")

            return 0
        except Exception as e:
            await db.rollback()
            print(f"错误：{e}", file=sys.stderr)
            return 1


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="删除指数/ETF 并重新同步 A 股股票")
    parser.add_argument("--dry-run", action="store_true", help="仅统计不执行")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
