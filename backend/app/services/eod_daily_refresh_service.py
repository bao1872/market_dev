"""盘后日线快照落库服务（EOD snapshot 后处理 owner）。

职责边界：
- 用全市场 EOD snapshot 先同步 instrument universe（新股发现必须先于行情覆盖率）。
- 批量落当日 raw 日线（不复权；``volume`` 单位统一为 canonical **股**，amount 为元；
  Eastmoney 原始 f5/f56 为「手」，在 provider boundary 已乘 :data:`SHARES_PER_LOT` 转股）。
- snapshot 之后用集合差找缺口，仅对缺失标的走历史 fallback
  （**Eastmoney fqt=0 优先**，与当日 snapshot 同源；pytdx raw 仅作 disaster fallback，且只覆盖 SH/SZ）。
- 对本次新发现的标的做历史补齐（listing_date 或 2023-01-01 起，且止于 T 日之前）。

canonical contract（bars_daily）：
- 仅接受 raw / 不复权 OHLCV；前复权只能通过 adj_factor 读取时计算。
- ``upsert_raw_daily_snapshot`` 在 conflict 时【保留】已有 adj_factor，只更新 OHLCV。
- 历史 fallback 也只写 raw（pytdx raw / Eastmoney fqt=0）；fqt=1 严禁写入。
- Eastmoney 兜底走 ``_persist_eastmoney_raw_daily``（insert-only），
  绝不覆盖既有行及其 adj_factor（Eastmoney 无 xdxr 依据，不得污染前复权体系）。

本模块不修改 15m/60m，也不改数据库 schema，也不触发 DSA/因子重建
（那些由 BarsSchedulerService._run_post_daily_phase 统一负责）。

成功判定契约（禁止「假成功」）：
- 「补 T 日缺口」成功 ⟺ ``bars_daily(instrument_id, T)`` 真实存在，由
  :func:`has_daily_bar` 查询确认；provider 返回非空 DataFrame 不算成功。
- 每次回补后重新求集合差，写入 ``daily_missing_after_fallback``。
"""
from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bar import BarDaily
from app.models.calendar import TradingCalendar
from app.models.instrument import Instrument
from app.services.eod_market_snapshot_provider import (
    EodSnapshotRow,
    SnapshotProviderError,
)
from app.services.instrument_maintenance_service import stock_symbol_sql_filter
from app.services.pinyin_util import compute_pinyin_initials

logger = logging.getLogger(__name__)

# snapshot 首插时给 adj_factor=1；conflict 时禁止用该值覆盖既有因子。
_ADJ_FACTOR_DEFAULT = Decimal("1")
_FALLBACK_BATCH_SIZE = 20
_EM_REQUEST_TIMEOUT = 15.0

# 「补缺当日」只回看最近 N 个自然日；不从头全量重拉。
_MISSING_FILL_LOOKBACK_DAYS = 10
# 新股历史补齐起点（listing_date 未知时）。
_NEW_INSTRUMENT_HISTORY_START = date(2023, 1, 1)
# pytdx 连续失败达到该次数后，本次回补剩余标的跳过 pytdx 直接走 Eastmoney。
_PYTDX_CONSECUTIVE_FAILURE_LIMIT = 3

# ── 快照规模防护 ────────────────────────────────────────────────
# DB 活跃 A 股基数达到该量级后才做比例校验（小基数/空库不适用比例）。
_SANITY_MIN_EXISTING_UNIVERSE = 1000
# snapshot 覆盖比例低于该值即判定「接口筛选失效」→ fail-closed，禁止把几千只
# 股票全部推入 historical fallback（fallback storm）。
_SANITY_MIN_SNAPSHOT_RATIO = 0.90

# ── 日线连续性 ─────────────────────────────────────────────────
# 当日覆盖率低于该阈值视为需要修复的缺口交易日。
_DAILY_COVERAGE_THRESHOLD = 0.90
# 整日空洞的缺失比例阈值：达到即判定 market_wide_gap（本轮只报告不批量回补）。
_MARKET_WIDE_GAP_RATIO = 0.20
_DEFAULT_CONTINUITY_LOOKBACK_TRADE_DAYS = 10


