"""盘中监控价格穿透与一次性事件引擎（Stage G4 / G5）。

核心契约：
1. 筹码共识事件（G4）：消费 NodeMonitorTargetSet，比对 [P_last, P_curr] 是否穿透峰值价位；
2. SMC 实时事件（G5）：消费 SmcMonitorTargetSet，比对是否越过 BOS / CHoCH 价位，或首次落入 Order Block；
3. 一次性触发原则（One-shot）：
   - 单个 target_id 仅触发一次，触发后立即加入 triggered_target_ids；
   - 在该 Target Set Version 生命期内绝不重复触发；
   - 彻底废弃旧有 retest 逻辑与 episode 状态机，无 EQH/EQL 触发。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from app.constants.indicator_contract import NODE_CLUSTER_EVENT_TTL_SECONDS
from app.services.node_monitor_target_service import NodeMonitorTargetSet
from app.services.smc_monitor_target_service import SmcMonitorTargetSet
from app.strategy.monitors.smc_monitor import (
    SMC_BOS_CROSS,
    SMC_CHOCH_CROSS,
    SMC_ORDER_BLOCK_FIRST_TOUCH,
)
from app.strategy.monitors.volume_node_monitor import EVENT_TYPE_NODE_CLUSTER_TOUCH
from app.strategy.runtime import StrategyEventDraft

logger = logging.getLogger(__name__)

EVENT_TTL_SECONDS = NODE_CLUSTER_EVENT_TTL_SECONDS  # 600s


def evaluate_node_crossings(
    instrument_id: UUID,
    node_target_set: NodeMonitorTargetSet,
    price_last: float,
    price_curr: float,
    event_time: datetime,
    triggered_target_ids: set[str],
) -> list[StrategyEventDraft]:
    """判定筹码共识区（VolumeNode Peak）价位穿透。

    穿透条件（离散价格跃迁）：
    - 向上穿透：price_last < target.price <= price_curr
    - 向下穿透：price_last > target.price >= price_curr

    触发后：
    - target_id 记入 triggered_target_ids；
    - 发射 StrategyEventDraft(node_cluster_touch)；
    - 严格一次性，已触发则跳过。
    """
    events: list[StrategyEventDraft] = []
    if price_last == price_curr:
        return events

    is_upward = price_curr > price_last

    for target in node_target_set.targets:
        if target.target_id in triggered_target_ids:
            continue

        target_price = target.price
        crossed = False

        if is_upward:
            if price_last < target_price <= price_curr:
                crossed = True
        else:
            if price_last > target_price >= price_curr:
                crossed = True

        if crossed:
            triggered_target_ids.add(target.target_id)
            direction = "UP" if is_upward else "DOWN"
            dedupe_key = f"node:{target.target_id}"
            logical_entity = f"{instrument_id}:{target.target_id}"
            payload: dict[str, Any] = {
                "target_id": target.target_id,
                "target_price": target_price,
                "price_last": price_last,
                "current_price": price_curr,
                "direction": direction,
                "target_set_version": node_target_set.target_set_version,
                "indicator_view": "node_cluster",
            }

            events.append(
                StrategyEventDraft(
                    event_type=EVENT_TYPE_NODE_CLUSTER_TOUCH,
                    event_time=event_time,
                    dedupe_key=dedupe_key,
                    logical_entity=logical_entity,
                    payload=payload,
                    state_ttl_seconds=EVENT_TTL_SECONDS,
                )
            )

    return events


def evaluate_smc_events(
    instrument_id: UUID,
    smc_target_set: SmcMonitorTargetSet,
    price_last: float,
    price_curr: float,
    event_time: datetime,
    triggered_target_ids: set[str],
) -> list[StrategyEventDraft]:
    """判定 SMC 结构穿透（BOS / CHoCH）与订单块触碰（OB First Touch）。

    1. 结构点位（BOS / CHoCH）：
       - High 点位向上越过：price_last < level <= price_curr
         若该 lane bias 为 1 -> BOS；若 bias 为 -1 -> CHoCH；
       - Low 点位向下越过：price_last > level >= price_curr
         若该 lane bias 为 -1 -> BOS；若 bias 为 1 -> CHoCH；
       - 穿越了那就是过了，发射对应事件，记入 triggered_target_ids，无 retest。

    2. 订单块（OB First Touch）：
       - 判定价格落入或进入 [bar_low, bar_high] 区间；
       - 首次触碰即发射 smc_order_block_first_touch，随后标记触碰，不再重复触发。
    """
    events: list[StrategyEventDraft] = []
    swing_bias = int(smc_target_set.structure_context.get("swing_bias") or 0)
    internal_bias = int(smc_target_set.structure_context.get("internal_bias") or 0)

    # 1. 结构穿透判定
    if price_last != price_curr:
        is_upward = price_curr > price_last

        for target in smc_target_set.active_structure_targets:
            if target.target_id in triggered_target_ids:
                continue

            level = target.level
            crossed = False

            if target.kind == "high" and is_upward:
                if price_last < level <= price_curr:
                    crossed = True
            elif target.kind == "low" and not is_upward:
                if price_last > level >= price_curr:
                    crossed = True

            if crossed:
                triggered_target_ids.add(target.target_id)
                lane_bias = swing_bias if target.lane == "swing" else internal_bias

                # 判定是顺势突破 (BOS) 还是转折突破 (CHoCH)
                if target.kind == "high":
                    is_bos = (lane_bias == 1)
                else:
                    is_bos = (lane_bias == -1)

                event_type = SMC_BOS_CROSS if is_bos else SMC_CHOCH_CROSS
                structure_type = "BOS" if is_bos else "CHOCH"
                direction = "UP" if is_upward else "DOWN"

                dedupe_key = f"smc_struct:{target.target_id}"
                logical_entity = f"{instrument_id}:{target.target_id}"
                payload = {
                    "target_id": target.target_id,
                    "structure_type": structure_type,
                    "lane": target.lane,
                    "kind": target.kind,
                    "level": level,
                    "price_last": price_last,
                    "current_price": price_curr,
                    "direction": direction,
                    "anchor_time": target.anchor_time,
                    "target_set_version": smc_target_set.target_set_version,
                    "indicator_view": "smc",
                }

                events.append(
                    StrategyEventDraft(
                        event_type=event_type,
                        event_time=event_time,
                        dedupe_key=dedupe_key,
                        logical_entity=logical_entity,
                        payload=payload,
                        state_ttl_seconds=EVENT_TTL_SECONDS,
                    )
                )

    # 2. 订单块首次触碰（OB First Touch）
    for ob in smc_target_set.active_order_block_targets:
        if ob.target_id in triggered_target_ids:
            continue

        bar_low = ob.bar_low
        bar_high = ob.bar_high

        # 价格当前在 OB 内，或价格区间进入 OB
        is_inside = (bar_low <= price_curr <= bar_high)
        entered_from_above = (price_last > bar_high and price_curr <= bar_high)
        entered_from_below = (price_last < bar_low and price_curr >= bar_low)

        if is_inside or entered_from_above or entered_from_below:
            triggered_target_ids.add(ob.target_id)
            dedupe_key = f"smc_ob:{ob.target_id}"
            logical_entity = f"{instrument_id}:{ob.target_id}"
            payload = {
                "target_id": ob.target_id,
                "internal": ob.internal,
                "bias": ob.bias,
                "bar_low": bar_low,
                "bar_high": bar_high,
                "price_last": price_last,
                "current_price": price_curr,
                "anchor_time": ob.anchor_time,
                "confirmed_time": ob.confirmed_time,
                "target_set_version": smc_target_set.target_set_version,
                "indicator_view": "smc",
            }

            events.append(
                StrategyEventDraft(
                    event_type=SMC_ORDER_BLOCK_FIRST_TOUCH,
                    event_time=event_time,
                    dedupe_key=dedupe_key,
                    logical_entity=logical_entity,
                    payload=payload,
                    state_ttl_seconds=EVENT_TTL_SECONDS,
                )
            )

    return events


__all__ = [
    "evaluate_node_crossings",
    "evaluate_smc_events",
]
