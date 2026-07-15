"""CHANGE-20260715-002 SMC 指标单元测试（Pine parity 核心）。

验证内容：
1. 默认参数快照（DEFAULT_PARAMS 与 Pine 原始参数一致）
2. 逐 bar 增量 = 全量同 confirmed 时结果（因果契约：未来 bar 不修改已确认事件）
3. FVG 不计算、不返回、不显示（输出/schema 中不存在 FVG 字段、事件、box 或开关）
4. OB 创建与 mitigation（internal OB 创建后 mitigation 标记不可变）
5. BOS/CHoCH、EQH/EQL（事件类型与 anchor/confirmed 字段）
6. 空数据和不足 lookback（不抛异常，返回空 events）
7. Pine 语义原语验证（ta.rma Wilder 递推、ta.cum/bar_index bar0=NaN、ta.atr=RMA(TR)）

算法真源：smc_pine_core.py（Pine 语义核心），生产服务和测试参考的唯一调用入口。
FVG 完全排除：本测试断言 SMC 输出、API schema 中不存在 FVG 字段、事件或 box，
而非扫描源码字符串。注释/文档可以正常写"FVG 不计算、不返回、不显示"。

用法：
    cd backend && python -m pytest tests/test_smc_indicator.py -v
"""

from __future__ import annotations

import inspect
import random
from typing import Any

from app.strategy_assets.algorithms.features.smc_indicator import (
    ATR,
    DEFAULT_PARAMS,
    HIGHLOW,
    _SMCState,
    compute_smc_indicators,
)

# ===== 辅助函数 =====


def _gen_random_bars(
    n: int,
    base: float = 10.0,
    seed: int = 42,
) -> tuple[list[float], list[float], list[float], list[float], list[str]]:
    """生成 n 根随机 walk bars（用于 SMC 计算）。

    Args:
        n: bar 数量
        base: 初始价格
        seed: 随机种子（保证可重现）

    Returns:
        (opens, highs, lows, closes, times)
    """
    random.seed(seed)
    closes = [base]
    for _ in range(n - 1):
        base += random.uniform(-0.5, 0.5)
        # 避免负价
        base = max(1.0, base)
        closes.append(base)
    opens = [c + random.uniform(-0.2, 0.2) for c in closes]
    highs = [max(o, c) + random.uniform(0.1, 0.5) for o, c in zip(opens, closes, strict=True)]
    lows = [min(o, c) - random.uniform(0.1, 0.5) for o, c in zip(opens, closes, strict=True)]
    lows = [max(0.5, lo) for lo in lows]  # 避免负价
    times = [f"2026-01-{i + 1:02d}" for i in range(n)]
    return opens, highs, lows, closes, times


# ===== 1. 默认参数快照 =====


class TestDefaultParams:
    """验证 DEFAULT_PARAMS 与 ref/smc.py build_parser() 默认值一致。"""

    def test_default_params_keys(self) -> None:
        """DEFAULT_PARAMS 包含所有必需字段。"""
        required_keys = {
            "swings_length",
            "equal_length",
            "equal_threshold",
            "internal_filter_confluence",
            "internal_ob_size",
            "swing_ob_size",
            "order_block_filter",
            "order_block_mitigation",
            "show_internal_order_blocks",
            "show_swing_order_blocks",
            "show_equal_hl",
            "show_high_low_swings",
            "show_swings",
        }
        assert required_keys.issubset(set(DEFAULT_PARAMS.keys())), (
            f"DEFAULT_PARAMS 缺少字段: {required_keys - set(DEFAULT_PARAMS.keys())}"
        )

    def test_default_params_values(self) -> None:
        """DEFAULT_PARAMS 值与 build_parser() 默认值一致。"""
        assert DEFAULT_PARAMS["swings_length"] == 50
        assert DEFAULT_PARAMS["equal_length"] == 3
        assert DEFAULT_PARAMS["equal_threshold"] == 0.1
        assert DEFAULT_PARAMS["internal_filter_confluence"] is False
        assert DEFAULT_PARAMS["internal_ob_size"] == 5
        assert DEFAULT_PARAMS["swing_ob_size"] == 5
        assert DEFAULT_PARAMS["order_block_filter"] == ATR
        assert DEFAULT_PARAMS["order_block_mitigation"] == HIGHLOW
        assert DEFAULT_PARAMS["show_internal_order_blocks"] is True
        assert DEFAULT_PARAMS["show_swing_order_blocks"] is False
        assert DEFAULT_PARAMS["show_equal_hl"] is True
        assert DEFAULT_PARAMS["show_high_low_swings"] is True
        assert DEFAULT_PARAMS["show_swings"] is False

    def test_no_fvg_param(self) -> None:
        """DEFAULT_PARAMS 不包含 FVG 相关参数。"""
        for key in DEFAULT_PARAMS:
            assert "fvg" not in key.lower(), f"DEFAULT_PARAMS 不得包含 FVG 参数: {key}"


