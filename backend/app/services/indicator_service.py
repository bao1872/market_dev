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

from app.core.exchange import get_exchange
from app.models.instrument import Instrument
from app.repositories.bar_repository import (
    _get_adj_factor_df,
    _query_15min_bars,
    _query_60min_bars,
    _query_minute_bars,
    apply_adj_factor_to_bars,
    fetch_daily_bars,
)
from app.services.strategy_batch_service import StrategyBatchService
from app.strategy.runtime import MarketDataContext, StrategyLoader

logger = logging.getLogger("services.indicator_service")

# 查询范围常量（日线 5000 天，日内 750 天）
_DEFAULT_DAILY_LOOKBACK_DAYS = 5000  # 日线默认回看 5000 天（与 bars.py 一致）
_INDICATOR_INTRADAY_LOOKBACK_DAYS = 750  # 指标计算专用 15min/1min 回看天数（750 天，与 bars.py 的 API 查询回看 180 天不同，指标计算需要更多数据）


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


def _truncate_lists(indicators: dict[str, Any], bars: int) -> dict[str, Any]:
    """截取指标数据到最近 N 根 bar。

    对值为 list 的字段，截取最后 bars 个元素。
    非列表字段（如标量）保持不变。

    Args:
        indicators: 策略返回的指标字典
        bars: 保留最近 N 根 bar

    Returns:
        截取后的指标字典
    """
    if bars <= 0:
        return indicators
    result: dict[str, Any] = {}
    for key, val in indicators.items():
        if isinstance(val, list) and len(val) > bars:
            result[key] = val[-bars:]
        else:
            result[key] = val
    return result


# ===== 主函数 =====


