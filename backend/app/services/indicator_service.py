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
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.indicator_contract import (
    INDICATOR_BARS,
    NODE_CLUSTER_LOW_BARS,
    NODE_CLUSTER_MINUTE_BARS,
)
from app.constants.strategy_keys import DSA_SELECTOR, WATCHLIST_MONITOR
from app.models.instrument import Instrument
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.repositories.bar_repository import (
    _get_adj_factor_df,
    _query_15min_bars,
    _query_60min_bars,
    _query_minute_bars,
    apply_adj_factor_to_bars,
    fetch_monthly_bars,
    fetch_weekly_bars,
)
from app.services.chart_bars_service import (
    compute_source_bar_hash,
    compute_source_bar_times,
)
from app.services.market_data_aggregation_service import MarketDataAggregationService
from app.services.smc_view_adapter import adapt_smc_to_display_dto
from app.services.strategy_batch_service import StrategyBatchService
from app.strategy.runtime import MarketDataContext, StrategyLoader
from app.strategy_assets.algorithms.features.merged_dsa_atr_rope_bb_factors import (
    compute_bollinger,
)
from app.strategy_assets.algorithms.features.smc_indicator import (
    compute_smc_indicators,
)
from app.strategy_assets.algorithms.features.sqzmom_lb import compute_sqzmom_lb

logger = logging.getLogger("services.indicator_service")

# 查询范围常量（日线 5000 天，日内按需）
_DEFAULT_DAILY_LOOKBACK_DAYS = 5000  # 日线默认回看 5000 天（与 bars.py 一致）
# [CHANGE-20260716-001 required_inputs] 日内回看天数按数据类型独立设定：
#   15min: 400 天足够覆盖 NODE_CLUSTER_LOW_BARS=4000（4000/16=250 交易日≈350 日历日）
#   minute: 5 天足够覆盖 NODE_CLUSTER_MINUTE_BARS=2（仅需最后 2 根）
#   60min: 750 天（1h 指标窗口需要更长历史）
_15MIN_LOOKBACK_DAYS = 400  # 15min 回看 400 天（VP 需要 4000 根，不再查 750 天 12000 根）
_MINUTE_LOOKBACK_DAYS = 5  # minute 回看 5 天（VP crossover 仅需 2 根，不再查 750 天 180000 根）
_60MIN_LOOKBACK_DAYS = 750  # 60min 回看 750 天（1h 指标计算需要完整历史）

# [CHANGE-20260717-001 Pine parity] SMC warmup/历史分离
# Pine 使用全历史计算 SMC；项目 15m 展示 4000 根时 SMC 必须额外查询 warmup，
# 计算 5000 根后由 adapter 裁成 4000 展示（pivot/BOS/CHoCH 在窗口左缘不丢失）
_SMC_WARMUP_BARS = 1000  # 15m 专用 SMC warmup（计算=展示+warmup）
_SMC_MONTHLY_MIN_BARS = 200  # 1mo 最少 bar 数（ATR200 需 200 根才能初始化）
_SMC_MONTHLY_LOOKBACK_DAYS = 7000  # 1mo 扩展回看（200 月 ≈ 6000 天，留余量）

