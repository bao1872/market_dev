"""Stage B2B — BusinessDateAdjustmentContext（只读复权 overlay）单元测试（纯单元）。

核心验收：10送10 除权日，历史 daily / 历史 15m / 当前 realtime quote 必须处于
**同一价格坐标**，且 context 构建全程零 DB 写入。

不连 DB / 网络 / pytdx（repository 读取与 provider 全部 monkeypatch）。
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.repositories import bar_repository as bar_repo
from app.services import business_date_adjustment_context as ctx_mod
from app.services.adjustment_factor_service import AdjustmentFactorService
from app.services.business_date_adjustment_context import (
    BusinessDateAdjustmentService,
    BusinessDateAdjustmentUnavailableError,
)

pytestmark = pytest.mark.pure_unit

IID = uuid.UUID("11111111-2222-3333-4444-555555555555")


# =============================================================================
# helpers
# =============================================================================


def _raw_df(pairs: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame({
        "datetime": [pd.Timestamp(d) for d, _ in pairs],
        "close": [c for _, c in pairs],
    })


def _xdxr_df(events: list[dict]) -> pd.DataFrame:
    rows = []
    for e in events:
        rows.append({
            "date": pd.Timestamp(e["date"]),
            "category": e.get("category", 1),
            "fenhong": e.get("fenhong", 0.0),
            "songzhuangu": e.get("songzhuangu", 0.0),
            "peigu": e.get("peigu", 0.0),
            "peigujia": e.get("peigujia", 0.0),
        })
    return pd.DataFrame(rows, columns=[
        "date", "category", "fenhong", "songzhuangu", "peigu", "peigujia",
    ])


class _RecordingAdapter:
    """记录 get_xdxr_info 调用 kwargs（锁定 force_refresh=True）。"""

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df
        self.calls: list[dict[str, Any]] = []

    def get_xdxr_info(self, symbol: str, **kwargs: Any) -> pd.DataFrame:
        self.calls.append({"symbol": symbol, **kwargs})
        return self._df


class _FailingAdapter:
    def get_xdxr_info(self, symbol: str, **kwargs: Any) -> pd.DataFrame:
        raise RuntimeError("calling function error")


class _GuardedDb:
    """任何写入/直接 SQL 都视为违规（context 必须纯 overlay）。"""

    def __init__(self) -> None:
        self.commit = AsyncMock(side_effect=AssertionError("context must never commit"))
        self.execute = AsyncMock(
            side_effect=AssertionError("context must not run SQL directly (repo only)")
        )

    async def flush(self) -> None:
        raise AssertionError("context must never flush")


def _guard_canonical_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    """canonical rebuild / fingerprint 若被 context 调用即立即失败。"""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("context must not mutate canonical factor state")

    monkeypatch.setattr(bar_repo, "rebuild_adj_factors", _boom)
    monkeypatch.setattr(AdjustmentFactorService, "rebuild_factor_series", _boom)
    monkeypatch.setattr(AdjustmentFactorService, "detect_company_action_change", _boom)
    monkeypatch.setattr(AdjustmentFactorService, "_store_fingerprint", _boom)


def _wire_raw(monkeypatch: pytest.MonkeyPatch, raw: pd.DataFrame) -> _GuardedDb:
    db = _GuardedDb()
    monkeypatch.setattr(
        ctx_mod,
        "get_raw_daily_close_series",
        AsyncMock(return_value=raw),
    )
    _guard_canonical_mutations(monkeypatch)
    return db


async def _build(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw: pd.DataFrame,
    xdxr: pd.DataFrame,
    business_date: date,
    expected_completed_through: date,
    adapter: Any = None,
) -> tuple[Any, _RecordingAdapter]:
    db = _wire_raw(monkeypatch, raw)
    if adapter is None:
        adapter = _RecordingAdapter(xdxr)
    service = BusinessDateAdjustmentService()
    ctx = await service.build_business_date_adjustment_context(
        db,  # type: ignore[arg-type]
        instrument_id=IID,
        symbol="600519",
        business_date=business_date,
        expected_completed_through=expected_completed_through,
        adapter=adapter,
    )
    db.commit.assert_not_called()
    return ctx, adapter


# =============================================================================
# §20 核心：10送10 daily / 15m / quote 同坐标
# =============================================================================


@pytest.mark.asyncio
async def test_main_10for10_daily_15m_quote_same_coordinate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_df([("2026-09-10", 20.0), ("2026-09-11", 20.0)])
    xdxr = _xdxr_df([{"date": "2026-09-12", "songzhuangu": 10}])

    ctx, _ = await _build(
        monkeypatch,
        raw=raw,
        xdxr=xdxr,
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
    )

    assert ctx.synthetic_anchor is True
    assert ctx.freshness_proven is True
    assert ctx.degraded_reason is None
    assert ctx.denominator_factor == Decimal("1")
    assert ctx.quote_qfq_ratio == Decimal("1")

    fdf = ctx.factor_df.set_index("trade_date")["adj_factor"]
    assert abs(float(fdf[pd.Timestamp("2026-09-10")]) - 0.5) < 1e-10
    assert abs(float(fdf[pd.Timestamp("2026-09-11")]) - 0.5) < 1e-10
    assert abs(float(fdf[pd.Timestamp("2026-09-12")]) - 1.0) < 1e-10

    service = BusinessDateAdjustmentService()

    # daily：9/11 raw close=20 → qfq=20×0.5/1.0=10
    daily = pd.DataFrame(
        {"open": [20.0], "high": [20.0], "low": [20.0], "close": [20.0], "volume": [100]},
        index=pd.to_datetime(["2026-09-11"]),
    )
    daily.index.name = "bar_time"
    daily_qfq = service.apply_context_qfq(daily, ctx, intraday=False)
    daily_close = float(daily_qfq.loc[pd.Timestamp("2026-09-11"), "close"])

    # 15m：9/11 14:45 raw close=20 → qfq=10
    m15 = pd.DataFrame(
        {"open": [20.0], "high": [20.0], "low": [20.0], "close": [20.0], "volume": [10]},
        index=pd.to_datetime(["2026-09-11 14:45"]),
    )
    m15.index.name = "trade_time"
    m15_qfq = service.apply_context_qfq(m15, ctx, intraday=True)
    m15_close = float(m15_qfq.loc[pd.Timestamp("2026-09-11 14:45"), "close"])

    # realtime quote：9/12 raw=10 → qfq=10（ratio=1）
    quote_qfq = service.quote_qfq_price(10.0, ctx)

    assert abs(daily_close - 10.0) < 1e-9
    assert abs(m15_close - 10.0) < 1e-9
    assert quote_qfq == Decimal("10.0")
    assert daily_close == m15_close == float(quote_qfq)


# =============================================================================
# §21 no-action day
# =============================================================================


@pytest.mark.asyncio
async def test_no_action_day_preserves_raw_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_df([("2026-09-10", 10.0), ("2026-09-11", 10.0)])
    xdxr = _xdxr_df([])

    ctx, _ = await _build(
        monkeypatch,
        raw=raw,
        xdxr=xdxr,
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
    )

    assert ctx.factor_source_fingerprint == ""
    assert ctx.synthetic_anchor is True
    assert set(ctx.factor_df["adj_factor"].tolist()) == {1.0}

    bars = pd.DataFrame(
        {"open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0], "volume": [1]},
        index=pd.to_datetime(["2026-09-11"]),
    )
    bars.index.name = "bar_time"
    qfq = BusinessDateAdjustmentService().apply_context_qfq(bars, ctx, intraday=False)
    assert abs(float(qfq.loc[pd.Timestamp("2026-09-11"), "close"]) - 10.0) < 1e-9


# =============================================================================
# §22 future event 不提前影响 context
# =============================================================================


@pytest.mark.asyncio
async def test_future_event_does_not_affect_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_df([("2026-09-10", 20.0), ("2026-09-11", 20.0)])
    xdxr = _xdxr_df([{"date": "2026-09-18", "songzhuangu": 10}])

    ctx, _ = await _build(
        monkeypatch,
        raw=raw,
        xdxr=xdxr,
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
    )

    assert ctx.factor_source_fingerprint == ""
    assert set(ctx.factor_df["adj_factor"].tolist()) == {1.0}


# =============================================================================
# §23 provider failure → fail-closed
# =============================================================================


@pytest.mark.asyncio
async def test_provider_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_df([("2026-09-11", 20.0)])
    _wire_raw(monkeypatch, raw)
    service = BusinessDateAdjustmentService()

    with pytest.raises(BusinessDateAdjustmentUnavailableError) as ei:
        await service.build_business_date_adjustment_context(
            _GuardedDb(),  # type: ignore[arg-type]
            instrument_id=IID,
            symbol="600519",
            business_date=date(2026, 9, 12),
            expected_completed_through=date(2026, 9, 11),
            adapter=_FailingAdapter(),
        )

    assert ei.value.reason == "xdxr_force_refresh_failed"
    assert ei.value.cause is not None


# =============================================================================
# §24 force_refresh=True 锁定
# =============================================================================


@pytest.mark.asyncio
async def test_force_refresh_true_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_df([("2026-09-11", 20.0)])
    adapter = _RecordingAdapter(_xdxr_df([]))

    await _build(
        monkeypatch,
        raw=raw,
        xdxr=_xdxr_df([]),
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
        adapter=adapter,
    )

    assert adapter.calls == [{"symbol": "600519", "force_refresh": True}]


# =============================================================================
# §25 stale raw daily → fail-closed
# =============================================================================


@pytest.mark.asyncio
async def test_stale_raw_daily_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_df([("2026-09-09", 20.0), ("2026-09-10", 20.0)])
    _wire_raw(monkeypatch, raw)

    with pytest.raises(BusinessDateAdjustmentUnavailableError) as ei:
        await BusinessDateAdjustmentService().build_business_date_adjustment_context(
            _GuardedDb(),  # type: ignore[arg-type]
            instrument_id=IID,
            symbol="600519",
            business_date=date(2026, 9, 12),
            expected_completed_through=date(2026, 9, 11),
            adapter=_RecordingAdapter(_xdxr_df([])),
        )

    assert ei.value.reason == "raw_daily_not_completed_through_expected_date"


# =============================================================================
# §26 raw future leakage → fail-closed
# =============================================================================


@pytest.mark.asyncio
async def test_raw_future_leak_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_df([("2026-09-11", 20.0), ("2026-09-15", 20.0)])
    _wire_raw(monkeypatch, raw)

    with pytest.raises(BusinessDateAdjustmentUnavailableError) as ei:
        await BusinessDateAdjustmentService().build_business_date_adjustment_context(
            _GuardedDb(),  # type: ignore[arg-type]
            instrument_id=IID,
            symbol="600519",
            business_date=date(2026, 9, 12),
            expected_completed_through=date(2026, 9, 11),
            adapter=_RecordingAdapter(_xdxr_df([])),
        )

    assert ei.value.reason == "raw_daily_future_leak"


# =============================================================================
# §27 calculator 数据缺口 → 包装为 fail-closed（保留 cause）
# =============================================================================


@pytest.mark.asyncio
async def test_calculator_gap_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    # raw 存在 84 天缺口，事件落在缺口中间 → calculator 抛 bars_daily_gap
    raw = _raw_df([("2026-01-30", 26.8), ("2026-06-29", 33.42)])
    xdxr = _xdxr_df([{"date": "2026-04-24", "fenhong": 1.3}])
    _wire_raw(monkeypatch, raw)

    with pytest.raises(BusinessDateAdjustmentUnavailableError) as ei:
        await BusinessDateAdjustmentService().build_business_date_adjustment_context(
            _GuardedDb(),  # type: ignore[arg-type]
            instrument_id=IID,
            symbol="600519",
            business_date=date(2026, 6, 29),
            expected_completed_through=date(2026, 6, 29),
            adapter=_RecordingAdapter(xdxr),
        )

    assert ei.value.reason == "bars_daily_gap"
    assert ei.value.cause is not None


# =============================================================================
# §28 zero DB mutation（纯 overlay）
# =============================================================================


@pytest.mark.asyncio
async def test_context_is_read_only_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_df([("2026-09-11", 20.0)])
    db = _wire_raw(monkeypatch, raw)  # canonical rebuild/fingerprint 被 guard 成 AssertionError

    ctx = await BusinessDateAdjustmentService().build_business_date_adjustment_context(
        db,  # type: ignore[arg-type]
        instrument_id=IID,
        symbol="600519",
        business_date=date(2026, 9, 12),
        expected_completed_through=date(2026, 9, 11),
        adapter=_RecordingAdapter(_xdxr_df([])),
    )

    db.commit.assert_not_called()
    db.execute.assert_not_called()
    # synthetic anchor 只存在内存，不是 K 线行
    assert ctx.synthetic_anchor is True
    assert pd.Timestamp("2026-09-12") in set(pd.to_datetime(ctx.factor_df["trade_date"]))


# =============================================================================
# §29 factor_hash / context_hash 稳定性
# =============================================================================


@pytest.mark.asyncio
async def test_hashes_are_stable_and_future_event_agnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_df([("2026-09-10", 20.0), ("2026-09-11", 20.0)])
    future_10 = _xdxr_df([{"date": "2026-09-18", "songzhuangu": 10}])
    future_20 = _xdxr_df([{"date": "2026-09-18", "songzhuangu": 20}])

    ctx_a, _ = await _build(
        monkeypatch, raw=raw, xdxr=future_10,
        business_date=date(2026, 9, 12), expected_completed_through=date(2026, 9, 11),
    )
    ctx_b, _ = await _build(
        monkeypatch, raw=raw, xdxr=future_10,
        business_date=date(2026, 9, 12), expected_completed_through=date(2026, 9, 11),
    )
    ctx_c, _ = await _build(
        monkeypatch, raw=raw, xdxr=future_20,
        business_date=date(2026, 9, 12), expected_completed_through=date(2026, 9, 11),
    )

    # 相同输入 → 稳定
    assert ctx_a.factor_hash == ctx_b.factor_hash
    assert ctx_a.context_hash == ctx_b.context_hash
    assert len(ctx_a.factor_hash) == 16
    assert len(ctx_a.context_hash) == 16

    # future event 数值变化（仍 > business_date）→ 不影响今天的坐标版本
    assert ctx_c.factor_hash == ctx_a.factor_hash
    assert ctx_c.context_hash == ctx_a.context_hash


@pytest.mark.asyncio
async def test_context_hash_changes_when_event_becomes_effective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事件进入 effective window 后，坐标版本身份必须改变。"""
    raw_before = _raw_df([("2026-09-10", 20.0), ("2026-09-11", 20.0)])
    xdxr = _xdxr_df([{"date": "2026-09-18", "songzhuangu": 10}])

    ctx_future, _ = await _build(
        monkeypatch, raw=raw_before, xdxr=xdxr,
        business_date=date(2026, 9, 12), expected_completed_through=date(2026, 9, 11),
    )

    raw_at_event = _raw_df([("2026-09-17", 20.0), ("2026-09-18", 10.0)])
    ctx_effective, _ = await _build(
        monkeypatch, raw=raw_at_event, xdxr=xdxr,
        business_date=date(2026, 9, 18), expected_completed_through=date(2026, 9, 18),
    )

    assert ctx_future.context_hash != ctx_effective.context_hash
    assert ctx_future.factor_source_fingerprint != ctx_effective.factor_source_fingerprint


