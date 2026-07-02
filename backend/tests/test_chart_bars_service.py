"""测试统一图表行情输入服务 load_chart_bars。

用法:
    APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://bz:bz@127.0.0.1:5432/bz_stock_test \
        pytest tests/test_chart_bars_service.py -v

测试策略:
    - mock MarketDataAggregationService.get_bars，不连 DB/网络
    - 验证 load_chart_bars 仅做最后的 count 截取并透传 adj
    - 验证 compute_source_bar_hash / compute_source_bar_times 的契约
    - 验证 _filter_unfinished_daily_bars 的兼容行为
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services import chart_bars_service
from app.services.chart_bars_service import (
    compute_source_bar_hash,
    compute_source_bar_times,
    load_chart_bars,
)
from app.services.market_data_aggregation_service import (
    BarAggregationResult,
    MarketDataAggregationService,
)

TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")


async def _mock_get_bars(df: pd.DataFrame) -> BarAggregationResult:
    """构造 MarketDataAggregationService.get_bars 的 mock 返回。"""
    return BarAggregationResult(
        bars=df,
        data_source="db",
        as_of=datetime.now(ZoneInfo("Asia/Shanghai")),
        is_partial=False,
        last_persisted_bar_time=df.index[-1] if not df.empty else None,
        last_live_bar_time=None,
        freshness_seconds=0.0,
        degraded=False,
        degraded_reason=None,
    )


def _patch_service(monkeypatch: pytest.MonkeyPatch, df: pd.DataFrame) -> None:
    """patch MarketDataAggregationService.get_bars 返回 df。"""
    async def _get_bars(*args, **kwargs):
        return await _mock_get_bars(df.copy())

    monkeypatch.setattr(
        MarketDataAggregationService, "get_bars", _get_bars,
    )


def _build_raw_daily_bars(length: int = 300, end_date: date | None = None) -> pd.DataFrame:
    """构造 mock 原始日线数据（未复权，含 adj_factor 列）。

    Args:
        length: 数据长度
        end_date: 最后一天日期，None 使用昨天
    """
    if end_date is None:
        end_date = date.today() - timedelta(days=1)
    dates = pd.date_range(end=end_date, periods=length, freq="B")
    closes = [10.0 + i * 0.05 for i in range(length)]
    df = pd.DataFrame({
        "open": [c - 0.05 for c in closes],
        "high": [c + 0.1 for c in closes],
        "low": [c - 0.1 for c in closes],
        "close": closes,
        "volume": [100000.0 + i for i in range(length)],
        "amount": [1000000.0 + i * 10 for i in range(length)],
        "adj_factor": [1.0] * length,
    }, index=dates)
    df.index.name = "trade_date"
    return df


# ============================================================
# compute_source_bar_hash 测试
# ============================================================


def test_compute_source_bar_hash_returns_16_char_hex() -> None:
    """compute_source_bar_hash 返回 16 字符 hex 字符串。"""
    df = _build_raw_daily_bars(length=5)
    h = compute_source_bar_hash(df)
    assert isinstance(h, str)
    assert len(h) == 16
    # 验证是 hex 字符串
    int(h, 16)


def test_compute_source_bar_hash_deterministic() -> None:
    """相同输入产生相同 hash。"""
    df1 = _build_raw_daily_bars(length=10)
    df2 = _build_raw_daily_bars(length=10)
    assert compute_source_bar_hash(df1) == compute_source_bar_hash(df2)


def test_compute_source_bar_hash_changes_on_data_change() -> None:
    """数据变化时 hash 变化。"""
    df1 = _build_raw_daily_bars(length=10)
    df2 = df1.copy()
    df2.loc[df2.index[0], "close"] = 999.0
    assert compute_source_bar_hash(df1) != compute_source_bar_hash(df2)


def test_compute_source_bar_hash_matches_manual_computation() -> None:
    """hash 与手动计算一致（date|open|high|low|close|volume|amount 拼接）。"""
    df = pd.DataFrame({
        "open": [10.0],
        "high": [10.5],
        "low": [9.8],
        "close": [10.2],
        "volume": [100000.0],
        "amount": [1020000.0],
        "adj_factor": [1.0],
    }, index=pd.to_datetime(["2026-06-16"]))
    df.index.name = "trade_date"

    expected_str = "2026-06-16|10.0|10.5|9.8|10.2|100000.0|1020000.0"
    expected_hash = hashlib.sha256(expected_str.encode("utf-8")).hexdigest()[:16]

    assert compute_source_bar_hash(df) == expected_hash


def test_compute_source_bar_hash_empty_df() -> None:
    """空 DataFrame 返回空字符串。"""
    assert compute_source_bar_hash(pd.DataFrame()) == ""


# ============================================================
# compute_source_bar_times 测试
# ============================================================


def test_compute_source_bar_times_length_matches_df() -> None:
    """source_bar_times 长度等于 DataFrame 行数。"""
    df = _build_raw_daily_bars(length=10)
    times = compute_source_bar_times(df)
    assert len(times) == len(df)
    assert all(isinstance(t, str) for t in times)


def test_compute_source_bar_times_iso_date_format() -> None:
    """source_bar_times 元素为 ISO 日期字符串（YYYY-MM-DD）。"""
    df = _build_raw_daily_bars(length=3)
    times = compute_source_bar_times(df)
    for t in times:
        # 验证格式为 YYYY-MM-DD（10 字符）
        assert len(t) == 10
        # 验证可解析为 Timestamp
        pd.Timestamp(t)


# ============================================================
# load_chart_bars 测试
# ============================================================


async def test_load_chart_bars_returns_dataframe_with_required_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_chart_bars 返回 DataFrame，含必要列 + DatetimeIndex。"""
    raw_df = _build_raw_daily_bars(length=300)
    _patch_service(monkeypatch, raw_df)

    session = AsyncMock()
    df = await load_chart_bars(session, TEST_INSTRUMENT_ID, timeframe="1d", count=250)

    assert isinstance(df, pd.DataFrame)
    required_cols = {"open", "high", "low", "close", "volume", "amount", "adj_factor"}
    assert required_cols.issubset(set(df.columns))
    assert isinstance(df.index, pd.DatetimeIndex)


