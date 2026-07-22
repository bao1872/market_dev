"""行情仓储 - 日线与分钟线数据的 DB 查询、pytdx 拉取（1m 仅手动/按需）、前复权应用。

从 ref/交易/datasource/k_data_loader.py 迁移行情加载逻辑，关键改进：
1. 异步 DB 访问：使用 AsyncSession（与 app/db.py 一致）。
2. DB 缺失自动回填：DB 无数据时从 pytdx 拉取并 upsert 入库（原始 k_data_loader 仅从 DB 读）。
3. pytdx 同步调用通过 asyncio.to_thread 桥接，不阻塞事件循环。
4. 前复权委托 services/adj_factor（纯计算，已向量化）。
5. 禁异常吞没：所有异常补充上下文后 re-raise。

设计说明：
- instrument_id 为 UUID（V1.1），pytdx 使用 symbol（6 位代码）；通过 instruments 表转换。
- bars_daily/bars_minute 表自带 adj_factor 列；前复权时从表中提取 distinct (trade_date, adj_factor)。
- pytdx 不提供 adj_factor，拉取写入时 adj_factor 默认 1.0；adj_factor 的实际获取属另一任务。

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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import select

from app.core.pytdx_adapter import PytdxAdapter, get_pytdx_adapter
from app.core.time import SHANGHAI_TZ
from app.models.bar import Bar15Min, Bar60Min, BarDaily, BarMinute, BarMonthly, BarWeekly
from app.services.adj_factor import apply_adj_factor, apply_adj_factor_intraday
from app.services.adjustment_factor_calculator import (
    AdjustmentFactorDataError,
    calculate_adjustment_factor_series,
)
from app.services.bars_validator import validate_bars

if TYPE_CHECKING:
    from typing import Protocol

    from sqlalchemy.sql.expression import Executable

    class _AdjFactorAdapterLike(Protocol):
        """_calculate_adj_factor 所需的 pytdx 适配器接口（结构化类型）。"""

        def get_xdxr_info(self, symbol: str) -> pd.DataFrame: ...

        def get_daily_bars(
            self, symbol: str, start: date, end: date
        ) -> pd.DataFrame: ...

    class _ResultLike(Protocol):
        """session.execute 返回结果的最小接口（结构化类型）。"""

        def all(self) -> list: ...

    class _SessionLike(Protocol):
        """_map_adj_factor_from_daily 所需的 session 接口（结构化类型）。"""

        async def execute(self, statement: Executable) -> _ResultLike: ...

logger = logging.getLogger("bar_repository")

# 行情数据列（DB 查询返回的标准列）
_BAR_COLUMNS = ["open", "high", "low", "close", "volume", "amount", "adj_factor"]

# asyncpg 单次查询参数上限 32767，每条 record 9 列，故每批最多 32767 // 9 = 3640 条
# 取 3000 留安全余量
_UPSERT_BATCH_SIZE = 3000

# [行情] - get_bars 支持的 timeframe → fetch 函数映射
_TIMEFRAME_MAP: dict[str, str] = {
    "1d": "daily",
    "15m": "15min",
    "1m": "minute",
}


@dataclass
class BarsResult:
    """统一行情查询结果，包含元数据。"""

    bars: pd.DataFrame  # 行情数据（OHLCV + adj_factor）
    source: str  # "db", "pytdx", "db+pytdx"
    timeframe: str  # "1d", "15m", "1m", etc.
    adjustment: str | None  # None, "qfq"
    first_bar_time: pd.Timestamp | None = None
    last_bar_time: pd.Timestamp | None = None
    completed_through: pd.Timestamp | None = None  # 最新已完成 Bar 时间
    is_complete: bool = True  # 数据是否完整（无缺口）
    missing_ranges: list[tuple[pd.Timestamp, pd.Timestamp]] = field(default_factory=list)


async def _batch_upsert_bars(
    session: AsyncSession,
    model: type,
    records: list[dict[str, Any]],
    index_elements: list[str],
    label: str,
    instrument_id: uuid.UUID,
) -> int:
    """分批 upsert 行情数据，避免 asyncpg 32767 参数限制。

    每批最多 _UPSERT_BATCH_SIZE 条记录（9 列 × 3000 = 27000 < 32767）。
    所有批次成功后统一 commit；任一批次失败则 rollback 并 re-raise。

    Args:
        session: 异步会话
        model: ORM 模型类（BarDaily/Bar15Min/Bar60Min 等）
        records: upsert 记录列表
        index_elements: 冲突检测列（如 ["instrument_id", "trade_time"]）
        label: 日志标识（如 "bars_15min"）
        instrument_id: 标的 UUID（用于日志）

    Returns:
        写入记录总数

    Raises:
        Exception: 任一批次写入失败时 re-raise（不吞没）
    """
    if not records:
        return 0

    total = len(records)
    for i in range(0, total, _UPSERT_BATCH_SIZE):
        batch = records[i:i + _UPSERT_BATCH_SIZE]
        try:
            stmt = pg_insert(model).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=index_elements,
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
        except Exception as exc:
            logger.warning(
                "upsert %s 失败 instrument_id=%s batch=%d-%d/%d: %s",
                label, instrument_id, i, i + len(batch), total, exc,
            )
            await session.rollback()
            raise

    await session.commit()
    logger.info("upsert %s: instrument_id=%s records=%d", label, instrument_id, total)
    return total


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


async def _get_listing_date(session: AsyncSession, instrument_id: uuid.UUID) -> date | None:
    """查询 instruments 表获取上市日期。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID

    Returns:
        上市日期（date）或 None（未记录或标的不存在）
    """
    try:
        result = await session.execute(
            text("SELECT listing_date FROM instruments WHERE id = :id"),
            {"id": instrument_id},
        )
        row = result.first()
        return row[0] if row else None
    except Exception as exc:
        logger.warning(
            "查询 instrument listing_date 失败 instrument_id=%s: %s",
            instrument_id,
            exc,
        )
        return None


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
    limit: int | None = None,
) -> pd.DataFrame:
    """从 DB 查询分钟线行情。

    Args:
        limit: 若指定，返回 end_time 之前最近 N 根（DESC + LIMIT + 反转），
               避免加载全量再截取。

    Returns:
        DataFrame: index=DatetimeIndex(trade_time), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    try:
        stmt = (
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
        )
        if limit is not None:
            stmt = stmt.order_by(BarMinute.trade_time.desc()).limit(limit)
        else:
            stmt = stmt.order_by(BarMinute.trade_time)
        result = await session.execute(stmt)
        rows = result.all()
    except Exception as exc:
        logger.warning("查询 bars_minute 失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if not rows:
        return pd.DataFrame()

    # limit 模式下 DESC 查询，反转为升序
    if limit is not None:
        rows = rows[::-1]

    df = pd.DataFrame(rows, columns=["trade_time"] + _BAR_COLUMNS)
    # DB 读取的 timestamptz 返回 UTC 时区感知 datetime，需转为 naive 上海时间与 pytdx 一致
    _ts = pd.to_datetime(df["trade_time"])
    if getattr(_ts.dt, "tz", None) is not None:
        _ts = _ts.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    df["trade_time"] = _ts
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
    start_date: date | None = None,
) -> int:
    """将 pytdx 拉取的日线数据 upsert 入库。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        raw_df: pytdx 返回的 DataFrame，含 datetime/open/high/low/close/volume/amount
        symbol: 股票代码，用于计算 adj_factor；None 时 adj_factor 默认 1.0
        adapter: pytdx 适配器，用于计算 adj_factor
        start_date: 日线回补起始日期，传递给 _calculate_adj_factor 的 min_date 参数

    Returns:
        写入记录数

    Raises:
        Exception: 写入失败时 re-raise（不吞没）
    """
    if raw_df.empty:
        return 0

    # 计算 adj_factor（基于 pytdx 除权除息数据）
    # [CHANGE-20260719-001 §1.2] _calculate_adj_factor wrapper 已删除 use_raw_close/min_date 参数，
    # 内部委托 calculate_adjustment_factor_series 纯函数 + 保留 supplement_df 补齐
    if symbol:
        try:
            adj_factors = await asyncio.to_thread(
                _calculate_adj_factor, symbol, raw_df, adapter,
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

    # 从日线表映射 adj_factor（保证分钟线与日线 adj_factor 一致）
    try:
        adj_factors = await _map_adj_factor_from_daily(session, instrument_id, raw_df)
    except Exception as exc:
        raise RuntimeError(
            f"从日线表映射 adj_factor 失败 instrument_id={instrument_id} "
            f"bar_count={len(raw_df)}: {exc}"
        ) from exc

    raw_df["adj_factor"] = adj_factors

    # 写入前校验数据质量
    validation = validate_bars(raw_df, "", "minute")
    if not validation.is_valid:
        logger.error(
            "分钟线数据校验失败 instrument_id=%s errors=%s",
            instrument_id, validation.errors[:5],
        )
        return 0

    # 向量化构建 records（替代 iterrows，分钟线 adj_factor 从日线表映射）
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
    adapter: _AdjFactorAdapterLike | None = None,
) -> list[float]:
    """[Deprecated CHANGE-20260719-001 §1.2] 委托给 calculate_adjustment_factor_series 纯函数。

    保留向后兼容：拉 xdxr + 补齐 supplement_df + 调纯函数。
    新代码应直接调用 calculate_adjustment_factor_series（不补齐 supplement_df，
    数据缺失时抛 AdjustmentFactorDataError，由调用方决定 degraded 或 re-raise）。

    删除的参数（000688 根因修复）：
    - use_raw_close: 死代码（所有调用方都传 True）；MDAS 统一行情出口后
      分钟线/周线/月线不再调用本函数（通过 apply_adj_factor_intraday 应用日线因子）
    - min_date: 000688 根因（过滤事件导致累积因子不完整）；纯函数处理全部事件

    保留的行为（仅 _upsert_daily_bars 回补场景使用）：
    - xdxr 获取失败 → 返回全 1.0（向后兼容）
    - 事件日 close 缺失 → 拉 supplement_df 补齐 → 调纯函数
    - 纯函数抛 AdjustmentFactorDataError → 返回全 1.0（向后兼容）

    Args:
        symbol: 股票代码（如 '000001'）
        raw_df: pytdx 返回的 DataFrame，含 datetime 列（用于提取 bar 日期）
        adapter: pytdx 适配器，None 使用模块单例

    Returns:
        adj_factor 列表，与 raw_df 行一一对应；获取失败时全为 1.0
    """
    if raw_df.empty:
        return []

    default_factors = [1.0] * len(raw_df)

    pytdx = adapter or get_pytdx_adapter()
    try:
        xdxr_df = pytdx.get_xdxr_info(symbol)
    except Exception as exc:
        logger.warning("获取除权除息数据失败 symbol=%s: %s，adj_factor 默认 1.0", symbol, exc)
        return default_factors

    if xdxr_df is None or xdxr_df.empty:
        return default_factors

    # 筛选 category=1 的除权除息事件
    exc_events = xdxr_df[xdxr_df["category"] == 1].copy()
    if exc_events.empty:
        return default_factors

    # 构建 close 查找表：date -> close（从 raw_df）
    close_map: dict[date, float] = {}
    for _, row in raw_df.iterrows():
        dt = pd.Timestamp(row["datetime"]).date()
        close_map[dt] = float(row["close"])

    # 检测 missing_dates：事件日不在 close_map 中
    missing_dates: list[date] = []
    for _, event in exc_events.iterrows():
        event_date = pd.Timestamp(event["date"]).date()
        if event_date not in close_map:
            missing_dates.append(event_date)

    # 补齐 supplement_df（仅 _upsert_daily_bars 回补场景需要）
    # 新代码（rebuild_adj_factors / compute_expected_adj_factors）不补齐，
    # 数据缺失时抛 AdjustmentFactorDataError，由调用方决定 degraded 或 re-raise
    extended_raw_df = raw_df
    if missing_dates:
        all_event_dates = [
            pd.Timestamp(e["date"]).date() for _, e in exc_events.iterrows()
        ]
        min_d = min(all_event_dates) if all_event_dates else date.today()
        max_d = max(all_event_dates) if all_event_dates else date.today()
        # 向前扩展 10 天确保覆盖前一交易日
        fetch_start = min_d - timedelta(days=10)
        try:
            supplement_df = pytdx.get_daily_bars(symbol, fetch_start, max_d)
            if supplement_df is not None and not supplement_df.empty:
                extended_raw_df = pd.concat(
                    [raw_df, supplement_df], ignore_index=True
                )
                extended_raw_df = extended_raw_df.drop_duplicates(
                    subset=["datetime"]
                ).sort_values("datetime").reset_index(drop=True)
        except Exception as exc:
            logger.warning(
                "补充拉取事件日收盘价失败 symbol=%s dates=%s~%s: %s",
                symbol, fetch_start, max_d, exc,
            )

    # 调纯函数（算法唯一入口）
    try:
        all_factors = calculate_adjustment_factor_series(extended_raw_df, xdxr_df)
    except AdjustmentFactorDataError as exc:
        logger.warning(
            "纯函数计算失败 symbol=%s: %s，使用默认 1.0", symbol, exc,
        )
        return default_factors

    # 只返回与原 raw_df 行对应的 factor
    if len(extended_raw_df) == len(raw_df):
        return all_factors

    # extended_raw_df 包含 supplement_df 的行，通过 datetime 匹配提取原 raw_df 的 factor
    extended_df = extended_raw_df.copy()
    extended_df["_factor"] = all_factors
    extended_df["_date"] = extended_df["datetime"].apply(
        lambda x: pd.Timestamp(x).date()
    )
    factor_map = dict(
        zip(extended_df["_date"], extended_df["_factor"], strict=True)
    )
    original_dates = raw_df["datetime"].apply(
        lambda x: pd.Timestamp(x).date()
    ).tolist()
    return [factor_map.get(d, 1.0) for d in original_dates]


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


