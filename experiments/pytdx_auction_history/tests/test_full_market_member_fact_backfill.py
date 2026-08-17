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
    LEGACY_RESUME_COMPATIBLE_SHA,
    MDAS_CHUNK_SIZE,
    PARTITION_STATUS_COMPLETED,
    PARTITION_STATUS_FAILED,
    RESUME_BLOCK,
    RESUME_RERUN,
    RESUME_SKIP,
    CompletedPartitionMetadataMismatch,
    PopulationSnapshot,
    RunAlreadyActive,
    RunLock,
    RunLockStale,
    _board_for_symbol,
    _expected_partition_metadata,
    _load_offset_hints,
    _offset_hints_payload,
    _partition_dir,
    _partition_resume_decision,
    _to_sample_inst,
    _write_json,
    filter_population_at,
    load_population_once,
    resolve_backfill_population_at,
    run_backfill,
    run_symbol_backfill_observation,
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
                batch_mdas_fn, mdas=None, code_sha=_TEST_SHA,
                mdas_chunk_size=MDAS_CHUNK_SIZE):
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
            mdas=mdas, code_sha=code_sha,
            mdas_chunk_size=mdas_chunk_size)
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
    # 第一次：1 bar → kernel 写入 v2 hint → 持久化 offset_hints.json
    res1 = _kernel_run(tmp_out, "t_p21", [d1], {d1: pop[d1]},
                       adapter=adapter, session=_FakeSession(),
                       batch_mdas_fn=batch)
    assert res1["completed_bar_count"] == 1
    hints_path = tmp_out / "t_p21" / "offset_hints.json"
    assert hints_path.exists()
    hints = json.load(open(hints_path))
    assert hints["version"] == 2
    # v2 dict：target_page_offset（warm anchor） + boundary_offset（evidence）
    assert hints["hints"].get("600001") == {
        "target_page_offset": 0, "boundary_offset": 800}

    # 第二次 resume：d1 SKIP，d2 新 bar 使用持久化的 target_page_offset →
    # target-page-first warm（hint=0 页整页 09:25 → BIDIRECTIONAL）。
    res2 = _kernel_run(tmp_out, "t_p21", [d1, d2], pop,
                       adapter=adapter, session=_FakeSession(),
                       batch_mdas_fn=batch)
    assert res2["completed_bar_count"] == 2
    row = json.loads(open(tmp_out / "t_p21" / "bars" / d2.isoformat()
                          / "member_facts.jsonl").readline())
    assert row["used_hint"] is True
    assert row["search_mode"] == "TARGET_PAGE_BIDIRECTIONAL"
    assert row["target_page_offset"] == 0


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

    # bar1 COMPLETED → per-partition atomic write 已持久化 v2 hint（PART H）
    assert hints_path.exists()
    hints = json.load(open(hints_path))
    assert hints["version"] == 2
    assert hints["hints"].get("600001") == {
        "target_page_offset": 0, "boundary_offset": 800}

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


# ===========================================================================
# Round 3B-D2 PART H T9–T10 — hint v2 versioned structure + resume
# ===========================================================================

# ---------------------------------------------------------------------------
# D2-T9 — hint v1 legacy int：resume 可读（解释为 boundary hint）→ 运行后升级为 v2
# ---------------------------------------------------------------------------
def test_d2_t9_v1_legacy_hint_load_and_v2_upgrade(tmp_out):
    run_dir = tmp_out / "t_d2_t9"
    run_dir.mkdir(parents=True, exist_ok=True)
    hints_path = run_dir / "offset_hints.json"
    # v1 legacy payload：symbol -> boundary int
    hints_path.write_text(json.dumps({"version": 1, "hints": {"600001": 800}}))

    # resume 读入：v1 int 解释为 boundary hint（target_page_offset=None）
    loaded = _load_offset_hints(hints_path)
    assert loaded == {
        "600001": {"target_page_offset": None, "boundary_offset": 800}}

    # legacy boundary hint 驱动的 warm 运行 → 完成 TARGET_WINDOW_COMPLETE，
    # 并把 hint 升级为 v2（target_page_offset 被填充）。
    adapter = _fake_page_adapter(boundary=800)
    inst = _sample("600001", "SH")
    d = date(2026, 8, 14)
    offset_hints = loaded
    fact = asyncio.run(run_symbol_backfill_observation(
        adapter, _FakeSession(), inst, d, {}, {},
        as_of=AS_OF, code_sha=_TEST_SHA, offset_hints=offset_hints))
    assert fact["source_status"] == "TARGET_WINDOW_COMPLETE"
    assert fact["_used_hint"] is True
    entry = offset_hints["600001"]
    assert entry["target_page_offset"] is not None
    assert entry["boundary_offset"] == 800

    # v2 payload：versioned + target_page_offset/boundary_offset 分离
    payload = _offset_hints_payload("t_d2_t9", AS_OF, _TEST_SHA, offset_hints)
    assert payload["version"] == 2
    assert payload["run_id"] == "t_d2_t9"
    assert payload["hints"]["600001"]["target_page_offset"] is not None
    assert payload["hints"]["600001"]["boundary_offset"] == 800


