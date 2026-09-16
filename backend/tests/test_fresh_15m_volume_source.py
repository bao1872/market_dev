"""USER-FIX-3 / C — 盘中监控 15m 成交量新鲜度（纯单元，无 DB / 无网络）。

问题定义（冻结）：
    盘中监控名义上使用 15m，但拿到的是**上一交易日收盘为止**的陈旧 15m
    （MDAS 的 completed_only=True 强制 include_realtime=False，而盘中没有任何
    15m 落库任务）。结果：当日盘中成交量不进入筹码分布。

本轮目标：
    历史 persisted 15m + **当前交易日最新 completed 15m**（不含 forming bar）。

契约（必须由测试锁死）：
  1. MDAS 默认行为完全不变：completed_only=True 仍强制 include_realtime=False
     （既有 15m consumer 不受影响）。
  2. 只有显式 fresh_intraday_tail=True 且周期为日内时，才允许读实时尾部。
  3. 日/周/月线即使传 fresh_intraday_tail=True 也不生效。
  4. NodeClusterInputProvider 的 15m 输入必须走 fresh 尾部，并继续用
     _filter_unfinished_15m_bars 丢弃 forming bar（10:07 不得包含 10:00-10:15）。
  5. 午休不得被拼成跨午休 bar（13:07 排除 13:00-13:15；13:16 包含）。

运行：
    cd backend && PURE_UNIT_TEST=1 python -m pytest tests/test_fresh_15m_volume_source.py -v
"""
from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.market_data_aggregation_service as mdas_mod
import app.services.node_cluster_input_provider as provider_mod
from app.core.time import SHANGHAI_TZ
from app.services.node_cluster_input_provider import NodeClusterInputProvider

pytestmark = pytest.mark.pure_unit


def _idx(*stamps: str) -> pd.DatetimeIndex:
    """构造 tz-naive 的 15m right-label 时间索引（trade_time = bar 结束时间）。"""
    return pd.DatetimeIndex([datetime.fromisoformat(s) for s in stamps])


def _bars(*stamps: str) -> pd.DataFrame:
    n = len(stamps)
    return pd.DataFrame(
        {
            "open": [10.0] * n,
            "high": [10.5] * n,
            "low": [9.5] * n,
            "close": [10.2] * n,
            "volume": [100.0 * (i + 1) for i in range(n)],
            "amount": [10000.0 * (i + 1) for i in range(n)],
        },
        index=_idx(*stamps),
    )


def _agg(bars: pd.DataFrame) -> SimpleNamespace:
    """模拟 MDAS BarAggregationResult 中被 Provider 使用的最小子集。"""
    return SimpleNamespace(
        bars=bars,
        source_bar_hash="hash",
        adj_factor_hash="adjhash",
        history_exhausted=False,
    )


def _cst(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 16, hour, minute, tzinfo=SHANGHAI_TZ)  # 周三


# =============================================================================
# 1) MDAS：显式 opt-in 才读实时尾部；默认行为不变
# =============================================================================


class _CacheRecorder:
    """捕获 MDAS 计算出的 cache key（key 内含最终生效的 include_realtime）。"""

    def __init__(self) -> None:
        self.keys: list[str] = []

    def get(self, key: str) -> None:
        self.keys.append(key)
        return None


async def _run_mdas_get_bars(
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeframe: str,
    completed_only: bool,
    include_realtime: bool,
    fresh_intraday_tail: bool,
    now: datetime,
) -> tuple[Any, _CacheRecorder, AsyncMock]:
    """在完全离线的环境里执行 MDAS.get_bars 并返回 (result, cache recorder, live fetch mock)。"""
    recorder = _CacheRecorder()
    monkeypatch.setattr(mdas_mod, "now_shanghai", lambda: now)
    monkeypatch.setattr(mdas_mod, "_cache_get", recorder.get)
    monkeypatch.setattr(mdas_mod, "_cache_set", lambda *a, **k: None)

    persisted = _bars("2026-09-15 14:45", "2026-09-15 15:00")
    live = _bars("2026-09-16 09:45", "2026-09-16 10:00", "2026-09-16 10:15")
    live_mock = AsyncMock(return_value=live)

    monkeypatch.setattr(
        mdas_mod,
        "_fetch_intraday_with_backfill",
        AsyncMock(return_value=(persisted, 0, False, "ok")),
    )
    monkeypatch.setattr(mdas_mod, "fetch_15min_bars", live_mock)
    monkeypatch.setattr(mdas_mod, "_get_listing_date", AsyncMock(return_value=None))

    # daily 路径（仅用于验证 fresh_intraday_tail 对 1d 不生效）
    monkeypatch.setattr(
        mdas_mod, "_query_daily_bars", AsyncMock(return_value=pd.DataFrame())
    )
    monkeypatch.setattr(
        mdas_mod,
        "_call_expected_last_completed_daily_bar",
        AsyncMock(return_value=now.date()),
    )
    monkeypatch.setattr(
        mdas_mod, "fetch_daily_bars", AsyncMock(return_value=pd.DataFrame())
    )

    result = await mdas_mod.MarketDataAggregationService().get_bars(
        session=None,  # type: ignore[arg-type]
        instrument_id=uuid.uuid4(),
        timeframe=timeframe,
        adj="none",
        include_realtime=include_realtime,
        completed_only=completed_only,
        fresh_intraday_tail=fresh_intraday_tail,
        limit=16,
    )
    return result, recorder, live_mock