async def get_adj_factor_series(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    as_of: date | None = None,
) -> pd.DataFrame:
    """公开别名：获取权威日线因子序列（CHANGE-20260717-002）。

    委托 _get_adj_factor_df，支持 as_of point-in-time 过滤。
    MDAS / AdjustmentFactorService 必须通过此公开别名访问，禁止直接导入 _get_adj_factor_df。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        as_of: 复权锚点日期（None=全量；date=只返回 trade_date <= as_of 的因子，
            用于禁止未来除权事件泄漏）

    Returns:
        DataFrame: columns=[trade_date, adj_factor]，按 trade_date 排序
    """
    df = await _get_adj_factor_df(session, instrument_id)
    if as_of is not None and not df.empty:
        as_of_ts = pd.Timestamp(as_of)
        df = df[df["trade_date"] <= as_of_ts].reset_index(drop=True)
    return df


async def rebuild_adj_factors(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    symbol: str,
    earliest_affected: date,
    adapter: PytdxAdapter | None = None,
) -> int:
    """公开别名：公司行为变化时重建 adj_factor 序列（CHANGE-20260717-002）。

    从 earliest_affected 起重新计算该股票完整日线 adj_factor 序列并原子 upsert。
    仅更新 adj_factor 列，不修改 OHLCV。失败时 re-raise（不吞没，不伪装）。

    [CHANGE-20260719-001 §1.2] 算法迁移到 calculate_adjustment_factor_series 纯函数：
    - 查询全量 bars_daily（不限日期，确保事件日前一交易日 close 可查找）
    - 显式拉 xdxr（不再通过 _calculate_adj_factor wrapper）
    - 调纯函数（不补齐 supplement_df；数据缺失抛 AdjustmentFactorDataError）
    - 只写入 >= earliest_affected 的部分（earliest_affected 之前的事件也会被
      纯函数处理，确保累积因子完整，修复 000688 min_date 过滤事件 bug）

    算法：
    1. 查询 bars_daily 全量 trade_date + OHLCV（确保 prev_close 可查找）
    2. 拉 xdxr（pytdx 除权除息事件）
    3. 调 calculate_adjustment_factor_series 纯函数重算全量因子
    4. 批量 UPDATE bars_daily.adj_factor（仅 >= earliest_affected 的部分）

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        symbol: 股票代码（用于 pytdx xdxr_info）
        earliest_affected: 最早受影响日期（从此日起写入；但纯函数处理全量事件）
        adapter: pytdx 适配器（None 用模块单例）

    Returns:
        更新的记录数（>= earliest_affected 的行数）

    Raises:
        RuntimeError: 数据缺失（AdjustmentFactorDataError）时抛出，
            调用方需补齐 bars_daily 后重试（禁止 1.0 伪装成功）
        Exception: 重算或 upsert 失败时 re-raise
    """
    # 1. 查询全量 bars_daily（不限日期，确保事件日前一交易日 close 可查找）
    try:
        result = await session.execute(
            select(
                BarDaily.trade_date, BarDaily.open, BarDaily.high,
                BarDaily.low, BarDaily.close, BarDaily.volume, BarDaily.amount,
            )
            .where(BarDaily.instrument_id == instrument_id)
            .order_by(BarDaily.trade_date)
        )
        rows = result.all()
    except Exception as exc:
        logger.warning(
            "rebuild_adj_factors 查询失败 instrument_id=%s: %s", instrument_id, exc,
        )
        raise

    if not rows:
        logger.info(
            "rebuild_adj_factors 无数据 instrument_id=%s earliest=%s",
            instrument_id, earliest_affected,
        )
        return 0

    # 构造 raw_df 供纯函数使用
    raw_df = pd.DataFrame(rows, columns=[
        "trade_date", "open", "high", "low", "close", "volume", "amount",
    ])
    raw_df["datetime"] = pd.to_datetime(raw_df["trade_date"])
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        raw_df[col] = pd.to_numeric(raw_df[col], errors="coerce")

    # 2. 拉 xdxr（显式拉取，不通过 wrapper）
    pytdx = adapter or get_pytdx_adapter()
    try:
        xdxr_df = await asyncio.to_thread(pytdx.get_xdxr_info, symbol)
    except Exception as exc:
        logger.warning(
            "rebuild_adj_factors 拉 xdxr 失败 symbol=%s: %s", symbol, exc,
        )
        raise

    if xdxr_df is None or xdxr_df.empty:
        # 无除权除息事件：adj_factor 应全 1.0，写入 earliest_affected 之后的所有行
        logger.info(
            "rebuild_adj_factors 无 xdxr 事件 symbol=%s，写入 adj_factor=1.0",
            symbol,
        )
        adj_factors: list[float] = [1.0] * len(raw_df)
    else:
        # 3. 调纯函数（不补齐 supplement_df）
        try:
            adj_factors = await asyncio.to_thread(
                calculate_adjustment_factor_series, raw_df, xdxr_df,
            )
        except AdjustmentFactorDataError as exc:
            # 数据缺失（如 000688 bars_daily 缺口）：不 1.0 伪装，抛异常让上层处理
            logger.error(
                "rebuild_adj_factors 数据缺失 symbol=%s: %s"
                "（需补齐 bars_daily 后重试，禁止 1.0 伪装）",
                symbol, exc,
            )
            raise RuntimeError(
                f"rebuild_adj_factors 数据缺失 symbol={symbol}: {exc}"
            ) from exc
        except Exception as exc:
            logger.warning(
                "rebuild_adj_factors 计算失败 symbol=%s: %s", symbol, exc,
            )
            raise

    if len(adj_factors) != len(raw_df):
        logger.warning(
            "rebuild_adj_factors 因子数量不匹配 symbol=%s expected=%d got=%d",
            symbol, len(raw_df), len(adj_factors),
        )
        raise ValueError(
            f"adj_factor 数量不匹配: expected={len(raw_df)}, got={len(adj_factors)}"
        )

    # 4. 批量 UPDATE bars_daily.adj_factor（仅 >= earliest_affected 的部分）
    trade_dates = raw_df["trade_date"].tolist()
    written = 0
    try:
        for td, factor in zip(trade_dates, adj_factors, strict=True):
            td_date = td if isinstance(td, date) else pd.Timestamp(td).date()
            if td_date < earliest_affected:
                continue
            await session.execute(
                text(
                    "UPDATE bars_daily SET adj_factor = :factor "
                    "WHERE instrument_id = :iid AND trade_date = :td"
                ),
                {
                    "factor": Decimal(str(factor)),
                    "iid": instrument_id,
                    "td": td_date,
                },
            )
            written += 1
        await session.commit()
    except Exception as exc:
        logger.warning(
            "rebuild_adj_factors upsert 失败 instrument_id=%s: %s", instrument_id, exc,
        )
        await session.rollback()
        raise

    logger.info(
        "rebuild_adj_factors 完成 instrument_id=%s symbol=%s written=%d "
        "total_bars=%d earliest=%s",
        instrument_id, symbol, written, len(trade_dates), earliest_affected,
    )
    return written


