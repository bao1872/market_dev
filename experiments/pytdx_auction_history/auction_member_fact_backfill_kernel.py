"""Auction 120-Bar Backfill Kernel — Round 3B-D.

正式 backfill 不再调用 ``fetch_full_day_transactions_paginated``（保留为
source-validation reference implementation）。本 kernel 只做两件事：

1. **targeted 09:25 source fetch**（``fetch_auction_0925_targeted``）：
   hint-first + exponential + boundary binary search，只覆盖 09:25:00～09:25:59
   target window，不扫描全天。
2. **纯函数 member fact builder**（``build_historical_member_fact``）：
   target source → canonicalization → Lane A → Lane B → Member Fact。

builder 是纯函数：不 new PytdxAdapter / 不调用 MDAS / 不查 DB / 不分页全天。
所有 MDAS 结果由调用方通过 preloaded batch 传入。

source_status 冻结词表（backfill-specific，不谎称 full-day COMPLETE）：
- TARGET_WINDOW_COMPLETE      : 09:25 target minute 已被完整 bracket/覆盖（不含全天逐笔完整语义）
- SOURCE_EMPTY                : 全天无数据
- SOURCE_ERROR                : 源错误（managed retry 后仍失败）
- TARGET_SEARCH_STALLED       : 搜索停滞（重复 page / 无进展）
- TARGET_SEARCH_LIMIT_REACHED : 超出搜索请求预算
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from app.core.pytdx_adapter import PytdxAdapter

from auction_history_semantics_validation import (
    CANON_STATUS_CANONICAL,
    _normalize_raw_transaction,
    _page_fingerprint,
    canonicalize_auction_0925,
    classify_transaction_time,
    classify_raw_volume,
    compute_lane_a,
    compute_lane_b,
    get_bar_for_date,
    get_prev_bar_before,
    SampleInstrument,
    TransactionTimeClass,
    PAGE_SIZE,
)

# ---------------------------------------------------------------------------
# Frozen backfill source statuses (Part C4)
# ---------------------------------------------------------------------------
SOURCE_TARGET_WINDOW_COMPLETE = "TARGET_WINDOW_COMPLETE"
SOURCE_EMPTY = "SOURCE_EMPTY"
SOURCE_ERROR = "SOURCE_ERROR"
SOURCE_TARGET_SEARCH_STALLED = "TARGET_SEARCH_STALLED"
SOURCE_TARGET_SEARCH_LIMIT_REACHED = "TARGET_SEARCH_LIMIT_REACHED"

BACKFILL_SOURCE_FROZEN = frozenset({
    SOURCE_TARGET_WINDOW_COMPLETE,
    SOURCE_EMPTY,
    SOURCE_ERROR,
    SOURCE_TARGET_SEARCH_STALLED,
    SOURCE_TARGET_SEARCH_LIMIT_REACHED,
})

# 09:25 target window（开市竞价撮合发生在 09:25，次日 09:25 gap/amount 事实）
TARGET_WINDOW_START = "09:25:00"
TARGET_WINDOW_END = "09:25:59"

# 硬性请求预算（per symbol）：单只股票搜索 + 边界读取的 page 请求上限
MAX_TARGETED_PAGES = 24

# ---------------------------------------------------------------------------
# 内部异常（结构化错误，不进入业务 canonicalization）
# ---------------------------------------------------------------------------
class _SearchBudgetExceeded(RuntimeError):
    """超过 MAX_TARGETED_PAGES 请求预算 → TARGET_SEARCH_LIMIT_REACHED。"""


class _SearchStalled(RuntimeError):
    """搜索无进展（重复 page fingerprint）→ TARGET_SEARCH_STALLED。"""


class _SourceError(RuntimeError):
    """源错误（managed retry 后仍失败）→ SOURCE_ERROR。"""


# ---------------------------------------------------------------------------
# Targeted fetch result DTO
# ---------------------------------------------------------------------------
@dataclass
class Targeted0925Result:
    source_status: str
    records: list[dict] = field(default_factory=list)
    page_count: int = 0
    resolved_offset: Optional[int] = None
    source_first_time: Optional[str] = None
    source_last_time: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    used_hint: bool = False


def _page_time_range(page: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """page 记录时间的 (min, max)。时间字符串 HH:MM:SS 可直接字典序比较。"""
    if not page:
        return None, None
    times = [str(r.get("time", "")) for r in page]
    return min(times), max(times)


def _page_entirely_before(page: list[dict]) -> bool:
    """整页记录都早于 09:25:00（page_max < TARGET_WINDOW_START）。"""
    _, mx = _page_time_range(page)
    return mx is not None and mx < TARGET_WINDOW_START


def _page_entirely_after(page: list[dict]) -> bool:
    """整页记录都晚于 09:25:59（page_min > TARGET_WINDOW_END）。"""
    mn, _ = _page_time_range(page)
    return mn is not None and mn > TARGET_WINDOW_END


# ---------------------------------------------------------------------------
# C1/C2 — Targeted 09:25 source fetch
# ---------------------------------------------------------------------------
def fetch_auction_0925_targeted(
    adapter: PytdxAdapter,
    symbol: str,
    trade_date: date,
    *,
    offset_hint: Optional[int] = None,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_TARGETED_PAGES,
) -> Targeted0925Result:
    """找到并完整覆盖 09:25:00～09:25:59 target window 的 source records。

    算法（不线性扫全天）：
      1. hint-first：优先从上一交易日 resolved offset 附近 probe；
      2. exponential search：找不到则从 offset=0 开始指数扩展 page offset；
      3. boundary binary search：定位「最早整页早于 09:25:00」的边界 page；
      4. 读取边界相邻 page（previous/target/next）按 raw record identity 去重。

    offset_hint 只影响 transport search efficiency，不改变 source result。
    只有 source_status == TARGET_WINDOW_COMPLETE 才允许 canonicalize。
    """
    counter: dict[str, int] = {"n": 0}
    page_cache: dict[int, list[dict]] = {}
    seen_fp: set[str] = set()

    def _fetch(offset: int) -> list[dict]:
        counter["n"] += 1
        if counter["n"] > max_pages:
            raise _SearchBudgetExceeded()
        if offset in page_cache:
            return page_cache[offset]
        try:
            page = adapter.get_history_transaction_page(
                symbol, trade_date, offset, page_size
            )
        except RuntimeError as exc:
            raise _SourceError(str(exc)) from exc
        page = list(page) if page else []
        page_cache[offset] = page
        if page:
            fp = _page_fingerprint(page)
            if fp in seen_fp:
                raise _SearchStalled()
            seen_fp.add(fp)
        return page

    def _before(offset: int) -> bool:
        """offset 处 page 为空或整页早于 09:25:00。"""
        page = _fetch(offset)
        if not page:
            return True
        _, mx = _page_time_range(page)
        return mx < TARGET_WINDOW_START

    # --- 第一步：offset=0 探针，确定全天是否有数据 / 是否整日早于 09:25 ---
    try:
        first_page = _fetch(0)
    except _SourceError as exc:
        return Targeted0925Result(
            source_status=SOURCE_ERROR, page_count=counter["n"],
            error_code=type(exc).__name__, error_message=str(exc))
    except _SearchBudgetExceeded:
        return Targeted0925Result(
            source_status=SOURCE_TARGET_SEARCH_LIMIT_REACHED,
            page_count=counter["n"])

    if not first_page:
        return Targeted0925Result(source_status=SOURCE_EMPTY,
                                  page_count=counter["n"])
    if _page_entirely_before(first_page):
        # 罕见：全天最后记录也早于 09:25 → window 无记录但已完整覆盖
        return Targeted0925Result(
            source_status=SOURCE_TARGET_WINDOW_COMPLETE,
            records=[], page_count=counter["n"], resolved_offset=0,
            used_hint=offset_hint is not None)

    # --- 第二步：定位边界 B（最小 offset，其 page 为空或整页早于 09:25:00）---
    try:
        if offset_hint is not None and offset_hint >= 0:
            # hint-first：从 hint 页附近开始（hint 是上一 bar 的 resolved offset）
            hint_page = offset_hint // page_size
            if _before(hint_page * page_size):
                # hint 已过界（偏早）→ 向下搜索
                lo, hi = 0, hint_page * page_size
            else:
                lo, hi = hint_page * page_size, hint_page * page_size + page_size
                while not _before(hi):
                    lo, hi = hi, hi * 2
        else:
            # cold：exponential search from offset=0
            lo, hi = 0, page_size
            while not _before(hi):
                lo, hi = hi, hi * 2

        # boundary binary search：最小 offset 使 _before(offset) 为 True
        while hi - lo > page_size:
            mid = ((lo // page_size) + (hi // page_size)) // 2 * page_size
            if _before(mid):
                hi = mid
            else:
                lo = mid
        boundary = hi
    except _SourceError as exc:
        return Targeted0925Result(
            source_status=SOURCE_ERROR, page_count=counter["n"],
            error_code=type(exc).__name__, error_message=str(exc))
    except _SearchBudgetExceeded:
        return Targeted0925Result(
            source_status=SOURCE_TARGET_SEARCH_LIMIT_REACHED,
            page_count=counter["n"])
    except _SearchStalled:
        return Targeted0925Result(
            source_status=SOURCE_TARGET_SEARCH_STALLED,
            page_count=counter["n"])

    # --- 第三步：读取边界相邻 page（previous/target/next）去重收集 window records ---
    try:
        window_records = _collect_window_records(
            _fetch, boundary, page_size)
    except _SourceError as exc:
        return Targeted0925Result(
            source_status=SOURCE_ERROR, page_count=counter["n"],
            error_code=type(exc).__name__, error_message=str(exc))
    except _SearchBudgetExceeded:
        return Targeted0925Result(
            source_status=SOURCE_TARGET_SEARCH_LIMIT_REACHED,
            page_count=counter["n"])

    times = sorted(str(r.get("time", "")) for r in window_records)
    return Targeted0925Result(
        source_status=SOURCE_TARGET_WINDOW_COMPLETE,
        records=window_records,
        page_count=counter["n"],
        resolved_offset=boundary,
        source_first_time=times[0] if times else None,
        source_last_time=times[-1] if times else None,
        used_hint=offset_hint is not None)


def _collect_window_records(
    fetch_fn,
    boundary: int,
    page_size: int,
) -> list[dict]:
    """读取边界相邻 page（B-2 / B-1 / B），按 raw record identity 去重，返回 window records。

    window ⊆ page(B-2) ∪ page(B-1) ∪ page(B)（B 为最早整页早于 09:25 的边界），
    B+P 整页早于 window 不会包含 window records，无需读取。
    """
    seen_rows: set[tuple] = set()
    out: list[dict] = []
    offsets = sorted({
        boundary - 2 * page_size,
        boundary - page_size,
        boundary,
    })
    for off in offsets:
        if off < 0:
            continue
        page = fetch_fn(off)
        for r in page:
            key = (r.get("time"), r.get("price"), r.get("vol"),
                   r.get("buyorsell"))
            if key in seen_rows:
                continue
            seen_rows.add(key)
            t = str(r.get("time", ""))
            if TARGET_WINDOW_START <= t <= TARGET_WINDOW_END:
                out.append(r)
    return out


# ---------------------------------------------------------------------------
# C5 — canonicalization gate（只对 TARGET_WINDOW_COMPLETE 允许 canonicalize）
# ---------------------------------------------------------------------------
def canonicalize_targeted_window(
    inst: SampleInstrument,
    trade_date: date,
    targeted: Targeted0925Result,
) -> dict:
    """把 targeted window records 归一化 + canonicalize（复用现有纯函数）。

    返回 canonicalization 层 dict；Lane A/B 由 builder 在 MDAS batch 可用时计算。
    不复制 price/volume/amount business logic。
    """
    if targeted.source_status != SOURCE_TARGET_WINDOW_COMPLETE:
        return {
            "source_status": targeted.source_status,
            "extraction_status": targeted.source_status,
            "canonicalization_status": None,
            "canonicalization_reason": "SOURCE_WINDOW_INCOMPLETE",
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
        }

    normalized = [
        _normalize_raw_transaction(
            inst.symbol, inst.market, str(inst.instrument_id), trade_date, rec)
        for rec in targeted.records
    ]
    canonical_records = [
        n for n in normalized
        if classify_transaction_time(n.source_time)
        == TransactionTimeClass.CANONICAL_0925
    ]
    canon = canonicalize_auction_0925(canonical_records)

    classes = [classify_raw_volume(n.raw_volume_value) for n in canonical_records]
    return {
        "source_status": targeted.source_status,
        "extraction_status": "TARGET_WINDOW",
        "canonicalization_status": canon.canonicalization_status,
        "canonicalization_reason": canon.reason,
        "raw_canonical_record_count": canon.raw_canonical_record_count,
        "positive_volume_record_count": canon.positive_volume_record_count,
        "zero_volume_record_count": canon.zero_volume_record_count,
        "invalid_volume_record_count": canon.invalid_volume_record_count,
        "invalid_price_count": (
            1 if canon.canonicalization_status == "INVALID_PRICE_0925" else 0),
        "auction_price_raw": canon.auction_price_raw,
        "auction_volume_raw_lots": canon.auction_volume_raw_lots,
        "auction_volume_shares": canon.auction_volume_shares,
        "auction_amount": canon.auction_amount,
        "auction_amount_source_type": canon.amount_source_type,
    }


# ---------------------------------------------------------------------------
# Part D — build_historical_member_fact（纯函数 kernel）
# ---------------------------------------------------------------------------
def build_historical_member_fact(
    inst: SampleInstrument,
    trade_date: date,
    targeted: Targeted0925Result,
    raw_res: Any,
    qfq_res: Any,
    *,
    listing_date: Optional[date] = None,
    as_of: date,
    bar_index: int = 0,
    code_sha: Optional[str] = None,
) -> dict:
    """target source → canonicalization → Lane A → Lane B → Member Fact。

    纯函数：不 new PytdxAdapter / 不调用 MDAS / 不查 DB / 不分页全天。
    ``raw_res`` / ``qfq_res`` 是调用方 preloaded MDAS batch 结果
    （含 .bars DataFrame / .data_source / .degraded / .degraded_reason /
    .adj_factor_hash）。
    """
    canon_layer = canonicalize_targeted_window(inst, trade_date, targeted)

    obs = {
        "symbol": inst.symbol,
        "market": inst.market,
        "instrument_id": str(inst.instrument_id),
        "board": inst.board,
        "coverage_tag": inst.coverage_tag,
        "cohort": inst.cohort,
        "trade_date": trade_date.isoformat(),
        "listing_date": listing_date.isoformat() if listing_date else None,
        **canon_layer,
    }

    # Lane A / Lane B 仅当 CANONICAL 且 MDAS batch 可用
    if (canon_layer["canonicalization_status"] == CANON_STATUS_CANONICAL
            and raw_res is not None and qfq_res is not None):
        auction_price = canon_layer["auction_price_raw"]
        open_bar_T = get_bar_for_date(raw_res.bars, trade_date)
        obs["lane_a"] = compute_lane_a(
            auction_price, open_bar_T, raw_res.data_source,
            raw_res.degraded, raw_res.degraded_reason)
        raw_Tm1 = get_prev_bar_before(raw_res.bars, trade_date)
        qfq_Tm1 = get_prev_bar_before(qfq_res.bars, trade_date)
        obs["lane_b"] = compute_lane_b(
            auction_price, raw_Tm1, open_bar_T, qfq_Tm1,
            get_bar_for_date(qfq_res.bars, trade_date), trade_date,
            qfq_res.adj_factor_hash, qfq_res.data_source,
            qfq_res.degraded, qfq_res.degraded_reason)
    else:
        obs["lane_a"] = None
        obs["lane_b"] = None

    # 元数据（不加入 L1 顶层 provenance；仅 runner 投影用）
    obs["_as_of"] = as_of.isoformat()
    obs["_bar_index"] = bar_index
    obs["_code_sha"] = code_sha
    return obs
