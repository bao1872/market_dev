"""Round 2 — 候选观察维度（State / Transition / Breadth / Diffusion / Concentration / Participation）审计。

DB-native / query-on-demand。不生成 raw dataset / parquet / full DataFrame。

固定基线（prompt §0）：
    DEV_BASE_SHA        = 6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0
    EXP_ROUND_1_FINAL   = fbe435f568d264477e02b5e19118f390bec45781
    真正生成 Round 1 数据的 RUN SHA = 47e692bac0d0bb8856cb9061904211b40c25b13f

本文件只回答候选维度是否：提供不同信息 / 高度重复 / 稳定 / 能解释不同类型市场日。
不预测、不评价机会/风险、不涉及行业/概念/style/index、不做 P/Q/U/C/V 最终评价、不改 Review PRD。

核心产物：
    round2_daily_observation.csv        # 120 rows × ~55 cols（分析结果，可保存 commit）
    round2_correlations.json            # Spearman 相关矩阵 + State vs Transition + Redundancy
    round2_archetype_days.json          # 自动挑选 8~12 个典型日期
    ROUND2_SUMMARY.md                   # A~H 结论

Denominators：
    每日     = 当日 valid_for_market_aggregation=true 的 universe
    Transition = T-1 与 T 都存在有效状态的共同 instruments
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

# 复用 Round 1 的 read-only session / DSN / trade-date 工具（throwaway research，不建 framework）
from experiments.review_market_observation.round1.round1_db_native import (  # noqa: E402
    SessionGuard,
    resolve_runtime_dsn,
    DEV_BASE_SHA_REQUIRED,
    EXPECTED_ALGORITHM_VERSION,
    EXPECTED_HISTORY_CONTRACT_VERSION,
    TARGET_TRADE_DATE_COUNT,
)
from experiments.review_market_observation.round1.dataset_schema import (  # noqa: E402
    build_selected_trade_dates,
    validate_120_consecutive_trade_dates,
)

_STATE_TABLE = "first_pyramid_history_daily_state"
_BARS_TABLE = "bars_daily"

# 分类比重字段：base 名 → (value_of_interest, is_numeric)。列名统一为 <base>_cnt / <base>_ratio。
_RATIO_SPECS: dict[str, tuple[Any, bool]] = {
    "regime_up": ("1", True),
    "regime_neutral": ("0", True),
    "regime_down": ("-1", True),
    "swing_up": ("1", True),
    "swing_down": ("-1", True),
    "internal_up": ("1", True),
    "internal_down": ("-1", True),
    "resonance": ("共振", False),
    "divergence": ("背离", False),
    "momentum_expanding": ("expanding", False),
    "momentum_contracting": ("contracting", False),
    "momentum_enhancing": ("enhancing", False),
    "momentum_weakening": ("weakening", False),
}
# <ratio base 名> -> payload key
_RATIO_KEY: dict[str, str] = {
    "regime_up": "regime_value",
    "regime_neutral": "regime_value",
    "regime_down": "regime_value",
    "swing_up": "swing_bias",
    "swing_down": "swing_bias",
    "internal_up": "internal_bias",
    "internal_down": "internal_bias",
    "resonance": "structure_alignment",
    "divergence": "structure_alignment",
    "momentum_expanding": "momentum_direction",
    "momentum_contracting": "momentum_direction",
    "momentum_enhancing": "momentum_change",
    "momentum_weakening": "momentum_change",
}

# 每日中位数字段（payload key → 列名）
_MEDIAN_FIELDS: dict[str, str] = {
    "regime_strength": "regime_strength_median",
    "sqzmom_val": "sqzmom_median",
    "sqzmom_delta": "sqzmom_delta_median",
    "volume_ratio_20": "volume_ratio20_median",
    "review_amount_ratio20": "amount_ratio20_median",
}

# 参与度（§10）与 Activity（§4）复用同一批字段
_PARTICIPATION_FIELDS = (
    "volume_ratio20_median",
    "amount_ratio20_median",
    "volume_above_1_ratio",
    "amount_above_1_ratio",
)

# Transition 原子 flows（§5）：<col> -> (payload key, prev_value, curr_value)
_TRANSITION_SPECS: dict[str, tuple[str, Any, Any]] = {
    "t_regime_0_1": ("regime_value", "0", "1"),
    "t_regime_0_neg1": ("regime_value", "0", "-1"),
    "t_regime_1_0": ("regime_value", "1", "0"),
    "t_regime_neg1_0": ("regime_value", "-1", "0"),
    "t_swing_neg1_1": ("swing_bias", "-1", "1"),
    "t_swing_1_neg1": ("swing_bias", "1", "-1"),
    "t_internal_neg1_1": ("internal_bias", "-1", "1"),
    "t_internal_1_neg1": ("internal_bias", "1", "-1"),
    "t_momdir_contract_expand": ("momentum_direction", "contracting", "expanding"),
    "t_momdir_expand_contract": ("momentum_direction", "expanding", "contracting"),
    "t_momchg_enhance_weaken": ("momentum_change", "enhancing", "weakening"),
    "t_momchg_weaken_enhance": ("momentum_change", "weakening", "enhancing"),
    "t_align_div_resonance": ("structure_alignment", "背离", "共振"),
    "t_align_resonance_div": ("structure_alignment", "共振", "背离"),
}


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _payload(key: str) -> str:
    return f"(s.state_payload ->> {_quote(key)})"


def _valid() -> str:
    return f"({_payload('valid_for_market_aggregation')})::boolean IS TRUE"


# ============================================================================
# SQL builders
# ============================================================================

def build_sql_daily_state() -> str:
    """每日 State/Breadth 聚合（GROUP BY trade_date，返回 ~120 行）。"""
    cols = [
        "s.trade_date",
        f"COUNT(*) FILTER (WHERE {_valid()}) AS denom",
    ]
    # 各 categorical ratio 计数（分母为 valid universe）
    for name, key in _RATIO_KEY.items():
        val, numeric = _RATIO_SPECS[name]
        if numeric:
            val_expr = f"{_payload(key)}::int = {val}"
        else:
            val_expr = f"{_payload(key)} = {_quote(val)}"
        cols.append(
            f"COUNT(*) FILTER (WHERE {_valid()} AND {val_expr}) AS {name}_cnt"
        )
    # 中位数
    for key, col in _MEDIAN_FIELDS.items():
        cols.append(
            f"percentile_cont(0.50) WITHIN GROUP (ORDER BY {_payload(key)}::numeric) "
            f"FILTER (WHERE {_valid()} AND {_payload(key)} IS NOT NULL) AS {col}"
        )
    # 参与度 >1 ratio
    cols.append(
        f"COUNT(*) FILTER (WHERE {_valid()} AND {_payload('volume_ratio_20')}::numeric > 1) "
        f"AS volume_above_1_cnt"
    )
    cols.append(
        f"COUNT(*) FILTER (WHERE {_valid()} AND {_payload('review_amount_ratio20')}::numeric > 1) "
        f"AS amount_above_1_cnt"
    )
    return f"""
