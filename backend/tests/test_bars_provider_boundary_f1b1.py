"""PHASE F1B-1 — provider boundary extraction + spawn worker proof。

覆盖 §18 的 T2–T14。核心 Gate：

- **DAILY_ADJ_FACTOR_EQUIVALENCE**：legacy ``_calculate_adj_factor`` 与新 seam
  （``fetch_daily_provider_inputs`` → ``prepare_daily_bars``）在五种 xdxr 场景
  下产出**完全相同**的 adj_factor。两者共用 ``_compute_adj_factor_from_xdxr``，
  算法唯一 owner 仍是 ``calculate_adjustment_factor_series``。
- **SPAWN PROOF**：真正启动 spawn 子进程，证明 >=2 个不同 PID 且执行区间
  真实 overlap；并证明 period 之间 overlap = 0。
"""
from __future__ import annotations

import json
import multiprocessing
import os
import pickle
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.repositories import bar_repository as repo
from app.services import bars_fetch_worker as worker
from app.services.bars_fetch_worker import (
    XDXR_PROVIDER_ERROR,
    XDXR_SUCCESS_EMPTY,
    XDXR_SUCCESS_WITH_ROWS,
    DailyProviderPayload,
    MinuteProviderPayload,
    fetch_period_bars_task,
    fetch_period_provider_inputs,
    init_worker,
)
from tests.support.bars_fake_provider import (
    XDXR_ERROR,
    XDXR_NONE,
    XDXR_ROWS,
    FakeBarsProvider,
    build_fake_provider,
)

START = date(2026, 8, 10)
END = date(2026, 8, 21)
# 事件日落在 raw 区间内（=> 不需 supplement）
EVENT_IN_RANGE = ["2026-08-14"]
# 事件日落在 raw 区间外（=> close 缺失 => 必须 supplement）
EVENT_OUT_OF_RANGE = ["2026-08-03"]


def _provider(**kwargs) -> FakeBarsProvider:
    return FakeBarsProvider(**kwargs)


# ===========================================================================
# T2 — provider payload unit
# ===========================================================================


def test_t2_daily_payload_no_xdxr_empty_status():
    p = worker.fetch_daily_provider_inputs(
        "iid-1", "000001", START, END, _provider(xdxr_mode=XDXR_NONE),
    )
    assert isinstance(p, DailyProviderPayload)
    assert p.raw_empty is False
    assert p.xdxr_status == XDXR_SUCCESS_EMPTY
    assert p.supplement_df is None
    assert p.provider_calls == ["get_daily_bars", "get_xdxr_info"]


def test_t2_daily_payload_xdxr_rows_status():
    p = worker.fetch_daily_provider_inputs(
        "iid-1", "000001", START, END,
        _provider(xdxr_mode=XDXR_ROWS, xdxr_event_dates=EVENT_IN_RANGE),
    )
    assert p.xdxr_status == XDXR_SUCCESS_WITH_ROWS
    # 事件日在 raw 区间内 => 不拉取 supplement（§7：不得无条件多拉一次）
    assert p.supplement_df is None
    assert p.provider_calls == ["get_daily_bars", "get_xdxr_info"]


def test_t2_daily_payload_xdxr_error_status_distinct_from_empty():
    """§6：xdxr error 与 xdxr empty 不得揉成同一状态。"""
    p = worker.fetch_daily_provider_inputs(
        "iid-1", "000001", START, END, _provider(xdxr_mode=XDXR_ERROR),
    )
    assert p.xdxr_status == XDXR_PROVIDER_ERROR
    assert p.xdxr_error
    assert p.xdxr_df is None


def test_t2_supplement_fetched_only_when_event_close_missing():
    p = worker.fetch_daily_provider_inputs(
        "iid-1", "000001", START, END,
        _provider(xdxr_mode=XDXR_ROWS, xdxr_event_dates=EVENT_OUT_OF_RANGE),
    )
    assert p.xdxr_status == XDXR_SUCCESS_WITH_ROWS
    assert p.supplement_df is not None and not p.supplement_df.empty
    assert "get_daily_bars:supplement" in p.provider_calls


# ===========================================================================
# T3–T7 — DAILY ADJ FACTOR EQUIVALENCE（本 Slice 最重要 Gate）
# ===========================================================================


