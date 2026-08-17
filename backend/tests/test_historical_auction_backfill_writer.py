"""历史竞价落库 writer 单测 — [CHANGE-20260817-001]。

覆盖：
1. 纯逻辑：project_row_to_fact 字段投影与 quality_status 映射（PURE 可跑）。
2. CaptureRun 创建/幂等复用（PG fixture）。
3. write_bar_quotes 幂等 upsert（chunk 分批、重跑安全、计数正确）（PG fixture）。
4. resume 补写：先写部分行，再 upsert 全量，行数收敛到全量（PG fixture）。

PG 相关用例在 PURE_UNIT_TEST=1 下自动 skip（conftest 标记）。
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.auction import AuctionFinalQuote, AuctionQuoteCaptureRun
from app.services.historical_auction_backfill_writer import (
    HISTORICAL_BACKFILL_NAMESPACE,
    HISTORICAL_BACKFILL_SOURCE,
    MemberFactProjection,
    get_or_create_historical_capture_run,
    project_row_to_fact,
    write_bar_quotes,
)


# ---------------------------------------------------------------------------
# 纯逻辑（PURE_UNIT_TEST=1 可跑）
# ---------------------------------------------------------------------------

def _sample_obs(price=10.5, vol=1000, amount=10500.0, src="TARGET_WINDOW_COMPLETE",
                canon="CANONICAL"):
    """正式 production row 词表（对齐 _project_member_row / project_row_to_fact）。

    [FIX] 旧 fixture 用 prev_close / auction_matched_volume_shares / canon=FINAL；
    正式字段为 previous_close_raw（lane_b.raw_close_Tm1 投影）与 CANONICAL 状态。
    """
    return {
        "auction_price_raw": price,
        "previous_close_raw": 10.0,
        "auction_volume_shares": vol,
        "auction_amount": amount,
        "auction_amount_source_type": "DERIVED_PRICE_X_NORMALIZED_VOLUME",
        "source_status": src,
        "canonicalization_status": canon,
    }


def test_project_row_to_fact_maps_fields():
    inst_id = uuid.uuid4()
    t = date(2026, 8, 14)
    fact = project_row_to_fact(_sample_obs(), inst_id, t)
    assert isinstance(fact, MemberFactProjection)
    assert fact.instrument_id == inst_id
    assert fact.trade_date == t
    assert fact.final_price == 10.5
    assert fact.prev_close == 10.0
    assert fact.volume == 1000
    assert fact.amount == 10500.0
    assert fact.matched_volume == 1000
    assert fact.unmatched_volume == 0
    assert fact.quality_status == "ok"
    assert "backfill_source:TARGET_WINDOW_COMPLETE" in fact.reason_codes
    assert "backfill_canon:CANONICAL" in fact.reason_codes
    assert fact.raw_payload["auction_price_raw"] == 10.5


def test_project_row_to_fact_quality_zero_volume_on_source_empty():
    inst_id = uuid.uuid4()
    fact = project_row_to_fact(_sample_obs(src="SOURCE_EMPTY"), inst_id, date(2026, 8, 14))
    assert fact.quality_status == "zero_volume"


def test_project_row_to_fact_quality_error_on_source_error():
    inst_id = uuid.uuid4()
    fact = project_row_to_fact(_sample_obs(src="SOURCE_ERROR"), inst_id, date(2026, 8, 14))
    assert fact.quality_status == "source_error"


def test_project_row_to_fact_quality_invalid_volume_on_canon():
    inst_id = uuid.uuid4()
    fact = project_row_to_fact(
        _sample_obs(canon="INVALID_VOLUME_0925"), inst_id, date(2026, 8, 14))
    assert fact.quality_status == "invalid_volume"


def test_member_fact_projection_normalizes_unknown_quality():
    f = MemberFactProjection(
        instrument_id=uuid.uuid4(), trade_date=date(2026, 8, 14),
        final_price=1.0, prev_close=1.0, volume=1, amount=1.0,
        matched_volume=1, unmatched_volume=0,
        quality_status="bogus_value", reason_codes=[], raw_payload={},
    )
    assert f.quality_status == "source_error"


# ---------------------------------------------------------------------------
# PG 集成（PURE_UNIT_TEST=1 下 skip）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_or_create_capture_run_idempotent(db_session):
    t = date(2026, 7, 1)
    run1 = await get_or_create_historical_capture_run(db_session, t)
    await db_session.flush()
    run2 = await get_or_create_historical_capture_run(db_session, t)
    await db_session.flush()
    assert run1.id == run2.id
    assert run1.source == HISTORICAL_BACKFILL_SOURCE
    assert run1.test_namespace == HISTORICAL_BACKFILL_NAMESPACE
    # 表中确实只有一条
    cnt = (await db_session.execute(
        __import__("sqlalchemy").select(__import__("sqlalchemy").func.count()).select_from(
            AuctionQuoteCaptureRun))).scalar()
    assert cnt == 1


@pytest.mark.asyncio
async def test_write_bar_quotes_idempotent_upsert(db_session):
    t = date(2026, 7, 2)
    run = await get_or_create_historical_capture_run(db_session, t)
    await db_session.flush()

    inst_a = uuid.uuid4()
    inst_b = uuid.uuid4()
    facts = [
        project_row_to_fact(_sample_obs(price=10.0, vol=500, amount=5000.0), inst_a, t),
        project_row_to_fact(_sample_obs(price=20.0, vol=800, amount=16000.0), inst_b, t),
    ]
    res1 = await write_bar_quotes(db_session, t, run, facts, chunk_size=1)
    await db_session.commit()
    assert res1["written"] == 2
    assert res1["failed"] == 0

    # 重跑：相同 facts（价格更新）→ 幂等 upsert，行数不增，值更新
    facts2 = [
        project_row_to_fact(_sample_obs(price=11.0, vol=550, amount=6050.0), inst_a, t),
        project_row_to_fact(_sample_obs(price=21.0, vol=850, amount=17850.0), inst_b, t),
    ]
    res2 = await write_bar_quotes(db_session, t, run, facts2, chunk_size=1)
    await db_session.commit()
    assert res2["written"] == 2
    assert res2["failed"] == 0

    rows = (await db_session.execute(
        __import__("sqlalchemy").select(AuctionFinalQuote).where(
            AuctionFinalQuote.trade_date == t,
            AuctionFinalQuote.source == HISTORICAL_BACKFILL_SOURCE,
        ))).scalars().all()
    assert len(rows) == 2
    by_inst = {r.instrument_id: r for r in rows}
    assert by_inst[inst_a].final_price == Decimal("11.0")
    assert by_inst[inst_b].final_price == Decimal("21.0")


@pytest.mark.asyncio
async def test_write_bar_quotes_skips_none_instrument(db_session):
    t = date(2026, 7, 3)
    run = await get_or_create_historical_capture_run(db_session, t)
    await db_session.flush()
    facts = [None, project_row_to_fact(_sample_obs(), uuid.uuid4(), t)]
    res = await write_bar_quotes(db_session, t, run, facts, chunk_size=1)
    await db_session.commit()
    assert res["written"] == 1
    assert res["skipped"] == 1


@pytest.mark.asyncio
async def test_resume_partial_write_completes(db_session):
    """模拟进程中断：先写部分行，resume 时 upsert 全量，行数收敛到全量。"""
    t = date(2026, 7, 4)
    run = await get_or_create_historical_capture_run(db_session, t)
    await db_session.flush()

    inst_a = uuid.uuid4()
    inst_b = uuid.uuid4()
    inst_c = uuid.uuid4()
    all_facts = [
        project_row_to_fact(_sample_obs(price=1.0), inst_a, t),
        project_row_to_fact(_sample_obs(price=2.0), inst_b, t),
        project_row_to_fact(_sample_obs(price=3.0), inst_c, t),
    ]
    # 第一次中断：只写了 a, b
    res_first = await write_bar_quotes(db_session, t, run, all_facts[:2], chunk_size=1)
    await db_session.commit()
    assert res_first["written"] == 2

    # resume：重跑全量（幂等 upsert 补 c，a/b 更新）
    res_resume = await write_bar_quotes(db_session, t, run, all_facts, chunk_size=1)
    await db_session.commit()
    assert res_resume["written"] == 3

    cnt = (await db_session.execute(
        __import__("sqlalchemy").select(__import__("sqlalchemy").func.count()).select_from(
            AuctionFinalQuote).where(
            AuctionFinalQuote.trade_date == t,
            AuctionFinalQuote.source == HISTORICAL_BACKFILL_SOURCE,
        ))).scalar()
    assert cnt == 3


@pytest.mark.asyncio
async def test_isolated_from_live_namespace(db_session):
    """历史回补 source/namespace 与 live（verified_consensus/production）隔离。"""
    t = date(2026, 7, 5)
    run = await get_or_create_historical_capture_run(db_session, t)
    await db_session.flush()
    facts = [project_row_to_fact(_sample_obs(), uuid.uuid4(), t)]
    await write_bar_quotes(db_session, t, run, facts, chunk_size=1)
    await db_session.commit()

    # live 路径查询 verified_consensus/production 不应看到 backfill 行
    live_rows = (await db_session.execute(
        __import__("sqlalchemy").select(AuctionFinalQuote).where(
            AuctionFinalQuote.trade_date == t,
            AuctionFinalQuote.source == "verified_consensus",
            AuctionFinalQuote.test_namespace == "production",
        ))).scalars().all()
    assert len(live_rows) == 0

    backfill_rows = (await db_session.execute(
        __import__("sqlalchemy").select(AuctionFinalQuote).where(
            AuctionFinalQuote.trade_date == t,
            AuctionFinalQuote.source == HISTORICAL_BACKFILL_SOURCE,
        ))).scalars().all()
    assert len(backfill_rows) == 1