SELECT
    {', '.join('    ' + c for c in cols)}
FROM {_STATE_TABLE} s
WHERE trade_date = ANY(%(trade_dates)s)
  AND algorithm_version = %(algo)s
  AND history_contract_version = %(hc)s
GROUP BY s.trade_date
ORDER BY s.trade_date ASC
"""


def build_sql_daily_transition() -> str:
    """每日 Transition rates（T-1/T 共同 valid universe；LAG 按前一交易日）。"""
    # common valid pair 判定：本日 valid 且前一交易日 valid
    common_cond = (
        f"({_payload('valid_for_market_aggregation')})::boolean IS TRUE "
        f"AND ({_payload('valid_for_market_aggregation')})::boolean IS TRUE"  # prev 用 prev_ 前缀处理
    )
    # 每个 transition col 的 SQL 条件（基于 LAG 结果）
    trans_cols: list[str] = []
    for col, (key, prev_v, curr_v) in _TRANSITION_SPECS.items():
        if key in ("regime_value", "swing_bias", "internal_bias"):
            prev_expr = f"prev_{key}::int = {prev_v}"
            curr_expr = f"curr_{key}::int = {curr_v}"
        else:
            prev_expr = f"prev_{key} = {_quote(prev_v)}"
            curr_expr = f"curr_{key} = {_quote(curr_v)}"
        trans_cols.append(
            f"COUNT(*) FILTER (WHERE {prev_expr} AND {curr_expr}) AS {col}"
        )
    return f"""
WITH per_instr AS (
    SELECT
        s.instrument_id,
        s.trade_date,
        ({_payload('valid_for_market_aggregation')})::boolean                       AS valid,
        {_payload('regime_value')}::int        AS curr_regime_value,
        {_payload('swing_bias')}::int          AS curr_swing_bias,
        {_payload('internal_bias')}::int       AS curr_internal_bias,
        {_payload('momentum_direction')}       AS curr_momentum_direction,
        {_payload('momentum_change')}          AS curr_momentum_change,
        {_payload('structure_alignment')}      AS curr_structure_alignment,
        LAG({_payload('valid_for_market_aggregation')}::boolean) OVER (
            PARTITION BY s.instrument_id ORDER BY s.trade_date)                      AS prev_valid,
        LAG({_payload('regime_value')}::int) OVER (
            PARTITION BY s.instrument_id ORDER BY s.trade_date)                      AS prev_regime_value,
        LAG({_payload('swing_bias')}::int) OVER (
            PARTITION BY s.instrument_id ORDER BY s.trade_date)                      AS prev_swing_bias,
        LAG({_payload('internal_bias')}::int) OVER (
            PARTITION BY s.instrument_id ORDER BY s.trade_date)                      AS prev_internal_bias,
        LAG({_payload('momentum_direction')}) OVER (
            PARTITION BY s.instrument_id ORDER BY s.trade_date)                      AS prev_momentum_direction,
        LAG({_payload('momentum_change')}) OVER (
            PARTITION BY s.instrument_id ORDER BY s.trade_date)                      AS prev_momentum_change,
        LAG({_payload('structure_alignment')}) OVER (
            PARTITION BY s.instrument_id ORDER BY s.trade_date)                      AS prev_structure_alignment
    FROM {_STATE_TABLE} s
    WHERE trade_date = ANY(%(trade_dates)s)
      AND algorithm_version = %(algo)s
      AND history_contract_version = %(hc)s
)
SELECT
    trade_date,
    COUNT(*) FILTER (WHERE valid AND prev_valid IS TRUE) AS n_common,
    {', '.join('    ' + c for c in trans_cols)}