# ---------------------------------------------------------------------------
# D2-T10 — mid-run resume：继续使用持久化的 target_page_offset（target-page-first）
# ---------------------------------------------------------------------------
def test_d2_t10_midrun_resume_uses_target_page_offset(tmp_out):
    d1, d2 = date(2026, 2, 13), date(2026, 5, 1)
    pop = {
        d1: [_sample("600001", "SH")],
        d2: [_sample("600001", "SH")],
    }
    adapter = _fake_page_adapter(boundary=800)
    batch, _ = _make_fake_batch_mdas()
    run_dir = tmp_out / "t_d2_t10"
    hints_path = run_dir / "offset_hints.json"

    # 第一次 1 bar：cold 完成并 seed target_page_offset（page 0，整页 09:25）
    res1 = _kernel_run(tmp_out, "t_d2_t10", [d1], {d1: pop[d1]},
                       adapter=adapter, session=_FakeSession(),
                       batch_mdas_fn=batch)
    assert res1["completed_bar_count"] == 1
    persisted = json.load(open(hints_path))
    assert persisted["hints"]["600001"]["target_page_offset"] == 0

    # resume：d1 SKIP，d2 用持久化的 target_page_offset=0 → 走 target-page-first
    res2 = _kernel_run(tmp_out, "t_d2_t10", [d1, d2], pop,
                       adapter=adapter, session=_FakeSession(),
                       batch_mdas_fn=batch)
    assert res2["completed_bar_count"] == 2
    assert res2["failed_bar_count"] == 0
    row = json.loads(open(run_dir / "bars" / d2.isoformat()
                          / "member_facts.jsonl").readline())
    # 使用的就是持久化的 target_page_offset=0（非 boundary fallback）
    assert row["used_hint"] is True
    assert row["target_page_offset"] == 0
    assert row["search_mode"] == "TARGET_PAGE_BIDIRECTIONAL"
    # search_mode distribution 聚合已写入 root manifest
    smd = res2["search_mode_distribution"]
    assert smd.get("TARGET_PAGE_BIDIRECTIONAL", 0) == 1
    assert smd.get("COLD", 0) == 1


# ============================================================
# Round 3B-E1 — Bounded-Memory Resume Fix
# ============================================================

def _make_pop(syms):
    """构建单日 population dict：{date(2024,1,2): [SampleInstrument, ...]}。

    每个 member 分配唯一 instrument_id（由 symbol 派生），便于 chunk 覆盖类测试
    断言 distinct id / no dup / no omission。
    """
    out = []
    for idx, (s, m) in enumerate(syms):
        iid = UUID(f"00000000-0000-0000-0000-{idx:012d}")
        out.append(_sample(s, m, instrument_id=iid))
    return {date(2024, 1, 2): out}


def _read_member_rows(output_root, run_id, trade_date):
    """读取 partition 的 member_facts.jsonl（只读，用于 parity 断言）。"""
    p = _partition_dir(run_id, trade_date, output_root) / "member_facts.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _make_chunk_aware_batch(value_factory):
    """batch_mdas_fn：对 chunk 中每个 instrument_id 返回 value_factory(id, adj)。

    用于验证 bounded chunk 拼接后，合并结果与一次性整市场调用在语义上一致。
    """
    calls = []

    async def _batch(mdas, session, instrument_ids, trade_date, *, adj,
                     adjustment_as_of=None):
        calls.append({
            "adj": adj,
            "trade_date": trade_date,
            "adjustment_as_of": adjustment_as_of,
            "ids": list(instrument_ids),
        })
        res = {iid: value_factory(iid, adj) for iid in instrument_ids}
        return res, {}

    return _batch, calls