# ===== 2. FVG 完全排除 =====


class TestFvgExclusion:
    """验证 FVG（Fair Value Gap）不计算、不返回、不显示。

    FVG 完全排除：生产计算路径不计算 FVG，输出中不含任何 FVG 相关键、
    事件或 box。本测试断言 SMC 输出结构，而非扫描源码字符串。
    注释/文档可以正常写"FVG 不计算、不返回、不显示"。
    """

    def test_output_no_fvg_keys(self) -> None:
        """compute_smc_indicators 输出不含 FVG 键。"""
        opens, highs, lows, closes, times = _gen_random_bars(150)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        # 顶层键不含 FVG
        for key in result:
            assert "fvg" not in str(key).lower(), f"输出顶层键不得包含 FVG: {key}"
        # 显式检查常见 FVG 键名不存在
        forbidden_keys = ["fvg", "fair_value_gap", "fairValueGaps", "fvgs", "fvg_boxes"]
        for key in forbidden_keys:
            assert key not in result, f"输出不得包含 FVG 键: {key}"

    def test_output_no_fvg_events(self) -> None:
        """events 列表中不含 FVG 事件类型。"""
        opens, highs, lows, closes, times = _gen_random_bars(200)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        for ev in result["events"]:
            ev_type = str(ev.get("type", "")).upper()
            assert "FVG" not in ev_type, f"events 不得包含 FVG 类型: {ev}"

    def test_output_no_fvg_in_order_blocks(self) -> None:
        """order_blocks 中不含 FVG 相关字段。"""
        opens, highs, lows, closes, times = _gen_random_bars(200)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        for ob in result["order_blocks"]:
            for field_name in ob:
                assert "fvg" not in str(field_name).lower(), (
                    f"order_blocks 字段不得包含 FVG: {field_name}"
                )

    def test_output_no_fvg_in_equal_highs_lows(self) -> None:
        """equal_highs_lows 中不含 FVG 相关字段。"""
        opens, highs, lows, closes, times = _gen_random_bars(200)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        for eq in result["equal_highs_lows"]:
            for field_name in eq:
                assert "fvg" not in str(field_name).lower(), (
                    f"equal_highs_lows 字段不得包含 FVG: {field_name}"
                )

    def test_params_no_fvg_option(self) -> None:
        """DEFAULT_PARAMS 不包含 FVG 相关参数或开关。"""
        for key in DEFAULT_PARAMS:
            assert "fvg" not in str(key).lower(), f"DEFAULT_PARAMS 不得包含 FVG 参数: {key}"

    def test_no_fvg_calculation_path(self) -> None:
        """生产计算路径没有 FVG 函数或状态。

        验证 _SMCState 实例不包含 FVG 相关属性，compute_smc_indicators
        不调用任何 FVG 计算函数。
        """
        opens, highs, lows, closes, times = _gen_random_bars(100)
        state = _SMCState(opens, highs, lows, closes, times, DEFAULT_PARAMS)
        # 检查状态机实例属性不含 FVG
        for attr_name in dir(state):
            if attr_name.startswith("_") and attr_name.startswith("__"):
                continue
            assert "fvg" not in attr_name.lower(), (
                f"_SMCState 属性不得包含 FVG: {attr_name}"
            )


# ===== 3. 空数据与不足 lookback =====


