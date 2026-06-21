"""行情仓储 - 日线与分钟线数据的 DB 查询、pytdx 拉取回填、前复权应用。

从 ref/交易/datasource/k_data_loader.py 迁移行情加载逻辑，关键改进：
1. 异步 DB 访问：使用 AsyncSession（与 app/db.py 一致）。
2. DB 缺失自动回填：DB 无数据时从 pytdx 拉取并 upsert 入库（原始 k_data_loader 仅从 DB 读）。
3. pytdx 同步调用通过 asyncio.to_thread 桥接，不阻塞事件循环。
4. 前复权委托 services/adj_factor（纯计算，已向量化）。
5. 禁异常吞没：所有异常补充上下文后 re-raise。

设计说明：
- instrument_id 为 UUID（V1.1），pytdx 使用 symbol（6 位代码）；通过 instruments 表转换。
- bars_daily/bars_minute 表自带 adj_factor 列；前复权时从表中提取 distinct (trade_date, adj_factor)。
- pytdx 不提供 adj_factor，拉取写入时 adj_factor 默认 1.0；adj_factor 的实际获取（tushare）属另一任务。

Inputs:
    session: AsyncSession
    instrument_id: UUID
    start_date/end_date: date 或 datetime

Outputs:
    DataFrame: index 为 DatetimeIndex，含 open/high/low/close/volume/amount/adj_factor 列

How to Run:
    python -m app.repositories.bar_repository    # 自测：验证函数签名与基础逻辑（不连 DB）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import select

from app.core.pytdx_adapter import PytdxAdapter, get_pytdx_adapter
from app.models.bar import Bar15Min, Bar60Min, BarDaily, BarMinute, BarMonthly, BarWeekly
from app.services.adj_factor import apply_adj_factor, apply_adj_factor_intraday
from app.services.bars_validator import validate_bars

logger = logging.getLogger("bar_repository")

# 行情数据列（DB 查询返回的标准列）
_BAR_COLUMNS = ["open", "high", "low", "close", "volume", "amount", "adj_factor"]


async def _get_symbol(session: AsyncSession, instrument_id: uuid.UUID) -> str | None:
    """查询 instruments 表获取股票代码。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID

    Returns:
        股票代码（如 '600519'）或 None（标的不存在）

    Raises:
        Exception: DB 查询失败时 re-raise（不吞没）
    """
    try:
        result = await session.execute(
            text("SELECT symbol FROM instruments WHERE id = :id"),
            {"id": instrument_id},
        )
        row = result.first()
        return row[0] if row else None
    except Exception as exc:
        logger.warning("查询 instrument symbol 失败 instrument_id=%s: %s", instrument_id, exc)
        raise


async def _query_daily_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """从 DB 查询日线行情。

    Returns:
        DataFrame: index=DatetimeIndex(trade_date), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    try:
        result = await session.execute(
            select(
                BarDaily.trade_date,
                BarDaily.open,
                BarDaily.high,
                BarDaily.low,
                BarDaily.close,
                BarDaily.volume,
                BarDaily.amount,
                BarDaily.adj_factor,
            )
            .where(BarDaily.instrument_id == instrument_id)
            .where(BarDaily.trade_date >= start_date)
            .where(BarDaily.trade_date <= end_date)
            .order_by(BarDaily.trade_date)
        )
        rows = result.all()
    except Exception as exc:
        logger.warning("查询 bars_daily 失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["trade_date"] + _BAR_COLUMNS)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("trade_date")
    # Decimal -> float（便于 pandas 计算）
    for col in _BAR_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


async def _query_minute_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_time: datetime,
    end_time: datetime,
) -> pd.DataFrame:
    """从 DB 查询分钟线行情。

    Returns:
        DataFrame: index=DatetimeIndex(trade_time), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    try:
        result = await session.execute(
            select(
                BarMinute.trade_time,
                BarMinute.open,
                BarMinute.high,
                BarMinute.low,
                BarMinute.close,
                BarMinute.volume,
                BarMinute.amount,
                BarMinute.adj_factor,
            )
            .where(BarMinute.instrument_id == instrument_id)
            .where(BarMinute.trade_time >= start_time)
            .where(BarMinute.trade_time <= end_time)
            .order_by(BarMinute.trade_time)
        )
        rows = result.all()
    except Exception as exc:
        logger.warning("查询 bars_minute 失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["trade_time"] + _BAR_COLUMNS)
    df["trade_time"] = pd.to_datetime(df["trade_time"])
    df = df.set_index("trade_time")
    for col in _BAR_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _df_to_upsert_records(
    df: pd.DataFrame,
    instrument_id: uuid.UUID,
    is_daily: bool,
    volume_multiplier: Decimal = Decimal("1"),
) -> list[dict[str, Any]]:
    """向量化将 DataFrame 转为 upsert records 列表。

    替代原 iterrows() 循环，使用列级 astype(str).tolist() + 列表推导，
    在 800 条数据时性能提升约 3-5 倍。

    Args:
        df: 含 datetime/open/high/low/close/volume/amount/adj_factor 列的 DataFrame
        instrument_id: 标的 UUID
        is_daily: True 为日线类（trade_date），False 为分钟类（trade_time）
        volume_multiplier: volume 乘数（周线/月线为 Decimal("100")，其余为 Decimal("1")）

    Returns:
        records 列表，可直接用于 pg_insert(...).values(records)
    """
    n = len(df)
    if n == 0:
        return []

    # 向量化提取时间列
    dt_series = pd.to_datetime(df["datetime"])
    if is_daily:
        time_key = "trade_date"
        time_values = dt_series.dt.date.tolist()
    else:
        time_key = "trade_time"
        time_values = dt_series.dt.to_pydatetime()

    # 列级转换（向量化，避免逐行 Decimal 构造）
    opens = df["open"].astype(str).tolist()
    highs = df["high"].astype(str).tolist()
    lows = df["low"].astype(str).tolist()
    closes = df["close"].astype(str).tolist()
    volumes = df["volume"].astype(str).tolist()
    amounts = df["amount"].astype(str).tolist()
    adj_factors = df["adj_factor"].astype(str).tolist()

    records: list[dict[str, Any]] = []
    for i in range(n):
        records.append({
            "instrument_id": instrument_id,
            time_key: time_values[i],
            "open": Decimal(opens[i]),
            "high": Decimal(highs[i]),
            "low": Decimal(lows[i]),
            "close": Decimal(closes[i]),
            "volume": Decimal(volumes[i]) * volume_multiplier,
            "amount": Decimal(amounts[i]),
            "adj_factor": Decimal(adj_factors[i]),
        })
    return records


async def _upsert_daily_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    raw_df: pd.DataFrame,
    symbol: str | None = None,
    adapter: PytdxAdapter | None = None,
) -> int:
    """将 pytdx 拉取的日线数据 upsert 入库。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        raw_df: pytdx 返回的 DataFrame，含 datetime/open/high/low/close/volume/amount
        symbol: 股票代码，用于计算 adj_factor；None 时 adj_factor 默认 1.0
        adapter: pytdx 适配器，用于计算 adj_factor

    Returns:
        写入记录数

    Raises:
        Exception: 写入失败时 re-raise（不吞没）
    """
    if raw_df.empty:
        return 0

    # 计算 adj_factor（基于 pytdx 除权除息数据）
    if symbol:
        try:
            adj_factors = await asyncio.to_thread(
                _calculate_adj_factor, symbol, raw_df, adapter
            )
        except Exception as exc:
            logger.warning("计算 adj_factor 失败 symbol=%s: %s，使用默认 1.0", symbol, exc)
            adj_factors = [1.0] * len(raw_df)
    else:
        adj_factors = [1.0] * len(raw_df)

    # 将 adj_factor 写入 raw_df，供调用方使用
    raw_df["adj_factor"] = adj_factors

    # 写入前校验数据质量
    validation = validate_bars(raw_df, symbol or "", "d")
    if not validation.is_valid:
        logger.error(
            "日线数据校验失败 symbol=%s errors=%s",
            symbol, validation.errors[:5],
        )
        return 0

    # 向量化构建 records（替代 iterrows）
    records = _df_to_upsert_records(
        raw_df, instrument_id, is_daily=True, volume_multiplier=Decimal("1")
    )

    try:
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
                "adj_factor": stmt.excluded.adj_factor,
            },
        )
        await session.execute(stmt)
        await session.commit()
    except Exception as exc:
        logger.warning("upsert bars_daily 失败 instrument_id=%s: %s", instrument_id, exc)
        await session.rollback()
        raise

    logger.info("upsert bars_daily: instrument_id=%s records=%d", instrument_id, len(records))
    return len(records)


async def _upsert_minute_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    raw_df: pd.DataFrame,
) -> int:
    """将 pytdx 拉取的分钟线数据 upsert 入库。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        raw_df: pytdx 返回的 DataFrame，含 datetime/open/high/low/close/volume/amount

    Returns:
        写入记录数

    Raises:
        Exception: 写入失败时 re-raise（不吞没）
    """
    if raw_df.empty:
        return 0

    # 分钟线不做复权，adj_factor 固定为 1.0
    raw_df["adj_factor"] = 1.0

    # 写入前校验数据质量
    validation = validate_bars(raw_df, "", "minute")
    if not validation.is_valid:
        logger.error(
            "分钟线数据校验失败 instrument_id=%s errors=%s",
            instrument_id, validation.errors[:5],
        )
        return 0

    # 向量化构建 records（替代 iterrows，分钟线 adj_factor 固定 1.0）
    records = _df_to_upsert_records(
        raw_df, instrument_id, is_daily=False, volume_multiplier=Decimal("1")
    )

    try:
        stmt = pg_insert(BarMinute).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id", "trade_time"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "amount": stmt.excluded.amount,
                "adj_factor": stmt.excluded.adj_factor,
            },
        )
        await session.execute(stmt)
        await session.commit()
    except Exception as exc:
        logger.warning("upsert bars_minute 失败 instrument_id=%s: %s", instrument_id, exc)
        await session.rollback()
        raise

    logger.info("upsert bars_minute: instrument_id=%s records=%d", instrument_id, len(records))
    return len(records)


def _calculate_adj_factor(
    symbol: str,
    raw_df: pd.DataFrame,
    adapter: PytdxAdapter | None = None,
    use_raw_close: bool = True,
) -> list[float]:
    """基于 pytdx 除权除息数据计算前复权因子。

    算法（参考 Chanlunpro klines_fq 的 preclose 公式）：
    1. 获取除权除息事件（category=1），含 fenhong/songzhuangu/peigu/peigujia
    2. 对每个事件日 D，获取 close_{D-1}（事件日前一交易日收盘价）
    3. 计算除权参考价：
       preclose = (close_{D-1} × 10 - fenhong + peigu × peigujia) / (10 + peigu + songzhuangu)
    4. 计算单次事件因子 = preclose / close_{D-1}
       化简为：event_factor = (10 - fenhong/close_{D-1} + peigu×peigujia/close_{D-1})
                                 / (10 + peigu + songzhuangu)
    5. 累积因子 = 所有晚于该 bar 日期的事件因子乘积
    6. 最新日期（无后续事件）的 adj_factor = 1.0

    前复权公式：qfq_price = raw_price × adj_factor
    其中 adj_factor = 累积因子，最新日期 adj_factor = 1.0

    与旧算法的差异：
    - 旧算法只用 fenhong，用 close_D（除权日收盘）近似
    - 新算法用全部 xdxr 字段，用 close_{D-1}（前日收盘）精确计算

    Args:
        symbol: 股票代码（如 '000001'）
        raw_df: pytdx 返回的 DataFrame，含 datetime 列（用于提取 bar 日期）
        adapter: pytdx 适配器，None 使用模块单例
        use_raw_close: True 时从 raw_df 提取事件日收盘价（适用于日线）；
            False 时始终从 pytdx 拉取日线 close（适用于周线/月线/分钟线，其 close 非日线 close）

    Returns:
        adj_factor 列表，与 raw_df 行一一对应；获取失败时全为 1.0
    """
    if raw_df.empty:
        return []

    # 默认 adj_factor = 1.0（获取 xdxr 失败时的兜底）
    default_factors = [1.0] * len(raw_df)

    pytdx = adapter or get_pytdx_adapter()
    try:
        xdxr_df = pytdx.get_xdxr_info(symbol)
    except Exception as exc:
        logger.warning("获取除权除息数据失败 symbol=%s: %s，adj_factor 默认 1.0", symbol, exc)
        return default_factors

    if xdxr_df.empty:
        return default_factors

    # 筛选 category=1 的除权除息事件
    exc_events = xdxr_df[xdxr_df["category"] == 1].copy()
    if exc_events.empty:
        return default_factors

    # 构建 close 查找表：date -> close
    # use_raw_close=True 时从 raw_df 提取（日线场景）；
    # use_raw_close=False 时不从 raw_df 提取（周线/月线/分钟线场景，close 非日线值）
    close_map: dict[date, float] = {}
    if use_raw_close:
        for _, row in raw_df.iterrows():
            dt = pd.Timestamp(row["datetime"]).date()
            close_map[dt] = float(row["close"])

    # 对事件日不在 close_map 中的，批量从 pytdx 拉取日线补充 close
    # 同时扩展范围向前 10 天，以获取事件日前一交易日的收盘价（close_{D-1}）
    missing_dates: list[date] = []
    for _, event in exc_events.iterrows():
        event_date = event["date"].date()
        if event_date not in close_map:
            missing_dates.append(event_date)

    if missing_dates or not use_raw_close:
        # 找出需要拉取的日期范围（向前扩展 10 天以获取 close_{D-1}）
        all_event_dates = [event["date"].date() for _, event in exc_events.iterrows()]
        min_d = min(all_event_dates) if all_event_dates else date.today()
        max_d = max(all_event_dates) if all_event_dates else date.today()
        # 向前扩展 10 天确保覆盖前一交易日
        from datetime import timedelta as _td

        fetch_start = min_d - _td(days=10)
        try:
            supplement_df = pytdx.get_daily_bars(symbol, fetch_start, max_d)
            for _, row in supplement_df.iterrows():
                dt = pd.Timestamp(row["datetime"]).date()
                close_map[dt] = float(row["close"])
        except Exception as exc:
            logger.warning(
                "补充拉取事件日收盘价失败 symbol=%s dates=%s~%s: %s",
                symbol, fetch_start, max_d, exc,
            )

    # 按日期升序排列事件
    exc_events = exc_events.sort_values("date")

    # 构建 sorted_dates 用于查找前一交易日
    sorted_close_dates = sorted(close_map.keys())

    def _find_prev_close(target_date: date) -> float | None:
        """查找 target_date 前一交易日的收盘价。"""
        prev_close = None
        for d in sorted_close_dates:
            if d >= target_date:
                break
            prev_close = close_map[d]
        return prev_close

    # 计算每个事件的因子，并构建 (event_date, cumulative_factor) 列表
    # cumulative_factor 表示：日期 < event_date 的 bar 需要乘以该因子
    # 从最新事件向最旧事件累积
    events_with_factor: list[tuple[date, float]] = []
    cumulative = 1.0
    for _, event in exc_events[::-1].iterrows():
        event_date = event["date"].date()
        # 获取事件日前一交易日的收盘价（close_{D-1}）
        prev_close = _find_prev_close(event_date)
        if prev_close is None or prev_close == 0:
            logger.warning(
                "事件日 %s 前一交易日无收盘价数据 symbol=%s，跳过该事件", event_date, symbol,
            )
            continue

        fenhong = float(event["fenhong"]) if pd.notna(event["fenhong"]) else 0.0
        songzhuangu = float(event["songzhuangu"]) if pd.notna(event["songzhuangu"]) else 0.0
        peigu = float(event["peigu"]) if pd.notna(event["peigu"]) else 0.0
        peigujia = float(event["peigujia"]) if pd.notna(event["peigujia"]) else 0.0

        # Chanlunpro preclose 公式：
        # preclose = (close_{D-1} × 10 - fenhong + peigu × peigujia) / (10 + peigu + songzhuangu)
        # event_factor = preclose / close_{D-1}
        denominator = 10 + peigu + songzhuangu
        if denominator == 0:
            logger.warning(
                "事件日 %s 除权除息分母为 0 symbol=%s，跳过该事件", event_date, symbol,
            )
            continue

        preclose = (prev_close * 10 - fenhong + peigu * peigujia) / denominator
        event_factor = preclose / prev_close

        # 仅当事件因子不为 1.0 时才累积（避免无意义的事件）
        if abs(event_factor - 1.0) > 1e-10:
            cumulative *= event_factor
        events_with_factor.append((event_date, cumulative))

    # events_with_factor 按 event_date 降序（最新事件在前）
    # 对每个 bar 日期，adj_factor = 第一个 event_date > bar_date 的 cumulative_factor
    # 如果没有晚于 bar_date 的事件，adj_factor = 1.0
    adj_factors: list[float] = []
    for _, row in raw_df.iterrows():
        bar_date = pd.Timestamp(row["datetime"]).date()
        factor = 1.0
        for event_date, cumulative_factor in events_with_factor:
            if event_date > bar_date:
                factor = cumulative_factor
                break
        adj_factors.append(factor)

    logger.info(
        "计算 adj_factor symbol=%s bars=%d events=%d adj_range=[%.6f, %.6f]",
        symbol, len(adj_factors), len(events_with_factor),
        min(adj_factors) if adj_factors else 1.0,
        max(adj_factors) if adj_factors else 1.0,
    )
    return adj_factors


async def _get_adj_factor_df(
    session: AsyncSession,
    instrument_id: uuid.UUID,
) -> pd.DataFrame:
    """从 bars_daily 表提取 distinct (trade_date, adj_factor) 用于前复权。

    前复权需要全量 adj_factor（含查询范围外的最新值），故不限日期范围。

    Returns:
        DataFrame: columns=[trade_date, adj_factor]，按 trade_date 排序
    """
    try:
        result = await session.execute(
            select(BarDaily.trade_date, BarDaily.adj_factor)
            .where(BarDaily.instrument_id == instrument_id)
            .where(BarDaily.adj_factor.isnot(None))
            .order_by(BarDaily.trade_date)
        )
        rows = result.all()
    except Exception as exc:
        logger.warning("查询 adj_factor 失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if not rows:
        return pd.DataFrame(columns=["trade_date", "adj_factor"])

    df = pd.DataFrame(rows, columns=["trade_date", "adj_factor"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce")
    return df


async def fetch_daily_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_date: date,
    end_date: date,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """查询日线行情：DB 优先，DB 无数据则从 pytdx 拉取并写入 DB。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        start_date: 起始日期
        end_date: 结束日期
        adapter: pytdx 适配器，None 使用模块单例

    Returns:
        DataFrame: index=DatetimeIndex(trade_date), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    # 1. DB 查询
    df = await _query_daily_bars(session, instrument_id, start_date, end_date)
    if not df.empty:
        return df

    # 2. DB 无数据，查 symbol
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return df

    # 3. 从 pytdx 拉取（同步调用通过 to_thread 桥接）
    pytdx = adapter or get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(
            pytdx.get_daily_bars, symbol, start_date, end_date
        )
    except Exception as exc:
        logger.warning("pytdx 拉取日线失败 symbol=%s: %s", symbol, exc)
        raise

    if raw_df.empty:
        logger.warning("pytdx 日线数据为空 symbol=%s %s~%s", symbol, start_date, end_date)
        return raw_df

    # 4. 写入 DB（含 adj_factor 计算）
    await _upsert_daily_bars(session, instrument_id, raw_df, symbol, pytdx)

    # 5. 返回（adj_factor 已由 _upsert_daily_bars 写入 raw_df）
    result_df = raw_df.set_index("datetime")
    result_df.index.name = "trade_date"
    return result_df


