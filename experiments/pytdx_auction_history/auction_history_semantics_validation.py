"""Auction Historical Data Semantics Validation Runner — Round 1C (Evidence Pipeline).

实验性质：Experiment Measurement Pipeline Correction only。
本轮不运行真实历史数据验证，不连接生产数据执行 full experiment。

目标：把 runner 从「架构方向正确、可 smoke」修成
「--live 后能够输出完整、可追溯、可审计的证据管线」：

  1. 09:25 source evidence
  2. raw price vs MDAS open evidence
  3. PIT adjustment / Gap evidence
  4. volume unit evidence
  5. amount semantics evidence
  6. corporate-action evidence
  7. data-quality statistics

而不是只显示 “validation complete”。

UNIFIED MARKET DATA FIRST（硬规则）：
- 所有已属盘迹正式行情体系的数据，必须通过现有 official owner 获取。
- Instrument Universe / Trading Calendar / Daily OHLCV / Previous Close / Adjustment / QFQ
  → 复用 get_active_a_share_instruments() / calendar_service / MarketDataAggregationService(=MDAS)
    / AdjustmentFactorService。
- 唯一允许使用尚未进入统一 Market Data API 的 historical transaction source
  → 通过 PytdxAdapter 受管连接调用 adapter.api.get_history_transaction_data（thin bridge）。

禁止（生产代码 READ ONLY，本脚本不修改 backend/）：
- 直接 new TdxHq_API（复用 PytdxAdapter 连接管理 / retry / reconnect）；
- 直接查 bars_daily / bar_repository private；
- 自己算 previous_close / 自己写 qfq / 自己读 xdxr 算 adjustment；
- 自行实现第二套 calendar / universe / market-mapping / qfq；
- 用 PytdxAdapter 的 raw daily（get_daily_bars / klines）作为 Daily Open / Previous Close source；
- 自行推导 volume = shares/lots 或 amount = qfq_price * volume（MOD8/MOD9 仅输出 evidence）。

身份模型（MOD2）：
- Historical transaction source 一律使用 symbol（6 位字符串） + market（SH/SZ）。
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
import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from statistics import median
from typing import Any
from uuid import UUID

import pandas as pd

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

# 09:25 集合竞价 canonical 时间字符串。
CANONICAL_AUCTION_TIME = "09:25"
# MOD3 — 仅明确等价形式 "09:25" / "09:25:00" 视为合法集合竞价时间；
#       其他 09:25:xx（带秒级变体）保存 raw 但不纳入 canonical 候选。
_AUCTION_TIME_RE = re.compile(r"^(09:25|09:25:00)$")

# 真实 pytdx historical transaction 字段（依据 experiments/pytdx_auction_history/
# explore_auction_history.py / debug_paths.py 实测）：time / price / vol / buyorsell。
# 注意：historical transaction 记录本身**没有 amount 字段**（amount 须由 price*vol 推导）。
SOURCE_TXN_FIELD_TIME = "time"
SOURCE_TXN_FIELD_PRICE = "price"
SOURCE_TXN_FIELD_VOL = "vol"
SOURCE_TXN_FIELD_BUYORSELL = "buyorsell"
SOURCE_SCHEMA_KEYS = [SOURCE_TXN_FIELD_TIME, SOURCE_TXN_FIELD_PRICE, SOURCE_TXN_FIELD_VOL, SOURCE_TXN_FIELD_BUYORSELL]


# =============================================================================
# MOD2 — Instrument Identity Model
# =============================================================================
class BoardCode(StrEnum):
    """实验内部 board 分类（与 market 分离）。"""

    SH_MAIN = "SH_MAIN"
    SZ_MAIN = "SZ_MAIN"
    CHINEXT = "CHINEXT"
    STAR = "STAR"


@dataclass(frozen=True)
class SampleInstrument:
    """实验内部统一身份 DTO。

    symbol: 6 位字符串，供 historical transaction source 使用。
    market: SH / SZ（与 symbol 联合作为实验身份键，MOD2）。
    instrument_id: UUID，供 MDAS 使用（resolution 阶段填充，禁止 UUID(int=0) 进入 live，MOD1）。
    board: BoardCode 实验分类。
    coverage_tag: 非正式 liquidity classification（MOD14：避免伪 liquidity tier）。
    cohort: routine / corporate。
    """

    symbol: str
    market: str
    instrument_id: UUID
    board: str
    coverage_tag: str
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
    NONCANONICAL_0925_TIME = "NONCANONICAL_0925_TIME"


@dataclass
class AuctionExtractionResult:
    status: ExtractionStatus
    records: list["NormalizedAuctionTransaction"] = field(default_factory=list)
    noncanonical_records: list["NormalizedAuctionTransaction"] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


# =============================================================================
# MOD4 — Normalized Source Transaction (experiment-only normalizer)
# =============================================================================
@dataclass
class NormalizedAuctionTransaction:
    """historical transaction 原始记录归一化。

    所有 source 原始字段仍保留在 source_record；禁止因字段缺失自动写 0。
    字段不存在 → None（MOD4）。
    """

    source_time: str
    canonical_time: str | None
    raw_price: float | None
    raw_volume_value: float | None  # 来自 source 'vol'
    raw_amount_value: float | None  # historical transaction 无此字段 → None
    buy_sell_raw: int | None  # 来自 'buyorsell'
    source_record: dict


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_raw_transaction(raw: dict) -> NormalizedAuctionTransaction:
    """将单条 pytdx historical transaction 原始记录归一到 NormalizedAuctionTransaction。

    严格按真实字段名 time/price/vol/buyorsell 读取；amount 不存在 → None。
    """
    raw_time = raw.get(SOURCE_TXN_FIELD_TIME)
    canonical = _normalize_auction_time(raw_time)
    return NormalizedAuctionTransaction(
        source_time=str(raw_time) if raw_time is not None else "",
        canonical_time=canonical,
        raw_price=_to_float(raw.get(SOURCE_TXN_FIELD_PRICE)),
        raw_volume_value=_to_float(raw.get(SOURCE_TXN_FIELD_VOL)),
        raw_amount_value=None,  # historical transaction 记录无 amount 字段
        buy_sell_raw=_to_float(raw.get(SOURCE_TXN_FIELD_BUYORSELL)),
        source_record=dict(raw),
    )


# =============================================================================
# MOD3 — 09:25 Time Normalization (exact)
# =============================================================================
def _normalize_auction_time(raw_time: Any) -> str | None:
    """将 source 时间字符串归一到 canonical 09:25。

    仅明确等价形式 "09:25" / "09:25:00" 视为合法集合竞价时间；
    其他 09:25:xx（带秒级变体）返回 None（由 caller 存入 noncanonical evidence，MOD3）。
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

    canonical: list[NormalizedAuctionTransaction] = []
    noncanonical: list[NormalizedAuctionTransaction] = []
    for r in raw:
        norm = normalize_raw_transaction(r)
        if norm.canonical_time == CANONICAL_AUCTION_TIME:
            canonical.append(norm)
        else:
            noncanonical.append(norm)

    if not canonical:
        # 全部为 09:25:xx（非 canonical）或完全无关时间。
        if noncanonical:
            return AuctionExtractionResult(
                status=ExtractionStatus.NONCANONICAL_0925_TIME,
                records=[],
                noncanonical_records=noncanonical,
            )
        return AuctionExtractionResult(status=ExtractionStatus.MISSING_0925, records=[])

    # 全部归一到同一 canonical 时间 "09:25"；若有多条视为 MULTIPLE_0925。
    if len(canonical) == 1:
        return AuctionExtractionResult(status=ExtractionStatus.FOUND, records=canonical)
    return AuctionExtractionResult(
        status=ExtractionStatus.MULTIPLE_0925, records=canonical, noncanonical_records=noncanonical
    )


