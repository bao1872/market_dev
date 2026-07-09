"""指标参数基线 - 所有指标参数的唯一真源。

用法:
    from app.constants.indicator_contract import VP_ROWS, DSA_LOOKBACK
    python -m app.constants.indicator_contract  # 打印所有参数供人工校验

说明:
    - 代码、manifest、测试和文档全部从此文件读取参数
    - 禁止在其他文件硬编码或定义第二套参数
    - 修改参数后运行 python tools/update_docs.py 重建文档
"""

from __future__ import annotations

from typing import Literal

# ===== 日线根数唯一字面量（advice.md v6 第4条：受控参数禁止散落硬编码）=====
# DAILY_HISTORY_BARS 是日线回看根数的唯一字面量，所有需要"取 250 根日线"的业务
# 必须引用本常量，禁止在其它文件再定义 250 字面量作为参数赋值。
# 受控参数清单（AST 一致性测试 test_no_duplicate_controlled_params 守门）：
#   - DAILY_HISTORY_BARS (=250)
#   - NODE_CLUSTER_15M_BARS_PER_DAY (=16)
#   - NODE_CLUSTER_LOW_BARS (=4000, =DAILY_HISTORY_BARS*NODE_CLUSTER_15M_BARS_PER_DAY)
#   - NODE_CLUSTER_EVENT_TTL_SECONDS (=600)
DAILY_HISTORY_BARS: int = 250

# ===== Node Cluster 每交易日 15m Bar 根数 =====
# 每个交易日有 16 根 15m Bar（4 根/小时 × 4 小时 = 16，A股交易日 4 小时）
NODE_CLUSTER_15M_BARS_PER_DAY: int = 16

# ===== Node Cluster / Volume Node 参数（advice.md 第3行）=====
NODE_CLUSTER_PRIMARY_PERIOD: str = "1d"
# [node_cluster] - 描述: 主周期（日线）取数根数，引用 DAILY_HISTORY_BARS 唯一字面量
NODE_CLUSTER_PRIMARY_BARS: int = DAILY_HISTORY_BARS
NODE_CLUSTER_LOW_PERIOD: str = "15m"
# [node_cluster] - 描述: 低周期（15m）取数根数 = DAILY_HISTORY_BARS × 每交易日 15m 根数 = 250×16=4000
NODE_CLUSTER_LOW_BARS: int = DAILY_HISTORY_BARS * NODE_CLUSTER_15M_BARS_PER_DAY
NODE_CLUSTER_MINUTE_BARS: int = 2

# Volume Profile 算法参数
VP_ROWS: int = 100
VP_VALUE_AREA_PCT: float = 0.70
VP_PEAK_DETECTION_PCT: float = 0.05
VP_NODE_THRESHOLD_PCT: float = 0.01
VP_TROUGHS_SHOW: Literal["troughs", "clusters", "none"] = "none"
VP_TROUGHS_DETECTION_PCT: float = 0.07
VP_HIGHEST_N_NODES: int = 0
VP_LOWEST_N_NODES: int = 0

# 事件 TTL
NODE_CLUSTER_EVENT_TTL_SECONDS: int = 600

# ===== DSA 参数 =====
# [dsa] - 描述: DSA 回看根数，引用 DAILY_HISTORY_BARS 唯一字面量
DSA_LOOKBACK: int = DAILY_HISTORY_BARS
DSA_BUDGET_MS: int = 100

# ===== 图表行情输入参数（advice.md v5 口径）=====
# [chart_bars] - 描述: load_chart_bars 服务统一为 /bars 和 indicator_service 提供日线行情输入
# 引用 DAILY_HISTORY_BARS 唯一字面量，与 DSA_LOOKBACK、INDICATOR_BARS["1d"] 保持一致，禁止散落硬编码
CHART_BARS_COUNT: int = DAILY_HISTORY_BARS

# ===== Bollinger Bands 参数 =====
BB_WIN: int = 20
BB_K: float = 2.0
BB_EVENT_TTL_SECONDS: int = 600

# ===== 各周期指标计算根数 =====
# [indicator_contract] - 描述: 按 advice.md 口径，1d=250（引用 DAILY_HISTORY_BARS，与 DSA_LOOKBACK 一致），
# 15m=NODE_CLUSTER_LOW_BARS（15m 指标即 Node Cluster 指标，引用 Node 输入常量），
# 1h=1200（1h 指标窗口与 Node Cluster 15m 输入语义不同，解绑），
# 1m=2（穿越检测）
INDICATOR_BARS: dict[str, int] = {
    "1d": DAILY_HISTORY_BARS,
    "15m": NODE_CLUSTER_LOW_BARS,
    "1h": 1200,
    "1w": 260,
    "1mo": 120,
    "1m": 2,
}

# ===== Token 有效期（供参考，实际值在 config.py）=====
JWT_ACCESS_TTL_SECONDS: int = 3600
JWT_REFRESH_TTL_SECONDS: int = 604800


def all_params() -> dict[str, object]:
    """返回所有参数的字典视图，供文档生成与一致性测试使用。"""
    return {
        "DAILY_HISTORY_BARS": DAILY_HISTORY_BARS,
        "NODE_CLUSTER_15M_BARS_PER_DAY": NODE_CLUSTER_15M_BARS_PER_DAY,
        "NODE_CLUSTER_PRIMARY_PERIOD": NODE_CLUSTER_PRIMARY_PERIOD,
        "NODE_CLUSTER_PRIMARY_BARS": NODE_CLUSTER_PRIMARY_BARS,
        "NODE_CLUSTER_LOW_PERIOD": NODE_CLUSTER_LOW_PERIOD,
        "NODE_CLUSTER_LOW_BARS": NODE_CLUSTER_LOW_BARS,
        "NODE_CLUSTER_MINUTE_BARS": NODE_CLUSTER_MINUTE_BARS,
        "VP_ROWS": VP_ROWS,
        "VP_VALUE_AREA_PCT": VP_VALUE_AREA_PCT,
        "VP_PEAK_DETECTION_PCT": VP_PEAK_DETECTION_PCT,
        "VP_NODE_THRESHOLD_PCT": VP_NODE_THRESHOLD_PCT,
        "VP_TROUGHS_SHOW": VP_TROUGHS_SHOW,
        "VP_TROUGHS_DETECTION_PCT": VP_TROUGHS_DETECTION_PCT,
        "VP_HIGHEST_N_NODES": VP_HIGHEST_N_NODES,
        "VP_LOWEST_N_NODES": VP_LOWEST_N_NODES,
        "NODE_CLUSTER_EVENT_TTL_SECONDS": NODE_CLUSTER_EVENT_TTL_SECONDS,
        "DSA_LOOKBACK": DSA_LOOKBACK,
        "DSA_BUDGET_MS": DSA_BUDGET_MS,
        "CHART_BARS_COUNT": CHART_BARS_COUNT,
        "BB_WIN": BB_WIN,
        "BB_K": BB_K,
        "BB_EVENT_TTL_SECONDS": BB_EVENT_TTL_SECONDS,
        "INDICATOR_BARS": dict(INDICATOR_BARS),
        "JWT_ACCESS_TTL_SECONDS": JWT_ACCESS_TTL_SECONDS,
        "JWT_REFRESH_TTL_SECONDS": JWT_REFRESH_TTL_SECONDS,
    }


if __name__ == "__main__":
    params = all_params()
    print("=" * 60)
    print("指标参数基线 (indicator_contract.py)")
    print("=" * 60)
    for key, value in params.items():
        print(f"  {key} = {value!r}")
    print("=" * 60)
    print(f"共 {len(params)} 项参数")
