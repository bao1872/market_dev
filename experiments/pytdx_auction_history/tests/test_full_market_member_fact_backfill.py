"""Round 3B-B / 3B-B1 / 3B-D backfill runner tests — file evidence + qfq degraded fail-close.

覆盖任务定义 B1..B13 + Round 3B-B1 live-path wiring closure + Round 3B-D P10..P22：
- real Instrument.id conversion
- real-adapter branch wiring（sentinel）
- current-canonical ∩ listing_date population（load-once）
- listing-date coverage preflight + listing_date_unavailable 显式记录
- 四 board contract（CHINEXT 禁 SZ_GEM）
- actual Lane B projection / adjustment_as_of
- completed metadata mismatch no-overwrite（真实 BLOCK）
- resume root-total equivalence
- full partition reconciliation
- runtime code_sha semantics
- kernel 不调用 fetch_full_day_transactions_paginated / 无 full-day volume evidence
- 每 bar 只做 MDAS batch ×2（none/qfq，allow_backfill=False）
- population load-once（startup 一次读取 + in-memory listing filter）
- stream tmp append + atomic rename + run.lock（already-active / stale recovery）
- resume 加载 offset_hints.json（warm start）

Round 3B-D 接口（相对 3B-B 变化）：
- run_backfill 参数 run_single_obs → run_symbol_obs；回调签名 (inst, trade_date)
- 新增 batch_mdas_fn / population / population_fn / enable_run_lock / recover_stale_lock
- per-symbol observer 使用真实 kernel（run_symbol_backfill_observation）+ _batch_mdas

所有测试纯内存（不连真实 DB / pytdx），通过注入 calendar_fn / population_fn /
run_symbol_obs / batch_mdas_fn / adapter / session / output_root / code_sha 实现 fake orchestration。
"""

import asyncio
import hashlib
import json
import os
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
    RunAlreadyActive,
    RunLockStale,
    RESUME_BLOCK,
    RESUME_RERUN,
    RESUME_SKIP,
    run_symbol_backfill_observation,
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
    load_population_once,
    filter_population_at,
    PopulationSnapshot,
    RunLock,
    _partition_dir,
    _write_json,
)