FROM per_instr
GROUP BY trade_date
ORDER BY trade_date ASC
"""


def build_sql_daily_concentration() -> str:
    """每日 Concentration（§9，C 类最简单 3 候选，只读 bars_daily close/amount）。

    denominator = 当日 valid universe。
    top5_price_contribution = sum(top5 |1d_return|) / sum(all |1d_return|)
    member_change_hhi        = sum((|ret|/sum|ret|)^2)
    top5_amount_contribution = sum(top5 amount) / sum(all amount)
    """
    return f"""
WITH valid AS (
    SELECT s.instrument_id, s.trade_date
    FROM {_STATE_TABLE} s
    WHERE trade_date = ANY(%(trade_dates)s)
      AND algorithm_version = %(algo)s
      AND history_contract_version = %(hc)s
      AND ({_payload('valid_for_market_aggregation')})::boolean IS TRUE
),
daily AS (
    SELECT b.instrument_id, b.trade_date, b.close, b.amount
    FROM {_BARS_TABLE} b
    JOIN valid v ON v.instrument_id = b.instrument_id AND v.trade_date = b.trade_date
    WHERE b.close IS NOT NULL AND b.amount IS NOT NULL
),
prev AS (
    SELECT instrument_id, trade_date, close, amount,
        LAG(close) OVER (PARTITION BY instrument_id ORDER BY trade_date) AS prev_close
    FROM daily
),
rets AS (
    SELECT instrument_id, trade_date,
        CASE WHEN prev_close IS NOT NULL AND prev_close <> 0
             THEN (close - prev_close) / prev_close END AS ret,
        amount
    FROM prev
),
ranked AS (
    SELECT trade_date,
        ABS(ret) AS abs_ret, amount,
        ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY ABS(ret) DESC) AS rn_ret,
        ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY amount DESC)   AS rn_amount,
        SUM(ABS(ret)) OVER (PARTITION BY trade_date)        AS total_abs_ret,
        SUM(amount) OVER (PARTITION BY trade_date)          AS total_amount
    FROM rets
    WHERE ret IS NOT NULL
)
SELECT
    trade_date,
    SUM(CASE WHEN rn_ret <= 5 THEN abs_ret ELSE 0 END) / NULLIF(MAX(total_abs_ret), 0)
        AS top5_price_contribution,
    SUM((abs_ret / NULLIF(total_abs_ret, 0)) ^ 2)
        AS member_change_hhi,
    SUM(CASE WHEN rn_amount <= 5 THEN amount ELSE 0 END) / NULLIF(MAX(total_amount), 0)
        AS top5_amount_contribution,
    COUNT(*) AS conc_denom
