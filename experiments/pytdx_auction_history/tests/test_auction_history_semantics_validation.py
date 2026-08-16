"""Round 1D offline tests — Final Runner Integrity Correction.

不连接生产，不执行 --live。全部 fake source / fake MDAS / fake adapter。
"""

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pandas as pd
import pytest

import importlib.util
import sys

SPEC = Path(__file__).resolve().parent.parent / "auction_history_semantics_validation.py"
spec = importlib.util.spec_from_file_location(
    "auction_history_semantics_validation_r1d", str(SPEC))
mod = importlib.util.module_from_spec(spec)
sys.modules["auction_history_semantics_validation_r1d"] = mod
spec.loader.exec_module(mod)

UUID_ZERO = UUID(int=0)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalars(self):
        # get_active_a_share_instruments 用 select(Instrument.id)...scalars().all()
        # 返回每行 id（兼容 fake session 的 dict 行）
        ids = [r["id"] if isinstance(r, dict) else r for r in self._rows]
        return _FakeScalarResult(ids)

    def scalar(self):
        return self._rows[0] if self._rows else None


class _FakeScalarResult:
    def __init__(self, ids):
        self._ids = ids

    def all(self):
        return self._ids


class _FakeSessionAsync:
    async def execute(self, *a, **k):
        return _FakeResult([])


class _FakeSession:
    """sync-style fake session（resolve_sample_instruments 同步使用）。"""
    def __init__(self, instrument_rows):
        self._rows = instrument_rows

    async def execute(self, stmt, params=None):
        return _FakeResult(self._rows)


class _FakeMdas:
    """返回固定 daily bars DataFrame（index=pd.Timestamp, columns open/high/low/close/volume/amount）。"""

    def __init__(self, bars_by_id=None):
        self.bars_by_id = bars_by_id or {}
        self.calls = []

    async def get_bars(self, session, instrument_id, timeframe="1d", adj="none",
                       end_date=None, limit=None, adjustment_as_of=None, **kw):
        self.calls.append((instrument_id, adj, end_date))
        bars = self.bars_by_id.get(str(instrument_id), pd.DataFrame())
        # 简单截断到最近 limit
        if limit and len(bars) > limit:
            bars = bars.tail(limit)

        class _R:
            pass
        r = _R()
        r.bars = bars
        r.data_source = "db"
        r.degraded = False
        r.degraded_reason = None
        r.adj_factor_hash = "h" if adj == "qfq" else ""
        return r


class _FakeAdjService:
    def __init__(self, factors=None):
        self._factors = factors  # dict instrument_id_str -> DataFrame
        self.captured_calls = []  # (instrument_id, as_of)

    async def get_factor_series(self, session, instrument_id, as_of=None, **kw):
        # MOD1: 真实 instance，必须收到 resolved_corporate 的真实 UUID
        self.captured_calls.append((instrument_id, as_of))
        return self._factors.get(str(instrument_id), pd.DataFrame())


class _FakeAdapter:
    """managed connection；api.get_history_transaction_data 由 caller 注入 pages 行为。"""

    def __init__(self, page_provider):
        self._page_provider = page_provider
        self.api = _FakeApi(page_provider)
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *a):
        self.entered = False


class _FakeApi:
    def __init__(self, page_provider):
        self._page_provider = page_provider
        self.call_count = 0

    def get_history_transaction_data(self, market, code, start, count, date_int):
        self.call_count += 1
        pages = self._page_provider(market, code, start, count, date_int)
        if isinstance(pages, Exception):
            raise pages
        return pages.get(start, [])


def _make_inst(market, symbol, iid=None):
    return {
        "market": market, "symbol": symbol,
        "id": iid if iid is not None else uuid4(),
    }


def _bars_from(rows):
    """rows: list of (date, open, close, volume)。"""
    data = {pd.Timestamp(d): {"open": o, "high": o, "low": o, "close": c,
                              "volume": v, "amount": v * 10.0}
            for d, o, c, v in rows}
    df = pd.DataFrame.from_dict(data, orient="index")
    df.index.name = "trade_date"
    return df.sort_index()


# ---------------------------------------------------------------------------
# 样本 / 身份
# ---------------------------------------------------------------------------
def test_load_sample_automatic_counts():
    samples = mod.load_sample()
    routine = [s for s in samples if s.cohort == "routine"]
    corporate = [s for s in samples if s.cohort == "corporate"]
    # 程序计算，不手工数
    assert len(routine) == 29
    assert len(corporate) == 8
    # board 分布
    from collections import Counter
    boards = Counter(s.board for s in routine)
    assert boards == {"SH_MAIN": 10, "SZ_MAIN": 9, "CHINEXT": 6, "STAR": 4}
    for s in samples:
        assert s.instrument_id == UUID_ZERO  # 占位


async def _impl_test_resolve_identity_nonzero_uuid():
    inst = _make_inst("SH", "600000")
    session = _FakeSession([inst])
    samples = [mod.SampleInstrument("600000", "SH", UUID_ZERO, "SH_MAIN",
                                    "ordinary", "routine")]
    resolved, skipped = await mod.resolve_sample_instruments(session, samples)
    assert len(resolved) == 1
    assert resolved[0].instrument_id != UUID_ZERO
    assert skipped == []


