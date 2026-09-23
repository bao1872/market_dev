"""PANJI-TDX-RELIABILITY-PARITY-02 / Task 2 — canonical live provider outage error.

验证 MDAS live PROVIDER_DIRECT 链把「已确认的 provider 源失败」
（``PytdxSourceError``）翻译成 provider-neutral 的 ``LiveMarketDataUnavailable``，
且不改变：
- 编程/契约错误（TypeError / KeyError / ...）的 loud 上抛；
- 空 DataFrame 的合法语义（不是 outage）；
- DB_ONLY 历史分钟行情的零触网合同。

Run:
    PURE_UNIT_TEST=1 APP_ENV=test .venv/bin/python -m pytest tests/test_live_market_data_unavailable.py -v
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.core.pytdx_adapter import PytdxSourceError
from app.services import market_data_aggregation_service as mdas
from app.services.market_data_aggregation_service import (
    LiveMarketDataUnavailable,
    MarketDataAggregationService,
    MarketDataSourcePolicy,
)

TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")


async def _async_return(value: Any) -> Any:
    return value


def _mock_session() -> AsyncMock:
    return AsyncMock()


def _build_df(count: int) -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=count, freq="15min")
    return pd.DataFrame(
        {
            "open": [1.0] * count,
            "high": [1.1] * count,
            "low": [0.9] * count,
            "close": [1.0] * count,
            "volume": [100.0] * count,
            "amount": [1000.0] * count,
            "adj_factor": [1.0] * count,
        },
        index=idx,
    )


class _FakeTdxAdapter:
    """可配置行为的假 TDX adapter：source / programming / empty 三种路径。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode

    def get_15min_bars(self, symbol: str, count: int) -> pd.DataFrame:
        if self.mode == "source_fail":
            raise PytdxSourceError(operation="get_15min_bars", message="provider unreachable")
        if self.mode == "programming_fail":
            raise TypeError("injected programming error")
        if self.mode == "empty":
            return pd.DataFrame()
        raise AssertionError(f"unexpected mode {self.mode}")

    def get_60min_bars(self, symbol: str, count: int) -> pd.DataFrame:
        if self.mode == "source_fail":
            raise PytdxSourceError(operation="get_60min_bars", message="provider unreachable")
        if self.mode == "empty":
            return pd.DataFrame()
        raise AssertionError(f"unexpected mode {self.mode}")


# ─────────────────────────────────────────────────────────────────────────────
# T2-1 / T2-2 — 确认 source failure 翻译成 canonical error（15m / 1h）
# ─────────────────────────────────────────────────────────────────────────────


async def test_t2_1_15m_source_error_translated_to_canonical(monkeypatch) -> None:
    monkeypatch.setattr(mdas, "_get_symbol", lambda *a, **kw: _async_return("600519"))
    monkeypatch.setattr(mdas, "get_pytdx_adapter", lambda: _FakeTdxAdapter("source_fail"))
    with pytest.raises(LiveMarketDataUnavailable) as ei:
        await mdas.fetch_15min_bars(_mock_session(), TEST_INSTRUMENT_ID, count=16)
    assert ei.value.symbol == "600519"
    assert ei.value.timeframe == "15m"
    assert ei.value.provider_family == "tdx"
    assert isinstance(ei.value.__cause__, PytdxSourceError)


async def test_t2_2_1h_source_error_translated_to_canonical(monkeypatch) -> None:
    monkeypatch.setattr(mdas, "_get_symbol", lambda *a, **kw: _async_return("600519"))
    monkeypatch.setattr(mdas, "get_pytdx_adapter", lambda: _FakeTdxAdapter("source_fail"))
    with pytest.raises(LiveMarketDataUnavailable) as ei:
        await mdas.fetch_60min_bars(_mock_session(), TEST_INSTRUMENT_ID, count=4)
    assert ei.value.symbol == "600519"
    assert ei.value.timeframe == "1h"
    assert ei.value.provider_family == "tdx"
    assert isinstance(ei.value.__cause__, PytdxSourceError)


# ─────────────────────────────────────────────────────────────────────────────
# T2-3 — 编程/契约错误原样 loud 上抛，绝不翻译成 canonical error
# ─────────────────────────────────────────────────────────────────────────────


