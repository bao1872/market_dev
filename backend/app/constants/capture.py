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

# [CHANGE-20260728-010] 新业务唯一 Capture Preset - 结构 + 筹码共识组合视图
# 任一结构事件或筹码共识事件触发时，截图固定使用本 preset，同时展示两类指标。
# 事件类别只决定 focus_event 与文字内容，不再决定截图图层组合。
#
# 历史 FEISHU_CAPTURE_PRESETS（node_cluster / bollinger / smc 三套独立视图）
# 已被本组合 preset 替代；旧 preset 保留读取兼容，新业务只写 structure_node。
#
# 字段说明：
# - indicator_view: 视图枚举（FEISHU_CAPTURE_VIEW="structure_node"）
# - timeframe: 飞书截图业务默认周期（1d，实时性由 partial daily 合成保证）
# - chart_version: 图表版本号（缓存键维度，版本变更强制刷新缓存）
# - layers: 该视图渲染的图层列表（前端按图层名决定显隐）
#     * candlestick: 日线 K 线（所有视图共享）
#     * volume: 成交量
#     * profile / poc / peak_node / trigger_node: 筹码共识专属
#     * bos / choch / ob / eqh_eql / strong_weak / trigger_entity: 结构专属
# - ready_check: 该视图 Ready 检查条件
#     * structure_node: Node 数据完整且 SMC DTO 结构存在（SMC 数组允许为空）
#       combined Ready = nodeReady && smcContractReady，SMC 无事件不导致永久 loading
FEISHU_CAPTURE_VIEW = "structure_node"

FEISHU_CAPTURE_PRESET: dict[str, Any] = {
    "indicator_view": FEISHU_CAPTURE_VIEW,
    "timeframe": FEISHU_CAPTURE_TIMEFRAME,
    "chart_version": "v1",
    "layers": [
        # 基础图层
        "candlestick",
        "volume",
        # 筹码共识图层
        "profile",
        "poc",
        "peak_node",
        "trigger_node",
        # 结构图层
        "bos",
        "choch",
        "ob",
        "eqh_eql",
        "strong_weak",
        "trigger_entity",
    ],
    "ready_check": {
        # 组合 Ready：Node 数据完整 + SMC DTO 结构存在（SMC 数组允许为空）
        "field": "node_and_smc",
        "condition": "node_complete_and_smc_dto_present",
        "min_profile_rows": 100,
    },
    # 组合 preset 标识（与历史三套 preset 区分）
    "combined": True,
    "label": "结构 + 筹码共识",
}

# [历史兼容] 旧三套 Capture Preset - 仅供历史数据回读，新业务不再写入
# 保留以兼容历史 CaptureJob.indicator_view 读取与前端旧 URL 参数兼容；
# 新链路直接使用 FEISHU_CAPTURE_PRESET（structure_node）。
FEISHU_CAPTURE_PRESETS: dict[str, dict[str, Any]] = {
    "structure_node": FEISHU_CAPTURE_PRESET,
    # 历史 preset（仅读取兼容）
    "node_cluster": {
        "indicator_view": "node_cluster",
        "timeframe": FEISHU_CAPTURE_TIMEFRAME,
        "chart_version": "v1",
        "layers": [
            "candlestick", "volume", "profile", "poc", "peak_node", "trigger_node",
        ],
        "ready_check": {
            "field": "profile_hash",
            "condition": "exists",
            "min_profile_rows": 100,
        },
        "_legacy": True,
    },
    "bollinger": {
        "indicator_view": "bollinger",
        "timeframe": FEISHU_CAPTURE_TIMEFRAME,
        "chart_version": "v1",
        "layers": [
            "candlestick", "bb_upper", "bb_mid", "bb_lower", "trigger_band",
        ],
        "ready_check": {
            "field": "bb_snapshot",
            "condition": "all_bands_present",
        },
        "_legacy": True,
    },
    "smc": {
        "indicator_view": "smc",
        "timeframe": FEISHU_CAPTURE_TIMEFRAME,
        "chart_version": "v1",
        "layers": [
            "candlestick", "bos", "choch", "ob", "eqh_eql",
            "strong_weak", "trigger_entity",
        ],
        "ready_check": {
            "field": "smc_dto",
            "condition": "loaded_and_version_match",
        },
        "_legacy": True,
    },
}