async def compute_expected_adj_factors(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    symbol: str,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """公开别名：只读重算预期 adj_factor 序列（CHANGE-20260718-005 全市场一致性审计）。

    从 bars_daily 读取 raw OHLCV，调用 calculate_adjustment_factor_series 纯函数
    重算预期因子，**不写库**。供 FactorConsistencyAuditor 比对存量因子是否正确。

    [CHANGE-20260719-001 §1.2] 算法迁移到纯函数：
    - 删除 min_date 参数（000688 根因：min_date 过滤事件导致累积因子不完整）
    - 不补齐 supplement_df（数据缺失抛 AdjustmentFactorDataError，让 auditor 标记 degraded）
    - 与 rebuild_adj_factors 共用同一纯函数，禁止两套算法

    与 rebuild_adj_factors 的区别：
    - rebuild_adj_factors：重算 + UPDATE 写库（公司行为变化时调用）；
        数据缺失抛 RuntimeError（不 1.0 伪装）
    - compute_expected_adj_factors：只读重算（审计时调用，零副作用）；
        数据缺失抛 AdjustmentFactorDataError（让 auditor 标记 degraded_reason）

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        symbol: 股票代码（用于 pytdx xdxr_info）
        adapter: pytdx 适配器（None 用模块单例）

    Returns:
        DataFrame: columns=[trade_date, expected_adj_factor]，按 trade_date 排序；
                   无数据时返回空 DataFrame

    Raises:
        AdjustmentFactorDataError: 事件日前一交易日 close 缺失（bars_daily 缺口）。
            Auditor 应捕获并标记 degraded_reason，不得归类为算法不一致（mismatch）。
        Exception: 其他计算失败时 re-raise
    """
    try:
        result = await session.execute(
            select(
                BarDaily.trade_date, BarDaily.open, BarDaily.high,
                BarDaily.low, BarDaily.close, BarDaily.volume, BarDaily.amount,
            )
            .where(BarDaily.instrument_id == instrument_id)
            .order_by(BarDaily.trade_date)
        )
        rows = result.all()
    except Exception as exc:
        logger.warning(
            "compute_expected_adj_factors 查询失败 instrument_id=%s: %s",
            instrument_id, exc,
        )
        raise

    if not rows:
        return pd.DataFrame(columns=["trade_date", "expected_adj_factor"])

    raw_df = pd.DataFrame(rows, columns=[
        "trade_date", "open", "high", "low", "close", "volume", "amount",
    ])
    raw_df["datetime"] = pd.to_datetime(raw_df["trade_date"])
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        raw_df[col] = pd.to_numeric(raw_df[col], errors="coerce")

    # 拉 xdxr（显式拉取，不通过 wrapper）
    pytdx = adapter or get_pytdx_adapter()
    try:
        xdxr_df = await asyncio.to_thread(pytdx.get_xdxr_info, symbol)
    except Exception as exc:
        logger.warning(
            "compute_expected_adj_factors 拉 xdxr 失败 symbol=%s: %s", symbol, exc,
        )
        raise

    if xdxr_df is None or xdxr_df.empty:
        # 无除权除息事件：expected 全 1.0
        expected_factors: list[float] = [1.0] * len(raw_df)
    else:
        # 调纯函数（不补齐 supplement_df；数据缺失抛 AdjustmentFactorDataError）
        try:
            expected_factors = await asyncio.to_thread(
                calculate_adjustment_factor_series, raw_df, xdxr_df,
            )
        except AdjustmentFactorDataError:
            # 数据缺失：让 auditor 捕获并标记 degraded_reason
            raise
        except Exception as exc:
            logger.warning(
                "compute_expected_adj_factors 计算失败 symbol=%s: %s", symbol, exc,
            )
            raise

    if len(expected_factors) != len(raw_df):
        logger.warning(
            "compute_expected_adj_factors 因子数量不匹配 symbol=%s expected=%d got=%d",
            symbol, len(raw_df), len(expected_factors),
        )
        raise ValueError(
            f"expected adj_factor 数量不匹配: expected={len(raw_df)}, "
            f"got={len(expected_factors)}"
        )

    return pd.DataFrame({
        "trade_date": raw_df["trade_date"].tolist(),
        "expected_adj_factor": expected_factors,
    })


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

    # 4. 写入 DB（含 adj_factor 计算，传递 start_date 优化性能）
    await _upsert_daily_bars(session, instrument_id, raw_df, symbol, pytdx, start_date)

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
    skip_upsert: bool = False,
) -> pd.DataFrame:
    """查询分钟线行情：DB 优先，DB 无数据则从 pytdx 拉取并写入 DB。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        start_time: 起始时间
        end_time: 结束时间
        adapter: pytdx 适配器，None 使用模块单例
        skip_upsert: 为 True 时仅从 pytdx 读取，不写入 DB（用于实时监控等无需持久化场景）

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

    # 4. 写入 DB（skip_upsert=True 时跳过，用于实时监控等无需持久化场景）
    if not skip_upsert:
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

    await _upsert_daily_bars(session, instrument_id, raw_df, symbol, pytdx, start_date)

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
    """按需从 pytdx 拉取分钟线并 upsert（不参与定时调度，仅供手动/按需调用）。"""
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
# - 日线/15min/60min：
#   - fetch_*_bars：DB 优先，按日期/时间范围查询，无数据则从 pytdx 拉取并入库
#   - refresh_*_bars：强制从 pytdx 拉取（按 count），供调度服务使用
# - 周线/月线：
#   - fetch_*_bars：从日线动态合成（convert_kline_frequency），不存储在 DB
#   - refresh_*_bars：从 DB 日线合并生成 DataFrame，不写入 DB
# - 周线/月线使用 trade_date（Date），15min/60min 使用 trade_time（DateTime）
# - pytdx 不支持并发，所有拉取通过 asyncio.to_thread 串行桥接


def convert_kline_frequency(daily_df: pd.DataFrame, to_f: str) -> pd.DataFrame:
    """将日线 K 线合并为周线/月线。

    参考 chanlunpro exchange.py:152-279 的 convert_stock_kline_frequency。

    核心规则：
    - resample("W") 或 resample("M")
    - label="left", closed="right"（后对齐）
    - OHLCV: open=first, close=last, high=max, low=min, volume=sum, amount=sum
    - 日期取周期内第一个交易日（前对齐）
    - adj_factor 取周期内最后一个交易日的 adj_factor（累积值，代表整个周期复权因子）

    Args:
        daily_df: 日线 DataFrame，index=DatetimeIndex(trade_date),
                  columns=open/high/low/close/volume/amount/adj_factor
        to_f: 目标周期 ("w" 或 "m")

    Returns:
        合并后的 DataFrame，index=DatetimeIndex(trade_date),
        columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame

    Raises:
        ValueError: to_f 不在 {"w", "m"} 时
    """
    if daily_df.empty:
        return pd.DataFrame()

    period_maps = {"w": "W", "m": "ME"}
    if to_f not in period_maps:
        raise ValueError(f"不支持的转换周期：{to_f}，仅支持 'w' 或 'm'")

    period_type = period_maps[to_f]

    # 复制避免修改原数据，并保留原始交易日用于前对齐
    df = daily_df.copy()
    df["_trade_date"] = df.index

    # resample 聚合：label="left", closed="right"（后对齐）
    agg_dict = {
        "_trade_date": "first",  # 周线/月线取周期内第一个交易日（前对齐）
        "open": "first",
        "close": "last",
        "high": "max",
        "low": "min",
        "volume": "sum",
        "amount": "sum",
        "adj_factor": "last",  # 累积值，取周期内最后一个交易日
    }

    period_df = df.resample(period_type, label="left", closed="right").agg(agg_dict)

    # 删除 resample 产生的空周期行（_trade_date 为 NaT 表示该周期无交易日数据）
    period_df = period_df.dropna(subset=["_trade_date"])

    # 用周期内第一个交易日作为 index（前对齐）
    period_df = period_df.set_index("_trade_date")
    period_df.index.name = "trade_date"

    return period_df


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


async def fetch_weekly_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_date: date,
    end_date: date,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """查询周线行情：从日线动态合成（使用 convert_kline_frequency）。

    设计原则：周线/月线不存储在数据库中，从日线动态合成。
    与 chanlunpro ExchangeTdx 设计一致。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        start_date: 起始日期
        end_date: 结束日期
        adapter: 保留兼容性（不再使用）

    Returns:
        DataFrame: index=DatetimeIndex(trade_date), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    # 从日线动态合成
    daily_df = await fetch_daily_bars(session, instrument_id, start_date, end_date)
    if daily_df.empty:
        return daily_df
    return convert_kline_frequency(daily_df, "w")


