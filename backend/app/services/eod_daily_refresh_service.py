"""盘后日线快照落库服务（EOD snapshot 后处理 owner）。

职责边界：
- 用全市场 EOD snapshot 先同步 instrument universe（新股发现必须先于行情覆盖率）。
- 批量落当日 raw 日线（不复权；volume=手，amount=元）。
- snapshot 之后用集合差找缺口，仅对缺失标的走历史 fallback（pytdx raw 优先，
  BJ / pytdx 不可用 → Eastmoney fqt=0）。
- 对本次新发现的标的做历史补齐（listing_date 或 2023-01-01 起）。

canonical contract（bars_daily）：
- 仅接受 raw / 不复权 OHLCV；前复权只能通过 adj_factor 读取时计算。
- ``upsert_raw_daily_snapshot`` 在 conflict 时【保留】已有 adj_factor，只更新 OHLCV。
- 历史 fallback 也只写 raw（pytdx raw / Eastmoney fqt=0）；fqt=1 严禁写入。
- Eastmoney 兜底走 ``_persist_eastmoney_raw_daily``（insert-only），
  绝不覆盖既有行及其 adj_factor（Eastmoney 无 xdxr 依据，不得污染前复权体系）。

本模块不修改 15m/60m，也不改数据库 schema，也不触发 DSA/因子重建
（那些由 BarsSchedulerService._run_post_daily_phase 统一负责）。
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.services.eod_market_snapshot_provider import EodSnapshotRow
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


@dataclass
class InstrumentSyncResult:
    """universe 同步结果。"""

    new_instruments: list[Instrument] = field(default_factory=list)
    new_symbols: list[str] = field(default_factory=list)
    updated_symbols: list[str] = field(default_factory=list)


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
) -> InstrumentSyncResult:
    """用 EOD snapshot 同步 instrument universe。

    规则：
    - 新 symbol → INSERT（name/pinyin_initials/market/status）。
    - 已存在且 name/market 变化 → UPDATE（不覆盖 listing_date，不自动 delist）。
    - snapshot 未出现的股票保持原状（delisting 归现有 maintenance owner）。

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
        inst = existing_by_symbol.get(row.symbol)
        if inst is None:
            new_records.append(
                {
                    "symbol": row.symbol,
                    "name": row.name,
                    "pinyin_initials": compute_pinyin_initials(row.name),
                    "market": row.market,
                    "status": "active",
                }
            )
            new_rows.append(row)
            continue

        changed = (inst.name != row.name) or (inst.market != row.market)
        if changed:
            inst.name = row.name
            inst.market = row.market
            inst.pinyin_initials = compute_pinyin_initials(row.name)
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

    入参 rows 为 (instrument_id, EodSnapshotRow) 序列。对以下情况跳过该行：
    - trade_date 与请求日不一致（老时间戳/停牌不伪造）；
    - 任意 OHLC 缺失或 <=0；
    - high < max(open,close) 或 low > min(open,close)（数据异常）；
    - volume/amount 缺失或 <0。

    返回成功写入（INSERT+UPDATE）的记录数。
    """
    records: list[dict] = []
    for instrument_id, row in rows:
        if row.trade_date != trade_date:
            continue

        o, h, lo, c = row.open, row.high, row.low, row.close
        if o is None or h is None or lo is None or c is None:
            continue
        if o <= 0 or h <= 0 or lo <= 0 or c <= 0:
            continue
        if h < max(o, c):
            continue
        if lo > min(o, c):
            continue

        v, a = row.volume, row.amount
        if v is None or v < 0:
            continue
        if a is None or a < 0:
            continue

        records.append(
            {
                "instrument_id": instrument_id,
                "trade_date": trade_date,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": v,
                "amount": a,
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

    返回至少写入一日的标的数。
    """
    if not instruments:
        return 0
    start = trade_date - timedelta(days=lookback_days)
    return await _backfill_batch(
        session, instruments, start, trade_date, breaker=breaker
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
    返回至少写入一日的标的数。
    """
    if not new_instruments:
        return 0
    written = 0
    items = list(new_instruments)
    for idx in range(0, len(items), _FALLBACK_BATCH_SIZE):
        for inst in items[idx : idx + _FALLBACK_BATCH_SIZE]:
            start = inst.listing_date or _NEW_INSTRUMENT_HISTORY_START
            if start > trade_date:
                start = trade_date
            if await _backfill_one(
                session, inst, start, trade_date, breaker=breaker
            ):
                written += 1
    return written


async def _backfill_batch(
    session: AsyncSession,
    instruments: Sequence[Instrument],
    start: date,
    end: date,
    *,
    breaker: _PytdxBreaker | None = None,
) -> int:
    written = 0
    items = list(instruments)
    for idx in range(0, len(items), _FALLBACK_BATCH_SIZE):
        for inst in items[idx : idx + _FALLBACK_BATCH_SIZE]:
            if await _backfill_one(session, inst, start, end, breaker=breaker):
                written += 1
    return written


async def _backfill_one(
    session: AsyncSession,
    inst: Instrument,
    start: date,
    end: date,
    *,
    breaker: _PytdxBreaker | None = None,
) -> bool:
    """单只标的回补；pytdx raw 优先，Eastmoney fqt=0 兜底。返回是否至少写入一日。

    熔断：当传入 breaker 且已达连续失败上限，跳过 pytdx 直接走 Eastmoney，
    避免源故障时每只股票都先付出一次连接/重试代价。
    """
    from app.repositories.bar_repository import refresh_daily_bars
    from app.services.eod_market_snapshot_provider import (
        SnapshotProviderError,
        fetch_eastmoney_daily_kline,
    )

    symbol = inst.symbol
    market = inst.market

    # 1) pytdx raw 优先（熔断开启时跳过）
    if breaker is None or breaker.allow:
        try:
            df = await refresh_daily_bars(session, inst.id, start, end, adapter=None)
            if df is not None and not df.empty:
                if breaker is not None:
                    breaker.record_success()
                return True
            if breaker is not None:
                breaker.record_failure()
        except Exception as exc:
            if breaker is not None:
                breaker.record_failure()
            logger.warning("pytdx 回补失败 symbol=%s: %s", symbol, exc)

    # 2) Eastmoney fqt=0 兜底（insert-only：绝不覆盖既有行 / 既有 adj_factor）
    try:
        async with httpx.AsyncClient(timeout=_EM_REQUEST_TIMEOUT) as client:
            recs = await fetch_eastmoney_daily_kline(client, symbol, market, start, end)
        if not recs:
            return False
        return await _persist_eastmoney_raw_daily(session, inst.id, symbol, recs)
    except SnapshotProviderError as exc:
        logger.warning("Eastmoney 回补失败 symbol=%s: %s", symbol, exc)
    except Exception as exc:
        logger.warning("Eastmoney 回补异常 symbol=%s: %s", symbol, exc)
    return False


async def _persist_eastmoney_raw_daily(
    session: AsyncSession,
    instrument_id: UUID,
    symbol: str,
    records: Sequence[dict],
) -> bool:
    """Eastmoney 历史记录落库（raw、不复权、insert-only）。

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
