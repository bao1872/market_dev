"""FeatureSnapshotService - 盘后特征快照计算与持久化服务。

核心功能：
1. compute_feature_snapshot_for_date: 为指定 instrument + trade_date 计算 point-in-time 特征快照。
2. upsert_snapshot: 按唯一键幂等写入。
3. compute_for_trade_date: 批量计算多个 instrument 的快照。
4. build_summary_payload: 从完整 payload 抽取前端列表用摘要。
5. create_snapshot_run / finish_snapshot_run: run 级别生命周期管理（publish gate）。

设计原则：
- 复用 structural_factor_service._compute_all_factors_for_bars 和
  temporal_feature_service._compute_daily_context / _compute_m15_response / _compute_derived_relation，
  不复制 DSA/BB/swing/temporal 数学公式。
- point-in-time：1d bars 只用 <= trade_date，15m bars 只用 <= trade_date 当日。
- 单股失败写 degraded_reasons，不抛全局失败。
- 不建 EAV 表，不给 full payload 加 GIN 索引。
- run 级 publish gate：watchlist 只读取 succeeded run 对应的 snapshot 行，
  failed/running run 对应的 snapshot 即使存在也不得被读取。

用法：
    from app.services.feature_snapshot_service import compute_feature_snapshot_for_date
    snapshot = await compute_feature_snapshot_for_date(
        session, instrument_id, trade_date=date(2026, 1, 10)
    )

模块自测：
    python -m app.services.feature_snapshot_service
"""

from __future__ import annotations

import logging
import platform
import resource
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.repositories.bar_repository import _get_symbol
from app.services.atomic_fact_contract_service import build_persisted_afc_payload

# [CHANGE-20260718-004 Node Cluster engine] 盘后链一次调用 engine 计算 Node Cluster Profile，
# 注入 _compute_all_factors_for_bars(primary)，修复三链不一致缺陷（原 _compute_cost_position_factors
# 单独调用 compute_unified_volume_profile(bars) 单周期 VP，与详情/监控链口径不一致）。
# 15m secondary 保持单周期 VP 语义（timeframe_volume_profile，非 Node Cluster）。
#
# [CP-13 Canonical 四链迁移] 四链禁止直接 import 算法 kernel 函数；
# 所有注册算法（node_cluster/bollinger/macd/structural_features/primary_secondary_relation）
# 必须经 CanonicalComputationService.compute() 调用。
# 仅保留 DTO builders（非算法 kernel，是视图层工具）、类型引用和 temporal 子函数
# （temporal 子函数无独立 registered adapter，compute_temporal_features_adapter 会
#  重复获取 bars 和重算因子，故保留直接调用）。
from app.services.canonical_adapters import (
    NodeClusterProfileResult,
    _compute_daily_context,
    _compute_derived_relation,
    _compute_m15_response,
    build_price_state,
    profile_to_dict,
)
from app.services.canonical_computation_service import CanonicalComputationService
from app.services.core_run_context import (
    ComputeOnceDiagnostics,
    CoreRunContext,
    enforce_compute_once_gate,
)
from app.services.first_pyramid_flatten import (
    assemble_first_pyramid_read_model,
    flatten_first_pyramid,
)
from app.services.market_data_aggregation_service import (
    BarAggregationResult,
    MarketDataAggregationService,
)
from app.services.node_cluster_input_provider import NodeClusterInputProvider


class PublishedSnapshotRunExistsError(Exception):
    """[P0-4] 已存在 canonical succeeded+published+full run，禁止重跑覆盖。

    由 create_snapshot_run 在 scope='full' 时抛出（无条件，无 bypass）。
    已归属 succeeded+published run 的 snapshot 无条件不可覆盖。
    未来纠错发布另做 supersede 机制，当前不提供绕过。
    """

    def __init__(self, existing_run: StockFeatureSnapshotRun) -> None:
        self.existing_run = existing_run
        super().__init__(
            f"已存在 published full snapshot run: "
            f"trade_date={existing_run.trade_date} run_id={existing_run.id} "
            f"published_at={existing_run.published_at}。"
            f"已发布快照无条件不可覆盖；如需纠错请使用 supersede 机制（未实现）。"
        )

logger = logging.getLogger(__name__)

# 常量
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
# [P0-5 修复 2026-07-29] v5→v6：盘后 review core daily-only 路径接入、core/chip 解耦、
# first_pyramid 字段重命名（current_vs_prev_volume_mean_ratio）、SMC OB 三事件、
# SQZ_RELEASE 方向修复、regime_strength 修正、history 逐 bar readiness
# 旧 v5 快照与新 v6 语义不可混用；部署后等待首次 v6 盘后运行成功，成功前 watchlist_ready=False
_SCHEMA_VERSION = 6
_PRIMARY_LOOKBACK = 500  # 日线回看天数（与 structural_factor_service 对齐）
_SECONDARY_LOOKBACK = 500  # 15m 回看天数
_BB_WIN = 20
_BB_K = 2.0

# MACD 参数（标准 12/26/9）
_MACD_FAST = 12
_MACD_SLOW = 26
_MACD_SIGNAL = 9


# =============================================================================
# C6: 紧凑状态计算（MACD 关系，不保存完整指标序列）
# =============================================================================


async def _compute_macd_state(
    df_1d: pd.DataFrame | None,
    instrument_id: uuid.UUID,
    trade_date: date,
    source_bar_hash: str | None = None,
    adj_factor_hash: str | None = None,
) -> dict[str, Any]:
    """C6: 计算MACD紧凑状态（只保存最终值+code，不保存完整序列）。

    [CP-13] 经 CanonicalComputationService.compute(algorithm_id="macd", ...) 调用，
    禁止直接 import compute_macd kernel，禁止在本模块内复制 EMA/MACD 公式。

    code 四象限（基于 DIF 和 DEA）：
    - bullish_above: DIF > 0 且 DIF > DEA（最强多头）
    - bullish_below: DIF > 0 且 DIF <= DEA（多头减弱）
    - bearish_below: DIF < 0 且 DIF < DEA（最强空头）
    - bearish_above: DIF < 0 且 DIF >= DEA（空头减弱）
    - None: 数据不足或 DIF == 0
    """
    empty = {"code": None, "macd_val": None, "signal_val": None, "histogram": None}
    if df_1d is None or df_1d.empty:
        return empty
    min_len = _MACD_SLOW + _MACD_SIGNAL  # 35
    if len(df_1d) < min_len:
        return empty

    # [CP-13] 经 canonical 调用 macd adapter（包装 indicator_service.compute_macd）
    canonical_result = await CanonicalComputationService.compute(
        algorithm_id="macd",
        instrument_id=instrument_id,
        as_of=trade_date.isoformat(),
        source_bar_hash=source_bar_hash,
        adj_factor_hash=adj_factor_hash,
        bars=df_1d,
        fast=_MACD_FAST,
        slow=_MACD_SLOW,
        signal=_MACD_SIGNAL,
    )
    macd_result = canonical_result.payload
    # 取最后一个非 None 值
    dif_list = macd_result["macd_dif"]
    dea_list = macd_result["macd_dea"]
    hist_list = macd_result["macd_hist"]

    last_dif = dif_list[-1] if dif_list and dif_list[-1] is not None else None
    last_dea = dea_list[-1] if dea_list and dea_list[-1] is not None else None
    last_hist = hist_list[-1] if hist_list and hist_list[-1] is not None else None

    if last_dif is None or last_dea is None:
        return empty

    if last_dif > 0 and last_dif > last_dea:
        code = "bullish_above"
    elif last_dif > 0 and last_dif <= last_dea:
        code = "bullish_below"
    elif last_dif < 0 and last_dif < last_dea:
        code = "bearish_below"
    elif last_dif < 0 and last_dif >= last_dea:
        code = "bearish_above"
    else:
        code = None

    return {
        "code": code,
        "macd_val": round(last_dif, 6),
        "signal_val": round(last_dea, 6),
        "histogram": round(last_hist, 6) if last_hist is not None else None,
    }


# =============================================================================
# 纯函数：point-in-time 截断
# =============================================================================


def _truncate_bars_to_trade_date(
    bars: pd.DataFrame | None,
    trade_date: date,
    timeframe: str,
) -> pd.DataFrame | None:
    """将 bars 截断到 <= trade_date，保证 point-in-time。

    对 1d 和 15m 均按 index.date <= trade_date 截断。
    截断后为空返回 None。

    Args:
        bars: K 线 DataFrame，index 为 DatetimeIndex
        trade_date: 截止交易日
        timeframe: 时间周期（1d / 15m）

    Returns:
        截断后的 DataFrame 或 None
    """
    if bars is None or bars.empty:
        return None
    mask = bars.index.date <= trade_date
    truncated = bars[mask]
    if truncated.empty:
        return None
    return truncated


# =============================================================================
# 纯函数：summary_payload 构建
# =============================================================================


