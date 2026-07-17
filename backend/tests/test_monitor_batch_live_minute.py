"""monitor_batch_service live 1m 测试 - 交易时段必须拿到最新已完成 1m bar。

覆盖：
1. `MonitorBatchService.execute_monitor_cycle()` 使用 `include_realtime=True` 获取 1m。
2. `result.last_minute_bar_time` 等于最新已完成 1m bar 时间。
3. 监控算法能基于 live 1m 进入 detect_events 并产生状态。

用法：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/test_monitor_batch_live_minute.py -q
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.watchlist import UserWatchlistItem
from app.services.monitor_batch_service import MonitorBatchService


async def _create_watchlist_monitor_version(db_session: AsyncSession) -> StrategyVersion:
    """创建 watchlist_monitor 策略定义及 released 版本。"""
    definition = StrategyDefinition(
        strategy_key="watchlist_monitor",
        kind="monitor",
        display_name="BB+节点监控",
    )
    db_session.add(definition)
    await db_session.flush()

    version = StrategyVersion(
        strategy_definition_id=definition.id,
        version="1.0.0",
        status="released",
        manifest={
            "outputs": [
                {"key": "bb_upper", "type": "numeric"},
                {"key": "bb_mid", "type": "numeric"},
                {"key": "bb_lower", "type": "numeric"},
                {"key": "node_position", "type": "numeric"},
            ],
        },
        build_hash="test_hash",
        released_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    return version


def _build_minute_df(
    trade_date: date,
    start_minute: int = 31,
    count: int = 5,
) -> pd.DataFrame:
    """构造已完成 1m bar DataFrame（9:30 后的连续分钟）。"""
    rows = []
    base_price = 10.0
    for i in range(count):
        price = base_price + i * 0.1
        rows.append({
            "open": price,
            "high": price + 0.05,
            "low": price - 0.05,
            "close": price + 0.02,
            "volume": 1000.0 + i * 100,
            "amount": (1000.0 + i * 100) * price,
            "adj_factor": 1.0,
        })
    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex([
        datetime.combine(trade_date, datetime.min.time().replace(hour=9, minute=start_minute + i)).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        for i in range(count)
    ])
    df.index.name = "trade_time"
    return df


def _build_daily_df(trade_date: date, count: int = 30) -> pd.DataFrame:
    """构造历史日线 DataFrame（满足监控算法最少 25 根日线输入）。"""
    rows = []
    dates = []
    for i in range(count, 0, -1):
        d = trade_date - pd.Timedelta(days=i)
        price = 10.0 + (count - i) * 0.01
        rows.append({
            "open": price - 0.05,
            "high": price + 0.05,
            "low": price - 0.08,
            "close": price,
            "volume": 10000.0,
            "amount": 10000.0 * price,
            "adj_factor": 1.0,
        })
        dates.append(pd.Timestamp(d))
    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex(dates)
    df.index.name = "trade_date"
    return df


@pytest.mark.asyncio
async def test_monitor_cycle_uses_live_minute_bars(
    db_session: AsyncSession,
    user_factory,
    subscription_factory,
    instrument_factory,
    monkeypatch,
):
    """execute_monitor_cycle 交易时段使用 live 1m，拿到最新已完成 bar 时间。"""
    active_admin = await user_factory(roles=["admin"], status="active")
    instrument = await instrument_factory(symbol="600519", market="SH", status="active")

    await _create_watchlist_monitor_version(db_session)

    # 清理本事务内可见的历史 watchlist 数据，确保测试隔离
    await db_session.execute(delete(UserWatchlistItem).where(UserWatchlistItem.active.is_(True)))
    await db_session.flush()

    db_session.add(UserWatchlistItem(
        user_id=active_admin.id,
        instrument_id=instrument.id,
        active=True,
        source="manual",
    ))
    await db_session.flush()

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    minute_df = _build_minute_df(today, count=5)
    daily_df = _build_daily_df(today, count=30)

    # mock _fetch_md_bars_with_meta：直接返回 (DataFrame, data_source)，与真实方法签名一致
    async def _mock_fetch_md_bars_with_meta(
        self,
        db: AsyncSession,
        instrument_id: uuid.UUID,
        timeframe: str,
        *,
        adj: str = "qfq",
        limit: int | None = None,
        include_realtime: bool = True,
        completed_only: bool = False,
        start_date=None,
        end_date=None,
    ) -> tuple[pd.DataFrame, str, bool]:
        if timeframe == "1m":
            return minute_df, "hybrid", True
        if timeframe == "1d":
            return daily_df, "db", False
        return pd.DataFrame(), "db", False

    monkeypatch.setattr(MonitorBatchService, "_fetch_md_bars_with_meta", _mock_fetch_md_bars_with_meta)

    service = MonitorBatchService()
    result = await service.execute_monitor_cycle(db_session)

    assert result.total_instruments == 1
    assert result.last_minute_bar_time is not None
    assert result.last_minute_data_source == "hybrid"
    # monitor_batch 会剔除最后一根可能未完成的 1m bar，所以期望时间为倒数第二根
    expected_time = minute_df.index[-2].floor("1min").to_pydatetime().replace(second=0, microsecond=0)
    actual_time = result.last_minute_bar_time
    if actual_time.tzinfo is None and expected_time.tzinfo is not None:
        actual_time = actual_time.replace(tzinfo=expected_time.tzinfo)
    assert actual_time == expected_time
    assert result.total_states_computed >= 1


@pytest.mark.asyncio
async def test_monitor_cycle_1m_uses_include_realtime(
    db_session: AsyncSession,
    user_factory,
    subscription_factory,
    instrument_factory,
    monkeypatch,
):
    """execute_monitor_cycle 调用 1m 时必须带 include_realtime=True。"""
    active_admin = await user_factory(roles=["admin"], status="active")
    instrument = await instrument_factory(symbol="600519", market="SH", status="active")

    await _create_watchlist_monitor_version(db_session)

    await db_session.execute(delete(UserWatchlistItem).where(UserWatchlistItem.active.is_(True)))
    await db_session.flush()

    db_session.add(UserWatchlistItem(
        user_id=active_admin.id,
        instrument_id=instrument.id,
        active=True,
        source="manual",
    ))
    await db_session.flush()

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    minute_df = _build_minute_df(today, count=5)
    daily_df = _build_daily_df(today, count=30)

    captured_calls: list[dict[str, Any]] = []

    async def _mock_fetch_md_bars_with_meta(
        self,
        db: AsyncSession,
        instrument_id: uuid.UUID,
        timeframe: str,
        *,
        adj: str = "qfq",
        limit: int | None = None,
        include_realtime: bool = True,
        completed_only: bool = False,
        start_date=None,
        end_date=None,
    ) -> tuple[pd.DataFrame, str, bool]:
        captured_calls.append({"timeframe": timeframe, "include_realtime": include_realtime, "completed_only": completed_only})
        if timeframe == "1m":
            return minute_df, "hybrid", True
        if timeframe == "1d":
            return daily_df, "db", False
        return pd.DataFrame(), "db", False

    monkeypatch.setattr(MonitorBatchService, "_fetch_md_bars_with_meta", _mock_fetch_md_bars_with_meta)

    service = MonitorBatchService()
    await service.execute_monitor_cycle(db_session)

    calls_1m = [c for c in captured_calls if c["timeframe"] == "1m"]
    assert calls_1m, "应该调用 timeframe=1m"
    assert all(c["include_realtime"] is True for c in calls_1m)


@pytest.mark.asyncio
async def test_monitor_calc_inputs_daily_15m_non_realtime(
    db_session: AsyncSession,
    user_factory,
    subscription_factory,
    instrument_factory,
    monkeypatch,
):
    """watchlist_monitor 计算输入口径（CHANGE-20260710-002）：

    - 1m 必须 include_realtime=True 且剔除最后一根未完成 bar；
    - daily/15m 计算输入必须 include_realtime=False（保守口径，不得被截图实时性污染）；
    - source_bar_time 仍来自最新已完成 1m bar（剔除最后一根）。
    """
    active_admin = await user_factory(roles=["admin"], status="active")
    instrument = await instrument_factory(symbol="600519", market="SH", status="active")

    await _create_watchlist_monitor_version(db_session)

    await db_session.execute(delete(UserWatchlistItem).where(UserWatchlistItem.active.is_(True)))
    await db_session.flush()

    db_session.add(UserWatchlistItem(
        user_id=active_admin.id,
        instrument_id=instrument.id,
        active=True,
        source="manual",
    ))
    await db_session.flush()

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    minute_df = _build_minute_df(today, count=5)
    daily_df = _build_daily_df(today, count=30)

    captured_calls: list[dict[str, Any]] = []

    async def _mock_fetch_md_bars_with_meta(
        self,
        db: AsyncSession,
        instrument_id: uuid.UUID,
        timeframe: str,
        *,
        adj: str = "qfq",
        limit: int | None = None,
        include_realtime: bool = True,
        completed_only: bool = False,
        start_date=None,
        end_date=None,
    ) -> tuple[pd.DataFrame, str, bool]:
        captured_calls.append({"timeframe": timeframe, "include_realtime": include_realtime, "completed_only": completed_only})
        if timeframe == "1m":
            return minute_df, "hybrid", True
        if timeframe == "1d":
            return daily_df, "db", False
        if timeframe == "15m":
            return pd.DataFrame(), "db", False
        return pd.DataFrame(), "db", False

    monkeypatch.setattr(MonitorBatchService, "_fetch_md_bars_with_meta", _mock_fetch_md_bars_with_meta)

    service = MonitorBatchService()
    result = await service.execute_monitor_cycle(db_session)

    calls_1m = [c for c in captured_calls if c["timeframe"] == "1m"]
    calls_daily = [c for c in captured_calls if c["timeframe"] == "1d"]
    calls_15m = [c for c in captured_calls if c["timeframe"] == "15m"]

    # 1m：实时 + 剔除最后一根未完成 bar → source_bar_time 为倒数第二根
    assert calls_1m, "必须调用 timeframe=1m"
    assert all(c["include_realtime"] is True for c in calls_1m), "1m 必须 include_realtime=True"
    assert result.last_minute_bar_time is not None
    expected_time = minute_df.index[-2].floor("1min").to_pydatetime().replace(second=0, microsecond=0)
    actual_time = result.last_minute_bar_time
    if actual_time.tzinfo is None and expected_time.tzinfo is not None:
        actual_time = actual_time.replace(tzinfo=expected_time.tzinfo)
    assert actual_time == expected_time

    # daily/15m 计算输入：非实时（保守口径，不被截图实时性污染）
    assert calls_daily, "必须调用 daily 计算输入"
    assert all(c["include_realtime"] is False for c in calls_daily), "daily 计算输入不得 include_realtime=True"
    # [CHANGE-20260717-002 SSOT] - daily/15m 计算输入必须 completed_only=True（仅已完成 bar）
    assert all(c["completed_only"] is True for c in calls_daily), "daily 计算输入必须 completed_only=True"
    assert calls_15m, "必须调用 15m 计算输入（Node Cluster 筹码分布）"
    assert all(c["include_realtime"] is False for c in calls_15m), "15m 计算输入不得 include_realtime=True"
    assert all(c["completed_only"] is True for c in calls_15m), "15m 计算输入必须 completed_only=True"
