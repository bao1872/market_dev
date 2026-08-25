"""Round 1 — DB-native / query-on-demand pipeline（prompt.md 新版架构）。

旧模式（停用，不再进入主执行路径）：
  - 一次性 fetchall 载入 69 列 × 120 日 × ~5k 股票的 full raw rows
  - pandas DataFrame merge bars
  - 写 frozen_dataset.parquet（full dataset hash）

新模式（本轮默认主路径）：
  - 每次 query 仅 SELECT 当前分析需要的列，优先在 PostgreSQL 聚合
  - 返回小型 aggregate 结果（最多 ~120 行 daily summary / 3×3 transition / top-20 sample）
  - 每步独立 read-only transaction；完成即 close + 释放对象
  - 不生成 parquet / 不复用 bars_daily（除非 prompt §14 明确需要）

固定的实验边界：
  - CANONICAL_VERSIONS:
      algorithm_version      = "1.0.0-core-split"
      history_contract_version = "review-history-v2"
  - TARGET_WINDOW: 最近 120 个 canonical 交易日（显式 END_DATE，fail-closed）
  - UNIVERSE: 当日 state_payload 行实际出现过的所有 instrument（不通过 instruments.status 过滤）
  - ONLY_READ: 永远 SET TRANSACTION READ ONLY + 断言 on

本文件输出的小型 JSON：
  round1_lineage.json（§8）
  integrity_summary.json（§9）
  coverage_missingness.json（§10）
  categorical_primitives.json（§11）
  continuous_primitives.json（§12）
  transition_summary.json（§13）
  ROUND1_SUMMARY.md（§14 final summary）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any, Iterable

# Canonical pin（单一事实源）
EXPECTED_ALGORITHM_VERSION = "1.0.0-core-split"
EXPECTED_HISTORY_CONTRACT_VERSION = "review-history-v2"
TARGET_TRADE_DATE_COUNT = 120
DEV_BASE_SHA_REQUIRED = "6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0"

# 真实 DB schema：first_pyramid_history_daily_state 仅含以下 10 列；
# payload / readiness / trend / structure / momentum / volume 全部从 state_payload JSONB 读取
_STATE_TABLE = "first_pyramid_history_daily_state"
_BARS_TABLE = "bars_daily"
_INSTRUMENTS_TABLE = "instruments"

# 分类字段（§11 / §13）：payload key → SQL expression
CATEGORICAL_STATE_FIELDS: tuple[str, ...] = (
    "regime_value",
    "swing_bias",
    "internal_bias",
    "structure_alignment",
    "volatility_phase",
    "momentum_direction",
    "momentum_change",
)

# 连续字段（§12）：payload key → SQL numeric cast
CONTINUOUS_STATE_FIELDS: tuple[str, ...] = (
    "regime_strength",
    "dsa_dir_bars",
    "dsa_vwap_dev_pct",
    "sqzmom_val",
    "sqzmom_delta",
    "volume_ratio_20",
    "review_volume_ratio20",
    "review_amount_ratio20",
    "review_volume_percentile20",
    "review_amount_percentile200",
    "price_position_120d",
)


# ============================================================================
# DSN runtime resolution（复用上轮已修复的 scheme / host override 纯函数）
# ============================================================================

_LIBPQ_SCHEME_ALIASES = {
    "postgresql+psycopg": "postgresql",
    "postgresql+psycopg2": "postgresql",
    "postgres": "postgresql",
}


def normalize_libpq_dsn(dsn: str) -> str:
    if not dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    norm = _LIBPQ_SCHEME_ALIASES.get(scheme, scheme)
    return dsn if norm == scheme else f"{norm}://{rest}"


def apply_dsn_host_override(dsn: str, *, from_host: str | None, to_host: str | None) -> str:
    if not dsn or not from_host or not to_host or "://" not in dsn:
        return dsn
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(dsn)
    except Exception:
        return dsn
    if parts.hostname != from_host:
        return dsn
    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username + (f":{parts.password}" if parts.password else "") + "@"
    port_suffix = f":{parts.port}" if parts.port else ""
    new_netloc = f"{userinfo}{to_host}{port_suffix}"
    return urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))


def resolve_runtime_dsn(dsn: str) -> str:
    dsn = normalize_libpq_dsn(dsn)
    host_from = os.environ.get("DSN_HOST_FROM")
    host_to = os.environ.get("DSN_HOST_TO")
    if host_from and host_to:
        dsn = apply_dsn_host_override(dsn, from_host=host_from, to_host=host_to)
    return dsn


# ============================================================================
# DB Session contract（§4）: unified read-only transaction helper
# ============================================================================

# PostgreSQL 允许通过 SET LOCAL 设置本地 transaction 参数；
# 如果当前 PG 版本不支持某个参数，代码会记录 SETTING_UNAVAILABLE 并继续，不静默绕过。
SESSION_STATEMENTS_TEMPLATE: tuple[tuple[str, str], ...] = (
    ("transaction_read_only", "SHOW transaction_read_only"),
    ("work_mem",               "SET LOCAL work_mem = '64MB'"),
    ("statement_timeout",      "SET LOCAL statement_timeout = '300s'"),
    ("lock_timeout",           "SET LOCAL lock_timeout = '5s'"),
    ("idle_in_transaction_session_timeout", "SET LOCAL idle_in_transaction_session_timeout = '60s'"),
    ("max_parallel_workers_per_gather",     "SET LOCAL max_parallel_workers_per_gather = 0"),
)


@dataclass
class SessionGuard:
    """每次 read-only query 的 context manager（fail-closed）。

    强制行为：
      1) psycopg2.connect(resolve_runtime_dsn(database_url))
      2) SET TRANSACTION READ ONLY
      3) SHOW transaction_read_only — 必须 = 'on'（否则 raise ReadOnlyAssertionError）
      4) 逐条 SET LOCAL <param>；任何一条失败 → 直接 raise / STOP
         （不 rollback 再继续、不维护 unavailable 状态、不兼容未知 PG 环境）
      5) yield cursor
      6) with-block 结束 → 事务 ROLLBACK（read-only 无需 COMMIT；连接关闭自动释放）
    """

    database_url: str
    verbose: bool = False

    def __post_init__(self):
        # 延迟 import psycopg2，pure unit 不需要安装
        import psycopg2  # noqa: F401
        self._conn = None
        self._cur = None
        self.settings_applied: dict[str, Any] = {}

    def __enter__(self):
        import psycopg2
        conn = psycopg2.connect(resolve_runtime_dsn(self.database_url))
        self._conn = conn
        self._cur = conn.cursor()
        cur = self._cur
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SHOW transaction_read_only")
        val = cur.fetchone()[0]
        if val != "on":
            raise ReadOnlyAssertionError(
                f"transaction_read_only expected='on' got={val!r}; STOP."
            )
        self.settings_applied["transaction_read_only"] = "on"
        # 其余 session settings 逐条执行；任何一条失败直接 fail-closed raise（不 continue）。
        for key, stmt in SESSION_STATEMENTS_TEMPLATE:
            if key == "transaction_read_only":
                continue
            try:
                cur.execute(stmt)
            except Exception as e:  # noqa: BLE001
                raise ResourceSettingError(
                    f"resource setting failed: {key}: {type(e).__name__}: {e}; STOP."
                ) from e
            self.settings_applied[key] = "SET_LOCAL_APPLIED"
        return cur

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._conn is not None:
                # read-only: rollback is safe + free locks/temp buffers
                self._conn.rollback()
        except Exception:
            pass
        try:
            if self._cur is not None:
                self._cur.close()
        except Exception:
            pass
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._cur = None
        self._conn = None
        return False


class ReadOnlyAssertionError(RuntimeError):
    pass


class ResourceSettingError(RuntimeError):
    pass


class ExpectationMismatch(RuntimeError):
    pass


# ============================================================================
# Git HEAD resolution（§8 lineage：EXP_SHA 必须等于工作树 git rev-parse HEAD）
# ============================================================================

def resolve_git_head_sha(search_from: Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(search_from), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out if len(out) == 40 else None


# ============================================================================
# Query builders（prompt §8 – §13；纯函数，无副作用，便于 unit test query shape）
# ============================================================================

# §8 Step 0 — candidate trade dates（algo + hc 过滤 + 最近 300，供 Python 选最近 120）
SQL_STEP0_CANDIDATE_TRADE_DATES = f"""
SELECT DISTINCT trade_date
FROM {_STATE_TABLE}
WHERE trade_date IS NOT NULL
  AND algorithm_version = %(algo)s
  AND history_contract_version = %(hc)s