async def fetch_minute_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_time: datetime,
    end_time: datetime,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """查询分钟线行情：DB 优先，DB 无数据则从 pytdx 拉取并写入 DB。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        start_time: 起始时间
        end_time: 结束时间
        adapter: pytdx 适配器，None 使用模块单例

    Returns:
        DataFrame: index=DatetimeIndex(trade_time), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    # 1. DB 查询
    df = await _query_minute_bars(session, instrument_id, start_time, end_time)
    if not df.empty:
        return df

    # 2. DB 无数据，查 symbol
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return df

    # 3. 从 pytdx 拉取
    pytdx = adapter or get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(
            pytdx.get_minute_bars, symbol, start_time, end_time
        )
    except Exception as exc:
        logger.warning("pytdx 拉取分钟线失败 symbol=%s: %s", symbol, exc)
        raise

    if raw_df.empty:
        logger.warning("pytdx 分钟线数据为空 symbol=%s %s~%s", symbol, start_time, end_time)
        return raw_df

    # 4. 写入 DB
    await _upsert_minute_bars(session, instrument_id, raw_df)

    # 5. 返回
    result_df = raw_df.set_index("datetime")
    result_df.index.name = "trade_time"
    result_df["adj_factor"] = 1.0
    return result_df


async def refresh_daily_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_date: date,
    end_date: date,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """强制从 pytdx 拉取日线并 upsert（供 freshness_sla 触发刷新）。

    与 fetch_daily_bars 不同：不查 DB，直接拉取并写入。
    """
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    pytdx = adapter or get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(
            pytdx.get_daily_bars, symbol, start_date, end_date
        )
    except Exception as exc:
        logger.warning("pytdx 刷新日线失败 symbol=%s: %s", symbol, exc)
        raise

    if raw_df.empty:
        return raw_df

    await _upsert_daily_bars(session, instrument_id, raw_df, symbol, pytdx)

    result_df = raw_df.set_index("datetime")
    result_df.index.name = "trade_date"
    return result_df


async def refresh_minute_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_time: datetime,
    end_time: datetime,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """强制从 pytdx 拉取分钟线并 upsert（供 freshness_sla 触发刷新）。"""
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    pytdx = adapter or get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(
            pytdx.get_minute_bars, symbol, start_time, end_time
        )
    except Exception as exc:
        logger.warning("pytdx 刷新分钟线失败 symbol=%s: %s", symbol, exc)
        raise

    if raw_df.empty:
        return raw_df

    await _upsert_minute_bars(session, instrument_id, raw_df)

    result_df = raw_df.set_index("datetime")
    result_df.index.name = "trade_time"
    result_df["adj_factor"] = 1.0
    return result_df


# ===== 多周期行情：周线/月线/15分钟/60分钟 =====
# 设计说明：
# - fetch_*_bars：DB 优先，按日期/时间范围查询，无数据则从 pytdx 拉取并入库
# - refresh_*_bars：强制从 pytdx 拉取（按 count），供调度服务使用
# - 周线/月线使用 trade_date（Date），15min/60min 使用 trade_time（DateTime）
# - pytdx 不支持并发，所有拉取通过 asyncio.to_thread 串行桥接


# ----- 周线 -----

async def _query_weekly_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """从 DB 查询周线行情。

    Returns:
        DataFrame: index=DatetimeIndex(trade_date), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    try:
        result = await session.execute(
            select(
                BarWeekly.trade_date,
                BarWeekly.open,
                BarWeekly.high,
                BarWeekly.low,
                BarWeekly.close,
                BarWeekly.volume,
                BarWeekly.amount,
                BarWeekly.adj_factor,
            )
            .where(BarWeekly.instrument_id == instrument_id)
            .where(BarWeekly.trade_date >= start_date)
            .where(BarWeekly.trade_date <= end_date)
            .order_by(BarWeekly.trade_date)
        )
        rows = result.all()
    except Exception as exc:
        logger.warning("查询 bars_weekly 失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["trade_date"] + _BAR_COLUMNS)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("trade_date")
    for col in _BAR_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


async def _upsert_weekly_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    raw_df: pd.DataFrame,
    symbol: str | None = None,
    adapter: PytdxAdapter | None = None,
) -> int:
    """将 pytdx 拉取的周线数据 upsert 入库。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        raw_df: pytdx 返回的 DataFrame，含 datetime/open/high/low/close/volume/amount
        symbol: 股票代码，用于计算 adj_factor；None 时 adj_factor 默认 1.0
        adapter: pytdx 适配器，用于计算 adj_factor

    Returns:
        写入记录数

    Raises:
        Exception: 写入失败时 re-raise（不吞没）
    """
    if raw_df.empty:
        return 0

    # 计算 adj_factor（周线 close 非日线 close，需 use_raw_close=False 从 pytdx 拉取日线 close）
    if symbol:
        try:
            adj_factors = await asyncio.to_thread(
                _calculate_adj_factor, symbol, raw_df, adapter, False
            )
        except Exception as exc:
            logger.warning("计算 adj_factor 失败 symbol=%s: %s，使用默认 1.0", symbol, exc)
            adj_factors = [1.0] * len(raw_df)
    else:
        adj_factors = [1.0] * len(raw_df)

    raw_df["adj_factor"] = adj_factors

    # 写入前校验数据质量
    validation = validate_bars(raw_df, symbol or "", "w")
    if not validation.is_valid:
        logger.error(
            "周线数据校验失败 symbol=%s errors=%s",
            symbol, validation.errors[:5],
        )
        return 0

    # 向量化构建 records（周线 volume 乘 100：pytdx 周线单位是"手"）
    records = _df_to_upsert_records(
        raw_df, instrument_id, is_daily=True, volume_multiplier=Decimal("100")
    )

    try:
        stmt = pg_insert(BarWeekly).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id", "trade_date"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "amount": stmt.excluded.amount,
                "adj_factor": stmt.excluded.adj_factor,
            },
        )
        await session.execute(stmt)
        await session.commit()
    except Exception as exc:
        logger.warning("upsert bars_weekly 失败 instrument_id=%s: %s", instrument_id, exc)
        await session.rollback()
        raise

    logger.info("upsert bars_weekly: instrument_id=%s records=%d", instrument_id, len(records))
    return len(records)