async def test_mdas_default_completed_only_still_forces_no_realtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C13：既有语义不变 —— completed_only=True 仍强制 include_realtime=False。"""
    result, recorder, live_mock = await _run_mdas_get_bars(
        monkeypatch,
        timeframe="15m",
        completed_only=True,
        include_realtime=False,
        fresh_intraday_tail=False,
        now=_cst(10, 7),
    )
    live_mock.assert_not_awaited()
    # cache key 中的 include_realtime 段必须为 False
    assert ":False:True:" in recorder.keys[0]
    assert len(result.bars) == 2  # 只有 persisted


async def test_mdas_explicit_flag_enables_fresh_intraday_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式 opt-in：15m + completed_only + fresh_intraday_tail → 合并当日实时尾部。"""
    result, recorder, live_mock = await _run_mdas_get_bars(
        monkeypatch,
        timeframe="15m",
        completed_only=True,
        include_realtime=True,
        fresh_intraday_tail=True,
        now=_cst(10, 7),
    )
    live_mock.assert_awaited()
    assert ":True:True:" in recorder.keys[0]
    # persisted(2) + live(3，MDAS 不负责过滤 15m forming bar)
    assert len(result.bars) == 5
    assert pd.Timestamp("2026-09-16 10:00") in result.bars.index


async def test_mdas_flag_absent_keeps_realtime_off_even_if_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C13：没有显式 flag 时，即使传 include_realtime=True 也不读实时（保护既有 consumer）。"""
    result, recorder, live_mock = await _run_mdas_get_bars(
        monkeypatch,
        timeframe="15m",
        completed_only=True,
        include_realtime=True,
        fresh_intraday_tail=False,
        now=_cst(10, 7),
    )
    live_mock.assert_not_awaited()
    assert ":False:True:" in recorder.keys[0]
    assert len(result.bars) == 2


async def test_mdas_flag_does_not_apply_to_daily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """flag 只对日内周期有效：1d 仍强制 include_realtime=False。"""
    _, recorder, _ = await _run_mdas_get_bars(
        monkeypatch,
        timeframe="1d",
        completed_only=True,
        include_realtime=True,
        fresh_intraday_tail=True,
        now=_cst(10, 7),
    )
    assert ":False:True:" in recorder.keys[0]


# =============================================================================
# 2) NodeClusterInputProvider：15m 走 fresh 尾部 + 丢弃 forming bar
# =============================================================================


async def _run_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    now: datetime,
    live_bars: pd.DataFrame,
) -> tuple[Any, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    async def _fake_get_bars(self: Any, session: Any, instrument_id: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        if kwargs.get("timeframe") == "15m":
            return _agg(live_bars)
        return _agg(_bars("2026-09-15 15:00"))

    monkeypatch.setattr(
        provider_mod.MarketDataAggregationService, "get_bars", _fake_get_bars
    )
    monkeypatch.setattr(provider_mod, "now_shanghai", lambda: now)
    monkeypatch.setattr(
        NodeClusterInputProvider,
        "_compute_exhaustion_proofs",
        AsyncMock(return_value={"1d": (False, "x"), "15m": (False, "x")}),
    )

    node_input = await NodeClusterInputProvider.get_inputs(
        None,  # type: ignore[arg-type]
        uuid.uuid4(),
    )
    return node_input, calls


async def test_provider_15m_uses_fresh_intraday_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider 的 15m 查询必须显式开启 fresh 尾部；daily 保持 completed-only。"""
    _, calls = await _run_provider(
        monkeypatch, now=_cst(10, 7), live_bars=_bars("2026-09-15 14:45")
    )
    daily = next(c for c in calls if c["timeframe"] == "1d")
    m15 = next(c for c in calls if c["timeframe"] == "15m")

    assert daily["completed_only"] is True
    assert daily["include_realtime"] is False
    assert daily.get("fresh_intraday_tail", False) is False

    assert m15["completed_only"] is True
    assert m15["include_realtime"] is True
    assert m15["fresh_intraday_tail"] is True


