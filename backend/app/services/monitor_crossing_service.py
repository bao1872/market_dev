"""盘中监控价格穿透与一次性事件引擎（Stage G4 / G5）。

核心契约：
1. 筹码共识事件（G4）：消费 NodeMonitorTargetSet，比对 [P_last, P_curr] 是否穿透峰值价位；
2. SMC 实时事件（G5）：消费 SmcMonitorTargetSet，比对是否越过 BOS / CHoCH 价位，或首次落入 Order Block；
3. 事件生命周期（RC 修正，按子系统区分，**不再一律永久 one-shot**）：
   - **筹码共识（Node）**：可重复。同一 target 再次穿透即可再次发射，
     由 600s 事件冷却（锚定「上一次事件时间」）决定是否真正写入；
   - **SMC 结构（BOS / CHoCH）**：Target Set Version 生命期内 one-shot（不变）；
   - **SMC 订单块（OB）**：可重复 re-entry。停留在 OB 内不重复，
     离开 OB 后重新进入即可再次发射，是否写入同样由 600s 冷却决定；
   - 因此可重复事件的 dedupe_key 必须带上事件分钟，否则会被
     ``strategy_events.event_key`` 的永久唯一约束吃掉。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
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


def _minute_key(event_time: datetime) -> str:
    """事件分钟键。

    可重复事件（Node crossing / OB re-entry）必须把分钟并入 dedupe_key，
    否则 ``strategy_events.event_key`` 的永久唯一约束会让第二次之后的事件
    全部被 ``ON CONFLICT DO NOTHING`` 静默丢弃。
    """
    normalized = event_time.astimezone(UTC) if event_time.tzinfo is not None else event_time
    return normalized.strftime("%Y%m%d%H%M")


def evaluate_node_crossings(
    instrument_id: UUID,
    node_target_set: NodeMonitorTargetSet,
    price_last: float,
    price_curr: float,
    event_time: datetime,
) -> list[StrategyEventDraft]:
    """判定筹码共识区（VolumeNode Peak）价位穿透。

    穿透条件（离散价格跃迁）：
    - 向上穿透：price_last < target.price <= price_curr
    - 向下穿透：price_last > target.price >= price_curr

    [RC 生命周期修正] 筹码共识是**可重复**事件，**不再**维护永久 triggered set：
    ``09:40`` 穿透可触发 → ``09:47`` 再次穿透由 600s 冷却挡掉 →
    ``09:51`` 再次穿透可再次触发。是否真正写入由写入侧冷却判定
    （锚定上一次 ``StrategyEvent.event_time``，而非 ``datetime.now()``）。

    因此本函数不再接收/修改 ``triggered_target_ids``，且 dedupe_key 带分钟。
    """
    events: list[StrategyEventDraft] = []
    if price_last == price_curr:
        return events

    is_upward = price_curr > price_last
    minute_key = _minute_key(event_time)

    for target in node_target_set.targets:
        target_price = target.price
        crossed = False

        if is_upward:
            if price_last < target_price <= price_curr:
                crossed = True
        else:
            if price_last > target_price >= price_curr:
                crossed = True

        if crossed:
            direction = "UP" if is_upward else "DOWN"
            dedupe_key = f"node:{target.target_id}:{minute_key}"
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

    2. 订单块（OB）：
       - [RC 生命周期修正] **可重复 re-entry**，不再永久 one-shot：
         停留在 OB 内不重复；离开 OB 后重新进入即可再次发射；
         是否真正写入由 600s 冷却（锚定上一次事件时间）判定。
       - 因此 OB 不消费/写入 triggered_target_ids（该集合只服务 BOS/CHoCH）。

    Args:
        triggered_target_ids: **仅**服务 BOS / CHoCH 的「version 内 one-shot」
            已触发集合（会被 mutate）。OB 与 Node 不参与。
    """
    events: list[StrategyEventDraft] = []
    minute_key = _minute_key(event_time)
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

    # 2. 订单块触碰（OB）
    # [RC 生命周期修正] 可重复 re-entry：一直留在 OB 内不重复，
    # 离开后重新进入可再次触发。故不消费/写入 triggered_target_ids。
    for ob in smc_target_set.active_order_block_targets:
        bar_low = ob.bar_low
        bar_high = ob.bar_high

        # 上一帧已在 OB 内 → 无论怎么移动都不算重新进入
        was_inside = (bar_low <= price_last <= bar_high)
        is_inside = (bar_low <= price_curr <= bar_high)
        entered_from_above = (price_last > bar_high and price_curr <= bar_high)
        entered_from_below = (price_last < bar_low and price_curr >= bar_low)

        entered = not was_inside and (
            is_inside or entered_from_above or entered_from_below
        )

        if entered:
            dedupe_key = f"smc_ob:{ob.target_id}:{minute_key}"
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
