"""WatchlistMonitor previous_close / change_pct 字段测试（advice.md 第三节）。

测试覆盖：
- previous_close = context.trade_date 之前最近一个交易日收盘价（前复权）
- change_pct = (current_price - previous_close) / previous_close * 100
- 当日未完成日线不得作为 previous_close
- 数据缺失时 previous_close/change_pct 为 None（不抛异常）

约束：
- 仅测试 WatchlistMonitor.calculate_state 新增字段，不重复测试 BB/VN 子 monitor 逻辑
- 通过 stub 子 monitor 隔离 WatchlistMonitor 层
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pandas as pd
import pytest

from app.strategy.monitors.watchlist_monitor import WatchlistMonitor
from app.strategy.runtime import MarketDataContext, MonitorState


def _make_daily_bars(
    end_date: date,
    n_days: int,
    start_price: float = 10.0,
    step: float = 0.0,
) -> pd.DataFrame:
    """构造 n_days 根日线（含 end_date 当日未完成 Bar），DatetimeIndex 升序。

    Args:
        end_date: 最后一根 Bar 的日期（含当日）
        n_days: 总根数
        start_price: 第一根 close 价格
        step: 每根 Bar close 增量

    Returns:
        DataFrame，index=DatetimeIndex（UTC），columns=open/high/low/close/volume
    """
    dates = [end_date - timedelta(days=n_days - 1 - i) for i in range(n_days)]
    closes = [start_price + step * i for i in range(n_days)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.05 for c in closes],
            "low": [c - 0.05 for c in closes],
            "close": closes,
            "volume": [1000] * n_days,
        },
        index=pd.DatetimeIndex([pd.Timestamp(d, tz=UTC) for d in dates], name="date"),
    )


def _make_minute_bars(close_price: float, n_bars: int = 1) -> pd.DataFrame:
    """构造 n_bars 根 1 分钟 Bar，最后一根 close 为 close_price。"""
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    times = [now - timedelta(minutes=n_bars - 1 - i) for i in range(n_bars)]
    closes = [close_price - 0.1 * (n_bars - 1 - i) for i in range(n_bars)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.01 for c in closes],
            "low": [c - 0.01 for c in closes],
            "close": closes,
            "volume": [100] * n_bars,
        },
        index=pd.DatetimeIndex(times, name="datetime"),
    )


def _stub_sub_monitor_state(state_dict: dict[str, object]) -> MonitorState:
    """构造一个最小化的 MonitorState（用于 stub BB/VN 子 monitor 返回值）。"""
    return MonitorState(
        instrument_id=uuid4(),
        strategy_version_id=uuid4(),
        state=state_dict,
        state_version=1,
        updated_at=datetime.now(UTC),
    )


def _build_context(
    bars_daily: pd.DataFrame,
    bars_minute: pd.DataFrame | None,
    trade_date: date,
) -> MarketDataContext:
    """构造 MarketDataContext（仅含必要字段）。"""
    return MarketDataContext(
        instrument_id=uuid4(),
        symbol="600519",
        bars_daily=bars_daily,
        bars_minute=bars_minute,
        bars_15min=None,
        adj_factor=None,
        trade_date=trade_date,
        bar_time=None,
    )


def _make_watchlist_monitor_with_stubs(
    bb_state: dict[str, object],
    vn_state: dict[str, object],
) -> WatchlistMonitor:
    """构造 WatchlistMonitor，把 _bb/_vn 替换为返回固定 state 的 AsyncMock。"""
    monitor = WatchlistMonitor()
    monitor._strategy_version_id = uuid4()

    bb_stub = MagicMock()
    bb_stub.calculate_state = AsyncMock(return_value=_stub_sub_monitor_state(bb_state))
    bb_stub.detect_events = AsyncMock(return_value=[])
    bb_stub.compute_indicators = AsyncMock(return_value={})

    vn_stub = MagicMock()
    vn_stub.calculate_state = AsyncMock(return_value=_stub_sub_monitor_state(vn_state))
    vn_stub.detect_events = AsyncMock(return_value=[])
    vn_stub.compute_indicators = AsyncMock(return_value={})

    monitor._bb = bb_stub
    monitor._vn = vn_stub
    return monitor


# ===== TDD Red 阶段：以下测试在实施前应失败 =====


@pytest.mark.asyncio
async def test_previous_close_is_yesterday_close():
    """previous_close 应为 trade_date 之前最近一个交易日的 close（不含当日）。"""
    today = date(2026, 6, 27)
    daily = _make_daily_bars(end_date=today, n_days=5, start_price=10.0, step=0.5)
    # closes: [10.0, 10.5, 11.0, 11.5, 12.0] - 最后一根为今日未完成
    expected_previous_close = 11.5  # 倒数第二根（昨日完成）

    current_price = 12.3
    minute = _make_minute_bars(close_price=current_price, n_bars=2)
    context = _build_context(daily, minute, trade_date=today)

    monitor = _make_watchlist_monitor_with_stubs(
        bb_state={"current_price": current_price},
        vn_state={"current_price": current_price},
    )

    state = await monitor.calculate_state(context)

    assert "previous_close" in state.state, "state 必须包含 previous_close 字段"
    assert state.state["previous_close"] == pytest.approx(expected_previous_close, abs=1e-6), (
        f"previous_close 应为 {expected_previous_close}（昨日收盘价），"
        f"实际为 {state.state['previous_close']}"
    )


@pytest.mark.asyncio
async def test_change_pct_formula_correct():
    """change_pct = (current_price - previous_close) / previous_close * 100。"""
    today = date(2026, 6, 27)
    daily = _make_daily_bars(end_date=today, n_days=3, start_price=10.0, step=0.0)
    # closes: [10.0, 10.0, 10.0] - 倒数第二根 = 10.0
    previous_close = 10.0
    current_price = 10.5
    expected_change_pct = (current_price - previous_close) / previous_close * 100  # 5.0

    minute = _make_minute_bars(close_price=current_price, n_bars=2)
    context = _build_context(daily, minute, trade_date=today)

    monitor = _make_watchlist_monitor_with_stubs(
        bb_state={"current_price": current_price},
        vn_state={"current_price": current_price},
    )

    state = await monitor.calculate_state(context)

    assert "change_pct" in state.state, "state 必须包含 change_pct 字段"
    assert state.state["change_pct"] == pytest.approx(expected_change_pct, abs=1e-6), (
        f"change_pct 应为 {expected_change_pct}，实际为 {state.state['change_pct']}"
    )


@pytest.mark.asyncio
async def test_today_incomplete_bar_not_used_as_previous_close():
    """当日未完成日线不得作为 previous_close（必须排除 trade_date 当日 Bar）。"""
    today = date(2026, 6, 27)
    # 构造 3 根：[10.0, 11.0, 999.0]，最后一根 999.0 是今日未完成
    daily = _make_daily_bars(end_date=today, n_days=3, start_price=10.0, step=1.0)
    # closes: [10.0, 11.0, 12.0]，但人为把今日（最后一根）改成 999.0 检查是否被误用
    daily.iloc[-1, daily.columns.get_loc("close")] = 999.0

    expected_previous_close = 11.0  # 倒数第二根

    current_price = 12.0
    minute = _make_minute_bars(close_price=current_price, n_bars=2)
    context = _build_context(daily, minute, trade_date=today)

    monitor = _make_watchlist_monitor_with_stubs(
        bb_state={"current_price": current_price},
        vn_state={"current_price": current_price},
    )

    state = await monitor.calculate_state(context)

    assert state.state["previous_close"] == pytest.approx(expected_previous_close, abs=1e-6), (
        f"previous_close 不得使用当日未完成 Bar (999.0)，应为 {expected_previous_close}，"
        f"实际为 {state.state['previous_close']}"
    )


@pytest.mark.asyncio
async def test_no_daily_data_returns_none():
    """日线数据为空时 previous_close/change_pct 应为 None（不抛异常）。"""
    today = date(2026, 6, 27)
    empty_daily = pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], name="date", tz=UTC),
    )
    current_price = 12.0
    minute = _make_minute_bars(close_price=current_price, n_bars=2)
    context = _build_context(empty_daily, minute, trade_date=today)

    monitor = _make_watchlist_monitor_with_stubs(
        bb_state={"current_price": current_price},
        vn_state={"current_price": current_price},
    )

    state = await monitor.calculate_state(context)

    assert state.state.get("previous_close") is None, "空日线时 previous_close 应为 None"
    assert state.state.get("change_pct") is None, "空日线时 change_pct 应为 None"


@pytest.mark.asyncio
async def test_only_today_bar_returns_none():
    """日线仅有当日 Bar 时（无历史），previous_close 应为 None。"""
    today = date(2026, 6, 27)
    daily = _make_daily_bars(end_date=today, n_days=1, start_price=10.0)
    # 仅有 1 根，且为今日，无历史可用

    current_price = 10.5
    minute = _make_minute_bars(close_price=current_price, n_bars=2)
    context = _build_context(daily, minute, trade_date=today)

    monitor = _make_watchlist_monitor_with_stubs(
        bb_state={"current_price": current_price},
        vn_state={"current_price": current_price},
    )

    state = await monitor.calculate_state(context)

    assert state.state.get("previous_close") is None, (
        "仅有当日 Bar 时 previous_close 应为 None"
    )
    assert state.state.get("change_pct") is None


@pytest.mark.asyncio
async def test_negative_change_pct():
    """下跌场景：current_price < previous_close，change_pct 应为负数。"""
    today = date(2026, 6, 27)
    daily = _make_daily_bars(end_date=today, n_days=3, start_price=10.0, step=0.0)
    # closes: [10.0, 10.0, 10.0]，previous_close = 10.0
    previous_close = 10.0
    current_price = 9.5  # 下跌
    expected_change_pct = (9.5 - 10.0) / 10.0 * 100  # -5.0

    minute = _make_minute_bars(close_price=current_price, n_bars=2)
    context = _build_context(daily, minute, trade_date=today)

    monitor = _make_watchlist_monitor_with_stubs(
        bb_state={"current_price": current_price},
        vn_state={"current_price": current_price},
    )

    state = await monitor.calculate_state(context)

    assert state.state["change_pct"] == pytest.approx(expected_change_pct, abs=1e-6), (
        f"下跌时 change_pct 应为 {expected_change_pct}（负数），"
        f"实际为 {state.state['change_pct']}"
    )


@pytest.mark.asyncio
async def test_merged_state_still_contains_bb_vn_fields():
    """新增 previous_close/change_pct 后，原 BB+VN 字段不得丢失。"""
    today = date(2026, 6, 27)
    daily = _make_daily_bars(end_date=today, n_days=5, start_price=10.0, step=0.5)
    current_price = 12.0
    minute = _make_minute_bars(close_price=current_price, n_bars=2)
    context = _build_context(daily, minute, trade_date=today)

    bb_state = {
        "bb_upper": 13.0, "bb_mid": 11.0, "bb_lower": 9.0,
        "current_price": current_price, "prev_close": 11.8,
        "bb_width": 0.36, "bb_pos": 0.83,
    }
    vn_state = {
        "current_price": current_price,
        "upper_node": {"price_mid": 12.5},
        "lower_node": {"price_mid": 9.5},
        "position_0_1": 0.6, "poc_price": None, "last_touched_node": None,
    }
    monitor = _make_watchlist_monitor_with_stubs(bb_state=bb_state, vn_state=vn_state)

    state = await monitor.calculate_state(context)

    # BB 字段保留
    for k in ("bb_upper", "bb_mid", "bb_lower", "prev_close", "bb_width", "bb_pos"):
        assert k in state.state, f"BB 字段 {k} 应保留在 merged_state"
    # VN 字段保留
    for k in ("upper_node", "lower_node", "position_0_1"):
        assert k in state.state, f"VN 字段 {k} 应保留在 merged_state"
    # 新增字段存在
    assert "previous_close" in state.state
    assert "change_pct" in state.state
