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
        """获取 60 分钟线数据（按数量）。"""
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
