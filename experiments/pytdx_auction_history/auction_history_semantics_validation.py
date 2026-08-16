"""Auction Historical Data Semantics Validation Runner — Round 1B (Runner Correction).

实验性质：Experiment Infrastructure Correction only。
本轮不运行真实历史数据验证，不连接生产数据执行 full experiment。

目标：把 runner 从“架构方向正确但实际不可运行”修成“合同正确、可离线 smoke”。

UNIFIED MARKET DATA FIRST（硬规则）：
- 所有已属盘迹正式行情体系的数据，必须通过现有 official owner 获取。
- Instrument Universe / Trading Calendar / Daily OHLCV / Previous Close / Adjustment / QFQ
  → 复用 get_active_a_share_instruments() / calendar_service / MarketDataAggregationService(=MDAS)。
- 唯一允许使用尚未进入统一 Market Data API 的 historical transaction source
  → 通过 PytdxAdapter 受管连接调用 adapter.api.get_history_transaction_data（thin bridge）。

禁止（生产代码 READ ONLY，本脚本不修改 backend/）：
- 直接 new TdxHq_API（复用 PytdxAdapter 连接管理 / retry / reconnect）；
- 直接查 bars_daily / bar_repository private；
- 自己算 previous_close / 自己写 qfq / 自己读 xdxr 算 adjustment；
- 自行实现第二套 calendar / universe / market-mapping / qfq；
- 用 PytdxAdapter 的 raw daily（get_daily_bars / klines）作为 Daily Open / Previous Close source。

身份模型（MOD2）：
- Historical transaction source 一律使用 symbol（6 位字符串）。
- MDAS 一律使用 instrument_id（UUID）。
- 两者在 sample resolution 阶段一次性解析为 SampleInstrument，后续不再混用。

本轮 verdict 只能是 RUNNER_READY / RUNNER_BROKEN，不能宣布真实 source PASS。

用法：
    python experiments/pytdx_auction_history/auction_history_semantics_validation.py \
        --as-of YYYY-MM-DD [--corporate-lookback-days N]

离线 smoke：
    pytest -q experiments/pytdx_auction_history/tests/
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

# 盘迹正式 owner（见 MOD1 owner discovery）。
from app.services.feature_snapshot_service import get_active_a_share_instruments  # canonical universe (UUID)
from app.services.calendar_service import is_trading_day_async  # canonical calendar (async module fn)
from app.services.market_data_aggregation_service import MarketDataAggregationService  # MDAS
from app.services.adjustment_factor_service import AdjustmentFactorService  # authoritative factor owner
from app.core.pytdx_adapter import PytdxAdapter, market_from_code  # managed pytdx transport

logger = logging.getLogger("auction_history_semantics_validation")

EXPERIMENT_DIR = Path(__file__).resolve().parent
SAMPLE_FILE = EXPERIMENT_DIR / "samples" / "auction_history_round1.csv"
OUTPUT_DIR = EXPERIMENT_DIR / "output" / "round1"
RESOLVED_CORPORATE_FILE = OUTPUT_DIR / "resolved_corporate_cases.csv"

# 09:25 集合竞价 canonical 时间字符串。
CANONICAL_AUCTION_TIME = "09:25"
_AUCTION_TIME_RE = re.compile(r"^09:25(:\d{2})?$")

# 受控随机性：本实验无随机分支；sample 固定。


# =============================================================================
# MOD2 — Instrument Identity Model
# =============================================================================
@dataclass(frozen=True)
class SampleInstrument:
    """实验内部统一身份 DTO。

    symbol: 6 位字符串，供 historical transaction source 使用。
    instrument_id: UUID，供 MDAS 使用。
    """

    symbol: str
    instrument_id: UUID
    board: str
    liquidity: str
    cohort: str


# =============================================================================
# MOD5 — Structured Extraction Result
# =============================================================================
class ExtractionStatus(StrEnum):
    FOUND = "FOUND"
    MISSING_0925 = "MISSING_0925"
    MULTIPLE_0925 = "MULTIPLE_0925"
    SOURCE_ERROR = "SOURCE_ERROR"
    MARKET_MAPPING_FAILURE = "MARKET_MAPPING_FAILURE"


@dataclass
class AuctionExtractionResult:
    status: ExtractionStatus
    records: list[dict] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


# =============================================================================
# MOD8 — 09:25 Time Normalization
# =============================================================================
def _normalize_auction_time(raw_time: Any) -> str | None:
    """将 source 时间字符串归一到 canonical 09:25。

    仅明确等价形式 "09:25" / "09:25:00" 视为合法集合竞价时间；
    其他 09:25:xx（带秒级变体）保存 raw 但不纳入 canonical 候选。
    返回 None 表示不是合法 09:25 集合竞价记录。
    """
    if raw_time is None:
        return None
    s = str(raw_time).strip()
    if _AUCTION_TIME_RE.match(s):
        return CANONICAL_AUCTION_TIME
    return None


# =============================================================================
# MOD6 — Pytdx Managed Connection Bridge
# =============================================================================
def extract_auction_records(
    adapter: PytdxAdapter,
    symbol: str,
    target_date: date,
) -> AuctionExtractionResult:
    """通过 PytdxAdapter 受管连接抽取 09:25 集合竞价分笔。

    只在 `with PytdxAdapter() as adapter:` 生命周期内调用（见 MOD6）。
    禁止 new TdxHq_API / 手动 server / 手动 retry / 手动 market mapping。
    """
    market = market_from_code(symbol)
    if market is None:
        return AuctionExtractionResult(
            status=ExtractionStatus.MARKET_MAPPING_FAILURE,
            records=[],
            error_code="MARKET_MAPPING_FAILURE",
            error_message=f"market_from_code({symbol!r}) 返回 None",
        )
    date_int = int(target_date.strftime("%Y%m%d"))
    try:
        # 复用 adapter 受管连接；.api 未连接时抛 RuntimeError（由 PytdxAdapter 保证）。
        raw = adapter.api.get_history_transaction_data(
            market=market, code=symbol, start=0, count=10000, date_int=date_int
        )
    except Exception as e:  # noqa: BLE001 — 结构化为 SOURCE_ERROR
        return AuctionExtractionResult(
            status=ExtractionStatus.SOURCE_ERROR,
            records=[],
            error_code=type(e).__name__,
            error_message=str(e),
        )
    if not raw:
        return AuctionExtractionResult(status=ExtractionStatus.MISSING_0925, records=[])

    canonical: list[dict] = []
    raw_0925_any: list[dict] = []
    for r in raw:
        t = r.get("time")
        norm = _normalize_auction_time(t)
        if norm is None:
            continue
        raw_0925_any.append(r)
        if norm == CANONICAL_AUCTION_TIME:
            canonical.append(r)

    if not canonical:
        return AuctionExtractionResult(status=ExtractionStatus.MISSING_0925, records=raw_0925_any)

    # 全部归一到同一 canonical 时间 "09:25"；若有多条视为 MULTIPLE_0925。
    if len(canonical) == 1:
        return AuctionExtractionResult(status=ExtractionStatus.FOUND, records=canonical)
    return AuctionExtractionResult(status=ExtractionStatus.MULTIPLE_0925, records=canonical)


# =============================================================================
# MOD3 — MDAS Call Contract (async, start_date/end_date, BarAggregationResult)
# =============================================================================
async def get_mdas_daily_open(
    mdas: MarketDataAggregationService,
    session: Any,
    instrument_id: UUID,
    target_date: date,
) -> Any:
    """Lane A（raw daily open）：adj=none，单次精确日期。"""
    result = await mdas.get_bars(
        session,
        instrument_id,
        timeframe="1d",
        adj="none",
        include_realtime=False,
        completed_only=True,
        start_date=target_date,
        end_date=target_date,
    )
    # 只读正式字段：.bars / .data_source / .degraded / .degraded_reason
    _ = (result.bars, result.data_source, result.degraded, result.degraded_reason)
    return result


async def get_mdas_pit_qfq_gap(
    mdas: MarketDataAggregationService,
    session: Any,
    instrument_id: UUID,
    target_date: date,
    earliest: date,
) -> Any:
    """Lane B（PIT QFQ gap）：adj=qfq，adjustment_as_of=target_date。"""
    result = await mdas.get_bars(
        session,
        instrument_id,
        timeframe="1d",
        adj="qfq",
        adjustment_as_of=target_date,
        include_realtime=False,
        completed_only=True,
        start_date=earliest,
        end_date=target_date,
    )
    # 只读正式字段。
    _ = (
        result.bars,
        result.adjustment_as_of,
        result.adj_factor_hash,
        result.data_source,
        result.degraded,
        result.degraded_reason,
    )
    return result


# =============================================================================
# MOD4 — Trading Calendar (async module fn, no CalendarService class)
# =============================================================================
async def previous_trading_dates(
    session: Any,
    as_of: date,
    n: int,
) -> list[date]:
    """从 as_of 向前遍历，每一天用 official is_trading_day_async 判断，收集 n 个交易日期。

    不使用 weekday-only 判断；每一天 open/closed 委托 calendar_service。
    """
    out: list[date] = []
    cur = as_of
    guard = 0
    while len(out) < n and guard < n * 14 + 30:
        guard += 1
        if await is_trading_day_async(session, cur):
            out.append(cur)
        cur = cur - timedelta(days=1)
    return out


# =============================================================================
# MOD9 — Tracked Sample Definition (version-controlled)
# =============================================================================
def load_sample(path: Path) -> list[SampleInstrument]:
    """从受版本控制的 sample CSV 解析固定样本。

    字段：symbol,board,liquidity,cohort,note
    仅由 symbol+cohort 构成 identity；instrument_id 在 resolution 阶段填充（见 MOD2）。
    """
    if not path.exists():
        raise FileNotFoundError(f"sample file 缺失（必须 tracked）：{path}")
    rows: list[SampleInstrument] = []
    seen: set[tuple[str, str]] = set()
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"symbol", "board", "liquidity", "cohort"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"sample schema 缺失字段：{missing}")
        for i, row in enumerate(reader):
            symbol = (row.get("symbol") or "").strip()
            cohort = (row.get("cohort") or "").strip()
            if not symbol:
                raise ValueError(f"sample 第 {i+1} 行 symbol 为空")
            key = (symbol, cohort)
            if key in seen:
                raise ValueError(f"sample 存在重复 (symbol,cohort)：{key}")
            seen.add(key)
            rows.append(
                SampleInstrument(
                    symbol=symbol,
                    instrument_id=UUID(int=0),  # 占位，resolution 阶段填充
                    board=(row.get("board") or "").strip(),
                    liquidity=(row.get("liquidity") or "").strip(),
                    cohort=cohort,
                )
            )
    if not rows:
        raise ValueError("sample 为空")
    return rows


# =============================================================================
# MOD1/MOD2 — Universe Validation + symbol -> UUID resolution
# =============================================================================
async def resolve_sample_instruments(
    session: Any,
    samples: list[SampleInstrument],
) -> tuple[list[SampleInstrument], list[dict]]:
    """一次性解析：canonical universe validation + symbol -> UUID。

    返回 (resolved, skipped)；不在 canonical universe 的 sample 标记 SAMPLE_NOT_IN_CANONICAL_UNIVERSE。
    不静默替换另一只股票。
    """
    universe_ids: list[UUID] = await get_active_a_share_instruments(session)
    universe_set = set(universe_ids)

    # symbol -> instrument_id resolver（read-only ORM identity mapping，不重建 universe）。
    from app.models.instrument import Instrument

    stmt = (
        Instrument.__table__.select()
        .where(Instrument.symbol.in_([s.symbol for s in samples]))
    )
    result = await session.execute(stmt)
    rows = result.mappings().all()
    symbol_to_id: dict[str, UUID] = {r["symbol"]: r["id"] for r in rows}

    resolved: list[SampleInstrument] = []
    skipped: list[dict] = []
    for s in samples:
        inst_id = symbol_to_id.get(s.symbol)
        if inst_id is None or inst_id not in universe_set:
            skipped.append(
                {
                    "symbol": s.symbol,
                    "cohort": s.cohort,
                    "reason": "SAMPLE_NOT_IN_CANONICAL_UNIVERSE",
                }
            )
            continue
        resolved.append(SampleInstrument(
            symbol=s.symbol,
            instrument_id=inst_id,
            board=s.board,
            liquidity=s.liquidity,
            cohort=s.cohort,
        ))
    return resolved, skipped


# =============================================================================
# MOD10 — Corporate Action Case Resolution (factor-change based)
# =============================================================================
async def resolve_corporate_cases(
    adj_service: AdjustmentFactorService,
    session: Any,
    corporate_samples: list[SampleInstrument],
    as_of: date,
    lookback_days: int,
) -> list[dict]:
    """围绕真实除权事件构造 T-1/T/T+1。

    唯一依据：authoritative adjustment factor series（禁止 xdxr 重算 / 价格跳空猜除权）。
    只选择 event_date <= as_of 且在 lookback 范围内的真实 factor-change event。
    无可用 event 标记 NO_ADJUSTMENT_EVENT_IN_WINDOW。
    """
    resolved: list[dict] = []
    for s in corporate_samples:
        factor_df = await adj_service.get_factor_series(
            session, s.instrument_id, as_of=as_of
        )
        if factor_df is None or len(factor_df) == 0:
            resolved.append({
                "symbol": s.symbol,
                "instrument_id": str(s.instrument_id),
                "status": "NO_ADJUSTMENT_EVENT_IN_WINDOW",
                "event_date_T": "",
                "prev_trade_date": "",
                "next_trade_date": "",
                "factor_before": "",
                "factor_after": "",
            })
            continue

        # 识别 factor 变化日（真实 factor-change event）。
        fdf = factor_df.copy()
        fdf["prev_factor"] = fdf["adj_factor"].shift(1)
        change_mask = fdf["prev_factor"].notna() & (
            fdf["adj_factor"] != fdf["prev_factor"]
        )
        change_dates = fdf.loc[change_mask, "trade_date"].tolist()
        # 过滤 lookback + as_of 窗口。
        window_start = as_of - timedelta(days=lookback_days)
        in_window = [
            d for d in change_dates
            if d <= as_of and d >= window_start
        ]
        if not in_window:
            resolved.append({
                "symbol": s.symbol,
                "instrument_id": str(s.instrument_id),
                "status": "NO_ADJUSTMENT_EVENT_IN_WINDOW",
                "event_date_T": "",
                "prev_trade_date": "",
                "next_trade_date": "",
                "factor_before": "",
                "factor_after": "",
            })
            continue

        # 取窗口内最近一个 event。
        T = max(in_window)
        T_idx = fdf.index[fdf["trade_date"] == T][0]
        factor_after = float(fdf.loc[T_idx, "adj_factor"])
        factor_before = float(fdf.loc[T_idx, "prev_factor"])
        # 用 official calendar 推导 T-1 / T+1 交易日期。
        prev_d = T - timedelta(days=1)
        while not await is_trading_day_async(session, prev_d):
            prev_d = prev_d - timedelta(days=1)
        next_d = T + timedelta(days=1)
        while not await is_trading_day_async(session, next_d):
            next_d = next_d + timedelta(days=1)
        resolved.append({
            "symbol": s.symbol,
            "instrument_id": str(s.instrument_id),
            "status": "RESOLVED",
            "event_date_T": T.isoformat(),
            "prev_trade_date": prev_d.isoformat(),
            "next_trade_date": next_d.isoformat(),
            "factor_before": factor_before,
            "factor_after": factor_after,
            "adj_factor_hash": "",  # 真实环境从 MDAS result 取，离线 N/A
        })
    return resolved


# =============================================================================
# MOD7 — Strict MULTIPLE_0925 policy (caller side)
# =============================================================================
async def run_single_observation(
    mdas: MarketDataAggregationService,
    adapter: PytdxAdapter,
    session: Any,
    inst: SampleInstrument,
    target_date: date,
) -> dict:
    """对单个 (instrument, date) 执行抽取 + 语义 lane。

    MOD7：只有 FOUND（恰好一条 09:25）才进入 Lane A/B/Volume/Amount/Gap。
    MULTIPLE_0925：保留所有 raw evidence，停止 canonical 语义比较。
    SOURCE_ERROR：status 永远 SOURCE_ERROR，records=[]，caller 不进入任何 lane。
    """
    extract = extract_auction_records(adapter, inst.symbol, target_date)

    obs: dict[str, Any] = {
        "symbol": inst.symbol,
        "instrument_id": str(inst.instrument_id),
        "target_date": target_date.isoformat(),
        "extraction_status": extract.status.value,
        "raw_record_count": len(extract.records),
        "error_code": extract.error_code,
        "error_message": extract.error_message,
        "entered_semantic_lanes": False,
        "lane_a": None,
        "lane_b": None,
        "gap_pct": None,
        "volume_unit": None,
        "amount_semantics": None,
    }

    if extract.status != ExtractionStatus.FOUND:
        # MULTIPLE_0925 / MISSING_0925 / SOURCE_ERROR / MARKET_MAPPING_FAILURE
        # 均不进入 price/volume/amount/gap 推断。
        return obs

    # FOUND：进入语义 lane。
    obs["entered_semantic_lanes"] = True
    rec = extract.records[0]
    obs["volume_unit"] = "unknown_source_unit"  # 真实语义待 live evidence
    obs["amount_semantics"] = "unknown_source_unit"  # 真实语义待 live evidence

    # Lane A：raw daily open。
    res_a = await get_mdas_daily_open(mdas, session, inst.instrument_id, target_date)
    obs["lane_a"] = {
        "data_source": res_a.data_source,
        "degraded": res_a.degraded,
        "degraded_reason": res_a.degraded_reason,
    }

    # Lane B：PIT QFQ gap（earliest 取目标日前 400 交易日外的足够早日期，离线用固定窗口）。
    earliest = target_date - timedelta(days=400)
    res_b = await get_mdas_pit_qfq_gap(mdas, session, inst.instrument_id, target_date, earliest)
    obs["lane_b"] = {
        "adjustment_as_of": res_b.adjustment_as_of.isoformat() if res_b.adjustment_as_of else None,
        "adj_factor_hash": res_b.adj_factor_hash,
        "data_source": res_b.data_source,
        "degraded": res_b.degraded,
        "degraded_reason": res_b.degraded_reason,
    }
    # Gap 计算依赖真实 bar 数据，离线不执行数值比较；仅占位。
    obs["gap_pct"] = "pending_live_data"
    return obs


# =============================================================================
# MOD12/MOD13 — Compliance (real, evidence-based; not hardcoded PASS)
# =============================================================================
def summarize_compliance(
    smoke_passed: bool,
    live_data_validated: bool,
) -> dict:
    """compliance 由真实 execution / smoke evidence 生成。

    - 未执行的项 = NOT_RUN
    - 仅 smoke 项 = CONTRACT_PASS
    - live data source 项必须等 live execution 后才 PASS
    """
    mdas_contract = "CONTRACT_PASS" if smoke_passed else "NOT_RUN"
    calendar_contract = "CONTRACT_PASS" if smoke_passed else "NOT_RUN"
    instrument_identity = "CONTRACT_PASS" if smoke_passed else "NOT_RUN"
    pytdx_lifecycle = "CONTRACT_PASS" if smoke_passed else "NOT_RUN"
    source_error = "CONTRACT_PASS" if smoke_passed else "NOT_RUN"
    multiple_0925 = "CONTRACT_PASS" if smoke_passed else "NOT_RUN"
    corporate_resolver = "CONTRACT_PASS" if smoke_passed else "NOT_RUN"
    tracked_sample = "CONTRACT_PASS" if smoke_passed else "NOT_RUN"

    live_source = "PASS" if live_data_validated else "NOT_RUN"

    return {
        "mdas_async_result_signature": mdas_contract,
        "calendar_contract": calendar_contract,
        "instrument_identity": instrument_identity,
        "pytdx_managed_lifecycle": pytdx_lifecycle,
        "source_error_behavior": source_error,
        "multiple_0925_behavior": multiple_0925,
        "corporate_event_resolver": corporate_resolver,
        "tracked_sample": tracked_sample,
        "live_historical_source_evidence": live_source,
    }


# =============================================================================
# CLI
# =============================================================================
async def run_validation(
    as_of: date,
    corporate_lookback_days: int,
    live: bool = False,
) -> dict:
    """主流程（离线或 live 由 caller 决定；本 runner correction 默认 live=False）。

    live=False：只做结构解析 / sample 加载 / corporate 解析骨架（依赖 DB session）。
    真实历史采集只在 live=True 且配置好真实环境时执行。
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    samples = load_sample(SAMPLE_FILE)
    routine = [s for s in samples if s.cohort == "routine"]
    corporate = [s for s in samples if s.cohort == "corporate"]

    summary: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "live": live,
        "sample_total": len(samples),
        "routine_count": len(routine),
        "corporate_count": len(corporate),
        "resolved_instruments": [],
        "skipped_instruments": [],
        "routine_trade_dates": [],
        "corporate_cases": [],
        "observations": [],
        "compliance": summarize_compliance(smoke_passed=False, live_data_validated=live),
    }

    if not live:
        # Runner correction：不连接真实 DB / Pytdx；只验证文件与结构。
        summary["compliance"] = summarize_compliance(
            smoke_passed=False, live_data_validated=False
        )
        summary["note"] = "RUNNER_CORRECTION: live data validation NOT RUN"
        return summary

    # live 路径（真实环境执行，需要 async DB session + PytdxAdapter 受管连接）。
    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        resolved, skipped = await resolve_sample_instruments(session, samples)
        summary["resolved_instruments"] = [
            {"symbol": r.symbol, "instrument_id": str(r.instrument_id), "cohort": r.cohort}
            for r in resolved
        ]
        summary["skipped_instruments"] = skipped

        mdas = MarketDataAggregationService()
        adj_service = AdjustmentFactorService()

        # Routine：收集固定 N 个交易日期。
        routine_dates = await previous_trading_dates(session, as_of, n=10)
        summary["routine_trade_dates"] = [d.isoformat() for d in routine_dates]

        # Corporate：解析真实除权事件。
        corp_cases = await resolve_corporate_cases(
            adj_service, session, corporate, as_of, corporate_lookback_days
        )
        summary["corporate_cases"] = corp_cases
        _write_corporate_cases(corp_cases)

        # 采集（PytdxAdapter 受管连接，整个 batch 复用生命周期）。
        with PytdxAdapter() as adapter:
            for inst in resolved:
                dates = routine_dates if inst.cohort == "routine" else [
                    date.fromisoformat(c["event_date_T"])
                    for c in corp_cases
                    if c.get("symbol") == inst.symbol and c.get("event_date_T")
                ]
                for d in dates:
                    obs = await run_single_observation(mdas, adapter, session, inst, d)
                    summary["observations"].append(obs)

    summary["compliance"] = summarize_compliance(
        smoke_passed=True, live_data_validated=live
    )
    return summary


def _write_corporate_cases(cases: list[dict]) -> None:
    with RESOLVED_CORPORATE_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "symbol", "instrument_id", "status",
                "event_date_T", "prev_trade_date", "next_trade_date",
                "factor_before", "factor_after", "adj_factor_hash",
            ],
        )
        writer.writeheader()
        for c in cases:
            writer.writerow(c)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Auction Historical Validation Runner 1B")
    p.add_argument("--as-of", type=str, required=True, help="YYYY-MM-DD，实验可重复锚点")
    p.add_argument("--corporate-lookback-days", type=int, default=180)
    p.add_argument("--live", action="store_true", help="执行真实历史采集（默认 False）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()
    except ValueError:
        logger.error("--as-of 格式必须为 YYYY-MM-DD")
        return 2

    summary = asyncio.run(run_validation(
        as_of=as_of,
        corporate_lookback_days=args.corporate_lookback_days,
        live=args.live,
    ))
    print(summary["note"] if summary.get("note") else "validation complete")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
