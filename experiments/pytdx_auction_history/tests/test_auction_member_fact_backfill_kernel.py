"""Kernel tests — targeted 09:25 source search + canonicalization gate.

Round 3B-D1（boundary-first cold path）+ Round 3B-D2（target-page-first warm path）：
- Real pytdx source shape: historical transaction ``time`` is HH:MM（如 "09:25"），
  kernel 按 ``_normalize_source_minute()`` 归一化为 HH:MM 后做 transport search/ordering，
  不修改 canonical business contract（仍接受 "09:25" / "09:25:00"）。

Round 3B-D1 PART I T1–T10 + canonicalization gate：
- T1 REAL_HHMM_CANONICAL              真实 HH:MM 正 volume fixture 必须 CANONICAL
- T2 HHMM_BOUNDARY                    09:30/09:25/09:24 minute ordering 正确，09:25 页不得判 entirely_before
- T3 HHMM_CROSS_PAGE                  09:25 records 跨两个 page 完整且不重复
- T4 HHMMSS_COMPATIBILITY             09:25:00 仍被现有 semantics contract 接受
- T5 CACHE_NOT_REQUEST                page_count == len(adapter.request_log)；重复 offset 不重复计数
- T6 COLD_EQUALS_WARM                 real-style HH:MM pages：cold records == warm records
- T7 EXACT_BOUNDARY_HINT              offset_hint == prior boundary → REAL page calls <= 3
- T8 ONE_PAGE_SHIFT_HINT              offset_hint == boundary-P → <= 3；== boundary+P → 局部 fast path
- T9 LARGE_DRIFT_SAFE                 hint 漂移 >1 page：允许 >3，但 records == cold 且 <= MAX_TARGETED_PAGES
- T10 CANONICALIZATION_NOT_100_PERCENT_NO_VOLUME  真实 source shape 下必须得到 CANONICAL（非全部 NO_VOLUME）
- canonicalize_only_when_complete     仅 TARGET_WINDOW_COMPLETE 才允许 canonicalize
- source_empty_not_business_zero      source empty → SOURCE_EMPTY / price=None

Round 3B-D2 PART H T1–T8（target-page-first warm path）：
- D2-T1 SINGLE        单页内 09:24/09:25/09:26 → 1 request + TARGET_PAGE_SINGLE
- D2-T2 CROSSES       页跨过 09:25 但无 record → 1 request + records=[] + TARGET_WINDOW_COMPLETE
- D2-T3 EDGE          target 在 lower/upper 边沿 → adjacent page → 2 requests + records 完整
- D2-T4 BIDIRECTIONAL 整页 ==09:25 → 两侧 adjacent → <=3 requests + records 完整
- D2-T5 +1 DRIFT      页 entirely after → probe H+P → 2 requests
- D2-T6 -1 DRIFT      页 entirely before → probe H-P → 2 requests
- D2-T7 LARGE DRIFT   大漂移 → existing boundary fallback → result == cold result
- D2-T8 CACHE HINT    fallback 后 target_page_offset 来自已有 cache，不产生额外 request

Mock 数据布局（pytdx reverse offset 语义）：
- offset 0 = 当日最新 ticks（收盘），offset 增大 = 更早 ticks；
- targeted search 的 boundary = 首个「整页早于 09:25 或空页」的 offset；
- window（09:25 minute）records 位于 boundary 前 1～2 个数据页（B-P / B-2P），
  collection 固定读 B-2P / B-P / B 并按 raw identity 去重（PART G）。
- Round 3B-D2 warm：hint 优先表示上一交易日 target_page_offset（含/跨越 09:25 的
  page），fast path 从 H 页做 SINGLE/ADJACENT/BIDIRECTIONAL/DRIFT 局部判定，
  无法 complete 才进入 existing boundary fallback。
"""

from __future__ import annotations

import importlib.util
import math
import sys
from datetime import date
from pathlib import Path
from uuid import UUID

# ---------------------------------------------------------------------------
# 模块加载（与 tests/test_auction_history_semantics_validation.py 一致的
# importlib 绝对路径加载，不依赖 cwd；kernel 内部依赖
# `from auction_history_semantics_validation import ...`，故先注册语义模块）
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent.parent


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SEM = _load_module(
    "auction_history_semantics_validation",
    _HERE / "auction_history_semantics_validation.py",
)
KERNEL = _load_module(
    "auction_member_fact_backfill_kernel",
    _HERE / "auction_member_fact_backfill_kernel.py",
)

