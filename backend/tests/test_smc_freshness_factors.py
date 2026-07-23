"""永久测试：SMC freshness 因子 14 个细分因子计算。

覆盖：
- BOS bullish/bearish × internal/swing
- CHoCH bullish/bearish × internal/swing
- OB touch bullish/bearish × internal/swing
- EQH / EQL（无 internal/swing 拆分）
- 从未发生 = null
- 多事件取最近（最小 freshness）
- OB 首次触碰 bar 而非创建 bar

不依赖生产 DB / Token / Secret，使用合成 OHLC 数据。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.canonical_adapters import compute_smc_adapter
from app.services.structural_factor_service import _compute_smc_freshness_factors

# ---------------------------------------------------------------------------
# 合成数据生成
# ---------------------------------------------------------------------------

def _make_synthetic_bars(n: int = 300, seed: int = 42) -> pd.DataFrame:
    """生成合成日线 OHLC 数据（含趋势变化，触发 BOS/CHoCH/OB/EQH/EQL）。

    使用确定性随机种子保证可重复。
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start="2024-01-01", periods=n)
    close = 100.0
    closes: list[float] = [close]
    for i in range(1, n):
        # 每 50 根切换趋势方向，制造 BOS/CHoCH
        trend = 0.3 if (i // 50) % 2 == 0 else -0.3
        noise = rng.normal(0, 1.5)
        close = max(5.0, close + trend + noise)
        closes.append(close)
    closes_arr = np.array(closes)
    # 生成 OHLC
    opens = closes_arr - rng.normal(0, 0.5, n)
    highs = np.maximum(opens, closes_arr) + rng.uniform(0.1, 1.0, n)
    lows = np.minimum(opens, closes_arr) - rng.uniform(0.1, 1.0, n)
    volumes = rng.integers(100000, 500000, n).astype(float)
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes_arr, "volume": volumes},
        index=pd.DatetimeIndex(dates, name="trade_date"),
    )
    return df


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def synthetic_bars() -> pd.DataFrame:
    return _make_synthetic_bars(300, seed=42)


@pytest.fixture(scope="module")
def smc_dto(synthetic_bars: pd.DataFrame) -> dict:
    return compute_smc_adapter(synthetic_bars, display_bars=len(synthetic_bars))


EXPECTED_KEYS = {
    "smc_bos_bullish_internal_freshness_bars",
    "smc_bos_bullish_swing_freshness_bars",
    "smc_bos_bearish_internal_freshness_bars",
    "smc_bos_bearish_swing_freshness_bars",
    "smc_choch_bullish_internal_freshness_bars",
    "smc_choch_bullish_swing_freshness_bars",
    "smc_choch_bearish_internal_freshness_bars",
    "smc_choch_bearish_swing_freshness_bars",
    "smc_order_block_touch_bullish_internal_freshness_bars",
    "smc_order_block_touch_bullish_swing_freshness_bars",
    "smc_order_block_touch_bearish_internal_freshness_bars",
    "smc_order_block_touch_bearish_swing_freshness_bars",
    "smc_eqh_freshness_bars",
    "smc_eql_freshness_bars",
}


# ---------------------------------------------------------------------------
# 基础结构测试
# ---------------------------------------------------------------------------

class TestFactorStructure:
    """测试因子结构完整性。"""

    def test_returns_14_keys(self, synthetic_bars: pd.DataFrame):
        result = _compute_smc_freshness_factors(synthetic_bars)
        assert set(result.keys()) == EXPECTED_KEYS
        assert len(result) == 14

    def test_all_values_int_or_none(self, synthetic_bars: pd.DataFrame):
        result = _compute_smc_freshness_factors(synthetic_bars)
        for k, v in result.items():
            assert v is None or isinstance(v, int), f"{k} 类型错误: {type(v)}"
            if v is not None:
                assert v >= 0, f"{k} 不能为负: {v}"

    def test_empty_dataframe_all_none(self):
        result = _compute_smc_freshness_factors(pd.DataFrame())
        assert all(v is None for v in result.values())
        assert set(result.keys()) == EXPECTED_KEYS

    def test_insufficient_bars_all_none(self):
        """< 250 根返回全 None。"""
        df = _make_synthetic_bars(100, seed=1)
        result = _compute_smc_freshness_factors(df)
        assert all(v is None for v in result.values())


