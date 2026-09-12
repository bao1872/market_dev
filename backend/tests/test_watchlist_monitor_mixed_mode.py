"""[MIXED-MODE] WatchlistMonitor.detect_events 新/旧路径**独立决策**契约测试。

背景（P0 生产 blocker）：
旧写法是「node_target_set 或 smc_target_set 任一存在 → 两个子系统一起进入
new-mode，然后直接 ``return events``」。production 当前只注入 ``node_target_set``、
不注入 ``smc_target_set``，于是实际执行是：

```
Node 新 crossing   ✅（node_target_set 存在）
SMC 新 crossing    ❌（smc_target_set 为 None）
直接 return
SMC 旧 fallback    ❌（永远到不了）
```

即代码注释声称的「SMC 仍走旧路径」**实际并不成立**，整条 SMC legacy 事件链
（含 ``_writeback_smc_substate``）被静默绕掉。

修复后：Node 与 SMC **各自独立**决定走新 crossing 还是 legacy：

```
有 node_target_set → Node 新 crossing；否则 → 旧 VN detect_events
有 smc_target_set  → SMC 新 crossing；否则 → 旧 SMC detect_events
```

纯单元测试（无 DB / 无网络；子 monitor 用假实现记录调用）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services.node_monitor_target_service import (
    NodeMonitorTarget,
    NodeMonitorTargetSet,
)
from app.strategy.monitors.watchlist_monitor import WatchlistMonitor
from app.strategy.runtime import MarketDataContext, MonitorState

pytestmark = pytest.mark.pure_unit

_TZ = ZoneInfo("Asia/Shanghai")
_T = datetime(2026, 9, 12, 9, 40, tzinfo=_TZ)

_LEGACY_SMC_EVENT: dict[str, Any] = {"source": "smc_legacy"}
_LEGACY_VN_EVENT: dict[str, Any] = {"source": "vn_legacy"}


def _node_set() -> NodeMonitorTargetSet:
    """price=10.5 的单一筹码目标；配合 10.0 → 11.0 构成向上穿透。"""
    return NodeMonitorTargetSet(
        target_set_version="node_v1",
        targets=(
            NodeMonitorTarget(
                target_id="target_10_5",
                kind="peak",
                price=10.5,
                source_region_ids=("r1",),
            ),
        ),
        daily_source_hash="h_d",
        m15_source_hash="h_m",
        adjustment_context_hash="h_a",
        factor_hash="h_f",
        algorithm_version="1.0",
        output_schema_version=1,
        contract_fingerprint="fp1",
        profile_hash="prof1",
        node_regions_hash="reg1",
        updated_through=pd.Timestamp.now(),
        input_availability="available",
        input_degraded_reason=None,
    )


def _smc_set() -> SimpleNamespace:
    """最小 SMC TargetSet 替身（无结构/OB 目标，只用于验证路径选择）。"""
    return SimpleNamespace(
        target_set_version="smc_v1",
        structure_context={"swing_bias": 1, "internal_bias": 1},
        active_structure_targets=(),
        active_order_block_targets=(),
    )


class _FakeSubMonitor:
    """假子 monitor：记录被调用次数并返回固定事件列表。"""

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.calls = 0
        self._events = events or []

    async def detect_events(
        self, context: Any, prev_state: Any, curr_state: Any
    ) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self._events)


def _build(
    *,
    node_target_set: Any = None,
    smc_target_set: Any = None,
) -> tuple[WatchlistMonitor, _FakeSubMonitor, _FakeSubMonitor, MarketDataContext, MonitorState]:
    inst_id, ver_id = uuid.uuid4(), uuid.uuid4()
    monitor = WatchlistMonitor()
    vn = _FakeSubMonitor([_LEGACY_VN_EVENT])
    smc = _FakeSubMonitor([_LEGACY_SMC_EVENT])
    monitor._vn = vn  # type: ignore[assignment]
    monitor._smc = smc  # type: ignore[assignment]

    context = MarketDataContext(
        instrument_id=inst_id,
        symbol="600519",
        bars_daily=pd.DataFrame(),
        bar_time=_T,
        node_target_set=node_target_set,
        smc_target_set=smc_target_set,
        current_price=11.0,
        price_last=10.0,
    )
    curr_state = MonitorState(
        instrument_id=inst_id, strategy_version_id=ver_id, state={}
    )
    return monitor, vn, smc, context, curr_state


async def test_node_new_mode_does_not_bypass_smc_legacy() -> None:
    """只注入 node_target_set（= 当前 production 形态）→ SMC legacy 仍必须执行。"""
    monitor, vn, smc, context, curr_state = _build(node_target_set=_node_set())

    events = await monitor.detect_events(context, None, curr_state)

    # Node 走新 crossing（10.0 → 11.0 向上穿透 10.5）
    node_events = [e for e in events if not isinstance(e, dict)]
    assert len(node_events) == 1
    # SMC 未注入 TargetSet → 必须回退 legacy，不能被绕过
    assert smc.calls == 1, "SMC legacy 路径被绕掉了（mixed-mode bug）"
    assert _LEGACY_SMC_EVENT in events
    # Node 已进入新路径 → 不应再跑旧 VN
    assert vn.calls == 0


async def test_both_target_sets_skip_both_legacy_paths() -> None:
    """两个 TargetSet 都注入 → 两条 legacy 都不执行。"""
    monitor, vn, smc, context, curr_state = _build(
        node_target_set=_node_set(), smc_target_set=_smc_set()
    )

    await monitor.detect_events(context, None, curr_state)

    assert vn.calls == 0
    assert smc.calls == 0


async def test_no_target_set_runs_both_legacy_paths() -> None:
    """两个 TargetSet 都缺失 → 两条 legacy 都执行（既有行为保持不变）。"""
    monitor, vn, smc, context, curr_state = _build()

    events = await monitor.detect_events(context, None, curr_state)

    assert vn.calls == 1
    assert smc.calls == 1
    assert _LEGACY_VN_EVENT in events
    assert _LEGACY_SMC_EVENT in events


async def test_smc_only_skips_vn_legacy_but_keeps_smc_new_mode() -> None:
    """只注入 smc_target_set → SMC 新路径 + VN legacy 共存。"""
    monitor, vn, smc, context, curr_state = _build(smc_target_set=_smc_set())

    await monitor.detect_events(context, None, curr_state)

    assert vn.calls == 1
    assert smc.calls == 0
