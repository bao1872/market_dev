"""ATR 公共工具 - Pine RMA (Wilder smoothing) 实现。

单一事实源 (SSOT) for ATR 计算。所有需要 ATR 的新代码应 import 此模块，
禁止再复制 true_range / pine_rma / atr_pine 实现。

与 TradingView ta.atr() 等价：
- 首根 TR = high - low（无 prev_close）
- RMA 种子 = 前 length 个 TR 的 SMA（忽略首根 NaN）
- alpha = 1 / length

用法：
    import numpy as np
    from app.strategy_assets.algorithms.features.atr_utils import compute_atr

    atr = compute_atr(highs, lows, closes, length=14)
    # atr: np.ndarray，与输入等长，前 length-1 个为 NaN

模块自测：
    python -m app.strategy_assets.algorithms.features.atr_utils
"""
from __future__ import annotations

import numpy as np


def compute_true_range(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray
) -> np.ndarray:
    """计算 True Range (TR)。

    Pine 语义：tr = max(high-low, |high-close[1]|, |low-close[1]|)
    首根 bar 无 prev_close，TR = high - low。

    Args:
        highs: 最高价数组
        lows: 最低价数组
        closes: 收盘价数组

    Returns:
        np.ndarray: TR 数组，与输入等长，首根为 high-low
    """
    if len(highs) == 0:
        return np.array([], dtype=float)
    prev_closes = np.empty_like(closes)
    prev_closes[0] = np.nan
    prev_closes[1:] = closes[:-1]
    return np.maximum.reduce([
        highs - lows,
        np.abs(highs - prev_closes),
        np.abs(lows - prev_closes),
    ])


def compute_atr(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    length: int = 14,
) -> np.ndarray:
    """计算 ATR (Pine RMA / Wilder smoothing)。

    与 TradingView ta.atr(length) 完全等价。
    RMA 种子 = 前 length 个 TR 的 SMA（忽略首根 NaN）。
    alpha = 1 / length，递推 prev = alpha * tr + (1 - alpha) * prev。

    Args:
        highs: 最高价数组
        lows: 最低价数组
        closes: 收盘价数组
        length: RMA 周期，默认 14

    Returns:
        np.ndarray: ATR 数组，与输入等长，前 length-1 个为 NaN
    """
    n = len(highs)
    if n == 0:
        return np.array([], dtype=float)
    out = np.full(n, np.nan, dtype=float)
    if length <= 0 or n == 0:
        return out

    tr = compute_true_range(highs, lows, closes)
    # 首根 TR 存在但 prev_close=NaN 导致 max 中两项为 NaN
    # Pine 的 nanmax 语义：首根 TR = high - low
    tr[0] = highs[0] - lows[0]

    first = min(length, n)
    # 种子 = 前 length 个 TR 的 SMA（此时首根 TR 已修正为 high-low）
    init = float(np.mean(tr[:first]))
    out[first - 1] = init
    prev = init
    alpha = 1.0 / length
    for i in range(first, n):
        prev = alpha * tr[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


if __name__ == "__main__":
    # 模块自测：验证与 atr_pine 等价
    import pandas as pd

    from app.strategy_assets.algorithms.features.merged_dsa_atr_rope_bb_factors import (
        atr_pine,
    )

    rng = np.random.default_rng(42)
    n = 60
    closes = 100.0 + np.linspace(0, 5.0, n) + rng.normal(0, 1.5, n)
    intrabar = np.abs(rng.normal(0, 1.0, n)) + 0.5
    highs = closes + intrabar
    lows = closes - intrabar

    df = pd.DataFrame({"high": highs, "low": lows, "close": closes})
    expected = atr_pine(df, 14).to_numpy()
    actual = compute_atr(highs, lows, closes, 14)

    valid_mask = ~np.isnan(expected)
    assert np.allclose(actual[valid_mask], expected[valid_mask], atol=1e-10), (
        "compute_atr 与 atr_pine 不一致"
    )
    print(f"自测通过：{n} bars, length=14, NaN count={int(np.isnan(actual).sum())}")