@dataclass
class InstrumentSyncResult:
    """universe 同步结果。"""

    new_instruments: list[Instrument] = field(default_factory=list)
    new_symbols: list[str] = field(default_factory=list)
    updated_symbols: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DailyGap:
    """一个交易日的日线覆盖缺口（只读诊断结果）。"""

    trade_date: date
    covered: int
    eligible: int
    coverage: float
    missing_count: int
    # coverage == 0：整日空洞，P0。max(trade_date) 无法暴露这种洞。
    is_total_gap: bool


class DailyContinuityBlockedError(RuntimeError):
    """[HARD GATE] 日线序列存在整日/大面积缺口，禁止继续盘后 Core。

    为什么是硬门禁而不是 warning：Core（MACD / 趋势 / 结构）建立在连续日线序列
    之上。若 09-10 整根缺失而 09-11 正常，``max(trade_date)`` 仍是 09-11，
    缺口会被完全掩盖 —— 结果是在**缺一根日 K 的序列**上算指标。

    因此无论 daily 数据来自 snapshot、legacy 逐股还是未来 provider，都必须在进入
    ``_run_post_daily_phase``（因子重建 / 审计 / DSA / Core / Review）之前过本门禁。
    今天的 T 日 snapshot 允许已经写库（raw 数据有用，详情页可恢复），但整个盘后任务
    必须失败/可恢复；补好缺口后重跑即可幂等继续。
    """

    def __init__(self, gaps: Sequence[DailyGap]) -> None:
        self.gaps = list(gaps)
        detail = ", ".join(
            f"{g.trade_date}:{g.covered}/{g.eligible}({g.coverage:.1%})"
            for g in self.gaps
        )
        super().__init__(f"DAILY_CONTINUITY_BLOCKED: {detail}")


def is_valid_snapshot_daily_row(row: EodSnapshotRow, trade_date: date) -> bool:
    """单条 snapshot 行是否可以当作 ``trade_date`` 的**有效日线**（唯一合法性 owner）。

    universe 复活判定与 raw 日线落库必须共用本函数，禁止第二套规则。

    拒绝的情形：
    - ``row.trade_date != trade_date``（老时间戳 / 停牌 / 跨日）；
    - 任一 OHLC 缺失或 <= 0；
    - ``high < max(open, close)`` 或 ``low > min(open, close)``（数据异常）；
    - volume / amount 缺失或 < 0。

    注意：``volume == 0``（停牌当日无成交但价格有效）在这里是**允许**的；
    本函数只排除「日期/价格结构/负值」这类不可能为真终值的情况。
    """
    if row.trade_date != trade_date:
        return False

    o, h, lo, c = row.open, row.high, row.low, row.close
    if o is None or h is None or lo is None or c is None:
        return False
    if o <= 0 or h <= 0 or lo <= 0 or c <= 0:
        return False
    if h < max(o, c):
        return False
    if lo > min(o, c):
        return False

    if row.volume is None or row.volume < 0:
        return False
    if row.amount is None or row.amount < 0:
        return False

    return True


@dataclass(frozen=True)
class DailyRepairPlan:
    """缺口修复计划（本轮只产出计划，不执行 market_wide 回补）。"""

    trade_date: date
    missing_count: int
    eligible: int
    missing_ratio: float
    mode: str  # "market_wide_gap" | "sparse_symbol_gap"


def plan_daily_repair(gap: DailyGap) -> DailyRepairPlan:
    """把 :class:`DailyGap` 归类为 market_wide_gap / sparse_symbol_gap。

    market_wide_gap 禁止用「逐股 historical provider」修复（几千次请求），
    必须走 ``daily_gap_repair_service.repair_market_wide_daily_gap``（批量、有界并发）。
    """
    ratio = (gap.missing_count / gap.eligible) if gap.eligible else 0.0
    mode = (
        "market_wide_gap"
        if ratio >= _MARKET_WIDE_GAP_RATIO
        else "sparse_symbol_gap"
    )
    return DailyRepairPlan(
        trade_date=gap.trade_date,
        missing_count=gap.missing_count,
        eligible=gap.eligible,
        missing_ratio=ratio,
        mode=mode,
    )


