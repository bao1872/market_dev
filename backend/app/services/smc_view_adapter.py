"""SMC view adapter — 将完整历史计算结果裁成展示窗口 DTO。

CHANGE-20260715-007: 解决 SMC 完整计算结果（全量历史 time + 全部历史事件）
直接写入响应/Redis 造成的体积爆炸与索引错位问题。

[CHANGE-20260717-001 Pine parity] warmup/历史分离后：
    - 15m 计算 5000 根（4000 展示 + 1000 warmup），adapter 裁成 4000 展示
    - 1mo 扩展到 ≥200 根确保 ATR200 可初始化
    - adapter 仅裁剪/重基准，不改变事件类型、顺序、level 或几何

设计原则：
    - smc_pine_core 保持唯一纯核心和完整历史计算（不裁剪、不重基准）
    - smc_indicator 仅薄包装（向后兼容入口）
    - 本模块（service/view adapter）负责将完整结果裁成请求 bars 的展示 DTO
    - 索引重基准到展示窗口：完整索引 i → 展示索引 i - offset
      offset = max(0, total_bars - display_bars)
    - 与窗口相交的活跃 OB 即使 anchor 在窗口左侧也必须保留并标记 clipped_left=true
    - 响应大小必须与 bars 上限同阶
    - swing_bias 透传（已在 core 计算完成，adapter 不重新计算）
    - OB 顺序保持 core 输出的 newest-first（Pine unshift 语义）

FVG 完全排除：
    本模块不引入任何 FVG 字段；输入若含 FVG 字段会被原样过滤丢弃（防御性）。

不变性：
    - 完整计算结果（输入）不会被修改
    - 已确认事件一旦写入 core 即不可变；adapter 仅做投影/重基准，不改语义
"""

from __future__ import annotations

from typing import Any