# ---------------------------------------------------------------------------
# BOS / CHoCH 因子正确性
# ---------------------------------------------------------------------------

class TestBosChochFactors:
    """测试 BOS/CHoCH 因子按方向和级别拆分。"""

    def test_bos_factors_match_dto(self, synthetic_bars: pd.DataFrame, smc_dto: dict):
        """BOS 因子值与 DTO 中对应子类型最近事件一致。"""
        result = _compute_smc_freshness_factors(synthetic_bars)
        current_index = len(synthetic_bars) - 1
        bos_events = [e for e in smc_dto.get("events", []) if e.get("type") == "BOS"]

        # 按 subtype 分组，取最大 confirmed_index
        expected: dict[str, int] = {}
        for e in bos_events:
            bullish = e.get("bullish")
            internal = e.get("internal")
            cidx = e.get("confirmed_index")
            if bullish is None or internal is None or cidx is None:
                continue
            direction = "bullish" if bullish else "bearish"
            level = "internal" if internal else "swing"
            key = f"smc_bos_{direction}_{level}_freshness_bars"
            idx = int(cidx)
            if key not in expected or idx > expected[key]:
                expected[key] = idx

        for key, best_idx in expected.items():
            assert result[key] == current_index - best_idx, (
                f"{key}: expected {current_index - best_idx}, got {result[key]}"
            )

    def test_choch_factors_match_dto(self, synthetic_bars: pd.DataFrame, smc_dto: dict):
        """CHoCH 因子值与 DTO 中对应子类型最近事件一致。"""
        result = _compute_smc_freshness_factors(synthetic_bars)
        current_index = len(synthetic_bars) - 1
        choch_events = [e for e in smc_dto.get("events", []) if e.get("type") == "CHoCH"]

        expected: dict[str, int] = {}
        for e in choch_events:
            bullish = e.get("bullish")
            internal = e.get("internal")
            cidx = e.get("confirmed_index")
            if bullish is None or internal is None or cidx is None:
                continue
            direction = "bullish" if bullish else "bearish"
            level = "internal" if internal else "swing"
            key = f"smc_choch_{direction}_{level}_freshness_bars"
            idx = int(cidx)
            if key not in expected or idx > expected[key]:
                expected[key] = idx

        for key, best_idx in expected.items():
            assert result[key] == current_index - best_idx, (
                f"{key}: expected {current_index - best_idx}, got {result[key]}"
            )

    def test_bos_bullish_bearish_distinct(self, synthetic_bars: pd.DataFrame, smc_dto: dict):
        """BOS bullish 和 bearish 因子独立计算（不混淆方向）。"""
        result = _compute_smc_freshness_factors(synthetic_bars)
        bos_events = [e for e in smc_dto.get("events", []) if e.get("type") == "BOS"]
        has_bullish = any(e.get("bullish") is True for e in bos_events)
        has_bearish = any(e.get("bullish") is False for e in bos_events)
        if has_bullish:
            assert any(
                result[f"smc_bos_bullish_{lvl}_freshness_bars"] is not None
                for lvl in ("internal", "swing")
            ), "有 bullish BOS 但对应因子全 None"
        if has_bearish:
            assert any(
                result[f"smc_bos_bearish_{lvl}_freshness_bars"] is not None
                for lvl in ("internal", "swing")
            ), "有 bearish BOS 但对应因子全 None"

    def test_internal_swing_distinct(self, synthetic_bars: pd.DataFrame, smc_dto: dict):
        """internal 和 swing 级别独立计算。"""
        result = _compute_smc_freshness_factors(synthetic_bars)
        bos_events = [e for e in smc_dto.get("events", []) if e.get("type") == "BOS"]
        has_internal = any(e.get("internal") is True for e in bos_events)
        has_swing = any(e.get("internal") is False for e in bos_events)
        if has_internal:
            assert any(
                result[f"smc_bos_{dir}_internal_freshness_bars"] is not None
                for dir in ("bullish", "bearish")
            ), "有 internal BOS 但对应因子全 None"
        if has_swing:
            assert any(
                result[f"smc_bos_{dir}_swing_freshness_bars"] is not None
                for dir in ("bullish", "bearish")
            ), "有 swing BOS 但对应因子全 None"


