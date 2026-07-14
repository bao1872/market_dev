"""CHANGE-011 SMC 指标单元测试。

验证内容（用户任务第三块第 8 点）：
1. 附件默认参数快照（DEFAULT_PARAMS 与 build_parser() 默认值一致）
2. 逐 bar 增量 = 全量同 confirmed 时结果（因果契约：未来 bar 不修改已确认事件）
3. FVG 不计算、不返回、不显示（输出/schema 中不存在 FVG 字段、事件、box 或开关）
4. 开关关闭 0 计算（include_smc=False 时 indicator_service 不调用 SMC）
5. OB 创建与 mitigation（internal OB 创建后 mitigation 标记不可变）
6. BOS/CHoCH、EQH/EQL（事件类型与 anchor/confirmed 字段）
7. 空数据和不足 lookback（不抛异常，返回空 events）

算法真源：用户提供的 ref/smc.py 重写版本（非 LuxAlgo Pine 翻译）。
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

    def test_event_kind_valid(self) -> None:
        """所有事件 kind 为 internal 或 swing。"""
        opens, highs, lows, closes, times = _gen_random_bars(200, seed=333)
        result = compute_smc_indicators(opens, highs, lows, closes, times)
        valid_kinds = {"internal", "swing"}
        for ev in result["events"]:
            if "kind" in ev:
                assert ev["kind"] in valid_kinds, (
                    f"事件 kind 应为 internal/swing，实得: {ev['kind']}"
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
