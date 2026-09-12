"""[MONITOR-CROSSING] 盘中监控穿透判定与一次性事件引擎单元测试。

覆盖契约：
1. 筹码共识（Node）穿透：
   - 向上穿透 [P_last < target <= P_curr] 触发 node_cluster_touch (UP)；
   - 向下穿透 [P_last > target >= P_curr] 触发 node_cluster_touch (DOWN)；
   - 未穿透不触发；
   - 一次性保证：触发后 target_id 记入已触发集合，再次经过绝不重复报警。
2. SMC 结构（BOS / CHoCH）穿透：
   - 顺势突破（high + bias=1 或 low + bias=-1）触发 BOS；
   - 逆势反转（high + bias=-1 或 low + bias=1）触发 CHoCH；
   - 穿越即发射，触发后标记已触发，无 retest 逻辑。
3. SMC Order Block First Touch：
   - 价格落入或进入 [bar_low, bar_high] 触发 smc_order_block_first_touch；
   - 首次进入后立即标记，后续停留在 OB 内绝不重复触发。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from app.services.monitor_crossing_service import (
    evaluate_node_crossings,
    evaluate_smc_events,
)
from app.services.node_monitor_target_service import (
    NodeMonitorTarget,
    NodeMonitorTargetSet,
)
from app.services.smc_monitor_target_service import (
    SmcMonitorTargetSet,
    SmcOrderBlockTarget,
    SmcStructureTarget,
)
from app.strategy.monitors.smc_monitor import (
    SMC_BOS_RETEST,
    SMC_CHOCH_RETEST,
    SMC_ORDER_BLOCK_FIRST_TOUCH,
)
from app.strategy.monitors.volume_node_monitor import EVENT_TYPE_NODE_CLUSTER_TOUCH

_SH_TZ = ZoneInfo("Asia/Shanghai")


def _make_node_target_set(targets: list[tuple[str, float]]) -> NodeMonitorTargetSet:
    node_targets = tuple(
        NodeMonitorTarget(
            target_id=tid,
            kind="peak",
            price=p,
            source_region_ids=("reg_1",),
        )
        for tid, p in targets
    )
    return NodeMonitorTargetSet(
        target_set_version="version_node_001",
        targets=node_targets,
        daily_source_hash="h_daily",
        m15_source_hash="h_m15",
        adjustment_context_hash="h_adj",
        factor_hash="h_fac",
        algorithm_version="1.0.0",
        output_schema_version=1,
        contract_fingerprint="fp_01",
        profile_hash="prof_01",
        node_regions_hash="reg_01",
        updated_through=pd.Timestamp.now(),
        input_availability="available",
        input_degraded_reason=None,
    )


def _make_smc_target_set(
    structures: list[SmcStructureTarget],
    obs: list[SmcOrderBlockTarget],
    *,
    swing_bias: int = 1,
    internal_bias: int = 1,
) -> SmcMonitorTargetSet:
    return SmcMonitorTargetSet(
        contract_identity={"algorithm_id": "smc"},
        input_identity={"daily_bars_hash": "h_daily"},
        structure_context={
            "swing_bias": swing_bias,
            "internal_bias": internal_bias,
            "slots": {},
        },
        active_structure_targets=structures,
        active_order_block_targets=obs,
        target_set_version="version_smc_001",
    )


def test_node_crossing_up_and_down() -> None:
    inst_id = uuid.uuid4()
    now = datetime.now(_SH_TZ)
    target_set = _make_node_target_set([("target_10_5", 10.5), ("target_12_0", 12.0)])
    triggered: set[str] = set()

    # 1. 向上穿透 10.5 (10.0 -> 11.0)
    events_up = evaluate_node_crossings(inst_id, target_set, 10.0, 11.0, now, triggered)
    assert len(events_up) == 1
    assert events_up[0].event_type == EVENT_TYPE_NODE_CLUSTER_TOUCH
    assert events_up[0].payload["target_id"] == "target_10_5"
    assert events_up[0].payload["direction"] == "UP"
    assert "target_10_5" in triggered

    # 2. 未穿透 12.0 (11.0 -> 11.8)
    events_none = evaluate_node_crossings(inst_id, target_set, 11.0, 11.8, now, triggered)
    assert len(events_none) == 0

    # 3. 再次穿透 10.5 (11.8 -> 10.2) -> 因为是一次性事件，之前已触发过，绝不重复触发！
    events_repeat = evaluate_node_crossings(inst_id, target_set, 11.8, 10.2, now, triggered)
    assert len(events_repeat) == 0

    # 4. 新的目标向下穿透（假设未触发的 12.0 从 12.5 跌到 11.5）
    triggered.clear()
    events_down = evaluate_node_crossings(inst_id, target_set, 12.5, 11.5, now, triggered)
    assert len(events_down) == 1
    assert events_down[0].payload["target_id"] == "target_12_0"
    assert events_down[0].payload["direction"] == "DOWN"


def test_smc_bos_and_choch_crossing() -> None:
    inst_id = uuid.uuid4()
    now = datetime.now(_SH_TZ)

    # 结构点位：High=100.0, Low=90.0
    high_target = SmcStructureTarget(
        target_id="high_100",
        lane="swing",
        kind="high",
        level=100.0,
        anchor_index=10,
        anchor_time="2026-09-01",
    )
    low_target = SmcStructureTarget(
        target_id="low_90",
        lane="swing",
        kind="low",
        level=90.0,
        anchor_index=5,
        anchor_time="2026-08-25",
    )

    # 场景 A: 多头趋势 (swing_bias = 1)，向上突破 high -> 顺势 BOS
    smc_bull = _make_smc_target_set([high_target, low_target], [], swing_bias=1)
    triggered_a: set[str] = set()

    events_bos = evaluate_smc_events(inst_id, smc_bull, 99.0, 101.0, now, triggered_a)
    assert len(events_bos) == 1
    assert events_bos[0].event_type == SMC_BOS_RETEST
    assert events_bos[0].payload["structure_type"] == "BOS"
    assert events_bos[0].payload["direction"] == "UP"
    assert "high_100" in triggered_a

    # 再次震荡 -> 不重复报警
    events_bos_re = evaluate_smc_events(inst_id, smc_bull, 101.0, 99.5, now, triggered_a)
    assert len(events_bos_re) == 0

    # 场景 B: 空头趋势 (swing_bias = -1)，向上突破 high -> 逆势反转 CHoCH
    smc_bear = _make_smc_target_set([high_target, low_target], [], swing_bias=-1)
    triggered_b: set[str] = set()

    events_choch = evaluate_smc_events(inst_id, smc_bear, 99.0, 101.0, now, triggered_b)
    assert len(events_choch) == 1
    assert events_choch[0].event_type == SMC_CHOCH_RETEST
    assert events_choch[0].payload["structure_type"] == "CHOCH"
    assert events_choch[0].payload["direction"] == "UP"
    assert "high_100" in triggered_b


def test_smc_order_block_first_touch() -> None:
    inst_id = uuid.uuid4()
    now = datetime.now(_SH_TZ)

    ob_target = SmcOrderBlockTarget(
        target_id="ob_demand_50_52",
        internal=False,
        bias=1,
        bar_low=50.0,
        bar_high=52.0,
        anchor_index=20,
        anchor_time="2026-09-05",
        confirmed_index=21,
        confirmed_time="2026-09-06",
    )

    smc_set = _make_smc_target_set([], [ob_target])
    triggered: set[str] = set()

    # 1. 价格从上方 54.0 下跌进入 OB 51.5 -> 首次触碰触发
    events_touch = evaluate_smc_events(inst_id, smc_set, 54.0, 51.5, now, triggered)
    assert len(events_touch) == 1
    assert events_touch[0].event_type == SMC_ORDER_BLOCK_FIRST_TOUCH
    assert events_touch[0].payload["target_id"] == "ob_demand_50_52"
    assert "ob_demand_50_52" in triggered

    # 2. 价格仍在 OB 内部移动 (51.5 -> 51.0) -> 一次性原则，不重复触发
    events_inside = evaluate_smc_events(inst_id, smc_set, 51.5, 51.0, now, triggered)
    assert len(events_inside) == 0