ORDER BY trade_date DESC
LIMIT 300
"""

# §8 Step 0bis — lineage counts / min/max updated_at / source_history_run_count
# 所有 COUNT / MIN / MAX 都在 DB 完成，不返回 raw rows。
SQL_STEP0_LINEAGE_COUNTS = f"""
SELECT
    COUNT(*)                                                    AS row_count,
    COUNT(DISTINCT instrument_id)                               AS distinct_instrument_count,
    MIN(updated_at)                                             AS min_updated_at,
    MAX(updated_at)                                             AS max_updated_at,
    COUNT(DISTINCT source_history_run_id)                       AS source_history_run_count
FROM {_STATE_TABLE}
WHERE trade_date = ANY(%(trade_dates)s)
  AND algorithm_version = %(algo)s
  AND history_contract_version = %(hc)s
"""

# §9 Step 1 — Integrity audit aggregates（只返回单行 summary + 20 duplicate sample 上限）
SQL_STEP1_ROW_SUMMARY = f"""
SELECT
    COUNT(*)                                          AS row_count,
    COUNT(DISTINCT trade_date)                        AS trade_date_count,
    COUNT(DISTINCT instrument_id)                     AS distinct_instrument_count,
    COUNT(*) FILTER (WHERE algorithm_version = %(algo)s) AS algo_match_count,
    COUNT(*) FILTER (WHERE history_contract_version = %(hc)s)  AS hc_outer_match_count,
    COUNT(*) FILTER (
        WHERE (state_payload ->> 'history_contract_version') = %(hc)s
    )                                                 AS hc_payload_match_count
