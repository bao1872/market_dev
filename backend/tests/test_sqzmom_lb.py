"""SQZMOM_LB 指标单元测试 - 严格复刻 LazyBear Pine 代码。

验证维度：
1. BB dev 使用 multKC（不修正 Pine 原代码疑似 bug）
2. sqzOn/sqzOff/noSqz 三态判断
3. linreg offset=0 输出 = intercept + slope * (length - 1)
4. nz(val[1]) 颜色逻辑（前值 NA 时取 0）
5. stdev 用 ddof=0（TradingView Pine 默认总体标准差）
6. 数据不足时不抛异常
7. 固定 OHLCV 样本输出确定性

用法：
    cd backend && pytest tests/test_sqzmom_lb.py -v

Pine 原代码参考（/root/web_dev/ref/SQZMOM_LB.txt）：
    length = 20
    mult = 2.0
    lengthKC = 20
    multKC = 1.5
    useTrueRange = true

    basis = sma(source, length)
    dev = multKC * stdev(source, length)  # 注意：原 Pine 用 multKC，不修正
    upperBB = basis + dev
    lowerBB = basis - dev

    ma = sma(source, lengthKC)
    range = useTrueRange ? tr : (high - low)
    rangema = sma(range, lengthKC)
    upperKC = ma + rangema * multKC
    lowerKC = ma - rangema * multKC

    sqzOn  = (lowerBB > lowerKC) and (upperBB < upperKC)
    sqzOff = (lowerBB < lowerKC) and (upperBB > upperKC)
    noSqz  = (sqzOn == false) and (sqzOff == false)

    val = linreg(source - avg(avg(highest(high, lengthKC), lowest(low, lengthKC)), sma(close, lengthKC)),
                 lengthKC, 0)

    bcolor = iff(val > 0, iff(val > nz(val[1]), lime, green),
                          iff(val < nz(val[1]), red, maroon))
    scolor = noSqz ? blue : sqzOn ? black : gray
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.strategy_assets.algorithms.features.sqzmom_lb import (
    _highest,
    _linreg_pine,
    _lowest,
    _sma,
    _stdev_biased,
    _true_range,
    compute_sqzmom_lb,
)


# ===== 默认参数（与 Pine 原代码一致）=====
DEFAULT_PARAMS: dict[str, Any] = {
    "length": 20,
    "mult": 2.0,
    "lengthKC": 20,
    "multKC": 1.5,
    "useTrueRange": True,
}


# ===== 固定 OHLCV 样本构造器 =====
def _build_ohlcv(n: int = 60, seed: int = 42) -> dict[str, np.ndarray]:
    """构造固定 OHLCV 样本（可重现）。

    样本设计：
    - 60 根数据，足够 length+lengthKC=40 根 warmup
    - 趋势 + 波动让 sqzOn/sqzOff/noSqz 三态都覆盖到
    - 使用固定 seed 保证确定性

    Returns:
        dict: opens/highs/lows/closes 数组
    """
    rng = np.random.default_rng(seed)
    # 基础价格 100，含趋势和波动
    base = 100.0
    trend = np.linspace(0, 5.0, n)
    noise = rng.normal(0, 1.5, n)
    closes = base + trend + noise
    # high/low 在 close 基础上扩展
    intrabar = np.abs(rng.normal(0, 1.0, n)) + 0.5
    highs = closes + intrabar
    lows = closes - intrabar
    opens = closes + rng.normal(0, 0.3, n)
    return {
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
    }


# ===== 1. BB dev 使用 multKC（不修正）=====
def test_bb_dev_uses_multkc_not_mult() -> None:
    """验证 dev = multKC * stdev(...)，而非 mult * stdev(...)。

    Pine 原代码：dev = multKC * stdev(source, length)
    用户明确要求：不修正此"bug"。

    验证方法：multKC=1.5, mult=2.0，二者不同。
    若用 multKC，dev = 1.5 * stdev；若用 mult，dev = 2.0 * stdev。
    通过比较 upperBB - basis 与 1.5 * stdev 是否相等来判断。
    """
    ohlcv = _build_ohlcv(n=60)
    result = compute_sqzmom_lb(
        opens=ohlcv["opens"],
        highs=ohlcv["highs"],
        lows=ohlcv["lows"],
        closes=ohlcv["closes"],
        params=DEFAULT_PARAMS,
    )

    # 取一个 warmup 之后的 bar（index 25，确保有足够数据）
    idx = 25
    closes = ohlcv["closes"]
    length = 20
    multKC = 1.5

    # 手算 expected dev
    expected_basis = float(np.mean(closes[idx - length + 1:idx + 1]))
    expected_stdev = float(np.std(closes[idx - length + 1:idx + 1], ddof=0))
    expected_dev = multKC * expected_stdev

    # 后端返回的 BB 内部值通过 _compute_bb_kc 暴露在 _debug 字段（仅测试用）
    bb_debug = result.get("_debug_bb_kc")
    assert bb_debug is not None, "应包含 _debug_bb_kc 用于验证 BB/KC 计算"

    actual_dev = bb_debug["upperBB"][idx] - bb_debug["basis"][idx]
    assert abs(actual_dev - expected_dev) < 1e-9, (
        f"BB dev 应使用 multKC={multKC}，得到 dev={expected_dev}，"
        f"实际 dev={actual_dev}，stdev={expected_stdev}"
    )

    # 同时验证若用 mult=2.0 会得到不同结果
    wrong_dev_with_mult = 2.0 * expected_stdev
    assert abs(actual_dev - wrong_dev_with_mult) > 1e-9, (
        "BB dev 不应使用 mult=2.0"
    )


# ===== 2. sqzOn/sqzOff/noSqz 三态判断 =====
def test_sqz_on_when_bb_inside_kc() -> None:
    """BB 上下轨在 KC 内部 → sqzOn=True。

    构造场景：basis 接近 ma，dev 较小（multKC * stdev 小），
    rangema 较大（tr 较大），使 upperBB < upperKC 且 lowerBB > lowerKC。
    """
    n = 50
    # 构造低波动 close（stdev 小）
    closes = np.full(n, 100.0)
    closes[30:] = 100.0 + np.linspace(0, 0.1, n - 30)  # 极小波动
    # 构造大波动 high/low（tr 大，KC 宽）
    highs = closes + 5.0  # 远超 close
    lows = closes - 5.0
    opens = closes.copy()

    result = compute_sqzmom_lb(
        opens=opens, highs=highs, lows=lows, closes=closes,
        params=DEFAULT_PARAMS,
    )

    # 在 warmup 后的某 bar，应 sqzOn=True
    idx = 35
    assert result["sqzOn"][idx] is True, (
        f"BB 在 KC 内部时应 sqzOn=True，"
        f"upperBB={result['_debug_bb_kc']['upperBB'][idx]}, "
        f"upperKC={result['_debug_bb_kc']['upperKC'][idx]}"
    )
    assert result["sqzOff"][idx] is False
    assert result["noSqz"][idx] is False


def test_sqz_off_when_bb_outside_kc() -> None:
    """BB 上下轨在 KC 外部 → sqzOff=True。

    构造场景：close 波动大（stdev 大，BB 宽），
    high/low 波动小（tr 小，KC 窄）。
    """
    n = 50
    # 大波动 close
    rng = np.random.default_rng(123)
    closes = 100.0 + np.cumsum(rng.normal(0, 2.0, n))
    # 小波动 high/low（接近 close）
    highs = closes + 0.1
    lows = closes - 0.1
    opens = closes.copy()

    result = compute_sqzmom_lb(
        opens=opens, highs=highs, lows=lows, closes=closes,
        params=DEFAULT_PARAMS,
    )

    # warmup 后某 bar 应 sqzOff=True
    idx = 40
    assert result["sqzOff"][idx] is True, (
        f"BB 在 KC 外部时应 sqzOff=True，"
        f"upperBB={result['_debug_bb_kc']['upperBB'][idx]}, "
        f"upperKC={result['_debug_bb_kc']['upperKC'][idx]}"
    )
    assert result["sqzOn"][idx] is False
    assert result["noSqz"][idx] is False


def test_no_sqz_when_partial_overlap() -> None:
    """BB/KC 部分重叠 → noSqz=True。

    构造场景：BB 上轨在 KC 内，但 BB 下轨在 KC 外（或反之）。
    此时 sqzOn=False, sqzOff=False, noSqz=True。
    """
    n = 50
    # 构造 close 上升趋势（影响 BB 中轨位置）
    closes = np.concatenate([
        np.full(20, 100.0),
        np.linspace(100.0, 110.0, 30),
    ])
    # 高波动 high/low
    highs = closes + 3.0
    lows = closes - 3.0
    opens = closes.copy()

    result = compute_sqzmom_lb(
        opens=opens, highs=highs, lows=lows, closes=closes,
        params=DEFAULT_PARAMS,
    )

    # 找一个 noSqz=True 的 bar
    no_sqz_indices = [i for i, v in enumerate(result["noSqz"]) if v is True]
    assert len(no_sqz_indices) > 0, (
        "应至少有一个 bar 满足 noSqz=True（BB/KC 部分重叠）"
    )


# ===== 3. linreg offset=0 输出 =====
def test_linreg_offset_zero_returns_current_bar_value() -> None:
    """验证 linreg(source, length, 0) = intercept + slope * (length - 1)。

    Pine linreg 语义：x = 0..length-1，offset=0 返回当前 bar 对应回归值。
    """
    # 构造线性数据 y = 2x + 5（slope=2, intercept=5）
    length = 20
    x = np.arange(length, dtype=float)
    source = 2.0 * x + 5.0

    result = _linreg_pine(source, length, offset=0)
    # 最后一个 bar（x=length-1）的回归值应等于 2*(length-1)+5
    expected_last = 2.0 * (length - 1) + 5.0
    assert abs(result[-1] - expected_last) < 1e-9, (
        f"linreg offset=0 最后 bar 应等于回归线当前值 {expected_last}，"
        f"实际 {result[-1]}"
    )


def test_linreg_matches_manual_least_squares() -> None:
    """linreg 输出与 numpy.polyfit 手算对比（浮点误差 1e-9）。"""
    rng = np.random.default_rng(7)
    length = 20
    n = 60
    # 构造带噪声的线性数据
    x = np.arange(length, dtype=float)
    true_slope = 0.5
    true_intercept = 10.0
    source = np.concatenate([
        true_slope * x + true_intercept + rng.normal(0, 0.1, length),
        # 后续 bar 用滑动窗口
        rng.normal(15, 0.5, n - length),
    ])

    result = _linreg_pine(source, length, offset=0)

    # 对每个有效窗口用手算 polyfit 验证
    for i in range(length - 1, n):
        window = source[i - length + 1:i + 1]
        # numpy polyfit degree=1 → [slope, intercept]
        coeffs = np.polyfit(np.arange(length), window, 1)
        slope_manual, intercept_manual = coeffs[0], coeffs[1]
        expected_val = intercept_manual + slope_manual * (length - 1)
        assert abs(result[i] - expected_val) < 1e-9, (
            f"bar {i}: linreg={result[i]} vs 手算={expected_val} 不一致"
        )


# ===== 4. 颜色逻辑 =====
def test_bcolor_lime_when_val_positive_and_rising() -> None:
    """val > 0 且 val > prev_val → lime。"""
    # 构造 val 单调上升且为正的场景
    n = 50
    closes = np.linspace(100, 120, n)  # 强上升趋势，val 多头且上升
    highs = closes + 1.0
    lows = closes - 1.0
    opens = closes.copy()

    result = compute_sqzmom_lb(
        opens=opens, highs=highs, lows=lows, closes=closes,
        params=DEFAULT_PARAMS,
    )

    # 找 val > 0 且上升的 bar
    lime_indices = [
        i for i in range(1, n)
        if result["val"][i] is not None
        and result["val"][i] > 0
        and result["val"][i] > (result["val"][i - 1] or 0.0)
        and result["bcolor"][i] == "lime"
    ]
    assert len(lime_indices) > 0, "应至少有一个 bar 满足 bcolor=lime"
    # 验证逻辑：所有 val>0 且 val>prev 的 bar 都应是 lime
    for i in range(1, n):
        val_i = result["val"][i]
        val_prev = result["val"][i - 1]
        if val_i is not None and val_prev is not None and val_i > 0 and val_i > val_prev:
            assert result["bcolor"][i] == "lime", (
                f"bar {i}: val={val_i} > 0 且 > prev={val_prev} 应 lime，"
                f"实际 {result['bcolor'][i]}"
            )


def test_bcolor_green_when_val_positive_and_falling() -> None:
    """val > 0 且 val <= prev_val → green。

    构造场景：先强上升使 val 大幅为正，再平缓波动让 val 仍正但下降。
    """
    n = 60
    # 40 根强上升建立正 val，20 根平缓波动使 val 下降但仍为正
    closes = np.concatenate([
        np.linspace(100, 130, 40),
        np.linspace(130, 128, 20),  # 小幅下降，val 仍正但下降
    ])
    highs = closes + 1.0
    lows = closes - 1.0
    opens = closes.copy()

    result = compute_sqzmom_lb(
        opens=opens, highs=highs, lows=lows, closes=closes,
        params=DEFAULT_PARAMS,
    )

    # 找 val > 0 且 val <= prev 的 bar
    found = False
    for i in range(1, n):
        val_i = result["val"][i]
        val_prev = result["val"][i - 1]
        if (val_i is not None and val_prev is not None
                and val_i > 0 and val_i <= val_prev):
            assert result["bcolor"][i] == "green", (
                f"bar {i}: val={val_i} > 0 且 <= prev={val_prev} 应 green，"
                f"实际 {result['bcolor'][i]}"
            )
            found = True
    assert found, "应至少有一个 bar 满足 bcolor=green"


def test_bcolor_red_when_val_negative_and_falling() -> None:
    """val < 0 且 val < prev_val → red。"""
    n = 50
    # 下降趋势，val 空头且下降
    closes = np.linspace(120, 100, n)
    highs = closes + 1.0
    lows = closes - 1.0
    opens = closes.copy()

    result = compute_sqzmom_lb(
        opens=opens, highs=highs, lows=lows, closes=closes,
        params=DEFAULT_PARAMS,
    )

    found = False
    for i in range(1, n):
        val_i = result["val"][i]
        val_prev = result["val"][i - 1]
        if (val_i is not None and val_prev is not None
                and val_i < 0 and val_i < val_prev):
            assert result["bcolor"][i] == "red", (
                f"bar {i}: val={val_i} < 0 且 < prev={val_prev} 应 red，"
                f"实际 {result['bcolor'][i]}"
            )
            found = True
    assert found, "应至少有一个 bar 满足 bcolor=red"


def test_bcolor_maroon_when_val_negative_and_rising() -> None:
    """val < 0 且 val >= prev_val → maroon。"""
    n = 50
    # 先下降使 val 变负，再小幅上升
    closes = np.concatenate([
        np.linspace(120, 100, 30),
        np.linspace(100, 103, 20),  # 小幅上升
    ])
    highs = closes + 1.0
    lows = closes - 1.0
    opens = closes.copy()

    result = compute_sqzmom_lb(
        opens=opens, highs=highs, lows=lows, closes=closes,
        params=DEFAULT_PARAMS,
    )

    found = False
    for i in range(1, n):
        val_i = result["val"][i]
        val_prev = result["val"][i - 1]
        if (val_i is not None and val_prev is not None
                and val_i < 0 and val_i >= val_prev):
            assert result["bcolor"][i] == "maroon", (
                f"bar {i}: val={val_i} < 0 且 >= prev={val_prev} 应 maroon，"
                f"实际 {result['bcolor'][i]}"
            )
            found = True
    assert found, "应至少有一个 bar 满足 bcolor=maroon"


def test_nz_handles_first_bar_na() -> None:
    """首根 prev_val 取 0（Pine nz() 语义），不抛异常。

    val[0] 存在但 val[-1] 不存在，nz(val[-1]) = 0。
    颜色逻辑应正常计算，不抛 IndexError/KeyError。
    """
    n = 50
    closes = np.linspace(100, 110, n)
    highs = closes + 1.0
    lows = closes - 1.0
    opens = closes.copy()

    # 不应抛异常
    result = compute_sqzmom_lb(
        opens=opens, highs=highs, lows=lows, closes=closes,
        params=DEFAULT_PARAMS,
    )

    # 首根 bcolor 应是合法字符串
    assert result["bcolor"][0] in {"lime", "green", "red", "maroon"}, (
        f"首根 bcolor 应是合法颜色，实际 {result['bcolor'][0]}"
    )


# ===== 5. scolor 逻辑 =====
def test_scolor_blue_when_no_sqz() -> None:
    """noSqz=True → scolor=blue。"""
    n = 50
    closes = np.concatenate([
        np.full(20, 100.0),
        np.linspace(100.0, 110.0, 30),
    ])
    highs = closes + 3.0
    lows = closes - 3.0
    opens = closes.copy()

    result = compute_sqzmom_lb(
        opens=opens, highs=highs, lows=lows, closes=closes,
        params=DEFAULT_PARAMS,
    )

    for i in range(n):
        if result["noSqz"][i] is True:
            assert result["scolor"][i] == "blue", (
                f"bar {i}: noSqz=True 应 scolor=blue，实际 {result['scolor'][i]}"
            )


def test_scolor_black_when_sqz_on() -> None:
    """sqzOn=True → scolor=black。"""
    n = 50
    closes = np.full(n, 100.0)
    closes[30:] = 100.0 + np.linspace(0, 0.1, n - 30)
    highs = closes + 5.0  # 大波动使 KC 宽
    lows = closes - 5.0
    opens = closes.copy()

    result = compute_sqzmom_lb(
        opens=opens, highs=highs, lows=lows, closes=closes,
        params=DEFAULT_PARAMS,
    )

    for i in range(n):
        if result["sqzOn"][i] is True:
            assert result["scolor"][i] == "black", (
                f"bar {i}: sqzOn=True 应 scolor=black，实际 {result['scolor'][i]}"
            )


def test_scolor_gray_when_sqz_off() -> None:
    """sqzOff=True → scolor=gray。"""
    n = 50
    rng = np.random.default_rng(123)
    closes = 100.0 + np.cumsum(rng.normal(0, 2.0, n))
    highs = closes + 0.1
    lows = closes - 0.1
    opens = closes.copy()

    result = compute_sqzmom_lb(
        opens=opens, highs=highs, lows=lows, closes=closes,
        params=DEFAULT_PARAMS,
    )

    for i in range(n):
        if result["sqzOff"][i] is True:
            assert result["scolor"][i] == "gray", (
                f"bar {i}: sqzOff=True 应 scolor=gray，实际 {result['scolor'][i]}"
            )


# ===== 6. stdev ddof=0 验证 =====
def test_stdev_uses_ddof_zero_matches_pine() -> None:
    """验证 _stdev_biased 用 ddof=0（TradingView Pine 默认总体标准差）。

    Pine 的 stdev 函数使用 biased estimator（总体标准差，ddof=0）。
    pandas 默认 ddof=1（样本标准差）。
    二者在 length=20 时差异约 2.7%。

    验证：_stdev_biased 输出 = numpy.std(arr, ddof=0) ≠ numpy.std(arr, ddof=1)。
    """
    rng = np.random.default_rng(99)
    length = 20
    values = rng.normal(100, 2.0, length)

    result = _stdev_biased(values, length)

    # 最后一个有效值（index=length-1）
    actual = result[-1]
    expected_ddof0 = float(np.std(values, ddof=0))
    expected_ddof1 = float(np.std(values, ddof=1))

    assert abs(actual - expected_ddof0) < 1e-9, (
        f"_stdev_biased 应使用 ddof=0 (Pine 默认)，得到 {expected_ddof0}，"
        f"实际 {actual}"
    )
    # 显式证明与 ddof=1 不同
    assert abs(expected_ddof0 - expected_ddof1) > 1e-6, (
        "ddof=0 和 ddof=1 应有差异（length=20）"
    )
    assert abs(actual - expected_ddof1) > 1e-6, (
        f"_stdev_biased 不应使用 ddof=1，差异应大于 1e-6"
    )


# ===== 7. 数据不足时不抛异常 =====
def test_insufficient_data_returns_nans_not_exception() -> None:
    """数据不足 length+lengthKC 时不抛异常，返回 NaN/默认值。

    Pine 行为：sma/stdev/highest/lowest 在数据不足时返回 na。
    Python 实现应返回 NaN（val）和合理默认（sqzOn=False, sqzOff=False, noSqz=True）。
    """
    # 只给 10 根数据（不足 length=20 + lengthKC=20 的 warmup）
    n = 10
    closes = np.linspace(100, 110, n)
    highs = closes + 1.0
    lows = closes - 1.0
    opens = closes.copy()

    # 不应抛异常
    result = compute_sqzmom_lb(
        opens=opens, highs=highs, lows=lows, closes=closes,
        params=DEFAULT_PARAMS,
    )

    # 所有字段长度应等于输入长度
    assert len(result["val"]) == n
    assert len(result["bcolor"]) == n
    assert len(result["scolor"]) == n

    # val 应为 None（NaN 转 JSON null）
    for i in range(n):
        assert result["val"][i] is None, f"bar {i}: 数据不足时 val 应为 None"
        # 数据不足时无法判断 sqzOn/sqzOff，应 noSqz=True（Pine 默认行为）
        assert result["noSqz"][i] is True, f"bar {i}: 数据不足时应 noSqz=True"
        assert result["sqzOn"][i] is False
        assert result["sqzOff"][i] is False
        # scolor 应是 blue（noSqz=True → blue）
        assert result["scolor"][i] == "blue"
        # bcolor 在 val=None 时应取合理默认（Pine 行为：val=na 视为 0）
        assert result["bcolor"][i] in {"lime", "green", "red", "maroon"}


# ===== 8. 固定样本输出确定性 =====
def test_fixed_sample_deterministic_output() -> None:
    """固定 60 根 OHLCV 样本，断言关键 bar 的输出值在允许浮点误差内。

    同一样本多次运行应得到完全相同结果（确定性）。
    """
    ohlcv = _build_ohlcv(n=60, seed=42)

    result1 = compute_sqzmom_lb(
        opens=ohlcv["opens"], highs=ohlcv["highs"],
        lows=ohlcv["lows"], closes=ohlcv["closes"],
        params=DEFAULT_PARAMS,
    )
    result2 = compute_sqzmom_lb(
        opens=ohlcv["opens"], highs=ohlcv["highs"],
        lows=ohlcv["lows"], closes=ohlcv["closes"],
        params=DEFAULT_PARAMS,
    )

    # 两次运行结果应完全一致
    for key in ["val", "sqzOn", "sqzOff", "noSqz", "bcolor", "scolor"]:
        assert result1[key] == result2[key], f"{key} 应确定性输出"

    # 验证最后一个 bar 的 val 是有限数值（warmup 充足）
    last_val = result1["val"][-1]
    assert last_val is not None
    assert math.isfinite(last_val), "最后一根 val 应是有限数值"


# ===== 辅助函数单独测试 =====
def test_sma_basic() -> None:
    """_sma 简单移动平均，前 length-1 根返回 NaN。"""
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    result = _sma(values, length=3)
    # 前 2 根 NaN，第 3 根 (1+2+3)/3=2.0
    assert math.isnan(result[0])
    assert math.isnan(result[1])
    assert abs(result[2] - 2.0) < 1e-9
    assert abs(result[3] - 3.0) < 1e-9
    assert abs(result[4] - 4.0) < 1e-9


def test_highest_basic() -> None:
    """_highest rolling max。"""
    values = np.array([1.0, 5.0, 3.0, 2.0, 4.0])
    result = _highest(values, length=3)
    assert math.isnan(result[0])
    assert math.isnan(result[1])
    assert result[2] == 5.0  # max(1,5,3)
    assert result[3] == 5.0  # max(5,3,2)
    assert result[4] == 4.0  # max(3,2,4)


def test_lowest_basic() -> None:
    """_lowest rolling min。"""
    values = np.array([1.0, 5.0, 3.0, 2.0, 4.0])
    result = _lowest(values, length=3)
    assert math.isnan(result[0])
    assert math.isnan(result[1])
    assert result[2] == 1.0  # min(1,5,3)
    assert result[3] == 2.0  # min(5,3,2)
    assert result[4] == 2.0  # min(3,2,4)


def test_true_range_first_bar_uses_high_low() -> None:
    """首根 tr（无 prev_close）按 Pine 行为用 high-low。"""
    highs = np.array([105.0, 110.0, 108.0])
    lows = np.array([95.0, 100.0, 98.0])
    closes = np.array([100.0, 105.0, 103.0])

    result = _true_range(highs, lows, closes)

    # 首根 tr = high - low = 105 - 95 = 10.0
    assert abs(result[0] - 10.0) < 1e-9, (
        f"首根 tr 应 = high - low = 10.0，实际 {result[0]}"
    )

    # 第二根 tr = max(high-low, |high-prev_close|, |low-prev_close|)
    expected_tr1 = max(
        110.0 - 100.0,
        abs(110.0 - 100.0),
        abs(100.0 - 100.0),
    )
    assert abs(result[1] - expected_tr1) < 1e-9


# ===== params 字段验证 =====
def test_params_field_includes_bb_dev_uses() -> None:
    """params 字段应包含 bb_dev_uses='multKC' 标识。"""
    ohlcv = _build_ohlcv(n=60)
    result = compute_sqzmom_lb(
        opens=ohlcv["opens"], highs=ohlcv["highs"],
        lows=ohlcv["lows"], closes=ohlcv["closes"],
        params=DEFAULT_PARAMS,
    )
    assert "params" in result, "结果应包含 params 字段"
    assert result["params"]["bb_dev_uses"] == "multKC", (
        "params.bb_dev_uses 应为 'multKC'"
    )
    assert result["params"]["length"] == 20
    assert result["params"]["mult"] == 2.0
    assert result["params"]["lengthKC"] == 20
    assert result["params"]["multKC"] == 1.5
    assert result["params"]["useTrueRange"] is True