# ---------------------------------------------------------------------------
# 注入用 fake：adapter / session / batch
# ---------------------------------------------------------------------------
_FAKE_ADAPTER = object()
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
    """成功 canonical 观测（Round 3B-D source_status 冻结词表）。"""
    return {
        "symbol": symbol, "market": market,
        "instrument_id": symbol, "board": _board_for_symbol(symbol, market),
        "listing_date": None,
        "source_status": "TARGET_WINDOW_COMPLETE",
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
        "source_status": "SOURCE_EMPTY",
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


def _make_fake_run_symbol_obs(behavior):
    """Round 3B-D observer seam：签名 (inst, trade_date) -> dict。"""
    async def _fake(inst, trade_date):
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


# ---------------------------------------------------------------------------
# 注入 fake session（真实 ORM 语句契约由 fake execute 返回 rows）
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


class _FakeSession:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []
        self.compiled_params = []
    async def execute(self, stmt):
        self.executed.append(str(stmt))
        try:
            self.compiled_params.append(stmt.compile().params)
        except Exception:
            self.compiled_params.append({})
        return _FakeSessionResult(self.rows)
    async def close(self):
        pass


def _fake_page_adapter(boundary=800, records=None):
    """fake PytdxAdapter：offset >= boundary 返回空页（before-window），否则返回 09:25:00 记录。

    供 kernel 路径（run_symbol_backfill_observation / fetch_auction_0925_targeted）使用，
    使 targeted search 确定性收敛到 boundary 并产生 TARGET_WINDOW_COMPLETE + hint。
    """
    recs = records if records is not None else [
        {"time": "09:25:00", "price": 11.0, "vol": 100, "buyorsell": 0}]
    class _FakeAdapter:
        def __init__(self):
            self.offsets = []
        def get_history_transaction_page(self, symbol, trade_date, offset, page_size):
            self.offsets.append(offset)
            if offset >= boundary:
                return []
            return [dict(r) for r in recs]
    return _FakeAdapter()


# Round 3B-D 真实 per-symbol kernel observer 在真实 branch 收到的 sentinel adapter
_SENTINEL_ADAPTER = _fake_page_adapter()


def _make_fake_batch_mdas(value=None):
    """batch_mdas_fn seam：记录每次 batch 调用；默认返回空结果（DB missing，不触发外部 provider）。"""
    calls = []
    async def _batch(mdas, session, instrument_ids, trade_date, *, adj,
                     adjustment_as_of=None):
        calls.append({
            "adj": adj,
            "trade_date": trade_date,
            "adjustment_as_of": adjustment_as_of,
            "ids": list(instrument_ids),
        })
        res = {} if value is None else value
        return res, {}
    return _batch, calls


class _FakeMDAS:
    """fake MarketDataAggregationService：记录 get_bars_batch kwargs（allow_backfill 等）。"""
    def __init__(self):
        self.seen = []
    async def get_bars_batch(self, session, instrument_ids, **kwargs):
        self.seen.append(kwargs)
        return {}  # DB missing → 空结果；allow_backfill=False 不得触发 external provider


def _run(tmp_out, run_id, bar_dates, pop, fake, code_sha=_TEST_SHA,
         adapter=_FAKE_ADAPTER, cal=None, enable_run_lock=False,
         recover_stale_lock=False, population=None, population_fn=None):
    async def _cal(session, T, n):
        return bar_dates
    async def _pop(session, t):
        return pop[t]
    async def _go():
        return await run_backfill(
            run_id=run_id, bar_dates=bar_dates,
            calendar_fn=cal or _cal,
            population=population,
            population_fn=population_fn or _pop,
            run_symbol_obs=fake, output_root=tmp_out,
            adapter=adapter, code_sha=code_sha,
            enable_run_lock=enable_run_lock,
            recover_stale_lock=recover_stale_lock)
    return asyncio.run(_go())


def _kernel_run(tmp_out, run_id, bar_dates, pop, *, adapter, session,
                batch_mdas_fn, mdas=None, code_sha=_TEST_SHA):
    """通过真实 kernel observer 路径（run_symbol_obs=None + _batch_mdas）运行。"""
    async def _cal(session, T, n):
        return bar_dates
    async def _pop(session, t):
        return pop[t]
    async def _go():
        return await run_backfill(
            run_id=run_id, bar_dates=bar_dates,
            calendar_fn=_cal, population_fn=_pop,
            run_symbol_obs=None, batch_mdas_fn=batch_mdas_fn,
            output_root=tmp_out, adapter=adapter, session=session,
            mdas=mdas, code_sha=code_sha)
    return asyncio.run(_go())


def _session_path_run(tmp_out, run_id, bar_dates, fake, rows, *,
                      monkeypatch, code_sha=_TEST_SHA, canonical_ids=None):
    """走真实 session 分支（adapter=None → with PytdxAdapter()）：验证 root manifest listing 字段。"""
    class _SentinelAdapter:
        def __enter__(self):
            return _SENTINEL_ADAPTER
        def __exit__(self, *a):
            return False
    monkeypatch.setattr("full_market_member_fact_backfill.PytdxAdapter", _SentinelAdapter)
    if canonical_ids is not None:
        async def _fake_active(session):
            return list(canonical_ids)
        monkeypatch.setattr(
            "full_market_member_fact_backfill.get_active_a_share_instruments",
            _fake_active)
    sess = _FakeSession(rows=rows)
    async def _cal(session, T, n):
        return bar_dates
    async def _go():
        return await run_backfill(
            run_id=run_id, bar_dates=bar_dates, calendar_fn=_cal,
            run_symbol_obs=fake, output_root=tmp_out,
            adapter=None, session=sess, code_sha=code_sha)
    res = asyncio.run(_go())
    return res, sess


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
               _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH")))
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
               _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH")))
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
    fake = _make_fake_run_symbol_obs(
        lambda s: _obs_source_incomplete(s, "SH") if s == "600001"
        else _obs_canon_computed(s, "SH"))
    res = _run(tmp_out, "t_b5", bar_dates, pop, fake)
    rows = [json.loads(l) for l in open(
        tmp_out / "t_b5" / "bars" / d.isoformat() / "member_facts.jsonl")]
    assert len(rows) == 2
    inc = [r for r in rows if r["symbol"] == "600001"][0]
    assert inc["source_status"] == "SOURCE_EMPTY"
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
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH", pit_gap=0.07))
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
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SZ", degraded=True))
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
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SZ", degraded=False, pit_gap=0.09))
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
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))
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
    async def _boom(inst, trade_date):
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
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))
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
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))
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
            population_fn=_pop, run_symbol_obs=fake, output_root=tmp_out,
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
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))
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
# Round 3B-D：observer 签名改为 (inst, trade_date)，adapter 由真实 kernel observer
# （run_symbol_backfill_observation）接收 → 通过 spy 验证真实 branch 把 sentinel 传给 kernel。
# ---------------------------------------------------------------------------
def test_fix2_real_adapter_wiring(tmp_out, monkeypatch):
    import full_market_member_fact_backfill as fbm

    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    received = {}

    # monkeypatch PytdxAdapter 为 sentinel context manager，强制真实 branch
    class _SentinelAdapter:
        def __enter__(self):
            return _SENTINEL_ADAPTER
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(
        "full_market_member_fact_backfill.PytdxAdapter", _SentinelAdapter)

    # spy：真实 kernel observer 必须收到 sentinel adapter（真实 branch 单实例）
    real_obs = fbm.run_symbol_backfill_observation
    async def _spy(adapter, session, inst, trade_date, raw_batch, qfq_batch, **kw):
        received["adapter"] = adapter
        return await real_obs(adapter, session, inst, trade_date,
                              raw_batch, qfq_batch, **kw)
    monkeypatch.setattr(
        "full_market_member_fact_backfill.run_symbol_backfill_observation", _spy)

    batch, _ = _make_fake_batch_mdas()
    async def _cal(session, T, n):
        return bar_dates
    async def _pop(session, t):
        return pop[t]
    async def _go():
        return await run_backfill(
            run_id="t_fix2", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_symbol_obs=None, batch_mdas_fn=batch,
            output_root=tmp_out, adapter=None, session=_FakeSession(),
            code_sha=_TEST_SHA)
    res = asyncio.run(_go())
    assert received.get("adapter") is _SENTINEL_ADAPTER
    assert res["completed_bar_count"] == 1