FROM {_STATE_TABLE}
WHERE trade_date = ANY(%(trade_dates)s)
  AND algorithm_version = %(algo)s
  AND history_contract_version = %(hc)s
"""

SQL_STEP1_DUPLICATE_COUNT = f"""
SELECT COUNT(*)
FROM (
    SELECT instrument_id, trade_date, COUNT(*) AS n
    FROM {_STATE_TABLE}
    WHERE trade_date = ANY(%(trade_dates)s)
      AND algorithm_version = %(algo)s
      AND history_contract_version = %(hc)s
    GROUP BY instrument_id, trade_date
    HAVING COUNT(*) > 1
) dup
"""

SQL_STEP1_DUPLICATE_SAMPLE = f"""
SELECT instrument_id, trade_date, COUNT(*) AS n
FROM {_STATE_TABLE}
WHERE trade_date = ANY(%(trade_dates)s)
  AND algorithm_version = %(algo)s
  AND history_contract_version = %(hc)s
GROUP BY instrument_id, trade_date
HAVING COUNT(*) > 1
ORDER BY n DESC, trade_date DESC
LIMIT 20
"""

SQL_STEP1_SOURCE_HISTOGRAM = f"""
SELECT
    COALESCE(source_history_run_id::text, 'UNAVAILABLE') AS source_history_run_id,
    COUNT(*) AS n
FROM {_STATE_TABLE}
WHERE trade_date = ANY(%(trade_dates)s)
  AND algorithm_version = %(algo)s
  AND history_contract_version = %(hc)s
GROUP BY 1
ORDER BY n DESC
LIMIT 50
"""


# §10 Step 2 — Coverage / Missingness（每日 GROUP BY 最多 120 行 + per-field missing summary）
READINESS_FIELD_KEYS: tuple[str, ...] = (
    "history_sufficient",
    "core_factor_ready",
    "valid_for_market_aggregation",
    "invalid_reason",
)


def _payload_bool(key: str) -> str:
    return f"(s.state_payload ->> {_quote(key)})::boolean"


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def build_sql_step2_daily_coverage() -> str:
    # 每天一行：denom + readiness counts + 69 列 null 比例代价过高 → 只统计 prompt §10 列出的
    # history_sufficient / core_factor_ready / valid_for_market_aggregation / invalid_reason
    # 再加上 payload 中核心 primitive 非空计数（6 个核心原子：regime_value / swing_bias /
    # regime_strength / sqzmom_val / volume_ratio_20 / price_position_120d）。
    col_selects = [
        "COUNT(*)                                                         AS row_count",
        "COUNT(DISTINCT instrument_id)                                   AS distinct_instrument_count",
    ]
    for k in READINESS_FIELD_KEYS:
        if k == "invalid_reason":
            col_selects.append(
                f"COUNT(*) FILTER (WHERE s.state_payload ->> {_quote(k)} IS NULL) "
                f"AS invalid_reason_null_count"
            )
            col_selects.append(
                f"COUNT(*) FILTER (WHERE s.state_payload ->> {_quote(k)} IS NOT NULL) "
                f"AS invalid_reason_nonnull_count"
            )
        else:
            # boolean readiness: count TRUE
            col_selects.append(
                f"COUNT(*) FILTER (WHERE {_payload_bool(k)} IS TRUE) "
                f"AS {k}_true_count"
            )
    # 核心 primitive 非空率
    CORE_PRIM = ("regime_value", "swing_bias", "regime_strength",
                 "sqzmom_val", "volume_ratio_20", "price_position_120d")
    for k in CORE_PRIM:
        col_selects.append(
            f"COUNT(*) FILTER (WHERE (s.state_payload ->> {_quote(k)}) IS NOT NULL) "
            f"AS {k}_nonnull_count"
        )
    cols_sql = ",\n    ".join(col_selects)
    return f"""
SELECT
    trade_date,
    {cols_sql}
FROM {_STATE_TABLE} s
WHERE trade_date = ANY(%(trade_dates)s)
  AND algorithm_version = %(algo)s
  AND history_contract_version = %(hc)s