def adapt_smc_to_display_dto(
    full_smc_result: dict[str, Any],
    display_bars: int,
) -> dict[str, Any]:
    """将完整 SMC 计算结果裁成展示窗口 DTO。

    Args:
        full_smc_result: smc_pine_core.compute_smc_pine 的完整返回
            必须包含: events, order_blocks, equal_highs_lows, trailing,
            swing_bias, pivots, time, params
        display_bars: 请求展示的 bar 数（前端可见窗口上限，与 indicators API
            的 `bars` 参数同源）。<=0 时返回空 DTO。

    Returns:
        dict 包含裁剪后的展示 DTO：
        - events: list[dict] BOS/CHoCH（anchor 或 confirmed 在窗口内）
        - order_blocks: list[dict] OB（与窗口相交；anchor 在窗口左侧时
            clipped_left=true，anchor_index 重基准后可能为负值，前端绘制
            时需 clamp 到 plotLeft）
        - equal_highs_lows: list[dict] EQH/EQL（anchor/second_pivot/confirmed
            任一在窗口内）
        - trailing: dict 索引重基准（bar_index 可能负）
        - swing_bias: int 透传 core 的 swing_trend.bias（1/-1/0）
        - pivots: list[dict] pivot（anchor 或 confirmed 在窗口内）
        - time: list[str] 仅窗口内时间
        - params: dict 透传
        - view: dict 视图元信息（total_bars/display_bars/offset/
            window_start/window_end），前端可用于一致性校验
    """
    times: list[str] = list(full_smc_result.get("time", []))
    total_bars = len(times)
    params = full_smc_result.get("params", {})

    if total_bars == 0 or display_bars <= 0:
        return _empty_dto(params)

    offset = max(0, total_bars - display_bars)
    window_start = offset            # 展示窗口在完整序列中的起始索引（含）
    window_end = total_bars          # 展示窗口在完整序列中的结束索引（不含）

    def _in_window(idx: Any) -> bool:
        if idx is None:
            return False
        try:
            i = int(idx)
        except (TypeError, ValueError):
            return False
        return window_start <= i < window_end

    def _rebase(idx: Any) -> int | None:
        if idx is None:
            return None
        try:
            i = int(idx)
        except (TypeError, ValueError):
            return None
        return i - offset

    # ----- events: anchor 或 confirmed 在窗口内 -----
    events_out: list[dict[str, Any]] = []
    for e in full_smc_result.get("events", []):
        a_idx = e.get("anchor_index")
        c_idx = e.get("confirmed_index")
        if not (_in_window(a_idx) or _in_window(c_idx)):
            continue
        out = dict(e)
        out["anchor_index"] = _rebase(a_idx)
        out["confirmed_index"] = _rebase(c_idx)
        # 防御性：过滤任何 fvg 字段（FVG 完全排除）
        for k in list(out.keys()):
            if "fvg" in str(k).lower():
                out.pop(k, None)
        events_out.append(out)

    # ----- equal_highs_lows: anchor/second_pivot/confirmed 任一在窗口内 -----
    eqhl_out: list[dict[str, Any]] = []
    for eq in full_smc_result.get("equal_highs_lows", []):
        a_idx = eq.get("anchor_index")
        sp_idx = eq.get("second_pivot_index")
        c_idx = eq.get("confirmed_index")
        if not (_in_window(a_idx) or _in_window(sp_idx) or _in_window(c_idx)):
            continue
        out = dict(eq)
        out["anchor_index"] = _rebase(a_idx)
        out["second_pivot_index"] = _rebase(sp_idx)
        out["confirmed_index"] = _rebase(c_idx)
        for k in list(out.keys()):
            if "fvg" in str(k).lower():
                out.pop(k, None)
        eqhl_out.append(out)

    # ----- pivots: anchor 或 confirmed 在窗口内 -----
    pivots_out: list[dict[str, Any]] = []
    for p in full_smc_result.get("pivots", []):
        a_idx = p.get("anchor_index")
        c_idx = p.get("confirmed_index")
        if not (_in_window(a_idx) or _in_window(c_idx)):
            continue
        out = dict(p)
        out["anchor_index"] = _rebase(a_idx)
        out["confirmed_index"] = _rebase(c_idx)
        for k in list(out.keys()):
            if "fvg" in str(k).lower():
                out.pop(k, None)
        pivots_out.append(out)

    # ----- order_blocks: 与窗口相交则保留；anchor 在窗口左侧标记 clipped_left -----
    # OB 时间范围：[anchor_index, mitigated_index 或 total_bars-1]
    # 相交条件：anchor_index < window_end 且 (mitigated is None 或 mitigated_index >= window_start)
    obs_out: list[dict[str, Any]] = []
    for ob in full_smc_result.get("order_blocks", []):
        a_idx = ob.get("anchor_index")
        if a_idx is None:
            continue
        try:
            a_int = int(a_idx)
        except (TypeError, ValueError):
            continue
        mit_idx = ob.get("mitigated_index")
        if mit_idx is None:
            ob_end_exclusive = total_bars  # 未 mitigated → 延伸到最右
        else:
            try:
                ob_end_exclusive = int(mit_idx) + 1
            except (TypeError, ValueError):
                ob_end_exclusive = total_bars

        # 不相交：OB 完全在窗口右侧 或 完全在窗口左侧
        if a_int >= window_end:
            continue
        if ob_end_exclusive - 1 < window_start:
            continue

        clipped_left = a_int < window_start
        out = dict(ob)
        # CHANGE-20260715-007: clipped_left 时 anchor_index clamp 到 0（展示窗口左端）
        # 前端根据 clipped_left 标记知道 OB 实际延伸到窗口左侧之外
        rebased_anchor = _rebase(a_int)
        if rebased_anchor is None:
            continue
        out["anchor_index"] = max(0, rebased_anchor) if clipped_left else rebased_anchor
        out["confirmed_index"] = _rebase(ob.get("confirmed_index"))
        out["mitigated_index"] = _rebase(ob.get("mitigated_index"))
        out["clipped_left"] = clipped_left
        for k in list(out.keys()):
            if "fvg" in str(k).lower():
                out.pop(k, None)
        obs_out.append(out)

    # ----- trailing: 索引重基准（bar_index 可能负） -----
    trailing_in = full_smc_result.get("trailing", {}) or {}
    trailing_out = dict(trailing_in)
    trailing_out["bar_index"] = _rebase(trailing_in.get("bar_index"))
    for k in list(trailing_out.keys()):
        if "fvg" in str(k).lower():
            trailing_out.pop(k, None)

    # ----- time: 仅窗口内 -----
    time_out = times[window_start:window_end]

    return {
        "events": events_out,
        "order_blocks": obs_out,
        "equal_highs_lows": eqhl_out,
        "trailing": trailing_out,
        "swing_bias": full_smc_result.get("swing_bias", 0),
        "pivots": pivots_out,
        "time": time_out,
        "params": params,
        "view": {
            "total_bars": total_bars,
            "display_bars": display_bars,
            "offset": offset,
            "window_start": window_start,
            "window_end": window_end,
        },
    }


def _empty_dto(params: dict[str, Any]) -> dict[str, Any]:
    """空 DTO（无数据或 display_bars<=0 时返回）。"""
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
        "params": params,
        "view": {
            "total_bars": 0,
            "display_bars": 0,
            "offset": 0,
            "window_start": 0,
            "window_end": 0,
        },
    }


# ===== 模块自测入口 =====

