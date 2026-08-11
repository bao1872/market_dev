"""HISTORY-BACKFILL-PIT-01 Round 2 单元测试。

覆盖：
  A. 公式方向：anchor=1, factor=0.985714 → K=1.014493
  B. 无公司行为：factor_t == anchor → exact no-op
  C. daily state sqzmom rescale
  D. event BOS/CHoCH level rescale
  E. OB bar_high/bar_low rescale
  F. ZERO_CROSS from_val/to_val rescale
  G. event identity unchanged
  H. None fields unchanged
  I. factor lookup ffill
  J. 早于第一个 factor 的行为
"""
from __future__ import annotations

import pandas as pd

from app.services.first_pyramid_service import normalize_history_result_to_pit

# =============================================================================
# Test fixtures
# =============================================================================

def _make_history(sqzmom_val: float = 10.0, sqzmom_delta: float = -0.5,
                  extra_state: dict | None = None,
                  events: list[dict] | None = None) -> dict:
    """构造最小 history dict（只需 scale-covariant 字段）。"""
    state = {
        "time": "2026-05-15T00:00:00",
        "sqzmom_val": sqzmom_val,
        "sqzmom_delta": sqzmom_delta,
        "trend_transition": "UP_CONFIRMED",  # scale-invariant, must NOT change
        "regime_value": 1,
        "dsa_dir_bars": 112,
        "dsa_vwap_dev_pct": 0.05,
        "price_position_120d": 0.68,
        "swing_bias": 1,
        "internal_bias": 1,
        "volatility_phase": "squeeze_off",
        "volume_ratio_20": 1.5,
    }
    if extra_state:
        state.update(extra_state)
    return {
        "daily_state": [state],
        "events": events or [],
        "meta": {"input_hash": "abc123"},
    }


def _make_factor_series(data: dict[str, float]) -> pd.Series:
    """构造 factor series，index 为日期字符串。

    注意：pd.Series 用 DatetimeIndex 时，值必须对应 index 位置。
    不能用 dict + index=DatetimeIndex（值会 NaN）。
    """
    dates = pd.DatetimeIndex(list(data.keys()))
    return pd.Series([data[k] for k in data.keys()], index=dates)


# =============================================================================
# A. 公式方向
# =============================================================================

def test_formula_direction_anchor_1_factor_0985714():
    """anchor=1.0, factor_t=0.985714 → K_t ≈ 1.014493"""
    history = _make_history(sqzmom_val=10.0, sqzmom_delta=-0.5)
    factor = _make_factor_series({"2026-05-15": 0.985714})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    state = result["daily_state"][0]

    k_expected = 1.0 / 0.985714
    assert abs(state["sqzmom_val"] - 10.0 * k_expected) < 1e-10
    assert abs(state["sqzmom_delta"] - (-0.5) * k_expected) < 1e-10


def test_formula_direction_anchor_0985714_factor_0985714():
    """anchor=factor_t → K_t=1 → no-op"""
    history = _make_history(sqzmom_val=10.0, sqzmom_delta=-0.5)
    factor = _make_factor_series({"2026-05-15": 0.985714})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=0.985714,
    )
    state = result["daily_state"][0]

    assert state["sqzmom_val"] == 10.0
    assert state["sqzmom_delta"] == -0.5


# =============================================================================
# B. 无公司行为 → exact no-op
# =============================================================================

def test_no_corporate_action_noop():
    """factor_t == anchor → 不修改任何值"""
    history = _make_history(sqzmom_val=42.0, sqzmom_delta=3.0)
    factor = _make_factor_series({"2026-05-15": 1.0})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    state = result["daily_state"][0]

    assert state["sqzmom_val"] == 42.0
    assert state["sqzmom_delta"] == 3.0
    # scale-invariant fields unchanged
    assert state["trend_transition"] == "UP_CONFIRMED"
    assert state["regime_value"] == 1
    assert state["dsa_vwap_dev_pct"] == 0.05
    assert state["price_position_120d"] == 0.68


# =============================================================================
# C. daily state sqzmom rescale
# =============================================================================