# [CHANGE-20260716-001 required_inputs] 策略→所需 bar 类型映射
# 定义每个注册策略实际需要哪些 bar 类型，避免无条件查询全量日内数据。
# volume_node_monitor 需要 15min（VP profile）和 minute（crossover 检测），
# 其他策略仅需 daily。
_REQUIRED_INPUTS: dict[str, frozenset[str]] = {
    DSA_SELECTOR: frozenset({"daily"}),
    "volume_node_monitor": frozenset({"daily", "15min", "minute"}),
    "bb_monitor": frozenset({"daily"}),
    WATCHLIST_MONITOR: frozenset({"daily"}),
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


def _adapt_watchlist_bb(
    indicators: dict[str, Any],
    timeframe: str,
    macd_bars: pd.DataFrame,
    macd_time_list: list[str],
    daily_time_list: list[str],
) -> dict[str, Any]:
    """调整 watchlist_monitor 的 BB 输出以匹配当前 timeframe。

    - 日线：保留完整日线 BB 序列（不截断），time 同步完整
    - 15m/1h/1w/1mo：用 macd_bars 重新计算 BB（length=20, mult=2.0），不再映射日线阶梯线

    修复根因（PR #31）：
        之前 15m/1h 调用 _map_daily_to_intraday 把日线 BB 映射到日内时间轴，
        导致 15m BB 全部相同（阶梯线），不是真正的 15m 周期 BB。
        新行为：15m/1h 用 compute_bollinger(macd_bars) 重新计算 BB，
        bb_upper/bb_mid/bb_lower 反映当前 timeframe close 的波动。

    [PR #32] - 1w/1mo 也用 compute_bollinger(macd_bars) 计算，不再移除 BB 字段。
        之前 1w/1mo 直接 pop BB 字段导致前端无 BB overlay。

    Args:
        indicators: watchlist_monitor 原始指标字典
        timeframe: 当前请求周期
        macd_bars: 当前 timeframe 对应的 bars（用于 BB 计算）
        macd_time_list: 当前 timeframe 对应的时间列表
        daily_time_list: 日线时间列表（15m/1h 路径不再使用，保留参数兼容）

    Returns:
        调整后的指标字典
    """
    result = dict(indicators)
    bb_fields_present = {f for f in _BB_FIELDS if f in result}

    if timeframe in ("15m", "1h", "1w", "1mo"):
        # [PR #31/#32] - 15m/1h/1w/1mo BB 用 macd_bars 重新计算，不再映射日线阶梯线或移除
        #   compute_bollinger 返回 bb_upper/bb_mid/bb_lower/bb_pos_01/bb_width_norm
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

        bb_result = compute_bollinger(macd_bars, length=20, mult=2.0)
        # 字段映射：compute_bollinger 返回名 → watchlist_monitor 字段名
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


# ===== 主函数 =====


async def compute_all_indicators(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    bars: int = 250,
    include_smc: bool = False,
) -> dict[str, Any]:
    """从 StrategyLoader._registry 获取所有策略，实时计算图表指标。

    流程：
    1. 查询 instrument 信息（symbol）
    2. [图表行情契约] 通过 load_chart_bars 获取日线（与 /bars API 共用 SSOT），
       日内/周线/月线通过 DB 查询获取
    3. 遍历 StrategyLoader._registry 中的所有策略
    4. 对每个策略，查询最新 released 版本（复用 StrategyBatchService._get_latest_released_version）
    5. 调用 StrategyLoader.load(version) 获取 runtime
    6. 调用 runtime.compute_indicators(context) 计算指标
    7. 收集 chart_layers 定义 + 计算结果（截取最近 bars 根，转 JSON 可序列化）
    8. 计算 source_bar_times/source_bar_hash 作为数据源诊断字段
    9. [CHANGE-011 SMC] 当 include_smc=True 时，按需计算 SMC 指标并注入 smc 图层；
       include_smc=False 时跳过 SMC 计算（不消耗 CPU）。

    异常处理：单个策略失败不阻塞其他策略，错误记录到 errors 字典返回给前端。

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        timeframe: 周期 1d | 15m | 1h | 1w | 1mo（当前图表指标基于日线）
        adj: 复权方式 qfq | none
        bars: 返回最近 N 根 bar 的指标（默认 250）
        include_smc: 是否计算 SMC 指标（默认 False，前端通过 ?include_smc=true 显式开启）；
            SMC 是按需计算的独立图层，不进入 DSA、Node 监控、Capture 或右栏 context；
            完全排除 FVG（不计算、不返回、不缓存、不渲染）。

    Returns:
        dict 包含：
        - layers: list[dict] - 图表图层定义（strategy_id/layer_id/renderer/pane/color/fields 等）
        - data: dict[str, dict] - 按策略分组的指标数据
        - errors: dict[str, str] - 策略错误信息（strategy_id -> error message）
        - source_bar_times: list[str] - 日线行情 ISO 日期字符串数组（数据源诊断）
        - source_bar_hash: str - 日线 OHLCV 拼接的 SHA256 哈希前 16 字符（数据源诊断）

    Raises:
        ValueError: instrument 不存在或无日线数据
    """
    logger.info(
        "计算全部策略指标 instrument_id=%s timeframe=%s adj=%s bars=%d",
        instrument_id, timeframe, adj, bars,
    )

    # 1. 查询 instrument symbol
    inst_stmt = select(Instrument.symbol).where(Instrument.id == instrument_id)
    inst_result = await session.execute(inst_stmt)
    inst_row = inst_result.first()
    if inst_row is None:
        raise ValueError(f"instrument 不存在: instrument_id={instrument_id}")
    symbol = inst_row[0]

    # 2. [图表行情契约] 数据获取：日线通过 MarketDataAggregationService（行情聚合 SSOT），
    #    日内/周线/月线通过 DB 查询获取
    today = date.today()
    daily_start = today - timedelta(days=_DEFAULT_DAILY_LOOKBACK_DAYS)
    intraday_end_dt = datetime.combine(today, datetime.max.time())

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
    needs_15min = "15min" in required_bars or timeframe == "15m"
    needs_minute = "minute" in required_bars

    # 日线：MarketDataAggregationService 统一处理 DB 优先 + Pytdx 兜底 +
    # 前复权 + 去重 + 未完成 Bar 过滤；本层再截取最近 N 根
    daily_count = INDICATOR_BARS.get("1d", 250)
    daily_agg = await MarketDataAggregationService().get_bars(
        session, instrument_id, timeframe="1d", adj=adj,
    )
    daily_bars = daily_agg.bars
    # [CHANGE-20260715-002 SMC warmup] 保存完整日线用于 SMC 预热（ATR200 需 200 根，
    # 用户要求展示区之前至少 500 根 warmup；daily_agg.bars 含 DB 全量日线，约 1000+ 根）
    full_daily_bars = daily_agg.bars
    if not daily_bars.empty:
        daily_bars = daily_bars.tail(daily_count)

    # 日内/周线/月线：DB 查询（与原 DB 降级路径一致，SSOT）
    # [CHANGE-20260716-001] 15min 仅在需要时查询，limit=NODE_CLUSTER_LOW_BARS 让 DB 只返回 4000 根
    if needs_15min:
        start_15min = datetime.combine(
            today - timedelta(days=_15MIN_LOOKBACK_DAYS), datetime.min.time(),
        )
        bars_15min = await _query_15min_bars(
            session, instrument_id, start_15min, intraday_end_dt,
            limit=NODE_CLUSTER_LOW_BARS,
        )
    else:
        bars_15min = pd.DataFrame()
    # [CHANGE-20260716-001] minute 仅在需要时查询，limit=NODE_CLUSTER_MINUTE_BARS 让 DB 只返回 2 根
    if needs_minute:
        start_minute = datetime.combine(
            today - timedelta(days=_MINUTE_LOOKBACK_DAYS), datetime.min.time(),
        )
        bars_minute = await _query_minute_bars(
            session, instrument_id, start_minute, intraday_end_dt,
            limit=NODE_CLUSTER_MINUTE_BARS,
        )
    else:
        bars_minute = pd.DataFrame()
    bars_60min: pd.DataFrame | None = None
    if timeframe == "1h":
        start_60min = datetime.combine(
            today - timedelta(days=_60MIN_LOOKBACK_DAYS), datetime.min.time(),
        )
        bars_60min = await _query_60min_bars(
            session, instrument_id, start_60min, intraday_end_dt,
        )
    bars_weekly = pd.DataFrame()
    if timeframe == "1w":
        bars_weekly = await fetch_weekly_bars(session, instrument_id, daily_start, today)
    bars_monthly = pd.DataFrame()
    if timeframe == "1mo":
        bars_monthly = await fetch_monthly_bars(session, instrument_id, daily_start, today)

    # 3. [图表行情契约] 前复权处理：仅对非日线（日线已由 load_chart_bars 处理）
    if adj == "qfq":
        adj_factor_df = await _get_adj_factor_df(session, instrument_id)
        # 周线/月线前复权
        if not bars_weekly.empty:
            bars_weekly = apply_adj_factor_to_bars(bars_weekly, adj_factor_df, intraday=False)
        if not bars_monthly.empty:
            bars_monthly = apply_adj_factor_to_bars(bars_monthly, adj_factor_df, intraday=False)
        # 15min/1min/60min 日内前复权
        if not bars_15min.empty:
            bars_15min = apply_adj_factor_to_bars(
                bars_15min, adj_factor_df, intraday=True
            )
        if not bars_minute.empty:
            bars_minute = apply_adj_factor_to_bars(
                bars_minute, adj_factor_df, intraday=True
            )
        if bars_60min is not None and not bars_60min.empty:
            bars_60min = apply_adj_factor_to_bars(
                bars_60min, adj_factor_df, intraday=True
            )

    # [MACD 副图] - 按当前 timeframe 选择对应周期 bars 计算 MACD
    # 必须在 apply_adj_factor 完成后选择，确保 macd_bars 指向已复权的 DataFrame
    if timeframe == "15m":
        macd_bars = bars_15min
    elif timeframe == "1h":
        macd_bars = bars_60min if bars_60min is not None else pd.DataFrame()
    elif timeframe == "1d":
        macd_bars = daily_bars
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
    # [PR #32] - bars_daily 传入 macd_bars（当前 timeframe bars），让 DSA 在全周期计算
    #   之前传 daily_bars 导致 15m/1h/1w/1mo 的 DSA 是日线 DSA，与当前周期 K线不对齐
    context = MarketDataContext(
        instrument_id=instrument_id,
        symbol=symbol,
        bars_daily=macd_bars,
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
    macd_indicators = compute_macd(macd_bars["close"].to_numpy(float))

    # [MACD 副图] - MACD time 与当前 timeframe bars 时间对齐（advice.md 第八节）
    macd_time_list: list[str] = [
        idx.isoformat() for idx in macd_bars.index
    ]

    # [SQZMOM_LB 副图] - 复刻 LazyBear Pine 代码，逐行等价
    # 不修正 dev = multKC * stdev(...)（Pine 原代码如此）
    # 参数：length=20, mult=2.0, lengthKC=20, multKC=1.5, useTrueRange=True
    # 复用 macd_bars（当前 timeframe 已选好的 bars），与 MACD 同源
    sqzmom_indicators = compute_sqzmom_lb(
        opens=macd_bars["open"].to_numpy(float),
        highs=macd_bars["high"].to_numpy(float),
        lows=macd_bars["low"].to_numpy(float),
        closes=macd_bars["close"].to_numpy(float),
        params={"length": 20, "mult": 2.0, "lengthKC": 20, "multKC": 1.5, "useTrueRange": True},
    )

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
                indicators_with_time = _adapt_watchlist_bb(
                    indicators_with_time,
                    timeframe,
                    macd_bars,
                    macd_time_list,
                    daily_time_list,
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
    #   - 1mo: 若 macd_bars < 200 则扩展回看到 _SMC_MONTHLY_LOOKBACK_DAYS（确保 ATR200 可初始化）
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
            # [CHANGE-20260717-001 Pine parity] 计算历史与展示窗口分离
            smc_bars: pd.DataFrame
            if timeframe == "1d" and not full_daily_bars.empty:
                # 1d: 完整日线（已有 ≥500 warmup）
                smc_bars = full_daily_bars
            elif timeframe == "15m":
                # 15m: 独立查询 bars+_SMC_WARMUP_BARS 根（计算 5000，展示 4000）
                smc_15min_limit = bars + _SMC_WARMUP_BARS
                smc_start_15min = datetime.combine(
                    today - timedelta(days=_15MIN_LOOKBACK_DAYS), datetime.min.time(),
                )
                smc_bars = await _query_15min_bars(
                    session, instrument_id, smc_start_15min, intraday_end_dt,
                    limit=smc_15min_limit,
                )
                # 前复权处理（与主 15m 路径一致）
                if adj == "qfq" and not smc_bars.empty:
                    adj_factor_df_smc = await _get_adj_factor_df(session, instrument_id)
                    smc_bars = apply_adj_factor_to_bars(
                        smc_bars, adj_factor_df_smc, intraday=True
                    )
                # 若查询不足则回退到 macd_bars
                if smc_bars.empty or len(smc_bars) < bars:
                    smc_bars = macd_bars
            elif timeframe == "1mo":
                # 1mo: 若 macd_bars < 200 则扩展回看（确保 ATR200 可初始化）
                if len(macd_bars) < _SMC_MONTHLY_MIN_BARS:
                    monthly_start_extended = today - timedelta(days=_SMC_MONTHLY_LOOKBACK_DAYS)
                    smc_bars = await fetch_monthly_bars(
                        session, instrument_id, monthly_start_extended, today,
                    )
                    if adj == "qfq" and not smc_bars.empty:
                        adj_factor_df_smc = await _get_adj_factor_df(session, instrument_id)
                        smc_bars = apply_adj_factor_to_bars(
                            smc_bars, adj_factor_df_smc, intraday=False
                        )
                    if smc_bars.empty:
                        smc_bars = macd_bars
                else:
                    smc_bars = macd_bars
            else:
                # 1h/1w: macd_bars（可获得完整历史）
                smc_bars = macd_bars

            smc_opens = smc_bars["open"].to_numpy(float).tolist()
            smc_highs = smc_bars["high"].to_numpy(float).tolist()
            smc_lows = smc_bars["low"].to_numpy(float).tolist()
            smc_closes = smc_bars["close"].to_numpy(float).tolist()
            smc_times = [idx.isoformat() for idx in smc_bars.index]
            # [CHANGE-20260716-001] SMC 输入诊断字段（基于完整 smc_bars，非截断 macd_bars）
            smc_source_diagnostics = {
                "smc_source_bar_hash": compute_source_bar_hash(smc_bars, timeframe),
                "smc_source_first_time": smc_times[0] if smc_times else None,
                "smc_source_last_time": smc_times[-1] if smc_times else None,
                "smc_source_bars": len(smc_times),
                "smc_adj": adj,
            }
            smc_result = compute_smc_indicators(
                opens=smc_opens,
                highs=smc_highs,
                lows=smc_lows,
                closes=smc_closes,
                times=smc_times,
            )
            # [CHANGE-20260715-007] 调用 view adapter 裁成展示窗口 DTO
            # display_bars = bars（前端可见窗口上限），与 indicators API 的 bars 参数同源
            smc_dto = adapt_smc_to_display_dto(smc_result, bars)
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

    return {
        "layers": layers,
        "data": data,
        "errors": errors,
        "calculation_window": calculation_window,
        "warmup_bars": warmup_bars,
        "visible_bars": bars,
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
    }


# ===== 模块自测入口 =====

if __name__ == "__main__":
    # 自测入口：验证模块加载和函数签名（不连 DB/网络）
    import inspect

    logging.basicConfig(level=logging.INFO)

    # 1. 验证 compute_all_indicators 函数存在且签名正确
    assert callable(compute_all_indicators), "compute_all_indicators 应可调用"
    sig = inspect.signature(compute_all_indicators)
    params = list(sig.parameters.keys())
    expected_params = ["session", "instrument_id", "timeframe", "adj", "bars", "include_smc"]
    assert params == expected_params, \
        f"compute_all_indicators 参数不匹配: {params} != {expected_params}"
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

    # 6. [SQZMOM_LB 副图] - 验证 compute_sqzmom_lb 可导入且签名正确
    assert callable(compute_sqzmom_lb), "compute_sqzmom_lb 应可调用"
    sig_sqzmom = inspect.signature(compute_sqzmom_lb)
    sqzmom_params = list(sig_sqzmom.parameters.keys())
    expected_sqzmom = ["opens", "highs", "lows", "closes", "params"]
    assert sqzmom_params == expected_sqzmom, \
        f"compute_sqzmom_lb 参数不匹配: {sqzmom_params} != {expected_sqzmom}"
    print(f"compute_sqzmom_lb params={sqzmom_params} ✓")

    # 7. [SQZMOM_LB 副图] - 验证小样本计算不抛异常
    import numpy as np
    rng = np.random.default_rng(42)
    n = 60
    closes_t = 100.0 + np.cumsum(rng.normal(0, 0.5, n))
    highs_t = closes_t + np.abs(rng.normal(0, 1.0, n))
    lows_t = closes_t - np.abs(rng.normal(0, 1.0, n))
    opens_t = closes_t + rng.normal(0, 0.3, n)
    sqzmom_result = compute_sqzmom_lb(
        opens=opens_t, highs=highs_t, lows=lows_t, closes=closes_t,
    )
    assert "val" in sqzmom_result and "bcolor" in sqzmom_result
    assert "sqzOn" in sqzmom_result and "sqzOff" in sqzmom_result
    assert "noSqz" in sqzmom_result and "scolor" in sqzmom_result
    assert sqzmom_result["params"]["bb_dev_uses"] == "multKC"
    print(f"compute_sqzmom_lb full run OK (n={n}) ✓")

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

    # 9. [CHANGE-011 SMC] - 验证 compute_smc_indicators 可导入且签名正确
    assert callable(compute_smc_indicators), "compute_smc_indicators 应可调用"
    sig_smc = inspect.signature(compute_smc_indicators)
    smc_params = list(sig_smc.parameters.keys())
    expected_smc = ["opens", "highs", "lows", "closes", "times", "params"]
    assert smc_params == expected_smc, \
        f"compute_smc_indicators 参数不匹配: {smc_params} != {expected_smc}"
    print(f"compute_smc_indicators params={smc_params} ✓")

    # 10. [CHANGE-011 SMC] - 验证 include_smc=False 不影响计算（默认值）
    sig_all = inspect.signature(compute_all_indicators)
    assert sig_all.parameters["include_smc"].default is False, \
        "include_smc 默认值应为 False（按需计算，前端默认不开启）"
    print("include_smc 默认值=False ✓")

    print("OK")
