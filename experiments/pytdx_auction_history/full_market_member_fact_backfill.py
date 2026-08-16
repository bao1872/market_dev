"""Auction Full-market 120-Bar Member Fact Backfill Runner — Round 3B-B.

职责：全市场 Auction Member Fact 历史回补的 orchestration。

核心口径（锁死）：
- 120 BAR = 截至 as_of 的最近 120 个官方 A 股交易日 / daily bars
  （通过 project official trading calendar: previous_trading_dates）。
- 禁止 date - timedelta(days=120) / 固定自然日区间 / 6 个月近似。
- 对交易日 T：只有 listing_date <= T 才允许生成 Member Fact。
- 窗口中途 IPO 股票不会向上市前扩展凑满 120 条。
- delisting lifecycle OUT OF SCOPE；Instrument.status 不得作为历史 eligibility 条件。

数据来源（全部复用既有 owner，禁止第二套算法）：
- previous_trading_dates()         official calendar
- resolve_listed_a_share_instruments_at()   canonical SH/SZ identity + listing boundary
- Instrument.listing_date          IPO rule
- MarketDataAggregationService     MDAS（Lane A 开盘 / qfq previous close）
- AdjustmentFactorService          PIT qfq
- PytdxAdapter                     historical 09:25 transaction tape
- run_single_observation()         canonicalization / Lane A / Lane B / corporate
- auction canonicalization / PIT qfq / volume validity

输出：FILE EVIDENCE ONLY（不新建 production member-fact DB table / migration / API / frontend）。
qfq degraded fail-close：pit_gap=None + lane_b.status=PIT_ADJUSTMENT_DEGRADED（已在
auction_history_semantics_validation.compute_lane_b 收口）。

本轮 NO FULL LIVE RUN：只实现 runner + 测试 + commit/push。
"""

from __future__ import annotations

import asyncio
import json
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 权威 owner（禁止第二套 market logic / calendar / qfq 重算）
# ---------------------------------------------------------------------------
from app.db import AsyncSessionLocal
from app.models.instrument import Instrument
from app.services.instrument_lifecycle_service import (
    resolve_listed_a_share_instruments_at,
)
from app.core.pytdx_adapter import PytdxAdapter
from app.services.market_data_aggregation_service import MarketDataAggregationService

from auction_history_semantics_validation import (
    run_single_observation,
    SampleInstrument,
    previous_trading_dates,
)

# 本轮 baseline：Round 3B-A1-R2A 收口 SHA（cf2b5ca）。
BASELINE_SHA = "cf2b5ca12d23e931da5c40a3f99cf4c638d821b4"

EXPERIMENT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = (
    EXPERIMENT_DIR / "output" / "member_fact_120bar" / "2026-08-14"
)

AS_OF = date(2026, 8, 14)
BAR_COUNT = 120
RUN_ID_PREFIX = "member_fact_120bar"


# ---------------------------------------------------------------------------
# board 派生（Instrument 不含 board 字段，由 market + symbol 推导）
# ---------------------------------------------------------------------------
def _board_for_symbol(symbol: str, market: str) -> str:
    s = symbol.strip()
    if market == "SH":
        if s.startswith("688"):
            return "STAR"  # 科创板
        if s.startswith("60"):
            return "SH_MAIN"  # 沪市主板
        return "SH_OTHER"
    if market == "SZ":
        if s.startswith("30"):
            return "SZ_GEM"  # 创业板
        if s.startswith("00") or s.startswith("02"):
            return "SZ_MAIN"  # 深市主板
        return "SZ_OTHER"
    return "OTHER"


def _to_sample_inst(inst: Instrument) -> SampleInstrument:
    return SampleInstrument(
        symbol=inst.symbol,
        market=inst.market,
        instrument_id=inst.instrument_id,
        board=_board_for_symbol(inst.symbol, inst.market),
        coverage_tag="all_a_share",
        cohort="routine",
    )


# ---------------------------------------------------------------------------
# partition 状态
# ---------------------------------------------------------------------------
PARTITION_STATUS_RUNNING = "RUNNING"
PARTITION_STATUS_COMPLETED = "COMPLETED"
PARTITION_STATUS_FAILED = "FAILED"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _partition_dir(run_id: str, trade_date: date, output_root: Path | None = None) -> Path:
    root = output_root or OUTPUT_DIR
    return root / run_id / "bars" / trade_date.isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# 单列 row 投影（机械 projection，不 invent 字段）