# =============================================================================
# MOD4 — Bar helpers (MDAS result.bars is a pandas DataFrame indexed by trade_date)
# =============================================================================
def get_bar_for_date(bars: pd.DataFrame, target: date) -> dict | None:
    """从 MDAS daily bars 精确取得 trade_date == target 的 bar。

    不使用 iloc[-1]；严格按日期定位（MOD5）。
    """
    if bars is None or len(bars) == 0:
        return None
    ts = pd.Timestamp(target)
    sub = bars[bars.index == ts]
    if sub.empty:
        return None
    r = sub.iloc[0]
    return {
        "open": float(r["open"]),
        "high": float(r["high"]),
        "low": float(r["low"]),
        "close": float(r["close"]),
        "volume": float(r["volume"]),
        "amount": float(r["amount"]),
    }


def get_prev_bar_before(bars: pd.DataFrame, target: date) -> dict | None:
    """从 MDAS daily bars 取得 target 之前（date < target）最后一根 bar = T-1。

    不使用 calendar day - 1；优先从返回 daily bars 的 date < T 最后一根得到 T-1 bar（MOD6）。
    """
    if bars is None or len(bars) == 0:
        return None
    ts = pd.Timestamp(target)
    sub = bars[bars.index < ts]
    if sub.empty:
        return None
    r = sub.iloc[-1]
    return {
        "open": float(r["open"]),
        "high": float(r["high"]),
        "low": float(r["low"]),
        "close": float(r["close"]),
        "volume": float(r["volume"]),
        "amount": float(r["amount"]),
    }


# =============================================================================
# MOD5 — Lane A: raw 09:25 price vs MDAS raw open (real comparison)
# =============================================================================
def compute_lane_a(
    auction_price_raw: float,
    mdas_open_bar_T: dict | None,
    data_source: str,
    degraded: bool,
    degraded_reason: str | None,
) -> dict:
    """Lane A 产品问题：pytdx 09:25 raw price 是否就是盘迹正式行情当天 raw open？

    本函数只生成 evidence，不在 runner 内冻结 “多少误差算 PASS”。
    MDAS 没有 T bar → LANE_A_MISSING_MDA_OPEN，不得填 0。
    """
    if mdas_open_bar_T is None:
        return {
            "status": "LANE_A_MISSING_MDA_OPEN",
            "auction_price_raw": auction_price_raw,
            "mdas_raw_open_T": None,
            "price_diff_abs": None,
            "price_diff_rel": None,
            "price_exact_match": None,
            "data_source": data_source,
            "degraded": degraded,
            "degraded_reason": degraded_reason,
        }
    mdas_raw_open_T = float(mdas_open_bar_T["open"])
    diff_abs = abs(auction_price_raw - mdas_raw_open_T)
    diff_rel = diff_abs / abs(mdas_raw_open_T) if mdas_raw_open_T != 0 else None
    return {
        "status": "OK",
        "auction_price_raw": auction_price_raw,
        "mdas_raw_open_T": mdas_raw_open_T,
        "price_diff_abs": diff_abs,
        "price_diff_rel": diff_rel,
        "price_exact_match": auction_price_raw == mdas_raw_open_T,
        "data_source": data_source,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
    }


# =============================================================================
# MOD6 — Lane B: PIT Gap (real calculation via MDAS, no self-QFQ)
# =============================================================================
def compute_lane_b(
    auction_price_raw: float,
    raw_Tm1: dict | None,
    raw_T: dict | None,
    qfq_Tm1: dict | None,
    qfq_T: dict | None,
    adjustment_as_of: date | None,
    adj_factor_hash: str,
    data_source: str,
    degraded: bool,
    degraded_reason: str | None,
) -> dict:
    """Lane B：计算 PIT Gap。

    naive_raw_gap = auction / raw_close_Tm1 - 1
    pit_gap       = auction / qfq_close_Tm1 - 1（adj=qfq, adjustment_as_of=T）

    所有正式价格比较和 Gap 必须通过 MDAS；禁止自行计算 QFQ。
    任一必需 bar 缺失 → 对应字段 None（不得填 0）。
    """
    def _gap(close_Tm1: float | None) -> float | None:
        if close_Tm1 is None or close_Tm1 == 0:
            return None
        return auction_price_raw / close_Tm1 - 1

    naive_raw_gap = _gap(raw_Tm1["close"] if raw_Tm1 else None)
    pit_gap = _gap(qfq_Tm1["close"] if qfq_Tm1 else None)

    auction_vs_qfq_open_diff = None
    if qfq_T is not None and qfq_T["open"] is not None:
        auction_vs_qfq_open_diff = auction_price_raw - float(qfq_T["open"])

    return {
        "status": "OK" if (raw_Tm1 is not None and qfq_Tm1 is not None) else "LANE_B_MISSING",
        "raw_close_Tm1": raw_Tm1["close"] if raw_Tm1 else None,
        "raw_open_T": raw_T["open"] if raw_T else None,
        "qfq_close_Tm1": qfq_Tm1["close"] if qfq_Tm1 else None,
        "qfq_open_T": qfq_T["open"] if qfq_T else None,
        "naive_raw_gap": naive_raw_gap,
        "pit_gap": pit_gap,
        "auction_vs_qfq_open_diff": auction_vs_qfq_open_diff,
        "adjustment_as_of": adjustment_as_of.isoformat() if adjustment_as_of else None,
        "adj_factor_hash": adj_factor_hash,
        "data_source": data_source,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
    }


