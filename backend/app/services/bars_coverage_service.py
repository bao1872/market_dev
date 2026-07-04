"""统一行情覆盖率服务。

设计原则：
- dsa-only、系统概览、bars_scheduler 必须调用本服务，禁止复制 SQL；
- trade_date 缺省时使用 shanghai_business_date()（Asia/Shanghai，非服务器本地 date.today()）；
- 分子：bars_daily 表中 trade_date 当日不同 instrument_id 数（JOIN instruments + stock_symbol_sql_filter，
  排除指数/基金/ETF 残留数据）；
- 分母：instruments 表中 status='active' 且为 A 股股票代码的标的数；
- 返回结构：{trade_date, covered, total, coverage, source}。

口径来源：原 bars_scheduler_service._check_daily_coverage_and_trigger_dsa（权威），
after_close_orchestrator.compute_daily_coverage（纯查询副本），
system_overview_service._compute_bars_coverage（系统概览副本）。
本服务收口三处重复实现。

用法：
    from app.services.bars_coverage_service import BarsCoverageService
    result = await BarsCoverageService.compute_daily_coverage(db, trade_date)

模块自测：
    python -m app.services.bars_coverage_service
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import shanghai_business_date
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.services.instrument_maintenance_service import stock_symbol_sql_filter

logger = logging.getLogger("bars_coverage_service")


class BarsCoverageService:
    """行情覆盖率统一服务。

    所有需要计算 bars_daily 覆盖率的场景 MUST 调用本服务，禁止另写 SQL。
    """

    @staticmethod
    async def get_latest_trade_date(db: AsyncSession) -> date | None:
        """查询最新已落盘的交易日（trade_date <= shanghai_business_date）。

        与 system_overview_service._compute_data_freshness 口径一致：
        过滤 trade_date <= today，避免占位/未来日期干扰。

        Args:
            db: 异步数据库会话

        Returns:
            最新已落盘交易日 date，无数据时返回 None
        """
        today = shanghai_business_date()
        result = await db.scalar(
            select(func.max(BarDaily.trade_date)).where(BarDaily.trade_date <= today)
        )
        return result

    @staticmethod
    async def compute_daily_coverage(
        db: AsyncSession,
        trade_date: date | None = None,
    ) -> dict[str, Any]:
        """计算指定交易日的行情覆盖率。

        - trade_date 为 None 时使用 shanghai_business_date()；
        - covered = 当日 bars_daily 不同 instrument_id 数（仅 A 股）；
        - total = instruments 中 status='active' 且为 A 股股票的标的数；
        - coverage = covered / total（total=0 时返 0.0），已 round(..., 4)，仅用于展示；
        - coverage_raw = covered / total 原始值，供阈值/门禁判断使用，避免四舍五入边缘误判；
        - source = "bars_daily"。

        Args:
            db: 异步数据库会话
            trade_date: 交易日期，None 时使用当前上海业务日期

        Returns:
            {trade_date, covered, total, coverage, coverage_raw, source}
        """
        if trade_date is None:
            trade_date = shanghai_business_date()

        # 分子：bars_daily 当日不同 instrument_id 数（JOIN instruments + stock_symbol_sql_filter）
        # bars_daily 中可能残留指数/基金/ETF 的日线数据，必须过滤
        covered_result = await db.execute(
            select(func.count(func.distinct(BarDaily.instrument_id)))
            .join(Instrument, BarDaily.instrument_id == Instrument.id)
            .where(BarDaily.trade_date == trade_date)
            .where(stock_symbol_sql_filter(Instrument))
        )
        covered = int(covered_result.scalar() or 0)

        # 分母：活跃 A 股股票数（排除指数/基金/ETF）
        total_result = await db.execute(
            select(func.count(Instrument.id))
            .where(Instrument.status == "active")
            .where(stock_symbol_sql_filter(Instrument))
        )
        total = int(total_result.scalar() or 0)

        coverage = covered / total if total > 0 else 0.0

        logger.info(
            "[BarsCoverage] trade_date=%s covered=%d total=%d coverage=%.4f",
            trade_date, covered, total, coverage,
        )

        return {
            "trade_date": trade_date.isoformat(),
            "covered": covered,
            "total": total,
            "coverage": round(coverage, 4),
            "coverage_raw": coverage,
            "source": "bars_daily",
        }


if __name__ == "__main__":
    import asyncio

    async def _self_test() -> None:
        # 自测：验证 trade_date 缺省逻辑（不查询数据库）
        bd = shanghai_business_date()
        print(f"shanghai_business_date: {bd}")
        # 验证返回结构字段（mock db 不可用，仅打印预期结构）
        print("expected keys: trade_date, covered, total, coverage, source")
        print("OK")

    asyncio.run(_self_test())
