"""Task 4.1 测试 - bars API DB 优先行为验证（适配 MarketDataAggregationService）。

测试 5 个场景：
1. DB 命中且为最新，返回 K 线，响应头 X-Data-Source: db
2. DB 未命中 + Pytdx 兜底命中，返回 K 线，X-Data-Source: hybrid
3. Pytdx 兜底失败不阻塞，返回 DB 数据，X-Data-Source: degraded（不返回 500）
4. 非交易时段不调 Pytdx 实时 1m（日内 15m 场景）
5. 交易时段调 Pytdx 实时 1m 并 merge，X-Data-Source: hybrid

测试策略：
- patch market_data_aggregation_service 模块内部函数（_query_daily_bars /
  fetch_daily_bars / _call_expected_last_completed_daily_bar /
  _is_trading_hours / fetch_minute_bars / _query_15min_bars 等）
- 使用 app.dependency_overrides[get_db] 注入 mock session（提供 symbol）
- 使用 TestClient 验证响应头与状态码

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_bars_api_db_first.py -q
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.api import bars as bars_api
from app.main import app
from app.services import market_data_aggregation_service as mdas

TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")


def _build_db_bars() -> pd.DataFrame:
    """构造 DB 命中的 mock 日线数据（2 根 bar，naive DatetimeIndex）。"""
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


def _build_pytdx_daily_tail() -> pd.DataFrame:
    """构造 Pytdx 兜底日线数据（3 根 bar，比 DB 更新，naive DatetimeIndex）。"""
    df = pd.DataFrame({
        "open": [10.0, 11.0, 12.0],
        "high": [10.5, 11.5, 12.5],
        "low": [9.8, 10.8, 11.8],
        "close": [10.2, 11.2, 12.2],
        "volume": [100000.0, 110000.0, 120000.0],
        "amount": [1020000.0, 1232000.0, 1464000.0],
        "adj_factor": [1.0, 1.0, 1.0],
    }, index=pd.to_datetime(["2026-06-17", "2026-06-18", "2026-06-19"]))
    df.index.name = "trade_date"
    return df


def _build_intraday_bars() -> pd.DataFrame:
    """构造 DB 命中的 mock 15min 日内数据（2 根 bar，naive DatetimeIndex）。"""
    df = pd.DataFrame({
        "open": [10.0, 10.5],
        "high": [10.5, 10.8],
        "low": [9.8, 10.3],
        "close": [10.2, 10.6],
        "volume": [100000.0, 110000.0],
        "amount": [1020000.0, 1166000.0],
        "adj_factor": [1.0, 1.0],
    }, index=pd.to_datetime(["2026-06-18 09:30:00", "2026-06-18 09:45:00"]))
    df.index.name = "trade_time"
    return df


def _build_live_1m_bars() -> pd.DataFrame:
    """构造 Pytdx 实时 1 分钟线数据（2 根 bar，naive DatetimeIndex）。

    位于 10:00-10:01，聚合为 15m 后落在 10:00 这一个 bin。
    """
    df = pd.DataFrame({
        "open": [10.6, 10.7],
        "high": [10.7, 10.9],
        "low": [10.5, 10.6],
        "close": [10.7, 10.8],
        "volume": [50000.0, 60000.0],
        "amount": [535000.0, 648000.0],
        "adj_factor": [1.0, 1.0],
    }, index=pd.to_datetime(["2026-06-18 10:00:00", "2026-06-18 10:01:00"]))
    df.index.name = "trade_time"
    return df


def _make_mock_session(symbol: str | None = "000001") -> AsyncMock:
    """构造 mock AsyncSession，execute 返回 Instrument.symbol 查询结果。"""
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


def _disable_mdas_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """[测试] - 禁用 MarketDataAggregationService 的 Redis 缓存（测试不依赖 Redis）。"""
    monkeypatch.setattr(mdas, "_cache_get", lambda *_a, **_k: None)
    monkeypatch.setattr(mdas, "_cache_set", lambda *_a, **_k: None)


# ============================================================
# 测试 1: DB 命中且为最新，X-Data-Source: db
# ============================================================


def test_db_hit_returns_data_source_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 1: DB 命中且为最新，返回 K 线，响应头 X-Data-Source: db。"""
    _override_get_db(symbol="000001")
    _disable_mdas_cache(monkeypatch)
    try:
        async def mock_query_daily(*args, **kwargs):
            return _build_db_bars()

        async def mock_expected(*args, **kwargs):
            # [测试] - DB 最新 bar 日期 2026-06-18，expected 返回更早日期使 need_tail=False
            return date(2026, 6, 17)

        monkeypatch.setattr(mdas, "_query_daily_bars", mock_query_daily)
        monkeypatch.setattr(mdas, "_call_expected_last_completed_daily_bar", mock_expected)

        client = TestClient(app)
        response = client.get(
            f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
            params={"timeframe": "1d", "adj": "none", "include_realtime": "false"},
        )

        assert response.status_code == 200
        assert response.headers["X-Data-Source"] == "db"
        assert int(response.headers["X-Total-Ms"]) >= 0
        data = response.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
    finally:
        _restore_get_db()


