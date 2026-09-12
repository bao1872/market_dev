"""[G7 生产生命周期] MonitorBatchService 连续快照价格区间 [P_last, P_curr] 契约测试。

背景（P0 缺陷）：
修复前生产 ``_process_instrument_evaluation`` 先调用 ``price_tracker.update_price()``、
**之后**才读取 ``prev_state``，导致：

- 进程重启：内存 PriceTracker 为空 → 首帧恒为 ``(p, p)``，持久化的 ``current_price``
  永远拿不到 → 重启窗口期的 catch-up 穿透被漏掉；
- XDXR / Node TargetSet version roll：``detect_events`` 只重置了 ``triggered_ids``，
  没重置 ``p_last`` → 拿旧复权坐标的 P_last 去扫描新坐标 TargetSet，制造假穿透。

修复后：``prev_state`` 先读，生命周期判定下沉为唯一 owner
``resolve_snapshot_price_range``，生产与旁路共用同一套规则。

本文件为纯单元测试（mock db / provider，无网络、无 DB）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services import monitor_batch_service as mbs
from app.services.monitor_batch_service import MonitorBatchService
from app.services.realtime_market_fact_service import (
    PriceTracker,
    resolve_snapshot_price_range,
)

pytestmark = pytest.mark.pure_unit

_SH_TZ = ZoneInfo("Asia/Shanghai")
_SYMBOL = "600519"
_NODE_V1 = "node_v1"
_NODE_V2 = "node_v2"


# ── helpers ────────────────────────────────────────────────────────────
def _minute_df(closes: list[float]) -> pd.DataFrame:
    """构造 1m bar DataFrame（生产会剔除最后一根未完成 bar）。"""
    rows = [
        {
            "open": c,
            "high": c + 0.05,
            "low": c - 0.05,
            "close": c,
            "volume": 1000.0,
            "amount": 1000.0 * c,
            "adj_factor": 1.0,
        }
        for c in closes
    ]
    df = pd.DataFrame(rows)
    base = datetime.now(_SH_TZ).replace(hour=9, minute=30, second=0, microsecond=0)
    df.index = pd.DatetimeIndex(
        [base.replace(minute=30 + i) for i in range(len(closes))]
    )
    df.index.name = "trade_time"
    return df


def _daily_df(count: int = 30) -> pd.DataFrame:
    rows = [
        {
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.0,
            "volume": 10000.0,
            "amount": 100000.0,
            "adj_factor": 1.0,
        }
        for _ in range(count)
    ]
    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex(
        [pd.Timestamp(datetime.now(UTC).date()) - pd.Timedelta(days=i) for i in range(count)]
    )
    df.index.name = "trade_date"
    return df


def _target_set(version: str) -> SimpleNamespace:
    return SimpleNamespace(target_set_version=version, targets=())


async def _drive_cycle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bar_closes: list[float],
    prev_state_payload: dict[str, Any] | None,
    curr_node_version: str = _NODE_V1,
) -> dict[str, Any]:
    """驱动生产 _process_instrument_evaluation，捕获注入到 context 的价格区间。

    返回 dict(context=..., service=...)。通过在 calculate_state 处抛错提前终止，
    避免 mock 事件写入 / 通知等下游（本测试只关心价格生命周期）。
    """
    captured: dict[str, Any] = {}

    daily = _daily_df()

    monkeypatch.setattr(
        MonitorBatchService,
        "_get_instrument_info",
        AsyncMock(return_value=(_SYMBOL, "测试标的")),
    )
    monkeypatch.setattr(
        MonitorBatchService,
        "_fetch_md_bars_with_meta",
        AsyncMock(return_value=(_minute_df(bar_closes), "db", False)),
    )
    monkeypatch.setattr(
        mbs.NodeClusterInputProvider,
        "get_inputs",
        AsyncMock(return_value=SimpleNamespace(daily_bars=daily, bars_15m=pd.DataFrame())),
    )
    monkeypatch.setattr(MonitorBatchService, "update_heartbeat", AsyncMock())
    monkeypatch.setattr(
        MonitorBatchService,
        "_compute_node_cluster_profile",
        AsyncMock(return_value=SimpleNamespace(profile="p")),
    )
    monkeypatch.setattr(
        mbs.NodeMonitorTargetService,
        "build_target_set",
        lambda node_input, profile: _target_set(curr_node_version),
    )
    monkeypatch.setattr(
        MonitorBatchService, "_mark_evaluation_failed", AsyncMock()
    )

    # prev_state：生产必须**先**读取，再更新 PriceTracker
    monkeypatch.setattr(
        mbs.monitor_state_repository,
        "get_state",
        AsyncMock(return_value=object() if prev_state_payload is not None else None),
    )
    monkeypatch.setattr(
        MonitorBatchService,
        "_orm_to_runtime_state",
        lambda self, orm: (
            SimpleNamespace(state=dict(prev_state_payload))
            if prev_state_payload is not None and orm is not None
            else None
        ),
    )

    class _StopEarlyError(Exception):
        """用于在本测试关心的断言点之后提前终止（避免 mock 事件写入等下游）。"""

    class _FakeMonitor:
        def __init__(self) -> None:
            pass

        async def initialize(self, strategy_version: Any) -> None:
            return None

        async def calculate_state(self, context: Any) -> Any:
            captured["context"] = context
            raise _StopEarlyError("stop after price injection")

        async def detect_events(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

    monkeypatch.setattr(mbs, "WatchlistMonitor", _FakeMonitor)

    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=uuid.uuid4()))
    )
    db.flush = AsyncMock()

    service = MonitorBatchService()
    result = mbs.MonitorCycleResult()
    strategy_version = SimpleNamespace(id=uuid.uuid4())

    await service._process_instrument_evaluation(
        db, uuid.uuid4(), strategy_version, result
    )

    captured["service"] = service
    return captured


# ── 1. 共享 owner 语义（生产与旁路共用的唯一规则）──────────────────────
def test_owner_restart_bootstrap_uses_persisted_price() -> None:
    """重启（内存无价）+ 同版本 → 用持久化 current_price 作为 P_last。"""
    tracker = PriceTracker()

    p_last, p_curr = resolve_snapshot_price_range(
        tracker,
        _SYMBOL,
        105.0,
        prev_node_version=_NODE_V1,
        curr_node_version=_NODE_V1,
        persisted_price=100.0,
    )

    assert (p_last, p_curr) == (100.0, 105.0)
    assert tracker.get_last_price(_SYMBOL) == 105.0


def test_owner_version_roll_forces_degenerate_range() -> None:
    """Node TargetSet version roll → 第一帧强制 (p, p)，禁止跨复权坐标断层。"""
    tracker = PriceTracker()
    tracker.set_last_price(_SYMBOL, 100.0)  # 除权前旧坐标

    p_last, p_curr = resolve_snapshot_price_range(
        tracker,
        _SYMBOL,
        80.0,
        prev_node_version=_NODE_V1,
        curr_node_version=_NODE_V2,
        persisted_price=100.0,
    )

    assert (p_last, p_curr) == (80.0, 80.0)
    assert tracker.get_last_price(_SYMBOL) == 80.0


def test_owner_normal_advance() -> None:
    """正常运行：连续推进快照区间。"""
    tracker = PriceTracker()
    tracker.set_last_price(_SYMBOL, 100.0)

    p_last, p_curr = resolve_snapshot_price_range(
        tracker,
        _SYMBOL,
        103.0,
        prev_node_version=_NODE_V1,
        curr_node_version=_NODE_V1,
        persisted_price=99.0,  # 内存中已有价 → 忽略持久化价
    )

    assert (p_last, p_curr) == (100.0, 103.0)


def test_owner_garbage_persisted_price_fails_safe() -> None:
    """持久化价不可解析 → 退化为 (p, p)，绝不产生假区间。"""
    tracker = PriceTracker()

    p_last, p_curr = resolve_snapshot_price_range(
        tracker,
        _SYMBOL,
        105.0,
        prev_node_version=_NODE_V1,
        curr_node_version=_NODE_V1,
        persisted_price="not-a-number",
    )

    assert (p_last, p_curr) == (105.0, 105.0)


# ── 2. 生产路径（P0：prev_state 必须先于 PriceTracker 更新）──────────────
async def test_production_restart_bootstrap_restores_persisted_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产重启：持久化 100 + 当前 bar 105 → 区间必须是 (100, 105)，不是 (105, 105)。"""
    out = await _drive_cycle(
        monkeypatch,
        bar_closes=[105.0, 106.0],  # 剔除最后一根 → 105
        prev_state_payload={
            "current_price": 100.0,
            "node_target_set_version": _NODE_V1,
        },
        curr_node_version=_NODE_V1,
    )

    ctx = out["context"]
    assert ctx.current_price == 105.0
    assert ctx.price_last == 100.0  # ← P0：修复前这里是 105.0（漏掉 catch-up 穿透）


async def test_production_node_version_roll_resets_price_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 XDXR / version roll：新坐标 80 → 区间必须是 (80, 80)，不是 (100, 80)。"""
    out = await _drive_cycle(
        monkeypatch,
        bar_closes=[80.0, 81.0],  # 剔除最后一根 → 80
        prev_state_payload={
            "current_price": 100.0,  # 除权前旧坐标
            "node_target_set_version": _NODE_V1,
        },
        curr_node_version=_NODE_V2,  # ← version roll
    )

    ctx = out["context"]
    assert ctx.current_price == 80.0
    assert ctx.price_last == 80.0  # ← P0：修复前这里是 100.0（假穿透）
    # tracker 也必须同步到新坐标，否则下一帧仍跨断层
    assert out["service"].fact_service.price_tracker.get_last_price(_SYMBOL) == 80.0


async def test_production_first_run_without_prev_state_is_degenerate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次执行（无 prev_state）→ (p, p)，避免由 0 跃迁触发伪穿透。"""
    out = await _drive_cycle(
        monkeypatch,
        bar_closes=[50.0, 51.0],
        prev_state_payload=None,
    )

    ctx = out["context"]
    assert ctx.current_price == 50.0
    assert ctx.price_last == 50.0