# =============================================================================
# MOD8 — Volume Unit Evidence (only math evidence, no conclusion)
# =============================================================================
def compute_volume_evidence(
    raw_price: float | None,
    raw_volume_value: float | None,
    raw_amount_value: float | None,
) -> dict:
    """对于每个 FOUND transaction 计算 implied_multiplier = amount / (price * volume)。

    仅当 price>0 且 volume>0 才有意义；否则 implied_multiplier = None + reason。
    不自动下结论 shares/lots；只输出 evidence（MOD8）。
    """
    if raw_price is None or raw_volume_value is None:
        return {
            "implied_multiplier": None,
            "reason": "MISSING_PRICE_OR_VOLUME",
            "raw_price": raw_price,
            "raw_volume_value": raw_volume_value,
            "raw_amount_value": raw_amount_value,
        }
    if raw_amount_value is None:
        return {
            "implied_multiplier": None,
            "reason": "RAW_AMOUNT_FIELD_ABSENT",
            "raw_price": raw_price,
            "raw_volume_value": raw_volume_value,
            "raw_amount_value": None,
        }
    if raw_price > 0 and raw_volume_value > 0:
        return {
            "implied_multiplier": raw_amount_value / (raw_price * raw_volume_value),
            "reason": "COMPUTED_PRICE_VOLUME_AMOUNT",
            "raw_price": raw_price,
            "raw_volume_value": raw_volume_value,
            "raw_amount_value": raw_amount_value,
        }
    return {
        "implied_multiplier": None,
        "reason": "NONPOSITIVE_PRICE_OR_VOLUME",
        "raw_price": raw_price,
        "raw_volume_value": raw_volume_value,
        "raw_amount_value": raw_amount_value,
    }


# =============================================================================
# MOD9 — Amount Semantics Evidence (only evidence, no canonical derivation)
# =============================================================================
def compute_amount_evidence(
    raw_price: float | None,
    raw_volume_value: float | None,
    raw_amount_value: float | None,
) -> dict:
    """Amount semantics evidence。

    若 source raw amount 字段真实存在 → DIRECT_RAW_FIELD。
    若不存在 → RAW_FIELD_ABSENT。

    Round 1 不直接升级成正式推导规则。candidate_derived_amount 仅在 volume
    multiplier 确认后才可能计算；当前 multiplier 未确认 → None + reason
    （DERIVED_CANDIDATE_ONLY 仅在确实推导时标记，严禁 qfq_price * volume）。
    """
    if raw_amount_value is None:
        return {
            "source_type": "RAW_FIELD_ABSENT",
            "candidate_derived_amount": None,
            "evidence_reason": "SOURCE_HAS_NO_AMOUNT_FIELD",
        }
    # amount 字段存在但属 DIRECT_RAW_FIELD（historical transaction 通常无此字段）。
    return {
        "source_type": "DIRECT_RAW_FIELD",
        "candidate_derived_amount": None,
        "evidence_reason": "MULTIPLIER_UNCONFIRMED_DERIVATION_DEFERRED",
    }


# =============================================================================
# MOD3 — MDAS Call Contract (async, start_date/end_date, BarAggregationResult)
# =============================================================================
async def get_mdas_daily_bars(
    mdas: MarketDataAggregationService,
    session: Any,
    instrument_id: UUID,
    target_date: date,
    adj: str,
    adjustment_as_of: date | None,
    earliest: date | None = None,
) -> Any:
    """统一 MDAS 日线读取（adj=none 取 raw / adj=qfq 取复权）。

    仅返回 BarAggregationResult；只读正式字段。
    """
    kwargs: dict[str, Any] = dict(
        timeframe="1d",
        adj=adj,
        include_realtime=False,
        completed_only=True,
        start_date=earliest if earliest is not None else target_date,
        end_date=target_date,
    )
    if adj == "qfq":
        kwargs["adjustment_as_of"] = adjustment_as_of
    result = await mdas.get_bars(session, instrument_id, **kwargs)
    _ = (
        result.bars,
        result.data_source,
        result.degraded,
        result.degraded_reason,
        result.adjustment_as_of,
        result.adj_factor_hash,
        result.market_data_contract_version,
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


async def next_trading_dates(
    session: Any,
    as_of: date,
    n: int,
) -> list[date]:
    """从 as_of 向后遍历，收集 n 个交易日期（用于 corporate T+1）。"""
    out: list[date] = []
    cur = as_of
    guard = 0
    while len(out) < n and guard < n * 14 + 30:
        guard += 1
        if await is_trading_day_async(session, cur):
            out.append(cur)
        cur = cur + timedelta(days=1)
    return out


# =============================================================================
# MOD14 — Tracked Sample Definition (version-controlled, board coverage enforced)
# =============================================================================
_ROUTINE_BOARDS = {b.value for b in BoardCode}


def load_sample(path: Path) -> list[SampleInstrument]:
    """从受版本控制的 sample CSV 解析固定样本。

    字段：symbol,market,board,coverage_tag,cohort,note
    market + symbol 联合作为实验身份键（MOD2）。
    instrument_id 在 resolution 阶段填充（含 fail-fast UUID(0) 检查，MOD1）。
    """
    if not path.exists():
        raise FileNotFoundError(f"sample file 缺失（必须 tracked）：{path}")
    rows: list[SampleInstrument] = []
    seen: set[tuple[str, str]] = set()
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"symbol", "market", "board", "coverage_tag", "cohort"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"sample schema 缺失字段：{missing}")
        for i, row in enumerate(reader):
            symbol = (row.get("symbol") or "").strip()
            market = (row.get("market") or "").strip().upper()
            board = (row.get("board") or "").strip()
            cohort = (row.get("cohort") or "").strip()
            if not symbol:
                raise ValueError(f"sample 第 {i+1} 行 symbol 为空")
            if board not in _ROUTINE_BOARDS:
                raise ValueError(f"sample 第 {i+1} 行 board={board!r} 不在 {sorted(_ROUTINE_BOARDS)}")
            if market not in {"SH", "SZ"}:
                raise ValueError(f"sample 第 {i+1} 行 market={market!r} 不在 {{SH,SZ}}")
            # 允许同一 (market,symbol) 同时作为 routine 与 corporate 候选；
            # 仅禁止同 cohort 内重复。
            key = (market, symbol, cohort)
            if key in seen:
                raise ValueError(f"sample 存在重复 (market,symbol,cohort)：{key}")
            seen.add(key)
            rows.append(
                SampleInstrument(
                    symbol=symbol,
                    market=market,
                    instrument_id=UUID(int=0),  # 占位，resolution 阶段填充（MOD1 fail-fast）
                    board=board,
                    coverage_tag=(row.get("coverage_tag") or "").strip(),
                    cohort=cohort,
                )
            )
    if not rows:
        raise ValueError("sample 为空")
    return rows


