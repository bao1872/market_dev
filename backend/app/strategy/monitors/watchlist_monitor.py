"""自选股监控 - 结构 + 筹码共识 双类别监控（薄包装）。

[CHANGE-20260728-010] WatchlistMonitor 仅保留 SMC + VolumeNode 两类触发。
布林带（Bollinger）不再参与盘中事件计算、状态合并、事件统计与通知文案。
Bollinger 算法本体、盘后 Bollinger 计算、个股详情页布林带图层工具栏不在本文件禁止范围。

唯一监控算法，内部委托 VolumeNodeMonitor、SmcMonitor：
- calculate_state(): 分别调用两个子 monitor，合并 state 字典到命名空间
  node_cluster/smc/market；并补充 previous_close/change_pct；单个子 monitor 失败
  只标记该项 degraded，不阻断其他项。
- detect_events(): Node 走既有 crossing（``evaluate_node_crossings``）；SMC 走
  canonical realtime transition（``context.smc_realtime_input``）；单个子系统失败
  只记录错误，不阻断其他项。
- compute_indicators(): 分别调用两个子 monitor，合并指标字典。

SMC 通过 Canonical SMC Adapter（compute_smc_adapter）调用，继续排除 FVG。

MonitorState 命名空间（state schema v3）：
- state["node_cluster"]: VN 子 monitor 状态（current_price/upper_node/lower_node/
  position_0_1/poc_price/last_touched_node）
- state["smc"]: SMC 子 monitor 状态（smc_confirmed_bos/smc_confirmed_choch/
  smc_equal_highs_lows/smc_active_obs/smc_current_price/smc_currently_touched/
  smc_swing_bias/smc_trailing/smc_availability/smc_degraded_reason/
  smc_episode_tracker）
- state["market"]: 市场数据（current_price/previous_close/change_pct）
- state["degraded"]: {"node_cluster": bool, "smc": bool}
兼容旧平铺状态：所有 node_cluster/smc 字段同时保留在 state 顶层。
旧 _extract_sub_state 读取时优先命名空间，fallback 顶层平铺。
兼容旧 state["bb"]：仅做读取兼容，不再生成 bb 状态。

[G canonical SMC cutover] detect_events 的 SMC 事件**唯一**来源 =
canonical realtime transition pipeline（``context.smc_realtime_input`` →
``evaluate_realtime_smc_events``）。旧 G5 ``evaluate_smc_events`` 与 legacy
``SmcMonitor.detect_events`` 作为 live producer 已移除。SMC 子状态（含
smc_episode_tracker）仍由 ``calculate_state`` 写入，供历史/展示读取兼容。

[自选股涨跌幅] - 描述: previous_close/change_pct 在合并 VN+SMC state 后计算
- current_price 取 merged_state["current_price"]（VN 已写入）
- previous_close = context.trade_date 之前最近一个交易日 close（前复权）
- change_pct = (current_price - previous_close) / previous_close * 100
- 当日未完成日线 Bar 不得作为 previous_close（按 trade_date 严格 < 过滤）

用法（模块自测）：
    python -m app.strategy.monitors.watchlist_monitor
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import pandas as pd

from app.models.strategy import StrategyVersion
from app.services.monitor_crossing_service import evaluate_node_crossings
from app.services.smc_realtime_event_service import evaluate_realtime_smc_events
from app.services.smc_realtime_transition_service import serialize_transition_state
from app.strategy.monitors.smc_monitor import SmcMonitor
from app.strategy.monitors.volume_node_monitor import VolumeNodeMonitor
from app.strategy.runtime import (
    MarketDataContext,
    MonitorState,
    StrategyEventDraft,
    StrategyRuntime,
)

logger = logging.getLogger("strategy.monitors.watchlist_monitor")

# 子 monitor 命名空间键
NAMESPACE_NODE_CLUSTER = "node_cluster"
NAMESPACE_SMC = "smc"
NAMESPACE_MARKET = "market"
NAMESPACE_DEGRADED = "degraded"
# [CHANGE-20260728-010] BB 命名空间仅做历史读取兼容，不再生成
NAMESPACE_BB_LEGACY = "bb"

# VN 字段集合
_VN_KEYS = {
    "current_price", "upper_node", "lower_node",
    "position_0_1", "poc_price", "last_touched_node",
}

# SMC 字段集合（含 smc_episode_tracker 用于显式回写）
_SMC_KEYS = {
    "smc_confirmed_bos", "smc_confirmed_choch", "smc_equal_highs_lows",
    "smc_active_obs", "smc_current_price", "smc_currently_touched",
    "smc_swing_bias", "smc_trailing", "smc_availability",
    "smc_degraded_reason", "smc_episode_tracker",
}

# 市场字段集合
_MARKET_KEYS = {"current_price", "previous_close", "change_pct"}

# State schema 版本：v3 = 移除 BB 委托，仅保留 VN + SMC（CHANGE-20260728-010）
STATE_VERSION = 3


class WatchlistMonitor(StrategyRuntime):
    """自选股监控 - 结构 + 筹码共识 双类别监控（薄包装）。

    内部持有 VolumeNodeMonitor、SmcMonitor 实例，
    将 calculate_state/detect_events/compute_indicators 委托给子 monitor，
    合并结果后返回。

    单个子 monitor 失败只标记该项 degraded，不阻断其他项。

    生命周期：
    1. StrategyLoader.load(version) 创建实例
    2. initialize(version) 创建子 monitor 实例并分别初始化
    3. calculate_state(context) 合并 VN + SMC 状态到命名空间
    4. detect_events(context, prev, curr) 合并 VN + SMC 事件
    """

    kind = "monitor"

    def __init__(self) -> None:
        self._vn: VolumeNodeMonitor = VolumeNodeMonitor()
        self._smc: SmcMonitor = SmcMonitor()
        self._strategy_version_id: UUID | None = None

    async def initialize(self, version: StrategyVersion) -> None:
        """创建子 monitor 实例并分别初始化。

        将同一个 StrategyVersion 传递给两个子 monitor，
        各自从 manifest 中提取所需参数。

        Args:
            version: 策略版本 ORM 对象
        """
        self._strategy_version_id = version.id
        await self._vn.initialize(version)
        await self._smc.initialize(version)
        logger.info(
            "WatchlistMonitor 初始化完成: lookback=%d, smc=enabled (BB 已移除)",
            self._vn._lookback,
        )

    async def execute(self, context: MarketDataContext) -> Any:  # type: ignore[override]
        """selector 执行接口（monitor 不支持）。"""
        raise NotImplementedError(
            "WatchlistMonitor 是 monitor 策略，不支持 execute（请使用 calculate_state + detect_events）"
        )

    async def calculate_state(self, context: MarketDataContext) -> MonitorState:
        """合并 VN + SMC 子 monitor 的状态到命名空间，并补充 previous_close/change_pct。

        分别调用 VolumeNodeMonitor.calculate_state()、SmcMonitor.calculate_state()。
        单个子 monitor 失败只标记该项 degraded，不阻断其他项。

        命名空间结构：
        - state["node_cluster"]: VN 字段
        - state["smc"]: SMC 字段
        - state["market"]: current_price/previous_close/change_pct
        - state["degraded"]: {"node_cluster": bool, "smc": bool}

        兼容旧平铺：所有子 monitor 字段同时保留在 state 顶层。

        [自选股涨跌幅] - 描述:
            current_price 已在 merged_state（由 VN 写入）；
            previous_close 由 _compute_previous_close 从 context.bars_daily 取
            context.trade_date 之前最近一个交易日的 close；
            change_pct = (current_price - previous_close) / previous_close * 100。
            当日未完成日线 Bar 不得作为 previous_close（按 trade_date 严格 < 过滤）。

        Args:
            context: 市场数据上下文

        Returns:
            合并后的监控状态（命名空间 + 平铺兼容 + degraded 标记）
        """
        # 子 monitor 状态分别计算，单个失败不影响其他
        vn_state_dict: dict[str, Any] = {}
        smc_state_dict: dict[str, Any] = {}
        vn_degraded = False
        smc_degraded = False
        bar_time = None

        # VN
        try:
            vn_state = await self._vn.calculate_state(context)
            vn_state_dict = dict(vn_state.state)
            bar_time = bar_time or vn_state.updated_at
        except Exception as exc:
            vn_degraded = True
            logger.warning(
                "VolumeNodeMonitor.calculate_state 失败（标记 degraded，不阻断其他）: %s",
                exc,
            )

        # SMC
        try:
            smc_state = await self._smc.calculate_state(context)
            smc_state_dict = dict(smc_state.state)
            bar_time = bar_time or smc_state.updated_at
        except Exception as exc:
            smc_degraded = True
            logger.warning(
                "SmcMonitor.calculate_state 失败（标记 degraded，不阻断其他）: %s",
                exc,
            )

        # 合并平铺 state（兼容旧读取）
        merged_flat: dict[str, Any] = {**vn_state_dict, **smc_state_dict}

        # [自选股涨跌幅] - 在合并 state 后补充 previous_close + change_pct
        current_price = merged_flat.get("current_price") or merged_flat.get("smc_current_price")
        previous_close = self._compute_previous_close(context)
        change_pct = self._compute_change_pct(current_price, previous_close)
        market_state: dict[str, Any] = {
            "current_price": current_price,
            "previous_close": previous_close,
            "change_pct": change_pct,
        }
        merged_flat["previous_close"] = previous_close
        merged_flat["change_pct"] = change_pct

        # 命名空间（new 结构）
        merged_flat[NAMESPACE_NODE_CLUSTER] = {
            k: v for k, v in vn_state_dict.items() if k in _VN_KEYS
        }
        merged_flat[NAMESPACE_SMC] = {
            k: v for k, v in smc_state_dict.items() if k in _SMC_KEYS
        }
        merged_flat[NAMESPACE_MARKET] = market_state
        merged_flat[NAMESPACE_DEGRADED] = {
            NAMESPACE_NODE_CLUSTER: vn_degraded,
            NAMESPACE_SMC: smc_degraded,
        }

        return MonitorState(
            instrument_id=context.instrument_id,
            strategy_version_id=self._strategy_version_id,  # type: ignore[arg-type]
            state=merged_flat,
            state_version=STATE_VERSION,
            updated_at=bar_time,
        )

    @staticmethod
    def _compute_previous_close(context: MarketDataContext) -> float | None:
        """从 context.bars_daily 取 trade_date 之前最近一个交易日的 close。

        [自选股涨跌幅] - 描述:
            - 严格 < context.trade_date，排除当日未完成 Bar
            - 数据缺失或 trade_date 为 None 时返回 None
            - 前复权数据由调用方在 get_bars(adjustment="qfq") 时已应用

        Args:
            context: 市场数据上下文

        Returns:
            前一交易日 close（float），或 None
        """
        bars = context.bars_daily
        if bars is None or bars.empty:
            return None
        if context.trade_date is None:
            return None
        # 仅取 trade_date 之前（严格 <）的 Bar
        if not isinstance(bars.index, pd.DatetimeIndex):
            return None
        trade_date_ts = pd.Timestamp(context.trade_date, tz=bars.index.tz)
        historical = bars[bars.index < trade_date_ts]
        if historical.empty:
            return None
        return round(float(historical["close"].iloc[-1]), 4)

    @staticmethod
    def _compute_change_pct(
        current_price: float | None,
        previous_close: float | None,
    ) -> float | None:
        """计算涨跌幅（%）：(current - previous) / previous * 100。

        Args:
            current_price: 当前价（来自 merged_state["current_price"]）
            previous_close: 前一交易日收盘价

        Returns:
            涨跌幅（%），保留 4 位小数；输入任一为 None 或 previous=0 时返回 None
        """
        if current_price is None or previous_close is None:
            return None
        if previous_close == 0:
            return None
        return round((float(current_price) - float(previous_close)) / float(previous_close) * 100, 4)

    async def detect_events(
        self,
        context: MarketDataContext,
        prev_state: MonitorState | None,
        curr_state: MonitorState,
    ) -> list[StrategyEventDraft]:
        """合并 Node 事件与 canonical realtime SMC 事件（G canonical SMC cutover）。

        Node：注入 ``node_target_set`` 时走 ``evaluate_node_crossings``（可重复事件），
        否则回退 VN legacy ``detect_events``。

        SMC：唯一 live producer = :meth:`_append_realtime_smc_events`
        （canonical realtime transition）；旧 G5 ``evaluate_smc_events`` 与
        legacy ``SmcMonitor.detect_events`` 已不可达。

        单个子系统失败只记录错误，不阻断其他项。

        Args:
            context: 市场数据上下文
            prev_state: 前一状态
            curr_state: 当前状态（将被 mutate 以回写 canonical transition state）

        Returns:
            合并后的事件草稿列表
        """
        events: list[StrategyEventDraft] = []

        # [G canonical SMC cutover] Node 与 SMC 完全解耦：
        # - Node：保持既有 crossing 逻辑（evaluate_node_crossings，可重复事件）；
        # - SMC：唯一 live producer = canonical realtime transition pipeline；
        #   旧 G5 evaluate_smc_events 与 legacy SmcMonitor.detect_events 不可达。
        node_target_set = getattr(context, "node_target_set", None)

        if node_target_set is not None:
            p_curr = (
                getattr(context, "current_price", None)
                or curr_state.state.get("current_price")
                or 0.0
            )
            p_last = getattr(context, "price_last", None)
            if p_last is None:
                p_last = (
                    prev_state.state.get("current_price", p_curr)
                    if prev_state
                    else p_curr
                )

            evt_time = context.bar_time or datetime.now()

            try:
                node_evts = evaluate_node_crossings(
                    context.instrument_id,
                    node_target_set,
                    float(p_last),
                    float(p_curr),
                    evt_time,
                )
                events.extend(node_evts)
            except Exception as exc:
                logger.warning("evaluate_node_crossings 失败: %s", exc)

            curr_state.state["node_target_set_version"] = node_target_set.target_set_version
            curr_state.state["price_last"] = p_last

        # [G7 price-tracker 生命周期输入] smc TargetSet 版本仅作为
        # resolve_snapshot_price_range 的 version-roll 输入（真实当前消费者），
        # **不再**决定 SMC 事件生成。
        smc_target_set = getattr(context, "smc_target_set", None)
        curr_state.state["smc_target_set_version"] = (
            smc_target_set.target_set_version if smc_target_set is not None else None
        )

        # ── VN legacy 路径：仅当未注入 node_target_set 时生效 ─────────
        if node_target_set is None:
            # VN 事件检测（旧 1m 判定）
            try:
                vn_prev = (
                    self._extract_sub_state(prev_state, NAMESPACE_NODE_CLUSTER)
                    if prev_state else None
                )
                vn_curr = self._extract_sub_state(curr_state, NAMESPACE_NODE_CLUSTER)
                vn_events = await self._vn.detect_events(context, vn_prev, vn_curr)
                events.extend(vn_events)
            except Exception as exc:
                logger.warning("VolumeNodeMonitor.detect_events 失败（不阻断其他）: %s", exc)

        # ── SMC：唯一 canonical realtime producer ────────────────────
        self._append_realtime_smc_events(context, prev_state, curr_state, events)

        return events

    def _append_realtime_smc_events(
        self,
        context: MarketDataContext,
        prev_state: MonitorState | None,
        curr_state: MonitorState,
        events: list[StrategyEventDraft],
    ) -> None:
        """canonical realtime SMC 事件 producer（SMC 事件的唯一来源）。

        契约（fail-closed，无 legacy fallback）：
        - ``context.smc_realtime_input`` 缺失（canonical input unavailable）
          → SMC 0 事件；Node 不受影响；原样保留既有 transition 命名空间。
        - 评估 fail closed（degraded_reason / 异常）
          → SMC 0 事件；**不推进** state；原样保留既有命名空间。
        - 评估成功（含 no-op）
          → extend drafts；``next_state`` 序列化写回 ``curr_state``。

        绝不调用 ``evaluate_smc_events`` 或 ``SmcMonitor.detect_events``。
        """
        bundle = getattr(context, "smc_realtime_input", None)

        if bundle is None:
            self._carry_forward_transition_state(prev_state, curr_state)
            self._record_realtime_smc_degraded(
                curr_state,
                getattr(context, "smc_realtime_degraded_reason", None)
                or "canonical realtime SMC input unavailable",
            )
            return

        try:
            result = evaluate_realtime_smc_events(bundle)
        except Exception as exc:  # noqa: BLE001 - 单标的评估失败不阻断 Node
            logger.warning("evaluate_realtime_smc_events 失败（fail closed）: %s", exc)
            result = None

        if result is None or result.degraded_reason:
            reason = (
                result.degraded_reason
                if result is not None
                else "realtime SMC evaluation failed"
            )
            logger.warning(
                "[%s] canonical realtime SMC fail closed: %s", context.symbol, reason
            )
            self._carry_forward_transition_state(prev_state, curr_state)
            self._record_realtime_smc_degraded(curr_state, reason)
            return

        events.extend(result.drafts)
        # 成功（含 no-op）→ 持久化 next_state，namespace 一旦建立即不再消失。
        curr_state.state["smc_realtime_transition"] = serialize_transition_state(
            result.next_state
        )
        self._record_realtime_smc_degraded(curr_state, None)

    @staticmethod
    def _carry_forward_transition_state(
        prev_state: MonitorState | None,
        curr_state: MonitorState,
    ) -> None:
        """fail closed 时原样保留上一轮持久化的 transition 命名空间。

        monitor_state 以 ``curr_state.state`` 整体 upsert；若本周期失败就丢掉该 key，
        下一轮 ``prepare_transition_state`` 会把「namespace 不存在」误判为 bootstrap，
        从而可能重发已消费的结构事件。corrupt payload 亦原样保留（继续 fail closed，
        绝不偷偷重新初始化）。
        """
        if prev_state is None:
            return
        persisted = prev_state.state.get("smc_realtime_transition")
        if persisted is not None:
            curr_state.state["smc_realtime_transition"] = persisted

    @staticmethod
    def _record_realtime_smc_degraded(
        curr_state: MonitorState,
        reason: str | None,
    ) -> None:
        """记录 / 清除 canonical realtime SMC 降级原因（可观测性）。"""
        curr_state.state["smc_realtime_degraded_reason"] = reason
        degraded = curr_state.state.get(NAMESPACE_DEGRADED)
        if isinstance(degraded, dict):
            degraded["smc_degraded_reason"] = reason

    @staticmethod
    def _extract_sub_state(
        state: MonitorState, sub: str
    ) -> MonitorState:
        """从合并状态中提取子 monitor 状态。

        优先从命名空间读取（state.state["node_cluster"]/["smc"]），
        fallback 到顶层平铺（兼容旧 state schema v1/v2）。

        VN 字段: current_price/upper_node/lower_node/position_0_1/poc_price/last_touched_node
        SMC 字段: smc_confirmed_bos/smc_confirmed_choch/smc_equal_highs_lows/
                  smc_active_obs/smc_current_price/smc_currently_touched/
                  smc_swing_bias/smc_trailing/smc_availability/smc_degraded_reason/
                  smc_episode_tracker

        [CHANGE-20260728-010] sub="bb" 仅做历史读取兼容（不再生成 bb 状态），
        返回空状态字典，调用方应跳过 BB 事件检测。

        Args:
            state: 合并后的 MonitorState
            sub: "node_cluster" / "smc" / "bb"（legacy 兼容）

        Returns:
            包含子 monitor 字段的 MonitorState
        """
        if sub == NAMESPACE_NODE_CLUSTER:
            keys = _VN_KEYS
        elif sub == NAMESPACE_SMC:
            keys = _SMC_KEYS
        elif sub == NAMESPACE_BB_LEGACY:
            # BB 仅历史读取兼容，返回空状态（不再生成 bb 状态）
            return MonitorState(
                instrument_id=state.instrument_id,
                strategy_version_id=state.strategy_version_id,
                state={},
                state_version=state.state_version,
                updated_at=state.updated_at,
            )
        else:
            keys = set()

        # 优先从命名空间读取
        namespaced = state.state.get(sub)
        if isinstance(namespaced, dict) and namespaced:
            return MonitorState(
                instrument_id=state.instrument_id,
                strategy_version_id=state.strategy_version_id,
                state=dict(namespaced),
                state_version=state.state_version,
                updated_at=state.updated_at,
            )

        # Fallback: 从顶层平铺读取（兼容旧 state schema v1/v2）
        sub_state = {k: v for k, v in state.state.items() if k in keys}
        return MonitorState(
            instrument_id=state.instrument_id,
            strategy_version_id=state.strategy_version_id,
            state=sub_state,
            state_version=state.state_version,
            updated_at=state.updated_at,
        )

    async def compute_indicators(self, context: MarketDataContext) -> dict[str, Any]:
        """合并 VN + SMC 子 monitor 的图表指标。

        单个子 monitor 失败只记录错误，不阻断其他项。

        Args:
            context: 市场数据上下文

        Returns:
            合并后的指标字典
        """
        result: dict[str, Any] = {}

        # VN
        try:
            vn_indicators = await self._vn.compute_indicators(context)
            result.update(vn_indicators)
        except Exception as exc:
            logger.warning("VolumeNodeMonitor.compute_indicators 失败（不阻断其他）: %s", exc)

        # SMC
        try:
            smc_indicators = await self._smc.compute_indicators(context)
            # SMC 指标放在 "smc" 命名空间下，避免与 VN 字段冲突
            result[NAMESPACE_SMC] = smc_indicators
        except Exception as exc:
            logger.warning("SmcMonitor.compute_indicators 失败（不阻断其他）: %s", exc)

        return result


if __name__ == "__main__":
    # 自测入口：验证 WatchlistMonitor 定义与子 monitor 委托（无副作用，不写库表）
    print(f"WatchlistMonitor.kind={WatchlistMonitor.kind}")
    assert WatchlistMonitor.kind == "monitor"

    # 验证继承
    assert issubclass(WatchlistMonitor, StrategyRuntime)
    print("WatchlistMonitor 继承 StrategyRuntime ✓")

    # 验证子 monitor 创建（仅 VN + SMC，不再有 BB）
    monitor = WatchlistMonitor()
    assert isinstance(monitor._vn, VolumeNodeMonitor)
    assert isinstance(monitor._smc, SmcMonitor)
    assert not hasattr(monitor, "_bb"), "WatchlistMonitor 不应再持有 BollingerMonitor"
    print("子 monitor VolumeNodeMonitor + SmcMonitor 创建 ✓（BB 已移除）")

    # 验证 state schema 版本
    assert STATE_VERSION == 3, f"STATE_VERSION 应为 3，实际 {STATE_VERSION}"
    print(f"STATE_VERSION={STATE_VERSION} ✓")

    # 验证 _extract_sub_state（命名空间优先）
    from datetime import UTC, datetime
    from uuid import uuid4

    test_state = MonitorState(
        instrument_id=uuid4(),
        strategy_version_id=uuid4(),
        state={
            # 命名空间
            "node_cluster": {
                "current_price": 9.5,
                "upper_node": {"price_mid": 10.5},
                "lower_node": {"price_mid": 8.5},
                "position_0_1": 0.5, "poc_price": None,
                "last_touched_node": None,
            },
            "smc": {
                "smc_confirmed_bos": [{"anchor_index": 100, "level": 10.0}],
                "smc_confirmed_choch": [],
                "smc_equal_highs_lows": [],
                "smc_active_obs": [],
                "smc_current_price": 9.5,
                "smc_currently_touched": {"BOS:100:10.0": False},
                "smc_swing_bias": 1,
                "smc_trailing": {},
                "smc_availability": "available",
                "smc_degraded_reason": None,
                "smc_episode_tracker": {"BOS:100:10.0": {"state": "watching"}},
            },
            "market": {
                "current_price": 9.5,
                "previous_close": 9.3,
                "change_pct": 2.15,
            },
            "degraded": {"node_cluster": False, "smc": False},
            # 顶层平铺兼容
            "current_price": 9.5,
            "upper_node": {"price_mid": 10.5},
            "smc_currently_touched": {"BOS:100:10.0": False},
        },
        state_version=3,
        updated_at=datetime.now(UTC),
    )

    vn_sub = WatchlistMonitor._extract_sub_state(test_state, "node_cluster")
    assert "upper_node" in vn_sub.state
    assert "smc_confirmed_bos" not in vn_sub.state
    print("_extract_sub_state(node_cluster) 命名空间优先 ✓")

    smc_sub = WatchlistMonitor._extract_sub_state(test_state, "smc")
    assert "smc_confirmed_bos" in smc_sub.state
    assert "smc_currently_touched" in smc_sub.state
    assert "smc_episode_tracker" in smc_sub.state
    assert "upper_node" not in smc_sub.state
    print("_extract_sub_state(smc) 命名空间优先 ✓")

    # [CHANGE-20260728-010] BB 仅历史读取兼容，返回空状态
    bb_sub = WatchlistMonitor._extract_sub_state(test_state, "bb")
    assert bb_sub.state == {}
    print("_extract_sub_state(bb) 历史兼容返回空 ✓")

    # 验证 canonical realtime SMC fail closed 时 transition 命名空间 carry-forward
    prev_state = MonitorState(
        instrument_id=uuid4(),
        strategy_version_id=uuid4(),
        state={"smc_realtime_transition": {"daily_epoch": "2026-09-11"}},
        state_version=3,
        updated_at=datetime.now(UTC),
    )
    curr_fail = MonitorState(
        instrument_id=uuid4(), strategy_version_id=uuid4(), state={}
    )
    WatchlistMonitor._carry_forward_transition_state(prev_state, curr_fail)
    assert curr_fail.state["smc_realtime_transition"] == {"daily_epoch": "2026-09-11"}
    WatchlistMonitor._record_realtime_smc_degraded(curr_fail, "proof failed")
    assert curr_fail.state["smc_realtime_degraded_reason"] == "proof failed"
    print("canonical SMC fail closed carry-forward transition state ✓")

    # 验证 fallback：无命名空间时从顶层平铺读取（兼容旧 state schema v1/v2）
    old_state = MonitorState(
        instrument_id=uuid4(),
        strategy_version_id=uuid4(),
        state={
            "upper_node": {"price_mid": 10.5},
            "lower_node": {"price_mid": 8.5},
            "position_0_1": 0.5, "poc_price": None,
            "last_touched_node": None,
            "current_price": 9.5,
            "smc_confirmed_bos": [{"anchor_index": 100, "level": 10.0}],
            "smc_episode_tracker": {"legacy": True},
        },
        state_version=2,
        updated_at=datetime.now(UTC),
    )
    vn_sub_old = WatchlistMonitor._extract_sub_state(old_state, "node_cluster")
    assert "upper_node" in vn_sub_old.state
    print("_extract_sub_state(node_cluster) fallback 平铺兼容 ✓")
    smc_sub_old = WatchlistMonitor._extract_sub_state(old_state, "smc")
    assert "smc_confirmed_bos" in smc_sub_old.state
    assert "smc_episode_tracker" in smc_sub_old.state
    print("_extract_sub_state(smc) fallback 平铺兼容 ✓")

    print("OK")