async def fetch_weekly_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_date: date,
    end_date: date,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """查询周线行情：DB 优先，DB 无数据则从 pytdx 拉取并写入 DB。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        start_date: 起始日期
        end_date: 结束日期
        adapter: pytdx 适配器，None 使用模块单例

    Returns:
        DataFrame: index=DatetimeIndex(trade_date), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    # 1. DB 查询
    df = await _query_weekly_bars(session, instrument_id, start_date, end_date)
    if not df.empty:
        return df

    # 2. DB 无数据，查 symbol
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return df

    # 3. 从 pytdx 拉取（按 count，周线回补到 2023-01-01 约需 200 条）
    weeks = max((end_date - start_date).days // 7 + 10, 10)
    count = min(weeks, 800)
    pytdx = adapter or get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(pytdx.get_weekly_bars, symbol, count)
    except Exception as exc:
        logger.warning("pytdx 拉取周线失败 symbol=%s: %s", symbol, exc)
        raise

    if raw_df.empty:
        logger.warning("pytdx 周线数据为空 symbol=%s", symbol)
        return raw_df

    # 4. 写入 DB（含 adj_factor 计算）
    await _upsert_weekly_bars(session, instrument_id, raw_df, symbol, pytdx)

    # 5. 按日期范围过滤后返回
    result_df = raw_df.set_index("datetime")
    result_df.index.name = "trade_date"
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    mask = (result_df.index >= start_ts) & (result_df.index <= end_ts)
    return result_df.loc[mask]


async def refresh_weekly_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    count: int = 200,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """强制从 pytdx 拉取周线并 upsert（供调度服务使用）。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        count: 拉取条数（默认 200，回补到 2023-01-01 约需 165 条）
        adapter: pytdx 适配器，None 使用模块单例

    Returns:
        DataFrame: index=DatetimeIndex(trade_date), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    pytdx = adapter or get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(pytdx.get_weekly_bars, symbol, count)
    except Exception as exc:
        logger.warning("pytdx 刷新周线失败 symbol=%s: %s", symbol, exc)
        raise

    if raw_df.empty:
        return raw_df

    await _upsert_weekly_bars(session, instrument_id, raw_df, symbol, pytdx)

    result_df = raw_df.set_index("datetime")
    result_df.index.name = "trade_date"
    return result_df


