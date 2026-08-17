"""历史竞价落库 PG 集成测试 — [CHANGE-20260817-001]。

验证 runner 接线（run_backfill + 注入 session + db_writer=write_bar_quotes）能正确落库：
1. runner 跑 2 bars × 小样本 → auction_final_quotes 行数 == JSON 行数（对账）。
2. manifest 含 db_written_rows，且 == 落库行数（逐 bar partition manifest）。
3. resume：第一次只跑 1 bar，第二次跑全量 → 行数收敛不重复。
4. 隔离性：backfill source/namespace 不与 live（verified_consensus/production）混淆。
5. [FIX] chunk 1 commit 后中断 → DB 保留 chunk 1 数据；重跑收敛不重复（即时保存护栏）。
6. [FIX] chunk DB 写失败 → 当前 bar FAILED（abort），不影响其他 bar。

注意：scan_service 默认只读 verified_consensus/production，历史回补用隔离 source，
      **不会被现有 scan 自动消费**（消费侧扩展属单独一轮，见 CHANGE）。本测试不验证
      scan 消费，只验证落库正确性与隔离。

运行（真实 PostgreSQL 验证库，PANJI_REMOTE_VERIFY_DB_TEST=1）：
    PANJI_REMOTE_VERIFY_DB_TEST=1 pytest backend/tests/test_historical_auction_backfill_pg.py -v
本地 PURE 模式自动 skip。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest

_EXP_DIR = Path(__file__).resolve().parents[2] / "experiments" / "pytdx_auction_history"
if str(_EXP_DIR) not in sys.path:
    sys.path.insert(0, str(_EXP_DIR))

from auction_history_semantics_validation import SampleInstrument  # noqa: E402
from full_market_member_fact_backfill import (  # noqa: E402
    PARTITION_STATUS_COMPLETED,
    PARTITION_STATUS_FAILED,
    _board_for_symbol,
    run_backfill,
)
from sqlalchemy import func, select  # noqa: E402

from app.models.auction import AuctionFinalQuote  # noqa: E402
from app.services.historical_auction_backfill_writer import (  # noqa: E402
    HISTORICAL_BACKFILL_SOURCE,
    write_bar_quotes,
)

_TEST_SHA = "test-sha-000000000000000000000000000000000000"


def _sample(symbol, market, instrument_id=None):
    return SampleInstrument(
        symbol=symbol, market=market,
        instrument_id=instrument_id or UUID("00000000-0000-0000-0000-000000000001"),
        board=_board_for_symbol(symbol, market),
        coverage_tag="all_a_share", cohort="routine",
    )


def _obs_canon_computed(symbol, market):
    """正式 production row 词表（对齐 _project_member_row / project_row_to_fact）。

    [FIX] 旧 fixture 用 prev_close / canonicalization_status=FINAL /
    auction_matched_volume_shares 等非正式词表；此处改为正式字段：
    previous_close_raw（lane_b.raw_close_Tm1 投影）与 CANONICAL 状态。
    """
    return {
        "auction_price_raw": 10.5,
        "previous_close_raw": 10.0,
        "auction_volume_shares": 1000,
        "auction_amount": 10500.0,
        "auction_amount_source_type": "DERIVED_PRICE_X_NORMALIZED_VOLUME",
        "source_status": "TARGET_WINDOW_COMPLETE",
        "canonicalization_status": "CANONICAL",
    }


def _make_fake_run_symbol_obs(instruments):
    by_symbol = {inst.symbol: inst for inst in instruments}
    async def _fake(inst, trade_date):
        return _obs_canon_computed(inst.symbol, inst.market)
    return _fake, by_symbol


def _make_flaky_writer(fail_dates: set[date]):
    """db_writer：命中 fail_dates 的 bar 返回 failed>0（不写库），否则 delegate 真实 writer。"""
    async def _flaky(session, trade_date, capture_run, facts, *, chunk_size=500):
        facts_list = list(facts)
        if trade_date in fail_dates:
            return {"written": 0, "skipped": 0, "failed": len(facts_list)}
        return await write_bar_quotes(
            session, trade_date, capture_run, facts_list, chunk_size=chunk_size)
    return _flaky


def _make_interrupt_writer(commit_before_fail: int):
    """db_writer：前 commit_before_fail 次 delegate 真实 writer（commit 落库），
    之后返回 failed>0（模拟 chunk 1 commit 后中断）。"""
    commits = 0
    async def _flaky(session, trade_date, capture_run, facts, *, chunk_size=500):
        nonlocal commits
        facts_list = list(facts)
        if commits < commit_before_fail:
            commits += 1
            return await write_bar_quotes(
                session, trade_date, capture_run, facts_list, chunk_size=chunk_size)
        return {"written": 0, "skipped": 0, "failed": len(facts_list)}
    return _flaky


def _partition_manifest(tmp_path, run_id, t):
    p = tmp_path / run_id / t.isoformat() / "partition_manifest.json"
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _count_quotes(db_session, *, trade_date=None, source=HISTORICAL_BACKFILL_SOURCE):
    stmt = select(func.count()).select_from(AuctionFinalQuote).where(
        AuctionFinalQuote.source == source)
    if trade_date is not None:
        stmt = stmt.where(AuctionFinalQuote.trade_date == trade_date)
    return (db_session.execute(stmt)).scalar()


@pytest.mark.asyncio
async def test_runner_writes_db_rows_and_reconciles(db_session, tmp_path):
    """runner 接线落库：DB 行数 == JSON 行数，逐 bar partition manifest db_written_rows 匹配。"""
    insts = [
        _sample("600000", "SH", UUID("00000000-0000-0000-0000-00000000000a")),
        _sample("000001", "SZ", UUID("00000000-0000-0000-0000-00000000000b")),
        _sample("300750", "SZ", UUID("00000000-0000-0000-0000-00000000000c")),
    ]
    fake, _ = _make_fake_run_symbol_obs(insts)

    t1 = date(2026, 8, 13)
    t2 = date(2026, 8, 14)
    bar_dates = [t1, t2]

    manifest = await run_backfill(
        run_id="pg_test_reconcile",
        bar_dates=bar_dates,
        dry_run=False,
        population=insts,
        run_symbol_obs=fake,
        adapter=object(),  # 注入路径 sentinel
        session=db_session,
        output_root=tmp_path,
        code_sha=_TEST_SHA,
        as_of=t2,
        db_writer=write_bar_quotes,
    )

    assert manifest["status"] == "DONE"
    assert manifest["completed_bar_count"] == 2
    assert manifest["failed_bar_count"] == 0

    # 总落库行数 = 2 bars × 3 instruments = 6
    assert _count_quotes(db_session) == 6

    # JSON 行数（两 bar 各 3 行）
    json_rows = 0
    for t in bar_dates:
        pdir = tmp_path / "pg_test_reconcile" / t.isoformat()
        with open(pdir / "member_facts.jsonl", "r", encoding="utf-8") as f:
            json_rows += sum(1 for _ in f)
    assert json_rows == 6

    # 逐 bar partition manifest：COMPLETED 且 db_written_rows == 3
    for t in bar_dates:
        p = _partition_manifest(tmp_path, "pg_test_reconcile", t)
        assert p["status"] == PARTITION_STATUS_COMPLETED
        assert p["db_written_rows"] == 3
        assert p["db_failed_rows"] == 0


@pytest.mark.asyncio
async def test_runner_resume_partial_then_full(db_session, tmp_path):
    """第一次只跑 t1，resume 跑 t1+t2 → 行数收敛到 4 不重复。"""
    insts = [
        _sample("600000", "SH", UUID("00000000-0000-0000-0000-00000000000a")),
        _sample("000001", "SZ", UUID("00000000-0000-0000-0000-00000000000b")),
    ]
    fake, _ = _make_fake_run_symbol_obs(insts)
    t1 = date(2026, 8, 13)
    t2 = date(2026, 8, 14)

    # 第一次：只 t1
    await run_backfill(
        run_id="pg_test_resume", bar_dates=[t1], dry_run=False,
        population=insts, run_symbol_obs=fake, adapter=object(),
        session=db_session, output_root=tmp_path, code_sha=_TEST_SHA, as_of=t2,
        db_writer=write_bar_quotes,
    )
    assert _count_quotes(db_session) == 2

    # resume：t1 + t2（t1 已完成 RESUME_SKIP，只重跑 t2，但 DB 幂等 upsert）
    await run_backfill(
        run_id="pg_test_resume", bar_dates=[t1, t2], dry_run=False,
        population=insts, run_symbol_obs=fake, adapter=object(),
        session=db_session, output_root=tmp_path, code_sha=_TEST_SHA, as_of=t2,
        db_writer=write_bar_quotes,
    )
    assert _count_quotes(db_session) == 4  # 2 bars × 2 instruments


@pytest.mark.asyncio
async def test_backfill_isolated_from_live_namespace(db_session, tmp_path):
    """回补数据不应出现在 live（verified_consensus/production）查询中。"""
    insts = [_sample("600000", "SH", UUID("00000000-0000-0000-0000-00000000000a"))]
    fake, _ = _make_fake_run_symbol_obs(insts)
    t = date(2026, 8, 14)

    await run_backfill(
        run_id="pg_test_iso", bar_dates=[t], dry_run=False,
        population=insts, run_symbol_obs=fake, adapter=object(),
        session=db_session, output_root=tmp_path, code_sha=_TEST_SHA, as_of=t,
        db_writer=write_bar_quotes,
    )

    live_cnt = (await db_session.execute(
        select(func.count()).select_from(AuctionFinalQuote).where(
            AuctionFinalQuote.trade_date == t,
            AuctionFinalQuote.source == "verified_consensus",
            AuctionFinalQuote.test_namespace == "production",
        ))).scalar()
    assert live_cnt == 0

    assert _count_quotes(db_session, trade_date=t) == 1


@pytest.mark.asyncio
async def test_chunk_interrupt_data_not_lost(db_session, tmp_path):
    """模拟 chunk 1 commit 后中断：DB 可见 chunk 1 数据；重跑后收敛不重复。

    即时保存护栏：已 commit 的 chunk 不得丢失，bar 标记 FAILED，重跑幂等补齐。
    """
    insts = [
        _sample("600000", "SH", UUID("00000000-0000-0000-0000-00000000000a")),
        _sample("000001", "SZ", UUID("00000000-0000-0000-0000-00000000000b")),
        _sample("300750", "SZ", UUID("00000000-0000-0000-0000-00000000000c")),
    ]
    fake, _ = _make_fake_run_symbol_obs(insts)
    t = date(2026, 8, 14)

    # 第一次：写完 chunk 1（1 只）后中断（failed>0 → PartialDbWriteFailure → bar FAILED）
    await run_backfill(
        run_id="pg_test_interrupt", bar_dates=[t], dry_run=False,
        population=insts, run_symbol_obs=fake, adapter=object(),
        session=db_session, output_root=tmp_path, code_sha=_TEST_SHA, as_of=t,
        db_writer=_make_interrupt_writer(commit_before_fail=1),
    )
    # 中断后 DB 保留 chunk 1（1 行），bar FAILED
    assert _count_quotes(db_session, trade_date=t) == 1
    p1 = _partition_manifest(tmp_path, "pg_test_interrupt", t)
    assert p1["status"] == PARTITION_STATUS_FAILED

    # 重跑：真实 writer → 3 行收敛，不重复
    await run_backfill(
        run_id="pg_test_interrupt", bar_dates=[t], dry_run=False,
        population=insts, run_symbol_obs=fake, adapter=object(),
        session=db_session, output_root=tmp_path, code_sha=_TEST_SHA, as_of=t,
        db_writer=write_bar_quotes,
    )
    assert _count_quotes(db_session, trade_date=t) == 3
    p2 = _partition_manifest(tmp_path, "pg_test_interrupt", t)
    assert p2["status"] == PARTITION_STATUS_COMPLETED


@pytest.mark.asyncio
async def test_bar_abort_on_db_failure(db_session, tmp_path):
    """chunk 写失败 → 当前 bar FAILED（abort，不浪费剩余），不影响其他 bar。"""
    insts = [
        _sample("600000", "SH", UUID("00000000-0000-0000-0000-00000000000a")),
        _sample("000001", "SZ", UUID("00000000-0000-0000-0000-00000000000b")),
    ]
    fake, _ = _make_fake_run_symbol_obs(insts)
    t1 = date(2026, 8, 13)
    t2 = date(2026, 8, 14)
    bar_dates = [t1, t2]

    manifest = await run_backfill(
        run_id="pg_test_db_failure", bar_dates=bar_dates, dry_run=False,
        population=insts, run_symbol_obs=fake, adapter=object(),
        session=db_session, output_root=tmp_path, code_sha=_TEST_SHA, as_of=t2,
        db_writer=_make_flaky_writer(fail_dates={t1}),
    )

    # t1 失败（abort，DB 无数据），t2 成功（DB 2 行）
    assert manifest["status"] == "PARTIAL"
    assert manifest["failed_bar_count"] == 1
    assert manifest["completed_bar_count"] == 1

    # t1 partition FAILED，t2 COMPLETED
    p1 = _partition_manifest(tmp_path, "pg_test_db_failure", t1)
    assert p1["status"] == PARTITION_STATUS_FAILED
    p2 = _partition_manifest(tmp_path, "pg_test_db_failure", t2)
    assert p2["status"] == PARTITION_STATUS_COMPLETED
    assert p2["db_written_rows"] == 2

    # DB：t1 无数据，t2 2 行
    assert _count_quotes(db_session, trade_date=t1) == 0
    assert _count_quotes(db_session, trade_date=t2) == 2
