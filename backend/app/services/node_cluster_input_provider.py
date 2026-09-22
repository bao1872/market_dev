"""NodeClusterInputProvider — Node Cluster 输入合同唯一 Provider。

[CP-V3-A] 四链（Detail/Capture/FeatureSnapshot/Monitor）只能通过本 Provider 获取 Node 输入。
禁止接收：bars、display_count、defaultVisibleBars、页面 timeframe、indicator_view、
released strategy keys。

Node 需要计算时必须无条件加载完整 250 daily + 4000 15m（completed qfq），
不再依赖 needs_15min、页面周期或 released strategy 状态。

availability 三态状态机（基于权威 listing_date + 交易日历安全上界证明）：
- 250 daily + 4000 completed qfq 15m 且两者均达标: available
- 任一维度不足，且基于 listing_date + 交易日历证明“理论最大可能历史仍 < required”（真实新股/次新股）: degraded
- 任一维度不足，但无法证明历史耗尽（DB 缺口/同步失败/覆盖不足/listing_date=NULL）:
  unavailable / INPUT_CONTRACT_VIOLATION（禁止继续生成看似正常的 Profile）
- daily < 10: unavailable / INSUFFICIENT_DAILY_BARS（绝对下限，与 250 生产合同分离）

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
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.indicator_contract import (
    DAILY_HISTORY_BARS,
    NODE_CLUSTER_LOW_BARS,
)
from app.core.time import now_shanghai
from app.services.business_date_adjustment_context import (
    BusinessDateAdjustmentContext,
    BusinessDateAdjustmentService,
)
from app.services.market_data_aggregation_service import (
    MarketDataAggregationService,
    MarketDataSourcePolicy,
)
from app.services.market_data_aggregation_service import (
    _filter_unfinished_15m_bars as _filter_completed_15m_bars,
)


class NodeClusterSourceMode(StrEnum):
    """Node Cluster 输入的数据来源模式（**显式业务参数**，禁止推断）。

    [PANJI-INTRADAY-DIRECT-SOURCE] 这是本次最重要的语义拆分：Node Cluster 算法
    本身没变（仍是 ``1d × 250 + 15m × 4000``），变的是**这份 15m 从哪来**。

    - ``LIVE_DIRECT``（默认）：当前图表 / 实时监控链。15m 走 ``PROVIDER_DIRECT``
      —— 实时分钟行情归 Provider，不读 DB 旧分钟线。
    - ``HISTORICAL_DB``：历史 / PIT 链（历史快照重建、as-of replay、回测）。
      15m 走 ``DB_ONLY`` —— PIT **绝对禁止访问网络**，否则今天去 provider 拉
      「最近 4000 根」再切到历史时点，就是把未来数据污染进历史结果。

    禁止用 ``adjustment_as_of is None`` 之类的间接特征猜模式：Capture / Monitor /
    当前交易日计算都可能显式传 ``adjustment_as_of=today``，它们仍然是 live。
    """

    LIVE_DIRECT = "live_direct"
    HISTORICAL_DB = "historical_db"


def _resolve_node_source_policies(
    source_mode: NodeClusterSourceMode,
) -> tuple[MarketDataSourcePolicy, MarketDataSourcePolicy]:
    """Node 输入的 source policy 唯一判定点，返回 ``(daily_policy, m15_policy)``。

    [PANJI-INTRADAY-DIRECT-SOURCE] 必须**同时**给出 daily 与 15m 两条策略：
    ``HISTORICAL_DB`` 这个名字承诺的是「整条 Node 输入都来自 DB」，
    只把 15m 切成 DB_ONLY 而 daily 留在 HYBRID 是不成立的 ——
    daily HYBRID 在 DB 缺目标日期尾部时会 ``fetch_daily_bars`` 访问 Pytdx，
    于是 PIT 路径仍然联网，名字与业务合同不符。

    - ``HISTORICAL_DB``（历史 / PIT）：daily 与 15m **都是 DB_ONLY**。
      PIT 绝对禁止访问网络：今天去 provider 拉「最近 4000 根」再切到历史时点，
      就是把未来数据污染进历史结果。
    - ``LIVE_DIRECT``（当前图表 / 实时监控）：daily 保持 ``HYBRID``（行为不变），
      15m 走 ``PROVIDER_DIRECT``（实时分钟行情归 Provider）。
    """
    if source_mode == NodeClusterSourceMode.HISTORICAL_DB:
        return (
            MarketDataSourcePolicy.DB_ONLY,  # daily：PIT 不触网
            MarketDataSourcePolicy.DB_ONLY,  # 15m：PIT 不触网
        )
    return (
        MarketDataSourcePolicy.HYBRID,           # daily：区间读取，行为不变
        MarketDataSourcePolicy.PROVIDER_DIRECT,  # 15m：实时分钟归 Provider
    )


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
# A 股 15m 每交易日最大槽位数：2 交易时段 × 120min / 15min。
# 仅用于“理论最大可能历史”安全上界证明（保守 over-estimate，永不低估）。
_MAX_15M_SLOTS_PER_TRADING_DAY: int = 16


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
        source_mode: NodeClusterSourceMode = NodeClusterSourceMode.LIVE_DIRECT,
    ) -> NodeClusterInput:
        """获取 Node Cluster 输入（固定 250 daily + 4000 15m）。

        Node 需要计算时必须无条件加载完整 250+4000，不再依赖 needs_15min、
        页面周期或 released strategy 状态。

        Source mode（[PANJI-INTRADAY-DIRECT-SOURCE]）：
        - ``LIVE_DIRECT``（默认）：daily → ``HYBRID``，15m → ``PROVIDER_DIRECT``。
        - ``HISTORICAL_DB``：daily **与** 15m 均 → ``DB_ONLY``（PIT 禁止访问网络）。
        这是**显式业务参数**，禁止从 adjustment_as_of / end_date 推断。

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
            source_mode: Node 15m 输入的数据来源模式（live_direct / historical_db）。

        Returns:
            NodeClusterInput（含 bars + hash + availability 状态机结果）
        """
        mdas = MarketDataAggregationService()

        if adjustment_context is None:
            return await cls._get_inputs_legacy(
                mdas, session, instrument_id,
                adjustment_as_of=adjustment_as_of, end_date=end_date,
                source_mode=source_mode,
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
        daily_policy, m15_policy = _resolve_node_source_policies(source_mode)

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
            source_policy=daily_policy,
        )
        m15_agg = await mdas.get_bars(
            session,
            instrument_id,
            timeframe="15m",
            adj="none",
            # [USER-FIX-3 / C] completed 语义下读取「当日已完成的实时尾部」：
            # 否则盘中（无外部 15m 落库任务）只能拿到上一交易日收盘为止的 stale 15m。
            # MDAS 已按 completed_only 语义在合并前剔除 forming bar，其返回结果本身
            # 全部为已完成 bar；下方 _filter_unfinished_15m_bars 仅为 defensive 复核。
            # [PANJI-INTRADAY-DIRECT-SOURCE] provider_direct 分支不读 DB，同样在 provider
            # 侧用同一 owner 剔除 forming bar；DB_ONLY 分支则由 MDAS 强制禁用 fresh tail。
            include_realtime=True,
            completed_only=True,
            fresh_intraday_tail=True,
            end_date=effective_end_date,
            limit=_NODE_15M_REQUIRED,
            source_policy=m15_policy,
        )

        raw_daily = daily_agg.bars
        raw_m15 = cls._filter_unfinished_15m_bars(m15_agg.bars, now_shanghai())

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

        proofs = await cls._compute_exhaustion_proofs(session, instrument_id, effective_end_date)
        availability, degraded_reason = cls._compute_availability(
            daily_count=len(daily_bars),
            m15_count=len(bars_15m),
            daily_proof=proofs["1d"],
            m15_proof=proofs["15m"],
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
            daily_history_exhausted=proofs["1d"][0],
            m15_history_exhausted=proofs["15m"][0],
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
        source_mode: NodeClusterSourceMode = NodeClusterSourceMode.LIVE_DIRECT,
    ) -> NodeClusterInput:
        """Legacy mode：MDAS 直接返回 completed qfq（与历史行为完全一致）。

        C1 不修改任何 Legacy 语义；仅新增 ``adjustment_context_hash=None`` 字段。
        [PANJI-INTRADAY-DIRECT-SOURCE] daily 与 15m 的 source policy 由 ``source_mode``
        统一决定（live_direct → (hybrid, provider_direct)；historical_db → (db_only, db_only)）。
        """
        daily_policy, m15_policy = _resolve_node_source_policies(source_mode)

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
            source_policy=daily_policy,
        )

        m15_agg = await mdas.get_bars(
            session,
            instrument_id,
            timeframe="15m",
            adj="qfq",
            # [USER-FIX-3 / C] completed 语义下读取「当日已完成的实时尾部」：
            # 否则盘中（无外部 15m 落库任务）只能拿到上一交易日收盘为止的 stale 15m。
            # MDAS 已按 completed_only 语义在合并前剔除 forming bar，其返回结果本身
            # 全部为已完成 bar；下方 _filter_unfinished_15m_bars 仅为 defensive 复核。
            include_realtime=True,
            completed_only=True,
            fresh_intraday_tail=True,
            adjustment_as_of=adjustment_as_of,
            end_date=end_date,
            limit=_NODE_15M_REQUIRED,
            source_policy=m15_policy,
        )

        daily_bars = daily_agg.bars
        bars_15m = cls._filter_unfinished_15m_bars(m15_agg.bars, now_shanghai())
        as_of = end_date if end_date is not None else (adjustment_as_of or date.today())
        proofs = await cls._compute_exhaustion_proofs(session, instrument_id, as_of)
        availability, degraded_reason = cls._compute_availability(
            daily_count=len(daily_bars),
            m15_count=len(bars_15m),
            daily_proof=proofs["1d"],
            m15_proof=proofs["15m"],
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
            daily_history_exhausted=proofs["1d"][0],
            m15_history_exhausted=proofs["15m"][0],
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
    def _classify(count: int, required: int, proven: bool) -> str:
        """单维度分类：ok / degraded / violation。

        - count >= required: ok（达标）
        - count < required 且 proven（已证明理论最大可能历史仍不足）: degraded（允许降级）
        - count < required 且 not proven（无法证明历史耗尽）: violation（系统缺口）
        """
        if count >= required:
            return "ok"
        if proven:
            return "degraded"
        return "violation"

    @staticmethod
    def _compute_availability(
        daily_count: int,
        m15_count: int,
        daily_proof: tuple[bool, str],
        m15_proof: tuple[bool, str],
    ) -> tuple[str, str | None]:
        """availability 三态状态机（修复版：基于权威上市边界 + 交易日历安全上界证明）。

        核心原则：“查询结果少” ≠ “股票历史少”。

        - 只有基于权威 listing_date + 交易日历证明“理论最大可能历史都不足 required”，
          才允许判定 genuine history exhausted（degraded）。
        - 否则（DB 缺口 / 同步失败 / 覆盖不足 / 无法证明历史边界）→ INPUT_CONTRACT_VIOLATION，
          绝不允许伪装成 degraded 继续算筹码。

        Args:
            daily_count / m15_count: 实际取得的根数
            daily_proof / m15_proof: (genuine_exhausted_proven, reason) 元组，
                由 ``_compute_exhaustion_proofs`` 基于 listing_date + 交易日历给出。

        Returns:
            (availability, degraded_reason) 元组
        """
        daily_proven, _daily_reason = daily_proof
        m15_proven, _m15_reason = m15_proof

        # 1. daily 绝对下限：低于最低可计算门槛 → unavailable（与 250 生产合同分离）
        if daily_count < _NODE_DAILY_MIN:
            return "unavailable", "INSUFFICIENT_DAILY_BARS"

        d_status = NodeClusterInputProvider._classify(
            daily_count, _NODE_DAILY_REQUIRED, daily_proven
        )
        m_status = NodeClusterInputProvider._classify(
            m15_count, _NODE_15M_REQUIRED, m15_proven
        )

        # 2. 任一维度“不足且无法证明历史耗尽” → 系统缺口，禁止生成看似正常的 Profile
        if d_status == "violation" or m_status == "violation":
            return "unavailable", "INPUT_CONTRACT_VIOLATION"

        # 3. 任一维度“不足但已证明历史天然不足” → 允许降级计算
        if d_status == "degraded" or m_status == "degraded":
            reason = (
                "INSUFFICIENT_DAILY_HISTORY"
                if d_status == "degraded"
                else "INSUFFICIENT_15M_HISTORY"
            )
            return "degraded", reason

        # 4. 两者都达标 → 正常
        return "available", None

    @classmethod
    async def _compute_exhaustion_proofs(
        cls,
        session: AsyncSession,
        instrument_id: uuid.UUID,
        as_of: date,
    ) -> dict[str, tuple[bool, str]]:
        """基于权威 listing_date + 交易日历计算安全上界 exhaustion proof。

        单向安全证明：
            listing_date 已知
            且 (listing_date, as_of] 内最大可能交易槽位 < required
                => 可证明 genuine history exhausted（degraded 合法）
            否则
                => 不能声称 exhausted（DB 缺口/同步失败/无法证明边界 → violation）

        ``listing_date=NULL``：绝不 fallback 到 1990 后声称 exhausted，
            返回 (False, "MISSING_HISTORY_BOUNDARY_PROOF")。

        复用权威 listing-date owner（``bar_repository._get_listing_date``，读
        instruments.listing_date）与交易日历 owner（``board_facts_service.
        _count_trading_days_between``，基于 TradingCalendar 模型）。
        """
        from app.repositories.bar_repository import _get_listing_date
        from app.services.board_facts_service import _count_trading_days_between

        listing_date = await _get_listing_date(session, instrument_id)
        if listing_date is None:
            return {
                "1d": (False, "MISSING_HISTORY_BOUNDARY_PROOF"),
                "15m": (False, "MISSING_HISTORY_BOUNDARY_PROOF"),
            }

        # 防御性归一：listing_date 可能因 DB 列为字符串或 mock 返回 str，统一转为 date。
        # 无法解析则视为历史边界不可证（fail closed），绝不伪造 exhaustion。
        if isinstance(listing_date, str):
            try:
                listing_date = date.fromisoformat(listing_date)
            except ValueError:
                return {
                    "1d": (False, "MISSING_HISTORY_BOUNDARY_PROOF"),
                    "15m": (False, "MISSING_HISTORY_BOUNDARY_PROOF"),
                }

        # (listing_date - 1, as_of] 含上市首日；这是“最大可能”上界（保守，只会低估不会高估）
        trading_days = await _count_trading_days_between(
            session, listing_date - timedelta(days=1), as_of
        )

        proofs: dict[str, tuple[bool, str]] = {}
        for tf, required in (
            ("1d", _NODE_DAILY_REQUIRED),
            ("15m", _NODE_15M_REQUIRED),
        ):
            max_bars = (
                trading_days
                if tf == "1d"
                else trading_days * _MAX_15M_SLOTS_PER_TRADING_DAY
            )
            if max_bars < required:
                proofs[tf] = (True, "GENUINE_HISTORY_EXHAUSTED")
            else:
                proofs[tf] = (False, "HISTORY_UNDERFILLED_DB_GAP")
        return proofs

    @staticmethod
    def _filter_unfinished_15m_bars(
        bars_15m: pd.DataFrame,
        now: datetime | None = None,
    ) -> pd.DataFrame:
        """丢弃 still-forming 的 15m bar（**defensive guard**）。

        [USER-FIX-3 / C corrective] canonical completion 语义的 owner 已前移到
        ``market_data_aggregation_service._filter_unfinished_15m_bars``：MDAS 在实时
        尾部合并前即剔除 forming bar，因此其返回结果本身全部为已完成 bar。

        本方法保留一次幂等复核（委托到同一 owner，不另写第二套 cutoff 规则），
        用于在上游合同被破坏时仍然守住「进入 Node 的 15m 全部为已完成 bar」。
        正常 production 路径下：

            MDAS 返回数据 == 本方法过滤后数据
        """
        return _filter_completed_15m_bars(bars_15m, now)

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
    import inspect

    provider = NodeClusterInputProvider

    def _p(proven: bool, reason: str = "x") -> tuple[bool, str]:
        return (proven, reason)

    # 1. 正常合同：250 daily + 4000 15m → available
    avail, reason = provider._compute_availability(250, 4000, _p(False), _p(False))
    assert avail == "available", f"应为 available, got {avail}"
    assert reason is None
    print(f"正常: avail={avail} reason={reason} ✓")

    # 2. daily 绝对下限 < 10 → unavailable / INSUFFICIENT_DAILY_BARS
    avail, reason = provider._compute_availability(9, 4000, _p(False), _p(False))
    assert avail == "unavailable", f"应为 unavailable, got {avail}"
    assert reason == "INSUFFICIENT_DAILY_BARS"
    print(f"daily不足: avail={avail} reason={reason} ✓")

    # 3. 15m 完全缺失 + 已证 genuine exhausted（真实新股）→ degraded / INSUFFICIENT_15M_HISTORY
    avail, reason = provider._compute_availability(250, 0, _p(False), _p(True, "GENUINE_HISTORY_EXHAUSTED"))
    assert avail == "degraded", f"应为 degraded, got {avail}"
    assert reason == "INSUFFICIENT_15M_HISTORY"
    print(f"新股15m缺失: avail={avail} reason={reason} ✓")

    # 4. 15m 历史不足（144 根）+ 已证 genuine exhausted → degraded / INSUFFICIENT_15M_HISTORY
    avail, reason = provider._compute_availability(250, 144, _p(False), _p(True, "GENUINE_HISTORY_EXHAUSTED"))
    assert avail == "degraded", f"应为 degraded, got {avail}"
    assert reason == "INSUFFICIENT_15M_HISTORY"
    print(f"15m历史不足: avail={avail} reason={reason} ✓")

    # 5. 15m 不足 + 无法证明历史耗尽（老股票 DB 缺口）→ unavailable / INPUT_CONTRACT_VIOLATION
    avail, reason = provider._compute_availability(250, 1872, _p(False), _p(False, "HISTORY_UNDERFILLED_DB_GAP"))
    assert avail == "unavailable", f"应为 unavailable, got {avail}"
    assert reason == "INPUT_CONTRACT_VIOLATION"
    print(f"输入合同违反: avail={avail} reason={reason} ✓")

    # 6. listing_date=NULL + 15m 不足 → 不得 degraded，fail closed → INPUT_CONTRACT_VIOLATION
    avail, reason = provider._compute_availability(
        250, 1872, _p(False, "MISSING_HISTORY_BOUNDARY_PROOF"), _p(False, "MISSING_HISTORY_BOUNDARY_PROOF"),
    )
    assert avail == "unavailable", f"应为 unavailable, got {avail}"
    assert reason == "INPUT_CONTRACT_VIOLATION"
    print(f"边界证明缺失: avail={avail} reason={reason} ✓")

    # 7. daily 100/250 + 老股票（无法证明耗尽）→ unavailable / INPUT_CONTRACT_VIOLATION
    avail, reason = provider._compute_availability(100, 4000, _p(False), _p(False))
    assert avail == "unavailable", f"应为 unavailable, got {avail}"
    assert reason == "INPUT_CONTRACT_VIOLATION"
    print(f"daily不足未证明: avail={avail} reason={reason} ✓")

    # 8. daily 100/250 + 真正新股（已证耗尽）→ degraded / INSUFFICIENT_DAILY_HISTORY
    avail, reason = provider._compute_availability(100, 4000, _p(True, "GENUINE_HISTORY_EXHAUSTED"), _p(False))
    assert avail == "degraded", f"应为 degraded, got {avail}"
    assert reason == "INSUFFICIENT_DAILY_HISTORY"
    print(f"新股daily不足: avail={avail} reason={reason} ✓")

    # 9. [PANJI-INTRADAY-DIRECT-SOURCE] source mode → (daily, 15m) source policy 映射
    assert _resolve_node_source_policies(NodeClusterSourceMode.LIVE_DIRECT) == (
        MarketDataSourcePolicy.HYBRID,
        MarketDataSourcePolicy.PROVIDER_DIRECT,
    ), "LIVE_DIRECT 必须是 (daily=hybrid, 15m=provider_direct)"
    assert _resolve_node_source_policies(NodeClusterSourceMode.HISTORICAL_DB) == (
        MarketDataSourcePolicy.DB_ONLY,
        MarketDataSourcePolicy.DB_ONLY,
    ), "HISTORICAL_DB 的 daily 与 15m 都必须是 db_only（PIT 绝对禁止访问网络）"
    default_mode = inspect.signature(
        NodeClusterInputProvider.get_inputs
    ).parameters["source_mode"].default
    assert default_mode is NodeClusterSourceMode.LIVE_DIRECT, (
        f"source_mode 默认必须是 LIVE_DIRECT, got {default_mode!r}"
    )
    print(
        "node source mode ✓ (LIVE_DIRECT→(hybrid, provider_direct), "
        "HISTORICAL_DB→(db_only, db_only), default=LIVE_DIRECT)"
    )

    print("\nOK — NodeClusterInputProvider 状态机验证通过")
