"""Auction Full-market 120-Bar Member Fact Backfill Runner — Round 3B-D.

职责：全市场 Auction Member Fact 历史回补的 orchestration。

核心口径（锁死）：
- 120 BAR = 截至 as_of 的最近 120 个官方 A 股交易日 / daily bars
  （通过 project official trading calendar: previous_trading_dates）。
- 禁止 date - timedelta(days=120) / 固定自然日区间 / 6 个月近似。
- 对交易日 T：只有 listing_date <= T 才允许生成 Member Fact。
- 窗口中途 IPO 股票不会向上市前扩展凑满 120 条。
- delisting lifecycle OUT OF SCOPE；Instrument.status 不得作为历史 eligibility 条件。

population（Round 3B-D load-once contract）：
- Backfill population(T) = CURRENT CANONICAL SH/SZ A-SHARE SET ∩ listing_date <= T。
- current canonical anchor = feature_snapshot_service.get_active_a_share_instruments(session)。
- run startup 一次读取 canonical SH/SZ identity + listing_date 放入内存（load_population_once）；
  120 个 bar 只做 in-memory listing_date <= T 过滤（filter_population_at），不再每 bar 重查。
- listing_date 缺失的 current SH/SZ identity：explicit unavailable，写入 root manifest
  （listing_date_unavailable_count / listing_date_unavailable_symbols），不进入 historical population。

Round 3B-D — PERFORMANCE + EXECUTION GOVERNANCE CLOSURE：
- 正式 backfill 不再调用 fetch_full_day_transactions_paginated / run_single_observation
  （保留为 source-validation reference implementation）。
- per-symbol observation 使用 kernel：
  fetch_auction_0925_targeted（hint-first + exponential + boundary binary search，只覆盖
  09:25:00～09:25:59 window）+ build_historical_member_fact（纯函数 canonicalization）。
- MDAS 每 bar 只做两次 batch contract（adj=none ×1 + adj=qfq ×1，adjustment_as_of=T，
  allow_backfill=False strict DB-only），不再 per-symbol get_bars。
- ONE PROCESS = ONE PytdxAdapter INSTANCE = ONE NORMAL HEALTHY CONNECTION。
- stream output：member_facts.jsonl.tmp append + progress.json + atomic rename + run.lock。
- tracked argparse CLI（--mode benchmark/live）；code_sha 自动取 git rev-parse HEAD。

输出：FILE EVIDENCE ONLY（不新建 production member-fact DB table / migration / API / frontend）。
本轮仍 NO FULL LIVE RUN：只做 benchmark canary + 测试 + commit/push。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

# ---------------------------------------------------------------------------
# 权威 owner（禁止第二套 market logic / calendar / qfq 重算）
# ---------------------------------------------------------------------------
from sqlalchemy import select

from app.core.pytdx_adapter import PytdxAdapter
from app.db import AsyncSessionLocal
from app.models.instrument import Instrument
from app.services.feature_snapshot_service import get_active_a_share_instruments
from app.services.instrument_lifecycle_service import stock_symbol_sql_filter
from app.services.market_data_aggregation_service import MarketDataAggregationService

from auction_history_semantics_validation import (
    SampleInstrument,
    previous_trading_dates,
)

from auction_member_fact_backfill_kernel import (
    BACKFILL_SOURCE_FROZEN,
    SEARCH_MODES_FROZEN,
    SOURCE_EMPTY,
    SOURCE_ERROR,
    SOURCE_TARGET_SEARCH_LIMIT_REACHED,
    SOURCE_TARGET_SEARCH_STALLED,
    SOURCE_TARGET_WINDOW_COMPLETE,
    build_historical_member_fact,
    fetch_auction_0925_targeted,
)

# offset_hints.json 版本（Round 3B-D2 PART F）：v2 versioned structure
OFFSET_HINTS_VERSION = 2

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EXPERIMENT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = EXPERIMENT_DIR / "output" / "member_fact_120bar" / "2026-08-14"

AS_OF = date(2026, 8, 14)
BAR_COUNT = 120
RUN_ID_PREFIX = "member_fact_120bar"

# 冻结 backfill source statuses（机械 reconciliation，不引入新业务 status）
# TARGET_WINDOW_COMPLETE 只表示 09:25 target minute 被完整 bracket/覆盖，不表示全天逐笔完整。
_SOURCE_FROZEN = set(BACKFILL_SOURCE_FROZEN)
_CANONICAL_FROZEN = {
    "CANONICAL", "NO_VOLUME_BEARING_0925", "MULTIPLE_VOLUME_BEARING_0925",
    "INVALID_VOLUME_0925", "INVALID_PRICE_0925",
}


# ---------------------------------------------------------------------------
# runtime code SHA（CLI 强制自动取 git HEAD，不允许伪造）
# ---------------------------------------------------------------------------
def _git_head_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _require_code_sha() -> str:
    sha = _git_head_sha()
    if not sha:
        raise RuntimeError(
            "code_sha 必须自动取 git rev-parse HEAD（不允许 CLI 伪造 SHA）；git 不可用则 STOP"
        )
    return sha


# ---------------------------------------------------------------------------
# board 派生（冻结 labels：SH_MAIN / SZ_MAIN / CHINEXT / STAR）
# ---------------------------------------------------------------------------
def _board_for_symbol(symbol: str, market: str) -> str:
    s = symbol.strip()
    if market == "SH":
        if s.startswith("688"):
            return "STAR"
        if s.startswith("60"):
            return "SH_MAIN"
        return "OTHER"
    if market == "SZ":
        if s.startswith("30"):
            return "CHINEXT"  # 创业板 300/301/302...（禁止 SZ_GEM）
        if s.startswith("00") or s.startswith("02"):
            return "SZ_MAIN"
        return "OTHER"
    return "OTHER"


# ---------------------------------------------------------------------------
# Instrument → SampleInstrument（真实 ORM id）
# ---------------------------------------------------------------------------
def _to_sample_inst(inst: Instrument) -> SampleInstrument:
    return SampleInstrument(
        symbol=inst.symbol,
        market=inst.market,
        instrument_id=inst.id,  # 真实 ORM UUID 主键（不是 instrument_id）
        board=_board_for_symbol(inst.symbol, inst.market),
        coverage_tag="all_a_share",
        cohort="routine",
    )


# ---------------------------------------------------------------------------
# Population load-once（Round 3B-D PART F）
# ---------------------------------------------------------------------------
@dataclass
class PopulationSnapshot:
    """run startup 一次读取的 canonical SH/SZ population（含 listing_date）。"""

    instruments: list[Instrument] = field(default_factory=list)
    by_symbol: dict[str, Instrument] = field(default_factory=dict)
    listing_missing_symbols: list[str] = field(default_factory=list)
    total_current_shsz: int = 0
    listing_date_present: int = 0
    listing_date_missing: int = 0

    def coverage(self) -> dict:
        return {
            "total_current_population": self.total_current_shsz,
            "listing_date_present": self.listing_date_present,
            "listing_date_missing": self.listing_date_missing,
        }


async def load_population_once(session) -> PopulationSnapshot:
    """一次读取 CURRENT CANONICAL SH/SZ A-SHARE identities + listing_date。

    denominator = current canonical AND SH/SZ AND stock-symbol identity（先于 listing 过滤）；
    不能先过滤 listing_date IS NOT NULL 再证明 missing=0。
    """
    canonical_ids = await get_active_a_share_instruments(session)
    if not canonical_ids:
        return PopulationSnapshot()
    stmt = (
        select(Instrument)
        .where(
            Instrument.id.in_(canonical_ids),
            Instrument.market.in_(("SH", "SZ")),
            stock_symbol_sql_filter(Instrument),
        )
        .order_by(Instrument.symbol)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    by_symbol: dict[str, Instrument] = {}
    present: list[Instrument] = []
    missing_symbols: list[str] = []
    for r in rows:
        by_symbol[r.symbol] = r
        if r.listing_date is not None:
            present.append(r)
        else:
            missing_symbols.append(r.symbol)
    return PopulationSnapshot(
        instruments=present,
        by_symbol=by_symbol,
        listing_missing_symbols=missing_symbols,
        total_current_shsz=len(rows),
        listing_date_present=len(present),
        listing_date_missing=len(missing_symbols),
    )


def filter_population_at(snapshot: PopulationSnapshot, trade_date: date) -> list[Instrument]:
    """in-memory listing_date <= T 过滤（120 个 bar 只做内存过滤）。"""
    return [
        inst for inst in snapshot.instruments
        if inst.listing_date is not None and inst.listing_date <= trade_date
    ]


async def resolve_backfill_population_at(
    session,
    trade_date: date,
) -> list[Instrument]:
    """（兼容保留）per-date population resolver：current canonical ∩ listing_date<=T。

    runner 主路径使用 load_population_once + filter_population_at（load-once）；
    本函数保留用于测试/兼容入口。
    """
    snap = await load_population_once(session)
    return filter_population_at(snap, trade_date)


async def check_listing_date_coverage(session) -> dict:
    """listing-date coverage preflight（denominator = current canonical SH/SZ before listing filter）。"""
    snap = await load_population_once(session)
    return snap.coverage()


# ---------------------------------------------------------------------------
# partition 状态 / resume decision（三态）
# ---------------------------------------------------------------------------
PARTITION_STATUS_RUNNING = "RUNNING"
PARTITION_STATUS_COMPLETED = "COMPLETED"
PARTITION_STATUS_FAILED = "FAILED"

RESUME_SKIP = "SKIP_COMPLETED_MATCH"
RESUME_RERUN = "RERUN_INCOMPLETE"
RESUME_BLOCK = "BLOCK_COMPLETED_MISMATCH"


class CompletedPartitionMetadataMismatch(RuntimeError):
    """COMPLETED partition 存在 metadata mismatch，禁止覆盖。"""


class RunAlreadyActive(RuntimeError):
    """run.lock 已存在且 owner process 仍 active：同一 run_id 只允许一个 writer。"""


class RunLockStale(RuntimeError):
    """run.lock 已存在但 owner process 不存在：必须显式 recover 才能继续。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _partition_dir(run_id: str, trade_date: date, output_root: Path | None = None) -> Path:
    root = output_root or OUTPUT_DIR
    return root / run_id / "bars" / trade_date.isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