def _safe_get(d: dict, *keys, default: Any = None) -> Any:
    """安全嵌套取值，任一层缺失返回 default。"""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def build_summary_payload(
    structural_payload: dict[str, Any],
    temporal_payload: dict[str, Any],
    trade_date: date,
    source_bar_time: str | None = None,
    extra: dict[str, Any] | None = None,
    first_pyramid: dict[str, Any] | None = None,
    *,
    source_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """从 structural/temporal payload 抽取前端列表用摘要。

    从 structural_payload["primary"]["1d"] 提取日线因子，
    从 structural_payload["secondary"]["15m"] 提取 15m 因子，
    从 temporal_payload["derived_relation"] 提取派生关系。

    Args:
        structural_payload: compute_structural_factors 的完整输出
        temporal_payload: compute_temporal_features 的完整输出
        trade_date: 业务交易日
        source_bar_time: 数据源截止时间（ISO 字符串）
        extra: 额外字段（current_price, change_pct, bb_upper/mid/lower 等）
        first_pyramid: 第一金字塔统一快照 dict（Gate1 持久化；None 表示未计算或数据不足）
        source_run_id: 快照归属 run ID（写入 fp_run_id 元数据字段）

    Returns:
        前端列表用摘要 dict，包含 _source='feature_snapshot'
    """
    extra = extra or {}
    primary_1d = _safe_get(structural_payload, "primary", "1d", default={})
    secondary_15m = _safe_get(structural_payload, "secondary", "15m", default={})
    cost_pos = primary_1d.get("cost_position") or {}
    swing_primary = primary_1d.get("swing_position") or {}
    swing_secondary = secondary_15m.get("swing_position") or {}
    derived = temporal_payload.get("derived_relation") or {}

    return {
        # 额外字段（来自 bars 最后一根 bar）
        "current_price": extra.get("current_price"),
        "change_pct": extra.get("change_pct"),
        "bb_upper": extra.get("bb_upper"),
        "bb_mid": extra.get("bb_mid"),
        "bb_lower": extra.get("bb_lower"),
        # 成本/节点
        "poc_price": cost_pos.get("poc_price"),
        "nearest_node_above": cost_pos.get("nearest_node_above_price"),
        "nearest_node_below": cost_pos.get("nearest_node_below_price"),
        "distance_to_node_above_atr": cost_pos.get("distance_to_node_above_atr"),
        "distance_to_node_below_atr": cost_pos.get("distance_to_node_below_atr"),
        "node_interval_position_0_1": cost_pos.get("node_interval_position_0_1"),
        "cost_position_zone": cost_pos.get("cost_position_zone"),
        "value_area_zone": cost_pos.get("value_area_zone"),
        # 日线 developing swing
        "daily_developing_swing_dir": swing_primary.get("developing_swing_dir"),
        "daily_developing_swing_high": swing_primary.get("developing_swing_high"),
        "daily_developing_swing_low": swing_primary.get("developing_swing_low"),
        # 15m developing swing
        "m15_developing_swing_dir": swing_secondary.get("developing_swing_dir"),
        "m15_developing_swing_high": swing_secondary.get("developing_swing_high"),
        "m15_developing_swing_low": swing_secondary.get("developing_swing_low"),
        # 派生关系
        "m15_position_relative_to_daily": derived.get("m15_position_relative_to_daily"),
        # 元信息
        "as_of": trade_date.isoformat(),
        "source_bar_time": source_bar_time,
        "_source": "feature_snapshot",
        # Atomic Fact Contract V1（仅新快照写入；旧已发布快照受 upsert WHERE 保护不覆盖）
        "atomic_fact_contract_v1": build_persisted_afc_payload(structural_payload, temporal_payload),
        # Gate1: 第一金字塔统一快照持久化（None 表示数据不足未计算；不静默省略）
        "first_pyramid": first_pyramid,
        # [CHANGE-20260729-005 二.2] 扁平化 99 字段对象，供服务端 filter/sort 统一读取
        # 包含全部 99 个 fp_ 键；chip 字段在 core 写入时可能为 None（chip 异步写入独立表）
        # [C1] 统一读模型组装：写入时即覆盖 fp_trade_date/fp_run_id 真实列，
        #      避免持久化 flat 与 API 响应 read model 元数据不一致
        "first_pyramid_flat": assemble_first_pyramid_read_model(
            flatten_first_pyramid(first_pyramid),
            snapshot_columns={
                "trade_date": trade_date,
                # [修复] 写入时即覆盖 fp_calculated_at/fp_run_id 真实列，
                #       fp_calculated_at 用快照计算时间（created_at 由 ORM flush 设置，
                #       这里用当前时间作为稳定回填），避免 fp_calculated_at 持久化为 null
                "created_at": datetime.now(UTC).isoformat(),
                "source_run_id": str(source_run_id) if source_run_id is not None else None,
            },
        ),
    }


# =============================================================================
# 核心：计算单股单日 snapshot
# =============================================================================


async def compute_feature_snapshot_for_date(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    trade_date: date,
    primary_timeframe: str = "1d",
    secondary_timeframe: str = "15m",
    adj: str = "qfq",
    *,
    primary_bars: pd.DataFrame | None = None,
    secondary_bars: pd.DataFrame | None = None,
    source_run_id: uuid.UUID | None = None,
    instrument_symbol: str | None = None,
    _diag_sink: dict[str, Any] | None = None,
    compute_diagnostics: ComputeOnceDiagnostics | None = None,
) -> StockFeatureSnapshot:
    """为指定 instrument + trade_date 计算 point-in-time 特征快照。

    内部复用现有算法，不复制公式：
    - structural_factor_service._compute_all_factors_for_bars
    - temporal_feature_service._compute_daily_context / _compute_m15_response / _compute_derived_relation
    - bollinger_features_plotly.bollinger（BB 绝对值）

    point-in-time：
    - 1d bars 只用 <= trade_date
    - 15m bars 只用 <= trade_date 当日
    - 禁止使用 trade_date 之后数据

    [P0-symbol合同 2026-07-30] 第一金字塔公共 symbol 必须是规范化6位股票代码。
    调用方应通过 instrument_symbol 显式传入；未传时本函数查询一次 instruments.symbol。
    禁止继续用 str(instrument_id) 作为 first_pyramid.symbol。

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        trade_date: 业务交易日
        primary_timeframe: 主周期（默认 1d）
        secondary_timeframe: 副周期（默认 15m）
        adj: 复权方式（默认 qfq）
        primary_bars: 预加载的日线 bars（可选，不传则从 DB 获取）
        secondary_bars: 预加载的 15m bars（可选，不传则从 DB 获取）
        instrument_symbol: 规范化股票代码（如 '300369'）；未传则按 instrument_id 查询一次
        _diag_sink: 诊断信息收集 dict

    Returns:
        StockFeatureSnapshot ORM 对象（未写入 DB）
    """
    degraded_reasons: list[str] = []
    warmup_notes: list[str] = []

    # 获取 K 线（如果未预加载）
    primary_adj_factor_hash: str | None = None
    primary_source_bar_hash: str | None = None
    if primary_bars is None:
        primary_bars, primary_diag = await _fetch_bars_from_db(
            session, instrument_id, primary_timeframe, adj, trade_date,
        )
        # [CHANGE-20260717-002 SSOT] 主周期诊断为权威，写入 _diag_sink 供 run 级收集
        if _diag_sink is not None and primary_diag:
            _diag_sink.update(primary_diag)
        # [CHANGE-20260718-004] 提取 adj_factor_hash 供 engine 诊断字段（point-in-time 复权因子 hash）
        primary_adj_factor_hash = primary_diag.get("adj_factor_hash") if primary_diag else None
        # [CP-13] 提取 source_bar_hash 供 canonical result_hash 计算
        primary_source_bar_hash = primary_diag.get("source_bar_hash") if primary_diag else None
    if secondary_bars is None:
        secondary_bars, _secondary_diag = await _fetch_bars_from_db(
            session, instrument_id, secondary_timeframe, adj, trade_date,
        )

    # point-in-time 截断
    df_1d = _truncate_bars_to_trade_date(primary_bars, trade_date, primary_timeframe)
    df_15m = _truncate_bars_to_trade_date(secondary_bars, trade_date, secondary_timeframe)

    # 数据不足检查（与 _fetch_bars 的 60 根 warmup 对齐）
    if df_1d is None:
        degraded_reasons.append(f"{primary_timeframe}: no bars <= {trade_date}")
    elif len(df_1d) < 60:
        degraded_reasons.append(
            f"{primary_timeframe}: insufficient bars ({len(df_1d)} < 60)"
        )
    if df_15m is None:
        degraded_reasons.append(f"{secondary_timeframe}: no bars <= {trade_date}")
    elif len(df_15m) < 60:
        degraded_reasons.append(
            f"{secondary_timeframe}: insufficient bars ({len(df_15m)} < 60)"
        )

    # [CP-V3-A] Node Cluster 输入由 NodeClusterInputProvider 唯一提供（四链统一入口）。
    # 盘后链 point-in-time：adjustment_as_of=trade_date + end_date=trade_date，
    # 保证不读取 trade_date 之后数据，且 qfq 因子不含未来除权事件。
    # availability 三态状态机由 Provider 预计算：
    # - available: 250+4000，正常计算
    # - degraded: history_exhausted=true 且真实历史不足，允许降级计算
    # - unavailable: INPUT_CONTRACT_VIOLATION / INSUFFICIENT_DAILY_BARS / MISSING_15M_BARS
    #   → 禁止生成看似正常的 Profile
    node_input = await NodeClusterInputProvider.get_inputs(
        session,
        instrument_id,
        adjustment_as_of=trade_date,
        end_date=trade_date,
    )
    node_cluster_profile: NodeClusterProfileResult | None = None
    node_availability: str = node_input.availability
    node_degraded_reason: str | None = node_input.degraded_reason
    if node_input.availability == "unavailable":
        # INPUT_CONTRACT_VIOLATION / INSUFFICIENT_DAILY_BARS / MISSING_15M_BARS
        # 禁止生成 Profile（不调用 Canonical compute）
        if node_degraded_reason and node_degraded_reason.startswith("INPUT_CONTRACT"):
            degraded_reasons.append(
                f"node_cluster: {node_degraded_reason} "
                f"(daily={node_input.daily_count}/{node_input.daily_requested}, "
                f"15m={node_input.m15_count}/{node_input.m15_requested})"
            )
    else:
        try:
            # [CP-13] 经 canonical 调用 node_cluster adapter
            # 使用 Provider 返回的 250+4000 bars + hash（四链一致）
            node_cluster_canonical = await CanonicalComputationService.compute(
                algorithm_id="node_cluster",
                instrument_id=instrument_id,
                as_of=trade_date.isoformat(),
                source_bar_hash=node_input.daily_source_hash,
                adj_factor_hash=node_input.daily_adj_factor_hash,
                daily_bars=node_input.daily_bars,
                bars_15m=node_input.bars_15m,
                adjustment_as_of=trade_date.isoformat(),
            )
            node_cluster_profile = node_cluster_canonical.payload
        except Exception as exc:
            logger.warning("Node Cluster engine 计算失败: %s", exc)
            node_cluster_profile = None
            node_availability = "unavailable"
            node_degraded_reason = f"COMPUTE_FAILED: {exc}"
            degraded_reasons.append(f"node_cluster: engine failed: {exc}")
        else:
            if node_cluster_profile is None or not node_cluster_profile.profile_rows:
                node_availability = "unavailable"
                node_degraded_reason = "PROFILE_EMPTY"
            # else: 使用 Provider 预计算的 availability（available 或 degraded/INSUFFICIENT_15M_HISTORY）

    # 计算 structural factors
    # [CP-13] 经 canonical 调用 structural_features adapter
    # adapter 内部创建 degraded_reasons/warmup_notes 列表并附加到 result，
    # 调用方需 pop 出来扩展到自身的 degraded_reasons/warmup_notes（保持原 side-effect 语义）。
    primary_canonical = await CanonicalComputationService.compute(
        algorithm_id="structural_features",
        instrument_id=instrument_id,
        as_of=trade_date.isoformat(),
        source_bar_hash=primary_source_bar_hash,
        adj_factor_hash=primary_adj_factor_hash,
        bars=df_1d if df_1d is not None else pd.DataFrame(),
        timeframe=primary_timeframe,
        precomputed_node_cluster=node_cluster_profile,
        # [Corrective-2 2026-08-05] run-scoped compute-once 计数：仅 primary(1d)
        # 帧传入 diagnostics，secondary(15m) 不传（不计入 canonical 保证）。
        diagnostics=compute_diagnostics,
    )
    primary_factors = primary_canonical.payload
    degraded_reasons.extend(primary_factors.pop("degraded_reasons", []))
    warmup_notes.extend(primary_factors.pop("warmup_notes", []))

    secondary_canonical = await CanonicalComputationService.compute(
        algorithm_id="structural_features",
        instrument_id=instrument_id,
        as_of=trade_date.isoformat(),
        bars=df_15m if df_15m is not None else pd.DataFrame(),
        timeframe=secondary_timeframe,
        precomputed_node_cluster=None,  # 15m secondary 单周期 VP，非 Node Cluster
    )
    secondary_factors = secondary_canonical.payload
    degraded_reasons.extend(secondary_factors.pop("degraded_reasons", []))
    warmup_notes.extend(secondary_factors.pop("warmup_notes", []))

    # C6: 计算真实 MACD 紧凑状态
    # 只保存紧凑状态（最终值+code），不保存完整指标序列
    primary_factors["macd_state"] = await _compute_macd_state(
        df_1d, instrument_id, trade_date, primary_source_bar_hash, primary_adj_factor_hash,
    )

    # 计算 temporal features（复用内部函数）
    # [CP-13] temporal 子函数无独立 registered adapter（compute_temporal_features_adapter
    # 会重复获取 bars 和重算因子），故保留直接调用。
    daily_context = _compute_daily_context(
        primary_factors, df_1d, degraded_reasons, warmup_notes
    )
    m15_response = _compute_m15_response(
        secondary_factors, df_15m, degraded_reasons, warmup_notes
    )
    derived_relation = _compute_derived_relation(
        daily_context, m15_response, degraded_reasons
    )

    # [Blocker4] - 复用 structural_factor_service._compute_relation 计算 primary vs secondary
    # 客观关系（trend_alignment / secondary_vs_primary_position_delta 等），
    # 禁止在 feature_snapshot_service 内复制关系计算公式。
    # [CP-13] 经 canonical 调用 primary_secondary_relation adapter
    relation_canonical = await CanonicalComputationService.compute(
        algorithm_id="primary_secondary_relation",
        instrument_id=instrument_id,
        as_of=trade_date.isoformat(),
        primary_factors=primary_factors,
        secondary_factors=secondary_factors,
    )
    relation = relation_canonical.payload

    # 构造 structural_payload（与 compute_structural_factors 输出格式对齐）
    # [CHANGE-20260718-004] primary.1d 新增 canonical node_cluster 字段（engine 不可变结果），
    # cost_position 兼容指向 engine 派生字段；secondary.15m.cost_position 重命名为
    # timeframe_volume_profile（单周期 15m VP，显式非 Node Cluster）。
    # [CHANGE-20260721-001] node_cluster 始终写入 availability/degraded_reason（即使 profile 为 None），
    # 供 StockContext 区分 NODE_PROFILE_EMPTY/NODE_15M_MISSING/NODE_COMPUTE_FAILED 三态。
    # [PROMPT.md §5.2.2 V2] node_cluster 必须包含 price_state（与 node_regions 配对）：
    # 前端通过 price_state.*_ref 在 node_regions 中查找完整节点信息。
    # snapshot 是 point-in-time，current_price 取 df_1d 最后一根收盘价（与 extra.current_price 一致）。
    current_price_for_state: float | None = None
    if df_1d is not None and not df_1d.empty:
        try:
            current_price_for_state = float(df_1d["close"].iloc[-1])
        except (IndexError, ValueError, KeyError):
            current_price_for_state = None
    primary_payload: dict[str, Any] = {**primary_factors}
    if node_cluster_profile is not None:
        node_cluster_dict = profile_to_dict(node_cluster_profile)
        node_cluster_dict["availability"] = node_availability
        node_cluster_dict["degraded_reason"] = node_degraded_reason
        # [PROMPT.md §5.2.2 V2] price_state：与 node_regions 配对的当前价状态
        # profile 为 None 时由 else 分支处理；profile 存在时调用 build_price_state
        # 当 current_price 不可得时传入 NaN 触发 build_price_state 的兜底逻辑
        try:
            price_for_state = current_price_for_state if current_price_for_state is not None else float("nan")
            node_cluster_dict["price_state"] = build_price_state(
                node_cluster_profile, price_for_state
            )
        except Exception as exc:
            logger.warning("build_price_state 失败: %s", exc)
            node_cluster_dict["price_state"] = {}
        primary_payload["node_cluster"] = node_cluster_dict
    else:
        # profile 计算失败或日线不足：仍写入最小诊断字段，StockContext 显式区分 unavailable 原因
        # [CP-V3-A] count/hash 来自 NodeClusterInputProvider（四链一致诊断）
        primary_payload["node_cluster"] = {
            "availability": node_availability,
            "degraded_reason": node_degraded_reason,
            "profile_hash": None,
            "poc_price": None,
            "vah_price": None,
            "val_price": None,
            "daily_source_hash": node_input.daily_source_hash,
            "bars_15m_source_hash": node_input.m15_source_hash,
            "algorithm_version": None,
            "output_schema_version": None,
            "contract_fingerprint": None,
            "daily_bars_count": node_input.daily_count,
            "bars_15m_count": node_input.m15_count,
            "profile_rows": [],
            "peak_rows": [],
            "all_peak_prices": [],
            # [PROMPT.md §5.2.2 V2] profile 为空时 price_state 写最小结构（current_price + 全 None refs）
            "price_state": {
                "current_price": current_price_for_state,
                "position_0_1": None,
                "upper_node_ref": None,
                "lower_node_ref": None,
                "poc_node_ref": None,
                "last_touched_node_ref": None,
            },
        }
    secondary_payload: dict[str, Any] = {**secondary_factors}
    # 重命名 cost_position → timeframe_volume_profile（显式非 Node Cluster）
    if "cost_position" in secondary_payload:
        secondary_payload["timeframe_volume_profile"] = secondary_payload.pop("cost_position")

    structural_payload: dict[str, Any] = {
        "primary": {primary_timeframe: primary_payload},
        "secondary": {secondary_timeframe: secondary_payload},
        "relation": relation,
        "meta": {
            "degraded_reasons": degraded_reasons,
            "warmup_notes": warmup_notes,
        },
    }

    # 构造 temporal_payload
    temporal_payload: dict[str, Any] = {
        "daily_context": daily_context,
        "m15_response": m15_response,
        "derived_relation": derived_relation,
        "meta": {
            "degraded_reasons": degraded_reasons,
            "warmup_notes": warmup_notes,
        },
    }

    # 提取额外字段（current_price, change_pct, BB 绝对值）
    extra = await _extract_extra_fields(
        df_1d, instrument_id, trade_date, primary_source_bar_hash, primary_adj_factor_hash,
    )

    # source_bar_time
    source_primary = _normalize_primary_bar_time(df_1d, trade_date)
    source_secondary = _normalize_secondary_bar_time(df_15m)
    source_bar_time_str = (
        source_secondary.isoformat() if source_secondary
        else (source_primary.isoformat() if source_primary else None)
    )

    # Gate1: 第一金字塔统一快照计算（数据不足时为 None，不阻断主流程）
    first_pyramid_dict: dict[str, Any] | None = None
    try:
        from app.services.first_pyramid_service import compute_first_pyramid_snapshot
        if df_1d is not None and not df_1d.empty and len(df_1d) >= 60:
            # [P0-symbol合同 2026-07-30] 公共 symbol 必须是规范化6位股票代码
            # 优先使用调用方传入的 instrument_symbol；未传则按 instrument_id 查询一次
            if instrument_symbol is None:
                instrument_symbol = await _get_symbol(session, instrument_id)
            if instrument_symbol is None:
                # 查询失败时回退到 str(instrument_id) 并记录诊断；不阻断主流程
                # 但生产路径不应走到这里（上游已校验 instrument 存在）
                logger.warning(
                    "compute_feature_snapshot_for_date instrument_symbol 查询失败，回退 UUID: "
                    "instrument_id=%s trade_date=%s",
                    instrument_id, trade_date,
                )
                symbol_for_pyramid = str(instrument_id)
            else:
                symbol_for_pyramid = instrument_symbol
            fp_snapshot = compute_first_pyramid_snapshot(
                bars=df_1d,
                symbol=symbol_for_pyramid,
                trade_date=trade_date.isoformat(),
            )
            first_pyramid_dict = fp_snapshot.to_dict()
    except Exception as exc:
        logger.warning(
            "第一金字塔计算失败 instrument_id=%s trade_date=%s: %s",
            instrument_id, trade_date, exc,
        )
        first_pyramid_dict = None  # 显式 None；不阻断 snapshot 主流程

    # 构造 summary_payload
    summary_payload = build_summary_payload(
        structural_payload, temporal_payload, trade_date,
        source_bar_time=source_bar_time_str, extra=extra,
        first_pyramid=first_pyramid_dict,
        source_run_id=source_run_id,
    )

    return StockFeatureSnapshot(
        instrument_id=instrument_id,
        trade_date=trade_date,
        primary_timeframe=primary_timeframe,
        secondary_timeframe=secondary_timeframe,
        adj=adj,
        schema_version=_SCHEMA_VERSION,
        source_run_id=source_run_id,
        source_primary_bar_time=source_primary,
        source_secondary_bar_time=source_secondary,
        structural_payload=structural_payload,
        temporal_payload=temporal_payload,
        summary_payload=summary_payload,
        degraded_reasons=degraded_reasons,
    )


# =============================================================================
# [CHANGE-20260729-003] 盘后 review core 计算路径（daily-core only）
# =============================================================================


def _make_run_calculated_at() -> str:
    """[QM-62 2026-08-04] 生成 run 级唯一计算时间（ISO8601，Asia/Shanghai）。

    必须在批任务入口调用一次，并把结果传给该 run 内所有股票，
    保证同一 run 的 calculatedAt 完全相同。禁止单股各自取时钟——
    否则同 run 快照时间戳散落，无法判断数据是否同批产出。
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    return _dt.now(ZoneInfo("Asia/Shanghai")).isoformat()


async def compute_review_core_for_trade_date(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    trade_date: date,
    primary_timeframe: str = "1d",
    adj: str = "qfq",
    *,
    primary_bars: pd.DataFrame | None = None,
    primary_source_bar_hash: str | None = None,
    primary_adj_factor_hash: str | None = None,
    source_run_id: uuid.UUID | None = None,
    instrument_symbol: str | None = None,
    run_calculated_at: str | None = None,
    _diag_sink: dict[str, Any] | None = None,
    core_context: CoreRunContext | None = None,
) -> StockFeatureSnapshot:
    """[CHANGE-20260729-003] 盘后 review core 计算路径（daily-core only）。

    与 compute_feature_snapshot_for_date 的关键差异（PRD20 盘后核心/筹码解耦）：
    - **禁止 Node Cluster 调用**：不调用 NodeClusterInputProvider，不调用 node_cluster adapter
    - **禁止 15m secondary 输入**：不获取 15m bars，不调用 15m structural_features
    - **使用 compute_first_pyramid_core_snapshot**：first_pyramid 只含 trend/structure/momentum，
      chip_consensus=None（chip 由独立 after_close_chip_consensus job 异步计算）
    - **不得用单周期 VP 伪装筹码**：primary 的 cost_position 仍由 structural_features adapter
      计算（single-period VP，仅用于节点价格水位），但 first_pyramid.chipConsensus 显式为 None；
      primary_payload.node_cluster 写入 `review_core_no_chip` 标记，禁止下游误读为筹码共识

    现有非盘后调用（compute_feature_snapshot_for_date）保持兼容，不受影响。

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        trade_date: 业务交易日
        primary_timeframe: 主周期（默认 1d）
        adj: 复权方式（默认 qfq）
        primary_bars: 预加载的日线 bars（可选，不传则从 DB 获取）
        primary_source_bar_hash: 预加载 bars 的 source_bar_hash（[AC-16] 批读时从
            BarAggregationResult 传入，不传且 primary_bars 传入时为 None）
        primary_adj_factor_hash: 预加载 bars 的 adj_factor_hash（同上）
        source_run_id: 关联的 snapshot run id
        _diag_sink: 诊断信息收集 dict

    Returns:
        StockFeatureSnapshot ORM 对象（未写入 DB）
        - summary_payload.first_pyramid 仅含 core 字段（无 chipConsensus）
        - summary_payload._review_core = True（标记 review core 路径）
        - degraded_reasons 含 "review_core: chip_consensus deferred"
    """
    degraded_reasons: list[str] = ["review_core: chip_consensus deferred to async after_close_chip_consensus job"]
    warmup_notes: list[str] = []

    # 获取日线 K 线（如果未预加载）
    if primary_bars is None:
        primary_bars, primary_diag = await _fetch_bars_from_db(
            session, instrument_id, primary_timeframe, adj, trade_date,
        )
        if _diag_sink is not None and primary_diag:
            _diag_sink.update(primary_diag)
        primary_adj_factor_hash = primary_diag.get("adj_factor_hash") if primary_diag else None
        primary_source_bar_hash = primary_diag.get("source_bar_hash") if primary_diag else None

    # point-in-time 截断（仅日线）
    df_1d = _truncate_bars_to_trade_date(primary_bars, trade_date, primary_timeframe)

    # 数据不足检查
    if df_1d is None:
        degraded_reasons.append(f"{primary_timeframe}: no bars <= {trade_date}")
    elif len(df_1d) < 60:
        degraded_reasons.append(
            f"{primary_timeframe}: insufficient bars ({len(df_1d)} < 60)"
        )

    # [CHANGE-20260805-CP4A-CP3 / P0-03] compute-once：通过**唯一 kernel owner**
    # `compute_core_kernel_bundle` 一次性算出 raw 结果（DSA/SMC/Bollinger/SQZMOM/VolumeContext），
    # 供 structural_features 与 compute_core_artifact 共享。禁止上层调用私有
    # `_compute_first_pyramid_raw_results`。若 bundle 暴露了 smc/volume 结果也一并复用。
    # [CHANGE-20260806-005 / Phase 1 / PC-02] 传入 run-scoped diagnostics（core_context 的
    # compute_diagnostics），使五类 kernel 计数在实际调用点递增（compute-once 门禁依据）。
    _shared_raw: Any = None
    if df_1d is not None and not df_1d.empty and len(df_1d) >= 60:
        try:
            from app.services.core_artifact_service import compute_core_kernel_bundle
            _kernel_diag = (
                core_context.compute_diagnostics if core_context is not None else None
            )
            _shared_raw = compute_core_kernel_bundle(df_1d, _kernel_diag)
        except Exception as raw_exc:
            logger.warning("compute_review_core_for_trade_date raw 预计算失败: %s", raw_exc)
            _shared_raw = None

    # [CHANGE-20260729-003] 禁止 Node Cluster：precomputed_node_cluster=None
    # structural_features adapter 内部会回退到单周期 VP 计算 cost_position
    # （仅用于节点价格水位，不作为筹码共识）
    _structural_precomputed: dict[str, Any] | None = None
    if _shared_raw is not None:
        _structural_precomputed = {
            "dsa_bundle": _shared_raw.dsa_bundle,
            "bb_df": _shared_raw.bb_df,
            "sqz_result": _shared_raw.sqzmom_result,
            # [CP4A-CP3] 若 structural 消费者需要 SMC/VolumeContext 结果，从同一 bundle 复用
            **(
                {"smc_result": _shared_raw.smc_result}
                if getattr(_shared_raw, "smc_result", None) is not None
                else {}
            ),
            **(
                {"vc_series": _shared_raw.vc_series}
                if getattr(_shared_raw, "vc_series", None) is not None
                else {}
            ),
        }
    primary_canonical = await CanonicalComputationService.compute(
        algorithm_id="structural_features",
        instrument_id=instrument_id,
        as_of=trade_date.isoformat(),
        source_bar_hash=primary_source_bar_hash,
        adj_factor_hash=primary_adj_factor_hash,
        bars=df_1d if df_1d is not None else pd.DataFrame(),
        timeframe=primary_timeframe,
        precomputed_node_cluster=None,  # [CHANGE-20260729-003] 禁止 Node Cluster
        precomputed=_structural_precomputed,  # [CP4A P0-03] 复用 raw，不重算 kernel
    )
    primary_factors = primary_canonical.payload
    degraded_reasons.extend(primary_factors.pop("degraded_reasons", []))
    warmup_notes.extend(primary_factors.pop("warmup_notes", []))

    # C6: MACD 紧凑状态
    primary_factors["macd_state"] = await _compute_macd_state(
        df_1d, instrument_id, trade_date, primary_source_bar_hash, primary_adj_factor_hash,
    )

    # temporal features：daily_context 用 primary，m15_response 为空（无 15m 输入）
    daily_context = _compute_daily_context(
        primary_factors, df_1d, degraded_reasons, warmup_notes
    )
    # [CHANGE-20260729-003] review core 禁止 15m 输入：m15_response 为空 dict
    empty_m15_response: dict[str, Any] = {}
    derived_relation = _compute_derived_relation(
        daily_context, empty_m15_response, degraded_reasons
    )

    # current_price（用于 price_state 兜底）
    current_price_for_state: float | None = None
    if df_1d is not None and not df_1d.empty:
        try:
            current_price_for_state = float(df_1d["close"].iloc[-1])
        except (IndexError, ValueError, KeyError):
            current_price_for_state = None

    # primary_payload：node_cluster 字段写 review_core_no_chip 标记
    # 禁止用单周期 VP 伪装筹码（cost_position 保留用于节点水位，但不写入 chip_consensus）
    primary_payload: dict[str, Any] = {**primary_factors}
    primary_payload["node_cluster"] = {
        "availability": "review_core_no_chip",
        "degraded_reason": "review_core: Node Cluster deferred to async chip_consensus job",
        "profile_hash": None,
        "poc_price": None,
        "vah_price": None,
        "val_price": None,
        "daily_source_hash": None,
        "bars_15m_source_hash": None,
        "algorithm_version": None,
        "output_schema_version": None,
        "contract_fingerprint": None,
        "daily_bars_count": len(df_1d) if df_1d is not None else 0,
        "bars_15m_count": 0,
        "profile_rows": [],
        "peak_rows": [],
        "all_peak_prices": [],
        "price_state": {
            "current_price": current_price_for_state,
            "position_0_1": None,
            "upper_node_ref": None,
            "lower_node_ref": None,
            "poc_node_ref": None,
            "last_touched_node_ref": None,
        },
    }

    structural_payload: dict[str, Any] = {
        "primary": {primary_timeframe: primary_payload},
        "secondary": {},  # [CHANGE-20260729-003] review core 禁止 15m
        "relation": derived_relation,
        "meta": {
            "degraded_reasons": degraded_reasons,
            "warmup_notes": warmup_notes,
        },
    }

    temporal_payload: dict[str, Any] = {
        "daily_context": daily_context,
        "m15_response": empty_m15_response,
        "derived_relation": derived_relation,
        "meta": {
            "degraded_reasons": degraded_reasons,
            "warmup_notes": warmup_notes,
        },
    }

    extra = await _extract_extra_fields(
        df_1d, instrument_id, trade_date, primary_source_bar_hash, primary_adj_factor_hash,
    )

    source_primary = _normalize_primary_bar_time(df_1d, trade_date)
    source_bar_time_str = source_primary.isoformat() if source_primary else None

    # [CHANGE-20260805-CP4A / P0-03] review core：经统一入口 compute_core_artifact
    # 各算法（DSA/SMC/momentum/VolumeContext）每股只计算一次，First Pyramid 由纯
    # builder 组装；DSA projection metrics/visual 一并提取（P0-05 round-trip）。
    # 不再单独调用 compute_first_pyramid_core_snapshot（会重复计算算法 kernel）。
    first_pyramid_dict: dict[str, Any] | None = None
    # [CHANGE-20260805-CP4A-CP3 / P0-05] 持有一份 core artifact 引用，用于在 summary 持久化
    # 完整 versioned DSA projection（dsaProjectionPayload/visual/availability/lineage）。
    _core_artifact: Any | None = None
    try:
        from datetime import datetime as _py_dt
        from zoneinfo import ZoneInfo

        from app.services.core_artifact_service import compute_core_artifact
        from app.services.core_run_context import CoreRunContext
        from app.services.first_pyramid_service import (
            inject_field_availability_provenance,
        )

        if df_1d is not None and not df_1d.empty and len(df_1d) >= 60:
            # [P0-symbol合同 2026-07-30] 公共 symbol 必须是规范化6位股票代码
            if instrument_symbol is None:
                instrument_symbol = await _get_symbol(session, instrument_id)
            if instrument_symbol is None:
                logger.warning(
                    "compute_review_core_for_trade_date instrument_symbol 查询失败，回退 UUID: "
                    "instrument_id=%s trade_date=%s",
                    instrument_id, trade_date,
                )
                symbol_for_pyramid = str(instrument_id)
            else:
                symbol_for_pyramid = instrument_symbol
            # run-scoped CoreRunContext：run 级唯一事实源由编排器在 run 入口创建一次并冻结
            # （P0-02），逐股函数**禁止**自行创建 context。core_context 为 None 时（仅兼容
            # 直接调用此函数的外部/测试），回退构造一个最小的单股 context。
            if core_context is None:
                from app.services.core_run_context import (
                    build_default_algorithm_versions,
                )

                ctx = CoreRunContext(
                    trade_date=trade_date,
                    run_calculated_at=(
                        _py_dt.fromisoformat(run_calculated_at)
                        if run_calculated_at
                        else _py_dt.now(ZoneInfo("Asia/Shanghai"))
                    ),
                    algorithm_versions=build_default_algorithm_versions(),
                    config={},
                    run_id=source_run_id,
                )
            else:
                ctx = core_context
            core_artifact = compute_core_artifact(
                context=ctx,
                instrument_id=instrument_id,
                symbol=symbol_for_pyramid,
                daily_frame=df_1d,
                input_hash=(primary_source_bar_hash or ""),
                bars_hash=(primary_source_bar_hash or ""),
                adj_factor_hash=(primary_adj_factor_hash or ""),
                # [CHANGE-20260805-CP4A / P0-03] 复用与 structural_features 共享的 raw 结果
                precomputed_raw=_shared_raw,
            )
            _core_artifact = core_artifact
            fp_core_payload = core_artifact.payload["first_pyramid"]
            # core snapshot 序列化：chip_consensus 显式为 None
            first_pyramid_dict = dict(fp_core_payload)
            first_pyramid_dict["chipConsensus"] = None
            first_pyramid_dict["_review_core"] = True
            # [QM-62/QM-63 run 级来源合同 2026-08-04] 注入 run 级来源。
            # 同一 run 的所有股票必须共享完全相同的 sourceRunId 与 calculatedAt。
            first_pyramid_dict["sourceRunId"] = (
                str(source_run_id) if source_run_id is not None else None
            )
            if run_calculated_at is not None:
                first_pyramid_dict["calculatedAt"] = run_calculated_at
            # [字段级 availability 合同 2026-08-04] 盘后主链必须持久化字段级原因。
            fp_avail = fp_core_payload.get("fieldAvailability") or {}
            if fp_avail:
                first_pyramid_dict["fieldAvailability"] = (
                    inject_field_availability_provenance(
                        fp_avail,
                        source_run_id=(
                            str(source_run_id) if source_run_id is not None else None
                        ),
                        calculated_at=run_calculated_at,
                    )
                )
    except Exception as exc:
        logger.warning(
            "review core 第一金字塔计算失败 instrument_id=%s trade_date=%s: %s",
            instrument_id, trade_date, exc,
        )
        first_pyramid_dict = None
        # [FP 失败完整性 2026-08-04] 第一金字塔属 core 必选结果。计算失败不能
        # 以无原因的 first_pyramid=None 冒充成功——记录明确的 FP_COMPUTE_FAILED，
        # 由下游从 publish-ready coverage 排除，避免"盘后任务成功但 FP 大量为空"。
        if "first_pyramid: compute failed" not in degraded_reasons:
            degraded_reasons.append("first_pyramid: compute failed")

    summary_payload = build_summary_payload(
        structural_payload, temporal_payload, trade_date,
        source_bar_time=source_bar_time_str, extra=extra,
        first_pyramid=first_pyramid_dict,
        source_run_id=source_run_id,
    )
    # 标记 review core 路径（供下游区分）
    summary_payload["_review_core"] = True
    # [FP 失败完整性 2026-08-04] 显式 FP 状态：ready / insufficient_history / FP_COMPUTE_FAILED
    if "first_pyramid: compute failed" in degraded_reasons:
        summary_payload["first_pyramid_status"] = "FP_COMPUTE_FAILED"
    elif first_pyramid_dict is None:
        summary_payload["first_pyramid_status"] = "insufficient_history"

    # [CHANGE-20260805-CP4A-CP3 / P0-05] 持久化完整 versioned DSA projection 载荷，
    # 供 CoreArtifactCodec 直接从 summary 读取，不再从面向 UI 的 continuousFactors 反向拼装。
    # 含：dsaProjectionPayload（指标）、dsaVisualContract、availability、lineage、schemaVersion。
    if _core_artifact is not None:
        # [CHANGE-20260806-CP4A.1 / Item 5] 持久化完整 versioned core artifact（不止 DSA projection），
        # 供正常链与 restart 链统一 decode（restart 不再从 continuousFactors 反向拼装）。
        from app.services.core_artifact_codec import encode_core_artifact_to_summary
        summary_payload["coreArtifact"] = encode_core_artifact_to_summary(
            # [CHANGE-20260806-005 / Phase 1 / PC-11] 使用 artifact 自身的 schema_version
            # （与 codec 的 CORE_ARTIFACT_SCHEMA_VERSION 单一真源一致），保证 round-trip 无损。
            schema_version=getattr(_core_artifact, "schema_version", 1),
            first_pyramid_core=dict(_core_artifact.payload.get("first_pyramid") or {}),
            structural_payload=dict(structural_payload or {}),
            dsa_projection_payload=dict(_core_artifact.payload.get("dsa") or {}),
            dsa_visual_contract=dict(_core_artifact.visual or {}),
            state_event_candidates=list(_core_artifact.events or []),
            availability=dict(_core_artifact.availability or {}),
            parameter_hash=_core_artifact.parameter_hash,
            source_core_run_id=(
                str(_core_artifact.source_core_run_id)
                if _core_artifact.source_core_run_id is not None else None
            ),
            algorithm_versions=dict(_core_artifact.algorithm_versions or {}),
            input_hash=(_core_artifact.hashes or {}).get("input_hash"),
            bars_hash=(_core_artifact.hashes or {}).get("bars_hash"),
            adj_factor_hash=(_core_artifact.hashes or {}).get("adj_factor_hash"),
            diagnostics=dict(_core_artifact.diagnostics or {}),
        )
        # 保留 dsaProjection 块（旧 codec 兼容）
        summary_payload["dsaProjection"] = {
            "schemaVersion": 1,
            "dsaProjectionPayload": dict(_core_artifact.payload.get("dsa") or {}),
            "dsaVisualContract": dict(_core_artifact.visual or {}),
            "availability": dict(_core_artifact.availability or {}),
            "lineage": {
                "parameterHash": _core_artifact.parameter_hash,
                "sourceCoreRunId": (
                    str(_core_artifact.source_core_run_id)
                    if _core_artifact.source_core_run_id is not None else None
                ),
                "algorithmVersions": dict(_core_artifact.algorithm_versions or {}),
                "inputHash": (_core_artifact.hashes or {}).get("input_hash"),
                "barsHash": (_core_artifact.hashes or {}).get("bars_hash"),
                "adjFactorHash": (_core_artifact.hashes or {}).get("adj_factor_hash"),
            },
        }

    return StockFeatureSnapshot(
        instrument_id=instrument_id,
        trade_date=trade_date,
        primary_timeframe=primary_timeframe,
        secondary_timeframe="15m",  # 保留字段以兼容 StockFeatureSnapshot 模型；实际未计算
        adj=adj,
        schema_version=_SCHEMA_VERSION,
        source_run_id=source_run_id,
        source_primary_bar_time=source_primary,
        source_secondary_bar_time=None,  # review core 无 15m
        structural_payload=structural_payload,
        temporal_payload=temporal_payload,
        summary_payload=summary_payload,
        degraded_reasons=degraded_reasons,
    )


async def compute_review_core_batch_for_trade_date(
    session: AsyncSession,
    trade_date: date,
    instrument_ids: Sequence[uuid.UUID] | None,
    *,
    batch_size: int = 20,
    failure_threshold: float = 0.3,
    progress_callback: Callable[..., Awaitable[None]] | None = None,
    source_run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """[P0-6 修复 2026-07-29] 盘后 review core 批量计算入口（daily-core only）。

    主链：日线 → daily-core 状态/事件 → 质量门禁 → 发布 → 主 run succeeded。
    禁止 Node Cluster 调用与 15m secondary 输入，所有 instrument 通过
    `compute_review_core_for_trade_date` 计算（chip_consensus=None 显式标记）。
    chip 共识由独立 after_close_chip_consensus job 异步执行，不在此函数内调用。

    与 `compute_for_trade_date` 关键差异：
    - 调用 `compute_review_core_for_trade_date` 而非 `compute_feature_snapshot_for_date`
    - 不触发 Node Cluster / 15m secondary
    - first_pyramid.chipConsensus 显式 None（chip 延后到异步 job）
    - summary_payload._review_core = True（供下游区分 review core 路径）

    事务边界（与 compute_for_trade_date 对齐）：
    - 本函数只 upsert（flush）+ 返回统计，不调用 session.commit()
    - 失败比例超 failure_threshold 时抛 RuntimeError，由 caller 决定 rollback
    - caller（after_close_orchestrator）负责成功 commit / 超阈值 rollback

    Args:
        session: 异步 DB 会话
        trade_date: 交易日
        instrument_ids: 标的 ID 列表
        batch_size: 每批 instrument 数（默认 20）
        failure_threshold: 失败比例阈值（默认 0.3）
        progress_callback: 进度回调，接收 processed/total/snapshot_count/failed_count
        source_run_id: 关联的 snapshot run ID

    Returns:
        统计信息 dict：snapshot_count, failed_count, schema_version, trade_date,
        source_bar_hash, adj_factor_hash, market_data_contract_version,
        completed_through, adjustment_as_of

    Raises:
        RuntimeError: 失败比例超过 failure_threshold（caller 应 rollback）
        ValueError: [QM-62] 缺 source_run_id（不得产出无来源快照）
    """
    if instrument_ids is None:
        instrument_ids = []
    total = len(instrument_ids)
    # [QM-62 run 级来源合同 2026-08-04] 批任务入口缺 sourceRunId 必须直接失败。
    # 否则会产出"一半有来源、一半没有来源"的快照，下游无法追溯与比对。
    if total > 0 and source_run_id is None:
        raise ValueError(
            "[QM-62] compute_review_core_batch_for_trade_date 缺少 source_run_id："
            f"trade_date={trade_date}, instruments={total}。"
            "批量入口必须提供 run 级来源，禁止产出无来源快照。"
        )
    snapshot_count = 0
    failed_count = 0
    # [CHANGE-20260717-002 SSOT] run 级行情诊断（取首个成功 instrument 的 primary_diag 为权威）
    run_diag: dict[str, Any] = {}
    # [QM-62 run 级来源合同 2026-08-04] 整个 run 只取一次时钟，
    # 保证同 run 所有股票 calculatedAt 完全相同。
    run_calculated_at = _make_run_calculated_at()
    # [P0-symbol合同 2026-07-30] 一次查询 instrument_id → symbol 映射，避免 N 次 DB 查询
    symbol_map: dict[uuid.UUID, str | None] = {}
    if total > 0:
        sym_rows = await session.execute(
            text("SELECT id, symbol FROM instruments WHERE id = ANY(:ids)"),
            {"ids": list(instrument_ids)},
        )
        symbol_map = {row[0]: row[1] for row in sym_rows}

    for i in range(0, total, batch_size):
        batch = instrument_ids[i : i + batch_size]
        for instrument_id in batch:
            try:
                snapshot = await compute_review_core_for_trade_date(
                    session,
                    instrument_id,
                    trade_date,
                    source_run_id=source_run_id,
                    instrument_symbol=symbol_map.get(instrument_id),
                    run_calculated_at=run_calculated_at,
                    _diag_sink=run_diag,
                )
                # [FP 失败完整性 2026-08-04] 第一金字塔计算失败（FP_COMPUTE_FAILED）
                # 不能计入成功快照：core 必选结果缺失，纳入 failed 由失败阈值兜底，
                # 且不参与 publish-ready coverage。
                fp_status = (snapshot.summary_payload or {}).get(
                    "first_pyramid_status"
                )
                if fp_status == "FP_COMPUTE_FAILED":
                    failed_count += 1
                    logger.error(
                        "review_core 第一金字塔计算失败不计入成功 instrument_id=%s "
                        "trade_date=%s",
                        instrument_id, trade_date,
                    )
                    continue
                await upsert_snapshot(session, snapshot)
                snapshot_count += 1
            except Exception as exc:
                failed_count += 1
                logger.error(
                    "review_core snapshot 计算失败 instrument_id=%s trade_date=%s: %s",
                    instrument_id, trade_date, exc, exc_info=True,
                )

        # [Heartbeat] 每批完成后回调进度，供长任务更新心跳/lease 与 metadata
        if progress_callback is not None:
            try:
                await progress_callback(
                    processed=min(i + len(batch), total),
                    total=total,
                    snapshot_count=snapshot_count,
                    failed_count=failed_count,
                )
            except Exception as exc:
                logger.warning(
                    "progress_callback 失败 trade_date=%s: %s",
                    trade_date, exc,
                )

    # 检查失败阈值（不 commit，由 caller 决定 commit/rollback）
    if total > 0:
        failure_rate = failed_count / total
        if failure_rate > failure_threshold:
            raise RuntimeError(
                f"review_core snapshot 失败比例 {failure_rate:.1%} 超过阈值 "
                f"{failure_threshold:.0%} (failed={failed_count}, total={total})"
            )

    logger.info(
        "review_core 批量完成 trade_date=%s snapshot_count=%d failed_count=%d",
        trade_date, snapshot_count, failed_count,
    )

    return {
        "snapshot_count": snapshot_count,
        "failed_count": failed_count,
        "schema_version": _SCHEMA_VERSION,
        "trade_date": trade_date.isoformat(),
        # [CHANGE-20260717-002 SSOT] run 级行情诊断（供 finish_snapshot_run 落库）
        "source_bar_hash": run_diag.get("source_bar_hash"),
        "adj_factor_hash": run_diag.get("adj_factor_hash"),
        "market_data_contract_version": run_diag.get("market_data_contract_version"),
        "completed_through": run_diag.get("completed_through"),
        "adjustment_as_of": run_diag.get("adjustment_as_of"),
        # [P0-6] 标记 review core 路径（供下游区分）
        "_review_core": True,
    }


async def compute_review_core_with_run_items(
    trade_date: date,
    instrument_ids: Sequence[uuid.UUID],
    snapshot_run_id: uuid.UUID,
    *,
    worker_id: str = "orchestrator",
    lease_epoch: int | None = None,
    batch_size: int = 25,
    failure_threshold: float = 0.3,
    progress_callback: Callable[..., Awaitable[None]] | None = None,
    algorithm_version: str = "v1",
    input_hash: str | None = None,
    released_config_resolver: Any | None = None,
    # [CHANGE-20260806-CP4A.2 / Step1] 依赖注入：测试可显式传 fake session factory /
    # market_data provider / runtime，生产默认用正式实现。使完整 scheduled 链可真实执行。
    session_factory: Any | None = None,
) -> dict[str, Any]:
    """[CHANGE-20260729-008] 单股×阶段检查点版 review core 计算。

    与 compute_review_core_batch_for_trade_date 关键差异：
    - 使用 stock_feature_snapshot_run_items 表做单股 claim/lease/commit
    - 每只股票在独立短事务中计算并 commit（失败只回滚该股）
    - coverage 从 run_items 实时统计（不靠调用方传值）
    - 成功且 hash/version 相同不重算（create_run_items 幂等 + succeeded 跳过）
    - 恢复只领 pending/可重试 failed/过期 running

    流程：
    1. create_run_items（幂等 INSERT ON CONFLICT DO NOTHING）
    2. 循环 claim_items → 逐股计算（独立事务）→ mark_item_succeeded/failed
    3. 返回统计 + run_diag

    不在此函数内调用 publish_stock_core（由 caller 在 coverage 达标后调用）。

    Args:
        trade_date: 交易日
        instrument_ids: eligible universe
        snapshot_run_id: StockFeatureSnapshotRun.id
        worker_id: Worker 标识
        lease_epoch: lease_epoch for fencing
        batch_size: claim 批次大小
        failure_threshold: 整体失败比例阈值
        progress_callback: 进度回调
        algorithm_version: 算法版本
        input_hash: 输入 hash

    Returns:
        统计 dict（含 snapshot_count/failed_count/skipped_count/coverage 等）
    """
    from app.db import AsyncSessionLocal
    # [CHANGE-20260806-CP4A.2 / Step1] 依赖注入的 session factory（测试传 fake；生产默认正式）
    _sf = session_factory if session_factory is not None else AsyncSessionLocal
    from app.services.snapshot_run_item_service import (
        claim_items,
        create_run_items,
        get_run_progress,
        mark_item_failed,
        mark_item_succeeded,
    )

    # [QM-62 run 级来源合同 2026-08-04] 整个 run 只取一次时钟，
    # 保证同 run 所有股票 calculatedAt 完全相同（禁止单股各自取时钟）。
    run_calculated_at = _make_run_calculated_at()

    # [CHANGE-20260805-CP4A-CP3 / P0-02] 在 run 入口创建一次 run-level CoreRunContext 并冻结，
    # 传给全部股票（逐股禁止自行创建 context）。**released config 唯一来源**：scheduled 模式
    # 必须解析 released dsa_selector StrategyVersion，无 released 时 fail-closed（禁止回退代码常量）。
    # 冻结：run_calculated_at / eligible universe hash / released DSA config / market-data contract
    # / adjustment contract / parameter hash / execution contract。
    from app.services.core_run_context import (
        SqlAlchemyReleasedConfigResolver,
        resolve_core_run_context,
    )

    if released_config_resolver is not None:
        run_core_context = await resolve_core_run_context(
            trade_date=trade_date,
            snapshot_run_id=snapshot_run_id,
            eligible_instrument_ids=instrument_ids,
            run_calculated_at=datetime.fromisoformat(run_calculated_at),
            resolver=released_config_resolver,
        )
    else:
        async with _sf() as cfg_db:
            run_core_context = await resolve_core_run_context(
                trade_date=trade_date,
                snapshot_run_id=snapshot_run_id,
                eligible_instrument_ids=instrument_ids,
                run_calculated_at=datetime.fromisoformat(run_calculated_at),
                resolver=SqlAlchemyReleasedConfigResolver(cfg_db),
            )

    # 1. 创建 run items（幂等）
    async with _sf() as db:
        created = await create_run_items(
            db, snapshot_run_id, instrument_ids,
            input_hash=input_hash,
        )
        await db.commit()
        if created > 0:
            logger.info(
                "[RunItems] 创建 %d 个 core/pending items: snapshot_run_id=%s",
                created, snapshot_run_id,
            )

    # 2. 循环 claim → compute → commit → mark
    snapshot_count = 0
    failed_count = 0
    skipped_count = 0
    total = len(instrument_ids)
    run_diag: dict[str, Any] = {}
    # [AC-16] 低基数 metrics：批次数、MDAS 批读次数（支持性能回归核验）
    batch_count = 0
    mdas_batch_read_count = 0

    while True:
        # 2.1 claim 一批 items（独立 session）
        async with _sf() as db:
            items = await claim_items(
                db, snapshot_run_id,
                worker_instance_id=worker_id,
                batch_size=batch_size,
            )
            await db.commit()

        if not items:
            break  # 无可领取 items，完成

        batch_count += 1

        # [P0-symbol合同 2026-07-30] 批量查询本批 items 的 instrument_id → symbol 映射
        async with AsyncSessionLocal() as sym_db:
            sym_rows = await sym_db.execute(
                text("SELECT id, symbol FROM instruments WHERE id = ANY(:ids)"),
                {"ids": [item.instrument_id for item in items]},
            )
            batch_symbol_map: dict[uuid.UUID, str | None] = {
                row[0]: row[1] for row in sym_rows
            }

        # [AC-16] 本批 items 通过 MDAS 批量入口一次预读 1d bars（review core 只允许日线；
        # 同一股票、周期、交易日的 canonical frame 与诊断 hash 在该批内复用），
        # 避免逐股 _fetch_bars_from_db 的 N×2 次 DB 往返。仍保持每股独立事务
        # （AC-08 单股×阶段检查点），批读只降低行情读取开销，不改变提交边界。
        primary_batch_results: dict[
            uuid.UUID, BarAggregationResult | Exception
        ] = {}
        try:
            async with AsyncSessionLocal() as mdas_db:
                primary_batch_results = await _get_mdas().get_bars_batch(
                    mdas_db,
                    [item.instrument_id for item in items],
                    timeframe="1d", adj="qfq",
                    include_realtime=False, completed_only=True,
                    end_date=trade_date, adjustment_as_of=trade_date,
                )
            mdas_batch_read_count += 1
        except Exception as mdas_exc:
            # 批读整体失败不阻断：降级为逐股读取（不抛，保留原语义）
            logger.error(
                "[RunItems] MDAS 批读失败，降级逐股读取: batch=%d, error=%s",
                batch_count, mdas_exc,
            )

        # 2.2 逐股计算（每股独立事务）
        for item in items:
            try:
                # 计算在事务外（长事务避免锁竞争）
                async with _sf() as compute_db:
                    # [AC-16] 从批读结果取 bars + 诊断 hash；失败/缺失则降级（bars=None
                    # 触发 compute_review_core_for_trade_date 内部逐股 _fetch_bars_from_db）
                    primary_result = primary_batch_results.get(item.instrument_id)
                    primary_bars = (
                        primary_result.bars
                        if isinstance(primary_result, BarAggregationResult)
                        else None
                    )
                    pre_hash = (
                        primary_result.source_bar_hash
                        if isinstance(primary_result, BarAggregationResult)
                        else None
                    )
                    pre_adj_hash = (
                        primary_result.adj_factor_hash
                        if isinstance(primary_result, BarAggregationResult)
                        else None
                    )
                    snapshot = await compute_review_core_for_trade_date(
                        compute_db,
                        item.instrument_id,
                        trade_date,
                        primary_bars=primary_bars,
                        primary_source_bar_hash=pre_hash,
                        primary_adj_factor_hash=pre_adj_hash,
                        source_run_id=snapshot_run_id,
                        instrument_symbol=batch_symbol_map.get(item.instrument_id),
                        run_calculated_at=run_calculated_at,
                        _diag_sink=run_diag,
                        # [CHANGE-20260805-CP4A / P0-02] run 级唯一 context，全部股票共享
                        core_context=run_core_context,
                    )
                    # [FP 失败完整性 2026-08-04] 第一金字塔计算失败（FP_COMPUTE_FAILED）
                    # 属 core 必选结果缺失，不能标记 succeeded：改为 failed，使
                    # publish-ready coverage 排除该股票，避免"任务成功但 FP 大量为空"。
                    fp_status = (snapshot.summary_payload or {}).get(
                        "first_pyramid_status"
                    )
                    if fp_status == "FP_COMPUTE_FAILED":
                        await upsert_snapshot(compute_db, snapshot)
                        await compute_db.commit()
                        failed_count += 1
                        async with AsyncSessionLocal() as fail_fp_db:
                            await mark_item_failed(
                                fail_fp_db, item.id,
                                error="first_pyramid compute failed (FP_COMPUTE_FAILED)",
                                lease_epoch=item.lease_epoch,
                            )
                            await fail_fp_db.commit()
                        logger.error(
                            "[RunItems] item %s 第一金字塔计算失败，标记 failed",
                            item.id,
                        )
                        continue

                    await upsert_snapshot(compute_db, snapshot)
                    await compute_db.commit()

                # 标记 succeeded（独立短事务 + lease_epoch fencing）
                async with AsyncSessionLocal() as mark_db:
                    ok = await mark_item_succeeded(
                        mark_db, item.id,
                        result_count=1,
                        lease_epoch=item.lease_epoch,
                    )
                    await mark_db.commit()

                if ok:
                    snapshot_count += 1
                else:
                    # lease_epoch 不匹配，被其他 Worker 接管
                    logger.warning(
                        "[RunItems] item %s lease_epoch 不匹配，跳过（已被接管）",
                        item.id,
                    )

            except Exception as exc:
                failed_count += 1
                logger.error(
                    "review_core 单股计算失败 instrument_id=%s trade_date=%s: %s",
                    item.instrument_id, trade_date, exc, exc_info=True,
                )
                # 标记 failed（独立事务）
                try:
                    async with AsyncSessionLocal() as fail_db:
                        await mark_item_failed(
                            fail_db, item.id,
                            error=str(exc),
                            lease_epoch=item.lease_epoch,
                        )
                        await fail_db.commit()
                except Exception as mark_exc:
                    logger.error(
                        "mark_item_failed 失败 item_id=%s: %s",
                        item.id, mark_exc,
                    )

        # 2.3 进度回调
        if progress_callback is not None:
            try:
                await progress_callback(
                    processed=snapshot_count + failed_count + skipped_count,
                    total=total,
                    snapshot_count=snapshot_count,
                    failed_count=failed_count,
                )
            except Exception as exc:
                logger.warning("progress_callback 失败: %s", exc)

    # 3. 从 DB 统计最终 coverage
    async with _sf() as db:
        progress = await get_run_progress(db, snapshot_run_id)

    coverage = progress.get("coverage", 0.0)
    succeeded = progress.get("succeeded", 0)
    failed = progress.get("failed", 0)
    skipped = progress.get("skipped", 0)

    # 4. 检查失败阈值（基于 items 统计，不是局部计数器）
    if total > 0:
        # 用 DB 统计的 failed 率
        failure_rate = failed / total if total > 0 else 0.0
        if failure_rate > failure_threshold:
            raise RuntimeError(
                f"review_core snapshot 失败比例 {failure_rate:.1%} 超过阈值 "
                f"{failure_threshold:.0%} (failed={failed}, total={total})"
            )

    logger.info(
        "[RunItems] review_core_with_run_items 完成: trade_date=%s, "
        "succeeded=%d, failed=%d, skipped=%d, coverage=%.4f, "
        "batches=%d, mdas_batch_reads=%d",
        trade_date, succeeded, failed, skipped, coverage,
        batch_count, mdas_batch_read_count,
    )

    return {
        "snapshot_count": succeeded,
        "failed_count": failed,
        "skipped_count": skipped,
        "coverage": coverage,
        "batch_count": batch_count,
        "mdas_batch_read_count": mdas_batch_read_count,
        "schema_version": _SCHEMA_VERSION,
        "trade_date": trade_date.isoformat(),
        "source_bar_hash": run_diag.get("source_bar_hash"),
        "adj_factor_hash": run_diag.get("adj_factor_hash"),
        "market_data_contract_version": run_diag.get("market_data_contract_version"),
        "completed_through": run_diag.get("completed_through"),
        "adjustment_as_of": run_diag.get("adjustment_as_of"),
        "_review_core": True,
        "_uses_run_items": True,
    }


def _get_mdas() -> MarketDataAggregationService:
    """[AC-16] 返回 MDAS 实例（批读唯一入口 get_bars_batch 的提供者）。"""
    return MarketDataAggregationService()


async def _fetch_bars_from_db(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    trade_date: date,
) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    """从 DB 获取 K 线数据（通过 MarketDataAggregationService，point-in-time）。

    [CHANGE-20260717-002 SSOT] 盘后/历史回算必须 point-in-time：
    - include_realtime=False / completed_only=True（只用已完成 bar）
    - end_date=trade_date（不读取 trade_date 之后数据）
    - adjustment_as_of=trade_date（复权锚点，禁止未来除权事件泄漏）

    返回 (bars, diag) 二元组；diag 含 source_bar_hash/adj_factor_hash/
    market_data_contract_version/completed_through/adjustment_as_of/degraded/degraded_reason。
    失败时返回 (None, {})。
    """
    from app.services.market_data_aggregation_service import MarketDataAggregationService

    try:
        service = MarketDataAggregationService()
        result = await service.get_bars(
            session,
            instrument_id,
            timeframe=timeframe,
            adj=adj,
            include_realtime=False,
            completed_only=True,
            end_date=trade_date,
            adjustment_as_of=trade_date,
        )
        bars = result.bars
        # [CHANGE-20260717-002 SSOT] completed_through 从 MDAS 返回为 pd.Timestamp，
        # 转换为 tz-aware datetime 便于 finish_snapshot_run 落库（DateTime(timezone=True) 列）
        _ct = result.completed_through
        if _ct is not None and isinstance(_ct, pd.Timestamp):
            if _ct.tzinfo is None:
                _ct = _ct.tz_localize("Asia/Shanghai")
            _ct = _ct.to_pydatetime()
        diag: dict[str, Any] = {
            "source_bar_hash": result.source_bar_hash,
            "adj_factor_hash": result.adj_factor_hash,
            "market_data_contract_version": result.market_data_contract_version,
            "completed_through": _ct,
            "adjustment_as_of": result.adjustment_as_of,
            "degraded": result.degraded,
            "degraded_reason": result.degraded_reason,
        }
        if bars is None or bars.empty:
            return None, diag
        return bars, diag
    except Exception as exc:
        logger.warning(
            "get_bars 失败 instrument_id=%s timeframe=%s: %s",
            instrument_id, timeframe, exc,
        )
        return None, {}


async def _extract_extra_fields(
    df_1d: pd.DataFrame | None,
    instrument_id: uuid.UUID,
    trade_date: date,
    source_bar_hash: str | None = None,
    adj_factor_hash: str | None = None,
) -> dict[str, Any]:
    """从日线 bars 最后一根提取 current_price, change_pct, BB 绝对值。

    [CP-13] BB 经 CanonicalComputationService.compute(algorithm_id="bollinger", ...) 调用，
    使用 compute_bollinger kernel（DataFrame 11 列）替代旧 bollinger 3-tuple kernel。
    bb_mid/bb_upper/bb_lower 公式不变（与 structural_factor_service 对齐）。
    """
    extra: dict[str, Any] = {
        "current_price": None,
        "change_pct": None,
        "bb_upper": None,
        "bb_mid": None,
        "bb_lower": None,
    }
    if df_1d is None or df_1d.empty or len(df_1d) < 2:
        return extra

    closes = df_1d["close"].to_numpy(dtype=float)
    current_price = float(closes[-1])
    prev_close = float(closes[-2])
    extra["current_price"] = current_price
    if prev_close > 0:
        extra["change_pct"] = round(
            (current_price - prev_close) / prev_close * 100, 4
        )

    # BB 绝对值（需要 >= 20 根 bar）
    if len(df_1d) >= _BB_WIN + 1:
        try:
            # [CP-13] 经 canonical 调用 bollinger adapter（v2: DataFrame 11 列）
            bb_canonical = await CanonicalComputationService.compute(
                algorithm_id="bollinger",
                instrument_id=instrument_id,
                as_of=trade_date.isoformat(),
                source_bar_hash=source_bar_hash,
                adj_factor_hash=adj_factor_hash,
                bars=df_1d,
                length=_BB_WIN,
                mult=_BB_K,
            )
            bb_df = bb_canonical.payload
            upper_series = bb_df["bb_upper"]
            mid_series = bb_df["bb_mid"]
            lower_series = bb_df["bb_lower"]
            extra["bb_upper"] = float(upper_series.iloc[-1]) if pd.notna(upper_series.iloc[-1]) else None
            extra["bb_mid"] = float(mid_series.iloc[-1]) if pd.notna(mid_series.iloc[-1]) else None
            extra["bb_lower"] = float(lower_series.iloc[-1]) if pd.notna(lower_series.iloc[-1]) else None
        except Exception:
            pass

    return extra


def _normalize_primary_bar_time(
    df_1d: pd.DataFrame | None,
    trade_date: date,
) -> datetime | None:
    """将 1d 最后一根 bar 的日期规范化为 trade_date 15:00+08:00。

    规范化规则：
    - 如果 df_1d 有数据，取最后一根 bar 的实际日期。
    - 将该日期转换为 Asia/Shanghai 15:00:00。
    - 如果 df_1d 为空，使用 trade_date。
    """
    if df_1d is not None and not df_1d.empty:
        last_date = df_1d.index[-1].date()
    else:
        last_date = trade_date
    return datetime(
        last_date.year, last_date.month, last_date.day,
        15, 0, 0, tzinfo=_SHANGHAI_TZ,
    )


def _normalize_secondary_bar_time(
    df_15m: pd.DataFrame | None,
) -> datetime | None:
    """取 15m 最后一根 bar 的实际 trade_time，确保 timezone-aware。

    规范化规则：
    - 如果 df_15m 有数据，取最后一根 bar 的 trade_time。
    - 如果 trade_time 是 naive，加上 Asia/Shanghai 时区。
    - 如果 df_15m 为空，返回 None。
    """
    if df_15m is None or df_15m.empty:
        return None
    last_ts = df_15m.index[-1]
    ts = pd.Timestamp(last_ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize(_SHANGHAI_TZ)
    else:
        ts = ts.astimezone(_SHANGHAI_TZ)
    return ts.to_pydatetime()


# =============================================================================
# upsert：幂等写入
# =============================================================================


async def upsert_snapshot(
    session: AsyncSession,
    snapshot: StockFeatureSnapshot,
) -> StockFeatureSnapshot:
    """按唯一键幂等 upsert snapshot。

    存在则更新 payload/source_bar_time/updated_at，不存在则 insert。
    使用 PostgreSQL INSERT ... ON CONFLICT DO UPDATE。

    [P0-4] published run 保护（无条件，无 bypass）：
    ON CONFLICT DO UPDATE 带 WHERE 子句，
    仅当现有 snapshot 的 source_run_id IS NULL 或链接的 run 非 succeeded+published 时才更新。
    已归属 succeeded+published run 的 snapshot 无条件不可覆盖。
    未来纠错发布另做 supersede 机制，当前不提供绕过。

    Args:
        session: 异步 DB 会话
        snapshot: 待写入的 StockFeatureSnapshot 对象

    Returns:
        写入后的 ORM 对象
    """
    stmt = pg_insert(StockFeatureSnapshot).values(
        instrument_id=snapshot.instrument_id,
        trade_date=snapshot.trade_date,
        primary_timeframe=snapshot.primary_timeframe,
        secondary_timeframe=snapshot.secondary_timeframe,
        adj=snapshot.adj,
        schema_version=snapshot.schema_version,
        source_run_id=snapshot.source_run_id,
        source_primary_bar_time=snapshot.source_primary_bar_time,
        source_secondary_bar_time=snapshot.source_secondary_bar_time,
        structural_payload=snapshot.structural_payload,
        temporal_payload=snapshot.temporal_payload,
        summary_payload=snapshot.summary_payload,
        degraded_reasons=snapshot.degraded_reasons,
    )

    update_cols = {
        # [P0-4] 冲突时更新 source_run_id：新 run 应成为快照归属。
        # 已归属 published run 的 snapshot 由 WHERE 子句无条件保护，不会被覆盖。
        # 失败 run 在事务中回滚，不会污染旧归属。
        "source_run_id": stmt.excluded.source_run_id,
        "source_primary_bar_time": stmt.excluded.source_primary_bar_time,
        "source_secondary_bar_time": stmt.excluded.source_secondary_bar_time,
        "structural_payload": stmt.excluded.structural_payload,
        "temporal_payload": stmt.excluded.temporal_payload,
        "summary_payload": stmt.excluded.summary_payload,
        "degraded_reasons": stmt.excluded.degraded_reasons,
        "updated_at": func.now(),
    }

    # [P0-4] 无条件保护：不覆盖已归属 published run 的 snapshot
    stmt = stmt.on_conflict_do_update(
        constraint="uq_feature_snapshot_instrument_date_tf_adj_schema",
        set_=update_cols,
        where=text(
            "stock_feature_snapshots.source_run_id IS NULL "
            "OR NOT EXISTS ("
            "  SELECT 1 FROM stock_feature_snapshot_runs r "
            "  WHERE r.id = stock_feature_snapshots.source_run_id "
            "  AND r.status = 'succeeded' "
            "  AND r.published_at IS NOT NULL"
            ")"
        ),
    )
    await session.execute(stmt)
    await session.flush()

    # 返回传入的 snapshot（upsert 已在 DB 层完成，不重新查询避免 identity map 返回旧值）
    return snapshot


# =============================================================================
# 批量计算
# =============================================================================


def _peak_rss_mb() -> float:
    """当前进程峰值 RSS（MB）。跨平台：macOS ru_maxrss 单位 KB，Linux 单位 Bytes。"""
    try:
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if platform.system() == "Darwin":
            return round(raw / 1024, 2)
        return round(raw / 1024 / 1024, 2)
    except Exception:
        return 0.0


async def compute_for_trade_date(
    session: AsyncSession,
    trade_date: date,
    instrument_ids: Sequence[uuid.UUID],
    batch_size: int = 20,
    failure_threshold: float = 0.3,
    progress_callback: Callable[..., Awaitable[None]] | None = None,
    source_run_id: uuid.UUID | None = None,
    enforce_compute_once: bool = True,
) -> dict[str, Any]:
    """为给定 instrument 列表批量计算并 upsert 快照（不内部 commit）。

    [Blocker2] 事务边界变更：
    - 本函数只负责 upsert（flush）+ 返回统计，不调用 session.commit()。
    - 失败比例超过 failure_threshold 时抛 RuntimeError，由 caller 决定 rollback。
    - caller（after_close / backfill）负责：成功时 commit，超阈值时 rollback。
    - 这样保证失败日期不会留下部分已 commit 行（half-baked）。

    [P0-4] published run 保护（无条件）：
    upsert_snapshot 内部 WHERE 子句无条件保护已归属 published run 的 snapshot。
    无 bypass 参数，未来纠错发布另做 supersede 机制。

    - 按 batch_size 分批遍历
    - 单股失败记录，不阻塞其他股票
    - 失败比例超过 failure_threshold 时整体抛异常
    - 每处理完一批调用 progress_callback（如提供），用于长任务心跳保活

    Args:
        session: 异步 DB 会话
        trade_date: 交易日
        instrument_ids: 标的 ID 列表
        batch_size: 每批 instrument 数（默认 20）
        failure_threshold: 失败比例阈值（默认 0.3）
        progress_callback: 可选的进度回调，接收关键字参数 processed/total/snapshot_count/failed_count
        source_run_id: 关联的 snapshot run ID

    Returns:
        统计信息 dict：snapshot_count, failed_count, schema_version, trade_date

    Raises:
        RuntimeError: 失败比例超过 failure_threshold（caller 应 rollback）
    """
    total = len(instrument_ids)
    snapshot_count = 0
    failed_count = 0
    batch_count = 0
    # [Commit B §7.2] compute-once/property 计数（供远程 DSA/SMC/momentum 每股各一次证明）
    attempted_count = 0
    peak_batch_size = 0
    mdas_batch_read_count = 0
    # [Performance Contract 2026-08-04] 阶段耗时累计（供性能基准与资源门禁使用）
    read_duration = 0.0
    compute_duration = 0.0
    persist_duration = 0.0
    fallback_count = 0
    # [P0-2 2026-08-04] 行情读取操作数改为 MDAS 真实批读诊断（不再静态推算）。
    # 名称用 market_data_read_operation_count（MDAS 层读操作数），
    # 不冒充精确 SQL 查询计数；真实 SQL 数由 get_bars_batch 的 repository_query_count 提供。
    market_data_read_operation_count = 0
    repository_query_count = 0
    _t0 = time.perf_counter()
    # [CHANGE-20260717-002 SSOT] run 级行情诊断（取首个成功 instrument 的 primary_diag 为权威）
    run_diag: dict[str, Any] = {}
    # 注：本函数走 compute_feature_snapshot_for_date（非 review-core 路径），
    # first_pyramid 由旧链路生成；QM-62 run 级来源合同在 review-core 入口实施。
    # [P0-symbol合同 2026-07-30] 一次查询 instrument_id → symbol 映射，避免 N 次 DB 查询
    symbol_map: dict[uuid.UUID, str | None] = {}
    if total > 0:
        sym_rows = await session.execute(
            text("SELECT id, symbol FROM instruments WHERE id = ANY(:ids)"),
            {"ids": list(instrument_ids)},
        )
        symbol_map = {row[0]: row[1] for row in sym_rows}

    from app.services.market_data_aggregation_service import MarketDataAggregationService
    mdas = MarketDataAggregationService()
    # [Corrective-2 2026-08-05] run-scoped compute-once 计数：本 run 持有独立
    # ComputeOnceDiagnostics 实例（并发 run 天然隔离），沿计算链传递到
    # _compute_all_factors_for_bars 的 canonical(1d) 帧埋点累计，结束时硬门禁校验。
    # 不再使用模块级全局计数（已删除 reset/get_compute_call_counts）。
    compute_diagnostics = ComputeOnceDiagnostics()
    # eligible_compute_count：本 run 实际纳入 compute-once 的标的数（成功消费 primary 1d 帧）。
    eligible_compute_count = 0
    for i in range(0, total, batch_size):
        batch = instrument_ids[i : i + batch_size]
        batch_count += 1
        peak_batch_size = max(peak_batch_size, len(batch))
        # [P0-2 2026-08-04] 批读诊断：由 get_bars_batch 真实返回 read_mode/回退数/读操作数。
        # 一级（1d）为批读模式；二级（15m）为日内周期必退回逐股 get_bars。
        # 不再用 _BATCH_READ_TIMEFRAMES 静态推断 fallback。
        _t_read = time.perf_counter()
        _primary_diag: dict[str, Any] = {}
        _secondary_diag: dict[str, Any] = {}
        primary_results = await mdas.get_bars_batch(session, batch, timeframe="1d", adj="qfq", include_realtime=False, completed_only=True, end_date=trade_date, adjustment_as_of=trade_date, _diag_sink=_primary_diag)
        secondary_results = await mdas.get_bars_batch(session, batch, timeframe="15m", adj="qfq", include_realtime=False, completed_only=True, end_date=trade_date, adjustment_as_of=trade_date, _diag_sink=_secondary_diag)
        read_duration += time.perf_counter() - _t_read
        mdas_batch_read_count += 2
        # 真实回退标计数（二级日内周期回退逐股时按标的计数）
        fallback_count += _secondary_diag.get("fallback_symbol_count", 0)
        # 真实读操作数/近似 SQL 数（来自 MDAS 批读诊断，非静态推算）
        market_data_read_operation_count += (
            _primary_diag.get("read_operation_count", 0)
            + _secondary_diag.get("read_operation_count", 0)
        )
        repository_query_count += (
            _primary_diag.get("repository_query_count", 0)
            + _secondary_diag.get("repository_query_count", 0)
        )
        batch_snapshots: list[StockFeatureSnapshot] = []
        _t_compute = time.perf_counter()
        for instrument_id in batch:
            try:
                attempted_count += 1
                # [Commit B §7.2] 每股每 core run 只消费一次 canonical frame（df_1d），
                # 同一个 frame 传给 DSA/SMC/Bollinger/SQZMOM/VolumeContext（compute-once）。
                # [Corrective-2 2026-08-05] 实际计数由 run-scoped ComputeOnceDiagnostics
                # 沿计算链传递到 _compute_all_factors_for_bars 的 canonical(1d) 帧埋点累计。
                primary_result = primary_results.get(instrument_id)
                secondary_result = secondary_results.get(instrument_id)
                if isinstance(primary_result, Exception):
                    raise primary_result
                if isinstance(secondary_result, Exception):
                    raise secondary_result
                # 成功消费 primary 1d 帧的标的才计入 eligible_compute_count（门禁基准）。
                if primary_result is not None and primary_result.bars is not None and len(primary_result.bars) > 0:
                    eligible_compute_count += 1
                snapshot = await compute_feature_snapshot_for_date(
                    session, instrument_id, trade_date,
                    primary_bars=primary_result.bars if primary_result is not None else None,
                    secondary_bars=secondary_result.bars if secondary_result is not None else None,
                    source_run_id=source_run_id,
                    instrument_symbol=symbol_map.get(instrument_id),
                    _diag_sink=run_diag,
                    compute_diagnostics=compute_diagnostics,
                )
                batch_snapshots.append(snapshot)
                snapshot_count += 1
            except Exception as exc:
                failed_count += 1
                logger.error(
                    "snapshot 计算失败 instrument_id=%s trade_date=%s: %s",
                    instrument_id, trade_date, exc, exc_info=True,
                )
        compute_duration += time.perf_counter() - _t_compute

        # 批内计算完成后统一执行 upsert；保持调用方整日期事务与 published 保护。
        _t_persist = time.perf_counter()
        for snapshot in batch_snapshots:
            await upsert_snapshot(session, snapshot)
        persist_duration += time.perf_counter() - _t_persist

        # [Heartbeat] 每批完成后回调进度，供长任务更新心跳/lease 与 metadata
        if progress_callback is not None:
            try:
                await progress_callback(
                    processed=min(i + len(batch), total),
                    total=total,
                    snapshot_count=snapshot_count,
                    failed_count=failed_count,
                )
            except Exception as exc:
                logger.warning(
                    "progress_callback 失败 trade_date=%s: %s",
                    trade_date, exc,
                )

    # 检查失败阈值（不 commit，由 caller 决定 commit/rollback）
    if total > 0:
        failure_rate = failed_count / total
        if failure_rate > failure_threshold:
            raise RuntimeError(
                f"feature_snapshot 失败比例 {failure_rate:.1%} 超过阈值 {failure_threshold:.0%} "
                f"(failed={failed_count}, total={total})"
            )

    logger.info(
        "feature_snapshot 批量完成 trade_date=%s snapshot_count=%d failed_count=%d batches=%d mdas_batch_reads=%d",
        trade_date, snapshot_count, failed_count, batch_count, mdas_batch_read_count,
    )

    _total = time.perf_counter() - _t0
    # [Corrective-2 2026-08-05] run-scoped compute-once 计数快照 + 硬门禁。
    # 四类计数（canonical/dsa/smc/momentum）必须 == eligible_compute_count，
    # 否则抛 ComputeOnceGateError，caller 不得发布 stock_core。
    #
    # [CHANGE-20260805-CP4A-CP3] compute-once 门禁**不可绕过**：生产默认 enforce_compute_once=True
    # 必然强制；仅单元测试 mock compute 时显式传 enforce_compute_once=False（测试契约与生产分开）。
    # 禁止通过「计数为 0 自动跳过硬门禁」，否则 instrumentation 回归会静默放行重复计算。
    _compute_counts = compute_diagnostics.to_dict()
    if total > 0 and enforce_compute_once:
        enforce_compute_once_gate(compute_diagnostics, eligible_compute_count)
    return {
        "snapshot_count": snapshot_count,
        "failed_count": failed_count,
        "batch_count": batch_count,
        "mdas_batch_read_count": mdas_batch_read_count,
        # [Corrective-2 §6] compute-once 硬门禁证明：四类计数 == eligible_compute_count。
        # canonical_frame_build / dsa / smc / momentum 均在同一 canonical(1d) 帧上各一次。
        "attempted_count": attempted_count,
        "eligible_compute_count": eligible_compute_count,
        "frame_build_count": _compute_counts["canonical_frame_build"],
        "dsa_call_count": _compute_counts["dsa"],
        "smc_call_count": _compute_counts["smc"],
        "bollinger_call_count": _compute_counts["bollinger"],
        "sqzmom_call_count": _compute_counts["sqzmom"],
        "volume_context_call_count": _compute_counts["volume_context"],
        "peak_batch_size": peak_batch_size,
        # [Performance Contract 2026-08-04] 阶段耗时/吞吐/回退指标（供 finish_snapshot_run 落库与基准对比）
        "read_duration": round(read_duration, 4),
        "compute_duration": round(compute_duration, 4),
        "persist_duration": round(persist_duration, 4),
        "total_duration": round(_total, 4),
        "symbols_per_second": round(snapshot_count / _total, 2) if _total > 0 else 0.0,
        # [P0-2 2026-08-04] 真实回退/行情读取操作计数：来源为 MDAS get_bars_batch 的
        # _diag_sink 诊断（read_mode/fallback_symbol_count/read_operation_count/
        # repository_query_count），非静态公式推算。
        "fallback_count": fallback_count,
        "market_data_read_operation_count": market_data_read_operation_count,
        "repository_query_count": repository_query_count,
        # 本函数只批量 upsert+flush（O(batch)），不调用 session.commit；由 caller 统一提交
        "internal_commit_count": 0,
        # [P0-2 2026-08-04] 事务归属如实声明：本函数不拥有事务，由 caller 管理。
        # 不再硬编码推测值 transaction_count=1。
        "transaction_owner": "caller",
        "peak_rss_mb": _peak_rss_mb(),
        "batch_size": batch_size,
        "configured_concurrency": 1,  # 当前实现为顺序分批处理，无并发
        "schema_version": _SCHEMA_VERSION,
        "trade_date": trade_date.isoformat(),
        # [CHANGE-20260717-002 SSOT] run 级行情诊断（供 finish_snapshot_run 落库）
        "source_bar_hash": run_diag.get("source_bar_hash"),
        "adj_factor_hash": run_diag.get("adj_factor_hash"),
        "market_data_contract_version": run_diag.get("market_data_contract_version"),
        "completed_through": run_diag.get("completed_through"),
        "adjustment_as_of": run_diag.get("adjustment_as_of"),
    }


# =============================================================================
# Run 生命周期管理：publish gate
# =============================================================================


async def create_snapshot_run(
    session: AsyncSession,
    trade_date: date,
    run_type: str,
    *,
    schema_version: int = _SCHEMA_VERSION,
    primary_timeframe: str = "1d",
    secondary_timeframe: str = "15m",
    adj: str = "qfq",
    expected_count: int | None = None,
    metadata: dict[str, Any] | None = None,
    scope: str | None = None,
) -> StockFeatureSnapshotRun:
    """创建或复用 running 状态的 snapshot run 记录。

    幂等设计：
    - 如果已存在 status='running' 的同 key run（部分唯一索引约束），返回该记录。
    - 否则创建新 running run。
    - 失败/已完成的 run 不影响新 run 创建（部分唯一索引仅约束 status='running'）。

    [Blocker Fix] scope 参数：
    - 'full'：全市场 backfill / after_close，watchlist 可读对应 snapshot
    - 'sample'：--symbols / --limit-instruments 小样本，watchlist 不可读
    - 注入到 metadata_['scope']，watchlist gate 据此过滤
    - finish_snapshot_run 的 metadata 完全替换 create 时的 metadata，调用方需在 finish 时再次传入 scope

    [P0-4] published run 保护（无条件，无 bypass）：
    - scope='full' 时，如已存在 canonical succeeded+published+full run，
      抛出 PublishedSnapshotRunExistsError，禁止重跑覆盖已发布数据。
    - scope='sample' 或 None 时不检查（小样本验证不影响 watchlist 可读的 full run）。
    - 未来纠错发布另做 supersede 机制，当前不提供绕过。

    Args:
        session: 异步 DB 会话
        trade_date: 业务交易日
        run_type: 触发方式（after_close/backfill/manual）
        schema_version: 快照 schema 版本（默认 _SCHEMA_VERSION）
        primary_timeframe: 主周期（默认 1d）
        secondary_timeframe: 次周期（默认 15m）
        adj: 复权方式（默认 qfx）
        expected_count: 预期快照数（active A 股总数）
        metadata: 额外元数据（如 failure_threshold、source）
        scope: run 范围（'full' 或 'sample'），注入到 metadata_['scope']

    Returns:
        StockFeatureSnapshotRun ORM 对象（status='running'）

    Raises:
        PublishedSnapshotRunExistsError: scope='full' 且已存在
            canonical succeeded+published+full run
    """
    # 查找已存在的 running run（幂等复用）
    stmt = select(StockFeatureSnapshotRun).where(
        StockFeatureSnapshotRun.trade_date == trade_date,
        StockFeatureSnapshotRun.schema_version == schema_version,
        StockFeatureSnapshotRun.primary_timeframe == primary_timeframe,
        StockFeatureSnapshotRun.secondary_timeframe == secondary_timeframe,
        StockFeatureSnapshotRun.adj == adj,
        StockFeatureSnapshotRun.run_type == run_type,
        StockFeatureSnapshotRun.status == STATUS_RUNNING,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        logger.info(
            "复用已存在 running snapshot run: trade_date=%s run_type=%s run_id=%s",
            trade_date, run_type, existing.id,
        )
        return existing

    # [P0-4] 无条件保护：scope='full' 时，
    # 如已存在 canonical succeeded+published+full run，禁止创建新 run
    if scope == "full":
        existing_published = await get_published_full_run(
            session, trade_date,
            schema_version=schema_version,
            primary_timeframe=primary_timeframe,
            secondary_timeframe=secondary_timeframe,
            adj=adj,
        )
        if existing_published is not None:
            logger.warning(
                "[P0-4] 拒绝创建新 full run：已存在 published run "
                "trade_date=%s run_id=%s published_at=%s",
                trade_date, existing_published.id, existing_published.published_at,
            )
            raise PublishedSnapshotRunExistsError(existing_published)

    # [Blocker Fix] 注入 scope 到 metadata_（如未在 metadata 中显式设置）
    final_metadata: dict[str, Any] = dict(metadata) if metadata else {}
    if scope is not None and "scope" not in final_metadata:
        final_metadata["scope"] = scope

    # 创建新 running run
    run = StockFeatureSnapshotRun(
        trade_date=trade_date,
        schema_version=schema_version,
        primary_timeframe=primary_timeframe,
        secondary_timeframe=secondary_timeframe,
        adj=adj,
        run_type=run_type,
        status=STATUS_RUNNING,
        expected_count=expected_count,
        started_at=datetime.now(UTC),
        metadata_=final_metadata if final_metadata else None,
    )
    session.add(run)
    await session.flush()
    logger.info(
        "创建 snapshot run: trade_date=%s run_type=%s run_id=%s expected_count=%s scope=%s",
        trade_date, run_type, run.id, expected_count, scope,
    )
    return run


async def get_published_full_run(
    session: AsyncSession,
    trade_date: date,
    *,
    schema_version: int = _SCHEMA_VERSION,
    primary_timeframe: str = "1d",
    secondary_timeframe: str = "15m",
    adj: str = "qfq",
) -> StockFeatureSnapshotRun | None:
    """[P0-4] 查询已存在的 canonical succeeded+published+full run。

    用于 create_snapshot_run 的保护检查：禁止普通重跑覆盖已发布的 full scope run。

    与 has_succeeded_snapshot_run 的区别：
    - has_succeeded_snapshot_run 只按 trade_date+schema_version 过滤（用于 watchlist gate）
    - 本函数按完整 key（trade_date+schema_version+primary_timeframe+secondary_timeframe+adj）过滤
      （用于 create_snapshot_run 的精确保护）

    Args:
        session: 异步 DB 会话
        trade_date: 业务交易日
        schema_version: 快照 schema 版本
        primary_timeframe: 主周期
        secondary_timeframe: 次周期
        adj: 复权方式

    Returns:
        已存在的 published full run，或 None
    """
    stmt = (
        select(StockFeatureSnapshotRun)
        .where(
            StockFeatureSnapshotRun.trade_date == trade_date,
            StockFeatureSnapshotRun.schema_version == schema_version,
            StockFeatureSnapshotRun.primary_timeframe == primary_timeframe,
            StockFeatureSnapshotRun.secondary_timeframe == secondary_timeframe,
            StockFeatureSnapshotRun.adj == adj,
            StockFeatureSnapshotRun.status == STATUS_SUCCEEDED,
            StockFeatureSnapshotRun.published_at.is_not(None),
            StockFeatureSnapshotRun.metadata_["scope"].astext == "full",
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def finish_snapshot_run(
    session: AsyncSession,
    run: StockFeatureSnapshotRun,
    *,
    status: str,
    snapshot_count: int | None = None,
    failed_count: int | None = None,
    skipped_count: int | None = None,
    expected_count: int | None = None,
    failure_rate: float | None = None,
    metadata: dict[str, Any] | None = None,
    source_bar_hash: str | None = None,
    adj_factor_hash: str | None = None,
    market_data_contract_version: str | None = None,
    completed_through: datetime | None = None,
    adjustment_as_of: date | None = None,
) -> StockFeatureSnapshotRun:
    """更新 run 状态为 succeeded/failed，写入统计与时间戳。

    - succeeded: 写 published_at（watchlist 据此判断是否可读 snapshot）
    - failed: 不写 published_at（watchlist 不读取该 run 的 snapshot）
    - 两者都写 finished_at

    metadata 覆盖语义：finish 时传入的 metadata 完全替换 create 时的 metadata。

    Args:
        session: 异步 DB 会话
        run: 待更新的 StockFeatureSnapshotRun 对象
        status: 目标状态（succeeded/failed）
        snapshot_count: 实际写入快照数
        failed_count: 失败股票数
        skipped_count: 跳过股票数
        expected_count: 预期快照数（覆盖 create 时的值）
        failure_rate: 失败率 0.0-1.0
        metadata: 额外元数据（覆盖 create 时的 metadata）

    Returns:
        更新后的 StockFeatureSnapshotRun ORM 对象
    """
    if status not in (STATUS_SUCCEEDED, STATUS_FAILED):
        raise ValueError(
            f"finish_snapshot_run 仅接受 status='{STATUS_SUCCEEDED}' 或 '{STATUS_FAILED}'，"
            f"实际='{status}'"
        )

    now = datetime.now(UTC)
    run.status = status
    run.finished_at = now
    if snapshot_count is not None:
        run.snapshot_count = snapshot_count
    if failed_count is not None:
        run.failed_count = failed_count
    if skipped_count is not None:
        run.skipped_count = skipped_count
    if expected_count is not None:
        run.expected_count = expected_count
    if failure_rate is not None:
        run.failure_rate = failure_rate
    if metadata is not None:
        run.metadata_ = metadata
    # [CHANGE-20260717-002 SSOT] 写入行情诊断字段（供审计与跨调用方对账）
    if source_bar_hash is not None:
        run.source_bar_hash = source_bar_hash
    if adj_factor_hash is not None:
        run.adj_factor_hash = adj_factor_hash
    if market_data_contract_version is not None:
        run.market_data_contract_version = market_data_contract_version
    if completed_through is not None:
        run.completed_through = completed_through
    if adjustment_as_of is not None:
        run.adjustment_as_of = adjustment_as_of
    # [RunGate] - succeeded 时写 published_at，failed 时保持 None
    if status == STATUS_SUCCEEDED:
        run.published_at = now

    await session.flush()
    logger.info(
        "完成 snapshot run: run_id=%s status=%s snapshot_count=%s failed_count=%s",
        run.id, status, snapshot_count, failed_count,
    )
    return run


async def has_succeeded_snapshot_run(
    session: AsyncSession,
    trade_date: date,
    *,
    schema_version: int = _SCHEMA_VERSION,
) -> bool:
    """[RunGate] - 检查指定 trade_date 是否存在 succeeded + published + full scope 的 snapshot run。

    publish gate 规则（严格化）：
    - 必须 status='succeeded'
    - 必须 published_at IS NOT NULL
    - 必须 metadata_['scope']='full'（after_close / 全市场 backfill 才允许 watchlist 读取）
    - running/failed run 对应的 snapshot 即使存在也不得被读取
    - 无 run 记录的 snapshot（如 smoke test 残留）也不得被读取
    - sample scope run（--symbols / --limit-instruments 小样本验证产生）不得被读取

    Args:
        session: 异步 DB 会话
        trade_date: 预期快照交易日
        schema_version: 快照 schema 版本（默认与 _SCHEMA_VERSION 一致）

    Returns:
        True 表示存在可读的 succeeded run，watchlist 可读取 snapshot；False 表示不可读取
    """
    stmt = (
        select(StockFeatureSnapshotRun.id)
        .where(
            StockFeatureSnapshotRun.trade_date == trade_date,
            StockFeatureSnapshotRun.schema_version == schema_version,
            StockFeatureSnapshotRun.status == STATUS_SUCCEEDED,
            StockFeatureSnapshotRun.published_at.is_not(None),
            StockFeatureSnapshotRun.metadata_["scope"].astext == "full",
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


# =============================================================================
# 辅助：获取需要快照的 instrument 列表
# =============================================================================


async def get_active_a_share_instruments(
    session: AsyncSession,
) -> list[uuid.UUID]:
    """获取所有活跃 A 股股票的 instrument_id 列表。

    与 BarsCoverageService 口径一致：
    - status='active'
    - symbol 匹配 A 股股票代码（6 位数字，排除指数/基金/ETF）
    """
    from app.models.instrument import Instrument

    stmt = select(Instrument.id).where(
        Instrument.status == "active",
        Instrument.symbol.op("~")(r"^\d{6}$"),
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# =============================================================================
# 模块自测
# =============================================================================


if __name__ == "__main__":
    # 纯函数自测（不连 DB）
    print("feature_snapshot_service 自测...")

    # _truncate_bars_to_trade_date
    idx = pd.date_range("2026-01-01", periods=10, freq="B")
    bars = pd.DataFrame({"close": range(10)}, index=idx)
    truncated = _truncate_bars_to_trade_date(bars, date(2026, 1, 7), "1d")
    assert truncated is not None
    assert truncated.index[-1].date() <= date(2026, 1, 7)
    print(f"_truncate_bars: {len(truncated)} bars (expect <= 5)")

    # build_summary_payload
    summary = build_summary_payload({}, {}, date(2026, 1, 10))
    assert summary["_source"] == "feature_snapshot"
    assert summary["poc_price"] is None
    print(f"build_summary_payload: {len(summary)} fields")

    print("OK")
