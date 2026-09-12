"""[WATCHLIST-REALTIME-MONITOR] 盘中自选股实时极速监控主干测试（Stage G6）。

覆盖契约：
1. 完整监控周期（Monitor Cycle）：
   - G3: 批量行情拉取（SH/SZ 走 pytdx，BJ 走 Eastmoney 备用）；
   - G4: 筹码共识区（VolumeNode）穿透事件准确触发；
   - G5: SMC 结构突破（BOS / CHoCH）与订单块首次触碰（OB First Touch）准确触发；
2. 一次性触发与状态滚动（One-shot Lifecycle）：
   - 首轮触发后，target_id 写入 updated_states["triggered_target_ids"]；
   - 次轮轮询即便价格持续处于触发区间，绝不重复报警；
3. pytdx 故障平滑降级：
   - pytdx 异常时自动降级走 Eastmoney 快照，保证监控不中断。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.core.pytdx_adapter import PytdxCallProvenance, PytdxSourceError
from app.models.instrument import Instrument
from app.services.eod_market_snapshot_provider import EodSnapshotRow
from app.services.node_monitor_target_service import (
    NodeMonitorTarget,
    NodeMonitorTargetSet,
)
from app.services.realtime_market_snapshot_provider import RealtimeMarketSnapshot
from app.services.smc_monitor_target_service import (
    SmcMonitorTargetSet,
    SmcOrderBlockTarget,
    SmcStructureTarget,
)
from app.services.watchlist_realtime_monitor_service import (
    WatchlistRealtimeMonitorService,
)
from app.strategy.monitors.smc_monitor import (
    SMC_BOS_RETEST,
    SMC_ORDER_BLOCK_FIRST_TOUCH,
)
from app.strategy.monitors.volume_node_monitor import EVENT_TYPE_NODE_CLUSTER_TOUCH

_SH_TZ = ZoneInfo("Asia/Shanghai")


def _make_node_target_set(price: float) -> NodeMonitorTargetSet:
    return NodeMonitorTargetSet(
        target_set_version="node_v1",
        targets=(
            NodeMonitorTarget(
                target_id=f"target_node_{price}",
                kind="peak",
                price=price,
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


def _make_smc_target_set(
    struct_level: float,
    ob_low: float,
    ob_high: float,
) -> SmcMonitorTargetSet:
    return SmcMonitorTargetSet(
        contract_identity={"algorithm_id": "smc"},
        input_identity={"daily_bars_hash": "h_d"},
        structure_context={"swing_bias": 1, "internal_bias": 1, "slots": {}},
        active_structure_targets=[
            SmcStructureTarget(
                target_id=f"smc_high_{struct_level}",
                lane="swing",
                kind="high",
                level=struct_level,
                anchor_index=5,
                anchor_time="2026-09-01",
            )
        ],
        active_order_block_targets=[
            SmcOrderBlockTarget(
                target_id=f"smc_ob_{ob_low}_{ob_high}",
                internal=False,
                bias=1,
                bar_low=ob_low,
                bar_high=ob_high,
                anchor_index=10,
                anchor_time="2026-09-02",
                confirmed_index=11,
                confirmed_time="2026-09-03",
            )
        ],
        target_set_version="smc_v1",
    )


@pytest.mark.asyncio
async def test_watchlist_monitor_cycle_full_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import realtime_market_fact_service as fact_mod

    inst_sh = Instrument(id=uuid.uuid4(), symbol="600519", name="贵州茅台", market="SH")
    inst_bj = Instrument(id=uuid.uuid4(), symbol="920001", name="北交龙头", market="BJ")

    now = datetime.now(_SH_TZ)

    # 1. Mock pytdx adapter（为 SH 股提供 quote）
    class MockPytdxAdapter:
        def get_security_quotes_with_provenance(
            self, symbols: list[str]
        ) -> tuple[list[dict[str, Any]], PytdxCallProvenance]:
            rows = [
                {
                    "code": "600519",
                    "price": 105.0,  # 上穿目标价 104.0
                    "last_close": 100.0,
                    "open": 101.0,
                    "high": 106.0,
                    "low": 100.0,
                    "vol": 500,
                    "amount": 5250000.0,
                }
            ]
            return rows, PytdxCallProvenance(server=("127.0.0.1", 7709), connection_generation=1)

    # 2. Mock Eastmoney（为 BJ 股提供 quote，落入 OB 15.0~16.0）
    fake_snapshot = RealtimeMarketSnapshot(
        source_host="push2.eastmoney.com",
        captured_at=now,
        market_watermark=now,
        rows=(
            EodSnapshotRow(
                symbol="920001",
                name="北交龙头",
                market="BJ",
                updated_at=now,
                open=Decimal("15.0"),
                high=Decimal("15.8"),
                low=Decimal("14.9"),
                close=Decimal("15.5"),  # 进入 OB [15.0, 16.0]
                previous_close=Decimal("15.0"),
                volume=Decimal("10000"),
                amount=Decimal("155000"),
            ),
        ),
        raw_count=1,
        normalized_count=1,
    )
    monkeypatch.setattr(
        fact_mod, "fetch_realtime_a_share_snapshot", AsyncMock(return_value=fake_snapshot)
    )

    service = WatchlistRealtimeMonitorService()

    # Target sets:
    # 600519: Node 104.0 (待向上穿透); SMC BOS High 103.0 (待向上穿透)
    # 920001: OB [15.0, 16.0] (待首次触碰)
    node_set_600519 = _make_node_target_set(104.0)
    smc_set_600519 = _make_smc_target_set(103.0, 95.0, 96.0)
    smc_set_920001 = _make_smc_target_set(20.0, 15.0, 16.0)

    target_sets = {
        inst_sh.id: (node_set_600519, smc_set_600519),
        inst_bj.id: (None, smc_set_920001),
    }

    # 前次状态设置初始价格低于目标位（模拟前次价格为 100.0 和 14.5）
    prev_states = {
        inst_sh.id: {"current_price": 100.0, "triggered_target_ids": []},
        inst_bj.id: {"current_price": 14.5, "triggered_target_ids": []},
    }
    service.fact_service.price_tracker.set_last_price("600519", 100.0)
    service.fact_service.price_tracker.set_last_price("920001", 14.5)

    # --- 第 1 轮监控 ---
    result1 = await service.run_monitor_cycle(
        [inst_sh, inst_bj],
        target_sets,
        prev_states,
        adapter=MockPytdxAdapter(),  # type: ignore[arg-type]
        now=now,
    )

    assert result1.total_instruments == 2
    assert result1.quotes_fetched == 2

    # 事件断言：
    # 600519 触发 node_cluster_touch (向上穿透 104.0)
    # 600519 触发 smc_bos_retest (顺势突破 103.0)
    # 920001 触发 smc_order_block_first_touch (首次触碰 15.0~16.0)
    event_types = [e.event_type for e in result1.events_detected]
    assert EVENT_TYPE_NODE_CLUSTER_TOUCH in event_types
    assert SMC_BOS_RETEST in event_types
    assert SMC_ORDER_BLOCK_FIRST_TOUCH in event_types
    assert len(result1.events_detected) == 3

    # 检查状态已正确记录 triggered_target_ids
    state_sh = result1.updated_states[inst_sh.id]
    assert "target_node_104.0" in state_sh["triggered_target_ids"]
    assert "smc_high_103.0" in state_sh["triggered_target_ids"]
    assert state_sh["current_price"] == 105.0

    state_bj = result1.updated_states[inst_bj.id]
    assert "smc_ob_15.0_16.0" in state_bj["triggered_target_ids"]

    # --- 第 2 轮监控（One-Shot 契约验证）：价格维持或继续上涨，已触发 target 绝不重复触发 ---
    result2 = await service.run_monitor_cycle(
        [inst_sh, inst_bj],
        target_sets,
        result1.updated_states,  # 传入第 1 轮更新后的状态
        adapter=MockPytdxAdapter(),  # type: ignore[arg-type]
        now=now,
    )
    assert len(result2.events_detected) == 0, "一次性事件在同一 TargetSet 生命期内绝不得重复触发"


@pytest.mark.asyncio
async def test_watchlist_monitor_cycle_pytdx_failover(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import realtime_market_fact_service as fact_mod

    inst_sh = Instrument(id=uuid.uuid4(), symbol="600519", name="贵州茅台", market="SH")
    now = datetime.now(_SH_TZ)

    # pytdx 抛异常模拟故障
    class FailingAdapter:
        def get_security_quotes_with_provenance(self, symbols: list[str]) -> Any:
            raise PytdxSourceError(operation="get_security_quotes", message="network unreachable")

    # Eastmoney 兜底返回有效快照
    fake_snapshot = RealtimeMarketSnapshot(
        source_host="push2.eastmoney.com",
        captured_at=now,
        market_watermark=now,
        rows=(
            EodSnapshotRow(
                symbol="600519",
                name="贵州茅台",
                market="SH",
                updated_at=now,
                open=Decimal("100.0"),
                high=Decimal("106.0"),
                low=Decimal("99.0"),
                close=Decimal("105.0"),
                previous_close=Decimal("100.0"),
                volume=Decimal("50000"),
                amount=Decimal("5250000"),
            ),
        ),
        raw_count=1,
        normalized_count=1,
    )
    fake_fetch = AsyncMock(return_value=fake_snapshot)
    monkeypatch.setattr(fact_mod, "fetch_realtime_a_share_snapshot", fake_fetch)

    service = WatchlistRealtimeMonitorService()
    service.fact_service.price_tracker.set_last_price("600519", 100.0)

    node_set = _make_node_target_set(104.0)
    target_sets = {inst_sh.id: (node_set, None)}
    prev_states = {inst_sh.id: {"current_price": 100.0, "triggered_target_ids": []}}

    result = await service.run_monitor_cycle(
        [inst_sh],
        target_sets,
        prev_states,
        adapter=FailingAdapter(),  # type: ignore[arg-type]
        now=now,
    )

    assert result.quotes_fetched == 1
    assert len(result.events_detected) == 1
    assert result.events_detected[0].event_type == EVENT_TYPE_NODE_CLUSTER_TOUCH
    fake_fetch.assert_awaited_once()