# =============================================================================
# 额外：xdqr 空 → raw_daily_empty
# =============================================================================


@pytest.mark.asyncio
async def test_empty_raw_daily_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_raw(monkeypatch, pd.DataFrame(columns=["datetime", "close"]))

    with pytest.raises(BusinessDateAdjustmentUnavailableError) as ei:
        await BusinessDateAdjustmentService().build_business_date_adjustment_context(
            _GuardedDb(),  # type: ignore[arg-type]
            instrument_id=IID,
            symbol="600519",
            business_date=date(2026, 9, 12),
            expected_completed_through=date(2026, 9, 11),
            adapter=_RecordingAdapter(_xdxr_df([])),
        )

    assert ei.value.reason == "raw_daily_empty"


# =============================================================================
# 新 public repository API：get_raw_daily_close_series
# =============================================================================


class _StubResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _StubDb:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def execute(self, stmt: Any) -> _StubResult:
        return _StubResult(self._rows)


@pytest.mark.asyncio
async def test_get_raw_daily_close_series_shape() -> None:
    db = _StubDb([(date(2026, 9, 10), 20.0), (date(2026, 9, 11), 20.5)])

    df = await bar_repo.get_raw_daily_close_series(db, IID, end_date=date(2026, 9, 11))  # type: ignore[arg-type]

    assert list(df.columns) == ["datetime", "close"]
    assert df["close"].tolist() == [20.0, 20.5]
    assert pd.api.types.is_datetime64_any_dtype(df["datetime"])


@pytest.mark.asyncio
async def test_get_raw_daily_close_series_empty() -> None:
    db = _StubDb([])

    df = await bar_repo.get_raw_daily_close_series(db, IID, end_date=date(2026, 9, 11))  # type: ignore[arg-type]

    assert df.empty
    assert list(df.columns) == ["datetime", "close"]