async def refresh_weekly_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    count: int = 200,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """从日线合成周线数据（不写入 DB）。

    设计原则：周线/月线不存储在数据库中，从日线动态合成。
    此函数保留供需要预合成周线数据的场景使用（如批量计算），但不写入 DB。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        count: 期望的周线条数（用于估算日线回溯天数）。注意：实际回溯天数
            受 max(count*7, 365*5) 下限约束，当 count*7 < 1825 时实际回溯 5 年，
            因此 count 参数在小值时不影响回溯范围。
        adapter: 保留兼容性（不再使用）

    Returns:
        DataFrame: index=DatetimeIndex(trade_date), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    # 1. 从 DB 读取日线数据：count 条周线 ≈ count*5 交易日，向前回溯 count*7 天确保覆盖
    end_date = date.today()
    lookback_days = max(count * 7, 365 * 5)  # 至少回溯 5 年
    start_date = end_date - timedelta(days=lookback_days)

    daily_df = await _query_daily_bars(session, instrument_id, start_date, end_date)
    if daily_df.empty:
        logger.warning("日线数据为空，无法合成周线 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    # 2. 合成周线
    weekly_df = convert_kline_frequency(daily_df, "w")

    # 3. 返回合成结果（不写入 DB）
    return weekly_df


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


async def fetch_monthly_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_date: date,
    end_date: date,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """查询月线行情：从日线动态合成（使用 convert_kline_frequency）。

    设计原则：周线/月线不存储在数据库中，从日线动态合成。
    与 chanlunpro ExchangeTdx 设计一致。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        start_date: 起始日期
        end_date: 结束日期
        adapter: 保留兼容性（不再使用）

    Returns:
        DataFrame: index=DatetimeIndex(trade_date), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    # 从日线动态合成
    daily_df = await fetch_daily_bars(session, instrument_id, start_date, end_date)
    if daily_df.empty:
        return daily_df
    return convert_kline_frequency(daily_df, "m")