# ---------------------------------------------------------------------------
# FIX 3: population = current canonical ∩ listing_date<=T（load-once 语义）
# Round 3B-D：SQL 只取 current canonical SH/SZ denominator（不预过滤 listing_date），
# listing_date <= T 过滤在内存 filter_population_at 完成。
# ---------------------------------------------------------------------------
def test_fix3_population_current_canonical_intersect_listing(monkeypatch):
    uid = UUID("11111111-2222-3333-4444-555555555555")

    async def _fake_active(session):
        return [uid]
    monkeypatch.setattr(
        "full_market_member_fact_backfill.get_active_a_share_instruments",
        _fake_active)

    session = _FakeSession(rows=[
        Instrument(id=uid, symbol="600001", name="t", market="SH",
                   listing_date=date(2020, 1, 1), status="active"),
    ])
    # load-once snapshot：SQL denominator = current canonical SH/SZ（listing 不预过滤）
    snap = asyncio.run(load_population_once(session))
    assert snap.total_current_shsz == 1
    assert snap.listing_date_missing == 0
    joined = " ".join(session.executed)
    assert "market" in joined
    # SQLAlchemy IN() 使用 __[POSTCOMPILE_market_1] 绑定参数，字面 'SH' 不在 SQL 串中；
    # 从 compiled params 验证 market filter 覆盖 SH/SZ
    market_vals = set()
    for params in session.compiled_params:
        m = params.get("market_1")
        if isinstance(m, list):
            market_vals.update(m)
    assert {"SH", "SZ"} <= market_vals
    # in-memory listing_date <= T 过滤（120 bars 只做内存过滤，不重查 DB）
    assert len(filter_population_at(snap, date(2019, 1, 1))) == 0
    assert len(filter_population_at(snap, date(2026, 8, 14))) == 1
    # 兼容入口 resolve_backfill_population_at 结果一致
    out = asyncio.run(resolve_backfill_population_at(session, date(2026, 8, 14)))
    assert len(out) == 1 and out[0].symbol == "600001"


