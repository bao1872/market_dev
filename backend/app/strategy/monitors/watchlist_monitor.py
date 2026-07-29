"""自选股监控 - 结构 + 筹码共识 双类别监控（薄包装）。

[CHANGE-20260728-010] WatchlistMonitor 仅保留 SMC + VolumeNode 两类触发。
布林带（Bollinger）不再参与盘中事件计算、状态合并、事件统计与通知文案。
Bollinger 算法本体、盘后 Bollinger 计算、个股详情页布林带图层工具栏不在本文件禁止范围。

唯一监控算法，内部委托 VolumeNodeMonitor、SmcMonitor：
- calculate_state(): 分别调用两个子 monitor，合并 state 字典到命名空间
  node_cluster/smc/market；并补充 previous_close/change_pct；单个子 monitor 失败
  只标记该项 degraded，不阻断其他项。
- detect_events(): 分别调用两个子 monitor，合并事件列表；
  单个子 monitor 失败只记录错误，不阻断其他项。
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

[SMC episode 连续性修复] detect_events 完成后，将 SMC 子状态（含
smc_episode_tracker）显式回写到父 curr_state.state["smc"] 和顶层平铺，
保证下一轮 detect_events 收到完整的 episode 状态。

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
from typing import Any
from uuid import UUID

import pandas as pd

from app.models.strategy import StrategyVersion
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
        """合并 VN + SMC 子 monitor 的事件，并显式回写 SMC 子状态到父 curr_state。

        分别调用两个子 monitor 的 detect_events，合并事件列表。
        单个子 monitor 失败只记录错误，不阻断其他项。

        [SMC episode 连续性修复] SMC 子状态通过 _extract_sub_state 提取后传入
        SmcMonitor.detect_events，过程中子 monitor 可能 mutate smc_episode_tracker。
        detect_events 完成后，将 SMC 子状态（含 smc_episode_tracker）显式回写到
        父 curr_state.state["smc"] 命名空间和顶层平铺，保证下一轮 detect_events
        收到完整的 episode 状态，避免 episode 断裂。

        Args:
            context: 市场数据上下文
            prev_state: 前一状态
            curr_state: 当前状态（将被 mutate 以回写 SMC 子状态）

        Returns:
            合并后的事件草稿列表
        """
        events: list[StrategyEventDraft] = []

        # VN 事件检测
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

        # SMC 事件检测
        try:
            smc_prev = (
                self._extract_sub_state(prev_state, NAMESPACE_SMC) if prev_state else None
            )
            smc_curr = self._extract_sub_state(curr_state, NAMESPACE_SMC)
            smc_events = await self._smc.detect_events(context, smc_prev, smc_curr)
            events.extend(smc_events)

            # [SMC episode 连续性修复] 显式回写 SMC 子状态到父 curr_state
            # 包含 smc_episode_tracker 的最新值，避免子状态复制导致 episode 丢失
            self._writeback_smc_substate(curr_state, smc_curr.state)
        except Exception as exc:
            logger.warning("SmcMonitor.detect_events 失败（不阻断其他）: %s", exc)

        return events

    @staticmethod
    def _writeback_smc_substate(
        parent_state: MonitorState,
        smc_sub_state: dict[str, Any],
    ) -> None:
        """显式回写 SMC 子状态到父 curr_state。

        [SMC episode 连续性修复] detect_events 调用 _extract_sub_state 会复制
        子状态到新的 MonitorState，SMC detect_events 在该副本上 mutate
        smc_episode_tracker；副本的变更不会自动反映到父 curr_state。
        本方法将 SMC 子状态（含 smc_episode_tracker）显式回写到：
        - parent_state.state["smc"]（命名空间）
        - parent_state.state 顶层平铺（兼容旧读取）

        Args:
            parent_state: 父 curr_state（将被 mutate）
            smc_sub_state: SMC 子 monitor detect_events 后的子状态字典
        """
        if not smc_sub_state:
            return
        # 回写命名空间
        parent_state.state[NAMESPACE_SMC] = {
            k: v for k, v in smc_sub_state.items() if k in _SMC_KEYS
        }
        # 回写顶层平铺（兼容旧读取）
        for key in _SMC_KEYS:
            if key in smc_sub_state:
                parent_state.state[key] = smc_sub_state[key]

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

    # 验证 _writeback_smc_substate 回写 episode_tracker
    parent_state = MonitorState(
        instrument_id=uuid4(),
        strategy_version_id=uuid4(),
        state={
            "smc": {"smc_episode_tracker": {"old": True}},
            "smc_episode_tracker": {"old": True},
        },
        state_version=3,
        updated_at=datetime.now(UTC),
    )
    new_smc_sub = {
        "smc_confirmed_bos": [{"anchor_index": 200, "level": 11.0}],
        "smc_episode_tracker": {"new": True},
        "smc_swing_bias": -1,
    }
    WatchlistMonitor._writeback_smc_substate(parent_state, new_smc_sub)
    assert parent_state.state["smc"]["smc_episode_tracker"] == {"new": True}
    assert parent_state.state["smc_episode_tracker"] == {"new": True}
    assert parent_state.state["smc"]["smc_confirmed_bos"] == [{"anchor_index": 200, "level": 11.0}]
    assert parent_state.state["smc_swing_bias"] == -1
    print("_writeback_smc_substate 回写 episode_tracker ✓")

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