async def refresh_monthly_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    count: int = 50,
    adapter: PytdxAdapter | None = None,
) -> pd.DataFrame:
    """从日线合成月线数据（不写入 DB）。

    设计原则：周线/月线不存储在数据库中，从日线动态合成。
    此函数保留供需要预合成月线数据的场景使用（如批量计算），但不写入 DB。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        count: 期望的月线条数（用于估算日线回溯天数）。注意：实际回溯天数
            受 max(count*31, 365*5) 下限约束，当 count*31 < 1825 时实际回溯 5 年，
            因此 count 参数在小值时不影响回溯范围。
        adapter: 保留兼容性（不再使用）

    Returns:
        DataFrame: index=DatetimeIndex(trade_date), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    # 1. 从 DB 读取日线数据：count 条月线 ≈ count*30 天，向前回溯 count*31 天确保覆盖
    end_date = date.today()
    lookback_days = max(count * 31, 365 * 5)  # 至少回溯 5 年
    start_date = end_date - timedelta(days=lookback_days)

    daily_df = await _query_daily_bars(session, instrument_id, start_date, end_date)
    if daily_df.empty:
        logger.warning("日线数据为空，无法合成月线 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    # 2. 合成月线
    monthly_df = convert_kline_frequency(daily_df, "m")

    # 3. 返回合成结果（不写入 DB）
    return monthly_df


# ----- 15分钟线 -----

async def _query_15min_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_time: datetime,
    end_time: datetime,
    limit: int | None = None,
) -> pd.DataFrame:
    """从 DB 查询 15 分钟线行情。

    Args:
        limit: 若指定，返回 end_time 之前最近 N 根（DESC + LIMIT + 反转），
               避免加载全量再截取。

    Returns:
        DataFrame: index=DatetimeIndex(trade_time), columns=open/high/low/close/volume/amount/adj_factor
        无数据时返回空 DataFrame
    """
    try:
        stmt = (
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
        )
        if limit is not None:
            stmt = stmt.order_by(Bar15Min.trade_time.desc()).limit(limit)
        else:
            stmt = stmt.order_by(Bar15Min.trade_time)
        result = await session.execute(stmt)
        rows = result.all()
    except Exception as exc:
        logger.warning("查询 bars_15min 失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if not rows:
        return pd.DataFrame()

    # limit 模式下 DESC 查询，反转为升序
    if limit is not None:
        rows = rows[::-1]

    df = pd.DataFrame(rows, columns=["trade_time"] + _BAR_COLUMNS)
    # DB 读取的 timestamptz 返回 UTC 时区感知 datetime，需转为 naive 上海时间与 pytdx 一致
    _ts = pd.to_datetime(df["trade_time"])
    if getattr(_ts.dt, "tz", None) is not None:
        _ts = _ts.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    df["trade_time"] = _ts
    df = df.set_index("trade_time")
    for col in _BAR_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


async def _map_adj_factor_from_daily(
    session: AsyncSession | _SessionLike,
    instrument_id: uuid.UUID,
    raw_df: pd.DataFrame,
) -> list[float]:
    """从 DB 日线表映射 adj_factor 到分钟线记录。

    按分钟线 bar 的日期部分，从 bars_daily 表查询对应日期的 adj_factor。
    如果某日期在日线表中不存在，则 fallback 到 1.0。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        raw_df: pytdx 返回的 DataFrame，含 datetime 列

    Returns:
        adj_factor 列表，与 raw_df 行一一对应；日线表无对应日期时为 1.0
    """
    # 提取分钟线 bar 的日期部分
    bar_dates = pd.to_datetime(raw_df["datetime"]).dt.date
    unique_dates = bar_dates.unique().tolist()

    if not unique_dates:
        return [1.0] * len(raw_df)

    # 查询 bars_daily 表获取这些日期的 adj_factor
    result = await session.execute(
        select(BarDaily.trade_date, BarDaily.adj_factor)
        .where(BarDaily.instrument_id == instrument_id)
        .where(BarDaily.trade_date.in_(unique_dates))
    )
    rows = result.all()

    # 构建 date -> adj_factor 映射（跳过 adj_factor 为 None 的记录）
    factor_map: dict[date, float] = {}
    for trade_date, adj_factor in rows:
        if adj_factor is not None:
            factor_map[trade_date] = float(adj_factor)

    # 按分钟线 bar 日期查找 adj_factor，找不到则用 1.0
    return [factor_map.get(d, 1.0) for d in bar_dates]


async def _upsert_15min_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    raw_df: pd.DataFrame,
    symbol: str | None = None,
    adapter: PytdxAdapter | None = None,
) -> int:
    """将 pytdx 拉取的 15 分钟线数据 upsert 入库。

    adj_factor 从日线表 bars_daily 映射（按 bar 日期匹配），保证分钟线与日线 adj_factor 一致。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        raw_df: pytdx 返回的 DataFrame，含 datetime/open/high/low/close/volume/amount
        symbol: 股票代码，用于数据校验
        adapter: pytdx 适配器（保留兼容，不再用于 adj_factor 计算）

    Returns:
        写入记录数

    Raises:
        Exception: 写入失败时 re-raise（不吞没）
    """
    if raw_df.empty:
        return 0

    # 从日线表映射 adj_factor（保证分钟线与日线 adj_factor 一致）
    try:
        adj_factors = await _map_adj_factor_from_daily(session, instrument_id, raw_df)
    except Exception as exc:
        raise RuntimeError(
            f"从日线表映射 adj_factor 失败 instrument_id={instrument_id} "
            f"symbol={symbol} bar_count={len(raw_df)}: {exc}"
        ) from exc

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

    return await _batch_upsert_bars(
        session, Bar15Min, records,
        index_elements=["instrument_id", "trade_time"],
        label="bars_15min", instrument_id=instrument_id,
    )


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
    # DB 读取的 timestamptz 返回 UTC 时区感知 datetime，需转为 naive 上海时间与 pytdx 一致
    _ts = pd.to_datetime(df["trade_time"])
    if getattr(_ts.dt, "tz", None) is not None:
        _ts = _ts.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    df["trade_time"] = _ts
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

    adj_factor 从日线表 bars_daily 映射（按 bar 日期匹配），保证分钟线与日线 adj_factor 一致。

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        raw_df: pytdx 返回的 DataFrame，含 datetime/open/high/low/close/volume/amount
        symbol: 股票代码，用于数据校验
        adapter: pytdx 适配器（保留兼容，不再用于 adj_factor 计算）

    Returns:
        写入记录数

    Raises:
        Exception: 写入失败时 re-raise（不吞没）
    """
    if raw_df.empty:
        return 0

    # 从日线表映射 adj_factor（保证分钟线与日线 adj_factor 一致）
    try:
        adj_factors = await _map_adj_factor_from_daily(session, instrument_id, raw_df)
    except Exception as exc:
        raise RuntimeError(
            f"从日线表映射 adj_factor 失败 instrument_id={instrument_id} "
            f"symbol={symbol} bar_count={len(raw_df)}: {exc}"
        ) from exc

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

    return await _batch_upsert_bars(
        session, Bar60Min, records,
        index_elements=["instrument_id", "trade_time"],
        label="bars_60min", instrument_id=instrument_id,
    )


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


