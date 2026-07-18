"""指标语义合同（frozen）— Node Cluster 唯一语义真源。

本文件冻结 Node Cluster 的业务语义（"是什么/怎么做"），与 `indicator_contract.py`
（数值参数真源，"用多少"）分离。两者共同构成 Node Cluster 计算合同：

- `app.constants.indicator_contract`: 数值参数（根数/行数/阈值/TTL）
- `app.contracts.indicator_semantics`: 语义合同（输入口径/过滤规则/输出口径）
- `app.services.node_cluster_engine`: 计算内核（唯一业务入口，调用底层 VP）

冻结的语义项（任何变更必须 bump `NODE_CLUSTER_CONTRACT_FINGERPRINT`）：
1. 1d 最近 250 根已完成 qfq 日线决定价格范围
2. 15m 最近 4000 根已完成 qfq bar 分配成交量
3. 1m 最近 2 根已完成 bar 只用于盘中穿越检测
4. Peak 搜索域为完整 100 行 Profile
5. `value_area_filters_peaks = False`（VA 外 Peak 有效，禁止过滤）
6. VAL/VAH 仅用于价值区显示/位置分类，不得排除 VA 外 Peak
7. nearest node 来自全部 Peak（含 VA 外）
8. 三链（盘后 / 详情 / 监控）同 stock/as_of/输入 → profile_hash 必须完全一致

复权语义：
- daily/15m: completed qfq bars（adjustment_as_of 锚定业务日，禁止未来除权事件泄漏）
- 监控链 1m: include_realtime=True（实时穿越），但仍用 completed daily/15m 计算 Profile

用法：
    from app.contracts.indicator_semantics import (
        NODE_CLUSTER_ALGORITHM_VERSION,
        NODE_CLUSTER_OUTPUT_SCHEMA_VERSION,
        NODE_CLUSTER_CONTRACT_FINGERPRINT,
        NODE_CLUSTER_SEMANTIC_INVARIANTS,
    )
"""

from __future__ import annotations

from typing import Final

# ===== Node Cluster 算法版本（engine 算法变更时 bump）=====
# 历史版本：
# - nc-v1: 初始版本（CHANGE-20260718-004），统一三链 + engine 唯一入口
NODE_CLUSTER_ALGORITHM_VERSION: Final[str] = "nc-v1"

# ===== Node Cluster 输出 schema 版本（NodeClusterProfileResult 字段变更时 bump）=====
# 历史版本：
# - 1: 初始版本（CHANGE-20260718-004）
NODE_CLUSTER_OUTPUT_SCHEMA_VERSION: Final[int] = 1

# ===== Node Cluster 合同指纹（语义变更时 bump，自动失效缓存）=====
# 任何对下方 SEMANTIC_INVARIANTS 的语义修改都必须 bump 此指纹。
# engine 缓存键含此指纹，指纹变化使旧缓存自动失效。
# 历史指纹：
# - nc-cf-v1: 初始版本（CHANGE-20260718-004）
NODE_CLUSTER_CONTRACT_FINGERPRINT: Final[str] = "nc-cf-v1"

# ===== 冻结的语义不变量（docstring + 常量，禁止运行时修改）=====
NODE_CLUSTER_SEMANTIC_INVARIANTS: Final[tuple[str, ...]] = (
    # 输入口径
    "1d 最近 250 根已完成 qfq 日线决定价格范围",
    "15m 最近 4000 根已完成 qfq bar 分配成交量",
    "1m 最近 2 根已完成 bar 只用于盘中穿越检测",
    # Profile 计算口径
    "Peak 搜索域为完整 100 行 Profile",
    "value_area_filters_peaks = False（VA 外 Peak 有效，禁止过滤）",
    "VAL/VAH 仅用于价值区显示/位置分类，不得排除 VA 外 Peak",
    "nearest node 来自全部 Peak（含 VA 外）",
    # 三链同核
    "三链（盘后 / 详情 / 监控）同 stock/as_of/输入 → profile_hash 必须完全一致",
)

# ===== 复权语义（与 MDAS 契约对齐）=====
# daily/15m: completed qfq（adjustment_as_of 锚定业务日）
# 监控链 1m: include_realtime=True（实时穿越），但 Profile 仍用 completed daily/15m
NODE_CLUSTER_ADJUSTMENT_MODE: Final[str] = "qfq"  # 三链统一前复权
NODE_CLUSTER_COMPLETED_ONLY: Final[bool] = True  # daily/15m 只用已完成 bar


def all_semantics() -> dict[str, object]:
    """返回所有语义常量的字典视图，供文档生成与一致性测试使用。"""
    return {
        "NODE_CLUSTER_ALGORITHM_VERSION": NODE_CLUSTER_ALGORITHM_VERSION,
        "NODE_CLUSTER_OUTPUT_SCHEMA_VERSION": NODE_CLUSTER_OUTPUT_SCHEMA_VERSION,
        "NODE_CLUSTER_CONTRACT_FINGERPRINT": NODE_CLUSTER_CONTRACT_FINGERPRINT,
        "NODE_CLUSTER_SEMANTIC_INVARIANTS": list(NODE_CLUSTER_SEMANTIC_INVARIANTS),
        "NODE_CLUSTER_ADJUSTMENT_MODE": NODE_CLUSTER_ADJUSTMENT_MODE,
        "NODE_CLUSTER_COMPLETED_ONLY": NODE_CLUSTER_COMPLETED_ONLY,
    }


if __name__ == "__main__":
    sem = all_semantics()
    print("=" * 60)
    print("指标语义合同 (indicator_semantics.py)")
    print("=" * 60)
    for key, value in sem.items():
        if key == "NODE_CLUSTER_SEMANTIC_INVARIANTS":
            print(f"  {key}:")
            for item in value:  # type: ignore[union-attr]
                print(f"    - {item}")
        else:
            print(f"  {key} = {value!r}")
    print("=" * 60)
    print(f"共 {len(sem)} 项语义常量，{len(NODE_CLUSTER_SEMANTIC_INVARIANTS)} 条不变量")