"""compute_dsa_bundle 一致性测试。

验证 compute_dsa_bundle 返回的 per_bar 最后一行与 last_row_metrics 完全一致，
确保 execute()（用 last_row_metrics）与 compute_indicators()（用 per_bar）共享
同一份计算结果，消除双路径不一致。

测试内容：
1. per_bar 最后一行的标量字段（dsa_vwap、dsa_dir_bars、offset_*、vwap_ret_*、regime_value 等）
   与 last_row_metrics 中对应字段完全一致
2. 数据不足时返回空结构
3. lookback 截断生效（输入 > lookback 时 per_bar 长度 = lookback）
4. execute() 与 compute_indicators() 共享同一份计算（端到端一致性）

测试数据：合成的日线行情（不依赖真实数据库/网络），至少 60 根。
"""
from __future__ import annotations

import math
from types import SimpleNamespace
import uuid

import numpy as np
import pandas as pd
import pytest

from app.constants.indicator_contract import DSA_LOOKBACK
from app.strategy_assets.algorithms.features.atr_rope_event_factor_lab_v4 import (
    ATRRopeConfig,
)
from app.strategy_assets.algorithms.features.dynamic_swing_anchored_vwap import (
    DSAConfig,
)
from app.strategy.selectors.dsa_selector import (
    MIN_DIR_BARS,
    _safe_float,
    compute_dsa_bundle,
)


def _build_synthetic_bars(n: int = 300, seed: int = 42, start_price: float = 10.0) -> pd.DataFrame:
    """构造前复权日线 DataFrame，含趋势 + 波动。

    生成逻辑：
    - 持续上涨趋势（每日 +0.3%~0.8%），使 dir 长期为 1 触发 regime=1
    - 每 7 天小幅回调，模拟真实波动
    - open/high/low 围绕 close 波动

    Args:
        n: 生成的 bar 数（>= 60，compute_dsa_bundle 的最小有效长度）
        seed: 随机种子（确保可复现）
        start_price: 起始价格

    Returns:
        DataFrame: index=DatetimeIndex(工作日), columns=open/high/low/close/volume/amount
    """
    assert n >= 60, f"测试数据至少 60 根（compute_dsa_bundle 最小长度），实际 n={n}"
    np.random.seed(seed)
    idx = pd.date_range("2025-01-01", periods=n, freq="B")

    daily_returns = np.random.uniform(0.003, 0.008, size=n)
    daily_returns[::7] = -0.002  # 每 7 天小幅回调
    close = start_price * np.cumprod(1 + daily_returns)
    close = np.maximum(close, 1.0)

    open_ = close * (1 + np.random.uniform(-0.005, 0.005, size=n))
    high = np.maximum(open_, close) * (1 + np.random.uniform(0.001, 0.01, size=n))
    low = np.minimum(open_, close) * (1 - np.random.uniform(0.001, 0.01, size=n))
    volume = np.random.uniform(500000, 2000000, size=n)
    amount = volume * close

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        },
        index=idx,
    )


def _build_config(lookback: int | None = DSA_LOOKBACK) -> dict:
    """构建与 DSASelector._build_history_config 一致的测试配置。"""
    return {
        "dsa_config": DSAConfig(),
        "rope_config": ATRRopeConfig(regime_lookback=55),
        "min_dir_bars": MIN_DIR_BARS,
        "lookback": lookback,
    }


def _build_mock_version():
    """构建与 dsa_selector.yaml 默认参数一致的 mock StrategyVersion。"""
    return SimpleNamespace(
        id=uuid.uuid4(),
        manifest={
            "strategy_id": "dsa_selector",
            "kind": "selector",
            "version": "1.3.0",
            "parameters": [
                {"key": "algorithm.lookback", "default": DSA_LOOKBACK},
                {"key": "dsa.prd", "default": 50},
                {"key": "dsa.base_apt", "default": 20.0},
                {"key": "dsa.use_adapt", "default": False},
                {"key": "dsa.vol_bias", "default": 10.0},
                {"key": "dsa.atr_len", "default": 50},
                {"key": "atr_rope.length", "default": 14},
                {"key": "atr_rope.multi", "default": 1.5},
                {"key": "atr_rope.regime_lookback", "default": 55},
                {"key": "atr_rope.regime_threshold", "default": 0.55},
            ],
            "resource_budget": {"target_ms_per_instrument": 5000},
        },
    )


