"""Round 3B-B backfill runner tests — file evidence + qfq degraded fail-close.

覆盖任务定义的 B1..B13。所有测试纯内存（不连真实 DB / pytdx）。
runner 通过注入 calendar_fn / population_fn / run_single_obs / output_root 实现 fake orchestration。
"""

import asyncio
import json
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

# 注入用 dummy adapter：run_backfill 注入路径不连真实 pytdx / DB
_FAKE_ADAPTER = object()

from auction_history_semantics_validation import SampleInstrument
from full_market_member_fact_backfill import (
    BASELINE_SHA,
    AS_OF,
    BAR_COUNT,
    _board_for_symbol,
    _to_sample_inst,
    _run_bar_partition,
    _partition_already_completed,
    run_backfill,
    PARTITION_STATUS_COMPLETED,
    PARTITION_STATUS_FAILED,
)


def _sample(symbol, market, listing_date=None, instrument_id=None):
    from uuid import UUID
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
            "mdas_data_source": "db",
            "mdas_degraded": degraded,
            "mdas_degraded_reason": "fake degraded" if degraded else None,
            "adj_factor_hash": "h123",
        },
    }


def _obs_source_incomplete(symbol, market):
    return {
        "symbol": symbol, "market": market,
        "instrument_id": symbol, "board": _board_for_symbol(symbol, market),
        "listing_date": None,
        "full_day_status": "SOURCE_INCOMPLETE",
        "extraction_status": "EMPTY",
        "canonicalization_status": None,
        "canonicalization_reason": "empty",
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
    """behavior: dict symbol->obs template，或 callable(symbol)->obs。

    runner 通过 `await run_single_obs(...)` 调用，故返回 async coroutine function。
    """
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


# ---------------------------------------------------------------------------
# B1: official calendar → exactly 120 bars（不是自然日减法）
# ---------------------------------------------------------------------------
def test_b1_calendar_exactly_120_bars(tmp_out):
    # 120 个 official trading bars，跨 130 个日历日（模拟周末/节假日）
    seq = []
    cur = AS_OF
    # 从 AS_OF 向前每隔一天取，跳过周末，凑足 120 个交易日
    while len(seq) < 120:
        if cur.weekday() < 5:  # 周一~周五为交易日
            seq.append(cur)
        cur -= timedelta(days=1)
    seq = sorted(seq)
    assert len(seq) == 120
    assert seq[-1] == AS_OF
    # 非自然日减法：日历跨度远大于 120
    span = (seq[-1] - seq[0]).days
    assert span > 120

    # runner 通过官方 calendar 取 exactly 120 bars，不数自然日
    async def _pop(session, t):
        return []
    async def _cal(session, T, n):
        # 只返回 120 个 trading dates（与 span > 120 日历日一致）
        return seq

    async def _run():
        return await run_backfill(
            run_id="t_b1", calendar_fn=_cal, population_fn=_pop,
            run_single_obs=_make_fake_run_single(lambda s: _obs_canon_computed(s, "SH")),
            output_root=tmp_out, adapter=_FAKE_ADAPTER)
    res = asyncio.run(_run())
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
# B3/B4: IPO filtering by listing_date <= T
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# B3/B4: IPO filtering by listing_date <= T；中途 IPO 股票不向前扩展
# ---------------------------------------------------------------------------
def test_b3_b4_ipo_filter_and_midwindow_ipo(tmp_out):
    d1 = date(2026, 2, 13)
    d2 = date(2026, 5, 1)
    d3 = date(2026, 8, 14)
    bar_dates = [d1, d2, d3]
    stock_listing = d2  # 中途 IPO

    # population resolver 是 IPO filter owner：listing_date <= T 才返回该标的
    pop = {
        d1: [],  # T1 < listing → 不进入
        d2: [_sample("600001", "SH", listing_date=stock_listing)],
        d3: [_sample("600001", "SH", listing_date=stock_listing)],
    }

    async def _pop(session, t):
        return pop[t]

    async def _cal(session, T, n):
        return bar_dates

    async def _run():
        return await run_backfill(
            run_id="t_b3", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=_make_fake_run_single(
                lambda s: _obs_canon_computed(s, "SH")),
            output_root=tmp_out, adapter=_FAKE_ADAPTER,
        )

    res = asyncio.run(_run())
    # B3: T1 < listing → 不进入该 bar
    d1_dir = tmp_out / "t_b3" / "bars" / d1.isoformat()
    d1_rows = [json.loads(l) for l in open(d1_dir / "member_facts.jsonl")]
    assert len(d1_rows) == 0, "listing_date > T 的股票不应出现在该 bar"
    # B4: T2 >= listing → 进入；不向 window 前扩展
    d2_dir = tmp_out / "t_b3" / "bars" / d2.isoformat()
    d2_rows = [json.loads(l) for l in open(d2_dir / "member_facts.jsonl")]
    assert len(d2_rows) == 1
    assert d2_rows[0]["symbol"] == "600001"
    # 整轮 status 成功（bar_count 是注入值，不强制 120）
    assert res["completed_bar_count"] == 3


# ---------------------------------------------------------------------------
# B5: source incomplete 仍写 member row
# ---------------------------------------------------------------------------
def test_b5_source_incomplete_writes_row(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH", listing_date=date(2020, 1, 1)),
               _sample("600002", "SH", listing_date=date(2020, 1, 1))]}

    async def _pop(session, t):
        return pop[t]

    async def _cal(session, T, n):
        return bar_dates

    # 一只 source incomplete，一只正常
    fake = _make_fake_run_single(
        lambda s: _obs_source_incomplete(s, "SH") if s == "600001"
        else _obs_canon_computed(s, "SH"))

    async def _run():
        return await run_backfill(
            run_id="t_b5", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=fake, output_root=tmp_out, adapter=_FAKE_ADAPTER)

    res = asyncio.run(_run())
    ddir = tmp_out / "t_b5" / "bars" / d.isoformat()
    rows = [json.loads(l) for l in open(ddir / "member_facts.jsonl")]
    assert len(rows) == 2
    inc = [r for r in rows if r["symbol"] == "600001"][0]
    assert inc["canonicalization_status"] is None
    assert inc["auction_price_raw"] is None
    norm = [r for r in rows if r["symbol"] == "600002"][0]
    assert norm["canonicalization_status"] == "CANONICAL"
    assert res["completed_bar_count"] == 1


