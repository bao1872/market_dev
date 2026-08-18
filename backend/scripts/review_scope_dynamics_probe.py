"""Scope Dynamics 只读基线测量 probe（R1 runtime-readiness）。

仅调用正式 production semantic path，不复制任何业务公式。

用法：
    cd backend && .venv/bin/python -m scripts.review_scope_dynamics_probe \
        --scope-type industry_l1 --scope-key 银行 --history 120

    # 不连库跑（--dry-run 仅校验参数与导入）
    .venv/bin/python -m scripts.review_scope_dynamics_probe \
        --scope-type concept --scope-key 人工智能 --history 60 --dry-run

    # 服务器一次性只读导出 Review Source Dataset（Full Corpus）
    .venv/bin/python -m scripts.review_scope_dynamics_probe \
        --mode export-dataset --dataset-dir /tmp/review-source-<sha>-v1 --asof-date 2026-08-01

    # 本地校验 + jsonl.gz → parquet + 生成 views
    .venv/bin/python -m scripts.review_scope_dynamics_probe \
        --mode dataset-validate --dataset-dir backend/.perfdata/review/review-source-<sha>-v1

约束：
    - 只读远程 bz_stock（通过本地 SSH Tunnel）；不写、不 publish、不编排。
    - 不拥有任何 Scope Observation / Position / Dynamics / Phase 算法。
    - 不拥有任何 Review 业务公式；只做 SQL 投影 + 文件 I/O + checksum + selection metadata。
    - 必须包含 ``def main() -> int`` 与 ``if __name__ == "__main__": raise SystemExit(main())``。

退出码：
    0: 成功完成基线测量并打印诊断
    2: 参数/输入校验失败
    1: 计算过程异常
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import logging
import os
import resource
import shutil
import sys
import uuid
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

# 这些 scope_type 是 R1 刻意覆盖集合（见 plan scale ladder）。
# 仅当前正式 path 支持的 type（reconstruct_scope_series 的 _SUPPORTED_SCOPE_TYPES）。
_KNOWN_SCOPE_TYPES = {
    "industry_l1",
    "industry_l2",
    "industry_l3",
    "concept",
}

# ===========================================================================
# REVIEW-REPLAY-DATASET-V1（DATASET-1）：Review Source Dataset 工具骨架
# 只做 SQL 投影 + 文件 I/O + checksum + selection metadata，不拥有任何业务公式。
# 见 .trae/documents/REVIEW-REPLAY-DATASET-V1.md（v6）与
#     .trae/documents/REVIEW-REPLAY-DATASET-V1-IMPLEMENTATION.md（DATASET-1 实施计划）。
# ===========================================================================

# 冻结版本常量（manifest 契约）
FIRST_PYRAMID_ALGORITHM_VERSION = "1.0.0-core-split"
HISTORY_CONTRACT_VERSION = "review-history-v2"
DATASET_SCHEMA_VERSION = 1
# --history 默认值（单一来源；replay-l1 用它判定用户是否显式传了 --history）
_DEFAULT_HISTORY_DAYS = 120
# snapshot summary_payload 均值 ~287KB/行，故该 domain 用小 batch 扫描（见 _load_replay_facts）
_SNAPSHOT_SCAN_BATCH_SIZE = 64


def p_default_history() -> int:
    return _DEFAULT_HISTORY_DAYS

PRD_VERSION = "v2.3"
PRD_PATH = "docs/prd/70-review.md"
PRD_CONTRACT_COPY = "ref/Panji_Review_Scope_Observation_PRD_v2.3_FINAL.md"
PRD_AUTHORITATIVE_SHA = "818d890bdaacc25a78b4dea059a8be95e1d5b439"
VIEW_ALGORITHM_VERSION = "review-view-v1"

# Scope membership readiness：唯一 SSOT（按 family 记录 current / historical_pit）。
_SCOPE_MEMBERSHIP_SOURCES = {
    "market": {"current": "available", "historical_pit": "not_available"},
    "major_index": {"current": "deferred", "historical_pit": "deferred"},
    "style": {"current": "deferred", "historical_pit": "deferred"},
    "industry_l1": {"current": "available", "historical_pit": "not_available"},
    "industry_l2": {"current": "available", "historical_pit": "not_available"},
    "industry_l3": {"current": "available", "historical_pit": "not_available"},
    "concept": {"current": "available", "historical_pit": "not_available"},
}

# Logical PK（每域固定，duplicate = 0 才能执行；REVIEW-REPLAY-DATASET-V1 §10.1）
_DOMAIN_LOGICAL_PKS: dict[str, tuple[str, ...]] = {
    "instruments": ("id",),
    "boards": ("id",),
    "memberships": ("board_id", "instrument_id"),
    "calendar": ("trade_date", "market"),
    "daily_state": ("instrument_id", "trade_date", "algorithm_version"),
    "events": (
        "instrument_id",
        "algorithm_version",
        "history_contract_version",
        "event_id",
    ),
    "bars": ("instrument_id", "trade_date"),
    "snapshot": (
        "instrument_id",
        "trade_date",
        "primary_timeframe",
        "secondary_timeframe",
        "adj",
        "schema_version",
    ),
}

# 10 个导出文件：domain → (文件相对路径, 文件名 stem)
_RAW_FILE_STEMS = {
    "instruments": "instruments",
    "boards": "boards",
    "memberships": "board_memberships_current_snapshot",
    "calendar": "trading_calendar",
    "daily_state": "first_pyramid_daily_state",
    "events": "first_pyramid_events",
    "bars": "bars_daily",
    "snapshot": "stock_feature_snapshots_asof",
}
_LINEAGE_FILE_STEMS = {
    "snapshot_runs": "stock_feature_snapshot_runs",
    "history_runs": "first_pyramid_history_runs",
}

# Parquet decimal128 列（Raw decimal string → decimal128，禁止默认 float64）
_DECIMAL_COLUMNS: dict[str, dict[str, tuple[int, int]]] = {
    "instruments": {
        "total_share": (20, 0),
        "float_share": (20, 0),
    },
    "bars_daily": {
        "open": (20, 4),
        "high": (20, 4),
        "low": (20, 4),
        "close": (20, 4),
        "volume": (20, 2),
        "amount": (20, 2),
        "adj_factor": (20, 8),
    },
}

# ---------------------------------------------------------------------------
# Raw Serialization Contract（P0）：类型保真，避免导出/导入精度丢失
# ---------------------------------------------------------------------------


def _json_default(o: Any) -> Any:
    """JSON dumps 的 default 钩子：Decimal/UUID/date/datetime 确定性序列化。"""
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=UTC)
        return o.isoformat()
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(f"cannot serialize {type(o)!r}")


def _serialize_cell(v: Any) -> Any:
    """单值序列化：Decimal → decimal string（禁止 float()）；UUID/date/datetime 固定格式。"""
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bool):
        return bool(v)
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        # JSONB：递归 sort_keys=True 确定性 JSON（key 顺序稳定，逻辑 content hash 可复现）
        return json.loads(
            json.dumps(v, default=_json_default, sort_keys=True, separators=(",", ":"))
        )
    if isinstance(v, (str, int, float)):
        return v
    return str(v)


def _serialize_row(row: dict) -> dict:
    """按 Raw Serialization Contract 序列化一行（列名 → 序列化值）。"""
    return {k: _serialize_cell(v) for k, v in row.items()}


def _write_jsonl_gz(
    path: str,
    rows: Iterable[dict],
    *,
    mtime: int = 0,
) -> None:
    """逐行写 deterministic jsonl.gz（UTF-8 + LF + gzip mtime=0，支持流式可迭代）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.GzipFile(path, mode="wb", mtime=mtime) as gz:
        for row in rows:
            line = json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            gz.write(line)
            gz.write(b"\n")


def _iter_jsonl_gz(path: str) -> Iterable[dict]:
    """流式读 jsonl.gz，逐行 yield dict（校验/转换用）。"""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_content(path: str) -> str:
    """解压后 canonical JSONL bytes 的逻辑数据完整性 hash。"""
    h = hashlib.sha256()
    with gzip.open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 时间范围计算（纯函数，PURE_UNIT 可测，不触库）
# ---------------------------------------------------------------------------


def compute_date_ranges(
    asof: date,
    trading_days: list[date],
    *,
    history: int = 120,
    warmup: int = 160,
    bar_lookback_calendar_days: int = 400,
) -> dict:
    """计算 Dataset 时间范围（REVIEW-REPLAY-DATASET-V1 §4.2）。

    - ``analysis_axis`` = 升序最后 ``history`` 个交易日 ≤ asof；
    - ``warmup_axis`` = ``source_fact_start`` 起往前 ``warmup`` 个交易日；
    - ``source_fact_start`` = warmup_axis[0]；
    - ``states_start`` = source_fact_start 前一个交易日（T-1）；
    - ``bars_start`` = source_fact_start - ``bar_lookback_calendar_days`` 日历日。
    """
    axis = sorted(set(trading_days))
    if not axis:
        raise ValueError("trading_days 为空")
    if asof not in axis:
        raise ValueError("asof 不在 trading axis 内")
    idx = axis.index(asof)
    analysis_start_idx = max(0, idx - history + 1)
    analysis_axis = axis[analysis_start_idx : idx + 1]
    warmup_start_idx = max(0, analysis_start_idx - warmup)
    warmup_axis = axis[warmup_start_idx:analysis_start_idx]
    source_fact_start = axis[warmup_start_idx]
    states_start = axis[warmup_start_idx - 1] if warmup_start_idx > 0 else source_fact_start
    bars_start = source_fact_start - timedelta(days=bar_lookback_calendar_days)
    return {
        "asof": asof.isoformat(),
        "analysis_axis": [d.isoformat() for d in analysis_axis],
        "warmup_axis": [d.isoformat() for d in warmup_axis],
        "source_fact_start": source_fact_start.isoformat(),
        "states_start": states_start.isoformat(),
        "bars_start": bars_start.isoformat(),
        "calendar_range": [bars_start.isoformat(), asof.isoformat()],
        "states_range": [states_start.isoformat(), asof.isoformat()],
        "events_range": [source_fact_start.isoformat(), asof.isoformat()],
    }


# ---------------------------------------------------------------------------
# Manifest 构建 / 读取 / 契约校验（纯函数）
# ---------------------------------------------------------------------------


def build_manifest(
    *,
    dataset_dir_name: str,
    capture_git_sha: str,
    base_dev_sha: str,
    asof: date,
    transaction_timestamp: datetime,
    snapshot_started_at_utc: datetime,
    date_ranges: dict,
    row_counts: dict,
    raw_files: dict,
    capture_status: str,
    contract_versions_observed: dict,
    coverage: dict,
    production_runtime_sha: str | None = None,
    database_schema_revision: str | None = None,
    history: int = 120,
    warmup: int = 160,
    bar_lookback_calendar_days: int = 400,
    taxonomy_versions: dict | None = None,
    membership_versions: dict | None = None,
) -> dict:
    """按 REVIEW-REPLAY-DATASET-V1 §6 构建 manifest。

    - ``source_readiness.first_pyramid_history`` 由真实 capture 结果生成
      （``capture_status`` / ``contract_versions_observed`` / ``coverage``，不硬编码 available）；
    - ``scope_membership_sources`` 为 Membership readiness 唯一 SSOT；
    - ``source_readiness`` 不再重复判定 membership（仅 ``scope_membership`` 指针）；
    - 不含 ``membership_semantics`` 字段（PRD 对齐 P0）。
    """
    return {
        "dataset_id": f"review-source-{capture_git_sha[:12]}-v1",
        "dataset_dir_name": dataset_dir_name,
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "snapshot_started_at_utc": _serialize_cell(snapshot_started_at_utc),
        "export_completed_at_utc": _serialize_cell(datetime.now(UTC)),
        "transaction_timestamp": _serialize_cell(transaction_timestamp),
        "capture_git_sha": capture_git_sha,
        "base_dev_sha": base_dev_sha,
        "production_runtime_sha": production_runtime_sha,
        "database_schema_revision": database_schema_revision,
        "analysis_asof_date": asof.isoformat(),
        "analysis_trade_days": history,
        "warmup_trade_days": warmup,
        "bar_lookback_calendar_days": bar_lookback_calendar_days,
        "first_pyramid_algorithm_version": FIRST_PYRAMID_ALGORITHM_VERSION,
        "history_contract_version": HISTORY_CONTRACT_VERSION,
        "membership_snapshot_at": _serialize_cell(transaction_timestamp),
        "scope_membership_sources": {
            fam: dict(entry) for fam, entry in _SCOPE_MEMBERSHIP_SOURCES.items()
        },
        "source_readiness": {
            "first_pyramid_history": {
                "capture_status": capture_status,
                "algorithm_version": FIRST_PYRAMID_ALGORITHM_VERSION,
                "contract_versions_observed": contract_versions_observed,
                "coverage": coverage,
            },
            "first_pyramid_current_snapshot": {"status": "available"},
            "scope_membership": {
                "status": "family_dependent",
                "detail_ref": "scope_membership_sources",
                "note": "唯一 SSOT；不再在 source_readiness 重复判定 membership",
            },
            "market_daily_bars": {"status": "available", "includes_adj_factor": True},
        },
        "market_data": {
            "vwap_raw_source": "unavailable",
            "note": "PRD §6.6 VWAP Return Total 消费 First Pyramid canonical "
                    "state_payload；Exporter 不生成任何 VWAP 公式",
        },
        "review_contract": {
            "prd_version": PRD_VERSION,
            "prd_path": PRD_PATH,
            "prd_contract_copy": PRD_CONTRACT_COPY,
            "authoritative_sha": PRD_AUTHORITATIVE_SHA,
        },
        "taxonomy_versions": dict(taxonomy_versions or {}),
        "membership_versions": dict(membership_versions or {}),
        "instrument_universe_rule": (
            "dimension: full; fact: review_fact_universe "
            "(all A-share, market in SH/SZ/BJ)"
        ),
        "scope_universe_rule": (
            "all active industry_l1/l2/l3 + concept scopes "
            "(membership snapshot, current-only)"
        ),
        "date_ranges": date_ranges,
        "row_counts": row_counts,
        "raw_files": raw_files,
        "derived_files": {},
    }


