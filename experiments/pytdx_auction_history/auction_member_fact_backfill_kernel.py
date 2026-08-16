"""Auction 120-Bar Backfill Kernel — Round 3B-D1.

正式 backfill 不再调用 ``fetch_full_day_transactions_paginated``（保留为
source-validation reference implementation）。本 kernel 只做两件事：

1. **targeted 09:25 source fetch**（``fetch_auction_0925_targeted``）：
   hint-first + local-bracket + boundary binary search，只覆盖 09:25 target
   minute，不扫描全天。
2. **纯函数 member fact builder**（``build_historical_member_fact``）：
   target source → canonicalization → Lane A → Lane B → Member Fact。

builder 是纯函数：不 new PytdxAdapter / 不调用 MDAS / 不查 DB / 不分页全天。
所有 MDAS 结果由调用方通过 preloaded batch 传入。

Round 3B-D1 transport fixes（不改 canonical business contract）：
- 真实 pytdx historical transaction time = HH:MM（如 "09:25"），kernel 曾按
  HH:MM:SS 处理导致窗口漏匹配。统一按 ``_normalize_source_minute()`` 归一化为
  HH:MM，transport search/ordering 全部基于 normalized minute。
- ``TARGET_MINUTE = "09:25"`` 冻结为 raw string ordering owner；
  不再以 "09:25:00" / "09:25:59" 作为排序 owner。
- non-empty page 无任何可解析 source time 时 fail closed：
  TARGET_SEARCH_STALLED + INVALID_OR_UNORDERABLE_SOURCE_TIME。
- page_count 只统计真实 adapter 请求（page_cache hit 不计数）。
- warm hint 走 local fast path（F1/F2/F3），稳定日 REAL requests <= 3。

source_status 冻结词表（backfill-specific，不谎称 full-day COMPLETE）：
- TARGET_WINDOW_COMPLETE      : 09:25 target minute 已被完整 bracket/覆盖（不含全天逐笔完整语义）
- SOURCE_EMPTY                : 全天无数据
- SOURCE_ERROR                : 源错误（managed retry 后仍失败）
- TARGET_SEARCH_STALLED       : 搜索停滞（重复 page / 无可解析 source time）
- TARGET_SEARCH_LIMIT_REACHED : 超出搜索请求预算
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from app.core.pytdx_adapter import PytdxAdapter

from auction_history_semantics_validation import (
    CANON_STATUS_CANONICAL,
    _normalize_raw_transaction,
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

# 09:25 target minute（开市竞价撮合发生在 09:25，次日 09:25 gap/amount 事实）。
# 冻结为 raw string ordering owner（Round 3B-D1 PART B）。
# 不再使用 TARGET_WINDOW_START = "09:25:00" / TARGET_WINDOW_END = "09:25:59"
# 作为 raw string ordering owner。
TARGET_MINUTE = "09:25"

# 硬性请求预算（per symbol）：单只股票搜索 + 边界读取的 REAL page 请求上限
MAX_TARGETED_PAGES = 24

# ---------------------------------------------------------------------------
# 内部异常（结构化错误，不进入业务 canonicalization）
# ---------------------------------------------------------------------------
class _SearchBudgetExceeded(RuntimeError):
    """超过 MAX_TARGETED_PAGES 请求预算 → TARGET_SEARCH_LIMIT_REACHED。"""


class _SearchStalled(RuntimeError):
    """搜索无进展（重复 page fingerprint）→ TARGET_SEARCH_STALLED。"""


class _UnorderableSourceTime(RuntimeError):
    """non-empty page 无可解析 source time → TARGET_SEARCH_STALLED。

    error_code / error_message = INVALID_OR_UNORDERABLE_SOURCE_TIME。
    不得把此类 page 假定为 before / after / empty。
    """


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


# ---------------------------------------------------------------------------
# Transport-level source time normalization（Round 3B-D1 PART B）
# ---------------------------------------------------------------------------
def _normalize_source_minute(value: Any) -> Optional[str]:
    """transport-level source time 归一化 → "HH:MM"；无法解析返回 None。

    语义：
      "09:25"    → "09:25"
      "09:25:00" → "09:25"
      "09:25:37" → "09:25"

    合法 HH:MM / HH:MM:SS（时钟合法）→ HH:MM。
    None / empty / malformed / invalid clock → None。

    只用于 transport search/ordering，不改写 raw source record。
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    if len(parts) == 3:
        try:
            ss = int(parts[2])
        except ValueError:
            return None
        if not (0 <= ss <= 59):
            return None
    return f"{hh:02d}:{mm:02d}"


def _in_window(t: Any) -> bool:
    """记录是否落在 09:25 target minute（PART D：normalized minute == TARGET_MINUTE）。"""
    m = _normalize_source_minute(t)
    return m is not None and m == TARGET_MINUTE