async def get_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    *,
    timeframe: str = "1d",
    start_date: date | None = None,
    end_date: date | None = None,
    adjustment: str | None = None,  # None=原始, "qfq"=前复权
    completed_only: bool = False,  # 只返回已完成的 Bar
    skip_upsert: bool = False,  # 仅 1m 有效，为 True 时不写入 DB
) -> BarsResult:
    """统一行情获取入口。

    封装 fetch_*_bars + apply_adj_factor_to_bars + 元数据收集，
    为所有调用方提供一致的行情数据。

    Args:
        session: 异步数据库会话
        instrument_id: 标的 ID
        timeframe: 周期 ("1d", "15m", "1m")
        start_date: 起始日期（含），None 使用默认值
        end_date: 结束日期（含），None 使用今天
        adjustment: 复权方式（None=不复权, "qfq"=前复权）
        completed_only: 是否只返回已完成的 Bar
            - 日线：排除当日 bar（盘中未收盘）
            - 分钟线：排除最后一根未完成 bar
        skip_upsert: 仅 1m 有效，为 True 时不写入 DB（用于实时监控等无需持久化场景）

    Returns:
        BarsResult 包含行情数据和元数据

    Raises:
        ValueError: timeframe 不在支持列表中
        Exception: fetch 或复权失败时 re-raise
    """
    if timeframe not in _TIMEFRAME_MAP:
        raise ValueError(
            f"不支持的 timeframe: {timeframe}，仅支持 {list(_TIMEFRAME_MAP.keys())}"
        )

    # 默认日期范围
    if end_date is None:
        end_date = date.today()
    if start_date is None:
        start_date = end_date - timedelta(days=5000)

    # [行情] - 根据 timeframe 调用对应的 fetch 函数
    is_intraday = timeframe != "1d"

    if timeframe == "1d":
        bars_df = await fetch_daily_bars(session, instrument_id, start_date, end_date)
    elif timeframe == "15m":
        start_time = datetime(start_date.year, start_date.month, start_date.day)
        end_time = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59)
        bars_df = await fetch_15min_bars(session, instrument_id, start_time, end_time)
    elif timeframe == "1m":
        start_time = datetime(start_date.year, start_date.month, start_date.day)
        end_time = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59)
        bars_df = await fetch_minute_bars(
            session, instrument_id, start_time, end_time,
            skip_upsert=skip_upsert,
        )
    else:
        raise ValueError(f"不支持的 timeframe: {timeframe}")

    # 判断数据来源
    source = "db" if not bars_df.empty else "empty"

    # [行情] - 前复权处理
    if adjustment == "qfq" and not bars_df.empty:
        try:
            adj_factor_df = await _get_adj_factor_df(session, instrument_id)
            if not adj_factor_df.empty:
                bars_df = apply_adj_factor_to_bars(bars_df, adj_factor_df, intraday=is_intraday)
        except Exception as exc:
            logger.warning("前复权处理失败 instrument_id=%s timeframe=%s: %s", instrument_id, timeframe, exc)
            raise

    # [行情] - completed_only 过滤
    if completed_only and not bars_df.empty:
        # 使用上海时区判断"今日"（A 股交易日边界），避免容器 UTC 时区导致盘中日线被误排除
        now = datetime.now(SHANGHAI_TZ)
        if timeframe == "1d":
            # 日线：排除当日 bar（盘中未收盘）
            today = now.date()
            bars_df = bars_df[bars_df.index.date < today]
        else:
            # 分钟线：排除最后一根 bar（可能未完成）
            if len(bars_df) > 1:
                bars_df = bars_df.iloc[:-1]

    # [行情] - 构建元数据
    first_bar_time: pd.Timestamp | None = None
    last_bar_time: pd.Timestamp | None = None
    completed_through: pd.Timestamp | None = None

    if not bars_df.empty:
        first_bar_time = pd.Timestamp(bars_df.index[0])
        last_bar_time = pd.Timestamp(bars_df.index[-1])
        # completed_through：最后一根 bar 的时间（completed_only 已过滤掉未完成的）
        completed_through = last_bar_time

    return BarsResult(
        bars=bars_df,
        source=source,
        timeframe=timeframe,
        adjustment=adjustment,
        first_bar_time=first_bar_time,
        last_bar_time=last_bar_time,
        completed_through=completed_through,
    )