PAGE_SIZE = SEM.PAGE_SIZE
TARGET_MINUTE = KERNEL.TARGET_MINUTE
MAX_TARGETED_PAGES = KERNEL.MAX_TARGETED_PAGES

TRADE_DATE = date(2026, 8, 14)
INST = SEM.SampleInstrument(
    symbol="000001",
    market="SZ",
    instrument_id=UUID("00000000-0000-0000-0000-000000000001"),
    board="主板",
    coverage_tag="all_a_share",
    cohort="routine",
)

SOURCE_TARGET_WINDOW_COMPLETE = KERNEL.SOURCE_TARGET_WINDOW_COMPLETE
SOURCE_EMPTY = KERNEL.SOURCE_EMPTY
SOURCE_ERROR = KERNEL.SOURCE_ERROR
SOURCE_TARGET_SEARCH_STALLED = KERNEL.SOURCE_TARGET_SEARCH_STALLED
SOURCE_TARGET_SEARCH_LIMIT_REACHED = KERNEL.SOURCE_TARGET_SEARCH_LIMIT_REACHED


# ---------------------------------------------------------------------------
# Fakes（真实 HH:MM source shape）
# ---------------------------------------------------------------------------
def _tick(ts: str, price: float, vol: float, buyorsell: int = 0) -> dict:
    return {"time": ts, "price": float(price), "vol": float(vol),
            "buyorsell": buyorsell}


def _build_full_day_pages_real(total: int = 4000, page_size: int = 800,
                               auction_n: int = 60) -> dict[int, list[dict]]:
    """构造真实 HH:MM 格式多页完整日数据（reverse order，window 位于数据尾部）。

    chrono = auction(全部 time=="09:25"，price/vol 各异，auction_n 条)
             + post(09:26 起 N-auction_n 条，price/vol 各异)
    reverse 后 window 位于 reverse list 末尾 → targeted search 的 boundary
    （首个空页）恰好在 window 之后，window 落在 boundary 前的最后数据页内。
    构造 distinct price/vol 避免 synthetic duplicate 被 raw identity 去重。
    """
    auction = [
        _tick("09:25", 10.0 + i * 0.001, 100.0 + i, buyorsell=0)
        for i in range(auction_n)
    ]
    post = [
        _tick("09:26", 10.5 + i * 0.001, 200.0 + i, buyorsell=1)
        for i in range(total - auction_n)
    ]
    chrono = auction + post
    rev = chrono[::-1]
    pages = {}
    for off in range(0, len(rev), page_size):
        pages[off] = rev[off:off + page_size]
    return pages


class FakePytdxAdapter:
    """纯内存 mock adapter：按 offset 返回预定义页面，记录每次 REAL page request。

    pages: dict[offset(int), list[dict]]；未定义的 offset 返回空页（= 全天结束）。
    """

    def __init__(self, pages):
        self._pages = {int(k): list(v) for k, v in pages.items()}
        self.request_log = []  # list[(offset, count)]

    def get_history_transaction_page(self, symbol, trade_date, offset, count):
        self.request_log.append((offset, count))
        return list(self._pages.get(int(offset), []))

    @property
    def page_count(self) -> int:
        return len(self.request_log)


def _fetch_0925(pages, offset_hint=None):
    adapter = FakePytdxAdapter(pages)
    result = KERNEL.fetch_auction_0925_targeted(
        adapter, INST.symbol, TRADE_DATE, offset_hint=offset_hint)
    return adapter, result


def _records_keys(records):
    return [
        (r["time"], r["price"], r["vol"], r["buyorsell"])
        for r in records
    ]


