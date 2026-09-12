"""自选股盘中实时极速监控服务（Stage G6）。

主干接线与闭环编排：
1. 行情事实：调用 RealtimeMarketFactService（pytdx 批量主源，东财受控兜底）；
2. 价格区间：通过 PriceTracker 维护连续快照的 [P_last, P_curr]；
3. 筹码共识：NodeMonitorTargetSet 价格穿透，一次性发射 node_cluster_touch；
4. SMC 实时：SmcMonitorTargetSet 结构突破（BOS / CHoCH）与订单块触碰（OB First Touch），
   严格一次性（One-shot），杜绝 retest 与复杂 episode 重算；
5. 标的级并发安全与状态持久化。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx

from app.core.pytdx_adapter import PytdxAdapter
from app.models.instrument import Instrument
from app.services.monitor_crossing_service import (
    evaluate_node_crossings,
    evaluate_smc_events,
)
from app.services.node_monitor_target_service import NodeMonitorTargetSet
from app.services.realtime_market_fact_service import (
    PriceTracker,
    RealtimeMarketFactService,
)
from app.services.smc_monitor_target_service import SmcMonitorTargetSet
from app.strategy.runtime import StrategyEventDraft

logger = logging.getLogger(__name__)

_SH_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class MonitorCycleResult:
    """单轮盘中监控执行结果。"""

    captured_at: datetime
    total_instruments: int = 0
    quotes_fetched: int = 0
    events_detected: list[StrategyEventDraft] = field(default_factory=list)
    updated_states: dict[UUID, dict[str, Any]] = field(default_factory=dict)


class WatchlistRealtimeMonitorService:
    """盘中实时自选股监控主服务。"""

    def __init__(
        self,
        fact_service: RealtimeMarketFactService | None = None,
    ) -> None:
        self.fact_service = fact_service or RealtimeMarketFactService(PriceTracker())

    async def run_monitor_cycle(
        self,
        instruments: Sequence[Instrument],
        target_sets: dict[UUID, tuple[NodeMonitorTargetSet | None, SmcMonitorTargetSet | None]],
        prev_states: dict[UUID, dict[str, Any]],
        *,
        adapter: PytdxAdapter | None = None,
        client: httpx.AsyncClient | None = None,
        now: datetime | None = None,
    ) -> MonitorCycleResult:
        """运行单轮全量自选股监控快速评估。

        全流程毫秒级执行，不发起任何繁重的 K 线重算或指标二次聚合。
        """
        captured_at = now or datetime.now(_SH_TZ)
        cycle_result = MonitorCycleResult(
            captured_at=captured_at,
            total_instruments=len(instruments),
        )

        if not instruments:
            return cycle_result

        symbols = [inst.symbol for inst in instruments]

        # 1. 批量拉取盘中实时行情事实（pytdx 主源 80只/批，东财仅兜底）
        quotes = await self.fact_service.fetch_quotes(
            symbols,
            adapter=adapter,
            client=client,
        )
        cycle_result.quotes_fetched = len(quotes)

        # 2. 逐标的极速运行 Crossing 穿透判定
        for inst in instruments:
            quote = quotes.get(inst.symbol)
            if quote is None:
                continue

            # 连续快照价格区间 [P_last, P_curr]
            p_last, p_curr = self.fact_service.price_tracker.update_price(
                inst.symbol, quote.price
            )

            # 读取该标的前次状态与已触发 target IDs
            prev_state_dict = prev_states.get(inst.id) or {}
            triggered_ids: set[str] = set(
                prev_state_dict.get("triggered_target_ids") or []
            )

            node_set, smc_set = target_sets.get(inst.id, (None, None))
            inst_events: list[StrategyEventDraft] = []

            # G4: 筹码共识区穿透
            if node_set is not None:
                try:
                    node_evts = evaluate_node_crossings(
                        inst.id,
                        node_set,
                        p_last,
                        p_curr,
                        captured_at,
                        triggered_ids,
                    )
                    inst_events.extend(node_evts)
                except Exception as exc:
                    logger.warning("[%s] evaluate_node_crossings 异常: %s", inst.symbol, exc)

            # G5: SMC 结构突破与订单块触碰
            if smc_set is not None:
                try:
                    smc_evts = evaluate_smc_events(
                        inst.id,
                        smc_set,
                        p_last,
                        p_curr,
                        captured_at,
                        triggered_ids,
                    )
                    inst_events.extend(smc_evts)
                except Exception as exc:
                    logger.warning("[%s] evaluate_smc_events 异常: %s", inst.symbol, exc)

            cycle_result.events_detected.extend(inst_events)

            # 3. 产出更新后的状态 payload
            updated_payload = dict(prev_state_dict)
            updated_payload["current_price"] = p_curr
            updated_payload["price_last"] = p_last
            updated_payload["previous_close"] = quote.last_close
            change_pct = (
                round((p_curr - quote.last_close) / quote.last_close * 100, 4)
                if quote.last_close > 0
                else 0.0
            )
            updated_payload["change_pct"] = change_pct
            updated_payload["triggered_target_ids"] = list(triggered_ids)
            updated_payload["market"] = {
                "current_price": p_curr,
                "previous_close": quote.last_close,
                "change_pct": change_pct,
            }

            cycle_result.updated_states[inst.id] = updated_payload

        return cycle_result


__all__ = [
    "MonitorCycleResult",
    "WatchlistRealtimeMonitorService",
]
