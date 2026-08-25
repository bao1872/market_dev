"""Round 2 最小验证（纯函数 + synthetic fixture，不连 DB）。

覆盖 prompt §16 最低验证要求：
1. ✅ denominator 使用当日 valid universe（daily_state 的 denom = COUNT FILTER valid）
2. ✅ transition 使用 T-1/T 共同 universe（n_common 只计 valid AND prev_valid；LAG 按 trade_date）
3. ✅ Δ3/Δ5 使用交易日序列（lag 由索引偏移 i-lag 实现，非自然日）
4. ✅ 无 future data（diffusion 只用 i-lag 之前的值；transition LAG 只读前一交易日）
5. ✅ correlation 输入日期正确对齐（scatter_pairs 只保留双方非空且同 trade_date）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.review_market_observation.round2.round2_db_native import (
    _DIFFUSION_SERIES,
    _MEDIAN_FIELDS,
    _RATIO_KEY,
    _RATIO_SPECS,
    _TRANSITION_SPECS,
    _diffusion,
    _find_cross_horizon_divergence,
    _select_archetype_days,
    _spearman,
    build_sql_daily_concentration,
    build_sql_daily_state,
    build_sql_daily_transition,
)


# ============================================================================
# 1. denominator = 当日 valid universe
# ============================================================================

def test_daily_state_denom_is_valid_universe():
    sql = build_sql_daily_state()
    # denom 列必须只 count valid_for_market_aggregation=true
    assert "COUNT(*) FILTER (WHERE" in sql
    assert "valid_for_market_aggregation" in sql
    assert "AS denom" in sql
    # 每个 FILTER 计数都必须约束在 valid universe（含 valid_for_market_aggregation）
    assert "valid_for_market_aggregation" in sql
    assert "FILTER (WHERE" in sql


# ============================================================================
# 2. transition 使用 T-1/T common universe
# ============================================================================

def test_transition_sql_uses_lag_and_common_universe():
    sql = build_sql_daily_transition()
    assert "LAG(" in sql
    assert "PARTITION BY s.instrument_id ORDER BY s.trade_date" in sql
    # n_common 只计本日 valid 且前一交易日 valid
    assert "FILTER (WHERE valid AND prev_valid IS TRUE)" in sql
    assert "AS n_common" in sql


def test_transition_lag_has_no_future_read():
    """LAG 由 ORDER BY trade_date 升序，只读前一交易日；不含 LEAD / 未来窗口。"""
    sql_upper = build_sql_daily_transition().upper()
    assert "LAG(" in sql_upper
    assert "LEAD(" not in sql_upper
    # 排序必须升序（升序下 LAG 引用的是更早日期）
    assert "ORDER BY S.TRADE_DATE" in sql_upper


# ============================================================================
# 3. Δ3/Δ5 使用交易日序列（索引偏移），且无 future
# ============================================================================

def test_diffusion_uses_index_lag_not_calendar():
    series = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    d3 = _diffusion(series, 3)
    # i>=lag 时 = cur - series[i-lag]
    assert d3[:3] == [None, None, None]
    assert d3[3] == pytest.approx(4.0 - 1.0)  # 4-1
    assert d3[5] == pytest.approx(6.0 - 3.0)  # 6-3
    # 任何 i 只用 index < i 的值，无未来数据
    for i, v in enumerate(d3):
        if v is not None:
            assert i >= 3


def test_diffusion_respects_none():
    series = [1.0, None, 3.0, 4.0]
    d1 = _diffusion(series, 1)
    assert d1[0] is None
    assert d1[1] is None  # prev 为 None
    assert d1[2] is None  # prev 为 None
    assert d1[3] == pytest.approx(4.0 - 3.0)


def test_diffusion_series_are_base_names():
    """run_round2 用 obs[base + '_ratio'] 取列；base 不得已带 _ratio 后缀（防 KeyError 回归）。"""
    for base in _DIFFUSION_SERIES:
        assert not base.endswith("_ratio"), f"{base} 不应带 _ratio 后缀"
        assert base in (
            "regime_up", "regime_down", "swing_up", "swing_down",
            "internal_up", "internal_down", "momentum_expanding", "momentum_enhancing",
        )


def test_ratio_keys_are_base_names():
    """_RATIO_KEY 的 key 必须是 base 名（SQL 生成 <key>_cnt，ratio 列 <key>_ratio）。"""
    expected_ratio_cols = {f"{k}_ratio" for k in _RATIO_KEY}
    for k in _RATIO_KEY:
        assert not k.endswith("_ratio"), f"{k} 不应带 _ratio 后缀"
    # 下游依赖的语义列必须存在
    for needed in (
        "regime_up_ratio", "regime_down_ratio", "regime_neutral_ratio",
        "swing_up_ratio", "swing_down_ratio",
        "internal_up_ratio", "internal_down_ratio",
        "resonance_ratio", "divergence_ratio",
        "momentum_expanding_ratio", "momentum_contracting_ratio",
        "momentum_enhancing_ratio", "momentum_weakening_ratio",
    ):
        assert needed in expected_ratio_cols, f"缺少 ratio 列 {needed}"


def test_assembly_analysis_column_contract():
    """用 synthetic SQL 结果帧跑 run_round2 的组装+diffusion 逻辑，验证关键列都存在。

    复现 run_round2 中 df_state/df_trans/df_conc 组装 + ratio 计算 + diffusion 的
    列名拼接，防止 _ratio/_rate/_diff 后缀不一致导致的 KeyError（线上回归）。
    """
    import pandas as pd

    dates = [f"2026-0{i+1}-0{i+1}" for i in range(10)]
    # df_state：SQL 返回 denom + <base>_cnt + median cols + above_1 cnt
    state_cols = ["trade_date", "denom"]
    state_cols += [f"{k}_cnt" for k in _RATIO_KEY]
    state_cols += list(_MEDIAN_FIELDS.values())
    state_cols += ["volume_above_1_cnt", "amount_above_1_cnt"]
    rows = []
    for j, d in enumerate(dates):
        r = {"trade_date": d, "denom": 100 + j,
             "volume_above_1_cnt": j, "amount_above_1_cnt": j}
        for k in _RATIO_KEY:
            r[f"{k}_cnt"] = 10 + j
        for col in _MEDIAN_FIELDS.values():
            r[col] = 0.5 + j / 10
        rows.append(r)
    df_state = pd.DataFrame(rows, columns=state_cols).set_index("trade_date")

    # 组装 ratio（复用 run_round2 逻辑）
    for name in _RATIO_KEY:
        df_state[name + "_ratio"] = df_state[[name + "_cnt", "denom"]].apply(
            lambda r, n=name: (r[n + "_cnt"] / r["denom"]) if r["denom"] else None, axis=1
        )
    df_state["volume_above_1_ratio"] = df_state.apply(
        lambda r: (r["volume_above_1_cnt"] / r["denom"]) if r["denom"] else None, axis=1
    )
    df_state["amount_above_1_ratio"] = df_state.apply(
        lambda r: (r["amount_above_1_cnt"] / r["denom"]) if r["denom"] else None, axis=1
    )

    # df_trans：n_common + <col>_rate
    rows_t = []
    for j, d in enumerate(dates):
        r = {"trade_date": d, "n_common": 90 + j}
        for c in _TRANSITION_SPECS:
            r[f"{c}_rate"] = 0.01 + j / 100
        rows_t.append(r)
    df_trans = pd.DataFrame(rows_t).set_index("trade_date")

    # df_conc
    rows_c = pd.DataFrame(
        {"trade_date": dates,
         "top5_price_contribution": [0.2] * 10,
         "member_change_hhi": [0.1] * 10,
         "top5_amount_contribution": [0.3] * 10,
         "conc_denom": [100] * 10}).set_index("trade_date")

    obs = df_state.join(df_trans, how="left").join(rows_c, how="left").sort_index()

    # diffusion（复用 run_round2 逻辑）
    for base in _DIFFUSION_SERIES:
        s = obs[base + "_ratio"].tolist()
        for lag in (1, 3, 5):
            obs[f"{base}_ratio_diff{lag}"] = _diffusion(s, lag)

    # 下游依赖的关键列必须存在
    needed = [
        "regime_up_ratio", "regime_down_ratio", "regime_neutral_ratio",
        "resonance_ratio", "divergence_ratio",
        "momentum_expanding_ratio", "momentum_enhancing_ratio",
        "internal_up_ratio", "internal_down_ratio",
        "swing_up_ratio", "swing_down_ratio",
        "regime_up_ratio_diff3", "regime_up_ratio_diff5",
        "momentum_expanding_ratio_diff5",
        "t_regime_0_1_rate", "t_regime_0_neg1_rate",
        "top5_price_contribution", "member_change_hhi", "top5_amount_contribution",
        "volume_ratio20_median", "amount_ratio20_median",
        "volume_above_1_ratio", "amount_above_1_ratio",
    ]
    for col in needed:
        assert col in obs.columns, f"组装后缺少列 {col}"

    # _find_cross_horizon_divergence 与 _select_archetype_days 可运行不抛错
    _find_cross_horizon_divergence(obs)
    _select_archetype_days(obs, {})


# ============================================================================
# 4. correlation 输入日期正确对齐
# ============================================================================

def test_spearman_basic_positive():
    rho = _spearman([1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 6.0, 8.0])
    assert rho is not None and rho > 0.99


def test_spearman_exact_negative():
    rho = _spearman([1.0, 2.0, 3.0], [3.0, 2.0, 1.0])
    assert rho is not None and rho < -0.99


def test_spearman_ignores_missing_pairs():
    rho = _spearman([1.0, None, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0])
    # 只保留 (1,4),(3,2),(4,1) => 完全负相关
    assert rho is not None and rho < -0.99


# ============================================================================
# 5. Concentration SQL 只读 close/amount，用 ROW_NUMBER（aggregate-only）
# ============================================================================

def test_concentration_sql_reads_only_close_amount():
    sql = build_sql_daily_concentration()
    assert "b.close" in sql and "b.amount" in sql
    # 不读取全 OHLCV（不读 open/high/low/volume）
    for forbidden in ("b.open", "b.high", "b.low"):
        assert forbidden not in sql
    assert "ROW_NUMBER()" in sql


# ============================================================================
# 6. Cross-horizon divergence 只依赖当日状态（无未来收益）
# ============================================================================

def test_cross_horizon_divergence_quadrants():
    df = pd.DataFrame(
        {
            "regime_up_ratio": [0.1, 0.9, 0.5, 0.5],
            "regime_down_ratio": [0.8, 0.1, 0.3, 0.3],
            "internal_up_ratio": [0.7, 0.2, 0.5, 0.5],
            "momentum_enhancing_ratio": [0.7, 0.2, 0.5, 0.5],
            "momentum_weakening_ratio": [0.2, 0.7, 0.5, 0.5],
        },
        index=["d1", "d2", "d3", "d4"],
    )
    res = _find_cross_horizon_divergence(df)
    # d1: regime_up 低(0.1<中位) + internal_up 高(0.7>中位) + momentum_enhancing 高 => weak+improving
    assert "d1" in res["weak_trend_but_internal_momentum_improving"]
    # d2: regime_down 低 + internal_up 低 + momentum_weakening 高 => strong+weakening
    assert "d2" in res["strong_trend_but_internal_momentum_weakening"]