GROUP BY trade_date
ORDER BY trade_date ASC
"""


SQL_STEP2_DAILY_COVERAGE: str = build_sql_step2_daily_coverage()


# §11 Step 3 — Categorical primitive（per-field aggregate，每字段单独执行一次）
def build_sql_step3_categorical(field_name: str) -> str:
    # 返回 trade_date × value × count — 最多 120 × (enum values) 行
    if field_name not in CATEGORICAL_STATE_FIELDS:
        raise ValueError(f"not a categorical field: {field_name}")
    return f"""
SELECT
    trade_date,
    (s.state_payload ->> {_quote(field_name)})          AS value,
    COUNT(*)                                            AS n
FROM {_STATE_TABLE} s
WHERE trade_date = ANY(%(trade_dates)s)
  AND algorithm_version = %(algo)s
  AND history_contract_version = %(hc)s
GROUP BY trade_date, (s.state_payload ->> {_quote(field_name)})
ORDER BY trade_date ASC, n DESC
"""


# §12 Step 4 — Continuous primitive（per-field 汇总统计；percentile_cont 可选 defer）
def build_sql_step4_continuous(field_name: str, *, include_percentile: bool = True) -> tuple[str, bool]:
    if field_name not in CONTINUOUS_STATE_FIELDS:
        raise ValueError(f"not a continuous field: {field_name}")
    val_cast = f"((s.state_payload ->> {_quote(field_name)})::numeric)"
    parts = [
        f"COUNT({val_cast})                                            AS n_nonnull",
        f"COUNT(*) - COUNT({val_cast})                                  AS n_null",
        f"AVG({val_cast})                                               AS mean",
        f"MIN({val_cast})                                               AS min_",
        f"MAX({val_cast})                                               AS max_",
    ]
    percentile_deferred = False
    if include_percentile:
        parts += [
            f"percentile_cont(0.05) WITHIN GROUP (ORDER BY {val_cast})   AS p05",
            f"percentile_cont(0.50) WITHIN GROUP (ORDER BY {val_cast})   AS p50",
            f"percentile_cont(0.95) WITHIN GROUP (ORDER BY {val_cast})   AS p95",
        ]
    cols = ",\n    ".join(parts)
    sql = f"""
SELECT
    trade_date,
    {cols}
FROM {_STATE_TABLE} s
WHERE trade_date = ANY(%(trade_dates)s)
  AND algorithm_version = %(algo)s
  AND history_contract_version = %(hc)s
GROUP BY trade_date
ORDER BY trade_date ASC
"""
    return sql, percentile_deferred


# §13 Step 5 — Transition audit（PostgreSQL LAG + PARTITION BY instrument_id ORDER BY trade_date）
def build_sql_step5_transition(field_name: str) -> str:
    if field_name not in CATEGORICAL_STATE_FIELDS:
        raise ValueError(f"not a categorical field for transition audit: {field_name}")
    val = f"(state_payload ->> {_quote(field_name)})"
    return f"""
WITH per_instr AS (
    SELECT
        instrument_id,
        trade_date,
        {val}                                                    AS current_state,
        LAG({val}) OVER (
            PARTITION BY instrument_id ORDER BY trade_date ASC
        )                                                         AS prev_state
    FROM {_STATE_TABLE}
    WHERE trade_date = ANY(%(trade_dates)s)
      AND algorithm_version = %(algo)s
      AND history_contract_version = %(hc)s
)
SELECT
    prev_state,
    current_state,
    COUNT(*)                                                     AS n,
    COUNT(DISTINCT instrument_id)                                AS n_instruments