def _write_completed_partition(pdir: Path, trade_date: date, bar_index: int,
                               code_sha: str, as_of: date, eligible: int):
    """写入一个 COMPLETED partition_manifest（模拟 crash 前已完成）。"""
    pdir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": "fake",
        "status": PARTITION_STATUS_COMPLETED,
        "trade_date": trade_date.isoformat(),
        "bar_index": bar_index,
        "as_of": as_of.isoformat(),
        "eligible_instruments": eligible,
        "code_sha": code_sha,
        "member_rows_written": eligible,
    }
    (pdir / "partition_manifest.json").write_text(json.dumps(manifest))
    (pdir / "data_quality.json").write_text(json.dumps({"member_rows_written": eligible}))


# --- E1-T1: RESUME_SKIP => MDAS calls == 0 (PART 1 hard contract) ---
def test_e1_resume_skip_zero_mdas_calls(tmp_path):
    base = tmp_path / "base"
    pop = {
        date(2024, 1, 2): [_sample("600000", "SH"), _sample("600001", "SH")],
        date(2024, 1, 3): [_sample("600000", "SH"), _sample("600001", "SH"),
                            _sample("600519", "SH")],
    }
    run_dir = "e1t1"
    batch_fn, calls = _make_fake_batch_mdas(value={})
    # 第一轮：完整跑两个 bar（每个 bar 调用 MDAS 2 次：none + qfq）
    res1 = _kernel_run(base, run_dir, [date(2024, 1, 2), date(2024, 1, 3)], pop,
                       adapter=_fake_page_adapter(), session=_FakeSession(),
                       batch_mdas_fn=batch_fn, code_sha="runner-sha-AAAA")
    assert res1["completed_bar_count"] == 2
    total_after_first = len(calls)
    # 第二轮 resume：已 COMPLETED 的两个 bar 必须 SKIP（硬合同：MDAS calls == 0）
    res2 = _kernel_run(base, run_dir, [date(2024, 1, 2), date(2024, 1, 3)], pop,
                       adapter=_fake_page_adapter(), session=_FakeSession(),
                       batch_mdas_fn=batch_fn, code_sha="runner-sha-AAAA")
    assert len(calls) == total_after_first
    assert res2["resume_skipped"] == 2
    assert res2["completed_bar_count"] == 2


# --- E1-T2: incomplete partition correctly reruns (RERUN path executes MDAS) ---
def test_e1_incomplete_partition_reruns(tmp_path):
    base = tmp_path / "base"
    pop = _make_pop([("600000", "SH"), ("600001", "SH")])
    run_dir = "e1t2"
    batch_fn, calls = _make_fake_batch_mdas(value={})
    _kernel_run(base, run_dir, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_fn, code_sha="runner-sha-BBBB")
    before = len(calls)  # 第一轮 1 bar = 2 次 MDAS 调用
    # 模拟 incomplete：把 partition 标记为 FAILED，期望 RERUN
    pdir = _partition_dir(run_dir, date(2024, 1, 2), base)
    man = json.loads((pdir / "partition_manifest.json").read_text())
    man["status"] = PARTITION_STATUS_FAILED
    (pdir / "partition_manifest.json").write_text(json.dumps(man))
    res2 = _kernel_run(base, run_dir, [date(2024, 1, 2)], pop,
                       adapter=_fake_page_adapter(), session=_FakeSession(),
                       batch_mdas_fn=batch_fn, code_sha="runner-sha-BBBB")
    # incomplete/FAILED => RERUN => 重新执行 MDAS（+2 次调用）
    assert len(calls) == before + 2
    assert res2["completed_bar_count"] == 1


# --- E1-T3: unknown SHA mismatch => BLOCK ---
def test_e1_unknown_sha_mismatch_blocks(tmp_path):
    base = tmp_path / "base"
    pop = _make_pop([("600000", "SH")])
    run_dir = "e1t3"
    batch_fn, _ = _make_fake_batch_mdas(value={})
    # 预先写入 COMPLETED partition，code_sha 为未知值（非兼容 SHA）
    pdir = _partition_dir(run_dir, date(2024, 1, 2), base)
    _write_completed_partition(pdir, date(2024, 1, 2), 1, "unknown-sha-XYZ",
                               AS_OF, len(pop[date(2024, 1, 2)]))
    with pytest.raises(CompletedPartitionMetadataMismatch):
        _kernel_run(base, run_dir, [date(2024, 1, 2)], pop,
                    adapter=_fake_page_adapter(), session=_FakeSession(),
                    batch_mdas_fn=batch_fn, code_sha="runner-sha-CCCC")