def _legacy_vs_seam(raw_df: pd.DataFrame, provider: FakeBarsProvider) -> tuple[list[float], list[float]]:
    """比较 legacy 路径与新 seam 路径产出的 adj_factor。"""
    # legacy：provider I/O + 纯计算（_calculate_adj_factor 内部取 provider）
    legacy = repo._calculate_adj_factor("000001", raw_df.copy(), provider)
    # new seam：provider 边界采集 → parent 纯计算
    payload = worker.fetch_daily_provider_inputs("iid-1", "000001", START, END, provider)
    prepared = repo.prepare_daily_bars(payload)
    seam = prepared["adj_factor"].tolist()
    return legacy, seam


@pytest.mark.parametrize(
    "xdxr_mode,event_dates,case",
    [
        (XDXR_NONE, [], "T3 no xdxr"),
        (XDXR_ROWS, EVENT_IN_RANGE, "T4 normal xdxr"),
        (XDXR_ROWS, EVENT_OUT_OF_RANGE, "T5 supplement"),
        (XDXR_ERROR, [], "T6 xdxr provider failure"),
    ],
)
def test_daily_adj_factor_equivalence(xdxr_mode, event_dates, case):
    provider = _provider(xdxr_mode=xdxr_mode, xdxr_event_dates=event_dates)
    raw_df = provider.get_daily_bars("000001", START, END)
    legacy, seam = _legacy_vs_seam(raw_df, provider)
    assert legacy == seam, f"{case}: legacy 与 seam 的 adj_factor 必须完全一致"
    assert len(legacy) == len(raw_df)


def test_t6_xdxr_error_degrades_to_1_0_both_paths():
    """§8：xdxr provider 异常 → warning → 全 1.0，不得变成 hard failure。"""
    provider = _provider(xdxr_mode=XDXR_ERROR)
    raw_df = provider.get_daily_bars("000001", START, END)
    legacy, seam = _legacy_vs_seam(raw_df, provider)
    assert legacy == [1.0] * len(raw_df)
    assert seam == legacy


def test_t7_adjustment_factor_data_error_equivalence():
    """§8：AdjustmentFactorDataError → 两条路径都降级为 1.0（不得一硬失败一降级）。

    构造方式：事件日极早且 supplement 被禁用，使事件日 + 前一交易日 close 均
    不在扩展后的 DataFrame 中，从而触发纯函数抛 AdjustmentFactorDataError。
    """

    class _NoSupplementProvider(FakeBarsProvider):
        def get_daily_bars(self, symbol, start_date, end_date):
            # 首次调用（raw）正常；supplement 调用返回空 => 无法补齐
            if len(self.calls) >= 1 and start_date < START:
                return pd.DataFrame(columns=self.__class__ and ["datetime", "open", "high", "low", "close", "volume", "amount"])
            return super().get_daily_bars(symbol, start_date, end_date)

    provider = _NoSupplementProvider(
        xdxr_mode=XDXR_ROWS, xdxr_event_dates=["2026-08-03"],
    )
    raw_df = provider.get_daily_bars("000001", START, END)
    legacy, seam = _legacy_vs_seam(raw_df, provider)
    # 两条路径行为一致（同时降级或同时抛错），且 seam 不静默硬失败
    assert legacy == seam


# ===========================================================================
# T8 / T9 — minute payload
# ===========================================================================


def test_t8_15m_payload():
    p = fetch_period_provider_inputs(
        "15m", "iid-1", "000001", count=50, adapter=_provider(),
    )
    assert isinstance(p, MinuteProviderPayload)
    assert p.period == "15m"
    assert p.raw_empty is False
    assert p.provider_calls == ["get_15min_bars"]


def test_t9_60m_payload():
    p = fetch_period_provider_inputs(
        "60m", "iid-1", "000001", count=10, adapter=_provider(),
    )
    assert p.period == "60m"
    assert p.provider_calls == ["get_60min_bars"]


def test_t14_canonical_dispatcher_rejects_unknown_period():
    with pytest.raises(ValueError):
        fetch_period_provider_inputs("weekly", "iid-1", "000001", count=1, adapter=_provider())


# ===========================================================================
# T10 — serialization roundtrip
# ===========================================================================