FROM per_instr
WHERE prev_state IS NOT NULL
GROUP BY prev_state, current_state
ORDER BY n DESC
LIMIT 500
"""


# ============================================================================
# Query shape assertions（pure unit §19 调用；不需要 psycopg2 / DB）
# ============================================================================

def query_contains_select_star(sql: str) -> bool:
    """§19.B — categorical/continuous 不应有 SELECT *。"""
    import re
    # 简单匹配：SELECT * 或 SELECT DISTINCT *
    return bool(re.search(r"SELECT\s+(DISTINCT\s+)?\*", sql, re.IGNORECASE))


def query_has_no_fetchall_raw_120day(sql: str) -> bool:
    """§19.E — 禁止 SELECT 69 列 的 raw 拉取。

    启发式：非分类查询不允许出现 state_payload.* 展开（我们只取 key）。
    本函数只用于防止旧模式回滚。
    """
    banned = [
        "frozen_dataset.parquet",
        "fetchall()",  # 这其实是 Python 层，但 query 字符串里不应该出现，留作占位；
        # 真正 raw guard：SELECT 不包含所有 DB 列 + payload 整体返回（如 “s.*”）
        "SELECT s.*",
    ]
    sql_lower = sql.lower()
    for b in banned:
        if b.lower() in sql_lower:
            return False
    return True


def query_uses_transition_lag(sql: str) -> bool:
    """§19.D — transition query 必须包含 LAG + PARTITION BY instrument_id + ORDER BY trade_date。"""
    s = sql.upper()
    return (
        "LAG(" in s
        and "PARTITION BY INSTRUMENT_ID" in s
        and "ORDER BY TRADE_DATE" in s
    )


def query_is_aggregate_only(sql: str) -> bool:
    """§19.A — 不包含 SELECT * / 没有直接返回 s.state_payload 整列 raw。"""
    if query_contains_select_star(sql):
        return False
    # 禁止 "s.state_payload" 作为输出列（只允许 state_payload ->> 'key' 形式）
    stripped = sql.replace("state_payload ->>", "")
    if "state_payload" in stripped and "--" not in stripped[: stripped.find("state_payload")+14]:
        # 允许 UPDATE-less：仅当存在 "FROM first_pyramid_history_daily_state s" 之后 state_payload 在 filter 中
        # 我们的 query 总是通过 state_payload ->> 访问，所以 stripped 中如果还出现 state_payload 则违规。
        return False
    return True


# ============================================================================
# 主 orchestrator（Round 1 的 6 步；每步独立事务 → 完成后立刻释放）
# ============================================================================

def _repo_root_from_file() -> Path:
    here = Path(__file__).resolve()
    # parents[0] = round1, parents[1] = review_market_observation,
    # parents[2] = experiments, parents[3] = worktree root (<repo>-exp-review-observation)
    return here.parents[3]


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, sort_keys=True, default=str, ensure_ascii=False)
    )


def run_round1_db_native(
    *,
    database_url: str,
    out_dir: Path,
    end_date: date,
    dev_base_sha: str,
    expected_exp_sha: str,
    dry_run: bool = False,
) -> dict:
    """§8 – §14 的主驱动（默认 DB-native，不写 parquet/DataFrame）。

    dry-run=True 时：只校验参数 + lineage / query SQL shape，不连 DB。
    """
    # §8 lineage 校验（fail-closed）
    repo_root = _repo_root_from_file()
    resolved_exp_sha = resolve_git_head_sha(repo_root)
    if dry_run:
        return {
            "mode": "dry-run",
            "repo_root": str(repo_root),
            "resolved_exp_sha": resolved_exp_sha,
            "expected_exp_sha": expected_exp_sha,
            "exp_sha_match": (resolved_exp_sha == expected_exp_sha),
            "dev_base_sha": dev_base_sha,
            "dev_base_sha_matches_required": (dev_base_sha == DEV_BASE_SHA_REQUIRED),
            "end_date": str(end_date) if end_date else None,
            "algo": EXPECTED_ALGORITHM_VERSION,
            "hc": EXPECTED_HISTORY_CONTRACT_VERSION,
            "target_trade_date_count": TARGET_TRADE_DATE_COUNT,
            "query_shapes": {
                "is_aggregate_only_step1_row_summary":
                    query_is_aggregate_only(SQL_STEP1_ROW_SUMMARY),
                "no_select_star_step2_daily_coverage":
                    not query_contains_select_star(SQL_STEP2_DAILY_COVERAGE),
                "uses_lag_partition_step5_regime":
                    query_uses_transition_lag(build_sql_step5_transition("regime_value")),
            },
        }

    if not database_url:
        raise ExpectationMismatch("database_url is required for real run")
    if not end_date:
        raise ExpectationMismatch("--end-date is required (§8 fail-closed; no MAX(trade_date))")
    if dev_base_sha != DEV_BASE_SHA_REQUIRED:
        raise ExpectationMismatch(
            f"DEV_BASE required={DEV_BASE_SHA_REQUIRED!r} got={dev_base_sha!r}"
        )
    if resolved_exp_sha is None:
        raise ExpectationMismatch("cannot resolve git HEAD at worktree; EXP_SHA invalid")
    if resolved_exp_sha != expected_exp_sha:
        raise ExpectationMismatch(
            f"EXP_SHA mismatch arg={expected_exp_sha!r} vs git HEAD={resolved_exp_sha!r}"
        )

    # 统一 params dict（全局 algo / hc 过滤；禁止写回）
    PARAMS_BASE = {
        "algo": EXPECTED_ALGORITHM_VERSION,
        "hc": EXPECTED_HISTORY_CONTRACT_VERSION,
    }

    # ============================================================
    # Step 0 (TX #1) — 候选交易日查询（最多300行）→ 取最近 120
    # ============================================================
    with SessionGuard(database_url) as cur:
        cur.execute(SQL_STEP0_CANDIDATE_TRADE_DATES, PARAMS_BASE)
        all_candidates_desc = [r[0] for r in cur.fetchall()]  # list[date] 或 list[str]
    # 归一化成 date 并选 <= end_date 的最近 120 升序
    from .dataset_schema import build_selected_trade_dates, validate_120_consecutive_trade_dates
    eligible_sorted_desc = sorted(
        [d for d in all_candidates_desc if d is not None and date.fromisoformat(str(d)) <= end_date],
        key=lambda d: str(d),  # YYYY-MM-DD str/date 可比较
        reverse=True,
    )
    eligible_asc = list(reversed(eligible_sorted_desc))
    selected_asc = build_selected_trade_dates(
        [date.fromisoformat(str(d)) for d in eligible_asc],
        target_count=TARGET_TRADE_DATE_COUNT,
    )
    if not selected_asc:
        raise ExpectationMismatch(
            f"§8 fail-closed: <= {end_date} 的 canonical 交易日为空（algo={PARAMS_BASE['algo']} hc={PARAMS_BASE['hc']}）。STOP."
        )
    date_info = validate_120_consecutive_trade_dates(selected_asc)
    trade_dates: list[date] = selected_asc

    lineage: dict[str, Any] = {
        "DEV_BASE_SHA": dev_base_sha,
        "EXP_SHA": resolved_exp_sha,
        "DATA_SNAPSHOT_AT": datetime.now(timezone.utc).isoformat(),
        "TRADE_DATE_START": date_info["start"],
        "TRADE_DATE_END": date_info["end"],
        "TRADE_DATE_COUNT": date_info["count"],
        "TRADE_DATE_IS_EXACT_TARGET": date_info["is_exact_target"],
        "ALGORITHM_VERSION": PARAMS_BASE["algo"],
        "HISTORY_CONTRACT_VERSION": PARAMS_BASE["hc"],
        "ARCHITECTURE": "DB_NATIVE_QUERY_ON_DEMAND",
    }

    # ============================================================
    # Step 0bis (TX #2) — lineage 补充计数 / updated_at / source run count
    # ============================================================
    # 传入 psycopg2 的 trade_dates 保持 list[datetime.date]，让驱动正确绑定 PostgreSQL date[]。
    params = dict(PARAMS_BASE, trade_dates=trade_dates)
    with SessionGuard(database_url) as cur:
        cur.execute(SQL_STEP0_LINEAGE_COUNTS, params)
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        counts = dict(zip(cols, row))
    # 转成可 JSON 序列化
    lineage["ROW_COUNT"] = int(counts["row_count"] or 0)
    lineage["DISTINCT_INSTRUMENT_COUNT"] = int(counts["distinct_instrument_count"] or 0)
    lineage["MIN_UPDATED_AT"] = (
        counts["min_updated_at"].isoformat() if counts.get("min_updated_at") else "UNAVAILABLE"
    )
    lineage["MAX_UPDATED_AT"] = (
        counts["max_updated_at"].isoformat() if counts.get("max_updated_at") else "UNAVAILABLE"
    )
    lineage["SOURCE_HISTORY_RUN_COUNT"] = (
        int(counts["source_history_run_count"] or 0)
        if counts.get("source_history_run_count") is not None
        else "UNAVAILABLE"
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "round1_lineage.json", lineage)

    # ============================================================
    # Step 1 (TX #3 + #4 + #5) — Integrity audit
    # ============================================================
    with SessionGuard(database_url) as cur:
        cur.execute(SQL_STEP1_ROW_SUMMARY, params)
        r = cur.fetchone()
        cols = [d[0] for d in cur.description]
        step1_summary = dict(zip(cols, r))
    with SessionGuard(database_url) as cur:
        cur.execute(SQL_STEP1_DUPLICATE_COUNT, params)
        duplicate_count = int(cur.fetchone()[0] or 0)
    duplicate_sample: list[dict] = []
    if duplicate_count > 0:
        with SessionGuard(database_url) as cur:
            cur.execute(SQL_STEP1_DUPLICATE_SAMPLE, params)
            ccols = [d[0] for d in cur.description]
            duplicate_sample = [dict(zip(ccols, rr)) for rr in cur.fetchall()]
    with SessionGuard(database_url) as cur:
        cur.execute(SQL_STEP1_SOURCE_HISTOGRAM, params)
        ccols = [d[0] for d in cur.description]
        source_hist = [dict(zip(ccols, rr)) for rr in cur.fetchall()]
    integrity_summary = {
        "lineage_snapshot_trade_date_start": lineage["TRADE_DATE_START"],
        "lineage_snapshot_trade_date_end": lineage["TRADE_DATE_END"],
        "row_summary": step1_summary,
        "duplicate_count": duplicate_count,
        "duplicate_sample_limit_20": duplicate_sample,
        "source_history_run_histogram_limit_50": source_hist,
    }
    _write_json(out_dir / "integrity_summary.json", integrity_summary)

    # ============================================================
    # Step 2 (TX #6) — Coverage / Missingness daily ≤ 120 rows
    # ============================================================
    with SessionGuard(database_url) as cur:
        cur.execute(SQL_STEP2_DAILY_COVERAGE, params)
        ccols = [d[0] for d in cur.description]
        daily_coverage = [dict(zip(ccols, rr)) for rr in cur.fetchall()]
    coverage_missingness = {"daily_coverage_rows": daily_coverage}
    _write_json(out_dir / "coverage_missingness.json", coverage_missingness)

    # ============================================================
    # Step 3 (TX per field — Categorical primitive)
    # ============================================================
    categorical_primitives: dict[str, list[dict]] = {}
    for field in CATEGORICAL_STATE_FIELDS:
        sql = build_sql_step3_categorical(field)
        with SessionGuard(database_url) as cur:
            cur.execute(sql, params)
            ccols = [d[0] for d in cur.description]
            categorical_primitives[field] = [dict(zip(ccols, rr)) for rr in cur.fetchall()]
    _write_json(out_dir / "categorical_primitives.json", categorical_primitives)

    # ============================================================
    # Step 4 (TX per field — Continuous primitive)
    # ============================================================
    continuous_primitives: dict[str, dict] = {}
    for field in CONTINUOUS_STATE_FIELDS:
        sql, _ = build_sql_step4_continuous(field, include_percentile=True)
        rows: list[dict] = []
        percentile_deferred = False
        try:
            with SessionGuard(database_url, verbose=False) as cur:
                cur.execute(sql, params)
                ccols = [d[0] for d in cur.description]
                rows = [dict(zip(ccols, rr)) for rr in cur.fetchall()]
        except Exception:  # noqa: BLE001
            # 如 percentile_cont 负担过高 → 标记 PERCENTILE_DEFERRED 并无 percentile 重跑
            percentile_deferred = True
            sql_no_p, _ = build_sql_step4_continuous(field, include_percentile=False)
            with SessionGuard(database_url) as cur:
                cur.execute(sql_no_p, params)
                ccols = [d[0] for d in cur.description]
                rows = [dict(zip(ccols, rr)) for rr in cur.fetchall()]
        continuous_primitives[field] = {
            "percentile_status": (
                "COMPUTED_IN_POSTGRES" if not percentile_deferred else "PERCENTILE_DEFERRED"
            ),
            "daily_aggregates": rows,
        }
    _write_json(out_dir / "continuous_primitives.json", continuous_primitives)

    # ============================================================
    # Step 5 (TX per field — Transition audit)
    # ============================================================
    transition_summary: dict[str, list[dict]] = {}
    for field in CATEGORICAL_STATE_FIELDS:
        sql = build_sql_step5_transition(field)
        with SessionGuard(database_url) as cur:
            cur.execute(sql, params)
            ccols = [d[0] for d in cur.description]
            transition_summary[field] = [dict(zip(ccols, rr)) for rr in cur.fetchall()]
    _write_json(out_dir / "transition_summary.json", transition_summary)

    # ============================================================
    # Step 6 (No TX) — Round 1 Summary Markdown
    # ============================================================
    dup_msg = (
        f"DUPLICATE ROWS = {duplicate_count}"
        if duplicate_count > 0
        else "NO_DUPLICATE_ROWS"
    )
    verdict = (
        "INVALID — duplicate (prompt §9)"
        if duplicate_count > 0
        else ("PARTIAL — trade_date_count < 120"
              if int(lineage["TRADE_DATE_COUNT"]) < TARGET_TRADE_DATE_COUNT
              else "PASS — DB-native integrity base OK")
    )
    summary_md = f"""# Round 1 Summary — DB-Native / Query-on-Demand (Arch v2)

