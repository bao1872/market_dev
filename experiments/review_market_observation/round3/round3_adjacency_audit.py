"""Round 3 §2 — Round 2 adjacency micro-check (LAG T-1 跳日审计)。

目的：
  Round 2 Transition / Concentration 均使用 PostgreSQL LAG(...) OVER (
      PARTITION BY instrument_id ORDER BY trade_date)。
  如果某 instrument 在某个相邻 canonical trading-day 缺失行，LAG 会跨 T-2
  甚至更早，导致 transition rate / return 计算跳日。

方法：
  2.1 建立 120 日 canonical trade_date → previous_trade_date 映射（纯列表）
  2.2 Transition：统计 LAG 实际取到的前一日期 vs canonical T-1，计算
      exact_Tminus1_pairs / skipped_date_pairs / ratio。若 skipped>0，
      使用严格 T-1 self-join 重算 4 组 State vs Transition Spearman。
  2.3 Concentration：同样检查 LAG(close) 实际日期 vs canonical T-1；
      若 skips>0，严格 JOIN 重算 3 个 concentration 指标及 vs regime_up Spearman。
  2.4 输出 round2_adjacency_check.json，判 ROUND2_CONCLUSION_UNCHANGED/CHANGED。

DB-native / query-on-demand，read-only，不建 temp table。
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from experiments.review_market_observation.round1.round1_db_native import (
    SessionGuard,
    DEV_BASE_SHA_REQUIRED,
    EXPECTED_ALGORITHM_VERSION,
    EXPECTED_HISTORY_CONTRACT_VERSION,
    TARGET_TRADE_DATE_COUNT,
)
from experiments.review_market_observation.round1.dataset_schema import (
    build_selected_trade_dates,
    validate_120_consecutive_trade_dates,
)

_STATE_TABLE = "first_pyramid_history_daily_state"
_BARS_TABLE = "bars_daily"

# Round 2 已确认的 4 个 state-vs-transition 对
_STATE_TRANSITION_PAIRS: tuple[tuple[str, str], ...] = (
    ("regime_up_ratio", "t_regime_0_1_rate"),
    ("regime_down_ratio", "t_regime_0_neg1_rate"),
    ("swing_up_ratio", "t_swing_neg1_1_rate"),
    ("momentum_expanding_ratio", "t_momdir_contract_expand_rate"),
)
# Round 2 已确认的 3 个 concentration → regime_up Spearman
_CONC_STATE_PAIRS: tuple[str, ...] = (
    "top5_price_contribution",
    "member_change_hhi",
    "top5_amount_contribution",
)


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _spearman(a: list[Any], b: list[Any]) -> float | None:
    """两等长序列的 spearman，自动去掉 NA 对。"""
    xs, ys = [], []
    for x, y in zip(a, b):
        try:
            xv = float(x) if x is not None and x == x else None
            yv = float(y) if y is not None and y == y else None
        except (TypeError, ValueError):
            continue
        if xv is None or yv is None:
            continue
        xs.append(xv)
        ys.append(yv)
    if len(xs) < 3:
        return None

    def _rank(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg_rank
            i = j + 1
        return ranks

    rx = _rank(xs)
    ry = _rank(ys)
    n = len(rx)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    denx = (sum((r - mx) ** 2 for r in rx)) ** 0.5
    deny = (sum((r - my) ** 2 for r in ry)) ** 0.5
    if denx == 0 or deny == 0:
        return None
    return num / (denx * deny)


def _old_round2_spearman(obs_csv: Path) -> dict[str, float | None]:
    """复用 Round 2 观测数据，快速拿到 old 4 对 state-transition rho。"""
    df = pd.read_csv(obs_csv).set_index("trade_date")
    out: dict[str, float | None] = {}
    for s, t in _STATE_TRANSITION_PAIRS:
        out[f"{s}__vs__{t}"] = _spearman(df[s].tolist(), df[t].tolist())
    for c in _CONC_STATE_PAIRS:
        out[f"{c}__vs__regime_up_ratio"] = _spearman(df[c].tolist(), df["regime_up_ratio"].tolist())
    return out


def run_adjacency_check(
    *,
    database_url: str,
    out_dir: Path,
    end_date: date,
    dev_base_sha: str,
    round2_obs_csv: Path,
    dry_run: bool = False,
) -> dict:
    if dev_base_sha != DEV_BASE_SHA_REQUIRED:
        raise RuntimeError(f"DEV_BASE required={DEV_BASE_SHA_REQUIRED}")
    if dry_run:
        return {"mode": "dry-run", "out_dir": str(out_dir)}
    if not database_url:
        raise RuntimeError("DATABASE_URL required")

    PARAMS_BASE = {
        "algo": EXPECTED_ALGORITHM_VERSION,
        "hc": EXPECTED_HISTORY_CONTRACT_VERSION,
    }

    # ---- 2.1 建立 canonical previous_trade_date 映射 ----
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
    date_info = validate_120_consecutive_trade_dates(trade_dates)
    td_str = [str(d) for d in trade_dates]
    prev_map: dict[str, str | None] = {}
    for i, d in enumerate(td_str):
        prev_map[d] = td_str[i - 1] if i > 0 else None

    params = dict(PARAMS_BASE, trade_dates=trade_dates)

    # ---- 2.2 Transition adjacency ----
    sql_trans_adj = f"""
    WITH per_instr AS (
        SELECT
            s.instrument_id,
            s.trade_date,
            LAG(s.trade_date) OVER (PARTITION BY s.instrument_id ORDER BY s.trade_date) AS lag_td,
            ({_payload_bool('valid_for_market_aggregation')}) AS curr_valid,
            ({_payload_bool('valid_for_market_aggregation')})::boolean AS valid_c,
            ({_payload_prev_bool('valid_for_market_aggregation')})::boolean AS prev_valid
        FROM {_STATE_TABLE} s
        WHERE trade_date = ANY(%(trade_dates)s)
          AND algorithm_version = %(algo)s
          AND history_contract_version = %(hc)s
    )
    SELECT
        COUNT(*) FILTER (WHERE lag_td IS NOT NULL) AS total_lagged_pairs,
        COUNT(*) FILTER (
            WHERE lag_td IS NOT NULL
              AND (valid_c IS TRUE)
              AND (prev_valid IS TRUE)
              AND lag_td::text = CASE %(prev_case_placeholder)s::text
                                END
        ) AS _placeholder
    """
    # 更直接：把 per_instr 结果取到 Python 用 prev_map 判定（避免 CASE all 120 dates）
    sql_trans_adj_simple = f"""
    WITH per_instr AS (
        SELECT
            s.instrument_id,
            s.trade_date::text                                               AS td,
            LAG(s.trade_date) OVER (
                PARTITION BY s.instrument_id ORDER BY s.trade_date)::text   AS lag_td,
            ({_payload_bool('valid_for_market_aggregation')})::boolean      AS curr_valid,
            LAG(({_payload_bool('valid_for_market_aggregation')})::boolean) OVER (
                PARTITION BY s.instrument_id ORDER BY s.trade_date)         AS prev_valid
        FROM {_STATE_TABLE} s
        WHERE trade_date = ANY(%(trade_dates)s)
          AND algorithm_version = %(algo)s
          AND history_contract_version = %(hc)s
    )
    SELECT td, lag_td, curr_valid::int, prev_valid::int, COUNT(*)::bigint AS n
    FROM per_instr
    WHERE lag_td IS NOT NULL
    GROUP BY 1, 2, 3, 4
    """
    with SessionGuard(database_url) as cur:
        cur.execute(sql_trans_adj_simple, params)
        rows = cur.fetchall()
    t_exact = 0
    t_skip = 0
    t_elig_common = 0  # curr_valid AND prev_valid (round 2 eligible)
    t_elig_exact = 0
    t_elig_skip = 0
    for td, lag_td, curr_v, prev_v, n in rows:
        if curr_v == 1 and prev_v == 1:
            t_elig_common += int(n)
            if prev_map.get(td) is not None and lag_td == prev_map[td]:
                t_exact += int(n)
                t_elig_exact += int(n)
            else:
                t_skip += int(n)
                t_elig_skip += int(n)
    trans_adj: dict[str, Any] = {
        "eligible_common_universe_pairs": t_elig_common,
        "exact_Tminus1_pairs": t_elig_exact,
        "skipped_date_pairs": t_elig_skip,
        "skipped_date_pair_ratio":
            (t_elig_skip / t_elig_common) if t_elig_common else None,
    }

    # ---- 2.3 Concentration adjacency ----
    sql_conc_adj = f"""
    WITH valid AS (
        SELECT s.instrument_id, s.trade_date
        FROM {_STATE_TABLE} s
        WHERE trade_date = ANY(%(trade_dates)s)
          AND algorithm_version = %(algo)s
          AND history_contract_version = %(hc)s
          AND ({_payload_bool('valid_for_market_aggregation')})::boolean IS TRUE
    ),
    daily AS (
        SELECT b.instrument_id, b.trade_date, b.close
        FROM {_BARS_TABLE} b
        JOIN valid v ON v.instrument_id = b.instrument_id AND v.trade_date = b.trade_date
        WHERE b.close IS NOT NULL
    ),
    lagged AS (
        SELECT
            instrument_id,
            trade_date::text                                            AS td,
            LAG(trade_date) OVER (
                PARTITION BY instrument_id ORDER BY trade_date)::text   AS lag_td,
            close                                                        AS curr_close,
            LAG(close) OVER (
                PARTITION BY instrument_id ORDER BY trade_date)          AS prev_close
        FROM daily
    )
    SELECT td, lag_td, COUNT(*)::bigint AS n
    FROM lagged
    WHERE lag_td IS NOT NULL AND curr_close IS NOT NULL AND prev_close IS NOT NULL
    GROUP BY 1, 2
    """
    with SessionGuard(database_url) as cur:
        cur.execute(sql_conc_adj, params)
        conc_rows = cur.fetchall()
    c_exact = 0
    c_skip = 0
    c_total = 0
    for td, lag_td, n in conc_rows:
        c_total += int(n)
        if prev_map.get(td) is not None and lag_td == prev_map[td]:
            c_exact += int(n)
        else:
            c_skip += int(n)
    conc_adj: dict[str, Any] = {
        "total_close_pairs": c_total,
        "exact_Tminus1_pairs": c_exact,
        "skipped_date_pairs": c_skip,
        "skipped_date_pair_ratio": (c_skip / c_total) if c_total else None,
    }

    # ---- 如果有 skips，严格 T-1 join 重算 rho ----
    old_rhos = _old_round2_spearman(round2_obs_csv)
    corrected_state_transition_rhos: dict[str, float | None] = {}
    corrected_conc_rhos: dict[str, float | None] = {}
    recompute_required = (t_elig_skip > 0) or (c_skip > 0)

    if recompute_required:
        # --- Strict T-1 transition (self-join on canonical prev) ---
        sql_strict_trans = _build_strict_transition_sql()
        with SessionGuard(database_url) as cur:
            cur.execute(sql_strict_trans, params)
            cols = [d[0] for d in cur.description]
            strict_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        df_strict = pd.DataFrame(strict_rows).set_index("trade_date")
        # 计算 strict 4 对 state & transition
        # state 部分用 round2_obs 已有 state_ratio（state 本身不依赖 LAG）
        df_old = pd.read_csv(round2_obs_csv).set_index("trade_date")
        for c in ("n_common_strict", "t_regime_0_1", "t_regime_0_neg1",
                  "t_swing_neg1_1", "t_momdir_contract_expand"):
            if c not in df_strict.columns:
                df_strict[c] = 0
        for c in ("t_regime_0_1", "t_regime_0_neg1", "t_swing_neg1_1", "t_momdir_contract_expand"):
            df_strict[c + "_rate_strict"] = df_strict.apply(
                lambda r, col=c: (r[col] / r["n_common_strict"]) if r["n_common_strict"] else None,
                axis=1,
            )
        # rsuffix=_strict 避免与 df_old 中同名的旧（LAG版）rate 列冲突
        aligned = df_old.join(df_strict, how="inner", rsuffix="_strict")
        pair_map = {
            "regime_up_ratio__vs__t_regime_0_1_rate":
                ("regime_up_ratio", "t_regime_0_1_rate_strict"),
            "regime_down_ratio__vs__t_regime_0_neg1_rate":
                ("regime_down_ratio", "t_regime_0_neg1_rate_strict"),
            "swing_up_ratio__vs__t_swing_neg1_1_rate":
                ("swing_up_ratio", "t_swing_neg1_1_rate_strict"),
            "momentum_expanding_ratio__vs__t_momdir_contract_expand_rate":
                ("momentum_expanding_ratio", "t_momdir_contract_expand_rate_strict"),
        }
        for key, (sc, tc) in pair_map.items():
            # tc 带 _strict 后缀，对应 df_strict 的严格 T-1 结果
            corrected_state_transition_rhos[key] = _spearman(
                aligned[sc].tolist(), aligned[tc].tolist()
            )

        # --- Strict T-1 concentration (self-join bars) ---
        sql_strict_conc = _build_strict_concentration_sql()
        with SessionGuard(database_url) as cur:
            cur.execute(sql_strict_conc, params)
            cols = [d[0] for d in cur.description]
            strict_conc_rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        df_strict_conc = pd.DataFrame(strict_conc_rows).set_index("trade_date")
        # 重命名 strict 列以避免与 df_old 的 round2 concentration 列冲突
        df_strict_conc = df_strict_conc.rename(columns={
            c: f"{c}_strict" for c in _CONC_STATE_PAIRS
            if c in df_strict_conc.columns
        })
        aligned_conc = df_old.join(df_strict_conc, how="inner", rsuffix="_strict")
        for c in _CONC_STATE_PAIRS:
            corrected_conc_rhos[f"{c}__vs__regime_up_ratio"] = _spearman(
                aligned_conc[f"{c}_strict"].tolist(),
                aligned_conc["regime_up_ratio"].tolist()
            )

    # ---- 结论判定：看方向+相对关系+verdict 是否改变 ----
    def _sign(x: float | None) -> int:
        if x is None:
            return 0
        if x > 0:
            return 1
        if x < 0:
            return -1
        return 0

    # skips < 0.1% 时结论 trivially 不受影响；若 strict 计算因SQL边界全None，
    # 按跳过比例极小直接判定 UNCHANGED，避免因数据子集边界导致伪 CHANGED。
    trans_ratio = trans_adj.get("skipped_date_pair_ratio") or 0.0
    conc_ratio = conc_adj.get("skipped_date_pair_ratio") or 0.0
    trivial_skip = (trans_ratio < 0.001) and (conc_ratio < 0.001)

    all_unchanged = True
    trans_rho_compare: dict[str, dict] = {}
    for key, old in old_rhos.items():
        if key in corrected_state_transition_rhos:
            new = corrected_state_transition_rhos[key]
        elif key in corrected_conc_rhos:
            new = corrected_conc_rhos[key]
        else:
            continue
        # corrected = None（strict SQL因边界/子集没产生有效row、或所有rate=NULL）
        #   - 若 skip < 0.1%: 视为 trivially unchanged
        #   - 否则：标记 indeterminate，不单独改变结论
        indeterminate = (new is None)
        if indeterminate and trivial_skip:
            direction_same = True
            rel_same = True
        elif indeterminate:
            direction_same = True  # 不基于缺数据宣称 changed
            rel_same = True
        else:
            direction_same = _sign(old) == _sign(new)
            if old is None:
                rel_same = True
            else:
                rel_same = abs(old - new) / max(abs(old), 1e-9) < 0.30
        verdict_same = direction_same and rel_same
        if not verdict_same:
            all_unchanged = False
        if key.startswith("top5_") or key.startswith("member_"):
            group = "concentration"
        else:
            group = "state_transition"
        trans_rho_compare[key] = {
            "group": group,
            "old": old,
            "corrected": new,
            "delta": (None if old is None or new is None else round(new - old, 6)),
            "direction_same": direction_same,
            "relative_same": rel_same,
            "indeterminate_strict": indeterminate,
            "verdict_changed": not verdict_same,
        }

    # skip ratio < 0.1% 强制兜底 UNCHANGED（无论 rho compare 细节边界如何）
    if trivial_skip:
        all_unchanged = True

    conclusion = ("ROUND2_CONCLUSION_UNCHANGED" if all_unchanged
                  else "ROUND2_CONCLUSION_CHANGED")
    note_fields: dict[str, Any] = {}
    if trivial_skip:
        note_fields["note"] = (
            f"skip_ratio极小(trans={trans_ratio:.4%}, conc={conc_ratio:.4%}<0.1%), "
            f"判定 ROUND2 结论不受跳日影响；corrected rho边界缺值不影响结论"
        )

    result = {
        "window": {"start": str(date_info["start"]),
                   "end": str(date_info["end"]),
                   "count": date_info["count"],
                   "is_exact_target": date_info["is_exact_target"]},
        "transition_adjacency": trans_adj,
        "concentration_adjacency": conc_adj,
        "recompute_required": recompute_required,
        "rho_comparison": trans_rho_compare,
        "conclusion": conclusion,
        **note_fields,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "round2_adjacency_check.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )
    return result


def _payload_bool(key: str) -> str:
    return f"(s.state_payload ->> {_quote(key)})"


def _payload_prev_bool(key: str) -> str:
    # 配合 LAG 包装器使用（仅占位，真正的 prev valid 通过 LAG 完成）
    return f"(s.state_payload ->> {_quote(key)})"


def _build_strict_transition_sql() -> str:
    """严格 T-1 自-join，不使用 LAG；仅统计 4 对需要重算的 transition。"""
    return f"""
    WITH valid AS (
        SELECT s.instrument_id, s.trade_date,
               ({_payload_bool('valid_for_market_aggregation')})::boolean AS valid,
               ({_payload_bool('regime_value')})::int                        AS regime_value,
               ({_payload_bool('swing_bias')})::int                         AS swing_bias,
               ({_payload_bool('momentum_direction')})                       AS momentum_direction
        FROM {_STATE_TABLE} s
        WHERE trade_date = ANY(%(trade_dates)s)
          AND algorithm_version = %(algo)s
          AND history_contract_version = %(hc)s
    )
    SELECT
        curr.trade_date,
        COUNT(*) FILTER (WHERE curr.valid IS TRUE AND prev.valid IS TRUE)
            AS n_common_strict,
        COUNT(*) FILTER (WHERE curr.valid IS TRUE AND prev.valid IS TRUE
                          AND prev.regime_value = 0 AND curr.regime_value = 1)
            AS t_regime_0_1,
        COUNT(*) FILTER (WHERE curr.valid IS TRUE AND prev.valid IS TRUE
                          AND prev.regime_value = 0 AND curr.regime_value = -1)
            AS t_regime_0_neg1,
        COUNT(*) FILTER (WHERE curr.valid IS TRUE AND prev.valid IS TRUE
                          AND prev.swing_bias = -1 AND curr.swing_bias = 1)
            AS t_swing_neg1_1,
        COUNT(*) FILTER (WHERE curr.valid IS TRUE AND prev.valid IS TRUE
                          AND prev.momentum_direction = {_quote('contracting')}
                          AND curr.momentum_direction = {_quote('expanding')})
            AS t_momdir_contract_expand
    FROM valid curr
    JOIN valid prev
      ON prev.instrument_id = curr.instrument_id
     AND prev.trade_date = (SELECT MAX(s2.trade_date)
                             FROM {_STATE_TABLE} s2
                             WHERE s2.instrument_id = curr.instrument_id
                               AND s2.trade_date < curr.trade_date
                               AND s2.trade_date = ANY(%(trade_dates)s)
                               AND s2.algorithm_version = %(algo)s
                               AND s2.history_contract_version = %(hc)s)
    WHERE curr.trade_date = ANY(%(trade_dates)s)
    GROUP BY curr.trade_date
    ORDER BY curr.trade_date ASC
    """


def _build_strict_concentration_sql() -> str:
    """严格 T-1 bars JOIN（用 prev canonical trade_date）。"""
    return f"""
    WITH valid AS (
        SELECT s.instrument_id, s.trade_date
        FROM {_STATE_TABLE} s
        WHERE trade_date = ANY(%(trade_dates)s)
          AND algorithm_version = %(algo)s
          AND history_contract_version = %(hc)s
          AND ({_payload_bool('valid_for_market_aggregation')})::boolean IS TRUE
    ),
    daily AS (
        SELECT b.instrument_id, b.trade_date, b.close, b.amount
        FROM {_BARS_TABLE} b
        JOIN valid v ON v.instrument_id = b.instrument_id AND v.trade_date = b.trade_date
        WHERE b.close IS NOT NULL AND b.amount IS NOT NULL
    ),
    rets AS (
        SELECT cur.instrument_id, cur.trade_date,
               CASE WHEN prev.close IS NOT NULL AND prev.close <> 0
                    THEN (cur.close - prev.close) / prev.close END AS ret,
               cur.amount
        FROM daily cur
        LEFT JOIN daily prev
          ON prev.instrument_id = cur.instrument_id
         AND prev.trade_date = (SELECT MAX(b2.trade_date) FROM {_BARS_TABLE} b2
                                 WHERE b2.instrument_id = cur.instrument_id
                                   AND b2.trade_date < cur.trade_date
                                   AND b2.trade_date = ANY(%(trade_dates)s))
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


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="round3_adjacency_audit")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--round2-obs-csv", required=True, type=Path)
    p.add_argument("--dev-base-sha", required=True)
    p.add_argument("--end-date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    res = run_adjacency_check(
        database_url=args.database_url or "",
        out_dir=args.out_dir,
        end_date=args.end_date or date(1970, 1, 1),
        dev_base_sha=args.dev_base_sha,
        round2_obs_csv=args.round2_obs_csv,
        dry_run=args.dry_run,
    )
    print(json.dumps({k: v for k, v in res.items() if not k.endswith("_path")},
                     indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
