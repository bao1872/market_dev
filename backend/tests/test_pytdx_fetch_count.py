"""Task 4.2 测试 - pytdx_adapter.klines() fetch_count 公式验证。

验证 fetch_count = min(limit + 250, 1000) 公式。
用法：APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_pytdx_fetch_count.py -q
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.core.pytdx_adapter import PytdxAdapter


def _build_mock_kline_df() -> pd.DataFrame:
    """构造单根 bar 的 mock DataFrame（含 datetime 列）。"""
    return pd.DataFrame({
        "open": [10.0],
        "high": [10.5],
        "low": [9.8],
        "close": [10.2],
        "volume": [100000.0],
        "amount": [1020000.0],
        "datetime": [pd.Timestamp("2026-06-18 15:00:00")],
    })


@pytest.mark.asyncio
async def test_fetch_count_limit_250(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 1: limit=250 -> fetch_count=500。"""
    adapter = PytdxAdapter()
    PytdxAdapter._klines_cache.clear()
    captured: dict[str, int] = {}

    def mock_fetch(symbol: str, period: str, count: int) -> pd.DataFrame:
        captured["count"] = count
        return _build_mock_kline_df()

    monkeypatch.setattr(adapter, "_fetch_with_retry", mock_fetch)
    await adapter.klines("000001", "1d", limit=250)
    assert captured.get("count") == 500


@pytest.mark.asyncio
async def test_fetch_count_limit_5000_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 2: limit=5000 -> fetch_count=1000（封顶）。"""
    adapter = PytdxAdapter()
    PytdxAdapter._klines_cache.clear()
    captured: dict[str, int] = {}

    def mock_fetch(symbol: str, period: str, count: int) -> pd.DataFrame:
        captured["count"] = count
        return _build_mock_kline_df()

    monkeypatch.setattr(adapter, "_fetch_with_retry", mock_fetch)
    await adapter.klines("000001", "1d", limit=5000)
    assert captured.get("count") == 1000


@pytest.mark.asyncio
async def test_fetch_count_limit_100(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 3: limit=100 -> fetch_count=350。"""
    adapter = PytdxAdapter()
    PytdxAdapter._klines_cache.clear()
    captured: dict[str, int] = {}

    def mock_fetch(symbol: str, period: str, count: int) -> pd.DataFrame:
        captured["count"] = count
        return _build_mock_kline_df()

    monkeypatch.setattr(adapter, "_fetch_with_retry", mock_fetch)
    await adapter.klines("000001", "1d", limit=100)
    assert captured.get("count") == 350


if __name__ == "__main__":
    print("=== test_pytdx_fetch_count self-test ===")
    df = _build_mock_kline_df()
    assert "datetime" in df.columns
    assert len(df) == 1
    print(f"mock DataFrame OK: columns={list(df.columns)}")
    for limit, expected in [(250, 500), (5000, 1000), (100, 350)]:
        fetch_count = min(limit + 250, 1000)
        assert fetch_count == expected
        print(f"limit={limit} -> fetch_count={fetch_count} OK")
    print("OK")