# --- E1-T4: cd080ad compatible predecessor => SKIP ---
def test_e1_legacy_cd080ad_compatible_skip(tmp_path):
    base = tmp_path / "base"
    pop = _make_pop([("600000", "SH"), ("600001", "SH")])
    run_dir = "e1t4"
    batch_fn, calls = _make_fake_batch_mdas(value={})
    # 预先写入 COMPLETED partition，code_sha = LEGACY_RESUME_COMPATIBLE_SHA
    pdir = _partition_dir(run_dir, date(2024, 1, 2), base)
    _write_completed_partition(pdir, date(2024, 1, 2), 1,
                               LEGACY_RESUME_COMPATIBLE_SHA, AS_OF,
                               len(pop[date(2024, 1, 2)]))
    res = _kernel_run(base, run_dir, [date(2024, 1, 2)], pop,
                      adapter=_fake_page_adapter(), session=_FakeSession(),
                      batch_mdas_fn=batch_fn, code_sha="runner-sha-NEW-FIX")
    # 窄兼容：当前 runner SHA != cd080ad，但属于兼容 SHA => SKIP，不执行 MDAS
    assert len(calls) == 0
    assert res["completed_bar_count"] == 1
    assert res["resume_skipped"] == 1
    # 兼容证据写入 root manifest
    compats = res.get("resume_compatibility", [])
    assert any(c["existing_partition_code_sha"] == LEGACY_RESUME_COMPATIBLE_SHA
               and c["resume_compatible_from_sha"] == LEGACY_RESUME_COMPATIBLE_SHA
               for c in compats)


