"""复权因子算法合同常量（CHANGE-20260718-005 全市场一致性修复）。

冻结的复权算法版本与对账版本。任何 adj_factor 计算公式变化必须 bump
`FACTOR_ALGORITHM_VERSION`；任何一致性审计/对账逻辑变化必须 bump
`FACTOR_RECONCILIATION_VERSION`。版本变化时必须触发全市场因子一致性审计，
从最早受影响日期完整重建不一致股票的因子序列（禁止只修最近 N 根）。

为什么需要版本字段（系统缺口修复）：
- 现有 `AdjustmentFactorService.detect_company_action_change` 仅检测 xdxr fingerprint
  变化。若 fingerprint 未变但历史因子序列已错误（如过去 bug、部分更新、1.0 伪装），
  系统无法发现存量错误。
- `FACTOR_ALGORITHM_VERSION` 变化时，即使 fingerprint 未变也必须重新审计全市场。
- `FACTOR_RECONCILIATION_VERSION` 变化时，审计/对账逻辑本身已升级，必须重跑。

版本字段存储位置：
- instruments 表新增列（迁移 065）：factor_algorithm_version / factor_reconciliation_version /
  factor_reconciled_at，记录该股票上次成功对账时使用的版本。
- 审计时若存储版本 < 当前常量版本，标记为 needs_reaudit（即使因子值看起来正确）。

用法：
    from app.constants.factor_contract import (
        FACTOR_ALGORITHM_VERSION,
        FACTOR_RECONCILIATION_VERSION,
        FACTOR_COMPARISON_TOLERANCE,
    )
"""

from __future__ import annotations

from typing import Final

# 复权算法版本（Chanlunpro preclose 公式）
# qfq = raw × adj_factor，adj_factor = 累积事件因子，最新日期 = 1.0
# 公式变更时 bump（如改用后复权、改用不同 preclose 公式、改用交易所官方因子）
FACTOR_ALGORITHM_VERSION: Final[str] = "fq-v1"

# 因子对账版本（审计/对账逻辑变更时 bump）
# 审计逻辑变化（如新增 mismatch 分类、改变比较阈值、改变 expected 计算方式）时 bump
FACTOR_RECONCILIATION_VERSION: Final[int] = 1

# 因子值比较容差（float 比较，避免 Decimal/float 精度差异误报）
# bars_daily.adj_factor 存储为 Decimal(20,10)，_calculate_adj_factor 返回 float。
# 1e-6 容差覆盖 Decimal→float 转换误差，同时捕获真实因子错误（通常 >0.001）
FACTOR_COMPARISON_TOLERANCE: Final[float] = 1e-6

# 因子全 1.0 但有除权除息事件的判定阈值
# 若 adj_factor 全为 1.0 但 xdxr 有 category=1 事件，且事件因子偏离 1.0 超过此阈值，
# 判定为 factor_all_unit_with_events（603538/利通电子 bug 模式）
FACTOR_ALL_UNIT_EVENT_THRESHOLD: Final[float] = 1e-6


if __name__ == "__main__":
    # 自测：验证常量定义
    assert FACTOR_ALGORITHM_VERSION == "fq-v1"
    assert FACTOR_RECONCILIATION_VERSION == 1
    assert FACTOR_COMPARISON_TOLERANCE > 0
    print(f"FACTOR_ALGORITHM_VERSION={FACTOR_ALGORITHM_VERSION}")
    print(f"FACTOR_RECONCILIATION_VERSION={FACTOR_RECONCILIATION_VERSION}")
    print(f"FACTOR_COMPARISON_TOLERANCE={FACTOR_COMPARISON_TOLERANCE}")
    print("OK")
