"""行情数据源抽象层（策略模式）。

参考 Chanlunpro exchange 包设计，抽象数据源接口，支持多数据源切换：
- PytdxExchange：从 pytdx 行情服务器拉取（在线场景）
- DBExchange：从 PostgreSQL 读取（离线场景或降级）

设计决策：
- 不采用 Chanlunpro 的 klines() 统一接口（参数太多，类型不安全），改为按周期分方法
- 不采用 Chanlunpro 的 ticks()/balance()/positions()/order() 等交易接口（仅做行情）
- 保留 get_xdxr_info 和 get_stock_list 用于复权和股票列表

用法：
    from app.core.exchange import get_exchange

    exchange = get_exchange("A")
    df = exchange.get_daily_bars("000001", date(2026, 1, 1), date(2026, 6, 1))
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import TYPE_CHECKING

import pandas as pd

from app.config import get_settings

if TYPE_CHECKING:
    pass

# K线周期映射（项目内部 frequency → pytdx category）
# 参考 chanlunpro exchange_tdx.py 的频率映射
FREQUENCY_MAP: dict[str, int] = {
    "1d": 8,     # 日线
    "15m": 5,    # 15分钟
    "1h": 4,     # 60分钟
    "1w": -1,    # 周线（从日线合成）
    "1mo": -2,   # 月线（从日线合成）
}


class Exchange(ABC):
    """行情数据源抽象基类（策略模式）。

    所有方法为同步签名（与 PytdxAdapter 一致），异步调用方通过 asyncio.to_thread 桥接。
    返回的 DataFrame 统一格式：columns=[datetime, open, high, low, close, volume, amount]
    """

    @abstractmethod
    def get_daily_bars(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """获取日线数据（按日期范围）。

        Args:
            symbol: 股票代码（如 '000001'）
            start: 起始日期
            end: 结束日期

        Returns:
            DataFrame: columns=[datetime, open, high, low, close, volume, amount]
            无数据时返回空 DataFrame
        """

    @abstractmethod
    def get_weekly_bars(self, symbol: str, count: int = 800) -> pd.DataFrame:
        """获取周线数据（按数量）。"""

    @abstractmethod
    def get_monthly_bars(self, symbol: str, count: int = 800) -> pd.DataFrame:
        """获取月线数据（按数量）。"""

    @abstractmethod
    def get_15min_bars(self, symbol: str, count: int = 800) -> pd.DataFrame:
        """获取 15 分钟线数据（按数量）。"""

    @abstractmethod
    def get_60min_bars(self, symbol: str, count: int = 800) -> pd.DataFrame:
        """获取 60 分钟线数据（按数量）。"""

    @abstractmethod
    def get_minute_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """获取 1 分钟线数据（按时间范围）。"""

    @abstractmethod
    def get_xdxr_info(self, symbol: str) -> pd.DataFrame:
        """获取除权除息数据。

        Returns:
            DataFrame: columns=[date, category, name, fenhong, peigujia, songzhuangu, peigu]
            无数据时返回空 DataFrame
        """

    @abstractmethod
    async def klines(
        self,
        symbol: str,
        frequency: str,
        start_date: date | None = None,
        end_date: date | None = None,
        count: int | None = None,
    ) -> pd.DataFrame | None:
        """统一行情读取接口（参考 chanlunpro Exchange.klines）

        Args:
            symbol: 股票代码（如 '688158'）
            frequency: K线周期（'1d', '15m', '1h', '1w', '1mo'）
            start_date: 起始日期
            end_date: 结束日期
            count: 返回 bar 数量（与 start_date/end_date 二选一）

        Returns:
            DataFrame with columns: [open, high, low, close, volume, amount, adj_factor]
            Index: DatetimeIndex (timezone-aware Asia/Shanghai)
            Returns None if no data available
        """

    @abstractmethod
    def get_stock_list(self, market: str | None = None) -> pd.DataFrame:
        """获取股票列表。

        Args:
            market: 市场标识（SH/SZ/BJ），None 表示全部

        Returns:
            DataFrame: columns=[code, name, market]
        """


# 全局缓存：避免重复创建数据源实例（参考 Chanlunpro g_exchange_obj）
_exchange_cache: dict[str, Exchange] = {}


def get_exchange(market: str = "A") -> Exchange:
    """工厂函数：根据配置返回数据源实例（单例缓存）。

    参考 Chanlunpro get_exchange 设计。

    Args:
        market: 市场标识（A=沪深A股，未来可扩展 HK/US）

    Returns:
        Exchange 实例

    Raises:
        ValueError: 未知数据源配置
    """
    if market in _exchange_cache:
        return _exchange_cache[market]

    settings = get_settings()
    source = settings.bars_data_source

    if source == "pytdx":
        from app.core.pytdx_adapter import PytdxAdapter

        _exchange_cache[market] = PytdxAdapter()
    elif source == "db":
        from app.core.exchange.db_exchange import DBExchange

        _exchange_cache[market] = DBExchange()
    else:
        raise ValueError(f"未知数据源: {source}，支持 pytdx / db")

    return _exchange_cache[market]


def clear_exchange_cache() -> None:
    """清空数据源缓存（供测试使用）。"""
    _exchange_cache.clear()