async def test_provider_excludes_forming_15m_bar_at_1007(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C11：10:07 只能看到 09:45 / 10:00 两根 completed；10:00-10:15 forming 必须排除。"""
    live = _bars("2026-09-16 09:45", "2026-09-16 10:00", "2026-09-16 10:15")
    node_input, _ = await _run_provider(monkeypatch, now=_cst(10, 7), live_bars=live)

    got = list(node_input.bars_15m.index)
    assert pd.Timestamp("2026-09-16 09:45") in got
    assert pd.Timestamp("2026-09-16 10:00") in got
    assert pd.Timestamp("2026-09-16 10:15") not in got, "forming bar 不得进入 Node 输入"
    assert node_input.m15_count == 2


async def test_provider_lunch_boundary_1307_excludes_forming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C12：13:07 时 13:00-13:15 仍 forming → 排除；且无跨午休伪 bar。"""
    live = _bars("2026-09-16 11:30", "2026-09-16 13:15")
    node_input, _ = await _run_provider(monkeypatch, now=_cst(13, 7), live_bars=live)

    got = list(node_input.bars_15m.index)
    assert pd.Timestamp("2026-09-16 11:30") in got
    assert pd.Timestamp("2026-09-16 13:15") not in got, "13:00-13:15 尚未完成"
    # 11:30 → 13:15 之间不得出现任何被拼出来的连续 bar
    assert pd.Timestamp("2026-09-16 12:00") not in got


async def test_provider_lunch_boundary_1316_includes_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C12：13:16 时 13:00-13:15 已完成 → 纳入。"""
    live = _bars("2026-09-16 11:30", "2026-09-16 13:15")
    node_input, _ = await _run_provider(monkeypatch, now=_cst(13, 16), live_bars=live)

    got = list(node_input.bars_15m.index)
    assert pd.Timestamp("2026-09-16 11:30") in got
    assert pd.Timestamp("2026-09-16 13:15") in got


async def test_provider_zero_live_bars_yields_no_today_contribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实时尾部为空时不得伪造当日 bar（宁可少算）。"""
    node_input, _ = await _run_provider(
        monkeypatch, now=_cst(10, 7), live_bars=_bars("2026-09-15 15:00")
    )
    assert pd.Timestamp("2026-09-15 15:00") in node_input.bars_15m.index
    assert node_input.m15_count == 1


# =============================================================================
# 3) C7 volume 语义：每根 bar 是 interval volume，直接求和（不是累计量）
# =============================================================================


async def test_provider_passes_interval_volume_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C7：15m volume 为「每根 bar 的区间成交量」，Provider 不得做 cumulative→delta
    或任何累加变换；3 根 bar 的 volume 直接相加即总量。
    """
    live = _bars("2026-09-16 09:45", "2026-09-16 10:00")
    node_input, _ = await _run_provider(monkeypatch, now=_cst(10, 7), live_bars=live)

    # _bars 生成的 interval volume 为 100 / 200（第 1、2 根）
    assert list(node_input.bars_15m["volume"]) == [100.0, 200.0]
    assert float(node_input.bars_15m["volume"].sum()) == 300.0


# =============================================================================
# 4) C9 daily / 15m 日期对齐：daily 尚未包含 today + 15m 已包含 today
# =============================================================================


def _vp_daily(*stamps: str) -> pd.DataFrame:
    """构造价格有变化的主数据（日线），满足 VP 的 highest!=lowest 约束。"""
    n = len(stamps)
    closes = [10.0 + 0.3 * i for i in range(n)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.4 for c in closes],
            "low": [c - 0.4 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * n,
        },
        index=_idx(*stamps),
    )


def test_c9_daily_without_today_plus_today_15m_tail_is_accepted() -> None:
    """C9 保护性测试：unified_volume_profile 是否允许
    「daily owner 尚未包含 today，而 15m profile 已包含今日 completed bars」。

    结论（由本测试固定）：**允许** —— daily 与 profile_df 是相互独立的输入，
    不存在「daily 必须覆盖 today」的对齐硬约束，因此**不需要伪造今日 daily bar**。
    同时证明今日 15m 尾部确实会改变 volume profile（不是被静默丢弃）。
    """
    from app.strategy_assets.algorithms.features.unified_volume_profile import (
        compute_unified_volume_profile,
    )

    # daily 只到上一交易日（不含 today）
    daily = _vp_daily(
        "2026-09-03 15:00", "2026-09-04 15:00", "2026-09-15 15:00"
    )
    # profile 使用 15m：上一交易日尾盘 + 今日已完成两根
    tail = _bars("2026-09-15 15:00", "2026-09-16 09:45", "2026-09-16 10:00")

    without_tail = compute_unified_volume_profile(daily, profile_df=None, main_period="day")
    with_tail = compute_unified_volume_profile(daily, profile_df=tail, main_period="day")

    # 1) 两种输入都不抛错（无日期对齐硬约束）
    assert not with_tail.profile_df.empty
    # 2) 今日 15m 尾部确实进入了 profile（而不是被静默忽略）
    assert not with_tail.profile_df.equals(without_tail.profile_df)