# ---------------------------------------------------------------------------
def _project_member_row(obs: dict, trade_date: date, bar_index: int,
                        listing_date: date | None = None) -> dict:
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
        # LANE B
        "lane_b_status": lane_b.get("status"),
        "previous_close_raw": lane_b.get("previous_close_raw"),
        "previous_close_pit_qfq": lane_b.get("previous_close_pit_qfq"),
        "naive_raw_gap": lane_b.get("naive_raw_gap"),
        "pit_gap": lane_b.get("pit_gap"),
        # LINEAGE / QUALITY
        "mdas_data_source": lane_b.get("mdas_data_source"),
        "degraded": lane_b.get("mdas_degraded"),
        "degraded_reason": lane_b.get("mdas_degraded_reason"),
        "adjustment_as_of": lane_b.get("adj_factor_hash"),
        "baseline_sha": BASELINE_SHA,
        "as_of": AS_OF.isoformat(),
    }
    return row


# ---------------------------------------------------------------------------
# 单 bar partition 执行 + reconcile
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
) -> dict:
    pdir = _partition_dir(run_id, trade_date, output_root)
    pdir.mkdir(parents=True, exist_ok=True)
    listing_date_by_symbol = listing_date_by_symbol or {}

    # 1) 当前 bar 的 canonical population（已在外部按 listing_date <= T 过滤）
    sample_insts = population
    eligible = len(sample_insts)

    rows: list[dict] = []
    source_status_agg: dict[str, int] = {}
    canonical_status_agg: dict[str, int] = {}
    lane_a_computed = 0
    lane_b_computed = 0
    pit_gap_unavailable = 0
    pit_gap_adj_degraded = 0

    for inst in sample_insts:
        listing_date = listing_date_by_symbol.get(inst.symbol)
        try:
            obs = await run_single_obs(
                mdas, adapter, session, inst, trade_date
            )
        except Exception as exc:  # 单只失败不污染整 bar
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
                "baseline_sha": BASELINE_SHA,
                "as_of": AS_OF.isoformat(),
            })
            source_status_agg["RUN_ERROR"] = source_status_agg.get("RUN_ERROR", 0) + 1
            continue

        row = _project_member_row(obs, trade_date, bar_index, listing_date)
        rows.append(row)

        fds = row["full_day_status"] or "UNKNOWN"
        source_status_agg[fds] = source_status_agg.get(fds, 0) + 1
        cstat = row["canonicalization_status"] or "UNKNOWN"
        canonical_status_agg[cstat] = canonical_status_agg.get(cstat, 0) + 1
        if row["lane_a_status"] == "COMPUTED":
            lane_a_computed += 1
        if row["lane_b_status"] == "COMPUTED":
            lane_b_computed += 1
        if row["lane_b_status"] == "PIT_ADJUSTMENT_DEGRADED":
            pit_gap_adj_degraded += 1
        # pit_gap unavailable：lane_b 不为 COMPUTED 且无有效 pit_gap
        if row["lane_b_status"] != "COMPUTED" or row["pit_gap"] is None:
            pit_gap_unavailable += 1

    written = len(rows)

    # reconcile（fail-closed）
    # - 每个 eligible instrument 必须恰好产生一行 → 否则 rows missing / FAILED (B10)
    # - 任何 RUN_ERROR（observation 崩溃）整 bar 失败
    run_error = source_status_agg.get("RUN_ERROR", 0)
    reconciled = (eligible == written) and (run_error == 0)
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
        "baseline_sha": BASELINE_SHA,
        "as_of": AS_OF.isoformat(),
        "status": partition_status,
        "eligible_instruments": eligible,
        "member_rows_written": written,
        "completed_at": _now_iso() if partition_status == PARTITION_STATUS_COMPLETED else None,
    }
    _write_json(pdir / "partition_manifest.json", manifest)

    return manifest


def _partition_already_completed(pdir: Path, trade_date: date, bar_index: int, output_root: Path | None = None) -> bool:
    mpath = pdir / "partition_manifest.json"
    if not mpath.exists():
        return False
    try:
        with open(mpath, "r", encoding="utf-8") as f:
            m = json.load(f)
    except Exception:
        return False
    return (
        m.get("status") == PARTITION_STATUS_COMPLETED
        and m.get("trade_date") == trade_date.isoformat()
        and m.get("bar_index") == bar_index
        and m.get("baseline_sha") == BASELINE_SHA
        and m.get("as_of") == AS_OF.isoformat()
        and m.get("eligible_instruments") is not None
    )


