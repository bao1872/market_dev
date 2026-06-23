"""自选股监控 - 统一监控算法（BB + VN 薄包装）。

唯一监控算法，内部委托 BollingerMonitor 和 VolumeNodeMonitor：
- calculate_state(): 分别调用两个子 monitor，合并 state 字典
- detect_events(): 分别调用两个子 monitor，合并事件列表
- compute_indicators(): 分别调用两个子 monitor，合并指标字典

这不是新算法，而是对现有 BB 和 VN 监控的薄包装（thin wrapper），
保证逻辑唯一性：所有计算逻辑仍在 BollingerMonitor/VolumeNodeMonitor 中。

用法（模块自测）：
    python -m app.strategy.monitors.watchlist_monitor
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.models.strategy import StrategyVersion
from app.strategy.monitors.bollinger_monitor import BollingerMonitor
from app.strategy.monitors.volume_node_monitor import VolumeNodeMonitor
from app.strategy.runtime import (
    MarketDataContext,
    MonitorState,
    StrategyEventDraft,
    StrategyRuntime,
)

logger = logging.getLogger("strategy.monitors.watchlist_monitor")


class WatchlistMonitor(StrategyRuntime):
    """自选股监控 - 统一监控算法（BB + VN 薄包装）。

    内部持有 BollingerMonitor 和 VolumeNodeMonitor 实例，
    将 calculate_state/detect_events/compute_indicators 委托给子 monitor，
    合并结果后返回。

    生命周期：
    1. StrategyLoader.load(version) 创建实例
    2. initialize(version) 创建子 monitor 实例并分别初始化
    3. calculate_state(context) 合并 BB + VN 状态
    4. detect_events(context, prev, curr) 合并 BB + VN 事件
    """

    kind = "monitor"

    def __init__(self) -> None:
        self._bb: BollingerMonitor = BollingerMonitor()
        self._vn: VolumeNodeMonitor = VolumeNodeMonitor()
        self._strategy_version_id: UUID | None = None

    async def initialize(self, version: StrategyVersion) -> None:
        """创建子 monitor 实例并分别初始化。

        将同一个 StrategyVersion 传递给两个子 monitor，
        各自从 manifest 中提取所需参数。

        Args:
            version: 策略版本 ORM 对象
        """
        self._strategy_version_id = version.id
        await self._bb.initialize(version)
        await self._vn.initialize(version)
        logger.info(
            "WatchlistMonitor 初始化完成: bb_win=%d, bb_k=%.1f, lookback=%d",
            self._bb._bb_win, self._bb._bb_k, self._vn._lookback,
        )

    async def execute(self, context: MarketDataContext) -> Any:  # type: ignore[override]
        """selector 执行接口（monitor 不支持）。"""
        raise NotImplementedError(
            "WatchlistMonitor 是 monitor 策略，不支持 execute（请使用 calculate_state + detect_events）"
        )

    async def calculate_state(self, context: MarketDataContext) -> MonitorState:
        """合并 BB + VN 子 monitor 的状态。

        分别调用 BollingerMonitor.calculate_state() 和
        VolumeNodeMonitor.calculate_state()，合并 state 字典。

        Args:
            context: 市场数据上下文

        Returns:
            合并后的监控状态（BB 字段 + VN 字段）
        """
        bb_state = await self._bb.calculate_state(context)
        vn_state = await self._vn.calculate_state(context)

        # 合并 state 字典（VN 的 current_price 覆盖 BB 的，两者语义相同）
        merged_state: dict[str, Any] = {**bb_state.state, **vn_state.state}

        bar_time = bb_state.updated_at or vn_state.updated_at

        return MonitorState(
            instrument_id=context.instrument_id,
            strategy_version_id=self._strategy_version_id,  # type: ignore[arg-type]
            state=merged_state,
            state_version=1,
            updated_at=bar_time,
        )

    async def detect_events(
        self,
        context: MarketDataContext,
        prev_state: MonitorState | None,
        curr_state: MonitorState,
    ) -> list[StrategyEventDraft]:
        """合并 BB + VN 子 monitor 的事件。

        分别调用 BollingerMonitor.detect_events() 和
        VolumeNodeMonitor.detect_events()，合并事件列表。

        Args:
            context: 市场数据上下文
            prev_state: 前一状态
            curr_state: 当前状态

        Returns:
            合并后的事件草稿列表
        """
        # BB 事件检测需要 BB 的 prev/curr state
        bb_prev = self._extract_sub_state(prev_state, "bb") if prev_state else None
        bb_curr = self._extract_sub_state(curr_state, "bb")
        bb_events = await self._bb.detect_events(context, bb_prev, bb_curr)

        # VN 事件检测需要 VN 的 prev/curr state
        vn_prev = self._extract_sub_state(prev_state, "vn") if prev_state else None
        vn_curr = self._extract_sub_state(curr_state, "vn")
        vn_events = await self._vn.detect_events(context, vn_prev, vn_curr)

        return bb_events + vn_events

    @staticmethod
    def _extract_sub_state(
        state: MonitorState, sub: str
    ) -> MonitorState:
        """从合并状态中提取子 monitor 状态。

        BB 字段: bb_upper/bb_mid/bb_lower/current_price/prev_close/bb_width/bb_pos
        VN 字段: current_price/upper_node/lower_node/position_0_1/poc_price/last_touched_node

        Args:
            state: 合并后的 MonitorState
            sub: "bb" 或 "vn"

        Returns:
            包含子 monitor 字段的 MonitorState
        """
        bb_keys = {"bb_upper", "bb_mid", "bb_lower", "current_price", "prev_close", "bb_width", "bb_pos"}
        vn_keys = {"current_price", "upper_node", "lower_node", "position_0_1", "poc_price", "last_touched_node"}

        keys = bb_keys if sub == "bb" else vn_keys
        sub_state = {k: v for k, v in state.state.items() if k in keys}

        return MonitorState(
            instrument_id=state.instrument_id,
            strategy_version_id=state.strategy_version_id,
            state=sub_state,
            state_version=state.state_version,
            updated_at=state.updated_at,
        )

    async def compute_indicators(self, context: MarketDataContext) -> dict[str, Any]:
        """合并 BB + VN 子 monitor 的图表指标。

        Args:
            context: 市场数据上下文

        Returns:
            合并后的指标字典
        """
        bb_indicators = await self._bb.compute_indicators(context)
        vn_indicators = await self._vn.compute_indicators(context)
        return {**bb_indicators, **vn_indicators}


if __name__ == "__main__":
    # 自测入口：验证 WatchlistMonitor 定义与子 monitor 委托（无副作用，不写库表）
    print(f"WatchlistMonitor.kind={WatchlistMonitor.kind}")
    assert WatchlistMonitor.kind == "monitor"

    # 验证继承
    assert issubclass(WatchlistMonitor, StrategyRuntime)
    print("WatchlistMonitor 继承 StrategyRuntime ✓")

    # 验证子 monitor 创建
    monitor = WatchlistMonitor()
    assert isinstance(monitor._bb, BollingerMonitor)
    assert isinstance(monitor._vn, VolumeNodeMonitor)
    print("子 monitor BollingerMonitor + VolumeNodeMonitor 创建 ✓")

    # 验证 _extract_sub_state
    from uuid import uuid4
    from datetime import UTC, datetime

    test_state = MonitorState(
        instrument_id=uuid4(),
        strategy_version_id=uuid4(),
        state={
            "bb_upper": 10.0, "bb_mid": 9.0, "bb_lower": 8.0,
            "current_price": 9.5, "prev_close": 9.3,
            "bb_width": 0.22, "bb_pos": 0.75,
            "upper_node": {"price_mid": 10.5},
            "lower_node": {"price_mid": 8.5},
            "position_0_1": 0.5, "poc_price": None,
            "last_touched_node": None,
        },
        state_version=1,
        updated_at=datetime.now(UTC),
    )

    bb_sub = WatchlistMonitor._extract_sub_state(test_state, "bb")
    assert "bb_upper" in bb_sub.state
    assert "upper_node" not in bb_sub.state
    print("_extract_sub_state(bb) ✓")

    vn_sub = WatchlistMonitor._extract_sub_state(test_state, "vn")
    assert "upper_node" in vn_sub.state
    assert "bb_upper" not in vn_sub.state
    print("_extract_sub_state(vn) ✓")

    print("OK")