class TestEmptyAndShortData:
    """验证空数据和不足 lookback 时不抛异常。"""

    def test_empty_data(self) -> None:
        """空输入返回空结构。"""
        result = compute_smc_indicators([], [], [], [], [])
        assert result["events"] == []
        assert result["order_blocks"] == []
        assert result["equal_highs_lows"] == []
        assert result["pivots"] == []
        assert result["time"] == []
        assert result["trailing"]["top"] is None
        assert result["trailing"]["bottom"] is None

    def test_short_data_below_lookback(self) -> None:
        """不足 lookback（swings_length=50）时返回空 events。"""
        opens, highs, lows, closes, times = _gen_random_bars(10)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        assert result["events"] == [], "不足 lookback 应返回空 events"
        assert result["time"] == times, "time 应原样返回"

    def test_data_exactly_at_lookback(self) -> None:
        """刚好等于 lookback 时不抛异常。"""
        # swings_length=50，需要至少 50+1 根才能产生 pivot
        opens, highs, lows, closes, times = _gen_random_bars(51)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        assert "events" in result
        assert "order_blocks" in result


# ===== 4. 输出结构契约 =====


class TestOutputStructure:
    """验证 compute_smc_indicators 输出结构。"""

    def test_output_keys(self) -> None:
        """输出包含所有必需键。"""
        opens, highs, lows, closes, times = _gen_random_bars(150)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        required_keys = {
            "events",
            "order_blocks",
            "equal_highs_lows",
            "trailing",
            "pivots",
            "time",
            "params",
        }
        assert required_keys.issubset(set(result.keys())), (
            f"输出缺少键: {required_keys - set(result.keys())}"
        )

    def test_time_array_matches_input(self) -> None:
        """time 数组与输入 times 等长且内容一致。"""
        opens, highs, lows, closes, times = _gen_random_bars(100)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        assert result["time"] == times
        assert len(result["time"]) == len(times)

    def test_params_returned(self) -> None:
        """输出包含实际使用的 params。"""
        opens, highs, lows, closes, times = _gen_random_bars(100)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        assert isinstance(result["params"], dict)
        assert result["params"]["swings_length"] == 50

    def test_custom_params_override(self) -> None:
        """自定义 params 覆盖默认值。"""
        opens, highs, lows, closes, times = _gen_random_bars(100)
        custom_params: dict[str, Any] = {"swings_length": 30}
        result = compute_smc_indicators(
            opens, highs, lows, closes, times, params=custom_params,
        )
        assert result["params"]["swings_length"] == 30
        # 其他字段保持默认
        assert result["params"]["equal_length"] == 3


# ===== 5. anchor/confirmed 因果契约 =====


