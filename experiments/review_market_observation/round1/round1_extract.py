"""Round 1 Frozen Dataset 提取入口（严格只读）。

本文件根据 prompt.md §2-7 做以下修正：

§2.1 真实执行入口
  - 使用 `python -m experiments.review_market_observation.round1.round1_extract`
    的 module 方式，必须显式传入：
      --dev-base-sha  DEV_BASE_SHA (6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0)
      --exp-sha       EXP_SHA      (b89387e... 或后续 PREEXEC SHA)
      --data-dir      输出目录
      --end-date      YYYY-MM-DD (fail-closed：不默认 max(date))
      [--dry-run]     仅打印查询、不连 DB
    调用者可传 env DATABASE_URL；本脚本不接受任何秘密写入 manifest。

§3 Universe
  - 不用 `instruments.status='listed'` 过滤（会有 survivorship bias）。
  - Universe SSOT = 指定 (algorithm_version, history_contract_version) ×
    指定日期窗口下实际出现 daily_state 行的 instrument 集合。
  - instruments 表仅补 symbol/name 元数据（LEFT JOIN），不裁剪样本。

§4 固定 canonical version
  - EXPECTED_ALGORITHM_VERSION      = 1.0.0-core-split
  - EXPECTED_HISTORY_CONTRACT_VERSION = review-history-v2
    两者都写入 SQL WHERE 中，不允许混版本；若目标窗口行数/日期数不足，
    直接报告 INVALID，不自动回退。

§5 完整交易日选择
  - 必须显式传 --end-date；否则 STOP (不默认取 max(date) 当 completed)
  - 另外提供 `--diagnose-recent=N`，在不写 frozen dataset 的前提下，
    只读拉最近 N 天的行计数/valid率/版本分布，便于人先判断哪一天是 complete。

§6 Lineage manifest 14 字段
  - DEV_BASE_SHA / EXP_SHA / DATASET_ID / TRADE_DATE_START / TRADE_DATE_END
    / TRADE_DATE_COUNT / ROW_COUNT / INSTRUMENT_COUNT / ALGORITHM_VERSION
    / HISTORY_CONTRACT_VERSION / EXTRACTED_AT / SCHEMA_HASH / DATA_HASH
  - EXP_SHA 运行时用 `git rev-parse HEAD` 重新取，并与 --exp-sha 比较；
    不一致 = STOP。

§7 只读证据
  - 事务执行前：`SET TRANSACTION READ ONLY; SHOW transaction_read_only;`
    必须断言结果 = "on"。若不是 on，raise ReadOnlyAssertionError 终止。
  - 整个过程只有 SELECT；无 DDL/DML/COPY FROM。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any

from .dataset_schema import (
    ALL_STATE_PAYLOAD_FIELDS,
    BAR_FIELDS,
    DATASET_CODE,
    DATASET_VERSION,
    DB_OUTER_COLUMNS,
    TARGET_TRADE_DATE_COUNT,
    compute_schema_hash,
    build_selected_trade_dates,
    validate_120_consecutive_trade_dates,
    flatten_state_payload,
)

# 固定 canonical version（§4 单一事实源）
EXPECTED_ALGORITHM_VERSION = "1.0.0-core-split"
EXPECTED_HISTORY_CONTRACT_VERSION = "review-history-v2"

DEV_BASE_SHA_REQUIRED = "6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0"


# ============================================================================
# 只读错误
# ============================================================================
class ReadOnlyAssertionError(RuntimeError):
    """当 PostgreSQL transaction_read_only != on 时抛出（硬 fail-closed）。"""


class ExpectationMismatch(RuntimeError):
    """版本 / 行计数 / git 校验失败时抛出。"""


# ============================================================================
# SQL 常量（全部 SELECT，无 write 语义）
#
# 真实表 first_pyramid_history_daily_state 仅含如下 DB 列：
#   id, instrument_id, trade_date, algorithm_version, input_hash,
#   state_payload (JSONB), created_at, updated_at,
#   source_history_run_id, history_contract_version
#
# readiness / meta / trend / structure / momentum / volume 字段均存储在
# state_payload JSONB 中，所以所有引用都必须通过
#   state_payload ->> 'key'           (返回 text, 可 NULL)
#   (state_payload ->> 'key')::boolean (text 'true'/'false' → bool)
# 读取；不能直接写成列名（会报 UndefinedColumn）。
# ============================================================================

# §5 最近若干交易日诊断（不抽取，只看计数/valid率/版本）
SQL_DIAGNOSE_RECENT_DATES = """
SELECT
    trade_date,
    COUNT(*)                                                                      AS row_count,
    COUNT(*) FILTER (WHERE (s.state_payload ->> 'valid_for_market_aggregation')::boolean IS TRUE) AS valid_count,
    COUNT(DISTINCT instrument_id)                                                 AS n_instr,
    COUNT(*) FILTER (WHERE algorithm_version = %(algo)s)                          AS algo_match_count,
    COUNT(*) FILTER (WHERE history_contract_version = %(hc)s)                     AS hc_match_count