## 1. Crash evidence（服务器重启前一次 boot OOM 扫描）
```
OOM_CONFIRMED
```
- python/python3 docker memcg scope 多次 OOM kill（anon-rss ~3.9G）。
- 原因：旧 frozen dataset + full DataFrame 模式。

## 2. Architecture change
```text
old: full extraction (69 columns × 600k rows × pandas merge × parquet write)
new: DB-native / aggregate-on-demand
```
- No parquet, no full DataFrame, no giant VALUES (...), no full bars fetchall.

## 3. Lineage (Logical Window)
```text
DEV_BASE_SHA = {lineage['DEV_BASE_SHA']}
EXP_SHA      = {lineage['EXP_SHA']}
ARCHITECTURE = {lineage['ARCHITECTURE']}
DATA_SNAPSHOT_AT = {lineage['DATA_SNAPSHOT_AT']}

TRADE_DATE_START = {lineage['TRADE_DATE_START']}
TRADE_DATE_END   = {lineage['TRADE_DATE_END']}
TRADE_DATE_COUNT = {lineage['TRADE_DATE_COUNT']} (TARGET={TARGET_TRADE_DATE_COUNT})
TRADE_DATE_IS_EXACT_TARGET = {lineage['TRADE_DATE_IS_EXACT_TARGET']}

ALGORITHM_VERSION       = {lineage['ALGORITHM_VERSION']}
HISTORY_CONTRACT_VERSION = {lineage['HISTORY_CONTRACT_VERSION']}

ROW_COUNT                = {lineage['ROW_COUNT']}
DISTINCT_INSTRUMENT_COUNT= {lineage['DISTINCT_INSTRUMENT_COUNT']}
MIN_UPDATED_AT           = {lineage['MIN_UPDATED_AT']}
MAX_UPDATED_AT           = {lineage['MAX_UPDATED_AT']}
SOURCE_HISTORY_RUN_COUNT = {lineage['SOURCE_HISTORY_RUN_COUNT']}
```

