"""Task 4.1 测试 - bars API DB 优先行为验证。

测试 4 个场景：
1. DB 命中，返回 K 线，响应头 X-Data-Source: db
2. DB 未命中 + Pytdx 命中，返回 K 线，X-Data-Source: pytdx
3. Pytdx 失败不阻塞，返回 DB 数据，X-Data-Source: db（不返回 502）
4. 非交易时段不调 Pytdx（交易时段内外行为差异）

测试策略：
- 使用 app.dependency_overrides[get_db] 注入 mock session（提供 symbol）
- monkeypatch bars_api 内部辅助函数（_query_db_only / _fetch_bars_with_pytdx_fallback 等）
- 使用 TestClient 验证响应头与状态码

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_bars_api_db_first.py -q
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.api import bars as bars_api
from app.main import app

TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")


def _build_db_bars() -> pd.DataFrame:
    """构造 DB 命中的 mock 日线数据（2 根 bar）。"""
    df = pd.DataFrame({
        "open": [10.0, 11.0],
        "high": [10.5, 11.5],
        "low": [9.8, 10.8],
        "close": [10.2, 11.2],
        "volume": [100000.0, 110000.0],
        "amount": [1020000.0, 1232000.0],
        "adj_factor": [1.0, 1.0],
    }, index=pd.to_datetime(["2026-06-17", "2026-06-18"]))
    df.index.name = "trade_date"
    return df


def _build_pytdx_last_bar() -> pd.DataFrame:
    """构造 Pytdx 实时最后一根 bar（tz-aware，比 DB 最新 bar 更新）。"""
    df = pd.DataFrame({
        "open": [12.0],
        "high": [12.5],
        "low": [11.8],
        "close": [12.2],
        "volume": [120000.0],
        "amount": [1464000.0],
        "adj_factor": [1.0],
    }, index=pd.to_datetime(["2026-06-19 14:30:00"]).tz_localize("Asia/Shanghai"))
    df.index.name = "trade_time"
    return df


def _make_mock_session(symbol: str | None = "000001") -> AsyncMock:
    """构造 mock AsyncSession，execute 返回 Instrument.symbol 查询结果。

    Args:
        symbol: 返回的股票代码，None 表示无 instrument

    Returns:
        mock AsyncSession
    """
    session = AsyncMock()
    result = MagicMock()
    if symbol is not None:
        result.first.return_value = (symbol,)
    else:
        result.first.return_value = None
    session.execute.return_value = result
    return session


def _override_get_db(symbol: str | None = "000001") -> None:
    """覆盖 app 的 get_db 依赖，注入 mock session 提供 symbol。"""
    async def _mock_get_db():
        yield _make_mock_session(symbol)
    app.dependency_overrides[bars_api.get_db] = _mock_get_db


def _restore_get_db() -> None:
    """恢复 get_db 依赖。"""
    app.dependency_overrides.pop(bars_api.get_db, None)


# ============================================================
# 测试 1: DB 命中，X-Data-Source: db
# ============================================================


def test_db_hit_returns_data_source_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 1: DB 命中，返回 K 线，响应头 X-Data-Source: db。"""
    _override_get_db(symbol="000001")
    try:
        async def mock_query_db_only(*args, **kwargs):
            return _build_db_bars()

        monkeypatch.setattr(bars_api, "_query_db_only", mock_query_db_only)
        # [测试] - 非交易时段，避免触发 Pytdx 实时补充
        monkeypatch.setattr(bars_api, "_is_trading_hours", lambda: False)

        client = TestClient(app)
        response = client.get(
            f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
            params={"timeframe": "1d", "adj": "none", "include_realtime": "false"},
        )

        assert response.status_code == 200
        assert response.headers["X-Data-Source"] == "db"
        assert response.headers["X-Cache-Hit"] == "true"
        assert int(response.headers["X-Total-Ms"]) >= 0
        data = response.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
    finally:
        _restore_get_db()


# ============================================================
# 测试 2: DB 未命中 + Pytdx 命中，X-Data-Source: pytdx
# ============================================================