def test_daily_sqzmom_rescale():
    """K=2 → sqzmom 翻倍，scale-invariant 不变"""
    history = _make_history(sqzmom_val=5.0, sqzmom_delta=1.0)
    factor = _make_factor_series({"2026-05-15": 0.5})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    state = result["daily_state"][0]

    assert state["sqzmom_val"] == 10.0
    assert state["sqzmom_delta"] == 2.0
    # scale-invariant MUST NOT change
    assert state["dsa_vwap_dev_pct"] == 0.05
    assert state["price_position_120d"] == 0.68
    assert state["swing_bias"] == 1
    assert state["volatility_phase"] == "squeeze_off"
    assert state["volume_ratio_20"] == 1.5


# =============================================================================
# D. event BOS/CHoCH level rescale
# =============================================================================

def test_event_bos_level_rescale():
    """BOS level 被正确缩放"""
    events = [{
        "type": "BOS",
        "direction": "up",
        "bar_index": 120,
        "time": "2026-05-15T00:00:00",
        "level": 15.0,
        "freshness": 5,
        "internal": False,
    }]
    history = _make_history(sqzmom_val=10.0, sqzmom_delta=0.0, events=events)
    factor = _make_factor_series({"2026-05-15": 0.5})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    evt = result["events"][0]

    assert evt["level"] == 30.0  # 15 * (1/0.5)
    # identity unchanged
    assert evt["type"] == "BOS"
    assert evt["direction"] == "up"
    assert evt["bar_index"] == 120
    assert evt["freshness"] == 5
    assert evt["internal"] is False


def test_event_choch_level_rescale():
    """CHoCH level 被正确缩放"""
    events = [{
        "type": "CHoCH",
        "direction": "down",
        "bar_index": 100,
        "time": "2026-03-15T00:00:00",
        "level": 8.0,
        "freshness": 3,
    }]
    history = _make_history(sqzmom_val=10.0, sqzmom_delta=0.0, events=events)
    factor = _make_factor_series({"2026-03-15": 0.8})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    evt = result["events"][0]

    assert evt["level"] == 10.0  # 8 * (1/0.8)


# =============================================================================
# E. OB bar_high/bar_low rescale
# =============================================================================

def test_ob_bar_high_low_rescale():
    """OB bar_high/bar_low 被正确缩放"""
    events = [{
        "type": "OB_CREATED",
        "direction": "up",
        "bar_index": 50,
        "time": "2026-05-15T00:00:00",
        "bar_high": 20.0,
        "bar_low": 18.0,
        "internal": True,
    }]
    history = _make_history(sqzmom_val=10.0, sqzmom_delta=0.0, events=events)
    factor = _make_factor_series({"2026-05-15": 0.5})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    evt = result["events"][0]

    assert evt["bar_high"] == 40.0
    assert evt["bar_low"] == 36.0
    assert evt["type"] == "OB_CREATED"  # identity


# =============================================================================
# F. ZERO_CROSS from_val/to_val rescale
# =============================================================================

def test_zero_cross_from_to_val_rescale():
    """ZERO_CROSS from_val/to_val 被正确缩放"""
    events = [{
        "type": "ZERO_CROSS_UP",
        "bar_index": 80,
        "time": "2026-05-15T00:00:00",
        "from_val": -0.5,
        "to_val": 0.3,
        "squeeze_length": 12,
    }]
    history = _make_history(sqzmom_val=10.0, sqzmom_delta=0.0, events=events)
    factor = _make_factor_series({"2026-05-15": 0.5})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    evt = result["events"][0]

    assert evt["from_val"] == -1.0
    assert evt["to_val"] == 0.6
    assert evt["squeeze_length"] == 12  # identity


# =============================================================================
# G. event identity unchanged
# =============================================================================

def test_event_identity_unchanged():
    """normalization 不改变 event identity 字段"""
    events = [{
        "type": "BOS",
        "direction": "up",
        "bar_index": 120,
        "anchor_index": 100,
        "time": "2026-05-15T00:00:00",
        "level": 15.0,
        "freshness": 5,
        "internal": False,
        "structure_level": 1,
    }]
    history = _make_history(sqzmom_val=10.0, sqzmom_delta=0.0, events=events)
    factor = _make_factor_series({"2026-05-15": 0.5})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    evt = result["events"][0]

    # identity MUST NOT change
    assert evt["type"] == "BOS"
    assert evt["direction"] == "up"
    assert evt["bar_index"] == 120
    assert evt["anchor_index"] == 100
    assert evt["freshness"] == 5
    assert evt["internal"] is False
    assert evt["structure_level"] == 1
    # level SHOULD change
    assert evt["level"] == 30.0


