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
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pandas as pd

from app.constants.indicator_contract import (
    NODE_CLUSTER_EVENT_TTL_SECONDS,
    NODE_CLUSTER_PRIMARY_BARS,
)
from app.models.strategy import StrategyVersion

# [CHANGE-20260718-004 Node Cluster engine] VolumeNodeMonitor 三入口（compute_indicators /
# calculate_state / detect_events）改为调用 node_cluster_engine，不再直接调用底层
# compute_unified_volume_profile / prepare_node_cluster_bars。架构守护测试
# test_node_cluster_architecture 强制此约束。
from app.services.node_cluster_engine import (
    NodeClusterProfileResult,
    compute_node_cluster_profile,
    derive_state_for_price,
    detect_crossover_signals,
)
from app.strategy.runtime import (
    MarketDataContext,
    MonitorState,
    StrategyEventDraft,
    StrategyRuntime,
)

logger = logging.getLogger("strategy.monitors.volume_node_monitor")

# 事件参数（对照 watchlist_monitor.yaml event_types）
EVENT_TYPE_NODE_CLUSTER_TOUCH = "node_cluster_touch"
# [volume_node_monitor] - 描述: 事件状态 TTL，从 indicator_contract 唯一真源导入，禁止硬编码
EVENT_STATE_TTL_SECONDS = NODE_CLUSTER_EVENT_TTL_SECONDS
# VP_LOOKBACK 引用 indicator_contract 唯一真源（原从 unified_volume_profile 导入别名）
VP_LOOKBACK = NODE_CLUSTER_PRIMARY_BARS


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
        # [CHANGE-20260718-004] Profile 缓存：供 detect_events 复用 calculate_state 的计算结果，避免重复计算
        # 原缓存 UnifiedVolumeProfileResult，现缓存 NodeClusterProfileResult（engine 不可变结果）
        self._last_profile: NodeClusterProfileResult | None = None
        self._last_vp_calc_id: str | None = None

    async def initialize(self, version: StrategyVersion) -> None:
        """初始化监控器，绑定策略版本。

        Node Cluster 参数由 indicator_contract.py 唯一真源控制，禁止从 manifest 覆盖。

        Args:
            version: 策略版本 ORM 对象（manifest 含 parameters/outputs/event_types）
        """
        self._strategy_version_id = version.id
        # lookback 由 indicator_contract.VP_LOOKBACK 控制，不再从 manifest 读取
        logger.info(
            "VolumeNodeMonitor 初始化: lookback=%d (from indicator_contract)",
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

        [CHANGE-20260718-004] 调用 node_cluster_engine.compute_node_cluster_profile 计算最近
        lookback 根日线 bar 的 Volume Node（唯一业务入口，三链同核）。
        主数据为日线 bars（context.bars_daily），当 15m bars 可用时作为 profile_df
        供成交量分配（低周期分配），否则日线 bars 同时作为主数据和分配来源。

        VP 只计算一次（expensive），然后对每根日线 bar 的收盘价提取最近 Node 信息。
        本函数是 Volume Profile 图表的唯一真源（SSOT）：
        - profile_rows: 完整 100 行 VP 价格档位快照（非时间序列），供前端直接渲染多空量柱
        - profile_meta: VP 元信息（row_count/price_step/poc_price/vah_price/val_price +
          algorithm_version/contract_fingerprint/profile_hash 等诊断字段）
        - peak_rows: 当前 VP 的 peak 节点快照（非时间序列），供前端渲染多空量标签与迷你多空柱

        所有字段直接从 NodeClusterProfileResult 转换，不重新计算（复用 SSOT）。

        Returns:
            {"upper_node": [...], "lower_node": [...], "poc_price": [...],
             "position_0_1": [...], "current_price": [...],
             "profile_rows": [{price_low, price_high, price_mid, bullish_volume,
                               bearish_volume, total_volume, is_peak, is_poc,
                               is_value_area}, ...共 100 行],
             "profile_meta": {row_count, price_step, poc_price, vah_price, val_price,
                              algorithm_version, output_schema_version, contract_fingerprint,
                              daily_source_hash, bars_15m_source_hash, profile_hash, ...},
             "peak_rows": [{price_mid, bullish_volume, bearish_volume, total_volume, is_peak}, ...]}
        """
        # [CHANGE-20260718-004] 调用 engine 唯一入口计算 Node Cluster Profile
        try:
            profile = compute_node_cluster_profile(
                context.bars_daily, context.bars_15min,
            )
        except Exception as e:
            raise RuntimeError(
                f"compute_node_cluster_profile 失败 instrument_id={context.instrument_id}: {e}"
            ) from e

        if not profile.profile_rows:
            return {
                "upper_node": [], "lower_node": [], "poc_price": [],
                "position_0_1": [], "current_price": [], "peak_rows": [],
                "profile_rows": [], "profile_meta": {},
            }

        # 对每根日线 bar 收盘价提取 Node 信息（使用 engine derive_state_for_price）
        # 取与 engine 内部 prepared.daily 相同根数的 closes（最后 daily_bars_count 根）
        daily_closes = (
            context.bars_daily["close"].astype(float)
            if context.bars_daily is not None and not context.bars_daily.empty
            else pd.Series(dtype=float)
        )
        n_bars = profile.daily_bars_count
        if len(daily_closes) > n_bars:
            daily_closes = daily_closes.iloc[-n_bars:]

        upper_nodes: list[Any] = []
        lower_nodes: list[Any] = []
        poc_prices: list[float | None] = []
        positions: list[float | None] = []
        current_prices: list[float] = []

        poc_price = profile.poc_price

        for price in daily_closes:
            try:
                state = derive_state_for_price(profile, float(price))
                state_dict = state.to_dict()
                upper_nodes.append(state_dict["upper_node"])
                lower_nodes.append(state_dict["lower_node"])
                poc_prices.append(poc_price)
                positions.append(state_dict["position_0_1"])
                current_prices.append(state_dict["current_price"])
            except Exception:
                upper_nodes.append(None)
                lower_nodes.append(None)
                poc_prices.append(poc_price)
                positions.append(None)
                current_prices.append(float(price))

        # [volume_node_monitor] - profile_rows: 直接从 engine result 取（已序列化为 list[dict]）
        profile_rows_list = profile.profile_rows

        # [volume_node_monitor] - peak_rows: 直接从 engine result 取（含 VA 外 Peak，禁止过滤）
        peak_rows_list = profile.peak_rows

        # [volume_node_monitor] - profile_meta: VP 元信息 + engine 诊断字段
        profile_meta: dict[str, Any] = {
            "row_count": len(profile_rows_list),
            "price_step": profile.price_step,
            "poc_price": profile.poc_price,
            "vah_price": profile.vah_price,
            "val_price": profile.val_price,
            # [CHANGE-20260718-004] engine 诊断字段（三链一致性 + 缓存键 + 不可变标识）
            "algorithm_version": profile.algorithm_version,
            "output_schema_version": profile.output_schema_version,
            "contract_fingerprint": profile.contract_fingerprint,
            "daily_source_hash": profile.daily_source_hash,
            "bars_15m_source_hash": profile.bars_15m_source_hash,
            "profile_hash": profile.profile_hash,
            "daily_bars_count": profile.daily_bars_count,
            "bars_15m_count": profile.bars_15m_count,
            "adjustment_as_of": profile.adjustment_as_of,
            # 输入根数/周期（原 prepared.profile_meta 等价字段）
            "input_daily_bars": profile.daily_bars_count,
            "input_15m_bars": profile.bars_15m_count,
            "input_minute_bars": int(len(context.bars_minute)) if context.bars_minute is not None else 0,
            "primary_period": "1d",
            "low_period": "15m",
        }

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

        [CHANGE-20260718-004] 调用 node_cluster_engine.compute_node_cluster_profile 计算
        Volume Profile（日线主数据 + 15m profile_df，与 monitoring.py 盘中实时监控逻辑一致），
        再通过 derive_state_for_price 派生当前价格状态。1m bars 仍用于 crossover 事件检测的
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
        bars_daily = context.bars_daily

        if bars_daily is None or bars_daily.empty:
            raise ValueError(
                f"VolumeNodeMonitor 需要 daily bars 数据，instrument_id={context.instrument_id}"
            )

        if len(bars_daily) < 10:
            raise ValueError(
                f"daily bars 数据不足（需要至少 10 根，实际 {len(bars_daily)}），"
                f"instrument_id={context.instrument_id}"
            )

        # [CHANGE-20260718-004] 调用 engine 唯一入口计算 Node Cluster Profile
        try:
            profile = compute_node_cluster_profile(
                context.bars_daily, context.bars_15min,
            )
        except Exception as e:
            raise RuntimeError(
                f"compute_node_cluster_profile 失败 instrument_id={context.instrument_id}: {e}"
            ) from e

        # 缓存 Profile 结果供 detect_events 复用
        calc_id = f"{context.instrument_id}:{context.bar_time.isoformat() if context.bar_time else 'unknown'}"
        self._last_profile = profile
        self._last_vp_calc_id = calc_id

        # 当前价格：优先从 1m bars 取最后一根 bar 收盘价，否则从日线取
        if context.bars_minute is not None and not context.bars_minute.empty:
            current_price = float(context.bars_minute["close"].iloc[-1])
        else:
            current_price = float(bars_daily["close"].iloc[-1])

        # 通过 engine derive_state_for_price 派生状态字段
        state = derive_state_for_price(profile, current_price).to_dict()

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
        profile: NodeClusterProfileResult,
    ) -> list[dict[str, Any]]:
        """Crossover 检测：1m bar 收盘价穿越 peak_price 时触发（与 monitoring.py 一致）。

        [CHANGE-20260718-004] 委托 node_cluster_engine.detect_crossover_signals，
        公式零变化：取 1m bars 最后两根 bar 的 close（prev_close / cur_close），
        遍历 profile.all_peak_prices，检测价格穿越：
        (prev_close <= peak_price < cur_close) or (cur_close <= peak_price < prev_close)

        Args:
            bars_minute: 1m OHLCV DataFrame
            profile: NodeClusterProfileResult（含 all_peak_prices）

        Returns:
            信号列表，每项含 boundary/cluster_price/price/dev_pct
        """
        if bars_minute is None or len(bars_minute) < 2:
            return []

        prev_close = float(bars_minute.iloc[-2]["close"])
        cur_close = float(bars_minute.iloc[-1]["close"])

        # 委托 engine detect_crossover_signals（公式零变化）
        return detect_crossover_signals(profile, prev_close, cur_close)

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
            context: 市场数据上下文（需含 bars_minute + Profile 结果）
            prev_state: 前一状态（crossover 模式下不使用，保留接口兼容）
            curr_state: 当前状态（含 Profile 计算结果）

        Returns:
            事件草稿列表（每个穿越信号一条）
        """
        # 需要 1m bars 进行 crossover 检测
        if context.bars_minute is None or len(context.bars_minute) < 2:
            return []

        # [CHANGE-20260718-004] 优先从缓存获取 Profile 结果（与 calculate_state 共享，避免重复计算）
        calc_id = f"{context.instrument_id}:{context.bar_time.isoformat() if context.bar_time else 'unknown'}"
        if self._last_profile is not None and self._last_vp_calc_id == calc_id:
            profile = self._last_profile
        else:
            # 缓存未命中，重新计算
            bars_daily = context.bars_daily
            if bars_daily is None or len(bars_daily) < 10:
                return []

            try:
                profile = compute_node_cluster_profile(
                    context.bars_daily, context.bars_15min,
                )
            except Exception as e:
                raise RuntimeError(
                    f"compute_node_cluster_profile 失败（detect_events）"
                    f"instrument_id={context.instrument_id}: {e}"
                ) from e

        # Crossover 检测（委托 engine，公式零变化）
        signals = self._detect_node_crossover_signals(context.bars_minute, profile)
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

    # [CHANGE-20260718-004 Node Cluster engine] 验证 engine 函数已通过包内导入可用
    # （原 compute_unified_volume_profile / UnifiedVolumeProfileResult 已移除直接导入，
    # 现通过 node_cluster_engine 唯一业务入口访问底层 VP）
    assert callable(compute_node_cluster_profile)
    assert callable(derive_state_for_price)
    assert callable(detect_crossover_signals)
    assert NodeClusterProfileResult is not None
    # [advice.md 第四节] - VP_LOOKBACK 与 indicator_contract.NODE_CLUSTER_PRIMARY_BARS 对齐（250）
    assert VP_LOOKBACK == 250
    print("compute_node_cluster_profile/derive_state_for_price/detect_crossover_signals/NodeClusterProfileResult/VP_LOOKBACK 可用 ✓")

    # 验证 ABC 继承
    from app.strategy.runtime import StrategyRuntime
    assert issubclass(VolumeNodeMonitor, StrategyRuntime)
    print("VolumeNodeMonitor 继承 StrategyRuntime ✓")
    print("OK")
