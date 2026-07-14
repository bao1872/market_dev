"""SMC (Smart Money Concepts) 指标计算模块（纯函数，无外部依赖）。

算法真源：用户提供的 ref/smc.py 重写版本（非 LuxAlgo Pine 翻译）。
本模块剥离 ref/smc.py 中的 pytdx datasource、argparse、Plotly、HTML 输出等演示壳，
仅保留 SMCIndicatorPineCloser 的逐 bar 状态机、pivot、BOS/CHoCH、OB、EQH/EQL、
trailing strong/weak 逻辑，改造成纯函数模块。

许可证与 clean-room 说明：
    本模块基于用户提供的 Python 重写版本，不是 LuxAlgo Pine 源码的翻译。
    不引用、不依赖 ref/ 中的 LuxAlgo Pine 源码。

FVG 完全排除：
    Fair Value Gap 不计算、不返回、不缓存、不渲染，也不暴露 FVG 开关。
    生产计算路径不包含 FVG 函数或状态；输出结构中不存在 FVG 相关键、
    事件或 box。注释/文档可以正常写"FVG 不计算、不返回、不显示"。

默认参数（来自 ref/smc.py build_parser() 默认值，不自行调整）：
    swings_length = 50
    equal_length = 3
    equal_threshold = 0.1
    internal_filter_confluence = False
    internal_ob_size = 5
    swing_ob_size = 5
    order_block_filter = "Atr"
    order_block_mitigation = "High/Low"
    show_internal_order_blocks = True   (默认开启)
    show_swing_order_blocks = False     (默认关闭)
    show_equal_hl = True                (默认开启)
    show_high_low_swings = True         (默认开启)
    show_swings = False                 (HH/HL/LH/LL 标签关闭)

anchor/confirmed 因果契约：
    每个pivot/事件同时返回 anchor_index/anchor_time 与 confirmed_index/confirmed_time。
    - pivot.anchor = ref_i (i-size)，pivot.confirmed = i (leg change 确认 bar)
    - BOS/CHoCH.anchor = pivot.barIndex (被穿越的 pivot bar)
      BOS/CHoCH.confirmed = i (close 穿越 pivot 的 bar)
    - OB.anchor = parsed_index (OB bar)
      OB.confirmed = current_i (触发 OB 创建的 BOS/CHoCH bar)
    - EQH/EQL.anchor = prev piv.barIndex (前一 pivot)
      EQH/EQL.confirmed = i-size (新 pivot bar)
    - Mitigation.confirmed = i (close/high/low 穿越 OB 的 bar)
    API 事件时间使用 confirmed；可视化从 anchor 画到 confirmed。
    未来 bar 不得修改已确认事件（事件一旦写入即不可变）。

输出结构：
    {
        "events": [BOS/CHoCH events],
        "order_blocks": [internal OBs (含 mitigation 状态)],
        "equal_highs_lows": [EQH/EQL events],
        "trailing": {strong/weak high/low},
        "pivots": [所有 pivot 信息],
        "time": [与输入对齐的时间字符串列表],
    }

Inputs:
    opens/highs/lows/closes: 价格序列（等长，长度 >= 0）
    times: ISO 格式时间字符串列表（与价格序列等长）
    params: 可选参数覆盖（默认使用 DEFAULT_PARAMS）

Outputs:
    dict: SMC 指标结果（可 JSON 序列化）

How to Run:
    python -m app.strategy_assets.algorithms.features.smc_indicator    # 自测
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger("strategy_assets.features.smc_indicator")

# ===== 常量（与 ref/smc.py 对齐）=====
BULLISH = 1
BEARISH = -1
BULLISH_LEG = 1
BEARISH_LEG = 0

# Order block filter 选项
ATR = "Atr"
RANGE = "Cumulative Mean Range"

# Mitigation 类型
CLOSE = "Close"
HIGHLOW = "High/Low"

# ===== 默认参数（严格读取自 ref/smc.py build_parser() 默认值）=====
DEFAULT_PARAMS: dict[str, Any] = {
    "swings_length": 50,
    "equal_length": 3,
    "equal_threshold": 0.1,
    "internal_filter_confluence": False,
    "internal_ob_size": 5,
    "swing_ob_size": 5,
    "order_block_filter": ATR,
    "order_block_mitigation": HIGHLOW,
    "show_internal_order_blocks": True,
    "show_swing_order_blocks": False,
    "show_equal_hl": True,
    "show_high_low_swings": True,
    "show_swings": False,
}


# ===== 数据类 =====


@dataclass
class _Pivot:
    """内部 pivot 状态（不直接序列化）。"""
    current_level: float = float("nan")
    last_level: float = float("nan")
    crossed: bool = False
    bar_time: str | None = None
    bar_index: int | None = None


@dataclass
class _Trend:
    bias: int = 0  # 0=未定，BULLISH=1，BEARISH=-1


@dataclass
class _TrailingExtremes:
    top: float = float("nan")
    bottom: float = float("nan")
    bar_time: str | None = None
    bar_index: int | None = None
    last_top_time: str | None = None
    last_bottom_time: str | None = None


@dataclass
class _OrderBlock:
    """内部 OB 状态。

    anchor_index/anchor_time: OB bar 位置（parsed_index）
    confirmed_index/confirmed_time: 触发 OB 创建的 BOS/CHoCH bar 位置
    mitigated/mitigated_index/mitigated_time: mitigation 信息（一旦 mitigated=True 不可变）
    """
    bar_high: float
    bar_low: float
    bar_time: str
    bar_index: int
    bias: int
    confirmed_index: int
    confirmed_time: str
    mitigated: bool = False
    mitigated_index: int | None = None
    mitigated_time: str | None = None


# ===== 状态机 =====


class _SMCState:
    """SMC 逐 bar 状态机（内部使用）。

    不依赖 pandas/numpy，使用 Python 原生数据结构。
    """

    def __init__(
        self,
        opens: list[float],
        highs: list[float],
        lows: list[float],
        closes: list[float],
        times: list[str],
        params: dict[str, Any],
    ) -> None:
        self.opens = opens
        self.highs = highs
        self.lows = lows
        self.closes = closes
        self.times = times
        self.params = params
        self.n = len(closes)

        # 计算波动率指标（tr/atr200/cmr/volatilityMeasure/parsedHigh/parsedLow）
        self.tr = self._compute_true_range()
        self.atr200 = self._compute_atr(self.tr, 200)
        self.cmr = self._compute_cumulative_mean_range(self.tr)
        self.volatility_measure = (
            self.atr200
            if params["order_block_filter"] == ATR
            else self.cmr
        )
        self.parsed_highs, self.parsed_lows = self._compute_parsed_high_low()

        # pivot 状态（swing/internal/equal high/low）
        self.swing_high = _Pivot()
        self.swing_low = _Pivot()
        self.internal_high = _Pivot()
        self.internal_low = _Pivot()
        self.equal_high = _Pivot()
        self.equal_low = _Pivot()

        # trend 状态
        self.swing_trend = _Trend()
        self.internal_trend = _Trend()

        # trailing extremes
        self.trailing = _TrailingExtremes()

        # order blocks（仅 internal，swing OB 默认关闭）
        self.internal_order_blocks: list[_OrderBlock] = []
        self.swing_order_blocks: list[_OrderBlock] = []

        # 事件输出列表（一旦写入不可变）
        self.events: list[dict[str, Any]] = []
        self.equal_highs_lows: list[dict[str, Any]] = []
        self.pivots: list[dict[str, Any]] = []
        # order_blocks 输出列表（与 internal_order_blocks 同步，含 mitigation 状态）
        self.order_blocks_output: list[dict[str, Any]] = []

        # leg 状态缓存：{(lane, size): {i: leg_value}}
        self.leg_states: dict[tuple[str, int], dict[int, int]] = {}

    # ----- 波动率指标 -----

    def _compute_true_range(self) -> list[float]:
        """计算 True Range：max(high-low, |high-prev_close|, |low-prev_close|)。"""
        n = self.n
        tr = [0.0] * n
        for i in range(n):
            if i == 0:
                tr[i] = self.highs[i] - self.lows[i]
            else:
                prev_close = self.closes[i - 1]
                tr[i] = max(
                    self.highs[i] - self.lows[i],
                    abs(self.highs[i] - prev_close),
                    abs(self.lows[i] - prev_close),
                )
        return tr

    @staticmethod
    def _compute_atr(tr: list[float], n: int) -> list[float]:
        """计算 ATR：tr.rolling(n, min_periods=1).mean()。"""
        result = [0.0] * len(tr)
        if not tr:
            return result
        window_sum = 0.0
        for i, val in enumerate(tr):
            window_sum += val
            if i >= n:
                window_sum -= tr[i - n]
            count = min(i + 1, n)
            result[i] = window_sum / count if count > 0 else 0.0
        return result

    @staticmethod
    def _compute_cumulative_mean_range(tr: list[float]) -> list[float]:
        """计算 Cumulative Mean Range：tr.cumsum() / arange(1, n+1)。"""
        n = len(tr)
        result = [0.0] * n
        cumsum = 0.0
        for i, val in enumerate(tr):
            cumsum += val
            result[i] = cumsum / (i + 1)
        return result

    def _compute_parsed_high_low(self) -> tuple[list[float], list[float]]:
        """计算 parsedHigh/parsedLow（高波动 bar 互换 high/low）。

        highVolatilityBar = (high - low) >= 2.0 * volatilityMeasure
        parsedHigh = low if highVolatilityBar else high
        parsedLow = high if highVolatilityBar else low
        """
        n = self.n
        parsed_high = [0.0] * n
        parsed_low = [0.0] * n
        for i in range(n):
            vol = self.volatility_measure[i]
            high_vol_bar = (self.highs[i] - self.lows[i]) >= 2.0 * vol
            if high_vol_bar:
                parsed_high[i] = self.lows[i]
                parsed_low[i] = self.highs[i]
            else:
                parsed_high[i] = self.highs[i]
                parsed_low[i] = self.lows[i]
        return parsed_high, parsed_low

    # ----- leg 检测 -----

    def _highest_after_ref_window(self, ref_i: int, size: int) -> float:
        """对应 Pine: high[size] > ta.highest(size)。

        窗口是 ref_i 之后直到当前 bar 的 size 根，即 [ref_i+1, ref_i+size]。
        """
        start = max(0, ref_i + 1)
        end = min(self.n, ref_i + size + 1)
        if start >= end:
            return self.highs[ref_i]
        return max(self.highs[start:end])

    def _lowest_after_ref_window(self, ref_i: int, size: int) -> float:
        """对应 Pine: low[size] < ta.lowest(size)。"""
        start = max(0, ref_i + 1)
        end = min(self.n, ref_i + size + 1)
        if start >= end:
            return self.lows[ref_i]
        return min(self.lows[start:end])

    def leg(self, i: int, size: int, lane: str) -> int:
        """逐 bar leg 检测（memoized per (lane, size)）。

        new_leg_high = highs[ref_i] > max(highs[ref_i+1..ref_i+size])
        new_leg_low = lows[ref_i] < min(lows[ref_i+1..ref_i+size])
        if new_leg_high → BEARISH_LEG(0)
        elif new_leg_low → BULLISH_LEG(1)
        else → prev
        """
        if i < size:
            return 0
        state_map = self.leg_states.setdefault((lane, size), {})
        if i in state_map:
            return state_map[i]
        prev = state_map.get(i - 1, 0)
        ref_i = i - size
        new_leg_high = self.highs[ref_i] > self._highest_after_ref_window(ref_i, size)
        new_leg_low = self.lows[ref_i] < self._lowest_after_ref_window(ref_i, size)
        out = prev
        if new_leg_high:
            out = BEARISH_LEG
        elif new_leg_low:
            out = BULLISH_LEG
        state_map[i] = out
        return out

    def start_of_new_leg(self, i: int, size: int, lane: str) -> bool:
        return i > size and self.leg(i, size, lane) != self.leg(i - 1, size, lane)

    def start_of_bearish_leg(self, i: int, size: int, lane: str) -> bool:
        return i > size and (self.leg(i, size, lane) - self.leg(i - 1, size, lane) == -1)

    def start_of_bullish_leg(self, i: int, size: int, lane: str) -> bool:
        return i > size and (self.leg(i, size, lane) - self.leg(i - 1, size, lane) == 1)

    # ----- pivot 检测（含 EQH/EQL） -----

    def get_current_structure(
        self,
        i: int,
        size: int,
        equal_high_low: bool = False,
        internal: bool = False,
    ) -> None:
        """pivot 检测 + EQH/EQL 生成。

        anchor = ref_i (i-size)，confirmed = i (leg change 确认 bar)。
        EQH/EQL.anchor = prev piv.barIndex，confirmed = i-size (新 pivot bar)。
        """
        if i <= size:
            return

        lane = "equal" if equal_high_low else "internal" if internal else "swing"
        new_pivot = self.start_of_new_leg(i, size, lane)
        pivot_low = self.start_of_bullish_leg(i, size, lane)
        pivot_high = self.start_of_bearish_leg(i, size, lane)
        atr_measure = self.atr200[i] if i < self.n else float("nan")

        if not new_pivot:
            return

        ref_i = i - size

        if pivot_low:
            piv = (
                self.equal_low if equal_high_low
                else self.internal_low if internal
                else self.swing_low
            )
            level = self.lows[ref_i]

            # EQH/EQL 检测：|prev_current_level - level| < equal_threshold * atr_measure
            if (
                equal_high_low
                and piv.current_level == piv.current_level  # not NaN
                and abs(piv.current_level - level) < self.params["equal_threshold"] * atr_measure
            ):
                # 输出 EQL 事件
                # anchor = prev piv.barIndex，confirmed = ref_i (新 pivot bar)
                self.equal_highs_lows.append({
                    "type": "EQL",
                    "anchor_index": piv.bar_index,
                    "anchor_time": piv.bar_time,
                    "confirmed_index": ref_i,
                    "confirmed_time": self.times[ref_i],
                    "level": level,
                    "prev_level": piv.current_level,
                })

            # 记录 pivot 事件（pivot 更新前）
            self._record_pivot(piv, level, ref_i, i, "low", internal, equal_high_low)

            piv.last_level = piv.current_level
            piv.current_level = level
            piv.crossed = False
            piv.bar_time = self.times[ref_i]
            piv.bar_index = ref_i

            # trailing bottom 仅 swing low 更新
            if not equal_high_low and not internal:
                self.trailing.bottom = piv.current_level
                self.trailing.bar_time = piv.bar_time
                self.trailing.bar_index = piv.bar_index
                self.trailing.last_bottom_time = piv.bar_time

        elif pivot_high:
            piv = (
                self.equal_high if equal_high_low
                else self.internal_high if internal
                else self.swing_high
            )
            level = self.highs[ref_i]

            if (
                equal_high_low
                and piv.current_level == piv.current_level  # not NaN
                and abs(piv.current_level - level) < self.params["equal_threshold"] * atr_measure
            ):
                self.equal_highs_lows.append({
                    "type": "EQH",
                    "anchor_index": piv.bar_index,
                    "anchor_time": piv.bar_time,
                    "confirmed_index": ref_i,
                    "confirmed_time": self.times[ref_i],
                    "level": level,
                    "prev_level": piv.current_level,
                })

            self._record_pivot(piv, level, ref_i, i, "high", internal, equal_high_low)

            piv.last_level = piv.current_level
            piv.current_level = level
            piv.crossed = False
            piv.bar_time = self.times[ref_i]
            piv.bar_index = ref_i

            if not equal_high_low and not internal:
                self.trailing.top = piv.current_level
                self.trailing.bar_time = piv.bar_time
                self.trailing.bar_index = piv.bar_index
                self.trailing.last_top_time = piv.bar_time

    def _record_pivot(
        self,
        piv: _Pivot,
        level: float,
        ref_i: int,
        confirmed_i: int,
        kind: Literal["high", "low"],
        internal: bool,
        equal_high_low: bool,
    ) -> None:
        """记录 pivot 事件到 pivots 输出列表（一旦写入不可变）。"""
        pivot_type = (
            "equal_high" if equal_high_low and kind == "high"
            else "equal_low" if equal_high_low
            else "internal_high" if internal and kind == "high"
            else "internal_low" if internal
            else "swing_high" if kind == "high"
            else "swing_low"
        )
        self.pivots.append({
            "type": pivot_type,
            "anchor_index": ref_i,
            "anchor_time": self.times[ref_i],
            "confirmed_index": confirmed_i,
            "confirmed_time": self.times[confirmed_i],
            "level": level,
            "last_level": piv.current_level if piv.current_level == piv.current_level else None,
        })

    # ----- BOS/CHoCH 检测 -----

    def display_structure(self, i: int, internal: bool = False) -> None:
        """BOS/CHoCH 检测（close crossover/crossunder pivot level）。

        anchor = pivot.barIndex (被穿越的 pivot bar)
        confirmed = i (close 穿越的 bar)
        """
        if i <= 0 or i >= self.n:
            return

        close_prev = self.closes[i - 1]
        close_curr = self.closes[i]

        # internal_filter_confluence 默认 False，bullish_bar/bearish_bar 默认 True
        bullish_bar = True
        bearish_bar = True
        if self.params["internal_filter_confluence"]:
            row_high = self.highs[i]
            row_low = self.lows[i]
            row_open = self.opens[i]
            row_close = self.closes[i]
            bullish_bar = (row_high - max(row_close, row_open)) > min(row_close, row_open - row_low)
            bearish_bar = (row_high - max(row_close, row_open)) < min(row_close, row_open - row_low)

        # Bullish cross (close 上穿 pivot.currentLevel)
        piv_high = self.internal_high if internal else self.swing_high
        trd = self.internal_trend if internal else self.swing_trend
        # extra_condition: internal 模式下 pivot.currentLevel != swingHigh.currentLevel
        extra_condition = (
            (piv_high.current_level != self.swing_high.current_level) and bullish_bar
            if internal else True
        )

        if (
            piv_high.current_level == piv_high.current_level  # not NaN
            and close_prev <= piv_high.current_level
            and close_curr > piv_high.current_level
            and not piv_high.crossed
            and extra_condition
        ):
            tag = "CHoCH" if trd.bias == BEARISH else "BOS"
            piv_high.crossed = True
            trd.bias = BULLISH

            # anchor = piv_high.bar_index (被穿越的 pivot bar)
            # confirmed = i (close 穿越的 bar)
            self.events.append({
                "type": tag,
                "internal": internal,
                "bullish": True,
                "bias": BULLISH,
                "anchor_index": piv_high.bar_index,
                "anchor_time": piv_high.bar_time,
                "confirmed_index": i,
                "confirmed_time": self.times[i],
                "level": piv_high.current_level,
            })

            # 创建 OB（仅 internal 默认开启）
            self.store_order_block(piv_high, i, internal, BULLISH)

        # Bearish cross (close 下穿 pivot.currentLevel)
        piv_low = self.internal_low if internal else self.swing_low
        extra_condition = (
            (piv_low.current_level != self.swing_low.current_level) and bearish_bar
            if internal else True
        )

        if (
            piv_low.current_level == piv_low.current_level  # not NaN
            and close_prev >= piv_low.current_level
            and close_curr < piv_low.current_level
            and not piv_low.crossed
            and extra_condition
        ):
            tag = "CHoCH" if trd.bias == BULLISH else "BOS"
            piv_low.crossed = True
            trd.bias = BEARISH

            self.events.append({
                "type": tag,
                "internal": internal,
                "bullish": False,
                "bias": BEARISH,
                "anchor_index": piv_low.bar_index,
                "anchor_time": piv_low.bar_time,
                "confirmed_index": i,
                "confirmed_time": self.times[i],
                "level": piv_low.current_level,
            })

            self.store_order_block(piv_low, i, internal, BEARISH)

    # ----- Order Block -----

    def store_order_block(
        self,
        piv: _Pivot,
        current_i: int,
        internal: bool,
        bias: int,
    ) -> None:
        """OB 创建：在 [piv.bar_index, current_i) 区间找 parsedHighs/parsedLows 极值。

        anchor = parsed_index (OB bar)，confirmed = current_i (BOS/CHoCH bar)。
        """
        if piv.bar_index is None:
            return
        if internal and not self.params["show_internal_order_blocks"]:
            return
        if (not internal) and not self.params["show_swing_order_blocks"]:
            return

        start = piv.bar_index
        end = current_i  # end-exclusive
        if end <= start:
            return

        if bias == BEARISH:
            arr = self.parsed_highs[start:end]
            if not arr:
                return
            local_idx = arr.index(max(arr))
        else:
            arr = self.parsed_lows[start:end]
            if not arr:
                return
            local_idx = arr.index(min(arr))

        parsed_index = start + local_idx
        ob = _OrderBlock(
            bar_high=float(self.parsed_highs[parsed_index]),
            bar_low=float(self.parsed_lows[parsed_index]),
            bar_time=self.times[parsed_index],
            bar_index=parsed_index,
            bias=bias,
            confirmed_index=current_i,
            confirmed_time=self.times[current_i],
        )
        target = self.internal_order_blocks if internal else self.swing_order_blocks
        # 维护最大 100 个（与 ref/smc.py 一致）
        if len(target) >= 100:
            target.pop()
        target.insert(0, ob)
        # 同步写入输出列表（一旦写入不可变，mitigation 状态后续更新）
        self.order_blocks_output.append({
            "internal": internal,
            "bias": bias,
            "anchor_index": parsed_index,
            "anchor_time": self.times[parsed_index],
            "confirmed_index": current_i,
            "confirmed_time": self.times[current_i],
            "bar_high": ob.bar_high,
            "bar_low": ob.bar_low,
            "mitigated": False,
            "mitigated_index": None,
            "mitigated_time": None,
            # 用 id 关联内部对象，便于 mitigation 后更新输出
            "_ob_ref": id(ob),
        })

    def delete_order_blocks(self, i: int, internal: bool = False) -> None:
        """OB mitigation：close 或 high/low 穿越 OB 边界。

        mitigation.confirmed = i (穿越 bar)。
        """
        obs = self.internal_order_blocks if internal else self.swing_order_blocks
        mitigation_src_high = (
            self.closes[i] if self.params["order_block_mitigation"] == CLOSE else self.highs[i]
        )
        mitigation_src_low = (
            self.closes[i] if self.params["order_block_mitigation"] == CLOSE else self.lows[i]
        )

        kept: list[_OrderBlock] = []
        for ob in obs:
            crossed = False
            if mitigation_src_high > ob.bar_high and ob.bias == BEARISH:
                crossed = True
            elif mitigation_src_low < ob.bar_low and ob.bias == BULLISH:
                crossed = True

            if crossed:
                # 更新内部对象 mitigation 状态
                ob.mitigated = True
                ob.mitigated_index = i
                ob.mitigated_time = self.times[i]
                # 同步更新输出列表中对应条目（通过 _ob_ref 关联）
                for out in self.order_blocks_output:
                    if out.get("_ob_ref") == id(ob):
                        out["mitigated"] = True
                        out["mitigated_index"] = i
                        out["mitigated_time"] = self.times[i]
                        break
            else:
                kept.append(ob)

        if internal:
            self.internal_order_blocks = kept
        else:
            self.swing_order_blocks = kept

    # ----- Trailing Strong/Weak -----

    def update_trailing_extremes(self, i: int) -> None:
        """更新 trailing strong/weak high/low。

        trailing.top/bottom 在 get_current_structure 中由 swing high/low 设置初始值，
        本方法在每个 bar 更新极值（high >= top 则更新 last_top_time）。
        """
        if self.trailing.bar_index is None:
            return
        if i >= self.n:
            return

        # NaN 检查
        if self.trailing.top != self.trailing.top or self.highs[i] >= self.trailing.top:
            self.trailing.top = self.highs[i]
            self.trailing.last_top_time = self.times[i]
        if self.trailing.bottom != self.trailing.bottom or self.lows[i] <= self.trailing.bottom:
            self.trailing.bottom = self.lows[i]
            self.trailing.last_bottom_time = self.times[i]

    # ----- 主循环 -----

    def run(self) -> None:
        """逐 bar 运行状态机。"""
        swings_length = self.params["swings_length"]
        equal_length = self.params["equal_length"]
        show_equal_hl = self.params["show_equal_hl"]
        show_internals = True  # internals 始终参与计算（用于 internal OB）
        show_internal_order_blocks = self.params["show_internal_order_blocks"]
        show_swing_order_blocks = self.params["show_swing_order_blocks"]

        for i in range(self.n):
            # 1. pivot 检测（swing + internal + equal）
            self.get_current_structure(i, swings_length, False, False)
            self.get_current_structure(i, 5, False, True)  # internal 用 size=5
            if show_equal_hl:
                self.get_current_structure(i, equal_length, True, False)

            # 2. BOS/CHoCH 检测（internal + swing）
            if show_internals or show_internal_order_blocks:
                self.display_structure(i, True)
            self.display_structure(i, False)

            # 3. trailing extremes 更新
            if self.trailing.bar_index is not None:
                self.update_trailing_extremes(i)

            # 4. OB mitigation（仅参与计算的 OB 类型）
            if show_internal_order_blocks:
                self.delete_order_blocks(i, True)
            if show_swing_order_blocks:
                self.delete_order_blocks(i, False)

        # 5. 清理输出列表中的 _ob_ref 内部字段
        for out in self.order_blocks_output:
            out.pop("_ob_ref", None)


# ===== 公开 API =====


def compute_smc_indicators(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    times: list[str],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """计算 SMC 指标（BOS/CHoCH/OB/EQH/EQL/trailing），完全排除 FVG。

    Args:
        opens: 开盘价序列
        highs: 最高价序列
        lows: 最低价序列
        closes: 收盘价序列
        times: ISO 格式时间字符串列表（与价格序列等长）
        params: 可选参数覆盖（默认使用 DEFAULT_PARAMS）

    Returns:
        dict 包含：
        - events: list[dict] BOS/CHoCH 事件
        - order_blocks: list[dict] 内部 OB（含 mitigation 状态）
        - equal_highs_lows: list[dict] EQH/EQL 事件
        - trailing: dict strong/weak high/low
        - pivots: list[dict] 所有 pivot 信息
        - time: list[str] 与输入对齐的时间字符串列表
        - params: dict 实际使用的参数

    Raises:
        ValueError: 输入长度不一致或为空
    """
    # 参数合并
    actual_params = {**DEFAULT_PARAMS, **(params or {})}

    # 输入校验
    n = len(closes)
    if not (len(opens) == len(highs) == len(lows) == len(times) == n):
        raise ValueError(
            f"输入序列长度不一致: opens={len(opens)} highs={len(highs)} "
            f"lows={len(lows)} closes={n} times={len(times)}"
        )
    if n == 0:
        return {
            "events": [],
            "order_blocks": [],
            "equal_highs_lows": [],
            "trailing": {
                "top": None,
                "bottom": None,
                "bar_time": None,
                "bar_index": None,
                "last_top_time": None,
                "last_bottom_time": None,
            },
            "pivots": [],
            "time": [],
            "params": actual_params,
        }

    # 不足 lookback 时返回空结果（避免 leg 检测异常）
    min_lookback = max(actual_params["swings_length"], 5) + 1
    if n < min_lookback:
        return {
            "events": [],
            "order_blocks": [],
            "equal_highs_lows": [],
            "trailing": {
                "top": None,
                "bottom": None,
                "bar_time": None,
                "bar_index": None,
                "last_top_time": None,
                "last_bottom_time": None,
            },
            "pivots": [],
            "time": list(times),
            "params": actual_params,
        }

    # 运行状态机
    state = _SMCState(opens, highs, lows, closes, times, actual_params)
    state.run()

    # 组装 trailing 输出（NaN → None）
    trailing = state.trailing
    trailing_out = {
        "top": float(trailing.top) if trailing.top == trailing.top else None,
        "bottom": float(trailing.bottom) if trailing.bottom == trailing.bottom else None,
        "bar_time": trailing.bar_time,
        "bar_index": trailing.bar_index,
        "last_top_time": trailing.last_top_time,
        "last_bottom_time": trailing.last_bottom_time,
    }

    return {
        "events": state.events,
        "order_blocks": state.order_blocks_output,
        "equal_highs_lows": state.equal_highs_lows,
        "trailing": trailing_out,
        "pivots": state.pivots,
        "time": list(times),
        "params": actual_params,
    }


# ===== 模块自测入口 =====

if __name__ == "__main__":
    # 自测入口：验证模块加载和基本计算（不连 DB/网络）
    import inspect

    # 1. 验证 compute_smc_indicators 函数签名
    assert callable(compute_smc_indicators), "compute_smc_indicators 应可调用"
    sig = inspect.signature(compute_smc_indicators)
    params = list(sig.parameters.keys())
    expected = ["opens", "highs", "lows", "closes", "times", "params"]
    assert params == expected, f"参数不匹配: {params} != {expected}"
    print(f"compute_smc_indicators params={params} OK")

    # 2. 验证默认参数
    assert DEFAULT_PARAMS["swings_length"] == 50, "swings_length 默认应为 50"
    assert DEFAULT_PARAMS["equal_length"] == 3, "equal_length 默认应为 3"
    assert DEFAULT_PARAMS["equal_threshold"] == 0.1, "equal_threshold 默认应为 0.1"
    assert DEFAULT_PARAMS["order_block_filter"] == ATR, "order_block_filter 默认应为 Atr"
    assert DEFAULT_PARAMS["order_block_mitigation"] == HIGHLOW, "order_block_mitigation 默认应为 High/Low"
    assert DEFAULT_PARAMS["show_internal_order_blocks"] is True, "show_internal_order_blocks 默认应为 True"
    assert DEFAULT_PARAMS["show_swing_order_blocks"] is False, "show_swing_order_blocks 默认应为 False"
    assert DEFAULT_PARAMS["show_equal_hl"] is True, "show_equal_hl 默认应为 True"
    assert DEFAULT_PARAMS["show_high_low_swings"] is True, "show_high_low_swings 默认应为 True"
    print(f"DEFAULT_PARAMS={DEFAULT_PARAMS} OK")

    # 3. 验证空数据
    empty_result = compute_smc_indicators([], [], [], [], [])
    assert empty_result["events"] == []
    assert empty_result["order_blocks"] == []
    assert empty_result["pivots"] == []
    assert empty_result["time"] == []
    print("空数据处理 OK")

    # 4. 验证不足 lookback
    short_result = compute_smc_indicators(
        [10.0, 11.0, 12.0],
        [10.5, 11.5, 12.5],
        [9.5, 10.5, 11.5],
        [11.0, 12.0, 13.0],
        ["2026-01-01", "2026-01-02", "2026-01-03"],
    )
    assert short_result["events"] == [], "不足 lookback 应返回空 events"
    assert short_result["time"] == ["2026-01-01", "2026-01-02", "2026-01-03"]
    print("不足 lookback 处理 OK")

    # 5. 验证 FVG 不计算、不返回、不显示
    # FVG 完全排除：输出中不含任何 FVG 相关键、事件或 box
    rng_state_pre = 42
    import random as _rng
    _rng.seed(rng_state_pre)
    n_pre = 120
    base_pre = 10.0
    closes_pre = [base_pre]
    for _ in range(n_pre - 1):
        base_pre += _rng.uniform(-0.5, 0.5)
        closes_pre.append(base_pre)
    opens_pre = [c + _rng.uniform(-0.2, 0.2) for c in closes_pre]
    highs_pre = [max(o, c) + _rng.uniform(0.1, 0.5) for o, c in zip(opens_pre, closes_pre, strict=True)]
    lows_pre = [min(o, c) - _rng.uniform(0.1, 0.5) for o, c in zip(opens_pre, closes_pre, strict=True)]
    times_pre = [f"2026-01-{i+1:02d}" for i in range(n_pre)]
    pre_result = compute_smc_indicators(opens_pre, highs_pre, lows_pre, closes_pre, times_pre)
    # 输出键不含 FVG
    for _key in pre_result:
        assert "fvg" not in str(_key).lower(), f"输出键不得包含 FVG: {_key}"
    # 事件类型不含 FVG
    for _ev in pre_result["events"]:
        assert "FVG" not in str(_ev.get("type", "")).upper(), f"事件不得包含 FVG: {_ev}"
    print("FVG 不计算、不返回、不显示 OK")

    # 6. 验证小样本计算不抛异常
    rng_state = 42
    import random
    random.seed(rng_state)
    n_test = 100
    base = 10.0
    closes_t = [base]
    for _ in range(n_test - 1):
        base += random.uniform(-0.5, 0.5)
        closes_t.append(base)
    opens_t = [c + random.uniform(-0.2, 0.2) for c in closes_t]
    highs_t = [max(o, c) + random.uniform(0.1, 0.5) for o, c in zip(opens_t, closes_t, strict=True)]
    lows_t = [min(o, c) - random.uniform(0.1, 0.5) for o, c in zip(opens_t, closes_t, strict=True)]
    times_t = [f"2026-01-{i+1:02d}" for i in range(n_test)]

    result = compute_smc_indicators(opens_t, highs_t, lows_t, closes_t, times_t)
    assert "events" in result
    assert "order_blocks" in result
    assert "equal_highs_lows" in result
    assert "trailing" in result
    assert "pivots" in result
    assert "time" in result
    assert "params" in result
    assert result["params"]["swings_length"] == 50
    # 验证所有事件都有 anchor/confirmed 字段
    for ev in result["events"]:
        assert "anchor_index" in ev and "confirmed_index" in ev
        assert "anchor_time" in ev and "confirmed_time" in ev
    for ob in result["order_blocks"]:
        assert "anchor_index" in ob and "confirmed_index" in ob
        assert "anchor_time" in ob and "confirmed_time" in ob
        assert "mitigated" in ob
    for eq in result["equal_highs_lows"]:
        assert "anchor_index" in eq and "confirmed_index" in eq
    for piv in result["pivots"]:
        assert "anchor_index" in piv and "confirmed_index" in piv
    print(f"小样本计算 OK (n={n_test}, events={len(result['events'])}, "
          f"obs={len(result['order_blocks'])}, pivots={len(result['pivots'])})")

    print("OK")