async def test_t2_3_programming_error_not_translated(monkeypatch) -> None:
    monkeypatch.setattr(mdas, "_get_symbol", lambda *a, **kw: _async_return("600519"))
    monkeypatch.setattr(mdas, "get_pytdx_adapter", lambda: _FakeTdxAdapter("programming_fail"))
    # 不是 LiveMarketDataUnavailable（否则 TypeError 不被捕获 → 用例失败）
    with pytest.raises(TypeError):
        await mdas.fetch_15min_bars(_mock_session(), TEST_INSTRUMENT_ID, count=16)


# ─────────────────────────────────────────────────────────────────────────────
# T2-4 — 空 DataFrame 是合法空数据，不是 outage
# ─────────────────────────────────────────────────────────────────────────────


async def test_t2_4_empty_provider_result_is_not_outage(monkeypatch) -> None:
    monkeypatch.setattr(mdas, "_get_symbol", lambda *a, **kw: _async_return("600519"))
    monkeypatch.setattr(mdas, "get_pytdx_adapter", lambda: _FakeTdxAdapter("empty"))
    result = await mdas.fetch_15min_bars(_mock_session(), TEST_INSTRUMENT_ID, count=16)
    assert isinstance(result, pd.DataFrame)
    assert result.empty


# ─────────────────────────────────────────────────────────────────────────────
# T2-5 — PROVIDER_DIRECT provider outage → canonical error，且禁止静默 fallback 到 DB
# ─────────────────────────────────────────────────────────────────────────────


async def test_t2_5_provider_direct_outage_is_canonical_and_no_db_fallback(monkeypatch) -> None:
    service = MarketDataAggregationService()
    stale_db_bars = _build_df(4000)
    db_query_calls: list[int] = []

    async def _db_fallback(*args: Any, **kwargs: Any) -> pd.DataFrame:
        db_query_calls.append(1)
        return stale_db_bars.copy()

    monkeypatch.setattr(mdas, "_get_symbol", lambda *a, **kw: _async_return("600519"))
    monkeypatch.setattr(mdas, "_query_15min_bars", _db_fallback)
    monkeypatch.setattr(mdas, "_get_listing_date", lambda *a, **kw: _async_return(None))
    monkeypatch.setattr(mdas, "get_pytdx_adapter", lambda: _FakeTdxAdapter("source_fail"))

    with pytest.raises(LiveMarketDataUnavailable) as ei:
        await service.get_bars(
            _mock_session(), TEST_INSTRUMENT_ID,
            timeframe="15m", adj="none", limit=4000,
            source_policy=MarketDataSourcePolicy.PROVIDER_DIRECT,
        )
    assert ei.value.timeframe == "15m"
    assert ei.value.provider_family == "tdx"
    assert db_query_calls == [], "provider 失败后绝不允许读 DB 旧分钟线兜底（掩盖故障）"


# ─────────────────────────────────────────────────────────────────────────────
# T2-6 — DB_ONLY 历史分钟行情零触网（15m / 1h）
# ─────────────────────────────────────────────────────────────────────────────


async def test_t2_6_db_only_does_not_call_provider(monkeypatch) -> None:
    service = MarketDataAggregationService()
    provider_calls: list[str] = []

    async def _tracking_15m(*args: Any, **kwargs: Any) -> pd.DataFrame:
        provider_calls.append("15m")
        return pd.DataFrame()

    async def _tracking_60m(*args: Any, **kwargs: Any) -> pd.DataFrame:
        provider_calls.append("1h")
        return pd.DataFrame()

    db_bars = _build_df(300)

    async def _db_15m(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return db_bars.copy()

    async def _db_60m(*args: Any, **kwargs: Any) -> pd.DataFrame:
        return db_bars.copy()

    monkeypatch.setattr(mdas, "fetch_15min_bars", _tracking_15m)
    monkeypatch.setattr(mdas, "fetch_60min_bars", _tracking_60m)
    monkeypatch.setattr(mdas, "_query_15min_bars", _db_15m)
    monkeypatch.setattr(mdas, "_query_60min_bars", _db_60m)
    monkeypatch.setattr(mdas, "_get_listing_date", lambda *a, **kw: _async_return(None))

    await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="15m", adj="none", limit=300,
        source_policy=MarketDataSourcePolicy.DB_ONLY,
    )
    await service.get_bars(
        _mock_session(), TEST_INSTRUMENT_ID,
        timeframe="1h", adj="none", limit=4,
        source_policy=MarketDataSourcePolicy.DB_ONLY,
    )
    assert provider_calls == [], "DB_ONLY 历史分钟行情不得触网（零 provider 调用）"
