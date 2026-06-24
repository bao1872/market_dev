"""Task 7 (R5 行情仓储) 测试。

测试内容：
1. 前复权计算（apply_adj_factor / apply_adj_factor_intraday）
2. 新鲜度检查（check_daily_freshness / check_minute_freshness）
3. 行情查询 API（GET /api/v1/instruments/{id}/bars）
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repositories.bar_repository import apply_adj_factor_to_bars
from app.services.adj_factor import apply_adj_factor, apply_adj_factor_intraday
from app.services.freshness_sla import (
    _MINUTE_CHECK_SLA_SECONDS,
    DAILY_SLA_SECONDS,
    check_daily_freshness,
    check_minute_freshness,
)

# 测试用 instrument_id
TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")


# ============================================================
# 1. 前复权计算测试
# ============================================================


def _build_sample_bars() -> pd.DataFrame:
    """构造不复权日线样本（3 个交易日，含 1 次送转）。"""
    bars = pd.DataFrame({
        "open": [10.0, 5.0, 5.2],
        "high": [10.5, 5.5, 5.6],
        "low": [9.8, 4.8, 5.0],
        "close": [10.2, 5.2, 5.4],
        "volume": [100000, 200000, 150000],
    }, index=pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"]))
    bars.index.name = "trade_date"
    return bars


def _build_sample_adj_factor() -> pd.DataFrame:
    """构造复权因子样本（06-16 adj=2.0，06-17/18 adj=1.0）。"""
    return pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"]),
        "adj_factor": [2.0, 1.0, 1.0],
    })


def test_apply_adj_factor_basic() -> None:
    """测试日线前复权基本计算。

    场景：06-16 adj=2.0，06-17/18 adj=1.0（latest）
    预期：06-16 价格 × (2.0/1.0) = ×2；06-17/18 价格不变
    """
    bars_df = _build_sample_bars()
    adj_df = _build_sample_adj_factor()

    qfq_df = apply_adj_factor(bars_df, adj_df)

    # 06-16 close = 10.2 × 2.0 = 20.4
    assert abs(float(qfq_df.loc[pd.Timestamp("2026-06-16"), "close"]) - 20.4) < 1e-6
    # 06-16 open = 10.0 × 2.0 = 20.0
    assert abs(float(qfq_df.loc[pd.Timestamp("2026-06-16"), "open"]) - 20.0) < 1e-6
    # 06-17/18 close 不变（ratio=1.0）
    assert abs(float(qfq_df.loc[pd.Timestamp("2026-06-17"), "close"]) - 5.2) < 1e-6
    assert abs(float(qfq_df.loc[pd.Timestamp("2026-06-18"), "close"]) - 5.4) < 1e-6
    # volume 不变（不复权）
    assert float(qfq_df.loc[pd.Timestamp("2026-06-16"), "volume"]) == 100000


def test_apply_adj_factor_empty_bars() -> None:
    """测试空 bars 输入。"""
    assert apply_adj_factor(pd.DataFrame(), _build_sample_adj_factor()).empty


def test_apply_adj_factor_empty_adj() -> None:
    """测试空 adj_factor 输入（应原样返回）。"""
    bars_df = _build_sample_bars()
    result = apply_adj_factor(bars_df, pd.DataFrame())
    pd.testing.assert_frame_equal(result, bars_df)


def test_apply_adj_factor_intraday() -> None:
    """测试分钟线前复权。

    同一交易日内的所有分钟 bar 使用相同的 adj_factor。
    """
    minute_idx = pd.to_datetime([
        "2026-06-16 09:30", "2026-06-16 09:31",
        "2026-06-17 09:30", "2026-06-18 09:30",
    ])
    bars_df = pd.DataFrame({
        "open": [10.0, 10.1, 5.0, 5.2],
        "high": [10.2, 10.3, 5.1, 5.3],
        "low": [9.9, 10.0, 4.9, 5.1],
        "close": [10.1, 10.2, 5.0, 5.2],
        "volume": [1000, 1200, 2000, 1500],
    }, index=minute_idx)
    bars_df.index.name = "trade_time"

    adj_df = _build_sample_adj_factor()
    qfq_df = apply_adj_factor_intraday(bars_df, adj_df)

    # 06-16 09:30 close = 10.1 × 2.0 = 20.2
    assert abs(float(qfq_df.loc[minute_idx[0], "close"]) - 20.2) < 1e-6
    # 06-16 09:31 close = 10.2 × 2.0 = 20.4
    assert abs(float(qfq_df.loc[minute_idx[1], "close"]) - 20.4) < 1e-6
    # 06-17/18 close 不变
    assert abs(float(qfq_df.loc[minute_idx[2], "close"]) - 5.0) < 1e-6


def test_apply_adj_factor_missing_date_ffill() -> None:
    """测试缺失日期的 ffill 逻辑（向量化 merge_asof）。

    场景：bars 有 06-16, 06-18；adj 只有 06-16, 06-17
    预期：06-18 ffill 06-17 的 adj_factor=1.0，close 不变
    """
    bars_df = pd.DataFrame({
        "open": [10.0, 5.2],
        "high": [10.5, 5.6],
        "low": [9.8, 5.0],
        "close": [10.2, 5.4],
        "volume": [100000, 150000],
    }, index=pd.to_datetime(["2026-06-16", "2026-06-18"]))
    bars_df.index.name = "trade_date"

    adj_df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-06-16", "2026-06-17"]),
        "adj_factor": [2.0, 1.0],
    })

    qfq_df = apply_adj_factor(bars_df, adj_df)
    # 06-16 close = 10.2 × 2.0 = 20.4
    assert abs(float(qfq_df.loc[pd.Timestamp("2026-06-16"), "close"]) - 20.4) < 1e-6
    # 06-18 close = 5.4 × (1.0/1.0) = 5.4（ffill 06-17 的 adj=1.0）
    assert abs(float(qfq_df.loc[pd.Timestamp("2026-06-18"), "close"]) - 5.4) < 1e-6


def test_apply_adj_factor_to_bars_repository_wrapper() -> None:
    """测试 repository 层的 apply_adj_factor_to_bars 封装。"""
    bars_df = _build_sample_bars()
    adj_df = _build_sample_adj_factor()

    qfq_df = apply_adj_factor_to_bars(bars_df, adj_df, intraday=False)
    assert abs(float(qfq_df.loc[pd.Timestamp("2026-06-16"), "close"]) - 20.4) < 1e-6

    # 分钟线模式
    minute_idx = pd.to_datetime(["2026-06-16 09:30", "2026-06-17 09:30"])
    minute_bars = pd.DataFrame({
        "open": [10.0, 5.0], "high": [10.5, 5.5],
        "low": [9.8, 4.8], "close": [10.2, 5.2],
        "volume": [1000, 2000],
    }, index=minute_idx)
    qfq_minute = apply_adj_factor_to_bars(minute_bars, adj_df, intraday=True)
    assert abs(float(qfq_minute.loc[minute_idx[0], "close"]) - 20.4) < 1e-6


# ============================================================
# 2. 新鲜度检查测试
# ============================================================


def _make_mock_session(scalar_value) -> AsyncMock:
    """构造 mock AsyncSession，execute 返回指定 scalar 值。"""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar.return_value = scalar_value
    session.execute.return_value = result_mock
    return session


@pytest.mark.asyncio
async def test_check_daily_freshness_no_data() -> None:
    """测试日线无数据时返回不新鲜。"""
    session = _make_mock_session(None)
    result = await check_daily_freshness(session, TEST_INSTRUMENT_ID)

    assert result.is_fresh is False
    assert result.last_update is None
    assert result.age_seconds is None
    assert result.sla_seconds == DAILY_SLA_SECONDS


@pytest.mark.asyncio
async def test_check_daily_freshness_with_data() -> None:
    """测试日线有数据时返回 FreshnessResult（age 取决于当前时间）。"""
    today = date.today()
    session = _make_mock_session(today)
    result = await check_daily_freshness(session, TEST_INSTRUMENT_ID)

    assert result.is_fresh is not None
    assert result.last_update is not None
    assert result.age_seconds is not None
    assert result.sla_seconds == DAILY_SLA_SECONDS
    # last_update 应为今天 15:00
    assert result.last_update.hour == 15


@pytest.mark.asyncio
async def test_check_minute_freshness_no_data() -> None:
    """测试分钟线无数据时返回不新鲜。"""
    session = _make_mock_session(None)
    result = await check_minute_freshness(session, TEST_INSTRUMENT_ID)

    assert result.is_fresh is False
    assert result.last_update is None
    assert result.age_seconds is None
    assert result.sla_seconds == _MINUTE_CHECK_SLA_SECONDS


@pytest.mark.asyncio
async def test_check_minute_freshness_recent() -> None:
    """测试分钟线数据新鲜（最近 60 秒）。"""
    recent_time = datetime.now() - pd.Timedelta(seconds=60)
    session = _make_mock_session(recent_time)
    result = await check_minute_freshness(session, TEST_INSTRUMENT_ID)

    # 60 秒 < 90 秒 SLA，应为 fresh
    assert result.is_fresh is True
    assert result.age_seconds is not None
    assert result.age_seconds >= 60
    assert result.sla_seconds == _MINUTE_CHECK_SLA_SECONDS


@pytest.mark.asyncio
async def test_check_minute_freshness_stale() -> None:
    """测试分钟线数据过期（2 小时前）。"""
    stale_time = datetime.now() - pd.Timedelta(hours=2)
    session = _make_mock_session(stale_time)
    result = await check_minute_freshness(session, TEST_INSTRUMENT_ID)

    assert result.is_fresh is False
    assert result.age_seconds is not None
    assert result.age_seconds > _MINUTE_CHECK_SLA_SECONDS


# ============================================================
# 3. 行情查询 API 测试
# ============================================================


def test_get_bars_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试行情查询 API（无数据返回空列表）。"""
    from app.api import bars as bars_api

    async def mock_fetch(*args, **kwargs):
        return pd.DataFrame()

    monkeypatch.setattr(bars_api, "fetch_daily_bars", mock_fetch)

    client = TestClient(app)
    response = client.get(
        f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
        params={"timeframe": "1d", "adj": "none"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["items"] == []
    assert data["timeframe"] == "1d"
    assert data["adj"] == "none"


def test_get_bars_with_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试行情查询 API（有数据返回）。"""
    from app.api import bars as bars_api

    async def mock_fetch(*args, **kwargs):
        df = pd.DataFrame({
            "open": [10.0, 11.0],
            "high": [10.5, 11.5],
            "low": [9.8, 10.8],
            "close": [10.2, 11.2],
            "volume": [100000, 110000],
            "amount": [1020000, 1232000],
            "adj_factor": [1.0, 1.0],
        }, index=pd.to_datetime(["2026-06-17", "2026-06-18"]))
        df.index.name = "trade_date"
        return df

    monkeypatch.setattr(bars_api, "fetch_daily_bars", mock_fetch)

    client = TestClient(app)
    response = client.get(
        f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
        params={"timeframe": "1d", "adj": "none"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["close"] == 10.2
    assert data["items"][0]["trade_date"] == "2026-06-17"
    assert data["items"][0]["trade_time"] is None


def test_get_bars_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试行情查询 API 分页。"""
    from app.api import bars as bars_api

    async def mock_fetch(*args, **kwargs):
        # 构造 5 条数据
        dates = pd.to_datetime([f"2026-06-1{i}" for i in range(5)])
        df = pd.DataFrame({
            "open": [10.0 + i for i in range(5)],
            "high": [10.5 + i for i in range(5)],
            "low": [9.8 + i for i in range(5)],
            "close": [10.2 + i for i in range(5)],
            "volume": [100000 + i * 1000 for i in range(5)],
            "amount": [1020000 + i * 10000 for i in range(5)],
            "adj_factor": [1.0] * 5,
        }, index=dates)
        df.index.name = "trade_date"
        return df

    monkeypatch.setattr(bars_api, "fetch_daily_bars", mock_fetch)

    client = TestClient(app)
    # 请求第 1 页，每页 2 条
    response = client.get(
        f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
        params={"timeframe": "1d", "adj": "none", "page": 1, "page_size": 2},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 5
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) == 2
    assert data["items"][0]["close"] == 13.2  # 第一页第一条（按最新返回）


def test_get_bars_invalid_timeframe() -> None:
    """测试无效 timeframe 参数返回 400。"""
    client = TestClient(app)
    response = client.get(
        f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
        params={"timeframe": "5m"},
    )
    assert response.status_code == 400


def test_get_bars_invalid_adj() -> None:
    """测试无效 adj 参数返回 400。"""
    client = TestClient(app)
    response = client.get(
        f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
        params={"timeframe": "1d", "adj": "hfq"},
    )
    assert response.status_code == 400


def test_get_bars_qfq(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试前复权行情查询。"""
    from app.api import bars as bars_api

    async def mock_fetch(*args, **kwargs):
        df = pd.DataFrame({
            "open": [10.0, 5.0],
            "high": [10.5, 5.5],
            "low": [9.8, 4.8],
            "close": [10.2, 5.2],
            "volume": [100000, 200000],
            "amount": [1020000, 1040000],
            "adj_factor": [2.0, 1.0],
        }, index=pd.to_datetime(["2026-06-16", "2026-06-17"]))
        df.index.name = "trade_date"
        return df

    async def mock_get_adj(*args, **kwargs):
        return pd.DataFrame({
            "trade_date": pd.to_datetime(["2026-06-16", "2026-06-17"]),
            "adj_factor": [2.0, 1.0],
        })

    monkeypatch.setattr(bars_api, "fetch_daily_bars", mock_fetch)
    monkeypatch.setattr(bars_api, "_get_adj_factor_df", mock_get_adj)

    client = TestClient(app)
    response = client.get(
        f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/bars",
        params={"timeframe": "1d", "adj": "qfq"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["adj"] == "qfq"
    # 06-16 close 前复权后 = 10.2 × 2.0 = 20.4
    assert data["items"][0]["close"] == 20.4
    # 06-17 close 不变 = 5.2
    assert data["items"][1]["close"] == 5.2


if __name__ == "__main__":
    # 自测入口：直接运行 pytest
    pytest.main([__file__, "-v"])