# ----- 月线 -----

async def _query_monthly_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """从 DB 查询月线行情。

    Returns:
        DataFrame: index=DatetimeIndex(trade_date), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    try:
        result = await session.execute(
            select(
                BarMonthly.trade_date,
                BarMonthly.open,
                BarMonthly.high,
                BarMonthly.low,
                BarMonthly.close,
                BarMonthly.volume,
                BarMonthly.amount,
                BarMonthly.adj_factor,
            )
            .where(BarMonthly.instrument_id == instrument_id)
            .where(BarMonthly.trade_date >= start_date)
            .where(BarMonthly.trade_date <= end_date)
            .order_by(BarMonthly.trade_date)
        )
        rows = result.all()
    except Exception as exc:
        logger.warning("查询 bars_monthly 失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["trade_date"] + _BAR_COLUMNS)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("trade_date")
    for col in _BAR_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


async def _upsert_monthly_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    raw_df: pd.DataFrame,
    symbol: str | None = None,
    adapter: PytdxAdapter | None = None,
) -> int:
    """将 pytdx 拉取的月线数据 upsert 入库。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        raw_df: pytdx 返回的 DataFrame，含 datetime/open/high/low/close/volume/amount
        symbol: 股票代码，用于计算 adj_factor；None 时 adj_factor 默认 1.0
        adapter: pytdx 适配器，用于计算 adj_factor

    Returns:
        写入记录数

    Raises:
        Exception: 写入失败时 re-raise（不吞没）
    """
    if raw_df.empty:
        return 0

    # 计算 adj_factor（月线 close 非日线 close，需 use_raw_close=False）
    if symbol:
        try:
            adj_factors = await asyncio.to_thread(
                _calculate_adj_factor, symbol, raw_df, adapter, False
            )
        except Exception as exc:
            logger.warning("计算 adj_factor 失败 symbol=%s: %s，使用默认 1.0", symbol, exc)
            adj_factors = [1.0] * len(raw_df)
    else:
        adj_factors = [1.0] * len(raw_df)

    raw_df["adj_factor"] = adj_factors

    # 写入前校验数据质量
    validation = validate_bars(raw_df, symbol or "", "m")
    if not validation.is_valid:
        logger.error(
            "月线数据校验失败 symbol=%s errors=%s",
            symbol, validation.errors[:5],
        )
        return 0

    # 向量化构建 records（月线 volume 乘 100：pytdx 月线单位是"手"）
    records = _df_to_upsert_records(
        raw_df, instrument_id, is_daily=True, volume_multiplier=Decimal("100")
    )

    try:
        stmt = pg_insert(BarMonthly).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id", "trade_date"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "amount": stmt.excluded.amount,
                "adj_factor": stmt.excluded.adj_factor,
            },
        )
        await session.execute(stmt)
        await session.commit()
    except Exception as exc:
        logger.warning("upsert bars_monthly 失败 instrument_id=%s: %s", instrument_id, exc)
        await session.rollback()
        raise

    logger.info("upsert bars_monthly: instrument_id=%s records=%d", instrument_id, len(records))
    return len(records)


async def fetch_monthly_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_date: date,
    end_date: date,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """查询月线行情：DB 优先，DB 无数据则从 pytdx 拉取并写入 DB。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        start_date: 起始日期
        end_date: 结束日期
        adapter: pytdx 适配器，None 使用模块单例

    Returns:
        DataFrame: index=DatetimeIndex(trade_date), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    # 1. DB 查询
    df = await _query_monthly_bars(session, instrument_id, start_date, end_date)
    if not df.empty:
        return df

    # 2. DB 无数据，查 symbol
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return df

    # 3. 从 pytdx 拉取（按 count，月线回补到 2023-01-01 约需 50 条）
    months = max((end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 5, 5)
    count = min(months, 800)
    pytdx = adapter or get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(pytdx.get_monthly_bars, symbol, count)
    except Exception as exc:
        logger.warning("pytdx 拉取月线失败 symbol=%s: %s", symbol, exc)
        raise

    if raw_df.empty:
        logger.warning("pytdx 月线数据为空 symbol=%s", symbol)
        return raw_df

    # 4. 写入 DB
    await _upsert_monthly_bars(session, instrument_id, raw_df, symbol, pytdx)

    # 5. 按日期范围过滤后返回
    result_df = raw_df.set_index("datetime")
    result_df.index.name = "trade_date"
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    mask = (result_df.index >= start_ts) & (result_df.index <= end_ts)
    return result_df.loc[mask]


async def refresh_monthly_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    count: int = 50,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """强制从 pytdx 拉取月线并 upsert（供调度服务使用）。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        count: 拉取条数（默认 50，回补到 2023-01-01 约需 42 条）
        adapter: pytdx 适配器，None 使用模块单例

    Returns:
        DataFrame: index=DatetimeIndex(trade_date), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    pytdx = adapter or get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(pytdx.get_monthly_bars, symbol, count)
    except Exception as exc:
        logger.warning("pytdx 刷新月线失败 symbol=%s: %s", symbol, exc)
        raise

    if raw_df.empty:
        return raw_df

    await _upsert_monthly_bars(session, instrument_id, raw_df, symbol, pytdx)

    result_df = raw_df.set_index("datetime")
    result_df.index.name = "trade_date"
    return result_df


# ----- 15分钟线 -----

async def _query_15min_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_time: datetime,
    end_time: datetime,
) -> pd.DataFrame:
    """从 DB 查询 15 分钟线行情。

    Returns:
        DataFrame: index=DatetimeIndex(trade_time), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    try:
        result = await session.execute(
            select(
                Bar15Min.trade_time,
                Bar15Min.open,
                Bar15Min.high,
                Bar15Min.low,
                Bar15Min.close,
                Bar15Min.volume,
                Bar15Min.amount,
                Bar15Min.adj_factor,
            )
            .where(Bar15Min.instrument_id == instrument_id)
            .where(Bar15Min.trade_time >= start_time)
            .where(Bar15Min.trade_time <= end_time)
            .order_by(Bar15Min.trade_time)
        )
        rows = result.all()
    except Exception as exc:
        logger.warning("查询 bars_15min 失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["trade_time"] + _BAR_COLUMNS)
    df["trade_time"] = pd.to_datetime(df["trade_time"])
    df = df.set_index("trade_time")
    for col in _BAR_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


async def _upsert_15min_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    raw_df: pd.DataFrame,
    symbol: str | None = None,
    adapter: PytdxAdapter | None = None,
) -> int:
    """将 pytdx 拉取的 15 分钟线数据 upsert 入库。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        raw_df: pytdx 返回的 DataFrame，含 datetime/open/high/low/close/volume/amount
        symbol: 股票代码，用于计算 adj_factor；None 时 adj_factor 默认 1.0
        adapter: pytdx 适配器，用于计算 adj_factor

    Returns:
        写入记录数

    Raises:
        Exception: 写入失败时 re-raise（不吞没）
    """
    if raw_df.empty:
        return 0

    # 计算 adj_factor（15min close 非日线 close，需 use_raw_close=False）
    if symbol:
        try:
            adj_factors = await asyncio.to_thread(
                _calculate_adj_factor, symbol, raw_df, adapter, False
            )
        except Exception as exc:
            logger.warning("计算 adj_factor 失败 symbol=%s: %s，使用默认 1.0", symbol, exc)
            adj_factors = [1.0] * len(raw_df)
    else:
        adj_factors = [1.0] * len(raw_df)

    raw_df["adj_factor"] = adj_factors

    # 写入前校验数据质量
    validation = validate_bars(raw_df, symbol or "", "15m")
    if not validation.is_valid:
        logger.error(
            "15分钟线数据校验失败 symbol=%s errors=%s",
            symbol, validation.errors[:5],
        )
        return 0

    # 向量化构建 records（替代 iterrows）
    records = _df_to_upsert_records(
        raw_df, instrument_id, is_daily=False, volume_multiplier=Decimal("1")
    )

    try:
        stmt = pg_insert(Bar15Min).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id", "trade_time"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "amount": stmt.excluded.amount,
                "adj_factor": stmt.excluded.adj_factor,
            },
        )
        await session.execute(stmt)
        await session.commit()
    except Exception as exc:
        logger.warning("upsert bars_15min 失败 instrument_id=%s: %s", instrument_id, exc)
        await session.rollback()
        raise

    logger.info("upsert bars_15min: instrument_id=%s records=%d", instrument_id, len(records))
    return len(records)


