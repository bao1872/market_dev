"""SQZMOM_LB - Squeeze Momentum Indicator [LazyBear] 后端算法。

严格逐行复刻 TradingView Pine 原代码（/root/web_dev/ref/SQZMOM_LB.txt）。
不做产品化简化，不修正原脚本疑似 bug（dev = multKC * stdev）。

Pine 原代码：
    study(shorttitle = "SQZMOM_LB", title="Squeeze Momentum Indicator [LazyBear]", overlay=false)
    length = input(20, title="BB Length")
    mult = input(2.0, title="BB MultFactor")
    lengthKC = input(20, title="KC Length")
    multKC = input(1.5, title="KC MultFactor")
    useTrueRange = input(true, title="Use TrueRange (KC)", type=bool)

    source = close
    basis = sma(source, length)
    dev = multKC * stdev(source, length)
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

Pine 语义关键点：
1. stdev 用 biased estimator (ddof=0)，对应 Pine 内置 stdev 函数
2. linreg(source, length, offset) 返回回归线在 x=(length-1-offset) 处的值
3. nz(val[1]) 前值为 na 时取 0
4. tr 首根无 prev_close 时用 high-low
5. na 与数字比较返回 false（Pine 默认行为）
6. dev = multKC * stdev(...) 不修正（原 Pine 代码如此）

用法：
    from app.strategy_assets.algorithms.features.sqzmom_lb import compute_sqzmom_lb
    result = compute_sqzmom_lb(opens, highs, lows, closes, params={
        "length": 20, "mult": 2.0, "lengthKC": 20, "multKC": 1.5, "useTrueRange": True
    })
    # result["val"], result["sqzOn"], result["sqzOff"], result["noSqz"],
    # result["bcolor"], result["scolor"], result["params"], result["_debug_bb_kc"]
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# Pine 颜色常量（与原代码 lime/green/red/maroon/blue/black/gray 一致）
_COLOR_LIME = "lime"
_COLOR_GREEN = "green"
_COLOR_RED = "red"
_COLOR_MAROON = "maroon"
_COLOR_BLUE = "blue"
_COLOR_BLACK = "black"
_COLOR_GRAY = "gray"

# 默认参数（与 Pine 原代码 input 默认值一致）
_DEFAULT_PARAMS: dict[str, Any] = {
    "length": 20,
    "mult": 2.0,
    "lengthKC": 20,
    "multKC": 1.5,
    "useTrueRange": True,
}


def _sma(values: np.ndarray, length: int) -> np.ndarray:
    """简单移动平均（对应 Pine sma(source, length)）。

    Args:
        values: 输入数值数组
        length: 窗口长度

    Returns:
        与输入等长的数组，前 length-1 个为 NaN，从 index=length-1 起为对应窗口均值
    """
    s = pd.Series(values, dtype=float)
    return s.rolling(window=length, min_periods=length).mean().to_numpy()


def _stdev_biased(values: np.ndarray, length: int) -> np.ndarray:
    """总体标准差（ddof=0），匹配 TradingView Pine 内置 stdev 默认行为。

    Pine stdev 函数使用 biased estimator（总体标准差，ddof=0）。
    pandas 默认 ddof=1（样本标准差），必须显式指定 ddof=0。

    Args:
        values: 输入数值数组
        length: 窗口长度

    Returns:
        与输入等长的数组，前 length-1 个为 NaN
    """
    s = pd.Series(values, dtype=float)
    return s.rolling(window=length, min_periods=length).std(ddof=0).to_numpy()


def _highest(values: np.ndarray, length: int) -> np.ndarray:
    """滚动最大值（对应 Pine highest(high, length)）。

    Args:
        values: 输入数值数组
        length: 窗口长度

    Returns:
        与输入等长的数组，前 length-1 个为 NaN
    """
    s = pd.Series(values, dtype=float)
    return s.rolling(window=length, min_periods=length).max().to_numpy()


def _lowest(values: np.ndarray, length: int) -> np.ndarray:
    """滚动最小值（对应 Pine lowest(low, length)）。

    Args:
        values: 输入数值数组
        length: 窗口长度

    Returns:
        与输入等长的数组，前 length-1 个为 NaN
    """
    s = pd.Series(values, dtype=float)
    return s.rolling(window=length, min_periods=length).min().to_numpy()


def _true_range(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> np.ndarray:
    """True Range（对应 Pine tr 内置函数）。

    公式：tr = max(high - low, |high - prev_close|, |low - prev_close|)
    首根无 prev_close 时按 Pine 行为返回 high - low。

    Args:
        highs: 最高价数组
        lows: 最低价数组
        closes: 收盘价数组（用于取 prev_close）

    Returns:
        与输入等长的 tr 数组，首根 = high - low
    """
    n = len(highs)
    tr = np.empty(n, dtype=float)
    if n == 0:
        return tr

    # 首根：无 prev_close，按 Pine tr 行为取 high - low
    tr[0] = highs[0] - lows[0]

    # 后续根：max(high-low, |high-prev_close|, |low-prev_close|)
    for i in range(1, n):
        prev_close = closes[i - 1]
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - prev_close)
        lc = abs(lows[i] - prev_close)
        tr[i] = max(hl, hc, lc)

    return tr


def _linreg_pine(source: np.ndarray, length: int, offset: int = 0) -> np.ndarray:
    """线性回归（对应 Pine linreg(source, length, offset)）。

    Pine linreg 语义：
    - 在每个 bar 取过去 length 根（含当前）的 source 值
    - 用 x = 0, 1, ..., length-1 做最小二乘拟合
    - 返回回归线在 x = (length - 1 - offset) 处的值
    - offset=0 时返回当前 bar 对应的回归值 = intercept + slope * (length - 1)

    最小二乘公式：
        slope = (n * Σxy - Σx * Σy) / (n * Σx² - (Σx)²)
        intercept = (Σy - slope * Σx) / n

    Args:
        source: 输入数值数组
        length: 回归窗口长度
        offset: 偏移量（0=当前 bar，正数=向前 N 根）

    Returns:
        与输入等长的数组，前 length-1 个为 NaN，含 NaN 窗口也为 NaN
    """
    n = len(source)
    result = np.full(n, np.nan)
    if n < length or length < 2:
        return result

    # 预计算 x 相关常数（x = 0..length-1）
    x = np.arange(length, dtype=float)
    sum_x = float(x.sum())
    sum_x2 = float((x * x).sum())
    denom = length * sum_x2 - sum_x * sum_x
    if denom == 0:
        return result  # length=1 退化情况，不应发生（前面已检查 length>=2）

    # 对每个有效窗口做最小二乘
    target_x = length - 1 - offset
    for i in range(length - 1, n):
        y = source[i - length + 1:i + 1]
        # 窗口含 NaN 时跳过（Pine 行为：linreg 在 na 输入时返回 na）
        if np.isnan(y).any():
            continue
        sum_y = float(y.sum())
        sum_xy = float((x * y).sum())
        slope = (length * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / length
        result[i] = intercept + slope * target_x

    return result


def _to_color_or_none(arr: np.ndarray) -> list[str | None]:
    """把颜色数组转为 list（颜色本身就是字符串，无 NaN 概念）。"""
    return [str(v) for v in arr]


def _to_float_or_none(arr: np.ndarray) -> list[float | None]:
    """把 numpy 数组转为 list[float | None]，NaN 转 None（JSON null）。"""
    result: list[float | None] = []
    for v in arr:
        if v is None:
            result.append(None)
        elif isinstance(v, float) and (np.isnan(v) or not np.isfinite(v)):
            result.append(None)
        else:
            result.append(float(v))
    return result


def _to_bool_list(arr: np.ndarray) -> list[bool]:
    """把 numpy bool/掩码数组转为 list[bool]，NaN 视为 False（Pine na 比较返回 false）。"""
    result: list[bool] = []
    for v in arr:
        if v is None:
            result.append(False)
        elif isinstance(v, float) and np.isnan(v):
            result.append(False)
        else:
            result.append(bool(v))
    return result


def compute_sqzmom_lb(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """计算 SQZMOM_LB 指标（逐行复刻 LazyBear Pine 代码）。

    Args:
        opens: 开盘价数组
        highs: 最高价数组
        lows: 最低价数组
        closes: 收盘价数组
        params: 参数字典（可选），默认 {"length": 20, "mult": 2.0, "lengthKC": 20, "multKC": 1.5, "useTrueRange": True}

    Returns:
        dict 包含：
        - val: list[float | None] - linreg 动量值（NaN 转 None）
        - sqzOn: list[bool] - BB 在 KC 内部（挤压开启）
        - sqzOff: list[bool] - BB 在 KC 外部（挤压释放）
        - noSqz: list[bool] - 部分重叠（无挤压状态）
        - bcolor: list[str] - histogram 颜色（lime/green/red/maroon）
        - scolor: list[str] - squeeze marker 颜色（blue/black/gray）
        - params: dict - 实际使用的参数（含 bb_dev_uses="multKC" 标识）
        - _debug_bb_kc: dict - BB/KC 中间值（仅用于测试验证，indicator_service 可忽略）

    数据不足时不抛异常，返回 NaN/默认值（与 Pine 行为一致）。
    """
    # 合并参数（不修改原字典）
    p = {**_DEFAULT_PARAMS, **(params or {})}
    length = int(p["length"])
    mult = float(p["mult"])
    length_kc = int(p["lengthKC"])
    mult_kc = float(p["multKC"])
    use_true_range = bool(p["useTrueRange"])

    n = len(closes)

    # 数据不足时返回全 NaN + 默认颜色
    # Pine 行为：sma/stdev/highest/lowest 在 length 不够时返回 na
    if n < max(length, length_kc):
        # 全部 val = None, sqzOn=False, sqzOff=False, noSqz=True, scolor=blue
        # bcolor：val=na 时按 Pine 逻辑（na > 0 = false, na < 0 = false）→ maroon
        bcolor_list = [_COLOR_MAROON] * n
        scolor_list = [_COLOR_BLUE] * n
        return {
            "val": [None] * n,
            "sqzOn": [False] * n,
            "sqzOff": [False] * n,
            "noSqz": [True] * n,
            "bcolor": bcolor_list,
            "scolor": scolor_list,
            "params": {
                "length": length,
                "mult": mult,
                "lengthKC": length_kc,
                "multKC": mult_kc,
                "useTrueRange": use_true_range,
                "bb_dev_uses": "multKC",
            },
            "_debug_bb_kc": {
                "basis": [None] * n,
                "dev": [None] * n,
                "upperBB": [None] * n,
                "lowerBB": [None] * n,
                "ma": [None] * n,
                "rangema": [None] * n,
                "upperKC": [None] * n,
                "lowerKC": [None] * n,
            },
        }

    # 转为 float 数组（防御性）
    closes_f = np.asarray(closes, dtype=float)
    highs_f = np.asarray(highs, dtype=float)
    lows_f = np.asarray(lows, dtype=float)

    # ===== BB（注意：dev 用 multKC，不修正 Pine 原代码）=====
    # basis = sma(source, length)
    basis = _sma(closes_f, length)
    # dev = multKC * stdev(source, length)  -- Pine 原代码用 multKC
    stdev_arr = _stdev_biased(closes_f, length)
    dev = mult_kc * stdev_arr
    upper_bb = basis + dev
    lower_bb = basis - dev

    # ===== KC =====
    # ma = sma(source, lengthKC)
    ma = _sma(closes_f, length_kc)
    # range = useTrueRange ? tr : (high - low)
    if use_true_range:
        range_val = _true_range(highs_f, lows_f, closes_f)
    else:
        range_val = highs_f - lows_f
    # rangema = sma(range, lengthKC)
    rangema = _sma(range_val, length_kc)
    # upperKC = ma + rangema * multKC
    upper_kc = ma + rangema * mult_kc
    # lowerKC = ma - rangema * multKC
    lower_kc = ma - rangema * mult_kc

    # ===== Squeeze 状态判断 =====
    # sqzOn  = (lowerBB > lowerKC) and (upperBB < upperKC)
    # Pine 语义：na 与数字比较返回 false，na and X 返回 false
    sqz_on_raw = (lower_bb > lower_kc) & (upper_bb < upper_kc)
    sqz_off_raw = (lower_bb < lower_kc) & (upper_bb > upper_kc)
    # 把 NaN 处理为 False（Pine na 比较返回 false）
    sqz_on_arr = np.where(np.isnan(sqz_on_raw), False, sqz_on_raw)
    sqz_off_arr = np.where(np.isnan(sqz_off_raw), False, sqz_off_raw)
    # noSqz = (sqzOn == false) and (sqzOff == false)
    no_sqz_arr = (~sqz_on_arr.astype(bool)) & (~sqz_off_arr.astype(bool))

    # ===== val = linreg(source - midline, lengthKC, 0) =====
    # midline = avg(avg(highest(high, lengthKC), lowest(low, lengthKC)), sma(close, lengthKC))
    # avg(a, b) = (a + b) / 2
    highest_high = _highest(highs_f, length_kc)
    lowest_low = _lowest(lows_f, length_kc)
    avg_hl = (highest_high + lowest_low) / 2.0  # avg(highest, lowest)
    sma_close_kc = _sma(closes_f, length_kc)
    midline = (avg_hl + sma_close_kc) / 2.0  # avg(avg_hl, sma_close)
    # source - midline
    delta = closes_f - midline
    # val = linreg(delta, lengthKC, 0)
    val = _linreg_pine(delta, length_kc, offset=0)

    # ===== 颜色逻辑 =====
    # bcolor = iff(val > 0, iff(val > nz(val[1]), lime, green),
    #                       iff(val < nz(val[1]), red, maroon))
    # nz(val[1])：前值为 na 时取 0
    prev_val = np.empty(n, dtype=float)
    if n == 0:
        pass
    else:
        prev_val[0] = 0.0  # 首根 nz(na) = 0
        prev_val[1:] = val[:-1]
        # 把 NaN prev_val 转为 0（nz 语义）
        prev_val = np.where(np.isnan(prev_val), 0.0, prev_val)

    # Pine na 比较语义：na > x = false, na < x = false, na > na = false
    bcolor_arr: list[str] = []
    for i in range(n):
        v = val[i]
        pv = prev_val[i]
        if np.isnan(v):
            # val=na 时：val>0=false → else; val<nz(val[1])=na<0=false → else 的 else → maroon
            bcolor_arr.append(_COLOR_MAROON)
        elif v > 0:
            if v > pv:
                bcolor_arr.append(_COLOR_LIME)
            else:
                bcolor_arr.append(_COLOR_GREEN)
        else:  # v <= 0
            if v < pv:
                bcolor_arr.append(_COLOR_RED)
            else:
                bcolor_arr.append(_COLOR_MAROON)

    # scolor = noSqz ? blue : sqzOn ? black : gray
    scolor_arr: list[str] = []
    for i in range(n):
        if no_sqz_arr[i]:
            scolor_arr.append(_COLOR_BLUE)
        elif sqz_on_arr[i]:
            scolor_arr.append(_COLOR_BLACK)
        else:  # sqzOff
            scolor_arr.append(_COLOR_GRAY)

    return {
        "val": _to_float_or_none(val),
        "sqzOn": _to_bool_list(sqz_on_arr),
        "sqzOff": _to_bool_list(sqz_off_arr),
        "noSqz": _to_bool_list(no_sqz_arr),
        "bcolor": bcolor_arr,
        "scolor": scolor_arr,
        "params": {
            "length": length,
            "mult": mult,
            "lengthKC": length_kc,
            "multKC": mult_kc,
            "useTrueRange": use_true_range,
            "bb_dev_uses": "multKC",
        },
        "_debug_bb_kc": {
            "basis": _to_float_or_none(basis),
            "dev": _to_float_or_none(dev),
            "upperBB": _to_float_or_none(upper_bb),
            "lowerBB": _to_float_or_none(lower_bb),
            "ma": _to_float_or_none(ma),
            "rangema": _to_float_or_none(rangema),
            "upperKC": _to_float_or_none(upper_kc),
            "lowerKC": _to_float_or_none(lower_kc),
        },
    }


# ===== 模块自测入口 =====
if __name__ == "__main__":
    # 自测入口：验证模块加载和函数签名（不连 DB/网络）
    import inspect

    # 1. 验证 compute_sqzmom_lb 函数存在且签名正确
    assert callable(compute_sqzmom_lb), "compute_sqzmom_lb 应可调用"
    sig = inspect.signature(compute_sqzmom_lb)
    params = list(sig.parameters.keys())
    expected = ["opens", "highs", "lows", "closes", "params"]
    assert params == expected, f"参数不匹配: {params} != {expected}"
    print(f"compute_sqzmom_lb params={params} OK")

    # 2. 验证默认参数
    assert _DEFAULT_PARAMS["length"] == 20
    assert _DEFAULT_PARAMS["mult"] == 2.0
    assert _DEFAULT_PARAMS["lengthKC"] == 20
    assert _DEFAULT_PARAMS["multKC"] == 1.5
    assert _DEFAULT_PARAMS["useTrueRange"] is True
    print(f"_DEFAULT_PARAMS={_DEFAULT_PARAMS} OK")

    # 3. 验证 _sma
    s = _sma(np.array([1.0, 2.0, 3.0, 4.0, 5.0]), 3)
    assert np.isnan(s[0]) and np.isnan(s[1])
    assert abs(s[2] - 2.0) < 1e-9 and abs(s[4] - 4.0) < 1e-9
    print("_sma OK")

    # 4. 验证 _stdev_biased 用 ddof=0
    arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    sd = _stdev_biased(arr, 5)
    expected_sd = float(np.std(arr, ddof=0))
    assert abs(sd[-1] - expected_sd) < 1e-9
    assert abs(sd[-1] - float(np.std(arr, ddof=1))) > 1e-6
    print(f"_stdev_biased ddof=0 OK (val={sd[-1]:.6f})")

    # 5. 验证 _true_range 首根用 high-low
    tr = _true_range(np.array([105.0]), np.array([95.0]), np.array([100.0]))
    assert abs(tr[0] - 10.0) < 1e-9
    print("_true_range first bar OK")

    # 6. 验证 _highest/_lowest
    h = _highest(np.array([1.0, 5.0, 3.0, 2.0, 4.0]), 3)
    lo = _lowest(np.array([1.0, 5.0, 3.0, 2.0, 4.0]), 3)
    assert h[2] == 5.0 and h[4] == 4.0
    assert lo[2] == 1.0 and lo[4] == 2.0
    print("_highest/_lowest OK")

    # 7. 验证 _linreg_pine offset=0
    x = np.arange(20, dtype=float)
    src = 2.0 * x + 5.0
    lr = _linreg_pine(src, 20, offset=0)
    assert abs(lr[-1] - (2.0 * 19 + 5.0)) < 1e-9
    print(f"_linreg_pine offset=0 OK (last={lr[-1]:.6f})")

    # 8. 验证数据不足不抛异常
    short_closes = np.array([100.0, 101.0, 102.0])
    short_highs = np.array([101.0, 102.0, 103.0])
    short_lows = np.array([99.0, 100.0, 101.0])
    short_opens = np.array([100.0, 101.0, 102.0])
    r = compute_sqzmom_lb(
        opens=short_opens, highs=short_highs, lows=short_lows, closes=short_closes,
    )
    assert all(v is None for v in r["val"])
    assert all(v is True for v in r["noSqz"])
    assert all(v is False for v in r["sqzOn"])
    assert r["params"]["bb_dev_uses"] == "multKC"
    print("insufficient data OK")

    # 9. 验证完整计算
    rng = np.random.default_rng(42)
    n = 60
    closes = 100.0 + np.cumsum(rng.normal(0, 0.5, n))
    highs = closes + np.abs(rng.normal(0, 1.0, n))
    lows = closes - np.abs(rng.normal(0, 1.0, n))
    opens = closes + rng.normal(0, 0.3, n)
    r = compute_sqzmom_lb(opens=opens, highs=highs, lows=lows, closes=closes)
    assert len(r["val"]) == n
    assert len(r["bcolor"]) == n
    assert len(r["scolor"]) == n
    # warmup 后 val 应有有效数值
    valid_count = sum(1 for v in r["val"] if v is not None)
    assert valid_count >= n - 40, f"应有足够 warmup 后的有效值，实际 {valid_count}/{n}"
    # bcolor 应都是合法颜色
    valid_colors = {_COLOR_LIME, _COLOR_GREEN, _COLOR_RED, _COLOR_MAROON}
    assert all(c in valid_colors for c in r["bcolor"])
    # scolor 应都是合法颜色
    valid_scolors = {_COLOR_BLUE, _COLOR_BLACK, _COLOR_GRAY}
    assert all(c in valid_scolors for c in r["scolor"])
    print(f"full compute OK (valid_vals={valid_count}/{n})")

    print("All self-tests passed")