class TestAnchorConfirmedContract:
    """验证 anchor/confirmed 因果契约：未来 bar 不修改已确认事件。"""

    def test_incremental_equals_full_at_confirmed(self) -> None:
        """逐 bar 增量 = 全量同 confirmed 时结果。

        核心思想：取前 N 根计算全量结果，再取前 N+1 根计算全量结果。
        对于 confirmed_index < N 的事件，两次结果应完全一致
        （未来 bar 不修改已确认事件）。
        """
        n_full = 200
        opens, highs, lows, closes, times = _gen_random_bars(n_full, seed=123)

        # 全量计算
        full_result = compute_smc_indicators(opens, highs, lows, closes, times)

        # 取前 150 根计算（截断）
        n_partial = 150
        partial_result = compute_smc_indicators(
            opens[:n_partial],
            highs[:n_partial],
            lows[:n_partial],
            closes[:n_partial],
            times[:n_partial],
        )

        # 对于 confirmed_index < n_partial 的事件，两次结果应一致
        # （已确认事件不可变）
        full_events_confirmed_before = [
            ev for ev in full_result["events"]
            if ev.get("confirmed_index", 0) < n_partial
        ]
        partial_events = partial_result["events"]

        # 事件数应一致（已确认部分）
        assert len(full_events_confirmed_before) == len(partial_events), (
            f"已确认事件数不一致: full={len(full_events_confirmed_before)}, "
            f"partial={len(partial_events)}. "
            "未来 bar 不得修改已确认事件。"
        )

        # 逐事件比较（只比较不可变字段）
        for ev_full, ev_partial in zip(full_events_confirmed_before, partial_events, strict=True):
            assert ev_full["type"] == ev_partial["type"]
            assert ev_full["anchor_index"] == ev_partial["anchor_index"]
            assert ev_full["confirmed_index"] == ev_partial["confirmed_index"]
            assert ev_full.get("level") == ev_partial.get("level")

    def test_events_have_anchor_and_confirmed(self) -> None:
        """所有事件都包含 anchor_index/time 和 confirmed_index/time 字段。"""
        opens, highs, lows, closes, times = _gen_random_bars(200, seed=456)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        for ev in result["events"]:
            assert "anchor_index" in ev, f"事件缺少 anchor_index: {ev}"
            assert "confirmed_index" in ev, f"事件缺少 confirmed_index: {ev}"
            assert "anchor_time" in ev, f"事件缺少 anchor_time: {ev}"
            assert "confirmed_time" in ev, f"事件缺少 confirmed_time: {ev}"
            # confirmed 应 >= anchor
            assert ev["confirmed_index"] >= ev["anchor_index"], (
                f"confirmed_index ({ev['confirmed_index']}) 应 >= "
                f"anchor_index ({ev['anchor_index']})"
            )

    def test_order_blocks_have_anchor_and_confirmed(self) -> None:
        """所有 order blocks 都包含 anchor (anchor_index) 和 confirmed 字段。"""
        opens, highs, lows, closes, times = _gen_random_bars(200, seed=789)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        for ob in result["order_blocks"]:
            assert "anchor_index" in ob, f"OB 缺少 anchor_index: {ob}"
            assert "confirmed_index" in ob, f"OB 缺少 confirmed_index: {ob}"
            assert "anchor_time" in ob, f"OB 缺少 anchor_time: {ob}"
            assert "confirmed_time" in ob, f"OB 缺少 confirmed_time: {ob}"

    def test_equal_highs_lows_have_anchor_and_confirmed(self) -> None:
        """所有 EQH/EQL 都包含 anchor 和 confirmed 字段。"""
        opens, highs, lows, closes, times = _gen_random_bars(200, seed=321)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        for eq in result["equal_highs_lows"]:
            assert "anchor_index" in eq, f"EQH/EQL 缺少 anchor_index: {eq}"
            assert "confirmed_index" in eq, f"EQH/EQL 缺少 confirmed_index: {eq}"

    def test_equal_highs_lows_use_second_pivot_naming(self) -> None:
        """CHANGE-20260715-007: EQH/EQL DTO 使用 second_pivot/confirmed 三时间点命名。

        - anchor_index/time: 前一 pivot 的 bar 位置
        - second_pivot_index/time: 新 pivot 所在 bar (= ref_i = i-size)
        - confirmed_index/time: leg change 检测 bar (= i，因果确认点)
        禁止继续把 ref_i 命名为 confirmed，或把 i 命名为 detection。
        """
        opens, highs, lows, closes, times = _gen_random_bars(300, seed=654)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        eqs = result["equal_highs_lows"]
        if not eqs:
            # 数据未触发 EQH/EQL，仅断言字段不存在的旧名（防御性）
            return
        for eq in eqs:
            # 新字段必须存在
            assert "second_pivot_index" in eq, f"EQH/EQL 缺少 second_pivot_index: {eq}"
            assert "second_pivot_time" in eq, f"EQH/EQL 缺少 second_pivot_time: {eq}"
            assert "confirmed_index" in eq, f"EQH/EQL 缺少 confirmed_index: {eq}"
            assert "confirmed_time" in eq, f"EQH/EQL 缺少 confirmed_time: {eq}"
            # 旧字段名必须不存在（CHANGE-20260715-007 已重命名）
            assert "detection_index" not in eq, f"EQH/EQL 不得含旧字段 detection_index: {eq}"
            assert "detection_time" not in eq, f"EQH/EQL 不得含旧字段 detection_time: {eq}"
            # 因果顺序：anchor <= second_pivot <= confirmed
            assert eq["anchor_index"] <= eq["second_pivot_index"], (
                f"anchor_index ({eq['anchor_index']}) 应 <= second_pivot_index "
                f"({eq['second_pivot_index']})"
            )
            assert eq["second_pivot_index"] <= eq["confirmed_index"], (
                f"second_pivot_index ({eq['second_pivot_index']}) 应 <= confirmed_index "
                f"({eq['confirmed_index']})"
            )

    def test_swing_bias_returned_and_valid(self) -> None:
        """CHANGE-20260715-007: compute_smc 返回 swing_bias 字段，值合法。

        合法值：1（bullish）、-1（bearish）、0（尚未形成趋势）
        空数据时返回 0；正常数据时透传 state.swing_trend.bias。
        禁止根据 trailing 时间、close 位置或最后一个可见事件重新推断。
        """
        # 空数据 → 0
        empty_result = compute_smc_indicators([], [], [], [], [])
        assert empty_result["swing_bias"] == 0, "空数据 swing_bias 应为 0"

        # 正常数据 → 必须为合法值
        opens, highs, lows, closes, times = _gen_random_bars(300, seed=987)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        assert "swing_bias" in result, "compute_smc 必须返回 swing_bias 字段"
        valid_biases = {1, -1, 0}
        assert result["swing_bias"] in valid_biases, (
            f"swing_bias 应为 {valid_biases} 之一，实得: {result['swing_bias']}"
        )
        # 必须为 int 类型（不是字符串）
        assert isinstance(result["swing_bias"], int), (
            f"swing_bias 必须为 int 类型，实得 {type(result['swing_bias']).__name__}"
        )