def _page_content_fingerprint(page: list[dict]) -> str:
    """内容级 page 指纹（Round 3B-D1）：检测「不同 offset 返回字面相同内容」的搜索停滞。

    semantics 模块的 ``_page_fingerprint`` 只取首尾 time + 长度；真实 pytdx 数据中
    连续 page 可能整页落在同一分钟（如长 09:30 块），首尾 time 相同会产生相同指纹，
    导致真实数据误判 TARGET_SEARCH_STALLED。这里对整页 (time, price, vol, buyorsell)
    做内容哈希：不同 offset 的合法 page（即使同分钟）内容必然不同 → 指纹不同；
    仅在 adapter 对不同 offset 返回字面相同内容（病态重复）时触发停滞。

    只用于 transport 搜索停滞检测，不改变 canonical business contract。
    """
    if not page:
        return "EMPTY"
    h = hashlib.sha1()
    for r in page:
        h.update(
            repr((r.get("time"), r.get("price"), r.get("vol"),
                  r.get("buyorsell"))).encode("utf-8", "replace")
        )
        h.update(b"\n")
    return h.hexdigest()


def _page_time_range(page: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """page 内可解析 source times 的 (min, max)（normalized "HH:MM"）。

    基于 ``_normalize_source_minute()`` 排序（Round 3B-D1 PART C）。
    正确语义：
      - page entirely before target：max(valid minute) < "09:25"
      - page entirely after  target：min(valid minute) > "09:25"
      - contains target minute：min <= "09:25" <= max
    non-empty page 无任何可解析 source time → fail closed（抛
    ``_UnorderableSourceTime``），不得假定 before / after / empty。
    空页返回 (None, None)。
    """
    if not page:
        return None, None
    minutes = [_normalize_source_minute(r.get("time")) for r in page]
    valid = [m for m in minutes if m is not None]
    if not valid:
        raise _UnorderableSourceTime()
    return min(valid), max(valid)


def _page_entirely_before(page: list[dict]) -> bool:
    """整页记录都早于 target minute（max(valid minute) < TARGET_MINUTE）。"""
    _, mx = _page_time_range(page)
    return mx is not None and mx < TARGET_MINUTE


def _page_entirely_after(page: list[dict]) -> bool:
    """整页记录都晚于 target minute（min(valid minute) > TARGET_MINUTE）。"""
    mn, _ = _page_time_range(page)
    return mn is not None and mn > TARGET_MINUTE


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
    """找到并完整覆盖 09:25 target minute 的 source records。

    算法（不线性扫全天）：
      - cold（无 hint）：offset=0 探针 → exponential search → boundary binary search；
      - warm（有 hint）：从上一交易日 resolved offset 附近做局部 bracketing
        （F1/F2/F3），稳定日典型 <=3 次 REAL page request；hint 漂移时仅局部
        扩展/回退，禁止重置 offset=0 全局搜索；
      - 读取 boundary 相邻 3 页（B-2P / B-P / B）按 raw record identity 去重。

    offset_hint 只影响 transport search efficiency，不改变 source result。
    只有 source_status == TARGET_WINDOW_COMPLETE 才允许 canonicalize。

    page_count 只统计 REAL adapter page calls（page_cache hit 不计数，
    Round 3B-D1 PART E）。
    """
    counter: dict[str, int] = {"n": 0}
    page_cache: dict[int, list[dict]] = {}
    seen_fp: set[str] = set()

    def _fetch(offset: int) -> list[dict]:
        # PART E：cache hit 先返回，不占用请求预算、不计 page_count。
        if offset in page_cache:
            return page_cache[offset]
        counter["n"] += 1
        if counter["n"] > max_pages:
            raise _SearchBudgetExceeded()
        try:
            page = adapter.get_history_transaction_page(
                symbol, trade_date, offset, page_size
            )
        except RuntimeError as exc:
            raise _SourceError(str(exc)) from exc
        page = list(page) if page else []
        page_cache[offset] = page
        if page:
            fp = _page_content_fingerprint(page)
            if fp in seen_fp:
                raise _SearchStalled()
            seen_fp.add(fp)
        return page

    def _before(offset: int) -> bool:
        """offset 处 page 为空（exhausted）或整页早于 target minute。

        空页 → True（source exhausted，构成 boundary）。
        non-empty page 无可解析 source time → 抛 _UnorderableSourceTime（fail closed）。
        """
        page = _fetch(offset)
        if not page:
            return True
        _, mx = _page_time_range(page)
        return mx < TARGET_MINUTE

    def _locate_boundary_cold() -> int:
        """cold：offset=0 探针 + exponential + boundary binary search。"""
        lo, hi = 0, page_size
        while not _before(hi):
            lo, hi = hi, hi * 2
        return _locate_boundary_binary(lo, hi)

    def _locate_boundary_binary(lo: int, hi: int) -> int:
        """boundary binary search：最小 offset 使 _before(offset) 为 True。

        invariant: _before(lo) == False, _before(hi) == True, hi > lo（页对齐）。
        """
        while hi - lo > page_size:
            mid = ((lo // page_size) + (hi // page_size)) // 2 * page_size
            if _before(mid):
                hi = mid
            else:
                lo = mid
        return hi

    def _locate_boundary_warm(hint: int) -> Optional[int]:
        """warm：hint-first local bracketing（Round 3B-D1 PART F）。

        F1 — 不从 offset=0 无条件探针；从 hint 页开始。
        F2 — local bracket：
          Fast path A：_before(H) 且 _before(H-P) == False → boundary = H
                       （hint 准确；覆盖上一交易日 boundary）
          Fast path B：_before(H) == False 且 _before(H+P) → boundary = H+P
                       （hint 少一页；本日 ticks 略多）
          正常 REAL requests 总数 <= 3（H、H±P、window 收集新增一页）。
        F3 — hint drift fallback：从 H 向「偏移方向」局部扩展（H±P、H±2P、
             H±4P…），建立局部 bracket 后仅在该 bracket 内 binary search。
            禁止一发现 hint 偏差就重置 offset=0 全局 binary search。
            仅当 hint 页为空且 offset=0 也空（SOURCE_EMPTY → None）时才终止。
        返回 boundary；返回 None 表示当天无数据（SOURCE_EMPTY）。
        """
        H = (hint // page_size) * page_size
        P = page_size

        # F2 Fast path A：hint 准确 → boundary == H
        if _before(H) and (H - P < 0 or not _before(H - P)):
            return H

        # F2 Fast path B：hint 少一页 → boundary == H+P
        if not _before(H) and _before(H + P):
            return H + P

        # F3 drift fallback：局部扩展，不重置 offset=0
        if _before(H):
            # boundary 在 [0, H-P] 内（hint 偏大：本日 ticks 较少）。
            hi = H
            lo = max(0, H - P)
            step = P
            while lo > 0 and _before(lo):
                hi = lo
                lo = max(0, lo - step)
                step *= 2
            if _before(lo):
                # lo == 0 且仍 before → 全天数据都在 target 前 → boundary = 0
                return 0
            return _locate_boundary_binary(lo, hi)
        else:
            # boundary 在 (H+P, +∞) 内（hint 偏小：本日 ticks 明显更多）。
            lo = H
            hi = H + P
            step = P
            while not _before(hi):
                lo = hi
                hi = hi + step
                step *= 2
            return _locate_boundary_binary(lo, hi)

    # --- 第一步：cold 必须 offset=0 探针；warm 不探 offset=0（F1）---
    try:
        if offset_hint is None or offset_hint < 0:
            first_page = _fetch(0)
            if not first_page:
                return Targeted0925Result(source_status=SOURCE_EMPTY,
                                          page_count=counter["n"])
            if _page_entirely_before(first_page):
                # 罕见：全天最后记录也早于 09:25 → window 无记录但已完整覆盖
                return Targeted0925Result(
                    source_status=SOURCE_TARGET_WINDOW_COMPLETE,
                    records=[], page_count=counter["n"], resolved_offset=0,
                    used_hint=False)
    except _UnorderableSourceTime:
        return Targeted0925Result(
            source_status=SOURCE_TARGET_SEARCH_STALLED,
            page_count=counter["n"],
            error_code="INVALID_OR_UNORDERABLE_SOURCE_TIME",
            error_message="non-empty page has no parseable source time")
    except _SourceError as exc:
        return Targeted0925Result(
            source_status=SOURCE_ERROR, page_count=counter["n"],
            error_code=type(exc).__name__, error_message=str(exc))
    except _SearchBudgetExceeded:
        return Targeted0925Result(
            source_status=SOURCE_TARGET_SEARCH_LIMIT_REACHED,
            page_count=counter["n"])

    # --- 第二步：定位边界 B ---
    try:
        if offset_hint is not None and offset_hint >= 0:
            boundary = _locate_boundary_warm(offset_hint)
            if boundary is None:
                return Targeted0925Result(source_status=SOURCE_EMPTY,
                                          page_count=counter["n"])
        else:
            boundary = _locate_boundary_cold()
    except _UnorderableSourceTime:
        return Targeted0925Result(
            source_status=SOURCE_TARGET_SEARCH_STALLED,
            page_count=counter["n"],
            error_code="INVALID_OR_UNORDERABLE_SOURCE_TIME",
            error_message="non-empty page has no parseable source time")
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

    # --- 第三步：读取 boundary 相邻 3 页（B-2P / B-P / B）去重收集 window ---
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
    except _SearchStalled:
        return Targeted0925Result(
            source_status=SOURCE_TARGET_SEARCH_STALLED,
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
    """读取 boundary 相邻 3 页（B-2P / B-P / B），按 raw record identity 去重。

    boundary B = 最小 offset 其 page 已在 target minute 之前或 source exhausted。
    window（09:25 minute）records 位于 B-P / B-2P（boundary 前 1～2 个数据页），
    收集 B-2P / B-P / B 保证 09:25 records 跨 page boundary 不漏（PART G）。
    raw identity = (time, price, vol, buyorsell)。
    """
    seen_rows: set[tuple] = set()
    out: list[dict] = []

    def _take(page: list[dict]) -> None:
        for r in page:
            key = (r.get("time"), r.get("price"), r.get("vol"),
                   r.get("buyorsell"))
            if key in seen_rows:
                continue
            seen_rows.add(key)
            if _in_window(r.get("time")):
                out.append(r)

    for off in (boundary - 2 * page_size, boundary - page_size, boundary):
        if off >= 0:
            _take(fetch_fn(off))
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