FROM ranked
GROUP BY trade_date
ORDER BY trade_date ASC
"""


# ============================================================================
# Python 计算：diffusion / correlations / quadrants / divergence / archetypes
# ============================================================================

# Diffusion 要在哪些 breadth series 上算 Δ1D/Δ3D/Δ5D（§6）。
# 这里用 base 名（无 _ratio 后缀），run_round2 内以 <base>_ratio 取列、<base>_ratio_diffN 命名。
_DIFFUSION_SERIES = (
    "regime_up", "regime_down",
    "swing_up", "swing_down",
    "internal_up", "internal_down",
    "momentum_expanding", "momentum_enhancing",
)


def _spearman(a: list[float], b: list[float]) -> float | None:
    """Spearman rank correlation（无 scipy 依赖，纯 Python）。"""
    n = len(a)
    if n < 3 or len(b) != n:
        return None
    pairs = list(zip(a, b))
    # 双侧非空
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]

    def rank(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    rx = rank(xs)
    ry = rank(ys)
    mx = sum(rx) / len(rx)
    my = sum(ry) / len(ry)
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(len(rx)))
    den = (sum((rx[i] - mx) ** 2 for i in range(len(rx)))
           * sum((ry[i] - my) ** 2 for i in range(len(ry)))) ** 0.5
    if den == 0:
        return None
    return num / den


def _diffusion(series: dict[str, list[float]], lag: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(series)):
        if i < lag:
            out.append(None)
        else:
            prev = series[i - lag]
            cur = series[i]
            if prev is None or cur is None:
                out.append(None)
            else:
                out.append(cur - prev)
    return out


def run_round2(
    *,
    database_url: str,
    out_dir: Path,
    end_date: date,
    dev_base_sha: str,
    expected_exp_sha: str,
    dry_run: bool = False,
) -> dict:
    repo_root = _repo_root_from_file()
    resolved_exp_sha = _resolve_git_head(repo_root)
    if dry_run:
        return {
            "mode": "dry-run",
            "repo_root": str(repo_root),
            "resolved_exp_sha": resolved_exp_sha,
            "expected_exp_sha": expected_exp_sha,
            "exp_sha_match": resolved_exp_sha == expected_exp_sha,
            "dev_base_sha": dev_base_sha,
            "dev_base_sha_matches_required": dev_base_sha == DEV_BASE_SHA_REQUIRED,
            "end_date": str(end_date),
            "sql_shapes": {
                "daily_state_has_denom": "denom" in build_sql_daily_state(),
                "transition_has_lag": "LAG(" in build_sql_daily_transition(),
                "concentration_has_rownumber": "ROW_NUMBER()" in build_sql_daily_concentration(),
            },
            "diffusion_series": list(_DIFFUSION_SERIES),
            "transition_specs": list(_TRANSITION_SPECS),
        }

    if not database_url:
        raise RuntimeError("database_url required for real run")
    if end_date is None or end_date == date(1970, 1, 1):
        raise RuntimeError("--end-date required (§0 fail-closed)")
    if dev_base_sha != DEV_BASE_SHA_REQUIRED:
        raise RuntimeError(f"DEV_BASE required={DEV_BASE_SHA_REQUIRED!r} got={dev_base_sha!r}")
    if resolved_exp_sha != expected_exp_sha:
        raise RuntimeError(f"EXP_SHA mismatch arg={expected_exp_sha!r} vs HEAD={resolved_exp_sha!r}")

    PARAMS_BASE = {
        "algo": EXPECTED_ALGORITHM_VERSION,
        "hc": EXPECTED_HISTORY_CONTRACT_VERSION,
    }

    # ---- Step 0: 交易日 ----
    with SessionGuard(database_url) as cur:
        cur.execute(
            f"SELECT DISTINCT trade_date FROM {_STATE_TABLE} "
            f"WHERE trade_date IS NOT NULL AND algorithm_version=%(algo)s "
            f"AND history_contract_version=%(hc)s ORDER BY trade_date DESC LIMIT 300",
            PARAMS_BASE,
        )
        cand_desc = [r[0] for r in cur.fetchall()]
    elig_asc = sorted(
        [d for d in cand_desc if d is not None and date.fromisoformat(str(d)) <= end_date],
        key=str,
    )
    trade_dates = build_selected_trade_dates(elig_asc, TARGET_TRADE_DATE_COUNT)
    if not trade_dates:
        raise RuntimeError(f"no canonical trade dates <= {end_date}")
    date_info = validate_120_consecutive_trade_dates(trade_dates)
    params = dict(PARAMS_BASE, trade_dates=trade_dates)

    # ---- Step 1: 每日 State/Breadth ----
    with SessionGuard(database_url) as cur:
        cur.execute(build_sql_daily_state(), params)
        cols = [d[0] for d in cur.description]
        state_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---- Step 2: 每日 Transition ----
    with SessionGuard(database_url) as cur:
        cur.execute(build_sql_daily_transition(), params)
        tcols = [d[0] for d in cur.description]
        trans_rows = [dict(zip(tcols, r)) for r in cur.fetchall()]

    # ---- Step 3: 每日 Concentration ----
    with SessionGuard(database_url) as cur:
        cur.execute(build_sql_daily_concentration(), params)
        ccols = [d[0] for d in cur.description]
        conc_rows = [dict(zip(ccols, r)) for r in cur.fetchall()]

    # ---- 组装 daily observation table ----
    df_state = pd.DataFrame(state_rows).set_index("trade_date")
    df_trans = pd.DataFrame(trans_rows).set_index("trade_date")
    df_conc = pd.DataFrame(conc_rows).set_index("trade_date")

    # ratio 列 = cnt / denom
    for name in _RATIO_KEY:
        df_state[name + "_ratio"] = df_state[[name + "_cnt", "denom"]].apply(
            lambda r: (r[name + "_cnt"] / r["denom"]) if r["denom"] else None, axis=1
        )
    df_state["volume_above_1_ratio"] = df_state.apply(
        lambda r: (r["volume_above_1_cnt"] / r["denom"]) if r["denom"] else None, axis=1
    )
    df_state["amount_above_1_ratio"] = df_state.apply(
        lambda r: (r["amount_above_1_cnt"] / r["denom"]) if r["denom"] else None, axis=1
    )
    # transition rates = count / n_common
    for col in _TRANSITION_SPECS:
        df_trans[col + "_rate"] = df_trans.apply(
            lambda r: (r[col] / r["n_common"]) if r["n_common"] else None, axis=1
        )

    obs = df_state.join(df_trans, how="left").join(df_conc, how="left")
    obs = obs.sort_index()

    # ---- Step 4: Diffusion (Δ1D/Δ3D/Δ5D) ----
    for series in _DIFFUSION_SERIES:
        base = obs[series + "_ratio"].tolist()
        for lag in (1, 3, 5):
            obs[f"{series}_ratio_diff{lag}"] = _diffusion(base, lag)

    # 数值化（保留日期索引）
    obs_csv = obs.copy()
    obs_csv.index.name = "trade_date"

    # ---- Step 5: Correlations / Redundancy ----
    numeric_cols = [c for c in obs.columns if obs[c].dtype.kind in "fi"]
    corr_matrix: dict[str, dict[str, float | None]] = {}
    for a in numeric_cols:
        corr_matrix[a] = {}
        for b in numeric_cols:
            corr_matrix[a][b] = _spearman(
                [x if x == x else None for x in obs[a].tolist()],
                [x if x == x else None for x in obs[b].tolist()],
            )

    # redundancy: |rho| > 0.85
    redundancy: list[dict[str, Any]] = []
    cols_list = numeric_cols
    for i in range(len(cols_list)):
        for j in range(i + 1, len(cols_list)):
            a, b = cols_list[i], cols_list[j]
            rho = corr_matrix[a][b]
            if rho is not None and abs(rho) > 0.85:
                redundancy.append({
                    "a": a, "b": b, "spearman": round(rho, 4),
                    "flag": "HIGH_REDUNDANCY_CANDIDATE",
                })

    # ---- Step 6: State vs Transition (§7) ----
    state_transition_pairs = [
        ("regime_up_ratio", "t_regime_0_1_rate"),
        ("regime_down_ratio", "t_regime_0_neg1_rate"),
        ("swing_up_ratio", "t_swing_neg1_1_rate"),
        ("momentum_expanding_ratio", "t_momdir_contract_expand_rate"),
    ]
    state_transition = []
    highest_lowest_dates: dict[str, dict[str, str | None]] = {}
    scatter_pairs: dict[str, list[list[float | None]]] = {}
    for a, b in state_transition_pairs:
        x = [v if v == v else None for v in obs[a].tolist()]
        y = [v if v == v else None for v in obs[b].tolist()]
        rho = _spearman(x, y)
        state_transition.append({"state": a, "transition": b, "spearman": rho})
        scatter_pairs[f"{a}__vs__{b}"] = [
            [obs.index[i], x[i], y[i]] for i in range(len(obs)) if x[i] is not None and y[i] is not None
        ]
        # 最高/最低 transition rate 日期
        y_series = obs[b]
        finite = y_series.dropna()
        if not finite.empty:
            highest_lowest_dates[b] = {
                "highest": str(finite.idxmax()),
                "lowest": str(finite.idxmin()),
            }

    # ---- Step 7: Breadth vs Diffusion 四象限 (§8) ----
    # 用 regime_up_ratio 作为 breadth level，regime_up_ratio_diff3 作为 diffusion
    quadrants: dict[str, list[str]] = {"A_high_pos": [], "B_high_neg": [], "C_low_pos": [], "D_low_neg": []}
    breadth_series = "regime_up_ratio"
    diff_series = "regime_up_ratio_diff3"
    b_vals = obs[breadth_series].tolist()
    d_vals = obs[diff_series].tolist()
    b_med = pd.Series([v for v in b_vals if v == v]).median()
    for i, dt in enumerate(obs.index):
        bv, dv = b_vals[i], d_vals[i]
        if bv is None or dv is None:
            continue
        high = bv >= b_med
        pos = dv >= 0
        if high and pos:
            quadrants["A_high_pos"].append(str(dt))
        elif high and not pos:
            quadrants["B_high_neg"].append(str(dt))
        elif not high and pos:
            quadrants["C_low_pos"].append(str(dt))
        else:
            quadrants["D_low_neg"].append(str(dt))

    # ---- Step 8: Concentration vs Breadth (§9) ----
    conc_breadth = {
        "top5_price_contribution__vs__regime_up_ratio": _spearman(
            [v if v == v else None for v in obs["top5_price_contribution"].tolist()],
            [v if v == v else None for v in obs["regime_up_ratio"].tolist()],
        ),
        "member_change_hhi__vs__regime_up_ratio": _spearman(
            [v if v == v else None for v in obs["member_change_hhi"].tolist()],
            [v if v == v else None for v in obs["regime_up_ratio"].tolist()],
        ),
        "top5_amount_contribution__vs__regime_up_ratio": _spearman(
            [v if v == v else None for v in obs["top5_amount_contribution"].tolist()],
            [v if v == v else None for v in obs["regime_up_ratio"].tolist()],
        ),
    }

    # ---- Step 9: Participation redundancy (§10) ----
    participation = {}
    for m in _PARTICIPATION_FIELDS:
        rho_with_state = _spearman(
            [v if v == v else None for v in obs[m].tolist()],
            [v if v == v else None for v in obs["regime_up_ratio"].tolist()],
        )
        rho_with_momentum = _spearman(
            [v if v == v else None for v in obs[m].tolist()],
            [v if v == v else None for v in obs["momentum_expanding_ratio"].tolist()],
        )
        participation[m] = {
            "vs_regime_up": rho_with_state,
            "vs_momentum_expanding": rho_with_momentum,
            "redundant_flag": (
                "REDUNDANT_CANDIDATE"
                if (rho_with_state is not None and abs(rho_with_state) > 0.85)
                or (rho_with_momentum is not None and abs(rho_with_momentum) > 0.85)
                else "NOT_REDUNDANT"
            ),
        }

    # ---- Step 10: Cross-horizon divergence (§13) ----
    divergence_days = _find_cross_horizon_divergence(obs)
    weak_improving = divergence_days["weak_trend_but_internal_momentum_improving"]
    strong_weakening = divergence_days["strong_trend_but_internal_momentum_weakening"]

    # ---- Step 11: Archetype days (§12) ----
    archetypes = _select_archetype_days(obs, conc_breadth)

    # ---- 输出 ----
    out_dir.mkdir(parents=True, exist_ok=True)
    obs_csv.to_csv(out_dir / "round2_daily_observation.csv")
    correlations = {
        "state_transition": state_transition,
        "scatter_pairs": scatter_pairs,
        "highest_lowest_transition_dates": highest_lowest_dates,
        "breadth_vs_diffusion_quadrants": {k: {"dates": v, "count": len(v)} for k, v in quadrants.items()},
        "breadth_series_for_quadrant": {"breadth": breadth_series, "diffusion": diff_series,
                                        "breadth_median": round(float(b_med), 4) if b_med == b_med else None},
        "concentration_vs_breadth": conc_breadth,
        "participation": participation,
        "redundancy": redundancy,
        "redundancy_count": len(redundancy),
        "cross_horizon_divergence": {
            "weak_trend_but_internal_momentum_improving": weak_improving,
            "strong_trend_but_internal_momentum_weakening": strong_weakening,
            "weak_improving_count": len(weak_improving),
            "strong_weakening_count": len(strong_weakening),
        },
    }
    (out_dir / "round2_correlations.json").write_text(
        json.dumps(correlations, indent=2, ensure_ascii=False, default=str)
    )
    (out_dir / "round2_archetype_days.json").write_text(
        json.dumps(archetypes, indent=2, ensure_ascii=False, default=str)
    )

    summary = _build_round2_summary(
        obs=obs,
        date_info=date_info,
        state_transition=state_transition,
        quadrants=quadrants,
        conc_breadth=conc_breadth,
        participation=participation,
        redundancy=redundancy,
        divergence_days=divergence_days,
        archetypes=archetypes,
        resolved_exp_sha=resolved_exp_sha,
    )
    (out_dir / "ROUND2_SUMMARY.md").write_text(summary)

    return {
        "round2_daily_path": str(out_dir / "round2_daily_observation.csv"),
        "round2_correlations_path": str(out_dir / "round2_correlations.json"),
        "round2_archetype_path": str(out_dir / "round2_archetype_days.json"),
        "round2_summary_path": str(out_dir / "ROUND2_SUMMARY.md"),
        "trade_date_count": date_info["count"],
        "trade_date_start": date_info["start"],
        "trade_date_end": date_info["end"],
        "is_exact_target": date_info["is_exact_target"],
        "redundancy_count": len(redundancy),
        "archetype_count": len(archetypes.get("archetype_days", [])),
    }


def _find_cross_horizon_divergence(obs: pd.DataFrame) -> dict[str, list[str]]:
    """§13: 长周期状态 vs 短周期/internal 变化方向不一致。

    长周期(Trend weak)  = regime_down or regime_neutral 主导（regime_up_ratio 低）
    短周期/internal 改善 = internal_up_ratio 上升 且 momentum_enhancing_ratio 上升
    相反方向 = Trend strong + Internal/Momentum weakening

    简化量化（避免过度建模）：
      trend_weak_mask  = regime_up_ratio 低于其中位数
      short_improve    = (internal_up_ratio 高于其中位数) AND (momentum_enhancing_ratio 高于其中位数)
      coordinated      = trend_weak AND NOT short_improve（悲观一致）
                        OR trend_strong AND NOT short_improve（乐观一致）
      divergent        = trend_weak AND short_improve（悲观但短周期改善）
                        OR trend_strong AND short_improve（乐观且短周期同步改善→不算分歧）
    这里只标记：(weak + improving) 与 (strong + weakening)
    """
    def median(c):
        s = obs[c].dropna()
        return s.median() if not s.empty else None

    ru_med = median("regime_up_ratio")
    iu_med = median("internal_up_ratio")
    me_med = median("momentum_enhancing_ratio")
    rd_med = median("regime_down_ratio")
    mw_med = median("momentum_weakening_ratio")

    weak_improving: list[str] = []
    strong_weakening: list[str] = []
    for dt, row in obs.iterrows():
        try:
            ru = float(row["regime_up_ratio"]) if pd.notna(row["regime_up_ratio"]) else None
            iu = float(row["internal_up_ratio"]) if pd.notna(row["internal_up_ratio"]) else None
            me = float(row["momentum_enhancing_ratio"]) if pd.notna(row["momentum_enhancing_ratio"]) else None
            rd = float(row["regime_down_ratio"]) if pd.notna(row["regime_down_ratio"]) else None
            mw = float(row["momentum_weakening_ratio"]) if pd.notna(row["momentum_weakening_ratio"]) else None
        except (TypeError, ValueError):
            continue
        # weak + improving
        if (ru is not None and ru_med is not None and ru < ru_med
                and iu is not None and iu_med is not None and iu > iu_med
                and me is not None and me_med is not None and me > me_med):
            weak_improving.append(str(dt))
        # strong + weakening
        if (rd is not None and rd_med is not None and rd < rd_med
                and iu is not None and iu_med is not None and iu < iu_med
                and mw is not None and mw_med is not None and mw > mw_med):
            strong_weakening.append(str(dt))
    return {
        "weak_trend_but_internal_momentum_improving": weak_improving,
        "strong_trend_but_internal_momentum_weakening": strong_weakening,
    }


def _select_archetype_days(obs: pd.DataFrame, conc_breadth: dict) -> dict:
    """§12: 从 120 日自动挑 ~8-12 个典型日期（不人工挑）。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(tag: str, dt, reason: str, score: float | None = None):
        if dt is None or str(dt) in seen:
            return
        seen.add(str(dt))
        row = obs.loc[dt]
        out.append({
            "trade_date": str(dt),
            "tag": tag,
            "reason": reason,
            "score": round(float(score), 4) if score is not None and score == score else None,
            "state": {
                "regime_up": _f(row["regime_up_ratio"]),
                "regime_neutral": _f(row["regime_neutral_ratio"]),
                "regime_down": _f(row["regime_down_ratio"]),
                "swing_up": _f(row["swing_up_ratio"]),
                "swing_down": _f(row["swing_down_ratio"]),
                "internal_up": _f(row["internal_up_ratio"]),
                "internal_down": _f(row["internal_down_ratio"]),
                "resonance": _f(row["resonance_ratio"]),
                "divergence": _f(row["divergence_ratio"]),
                "momentum_expanding": _f(row["momentum_expanding_ratio"]),
                "momentum_enhancing": _f(row["momentum_enhancing_ratio"]),
            },
            "transition": {c: _f(row.get(c + "_rate")) for c in _TRANSITION_SPECS},
            "diffusion": {
                "regime_up_d3": _f(row.get("regime_up_ratio_diff3")),
                "regime_down_d3": _f(row.get("regime_down_ratio_diff3")),
                "momentum_expanding_d3": _f(row.get("momentum_expanding_ratio_diff3")),
            },
            "concentration": {
                "top5_price": _f(row.get("top5_price_contribution")),
                "hhi": _f(row.get("member_change_hhi")),
                "top5_amount": _f(row.get("top5_amount_contribution")),
            },
            "participation": {
                "volume_ratio20_median": _f(row.get("volume_ratio20_median")),
                "amount_ratio20_median": _f(row.get("amount_ratio20_median")),
                "volume_above_1": _f(row.get("volume_above_1_ratio")),
                "amount_above_1": _f(row.get("amount_above_1_ratio")),
            },
        })

    # 1) 最大 regime breadth expansion / contraction（Δ5D）
    if "regime_up_ratio_diff5" in obs:
        add("max_regime_breadth_expansion",
            obs["regime_up_ratio_diff5"].idxmax(), "最大 regime_up breadth 5日扩张",
            obs["regime_up_ratio_diff5"].max())
        add("max_regime_breadth_contraction",
            obs["regime_up_ratio_diff5"].idxmin(), "最大 regime_up breadth 5日收缩",
            obs["regime_up_ratio_diff5"].min())
    # 2) 最大 momentum expansion / contraction（Δ5D）
    if "momentum_expanding_ratio_diff5" in obs:
        add("max_momentum_expansion",
            obs["momentum_expanding_ratio_diff5"].idxmax(), "最大 momentum expanding 5日扩张",
            obs["momentum_expanding_ratio_diff5"].max())
        add("max_momentum_contraction",
            obs["momentum_expanding_ratio_diff5"].idxmin(), "最大 momentum expanding 5日收缩",
            obs["momentum_expanding_ratio_diff5"].min())
    # 3) 最大正/负 transition（t_regime_0_1_rate 最高/最低）
    add("max_positive_transition",
        obs["t_regime_0_1_rate"].idxmax(), "最大 regime 0->1 当日 transition rate",
        obs["t_regime_0_1_rate"].max())
    add("max_negative_transition",
        obs["t_regime_0_neg1_rate"].idxmax(), "最大 regime 0->-1 当日 transition rate",
        obs["t_regime_0_neg1_rate"].max())
    # 4) concentration 最高/最低
    add("max_concentration",
        obs["member_change_hhi"].idxmax(), "成员变化 HHI 最高",
        obs["member_change_hhi"].max())
    add("min_concentration",
        obs["member_change_hhi"].idxmin(), "成员变化 HHI 最低",
        obs["member_change_hhi"].min())
    # 5) participation 增加最大（amount_above_1_ratio diff）
    if "amount_above_1_ratio" in obs:
        d = obs["amount_above_1_ratio"].diff()
        add("max_participation_increase", d.idxmax(), "参与度(amount>1) 单日增幅最大", d.max())
    # 6) price/state 不明显但 transition 大（momentum 短周期 transition 高但 regime 平稳）
    add("high_internal_transition_low_regime_change",
        _percolation_penalty_day(obs), "短周期内部 transition 大但长周期 regime 平稳",
        None)

    return {"archetype_days": out, "count": len(out)}


