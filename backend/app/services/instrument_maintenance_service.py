"""Instrument 维护服务 - 清理长期无日线数据的 active 股票。

将最近 N 天在 bars_daily 中无任何记录的 active 股票标记为 inactive，
让 BarsSchedulerService 的覆盖率计算（denominator = active 股票数）回归真实值，
避免因退市/停牌股票长期保留 active 状态导致覆盖率永远卡在 75% 触发不了 DSA。

权威口径：
- 分子：bars_daily 表中 trade_date 当日不同 instrument_id 数（BarsSchedulerService._check_daily_coverage_and_trigger_dsa）
- 分母：instruments 表中 status='active' 的股票数
- 阈值：coverage >= 0.9 才触发 DSA

清理规则：
- 仅清理 status='active' 的股票
- 排除指数类标的（symbol 以 SH000 / SZ399 开头），保留用于指数引用
- 查询最近 stale_days 天内是否有任何 bars_daily 记录，无记录则标记 inactive
- 支持干跑（dry_run=True）只返回预览不修改数据库

用法：
    from app.services.instrument_maintenance_service import cleanup_inactive_instruments
    result = await cleanup_inactive_instruments(db, stale_days=30, dry_run=True)  # 预览
    result = await cleanup_inactive_instruments(db, stale_days=30)  # 实际清理
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.bar import BarDaily
from app.models.instrument import Instrument

logger = logging.getLogger("instrument_maintenance_service")

# [InstrumentMaintenance] - 指数类标的前缀（不参与清理，保留用于指数引用）
# 注：cleanup_inactive_instruments 已改用 stock_symbol_sql_filter（更全面），
# 此常量保留供历史调用方与文档引用。
_INDEX_PREFIXES = ("SH000", "SZ399")

# [InstrumentMaintenance] - A 股股票代码前缀（按市场分组）
# 与 chanlunpro exchange_tdx.py for_sz/for_sh 规则对齐：
# - SH 6xxxxx: 上交所 A 股（含 688xxx 科创板）
# - SZ 00xxxx/02xxxx/30xxxx: 深交所主板（000/001/003）/ 中小板（002）/ 创业板（300/301/302）
# - BJ 920xxx/83xxxx/87xxxx/88xxxx/43xxxx: 北交所（排除 899xxx 北证指数）
# 参考：ref/chanlun-pro-master/src/chanlun/exchange/exchange_tdx.py::for_sz/for_sh
_STOCK_SYMBOL_PREFIXES_BY_MARKET: dict[str, tuple[str, ...]] = {
    "SH": ("6",),
    "SZ": ("00", "02", "30"),
    "BJ": ("920", "83", "87", "88", "43"),
}


def is_stock_symbol(symbol: str, market: str) -> bool:
    """判断 (symbol, market) 是否为 A 股股票代码（排除指数/基金/ETF）。

    [InstrumentMaintenance] - 描述: 区分股票与指数/基金/ETF，用于覆盖率分母与行情刷新范围

    规则：
    - SH 6xxxxx: True（上交所 A 股，含 688xxx 科创板）
    - SH 000xxx/5xxxxx/880xxx/999xxx: False（指数/基金/ETF）
    - SZ 000xxx/002xxx/300xxx: True（深交所主板/中小板/创业板）
    - SZ 399xxx/159xxx/395xxx: False（指数/ETF/基金）
    - BJ 8xxxxx/4xxxxx/920xxx: True（北交所）

    Args:
        symbol: 股票代码（纯数字，如 '600000'）
        market: 市场（'SH'/'SZ'/'BJ'）

    Returns:
        True 为股票，False 为指数/基金/ETF/未知市场
    """
    if not symbol or not market:
        return False
    prefixes = _STOCK_SYMBOL_PREFIXES_BY_MARKET.get(market)
    if prefixes is None:
        return False
    return symbol.startswith(prefixes)


def stock_symbol_sql_filter(instrument_model: type[Instrument]) -> ColumnElement[bool]:
    """返回 SQLAlchemy 过滤条件：只匹配 A 股股票代码（排除指数/基金/ETF）。

    [InstrumentMaintenance] - 描述: SQL 层过滤股票代码，供 BarsSchedulerService 覆盖率分母与 _get_active_instruments 使用

    规则与 is_stock_symbol 一致（与 chanlunpro for_sz/for_sh 对齐），以 SQLAlchemy 表达式形式提供：
    - SH 6xxxxx: 上交所 A 股（含 688xxx 科创板）
    - SZ 00xxxx / 02xxxx / 30xxxx: 深交所主板（000/001/003）/ 中小板（002）/ 创业板（300/301/302）
    - BJ 920xxx / 83xxxx / 87xxxx / 88xxxx / 43xxxx: 北交所（排除 899xxx 北证指数）

    Args:
        instrument_model: Instrument ORM 模型类

    Returns:
        SQLAlchemy 布尔表达式，可直接传入 .where()
    """
    return or_(
        # SH 6xxxxx（含科创板 688xxx）
        (instrument_model.market == "SH") & (instrument_model.symbol.like("6%")),
        # SZ 00xxxx / 02xxxx / 30xxxx（主板 000/001/003 + 中小板 002 + 创业板 300/301/302）
        (instrument_model.market == "SZ") & or_(
            instrument_model.symbol.like("00%"),
            instrument_model.symbol.like("02%"),
            instrument_model.symbol.like("30%"),
        ),
        # BJ 920xxx / 83xxxx / 87xxxx / 88xxxx / 43xxxx（排除 899xxx 北证指数）
        (instrument_model.market == "BJ") & or_(
            instrument_model.symbol.like("920%"),
            instrument_model.symbol.like("83%"),
            instrument_model.symbol.like("87%"),
            instrument_model.symbol.like("88%"),
            instrument_model.symbol.like("43%"),
        ),
    )


async def cleanup_inactive_instruments(
    db: AsyncSession,
    stale_days: int = 30,
    *,
    today: date | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """清理长期无日线数据的 active 股票。

    [InstrumentMaintenance] - 描述: 将最近 stale_days 天无 bars_daily 记录的 active 股票标记为 inactive

    流程：
    1. 计算 cutoff_date = today - stale_days 天
    2. 查询所有 status='active' 且 symbol 非指数前缀的股票
    3. 子查询：NOT EXISTS (bars_daily WHERE instrument_id=i.id AND trade_date >= cutoff_date)
    4. 命中的股票列表即为待清理股票
    5. dry_run=True 仅返回预览；dry_run=False 批量 UPDATE status='inactive'
    6. 返回 {cleaned_count, cleaned_symbols, remaining_active}

    Args:
        db: 异步数据库会话
        stale_days: 阈值天数，最近 N 天无数据则清理（默认 30）
        today: 基准日期，None 时取 date.today()
        dry_run: True 时只返回预览不修改数据库

    Returns:
        dict:
        - cleaned_count: 清理的股票数量
        - cleaned_symbols: 清理的股票代码列表
        - remaining_active: 清理后剩余 active 股票数
    """
    if today is None:
        today = date.today()
    cutoff_date = today - timedelta(days=stale_days)

    # [InstrumentMaintenance] - 查询待清理股票：
    # status='active' AND symbol 不以指数前缀开头 AND 最近 stale_days 天无 bars_daily 记录
    # 使用 NOT EXISTS 子查询避免 N+1，单条 SQL 完成筛选
    stale_active_stmt = (
        select(Instrument.id, Instrument.symbol)
        .where(Instrument.status == "active")
        .where(stock_symbol_sql_filter(Instrument))
        .where(
            ~select(BarDaily.instrument_id)
            .where(
                BarDaily.instrument_id == Instrument.id,
                BarDaily.trade_date >= cutoff_date,
            )
            .exists()
        )
    )
    result = await db.execute(stale_active_stmt)
    stale_rows = result.fetchall()

    cleaned_symbols = [row.symbol for row in stale_rows]
    cleaned_ids = [row.id for row in stale_rows]
    cleaned_count = len(cleaned_symbols)

    if dry_run:
        # dry_run 模式：不修改数据库，仅查询剩余 active 数量
        remaining_result = await db.execute(
            select(func.count(Instrument.id)).where(Instrument.status == "active")
        )
        remaining_active = remaining_result.scalar() or 0
        logger.info(
            "[InstrumentMaintenance] dry_run 预览: stale_days=%d cutoff_date=%s "
            "would_clean=%d remaining_active=%d",
            stale_days, cutoff_date, cleaned_count, remaining_active,
        )
        return {
            "cleaned_count": cleaned_count,
            "cleaned_symbols": cleaned_symbols,
            "remaining_active": remaining_active,
            "dry_run": True,
        }

    # 实际清理：批量 UPDATE status='inactive'
    if cleaned_count > 0:
        await db.execute(
            update(Instrument)
            .where(Instrument.id.in_(cleaned_ids))
            .values(status="inactive")
        )
        await db.flush()

    # 查询清理后剩余 active 数量
    remaining_result = await db.execute(
        select(func.count(Instrument.id)).where(Instrument.status == "active")
    )
    remaining_active = remaining_result.scalar() or 0

    logger.info(
        "[InstrumentMaintenance] 清理完成: stale_days=%d cutoff_date=%s "
        "cleaned=%d remaining_active=%d",
        stale_days, cutoff_date, cleaned_count, remaining_active,
    )
    return {
        "cleaned_count": cleaned_count,
        "cleaned_symbols": cleaned_symbols,
        "remaining_active": remaining_active,
        "dry_run": False,
    }


if __name__ == "__main__":
    # 自测入口：验证模块导入与函数签名（不连接数据库）
    import inspect

    # 验证 cleanup_inactive_instruments 签名
    sig = inspect.signature(cleanup_inactive_instruments)
    params = set(sig.parameters.keys())
    assert params == {"db", "stale_days", "today", "dry_run"}, (
        f"cleanup_inactive_instruments 参数不匹配: {params}"
    )
    assert sig.parameters["stale_days"].default == 30, "stale_days 默认应为 30"
    assert sig.parameters["today"].default is None, "today 默认应为 None"
    assert sig.parameters["dry_run"].default is False, "dry_run 默认应为 False"
    print(f"cleanup_inactive_instruments 签名 ✓: {sorted(params)}")

    # 验证指数前缀常量
    assert _INDEX_PREFIXES == ("SH000", "SZ399"), (
        f"_INDEX_PREFIXES 应为 ('SH000', 'SZ399')，实际 {_INDEX_PREFIXES}"
    )
    print(f"_INDEX_PREFIXES 常量 ✓: {_INDEX_PREFIXES}")

    print("OK: 模块自测通过")
