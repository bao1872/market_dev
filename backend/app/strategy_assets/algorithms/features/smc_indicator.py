"""SMC 指标计算模块 — 薄包装层，委托到 smc_pine_core。

本模块是向后兼容的入口。真正的 Pine 语义核心在 smc_pine_core.py。
生产服务和测试参考均调用 smc_pine_core 的 compute_smc_pine，禁止维护两套近似算法。

FVG 完全排除：
    Fair Value Gap 不计算、不返回、不缓存、不渲染，也不暴露 FVG 开关。
    生产计算路径不包含 FVG 函数或状态；输出结构中不存在 FVG 相关键、
    事件或 box。注释/文档可以正常写"FVG 不计算、不返回、不显示"。

Pine 语义兼容：
    本模块通过 smc_pine_core 实现 Pine 语义原语（ta.rma/ta.atr/ta.cum 等），
    默认参数逐项匹配原始 Pine，warmup 至少展示区之前 500 根。
    详见 docs/analysis/smc-pine-parity.md。
"""

from __future__ import annotations

from typing import Any

from app.strategy_assets.algorithms.features.smc_pine_core import (
    ATR,
    DEFAULT_PARAMS,
    HIGHLOW,
    _SMCPineState,
    compute_smc_pine,
)

# 向后兼容别名
_SMCState = _SMCPineState


def compute_smc_indicators(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    times: list[str],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """计算 SMC 指标（委托到 smc_pine_core.compute_smc_pine）。

    完全排除 FVG。Pine 语义核心实现详见 smc_pine_core.py。

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
    return compute_smc_pine(opens, highs, lows, closes, times, params)


# ===== 模块自测入口 =====

if __name__ == "__main__":
    import inspect

    # 1. 验证 compute_smc_indicators 函数签名
    assert callable(compute_smc_indicators), "compute_smc_indicators 应可调用"
    sig = inspect.signature(compute_smc_indicators)
    params = list(sig.parameters.keys())
    expected = ["opens", "highs", "lows", "closes", "times", "params"]
    assert params == expected, f"参数不匹配: {params} != {expected}"
    print(f"compute_smc_indicators params={params} OK")

    # 2. 验证默认参数
    assert DEFAULT_PARAMS["swings_length"] == 50
    assert DEFAULT_PARAMS["equal_length"] == 3
    assert DEFAULT_PARAMS["equal_threshold"] == 0.1
    assert DEFAULT_PARAMS["order_block_filter"] == ATR
    assert DEFAULT_PARAMS["order_block_mitigation"] == HIGHLOW
    assert DEFAULT_PARAMS["show_internal_order_blocks"] is True
    assert DEFAULT_PARAMS["show_swing_order_blocks"] is False
    assert DEFAULT_PARAMS["show_equal_hl"] is True
    assert DEFAULT_PARAMS["show_high_low_swings"] is True
    print(f"DEFAULT_PARAMS={DEFAULT_PARAMS} OK")

    # 3. 验证空数据
    empty_result = compute_smc_indicators([], [], [], [], [])
    assert empty_result["events"] == []
    assert empty_result["order_blocks"] == []
    assert empty_result["pivots"] == []
    assert empty_result["time"] == []
    print("空数据处理 OK")

    # 4. 验证 FVG 完全排除
    result = compute_smc_indicators(
        [10.0] * 60, [11.0] * 60, [9.0] * 60, [10.5] * 60,
        [f"2026-01-{i:02d}" for i in range(1, 61)],
    )
    assert "fvg" not in result, "输出不得包含 fvg 键"
    for key in result:
        assert "fvg" not in str(key).lower(), f"输出键含 fvg: {key}"
    print("FVG 完全排除 OK")

    print("✅ smc_indicator 自测通过")
