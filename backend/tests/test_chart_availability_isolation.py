"""PANJI-TDX-RELIABILITY-PARITY-03 / Task 3 — chart availability isolation (C1–C9).

冻结合同：健康的 1d / 1w / 1mo 图表，在 **optional** live 日内富化（Node 15m
LIVE_DIRECT / PROVIDER_DIRECT）不可用时，必须仍然 HTTP 200 + 保留 K 线，
只把 Node 依赖指标显式标记为 unavailable（绝不伪造指标值）。

而 **MANDATORY** base timeframe（15m / 1h）自身依赖 live provider 时，
provider outage 必须原样上抛 → API 映射为 typed 503。

Run:
    PURE_UNIT_TEST=1 APP_ENV=test .venv/bin/python -m pytest tests/test_chart_availability_isolation.py -v
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services import chart_snapshot_service as css
from app.services.indicator_service import IndicatorMarketDataMode
from app.services.chart_snapshot_service import (
    LIVE_INTRADAY_UNAVAILABLE_REASON,
    ChartSnapshotService,
)
from app.services.market_data_aggregation_service import (
    BarAggregationResult,
    LiveMarketDataUnavailable,
    MarketDataAggregationService,
    MarketDataSourcePolicy,
)

TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")


def _build_daily_bars(count: int = 250) -> pd.DataFrame:
    """构造健康的日线 DataFrame（index=trade_date）。"""
    dates = pd.date_range("2026-01-01", periods=count, freq="D")
    closes = [10.0 + i * 0.01 for i in range(count)]
    df = pd.DataFrame(
        {
            "open": [c - 0.05 for c in closes],
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [100000.0 + i for i in range(count)],
            "amount": [1000000.0 + i * 10 for i in range(count)],
            "adj_factor": [1.0] * count,
        },
        index=dates,
    )
    df.index.name = "trade_date"
    return df


def _healthy_bars_result(df: pd.DataFrame) -> BarAggregationResult:
    return BarAggregationResult(
        bars=df,
        data_source="db",
        as_of=datetime.now(),
        is_partial=False,
        last_persisted_bar_time=pd.Timestamp(df.index[-1]),
        last_live_bar_time=None,
        freshness_seconds=1.0,
        degraded=False,
        degraded_reason=None,
    )


def _normal_indicators() -> dict[str, Any]:
    return {
        "layers": [{"layer_id": "macd"}],
        "data": {"macd": {"value": 1.0}},
        "errors": {},
        "timeframe": "1d",
        "source_bar_times": [],
        "source_bar_hash": "abc",
        "availability": "available",
        "degraded_reason": None,
        "display_frame": None,
    }


def _node_outage_error() -> LiveMarketDataUnavailable:
    """Node live 15m（LIVE_DIRECT → PROVIDER_DIRECT → TDX）源失败。"""
    return LiveMarketDataUnavailable(
        symbol="688813",
        timeframe="15m",
        provider_family="tdx",
        reason="pytdx connection unavailable; operation=get_15min_bars",
    )


def _patch_healthy_base_bars(monkeypatch, df: pd.DataFrame) -> None:
    async def _fake_get_bars(self, *args: Any, **kwargs: Any) -> BarAggregationResult:
        return _healthy_bars_result(df)

    monkeypatch.setattr(MarketDataAggregationService, "get_bars", _fake_get_bars)


# ─────────────────────────────────────────────────────────────────────────────
# C1 — 1d base bars 健康 + Node 健康 → 正常，不降级
# ─────────────────────────────────────────────────────────────────────────────


async def test_c1_1d_healthy_is_not_degraded(monkeypatch) -> None:
    df = _build_daily_bars(250)
    _patch_healthy_base_bars(monkeypatch, df)

    async def _ok_indicators(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return _normal_indicators()

    monkeypatch.setattr(css, "compute_all_indicators", _ok_indicators)

    result = await ChartSnapshotService.compute_bars_and_indicators(
        AsyncMock(), TEST_INSTRUMENT_ID, timeframe="1d", adj="qfq", bars=250,
    )
    assert result.degraded is False
    assert result.degraded_reason is None
    assert result.indicators["availability"] == "available"
    assert not result.page_df.empty


# ─────────────────────────────────────────────────────────────────────────────
# C2 / C3 — 1d / 1w / 1mo：base 健康 + Node outage → 降级返回，图表仍然可用
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("timeframe", ["1d", "1w", "1mo"])
async def test_c2_c3_optional_node_outage_degrades_chart_not_fail(
    monkeypatch, timeframe: str,
) -> None:
    df = _build_daily_bars(250)
    _patch_healthy_base_bars(monkeypatch, df)

    async def _failing_indicators(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise _node_outage_error()

    monkeypatch.setattr(css, "compute_all_indicators", _failing_indicators)

    result = await ChartSnapshotService.compute_bars_and_indicators(
        AsyncMock(), TEST_INSTRUMENT_ID, timeframe=timeframe, adj="qfq", bars=250,
    )

    # 图表不失败：base bars / page_df 完好保留
    assert result.is_empty is False
    assert not result.page_df.empty
    assert len(result.page_df) > 0
    # base bars 本身没有降级（bars 是健康的）
    assert result.bars_result.degraded is False
    # 图表级降级元数据
    assert result.degraded is True
    assert result.degraded_reason == LIVE_INTRADAY_UNAVAILABLE_REASON
    # 指标显式 unavailable（沿用既有嵌套约定 data["node_cluster"]），且**没有伪造任何值**
    node_cluster = result.indicators["data"]["node_cluster"]
    assert node_cluster["availability"] == "unavailable"
    assert node_cluster["degraded_reason"] == LIVE_INTRADAY_UNAVAILABLE_REASON
    assert result.indicators["layers"] == []
    assert result.indicators["errors"]["_chart_snapshot"] == LIVE_INTRADAY_UNAVAILABLE_REASON
    # 顶层不再另造 envelope 字段（FIX5）
    assert "availability" not in result.indicators
    assert "degraded_reason" not in result.indicators
    # 不谎报 bars/indicators 一致
    assert result.render_frame["matched"] is False


# ─────────────────────────────────────────────────────────────────────────────
# C4 / C5 — 15m / 1h：MANDATORY base → provider outage 必须原样上抛（→ API 503）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("timeframe", ["15m", "1h"])
async def test_c4_c5_mandatory_timeframe_propagates_outage(
    monkeypatch, timeframe: str,
) -> None:
    df = _build_daily_bars(250)
    _patch_healthy_base_bars(monkeypatch, df)

    async def _failing_indicators(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise _node_outage_error()

    monkeypatch.setattr(css, "compute_all_indicators", _failing_indicators)

    # MANDATORY：绝不能被降级吞掉，必须原样上抛给 API 层映射 503
    with pytest.raises(LiveMarketDataUnavailable):
        await ChartSnapshotService.compute_bars_and_indicators(
            AsyncMock(), TEST_INSTRUMENT_ID, timeframe=timeframe, adj="qfq", bars=250,
        )


# ─────────────────────────────────────────────────────────────────────────────
# C6 — 编程错误绝不能被当作「行情源降级」吞掉
# ─────────────────────────────────────────────────────────────────────────────


async def test_c6_programming_error_not_swallowed_as_degraded(monkeypatch) -> None:
    df = _build_daily_bars(250)
    _patch_healthy_base_bars(monkeypatch, df)

    async def _programming_error(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise TypeError("injected programming error")

    monkeypatch.setattr(css, "compute_all_indicators", _programming_error)

    # TypeError 必须原样 loud 上抛（保留正常 500 行为）
    with pytest.raises(TypeError):
        await ChartSnapshotService.compute_bars_and_indicators(
            AsyncMock(), TEST_INSTRUMENT_ID, timeframe="1d", adj="qfq", bars=250,
        )


# ─────────────────────────────────────────────────────────────────────────────
# C7 — 历史 / PIT：零 provider 调用（DB_ONLY 下推）
# ─────────────────────────────────────────────────────────────────────────────


async def test_c7_historical_request_uses_db_only(monkeypatch) -> None:
    df = _build_daily_bars(250)
    captured: list[Any] = []
    indicator_kwargs: dict[str, Any] = {}

    async def _fake_get_bars(self, *args: Any, **kwargs: Any) -> BarAggregationResult:
        captured.append(kwargs.get("source_policy"))
        return _healthy_bars_result(df)

    monkeypatch.setattr(MarketDataAggregationService, "get_bars", _fake_get_bars)

    async def _ok_indicators(*args: Any, **kwargs: Any) -> dict[str, Any]:
        indicator_kwargs.update(kwargs)
        return _normal_indicators()

    monkeypatch.setattr(css, "compute_all_indicators", _ok_indicators)

    await ChartSnapshotService.compute_bars_and_indicators(
        AsyncMock(), TEST_INSTRUMENT_ID, timeframe="1d", adj="qfq", bars=250,
        adjustment_as_of=date(2026, 7, 1),
    )
    # base bars：DB_ONLY（zero-network）
    assert captured, "应发生一次 base bars 读取"
    assert captured[0] is MarketDataSourcePolicy.DB_ONLY
    # 指标链必须收到 HISTORICAL_DB —— 否则 Node 会走 LIVE_DIRECT 访问 live provider
    assert indicator_kwargs.get("market_data_mode") is IndicatorMarketDataMode.HISTORICAL_DB


async def test_c7b_historical_live_outage_is_not_swallowed(monkeypatch) -> None:
    """[FIX4] historical / PIT 链意外访问 live provider → 必须 loud fail。

    zero-network 是冻结合同：任何 live provider 调用都是错误，绝不能被
    「可选富化降级」静默吞成 HTTP 200 degraded（那会把回归藏起来）。
    """
    df = _build_daily_bars(250)
    _patch_healthy_base_bars(monkeypatch, df)

    async def _failing_indicators(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise _node_outage_error()

    monkeypatch.setattr(css, "compute_all_indicators", _failing_indicators)

    with pytest.raises(LiveMarketDataUnavailable):
        await ChartSnapshotService.compute_bars_and_indicators(
            AsyncMock(), TEST_INSTRUMENT_ID, timeframe="1d", adj="qfq", bars=250,
            adjustment_as_of=date(2026, 7, 1),
        )


# ─────────────────────────────────────────────────────────────────────────────
# C8 — 688813 事故复现：健康日 K + Node 15m 全挂 → 图表仍然成功，日 K 非空
# ─────────────────────────────────────────────────────────────────────────────


async def test_c8_incident_688813_daily_chart_survives_node_outage(monkeypatch) -> None:
    df = _build_daily_bars(250)
    _patch_healthy_base_bars(monkeypatch, df)

    async def _failing_indicators(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise _node_outage_error()

    monkeypatch.setattr(css, "compute_all_indicators", _failing_indicators)

    result = await ChartSnapshotService.compute_bars_and_indicators(
        AsyncMock(), TEST_INSTRUMENT_ID, timeframe="1d", adj="qfq", bars=250,
    )
    # 事故不再可能：日 K payload 非空、图表成功
    assert not result.page_df.empty
    assert len(result.page_df) == 250
    assert result.bars_display_frame is not None
    assert result.degraded is True
    assert result.degraded_reason == LIVE_INTRADAY_UNAVAILABLE_REASON


# ─────────────────────────────────────────────────────────────────────────────
# C9 — Node outage 不得用 stale DB 分钟线冒充：降级响应里没有任何 Node 值
# ─────────────────────────────────────────────────────────────────────────────


async def test_c9_no_stale_db_minute_fallback_for_node_outage(monkeypatch) -> None:
    df = _build_daily_bars(250)
    _patch_healthy_base_bars(monkeypatch, df)

    async def _failing_indicators(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise _node_outage_error()

    monkeypatch.setattr(css, "compute_all_indicators", _failing_indicators)

    result = await ChartSnapshotService.compute_bars_and_indicators(
        AsyncMock(), TEST_INSTRUMENT_ID, timeframe="1d", adj="qfq", bars=250,
    )
    indicators = result.indicators
    node_cluster = indicators["data"]["node_cluster"]
    # 没有任何被"补"出来的 Node / 指标值（无 layers、profile 为空）
    assert indicators["layers"] == []
    assert node_cluster["profile_rows"] == []
    assert node_cluster["node_regions"] == []
    # 可用性状态与「合法空结果」可区分（沿用既有嵌套约定）
    assert node_cluster["availability"] == "unavailable"


# ─────────────────────────────────────────────────────────────────────────────
# C4 / C5（HTTP 层）— MANDATORY 15m / 1h provider outage → typed 503，不是裸 500
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("timeframe", ["15m", "1h"])
async def test_http_503_for_mandatory_live_outage(monkeypatch, timeframe: str) -> None:
    from httpx import AsyncClient

    from app.core.deps import get_db
    from app.main import app
    from app.services.access_control_service import (
        require_instrument_market_access,
    )
    from tests.conftest import make_asgi_transport

    async def _fake_db() -> Any:
        yield AsyncMock()

    app.dependency_overrides[get_db] = _fake_db
    app.dependency_overrides[require_instrument_market_access] = lambda: object()

    async def _raise_outage(*args: Any, **kwargs: Any) -> Any:
        raise _node_outage_error()

    monkeypatch.setattr(
        ChartSnapshotService, "compute_bars_and_indicators", _raise_outage,
    )

    try:
        transport = make_asgi_transport(app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                f"/v1/instruments/{TEST_INSTRUMENT_ID}/chart-snapshot"
                f"?timeframe={timeframe}",
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "LIVE_MARKET_DATA_UNAVAILABLE"
    assert detail["timeframe"] == timeframe
    assert detail["provider_family"] == "tdx"
    # 不得泄漏底层异常原文 / server IP
    assert "pytdx connection unavailable" not in resp.text