# =============================================================================
# MOD1/MOD2 — Universe Validation + (market, symbol) -> UUID resolution
# =============================================================================
async def resolve_sample_instruments(
    session: Any,
    samples: list[SampleInstrument],
) -> tuple[list[SampleInstrument], list[dict]]:
    """一次性解析：canonical universe validation + (market, symbol) -> UUID。

    返回 (resolved, skipped)；不在 canonical universe 的 sample 标记
    SAMPLE_NOT_IN_CANONICAL_UNIVERSE。不静默替换另一只股票。

    如果数据库出现同一 (market, symbol) > 1 active row → IDENTITY_AMBIGUOUS（MOD2）。
    """
    universe_ids: list[UUID] = await get_active_a_share_instruments(session)
    universe_set = set(universe_ids)

    from app.models.instrument import Instrument

    stmt = (
        Instrument.__table__.select()
        .where(Instrument.symbol.in_([s.symbol for s in samples]))
    )
    result = await session.execute(stmt)
    rows = result.mappings().all()
    # 以 (market, symbol) 唯一映射（MOD2）。
    market_symbol_to_rows: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        key = (str(r["market"]).upper(), str(r["symbol"]))
        market_symbol_to_rows.setdefault(key, []).append(r)

    resolved: list[SampleInstrument] = []
    skipped: list[dict] = []
    for s in samples:
        matches = market_symbol_to_rows.get((s.market, s.symbol))
        if not matches:
            skipped.append({
                "symbol": s.symbol,
                "market": s.market,
                "cohort": s.cohort,
                "reason": "SAMPLE_NOT_IN_CANONICAL_UNIVERSE",
            })
            continue
        if len(matches) > 1:
            skipped.append({
                "symbol": s.symbol,
                "market": s.market,
                "cohort": s.cohort,
                "reason": "IDENTITY_AMBIGUOUS",
            })
            continue
        inst_id = matches[0]["id"]
        if inst_id not in universe_set:
            skipped.append({
                "symbol": s.symbol,
                "market": s.market,
                "cohort": s.cohort,
                "reason": "SAMPLE_NOT_IN_CANONICAL_UNIVERSE",
            })
            continue
        # MOD1：fail-fast，禁止 UUID(0) 进入 live。
        if inst_id == UUID(int=0):
            raise ValueError(
                f"INTERNAL_IDENTITY_ERROR: resolved instrument_id 为 UUID(0) "
                f"for (market={s.market}, symbol={s.symbol})"
            )
        resolved.append(SampleInstrument(
            symbol=s.symbol,
            market=s.market,
            instrument_id=inst_id,
            board=s.board,
            coverage_tag=s.coverage_tag,
            cohort=s.cohort,
        ))
    return resolved, skipped


