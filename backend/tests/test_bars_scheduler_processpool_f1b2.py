"""F1B-2 bounded spawn ProcessPool integration contracts."""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import time
import uuid
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import bars_scheduler_service as scheduler_module
from app.services.bars_scheduler_service import (
    BarsSchedulerService,
    PoolFatalError,
    _InstrumentItem,
)


def _instruments(count: int) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(id=uuid.uuid4(), symbol=f"{990000 + index:06d}")
        for index in range(count)
    ]


def _adapter_spec(trace_dir: Path, **kwargs: object) -> dict[str, Any]:
    return {
        "module": "tests.support.bars_fake_provider",
        "attr": "build_fake_provider",
        "kwargs": {"trace_dir": str(trace_dir), **kwargs},
    }


def _read_traces(trace_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in trace_dir.glob("*.jsonl"):
        records.extend(
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        )
    return records


async def _configure_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    service: BarsSchedulerService,
    instruments: list[SimpleNamespace],
    persist_events: list[dict[str, Any]],
    post_d_events: list[tuple[str, float]],
) -> None:
    monkeypatch.setattr(
        scheduler_module, "is_trading_day_async", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        service, "_get_active_instruments", AsyncMock(return_value=instruments)
    )
    active_writers = 0

    async def persist(item, raw_result, _db_session):
        nonlocal active_writers
        active_writers += 1
        started = time.time()
        await asyncio.sleep(0.005)
        ended = time.time()
        persist_events.append(
            {
                "period": raw_result["period"],
                "symbol": item.symbol,
                "pid": os.getpid(),
                "started": started,
                "ended": ended,
                "concurrency": active_writers,
            }
        )
        active_writers -= 1
        return len(raw_result["raw_df"])

    async def post_d(*_args, **_kwargs):
        post_d_events.append(("start", time.time()))
        await asyncio.sleep(0.01)
        post_d_events.append(("end", time.time()))

    monkeypatch.setattr(service, "_persist_provider_result", persist)
    monkeypatch.setattr(service, "_run_post_daily_phase", post_d)


@pytest.mark.asyncio
async def test_real_scheduler_spawn_pool_barriers_and_parent_serial_write(
    monkeypatch, tmp_path
) -> None:
    trace_dir = tmp_path / "trace"
    service = BarsSchedulerService(
        fetch_processes=2,
        adapter_spec=_adapter_spec(trace_dir, latency_seconds=0.06),
    )
    instruments = _instruments(8)
    persist_events: list[dict[str, Any]] = []
    post_d_events: list[tuple[str, float]] = []
    await _configure_scheduler(
        monkeypatch, service, instruments, persist_events, post_d_events
    )

    result = await service.refresh_all_instruments(
        date(2026, 8, 28), db_session=object(), trigger_dsa=False
    )

    traces = _read_traces(trace_dir)
    provider = [record for record in traces if record["event"] in {"daily", "15m", "60m"}]
    child_pids = {int(record["pid"]) for record in provider}
    assert len(child_pids) >= 2
    assert os.getpid() not in child_pids
    daily_windows = [record for record in provider if record["event"] == "daily"]
    assert any(
        left["pid"] != right["pid"]
        and max(left["t0"], right["t0"]) < min(left["t1"], right["t1"])
        for index, left in enumerate(daily_windows)
        for right in daily_windows[index + 1 :]
    )
    assert service.last_process_metrics["pool_creations"] == 1
    assert service.last_process_metrics["max_inflight_observed"] <= 4
    assert result.failed == 0

    assert {event["pid"] for event in persist_events} == {os.getpid()}
    assert max(event["concurrency"] for event in persist_events) == 1
    d_persist_end = max(
        event["ended"] for event in persist_events if event["period"] == "d"
    )
    post_d_start = dict(post_d_events)["start"]
    post_d_end = dict(post_d_events)["end"]
    first_15m_start = min(
        record["t0"] for record in provider if record["event"] == "15m"
    )
    last_15m_persist = max(
        event["ended"] for event in persist_events if event["period"] == "15m"
    )
    first_60m_start = min(
        record["t0"] for record in provider if record["event"] == "60m"
    )
    assert d_persist_end < post_d_start < post_d_end < first_15m_start
    assert last_15m_persist < first_60m_start

    for event_name, expected_count in (("15m", 50), ("60m", 10)):
        assert {
            record["count"] for record in provider if record["event"] == event_name
        } == {expected_count}