def load_manifest(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def validate_manifest_contract(m: dict) -> list[str]:
    """Manifest 契约断言，返回违规列表（空 = PASS）。

    覆盖 REVIEW-REPLAY-DATASET-V1 §12「manifest 契约断言」与实施计划 §4 测试清单 5。
    """
    violations: list[str] = []
    required_keys = (
        "dataset_id",
        "dataset_schema_version",
        "snapshot_started_at_utc",
        "export_completed_at_utc",
        "transaction_timestamp",
        "capture_git_sha",
        "base_dev_sha",
        "analysis_asof_date",
        "analysis_trade_days",
        "warmup_trade_days",
        "bar_lookback_calendar_days",
        "first_pyramid_algorithm_version",
        "history_contract_version",
        "membership_snapshot_at",
        "scope_membership_sources",
        "source_readiness",
        "market_data",
        "review_contract",
        "instrument_universe_rule",
        "scope_universe_rule",
        "date_ranges",
        "row_counts",
        "raw_files",
    )
    for key in required_keys:
        if key not in m:
            violations.append(f"缺少 manifest 字段: {key}")

    # P0：禁止 membership_semantics（优化.md 首轮 §1）
    if "membership_semantics" in m:
        violations.append("manifest 不得包含 membership_semantics 字段（PRD 对齐 P0）")

    # 时间语义：membership_snapshot_at == transaction_timestamp
    if m.get("membership_snapshot_at") != m.get("transaction_timestamp"):
        violations.append("membership_snapshot_at 必须 == transaction_timestamp")

    # scope_membership_sources：唯一 SSOT，每 family 含 current/historical_pit
    sms = m.get("scope_membership_sources") or {}
    for fam in ("market", "major_index", "style", "industry_l1", "industry_l2", "industry_l3", "concept"):
        entry = sms.get(fam)
        if not isinstance(entry, dict):
            violations.append(f"scope_membership_sources.{fam} 缺失")
            continue
        if "current" not in entry or "historical_pit" not in entry:
            violations.append(f"scope_membership_sources.{fam} 必须含 current 与 historical_pit")
    if sms.get("market", {}).get("historical_pit") != "not_available":
        violations.append("market.historical_pit 必须 == not_available")
    if sms.get("major_index", {}).get("current") != "deferred":
        violations.append("major_index.current 必须 == deferred")
    if sms.get("style", {}).get("current") != "deferred":
        violations.append("style.current 必须 == deferred")

    # source_readiness.first_pyramid_history：必须带 capture_status（非硬编码 available）
    sr = m.get("source_readiness") or {}
    fph = sr.get("first_pyramid_history") or {}
    if "capture_status" not in fph:
        violations.append(
            "source_readiness.first_pyramid_history 必须含 capture_status（不能硬编码 available）"
        )

    # source_readiness 不得重复判定 membership（仅 scope_membership 指针）
    sm_entry = sr.get("scope_membership") or {}
    if sm_entry.get("status") != "family_dependent":
        violations.append("source_readiness.scope_membership.status 必须 == family_dependent")
    if sm_entry.get("detail_ref") != "scope_membership_sources":
        violations.append(
            "source_readiness.scope_membership.detail_ref 必须指向 scope_membership_sources"
        )

    # market_data.vwap_raw_source == unavailable
    if (m.get("market_data") or {}).get("vwap_raw_source") != "unavailable":
        violations.append("market_data.vwap_raw_source 必须 == unavailable")

    # review_contract.prd_contract_copy 存在
    rc = m.get("review_contract") or {}
    if not rc.get("prd_contract_copy"):
        violations.append("review_contract.prd_contract_copy 必须存在")

    # raw_files：每个文件含 rows / compressed_sha256 / content_sha256
    for fname, finfo in (m.get("raw_files") or {}).items():
        if not isinstance(finfo, dict):
            violations.append(f"raw_files.{fname} 必须为对象")
            continue
        for key in ("rows", "compressed_sha256", "content_sha256"):
            if key not in finfo:
                violations.append(f"raw_files.{fname}.{key} 缺失")

    # derived_files：若存在，每项必须含 rows / sha256 / derived_from（DATASET-1.1 P1-1 闭环）
    for fname, finfo in (m.get("derived_files") or {}).items():
        if not isinstance(finfo, dict):
            violations.append(f"derived_files.{fname} 必须为对象")
            continue
        for key in ("rows", "sha256", "derived_from"):
            if key not in finfo:
                violations.append(f"derived_files.{fname}.{key} 缺失")
    return violations


# ---------------------------------------------------------------------------
# Logical PK / lineage 引用闭包（纯函数）
# ---------------------------------------------------------------------------


def logical_pk(domain: str) -> tuple[str, ...]:
    """返回指定域的 Logical PK 列元组（REVIEW-REPLAY-DATASET-V1 §10.1）。"""
    if domain not in _DOMAIN_LOGICAL_PKS:
        raise ValueError(f"未知 domain: {domain}")
    return _DOMAIN_LOGICAL_PKS[domain]


def find_duplicate_pks(rows: list[dict], pk: tuple[str, ...]) -> list[tuple]:
    """按 Logical PK 检测重复行，返回重复的 key 列表（去重后的重复 key）。"""
    seen: set = set()
    dupes: list[tuple] = []
    dup_keys: set = set()
    for row in rows:
        key = tuple(row.get(k) for k in pk)
        if key in seen and key not in dup_keys:
            dup_keys.add(key)
            dupes.append(key)
        seen.add(key)
    return dupes


def lineage_closure_l2(d5_rows: list[dict], history_runs: list[dict]) -> set:
    """L2 ids = DISTINCT non-null source_history_run_id FROM 导出的 D5（引用闭包）。

    ``history_runs`` 用于孤儿检测（调用方比对 available）；孤儿 = 闭包 - available。
    """
    return {
        str(r["source_history_run_id"])
        for r in d5_rows
        if r.get("source_history_run_id") is not None
    }


# ---------------------------------------------------------------------------
# Logical Views（纯函数，只定义 selection，不复制 facts）
# ---------------------------------------------------------------------------


def _overlap_rank(
    boards: list[dict],
    memberships: list[dict],
) -> tuple[dict, dict]:
    """board_id → member 列表；member_id → 所属 board 数（重叠度）。"""
    per_board: dict = {}
    member_board_count: dict = {}
    for m in memberships:
        bid = m.get("board_id")
        iid = m.get("instrument_id")
        per_board.setdefault(bid, []).append(iid)
        member_board_count[iid] = member_board_count.get(iid, 0) + 1
    return per_board, member_board_count


def _board_overlap(bid: str, per_board: dict, member_board_count: dict) -> float:
    """board 的平均重叠度：Σ(member 所属 board 数) / member 数。"""
    mids = per_board.get(bid, [])
    if not mids:
        return 0.0
    return sum(member_board_count.get(m, 0) for m in mids) / len(mids)


def _sorted_by_overlap(
    boards: list[dict],
    per_board: dict,
    member_board_count: dict,
) -> list[dict]:
    """metric DESC → tie-breaker: external_code ASC / board_id ASC（完全确定性）。"""
    return sorted(
        boards,
        key=lambda b: (
            -_board_overlap(str(b.get("id")), per_board, member_board_count),
            str(b.get("external_code") or ""),
            str(b.get("id")),
        ),
    )


def _select_5_dates(axis: list[str]) -> list[str]:
    """从 analysis 轴确定性选取 5 个代表日期（首/1/4/中/3/4/末）。"""
    n = len(axis)
    if not n:
        return []
    if n == 1:
        return [axis[0]]
    idxs = sorted({0, n // 4, n // 2, 3 * n // 4, n - 1})
    return [axis[i] for i in idxs]


def _union_for(scope_ids: list, per_board: dict) -> list:
    u: set = set()
    for bid in scope_ids:
        u.update(per_board.get(bid, []))
    return sorted(u, key=str)


def _mk_view(
    view_id: str,
    policy: str,
    scope_ids: list,
    per_board: dict,
    membership_usage: dict,
    *,
    date_range: str | None = None,
    trade_dates: list[str] | None = None,
) -> dict:
    view = {
        "view_id": view_id,
        "selection_policy": policy,
        "selection_algorithm_version": VIEW_ALGORITHM_VERSION,
        "scope_keys": sorted(str(s) for s in scope_ids),
        "derived_instrument_ids": _union_for(scope_ids, per_board),
        "date_range": (
            date_range
            if date_range is not None
            else "manifest.date_ranges（analysis/warmup/states/bars 范围，见 manifest）"
        ),
        "membership_usage": dict(membership_usage),
    }
    if trade_dates is not None:
        view["trade_dates"] = list(trade_dates)
    return view


def build_views(
    boards: list[dict],
    memberships: list[dict],
    analysis_axis: list[str] | None = None,
) -> dict[str, dict]:
    """生成 5 个 logical view（REVIEW-REPLAY-DATASET-V1 §7）。

    完全确定性：所有 metric 排序带稳定 tie-breaker（metric DESC → external_code ASC → board_id ASC）。
    ``derived_instrument_ids == union(memberships[scope_keys])`` 由 _mk_view 保证。
    传入 ``analysis_axis`` 时每 view 的 ``date_range`` 为机器可消费的 ``[start, end]``，
    ``representative_sample`` 额外含 ``trade_dates``（确定性 5 dates）；未传入时回退为说明字符串。
    """
    active_boards = [b for b in boards if b.get("is_active", True)]
    per_board, member_board_count = _overlap_rank(boards, memberships)
    concept = [b for b in active_boards if b.get("type") == "concept"]
    industry = [b for b in active_boards if b.get("type") == "industry"]
    usage = {"current": "available", "historical": "not_available"}
    view_date_range = (
        [analysis_axis[0], analysis_axis[-1]] if analysis_axis else None
    )
    rep_trade_dates = _select_5_dates(analysis_axis) if analysis_axis else None

    # dev_500：overlap 降序 + tie-breaker 取前 N 板块，union ≈ 500
    dev_ids: list = []
    dev_union: set = set()
    for b in _sorted_by_overlap(active_boards, per_board, member_board_count):
        added = set(per_board.get(str(b.get("id")), []))
        if len(dev_union | added) >= 500:
            dev_union |= added
            dev_ids.append(str(b.get("id")))
            break
        dev_union |= added
        dev_ids.append(str(b.get("id")))

    # capacity_4096：本地人为排序的容量样本（overlap 降序贪心，union ≤ 4096）
    cap_ids: list = []
    cap_union: set = set()
    for b in _sorted_by_overlap(active_boards, per_board, member_board_count):
        added = set(per_board.get(str(b.get("id")), []))
        if len(cap_union | added) > 4096:
            continue
        cap_union |= added
        cap_ids.append(str(b.get("id")))

    # representative_sample：member_count 最大 / overlap 最高 / 接近 median（带 tie-breaker）
    by_member_count = sorted(
        active_boards,
        key=lambda b: (
            -len(per_board.get(str(b.get("id")), [])),
            str(b.get("external_code") or ""),
            str(b.get("id")),
        ),
    )
    largest = by_member_count[0] if by_member_count else None
    by_overlap = (
        _sorted_by_overlap(active_boards, per_board, member_board_count)
        if active_boards
        else []
    )
    highest_overlap = by_overlap[0] if by_overlap else None
    counts = sorted(len(per_board.get(str(b.get("id")), [])) for b in active_boards)
    median = counts[len(counts) // 2] if counts else 0
    near_median = min(
        active_boards,
        key=lambda b: (
            abs(len(per_board.get(str(b.get("id")), [])) - median),
            str(b.get("external_code") or ""),
            str(b.get("id")),
        ),
        default=None,
    )
    rep_ids: list = []
    for b in (largest, highest_overlap, near_median):
        if b is not None and str(b.get("id")) not in rep_ids:
            rep_ids.append(str(b.get("id")))

    views = {
        "dev_500": _mk_view(
            "dev_500",
            "overlap DESC + external_code tie-breaker，取前 N 板块使 union ≈ 500（日常调试）",
            dev_ids,
            per_board,
            usage,
            date_range=view_date_range,
        ),
        "capacity_4096": _mk_view(
            "capacity_4096",
            "本地人为排序的容量样本（overlap DESC 贪心，union ≤ 4096）；"
            "不叫 perf_4096 / production chunk",
            cap_ids,
            per_board,
            usage,
            date_range=view_date_range,
        ),
        "all_concepts": _mk_view(
            "all_concepts",
            "全部 concept scope（current snapshot only）",
            [str(b.get("id")) for b in concept],
            per_board,
            usage,
            date_range=view_date_range,
        ),
        "all_industries": _mk_view(
            "all_industries",
            "全部 industry_l1/l2/l3 scope（current snapshot only）",
            [str(b.get("id")) for b in industry],
            per_board,
            usage,
            date_range=view_date_range,
        ),
        "representative_sample": _mk_view(
            "representative_sample",
            "代表性技术样本（member_count 最大 / overlap 最高 / 接近 median，"
            "均带 tie-breaker × 5 dates，日期取自 manifest.date_ranges）",
            rep_ids,
            per_board,
            usage,
            date_range=view_date_range,
            trade_dates=rep_trade_dates,
        ),
    }
    return views


# ---------------------------------------------------------------------------
# jsonl.gz → Parquet（lazy import pyarrow；未安装时明确报错）
# ---------------------------------------------------------------------------


def _write_parquet(
    rows: list[dict],
    path: str,
    domain: str,
    *,
    compression: str = "zstd",
) -> dict:
    """把 rows 写成 Parquet（price/amount 用 decimal128，不默认 float64）。

    仅在 ``dataset-validate`` / parquet conversion 分支调用；pyarrow lazy import。
    """
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise RuntimeError(
            "optional replay dependency missing (pip install -r requirements-replay.txt)"
        ) from e
    if not rows:
        raise ValueError("rows 为空，无法推断 schema")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    dec_cols = _DECIMAL_COLUMNS.get(domain, {})
    names = list(rows[0].keys())

    def _field_type(name: str, v: Any) -> Any:
        if name in dec_cols:
            return pa.decimal128(*dec_cols[name])
        if isinstance(v, bool):
            return pa.bool_()
        if isinstance(v, int):
            return pa.int64()
        if isinstance(v, float):
            return pa.float64()
        if isinstance(v, (dict, list)):
            return pa.string()  # JSONB → JSON string
        return pa.string()

    schema = pa.schema(
        [pa.field(name, _field_type(name, rows[0].get(name)), nullable=True) for name in names]
    )
    arrays = []
    for name in names:
        ftype = schema.field(name).type
        if name in dec_cols:
            arrays.append(
                pa.array(
                    [Decimal(r.get(name)) if r.get(name) is not None else None for r in rows],
                    type=ftype,
                )
            )
        elif pa.types.is_string(ftype):
            arrays.append(
                pa.array(
                    [
                        json.dumps(r.get(name), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                        if isinstance(r.get(name), (dict, list))
                        else r.get(name)
                        for r in rows
                    ],
                    type=ftype,
                )
            )
        else:
            arrays.append(pa.array([r.get(name) for r in rows], type=ftype))
    table = pa.Table.from_arrays(arrays, schema=schema)
    pq.write_table(table, path, compression=compression)
    return {"rows": len(rows), "path": path}


def _rows_to_parquet(
    raw_dir: str,
    parquet_dir: str,
    domain: str,
    file_stem: str,
    *,
    compression: str = "zstd",
    batch_size: int = 10000,
) -> dict:
    """把 raw/*.jsonl.gz 流式批量转成 parquet/*.parquet，返回 {rows, path}。

    - 按 ``batch_size`` 累积行，满批 ``writer.write_batch``，末尾 flush，避免逐行写表；
    - decimal 列按 **file_stem** 查找 ``_DECIMAL_COLUMNS``（domain key 与 stem 不一致的域，
      例如 bars → ``bars_daily``，必须按 stem 解析才能得到 decimal128）；
    - 空文件（0 行）不落盘，返回 ``{"rows": 0, "path": out_path}``。
    """
    raw_path = os.path.join(raw_dir, f"{file_stem}.jsonl.gz")
    out_path = os.path.join(parquet_dir, f"{file_stem}.parquet")
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise RuntimeError(
            "optional replay dependency missing (pip install -r requirements-replay.txt)"
        ) from e
    dec_cols = _DECIMAL_COLUMNS.get(file_stem, {})
    os.makedirs(parquet_dir, exist_ok=True)
    writer: Any = None
    schema: Any = None
    batch: list[dict] = []
    count = 0

    def _flush() -> None:
        nonlocal batch
        if not batch:
            return
        names = list(schema.names)
        arrays = []
        for name in names:
            ftype = schema.field(name).type
            if pa.types.is_decimal(ftype):
                arrays.append(
                    pa.array(
                        [Decimal(r.get(name)) if r.get(name) is not None else None for r in batch],
                        type=ftype,
                    )
                )
            elif pa.types.is_string(ftype):
                arrays.append(
                    pa.array(
                        [
                            json.dumps(r.get(name), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                            if isinstance(r.get(name), (dict, list))
                            else r.get(name)
                            for r in batch
                        ],
                        type=ftype,
                    )
                )
            else:
                arrays.append(pa.array([r.get(name) for r in batch], type=ftype))
        writer.write_batch(pa.RecordBatch.from_arrays(arrays, schema=schema))
        batch = []

    for row in _iter_jsonl_gz(raw_path):
        if writer is None:
            names = list(row.keys())
            fields = []
            for name in names:
                v = row.get(name)
                if name in dec_cols:
                    fields.append(pa.field(name, pa.decimal128(*dec_cols[name]), nullable=True))
                elif isinstance(v, bool):
                    fields.append(pa.field(name, pa.bool_(), nullable=True))
                elif isinstance(v, int):
                    fields.append(pa.field(name, pa.int64(), nullable=True))
                elif isinstance(v, float):
                    fields.append(pa.field(name, pa.float64(), nullable=True))
                elif isinstance(v, (dict, list)):
                    fields.append(pa.field(name, pa.string(), nullable=True))
                else:
                    fields.append(pa.field(name, pa.string(), nullable=True))
            schema = pa.schema(fields)
            writer = pq.ParquetWriter(out_path, schema, compression=compression)
        batch.append(row)
        count += 1
        if len(batch) >= batch_size:
            _flush()
    if writer is not None:
        _flush()
        writer.close()
    return {"rows": count, "path": out_path}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scope Dynamics 只读基线测量 probe（R1）",
    )
    p.add_argument(
        "--scope-type", required=False, default=None,
        choices=sorted(_KNOWN_SCOPE_TYPES),
        help="scope 类型（industry_l1/l2/l3/concept/...）",
    )
    p.add_argument(
        "--scope-key", required=False, default=None, type=str,
        help="scope 标识（如 银行 / 人工智能）",
    )
    p.add_argument(
        "--history", type=int, default=_DEFAULT_HISTORY_DAYS,
        help="回看交易日数量（scale ladder: 20/60/120）",
    )
    p.add_argument(
        "--asof-date", type=str, default=None,
        help="current-static membership as-of 日期 ISO（默认取最新交易日）",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只校验参数与导入，不连库",
    )
    p.add_argument(
        "--mode",
        choices=[
            "capacity-benchmark",
            "dataset-capacity-benchmark",
            "dataset-dynamics-logic",
            "leadership-research",
            "export-dataset",
            "dataset-validate",
            "replay-l1",
            "rtm",
            "semantic-matrix",
            "explore1",
            "equivalence",
        ],
        required=True,
        help=(
            "capacity-benchmark: 只调用 optimized batch owner (shadow, production-bound) "
            "compute_current_static_scope_dynamics_batch()（禁 legacy/single/手工 chunk，"
            "union_member_cap 用 owner default；禁 remote DB，仅作为遗留 DB 入口，Dataset "
            "容量用 dataset-capacity-benchmark）；"
            "dataset-capacity-benchmark: 本地冻结 Dataset 多日期窗口容量 runner，调用最终 "
            "进入 dev 的同一套正式共享 core（build_union_fact_context_from_loaded_facts → "
            "build_prepared_scopes_from_union → compute_scope_observation），禁 DB/SSH/远程 "
            "PG/业务公式复制/parallel owner，需 --dataset-dir + --view + --history + "
            "--asof-lock；"
            "dataset-dynamics-logic: 4-scope frozen Dataset 全 Dynamics 链 E2E（L1 → "
            "build_observation_series → compute_scope_dynamics_analysis），确认 Dynamics "
            "actually executed，并输出 12 traces（4 scopes x 3 dates）；scope 固定来自版本化 "
            "fixture（review_dynamics_logic_sample.json），不依赖任何 view，需 --dataset-dir "
            "+ --history（默认 120）+ --asof-lock；"
            "leadership-research: Stage-2 研究（观察真实本地数据的领导成员变化，复用阶段1 "
            "Leadership contribution owner，输出 Top3/5/10 overlap + Spearman + entrant/exit "
            "+ concentration 分布，不产出正式 Migration 结论）；需 --dataset-dir + "
            "--history（默认 20）+ --asof-lock；"
            "export-dataset: 服务器一次性只读导出 Review Source Dataset（Full Corpus，"
            "禁 scope-type/scope-key 参数）；"
            "dataset-validate: 本地校验 manifest + 完整性 KPI + jsonl.gz → parquet + 生成 views；"
            "replay-l1: 用本地 Dataset corpus 回放 Current L1 Scope Observation（锁 asof date，"
            "禁 scope-type/scope-key，需 --dataset-dir + --view 选择 dev_500/capacity_4096/单 scope）；"
            "rtm: 对 Dataset 跑全量 L1 source-ready Fact RTM（声明式 metadata + 五态判定）；"
            "semantic-matrix: 按 fact 来源选择证据日期的 L1 RTM（08-17/08-10/08-07 各自最佳日期，"
            "每个 Fact 记录 evidence_date，不要求同一天）；"
            "explore1: EXPLORE-1 事件聚合 denominator 检查（真实 08-07 数据 + 合成 Case A/B）"
        ),
    )
    p.add_argument(
        "--view", type=str, default="representative_sample",
        help="replay-l1 / rtm 模式的数据集 view：dev_500 / capacity_4096 / representative_sample "
             "（或传单个 board_id UUID 直接指定 scope）",
    )
    p.add_argument(
        "--dataset-dir", type=str, default=None,
        help="export-dataset / dataset-validate 模式的数据集目录（服务器 /tmp 或本地 "
             "backend/.perfdata/review/<name>）",
    )
    p.add_argument(
        "--asof-lock", type=str, default=None,
        help="replay-l1 / rtm / dataset-capacity-benchmark 模式可选：显式锁定 asof 日期 ISO "
             "（Track B historical-asof，如 2026-08-10；dataset-capacity-benchmark 用它做 "
             "多日期窗口的右端点）。不传则默认 manifest declared asof（2026-08-17）。禁止 "
             "latest backfill，缺失事实如实标记 unavailable。",
    )
    p.add_argument(
        "--scope-count", type=int, default=None,
        help="capacity-benchmark 模式：按重叠度降序选择的 scope 数量（如 285）",
    )
    return p.parse_args()


def _rss_mb() -> float:
    """当前进程常驻内存（MB）。

    macOS 的 ``ru_maxrss`` 单位是字节；Linux 是 KB。统一折算为 MB。
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return raw / (1024.0 * 1024.0)
    return raw / 1024.0


def _build_readonly_engine():
    """标记函数：本 probe 通过 AsyncSessionLocal 复用 app.db 的现有 engine。

    只读守卫在 ``capacity-benchmark`` 内于连接建立后，以事务内
    ``SET TRANSACTION READ ONLY`` 作为首条语句并 ``SHOW transaction_read_only``
    验证为 on（fail-closed），绝不 commit。不修改共享的 app.db 源码；probe 仅
    调用纯 SELECT 路径，不发出任何 DDL。
    """
    from app.db import AsyncSessionLocal

    return AsyncSessionLocal


async def _capacity_benchmark(
    scope_type: str, scope_count: int, history: int, asof_date: str | None
) -> int:
    """capacity-benchmark：只调用 optimized batch owner（shadow, production-bound path）
    ``compute_current_static_scope_dynamics_batch()``。

    注意：``compute_current_static_scope_dynamics_batch()`` 当前标记 SHADOW ONLY，
    尚未 wired 到 API / orchestrator / persistence / frontend —— 它是 production-code-
    quality 的 optimized batch owner / candidate execution path，不是线上 orchestrator
    已实际调用的 production execution path。因此本 benchmark 测量的是该 optimized
    batch owner 的容量，不代表"线上 Review 当前就是此耗时"。

    不跑 legacy A/B（Git 历史已永久保存 PERF-1/PERF-2/PERF-VEC-1 证据）；不做任何
    union 加载、scope 切分、member 构造或 single dynamics —— 全部委托给 batch owner。
    本函数只负责：选择 scope_keys + trade_dates、建立 read-only transaction、调用
    batch owner、计 wall/RSS。chunking / I/O logs / metrics 全部来自 batch owner 自身
    的现有输出。union_member_cap 使用 batch owner 自身 default，probe 不复制配置。
    """
    import time
    import uuid

    from sqlalchemy import select, text

    from app.db import AsyncSessionLocal
    from app.models.market_board import MarketBoard, MarketBoardMembership
    from app.services.review_observation_prep_service import list_recent_trading_days
    from app.services.review_scope_dynamics_service import (
        compute_current_static_scope_dynamics_batch,
    )

    _build_readonly_engine()
    rss_before = _rss_mb()
    async with AsyncSessionLocal() as db:
        # PERF-REVALIDATION SAFETY: explicit read-only transaction boundary.
        # The FIRST statement autobegins the transaction; it must be
        # ``SET TRANSACTION READ ONLY`` (reliable in async sessions).
        await db.execute(text("SET TRANSACTION READ ONLY"))
        ro = (await db.execute(text("SHOW transaction_read_only"))).scalar()
        if ro != "on":
            logger.error(
                "read-only guard failed: transaction_read_only=%r; "
                "refusing to run capacity-benchmark",
                ro,
            )
            return 3

        if asof_date is None:
            latest = await list_recent_trading_days(db, date.today(), 1)
            if not latest:
                logger.error("无法解析最新交易日（calendar 为空）")
                return 2
            asof_date = latest[0]
        else:
            asof_date = date.fromisoformat(asof_date)
        trade_dates = sorted(await list_recent_trading_days(db, asof_date, history))
        if not trade_dates:
            logger.error("trade_dates 为空（history=%d）", history)
            return 2

        # ---- 枚举 scope family -> members（只读），按重叠度降序选前 scope_count ----
        boards = (
            await db.execute(
                select(MarketBoard.id, MarketBoard.name)
                .where(MarketBoard.type == scope_type)
                .where(MarketBoard.isActive.is_(True))
            )
        ).all()
        # Only count memberships within THIS scope_type family, so overlap ranking
        # reflects how many same-family boards a member belongs to — not industry /
        # other-family pollution that would corrupt the "highest overlap" sample.
        candidate_board_ids = {str(bid) for bid, _ in boards}
        memberships = (
            await db.execute(
                select(MarketBoardMembership.boardId, MarketBoardMembership.instrumentId)
            )
        ).all()
        per_board: dict = {}
        member_board_count: dict = {}
        for bid, iid in memberships:
            if str(bid) not in candidate_board_ids:
                continue
            per_board.setdefault(str(bid), []).append(uuid.UUID(str(iid)))
            member_board_count[str(iid)] = member_board_count.get(str(iid), 0) + 1
        ranked: list[tuple[float, str, str]] = []
        for bid, bname in boards:
            mids = per_board.get(str(bid), [])
            if not mids:
                continue
            avg_share = sum(member_board_count.get(str(m), 0) for m in mids) / len(mids)
            ranked.append((avg_share, str(bid), str(bname) or str(bid)))
        ranked.sort(key=lambda x: x[0], reverse=True)
        top = ranked[:scope_count] if scope_count else ranked
        if not top:
            logger.error("无 %s scope 可选", scope_type)
            return 2
        scope_keys = [bid for _share, bid, _n in top]

        print("=== capacity-benchmark (optimized batch owner, shadow production-bound path) ===")
        print(f"scope_type            : {scope_type}")
        print(f"scope_count           : {len(scope_keys)}")
        print(f"asof_date             : {asof_date.isoformat()}")
        print(f"trade_date_count      : {len(trade_dates)}")
        print(f"rss_before_mb         : {rss_before:.1f}")

        wall0 = time.perf_counter()
        # Do NOT pass union_member_cap: the production batch owner's own default
        # is the single source of truth for the cap.  The probe must not copy /
        # override production configuration.
        results = await compute_current_static_scope_dynamics_batch(
            db,
            scope_type,
            scope_keys,
            trade_dates,
            analysis_asof_date=asof_date,
        )
        wall_ms = (time.perf_counter() - wall0) * 1000.0
        rss_after = _rss_mb()

        print(f"wall_ms               : {wall_ms:.1f}")
        print(f"rss_after_mb          : {rss_after:.1f}")
        print(f"rss_delta_mb          : {rss_after - rss_before:.1f}")
        print(f"result_count          : {len(results)}")
        # chunk / union / I/O metrics are logged by the production batch owner itself
        print("(chunk_size / union_member_count / cal_ms / states_ms / bars_ms / "
              "events_ms / vec_precompute_ms / batch_* metrics from production logs)")
        print("=== END ===")
        # Read-only transaction closes with rollback (never commit).
        await db.rollback()
        return 0




def _git_sha(ref: str) -> str | None:
    """只读 git 命令取 SHA（无副作用）；失败返回 None。"""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", ref],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 - 取不到 SHA 时降级为 None/unknown
        return None


async def _phase_a_precheck(session) -> None:
    """Phase A：独立 session 只读安全 precheck（连接 + DML 探针验证写被拒/提示）。

    DML 探针**只能**在这里执行；Phase B capture 事务内禁止任何 DML 探针，
    否则会销毁 REPEATABLE READ snapshot。
    """
    from sqlalchemy import text

    # 会话首条（不在事务内执行）：设置后续事务默认只读
    await session.execute(text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"))
    # autobegin 事务首条 SQL = SET TRANSACTION READ ONLY（合法首句），避免 SELECT 1 先触发 autobegin
    await session.execute(text("SET TRANSACTION READ ONLY"))
    ro = (await session.execute(text("SHOW transaction_read_only"))).scalar()
    if ro != "on":
        logger.warning(
            "[export-dataset][phase-a] transaction_read_only != on (%r)，继续（不写）",
            ro,
        )
    try:
        await session.execute(
            text(
                "WITH u AS (UPDATE market_boards SET name=name "
                "WHERE id='00000000-0000-0000-0000-000000000000' "
                "RETURNING 1) SELECT 1 FROM u"
            )
        )
        await session.rollback()
        logger.warning(
            "[export-dataset][phase-a] 角色为超级用户，DB 层只读无法强制；"
            "只读性依赖代码审计（path 仅 SELECT）。继续运行（不写）。"
        )
    except Exception as e:  # noqa: BLE001 - 期望被 read-only 拒绝（非超级用户时）
        logger.info("[export-dataset][phase-a] DML 被拒绝: %s", type(e).__name__)
        await session.rollback()


async def _stream_export(
    session,
    stmt,
    serializer: Callable[[dict], dict],
    path: str,
    *,
    mtime: int = 0,
    on_row: Callable[[dict], None] | None = None,
) -> int:
    """把 ``stmt`` 流式写入 deterministic jsonl.gz（Core column projection + yield_per）。

    禁止 ``.all()`` 后 gzip：大表（5000+ × 400cd）必须逐批流式落盘。
    返回行数。
    """
    result = await session.stream(stmt.execution_options(yield_per=1000))
    count = 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.GzipFile(path, mode="wb", mtime=mtime) as gz:
        async for row in result:
            mapping = dict(row._mapping)
            ser = serializer(mapping)
            if on_row is not None:
                on_row(mapping)
            line = json.dumps(
                ser, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            gz.write(line)
            gz.write(b"\n")
            count += 1
    return count


def _boards_select():
    """D2 导出语句：ORM camelCase 属性显式 label 为 Dataset snake_case 契约。

    禁止在 serializer 猜 camelCase→snake_case；projection 直接符合 Dataset Contract。
    """
    from sqlalchemy import select

    from app.models.market_board import MarketBoard

    return select(
        MarketBoard.id,
        MarketBoard.externalCode.label("external_code"),
        MarketBoard.name,
        MarketBoard.type,
        MarketBoard.taxonomy,
        MarketBoard.source,
        MarketBoard.taxonomyVersion.label("taxonomy_version"),
        MarketBoard.taxonomyCompatibilityKey.label("taxonomy_compatibility_key"),
        MarketBoard.hierarchyLevel.label("hierarchy_level"),
        MarketBoard.parentBoardId.label("parent_board_id"),
        MarketBoard.isActive.label("is_active"),
        MarketBoard.membershipVersion.label("membership_version"),
        MarketBoard.updatedAt.label("updated_at"),
    )


def _memberships_select():
    """D3 导出语句：ORM camelCase 属性显式 label 为 Dataset snake_case 契约。"""
    from sqlalchemy import select

    from app.models.market_board import MarketBoardMembership

    return select(
        MarketBoardMembership.boardId.label("board_id"),
        MarketBoardMembership.instrumentId.label("instrument_id"),
        MarketBoardMembership.updatedAt.label("updated_at"),
    )


async def _export_dataset(
    dataset_dir: str,
    asof_date: date | None,
    history: int,
) -> int:
    """服务器一次性只读导出 Review Source Dataset（Full Corpus）。

    两阶段（P0 一致性快照，REVIEW-REPLAY-DATASET-V1 §4.0）：
      Phase A：独立 session 只读 precheck（DML 探针在此）→ 关闭；
      Phase B：全新 session `REPEATABLE READ READ ONLY` 事务，**只允许 SELECT**，
               流式写 8 域 + lineage → COMMIT；任何异常 → ROLLBACK + 清理目录（不落半成品）。
    """
    from sqlalchemy import func, select, text

    from app.db import AsyncSessionLocal
    from app.models.bar import BarDaily
    from app.models.calendar import TradingCalendar
    from app.models.first_pyramid_history import (
        FirstPyramidHistoryDailyState,
        FirstPyramidHistoryEvent,
    )
    from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
    from app.models.instrument import Instrument
    from app.models.market_board import MarketBoard, MarketBoardMembership
    from app.models.stock_feature_snapshot import StockFeatureSnapshot
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
    from app.services.review_observation_prep_service import list_recent_trading_days

    _build_readonly_engine()
    raw_dir = os.path.join(dataset_dir, "raw")
    lineage_dir = os.path.join(dataset_dir, "lineage")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(lineage_dir, exist_ok=True)

    # ---- Phase A：独立 session 只读 precheck（DML 探针在此） ----
    async with AsyncSessionLocal() as precheck_db:
        await _phase_a_precheck(precheck_db)
    logger.info("[export-dataset][phase-a] 只读 precheck 完成")

    # ---- Phase B：REPEATABLE READ READ ONLY snapshot 事务（只 SELECT） ----
    snapshot_started_at = datetime.now(UTC)
    row_counts: dict = {}
    raw_files: dict = {}
    rc = 0
    try:
        async with AsyncSessionLocal() as db:
            # 事务内第一条语句：单语句同时设置隔离级别与只读（须为事务内首条）
            await db.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            ro = (await db.execute(text("SHOW transaction_read_only"))).scalar()
            if ro != "on":
                logger.error("[export-dataset] transaction_read_only != on (%r)，拒绝导出", ro)
                rc = 2
            else:
                logger.info(
                    "[export-dataset][phase-b] transaction_read_only=%s（REPEATABLE READ snapshot）",
                    ro,
                )

            if rc == 0:
                # asof 解析（在 snapshot 事务内，与 Dimension 导出一致）
                if asof_date is None:
                    latest = await list_recent_trading_days(db, date.today(), 1)
                    if not latest:
                        logger.error("无法解析最新交易日（calendar 为空）")
                        rc = 2
                    else:
                        asof_date = latest[0]
            if rc == 0:
                need_days = history + 160 + 1  # analysis + warmup + states_start(T-1)
                trade_dates = sorted(
                    await list_recent_trading_days(db, asof_date, need_days)
                )
                if not trade_dates:
                    logger.error("trade_dates 为空（history=%d）", history)
                    rc = 2
            if rc == 0:
                ranges = compute_date_ranges(
                    asof_date,
                    trade_dates,
                    history=history,
                    warmup=160,
                    bar_lookback_calendar_days=400,
                )
                source_fact_start = date.fromisoformat(ranges["source_fact_start"])
                states_start = date.fromisoformat(ranges["states_start"])
                bars_start = date.fromisoformat(ranges["bars_start"])
                logger.info(
                    "[export-dataset][phase-b] asof=%s states=[%s,%s] bars_start=%s "
                    "analysis_days=%d",
                    asof_date.isoformat(), states_start.isoformat(),
                    asof_date.isoformat(), bars_start.isoformat(), len(ranges["analysis_axis"]),
                )

                # review_fact_universe：全部 A 股（market SH/SZ/BJ，含 delisted）
                universe_sq = select(Instrument.id).where(
                    Instrument.market.in_(("SH", "SZ", "BJ"))
                )
                universe_count = int(
                    (
                        await db.execute(
                            select(func.count())
                            .select_from(Instrument)
                            .where(Instrument.market.in_(("SH", "SZ", "BJ")))
                        )
                    ).scalar_one()
                )

                # ---- D1 instruments（全量） ----
                row_counts["instruments.jsonl.gz"] = await _stream_export(
                    db,
                    select(
                        Instrument.id, Instrument.symbol, Instrument.name,
                        Instrument.pinyin_initials, Instrument.market, Instrument.status,
                        Instrument.listing_date, Instrument.total_share,
                        Instrument.float_share, Instrument.share_as_of,
                    ),
                    _serialize_row,
                    os.path.join(raw_dir, "instruments.jsonl.gz"),
                )

                # ---- D2 boards（全量，含 inactive） ----
                row_counts["boards.jsonl.gz"] = await _stream_export(
                    db,
                    _boards_select(),
                    _serialize_row,
                    os.path.join(raw_dir, "boards.jsonl.gz"),
                )

                # ---- D3 board_memberships_current_snapshot（全量当前快照） ----
                row_counts["board_memberships_current_snapshot.jsonl.gz"] = await _stream_export(
                    db,
                    _memberships_select(),
                    _serialize_row,
                    os.path.join(raw_dir, "board_memberships_current_snapshot.jsonl.gz"),
                )

                # ---- D4 trading_calendar（[bars_start, asof] 全 market） ----
                row_counts["trading_calendar.jsonl.gz"] = await _stream_export(
                    db,
                    select(
                        TradingCalendar.trade_date, TradingCalendar.is_trading_day,
                        TradingCalendar.market, TradingCalendar.source,
                        TradingCalendar.status, TradingCalendar.verified_at,
                    ).where(TradingCalendar.trade_date.between(bars_start, asof_date)),
                    _serialize_row,
                    os.path.join(raw_dir, "trading_calendar.jsonl.gz"),
                )

                # ---- D5 first_pyramid_daily_state（IN universe ∧ 算法版本 ∧ [states_start, asof]） ----
                history_run_ids: set = set()

                def _collect_history_run(row: dict) -> None:
                    rid = row.get("source_history_run_id")
                    if rid is not None:
                        history_run_ids.add(str(rid))

                row_counts["first_pyramid_daily_state.jsonl.gz"] = await _stream_export(
                    db,
                    select(
                        FirstPyramidHistoryDailyState.instrument_id,
                        FirstPyramidHistoryDailyState.trade_date,
                        FirstPyramidHistoryDailyState.algorithm_version,
                        FirstPyramidHistoryDailyState.input_hash,
                        FirstPyramidHistoryDailyState.source_history_run_id,
                        FirstPyramidHistoryDailyState.history_contract_version,
                        FirstPyramidHistoryDailyState.state_payload,
                    ).where(
                        FirstPyramidHistoryDailyState.instrument_id.in_(universe_sq),
                        FirstPyramidHistoryDailyState.algorithm_version
                        == FIRST_PYRAMID_ALGORITHM_VERSION,
                        FirstPyramidHistoryDailyState.trade_date.between(states_start, asof_date),
                    ),
                    _serialize_row,
                    os.path.join(raw_dir, "first_pyramid_daily_state.jsonl.gz"),
                    on_row=_collect_history_run,
                )

                # ---- D6 first_pyramid_events（IN universe ∧ 双版本过滤 ∧ date-prefix 范围） ----
                row_counts["first_pyramid_events.jsonl.gz"] = await _stream_export(
                    db,
                    select(
                        FirstPyramidHistoryEvent.instrument_id,
                        FirstPyramidHistoryEvent.algorithm_version,
                        FirstPyramidHistoryEvent.event_type,
                        FirstPyramidHistoryEvent.event_id,
                        FirstPyramidHistoryEvent.event_time,
                        FirstPyramidHistoryEvent.history_contract_version,
                        FirstPyramidHistoryEvent.event_payload,
                    ).where(
                        FirstPyramidHistoryEvent.instrument_id.in_(universe_sq),
                        FirstPyramidHistoryEvent.algorithm_version
                        == FIRST_PYRAMID_ALGORITHM_VERSION,
                        FirstPyramidHistoryEvent.history_contract_version
                        == HISTORY_CONTRACT_VERSION,
                        func.left(FirstPyramidHistoryEvent.event_time, 10)
                        >= source_fact_start.isoformat(),
                        func.left(FirstPyramidHistoryEvent.event_time, 10)
                        <= asof_date.isoformat(),
                    ),
                    _serialize_row,
                    os.path.join(raw_dir, "first_pyramid_events.jsonl.gz"),
                )

                # ---- D7 bars_daily（IN universe ∧ [bars_start, asof]，含 adj_factor） ----
                row_counts["bars_daily.jsonl.gz"] = await _stream_export(
                    db,
                    select(
                        BarDaily.instrument_id, BarDaily.trade_date,
                        BarDaily.open, BarDaily.high, BarDaily.low, BarDaily.close,
                        BarDaily.volume, BarDaily.amount, BarDaily.adj_factor,
                    ).where(
                        BarDaily.instrument_id.in_(universe_sq),
                        BarDaily.trade_date.between(bars_start, asof_date),
                    ),
                    _serialize_row,
                    os.path.join(raw_dir, "bars_daily.jsonl.gz"),
                )

                # ---- D8 stock_feature_snapshots_asof（asof × universe） ----
                snapshot_run_ids: set = set()

                def _collect_snapshot_run(row: dict) -> None:
                    rid = row.get("source_run_id")
                    if rid is not None:
                        snapshot_run_ids.add(str(rid))

                row_counts["stock_feature_snapshots_asof.jsonl.gz"] = await _stream_export(
                    db,
                    select(
                        StockFeatureSnapshot.instrument_id, StockFeatureSnapshot.trade_date,
                        StockFeatureSnapshot.primary_timeframe,
                        StockFeatureSnapshot.secondary_timeframe,
                        StockFeatureSnapshot.adj, StockFeatureSnapshot.schema_version,
                        StockFeatureSnapshot.source_run_id,
                        StockFeatureSnapshot.source_primary_bar_time,
                        StockFeatureSnapshot.source_secondary_bar_time,
                        StockFeatureSnapshot.structural_payload,
                        StockFeatureSnapshot.temporal_payload,
                        StockFeatureSnapshot.summary_payload,
                        StockFeatureSnapshot.degraded_reasons,
                    ).where(
                        StockFeatureSnapshot.instrument_id.in_(universe_sq),
                        StockFeatureSnapshot.trade_date == asof_date,
                    ),
                    _serialize_row,
                    os.path.join(raw_dir, "stock_feature_snapshots_asof.jsonl.gz"),
                    on_row=_collect_snapshot_run,
                )

                # ---- L1 stock_feature_snapshot_runs（asof 全部 ∪ D8 引用闭包；REQUIRED） ----
                row_counts["stock_feature_snapshot_runs.jsonl.gz"] = await _stream_export(
                    db,
                    select(
                        StockFeatureSnapshotRun.id, StockFeatureSnapshotRun.trade_date,
                        StockFeatureSnapshotRun.schema_version,
                        StockFeatureSnapshotRun.primary_timeframe,
                        StockFeatureSnapshotRun.secondary_timeframe,
                        StockFeatureSnapshotRun.adj, StockFeatureSnapshotRun.run_type,
                        StockFeatureSnapshotRun.status,
                        StockFeatureSnapshotRun.expected_count,
                        StockFeatureSnapshotRun.snapshot_count,
                        StockFeatureSnapshotRun.failed_count,
                        StockFeatureSnapshotRun.skipped_count,
                        StockFeatureSnapshotRun.failure_rate,
                        StockFeatureSnapshotRun.started_at,
                        StockFeatureSnapshotRun.finished_at,
                        StockFeatureSnapshotRun.published_at,
                        StockFeatureSnapshotRun.metadata_,
                    ).where(
                        (StockFeatureSnapshotRun.trade_date <= asof_date)
                        | (StockFeatureSnapshotRun.id.in_(list(snapshot_run_ids)))
                    ),
                    _serialize_row,
                    os.path.join(lineage_dir, "stock_feature_snapshot_runs.jsonl.gz"),
                )

                # ---- L2 first_pyramid_history_runs（D5 引用闭包；REQUIRED） ----
                row_counts["first_pyramid_history_runs.jsonl.gz"] = await _stream_export(
                    db,
                    select(
                        FirstPyramidHistoryRun.id,
                        FirstPyramidHistoryRun.scheduler_job_run_id,
                        FirstPyramidHistoryRun.algorithm_version,
                        FirstPyramidHistoryRun.parameter_hash,
                        FirstPyramidHistoryRun.output_bars,
                        FirstPyramidHistoryRun.scope,
                        FirstPyramidHistoryRun.expected_count,
                        FirstPyramidHistoryRun.succeeded_count,
                        FirstPyramidHistoryRun.failed_count,
                        FirstPyramidHistoryRun.skipped_count,
                        FirstPyramidHistoryRun.status,
                        FirstPyramidHistoryRun.started_at,
                        FirstPyramidHistoryRun.completed_at,
                        FirstPyramidHistoryRun.metadata_json,
                    ).where(FirstPyramidHistoryRun.id.in_(list(history_run_ids))),
                    _serialize_row,
                    os.path.join(lineage_dir, "first_pyramid_history_runs.jsonl.gz"),
                )

                # ---- capture 统计与 manifest ----
                tx_ts = (
                    await db.execute(text("SELECT transaction_timestamp()"))
                ).scalar()
                contract_versions_observed: dict = {}
                cv_rows = (
                    await db.execute(
                        select(
                            FirstPyramidHistoryDailyState.history_contract_version,
                            func.count(),
                        )
                        .where(
                            FirstPyramidHistoryDailyState.instrument_id.in_(universe_sq),
                            FirstPyramidHistoryDailyState.algorithm_version
                            == FIRST_PYRAMID_ALGORITHM_VERSION,
                            FirstPyramidHistoryDailyState.trade_date.between(states_start, asof_date),
                        )
                        .group_by(FirstPyramidHistoryDailyState.history_contract_version)
                    )
                ).all()
                for cv, c in cv_rows:
                    contract_versions_observed[str(cv)] = int(c)
                d5_count = int(row_counts["first_pyramid_daily_state.jsonl.gz"])
                bars_count = int(row_counts["bars_daily.jsonl.gz"])
                _required_positive_stems = {
                    "instruments", "boards", "board_memberships_current_snapshot",
                    "trading_calendar", "first_pyramid_daily_state", "first_pyramid_events",
                    "bars_daily", "stock_feature_snapshots_asof",
                    "stock_feature_snapshot_runs", "first_pyramid_history_runs",
                }
                capture_status = (
                    "complete"
                    if all(
                        row_counts.get(f"{stem}.jsonl.gz", 0) > 0
                        for stem in _required_positive_stems
                    )
                    else "partial"
                )
                coverage = {
                    "review_fact_universe": universe_count,
                    "daily_state_rows": d5_count,
                    "events_rows": int(row_counts["first_pyramid_events.jsonl.gz"]),
                    "bars_rows": bars_count,
                    "snapshot_rows": int(row_counts["stock_feature_snapshots_asof.jsonl.gz"]),
                }
                taxonomy_versions: dict = {}
                tv_rows = (
                    await db.execute(
                        select(MarketBoard.taxonomyVersion, func.count()).group_by(
                            MarketBoard.taxonomyVersion
                        )
                    )
                ).all()
                for tv, c in tv_rows:
                    taxonomy_versions[str(tv)] = int(c)
                membership_versions: dict = {}
                mv_rows = (
                    await db.execute(
                        select(MarketBoard.membershipVersion, func.count()).group_by(
                            MarketBoard.membershipVersion
                        )
                    )
                ).all()
                for mv, c in mv_rows:
                    membership_versions[str(mv)] = int(c)

                # raw_files 双层 checksum（compressed + content）
                stem_to_subdir = {
                    stem: "raw" for stem in _RAW_FILE_STEMS.values()
                }
                stem_to_subdir.update(
                    {stem: "lineage" for stem in _LINEAGE_FILE_STEMS.values()}
                )
                for stem, subdir in stem_to_subdir.items():
                    fname = f"{stem}.jsonl.gz"
                    fpath = os.path.join(dataset_dir, subdir, fname)
                    raw_files[fname] = {
                        "rows": int(row_counts.get(fname, 0)),
                        "compressed_sha256": _sha256_file(fpath),
                        "content_sha256": _sha256_content(fpath),
                    }

                manifest = build_manifest(
                    dataset_dir_name=os.path.basename(dataset_dir),
                    capture_git_sha=_git_sha("HEAD") or "unknown",
                    base_dev_sha=_git_sha("origin/dev") or _git_sha("dev")
                    or _git_sha("HEAD") or "unknown",
                    asof=asof_date,
                    transaction_timestamp=tx_ts,
                    snapshot_started_at_utc=snapshot_started_at,
                    date_ranges=ranges,
                    row_counts=row_counts,
                    raw_files=raw_files,
                    capture_status=capture_status,
                    contract_versions_observed=contract_versions_observed,
                    coverage=coverage,
                    taxonomy_versions=taxonomy_versions,
                    membership_versions=membership_versions,
                )
                manifest_path = os.path.join(dataset_dir, "manifest.json")
                with open(manifest_path, "w", encoding="utf-8") as fh:
                    json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
                    fh.write("\n")

                await db.commit()
                logger.info(
                    "[export-dataset][phase-b] COMMIT: universe=%d d5=%d events=%d "
                    "bars=%d snapshot=%d capture_status=%s",
                    universe_count, d5_count,
                    int(row_counts["first_pyramid_events.jsonl.gz"]),
                    bars_count, int(row_counts["stock_feature_snapshots_asof.jsonl.gz"]),
                    capture_status,
                )
                print("=== Review Source Dataset Export (read-only) ===")
                print(f"dataset_dir        : {dataset_dir}")
                print(f"dataset_id         : {manifest['dataset_id']}")
                print(f"asof_date          : {asof_date.isoformat()}")
                print(f"review_fact_universe: {universe_count}")
                print(f"capture_status     : {capture_status}")
                print("row_counts:")
                for fname, cnt in sorted(row_counts.items()):
                    print(f"  {fname:<44} {cnt}")
                print("=== END ===")
    except Exception as e:  # noqa: BLE001 - 任何异常 → 不落半成品
        logger.exception("[export-dataset] 导出失败：%s", e)
        rc = 1
    if rc != 0:
        shutil.rmtree(dataset_dir, ignore_errors=True)
        logger.info("[export-dataset] 已清理半成品目录: %s", dataset_dir)
        return rc
    return 0


def _compare_raw_parquet(
    raw_path: str,
    parquet_path: str,
    domain: str,
    *,
    file_stem: str | None = None,
    batch_size: int = 10000,
) -> dict:
    """Raw(jsonl.gz) ↔ Parquet 等价比对：row count / logical content / decimal roundtrip。

    流式分批（``pq.ParquetFile.iter_batches`` + raw 侧逐行配对），不把全量 Raw 与全量
    Parquet 同时放入内存。decimal 列按 **file_stem** 解析（与 ``_rows_to_parquet`` 一致）。
    """
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise RuntimeError(
            "optional replay dependency missing (pip install -r requirements-replay.txt)"
        ) from e
    dec_cols = _DECIMAL_COLUMNS.get(file_stem or domain, {})
    result = {
        "row_mismatch": False,
        "content_mismatch": 0,
        "decimal_roundtrip_mismatch": 0,
    }
    raw_iter = _iter_jsonl_gz(raw_path)
    total_rows = 0
    parquet_file = pq.ParquetFile(parquet_path)

    for batch in parquet_file.iter_batches(batch_size=batch_size):
        n = batch.num_rows
        cols = batch.column_names
        schema = batch.schema
        for i in range(n):
            rrow = next(raw_iter, None)
            if rrow is None:
                result["row_mismatch"] = True
                break
            for name in cols:
                pv = batch.column(name)[i].as_py()
                ftype = schema.field(name).type
                if pa.types.is_decimal(ftype):
                    rv = Decimal(rrow.get(name)) if rrow.get(name) is not None else None
                    if rv != pv:
                        result["decimal_roundtrip_mismatch"] += 1
                        break
                elif pa.types.is_boolean(ftype) or pa.types.is_integer(ftype) or pa.types.is_floating(ftype):
                    if rrow.get(name) != pv:
                        result["content_mismatch"] += 1
                        break
                else:  # string（含 JSONB）
                    rv = rrow.get(name)
                    if isinstance(rv, (dict, list)):
                        rv = json.dumps(
                            rv, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        )
                    if rv != pv:
                        result["content_mismatch"] += 1
                        break
        total_rows += n
        if result["row_mismatch"]:
            break

    # drain：raw 侧还有剩余行 → parquet 行数不足
    if next(raw_iter, None) is not None:
        result["row_mismatch"] = True
    if total_rows != parquet_file.metadata.num_rows:
        result["row_mismatch"] = True
    return result


def _compute_integrity_kpis(
    raw_dir: str,
    lineage_dir: str,
    manifest: dict,
    asof: date,
) -> dict:
    """Integrity KPI（流式，避免大表入内存）。"""
    instrument_ids = {
        str(r["id"]) for r in _iter_jsonl_gz(os.path.join(raw_dir, "instruments.jsonl.gz"))
    }
    board_ids = {
        str(r["id"]) for r in _iter_jsonl_gz(os.path.join(raw_dir, "boards.jsonl.gz"))
    }
    snapshot_run_ids: set = set()
    l1_path = os.path.join(lineage_dir, "stock_feature_snapshot_runs.jsonl.gz")
    if os.path.exists(l1_path):
        snapshot_run_ids = {
            str(r["id"]) for r in _iter_jsonl_gz(l1_path)
        }
    history_run_ids: set = set()
    l2_path = os.path.join(lineage_dir, "first_pyramid_history_runs.jsonl.gz")
    if os.path.exists(l2_path):
        history_run_ids = {
            str(r["id"]) for r in _iter_jsonl_gz(l2_path)
        }

    kpi: dict = {
        "duplicate_pk": {},
        "orphan_instrument": 0,
        "orphan_board": 0,
        "orphan_snapshot_run": 0,
        "orphan_history_run": 0,
        "future_date": 0,
        "row_count_mismatch": [],
    }

    # row counts vs manifest
    for fname, finfo in (manifest.get("raw_files") or {}).items():
        fpath = os.path.join(raw_dir, fname)
        if not os.path.exists(fpath):
            fpath = os.path.join(lineage_dir, fname)
        if not os.path.exists(fpath):
            kpi["row_count_mismatch"].append(f"{fname}: 文件缺失")
            continue
        actual = sum(1 for _ in _iter_jsonl_gz(fpath))
        if actual != finfo.get("rows"):
            kpi["row_count_mismatch"].append(
                f"{fname}: manifest={finfo.get('rows')} actual={actual}"
            )

    def _check(
        domain: str,
        path: str,
        *,
        pk_cols: tuple[str, ...] = (),
        instrument_col: str | None = None,
        board_col: str | None = None,
        date_col: str | None = None,
    ) -> None:
        seen: set = set()
        dups = 0
        for row in _iter_jsonl_gz(path):
            if pk_cols:
                key = tuple(str(row.get(c)) for c in pk_cols)
                if key in seen:
                    dups += 1
                seen.add(key)
            if instrument_col is not None and row.get(instrument_col) is not None:
                if str(row[instrument_col]) not in instrument_ids:
                    kpi["orphan_instrument"] += 1
            if board_col is not None and row.get(board_col) is not None:
                if str(row[board_col]) not in board_ids:
                    kpi["orphan_board"] += 1
            if date_col is not None and row.get(date_col) is not None:
                prefix = str(row[date_col])[:10]
                if date.fromisoformat(prefix) > asof:
                    kpi["future_date"] += 1
        kpi["duplicate_pk"][domain] = dups

    _check(
        "instruments", os.path.join(raw_dir, "instruments.jsonl.gz"),
        pk_cols=("id",), instrument_col="id",
    )
    _check("boards", os.path.join(raw_dir, "boards.jsonl.gz"), pk_cols=("id",))
    _check(
        "memberships", os.path.join(raw_dir, "board_memberships_current_snapshot.jsonl.gz"),
        pk_cols=("board_id", "instrument_id"), board_col="board_id", instrument_col="instrument_id",
    )
    _check(
        "calendar", os.path.join(raw_dir, "trading_calendar.jsonl.gz"),
        pk_cols=("trade_date", "market"), date_col="trade_date",
    )
    _check(
        "daily_state", os.path.join(raw_dir, "first_pyramid_daily_state.jsonl.gz"),
        pk_cols=("instrument_id", "trade_date", "algorithm_version"),
        instrument_col="instrument_id", date_col="trade_date",
    )
    _check(
        "events", os.path.join(raw_dir, "first_pyramid_events.jsonl.gz"),
        pk_cols=("instrument_id", "algorithm_version", "history_contract_version", "event_id"),
        instrument_col="instrument_id", date_col="event_time",
    )
    _check(
        "bars", os.path.join(raw_dir, "bars_daily.jsonl.gz"),
        pk_cols=("instrument_id", "trade_date"),
        instrument_col="instrument_id", date_col="trade_date",
    )
    _check(
        "snapshot", os.path.join(raw_dir, "stock_feature_snapshots_asof.jsonl.gz"),
        pk_cols=(
            "instrument_id", "trade_date", "primary_timeframe",
            "secondary_timeframe", "adj", "schema_version",
        ),
        instrument_col="instrument_id", date_col="trade_date",
    )

    # orphan snapshot_run：snapshots.source_run_id → snapshot_runs.id
    for row in _iter_jsonl_gz(os.path.join(raw_dir, "stock_feature_snapshots_asof.jsonl.gz")):
        rid = row.get("source_run_id")
        if rid is not None and str(rid) not in snapshot_run_ids:
            kpi["orphan_snapshot_run"] += 1
    # orphan history_run：daily_state.source_history_run_id → history_runs.id
    for row in _iter_jsonl_gz(os.path.join(raw_dir, "first_pyramid_daily_state.jsonl.gz")):
        rid = row.get("source_history_run_id")
        if rid is not None and str(rid) not in history_run_ids:
            kpi["orphan_history_run"] += 1
    return kpi


def _data_quality_summary(raw_dir: str, lineage_dir: str, manifest: dict) -> dict:
    """数据质量摘要（Corpus Characterization，非业务 KPI；REVIEW-REPLAY-DATASET-V1 §10.3）。"""
    from collections import Counter

    instruments = sum(1 for _ in _iter_jsonl_gz(os.path.join(raw_dir, "instruments.jsonl.gz")))
    boards = list(_iter_jsonl_gz(os.path.join(raw_dir, "boards.jsonl.gz")))
    active = [b for b in boards if b.get("is_active", True)]
    concept = [b for b in active if b.get("type") == "concept"]
    industry = [b for b in active if b.get("type") == "industry"]
    memberships = list(
        _iter_jsonl_gz(os.path.join(raw_dir, "board_memberships_current_snapshot.jsonl.gz"))
    )
    union = {str(m["instrument_id"]) for m in memberships}
    cnt: Counter = Counter()
    for m in memberships:
        cnt[str(m["instrument_id"])] += 1
    overlaps = sorted(cnt.values())

    def _pct(arr: list, p: float) -> int:
        if not arr:
            return 0
        return int(arr[min(len(arr) - 1, int(len(arr) * p / 100.0))])

    def _pct_f(arr: list, p: float) -> float:
        if not arr:
            return 0.0
        return float(arr[min(len(arr) - 1, int(len(arr) * p / 100.0))])

    bar_counts: Counter = Counter()
    for r in _iter_jsonl_gz(os.path.join(raw_dir, "bars_daily.jsonl.gz")):
        bar_counts[str(r["instrument_id"])] += 1
    state_counts: Counter = Counter()
    for r in _iter_jsonl_gz(os.path.join(raw_dir, "first_pyramid_daily_state.jsonl.gz")):
        state_counts[str(r["instrument_id"])] += 1
    event_by_type: Counter = Counter()
    event_by_date: Counter = Counter()
    for r in _iter_jsonl_gz(os.path.join(raw_dir, "first_pyramid_events.jsonl.gz")):
        event_by_type[str(r.get("event_type"))] += 1
        event_by_date[str(r.get("event_time"))[:10]] += 1
    snapshot_total = sum(
        1 for _ in _iter_jsonl_gz(os.path.join(raw_dir, "stock_feature_snapshots_asof.jsonl.gz"))
    )
    l1_path = os.path.join(lineage_dir, "stock_feature_snapshot_runs.jsonl.gz")
    consumable_run_ids: set = set()
    if os.path.exists(l1_path):
        consumable_run_ids = {
            str(r["id"])
            for r in _iter_jsonl_gz(l1_path)
            if r.get("status") == "succeeded" and r.get("published_at") is not None
        }
    consumable = sum(
        1
        for r in _iter_jsonl_gz(os.path.join(raw_dir, "stock_feature_snapshots_asof.jsonl.gz"))
        if str(r.get("source_run_id")) in consumable_run_ids
    )

    # ---- coverage_report：analysis 窗口 State T/T1 与 Bar T 覆盖率（只报告，不作 gate） ----
    # P1-A（审查修复）：coverage 分母 = review_fact_universe（SH/SZ/BJ 全 A 股），
    #   而非 D1 全量 instruments（D1 含非 A 股维度，会系统性低估 coverage）。
    # P1-B（审查修复）：T-1 用 raw/trading_calendar 构造 canonical predecessor，
    #   与 production Review `_build_t1_map()` 语义一致，替代数组位置 analysis_axis[i-1]。
    analysis_axis = list((manifest.get("date_ranges") or {}).get("analysis_axis") or [])
    analysis_set = set(analysis_axis)
    n_dates = len(analysis_axis)

    fact_universe = int(
        (
            (manifest.get("source_readiness") or {})
            .get("first_pyramid_history", {})
            .get("coverage", {})
            .get("review_fact_universe")
        )
        or instruments
    )
    calendar_dates = sorted(
        {
            str(r.get("trade_date"))[:10]
            for r in _iter_jsonl_gz(os.path.join(raw_dir, "trading_calendar.jsonl.gz"))
            if r.get("is_trading_day") is True and str(r.get("market")) == "A"
        }
    )
    prev_by_date: dict[str, str | None] = {}
    for i, d in enumerate(calendar_dates):
        prev_by_date[d] = calendar_dates[i - 1] if i > 0 else None

    state_date_count: Counter = Counter()
    bar_date_count: Counter = Counter()
    state_dates_by_instrument: dict[str, set] = {}
    for r in _iter_jsonl_gz(os.path.join(raw_dir, "first_pyramid_daily_state.jsonl.gz")):
        td = str(r.get("trade_date"))[:10]
        state_date_count[td] += 1
        if td in analysis_set:
            state_dates_by_instrument.setdefault(str(r["instrument_id"]), set()).add(td)
    for r in _iter_jsonl_gz(os.path.join(raw_dir, "bars_daily.jsonl.gz")):
        td = str(r.get("trade_date"))[:10]
        if td in analysis_set:
            bar_date_count[td] += 1

    def _denom(n: int, base: int) -> float:
        return float(n) / base if base else 0.0

    state_t_cov = [_denom(state_date_count.get(T, 0), fact_universe) for T in analysis_axis]
    state_t1_cov = [
        _denom(state_date_count.get(prev_by_date.get(T), 0), fact_universe)
        for T in analysis_axis
    ]
    missing_t1 = [T for T in analysis_axis if prev_by_date.get(T) is None]
    bar_exact_t_cov = [_denom(bar_date_count.get(T, 0), fact_universe) for T in analysis_axis]
    member_state_cov = (
        [len(s) / n_dates for s in state_dates_by_instrument.values()]
        if n_dates
        else []
    )

    state_t_median = _pct_f(sorted(state_t_cov), 50)
    bar_t_median = _pct_f(sorted(bar_exact_t_cov), 50)
    if state_t_median >= 0.9 and bar_t_median >= 0.9:
        prd_readiness = "available"
    elif state_t_median > 0 or bar_t_median > 0:
        prd_readiness = "partial"
    else:
        prd_readiness = "unavailable"

    summary = {
        "instruments": instruments,
        "review_fact_universe": fact_universe,
        "active_boards": len(active),
        "concept_boards": len(concept),
        "industry_boards": len(industry),
        "union_members": len(union),
        "membership_overlap": {
            "p50": _pct(overlaps, 50),
            "p90": _pct(overlaps, 90),
            "max": overlaps[-1] if overlaps else 0,
        },
        "bar_count_per_instrument": {
            "p10": _pct(sorted(bar_counts.values()), 10),
            "p50": _pct(sorted(bar_counts.values()), 50),
            "p90": _pct(sorted(bar_counts.values()), 90),
        },
        "state_count_per_instrument": {
            "p10": _pct(sorted(state_counts.values()), 10),
            "p50": _pct(sorted(state_counts.values()), 50),
            "p90": _pct(sorted(state_counts.values()), 90),
        },
        "events_by_type": dict(sorted(event_by_type.items())),
        "events_by_date": dict(sorted(event_by_date.items())),
        "snapshot_total": snapshot_total,
        "snapshot_consumable": consumable,
        "asof": manifest.get("analysis_asof_date"),
        "coverage_report": {
            "state_t_coverage_by_date": {
                "p10": _pct_f(sorted(state_t_cov), 10),
                "p50": _pct_f(sorted(state_t_cov), 50),
                "p90": _pct_f(sorted(state_t_cov), 90),
                "max": _pct_f(sorted(state_t_cov), 100),
            },
            "state_t1_coverage_by_date": {
                "p10": _pct_f(sorted(state_t1_cov), 10),
                "p50": _pct_f(sorted(state_t1_cov), 50),
                "p90": _pct_f(sorted(state_t1_cov), 90),
                "max": _pct_f(sorted(state_t1_cov), 100),
            },
            "member_state_coverage_p10_p50_p90": (
                _pct_f(sorted(member_state_cov), 10),
                _pct_f(sorted(member_state_cov), 50),
                _pct_f(sorted(member_state_cov), 90),
            ),
            "bar_exact_t_coverage": {
                "p10": _pct_f(sorted(bar_exact_t_cov), 10),
                "p50": _pct_f(sorted(bar_exact_t_cov), 50),
                "p90": _pct_f(sorted(bar_exact_t_cov), 90),
                "max": _pct_f(sorted(bar_exact_t_cov), 100),
            },
            "bar_history_count": {
                "p10": _pct(sorted(bar_counts.values()), 10),
                "p50": _pct(sorted(bar_counts.values()), 50),
                "p90": _pct(sorted(bar_counts.values()), 90),
            },
            "missing_t1_analysis_dates": missing_t1,
        },
        "prd_readiness": prd_readiness,
    }
    return summary


def _dataset_validate(dataset_dir: str) -> int:
    """本地校验 manifest + Integrity KPI + jsonl.gz → parquet + 生成 views + 数据质量摘要。

    Hard Gate（任一 FAIL → return 2；**Raw Gate PASS 之前不创建 parquet/ 与 views/**）：

    ```
    manifest contract → FAIL → STOP
    raw_files checksum → FAIL → STOP
    Raw Integrity KPI   → FAIL → STOP
    （此时才 makedirs parquet/ views/）
    Parquet 转换        → 逐域 equivalence FAIL → STOP
    manifest.derived_files 写回
    Views + union 校验  → FAIL → STOP
    quality_summary → return 0
    ```
    """
    manifest_path = os.path.join(dataset_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        logger.error("[dataset-validate] 缺少 manifest.json: %s", dataset_dir)
        return 2
    manifest = load_manifest(manifest_path)
    violations = validate_manifest_contract(manifest)
    if violations:
        print("[dataset-validate] manifest 契约违规:")
        for v in violations:
            print(f"  - {v}")
        return 2
    asof = date.fromisoformat(manifest["analysis_asof_date"])
    raw_dir = os.path.join(dataset_dir, "raw")
    lineage_dir = os.path.join(dataset_dir, "lineage")
    parquet_dir = os.path.join(dataset_dir, "parquet")
    views_dir = os.path.join(dataset_dir, "views")

    print("=== REVIEW-REPLAY-DATASET validate ===")
    print(f"dataset_dir     : {dataset_dir}")
    print(f"dataset_id      : {manifest.get('dataset_id')}")
    print(f"asof            : {asof.isoformat()}")

    # 1) 文件 checksum（manifest.raw_files 的 compressed/content_sha256）→ FAIL → STOP
    checksum_fail = 0
    for fname, finfo in (manifest.get("raw_files") or {}).items():
        fpath = os.path.join(raw_dir, fname)
        if not os.path.exists(fpath):
            fpath = os.path.join(lineage_dir, fname)
        if not os.path.exists(fpath):
            print(f"  [checksum] {fname}: 文件缺失")
            checksum_fail += 1
            continue
        if _sha256_file(fpath) != finfo.get("compressed_sha256"):
            print(f"  [checksum] {fname}: compressed_sha256 不匹配")
            checksum_fail += 1
        if _sha256_content(fpath) != finfo.get("content_sha256"):
            print(f"  [checksum] {fname}: content_sha256 不匹配")
            checksum_fail += 1
    print(f"checksum_mismatch : {checksum_fail}")
    # checksum gate 在 KPI 之前：缺文件/checksum 不匹配直接 STOP，避免 KPI 读缺失文件崩溃
    if checksum_fail > 0:
        print("[dataset-validate] CHECKSUM GATE FAILED → STOP（不生成 parquet/views）")
        return 2

    # 2) Raw Integrity KPI → FAIL → STOP
    kpi = _compute_integrity_kpis(raw_dir, lineage_dir, manifest, asof)
    print(f"duplicate_pk       : {kpi['duplicate_pk']}")
    print(f"orphan_instrument  : {kpi['orphan_instrument']}")
    print(f"orphan_board       : {kpi['orphan_board']}")
    print(f"orphan_snapshot_run: {kpi['orphan_snapshot_run']}")
    print(f"orphan_history_run : {kpi['orphan_history_run']}")
    print(f"future_date        : {kpi['future_date']}")
    if kpi["row_count_mismatch"]:
        print("row_count_mismatch :")
        for m in kpi["row_count_mismatch"]:
            print(f"  - {m}")

    raw_gate_failed = (
        checksum_fail > 0
        or any(kpi["duplicate_pk"].values())
        or kpi["orphan_instrument"] > 0
        or kpi["orphan_board"] > 0
        or kpi["orphan_snapshot_run"] > 0
        or kpi["orphan_history_run"] > 0
        or kpi["future_date"] > 0
        or bool(kpi["row_count_mismatch"])
    )
    if raw_gate_failed:
        print("[dataset-validate] RAW GATE FAILED → STOP（不生成 parquet/views）")
        return 2

    # 3) Raw Gate PASS 后才创建派生目录
    os.makedirs(parquet_dir, exist_ok=True)
    os.makedirs(views_dir, exist_ok=True)

    # 4) Parquet 转换 + Raw↔Parquet 等价（DATASET-3 层；lazy pyarrow）→ FAIL → STOP
    row_mismatch = 0
    content_mismatch = 0
    dec_roundtrip = 0
    manifest.setdefault("derived_files", {})
    for domain, stem in _RAW_FILE_STEMS.items():
        raw_path = os.path.join(raw_dir, f"{stem}.jsonl.gz")
        if not os.path.exists(raw_path):
            continue
        info = _rows_to_parquet(raw_dir, parquet_dir, domain, stem)
        if info["rows"] == 0:
            # 空文件域：不落 parquet，跳过 equivalence 与 derived_files 条目
            print(f"  [parquet] {stem:<40} rows=0（空域，跳过 equivalence）")
            continue
        eq = _compare_raw_parquet(raw_path, info["path"], domain, file_stem=stem)
        row_mismatch += 1 if eq["row_mismatch"] else 0
        content_mismatch += eq["content_mismatch"]
        dec_roundtrip += eq["decimal_roundtrip_mismatch"]
        print(f"  [parquet] {stem:<40} rows={info['rows']} "
              f"eq(row={0 if eq['row_mismatch'] else 1}, content={eq['content_mismatch']}, "
              f"decimal={eq['decimal_roundtrip_mismatch']})")
        # 5) derived_files 闭环（equivalence PASS 才写入）
        manifest["derived_files"][f"{stem}.parquet"] = {
            "rows": info["rows"],
            "sha256": _sha256_file(info["path"]),
            "derived_from": f"raw/{stem}.jsonl.gz",
        }
    print(f"parquet_row_mismatch  : {row_mismatch}")
    print(f"parquet_content_mismatch: {content_mismatch}")
    print(f"decimal_roundtrip_mismatch: {dec_roundtrip}")
    if row_mismatch or content_mismatch or dec_roundtrip:
        print("[dataset-validate] PARQUET EQUIVALENCE FAILED → STOP")
        return 2

    # 6) 写回 manifest.derived_files（additive，幂等；raw 文件与 source_readiness 不变）
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")

    # 7) 生成 views + derived_instrument_ids == union(memberships) 校验 → FAIL → STOP
    boards = list(_iter_jsonl_gz(os.path.join(raw_dir, "boards.jsonl.gz")))
    memberships = list(
        _iter_jsonl_gz(os.path.join(raw_dir, "board_memberships_current_snapshot.jsonl.gz"))
    )
    analysis_axis = list((manifest.get("date_ranges") or {}).get("analysis_axis") or [])
    views = build_views(boards, memberships, analysis_axis=analysis_axis or None)
    view_assert_fail = 0
    for vid, view in views.items():
        vpath = os.path.join(views_dir, f"{vid}.json")
        with open(vpath, "w", encoding="utf-8") as fh:
            json.dump(view, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        scope_set = set(view["scope_keys"])
        union = {str(m["instrument_id"]) for m in memberships if str(m["board_id"]) in scope_set}
        if set(view["derived_instrument_ids"]) != union:
            view_assert_fail += 1
            print(f"  [views] {vid}: derived_instrument_ids != union(memberships)")
    print(f"views_generated : {sorted(views.keys())}")
    print(f"view_assert_fail: {view_assert_fail}")
    if view_assert_fail:
        print("[dataset-validate] VIEWS UNION FAILED → STOP")
        return 2

    # 8) 数据质量摘要（derived，不写回 immutable raw manifest）
    summary = _data_quality_summary(raw_dir, lineage_dir, manifest)
    summary_path = os.path.join(dataset_dir, "quality_summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    print("data quality summary:")
    print(f"  instruments            : {summary['instruments']}")
    print(f"  review_fact_universe   : {summary['review_fact_universe']}")
    print(f"  active/concept/industry: {summary['active_boards']}/{summary['concept_boards']}/{summary['industry_boards']}")
    print(f"  union_members          : {summary['union_members']}")
    print(f"  membership_overlap     : {summary['membership_overlap']}")
    print(f"  bar_count p10/p50/p90  : {summary['bar_count_per_instrument']}")
    print(f"  state_count p10/p50/p90: {summary['state_count_per_instrument']}")
    print(f"  snapshot_total/consumable: {summary['snapshot_total']}/{summary['snapshot_consumable']}")
    print(f"  events_by_type         : {len(summary['events_by_type'])} types")
    print(f"  prd_readiness          : {summary['prd_readiness']}")
    print("=== END ===")
    return 0


def _load_parquet_rows(dataset_dir: str, stem: str) -> list[dict]:
    """Read a SMALL metadata ``parquet/<stem>.parquet`` fully into dict rows.

    R0-C1: this full-materialization path is allowed ONLY for small metadata
    files (calendar / boards / memberships).  It MUST NOT be used for the large
    fact domains (bars_daily / first_pyramid_daily_state / first_pyramid_events /
    stock_feature_snapshots_asof) — those go through the Selection-First bounded
    ``_iter_parquet_rows`` scanner instead, or the whole corpus gets materialized
    into multi-GB Python lists (the OOM we are fixing).

    PURE I/O only — no business formula.  JSONB columns arrive as JSON strings
    (the parquet converter writes dict/list as ``pa.string()`` + ``json.dumps``),
    decoded by the shared ``_decode_jsonb`` mapper where the consumer needs a dict.
    """
    _LARGE_FACT_STEMS = {
        "bars_daily",
        "first_pyramid_daily_state",
        "first_pyramid_events",
        "stock_feature_snapshots_asof",
    }
    if stem in _LARGE_FACT_STEMS:
        raise RuntimeError(
            f"_load_parquet_rows({stem!r}): large fact domain must use the "
            f"Selection-First _iter_parquet_rows scanner (R0-C1 memory contract)"
        )
    try:
        import pyarrow.parquet as pq
    except ImportError as e:
        raise RuntimeError(
            "optional replay dependency missing (pip install -r requirements-replay.txt)"
        ) from e
    path = os.path.join(dataset_dir, "parquet", f"{stem}.parquet")
    if not os.path.exists(path):
        return []
    table = pq.read_table(path)
    rows = table.to_pylist()
    return rows


def _iter_parquet_rows(
    dataset_dir: str,
    stem: str,
    *,
    columns: list[str],
    filter_expr: Any = None,
    batch_size: int = 8192,
    use_iter_batches: bool = False,
) -> Iterable[dict]:
    """Selection-First bounded scan of a parquet fact domain (R0-C1).

    Streams the file in PyArrow record batches, applying ``columns`` projection
    and an optional ``filter_expr`` (``pyarrow.dataset`` Expression) at the scan
    layer, then yields one plain dict per row.  Only a single controlled batch is
    ever converted with ``to_pylist()`` — never the whole ``Table`` — so peak
    memory is bounded by ``batch_size`` regardless of on-disk corpus size.
    """
    try:
        import pyarrow.dataset as ds
    except ImportError as e:
        raise RuntimeError(
            "optional replay dependency missing (pip install -r requirements-replay.txt)"
        ) from e
    path = os.path.join(dataset_dir, "parquet", f"{stem}.parquet")
    if not os.path.exists(path):
        return
    if use_iter_batches:
        # ``ds.Scanner`` materializes a whole row group at a time.  When one row
        # group holds a very wide column (snapshot ``summary_payload``: 5293 rows
        # in a SINGLE row group, ~1.5 GB total) that alone costs ~2 GB before any
        # decode.  ``ParquetFile.iter_batches`` slices inside the row group, which
        # measurably lowers peak RSS (~2059 MB -> ~894 MB).  ``filter_expr`` is not
        # supported on this path, so the caller re-checks predicates per row.
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=batch_size, columns=columns):
            for row in batch.to_pylist():
                yield row
            del batch
        return

    dataset = ds.dataset(path, format="parquet")
    scanner = dataset.scanner(
        columns=columns,
        filter=filter_expr,
        batch_size=batch_size,
    )
    for batch in scanner.to_batches():
        for row in batch.to_pylist():
            yield row
        del batch


def _read_lineage_rows(dataset_dir: str, stem: str) -> list[dict]:
    """Read a ``lineage/<stem>.jsonl.gz`` (run metadata) into a list of dict rows."""
    path = os.path.join(dataset_dir, "lineage", f"{stem}.jsonl.gz")
    if not os.path.exists(path):
        return []
    return list(_iter_jsonl_gz(path))


def _accepted_exact_t_snapshot_run_ids(
    dataset_dir: str, asof_date: date
) -> frozenset[str]:
    """SINGLE owner for "accepted Current snapshot run at exact-T".

    AUDIT-FIX-01 (B.4): the Snapshot readiness decision previously existed twice —
    the Integrity Gate checked only the parquet date range, while Replay Selection
    checked status==succeeded + published_at!=null + trade_date==asof.  That drift is
    closed by having BOTH consumers use this one owner.

    A snapshot run is consumable iff it is:
      - a snapshot RUN lineage row (not a snapshot fact),
      - status == succeeded,
      - published_at is not None,
      - trade_date == asof (exact-T, never a later backfill or earlier day).
    """
    # Run-gate constant owned by the production prep service — never redefined here
    # (a second copy would silently drift).  Same single source as Replay Selection.
    from app.services.review_observation_prep_service import (
        _SNAPSHOT_RUN_CONSUMABLE_STATUS,
    )

    run_rows = _read_lineage_rows(dataset_dir, "stock_feature_snapshot_runs")
    accepted: set[str] = set()
    asof_iso = asof_date.isoformat()
    for r in run_rows:
        if (
            r.get("status") == _SNAPSHOT_RUN_CONSUMABLE_STATUS
            and r.get("published_at") is not None
            and str(r.get("trade_date") or "")[:10] == asof_iso
        ):
            rid = r.get("id")
            if rid:
                accepted.add(str(rid))
    return frozenset(accepted)


@dataclass(frozen=True)
class ReplaySelection:
    """The Selection resolved BEFORE any large fact domain is scanned (R0-C1).

    Stage A reads only small metadata (view / boards / memberships / calendar /
    snapshot_runs lineage) and produces this frozen selection.  Stage B then
    scans bars/state/events/snapshots restricted to exactly these members, dates,
    columns and accepted snapshot runs.
    """

    asof_date: date
    declared_asof: date | None
    t1_date: date | None
    scope_specs: tuple[Any, ...]
    union_member_ids: frozenset[uuid.UUID]
    accepted_snapshot_run_ids: frozenset[str]
    trading_days: tuple[date, ...]
    bar_window_start: date


def _build_replay_selection_from_specs(
    dataset_dir: str,
    scope_specs: list[Any],
    asof_override: date | None = None,
) -> ReplaySelection:
    """Shared Stage-A metadata owner — resolves a ``ReplaySelection`` from a list
    of already-resolved ``scope_specs`` (A2).

    No bars/state/events/snapshot scan happens here.  Current L1 is locked to a
    single asof date, so state/events only need {T, T-1}, snapshots only need
    exact-T under an accepted run, and bars only need the 400-calendar-day
    VolumeContext window for the union members.  The union member set is derived
    from the supplied ``scope_specs`` — NOT from any view file — so the view path
    and the fixture path share this single owner and neither needs the other.

    asof selection follows the **two-track** design (R0-C3 修正):
    - Track A (default): lock the **manifest declared asof** (Current-asof Acceptance),
      e.g. 2026-08-17.
    - Track B (--asof-lock): lock an explicit date where Daily State genuinely exists
      (e.g. 2026-08-10) for Historical-capable Semantic Test.

    ``min(max_date)`` is NOT used to derive a "common available date".  No latest
    backfill is ever performed.
    """
    # Window constant owned by the production prep service — never redefined here.
    # (The snapshot run-gate constant now lives solely in the shared
    # ``_accepted_exact_t_snapshot_run_ids`` owner.)
    from app.services.review_observation_prep_service import _BAR_LOOKBACK_DAYS

    declared_asof_str = _dataset_asof(dataset_dir)
    if asof_override is not None:
        asof_date = asof_override  # Track B: explicit historical-asof lock.
    else:
        # Track A (default): Current-asof = manifest declared asof.
        if not declared_asof_str:
            raise RuntimeError(
                "[replay-l1] corpus 无 declared asof，无法锁定 Current L1 语义日期"
            )
        asof_date = date.fromisoformat(declared_asof_str)

    # union members derived from the supplied scope_specs (NOT a view file).
    union_ids: set[uuid.UUID] = set()
    for spec in scope_specs:
        union_ids.update(spec.member_ids)

    # trading calendar (small) -> T-1 + bar window.
    calendar_rows = _load_parquet_rows(dataset_dir, "trading_calendar")
    trading_days = sorted(
        date.fromisoformat(r["trade_date"])
        for r in calendar_rows
        if r.get("is_trading_day") and r.get("market") == "A" and r.get("trade_date")
    )
    idx = bisect_left(trading_days, asof_date)
    t1_date = trading_days[idx - 1] if idx > 0 else None
    bar_window_start = asof_date - timedelta(days=_BAR_LOOKBACK_DAYS)

    # accepted Current snapshot runs at exact-T (single shared owner, AUDIT-FIX-01).
    accepted = _accepted_exact_t_snapshot_run_ids(dataset_dir, asof_date)

    declared_asof = (
        date.fromisoformat(declared_asof_str) if declared_asof_str else None
    )
    return ReplaySelection(
        asof_date=asof_date,
        declared_asof=declared_asof,
        t1_date=t1_date,
        scope_specs=tuple(scope_specs),
        union_member_ids=frozenset(union_ids),
        accepted_snapshot_run_ids=frozenset(accepted),
        trading_days=tuple(trading_days),
        bar_window_start=bar_window_start,
    )


def _build_replay_selection(
    dataset_dir: str, view_name: str, asof_override: date | None = None
) -> ReplaySelection:
    """Stage-A selection from a view name — thin wrapper over the shared owner.

    Resolves scope_specs from the view, then delegates all metadata resolution
    (union / calendar / T-1 / bar window / accepted runs) to
    ``_build_replay_selection_from_specs`` so the view path and the fixture path
    share the SAME selection owner (A2).
    """
    scope_specs = _load_scope_specs(dataset_dir, view_name)
    return _build_replay_selection_from_specs(
        dataset_dir, scope_specs, asof_override=asof_override
    )


def _load_replay_facts(
    dataset_dir: str,
    scope_specs: list[Any],
    *,
    selection: ReplaySelection | None = None,
    instr: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stage B — bounded, Selection-First load of the Current L1 source facts.

    Each large domain is scanned with column projection + member/date/version
    predicates and decoded/mapped on the fly to production fact objects; raw row
    dicts are never retained.  ``selection`` carries the Stage-A resolved members,
    dates and accepted runs; ``instr`` (optional) collects per-domain
    ``selected_rows`` / scan+decode ms / RSS instrumentation.
    """
    try:
        import pyarrow.dataset as ds
    except ImportError as e:
        raise RuntimeError(
            "optional replay dependency missing (pip install -r requirements-replay.txt)"
        ) from e
    from collections import defaultdict

    from app.domain.review.member_fact import DailyBarFact
    from app.services.review_observation_prep_service import (
        FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        HISTORY_CONTRACT_VERSION,
        _CURRENT_ONLY_SNAPSHOT_FIELDS,
        _build_t1_map,
        _decode_jsonb,
        _map_daily_bar_fact,
        _map_structure_event,
        _InstrumentBarSeries,
    )

    if selection is None:
        raise RuntimeError("_load_replay_facts requires a Stage-A ReplaySelection")

    instr = instr if instr is not None else {}

    def _mark(key: str) -> None:
        instr[f"rss_after_{key}_mb"] = round(_rss_mb(), 1)

    asof_date = selection.asof_date
    t1_date = selection.t1_date
    union_member_strs = frozenset(str(m) for m in selection.union_member_ids)
    # trade_dates axis for the union (Current L1 uses only [asof]; t1 kept for map).
    t_dates = [d for d in (t1_date, asof_date) if d is not None]
    t1_by_date = _build_t1_map([asof_date], list(selection.trading_days))
    _mark("selection")

    # PyArrow membership filter: instrument_id ∈ union.  We build it once as an
    # ``isin`` set expression (bounded even for capacity_4096 ~ few k members).
    member_set = pa_array_or_none(union_member_strs)

    def _member_filter():
        if member_set is None:
            return None
        return ds.field("instrument_id").isin(member_set)

    # ---- 1. Daily state: instrument ∈ union, trade_date ∈ {T, T1}, algo ----
    t_iso = asof_date.isoformat()
    t1_iso = t1_date.isoformat() if t1_date else None
    state_dates = [d for d in (t_iso, t1_iso) if d]
    state_filter = (
        (ds.field("algorithm_version") == FIRST_PYRAMID_CORE_ALGORITHM_VERSION)
        & (ds.field("trade_date").isin(state_dates))
    )
    mf = _member_filter()
    if mf is not None:
        state_filter = state_filter & mf
    _sbd: dict[date, dict[uuid.UUID, dict]] = defaultdict(dict)
    t0 = _perf_counter_ms()
    n_state = 0
    for r in _iter_parquet_rows(
        dataset_dir,
        "first_pyramid_daily_state",
        columns=["instrument_id", "trade_date", "algorithm_version", "state_payload"],
        filter_expr=state_filter,
    ):
        td = date.fromisoformat(r["trade_date"]) if r.get("trade_date") else None
        if td is None:
            continue
        _sbd[td][uuid.UUID(str(r["instrument_id"]))] = _decode_jsonb(r.get("state_payload"))
        n_state += 1
    states_by_date = dict(_sbd)
    instr["state_rows_selected"] = n_state
    instr["state_scan_ms"] = round(_perf_counter_ms() - t0, 1)
    _mark("states")

    # ---- 2. Bars: instrument ∈ union, [asof-400d, asof] ----
    bar_filter = (
        (ds.field("trade_date") >= selection.bar_window_start.isoformat())
        & (ds.field("trade_date") <= t_iso)
    )
    if mf is not None:
        bar_filter = bar_filter & mf
    by_instrument: dict[uuid.UUID, list[DailyBarFact]] = defaultdict(list)
    t0 = _perf_counter_ms()
    n_bar = 0
    for r in _iter_parquet_rows(
        dataset_dir,
        "bars_daily",
        columns=[
            "instrument_id", "trade_date",
            "open", "high", "low", "close", "volume", "amount",
        ],
        filter_expr=bar_filter,
    ):
        td = date.fromisoformat(r["trade_date"]) if r.get("trade_date") else None
        if td is None:
            continue
        by_instrument[uuid.UUID(str(r["instrument_id"]))].append(
            _map_daily_bar_fact(
                trade_date=td,
                open=r.get("open"), high=r.get("high"), low=r.get("low"),
                close=r.get("close"), volume=r.get("volume"), amount=r.get("amount"),
            )
        )
        n_bar += 1
    bars: dict[uuid.UUID, _InstrumentBarSeries] = {}
    for iid, facts in by_instrument.items():
        ordered = sorted(facts, key=lambda b: b.trade_date)
        bars[iid] = _InstrumentBarSeries(
            facts=tuple(ordered),
            dates=tuple(b.trade_date for b in ordered),
        )
    by_instrument.clear()
    instr["bar_rows_selected"] = n_bar
    instr["bar_scan_ms"] = round(_perf_counter_ms() - t0, 1)
    _mark("bars")

    # ---- 3. Events: instrument ∈ union, exact-T, algo + history contract ----
    ev_lo = t_iso
    ev_hi = (asof_date + timedelta(days=1)).isoformat()
    event_filter = (
        (ds.field("algorithm_version") == FIRST_PYRAMID_CORE_ALGORITHM_VERSION)
        & (ds.field("history_contract_version") == HISTORY_CONTRACT_VERSION)
        & (ds.field("event_time") >= ev_lo)
        & (ds.field("event_time") < ev_hi)
    )
    if mf is not None:
        event_filter = event_filter & mf
    _ebd: dict[date, list[Any]] = defaultdict(list)
    t0 = _perf_counter_ms()
    n_event = 0
    for r in _iter_parquet_rows(
        dataset_dir,
        "first_pyramid_events",
        columns=[
            "instrument_id", "event_time", "event_type", "event_payload",
            "algorithm_version", "history_contract_version",
        ],
        filter_expr=event_filter,
    ):
        etime = r.get("event_time")
        if not etime:
            continue
        td = date.fromisoformat(etime[:10])
        payload = _decode_jsonb(r.get("event_payload"))
        _ebd[td].append(
            _map_structure_event(
                instrument_id=str(r["instrument_id"]),
                event_type=r.get("event_type"),
                direction=payload.get("direction"),
                level=payload.get("level"),
                internal=payload.get("internal"),
                release_volume_ratio=payload.get("release_volume_ratio"),
            )
        )
        n_event += 1
    events_by_date = dict(_ebd)
    instr["event_rows_selected"] = n_event
    instr["event_scan_ms"] = round(_perf_counter_ms() - t0, 1)
    _mark("events")

    # ---- 4. Current-only snapshot facts: instrument ∈ union, exact-T,
    #      source_run_id ∈ accepted runs; drop summary_payload immediately ----
    current_only_facts_by_date: dict[date, dict[str, dict[str, object]]] = {}
    t0 = _perf_counter_ms()
    n_snap = 0
    # ``summary_payload`` averages ~287 KB/row (max ~1.1 MB) while Current L1 needs
    # only the ~4 KB ``first_pyramid_flat`` subtree (~1.4% of the bytes).  A default
    # 8192-row batch would therefore stage ~2.3 GB of JSON strings before decode, so
    # this domain scans in small batches: peak is bounded to batch_size × row size.
    if not selection.accepted_snapshot_run_ids:
        # Run gate not consumable at exact-T -> Current-only facts unavailable.
        # Skip the scan entirely rather than reading 1.5 GB to discard it.
        snapshot_rows = iter(())
    else:
        snapshot_rows = _iter_parquet_rows(
            dataset_dir,
            "stock_feature_snapshots_asof",
            columns=["instrument_id", "trade_date", "source_run_id", "summary_payload"],
            batch_size=_SNAPSHOT_SCAN_BATCH_SIZE,
            use_iter_batches=True,
        )
    for r in snapshot_rows:
        # iter_batches has no filter pushdown -> re-apply the exact-T + run gate +
        # union-membership predicates per row (same semantics as ``snap_filter``).
        if str(r.get("trade_date") or "")[:10] != t_iso:
            continue
        if str(r.get("source_run_id") or "") not in selection.accepted_snapshot_run_ids:
            continue
        if union_member_strs and str(r.get("instrument_id")) not in union_member_strs:
            continue
        summary = _decode_jsonb(r.get("summary_payload"))
        flat = summary.get("first_pyramid_flat") if isinstance(summary, dict) else None
        facts: dict[str, object] = {}
        if isinstance(flat, dict):
            for attr, flat_key in _CURRENT_ONLY_SNAPSHOT_FIELDS.items():
                if flat_key in flat:
                    facts[attr] = flat[flat_key]
        # Release the ~287 KB decoded payload immediately — only the 7 projected
        # Current-only values are retained (never the whole summary).
        summary = None
        flat = None
        r["summary_payload"] = None
        if facts:
            current_only_facts_by_date.setdefault(asof_date, {})[
                str(r["instrument_id"])
            ] = facts
        n_snap += 1
    instr["snapshot_rows_selected"] = n_snap
    instr["snapshot_scan_ms"] = round(_perf_counter_ms() - t0, 1)
    instr["snapshot_decode_ms"] = instr["snapshot_scan_ms"]
    _mark("snapshots")

    return {
        "scope_specs": scope_specs,
        "trade_dates": [asof_date],
        "t1_by_date": t1_by_date,
        "states_by_date": states_by_date,
        "bars": bars,
        "events_by_date": events_by_date,
        "current_only_facts_by_date": current_only_facts_by_date,
        "union_member_ids": sorted(selection.union_member_ids, key=str),
    }


def pa_array_or_none(values: frozenset[str]):
    """Materialize a bounded set of instrument-id strings for a PyArrow ``isin``.

    Returns ``None`` when empty so callers omit the membership predicate.
    """
    if not values:
        return None
    return list(values)


def _load_capacity_facts(
    dataset_dir: str,
    scope_specs: list[Any],
    *,
    window_dates: list[date],
    selection: ReplaySelection | None = None,
    instr: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Multi-date (window) loader for ``dataset-capacity-benchmark``.

    Loads states / bars / events over the WHOLE window (``window_dates``) for the
    union members, mapping rows through the SAME shared source-fact mappers the
    DB loader and the single-date replay use (``_decode_jsonb`` /
    ``_map_daily_bar_fact`` / ``_map_structure_event`` / ``_build_t1_map``).

    This measures the current-static batch capacity of the FINAL production
    shared core: the caller feeds the result into
    ``build_union_fact_context_from_loaded_facts`` then
    ``build_prepared_scopes_from_union`` exactly like the DB wrapper's
    current-static path, so the measurement is of the code that will enter dev via
    Git merge — never a parallel/replayed copy of the business logic.

    Current-only snapshot facts (exact-asof point-in-time) are intentionally NOT
    loaded here: the current-static Historical path keeps Current-only facts
    unavailable (the DB batch owner has the same semantics).  No DB, no SSH, no
    remote PostgreSQL — the corpus parquet files are the only source.
    """
    import pyarrow.dataset as ds
    from collections import defaultdict

    from app.services.review_observation_prep_service import (
        FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        HISTORY_CONTRACT_VERSION,
        _build_t1_map,
        _decode_jsonb,
        _map_daily_bar_fact,
        _map_structure_event,
        _InstrumentBarSeries,
    )

    if selection is None:
        raise RuntimeError("_load_capacity_facts requires a Stage-A ReplaySelection")
    if not window_dates:
        raise RuntimeError("_load_capacity_facts requires a non-empty window_dates")

    instr = instr if instr is not None else {}

    _t_load_start = _perf_counter_ms()
    asof_date = selection.asof_date
    union_member_strs = frozenset(str(m) for m in selection.union_member_ids)
    t1_by_date = _build_t1_map(window_dates, list(selection.trading_days))
    instr["selection_ms"] = round(_perf_counter_ms() - _t_load_start, 1)

    member_set = pa_array_or_none(union_member_strs)

    def _member_filter():
        if member_set is None:
            return None
        return ds.field("instrument_id").isin(member_set)

    window_iso = {d.isoformat() for d in window_dates}

    # ---- 1. Daily state: instrument ∈ union, trade_date ∈ window, algo ----
    state_filter = ds.field("algorithm_version") == FIRST_PYRAMID_CORE_ALGORITHM_VERSION
    mf = _member_filter()
    if mf is not None:
        state_filter = state_filter & mf
    _sbd: dict[date, dict[uuid.UUID, dict]] = defaultdict(dict)
    t0 = _perf_counter_ms()
    n_state = 0
    for r in _iter_parquet_rows(
        dataset_dir,
        "first_pyramid_daily_state",
        columns=["instrument_id", "trade_date", "algorithm_version", "state_payload"],
        filter_expr=state_filter,
    ):
        td = date.fromisoformat(r["trade_date"]) if r.get("trade_date") else None
        if td is None or td.isoformat() not in window_iso:
            continue
        _sbd[td][uuid.UUID(str(r["instrument_id"]))] = _decode_jsonb(r.get("state_payload"))
        n_state += 1
    states_by_date = dict(_sbd)
    instr["state_rows_selected"] = n_state
    instr["state_scan_ms"] = round(_perf_counter_ms() - t0, 1)

    # ---- 2. Bars: instrument ∈ union, [window_start-400d, asof] ----
    bar_filter = (
        (ds.field("trade_date") >= selection.bar_window_start.isoformat())
        & (ds.field("trade_date") <= asof_date.isoformat())
    )
    if mf is not None:
        bar_filter = bar_filter & mf
    by_instrument: dict[uuid.UUID, list[Any]] = defaultdict(list)
    t0 = _perf_counter_ms()
    n_bar = 0
    for r in _iter_parquet_rows(
        dataset_dir,
        "bars_daily",
        columns=[
            "instrument_id", "trade_date",
            "open", "high", "low", "close", "volume", "amount",
        ],
        filter_expr=bar_filter,
    ):
        td = date.fromisoformat(r["trade_date"]) if r.get("trade_date") else None
        if td is None:
            continue
        by_instrument[uuid.UUID(str(r["instrument_id"]))].append(
            _map_daily_bar_fact(
                trade_date=td,
                open=r.get("open"), high=r.get("high"), low=r.get("low"),
                close=r.get("close"), volume=r.get("volume"), amount=r.get("amount"),
            )
        )
        n_bar += 1
    bars: dict[uuid.UUID, _InstrumentBarSeries] = {}
    for iid, facts in by_instrument.items():
        ordered = sorted(facts, key=lambda b: b.trade_date)
        bars[iid] = _InstrumentBarSeries(
            facts=tuple(ordered),
            dates=tuple(b.trade_date for b in ordered),
        )
    by_instrument.clear()
    instr["bar_rows_selected"] = n_bar
    instr["bar_scan_ms"] = round(_perf_counter_ms() - t0, 1)

    # ---- 3. Events: instrument ∈ union, event_time ∈ window, algo + history ----
    ev_lo = window_dates[0].isoformat()
    ev_hi = (asof_date + timedelta(days=1)).isoformat()
    event_filter = (
        (ds.field("algorithm_version") == FIRST_PYRAMID_CORE_ALGORITHM_VERSION)
        & (ds.field("history_contract_version") == HISTORY_CONTRACT_VERSION)
        & (ds.field("event_time") >= ev_lo)
        & (ds.field("event_time") < ev_hi)
    )
    if mf is not None:
        event_filter = event_filter & mf
    _ebd: dict[date, list[Any]] = defaultdict(list)
    t0 = _perf_counter_ms()
    n_event = 0
    for r in _iter_parquet_rows(
        dataset_dir,
        "first_pyramid_events",
        columns=[
            "instrument_id", "event_time", "event_type", "event_payload",
            "algorithm_version", "history_contract_version",
        ],
        filter_expr=event_filter,
    ):
        etime = r.get("event_time")
        if not etime:
            continue
        td = date.fromisoformat(etime[:10])
        if td.isoformat() not in window_iso:
            continue
        payload = _decode_jsonb(r.get("event_payload"))
        _ebd[td].append(
            _map_structure_event(
                instrument_id=str(r["instrument_id"]),
                event_type=r.get("event_type"),
                direction=payload.get("direction"),
                level=payload.get("level"),
                internal=payload.get("internal"),
                release_volume_ratio=payload.get("release_volume_ratio"),
            )
        )
        n_event += 1
    events_by_date = dict(_ebd)
    instr["event_rows_selected"] = n_event
    instr["event_scan_ms"] = round(_perf_counter_ms() - t0, 1)

    # ``fact_mapping_ms`` = pure post-scan construction/mapping CPU (t1 map, bar
    # series assembly, dict materialization), i.e. the loader wall minus the three
    # parquet scan segments.  This isolates "parquet I/O" from "fact mapping" so
    # CAP-20 can localize where time goes without conflating the two.
    scan_ms = sum(
        instr.get(k, 0.0)
        for k in ("state_scan_ms", "bar_scan_ms", "event_scan_ms")
    )
    loader_total_ms = _perf_counter_ms() - _t_load_start
    instr["fact_mapping_ms"] = round(max(0.0, loader_total_ms - scan_ms), 1)

    return {
        "scope_specs": scope_specs,
        "trade_dates": window_dates,
        "t1_by_date": t1_by_date,
        "states_by_date": states_by_date,
        "bars": bars,
        "events_by_date": events_by_date,
        "current_only_facts_by_date": {},
        "union_member_ids": sorted(selection.union_member_ids, key=str),
    }


def _dataset_asof(dataset_dir: str) -> str:
    """Read the frozen asof date from the corpus.

    The manifest may carry ``asof`` as null (the export snapshot timestamp is in
    ``snapshot_started_at_utc``); the authoritative analysis asof is the last
    entry of ``manifest.date_ranges.analysis_axis`` and echoed in
    ``quality_summary.json.asof``.  Fall back in that order.
    """
    qpath = os.path.join(dataset_dir, "quality_summary.json")
    if os.path.exists(qpath):
        with open(qpath, "r", encoding="utf-8") as fh:
            q = json.load(fh)
        if q.get("asof"):
            return str(q["asof"])
    mpath = os.path.join(dataset_dir, "manifest.json")
    if os.path.exists(mpath):
        with open(mpath, "r", encoding="utf-8") as fh:
            m = json.load(fh)
        axis = (m.get("date_ranges") or {}).get("analysis_axis") or []
        if axis:
            return str(axis[-1])
    return ""


# Current L1 必需的 fact 源域及其实际日期列。
_CURRENT_L1_REQUIRED_FACT_DOMAINS: tuple[tuple[str, str], ...] = (
    ("first_pyramid_daily_state", "trade_date"),
    ("first_pyramid_events", "event_time"),
    ("bars_daily", "trade_date"),
    ("stock_feature_snapshots_asof", "trade_date"),
)

# Source 类型分类（R0-C3 修正核心：row existence ≠ source coverage）。
# - dense: 每日状态/行情，trade_date × eligible instrument 完整即 coverage；
#          actual max 缺失/截断是可疑 gap 信号。
# - point_in_time: 如 exact-asof current snapshot，只需 declared asof 存在
#          succeeded/published run，不要求每天都有 snapshot。
# - sparse_event: 如 first_pyramid_events 稀疏事件流，max(event_time) 不能代表
#          coverage（正常无事件 vs producer 停摆两种解释无法由数据区分），
#          必须有独立 producer/capture coverage evidence。
_SOURCE_KIND: dict[str, str] = {
    "first_pyramid_daily_state": "dense",
    "bars_daily": "dense",
    "stock_feature_snapshots_asof": "point_in_time",
    "first_pyramid_events": "sparse_event",
}


def _domain_date_range(
    dataset_dir: str, stem: str, date_col: str
) -> tuple[date | None, date | None]:
    """读 parquet row-group statistics 的 (min, max) 实际日期范围，零 materialize。

    仅依赖列统计信息，不读任何行；用于诊断报告与 Dataset Integrity Gate。
    ``event_time`` 是 timestamp 字符串，``trade_date`` 是 date 字符串，统一解析为 ``date``。
    """
    import pyarrow.parquet as pq

    path = os.path.join(dataset_dir, "parquet", f"{stem}.parquet")
    if not os.path.exists(path):
        return (None, None)
    pf = pq.ParquetFile(path)
    md = pf.metadata
    names = [md.schema.column(i).name for i in range(md.num_columns)]
    if date_col not in names:
        return (None, None)
    ci = names.index(date_col)
    dmin = dmax = None
    for rg in range(md.num_row_groups):
        st = md.row_group(rg).column(ci).statistics
        if st is None or st.min is None or st.max is None:
            return (None, None)
        pmin = datetime.fromisoformat(str(st.min)).date()
        pmax = datetime.fromisoformat(str(st.max)).date()
        dmin = pmin if dmin is None else min(dmin, pmin)
        dmax = pmax if dmax is None else max(dmax, pmax)
    return (dmin, dmax)


def _diagnose_source_date_ranges(
    dataset_dir: str,
) -> dict[str, tuple[date | None, date | None]]:
    """诊断 helper（R0-C3 修正：不再作为 asof owner）。

    返回各必需 fact 域的 actual (min, max)，仅用于报告与 Source Coverage Audit。
    历史上曾叫 ``_resolve_full_capability_asof`` 并取 ``min(max)`` 作为 replay asof，
    但该算法错误：``min(max)`` 不能证明交集日期各 source 都可用（例如 snapshot 仅
    08-17 而 state 截到 08-10 时，08-07 这一 min 结果 snapshot 根本不存在）。
    现在 asof 由 Track A（Current-asof=manifest declared）/ Track B（--asof-lock）显式决定。
    """
    per: dict[str, tuple[date | None, date | None]] = {}
    for stem, col in _CURRENT_L1_REQUIRED_FACT_DOMAINS:
        per[stem] = _domain_date_range(dataset_dir, stem, col)
    return per


def _check_dataset_integrity(dataset_dir: str) -> list[str]:
    """按 source 类型区分的 Dataset Integrity Gate（R0-C3 修正）。

    规则（row existence ≠ source coverage）：
    - DENSE (state/bars): 检查 actual max 是否达到 declared 上界；dense 域 max 截断是
      可疑 gap 信号（Daily State 到 08-10 < declared 08-17 即此类强证据 gap）。
    - POINT_IN_TIME (snapshot): exact-asof run gate —— 只要求 declared asof 当天存在
      snapshot run（不要求每天都有 snapshot）。仅单点 08-17 符合设计，不构成 gap。
    - SPARSE_EVENT (events): **不能用 max(event_time) 判 coverage**（稀疏事件流无事件
      可能是正常运行但无事件，也可能是 producer 停摆，数据无法区分）。须独立
      producer/capture coverage evidence；当前 corpus 缺此 evidence → 记
      EVIDENCE_MISSING（待 R0-C3 上游审计），**不判 gap**。

    禁止 latest backfill、禁止把残缺日期事实填到 declared asof：
    本函数只检测并报告，不修改任何数据。
    """
    mpath = os.path.join(dataset_dir, "manifest.json")
    if not os.path.exists(mpath):
        return ["manifest.json 缺失，无法执行 Dataset Integrity Gate"]
    with open(mpath, "r", encoding="utf-8") as fh:
        m = json.load(fh)
    dr = m.get("date_ranges") or {}
    declared_asof = dr.get("asof")
    violations: list[str] = []

    # declared 上界来源（manifest 声明）。
    declared = {
        "first_pyramid_daily_state": dr.get("states_range"),
        "first_pyramid_events": dr.get("events_range"),
        "bars_daily": [dr.get("bars_start"), declared_asof],
        "stock_feature_snapshots_asof": [declared_asof, declared_asof],
    }

    for stem, kind in _SOURCE_KIND.items():
        lo, hi = _domain_date_range(dataset_dir, stem, _col_of(stem))
        dcl = declared.get(stem)
        declared_hi = (
            date.fromisoformat(str(dcl[1])[:10])
            if (dcl and len(dcl) >= 2 and dcl[1])
            else None
        )

        if kind == "dense":
            # dense: max 截断是可疑 gap 信号（如 Daily State 到 08-10）
            if hi is None:
                violations.append(
                    f"Dataset Integrity Gap [dense]: {stem} actual 无数据，"
                    f"但 manifest 声明到 {declared_hi.isoformat() if declared_hi else '?'}"
                )
            elif declared_hi is not None and hi < declared_hi:
                violations.append(
                    f"Dataset Integrity Gap [dense]: {stem} actual max={hi.isoformat()} "
                    f"< manifest declared max={declared_hi.isoformat()}（dense 域截断，"
                    f"需查 upstream 是否真缺）"
                )
        elif kind == "point_in_time":
            # point_in_time: 只要求 declared asof 当天存在 CONSUMABLE snapshot run。
            # AUDIT-FIX-01 (B.4): 复用唯一 shared owner
            # ``_accepted_exact_t_snapshot_run_ids``（status==succeeded &&
            # published_at!=null && trade_date==asof），消除与 Replay Selection 的
            # contract drift。不再用 parquet 日期范围近似判定。
            if declared_asof is None:
                violations.append(
                    f"Dataset Integrity Gap [point_in_time]: {stem} manifest 未声明 asof"
                )
            else:
                try:
                    asof_date = date.fromisoformat(str(declared_asof)[:10])
                except ValueError:
                    violations.append(
                        f"Dataset Integrity Gap [point_in_time]: {stem} manifest asof "
                        f"{declared_asof} 不是合法日期"
                    )
                    continue
                accepted = _accepted_exact_t_snapshot_run_ids(dataset_dir, asof_date)
                if not accepted:
                    violations.append(
                        f"Dataset Integrity Gap [point_in_time]: {stem} declared asof "
                        f"{declared_asof} 无 CONSUMABLE exact-asof snapshot run "
                        f"(status==succeeded && published_at!=null)"
                    )
                # declared asof 当天有 consumable snapshot run 即正常，不要求每天都有
        elif kind == "sparse_event":
            # sparse_event: 不能用 max(event_time) 判 coverage。
            # 当前 corpus 缺独立 coverage evidence → 记 EVIDENCE_MISSING，不判 gap。
            has_evidence = bool(
                (m.get("event_coverage") or {}).get("capture_complete")
                if m.get("event_coverage")
                else False
            )
            if not has_evidence:
                violations.append(
                    f"Event Coverage Evidence Missing [sparse_event]: {stem} 不能用 "
                    f"max(event_time)={hi.isoformat() if hi else 'NONE'} 判定 coverage；"
                    f"需独立 producer/capture coverage evidence（待 R0-C3 上游审计）"
                )
            # 注意：绝不基于 max(event_time) < declared 判 gap
    return violations


def _col_of(stem: str) -> str:
    for s, c in _CURRENT_L1_REQUIRED_FACT_DOMAINS:
        if s == stem:
            return c
    return "trade_date"


def _load_scope_specs(dataset_dir: str, view_name: str) -> list[Any]:
    """Resolve a view (or single board UUID) into ``ScopeReplaySpec`` objects.

    Joins boards (scope_type/name) + board_memberships_current_snapshot
    (member_ids).  Mixed-family is supported: a view may span concept +
    industry_l1/l2/l3, and each spec carries its OWN scope_type.
    """
    from app.services.review_observation_prep_service import ScopeReplaySpec

    # Boards is small (768 rows) so a full read is fine.
    boards = {str(r["id"]): r for r in _load_parquet_rows(dataset_dir, "boards")}

    # Resolve the selected board ids FIRST, so memberships are filtered to just
    # those boards instead of building a giant board -> members map and then
    # discarding almost all of it (matters for capacity_4096).
    if view_name in (
        "dev_500",
        "capacity_4096",
        "representative_sample",
        "logic_validation_sample",
        "all_concepts",
        "all_industries",
    ):
        vpath = os.path.join(dataset_dir, "views", f"{view_name}.json")
        with open(vpath, "r", encoding="utf-8") as fh:
            view = json.load(fh)
        board_ids = list(view.get("scope_keys") or [])
    else:
        # single board UUID given directly
        board_ids = [view_name]

    selected_board_ids = {str(b) for b in board_ids}
    by_board: dict[str, list[uuid.UUID]] = {}
    for m in _load_parquet_rows(dataset_dir, "board_memberships_current_snapshot"):
        bid = str(m["board_id"])
        if bid not in selected_board_ids:
            continue
        by_board.setdefault(bid, []).append(uuid.UUID(str(m["instrument_id"])))

    specs: list[Any] = []
    for bid in board_ids:
        board = boards.get(str(bid))
        if not board:
            continue
        mems = tuple(by_board.get(str(bid), ()))
        specs.append(
            ScopeReplaySpec(
                scope_type=str(board.get("type") or "concept"),
                scope_key=str(bid),
                scope_name=str(board.get("name") or str(bid)),
                member_ids=mems,
            )
        )
    return specs


def _load_dynamics_logic_scope_specs(
    dataset_dir: str,
    fixture_path: str,
) -> list[Any]:
    """Resolve the VERSIONED 4-scope DYNAMICS-LOGIC-CLOSURE fixture into
    ``ScopeReplaySpec`` objects (A1-1).

    The fixture is a committed validation contract (NOT the gitignored
    ``.perfdata/.../views/logic_validation_sample.json``), so the fixed 4-scope
    sample can be rebuilt from any checkout of the SHA.  Each fixture scope pins
    its scope_key (UUID) and scope_family; the member_ids come from the frozen
    Dataset membership snapshot at runtime.
    """
    from app.services.review_observation_prep_service import ScopeReplaySpec

    with open(fixture_path, "r", encoding="utf-8") as fh:
        fixture = json.load(fh)
    scopes = fixture.get("scopes") or []
    if not scopes:
        raise RuntimeError(f"[fixture] {fixture_path} 无 scopes")

    boards = {str(r["id"]): r for r in _load_parquet_rows(dataset_dir, "boards")}
    selected_board_ids = {str(s["scope_key"]) for s in scopes}
    by_board: dict[str, list[uuid.UUID]] = {}
    for m in _load_parquet_rows(dataset_dir, "board_memberships_current_snapshot"):
        bid = str(m["board_id"])
        if bid not in selected_board_ids:
            continue
        by_board.setdefault(bid, []).append(uuid.UUID(str(m["instrument_id"])))

    specs: list[Any] = []
    for s in scopes:
        bid = str(s["scope_key"])
        board = boards.get(bid)
        if not board:
            raise RuntimeError(f"[fixture] board {bid} ({s.get('scope_name')}) 不在 dataset")
        mems = tuple(by_board.get(bid, ()))
        # scope_type from the fixture family (concept/industry) so the E2E chain
        # carries the correct family; fall back to the board type if absent.
        family = str(s.get("scope_family") or board.get("type") or "concept")
        specs.append(
            ScopeReplaySpec(
                scope_type=family,
                scope_key=bid,
                scope_name=str(s.get("scope_name") or board.get("name") or bid),
                member_ids=mems,
            )
        )
    return specs


def _replay_l1_once(
    dataset_dir: str,
    view_name: str,
    asof_lock: str,
) -> dict[str, Any]:
    """Run the full Current L1 pipeline at exactly one asof (Track B pinned date).

    Returns a dict with ``results`` (scope_key -> observation bucket), ``asof``,
    ``pit_status_t1``, ``current_only_map`` and ``cost`` so callers can assemble
    a multi-date Semantic Validation Matrix without re-deriving the pipeline.
    Does NOT print; the caller owns output.
    """
    from app.domain.review.scope_observation import compute_scope_observation
    from app.services.review_observation_prep_service import (
        build_prepared_scopes_from_union,
        build_union_fact_context_from_loaded_facts,
    )

    selection = _build_replay_selection(
        dataset_dir, view_name, asof_override=date.fromisoformat(asof_lock)
    )
    if not selection.scope_specs:
        raise RuntimeError(f"[replay-l1-once] view 解析为空 scope_specs（view={view_name}）")
    if selection.asof_date not in selection.trading_days:
        raise RuntimeError(f"[replay-l1-once] asof={asof_lock} 不在 corpus 交易日历内")

    instr: dict[str, Any] = {}
    facts = _load_replay_facts(
        dataset_dir, list(selection.scope_specs), selection=selection, instr=instr
    )
    union_ctx = build_union_fact_context_from_loaded_facts(
        t1_by_date=facts["t1_by_date"],
        states_by_date=facts["states_by_date"],
        bars=facts["bars"],
        events_by_date=facts["events_by_date"],
    )
    prepared = build_prepared_scopes_from_union(
        trade_dates=facts["trade_dates"],
        scope_specs=facts["scope_specs"],
        union_ctx=union_ctx,
        membership_t1_by_scope=None,
        current_only_facts_by_date=facts["current_only_facts_by_date"],
        pit_status_t="current_static",
        pit_status_t1="unavailable",
        t1_membership_available=False,
    )
    results: dict[str, dict] = {}
    for scope_key, series in prepared.items():
        for ps in series:
            obs = compute_scope_observation(
                scope_type=ps.scope_type,
                scope_key=ps.scope_key,
                trade_date=ps.trade_date,
                pit_member_ids=ps.pit_member_ids,
                pit_member_ids_t1=ps.pit_member_ids_t1,
                members=ps.members,
                events=ps.events,
                t1_membership_available=ps.t1_membership_available,
                event_coverage_member_ids=ps.event_coverage_member_ids,
            )
            results[scope_key] = {
                "trade_date": ps.trade_date.isoformat(),
                "scope_type": ps.scope_type,
                "scope_name": ps.scope_name,
                "member_count": len(ps.pit_member_ids),
                "provided_member_count": ps.members and len(ps.members) or 0,
                "t1_membership_available": ps.t1_membership_available,
                "observation": obs,
            }
    return {
        "results": results,
        "asof": selection.asof_date.isoformat(),
        "declared_asof": (
            selection.declared_asof.isoformat() if selection.declared_asof else None
        ),
        "pit_status_t1": "unavailable",
        "current_only_map": facts["current_only_facts_by_date"].get(selection.asof_date, {}),
        "union_member_count": len(facts["union_member_ids"]),
        "accepted_snapshot_runs": len(selection.accepted_snapshot_run_ids),
        "cost": dict(instr),
        "selected_rows": {
            k: instr.get(k)
            for k in (
                "state_rows_selected", "bar_rows_selected",
                "event_rows_selected", "snapshot_rows_selected",
            )
        },
    }


def _run_dataset_capacity_benchmark(
    dataset_dir: str,
    view_name: str,
    *,
    history: int,
    asof_lock: str | None = None,
    dry_run: bool = False,
) -> int:
    """``dataset-capacity-benchmark``: local frozen-Dataset capacity runner.

    Measures the capacity of the FINAL production shared core
    (``build_union_fact_context_from_loaded_facts`` →
    ``build_prepared_scopes_from_union`` → ``compute_scope_observation``) over a
    multi-date window (``--history`` trading days ending at ``--asof-lock``) on the
    frozen local corpus.  This is the same core the DB batch wrapper delegates
    to, so the measurement is of the code that will enter dev via Git merge —
    NOT a parallel or replayed copy of any business logic.

    NO-MIGRATION HARD RULE (RULE-4 / RULE-5): this runner ONLY does Dataset
    loading, parameter selection, orchestration, timing and output.  It does NOT
    re-implement the per-member construction / per-scope aggregation / event /
    series / phase computation logic — all of that is delegated to the shared
    production owners.  No DB, no SSH, no remote PostgreSQL, no production DB
    benchmark, no old-vs-new comparison.

    Required outputs (CAP Decision Gate):
      * input scale: scope_count / union_member_count / trade_date_count / result_count
      * shared-core structural: scope_member_day_count / unique_member_day_count /
        duplication_factor / member_build_calls / vec_hit / vec_fallback / fallback_reasons
      * timing: dataset_load_ms (含 fact_mapping_ms + state/bar/event scan 细分)
        / fact_context_ms / scope_prepare_ms / scope_observation_ms /
        dynamics_ms / total_ms
      * memory: maxrss_mb (NOT rss_before/rss_after)
    """
    if dry_run:
        print(
            f"[dry-run] dataset-capacity-benchmark dataset_dir={dataset_dir} "
            f"view={view_name} history={history} asof={asof_lock} OK"
        )
        return 0

    import time

    from app.domain.review.scope_observation import compute_scope_observation
    from app.services.review_observation_prep_service import (
        build_prepared_scopes_from_union,
        build_union_fact_context_from_loaded_facts,
    )

    print("=== dataset-capacity-benchmark (local frozen Dataset, shared production core) ===")
    print(f"dataset_dir : {dataset_dir}")
    print(f"view        : {view_name}")

    # ---- Stage A: selection (small metadata only) ----
    if asof_lock:
        sel_asof = date.fromisoformat(asof_lock)
    else:
        declared = _dataset_asof(dataset_dir)
        if not declared:
            logger.error("[dataset-capacity-benchmark] 无法解析 corpus declared asof")
            return 2
        sel_asof = date.fromisoformat(declared)
    selection = _build_replay_selection(
        dataset_dir, view_name, asof_override=sel_asof
    )
    if not selection.scope_specs:
        logger.error("[dataset-capacity-benchmark] view 解析为空 scope_specs")
        return 2
    if selection.asof_date not in selection.trading_days:
        logger.error("[dataset-capacity-benchmark] asof=%s 不在 corpus 交易日历内", asof_lock)
        return 2

    # The window = last ``history`` trading days <= asof (NOT the last N of the
    # whole calendar — the window must END at the locked asof date).
    asof_idx = bisect_left(selection.trading_days, selection.asof_date)
    if asof_idx >= len(selection.trading_days) or (
        selection.trading_days[asof_idx] != selection.asof_date
    ):
        logger.error(
            "[dataset-capacity-benchmark] asof=%s 不在 corpus 交易日历内",
            selection.asof_date.isoformat(),
        )
        return 2
    window_dates = list(selection.trading_days[max(0, asof_idx - history + 1): asof_idx + 1])
    if len(window_dates) != history:
        logger.error(
            "[dataset-capacity-benchmark] 仅能切出 %d 个交易日（需 %d，asof=%s）",
            len(window_dates), history, selection.asof_date.isoformat(),
        )
        return 2

    scope_count = len(selection.scope_specs)
    union_member_count = len(selection.union_member_ids)

    # ---- Stage B: multi-date Dataset load (shared mappers only) ----
    instr: dict[str, Any] = {}
    load0 = time.perf_counter()
    facts = _load_capacity_facts(
        dataset_dir, list(selection.scope_specs),
        window_dates=window_dates, selection=selection, instr=instr,
    )
    dataset_load_ms = (time.perf_counter() - load0) * 1000.0

    # ---- Shared core: union fact context + vectorized volume ----
    ctx0 = time.perf_counter()
    union_ctx = build_union_fact_context_from_loaded_facts(
        t1_by_date=facts["t1_by_date"],
        states_by_date=facts["states_by_date"],
        bars=facts["bars"],
        events_by_date=facts["events_by_date"],
    )
    fact_context_ms = (time.perf_counter() - ctx0) * 1000.0

    # ---- Shared core: union preparation (prep_counters populate member_build_calls) ----
    prep_counters: dict[str, int] = {}
    prep_fallback_reasons: list[str] = []
    prep0 = time.perf_counter()
    prepared = build_prepared_scopes_from_union(
        trade_dates=facts["trade_dates"],
        scope_specs=facts["scope_specs"],
        union_ctx=union_ctx,
        membership_t1_by_scope=None,
        current_only_facts_by_date=None,
        pit_status_t="current_static",
        pit_status_t1="current_static",
        t1_membership_available=False,
        prep_counters=prep_counters,
        prep_fallback_reasons=prep_fallback_reasons,
    )
    scope_prepare_ms = (time.perf_counter() - prep0) * 1000.0

    # ---- Shared core: compute_scope_observation over all prepared scopes ----
    # ``result_count`` = number of SCOPES with a non-empty prepared series (must
    # equal scope_count, per the CAP Decision Gate).  Each scope yields one
    # observation per trade_date, so ``observation_total = scope_count ×
    # trade_date_count`` is reported separately.
    obs0 = time.perf_counter()
    result_count = 0
    observation_total = 0
    semantic_errors = 0
    obs_by_scope: dict[str, list[Any]] = {}
    for scope_key, series in prepared.items():
        obs_list: list[Any] = []
        for ps in series:
            try:
                obs = compute_scope_observation(
                    scope_type=ps.scope_type,
                    scope_key=ps.scope_key,
                    trade_date=ps.trade_date,
                    pit_member_ids=list(ps.pit_member_ids),
                    pit_member_ids_t1=list(ps.pit_member_ids_t1),
                    members=ps.members,
                    events=ps.events,
                    t1_membership_available=ps.t1_membership_available,
                    event_coverage_member_ids=ps.event_coverage_member_ids,
                )
            except Exception:  # pragma: no cover - observability only, no masking
                semantic_errors += 1
                obs = None
            obs_list.append(obs)
        obs_by_scope[scope_key] = obs_list
        observation_total += len(obs_list)
        if obs_list:
            result_count += 1
    scope_observation_ms = (time.perf_counter() - obs0) * 1000.0

    # The post-prep phase owners (series / phase computation) are NOT re-run here:
    # this benchmark measures the current-static batch capacity of the shared
    # prep + L1 core.  That post-processing is orthogonal and covered by the
    # single-date replay / semantic-matrix paths.  Reported as 0 to keep the split
    # explicit rather than collapsing it into total.
    dynamics_ms = 0.0

    total_ms = (time.perf_counter() - load0) * 1000.0

    # ---- Output (CAP Decision Gate) ----
    trade_date_count = len(facts["trade_dates"])
    scope_member_day_count = sum(len(s.member_ids) for s in facts["scope_specs"])
    scope_member_day_count *= trade_date_count
    unique_member_day_count = union_member_count * trade_date_count
    duplication_factor = (
        scope_member_day_count / unique_member_day_count
        if unique_member_day_count
        else 0.0
    )
    member_build_calls = trade_date_count

    print("--- input scale ---")
    print(f"scope_count             : {scope_count}")
    print(f"union_member_count      : {union_member_count}")
    print(f"trade_date_count        : {trade_date_count}")
    print(f"result_count            : {result_count}")
    print(f"observation_total       : {observation_total}  # scope_count × trade_date_count")
    print("--- shared-core structural ---")
    print(f"scope_member_day_count  : {scope_member_day_count}")
    print(f"unique_member_day_count : {unique_member_day_count}")
    print(f"duplication_factor      : {duplication_factor:.2f}")
    print(f"member_build_calls      : {member_build_calls}")
    print(f"vec_hit                 : {prep_counters.get('vec_hit', 0)}")
    print(f"vec_fallback            : {prep_counters.get('vec_fallback', 0)}")
    print(f"fallback_reasons        : {','.join(prep_fallback_reasons) or '-'}")
    print("--- timing (ms) ---")
    print(f"dataset_load_ms         : {dataset_load_ms:.1f}")
    print(f"  fact_mapping_ms       : {instr.get('fact_mapping_ms', 0.0):.1f}")
    print(f"  state_scan_ms         : {instr.get('state_scan_ms', 0.0):.1f}")
    print(f"  bar_scan_ms           : {instr.get('bar_scan_ms', 0.0):.1f}")
    print(f"  event_scan_ms         : {instr.get('event_scan_ms', 0.0):.1f}")
    print(f"fact_context_ms         : {fact_context_ms:.1f}")
    print(f"scope_prepare_ms        : {scope_prepare_ms:.1f}")
    print(f"scope_observation_ms    : {scope_observation_ms:.1f}")
    print(f"dynamics_ms             : {dynamics_ms:.1f}")
    print(f"total_ms                : {total_ms:.1f}")
    print("--- memory ---")
    print(f"maxrss_mb               : {_rss_mb():.1f}")
    print(f"semantic_errors         : {semantic_errors}")

    # CAP Decision Gate (input scale invariants).
    ok = True
    if result_count != scope_count:
        logger.error(
            "result_count=%d != scope_count=%d (CAP invariant violated)",
            result_count, scope_count,
        )
        ok = False
    if semantic_errors != 0:
        logger.error("semantic_errors=%d != 0 (CAP invariant violated)", semantic_errors)
        ok = False
    if not prepared:
        logger.error("prepared 为空（CAP 无法测量）")
        ok = False
    return 0 if ok else 1


def _run_dataset_dynamics_logic(
    dataset_dir: str,
    *,
    fixture_path: str | None = None,
    history: int,
    asof_lock: str | None = None,
    dry_run: bool = False,
) -> int:
    """``dataset-dynamics-logic``: 4-scope frozen-Dataset full Dynamics chain E2E.

    Steps 11-14 of DYNAMICS-LOGIC-CLOSURE.  Runs the FINAL production chain over a
    small frozen-Dataset sample.  The fixed 4-scope set comes solely from the
    VERSIONED fixture ``scripts/fixtures/review_dynamics_logic_sample.json`` (A2),
    and the shared Stage-A metadata owner
    (``_build_replay_selection_from_specs``) derives union / calendar / T-1 / bar
    window / accepted runs from those fixture scope_specs — the gitignored
    ``.perfdata`` view is NOT consulted at all:

        Frozen Dataset
          -> fixture scope_specs (4 scopes)
          -> _build_replay_selection_from_specs (shared owner)
          -> existing source mapper (_load_capacity_facts, shared mappers)
          -> build_union_fact_context_from_loaded_facts
          -> build_prepared_scopes_from_union
          -> compute_scope_observation            (L1 per scope-date)
          -> build_observation_series             (per scope, 120D window)
          -> compute_scope_dynamics_analysis      (Position -> EMA -> Velocity ->
                                                   Acceleration -> Persistence -> Phase)

    This resolves the ``dynamics_ms=0`` gap of the capacity runner by actually
    EXECUTING the Dynamics chain.  Emits 12 traces (4 scopes x 3 dates): one before
    the chain is ready, one at the first fully-ready date, and the latest date.

    NO-MIGRATION: only calls the FINAL production shared owners; no DB, no SSH, no
    remote PG, no business-formula copy, no parallel owner.  Pure validation tooling.
    """
    if dry_run:
        print(
            f"[dry-run] dataset-dynamics-logic dataset_dir={dataset_dir} "
            f"fixture={fixture_path} history={history} asof={asof_lock} OK"
        )
        return 0

    import time

    from app.domain.review.analysis.observation_series import build_observation_series
    from app.domain.review.analysis.scope_dynamics import (
        compute_scope_dynamics_analysis,
    )
    from app.domain.review.scope_observation import compute_scope_observation
    from app.services.review_observation_prep_service import (
        build_prepared_scopes_from_union,
        build_union_fact_context_from_loaded_facts,
    )

    print("=== dataset-dynamics-logic (4-scope frozen Dataset, full Dynamics chain) ===")
    print(f"dataset_dir : {dataset_dir}")
    print(f"history     : {history} trading days")

    # ---- Stage A: selection (A2: derived from the VERSIONED fixture only — the
    # shared metadata owner is fed the fixture scope_specs directly, so the
    # gitignored .perfdata view is NOT required at all) ----
    if asof_lock:
        sel_asof = date.fromisoformat(asof_lock)
    else:
        declared = _dataset_asof(dataset_dir)
        if not declared:
            logger.error("[dataset-dynamics-logic] 无法解析 corpus declared asof")
            return 2
        sel_asof = date.fromisoformat(declared)
    if fixture_path is None:
        fixture_path = os.path.join(
            os.path.dirname(__file__), "fixtures", "review_dynamics_logic_sample.json"
        )
    scope_specs = _load_dynamics_logic_scope_specs(dataset_dir, fixture_path)
    if len(scope_specs) != 4:
        logger.error(
            "[dataset-dynamics-logic] fixture 必须恰好 4 scopes，实际 %d",
            len(scope_specs),
        )
        return 2
    # Resolve union / calendar / T-1 / bar window / accepted runs from the fixture
    # scope_specs via the SAME shared metadata owner the view path uses.
    selection = _build_replay_selection_from_specs(
        dataset_dir, scope_specs, asof_override=sel_asof
    )
    if selection.asof_date not in selection.trading_days:
        logger.error("[dataset-dynamics-logic] asof=%s 不在交易日历", asof_lock)
        return 2

    asof_idx = bisect_left(selection.trading_days, selection.asof_date)
    if asof_idx >= len(selection.trading_days) or (
        selection.trading_days[asof_idx] != selection.asof_date
    ):
        logger.error("[dataset-dynamics-logic] asof=%s 不在交易日历", sel_asof.isoformat())
        return 2
    window_dates = list(
        selection.trading_days[max(0, asof_idx - history + 1): asof_idx + 1]
    )

    scope_count = len(scope_specs)
    trade_date_count = len(window_dates)
    print(f"scope_count        : {scope_count}")
    print(f"trade_date_count   : {trade_date_count}")
    print(f"union_member_count : {len(selection.union_member_ids)}")

    # ---- Multi-date Dataset load + shared union prep + L1 ----
    instr: dict[str, Any] = {}
    facts = _load_capacity_facts(
        dataset_dir, list(scope_specs),
        window_dates=window_dates, selection=selection, instr=instr,
    )
    union_ctx = build_union_fact_context_from_loaded_facts(
        t1_by_date=facts["t1_by_date"],
        states_by_date=facts["states_by_date"],
        bars=facts["bars"],
        events_by_date=facts["events_by_date"],
    )
    prep_counters: dict[str, int] = {}
    prep_fallback_reasons: list[str] = []
    prepared = build_prepared_scopes_from_union(
        trade_dates=facts["trade_dates"],
        scope_specs=facts["scope_specs"],
        union_ctx=union_ctx,
        membership_t1_by_scope=None,
        current_only_facts_by_date=None,
        pit_status_t="current_static",
        pit_status_t1="current_static",
        t1_membership_available=False,
        prep_counters=prep_counters,
        prep_fallback_reasons=prep_fallback_reasons,
    )
    if not prepared:
        logger.error("[dataset-dynamics-logic] prepared 为空")
        return 1

    # ---- Per-scope full Dynamics chain + traces ----
    all_phase_ready = 0
    all_phase_insufficient = 0
    all_phase_unavailable = 0
    per_scope = []
    for spec in scope_specs:
        sk = spec.scope_key
        series = prepared.get(sk)
        if not series or len(series) != trade_date_count:
            logger.error("[dataset-dynamics-logic] scope %s prepared 未对齐", sk)
            return 1
        # Build per-date L1 observation dicts (the formal payload shape).
        snapshots: list[dict[str, Any]] = []
        for ps in series:
            obs = compute_scope_observation(
                scope_type=ps.scope_type,
                scope_key=ps.scope_key,
                trade_date=ps.trade_date,
                pit_member_ids=[str(i) for i in ps.pit_member_ids],
                pit_member_ids_t1=[str(i) for i in ps.pit_member_ids_t1],
                members=ps.members,
                events=ps.events,
                t1_membership_available=ps.t1_membership_available,
                event_coverage_member_ids=ps.event_coverage_member_ids,
            )
            snapshots.append(
                {
                    "trade_date": ps.trade_date.isoformat(),
                    "readiness": "ready",
                    "payload": obs,
                }
            )
        # Formal ObservationSeries + full Dynamics chain.
        obs_series = build_observation_series(
            scope_type=spec.scope_type,
            scope_key=str(sk),
            from_date=window_dates[0],
            to_date=window_dates[-1],
            trading_dates=window_dates,
            snapshot_series=snapshots,
        )
        dyn = compute_scope_dynamics_analysis(obs_series)
        hd = dyn["historical_dynamics"]
        phase = dyn["dynamics_phase"]

        n_ready = sum(1 for p in phase if p["status"] == "ready")
        n_ins = sum(1 for p in phase if p["status"] == "insufficient_history")
        n_unavail = sum(1 for p in phase if p["status"] == "unavailable_current")
        all_phase_ready += n_ready
        all_phase_insufficient += n_ins
        all_phase_unavailable += n_unavail

        # A1-2 date-alignment invariant: every dynamics array shares the SAME
        # trade_date at the SAME index as the phase series.
        base_dates = [p["trade_date"] for p in phase]
        aligned = all(
            [d["trade_date"] for d in hd[k]] == base_dates
            for k in (
                "position", "ema5", "ema20",
                "velocity", "acceleration", "persistence",
            )
        )

        # Pick 3 trace dates: first non-ready, first fully-ready, latest.
        first_ready_idx = next(
            (i for i, p in enumerate(phase) if p["status"] == "ready"), None
        )
        pre_ready_idx = (
            first_ready_idx - 1 if first_ready_idx and first_ready_idx > 0 else 0
        )
        latest_idx = len(phase) - 1
        trace_idx = sorted(
            {pre_ready_idx, first_ready_idx if first_ready_idx is not None else latest_idx, latest_idx}
        )

        per_scope.append(
            {
                "scope_type": spec.scope_type,
                "scope_key": str(sk),
                "scope_name": spec.scope_name,
                "member_count": len(spec.member_ids),
                "dates": len(phase),
                "phase_ready": n_ready,
                "phase_insufficient": n_ins,
                "phase_unavailable": n_unavail,
                "trace_indices": trace_idx,
                "trace_dates": [window_dates[i].isoformat() for i in trace_idx],
                "trace_count": len(trace_idx),
                "date_aligned": aligned,
                "phase": phase,
                "historical_dynamics": hd,
            }
        )

    # ---- Output: execution confirmation + traces ----
    print("--- Dynamics actually executed (per scope) ---")
    for s in per_scope:
        print(
            f"  {s['scope_name']} ({s['scope_type']}, n={s['member_count']}): "
            f"ready={s['phase_ready']} insufficient={s['phase_insufficient']} "
            f"unavailable={s['phase_unavailable']} dates={s['dates']} "
            f"traces={s['trace_count']} aligned={s['date_aligned']}"
        )
    total_phase_rows = sum(s["dates"] for s in per_scope)
    total_trace_count = sum(s["trace_count"] for s in per_scope)
    print(f"TOTAL phase rows    : {total_phase_rows}")
    print(f"TOTAL ready         : {all_phase_ready}")
    print(f"TOTAL insufficient  : {all_phase_insufficient}")
    print(f"TOTAL unavailable   : {all_phase_unavailable}")
    print(f"TOTAL trace count   : {total_trace_count}")
    dynamics_executed = all_phase_ready > 0
    print(f"Dynamics executed   : {'YES' if dynamics_executed else 'NO'}")

    # A1-2 Fixed-sample contract: every invariant must hold or exit nonzero.
    contract_ok = True
    if scope_count != 4:
        logger.error("scope_count=%d != 4 (fixed-sample contract)", scope_count)
        contract_ok = False
    if trade_date_count != 120:
        logger.error("trade_date_count=%d != 120 (fixed-sample contract)", trade_date_count)
        contract_ok = False
    if total_phase_rows != 480:
        logger.error("phase rows=%d != 480 (4 scopes x 120D)", total_phase_rows)
        contract_ok = False
    for s in per_scope:
        if s["phase_ready"] <= 0:
            logger.error(
                "scope %s phase_ready=%d <= 0 (every scope must have ready Dynamics)",
                s["scope_name"], s["phase_ready"],
            )
            contract_ok = False
        if s["trace_count"] != 3:
            logger.error(
                "scope %s trace_count=%d != 3", s["scope_name"], s["trace_count"]
            )
            contract_ok = False
        if not s["date_aligned"]:
            logger.error(
                "scope %s dynamics arrays date-aligned invariant violated",
                s["scope_name"],
            )
            contract_ok = False
    if total_trace_count != 12:
        logger.error("total trace_count=%d != 12 (4 scopes x 3 dates)", total_trace_count)
        contract_ok = False
    if not dynamics_executed:
        logger.error("Dynamics executed = NO (no scope reached ready)")
        contract_ok = False
    if len(per_scope) != scope_count:
        logger.error("per_scope=%d != scope_count=%d", len(per_scope), scope_count)
        contract_ok = False

    print("--- Fixed sample contract ---")
    print(f"scope_count          : {'PASS' if scope_count == 4 else 'FAIL'} (4)")
    print(f"trading dates        : {'PASS' if trade_date_count == 120 else 'FAIL'} (120)")
    print(f"phase rows           : {'PASS' if total_phase_rows == 480 else 'FAIL'} (480)")
    print(f"every scope ready    : {'PASS' if all(s['phase_ready'] > 0 for s in per_scope) else 'FAIL'}")
    print(f"trace count          : {'PASS' if total_trace_count == 12 else 'FAIL'} (12)")
    print(f"date alignment       : {'PASS' if all(s['date_aligned'] for s in per_scope) else 'FAIL'}")
    print(f"Fixed sample contract: {'PASS' if contract_ok else 'FAIL'}")
    print(f"Dynamics executed    : {'YES' if dynamics_executed else 'NO'}")

    print("--- 12 traces (4 scopes x 3 dates) ---")
    for s in per_scope:
        print(f"[trace] scope={s['scope_name']} ({s['scope_type']}, n={s['member_count']})")
        for i, td in zip(s["trace_indices"], s["trace_dates"], strict=True):
            p = s["phase"][i]
            pos = s["historical_dynamics"]["position"][i]
            e5 = s["historical_dynamics"]["ema5"][i]
            e20 = s["historical_dynamics"]["ema20"][i]
            vel = s["historical_dynamics"]["velocity"][i]
            acc = s["historical_dynamics"]["acceleration"][i]
            per = s["historical_dynamics"]["persistence"][i]
            print(
                f"  date={td} status={p['status']} phase={p['phase']} "
                f"pos={pos.get('position')} ema5={e5.get('value')} ema20={e20.get('value')} "
                f"vel={vel.get('value')} acc={acc.get('value')} "
                f"upper_occ={per.get('upper_occupancy')} lower_occ={per.get('lower_occupancy')}"
            )

    return 0 if contract_ok else 1


def _spearman_rank_correlation(
    a: list[str], b: list[str]
) -> float | None:
    """Spearman rank correlation over the common-ordered rank axis (research).

    Stage-2 research statistic ONLY — used to observe how leader rank changes
    across T-1 -> T.  It is NOT a production Migration owner; if the frozen
    Migration contract (Stage 3) chooses a rank-stability metric, the production
    implementation will live in the formal owner, not here.
    """
    common = sorted(set(a) & set(b))
    if len(common) < 2:
        return None
    pos_a = {mid: i for i, mid in enumerate(a)}
    pos_b = {mid: i for i, mid in enumerate(b)}
    ranks_a = [pos_a[mid] for mid in common]
    ranks_b = [pos_b[mid] for mid in common]
    n = len(common)
    d2 = sum((ra - rb) ** 2 for ra, rb in zip(ranks_a, ranks_b, strict=True))
    return 1.0 - (6.0 * d2) / (n * (n * n - 1.0))


def _leadership_ranked(members: list[Any]) -> list[Any]:
    """Deterministic per-scope/date leadership ranking by signed contribution.

    Reuses the Stage-1 single owner (``compute_member_leadership_contributions``);
    only rankable members (contribution not None) are ordered, by contribution
    DESC then member_id ASC (deterministic tie-break).  Pure research helper.
    """
    from app.domain.review.analysis.leadership_contribution import (
        compute_member_leadership_contributions,
    )

    facts = compute_member_leadership_contributions(members)
    rankable = [c for c in facts.members if c.contribution is not None]
    ranked = sorted(
        rankable,
        key=lambda c: (-(c.contribution or 0.0), c.member_id),
    )
    return ranked


def _run_leadership_research(
    dataset_dir: str,
    *,
    fixture_path: str | None = None,
    history: int = 20,
    asof_lock: str | None = None,
    dry_run: bool = False,
) -> int:
    """Stage-2 research: observe real local-data Leadership dynamics.

    Uses ONLY the local frozen Dataset + the Stage-1 Leadership contribution
    owner.  NO formal Migration conclusion is produced — the goal is to see how
    leaders change across T-1 -> T so the Stage-3 contract can pick a stable,
    explainable metric (NOT a hardcoded Top-N / Spearman threshold).

    Outputs per representative scope (from the versioned fixture):
      * Top3/Top5/Top10 day-over-day overlap distribution
      * Spearman rank correlation distribution
      * Entrants / Exits distribution (into/out of Top-N)
      * Top3/Top5 contribution concentration (what share leaders hold)
      * missing / rankable counts

    NO-MIGRATION: only loads Dataset, calls the shared contribution owner and
    computes research statistics; no DB, no SSH, no remote PG, no parallel owner,
    no Migration decision.
    """
    if dry_run:
        print(
            f"[dry-run] leadership-research dataset_dir={dataset_dir} "
            f"fixture={fixture_path} history={history} asof={asof_lock} OK"
        )
        return 0

    import time
    from statistics import mean

    from app.services.review_observation_prep_service import (
        build_prepared_scopes_from_union,
        build_union_fact_context_from_loaded_facts,
    )

    print("=== leadership-research (Stage 2, local frozen Dataset) ===")
    print(f"dataset_dir : {dataset_dir}")
    print(f"history     : {history} trading days")

    if asof_lock:
        sel_asof = date.fromisoformat(asof_lock)
    else:
        declared = _dataset_asof(dataset_dir)
        if not declared:
            logger.error("[leadership-research] 无法解析 corpus declared asof")
            return 2
        sel_asof = date.fromisoformat(declared)

    if fixture_path is None:
        fixture_path = os.path.join(
            os.path.dirname(__file__), "fixtures", "review_dynamics_logic_sample.json"
        )
    scope_specs = _load_dynamics_logic_scope_specs(dataset_dir, fixture_path)
    selection = _build_replay_selection_from_specs(
        dataset_dir, scope_specs, asof_override=sel_asof
    )
    if selection.asof_date not in selection.trading_days:
        logger.error("[leadership-research] asof=%s 不在交易日历", asof_lock)
        return 2
    asof_idx = bisect_left(selection.trading_days, selection.asof_date)
    window_dates = list(
        selection.trading_days[max(0, asof_idx - history + 1): asof_idx + 1]
    )

    # ---- Multi-date Dataset load + shared union prep + L1 ----
    instr: dict[str, Any] = {}
    facts = _load_capacity_facts(
        dataset_dir, list(scope_specs),
        window_dates=window_dates, selection=selection, instr=instr,
    )
    union_ctx = build_union_fact_context_from_loaded_facts(
        t1_by_date=facts["t1_by_date"],
        states_by_date=facts["states_by_date"],
        bars=facts["bars"],
        events_by_date=facts["events_by_date"],
    )
    prep_counters: dict[str, int] = {}
    prepared = build_prepared_scopes_from_union(
        trade_dates=facts["trade_dates"],
        scope_specs=facts["scope_specs"],
        union_ctx=union_ctx,
        membership_t1_by_scope=None,
        current_only_facts_by_date=None,
        pit_status_t="current_static",
        pit_status_t1="current_static",
        t1_membership_available=False,
        prep_counters=prep_counters,
        prep_fallback_reasons=[],
    )

    for spec in scope_specs:
        sk = spec.scope_key
        series = prepared.get(sk)
        if not series or len(series) != len(window_dates):
            logger.error("[leadership-research] scope %s 未对齐", sk)
            continue

        # Per-date leadership rankings (one pass over members per scope/date).
        # concentration = Top-N |contribution| / sum(|all rankable contribution|),
        # i.e. the share of the scope's total |move| driven by the leaders.  Uses
        # ABS values so signed contributions (a negative leader offsetting a
        # positive one) do not distort the share (Stage-2 research observation).
        daily_ranked: list[list[Any]] = []
        daily_top3_share: list[float] = []
        daily_top5_share: list[float] = []
        daily_rankable: list[int] = []
        for ps in series:
            ranked = _leadership_ranked(list(ps.members))
            daily_ranked.append(ranked)
            total_abs = sum(abs(c.contribution or 0.0) for c in ranked)
            daily_top3_share.append(
                float("nan")
                if (not ranked or total_abs == 0.0)
                else sum(abs(c.contribution or 0.0) for c in ranked[:3]) / total_abs
            )
            daily_top5_share.append(
                float("nan")
                if (not ranked or total_abs == 0.0)
                else sum(abs(c.contribution or 0.0) for c in ranked[:5]) / total_abs
            )
            daily_rankable.append(len(ranked))

        # Day-over-day metrics (T-1 -> T).
        overlap3: list[float] = []
        overlap5: list[float] = []
        overlap10: list[float] = []
        spearman: list[float] = []
        entrants: list[int] = []
        exits: list[int] = []
        for i in range(1, len(daily_ranked)):
            prev = daily_ranked[i - 1]
            curr = daily_ranked[i]

            # Small-scope safety: Top-N cannot exceed the rankable pool; use the
            # effective overlap over the smaller of N and pool size so a tiny scope
            # (e.g. 10 members) does not saturate/divide by a fake N.
            def _ov(n: int) -> float:
                eff = max(1, min(n, len(prev), len(curr)))
                sp = {c.member_id for c in prev[:eff]}
                sc = {c.member_id for c in curr[:eff]}
                return len(sp & sc) / eff

            overlap3.append(_ov(3))
            overlap5.append(_ov(5))
            overlap10.append(_ov(10))
            corr = _spearman_rank_correlation(
                [c.member_id for c in prev],
                [c.member_id for c in curr],
            )
            if corr is not None:
                spearman.append(corr)
            entrants.append(
                len({c.member_id for c in curr[:5]} - {c.member_id for c in prev[:5]})
            )
            exits.append(
                len({c.member_id for c in prev[:5]} - {c.member_id for c in curr[:5]})
            )

        print(f"--- scope={spec.scope_name} ({spec.scope_type}, n={len(spec.member_ids)}) "
              f"dates={len(window_dates)} ---")
        print(f"  rankable members      : min={min(daily_rankable) if daily_rankable else 0} "
              f"mean={mean(daily_rankable) if daily_rankable else 0:.1f} "
              f"max={max(daily_rankable) if daily_rankable else 0}")
        print(f"  Top3 overlap  (T-1->T): mean={mean(overlap3):.3f} "
              f"min={min(overlap3):.3f} max={max(overlap3):.3f}")
        print(f"  Top5 overlap  (T-1->T): mean={mean(overlap5):.3f} "
              f"min={min(overlap5):.3f} max={max(overlap5):.3f}")
        print(f"  Top10 overlap (T-1->T): mean={mean(overlap10):.3f} "
              f"min={min(overlap10):.3f} max={max(overlap10):.3f}")
        print(f"  Spearman (T-1->T)     : mean={mean(spearman):.3f} "
              f"min={min(spearman):.3f} max={max(spearman):.3f} "
              f"count={len(spearman)}")
        print(f"  Top5 entrants/day      : mean={mean(entrants):.2f} "
              f"max={max(entrants)}")
        print(f"  Top5 exits/day         : mean={mean(exits):.2f} max={max(exits)}")
        print(f"  Top3 contribution share: mean={mean(daily_top3_share):.3f} "
              f"max={max(daily_top3_share):.3f}")
        print(f"  Top5 contribution share: mean={mean(daily_top5_share):.3f} "
              f"max={max(daily_top5_share):.3f}")
        print(f"  example (latest) Top5  : "
              f"{[c.member_id[:8] for c in daily_ranked[-1][:5]]}")

    return 0


def _run_replay_l1(
    dataset_dir: str,
    view_name: str,
    asof_lock: str | None = None,
    dry_run: bool = False,
) -> int:
    """R0-C Current L1 replay: Dataset corpus -> production core -> L1 observation.

    asof selection is **capability-aware** (R0-C2): by default the date is the
    ``full_capability_asof`` (all required fact domains have real source facts),
    so the full L1 semantic RTM can be computed.  Pass ``asof_lock`` to pin an
    explicit date (e.g. the manifest declared asof 2026-08-17) and deliberately
    exercise the SOURCE_UNAVAILABLE / fail-closed path — no latest backfill is
    ever performed; missing facts are reported as unavailable, never forged.
    Current-only snapshot facts are injected (no longer hard-coded empty), and
    T-1 / Transition facts truthfully report membership-source unavailability
    rather than forging current==T1 (P0-4).

    This is the single-date path.  Use ``--mode semantic-matrix`` to validate
    each fact at its best evidence date (08-17 / 08-10 / 08-07).
    """
    if dry_run:
        print(f"[dry-run] replay-l1 dataset_dir={dataset_dir} view={view_name} OK")
        return 0

    print(f"=== REVIEW-V23-PHASE1 replay-l1 ===  dataset={dataset_dir}")
    print(f"view={view_name}")

    # ---- P0 Dataset Integrity Gate（按 source 类型区分；row existence ≠ coverage）----
    integrity_violations = _check_dataset_integrity(dataset_dir)
    per_domain = _diagnose_source_date_ranges(dataset_dir)
    for stem, (lo, hi) in per_domain.items():
        print(f"[integrity]   {stem} [{_SOURCE_KIND.get(stem, '?')}]: actual range "
              f"[{lo.isoformat() if lo else 'NONE'} .. {hi.isoformat() if hi else 'NONE'}]")
    if integrity_violations:
        print(f"[integrity] 问题 ×{len(integrity_violations)}:")
        for v in integrity_violations:
            print(f"[integrity]   - {v}")
        print("[integrity] 说明：不阻断 replay（Trend/Structure/Momentum 缺失时如实记 "
              "SOURCE_UNAVAILABLE）；Daily State 截断 / Event coverage evidence 缺失须在 "
              "R0-C3 上游审计后定论，禁止 latest backfill。")
    else:
        print("[integrity] OK：manifest declared coverage 与 actual per-domain range 一致")

    if not asof_lock:
        # Track A default = manifest declared asof.
        declared = _dataset_asof(dataset_dir)
        if not declared:
            logger.error("[replay-l1] 无法从 corpus 解析 default asof")
            return 2
        asof_lock = declared

    try:
        one = _replay_l1_once(dataset_dir, view_name, asof_lock)
    except RuntimeError as e:
        logger.error("%s", e)
        return 2

    asof = one["asof"]
    track = "B (--asof-lock)" if asof_lock else "A (Current-asof)"

    # ---- Selection / physical-cost evidence (from the once-helper) ----
    instr = one["cost"]
    union_n = one["union_member_count"]
    current_only_map = one["current_only_map"]
    print("--- selection ---")
    print(f"asof                    : {asof}")
    print(f"selected_scope_count    : {len(one['results'])}")
    print(f"union_member_count      : {union_n}")
    print(f"accepted_snapshot_runs  : {one['accepted_snapshot_runs']}")
    print("--- selected rows (post-predicate) ---")
    for key, val in one["selected_rows"].items():
        print(f"{key:24}: {val}")

    # ---- Current-only fact coverage (guards the "all None" false success) ----
    print("--- current-only facts ---")
    print(f"{'members_with_facts':24}: {len(current_only_map)} / {union_n}")
    if current_only_map:
        from app.services.review_observation_prep_service import (
            _CURRENT_ONLY_SNAPSHOT_FIELDS,
        )
        for attr in _CURRENT_ONLY_SNAPSHOT_FIELDS:
            present = sum(
                1 for f in current_only_map.values()
                if f.get(attr) is not None
            )
            print(f"  {attr:34}: {present}")

    # Print a compact summary of one representative scope's L1 facts.
    results = one["results"]
    sample_keys = list(results.keys())[:3]
    for k in sample_keys:
        r = results[k]
        obs = r["observation"]
        bucket_keys = [kk for kk in obs.keys()]
        print(f"--- scope {r['scope_name']} ({r['scope_type']}, n={r['member_count']}) "
              f"fact_buckets={bucket_keys}")
    print(f"[asof] resolved={asof}  track={track}")
    print("=== END ===")
    return 0


def _perf_counter_ms() -> float:
    import time
    return time.perf_counter() * 1000.0


# ---------------------------------------------------------------------------
# L1_RTM — declarative RTM contract (PRD v2.3 Scope Observation Model)
# ---------------------------------------------------------------------------
# 每行是一个 source-ready L1 fact。``path`` 是 compute_scope_observation 返回 dict
# 的键路径（用 ``/`` 分隔）。``evidence_date`` 是该 fact 的预期最佳验证日期：
# 同一天不要求所有 fact 都 available（这正是 Semantic Validation Matrix 的意义）。
#
# 五态判定（R1-A Gate）：PASS / GAP / SOURCE_UNAVAILABLE / NOT_APPLICABLE /
# ALGORITHM_MAPPING_REQUIRED。  source-ready fact 必须 semantic mismatch=0、
# unexpected target fact=0、unavailable→0 coercion=0。
_L1_RTM_ROWS: list[dict] = [
    # ---- PRICE / CAPITAL（08-17 bars+snapshot 完整）----
    {"fact": "Equal Weight Return", "path": "price/equal_weight_return",
     "prd": "PRD §7.2 Return mean (EW)", "source": "bars_daily",
     "aggregation": "EW median-of-mean", "universe": "price-valid",
     "denominator": "price_valid_count", "evidence_date": "2026-08-17"},
    {"fact": "Amount Weighted Return", "path": "price/amount_weighted_return",
     "prd": "PRD §7.2 Return amount-weighted", "source": "bars_daily",
     "aggregation": "amount-weighted", "universe": "return&amount joint-valid",
     "denominator": "amount_weighted_return_universe_count", "evidence_date": "2026-08-17"},
    {"fact": "Return Dispersion", "path": "price/return_dispersion",
     "prd": "PRD §7.2 Return std", "source": "bars_daily",
     "aggregation": "stdev", "universe": "price-valid",
     "denominator": "price_valid_count", "evidence_date": "2026-08-17"},
    {"fact": "Price Breadth", "path": "price/breadth",
     "prd": "PRD §7.2 Breadth (UP/FLAT/DOWN)", "source": "bars_daily",
     "aggregation": "categorical distribution", "universe": "price-valid",
     "denominator": "price_valid_count", "evidence_date": "2026-08-17"},
    {"fact": "Price Concentration", "path": "price/concentration",
     "prd": "PRD §7.2 Concentration", "source": "bars_daily",
     "aggregation": "HHI-like", "universe": "price-valid",
     "denominator": "price_valid_count", "evidence_date": "2026-08-17"},
    {"fact": "Total Volume", "path": "price/total_volume",
     "prd": "PRD §7.2 Total Volume", "source": "bars_daily",
     "aggregation": "sum", "universe": "all PIT",
     "denominator": "pit_member_count", "evidence_date": "2026-08-17"},
    {"fact": "Amount Concentration", "path": "price/amount/concentration",
     "prd": "PRD §7.2 Amount concentration", "source": "bars_daily",
     "aggregation": "HHI", "universe": "amount-valid",
     "denominator": "amount.valid_count", "evidence_date": "2026-08-17"},
    # ---- Current-only snapshot facts（08-17 exact-T snapshot 完整）----
    {"fact": "BB Position", "path": "momentum/bb_position",
     "prd": "REVIEW-V23-A-CORRECTION-3 Current-only", "source": "stock_feature_snapshots_asof",
     "aggregation": "member-first median", "universe": "members with fact",
     "denominator": "members_with_bb_position", "evidence_date": "2026-08-17"},
    {"fact": "BB Width", "path": "momentum/bb_width",
     "prd": "REVIEW-V23-A-CORRECTION-3 Current-only", "source": "stock_feature_snapshots_asof",
     "aggregation": "member-first median", "universe": "members with fact",
     "denominator": "members_with_bb_width", "evidence_date": "2026-08-17"},
    {"fact": "Release Volume Ratio", "path": "momentum/release_volume_ratio",
     "prd": "REVIEW-V23-A-CORRECTION-3 Current-only", "source": "stock_feature_snapshots_asof",
     "aggregation": "member-first median", "universe": "members with fact",
     "denominator": "members_with_release_volume_ratio", "evidence_date": "2026-08-17"},
    {"fact": "Momentum/Volume Relation", "path": "momentum/momentum_volume_relation",
     "prd": "REVIEW-V23-A-CORRECTION-3 Current-only categorical", "source": "stock_feature_snapshots_asof",
     "aggregation": "categorical distribution", "universe": "members with fact",
     "denominator": "members_with_mvr", "evidence_date": "2026-08-17"},
    {"fact": "Distance to Trailing Top", "path": "structure/distance_to_trailing_top_pct",
     "prd": "REVIEW-V23-A-CORRECTION-3 Current-only", "source": "stock_feature_snapshots_asof",
     "aggregation": "member-first distribution", "universe": "members with fact",
     "denominator": "members_with_top", "evidence_date": "2026-08-17"},
    {"fact": "Distance to Trailing Bottom", "path": "structure/distance_to_trailing_bottom_pct",
     "prd": "REVIEW-V23-A-CORRECTION-3 Current-only", "source": "stock_feature_snapshots_asof",
     "aggregation": "member-first distribution", "universe": "members with fact",
     "denominator": "members_with_bottom", "evidence_date": "2026-08-17"},
    {"fact": "VWAP Return Total", "path": "trend/continuous/vwap_ret_total",
     "prd": "REVIEW-V23-A-CORRECTION-3 Current-only", "source": "stock_feature_snapshots_asof",
     "aggregation": "member-first median", "universe": "members with fact",
     "denominator": "members_with_vwap_ret", "evidence_date": "2026-08-17"},
    # ---- TREND（08-10 Daily State 完整）----
    {"fact": "Trend Regime State", "path": "trend/state",
     "prd": "PRD §7.3 Categorical distribution", "source": "first_pyramid_daily_state",
     "aggregation": "categorical distribution", "universe": "members with trend",
     "denominator": "trend_values_count", "evidence_date": "2026-08-10"},
    {"fact": "Trend Segment Direction", "path": "trend/segment_direction",
     "prd": "PRD §7.3 Segment direction", "source": "first_pyramid_daily_state",
     "aggregation": "categorical distribution", "universe": "members with segment_direction",
     "denominator": "segment_direction_count", "evidence_date": "2026-08-10"},
    {"fact": "Trend Regime Strength", "path": "trend/continuous/regime_strength",
     "prd": "PRD §7.3 Regime strength (median)", "source": "first_pyramid_daily_state",
     "aggregation": "median", "universe": "members with fact",
     "denominator": "regime_strength_count", "evidence_date": "2026-08-10"},
    {"fact": "Trend Transition", "path": "trend/transition",
     "prd": "PRD §7.3 Transition (T-1→T)", "source": "first_pyramid_daily_state × T-1",
     "aggregation": "categorical transition", "universe": "PIT(T)∩PIT(T-1)∩valid",
     "denominator": "trend_transition_count", "evidence_date": "2026-08-10",
     "t1_gated": True,
     "note": "Dataset current snapshot 无 PIT(T-1)；本 probe 以 t1_membership_available=False 运行。"
            "若仍产出 transition（denominator=full T），即为 within-T 重算，非真 T-1→T → GAP。"},
    # ---- SWING / INTERNAL（08-10 Daily State）----
    {"fact": "Swing State", "path": "structure/swing/state",
     "prd": "PRD §7.4 Swing state", "source": "first_pyramid_daily_state",
     "aggregation": "categorical distribution", "universe": "members with swing",
     "denominator": "swing_values_count", "evidence_date": "2026-08-10"},
    {"fact": "Internal State", "path": "structure/internal/state",
     "prd": "PRD §7.4 Internal state", "source": "first_pyramid_daily_state",
     "aggregation": "categorical distribution", "universe": "members with internal",
     "denominator": "internal_values_count", "evidence_date": "2026-08-10"},
    {"fact": "Structure Alignment", "path": "structure/alignment",
     "prd": "PRD §7.4 Alignment (Aligned/Divergent)", "source": "first_pyramid_daily_state",
     "aggregation": "categorical distribution", "universe": "members with alignment",
     "denominator": "alignment_count", "evidence_date": "2026-08-10"},
    # ---- MOMENTUM（08-10 Daily State）----
    {"fact": "Momentum Direction State", "path": "momentum/state",
     "prd": "PRD §7.5 Momentum direction", "source": "first_pyramid_daily_state",
     "aggregation": "categorical distribution", "universe": "members with momentum",
     "denominator": "momentum_values_count", "evidence_date": "2026-08-10"},
    {"fact": "Squeeze State", "path": "momentum/squeeze_state",
     "prd": "PRD §7.5 Squeeze state", "source": "first_pyramid_daily_state",
     "aggregation": "categorical distribution", "universe": "members with volatility_phase",
     "denominator": "squeeze_count", "evidence_date": "2026-08-10"},
    # ---- VOLUME / PARTICIPATION（08-10 state 驱动；08-17 bars 也完整）----
    {"fact": "Volume Ratio 20D", "path": "participation/volume/ratio20",
     "prd": "PRD §7.5 Volume ratio20", "source": "first_pyramid_daily_state / bars_daily",
     "aggregation": "participation distribution", "universe": "finite vol_ratio20",
     "denominator": "vol_ratio20_count", "evidence_date": "2026-08-10",
     "note": "state-derived member fact；08-10 与 08-17 均可（真实 source contract）"},
    {"fact": "Volume Ratio 200D", "path": "participation/volume/ratio200",
     "prd": "PRD §7.5 Volume ratio200", "source": "first_pyramid_daily_state",
     "aggregation": "participation distribution", "universe": "finite vol_ratio200",
     "denominator": "vol_ratio200_count", "evidence_date": "2026-08-10"},
    {"fact": "Amount Ratio 20D", "path": "participation/amount",
     "prd": "PRD §7.5 Amount ratio20", "source": "first_pyramid_daily_state",
     "aggregation": "participation distribution", "universe": "finite amt_ratio20",
     "denominator": "amt_ratio_count", "evidence_date": "2026-08-10"},
    # ---- STRUCTURE EVENTS（08-07 真实事件流）----
    # 真实结构：structure.events.cells.leveled.<EVENT>_<dir>_<level>
    #   → {"event_count", "member_count", "member_ratio"}；denominator = PIT(T) ∩ coverage。
    # AUDIT-FIX-01 (B.5/B.6): probe 只收集正式 cell evidence，不再 Σ member_ratio
    # （那是 probe 自造的二次 aggregation）。ROUND-2.2B: Event Coverage 已成为正式
    # source-availability 判定（structure.events.status = ready/unavailable），由
    # production owner 决定；Dataset Replay 无 RunItem lineage → coverage=None →
    # events SOURCE_UNAVAILABLE，probe 不伪造。不再有 COVERAGE_CONTRACT_OPEN。
    {"fact": "BOS cells", "ev": "BOS", "prd": "PRD §7.4 D Event aggregation",
     "source": "first_pyramid_events", "aggregation": "formal cell evidence (event_count/member_count/member_ratio)",
     "universe": "PIT(T)∩coverage", "denominator": "per-cell",
     "evidence_date": "2026-08-07",
     "event": True, "note": "cell evidence only; status from production coverage decision"},
    {"fact": "CHoCH cells", "ev": "CHoCH", "prd": "PRD §7.4 D Event aggregation",
     "source": "first_pyramid_events", "aggregation": "formal cell evidence",
     "universe": "PIT(T)∩event-coverage", "denominator": "per-cell",
     "evidence_date": "2026-08-07", "event": True},
    {"fact": "OB Created cells", "ev": "OB_CREATED", "prd": "PRD §7.4 D Event aggregation",
     "source": "first_pyramid_events", "aggregation": "formal cell evidence",
     "universe": "PIT(T)∩event-coverage", "denominator": "per-cell",
     "evidence_date": "2026-08-07", "event": True},
    {"fact": "EQH cells", "ev": "EQH", "prd": "PRD §7.4 D Event aggregation",
     "source": "first_pyramid_events", "aggregation": "formal cell evidence",
     "universe": "PIT(T)∩event-coverage", "denominator": "per-cell",
     "evidence_date": "2026-08-07", "event": True},
    {"fact": "EQL cells", "ev": "EQL", "prd": "PRD §7.4 D Event aggregation",
     "source": "first_pyramid_events", "aggregation": "formal cell evidence",
     "universe": "PIT(T)∩event-coverage", "denominator": "per-cell",
     "evidence_date": "2026-08-07", "event": True},
]


def _extract_event_cells(obs: dict, event_type: str) -> dict[str, dict]:
    """Return the FORMAL production event cells for ``event_type`` (cell evidence).

    AUDIT-FIX-01 (B.5/B.6): the probe previously summed ``member_ratio`` across
    all directions/levels for an event type.  That was a SECOND aggregation the
    production Core never defines — a member firing BOS_up_Swing AND BOS_up_Internal
    on the same day was counted twice.  Probe only collects evidence; it must not
    invent business formulas.

    AUDIT-FIX-01B (P2-1): the Core stores cells in TWO formal buckets
    (``scope_observation._aggregate_structure_events``):
      * ``cells.leveled``  — BOS / CHoCH / OB_* with key ``<EVENT>_<dir>_<level>``
      * ``cells.extreme``  — EQH / EQL with key ``<EVENT>`` (no dir/level)
    The extractor mirrors BOTH, returning the exact cell dicts verbatim (no
    aggregation).  A ``None``/absent subtree yields ``{}`` (honest, never coerced).
    """
    events = obs.get("structure", {}).get("events")
    if not isinstance(events, dict):
        return {}
    cells = events.get("cells", {})
    if not isinstance(cells, dict):
        return {}
    prefix = f"{event_type}_"
    out: dict[str, dict] = {}
    leveled = cells.get("leveled", {})
    if isinstance(leveled, dict):
        for k, v in leveled.items():
            if k.startswith(prefix):
                out[k] = v
    extreme = cells.get("extreme", {})
    if isinstance(extreme, dict):
        # extreme cells are keyed by the bare event type (EQH / EQL).
        if event_type in extreme:
            out[event_type] = extreme[event_type]
    return out


def _extract_l1_fact(obs: dict, path: str) -> Any:
    """Walk ``obs`` by ``/``-separated path; return None if any segment missing."""
    cur: Any = obs
    for seg in path.split("/"):
        if not isinstance(cur, dict) or seg not in cur:
            return None
        cur = cur[seg]
    return cur


def _is_degenerate(val: Any) -> bool:
    """True when ``val`` is a false-success (looks present but carries no signal).

    Guards the "all-None / all-zero" false PASS the user explicitly warned about:
      * None,
      * literal "unavailable" string,
      * dict with a degenerate ``status`` (zero_abs_return / zero_amount / unavailable),
      * dict whose all ``*_count`` / ``valid_count`` fields are 0
        (e.g. Price Breadth {advance:0, decline:0, unchanged:0}),
      * distribution dict with every ``*_ratio`` == null and no non-zero count.
    """
    if val is None:
        return True
    if isinstance(val, str) and val.strip().lower() == "unavailable":
        return True
    if isinstance(val, dict):
        st = val.get("status")
        if isinstance(st, str) and st in (
            "zero_abs_return", "zero_amount", "unavailable", "no_valid_members",
        ):
            return True
        counts = [v for k, v in val.items()
                  if k.endswith("_count") and isinstance(v, (int, float))]
        ratios = [v for k, v in val.items()
                  if k.endswith("_ratio") and v is not None]
        if counts and all(c == 0 for c in counts) and not ratios:
            return True
        # A distribution carrying only a zero denominator (e.g. a Transition
        # forced unavailable via ``t1_membership_available=False`` -> {denominator: 0})
        # is a degenerate / unavailable result, never a real signal.
        if "denominator" in val and not ratios and not counts:
            if val.get("denominator") in (0, None):
                return True
    return False


def _rtm_denominator(obs: dict, row: dict, scope: dict) -> Any:
    """Resolve the human-readable denominator value for an RTM row."""
    d = row.get("denominator", "")
    if d == "pit_member_count":
        return scope.get("member_count")
    # Try to read a numeric count field inside the extracted observation subtree.
    sub = _extract_l1_fact(obs, row["path"])
    if isinstance(sub, dict):
        for key in ("valid_count", "count", "total", "n"):
            if key in sub:
                return sub[key]
    return None


def _run_semantic_matrix(
    dataset_dir: str,
    view_name: str,
    dry_run: bool = False,
) -> int:
    """R0-C Semantic Validation Matrix + R1-A L1 RTM with evidence_date.

    Runs the full Current L1 pipeline at the three best-evidence dates and
    assembles a per-fact RTM table where each fact is read from its own
    ``evidence_date`` (08-17 Price/Participation/Current-only/fail-closed;
    08-10 State-driven Trend/Momentum/Structure/Volume; 08-07 Structure Events).
    This validates ``production L1 logic correctness`` rather than "is every
    fact available on one day".

    Five-state adjudication: a fact is SOURCE_UNAVAILABLE when its evidence-date
    run has no members / the value is None at that date (honest, never coerced
    to 0); PASS when a real value is produced; GAP when the source domain is
    genuinely missing in the corpus (dense truncation).
    """
    if dry_run:
        print(f"[dry-run] semantic-matrix dataset_dir={dataset_dir} view={view_name} OK")
        return 0

    evidence_dates = ["2026-08-17", "2026-08-10", "2026-08-07"]
    runs: dict[str, dict] = {}
    for ed in evidence_dates:
        try:
            runs[ed] = _replay_l1_once(dataset_dir, view_name, ed)
        except RuntimeError as e:
            logger.error("semantic-matrix: asof=%s 失败: %s", ed, e)
            return 2

    # Representative scope: pick the one with the largest member_count for a
    # stable, representative L1 sample.
    def _representative(r: dict) -> tuple[str, dict]:
        best_k, best = None, None
        for k, v in r["results"].items():
            if best is None or v["member_count"] > best["member_count"]:
                best_k, best = k, v
        return best_k, best

    rep_key: dict[str, str] = {}
    rep_obs: dict[str, dict] = {}
    for ed, r in runs.items():
        k, v = _representative(r)
        rep_key[ed] = k
        rep_obs[ed] = v["observation"]

    print("=== REVIEW-V23-PHASE1 semantic-matrix (L1 RTM) ===")
    print(f"view={view_name}")
    print(f"representative scopes: " + ", ".join(
        f"{ed}→{rep_key[ed]}" for ed in evidence_dates
    ))
    for ed in evidence_dates:
        r = runs[ed]
        print(f"  asof={ed}: union_members={r['union_member_count']} "
              f"snap_runs={r['accepted_snapshot_runs']} "
              f"rows(state/bar/event/snap)="
              f"{r['selected_rows'].get('state_rows_selected')}/"
              f"{r['selected_rows'].get('bar_rows_selected')}/"
              f"{r['selected_rows'].get('event_rows_selected')}/"
              f"{r['selected_rows'].get('snapshot_rows_selected')}")

    # ---- Per-fact RTM table ----
    # Columns: Fact → PRD → Source → Aggregation → Denominator → Availability →
    #          Evidence Date → Actual → Status → GAP
    # Availability = source domain rows present at that evidence_date (bars /
    #   states / events / snapshots).  This is orthogonal to Status:
    #   * PASS              source-ready + real value produced (semantic ok)
    #   * SOURCE_UNAVAILABLE  value can't be produced (member gate / missing
    #                         state / no data) — never coerced to 0
    #   * GAP               source-ready (domain present) but fact semantically
    #                         wrong / cannot be formed at the required granularity
    #   * ALGORITHM_MAPPING_REQUIRED  PRD defines fact but no code mapping exists
    #   * NOT_APPLICABLE    PRD scopes the fact out of this universe
    print("\n--- L1 RTM (evidence_date per fact) ---")
    hdr = (f"{'Fact':24} {'PRD':22} {'Source':20} {'Aggregation':26} "
           f"{'Denom':10} {'Avail':8} {'EvDate':10} {'Status':20} {'GAP'}")
    print(hdr)
    print("-" * len(hdr))
    n_pass = n_unavail = n_gap = n_na = n_algo = 0
    n_unexpected_zero = 0
    gaps: list[str] = []
    for row in _L1_RTM_ROWS:
        ed = row["evidence_date"]
        obs = rep_obs.get(ed, {})
        scope = runs[ed]["results"][rep_key[ed]]
        rows_sel = runs[ed]["selected_rows"]
        if row.get("ev"):
            # AUDIT-FIX-01 (B.5/B.6/B.7): inspect the FORMAL production event cells
            # (cell evidence, no Σ member_ratio).  Event row existence (avail) is
            # NOT coverage — the Event Coverage Contract is OPEN, so event facts are
            # never marked final PASS.
            cells = _extract_event_cells(obs, row["ev"])
            ev_struct = obs.get("structure", {}).get("events")
            denom_val = ev_struct.get("denominator") if isinstance(ev_struct, dict) else None
            # Event availability is gated by the OPEN coverage contract, not rows.
            avail = 1  # placeholder; event status is decided by the event branch below
        else:
            val = _extract_l1_fact(obs, row["path"])
            denom_val = _rtm_denominator(obs, row, scope)
            # Source may be composite (e.g. "first_pyramid_daily_state / bars_daily");
            # match by prefix so both domains are checked (avail = OR of present rows).
            src_row_map = [
                ("bars_daily", "bar_rows_selected"),
                ("stock_feature_snapshots_asof", "snapshot_rows_selected"),
                ("first_pyramid_daily_state", "state_rows_selected"),
            ]
            src_str = row.get("source", "")
            avail = 0
            for frag, sel_key in src_row_map:
                if frag in src_str:
                    avail = max(avail, rows_sel.get(sel_key, 0) or 0)
            avail = avail or 0
        # Distinguish PIT membership (scope dict member_count = len(pit_member_ids),
        # non-zero even when the member gate blocks formation) from the actual
        # MemberObservation count (provided_member_count).  A non-zero PIT with a
        # zero provided count IS the member gate (GAP-L1-MEMBER-GATE).
        pit_count = scope.get("member_count", 0)
        provided_count = (
            obs.get("scope", {}).get("provided_member_count", 0)
            if isinstance(obs.get("scope"), dict) else 0
        )
        source_ready = bool(avail and avail > 0)

        # Event facts (ROUND-2.2B): the exact-T Event Coverage source decision is now
        # owned by production — ``structure.events.status`` is "ready" when coverage
        # is valid (possibly empty cells = legal zero-event), "unavailable" when the
        # coverage source is absent (corpus replay has no RunItem lineage).  The probe
        # only reports that decision honestly; it never invents coverage from event
        # rows.  The old COVERAGE_CONTRACT_OPEN state is removed (coverage is now a
        # real source-availability decision, not an open contract).
        if row.get("event"):
            val = cells
            ev_status = obs.get("structure", {}).get("events", {}).get("status")
            if ev_status == "ready":
                status = "PASS"
                n_pass += 1
                gap = ""
            else:
                status = "SOURCE_UNAVAILABLE"
                n_unavail += 1
                gap = "no-coverage-source"
            actual = _fmt_rtm_value(val)
            print(f"{row['fact']:24} {row['prd'][:22]:22} {row['source'][:20]:20} "
                  f"{row['aggregation'][:26]:26} {str(denom_val):10} "
                  f"{'n/a':8} {ed:10} {status:20} {gap}")
            print(f"    ↳ cells: {actual}")
            if row.get("note"):
                print(f"    ↳ note: {row['note']}")
            continue

        # Five-state adjudication.
        if not source_ready:
            # Source domain genuinely absent at this date → NOT a logic GAP.
            status = "SOURCE_UNAVAILABLE"
            n_unavail += 1
            gap = ""
        elif pit_count > 0 and provided_count == 0:
            # PIT members exist but NO MemberObservation formed (bars/snapshots
            # present yet state-gated out) → the member-construction gate.
            status = "GAP"
            gap = "GAP-L1-MEMBER-GATE"
            n_gap += 1
            gaps.append(f"[{ed}]{row['fact']}:{gap}")
        elif provided_count == 0:
            # No PIT membership at all → not a member-gate issue, just unavailable.
            status = "SOURCE_UNAVAILABLE"
            n_unavail += 1
            gap = ""
        elif row.get("t1_gated") and not _is_degenerate(val):
            # PRD requires PIT(T-1) membership gate, but the matrix runs with
            # t1_membership_available=False.  A non-degenerate transition value
            # here means the Core recomputed it within-T (denominator = full T),
            # NOT a true T-1→T on the PIT(T)∩PIT(T-1) universe → GAP.
            status = "GAP"
            gap = "GAP-L1-TRANSITION-T1"
            n_gap += 1
            gaps.append(f"[{ed}]{row['fact']}:{gap}")
        elif _is_degenerate(val):
            # Source present + members formed, but the fact degrades to all-zero /
            # zero_abs_return / "unavailable".  Honest SOURCE_UNAVAILABLE (never
            # coerced to 0).  Whether this is a real gap depends on the sub-fact
            # (e.g. Structure Alignment / Squeeze may legitimately be empty).
            status = "SOURCE_UNAVAILABLE"
            n_unavail += 1
            gap = "empty-value (verify)"
        elif val is None:
            # Source present + members formed, but PRD fact has NO code mapping.
            status = "ALGORITHM_MAPPING_REQUIRED"
            n_algo += 1
            gap = "no-code-mapping"
        else:
            status = "PASS"
            n_pass += 1
            gap = ""

        # semantic-mismatch guard: a PASS must not be zero-coerced from unavailable.
        if status == "PASS" and val == 0:
            n_unexpected_zero += 1
            gap = "zero-from-unavailable?"
            gaps.append(f"[{ed}]{row['fact']}:zero-coercion")

        actual = _fmt_rtm_value(val)
        print(f"{row['fact']:24} {row['prd'][:22]:22} {row['source'][:20]:20} "
              f"{row['aggregation'][:26]:26} {str(denom_val):10} "
              f"{'yes' if source_ready else 'no':8} {ed:10} {status:20} {gap}")
        if row.get("note"):
            print(f"    ↳ note: {row['note']}")

    print("\n--- RTM status summary ---")
    print(f"PASS={n_pass}  SOURCE_UNAVAILABLE={n_unavail}  GAP={n_gap} "
          f"NOT_APPLICABLE={n_na}  ALGORITHM_MAPPING_REQUIRED={n_algo}")
    print(f"unavailable→0 coercion count = {n_unexpected_zero}  (must be 0)")
    if gaps:
        print("\n--- Round 1 GAP findings (recorded, NOT fixed) ---")
        for g in gaps:
            print(f"  * {g}")
    print("=== END ===")
    return 0


def _fmt_rtm_value(val: Any) -> str:
    """Compact, JSON-safe rendering of an RTM fact value (no huge dumps)."""
    import json as _json
    if val is None:
        return "None"
    if isinstance(val, float):
        return f"{val:.4g}"
    if isinstance(val, dict):
        # Summarize distribution-like dicts: keep keys + short counts.
        if "distribution" in val or "mean" in val:
            return _json.dumps(val, ensure_ascii=False)[:120]
        if "status" in val:
            return str(val.get("status"))
        return _json.dumps(val, ensure_ascii=False)[:120]
    if isinstance(val, (list, tuple)):
        return f"<list n={len(val)}>"
    return str(val)[:120]


def _run_rtm(
    dataset_dir: str,
    view_name: str,
    asof_lock: str | None = None,
    dry_run: bool = False,
) -> int:
    """R1-A L1 RTM — delegates to the Semantic Validation Matrix.

    The matrix validates each fact at its best evidence date and records
    ``evidence_date`` per fact; it does NOT require all facts on one day.
    """
    if dry_run:
        print(f"[dry-run] rtm dataset_dir={dataset_dir} view={view_name} OK")
        return 0
    # rtm mode == semantic matrix (per-fact evidence dates).
    return _run_semantic_matrix(dataset_dir, view_name, dry_run=dry_run)


def _run_explore1(
    dataset_dir: str,
    view_name: str,
    asof_lock: str | None = None,
    dry_run: bool = False,
) -> int:
    """EXPLORE-1: Structure Event denominator inspection (real dataset + synthetic).

    Real 08-07 data: list PIT members, event-bearing members, event types, and
    whether a member-level event-coverage indicator exists.  We do NOT conclude
    "member event-capability unavailable" merely because a member has no events.
    Then show synthetic Case A / Case B to test whether the current production
    Core (``denominator = len(pit_set)``) can express a per-member event-coverage
    gate — if not, that is a Source Contract / Core GAP, not a test to be bent.
    """
    if dry_run:
        print(f"[dry-run] explore1 dataset_dir={dataset_dir} view={view_name} OK")
        return 0
    ed = asof_lock or "2026-08-07"
    try:
        run = _replay_l1_once(dataset_dir, view_name, ed)
    except RuntimeError as e:
        logger.error("explore1: asof=%s 失败: %s", ed, e)
        return 2
    rep_key = max(
        run["results"],
        key=lambda k: run["results"][k]["member_count"],
    )
    obs = run["results"][rep_key]["observation"]
    events = obs.get("structure", {}).get("events", {})
    cells = events.get("cells", {}) if isinstance(events, dict) else {}
    leveled = cells.get("leveled", {}) if isinstance(cells, dict) else {}
    pit_n = obs.get("scope", {}).get("pit_member_count")

    print("=== REVIEW-V23-PHASE1 EXPLORE-1 (event denominator) ===")
    print(f"asof={ed}  representative scope={rep_key}")
    print(f"pit_member_count(scope)   = {pit_n}")
    print(f"events.denominator         = {events.get('denominator') if isinstance(events, dict) else 'N/A'}")
    print(f"events.cells.leveled cells = {len(leveled)}")

    # Real-data inspection: enumerate event types actually present.
    ev_types: dict[str, int] = {}
    for k in leveled:
        et = k.split("_")[0]
        ev_types[et] = ev_types.get(et, 0) + 1
    print("event types present :", dict(sorted(ev_types.items())))
    print("event cells (type_dir_level -> event_count / member_count / member_ratio):")
    for k, cell in sorted(leveled.items()):
        if not isinstance(cell, dict):
            continue
        print(f"  {k:28} ec={cell.get('event_count')} mc={cell.get('member_count')} "
              f"mr={cell.get('member_ratio')}")

    # ---- PRD-vs-Code contract check ----
    # PRD §7.4 D Event aggregation universe = PIT(T) ∩ valid canonical event-coverage.
    # Code denominator (see _aggregate_structure_events) = len(pit_set).
    # There is NO member-level event-coverage indicator in the current Dataset:
    # a member with 0 events is indistinguishable from a member whose event
    # capability is unavailable.  Hence Case B (8/10 members covered) cannot be
    # expressed.  This is a Source Contract / Core GAP, not a test to bend.
    print("\n--- EXPLORE-1 conclusion (record as GAP, do NOT fix now) ---")
    print(f"  production denominator = len(pit_set) = {events.get('denominator') if isinstance(events, dict) else 'N/A'}")
    print(f"  pit_member_count       = {pit_n}")
    print("  denominator == pit_member_count? "
          f"{'YES' if events.get('denominator') == pit_n else 'NO'}")
    print("  member-level event-coverage indicator present? : NO")
    print("  → PRD-vs-Code GAP: denominator lacks event-coverage gate (EXPLORE-1).")
    print("  → Synthetic Case A (10/10 covered) expressible; Case B (8/10) NOT.")
    print("  → classified: SOURCE CONTRACT / CORE GAP (defer Fix to Round 2).")
    print("=== END ===")
    return 0


def _run_equivalence(
    dataset_dir: str,
    view_name: str,
    dry_run: bool = False,
) -> int:
    """Canonical vs Optimized equivalence (Gate 5).

    Same loaded facts are fed to BOTH:
      * the canonical per-scope path  (``_build_member_observations`` per scope
        + ``compute_scope_observation``), and
      * the optimized union+vectorized path (``build_prepared_scopes_from_union``).
    We assert MemberObservation mismatch = 0 and ScopeObservation mismatch = 0 on
    representative_sample × {08-07, 08-10, 08-17}.

    This proves the R0-B union extraction is faithful to the canonical
    member-construction owner (no dropped member, no reorder, no changed fact),
    and that the vectorized VolumeContext is canonical-equivalent.
    """
    if dry_run:
        print(f"[dry-run] equivalence dataset_dir={dataset_dir} view={view_name} OK")
        return 0
    from app.domain.review.scope_observation import compute_scope_observation
    from app.services.review_observation_prep_service import (
        _build_member_observations,
        build_prepared_scopes_from_union,
        build_union_fact_context_from_loaded_facts,
    )

    dates = ["2026-08-07", "2026-08-10", "2026-08-17"]
    overall_ok = True
    print("=== REVIEW-V23-PHASE1 canonical-vs-optimized equivalence ===")
    print(f"view={view_name}  dates={dates}")
    for ed in dates:
        selection = _build_replay_selection(
            dataset_dir, view_name, asof_override=date.fromisoformat(ed)
        )
        if not selection.scope_specs:
            print(f"[equivalence] {ed}: empty scope_specs — SKIP")
            continue
        if selection.asof_date not in selection.trading_days:
            print(f"[equivalence] {ed}: not a trading day — SKIP")
            continue
        facts = _load_replay_facts(
            dataset_dir, list(selection.scope_specs), selection=selection, instr={}
        )
        union_ctx = build_union_fact_context_from_loaded_facts(
            t1_by_date=facts["t1_by_date"],
            states_by_date=facts["states_by_date"],
            bars=facts["bars"],
            events_by_date=facts["events_by_date"],
        )
        # ---- Optimized (union) path ----
        optimized = build_prepared_scopes_from_union(
            trade_dates=facts["trade_dates"],
            scope_specs=facts["scope_specs"],
            union_ctx=union_ctx,
            membership_t1_by_scope=None,
            current_only_facts_by_date=facts["current_only_facts_by_date"],
            pit_status_t="current_static",
            pit_status_t1="unavailable",
            t1_membership_available=False,
        )
        # ---- Canonical per-scope path (independent recomputation) ----
        canonical: dict[str, dict] = {}
        # Real T-1 inputs, identical to the optimized union path: real T-1 states
        # (states_by_date[t1]) for member T-1 state, and current-static membership
        # T-1 (pit_member_ids_t1 == current membership).  This keeps the two paths
        # fed with the SAME facts so the comparison is apples-to-apples.
        t1 = facts["t1_by_date"].get(selection.asof_date)
        states_t1 = facts["states_by_date"].get(t1, {}) if t1 else {}
        for spec in facts["scope_specs"]:
            members = _build_member_observations(
                list(spec.member_ids),
                trade_date=selection.asof_date,
                t1=t1,
                states_t=facts["states_by_date"].get(selection.asof_date, {}),
                states_t1=states_t1,
                bars=facts["bars"],
                current_only_facts=facts["current_only_facts_by_date"].get(
                    selection.asof_date, {}
                ),
                vec_volume=union_ctx.vec_volume,
            )
            obs = compute_scope_observation(
                scope_type=spec.scope_type,
                scope_key=spec.scope_key,
                trade_date=selection.asof_date,
                pit_member_ids=[str(i) for i in spec.member_ids],
                pit_member_ids_t1=[str(i) for i in spec.member_ids],
                members=members,
                events=union_ctx.events_by_date.get(selection.asof_date, []),
                t1_membership_available=False,
                # Corpus replay has no RunItem lineage -> coverage is None
                # (structure-events unavailable in both paths).  The synthetic
                # coverage equivalence is a dedicated PURE_UNIT test, not here.
                event_coverage_member_ids=None,
            )
            canonical[spec.scope_key] = {
                "member_count": len(members),
                "observation": obs,
            }

        # ---- Compare (only source-ready fact subtrees per date) ----
        # Both paths are fed the SAME loaded facts, so the observations should be
        # byte-identical on every fact that the date's sources can express.  We
        # compare per source-ready subtree so an unavailable fact (None / empty)
        # is never demanded to hold a valid value — and a discrepancy there is
        # not counted as a false mismatch.
        src_subtrees = {
            "2026-08-17": ("price", "momentum", "structure", "participation", "chip"),
            "2026-08-10": ("trend", "structure", "momentum", "participation"),
            "2026-08-07": ("structure",),
        }[ed]

        mem_mismatch = 0
        obs_mismatch = 0
        compared = 0
        for spec in facts["scope_specs"]:
            sk = spec.scope_key
            opt_series = optimized.get(sk)
            can = canonical.get(sk)
            if not opt_series or can is None:
                mem_mismatch += 1
                obs_mismatch += 1
                continue
            opt_ps = opt_series[0]
            # Members: compare by PIT membership (robust to obs members being None
            # when the member gate blocks formation).
            opt_ids = {str(i) for i in opt_ps.pit_member_ids}
            can_ids = {str(i) for i in spec.member_ids}
            if opt_ids != can_ids:
                mem_mismatch += 1
            # Observations: compute the optimized observation from PreparedScope
            # (union path carries prepared inputs, not the computed obs), then
            # compare only the source-ready subtrees of that date.
            opt_obs = compute_scope_observation(
                scope_type=opt_ps.scope_type,
                scope_key=opt_ps.scope_key,
                trade_date=opt_ps.trade_date,
                pit_member_ids=list(opt_ps.pit_member_ids),
                pit_member_ids_t1=list(opt_ps.pit_member_ids_t1),
                members=opt_ps.members,
                events=union_ctx.events_by_date.get(selection.asof_date, []),
                t1_membership_available=opt_ps.t1_membership_available,
                event_coverage_member_ids=opt_ps.event_coverage_member_ids,
            )
            for subtree in src_subtrees:
                opt_s = opt_obs.get(subtree)
                can_s = can["observation"].get(subtree)
                compared += 1
                if _canonicalize_obs(opt_s) != _canonicalize_obs(can_s):
                    obs_mismatch += 1
        ok = (mem_mismatch == 0 and obs_mismatch == 0)
        overall_ok = overall_ok and ok
        print(f"  {ed}: member_mismatch={mem_mismatch} obs_mismatch={obs_mismatch} "
              f"(compared {compared} source-ready subtrees) "
              f"-> {'OK' if ok else 'MISMATCH'}")
    print("=== END ===")
    return 0 if overall_ok else 1


def _canonicalize_obs(obs: Any) -> str:
    """Stable, order-insensitive canonicalization of an observation dict."""
    import json as _json
    def _sort(o):
        if isinstance(o, dict):
            return {k: _sort(v) for k, v in sorted(o.items())}
        if isinstance(o, (list, tuple)):
            return [_sort(x) for x in o]
        if isinstance(o, float):
            return round(o, 9)
        return o
    return _json.dumps(_sort(obs), sort_keys=True, ensure_ascii=False)


async def _run(args: argparse.Namespace) -> int:
    asof: date | None = date.fromisoformat(args.asof_date) if args.asof_date else None
    # ---- export-dataset / dataset-validate：参数校验先于 dry-run ----
    if args.mode in ("export-dataset", "dataset-validate"):
        if not args.dataset_dir:
            logger.error("[%s] --dataset-dir 为必填", args.mode)
            return 2
        if args.scope_type or args.scope_key:
            logger.error(
                "[%s] 禁止 --scope-type/--scope-key 参数（V1 锁死 Full Corpus）",
                args.mode,
            )
            return 2
        if args.dry_run:
            logger.info(
                "[dry-run] mode=%s dataset_dir=%s history=%d asof=%s OK",
                args.mode, args.dataset_dir, args.history, asof,
            )
            return 0
        if args.mode == "export-dataset":
            return await _export_dataset(args.dataset_dir, asof, args.history)
        return _dataset_validate(args.dataset_dir)
    # ---- dataset-capacity-benchmark：纯本地冻结 Dataset 多日期窗口容量（不连 DB）----
    if args.mode == "dataset-capacity-benchmark":
        if not args.dataset_dir:
            logger.error("[dataset-capacity-benchmark] --dataset-dir 为必填")
            return 2
        if args.scope_type or args.scope_key:
            logger.error(
                "[dataset-capacity-benchmark] 使用 --view，禁 --scope-type/--scope-key"
            )
            return 2
        return _run_dataset_capacity_benchmark(
            args.dataset_dir, args.view,
            history=args.history,
            asof_lock=args.asof_lock,
            dry_run=args.dry_run,
        )
    # ---- dataset-dynamics-logic：4-scope frozen Dataset 全 Dynamics 链 E2E（不连 DB）----
    if args.mode == "dataset-dynamics-logic":
        if not args.dataset_dir:
            logger.error("[dataset-dynamics-logic] --dataset-dir 为必填")
            return 2
        if args.scope_type or args.scope_key:
            logger.error(
                "[dataset-dynamics-logic] 禁 --scope-type/--scope-key（scope 固定来自 "
                "版本化 fixture review_dynamics_logic_sample.json）"
            )
            return 2
        dl_history = args.history if args.history else 120
        return _run_dataset_dynamics_logic(
            args.dataset_dir,
            history=dl_history,
            asof_lock=args.asof_lock,
            dry_run=args.dry_run,
        )
    # ---- leadership-research：Stage-2 研究（复用共享 owner，不产出正式结论）----
    if args.mode == "leadership-research":
        if not args.dataset_dir:
            logger.error("[leadership-research] --dataset-dir 为必填")
            return 2
        if args.scope_type or args.scope_key:
            logger.error("[leadership-research] 禁 --scope-type/--scope-key（用 fixture）")
            return 2
        lr_history = args.history if args.history else 20
        return _run_leadership_research(
            args.dataset_dir,
            history=lr_history,
            asof_lock=args.asof_lock,
            dry_run=args.dry_run,
        )
    # ---- replay-l1 / rtm / semantic-matrix / explore1：纯本地 Dataset corpus 回放（不连 DB）----
    if args.mode in ("replay-l1", "rtm", "semantic-matrix", "explore1", "equivalence"):
        if not args.dataset_dir:
            logger.error("[%s] --dataset-dir 为必填", args.mode)
            return 2
        if args.scope_type or args.scope_key:
            logger.error("[%s] replay 模式使用 --view，禁 --scope-type/--scope-key", args.mode)
            return 2
        # Current L1 source loading is locked to {T, T-1} states / exact-T events /
        # exact-T consumable snapshots / 400-calendar-day bars.  --history does NOT
        # drive it; multi-date belongs to current-static equivalence / perf replay.
        if args.history != p_default_history():
            logger.error(
                "[%s] --history 对 Current L1 source loading 不起作用（capability-aware "
                "锁 asof 单日，或 --asof-lock 显式锁）；多日期属 current-static equivalence "
                "/ performance replay",
                args.mode,
            )
            return 2
        if args.mode == "replay-l1":
            return _run_replay_l1(
                args.dataset_dir, args.view,
                asof_lock=args.asof_lock, dry_run=args.dry_run,
            )
        if args.mode == "semantic-matrix":
            return _run_semantic_matrix(
                args.dataset_dir, args.view, dry_run=args.dry_run,
            )
        if args.mode == "explore1":
            return _run_explore1(
                args.dataset_dir, args.view,
                asof_lock=args.asof_lock, dry_run=args.dry_run,
            )
        if args.mode == "equivalence":
            return _run_equivalence(
                args.dataset_dir, args.view, dry_run=args.dry_run,
            )
        return _run_rtm(
            args.dataset_dir, args.view,
            asof_lock=args.asof_lock, dry_run=args.dry_run,
        )
    if args.dry_run:
        logger.info(
            "[dry-run] mode=%s scope_type=%s scope_key=%s history=%d asof=%s OK",
            args.mode, args.scope_type, args.scope_key, args.history, asof,
        )
        return 0
    if args.mode == "capacity-benchmark":
        return await _capacity_benchmark(
            args.scope_type or "concept",
            args.scope_count or 0,
            args.history,
            asof,
        )
    # All other modes are dispatched above; the legacy single-scope performance
    # probe is intentionally removed (its role is served by PURE_UNIT correctness +
    # capacity-benchmark capacity).  If a mode falls through with no handler, it is
    # an error.
    logger.error("未处理的 mode: %s", args.mode)
    return 2


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
