"""NodeClusterInputProvider — Node Cluster 输入合同唯一 Provider。

[CP-V3-A] 四链（Detail/Capture/FeatureSnapshot/Monitor）只能通过本 Provider 获取 Node 输入。
禁止接收：bars、display_count、defaultVisibleBars、页面 timeframe、indicator_view、
released strategy keys。

Node 需要计算时必须无条件加载完整 250 daily + 4000 15m（completed qfq），
不再依赖 needs_15min、页面周期或 released strategy 状态。

availability 三态状态机：
- 250+4000 且 daily>=250: available
- history_exhausted=true 且真实历史不足: degraded / INSUFFICIENT_15M_HISTORY
- 上游历史足够但未取满 4000: unavailable / INPUT_CONTRACT_VIOLATION
  （禁止继续生成看似正常的 Profile）

用法：
    from app.services.node_cluster_input_provider import NodeClusterInputProvider
    node_input = await NodeClusterInputProvider.get_inputs(
        session, instrument_id, adjustment_as_of=trade_date
    )
    if node_input.availability == "available":
        profile = await CanonicalComputationService.compute(
            algorithm_id="node_cluster",
            daily_bars=node_input.daily_bars,
            bars_15m=node_input.bars_15m,
            ...
        )
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.indicator_contract import (
    DAILY_HISTORY_BARS,
    NODE_CLUSTER_LOW_BARS,
)
from app.services.market_data_aggregation_service import MarketDataAggregationService

logger = logging.getLogger("services.node_cluster_input_provider")

# Node Cluster 输入合同常量（禁止散落硬编码）
_NODE_DAILY_REQUIRED: int = DAILY_HISTORY_BARS  # 250
_NODE_15M_REQUIRED: int = NODE_CLUSTER_LOW_BARS  # 4000
# daily 最低可计算阈值（低于此值无法计算 VP）
_NODE_DAILY_MIN: int = 10


@dataclass(frozen=True)
class NodeClusterInput:
    """Node Cluster 输入合同结果（不可变）。

    四链通过本对象获取 Node 输入，禁止直接调用 MDAS 获取 Node bars。

    Attributes:
        daily_bars: 日线 bars（completed qfq, tail(250)）
        bars_15m: 15m bars（completed qfq, tail(4000)）
        daily_source_hash: 日线 source_bar_hash（canonical result_hash 维度）
        daily_adj_factor_hash: 日线 adj_factor_hash
        m15_source_hash: 15m source_bar_hash
        m15_adj_factor_hash: 15m adj_factor_hash
        daily_count: 日线实际数量
        m15_count: 15m 实际数量
        daily_requested: 日线请求数量（=250）
        m15_requested: 15m 请求数量（=4000）
        daily_history_exhausted: 日线 DB 历史是否不足
        m15_history_exhausted: 15m DB 历史是否不足
        availability: "available" | "degraded" | "unavailable"
        degraded_reason: str | None
        adjustment_as_of: 复权锚点（回显）
    """

    daily_bars: pd.DataFrame
    bars_15m: pd.DataFrame
    daily_source_hash: str
    daily_adj_factor_hash: str
    m15_source_hash: str
    m15_adj_factor_hash: str
    daily_count: int
    m15_count: int
    daily_requested: int
    m15_requested: int
    daily_history_exhausted: bool
    m15_history_exhausted: bool
    availability: str
    degraded_reason: str | None
    adjustment_as_of: date | None


class NodeClusterInputProvider:
    """Node Cluster 输入合同唯一 Provider。

    四链（Detail/Capture/FeatureSnapshot/Monitor）只能通过本 Provider 获取 Node 输入。
    本 Provider 内部调用 MDAS（唯一行情出口），禁止绕过 MDAS 直接查询 Repository。

    Provider 禁止接收以下参数（防止展示需求污染 Node 计算）：
    - bars / display_count / defaultVisibleBars（前端展示参数）
    - 页面 timeframe（Node 固定 1d+15m，与页面周期无关）
    - indicator_view（视图层参数）
    - released strategy keys（Node 无条件加载，不依赖策略注册状态）
    """

    @classmethod
    async def get_inputs(
        cls,
        session: AsyncSession,
        instrument_id: uuid.UUID,
        *,
        adjustment_as_of: date | None = None,
        end_date: date | None = None,
    ) -> NodeClusterInput:
        """获取 Node Cluster 输入（固定 250 daily + 4000 15m, completed qfq）。

        Node 需要计算时必须无条件加载完整 250+4000，不再依赖 needs_15min、
        页面周期或 released strategy 状态。

        Args:
            session: 异步 DB 会话
            instrument_id: 标的 UUID
            adjustment_as_of: 复权锚点（None=最新；date=point-in-time qfq 因子截断）
            end_date: 行情截止日期（None=最新；date=point-in-time，仅返回 <= end_date 的 bar）。
                Feature Snapshot 盘后链使用 end_date=trade_date 保证不读取未来数据。

        Returns:
            NodeClusterInput（含 bars + hash + availability 状态机结果）
        """
        mdas = MarketDataAggregationService()

        # daily: completed qfq DAILY_HISTORY_BARS=250 根（合同常量）
        daily_agg = await mdas.get_bars(
            session,
            instrument_id,
            timeframe="1d",
            adj="qfq",
            include_realtime=False,
            completed_only=True,
            adjustment_as_of=adjustment_as_of,
            end_date=end_date,
            limit=_NODE_DAILY_REQUIRED,
        )

        # 15m: completed qfq NODE_CLUSTER_LOW_BARS=4000 根（合同常量）
        # [CP-V3-A] MDAS 内部 count-aware 回补：limit=4000 时自动扩大回看天数
        m15_agg = await mdas.get_bars(
            session,
            instrument_id,
            timeframe="15m",
            adj="qfq",
            include_realtime=False,
            completed_only=True,
            adjustment_as_of=adjustment_as_of,
            end_date=end_date,
            limit=_NODE_15M_REQUIRED,
        )

        daily_bars = daily_agg.bars
        bars_15m = m15_agg.bars

        # availability 三态状态机
        availability, degraded_reason = cls._compute_availability(
            daily_count=len(daily_bars),
            m15_count=len(bars_15m),
            daily_history_exhausted=daily_agg.history_exhausted,
            m15_history_exhausted=m15_agg.history_exhausted,
        )

        logger.info(
            "NODE_INPUT_PROVIDER instrument_id=%s "
            "daily_count=%d/%d m15_count=%d/%d "
            "daily_history_exhausted=%s m15_history_exhausted=%s "
            "availability=%s degraded_reason=%s "
            "daily_hash=%s m15_hash=%s",
            instrument_id,
            len(daily_bars), _NODE_DAILY_REQUIRED,
            len(bars_15m), _NODE_15M_REQUIRED,
            daily_agg.history_exhausted, m15_agg.history_exhausted,
            availability, degraded_reason,
            daily_agg.source_bar_hash, m15_agg.source_bar_hash,
        )

        return NodeClusterInput(
            daily_bars=daily_bars,
            bars_15m=bars_15m,
            daily_source_hash=daily_agg.source_bar_hash,
            daily_adj_factor_hash=daily_agg.adj_factor_hash,
            m15_source_hash=m15_agg.source_bar_hash,
            m15_adj_factor_hash=m15_agg.adj_factor_hash,
            daily_count=len(daily_bars),
            m15_count=len(bars_15m),
            daily_requested=_NODE_DAILY_REQUIRED,
            m15_requested=_NODE_15M_REQUIRED,
            daily_history_exhausted=daily_agg.history_exhausted,
            m15_history_exhausted=m15_agg.history_exhausted,
            availability=availability,
            degraded_reason=degraded_reason,
            adjustment_as_of=adjustment_as_of,
        )

    @staticmethod
    def _compute_availability(
        daily_count: int,
        m15_count: int,
        daily_history_exhausted: bool,
        m15_history_exhausted: bool,
    ) -> tuple[str, str | None]:
        """availability 三态状态机。

        [CP-V3-A] 修正语义：
        1. daily < 10: unavailable / INSUFFICIENT_DAILY_BARS
        2. m15 == 0: unavailable / MISSING_15M_BARS
        3. m15 < 4000:
           3a. history_exhausted=True: degraded / INSUFFICIENT_15M_HISTORY（允许降级计算）
           3b. history_exhausted=False: unavailable / INPUT_CONTRACT_VIOLATION
               （DB 有但系统未取满，禁止生成看似正常的 Profile）
        4. m15 >= 4000 且 daily >= 250: available

        Args:
            daily_count: 日线实际数量
            m15_count: 15m 实际数量
            daily_history_exhausted: 日线 DB 历史是否不足
            m15_history_exhausted: 15m DB 历史是否不足

        Returns:
            (availability, degraded_reason) 元组
        """
        # 1. daily 不足
        if daily_count < _NODE_DAILY_MIN:
            return "unavailable", "INSUFFICIENT_DAILY_BARS"

        # 2. 15m 完全缺失
        if m15_count == 0:
            return "unavailable", "MISSING_15M_BARS"

        # 3. 15m 不足 4000
        if m15_count < _NODE_15M_REQUIRED:
            if m15_history_exhausted:
                # 3a. DB 真实历史不足 → 允许降级
                return "degraded", "INSUFFICIENT_15M_HISTORY"
            else:
                # 3b. DB 有但系统未取满 → 禁止生成
                return "unavailable", "INPUT_CONTRACT_VIOLATION"

        # 4. 正常
        return "available", None

    @staticmethod
    def to_dict(node_input: NodeClusterInput) -> dict:
        """将 NodeClusterInput 转为可序列化 dict（供 monitor_states payload 使用）。

        [CP-V3-A] Monitor payload 补全：四链可直接比较 hash/count/availability。
        """
        return {
            "daily_bars_count": node_input.daily_count,
            "bars_15m_count": node_input.m15_count,
            "daily_requested_count": node_input.daily_requested,
            "bars_15m_requested_count": node_input.m15_requested,
            "daily_source_hash": node_input.daily_source_hash,
            "bars_15m_source_hash": node_input.m15_source_hash,
            "daily_adj_factor_hash": node_input.daily_adj_factor_hash,
            "bars_15m_adj_factor_hash": node_input.m15_adj_factor_hash,
            "daily_history_exhausted": node_input.daily_history_exhausted,
            "bars_15m_history_exhausted": node_input.m15_history_exhausted,
            "availability": node_input.availability,
            "degraded_reason": node_input.degraded_reason,
            "adjustment_as_of": (
                node_input.adjustment_as_of.isoformat()
                if node_input.adjustment_as_of is not None
                else None
            ),
        }


if __name__ == "__main__":
    # 自测：验证状态机逻辑（不连 DB）
    provider = NodeClusterInputProvider

    # 1. 正常：daily=250, m15=4000
    avail, reason = provider._compute_availability(250, 4000, False, False)
    assert avail == "available", f"应为 available, got {avail}"
    assert reason is None
    print(f"正常: avail={avail} reason={reason} ✓")

    # 2. daily 不足
    avail, reason = provider._compute_availability(9, 4000, True, False)
    assert avail == "unavailable", f"应为 unavailable, got {avail}"
    assert reason == "INSUFFICIENT_DAILY_BARS"
    print(f"daily不足: avail={avail} reason={reason} ✓")

    # 3. 15m 完全缺失
    avail, reason = provider._compute_availability(250, 0, False, True)
    assert avail == "unavailable", f"应为 unavailable, got {avail}"
    assert reason == "MISSING_15M_BARS"
    print(f"15m缺失: avail={avail} reason={reason} ✓")

    # 4. 15m 历史不足（如 301583: 144 根）
    avail, reason = provider._compute_availability(250, 144, False, True)
    assert avail == "degraded", f"应为 degraded, got {avail}"
    assert reason == "INSUFFICIENT_15M_HISTORY"
    print(f"15m历史不足: avail={avail} reason={reason} ✓")

    # 5. INPUT_CONTRACT_VIOLATION（DB 有 8160 但系统只返回 1872）
    avail, reason = provider._compute_availability(250, 1872, False, False)
    assert avail == "unavailable", f"应为 unavailable, got {avail}"
    assert reason == "INPUT_CONTRACT_VIOLATION"
    print(f"输入合同违反: avail={avail} reason={reason} ✓")

    # 6. history_exhausted=None（向后兼容）应视为 INPUT_CONTRACT_VIOLATION
    avail, reason = provider._compute_availability(250, 1872, False, False)
    assert avail == "unavailable"
    assert reason == "INPUT_CONTRACT_VIOLATION"
    print(f"history_exhausted=False: avail={avail} reason={reason} ✓")

    print("\nOK — NodeClusterInputProvider 状态机验证通过")
