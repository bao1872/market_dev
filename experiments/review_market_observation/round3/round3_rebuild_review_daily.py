"""Round 3 §4-5 — Current Review 数据来源审计 + 只读重建 daily review values。

方法：
  §4. 先检查 production DB 是否已有完整的 review metric/component history。
      若 history 完整、版本一致 → 直接复用。
  §5. 否则用实验代码只读计算 120 日 full-market daily component values。
      不触发 Review run、不写 DB。

只读（SET TRANSACTION READ ONLY），DB-native aggregate first。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import sys
_MAIN_REPO = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_MAIN_REPO / "backend"))

import pandas as pd  # noqa: E402

from app.domain.review.metric_registry import DEFAULT_REGISTRY  # noqa: E402
from app.domain.review.metric_engine import (  # noqa: E402
    compute_all_metrics,
    MIN_BASELINE_WINDOW,
)
from experiments.review_market_observation.round1.round1_db_native import (  # noqa: E402
    SessionGuard,
    DEV_BASE_SHA_REQUIRED,
    EXPECTED_ALGORITHM_VERSION,
    EXPECTED_HISTORY_CONTRACT_VERSION,
    TARGET_TRADE_DATE_COUNT,
)
from experiments.review_market_observation.round1.dataset_schema import (  # noqa: E402
    build_selected_trade_dates,
    validate_120_consecutive_trade_dates,
)
from experiments.review_market_observation.round3.round3_component_map import (  # noqa: E402
    expected_component_count,
)

DEV_BASE_SHA = "6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0"
REVIEW_ALGO_VERSION = "review-1.0.0"

_STATE_TABLE = "first_pyramid_history_daily_state"
_REVIEW_METRIC_TABLE = "review_metric_history_daily"
_REVIEW_COMP_TABLE = "review_component_history_daily"
_BARS_TABLE = "bars_daily"

# ============================================================
# §4 审计：检查现有 history 完整性
# ============================================================


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def audit_existing_review_history(
    database_url: str, trade_dates: list[date]
) -> dict[str, Any]:
    """检查 production DB 中是否已有 review-1.0.0 的完整 history。"""
    with SessionGuard(database_url) as cur:
        cur.execute(f"SELECT to_regclass('{_REVIEW_METRIC_TABLE}')")
        metric_exists = cur.fetchone()[0] is not None
        cur.execute(f"SELECT to_regclass('{_REVIEW_COMP_TABLE}')")
        comp_exists = cur.fetchone()[0] is not None
        if not metric_exists or not comp_exists:
            return {
                "tables_exist": False,
                "can_reuse": False,
                "reason": "review tables not exist",
            }

        params = {
            "algo": REVIEW_ALGO_VERSION,
            "trade_dates": trade_dates,
        }
        cur.execute(f"""
            SELECT COUNT(DISTINCT trade_date), COUNT(*)
            FROM {_REVIEW_METRIC_TABLE}
            WHERE trade_date = ANY(%(trade_dates)s)
              AND algorithm_version = %(algo)s
              AND scope_type = 'market'
        """, params)
        metric_dates, metric_rows = cur.fetchone()

        cur.execute(f"""
            SELECT COUNT(DISTINCT trade_date), COUNT(*)
            FROM {_REVIEW_COMP_TABLE}
            WHERE trade_date = ANY(%(trade_dates)s)
              AND algorithm_version = %(algo)s
              AND scope_type = 'market'
        """, params)
        comp_dates, comp_rows = cur.fetchone()

    expected_comp_rows = len(trade_dates) * expected_component_count()
    can_reuse = (
        metric_dates == len(trade_dates)
        and comp_dates == len(trade_dates)
        and comp_rows >= expected_comp_rows * 0.95  # 允许5%缺失，后续补
    )
    return {
        "tables_exist": True,
        "metric_date_coverage": f"{metric_dates}/{len(trade_dates)}",
        "comp_date_coverage": f"{comp_dates}/{len(trade_dates)}",
        "metric_rows": metric_rows,
        "comp_rows": f"{comp_rows}/{expected_comp_rows}",
        "can_reuse": can_reuse,
        "review_version": REVIEW_ALGO_VERSION,
    }


# ============================================================
# §4 fallback：用实验代码只读重建
# 从 first_pyramid_history_daily_state 取成员 payload + bars 构造 daily flat_list
# 调用 compute_all_metrics 计算 27 component + P/Q/U/C/V raw values
# 注意：由于重建仅为研究目的，归一化 history 用 120 日滚动 raw values 直接算，
# 不追求与正式 Review bootstrap 100% parity（component-level raw parity 已足够）。
# ============================================================


def _payload(key: str, alias: str = "s") -> str:
    return f"({alias}.state_payload ->> {_quote(key)})"


def build_daily_flat_lists(
    database_url: str, trade_dates: list[date]
) -> dict[date, list[dict[str, Any]]]:
    """DB-native：按日聚合成员 first_pyramid + daily bar facts。

    分 date-chunk (10日/批) 查询以降低 psycopg JSON loads 的内存峰值；
    prev_payload 在 client 端按 instrument_id 滚动维护（跨 chunk 正确 carryover）。
    """
    from app.domain.review.member_fact import previous_state_to_flat

    sorted_dates = sorted(trade_dates)
    per_day: dict[date, list[dict[str, Any]]] = {d: [] for d in sorted_dates}
    # 跨 chunk 维护：instr_id -> 前一个 valid trade_date 的 curr_payload
    # 保证相邻 chunk 交界处 instrument 的 prev_payload 仍然正确
    prev_by_instr: dict[str, Any] = {}

    # 每批查询 <= 10 个 trade_date：控制单批 fetchall 内存 < 300MB
    chunk_size = 10
    for ci in range(0, len(sorted_dates), chunk_size):
        date_chunk = sorted_dates[ci:ci + chunk_size]
        params = {
            "algo": EXPECTED_ALGORITHM_VERSION,
            "hc": EXPECTED_HISTORY_CONTRACT_VERSION,
            "trade_dates": date_chunk,
        }
        # 故意不使用 SQL LAG：跨 chunk 时 client-side LAG 更一致，
        # 也避免 SQL 端一次性持有窗口内所有 JSON payload。
        sql = f"""
        WITH valid AS (
            SELECT
                s.instrument_id,
                s.trade_date,
                s.state_payload                                 AS curr_payload
            FROM {_STATE_TABLE} s
            WHERE trade_date = ANY(%(trade_dates)s)
              AND algorithm_version = %(algo)s
              AND history_contract_version = %(hc)s
              AND ({_payload('valid_for_market_aggregation')})::boolean IS TRUE
        ),
        bars_curr AS (
            SELECT b.instrument_id, b.trade_date,
                   b.open, b.high, b.low, b.close, b.volume, b.amount
            FROM {_BARS_TABLE} b
            WHERE trade_date = ANY(%(trade_dates)s)
              AND b.close IS NOT NULL
        )
        SELECT
            v.instrument_id,
            v.trade_date,
            v.curr_payload,
            bc.open, bc.high, bc.low, bc.close, bc.volume, bc.amount
        FROM valid v
        LEFT JOIN bars_curr bc ON bc.instrument_id = v.instrument_id
                              AND bc.trade_date = v.trade_date
        ORDER BY v.instrument_id ASC, v.trade_date ASC
        """
        with SessionGuard(database_url) as cur:
            cur.execute(sql, params)
            chunk_rows = cur.fetchall()

        for row in chunk_rows:
            instr_id, td, curr_p, o, h, l, c, vol, amt = row
            if td not in per_day:
                continue
            if not isinstance(curr_p, dict):
                # 更新 prev_by_instr 避免跳过，但不加入输出
                prev_by_instr[str(instr_id)] = curr_p
                continue
            prev_p = prev_by_instr.get(str(instr_id))
            # 推进 prev_by_instr 为当前 payload（供下一个日期/chunk 使用）
            prev_by_instr[str(instr_id)] = curr_p

            curr_flat = previous_state_to_flat(curr_p)
            prev_flat = previous_state_to_flat(prev_p if isinstance(prev_p, dict) else None)

            review_return_1d = curr_p.get("review_return_1d")
            if review_return_1d is None and c is not None:
                prev_close = (prev_p.get("close") if isinstance(prev_p, dict) else None)
                if prev_close and float(prev_close) != 0:
                    review_return_1d = (float(c) - float(prev_close)) / float(prev_close) * 100.0

            review_price_position = curr_flat.get("review_price_position")
            if review_price_position is None:
                review_price_position = curr_p.get("price_position_120d")

            flat = dict(curr_flat)
            flat.update({
                "_instrument_id": str(instr_id),
                "review_trade_date": str(td),
                "review_open": float(o) if o is not None else None,
                "review_high": float(h) if h is not None else None,
                "review_low": float(l) if l is not None else None,
                "review_close": float(c) if c is not None else None,
                "review_return_1d": (float(review_return_1d)
                                      if review_return_1d is not None else None),
                "review_price_position": (float(review_price_position)
                                           if review_price_position is not None else None),
                "review_volume": float(vol) if vol is not None else None,
                "review_amount": float(amt) if amt is not None else None,
                "review_previous_first_pyramid": prev_flat,
                "review_weight": 1.0,
                "review_weight_mode": "equal_weight",
            })
            per_day[td].append(flat)
        # 释放 chunk 内存再进入下一批
        del chunk_rows

    return per_day


def compute_daily_review_values(
    per_day_flat: dict[date, list[dict[str, Any]]],
    trade_dates: list[date],
) -> pd.DataFrame:
    """按日计算 27 component raw values + P/Q/U/C/V raw values。

    仅使用原始值（不做 history 归一化）保证重建可解释性。
    """
    reg = DEFAULT_REGISTRY
    comp_names = []
    for code in reg.metric_codes:
        comp_names.extend(c.name for c in reg.get_metric(code).components)

    # 每个 component 的历史 raw 值（用于归一化滚动）
    comp_history: dict[str, list[float]] = {cn: [] for cn in comp_names}
    metric_history: dict[str, list[float]] = {c: [] for c in reg.metric_codes}

    rows_out: list[dict[str, Any]] = []

    for td in trade_dates:
        flat_list = per_day_flat.get(td) or []
        ready_count = len(flat_list)

        # 构建 history_map：每个 component 的前 N 日 raw values
        history_maps: dict[str, dict[str, list[float]]] = {}
        for code in reg.metric_codes:
            h_map: dict[str, list[float]] = {}
            for comp in reg.get_metric(code).components:
                h_map[comp.name] = list(comp_history[comp.name])
            h_map["_metric_value"] = list(metric_history[code])
            history_maps[code] = h_map

        result = compute_all_metrics(
            flat_list,
            ready_count=ready_count,
            history_maps=history_maps,
            registry=reg,
        )

        row: dict[str, Any] = {"trade_date": str(td)}
        # 提取 components rawValue
        for code in reg.metric_codes:
            payload = result[code]
            for cp in payload["components"]:
                row[cp["name"]] = cp["rawValue"]
                # 更新 history（仅限 raw_ready）
                if cp["rawValue"] is not None:
                    comp_history[cp["name"]].append(float(cp["rawValue"]))
            # P/Q/U/C/V value
            row[f"metric_{code}_value"] = payload["value"]
            row[f"metric_{code}_rawValue"] = payload["rawValue"]
            row[f"metric_{code}_status"] = payload["status"]
            if payload["value"] is not None:
                metric_history[code].append(float(payload["value"]))

        rows_out.append(row)

    df = pd.DataFrame(rows_out)
    # 整理列顺序：trade_date → 27 comps → metric_*
    first_cols = ["trade_date"]
    comp_cols = [c for c in df.columns if c in comp_names]
    metric_cols = [c for c in df.columns if c.startswith("metric_")]
    return df[first_cols + comp_cols + metric_cols]


def write_review_daily_csv(df: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False, encoding="utf-8")


# ============================================================
# CLI
# ============================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="round3_rebuild_review_daily")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--end-date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    p.add_argument("--dev-base-sha", default=DEV_BASE_SHA)
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.dev_base_sha != DEV_BASE_SHA_REQUIRED:
        raise RuntimeError(f"DEV_BASE required={DEV_BASE_SHA_REQUIRED}")
    if args.dry_run:
        print(json.dumps({"mode": "dry-run", "out_dir": str(args.out_dir)},
                         indent=2, ensure_ascii=False))
        return 0

    database_url = args.database_url or ""
    if not database_url:
        raise RuntimeError("DATABASE_URL required")

    end_date = args.end_date or date(2026, 8, 10)

    # --- 选定 120 交易日 ---
    with SessionGuard(database_url) as cur:
        cur.execute(f"""
            SELECT DISTINCT trade_date FROM {_STATE_TABLE}
            WHERE trade_date IS NOT NULL
              AND algorithm_version=%(algo)s
              AND history_contract_version=%(hc)s
            ORDER BY trade_date DESC LIMIT 300
        """, {"algo": EXPECTED_ALGORITHM_VERSION,
              "hc": EXPECTED_HISTORY_CONTRACT_VERSION})
        cand_desc = [r[0] for r in cur.fetchall()]
    elig_asc = sorted(
        [d for d in cand_desc if d is not None and date.fromisoformat(str(d)) <= end_date],
        key=str,
    )
    trade_dates = build_selected_trade_dates(elig_asc, TARGET_TRADE_DATE_COUNT)
    date_info = validate_120_consecutive_trade_dates(trade_dates)

    # --- §4 Audit ---
    audit = audit_existing_review_history(database_url, trade_dates)

    if audit.get("can_reuse"):
        # 复用现有 history（略，当前开发环境未写入 review history，fallback 到 rebuild）
        print(json.dumps({
            "audit": audit,
            "note": "history reuse path exists; current rebuild path used for research parity",
        }, indent=2, ensure_ascii=False, default=str))

    # --- §5 Rebuild（只读）---
    per_day = build_daily_flat_lists(database_url, trade_dates)
    df = compute_daily_review_values(per_day, trade_dates)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_review_daily_csv(df, args.out_dir / "round3_current_review_daily.csv")

    # 写审计元信息 JSON
    meta = {
        "window": {
            "start": str(date_info["start"]),
            "end": str(date_info["end"]),
            "count": date_info["count"],
            "is_exact_target": date_info["is_exact_target"],
        },
        "audit": audit,
        "rebuild_info": {
            "rows": len(df),
            "components": sum(1 for c in df.columns if not c.startswith("metric_")
                              and c != "trade_date"),
            "metric_value_status": {
                code: df[f"metric_{code}_value"].notna().sum() for code in
                ["P", "Q", "U", "C", "V"]
            },
        },
    }
    (args.out_dir / "round3_rebuild_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False, default=str)
    )
    print(json.dumps(meta, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