def test_resolve_identity_nonzero_uuid_sync():
    asyncio.run(_impl_test_resolve_identity_nonzero_uuid())


async def _impl_test_resolve_identity_uuid_zero_failfast():
    # Instrument ORM 不应返回 UUID(0)；若返回则必须 raise
    inst = {"market": "SH", "symbol": "600000", "id": UUID_ZERO}
    session = _FakeSession([inst])
    samples = [mod.SampleInstrument("600000", "SH", UUID_ZERO, "SH_MAIN",
                                    "ordinary", "routine")]
    with pytest.raises(RuntimeError):
        await mod.resolve_sample_instruments(session, samples)


def test_resolve_identity_uuid_zero_failfast_sync():
    asyncio.run(_impl_test_resolve_identity_uuid_zero_failfast())


async def _impl_test_resolve_identity_ambiguous():
    i1 = _make_inst("SH", "600000")
    i2 = _make_inst("SH", "600000")
    session = _FakeSession([i1, i2])
    samples = [mod.SampleInstrument("600000", "SH", UUID_ZERO, "SH_MAIN",
                                    "ordinary", "routine")]
    resolved, skipped = await mod.resolve_sample_instruments(session, samples)
    assert resolved == []
    assert skipped[0]["reason"] == "IDENTITY_AMBIGUOUS"


def test_resolve_identity_ambiguous_sync():
    asyncio.run(_impl_test_resolve_identity_ambiguous())


# ---------------------------------------------------------------------------
# MOD3 — 时间分类
# ---------------------------------------------------------------------------
def test_classify_canonical():
    assert mod.classify_transaction_time("09:25") == mod.TransactionTimeClass.CANONICAL_0925
    assert mod.classify_transaction_time("09:25:00") == mod.TransactionTimeClass.CANONICAL_0925


def test_classify_noncanonical_0925():
    assert mod.classify_transaction_time("09:25:01") == mod.TransactionTimeClass.NONCANONICAL_0925
    assert mod.classify_transaction_time("09:25:59") == mod.TransactionTimeClass.NONCANONICAL_0925


def test_classify_other():
    for t in ["09:24:59", "09:30", "10:15:32", "14:57"]:
        assert mod.classify_transaction_time(t) == mod.TransactionTimeClass.OTHER


def test_extract_full_day_other_times_missing_0925():
    records = [
        {"time": "09:30", "price": 1, "vol": 1, "buyorsell": 0},
        {"time": "10:00", "price": 1, "vol": 1, "buyorsell": 0},
        {"time": "14:57", "price": 1, "vol": 1, "buyorsell": 0},
    ]
    fdr = mod.FullDayTransactionResult(status="COMPLETE", records=records,
                                      page_count=1, record_count=3)
    ex = mod.extract_from_full_day("600000", "SH", str(uuid4()),
                                   date(2024, 1, 8), fdr)
    assert ex.status == "MISSING_0925"
    assert ex.records == []
    assert ex.noncanonical_records == []


def test_extract_noncanonical_only():
    records = [
        {"time": "09:25:01", "price": 1, "vol": 1, "buyorsell": 0},
    ]
    fdr = mod.FullDayTransactionResult(status="COMPLETE", records=records,
                                      page_count=1, record_count=1)
    ex = mod.extract_from_full_day("600000", "SH", str(uuid4()),
                                   date(2024, 1, 8), fdr)
    assert ex.status == "NONCANONICAL_0925_TIME"
    assert ex.records == []
    assert len(ex.noncanonical_records) == 1


def test_extract_found_and_noncanonical_separate():
    records = [
        {"time": "09:25", "price": 10, "vol": 5, "buyorsell": 1},
        {"time": "09:25:37", "price": 10, "vol": 5, "buyorsell": 0},
        {"time": "09:30", "price": 10, "vol": 5, "buyorsell": 0},
    ]
    fdr = mod.FullDayTransactionResult(status="COMPLETE", records=records,
                                      page_count=1, record_count=3)
    ex = mod.extract_from_full_day("600000", "SH", str(uuid4()),
                                   date(2024, 1, 8), fdr)
    assert ex.status == "FOUND"
    assert len(ex.records) == 1
    assert len(ex.noncanonical_records) == 1


# ---------------------------------------------------------------------------
# MOD2 — exclusive T-1 / T+1
# ---------------------------------------------------------------------------
async def _impl_test_previous_exclusive_monday():
    session = _FakeSessionAsync()
    T = date(2024, 1, 8)  # Monday
    prev = await mod.previous_trading_day_before(session, T)
    assert prev == date(2024, 1, 5)  # Friday
    assert prev < T


def test_previous_exclusive_monday_sync():
    asyncio.run(_impl_test_previous_exclusive_monday())


async def _impl_test_next_exclusive_monday():
    session = _FakeSessionAsync()
    T = date(2024, 1, 8)
    nxt = await mod.next_trading_day_after(session, T)
    assert nxt == date(2024, 1, 9)  # Tuesday
    assert nxt > T


def test_next_exclusive_monday_sync():
    asyncio.run(_impl_test_next_exclusive_monday())


