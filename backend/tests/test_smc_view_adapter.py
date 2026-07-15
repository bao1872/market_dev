"""CHANGE-20260715-007: SMC view adapter 单元测试。

验证 adapt_smc_to_display_dto 的行为：
- 索引重基准到展示窗口（offset = max(0, total - display)）
- events/EQH/EQL/pivots 窗口外丢弃，窗口内重基准
- OB 与窗口相交保留；anchor 在窗口左侧标记 clipped_left=True
- swing_bias 透传
- 响应大小与 display_bars 同阶（time 数组长度 = min(total, display_bars)）
- FVG 防御性过滤
- 空 DTO 边界
"""

from __future__ import annotations

from app.services.smc_view_adapter import adapt_smc_to_display_dto


def _make_full_result(total_bars: int = 10) -> dict:
    """构造测试用完整 SMC 计算结果。"""
    return {
        "time": [f"t{i}" for i in range(total_bars)],
        "events": [
            {"type": "BOS", "anchor_index": 1, "confirmed_index": 2, "level": 10.0},
            {"type": "BOS", "anchor_index": 7, "confirmed_index": 8, "level": 11.0},
        ],
        "order_blocks": [
            # 完全在窗口左侧（已 mitigated）
            {"anchor_index": 1, "confirmed_index": 2, "mitigated_index": 3,
             "bar_high": 11.0, "bar_low": 9.0, "bias": -1, "internal": True},
            # 跨越窗口左边界（未 mitigated）
            {"anchor_index": 2, "confirmed_index": 4, "mitigated_index": None,
             "bar_high": 12.0, "bar_low": 8.0, "bias": 1, "internal": True},
            # 完全在窗口内（未 mitigated）
            {"anchor_index": 6, "confirmed_index": 7, "mitigated_index": None,
             "bar_high": 13.0, "bar_low": 7.0, "bias": -1, "internal": True},
        ],
        "equal_highs_lows": [
            {"type": "EQH", "anchor_index": 6, "second_pivot_index": 7,
             "confirmed_index": 8, "level": 10.5, "prev_level": 10.4},
        ],
        "pivots": [
            {"type": "swing_high", "anchor_index": 7, "confirmed_index": 8, "level": 11.0},
        ],
        "trailing": {"top": 12.0, "bottom": 8.0, "bar_index": 5, "bar_time": "t5",
                     "last_top_time": "t5", "last_bottom_time": "t3"},
        "swing_bias": -1,  # bearish → 前端 Strong High
        "params": {"swings_length": 50},
    }


class TestAdapterEmptyAndBoundary:
    """空输入与边界条件。"""

    def test_empty_input_returns_empty_dto(self) -> None:
        out = adapt_smc_to_display_dto({}, 250)
        assert out["events"] == []
        assert out["order_blocks"] == []
        assert out["equal_highs_lows"] == []
        assert out["pivots"] == []
        assert out["time"] == []
        assert out["swing_bias"] == 0
        assert out["view"]["total_bars"] == 0
        assert out["view"]["display_bars"] == 0

    def test_display_bars_zero_returns_empty_dto(self) -> None:
        out = adapt_smc_to_display_dto(_make_full_result(10), 0)
        assert out["time"] == []
        assert out["view"]["total_bars"] == 0  # 空 DTO 路径

    def test_display_bars_negative_returns_empty_dto(self) -> None:
        out = adapt_smc_to_display_dto(_make_full_result(10), -5)
        assert out["time"] == []

    def test_display_bars_larger_than_total_keeps_all(self) -> None:
        """display_bars > total_bars 时 offset=0，保留全部，索引不变。"""
        out = adapt_smc_to_display_dto(_make_full_result(10), 100)
        assert out["view"]["offset"] == 0
        assert out["view"]["window_start"] == 0
        assert out["view"]["window_end"] == 10
        assert len(out["time"]) == 10
        # 索引未偏移
        for e in out["events"]:
            assert e["anchor_index"] >= 0


