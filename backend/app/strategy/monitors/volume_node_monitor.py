"""Volume Node Cluster 分钟监控插件（M2）。

从 ref/交易/selection/selection_node.py 迁移核心算法，重构为持续监控逻辑：
- selector 范式：按交易日选股（当日 [low, high] 触碰 Peak Node + 近期涨停）
- monitor 范式：按 1m bar 持续监控（crossover 穿越检测 → 输出状态 + 事件）

调用 features/ 算法（严格不修改 features/）：
- compute_unified_volume_profile: 统一 Volume Profile + Peak Node 检测（唯一真源）
- UnifiedVolumeProfileResult: 统一结果包装，提供 state_for_price 等查询方法

输入：MarketDataContext（bars_daily + bars_15min + bars_minute）
输出：MonitorState（current_price/upper_node/lower_node/position_0_1/poc_price/last_touched_node）
      + StrategyEventDraft（node_cluster_touch 事件）

VP 数据源（与 monitoring.py 一致）：
- 主数据：日线 bars（context.bars_daily）
- profile_df：15m bars（context.bars_15min，低周期成交量分配）
- main_period="day"

事件检测（crossover 穿越检测，与 monitoring.py detect_node_cluster_signals 一致）：
- node_cluster_touch: 1m bar 收盘价穿越 peak_price（prev_close/cur_close 跨越边界）
- dedupe_key: instrument+boundary+bar_time
- state_ttl=600s: 状态有效期 600 秒（与 monitoring.py NOTIFY_COOLDOWN_SECONDS 一致）

对照 volume_node_monitor.yaml 字段定义：
- outputs: current_price(number), upper_node(json), lower_node(json),
           position_0_1(number, ratio_0_1), poc_price(json), last_touched_node(json)
- event_types: node_cluster_touch (dedupe=touch_episode, state_ttl_seconds=600)
- resource_budget: target_ms_per_instrument=500

用法（模块自测）：
    python -m app.strategy.monitors.volume_node_monitor
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pandas as pd

from app.models.strategy import StrategyVersion
from app.strategy.runtime import (
    MarketDataContext,
    MonitorState,
    StrategyEventDraft,
    StrategyRuntime,
)
from app.strategy_assets.algorithms.features.unified_volume_profile import (
    NodeClusterBarsResult,
    UnifiedVolumeProfileResult,
    VP_LOOKBACK,
    compute_unified_volume_profile,
    prepare_node_cluster_bars,
)

logger = logging.getLogger("strategy.monitors.volume_node_monitor")

# 事件参数（对照 volume_node_monitor.yaml event_types）
EVENT_TYPE_NODE_CLUSTER_TOUCH = "node_cluster_touch"
EVENT_STATE_TTL_SECONDS = 600


class VolumeNodeMonitor(StrategyRuntime):
    """Volume Node Cluster 分钟监控策略（kind="monitor"）。

    按 1m bar 持续监控价格与 Volume Profile Peak Node 的触碰关系，
    输出当前状态（MonitorState）与触碰事件（StrategyEventDraft）。

    生命周期：
    1. StrategyLoader.load(version) 创建实例
    2. initialize(version) 从 manifest 提取参数
    3. calculate_state(context) 每个 bar 计算当前状态
    4. detect_events(context, prev, curr) 对比前后状态检测触碰事件
    """

    kind = "monitor"

    def __init__(self) -> None:
        self._lookback: int = VP_LOOKBACK
        self._strategy_version_id: UUID | None = None
        # VP 缓存：供 detect_events 复用 calculate_state 的计算结果，避免重复计算
        self._last_vp_result: UnifiedVolumeProfileResult | None = None
        self._last_vp_calc_id: str | None = None

    async def initialize(self, version: StrategyVersion) -> None:
        """从 manifest 提取参数。

        Args:
            version: 策略版本 ORM 对象（manifest 含 parameters/outputs/event_types）
        """
        self._strategy_version_id = version.id
        manifest = version.manifest

        # 提取 algorithm.lookback 参数
        for param in manifest.get("parameters", []):
            if param.get("key") == "algorithm.lookback":
                self._lookback = int(param.get("default", VP_LOOKBACK))
                break

        logger.info(
            "VolumeNodeMonitor 初始化: lookback=%d",
            self._lookback,
        )

    async def execute(self, context: MarketDataContext) -> Any:  # type: ignore[override]
        """selector 执行接口（monitor 不支持）。

        VolumeNodeMonitor 为 monitor kind，使用 calculate_state + detect_events，
        不支持 selector 的 execute 语义。调用时抛出 NotImplementedError。
        """
        raise NotImplementedError(
            "VolumeNodeMonitor 是 monitor 策略，不支持 execute（请使用 calculate_state + detect_events）"
        )

    async def compute_indicators(self, context: MarketDataContext) -> dict[str, Any]:
        """计算 Volume Profile + Node 图表指标（供个股详情页面使用）。

        复用 compute_unified_volume_profile 计算最近 lookback 根日线 bar 的 Volume Node。
        主数据为日线 bars（context.bars_daily），当 15m bars 可用时作为 profile_df
        供成交量分配（低周期分配），否则日线 bars 同时作为主数据和分配来源。

        VP 只计算一次（expensive），然后对每根日线 bar 的收盘价提取最近 Node 信息。
        本函数是 Volume Profile 图表的唯一真源（SSOT）：
        - profile_rows: 完整 100 行 VP 价格档位快照（非时间序列），供前端直接渲染多空量柱
        - profile_meta: VP 元信息（row_count/price_step/poc_price/vah_price/val_price）
        - peak_rows: 当前 VP 的 peak 节点快照（非时间序列），供前端渲染多空量标签与迷你多空柱

        所有字段直接从 UnifiedVolumeProfileResult 转换，不重新计算（复用 SSOT）。

        Returns:
            {"upper_node": [...], "lower_node": [...], "poc_price": [...],
             "position_0_1": [...], "current_price": [...],
             "profile_rows": [{price_low, price_high, price_mid, bullish_volume,
                               bearish_volume, total_volume, is_peak, is_poc,
                               is_value_area}, ...共 100 行],
             "profile_meta": {row_count, price_step, poc_price, vah_price, val_price},
             "peak_rows": [{price_mid, bullish_volume, bearish_volume, total_volume, is_peak}, ...]}
        """
        # [volume_node_monitor] - 描述: 调用 prepare_node_cluster_bars 统一准备数据
        # （DatetimeIndex 排序/去重/tail），与 MonitorBatchService/IndicatorService 共用唯一真源
        prepared: NodeClusterBarsResult = prepare_node_cluster_bars(
            context.bars_daily, context.bars_15min, context.bars_minute
        )
        # 主数据为准备后的日线 bars
        bars = prepared.daily
        if bars is None or len(bars) < 10:
            return {
                "upper_node": [], "lower_node": [], "poc_price": [],
                "position_0_1": [], "current_price": [], "peak_rows": [],
                "profile_rows": [], "profile_meta": {},
            }

        # 15m bars 作为 profile_df（低周期成交量分配来源，使用准备后的数据）
        profile_df = prepared.bars_15m if not prepared.bars_15m.empty else None

        try:
            vp_result = compute_unified_volume_profile(
                bars, profile_df=profile_df, main_period="day"
            )
        except Exception as e:
            raise RuntimeError(
                f"compute_unified_volume_profile 失败 instrument_id={context.instrument_id}: {e}"
            ) from e

        # 对每根日线 bar 收盘价提取 Node 信息
        close_series = bars["close"].astype(float)

        upper_nodes: list[Any] = []
        lower_nodes: list[Any] = []
        poc_prices: list[float | None] = []
        positions: list[float] = []
        current_prices: list[float] = []

        poc_price = vp_result.poc_node()

        for price in close_series:
            state = vp_result.state_for_price(float(price))
            upper_nodes.append(state["upper_node"])
            lower_nodes.append(state["lower_node"])
            poc_prices.append(poc_price)
            positions.append(state["position_0_1"])
            current_prices.append(state["current_price"])

        # [volume_node_monitor] - profile_rows: 完整 100 行 VP 价格档位快照（SSOT）
        # 直接从 vp_result.profile_df 转换，供前端渲染多空量柱，禁止前端重算
        profile_rows_list: list[dict[str, Any]] = []
        vp_profile_df = vp_result.profile_df
        if vp_profile_df is not None and not vp_profile_df.empty:
            for _, row in vp_profile_df.iterrows():
                profile_rows_list.append({
                    "price_low": round(float(row["price_low"]), 4),
                    "price_high": round(float(row["price_high"]), 4),
                    "price_mid": round(float(row["price_mid"]), 4),
                    "bullish_volume": float(row["bullish_volume"]),
                    "bearish_volume": float(row["bearish_volume"]),
                    "total_volume": float(row["total_volume"]),
                    "is_peak": bool(row["is_peak"]),
                    "is_poc": bool(row["is_poc"]),
                    "is_value_area": bool(row["is_value_area"]),
                })

        # [volume_node_monitor] - profile_meta: VP 元信息（行数/步长/POC/VAH/VAL）
        # NaN 转为 None 保证 JSON 可序列化
        def _finite_or_none(v: float) -> float | None:
            f = float(v)
            return f if math.isfinite(f) else None

        profile_meta: dict[str, Any] = {
            "row_count": len(profile_rows_list),
            "price_step": _finite_or_none(vp_result.price_step),
            "poc_price": _finite_or_none(vp_result.poc_price),
            "vah_price": _finite_or_none(vp_result.vah_price),
            "val_price": _finite_or_none(vp_result.val_price),
        }
        # [volume_node_monitor] - 描述: 合并 prepare_node_cluster_bars 诊断字段
        # （输入根数/周期/参数版本），供前端与日志排查数据完整性
        profile_meta.update(prepared.profile_meta)

        # [volume_node_monitor] - peak_rows: 当前 VP 的 peak 节点多空量快照
        # 供前端图表渲染 peak 节点价格标签 + 多空量标签 + 迷你多空柱
        peak_rows_list: list[dict[str, Any]] = []
        peak_df = vp_result.peak_rows
        if peak_df is not None and not peak_df.empty:
            for _, row in peak_df.iterrows():
                peak_rows_list.append({
                    "price_mid": round(float(row["price_mid"]), 4),
                    "bullish_volume": float(row["bullish_volume"]),
                    "bearish_volume": float(row["bearish_volume"]),
                    "total_volume": float(row["total_volume"]),
                    "is_peak": bool(row["is_peak"]),
                })

        return {
            "upper_node": upper_nodes,
            "lower_node": lower_nodes,
            "poc_price": poc_prices,
            "position_0_1": positions,
            "current_price": current_prices,
            "profile_rows": profile_rows_list,
            "profile_meta": profile_meta,
            "peak_rows": peak_rows_list,
        }

    async def calculate_state(self, context: MarketDataContext) -> MonitorState:
        """计算当前 bar 的监控状态。

        VP 数据源：日线 bars 作为主数据 + 15m bars 作为 profile_df（低周期成交量分配），
        与 monitoring.py 盘中实时监控逻辑一致。1m bars 仍用于 crossover 事件检测的
        prev_close/cur_close 取值。

        state 字典含 manifest.outputs 声明的所有字段：
        current_price/upper_node/lower_node/position_0_1/poc_price/last_touched_node

        Args:
            context: 市场数据上下文（bars_daily/bars_15min/bars_minute）

        Returns:
            当前 bar 的监控状态

        Raises:
            ValueError: bars_daily 为 None 或数据不足
        """
        # [volume_node_monitor] - 描述: 调用 prepare_node_cluster_bars 统一准备数据
        # 与 compute_indicators 共用同一组准备后数据，保证两入口 VP 计算一致
        prepared = prepare_node_cluster_bars(
            context.bars_daily, context.bars_15min, context.bars_minute
        )
        bars_daily = prepared.daily

        if bars_daily.empty:
            raise ValueError(
                f"VolumeNodeMonitor 需要 daily bars 数据，instrument_id={context.instrument_id}"
            )

        if len(bars_daily) < 10:
            raise ValueError(
                f"daily bars 数据不足（需要至少 10 根，实际 {len(bars_daily)}），"
                f"instrument_id={context.instrument_id}"
            )

        # 15m bars 作为 profile_df（使用准备后的数据）
        ltf_bars = prepared.bars_15m if not prepared.bars_15m.empty else None

        # 计算 Volume Profile（日线主数据 + 15m profile_df，与 monitoring.py 一致）
        try:
            vp_result = compute_unified_volume_profile(
                bars_daily, profile_df=ltf_bars, main_period="day"
            )
        except Exception as e:
            raise RuntimeError(
                f"compute_unified_volume_profile 失败 instrument_id={context.instrument_id}: {e}"
            ) from e

        # 缓存 VP 结果供 detect_events 复用
        calc_id = f"{context.instrument_id}:{context.bar_time.isoformat() if context.bar_time else 'unknown'}"
        self._last_vp_result = vp_result
        self._last_vp_calc_id = calc_id

        # 当前价格：优先从 1m bars 取最后一根 bar 收盘价，否则从日线取
        if context.bars_minute is not None and not context.bars_minute.empty:
            current_price = float(context.bars_minute["close"].iloc[-1])
        else:
            current_price = float(bars_daily["close"].iloc[-1])

        # 通过统一结果对象计算状态字段
        state = vp_result.state_for_price(current_price)

        bar_time = context.bar_time or (
            bars_daily.index[-1].to_pydatetime()
            if isinstance(bars_daily.index, pd.DatetimeIndex)
            else datetime.now(UTC)
        )

        return MonitorState(
            instrument_id=context.instrument_id,
            strategy_version_id=self._strategy_version_id,  # type: ignore[arg-type]
            state=state,
            state_version=1,
            updated_at=bar_time,
        )

    def _detect_node_crossover_signals(
        self,
        bars_minute: pd.DataFrame,
        vp_result: UnifiedVolumeProfileResult,
    ) -> list[dict[str, Any]]:
        """Crossover 检测：1m bar 收盘价穿越 peak_price 时触发（与 monitoring.py 一致）。

        逻辑：取 1m bars 最后两根 bar 的 close（prev_close / cur_close），
        遍历 vp_result.all_peak_prices，检测价格穿越：
        (prev_close <= peak_price < cur_close) or (cur_close <= peak_price < prev_close)

        Args:
            bars_minute: 1m OHLCV DataFrame
            vp_result: UnifiedVolumeProfileResult（含 all_peak_prices）

        Returns:
            信号列表，每项含 boundary/cluster_price/price/dev_pct
        """
        cluster_prices = vp_result.all_peak_prices
        if not cluster_prices:
            return []

        if bars_minute is None or len(bars_minute) < 2:
            return []

        prev_close = float(bars_minute.iloc[-2]["close"])
        cur_close = float(bars_minute.iloc[-1]["close"])

        signals: list[dict[str, Any]] = []
        for cp in cluster_prices:
            peak_cross = (prev_close <= cp < cur_close) or (cur_close <= cp < prev_close)
            if peak_cross:
                dev_pct = (cur_close - cp) / cp * 100 if cp != 0 else 0.0
                signals.append({
                    "trigger_type": EVENT_TYPE_NODE_CLUSTER_TOUCH,
                    "price": cur_close,
                    "cluster_price": cp,
                    "boundary": cp,
                    "dev_pct": round(dev_pct, 4),
                })

        return signals

    async def detect_events(
        self,
        context: MarketDataContext,
        prev_state: MonitorState | None,
        curr_state: MonitorState,
    ) -> list[StrategyEventDraft]:
        """检测 node_cluster_touch 事件（crossover 穿越检测，与 monitoring.py 一致）。

        使用 _detect_node_crossover_signals() 检测 1m bar 收盘价穿越 peak_price，
        不依赖 prev/curr state 的 last_touched_node 对比。
        每个 crossover 信号生成一条事件，dedupe_key 按 instrument+boundary+bar_time 去重。

        Args:
            context: 市场数据上下文（需含 bars_minute + VP 结果）
            prev_state: 前一状态（crossover 模式下不使用，保留接口兼容）
            curr_state: 当前状态（含 VP 计算结果）

        Returns:
            事件草稿列表（每个穿越信号一条）
        """
        # 需要 1m bars 进行 crossover 检测
        if context.bars_minute is None or len(context.bars_minute) < 2:
            return []

        # 优先从缓存获取 VP 结果（与 calculate_state 共享，避免重复计算）
        calc_id = f"{context.instrument_id}:{context.bar_time.isoformat() if context.bar_time else 'unknown'}"
        if self._last_vp_result is not None and self._last_vp_calc_id == calc_id:
            vp_result = self._last_vp_result
        else:
            # 缓存未命中，重新计算
            bars_daily = context.bars_daily
            if bars_daily is None or len(bars_daily) < 10:
                return []

            ltf_bars = (
                context.bars_15min
                if context.bars_15min is not None and not context.bars_15min.empty
                else None
            )
            try:
                vp_result = compute_unified_volume_profile(
                    bars_daily, profile_df=ltf_bars, main_period="day"
                )
            except Exception as e:
                raise RuntimeError(
                    f"compute_unified_volume_profile 失败（detect_events）"
                    f"instrument_id={context.instrument_id}: {e}"
                ) from e

        # Crossover 检测
        signals = self._detect_node_crossover_signals(context.bars_minute, vp_result)
        if not signals:
            return []

        bar_time = curr_state.updated_at or datetime.now(UTC)
        # [volume_node_monitor] - dedupe_key 使用整分钟时间戳（而非微秒精度），
        # 同一 1m bar 内多次调用不会产生不同 dedupe_key
        bar_time_key = bar_time.strftime("%Y%m%d%H%M") if isinstance(bar_time, datetime) else str(bar_time)
        instrument_id_str = str(curr_state.instrument_id)

        events: list[StrategyEventDraft] = []
        for sig in signals:
            boundary = sig["boundary"]
            dedupe_key = f"{EVENT_TYPE_NODE_CLUSTER_TOUCH}:{instrument_id_str}:{boundary}:{bar_time_key}"
            logical_entity = f"{instrument_id_str}:{boundary}"

            payload: dict[str, Any] = {
                "instrument_id": instrument_id_str,
                "boundary": boundary,
                "cluster_price": sig["cluster_price"],
                "current_price": sig["price"],
                "dev_pct": sig["dev_pct"],
                "position_0_1": curr_state.state.get("position_0_1"),
                "upper_node": curr_state.state.get("upper_node"),
                "lower_node": curr_state.state.get("lower_node"),
                "poc_price": curr_state.state.get("poc_price"),
                "bar_time": bar_time_key,
            }

            events.append(
                StrategyEventDraft(
                    event_type=EVENT_TYPE_NODE_CLUSTER_TOUCH,
                    event_time=bar_time,
                    dedupe_key=dedupe_key,
                    logical_entity=logical_entity,
                    payload=payload,
                    state_ttl_seconds=EVENT_STATE_TTL_SECONDS,
                )
            )

        return events


if __name__ == "__main__":
    # 自测入口：验证插件定义与共享模块导入（无副作用，不写库表）
    print(f"VolumeNodeMonitor.kind={VolumeNodeMonitor.kind}")
    assert VolumeNodeMonitor.kind == "monitor"

    # 验证共享模块已通过包内导入可用
    assert callable(compute_unified_volume_profile)
    assert UnifiedVolumeProfileResult is not None
    # [advice.md 第四节] - VP_LOOKBACK 与 indicator_contract.NODE_CLUSTER_PRIMARY_BARS 对齐（250）
    assert VP_LOOKBACK == 250
    print("compute_unified_volume_profile/UnifiedVolumeProfileResult/VP_LOOKBACK 可用 ✓")

    # 验证 ABC 继承
    from app.strategy.runtime import StrategyRuntime
    assert issubclass(VolumeNodeMonitor, StrategyRuntime)
    print("VolumeNodeMonitor 继承 StrategyRuntime ✓")
    print("OK")