"""统一行情覆盖率服务。

设计原则：
- 系统概览、bars_scheduler、after-close force?restart_from=daily_ready 必须调用本服务，禁止复制 SQL；
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
from datetime import date, datetime
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import shanghai_business_date
from app.models.bar import Bar15Min, BarDaily
from app.models.instrument import Instrument
from app.services.instrument_maintenance_service import stock_symbol_sql_filter

logger = logging.getLogger("bars_coverage_service")

_SHANGHAI = ZoneInfo("Asia/Shanghai")


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

    @staticmethod
    async def compute_daily_facts_detail(
        db: AsyncSession,
        trade_date: date | None = None,
    ) -> dict[str, Any]:
        """[R1.1b DailyFacts minimal contract] 在 compute_daily_coverage 之上，
        仅用现有 bars_daily / instruments 无副作用派生 V2.1 DailyFacts 字段，
        不新增表 / migration / run / publication / pointer。

        字段来源（全部可直接从现有 DB 事实派生）：
        - eligible_count     = active A 股标的数（= compute_daily_coverage 的 total）
        - daily_ready_count  = 当日有 bars_daily 的 A 股数（= covered）
        - daily_missing_count = total - covered（可派生，非缺 SSOT）
        - coverage_ratio     = covered / total
        - max_bar_date       = bars_daily 全局最大 trade_date（已有查询能力）
        - future_data_count  = bars_daily.trade_date > trade_date 的 A 股 bar 数
                               （数据质量信号，仅上报，不阻断 daily_facts 可用性）
        - adj_factor_valid_count / adj_factor_total_count =
            当日 bars_daily 中 adj_factor 非空且 >0 的计数 / 总数
            （R1.1b 简化：以现有真实事实 adj_factor 合法性替代不存在的
             adjustment_as_of SSOT；不得伪造 as_of/version）
        - source = "bars_daily"

        daily_invalid_count：PRD10 未定义 invalid bar 规则，不自行发明，
        故本方法不计算（标记为真实缺失字段，外部不得假设其存在）。
        """
        if trade_date is None:
            trade_date = shanghai_business_date()

        # 分子 / 分母（与 compute_daily_coverage 同口径）
        covered_result = await db.execute(
            select(func.count(func.distinct(BarDaily.instrument_id)))
            .join(Instrument, BarDaily.instrument_id == Instrument.id)
            .where(BarDaily.trade_date == trade_date)
            .where(stock_symbol_sql_filter(Instrument))
        )
        covered = int(covered_result.scalar() or 0)

        total_result = await db.execute(
            select(func.count(Instrument.id))
            .where(Instrument.status == "active")
            .where(stock_symbol_sql_filter(Instrument))
        )
        total = int(total_result.scalar() or 0)

        # max_bar_date：bars_daily 全局最大 trade_date（已有查询能力，不过滤）
        max_bar_result = await db.execute(select(func.max(BarDaily.trade_date)))
        max_bar_date = max_bar_result.scalar()

        # future_data_count：严格晚于 trade_date 的 A 股 bar 数（数据质量信号）
        future_result = await db.execute(
            select(func.count())
            .select_from(BarDaily)
            .join(Instrument, BarDaily.instrument_id == Instrument.id)
            .where(BarDaily.trade_date > trade_date)
            .where(stock_symbol_sql_filter(Instrument))
        )
        future_data_count = int(future_result.scalar() or 0)

        # adj_factor 合法性：当日 bars_daily 中 adj_factor 非空且 >0 的计数 / 总数
        # （R1.1b 简化：以现有真实 adj_factor 事实替代 adjustment_as_of SSOT）
        adj_total_result = await db.execute(
            select(func.count())
            .select_from(BarDaily)
            .join(Instrument, BarDaily.instrument_id == Instrument.id)
            .where(BarDaily.trade_date == trade_date)
            .where(stock_symbol_sql_filter(Instrument))
        )
        adj_total = int(adj_total_result.scalar() or 0)
        adj_valid_result = await db.execute(
            select(func.count())
            .select_from(BarDaily)
            .join(Instrument, BarDaily.instrument_id == Instrument.id)
            .where(BarDaily.trade_date == trade_date)
            .where(stock_symbol_sql_filter(Instrument))
            .where(BarDaily.adj_factor.isnot(None))
            .where(BarDaily.adj_factor > 0)
        )
        adj_valid = int(adj_valid_result.scalar() or 0)

        coverage = covered / total if total > 0 else 0.0

        return {
            "trade_date": trade_date.isoformat(),
            "eligible_count": total,
            "daily_ready_count": covered,
            "daily_missing_count": total - covered,
            "coverage_ratio": round(coverage, 4),
            "coverage_raw": coverage,
            "max_bar_date": max_bar_date.isoformat() if max_bar_date else None,
            "future_data_count": future_data_count,
            "adj_factor_valid_count": adj_valid,
            "adj_factor_total_count": adj_total,
            "source": "bars_daily",
        }

    @staticmethod
    async def compute_intraday_coverage(
        db: AsyncSession,
        trade_date: date | None = None,
    ) -> dict[str, Any]:
        """[Phase8A-correction] 计算指定交易日的 15m 行情覆盖率与就绪状态（按 instrument 聚合）。

        盘后 checking_coverage 步骤使用，验证 15m 数据就绪。

        算法（按 instrument 聚合，避免全局 max 误判）：
        - eligible_count = 活跃 A 股股票数（status='active' + stock_symbol_sql_filter）
        - per_instrument_last = GROUP BY instrument_id MAX(trade_time) WHERE DATE(trade_time)=trade_date
        - any_bar_count = 有任意 15m bar 的 instrument 数
        - complete_to_close_count = per_instrument_last >= 14:45（Shanghai）的 instrument 数
        - complete_ratio = complete_to_close_count / eligible_count
        - ready = complete_ratio >= 0.9

        关键修正：时间完整性必须进入每只股票的覆盖率分子，不能只看全局最大时间。
        例如 90% 股票只更新到 10:00 + 1 只更新到 15:00 → 全局 max 达标但 complete_ratio 低 → fail。

        Args:
            db: 异步数据库会话
            trade_date: 交易日期，None 时使用当前上海业务日期

        Returns:
            {trade_date, eligible_count, any_bar_count, complete_to_close_count,
             complete_ratio, complete_ratio_raw, earliest_latest_bar,
             latest_latest_bar, cutoff_time, ready, source}
        """
        if trade_date is None:
            trade_date = shanghai_business_date()

        # 分母：活跃 A 股股票数
        eligible_result = await db.execute(
            select(func.count(Instrument.id))
            .where(Instrument.status == "active")
            .where(stock_symbol_sql_filter(Instrument))
        )
        eligible_count = int(eligible_result.scalar() or 0)

        # 按instrument聚合：MAX(trade_time) per instrument_id WHERE DATE(trade_time)=trade_date
        per_instrument_subq = (
            select(
                Bar15Min.instrument_id.label("instrument_id"),
                func.max(Bar15Min.trade_time).label("last_bar_time"),
            )
            .join(Instrument, Bar15Min.instrument_id == Instrument.id)
            .where(func.date(Bar15Min.trade_time) == trade_date)
            .where(stock_symbol_sql_filter(Instrument))
            .group_by(Bar15Min.instrument_id)
            .subquery()
        )

        # 聚合统计：any_bar_count, complete_to_close_count, min/max last_bar
        # cutoff 为上海时间 14:45，DB存储naive Shanghai时间，直接比较
        cutoff_naive = datetime.combine(trade_date, dtime(14, 45))
        stats_result = await db.execute(
            select(
                func.count().label("any_bar_count"),
                func.count(
                    case((per_instrument_subq.c.last_bar_time >= cutoff_naive, 1))
                ).label("complete_to_close_count"),
                func.min(per_instrument_subq.c.last_bar_time).label("earliest_latest_bar"),
                func.max(per_instrument_subq.c.last_bar_time).label("latest_latest_bar"),
            )
        )
        stats_row = stats_result.one()

        any_bar_count = int(stats_row.any_bar_count or 0)
        complete_to_close_count = int(stats_row.complete_to_close_count or 0)

        # min/max last_bar 可能带tzinfo（asyncpg UTC-aware），统一转Shanghai naive
        def _to_shanghai_naive(dt: datetime | None) -> datetime | None:
            if dt is None:
                return None
            if dt.tzinfo is not None:
                return dt.astimezone(_SHANGHAI).replace(tzinfo=None)
            return dt

        earliest_latest_bar = _to_shanghai_naive(stats_row.earliest_latest_bar)
        latest_latest_bar = _to_shanghai_naive(stats_row.latest_latest_bar)

        complete_ratio = (
            complete_to_close_count / eligible_count if eligible_count > 0 else 0.0
        )

        # 就绪判断：完整收盘覆盖率 >= 0.9
        ready = complete_ratio >= 0.9

        logger.info(
            "[BarsCoverage] intraday trade_date=%s eligible=%d any_bar=%d "
            "complete_to_close=%d complete_ratio=%.4f ready=%s "
            "earliest_latest=%s latest_latest=%s cutoff=%s",
            trade_date, eligible_count, any_bar_count,
            complete_to_close_count, complete_ratio, ready,
            earliest_latest_bar, latest_latest_bar, cutoff_naive,
        )

        return {
            "trade_date": trade_date.isoformat(),
            "eligible_count": eligible_count,
            "any_bar_count": any_bar_count,
            "complete_to_close_count": complete_to_close_count,
            "complete_ratio": round(complete_ratio, 4),
            "complete_ratio_raw": complete_ratio,
            "earliest_latest_bar": earliest_latest_bar.isoformat() if earliest_latest_bar else None,
            "latest_latest_bar": latest_latest_bar.isoformat() if latest_latest_bar else None,
            "cutoff_time": cutoff_naive.isoformat(),
            "ready": ready,
            "source": "bars_15min",
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
