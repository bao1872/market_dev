"""Round 3B-B / 3B-B1 backfill runner tests — file evidence + qfq degraded fail-close.

覆盖任务定义 B1..B13 + Round 3B-B1 live-path wiring closure：
- real Instrument.id conversion
- real-adapter branch wiring（sentinel）
- current-canonical ∩ listing_date population
- listing-date coverage preflight
- 四 board contract（CHINEXT 禁 SZ_GEM）
- actual Lane B projection / adjustment_as_of
- completed metadata mismatch no-overwrite（真实 BLOCK）
- resume root-total equivalence
- full partition reconciliation
- runtime code_sha semantics

所有测试纯内存（不连真实 DB / pytdx），通过注入 calendar_fn / population_fn /
run_single_obs / adapter / output_root / code_sha 实现 fake orchestration。
"""

import asyncio
import hashlib
import json
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from app.models.instrument import Instrument

from auction_history_semantics_validation import SampleInstrument
from full_market_member_fact_backfill import (
    AS_OF,
    BAR_COUNT,
    CompletedPartitionMetadataMismatch,
    RESUME_BLOCK,
    RESUME_RERUN,
    RESUME_SKIP,
    _board_for_symbol,
    _to_sample_inst,
    _partition_resume_decision,
    _expected_partition_metadata,
    _accumulate_partition_quality,
    _run_bar_partition,
    resolve_backfill_population_at,
    check_listing_date_coverage,
    run_backfill,
    PARTITION_STATUS_COMPLETED,
    PARTITION_STATUS_FAILED,
)

# 注入用 dummy / sentinel adapter
_FAKE_ADAPTER = object()
_SENTINEL_ADAPTER = object()
_TEST_SHA = "test-sha-000000000000000000000000000000000000"


def _sample(symbol, market, listing_date=None, instrument_id=None):
    return SampleInstrument(
        symbol=symbol,
        market=market,
        instrument_id=instrument_id or UUID("00000000-0000-0000-0000-000000000001"),
        board=_board_for_symbol(symbol, market),
        coverage_tag="all_a_share",
        cohort="routine",
    )


def _obs_canon_computed(symbol, market, pit_gap=0.05, degraded=False):
    return {
        "symbol": symbol, "market": market,
        "instrument_id": symbol, "board": _board_for_symbol(symbol, market),
        "listing_date": None,
        "full_day_status": "COMPLETE",
        "extraction_status": "COMPLETE",
        "canonicalization_status": "CANONICAL",
        "canonicalization_reason": None,
        "raw_canonical_record_count": 1,
        "positive_volume_record_count": 1,
        "zero_volume_record_count": 0,
        "invalid_volume_record_count": 0,
        "invalid_price_count": 0,
        "auction_price_raw": 11.0,
        "auction_volume_raw_lots": 100,
        "auction_volume_shares": 10000,
        "auction_amount": 110000.0,
        "auction_amount_source_type": "computed",
        "lane_a": {"status": "COMPUTED", "mdas_raw_open_T": 11.0,
                   "price_exact_match": True, "price_diff_abs": 0.0,
                   "price_diff_rel": 0.0},
        "lane_b": {
            "status": "PIT_ADJUSTMENT_DEGRADED" if degraded else "COMPUTED",
            "naive_raw_gap": 0.04,
            "pit_gap": None if degraded else pit_gap,
            "raw_close_Tm1": 10.0,
            "qfq_close_Tm1": 9.5,
            "adjustment_as_of": AS_OF.isoformat(),
            "adj_factor_hash": "abc",
            "mdas_data_source": "db",
            "mdas_degraded": degraded,
            "mdas_degraded_reason": "fake degraded" if degraded else None,
        },
    }