# ---------------------------------------------------------------------------
# 模块级共享 fixture（避免 class-scoped fixture 实例方法弃用警告）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def shared_bundle() -> dict:
    """共享一份 compute_dsa_bundle 结果（300 根输入，lookback=250 截断）。"""
    bars = _build_synthetic_bars(n=300)
    return compute_dsa_bundle(bars, _build_config())


@pytest.fixture(scope="module")
def shared_selector_and_bars():
    """初始化 DSASelector 并构造测试行情（模块级共享）。"""
    import asyncio

    from app.strategy.selectors.dsa_selector import DSASelector

    selector = DSASelector()
    asyncio.run(selector.initialize(_build_mock_version()))  # type: ignore[arg-type]
    bars = _build_synthetic_bars(n=300)
    return selector, bars


# ---------------------------------------------------------------------------
# 1. per_bar 最后一行与 last_row_metrics 一致性
# ---------------------------------------------------------------------------


class TestDsaBundleLastRowConsistency:
    """验证 per_bar 最后一行的标量字段与 last_row_metrics 完全一致。"""

    def test_bundle_not_empty(self, shared_bundle: dict):
        """前置：bundle 应非空（300 根输入足够计算）。"""
        bundle = shared_bundle
        assert not bundle["per_bar"].empty, "per_bar 不应为空"
        assert bundle["last_row_metrics"], "last_row_metrics 不应为空"

    def test_dsa_vwap_matches(self, shared_bundle: dict):
        """per_bar 最后一行 dsa_vwap 与 last_row_metrics["dsa_vwap"] 一致。"""
        bundle = shared_bundle
        last_row = bundle["per_bar"].iloc[-1]
        metrics = bundle["last_row_metrics"]
        assert metrics["dsa_vwap"] == _safe_float(last_row["dsa_vwap"])

    def test_dsa_dir_bars_matches(self, shared_bundle: dict):
        """per_bar 最后一行 dsa_dir_bars（持续天数）与 metrics 一致。"""
        bundle = shared_bundle
        last_row = bundle["per_bar"].iloc[-1]
        metrics = bundle["last_row_metrics"]
        expected = int(last_row["dsa_dir_bars"]) if pd.notna(last_row["dsa_dir_bars"]) else 0
        assert metrics["dsa_dir_bars"] == expected

    def test_regime_value_matches(self, shared_bundle: dict):
        """per_bar 最后一行 regime_value（方向/regime）与 metrics 一致。"""
        bundle = shared_bundle
        last_row = bundle["per_bar"].iloc[-1]
        metrics = bundle["last_row_metrics"]
        expected = int(last_row["regime_value"]) if pd.notna(last_row["regime_value"]) else 0
        assert metrics["regime_value"] == expected

    def test_offset_fields_match(self, shared_bundle: dict):
        """per_bar 最后一行 offset_*（偏离率）字段与 metrics 一致。"""
        bundle = shared_bundle
        last_row = bundle["per_bar"].iloc[-1]
        metrics = bundle["last_row_metrics"]
        for col in [
            "offset_mean",
            "offset_std",
            "offset_rate",
            "offset_variance_rate",
            "offset_percentile",
        ]:
            assert metrics[col] == _safe_float(last_row[col]), f"字段 {col} 不一致"

    def test_vwap_ret_fields_match(self, shared_bundle: dict):
        """per_bar 最后一行 vwap_ret_*（收益）字段与 metrics 一致。"""
        bundle = shared_bundle
        last_row = bundle["per_bar"].iloc[-1]
        metrics = bundle["last_row_metrics"]
        for col in ["vwap_ret_avg", "vwap_ret_total", "vwap_ret_5", "vwap_ret_10", "vwap_ret_20"]:
            assert metrics[col] == _safe_float(last_row[col]), f"字段 {col} 不一致"

    def test_cross_count_fields_match(self, shared_bundle: dict):
        """per_bar 最后一行交叉计数与 metrics 一致。"""
        bundle = shared_bundle
        last_row = bundle["per_bar"].iloc[-1]
        metrics = bundle["last_row_metrics"]
        for col in [
            "cross_up_count",
            "cross_down_count",
            "rope_cross_up_count",
            "rope_cross_down_count",
        ]:
            expected = int(last_row[col]) if pd.notna(last_row[col]) else 0
            assert metrics[col] == expected, f"字段 {col} 不一致"

    def test_all_metrics_keys_have_consistent_source(self, shared_bundle: dict):
        """遍历 last_row_metrics 全部 key，验证与 per_bar 最后一行对应字段一致。

        覆盖 _history_row_to_metrics 产出的所有字段，确保无遗漏。
        对日期字段（*_date）单独处理：metrics 为 ISO 字符串，per_bar 为 Timestamp。
        """
        bundle = shared_bundle
        last_row = bundle["per_bar"].iloc[-1]
        metrics = bundle["last_row_metrics"]

        date_keys = {
            "last_cross_up_date",
            "last_cross_down_date",
            "rope_cross_up_date",
            "rope_cross_down_date",
        }
        bool_keys = {"touch_rope", "touch_vwap"}

        for key, val in metrics.items():
            if key in date_keys:
                # 日期字段：metrics 是 ISO 字符串或 None
                raw = last_row[key]
                if val is None:
                    assert pd.isna(raw), f"日期字段 {key} metrics=None 但 per_bar 非 NaT"
                else:
                    assert pd.notna(raw), f"日期字段 {key} metrics 非空 但 per_bar 为 NaT"
                    assert val == raw.date().isoformat() or val == str(raw.date())
                continue
            if key in bool_keys:
                raw = last_row[key]
                expected = bool(raw) if pd.notna(raw) else False
                assert val == expected, f"布尔字段 {key} 不一致: {val} vs {expected}"
                continue
            if key not in last_row.index:
                # last_close 等额外字段不在 per_bar 中，跳过
                continue
            # 数值字段：用 _safe_float 比较
            assert val == _safe_float(last_row[key]), (
                f"字段 {key} 不一致: metrics={val!r} vs per_bar={_safe_float(last_row[key])!r}"
            )


