"""SMC Pine 语义核心模块 — 唯一纯函数核心。

本模块实现用户 Pine SMC 源码（`ref/smc_user_source.pine`，SHA256 0bd3d2ad，843 行，
用户原创并授权盘迹商业项目使用）的 Pine 语义原语，作为生产服务和测试参考的唯一调用入口。
用户 Pine 文件为 SMC 算法唯一真源；禁止维护两套近似算法；禁止读取或引用任何
第三方 LuxAlgo Pine 源码。

Pine 语义原语实现：
    - ta.rma(src, length): Wilder's Running Moving Average
    - ta.atr(n): ta.rma(ta.tr, n)
    - ta.cum(src) / bar_index: Cumulative Mean Range（bar 0 = NaN）
    - ta.highest/ta.lowest: 滚动极值
    - ta.crossover/ta.crossunder: 穿越检测
    - array push/unshift/pop/slice: OB 列表管理
    - Pine 逐 bar 持久状态和完全相同的函数调用顺序

默认参数（逐项匹配用户 Pine 代码）：
    Historical, Colored;
    internal structure=true, size=5, All, confluence=false, tiny;
    swing structure=true, length=50, All, small;
    Strong/Weak=true;
    internal OB=true, 5, ATR, High/Low;
    swing OB=false;
    EQH/EQL=true, bars confirmation=3, threshold=0.1, tiny;
    swing points/MTF/premium-discount=false.

FVG 完全排除：
    Fair Value Gap 不计算、不返回、不缓存、不渲染，也不暴露 FVG 开关。
    生产计算路径不包含 FVG 函数或状态。
    FVG 排除不改变其他逻辑的索引、执行顺序和右侧延伸。

anchor/confirmed 因果契约：
    每个 pivot/事件同时返回 anchor_index/anchor_time 与 confirmed_index/confirmed_time。
    - pivot.anchor = ref_i (i-size)，pivot.confirmed = i (leg change 确认 bar)
    - BOS/CHoCH.anchor = pivot.barIndex (被穿越的 pivot bar)
      BOS/CHoCH.confirmed = i (close 穿越 pivot 的 bar)
    - OB.anchor = parsed_index (OB bar)
      OB.confirmed = current_i (触发 OB 创建的 BOS/CHoCH bar)
    - EQH/EQL.anchor = prev piv.barIndex (前一 pivot)
      EQH/EQL.second_pivot = ref_i (i-size, 新 pivot 所在 bar)
      EQH/EQL.confirmed = i (leg change 检测 bar, 因果确认点)
    - Mitigation.confirmed = i (close/high/low 穿越 OB 的 bar)
    未来 bar 不得修改已确认事件（事件一旦写入即不可变）。

warmup 契约：
    计算必须包含足够 warmup，至少展示区之前 500 根。
    可获得时使用完整历史计算后裁剪输出。
    不得只用当前可见 bars 初始化状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# ===== 常量 =====
BULLISH = 1
BEARISH = -1
BULLISH_LEG = 1
BEARISH_LEG = 0

ATR = "Atr"
RANGE = "Cumulative Mean Range"
CLOSE = "Close"
HIGHLOW = "High/Low"

# ===== 默认参数（严格匹配原始 Pine）=====
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
    # [CHANGE-20260717-001 Pine parity] execution gate 参数（Pine L784/L787）
    # internal gate: showInternals OR showInternalOrderBlocks OR showTrend
    # swing gate: showStructure OR showSwingOrderBlocks OR showHighLowSwings
    "show_internals": True,
    "show_structure": True,
    "show_trend": True,
}


# ===== Pine 语义原语 =====


def pine_rma(src: list[float], length: int) -> list[float]:
    """Pine ta.rma(src, length) — Wilder's Running Moving Average。

    CHANGE-20260715-006: 严格复现 Pine v5 ta.rma 的 NA 和 SMA 种子语义。
    Pine v5 ta.rma 在 bar_index < length-1 时返回 na（不是逐步 SMA），
    在 bar_index == length-1 时写入 SMA(src, length) 作为种子，
    之后使用 Wilder 递推 rma[i] = (rma[i-1] * (length-1) + src[i]) / length。

    语义：
        - bar_index < length-1: na（数据不足以计算 SMA 种子）
        - bar_index == length-1: SMA(src, length) 种子
        - bar_index >= length: Wilder 递推

    旧实现错误地在 bar_index < length-1 时返回逐步 SMA（min_periods 行为），
    导致 ATR(200) 在前 199 根产生非 na 值，与 Pine v5 不一致。
    """
    n = len(src)
    if n == 0 or length <= 0:
        return [float("nan")] * n

    result = [float("nan")] * n

    # 前 length-1 根：na（Pine v5 ta.rma 语义）
    # 第 length-1 根：SMA 种子
    if n >= length:
        if length == 1:
            result[0] = src[0]
        else:
            sma_seed = sum(src[:length]) / length
            result[length - 1] = sma_seed

        # 之后：Wilder 递推
        alpha = 1.0 / length
        for i in range(length, n):
            result[i] = alpha * src[i] + (1.0 - alpha) * result[i - 1]

    return result


def pine_true_range(
    highs: list[float], lows: list[float], closes: list[float]
) -> list[float]:
    """Pine ta.tr — True Range。"""
    n = len(highs)
    if n == 0:
        return []
    tr = [0.0] * n
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        prev_close = closes[i - 1]
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
    return tr


def pine_atr(
    highs: list[float], lows: list[float], closes: list[float], length: int
) -> list[float]:
    """Pine ta.atr(length) = ta.rma(ta.tr, length)。"""
    tr = pine_true_range(highs, lows, closes)
    return pine_rma(tr, length)


def pine_cumulative_mean_range(
    highs: list[float], lows: list[float], closes: list[float]
) -> list[float]:
    """Pine ta.cum(ta.tr) / bar_index — Cumulative Mean Range。

    Pine 语义：
        - bar 0: tr[0] / 0 = NaN（除零）
        - bar i (i>0): sum(tr[0..i]) / i
    """
    tr = pine_true_range(highs, lows, closes)
    n = len(tr)
    if n == 0:
        return []
    result = [float("nan")] * n
    cumsum = 0.0
    for i in range(n):
        cumsum += tr[i]
        if i > 0:
            result[i] = cumsum / i
    return result


def pine_highest(src: list[float], length: int, ref_i: int) -> float:
    """Pine ta.highest(src, length) 在 ref_i 之后窗口的 max。

    对应 Pine: high[size] > ta.highest(size)
    窗口是 ref_i 之后直到当前 bar 的 length 根，即 [ref_i+1, ref_i+length]。
    """
    start = max(0, ref_i + 1)
    end = min(len(src), ref_i + length + 1)
    if start >= end:
        return src[ref_i] if 0 <= ref_i < len(src) else float("nan")
    return max(src[start:end])


def pine_lowest(src: list[float], length: int, ref_i: int) -> float:
    """Pine ta.lowest(src, length) 在 ref_i 之后窗口的 min。"""
    start = max(0, ref_i + 1)
    end = min(len(src), ref_i + length + 1)
    if start >= end:
        return src[ref_i] if 0 <= ref_i < len(src) else float("nan")
    return min(src[start:end])


def pine_crossover(a_curr: float, a_prev: float, b_curr: float, b_prev: float) -> bool:
    """Pine ta.crossover(a, b) = a[0] > b[0] and a[1] <= b[1]。"""
    return a_curr > b_curr and a_prev <= b_prev


def pine_crossunder(a_curr: float, a_prev: float, b_curr: float, b_prev: float) -> bool:
    """Pine ta.crossunder(a, b) = a[0] < b[0] and a[1] >= b[1]。"""
    return a_curr < b_curr and a_prev >= b_prev


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


class _SMCPineState:
    """SMC 逐 bar 状态机（Pine 语义核心）。

    使用 Python 原生数据结构，无外部依赖。
    所有 Pine 语义原语通过 smc_pine_core 模块函数调用。
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

        # Pine 语义波动率指标
        self.tr = pine_true_range(highs, lows, closes)
        self.atr200 = pine_rma(self.tr, 200)  # ta.atr(200) = ta.rma(tr, 200)
        self.cmr = pine_cumulative_mean_range(highs, lows, closes)
        self.volatility_measure = (
            self.atr200
            if params["order_block_filter"] == ATR
            else self.cmr
        )
        self.parsed_highs, self.parsed_lows = self._compute_parsed_high_low()

        # pivot 状态
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

        # order blocks
        self.internal_order_blocks: list[_OrderBlock] = []
        self.swing_order_blocks: list[_OrderBlock] = []

        # 事件输出列表（一旦写入不可变）
        self.events: list[dict[str, Any]] = []
        self.equal_highs_lows: list[dict[str, Any]] = []
        self.pivots: list[dict[str, Any]] = []
        self.order_blocks_output: list[dict[str, Any]] = []

        # leg 状态缓存
        self.leg_states: dict[tuple[str, int], dict[int, int]] = {}

    def _compute_parsed_high_low(self) -> tuple[list[float], list[float]]:
        """计算 parsedHigh/parsedLow（高波动 bar 互换 high/low）。

        Pine: highVolatilityBar = (high - low) >= 2.0 * volatilityMeasure
        parsedHigh = low if highVolatilityBar else high
        parsedLow = high if highVolatilityBar else low
        """
        n = self.n
        parsed_high = [0.0] * n
        parsed_low = [0.0] * n
        for i in range(n):
            vol = self.volatility_measure[i]
            # vol 可能是 NaN（CMR bar 0），此时 highVolatilityBar=False
            if vol != vol:  # NaN check
                high_vol_bar = False
            else:
                high_vol_bar = (self.highs[i] - self.lows[i]) >= 2.0 * vol
            if high_vol_bar:
                parsed_high[i] = self.lows[i]
                parsed_low[i] = self.highs[i]
            else:
                parsed_high[i] = self.highs[i]
                parsed_low[i] = self.lows[i]
        return parsed_high, parsed_low

    # ----- leg 检测（Pine 语义）-----

    def leg(self, i: int, size: int, lane: str) -> int:
        """逐 bar leg 检测（Pine 语义）。

        new_leg_high = highs[ref_i] > pine_highest(highs, size, ref_i)
        new_leg_low = lows[ref_i] < pine_lowest(lows, size, ref_i)
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
        new_leg_high = self.highs[ref_i] > pine_highest(self.highs, size, ref_i)
        new_leg_low = self.lows[ref_i] < pine_lowest(self.lows, size, ref_i)
        out = prev
        if new_leg_high:
            out = BEARISH_LEG
        elif new_leg_low:
            out = BULLISH_LEG
        state_map[i] = out
        return out

    def start_of_new_leg(self, i: int, size: int, lane: str) -> bool:
        # CHANGE-20260715-006: i >= size（非 i > size），首个 leg/pivot 在 i==size 检测
        # Pine: ta.change(leg) 在 bar_index==size 时可首次非零（leg 从 0 变为 0/1）
        return i >= size and self.leg(i, size, lane) != self.leg(i - 1, size, lane)

    def start_of_bearish_leg(self, i: int, size: int, lane: str) -> bool:
        # CHANGE-20260715-006: i >= size（非 i > size），首个 pivot 在 i==size 检测
        return i >= size and (self.leg(i, size, lane) - self.leg(i - 1, size, lane) == -1)

    def start_of_bullish_leg(self, i: int, size: int, lane: str) -> bool:
        # CHANGE-20260715-006: i >= size（非 i > size），首个 pivot 在 i==size 检测
        return i >= size and (self.leg(i, size, lane) - self.leg(i - 1, size, lane) == 1)

    # ----- pivot 检测（含 EQH/EQL）-----

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
        CHANGE-20260715-006: i < size（非 i <= size），首个 pivot 在 i==size 检测。
        """
        if i < size:
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

            # EQH/EQL 检测
            if (
                equal_high_low
                and piv.current_level == piv.current_level  # not NaN
                and atr_measure == atr_measure  # not NaN
                and abs(piv.current_level - level) < self.params["equal_threshold"] * atr_measure
            ):
                self.equal_highs_lows.append({
                    "type": "EQL",
                    "anchor_index": piv.bar_index,
                    "anchor_time": piv.bar_time,
                    # CHANGE-20260715-007: ref_i 是新 pivot 所在 bar，命名为 second_pivot
                    "second_pivot_index": ref_i,
                    "second_pivot_time": self.times[ref_i],
                    # CHANGE-20260715-007: i 是 leg change 检测 bar，命名为 confirmed（因果确认点）
                    "confirmed_index": i,
                    "confirmed_time": self.times[i],
                    "level": level,
                    "prev_level": piv.current_level,
                })

            self._record_pivot(piv, level, ref_i, i, "low", internal, equal_high_low)

            piv.last_level = piv.current_level
            piv.current_level = level
            piv.crossed = False
            piv.bar_time = self.times[ref_i]
            piv.bar_index = ref_i

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
                and piv.current_level == piv.current_level
                and atr_measure == atr_measure
                and abs(piv.current_level - level) < self.params["equal_threshold"] * atr_measure
            ):
                self.equal_highs_lows.append({
                    "type": "EQH",
                    "anchor_index": piv.bar_index,
                    "anchor_time": piv.bar_time,
                    # CHANGE-20260715-007: ref_i 是新 pivot 所在 bar，命名为 second_pivot
                    "second_pivot_index": ref_i,
                    "second_pivot_time": self.times[ref_i],
                    # CHANGE-20260715-007: i 是 leg change 检测 bar，命名为 confirmed（因果确认点）
                    "confirmed_index": i,
                    "confirmed_time": self.times[i],
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

    # ----- BOS/CHoCH 检测（Pine crossover/crossunder）-----

    def display_structure(
        self,
        i: int,
        internal: bool = False,
        prev_levels: dict[str, float] | None = None,
    ) -> None:
        """BOS/CHoCH 检测（close crossover/crossunder pivot level）。

        anchor = pivot.barIndex (被穿越的 pivot bar)
        confirmed = i (close 穿越的 bar)

        Pine ta.crossover(close, p_ivot.currentLevel) 使用：
            close[0] > currentLevel[0]  (当前 bar close vs 当前 bar pivot level)
            close[1] <= currentLevel[1]  (上一 bar close vs 上一 bar pivot level)

        prev_levels 是在当前 bar 开始时（getCurrentStructure 之前）快照的 pivot level，
        代表上一 bar 结束时的 currentLevel。如果为 None 则回退到当前 level（旧行为）。
        NaN 作为 prev_level 时 crossover/crossunder 结果为 False（Pine 语义）。
        """
        if i <= 0 or i >= self.n:
            return

        close_prev = self.closes[i - 1]
        close_curr = self.closes[i]

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
        # CHANGE-20260716-001: Pine ta.crossover 需要 currentLevel[0] 和 currentLevel[1]。
        # currentLevel[0] = getCurrentStructure 更新后的当前 level
        # currentLevel[1] = 上一 bar 结束时的 level（prev_levels 快照）
        level_curr_high = piv_high.current_level
        level_prev_high = (
            prev_levels["internal_high" if internal else "swing_high"]
            if prev_levels is not None
            else level_curr_high
        )
        extra_condition = (
            (piv_high.current_level != self.swing_high.current_level) and bullish_bar
            if internal else True
        )

        if (
            piv_high.current_level == piv_high.current_level  # not NaN
            and pine_crossover(close_curr, close_prev, level_curr_high, level_prev_high)
            and not piv_high.crossed
            and extra_condition
        ):
            tag = "CHoCH" if trd.bias == BEARISH else "BOS"
            piv_high.crossed = True
            trd.bias = BULLISH

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

            self.store_order_block(piv_high, i, internal, BULLISH)

        # Bearish cross (close 下穿 pivot.currentLevel)
        piv_low = self.internal_low if internal else self.swing_low
        level_curr_low = piv_low.current_level
        level_prev_low = (
            prev_levels["internal_low" if internal else "swing_low"]
            if prev_levels is not None
            else level_curr_low
        )
        extra_condition = (
            (piv_low.current_level != self.swing_low.current_level) and bearish_bar
            if internal else True
        )

        if (
            piv_low.current_level == piv_low.current_level
            and pine_crossunder(close_curr, close_prev, level_curr_low, level_prev_low)
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

    # ----- Order Block（Pine array push/pop/slice）-----

    def store_order_block(
        self,
        piv: _Pivot,
        current_i: int,
        internal: bool,
        bias: int,
    ) -> None:
        """OB 创建：在 [piv.bar_index, current_i) 区间找 parsedHighs/parsedLows 极值。

        Pine: array.push(orderBlocks, 0, OB)，array.pop if > 100。
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
        # Pine: array.push(arr, 0, item) 头部插入; array.pop if > 100
        if len(target) >= 100:
            target.pop()
        target.insert(0, ob)
        # [CHANGE-20260717-001 Pine parity] order_blocks_output 也保持 newest-first
        # Pine array.unshift 语义：最新 OB 在数组头部，前端 slice(0,5) 取最新 5 个
        # 旧实现用 append（oldest-first），前端 slice(0,5) 取最旧 5 个，违反 Pine 语义
        self.order_blocks_output.insert(0, {
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
            "_ob_ref": id(ob),
        })

    def delete_order_blocks(self, i: int, internal: bool = False) -> None:
        """OB mitigation（Pine 语义）。

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
                ob.mitigated = True
                ob.mitigated_index = i
                ob.mitigated_time = self.times[i]
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
        """更新 trailing strong/weak high/low（Pine 语义）。

        [CHANGE-20260717-001 Pine parity] 严格复刻 Pine L706-709:
            trailing.top := math.max(high, trailing.top)
            trailing.bottom := math.min(low, trailing.bottom)

        Pine math.max(high, na) = na：trailing.top 为 NaN 时不会被当前 high 凭空初始化。
        初始化只在 get_current_structure 检测到新 swing pivot 时发生（L454-458/L426-429）。
        旧实现用 `self.trailing.top != self.trailing.top or self.highs[i] >= self.trailing.top`
        在 NaN 时用 self.highs[i] 初始化，违反 Pine 语义。
        """
        if self.trailing.bar_index is None:
            return
        if i >= self.n:
            return

        # Pine 语义：math.max(high, na) = na；只有 trailing.top 非 NaN 时才更新
        if self.trailing.top == self.trailing.top:  # not NaN
            if self.highs[i] >= self.trailing.top:
                self.trailing.top = self.highs[i]
                self.trailing.last_top_time = self.times[i]
        if self.trailing.bottom == self.trailing.bottom:  # not NaN
            if self.lows[i] <= self.trailing.bottom:
                self.trailing.bottom = self.lows[i]
                self.trailing.last_bottom_time = self.times[i]

    # ----- 主循环（Pine 执行顺序）-----

    def run(self) -> None:
        """逐 bar 运行状态机（Pine 执行顺序）。

        Pine lines 766-807 的固定顺序：
        1. update_trailing_extremes (FIRST, 在任何 getCurrentStructure 之前)
        2. get_current_structure(swing, length=50)
        3. get_current_structure(internal, length=5)
        4. get_current_structure(equal, length=3) if show_equal_hl
        5. display_structure(internal) if show_internals or show_internal_order_blocks
        6. display_structure(swing)
        7. delete_order_blocks(internal) if show_internal_order_blocks
        8. delete_order_blocks(swing) if show_swing_order_blocks

        trailing 必须在最前面：Pine 中 updateTrailingExtremes 用当前 bar 的 high/low
        更新 trailing.top/bottom，然后 getCurrentStructure 检测到新 swing pivot 时会
        覆盖 trailing.top/bottom 为新 pivot level。若顺序颠倒，trailing 会被当前 bar
        的 high/low 二次覆盖，与 Pine 不一致。详见 docs/analysis/smc-user-pine-parity.md 5.3。
        """
        swings_length = self.params["swings_length"]
        equal_length = self.params["equal_length"]
        show_equal_hl = self.params["show_equal_hl"]
        show_high_low_swings = self.params["show_high_low_swings"]
        show_internal_order_blocks = self.params["show_internal_order_blocks"]
        show_swing_order_blocks = self.params["show_swing_order_blocks"]
        # [CHANGE-20260717-001 Pine parity] execution gate 参数（Pine L784/L787）
        show_internals = self.params.get("show_internals", True)
        show_structure = self.params.get("show_structure", True)
        show_trend = self.params.get("show_trend", True)
        # Pine L784: internal gate = showInternals OR showInternalOrderBlocks OR showTrend
        internal_gate = show_internals or show_internal_order_blocks or show_trend
        # Pine L787: swing gate = showStructure OR showSwingOrderBlocks OR showHighLowSwings
        swing_gate = show_structure or show_swing_order_blocks or show_high_low_swings

        for i in range(self.n):
            # 0. 快照上一 bar 结束时的 pivot level（Pine currentLevel[1]）
            # CHANGE-20260716-001: ta.crossover/crossunder 需要 currentLevel[0] 和 [1]。
            # [0] = getCurrentStructure 更新后的当前值；[1] = 上一 bar 最终值。
            # 必须在任何 getCurrentStructure 之前快照，否则新 pivot 会覆盖旧值。
            prev_levels: dict[str, float] = {
                "swing_high": self.swing_high.current_level,
                "swing_low": self.swing_low.current_level,
                "internal_high": self.internal_high.current_level,
                "internal_low": self.internal_low.current_level,
            }

            # 1. trailing 极值更新（Pine: 在 getCurrentStructure 之前）
            # Pine 条件: if showHighLowSwingsInput or showPremiumDiscountZonesInput
            # show_high_low_swings 默认 true，show_premium_discount_zones 默认 false（不实现）
            if show_high_low_swings and self.trailing.bar_index is not None:
                self.update_trailing_extremes(i)

            # 2. swing pivot (size=swings_length=50)
            self.get_current_structure(i, swings_length, False, False)
            # 3. internal pivot (size=5)
            self.get_current_structure(i, 5, False, True)
            # 4. equal H/L pivot (size=equal_length=3)
            if show_equal_hl:
                self.get_current_structure(i, equal_length, True, False)

            # 5. internal BOS/CHoCH + OB 创建
            # [CHANGE-20260717-001 Pine parity] Pine L784: showInternals OR showInternalOB OR showTrend
            if internal_gate:
                self.display_structure(i, True, prev_levels)
            # 6. swing BOS/CHoCH + OB 创建
            # [CHANGE-20260717-001 Pine parity] Pine L787: showStructure OR showSwingOB OR showHighLowSwings
            if swing_gate:
                self.display_structure(i, False, prev_levels)

            # 7. internal OB mitigation
            if show_internal_order_blocks:
                self.delete_order_blocks(i, True)
            # 8. swing OB mitigation
            if show_swing_order_blocks:
                self.delete_order_blocks(i, False)

        # 清理内部字段
        for out in self.order_blocks_output:
            out.pop("_ob_ref", None)


# ===== 公开 API =====


def compute_smc_pine(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    times: list[str],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """计算 SMC 指标（Pine 语义核心），完全排除 FVG。

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
        - order_blocks: list[dict] OB（含 mitigation 状态）
        - equal_highs_lows: list[dict] EQH/EQL 事件
        - trailing: dict strong/weak high/low
        - pivots: list[dict] 所有 pivot 信息
        - time: list[str] 与输入对齐的时间字符串列表
        - params: dict 实际使用的参数

    Raises:
        ValueError: 输入长度不一致或为空
    """
    actual_params = {**DEFAULT_PARAMS, **(params or {})}

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
            "swing_bias": 0,
            "pivots": [],
            "time": [],
            "params": actual_params,
        }

    state = _SMCPineState(opens, highs, lows, closes, times, actual_params)
    state.run()

    return {
        "events": state.events,
        "order_blocks": state.order_blocks_output,
        "equal_highs_lows": state.equal_highs_lows,
        "trailing": {
            "top": state.trailing.top if state.trailing.top == state.trailing.top else None,
            "bottom": state.trailing.bottom if state.trailing.bottom == state.trailing.bottom else None,
            "bar_time": state.trailing.bar_time,
            "bar_index": state.trailing.bar_index,
            "last_top_time": state.trailing.last_top_time,
            "last_bottom_time": state.trailing.last_bottom_time,
        },
        # CHANGE-20260715-007: swing_bias 直接透传 Pine 核心 swing_trend.bias
        # 取值：1=bullish, -1=bearish, 0=尚未形成趋势
        # 前端规则：bias===-1 → Strong High（否则 Weak High）；bias===1 → Strong Low（否则 Weak Low）
        # 禁止根据 trailing 时间、close 位置或最后一个可见事件重新推断
        "swing_bias": state.swing_trend.bias,
        "pivots": state.pivots,
        "time": list(times),
        "params": actual_params,
    }