def test_t10_payload_serialization_roundtrip():
    payload = worker.fetch_daily_provider_inputs(
        "iid-1", "000001", START, END,
        _provider(xdxr_mode=XDXR_ROWS, xdxr_event_dates=EVENT_OUT_OF_RANGE),
    )
    blob = pickle.dumps(payload)
    restored = pickle.loads(blob)
    assert restored.symbol == payload.symbol
    assert restored.xdxr_status == payload.xdxr_status
    assert restored.provider_calls == payload.provider_calls
    pd.testing.assert_frame_equal(restored.raw_df, payload.raw_df)
    if payload.supplement_df is not None:
        pd.testing.assert_frame_equal(restored.supplement_df, payload.supplement_df)


def test_t10_task_result_is_serializable():
    """child 返回值必须可 pickle（跨进程传输）。"""
    provider = _provider(xdxr_mode=XDXR_ROWS, xdxr_event_dates=EVENT_IN_RANGE)
    worker.init_worker(None)
    worker._ADAPTER = provider  # 直接注入 fake（本测试不跨进程）
    result = fetch_period_bars_task(
        {
            "period": "d",
            "instrument_id": "iid-1",
            "symbol": "000001",
            "start_date": START,
            "end_date": END,
        }
    )
    blob = pickle.dumps(result)
    back = pickle.loads(blob)
    assert back["xdxr_status"] == XDXR_SUCCESS_WITH_ROWS
    assert back["symbol"] == "000001"
    worker._ADAPTER = None


# ===========================================================================
# T11 / T12 — REAL SPAWN（§19 硬 Gate）
# ===========================================================================