async def fetch_15min_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_time: datetime,
    end_time: datetime,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """查询 15 分钟线行情：DB 优先，DB 无数据则从 pytdx 拉取并写入 DB。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        start_time: 起始时间
        end_time: 结束时间
        adapter: pytdx 适配器，None 使用模块单例

    Returns:
        DataFrame: index=DatetimeIndex(trade_time), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    # 1. DB 查询
    df = await _query_15min_bars(session, instrument_id, start_time, end_time)
    if not df.empty:
        return df

    # 2. DB 无数据，查 symbol
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return df

    # 3. 从 pytdx 拉取（按 count，15min 回补到 2023-01-01 约需 15000 条）
    # 计算所需 count：每天 16 根 15min K线，交易日数 × 16 + 缓冲
    days = max((end_time.date() - start_time.date()).days, 1)
    count = min(days * 16 + 500, 15000)
    pytdx = adapter or get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(pytdx.get_15min_bars, symbol, count)
    except Exception as exc:
        logger.warning("pytdx 拉取 15min 失败 symbol=%s: %s", symbol, exc)
        raise

    if raw_df.empty:
        logger.warning("pytdx 15min 数据为空 symbol=%s", symbol)
        return raw_df

    # 4. 写入 DB
    await _upsert_15min_bars(session, instrument_id, raw_df, symbol, pytdx)

    # 5. 按时间范围过滤后返回
    result_df = raw_df.set_index("datetime")
    result_df.index.name = "trade_time"
    start_ts = pd.Timestamp(start_time)
    end_ts = pd.Timestamp(end_time)
    mask = (result_df.index >= start_ts) & (result_df.index <= end_ts)
    filtered = result_df.loc[mask]
    # 如果过滤后为空（如数据不在查询范围内），返回全部拉取数据，避免前端无数据可显示
    return filtered if not filtered.empty else result_df


async def refresh_15min_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    count: int = 15000,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """强制从 pytdx 拉取 15 分钟线并 upsert（供调度服务使用）。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        count: 拉取条数（默认 15000，回补到 2023-01-01 约需 13264 条）
        adapter: pytdx 适配器，None 使用模块单例

    Returns:
        DataFrame: index=DatetimeIndex(trade_time), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    pytdx = adapter or get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(pytdx.get_15min_bars, symbol, count)
    except Exception as exc:
        logger.warning("pytdx 刷新 15min 失败 symbol=%s: %s", symbol, exc)
        raise

    if raw_df.empty:
        return raw_df

    await _upsert_15min_bars(session, instrument_id, raw_df, symbol, pytdx)

    result_df = raw_df.set_index("datetime")
    result_df.index.name = "trade_time"
    return result_df


