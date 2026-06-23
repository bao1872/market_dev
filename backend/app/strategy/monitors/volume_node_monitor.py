"""Volume Node Cluster 分钟监控插件（M2）。

从 ref/交易/selection/selection_node.py 迁移核心算法，重构为持续监控逻辑：
- selector 范式：按交易日选股（当日 [low, high] 触碰 Peak Node + 近期涨停）
- monitor 范式：按 1m bar 持续监控（crossover 穿越检测 → 输出状态 + 事件）

调用 features/ 算法（严格不修改 features/）：
- compute_volume_profile: 计算 Volume Profile + Peak Node 检测
- extract_nearest_nodes: 提取参考价上方/下方最近 Peak Node（SSOT）
- VolumeProfileConfig: VP 配置

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
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd

from app.models.strategy import StrategyVersion
from app.strategy._plotly_mock import ensure_plotly_mock
from app.strategy.runtime import (
    MarketDataContext,
    MonitorState,
    StrategyEventDraft,
    StrategyRuntime,
)

logger = logging.getLogger("strategy.monitors.volume_node_monitor")

# 导入 features/ 算法（从包内 app.strategy_assets.algorithms.features，Docker 兼容）
ensure_plotly_mock()
from app.strategy_assets.algorithms.features.luxalgo_volume_profile_pytdx_15m_aligned import (
    VolumeProfileConfig,
    compute_volume_profile,
    extract_nearest_nodes,
)

# VP 标准参数（与 monitoring.py 盘中实时监控逻辑一致，VP_PEAK_DETECTION_PCT=0.05）
VP_LOOKBACK_DEFAULT = 360
VP_ROWS_DEFAULT = 100
VP_PEAKS_DETECTION_PERCENT = 0.05
VP_VALUE_AREA_THRESHOLD = 0.70
VP_TROUGHS_SHOW = "none"
VP_TROUGHS_DETECTION_PERCENT = 0.07
VP_VOLUME_NODE_THRESHOLD = 0.01
VP_HIGHEST_N_NODES = 0
VP_LOWEST_N_NODES = 0

# 事件参数（对照 volume_node_monitor.yaml event_types）
EVENT_TYPE_NODE_CLUSTER_TOUCH = "node_cluster_touch"
EVENT_STATE_TTL_SECONDS = 600


def _node_row_to_json(row: pd.Series | None) -> dict[str, float] | None:
    """将 peak_df 行转换为 json 结构（price_mid/price_low/price_high）。

    Args:
        row: peak_df 的一行（含 price_mid/price_low/price_high），或 None

    Returns:
        {"price_mid": ..., "price_low": ..., "price_high": ...} 或 None
    """
    if row is None:
        return None
    return {
        "price_mid": round(float(row["price_mid"]), 4),
        "price_low": round(float(row["price_low"]), 4),
        "price_high": round(float(row["price_high"]), 4),
    }


def _prepare_bars_for_vp(bars: pd.DataFrame) -> pd.DataFrame:
    """准备 1m bars DataFrame 供 compute_volume_profile 使用。

    compute_volume_profile 内部调用 _normalize_columns，要求含 open/high/low/close/volume
    列及 datetime 列（或可由 index 重置得到）。

    Args:
        bars: 1m OHLCV DataFrame，DatetimeIndex + open/high/low/close/volume 列

    Returns:
        含 datetime 列的 OHLCV DataFrame（副本）
    """
    df = bars.copy()
    # 确保 datetime 列存在（_normalize_columns 依赖）
    if "datetime" not in df.columns:
        if isinstance(df.index, pd.DatetimeIndex):
            df["datetime"] = df.index
        else:
            df["datetime"] = pd.to_datetime(df.index, errors="coerce")
    return df


class VolumeNodeMonitor(StrategyRuntime):
    """Volume Node Cluster 分钟监控策略（kind="monitor"）。

    按 1m bar 持续监控价格与 Volume Profile Peak Node 的触碰关系，
    输出当前状态（MonitorState）与触碰事件（StrategyEventDraft）。

    生命周期：
    1. StrategyLoader.load(version) 创建实例
    2. initialize(version) 从 manifest 提取参数 + 懒加载 features 模块
    3. calculate_state(context) 每个 bar 计算当前状态
    4. detect_events(context, prev, curr) 对比前后状态检测触碰事件
    """

    kind = "monitor"

    def __init__(self) -> None:
        self._lookback: int = VP_LOOKBACK_DEFAULT
        self._rows: int = VP_ROWS_DEFAULT
        self._strategy_version_id: UUID | None = None
        # VP 缓存：供 detect_events 复用 calculate_state 的计算结果，避免重复计算
        self._last_vp_result: Any | None = None
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
                self._lookback = int(param.get("default", VP_LOOKBACK_DEFAULT))
                break

        logger.info(
            "VolumeNodeMonitor 初始化: lookback=%d, rows=%d",
            self._lookback, self._rows,
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

        复用现有 _compute_volume_profile 逻辑，计算最近 N 根日线 bar 的 Volume Node。
        主数据为日线 bars（context.bars_daily），当 15m bars 可用时作为 profile_df
        供成交量分配（低周期分配），否则日线 bars 同时作为主数据和分配来源。

        VP 只计算一次（expensive），然后对每根日线 bar 的收盘价提取最近 Node 信息。
        说明：extract_nearest_nodes 是 features/ SSOT 函数，接收单个价格，
        无法向量化（禁止修改 features/）。bar 数量受 lookback 限制（≤360），
        图表展示场景性能可接受。

        Returns:
            {"upper_node": [...], "lower_node": [...], "poc_price": [...],
             "position_0_1": [...], "current_price": [...]}
        """
        # 主数据为日线 bars
        bars = context.bars_daily
        if bars is None or len(bars) < 10:
            return {"upper_node": [], "lower_node": [], "poc_price": [],
                    "position_0_1": [], "current_price": []}

        # 准备数据并计算 VP（复用现有 _compute_volume_profile，VP 只计算一次）
        bars_prepared = _prepare_bars_for_vp(bars)
        # 15m bars 作为 profile_df（低周期成交量分配来源）
        profile_df = _prepare_bars_for_vp(context.bars_15min) if context.bars_15min is not None and not context.bars_15min.empty else None
        try:
            vp_result = self._compute_volume_profile(bars_prepared, profile_df=profile_df, main_period="day")
        except Exception as e:
            raise RuntimeError(
                f"compute_volume_profile 失败 instrument_id={context.instrument_id}: {e}"
            ) from e

        # VP 价格范围（用于 position_0_1）
        lowest_price = float(vp_result.lowest_price)
        highest_price = float(vp_result.highest_price)
        price_range = highest_price - lowest_price
        poc_price = float(vp_result.poc_price) if pd.notna(vp_result.poc_price) else None

        peak_df = vp_result.peak_df

        # 对每根日线 bar 提取 Node 信息（基于该 bar 的收盘价）
        close_series = bars["close"].astype(float)

        upper_nodes: list[Any] = []
        lower_nodes: list[Any] = []
        poc_prices: list[float | None] = []
        positions: list[float] = []
        current_prices: list[float] = []

        for price in close_series:
            # 调用 SSOT extract_nearest_nodes 获取上下方最近 Node 价格
            nearest_info = extract_nearest_nodes(vp_result, float(price))
            upper_node = self._lookup_node_by_price(
                peak_df, nearest_info["nearest_above_node_price"]
            )
            lower_node = self._lookup_node_by_price(
                peak_df, nearest_info["nearest_below_node_price"]
            )

            # position_0_1: 当前价在 VP 价格范围中的相对位置 [0, 1]
            if price_range > 0:
                pos = round(
                    float(np.clip((float(price) - lowest_price) / price_range, 0.0, 1.0)), 4
                )
            else:
                pos = 0.5

            upper_nodes.append(upper_node)
            lower_nodes.append(lower_node)
            poc_prices.append(poc_price)
            positions.append(pos)
            current_prices.append(round(float(price), 4))

        return {
            "upper_node": upper_nodes,
            "lower_node": lower_nodes,
            "poc_price": poc_prices,
            "position_0_1": positions,
            "current_price": current_prices,
        }

    def _compute_volume_profile(
        self,
        bars: pd.DataFrame,
        profile_df: pd.DataFrame | None = None,
        main_period: str = "1m",
    ) -> Any:
        """调用 features/ compute_volume_profile 计算 VP（向量化）。

        Args:
            bars: 主数据 OHLCV DataFrame（含 datetime 列）
            profile_df: 低周期 bars DataFrame（供成交量分配），或 None
            main_period: 主数据周期（"day"/"1m" 等）

        Returns:
            VolumeProfileResult 对象（含 peak_df/poc_price/lowest_price/highest_price 等）
        """
        cfg = VolumeProfileConfig(
            peaks_show="peaks",
            profile_lookback_length=self._lookback,
            profile_number_of_rows=self._rows,
            peaks_detection_percent=VP_PEAKS_DETECTION_PERCENT,
            value_area_threshold=VP_VALUE_AREA_THRESHOLD,
            troughs_show=VP_TROUGHS_SHOW,
            troughs_detection_percent=VP_TROUGHS_DETECTION_PERCENT,
            volume_node_threshold=VP_VOLUME_NODE_THRESHOLD,
            highest_n_volume_nodes=VP_HIGHEST_N_NODES,
            lowest_n_volume_nodes=VP_LOWEST_N_NODES,
        )
        return compute_volume_profile(
            bars, cfg, profile_df=profile_df, main_period=main_period
        )

    def _find_touched_node(self, peak_df: pd.DataFrame | None, current_price: float) -> dict[str, float] | None:
        """向量化检测当前价触碰的 Peak Node（价格落在 [price_low, price_high] 内）。

        若触碰多个 Node，选择 price_mid 最接近 current_price 的那个。

        Args:
            peak_df: VolumeProfileResult.peak_df（已过滤为 peaks），或 None
            current_price: 当前价格

        Returns:
            触碰 Node 的 json 结构，或 None
        """
        if peak_df is None or peak_df.empty:
            return None
        # 向量化：价格落在 Node 价格区间内
        mask = (peak_df["price_low"] <= current_price) & (peak_df["price_high"] >= current_price)
        touched = peak_df[mask]
        if touched.empty:
            return None
        # 选择 price_mid 最接近 current_price 的 Node
        idx = (touched["price_mid"] - current_price).abs().idxmin()
        return _node_row_to_json(touched.loc[idx])

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
        if context.bars_daily is None or context.bars_daily.empty:
            raise ValueError(
                f"VolumeNodeMonitor 需要 daily bars 数据，instrument_id={context.instrument_id}"
            )

        bars_daily = context.bars_daily
        if len(bars_daily) < 10:
            raise ValueError(
                f"daily bars 数据不足（需要至少 10 根，实际 {len(bars_daily)}），"
                f"instrument_id={context.instrument_id}"
            )

        # 准备数据：日线为主数据，15m 为 profile_df
        daily_bars_prepared = _prepare_bars_for_vp(bars_daily)
        ltf_bars_prepared = (
            _prepare_bars_for_vp(context.bars_15min)
            if context.bars_15min is not None and not context.bars_15min.empty
            else None
        )

        # 计算 Volume Profile（日线主数据 + 15m profile_df，与 monitoring.py 一致）
        try:
            vp_result = self._compute_volume_profile(
                daily_bars_prepared, profile_df=ltf_bars_prepared, main_period="day"
            )
        except Exception as e:
            raise RuntimeError(
                f"compute_volume_profile 失败 instrument_id={context.instrument_id}: {e}"
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

        # 上下方最近 Node（调用 SSOT extract_nearest_nodes）
        nearest_info = extract_nearest_nodes(vp_result, current_price)
        nearest_above_price = nearest_info["nearest_above_node_price"]
        nearest_below_price = nearest_info["nearest_below_node_price"]

        # 从 peak_df 查找完整 Node 信息（price_low/price_high）用于 json 结构
        peak_df = vp_result.peak_df
        upper_node = self._lookup_node_by_price(peak_df, nearest_above_price)
        lower_node = self._lookup_node_by_price(peak_df, nearest_below_price)

        # position_0_1: 当前价在 VP 价格范围中的相对位置 [0, 1]
        lowest_price = float(vp_result.lowest_price)
        highest_price = float(vp_result.highest_price)
        price_range = highest_price - lowest_price
        if price_range > 0:
            position_0_1 = round(
                float(np.clip((current_price - lowest_price) / price_range, 0.0, 1.0)), 4
            )
        else:
            position_0_1 = 0.5

        # poc_price: POC 价格对应的 profile 行（json 结构）
        poc_price = self._lookup_poc_node(vp_result)

        # last_touched_node: 当前价触碰的 Peak Node（向量化检测）
        last_touched_node = self._find_touched_node(peak_df, current_price)

        # 组装 state 字典（对照 volume_node_monitor.yaml outputs）
        state: dict[str, Any] = {
            "current_price": round(current_price, 4),
            "upper_node": upper_node,
            "lower_node": lower_node,
            "position_0_1": position_0_1,
            "poc_price": poc_price,
            "last_touched_node": last_touched_node,
        }

        bar_time = context.bar_time or (
            bars_daily.index[-1].to_pydatetime() if isinstance(bars_daily.index, pd.DatetimeIndex)
            else datetime.now(UTC)
        )

        return MonitorState(
            instrument_id=context.instrument_id,
            strategy_version_id=self._strategy_version_id,  # type: ignore[arg-type]
            state=state,
            state_version=1,
            updated_at=bar_time,
        )

    def _lookup_node_by_price(
        self, peak_df: pd.DataFrame | None, price: float | None
    ) -> dict[str, float] | None:
        """从 peak_df 按 price_mid 查找完整 Node 信息（json 结构）。

        Args:
            peak_df: VolumeProfileResult.peak_df，或 None
            price: Node 的 price_mid（由 extract_nearest_nodes 返回），或 None

        Returns:
            Node json 结构，或 None
        """
        if price is None or peak_df is None or peak_df.empty:
            return None
        # 向量化查找 price_mid 匹配的行
        mask = np.isclose(peak_df["price_mid"].to_numpy(dtype=float), float(price), atol=1e-4)
        matched = peak_df[mask]
        if matched.empty:
            return None
        return _node_row_to_json(matched.iloc[0])

    def _lookup_poc_node(self, vp_result: Any) -> dict[str, float] | None:
        """从 VP 结果中查找 POC 对应的 profile 行（json 结构）。

        POC 是成交量最大的价格行（is_poc=True）。

        Args:
            vp_result: VolumeProfileResult

        Returns:
            POC Node json 结构 {price_mid, price_low, price_high}，或 None
        """
        profile_df = vp_result.profile_df
        if profile_df is None or profile_df.empty:
            return None
        poc_rows = profile_df[profile_df["is_poc"]]
        if poc_rows.empty:
            return None
        row = poc_rows.iloc[0]
        return {
            "price_mid": round(float(row["price_mid"]), 4),
            "price_low": round(float(row["price_low"]), 4),
            "price_high": round(float(row["price_high"]), 4),
        }

    def _detect_node_crossover_signals(
        self,
        bars_minute: pd.DataFrame,
        vp_result: Any,
    ) -> list[dict[str, Any]]:
        """Crossover 检测：1m bar 收盘价穿越 peak_price 时触发（与 monitoring.py 一致）。

        逻辑：取 1m bars 最后两根 bar 的 close（prev_close / cur_close），
        遍历 vp_result.all_peak_prices，检测价格穿越：
        (prev_close <= peak_price < cur_close) or (cur_close <= peak_price < prev_close)

        Args:
            bars_minute: 1m OHLCV DataFrame
            vp_result: VolumeProfileResult（含 all_peak_prices）

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
        不再依赖 prev/curr state 的 last_touched_node 对比。
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

        # 从 curr_state 中获取 vp_result（由 calculate_state 缓存到 context）
        # 由于 detect_events 无法直接访问 vp_result，需要重新计算

        # 优先从缓存获取 VP 结果（与 calculate_state 共享，避免重复计算）
        calc_id = f"{context.instrument_id}:{context.bar_time.isoformat() if context.bar_time else 'unknown'}"
        if self._last_vp_result is not None and self._last_vp_calc_id == calc_id:
            vp_result = self._last_vp_result
        else:
            # 缓存未命中，重新计算
            bars_daily = context.bars_daily
            if bars_daily is None or len(bars_daily) < 10:
                return []

            daily_bars_prepared = _prepare_bars_for_vp(bars_daily)
            ltf_bars_prepared = (
                _prepare_bars_for_vp(context.bars_15min)
                if context.bars_15min is not None and not context.bars_15min.empty
                else None
            )
            try:
                vp_result = self._compute_volume_profile(
                    daily_bars_prepared, profile_df=ltf_bars_prepared, main_period="day"
                )
            except Exception:
                return []

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
    # 自测入口：验证插件定义与 features 模块导入（无副作用，不写库表）
    print(f"VolumeNodeMonitor.kind={VolumeNodeMonitor.kind}")
    assert VolumeNodeMonitor.kind == "monitor"

    # 验证 features 模块已通过包内导入可用
    assert callable(compute_volume_profile)
    assert callable(extract_nearest_nodes)
    assert VolumeProfileConfig is not None
    print("compute_volume_profile/extract_nearest_nodes/VolumeProfileConfig 可用 ✓")

    # 验证 ABC 继承
    from app.strategy.runtime import StrategyRuntime
    assert issubclass(VolumeNodeMonitor, StrategyRuntime)
    print("VolumeNodeMonitor 继承 StrategyRuntime ✓")
    print("OK")