# ---------------------------------------------------------------------------
# T1 — REAL_HHMM_CANONICAL：真实 HH:MM 正 volume fixture 必须 CANONICAL
# ---------------------------------------------------------------------------
def test_t1_real_hhmm_canonical():
    pages = {0: [_tick("09:25", 10.0, 100.0)]}

    adapter, result = _fetch_0925(pages)
    assert result.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert _records_keys(result.records) == [("09:25", 10.0, 100.0, 0)]

    layer = KERNEL.canonicalize_targeted_window(INST, TRADE_DATE, result)
    assert layer["canonicalization_status"] == SEM.CANON_STATUS_CANONICAL
    assert layer["auction_price_raw"] == 10.0
    assert layer["auction_volume_raw_lots"] == 100.0
    assert layer["auction_volume_shares"] == 100.0 * SEM.AUCTION_LOT_MULTIPLIER
    assert layer["auction_amount"] == 10.0 * 100.0 * SEM.AUCTION_LOT_MULTIPLIER
    # page_count == REAL adapter calls
    assert result.page_count == adapter.page_count == len(adapter.request_log)


# ---------------------------------------------------------------------------
# T2 — HHMM_BOUNDARY：09:30/09:25/09:24 minute ordering 正确，09:25 页不得判 entirely_before
# ---------------------------------------------------------------------------
def test_t2_hhmm_boundary_ordering():
    page = [
        _tick("09:30", 10.0, 100.0),
        _tick("09:25", 10.0, 100.0),
        _tick("09:24", 10.0, 100.0),
    ]
    # normalized minute ordering 必须正确
    mn, mx = KERNEL._page_time_range(page)
    assert mn == "09:24"
    assert mx == "09:30"
    # 含 09:25 的 page 不得被判 entirely_before / entirely_after
    assert KERNEL._page_entirely_before(page) is False
    assert KERNEL._page_entirely_after(page) is False
    assert KERNEL._in_window("09:25") is True
    assert KERNEL._in_window("09:30") is False
    assert KERNEL._in_window("09:24") is False

    # 与 HH:MM:SS 混合：normalized minute 排序不受秒级精度影响
    mixed = [
        _tick("09:30", 10.0, 100.0),
        _tick("09:25:37", 10.0, 100.0),
    ]
    mn2, mx2 = KERNEL._page_time_range(mixed)
    assert mn2 == "09:25" and mx2 == "09:30"
    assert KERNEL._page_entirely_before(mixed) is False
    assert KERNEL._normalize_source_minute("09:25:37") == "09:25"


# ---------------------------------------------------------------------------
# T3 — HHMM_CROSS_PAGE：09:25 records 跨两个 page 仍完整且不重复
# ---------------------------------------------------------------------------
def test_t3_hhmm_cross_page():
    # auction 900 条（time 全部 "09:25"，price/vol 各异）跨 rev 最后两页
    pages = _build_full_day_pages_real(total=4000, auction_n=900)
    # 900 auction 记录（distinct price/vol）
    auction = [_tick("09:25", 10.0 + i * 0.001, 100.0 + i, 0)
               for i in range(900)]

    adapter, result = _fetch_0925(pages)
    assert result.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert len(result.records) == 900
    assert len(set(_records_keys(result.records))) == 900  # 无重复（跨页去重生效）
    # 与 cold 期望的 auction 记录集合一致（价格/成交量递增分布）
    got = sorted(_records_keys(result.records))
    exp = sorted(_records_keys(auction))
    assert got == exp


# ---------------------------------------------------------------------------
# T4 — HHMMSS_COMPATIBILITY：09:25:00 仍被现有 semantics contract 接受
# ---------------------------------------------------------------------------
def test_t4_hhmmss_compatibility():
    assert KERNEL._normalize_source_minute("09:25:00") == "09:25"
    pages = {0: [_tick("09:25:00", 10.0, 100.0)]}

    _, result = _fetch_0925(pages)
    layer = KERNEL.canonicalize_targeted_window(INST, TRADE_DATE, result)
    assert layer["canonicalization_status"] == SEM.CANON_STATUS_CANONICAL
    assert layer["auction_price_raw"] == 10.0
    assert layer["auction_volume_shares"] == 100.0 * SEM.AUCTION_LOT_MULTIPLIER