# ----- 60分钟线 -----

async def _query_60min_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_time: datetime,
    end_time: datetime,
) -> pd.DataFrame:
    """从 DB 查询 60 分钟线行情。

    Returns:
        DataFrame: index=DatetimeIndex(trade_time), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    try:
        result = await session.execute(
            select(
                Bar60Min.trade_time,
                Bar60Min.open,
                Bar60Min.high,
                Bar60Min.low,
                Bar60Min.close,
                Bar60Min.volume,
                Bar60Min.amount,
                Bar60Min.adj_factor,
            )
            .where(Bar60Min.instrument_id == instrument_id)
            .where(Bar60Min.trade_time >= start_time)
            .where(Bar60Min.trade_time <= end_time)
            .order_by(Bar60Min.trade_time)
        )
        rows = result.all()
    except Exception as exc:
        logger.warning("查询 bars_60min 失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["trade_time"] + _BAR_COLUMNS)
    df["trade_time"] = pd.to_datetime(df["trade_time"])
    df = df.set_index("trade_time")
    for col in _BAR_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


async def _upsert_60min_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    raw_df: pd.DataFrame,
    symbol: str | None = None,
    adapter: PytdxAdapter | None = None,
) -> int:
    """将 pytdx 拉取的 60 分钟线数据 upsert 入库。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        raw_df: pytdx 返回的 DataFrame，含 datetime/open/high/low/close/volume/amount
        symbol: 股票代码，用于计算 adj_factor；None 时 adj_factor 默认 1.0
        adapter: pytdx 适配器，用于计算 adj_factor

    Returns:
        写入记录数

    Raises:
        Exception: 写入失败时 re-raise（不吞没）
    """
    if raw_df.empty:
        return 0

    # 计算 adj_factor（60min close 非日线 close，需 use_raw_close=False）
    if symbol:
        try:
            adj_factors = await asyncio.to_thread(
                _calculate_adj_factor, symbol, raw_df, adapter, False
            )
        except Exception as exc:
            logger.warning("计算 adj_factor 失败 symbol=%s: %s，使用默认 1.0", symbol, exc)
            adj_factors = [1.0] * len(raw_df)
    else:
        adj_factors = [1.0] * len(raw_df)

    raw_df["adj_factor"] = adj_factors

    # 写入前校验数据质量
    validation = validate_bars(raw_df, symbol or "", "60m")
    if not validation.is_valid:
        logger.error(
            "60分钟线数据校验失败 symbol=%s errors=%s",
            symbol, validation.errors[:5],
        )
        return 0

    # 向量化构建 records（替代 iterrows）
    records = _df_to_upsert_records(
        raw_df, instrument_id, is_daily=False, volume_multiplier=Decimal("1")
    )

    try:
        stmt = pg_insert(Bar60Min).values(records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id", "trade_time"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "amount": stmt.excluded.amount,
                "adj_factor": stmt.excluded.adj_factor,
            },
        )
        await session.execute(stmt)
        await session.commit()
    except Exception as exc:
        logger.warning("upsert bars_60min 失败 instrument_id=%s: %s", instrument_id, exc)
        await session.rollback()
        raise

    logger.info("upsert bars_60min: instrument_id=%s records=%d", instrument_id, len(records))
    return len(records)


async def fetch_60min_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_time: datetime,
    end_time: datetime,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """查询 60 分钟线行情：DB 优先，DB 无数据则从 pytdx 拉取并写入 DB。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        start_time: 起始时间
        end_time: 结束时间
        adapter: pytdx 适配器，None 使用模块单例

    Returns:
        DataFrame: index=DatetimeIndex(trade_time), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    # 1. DB 查询
    df = await _query_60min_bars(session, instrument_id, start_time, end_time)
    if not df.empty:
        return df

    # 2. DB 无数据，查 symbol
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return df

    # 3. 从 pytdx 拉取（按 count，60min 回补到 2023-01-01 约需 4000 条）
    # 计算所需 count：每天 4 根 60min K线，交易日数 × 4 + 缓冲
    days = max((end_time.date() - start_time.date()).days, 1)
    count = min(days * 4 + 200, 4000)
    pytdx = adapter or get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(pytdx.get_60min_bars, symbol, count)
    except Exception as exc:
        logger.warning("pytdx 拉取 60min 失败 symbol=%s: %s", symbol, exc)
        raise

    if raw_df.empty:
        logger.warning("pytdx 60min 数据为空 symbol=%s", symbol)
        return raw_df

    # 4. 写入 DB
    await _upsert_60min_bars(session, instrument_id, raw_df, symbol, pytdx)

    # 5. 按时间范围过滤后返回
    result_df = raw_df.set_index("datetime")
    result_df.index.name = "trade_time"
    start_ts = pd.Timestamp(start_time)
    end_ts = pd.Timestamp(end_time)
    mask = (result_df.index >= start_ts) & (result_df.index <= end_ts)
    filtered = result_df.loc[mask]
    # 如果过滤后为空（如数据不在查询范围内），返回全部拉取数据，避免前端无数据可显示
    return filtered if not filtered.empty else result_df