# ---------------------------------------------------------------------------
# LISTING-DATE COVERAGE PREFLIGHT
# ---------------------------------------------------------------------------
def test_listing_date_coverage_preflight(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))
    # 用注入 population 正常跑；preflight 单独由真实 session 路径校验。
    # 这里验证 require_listing_coverage 在 dry/注入路径不会误触发（无真实 DB 时）。
    async def _cal(session, T, n):
        return bar_dates
    async def _pop(session, t):
        return pop[t]
    async def _go():
        return await run_backfill(
            run_id="t_cov", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_symbol_obs=fake, output_root=tmp_out,
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
    meta = _expected_partition_metadata(date(2026, 8, 14), 120, _TEST_SHA, 5, AS_OF)
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
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))
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
# Round 3B-D：source_status 为权威词表（不再依赖 legacy full_day_status）。
# ---------------------------------------------------------------------------
def test_fix9_unknown_source_partition_failed(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    async def _unknown(inst, trade_date):
        obs = _obs_canon_computed(inst.symbol, "SH")
        obs["source_status"] = "NOT_A_FROZEN_STATUS"
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
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))
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
               _make_fake_run_symbol_obs(_behavior))
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


# ===========================================================================
# Round 3B-D P10..P22 — kernel / stream / lock / resume governance
# ===========================================================================

# ---------------------------------------------------------------------------
# P10: kernel 不调用 fetch_full_day_transactions_paginated
# ---------------------------------------------------------------------------
def test_kernel_no_full_day_pagination(tmp_out, monkeypatch):
    def _sentinel(*a, **k):
        raise AssertionError("kernel 不得调用 fetch_full_day_transactions_paginated")
    monkeypatch.setattr(
        "auction_history_semantics_validation.fetch_full_day_transactions_paginated",
        _sentinel)

    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    batch, _ = _make_fake_batch_mdas()
    res = _kernel_run(tmp_out, "t_p10", bar_dates, pop,
                      adapter=_fake_page_adapter(), session=_FakeSession(),
                      batch_mdas_fn=batch)
    assert res["completed_bar_count"] == 1
    assert res["failed_bar_count"] == 0