async def _impl_test_next_exclusive_friday():
    session = _FakeSessionAsync()
    T = date(2024, 1, 5)  # Friday
    nxt = await mod.next_trading_day_after(session, T)
    assert nxt == date(2024, 1, 8)  # following Monday
    assert nxt > T


def test_next_exclusive_friday_sync():
    asyncio.run(_impl_test_next_exclusive_friday())


# ---------------------------------------------------------------------------
# MOD1 — corporate uses resolved instruments
# ---------------------------------------------------------------------------
async def _impl_test_corporate_resolved_nonzero_id():
    adj = _FakeAdjService({})
    session = _FakeSessionAsync()
    inst = mod.SampleInstrument("600000", "SH", uuid4(), "SH_MAIN",
                                "ordinary", "corporate")
    cases = await mod.resolve_corporate_cases(adj, session, [inst],
                                              date(2024, 1, 8), 180)
    assert cases[0]["instrument_id"] != str(UUID_ZERO)


def test_corporate_resolved_nonzero_id_sync():
    asyncio.run(_impl_test_corporate_resolved_nonzero_id())


async def _impl_test_corporate_uuid_zero_rejected():
    adj = _FakeAdjService({})
    session = _FakeSessionAsync()
    inst = mod.SampleInstrument("600000", "SH", UUID_ZERO, "SH_MAIN",
                                "ordinary", "corporate")
    with pytest.raises(ValueError):
        await mod.resolve_corporate_cases(adj, session, [inst],
                                          date(2024, 1, 8), 180)


def test_corporate_uuid_zero_rejected_sync():
    asyncio.run(_impl_test_corporate_uuid_zero_rejected())