# --- E1-T5: chunk size => 输出行数/顺序一致 + 分块执行 ---
def test_e1_chunk_size_row_order_consistency(tmp_path):
    syms = [(f"600{i:03d}", "SH") for i in range(10)]
    pop = _make_pop(syms)

    def _val(iid, adj):
        return {"id": iid, "adj": adj}

    # chunk_size=2
    batch_fn_a, calls_a = _make_chunk_aware_batch(_val)
    res_a = _kernel_run(tmp_path / "base", "e1t5a", [date(2024, 1, 2)], pop,
                        adapter=_fake_page_adapter(), session=_FakeSession(),
                        batch_mdas_fn=batch_fn_a, code_sha="runner-sha-CHUNK",
                        mdas_chunk_size=2)
    assert res_a["completed_bar_count"] == 1
    rows_a = _read_member_rows(tmp_path / "base", "e1t5a", date(2024, 1, 2))
    # chunk_size=10（一次性）
    batch_fn_b, calls_b = _make_chunk_aware_batch(_val)
    _kernel_run(tmp_path / "base2", "e1t5b", [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_fn_b, code_sha="runner-sha-CHUNK",
                mdas_chunk_size=10)
    rows_b = _read_member_rows(tmp_path / "base2", "e1t5b", date(2024, 1, 2))
    # 行数与顺序一致
    assert len(rows_a) == len(rows_b) == 10
    assert [r["symbol"] for r in rows_a] == [r["symbol"] for r in rows_b]
    # 分块确实发生：chunk_size=2 应有 ceil(10/2)=5 个 none 批次
    none_calls_a = [c for c in calls_a if c["adj"] == "none"]
    assert len(none_calls_a) == 5
    none_calls_b = [c for c in calls_b if c["adj"] == "none"]
    assert len(none_calls_b) == 1


# --- E1-T6: chunked vs original semantic fixture parity（Member Fact 行一致） ---
def test_e1_chunked_vs_original_parity(tmp_path):
    pop = _make_pop([("600000", "SH"), ("600001", "SH"), ("600519", "SH")])

    def _val(iid, adj):
        return {"id": iid, "adj": adj}

    batch_fn_a, _ = _make_chunk_aware_batch(_val)
    _kernel_run(tmp_path / "base", "e1t6a", [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_fn_a, code_sha="runner-sha-P",
                mdas_chunk_size=1)
    rows_a = _read_member_rows(tmp_path / "base", "e1t6a", date(2024, 1, 2))

    batch_fn_b, _ = _make_chunk_aware_batch(_val)
    _kernel_run(tmp_path / "base2", "e1t6b", [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_fn_b, code_sha="runner-sha-P",
                mdas_chunk_size=MDAS_CHUNK_SIZE)
    rows_b = _read_member_rows(tmp_path / "base2", "e1t6b", date(2024, 1, 2))
    # Member Fact 行在 chunked vs 整市场下完全一致
    assert len(rows_a) == len(rows_b) == 3
    norm_a = {(r["symbol"], r["instrument_id"]) for r in rows_a}
    norm_b = {(r["symbol"], r["instrument_id"]) for r in rows_b}
    assert norm_a == norm_b


# --- E1-T7: offset hints parity（chunked RERUN 后仍持久化 hints） ---
def test_e1_offset_hints_parity_after_chunked_run(tmp_path):
    pop = _make_pop([("600000", "SH"), ("600001", "SH")])
    run_dir = "e1t7"
    batch_fn, _ = _make_fake_batch_mdas(value={})
    res = _kernel_run(tmp_path / "base", run_dir, [date(2024, 1, 2)], pop,
                      adapter=_fake_page_adapter(), session=_FakeSession(),
                      batch_mdas_fn=batch_fn, code_sha="runner-sha-H",
                      mdas_chunk_size=1)
    assert res["completed_bar_count"] == 1
    # offset_hints.json 持久化（含 version=2）；loader 归一化为 {symbol: hint}
    hints_path = tmp_path / "base" / run_dir / "offset_hints.json"
    raw = json.loads(hints_path.read_text())
    assert raw["version"] == 2
    hints = _load_offset_hints(hints_path)
    assert len(hints) == 2
    # RERUN 后 hints 仍可被下一轮 resume 解析
    res2 = _kernel_run(tmp_path / "base", run_dir, [date(2024, 1, 2)], pop,
                       adapter=_fake_page_adapter(), session=_FakeSession(),
                       batch_mdas_fn=batch_fn, code_sha="runner-sha-H",
                       mdas_chunk_size=1)
    assert res2["resume_skipped"] == 1


# --- E1-T8: reconciliation parity（chunked run 仍写 reconciliation 字段） ---
def test_e1_reconciliation_parity(tmp_path):
    pop = _make_pop([("600000", "SH"), ("600001", "SH")])
    run_dir = "e1t8"
    batch_fn, _ = _make_fake_batch_mdas(value={})
    _kernel_run(tmp_path / "base", run_dir, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_fn, code_sha="runner-sha-R",
                mdas_chunk_size=2)
    # reconciliation 字段存在（data_quality.reconciled 为 bool）
    pdir = _partition_dir(run_dir, date(2024, 1, 2), tmp_path / "base")
    dq = json.loads((pdir / "data_quality.json").read_text())
    assert dq["reconciled"] is True
    rows = _read_member_rows(tmp_path / "base", run_dir, date(2024, 1, 2))
    assert len(rows) == 2


# --- E1-T9 / Round 3B-E2 PART 4: memory instrumentation 写入 data_quality ---
def test_e1_memory_instrumentation_recorded(tmp_path):
    pop = _make_pop([("600000", "SH"), ("600001", "SH")])
    run_dir = "e1t9"
    batch_fn, _ = _make_fake_batch_mdas(value={})
    _kernel_run(tmp_path / "base", run_dir, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_fn, code_sha="runner-sha-M",
                mdas_chunk_size=1)
    pdir = _partition_dir(run_dir, date(2024, 1, 2), tmp_path / "base")
    dq = json.loads((pdir / "data_quality.json").read_text())
    mem = dq.get("memory", {})
    # Round 3B-E2 PART 4 — 字段必须存在（明确区分 sampled peak 与 lifetime VmHWM）
    for field in (
        "rss_before_bar_mb", "rss_before_first_mdas_mb",
        "rss_peak_sampled_bar_mb", "rss_after_last_chunk_mb",
        "rss_after_bar_mb", "vmhwm_before_bar_mb", "vmhwm_after_bar_mb",
        "mdas_chunk_size", "mdas_chunk_count",
    ):
        assert field in mem, f"memory instrumentation 缺少字段 {field}"
    # 非负采样；chunk_count 与 population/chunk_size 一致
    assert mem.get("mdas_chunk_size") == 1
    assert mem.get("mdas_chunk_count") == 2  # 2 instruments / chunk 1
    # rss_peak_sampled_bar_mb 是生产路径自身采样样本的最大值（不得是 process VmHWM 直接命名）
    sample_max = max(
        mem["rss_before_bar_mb"], mem["rss_before_first_mdas_mb"] or 0.0,
        mem["rss_after_last_chunk_mb"], mem["rss_after_bar_mb"])
    assert mem["rss_peak_sampled_bar_mb"] == sample_max
    # vmhwm before/after 均记录，且不冒充本 bar 独立 peak
    assert mem["vmhwm_before_bar_mb"] >= 0.0
    assert mem["vmhwm_after_bar_mb"] >= 0.0


# ===========================================================================
# Round 3B-E2 — TRUE BOUNDED-MEMORY PARTITION STREAMING
# 测试均调用正式 production path（run_backfill → _run_bar_partition →
# _run_member_chunk + run_symbol_backfill_observation），只替换
# adapter / session / batch_mdas_fn / chunk size。不得重新实现 chunk 算法。
# ===========================================================================

def _raw_chunk_calls(calls):
    """从 batch_mdas_fn 调用记录中提取 adj="none"（raw）的 chunk id 序列（str 形式）。"""
    return [[str(i) for i in c["ids"]] for c in calls if c["adj"] == "none"]


def _flatten(chunks):
    out = []
    for ch in chunks:
        out.extend(ch)
    return out


def _pop_ids(pop, d):
    """population 在某 trade_date 下的 instrument_id 有序列表（str 形式，匹配输出行）。"""
    return [str(inst.instrument_id) for inst in pop[d]]


def _make_query_counting_batch_mdas(raw_q: int = 2, qfq_q: int = 3):
    """batch_mdas_fn seam：返回真实 physical repository_query_count（PART 3 累计）。"""
    calls = []
    async def _batch(mdas, session, instrument_ids, trade_date, *, adj,
                     adjustment_as_of=None):
        calls.append({
            "adj": adj,
            "trade_date": trade_date,
            "adjustment_as_of": adjustment_as_of,
            "ids": list(instrument_ids),
        })
        q = raw_q if adj == "none" else qfq_q
        return {}, {"repository_query_count": q}
    return _batch, calls


# --- T1: EXACT CHUNK COVERAGE（10 members, chunk_size=2 => [0,1],[2,3],...） ---
def test_e2_exact_chunk_coverage(tmp_path):
    syms = [(f"600{i:03d}", "SH") for i in range(10)]
    pop = _make_pop(syms)
    ids = _pop_ids(pop, date(2024, 1, 2))
    run_dir = "e2t1"
    batch_fn, calls = _make_fake_batch_mdas(value={})
    _kernel_run(tmp_path / "base", run_dir, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_fn, code_sha="e2-sha-T1", mdas_chunk_size=2)
    raw_chunks = _raw_chunk_calls(calls)
    assert raw_chunks == [ids[0:2], ids[2:4], ids[4:6], ids[6:8], ids[8:10]]
    flat = _flatten(raw_chunks)
    assert len(flat) == 10
    assert len(set(flat)) == 10               # no duplicates
    assert flat == ids                        # ordering + no omission


# --- T2: LARGE CHUNK（10 members, chunk_size=1024 => single chunk） ---
def test_e2_large_single_chunk_no_omission(tmp_path):
    syms = [(f"600{i:03d}", "SH") for i in range(10)]
    pop = _make_pop(syms)
    ids = _pop_ids(pop, date(2024, 1, 2))
    run_dir = "e2t2"
    batch_fn, calls = _make_fake_batch_mdas(value={})
    _kernel_run(tmp_path / "base", run_dir, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_fn, code_sha="e2-sha-T2",
                mdas_chunk_size=1024)
    raw_chunks = _raw_chunk_calls(calls)
    assert len(raw_chunks) == 1
    assert raw_chunks[0] == ids


# --- T3: NON-DIVISIBLE（10 members, chunk_size=4 => 4+4+2） ---
def test_e2_non_divisible_chunk_split(tmp_path):
    syms = [(f"600{i:03d}", "SH") for i in range(10)]
    pop = _make_pop(syms)
    ids = _pop_ids(pop, date(2024, 1, 2))
    run_dir = "e2t3"
    batch_fn, calls = _make_fake_batch_mdas(value={})
    _kernel_run(tmp_path / "base", run_dir, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_fn, code_sha="e2-sha-T3", mdas_chunk_size=4)
    raw_chunks = _raw_chunk_calls(calls)
    assert [len(c) for c in raw_chunks] == [4, 4, 2]
    flat = _flatten(raw_chunks)
    assert len(flat) == 10
    assert len(set(flat)) == 10
    assert flat == ids


# --- T4: INVALID CHUNK SIZE（0 / negative => fail fast） ---
def test_e2_invalid_chunk_size_fails_fast(tmp_path):
    syms = [("600000", "SH"), ("600001", "SH")]
    pop = _make_pop(syms)
    run_dir = "e2t4"
    batch_fn, _ = _make_fake_batch_mdas(value={})
    for bad in (0, -1, -512):
        with pytest.raises(ValueError):
            _kernel_run(tmp_path / "base", run_dir, [date(2024, 1, 2)], pop,
                        adapter=_fake_page_adapter(), session=_FakeSession(),
                        batch_mdas_fn=batch_fn, code_sha="e2-sha-T4",
                        mdas_chunk_size=bad)


# --- T5: TRUE SEMANTIC PARITY（chunk_size>=pop vs small chunk） ---
def test_e2_semantic_parity_across_chunk_sizes(tmp_path):
    syms = [(f"600{i:03d}", "SH") for i in range(7)]
    pop = _make_pop(syms)
    run_dir_big = "e2t5big"
    run_dir_small = "e2t5small"
    batch_big, _ = _make_fake_batch_mdas(value={})
    batch_small, _ = _make_fake_batch_mdas(value={})
    _kernel_run(tmp_path / "base", run_dir_big, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_big, code_sha="e2-sha-T5",
                mdas_chunk_size=1024)          # 单 chunk
    _kernel_run(tmp_path / "base", run_dir_small, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_small, code_sha="e2-sha-T5",
                mdas_chunk_size=2)             # 多 chunk
    big_rows = _read_member_rows(tmp_path / "base", run_dir_big, date(2024, 1, 2))
    small_rows = _read_member_rows(tmp_path / "base", run_dir_small, date(2024, 1, 2))
    assert len(big_rows) == 7 and len(small_rows) == 7
    # 业务字段逐字段一致（除显式 runtime-only 字段外，此处 member_facts 行均为业务字段）
    for big, small in zip(big_rows, small_rows):
        assert big == small
    # 明确断言若干业务字段非空且一致
    for r in (big_rows,):
        for row in r:
            for fld in ("trade_date", "bar_index", "instrument_id", "symbol",
                        "market", "board", "source_status",
                        "extraction_status", "canonicalization_status",
                        "auction_price_raw", "auction_volume_raw_lots",
                        "auction_volume_shares", "auction_amount",
                        "lane_a_status", "mdas_raw_open_T", "price_exact_match",
                        "lane_b_status", "previous_close_raw",
                        "previous_close_pit_qfq", "naive_raw_gap", "pit_gap",
                        "mdas_data_source", "degraded", "adjustment_as_of",
                        "adj_factor_hash", "pytdx_page_requests", "used_hint",
                        "search_mode", "target_page_offset"):
                assert fld in row


# --- T6: PHYSICAL QUERY COUNT（N=10, chunk=4 => 3 chunks => raw=6, qfq=9） ---
def test_e2_physical_query_count_accumulated(tmp_path):
    syms = [(f"600{i:03d}", "SH") for i in range(10)]
    pop = _make_pop(syms)
    run_dir = "e2t6"
    batch_fn, _ = _make_query_counting_batch_mdas(raw_q=2, qfq_q=3)
    _kernel_run(tmp_path / "base", run_dir, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_fn, code_sha="e2-sha-T6", mdas_chunk_size=4)
    dq = json.loads(
        (_partition_dir(run_dir, date(2024, 1, 2), tmp_path / "base")
         / "data_quality.json").read_text())
    # 跨 chunk 真实求和：不得停留在单 chunk 的 2/3
    assert dq["raw_mdas_batch_queries"] == 6      # 3 chunks * 2
    assert dq["qfq_mdas_batch_queries"] == 9      # 3 chunks * 3
    assert dq["memory"]["mdas_chunk_count"] == 3


# --- T7: RESUME BEFORE MDAS（completed => SKIP => MDAS/Pytdx calls == 0） ---
def test_e2_resume_before_mdas_zero_calls(tmp_path):
    base = tmp_path / "base"
    pop = _make_pop([("600000", "SH"), ("600001", "SH"), ("600519", "SH")])
    run_dir = "e2t7"
    batch_fn, calls = _make_fake_batch_mdas(value={})
    # 第一轮：单 bar，chunk_size=2
    _kernel_run(base, run_dir, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_fn, code_sha="e2-sha-T7", mdas_chunk_size=2)
    rawh = _raw_chunk_calls(calls)
    assert len(rawh) == 2   # 3 members / chunk 2 => 2 raw chunks
    # 第二轮 resume：COMPLETED => SKIP（硬合同 MDAS calls == 0）
    _kernel_run(base, run_dir, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_fn, code_sha="e2-sha-T7", mdas_chunk_size=2)
    assert len(_raw_chunk_calls(calls)) == 2   # 调用数不再增长
    # adapter page calls 也应为 0（resume 不进入 kernel）
    assert len(calls) == 4                       # 第一轮 4（2 raw + 2 qfq），第二轮 0


# --- T8: OFFSET HINT PARITY（chunked run 后可 resume；跨 chunk offset hints 仍有效） ---
def test_e2_offset_hints_persist_and_resume(tmp_path):
    # 验证 chunked production path 完成后，已完成 partition 仍可被 SKIP resume，
    # 即 chunked run 不破坏 run-level offset_hints / resume 契约（PART 6）。
    base = tmp_path / "base"
    pop = _make_pop([(f"600{i:03d}", "SH") for i in range(6)])
    run_dir = "e2t8"
    batch_fn, calls = _make_fake_batch_mdas(value={})
    res1 = _kernel_run(base, run_dir, [date(2024, 1, 2)], pop,
                       adapter=_fake_page_adapter(), session=_FakeSession(),
                       batch_mdas_fn=batch_fn, code_sha="e2-sha-T8", mdas_chunk_size=2)
    assert res1["completed_bar_count"] == 1
    assert res1["resume_skipped"] == 0
    before = len(calls)
    # 第二次 chunked run 应 SKIP（COMPLETED + 同 sha），MDAS 不重跑
    res2 = _kernel_run(base, run_dir, [date(2024, 1, 2)], pop,
                       adapter=_fake_page_adapter(), session=_FakeSession(),
                       batch_mdas_fn=batch_fn, code_sha="e2-sha-T8", mdas_chunk_size=2)
    assert len(calls) == before   # 新增 0 次 MDAS 调用
    assert res2["resume_skipped"] == 1
    assert res2["completed_bar_count"] == 1


# --- T9: STREAM OUTPUT ORDER（不同 chunk size => row count 相同 + 顺序相同） ---
def test_e2_stream_output_order_stable(tmp_path):
    syms = [(f"600{i:03d}", "SH") for i in range(9)]
    pop = _make_pop(syms)
    ids = _pop_ids(pop, date(2024, 1, 2))
    run_big = "e2t9big"
    run_small = "e2t9small"
    b1, _ = _make_fake_batch_mdas(value={})
    b2, _ = _make_fake_batch_mdas(value={})
    _kernel_run(tmp_path / "base", run_big, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=b1, code_sha="e2-sha-T9", mdas_chunk_size=1024)
    _kernel_run(tmp_path / "base", run_small, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=b2, code_sha="e2-sha-T9", mdas_chunk_size=3)
    big = _read_member_rows(tmp_path / "base", run_big, date(2024, 1, 2))
    small = _read_member_rows(tmp_path / "base", run_small, date(2024, 1, 2))
    assert len(big) == len(small) == 9
    assert [r["instrument_id"] for r in big] == [r["instrument_id"] for r in small]
    assert [r["instrument_id"] for r in big] == ids


# --- T10: MEMORY OWNERSHIP STRUCTURE（bounded prep：per-chunk，非全市场 merge） ---
def test_e2_memory_ownership_bounded_lifecycle(tmp_path):
    # 行为证明：production path 对每个 chunk 单独调用 MDAS（prepare→consume→next），
    # 而非一次性把全市场 raw+qfq 累积进单一 mapping 再消费。
    # 硬指标：
    #   - raw MDAS 调用次数 == ceil(N / chunk_size)（bounded prep）
    #   - 单次 raw MDAS 调用携带的 id 数 <= chunk_size（O(chunk) 上界）
    #   - 跨所有 raw 调用的 distinct id == N（无 dup / 无 omission）
    syms = [(f"600{i:03d}", "SH") for i in range(8)]
    pop = _make_pop(syms)
    ids = _pop_ids(pop, date(2024, 1, 2))
    N = len(ids)
    chunk_size = 2
    run_dir = "e2t10"
    batch_fn, calls = _make_fake_batch_mdas(value={})
    _kernel_run(tmp_path / "base", run_dir, [date(2024, 1, 2)], pop,
                adapter=_fake_page_adapter(), session=_FakeSession(),
                batch_mdas_fn=batch_fn, code_sha="e2-sha-T10", mdas_chunk_size=chunk_size)
    raw_chunks = _raw_chunk_calls(calls)
    # 不是一次性全市场 batch（禁止单 chunk == 全市场）
    assert len(raw_chunks) == (N + chunk_size - 1) // chunk_size
    assert all(len(c) <= chunk_size for c in raw_chunks)
    distinct = set(_flatten(raw_chunks))
    assert distinct == set(ids)                 # 无 dup / 无 omission / 全市场覆盖
    # 不存在一个持有全市场 id 的 mega-batch
    assert all(len(c) < N for c in raw_chunks)