FROM first_pyramid_history_daily_state s
WHERE trade_date IS NOT NULL
GROUP BY trade_date
ORDER BY trade_date DESC
LIMIT %(limit)s
"""

# 已知版本下的真实候选交易日（取最近 300）
SQL_CANDIDATE_TRADE_DATES = """
SELECT DISTINCT trade_date
FROM first_pyramid_history_daily_state
WHERE trade_date IS NOT NULL
  AND algorithm_version = %(algo)s
  AND history_contract_version = %(hc)s
ORDER BY trade_date DESC
LIMIT 300
"""

# §3 抽取 daily_state（纯 SELECT；universe=版本匹配 + 日期内出现的所有 instrument）
# 说明：readiness 四列（history_sufficient / core_factor_ready /
# valid_for_market_aggregation / invalid_reason）在真实 schema 中不作为 DB 列，
# 所以必须从 state_payload JSONB 读取，类型上显式 CAST。
SQL_EXTRACT_DAILY_STATE = """
SELECT
    s.instrument_id,
    s.trade_date,
    s.algorithm_version,
    s.input_hash,
    s.source_history_run_id,
    s.history_contract_version                                   AS hc_outer,
    s.state_payload                                              AS payload,
    -- readiness（state_payload JSONB 转 bool / text）
    COALESCE((s.state_payload ->> 'history_sufficient')::boolean, FALSE)      AS history_sufficient,
    COALESCE((s.state_payload ->> 'core_factor_ready')::boolean, FALSE)       AS core_factor_ready,
    COALESCE((s.state_payload ->> 'valid_for_market_aggregation')::boolean, FALSE) AS valid_for_market_aggregation,
    (s.state_payload ->> 'invalid_reason')                                                     AS invalid_reason,
    i.symbol                                                     AS instrument_symbol,
    i.name                                                       AS instrument_name
FROM first_pyramid_history_daily_state s
LEFT JOIN instruments i ON i.id = s.instrument_id
WHERE s.trade_date = ANY(%(trade_dates)s)
  AND s.algorithm_version = %(algo)s
  AND s.history_contract_version = %(hc)s
"""

# Bar 抽取（LEFT JOIN daily_state 只抽冻结到的日期/instrument；不单独筛 status）
# 真实 schema 表名 = bars_daily；列名 = open/high/low/close/volume/amount/adj_factor
# 但 frozen canonical 列需要 bar_open / bar_high ... 形式，统一在 SELECT 中起别名。
SQL_EXTRACT_BARS = """
SELECT
    b.instrument_id,
    b.trade_date,
    b.open_price,
    b.high_price,
    b.low_price,
    b.close_price,
    b.volume,
    b.amount,
    b.adj_factor