# ---------------------------------------------------------------------------
# 顶层 runner（记忆友好：每 bar resolve→run→write→reconcile→release）
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
) -> dict:
    if run_id is None:
        run_id = f"{RUN_ID_PREFIX}_{AS_OF.isoformat()}"

    _out_root = output_root or OUTPUT_DIR

    async def _default_calendar(session, T, n):
        return await previous_trading_dates(session, T, n)

    async def _default_population(session, trade_date):
        return await resolve_listed_a_share_instruments_at(session, trade_date)

    _calendar = calendar_fn or _default_calendar
    _population = population_fn or _default_population
    _run_obs = run_single_obs or run_single_observation

    if bar_dates is None:
        async with AsyncSessionLocal() as session:
            bar_dates = await _calendar(session, AS_OF, BAR_COUNT)
        # 真实 calendar 路径：强制 exactly 120 bars 且 latest=AS_OF
        bar_dates = sorted(set(bar_dates))
        assert len(bar_dates) == BAR_COUNT, (
            f"official bar_count must be exactly {BAR_COUNT}, got {len(bar_dates)}"
        )
        assert bar_dates[-1] == AS_OF, (
            f"latest bar must be {AS_OF}, got {bar_dates[-1]}"
        )
    else:
        # 注入测试路径：尊重调用者提供的 bar_dates（不强制 120）
        bar_dates = sorted(set(bar_dates))

    root_manifest = {
        "baseline_sha": BASELINE_SHA,
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
        # 不连 pytdx，只校验 calendar + 每 bar population 计数（用只读 DB）
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

    async def _bars_loop(session):
        nonlocal completed, failed
        for idx, t in enumerate(bar_dates, start=1):
            pdir = _partition_dir(run_id, t, _out_root)
            if _partition_already_completed(pdir, t, idx, _out_root):
                # resume skip（metadata 完全匹配）
                completed += 1
                continue
            # 当前 bar 的 canonical population（listing_date <= T）
            instruments = await _population(session, t)
            population = [
                _to_sample_inst(inst) if not isinstance(inst, SampleInstrument)
                else inst
                for inst in instruments
            ]
            # listing_date 仅当 population owner 提供（Instrument model）时取
            listing_date_by_symbol = {
                inst.symbol: getattr(inst, "listing_date", None)
                for inst in instruments
                if getattr(inst, "listing_date", None) is not None
            }
            try:
                manifest = await _run_bar_partition(
                    run_id, t, idx, population,
                    _run_obs,
                    mdas, adapter, session, _out_root,
                    listing_date_by_symbol,
                )
            except Exception:
                failed += 1
                _write_json(
                    pdir / "partition_manifest.json",
                    {
                        "trade_date": t.isoformat(),
                        "bar_index": idx,
                        "baseline_sha": BASELINE_SHA,
                        "as_of": AS_OF.isoformat(),
                        "status": PARTITION_STATUS_FAILED,
                        "error": traceback.format_exc(),
                        "completed_at": None,
                    },
                )
                continue
            root_manifest["eligible_instrument_days"] += manifest.get(
                "eligible_instruments", 0
            )
            root_manifest["member_rows"] += manifest.get(
                "member_rows_written", 0
            )
            if manifest["status"] == PARTITION_STATUS_COMPLETED:
                completed += 1
            else:
                failed += 1

    if adapter is not None:
        # 注入路径：fake adapter（测试用），不连真实 pytdx / DB
        await _bars_loop(None)
    else:
        with PytdxAdapter() as real_adapter:
            async with AsyncSessionLocal() as session:
                await _bars_loop(session)

    root_manifest.update({
        "status": "DONE" if failed == 0 else "PARTIAL",
        "completed_bar_count": completed,
        "failed_bar_count": failed,
    })
    _write_json(_out_root / run_id / "manifest.json", root_manifest)
    return root_manifest


if __name__ == "__main__":
    # 仅供人工/调试调用；正式 live 120-bar 不在此轮启动。
    import sys

    _dry = "--dry-run" in sys.argv
    result = asyncio.run(run_backfill(dry_run=_dry))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