# ---------------------------------------------------------------------------
# T5 — CACHE_NOT_REQUEST：page_count == len(adapter.request_log)；重复 offset 不重复计数
# ---------------------------------------------------------------------------
def test_t5_cache_not_request():
    pages = _build_full_day_pages_real(total=4000, auction_n=60)

    cold_adapter, cold = _fetch_0925(pages)
    assert cold.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    # page_count 必须 == REAL adapter calls
    assert cold.page_count == cold_adapter.page_count == len(cold_adapter.request_log)

    # Round 3B-D2：warm hint=cold.resolved_offset（boundary）→ target-page-first 下
    # H 页为空 → DRIFT 向 newer 局部扩展（H-P / H-2P）→ 2 REAL requests。
    # page_count 只统计真实 adapter 请求；缓存命中（重复 offset）不重复计数。
    warm_adapter, warm = _fetch_0925(pages, offset_hint=cold.resolved_offset)
    assert warm.page_count == warm_adapter.page_count == len(warm_adapter.request_log)
    assert warm.page_count == 2
    assert warm.search_mode == KERNEL.SEARCH_MODE_TARGET_PAGE_DRIFT
    assert len(set(warm_adapter.request_log)) == len(warm_adapter.request_log)
    assert _records_keys(warm.records) == _records_keys(cold.records)


# ---------------------------------------------------------------------------
# T6 — COLD_EQUALS_WARM：real-style HH:MM pages 下 cold records == warm records
# ---------------------------------------------------------------------------
def test_t6_cold_equals_warm():
    pages = _build_full_day_pages_real(total=4000, auction_n=60)

    cold_adapter, cold = _fetch_0925(pages)
    warm_adapter, warm = _fetch_0925(pages, offset_hint=cold.resolved_offset)

    assert cold.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert warm.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert _records_keys(cold.records) == _records_keys(warm.records)


# ---------------------------------------------------------------------------
# T7 — EXACT_BOUNDARY_HINT：offset_hint == prior boundary → REAL page calls <= 3
# ---------------------------------------------------------------------------
def test_t7_exact_boundary_hint():
    pages = _build_full_day_pages_real(total=4000, auction_n=60)

    _, cold = _fetch_0925(pages)
    assert cold.resolved_offset is not None
    boundary = cold.resolved_offset

    warm_adapter, warm = _fetch_0925(pages, offset_hint=boundary)
    assert warm.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert warm.used_hint is True
    assert warm.page_count <= 3
    assert warm_adapter.page_count == warm.page_count


# ---------------------------------------------------------------------------
# T8 — ONE_PAGE_SHIFT_HINT：offset_hint == boundary-P → <= 3；== boundary+P → 局部 fast path
# ---------------------------------------------------------------------------
def test_t8_one_page_shift_hint():
    pages = _build_full_day_pages_real(total=4000, auction_n=60)

    _, cold = _fetch_0925(pages)
    boundary = cold.resolved_offset

    # hint == boundary-P（fast path B：boundary = H+P）→ <= 3
    a1, r1 = _fetch_0925(pages, offset_hint=boundary - PAGE_SIZE)
    assert r1.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert r1.page_count <= 3
    assert a1.page_count == r1.page_count
    assert _records_keys(r1.records) == _records_keys(cold.records)

    # hint == boundary+P：F3 局部向后扩展（H、H-P、H-2P）→ 3 次搜索 + PART G 强制
    # B-2P 完整性页 → <= 4。仍是局部 fast path（不重置 offset=0），records 与 cold 一致。
    a2, r2 = _fetch_0925(pages, offset_hint=boundary + PAGE_SIZE)
    assert r2.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert r2.page_count <= 4, (
        "boundary+P 需额外读取 PART G 完整性页 B-2P，最多 4 次 REAL requests")
    assert a2.page_count == r2.page_count
    assert 0 not in [o for o, _ in a2.request_log], (
        "warm hint 不得无条件 offset=0 探针（F1）")
    assert _records_keys(r2.records) == _records_keys(cold.records)