## 4. Integrity (Step 1)
- {dup_msg}
- row_summary total rows = {step1_summary.get('row_count')}
- trade_date_count = {step1_summary.get('trade_date_count')}
- distinct_instrument_count = {step1_summary.get('distinct_instrument_count')}
- algo_match_count (should equal rows) = {step1_summary.get('algo_match_count')}
- hc_outer_match_count = {step1_summary.get('hc_outer_match_count')}
- hc_payload_match_count = {step1_summary.get('hc_payload_match_count')}

## 5. Coverage / Missingness (Step 2)
- daily rows = {len(daily_coverage)}
- See coverage_missingness.json for per-readiness + core primitive counts.

## 6. Categorical primitives (Step 3)
- fields = {', '.join(CATEGORICAL_STATE_FIELDS)}
- max rows per field = 120 × card(value) aggregate only, no raw rows.

## 7. Continuous primitives (Step 4)
- fields = {', '.join(CONTINUOUS_STATE_FIELDS)}
- Stat aggregates: count / avg / min / max / p05 / p50 / p95 (or PERCENTILE_DEFERRED).

## 8. Transition audit (Step 5)
- Uses PostgreSQL LAG(state) OVER (PARTITION BY instrument_id ORDER BY trade_date).
- Output: prev_state × current_state × count (≤ 500 rows per field).

