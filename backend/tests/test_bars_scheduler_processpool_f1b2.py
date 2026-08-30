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
from datetime import date, timedelta
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
    InstrumentRefreshExhaustedError,
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
    # [F1B-2 P2] 60m 补齐：daily refresh 用 start/end，60m 用 count=10
    expected_60m = service._build_provider_request(
        item, period="60m", count=10, start_date=None
    )
    assert expected_daily["symbol"] == expected_15m["symbol"] == expected_60m["symbol"] == item.symbol
    # d: start/end 由 count(回看天数) 推导；15m/60m: 原样透传 count
    assert expected_daily["count"] == 5
    assert (expected_daily["start_date"], expected_daily["end_date"]) == (
        fixed_end - timedelta(days=5),
        fixed_end,
    )
    assert expected_15m["count"] == 50
    assert expected_60m["count"] == 10
    calls: list[tuple[Any, ...]] = []

    async def daily(_session, instrument_id, start, end, _adapter):
        calls.append(("d", str(instrument_id), start, end))
        return pd.DataFrame()

    async def minute(_session, instrument_id, count, _adapter):
        calls.append(("15m", str(instrument_id), count))
        return pd.DataFrame()

    async def minute60(_session, instrument_id, count, _adapter):
        calls.append(("60m", str(instrument_id), count))
        return pd.DataFrame()

    monkeypatch.setitem(service._REFRESH_FUNCS, "d", daily)
    monkeypatch.setitem(service._REFRESH_FUNCS, "15m", minute)
    monkeypatch.setitem(service._REFRESH_FUNCS, "60m", minute60)
    monkeypatch.setattr(scheduler_module, "get_pytdx_adapter", lambda: object())
    await service._refresh_one_period_with_retry(
        item.instrument_id, item.symbol, "d", 5, db_session=object()
    )
    await service._refresh_one_period_with_retry(
        item.instrument_id, item.symbol, "15m", 50, db_session=object()
    )
    await service._refresh_one_period_with_retry(
        item.instrument_id, item.symbol, "60m", 10, db_session=object()
    )
    assert calls == [
        (
            "d",
            expected_daily["instrument_id"],
            expected_daily["start_date"],
            expected_daily["end_date"],
        ),
        ("15m", expected_15m["instrument_id"], expected_15m["count"]),
        ("60m", expected_60m["instrument_id"], expected_60m["count"]),
    ]


# ---------------------------------------------------------------------------
# [F1B-2 P1-A] serial / parallel 失败语义等价机器测试
# ---------------------------------------------------------------------------

# 场景策略（同一份 policy 同时驱动 workers=1 与 workers=2）：
#   990000  正常成功
#   990001  A. 永久 provider 失败 → 必须计 failed
#   990002  B. 首次失败、重试成功 → 必须计 succeeded
#   990003  C. 合法空 provider 结果 → 成功且 0 行（不得被当成失败）
_FAILURE_POLICY: dict[str, list[str]] = {
    "fail_symbols": ["990001"],
    "transient_fail_symbols": ["990002"],
    "empty_symbols": ["990003"],
}
_FAILURE_SYMBOLS = ("990000", "990001", "990002", "990003")


def _install_serial_canonical_provider(
    monkeypatch: pytest.MonkeyPatch,
    service: BarsSchedulerService,
    provider: Any,
    symbol_by_id: dict[str, str],
) -> None:
    """让 serial 路径经**与 parallel child 完全相同**的 canonical provider boundary 驱动。

    两侧唯一差异保持在「调度/重试/计数」本身，provider 语义逐字节一致，
    因此 BatchResult 的差异只可能来自 serial/parallel 的记账逻辑。
    """
    from app.services.bars_fetch_worker import (
        fetch_daily_provider_inputs,
        fetch_minute_provider_inputs,
    )

    async def daily(_session, instrument_id, start_date, end_date, _adapter=None):
        symbol = symbol_by_id[str(instrument_id)]
        payload = fetch_daily_provider_inputs(
            str(instrument_id), symbol, start_date, end_date, provider
        )
        return payload.raw_df

    def minute(period: str):
        async def _refresh(_session, instrument_id, count, _adapter=None):
            symbol = symbol_by_id[str(instrument_id)]
            payload = fetch_minute_provider_inputs(
                str(instrument_id), symbol, period, count, provider
            )
            return payload.raw_df

        return _refresh

    monkeypatch.setitem(service._REFRESH_FUNCS, "d", daily)
    monkeypatch.setitem(service._REFRESH_FUNCS, "15m", minute("15m"))
    monkeypatch.setitem(service._REFRESH_FUNCS, "60m", minute("60m"))


