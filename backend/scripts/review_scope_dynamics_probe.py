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
            str(b.get("external_code") or b.get("id")),
        ),
    )


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
) -> dict:
    return {
        "view_id": view_id,
        "selection_policy": policy,
        "selection_algorithm_version": VIEW_ALGORITHM_VERSION,
        "scope_keys": sorted(str(s) for s in scope_ids),
        "derived_instrument_ids": _union_for(scope_ids, per_board),
        "date_range": "manifest.date_ranges（analysis/warmup/states/bars 范围，见 manifest）",
        "membership_usage": dict(membership_usage),
    }


def build_views(boards: list[dict], memberships: list[dict]) -> dict[str, dict]:
    """生成 5 个 logical view（REVIEW-REPLAY-DATASET-V1 §7）。

    完全确定性：所有 metric 排序带稳定 tie-breaker（metric DESC → external_code ASC）。
    ``derived_instrument_ids == union(memberships[scope_keys])`` 由 _mk_view 保证。
    """
    active_boards = [b for b in boards if b.get("is_active", True)]
    per_board, member_board_count = _overlap_rank(boards, memberships)
    concept = [b for b in active_boards if b.get("type") == "concept"]
    industry = [b for b in active_boards if b.get("type") == "industry"]
    usage = {"current": "available", "historical": "not_available"}

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
        key=lambda b: (-len(per_board.get(str(b.get("id")), [])), str(b.get("external_code") or b.get("id"))),
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
            str(b.get("external_code") or b.get("id")),
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
        ),
        "capacity_4096": _mk_view(
            "capacity_4096",
            "本地人为排序的容量样本（overlap DESC 贪心，union ≤ 4096）；"
            "不叫 perf_4096 / production chunk",
            cap_ids,
            per_board,
            usage,
        ),
        "all_concepts": _mk_view(
            "all_concepts",
            "全部 concept scope（current snapshot only）",
            [str(b.get("id")) for b in concept],
            per_board,
            usage,
        ),
        "all_industries": _mk_view(
            "all_industries",
            "全部 industry_l1/l2/l3 scope（current snapshot only）",
            [str(b.get("id")) for b in industry],
            per_board,
            usage,
        ),
        "representative_sample": _mk_view(
            "representative_sample",
            "代表性技术样本（member_count 最大 / overlap 最高 / 接近 median，"
            "均带 tie-breaker × 5 dates，日期取自 manifest.date_ranges）",
            rep_ids,
            per_board,
            usage,
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
) -> dict:
    """把 raw/*.jsonl.gz 流式转成 parquet/*.parquet，返回 {rows, path}。"""
    raw_path = os.path.join(raw_dir, f"{file_stem}.jsonl.gz")
    out_path = os.path.join(parquet_dir, f"{file_stem}.parquet")
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise RuntimeError(
            "optional replay dependency missing (pip install -r requirements-replay.txt)"
        ) from e
    dec_cols = _DECIMAL_COLUMNS.get(domain, {})
    os.makedirs(parquet_dir, exist_ok=True)
    writer: Any = None
    count = 0
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
        names = list(schema.names)
        arrays = []
        for name in names:
            v = row.get(name)
            ftype = schema.field(name).type
            if pa.types.is_decimal(ftype):
                arrays.append(pa.array([Decimal(v) if v is not None else None], type=ftype))
            elif pa.types.is_string(ftype):
                arrays.append(
                    pa.array(
                        [
                            json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                            if isinstance(v, (dict, list))
                            else v
                        ],
                        type=ftype,
                    )
                )
            else:
                arrays.append(pa.array([v], type=ftype))
        writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
        count += 1
    if writer is not None:
        writer.close()
    return {"rows": count, "path": out_path}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scope Dynamics 只读基线测量 probe（R1）",
    )
    p.add_argument(
        "--scope-type", required=False, default=None,
        choices=sorted(_KNOWN_SCOPE_TYPES),
        help="scope 类型（industry_l1/l2/l3/concept/...），measure-all-scopes 模式可省略",
    )
    p.add_argument(
        "--scope-key", required=False, default=None, type=str,
        help="scope 标识（如 银行 / 人工智能），measure-all-scopes 模式可省略",
    )
    p.add_argument(
        "--history", type=int, default=120,
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
            "single",
            "measure-all-scopes",
            "vec1-benchmark",
            "export-dataset",
            "dataset-validate",
        ],
        default="single",
        help=(
            "single: 单 scope 基线测量（默认）；"
            "measure-all-scopes: 枚举全量 scope 测物理数据量，定 batch boundary；"
            "vec1-benchmark: 对重叠度最高的 concept 子集执行 batch dynamics，"
            "报告 VEC-1 duplication_factor 等物理成本指标；"
            "export-dataset: 服务器一次性只读导出 Review Source Dataset（Full Corpus，"
            "禁 scope-type/scope-key 参数）；"
            "dataset-validate: 本地校验 manifest + 完整性 KPI + jsonl.gz → parquet + 生成 views"
        ),
    )
    p.add_argument(
        "--dataset-dir", type=str, default=None,
        help="export-dataset / dataset-validate 模式的数据集目录（服务器 /tmp 或本地 "
             "backend/.perfdata/review/<name>）",
    )
    p.add_argument(
        "--sample-bar-members", type=int, default=200,
        help="measure-all-scopes 模式下抽样估算单 member 400d bar 体积的样本数",
    )
    p.add_argument(
        "--benchmark-scopes", type=int, default=50,
        help="vec1-benchmark 模式：重叠度最高的 scope 数量（默认 50）",
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

    只读守卫在 ``_probe`` 内于连接建立后立即执行
    ``SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`` 并验证写被拒。
    不修改共享的 app.db 源码；probe 仅调用纯 SELECT 路径，不发出任何 DDL。
    """
    from app.db import AsyncSessionLocal

    return AsyncSessionLocal


async def _probe(
    scope_type: str,
    scope_key: str,
    history: int,
    asof_date: date | None,
) -> int:
    from sqlalchemy import text

    from app.db import AsyncSessionLocal
    from app.services.review_observation_prep_service import (
        list_recent_trading_days,
    )
    from app.services.review_scope_dynamics_service import (
        compute_current_static_scope_dynamics,
    )

    _build_readonly_engine()  # 复用 app.db 的 AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        # 只读守卫：连接建立后立即设为 read-only（对非超级用户角色生效）。
        # 注意：bz_stock 的 bz 角色是超级用户，PostgreSQL 不会对其强制
        # transaction_read_only，因此 DB 层只读无法由会话设置保证。
        # 本 probe 的只读性由 **代码审计保证**：被调用的正式 path
        # （reconstruct / prep / observation / dynamics）仅发出 SELECT，
        # 且 probe 本身不发出任何写语句。此处 SET 为纵深防御，验证仅作信息提示。
        await db.execute(text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"))
        try:
            await db.execute(
                text(
                    "WITH u AS (UPDATE market_boards SET name=name "
                    "WHERE id='00000000-0000-0000-0000-000000000000' "
                    "RETURNING 1) SELECT 1 FROM u"
                )
            )
            await db.rollback()
            logger.warning(
                "[readonly-check] 角色为超级用户，DB 层只读无法强制；"
                "只读性依赖代码审计（path 仅 SELECT）。继续运行（不写）。"
            )
        except Exception as e:  # noqa: BLE001 - 期望被 read-only 拒绝（非超级用户时）
            logger.info("[readonly-check] DML 被拒绝: %s", type(e).__name__)
            await db.rollback()
        # canonical trading-date axis（复用正式 owner，不自造时间轴）
        if asof_date is None:
            latest = await list_recent_trading_days(db, date.today(), 1)
            if not latest:
                logger.error("无法解析最新交易日（calendar 为空）")
                return 2
            asof_date = latest[0]

        trade_dates = await list_recent_trading_days(db, asof_date, history)
        if not trade_dates:
            logger.error("trade_dates 为空（history=%d）", history)
            return 2
        # list_recent_trading_days 返回降序，正式 path 要求严格升序
        trade_dates = sorted(trade_dates)

        logger.info(
            "[probe] scope_type=%s scope_key=%s asof_date=%s "
            "trade_date_count=%d window=[%s, %s]",
            scope_type, scope_key, asof_date.isoformat(),
            len(trade_dates),
            trade_dates[0].isoformat(), trade_dates[-1].isoformat(),
        )

        rss_before = _rss_mb()
        result = await compute_current_static_scope_dynamics(
            db,
            scope_type,
            scope_key,
            trade_dates,
            analysis_asof_date=asof_date,
        )
        rss_after = _rss_mb()

        membership = result.get("membership") or {}
        member_count = (
            membership.get("member_count")
            if isinstance(membership, dict)
            else len(membership)
        )
        scope_dynamics = result.get("scope_dynamics") or {}
        observation_series = result.get("observation_series") or {}

        # 诊断输出（只读，不拥有算法）
        print("=== Scope Dynamics Probe (read-only baseline) ===")
        print(f"scope_type        : {scope_type}")
        print(f"scope_key         : {scope_key}")
        print(f"asof_date         : {asof_date.isoformat()}")
        print(f"trade_date_count  : {len(trade_dates)}")
        print(f"member_count      : {member_count}")
        print(f"member_x_days     : {member_count * len(trade_dates)}")
        print(f"scope_dynamics_keys: {sorted(scope_dynamics.keys())}")
        print(f"observation_series_keys: {sorted(observation_series.keys())}")
        print(f"rss_before_mb     : {rss_before:.1f}")
        print(f"rss_after_mb      : {rss_after:.1f}")
        print(f"rss_delta_mb      : {rss_after - rss_before:.1f}")
        print("=== END ===")
        return 0


async def _measure_all_scopes(sample_bar_members: int) -> int:
    """measure-all-scopes 模式：枚举全量 scope，测物理数据量定 batch boundary。

    只发 SELECT，不写库。输出每 type 的板块数、unique member 总数、
    单 member 平均所属板块数（重叠度）、抽样估算的单 member 400d bar 体积，
    并据 union 一次加载的物理成本给出有界 batch size N 的建议。
    """
    from sqlalchemy import func, select, text

    from app.db import AsyncSessionLocal
    from app.models.bar import BarDaily
    from app.models.market_board import MarketBoard, MarketBoardMembership

    _build_readonly_engine()
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        )
        # 1) 枚举所有 board：industry(L1/L2/L3) + concept
        boards = (
            await db.execute(
                select(
                    MarketBoard.id,
                    MarketBoard.name,
                    MarketBoard.type,
                    MarketBoard.hierarchyLevel,
                ).where(MarketBoard.isActive.is_(True))
            )
        ).all()

        # 2) 枚举所有 membership（board -> instrument）
        memberships = (
            await db.execute(
                select(
                    MarketBoardMembership.boardId,
                    MarketBoardMembership.instrumentId,
                )
            )
        ).all()

        # 按 scope_type 分组（industry_l1/l2/l3, concept）
        def _scope_type_of(b_type: str, level: str) -> str:
            if b_type == "concept":
                return "concept"
            return f"industry_{level.lower()}"

        # 建 board_id -> scope_type 映射
        bid_to_st: dict = {}
        for bid, _bname, btype, level in boards:
            bid_to_st[bid] = _scope_type_of(btype, str(level))

        # 按 board 收集 member 集合
        per_board_members: dict = {}
        for bid, iid in memberships:
            st = bid_to_st.get(bid)
            if st is None:
                continue
            per_board_members.setdefault(bid, set()).add(iid)

        # 组织 boards_meta：每 type 的板块数、union member、重叠度
        boards_meta: dict = {}
        for bid, _bname, btype, level in boards:
            st = _scope_type_of(btype, str(level))
            boards_meta.setdefault(
                st, {"board_count": 0, "union": set(), "member_board_count": {}}
            )
            boards_meta[st]["board_count"] += 1
            mset = per_board_members.get(bid, set())
            boards_meta[st]["union"] |= mset
            for iid in mset:
                boards_meta[st]["member_board_count"][iid] = (
                    boards_meta[st]["member_board_count"].get(iid, 0) + 1
                )

        # 3) 抽样估算单 member 近 400d bar 体积
        from datetime import timedelta

        today = date.today()
        cutoff = today - timedelta(days=400)
        # union 全量 member（跨所有 type 合并，用于抽样代表性）
        all_union: set = set()
        for st in boards_meta:
            all_union |= boards_meta[st]["union"]

        sample_ids = list(all_union)[: max(1, sample_bar_members)]
        avg_bars = 0.0
        if sample_ids:
            bar_counts = (
                await db.execute(
                    select(
                        BarDaily.instrument_id,
                        func.count(BarDaily.trade_date),
                    )
                    .where(BarDaily.instrument_id.in_(sample_ids))
                    .where(BarDaily.trade_date >= cutoff)
                    .group_by(BarDaily.instrument_id)
                )
            ).all()
            if bar_counts:
                avg_bars = sum(c for _, c in bar_counts) / len(bar_counts)

        # 4) 输出诊断
        print("=== Scope Physical Volume Measurement (read-only) ===")
        print(f"sample_bar_members      : {len(sample_ids)}")
        print(f"avg_bars_per_member_400d: {avg_bars:.1f}")
        print()
        print(f"{'scope_type':<12} {'boards':>7} {'union_mems':>11} "
              f"{'avg_boards/mem':>14} {'max_boards/mem':>14}")
        suggested_n = {}
        for st in sorted(boards_meta):
            info = boards_meta[st]
            bc = info["member_board_count"]
            avg_bm = (sum(bc.values()) / len(bc)) if bc else 0.0
            max_bm = max(bc.values()) if bc else 0
            n_boards = info["board_count"]
            union_mems = len(info["union"])
            print(f"{st:<12} {n_boards:>7} {union_mems:>11} "
                  f"{avg_bm:>14.2f} {max_bm:>14}")
            # 建议 batch N：使单次 union 加载的 member 总量约等于
            # "单批 union member 上界"，这里取经验上界 4000 去反推 N。
            mem_per_batch_cap = 4000
            est_n = max(1, round(mem_per_batch_cap / max(1, avg_bm)))
            suggested_n[st] = est_n

        print()
        print("batch_size suggestion (union member cap ~4000):")
        for st in sorted(suggested_n):
            print(f"  {st:<12} -> N ~= {suggested_n[st]}")
        print("=== END ===")
        return 0


def _observations_close(a: Any, b: Any, *, rel_tol: float = 1e-9, abs_tol: float = 1e-9) -> bool:
    """Recursive structural equivalence for two ``compute_scope_observation`` dicts.

    Structural exactness for keys / lists / categorical values; float leaves use
    ``math.isclose`` so tiny ULP differences between the legacy and VEC-1 loop
    orders (e.g. sum order in a mean / weighted return) do not fail the check.
    """
    import math

    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, str)) and isinstance(b, (int, str)):
        return a == b
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return False
        return all(_observations_close(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_observations_close(x, y) for x, y in zip(a, b))
    return a == b


async def _vec1_benchmark(
    scope_type: str,
    history: int,
    asof_date: date | None,
    benchmark_scopes: int,
) -> int:
    """vec1-benchmark：对重叠度最高的 concept 子集执行 VEC-1 batch。

    只发 SELECT，不写库。报告物理成本指标（rules/25 §8.7 纯观测）：
      - scope_count / union_member_count / trade_date_count
      - scope_member_day_count / unique_member_day_count / duplication_factor
      - member build 调用次数从 scope×date（legacy）降到 date（VEC-1）
      - member_build_ms / scope_slice_ms / scope_observation_ms / total_ms

    legacy 与 vec1 两个路径都调用同一个 canonical owner
    （``_build_member_observations`` / ``compute_scope_observation``），只是循环
    顺序不同；本函数只计时并验证两者 PreparedScope 数量一致，不复制任何公式。
    """
    import time
    import uuid

    from sqlalchemy import select, text

    from app.db import AsyncSessionLocal
    from app.domain.review.scope_observation import compute_scope_observation
    from app.models.market_board import MarketBoard, MarketBoardMembership
    from app.services.review_observation_prep_service import (
        PreparedScope,
        _build_member_observations,
        list_recent_trading_days,
        prepare_scopes_from_union,
        prepare_union_fact_context,
    )

    _build_readonly_engine()
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        )
        try:
            await db.execute(
                text(
                    "WITH u AS (UPDATE market_boards SET name=name "
                    "WHERE id='00000000-0000-0000-0000-000000000000' "
                    "RETURNING 1) SELECT 1 FROM u"
                )
            )
            await db.rollback()
        except Exception as e:  # noqa: BLE001
            await db.rollback()

        if asof_date is None:
            latest = await list_recent_trading_days(db, date.today(), 1)
            if not latest:
                logger.error("无法解析最新交易日（calendar 为空）")
                return 2
            asof_date = latest[0]
        trade_dates = sorted(await list_recent_trading_days(db, asof_date, history))
        if not trade_dates:
            logger.error("trade_dates 为空（history=%d）", history)
            return 2

        # ---- 枚举 concept scope -> members（只读） ----
        boards = (
            await db.execute(
                select(MarketBoard.id, MarketBoard.name)
                .where(MarketBoard.type == "concept")
                .where(MarketBoard.isActive.is_(True))
            )
        ).all()
        memberships = (
            await db.execute(
                select(
                    MarketBoardMembership.boardId,
                    MarketBoardMembership.instrumentId,
                )
            )
        ).all()
        per_board: dict = {}
        member_board_count: dict = {}
        for bid, iid in memberships:
            per_board.setdefault(str(bid), []).append(uuid.UUID(str(iid)))
            member_board_count[str(iid)] = member_board_count.get(str(iid), 0) + 1
        # 重叠度 = 该 scope 平均每个 member 属于的 concept 数量（用全局计数估算）
        ranked: list[tuple[float, str, str]] = []
        for bid, bname in boards:
            mids = per_board.get(str(bid), [])
            if not mids:
                continue
            avg_share = sum(member_board_count.get(str(m), 0) for m in mids) / len(mids)
            ranked.append((avg_share, str(bid), str(bname) or str(bid)))
        ranked.sort(key=lambda x: x[0], reverse=True)
        top = ranked[:benchmark_scopes]
        if not top:
            logger.error("无 concept scope 可选（scope_type=%s）", scope_type)
            return 2
        scope_members: dict[str, tuple[list[uuid.UUID], str]] = {
            bid: (per_board[bid], bname) for _share, bid, bname in top
        }

        union_ids: list[uuid.UUID] = []
        seen: set = set()
        for mids, _n in scope_members.values():
            for m in mids:
                if m not in seen:
                    seen.add(m)
                    union_ids.append(m)
        scope_member_day_count = (
            sum(len(m) for m, _n in scope_members.values()) * len(trade_dates)
        )
        unique_member_day_count = len(union_ids) * len(trade_dates)
        duplication_factor = (
            scope_member_day_count / unique_member_day_count
            if unique_member_day_count
            else 0.0
        )

        print("=== VEC-1 Union Member-Day Benchmark (read-only) ===")
        print(f"scope_type            : {scope_type}")
        print(f"benchmark_scopes      : {len(scope_members)}")
        print(f"asof_date             : {asof_date.isoformat()}")
        print(f"trade_date_count      : {len(trade_dates)}")
        print(f"union_member_count    : {len(union_ids)}")
        print(f"scope_member_day_count: {scope_member_day_count}")
        print(f"unique_member_day_count: {unique_member_day_count}")
        print(f"duplication_factor    : {duplication_factor:.2f}")

        # ---- union facts 只加载一次（PERF-2） ----
        t0 = time.perf_counter()
        union_ctx = await prepare_union_fact_context(db, trade_dates, union_ids)
        union_fact_load_ms = (time.perf_counter() - t0) * 1000.0
        print(f"union_fact_load_ms    : {union_fact_load_ms:.1f}")

        prep_counters: dict[str, int] = {}
        prep_fallback: list[str] = []

        # ---- legacy：scope -> date -> member（before 基线，纯 member build 计时） ----
        legacy_members: dict[str, list] = {k: [] for k in scope_members}
        t0 = time.perf_counter()
        for scope_key, (member_ids, _n) in scope_members.items():
            for t in trade_dates:
                t1 = union_ctx.t1_by_date.get(t)
                states_t = union_ctx.states_by_date.get(t, {})
                states_t1 = union_ctx.states_by_date.get(t1, {}) if t1 else {}
                members = _build_member_observations(
                    list(member_ids),
                    trade_date=t, t1=t1, states_t=states_t, states_t1=states_t1,
                    bars=union_ctx.bars, current_only_facts={},
                    vec_volume=union_ctx.vec_volume,
                    counters=prep_counters, fallback_reasons=prep_fallback,
                )
                legacy_members[scope_key].append(members)
        legacy_member_build_ms = (time.perf_counter() - t0) * 1000.0

        # ---- VEC-1：date -> union member -> scope slice（after） ----
        t0 = time.perf_counter()
        vec1_prepared = await prepare_scopes_from_union(
            db, scope_type, trade_dates, scope_members, union_ctx,
            prep_counters=prep_counters, prep_fallback_reasons=prep_fallback,
        )
        vec1_prepare_ms = (time.perf_counter() - t0) * 1000.0

        # ---- scope observation + SEMANTIC EQUIVALENCE（审查项） ----
        # 对每个 scope x date，用同一 canonical ``compute_scope_observation``
        # 计算 legacy（按旧循环顺序、事件按 scope 过滤）与 VEC-1 两个 observation，
        # 逐字段比较（结构 exact，float 用 ULP 容忍）。legacy 复用 union_ctx 数据，
        # 仅循环顺序不同，因此能直接对比业务输出，而不是只对比 count。
        t0 = time.perf_counter()
        obs_count = 0
        mismatch_count = 0
        checked_count = 0
        for scope_key, (member_ids, _n) in scope_members.items():
            member_set = {str(m) for m in member_ids}
            for i, t in enumerate(trade_dates):
                t1 = union_ctx.t1_by_date.get(t)
                scope_events = tuple(
                    e for e in union_ctx.events_by_date.get(t, [])
                    if e.member_id in member_set
                )
                legacy_obs = compute_scope_observation(
                    scope_type=scope_type,
                    scope_key=scope_key,
                    trade_date=t,
                    pit_member_ids=tuple(str(m) for m in member_ids),
                    pit_member_ids_t1=tuple(str(m) for m in member_ids),
                    members=tuple(legacy_members[scope_key][i]),
                    events=scope_events,
                )
                prepared = vec1_prepared[scope_key][i]
                vec1_obs = compute_scope_observation(
                    scope_type=scope_type,
                    scope_key=scope_key,
                    trade_date=prepared.trade_date,
                    pit_member_ids=prepared.pit_member_ids,
                    pit_member_ids_t1=prepared.pit_member_ids_t1,
                    members=prepared.members,
                    events=prepared.events,
                )
                obs_count += 1
                checked_count += 1
                if not _observations_close(legacy_obs, vec1_obs):
                    mismatch_count += 1
        scope_observation_ms = (time.perf_counter() - t0) * 1000.0

        # ---- VEC-1B closure：batch dynamics vs per-scope single dynamics ----
        from app.services.review_scope_dynamics_service import (
            compute_current_static_scope_dynamics,
            compute_current_static_scope_dynamics_batch,
        )
        first_scope = next(iter(scope_members))
        single_result = await compute_current_static_scope_dynamics(
            db, scope_type, first_scope, trade_dates, analysis_asof_date=asof_date,
        )
        batch_results = await compute_current_static_scope_dynamics_batch(
            db, scope_type, [first_scope], trade_dates, analysis_asof_date=asof_date,
        )
        batch_result = batch_results[0]
        dynamics_equal = (
            batch_result["observation_series"] == single_result["observation_series"]
            and batch_result["scope_dynamics"] == single_result["scope_dynamics"]
        )
        batch_metrics = batch_result["metrics"]
        single_metrics = single_result["metrics"]

        # ---- 一致性：两路径 PreparedScope 数量相同 ----
        legacy_count = sum(len(v) for v in legacy_members.values())
        vec1_count = sum(len(v) for v in vec1_prepared.values())
        print(f"legacy_prepared_count : {legacy_count}")
        print(f"vec1_prepared_count   : {vec1_count}")
        print(f"member_build_calls    : legacy={len(scope_members) * len(trade_dates)} "
              f"vec1={len(trade_dates)}")
        print(f"member_build_ms       : legacy={legacy_member_build_ms:.1f} "
              f"vec1_prepare_and_slice_ms={vec1_prepare_ms:.1f}")
        print(f"scope_observation_ms  : {scope_observation_ms:.1f} (count={obs_count})")
        print(f"obs_equivalent        : checked={checked_count} mismatch={mismatch_count}")
        print(f"dynamics_batch_vs_single: equal={dynamics_equal} "
              f"scope={first_scope} "
              f"single_total_ms={single_metrics.get('total_ms', 0.0):.1f} "
              f"batch_total_ms={batch_metrics.get('batch_total_ms', 0.0):.1f} "
              f"batch_reconstruction_ms={batch_metrics.get('batch_reconstruction_ms', 0.0):.1f}")
        print(f"rss_mb                : {_rss_mb():.1f}")
        print(f"vec_hit={prep_counters.get('vec_hit', 0)} "
              f"vec_fallback={prep_counters.get('vec_fallback', 0)} "
              f"fallback_reasons={','.join(prep_fallback) or '-'}")
        print("=== END ===")
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

    await session.execute(text("SELECT 1"))
    await session.execute(text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"))
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
                    select(
                        MarketBoard.id, MarketBoard.externalCode, MarketBoard.name,
                        MarketBoard.type, MarketBoard.taxonomy, MarketBoard.source,
                        MarketBoard.taxonomyVersion, MarketBoard.taxonomyCompatibilityKey,
                        MarketBoard.hierarchyLevel, MarketBoard.parentBoardId,
                        MarketBoard.isActive, MarketBoard.membershipVersion,
                        MarketBoard.updatedAt,
                    ),
                    _serialize_row,
                    os.path.join(raw_dir, "boards.jsonl.gz"),
                )

                # ---- D3 board_memberships_current_snapshot（全量当前快照） ----
                row_counts["board_memberships_current_snapshot.jsonl.gz"] = await _stream_export(
                    db,
                    select(
                        MarketBoardMembership.boardId,
                        MarketBoardMembership.instrumentId,
                        MarketBoardMembership.updatedAt,
                    ),
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
                capture_status = "complete" if d5_count > 0 and bars_count > 0 else "partial"
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


def _compare_raw_parquet(raw_path: str, parquet_path: str, domain: str) -> dict:
    """Raw(jsonl.gz) ↔ Parquet 等价比对：row count / logical content / decimal roundtrip。"""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise RuntimeError(
            "optional replay dependency missing (pip install -r requirements-replay.txt)"
        ) from e
    raw_rows = list(_iter_jsonl_gz(raw_path))
    table = pq.read_table(parquet_path)
    result = {
        "row_mismatch": len(raw_rows) != table.num_rows,
        "content_mismatch": 0,
        "decimal_roundtrip_mismatch": 0,
    }
    if result["row_mismatch"]:
        return result
    dec_cols = _DECIMAL_COLUMNS.get(domain, {})
    cols = table.column_names
    col_lists = {name: table.column(name).to_pylist() for name in cols}
    schema = table.schema
    for i, rrow in enumerate(raw_rows):
        for name in cols:
            pv = col_lists[name][i]
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

    summary = {
        "instruments": instruments,
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
    }
    return summary


def _dataset_validate(dataset_dir: str) -> int:
    """本地校验 manifest + Integrity KPI + jsonl.gz → parquet + 生成 views + 数据质量摘要。"""
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
    os.makedirs(parquet_dir, exist_ok=True)
    os.makedirs(views_dir, exist_ok=True)

    print("=== REVIEW-REPLAY-DATASET validate ===")
    print(f"dataset_dir     : {dataset_dir}")
    print(f"dataset_id      : {manifest.get('dataset_id')}")
    print(f"asof            : {asof.isoformat()}")

    # 1) 文件 checksum（manifest.raw_files 的 compressed/content_sha256）
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

    # 2) Integrity KPI
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

    # 3) Parquet 转换 + Raw↔Parquet 等价（DATASET-3 层；lazy pyarrow）
    row_mismatch = 0
    content_mismatch = 0
    dec_roundtrip = 0
    for domain, stem in _RAW_FILE_STEMS.items():
        raw_path = os.path.join(raw_dir, f"{stem}.jsonl.gz")
        if not os.path.exists(raw_path):
            continue
        info = _rows_to_parquet(raw_dir, parquet_dir, domain, stem)
        eq = _compare_raw_parquet(raw_path, info["path"], domain)
        row_mismatch += 1 if eq["row_mismatch"] else 0
        content_mismatch += eq["content_mismatch"]
        dec_roundtrip += eq["decimal_roundtrip_mismatch"]
        print(f"  [parquet] {stem:<40} rows={info['rows']} "
              f"eq(row={0 if eq['row_mismatch'] else 1}, content={eq['content_mismatch']}, "
              f"decimal={eq['decimal_roundtrip_mismatch']})")
    print(f"parquet_row_mismatch  : {row_mismatch}")
    print(f"parquet_content_mismatch: {content_mismatch}")
    print(f"decimal_roundtrip_mismatch: {dec_roundtrip}")

    # 4) 生成 views + derived_instrument_ids == union(memberships) 校验
    boards = list(_iter_jsonl_gz(os.path.join(raw_dir, "boards.jsonl.gz")))
    memberships = list(
        _iter_jsonl_gz(os.path.join(raw_dir, "board_memberships_current_snapshot.jsonl.gz"))
    )
    views = build_views(boards, memberships)
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

    # 5) 数据质量摘要（derived，不写回 immutable raw manifest）
    summary = _data_quality_summary(raw_dir, lineage_dir, manifest)
    summary_path = os.path.join(dataset_dir, "quality_summary.json")
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    print("data quality summary:")
    print(f"  instruments            : {summary['instruments']}")
    print(f"  active/concept/industry: {summary['active_boards']}/{summary['concept_boards']}/{summary['industry_boards']}")
    print(f"  union_members          : {summary['union_members']}")
    print(f"  membership_overlap     : {summary['membership_overlap']}")
    print(f"  bar_count p10/p50/p90  : {summary['bar_count_per_instrument']}")
    print(f"  state_count p10/p50/p90: {summary['state_count_per_instrument']}")
    print(f"  snapshot_total/consumable: {summary['snapshot_total']}/{summary['snapshot_consumable']}")
    print(f"  events_by_type         : {len(summary['events_by_type'])} types")
    print("=== END ===")
    return 0


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
    if args.dry_run:
        logger.info(
            "[dry-run] mode=%s scope_type=%s scope_key=%s history=%d asof=%s OK",
            args.mode, args.scope_type, args.scope_key, args.history, asof,
        )
        return 0
    if args.mode == "measure-all-scopes":
        return await _measure_all_scopes(args.sample_bar_members)
    if args.mode == "vec1-benchmark":
        return await _vec1_benchmark(
            args.scope_type or "concept",
            args.history,
            asof,
            args.benchmark_scopes,
        )
    if not args.scope_type or not args.scope_key:
        logger.error("[single] --scope-type 与 --scope-key 为必填")
        return 2
    return await _probe(
        args.scope_type, args.scope_key, args.history, asof,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