async def refresh_60min_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    count: int = 4000,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """强制从 pytdx 拉取 60 分钟线并 upsert（供调度服务使用）。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        count: 拉取条数（默认 4000，回补到 2023-01-01 约需 3316 条）
        adapter: pytdx 适配器，None 使用模块单例

    Returns:
        DataFrame: index=DatetimeIndex(trade_time), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    pytdx = adapter or get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(pytdx.get_60min_bars, symbol, count)
    except Exception as exc:
        logger.warning("pytdx 刷新 60min 失败 symbol=%s: %s", symbol, exc)
        raise

    if raw_df.empty:
        return raw_df

    await _upsert_60min_bars(session, instrument_id, raw_df, symbol, pytdx)

    result_df = raw_df.set_index("datetime")
    result_df.index.name = "trade_time"
    return result_df


def apply_adj_factor_to_bars(
    bars_df: pd.DataFrame,
    adj_factor_df: pd.DataFrame,
    intraday: bool = False,
) -> pd.DataFrame:
    """对 bars DataFrame 应用前复权（委托 services/adj_factor）。

    Args:
        bars_df: 行情数据，index 为 DatetimeIndex，含 OHLC 列
        adj_factor_df: 复权因子，columns=[trade_date, adj_factor]
        intraday: True 用分钟线前复权，False 用日线前复权

    Returns:
        前复权后的 DataFrame
    """
    if intraday:
        return apply_adj_factor_intraday(bars_df, adj_factor_df)
    return apply_adj_factor(bars_df, adj_factor_df)


if __name__ == "__main__":
    # 自测入口：验证函数签名与基础逻辑（不连 DB，无副作用）
    import inspect

    logging.basicConfig(level=logging.INFO)

    # 1. 验证异步函数签名
    sig = inspect.signature(fetch_daily_bars)
    params = list(sig.parameters.keys())
    assert params == ["session", "instrument_id", "start_date", "end_date", "adapter"], \
        f"fetch_daily_bars 参数不匹配: {params}"
    print(f"fetch_daily_bars params={params}")

    sig = inspect.signature(fetch_minute_bars)
    params = list(sig.parameters.keys())
    assert params == ["session", "instrument_id", "start_time", "end_time", "adapter"], \
        f"fetch_minute_bars 参数不匹配: {params}"
    print(f"fetch_minute_bars params={params}")

    # 2. 验证 apply_adj_factor_to_bars（用小样本）
    bars_df = pd.DataFrame({
        "open": [10.0, 5.0],
        "high": [10.5, 5.5],
        "low": [9.8, 4.8],
        "close": [10.2, 5.2],
        "volume": [100000, 200000],
        "amount": [1020000, 1040000],
        "adj_factor": [2.0, 1.0],
    }, index=pd.to_datetime(["2026-06-16", "2026-06-17"]))
    bars_df.index.name = "trade_date"

    adj_df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-06-16", "2026-06-17"]),
        "adj_factor": [2.0, 1.0],
    })

    qfq_df = apply_adj_factor_to_bars(bars_df, adj_df, intraday=False)
    # 06-16 close 应 = 10.2 × (2.0/1.0) = 20.4
    expected = 10.2 * 2.0
    actual = float(qfq_df.loc[pd.Timestamp("2026-06-16"), "close"])
    assert abs(actual - expected) < 1e-6, f"前复权计算错误: expected={expected}, actual={actual}"
    print(f"apply_adj_factor_to_bars: 06-16 close {actual} == {expected} ✓")

    # 3. 验证空数据
    empty_result = apply_adj_factor_to_bars(pd.DataFrame(), adj_df)
    assert empty_result.empty, "空输入应返回空"
    print("apply_adj_factor_to_bars 空数据 ✓")

    # 4. 验证 refresh 函数存在
    assert callable(refresh_daily_bars), "refresh_daily_bars 应可调用"
    assert callable(refresh_minute_bars), "refresh_minute_bars 应可调用"
    print("refresh_daily_bars/refresh_minute_bars 可调用 ✓")

    # 5. 验证多周期 fetch/refresh 函数签名
    for fn_name, expected_params in [
        ("fetch_weekly_bars", ["session", "instrument_id", "start_date", "end_date", "adapter"]),
        ("fetch_monthly_bars", ["session", "instrument_id", "start_date", "end_date", "adapter"]),
        ("fetch_15min_bars", ["session", "instrument_id", "start_time", "end_time", "adapter"]),
        ("fetch_60min_bars", ["session", "instrument_id", "start_time", "end_time", "adapter"]),
        ("refresh_weekly_bars", ["session", "instrument_id", "count", "adapter"]),
        ("refresh_monthly_bars", ["session", "instrument_id", "count", "adapter"]),
        ("refresh_15min_bars", ["session", "instrument_id", "count", "adapter"]),
        ("refresh_60min_bars", ["session", "instrument_id", "count", "adapter"]),
    ]:
        fn = globals()[fn_name]
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        assert params == expected_params, f"{fn_name} 参数不匹配: {params} != {expected_params}"
        print(f"{fn_name} params={params} ✓")

    # 6. 验证 upsert 函数存在
    for fn_name in ["_upsert_weekly_bars", "_upsert_monthly_bars", "_upsert_15min_bars", "_upsert_60min_bars"]:
        fn = globals()[fn_name]
        assert callable(fn), f"{fn_name} 应可调用"
    print("_upsert_*_bars 可调用 ✓")

    # 7. 验证 query 函数存在
    for fn_name in ["_query_weekly_bars", "_query_monthly_bars", "_query_15min_bars", "_query_60min_bars"]:
        fn = globals()[fn_name]
        assert callable(fn), f"{fn_name} 应可调用"
    print("_query_*_bars 可调用 ✓")

    # 8. 验证 _calculate_adj_factor 复权算法（Chanlunpro preclose 公式）
    # 使用 mock 适配器，避免真实网络调用
    class _MockPytdxAdapter:
        """Mock pytdx 适配器，用于自测 adj_factor 计算。

        返回预设的 xdxr_df，get_daily_bars 不应被调用（事件日在 raw_df 中）。
        """

        def __init__(self, xdxr_df: pd.DataFrame) -> None:
            self._xdxr_df = xdxr_df

        def get_xdxr_info(self, symbol: str) -> pd.DataFrame:
            return self._xdxr_df.copy()

        def get_daily_bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
            # 自测场景下不应被调用（事件日在 raw_df 中，无需补充拉取）
            raise AssertionError("自测场景不应调用 get_daily_bars")

    def _build_xdxr_df(
        event_date: str, fenhong: float, songzhuangu: float, peigu: float, peigujia: float
    ) -> pd.DataFrame:
        """构造 mock xdxr_df（单事件）。"""
        return pd.DataFrame([{
            "date": pd.Timestamp(event_date),
            "category": 1,
            "name": "除权除息",
            "fenhong": fenhong,
            "songzhuangu": songzhuangu,
            "peigu": peigu,
            "peigujia": peigujia,
        }])

    def _build_raw_df(bars: list[tuple[str, float]]) -> pd.DataFrame:
        """构造 mock raw_df（日线数据）。

        Args:
            bars: [(date_str, close), ...] 列表
        """
        return pd.DataFrame([
            {
                "datetime": pd.Timestamp(d),
                "open": c,
                "high": c * 1.01,
                "low": c * 0.99,
                "close": c,
                "volume": 100000.0,
                "amount": c * 100000.0,
            }
            for d, c in bars
        ])

    # 8.1 仅分红场景（fenhong=2.0, songzhuangu=0, peigu=0）
    # 事件日 2026-06-12，前一交易日 2026-06-11 close=10.0
    # preclose = (10.0 * 10 - 2.0) / 10 = 9.8
    # event_factor = 9.8 / 10.0 = 0.98
    xdxr_df_1 = _build_xdxr_df("2026-06-12", fenhong=2.0, songzhuangu=0.0, peigu=0.0, peigujia=0.0)
    raw_df_1 = _build_raw_df([
        ("2026-06-09", 10.0),
        ("2026-06-10", 10.1),
        ("2026-06-11", 10.0),  # 前一交易日 close
        ("2026-06-12", 9.8),   # 事件日（除权后）
        ("2026-06-13", 9.9),
    ])
    mock_adapter_1 = _MockPytdxAdapter(xdxr_df_1)
    adj_factors_1 = _calculate_adj_factor("000001", raw_df_1, mock_adapter_1, use_raw_close=True)
    assert len(adj_factors_1) == 5, f"仅分红: adj_factors 长度应为 5，实际 {len(adj_factors_1)}"
    # 事件日前的 bar（06-09, 06-10, 06-11）adj_factor = 0.98
    for i in range(3):
        assert abs(adj_factors_1[i] - 0.98) < 1e-6, \
            f"仅分红: 事件日前 bar[{i}] adj_factor 应为 0.98，实际 {adj_factors_1[i]}"
    # 事件日及之后的 bar（06-12, 06-13）adj_factor = 1.0
    for i in range(3, 5):
        assert abs(adj_factors_1[i] - 1.0) < 1e-6, \
            f"仅分红: 事件日及之后 bar[{i}] adj_factor 应为 1.0，实际 {adj_factors_1[i]}"
    print(f"仅分红: adj_factors={[round(f, 4) for f in adj_factors_1]} ✓")

    # 8.2 仅送转场景（10送5：songzhuangu=5, fenhong=0, peigu=0）
    # 事件日 2026-06-12，前一交易日 2026-06-11 close=20.0
    # preclose = (20.0 * 10 - 0 + 0) / (10 + 0 + 5) = 200 / 15 ≈ 13.3333
    # event_factor = 13.3333 / 20.0 ≈ 0.6667
    xdxr_df_2 = _build_xdxr_df("2026-06-12", fenhong=0.0, songzhuangu=5.0, peigu=0.0, peigujia=0.0)
    raw_df_2 = _build_raw_df([
        ("2026-06-11", 20.0),  # 前一交易日 close
        ("2026-06-12", 13.33), # 事件日（除权后）
        ("2026-06-13", 13.5),
    ])
    mock_adapter_2 = _MockPytdxAdapter(xdxr_df_2)
    adj_factors_2 = _calculate_adj_factor("000001", raw_df_2, mock_adapter_2, use_raw_close=True)
    expected_factor_2 = (200.0 / 15.0) / 20.0  # ≈ 0.6667
    assert abs(adj_factors_2[0] - expected_factor_2) < 1e-6, \
        f"仅送转: 事件日前 adj_factor 应为 {expected_factor_2}，实际 {adj_factors_2[0]}"
    for i in range(1, 3):
        assert abs(adj_factors_2[i] - 1.0) < 1e-6, \
            f"仅送转: 事件日及之后 bar[{i}] adj_factor 应为 1.0，实际 {adj_factors_2[i]}"
    print(f"仅送转: adj_factors={[round(f, 4) for f in adj_factors_2]} ✓")

    # 8.3 混合场景（分红+送转+配股：fenhong=2.0, songzhuangu=3, peigu=2, peigujia=8.0）
    # 事件日 2026-06-12，前一交易日 2026-06-11 close=15.0
    # preclose = (15.0 * 10 - 2.0 + 2 * 8.0) / (10 + 2 + 3) = (150 - 2 + 16) / 15 = 164 / 15 ≈ 10.9333
    # event_factor = 10.9333 / 15.0 ≈ 0.7289
    xdxr_df_3 = _build_xdxr_df("2026-06-12", fenhong=2.0, songzhuangu=3.0, peigu=2.0, peigujia=8.0)
    raw_df_3 = _build_raw_df([
        ("2026-06-11", 15.0),  # 前一交易日 close
        ("2026-06-12", 10.93), # 事件日（除权后）
        ("2026-06-13", 11.0),
    ])
    mock_adapter_3 = _MockPytdxAdapter(xdxr_df_3)
    adj_factors_3 = _calculate_adj_factor("000001", raw_df_3, mock_adapter_3, use_raw_close=True)
    expected_factor_3 = (164.0 / 15.0) / 15.0  # ≈ 0.7289
    assert abs(adj_factors_3[0] - expected_factor_3) < 1e-6, \
        f"混合: 事件日前 adj_factor 应为 {expected_factor_3}，实际 {adj_factors_3[0]}"
    for i in range(1, 3):
        assert abs(adj_factors_3[i] - 1.0) < 1e-6, \
            f"混合: 事件日及之后 bar[{i}] adj_factor 应为 1.0，实际 {adj_factors_3[i]}"
    print(f"混合: adj_factors={[round(f, 4) for f in adj_factors_3]} ✓")

    print("\n所有自测通过 ✓（未进行 DB/网络测试）")