async def _run_batch_with_policy(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    *,
    workers: int,
    instruments: list[SimpleNamespace],
    fixed_end: date,
) -> Any:
    """以同一份 failure policy 跑一轮 refresh_all_instruments，返回完整 BatchResult。"""
    from tests.support.bars_fake_provider import FakeBarsProvider

    symbol_by_id = {str(item.id): item.symbol for item in instruments}
    provider_kwargs = {
        **_FAILURE_POLICY,
        # 每次运行独立 attempt 计数器：parallel 重试可能落到另一个 child，
        # 计数必须跨进程共享（同一次运行内），但两次运行之间必须互不影响。
        "attempt_dir": str(run_dir / "attempts"),
    }
    if workers > 1:
        service = BarsSchedulerService(
            fetch_processes=workers,
            adapter_spec=_adapter_spec(run_dir / "trace", **provider_kwargs),
        )
    else:
        service = BarsSchedulerService(fetch_processes=1)

    monkeypatch.setattr(service, "RETRY_DELAY", 0)
    monkeypatch.setattr(
        service, "_get_active_instruments", AsyncMock(return_value=instruments)
    )

    async def post_daily(*_args, **_kwargs):
        return None

    async def persist(item, raw_result, _db_session):
        return len(raw_result["raw_df"])

    monkeypatch.setattr(service, "_run_post_daily_phase", post_daily)
    monkeypatch.setattr(service, "_persist_provider_result", persist)

    if workers == 1:
        _install_serial_canonical_provider(
            monkeypatch, service, FakeBarsProvider(**provider_kwargs), symbol_by_id
        )

    result = await service.refresh_all_instruments(
        fixed_end, db_session=object(), trigger_dsa=False
    )

    # 反 false-green：确认本轮真的走了预期执行路径。
    # 若 parallel 静默回退到 serial（或反之），下面的等价断言将失去意义。
    pool_creations = service.last_process_metrics.get("pool_creations", 0)
    if workers > 1:
        assert pool_creations == 1, (
            f"workers={workers} 未创建 ProcessPool（pool_creations={pool_creations}），"
            "等价断言无意义"
        )
    else:
        assert pool_creations == 0, (
            f"workers=1 不应创建 ProcessPool（pool_creations={pool_creations}）"
        )
    return result


def _attempt_count(run_dir: Path, symbol: str, method: str) -> int:
    """读取共享 attempt 计数器（跨 child 进程累加），用于证明重试真实发生。"""
    path = run_dir / "attempts" / f"attempt_{symbol}_{method}.count"
    return len(path.read_text(encoding="utf-8")) if path.exists() else 0