# ---------------------------------------------------------------------------
# OB touch 因子正确性
# ---------------------------------------------------------------------------

class TestObTouchFactors:
    """测试 OB touch 因子：首次触碰 bar（非创建 bar）。"""

    def test_ob_touch_uses_first_touch_not_creation(
        self, synthetic_bars: pd.DataFrame, smc_dto: dict
    ):
        """OB touch freshness 基于首次触碰 bar，不是创建 bar（confirmed_index）。"""
        result = _compute_smc_freshness_factors(synthetic_bars)
        current_index = len(synthetic_bars) - 1
        bars_high = synthetic_bars["high"].to_numpy(dtype=float)
        bars_low = synthetic_bars["low"].to_numpy(dtype=float)

        obs = smc_dto.get("order_blocks", [])
        expected: dict[str, int] = {}
        for ob in obs:
            ob_high = ob.get("bar_high")
            ob_low = ob.get("bar_low")
            confirmed_idx = ob.get("confirmed_index")
            bias = ob.get("bias")
            internal = ob.get("internal")
            if ob_high is None or ob_low is None or confirmed_idx is None:
                continue
            if bias is None or internal is None:
                continue
            ob_high_f = float(ob_high)
            ob_low_f = float(ob_low)
            start_idx = int(confirmed_idx) + 1
            direction = "bullish" if bias == 1 else "bearish"
            level = "internal" if internal else "swing"
            key = f"smc_order_block_touch_{direction}_{level}_freshness_bars"
            first_touch = -1
            for i in range(start_idx, len(synthetic_bars)):
                if bars_high[i] >= ob_low_f and bars_low[i] <= ob_high_f:
                    first_touch = i
                    break
            if first_touch >= 0:
                if key not in expected or first_touch > expected[key]:
                    expected[key] = first_touch

        for key, best_touch in expected.items():
            assert result[key] == current_index - best_touch, (
                f"{key}: expected {current_index - best_touch} (first_touch={best_touch}), "
                f"got {result[key]}"
            )

    def test_ob_no_touch_is_none(self, synthetic_bars: pd.DataFrame, smc_dto: dict):
        """OB 从未被触碰的子类型因子为 None。"""
        result = _compute_smc_freshness_factors(synthetic_bars)
        bars_high = synthetic_bars["high"].to_numpy(dtype=float)
        bars_low = synthetic_bars["low"].to_numpy(dtype=float)
        obs = smc_dto.get("order_blocks", [])

        touched_subtypes: set[str] = set()
        for ob in obs:
            ob_high = ob.get("bar_high")
            ob_low = ob.get("bar_low")
            confirmed_idx = ob.get("confirmed_index")
            bias = ob.get("bias")
            internal = ob.get("internal")
            if ob_high is None or ob_low is None or confirmed_idx is None:
                continue
            if bias is None or internal is None:
                continue
            ob_high_f = float(ob_high)
            ob_low_f = float(ob_low)
            start_idx = int(confirmed_idx) + 1
            direction = "bullish" if bias == 1 else "bearish"
            level = "internal" if internal else "swing"
            key = f"smc_order_block_touch_{direction}_{level}_freshness_bars"
            for i in range(start_idx, len(synthetic_bars)):
                if bars_high[i] >= ob_low_f and bars_low[i] <= ob_high_f:
                    touched_subtypes.add(key)
                    break

        for key in EXPECTED_KEYS:
            if key.startswith("smc_order_block_touch_") and key not in touched_subtypes:
                assert result[key] is None, f"{key} 应为 None（从未触碰）"