async def count_active_a_share_instruments(session: AsyncSession) -> int:
    """活跃 A 股股票数（覆盖率分母的唯一口径）。"""
    stmt = (
        select(func.count())
        .select_from(Instrument)
        .where(Instrument.status == "active")
        .where(stock_symbol_sql_filter(Instrument))
    )
    return int(await session.scalar(stmt) or 0)


async def count_daily_bars(
    session: AsyncSession,
    instrument_id: UUID,
    start: date | None = None,
    end: date | None = None,
) -> int:
    """统计某标的在 [start, end] 的行数（用于「是否真的写了」判定）。"""
    stmt = (
        select(func.count())
        .select_from(BarDaily)
        .where(BarDaily.instrument_id == instrument_id)
    )
    if start is not None:
        stmt = stmt.where(BarDaily.trade_date >= start)
    if end is not None:
        stmt = stmt.where(BarDaily.trade_date <= end)
    return int(await session.scalar(stmt) or 0)


async def has_daily_bar(
    session: AsyncSession,
    instrument_id: UUID,
    trade_date: date,
) -> bool:
    """``bars_daily(instrument_id, trade_date)`` 是否真实存在。

    这是「补 T 日成功」的唯一判据。provider 返回非空 DataFrame 只说明
    「调用没抛异常」，不能代表目标交易日已经补好。
    """
    found = await session.scalar(
        select(BarDaily.instrument_id)
        .where(
            BarDaily.instrument_id == instrument_id,
            BarDaily.trade_date == trade_date,
        )
        .limit(1)
    )
    return found is not None


def check_snapshot_universe_sanity(
    snapshot_symbols: Collection[str],
    existing_active_count: int,
) -> None:
    """快照规模防护：接口筛选失效时 fail-closed，避免 fallback storm。

    目的不是阻止正常的 IPO/退市变化，而是防止「clist 筛选规则变化 → 只返回几百只
    → 缺口集合差算成几千只 → 全部送进 historical provider」。

    Raises:
        SnapshotProviderError: 覆盖比例低于阈值。
    """
    if existing_active_count < _SANITY_MIN_EXISTING_UNIVERSE:
        return
    unique = len(set(snapshot_symbols))
    ratio = unique / existing_active_count
    if ratio < _SANITY_MIN_SNAPSHOT_RATIO:
        raise SnapshotProviderError(
            f"snapshot universe suspiciously small: snapshot={unique}, "
            f"existing={existing_active_count}, ratio={ratio:.3f}"
        )


async def get_recent_expected_trade_dates(
    session: AsyncSession,
    through: date,
    limit: int = _DEFAULT_CONTINUITY_LOOKBACK_TRADE_DAYS,
) -> list[date]:
    """取 <= ``through`` 的最近 N 个交易日（升序），唯一事实源是 trading_calendar。

    禁止自己按 weekday 猜周末/节假日。
    """
    rows = await session.scalars(
        select(TradingCalendar.trade_date)
        .where(TradingCalendar.market == "A")
        .where(TradingCalendar.is_trading_day.is_(True))
        .where(TradingCalendar.trade_date <= through)
        .order_by(TradingCalendar.trade_date.desc())
        .limit(limit)
    )
    return sorted(rows.all())


async def count_covered_daily_instruments(
    session: AsyncSession,
    trade_date: date,
) -> int:
    """当日已落库的**活跃 A 股**只数（与覆盖率分母同口径）。"""
    covered_ids = select(BarDaily.instrument_id).where(
        BarDaily.trade_date == trade_date
    )
    stmt = (
        select(func.count())
        .select_from(Instrument)
        .where(Instrument.status == "active")
        .where(stock_symbol_sql_filter(Instrument))
        .where(Instrument.id.in_(covered_ids))
    )
    return int(await session.scalar(stmt) or 0)