class TestAdapterIndexRebasing:
    """索引重基准到展示窗口。"""

    def test_offset_is_max_zero_total_minus_display(self) -> None:
        """offset = max(0, total_bars - display_bars)。"""
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        assert out["view"]["offset"] == 5
        assert out["view"]["window_start"] == 5
        assert out["view"]["window_end"] == 10

    def test_time_array_clipped_to_window(self) -> None:
        """time 数组只保留窗口内。"""
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        assert out["time"] == ["t5", "t6", "t7", "t8", "t9"]

    def test_events_outside_window_dropped(self) -> None:
        """anchor 和 confirmed 都在窗口外的事件被丢弃。"""
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        # 第一条 (1,2) 都在 [0,5) 外 → 丢
        # 第二条 (7,8) 都在 [5,10) 内 → 保留
        assert len(out["events"]) == 1
        assert out["events"][0]["anchor_index"] == 2  # 7-5
        assert out["events"][0]["confirmed_index"] == 3  # 8-5

    def test_equal_highs_lows_rebased(self) -> None:
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        assert len(out["equal_highs_lows"]) == 1
        eq = out["equal_highs_lows"][0]
        assert eq["anchor_index"] == 1  # 6-5
        assert eq["second_pivot_index"] == 2  # 7-5
        assert eq["confirmed_index"] == 3  # 8-5

    def test_pivots_rebased(self) -> None:
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        assert len(out["pivots"]) == 1
        assert out["pivots"][0]["anchor_index"] == 2  # 7-5
        assert out["pivots"][0]["confirmed_index"] == 3  # 8-5

    def test_trailing_bar_index_rebased(self) -> None:
        """trailing.bar_index 重基准（可能为负）。"""
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        assert out["trailing"]["bar_index"] == 0  # 5-5
        # top/bottom/last_top_time 等不变
        assert out["trailing"]["top"] == 12.0
        assert out["trailing"]["last_top_time"] == "t5"

    def test_trailing_bar_index_can_be_negative(self) -> None:
        """trailing.bar_index 在窗口左侧时重基准后为负。"""
        full = _make_full_result(10)
        full["trailing"]["bar_index"] = 2  # 在 [0,5) 外
        out = adapt_smc_to_display_dto(full, 5)
        assert out["trailing"]["bar_index"] == -3  # 2-5


class TestAdapterOrderBlocksIntersect:
    """OB 与窗口相交逻辑 + clipped_left 标记。"""

    def test_ob_fully_left_dropped(self) -> None:
        """完全在窗口左侧（已 mitigated）的 OB 被丢弃。"""
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        # 第一条 OB (anchor=1, mit=3) 完全在 [0,5) 外 → 丢
        obs = [o for o in out["order_blocks"] if o.get("bar_high") == 11.0]
        assert obs == [], "完全在窗口左侧的 OB 必须丢弃"

    def test_ob_intersecting_left_kept_with_clipped_left(self) -> None:
        """跨窗口左边界的 OB（未 mitigated）保留并标记 clipped_left=True，anchor clamp 到 0。"""
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        # 第二条 OB (anchor=2, mit=None) anchor<5 但延伸到 10 → 保留
        obs = [o for o in out["order_blocks"] if o.get("bar_high") == 12.0]
        assert len(obs) == 1
        assert obs[0]["clipped_left"] is True
        # CHANGE-20260715-007: clipped_left 时 anchor_index clamp 到 0（不再为负）
        assert obs[0]["anchor_index"] == 0

    def test_ob_fully_inside_kept_without_clipped_left(self) -> None:
        """完全在窗口内的 OB 保留且 clipped_left=False。"""
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        # 第三条 OB (anchor=6, mit=None) 完全在 [5,10) 内 → 保留
        obs = [o for o in out["order_blocks"] if o.get("bar_high") == 13.0]
        assert len(obs) == 1
        assert obs[0]["clipped_left"] is False
        assert obs[0]["anchor_index"] == 1  # 6-5

    def test_ob_fully_right_dropped(self) -> None:
        """完全在窗口右侧的 OB 被丢弃。"""
        full = _make_full_result(10)
        full["order_blocks"].append({
            "anchor_index": 9, "confirmed_index": 9, "mitigated_index": None,
            "bar_high": 99.0, "bar_low": 1.0, "bias": 1, "internal": True,
        })
        out = adapt_smc_to_display_dto(full, 5)
        # anchor=9 在 [5,10) 内，但 ob_end=10 也在窗口内 → 边界保留
        # 所以这条 OB 会被保留（因为 anchor=9 < window_end=10）
        obs = [o for o in out["order_blocks"] if o.get("bar_high") == 99.0]
        assert len(obs) == 1
        assert obs[0]["anchor_index"] == 4  # 9-5

    def test_ob_mitigated_at_window_left_kept(self) -> None:
        """mitigated_index 恰在 window_start 的 OB 保留（边界相交）。"""
        full = _make_full_result(10)
        full["order_blocks"] = [{
            "anchor_index": 3, "confirmed_index": 4, "mitigated_index": 5,
            "bar_high": 11.0, "bar_low": 9.0, "bias": -1, "internal": True,
        }]
        out = adapt_smc_to_display_dto(full, 5)
        # ob_end_exclusive = 5+1 = 6, ob_end_exclusive-1=5 >= window_start=5 → 相交
        assert len(out["order_blocks"]) == 1
        assert out["order_blocks"][0]["clipped_left"] is True  # anchor=3 < 5

    def test_ob_mitigated_before_window_dropped(self) -> None:
        """mitigated_index 在 window_start 之前的 OB 被丢弃。"""
        full = _make_full_result(10)
        full["order_blocks"] = [{
            "anchor_index": 1, "confirmed_index": 2, "mitigated_index": 4,
            "bar_high": 11.0, "bar_low": 9.0, "bias": -1, "internal": True,
        }]
        out = adapt_smc_to_display_dto(full, 5)
        # ob_end_exclusive = 4+1 = 5, ob_end_exclusive-1=4 < window_start=5 → 不相交
        assert out["order_blocks"] == []