@pytest.mark.asyncio
async def test_serial_parallel_failure_semantics_equivalence(monkeypatch, tmp_path) -> None:
    """[F1B-2 P1-A] workers=1 与 workers=2 的完整 BatchResult 必须逐字段等价。

    覆盖：
      A. 单标的永久 provider 失败      → failed
      B. 首次失败、重试成功            → succeeded
      C. 合法空 provider 结果           → succeeded + 0 行（禁止 upsert_count==0 推断失败）
    """
    fixed_end = date(2026, 8, 28)
    monkeypatch.setattr(scheduler_module, "shanghai_business_date", lambda: fixed_end)
    monkeypatch.setattr(
        scheduler_module, "is_trading_day_async", AsyncMock(return_value=True)
    )
    instruments = [
        SimpleNamespace(id=uuid.uuid4(), symbol=symbol)
        for symbol in _FAILURE_SYMBOLS
    ]

    serial = await _run_batch_with_policy(
        monkeypatch, tmp_path / "serial", workers=1,
        instruments=instruments, fixed_end=fixed_end,
    )
    parallel = await _run_batch_with_policy(
        monkeypatch, tmp_path / "parallel", workers=2,
        instruments=instruments, fixed_end=fixed_end,
    )

    # 1) 逐字段等价（核心断言）
    for field in ("total", "succeeded", "failed", "failed_symbols", "period_counts"):
        assert getattr(serial, field) == getattr(parallel, field), (
            f"BatchResult.{field} 不等价: serial={getattr(serial, field)} "
            f"parallel={getattr(parallel, field)}"
        )

    # 2) 显式语义断言（防止两边一起错 → false-green）
    assert serial.total == 4
    assert serial.failed == 1
    assert serial.succeeded == 3
    # 990001 永久失败；990002 重试后成功不得进 failed_symbols
    assert serial.failed_symbols == ["990001"]
    # 990003 是合法空结果：成功但 0 行
    assert serial.period_counts == {"d": 10, "15m": 100, "60m": 20}

    # 3) 重试真实发生（不是"根本没调用 provider"造成的假等价）
    max_retries = BarsSchedulerService.MAX_RETRIES
    for run_dir in (tmp_path / "serial", tmp_path / "parallel"):
        # 990001：连续 MAX_RETRIES 次失败后放弃
        assert _attempt_count(run_dir, "990001", "get_daily_bars") == max_retries
        assert _attempt_count(run_dir, "990001", "get_15min_bars") == max_retries
        assert _attempt_count(run_dir, "990001", "get_60min_bars") == max_retries
        # 990002：首次失败 + 重试成功 = 2 次
        assert _attempt_count(run_dir, "990002", "get_daily_bars") == 2
        # 990003 / 990000：一次成功，无重试
        assert _attempt_count(run_dir, "990003", "get_daily_bars") == 1
        assert _attempt_count(run_dir, "990000", "get_daily_bars") == 1


@pytest.mark.asyncio
async def test_serial_retry_exhausted_is_explicit_failure(monkeypatch, tmp_path) -> None:
    """[F1B-2 P1-A] serial 路径重试耗尽必须抛显式信号，不得静默返回 0。"""
    from tests.support.bars_fake_provider import FakeBarsProvider

    fixed_end = date(2026, 8, 28)
    monkeypatch.setattr(scheduler_module, "shanghai_business_date", lambda: fixed_end)
    service = BarsSchedulerService(fetch_processes=1)
    monkeypatch.setattr(service, "RETRY_DELAY", 0)

    instrument_id = uuid.uuid4()
    symbol_by_id = {str(instrument_id): "990001"}
    _install_serial_canonical_provider(
        monkeypatch,
        service,
        FakeBarsProvider(fail_symbols=["990001"]),
        symbol_by_id,
    )

    with pytest.raises(InstrumentRefreshExhaustedError):
        await service._refresh_one_period_with_retry(
            instrument_id, "990001", "d", 5, db_session=object()
        )


@pytest.mark.asyncio
async def test_empty_provider_result_is_success_not_failure(monkeypatch) -> None:
    """[F1B-2 §5] 合法空 provider 返回 = 成功执行 + 0 行，不得计 failed。"""
    from tests.support.bars_fake_provider import FakeBarsProvider

    fixed_end = date(2026, 8, 28)
    monkeypatch.setattr(scheduler_module, "shanghai_business_date", lambda: fixed_end)
    service = BarsSchedulerService(fetch_processes=1)
    instrument_id = uuid.uuid4()
    _install_serial_canonical_provider(
        monkeypatch,
        service,
        FakeBarsProvider(empty_symbols=["990003"]),
        {str(instrument_id): "990003"},
    )

    upsert_count = await service._refresh_one_period_with_retry(
        instrument_id, "990003", "d", 5, db_session=object()
    )
    assert upsert_count == 0  # 0 行是**成功**，不是失败信号
