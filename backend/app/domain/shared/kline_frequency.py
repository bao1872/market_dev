"""Pure daily-bar aggregation shared by market-data infrastructure adapters.

This module owns only the deterministic daily -> weekly/monthly calculation.  It
does not read, adjust, persist, or publish bars; callers remain responsible for
those boundaries.
"""

from __future__ import annotations

import pandas as pd


def convert_kline_frequency(daily_df: pd.DataFrame, to_f: str) -> pd.DataFrame:
    """Aggregate daily OHLCV bars into weekly or monthly bars.

    The contract is intentionally unchanged from the former repository helper:
    periods are right-closed, the output index is the first trade date, OHLCV
    uses first/max/min/last/sum, and ``adj_factor`` uses the last daily value.
    """
    if daily_df.empty:
        return pd.DataFrame()

    period_maps = {"w": "W", "m": "ME"}
    if to_f not in period_maps:
        raise ValueError(f"不支持的转换周期：{to_f}，仅支持 'w' 或 'm'")

    df = daily_df.copy()
    df["_trade_date"] = df.index
    period_df = df.resample(
        period_maps[to_f], label="left", closed="right"
    ).agg(
        {
            "_trade_date": "first",
            "open": "first",
            "close": "last",
            "high": "max",
            "low": "min",
            "volume": "sum",
            "amount": "sum",
            "adj_factor": "last",
        }
    )
    period_df = period_df.dropna(subset=["_trade_date"])
    period_df = period_df.set_index("_trade_date")
    period_df.index.name = "trade_date"
    return period_df