@pytest.mark.asyncio
async def test_non_trading_day_and_backfill_never_create_pool(monkeypatch) -> None:
    service = BarsSchedulerService(fetch_processes=3)
    create_pool = Mock(side_effect=AssertionError("pool must not be created"))
    monkeypatch.setattr(service, "_create_process_pool", create_pool)
    monkeypatch.setattr(
        scheduler_module, "is_trading_day_async", AsyncMock(return_value=False)
    )
    result = await service.refresh_all_instruments(date(2026, 8, 29), db_session=object())
    assert result.skip_reason == "NON_TRADING_DAY"
    create_pool.assert_not_called()

    monkeypatch.setattr(
        service, "_get_active_instruments", AsyncMock(return_value=_instruments(2))
    )
    serial_period = AsyncMock(return_value=(2, 0))
    monkeypatch.setattr(service, "_run_serial_period", serial_period)
    await service.backfill_all_instruments(db_session=object())
    create_pool.assert_not_called()
    assert serial_period.await_count == 3

    monkeypatch.setattr(
        scheduler_module, "is_trading_day_async", AsyncMock(return_value=True)
    )
    serial_service = BarsSchedulerService(fetch_processes=1)
    serial_create_pool = Mock(side_effect=AssertionError("pool must not be created"))
    monkeypatch.setattr(serial_service, "_create_process_pool", serial_create_pool)
    monkeypatch.setattr(
        serial_service,
        "_get_active_instruments",
        AsyncMock(return_value=_instruments(2)),
    )
    daily_serial_period = AsyncMock(return_value=(2, 0))
    monkeypatch.setattr(serial_service, "_run_serial_period", daily_serial_period)
    monkeypatch.setattr(
        serial_service, "_run_post_daily_phase", AsyncMock(return_value=None)
    )
    await serial_service.refresh_all_instruments(
        date(2026, 8, 28), db_session=object()
    )
    serial_create_pool.assert_not_called()
    assert daily_serial_period.await_count == 3


def _raw_result(request: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "period": request["period"],
        "instrument_id": request["instrument_id"],
        "symbol": request["symbol"],
        "pid": 4242,
        "raw_df": pd.DataFrame(),
        "provider_elapsed_seconds": 0.0,
        "provider_calls": [],
    }
    if request["period"] == "d":
        result.update(
            xdxr_df=None,
            xdxr_status="success_empty",
            xdxr_error=None,
            supplement_df=None,
            supplement_error=None,
        )
    return result


class _ImmediatePool:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []
        self.shutdown_calls = 0

    def submit(self, _fn, request):
        future: Future[dict[str, Any]] = Future()
        self.calls.append(request)
        try:
            value = self.handler(request, len(self.calls))
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(value)
        return future

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        assert wait is True and cancel_futures is True
        self.shutdown_calls += 1


@pytest.mark.asyncio
async def test_retry_is_per_item_nonblocking_and_bounded(monkeypatch) -> None:
    service = BarsSchedulerService(fetch_processes=2)
    service.RETRY_DELAY = 0
    items = [
        _InstrumentItem(index, uuid.uuid4(), f"{991000 + index:06d}")
        for index in range(20)
    ]
    attempts: dict[str, int] = {}

    def handler(request, _call_no):
        symbol = request["symbol"]
        attempts[symbol] = attempts.get(symbol, 0) + 1
        if symbol == items[0].symbol and attempts[symbol] == 1:
            raise RuntimeError("first provider failure")
        return _raw_result(request)

    persisted: list[str] = []

    async def persist(item, _raw_result, _db_session):
        persisted.append(item.symbol)
        return 1

    monkeypatch.setattr(service, "_persist_provider_result", persist)
    phase = await service._run_parallel_period(
        cast(Any, _ImmediatePool(handler)),
        items,
        period="15m",
        count=50,
        db_session=object(),
        start_date=None,
    )
    assert phase.succeeded == 20
    assert phase.failed == 0
    assert phase.max_inflight_observed <= 4
    assert attempts[items[0].symbol] == 2
    assert persisted.index(items[1].symbol) < persisted.index(items[0].symbol)