# ---------------------------------------------------------------------------
# T9 — LARGE_DRIFT_SAFE：hint 漂移 >1 page：允许 >3，但 records == cold 且 <= MAX_TARGETED_PAGES
# ---------------------------------------------------------------------------
def test_t9_large_drift_safe():
    pages = _build_full_day_pages_real(total=4000, auction_n=60)

    _, cold = _fetch_0925(pages)
    boundary = cold.resolved_offset

    # 大幅漂移：hint 指向 boundary 前方 3 页 → target-page fast path 无法 complete，
    # 进入 existing boundary fallback（Round 3B-D2 PART D）。
    drift = boundary + 3 * PAGE_SIZE
    a, r = _fetch_0925(pages, offset_hint=drift)
    assert r.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert _records_keys(r.records) == _records_keys(cold.records)
    assert r.page_count <= MAX_TARGETED_PAGES
    assert r.search_mode == KERNEL.SEARCH_MODE_BOUNDARY_FALLBACK
    # 大漂移时 fast path 全部探针为空 → 允许 offset=0 判定 SOURCE_EMPTY（非重置搜索，
    # 仅空数据 discrimination）；result 仍与 cold 完全一致。
    assert r.target_page_offset is not None
    assert r.target_page_offset in {o for o, _ in a.request_log}


# ---------------------------------------------------------------------------
# T10 — CANONICALIZATION_NOT_100_PERCENT_NO_VOLUME：真实 source shape 必须 CANONICAL
# ---------------------------------------------------------------------------
def test_t10_real_style_positive_fixture_canonical():
    # 多页真实 HH:MM fixture：09:25 window 内恰含一条正 volume 记录 → 必须 CANONICAL
    # （其余正量记录在 09:26，不在 09:25 window，不参与 canonicalization）。
    pages = _build_full_day_pages_real(total=4000, auction_n=1)

    _, result = _fetch_0925(pages)
    assert result.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    layer = KERNEL.canonicalize_targeted_window(INST, TRADE_DATE, result)
    assert layer["canonicalization_status"] == SEM.CANON_STATUS_CANONICAL
    assert layer["positive_volume_record_count"] > 0
    assert layer["auction_volume_shares"] > 0


# ---------------------------------------------------------------------------
# canonicalization gate — 仅 TARGET_WINDOW_COMPLETE 才允许 canonicalize
# ---------------------------------------------------------------------------
def test_canonicalize_only_when_complete():
    for status in (SOURCE_EMPTY, SOURCE_ERROR,
                   SOURCE_TARGET_SEARCH_STALLED,
                   SOURCE_TARGET_SEARCH_LIMIT_REACHED):
        targeted = KERNEL.Targeted0925Result(source_status=status, records=[])
        layer = KERNEL.canonicalize_targeted_window(INST, TRADE_DATE, targeted)
        assert layer["source_status"] == status
        assert layer["extraction_status"] == status
        assert layer["canonicalization_status"] is None
        assert layer["canonicalization_reason"] == "SOURCE_WINDOW_INCOMPLETE"
        assert layer["auction_price_raw"] is None

    # 反向对照：TARGET_WINDOW_COMPLETE 才允许 canonicalize
    complete = KERNEL.Targeted0925Result(
        source_status=SOURCE_TARGET_WINDOW_COMPLETE,
        records=[{"time": "09:25", "price": 10.0, "vol": 100.0,
                  "buyorsell": 0}],
    )
    layer = KERNEL.canonicalize_targeted_window(INST, TRADE_DATE, complete)
    assert layer["canonicalization_status"] == SEM.CANON_STATUS_CANONICAL
    assert layer["auction_price_raw"] == 10.0
    assert layer["auction_volume_shares"] == 100.0 * SEM.AUCTION_LOT_MULTIPLIER


# ---------------------------------------------------------------------------
# source empty → SOURCE_EMPTY / price=None（非 0 / NaN）
# ---------------------------------------------------------------------------
def test_source_empty_not_business_zero():
    adapter = FakePytdxAdapter({})  # 全天无数据

    result = KERNEL.fetch_auction_0925_targeted(
        adapter, INST.symbol, TRADE_DATE)

    assert result.source_status == SOURCE_EMPTY
    assert result.records == []
    assert result.page_count == 1

    layer = KERNEL.canonicalize_targeted_window(INST, TRADE_DATE, result)
    assert layer["canonicalization_status"] is None
    raw_price = layer["auction_price_raw"]
    assert raw_price is None  # 不是 0，也不是 NaN
    assert raw_price != 0
    assert not (isinstance(raw_price, float) and math.isnan(raw_price))
    assert layer["auction_volume_raw_lots"] is None
    assert layer["auction_volume_shares"] is None
    assert layer["auction_amount"] is None