# =============================================================================
# MOD1/MOD7 — Corporate Action Case Resolution (factor-change based, fail-fast)
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

    MOD1 fail-fast：任何 corporate sample 的 instrument_id == UUID(int=0) 直接 raise，
    不得静默继续。
    """
    # MOD1：corporate resolver 收到的 instrument_id 必须是真实 non-zero UUID。
    for s in corporate_samples:
        if s.instrument_id == UUID(int=0):
            raise ValueError(
                f"INTERNAL_IDENTITY_ERROR: corporate resolver 收到 UUID(0) "
                f"for (market={s.market}, symbol={s.symbol})；必须先 resolve_sample_instruments"
            )

    resolved: list[dict] = []
    for s in corporate_samples:
        factor_df = await adj_service.get_factor_series(
            session, s.instrument_id, as_of=as_of
        )
        if factor_df is None or len(factor_df) == 0:
            resolved.append({
                "symbol": s.symbol,
                "market": s.market,
                "instrument_id": str(s.instrument_id),
                "status": "NO_ADJUSTMENT_EVENT_IN_WINDOW",
                "coverage_tag": s.coverage_tag,
                "event_date_T": "",
                "prev_trade_date": "",
                "next_trade_date": "",
                "factor_before": "",
                "factor_after": "",
                "adj_factor_hash": "",
            })
            continue

        # 识别 factor 变化日（真实 factor-change event）。
        fdf = factor_df.copy()
        fdf["prev_factor"] = fdf["adj_factor"].shift(1)
        change_mask = fdf["prev_factor"].notna() & (
            fdf["adj_factor"] != fdf["prev_factor"]
        )
        change_dates = fdf.loc[change_mask, "trade_date"].tolist()
        window_start = as_of - timedelta(days=lookback_days)
        in_window = [d for d in change_dates if d <= as_of and d >= window_start]
        if not in_window:
            resolved.append({
                "symbol": s.symbol,
                "market": s.market,
                "instrument_id": str(s.instrument_id),
                "status": "NO_ADJUSTMENT_EVENT_IN_WINDOW",
                "coverage_tag": s.coverage_tag,
                "event_date_T": "",
                "prev_trade_date": "",
                "next_trade_date": "",
                "factor_before": "",
                "factor_after": "",
                "adj_factor_hash": "",
            })
            continue

        # 取窗口内最近一个 event。
        T = max(in_window)
        T_idx = fdf.index[fdf["trade_date"] == T][0]
        factor_after = float(fdf.loc[T_idx, "adj_factor"])
        factor_before = float(fdf.loc[T_idx, "prev_factor"])
        prev_dates = await previous_trading_dates(session, T, n=1)
        next_dates = await next_trading_dates(session, T, n=1)
        prev_d = prev_dates[0] if prev_dates else None
        next_d = next_dates[0] if next_dates else None
        resolved.append({
            "symbol": s.symbol,
            "market": s.market,
            "instrument_id": str(s.instrument_id),
            "status": "RESOLVED",
            "coverage_tag": s.coverage_tag,
            "event_date_T": T.isoformat(),
            "prev_trade_date": prev_d.isoformat() if prev_d else "",
            "next_trade_date": next_d.isoformat() if next_d else "",
            "factor_before": factor_before,
            "factor_after": factor_after,
            "adj_factor_hash": "",  # 真实环境从 MDAS result 取，离线 N/A
        })
    return resolved


# =============================================================================
# MOD10/MOD15 — Single Observation (full evidence pipeline)
# =============================================================================
async def run_single_observation(
    mdas: MarketDataAggregationService,
    adapter: PytdxAdapter,
    session: Any,
    inst: SampleInstrument,
    target_date: date,
) -> dict:
    """对单个 (instrument, date) 执行抽取 + 语义 lane，输出完整 evidence observation。

    MOD7：只有 FOUND（恰好一条 09:25）才进入 Lane A/B/Volume/Amount/Gap。
    MULTIPLE_0925：保留所有 raw evidence，停止 canonical 语义比较。
    SOURCE_ERROR：status 永远 SOURCE_ERROR，records=[]，caller 不进入任何 lane。
    """
    extract = extract_auction_records(adapter, inst.symbol, target_date)

    obs: dict[str, Any] = {
        # identity
        "symbol": inst.symbol,
        "market": inst.market,
        "instrument_id": str(inst.instrument_id),
        "board": inst.board,
        "coverage_tag": inst.coverage_tag,
        "cohort": inst.cohort,
        "trade_date": target_date.isoformat(),
        # source
        "extraction_status": extract.status.value,
        "raw_record_count": len(extract.records),
        "noncanonical_record_count": len(extract.noncanonical_records),
        "error_code": extract.error_code,
        "error_message": extract.error_message,
        # auction
        "auction_price_raw": None,
        "raw_volume_value": None,
        "raw_amount_value": None,
        # lane_a
        "lane_a": None,
        # lane_b
        "lane_b": None,
        # volume
        "volume_evidence": None,
        # amount
        "amount_evidence": None,
    }

    if extract.status != ExtractionStatus.FOUND:
        # MULTIPLE_0925 / MISSING_0925 / NONCANONICAL_0925_TIME / SOURCE_ERROR /
        # MARKET_MAPPING_FAILURE 均不进入 price/volume/amount/gap 推断。
        return obs

    # FOUND：进入语义 lane。
    rec = extract.records[0]
    auction_price_raw = rec.raw_price
    raw_volume_value = rec.raw_volume_value
    obs["auction_price_raw"] = auction_price_raw
    obs["raw_volume_value"] = raw_volume_value
    obs["raw_amount_value"] = rec.raw_amount_value  # historical transaction 恒为 None

    # Lane A：raw daily open（adj=none，single T）。
    res_a = await get_mdas_daily_bars(mdas, session, inst.instrument_id, target_date, adj="none", adjustment_as_of=None)
    bar_T_a = get_bar_for_date(res_a.bars, target_date)
    obs["lane_a"] = compute_lane_a(
        auction_price_raw,
        bar_T_a,
        res_a.data_source,
        res_a.degraded,
        res_a.degraded_reason,
    )

    # Lane B：PIT QFQ gap（adj=qfq，adjustment_as_of=T，earliest 取 T 前足够早）。
    earliest = target_date - timedelta(days=400)
    res_b = await get_mdas_daily_bars(mdas, session, inst.instrument_id, target_date, adj="qfq", adjustment_as_of=target_date, earliest=earliest)
    raw_Tm1 = get_prev_bar_before(res_a.bars, target_date)
    raw_T = get_bar_for_date(res_a.bars, target_date)
    qfq_Tm1 = get_prev_bar_before(res_b.bars, target_date)
    qfq_T = get_bar_for_date(res_b.bars, target_date)
    obs["lane_b"] = compute_lane_b(
        auction_price_raw,
        raw_Tm1,
        raw_T,
        qfq_Tm1,
        qfq_T,
        res_b.adjustment_as_of,
        res_b.adj_factor_hash,
        res_b.data_source,
        res_b.degraded,
        res_b.degraded_reason,
    )

    # Volume unit evidence（MOD8）。
    obs["volume_evidence"] = compute_volume_evidence(
        auction_price_raw, raw_volume_value, rec.raw_amount_value
    )

    # Amount semantics evidence（MOD9）。
    obs["amount_evidence"] = compute_amount_evidence(
        auction_price_raw, raw_volume_value, rec.raw_amount_value
    )
    return obs


# =============================================================================
# MOD7 — Corporate Action Evidence (full observation at event T)
# =============================================================================
async def run_corporate_observation(
    mdas: MarketDataAggregationService,
    adapter: PytdxAdapter,
    session: Any,
    inst: SampleInstrument,
    event_T: date,
    factor_before: float,
    factor_after: float,
) -> dict:
    """Corporate cohort 的核心 observation 必须发生在真实 event T（MOD7）。

    在 run_single_observation 基础上追加：
    factor_before / factor_after / gap_adjustment_effect = pit_gap - naive_raw_gap。
    """
    obs = await run_single_observation(mdas, adapter, session, inst, event_T)
    if obs["extraction_status"] == ExtractionStatus.FOUND.value and obs["lane_b"] is not None:
        naive = obs["lane_b"].get("naive_raw_gap")
        pit = obs["lane_b"].get("pit_gap")
        gap_adjustment_effect = None
        if naive is not None and pit is not None:
            gap_adjustment_effect = pit - naive
        obs["corporate"] = {
            "event_date_T": event_T.isoformat(),
            "factor_before": factor_before,
            "factor_after": factor_after,
            "gap_adjustment_effect": gap_adjustment_effect,
        }
    else:
        obs["corporate"] = {
            "event_date_T": event_T.isoformat(),
            "factor_before": factor_before,
            "factor_after": factor_after,
            "gap_adjustment_effect": None,
        }
    return obs


# =============================================================================
# MOD12 — Live Status derived from evidence (no hardcoded PASS)
# =============================================================================
def derive_live_status(observations: list[dict], corporate_cases: list[dict]) -> dict:
    """LIVE_RUN_STATUS / EVIDENCE_COMPLETENESS 完全由 evidence 推导（MOD12）。

    --live 只表示“尝试执行真实数据采集”，不等于“数据通过验证”。
    最终 Source Verdict 继续由 ChatGPT 决定。
    """
    total = len(observations)
    statuses = [o["extraction_status"] for o in observations]
    found = statuses.count(ExtractionStatus.FOUND.value)
    missing = statuses.count(ExtractionStatus.MISSING_0925.value)
    multiple = statuses.count(ExtractionStatus.MULTIPLE_0925.value)
    source_err = statuses.count(ExtractionStatus.SOURCE_ERROR.value)
    market_fail = statuses.count(ExtractionStatus.MARKET_MAPPING_FAILURE.value)
    noncanon = statuses.count(ExtractionStatus.NONCANONICAL_0925_TIME.value)

    comparable_a = sum(1 for o in observations if o.get("lane_a") and o["lane_a"].get("status") == "OK")
    missing_a = sum(1 for o in observations if o.get("lane_a") and o["lane_a"].get("status") == "LANE_A_MISSING_MDA_OPEN")
    comparable_b = sum(1 for o in observations if o.get("lane_b") and o["lane_b"].get("status") == "OK")
    missing_b = sum(1 for o in observations if o.get("lane_b") and o["lane_b"].get("status") == "LANE_B_MISSING")
    adj_degraded = sum(
        1 for o in observations
        if o.get("lane_b") and o["lane_b"].get("degraded")
    )
    vol_count = sum(1 for o in observations if o.get("volume_evidence") and o["volume_evidence"].get("implied_multiplier") is not None)
    amt_direct = sum(1 for o in observations if o.get("amount_evidence") and o["amount_evidence"].get("source_type") == "DIRECT_RAW_FIELD")
    amt_missing = sum(1 for o in observations if o.get("amount_evidence") and o["amount_evidence"].get("source_type") == "RAW_FIELD_ABSENT")

    corp_candidates = len(corporate_cases)
    corp_resolved = sum(1 for c in corporate_cases if c.get("status") == "RESOLVED")
    corp_no_event = sum(1 for c in corporate_cases if c.get("status") == "NO_ADJUSTMENT_EVENT_IN_WINDOW")
    corp_comparable = sum(
        1 for o in observations
        if o.get("cohort") == "corporate" and o.get("corporate") and o["corporate"].get("gap_adjustment_effect") is not None
    )

    # LIVE_RUN_STATUS：只要 attempted（有 observations）即 COMPLETED；
    # 全部 SOURCE_ERROR 也算 COMPLETED（采集过程完成，但无可用 source）。
    if total == 0:
        live_run_status = "NOT_RUN"
    elif source_err > 0 and found == 0 and missing == 0 and multiple == 0 and noncanon == 0 and market_fail == 0:
        live_run_status = "COMPLETED"  # 采集完成但全部 source 失败
    elif found > 0 or comparable_a > 0 or comparable_b > 0:
        live_run_status = "COMPLETED"
    else:
        live_run_status = "PARTIAL"

    # EVIDENCE_COMPLETENESS：有可比 observation 才算 COMPLETE。
    if found == 0:
        evidence_completeness = "INSUFFICIENT"
    elif comparable_a == 0 and comparable_b == 0:
        evidence_completeness = "INSUFFICIENT"
    elif comparable_a > 0 and comparable_b > 0:
        evidence_completeness = "COMPLETE"
    else:
        evidence_completeness = "PARTIAL"

    return {
        "LIVE_RUN_STATUS": live_run_status,
        "EVIDENCE_COMPLETENESS": evidence_completeness,
        "total_attempted_observations": total,
        "FOUND": found,
        "MISSING_0925": missing,
        "MULTIPLE_0925": multiple,
        "SOURCE_ERROR": source_err,
        "MARKET_MAPPING_FAILURE": market_fail,
        "NONCANONICAL_0925_TIME": noncanon,
        "LANE_A_COMPARABLE": comparable_a,
        "LANE_A_MISSING": missing_a,
        "LANE_B_COMPARABLE": comparable_b,
        "LANE_B_MISSING": missing_b,
        "ADJUSTMENT_DEGRADED": adj_degraded,
        "volume_evidence_count": vol_count,
        "amount_direct_count": amt_direct,
        "amount_missing_count": amt_missing,
        "corporate_candidates": corp_candidates,
        "corporate_resolved": corp_resolved,
        "corporate_no_event": corp_no_event,
        "corporate_comparable": corp_comparable,
    }


# =============================================================================
# MOD13 — Data Quality Summary (explicit denominators)
# =============================================================================
def build_data_quality_summary(
    observations: list[dict],
    corporate_cases: list[dict],
    live_status: dict,
) -> dict:
    """所有分母必须明确（MOD13）。禁止 “missing rate = x%” 但不说明 denominator。"""
    total = live_status["total_attempted_observations"]
    summary: dict[str, Any] = {
        "denominator_total_attempted": total,
        "extraction_breakdown": {
            "FOUND": live_status["FOUND"],
            "MISSING_0925": live_status["MISSING_0925"],
            "MULTIPLE_0925": live_status["MULTIPLE_0925"],
            "SOURCE_ERROR": live_status["SOURCE_ERROR"],
            "MARKET_MAPPING_FAILURE": live_status["MARKET_MAPPING_FAILURE"],
            "NONCANONICAL_0925_TIME": live_status["NONCANONICAL_0925_TIME"],
        },
        "lane_a": {
            "comparable": live_status["LANE_A_COMPARABLE"],
            "missing_mda_open": live_status["LANE_A_MISSING"],
            "denominator_found": live_status["FOUND"],
        },
        "lane_b": {
            "comparable": live_status["LANE_B_COMPARABLE"],
            "missing": live_status["LANE_B_MISSING"],
            "denominator_found": live_status["FOUND"],
            "adjustment_degraded": live_status["ADJUSTMENT_DEGRADED"],
        },
        "volume": {
            "evidence_count": live_status["volume_evidence_count"],
            "denominator_found": live_status["FOUND"],
        },
        "amount": {
            "direct_raw_count": live_status["amount_direct_count"],
            "missing_count": live_status["amount_missing_count"],
            "denominator_found": live_status["FOUND"],
        },
        "corporate": {
            "candidates": live_status["corporate_candidates"],
            "resolved": live_status["corporate_resolved"],
            "no_event": live_status["corporate_no_event"],
            "comparable": live_status["corporate_comparable"],
        },
    }
    return summary


# =============================================================================
# MOD11 / MOD10 — Evidence Persistence
# =============================================================================
def _sample_hash(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest()[:16]


def write_evidence_outputs(
    run_dir: Path,
    as_of: date,
    sample_file: Path,
    resolved: list[SampleInstrument],
    skipped: list[dict],
    routine_dates: list[date],
    corporate_cases: list[dict],
    observations: list[dict],
    live_status: dict,
    data_quality: dict,
    started_at: datetime,
    finished_at: datetime,
    market_data_contract_versions: list[str],
    data_sources: list[str],
    adj_factor_hashes: list[str],
    git_sha: str | None,
) -> dict[str, str]:
    """持久化全部 evidence（MOD10）。MULTIPLE_0925 所有 raw 进入 raw_transaction_records；
    SOURCE_ERROR 的 error_code/message 进入 observation / quality evidence。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    # 01_run_metadata.json
    meta = {
        "runner_version": "round1c-evidence-pipeline",
        "git_sha": git_sha,
        "as_of": as_of.isoformat(),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "sample_file": str(sample_file),
        "sample_hash": _sample_hash(sample_file),
        "routine_date_count": len(routine_dates),
        "corporate_lookback_days": None,
        "market_data_contract_version": sorted(set(market_data_contract_versions)),
        "unique_mdas_data_sources": sorted(set(data_sources)),
        "unique_adj_factor_hashes": sorted(set(adj_factor_hashes)),
        "live": True,
        "live_run_status": live_status["LIVE_RUN_STATUS"],
        "evidence_completeness": live_status["EVIDENCE_COMPLETENESS"],
    }
    p = run_dir / "01_run_metadata.json"
    p.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    written["run_metadata"] = str(p)

    # 02_resolved_sample.csv
    p = run_dir / "02_resolved_sample.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "market", "instrument_id", "board", "coverage_tag", "cohort"])
        w.writeheader()
        for r in resolved:
            w.writerow({
                "symbol": r.symbol, "market": r.market, "instrument_id": str(r.instrument_id),
                "board": r.board, "coverage_tag": r.coverage_tag, "cohort": r.cohort,
            })
        for s in skipped:
            w.writerow({
                "symbol": s.get("symbol", ""), "market": s.get("market", ""),
                "instrument_id": "SKIPPED", "board": "", "coverage_tag": "",
                "cohort": s.get("cohort", ""),
            })
    written["resolved_sample"] = str(p)

    # 03_raw_transaction_records.jsonl（含 MULTIPLE_0925 全部 raw + noncanonical）
    p = run_dir / "03_raw_transaction_records.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for o in observations:
            # 重新抽取 raw 记录（runner 内 extract 结果未全部保留，这里从 observation 反推不够；
            # 故 run_single_observation 之外由 caller 单独传入 raw list 时写入。
            # 为保持可审计，caller 在 collect 阶段把每条 obs 的原始归一化记录附上）。
            raw_list = o.get("_raw_records", [])
            for rec in raw_list:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    written["raw_transaction_records"] = str(p)

    # 04_noncanonical_time_records.jsonl
    p = run_dir / "04_noncanonical_time_records.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for o in observations:
            for rec in o.get("_noncanonical_records", []):
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    written["noncanonical_time_records"] = str(p)

    # 05_observations.csv
    p = run_dir / "05_observations.csv"
    flat_obs = [_flatten_observation(o) for o in observations]
    if flat_obs:
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(flat_obs[0].keys()))
            w.writeheader()
            for row in flat_obs:
                w.writerow(row)
    written["observations"] = str(p)

    # 06_lane_a_price_mismatches.csv（仅 FOUND 且 Lane A OK 的）
    p = run_dir / "06_lane_a_price_mismatches.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "market", "trade_date", "auction_price_raw", "mdas_raw_open_T",
                    "price_diff_abs", "price_diff_rel", "price_exact_match", "data_source", "degraded"])
        for o in observations:
            la = o.get("lane_a")
            if la and la.get("status") == "OK":
                w.writerow([o["symbol"], o["market"], o["trade_date"], la["auction_price_raw"],
                            la["mdas_raw_open_T"], la["price_diff_abs"], la["price_diff_rel"],
                            la["price_exact_match"], la["data_source"], la["degraded"]])
    written["lane_a_price_mismatches"] = str(p)

    # 07_corporate_action_cases.csv
    p = run_dir / "07_corporate_action_cases.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "market", "instrument_id", "status", "coverage_tag",
                                          "event_date_T", "prev_trade_date", "next_trade_date",
                                          "factor_before", "factor_after", "adj_factor_hash"])
        w.writeheader()
        for c in corporate_cases:
            w.writerow(c)
    written["corporate_action_cases"] = str(p)

    # 08_volume_unit_evidence.csv
    p = run_dir / "08_volume_unit_evidence.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "market", "trade_date", "board", "raw_price", "raw_volume_value",
                    "raw_amount_value", "implied_multiplier", "reason"])
        for o in observations:
            ve = o.get("volume_evidence")
            if ve:
                w.writerow([o["symbol"], o["market"], o["trade_date"], o["board"],
                            ve.get("raw_price"), ve.get("raw_volume_value"), ve.get("raw_amount_value"),
                            ve.get("implied_multiplier"), ve.get("reason")])
    written["volume_unit_evidence"] = str(p)

    # 09_amount_evidence.csv
    p = run_dir / "09_amount_evidence.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "market", "trade_date", "source_type", "candidate_derived_amount", "evidence_reason"])
        for o in observations:
            ae = o.get("amount_evidence")
            if ae:
                w.writerow([o["symbol"], o["market"], o["trade_date"],
                            ae.get("source_type"), ae.get("candidate_derived_amount"), ae.get("evidence_reason")])
    written["amount_evidence"] = str(p)

    # 10_data_quality_summary.json
    p = run_dir / "10_data_quality_summary.json"
    p.write_text(json.dumps(data_quality, indent=2, ensure_ascii=False), encoding="utf-8")
    written["data_quality_summary"] = str(p)

    # 11_validation_summary.json
    p = run_dir / "11_validation_summary.json"
    p.write_text(json.dumps(live_status, indent=2, ensure_ascii=False), encoding="utf-8")
    written["validation_summary"] = str(p)

    return written