async def scan_daily_continuity(
    session: AsyncSession,
    through: date,
    *,
    lookback_trade_days: int = _DEFAULT_CONTINUITY_LOOKBACK_TRADE_DAYS,
    coverage_threshold: float = _DAILY_COVERAGE_THRESHOLD,
) -> list[DailyGap]:
    """扫描最近 N 个交易日的日线覆盖，返回所有低于阈值的缺口（按日期升序）。

    关键点：**不能只看 ``max(trade_date)``**。若 09-10 整日缺失、09-11 正常，
    ``max`` 仍是 09-11，整日空洞会被完全掩盖。本函数逐交易日核对。
    """
    eligible = await count_active_a_share_instruments(session)
    expected = await get_recent_expected_trade_dates(
        session, through, lookback_trade_days
    )

    gaps: list[DailyGap] = []
    for trade_date in expected:
        covered = await count_covered_daily_instruments(session, trade_date)
        coverage = (covered / eligible) if eligible else 0.0
        if coverage < coverage_threshold:
            gaps.append(
                DailyGap(
                    trade_date=trade_date,
                    covered=covered,
                    eligible=eligible,
                    coverage=coverage,
                    missing_count=max(eligible - covered, 0),
                    is_total_gap=covered == 0,
                )
            )
    if gaps:
        logger.warning(
            "[EOD-CONTINUITY] 发现 %d 个缺口交易日（through=%s）: %s",
            len(gaps),
            through,
            ", ".join(
                f"{g.trade_date}(cov={g.coverage:.1%},missing={g.missing_count}"
                f"{',TOTAL_GAP' if g.is_total_gap else ''})"
                for g in gaps
            ),
        )
    return gaps