# ============================================================
# 测试 2: DB 未命中 + Pytdx 兜底命中，X-Data-Source: hybrid
# ============================================================


def test_db_miss_pytdx_hit_returns_data_source_hybrid(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 2: DB 未命中 + Pytdx 兜底命中，X-Data-Source: hybrid。"""
    _override_get_db(symbol="000001")
    _disable_mdas_cache(monkeypatch)
    try:
        async def mock_query_daily_empty(*args, **kwargs):
            return pd.DataFrame()

        async def mock_expected(*args, **kwargs):
            # [测试] - DB 空，expected 返回较新日期使 need_tail=True
            return date(2026, 6, 19)

        async def mock_fetch_daily(*args, **kwargs):
            return _build_pytdx_daily_tail()

        monkeypatch.setattr(mdas, "_query_daily_bars", mock_query_daily_empty)
        monkeypatch.setattr(mdas, "_call_expected_last_completed_daily_bar", mock_expected)
        monkeypatch.setattr(mdas, "fetch_daily_bars", mock_fetch_daily)

        client = TestClient(app)
        response = client.get(
            f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
            params={"timeframe": "1d", "adj": "none"},
        )

        assert response.status_code == 200
        # [mdas] - DB miss + Pytdx 兜底命中 → data_source=hybrid（合并后）
        assert response.headers["X-Data-Source"] == "hybrid"
        assert response.headers["X-Cache-Hit"] == "false"
        data = response.json()
        assert data["total"] == 3
    finally:
        _restore_get_db()


# ============================================================
# 测试 3: Pytdx 兜底失败不阻塞，返回 DB 数据，X-Data-Source: degraded
# ============================================================


def test_pytdx_failure_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 3: Pytdx 兜底失败不阻塞，返回 DB 数据，X-Data-Source: degraded（非 500）。

    场景：DB 有数据但不是最新 + Pytdx 兜底失败（抛异常）。
    预期：响应 200，X-Data-Source: degraded（仍返回 DB 数据，不阻塞）。
    """
    _override_get_db(symbol="000001")
    _disable_mdas_cache(monkeypatch)
    try:
        async def mock_query_daily(*args, **kwargs):
            return _build_db_bars()

        async def mock_expected(*args, **kwargs):
            # [测试] - DB 最新 2026-06-18，expected 返回 2026-06-19 使 need_tail=True
            return date(2026, 6, 19)

        async def mock_fetch_daily_fail(*args, **kwargs):
            # [测试] - 模拟 Pytdx 兜底失败：抛异常（mdas 内部 try/except 降级为 degraded）
            raise RuntimeError("pytdx 模拟失败")

        monkeypatch.setattr(mdas, "_query_daily_bars", mock_query_daily)
        monkeypatch.setattr(mdas, "_call_expected_last_completed_daily_bar", mock_expected)
        monkeypatch.setattr(mdas, "fetch_daily_bars", mock_fetch_daily_fail)

        client = TestClient(app)
        response = client.get(
            f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
            params={"timeframe": "1d", "adj": "none", "include_realtime": "false"},
        )

        assert response.status_code == 200
        # [mdas] - Pytdx 失败 → data_source=degraded（仍返回 DB 数据，不阻塞）
        assert response.headers["X-Data-Source"] == "degraded"
        data = response.json()
        assert data["total"] == 2
    finally:
        _restore_get_db()


# ============================================================
# 测试 4: 非交易时段不调 Pytdx 实时 1m（日内 15m 场景）
# ============================================================


def test_non_trading_hours_skips_pytdx_realtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 4: 非交易时段不调用 Pytdx 实时 1m 拉取（日内 15m 场景）。

    场景：DB 有 15min 数据 + include_realtime=True + 非交易时段。
    预期：fetch_minute_bars 不被调用，X-Data-Source: db。
    """
    _override_get_db(symbol="000001")
    _disable_mdas_cache(monkeypatch)
    try:
        async def mock_query_15min(*args, **kwargs):
            return _build_intraday_bars()

        fetch_minute_called = {"called": False}

        async def mock_fetch_minute_should_not_call(*args, **kwargs):
            fetch_minute_called["called"] = True
            return _build_live_1m_bars()

        monkeypatch.setattr(mdas, "_query_15min_bars", mock_query_15min)
        # [测试] - 非交易时段：_is_trading_hours 返回 False
        monkeypatch.setattr(mdas, "_is_trading_hours", lambda now=None: False)
        monkeypatch.setattr(mdas, "fetch_minute_bars", mock_fetch_minute_should_not_call)

        client = TestClient(app)
        response = client.get(
            f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
            params={"timeframe": "15m", "adj": "none", "include_realtime": "true"},
        )

        assert response.status_code == 200
        assert response.headers["X-Data-Source"] == "db"
        assert fetch_minute_called["called"] is False, "非交易时段不应调用 Pytdx 实时 1m"
    finally:
        _restore_get_db()


# ============================================================
# 测试 5: 交易时段调 Pytdx 实时 1m 并 merge，X-Data-Source: hybrid
# ============================================================


def test_trading_hours_calls_pytdx_and_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试 5: 交易时段内调用 Pytdx 实时 1m 并成功合并，X-Data-Source: hybrid。

    场景：DB 有 15min 数据 + 交易时段内 + include_realtime=True + Pytdx 返回 1m 数据。
    预期：fetch_minute_bars 被调用，X-Data-Source: hybrid，bars 数量增加。
    """
    _override_get_db(symbol="000001")
    _disable_mdas_cache(monkeypatch)
    try:
        async def mock_query_15min(*args, **kwargs):
            return _build_intraday_bars()

        fetch_minute_called = {"called": False}

        async def mock_fetch_minute(*args, **kwargs):
            fetch_minute_called["called"] = True
            return _build_live_1m_bars()

        monkeypatch.setattr(mdas, "_query_15min_bars", mock_query_15min)
        # [测试] - 交易时段：_is_trading_hours 返回 True
        monkeypatch.setattr(mdas, "_is_trading_hours", lambda now=None: True)
        monkeypatch.setattr(mdas, "fetch_minute_bars", mock_fetch_minute)

        client = TestClient(app)
        response = client.get(
            f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
            params={"timeframe": "15m", "adj": "none", "include_realtime": "true"},
        )

        assert response.status_code == 200
        assert response.headers["X-Data-Source"] == "hybrid"
        assert fetch_minute_called["called"] is True
        data = response.json()
        # [mdas] - DB 2 根 15m (09:30, 09:45) + 1m 聚合为 1 根 15m (10:00) = 3 根
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

    pytdx_df = _build_pytdx_daily_tail()
    assert len(pytdx_df) == 3
    print(f"Pytdx daily tail OK: shape={pytdx_df.shape}")

    intraday_df = _build_intraday_bars()
    assert len(intraday_df) == 2
    print(f"Intraday bars OK: shape={intraday_df.shape}")

    live_1m_df = _build_live_1m_bars()
    assert len(live_1m_df) == 2
    print(f"Live 1m bars OK: shape={live_1m_df.shape}")

    # 2. 验证 mock session
    session = _make_mock_session("000001")
    assert session is not None
    print("mock session OK")

    # 3. 验证 TEST_INSTRUMENT_ID
    assert TEST_INSTRUMENT_ID is not None
    print(f"TEST_INSTRUMENT_ID={TEST_INSTRUMENT_ID} OK")

    print("OK")