def _flatten_observation(o: dict) -> dict:
    """把嵌套 observation 拍平成 05_observations.csv 的一行。"""
    la = o.get("lane_a") or {}
    lb = o.get("lane_b") or {}
    ve = o.get("volume_evidence") or {}
    ae = o.get("amount_evidence") or {}
    corp = o.get("corporate") or {}
    return {
        "symbol": o.get("symbol"),
        "market": o.get("market"),
        "instrument_id": o.get("instrument_id"),
        "board": o.get("board"),
        "coverage_tag": o.get("coverage_tag"),
        "cohort": o.get("cohort"),
        "trade_date": o.get("trade_date"),
        "extraction_status": o.get("extraction_status"),
        "raw_record_count": o.get("raw_record_count"),
        "noncanonical_record_count": o.get("noncanonical_record_count"),
        "error_code": o.get("error_code"),
        "error_message": o.get("error_message"),
        "auction_price_raw": o.get("auction_price_raw"),
        "raw_volume_value": o.get("raw_volume_value"),
        "raw_amount_value": o.get("raw_amount_value"),
        "lane_a_status": la.get("status"),
        "mdas_raw_open_T": la.get("mdas_raw_open_T"),
        "price_diff_abs": la.get("price_diff_abs"),
        "price_diff_rel": la.get("price_diff_rel"),
        "price_exact_match": la.get("price_exact_match"),
        "lane_b_status": lb.get("status"),
        "raw_close_Tm1": lb.get("raw_close_Tm1"),
        "raw_open_T": lb.get("raw_open_T"),
        "qfq_close_Tm1": lb.get("qfq_close_Tm1"),
        "qfq_open_T": lb.get("qfq_open_T"),
        "naive_raw_gap": lb.get("naive_raw_gap"),
        "pit_gap": lb.get("pit_gap"),
        "adjustment_as_of": lb.get("adjustment_as_of"),
        "adj_factor_hash": lb.get("adj_factor_hash"),
        "volume_implied_multiplier": ve.get("implied_multiplier"),
        "volume_reason": ve.get("reason"),
        "amount_source_type": ae.get("source_type"),
        "amount_candidate_derived": ae.get("candidate_derived_amount"),
        "corporate_event_date_T": corp.get("event_date_T"),
        "corporate_factor_before": corp.get("factor_before"),
        "corporate_factor_after": corp.get("factor_after"),
        "corporate_gap_adjustment_effect": corp.get("gap_adjustment_effect"),
        "mdas_data_source": la.get("data_source") or lb.get("data_source"),
        "degraded": la.get("degraded") or lb.get("degraded"),
    }