def _percolation_penalty_day(obs: pd.DataFrame):
    """挑一个 regime 高度平稳但 internal/alignment transition 大的日期。"""
    best_dt, best_score = None, -1e9
    for dt, row in obs.iterrows():
        try:
            regime_trans = sum(
                float(row.get(f"{c}_rate")) if pd.notna(row.get(f"{c}_rate")) else 0.0
                for c in ("t_regime_0_1", "t_regime_0_neg1", "t_regime_1_0", "t_regime_neg1_0")
            )
            internal_trans = sum(
                float(row.get(f"{c}_rate")) if pd.notna(row.get(f"{c}_rate")) else 0.0
                for c in ("t_internal_neg1_1", "t_internal_1_neg1")
            )
            align_trans = sum(
                float(row.get(f"{c}_rate")) if pd.notna(row.get(f"{c}_rate")) else 0.0
                for c in ("t_align_div_resonance", "t_align_resonance_div")
            )
            score = (internal_trans + align_trans) - regime_trans
            if score > best_score:
                best_score, best_dt = score, dt
        except (TypeError, ValueError):
            continue
    return best_dt


def _repo_root_from_file() -> Path:
    here = Path(__file__).resolve()
    return here.parents[3]  # round2 -> review_market_observation -> experiments -> worktree root


