"""Slice 1 (REVIEW-CURRENT-OWNER-01) POSTGRES tests — current facts locked to Core run.

[CHANGE-20260826-001 Slice 1] Review(T) current First Pyramid facts come ONLY from the
published StockFeatureSnapshot(T) locked to source_core_run_id.

Requires real PostgreSQL (verify DB): KPI-2/KPI-3/Case C.
- same-day two runs → Review consumes only source_core_run_id's snapshot
- wrong same-day run → empty (fail-closed), no fallback to latest
"""
import datetime
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.services.review_observation_prep_service import (
    _load_current_only_snapshot_facts,
)

pytestmark = pytest.mark.postgres


def _mk_run(rid, trade_date, status="succeeded", published=True):
    return StockFeatureSnapshotRun(
        id=rid,
        trade_date=trade_date,
        status=status,
        published_at=datetime.datetime.utcnow() if published else None,
    )


def _mk_snap(iid, rid, trade_date, flat):
    return StockFeatureSnapshot(
        instrument_id=iid,
        source_run_id=rid,
        trade_date=trade_date,
        summary_payload={"first_pyramid_flat": flat},
    )


@pytest.mark.asyncio
async def test_current_facts_locked_to_source_core_run_id():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    iid = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
    td = datetime.date(2026, 8, 25)
    correct_run = uuid.UUID("11111111-1111-1111-1111-111111111111")
    wrong_run = uuid.UUID("22222222-2222-2222-2222-222222222222")

    async with factory() as s:
        s.add_all([
            _mk_run(correct_run, td),
            _mk_run(wrong_run, td),
            _mk_snap(iid, correct_run, td, {"fp_regime_value": "强势"}),
            _mk_snap(iid, wrong_run, td, {"fp_regime_value": "弱势"}),
        ])
        await s.commit()

    async with factory() as s:
        out = await _load_current_only_snapshot_facts(
            s, [iid], td, source_core_run_id=correct_run
        )
        out_wrong = await _load_current_only_snapshot_facts(
            s, [iid], td, source_core_run_id=wrong_run
        )

    assert out[str(iid)]["regime_value"] == "强势"
    assert out_wrong[str(iid)]["regime_value"] == "弱势"
    assert "弱势" not in str(out)
    assert "强势" not in str(out_wrong)


@pytest.mark.asyncio
async def test_current_facts_wrong_run_fails_closed():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    iid = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
    td = datetime.date(2026, 8, 25)
    present_run = uuid.UUID("33333333-3333-3333-3333-333333333333")
    missing_run = uuid.UUID("44444444-4444-4444-4444-444444444444")

    async with factory() as s:
        s.add_all([
            _mk_run(present_run, td),
            _mk_snap(iid, present_run, td, {"fp_regime_value": "强势"}),
        ])
        await s.commit()

    async with factory() as s:
        out = await _load_current_only_snapshot_facts(
            s, [iid], td, source_core_run_id=missing_run
        )
    assert out == {}, "wrong core run must fail closed (empty), never fallback"
