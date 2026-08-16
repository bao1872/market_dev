"""Kernel P4–P9 tests — targeted 09:25 source search + canonicalization gate.

Round 3B-D kernel（auction_member_fact_backfill_kernel）的 P4–P9 测试。全部纯内存：
- 不连真实 DB / pytdx；
- 通过 FakePytdxAdapter mock get_history_transaction_page 的页面数据；
- 直接调用 kernel 纯函数验证 targeted search / canonicalization gate。

覆盖：
- P4 test_cold_search_finds_0925        冷启动 targeted search 找到 09:25 window
- P5 test_cross_page_boundary           09:25 记录跨 page boundary 仍取完整
- P6 test_hinted_search_equals_cold     hinted 与 cold 的 raw records 完全一致
- P7 test_warm_hint_fewer_requests      warm hint 的 page request 数显著低于 cold
- P8 test_canonicalize_only_when_complete 仅 TARGET_WINDOW_COMPLETE 才 canonicalize
- P9 test_source_empty_not_business_zero  source empty → SOURCE_EMPTY / price=None

Mock 数据布局说明：kernel 的 boundary binary search 假定「page(0) 非整页早于
09:25:00」且 window records 落在 boundary（首个空页/整页早于 09:25 的页）之前的
最后两个数据页内。因此测试数据从 09:25:00 开始（page(0) max >= 09:25:00），
window 位于数据尾部，boundary 之后即空页。该约定与 P4/P5 的页面定义一致。
"""

from __future__ import annotations

import importlib.util
import math
import sys
from datetime import date, datetime, timedelta
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
# Fakes
# ---------------------------------------------------------------------------
def _tick(ts: str, price: float, vol: float = 1.0, buyorsell: int = 1) -> dict:
    return {"time": ts, "price": float(price), "vol": float(vol),
            "buyorsell": buyorsell}


def _ticks_from(start_ts: str, n: int, price: float,
                vol: float = 1.0, buyorsell: int = 1) -> list[dict]:
    """生成从 start_ts 起 n 条逐秒递增的逐笔记录。"""
    start = datetime.strptime(start_ts, "%H:%M:%S")
    return [
        _tick((start + timedelta(seconds=i)).strftime("%H:%M:%S"),
              price, vol, buyorsell)
        for i in range(n)
    ]


def _build_full_day_pages(total: int = 4000, page_size: int = 800):
    """构造多页完整日数据（reverse order，window 位于数据尾部）。

    chrono = window(09:25:00~09:25:59, 60 条) + post-window(09:26:00 起 N-60 条)
    reverse 后 window 位于 reverse list 末尾 → targeted search 的 boundary
    （首个空页）恰好在 window 之后，window 落在 boundary 前的最后两个数据页内。
    """
    chrono = (
        _ticks_from("09:25:00", 60, 10.5)
        + _ticks_from("09:26:00", total - 60, 10.6)
    )
    rev = chrono[::-1]
    pages = {}
    for off in range(0, len(rev), page_size):
        pages[off] = rev[off:off + page_size]
    return pages, rev


