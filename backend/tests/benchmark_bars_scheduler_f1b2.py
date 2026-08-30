"""Run the F1B-2 synthetic scheduler benchmark for workers 1, 2, and 3."""
from __future__ import annotations

import asyncio
import json
import resource
import sys
import tempfile
import time
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.repositories.bar_repository import prepare_daily_bars
from app.services import bars_scheduler_service as scheduler_module
from app.services.bars_fetch_worker import fetch_period_provider_inputs
from app.services.bars_scheduler_service import BarsSchedulerService
from tests.support.bars_fake_provider import XDXR_ROWS, FakeBarsProvider

INSTRUMENT_COUNT = 24
LATENCY_SECONDS = 0.03


def _rss_kb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / 1024 if sys.platform == "darwin" else float(value)


async def _serial_daily(_session, _instrument_id, start, end, adapter):
    payload = fetch_period_provider_inputs(
        "d", str(_instrument_id), "unused", start_date=start, end_date=end, adapter=adapter
    )
    return prepare_daily_bars(payload)


async def _serial_15m(_session, instrument_id, count, adapter):
    payload = fetch_period_provider_inputs(
        "15m", str(instrument_id), "unused", count=count, adapter=adapter
    )
    return payload.raw_df


async def _serial_60m(_session, instrument_id, count, adapter):
    payload = fetch_period_provider_inputs(
        "60m", str(instrument_id), "unused", count=count, adapter=adapter
    )
    return payload.raw_df


async def _run(workers: int, root: Path) -> dict[str, Any]:
    trace_dir = root / f"workers-{workers}"
    provider_kwargs = {
        "xdxr_mode": XDXR_ROWS,
        "xdxr_event_dates": ["2026-08-01"],
        "latency_seconds": LATENCY_SECONDS,
        "trace_dir": str(trace_dir),
    }
    adapter_spec = {
        "module": "tests.support.bars_fake_provider",
        "attr": "build_fake_provider",
        "kwargs": provider_kwargs,
    }
    service = BarsSchedulerService(
        fetch_processes=workers,
        adapter_spec=adapter_spec if workers > 1 else None,
    )
    instruments = [
        SimpleNamespace(id=uuid.uuid4(), symbol=f"{980000 + index:06d}")
        for index in range(INSTRUMENT_COUNT)
    ]

    async def get_instruments(_db_session):
        return instruments

    async def no_post_d(*_args, **_kwargs):
        return None

    async def persist(_item, raw_result, _db_session):
        return len(raw_result["raw_df"])

    service._get_active_instruments = get_instruments  # type: ignore[method-assign]
    service._run_post_daily_phase = no_post_d  # type: ignore[method-assign]
    service._persist_provider_result = persist  # type: ignore[method-assign]

    old_trading = scheduler_module.is_trading_day_async
    old_adapter = scheduler_module.get_pytdx_adapter
    old_refresh = dict(service._REFRESH_FUNCS)

    async def trading_day(_session, _trade_date):
        return True

    scheduler_module.is_trading_day_async = trading_day
    scheduler_module.get_pytdx_adapter = lambda: FakeBarsProvider(**provider_kwargs)
    service._REFRESH_FUNCS.update(
        {"d": _serial_daily, "15m": _serial_15m, "60m": _serial_60m}
    )
    rss_before = _rss_kb()
    started = time.perf_counter()
    try:
        result = await service.refresh_all_instruments(
            date(2026, 8, 28), db_session=object(), trigger_dsa=False
        )
    finally:
        scheduler_module.is_trading_day_async = old_trading
        scheduler_module.get_pytdx_adapter = old_adapter
        service._REFRESH_FUNCS.clear()
        service._REFRESH_FUNCS.update(old_refresh)
    elapsed = time.perf_counter() - started
    rss_after = _rss_kb()

    pids: set[int] = set()
    for path in trace_dir.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            pids.add(int(json.loads(line)["pid"]))
    periods = service.last_process_metrics["periods"]
    child_rss = max(
        (float(metrics.get("child_max_rss_kb", 0.0)) for metrics in periods.values()),
        default=0.0,
    )
    retries = sum(
        max(0, int(metrics["submitted"]) - INSTRUMENT_COUNT)
        for metrics in periods.values()
    )
    item_count = INSTRUMENT_COUNT * 3
    return {
        "workers": workers,
        "wall_seconds": elapsed,
        "items_per_second": item_count / elapsed,
        "distinct_pids": sorted(pids),
        "max_inflight": service.last_process_metrics["max_inflight_observed"],
        "parent_max_rss_kb": max(rss_before, rss_after),
        "child_max_rss_kb": child_rss,
        "retry_count": retries,
        "error_count": result.failed,
    }


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="panji-f1b2-benchmark-") as temp_dir:
        root = Path(temp_dir)
        rows = [await _run(workers, root) for workers in (1, 2, 3)]
    baseline = rows[0]["wall_seconds"]
    for row in rows:
        row["speedup"] = baseline / row["wall_seconds"]
    print(json.dumps(rows, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
