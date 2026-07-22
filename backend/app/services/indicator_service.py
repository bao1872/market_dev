"""策略 manifest 驱动指标计算服务。

从 StrategyLoader._registry 获取所有已注册策略，实时计算图表指标。
复用 StrategyRuntime.compute_indicators() 和 bar_repository.py fetch 函数，
不重新实现算法逻辑（SSOT）。

架构（策略 manifest 驱动指标自动体现）：
- 遍历 StrategyLoader._registry 中的所有策略
- 对每个策略，查询最新 released 版本
- 调用 StrategyLoader.load(version) 获取 runtime
- 调用 runtime.compute_indicators(context) 计算指标
- 从 manifest.chart_layers 收集图层定义 + 计算结果

异常处理：
- 单个策略失败不阻塞其他策略（记录错误并跳过，错误信息返回给前端）
- 这不是吞异常，而是隔离故障策略，保证图表可用性

Inputs:
    session: AsyncSession
    instrument_id: UUID
    timeframe: 1d | 15m | 1h | 1w | 1mo
    adj: qfq | none
    bars: 返回最近 N 根 bar 的指标

Outputs:
    dict: layers/data/errors（可 JSON 序列化）

How to Run:
    python -m app.services.indicator_service    # 自测：验证模块加载和函数签名（不连 DB/网络）
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.indicator_contract import (
    INDICATOR_BARS,
    NODE_CLUSTER_MINUTE_BARS,
)
from app.constants.strategy_keys import DSA_SELECTOR, WATCHLIST_MONITOR
from app.models.instrument import Instrument
from app.models.strategy import StrategyDefinition, StrategyVersion

# [CP-13 Canonical 四链迁移] 四链禁止直接 import 算法 kernel 函数；
# 所有注册算法（macd/sqzmom/bollinger/smc/node_cluster）必须经 CanonicalComputationService.compute() 调用。
# 仅保留 DTO builders（非算法 kernel，是视图层工具）和类型引用。
from app.services.canonical_adapters import (
    NodeClusterProfileResult,
    build_node_regions,
    build_price_state,
    compute_node_regions_hash,
    derive_state_for_price,
)
from app.services.canonical_computation_service import CanonicalComputationService
from app.services.chart_bars_service import (
    compute_source_bar_hash,
    compute_source_bar_times,
)
from app.services.indicator_display_frame import (
    DisplayWindowSpec,
    build_calculation_diagnostics,
    build_display_frame,
)
from app.services.market_data_aggregation_service import (
    BarAggregationResult,
    MarketDataAggregationService,
)
from app.services.node_cluster_input_provider import (
    NodeClusterInput,
    NodeClusterInputProvider,
)
from app.services.strategy_batch_service import StrategyBatchService
from app.strategy.runtime import MarketDataContext, StrategyLoader

logger = logging.getLogger("services.indicator_service")

# [CHANGE-20260717-002 SSOT] 查询回看范围由 MarketDataAggregationService 内部管理，
# 本层仅保留 SMC warmup/最少 bar 数常量（用于 MDAS limit/warmup_bars 参数）。
# [CHANGE-20260717-001 Pine parity] SMC warmup/历史分离
# Pine 使用全历史计算 SMC；项目 15m 展示 4000 根时 SMC 必须额外查询 warmup，
# 计算 5000 根后由 adapter 裁成 4000 展示（pivot/BOS/CHoCH 在窗口左缘不丢失）
_SMC_WARMUP_BARS = 1000  # 15m 专用 SMC warmup（计算=展示+warmup）
_SMC_MONTHLY_MIN_BARS = 200  # 1mo 最少 bar 数（ATR200 需 200 根才能初始化）

# [CHANGE-20260718-001 SMC input contract] SMC 输入契约与 deterministic 模式
#
# SMC 输入范围（禁止称"全历史"，各周期实际范围如下）：
#   - 1d:  MDAS 无 limit → DB 全量日线（约 1000-5000 根，受 DB 覆盖范围限制，非"全历史"）
#   - 15m: MDAS limit=bars + _SMC_WARMUP_BARS(1000) → bars+1000 根（非全历史）
#   - 1h:  MDAS 无 limit → DB 全量 1h（约 180 交易日，非"全历史"）
#   - 1mo: MDAS limit=_SMC_MONTHLY_MIN_BARS(200) → 至少 200 根（ATR200 可初始化）
#   - 1w:  复用 macd_bars（MDAS 无 limit → DB 全量周线）
#
# deterministic 模式（Pine parity 对齐）：
#   - include_realtime=False, completed_only=True → 仅使用已完成 bar
#   - 不包含当前未完成 bar（partial bar），与 TV 历史导出一致
#   - 输出 smc_mode="deterministic"
#
# realtime 模式（盘中图表展示）：
#   - include_realtime=True → 包含当前 partial bar
#   - 输出 smc_mode="realtime"
#   - 不得与 TV 历史导出混比（TV 历史导出仅含已完成 bar）
#
# 当前生产图表 API 使用 deterministic 模式（include_realtime=False），
# 确保 SMC 计算结果与 TV 历史导出可比。
# 盘中实时刷新通过前端 quote overlay 呈现，不依赖后端 partial bar SMC 重算。
_SMC_MODE_DETERMINISTIC = "deterministic"
_SMC_MODE_REALTIME = "realtime"

# [CP-V3-A] 策略→所需 bar 类型映射。仅用于 needs_minute 判断（1m crossover）。
# 15min 已由 NodeClusterInputProvider 无条件加载（250+4000），不再依赖此映射。
# volume_node_monitor 需要 minute（crossover 检测），其他策略不需要 1m。
_REQUIRED_INPUTS: dict[str, frozenset[str]] = {
    DSA_SELECTOR: frozenset({"daily"}),
    "volume_node_monitor": frozenset({"daily", "15min", "minute"}),
    "bb_monitor": frozenset({"daily"}),
    WATCHLIST_MONITOR: frozenset({"daily", "15min"}),
}


async def _get_available_strategy_keys(
    session: AsyncSession, strategy_keys: list[str]
) -> set[str]:
    """批量查询哪些策略在数据库中有 released version。

    避免为数据库中不存在或无 released version 的策略加载额外数据。
    一次查询返回所有可用策略 key。

    [CHANGE-20260716-001 required_inputs] 修复优化在生产环境无效的根因：
    静态 _registry 包含 volume_node_monitor/bb_monitor，但数据库中无定义，
    导致 _determine_required_bars() 错误地纳入 15min/minute。
    新逻辑基于实际可用策略（有 released version）计算 required_bars。

    Args:
        session: 异步 DB 会话
        strategy_keys: 待检查的策略 key 列表

    Returns:
        有 released version 的策略 key 集合
    """
    if not strategy_keys:
        return set()
    stmt = (
        select(StrategyDefinition.strategy_key)
        .join(
            StrategyVersion,
            StrategyVersion.strategy_definition_id == StrategyDefinition.id,
        )
        .where(StrategyDefinition.strategy_key.in_(strategy_keys))
        .where(StrategyVersion.status == "released")
        .distinct()
    )
    result = await session.execute(stmt)
    return {row[0] for row in result.all()}


def _determine_required_bars(available_keys: set[str]) -> frozenset[str]:
    """根据实际可用的策略确定需要加载的 bar 类型集合。

    [CHANGE-20260716-001 required_inputs] 不再基于静态 _registry，
    而是基于数据库中实际有 released version 的策略。
    当前 timeframe 自身的 macd_bars 完全独立于此函数（由 timeframe 直接决定）。

    Args:
        available_keys: 数据库中有 released version 的策略 key 集合

    Returns:
        所需 bar 类型的不可变集合（如 frozenset({"daily", "15min", "minute"})）
    """
    needed: set[str] = {"daily"}  # daily 总是需要（MACD/DSA 等基础指标）
    for strategy_id in available_keys:
        needed |= _REQUIRED_INPUTS.get(strategy_id, frozenset({"daily"}))
    return frozenset(needed)

# [DSA/MACD 计算窗口] - 从 indicator_contract 基线读取（advice.md 第一节）
# INDICATOR_BARS 已从 app.constants.indicator_contract 导入（第44行）
# warmup_bars: 算法预热期（如 EMA/MACD 前 N 根不稳定）
INDICATOR_WARMUP_BARS: dict[str, int] = {
    "15m": 60,
    "1h": 60,
    "1d": 60,
    "1w": 26,
    "1mo": 12,
}


# ===== 工具函数 =====


def _to_json_safe(val: Any) -> Any:
    """递归将值转为 JSON 可序列化的 Python 原生类型。

    处理 numpy 标量/数组、pandas Timestamp、dict、list 等嵌套结构。
    NaN/Inf 转为 None（JSON 不支持）。

    Args:
        val: 任意值（可能是 numpy/pandas 类型或嵌套结构）

    Returns:
        JSON 可序列化的 Python 原生类型
    """
    if val is None:
        return None
    # numpy 标量
    if isinstance(val, np.integer):
        return int(val)
    if isinstance(val, np.floating):
        f = float(val)
        return f if np.isfinite(f) else None
    if isinstance(val, np.bool_):
        return bool(val)
    if isinstance(val, np.ndarray):
        return [_to_json_safe(v) for v in val.tolist()]
    if isinstance(val, pd.Timestamp):
        return val.isoformat()
    # Python 标量
    if isinstance(val, float):
        return val if np.isfinite(val) else None
    # 嵌套结构
    if isinstance(val, dict):
        return {str(k): _to_json_safe(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_to_json_safe(v) for v in val]
    return val


# 快照类字段（VP 价格档位/元信息/peak 节点）：非 bar 对齐时间序列，禁止按 bars 截断
# 否则 profile_rows(100 行) 在 bars<100 时会被错误截断，破坏 SSOT 完整透传
_SNAPSHOT_KEYS: frozenset[str] = frozenset({"profile_rows", "profile_meta", "peak_rows"})


def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    """计算指数移动平均（EMA）。

    使用 pandas ewm 计算，忽略 NaN，与 ta.ema 一致。

    Args:
        arr: 输入价格数组
        span: EMA 周期

    Returns:
        EMA 数组
    """
    return pd.Series(arr).ewm(span=span, adjust=False).mean().to_numpy()


def compute_macd(
    closes: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, list[float | None]]:
    """计算 MACD 指标（A 股 2× 版本）。

    公式：
    - DIF = EMA(close, fast) - EMA(close, slow)
    - DEA = EMA(DIF, signal)
    - MACD(Hist) = 2 * (DIF - DEA)

    Args:
        closes: 收盘价数组
        fast: 快线周期（默认 12）
        slow: 慢线周期（默认 26）
        signal: 信号线周期（默认 9）

    Returns:
        dict: macd_dif / macd_dea / macd_hist 数组
    """
    dif = _ema(closes, fast) - _ema(closes, slow)
    dea = _ema(dif, signal)
    hist = 2.0 * (dif - dea)

    return {
        "macd_dif": [None if pd.isna(v) or not np.isfinite(v) else float(v) for v in dif],
        "macd_dea": [None if pd.isna(v) or not np.isfinite(v) else float(v) for v in dea],
        "macd_hist": [None if pd.isna(v) or not np.isfinite(v) else float(v) for v in hist],
    }


def _truncate_lists(
    indicators: dict[str, Any],
    bars: int,
    preserve_keys: frozenset[str] | None = None,
) -> dict[str, Any]:
    """截取指标数据到最近 N 根 bar。

    对值为 list 的字段，截取最后 bars 个元素。
    非列表字段（如标量）保持不变。

    快照类字段（profile_rows/profile_meta/peak_rows）为 VP 价格档位快照，
    非 bar 对齐时间序列，不参与截断（保证 SSOT 完整透传）。

    Args:
        indicators: 策略返回的指标字典
        bars: 保留最近 N 根 bar
        preserve_keys: 额外不参与截断的字段集合（如日线 BB 完整序列）

    Returns:
        截取后的指标字典
    """
    if bars <= 0:
        return indicators
    preserve = preserve_keys or frozenset()
    result: dict[str, Any] = {}
    for key, val in indicators.items():
        if key in _SNAPSHOT_KEYS or key in preserve:
            result[key] = val
        elif isinstance(val, list) and len(val) > bars:
            result[key] = val[-bars:]
        else:
            result[key] = val
    return result


# BB 字段集合（来自 watchlist_monitor / bollinger_monitor）
_BB_FIELDS: frozenset[str] = frozenset({"bb_upper", "bb_mid", "bb_lower", "bb_width", "bb_pos"})


def _map_daily_to_intraday(
    daily_values: list[Any],
    daily_times: list[str],
    intraday_times: list[str],
) -> list[Any]:
    """将日线值映射到日内时间序列（阶梯线）。

    对每个 intraday bar，取 daily_times 中 <= 该 bar 时间的最后一个日线值。
    这样 15m/1h 上的 BB 呈现为日内阶梯线，符合“上一根已完成日线”的参考逻辑。

    Args:
        daily_values: 日线指标值列表
        daily_times: 日线时间字符串列表
        intraday_times: 日内时间字符串列表

    Returns:
        与 intraday_times 等长的映射后列表
    """
    if not daily_values or not daily_times or not intraday_times:
        return [None] * len(intraday_times)

    daily_dates = pd.to_datetime(daily_times)
    intraday_dates = pd.to_datetime(intraday_times)
    pos = daily_dates.searchsorted(intraday_dates, side="right") - 1
    pos = np.clip(pos, 0, len(daily_values) - 1)
    return [daily_values[i] for i in pos]


async def _adapt_watchlist_bb(
    indicators: dict[str, Any],
    timeframe: str,
    macd_bars: pd.DataFrame,
    macd_time_list: list[str],
    daily_time_list: list[str],
    *,
    instrument_id: uuid.UUID | None = None,
    adjustment_as_of: str | None = None,
    source_bar_hash: str | None = None,
    adj_factor_hash: str | None = None,
) -> dict[str, Any]:
    """调整 watchlist_monitor 的 BB 输出以匹配当前 timeframe。

    - 日线：保留完整日线 BB 序列（不截断），time 同步完整
    - 15m/1h/1w/1mo：用 macd_bars 重新计算 BB（length=20, mult=2.0），不再映射日线阶梯线

    [CP-13 Canonical 四链迁移] BB kernel 通过 CanonicalComputationService.compute()
    调用（algorithm_id="bollinger"），不再直接调用 compute_bollinger。
    canonical result_hash 含 source_bar_hash/adj_factor_hash/contract_fingerprint，
    供四链一致性矩阵断言。

    修复根因（PR #31）：
        之前 15m/1h 调用 _map_daily_to_intraday 把日线 BB 映射到日内时间轴，
        导致 15m BB 全部相同（阶梯线），不是真正的 15m 周期 BB。
        新行为：15m/1h 用 canonical BB（基于 macd_bars）重新计算，
        bb_upper/bb_mid/bb_lower 反映当前 timeframe close 的波动。

    [PR #32] - 1w/1mo 也用 canonical BB 计算，不再移除 BB 字段。
        之前 1w/1mo 直接 pop BB 字段导致前端无 BB overlay。

    Args:
        indicators: watchlist_monitor 原始指标字典
        timeframe: 当前请求周期
        macd_bars: 当前 timeframe 对应的 bars（用于 BB 计算）
        macd_time_list: 当前 timeframe 对应的时间列表
        daily_time_list: 日线时间列表（15m/1h 路径不再使用，保留参数兼容）
        instrument_id: 标的 UUID（canonical result_hash 维度）
        adjustment_as_of: 复权锚点 ISO 字符串
        source_bar_hash: 当前 timeframe bars 的 source_bar_hash
        adj_factor_hash: 当前 timeframe bars 的 adj_factor_hash

    Returns:
        调整后的指标字典
    """
    result = dict(indicators)
    bb_fields_present = {f for f in _BB_FIELDS if f in result}

    if timeframe in ("15m", "1h", "1w", "1mo"):
        # [PR #31/#32] - 15m/1h/1w/1mo BB 用 macd_bars 重新计算，不再映射日线阶梯线或移除
        #   canonical BB adapter 返回 DataFrame: bb_upper/bb_mid/bb_lower/bb_pos_01/bb_width_norm
        #   映射到 watchlist_monitor 字段名：bb_pos_01→bb_pos, bb_width_norm→bb_width
        if not bb_fields_present or macd_bars.empty or not macd_time_list:
            return result
        if len(macd_bars) < 20:
            # 不足 20 根无法计算 BB length=20，返回 None 填充
            n = len(macd_time_list)
            for field in bb_fields_present:
                result[field] = [None] * n
            result["time"] = macd_time_list
            return result

        # [CP-13] 通过 CanonicalComputationService.compute() 调用 bollinger kernel
        canonical_result = await CanonicalComputationService.compute(
            algorithm_id="bollinger",
            instrument_id=instrument_id or uuid.UUID(int=0),
            as_of=adjustment_as_of,
            source_bar_hash=source_bar_hash,
            adj_factor_hash=adj_factor_hash,
            bars=macd_bars,
            length=20,
            mult=2.0,
        )
        bb_result = canonical_result.payload  # DataFrame（11 列）
        # 字段映射：canonical BB 返回名 → watchlist_monitor 字段名
        field_map = {
            "bb_upper": "bb_upper",
            "bb_mid": "bb_mid",
            "bb_lower": "bb_lower",
            "bb_pos_01": "bb_pos",
            "bb_width_norm": "bb_width",
        }
        for src_field, dst_field in field_map.items():
            if dst_field in bb_fields_present and src_field in bb_result.columns:
                # NaN → None（前端 JSON 序列化 null）
                vals = bb_result[src_field].where(
                    bb_result[src_field].notna(), None
                ).tolist()
                result[dst_field] = vals
        result["time"] = macd_time_list
        return result

    # 日线：保持完整 BB 序列，由调用方设置 preserve_keys 避免截断
    return result


async def _compute_independent_node_cluster(
    node_input: NodeClusterInput,
    *,
    symbol: str = "",
    instrument_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """独立计算 Node Cluster Profile，输出 data["node_cluster"]。

    [CP-V3-A] Node 输入由 NodeClusterInputProvider 唯一提供（四链统一入口）。
    availability 三态状态机由 Provider 预计算：
    - available: 250+4000，正常计算
    - degraded: history_exhausted=true 且真实历史不足，允许降级计算
    - unavailable: INPUT_CONTRACT_VIOLATION / INSUFFICIENT_DAILY_BARS / MISSING_15M_BARS
      → 禁止生成看似正常的 Profile（提前返回）

    [CHANGE-20260720-001] Node Cluster 固定使用 completed qfq 1d×250 + 15m×4000，
    不加载 1m，不随页面周期变化。五周期切换时 profile_hash 必须一致。

    [CP-13 Canonical 四链迁移] Node Cluster kernel 通过 CanonicalComputationService.compute()
    调用（algorithm_id="node_cluster"），不再直接调用 compute_node_cluster_profile。
    canonical result_hash 含 source_bar_hash/adj_factor_hash/contract_fingerprint，
    供四链一致性矩阵断言。

    [PROMPT.md §三.3 V2] Canonical Node DTO V2：
    - node_regions: 稳定的 Peak Node 列表（entity_id/kind/low/mid/high/多空量/is_poc）
    - price_state: 独立的当前价状态（含 upper/lower/poc/last_touched 节点的 entity_id 引用）
    - node_regions_hash: 四链一致性 hash
    前端禁止从 state/peak_rows 重建 Node 列表，必须直接读 node_regions。
    state 字段保留向后兼容（旧 volume_node_monitor schema），新代码应读 price_state。

    输出字段：
    - profile_rows: 完整 100 行 VP 价格档位快照
    - profile_meta: VP 元信息 + algorithm/schema/fingerprint/daily_hash/15m_hash/profile_hash
    - peak_rows: Peak 节点快照（含 VA 外）
    - node_regions: [V2] Canonical Node DTO 列表（四链统一读取）
    - node_regions_hash: [V2] 四链一致性 hash
    - state: 当前价格状态（旧 schema，向后兼容；新代码读 price_state）
    - price_state: [V2] 独立价格状态（含 entity_id 引用）
    - availability: "available" | "degraded" | "unavailable"
    - degraded_reason: "INSUFFICIENT_DAILY_BARS" | "MISSING_15M_BARS" |
        "INSUFFICIENT_15M_HISTORY" | "INPUT_CONTRACT_VIOLATION" |
        "PROFILE_EMPTY" | "COMPUTE_FAILED: ..." | None

    Args:
        node_input: NodeClusterInput（由 Provider 提供，含 bars + hash + availability）
        symbol: 股票代码（日志用）
        instrument_id: 标的 UUID（canonical result_hash 维度）

    Returns:
        Node Cluster 独立输出字典
    """
    # [CP-V3-A] availability 三态门禁：unavailable 禁止生成 Profile
    # INPUT_CONTRACT_VIOLATION / INSUFFICIENT_DAILY_BARS / MISSING_15M_BARS 提前返回
    if node_input.availability == "unavailable":
        return {
            "profile_rows": [],
            "profile_meta": {"row_count": 0},
            "peak_rows": [],
            "node_regions": [],
            "node_regions_hash": "empty",
            "state": {},
            "price_state": {},
            "availability": "unavailable",
            "degraded_reason": node_input.degraded_reason,
        }

    daily_bars = node_input.daily_bars
    bars_15min = node_input.bars_15m
    adjustment_as_of = (
        node_input.adjustment_as_of.isoformat()
        if node_input.adjustment_as_of is not None
        else None
    )
    daily_source_hash = node_input.daily_source_hash
    daily_adj_factor_hash = node_input.daily_adj_factor_hash
    has_15m = not bars_15min.empty

    try:
        # [CP-13] 通过 CanonicalComputationService.compute() 调用 node_cluster kernel
        # canonical result_hash 含 contract_fingerprint + source_bar_hash + adj_factor_hash，
        # 供四链一致性矩阵断言（详情/盘后/盘中/Capture 同输入 → 同 result_hash）
        canonical_result = await CanonicalComputationService.compute(
            algorithm_id="node_cluster",
            instrument_id=instrument_id or uuid.UUID(int=0),
            as_of=adjustment_as_of,
            source_bar_hash=daily_source_hash,
            adj_factor_hash=daily_adj_factor_hash,
            daily_bars=daily_bars,
            bars_15m=bars_15min if has_15m else pd.DataFrame(),
            adjustment_as_of=adjustment_as_of,
        )
        profile: NodeClusterProfileResult = canonical_result.payload
    except Exception as exc:
        logger.warning("node_cluster 独立计算失败 symbol=%s: %s", symbol, exc)
        return {
            "profile_rows": [],
            "profile_meta": {"row_count": 0},
            "peak_rows": [],
            "node_regions": [],
            "node_regions_hash": "empty",
            "state": {},
            "price_state": {},
            "availability": "unavailable",
            "degraded_reason": f"COMPUTE_FAILED: {exc}",
        }

    # [CP-V3-A] 计算后可用性：profile 空 → unavailable/PROFILE_EMPTY；
    # 否则使用 Provider 预计算的 availability（available 或 degraded/INSUFFICIENT_15M_HISTORY）
    if not profile.profile_rows:
        availability = "unavailable"
        degraded_reason = "PROFILE_EMPTY"
    else:
        availability = node_input.availability
        degraded_reason = node_input.degraded_reason

    # [PROMPT.md §三.3 V2] Canonical Node DTO V2：node_regions + hash
    # 详情/Capture/Monitor 四链统一读取；前端禁止从 state/peak_rows 重建
    node_regions = build_node_regions(profile)
    node_regions_hash = compute_node_regions_hash(node_regions)

    # 当前价格状态（取最新日线 close）
    state: dict[str, Any] = {}
    price_state: dict[str, Any] = {}
    if profile.profile_rows and not daily_bars.empty:
        try:
            latest_close = float(daily_bars["close"].iloc[-1])
            derived = derive_state_for_price(profile, latest_close)
            state = derived.to_dict()
            # [V2] price_state 与 node_regions 配对（entity_id 引用）
            price_state = build_price_state(profile, latest_close)
        except Exception:
            state = {}
            price_state = {}

    profile_meta: dict[str, Any] = {
        "row_count": len(profile.profile_rows),
        "price_step": profile.price_step,
        "poc_price": profile.poc_price,
        "vah_price": profile.vah_price,
        "val_price": profile.val_price,
        "algorithm_version": profile.algorithm_version,
        "output_schema_version": profile.output_schema_version,
        "contract_fingerprint": profile.contract_fingerprint,
        "daily_source_hash": profile.daily_source_hash,
        "bars_15m_source_hash": profile.bars_15m_source_hash,
        "profile_hash": profile.profile_hash,
        # [V2] node_regions_hash 进 meta 供四链一致性断言（与 profile_hash 同源）
        "node_regions_hash": node_regions_hash,
        "daily_bars_count": profile.daily_bars_count,
        "bars_15m_count": profile.bars_15m_count,
        "adjustment_as_of": profile.adjustment_as_of,
        "primary_period": "1d",
        "low_period": "15m",
        # [CP-13] canonical result_hash — 四链一致性矩阵权威 hash
        # 同 instrument/timeframe/as_of/adjustment_as_of 下四链必须一致
        "canonical_result_hash": canonical_result.result_hash,
        "canonical_algorithm_id": canonical_result.algorithm_id,
        "canonical_algorithm_version": canonical_result.algorithm_version,
        "canonical_output_schema_version": canonical_result.output_schema_version,
    }

    return {
        "profile_rows": profile.profile_rows,
        "profile_meta": profile_meta,
        "peak_rows": profile.peak_rows,
        # [V2] Canonical Node DTO + hash（四链统一读取）
        "node_regions": node_regions,
        "node_regions_hash": node_regions_hash,
        # 旧 schema 字段（向后兼容，新代码应读 price_state/node_regions）
        "state": state,
        # [V2] 独立价格状态（含 entity_id 引用）
        "price_state": price_state,
        "availability": availability,
        "degraded_reason": degraded_reason,
    }


async def _load_node_cluster_inputs(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    adj: str,
    *,
    adjustment_as_of: date | None = None,
    load_15m: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, str | None, str | None]:
    """[CP-V3-A DEPRECATED] 已迁移至 NodeClusterInputProvider（四链唯一入口）。

    保留此函数仅为向后兼容测试；生产代码必须使用 NodeClusterInputProvider.get_inputs()。
    本函数内部委托 Provider，忽略 load_15m（Node 无条件加载 250+4000）。
    """
    node_input = await NodeClusterInputProvider.get_inputs(
        session, instrument_id, adjustment_as_of=adjustment_as_of,
    )
    return (
        node_input.daily_bars,
        node_input.bars_15m,
        node_input.daily_source_hash,
        node_input.daily_adj_factor_hash,
    )


# ===== 主函数 =====


async def compute_all_indicators(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    bars: int = 250,
    include_smc: bool = False,
    *,
    include_realtime: bool = True,
    completed_only: bool = False,
    adjustment_as_of: date | None = None,
    preloaded_display_bars: BarAggregationResult | None = None,
) -> dict[str, Any]:
    """从 StrategyLoader._registry 获取所有策略，实时计算图表指标。

    流程：
    1. 查询 instrument 信息（symbol）
    2. [图表行情契约] 全部周期通过 MarketDataAggregationService 获取（与 /bars API 共用 SSOT），
       MDAS 内部完成 DB 查询 + Pytdx 兜底 + 复权一次 + 周月聚合
    3. 遍历 StrategyLoader._registry 中的所有策略
    4. 对每个策略，查询最新 released 版本（复用 StrategyBatchService._get_latest_released_version）
    5. 调用 StrategyLoader.load(version) 获取 runtime
    6. 调用 runtime.compute_indicators(context) 计算指标
    7. 收集 chart_layers 定义 + 计算结果（截取最近 bars 根，转 JSON 可序列化）
    8. 计算 source_bar_times/source_bar_hash 作为数据源诊断字段
    9. [CHANGE-011 SMC] 当 include_smc=True 时，按需计算 SMC 指标并注入 smc 图层；
       include_smc=False 时跳过 SMC 计算（不消耗 CPU）。

    [PROMPT.md §二 V2 DisplayWindowSpec] 新增 include_realtime/completed_only/
    adjustment_as_of 参数，与 bars API 同款。展示帧基于同一 DisplayWindowSpec 生成，
    删除 _display_window=100 硬编码，改用 bars 参数。Capture 透传同款 Spec，
    render_frame.matched 由 is_display_frame_match 校验。

    异常处理：单个策略失败不阻塞其他策略，错误记录到 errors 字典返回给前端。

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        timeframe: 周期 1d | 15m | 1h | 1w | 1mo（当前图表指标基于日线）
        adj: 复权方式 qfq | none
        bars: 返回最近 N 根 bar 的指标（默认 250）；同时作为展示窗口大小（V2）
        include_smc: 是否计算 SMC 指标（默认 False，前端通过 ?include_smc=true 显式开启）；
            SMC 是按需计算的独立图层，不进入 DSA、Node 监控、Capture 或右栏 context；
            完全排除 FVG（不计算、不返回、不缓存、不渲染）。
        include_realtime: 是否包含实时 partial bar（默认 True，与 bars API 默认对齐）。
            仅影响 macd_bars（display）查询；Node Cluster 输入固定 completed qfq
            （由 _load_node_cluster_inputs 独立查询，NC-01/NC-02 V2 修复）。
        completed_only: 是否只返回已完成 bar（默认 False，与 bars API 默认对齐）。
            True 时强制 include_realtime=False。仅影响 display；Node Cluster 输入
            固定 completed_only=True（合同常量，不受页面参数影响）。
        adjustment_as_of: 复权锚点 YYYY-MM-DD（默认 None=最新）。透传到 MDAS（display
            与 Node 输入共用同一锚点，保证四链 hash 一致）。
        preloaded_display_bars: [CP-16] 预加载的展示周期行情结果（BarAggregationResult）。
            由 chart_snapshot 端点在一次 MDAS 读取后传入，避免 compute_all_indicators
            内部对同一展示周期再次调用 MDAS get_bars（"一次 MDAS 读取"原子性保证）。
            - 传入时：跳过当前 timeframe 对应的 MDAS 调用，直接使用 preloaded.bars
            - 不传时（None）：保持原有行为，内部自行调 MDAS（向后兼容 /indicators API）
            Node Cluster 输入仍由 _load_node_cluster_inputs 独立查询（completed qfq 合同，
            与展示参数隔离），不受 preloaded 影响。

    Returns:
        dict 包含：
        - layers: list[dict] - 图表图层定义（strategy_id/layer_id/renderer/pane/color/fields 等）
        - data: dict[str, dict] - 按策略分组的指标数据
        - errors: dict[str, str] - 策略错误信息（strategy_id -> error message）
        - source_bar_times: list[str] - 日线行情 ISO 日期字符串数组（数据源诊断）
        - source_bar_hash: str - 日线 OHLCV 拼接的 SHA256 哈希前 16 字符（数据源诊断）
        - display_frame: dict - 展示帧（V2 含 requested_count/actual_count/first_time/
          last_time/include_realtime/is_partial/adjustment_as_of）

    Raises:
        ValueError: instrument 不存在或无日线数据
    """
    logger.info(
        "计算全部策略指标 instrument_id=%s timeframe=%s adj=%s bars=%d "
        "include_realtime=%s completed_only=%s adjustment_as_of=%s",
        instrument_id, timeframe, adj, bars,
        include_realtime, completed_only, adjustment_as_of,
    )

    # 1. 查询 instrument symbol
    inst_stmt = select(Instrument.symbol).where(Instrument.id == instrument_id)
    inst_result = await session.execute(inst_stmt)
    inst_row = inst_result.first()
    if inst_row is None:
        raise ValueError(f"instrument 不存在: instrument_id={instrument_id}")
    symbol = inst_row[0]

    # 2. [图表行情契约] 数据获取：全部周期通过 MarketDataAggregationService（行情聚合 SSOT），
    #    MDAS 内部处理 DB 查询 + Pytdx 兜底 + 复权一次 + 周月聚合
    today = date.today()

    # [CHANGE-20260716-001 required_inputs] 基于实际可用策略（有 released version）
    #   加载日内数据，避免为数据库中不存在的策略（如 volume_node_monitor 未发布）
    #   加载 15min/minute 数据。
    #   当前 timeframe 自身的 macd_bars 完全独立于此逻辑（由 timeframe 直接决定）。
    #   修复根因：静态 _registry 包含 volume_node_monitor/bb_monitor，但数据库无定义，
    #   导致旧逻辑错误地纳入 15min/minute，1d 请求仍执行 2 条不必要的查询。
    available_keys = await _get_available_strategy_keys(
        session, list(StrategyLoader._registry.keys())
    )
    required_bars = _determine_required_bars(available_keys)
    # [CP-V3-A] needs_15min 已删除：Node 无条件加载 250+4000（不再依赖策略注册状态/页面周期）
    needs_minute = "minute" in required_bars

    # 日线：MarketDataAggregationService 统一处理 DB 优先 + Pytdx 兜底 +
    # 前复权 + 去重 + 未完成 Bar 过滤；本层再截取最近 N 根
    # [PROMPT.md §二 V2] 透传 include_realtime/completed_only/adjustment_as_of 到 MDAS，
    #   与 bars API 同款参数，保证同一展示窗口产生同一 display_hash。
    # [CP-16] 当 timeframe=="1d" 且 preloaded_display_bars 传入时，复用预加载结果，
    #   避免对同一展示周期再次调用 MDAS get_bars（"一次 MDAS 读取"原子性保证）。
    daily_count = INDICATOR_BARS.get("1d", 250)
    if preloaded_display_bars is not None and timeframe == "1d":
        daily_agg = preloaded_display_bars
    else:
        daily_agg = await MarketDataAggregationService().get_bars(
            session, instrument_id, timeframe="1d", adj=adj,
            include_realtime=include_realtime,
            completed_only=completed_only,
            adjustment_as_of=adjustment_as_of,
        )
    daily_bars = daily_agg.bars
    # [CHANGE-20260715-002 SMC warmup] 保存完整日线用于 SMC 预热（ATR200 需 200 根，
    # 用户要求展示区之前至少 500 根 warmup；daily_agg.bars 含 DB 全量日线，约 1000+ 根）
    full_daily_bars = daily_agg.bars
    if not daily_bars.empty:
        daily_bars = daily_bars.tail(daily_count)

    # 日内/周线/月线：通过 MarketDataAggregationService 获取（SSOT）。
    # MDAS 内部完成 DB 查询 + Pytdx 兜底 + 复权一次（qfq）+ 周月"日线复权后聚合"。
    # 外层不再二次复权，保证"复权一次"原则（CHANGE-20260717-002）。
    _mdas = MarketDataAggregationService()
    # [CP-V3-A] NodeClusterInputProvider 唯一入口：四链通过 Provider 获取 Node 输入。
    # Node 无条件加载 250 daily + 4000 15m（completed qfq），不再依赖 needs_15min、
    # 页面周期或 released strategy 状态。Provider 内部实现 availability 三态状态机。
    # node_input.daily_bars/bars_15m 同时供 MarketDataContext.bars_15min（策略执行）
    #   和 _compute_independent_node_cluster 使用，保证四链一致。
    node_input = await NodeClusterInputProvider.get_inputs(
        session, instrument_id, adjustment_as_of=adjustment_as_of,
    )
    bars_15min = node_input.bars_15m
    # minute：仅 needs_minute 时查询（VP crossover 仅需 2 根）
    bars_minute = pd.DataFrame()
    if needs_minute:
        rm = await _mdas.get_bars(
            session, instrument_id, timeframe="1m", adj=adj,
            include_realtime=True, limit=NODE_CLUSTER_MINUTE_BARS,
        )
        bars_minute = rm.bars
    # [PROMPT.md §二 V2] macd_agg：当前 timeframe 对应的 MDAS 结果，用于提取 is_partial
    #   和构建 display_frame。各周期均透传 include_realtime/completed_only/adjustment_as_of，
    #   与 bars API 同款，保证 display_hash 一致。
    macd_agg = None  # 由下方各分支赋值
    # 60min：仅 timeframe=="1h" 时查询（MACD 副图 + display）
    # [CP-16] preloaded_display_bars 传入时复用，跳过 MDAS 调用
    bars_60min: pd.DataFrame | None = None
    if timeframe == "1h":
        if preloaded_display_bars is not None:
            r60 = preloaded_display_bars
        else:
            r60 = await _mdas.get_bars(
                session, instrument_id, timeframe="1h", adj=adj,
                include_realtime=include_realtime,
                completed_only=completed_only,
                adjustment_as_of=adjustment_as_of,
            )
        bars_60min = r60.bars
        macd_agg = r60
    # weekly/monthly：MDAS 内部"日线完成复权后再聚合"
    # [CP-16] preloaded_display_bars 传入时复用，跳过 MDAS 调用
    bars_weekly = pd.DataFrame()
    if timeframe == "1w":
        if preloaded_display_bars is not None:
            rw = preloaded_display_bars
        else:
            rw = await _mdas.get_bars(
                session, instrument_id, timeframe="1w", adj=adj,
                include_realtime=include_realtime,
                completed_only=completed_only,
                adjustment_as_of=adjustment_as_of,
            )
        bars_weekly = rw.bars
        macd_agg = rw
    bars_monthly = pd.DataFrame()
    if timeframe == "1mo":
        if preloaded_display_bars is not None:
            rmo = preloaded_display_bars
        else:
            rmo = await _mdas.get_bars(
                session, instrument_id, timeframe="1mo", adj=adj,
                include_realtime=include_realtime,
                completed_only=completed_only,
                adjustment_as_of=adjustment_as_of,
            )
        bars_monthly = rmo.bars
        macd_agg = rmo
    # 15m display：独立查询以透传 include_realtime（不复用 Node 的 bars_15min）
    #   仅当 timeframe=="15m" 时查询；Node 的 bars_15min 仍用 include_realtime=True（Phase 3 将切换）
    # [CP-16] preloaded_display_bars 传入时复用，跳过 MDAS 调用。
    #   注意：chart_snapshot 传入的 preloaded 是无 limit 的完整 DataFrame，
    #   下游 display_df=macd_bars.tail(bars) 会正确截取末尾 bars 根，保证 display_hash 一致。
    macd_agg_15m = None
    if timeframe == "15m":
        if preloaded_display_bars is not None:
            macd_agg_15m = preloaded_display_bars
        else:
            r15_display = await _mdas.get_bars(
                session, instrument_id, timeframe="15m", adj=adj,
                include_realtime=include_realtime,
                completed_only=completed_only,
                adjustment_as_of=adjustment_as_of,
                limit=bars,
            )
            macd_agg_15m = r15_display

    # [MACD 副图] - 按当前 timeframe 选择对应周期 bars 计算 MACD
    # macd_bars 已由 MDAS 完成复权（qfq 在出口应用一次，无需外层二次复权）
    if timeframe == "15m":
        macd_bars = macd_agg_15m.bars if macd_agg_15m is not None else pd.DataFrame()
        macd_agg = macd_agg_15m
    elif timeframe == "1h":
        macd_bars = bars_60min if bars_60min is not None else pd.DataFrame()
    elif timeframe == "1d":
        macd_bars = daily_bars
        macd_agg = daily_agg
    elif timeframe == "1w":
        macd_bars = bars_weekly
    elif timeframe == "1mo":
        macd_bars = bars_monthly
    else:
        macd_bars = pd.DataFrame()

    if macd_bars.empty:
        raise ValueError(
            f"无对应周期行情数据 instrument_id={instrument_id} symbol={symbol} timeframe={timeframe}"
        )

    # 确保 index 是 DatetimeIndex（策略计算依赖）
    if not isinstance(daily_bars.index, pd.DatetimeIndex):
        daily_bars = daily_bars.copy()
        daily_bars.index = pd.to_datetime(daily_bars.index)

    # [图表行情契约] - 计算 source_bar_times/source_bar_hash（SubTask 1.4）
    #   作为数据源诊断字段，前端据此验证 K 线时间与指标数据源一致性
    #   必须在 macd_bars 最终确定后计算（与当前 timeframe 一致，15m/1h 含时间，1d 仅日期）
    #   修复：之前永远用 daily_bars，导致 15m/1h source_bar_times 是日线日期格式，
    #   与 15m K线时间格式不匹配，前端 normalizeChartTime 对 15m 要求 HH:MM，
    #   日线日期返回 null，必然触发 "DSA 数据源不一致" banner。
    source_bar_times: list[str] = compute_source_bar_times(macd_bars, timeframe)
    source_bar_hash: str = compute_source_bar_hash(macd_bars, timeframe)

    # 4. 构建 MarketDataContext
    # [CHANGE-20260720-001 bars_display/bars_daily 分离]
    #   bars_daily = 真正日线（daily_bars），供 Node/BB/SMC 日线结构算法使用；
    #   bars_display = 当前显示周期（macd_bars），供 DSA/MACD/SQZMOM 等当前周期图层使用。
    #   之前 bars_daily=macd_bars 导致 Node/BB 在 15m/1h/1w/1mo 收到非日线数据，
    #   Node Cluster 因日线根数不足返回"暂不可用"。
    #   DSA 改为从 context.bars_display 读取（见 dsa_selector.py），保持全周期对齐。
    context = MarketDataContext(
        instrument_id=instrument_id,
        symbol=symbol,
        bars_daily=daily_bars,
        bars_display=macd_bars,
        display_timeframe=timeframe,
        bars_minute=bars_minute if not bars_minute.empty else None,
        bars_15min=bars_15min if not bars_15min.empty else None,
        trade_date=today,
    )

    # 5. 遍历所有策略，计算指标
    layers: list[dict[str, Any]] = []
    data: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}

    # [indicator_service] - 策略指标 time 来自当前 timeframe bars（与策略输出长度一致）
    # [PR #32] - 改用 macd_bars.index（当前 tf），让 DSA 在 15m/1h/1w/1mo 也有正确 time
    daily_time_list: list[str] = [
        idx.isoformat() for idx in macd_bars.index
    ]

    # [MACD 副图] - 统一在后端按当前 timeframe 计算 MACD 指标，避免前后端多套实现
    # 使用当前 timeframe 对应 bars 的 close 计算，参数 fast=12, slow=26, signal=9
    # [CP-13] 通过 CanonicalComputationService.compute() 调用 macd kernel
    #   canonical adapter 接收 bars DataFrame，内部提取 close 转发到 compute_macd
    #   canonical result_hash 含 source_bar_hash/adj_factor_hash，供四链一致性断言
    macd_canonical = await CanonicalComputationService.compute(
        algorithm_id="macd",
        instrument_id=instrument_id,
        as_of=str(adjustment_as_of) if adjustment_as_of else str(today),
        source_bar_hash=source_bar_hash,
        adj_factor_hash=macd_agg.adj_factor_hash if macd_agg is not None else None,
        bars=macd_bars,
        fast=12,
        slow=26,
        signal=9,
    )
    macd_indicators = macd_canonical.payload  # dict: macd_dif/macd_dea/macd_hist

    # [MACD 副图] - MACD time 与当前 timeframe bars 时间对齐（advice.md 第八节）
    macd_time_list: list[str] = [
        idx.isoformat() for idx in macd_bars.index
    ]

    # [SQZMOM_LB 副图] - 复刻 LazyBear Pine 代码，逐行等价
    # 不修正 dev = multKC * stdev(...)（Pine 原代码如此）
    # 参数：length=20, mult=2.0, lengthKC=20, multKC=1.5, useTrueRange=True
    # 复用 macd_bars（当前 timeframe 已选好的 bars），与 MACD 同源
    # [CP-13] 通过 CanonicalComputationService.compute() 调用 sqzmom kernel
    sqzmom_canonical = await CanonicalComputationService.compute(
        algorithm_id="sqzmom",
        instrument_id=instrument_id,
        as_of=str(adjustment_as_of) if adjustment_as_of else str(today),
        source_bar_hash=source_bar_hash,
        adj_factor_hash=macd_agg.adj_factor_hash if macd_agg is not None else None,
        bars=macd_bars,
        params={"length": 20, "mult": 2.0, "lengthKC": 20, "multKC": 1.5, "useTrueRange": True},
    )
    sqzmom_indicators = sqzmom_canonical.payload  # dict: val/sqzOn/sqzOff/noSqz/bcolor/scolor/params

    # 复用 StrategyBatchService._get_latest_released_version 查询最新 released 版本
    batch_service = StrategyBatchService()

    for strategy_id in StrategyLoader._registry:
        try:
            # 查询最新 released 版本
            _, version = await batch_service._get_latest_released_version(
                session, strategy_id
            )

            # 加载 runtime
            runtime = await StrategyLoader.load(version)

            # 计算指标
            indicators = await runtime.compute_indicators(context)

            # 收集 chart_layers 定义（从 manifest 读取）
            manifest = version.manifest
            chart_layers = manifest.get("chart_layers", [])
            strategy_name = manifest.get("display_name", strategy_id)
            for layer in chart_layers:
                # [PR #32] - 1w/1mo BB 不再移除，由 _adapt_watchlist_bb 用 macd_bars 计算
                layers.append({
                    "strategy_id": strategy_id,
                    "strategy_name": strategy_name,
                    "layer_id": layer.get("id"),
                    "layer_name": layer.get("name"),
                    "renderer": layer.get("renderer"),
                    "pane": layer.get("pane", "price"),
                    "color": layer.get("color"),
                    "direction_colored": layer.get("direction_colored", False),
                    "direction_up_color": layer.get("direction_up_color"),
                    "direction_down_color": layer.get("direction_down_color"),
                    # [DSA 分段] - 透传 regime_field/anchor_field 供前端按 regime 分段渲染
                    "regime_field": layer.get("regime_field"),
                    "anchor_field": layer.get("anchor_field"),
                    "fields": layer.get("fields", []),
                    "hover_fields": layer.get("hover_fields", []),
                })

            # [图表行情契约] - 注入 time 字段（仅当策略未返回 time 时）
            #   SubTask 1.3: 策略（如 DSA）返回自身精确 time 时不再覆盖
            #   daily_time_list 与其他 list 字段一起被 _truncate_lists 截取（保持长度一致），
            #   前端可通过 data[strategy_id]["time"][i] 与 K线 time join 对齐
            if "time" not in indicators:
                indicators_with_time = {**indicators, "time": daily_time_list}
            else:
                indicators_with_time = indicators

            # [BB 图层] - watchlist_monitor BB 按 timeframe 调整后处理
            preserve_keys: frozenset[str] | None = None
            if strategy_id == "watchlist_monitor":
                # [CP-13] _adapt_watchlist_bb 已改为 async + canonical BB 调用
                indicators_with_time = await _adapt_watchlist_bb(
                    indicators_with_time,
                    timeframe,
                    macd_bars,
                    macd_time_list,
                    daily_time_list,
                    instrument_id=instrument_id,
                    adjustment_as_of=str(adjustment_as_of) if adjustment_as_of else None,
                    source_bar_hash=source_bar_hash,
                    adj_factor_hash=macd_agg.adj_factor_hash if macd_agg is not None else None,
                )
                if timeframe == "1d":
                    # 日线保留完整 BB 序列与完整 time，便于前端按时间键匹配
                    preserve_keys = _BB_FIELDS | {"time"}

            data[strategy_id] = _to_json_safe(
                _truncate_lists(indicators_with_time, bars, preserve_keys)
            )

            logger.info(
                "策略指标计算成功 strategy_id=%s layers=%d",
                strategy_id, len(chart_layers),
            )
        except Exception as exc:
            # 记录错误，不阻塞其他策略（错误信息返回给前端）
            errors[strategy_id] = str(exc)
            logger.warning(
                "策略指标计算失败 strategy_id=%s: %s", strategy_id, exc,
            )
            continue

    logger.info(
        "全部策略指标计算完成 instrument_id=%s strategies=%d success=%d failed=%d",
        instrument_id,
        len(StrategyLoader._registry),
        len(data),
        len(errors),
    )

    # [CP-V3-A] 独立输出 data["node_cluster"]
    # Node Cluster 输入由 NodeClusterInputProvider 唯一提供（四链统一入口），
    # availability 三态状态机由 Provider 预计算，unavailable 禁止生成 Profile。
    # 五周期切换时 profile_hash 必须一致（因为输入始终是 node_input.daily_bars + bars_15m）。
    # [CP-13] _compute_independent_node_cluster 通过 canonical node_cluster 调用，
    #   canonical result_hash 含四链一致性维度
    data["node_cluster"] = await _compute_independent_node_cluster(
        node_input,
        symbol=symbol,
        instrument_id=instrument_id,
    )

    # [MACD 副图] - 将 MACD 作为全局图层注入 layers/data
    layers.append({
        "strategy_id": "macd",
        "strategy_name": "MACD",
        "layer_id": "macd",
        "layer_name": "MACD",
        "renderer": "macd",
        "pane": "macd",
        "color": "#f4c430",
        "direction_colored": False,
        "fields": ["macd_dif", "macd_dea", "macd_hist"],
        "hover_fields": ["macd_dif", "macd_dea", "macd_hist"],
    })
    macd_with_time = {**macd_indicators, "time": macd_time_list}
    data["macd"] = _to_json_safe(_truncate_lists(macd_with_time, bars))

    # [SQZMOM_LB 副图] - 将 SQZMOM 作为全局图层注入 layers/data
    # 字段命名加 sqzmom_ 前缀避免与其他策略字段冲突
    sqzmom_renamed = {
        "sqzmom_val": sqzmom_indicators["val"],
        "sqzmom_bcolor": sqzmom_indicators["bcolor"],
        "sqzmom_scolor": sqzmom_indicators["scolor"],
        "sqzmom_sqz_on": sqzmom_indicators["sqzOn"],
        "sqzmom_sqz_off": sqzmom_indicators["sqzOff"],
        "sqzmom_no_sqz": sqzmom_indicators["noSqz"],
        "params": sqzmom_indicators["params"],
        "time": macd_time_list,  # 与 MACD 共用 timeframe bars 时间
    }
    layers.append({
        "strategy_id": "sqzmom_lb",
        "strategy_name": "SQZMOM_LB",
        "layer_id": "sqzmom_lb",
        "layer_name": "SQZMOM_LB",
        "renderer": "sqzmom",
        "pane": "sqzmom",
        "color": "#26a69a",
        "direction_colored": False,
        "fields": ["sqzmom_val", "sqzmom_bcolor", "sqzmom_scolor",
                    "sqzmom_sqz_on", "sqzmom_sqz_off", "sqzmom_no_sqz"],
        "hover_fields": ["sqzmom_val", "sqzmom_bcolor", "sqzmom_scolor",
                          "sqzmom_sqz_on", "sqzmom_sqz_off", "sqzmom_no_sqz"],
    })
    data["sqzmom_lb"] = _to_json_safe(_truncate_lists(sqzmom_renamed, bars))

    # [CHANGE-20260715-007 SMC view adapter] - 按需计算 SMC 指标（include_smc=False 时跳过，0 CPU）
    # SMC 是独立图层，不进入 DSA、Node 监控、Capture 或右栏 context；
    # 完全排除 FVG（不计算、不返回、不缓存、不渲染）；
    # 输出 BOS/CHoCH/OB/EQH/EQL/trailing/swing_bias，每个事件含 anchor/confirmed 因果契约。
    # [CHANGE-20260717-001 Pine parity warmup/历史分离]
    #   Pine 使用全历史计算 SMC；项目必须分离计算历史与展示窗口：
    #   - 1d: full_daily_bars（DB 全量日线，≥500 warmup）
    #   - 15m: 独立查询 bars+_SMC_WARMUP_BARS（5000）根，计算后 adapter 裁成 bars（4000）展示
    #   - 1h/1w: macd_bars（可获得完整历史）
    #   - 1mo: 若 macd_bars < 200 则通过 MDAS 扩展回看到 _SMC_MONTHLY_MIN_BARS（确保 ATR200 可初始化）
    # [view adapter] 完整计算结果经 adapt_smc_to_display_dto 裁成展示窗口 DTO：
    #   - 索引重基准到展示窗口（offset = max(0, total_bars - display_bars)）
    #   - 与窗口相交的活跃 OB 即使 anchor 在窗口左侧也保留并标记 clipped_left
    #   - 响应大小与 bars 上限同阶
    #   - swing_bias 显式返回，前端不再从事件猜测
    # [CHANGE-20260716-001 SMC source diagnostics] 新增 smc_source_bar_hash 等诊断字段，
    #   hash 基于 SMC 实际完整输入（smc_bars），不复用截断后的 macd_bars hash。
    smc_source_diagnostics: dict[str, Any] | None = None
    if include_smc:
        try:
            # [CHANGE-20260718-001 SMC deterministic mode] SMC 使用 completed_only=True
            # 确保仅使用已完成 bar（无 partial bar），与 TV 历史导出可比。
            # 各周期 SMC 输入范围见 _SMC input contract 注释（文件头部）。
            smc_bars: pd.DataFrame
            if timeframe == "1d":
                # 1d: 独立 deterministic 查询（不复用 include_realtime=True 的 daily_agg）
                # 确保 SMC 日线不含今日 partial bar
                r_smc1d = await _mdas.get_bars(
                    session, instrument_id, timeframe="1d", adj=adj,
                    completed_only=True,
                )
                smc_bars = r_smc1d.bars if not r_smc1d.bars.empty else full_daily_bars
            elif timeframe == "15m":
                # 15m: MDAS 获取 bars+_SMC_WARMUP_BARS 根计算集（5000），adapter 裁成 bars（4000）展示
                # [CHANGE-20260718-001] completed_only=True 确保 15m 不含当前未完成 bar
                r_smc15 = await _mdas.get_bars(
                    session, instrument_id, timeframe="15m", adj=adj,
                    completed_only=True, limit=bars, warmup_bars=_SMC_WARMUP_BARS,
                )
                smc_bars = (
                    r_smc15.warmup_bars_full
                    if r_smc15.warmup_bars_full is not None
                    else r_smc15.bars
                )
                # 若查询不足则回退到 macd_bars（标记降级）
                if smc_bars.empty or len(smc_bars) < bars:
                    smc_bars = macd_bars
            elif timeframe == "1mo":
                # 1mo: 若 macd_bars < 200 则通过 MDAS 扩展回看（确保 ATR200 可初始化）
                # [CHANGE-20260718-001] completed_only=True 确保月线不含当前未完成 bar
                if len(macd_bars) < _SMC_MONTHLY_MIN_BARS:
                    r_smcmo = await _mdas.get_bars(
                        session, instrument_id, timeframe="1mo", adj=adj,
                        completed_only=True, limit=_SMC_MONTHLY_MIN_BARS,
                    )
                    smc_bars = r_smcmo.bars if not r_smcmo.bars.empty else macd_bars
                else:
                    # macd_bars 已有 ≥200 根，但可能含 partial bar → 独立 deterministic 查询
                    r_smcmo = await _mdas.get_bars(
                        session, instrument_id, timeframe="1mo", adj=adj,
                        completed_only=True, limit=_SMC_MONTHLY_MIN_BARS,
                    )
                    smc_bars = r_smcmo.bars if not r_smcmo.bars.empty else macd_bars
            elif timeframe in ("1h", "1w"):
                # 1h/1w: 独立 deterministic 查询（不复用 include_realtime=True 的 macd_bars）
                r_smc_intra = await _mdas.get_bars(
                    session, instrument_id, timeframe=timeframe, adj=adj,
                    completed_only=True,
                )
                smc_bars = r_smc_intra.bars if not r_smc_intra.bars.empty else macd_bars
            else:
                smc_bars = macd_bars

            smc_times = [idx.isoformat() for idx in smc_bars.index]
            # [CHANGE-20260716-001] SMC 输入诊断字段（基于完整 smc_bars，非截断 macd_bars）
            # [CP-13] smc_opens/highs/lows/closes 已移除 — canonical adapter 直接接收 bars DataFrame
            smc_source_diagnostics = {
                "smc_source_bar_hash": compute_source_bar_hash(smc_bars, timeframe),
                "smc_source_first_time": smc_times[0] if smc_times else None,
                "smc_source_last_time": smc_times[-1] if smc_times else None,
                "smc_source_bars": len(smc_times),
                "smc_adj": adj,
                "smc_mode": _SMC_MODE_DETERMINISTIC,
            }
            # [CP-13] 通过 CanonicalComputationService.compute() 调用 smc kernel
            #   canonical adapter 内部调 compute_smc_indicators + adapt_smc_to_display_dto
            #   接收 bars DataFrame + display_bars，返回展示窗口 DTO
            #   canonical result_hash 含 source_bar_hash/adj_factor_hash，供四链一致性断言
            smc_canonical = await CanonicalComputationService.compute(
                algorithm_id="smc",
                instrument_id=instrument_id,
                as_of=str(adjustment_as_of) if adjustment_as_of else str(today),
                source_bar_hash=smc_source_diagnostics["smc_source_bar_hash"],
                adj_factor_hash=macd_agg.adj_factor_hash if macd_agg is not None else None,
                bars=smc_bars,
                display_bars=bars,
            )
            smc_dto = smc_canonical.payload  # 展示窗口 DTO dict
            # 注入 smc 图层（main pane，renderer=smc）
            layers.append({
                "strategy_id": "smc",
                "strategy_name": "SMC",
                "layer_id": "smc",
                "layer_name": "SMC",
                "renderer": "smc",
                "pane": "price",
                "color": None,  # SMC 颜色由前端按方向决定（A股红涨绿跌）
                "direction_colored": True,
                "direction_up_color": "#FF4D4F",  # A 股红涨
                "direction_down_color": "#22C55E",  # A 股绿跌
                "fields": [
                    "events", "order_blocks", "equal_highs_lows",
                    "trailing", "swing_bias", "pivots", "time", "view",
                ],
                "hover_fields": [],
            })
            # [CHANGE-20260715-007] 写入展示 DTO（不再透传完整计算结果）
            data["smc"] = _to_json_safe(smc_dto)
            logger.info(
                "SMC 指标计算成功 instrument_id=%s timeframe=%s total_bars=%d display_bars=%d "
                "events=%d obs=%d eqhl=%d swing_bias=%s",
                instrument_id, timeframe,
                smc_dto["view"]["total_bars"], smc_dto["view"]["display_bars"],
                len(smc_dto["events"]), len(smc_dto["order_blocks"]),
                len(smc_dto["equal_highs_lows"]), smc_dto["swing_bias"],
            )
        except Exception as exc:
            # SMC 失败不阻塞主图（记录错误到 errors，前端显示降级提示）
            errors["smc"] = str(exc)
            logger.warning(
                "SMC 指标计算失败 instrument_id=%s: %s", instrument_id, exc,
            )

    # [指标服务] - 返回计算窗口元信息，前端据此决定显示范围，不硬编码
    calculation_window = INDICATOR_BARS.get(timeframe, 800)
    warmup_bars = INDICATOR_WARMUP_BARS.get(timeframe, 60)

    # [display_frame V2] - 展示帧（PROMPT.md §二.1 DisplayWindowSpec V2）：
    #   只描述真正交给前端绘制的 K线窗口。删除 _display_window=100 硬编码，
    #   改用请求 bars 参数作为展示窗口大小，与 bars API page_size 对齐。
    #   bars API 与 indicators API 基于同一 DisplayWindowSpec 生成 frame，
    #   保证同一展示窗口产生同一 display_hash。算法输入 hash 移入 calculation_diagnostics。
    #   Node 的 daily_hash/15m_hash/profile_hash 不参与展示帧匹配。
    display_df = macd_bars.tail(bars) if len(macd_bars) > bars else macd_bars
    # completed_through：优先用 macd_agg.completed_through（MDAS 诊断），回退到 macd_bars 末根时间
    _display_completed_through: str | None = None
    try:
        if macd_agg is not None and macd_agg.completed_through is not None:
            ct = macd_agg.completed_through
            _display_completed_through = (
                ct.isoformat() if hasattr(ct, "isoformat") else str(ct)
            )
    except Exception:
        pass
    if _display_completed_through is None and not macd_bars.empty:
        _display_completed_through = macd_bars.index[-1].isoformat()
    # [PROMPT.md §二 V2] 构建 DisplayWindowSpec，与 bars API 共用同一规格
    display_spec = DisplayWindowSpec(
        instrument_id=str(instrument_id),
        timeframe=timeframe,
        adj=adj,
        requested_count=bars,
        include_realtime=include_realtime,
        completed_only=completed_only,
        adjustment_as_of=(str(adjustment_as_of) if adjustment_as_of else None),
    )
    # is_partial 来自 macd_agg（当前 timeframe 的 MDAS 结果）
    _display_is_partial: bool | None = None
    if macd_agg is not None:
        try:
            _display_is_partial = bool(macd_agg.is_partial)
        except Exception:
            _display_is_partial = None
    display_frame = build_display_frame(
        instrument_id=str(instrument_id),
        timeframe=timeframe,
        adj=adj,
        display_df=display_df,
        completed_through=_display_completed_through,
        spec=display_spec,
        is_partial=_display_is_partial,
    )

    # [calculation_diagnostics] - 算法输入诊断（PROMPT.md §二.1）
    #   所有算法输入侧的 hash/warmup 信息放这里，不参与展示帧匹配。
    #   前端只读不阻塞，用于审计和排查算法输入与展示窗口的偏差。
    _node_meta = (data.get("node_cluster") or {}).get("profile_meta") or {}
    calculation_diagnostics = build_calculation_diagnostics(
        source_bar_hash=source_bar_hash or None,
        source_bar_times=source_bar_times,
        warmup_bars=warmup_bars,
        calculation_window=calculation_window,
        smc_source_bar_hash=(smc_source_diagnostics or {}).get("smc_source_bar_hash"),
        smc_source_bars=(smc_source_diagnostics or {}).get("smc_source_bars") or 0,
        smc_source_first_time=(smc_source_diagnostics or {}).get("smc_source_first_time"),
        smc_source_last_time=(smc_source_diagnostics or {}).get("smc_source_last_time"),
        node_daily_hash=_node_meta.get("daily_source_hash"),
        node_15m_hash=_node_meta.get("bars_15m_source_hash"),
        node_profile_hash=_node_meta.get("profile_hash"),
        algorithm_version=_node_meta.get("algorithm_version"),
        contract_fingerprint=_node_meta.get("contract_fingerprint"),
        market_data_contract_version=(daily_agg.market_data_contract_version if hasattr(daily_agg, "market_data_contract_version") else None),
        adj_factor_hash=(daily_agg.adj_factor_hash if hasattr(daily_agg, "adj_factor_hash") else None),
        adjustment_as_of=(str(daily_agg.adjustment_as_of) if hasattr(daily_agg, "adjustment_as_of") and daily_agg.adjustment_as_of else None),
    )

    return {
        "layers": layers,
        "data": data,
        "errors": errors,
        "calculation_window": calculation_window,
        "warmup_bars": warmup_bars,
        "visible_bars": bars,
        # [CHANGE-20260719-003 §四] 响应 echo timeframe 字段，供前端周期切换乱序丢弃检查
        #   （PROMPT.md §4 要求"generation 不一致响应丢弃"，前端比对 response.timeframe vs 当前 timeframe）
        "timeframe": timeframe,
        # [图表行情契约] - 数据源诊断字段（SubTask 1.4）
        #   前端据此验证 K 线时间与指标数据源一致性；hash 用于跨场景比对
        "source_bar_times": source_bar_times,
        "source_bar_hash": source_bar_hash,
        # [CHANGE-20260716-001 SMC source diagnostics] - SMC 实际输入诊断字段
        #   hash 基于 SMC 完整输入（smc_bars），不复用截断后的 macd_bars hash；
        #   include_smc=False 时为 None，前端据此判断 SMC 是否计算
        "smc_source_bar_hash": smc_source_diagnostics["smc_source_bar_hash"] if smc_source_diagnostics else None,
        "smc_source_first_time": smc_source_diagnostics["smc_source_first_time"] if smc_source_diagnostics else None,
        "smc_source_last_time": smc_source_diagnostics["smc_source_last_time"] if smc_source_diagnostics else None,
        "smc_source_bars": smc_source_diagnostics["smc_source_bars"] if smc_source_diagnostics else 0,
        "smc_adj": smc_source_diagnostics["smc_adj"] if smc_source_diagnostics else None,
        # [display_frame] - 展示帧（PROMPT.md §二.1）：前端 ChartRenderFrame 只比较此字段。
        #   与 bars API 的 display_frame 调用同一个 build_display_frame() 生成，
        #   保证同一展示窗口产生同一 display_hash。
        "display_frame": display_frame,
        # [calculation_diagnostics] - 算法输入诊断（PROMPT.md §二.1）：
        #   算法输入 hash/warmup/版本信息，不参与展示帧匹配，前端只读不阻塞。
        "calculation_diagnostics": calculation_diagnostics,
    }


# ===== 模块自测入口 =====

if __name__ == "__main__":
    # 自测入口：验证模块加载和函数签名（不连 DB/网络）
    import inspect

    logging.basicConfig(level=logging.INFO)

    # 1. 验证 compute_all_indicators 函数存在且签名正确
    # [PROMPT.md §二 V2] 新增 include_realtime/completed_only/adjustment_as_of 关键字参数
    assert callable(compute_all_indicators), "compute_all_indicators 应可调用"
    sig = inspect.signature(compute_all_indicators)
    params = list(sig.parameters.keys())
    expected_params = [
        "session", "instrument_id", "timeframe", "adj", "bars", "include_smc",
        "include_realtime", "completed_only", "adjustment_as_of",
    ]
    assert params == expected_params, \
        f"compute_all_indicators 参数不匹配: {params} != {expected_params}"
    # 验证 V2 新参数默认值与 bars API 对齐
    assert sig.parameters["include_realtime"].default is True, \
        "include_realtime 默认应为 True（与 bars API 默认对齐）"
    assert sig.parameters["completed_only"].default is False, \
        "completed_only 默认应为 False（与 bars API 默认对齐）"
    assert sig.parameters["adjustment_as_of"].default is None, \
        "adjustment_as_of 默认应为 None（最新）"
    print(f"compute_all_indicators params={params} ✓")

    # 2. 验证 StrategyLoader._registry 可访问且非空
    assert hasattr(StrategyLoader, "_registry"), "StrategyLoader 应有 _registry"
    assert len(StrategyLoader._registry) > 0, "_registry 不应为空"
    assert DSA_SELECTOR in StrategyLoader._registry, f"应注册 {DSA_SELECTOR}"
    assert "volume_node_monitor" in StrategyLoader._registry, "应注册 volume_node_monitor"
    print(f"StrategyLoader._registry={list(StrategyLoader._registry.keys())} ✓")

    # 3. 验证 MarketDataContext 字段
    ctx_fields = [f.name for f in MarketDataContext.__dataclass_fields__.values()]
    assert "bars_daily" in ctx_fields, "MarketDataContext 应有 bars_daily"
    assert "bars_15min" in ctx_fields, "MarketDataContext 应有 bars_15min"
    assert "bars_minute" in ctx_fields, "MarketDataContext 应有 bars_minute"
    print(f"MarketDataContext fields={ctx_fields} ✓")

    # 3b. [CHANGE-20260716-001] 验证 required_inputs 映射
    assert DSA_SELECTOR in _REQUIRED_INPUTS, "dsa_selector 应在 required_inputs"
    assert "volume_node_monitor" in _REQUIRED_INPUTS, "volume_node_monitor 应在 required_inputs"
    assert _REQUIRED_INPUTS["volume_node_monitor"] == frozenset({"daily", "15min", "minute"}), \
        "volume_node_monitor 应需要 daily+15min+minute"
    assert _REQUIRED_INPUTS[DSA_SELECTOR] == frozenset({"daily"}), \
        "dsa_selector 应仅需 daily"
    required = _determine_required_bars(set(StrategyLoader._registry.keys()))
    assert "15min" in required, "注册了 volume_node_monitor，应需要 15min"
    assert "minute" in required, "注册了 volume_node_monitor，应需要 minute"
    print(f"_determine_required_bars()={set(required)} ✓")

    # 4. 验证 _to_json_safe 类型转换
    assert _to_json_safe(None) is None, "None 应返回 None"
    assert _to_json_safe(np.int64(42)) == 42, "np.int64 应返回 int"
    assert _to_json_safe(np.float64(3.14)) == 3.14, "np.float64 应返回 float"
    assert _to_json_safe(np.nan) is None, "np.nan 应返回 None"
    assert _to_json_safe(float("inf")) is None, "inf 应返回 None"
    assert _to_json_safe(np.array([1, 2, 3])) == [1, 2, 3], "np.array 应返回 list"
    assert _to_json_safe({"a": np.int64(1)}) == {"a": 1}, "dict 应递归转换"
    assert _to_json_safe([np.float64(1.0), None]) == [1.0, None], "list 应递归转换"
    print("_to_json_safe 类型转换 ✓")

    # 5. 验证 _truncate_lists 截取
    assert _truncate_lists({"a": [1, 2, 3, 4, 5]}, 3) == {"a": [3, 4, 5]}, \
        "应截取最后 3 个元素"
    assert _truncate_lists({"a": [1, 2], "b": 42}, 5) == {"a": [1, 2], "b": 42}, \
        "短列表和标量应保持不变"
    print("_truncate_lists 截取 ✓")

    # 6. [SQZMOM_LB 副图] - [CP-13] kernel 签名验证已移至 canonical_adapters 自测
    #    四链模块不再直接 import kernel 函数，签名检查由 canonical_adapters/__main__ 负责
    print("compute_sqzmom_lb 签名检查已移至 canonical_adapters 自测 ✓")

    # 7. [SQZMOM_LB 副图] - [CP-13] 小样本计算验证已移至 canonical_adapters 自测
    print("compute_sqzmom_lb 小样本计算已移至 canonical_adapters 自测 ✓")

    # 5.1 验证 time 字段注入与截取（advice.md 第三节问题 2/3 修复）
    #   daily_time_list 与其他 list 字段一起被 _truncate_lists 截取，保持长度一致
    indicators_sample = {
        "dsa_vwap": [1.0, 2.0, 3.0, 4.0, 5.0],
        "dsa_dir": [1, 1, 0, 0, 1],
    }
    time_sample = ["t1", "t2", "t3", "t4", "t5"]
    indicators_with_time = {**indicators_sample, "time": time_sample}
    truncated = _truncate_lists(indicators_with_time, 3)
    assert truncated["time"] == ["t3", "t4", "t5"], \
        f"time 字段应与其他 list 一起截取到最后 3 个，实际: {truncated['time']}"
    assert len(truncated["time"]) == len(truncated["dsa_vwap"]), \
        "time 字段长度应与 dsa_vwap 一致"
    # 验证 time 字段不会被当作快照字段跳过
    assert "time" not in _SNAPSHOT_KEYS, "time 不应在快照字段集合中"
    print("time 字段注入与截取 ✓")

    # 6. 验证 StrategyBatchService 可实例化（复用 _get_latest_released_version）
    svc = StrategyBatchService()
    assert hasattr(svc, "_get_latest_released_version"), \
        "StrategyBatchService 应有 _get_latest_released_version 方法"
    print("StrategyBatchService 可实例化 ✓")

    # 7. 验证 INDICATOR_BARS 常量与返回结构元信息（advice.md 第四节）
    assert "1d" in INDICATOR_BARS, "INDICATOR_BARS 应包含 1d"
    assert INDICATOR_BARS["1d"] == 250, "1d 计算窗口应为 250"
    assert INDICATOR_BARS["1w"] == 260, "1w 计算窗口应为 260"
    assert INDICATOR_BARS["1mo"] == 120, "1mo 计算窗口应为 120"
    assert "1d" in INDICATOR_WARMUP_BARS, "INDICATOR_WARMUP_BARS 应包含 1d"
    print("INDICATOR_BARS 常量 ✓")

    # 8. 验证 compute_macd 计算（advice.md 第五节）
    sample_close = np.array([10.0, 10.5, 10.3, 10.8, 11.0, 11.2, 10.9, 11.5, 11.3, 11.8])
    macd = compute_macd(sample_close)
    assert "macd_dif" in macd
    assert "macd_dea" in macd
    assert "macd_hist" in macd
    assert len(macd["macd_dif"]) == len(sample_close)
    # 验证 hist = 2 * (dif - dea)
    for i in range(len(sample_close)):
        dif = macd["macd_dif"][i]
        dea = macd["macd_dea"][i]
        hist = macd["macd_hist"][i]
        if dif is not None and dea is not None and hist is not None:
            assert abs(hist - 2.0 * (dif - dea)) < 1e-9, "MACD 柱值公式错误"
    print("compute_macd 公式 ✓")

    # 9. [CHANGE-011 SMC] - [CP-13] kernel 签名验证已移至 canonical_adapters 自测
    #    四链模块不再直接 import kernel 函数，签名检查由 canonical_adapters/__main__ 负责
    print("compute_smc_indicators 签名检查已移至 canonical_adapters 自测 ✓")

    # 10. [CHANGE-011 SMC] - 验证 include_smc=False 不影响计算（默认值）
    sig_all = inspect.signature(compute_all_indicators)
    assert sig_all.parameters["include_smc"].default is False, \
        "include_smc 默认值应为 False（按需计算，前端默认不开启）"
    print("include_smc 默认值=False ✓")

    print("OK")
