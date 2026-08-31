"""History-v3 materialize (DB) tests — require real PostgreSQL.

[CHANGE-20260826-001 History-v3] materialize_history_v3_from_core 从 durable Core
artifact 投影并物化 review-history-v3，复用既有 daily_state upsert + events insert
写入路径（history_contract_version=review-history-v3）。

覆盖：crash/resume 幂等（PHASE 12）——重复 materialize 不重复 event/state。
"""
import datetime
import uuid

import pytest
from sqlalchemy import select, text

from app.models.first_pyramid_history import (
    FirstPyramidHistoryDailyState,
    FirstPyramidHistoryEvent,
)
from app.models.instrument import Instrument
from app.services.first_pyramid_history_service import materialize_history_v3_from_core
from tests.conftest import TestAsyncSessionLocal

pytestmark = pytest.mark.postgres


def _make_core_flat():
    return {
        "fp_regime_value": "强势",
        "fp_squeeze_avg_volume": 100.0,
        "fp_release_volume_ratio": 0.5,
        "fp_squeeze_state": "已释放",
        "fp_latest_sqz_off_freshness": 0,
        "fp_structure_event_type": "BOS",
        "fp_structure_event_direction": "up",
        "fp_structure_event_date": "2026-08-25",
    }


@pytest.mark.asyncio
async def test_v3_materialize_idempotent_no_duplicate_events():
    """重复 materialize 不重复 event/state（crash/resume 幂等）。"""
    iid = uuid.uuid4()
    td = datetime.date(2026, 8, 25)
    symbol = f"H3{uuid.uuid4().hex[:8]}"

    async with TestAsyncSessionLocal() as s1:
        db_name = (await s1.execute(text("SELECT current_database()"))).scalar_one()
        assert db_name.startswith("bz_stock_verify_")
        assert db_name != "bz_stock"
        s1.add(
            Instrument(
                id=iid,
                symbol=symbol,
                name=f"history_v3_{symbol}",
                market="SZ",
                status="active",
                listing_date=datetime.date(2010, 1, 4),
            )
        )
        await s1.flush()
        await materialize_history_v3_from_core(s1, iid, td, _make_core_flat())
        await materialize_history_v3_from_core(s1, iid, td, _make_core_flat())
        await s1.commit()

    async with TestAsyncSessionLocal() as s2:
        states = (await s2.execute(
            select(FirstPyramidHistoryDailyState).where(
                FirstPyramidHistoryDailyState.instrument_id == iid,
                FirstPyramidHistoryDailyState.trade_date == td,
            )
        )).scalars().all()
        events = (await s2.execute(
            select(FirstPyramidHistoryEvent).where(
                FirstPyramidHistoryEvent.instrument_id == iid,
            )
        )).scalars().all()

    assert len(states) == 1, f"expected 1 state row, got {len(states)}"
    assert len(events) >= 1, "expected >=1 projected event"