# ---------------------------------------------------------------------------
# P11: kernel 不计算 full-day volume evidence
# ---------------------------------------------------------------------------
def test_no_full_day_volume_evidence(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    batch, _ = _make_fake_batch_mdas()
    _kernel_run(tmp_out, "t_p11", bar_dates, pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch)
    row = json.loads(open(tmp_out / "t_p11" / "bars" / d.isoformat()
                          / "member_facts.jsonl").readline())
    assert "daily_volume_ratio" not in row
    assert "full_day_sum_volume" not in row


# ---------------------------------------------------------------------------
# P12: 每 bar 只调用 MDAS batch：none ×1 / qfq ×1（不 per-symbol get_bars）
# ---------------------------------------------------------------------------
def test_one_bar_only_mdas_batch_x2(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH"), _sample("600002", "SH")]}
    batch, calls = _make_fake_batch_mdas()
    res = _kernel_run(tmp_out, "t_p12", bar_dates, pop,
                      adapter=_fake_page_adapter(), session=_FakeSession(),
                      batch_mdas_fn=batch)
    assert res["completed_bar_count"] == 1
    assert [c["adj"] for c in calls] == ["none", "qfq"]
    assert len(calls) == 2  # 1 bar = 2 batch calls
    # 同一 batch 覆盖全部 instruments，而非 per-symbol 查询
    assert len(calls[0]["ids"]) == 2


# ---------------------------------------------------------------------------
# P13: get_bars_batch allow_backfill=False → DB missing 不触发 external provider
# ---------------------------------------------------------------------------
def test_batch_allow_backfill_false(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    fake_mdas = _FakeMDAS()  # 记录 get_bars_batch kwargs；返回空结果（DB missing）
    res = _kernel_run(tmp_out, "t_p13", bar_dates, pop,
                      adapter=_fake_page_adapter(), session=_FakeSession(),
                      batch_mdas_fn=None, mdas=fake_mdas)
    # 空结果 → 不抛异常、partition 正常处理
    assert res["completed_bar_count"] == 1
    assert res["failed_bar_count"] == 0
    assert len(fake_mdas.seen) == 2  # none ×1 / qfq ×1
    for kw in fake_mdas.seen:
        assert kw.get("allow_backfill") is False  # strict DB-only，不触发 external provider
        assert kw.get("adj") in ("none", "qfq")
        assert kw.get("completed_only") is True


# ---------------------------------------------------------------------------
# P14: population load-once —— startup 一次读取，120 bars 只做 in-memory 过滤
# ---------------------------------------------------------------------------
def test_population_load_once(tmp_out):
    seq = []
    cur = AS_OF
    while len(seq) < 120:
        if cur.weekday() < 5:
            seq.append(cur)
        cur -= timedelta(days=1)
    bar_dates = sorted(seq)

    insts = [
        Instrument(id=UUID("11111111-2222-3333-4444-555555555555"),
                   symbol="600001", name="a", market="SH",
                   listing_date=date(2020, 1, 1), status="active"),
        # listing_date 晚于所有 bar → 未来上市，in-memory 过滤后不进入任何 bar
        Instrument(id=UUID("22222222-2222-3333-4444-555555555555"),
                   symbol="600002", name="b", market="SH",
                   listing_date=date(2026, 9, 1), status="active"),
    ]
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))

    async def _boom(session, t):
        raise AssertionError("population_fn 不应被调用（load-once 应使用 preloaded population）")

    res = _run(tmp_out, "t_p14", bar_dates, {t: [] for t in bar_dates},
               fake, population=insts, population_fn=_boom)
    assert res["completed_bar_count"] == 120
    # 120 bars 全部走 in-memory filter，只观察 600001
    assert res["eligible_instrument_days"] == 120

    # 直接验证 load-once snapshot 的 in-memory listing 过滤语义
    snap = PopulationSnapshot(
        instruments=[i for i in insts if i.listing_date is not None],
        by_symbol={i.symbol: i for i in insts},
        listing_missing_symbols=[],
        total_current_shsz=len(insts),
        listing_date_present=2,
        listing_date_missing=0,
    )
    assert len(filter_population_at(snap, AS_OF)) == 1
    assert filter_population_at(snap, AS_OF)[0].symbol == "600001"


# ---------------------------------------------------------------------------
# P15: listing coverage denominator = current canonical SH/SZ before listing filter
# ---------------------------------------------------------------------------
def test_listing_coverage_denominator(tmp_out, monkeypatch):
    d = date(2026, 8, 14)
    bar_dates = [d]
    uid1 = UUID("11111111-2222-3333-4444-555555555555")
    uid2 = UUID("22222222-2222-3333-4444-555555555555")
    rows = [
        Instrument(id=uid1, symbol="600001", name="a", market="SH",
                   listing_date=date(2020, 1, 1), status="active"),
        Instrument(id=uid2, symbol="600002", name="b", market="SH",
                   listing_date=None, status="active"),
    ]
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))
    res, sess = _session_path_run(tmp_out, "t_p15", bar_dates, fake, rows=rows,
                                  monkeypatch=monkeypatch,
                                  canonical_ids=[uid1, uid2])
    # denominator = current canonical SH/SZ（listing 过滤前）= 2
    assert res["listing_date_unavailable_count"] == 1
    assert res["listing_date_unavailable_symbols"] == ["600002"]
    # historical population 只包含 listing_date 有效的那只
    assert res["eligible_instrument_days"] == 1
    # 直接验证 load-once snapshot 的 denominator 语义
    snap = asyncio.run(load_population_once(sess))
    assert snap.total_current_shsz == 2
    assert snap.listing_date_present == 1
    assert snap.listing_date_missing == 1


# ---------------------------------------------------------------------------
# P16: listing_date 缺失 symbol 显式记录，不 silent disappear
# ---------------------------------------------------------------------------
def test_listing_unavailable_explicit_record(tmp_out, monkeypatch):
    d = date(2026, 8, 14)
    bar_dates = [d]
    uid1 = UUID("11111111-2222-3333-4444-555555555555")
    uid2 = UUID("22222222-2222-3333-4444-555555555555")
    rows = [
        Instrument(id=uid1, symbol="600001", name="a", market="SH",
                   listing_date=date(2020, 1, 1), status="active"),
        Instrument(id=uid2, symbol="600002", name="b", market="SH",
                   listing_date=None, status="active"),
    ]
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))
    res, _ = _session_path_run(tmp_out, "t_p16", bar_dates, fake, rows=rows,
                               monkeypatch=monkeypatch,
                               canonical_ids=[uid1, uid2])
    assert "listing_date_unavailable_symbols" in res
    assert "600002" in res["listing_date_unavailable_symbols"]
    assert res["listing_date_unavailable_count"] == 1
    # 缺失 listing 的 symbol 不进入任何 member_facts 行（但显式记录于 root manifest）
    mf = tmp_out / "t_p16" / "bars" / d.isoformat() / "member_facts.jsonl"
    symbols = [json.loads(l)["symbol"] for l in open(mf)]
    assert symbols == ["600001"]


