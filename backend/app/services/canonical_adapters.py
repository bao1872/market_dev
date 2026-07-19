"""Canonical 统一 adapter 层 — 把签名各异的 kernel 包为统一 (bars_daily: pd.DataFrame, **kwargs) 签名。

CHANGE-20260718-007 S3.2：每个算法使用统一 adapter 签名，使 CanonicalComputationService
能经 compute_with_mdas() 统一调度，无需调用方了解各 kernel 的参数差异。

设计：
- adapter 是薄封装，不改算法公式（SMC/DSA/Node 不动）
- adapter 接受 MDAS 返回的 pd.DataFrame（含 open/high/low/close/volume/amount/adj_factor 列）
- adapter 提取所需列后调用真实 kernel
- adapter 返回值即 kernel 返回值（Canonical 计算 result_hash）

统一签名约定：
    def compute_<algo>_adapter(bars_daily: pd.DataFrame, **params) -> Any

注册：在 algorithm_registry.py 把 kernel_entrypoint 指向 adapter，并设
migration_status="input_provider_wired"。

新增 adapter 时：
1. 在本文件定义 compute_<algo>_adapter
2. 在 algorithm_registry.py 更新对应 AlgorithmContract 的 kernel_entrypoint + migration_status
3. 运行 test_algorithm_registry_architecture.py::test_wired_algorithms_have_existing_callables 验证
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from app.services.indicator_service import compute_macd

logger = logging.getLogger("services.canonical_adapters")


def compute_macd_adapter(
    bars: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, list[float | None]]:
    """MACD 统一 adapter — 从 bars 提取 close 后调用 compute_macd。

    统一签名：接受 MDAS 返回的 DataFrame（任意周期：1d/15m/1h/1w/1mo），
    提取 close 列为 numpy 数组，转发到 compute_macd
    （A 股 2× 版本：DIF=EMA(fast)-EMA(slow), DEA=EMA(DIF,signal), HIST=2*(DIF-DEA)）。

    Args:
        bars: MDAS 返回的 DataFrame，必须含 "close" 列（周期由合同 input_timeframes 约束）
        fast: 快线周期（默认 12）
        slow: 慢线周期（默认 26）
        signal: 信号线周期（默认 9）

    Returns:
        dict: macd_dif / macd_dea / macd_hist 数组（与 compute_macd 返回一致）

    Raises:
        ValueError: bars 为空或缺少 close 列
    """
    if bars is None or bars.empty:
        raise ValueError("compute_macd_adapter: bars 为空，无法计算 MACD")
    if "close" not in bars.columns:
        raise ValueError(
            f"compute_macd_adapter: bars 缺少 close 列，实际列={list(bars.columns)}"
        )
    closes = bars["close"].to_numpy(dtype=float)
    return compute_macd(closes, fast=fast, slow=slow, signal=signal)


# =============================================================================
# 自测入口
# =============================================================================


if __name__ == "__main__":
    print("=" * 60)
    print("Canonical Adapters (canonical_adapters.py)")
    print("=" * 60)

    # 构造测试 DataFrame（30 根模拟日线）
    np.random.seed(42)
    prices = 100.0 + np.cumsum(np.random.randn(30) * 0.5)
    bars = pd.DataFrame(
        {
            "open": prices - 0.1,
            "high": prices + 0.5,
            "low": prices - 0.5,
            "close": prices,
            "volume": np.random.randint(1000, 10000, size=30).astype(float),
        },
        index=pd.date_range("2026-06-01", periods=30, freq="B"),
    )

    # 测试 macd adapter
    result = compute_macd_adapter(bars)
    assert isinstance(result, dict)
    assert "macd_dif" in result
    assert "macd_dea" in result
    assert "macd_hist" in result
    assert len(result["macd_dif"]) == 30
    print(f"macd_adapter OK: dif[-1]={result['macd_dif'][-1]:.4f} "
          f"dea[-1]={result['macd_dea'][-1]:.4f} hist[-1]={result['macd_hist'][-1]:.4f}")

    # 测试空 DataFrame 抛 ValueError
    try:
        compute_macd_adapter(pd.DataFrame())
        raise AssertionError("应抛出 ValueError")
    except ValueError as e:
        print(f"empty guard OK: {e}")

    # 测试缺少 close 列抛 ValueError
    try:
        compute_macd_adapter(bars.drop(columns=["close"]))
        raise AssertionError("应抛出 ValueError")
    except ValueError as e:
        print(f"missing close guard OK: {e}")

    # 确定性验证：相同输入相同输出
    r1 = compute_macd_adapter(bars)
    r2 = compute_macd_adapter(bars)
    assert r1 == r2, "相同输入应得到相同输出"
    print("determinism OK")

    print("OK")