# ---------------------------------------------------------------------------
# 2. 数据不足与边界
# ---------------------------------------------------------------------------


class TestDsaBundleEdgeCases:
    """验证数据不足与边界情况。"""

    def test_insufficient_data_returns_empty(self):
        """数据 < 60 根时返回空结构。"""
        bars = _build_synthetic_bars(n=250).head(50)
        bundle = compute_dsa_bundle(bars, _build_config())
        assert bundle["per_bar"].empty
        assert bundle["last_row_metrics"] == {}

    def test_empty_input_returns_empty(self):
        """空 DataFrame 返回空结构。"""
        bundle = compute_dsa_bundle(pd.DataFrame(), _build_config())
        assert bundle["per_bar"].empty
        assert bundle["last_row_metrics"] == {}

    def test_none_input_returns_empty(self):
        """None 输入返回空结构。"""
        bundle = compute_dsa_bundle(None, _build_config())  # type: ignore[arg-type]
        assert bundle["per_bar"].empty
        assert bundle["last_row_metrics"] == {}


# ---------------------------------------------------------------------------
# 3. lookback 截断
# ---------------------------------------------------------------------------


class TestDsaBundleLookback:
    """验证 lookback 截断生效。"""

    def test_per_bar_length_truncated_to_lookback(self):
        """输入 > lookback 时 per_bar 长度 = lookback。"""
        bars = _build_synthetic_bars(n=300)
        bundle = compute_dsa_bundle(bars, _build_config(lookback=DSA_LOOKBACK))
        assert len(bundle["per_bar"]) == DSA_LOOKBACK

    def test_per_bar_length_unchanged_when_input_le_lookback(self):
        """输入 < lookback 时 per_bar 长度 = 输入长度（不截断）。"""
        bars = _build_synthetic_bars(n=200)
        bundle = compute_dsa_bundle(bars, _build_config(lookback=DSA_LOOKBACK))
        assert len(bundle["per_bar"]) == 200

    def test_no_lookback_uses_all_bars(self):
        """lookback=None 时使用全部输入。"""
        bars = _build_synthetic_bars(n=300)
        bundle = compute_dsa_bundle(bars, _build_config(lookback=None))
        assert len(bundle["per_bar"]) == 300


# ---------------------------------------------------------------------------
# 4. execute() 与 compute_indicators() 端到端一致性
# ---------------------------------------------------------------------------