## 9. Verdict
```text
{verdict}
```

## 10. Interpretation
> Results are valid for the recorded logical window and database state observed at DATA_SNAPSHOT_AT ({lineage['DATA_SNAPSHOT_AT']}). No byte-for-byte frozen reproducibility is claimed; this is intentional.
"""
    (out_dir / "ROUND1_SUMMARY.md").write_text(summary_md)
    return {
        "round1_lineage_path": str(out_dir / "round1_lineage.json"),
        "integrity_summary_path": str(out_dir / "integrity_summary.json"),
        "coverage_missingness_path": str(out_dir / "coverage_missingness.json"),
        "categorical_primitives_path": str(out_dir / "categorical_primitives.json"),
        "continuous_primitives_path": str(out_dir / "continuous_primitives.json"),
        "transition_summary_path": str(out_dir / "transition_summary.json"),
        "round1_summary_md_path": str(out_dir / "ROUND1_SUMMARY.md"),
        "duplicate_count": duplicate_count,
        "round1_verdict": verdict,
    }


# ============================================================================
# CLI
# ============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="round1_db_native",
        description="Round 1 DB-native pipeline（query-on-demand，不生成 parquet/DataFrame）",
    )
    p.add_argument("--out-dir", required=True, type=Path,
                   help="输出 JSON/MD 目录：round1_lineage.json / integrity_summary.json / ...")
    p.add_argument("--dev-base-sha", required=True,
                   help=f"必须为 {DEV_BASE_SHA_REQUIRED}")
    p.add_argument("--exp-sha", required=True,
                   help="实验分支 commit SHA；运行时会和 git rev-parse HEAD 比较，不一致 STOP。")
    p.add_argument("--end-date",
                   type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                   help="§8 fail-closed：不允许默认 MAX(trade_date)；请先用 diagnose 选最近 complete date。")
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                   help="默认读 env DATABASE_URL（resolve 时支持 DSN_HOST_FROM/DSN_HOST_TO 覆盖 docker host）。")
    p.add_argument("--dry-run", action="store_true",
                   help="不连 DB；打印参数 + query shape 断言 + 版本校验。")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    result = run_round1_db_native(
        database_url=args.database_url or "",
        out_dir=args.out_dir,
        end_date=args.end_date or date(1970, 1, 1),
        dev_base_sha=args.dev_base_sha,
        expected_exp_sha=args.exp_sha,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
        if result["dev_base_sha_matches_required"] and result["exp_sha_match"]:
            return 0
        return 2

    print(json.dumps({
        "OK": True,
        "round1_verdict": result.get("round1_verdict"),
        "duplicate_count": result.get("duplicate_count"),
        "outputs": {k: v for k, v in result.items() if k.endswith("_path")},
    }, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import dataclasses  # fallback for SessionGuard __KW_ONLY__ in <3.10 compat
    if sys.version_info < (3, 10):
        # SessionGuard uses dataclasses.KW_ONLY; stub if not available
        if not hasattr(dataclasses, "KW_ONLY"):
            dataclasses.KW_ONLY = type("_KW_ONLY", (), {})()
    raise SystemExit(main())
