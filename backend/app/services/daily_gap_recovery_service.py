"""multi-day 日线缺口历史恢复 owner（R2 / G1B-3B2 后续能力）。

职责边界：
- 一次恢复「截至 ``through`` 的最近 ``lookback_trade_days`` 个交易日」内的**全部**日线
  缺口，严格按日期升序，用「早于目标日且覆盖率完整的 reference day」证明 source
  contract 之后才写入。避免以后再缺两天就手工拼 9/10、9/11。

为什么独立成 service，不塞进 :class:`BarsSchedulerService`：
    该 service 已承担盘后编排（snapshot / 因子 / DSA / Core / Review）。历史缺口恢复
    是独立 owner，有自己的失败边界（还原 + 核验），不应扩大盘后编排的职责。

为什么不直接对 5000+ 走 ``fill_missing_daily_instruments``：
    那是**正常业务主源路径**（pytdx primary），逐股新建连接与逐股 commit。整日空洞
    （``market_wide_gap``）必须复用 :func:`repair_market_wide_daily_gap`（共享 AsyncClient、
    有界并发、chunk 批量 insert）。残余少量缺口才回到 pytdx-first 逐股路径。

契约（fail-closed，无生产绕过开关）：
- 只修 ``trading_calendar`` 认定的交易日，禁止按 weekday 猜周末/节假日。
- 严格升序修复：``09-10`` 修好之后才允许把 ``09-10`` 当作 ``09-11`` 的 reference。
- ``market_wide_gap`` 写入前必须找到**早于目标日**且 coverage >= 阈值的 reference，
  并对其跑 :func:`compare_db_vs_ths_for_date` + :func:`validate_consistency`；
  找不到完整 reference → :class:`DailyGapRecoveryBlockedError`（禁止拿目标日自证）。
- bulk 之后必须再做一次 residual 扫描，残余走 pytdx-first 逐股修复。
- 每个日期修复后用 :func:`find_missing_daily_instruments` **真实核验**；
  provider 返回成功一律不算成功。
- ``dry_run=True`` 不写任何表（也因此不执行 sparse residual —— 它会写库）。
- 某天失败即终止（异常直接传播），不继续修后一天。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.calendar import TradingCalendar
from app.services.daily_gap_repair_service import (
    compare_db_vs_ths_for_date,
    repair_market_wide_daily_gap,
    validate_consistency,
)
from app.services.eod_daily_refresh_service import (
    DailyGap,
    _PytdxBreaker,
    count_active_a_share_instruments,
    count_covered_daily_instruments,
    fill_missing_daily_instruments,
    find_missing_daily_instruments,
    plan_daily_repair,
    scan_daily_continuity,
)

logger = logging.getLogger(__name__)

# reference day 的覆盖率阈值：只有 >= 该值才允许作为「完整交易日」。
_DEFAULT_REFERENCE_COVERAGE_THRESHOLD = 0.99
# 最多向前找多少个交易日；找不到即 fail-closed。
_MAX_REFERENCE_LOOKBACK_TRADE_DAYS = 10

_MODE_MARKET_WIDE = "market_wide_gap"
_MODE_SPARSE = "sparse_symbol_gap"


class DailyGapRecoveryBlockedError(RuntimeError):
    """多日恢复被 fail-closed 门禁阻断（无完整 reference / 一致性门禁失败）。"""


@dataclass
class DailyGapRecoveryDayResult:
    """单个交易日的历史恢复结果。"""

    trade_date: date
    mode: str
    eligible: int
    missing_before: int

    # market-wide 分支：reference + bulk 结果
    reference_trade_date: date | None = None
    bulk_requested: int = 0
    bulk_fetched: int = 0
    bulk_inserted: int = 0

    # residual / sparse 分支（pytdx-first 逐股）
    sparse_attempted: int = 0
    sparse_filled: int = 0

    # 真实核验结果
    missing_after: int = 0
    coverage_after: float = 0.0
    failed_symbols: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.missing_after == 0


@dataclass
class DailyGapRecoveryResult:
    """多日恢复整体结果。"""

    through: date
    gaps_before: list[DailyGap]
    days: list[DailyGapRecoveryDayResult]
    gaps_after: list[DailyGap]

    @property
    def unresolved_dates(self) -> list[date]:
        """恢复后仍然低于连续性阈值的交易日（调用方据此判定整体 FAIL）。"""
        return [g.trade_date for g in self.gaps_after]

    @property
    def is_complete(self) -> bool:
        return not self.gaps_after


async def find_previous_complete_trade_date(
    session: AsyncSession,
    *,
    before: date,
    coverage_threshold: float = _DEFAULT_REFERENCE_COVERAGE_THRESHOLD,
    max_lookback_trade_days: int = _MAX_REFERENCE_LOOKBACK_TRADE_DAYS,
) -> date | None:
    """找 ``before`` 之前最近的「覆盖率 >= 阈值」的交易日。

    唯一事实源：``trading_calendar``（交易日）+ ``bars_daily``（覆盖）
    + active A-share universe（分母口径）。禁止 weekday 猜测。

    返回 ``None`` 表示向前 ``max_lookback_trade_days`` 个交易日内都没有完整交易日
    （调用方必须 fail-closed）。
    """
    candidates = list(
        (
            await session.scalars(
                select(TradingCalendar.trade_date)
                .where(TradingCalendar.market == "A")
                .where(TradingCalendar.is_trading_day.is_(True))
                .where(TradingCalendar.trade_date < before)
                .order_by(TradingCalendar.trade_date.desc())
                .limit(max_lookback_trade_days)
            )
        ).all()
    )
    if not candidates:
        return None

    eligible = await count_active_a_share_instruments(session)
    if eligible <= 0:
        return None

    for candidate in candidates:  # 从最近的往旧找
        covered = await count_covered_daily_instruments(session, candidate)
        coverage = covered / eligible
        if coverage >= coverage_threshold:
            return candidate
    return None


async def _recover_market_wide_day(
    session: AsyncSession,
    trade_date: date,
    *,
    eligible: int,
    missing_before: int,
    reference_coverage_threshold: float,
    dry_run: bool,
) -> DailyGapRecoveryDayResult:
    """整日空洞：reference 门禁 → bulk repair → residual（pytdx-first）。"""
    day = DailyGapRecoveryDayResult(
        trade_date=trade_date,
        mode=_MODE_MARKET_WIDE,
        eligible=eligible,
        missing_before=missing_before,
    )

    reference = await find_previous_complete_trade_date(
        session, before=trade_date, coverage_threshold=reference_coverage_threshold
    )
    if reference is None:
        raise DailyGapRecoveryBlockedError(
            "NO_COMPLETE_REFERENCE_BEFORE: "
            f"target={trade_date} lookback<={_MAX_REFERENCE_LOOKBACK_TRADE_DAYS} "
            f"threshold={reference_coverage_threshold:.2f}"
        )
    day.reference_trade_date = reference

    # 写库前置门禁：只对「早于目标日」的完整 reference 做 A/B；dry_run 无需 report。
    consistency_report = None
    if not dry_run:
        consistency_report = await compare_db_vs_ths_for_date(session, reference)
        validate_consistency(consistency_report)  # 失败抛 SourceConsistencyError

    repair = await repair_market_wide_daily_gap(
        session,
        trade_date,
        consistency_report=consistency_report,
        dry_run=dry_run,
        use_eastmoney_fallback=False,
    )
    day.bulk_requested = repair.requested
    day.bulk_fetched = repair.fetched
    day.bulk_inserted = repair.inserted

    # bulk 之后的残余必须回到 pytdx-first 逐股路径（dry_run 不写库，故跳过）。
    if not dry_run:
        residual = await find_missing_daily_instruments(session, trade_date)
        if residual:
            day.sparse_attempted = len(residual)
            day.sparse_filled = await fill_missing_daily_instruments(
                session, residual, trade_date, breaker=_PytdxBreaker()
            )
    return day


async def _recover_sparse_day(
    session: AsyncSession,
    trade_date: date,
    *,
    eligible: int,
    missing_before: int,
    dry_run: bool,
) -> DailyGapRecoveryDayResult:
    """少量缺口：直接 pytdx-first 逐股修复（不调用 bulk repair）。"""
    day = DailyGapRecoveryDayResult(
        trade_date=trade_date,
        mode=_MODE_SPARSE,
        eligible=eligible,
        missing_before=missing_before,
    )
    if dry_run:
        return day

    missing = await find_missing_daily_instruments(session, trade_date)
    if missing:
        day.sparse_attempted = len(missing)
        day.sparse_filled = await fill_missing_daily_instruments(
            session, missing, trade_date, breaker=_PytdxBreaker()
        )
    return day


async def recover_recent_daily_gaps(
    session: AsyncSession,
    *,
    through: date,
    lookback_trade_days: int = 10,
    dry_run: bool = False,
    reference_coverage_threshold: float = _DEFAULT_REFERENCE_COVERAGE_THRESHOLD,
) -> DailyGapRecoveryResult:
    """恢复「截至 ``through`` 最近 N 个交易日」内的全部日线缺口。

    Args:
        session: 异步 DB 会话（写库 owner）。
        through: 恢复目标上界（含）。只修 <= through 的交易日。
        lookback_trade_days: 向前回看多少个交易日。
        dry_run: True 时只拉取/扫描，不写任何表。
        reference_coverage_threshold: reference day 覆盖率阈值。

    Returns:
        :class:`DailyGapRecoveryResult`；调用方据 ``unresolved_dates`` 判定整体成败。

    Raises:
        DailyGapRecoveryBlockedError: 某 market-wide 缺口找不到完整 reference。
        SourceConsistencyError: reference A/B 门禁失败。
        其他异常（provider/DB）：直接传播，后续日期不再处理。
    """
    gaps_before = sorted(
        await scan_daily_continuity(
            session, through, lookback_trade_days=lookback_trade_days
        ),
        key=lambda g: g.trade_date,
    )
    eligible = await count_active_a_share_instruments(session)

    logger.info(
        "[GAP-RECOVERY] through=%s lookback=%d dry_run=%s eligible=%d gaps_before=%s",
        through,
        lookback_trade_days,
        dry_run,
        eligible,
        [g.trade_date.isoformat() for g in gaps_before],
    )

    days: list[DailyGapRecoveryDayResult] = []
    for gap in gaps_before:  # 已按日期升序：先旧后新
        plan = plan_daily_repair(gap)
        if plan.mode == _MODE_MARKET_WIDE:
            day = await _recover_market_wide_day(
                session,
                gap.trade_date,
                eligible=eligible,
                missing_before=gap.missing_count,
                reference_coverage_threshold=reference_coverage_threshold,
                dry_run=dry_run,
            )
        else:
            day = await _recover_sparse_day(
                session,
                gap.trade_date,
                eligible=eligible,
                missing_before=gap.missing_count,
                dry_run=dry_run,
            )

        still_missing = await find_missing_daily_instruments(session, gap.trade_date)
        day.missing_after = len(still_missing)
        day.failed_symbols = [inst.symbol for inst in still_missing]
        day.coverage_after = (
            (eligible - day.missing_after) / eligible if eligible else 0.0
        )
        days.append(day)

        logger.info(
            "[GAP-RECOVERY] day=%s mode=%s missing_before=%d bulk_inserted=%d "
            "sparse=%d missing_after=%d coverage=%.4f",
            gap.trade_date,
            day.mode,
            day.missing_before,
            day.bulk_inserted,
            day.sparse_attempted,
            day.missing_after,
            day.coverage_after,
        )

    gaps_after = sorted(
        await scan_daily_continuity(
            session, through, lookback_trade_days=lookback_trade_days
        ),
        key=lambda g: g.trade_date,
    )

    result = DailyGapRecoveryResult(
        through=through,
        gaps_before=gaps_before,
        days=days,
        gaps_after=gaps_after,
    )
    logger.info(
        "[GAP-RECOVERY] 完成 through=%s days=%d unresolved=%s",
        through,
        len(days),
        [d.isoformat() for d in result.unresolved_dates],
    )
    return result


__all__ = [
    "DailyGapRecoveryBlockedError",
    "DailyGapRecoveryDayResult",
    "DailyGapRecoveryResult",
    "find_previous_complete_trade_date",
    "recover_recent_daily_gaps",
]