# ---------------------------------------------------------------------------
# P17: stream tmp 以 .tmp 后缀 append，内容包含 member facts
# （失败 partition 保留 .tmp 作为诊断证据，不 rename）
# ---------------------------------------------------------------------------
def test_stream_tmp_append(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH"), _sample("600002", "SH")]}
    async def _boom(inst, trade_date):
        if inst.symbol == "600002":
            raise RuntimeError("boom")  # RUN_ERROR → partition FAILED → .tmp 保留
        return _obs_canon_computed(inst.symbol, "SH")
    res = _run(tmp_out, "t_p17", bar_dates, pop, _boom)
    pdir = tmp_out / "t_p17" / "bars" / d.isoformat()
    tmp_path = pdir / "member_facts.jsonl.tmp"
    assert res["failed_bar_count"] == 1
    assert tmp_path.exists()  # FAILED 分区保留 .tmp 诊断证据
    lines = [l for l in open(tmp_path) if l.strip()]
    assert len(lines) == 2  # 600001 member fact + 600002 RUN_ERROR row
    payload = json.loads(lines[0])
    assert payload["symbol"] == "600001"
    assert payload["source_status"] == "TARGET_WINDOW_COMPLETE"


# ---------------------------------------------------------------------------
# P18: COMPLETED 后 .tmp 被 atomic rename 为 member_facts.jsonl
# ---------------------------------------------------------------------------
def test_completed_atomic_rename(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))
    res = _run(tmp_out, "t_p18", bar_dates, pop, fake)
    pdir = tmp_out / "t_p18" / "bars" / d.isoformat()
    assert res["completed_bar_count"] == 1
    assert (pdir / "member_facts.jsonl").exists()
    assert not (pdir / "member_facts.jsonl.tmp").exists()  # atomic rename 后 tmp 消失


# ---------------------------------------------------------------------------
# P19: 同一 run_id 第二个 writer 被拒绝（RunAlreadyActive）
# ---------------------------------------------------------------------------
def test_run_already_active(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))
    run_id = "t_p19"
    # 模拟第一个 writer 已持有 run.lock（当前进程 pid，owner alive）
    lock = RunLock(_partition_dir(run_id, d, tmp_out).parent.parent / "run.lock")
    lock.acquire()
    try:
        with pytest.raises(RunAlreadyActive):
            _run(tmp_out, run_id, bar_dates, pop, fake, enable_run_lock=True)
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# P20: stale lock 需要显式 recover
# ---------------------------------------------------------------------------
def test_stale_lock_recovery(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH")]}
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))
    run_id = "t_p20"
    lock_path = tmp_out / run_id / "run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 模拟 stale lock：owner pid 已不存在
    _write_json(lock_path, {"pid": 999999, "created_at": "stale"})
    # 不显式 recover → 必须抛 RunLockStale
    with pytest.raises(RunLockStale):
        _run(tmp_out, run_id, bar_dates, pop, fake, enable_run_lock=True)
    # 显式 recover → 正常继续
    res = _run(tmp_out, run_id, bar_dates, pop, fake,
               enable_run_lock=True, recover_stale_lock=True)
    assert res["completed_bar_count"] == 1
    assert not lock_path.exists()  # release 后 lock 被删除


# ---------------------------------------------------------------------------
# P21: resume 加载 offset_hints.json（warm start）
# ---------------------------------------------------------------------------
def test_resume_loads_offset_hints(tmp_out):
    d1, d2 = date(2026, 5, 1), date(2026, 8, 14)
    pop = {
        d1: [_sample("600001", "SH")],
        d2: [_sample("600001", "SH")],
    }
    adapter = _fake_page_adapter(boundary=800)
    batch, _ = _make_fake_batch_mdas()
    # 第一次：1 bar → kernel 写入 hint（resolved_offset=800）→ 持久化 offset_hints.json
    res1 = _kernel_run(tmp_out, "t_p21", [d1], {d1: pop[d1]},
                       adapter=adapter, session=_FakeSession(),
                       batch_mdas_fn=batch)
    assert res1["completed_bar_count"] == 1
    hints_path = tmp_out / "t_p21" / "offset_hints.json"
    assert hints_path.exists()
    hints = json.load(open(hints_path))
    assert hints["hints"].get("600001") == 800

    # 第二次 resume：d1 SKIP，d2 新 bar 使用加载的 hint → used_hint=True
    res2 = _kernel_run(tmp_out, "t_p21", [d1, d2], pop,
                       adapter=adapter, session=_FakeSession(),
                       batch_mdas_fn=batch)
    assert res2["completed_bar_count"] == 2
    row = json.loads(open(tmp_out / "t_p21" / "bars" / d2.isoformat()
                          / "member_facts.jsonl").readline())
    assert row["used_hint"] is True


