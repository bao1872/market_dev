"""F1B-1 canonical provider boundary and PostgreSQL persistence canary.

Runs only through the registered remote verification database. Fixtures are
fully synthetic and each test is isolated by the shared savepoint fixture.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy import delete, func, select, text

from app.core.pytdx_adapter import PytdxAdapter
from app.models.bar import Bar15Min, Bar60Min, BarDaily
from app.models.instrument import Instrument
from app.repositories import bar_repository as repo
from app.services import bars_scheduler_service as scheduler_module
from app.services.bars_scheduler_service import BarsSchedulerService
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


async def _bars_snapshot(db_session, instrument_ids: list[uuid.UUID]) -> dict[str, list[tuple]]:
    snapshots: dict[str, list[tuple]] = {}
    for name, model, time_column in (
        ("d", BarDaily, BarDaily.trade_date),
        ("15m", Bar15Min, Bar15Min.trade_time),
        ("60m", Bar60Min, Bar60Min.trade_time),
    ):
        rows = (
            await db_session.execute(
                select(
                    model.instrument_id,
                    time_column,
                    model.open,
                    model.high,
                    model.low,
                    model.close,
                    model.volume,
                    model.amount,
                    model.adj_factor,
                )
                .where(model.instrument_id.in_(instrument_ids))
                .order_by(model.instrument_id, time_column)
            )
        ).all()
        snapshots[name] = [tuple(row) for row in rows]
    return snapshots


# ---------------------------------------------------------------------------
# [F1B-2 P1-B] PostgreSQL non-false-green parallel write proof
# ---------------------------------------------------------------------------

_PG_SYMBOLS = ["009911", "009912", "009913", "009914"]
_PG_BASE_CLOSE = 21.0
# 固定业务日 → daily 回看窗口可精确推导（shanghai_business_date 被 monkeypatch）
_PG_FIXED_END = date(2026, 8, 28)
_PG_DAILY_LOOKBACK = 5

# 精确期望行数（4 个 synthetic 标的）：
#   d   : bdate_range(2026-08-23, 2026-08-28) = 5 个交易日 × 4
#   15m : DAILY_COUNTS["15m"] = 50 行 × 4
#   60m : DAILY_COUNTS["60m"] = 10 行 × 4
EXPECTED_D_ROWS = 20
EXPECTED_15M_ROWS = 200
EXPECTED_60M_ROWS = 40


async def _count_many(db_session, model, instrument_ids: list[uuid.UUID]) -> int:
    value = await db_session.scalar(
        select(func.count())
        .select_from(model)
        .where(model.instrument_id.in_(instrument_ids))
    )
    return int(value or 0)


async def _row_counts(
    db_session, instrument_ids: list[uuid.UUID]
) -> tuple[int, int, int]:
    return (
        await _count_many(db_session, BarDaily, instrument_ids),
        await _count_many(db_session, Bar15Min, instrument_ids),
        await _count_many(db_session, Bar60Min, instrument_ids),
    )


async def _delete_bars(db_session, instrument_ids: list[uuid.UUID]) -> None:
    """[§8-B] 显式删除这批 synthetic 标的三张行情表数据（仅 verification DB）。

    parallel 必须从**空表**证明自己真的写入；禁止依赖 serial 预写数据。
    """
    for model in (BarDaily, Bar15Min, Bar60Min):
        await db_session.execute(
            delete(model).where(model.instrument_id.in_(instrument_ids))
        )
    await db_session.commit()


async def _assert_value_contract(
    db_session, instrument_ids: list[uuid.UUID]
) -> None:
    """[§10] 字段级校验：OHLCV / amount / adj_factor / trade_date / trade_time。"""
    target = instrument_ids[0]

    # --- daily ---
    daily_rows = (
        await db_session.execute(
            select(BarDaily)
            .where(BarDaily.instrument_id == target)
            .order_by(BarDaily.trade_date)
        )
    ).scalars().all()
    expected_dates = [
        ts.date()
        for ts in pd.bdate_range(
            _PG_FIXED_END - timedelta(days=_PG_DAILY_LOOKBACK), _PG_FIXED_END
        )
    ]
    assert [row.trade_date for row in daily_rows] == expected_dates
    for index, row in enumerate(daily_rows):
        close = _PG_BASE_CLOSE + index * 0.5
        assert float(row.open) == pytest.approx(close)
        assert float(row.close) == pytest.approx(close)
        assert float(row.high) == pytest.approx(round(close * 1.01, 4))
        assert float(row.low) == pytest.approx(round(close * 0.99, 4))
        assert float(row.volume) == pytest.approx(1000.0)
        assert float(row.amount) == pytest.approx(round(close * 1000.0, 4))
        # xdxr 为 NONE → 无除权事件 → adj_factor 恒为 1.0
        assert float(row.adj_factor) == pytest.approx(1.0)

    # --- minute (15m / 60m) ---
    for model, expected_rows in ((Bar15Min, 50), (Bar60Min, 10)):
        rows = (
            await db_session.execute(
                select(model)
                .where(model.instrument_id == target)
                .order_by(model.trade_time)
            )
        ).scalars().all()
        assert len(rows) == expected_rows
        times = [row.trade_time for row in rows]
        assert all(time is not None for time in times)
        assert len(set(times)) == expected_rows  # trade_time 唯一，无重复行
        assert times == sorted(times)
        first = rows[0]
        assert float(first.close) == pytest.approx(_PG_BASE_CLOSE)
        assert float(first.open) == pytest.approx(_PG_BASE_CLOSE)
        assert float(first.volume) == pytest.approx(100.0)
        assert float(first.amount) == pytest.approx(round(_PG_BASE_CLOSE * 100.0, 4))
        assert float(first.adj_factor) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_f1b2_scheduler_spawn_postgres_equivalence_and_idempotency(
    db_session, monkeypatch, tmp_path
) -> None:
    """[F1B-2 §8-§11] parallel 必须证明自己从空表真实写入，且幂等。

    non-false-green 关键：旧版先 serial 写入、再 parallel 覆写同一批行，
    即便 parallel 一行未写，snapshot 比较也会通过。本版先**清空**再跑 parallel，
    parallel 若未真实写入，行数断言必然失败。
    """
    db_name = await db_session.scalar(text("SELECT current_database()"))
    assert db_name != "bz_stock"
    assert str(db_name).startswith("bz_stock_verify_")

    instrument_ids = [
        await _add_instrument(db_session, symbol=symbol) for symbol in _PG_SYMBOLS
    ]
    instruments = [
        SimpleNamespace(id=instrument_id, symbol=symbol)
        for instrument_id, symbol in zip(instrument_ids, _PG_SYMBOLS, strict=True)
    ]
    monkeypatch.setattr(
        scheduler_module, "is_trading_day_async", AsyncMock(return_value=True)
    )
    # 固定业务日：使 daily 回看窗口与期望行数可精确推导
    monkeypatch.setattr(
        scheduler_module, "shanghai_business_date", lambda: _PG_FIXED_END
    )
    monkeypatch.setattr(
        scheduler_module,
        "get_pytdx_adapter",
        lambda: FakeBarsProvider(base_close=_PG_BASE_CLOSE),
    )

    async def no_post_d(*_args, **_kwargs):
        return None

    # === A. serial run → capture serial_snapshot ===
    serial = BarsSchedulerService(fetch_processes=1)
    monkeypatch.setattr(
        serial, "_get_active_instruments", AsyncMock(return_value=instruments)
    )
    monkeypatch.setattr(serial, "_run_post_daily_phase", no_post_d)
    serial_result = await serial.refresh_all_instruments(
        _PG_FIXED_END, db_session=db_session, trigger_dsa=False
    )
    assert serial_result.failed == 0
    assert serial_result.period_counts == {
        "d": EXPECTED_D_ROWS,
        "15m": EXPECTED_15M_ROWS,
        "60m": EXPECTED_60M_ROWS,
    }
    serial_snapshot = await _bars_snapshot(db_session, instrument_ids)
    assert all(serial_snapshot[period] for period in ("d", "15m", "60m"))
    assert await _row_counts(db_session, instrument_ids) == (
        EXPECTED_D_ROWS,
        EXPECTED_15M_ROWS,
        EXPECTED_60M_ROWS,
    )

    # === B/C. 清空 → 确认 row_count == 0（parallel 的起点必须是空表）===
    await _delete_bars(db_session, instrument_ids)
    assert await _row_counts(db_session, instrument_ids) == (0, 0, 0)

    # === D/E/F. parallel workers=2 从空表写入 → 精确行数断言 ===
    adapter_spec = {
        "module": "tests.support.bars_fake_provider",
        "attr": "build_fake_provider",
        "kwargs": {
            "base_close": _PG_BASE_CLOSE,
            "latency_seconds": 0.05,
            "trace_dir": str(tmp_path / "pg-spawn-trace"),
        },
    }
    parallel = BarsSchedulerService(fetch_processes=2, adapter_spec=adapter_spec)
    monkeypatch.setattr(
        parallel, "_get_active_instruments", AsyncMock(return_value=instruments)
    )
    monkeypatch.setattr(parallel, "_run_post_daily_phase", no_post_d)

    first_parallel = await parallel.refresh_all_instruments(
        _PG_FIXED_END, db_session=db_session, trigger_dsa=False
    )
    assert first_parallel.failed == 0
    # parallel 真的写了：行数 > 0 且等于精确期望
    counts_after_first = await _row_counts(db_session, instrument_ids)
    assert all(count > 0 for count in counts_after_first), (
        f"parallel 未写入任何行情行: {counts_after_first}"
    )
    assert counts_after_first == (
        EXPECTED_D_ROWS,
        EXPECTED_15M_ROWS,
        EXPECTED_60M_ROWS,
    )
    # §10：period_counts 与实际写入规模相符
    assert first_parallel.period_counts == {
        "d": EXPECTED_D_ROWS,
        "15m": EXPECTED_15M_ROWS,
        "60m": EXPECTED_60M_ROWS,
    }

    # === G. parallel_snapshot == serial_snapshot（此时才是有意义的等价）===
    parallel_snapshot = await _bars_snapshot(db_session, instrument_ids)
    assert parallel_snapshot == serial_snapshot

    # 真实 spawn ProcessPool 证据
    assert parallel.last_process_metrics["pool_creations"] == 1
    assert parallel.last_process_metrics["max_inflight_observed"] <= 4
    assert len(parallel.last_process_metrics["distinct_child_pids"]) >= 2

    # === §10 字段级校验（OHLCV / amount / adj_factor / trade_date / trade_time）===
    await _assert_value_contract(db_session, instrument_ids)

    # === H/§11. 第二次 parallel → 幂等（不增长、snapshot 一致）===
    second_parallel = await parallel.refresh_all_instruments(
        _PG_FIXED_END, db_session=db_session, trigger_dsa=False
    )
    assert second_parallel.failed == 0
    assert await _row_counts(db_session, instrument_ids) == (
        EXPECTED_D_ROWS,
        EXPECTED_15M_ROWS,
        EXPECTED_60M_ROWS,
    )
    idempotent_snapshot = await _bars_snapshot(db_session, instrument_ids)
    assert idempotent_snapshot == serial_snapshot
