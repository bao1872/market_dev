"""Capture 链路常量。

用途：避免服务层直接 import FastAPI deps 模块（app.core.deps）带来的循环依赖风险，
统一维护 capture token / screenshot 相关的纯常量。
"""

from __future__ import annotations

from typing import Any

# [Capture] - 描述: stock_detail 截图链路作用域（advice.md 第六节硬规则）
CAPTURE_SCOPE_STOCK_DETAIL = "stock_detail_capture"

# [Feishu] - 描述: 飞书盘中截图业务默认周期（盘迹硬规则，CHANGE-20260710-002 确立）
# 盘中监控触发只依赖最新已完成 1m bar；飞书盘中截图默认展示 1d（日线）。
# 实时性由 Capture Snapshot 1d + include_realtime=True 的 partial daily 合成保证，
# 截图修复不得改变 watchlist_monitor 事件计算口径。
# Capture API 支持多周期（1d/15m/1h/...）是能力，不等于飞书业务默认 15m。
FEISHU_CAPTURE_TIMEFRAME = "1d"

# [CHANGE-20260720-003 §三] 三套 Capture Preset - 一张图只渲染一个指标视图
# 禁止三类指标叠在同一张图；前端 /capture/stock/{symbol} 页面根据 indicator_view
# 切换图层组合，每个 preset 描述该视图的 timeframe / chart_version / 渲染图层。
#
# 字段说明：
# - indicator_view: 视图枚举（与 app.constants.indicator_view.INDICATOR_VIEW_VALUES 对齐）
# - timeframe: 飞书截图业务默认周期（统一 1d，实时性由 partial daily 合成保证）
# - chart_version: 图表版本号（缓存键维度，版本变更强制刷新缓存）
# - layers: 该视图渲染的图层列表（前端按图层名决定显隐）
#     * candlestick: 日线 K 线（所有视图共享）
#     * volume: 成交量（node_cluster 视图展示）
#     * profile / poc / peak_node / trigger_node: node_cluster 专属
#     * bb_upper / bb_mid / bb_lower / trigger_band: bollinger 专属
#     * bos / choch / ob / eqh_eql / strong_weak / trigger_entity: smc 专属
# - ready_check: 该视图 Ready 检查条件（PROMPT.md §三 "分类型Ready检查"）
#     * node_cluster: profile_rows == 100 且 profile_hash 存在
#     * bollinger: 三条轨道存在且 frame 匹配
#     * smc: SMC DTO 加载完成，日线周期和算法版本匹配
FEISHU_CAPTURE_PRESETS: dict[str, dict[str, Any]] = {
    "node_cluster": {
        "indicator_view": "node_cluster",
        "timeframe": FEISHU_CAPTURE_TIMEFRAME,
        "chart_version": "v1",
        "layers": [
            "candlestick",
            "volume",
            "profile",
            "poc",
            "peak_node",
            "trigger_node",
        ],
        "ready_check": {
            "field": "profile_hash",
            "condition": "exists",
            "min_profile_rows": 100,
        },
    },
    "bollinger": {
        "indicator_view": "bollinger",
        "timeframe": FEISHU_CAPTURE_TIMEFRAME,
        "chart_version": "v1",
        "layers": [
            "candlestick",
            "bb_upper",
            "bb_mid",
            "bb_lower",
            "trigger_band",
        ],
        "ready_check": {
            "field": "bb_snapshot",
            "condition": "all_bands_present",
        },
    },
    "smc": {
        "indicator_view": "smc",
        "timeframe": FEISHU_CAPTURE_TIMEFRAME,
        "chart_version": "v1",
        "layers": [
            "candlestick",
            "bos",
            "choch",
            "ob",
            "eqh_eql",
            "strong_weak",
            "trigger_entity",
        ],
        "ready_check": {
            "field": "smc_dto",
            "condition": "loaded_and_version_match",
        },
    },
}


def get_capture_preset(indicator_view: str) -> dict[str, Any]:
    """按 indicator_view 获取 Capture Preset。

    Args:
        indicator_view: node_cluster | bollinger | smc

    Returns:
        Preset dict（含 indicator_view/timeframe/chart_version/layers/ready_check）

    Raises:
        ValueError: 未知 indicator_view
    """
    if indicator_view not in FEISHU_CAPTURE_PRESETS:
        raise ValueError(
            f"未知 indicator_view: {indicator_view!r}, "
            f"合法值: {list(FEISHU_CAPTURE_PRESETS.keys())}"
        )
    return FEISHU_CAPTURE_PRESETS[indicator_view]


if __name__ == "__main__":
    # 自测入口：验证常量值
    assert CAPTURE_SCOPE_STOCK_DETAIL == "stock_detail_capture"
    assert FEISHU_CAPTURE_TIMEFRAME == "1d"

    # 验证三套 Preset 完整性
    assert set(FEISHU_CAPTURE_PRESETS.keys()) == {"node_cluster", "bollinger", "smc"}
    for view, preset in FEISHU_CAPTURE_PRESETS.items():
        assert preset["indicator_view"] == view
        assert preset["timeframe"] == FEISHU_CAPTURE_TIMEFRAME
        assert preset["chart_version"] == "v1"
        assert isinstance(preset["layers"], list) and len(preset["layers"]) > 0
        assert "ready_check" in preset
        # 所有 preset 必须含 candlestick 基础图层
        assert "candlestick" in preset["layers"]
        # 不同视图的 layers 必须互斥（除 candlestick 共享外）
        non_candle = {layer for layer in preset["layers"] if layer != "candlestick"}

    # 三视图 layers 互斥校验（除 candlestick）
    nc_layers = {layer for layer in FEISHU_CAPTURE_PRESETS["node_cluster"]["layers"] if layer != "candlestick"}
    bb_layers = {layer for layer in FEISHU_CAPTURE_PRESETS["bollinger"]["layers"] if layer != "candlestick"}
    smc_layers = {layer for layer in FEISHU_CAPTURE_PRESETS["smc"]["layers"] if layer != "candlestick"}
    assert nc_layers & bb_layers == set(), f"node_cluster 与 bollinger layers 重叠: {nc_layers & bb_layers}"
    assert nc_layers & smc_layers == set(), f"node_cluster 与 smc layers 重叠: {nc_layers & smc_layers}"
    assert bb_layers & smc_layers == set(), f"bollinger 与 smc layers 重叠: {bb_layers & smc_layers}"

    # get_capture_preset
    preset = get_capture_preset("smc")
    assert preset["indicator_view"] == "smc"
    assert "bos" in preset["layers"]

    try:
        get_capture_preset("invalid")
    except ValueError as e:
        assert "未知 indicator_view" in str(e)
    else:
        raise AssertionError("未知 indicator_view 应抛 ValueError")

    print(f"CAPTURE_SCOPE_STOCK_DETAIL={CAPTURE_SCOPE_STOCK_DETAIL}")
    print(f"FEISHU_CAPTURE_TIMEFRAME={FEISHU_CAPTURE_TIMEFRAME}")
    print(f"FEISHU_CAPTURE_PRESETS ({len(FEISHU_CAPTURE_PRESETS)} 项) ✓")
    print("OK")