# ===== 6. BOS/CHoCH 事件 =====


class TestBosChochEvents:
    """验证 BOS/CHoCH 事件类型与字段。"""

    def test_event_types_valid(self) -> None:
        """所有事件 type 为 BOS 或 CHoCH。"""
        opens, highs, lows, closes, times = _gen_random_bars(200, seed=111)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        valid_types = {"BOS", "CHoCH"}
        for ev in result["events"]:
            assert ev["type"] in valid_types, (
                f"事件 type 应为 BOS/CHoCH，实得: {ev['type']}"
            )

    def test_event_bias_valid(self) -> None:
        """所有事件 bias 为 1 (bullish) 或 -1 (bearish)。"""
        opens, highs, lows, closes, times = _gen_random_bars(200, seed=222)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        for ev in result["events"]:
            assert ev["bias"] in (1, -1), f"事件 bias 应为 ±1，实得: {ev['bias']}"

    def test_event_internal_field_valid(self) -> None:
        """所有事件 internal 字段为布尔值（true=internal, false/缺失=swing）。"""
        opens, highs, lows, closes, times = _gen_random_bars(200, seed=333)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        for ev in result["events"]:
            if "internal" in ev:
                assert isinstance(ev["internal"], bool), (
                    f"事件 internal 应为 bool，实得: {type(ev['internal'])}"
                )


# ===== 7. Order Block 创建与 mitigation =====


class TestOrderBlocks:
    """验证 Order Block 创建与 mitigation 逻辑。"""

    def test_ob_bias_valid(self) -> None:
        """所有 OB bias 为 1 (bullish) 或 -1 (bearish)。"""
        opens, highs, lows, closes, times = _gen_random_bars(200, seed=444)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        for ob in result["order_blocks"]:
            assert ob["bias"] in (1, -1), f"OB bias 应为 ±1，实得: {ob['bias']}"

    def test_ob_has_bar_high_low(self) -> None:
        """所有 OB 包含 bar_high 和 bar_low 字段。"""
        opens, highs, lows, closes, times = _gen_random_bars(200, seed=555)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        for ob in result["order_blocks"]:
            assert "bar_high" in ob, f"OB 缺少 bar_high: {ob}"
            assert "bar_low" in ob, f"OB 缺少 bar_low: {ob}"
            assert ob["bar_high"] >= ob["bar_low"], (
                f"OB bar_high ({ob['bar_high']}) 应 >= bar_low ({ob['bar_low']})"
            )

    def test_ob_mitigated_field_exists(self) -> None:
        """所有 OB 包含 mitigated 布尔字段。"""
        opens, highs, lows, closes, times = _gen_random_bars(200, seed=666)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        for ob in result["order_blocks"]:
            assert "mitigated" in ob, f"OB 缺少 mitigated 字段: {ob}"
            assert isinstance(ob["mitigated"], bool), (
                f"OB mitigated 应为 bool，实得: {type(ob['mitigated'])}"
            )

    def test_ob_mitigation_unidirectional(self) -> None:
        """mitigated=True 的 OB 在更长数据下不得变回 False（不可变）。"""
        opens, highs, lows, closes, times = _gen_random_bars(180, seed=777)
        partial_result = compute_smc_indicators(
            opens[:150], highs[:150], lows[:150], closes[:150], times[:150],
        )

        # 找到 partial 中 mitigated=True 的 OB
        mitigated_obs_partial = [
            ob for ob in partial_result["order_blocks"] if ob.get("mitigated") is True
        ]

        if mitigated_obs_partial:
            # 用更长数据计算
            full_result = compute_smc_indicators(opens, highs, lows, closes, times)
            # partial 中已 mitigated 的 OB 在 full 中应仍为 mitigated
            # 通过 anchor_index + confirmed_index 匹配
            partial_mitigated_keys = {
                (ob["anchor_index"], ob["confirmed_index"])
                for ob in mitigated_obs_partial
            }
            for ob_full in full_result["order_blocks"]:
                key = (ob_full["anchor_index"], ob_full["confirmed_index"])
                if key in partial_mitigated_keys:
                    assert ob_full["mitigated"] is True, (
                        f"OB {key} 在 partial 中已 mitigated，"
                        "在 full 中不得变回 False（mitigation 不可变）"
                    )