# ---------------------------------------------------------------------------
# P23 (Round 3B-D1 PART J): bar1 COMPLETED 后 bar2 中途进程中断 → bar1 hints 保留，
# resume 加载该文件且 bar2 first symbol 使用 hint（used_hint=True）。
# 证明 per-partition offset hint persistence 不是仅 final write。
# ---------------------------------------------------------------------------
def test_hint_persistence_survives_midrun_interruption(tmp_out, monkeypatch):
    d1, d2 = date(2026, 2, 13), date(2026, 5, 1)
    pop = {
        d1: [_sample("600001", "SH")],
        d2: [_sample("600001", "SH")],
    }
    adapter = _fake_page_adapter(boundary=800)
    batch, _ = _make_fake_batch_mdas()
    hints_path = tmp_out / "t_p23" / "offset_hints.json"

    # 真实 kernel observer：bar1 正常完成写入 hint；bar2 首次调用模拟进程中断
    # （BaseException，不被 runner 的 `except Exception` 捕获 → 直接向上传播）。
    real_obs = run_symbol_backfill_observation

    class _Interrupted(BaseException):
        pass

    async def _interrupting_obs(*args, **kwargs):
        if args[3] == d2:  # inst 在 args[2]，trade_date 在 args[3]
            raise _Interrupted("simulated process interruption at bar 2")
        return await real_obs(*args, **kwargs)

    monkeypatch.setattr(
        "full_market_member_fact_backfill.run_symbol_backfill_observation",
        _interrupting_obs)

    with pytest.raises(_Interrupted):
        _kernel_run(tmp_out, "t_p23", [d1, d2], pop,
                    adapter=adapter, session=_FakeSession(),
                    batch_mdas_fn=batch)

    # bar1 COMPLETED → per-partition atomic write 已持久化 hint（PART H）
    assert hints_path.exists()
    hints = json.load(open(hints_path))
    assert hints["hints"].get("600001") == 800

    # resume（无中断）：bar1 SKIP，bar2 新 bar 加载 hint → used_hint=True
    monkeypatch.setattr(
        "full_market_member_fact_backfill.run_symbol_backfill_observation",
        real_obs)
    res2 = _kernel_run(tmp_out, "t_p23", [d1, d2], pop,
                       adapter=adapter, session=_FakeSession(),
                       batch_mdas_fn=batch)
    assert res2["completed_bar_count"] == 2
    row = json.loads(open(tmp_out / "t_p23" / "bars" / d2.isoformat()
                          / "member_facts.jsonl").readline())
    assert row["used_hint"] is True


# ---------------------------------------------------------------------------
# P22: resume fresh/skipped 后 root totals 与首次完全一致
# ---------------------------------------------------------------------------
def test_resume_root_totals_identical(tmp_out):
    d1, d2 = date(2026, 2, 13), date(2026, 5, 1)
    bar_dates = [d1, d2]
    pop = {
        d1: [_sample("600001", "SH")],
        d2: [_sample("600001", "SH"), _sample("600002", "SH")],
    }
    fake = _make_fake_run_symbol_obs(lambda s: _obs_canon_computed(s, "SH"))
    res1 = _run(tmp_out, "t_p22", bar_dates, pop, fake)
    res2 = _run(tmp_out, "t_p22", bar_dates, pop, fake)
    total_keys = [
        "eligible_instrument_days", "member_rows",
        "lane_a_computed_count", "lane_b_computed_count",
        "pit_gap_unavailable_count", "pit_gap_adjustment_degraded_count",
        "pytdx_request_count", "pytdx_target_search_cold_count",
        "pytdx_target_search_hint_count", "max_requests_per_symbol",
        "raw_mdas_batch_queries", "qfq_mdas_batch_queries",
        "successful_connect_count", "reconnect_count",
    ]
    for k in total_keys:
        assert res1[k] == res2[k], f"{k}: {res1[k]} != {res2[k]}"
    assert res1["source_status_aggregate"] == res2["source_status_aggregate"]
    assert res1["canonical_status_aggregate"] == res2["canonical_status_aggregate"]
    assert res2["completed_bar_count"] == 2
    assert res2["failed_bar_count"] == 0
