"""K线周期聚合器（CHANGE-20260717-002）。

薄封装：从已复权日线合成周/月线。委托 bar_repository.convert_kline_frequency。

分层约束：
- MDAS 通过本模块聚合周月，不直接导入 bar_repository 私有行情函数
- 本模块仅做参数映射（"1w"/"1mo" → "w"/"m"），核心聚合逻辑复用现有 convert_kline_frequency
- 调用方保证传入的 daily_df 已完成复权（"日线完成复权后再聚合"）

How to Run:
    python -m app.services.kline_aggregator    # 自测：验证参数映射与空数据处理
"""

from __future__ import annotations

import logging

import pandas as pd

from app.repositories.bar_repository import convert_kline_frequency

logger = logging.getLogger("services.kline_aggregator")

# 周期映射：MDAS 对外周期 → convert_kline_frequency 内部 freq
_TARGET_FREQ: dict[str, str] = {"1w": "w", "1mo": "m"}


def aggregate(daily_df: pd.DataFrame, target: str) -> pd.DataFrame:
    """从已复权日线合成周/月线（"日线完成复权后再聚合"）。

    Args:
        daily_df: 已复权日线 DataFrame，index 为 DatetimeIndex（trade_date），
                  含 open/high/low/close/volume/amount/adj_factor 列
        target: 目标周期 "1w" | "1mo"

    Returns:
        合成后的周/月线 DataFrame；空输入原样返回

    Raises:
        KeyError: target 不在 {"1w", "1mo"}
    """
    if daily_df.empty:
        return daily_df
    if target not in _TARGET_FREQ:
        raise KeyError(f"aggregate 仅支持 1w/1mo, got {target!r}")
    freq = _TARGET_FREQ[target]
    return convert_kline_frequency(daily_df, freq)


if __name__ == "__main__":
    # 自测入口：验证参数映射与空数据处理（不连 DB）
    import inspect

    logging.basicConfig(level=logging.INFO)

    # 1. 函数签名校验
    sig = inspect.signature(aggregate)
    params = list(sig.parameters.keys())
    assert params == ["daily_df", "target"], f"aggregate 参数应为 [daily_df, target], got {params}"
    print("函数签名校验 ✓")

    # 2. 空数据
    empty = pd.DataFrame()
    assert aggregate(empty, "1w").empty
    assert aggregate(empty, "1mo").empty
    print("空数据原样返回 ✓")

    # 3. 参数映射（通过构造周线验证：5 个交易日 → 1 周根数变化）
    # 构造 5 个交易日（周一至周五），合成周线应为 1 根
    daily_df = pd.DataFrame({
        "open": [10.0, 10.1, 10.2, 10.3, 10.4],
        "high": [10.5, 10.6, 10.7, 10.8, 10.9],
        "low": [9.8, 9.9, 10.0, 10.1, 10.2],
        "close": [10.2, 10.3, 10.4, 10.5, 10.6],
        "volume": [1000.0, 1100.0, 1200.0, 1300.0, 1400.0],
        "amount": [10000.0, 11000.0, 12000.0, 13000.0, 14000.0],
        "adj_factor": [1.0, 1.0, 1.0, 1.0, 1.0],
    }, index=pd.to_datetime(["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19"]))
    daily_df.index.name = "trade_date"

    weekly = aggregate(daily_df, "1w")
    assert not weekly.empty, "周线不应为空"
    # 周线 close = 最后一天 close
    assert float(weekly.iloc[-1]["close"]) == 10.6, f"周线 close 应=10.6, got {weekly.iloc[-1]['close']}"
    print(f"周线聚合 ✓ (close={float(weekly.iloc[-1]['close'])})")

    # 4. 非法 target
    try:
        aggregate(daily_df, "5m")  # type: ignore[arg-type]
        raise AssertionError("应抛出 KeyError")
    except KeyError:
        print("非法 target 抛出 KeyError ✓")

    print("OK")