# ===========================================================================
# Round 3B-D2 PART H T1–T8 — target-page-first warm path
# ===========================================================================

# ---------------------------------------------------------------------------
# D2-T1 — SINGLE：单页内 09:24/09:25/09:26 → 1 request + TARGET_PAGE_SINGLE
# ---------------------------------------------------------------------------
def test_d2_t1_single_page_three_minutes():
    H = PAGE_SIZE
    pages = {
        H: [
            _tick("09:26", 10.6, 200.0),   # after target minute
            _tick("09:25", 10.0, 100.0),   # target
            _tick("09:24", 9.9, 50.0),     # before target minute
        ],
    }
    a, r = _fetch_0925(pages, offset_hint=H)
    assert r.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert r.page_count == 1
    assert r.search_mode == KERNEL.SEARCH_MODE_TARGET_PAGE_SINGLE
    assert _records_keys(r.records) == [("09:25", 10.0, 100.0, 0)]
    assert r.target_page_offset == H
    assert r.used_hint is True
    assert a.page_count == r.page_count


# ---------------------------------------------------------------------------
# D2-T2 — CROSSES：页跨过 09:25 但无 record → 1 request + records=[] + COMPLETE
# ---------------------------------------------------------------------------
def test_d2_t2_page_crosses_but_no_record():
    H = PAGE_SIZE
    pages = {
        H: [
            _tick("09:26", 10.6, 200.0),   # after
            _tick("09:24", 9.9, 50.0),     # before（中间跨过 09:25，无 09:25 record）
        ],
    }
    a, r = _fetch_0925(pages, offset_hint=H)
    assert r.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert r.page_count == 1
    assert r.search_mode == KERNEL.SEARCH_MODE_TARGET_PAGE_SINGLE
    assert r.records == []
    assert r.target_page_offset == H

    # 合法 non-CANONICAL：空 window → NO_VOLUME_BEARING_0925，不是 bug 也不谎称 CANONICAL
    layer = KERNEL.canonicalize_targeted_window(INST, TRADE_DATE, r)
    assert layer["canonicalization_status"] == SEM.CANON_STATUS_NO_VOLUME_BEARING
    assert layer["auction_price_raw"] is None


# ---------------------------------------------------------------------------
# D2-T3 — EDGE：target 在 lower/upper 边沿 → adjacent page → 2 requests + 完整
# ---------------------------------------------------------------------------
def test_d2_t3_adjacent_edge():
    # lower edge：page 起点即 09:25（mn==09:25, mx>09:25）→ 09:25 block 向 older 延续
    H = PAGE_SIZE
    pages_lower = {
        H: [_tick("09:26", 10.6, 200.0), _tick("09:25", 10.0, 100.0)],
        H + PAGE_SIZE: [_tick("09:25", 10.1, 150.0), _tick("09:24", 9.9, 50.0)],
    }
    a1, r1 = _fetch_0925(pages_lower, offset_hint=H)
    assert r1.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert r1.page_count == 2
    assert r1.search_mode == KERNEL.SEARCH_MODE_TARGET_PAGE_ADJACENT
    assert _records_keys(r1.records) == [
        ("09:25", 10.0, 100.0, 0), ("09:25", 10.1, 150.0, 0)]

    # upper edge：page 终点即 09:25（mn<09:25, mx==09:25）→ 09:25 block 向 newer 延续
    H2 = 2 * PAGE_SIZE
    pages_upper = {
        H2: [_tick("09:25", 10.0, 100.0), _tick("09:24", 9.9, 50.0)],
        H2 - PAGE_SIZE: [_tick("09:26", 10.6, 200.0), _tick("09:25", 10.1, 150.0)],
    }
    a2, r2 = _fetch_0925(pages_upper, offset_hint=H2)
    assert r2.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert r2.page_count == 2
    assert r2.search_mode == KERNEL.SEARCH_MODE_TARGET_PAGE_ADJACENT
    assert sorted(_records_keys(r2.records)) == sorted([
        ("09:25", 10.0, 100.0, 0), ("09:25", 10.1, 150.0, 0)])
    assert a1.page_count == r1.page_count and a2.page_count == r2.page_count