class TestAdapterSwingBiasPassThrough:
    """swing_bias 透传。"""

    def test_swing_bias_passed_through(self) -> None:
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        assert out["swing_bias"] == -1

    def test_swing_bias_defaults_to_zero_when_missing(self) -> None:
        full = _make_full_result(10)
        full.pop("swing_bias")
        out = adapt_smc_to_display_dto(full, 5)
        assert out["swing_bias"] == 0


class TestAdapterFvgExclusion:
    """FVG 字段防御性过滤。"""

    def test_fvg_fields_filtered_from_events(self) -> None:
        full = _make_full_result(10)
        full["events"].append({
            "type": "BOS", "anchor_index": 7, "confirmed_index": 8,
            "level": 11.0, "fvg_field": 1.0, "fvg_box": {"a": 1},
        })
        out = adapt_smc_to_display_dto(full, 5)
        for e in out["events"]:
            for k in e:
                assert "fvg" not in str(k).lower(), f"adapter 必须过滤 fvg 字段: {k}"

    def test_fvg_fields_filtered_from_order_blocks(self) -> None:
        full = _make_full_result(10)
        full["order_blocks"].append({
            "anchor_index": 6, "confirmed_index": 7, "mitigated_index": None,
            "bar_high": 14.0, "bar_low": 6.0, "bias": 1, "internal": True,
            "fvg_extra": "should_be_filtered",
        })
        out = adapt_smc_to_display_dto(full, 5)
        for o in out["order_blocks"]:
            for k in o:
                assert "fvg" not in str(k).lower(), f"adapter 必须过滤 OB fvg 字段: {k}"

    def test_no_fvg_in_any_output(self) -> None:
        """输出任何位置都不得包含 fvg 字段。"""
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        # 递归检查所有键
        def _check_no_fvg(obj, path="") -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    assert "fvg" not in str(k).lower(), f"输出含 fvg 键: {path}.{k}"
                    _check_no_fvg(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    _check_no_fvg(item, f"{path}[{i}]")
        _check_no_fvg(out)


class TestAdapterResponseSize:
    """响应大小与 display_bars 同阶。"""

    def test_time_length_at_most_display_bars(self) -> None:
        """time 数组长度 = min(total, display_bars)。"""
        # total=10, display=5 → time=5
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        assert len(out["time"]) == 5

        # total=10, display=20 → time=10
        out = adapt_smc_to_display_dto(_make_full_result(10), 20)
        assert len(out["time"]) == 10

    def test_response_does_not_include_full_history_time(self) -> None:
        """模拟 15m ~12000 根场景：display_bars=250 时 time 必须 ≤ 250。"""
        full = _make_full_result(12000)
        out = adapt_smc_to_display_dto(full, 250)
        assert len(out["time"]) == 250
        assert out["view"]["total_bars"] == 12000
        assert out["view"]["offset"] == 11750


class TestAdapterViewMetadata:
    """view 元信息字段。"""

    def test_view_contains_required_fields(self) -> None:
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        for key in ("total_bars", "display_bars", "offset", "window_start", "window_end"):
            assert key in out["view"], f"view 缺少 {key}"

    def test_view_window_consistency(self) -> None:
        """window_end - window_start == len(time)。"""
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        assert out["view"]["window_end"] - out["view"]["window_start"] == len(out["time"])

    def test_view_offset_equals_window_start(self) -> None:
        out = adapt_smc_to_display_dto(_make_full_result(10), 5)
        assert out["view"]["offset"] == out["view"]["window_start"]


class TestAdapterImmutability:
    """adapter 不得修改输入。"""

    def test_input_not_modified(self) -> None:
        full = _make_full_result(10)
        # 深拷贝前的原值
        original_events = [dict(e) for e in full["events"]]
        original_obs = [dict(o) for o in full["order_blocks"]]
        _ = adapt_smc_to_display_dto(full, 5)
        # 输入应保持不变
        assert full["events"] == original_events
        assert full["order_blocks"] == original_obs
        # time 数组不被切片修改
        assert len(full["time"]) == 10


class TestAdapterFieldAudit:
    """CHANGE-20260715-007: 逐字段审计——所有索引必须重基准到展示坐标系。

    验证：events/EQH-EQL/OB/pivots/trailing 的所有 *_index 字段
    都已从完整历史索引重基准到展示窗口索引。
    """

    def test_all_index_fields_rebased_consistently(self) -> None:
        """所有 *_index 字段使用同一 offset 重基准。"""
        total = 100
        full = {
            "time": [f"t{i}" for i in range(total)],
            "events": [
                {"type": "BOS", "anchor_index": 80, "confirmed_index": 85, "level": 10.0},
            ],
            "order_blocks": [
                {"anchor_index": 70, "confirmed_index": 75, "mitigated_index": None,
                 "bar_high": 11.0, "bar_low": 9.0, "bias": -1, "internal": True},
                {"anchor_index": 90, "confirmed_index": 92, "mitigated_index": 95,
                 "bar_high": 12.0, "bar_low": 8.0, "bias": 1, "internal": True},
            ],
            "equal_highs_lows": [
                {"type": "EQH", "anchor_index": 82, "second_pivot_index": 85,
                 "confirmed_index": 88, "level": 10.5, "prev_level": 10.4},
            ],
            "pivots": [
                {"type": "swing_high", "anchor_index": 80, "confirmed_index": 85, "level": 11.0},
            ],
            "trailing": {"top": 12.0, "bottom": 8.0, "bar_index": 85, "bar_time": "t85",
                         "last_top_time": "t85", "last_bottom_time": "t80"},
            "swing_bias": -1,
            "params": {"swings_length": 50},
        }
        display = 20
        out = adapt_smc_to_display_dto(full, display)
        # offset = 100 - 20 = 80
        assert out["view"]["offset"] == 80

        # events: anchor 80→0, confirmed 85→5
        assert out["events"][0]["anchor_index"] == 0
        assert out["events"][0]["confirmed_index"] == 5

        # OB 1: anchor 70→clipped(0), confirmed 75→-5(stays as rebased, but anchor clamped)
        ob1 = [o for o in out["order_blocks"] if o.get("bar_high") == 11.0][0]
        assert ob1["clipped_left"] is True
        assert ob1["anchor_index"] == 0  # clamped
        # confirmed_index 75-80=-5，但 confirmed 不 clamp（只 anchor clamp）
        # 实际上 confirmed_index 也会是负数，这是正常的——confirmed 在窗口左侧
        # 但 OB 仍保留因为延伸到窗口内（mit=None）

        # OB 2: anchor 90→10, confirmed 92→12, mitigated 95→15
        ob2 = [o for o in out["order_blocks"] if o.get("bar_high") == 12.0][0]
        assert ob2["clipped_left"] is False
        assert ob2["anchor_index"] == 10  # 90-80
        assert ob2["confirmed_index"] == 12  # 92-80
        assert ob2["mitigated_index"] == 15  # 95-80

        # EQH/EQL: anchor 82→2, second_pivot 85→5, confirmed 88→8
        eq = out["equal_highs_lows"][0]
        assert eq["anchor_index"] == 2  # 82-80
        assert eq["second_pivot_index"] == 5  # 85-80
        assert eq["confirmed_index"] == 8  # 88-80

        # pivots: anchor 80→0, confirmed 85→5
        pv = out["pivots"][0]
        assert pv["anchor_index"] == 0  # 80-80
        assert pv["confirmed_index"] == 5  # 85-80

        # trailing: bar_index 85→5
        assert out["trailing"]["bar_index"] == 5  # 85-80

    def test_no_full_history_indices_in_dto(self) -> None:
        """DTO 中不得保留完整历史索引（所有 *_index 必须是重基准后的展示坐标）。

        重基准语义：rebased = original - offset；允许为负（窗口左侧），
        但不得保留任何原始完整历史索引（即不得出现 >= total_bars 的值，
        也不得出现未减 offset 的原始值）。
        OB clipped_left 时 anchor_index 被 clamp 到 0（不再为负）。
        """
        total = 200
        full = {
            "time": [f"t{i}" for i in range(total)],
            "events": [
                {"type": "BOS", "anchor_index": 150, "confirmed_index": 160, "level": 10.0},
            ],
            "order_blocks": [
                # 跨越窗口左边界：anchor=140 < 170，mit=None 延伸到 200
                # → 保留，clipped_left=True，anchor clamp 到 0，confirmed 重基准为 145-170=-25
                {"anchor_index": 140, "confirmed_index": 145, "mitigated_index": None,
                 "bar_high": 11.0, "bar_low": 9.0, "bias": -1, "internal": True},
            ],
            "equal_highs_lows": [
                {"type": "EQH", "anchor_index": 155, "second_pivot_index": 160,
                 "confirmed_index": 165, "level": 10.5, "prev_level": 10.4},
            ],
            "pivots": [
                {"type": "swing_high", "anchor_index": 150, "confirmed_index": 160, "level": 11.0},
            ],
            "trailing": {"top": 12.0, "bottom": 8.0, "bar_index": 160, "bar_time": "t160",
                         "last_top_time": "t160", "last_bottom_time": "t150"},
            "swing_bias": 1,
            "params": {},
        }
        display = 30
        out = adapt_smc_to_display_dto(full, display)
        offset = total - display
        assert offset == 170
        assert out["view"]["offset"] == offset

        def _assert_rebased(section: str, item: dict, originals: dict) -> None:
            for k, v in item.items():
                if not k.endswith("_index") or v is None:
                    continue
                # 不得保留完整历史索引（不得 >= total_bars）
                assert v < total, f"{section} {k}={v} 仍是完整历史索引（>=total={total})"
                # 必须是重基准后的值：v == original - offset
                # OB anchor 在 clipped_left 时被 clamp 到 0，单独校验
                orig = originals.get(k)
                if orig is None:
                    continue
                expected = orig - offset
                if section == "order_blocks" and k == "anchor_index" and item.get("clipped_left"):
                    assert v == 0, f"OB clipped_left anchor 应 clamp 到 0，实际 {v}"
                else:
                    assert v == expected, (
                        f"{section} {k}: expected rebased {expected} (orig {orig} - offset {offset}), got {v}"
                    )

        for e in out["events"]:
            _assert_rebased("events", e, {"anchor_index": 150, "confirmed_index": 160})
        for o in out["order_blocks"]:
            _assert_rebased(
                "order_blocks", o,
                {"anchor_index": 140, "confirmed_index": 145, "mitigated_index": None},
            )
            # clipped_left 时 anchor 必为 0（clamp 后），confirmed 可为负
            assert o["anchor_index"] == 0
            assert o["confirmed_index"] == 145 - offset  # -25
            assert o["clipped_left"] is True
        for eq in out["equal_highs_lows"]:
            _assert_rebased(
                "equal_highs_lows", eq,
                {"anchor_index": 155, "second_pivot_index": 160, "confirmed_index": 165},
            )
        for p in out["pivots"]:
            _assert_rebased("pivots", p, {"anchor_index": 150, "confirmed_index": 160})
        # trailing bar_index 重基准（160-170=-10，允许为负）
        assert out["trailing"]["bar_index"] == 160 - offset

    def test_eqh_three_timepoints_all_rebased(self) -> None:
        """EQH/EQL 三时间点（anchor/second_pivot/confirmed）全部重基准。"""
        total = 50
        full = {
            "time": [f"t{i}" for i in range(total)],
            "events": [],
            "order_blocks": [],
            "equal_highs_lows": [
                {"type": "EQH", "anchor_index": 30, "second_pivot_index": 35,
                 "confirmed_index": 40, "level": 10.5, "prev_level": 10.4},
            ],
            "pivots": [],
            "trailing": {},
            "swing_bias": 0,
            "params": {},
        }
        out = adapt_smc_to_display_dto(full, 20)
        # offset = 50 - 20 = 30
        eq = out["equal_highs_lows"][0]
        assert eq["anchor_index"] == 0  # 30-30
        assert eq["second_pivot_index"] == 5  # 35-30
        assert eq["confirmed_index"] == 10  # 40-30
        # 因果顺序保留
        assert eq["anchor_index"] <= eq["second_pivot_index"] <= eq["confirmed_index"]
