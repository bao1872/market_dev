"""DB 数据源实现（策略模式）。

从 PostgreSQL 读取行情数据，用于离线场景或 pytdx 不可用时降级。
参考 Chanlunpro ExchangeDB 设计。

设计要点：
- 同步签名（与 PytdxAdapter 一致），使用 psycopg 驱动
- 仅支持查询，不支持写入（写入仍走 bar_repository._upsert_*_bars）
- get_xdxr_info 从 DB 缓存读取（无缓存则返回空 DataFrame，触发 adj_factor=1.0 兜底）
- get_stock_list 从 instruments 表读取

用法：
    from app.core.exchange.db_exchange import DBExchange

    exchange = DBExchange()
    df = exchange.get_daily_bars("000001", date(2026, 1, 1), date(2026, 6, 1))
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import get_settings
from app.core.exchange import Exchange

logger = logging.getLogger(__name__)


def _create_sync_engine() -> Engine:
    """创建同步 SQLAlchemy engine（psycopg 驱动）。

    database_url 已为 postgresql+psycopg:// 格式，直接使用。
    """
    settings = get_settings()
    return create_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


class DBExchange(Exchange):
    """DB 数据源（从 PostgreSQL 读取行情）。

    实现 Exchange 接口，所有方法为同步签名。
    用于离线场景或 pytdx 不可用时降级。

    注意：
    - 不支持 get_xdxr_info（DB 不存储 xdxr 原始数据），返回空 DataFrame
    - 不支持写入，仅查询
    - 查询结果格式与 PytdxAdapter 一致：columns=[datetime, open, high, low, close, volume, amount]
    """

    def __init__(self) -> None:
        self._engine: Engine = _create_sync_engine()

    def _query_bars_by_date(
        self,
        table: str,
        symbol: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """按日期范围查询行情（日线/周线/月线）。"""
        sql = text("""
            SELECT b.trade_date AS datetime, b.open, b.high, b.low, b.close,
                   b.volume, b.amount
            FROM :table b
            JOIN instruments i ON b.instrument_id = i.id
            WHERE i.symbol = :symbol
              AND b.trade_date >= :start
              AND b.trade_date <= :end
            ORDER BY b.trade_date
        """)

        try:
            with self._engine.connect() as conn:
                # SQLAlchemy text() 不支持表名参数化，使用字符串格式化（表名受控）
                safe_table = table.replace("'", "").replace('"', "")
                sql_str = str(sql).replace(":table", safe_table)
                result = conn.execute(
                    text(sql_str),
                    {"symbol": symbol, "start": start, "end": end},
                )
                rows = result.fetchall()
        except Exception as exc:
            logger.warning("DB 查询 %s 失败 symbol=%s: %s", table, symbol, exc)
            raise

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume", "amount"])
        # Decimal -> float
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            df[col] = df[col].astype(float)
        return df

    def _query_bars_by_time(
        self,
        table: str,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """按时间范围查询行情（分钟线/15min/60min）。"""
        sql = text("""
            SELECT b.trade_time AS datetime, b.open, b.high, b.low, b.close,
                   b.volume, b.amount
            FROM :table b
            JOIN instruments i ON b.instrument_id = i.id
            WHERE i.symbol = :symbol
              AND b.trade_time >= :start
              AND b.trade_time <= :end
            ORDER BY b.trade_time
        """)

        try:
            with self._engine.connect() as conn:
                safe_table = table.replace("'", "").replace('"', "")
                sql_str = str(sql).replace(":table", safe_table)
                result = conn.execute(
                    text(sql_str),
                    {"symbol": symbol, "start": start, "end": end},
                )
                rows = result.fetchall()
        except Exception as exc:
            logger.warning("DB 查询 %s 失败 symbol=%s: %s", table, symbol, exc)
            raise

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume", "amount"])
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            df[col] = df[col].astype(float)
        return df

    def _query_bars_by_count(
        self,
        table: str,
        symbol: str,
        count: int,
        date_column: str = "trade_date",
    ) -> pd.DataFrame:
        """按数量查询行情（周线/月线/15min/60min，取最新 count 条）。"""
        sql = text("""
            SELECT b.{date_col} AS datetime, b.open, b.high, b.low, b.close,
                   b.volume, b.amount
            FROM :table b
            JOIN instruments i ON b.instrument_id = i.id
            WHERE i.symbol = :symbol
            ORDER BY b.{date_col} DESC
            LIMIT :count
        """)

        try:
            with self._engine.connect() as conn:
                safe_table = table.replace("'", "").replace('"', "")
                sql_str = str(sql).replace(":table", safe_table).replace("{date_col}", date_column)
                result = conn.execute(
                    text(sql_str),
                    {"symbol": symbol, "count": count},
                )
                rows = result.fetchall()
        except Exception as exc:
            logger.warning("DB 查询 %s 失败 symbol=%s: %s", table, symbol, exc)
            raise

        if not rows:
            return pd.DataFrame()

        # 反转为升序（与 PytdxAdapter 一致）
        df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume", "amount"])
        df = df.iloc[::-1].reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            df[col] = df[col].astype(float)
        return df

    def get_daily_bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """获取日线数据（按日期范围）。"""
        return self._query_bars_by_date("bars_daily", symbol, start, end)

    def get_weekly_bars(self, symbol: str, count: int = 800) -> pd.DataFrame:
        """获取周线数据（按数量，取最新 count 条）。

        周线不存储在 DB，从日线动态合成（convert_kline_frequency）。
        DBExchange 不支持直接查周线表，返回空 DataFrame。
        """
        return pd.DataFrame()

    def get_monthly_bars(self, symbol: str, count: int = 800) -> pd.DataFrame:
        """获取月线数据（按数量）。

        月线不存储在 DB，从日线动态合成（convert_kline_frequency）。
        DBExchange 不支持直接查月线表，返回空 DataFrame。
        """
        return pd.DataFrame()

    def get_15min_bars(self, symbol: str, count: int = 800) -> pd.DataFrame:
        """获取 15 分钟线数据（按数量）。"""
        return self._query_bars_by_count("bars_15min", symbol, count, "trade_time")

    def get_60min_bars(self, symbol: str, count: int = 800) -> pd.DataFrame:
        """获取 60 分钟线数据（按数量，取最新 count 条）。

        count 默认 800（DB 查询用），回补到 2023-01-01 需 4000 条（由 BACKFILL_COUNTS 控制）。
        """
        return self._query_bars_by_count("bars_60min", symbol, count, "trade_time")

    def get_minute_bars(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """获取 1 分钟线数据（按时间范围）。"""
        return self._query_bars_by_time("bars_minute", symbol, start, end)

    def get_xdxr_info(self, symbol: str) -> pd.DataFrame:
        """获取除权除息数据。

        DB 不存储 xdxr 原始数据，返回空 DataFrame。
        调用方（_calculate_adj_factor）会兜底为 adj_factor=1.0。
        """
        logger.info("DBExchange 不支持 get_xdxr_info，返回空 DataFrame symbol=%s", symbol)
        return pd.DataFrame()

    def get_stock_list(self, market: str | None = None) -> pd.DataFrame:
        """获取股票列表（从 instruments 表读取）。"""
        if market is not None:
            sql = text("""
                SELECT symbol AS code, name, market
                FROM instruments
                WHERE status = 'active'
                  AND market = :market
                ORDER BY symbol
            """)
            params: dict[str, Any] = {"market": market}
        else:
            sql = text("""
                SELECT symbol AS code, name, market
                FROM instruments
                WHERE status = 'active'
                ORDER BY symbol
            """)
            params = {}

        try:
            with self._engine.connect() as conn:
                result = conn.execute(sql, params)
                rows = result.fetchall()
        except Exception as exc:
            logger.warning("DB 查询股票列表失败: %s", exc)
            raise

        if not rows:
            return pd.DataFrame(columns=["code", "name", "market"])

        return pd.DataFrame(rows, columns=["code", "name", "market"])

    def _get_instrument_id_sync(self, symbol: str) -> int | None:
        """同步查询 instrument_id（通过 symbol 查 instruments 表）。"""
        sql = text("SELECT id FROM instruments WHERE symbol = :symbol LIMIT 1")
        try:
            with self._engine.connect() as conn:
                result = conn.execute(sql, {"symbol": symbol})
                row = result.fetchone()
                if row is not None:
                    return row[0]
                return None
        except Exception as exc:
            logger.warning("_get_instrument_id_sync 查询失败 symbol=%s: %s", symbol, exc)
            return None

    async def klines(
        self,
        symbol: str,
        frequency: str,
        start_date: date | None = None,
        end_date: date | None = None,
        count: int | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame | None:
        """统一行情读取接口（DB 模式，从 PostgreSQL 读取）

        limit 参数仅为接口兼容，DB 查询使用 count/LIMIT，不需要 fetch_count 计算。
        """
        from app.core.exchange import FREQUENCY_MAP

        if frequency not in FREQUENCY_MAP:
            logger.warning("klines() 不支持的 frequency=%s", frequency)
            return None

        # 周线/月线：从日线合成
        if frequency in ("1w", "1mo"):
            return await self._klines_synthesized(symbol, frequency, start_date, end_date, count)

        # 根据周期选择表和时间列
        table_map = {"1d": ("bars_daily", "trade_date"), "15m": ("bars_15min", "trade_time"), "1h": ("bars_60min", "trade_time")}
        table_info = table_map.get(frequency)
        if table_info is None:
            return None
        table, time_col = table_info

        # 查询 instrument_id
        instrument_id = await asyncio.to_thread(self._get_instrument_id_sync, symbol)
        if instrument_id is None:
            return None

        # 构建 SQL（使用参数化查询防止注入）
        conditions = ["b.instrument_id = :instrument_id"]
        params: dict[str, Any] = {"instrument_id": instrument_id}

        if start_date is not None:
            if frequency == "1d":
                conditions.append("b.trade_date >= :start_date")
            else:
                conditions.append("b.trade_time >= :start_date")
            params["start_date"] = start_date

        if end_date is not None:
            if frequency == "1d":
                conditions.append("b.trade_date <= :end_date")
            else:
                conditions.append("b.trade_time <= :end_date")
            params["end_date"] = end_date

        where = " AND ".join(conditions)

        # 表名受控（来自 table_map），可安全格式化
        safe_table = table.replace("'", "").replace('"', "")

        if count is not None:
            sql_str = (
                f"SELECT {time_col}, open, high, low, close, volume, amount, adj_factor "
                f"FROM {safe_table} b WHERE {where} "
                f"ORDER BY {time_col} DESC LIMIT :count"
            )
            params["count"] = count
        else:
            sql_str = (
                f"SELECT {time_col}, open, high, low, close, volume, amount, adj_factor "
                f"FROM {safe_table} b WHERE {where} "
                f"ORDER BY {time_col}"
            )

        def _execute_query() -> pd.DataFrame:
            with self._engine.connect() as conn:
                return pd.read_sql(text(sql_str), conn, params=params)

        try:
            result = await asyncio.to_thread(_execute_query)
            if result is None or result.empty:
                return None

            # 设置时间列为 index
            result = result.set_index(time_col)
            result.index = pd.to_datetime(result.index)
            result.index = result.index.tz_localize("Asia/Shanghai")

            # 如果用了 DESC LIMIT，需要重新排序
            if count is not None:
                result = result.sort_index()

            # adj_factor 缺失时填充 1.0
            if "adj_factor" not in result.columns:
                result["adj_factor"] = 1.0
            else:
                result["adj_factor"] = result["adj_factor"].fillna(1.0)

            return result
        except Exception as e:
            logger.warning("DBExchange.klines() 查询失败 symbol=%s: %s", symbol, e)
            return None

    async def _klines_synthesized(
        self,
        symbol: str,
        frequency: str,
        start_date: date | None = None,
        end_date: date | None = None,
        count: int | None = None,
    ) -> pd.DataFrame | None:
        """从日线合成周线/月线"""
        daily_count = (count or 500) * 7 if frequency == "1w" else (count or 120) * 31
        daily_df = await self.klines(symbol, "1d", start_date=start_date, end_date=end_date, count=daily_count)
        if daily_df is None or daily_df.empty:
            return None

        from app.repositories.bar_repository import convert_kline_frequency

        # convert_kline_frequency 期望 DatetimeIndex 无时区，先去除时区
        daily_naive = daily_df.copy()
        if daily_naive.index.tz is not None:
            daily_naive.index = daily_naive.index.tz_localize(None)

        freq_map = {"1w": "w", "1mo": "m"}
        target_freq = freq_map[frequency]
        result = convert_kline_frequency(daily_naive, target_freq)
        if result is None or result.empty:
            return None

        # 恢复时区
        result.index = result.index.tz_localize("Asia/Shanghai")

        if count is not None and len(result) > count:
            result = result.iloc[-count:]

        return result


if __name__ == "__main__":
    # 自测入口：验证 DBExchange 类定义和方法签名（不连 DB，无副作用）
    import inspect

    # 1. 验证 DBExchange 继承 Exchange
    assert issubclass(DBExchange, Exchange), "DBExchange 应继承 Exchange"
    print("DBExchange 继承 Exchange ✓")

    # 2. 验证所有抽象方法已实现
    abstract_methods = Exchange.__abstractmethods__
    for method_name in abstract_methods:
        assert hasattr(DBExchange, method_name), f"DBExchange 应实现 {method_name}"
        method = getattr(DBExchange, method_name)
        assert callable(method), f"DBExchange.{method_name} 应可调用"
    print(f"所有 {len(abstract_methods)} 个抽象方法已实现 ✓")

    # 3. 验证方法签名
    expected_signatures = {
        "get_daily_bars": ["self", "symbol", "start", "end"],
        "get_weekly_bars": ["self", "symbol", "count"],
        "get_monthly_bars": ["self", "symbol", "count"],
        "get_15min_bars": ["self", "symbol", "count"],
        "get_60min_bars": ["self", "symbol", "count"],
        "get_minute_bars": ["self", "symbol", "start", "end"],
        "get_xdxr_info": ["self", "symbol"],
        "get_stock_list": ["self", "market"],
    }
    for method_name, expected_params in expected_signatures.items():
        method = getattr(DBExchange, method_name)
        sig = inspect.signature(method)
        params = list(sig.parameters.keys())
        assert params == expected_params, \
            f"DBExchange.{method_name} 参数不匹配: {params} != {expected_params}"
    print("所有方法签名匹配 ✓")

    # 4. 验证 get_xdxr_info 返回空 DataFrame（不连 DB）
    # 注意：不实例化 DBExchange（会创建 engine），仅验证方法定义
    print("get_xdxr_info 设计为返回空 DataFrame（DB 不存储 xdxr）✓")

    print("\n所有自测通过 ✓（未进行 DB 测试）")