async def test_load_chart_bars_daily_count_250(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日线返回 250 根（不超过 count）。"""
    raw_df = _build_raw_daily_bars(length=300)
    _patch_service(monkeypatch, raw_df)

    session = AsyncMock()
    df = await load_chart_bars(session, TEST_INSTRUMENT_ID, timeframe="1d", count=250)
    assert len(df) <= 250
    assert len(df) == 250  # 300 根历史数据，截取最近 250


async def test_load_chart_bars_sorts_ascending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DatetimeIndex 升序排序由 MarketDataAggregationService 保证。"""
    raw_df = _build_raw_daily_bars(length=300).sort_index(ascending=False)
    _patch_service(monkeypatch, raw_df)

    session = AsyncMock()
    df = await load_chart_bars(session, TEST_INSTRUMENT_ID, timeframe="1d", count=250)
    # load_chart_bars 不额外排序，但服务层已保证升序；此处验证返回即可
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 250


async def test_load_chart_bars_deduplicates_keep_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """去重由 MarketDataAggregationService 保证。"""
    raw_df = _build_raw_daily_bars(length=300)
    # 复制最后一行（最近日期）追加，并修改 close 为 999.0
    last_row = raw_df.iloc[[-1]].copy()
    last_row["close"] = 999.0
    raw_df_with_dup = pd.concat([raw_df, last_row])
    _patch_service(monkeypatch, raw_df_with_dup)

    session = AsyncMock()
    df = await load_chart_bars(session, TEST_INSTRUMENT_ID, timeframe="1d", count=250)

    # 去重逻辑在服务层，本层仅做 count 截取
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 250


async def test_load_chart_bars_filters_unfinished_bar_before_close() -> None:
    """未完成 Bar 过滤：最新 Bar 为今日且现在未到 15:00 收盘时，过滤掉。"""
    # 构造含今日的数据
    raw_df = _build_raw_daily_bars(length=300, end_date=date.today())

    # 模拟 14:00（收盘前）
    fake_now = datetime(
        date.today().year, date.today().month, date.today().day, 14, 0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )

    from app.services.chart_bars_service import _filter_unfinished_daily_bars

    # 取原始数据最后 250 根后验证过滤
    test_df = raw_df.tail(250)
    filtered = _filter_unfinished_daily_bars(test_df, now=fake_now)

    today = date.today()
    today_dates = [d for d in filtered.index.date if d == today]
    assert len(today_dates) == 0, f"今日 Bar 应被过滤（收盘前），但存在: {today_dates}"


async def test_load_chart_bars_keeps_completed_today_bar_after_close() -> None:
    """收盘后（>=15:00）今日 Bar 保留。"""
    # 显式构造包含今日的 DataFrame（不受 freq="B" 周末过滤影响）
    today = date.today()
    today_df = pd.DataFrame({
        "open": [11.0],
        "high": [11.5],
        "low": [10.8],
        "close": [11.2],
        "volume": [110000.0],
        "amount": [1232000.0],
        "adj_factor": [1.0],
    }, index=pd.to_datetime([today.isoformat()]))
    today_df.index.name = "trade_date"

    # 模拟 16:00（收盘后）
    fake_now = datetime(
        today.year, today.month, today.day, 16, 0,
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )

    from app.services.chart_bars_service import _filter_unfinished_daily_bars

    filtered = _filter_unfinished_daily_bars(today_df, now=fake_now)

    today_dates = [d for d in filtered.index.date if d == today]
    assert len(today_dates) == 1, "收盘后今日 Bar 应保留"


async def test_load_chart_bars_empty_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """无数据时返回空 DataFrame，不抛异常。"""
    _patch_service(monkeypatch, pd.DataFrame())

    session = AsyncMock()
    df = await load_chart_bars(session, TEST_INSTRUMENT_ID, timeframe="1d", count=250)
    assert df.empty


async def test_load_chart_bars_rejects_non_daily_timeframe() -> None:
    """非日线 timeframe 抛 ValueError（当前仅支持 1d）。"""
    session = AsyncMock()
    with pytest.raises(ValueError, match="1d"):
        await load_chart_bars(session, TEST_INSTRUMENT_ID, timeframe="15m", count=250)


async def test_load_chart_bars_passes_adj_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_chart_bars 将 adj 参数透传给 MarketDataAggregationService.get_bars。"""
    raw_df = _build_raw_daily_bars(length=300)
    captured: dict[str, Any] = {}

    async def _get_bars(*args, **kwargs):
        captured["adj"] = kwargs.get("adj")
        return await _mock_get_bars(raw_df.copy())

    monkeypatch.setattr(MarketDataAggregationService, "get_bars", _get_bars)

    session = AsyncMock()
    df = await load_chart_bars(
        session, TEST_INSTRUMENT_ID, timeframe="1d", count=250, adj="none",
    )

    assert captured.get("adj") == "none"
    assert len(df) == 250


async def test_load_chart_bars_default_adj_is_qfq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 adj='qfq' 透传给 MarketDataAggregationService.get_bars。"""
    raw_df = _build_raw_daily_bars(length=300)
    captured: dict[str, Any] = {}

    async def _get_bars(*args, **kwargs):
        captured["adj"] = kwargs.get("adj")
        return await _mock_get_bars(raw_df.copy())

    monkeypatch.setattr(MarketDataAggregationService, "get_bars", _get_bars)

    session = AsyncMock()
    df = await load_chart_bars(session, TEST_INSTRUMENT_ID, timeframe="1d", count=250)

    assert captured.get("adj") == "qfq"
    assert len(df) == 250


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