@pytest.mark.asyncio
async def test_retry_exhaustion_and_persistence_retry(monkeypatch) -> None:
    service = BarsSchedulerService(fetch_processes=2)
    service.RETRY_DELAY = 0
    items = [_InstrumentItem(0, uuid.uuid4(), "992001")]
    failing_pool = _ImmediatePool(
        lambda _request, _call_no: (_ for _ in ()).throw(RuntimeError("provider down"))
    )
    monkeypatch.setattr(
        service, "_persist_provider_result", AsyncMock(return_value=1)
    )
    failed = await service._run_parallel_period(
        cast(Any, failing_pool),
        items,
        period="15m",
        count=50,
        db_session=object(),
        start_date=None,
    )
    assert failed.failed == 1
    assert failed.failed_symbols == ["992001"]
    assert len(failing_pool.calls) == service.MAX_RETRIES

    persistence_attempts = 0

    async def persist(*_args):
        nonlocal persistence_attempts
        persistence_attempts += 1
        if persistence_attempts == 1:
            raise RuntimeError("db transient")
        return 1

    monkeypatch.setattr(service, "_persist_provider_result", persist)
    recovered = await service._run_parallel_period(
        cast(Any, _ImmediatePool(lambda request, _call_no: _raw_result(request))),
        items,
        period="60m",
        count=10,
        db_session=object(),
        start_date=None,
    )
    assert recovered.succeeded == 1
    assert recovered.failed == 0
    assert persistence_attempts == 2


@pytest.mark.asyncio
async def test_scheduler_batch_result_is_stable_after_item_retries(monkeypatch) -> None:
    service = BarsSchedulerService(fetch_processes=2)
    service.RETRY_DELAY = 0
    instruments = _instruments(3)
    failed_symbol = instruments[1].symbol

    def handler(request, _call_no):
        if request["symbol"] == failed_symbol:
            raise RuntimeError("instrument provider failure")
        return _raw_result(request)

    pool = _ImmediatePool(handler)
    monkeypatch.setattr(service, "_create_process_pool", lambda: pool)
    monkeypatch.setattr(
        scheduler_module, "is_trading_day_async", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        service, "_get_active_instruments", AsyncMock(return_value=instruments)
    )
    monkeypatch.setattr(
        service, "_run_post_daily_phase", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        service, "_persist_provider_result", AsyncMock(return_value=1)
    )
    result = await service.refresh_all_instruments(
        date(2026, 8, 28), db_session=object(), trigger_dsa=False
    )
    assert result.total == 3
    assert result.succeeded == 2
    assert result.failed == 1
    assert result.failed_symbols == [failed_symbol]
    assert result.period_counts == {"d": 2, "15m": 2, "60m": 2}
    assert pool.shutdown_calls == 1


@pytest.mark.asyncio
async def test_broken_pool_fails_closed(monkeypatch) -> None:
    service = BarsSchedulerService(fetch_processes=2)
    item = _InstrumentItem(0, uuid.uuid4(), "993001")
    pool = _ImmediatePool(
        lambda _request, _call_no: (_ for _ in ()).throw(BrokenProcessPool("boom"))
    )
    persist = AsyncMock(return_value=1)
    monkeypatch.setattr(service, "_persist_provider_result", persist)
    with pytest.raises(PoolFatalError, match="process pool failed"):
        await service._run_parallel_period(
            cast(Any, pool),
            [item],
            period="d",
            count=5,
            db_session=object(),
            start_date=None,
        )
    persist.assert_not_awaited()
    assert len(pool.calls) == 1


@pytest.mark.asyncio
async def test_malformed_child_payload_fails_closed(monkeypatch) -> None:
    service = BarsSchedulerService(fetch_processes=2)
    item = _InstrumentItem(0, uuid.uuid4(), "993002")
    malformed = _ImmediatePool(lambda _request, _call_no: {"pid": "bad"})
    persist = AsyncMock(return_value=1)
    monkeypatch.setattr(service, "_persist_provider_result", persist)
    with pytest.raises(PoolFatalError, match="payload PID"):
        await service._run_parallel_period(
            cast(Any, malformed),
            [item],
            period="d",
            count=5,
            db_session=object(),
            start_date=None,
        )
    persist.assert_not_awaited()
    assert len(malformed.calls) == 1


