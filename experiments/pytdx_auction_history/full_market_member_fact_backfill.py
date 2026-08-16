"""Auction Full-market 120-Bar Member Fact Backfill Runner — Round 3B-B / 3B-B1.

职责：全市场 Auction Member Fact 历史回补的 orchestration。

核心口径（锁死）：
- 120 BAR = 截至 as_of 的最近 120 个官方 A 股交易日 / daily bars
  （通过 project official trading calendar: previous_trading_dates）。
- 禁止 date - timedelta(days=120) / 固定自然日区间 / 6 个月近似。
- 对交易日 T：只有 listing_date <= T 才允许生成 Member Fact。
- 窗口中途 IPO 股票不会向上市前扩展凑满 120 条。
- delisting lifecycle OUT OF SCOPE；Instrument.status 不得作为历史 eligibility 条件。

population（Round 3B-B1 简化合同）：
- Backfill population(T) = CURRENT CANONICAL SH/SZ A-SHARE SET ∩ listing_date <= T。
- current canonical anchor = feature_snapshot_service.get_active_a_share_instruments(session)。
- resolve_backfill_population_at(session, T) 复用 get_active_a_share_instruments + SQL
  （Instrument.id IN canonical AND market in SH/SZ AND stock_symbol_sql_filter AND
   listing_date IS NOT NULL AND listing_date <= T）。
- 不修改 instrument_lifecycle_service。

qfq degraded fail-close：pit_gap=None + lane_b.status=PIT_ADJUSTMENT_DEGRADED（compute_lane_b）。
adjustment_as_of = target（该 historical T 的 PIT qfq anchor）；adj_factor_hash 单独保留。

输出：FILE EVIDENCE ONLY（不新建 production member-fact DB table / migration / API / frontend）。
本轮仍 NO FULL LIVE RUN：只做 live-path wiring closure + 测试 + commit/push。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 权威 owner（禁止第二套 market logic / calendar / qfq 重算）
# ---------------------------------------------------------------------------
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.instrument import Instrument
from app.services.instrument_lifecycle_service import (
    stock_symbol_sql_filter,
)
from app.services.feature_snapshot_service import get_active_a_share_instruments
from app.core.pytdx_adapter import PytdxAdapter
from app.services.market_data_aggregation_service import MarketDataAggregationService

from auction_history_semantics_validation import (
    run_single_observation,
    SampleInstrument,
    previous_trading_dates,
)

# 可在此覆写（live CLI / 测试显式注入）；默认不写死旧 SHA。
DEFAULT_CODE_SHA: str | None = None

EXPERIMENT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = (
    EXPERIMENT_DIR / "output" / "member_fact_120bar" / "2026-08-14"
)

AS_OF = date(2026, 8, 14)
BAR_COUNT = 120
RUN_ID_PREFIX = "member_fact_120bar"

# 冻结 source / canonical statuses（机械 reconciliation，不引入新业务 status）
_SOURCE_FROZEN = {
    "COMPLETE", "EMPTY", "SOURCE_ERROR",
    "PAGINATION_STALLED", "PAGINATION_LIMIT_REACHED",
}
_CANONICAL_FROZEN = {
    "CANONICAL", "NO_VOLUME_BEARING_0925", "MULTIPLE_VOLUME_BEARING_0925",
    "INVALID_VOLUME_0925", "INVALID_PRICE_0925",
}


# ---------------------------------------------------------------------------
# runtime code SHA（FIX 10）
# ---------------------------------------------------------------------------
def _git_head_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip() or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# board 派生（冻结 labels：SH_MAIN / SZ_MAIN / CHINEXT / STAR）— FIX 4
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
# Instrument → SampleInstrument（FIX 1：真实 ORM id）
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
# population resolver（FIX 3）：current canonical ∩ listing_date<=T
# ---------------------------------------------------------------------------
async def resolve_backfill_population_at(
    session,
    trade_date: date,
) -> list[Instrument]:
    canonical_ids = await get_active_a_share_instruments(session)
    if not canonical_ids:
        return []
    stmt = (
        select(Instrument)
        .where(
            Instrument.id.in_(canonical_ids),
            Instrument.market.in_(("SH", "SZ")),
            stock_symbol_sql_filter(Instrument),
            Instrument.listing_date.is_not(None),
            Instrument.listing_date <= trade_date,
        )
        .order_by(Instrument.symbol)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return rows


# ---------------------------------------------------------------------------
# listing-date coverage preflight（FIX 3 preflight）
# ---------------------------------------------------------------------------
async def check_listing_date_coverage(session) -> dict:
    canonical_ids = await get_active_a_share_instruments(session)
    if not canonical_ids:
        return {
            "total_current_population": 0,
            "listing_date_present": 0,
            "listing_date_missing": 0,
        }
    rows = list(
        (await session.execute(
            select(Instrument).where(Instrument.id.in_(canonical_ids))
        )).scalars().all()
    )
    present = sum(1 for r in rows if r.listing_date is not None)
    missing = sum(1 for r in rows if r.listing_date is None)
    return {
        "total_current_population": len(rows),
        "listing_date_present": present,
        "listing_date_missing": missing,
    }


# ---------------------------------------------------------------------------
# partition 状态 / resume decision（FIX 7 三态）
# ---------------------------------------------------------------------------
PARTITION_STATUS_RUNNING = "RUNNING"
PARTITION_STATUS_COMPLETED = "COMPLETED"
PARTITION_STATUS_FAILED = "FAILED"

RESUME_SKIP = "SKIP_COMPLETED_MATCH"
RESUME_RERUN = "RERUN_INCOMPLETE"
RESUME_BLOCK = "BLOCK_COMPLETED_MISMATCH"


class CompletedPartitionMetadataMismatch(RuntimeError):
    """COMPLETED partition 存在 metadata mismatch，禁止覆盖。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _partition_dir(run_id: str, trade_date: date, output_root: Path | None = None) -> Path:
    root = output_root or OUTPUT_DIR
    return root / run_id / "bars" / trade_date.isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