def test_db_miss_pytdx_hit_returns_data_source_pytdx(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 2: DB 未命中 + Pytdx 兜底命中，X-Data-Source: pytdx。"""
    _override_get_db(symbol="000001")
    try:
        async def mock_query_db_only_empty(*args, **kwargs):
            return pd.DataFrame()

        async def mock_pytdx_fallback(*args, **kwargs):
            return _build_db_bars()

        monkeypatch.setattr(bars_api, "_query_db_only", mock_query_db_only_empty)
        monkeypatch.setattr(bars_api, "_fetch_bars_with_pytdx_fallback", mock_pytdx_fallback)
        monkeypatch.setattr(bars_api, "_is_trading_hours", lambda: False)

        client = TestClient(app)
        response = client.get(
            f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
            params={"timeframe": "1d", "adj": "none"},
        )

        assert response.status_code == 200
        assert response.headers["X-Data-Source"] == "pytdx"
        assert response.headers["X-Cache-Hit"] == "false"
        data = response.json()
        assert data["total"] == 2
    finally:
        _restore_get_db()


# ============================================================
# 测试 3: Pytdx 失败不阻塞，返回 DB 数据，X-Data-Source: db
# ============================================================


def test_pytdx_failure_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 3: Pytdx 实时补充失败不阻塞，返回 DB 数据，X-Data-Source: db（非 502）。

    场景：DB 有数据 + 交易时段内 + include_realtime=True + Pytdx 失败返回 None。
    预期：响应 200，X-Data-Source: db（未合并为 hybrid）。
    """
    _override_get_db(symbol="000001")
    try:
        async def mock_query_db_only(*args, **kwargs):
            return _build_db_bars()

        async def mock_fetch_last_bar_pytdx_fail(*args, **kwargs):
            # [测试] - 模拟 Pytdx 失败：返回 None（不抛异常，符合 _fetch_last_bar_from_pytdx 的降级逻辑）
            return None

        monkeypatch.setattr(bars_api, "_query_db_only", mock_query_db_only)
        monkeypatch.setattr(bars_api, "_is_trading_hours", lambda: True)
        monkeypatch.setattr(bars_api, "_fetch_last_bar_from_pytdx", mock_fetch_last_bar_pytdx_fail)

        client = TestClient(app)
        response = client.get(
            f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
            params={"timeframe": "1d", "adj": "none", "include_realtime": "true"},
        )

        assert response.status_code == 200
        # Pytdx 失败，未合并新 bar，data_source 保持 db
        assert response.headers["X-Data-Source"] == "db"
        data = response.json()
        assert data["total"] == 2
    finally:
        _restore_get_db()


# ============================================================
# 测试 4: 非交易时段不调 Pytdx（交易时段内外行为差异）
# ============================================================


def test_non_trading_hours_skips_pytdx(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 4: 非交易时段不调用 Pytdx 实时补充。

    场景：DB 有数据 + include_realtime=True + 非交易时段。
    预期：_fetch_last_bar_from_pytdx 不被调用，X-Data-Source: db。
    """
    _override_get_db(symbol="000001")
    try:
        async def mock_query_db_only(*args, **kwargs):
            return _build_db_bars()

        pytdx_called = {"called": False}

        async def mock_fetch_last_bar_should_not_call(*args, **kwargs):
            pytdx_called["called"] = True
            return _build_pytdx_last_bar()

        monkeypatch.setattr(bars_api, "_query_db_only", mock_query_db_only)
        # [测试] - 非交易时段：_is_trading_hours 返回 False
        monkeypatch.setattr(bars_api, "_is_trading_hours", lambda: False)
        monkeypatch.setattr(bars_api, "_fetch_last_bar_from_pytdx", mock_fetch_last_bar_should_not_call)

        client = TestClient(app)
        response = client.get(
            f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
            params={"timeframe": "1d", "adj": "none", "include_realtime": "true"},
        )

        assert response.status_code == 200
        assert response.headers["X-Data-Source"] == "db"
        assert pytdx_called["called"] is False, "非交易时段不应调用 Pytdx 实时补充"
    finally:
        _restore_get_db()


def test_trading_hours_calls_pytdx_and_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 4 补充: 交易时段内调用 Pytdx 并成功合并，X-Data-Source: hybrid。

    场景：DB 有数据 + 交易时段内 + include_realtime=True + Pytdx 返回更新 bar。
    预期：_fetch_last_bar_from_pytdx 被调用，X-Data-Source: hybrid。
    """
    _override_get_db(symbol="000001")
    try:
        async def mock_query_db_only(*args, **kwargs):
            return _build_db_bars()

        pytdx_called = {"called": False}

        async def mock_fetch_last_bar(*args, **kwargs):
            pytdx_called["called"] = True
            return _build_pytdx_last_bar()

        monkeypatch.setattr(bars_api, "_query_db_only", mock_query_db_only)
        monkeypatch.setattr(bars_api, "_is_trading_hours", lambda: True)
        monkeypatch.setattr(bars_api, "_fetch_last_bar_from_pytdx", mock_fetch_last_bar)

        client = TestClient(app)
        response = client.get(
            f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
            params={"timeframe": "1d", "adj": "none", "include_realtime": "true"},
        )

        assert response.status_code == 200
        assert response.headers["X-Data-Source"] == "hybrid"
        assert pytdx_called["called"] is True
        data = response.json()
        # DB 2 根 + Pytdx 1 根 = 3 根
        assert data["total"] == 3
    finally:
        _restore_get_db()


if __name__ == "__main__":
    print("=== test_bars_api_db_first self-test ===")

    # 1. 验证 mock DataFrame 结构
    db_df = _build_db_bars()
    assert len(db_df) == 2
    assert "close" in db_df.columns
    print(f"DB bars OK: shape={db_df.shape}")

    pytdx_df = _build_pytdx_last_bar()
    assert len(pytdx_df) == 1
    assert pytdx_df.index.tz is not None
    print(f"Pytdx last bar OK: shape={pytdx_df.shape}, tz={pytdx_df.index.tz}")

    # 2. 验证 mock session
    session = _make_mock_session("000001")
    assert session is not None
    print("mock session OK")

    # 3. 验证 TEST_INSTRUMENT_ID
    assert TEST_INSTRUMENT_ID is not None
    print(f"TEST_INSTRUMENT_ID={TEST_INSTRUMENT_ID} OK")

    print("OK")
