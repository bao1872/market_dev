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
from decimal import Decimal

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.indicator_contract import (
    DAILY_HISTORY_BARS,
    NODE_CLUSTER_LOW_BARS,
)
from app.services.business_date_adjustment_context import (
    BusinessDateAdjustmentContext,
    BusinessDateAdjustmentService,
)
from app.services.market_data_aggregation_service import MarketDataAggregationService


class NodeAdjustmentContextMismatchError(RuntimeError):
    """Node 输入与上游传入的 BusinessDateAdjustmentContext 身份冲突（fail-closed）。

    与 B2B 的 BusinessDateAdjustmentUnavailableError 区分：后者是「坐标本身不可信」，
    本错误是「调用方传入的 Context 与本次 Node 输入请求参数不一致」（instrument_id /
    adjustment_as_of / end_date 冲突）。两类都属于 preflight 阶段、零 MDAS I/O。
    """

    def __init__(
        self,
        *,
        instrument_id: uuid.UUID,
        reason: str,
    ) -> None:
        self.instrument_id = instrument_id
        self.reason = reason
        super().__init__(
            "node adjustment context mismatch "
            f"instrument_id={instrument_id} reason={reason}"
        )

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
        adjustment_context_hash: 复权坐标版本身份（Context mode）；
            Legacy mode 为 None。下一阶段 Node Target version 的输入维度之一，
            与 daily/m15 source hash 正交：source 不变但除权坐标变 → 此值变。
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
    adjustment_context_hash: str | None = None


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
        adjustment_context: BusinessDateAdjustmentContext | None = None,
    ) -> NodeClusterInput:
        """获取 Node Cluster 输入（固定 250 daily + 4000 15m）。

        Node 需要计算时必须无条件加载完整 250+4000，不再依赖 needs_15min、
        页面周期或 released strategy 状态。

        双模式：
        - Legacy mode（``adjustment_context is None``）：MDAS 直接返回 completed qfq，
          行为与历史完全一致；``adjustment_context_hash=None``。
        - Context mode（``adjustment_context is not None``）：MDAS 只返回
          ``adj="none"`` 的 completed raw bars，复权由 BusinessDateAdjustmentContext
          统一施加。Context 必须由上层 orchestration 预先构建并显式传入（Provider
          自身不构造、不触发 XDXR force refresh）。

        Args:
            session: 异步 DB 会话
            instrument_id: 标的 UUID
            adjustment_as_of: 复权锚点（Legacy mode 透传给 MDAS；Context mode 必须与
                Context.business_date 一致，否则冲突）。
            end_date: 行情截止日期（None=最新；date=point-in-time，仅返回 <= end_date 的 bar）。
                Context mode 下 end_date=None 时默认取 Context.business_date，
                保证 point-in-time 不读取未来 bars。
            adjustment_context: 预构建的 business-date 复权坐标（C1 接入点）。

        Returns:
            NodeClusterInput（含 bars + hash + availability 状态机结果）
        """
        mdas = MarketDataAggregationService()

        if adjustment_context is None:
            return await cls._get_inputs_legacy(
                mdas, session, instrument_id,
                adjustment_as_of=adjustment_as_of, end_date=end_date,
            )

        cls._validate_adjustment_context(
            adjustment_context, instrument_id,
            adjustment_as_of=adjustment_as_of, end_date=end_date,
        )

        context_service = BusinessDateAdjustmentService()

        # Context mode：end_date=None 时锁定为 Context.business_date（禁止未来泄漏）
        effective_end_date = (
            end_date if end_date is not None else adjustment_context.business_date
        )

        # MDAS 只负责唯一行情出口 + completed raw bars（adj="none"，不复权）
        daily_agg = await mdas.get_bars(
            session,
            instrument_id,
            timeframe="1d",
            adj="none",
            include_realtime=False,
            completed_only=True,
            end_date=effective_end_date,
            limit=_NODE_DAILY_REQUIRED,
        )
        m15_agg = await mdas.get_bars(
            session,
            instrument_id,
            timeframe="15m",
            adj="none",
            include_realtime=False,
            completed_only=True,
            end_date=effective_end_date,
            limit=_NODE_15M_REQUIRED,
        )

        raw_daily = daily_agg.bars
        raw_m15 = m15_agg.bars

        # 复权 owner 从 MDAS canonical DB factor 切换为 BusinessDateAdjustmentContext
        daily_bars = (
            context_service.apply_context_qfq(
                raw_daily, adjustment_context, intraday=False,
            )
            if not raw_daily.empty else raw_daily
        )
        bars_15m = (
            context_service.apply_context_qfq(
                raw_m15, adjustment_context, intraday=True,
            )
            if not raw_m15.empty else raw_m15
        )

        availability, degraded_reason = cls._compute_availability(
            daily_count=len(daily_bars),
            m15_count=len(bars_15m),
            daily_history_exhausted=daily_agg.history_exhausted,
            m15_history_exhausted=m15_agg.history_exhausted,
        )

        logger.info(
            "NODE_INPUT_PROVIDER instrument_id=%s context_mode=True "
            "daily_count=%d/%d m15_count=%d/%d "
            "daily_history_exhausted=%s m15_history_exhausted=%s "
            "availability=%s degraded_reason=%s "
            "daily_source_hash=%s m15_source_hash=%s "
            "adjustment_context_hash=%s",
            instrument_id,
            len(daily_bars), _NODE_DAILY_REQUIRED,
            len(bars_15m), _NODE_15M_REQUIRED,
            daily_agg.history_exhausted, m15_agg.history_exhausted,
            availability, degraded_reason,
            daily_agg.source_bar_hash, m15_agg.source_bar_hash,
            adjustment_context.context_hash,
        )

        return NodeClusterInput(
            daily_bars=daily_bars,
            bars_15m=bars_15m,
            daily_source_hash=daily_agg.source_bar_hash,
            # Context mode：factor identity 来自 Context（daily/15m 共享 business-date 坐标）
            daily_adj_factor_hash=adjustment_context.factor_hash,
            m15_source_hash=m15_agg.source_bar_hash,
            m15_adj_factor_hash=adjustment_context.factor_hash,
            daily_count=len(daily_bars),
            m15_count=len(bars_15m),
            daily_requested=_NODE_DAILY_REQUIRED,
            m15_requested=_NODE_15M_REQUIRED,
            daily_history_exhausted=daily_agg.history_exhausted,
            m15_history_exhausted=m15_agg.history_exhausted,
            availability=availability,
            degraded_reason=degraded_reason,
            adjustment_as_of=adjustment_context.business_date,
            adjustment_context_hash=adjustment_context.context_hash,
        )

    @classmethod
    async def _get_inputs_legacy(
        cls,
        mdas: MarketDataAggregationService,
        session: AsyncSession,
        instrument_id: uuid.UUID,
        *,
        adjustment_as_of: date | None = None,
        end_date: date | None = None,
    ) -> NodeClusterInput:
        """Legacy mode：MDAS 直接返回 completed qfq（与历史行为完全一致）。

        C1 不修改任何 Legacy 语义；仅新增 ``adjustment_context_hash=None`` 字段。
        """
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

        availability, degraded_reason = cls._compute_availability(
            daily_count=len(daily_bars),
            m15_count=len(bars_15m),
            daily_history_exhausted=daily_agg.history_exhausted,
            m15_history_exhausted=m15_agg.history_exhausted,
        )

        logger.info(
            "NODE_INPUT_PROVIDER instrument_id=%s context_mode=False "
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
            adjustment_context_hash=None,
        )

    @classmethod
    def _validate_adjustment_context(
        cls,
        adjustment_context: BusinessDateAdjustmentContext,
        instrument_id: uuid.UUID,
        *,
        adjustment_as_of: date | None,
        end_date: date | None,
    ) -> None:
        """Context mode 的零 I/O preflight（任何 MDAS 调用之前）。

        - instrument_id 必须一致；
        - adjustment_as_of 若显式给出必须与 Context.business_date 一致（不静默选边）；
        - end_date 不得晚于 Context.business_date（禁止未来泄漏）；
        - 复用 B2B 公共消费入口 ``quote_qfq_price(Decimal("1"), context)`` 完成完整
          integrity 校验（freshness / factor_hash / context_hash / 派生 invariant）。
          任何 B2B 不可用 / 篡改都直接传播 BusinessDateAdjustmentUnavailableError，
          **绝不** fallback 到 legacy MDAS qfq。
        """
        if adjustment_context.instrument_id != instrument_id:
            raise NodeAdjustmentContextMismatchError(
                instrument_id=instrument_id,
                reason="instrument_id_mismatch",
            )

        if (
            adjustment_as_of is not None
            and adjustment_as_of != adjustment_context.business_date
        ):
            raise NodeAdjustmentContextMismatchError(
                instrument_id=instrument_id,
                reason="adjustment_as_of_mismatch",
            )

        if end_date is not None and end_date > adjustment_context.business_date:
            raise NodeAdjustmentContextMismatchError(
                instrument_id=instrument_id,
                reason="end_date_after_context_business_date",
            )

        # 零 I/O：完整 integrity 校验，失败直接传播 B2B 错误（禁止降级为 legacy qfq）
        BusinessDateAdjustmentService().quote_qfq_price(
            Decimal("1"), adjustment_context,
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
            "adjustment_context_hash": node_input.adjustment_context_hash,
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