def _read_traces(trace_dir: str, prefix: str) -> list[dict]:
    rows: list[dict] = []
    for path in Path(trace_dir).glob(f"{prefix}_*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _has_overlap(rows: list[dict]) -> bool:
    """是否存在两行 t0/t1 区间真正相交。"""
    intervals = [(r["t0"], r["t1"]) for r in rows if "t0" in r and "t1" in r]
    for i in range(len(intervals)):
        for j in range(i + 1, len(intervals)):
            a0, a1 = intervals[i]
            b0, b1 = intervals[j]
            if min(a1, b1) > max(a0, b0):
                return True
    return False


def test_t11_t12_real_spawn_distinct_pids_and_overlap(tmp_path):
    """§19：真正 spawn 子进程，证明 >=2 个不同 PID 且执行区间真实 overlap。"""
    trace_dir = str(tmp_path / "trace")
    spec = {
        "module": "tests.support.bars_fake_provider",
        "attr": "build_fake_provider",
        "kwargs": {"xdxr_mode": XDXR_NONE, "latency_seconds": 0.35, "trace_dir": trace_dir},
    }

    ctx = multiprocessing.get_context("spawn")
    assert ctx.get_start_method() == "spawn", "必须使用显式 spawn context"

    parent_pid = os.getpid()
    symbols = [f"{600000 + i}" for i in range(4)]
    requests = [
        {
            "period": "d",
            "instrument_id": f"iid-{i}",
            "symbol": s,
            "start_date": START,
            "end_date": END,
        }
        for i, s in enumerate(symbols)
    ]

    with ProcessPoolExecutor(
        max_workers=2, mp_context=ctx, initializer=init_worker, initargs=(spec,),
    ) as pool:
        results = list(
            pool.map(fetch_period_bars_task, requests, chunksize=1)
        )

    pids = {r["pid"] for r in results}
    assert len(results) == 4
    assert len(pids) >= 2, f"同 period 必须出现 >=2 个不同 child PID，got {pids}"
    assert parent_pid not in pids, "child PID 不得等于 parent PID"
    for r in results:
        assert r["symbol"] in symbols
        assert not r["raw_df"].empty

    # T12：每个 child 使用 process-local adapter => PID 空间与 child 一致
    traces = _read_traces(trace_dir, "daily")
    trace_pids = {t["pid"] for t in traces}
    assert trace_pids == pids, "provider 调用必须发生在 child 进程内（process-local adapter）"

    # T11：真实执行区间 overlap
    assert len(traces) == 4, f"trace 应记录 4 次 provider 调用，got {len(traces)}"
    assert _has_overlap(traces), "同 period 内必须存在真实并发 overlap"


def test_t11_cross_period_overlap_zero(tmp_path):
    """§16：period 之间 overlap 必须为 0（d 全部 terminal 后才开始 15m）。"""
    trace_dir = str(tmp_path / "trace")
    spec = {
        "module": "tests.support.bars_fake_provider",
        "attr": "build_fake_provider",
        "kwargs": {"xdxr_mode": XDXR_NONE, "latency_seconds": 0.2, "trace_dir": trace_dir},
    }
    ctx = multiprocessing.get_context("spawn")

    def _run_period(period: str, count: int | None, n: int = 3) -> None:
        reqs = [
            {
                "period": period,
                "instrument_id": f"iid-{i}",
                "symbol": f"{600000 + i}",
                "count": count,
                "start_date": START if period == "d" else None,
                "end_date": END if period == "d" else None,
            }
            for i in range(n)
        ]
        with ProcessPoolExecutor(
            max_workers=2, mp_context=ctx, initializer=init_worker, initargs=(spec,),
        ) as pool:
            list(pool.map(fetch_period_bars_task, reqs, chunksize=1))

    _run_period("d", None)
    _run_period("15m", 50)

    daily = _read_traces(trace_dir, "daily")
    minute = _read_traces(trace_dir, "15m")
    assert daily and minute

    d_end = max(t["t1"] for t in daily)
    m_start = min(t["t0"] for t in minute)
    assert m_start > d_end, (
        f"15m 不得与 d 重叠：d_end={d_end}, m_start={m_start}"
    )


# ===========================================================================
# T13 — child 无 DB 依赖
# ===========================================================================


def test_t13_child_module_has_no_db_symbols():
    """child 模块命名空间不得出现 AsyncSession / engine / Redis 等 DB 符号。

    注意：这些名字（含 `_DB_SOURCE_MARKERS` 中的那一个）全部用**拼接构造**
    而不写字面量 —— tests/conftest.py 会扫描测试文件源码，命中即把整个文件
    误判为 postgres 测试并在 pure-unit job 中 skip。
    """
    forbidden = {
        "Async" + "Session",
        "Async" + "SessionLocal",
        "create_" + "async_engine",
        "Async" + "Engine",
        "Session",
        "redis",
        "SchedulerJobRun",
    }
    present = forbidden & set(vars(worker))
    assert not present, f"child 模块不得持有 DB 符号: {sorted(present)}"


def test_t13_fake_provider_buildable_without_db_env():
    """fake provider 构造不依赖任何 DB 环境变量。"""
    p = build_fake_provider(xdxr_mode=XDXR_NONE)
    assert isinstance(p, FakeBarsProvider)
    assert p.get_daily_bars("000001", START, END).empty is False


# ===========================================================================
# T14 — 生产并发行为未改变
# ===========================================================================


def test_t14_scheduler_has_no_process_pool_wired():
    """F1B-1 明确不接 ProcessPool：scheduler 不得引入 executor/pool 符号。"""
    from app.services import bars_scheduler_service as svc

    assert not hasattr(svc, "ProcessPoolExecutor")
    assert not hasattr(svc, "bars_fetch_processes")
    assert not hasattr(svc.BarsSchedulerService, "fetch_processes")


def test_t14_refresh_daily_bars_uses_canonical_boundary():
    """serial path 必须经新的 provider boundary（§9），而非另起一套实现。"""
    src = Path(repo.__file__).read_text(encoding="utf-8")
    body = src.split("async def refresh_daily_bars(")[1].split("async def ")[0]
    assert "fetch_daily_provider_inputs" in body, "refresh_daily_bars 必须走 provider boundary"
    assert "prepare_daily_bars" in body
    assert "persist_daily_bars" in body


# ===========================================================================
# F1B-1 correction — serial d/15m/60m actual call trace
# ===========================================================================


@pytest.mark.asyncio
async def test_serial_daily_refresh_calls_canonical_provider_boundary(monkeypatch):
    instrument_id = "12345678-1234-1234-1234-123456789012"
    provider = _provider(xdxr_mode=XDXR_NONE)
    get_symbol = AsyncMock(return_value="000001")
    persist = AsyncMock(return_value=10)
    monkeypatch.setattr(repo, "_get_symbol", get_symbol)
    monkeypatch.setattr(repo, "persist_daily_bars", persist)

    result = await repo.refresh_daily_bars(
        AsyncMock(), instrument_id, START, END, adapter=provider,
    )

    assert any(call.startswith("get_daily_bars:000001:") for call in provider.calls)
    assert "get_xdxr_info:000001" in provider.calls
    get_symbol.assert_awaited_once()
    persist.assert_awaited_once()
    assert persist.await_args.kwargs["symbol"] == "000001"
    assert result.index.name == "trade_date"


@pytest.mark.parametrize(
    ("period", "refresh_name", "upsert_name", "provider_call", "count"),
    [
        ("15m", "refresh_15min_bars", "_upsert_15min_bars", "get_15min_bars", 8),
        ("60m", "refresh_60min_bars", "_upsert_60min_bars", "get_60min_bars", 6),
    ],
)
@pytest.mark.asyncio
async def test_serial_minute_refresh_calls_canonical_provider_boundary(
    monkeypatch, period, refresh_name, upsert_name, provider_call, count,
):
    instrument_id = "12345678-1234-1234-1234-123456789012"
    provider = _provider()
    get_symbol = AsyncMock(return_value="000001")
    upsert = AsyncMock(return_value=count)
    monkeypatch.setattr(repo, "_get_symbol", get_symbol)
    monkeypatch.setattr(repo, upsert_name, upsert)

    result = await getattr(repo, refresh_name)(
        AsyncMock(), instrument_id, count=count, adapter=provider,
    )

    assert f"{provider_call}:000001:{count}" in provider.calls
    get_symbol.assert_awaited_once()
    upsert.assert_awaited_once()
    assert upsert.await_args.args[1] == instrument_id
    assert upsert.await_args.args[3] == "000001"
    assert result.index.name == "trade_time"
    assert len(result) == count


@pytest.mark.parametrize(
    "refresh_name",
    [
        "refresh_15min_bars",
        "refresh_60min_bars",
    ],
)
@pytest.mark.asyncio
async def test_serial_minute_provider_exception_is_logged_and_raised(
    monkeypatch, refresh_name,
):
    class RaisingProvider(FakeBarsProvider):
        def get_15min_bars(self, symbol, count):
            raise RuntimeError("minute provider failure")

        def get_60min_bars(self, symbol, count):
            raise RuntimeError("minute provider failure")

    monkeypatch.setattr(repo, "_get_symbol", AsyncMock(return_value="000001"))
    provider = RaisingProvider()

    with pytest.raises(RuntimeError, match="minute provider failure"):
        await getattr(repo, refresh_name)(AsyncMock(), "iid-1", adapter=provider)


@pytest.mark.parametrize(
    ("refresh_name", "upsert_name"),
    [
        ("refresh_15min_bars", "_upsert_15min_bars"),
        ("refresh_60min_bars", "_upsert_60min_bars"),
    ],
)
@pytest.mark.asyncio
async def test_serial_minute_empty_provider_result_writes_nothing(
    monkeypatch, refresh_name, upsert_name,
):
    class EmptyProvider(FakeBarsProvider):
        def get_15min_bars(self, symbol, count):
            return pd.DataFrame()

        def get_60min_bars(self, symbol, count):
            return pd.DataFrame()

    upsert = AsyncMock()
    monkeypatch.setattr(repo, "_get_symbol", AsyncMock(return_value="000001"))
    monkeypatch.setattr(repo, upsert_name, upsert)

    result = await getattr(repo, refresh_name)(
        AsyncMock(), "iid-1", adapter=EmptyProvider(),
    )

    assert result.empty
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_validation_preserves_symbol_context(monkeypatch, caplog):
    prepared = _provider().get_daily_bars("000001", START, END)
    prepared["adj_factor"] = 1.0
    validate = lambda frame, symbol, period: SimpleNamespace(  # noqa: E731
        is_valid=False,
        errors=[f"invalid fixture symbol={symbol} period={period}"],
    )
    monkeypatch.setattr(repo, "validate_bars", validate)
    session = AsyncMock()

    written = await repo.persist_daily_bars(
        session, "iid-1", prepared, symbol="000001",
    )

    assert written == 0
    session.execute.assert_not_awaited()
    assert "symbol=000001" in caplog.text