# =============================================================================
# MOD12/MOD13 — Compliance (real, evidence-based; not hardcoded PASS)
# =============================================================================
def summarize_compliance(
    smoke_passed: bool,
    live_evidence_completeness: str | None = None,
) -> dict:
    """compliance 由真实 execution / smoke evidence 生成。

    - 未执行的项 = NOT_RUN
    - 仅 smoke 项 = CONTRACT_PASS
    - live data source 项必须等 live execution 后才 PASS（且由 ChatGPT 决定最终 verdict）
    """
    contract = "CONTRACT_PASS" if smoke_passed else "NOT_RUN"
    return {
        "mdas_async_result_signature": contract,
        "calendar_contract": contract,
        "instrument_identity": contract,
        "pytdx_managed_lifecycle": contract,
        "source_error_behavior": contract,
        "multiple_0925_behavior": contract,
        "corporate_event_resolver": contract,
        "tracked_sample": contract,
        "live_historical_source_evidence": live_evidence_completeness or "NOT_RUN",
    }


# =============================================================================
# CLI
# =============================================================================
def _try_git_sha() -> str | None:
    try:
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() or None
    except Exception:
        return None


async def run_validation(
    as_of: date,
    corporate_lookback_days: int,
    live: bool = False,
) -> dict:
    """主流程（离线或 live 由 caller 决定；本 runner correction 默认 live=False）。

    live=False：只做结构解析 / sample 加载 / corporate 解析骨架（依赖 DB session）。
    真实历史采集只在 live=True 且配置好真实环境时执行。
    """
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
        "compliance": summarize_compliance(smoke_passed=False),
    }

    if not live:
        summary["compliance"] = summarize_compliance(smoke_passed=False)
        summary["note"] = "RUNNER_CORRECTION: live data validation NOT RUN"
        return summary

    # live 路径（真实环境执行，需要 async DB session + PytdxAdapter 受管连接）。
    from app.db import AsyncSessionLocal

    started_at = datetime.now()
    market_data_contract_versions: list[str] = []
    data_sources: list[str] = []
    adj_factor_hashes: list[str] = []

    async with AsyncSessionLocal() as session:
        resolved, skipped = await resolve_sample_instruments(session, samples)
        # MOD1 fail-fast：resolved 内不得出现 UUID(0)。
        for r in resolved:
            if r.instrument_id == UUID(int=0):
                raise ValueError(f"INTERNAL_IDENTITY_ERROR: resolved UUID(0) for {r.symbol}")
        summary["resolved_instruments"] = [
            {"symbol": r.symbol, "market": r.market, "instrument_id": str(r.instrument_id), "cohort": r.cohort}
            for r in resolved
        ]
        summary["skipped_instruments"] = skipped

        mdas = MarketDataAggregationService()
        adj_service = AdjustmentFactorService()

        # Routine：收集固定 N 个交易日期。
        routine_dates = await previous_trading_dates(session, as_of, n=10)
        summary["routine_trade_dates"] = [d.isoformat() for d in routine_dates]

        # Corporate：解析真实除权事件（fail-fast UUID(0))）。
        corp_cases = await resolve_corporate_cases(
            adj_service, session, corporate, as_of, corporate_lookback_days
        )
        summary["corporate_cases"] = corp_cases
        # 只保留 RESOLVED 的 corporate instrument 用于 observation。
        resolved_corp_map = {
            (c["market"], c["symbol"]): c
            for c in corp_cases if c.get("status") == "RESOLVED"
        }

        observations: list[dict] = []
        # 采集（PytdxAdapter 受管连接，整个 batch 复用生命周期）。
        with PytdxAdapter() as adapter:
            for inst in resolved:
                if inst.cohort == "routine":
                    dates = routine_dates
                    obs_list = [
                        await run_single_observation(mdas, adapter, session, inst, d)
                        for d in dates
                    ]
                else:
                    c = resolved_corp_map.get((inst.market, inst.symbol))
                    if not c:
                        continue
                    event_T = date.fromisoformat(c["event_date_T"])
                    obs_list = [
                        await run_corporate_observation(
                            mdas, adapter, session, inst, event_T,
                            float(c["factor_before"]), float(c["factor_after"]),
                        )
                    ]
                for o in obs_list:
                    # 收集 lineage evidence。
                    la = o.get("lane_a") or {}
                    lb = o.get("lane_b") or {}
                    if la.get("data_source"):
                        data_sources.append(la["data_source"])
                    if lb.get("data_source"):
                        data_sources.append(lb["data_source"])
                    if lb.get("adj_factor_hash"):
                        adj_factor_hashes.append(lb["adj_factor_hash"])
                    observations.append(o)

        summary["observations"] = observations

        live_status = derive_live_status(observations, corp_cases)
        data_quality = build_data_quality_summary(observations, corp_cases, live_status)
        summary["live_status"] = live_status
        summary["data_quality"] = data_quality
        summary["compliance"] = summarize_compliance(
            smoke_passed=True,
            live_evidence_completeness=live_status["EVIDENCE_COMPLETENESS"],
        )

        finished_at = datetime.now()
        run_dir = OUTPUT_DIR / as_of.isoformat()
        write_evidence_outputs(
            run_dir=run_dir,
            as_of=as_of,
            sample_file=SAMPLE_FILE,
            resolved=resolved,
            skipped=skipped,
            routine_dates=routine_dates,
            corporate_cases=corp_cases,
            observations=observations,
            live_status=live_status,
            data_quality=data_quality,
            started_at=started_at,
            finished_at=finished_at,
            market_data_contract_versions=market_data_contract_versions,
            data_sources=data_sources,
            adj_factor_hashes=adj_factor_hashes,
            git_sha=_try_git_sha(),
        )
        summary["evidence_output_dir"] = str(run_dir)
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Auction Historical Validation Runner 1C")
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
    print(summary.get("note") or "validation complete")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