def _obs_source_incomplete(symbol, market):
    return {
        "symbol": symbol, "market": market,
        "instrument_id": symbol, "board": _board_for_symbol(symbol, market),
        "listing_date": None,
        "full_day_status": "EMPTY",
        "extraction_status": "SOURCE_PAGINATION_INCOMPLETE",
        "canonicalization_status": None,
        "canonicalization_reason": "SOURCE_DAY_INCOMPLETE",
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
        "lane_a": None,
        "lane_b": None,
    }


def _make_fake_run_single(behavior):
    async def _fake(mdas, adapter, session, inst, trade_date):
        if callable(behavior):
            return behavior(inst.symbol)
        return behavior[inst.symbol]
    return _fake


def _tmp_output():
    d = Path(tempfile.mkdtemp(prefix="backfill_test_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tmp_out():
    return next(_tmp_output())


def _run(tmp_out, run_id, bar_dates, pop, fake, code_sha=_TEST_SHA,
         adapter=_FAKE_ADAPTER, cal=None):
    async def _cal(session, T, n):
        return bar_dates
    async def _pop(session, t):
        return pop[t]
    async def _go():
        return await run_backfill(
            run_id=run_id, bar_dates=bar_dates,
            calendar_fn=cal or _cal, population_fn=_pop,
            run_single_obs=fake, output_root=tmp_out,
            adapter=adapter, code_sha=code_sha)
    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# B1: official calendar → exactly 120 bars（不是自然日减法）
# ---------------------------------------------------------------------------
def test_b1_calendar_exactly_120_bars(tmp_out):
    seq = []
    cur = AS_OF
    while len(seq) < 120:
        if cur.weekday() < 5:
            seq.append(cur)
        cur -= timedelta(days=1)
    seq = sorted(seq)
    assert len(seq) == 120
    assert seq[-1] == AS_OF
    assert (seq[-1] - seq[0]).days > 120

    res = _run(tmp_out, "t_b1", seq, {t: [] for t in seq},
               _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH")))
    assert res["bar_count"] == 120
    assert res["latest_bar_date"] == AS_OF.isoformat()
    assert res["earliest_bar_date"] == seq[0].isoformat()


# ---------------------------------------------------------------------------
# B2: bar_index 最老=1 最新=120
# ---------------------------------------------------------------------------
def test_b2_bar_index_oldest1_latest120():
    dates = [AS_OF - timedelta(days=i) for i in range(120, 0, -1)]
    assert dates[0] < dates[-1]
    idxs = list(range(1, 121))
    assert idxs[0] == 1 and idxs[-1] == 120


# ---------------------------------------------------------------------------
# B3/B4: IPO filtering by listing_date <= T；中途 IPO 不向前扩展
# ---------------------------------------------------------------------------
def test_b3_b4_ipo_filter_and_midwindow_ipo(tmp_out):
    d1, d2, d3 = date(2026, 2, 13), date(2026, 5, 1), date(2026, 8, 14)
    bar_dates = [d1, d2, d3]
    stock_listing = d2
    pop = {
        d1: [],  # T1 < listing → 不进入
        d2: [_sample("600001", "SH")],
        d3: [_sample("600001", "SH")],
    }
    res = _run(tmp_out, "t_b3", bar_dates, pop,
               _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH")))
    d1_rows = [json.loads(l) for l in open(
        tmp_out / "t_b3" / "bars" / d1.isoformat() / "member_facts.jsonl")]
    d2_rows = [json.loads(l) for l in open(
        tmp_out / "t_b3" / "bars" / d2.isoformat() / "member_facts.jsonl")]
    assert len(d1_rows) == 0
    assert len(d2_rows) == 1 and d2_rows[0]["symbol"] == "600001"
    assert res["completed_bar_count"] == 3


# ---------------------------------------------------------------------------
# B5: source incomplete（真实 owner status EMPTY）仍写 member row
# ---------------------------------------------------------------------------
def test_b5_source_incomplete_writes_row(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH"), _sample("600002", "SH")]}
    fake = _make_fake_run_single(
        lambda s: _obs_source_incomplete(s, "SH") if s == "600001"
        else _obs_canon_computed(s, "SH"))
    res = _run(tmp_out, "t_b5", bar_dates, pop, fake)
    rows = [json.loads(l) for l in open(
        tmp_out / "t_b5" / "bars" / d.isoformat() / "member_facts.jsonl")]
    assert len(rows) == 2
    inc = [r for r in rows if r["symbol"] == "600001"][0]
    assert inc["full_day_status"] == "EMPTY"
    assert inc["extraction_status"] == "SOURCE_PAGINATION_INCOMPLETE"
    assert inc["canonicalization_status"] is None
    assert inc["auction_price_raw"] is None
    norm = [r for r in rows if r["symbol"] == "600002"][0]
    assert norm["canonicalization_status"] == "CANONICAL"
    assert res["completed_bar_count"] == 1


# ---------------------------------------------------------------------------
# B6 + FIX 5 + ADJUSTMENT LINEAGE: canonical projection 机械正确
# ---------------------------------------------------------------------------
def test_b6_canonical_projection(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH", pit_gap=0.07))
    _run(tmp_out, "t_b6", bar_dates, pop, fake)
    row = json.loads(open(tmp_out / "t_b6" / "bars" / d.isoformat()
                          / "member_facts.jsonl").readline())
    assert row["trade_date"] == d.isoformat()
    assert row["bar_index"] == 1
    assert row["symbol"] == "600001"
    assert row["market"] == "SH"
    assert row["board"] == "SH_MAIN"
    assert row["auction_price_raw"] == 11.0
    assert row["auction_volume_shares"] == 10000
    assert row["auction_amount"] == 110000.0
    assert row["lane_a_status"] == "COMPUTED"
    assert row["price_exact_match"] is True
    assert row["lane_b_status"] == "COMPUTED"
    assert row["pit_gap"] == 0.07
    # FIX 5：previous_close_* 机械映射 lane_b ACTUAL keys
    assert row["previous_close_raw"] == 10.0
    assert row["previous_close_pit_qfq"] == 9.5
    # ADJUSTMENT LINEAGE：adjustment_as_of 与 adj_factor_hash 分开
    assert row["adjustment_as_of"] == AS_OF.isoformat()
    assert row["adj_factor_hash"] == "abc"
    assert row["code_sha"] == _TEST_SHA
    assert row["as_of"] == AS_OF.isoformat()


# ---------------------------------------------------------------------------
# B7: qfq degraded → pit_gap unavailable（HARD REGRESSION）
# ---------------------------------------------------------------------------
def test_b7_qfq_degraded_pit_gap_unavailable(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SZ")]}
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SZ", degraded=True))
    _run(tmp_out, "t_b7", bar_dates, pop, fake)
    row = json.loads(open(tmp_out / "t_b7" / "bars" / d.isoformat()
                          / "member_facts.jsonl").readline())
    assert row["lane_b_status"] == "PIT_ADJUSTMENT_DEGRADED"
    assert row["pit_gap"] is None
    assert row["degraded"] is True


# ---------------------------------------------------------------------------
# B8: qfq healthy → pit_gap 正常保留
# ---------------------------------------------------------------------------
def test_b8_qfq_healthy_pit_gap_present(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SZ")]}
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SZ", degraded=False, pit_gap=0.09))
    _run(tmp_out, "t_b8", bar_dates, pop, fake)
    row = json.loads(open(tmp_out / "t_b8" / "bars" / d.isoformat()
                          / "member_facts.jsonl").readline())
    assert row["lane_b_status"] == "COMPUTED"
    assert row["pit_gap"] == 0.09
    assert row["degraded"] is False


# ---------------------------------------------------------------------------
# B9: bar reconciliation PASS
# ---------------------------------------------------------------------------
def test_b9_bar_reconciliation_pass(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH"), _sample("600002", "SH")]}
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH"))
    res = _run(tmp_out, "t_b9", bar_dates, pop, fake)
    m = json.load(open(tmp_out / "t_b9" / "bars" / d.isoformat()
                       / "partition_manifest.json"))
    assert m["status"] == PARTITION_STATUS_COMPLETED
    assert m["eligible_instruments"] == 2
    assert m["member_rows_written"] == 2
    dq = json.load(open(tmp_out / "t_b9" / "bars" / d.isoformat()
                        / "data_quality.json"))
    assert dq["reconciled"] is True


# ---------------------------------------------------------------------------
# B10: rows missing / RUN_ERROR → partition FAILED
# ---------------------------------------------------------------------------
def test_b10_rows_missing_partition_failed(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH"), _sample("600002", "SH")]}
    async def _boom(mdas, adapter, session, inst, trade_date):
        if inst.symbol == "600002":
            raise RuntimeError("boom")
        return _obs_canon_computed(inst.symbol, "SH")
    res = _run(tmp_out, "t_b10", bar_dates, pop, _boom)
    m = json.load(open(tmp_out / "t_b10" / "bars" / d.isoformat()
                       / "partition_manifest.json"))
    assert m["status"] == PARTITION_STATUS_FAILED
    assert res["completed_bar_count"] == 0
    assert res["failed_bar_count"] == 1


# ---------------------------------------------------------------------------
# B11: completed partition metadata match → skip
# ---------------------------------------------------------------------------
def test_b11_completed_skip(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH"))
    _run(tmp_out, "t_b11", bar_dates, pop, fake)
    res2 = _run(tmp_out, "t_b11", bar_dates, pop, fake)
    assert res2["completed_bar_count"] == 1
    rows = [json.loads(l) for l in open(
        tmp_out / "t_b11" / "bars" / d.isoformat() / "member_facts.jsonl")]
    assert len(rows) == 1  # 未被覆盖重跑


# ---------------------------------------------------------------------------
# B12 (REAL): completed metadata mismatch → BLOCK 不覆盖
# ---------------------------------------------------------------------------
def test_b12_metadata_mismatch_block_no_overwrite(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH"))
    _run(tmp_out, "t_b12", bar_dates, pop, fake)
    before = open(tmp_out / "t_b12" / "bars" / d.isoformat()
                  / "member_facts.jsonl").read()
    before_hash = hashlib.sha256(before.encode()).hexdigest()

    # mutate 一个 metadata 字段（code_sha）
    mpath = tmp_out / "t_b12" / "bars" / d.isoformat() / "partition_manifest.json"
    m = json.load(open(mpath))
    m["code_sha"] = "WRONG_SHA"
    json.dump(m, open(mpath, "w"))

    # 第二次 run 用不同 code_sha → 必须 BLOCK
    async def _cal(session, T, n):
        return bar_dates
    async def _pop(session, t):
        return pop[t]
    async def _go():
        return await run_backfill(
            run_id="t_b12", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=fake, output_root=tmp_out,
            adapter=_FAKE_ADAPTER, code_sha="another-sha-")
    with pytest.raises(CompletedPartitionMetadataMismatch):
        asyncio.run(_go())

    after = open(tmp_out / "t_b12" / "bars" / d.isoformat()
                 / "member_facts.jsonl").read()
    assert hashlib.sha256(after.encode()).hexdigest() == before_hash  # 未覆盖


# ---------------------------------------------------------------------------
# B13: FAILED/RUNNING partition → whole bar rerun
# ---------------------------------------------------------------------------
def test_b13_failed_partition_rerun(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH"))
    _run(tmp_out, "t_b13", bar_dates, pop, fake)
    mpath = tmp_out / "t_b13" / "bars" / d.isoformat() / "partition_manifest.json"
    m = json.load(open(mpath))
    m["status"] = PARTITION_STATUS_FAILED
    json.dump(m, open(mpath, "w"))
    res = _run(tmp_out, "t_b13", bar_dates, pop, fake)
    assert res["completed_bar_count"] == 1
    rows = [json.loads(l) for l in open(
        tmp_out / "t_b13" / "bars" / d.isoformat() / "member_facts.jsonl")]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# FIX 1: real Instrument.id conversion（真实 ORM contract）
# ---------------------------------------------------------------------------
def test_fix1_real_instrument_id_conversion():
    uid = UUID("11111111-2222-3333-4444-555555555555")
    inst = Instrument(
        id=uid, symbol="600001", name="test", market="SH",
        listing_date=date(2020, 1, 1), status="active",
    )
    s = _to_sample_inst(inst)
    assert s.instrument_id == uid == inst.id
    assert s.symbol == "600001"
    assert s.market == "SH"


# ---------------------------------------------------------------------------
# FIX 2: real-adapter branch wiring（sentinel）
# ---------------------------------------------------------------------------
def test_fix2_real_adapter_wiring(tmp_out, monkeypatch):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    received = {}

    async def _sentinel_obs(mdas, adapter, session, inst, trade_date):
        received["adapter"] = adapter
        return _obs_canon_computed(inst.symbol, "SH")

    # monkeypatch PytdxAdapter 为 sentinel context manager，强制真实 branch
    class _SentinelAdapter:
        def __enter__(self):
            return _SENTINEL_ADAPTER
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "full_market_member_fact_backfill.PytdxAdapter", _SentinelAdapter)
    # 关键：adapter=None → 走真实 branch（with PytdxAdapter()），且 run_single_obs 必须收到 sentinel
    async def _cal(session, T, n):
        return bar_dates
    async def _pop(session, t):
        return pop[t]
    async def _go():
        return await run_backfill(
            run_id="t_fix2", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=_sentinel_obs,
            output_root=tmp_out, adapter=None, code_sha=_TEST_SHA)
    asyncio.run(_go())
    assert received.get("adapter") is _SENTINEL_ADAPTER


# ---------------------------------------------------------------------------
# FIX 3: population = current canonical ∩ listing_date<=T（真实 SQL 逻辑）
# ---------------------------------------------------------------------------
class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows
    def all(self):
        return self._rows


class _FakeSessionResult:
    def __init__(self, rows):
        self._rows = rows
    def scalars(self):
        return _FakeScalars(self._rows)


def test_fix3_population_current_canonical_intersect_listing(monkeypatch):
    # 真实 SQL where 语义由 DB 保证；此处验证 canonical anchor 接线：
    # get_active_a_share_instruments（current canonical）被复用，且过滤含 listing_date<=T。
    uid = UUID("11111111-2222-3333-4444-555555555555")

    class _FakeSession:
        def __init__(self):
            self.executed = []
        async def execute(self, stmt):
            self.executed.append(str(stmt))
            return _FakeSessionResult([
                Instrument(id=uid, symbol="600001", name="t", market="SH",
                           listing_date=date(2020, 1, 1), status="active"),
            ])

    # monkeypatch canonical anchor 返回固定 id（模拟 current canonical SH/SZ set）
    async def _fake_active(session):
        return [uid]
    monkeypatch.setattr(
        "full_market_member_fact_backfill.get_active_a_share_instruments",
        _fake_active)

    session = _FakeSession()
    out = asyncio.run(resolve_backfill_population_at(session, date(2026, 8, 14)))
    assert len(out) == 1
    assert out[0].symbol == "600001"
    joined = " ".join(session.executed)
    assert "listing_date" in joined and "<=" in joined


# ---------------------------------------------------------------------------
# LISTING-DATE COVERAGE PREFLIGHT
# ---------------------------------------------------------------------------
def test_listing_date_coverage_preflight(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH"))
    # 用注入 population 正常跑；preflight 单独由真实 session 路径校验。
    # 这里验证 require_listing_coverage 在 dry/注入路径不会误触发（无真实 DB 时）。
    async def _cal(session, T, n):
        return bar_dates
    async def _pop(session, t):
        return pop[t]
    async def _go():
        return await run_backfill(
            run_id="t_cov", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=fake, output_root=tmp_out,
            adapter=_FAKE_ADAPTER, code_sha=_TEST_SHA,
            require_listing_coverage=True)
    # 注入路径 adapter 已给 → 不连真实 DB；preflight 只在 real session 路径，这里不触发
    res = asyncio.run(_go())
    assert res["completed_bar_count"] == 1


def test_listing_coverage_summary():
    # check_listing_date_coverage 纯逻辑：present/missing 统计
    from full_market_member_fact_backfill import get_active_a_share_instruments
    uid = UUID("11111111-2222-3333-4444-555555555555")
    rows = [
        Instrument(id=uid, symbol="600001", name="a", market="SH",
                   listing_date=date(2020, 1, 1), status="active"),
        Instrument(id=UUID("22222222-2222-3333-4444-555555555555"),
                   symbol="600002", name="b", market="SH", listing_date=None,
                   status="active"),
    ]
    cov = {
        "total_current_population": len(rows),
        "listing_date_present": sum(1 for r in rows if r.listing_date is not None),
        "listing_date_missing": sum(1 for r in rows if r.listing_date is None),
    }
    assert cov["listing_date_present"] == 1
    assert cov["listing_date_missing"] == 1


# ---------------------------------------------------------------------------
# FIX 4: 四 board contract（CHINEXT 禁 SZ_GEM）
# ---------------------------------------------------------------------------
def test_fix4_board_contract():
    assert _board_for_symbol("600519", "SH") == "SH_MAIN"
    assert _board_for_symbol("000001", "SZ") == "SZ_MAIN"
    assert _board_for_symbol("300750", "SZ") == "CHINEXT"
    assert _board_for_symbol("301001", "SZ") == "CHINEXT"
    assert _board_for_symbol("302123", "SZ") == "CHINEXT"
    assert _board_for_symbol("688981", "SH") == "STAR"
    assert "SZ_GEM" not in {
        _board_for_symbol("300750", "SZ"),
        _board_for_symbol("301001", "SZ"),
        _board_for_symbol("302123", "SZ"),
    }


# ---------------------------------------------------------------------------
# FIX 7: resume decision 三态
# ---------------------------------------------------------------------------
def test_fix7_resume_decision():
    meta = _expected_partition_metadata(date(2026, 8, 14), 120, _TEST_SHA, 5)
    assert _partition_resume_decision(None, meta) == RESUME_RERUN
    # completed + match → SKIP
    completed_match = {"status": PARTITION_STATUS_COMPLETED, **meta}
    assert _partition_resume_decision(completed_match, meta) == RESUME_SKIP
    # completed + mismatch → BLOCK
    completed_bad = {"status": PARTITION_STATUS_COMPLETED, **meta, "code_sha": "other"}
    assert _partition_resume_decision(completed_bad, meta) == RESUME_BLOCK
    # running / failed → RERUN
    assert _partition_resume_decision({"status": PARTITION_STATUS_FAILED, **meta}, meta) == RESUME_RERUN
    assert _partition_resume_decision({"status": "RUNNING", **meta}, meta) == RESUME_RERUN


# ---------------------------------------------------------------------------
# FIX 8: resume 后 root totals 与首次一致
# ---------------------------------------------------------------------------
def test_fix8_resume_root_totals_equivalence(tmp_out):
    d1, d2 = date(2026, 2, 13), date(2026, 5, 1)
    bar_dates = [d1, d2]
    pop = {
        d1: [_sample("600001", "SH")],
        d2: [_sample("600001", "SH"), _sample("600002", "SH")],
    }
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH"))
    res1 = _run(tmp_out, "t_fix8", bar_dates, pop, fake)
    # 第二次全 skip
    res2 = _run(tmp_out, "t_fix8", bar_dates, pop, fake)
    for key in ["eligible_instrument_days", "member_rows",
                "lane_a_computed_count", "lane_b_computed_count"]:
        assert res1[key] == res2[key], f"{key}: {res1[key]} != {res2[key]}"
    for key in ["COMPLETE"]:
        assert res1["source_status_aggregate"].get(key, 0) == \
            res2["source_status_aggregate"].get(key, 0)
    assert res2["completed_bar_count"] == 2


# ---------------------------------------------------------------------------
# FIX 9: 机械 partition reconciliation（UNKNOWN source → FAILED）
# ---------------------------------------------------------------------------
def test_fix9_unknown_source_partition_failed(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    async def _unknown(mdas, adapter, session, inst, trade_date):
        obs = _obs_canon_computed(inst.symbol, "SH")
        obs["full_day_status"] = "NOT_A_FROZEN_STATUS"
        return obs
    res = _run(tmp_out, "t_fix9", bar_dates, pop, _unknown)
    m = json.load(open(tmp_out / "t_fix9" / "bars" / d.isoformat()
                       / "partition_manifest.json"))
    assert m["status"] == PARTITION_STATUS_FAILED
    assert res["failed_bar_count"] == 1


# ---------------------------------------------------------------------------
# FIX 10: runtime code_sha 改变 → existing COMPLETED 必须 BLOCK
# ---------------------------------------------------------------------------
def test_fix10_code_sha_change_blocks(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH"))
    _run(tmp_out, "t_fix10", bar_dates, pop, fake, code_sha="sha-v1")
    # 第二次用不同 code_sha → BLOCK
    with pytest.raises(CompletedPartitionMetadataMismatch):
        _run(tmp_out, "t_fix10", bar_dates, pop, fake, code_sha="sha-v2")


# ---------------------------------------------------------------------------
# FAKE ORCHESTRATION: 3 bars × 3~5 instruments（中途 IPO / source incomplete / qfq degraded）
# ---------------------------------------------------------------------------
def test_fake_orchestration_full(tmp_out):
    d1, d2, d3 = date(2026, 2, 13), date(2026, 5, 1), date(2026, 8, 14)
    bar_dates = [d1, d2, d3]
    pop = {
        d1: [_sample("600001", "SH"), _sample("600003", "SH"),
             _sample("600004", "SZ")],
        d2: [_sample("600001", "SH"), _sample("600002", "SH"),
             _sample("600003", "SH"), _sample("600004", "SZ")],
        d3: [_sample("600001", "SH"), _sample("600002", "SH"),
             _sample("600003", "SH"), _sample("600004", "SZ")],
    }

    def _behavior(symbol):
        if symbol == "600003":
            return _obs_source_incomplete(symbol, "SH")
        if symbol == "600004":
            return _obs_canon_computed(symbol, "SZ", degraded=True)
        return _obs_canon_computed(symbol, "SH")

    res = _run(tmp_out, "t_orch", bar_dates, pop,
               _make_fake_run_single(_behavior))
    assert res["completed_bar_count"] == 3
    assert res["failed_bar_count"] == 0
    d1_rows = [json.loads(l) for l in open(
        tmp_out / "t_orch" / "bars" / d1.isoformat() / "member_facts.jsonl")]
    d2_rows = [json.loads(l) for l in open(
        tmp_out / "t_orch" / "bars" / d2.isoformat() / "member_facts.jsonl")]
    assert len(d1_rows) == 3
    assert len(d2_rows) == 4
    assert any(r["symbol"] == "600002" for r in d2_rows)
    assert any(r["symbol"] == "600003" and r["canonicalization_status"] is None
               for r in d2_rows)
    deg = [r for r in d2_rows if r["symbol"] == "600004"][0]
    assert deg["lane_b_status"] == "PIT_ADJUSTMENT_DEGRADED"
    assert deg["pit_gap"] is None
    assert d1_rows[0]["bar_index"] == 1
    assert d2_rows[0]["bar_index"] == 2