# [行情] - 描述: period → (ORM 模型, 时间列, 是否日线类) 映射，供 get_recent_bars 使用
_PERIOD_MODEL_MAP: dict[
    str,
    tuple[type[BarDaily] | type[Bar15Min] | type[BarMinute], str, bool],
] = {
    "1d": (BarDaily, "trade_date", True),
    "15m": (Bar15Min, "trade_time", False),
    "1m": (BarMinute, "trade_time", False),
}


async def get_recent_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    period: str,
    limit: int,
    adjustment: str | None = None,
) -> pd.DataFrame:
    """按根数获取最近 N 根 bar（DB 优先查询，统一升序返回）。

    替代旧的自然日估算取数（如 _DAILY_FETCH_DAYS=370 → tail(250)），统一为
    "按根数 LIMIT N" 语义，避免"自然日 vs 交易日"换算误差。

    实现逻辑（advice.md v6 第4条：取数根数从基线读取）：
        1. 按 instrument_id 过滤，order_by(desc).limit(N) 查询最近 N 根
        2. 恢复升序返回（pd.DataFrame.sort_index），与 fetch_*_bars 返回顺序一致
        3. 前复权：adjustment="qfq" 时从 bars_daily 提取 adj_factor 应用

    与 fetch_*_bars 的差异：
        - fetch_*_bars 按日期范围查询（start_date/end_date）
        - get_recent_bars 按根数查询（LIMIT N），更适合"取最近 250 根日线"场景
        - 不自动从 pytdx 拉取（避免隐藏写入），DB 无数据时返回空 DataFrame

    Args:
        session: 异步会话
        instrument_id: 标的 UUID
        period: 周期 ("1d", "15m", "1m")
        limit: 返回最近 N 根（受控参数应从 indicator_contract 读取，如
            IC.NODE_CLUSTER_PRIMARY_BARS / IC.NODE_CLUSTER_LOW_BARS /
            IC.NODE_CLUSTER_MINUTE_BARS）
        adjustment: 复权方式（None=不复权, "qfq"=前复权）

    Returns:
        DataFrame: index 为 DatetimeIndex（升序），含 open/high/low/close/volume/
        amount/adj_factor 列。无数据时返回空 DataFrame。

    Raises:
        ValueError: period 不在支持列表中
        Exception: DB 查询或前复权失败时 re-raise（不吞没）
    """
    if period not in _PERIOD_MODEL_MAP:
        raise ValueError(
            f"不支持的 period: {period}，仅支持 {list(_PERIOD_MODEL_MAP.keys())}"
        )
    if limit <= 0:
        raise ValueError(f"limit 必须 > 0，当前 {limit}")

    model, time_col, is_daily = _PERIOD_MODEL_MAP[period]
    time_attr = getattr(model, time_col)

    # 1. DB 查询：order_by(desc).limit(N) 取最近 N 根
    try:
        result = await session.execute(
            select(
                time_attr,
                model.open,
                model.high,
                model.low,
                model.close,
                model.volume,
                model.amount,
                model.adj_factor,
            )
            .where(model.instrument_id == instrument_id)
            .order_by(time_attr.desc())
            .limit(limit)
        )
        rows = result.all()
    except Exception as exc:
        logger.warning(
            "get_recent_bars 查询失败 instrument_id=%s period=%s limit=%d: %s",
            instrument_id, period, limit, exc,
        )
        raise

    if not rows:
        return pd.DataFrame()

    # 2. 构建 DataFrame，恢复升序
    df = pd.DataFrame(rows, columns=[time_col] + _BAR_COLUMNS)
    _ts = pd.to_datetime(df[time_col])
    if not is_daily:
        # 分钟线：DB 读取的 timestamptz 返回 UTC 时区感知 datetime，转为 naive 上海时间
        if getattr(_ts.dt, "tz", None) is not None:
            _ts = _ts.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    df[time_col] = _ts
    df = df.set_index(time_col).sort_index()
    for col in _BAR_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 3. 前复权处理（adjustment="qfq"）
    if adjustment == "qfq" and not df.empty:
        try:
            adj_factor_df = await _get_adj_factor_df(session, instrument_id)
            if not adj_factor_df.empty:
                df = apply_adj_factor_to_bars(df, adj_factor_df, intraday=not is_daily)
        except Exception as exc:
            logger.warning(
                "get_recent_bars 前复权失败 instrument_id=%s period=%s: %s",
                instrument_id, period, exc,
            )
            raise

    return df


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
    assert params == ["session", "instrument_id", "start_time", "end_time", "adapter", "skip_upsert"], \
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

    # 6. 验证 upsert 函数存在（周线/月线不存储，已删除 _upsert_weekly/monthly_bars）
    for fn_name in ["_upsert_15min_bars", "_upsert_60min_bars"]:
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
    adj_factors_1 = _calculate_adj_factor("000001", raw_df_1, mock_adapter_1)
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
    adj_factors_2 = _calculate_adj_factor("000001", raw_df_2, mock_adapter_2)
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
    adj_factors_3 = _calculate_adj_factor("000001", raw_df_3, mock_adapter_3)
    expected_factor_3 = (164.0 / 15.0) / 15.0  # ≈ 0.7289
    assert abs(adj_factors_3[0] - expected_factor_3) < 1e-6, \
        f"混合: 事件日前 adj_factor 应为 {expected_factor_3}，实际 {adj_factors_3[0]}"
    for i in range(1, 3):
        assert abs(adj_factors_3[i] - 1.0) < 1e-6, \
            f"混合: 事件日及之后 bar[{i}] adj_factor 应为 1.0，实际 {adj_factors_3[i]}"
    print(f"混合: adj_factors={[round(f, 4) for f in adj_factors_3]} ✓")

    # 9. 验证 convert_kline_frequency（Task 12: 日线合并为周线/月线）
    print("\n--- Task 12: convert_kline_frequency 自测 ---")

    # 构造 2 周日线数据（2026-06-15 周一到 2026-06-26 周五，跳过周末）
    # 06-15(周一) ~ 06-19(周五): Week1, 06-22(周一) ~ 06-26(周五): Week2
    daily_data = {
        "open":       [10.0, 10.2, 10.4, 10.6, 10.8, 11.0, 11.3],
        "high":       [10.5, 10.6, 10.8, 11.0, 11.2, 11.5, 11.6],
        "low":        [9.8, 10.0, 10.2, 10.4, 10.6, 10.8, 11.0],
        "close":      [10.2, 10.4, 10.6, 10.8, 11.0, 11.3, 11.5],
        "volume":     [100000, 110000, 120000, 130000, 140000, 150000, 160000],
        "amount":     [1020000, 1144000, 1272000, 1404000, 1540000, 1695000, 1840000],
        "adj_factor": [0.98, 0.98, 0.98, 1.0, 1.0, 1.0, 1.0],
    }
    daily_dates = pd.to_datetime([
        "2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19",
        "2026-06-22", "2026-06-26",
    ])
    daily_df_test = pd.DataFrame(daily_data, index=daily_dates)
    daily_df_test.index.name = "trade_date"

    # 9.1 周线合并
    weekly_df_test = convert_kline_frequency(daily_df_test, "w")
    assert len(weekly_df_test) == 2, f"周线合并应有 2 条，实际 {len(weekly_df_test)}"
    # Week 1: 06-15 到 06-19
    w1 = weekly_df_test.loc[pd.Timestamp("2026-06-15")]
    assert w1["open"] == 10.0, f"Week1 open 应为 10.0，实际 {w1['open']}"
    assert w1["close"] == 11.0, f"Week1 close 应为 11.0，实际 {w1['close']}"
    assert w1["high"] == 11.2, f"Week1 high 应为 11.2，实际 {w1['high']}"
    assert w1["low"] == 9.8, f"Week1 low 应为 9.8，实际 {w1['low']}"
    assert w1["volume"] == 600000, f"Week1 volume 应为 600000，实际 {w1['volume']}"
    # adj_factor 取周期内最后一个交易日（06-19 的 1.0，非 06-15 的 0.98）
    assert w1["adj_factor"] == 1.0, f"Week1 adj_factor 应为 1.0（最后一个交易日），实际 {w1['adj_factor']}"
    print(f"周线 Week1: date=06-15 open={w1['open']} close={w1['close']} high={w1['high']} low={w1['low']} adj={w1['adj_factor']} ✓")

    # Week 2: 06-22, 06-26
    w2 = weekly_df_test.loc[pd.Timestamp("2026-06-22")]
    assert w2["open"] == 11.0, f"Week2 open 应为 11.0，实际 {w2['open']}"
    assert w2["close"] == 11.5, f"Week2 close 应为 11.5，实际 {w2['close']}"
    assert w2["adj_factor"] == 1.0, f"Week2 adj_factor 应为 1.0，实际 {w2['adj_factor']}"
    print(f"周线 Week2: date=06-22 open={w2['open']} close={w2['close']} adj={w2['adj_factor']} ✓")

    # 9.2 前对齐验证：周线 trade_date 应为周期内第一个交易日
    assert pd.Timestamp("2026-06-15") in weekly_df_test.index, "Week1 trade_date 应为 06-15（第一个交易日）"
    assert pd.Timestamp("2026-06-22") in weekly_df_test.index, "Week2 trade_date 应为 06-22（第一个交易日）"
    print("前对齐: 周线 trade_date = 周期内第一个交易日 ✓")

    # 9.3 月线合并
    monthly_df_test = convert_kline_frequency(daily_df_test, "m")
    assert len(monthly_df_test) == 1, f"月线合并应有 1 条（都在 6 月），实际 {len(monthly_df_test)}"
    m1 = monthly_df_test.iloc[0]
    assert m1["open"] == 10.0, f"月线 open 应为 10.0，实际 {m1['open']}"
    assert m1["close"] == 11.5, f"月线 close 应为 11.5，实际 {m1['close']}"
    assert m1["high"] == 11.6, f"月线 high 应为 11.6，实际 {m1['high']}"
    assert m1["low"] == 9.8, f"月线 low 应为 9.8，实际 {m1['low']}"
    assert m1["volume"] == 910000, f"月线 volume 应为 910000，实际 {m1['volume']}"
    assert m1["adj_factor"] == 1.0, f"月线 adj_factor 应为 1.0，实际 {m1['adj_factor']}"
    print(f"月线: open={m1['open']} close={m1['close']} high={m1['high']} low={m1['low']} adj={m1['adj_factor']} ✓")

    # 9.4 空数据
    empty_conv = convert_kline_frequency(pd.DataFrame(), "w")
    assert empty_conv.empty, "空输入应返回空"
    print("空数据 ✓")

    # 9.5 非法周期
    try:
        convert_kline_frequency(daily_df_test, "d")
        raise AssertionError("应抛出 ValueError")
    except ValueError as e:
        assert "不支持" in str(e), f"错误信息不匹配: {e}"
    print("非法周期 ValueError ✓")

    # 10. 验证 _map_adj_factor_from_daily（从日线表映射 adj_factor 到分钟线）
    print("\n--- _map_adj_factor_from_daily 自测 ---")

    class _MockResult:
        """Mock session.execute 返回结果。"""

        def __init__(self, rows: list) -> None:
            self._rows = rows

        def all(self) -> list:
            return self._rows

    class _MockSession:
        """Mock AsyncSession，返回预设的 bars_daily 行。"""

        def __init__(self, rows: list) -> None:
            self._rows = rows

        async def execute(self, stmt):  # noqa: ANN001
            return _MockResult(self._rows)

    # 10.1 正常映射 + fallback：分钟线日期匹配日线 adj_factor，无匹配日期 fallback 1.0
    mock_rows = [
        (date(2026, 6, 16), Decimal("1.5")),
        (date(2026, 6, 17), Decimal("1.0")),
    ]
    mock_session = _MockSession(mock_rows)
    test_df = pd.DataFrame({
        "datetime": pd.to_datetime([
            "2026-06-16 09:30", "2026-06-16 10:00", "2026-06-17 09:30", "2026-06-18 09:30",
        ]),
        "open": [10.0, 10.1, 10.2, 10.3],
        "high": [10.5, 10.6, 10.7, 10.8],
        "low": [9.8, 9.9, 10.0, 10.1],
        "close": [10.2, 10.3, 10.4, 10.5],
        "volume": [100000, 110000, 120000, 130000],
        "amount": [1020000, 1133000, 1248000, 1365000],
    })
    factors = asyncio.run(_map_adj_factor_from_daily(mock_session, uuid.uuid4(), test_df))
    # 06-16 两条 -> 1.5, 06-17 一条 -> 1.0, 06-18 日线表无 -> 1.0（fallback）
    assert factors == [1.5, 1.5, 1.0, 1.0], f"adj_factor 映射错误: {factors}"
    print(f"正常映射+fallback: {factors} ✓")

    # 10.2 边界：日线表无任何匹配（全 fallback 1.0）
    mock_session_empty = _MockSession([])
    factors_empty = asyncio.run(_map_adj_factor_from_daily(mock_session_empty, uuid.uuid4(), test_df))
    assert factors_empty == [1.0, 1.0, 1.0, 1.0], f"空日线表应全为 1.0: {factors_empty}"
    print(f"空日线表 fallback: {factors_empty} ✓")

    print("\n所有自测通过 ✓（未进行 DB/网络测试）")