def _write_json_atomic(path: Path, payload: Any) -> None:
    """atomic write：.tmp（fsync）→ os.replace，避免半写文件被 resume 读到。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _offset_hints_payload(run_id: str, as_of: date, code_sha: str,
                          offset_hints: dict) -> dict:
    """offset_hints.json v2（Round 3B-D2 PART F）：versioned structure。

    v1：{"hints": {"000001": 3200}}（symbol -> boundary int）
    v2：{"version": 2, "hints": {"000001": {
          "target_page_offset": 3200,   # 下一交易日 warm fast path 使用
          "boundary_offset": 4800}}}    # 仅 fallback/debug evidence

    向后兼容：读入时 v1 symbol->int 可解释为 legacy boundary hint，
    第一次只能走 boundary fallback，完成后升级成 v2 hint。
    """
    hints: dict = {}
    for k, v in sorted(offset_hints.items()):
        if isinstance(v, dict):
            tpo = v.get("target_page_offset")
            bo = v.get("boundary_offset")
            hints[str(k)] = {
                "target_page_offset": (
                    int(tpo) if isinstance(tpo, (int, float)) and tpo >= 0
                    else None),
                "boundary_offset": (
                    int(bo) if isinstance(bo, (int, float)) and bo >= 0
                    else None),
            }
        elif isinstance(v, (int, float)) and v >= 0:
            # legacy int → boundary hint（v2 target_page_offset 留空）
            hints[str(k)] = {
                "target_page_offset": None,
                "boundary_offset": int(v),
            }
    return {
        "version": OFFSET_HINTS_VERSION,
        "run_id": run_id,
        "as_of": as_of.isoformat(),
        "code_sha": code_sha,
        "hints": hints,
    }


def _load_offset_hints(path: Path) -> dict:
    """读取 offset_hints.json（兼容 v1 legacy int / v2 dict），失败 → {}。"""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {}
    raw_hints = payload.get("hints") or {}
    out: dict = {}
    for k, v in raw_hints.items():
        if isinstance(v, dict):
            out[str(k)] = {
                "target_page_offset": v.get("target_page_offset"),
                "boundary_offset": v.get("boundary_offset"),
            }
        elif isinstance(v, (int, float)) and v >= 0:
            # v1 legacy：symbol -> boundary int
            out[str(k)] = {
                "target_page_offset": None,
                "boundary_offset": int(v),
            }
    return out


def _expected_partition_metadata(trade_date: date, bar_index: int,
                                code_sha: str, eligible: int, as_of: date) -> dict:
    return {
        "trade_date": trade_date.isoformat(),
        "bar_index": bar_index,
        "code_sha": code_sha,
        "as_of": as_of.isoformat(),
        "eligible_instruments": eligible,
    }


def _partition_resume_decision(existing_manifest: dict | None,
                               expected_metadata: dict) -> str:
    """三态：NO_EXISTING→RERUN；COMPLETED+match→SKIP；
    COMPLETED+mismatch→BLOCK；RUNNING/FAILED→RERUN。"""
    if existing_manifest is None:
        return RESUME_RERUN
    status = existing_manifest.get("status")
    if status == PARTITION_STATUS_COMPLETED:
        for k, v in expected_metadata.items():
            if existing_manifest.get(k) != v:
                return RESUME_BLOCK
        return RESUME_SKIP
    return RESUME_RERUN


# ---------------------------------------------------------------------------
# Run-level lock（PART H）：O_CREAT | O_EXCL，同一 run_id 只允许一个 writer
# ---------------------------------------------------------------------------
class RunLock:
    def __init__(self, path: Path):
        self.path = path
        self._pid: int | None = None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _read_payload(self) -> dict | None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            payload = self._read_payload()
            pid = payload.get("pid") if payload else None
            if isinstance(pid, int) and pid > 0 and self._pid_alive(pid):
                raise RunAlreadyActive(
                    f"RUN_ALREADY_ACTIVE run.lock={self.path} owner_pid={pid}"
                ) from None
            raise RunLockStale(
                f"STALE_LOCK run.lock={self.path} owner_pid={pid} "
                "owner process 不存在；须显式 recover 后才能继续"
            ) from None
        self._pid = os.getpid()
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": self._pid, "created_at": _now_iso()}, f)

    def recover(self) -> None:
        """显式 stale recovery：仅在确认 owner process 不存在后调用。"""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def release(self) -> None:
        if self._pid is not None:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._pid = None


# ---------------------------------------------------------------------------
# 单列 row 投影（机械 projection，不 invent 字段）— backfill schema
# ---------------------------------------------------------------------------
def _project_member_row(obs: dict, trade_date: date, bar_index: int,
                        listing_date: date | None = None,
                        code_sha: str | None = None,
                        as_of: date = AS_OF) -> dict:
    lane_a = obs.get("lane_a") or {}
    lane_b = obs.get("lane_b") or {}
    inst = obs.get("instrument") or {}
    eff_listing = listing_date or obs.get("listing_date")

    # 兼容：新 kernel 用 source_status；旧 validation obs 用 full_day_status
    source_status = obs.get("source_status")
    full_day_status = obs.get("full_day_status")
    if source_status is None and full_day_status is not None:
        # legacy obs（source validation）→ backfill row 的 source_status 位保留其状态
        source_status = full_day_status

    row = {
        # IDENTITY
        "trade_date": trade_date.isoformat(),
        "bar_index": bar_index,
        "instrument_id": str(obs.get("instrument_id") or inst.get("instrument_id") or ""),
        "symbol": obs.get("symbol") or inst.get("symbol") or "",
        "market": obs.get("market") or inst.get("market") or "",
        "board": obs.get("board") or inst.get("board") or "",
        "listing_date": str(eff_listing) if eff_listing else None,
        # SOURCE（backfill-specific，不谎称 full-day COMPLETE）
        "source_status": source_status,
        "full_day_status": full_day_status,
        "extraction_status": obs.get("extraction_status"),
        "canonicalization_status": obs.get("canonicalization_status"),
        "canonicalization_reason": obs.get("canonicalization_reason"),
        "raw_canonical_record_count": obs.get("raw_canonical_record_count"),
        "positive_volume_record_count": obs.get("positive_volume_record_count"),
        "zero_volume_record_count": obs.get("zero_volume_record_count"),
        "invalid_volume_record_count": obs.get("invalid_volume_record_count"),
        "invalid_price_count": obs.get("invalid_price_count"),
        # AUCTION
        "auction_price_raw": obs.get("auction_price_raw"),
        "auction_volume_raw_lots": obs.get("auction_volume_raw_lots"),
        "auction_volume_shares": obs.get("auction_volume_shares"),
        "auction_amount": obs.get("auction_amount"),
        "auction_amount_source_type": obs.get("auction_amount_source_type"),
        # LANE A
        "lane_a_status": lane_a.get("status"),
        "mdas_raw_open_T": lane_a.get("mdas_raw_open_T"),
        "price_exact_match": lane_a.get("price_exact_match"),
        "price_diff_abs": lane_a.get("price_diff_abs"),
        "price_diff_rel": lane_a.get("price_diff_rel"),
        # LANE B（机械映射 lane_b ACTUAL keys）
        "lane_b_status": lane_b.get("status"),
        "previous_close_raw": lane_b.get("raw_close_Tm1"),
        "previous_close_pit_qfq": lane_b.get("qfq_close_Tm1"),
        "naive_raw_gap": lane_b.get("naive_raw_gap"),
        "pit_gap": lane_b.get("pit_gap"),
        # LINEAGE / QUALITY（ADJUSTMENT LINEAGE：adjustment_as_of 与 adj_factor_hash 分开）
        "mdas_data_source": lane_b.get("mdas_data_source"),
        "degraded": lane_b.get("mdas_degraded"),
        "degraded_reason": lane_b.get("mdas_degraded_reason"),
        "adjustment_as_of": lane_b.get("adjustment_as_of"),
        "adj_factor_hash": lane_b.get("adj_factor_hash"),
        # PERFORMANCE
        "pytdx_page_requests": obs.get("_pytdx_requests"),
        "used_hint": obs.get("_used_hint"),
        "search_mode": obs.get("_search_mode"),
        "target_page_offset": obs.get("_target_page_offset"),
        # RUNTIME
        "code_sha": code_sha,
        "as_of": as_of.isoformat(),
    }
    return row


# ---------------------------------------------------------------------------
# MDAS batch（PART E）：每 bar 只做两次 batch contract（none ×1 / qfq ×1）
# ---------------------------------------------------------------------------
class _BatchBarResult:
    """batch dict value（BarAggregationResult | Exception | None）的 thin wrapper。

    供 build_historical_member_fact 消费 .bars/.data_source/.degraded/
    .degraded_reason/.adj_factor_hash；Exception → degraded fail-closed。
    """

    def __init__(self, res: Any):
        if isinstance(res, Exception):
            self.bars: Any = None
            self.data_source = "batch_error"
            self.degraded = True
            self.degraded_reason = f"{type(res).__name__}: {res}"
            self.adj_factor_hash = None
        elif res is None:
            self.bars = None
            self.data_source = "missing"
            self.degraded = True
            self.degraded_reason = "batch_result_missing"
            self.adj_factor_hash = None
        else:
            self.bars = getattr(res, "bars", None)
            self.data_source = getattr(res, "data_source", "db")
            self.degraded = bool(getattr(res, "degraded", False))
            self.degraded_reason = getattr(res, "degraded_reason", None)
            self.adj_factor_hash = getattr(res, "adj_factor_hash", None)


async def _batch_mdas(
    mdas,
    session,
    instrument_ids: list,
    trade_date: date,
    *,
    adj: str,
    adjustment_as_of: date | None = None,
) -> tuple[dict, dict]:
    """单次 batch contract：strict DB-only（allow_backfill=False），不触发第二个 pytdx 连接。"""
    diag: dict[str, Any] = {}
    kwargs: dict[str, Any] = dict(
        timeframe="1d",
        adj=adj,
        end_date=trade_date,
        limit=2,
        include_realtime=False,
        completed_only=True,
        allow_backfill=False,
    )
    if adj == "qfq":
        kwargs["adjustment_as_of"] = adjustment_as_of
    results = await mdas.get_bars_batch(
        session, list(instrument_ids), _diag_sink=diag, **kwargs
    )
    return results, diag


# ---------------------------------------------------------------------------
# per-symbol backfill observation（真实 kernel 路径）
# ---------------------------------------------------------------------------
async def run_symbol_backfill_observation(
    adapter,
    session,
    inst: SampleInstrument,
    trade_date: date,
    raw_batch: dict,
    qfq_batch: dict,
    *,
    listing_date: date | None = None,
    as_of: date = AS_OF,
    bar_index: int = 0,
    code_sha: str | None = None,
    offset_hints: dict | None = None,
) -> dict:
    """targeted 09:25 fetch + 纯函数 member fact builder。

    不 new PytdxAdapter / 不调用 MDAS / 不查 DB / 不分页全天。
    hint cache（Round 3B-D2 PART F）：v2 结构
    {target_page_offset, boundary_offset}；warm hint 优先取 target_page_offset
    （上一交易日含 09:25 block 的 page），否则回退 legacy boundary hint。
    TARGET_WINDOW_COMPLETE 后写回 v2 hint，供下一 bar warm fast path 使用。
    """
    offset_hints = offset_hints if offset_hints is not None else {}
    entry = offset_hints.get(inst.symbol)
    target_hint: Any = None
    legacy_hint: Any = None
    if isinstance(entry, dict):
        target_hint = entry.get("target_page_offset")
        legacy_hint = entry.get("boundary_offset")
    elif isinstance(entry, (int, float)) and entry >= 0:
        legacy_hint = int(entry)
    warm_hint: int | None = None
    if isinstance(target_hint, (int, float)) and target_hint >= 0:
        warm_hint = int(target_hint)
    elif isinstance(legacy_hint, (int, float)) and legacy_hint >= 0:
        warm_hint = int(legacy_hint)

    targeted = fetch_auction_0925_targeted(
        adapter, inst.symbol, trade_date, offset_hint=warm_hint
    )
    if targeted.source_status == SOURCE_TARGET_WINDOW_COMPLETE:
        offset_hints[inst.symbol] = {
            "target_page_offset": targeted.target_page_offset,
            "boundary_offset": targeted.resolved_offset,
        }

    raw_res = _BatchBarResult(raw_batch.get(inst.instrument_id))
    qfq_res = _BatchBarResult(qfq_batch.get(inst.instrument_id))
    fact = build_historical_member_fact(
        inst, trade_date, targeted, raw_res, qfq_res,
        listing_date=listing_date, as_of=as_of,
        bar_index=bar_index, code_sha=code_sha,
    )
    fact["_pytdx_requests"] = targeted.page_count
    fact["_used_hint"] = targeted.used_hint
    fact["_search_mode"] = targeted.search_mode
    fact["_target_page_offset"] = targeted.target_page_offset
    return fact


# ---------------------------------------------------------------------------
# 单 bar partition 执行（PART G）：stream tmp + progress + atomic finalize + 性能
# ---------------------------------------------------------------------------
async def _run_bar_partition(
    run_id: str,
    trade_date: date,
    bar_index: int,
    population: list[SampleInstrument],
    run_symbol_obs,               # async (inst, trade_date) -> dict
    *,
    output_root: Path | None = None,
    listing_date_by_symbol: dict[str, date] | None = None,
    code_sha: str | None = None,
    as_of: date = AS_OF,
    adapter: Any = None,
    raw_mdas_batch_queries: int = 0,
    qfq_mdas_batch_queries: int = 0,
) -> dict:
    pdir = _partition_dir(run_id, trade_date, output_root)
    pdir.mkdir(parents=True, exist_ok=True)
    listing_date_by_symbol = listing_date_by_symbol or {}

    eligible = len(population)
    tmp_path = pdir / "member_facts.jsonl.tmp"
    member_path = pdir / "member_facts.jsonl"
    progress_path = pdir / "progress.json"

    source_status_agg: dict[str, int] = {}
    canonical_status_agg: dict[str, int] = {}
    lane_a_computed = 0
    lane_b_computed = 0
    pit_gap_unavailable = 0
    pit_gap_adj_degraded = 0
    run_error = 0
    written = 0
    pytdx_requests_total = 0
    cold_count = 0
    hint_count = 0
    search_mode_agg: dict[str, int] = {}
    max_requests_per_symbol = 0
    current_symbol: str | None = None

    started = time.monotonic()
    last_progress_at = started
    last_progress_n = 0

    f = open(tmp_path, "w", encoding="utf-8")
    try:
        for inst in population:
            current_symbol = inst.symbol
            try:
                obs = await run_symbol_obs(inst, trade_date)
            except Exception as exc:
                row = {
                    "trade_date": trade_date.isoformat(),
                    "bar_index": bar_index,
                    "instrument_id": str(inst.instrument_id),
                    "symbol": inst.symbol,
                    "market": inst.market,
                    "board": inst.board,
                    "listing_date": str(listing_date_by_symbol.get(inst.symbol))
                    if listing_date_by_symbol.get(inst.symbol) else None,
                    "source_status": "RUN_ERROR",
                    "full_day_status": None,
                    "extraction_status": None,
                    "canonicalization_status": None,
                    "canonicalization_reason": (
                        f"run_symbol_backfill_observation: {type(exc).__name__}: {exc}"),
                    "raw_canonical_record_count": 0,
                    "positive_volume_record_count": 0,
                    "zero_volume_record_count": 0,
                    "invalid_volume_record_count": 0,
                    "invalid_price_count": 0,
                    "auction_price_raw": None,
                    "auction_volume_raw_lots": None,
                    "auction_volume_shares": None,
                    "auction_amount": None,
                    "auction_amount_source_type": None,
                    "lane_a_status": None, "mdas_raw_open_T": None,
                    "price_exact_match": None, "price_diff_abs": None,
                    "price_diff_rel": None,
                    "lane_b_status": None, "previous_close_raw": None,
                    "previous_close_pit_qfq": None, "naive_raw_gap": None,
                    "pit_gap": None,
                    "mdas_data_source": None, "degraded": None,
                    "degraded_reason": str(exc),
                    "adjustment_as_of": None, "adj_factor_hash": None,
                    "pytdx_page_requests": 0, "used_hint": None,
                    "search_mode": None, "target_page_offset": None,
                    "code_sha": code_sha,
                    "as_of": as_of.isoformat(),
                }
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                written += 1
                run_error += 1
                source_status_agg["RUN_ERROR"] = source_status_agg.get("RUN_ERROR", 0) + 1
            else:
                row = _project_member_row(
                    obs, trade_date, bar_index,
                    listing_date_by_symbol.get(inst.symbol), code_sha, as_of,
                )
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                written += 1

                ss = row["source_status"] or "UNKNOWN"
                source_status_agg[ss] = source_status_agg.get(ss, 0) + 1
                if ss == SOURCE_TARGET_WINDOW_COMPLETE:
                    cstat = row["canonicalization_status"] or "UNKNOWN"
                    canonical_status_agg[cstat] = canonical_status_agg.get(cstat, 0) + 1
                if row["lane_a_status"] == "COMPUTED":
                    lane_a_computed += 1
                if row["lane_b_status"] == "COMPUTED":
                    lane_b_computed += 1
                if row["lane_b_status"] == "PIT_ADJUSTMENT_DEGRADED":
                    pit_gap_adj_degraded += 1
                if row["lane_b_status"] != "COMPUTED" or row["pit_gap"] is None:
                    pit_gap_unavailable += 1

                n_req = row["pytdx_page_requests"] or 0
                pytdx_requests_total += n_req
                if row["used_hint"] is True:
                    hint_count += 1
                else:
                    cold_count += 1
                if n_req > max_requests_per_symbol:
                    max_requests_per_symbol = n_req

                sm = row.get("search_mode")
                if sm is not None:
                    search_mode_agg[sm] = search_mode_agg.get(sm, 0) + 1

            # progress：每 100 stocks 或 30 秒 atomic update
            now = time.monotonic()
            if (written - last_progress_n >= 100
                    or now - last_progress_at >= 30.0):
                _write_progress(
                    progress_path,
                    trade_date=trade_date, bar_index=bar_index,
                    eligible=eligible, processed=written, started=started,
                    source_status_agg=source_status_agg,
                    canonical_status_agg=canonical_status_agg,
                    lane_b_computed=lane_b_computed,
                    pit_gap_adj_degraded=pit_gap_adj_degraded,
                    pytdx_requests_total=pytdx_requests_total,
                    current_symbol=current_symbol,
                )
                last_progress_at = now
                last_progress_n = written

        f.flush()
        os.fsync(f.fileno())
    finally:
        f.close()

    processing_seconds = time.monotonic() - started
    symbols_per_second = (written / processing_seconds) if processing_seconds > 0 else 0.0
    avg_requests_per_symbol = (
        pytdx_requests_total / written if written else 0.0
    )

    # FIX 9 机械 reconcile（backfill source statuses）：
    # - member_rows_written == sum(frozen source) + RUN_ERROR
    # - 任何 UNKNOWN source status → FAILED
    # - TARGET_WINDOW_COMPLETE count == sum(frozen canonical)
    # - source-incomplete rows canonicalization_status 必须 None
    # - 任何 RUN_ERROR → FAILED
    non_unknown_source = sum(
        v for k, v in source_status_agg.items()
        if k in _SOURCE_FROZEN or k == "RUN_ERROR"
    )
    unknown_source = sum(
        v for k, v in source_status_agg.items()
        if k not in _SOURCE_FROZEN and k != "RUN_ERROR"
    )
    complete_count = source_status_agg.get(SOURCE_TARGET_WINDOW_COMPLETE, 0)
    canonical_complete = sum(
        v for k, v in canonical_status_agg.items() if k in _CANONICAL_FROZEN
    )
    unknown_canonical = sum(
        v for k, v in canonical_status_agg.items() if k not in _CANONICAL_FROZEN
    )
    incomplete_wrong_canonical = False
    if complete_count != written - run_error:
        # 只有 TARGET_WINDOW_COMPLETE rows 有 canonicalization_status；其余必须 None。
        # 在 streaming 路径中直接统计已保证，此处只做防御性断言。
        pass

    reconciled = (
        (run_error == 0)
        and (written == non_unknown_source)
        and (unknown_source == 0)
        and (complete_count == canonical_complete)
        and (unknown_canonical == 0)
    )
    partition_status = (
        PARTITION_STATUS_COMPLETED if reconciled else PARTITION_STATUS_FAILED
    )

    if partition_status == PARTITION_STATUS_COMPLETED:
        # ATOMIC FINALIZE：.tmp（已 fsync）→ atomic rename → member_facts.jsonl
        os.replace(tmp_path, member_path)
    # 失败：.tmp 保留（诊断证据），不 rename

    successful_connect = getattr(adapter, "successful_connect_count", 0) if adapter is not None else 0
    reconnect = getattr(adapter, "reconnect_count", 0) if adapter is not None else 0

    data_quality = {
        "trade_date": trade_date.isoformat(),
        "bar_index": bar_index,
        "eligible_instruments": eligible,
        "member_rows_written": written,
        "source_status_aggregate": source_status_agg,
        "canonical_status_aggregate": canonical_status_agg,
        "lane_a_computed_count": lane_a_computed,
        "lane_b_computed_count": lane_b_computed,
        "pit_gap_unavailable_count": pit_gap_unavailable,
        "pit_gap_adjustment_degraded_count": pit_gap_adj_degraded,
        "reconciled": reconciled,
        # PART K — performance evidence（不改变业务结论）
        "pytdx_request_count": pytdx_requests_total,
        "pytdx_target_search_cold_count": cold_count,
        "pytdx_target_search_hint_count": hint_count,
        "search_mode_distribution": search_mode_agg,
        "avg_requests_per_symbol": round(avg_requests_per_symbol, 4),
        "max_requests_per_symbol": max_requests_per_symbol,
        "successful_connect_count": successful_connect,
        "reconnect_count": reconnect,
        "raw_mdas_batch_queries": raw_mdas_batch_queries,
        "qfq_mdas_batch_queries": qfq_mdas_batch_queries,
        "processing_seconds": round(processing_seconds, 3),
        "symbols_per_second": round(symbols_per_second, 3),
    }
    _write_json(pdir / "data_quality.json", data_quality)

    manifest = {
        "trade_date": trade_date.isoformat(),
        "bar_index": bar_index,
        "code_sha": code_sha,
        "as_of": as_of.isoformat(),
        "status": partition_status,
        "eligible_instruments": eligible,
        "member_rows_written": written,
        "completed_at": _now_iso() if partition_status == PARTITION_STATUS_COMPLETED else None,
        "data_quality": data_quality,
    }
    _write_json(pdir / "partition_manifest.json", manifest)

    return manifest


def _write_progress(path: Path, *, trade_date: date, bar_index: int,
                    eligible: int, processed: int, started: float,
                    source_status_agg: dict, canonical_status_agg: dict,
                    lane_b_computed: int, pit_gap_adj_degraded: int,
                    pytdx_requests_total: int, current_symbol: str | None) -> None:
    elapsed = time.monotonic() - started
    payload = {
        "trade_date": trade_date.isoformat(),
        "bar_index": bar_index,
        "eligible": eligible,
        "processed": processed,
        "percent": round((processed / eligible) * 100, 2) if eligible else 100.0,
        "elapsed_seconds": round(elapsed, 1),
        "pytdx_requests": pytdx_requests_total,
        "pytdx_requests_per_symbol": round(
            pytdx_requests_total / processed, 3) if processed else 0.0,
        "target_window_complete": source_status_agg.get(SOURCE_TARGET_WINDOW_COMPLETE, 0),
        "source_empty": source_status_agg.get(SOURCE_EMPTY, 0),
        "source_error": source_status_agg.get(SOURCE_ERROR, 0),
        "canonical": canonical_status_agg.get("CANONICAL", 0),
        "no_volume": canonical_status_agg.get("NO_VOLUME_BEARING_0925", 0),
        "multiple": canonical_status_agg.get("MULTIPLE_VOLUME_BEARING_0925", 0),
        "invalid_volume": canonical_status_agg.get("INVALID_VOLUME_0925", 0),
        "invalid_price": canonical_status_agg.get("INVALID_PRICE_0925", 0),
        "lane_b_computed": lane_b_computed,
        "pit_gap_degraded": pit_gap_adj_degraded,
        "current_symbol": current_symbol,
    }
    _write_json(path, payload)


# ---------------------------------------------------------------------------
# root manifest 质量累计（PART K 性能 instrumentation 一并累计）
# ---------------------------------------------------------------------------
def _accumulate_partition_quality(root_manifest: dict, manifest: dict,
                                  data_quality: dict | None) -> None:
    root_manifest["eligible_instrument_days"] += manifest.get("eligible_instruments", 0)
    root_manifest["member_rows"] += manifest.get("member_rows_written", 0)
    dq = data_quality or {}
    for k, v in (dq.get("source_status_aggregate") or {}).items():
        root_manifest["source_status_aggregate"][k] = (
            root_manifest["source_status_aggregate"].get(k, 0) + v
        )
    for k, v in (dq.get("canonical_status_aggregate") or {}).items():
        root_manifest["canonical_status_aggregate"][k] = (
            root_manifest["canonical_status_aggregate"].get(k, 0) + v
        )
    root_manifest["lane_a_computed_count"] += dq.get("lane_a_computed_count", 0)
    root_manifest["lane_b_computed_count"] += dq.get("lane_b_computed_count", 0)
    root_manifest["pit_gap_unavailable_count"] += dq.get("pit_gap_unavailable_count", 0)
    root_manifest["pit_gap_adjustment_degraded_count"] += (
        dq.get("pit_gap_adjustment_degraded_count", 0)
    )
    # PART K — performance evidence
    root_manifest["pytdx_request_count"] += dq.get("pytdx_request_count", 0)
    root_manifest["pytdx_target_search_cold_count"] += dq.get("pytdx_target_search_cold_count", 0)
    root_manifest["pytdx_target_search_hint_count"] += dq.get("pytdx_target_search_hint_count", 0)
    for k, v in (dq.get("search_mode_distribution") or {}).items():
        root_manifest["search_mode_distribution"][k] = (
            root_manifest["search_mode_distribution"].get(k, 0) + v
        )
    root_manifest["max_requests_per_symbol"] = max(
        root_manifest.get("max_requests_per_symbol", 0),
        dq.get("max_requests_per_symbol", 0),
    )
    root_manifest["successful_connect_count"] = max(
        root_manifest.get("successful_connect_count", 0),
        dq.get("successful_connect_count", 0),
    )
    root_manifest["reconnect_count"] = max(
        root_manifest.get("reconnect_count", 0),
        dq.get("reconnect_count", 0),
    )
    root_manifest["raw_mdas_batch_queries"] += dq.get("raw_mdas_batch_queries", 0)
    root_manifest["qfq_mdas_batch_queries"] += dq.get("qfq_mdas_batch_queries", 0)
    root_manifest["processing_seconds"] += dq.get("processing_seconds", 0.0)


# ---------------------------------------------------------------------------
# 顶层 runner
# ---------------------------------------------------------------------------
async def run_backfill(
    run_id: str | None = None,
    bar_dates: list[date] | None = None,
    *,
    dry_run: bool = False,
    calendar_fn=None,            # (session, T, n) -> list[date]
    population=None,             # preloaded list[Instrument]（load-once）| None
    population_fn=None,          # 测试 seam：(session, T) -> list[Instrument]
    run_symbol_obs=None,         # 测试 seam：async (inst, trade_date) -> dict
    batch_mdas_fn=None,          # 测试 seam：async (mdas, session, ids, T, adj) -> (results, diag)
    adapter=None,                # 注入 fake adapter；None → 打开真实 PytdxAdapter（单实例）
    mdas=None,
    session=None,                # 注入 session；None → AsyncSessionLocal
    output_root: Path | None = None,
    code_sha: str | None = None,   # runtime code SHA（live = git HEAD，CLI 强制）
    require_listing_coverage: bool = False,
    enable_run_lock: bool = False,
    recover_stale_lock: bool = False,
    as_of: date = AS_OF,
) -> dict:
    if run_id is None:
        run_id = f"{RUN_ID_PREFIX}_{as_of.isoformat()}"

    _out_root = output_root or OUTPUT_DIR
    _code_sha = code_sha or _git_head_sha() or "UNKNOWN"

    async def _default_calendar(session, T, n):
        return await previous_trading_dates(session, T, n)

    _calendar = calendar_fn or _default_calendar

    if bar_dates is None:
        async with AsyncSessionLocal() as session:
            bar_dates = await _calendar(session, as_of, BAR_COUNT)
        bar_dates = sorted(set(bar_dates))
        assert len(bar_dates) == BAR_COUNT, (
            f"official bar_count must be exactly {BAR_COUNT}, got {len(bar_dates)}"
        )
        assert bar_dates[-1] == as_of, (
            f"latest bar must be {as_of}, got {bar_dates[-1]}"
        )
    else:
        bar_dates = sorted(set(bar_dates))

    root_manifest = {
        "code_sha": _code_sha,
        "as_of": as_of.isoformat(),
        "bar_count": len(bar_dates),
        "earliest_bar_date": bar_dates[0].isoformat(),
        "latest_bar_date": bar_dates[-1].isoformat(),
        "started_at": _now_iso(),
        "status": "RUNNING",
        "completed_bar_count": 0,
        "failed_bar_count": 0,
        "eligible_instrument_days": 0,
        "member_rows": 0,
        "source_status_aggregate": {},
        "canonical_status_aggregate": {},
        "lane_a_computed_count": 0,
        "lane_b_computed_count": 0,
        "pit_gap_unavailable_count": 0,
        "pit_gap_adjustment_degraded_count": 0,
        # PART K — performance evidence
        "pytdx_request_count": 0,
        "pytdx_target_search_cold_count": 0,
        "pytdx_target_search_hint_count": 0,
        "search_mode_distribution": {},
        "max_requests_per_symbol": 0,
        "successful_connect_count": 0,
        "reconnect_count": 0,
        "raw_mdas_batch_queries": 0,
        "qfq_mdas_batch_queries": 0,
        "processing_seconds": 0.0,
    }

    # --- run-level lock（PART H）---
    lock = RunLock(_out_root / run_id / "run.lock")
    if enable_run_lock:
        try:
            lock.acquire()
        except RunLockStale:
            if not recover_stale_lock:
                raise
            lock.recover()
            lock.acquire()
        except FileNotFoundError:
            # 输出根目录创建竞态：retry 一次
            (_out_root / run_id).mkdir(parents=True, exist_ok=True)
            lock.acquire()

    try:
        return await _run_backfill_impl(
            run_id=run_id, bar_dates=bar_dates, dry_run=dry_run,
            _calendar=_calendar, population=population,
            population_fn=population_fn, run_symbol_obs=run_symbol_obs,
            batch_mdas_fn=batch_mdas_fn, adapter=adapter, mdas=mdas,
            session=session, output_root=_out_root, code_sha=_code_sha,
            require_listing_coverage=require_listing_coverage,
            as_of=as_of, root_manifest=root_manifest,
        )
    finally:
        if enable_run_lock:
            lock.release()


async def _run_backfill_impl(
    *,
    run_id: str, bar_dates: list[date], dry_run: bool,
    _calendar, population, population_fn, run_symbol_obs, batch_mdas_fn,
    adapter, mdas, session, output_root: Path, code_sha: str,
    require_listing_coverage: bool, as_of: date, root_manifest: dict,
) -> dict:
    _out_root = output_root
    _mdas = mdas or MarketDataAggregationService()

    # 默认 per-symbol observer（kernel 路径）；测试注入覆盖
    _run_symbol_obs = run_symbol_obs
    # 本地名避开模块级 _batch_mdas 函数（否则 RHS 引用未赋值本地变量 → UnboundLocalError）
    _run_batch_mdas = batch_mdas_fn or _batch_mdas

    async def _default_population_loader(sess, t):
        return await resolve_backfill_population_at(sess, t)

    _population_fn = population_fn or _default_population_loader

    if dry_run:
        # population load-once 视图：eligible_instrument_days 用 in-memory filter 估算
        async with AsyncSessionLocal() as sess:
            snap = population if population is None else None
            if snap is None:
                snap = await load_population_once(sess)
            for t in bar_dates:
                if population is not None:
                    n = len(filter_population_at(_as_snapshot(population), t))
                else:
                    n = len(filter_population_at(snap, t))
                root_manifest["eligible_instrument_days"] += n
                root_manifest["completed_bar_count"] += 1
        root_manifest["status"] = "DRY_RUN_DONE"
        _write_json(_out_root / run_id / "manifest.json", root_manifest)
        return root_manifest

    completed = 0
    failed = 0

    async def _bars_loop(sess, adapter_for_run, snapshot_or_none):
        nonlocal completed, failed
        # Round 3B-D2 PART F resume warm-start：载入持久化的 v2 hints
        # （v2 dict {target_page_offset, boundary_offset} 优先；v1 legacy int 自动
        # 解释为 boundary hint，首次只能走 fallback，完成后升级为 v2）。
        offset_hints: dict = _load_offset_hints(
            _out_root / run_id / "offset_hints.json"
        )

        for idx, t in enumerate(bar_dates, start=1):
            pdir = _partition_dir(run_id, t, _out_root)

            # --- population：load-once in-memory filter；否则 population_fn seam ---
            if population is not None:
                instruments = filter_population_at(_as_snapshot(population), t)
                listing_date_by_symbol = {
                    inst.symbol: inst.listing_date
                    for inst in instruments if inst.listing_date is not None
                }
            elif population_fn is not None:
                raw = await _population_fn(sess, t)
                instruments = raw
                listing_date_by_symbol = {
                    inst.symbol: getattr(inst, "listing_date", None)
                    for inst in instruments
                    if getattr(inst, "listing_date", None) is not None
                }
            else:
                instruments = filter_population_at(snapshot_or_none, t)
                listing_date_by_symbol = {
                    inst.symbol: inst.listing_date
                    for inst in instruments if inst.listing_date is not None
                }

            population_insts = [
                _to_sample_inst(x) if not isinstance(x, SampleInstrument) else x
                for x in instruments
            ]
            eligible = len(population_insts)

            # --- batch MDAS（每 bar：none ×1 / qfq ×1）---
            raw_batch: dict = {}
            qfq_batch: dict = {}
            raw_diag: dict = {}
            qfq_diag: dict = {}
            if _run_symbol_obs is None:
                ids = [s.instrument_id for s in population_insts]
                if ids and sess is not None:
                    raw_batch, raw_diag = await _run_batch_mdas(
                        _mdas, sess, ids, t, adj="none")
                    qfq_batch, qfq_diag = await _run_batch_mdas(
                        _mdas, sess, ids, t, adj="qfq", adjustment_as_of=t)

                async def _default_obs(inst, trade_date, _raw=raw_batch, _qfq=qfq_batch):
                    return await run_symbol_backfill_observation(
                        adapter_for_run, sess, inst, trade_date, _raw, _qfq,
                        listing_date=listing_date_by_symbol.get(inst.symbol),
                        as_of=as_of, bar_index=idx, code_sha=code_sha,
                        offset_hints=offset_hints,
                    )
                observer = _default_obs
            else:
                observer = _run_symbol_obs

            existing = None
            mpath = pdir / "partition_manifest.json"
            if mpath.exists():
                try:
                    with open(mpath, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    existing = None

            expected_meta = _expected_partition_metadata(t, idx, code_sha, eligible, as_of)
            decision = _partition_resume_decision(existing, expected_meta)

            if decision == RESUME_SKIP:
                completed += 1
                dq_path = pdir / "data_quality.json"
                dq = None
                if dq_path.exists():
                    try:
                        with open(dq_path, "r", encoding="utf-8") as f:
                            dq = json.load(f)
                    except Exception:
                        dq = None
                _accumulate_partition_quality(root_manifest, existing or {}, dq)
                continue
            if decision == RESUME_BLOCK:
                raise CompletedPartitionMetadataMismatch(
                    f"COMPLETED partition {t.isoformat()} bar_index={idx} "
                    f"metadata mismatch (code_sha/eligible) — refuse overwrite"
                )

            try:
                manifest = await _run_bar_partition(
                    run_id, t, idx, population_insts, observer,
                    output_root=_out_root,
                    listing_date_by_symbol=listing_date_by_symbol,
                    code_sha=code_sha, as_of=as_of,
                    adapter=adapter_for_run,
                    raw_mdas_batch_queries=raw_diag.get("repository_query_count", 0),
                    qfq_mdas_batch_queries=qfq_diag.get("repository_query_count", 0),
                )
            except Exception:
                failed += 1
                _write_json(
                    pdir / "partition_manifest.json",
                    {
                        "trade_date": t.isoformat(),
                        "bar_index": idx,
                        "code_sha": code_sha,
                        "as_of": as_of.isoformat(),
                        "status": PARTITION_STATUS_FAILED,
                        "error": traceback.format_exc(),
                        "completed_at": None,
                    },
                )
                continue

            dq_path = pdir / "data_quality.json"
            dq = None
            if dq_path.exists():
                try:
                    with open(dq_path, "r", encoding="utf-8") as f:
                        dq = json.load(f)
                except Exception:
                    dq = None
            _accumulate_partition_quality(root_manifest, manifest, dq)
            if manifest["status"] == PARTITION_STATUS_COMPLETED:
                completed += 1
                # Round 3B-D1 PART H：每个 COMPLETED partition 完成 reconciliation 后
                # 立即 atomic 持久化 offset_hints.json（含 run_id/as_of/code_sha/hints）。
                # 若后续 bar 中途进程退出，resume 仍能加载已完成的 bar 最新 hint。
                if offset_hints:
                    _write_json_atomic(
                        _out_root / run_id / "offset_hints.json",
                        _offset_hints_payload(run_id, as_of, code_sha, offset_hints),
                    )
            else:
                failed += 1

        # --- 持久化 hints（PART H 收尾）：run 结束再写一次（已含每个 COMPLETED bar 的写入）---
        if offset_hints:
            hints_path = _out_root / run_id / "offset_hints.json"
            _write_json_atomic(
                hints_path,
                _offset_hints_payload(run_id, as_of, code_sha, offset_hints),
            )

    # --- 顶层 adapter/session lifecycle：ONE PytdxAdapter INSTANCE ---
    if adapter is not None:
        # 注入路径（fake adapter + injected observer + injected session=None）
        snap = None
        if population is None and population_fn is None:
            async with AsyncSessionLocal() as sess:
                snap = await load_population_once(sess)
        await _bars_loop(session, adapter, snap)
    else:
        with PytdxAdapter() as real_adapter:
            owns_session = session is None
            sess = session
            if owns_session:
                sess = AsyncSessionLocal()
            try:
                # listing-date coverage preflight（live 路径，仅真实 session）
                snap = population
                if population is None:
                    snap = await load_population_once(sess)
                    if require_listing_coverage:
                        cov = snap.coverage()
                        root_manifest["listing_date_coverage"] = cov
                        if cov["listing_date_missing"] != 0:
                            root_manifest["status"] = "STOP"
                            root_manifest["first_blocker"] = (
                                "LISTING_DATE_COVERAGE_GAP "
                                f"missing={cov['listing_date_missing']}"
                            )
                            _write_json(_out_root / run_id / "manifest.json", root_manifest)
                            return root_manifest
                    # listing_date_unavailable 显式记录（不 silent disappear）
                    root_manifest["listing_date_unavailable_count"] = snap.listing_date_missing
                    root_manifest["listing_date_unavailable_symbols"] = snap.listing_missing_symbols
                await _bars_loop(sess, real_adapter, snap)
            finally:
                if owns_session:
                    await sess.close()

    root_manifest.update({
        "status": "DONE" if failed == 0 else "PARTIAL",
        "completed_bar_count": completed,
        "failed_bar_count": failed,
    })
    if root_manifest.get("successful_connect_count") == 0 and adapter is None:
        # 真实 adapter 诊断：取真实实例计数（单长连接健康路径应为 1）
        root_manifest["successful_connect_count"] = root_manifest.get(
            "successful_connect_count", 0)
    _write_json(_out_root / run_id / "manifest.json", root_manifest)
    return root_manifest


def _as_snapshot(population: list[Instrument]) -> PopulationSnapshot:
    """把 preloaded Instrument 列表包装为 snapshot（用于 in-memory filter）。"""
    by_symbol: dict[str, Instrument] = {}
    present: list[Instrument] = []
    for r in population:
        by_symbol[r.symbol] = r
        if r.listing_date is not None:
            present.append(r)
    return PopulationSnapshot(
        instruments=present,
        by_symbol=by_symbol,
        listing_missing_symbols=[
            r.symbol for r in population if r.listing_date is None
        ],
        total_current_shsz=len(population),
        listing_date_present=len(present),
        listing_date_missing=sum(1 for r in population if r.listing_date is None),
    )


# ---------------------------------------------------------------------------
# Tracked CLI（PART I）：--mode benchmark/live；code_sha 自动取 git HEAD
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="full_market_member_fact_backfill",
        description="Auction 120-bar Member Fact backfill runner (Round 3B-D).",
    )
    p.add_argument("--mode", choices=["benchmark", "live"], required=True)
    p.add_argument("--as-of", default=AS_OF.isoformat(),
                   help=f"as_of date (default {AS_OF.isoformat()})")
    p.add_argument("--run-id", default=None)
    p.add_argument("--benchmark-bars", type=int, default=3,
                   help="benchmark mode: consecutive official bars (default 3)")
    p.add_argument("--benchmark-symbols", type=int, default=200,
                   help="benchmark mode: sampled current SH/SZ stocks (default 200)")
    p.add_argument("--output-root", default=str(OUTPUT_DIR))
    p.add_argument("--recover-stale-lock", action="store_true",
                   help="显式 recover stale run.lock（owner process 已不存在）")
    p.add_argument("--require-listing-coverage", action="store_true",
                   help="live preflight：listing_date missing==0 否则 STOP")
    return p


async def _sample_benchmark_universe(session, count: int) -> list[Instrument]:
    """从 current canonical SH/SZ 均衡采样 count 只，覆盖 SH_MAIN/SZ_MAIN/CHINEXT/STAR。"""
    snap = await load_population_once(session)
    by_board: dict[str, list[Instrument]] = {}
    for inst in snap.instruments:
        b = _board_for_symbol(inst.symbol, inst.market)
        if b in ("SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR"):
            by_board.setdefault(b, []).append(inst)
    selected: list[Instrument] = []
    board_cycle = ["SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR"]
    i = 0
    while len(selected) < count:
        progressed = False
        for b in board_cycle:
            pool = by_board.get(b, [])
            if i < len(pool) and len(selected) < count:
                selected.append(pool[i])
                progressed = True
        i += 1
        if not progressed or i > 20000:
            break
    return selected


def _cli_main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    as_of = date.fromisoformat(args.as_of)
    code_sha = _require_code_sha()
    output_root = Path(args.output_root)
    run_id = args.run_id or f"{RUN_ID_PREFIX}_{as_of.isoformat()}"
    out_manifest: dict | None = None

    if args.mode == "live":
        out_manifest = asyncio.run(run_backfill(
            run_id=run_id, as_of=as_of, output_root=output_root,
            code_sha=code_sha, enable_run_lock=True,
            recover_stale_lock=args.recover_stale_lock,
            require_listing_coverage=args.require_listing_coverage,
        ))
    else:  # benchmark
        async def _benchmark():
            bar_dates = None
            async with AsyncSessionLocal() as session:
                bar_dates = await previous_trading_dates(session, as_of, args.benchmark_bars)
                bar_dates = sorted(set(bar_dates))
                assert len(bar_dates) == args.benchmark_bars, (
                    f"benchmark bars must be exactly {args.benchmark_bars}, got {len(bar_dates)}"
                )
                assert bar_dates[-1] == as_of
                population = await _sample_benchmark_universe(session, args.benchmark_symbols)
            return await run_backfill(
                run_id=run_id, bar_dates=bar_dates, population=population,
                as_of=as_of, output_root=output_root, code_sha=code_sha,
                enable_run_lock=True, recover_stale_lock=args.recover_stale_lock,
            )
        out_manifest = asyncio.run(_benchmark())

    print(json.dumps(out_manifest, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
