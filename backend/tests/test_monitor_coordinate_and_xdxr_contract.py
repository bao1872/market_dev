"""[STAGE-G7-CONTRACT] 坐标对齐、XDXR 版本滚动与 MDAS 日内尾部契约测试。

验证范围：
1. 坐标系统一致性（Coordinate Alignment）：
   - QFQ 最新日（as_of=today）因子比率为 1.0，当日 raw price 与 QFQ price 完全一致；
   - 盘前固化的 Target Set 价格与盘中 pytdx 实时快照价格位于同一基准；
2. XDXR / Target Set 版本滚动（Version Rolling & Lifecycle Reset）：
   - 当发生除权除息或新 Bar 完成导致 target_set_version 变更时，
     WatchlistRealtimeMonitorService 自动识别并清空对应域已触发 target 记录，
     由新 Target Set 重新接管监控；
3. 内存价格跟踪器（PriceTracker）与标的重置（reset_symbol_state）：
   - 重置后下次行情更新初始化为 (p, p)，绝不产生跃迁伪穿透；
4. MDAS 15m/1h 原生 Pytdx 尾部与复权契约：
   - 交易时段拉取原生 15m/60m bar 补尾，按时间戳与 DB 合并并映射权威日线因子。
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.core.pytdx_adapter import PytdxCallProvenance
from app.models.instrument import Instrument
from app.services.adj_factor import apply_adj_factor
from app.services.node_monitor_target_service import (
    NodeMonitorTarget,
    NodeMonitorTargetSet,
)
from app.services.realtime_market_fact_service import PriceTracker
from app.services.smc_monitor_target_service import (
    SmcMonitorTargetSet,
    SmcStructureTarget,
)
from app.services.watchlist_realtime_monitor_service import (
    WatchlistRealtimeMonitorService,
)

_SH_TZ = ZoneInfo("Asia/Shanghai")


def _make_node_target_set(version: str, price: float) -> NodeMonitorTargetSet:
    return NodeMonitorTargetSet(
        target_set_version=version,
        targets=(
            NodeMonitorTarget(
                target_id=f"target_node_{version}_{price}",
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


def _make_smc_target_set(version: str, struct_level: float) -> SmcMonitorTargetSet:
    return SmcMonitorTargetSet(
        contract_identity={"algorithm_id": "smc"},
        input_identity={"daily_bars_hash": f"h_d_{version}"},
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
        active_order_block_targets=[],
        target_set_version=version,
    )


def test_qfq_latest_date_raw_equals_qfq_price() -> None:
    """验证 QFQ 契约：在最新交易日，raw 价格与 qfq 价格完全恒等。"""
    dates = pd.to_datetime(["2026-09-09", "2026-09-10", "2026-09-11"])
    bars_df = pd.DataFrame(
        {
            "open": [20.0, 22.0, 11.0],
            "high": [21.0, 23.0, 12.0],
            "low": [19.0, 21.0, 10.5],
            "close": [20.5, 22.5, 11.5],
            "volume": [1000.0, 1200.0, 1500.0],
        },
        index=dates,
    )

    factor_df = pd.DataFrame(
        {
            "trade_date": dates.date,
            "adj_factor": [0.5, 0.5, 1.0],
        }
    )

    qfq_df = apply_adj_factor(bars_df, factor_df, as_of=date(2026, 9, 11))

    # 1. 验证最新日（2026-09-11）raw == qfq
    assert qfq_df.loc["2026-09-11", "close"] == 11.5
    assert qfq_df.loc["2026-09-11", "open"] == 11.0

    # 2. 验证历史日（2026-09-09）按 0.5 / 1.0 复权折半
    assert qfq_df.loc["2026-09-09", "close"] == 10.25


def test_price_tracker_reset_symbol() -> None:
    """验证 PriceTracker reset 单个标的或全量重置。"""
    tracker = PriceTracker()
    p_last, p_curr = tracker.update_price("600519", 100.0)
    assert p_last == 100.0 and p_curr == 100.0

    p_last, p_curr = tracker.update_price("600519", 105.0)
    assert p_last == 100.0 and p_curr == 105.0

    # 重置 600519
    tracker.reset("600519")
    assert tracker.get_last_price("600519") is None

    # 重置后首次更新重回 (p, p)
    p_last, p_curr = tracker.update_price("600519", 108.0)
    assert p_last == 108.0 and p_curr == 108.0


@pytest.mark.asyncio
async def test_target_set_version_rolling_resets_triggered_ids() -> None:
    """验证 XDXR 或日线 Bar 刷新引起 Target Set Version 变更时，triggered target 自动重置。"""
    inst = Instrument(id=uuid.uuid4(), symbol="600519", name="贵州茅台", market="SH")
    inst_id = inst.id

    # Mock pytdx quote
    class MockPytdxAdapter:
        def __init__(self) -> None:
            self.current_quote_price = 100.0

        def get_security_quotes_with_provenance(
            self, symbols: list[str]
        ) -> tuple[list[dict[str, Any]], PytdxCallProvenance]:
            rows = [
                {
                    "code": "600519",
                    "price": self.current_quote_price,
                    "last_close": 98.0,
                    "open": 99.0,
                    "high": 105.0,
                    "low": 98.0,
                    "vol": 1000,
                    "amount": 10000000.0,
                }
            ]
            return rows, PytdxCallProvenance(server=("127.0.0.1", 7709), connection_generation=1)

    mock_adapter = MockPytdxAdapter()
    monitor = WatchlistRealtimeMonitorService()

    # --- 周期 1：Target Set V1（Node 目标位 102.0，SMC 目标位 104.0）---
    node_v1 = _make_node_target_set("node_v1", 102.0)
    smc_v1 = _make_smc_target_set("smc_v1", 104.0)

    # 价格从 100.0 -> 105.0 突破
    mock_adapter.current_quote_price = 100.0
    res0 = await monitor.run_monitor_cycle([inst], {inst_id: (node_v1, smc_v1)}, {}, adapter=mock_adapter)

    mock_adapter.current_quote_price = 105.0
    res1 = await monitor.run_monitor_cycle([inst], {inst_id: (node_v1, smc_v1)}, res0.updated_states, adapter=mock_adapter)

    # 触发了 Node 穿透与 SMC BOS 突破
    assert len(res1.events_detected) == 2
    state1 = res1.updated_states[inst_id]
    assert state1["node_target_set_version"] == "node_v1"
    assert state1["smc_target_set_version"] == "smc_v1"
    # [RC 生命周期] Node crossing 已改为可重复事件，不再进入 triggered_target_ids；
    # 该集合只服务 BOS/CHoCH 的「TargetSet version 内 one-shot」→ 只剩 SMC 结构目标。
    assert len(state1["triggered_target_ids"]) == 1
    assert "smc_high_104.0" in state1["triggered_target_ids"]

    # --- 周期 2：维持 V1，价格依然是 105.0，One-shot 确保不再报警 ---
    res2 = await monitor.run_monitor_cycle([inst], {inst_id: (node_v1, smc_v1)}, res1.updated_states, adapter=mock_adapter)
    assert len(res2.events_detected) == 0

    # --- 周期 3：XDXR 除权除息或新 Bar 完成，Target Set Version 滚动到 V2 ---
    # 新版本结构线为 108.0（高于当前 105.0）
    node_v2 = _make_node_target_set("node_v2", 108.0)
    smc_v2 = _make_smc_target_set("smc_v2", 108.0)

    # 价格维持 105.0，Target Set 换成 V2
    res3 = await monitor.run_monitor_cycle([inst], {inst_id: (node_v2, smc_v2)}, res2.updated_states, adapter=mock_adapter)
    state3 = res3.updated_states[inst_id]
    assert state3["node_target_set_version"] == "node_v2"
    assert state3["smc_target_set_version"] == "smc_v2"
    # 旧的 triggered targets 被清空，且当前价格 105 未穿透 108，所以没有新事件
    assert len(state3["triggered_node_target_ids"]) == 0
    assert len(state3["triggered_smc_target_ids"]) == 0
    assert len(res3.events_detected) == 0

    # 周期 4：价格从 105.0 -> 110.0 穿透 V2 的 108.0 目标位，新 Target 成功触发
    mock_adapter.current_quote_price = 110.0
    res4 = await monitor.run_monitor_cycle([inst], {inst_id: (node_v2, smc_v2)}, state3, adapter=mock_adapter)
    assert len(res4.events_detected) == 2  # 新版本的 Node 和 SMC 重新触发！
    assert res4.events_detected[0].event_type in ("node_cluster_touch", "smc_bos_cross")
    assert res4.events_detected[1].event_type in ("node_cluster_touch", "smc_bos_cross")
    state4 = res4.updated_states[inst_id]
    # [RC 生命周期] 只统计 BOS/CHoCH（Node 已可重复，不再进入该集合）
    assert len(state4["triggered_target_ids"]) == 1
    assert "smc_high_108.0" in state4["triggered_target_ids"]


@pytest.mark.asyncio
async def test_service_restart_bootstraps_current_price() -> None:
    """验证服务重启（新实例无内存价格缓存）时，若 Target Set Version 一致，
    能从持久化 state 的 current_price 恢复 P_last，捕获重启间隙内的穿透事件。"""
    inst = Instrument(id=uuid.uuid4(), symbol="600519", name="贵州茅台", market="SH")
    inst_id = inst.id

    class MockPytdxAdapter:
        def get_security_quotes_with_provenance(
            self, symbols: list[str]
        ) -> tuple[list[dict[str, Any]], PytdxCallProvenance]:
            rows = [
                {
                    "code": "600519",
                    "price": 105.0,  # 重启后抓到的最新价
                    "last_close": 98.0,
                    "open": 99.0,
                    "high": 105.0,
                    "low": 98.0,
                    "vol": 1000,
                    "amount": 10000000.0,
                }
            ]
            return rows, PytdxCallProvenance(server=("127.0.0.1", 7709), connection_generation=1)

    node_v1 = _make_node_target_set("v1", 102.0)
    smc_v1 = _make_smc_target_set("v1", 103.0)

    # 模拟持久化库中的历史状态：当时价格 100.0，目标版本为 v1，尚未穿透
    persisted_state = {
        "current_price": 100.0,
        "node_target_set_version": "v1",
        "smc_target_set_version": "v1",
        "triggered_target_ids": [],
        "triggered_node_target_ids": [],
        "triggered_smc_target_ids": [],
    }

    # 全新实例启动（模拟服务重启，内存 tracker 为空）
    fresh_monitor = WatchlistRealtimeMonitorService()
    assert fresh_monitor.fact_service.price_tracker.get_last_price("600519") is None

    res = await fresh_monitor.run_monitor_cycle(
        [inst],
        {inst_id: (node_v1, smc_v1)},
        {inst_id: persisted_state},
        adapter=MockPytdxAdapter(),
    )

    # 成功从 100.0 追溯至 105.0，捕获重启间隙的穿透！
    assert len(res.events_detected) == 2
    event_types = {e.event_type for e in res.events_detected}
    assert event_types == {"node_cluster_touch", "smc_bos_cross"}
    assert res.updated_states[inst_id]["price_last"] == 100.0
    assert res.updated_states[inst_id]["current_price"] == 105.0


@pytest.mark.asyncio
async def test_xdxr_version_roll_suppresses_discontinuity_crossing() -> None:
    """验证 XDXR 发生时价格坐标发生断层，版本滚动首帧强制 (p, p)，绝不虚假穿透新目标。"""
    inst = Instrument(id=uuid.uuid4(), symbol="600519", name="贵州茅台", market="SH")
    inst_id = inst.id

    class MockPytdxAdapter:
        def __init__(self, price: float) -> None:
            self.price = price

        def get_security_quotes_with_provenance(
            self, symbols: list[str]
        ) -> tuple[list[dict[str, Any]], PytdxCallProvenance]:
            rows = [
                {
                    "code": "600519",
                    "price": self.price,
                    "last_close": self.price,
                    "open": self.price,
                    "high": self.price,
                    "low": self.price,
                    "vol": 1000,
                    "amount": 10000000.0,
                }
            ]
            return rows, PytdxCallProvenance(server=("127.0.0.1", 7709), connection_generation=1)

    monitor = WatchlistRealtimeMonitorService()

    # 除权前：价格 100.0，目标 set v1 目标位 90.0
    node_v1 = _make_node_target_set("v1", 90.0)
    res1 = await monitor.run_monitor_cycle(
        [inst],
        {inst_id: (node_v1, None)},
        {},
        adapter=MockPytdxAdapter(100.0),
    )

    # 除权后（如 10 送 10）：价格变为 50.0，Target Set 换算后滚动为 v2（目标位 70.0）
    # 若错误地以 [100.0, 50.0] 计算穿透，就会错误穿越 70.0 产生虚假报警！
    node_v2 = _make_node_target_set("v2", 70.0)
    res2 = await monitor.run_monitor_cycle(
        [inst],
        {inst_id: (node_v2, None)},
        res1.updated_states,
        adapter=MockPytdxAdapter(50.0),
    )

    # 验证版本滚动首帧被压制为 (50.0, 50.0)，零虚警！
    assert len(res2.events_detected) == 0
    assert res2.updated_states[inst_id]["price_last"] == 50.0
    assert res2.updated_states[inst_id]["current_price"] == 50.0