class FakePytdxAdapter:
    """纯内存 mock adapter：按 offset 返回预定义页面，记录每次 page request。

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


# ---------------------------------------------------------------------------
# P4 — 冷启动（offset_hint=None）targeted search 找到 09:25 window
# ---------------------------------------------------------------------------
def test_cold_search_finds_0925():
    # 单页数据：09:25:00~09:25:30（31 条）落在 offset 0 页
    adapter = FakePytdxAdapter({0: _ticks_from("09:25:00", 31, 10.5, vol=100.0)})

    result = KERNEL.fetch_auction_0925_targeted(adapter, INST.symbol, TRADE_DATE)

    assert result.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert result.used_hint is False
    assert result.records, "records 不应为空"
    assert all(
        KERNEL.TARGET_WINDOW_START <= str(r["time"]) <= KERNEL.TARGET_WINDOW_END
        for r in result.records
    )
    assert len(result.records) == 31
    assert result.source_first_time == "09:25:00"
    assert result.source_last_time == "09:25:30"


# ---------------------------------------------------------------------------
# P5 — 09:25 记录跨 page boundary 时仍取完整
# ---------------------------------------------------------------------------
def test_cross_page_boundary():
    # page(0) = 09:25:00~09:25:29；page(800) = 09:25:30~09:25:59；page(1600)=空
    page0 = _ticks_from("09:25:00", 30, 10.5, vol=100.0)
    page1 = _ticks_from("09:25:30", 30, 10.5, vol=100.0)
    adapter = FakePytdxAdapter({0: page0, PAGE_SIZE: page1})

    result = KERNEL.fetch_auction_0925_targeted(adapter, INST.symbol, TRADE_DATE)

    assert result.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    # 覆盖完整 60 秒 window
    assert len(result.records) == 60
    times = sorted(str(r["time"]) for r in result.records)
    assert times[0] == "09:25:00"
    assert times[-1] == "09:25:59"
    assert len(set(times)) == 60  # 无重复记录（跨页去重生效）


# ---------------------------------------------------------------------------
# P6 — hinted search 与 cold search 的 raw records 完全一致
# ---------------------------------------------------------------------------
def test_hinted_search_equals_cold():
    pages, _ = _build_full_day_pages()

    cold_adapter = FakePytdxAdapter(pages)
    cold = KERNEL.fetch_auction_0925_targeted(
        cold_adapter, INST.symbol, TRADE_DATE)

    hinted_adapter = FakePytdxAdapter(pages)
    hinted = KERNEL.fetch_auction_0925_targeted(
        hinted_adapter, INST.symbol, TRADE_DATE,
        offset_hint=cold.resolved_offset)

    assert cold.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert hinted.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert cold.records and hinted.records

    cold_keys = [
        (r["time"], r["price"], r["vol"], r["buyorsell"])
        for r in cold.records
    ]
    hint_keys = [
        (r["time"], r["price"], r["vol"], r["buyorsell"])
        for r in hinted.records
    ]
    assert cold_keys == hint_keys


# ---------------------------------------------------------------------------
# P7 — warm hint 的 page request 数显著低于 cold search
# ---------------------------------------------------------------------------
def test_warm_hint_fewer_requests():
    pages, _ = _build_full_day_pages()

    cold_adapter = FakePytdxAdapter(pages)
    cold = KERNEL.fetch_auction_0925_targeted(
        cold_adapter, INST.symbol, TRADE_DATE)

    warm_adapter = FakePytdxAdapter(pages)
    warm = KERNEL.fetch_auction_0925_targeted(
        warm_adapter, INST.symbol, TRADE_DATE,
        offset_hint=cold.resolved_offset - PAGE_SIZE)

    assert cold.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert warm.source_status == SOURCE_TARGET_WINDOW_COMPLETE
    assert cold.records and warm.records
    # warm hint 请求数必须明显低于 cold
    assert cold_adapter.page_count > warm_adapter.page_count
    assert warm_adapter.page_count <= cold_adapter.page_count // 2


# ---------------------------------------------------------------------------
# P8 — 只有 TARGET_WINDOW_COMPLETE 才允许 canonicalize
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
        records=[{"time": "09:25:00", "price": 10.0, "vol": 100.0,
                  "buyorsell": 0}],
    )
    layer = KERNEL.canonicalize_targeted_window(INST, TRADE_DATE, complete)
    assert layer["canonicalization_status"] == SEM.CANON_STATUS_CANONICAL
    assert layer["auction_price_raw"] == 10.0
    assert layer["auction_volume_shares"] == 100.0 * SEM.AUCTION_LOT_MULTIPLIER


# ---------------------------------------------------------------------------
# P9 — source empty 返回 SOURCE_EMPTY，auction_price_raw=None（非 0 / NaN）
# ---------------------------------------------------------------------------
def test_source_empty_not_business_zero():
    adapter = FakePytdxAdapter({})  # 全天无数据

    result = KERNEL.fetch_auction_0925_targeted(adapter, INST.symbol, TRADE_DATE)

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