def _expected_partition_metadata(trade_date: date, bar_index: int,
                                code_sha: str, eligible: int) -> dict:
    return {
        "trade_date": trade_date.isoformat(),
        "bar_index": bar_index,
        "code_sha": code_sha,
        "as_of": AS_OF.isoformat(),
        "eligible_instruments": eligible,
    }


def _partition_resume_decision(existing_manifest: dict | None,
                               expected_metadata: dict) -> str:
    """FIX 7 三态：NO_EXISTING→RERUN；COMPLETED+match→SKIP；
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
# 单列 row 投影（机械 projection，不 invent 字段）— FIX 5 / ADJUSTMENT LINEAGE
# ---------------------------------------------------------------------------
def _project_member_row(obs: dict, trade_date: date, bar_index: int,
                        listing_date: date | None = None,
                        code_sha: str | None = None) -> dict:
    lane_a = obs.get("lane_a") or {}
    lane_b = obs.get("lane_b") or {}
    inst = obs.get("instrument") or {}
    src = obs.get("source", {})
    eff_listing = listing_date or obs.get("listing_date")

    row = {
        # IDENTITY
        "trade_date": trade_date.isoformat(),
        "bar_index": bar_index,
        "instrument_id": str(obs.get("instrument_id") or inst.get("instrument_id") or ""),
        "symbol": obs.get("symbol") or inst.get("symbol") or "",
        "market": obs.get("market") or inst.get("market") or "",
        "board": obs.get("board") or inst.get("board") or "",
        "listing_date": str(eff_listing) if eff_listing else None,
        # SOURCE
        "full_day_status": obs.get("full_day_status"),
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
        # LANE B（FIX 5：机械映射 lane_b ACTUAL keys）
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
        "code_sha": code_sha,
        "as_of": AS_OF.isoformat(),
    }
    return row


# ---------------------------------------------------------------------------
# 单 bar partition 执行 + 机械 reconcile（FIX 9）
# ---------------------------------------------------------------------------
async def _run_bar_partition(
    run_id: str,
    trade_date: date,
    bar_index: int,
    population: list[SampleInstrument],
    run_single_obs,
    mdas: Any = None,
    adapter: Any = None,
    session: Any = None,
    output_root: Path | None = None,
    listing_date_by_symbol: dict[str, date] | None = None,
    code_sha: str | None = None,
) -> dict:
    pdir = _partition_dir(run_id, trade_date, output_root)
    pdir.mkdir(parents=True, exist_ok=True)
    listing_date_by_symbol = listing_date_by_symbol or {}

    sample_insts = population
    eligible = len(sample_insts)

    rows: list[dict] = []
    source_status_agg: dict[str, int] = {}
    canonical_status_agg: dict[str, int] = {}
    lane_a_computed = 0
    lane_b_computed = 0
    pit_gap_unavailable = 0
    pit_gap_adj_degraded = 0
    run_error = 0

    for inst in sample_insts:
        listing_date = listing_date_by_symbol.get(inst.symbol)
        try:
            obs = await run_single_obs(
                mdas, adapter, session, inst, trade_date
            )
        except Exception as exc:  # 单只失败不污染整 bar，但整 bar 判 FAILED
            rows.append({
                "trade_date": trade_date.isoformat(),
                "bar_index": bar_index,
                "symbol": inst.symbol,
                "market": inst.market,
                "instrument_id": str(inst.instrument_id),
                "board": inst.board,
                "listing_date": str(listing_date) if listing_date else None,
                "full_day_status": "RUN_ERROR",
                "extraction_status": None,
                "canonicalization_status": None,
                "canonicalization_reason": f"run_single_observation: {type(exc).__name__}: {exc}",
                "error": str(exc),
                "code_sha": code_sha,
                "as_of": AS_OF.isoformat(),
            })
            run_error += 1
            continue

        row = _project_member_row(obs, trade_date, bar_index, listing_date, code_sha)
        rows.append(row)

        fds = row["full_day_status"] or "UNKNOWN"
        source_status_agg[fds] = source_status_agg.get(fds, 0) + 1
        # canonicalization 只对 COMPLETE source 有意义；source-incomplete 必须 None
        if row["full_day_status"] == "COMPLETE":
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

    written = len(rows)

    # FIX 9 机械 reconcile：
    # - member_rows_written == sum(frozen source statuses) + RUN_ERROR（若 0）
    # - 任何 UNKNOWN source status → FAILED（不接受）
    # - COMPLETE count == sum(frozen canonical statuses)
    # - source-incomplete rows canonicalization_status 必须 None
    # - 任何 RUN_ERROR → FAILED
    non_unknown_source = sum(
        v for k, v in source_status_agg.items() if k in _SOURCE_FROZEN
    )
    unknown_source = sum(
        v for k, v in source_status_agg.items() if k not in _SOURCE_FROZEN
    )
    complete_count = source_status_agg.get("COMPLETE", 0)
    canonical_complete = sum(
        v for k, v in canonical_status_agg.items() if k in _CANONICAL_FROZEN
    )
    unknown_canonical = sum(
        v for k, v in canonical_status_agg.items() if k not in _CANONICAL_FROZEN
    )
    source_incomplete_rows = [
        r for r in rows
        if r["full_day_status"] in _SOURCE_FROZEN and r["full_day_status"] != "COMPLETE"
    ]
    incomplete_wrong_canonical = any(
        r["canonicalization_status"] is not None for r in source_incomplete_rows
    )

    reconciled = (
        (run_error == 0)
        and (written == non_unknown_source)
        and (unknown_source == 0)
        and (complete_count == canonical_complete)
        and (unknown_canonical == 0)
        and (not incomplete_wrong_canonical)
    )
    partition_status = (
        PARTITION_STATUS_COMPLETED if reconciled else PARTITION_STATUS_FAILED
    )

    member_path = pdir / "member_facts.jsonl"
    with open(member_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

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
    }
    _write_json(pdir / "data_quality.json", data_quality)

    manifest = {
        "trade_date": trade_date.isoformat(),
        "bar_index": bar_index,
        "code_sha": code_sha,
        "as_of": AS_OF.isoformat(),
        "status": partition_status,
        "eligible_instruments": eligible,
        "member_rows_written": written,
        "completed_at": _now_iso() if partition_status == PARTITION_STATUS_COMPLETED else None,
    }
    _write_json(pdir / "partition_manifest.json", manifest)

    return manifest


# ---------------------------------------------------------------------------
# root manifest 质量累计（FIX 8）
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


# ---------------------------------------------------------------------------
# 顶层 runner
# ---------------------------------------------------------------------------
async def run_backfill(
    run_id: str | None = None,
    bar_dates: list[date] | None = None,
    *,
    dry_run: bool = False,
    calendar_fn=None,       # (session, T, n) -> list[date]
    population_fn=None,     # (session, trade_date) -> list[Instrument]
    run_single_obs=None,    # (mdas, adapter, session, inst, trade_date) -> dict
    adapter=None,           # 注入 fake adapter；None → 打开真实 PytdxAdapter
    output_root: Path | None = None,
    code_sha: str | None = None,   # FIX 10 runtime code SHA（默认 git HEAD）
    require_listing_coverage: bool = False,  # live preflight：missing==0 否则 STOP
) -> dict:
    if run_id is None:
        run_id = f"{RUN_ID_PREFIX}_{AS_OF.isoformat()}"

    _out_root = output_root or OUTPUT_DIR
    _code_sha = code_sha or _git_head_sha() or "UNKNOWN"

    async def _default_calendar(session, T, n):
        return await previous_trading_dates(session, T, n)

    async def _default_population(session, trade_date):
        return await resolve_backfill_population_at(session, trade_date)

    _calendar = calendar_fn or _default_calendar
    _population = population_fn or _default_population
    _run_obs = run_single_obs or run_single_observation

    if bar_dates is None:
        async with AsyncSessionLocal() as session:
            bar_dates = await _calendar(session, AS_OF, BAR_COUNT)
        bar_dates = sorted(set(bar_dates))
        assert len(bar_dates) == BAR_COUNT, (
            f"official bar_count must be exactly {BAR_COUNT}, got {len(bar_dates)}"
        )
        assert bar_dates[-1] == AS_OF, (
            f"latest bar must be {AS_OF}, got {bar_dates[-1]}"
        )
    else:
        bar_dates = sorted(set(bar_dates))

    root_manifest = {
        "code_sha": _code_sha,
        "as_of": AS_OF.isoformat(),
        "bar_count": BAR_COUNT,
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
    }

    if dry_run:
        async with AsyncSessionLocal() as session:
            for idx, t in enumerate(bar_dates, start=1):
                insts = await _population(session, t)
                root_manifest["eligible_instrument_days"] += len(insts)
                root_manifest["completed_bar_count"] += 1
        root_manifest["status"] = "DRY_RUN_DONE"
        _write_json(_out_root / run_id / "manifest.json", root_manifest)
        return root_manifest

    completed = 0
    failed = 0
    mdas = MarketDataAggregationService()

    async def _bars_loop(session, adapter_for_run):
        nonlocal completed, failed
        for idx, t in enumerate(bar_dates, start=1):
            pdir = _partition_dir(run_id, t, _out_root)
            # FIX 3/7：先 resolve population 得 expected_eligible，再读 existing 决定
            instruments = await _population(session, t)
            population = [
                _to_sample_inst(inst) if not isinstance(inst, SampleInstrument)
                else inst
                for inst in instruments
            ]
            eligible = len(population)
            listing_date_by_symbol = {
                inst.symbol: getattr(inst, "listing_date", None)
                for inst in instruments
                if getattr(inst, "listing_date", None) is not None
            }

            existing = None
            mpath = pdir / "partition_manifest.json"
            if mpath.exists():
                try:
                    with open(mpath, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    existing = None

            expected_meta = _expected_partition_metadata(t, idx, _code_sha, eligible)
            decision = _partition_resume_decision(existing, expected_meta)

            if decision == RESUME_SKIP:
                # FIX 8：resume-skip 的 completed bar 也要累计已有 quality
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
                    run_id, t, idx, population,
                    _run_obs,
                    mdas, adapter_for_run, session, _out_root,
                    listing_date_by_symbol, _code_sha,
                )
            except Exception:
                failed += 1
                _write_json(
                    pdir / "partition_manifest.json",
                    {
                        "trade_date": t.isoformat(),
                        "bar_index": idx,
                        "code_sha": _code_sha,
                        "as_of": AS_OF.isoformat(),
                        "status": PARTITION_STATUS_FAILED,
                        "error": traceback.format_exc(),
                        "completed_at": None,
                    },
                )
                continue
            _accumulate_partition_quality(
                root_manifest, manifest, json.load(open(pdir / "data_quality.json"))
            )
            if manifest["status"] == PARTITION_STATUS_COMPLETED:
                completed += 1
            else:
                failed += 1

    if adapter is not None:
        # 注入路径：fake adapter（测试用），不连真实 pytdx / DB
        await _bars_loop(None, adapter)
    else:
        with PytdxAdapter() as real_adapter:
            async with AsyncSessionLocal() as session:
                # live listing-date coverage preflight（FIX 3 preflight，仅真实路径）
                if require_listing_coverage:
                    cov = await check_listing_date_coverage(session)
                    root_manifest["listing_date_coverage"] = cov
                    if cov["listing_date_missing"] != 0:
                        root_manifest["status"] = "STOP"
                        root_manifest["first_blocker"] = (
                            f"LISTING_DATE_COVERAGE_GAP missing={cov['listing_date_missing']}"
                        )
                        _write_json(_out_root / run_id / "manifest.json", root_manifest)
                        return root_manifest
                await _bars_loop(session, real_adapter)  # FIX 2：用 real_adapter

    root_manifest.update({
        "status": "DONE" if failed == 0 else "PARTIAL",
        "completed_bar_count": completed,
        "failed_bar_count": failed,
    })
    _write_json(_out_root / run_id / "manifest.json", root_manifest)
    return root_manifest


if __name__ == "__main__":
    import sys

    _dry = "--dry-run" in sys.argv
    result = asyncio.run(run_backfill(dry_run=_dry))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