# [CHANGE-20260728-010] 截图调用方 timeout（秒）
# 必须 > Capture 渲染最大 90 秒，固定设为 120 秒
CAPTURE_HTTP_TIMEOUT_SECONDS: int = 120


def get_capture_preset(indicator_view: str) -> dict[str, Any]:
    """按 indicator_view 获取 Capture Preset。

    [CHANGE-20260728-010] 新业务应直接使用 FEISHU_CAPTURE_PRESET 常量
    或 get_capture_preset(FEISHU_CAPTURE_VIEW)。本函数保留以兼容历史调用。

    Args:
        indicator_view: node_cluster | bollinger | smc | structure_node

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
    assert FEISHU_CAPTURE_VIEW == "structure_node"
    assert CAPTURE_HTTP_TIMEOUT_SECONDS == 120

    # 验证组合 Preset 完整性
    assert FEISHU_CAPTURE_PRESET["indicator_view"] == "structure_node"
    assert FEISHU_CAPTURE_PRESET["combined"] is True
    assert FEISHU_CAPTURE_PRESET["label"] == "结构 + 筹码共识"
    assert FEISHU_CAPTURE_PRESET["timeframe"] == FEISHU_CAPTURE_TIMEFRAME
    assert FEISHU_CAPTURE_PRESET["chart_version"] == "v1"
    layers = FEISHU_CAPTURE_PRESET["layers"]
    assert "candlestick" in layers
    # 筹码共识图层
    assert "profile" in layers and "poc" in layers
    assert "peak_node" in layers and "trigger_node" in layers
    # 结构图层
    assert "bos" in layers and "choch" in layers and "ob" in layers
    assert "eqh_eql" in layers and "trigger_entity" in layers
    # ready_check
    rc = FEISHU_CAPTURE_PRESET["ready_check"]
    assert rc["field"] == "node_and_smc"
    assert rc["min_profile_rows"] == 100
    print(f"FEISHU_CAPTURE_PRESET ✓ (layers={len(layers)} 项)")

    # 验证历史兼容 preset 仍可读取
    assert set(FEISHU_CAPTURE_PRESETS.keys()) == {
        "structure_node", "node_cluster", "bollinger", "smc",
    }
    for view, preset in FEISHU_CAPTURE_PRESETS.items():
        assert preset["indicator_view"] == view
        assert preset["timeframe"] == FEISHU_CAPTURE_TIMEFRAME
        assert isinstance(preset["layers"], list) and len(preset["layers"]) > 0
        assert "candlestick" in preset["layers"]
    # 历史 preset 标记 _legacy
    assert FEISHU_CAPTURE_PRESETS["node_cluster"].get("_legacy") is True
    assert FEISHU_CAPTURE_PRESETS["bollinger"].get("_legacy") is True
    assert FEISHU_CAPTURE_PRESETS["smc"].get("_legacy") is True
    # 新 preset 未标记 _legacy
    assert "_legacy" not in FEISHU_CAPTURE_PRESET
    print(f"FEISHU_CAPTURE_PRESETS ({len(FEISHU_CAPTURE_PRESETS)} 项) ✓")

    # get_capture_preset
    preset = get_capture_preset("structure_node")
    assert preset["indicator_view"] == "structure_node"
    preset_smc = get_capture_preset("smc")
    assert preset_smc["indicator_view"] == "smc"

    try:
        get_capture_preset("invalid")
    except ValueError as e:
        assert "未知 indicator_view" in str(e)
    else:
        raise AssertionError("未知 indicator_view 应抛 ValueError")

    print(f"CAPTURE_SCOPE_STOCK_DETAIL={CAPTURE_SCOPE_STOCK_DETAIL}")
    print(f"FEISHU_CAPTURE_TIMEFRAME={FEISHU_CAPTURE_TIMEFRAME}")
    print(f"FEISHU_CAPTURE_VIEW={FEISHU_CAPTURE_VIEW}")
    print(f"CAPTURE_HTTP_TIMEOUT_SECONDS={CAPTURE_HTTP_TIMEOUT_SECONDS}")
    print("OK")