# ---------------------------------------------------------------------------
# EQH / EQL 因子正确性
# ---------------------------------------------------------------------------

class TestEqhlFactors:
    """测试 EQH/EQL 因子。"""

    def test_eqh_factor_matches_dto(self, synthetic_bars: pd.DataFrame, smc_dto: dict):
        result = _compute_smc_freshness_factors(synthetic_bars)
        current_index = len(synthetic_bars) - 1
        eqh_events = [e for e in smc_dto.get("equal_highs_lows", []) if e.get("type") == "EQH"]
        if eqh_events:
            best_idx = max(int(e["confirmed_index"]) for e in eqh_events if e.get("confirmed_index") is not None)
            assert result["smc_eqh_freshness_bars"] == current_index - best_idx
        else:
            assert result["smc_eqh_freshness_bars"] is None

    def test_eql_factor_matches_dto(self, synthetic_bars: pd.DataFrame, smc_dto: dict):
        result = _compute_smc_freshness_factors(synthetic_bars)
        current_index = len(synthetic_bars) - 1
        eql_events = [e for e in smc_dto.get("equal_highs_lows", []) if e.get("type") == "EQL"]
        if eql_events:
            best_idx = max(int(e["confirmed_index"]) for e in eql_events if e.get("confirmed_index") is not None)
            assert result["smc_eql_freshness_bars"] == current_index - best_idx
        else:
            assert result["smc_eql_freshness_bars"] is None


# ---------------------------------------------------------------------------
# 多事件取最近
# ---------------------------------------------------------------------------

class TestMultipleEventsMostRecent:
    """测试同子类型多事件取最近（最小 freshness）。"""

    def test_bos_takes_most_recent(self, synthetic_bars: pd.DataFrame, smc_dto: dict):
        """BOS 同子类型多事件取最近 confirmed_index。"""
        result = _compute_smc_freshness_factors(synthetic_bars)
        current_index = len(synthetic_bars) - 1
        bos_events = [e for e in smc_dto.get("events", []) if e.get("type") == "BOS"]

        # 按 subtype 分组
        subtypes: dict[str, list[int]] = {}
        for e in bos_events:
            bullish = e.get("bullish")
            internal = e.get("internal")
            cidx = e.get("confirmed_index")
            if bullish is None or internal is None or cidx is None:
                continue
            direction = "bullish" if bullish else "bearish"
            level = "internal" if internal else "swing"
            key = f"smc_bos_{direction}_{level}_freshness_bars"
            subtypes.setdefault(key, []).append(int(cidx))

        for key, indices in subtypes.items():
            if len(indices) > 1:
                expected_freshness = current_index - max(indices)
                assert result[key] == expected_freshness, (
                    f"{key}: 多事件应取最近 (max idx={max(indices)}), "
                    f"expected freshness={expected_freshness}, got {result[key]}"
                )


# ---------------------------------------------------------------------------
# 从未发生 = null
# ---------------------------------------------------------------------------

class TestNeverOccurred:
    """测试从未发生的事件因子为 null。"""

    def test_subtype_no_event_is_none(self, synthetic_bars: pd.DataFrame, smc_dto: dict):
        """DTO 中不存在的子类型，对应因子为 None。"""
        result = _compute_smc_freshness_factors(synthetic_bars)

        # 收集 DTO 中实际存在的 BOS/CHoCH 子类型
        existing_subtypes: set[str] = set()
        for e in smc_dto.get("events", []):
            etype = e.get("type")
            bullish = e.get("bullish")
            internal = e.get("internal")
            if etype and bullish is not None and internal is not None:
                direction = "bullish" if bullish else "bearish"
                level = "internal" if internal else "swing"
                existing_subtypes.add(f"smc_{etype.lower()}_{direction}_{level}_freshness_bars")

        for key in EXPECTED_KEYS:
            if key.startswith("smc_bos_") or key.startswith("smc_choch_"):
                if key not in existing_subtypes:
                    assert result[key] is None, f"{key} 应为 None（DTO 中无此类事件）"