class TestExecuteComputeIndicatorsConsistency:
    """验证 execute() 与 compute_indicators() 通过 compute_dsa_bundle 共享同一份计算。

    核心断言：execute() 输出的 metrics 中的 dsa_vwap / dsa_dir_bars / offset_mean /
    vwap_ret_avg 等字段，与 compute_indicators() 输出的 per_bar 数组最后一行对应。
    """

    def test_execute_metrics_match_compute_indicators_last_bar(self, shared_selector_and_bars):
        """execute() 的 metrics 与 compute_indicators() 最后一根 bar 的对应字段一致。"""
        import asyncio

        from app.strategy.runtime import MarketDataContext

        selector, bars = shared_selector_and_bars
        trade_date = bars.index[-1].date()

        ctx = MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="600519",
            bars_daily=bars,
            trade_date=trade_date,
        )

        # execute() 走 BudgetGuard -> _compute_metrics_sync -> compute_dsa_bundle
        result = asyncio.run(selector.execute(ctx))
        metrics = result.metrics

        # compute_indicators() 走 compute_dsa_bundle -> per_bar
        indicators = asyncio.run(selector.compute_indicators(ctx))

        assert metrics.get("error") is None, f"execute 返回错误: {metrics.get('error')}"
        assert len(indicators["dsa_vwap"]) > 0, "compute_indicators 返回空"

        # 最后一根 bar 的 dsa_vwap
        last_vwap = indicators["dsa_vwap"][-1]
        assert last_vwap is not None, "最后一根 bar dsa_vwap 不应为 None"
        assert math.isclose(metrics["dsa_vwap"], last_vwap, rel_tol=1e-9, abs_tol=1e-9), (
            f"dsa_vwap 不一致: execute={metrics['dsa_vwap']} vs indicators={last_vwap}"
        )

        # regime_value 应与 dsa_dir 最后一根 bar 的方向一致（regime_value 来自 dsa_dir_bars）
        # dsa_dir_bars 为正表示多头方向
        assert metrics["dsa_dir_bars"] is not None
        last_dir = indicators["dsa_dir"][-1]
        if metrics["dsa_dir_bars"] > 0:
            assert last_dir == 1, f"dsa_dir_bars>0 但最后一根 dsa_dir={last_dir}"
        elif metrics["dsa_dir_bars"] < 0:
            assert last_dir == -1, f"dsa_dir_bars<0 但最后一根 dsa_dir={last_dir}"

    def test_compute_indicators_uses_lookback(self, shared_selector_and_bars):
        """compute_indicators() 应用 lookback=250，输出长度 = 250（输入 300 根）。"""
        import asyncio

        from app.strategy.runtime import MarketDataContext

        selector, bars = shared_selector_and_bars
        ctx = MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="600519",
            bars_daily=bars,
            trade_date=bars.index[-1].date(),
        )
        indicators = asyncio.run(selector.compute_indicators(ctx))
        # 输入 300 根，lookback=250，输出应为 250
        assert len(indicators["dsa_vwap"]) == DSA_LOOKBACK, (
            f"compute_indicators 输出长度应为 {DSA_LOOKBACK}，"
            f"实际 {len(indicators['dsa_vwap'])}"
        )
        # 所有数组长度一致
        for key in ["dsa_vwap", "dsa_dir", "regime_id", "anchor_time", "pivot_type", "pivot_price"]:
            assert len(indicators[key]) == DSA_LOOKBACK, f"字段 {key} 长度不一致"


if __name__ == "__main__":
    # 模块自测入口：直接运行验证基础逻辑（不依赖 pytest/DB）
    bars = _build_synthetic_bars(n=300)
    bundle = compute_dsa_bundle(bars, _build_config())
    assert not bundle["per_bar"].empty, "per_bar 不应为空"
    assert bundle["last_row_metrics"], "last_row_metrics 不应为空"

    last_row = bundle["per_bar"].iloc[-1]
    metrics = bundle["last_row_metrics"]
    assert metrics["dsa_vwap"] == _safe_float(last_row["dsa_vwap"])
    assert metrics["dsa_dir_bars"] == int(last_row["dsa_dir_bars"])
    assert metrics["offset_mean"] == _safe_float(last_row["offset_mean"])
    assert metrics["vwap_ret_avg"] == _safe_float(last_row["vwap_ret_avg"])
    assert len(bundle["per_bar"]) == DSA_LOOKBACK
    print("compute_dsa_bundle 一致性自测通过 ✓")
    print(f"  per_bar 行数: {len(bundle['per_bar'])}")
    print(f"  last_row dsa_vwap: {metrics['dsa_vwap']}")
    print(f"  last_row dsa_dir_bars: {metrics['dsa_dir_bars']}")
    print(f"  last_row regime_value: {metrics['regime_value']}")
    print("OK")
