"""[MARKET-FACT] RealtimeMarketFactService 单元测试（纯单元，mock pytdx / Eastmoney）。

覆盖契约：
1. pytdx 主源优先：SH / SZ 优先批量调用 get_security_quotes_with_provenance；
2. 数量规范化：pytdx vol（手）乘以 100 转换为 canonical volume（股）；
3. 容错回退：pytdx 抛出 PytdxSourceError 时，降级至 Eastmoney 实时快照；
4. 北交所路由：BJ 标的（如 920xxx）pytdx 原生不支持，直接路由至 Eastmoney 备用源；
5. PriceTracker 连续价格区间正确维护：首次调用返回 (p, p)，后续返回 (p_last, p_curr)。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.core.pytdx_adapter import PytdxCallProvenance, PytdxSourceError
from app.services.eod_market_snapshot_provider import EodSnapshotRow
from app.services.realtime_market_fact_service import (
    PriceTracker,
    RealtimeMarketFactService,
)
from app.services.realtime_market_snapshot_provider import RealtimeMarketSnapshot

_SH_TZ = ZoneInfo("Asia/Shanghai")


def test_price_tracker_intervals() -> None:
    tracker = PriceTracker()

    # 首次更新：无跨度区间 (10.0, 10.0)
    p_last, p_curr = tracker.update_price("600519", 10.0)
    assert p_last == 10.0
    assert p_curr == 10.0

    # 第二次更新：产生真实区间 (10.0, 10.5)
    p_last, p_curr = tracker.update_price("600519", 10.5)
    assert p_last == 10.0
    assert p_curr == 10.5

    # 另一只标的独立记录
    p_last_b, p_curr_b = tracker.update_price("000001", 15.0)
    assert p_last_b == 15.0
    assert p_curr_b == 15.0

    # 清空后重新从当前价起步
    tracker.clear()
    assert tracker.get_last_price("600519") is None
    p_last, p_curr = tracker.update_price("600519", 12.0)
    assert p_last == 12.0
    assert p_curr == 12.0


@pytest.mark.asyncio
async def test_fact_service_prefers_pytdx_for_sh_sz() -> None:
    class MockAdapter:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def get_security_quotes_with_provenance(
            self, symbols: list[str]
        ) -> tuple[list[dict[str, Any]], PytdxCallProvenance]:
            self.calls.append(symbols)
            rows = [
                {
                    "code": "600519",
                    "price": 105.5,
                    "last_close": 100.0,
                    "open": 101.0,
                    "high": 106.0,
                    "low": 100.5,
                    "vol": 500,  # 500 手
                    "amount": 5275000.0,
                },
                {
                    "code": "000001",
                    "price": 12.0,
                    "last_close": 11.5,
                    "open": 11.6,
                    "high": 12.2,
                    "low": 11.4,
                    "vol": 1000,  # 1000 手
                    "amount": 1200000.0,
                },
            ]
            return rows, PytdxCallProvenance(server=("127.0.0.1", 7709), connection_generation=1)

    adapter = MockAdapter()
    service = RealtimeMarketFactService()

    quotes = await service.fetch_quotes(["600519", "000001"], adapter=adapter)  # type: ignore[arg-type]

    assert len(quotes) == 2
    assert "600519" in quotes
    assert quotes["600519"].source == "pytdx"
    assert quotes["600519"].price == 105.5
    # 手转股：500 手 * 100 = 50000 股
    assert quotes["600519"].volume == 50000.0
    assert quotes["000001"].volume == 100000.0
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_fact_service_bj_routed_to_eastmoney(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import realtime_market_fact_service as fact_mod

    now = datetime.now(_SH_TZ)
    fake_snapshot = RealtimeMarketSnapshot(
        source_host="push2.eastmoney.com",
        captured_at=now,
        market_watermark=now,
        rows=(
            EodSnapshotRow(
                symbol="920001",
                name="北交测试",
                market="BJ",
                updated_at=now,
                open=Decimal("10.0"),
                high=Decimal("10.5"),
                low=Decimal("9.8"),
                close=Decimal("10.2"),
                previous_close=Decimal("10.0"),
                volume=Decimal("20000"),  # 股
                amount=Decimal("204000"),
            ),
        ),
        raw_count=1,
        normalized_count=1,
    )

    fake_fetch = AsyncMock(return_value=fake_snapshot)
    monkeypatch.setattr(fact_mod, "fetch_realtime_a_share_snapshot", fake_fetch)

    class MockAdapter:
        def get_security_quotes_with_provenance(self, symbols: list[str]) -> Any:
            return [], PytdxCallProvenance(server=("", 0), connection_generation=0)

    service = RealtimeMarketFactService()
    quotes = await service.fetch_quotes(["920001"], adapter=MockAdapter())  # type: ignore[arg-type]

    assert "920001" in quotes
    assert quotes["920001"].source == "eastmoney"
    assert quotes["920001"].price == 10.2
    assert quotes["920001"].volume == 20000.0
    fake_fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_fact_service_falls_back_to_eastmoney_on_pytdx_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import realtime_market_fact_service as fact_mod

    now = datetime.now(_SH_TZ)
    fake_snapshot = RealtimeMarketSnapshot(
        source_host="push2.eastmoney.com",
        captured_at=now,
        market_watermark=now,
        rows=(
            EodSnapshotRow(
                symbol="600519",
                name="贵州茅台",
                market="SH",
                updated_at=now,
                open=Decimal("100.0"),
                high=Decimal("105.0"),
                low=Decimal("99.0"),
                close=Decimal("104.0"),
                previous_close=Decimal("100.0"),
                volume=Decimal("50000"),
                amount=Decimal("5200000"),
            ),
        ),
        raw_count=1,
        normalized_count=1,
    )

    fake_fetch = AsyncMock(return_value=fake_snapshot)
    monkeypatch.setattr(fact_mod, "fetch_realtime_a_share_snapshot", fake_fetch)

    class FailingAdapter:
        def get_security_quotes_with_provenance(self, symbols: list[str]) -> Any:
            raise PytdxSourceError(operation="get_security_quotes", message="mock failure")

    service = RealtimeMarketFactService()
    quotes = await service.fetch_quotes(["600519"], adapter=FailingAdapter())  # type: ignore[arg-type]

    assert "600519" in quotes
    assert quotes["600519"].source == "eastmoney"
    assert quotes["600519"].price == 104.0
    assert quotes["600519"].volume == 50000.0
    fake_fetch.assert_awaited_once()