async def find_missing_daily_instruments(
    session: AsyncSession,
    trade_date: date,
) -> list[Instrument]:
    """求「活跃 A 股」与「当日 bars_daily」的集合差 = 缺失标的。

    使用 stock_symbol_sql_filter 保证覆盖率分母与 _get_active_instruments 一致。
    """
    existing_ids = select(BarDaily.instrument_id).where(
        BarDaily.trade_date == trade_date
    )
    stmt = (
        select(Instrument)
        .where(Instrument.status == "active")
        .where(stock_symbol_sql_filter(Instrument))
        .where(~Instrument.id.in_(existing_ids))
        .order_by(Instrument.symbol)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def sync_instruments_from_eod_snapshot(
    session: AsyncSession,
    snapshot: Sequence[EodSnapshotRow],
    trade_date: date,
) -> InstrumentSyncResult:
    """用 EOD snapshot 同步 instrument universe。

    规则：
    - 新 symbol → INSERT。status 由 :func:`is_valid_snapshot_daily_row` 决定：
      只有拿到**有效 T 日日线**才允许 active，否则先 inactive
      （不把还没真正开始交易的证券塞进覆盖率分母）。
    - 已存在且 name/market 变化 → UPDATE（不覆盖 listing_date，不自动 delist）。
    - 已存在但当前 inactive、且本次拿到**有效 T 日日线** → 恢复 active。
      旧逻辑只在 name/market 变化时才置 active，导致「停牌恢复但名称未变」的股票
      永远回不到 active。
    - snapshot 未出现的股票保持原状（delisting 归现有 maintenance owner）。

    Args:
        trade_date: 目标交易日（用于判定 snapshot 行是否为有效 T 日日线）。

    调用方在返回后负责 ``clear_instruments_cache()`` 并重新读取 universe。
    """

    seen: set[str] = set()
    unique_rows: list[EodSnapshotRow] = []
    for row in snapshot:
        if row.symbol in seen:
            continue
        seen.add(row.symbol)
        unique_rows.append(row)

    if not unique_rows:
        return InstrumentSyncResult()

    symbols = [r.symbol for r in unique_rows]
    existing = (
        await session.execute(
            select(Instrument).where(Instrument.symbol.in_(symbols))
        )
    ).scalars().all()
    existing_by_symbol = {i.symbol: i for i in existing}

    new_records: list[dict] = []
    new_rows: list[EodSnapshotRow] = []
    updated_symbols: list[str] = []

    for row in unique_rows:
        valid_today = is_valid_snapshot_daily_row(row, trade_date)
        inst = existing_by_symbol.get(row.symbol)
        if inst is None:
            new_records.append(
                {
                    "symbol": row.symbol,
                    "name": row.name,
                    "pinyin_initials": compute_pinyin_initials(row.name),
                    "market": row.market,
                    "status": "active" if valid_today else "inactive",
                }
            )
            new_rows.append(row)
            continue

        identity_changed = (inst.name != row.name) or (inst.market != row.market)
        should_reactivate = inst.status != "active" and valid_today

        if identity_changed or should_reactivate:
            inst.name = row.name
            inst.market = row.market
            inst.pinyin_initials = compute_pinyin_initials(row.name)
            if should_reactivate:
                inst.status = "active"
            updated_symbols.append(row.symbol)

    if new_records:
        stmt = pg_insert(Instrument).values(new_records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol"],
            set_={
                "name": stmt.excluded.name,
                "pinyin_initials": stmt.excluded.pinyin_initials,
                "market": stmt.excluded.market,
                "status": stmt.excluded.status,
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)
        await session.commit()

    if updated_symbols:
        await session.commit()

    new_symbols = [r.symbol for r in new_rows]
    new_instruments: list[Instrument] = []
    if new_symbols:
        new_instruments = list(
            (
                await session.execute(
                    select(Instrument).where(Instrument.symbol.in_(new_symbols))
                )
            ).scalars().all()
        )

    return InstrumentSyncResult(
        new_instruments=new_instruments,
        new_symbols=new_symbols,
        updated_symbols=updated_symbols,
    )


async def upsert_raw_daily_snapshot(
    session: AsyncSession,
    trade_date: date,
    rows: Iterable[tuple[UUID, EodSnapshotRow]],
) -> int:
    """批量落当日 raw 日线。conflict 时【保留】原有 adj_factor，只更新 OHLCV。

    入参 rows 为 (instrument_id, EodSnapshotRow) 序列。合法性判定**完全复用**
    :func:`is_valid_snapshot_daily_row`（唯一 owner），禁止在此处维护第二套规则。

    返回成功写入（INSERT+UPDATE）的记录数。
    """
    records: list[dict] = []
    for instrument_id, row in rows:
        if not is_valid_snapshot_daily_row(row, trade_date):
            continue

        records.append(
            {
                "instrument_id": instrument_id,
                "trade_date": trade_date,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "amount": row.amount,
                # 仅首插时使用；conflict 分支不引用该列。
                "adj_factor": _ADJ_FACTOR_DEFAULT,
            }
        )

    if not records:
        return 0

    stmt = pg_insert(BarDaily).values(records)
    stmt = stmt.on_conflict_do_update(
        index_elements=["instrument_id", "trade_date"],
        set_={
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low": stmt.excluded.low,
            "close": stmt.excluded.close,
            "volume": stmt.excluded.volume,
            "amount": stmt.excluded.amount,
            # 关键：conflict 保留既有 adj_factor，禁止用 1.0 覆盖既有前复权体系。
        },
    )
    await session.execute(stmt)
    await session.commit()
    return len(records)


@dataclass
class _PytdxBreaker:
    """pytdx 连续失败熔断器（源故障时避免逐股慢重试拖垮 fast path）。

    只统计「pytdx 取数失败」的连续次数；任意一次成功即复位。
    """

    consecutive_failures: int = 0
    limit: int = _PYTDX_CONSECUTIVE_FAILURE_LIMIT

    @property
    def allow(self) -> bool:
        return self.consecutive_failures < self.limit

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1


async def fill_missing_daily_instruments(
    session: AsyncSession,
    instruments: Sequence[Instrument],
    trade_date: date,
    *,
    lookback_days: int = _MISSING_FILL_LOOKBACK_DAYS,
    breaker: _PytdxBreaker | None = None,
) -> int:
    """补「缺当日日线」的标的：只取 [trade_date-lookback, trade_date] 窄窗口。

    与新股历史补齐（``backfill_new_instruments``）区分：这里标的历史大概率已存在，
    只需补最近几日；避免从 listing_date 全量重拉造成的无谓开销与重写面。

    返回值 = **真正确认 T 日 bar 已存在**的标的数（不是「provider 返了非空数据」）。
    """
    if not instruments:
        return 0
    start = trade_date - timedelta(days=lookback_days)
    return await _backfill_batch(
        session,
        instruments,
        start,
        trade_date,
        breaker=breaker,
        target_trade_date=trade_date,
    )


async def backfill_new_instruments(
    session: AsyncSession,
    new_instruments: Sequence[Instrument],
    trade_date: date,
    *,
    breaker: _PytdxBreaker | None = None,
) -> int:
    """对新发现的标的补齐历史：[listing_date 或 2023-01-01, trade_date]。

    仅针对本次新发现的 symbol，不是每日全市场执行。
    返回值 = 窗口内**真实存在任意日线行**的标的数。

    历史窗口必须**止于 T 日之前**（若该标的的 T 日 bar 已经由当日 snapshot 写入）：
    否则 pytdx 路径（on_conflict_do_update）会用历史口径重新 upsert T 日，覆盖
    canonical EOD 快照写入的当日值。T 日 bar 只允许由当日 canonical 来源拥有。
    """
    from app.services.calendar_service import get_previous_trading_day_async

    if not new_instruments:
        return 0

    # 只在需要时查一次前一交易日（避免逐股查询）。
    previous_trade_date = await get_previous_trading_day_async(session, trade_date)

    written = 0
    items = list(new_instruments)
    for idx in range(0, len(items), _FALLBACK_BATCH_SIZE):
        for inst in items[idx : idx + _FALLBACK_BATCH_SIZE]:
            start = inst.listing_date or _NEW_INSTRUMENT_HISTORY_START
            if start > trade_date:
                start = trade_date

            has_today = await has_daily_bar(session, inst.id, trade_date)
            if has_today:
                if previous_trade_date is None:
                    logger.warning(
                        "新股 symbol=%s T=%s 已有 bar 但无前一交易日，跳过历史补齐"
                        "（禁止覆盖 canonical EOD）",
                        inst.symbol,
                        trade_date,
                    )
                    continue
                end = previous_trade_date
            else:
                end = trade_date

            if start > end:
                # 上市日就是 T 日本身：没有历史可补。
                written += 1 if has_today else 0
                continue

            if await _backfill_one(session, inst, start, end, breaker=breaker):
                written += 1
    return written


async def _did_backfill(
    session: AsyncSession,
    instrument_id: UUID,
    start: date,
    end: date,
    target_trade_date: date | None,
) -> bool:
    """回补是否真的落到了应有的数据（唯一成功判据）。

    - 补目标日缺口：必须是 ``bars_daily(instrument_id, target_trade_date)`` 存在。
    - 新股历史补齐：窗口内至少存在一行。
    """
    if target_trade_date is not None:
        return await has_daily_bar(session, instrument_id, target_trade_date)
    return await count_daily_bars(session, instrument_id, start, end) > 0


async def _backfill_batch(
    session: AsyncSession,
    instruments: Sequence[Instrument],
    start: date,
    end: date,
    *,
    breaker: _PytdxBreaker | None = None,
    target_trade_date: date | None = None,
) -> int:
    written = 0
    items = list(instruments)
    for idx in range(0, len(items), _FALLBACK_BATCH_SIZE):
        for inst in items[idx : idx + _FALLBACK_BATCH_SIZE]:
            if await _backfill_one(
                session,
                inst,
                start,
                end,
                breaker=breaker,
                target_trade_date=target_trade_date,
            ):
                written += 1
    return written


async def _backfill_one(
    session: AsyncSession,
    inst: Instrument,
    start: date,
    end: date,
    *,
    breaker: _PytdxBreaker | None = None,
    target_trade_date: date | None = None,
) -> bool:
    """单只标的回补。**Eastmoney fqt=0 优先，pytdx raw 仅作 disaster fallback。**

    数据源一致性：每日 EOD 的 canonical raw 来源是 Eastmoney（snapshot 与 historical
    同源），因此 sparse 缺口与新股历史也优先走 Eastmoney。pytdx 只在 Eastmoney 拿不到
    数据时兜底（SH/SZ），且它是当前故障源，必须放在后面。

    返回**由 DB 查询确认**的成功（见 :func:`_did_backfill`），不是 provider 是否
    返回了数据。

    北交所：pytdx 标准接口不覆盖 BSE，直接走 Eastmoney。这既省掉一次必然失败的
    连接，也避免 BJ 连续失败把共享 breaker 熔断、连带影响后面的沪深标的。
    """
    from app.repositories.bar_repository import refresh_daily_bars
    from app.services.eod_market_snapshot_provider import fetch_eastmoney_daily_kline

    symbol = inst.symbol
    market = inst.market
    use_pytdx = market in ("SH", "SZ")

    # 1) Eastmoney fqt=0 primary（insert-only：绝不覆盖既有行 / 既有 adj_factor）
    try:
        async with httpx.AsyncClient(timeout=_EM_REQUEST_TIMEOUT) as client:
            recs = await fetch_eastmoney_daily_kline(client, symbol, market, start, end)
        if recs:
            await _persist_eastmoney_raw_daily(session, inst.id, symbol, recs)
    except SnapshotProviderError as exc:
        logger.warning("Eastmoney 回补失败 symbol=%s: %s", symbol, exc)
    except Exception as exc:
        logger.warning("Eastmoney 回补异常 symbol=%s: %s", symbol, exc)

    if await _did_backfill(session, inst.id, start, end, target_trade_date):
        return True

    # 2) pytdx raw disaster fallback（仅沪深；熔断开启时跳过）
    if use_pytdx and (breaker is None or breaker.allow):
        try:
            df = await refresh_daily_bars(session, inst.id, start, end, adapter=None)
            if df is not None and not df.empty:
                if breaker is not None:
                    breaker.record_success()
            elif breaker is not None:
                breaker.record_failure()
        except Exception as exc:
            if breaker is not None:
                breaker.record_failure()
            logger.warning("pytdx 回补失败 symbol=%s: %s", symbol, exc)

    return await _did_backfill(session, inst.id, start, end, target_trade_date)


async def _persist_eastmoney_raw_daily(
    session: AsyncSession,
    instrument_id: UUID,
    symbol: str,
    records: Sequence[dict],
) -> bool:
    """Eastmoney 历史记录落库（raw、不复权、insert-only）。

    **返回值只表示「执行未出错」，不代表目标交易日已补齐** —— 真正的成功判据是
    调用方随后的 :func:`has_daily_bar` / :func:`_did_backfill` 查询。

    **不复用** ``_upsert_daily_bars``：后者 conflict 时会用（Eastmoney 场景下无 xdxr
    依据的）adj_factor 覆盖既有前复权体系。此处用 ``on_conflict_do_nothing``，
    只补缺失行、绝不触碰既有行的 OHLCV 与 adj_factor。
    """
    import pandas as pd

    from app.repositories.bar_repository import _df_to_upsert_records
    from app.services.bars_validator import validate_bars

    df = pd.DataFrame(records)
    if df.empty:
        return False
    df["adj_factor"] = 1.0

    validation = validate_bars(df, symbol, "d")
    if not validation.is_valid:
        logger.warning(
            "Eastmoney 回补校验失败 symbol=%s errors=%s",
            symbol,
            validation.errors[:5],
        )
        return False

    rows = _df_to_upsert_records(df, instrument_id, is_daily=True)
    if not rows:
        return False

    stmt = pg_insert(BarDaily).values(rows)
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["instrument_id", "trade_date"]
    )
    await session.execute(stmt)
    await session.commit()
    return True