def _resolve_git_head(search_from: Path) -> str | None:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(search_from), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=15)
        out = r.stdout.strip()
        return out if r.returncode == 0 and len(out) == 40 else None
    except Exception:
        return None


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return round(f, 4) if f == f else None
    except (TypeError, ValueError):
        return None


def _build_round2_summary(**kw) -> str:
    obs = kw["obs"]
    date_info = kw["date_info"]
    st = kw["state_transition"]
    quadrants = kw["quadrants"]
    conc_breadth = kw["conc_breadth"]
    participation = kw["participation"]
    redundancy = kw["redundancy"]
    div = kw["divergence_days"]
    arch = kw["archetypes"]
    exp_sha = kw["resolved_exp_sha"]

    def verdict(rho: float | None) -> str:
        if rho is None:
            return "INCONCLUSIVE"
        a = abs(rho)
        if a < 0.2:
            return "SUPPORTED"
        if a < 0.6:
            return "PARTIALLY_SUPPORTED"
        return "NOT_SUPPORTED"

    lines = []
    lines.append("# Round 2 Summary — 候选观察维度审计（DB-native / query-on-demand）\n")
    lines.append(f"- EXP_SHA = {exp_sha}")
    lines.append(f"- DEV_BASE_SHA = {DEV_BASE_SHA_REQUIRED}")
    lines.append(f"- 窗口 = {date_info['start']} .. {date_info['end']}（{date_info['count']} 交易日, is_exact_target={date_info['is_exact_target']}）\n")

    lines.append("## A. State vs Transition\n")
    for r in st:
        lines.append(f"- {r['state']} vs {r['transition']}: spearman={r['spearman'] if r['spearman'] is None else round(r['spearman'],3)} → {verdict(r['spearman'])}")
    lines.append("")

    lines.append("## B. Breadth vs Diffusion\n")
    for k, v in quadrants.items():
        lines.append(f"- {k}: {len(v)} 天")
    lines.append("（A=高breadth+正扩散 / B=高breadth+负扩散 / C=低breadth+正扩散 / D=低breadth+负扩散）\n")

    lines.append("## C. Breadth vs Concentration\n")
    for k, v in conc_breadth.items():
        lines.append(f"- {k}: spearman={None if v is None else round(v,3)}")
    lines.append("")

    lines.append("## D. Participation\n")
    for k, v in participation.items():
        lines.append(f"- {k}: vs_regime={None if v['vs_regime_up'] is None else round(v['vs_regime_up'],3)} "
                     f"vs_mom={None if v['vs_momentum_expanding'] is None else round(v['vs_momentum_expanding'],3)} "
                     f"→ {v['redundant_flag']}")
    lines.append("")

    lines.append("## E. Redundancy（|rho|>0.85）\n")
    if redundancy:
        for r in redundancy[:40]:
            lines.append(f"- {r['a']} ~ {r['b']}: {r['spearman']} {r['flag']}")
        lines.append(f"\n共 {len(redundancy)} 对")
    else:
        lines.append("- 无 |rho|>0.85 的候选对")
    lines.append("")

    lines.append("## F. Cross-horizon divergence\n")
    lines.append(f"- weak_trend + internal/momentum improving: {len(div['weak_trend_but_internal_momentum_improving'])} 天")
    lines.append(f"- strong_trend + internal/momentum weakening: {len(div['strong_trend_but_internal_momentum_weakening'])} 天")
    lines.append("")

    lines.append("## G. Archetype Days\n")
    for a in arch.get("archetype_days", []):
        lines.append(f"- {a['trade_date']} [{a['tag']}] {a['reason']}")

    lines.append("\n## H. Round 2 Verdict（按 candidate）\n")
    verdicts = {
        "State vs Transition information distinctness": verdict(_avg_abs([r["spearman"] for r in st])),
        "Breadth vs Diffusion independence": verdict(_avg_abs([conc_breadth.get("member_change_hhi__vs__regime_up_ratio")])),
        "Concentration independence from Breadth": verdict(_avg_abs(list(conc_breadth.values()))),
        "Participation distinctness": verdict(_max_abs([v["vs_regime_up"] for v in participation.values()] + [v["vs_momentum_expanding"] for v in participation.values()])),
    }
    for k, v in verdicts.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("> 本结果仅用于候选维度判断，不构成 Review PRD 修改建议。")
    return "\n".join(lines)


def _avg_abs(vals) -> float | None:
    f = [abs(v) for v in vals if v is not None]
    return (sum(f) / len(f)) if f else None


def _max_abs(vals) -> float | None:
    f = [abs(v) for v in vals if v is not None]
    return max(f) if f else None


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="round2_db_native")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--dev-base-sha", required=True)
    p.add_argument("--exp-sha", required=True)
    p.add_argument("--end-date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    result = run_round2(
        database_url=args.database_url or "",
        out_dir=args.out_dir,
        end_date=args.end_date or date(1970, 1, 1),
        dev_base_sha=args.dev_base_sha,
        expected_exp_sha=args.exp_sha,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        if result["dev_base_sha_matches_required"] and result["exp_sha_match"]:
            return 0
        return 2
    print(json.dumps({k: v for k, v in result.items() if not k.endswith("_path")}, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())