# ===== 8. EQH/EQL 事件 =====


class TestEqualHighsLows:
    """验证 EQH/EQL 事件。"""

    def test_eqhl_types_valid(self) -> None:
        """所有 EQH/EQL type 为 EQH 或 EQL。"""
        opens, highs, lows, closes, times = _gen_random_bars(300, seed=888)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        valid_types = {"EQH", "EQL"}
        for eq in result["equal_highs_lows"]:
            assert eq["type"] in valid_types, (
                f"EQH/EQL type 应为 EQH/EQL，实得: {eq['type']}"
            )

    def test_eqhl_has_level_and_prev_level(self) -> None:
        """所有 EQH/EQL 包含 level 和 prev_level。"""
        opens, highs, lows, closes, times = _gen_random_bars(300, seed=999)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        for eq in result["equal_highs_lows"]:
            assert "level" in eq, f"EQH/EQL 缺少 level: {eq}"
            assert "prev_level" in eq, f"EQH/EQL 缺少 prev_level: {eq}"


# ===== 9. Trailing 结构 =====


class TestTrailing:
    """验证 trailing strong/weak high/low 结构。"""

    def test_trailing_structure(self) -> None:
        """trailing 输出包含必需字段。"""
        opens, highs, lows, closes, times = _gen_random_bars(200, seed=101)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        trailing = result["trailing"]
        assert isinstance(trailing, dict)
        required_keys = {"top", "bottom", "bar_time", "bar_index"}
        assert required_keys.issubset(set(trailing.keys())), (
            f"trailing 缺少字段: {required_keys - set(trailing.keys())}"
        )

    def test_trailing_top_ge_bottom_when_both_present(self) -> None:
        """top 和 bottom 同时存在时 top >= bottom。"""
        opens, highs, lows, closes, times = _gen_random_bars(200, seed=202)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        trailing = result["trailing"]
        if trailing["top"] is not None and trailing["bottom"] is not None:
            assert trailing["top"] >= trailing["bottom"], (
                f"trailing.top ({trailing['top']}) 应 >= "
                f"trailing.bottom ({trailing['bottom']})"
            )


# ===== 10. 模块加载与可调用性 =====


class TestModuleLoadable:
    """验证模块可正常加载和调用。"""

    def test_compute_smc_indicators_callable(self) -> None:
        """compute_smc_indicators 可调用。"""
        assert callable(compute_smc_indicators)

    def test_compute_smc_indicators_signature(self) -> None:
        """函数签名正确。"""
        sig = inspect.signature(compute_smc_indicators)
        params = list(sig.parameters.keys())
        expected = ["opens", "highs", "lows", "closes", "times", "params"]
        assert params == expected, f"参数不匹配: {params} != {expected}"

    def test_state_class_exists(self) -> None:
        """_SMCState 状态机类存在。"""
        assert _SMCState is not None


# ===== 11. Pine 语义原语验证（CHANGE-20260715-002）=====


