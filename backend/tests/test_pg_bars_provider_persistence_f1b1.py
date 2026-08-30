"""F1B-1 canonical provider boundary and PostgreSQL persistence canary.

Runs only through the registered remote verification database. Fixtures are
fully synthetic and each test is isolated by the shared savepoint fixture.
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal
from typing import cast

import pandas as pd
import pytest
from sqlalchemy import func, select, text

from app.core.pytdx_adapter import PytdxAdapter
from app.models.bar import Bar15Min, Bar60Min, BarDaily
from app.models.instrument import Instrument
from app.repositories import bar_repository as repo
from tests.support.bars_fake_provider import (
    XDXR_ROWS,
    FakeBarsProvider,
)

_PURE_UNIT_TEST = os.environ.get("PURE_UNIT_TEST", "0") == "1"

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        _PURE_UNIT_TEST,
        reason="F1B-1 persistence canary requires the remote verification PostgreSQL",
    ),
]


class _EmptyMinuteProvider(FakeBarsProvider):
    def get_15min_bars(self, symbol: str, count: int) -> pd.DataFrame:
        return pd.DataFrame()

    def get_60min_bars(self, symbol: str, count: int) -> pd.DataFrame:
        return pd.DataFrame()


async def _add_instrument(db_session, *, symbol: str) -> uuid.UUID:
    instrument_id = uuid.uuid4()
    db_session.add(
        Instrument(
            id=instrument_id,
            symbol=symbol,
            name=f"F1B1-{symbol}",
            market="SZ",
            status="active",
            listing_date=date(2010, 1, 4),
        )
    )
    await db_session.commit()
    return instrument_id


async def _count(db_session, model, instrument_id: uuid.UUID) -> int:
    value = await db_session.scalar(
        select(func.count()).select_from(model).where(model.instrument_id == instrument_id)
    )
    return int(value or 0)


def _assert_values_match(row, frame_row: pd.Series) -> None:
    for field in ("open", "high", "low", "close", "volume", "amount", "adj_factor"):
        assert float(getattr(row, field)) == pytest.approx(float(frame_row[field]))


def _invalid_frame(*, minute: bool) -> pd.DataFrame:
    timestamp = "2026-08-28 09:30" if minute else "2026-08-28"
    return pd.DataFrame(
        {
            "datetime": [pd.Timestamp(timestamp)],
            "open": [10.0],
            "high": [9.0],
            "low": [11.0],
            "close": [10.0],
            "volume": [100.0],
            "amount": [1000.0],
            "adj_factor": [1.0],
        }
    )


@pytest.mark.asyncio
async def test_f1b1_serial_provider_to_postgres_contract(db_session) -> None:
    db_name = await db_session.scalar(text("SELECT current_database()"))
    assert db_name != "bz_stock"
    assert str(db_name).startswith("bz_stock_verify_")

    instrument_id = await _add_instrument(db_session, symbol="009901")
    daily_provider = FakeBarsProvider(
        xdxr_mode=XDXR_ROWS,
        xdxr_event_dates=["2026-08-14"],
        base_close=10.0,
    )
    daily_result = await repo.refresh_daily_bars(
        db_session,
        instrument_id,
        date(2026, 8, 10),
        date(2026, 8, 21),
        adapter=cast(PytdxAdapter, daily_provider),
    )
    assert not daily_result.empty
    assert daily_result.index.name == "trade_date"
    assert any(call.startswith("get_daily_bars:009901:") for call in daily_provider.calls)
    assert "get_xdxr_info:009901" in daily_provider.calls

    daily_rows = (
        await db_session.execute(
            select(BarDaily)
            .where(BarDaily.instrument_id == instrument_id)
            .order_by(BarDaily.trade_date)
        )
    ).scalars().all()
    assert len(daily_rows) == len(daily_result)
    assert [float(row.adj_factor) for row in daily_rows] == pytest.approx(
        daily_result["adj_factor"].astype(float).tolist()
    )
    assert any(float(row.adj_factor) != 1.0 for row in daily_rows)
    _assert_values_match(daily_rows[0], daily_result.iloc[0])

    original_count = len(daily_rows)
    updated_provider = FakeBarsProvider(
        xdxr_mode=XDXR_ROWS,
        xdxr_event_dates=["2026-08-14"],
        base_close=20.0,
    )
    updated_result = await repo.refresh_daily_bars(
        db_session,
        instrument_id,
        date(2026, 8, 10),
        date(2026, 8, 21),
        adapter=cast(PytdxAdapter, updated_provider),
    )
    assert await _count(db_session, BarDaily, instrument_id) == original_count
    first_daily_close = await db_session.scalar(
        select(BarDaily.close)
        .where(BarDaily.instrument_id == instrument_id)
        .order_by(BarDaily.trade_date)
        .limit(1)
    )
    assert first_daily_close is not None
    assert float(first_daily_close) == pytest.approx(float(updated_result.iloc[0]["close"]))

    db_session.add(
        BarDaily(
            instrument_id=instrument_id,
            trade_date=date(2026, 8, 28),
            open=Decimal("30.00"),
            high=Decimal("31.00"),
            low=Decimal("29.00"),
            close=Decimal("30.50"),
            volume=Decimal("1000"),
            amount=Decimal("30500"),
            adj_factor=Decimal("0.75"),
        )
    )
    await db_session.commit()

    minute_provider = FakeBarsProvider(base_close=30.0)
    result_15m = await repo.refresh_15min_bars(
        db_session, instrument_id, count=8, adapter=cast(PytdxAdapter, minute_provider),
    )
    result_60m = await repo.refresh_60min_bars(
        db_session, instrument_id, count=6, adapter=cast(PytdxAdapter, minute_provider),
    )
    assert result_15m.index.name == "trade_time"
    assert result_60m.index.name == "trade_time"
    assert await _count(db_session, Bar15Min, instrument_id) == len(result_15m) == 8
    assert await _count(db_session, Bar60Min, instrument_id) == len(result_60m) == 6

    rows_15m = (
        await db_session.execute(
            select(Bar15Min)
            .where(Bar15Min.instrument_id == instrument_id)
            .order_by(Bar15Min.trade_time)
        )
    ).scalars().all()
    rows_60m = (
        await db_session.execute(
            select(Bar60Min)
            .where(Bar60Min.instrument_id == instrument_id)
            .order_by(Bar60Min.trade_time)
        )
    ).scalars().all()
    expected_factor = float(
        await db_session.scalar(
            select(BarDaily.adj_factor).where(
                BarDaily.instrument_id == instrument_id,
                BarDaily.trade_date == date(2026, 8, 28),
            )
        )
    )
    assert expected_factor == pytest.approx(0.75)
    assert [float(row.adj_factor) for row in rows_15m] == pytest.approx(
        [expected_factor] * len(rows_15m)
    )
    assert [float(row.adj_factor) for row in rows_60m] == pytest.approx(
        [expected_factor] * len(rows_60m)
    )
    _assert_values_match(rows_15m[0], result_15m.iloc[0])
    _assert_values_match(rows_60m[0], result_60m.iloc[0])

    updated_minute_provider = FakeBarsProvider(base_close=40.0)
    updated_15m = await repo.refresh_15min_bars(
        db_session,
        instrument_id,
        count=8,
        adapter=cast(PytdxAdapter, updated_minute_provider),
    )
    updated_60m = await repo.refresh_60min_bars(
        db_session,
        instrument_id,
        count=6,
        adapter=cast(PytdxAdapter, updated_minute_provider),
    )
    db_session.expire_all()
    assert await _count(db_session, Bar15Min, instrument_id) == 8
    assert await _count(db_session, Bar60Min, instrument_id) == 6
    first_15m = await db_session.scalar(
        select(Bar15Min)
        .where(Bar15Min.instrument_id == instrument_id)
        .order_by(Bar15Min.trade_time)
        .limit(1)
    )
    first_60m = await db_session.scalar(
        select(Bar60Min)
        .where(Bar60Min.instrument_id == instrument_id)
        .order_by(Bar60Min.trade_time)
        .limit(1)
    )
    assert first_15m is not None and first_60m is not None
    _assert_values_match(first_15m, updated_15m.iloc[0])
    _assert_values_match(first_60m, updated_60m.iloc[0])

    before_daily = await _count(db_session, BarDaily, instrument_id)
    before_15m = await _count(db_session, Bar15Min, instrument_id)
    assert await repo.persist_daily_bars(
        db_session, instrument_id, _invalid_frame(minute=False), symbol="009901"
    ) == 0
    assert await repo._upsert_15min_bars(
        db_session, instrument_id, _invalid_frame(minute=True), symbol="009901"
    ) == 0
    assert await _count(db_session, BarDaily, instrument_id) == before_daily
    assert await _count(db_session, Bar15Min, instrument_id) == before_15m

    empty_provider = _EmptyMinuteProvider(daily_empty=True)
    empty_daily = await repo.refresh_daily_bars(
        db_session,
        instrument_id,
        date(2026, 8, 10),
        date(2026, 8, 21),
        adapter=cast(PytdxAdapter, empty_provider),
    )
    empty_15m = await repo.refresh_15min_bars(
        db_session, instrument_id, count=8, adapter=cast(PytdxAdapter, empty_provider),
    )
    empty_60m = await repo.refresh_60min_bars(
        db_session, instrument_id, count=6, adapter=cast(PytdxAdapter, empty_provider),
    )
    assert empty_daily.empty and empty_15m.empty and empty_60m.empty
    assert await _count(db_session, BarDaily, instrument_id) == before_daily
    assert await _count(db_session, Bar15Min, instrument_id) == before_15m
    assert await _count(db_session, Bar60Min, instrument_id) == 6


@pytest.mark.asyncio
async def test_f1b1_daily_persistence_exception_rolls_back(db_session) -> None:
    instrument_id = await _add_instrument(db_session, symbol="009902")
    suffix = uuid.uuid4().hex
    function_name = f"_f1b1_fail_{suffix}"
    trigger_name = f"_f1b1_fail_{suffix}"
    await db_session.execute(
        text(
            f"CREATE FUNCTION {function_name}() RETURNS trigger AS $$ "
            "BEGIN IF NEW.close = 999 THEN RAISE EXCEPTION 'f1b1 injected failure'; "
            "END IF; RETURN NEW; END $$ LANGUAGE plpgsql"
        )
    )
    await db_session.execute(
        text(
            f"CREATE TRIGGER {trigger_name} BEFORE INSERT OR UPDATE ON bars_daily "
            f"FOR EACH ROW EXECUTE FUNCTION {function_name}()"
        )
    )
    await db_session.commit()

    frame = pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-08-27", "2026-08-28"]),
            "open": [10.0, 999.0],
            "high": [11.0, 1000.0],
            "low": [9.0, 998.0],
            "close": [10.5, 999.0],
            "volume": [100.0, 200.0],
            "amount": [1050.0, 199800.0],
            "adj_factor": [1.0, 1.0],
        }
    )

    with pytest.raises(Exception, match="f1b1 injected failure"):
        await repo.persist_daily_bars(
            db_session, instrument_id, frame, symbol="009902",
        )

    assert await _count(db_session, BarDaily, instrument_id) == 0
