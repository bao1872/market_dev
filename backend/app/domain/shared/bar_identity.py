"""Canonical, side-effect-free identity helpers for rendered bar sets."""
from __future__ import annotations

import hashlib

import pandas as pd


def compute_source_bar_times(df: pd.DataFrame, timeframe: str = "1d") -> list[str]:
    """Serialize bar timestamps using the public bar API time convention."""
    fmt = "%Y-%m-%dT%H:%M:%S" if timeframe in ("15m", "1h") else "%Y-%m-%d"
    return [idx.strftime(fmt) for idx in df.index]


def compute_source_bar_hash(df: pd.DataFrame, timeframe: str = "1d") -> str:
    """Return the frozen 16-character SHA256 identity of an OHLCV bar set."""
    if df.empty:
        return ""
    fmt = "%Y-%m-%dT%H:%M:%S" if timeframe in ("15m", "1h") else "%Y-%m-%d"
    # ``iterrows`` materializes one Series per row.  A homogeneous NumPy view
    # preserves the same scalar string representation for the numeric bar
    # contract while avoiding that allocation cost.
    values = df[["open", "high", "low", "close", "volume", "amount"]].to_numpy()
    parts = [
        f"{idx.strftime(fmt)}|{row[0]}|{row[1]}|{row[2]}|"
        f"{row[3]}|{row[4]}|{row[5]}"
        for idx, row in zip(df.index, values, strict=True)
    ]
    joined = "\n".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


__all__ = ["compute_source_bar_hash", "compute_source_bar_times"]