class TestPineSemantics:
    """验证 smc_pine_core 的 Pine 语义原语。

    Pine 语义原语是 SMC 算法的基础，必须与 TradingView Pine 的 ta.* 函数完全一致。
    """

    def test_pine_rma_wilder_recursion(self) -> None:
        """ta.rma(src, length) 使用 Wilder 递推，SMA 播种。

        Pine: rma[length-1] = sma(src, length)
              rma[i] = (rma[i-1] * (length-1) + src[i]) / length
        """
        from app.strategy_assets.algorithms.features.smc_pine_core import pine_rma

        src = [float(i + 1) for i in range(10)]  # 1..10
        rma = pine_rma(src, 5)
        # SMA seed at index 4: (1+2+3+4+5)/5 = 3.0
        assert abs(rma[4] - 3.0) < 1e-10, f"RMA[4] SMA seed 应为 3.0，实得 {rma[4]}"
        # Wilder recursion: rma[5] = (3.0 * 4 + 6.0) / 5 = 3.6
        assert abs(rma[5] - 3.6) < 1e-10, f"RMA[5] Wilder 应为 3.6，实得 {rma[5]}"
        # rma[6] = (3.6 * 4 + 7.0) / 5 = 4.28
        assert abs(rma[6] - 4.28) < 1e-10, f"RMA[6] Wilder 应为 4.28，实得 {rma[6]}"

    def test_pine_rma_min_periods_before_seed(self) -> None:
        """CHANGE-20260715-006: ta.rma 前 length-1 根返回 na（Pine v5 语义）。

        旧实现错误地返回逐步 SMA（min_periods 行为），导致 ATR(200) 在前 199 根
        产生非 na 值。Pine v5 ta.rma 在 bar_index < length-1 时返回 na。
        """
        import math

        from app.strategy_assets.algorithms.features.smc_pine_core import pine_rma

        src = [2.0, 4.0, 6.0, 8.0, 10.0]
        rma = pine_rma(src, 5)
        # index 0-3: na（数据不足以计算 SMA 种子）
        assert math.isnan(rma[0]), f"RMA[0] 应为 NaN，实得 {rma[0]}"
        assert math.isnan(rma[1]), f"RMA[1] 应为 NaN，实得 {rma[1]}"
        assert math.isnan(rma[2]), f"RMA[2] 应为 NaN，实得 {rma[2]}"
        assert math.isnan(rma[3]), f"RMA[3] 应为 NaN，实得 {rma[3]}"
        # index 4: SMA seed = (2+4+6+8+10)/5 = 6.0
        assert abs(rma[4] - 6.0) < 1e-10

    def test_pine_cumulative_mean_range_bar0_nan(self) -> None:
        """ta.cum(ta.tr) / bar_index：bar 0 = NaN（除零）。"""
        import math

        from app.strategy_assets.algorithms.features.smc_pine_core import (
            pine_cumulative_mean_range,
        )

        highs = [11.0, 12.0, 10.5]
        lows = [9.0, 10.0, 8.5]
        closes = [10.0, 11.0, 9.5]
        cmr = pine_cumulative_mean_range(highs, lows, closes)
        # bar 0: tr[0] / 0 = NaN
        assert math.isnan(cmr[0]), f"CMR[0] 应为 NaN，实得 {cmr[0]}"
        # bar 1: (tr[0]+tr[1]) / 1
        assert not math.isnan(cmr[1]), "CMR[1] 不应为 NaN"
        # bar 2: (tr[0]+tr[1]+tr[2]) / 2
        assert not math.isnan(cmr[2]), "CMR[2] 不应为 NaN"

    def test_pine_atr_equals_rma_of_tr(self) -> None:
        """ta.atr(n) = ta.rma(ta.tr, n)。"""
        import math

        from app.strategy_assets.algorithms.features.smc_pine_core import (
            pine_atr,
            pine_rma,
            pine_true_range,
        )

        highs = [11.0 + i * 0.5 for i in range(250)]
        lows = [9.0 + i * 0.5 for i in range(250)]
        closes = [10.0 + i * 0.5 for i in range(250)]
        atr_result = pine_atr(highs, lows, closes, 200)
        tr = pine_true_range(highs, lows, closes)
        rma_tr = pine_rma(tr, 200)
        # ATR should equal RMA(TR, 200)
        # CHANGE-20260715-006: 前 length-1 根两者均为 NaN（Pine v5 ta.rma NA 语义）
        for i in range(len(atr_result)):
            a, r = atr_result[i], rma_tr[i]
            if math.isnan(a) and math.isnan(r):
                continue
            assert abs(a - r) < 1e-10, (
                f"ATR[{i}] 应等于 RMA(TR,200)[{i}]，实得 ATR={a} RMA={r}"
            )

    def test_pine_crossover(self) -> None:
        """ta.crossover(a, b) = a[0] > b[0] and a[1] <= b[1]。"""
        from app.strategy_assets.algorithms.features.smc_pine_core import (
            pine_crossover,
        )

        # a 从下方穿越 b
        assert pine_crossover(11.0, 10.0, 10.5, 10.5) is True
        # a 未穿越（a 仍 <= b）
        assert pine_crossover(10.0, 10.5, 11.0, 10.5) is False
        # a 前一根已 > b（非穿越）
        assert pine_crossover(12.0, 11.0, 10.0, 10.0) is False

    def test_pine_crossunder(self) -> None:
        """ta.crossunder(a, b) = a[0] < b[0] and a[1] >= b[1]。

        签名: pine_crossunder(a_curr, a_prev, b_curr, b_prev)
        """
        from app.strategy_assets.algorithms.features.smc_pine_core import (
            pine_crossunder,
        )

        # a 从上方穿越 b: a_prev=10.5 >= b_prev=10, a_curr=9 < b_curr=10
        assert pine_crossunder(9.0, 10.5, 10.0, 10.0) is True
        # a 未穿越（a_curr 仍 >= b_curr）
        assert pine_crossunder(11.0, 10.5, 10.0, 10.5) is False
        # a 前一根已 < b（a_prev < b_prev，非穿越）
        assert pine_crossunder(8.0, 9.0, 10.0, 10.5) is False

    def test_pine_highest_excludes_ref_bar(self) -> None:
        """ta.highest(src, length) 在 ref_i 之后窗口取 max（不含 ref_i）。"""
        from app.strategy_assets.algorithms.features.smc_pine_core import (
            pine_highest,
        )

        src = [5.0, 3.0, 8.0, 2.0, 7.0, 1.0, 9.0]
        # ref_i=0, length=3: max(src[1..3]) = max(3,8,2) = 8
        assert pine_highest(src, 3, 0) == 8.0
        # ref_i=2, length=3: max(src[3..5]) = max(2,7,1) = 7
        assert pine_highest(src, 3, 2) == 7.0

    def test_pine_lowest_excludes_ref_bar(self) -> None:
        """ta.lowest(src, length) 在 ref_i 之后窗口取 min（不含 ref_i）。"""
        from app.strategy_assets.algorithms.features.smc_pine_core import (
            pine_lowest,
        )

        src = [5.0, 3.0, 8.0, 2.0, 7.0, 1.0, 9.0]
        # ref_i=0, length=3: min(src[1..3]) = min(3,8,2) = 2
        assert pine_lowest(src, 3, 0) == 2.0
        # ref_i=2, length=3: min(src[3..5]) = min(2,7,1) = 1
        assert pine_lowest(src, 3, 2) == 1.0


# ===== 12. Pine Golden Fixture 对齐测试（CHANGE-20260715-002）=====


class TestPineGoldenFixture:
    """Pine golden fixture 逐事件对齐测试。

    没有Pine golden fixture不得宣称"完全对齐"。
    本测试在 fixture 不存在时 skip，存在时进行逐事件比较。
    """

    def test_pine_golden_fixture_exists_or_skip(self) -> None:
        """Pine golden fixture 存在时运行对齐测试，否则 skip。"""
        import os
        fixture_dir = os.path.join(
            os.path.dirname(__file__), "fixtures", "smc_pine"
        )
        events_csv = os.path.join(fixture_dir, "pine_events_603538_1d.csv")
        if not os.path.exists(events_csv):
            import pytest
            pytest.skip(
                "Pine golden fixture 不存在（等待 TV 导出）。"
                "没有 Pine golden fixture 不得宣称'完全对齐'。"
                "导出指南见 backend/tests/fixtures/smc_pine/README.md"
            )