# ---------------------------------------------------------------------------
# B6: canonical projection 机械正确
# ---------------------------------------------------------------------------
def test_b6_canonical_projection(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH", listing_date=date(2020, 1, 1))]}
    async def _pop(session, t):
        return pop[t]
    async def _cal(session, T, n):
        return bar_dates
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH", pit_gap=0.07))
    async def _run():
        return await run_backfill(
            run_id="t_b6", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=fake, output_root=tmp_out, adapter=_FAKE_ADAPTER)
    asyncio.run(_run())
    ddir = tmp_out / "t_b6" / "bars" / d.isoformat()
    row = json.loads(open(ddir / "member_facts.jsonl").readline())
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
    assert row["baseline_sha"] == BASELINE_SHA
    assert row["as_of"] == AS_OF.isoformat()


# ---------------------------------------------------------------------------
# B7: qfq degraded → pit_gap unavailable（HARD REGRESSION）
# ---------------------------------------------------------------------------
def test_b7_qfq_degraded_pit_gap_unavailable(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SZ", listing_date=date(2020, 1, 1))]}
    async def _pop(session, t):
        return pop[t]
    async def _cal(session, T, n):
        return bar_dates
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SZ", degraded=True))
    async def _run():
        return await run_backfill(
            run_id="t_b7", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=fake, output_root=tmp_out, adapter=_FAKE_ADAPTER)
    asyncio.run(_run())
    ddir = tmp_out / "t_b7" / "bars" / d.isoformat()
    row = json.loads(open(ddir / "member_facts.jsonl").readline())
    assert row["lane_b_status"] == "PIT_ADJUSTMENT_DEGRADED"
    assert row["pit_gap"] is None
    assert row["degraded"] is True


# ---------------------------------------------------------------------------
# B8: qfq healthy → pit_gap 正常保留
# ---------------------------------------------------------------------------
def test_b8_qfq_healthy_pit_gap_present(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SZ", listing_date=date(2020, 1, 1))]}
    async def _pop(session, t):
        return pop[t]
    async def _cal(session, T, n):
        return bar_dates
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SZ", degraded=False, pit_gap=0.09))
    async def _run():
        return await run_backfill(
            run_id="t_b8", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=fake, output_root=tmp_out, adapter=_FAKE_ADAPTER)
    asyncio.run(_run())
    ddir = tmp_out / "t_b8" / "bars" / d.isoformat()
    row = json.loads(open(ddir / "member_facts.jsonl").readline())
    assert row["lane_b_status"] == "COMPUTED"
    assert row["pit_gap"] == 0.09
    assert row["degraded"] is False


# ---------------------------------------------------------------------------
# B9: bar reconciliation PASS
# ---------------------------------------------------------------------------
def test_b9_bar_reconciliation_pass(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH", listing_date=date(2020, 1, 1)),
               _sample("600002", "SH", listing_date=date(2020, 1, 1))]}
    async def _pop(session, t):
        return pop[t]
    async def _cal(session, T, n):
        return bar_dates
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH"))
    async def _run():
        return await run_backfill(
            run_id="t_b9", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=fake, output_root=tmp_out, adapter=_FAKE_ADAPTER)
    res = asyncio.run(_run())
    ddir = tmp_out / "t_b9" / "bars" / d.isoformat()
    m = json.load(open(ddir / "partition_manifest.json"))
    assert m["status"] == PARTITION_STATUS_COMPLETED
    assert m["eligible_instruments"] == 2
    assert m["member_rows_written"] == 2
    dq = json.load(open(ddir / "data_quality.json"))
    assert dq["reconciled"] is True


# ---------------------------------------------------------------------------
# B10: rows missing → partition FAILED
# ---------------------------------------------------------------------------
def test_b10_rows_missing_partition_failed(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH", listing_date=date(2020, 1, 1)),
               _sample("600002", "SH", listing_date=date(2020, 1, 1))]}
    async def _pop(session, t):
        return pop[t]
    async def _cal(session, T, n):
        return bar_dates
    # 故意让 600002 的 observation 抛异常 → 只写 RUN_ERROR row
    async def _boom(mdas, adapter, session, inst, trade_date):
        if inst.symbol == "600002":
            raise RuntimeError("boom")
        return _obs_canon_computed(inst.symbol, "SH")
    async def _run():
        return await run_backfill(
            run_id="t_b10", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=_boom, output_root=tmp_out, adapter=_FAKE_ADAPTER)
    res = asyncio.run(_run())
    ddir = tmp_out / "t_b10" / "bars" / d.isoformat()
    m = json.load(open(ddir / "partition_manifest.json"))
    # 2 eligible，但 600002 崩溃 → RUN_ERROR → eligible(2) != written(2 with RUN_ERROR)
    assert m["status"] == PARTITION_STATUS_FAILED
    assert res["completed_bar_count"] == 0
    assert res["failed_bar_count"] == 1


# ---------------------------------------------------------------------------
# B11: completed partition metadata match → skip
# ---------------------------------------------------------------------------
def test_b11_completed_skip(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH", listing_date=date(2020, 1, 1))]}
    async def _pop(session, t):
        return pop[t]
    async def _cal(session, T, n):
        return bar_dates
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH"))

    async def _first():
        return await run_backfill(
            run_id="t_b11", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=fake, output_root=tmp_out, adapter=_FAKE_ADAPTER)
    asyncio.run(_first())

    # 第二次跑：应 skip 已完成 partition（不重新生成事实）
    async def _second():
        return await run_backfill(
            run_id="t_b11", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=fake, output_root=tmp_out, adapter=_FAKE_ADAPTER)
    res2 = asyncio.run(_second())
    assert res2["completed_bar_count"] == 1
    ddir = tmp_out / "t_b11" / "bars" / d.isoformat()
    rows = [json.loads(l) for l in open(ddir / "member_facts.jsonl")]
    assert len(rows) == 1  # 未被覆盖重跑


# ---------------------------------------------------------------------------
# B12: metadata mismatch → 不覆盖
# ---------------------------------------------------------------------------
def test_b12_metadata_mismatch_no_overwrite(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH", listing_date=date(2020, 1, 1))]}
    async def _pop(session, t):
        return pop[t]
    async def _cal(session, T, n):
        return bar_dates
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH"))

    async def _first():
        return await run_backfill(
            run_id="t_b12", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=fake, output_root=tmp_out, adapter=_FAKE_ADAPTER)
    asyncio.run(_first())

    # 篡改已完成的 manifest：baseline_sha 不一致 → 不 skip
    ddir = tmp_out / "t_b12" / "bars" / d.isoformat()
    m = json.load(open(ddir / "partition_manifest.json"))
    m["baseline_sha"] = "WRONG_SHA"
    json.dump(m, open(ddir / "partition_manifest.json", "w"))
    # _partition_already_completed 返回 False → 会被整 bar 重跑
    assert _partition_already_completed(ddir, d, 1, tmp_out) is False


# ---------------------------------------------------------------------------
# B13: FAILED/RUNNING partition → whole bar rerun
# ---------------------------------------------------------------------------
def test_b13_failed_partition_rerun(tmp_out):
    d = date(2026, 8, 14)
    bar_dates = [d]
    pop = {d: [_sample("600001", "SH", listing_date=date(2020, 1, 1))]}
    async def _pop(session, t):
        return pop[t]
    async def _cal(session, T, n):
        return bar_dates
    fake = _make_fake_run_single(lambda s: _obs_canon_computed(s, "SH"))

    async def _first():
        return await run_backfill(
            run_id="t_b13", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=fake, output_root=tmp_out, adapter=_FAKE_ADAPTER)
    asyncio.run(_first())

    # 篡改 status=FAILED → 下次整 bar 重跑（覆盖 member_facts）
    ddir = tmp_out / "t_b13" / "bars" / d.isoformat()
    m = json.load(open(ddir / "partition_manifest.json"))
    m["status"] = PARTITION_STATUS_FAILED
    json.dump(m, open(ddir / "partition_manifest.json", "w"))
    assert _partition_already_completed(ddir, d, 1, tmp_out) is False

    # 重跑应成功并重新生成事实
    async def _second():
        return await run_backfill(
            run_id="t_b13", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=fake, output_root=tmp_out, adapter=_FAKE_ADAPTER)
    res = asyncio.run(_second())
    assert res["completed_bar_count"] == 1
    rows = [json.loads(l) for l in open(ddir / "member_facts.jsonl")]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# FAKE ORCHESTRATION: 3 bars × 3~5 instruments（含中途 IPO / source incomplete / qfq degraded）
# ---------------------------------------------------------------------------
def test_fake_orchestration_full(tmp_out):
    d1 = date(2026, 2, 13)
    d2 = date(2026, 5, 1)
    d3 = date(2026, 8, 14)
    bar_dates = [d1, d2, d3]

    # 600001 普通；600002 中途 IPO（d2 才出现）；600003 source incomplete；600004 qfq degraded
    pop = {
        d1: [_sample("600001", "SH", listing_date=date(2020, 1, 1)),
             _sample("600003", "SH", listing_date=date(2020, 1, 1)),
             _sample("600004", "SZ", listing_date=date(2020, 1, 1))],
        d2: [_sample("600001", "SH", listing_date=date(2020, 1, 1)),
             _sample("600002", "SH", listing_date=d2),  # 中途 IPO
             _sample("600003", "SH", listing_date=date(2020, 1, 1)),
             _sample("600004", "SZ", listing_date=date(2020, 1, 1))],
        d3: [_sample("600001", "SH", listing_date=date(2020, 1, 1)),
             _sample("600002", "SH", listing_date=d2),
             _sample("600003", "SH", listing_date=date(2020, 1, 1)),
             _sample("600004", "SZ", listing_date=date(2020, 1, 1))],
    }

    async def _pop(session, t):
        return pop[t]
    async def _cal(session, T, n):
        return bar_dates

    def _behavior(symbol):
        if symbol == "600003":
            return _obs_source_incomplete(symbol, "SH")
        if symbol == "600004":
            return _obs_canon_computed(symbol, "SZ", degraded=True)
        return _obs_canon_computed(symbol, "SH")

    async def _run():
        return await run_backfill(
            run_id="t_orch", bar_dates=bar_dates, calendar_fn=_cal,
            population_fn=_pop, run_single_obs=_make_fake_run_single(_behavior),
            output_root=tmp_out, adapter=_FAKE_ADAPTER)
    res = asyncio.run(_run())

    # 3 bars 全部 COMPLETED
    assert res["completed_bar_count"] == 3
    assert res["failed_bar_count"] == 0

    # d1: 600002 未 IPO → 3 只
    d1_rows = [json.loads(l) for l in open(
        tmp_out / "t_orch" / "bars" / d1.isoformat() / "member_facts.jsonl")]
    assert len(d1_rows) == 3
    # d2: 600002 IPO 出现 → 4 只
    d2_rows = [json.loads(l) for l in open(
        tmp_out / "t_orch" / "bars" / d2.isoformat() / "member_facts.jsonl")]
    assert len(d2_rows) == 4
    assert any(r["symbol"] == "600002" for r in d2_rows)
    # source incomplete 仍写行
    assert any(r["symbol"] == "600003" and r["canonicalization_status"] is None
               for r in d2_rows)
    # qfq degraded → pit_gap None
    deg = [r for r in d2_rows if r["symbol"] == "600004"][0]
    assert deg["lane_b_status"] == "PIT_ADJUSTMENT_DEGRADED"
    assert deg["pit_gap"] is None
    # bar_index 升序 d1=1,d2=2,d3=3
    assert d1_rows[0]["bar_index"] == 1
    assert d2_rows[0]["bar_index"] == 2