# =============================================================================
# H. None fields unchanged
# =============================================================================

def test_none_fields_unchanged():
    """None 值保持 None"""
    history = _make_history(sqzmom_val=None, sqzmom_delta=None)
    factor = _make_factor_series({"2026-05-15": 0.5})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    state = result["daily_state"][0]

    assert state["sqzmom_val"] is None
    assert state["sqzmom_delta"] is None


def test_none_event_price_unchanged():
    """事件中的 None price 保持 None"""
    events = [{
        "type": "BOS",
        "bar_index": 120,
        "time": "2026-05-15T00:00:00",
        "level": None,
        "direction": "up",
    }]
    history = _make_history(sqzmom_val=10.0, sqzmom_delta=0.0, events=events)
    factor = _make_factor_series({"2026-05-15": 0.5})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    evt = result["events"][0]

    assert evt["level"] is None
    assert evt["direction"] == "up"


# =============================================================================
# I. factor lookup ffill
# =============================================================================

def test_factor_lookup_ffill():
    """factor lookup 使用 ffill：state_date 无精确匹配时回退到最近的前一日"""
    # factor 只在 2026-05-10 和 2026-05-20 有值
    factor = _make_factor_series({
        "2026-05-10": 0.9,
        "2026-05-20": 1.0,
    })
    history = _make_history(sqzmom_val=10.0, sqzmom_delta=0.0)
    # state_date=2026-05-15 没有精确匹配 → 应该用 0.9（ffill）
    history["daily_state"][0]["time"] = "2026-05-15T00:00:00"

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    state = result["daily_state"][0]

    k_factor = 1.0 / 0.9
    assert abs(state["sqzmom_val"] - 10.0 * k_factor) < 1e-10


def test_factor_lookup_before_first_factor():
    """早于第一个 factor 的日期 → 使用第一个 factor（与 adj_factor.py authority 一致）"""
    factor = _make_factor_series({"2026-05-20": 0.9})
    history = _make_history(sqzmom_val=10.0, sqzmom_delta=0.0)
    history["daily_state"][0]["time"] = "2026-05-01T00:00:00"  # before first factor

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    state = result["daily_state"][0]

    # 早于第一个 factor → 用第一个 factor=0.9, K_t = 1.0/0.9
    k_factor = 1.0 / 0.9
    assert abs(state["sqzmom_val"] - 10.0 * k_factor) < 1e-10


# =============================================================================
# J. 边界：多个 daily_state 各自不同 K
# =============================================================================

def test_multiple_daily_states_different_k():
    """多个 daily_state 各自使用自己的 K_t"""
    history = {
        "daily_state": [
            {"time": "2026-05-10T00:00:00", "sqzmom_val": 10.0, "sqzmom_delta": 1.0},
            {"time": "2026-05-20T00:00:00", "sqzmom_val": 20.0, "sqzmom_delta": 2.0},
        ],
        "events": [],
        "meta": {},
    }
    factor = _make_factor_series({"2026-05-10": 0.9, "2026-05-20": 1.0})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )

    # state 0: K = 1/0.9
    assert abs(result["daily_state"][0]["sqzmom_val"] - 10.0 * (1.0 / 0.9)) < 1e-10
    # state 1: K = 1/1.0 = 1
    assert result["daily_state"][1]["sqzmom_val"] == 20.0


# =============================================================================
# 边界：空输入
# =============================================================================

def test_empty_history_noop():
    """空 history 不抛异常"""
    history = {"daily_state": [], "events": [], "meta": {}}
    factor = _make_factor_series({"2026-05-15": 0.5})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    assert result == history


def test_empty_factor_series_noop():
    """空 factor series 不修改"""
    history = _make_history(sqzmom_val=10.0, sqzmom_delta=0.0)
    factor = pd.Series(dtype=float)

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=1.0,
    )
    state = result["daily_state"][0]
    assert state["sqzmom_val"] == 10.0


def test_zero_anchor_factor_noop():
    """anchor_factor=0 不修改"""
    history = _make_history(sqzmom_val=10.0, sqzmom_delta=0.0)
    factor = _make_factor_series({"2026-05-15": 0.5})

    result = normalize_history_result_to_pit(
        history, factor_series=factor, anchor_factor=0.0,
    )
    state = result["daily_state"][0]
    assert state["sqzmom_val"] == 10.0
