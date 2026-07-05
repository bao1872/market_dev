"""ATR 公共工具单元测试 - Pine RMA (Wilder smoothing) 等价性验证。

验证维度：
1. compute_atr 输出与 merged_dsa_atr_rope_bb_factors.atr_pine 完全一致
2. 首根 TR = high - low（无 prev_close）
3. RMA 种子 = 前 length 个 TR 的 SMA
4. 常数价格 ATR = 0
5. 数据不足返回全 NaN
6. 空输入返回空数组
7. 返回类型为 np.ndarray

用法：
    cd backend && APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://... \
        pytest tests/test_atr_utils.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategy_assets.algorithms.features.atr_utils import compute_atr
from app.strategy_assets.algorithms.features.merged_dsa_atr_rope_bb_factors import (
    atr_pine,
)


def _build_ohlcv(n: int = 60, seed: int = 42) -> dict[str, np.ndarray]:
    """构造固定 OHLCV 样本（可重现）。"""
    rng = np.random.default_rng(seed)
    base = 100.0
    trend = np.linspace(0, 5.0, n)
    noise = rng.normal(0, 1.5, n)
    closes = base + trend + noise
    intrabar = np.abs(rng.normal(0, 1.0, n)) + 0.5
    highs = closes + intrabar
    lows = closes - intrabar
    return {"highs": highs, "lows": lows, "closes": closes}


# ===== 1. 等价性：与 atr_pine 完全一致 =====
def test_compute_atr_matches_pine_atr_pine() -> None:
    """compute_atr 输出必须与 atr_pine (Pine RMA) 完全一致。"""
    ohlcv = _build_ohlcv(n=60)
    length = 14

    # reference: atr_pine 接受 DataFrame
    df = pd.DataFrame({
        "high": ohlcv["highs"],
        "low": ohlcv["lows"],
        "close": ohlcv["closes"],
    })
    expected = atr_pine(df, length).to_numpy()

    # actual: compute_atr 接受 numpy 数组
    actual = compute_atr(
        highs=ohlcv["highs"],
        lows=ohlcv["lows"],
        closes=ohlcv["closes"],
        length=length,
    )

    assert actual.shape == expected.shape
    # NaN 位置必须一致
    assert np.array_equal(np.isnan(actual), np.isnan(expected))
    # 非 NaN 值必须几乎相等（RMA 浮点累积误差）
    valid_mask = ~np.isnan(expected)
    assert np.allclose(actual[valid_mask], expected[valid_mask], atol=1e-10)


def test_compute_atr_matches_different_length() -> None:
    """不同 length 参数下等价性。"""
    ohlcv = _build_ohlcv(n=100, seed=7)
    df = pd.DataFrame({
        "high": ohlcv["highs"],
        "low": ohlcv["lows"],
        "close": ohlcv["closes"],
    })

    for length in (5, 10, 20, 30):
        expected = atr_pine(df, length).to_numpy()
        actual = compute_atr(
            highs=ohlcv["highs"],
            lows=ohlcv["lows"],
            closes=ohlcv["closes"],
            length=length,
        )
        valid_mask = ~np.isnan(expected)
        assert np.allclose(actual[valid_mask], expected[valid_mask], atol=1e-10), (
            f"length={length}: compute_atr 与 atr_pine 不一致"
        )


# ===== 2. 首根 TR = high - low =====
def test_first_bar_true_range_uses_high_low() -> None:
    """首根 bar 无 prev_close，TR = high - low。

    Pine 语义：tr = max(high-low, high-close[1], low-close[1])，首根 close[1]=NaN。
    """
    highs = np.array([105.0, 110.0, 108.0], dtype=float)
    lows = np.array([95.0, 100.0, 98.0], dtype=float)
    closes = np.array([100.0, 105.0, 103.0], dtype=float)

    # length=1 时 RMA=SMA(1)=当前值，ATR=TR
    atr = compute_atr(highs=highs, lows=lows, closes=closes, length=1)
    expected_first_tr = highs[0] - lows[0]  # 10.0
    assert abs(atr[0] - expected_first_tr) < 1e-10, (
        f"首根 TR 应等于 high-low={expected_first_tr}，实际 {atr[0]}"
    )


# ===== 3. RMA 种子 = 前 length 个 TR 的 SMA =====
def test_rma_seed_is_sma_of_first_length_true_ranges() -> None:
    """Pine RMA 在第 length 个 bar 用前 length 个 TR 的 SMA 作为种子。

    注意：pandas max(axis=1) 跳过 NaN，所以首根 TR = high - low（有效值）。
    种子 = mean(TR[0:length])，包含首根 high-low。
    """
    ohlcv = _build_ohlcv(n=30, seed=99)
    length = 14

    # 手算前 length 个 TR 的 SMA（首根 TR = high-low，与 pandas max 一致）
    highs, lows, closes = ohlcv["highs"], ohlcv["lows"], ohlcv["closes"]
    tr = np.empty(length, dtype=float)
    tr[0] = highs[0] - lows[0]  # 首根 TR = high - low
    for i in range(1, length):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    expected_seed = float(np.mean(tr))

    atr = compute_atr(highs=highs, lows=lows, closes=closes, length=length)
    # 第 length-1 个位置（0-indexed）是种子位置
    assert abs(atr[length - 1] - expected_seed) < 1e-10, (
        f"RMA 种子应等于前 {length} 个 TR 的 SMA={expected_seed}，"
        f"实际 {atr[length - 1]}"
    )


# ===== 4. 常数价格 ATR = 0 =====
def test_constant_prices_atr_is_zero() -> None:
    """所有 OHLC 相同时 TR=0，ATR=0。"""
    n = 30
    prices = np.full(n, 100.0)
    atr = compute_atr(highs=prices, lows=prices, closes=prices, length=14)
    # 第 length-1 之后 ATR 应为 0
    assert np.allclose(atr[13:], 0.0, atol=1e-10), (
        f"常数价格 ATR 应为 0，实际 {atr[13:]}"
    )


# ===== 5. 数据不足时部分计算（与 atr_pine 一致）=====
def test_insufficient_data_matches_pine_partial() -> None:
    """数据少于 length 时与 atr_pine 行为一致：返回部分值，非全 NaN。

    atr_pine 使用 nanmean(arr[:first]) 作为种子，first=min(length,n)，
    所以 n=2 < length=14 时仍会在 index 1 处返回一个值。
    """
    highs = np.array([105.0, 110.0], dtype=float)
    lows = np.array([95.0, 100.0], dtype=float)
    closes = np.array([100.0, 105.0], dtype=float)

    df = pd.DataFrame({"high": highs, "low": lows, "close": closes})
    expected = atr_pine(df, 14).to_numpy()

    atr = compute_atr(highs=highs, lows=lows, closes=closes, length=14)
    assert atr.shape == (2,)
    assert np.array_equal(np.isnan(atr), np.isnan(expected))
    valid_mask = ~np.isnan(expected)
    assert np.allclose(atr[valid_mask], expected[valid_mask], atol=1e-10), (
        f"数据不足时应与 atr_pine 一致，expected={expected}, actual={atr}"
    )


# ===== 6. 空输入返回空数组 =====
def test_empty_input_returns_empty_array() -> None:
    """空输入返回空数组，不抛异常。"""
    empty = np.array([], dtype=float)
    atr = compute_atr(highs=empty, lows=empty, closes=empty, length=14)
    assert atr.shape == (0,)


# ===== 7. 返回类型为 np.ndarray =====
def test_returns_numpy_array() -> None:
    """返回类型必须是 np.ndarray。"""
    ohlcv = _build_ohlcv(n=30)
    atr = compute_atr(
        highs=ohlcv["highs"], lows=ohlcv["lows"], closes=ohlcv["closes"], length=14
    )
    assert isinstance(atr, np.ndarray), f"返回类型应为 np.ndarray，实际 {type(atr)}"


# ===== 8. 长度一致性 =====
def test_output_length_matches_input() -> None:
    """输出长度必须等于输入长度。"""
    ohlcv = _build_ohlcv(n=50)
    atr = compute_atr(
        highs=ohlcv["highs"], lows=ohlcv["lows"], closes=ohlcv["closes"], length=14
    )
    assert len(atr) == len(ohlcv["highs"])


# ===== 9. 模块自测入口 =====
if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
