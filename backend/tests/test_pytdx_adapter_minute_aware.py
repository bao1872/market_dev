"""Task 4.3 测试 - pytdx_adapter.get_minute_bars 兼容 Asia/Shanghai aware start/end。

根因：MDAS 修复后传入 aware datetime，但 pytdx_adapter 内部将拉取到的
1m 数据 datetime 列显式 tz_localize(None) 为 naive，导致 aware Timestamp
与 datetime64[us] 比较抛出 Invalid comparison between dtype=datetime64[us] and Timestamp。

本测试验证：传入 aware start/end 时，get_minute_bars 仍能正确过滤 naive datetime 列。
用法：APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@localhost:5432/bz_stock_test \
      pytest tests/test_pytdx_adapter_minute_aware.py -q
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.core.pytdx_adapter import PytdxAdapter

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _build_mock_minute_df() -> pd.DataFrame:
    """构造 mock 1m DataFrame，datetime 列为 naive（与真实 pytdx 数据一致）。"""
    datetimes = pd.date_range("2026-07-07 09:30:00", periods=10, freq="1min")
    return pd.DataFrame({
        "open": [10.0 + i * 0.01 for i in range(10)],
        "high": [10.05 + i * 0.01 for i in range(10)],
        "low": [9.95 + i * 0.01 for i in range(10)],
        "close": [10.02 + i * 0.01 for i in range(10)],
        "volume": [100000.0] * 10,
        "amount": [1020000.0] * 10,
        # 模拟真实 pytdx 返回：naive datetime
        "datetime": datetimes.tz_localize(None),
    })


def test_get_minute_bars_aware_start_end_filters_naive_datetime(monkeypatch: pytest.MonkeyPatch) -> None:
    """aware start/end 与 naive datetime 列比较不抛异常，且过滤正确。"""
    adapter = PytdxAdapter()

    def mock_fetch(symbol: str, period: str, count: int) -> pd.DataFrame:
        return _build_mock_minute_df()

    monkeypatch.setattr(adapter, "_fetch_with_retry", mock_fetch)

    start = datetime(2026, 7, 7, 9, 30, 0, tzinfo=SHANGHAI_TZ)
    end = datetime(2026, 7, 7, 9, 35, 0, tzinfo=SHANGHAI_TZ)

    df = adapter.get_minute_bars("000001", start, end)

    assert len(df) == 6
    assert df["datetime"].iloc[0] == pd.Timestamp("2026-07-07 09:30:00")
    assert df["datetime"].iloc[-1] == pd.Timestamp("2026-07-07 09:35:00")


def test_get_minute_bars_naive_start_end_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """naive start/end 保持兼容。"""
    adapter = PytdxAdapter()

    def mock_fetch(symbol: str, period: str, count: int) -> pd.DataFrame:
        return _build_mock_minute_df()

    monkeypatch.setattr(adapter, "_fetch_with_retry", mock_fetch)

    start = datetime(2026, 7, 7, 9, 30, 0)
    end = datetime(2026, 7, 7, 9, 35, 0)

    df = adapter.get_minute_bars("000001", start, end)

    assert len(df) == 6


if __name__ == "__main__":
    print("=== test_pytdx_adapter_minute_aware self-test ===")
    df = _build_mock_minute_df()
    assert "datetime" in df.columns
    assert df["datetime"].dt.tz is None
    assert len(df) == 10
    print(f"mock naive minute DataFrame OK: columns={list(df.columns)}, rows={len(df)}")
    print("OK")