FROM (
    SELECT
        instrument_id,
        trade_date,
        open       AS open_price,
        high       AS high_price,
        low        AS low_price,
        close      AS close_price,
        volume,
        amount,
        adj_factor
    FROM bars_daily
) b
WHERE (b.instrument_id, b.trade_date) IN (
    -- 用 VALUES list 绑定冻结到的 (instrument, date) 对
    SELECT v.instrument_id::uuid, v.trade_date::date
    FROM (VALUES {values_placeholders}) AS v(instrument_id, trade_date)
)
"""


# ============================================================================
# Git SHA 校验（§6）
# ============================================================================

def resolve_git_head_sha(search_from: Path) -> str | None:
    """在指定目录下执行 `git rev-parse HEAD`，返回 stripped 40 位 SHA。"""
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
    if len(out) != 40:
        return None
    return out


# ============================================================================
# DSN 规范化（§12 blocker fix：SQLAlchemy scheme → libpq scheme）
# ============================================================================

_LIBPQ_SCHEME_ALIASES = {
    "postgresql+psycopg": "postgresql",
    "postgresql+psycopg2": "postgresql",
    "postgres": "postgresql",
}


def normalize_libpq_dsn(dsn: str) -> str:
    """把 SQLAlchemy 风格的 DSN 方案名翻译成 libpq/psycopg2 接受的方案名。

    Blocker 根因：容器环境 DATABASE_URL 常为 SQLAlchemy URL
    (``postgresql+psycopg://user:pw@host:port/db``)；
    psycopg2.connect() 只接受 libpq 方案 ``postgresql://...``。
    不修改凭据、路径或查询参数，仅替换 scheme。
    """
    if not dsn:
        return dsn
    # "scheme://..." 最短匹配
    if "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    norm_scheme = _LIBPQ_SCHEME_ALIASES.get(scheme, scheme)
    if norm_scheme == scheme:
        return dsn
    return f"{norm_scheme}://{rest}"


def apply_dsn_host_override(dsn: str, *, from_host: str | None, to_host: str | None) -> str:
    """按显式映射替换 URL DSN 中的 host（仅改 host，不动凭据/端口/路径/查询）。

    §12 远程执行场景：
      - 容器内 DATABASE_URL 的 host 常是 docker-compose 别名（如 postgres）；
      - 在容器外部的宿主机 python 进程上，该 DNS 名无法解析；
      - 运行者可先 `docker inspect` 拿到 postgres 容器 IP，再通过本函数
        把 host 精确替换为可路由 IP（不触碰 scheme / userinfo / port / path / query）。

    匹配规则：
      - ``from_host`` 与 URL host 精确相等才替换（大小写敏感；非空时有效）；
      - ``to_host`` 为空 或 from_host 为空 / 未匹配 → 原样返回；
      - URL host 包含 IPv6 ``[...]`` 时若不匹配则跳过。
    """
    if not dsn or not from_host or not to_host or "://" not in dsn:
        return dsn
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(dsn)
    except Exception:
        # key=value 形式或非 URL DSN：不改动
        return dsn
    # parts.hostname 去掉端口；比较时用 hostname（去掉 user:pass@、去掉 :port）
    if parts.hostname != from_host:
        return dsn
    # 用 netloc 精确替换 host 段，保留 userinfo + port
    # netloc 形式可能为：[user[:pass]@]host[:port]
    # 最稳妥：按 parts 分量重新组装（拆分 netloc 再拼回也可）
    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo = f"{userinfo}:{parts.password}"
        userinfo += "@"
    # 原 port 保留
    port_suffix = ""
    if parts.port is not None:
        port_suffix = f":{parts.port}"
    new_netloc = f"{userinfo}{to_host}{port_suffix}"
    return urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))


def resolve_runtime_dsn(dsn: str) -> str:
    """组合调用：normalize_libpq_dsn → 可选 env-driven host override。

    可选环境变量（纯运行时注入，不写进任何 manifest/commit）：
      DSN_HOST_FROM / DSN_HOST_TO：若同时设置，则按 apply_dsn_host_override 替换。
    """
    dsn = normalize_libpq_dsn(dsn)
    host_from = os.environ.get("DSN_HOST_FROM")
    host_to = os.environ.get("DSN_HOST_TO")
    if host_from and host_to:
        dsn = apply_dsn_host_override(dsn, from_host=host_from, to_host=host_to)
    return dsn


# ============================================================================
# 只读连接 + 断言（§7）
# ============================================================================

def _assert_transaction_readonly(conn_cursor):
    """在当前游标上执行 SHOW 并断言 transaction_read_only == on。

    注意：SET TRANSACTION READ ONLY 必须在事务第一条语句前调用；
    但为了 fail-closed，我们仍然再 SHOW 一次确认，任何异常直接 raise。
    """
    conn_cursor.execute("SHOW transaction_read_only")
    row = conn_cursor.fetchone()
    val = None if row is None else (row[0] if not hasattr(row, "_fields") else row[0])
    if val != "on":
        raise ReadOnlyAssertionError(
            f"PostgreSQL transaction_read_only expected='on' got={val!r}. "
            f"STOP: extraction requires a read-only transaction."
        )


# ============================================================================
# 核心执行（read-only transaction）
# ============================================================================

@dataclass
class ExtractionResult:
    frozen_df_rows: list[dict]
    trade_dates: list[date]
    instrument_count: int
    row_count: int


def run_readonly_extraction(
    database_url: str,
    *,
    end_date: date,
    dev_base_sha: str,
    expected_exp_sha: str,
    resolved_exp_sha: str | None,
    data_dir: Path,
    algo: str,
    hc: str,
    target_count: int = TARGET_TRADE_DATE_COUNT,
) -> tuple[dict, Path | None]:
    """read-only 全流程；返回 (manifest, frozen_parquet_path)。

    manifest 一定是 dict，是否生成文件取决于 caller（§11 dry-run 不写）。
    """
    import pandas as pd

    # §6 EXP_SHA 校验（fail-closed）
    if resolved_exp_sha is None:
        raise ExpectationMismatch(
            "§6 lineage: 无法在当前 worktree 解析 git HEAD；"
            "本提取器必须从 git checkout 执行，以绑定 EXP_SHA。"
        )
    if resolved_exp_sha != expected_exp_sha:
        raise ExpectationMismatch(
            f"§6 lineage: EXP_SHA mismatch. --exp-sha={expected_exp_sha!r} "
            f"!= resolved HEAD={resolved_exp_sha!r}. STOP."
        )
    if dev_base_sha != DEV_BASE_SHA_REQUIRED:
        raise ExpectationMismatch(
            f"§6 lineage: DEV_BASE_SHA mismatch. required={DEV_BASE_SHA_REQUIRED!r} "
            f"got={dev_base_sha!r}. STOP. 实验固定 DEV_BASE，不 rebase。"
        )

    if algo != EXPECTED_ALGORITHM_VERSION:
        raise ExpectationMismatch(
            f"§4 canonical: algo={algo!r} != expected {EXPECTED_ALGORITHM_VERSION!r}"
        )
    if hc != EXPECTED_HISTORY_CONTRACT_VERSION:
        raise ExpectationMismatch(
            f"§4 canonical: hc={hc!r} != expected {EXPECTED_HISTORY_CONTRACT_VERSION!r}"
        )

    import psycopg2  # 延迟 import，dry-run 不需要 psycopg2

    manifest: dict = {
        "DEV_BASE_SHA": dev_base_sha,
        "EXP_SHA": resolved_exp_sha,
        "EXTRACTED_AT": datetime.now(timezone.utc).isoformat(),
        "SCHEMA_HASH": compute_schema_hash(),
        "ALGORITHM_VERSION": algo,
        "HISTORY_CONTRACT_VERSION": hc,
        "DATASET_CODE": DATASET_CODE,
        "DATASET_VERSION": DATASET_VERSION,
    }

    # §5.2 — 先拿候选日期，再选 120
    with psycopg2.connect(resolve_runtime_dsn(database_url)) as conn:
        # 整个事务设为只读（§7）
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            _assert_transaction_readonly(cur)

            cur.execute(SQL_CANDIDATE_TRADE_DATES, {"algo": algo, "hc": hc})
            candidate_dates_desc = [r[0] for r in cur.fetchall()]  # date obj desc order

    candidate_dates_desc_sorted = sorted(candidate_dates_desc, reverse=True)
    # 选 end_date 往前（含 end_date）最近 target_count 个
    eligible_asc = sorted([d for d in candidate_dates_desc_sorted if d <= end_date])
    selected_asc = build_selected_trade_dates(eligible_asc, target_count)
    date_info = validate_120_consecutive_trade_dates(selected_asc)
    if not selected_asc:
        raise ExpectationMismatch(
            f"§5 complete-date: 目标算法/合同下 <= {end_date} 的交易日为空，"
            f"无法向前冻结 {target_count} 日。STOP."
        )
    manifest["TRADE_DATE_START"] = str(selected_asc[0])
    manifest["TRADE_DATE_END"] = str(selected_asc[-1])
    manifest["TRADE_DATE_COUNT"] = len(selected_asc)
    manifest["TRADE_DATE_IS_EXACT_TARGET"] = date_info["is_exact_target"]

    # 实际 frozen join
    with psycopg2.connect(resolve_runtime_dsn(database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            _assert_transaction_readonly(cur)
            # 只读：daily_state + instrument metadata
            cur.execute(
                SQL_EXTRACT_DAILY_STATE,
                {
                    "trade_dates": list(selected_asc),
                    "algo": algo,
                    "hc": hc,
                },
            )
            colnames = [d[0] for d in cur.description]
            rows = cur.fetchall()

    # 构造 DataFrame（flatten payload）
    records: list[dict] = []
    for row in rows:
        rec = dict(zip(colnames, row))
        payload = rec.get("payload") or {}
        flat = flatten_state_payload(payload, hc_outer=rec.get("hc_outer"))
        # 外层列优先，然后是 payload flat 列
        out_rec: dict = {}
        # 先填外层 DB 列（确保 HC 一致校验会比对）
        for k in DB_OUTER_COLUMNS:
            out_rec[k] = rec.get(k)
        # symbol/name 由 §3 universe 决定：直接写入
        out_rec["instrument_symbol"] = rec.get("instrument_symbol")
        out_rec["instrument_name"] = rec.get("instrument_name")
        # readiness 显式列（DB 列优先；fallback=payload 解析，两者都允许为 False/None）
        out_rec["history_sufficient"] = rec.get("history_sufficient")
        out_rec["core_factor_ready"] = rec.get("core_factor_ready")
        out_rec["valid_for_market_aggregation"] = rec.get("valid_for_market_aggregation")
        out_rec["invalid_reason"] = rec.get("invalid_reason")
        # payload flat（覆盖）
        for k, v in flat.items():
            out_rec[k] = v
        # 再补 source columns 直接值，防止 payload 中缺失
        for k in ("instrument_id", "trade_date", "algorithm_version", "input_hash",
                  "source_history_run_id", "hc_outer"):
            if k in rec and out_rec.get(k) is None:
                out_rec[k] = rec[k]
        records.append(out_rec)

    df = pd.DataFrame.from_records(records)
    if df.empty:
        raise ExpectationMismatch(f"§3 universe: frozen 行集为空。STOP.")

    # 再做一次 Bars：只读事务
    with psycopg2.connect(resolve_runtime_dsn(database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            _assert_transaction_readonly(cur)
            # 构造 (instrument_id, trade_date) 的 VALUES list
            pairs = (
                df[["instrument_id", "trade_date"]]
                .drop_duplicates()
                .astype({"instrument_id": str, "trade_date": str})
            )
            if pairs.empty:
                bars_rows: list = []
            else:
                placeholders = ", ".join(["(%s, %s)"] * len(pairs))
                sql = SQL_EXTRACT_BARS.format(values_placeholders=placeholders)
                params = [v for _, r in pairs.iterrows() for v in (r.instrument_id, r.trade_date)]
                cur.execute(sql, params)
                bcols = [d[0] for d in cur.description]
                bars_rows = [dict(zip(bcols, r)) for r in cur.fetchall()]

    bars_df = pd.DataFrame(bars_rows) if bars_rows else pd.DataFrame(columns=BAR_FIELDS)

    # 合并 bars 到 frozen df（left on instrument_id + trade_date）
    if not bars_df.empty:
        df = df.merge(bars_df, on=["instrument_id", "trade_date"], how="left")

    # 确保所有 dataset_schema 列存在，缺失列填 None，保证 69 列 canonical
    from .dataset_schema import FROZEN_COLUMNS
    for c in FROZEN_COLUMNS:
        if c not in df.columns:
            df[c] = None
    df = df[list(FROZEN_COLUMNS)]

    # 写出 parquet（本地/远程 data_dir）
    data_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = data_dir / "frozen_dataset.parquet"
    df.to_parquet(parquet_path, index=False, compression="snappy")

    # data hash = sha256(parquet bytes)
    data_hash = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    manifest["DATA_HASH"] = data_hash
    manifest["ROW_COUNT"] = int(len(df))
    manifest["INSTRUMENT_COUNT"] = int(df["instrument_id"].nunique())
    manifest["DATASET_ID"] = (
        f"{DATASET_CODE}_{manifest['TRADE_DATE_START']}_{manifest['TRADE_DATE_END']}_"
        f"sha{data_hash[:10]}"
    )
    return manifest, parquet_path


# ============================================================================
# §5.1 Diagnose Recent Dates（不写 frozen dataset，仅诊断）
# ============================================================================

def run_recent_diagnose(database_url: str, limit: int, algo: str, hc: str) -> list[dict]:
    import psycopg2
    with psycopg2.connect(resolve_runtime_dsn(database_url)) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            _assert_transaction_readonly(cur)
            cur.execute(SQL_DIAGNOSE_RECENT_DATES, {"limit": limit, "algo": algo, "hc": hc})
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


# ============================================================================
# Manifest 落盘（private，含 DB host/？不：只写已经在 manifest dict 里的字段）
# ============================================================================

def write_outputs(
    data_dir: Path,
    manifest: dict,
    parquet_path: Path | None,
    dataset_code: str,
) -> tuple[Path, Path]:
    """写 extracted_manifest.json + checksums.sha256 到 data_dir。返回两文件路径。"""
    manifest_path = data_dir / "extracted_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False))
    # checksum 只包含 parquet 与 manifest（两文件 sha）
    checksums = ""
    for f in (parquet_path, manifest_path):
        if f is None or not f.exists():
            continue
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        checksums += f"{digest}  {f.name}\n"
    cks_path = data_dir / "checksums.sha256"
    cks_path.write_text(checksums)
    return manifest_path, cks_path


# ============================================================================
# Public manifest 导出（§9：脱敏，无 DATABASE_URL 无密码）
# ============================================================================

PUBLIC_MANIFEST_ALLOW_KEYS = (
    "DEV_BASE_SHA", "EXP_SHA", "DATASET_ID",
    "TRADE_DATE_START", "TRADE_DATE_END", "TRADE_DATE_COUNT",
    "ROW_COUNT", "INSTRUMENT_COUNT",
    "ALGORITHM_VERSION", "HISTORY_CONTRACT_VERSION",
    "EXTRACTED_AT", "SCHEMA_HASH", "DATA_HASH",
    "TRADE_DATE_IS_EXACT_TARGET",
    "DATASET_CODE", "DATASET_VERSION",
)


def build_public_manifest(manifest: dict, extra: dict | None = None) -> dict:
    """从 private manifest 构造无秘密的公共 manifest（用于 Git 入库）。"""
    public = {k: manifest[k] for k in PUBLIC_MANIFEST_ALLOW_KEYS if k in manifest}
    if extra:
        for k, v in extra.items():
            if k in PUBLIC_MANIFEST_ALLOW_KEYS:
                continue
            public[k] = v
    return public


# ============================================================================
# CLI
# ============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="round1_extract",
        description="Round 1 Frozen Dataset 提取器（只读；显式 end-date；EXP_SHA 硬校验）",
    )
    p.add_argument("--data-dir", required=True, type=Path,
                   help="输出目录（写 frozen_dataset.parquet / extracted_manifest.json / checksums.sha256）")
    p.add_argument("--dev-base-sha", required=True,
                   help=f"必须为 {DEV_BASE_SHA_REQUIRED}（固定 DEV_BASE，不 rebase）")
    p.add_argument("--exp-sha", required=True,
                   help="真实产生冻结数据集的实验 commit SHA（运行时会用 git rev-parse HEAD 二次校验）")
    p.add_argument("--end-date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                   help="冻结窗口包含的最后一个交易日（§5 fail-closed，不得省略）；"
                        "省略时请使用 --diagnose-recent 先诊断最近日期")
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                   help="读取 DB；默认读 env DATABASE_URL")
    p.add_argument("--dry-run", action="store_true",
                   help="不连 DB、不写文件；仅打印 CLI 参数 & 版本 & schema hash")
    p.add_argument("--diagnose-recent", type=int, metavar="N", default=0,
                   help="若 > 0，只读查询最近 N 个交易日的行数/valid率/版本匹配度，"
                        "写 data_dir/recent_trade_dates_diagnose.json，不生成 frozen dataset")
    p.add_argument("--target-trade-date-count", type=int, default=TARGET_TRADE_DATE_COUNT,
                   help=f"目标交易日数，默认 {TARGET_TRADE_DATE_COUNT}；仅用于 diagnose 对比")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # 解析 repo root（无论是否 git worktree）
    script_dir = Path(__file__).resolve()
    # 从 experiments/ 往上找 .git
    repo_root: Path | None = None
    for p in [script_dir.parents[3], script_dir.parents[2], script_dir.parent, Path.cwd()]:
        if (p / ".git").exists():
            repo_root = p
            break
    resolved_exp_sha = None
    if repo_root is not None:
        resolved_exp_sha = resolve_git_head_sha(repo_root)

    if args.dry_run:
        payload = {
            "mode": "dry-run",
            "data_dir": str(args.data_dir.resolve()),
            "dev_base_sha": args.dev_base_sha,
            "dev_base_sha_matches_required": args.dev_base_sha == DEV_BASE_SHA_REQUIRED,
            "exp_sha_arg": args.exp_sha,
            "exp_sha_resolved_git_head": resolved_exp_sha,
            "exp_sha_matches_resolved": (
                (resolved_exp_sha is not None) and (resolved_exp_sha == args.exp_sha)
            ),
            "end_date": str(args.end_date) if args.end_date else None,
            "target_trade_date_count": args.target_trade_date_count,
            "algo_expected": EXPECTED_ALGORITHM_VERSION,
            "hc_expected": EXPECTED_HISTORY_CONTRACT_VERSION,
            "schema_hash": compute_schema_hash(),
            "dataset_code": DATASET_CODE,
            "dataset_version": DATASET_VERSION,
        }
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
        # dry-run 对下列三项做 fail-closed 校验：
        if payload["dev_base_sha_matches_required"] and payload["exp_sha_matches_resolved"]:
            return 0
        print("[dry-run] FAIL：DEV_BASE_SHA 或 EXP_SHA 校验不通过（真实执行将 STOP）",
              file=sys.stderr)
        return 2

    if args.database_url is None:
        print("ERROR: --database-url 或 DATABASE_URL 未设置", file=sys.stderr)
        return 2

    # §5: diagnose mode（不抽 frozen，只看最近 N）
    if args.diagnose_recent and args.diagnose_recent > 0:
        rows = run_recent_diagnose(
            args.database_url, args.diagnose_recent,
            EXPECTED_ALGORITHM_VERSION, EXPECTED_HISTORY_CONTRACT_VERSION,
        )
        args.data_dir.mkdir(parents=True, exist_ok=True)
        out_path = args.data_dir / "recent_trade_dates_diagnose.json"
        out_path.write_text(json.dumps(
            {
                "algo": EXPECTED_ALGORITHM_VERSION,
                "hc": EXPECTED_HISTORY_CONTRACT_VERSION,
                "DEV_BASE_SHA": args.dev_base_sha,
                "EXP_SHA_arg": args.exp_sha,
                "EXP_SHA_resolved": resolved_exp_sha,
                "rows": rows,
            },
            indent=2, sort_keys=True, default=str, ensure_ascii=False,
        ))
        print(f"[diagnose] 最近 {args.diagnose_recent} 交易日诊断写入 {out_path}")
        print(json.dumps(rows[:5], indent=2, default=str))
        return 0

    # §5: actual frozen extraction requires --end-date
    if args.end_date is None:
        print(
            "ERROR: §5 fail-closed：frozen dataset 必须显式 --end-date YYYY-MM-DD，\n"
            "       不得默认 max(trade_date)。请先用 --diagnose-recent=30 确认 complete date。",
            file=sys.stderr,
        )
        return 2

    # clean state guard：data_dir 若有 frozen dataset 必须先手工删除（防止 silent overwrite）
    if (args.data_dir / "frozen_dataset.parquet").exists() or \
       (args.data_dir / "extracted_manifest.json").exists():
        print(
            "ERROR: data-dir 已存在 frozen_dataset.parquet 或 extracted_manifest.json，\n"
            f"       防止覆盖，请先清空：rm -rf '{args.data_dir}'",
            file=sys.stderr,
        )
        return 3

    # actual extraction
    manifest, parquet = run_readonly_extraction(
        args.database_url,
        end_date=args.end_date,
        dev_base_sha=args.dev_base_sha,
        expected_exp_sha=args.exp_sha,
        resolved_exp_sha=resolved_exp_sha,
        data_dir=args.data_dir,
        algo=EXPECTED_ALGORITHM_VERSION,
        hc=EXPECTED_HISTORY_CONTRACT_VERSION,
        target_count=args.target_trade_date_count,
    )
    manifest_path, cks_path = write_outputs(args.data_dir, manifest, parquet, DATASET_CODE)

    # 控制台打印 lineage 概览（不含密码）
    print("=== Extraction done ===")
    keys = [
        "DATASET_ID", "TRADE_DATE_START", "TRADE_DATE_END", "TRADE_DATE_COUNT",
        "TRADE_DATE_IS_EXACT_TARGET",
        "ROW_COUNT", "INSTRUMENT_COUNT",
        "ALGORITHM_VERSION", "HISTORY_CONTRACT_VERSION",
        "SCHEMA_HASH", "DATA_HASH",
    ]
    for k in keys:
        print(f"  {k}: {manifest.get(k)}")
    print(f"  files: {manifest_path} / {parquet} / {cks_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
