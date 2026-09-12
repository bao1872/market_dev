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

            # 读取该标的前次状态与 Target Sets
            prev_state_dict = prev_states.get(inst.id) or {}

            node_set, smc_set = target_sets.get(inst.id, (None, None))
            curr_node_ver = node_set.target_set_version if node_set else None
            curr_smc_ver = smc_set.target_set_version if smc_set else None

            prev_node_ver = prev_state_dict.get("node_target_set_version")
            prev_smc_ver = prev_state_dict.get("smc_target_set_version")

            is_node_ver_changed = (prev_node_ver is not None and curr_node_ver != prev_node_ver)
            is_smc_ver_changed = (prev_smc_ver is not None and curr_smc_ver != prev_smc_ver)
            version_changed = is_node_ver_changed or is_smc_ver_changed

            # 区分 node 与 smc 的已触发 target_ids
            # 若 target_set_version 发生改变（新 Bar 完成、盘后更新、或 XDXR 公司行为导致重算），
            # 自动清空对应旧版本的 triggered targets，由新版本重新接管
            node_triggered: set[str] = set(prev_state_dict.get("triggered_node_target_ids") or [])
            if is_node_ver_changed:
                logger.info("[%s] Node target set version rolled %s -> %s, reset triggered targets", inst.symbol, prev_node_ver, curr_node_ver)
                node_triggered = set()
            elif not node_triggered and prev_state_dict.get("triggered_target_ids"):
                node_triggered = set(prev_state_dict.get("triggered_target_ids") or [])

            smc_triggered: set[str] = set(prev_state_dict.get("triggered_smc_target_ids") or [])
            if is_smc_ver_changed:
                logger.info("[%s] SMC target set version rolled %s -> %s, reset triggered targets", inst.symbol, prev_smc_ver, curr_smc_ver)
                smc_triggered = set()
            elif not smc_triggered and prev_state_dict.get("triggered_target_ids"):
                smc_triggered = set(prev_state_dict.get("triggered_target_ids") or [])

            # 价格追踪器 bootstrap 与连续快照 [P_last, P_curr]：
            # 1. 若 Target Set Version 发生变更（XDXR 除权除息或日线滚动），第一帧强制以本次价初始化为 (p, p)，
            #    严禁跨越新旧复权坐标断层产生假穿透；
            # 2. 若内存中尚无该标的价格（进程重启或首次执行）：
            #    若持久化的前次状态版本与当前 Target Set Version 一致，则从持久化的 current_price 恢复 P_last，
            #    支持重启窗口期断口追溯（catch-up crossing）；
            # 3. 正常运行中，连续推进快照区间。
            in_memory_last = self.fact_service.price_tracker.get_last_price(inst.symbol)
            if version_changed:
                self.fact_service.price_tracker.set_last_price(inst.symbol, quote.price)
                p_last = quote.price
                p_curr = quote.price
            elif in_memory_last is None:
                persisted_price = prev_state_dict.get("current_price")
                same_version = (
                    (prev_node_ver == curr_node_ver if curr_node_ver else True) and
                    (prev_smc_ver == curr_smc_ver if curr_smc_ver else True)
                )
                if persisted_price is not None and same_version:
                    p_last = float(persisted_price)
                    p_curr = quote.price
                    self.fact_service.price_tracker.set_last_price(inst.symbol, quote.price)
                else:
                    p_last, p_curr = self.fact_service.price_tracker.update_price(inst.symbol, quote.price)
            else:
                p_last, p_curr = self.fact_service.price_tracker.update_price(inst.symbol, quote.price)

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
                        node_triggered,
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
                        smc_triggered,
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
            updated_payload["node_target_set_version"] = curr_node_ver
            updated_payload["smc_target_set_version"] = curr_smc_ver
            updated_payload["triggered_node_target_ids"] = list(node_triggered)
            updated_payload["triggered_smc_target_ids"] = list(smc_triggered)
            updated_payload["triggered_target_ids"] = list(node_triggered | smc_triggered)
            updated_payload["market"] = {
                "current_price": p_curr,
                "previous_close": quote.last_close,
                "change_pct": change_pct,
            }

            cycle_result.updated_states[inst.id] = updated_payload

        return cycle_result

    def reset_symbol_state(self, symbol: str) -> None:
        """重置指定标的的内存价格追踪状态。"""
        self.fact_service.price_tracker.reset(symbol)


__all__ = [
    "MonitorCycleResult",
    "WatchlistRealtimeMonitorService",
]