async def _impl_test_corporate_date_evidence_hard_assert():
    # MOD10: prev < event, next > event
    # MOD3: factor_before=1.0, factor_after=1.5 真正来自 authoritative series
    df = pd.DataFrame({
        "trade_date": [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-08")],
        "adj_factor": [1.0, 1.5],
    })
    session = _FakeSessionAsync()
    iid = uuid4()
    adj = _FakeAdjService({str(iid): df})
    inst = mod.SampleInstrument("600000", "SH", iid, "SH_MAIN",
                                "ordinary", "corporate")
    cases = await mod.resolve_corporate_cases(adj, session, [inst],
                                              date(2024, 1, 8), 180)
    # event should be discovered at 2024-01-08
    c = cases[0]
    assert c["status"] == "RESOLVED"
    assert c["event_date"] == "2024-01-08"
    assert c["prev_trade_date"] == "2024-01-05"
    assert c["next_trade_date"] == "2024-01-09"
    assert c["prev_trade_date"] < c["event_date"]
    assert c["next_trade_date"] > c["event_date"]
    # MOD1: adj service instance 收到真实 UUID
    assert any(cid == iid for cid, _ in adj.captured_calls)
    # MOD3: factor evidence 非 None 且不相等
    assert c["factor_before"] is not None
    assert c["factor_after"] is not None
    assert c["factor_before"] != c["factor_after"]
    assert abs(c["factor_before"] - 1.0) < 1e-9
    assert abs(c["factor_after"] - 1.5) < 1e-9


def test_corporate_factor_event_incomplete():
    async def _impl():
        # 单点 factor series 无 change → NO_EVENT_IN_LOOKBACK（不伪造 INCOMPLETE）
        df = pd.DataFrame({
            "trade_date": [pd.Timestamp("2024-01-08")],
            "adj_factor": [1.0],
        })
        session = _FakeSessionAsync()
        iid = uuid4()
        adj = _FakeAdjService({str(iid): df})
        inst = mod.SampleInstrument("600000", "SH", iid, "SH_MAIN",
                                    "ordinary", "corporate")
        cases = await mod.resolve_corporate_cases(adj, session, [inst],
                                                  date(2024, 1, 8), 180)
        assert cases[0]["status"] == "NO_EVENT_IN_LOOKBACK"
        assert cases[0]["factor_before"] is None
        assert cases[0]["factor_after"] is None
    asyncio.run(_impl())


def test_corporate_date_evidence_hard_assert_sync():
    asyncio.run(_impl_test_corporate_date_evidence_hard_assert())


# ---------------------------------------------------------------------------
# MOD4 / MOD14 — pagination
# ---------------------------------------------------------------------------
def test_pagination_two_pages_short_final():
    page0 = [{"time": "09:25", "price": 1, "vol": 1, "buyorsell": 0}] * 800
    page1 = [{"time": "14:57", "price": 1, "vol": 1, "buyorsell": 0}]
    seq = {0: page0, 800: page1, 1600: []}
    adapter = _FakeAdapter(lambda *a: seq)
    r = mod.fetch_full_day_transactions_paginated(adapter, "600000", 1, date(2024, 1, 8))
    assert r.status == "COMPLETE"
    assert r.record_count == 801
    assert r.page_count == 2


def test_pagination_empty_first():
    def provider(market, code, start, count, date_int):
        return {0: []}
    adapter = _FakeAdapter(provider)
    r = mod.fetch_full_day_transactions_paginated(adapter, "600000", 1, date(2024, 1, 8))
    assert r.status == "EMPTY"


def test_pagination_source_error():
    def provider(market, code, start, count, date_int):
        return RuntimeError("boom")
    adapter = _FakeAdapter(provider)
    r = mod.fetch_full_day_transactions_paginated(adapter, "600000", 1, date(2024, 1, 8))
    assert r.status == "SOURCE_ERROR"
    assert r.error_code == "RuntimeError"


def test_pagination_stalled():
    page = [{"time": "09:25", "price": 1, "vol": 1, "buyorsell": 0}] * 800
    # 第一页指纹与后续重复（offset 前进但 source 不变）
    seq = {0: page, 800: page, 1600: page}
    adapter = _FakeAdapter(lambda *a: seq)
    r = mod.fetch_full_day_transactions_paginated(adapter, "600000", 1, date(2024, 1, 8))
    assert r.status == "PAGINATION_STALLED"


def test_pagination_limit_reached():
    # 每页 800 条且指纹各不相同（不触发 STALLED），直到 MAX_PAGES 上限
    seq = {}
    for i in range(250):
        hh = 9 + (i % 10)
        mm = i % 60
        seq[i * 800] = [{"time": f"{hh:02d}:{mm:02d}", "price": 1,
                         "vol": 1, "buyorsell": 0}] * 800
    adapter = _FakeAdapter(lambda *a: seq)
    r = mod.fetch_full_day_transactions_paginated(adapter, "600000", 1, date(2024, 1, 8))
    assert r.status == "PAGINATION_LIMIT_REACHED"


def test_pagination_incomplete_no_lane():
    def provider(market, code, start, count, date_int):
        return {0: [{"time": "09:25", "price": 1, "vol": 1, "buyorsell": 0}]}
    adapter = _FakeAdapter(provider)
    r = mod.fetch_full_day_transactions_paginated(adapter, "600000", 1, date(2024, 1, 8))
    # 单页满 800 → 实际会再请求；用 short page 模拟完整
    # 改为 short final：
    seq = {0: [{"time": "09:25", "price": 1, "vol": 1, "buyorsell": 0}], 800: []}
    adapter2 = _FakeAdapter(lambda *a: seq)
    r2 = mod.fetch_full_day_transactions_paginated(adapter2, "600000", 1, date(2024, 1, 8))
    assert r2.status == "COMPLETE"
    ex = mod.extract_from_full_day("600000", "SH", str(uuid4()),
                                   date(2024, 1, 8), r2)
    assert ex.status == "FOUND"


def test_pagination_incomplete_blocks_extraction():
    r = mod.FullDayTransactionResult(status="PAGINATION_STALLED", records=[])
    ex = mod.extract_from_full_day("600000", "SH", str(uuid4()),
                                   date(2024, 1, 8), r)
    assert ex.status == "SOURCE_PAGINATION_INCOMPLETE"


# ---------------------------------------------------------------------------
# MOD7 / MOD9 — volume evidence + amount unresolved
# ---------------------------------------------------------------------------
def test_volume_evidence_ratio_math():
    bars = _bars_from([(date(2024, 1, 8), 10, 10, 10000),
                       (date(2024, 1, 5), 9, 9, 9000)])
    mdas = _FakeMdas({"X": bars})

    class _R:
        pass
    r = _R()
    r.bars = bars
    r.data_source = "db"
    r.degraded = False
    r.degraded_reason = None

    records = [{"time": "09:30", "price": 1, "vol": 50, "buyorsell": 0} for _ in range(100)]
    fdr = mod.FullDayTransactionResult(status="COMPLETE", records=records,
                                      page_count=1, record_count=100)
    ve = mod._compute_volume_from_full_day(
        mod.SampleInstrument("600000", "SH", uuid4(), "SH_MAIN", "x", "routine"),
        date(2024, 1, 8), fdr, r)
    assert ve["sum_transaction_raw_vol"] == 5000
    assert ve["valid_volume_record_count"] == 100
    assert ve["mdas_daily_volume"] == 10000
    assert abs(ve["daily_volume_ratio"] - 2.0) < 1e-9
    amt = mod.compute_amount_evidence()
    assert amt["amount_source_type"] == "RAW_FIELD_ABSENT"
    assert amt["candidate_derived_amount"] is None


# ---------------------------------------------------------------------------
# MOD12 — live status dimensions
# ---------------------------------------------------------------------------
def test_derive_live_status_partial():
    obs = [{
        "full_day_status": "COMPLETE", "extraction_status": "FOUND",
        "volume_evidence": {"daily_volume_ratio": 100}, "board": "SH_MAIN",
        "symbol": "600000", "market": "SH", "trade_date": "2024-01-08",
    }, {
        "full_day_status": "SOURCE_ERROR", "extraction_status": "SOURCE_ERROR",
        "volume_evidence": {"daily_volume_ratio": None}, "board": "SZ_MAIN",
        "symbol": "000001", "market": "SZ", "trade_date": "2024-01-08",
    }]
    status = mod.derive_live_status(obs, [])
    assert status["auction_source_evidence"] in ("COMPLETE", "PARTIAL")
    assert status["EVIDENCE_COMPLETENESS"] != "COMPLETE"  # 有 SOURCE_ERROR 维度
    # 不输出单一宽泛 COMPLETE 掩盖
    assert "auction_source_evidence" in status
    assert "volume_unit_evidence" in status


# ---------------------------------------------------------------------------
# MOD11 — data quality denominator
# ---------------------------------------------------------------------------
def test_data_quality_denominator():
    obs = [{"full_day_status": "COMPLETE"}, {"full_day_status": "EMPTY"},
           {"full_day_status": "SOURCE_ERROR"}, {"full_day_status": "COMPLETE",
                                                 "volume_evidence": {"daily_volume_ratio": 1}}]
    dq = mod.compute_data_quality_summary(obs)
    assert dq["total_source_days_attempted"] == 4
    assert dq["pagination"]["COMPLETE"] == 2
    assert dq["pagination"]["EMPTY"] == 1
    assert dq["auction_semantics_eligible_days"] == 2
    assert dq["volume_unit_eligible_days"] == 1


# ---------------------------------------------------------------------------
# MOD13 — END-TO-END fake evidence pipeline
# ---------------------------------------------------------------------------
def test_end_to_end_fake_evidence(tmp_path, monkeypatch):
    import json

    out = tmp_path / "round1" / "2024-01-08"
    monkeypatch.setattr(mod, "OUTPUT_DIR", out)

    # Fake full-day pagination: Page1 (09:25 cano, 09:25:01 noncano, 09:30),
    # Page2 (10:00, 14:57), final short/empty
    page1 = [
        {"time": "09:25", "price": 100, "vol": 50, "buyorsell": 1},
        {"time": "09:25:01", "price": 100, "vol": 50, "buyorsell": 0},
        {"time": "09:30", "price": 100, "vol": 50, "buyorsell": 0},
    ]
    page2 = [
        {"time": "10:00", "price": 100, "vol": 50, "buyorsell": 0},
        {"time": "14:57", "price": 100, "vol": 50, "buyorsell": 0},
    ]
    seq = {0: page1, 800: page2, 1600: []}

    adapter = _FakeAdapter(lambda *a: seq)
    bars = _bars_from([(date(2024, 1, 8), 100, 100, 10000),
                       (date(2024, 1, 5), 90, 90, 9000)])
    mdas = _FakeMdas({"X": bars})

    # 直接构造 observation 并写 evidence
    iid = uuid4()
    fdr = mod.fetch_full_day_transactions_paginated(adapter, "600000", 1, date(2024, 1, 8))
    ex = mod.extract_from_full_day("600000", "SH", str(iid), date(2024, 1, 8), fdr)
    assert ex.status == "FOUND"

    obs = {
        "symbol": "600000", "market": "SH", "instrument_id": str(iid),
        "board": "SH_MAIN", "cohort": "routine", "trade_date": "2024-01-08",
        "coverage_tag": "large_cap",
        "full_day_status": fdr.status,
        "extraction_status": ex.status,
        "raw_records": [mod._raw_evidence_dict(r) for r in ex.records],
        "noncanonical_records": [mod._raw_evidence_dict(r) for r in ex.noncanonical_records],
        "raw_record_count": len(ex.records),
        "noncanonical_record_count": len(ex.noncanonical_records),
        "lane_a": {"status": "COMPUTED", "price_exact_match": True},
        "lane_b": {"status": "COMPUTED", "pit_gap": 0.1},
        "volume_evidence": {
            "status": "COMPUTED",
            "sum_transaction_raw_vol": 100,  # 5 records × 50? actually 2 pages w/ vol 50*5
            "valid_volume_record_count": 5,
            "invalid_volume_record_count": 0,
            "transaction_record_count": 5,
            "mdas_daily_volume": 10000,
            "daily_volume_ratio": 100.0,
            "pagination_status": "COMPLETE", "page_count": 2,
            "mdas_data_source": "db", "mdas_degraded": False,
            "mdas_degraded_reason": None,
            "evidence_reason": "RATIO_DISTRIBUTION_ONLY_NO_UNIT_CONCLUSION",
        },
        "amount_evidence": mod.compute_amount_evidence(),
        "raw_amount_value": None,
    }
    # override sum to match math (5*50=250 → ratio=40). fix:
    obs["volume_evidence"]["sum_transaction_raw_vol"] = 250
    obs["volume_evidence"]["daily_volume_ratio"] = 10000 / 250

    dq = mod.compute_data_quality_summary([obs])
    vol_dist = mod.compute_volume_unit_distribution([obs])
    live = mod.derive_live_status([obs], [])
    mod.write_evidence_outputs(
        out, date(2024, 1, 8), [obs], [], live, dq, vol_dist,
        [{"symbol": "600000", "market": "SH", "instrument_id": str(iid),
          "trade_date": "2024-01-08", "pagination_status": "COMPLETE",
          "page_count": 2, "record_count": 5, "page_size": 800,
          "source_first_time": "09:25", "source_last_time": "14:57",
          "sum_transaction_raw_vol": 250, "valid_volume_record_count": 5,
          "invalid_volume_record_count": 0, "mdas_daily_volume": 10000,
          "daily_volume_ratio": 40.0, "source_error_code": None,
          "source_error_message": None}],
        routine_count=1, corporate_count=0, corporate_lookback_days=180)

    # reopen 03
    raw_lines = (out / "03_raw_transaction_records.jsonl").read_text().strip().splitlines()
    assert len(raw_lines) == 1
    rec0 = json.loads(raw_lines[0])
    assert rec0["source_time"] == "09:25"
    assert rec0["symbol"] == "600000"

    # reopen 04
    nc_lines = (out / "04_noncanonical_time_records.jsonl").read_text().strip().splitlines()
    assert len(nc_lines) == 1
    nc0 = json.loads(nc_lines[0])
    assert nc0["source_time"] == "09:25:01"

    # reopen 08
    import csv as _csv
    with (out / "08_volume_unit_evidence.csv").open() as f:
        rows = list(_csv.DictReader(f))
    assert len(rows) == 1
    assert float(rows[0]["daily_volume_ratio"]) == 40.0

    # reopen 10
    dq_re = json.loads((out / "10_data_quality_summary.json").read_text())
    assert dq_re["total_source_days_attempted"] == 1

    # reopen 11: no PASS / confirmed
    vs = json.loads((out / "11_validation_summary.json").read_text())
    assert vs["amount_evidence"]["DIRECT_RAW_AMOUNT"] == "UNAVAILABLE"
    assert vs["amount_evidence"]["DERIVED_AMOUNT"] == "PENDING_VOLUME_UNIT_CONFIRMATION"
    text = json.dumps(vs)
    for forbidden in ["source PASS", "volume unit confirmed", "amount confirmed",
                     "CONFIRMED", "RUNNER_CONFIRMED"]:
        assert forbidden.upper() not in text.upper()


# ---------------------------------------------------------------------------
# MOD15 — one source fetch per symbol/day
# ---------------------------------------------------------------------------
def test_one_source_fetch_per_day():
    page1 = [{"time": "09:25", "price": 1, "vol": 1, "buyorsell": 0}]
    seq = {0: page1, 800: []}
    adapter = _FakeAdapter(lambda *a: seq)
    fdr = mod.fetch_full_day_transactions_paginated(adapter, "600000", 1, date(2024, 1, 8))
    ex = mod.extract_from_full_day("600000", "SH", str(uuid4()),
                                   date(2024, 1, 8), fdr)
    # extraction 复用同一 fdr，不重新调 source
    assert ex.status == "FOUND"
    # adapter.api.call_count reflects only pagination, not a separate 09:25 call
    assert adapter.api.call_count == 1


# ---------------------------------------------------------------------------
# MOD7 — previous_trading_dates respects official calendar (not weekday-only)
# ---------------------------------------------------------------------------
def _fake_calendar(trading_dates: set):
    async def _cal(session, d: date):
        return d in trading_dates
    return _cal


def _make_10_dates(ending: date) -> list[date]:
    # 连续 10 个 weekday（不含周末），结束于 ending，向前回溯
    out = []
    cur = ending
    while len(out) < 10:
        if cur.weekday() < 5:
            out.append(cur)
        cur -= timedelta(days=1)
    return out


def test_previous_trading_dates_includes_as_of_if_trading_day(monkeypatch):
    # Case A: as_of = Monday trading day → includes Monday
    as_of = date(2024, 1, 8)  # Monday
    dates = set(_make_10_dates(as_of))
    monkeypatch.setattr(mod, "is_trading_day_async", _fake_calendar(dates))
    session = _FakeSessionAsync()
    out = asyncio.run(mod.previous_trading_dates(session, as_of, 10))
    assert len(out) == 10
    assert out[0] == as_of  # 包含当天


def test_previous_trading_dates_skips_non_trading_as_of(monkeypatch):
    # Case B: as_of = Sunday → first result = previous Friday
    sunday = date(2024, 1, 7)  # Sunday
    friday = date(2024, 1, 5)
    dates = set(_make_10_dates(friday))  # 向后回溯的 10 个交易日，含周五
    monkeypatch.setattr(mod, "is_trading_day_async", _fake_calendar(dates))
    session = _FakeSessionAsync()
    out = asyncio.run(mod.previous_trading_dates(session, sunday, 10))
    assert len(out) == 10
    assert out[0] == friday  # 从最近 previous official trading day 开始
    assert out[0] != sunday


def test_previous_trading_dates_holiday_weekday(monkeypatch):
    # Case C: 某个 weekday 是 holiday（非交易日）→ official calendar 决定
    as_of = date(2024, 1, 10)  # Wednesday
    holiday = date(2024, 1, 11)  # Thursday 作为 holiday（非交易）
    dates = set(_make_10_dates(as_of))
    dates.discard(holiday)  # 官方日历剔除该周三
    monkeypatch.setattr(mod, "is_trading_day_async", _fake_calendar(dates))
    session = _FakeSessionAsync()
    out = asyncio.run(mod.previous_trading_dates(session, as_of, 10))
    assert len(out) == 10
    assert holiday not in out  # holiday weekday 不进入结果


# ---------------------------------------------------------------------------
# MOD5 — canonical universe membership gate
# ---------------------------------------------------------------------------
def test_universe_gate_skips_non_canonical(monkeypatch):
    async def _fake_canonical(session):
        # 只含另一支 instrument 的 UUID
        return [uuid4()]
    monkeypatch.setattr(mod, "get_active_a_share_instruments", _fake_canonical)

    inst = {"market": "SZ", "symbol": "000002", "id": uuid4()}  # 不在 canonical
    session = _FakeSession([inst])
    samples = [mod.SampleInstrument("000002", "SZ", UUID_ZERO, "SZ_MAIN",
                                    "ordinary", "routine")]
    resolved, skipped = asyncio.run(
        mod.resolve_sample_instruments(session, samples))
    assert resolved == []
    assert skipped[0]["reason"] == "SAMPLE_NOT_IN_CANONICAL_UNIVERSE"
    assert skipped[0]["instrument_id"] == str(inst["id"])


# ---------------------------------------------------------------------------
# MOD2 / MOD4 — corporate observation runs on event_T with factor evidence
# ---------------------------------------------------------------------------
def test_run_corporate_observation_on_event_T(monkeypatch):
    async def _fake_calendar(session, d: date):
        return d.weekday() < 5
    monkeypatch.setattr(mod, "is_trading_day_async", _fake_calendar)

    event_T = date(2024, 1, 8)
    prev_d = date(2024, 1, 5)
    next_d = date(2024, 1, 9)
    iid = uuid4()

    page1 = [{"time": "09:25", "price": 10, "vol": 1, "buyorsell": 1}]
    seq = {0: page1, 800: []}
    adapter = _FakeAdapter(lambda *a: seq)
    bars = _bars_from([(date(2024, 1, 8), 10.2, 10.2, 10000),
                       (date(2024, 1, 5), 20, 20, 9000)])
    mdas = _FakeMdas({str(iid): bars})

    inst = mod.SampleInstrument("600519", "SH", iid, "SH_MAIN",
                                "ordinary", "corporate")
    obs = asyncio.run(mod.run_corporate_observation(
        mdas, adapter, _FakeSessionAsync(), inst, event_T, prev_d, next_d,
        factor_before=1.0, factor_after=2.0))
    # MOD2: observation trade_date 必须 == event_T，不等于 as_of
    assert obs["trade_date"] == "2024-01-08"
    assert obs["cohort"] == "corporate"
    # MOD4: factor evidence 贯通 observation
    assert obs["corporate"]["event_date"] == "2024-01-08"
    assert obs["corporate"]["prev_trade_date"] == "2024-01-05"
    assert obs["corporate"]["next_trade_date"] == "2024-01-09"
    assert obs["corporate"]["factor_before"] == 1.0
    assert obs["corporate"]["factor_after"] == 2.0
    assert obs["corporate"]["prev_trade_date"] < obs["corporate"]["event_date"]
    assert obs["corporate"]["event_date"] < obs["corporate"]["next_trade_date"]


# ---------------------------------------------------------------------------
# MOD8 — FULL run_validation orchestration (no manual bypass)
# ---------------------------------------------------------------------------
def test_run_validation_full_orchestration(tmp_path, monkeypatch):
    import json as _json

    out = tmp_path / "full"
    monkeypatch.setattr(mod, "OUTPUT_DIR", out)

    # 1 routine + 1 corporate（controlled fixture）
    routine_iid = uuid4()
    corp_iid = uuid4()

    def fake_sample():
        return [
            mod.SampleInstrument("600519", "SH", UUID_ZERO, "SH_MAIN",
                                 "large_cap", "routine"),
            mod.SampleInstrument("000002", "SZ", UUID_ZERO, "SZ_MAIN",
                                 "ordinary", "corporate"),
        ]
    monkeypatch.setattr(mod, "load_sample", fake_sample)

    async def fake_canonical(session):
        return [routine_iid, corp_iid]
    monkeypatch.setattr(mod, "get_active_a_share_instruments", fake_canonical)

    # identity resolver via session.execute params (market, symbol) -> UUID
    def identity_for(market, symbol):
        if market == "SH" and symbol == "600519":
            return [{"market": "SH", "symbol": "600519", "id": routine_iid}]
        if market == "SZ" and symbol == "000002":
            return [{"market": "SZ", "symbol": "000002", "id": corp_iid}]
        return []

    # official calendar: 10 个连续交易日（结束于 as_of，向后回溯）
    as_of = date(2024, 2, 1)  # Friday
    trading_dates = set(_make_10_dates(as_of))
    trading_dates.update({date(2024, 1, 5), date(2024, 1, 8), date(2024, 1, 9)})

    async def fake_is_trading_day(session, d: date):
        return d in trading_dates
    monkeypatch.setattr(mod, "is_trading_day_async", fake_is_trading_day)

    # MDAS: routine + corporate bars
    routine_bars = _bars_from([(d, 100, 100, 10000) for d in trading_dates])
    corp_bars = _bars_from([
        (date(2024, 1, 8), 10.2, 10.2, 10000),  # T open/close
        (date(2024, 1, 5), 20, 20, 9000),       # T-1 raw close
    ])
    qfq_corp_bars = _bars_from([
        (date(2024, 1, 8), 10.2, 10.2, 10000),  # qfq T open
        (date(2024, 1, 5), 10, 10, 9000),       # qfq T-1 close
    ])

    class _FullMdas:
        def __init__(self):
            self.calls = []

        async def get_bars(self, session, instrument_id, timeframe="1d",
                           adj="none", end_date=None, limit=None,
                           adjustment_as_of=None, **kw):
            self.calls.append((instrument_id, adj, end_date))
            if adj == "qfq":
                bars = qfq_corp_bars
            else:
                bars = routine_bars if str(instrument_id) != str(corp_iid) else corp_bars
            if limit and len(bars) > limit:
                bars = bars.tail(limit)

            class _R:
                pass
            r = _R()
            r.bars = bars
            r.data_source = "db"
            r.degraded = False
            r.degraded_reason = None
            r.adj_factor_hash = "h" if adj == "qfq" else ""
            return r
    mdas = _FullMdas()

    # AdjustmentFactorService instance（MOD1）
    factor_df = pd.DataFrame({
        "trade_date": [pd.Timestamp("2024-01-05"), pd.Timestamp("2024-01-08")],
        "adj_factor": [1.0, 2.0],
    })
    adj = _FakeAdjService({str(corp_iid): factor_df})

    # historical transaction source
    page1 = [{"time": "09:25", "price": 10.2, "vol": 1, "buyorsell": 1}]
    seq = {0: page1, 800: []}
    adapter = _FakeAdapter(lambda *a: seq)

    class _OrchSession(_FakeSessionAsync):
        async def execute(self, stmt, params=None):
            params = params or {}
            market = params.get("market")
            symbol = params.get("symbol")
            return _FakeResult(identity_for(market, symbol) or [])
    session = _OrchSession()

    result = asyncio.run(mod.run_validation(
        session, mdas, adapter, as_of,
        corporate_lookback_days=180, adj_service=adj, output_dir=out))

    # MOD1: adj service instance 收到 corporate 真实 UUID
    assert any(cid == corp_iid for cid, _ in adj.captured_calls)

    # ROUTINE: 1 instrument × 10 trading dates
    routine_obs = [o for o in result["observations"] if o["cohort"] == "routine"]
    assert len(routine_obs) == 10
    for o in routine_obs:
        assert o["trade_date"] in {d.isoformat() for d in trading_dates}
        assert o["symbol"] == "600519"
    # corporate 只运行一次
    corp_obs = [o for o in result["observations"] if o["cohort"] == "corporate"]
    assert len(corp_obs) == 1

    # MOD2: corporate observation trade_date == event_T (2024-01-08), != as_of (2024-02-01)
    cob = corp_obs[0]
    assert cob["trade_date"] == "2024-01-08"
    assert cob["trade_date"] != as_of.isoformat()

    # MOD3: factor_before/factor_after evidence
    cc = next(c for c in result["corporate_cases"]
              if c["instrument_id"] == str(corp_iid))
    assert cc["status"] == "RESOLVED"
    assert cc["event_date"] == "2024-01-08"
    assert cc["prev_trade_date"] == "2024-01-05"
    assert cc["next_trade_date"] == "2024-01-09"
    assert cc["factor_before"] == 1.0
    assert cc["factor_after"] == 2.0
    assert cc["factor_before"] != cc["factor_after"]
    assert date.fromisoformat(cc["prev_trade_date"]) < date.fromisoformat(cc["event_date"]) < date.fromisoformat(cc["next_trade_date"])

    # MOD4: factor evidence 进入 observation.corporate
    assert cob["corporate"]["factor_before"] == 1.0
    assert cob["corporate"]["factor_after"] == 2.0

    # MOD8 PIT Gap: naive_raw_gap ≈ -0.49, pit_gap ≈ +0.02
    lane_b = cob["lane_b"]
    assert lane_b is not None
    assert lane_b["status"] == "COMPUTED"
    assert abs(lane_b["naive_raw_gap"] - (-0.49)) < 0.02
    assert abs(lane_b["pit_gap"] - 0.02) < 0.02
    assert cob["corporate"]["naive_raw_gap"] is not None
    assert cob["corporate"]["pit_gap"] is not None

    # WRITER: reopen 01_observations.json
    obs_file = out / "01_observations.json"
    assert obs_file.exists()
    obs_data = _json.loads(obs_file.read_text())
    assert len([o for o in obs_data if o["cohort"] == "routine"]) == 10
    assert len([o for o in obs_data if o["cohort"] == "corporate"]) == 1

    # corporate row traceable
    crow = next(o for o in obs_data if o["cohort"] == "corporate")
    assert crow["trade_date"] == "2024-01-08"
    assert crow["corporate"]["factor_before"] == 1.0
    assert crow["corporate"]["factor_after"] == 2.0

    # 02 / 07 corporate cases: factor_before/after non-empty
    for fn in ["02_corporate_action_cases.csv"]:
        fpath = out / fn
        if fpath.exists():
            import csv as _csv
            with fpath.open() as f:
                rows = list(_csv.DictReader(f))
            if rows:
                assert rows[0]["factor_before"] not in (None, "")
                assert rows[0]["factor_after"] not in (None, "")

    # 11_validation_summary.json: no PASS / confirmed
    vs = _json.loads((out / "11_validation_summary.json").read_text())
    text = _json.dumps(vs)
    for forbidden in ["source PASS", "volume unit confirmed", "amount confirmed",
                      "CONFIRMED", "RUNNER_CONFIRMED"]:
        assert forbidden.upper() not in text.upper()