@pytest.mark.asyncio
async def test_real_child_abrupt_exit_fails_scheduler_phase(monkeypatch, tmp_path) -> None:
    trace_dir = tmp_path / "fatal-trace"
    instruments = _instruments(6)
    service = BarsSchedulerService(
        fetch_processes=2,
        adapter_spec=_adapter_spec(
            trace_dir,
            latency_seconds=0.03,
            abrupt_exit_symbol=instruments[0].symbol,
        ),
    )
    await _configure_scheduler(monkeypatch, service, instruments, [], [])
    with pytest.raises(PoolFatalError, match="process pool failed"):
        await service.refresh_all_instruments(
            date(2026, 8, 28), db_session=object(), trigger_dsa=False
        )
    child_pids = {record["pid"] for record in _read_traces(trace_dir)}
    active_pids = {child.pid for child in multiprocessing.active_children()}
    assert child_pids.isdisjoint(active_pids)


@pytest.mark.asyncio
async def test_real_scheduler_cancellation_cleans_children(monkeypatch, tmp_path) -> None:
    trace_dir = tmp_path / "cancel-trace"
    service = BarsSchedulerService(
        fetch_processes=2,
        adapter_spec=_adapter_spec(trace_dir, latency_seconds=0.15),
    )
    instruments = _instruments(12)
    persisted: list[float] = []
    await _configure_scheduler(monkeypatch, service, instruments, [], [])

    async def persist(*_args):
        persisted.append(time.time())
        return 1

    monkeypatch.setattr(service, "_persist_provider_result", persist)
    task = asyncio.create_task(
        service.refresh_all_instruments(
            date(2026, 8, 28), db_session=object(), trigger_dsa=False
        )
    )
    deadline = time.monotonic() + 5
    while not list(trace_dir.glob("*.jsonl")) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    persisted_after_cancel = len(persisted)
    await asyncio.sleep(0.1)
    assert len(persisted) == persisted_after_cancel
    child_pids = {record["pid"] for record in _read_traces(trace_dir)}
    active_pids = {child.pid for child in multiprocessing.active_children()}
    assert child_pids.isdisjoint(active_pids)


def test_process_count_config_default_env_and_bounds(monkeypatch) -> None:
    monkeypatch.delenv("PANJI_BARS_FETCH_PROCESSES", raising=False)
    assert Settings().bars_fetch_processes == 1
    monkeypatch.setenv("PANJI_BARS_FETCH_PROCESSES", "3")
    assert Settings().bars_fetch_processes == 3
    for invalid in ("0", "9", "not-an-int"):
        monkeypatch.setenv("PANJI_BARS_FETCH_PROCESSES", invalid)
        with pytest.raises(ValidationError):
            Settings()


@pytest.mark.asyncio
async def test_serial_parallel_request_builder_equivalence(monkeypatch) -> None:
    fixed_end = date(2026, 8, 28)
    monkeypatch.setattr(scheduler_module, "shanghai_business_date", lambda: fixed_end)
    service = BarsSchedulerService(fetch_processes=1)
    item = _InstrumentItem(0, uuid.uuid4(), "994001")
    expected_daily = service._build_provider_request(
        item, period="d", count=5, start_date=None
    )
    expected_15m = service._build_provider_request(
        item, period="15m", count=50, start_date=None
    )
    assert expected_daily["symbol"] == expected_15m["symbol"] == item.symbol
    calls: list[tuple[Any, ...]] = []

    async def daily(_session, instrument_id, start, end, _adapter):
        calls.append(("d", str(instrument_id), start, end))
        return pd.DataFrame()

    async def minute(_session, instrument_id, count, _adapter):
        calls.append(("15m", str(instrument_id), count))
        return pd.DataFrame()

    monkeypatch.setitem(service._REFRESH_FUNCS, "d", daily)
    monkeypatch.setitem(service._REFRESH_FUNCS, "15m", minute)
    monkeypatch.setattr(scheduler_module, "get_pytdx_adapter", lambda: object())
    await service._refresh_one_period_with_retry(
        item.instrument_id, item.symbol, "d", 5, db_session=object()
    )
    await service._refresh_one_period_with_retry(
        item.instrument_id, item.symbol, "15m", 50, db_session=object()
    )
    assert calls == [
        (
            "d",
            expected_daily["instrument_id"],
            expected_daily["start_date"],
            expected_daily["end_date"],
        ),
        ("15m", expected_15m["instrument_id"], expected_15m["count"]),
    ]