# ---------------------------------------------------------------------------
# D2-T4 — BIDIRECTIONAL：整页 ==09:25 → 两侧 adjacent → <=3 requests + 完整
# ---------------------------------------------------------------------------
def test_d2_t4_whole_page_bidirectional():
    H = PAGE_SIZE
    pages = {
        H: [_tick("09:25", 10.0, 100.0), _tick("09:25", 10.1, 120.0)],
        H + PAGE_SIZE: [_tick("09:25", 10.2, 130.0), _tick("09:24", 9.9, 50.0)],
        H - PAGE_SIZE: [_tick("09:26", 10.6, 200.0), _tick("09:25", 10.3, 140.0)],
    }
    a, r = _fetch_0925(pages, offset_hint=H)
    assert r.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert r.page_count <= 3
    assert r.search_mode == KERNEL.SEARCH_MODE_TARGET_PAGE_BIDIRECTIONAL
    assert len(_records_keys(r.records)) == 4  # 三页 09:25 合并去重
    assert r.used_hint is True
    assert a.page_count == r.page_count


# ---------------------------------------------------------------------------
# D2-T5 — +1 DRIFT：H 整页 after（mn>09:25）→ target 更 old → probe H+P → 2 requests
# ---------------------------------------------------------------------------
def test_d2_t5_plus_one_page_drift():
    H = PAGE_SIZE
    pages = {
        H: [_tick("09:26", 10.6, 200.0)],                       # entirely after
        H + PAGE_SIZE: [_tick("09:25", 10.0, 100.0), _tick("09:24", 9.9, 50.0)],
    }
    a, r = _fetch_0925(pages, offset_hint=H)
    assert r.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert r.page_count == 2
    assert r.search_mode == KERNEL.SEARCH_MODE_TARGET_PAGE_DRIFT
    assert _records_keys(r.records) == [("09:25", 10.0, 100.0, 0)]
    assert a.page_count == r.page_count


# ---------------------------------------------------------------------------
# D2-T6 — -1 DRIFT：H 整页 before（mx<09:25）→ target 更 new → probe H-P → 2 requests
# ---------------------------------------------------------------------------
def test_d2_t6_minus_one_page_drift():
    H = 2 * PAGE_SIZE
    pages = {
        H: [_tick("09:24", 9.9, 50.0)],                         # entirely before
        H - PAGE_SIZE: [_tick("09:25", 10.0, 100.0), _tick("09:26", 10.6, 200.0)],
    }
    a, r = _fetch_0925(pages, offset_hint=H)
    assert r.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert r.page_count == 2
    assert r.search_mode == KERNEL.SEARCH_MODE_TARGET_PAGE_DRIFT
    assert _records_keys(r.records) == [("09:25", 10.0, 100.0, 0)]
    assert a.page_count == r.page_count


# ---------------------------------------------------------------------------
# D2-T7 — LARGE DRIFT：大漂移 → existing boundary fallback → result == cold result
# ---------------------------------------------------------------------------
def test_d2_t7_large_drift_fallback():
    pages = _build_full_day_pages_real(total=4000, auction_n=60)

    _, cold = _fetch_0925(pages)
    drift = cold.resolved_offset + 3 * PAGE_SIZE
    a, r = _fetch_0925(pages, offset_hint=drift)
    assert r.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert _records_keys(r.records) == _records_keys(cold.records)
    assert r.page_count <= MAX_TARGETED_PAGES
    assert r.search_mode == KERNEL.SEARCH_MODE_BOUNDARY_FALLBACK


# ---------------------------------------------------------------------------
# D2-T8 — CACHE HINT：fallback 后 target_page_offset 来自已有 cache，不产生额外 request
# ---------------------------------------------------------------------------
def test_d2_t8_fallback_hint_from_cache():
    pages = _build_full_day_pages_real(total=4000, auction_n=60)

    _, cold = _fetch_0925(pages)
    drift = cold.resolved_offset + 3 * PAGE_SIZE
    a, r = _fetch_0925(pages, offset_hint=drift)
    assert r.page_count == a.page_count == len(a.request_log)
    # target_page_offset 必须来自本次已请求过的 offset（page_cache 内），不新增 request
    assert r.target_page_offset in {o for o, _ in a.request_log}
