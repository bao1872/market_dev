"""[MONITOR-CROSSING] 盘中监控穿透判定与一次性事件引擎单元测试。

覆盖契约：
1. 筹码共识（Node）穿透：
   - 向上穿透 [P_last < target <= P_curr] 触发 node_cluster_touch (UP)；
   - 向下穿透 [P_last > target >= P_curr] 触发 node_cluster_touch (DOWN)；
   - 未穿透不触发；
   - **可重复**：不再维护永久 triggered set；同一分钟内 dedupe_key 相同
     （由 event_key 唯一约束去重），跨分钟可再次触发，
     最终是否写入由 600s 冷却（锚定上一次事件时间）决定。
2. SMC 结构（BOS / CHoCH）穿透：
   - 顺势突破（high + bias=1 或 low + bias=-1）触发 BOS；
   - 逆势反转（high + bias=-1 或 low + bias=1）触发 CHoCH；
   - 穿越即发射，触发后标记已触发，无 retest 逻辑（保持 TargetSet version 内 one-shot）。
3. SMC Order Block：
   - 从外部进入 [bar_low, bar_high] 触发 smc_order_block_first_touch；
   - 停留在 OB 内不重复；离开后**重新进入**可再次触发（可重复 re-entry）。
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
    SMC_BOS_CROSS,
    SMC_CHOCH_CROSS,
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


def test_node_crossing_is_repeatable_and_minute_scoped() -> None:
    """Node 是可重复事件：09:40 触发 → 09:51 可再次触发（不再永久 one-shot）。"""
    inst_id = uuid.uuid4()
    t_0940 = datetime(2026, 9, 12, 9, 40, 0, tzinfo=_SH_TZ)
    t_0940_30 = datetime(2026, 9, 12, 9, 40, 30, tzinfo=_SH_TZ)
    t_0951 = datetime(2026, 9, 12, 9, 51, 0, tzinfo=_SH_TZ)

    target_set = _make_node_target_set([("target_10_5", 10.5), ("target_12_0", 12.0)])

    # 1. 09:40 向上穿透 10.5 (10.0 -> 11.0)
    events_up = evaluate_node_crossings(inst_id, target_set, 10.0, 11.0, t_0940)
    assert len(events_up) == 1
    assert events_up[0].event_type == EVENT_TYPE_NODE_CLUSTER_TOUCH
    assert events_up[0].payload["target_id"] == "target_10_5"
    assert events_up[0].payload["direction"] == "UP"

    # 2. 未穿透 12.0 (11.0 -> 11.8)
    assert len(evaluate_node_crossings(inst_id, target_set, 11.0, 11.8, t_0940)) == 0

    # 3. 同一分钟内再次穿透 → dedupe_key 相同，由 event_key 唯一约束去重
    events_same = evaluate_node_crossings(inst_id, target_set, 11.8, 10.2, t_0940_30)
    assert len(events_same) == 1
    assert events_same[0].dedupe_key == events_up[0].dedupe_key

    # 4. 09:51 再次穿透 10.5 → 可再次触发（不再永久 one-shot），dedupe_key 不同
    events_repeat = evaluate_node_crossings(inst_id, target_set, 11.8, 10.2, t_0951)
    assert len(events_repeat) == 1
    assert events_repeat[0].payload["target_id"] == "target_10_5"
    assert events_repeat[0].payload["direction"] == "DOWN"
    assert events_repeat[0].dedupe_key != events_up[0].dedupe_key

    # 5. 新的目标向下穿透（12.0 从 12.5 跌到 11.5）
    events_down = evaluate_node_crossings(inst_id, target_set, 12.5, 11.5, t_0940)
    assert len(events_down) == 1
    assert events_down[0].payload["target_id"] == "target_12_0"
    assert events_down[0].payload["direction"] == "DOWN"

    # 6. 区间退化（P_last == P_curr）→ 绝不触发
    assert len(evaluate_node_crossings(inst_id, target_set, 10.5, 10.5, t_0940)) == 0

    # 7. dedupe_key 必须含分钟，否则跨分钟事件会被永久唯一约束吃掉
    assert t_0951.astimezone(ZoneInfo("UTC")).strftime("%Y%m%d%H%M") in (
        events_repeat[0].dedupe_key
    )


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
    assert events_bos[0].event_type == SMC_BOS_CROSS
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
    assert events_choch[0].event_type == SMC_CHOCH_CROSS
    assert events_choch[0].payload["structure_type"] == "CHOCH"
    assert events_choch[0].payload["direction"] == "UP"
    assert "high_100" in triggered_b


def test_smc_order_block_reentry_is_repeatable() -> None:
    """OB 可重复 re-entry：留在内部不重复，离开后重新进入可再次触发。"""
    inst_id = uuid.uuid4()
    t_0940 = datetime(2026, 9, 12, 9, 40, 0, tzinfo=_SH_TZ)
    t_0951 = datetime(2026, 9, 12, 9, 51, 0, tzinfo=_SH_TZ)

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

    # 1. 价格从上方 54.0 下跌进入 OB 51.5 -> 触发
    events_touch = evaluate_smc_events(inst_id, smc_set, 54.0, 51.5, t_0940, triggered)
    assert len(events_touch) == 1
    assert events_touch[0].event_type == SMC_ORDER_BLOCK_FIRST_TOUCH
    assert events_touch[0].payload["target_id"] == "ob_demand_50_52"
    # OB 不再进入永久 triggered set（该集合只服务 BOS/CHoCH）
    assert "ob_demand_50_52" not in triggered

    # 2. 价格仍在 OB 内部移动 (51.5 -> 51.0) -> 不算 re-entry，不重复触发
    assert len(evaluate_smc_events(inst_id, smc_set, 51.5, 51.0, t_0940, triggered)) == 0

    # 3. 离开 OB (51.0 -> 54.0) -> 不触发
    assert len(evaluate_smc_events(inst_id, smc_set, 51.0, 54.0, t_0940, triggered)) == 0

    # 4. 09:51 从上方重新进入 OB (54.0 -> 51.8) -> 可再次触发，dedupe_key 带分钟
    events_reentry = evaluate_smc_events(inst_id, smc_set, 54.0, 51.8, t_0951, triggered)
    assert len(events_reentry) == 1
    assert events_reentry[0].payload["target_id"] == "ob_demand_50_52"
    assert events_reentry[0].dedupe_key != events_touch[0].dedupe_key

    # 5. 从下方重新进入 (48.0 -> 50.5) -> 也算 re-entry
    assert len(evaluate_smc_events(inst_id, smc_set, 48.0, 50.5, t_0951, triggered)) == 1
