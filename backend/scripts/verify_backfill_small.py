"""小批量回补验证脚本（Task 9.1-9.3）。

验证内容：
1. 检查 DB 中 adj_factor=1.0 的股票数量（回补前基线）
2. 对 5 只股票执行日线回补（最近 30 天）
3. 验证回补后 adj_factor 非 1.0（除最新日期外）
4. 验证回补后数据可通过 reconcile_instrument 对账

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.verify_backfill_small

Side Effects:
    写入 DB（upsert 幂等，可重复执行）
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.repositories.bar_repository import refresh_daily_bars
from app.services.reconcile_bars import reconcile_instrument

# 测试股票（5 只，覆盖 SH/SZ 主板/创业板/科创板）
TEST_SYMBOLS = ["000001", "000002", "600000", "300001", "688001"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("verify_backfill")


async def check_adj_factor_baseline(db: AsyncSession) -> dict:
    """检查 DB 中 adj_factor=1.0 的股票数量（回补前基线）。"""
    # 统计总记录数
    total_stmt = select(func.count()).select_from(BarDaily)
    total_result = await db.execute(total_stmt)
    total_count = int(total_result.scalar() or 0)

    # 统计 adj_factor=1.0 的记录数
    default_stmt = select(func.count()).select_from(BarDaily).where(
        BarDaily.adj_factor == Decimal("1.0")
    )
    default_result = await db.execute(default_stmt)
    default_count = int(default_result.scalar() or 0)

    # 统计 adj_factor!=1.0 的记录数
    adjusted_stmt = select(func.count()).select_from(BarDaily).where(
        BarDaily.adj_factor != Decimal("1.0")
    )
    adjusted_result = await db.execute(adjusted_stmt)
    adjusted_count = int(adjusted_result.scalar() or 0)

    return {
        "total": total_count,
        "default_1_0": default_count,
        "adjusted": adjusted_count,
    }


async def get_test_instruments(db: AsyncSession) -> list[Instrument]:
    """查询测试股票。"""
    stmt = (
        select(Instrument)
        .where(Instrument.symbol.in_(TEST_SYMBOLS))
        .where(Instrument.status == "active")
        .order_by(Instrument.symbol)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def backfill_one_stock(
    db: AsyncSession,
    instrument: Instrument,
    days: int = 30,
) -> dict:
    """对单只股票执行日线回补（最近 N 天）。"""
    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    try:
        df = await refresh_daily_bars(db, instrument.id, start_date, end_date)
        return {
            "symbol": instrument.symbol,
            "name": instrument.name,
            "rows": len(df),
            "success": True,
        }
    except Exception as exc:
        logger.error("回补失败 symbol=%s: %s", instrument.symbol, exc)
        return {
            "symbol": instrument.symbol,
            "name": instrument.name,
            "rows": 0,
            "success": False,
            "error": str(exc),
        }


async def verify_adj_factor(db: AsyncSession, instrument: Instrument) -> dict:
    """验证回补后 adj_factor 非 1.0（除最新日期外）。"""
    end_date = date.today()
    start_date = end_date - timedelta(days=30)

    stmt = (
        select(BarDaily.trade_date, BarDaily.close, BarDaily.adj_factor)
        .where(BarDaily.instrument_id == instrument.id)
        .where(BarDaily.trade_date >= start_date)
        .where(BarDaily.trade_date <= end_date)
        .order_by(BarDaily.trade_date)
    )
    result = await db.execute(stmt)
    rows = result.all()

    if not rows:
        return {"symbol": instrument.symbol, "total": 0, "default_1_0": 0, "adjusted": 0}

    total = len(rows)
    default_count = sum(1 for r in rows if r[2] == Decimal("1.0"))
    adjusted_count = total - default_count

    # 最新日期的 adj_factor=1.0 是正常的（尚未发生除权除息）
    latest_date = rows[-1][0] if rows else None
    latest_adj = rows[-1][2] if rows else None

    return {
        "symbol": instrument.symbol,
        "total": total,
        "default_1_0": default_count,
        "adjusted": adjusted_count,
        "latest_date": str(latest_date) if latest_date else None,
        "latest_adj": float(latest_adj) if latest_adj else None,
    }


async def verify_reconcile(db: AsyncSession, instrument: Instrument) -> dict:
    """验证回补后数据可通过 reconcile_instrument 对账。"""
    end_date = date.today()
    start_date = end_date - timedelta(days=30)

    try:
        result = await reconcile_instrument(
            db,
            instrument_id=instrument.id,
            symbol=instrument.symbol,
            period="d",
            start_date=start_date,
            end_date=end_date,
        )
        return {
            "symbol": instrument.symbol,
            "db_count": result.db_count,
            "source_count": result.source_count,
            "missing": result.missing_count,
            "extra": result.extra_count,
            "mismatch": result.mismatch_count,
        }
    except Exception as exc:
        logger.error("对账失败 symbol=%s: %s", instrument.symbol, exc)
        return {
            "symbol": instrument.symbol,
            "error": str(exc),
        }


async def main() -> None:
    """主函数：小批量回补验证。"""
    print("=" * 60)
    print("Task 9: 小批量回补验证（5 只股票 × 30 天日线）")
    print("=" * 60)

    async with AsyncSessionLocal() as db:
        # 1. 检查回补前基线
        print("\n--- 1. 检查 adj_factor 基线 ---")
        baseline = await check_adj_factor_baseline(db)
        print(f"  DB 日线总记录: {baseline['total']}")
        print(f"  adj_factor=1.0: {baseline['default_1_0']}")
        print(f"  adj_factor!=1.0: {baseline['adjusted']}")

        # 2. 查询测试股票
        print("\n--- 2. 查询测试股票 ---")
        instruments = await get_test_instruments(db)
        if not instruments:
            print("  [WARN] 未找到测试股票，跳过回补验证")
            return
        for inst in instruments:
            print(f"  {inst.symbol} {inst.name} ({inst.market})")

        # 3. 执行日线回补
        print("\n--- 3. 执行日线回补（最近 30 天）---")
        backfill_results = []
        for inst in instruments:
            result = await backfill_one_stock(db, inst, days=30)
            backfill_results.append(result)
            status = "OK" if result["success"] else "FAIL"
            print(f"  [{status}] {result['symbol']} {result['name']}: {result['rows']} rows")

        # 4. 验证 adj_factor
        print("\n--- 4. 验证回补后 adj_factor ---")
        for inst in instruments:
            adj_result = await verify_adj_factor(db, inst)
            print(
                f"  {adj_result['symbol']}: total={adj_result['total']}, "
                f"adj=1.0: {adj_result['default_1_0']}, adjusted: {adj_result['adjusted']}, "
                f"latest: {adj_result['latest_date']} adj={adj_result['latest_adj']}"
            )

        # 5. 验证对账
        print("\n--- 5. 验证对账（reconcile_instrument）---")
        for inst in instruments:
            recon_result = await verify_reconcile(db, inst)
            if "error" in recon_result:
                print(f"  [FAIL] {recon_result['symbol']}: {recon_result['error']}")
            else:
                print(
                    f"  {recon_result['symbol']}: db={recon_result['db_count']}, "
                    f"source={recon_result['source_count']}, "
                    f"missing={recon_result['missing']}, extra={recon_result['extra']}, "
                    f"mismatch={recon_result['mismatch']}"
                )

    print("\n" + "=" * 60)
    print("小批量回补验证完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
