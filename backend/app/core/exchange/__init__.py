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

from app.config import get_settings
from app.core.exchange.contracts import FREQUENCY_MAP as FREQUENCY_MAP
from app.core.exchange.contracts import Exchange as Exchange

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