async def compute_all_indicators(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    bars: int = 250,
) -> dict[str, Any]:
    """从 StrategyLoader._registry 获取所有策略，实时计算图表指标。

    流程：
    1. 查询 instrument 信息（symbol）
    2. 通过 Exchange.klines() 获取 bars 数据（日线 + 15min + 1min），失败降级到 DB
    3. 遍历 StrategyLoader._registry 中的所有策略
    4. 对每个策略，查询最新 released 版本（复用 StrategyBatchService._get_latest_released_version）
    5. 调用 StrategyLoader.load(version) 获取 runtime
    6. 调用 runtime.compute_indicators(context) 计算指标
    7. 收集 chart_layers 定义 + 计算结果（截取最近 bars 根，转 JSON 可序列化）

    异常处理：单个策略失败不阻塞其他策略，错误记录到 errors 字典返回给前端。

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        timeframe: 周期 1d | 15m | 1h | 1w | 1mo（当前图表指标基于日线）
        adj: 复权方式 qfq | none
        bars: 返回最近 N 根 bar 的指标（默认 250）

    Returns:
        dict 包含：
        - layers: list[dict] - 图表图层定义（strategy_id/layer_id/renderer/pane/color/fields 等）
        - data: dict[str, dict] - 按策略分组的指标数据
        - errors: dict[str, str] - 策略错误信息（strategy_id -> error message）

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

    # 2. 数据获取：通过 Exchange.klines()（与 bars API 统一数据源）
    today = date.today()
    exchange = None
    try:
        exchange = get_exchange("A")
    except Exception as exc:
        logger.warning("获取 Exchange 实例失败 instrument_id=%s: %s", instrument_id, exc)

    daily_bars = pd.DataFrame()
    bars_15min = pd.DataFrame()
    bars_minute = pd.DataFrame()
    bars_60min: pd.DataFrame | None = None

    if exchange is not None:
        try:
            # 日线（5000 天回看，与 bars.py 一致）
            daily_bars = await exchange.klines(symbol, "1d", count=5000) or pd.DataFrame()
            # 15 分钟线（指标计算需要更多数据）
            bars_15min = await exchange.klines(symbol, "15m", count=800) or pd.DataFrame()
            # 1 分钟线（仅 2 天，用于监控策略）
            bars_minute = await exchange.klines(symbol, "1m", count=2) or pd.DataFrame()
            # 60 分钟线（策略在 timeframe='1h' 时可能需要）
            if timeframe == "1h":
                bars_60min = await exchange.klines(symbol, "1h", count=800) or pd.DataFrame()
        except Exception as exc:
            logger.warning("Exchange.klines() 失败 instrument_id=%s: %s，降级到 DB", instrument_id, exc)
            exchange = None  # 标记为失败，后续使用 DB 降级

    # Exchange 失败或无数据时降级到 DB 查询
    if exchange is None or daily_bars.empty:
        logger.info("使用 DB 降级查询 instrument_id=%s", instrument_id)
        daily_start = today - timedelta(days=_DEFAULT_DAILY_LOOKBACK_DAYS)
        intraday_start_dt = datetime.combine(
            today - timedelta(days=_INDICATOR_INTRADAY_LOOKBACK_DAYS),
            datetime.min.time(),
        )
        intraday_end_dt = datetime.combine(today, datetime.max.time())
        try:
            daily_bars = await fetch_daily_bars(session, instrument_id, daily_start, today)
            bars_15min = await _query_15min_bars(session, instrument_id, intraday_start_dt, intraday_end_dt)
            bars_minute = await _query_minute_bars(session, instrument_id, intraday_start_dt, intraday_end_dt)
            if timeframe == "1h":
                bars_60min = await _query_60min_bars(session, instrument_id, intraday_start_dt, intraday_end_dt)
        except Exception as exc:
            logger.warning("DB 降级查询也失败 instrument_id=%s: %s", instrument_id, exc)

    if daily_bars.empty:
        raise ValueError(
            f"无日线行情数据 instrument_id={instrument_id} symbol={symbol}"
        )

    # 3. 前复权处理
    if adj == "qfq":
        adj_factor_df = await _get_adj_factor_df(session, instrument_id)
        # 日线前复权
        daily_bars = apply_adj_factor_to_bars(daily_bars, adj_factor_df, intraday=False)
        # 15min/1min 日内前复权
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

    # 确保 index 是 DatetimeIndex（策略计算依赖）
    if not isinstance(daily_bars.index, pd.DatetimeIndex):
        daily_bars = daily_bars.copy()
        daily_bars.index = pd.to_datetime(daily_bars.index)

    # 4. 构建 MarketDataContext
    context = MarketDataContext(
        instrument_id=instrument_id,
        symbol=symbol,
        bars_daily=daily_bars,
        bars_minute=bars_minute if not bars_minute.empty else None,
        bars_15min=bars_15min if not bars_15min.empty else None,
        trade_date=today,
    )

    # 5. 遍历所有策略，计算指标
    layers: list[dict[str, Any]] = []
    data: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}

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
                    "fields": layer.get("fields", []),
                    "hover_fields": layer.get("hover_fields", []),
                })

            # 截取到最近 bars 根 + 转 JSON 可序列化
            data[strategy_id] = _to_json_safe(_truncate_lists(indicators, bars))

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

    return {
        "layers": layers,
        "data": data,
        "errors": errors,
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
    expected_params = ["session", "instrument_id", "timeframe", "adj", "bars"]
    assert params == expected_params, \
        f"compute_all_indicators 参数不匹配: {params} != {expected_params}"
    print(f"compute_all_indicators params={params} ✓")

    # 2. 验证 StrategyLoader._registry 可访问且非空
    assert hasattr(StrategyLoader, "_registry"), "StrategyLoader 应有 _registry"
    assert len(StrategyLoader._registry) > 0, "_registry 不应为空"
    assert "dsa_selector" in StrategyLoader._registry, "应注册 dsa_selector"
    assert "volume_node_monitor" in StrategyLoader._registry, "应注册 volume_node_monitor"
    print(f"StrategyLoader._registry={list(StrategyLoader._registry.keys())} ✓")

    # 3. 验证 MarketDataContext 字段
    ctx_fields = [f.name for f in MarketDataContext.__dataclass_fields__.values()]
    assert "bars_daily" in ctx_fields, "MarketDataContext 应有 bars_daily"
    assert "bars_15min" in ctx_fields, "MarketDataContext 应有 bars_15min"
    assert "bars_minute" in ctx_fields, "MarketDataContext 应有 bars_minute"
    print(f"MarketDataContext fields={ctx_fields} ✓")

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

    # 6. 验证 StrategyBatchService 可实例化（复用 _get_latest_released_version）
    svc = StrategyBatchService()
    assert hasattr(svc, "_get_latest_released_version"), \
        "StrategyBatchService 应有 _get_latest_released_version 方法"
    print("StrategyBatchService 可实例化 ✓")

    print("OK")