if __name__ == "__main__":
    # 自测：验证 adapter 行为（不依赖外部库）
    print("=== smc_view_adapter self-test ===")

    # 1. 空 DTO
    empty = adapt_smc_to_display_dto({}, 250)
    assert empty["events"] == []
    assert empty["order_blocks"] == []
    assert empty["swing_bias"] == 0
    assert empty["view"]["total_bars"] == 0
    print("空 DTO OK")

    # 2. display_bars<=0 返回空 DTO（view 字段全为 0）
    result = adapt_smc_to_display_dto(
        {"time": ["t1", "t2", "t3"], "events": [], "order_blocks": [],
         "equal_highs_lows": [], "pivots": [], "trailing": {}, "params": {}},
        0,
    )
    assert result["time"] == []
    assert result["view"]["total_bars"] == 0  # display_bars<=0 → 空 DTO
    assert result["swing_bias"] == 0  # display_bars<=0 → 空 DTO，swing_bias=0
    print("display_bars=0 OK")

    # 3. 索引重基准 + 窗口过滤
    full: dict[str, Any] = {
        "time": [f"t{i}" for i in range(10)],
        "events": [
            {"type": "BOS", "anchor_index": 2, "confirmed_index": 3, "level": 10.0},  # 在窗口前
            {"type": "BOS", "anchor_index": 7, "confirmed_index": 8, "level": 11.0},  # 在窗口内
            {"type": "BOS", "anchor_index": 9, "confirmed_index": 9, "level": 12.0},  # 边界
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
            # 完全在窗口右侧
            {"anchor_index": 9, "confirmed_index": 9, "mitigated_index": None,
             "bar_high": 14.0, "bar_low": 6.0, "bias": 1, "internal": True},
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
        "swing_bias": -1,  # bearish → Strong High
        "params": {"swings_length": 50},
    }
    # display_bars=5: 窗口是 [5, 10)
    out = adapt_smc_to_display_dto(full, 5)
    assert out["view"]["offset"] == 5
    assert out["view"]["window_start"] == 5
    assert out["view"]["window_end"] == 10
    assert out["time"] == ["t5", "t6", "t7", "t8", "t9"]

    # events: 第一条（2,3）在窗口外被丢；第二条（7,8）保留且重基准为 (2,3)；
    # 第三条（9,9）边界保留且重基准为 (4,4)
    assert len(out["events"]) == 2
    assert out["events"][0]["anchor_index"] == 2  # 7-5
    assert out["events"][0]["confirmed_index"] == 3  # 8-5
    assert out["events"][1]["anchor_index"] == 4  # 9-5
    assert out["events"][1]["confirmed_index"] == 4  # 9-5
    print("events 重基准 + 窗口过滤 OK")

    # OB:
    # - 第一条 (1,3) 完全在窗口左侧 → 丢
    # - 第二条 (2,None) anchor<5 但 mit=None 延伸到 10 → 跨左边界，保留，clipped_left=True，anchor=0
    # - 第三条 (6,None) 完全在窗口内 → 保留，clipped_left=False
    # - 第四条 (9,None) anchor=9 在窗口内但 ob_end=10 → 边界，保留
    assert len(out["order_blocks"]) == 3
    obs_by_anchor = sorted(out["order_blocks"], key=lambda o: o["anchor_index"])
    # 第二条：anchor=2-5=-3 但 clipped_left=True → clamp 到 0
    assert obs_by_anchor[0]["anchor_index"] == 0  # clamp(-3)
    assert obs_by_anchor[0]["clipped_left"] is True
    # 第三条：anchor=6-5=1 (clipped_left=False)
    assert obs_by_anchor[1]["anchor_index"] == 1  # 6-5
    assert obs_by_anchor[1]["clipped_left"] is False
    # 第四条：anchor=9-5=4
    assert obs_by_anchor[2]["anchor_index"] == 4  # 9-5
    print("OB clipped_left + 重基准 OK")

    # EQH/EQL: anchor=6, second_pivot=7, confirmed=8 都在窗口 → 保留且重基准
    assert len(out["equal_highs_lows"]) == 1
    eq = out["equal_highs_lows"][0]
    assert eq["anchor_index"] == 1  # 6-5
    assert eq["second_pivot_index"] == 2  # 7-5
    assert eq["confirmed_index"] == 3  # 8-5
    print("EQH/EQL 重基准 OK")

    # pivots
    assert len(out["pivots"]) == 1
    assert out["pivots"][0]["anchor_index"] == 2  # 7-5
    assert out["pivots"][0]["confirmed_index"] == 3  # 8-5
    print("pivots 重基准 OK")

    # trailing: bar_index 重基准（5-5=0）
    assert out["trailing"]["bar_index"] == 0
    assert out["trailing"]["top"] == 12.0  # 不变
    print("trailing 重基准 OK")

    # swing_bias 透传
    assert out["swing_bias"] == -1
    print("swing_bias 透传 OK")

    # 4. FVG 防御性过滤
    full_with_fvg = dict(full)
    full_with_fvg["events"] = full["events"] + [
        {"type": "BOS", "anchor_index": 7, "confirmed_index": 8, "fvg_field": 1.0},
    ]
    out_fvg = adapt_smc_to_display_dto(full_with_fvg, 5)
    for e in out_fvg["events"]:
        for k in e:
            assert "fvg" not in str(k).lower(), f"adapter 必须过滤 fvg 字段: {k}"
    print("FVG 防御性过滤 OK")

    print("✅ smc_view_adapter 自测通过")
