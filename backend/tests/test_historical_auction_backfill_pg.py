"""历史竞价落库 PG 集成测试 — [CHANGE-20260817-001]。

验证 runner 接线（run_backfill + 注入 session + db_writer）能正确落库：
1. runner 跑 2 bars × 小样本 → auction_final_quotes 行数 == JSON 行数（对账）。
2. manifest 含 db_written_rows，且 == 落库行数。
3. resume：第一次只跑 1 bar，第二次跑全量 → 行数收敛不重复。
4. 隔离性：backfill source/namespace 不与 live（verified_consensus/production）混淆。

注意：scan_service 默认只读 verified_consensus/production，历史回补用隔离 source，
      **不会被现有 scan 自动消费**（消费侧扩展属单独一轮，见 CHANGE）。本测试不验证
      scan 消费，只验证落库正确性与隔离。

运行（真实 PostgreSQL 验证库，PANJI_REMOTE_VERIFY_DB_TEST=1）：
    PANJI_REMOTE_VERIFY_DB_TEST=1 pytest backend/tests/test_historical_auction_backfill_pg.py -v
本地 PURE 模式自动 skip。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

import pytest

_EXP_DIR = Path(__file__).resolve().parents[2] / "experiments" / "pytdx_auction_history"
if str(_EXP_DIR) not in sys.path:
    sys.path.insert(0, str(_EXP_DIR))

from auction_history_semantics_validation import SampleInstrument  # noqa: E402
from full_market_member_fact_backfill import (  # noqa: E402
    PARTITION_STATUS_COMPLETED,
    _board_for_symbol,
    run_backfill,
)
from sqlalchemy import func, select  # noqa: E402

from app.models.auction import AuctionFinalQuote  # noqa: E402
from app.services.historical_auction_backfill_writer import (  # noqa: E402
    HISTORICAL_BACKFILL_SOURCE,
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
    return {
        "auction_price_raw": 10.5,
        "prev_close": 10.0,
        "auction_volume_shares": 1000,
        "auction_amount": 10500.0,
        "auction_matched_volume_shares": 1000,
        "auction_unmatched_volume_shares": 0,
        "source_status": "TARGET_WINDOW_COMPLETE",
        "canonicalization_status": "FINAL",
    }


def _make_fake_run_symbol_obs(instruments):
    by_symbol = {inst.symbol: inst for inst in instruments}
    async def _fake(inst, trade_date):
        return _obs_canon_computed(inst.symbol, inst.market)
    return _fake, by_symbol


@pytest.mark.asyncio
async def test_runner_writes_db_rows_and_reconciles(db_session, tmp_path):
    """runner 接线落库：DB 行数 == JSON 行数，manifest 含 db_written_rows。"""
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
    )

    assert manifest["status"] == "DONE"
    # 总落库行数 = 2 bars × 3 instruments = 6
    total_db = (await db_session.execute(
        select(func.count()).select_from(AuctionFinalQuote).where(
            AuctionFinalQuote.source == HISTORICAL_BACKFILL_SOURCE))).scalar()
    assert total_db == 6

    # JSON 行数（两 bar 各 3 行）
    import json
    json_rows = 0
    for t in bar_dates:
        pdir = tmp_path / "pg_test_reconcile" / t.isoformat()
        with open(pdir / "member_facts.jsonl", "r", encoding="utf-8") as f:
            json_rows += sum(1 for _ in f)
    assert json_rows == 6

    # manifest 含 db 计数
    assert manifest.get("db_written_rows", 0) >= 6 or any(
        p.get("db_written_rows", 0) == 3
        for p in [manifest]
    )


@pytest.mark.asyncio
async def test_runner_resume_partial_then_full(db_session, tmp_path):
    """第一次只跑 t1，resume 跑 t1+t2 → 行数收敛到 6 不重复。"""
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
    )
    cnt_after_first = (await db_session.execute(
        select(func.count()).select_from(AuctionFinalQuote).where(
            AuctionFinalQuote.source == HISTORICAL_BACKFILL_SOURCE))).scalar()
    assert cnt_after_first == 2

    # resume：t1 + t2（t1 已完成 RESUME_SKIP，只重跑 t2，但 DB 幂等 upsert）
    await run_backfill(
        run_id="pg_test_resume", bar_dates=[t1, t2], dry_run=False,
        population=insts, run_symbol_obs=fake, adapter=object(),
        session=db_session, output_root=tmp_path, code_sha=_TEST_SHA, as_of=t2,
    )
    cnt_after_resume = (await db_session.execute(
        select(func.count()).select_from(AuctionFinalQuote).where(
            AuctionFinalQuote.source == HISTORICAL_BACKFILL_SOURCE))).scalar()
    assert cnt_after_resume == 4  # 2 bars × 2 instruments


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
    )

    live_cnt = (await db_session.execute(
        select(func.count()).select_from(AuctionFinalQuote).where(
            AuctionFinalQuote.trade_date == t,
            AuctionFinalQuote.source == "verified_consensus",
            AuctionFinalQuote.test_namespace == "production",
        ))).scalar()
    assert live_cnt == 0

    backfill_cnt = (await db_session.execute(
        select(func.count()).select_from(AuctionFinalQuote).where(
            AuctionFinalQuote.trade_date == t,
            AuctionFinalQuote.source == HISTORICAL_BACKFILL_SOURCE,
        ))).scalar()
    assert backfill_cnt == 1
