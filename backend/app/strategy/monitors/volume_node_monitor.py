"""Volume Node Cluster 分钟监控插件（M2）。

从 ref/交易/selection/selection_node.py 迁移核心算法，重构为持续监控逻辑：
- selector 范式：按交易日选股（当日 [low, high] 触碰 Peak Node + 近期涨停）
- monitor 范式：按 1m bar 持续监控（当前价触碰 Node 边界 → 输出状态 + 事件）

调用 features/ 算法（严格不修改 features/）：
- compute_volume_profile: 计算 Volume Profile + Peak Node 检测
- extract_nearest_nodes: 提取参考价上方/下方最近 Peak Node（SSOT）
- VolumeProfileConfig: VP 配置

输入：MarketDataContext（bars_minute 含 1m OHLCV bars，min_bars=360）
输出：MonitorState（current_price/upper_node/lower_node/position_0_1/poc_node/last_touched_node）
      + StrategyEventDraft（node_cluster_touch 事件）

事件检测：
- node_cluster_touch: 价格触碰 Peak Node 边界
- dedupe=touch_episode: 同一触碰 episode 只触发一次（prev 无触碰或触碰不同 node 时触发）
- state_ttl=120s: 状态有效期 120 秒

对照 volume_node_monitor.yaml 字段定义：
- outputs: current_price(number), upper_node(json), lower_node(json),
           position_0_1(number, ratio_0_1), poc_node(json), last_touched_node(json)
- event_types: node_cluster_touch (dedupe=touch_episode, state_ttl_seconds=120)
- resource_budget: target_ms_per_instrument=500

用法（模块自测）：
    python -m app.strategy.monitors.volume_node_monitor
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import types
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd

from app.models.strategy import StrategyVersion
from app.strategy.runtime import (
    MarketDataContext,
    MonitorState,
    StrategyEventDraft,
    StrategyRuntime,
)

logger = logging.getLogger("strategy.monitors.volume_node_monitor")

# features/ 算法模块路径（可通过环境变量覆盖，默认指向 ref/交易/features）
FEATURES_DIR = os.environ.get(
    "FEATURES_DIR", "/root/web_dev/ref/交易/features"
)
_VP_MODULE_NAME = "luxalgo_volume_profile_pytdx_15m_aligned"
_VP_MODULE_PATH = os.path.join(FEATURES_DIR, f"{_VP_MODULE_NAME}.py")

# VP 标准参数（与 volume_node_monitor.yaml + selection_node.py 一致）
VP_LOOKBACK_DEFAULT = 360
VP_ROWS_DEFAULT = 100
VP_PEAKS_DETECTION_PERCENT = 0.05

# 事件参数（对照 volume_node_monitor.yaml event_types）
EVENT_TYPE_NODE_CLUSTER_TOUCH = "node_cluster_touch"
EVENT_STATE_TTL_SECONDS = 120


def _ensure_plotly_mock() -> None:
    """若 plotly 未安装，注入轻量 mock 到 sys.modules（仅用于满足 features 模块顶层 import）。

    features/ 模块顶层 `import plotly.graph_objects as go` 仅用于可视化函数
    （make_volume_profile_figure 等）。monitor 仅调用 compute_volume_profile
    与 extract_nearest_nodes，不依赖 plotly。注入 mock 避免引入重依赖，
    同时不修改 features/ 源码。
    """
    if "plotly" in sys.modules:
        return
    try:
        import plotly  # noqa: F401
        return
    except ImportError:
        pass
    # 构造 plotly + plotly.graph_objects mock
    plotly_mock = types.ModuleType("plotly")
    go_mock = types.ModuleType("plotly.graph_objects")
    # 提供最小占位属性（可视化函数不会被 monitor 调用）
    go_mock.Figure = type("Figure", (), {"__init__": lambda self, *a, **kw: None})
    go_mock.Candlestick = type("Candlestick", (), {"__init__": lambda self, *a, **kw: None})
    go_mock.Bar = type("Bar", (), {"__init__": lambda self, *a, **kw: None})
    go_mock.Layout = type("Layout", (), {"__init__": lambda self, *a, **kw: None})
    plotly_mock.graph_objects = go_mock
    sys.modules["plotly"] = plotly_mock
    sys.modules["plotly.graph_objects"] = go_mock
    logger.debug("已注入 plotly mock（features 可视化依赖，monitor 不使用）")


def _load_features_module() -> Any:
    """通过 importlib 从文件路径加载 features/ 算法模块（不修改 features/）。

    features/ 模块无内部相对导入（仅依赖 numpy/pandas/plotly），
    可独立加载。plotly 未安装时注入 mock（仅可视化用，monitor 不依赖）。
    路径由 FEATURES_DIR 环境变量控制。

    Returns:
        features 模块对象（含 compute_volume_profile/extract_nearest_nodes/VolumeProfileConfig）

    Raises:
        FileNotFoundError: features 模块文件不存在
        ImportError: 模块加载失败（补上下文后 re-raise）
    """
    if not os.path.exists(_VP_MODULE_PATH):
        raise FileNotFoundError(
            f"features 算法模块不存在: {_VP_MODULE_PATH}"
            f"（请设置 FEATURES_DIR 环境变量指向 ref/交易/features 目录）"
        )
    # plotly 未安装时注入 mock（features 顶层 import 需要）
    _ensure_plotly_mock()
    try:
        spec = importlib.util.spec_from_file_location(_VP_MODULE_NAME, _VP_MODULE_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法创建模块 spec: {_VP_MODULE_PATH}")
        module = importlib.util.module_from_spec(spec)
        # 必须在 exec_module 前注册到 sys.modules，否则 features 中的 @dataclass
        # 装饰器内部 _is_type 会通过 sys.modules.get(cls.__module__).__dict__ 查找
        # 模块字典，未注册时返回 None 导致 AttributeError
        sys.modules[_VP_MODULE_NAME] = module
        spec.loader.exec_module(module)
        return module
    except Exception as e:
        # 加载失败时清理 sys.modules 中的残留
        sys.modules.pop(_VP_MODULE_NAME, None)
        raise ImportError(
            f"features 算法模块加载失败: path={_VP_MODULE_PATH}, error={e}"
        ) from e


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
        self._vp_module: Any | None = None  # 懒加载
        self._strategy_version_id: UUID | None = None

    async def initialize(self, version: StrategyVersion) -> None:
        """从 manifest 提取参数并懒加载 features 模块。

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

        # 从 outputs 配置中提取 rows（如 manifest 声明）
        # 默认使用 VP_ROWS_DEFAULT=100（与 selection_node.py 一致）

        # 懒加载 features 模块（首次 initialize 时加载）
        if self._vp_module is None:
            self._vp_module = _load_features_module()
            logger.info(
                "VolumeNodeMonitor 初始化: lookback=%d, rows=%d, features=%s",
                self._lookback, self._rows, _VP_MODULE_PATH,
            )

    async def execute(self, context: MarketDataContext) -> Any:  # type: ignore[override]
        """selector 执行接口（monitor 不支持）。

        VolumeNodeMonitor 为 monitor kind，使用 calculate_state + detect_events，
        不支持 selector 的 execute 语义。调用时抛出 NotImplementedError。
        """
        raise NotImplementedError(
            "VolumeNodeMonitor 是 monitor 策略，不支持 execute（请使用 calculate_state + detect_events）"
        )

    def _compute_volume_profile(self, bars: pd.DataFrame) -> Any:
        """调用 features/ compute_volume_profile 计算 VP（向量化）。

        Args:
            bars: 1m OHLCV DataFrame（含 datetime 列）

        Returns:
            VolumeProfileResult 对象（含 peak_df/poc_price/lowest_price/highest_price 等）
        """
        cfg = self._vp_module.VolumeProfileConfig(
            peaks_show="peaks",
            profile_lookback_length=self._lookback,
            profile_number_of_rows=self._rows,
            peaks_detection_percent=VP_PEAKS_DETECTION_PERCENT,
        )
        # 不传 profile_df：1m bars 同时作为主数据与成交量分配来源
        # main_period 在 profile_df=None 时被忽略（_prepare_profile_bars 直接返回 main_window）
        return self._vp_module.compute_volume_profile(bars, cfg)

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

        输入 MarketDataContext.bars_minute（1m bars），输出 MonitorState。
        state 字典含 manifest.outputs 声明的所有字段：
        current_price/upper_node/lower_node/position_0_1/poc_node/last_touched_node

        Args:
            context: 市场数据上下文（bars_minute 含 1m OHLCV bars）

        Returns:
            当前 bar 的监控状态

        Raises:
            ValueError: bars_minute 为 None 或数据不足
        """
        if context.bars_minute is None or context.bars_minute.empty:
            raise ValueError(
                f"VolumeNodeMonitor 需要 1m bars 数据，instrument_id={context.instrument_id}"
            )
        if self._vp_module is None:
            raise RuntimeError("features 模块未加载，请先调用 initialize()")

        bars = context.bars_minute
        if len(bars) < 10:
            raise ValueError(
                f"1m bars 数据不足（需要至少 10 根，实际 {len(bars)}），"
                f"instrument_id={context.instrument_id}"
            )

        # 准备数据
        bars_prepared = _prepare_bars_for_vp(bars)

        # 计算 Volume Profile（调用 features/ 算法）
        try:
            vp_result = self._compute_volume_profile(bars_prepared)
        except Exception as e:
            raise RuntimeError(
                f"compute_volume_profile 失败 instrument_id={context.instrument_id}: {e}"
            ) from e

        # 当前价格（最后一根 bar 的收盘价）
        current_price = float(bars["close"].iloc[-1])

        # 上下方最近 Node（调用 SSOT extract_nearest_nodes）
        nearest_info = self._vp_module.extract_nearest_nodes(vp_result, current_price)
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

        # poc_node: POC 价格对应的 profile 行（json 结构）
        poc_node = self._lookup_poc_node(vp_result)

        # last_touched_node: 当前价触碰的 Peak Node（向量化检测）
        last_touched_node = self._find_touched_node(peak_df, current_price)

        # 组装 state 字典（对照 volume_node_monitor.yaml outputs）
        state: dict[str, Any] = {
            "current_price": round(current_price, 4),
            "upper_node": upper_node,
            "lower_node": lower_node,
            "position_0_1": position_0_1,
            "poc_node": poc_node,
            "last_touched_node": last_touched_node,
        }

        bar_time = context.bar_time or (
            bars.index[-1].to_pydatetime() if isinstance(bars.index, pd.DatetimeIndex)
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

    async def detect_events(
        self,
        context: MarketDataContext,
        prev_state: MonitorState | None,
        curr_state: MonitorState,
    ) -> list[StrategyEventDraft]:
        """检测 node_cluster_touch 事件（价格触碰 Node 边界）。

        触碰 episode 去重逻辑（dedupe=touch_episode）：
        - prev 无触碰（last_touched_node=None）且 curr 有触碰 → 新 episode，触发事件
        - prev 触碰 Node A 且 curr 触碰 Node B（不同 Node）→ 新 episode，触发事件
        - prev 触碰 Node A 且 curr 触碰同一 Node A → 同一 episode，不触发
        - prev 有触碰且 curr 无触碰 → episode 结束，不触发

        Args:
            context: 市场数据上下文
            prev_state: 前一状态（首个 bar 时为 None）
            curr_state: 当前状态

        Returns:
            事件草稿列表（触碰开始时返回 1 条，否则空列表）
        """
        curr_touched = curr_state.state.get("last_touched_node")
        prev_touched = prev_state.state.get("last_touched_node") if prev_state else None

        # 无新触碰 → 无事件
        if curr_touched is None:
            return []

        # 同一 episode（触碰同一 Node）→ 不触发
        if prev_touched is not None:
            prev_mid = prev_touched.get("price_mid")
            curr_mid = curr_touched.get("price_mid")
            if prev_mid is not None and curr_mid is not None and np.isclose(prev_mid, curr_mid, atol=1e-4):
                return []

        # 新 episode 开始 → 触发 node_cluster_touch 事件
        node_price_mid = curr_touched["price_mid"]
        bar_time = curr_state.updated_at or datetime.now(UTC)
        # bar_time 可能带时区，转 ISO 字符串用于 dedupe_key
        bar_time_iso = bar_time.isoformat() if isinstance(bar_time, datetime) else str(bar_time)

        instrument_id_str = str(curr_state.instrument_id)
        dedupe_key = f"{EVENT_TYPE_NODE_CLUSTER_TOUCH}:{instrument_id_str}:{node_price_mid}:{bar_time_iso}"
        logical_entity = f"{instrument_id_str}:{node_price_mid}"

        payload: dict[str, Any] = {
            "instrument_id": instrument_id_str,
            "node": curr_touched,
            "current_price": curr_state.state.get("current_price"),
            "position_0_1": curr_state.state.get("position_0_1"),
            "upper_node": curr_state.state.get("upper_node"),
            "lower_node": curr_state.state.get("lower_node"),
            "poc_node": curr_state.state.get("poc_node"),
            "bar_time": bar_time_iso,
        }

        return [
            StrategyEventDraft(
                event_type=EVENT_TYPE_NODE_CLUSTER_TOUCH,
                event_time=bar_time,
                dedupe_key=dedupe_key,
                logical_entity=logical_entity,
                payload=payload,
                state_ttl_seconds=EVENT_STATE_TTL_SECONDS,
            )
        ]


if __name__ == "__main__":
    # 自测入口：验证插件定义与 features 模块加载（无副作用，不写库表）
    print(f"VolumeNodeMonitor.kind={VolumeNodeMonitor.kind}")
    assert VolumeNodeMonitor.kind == "monitor"

    # 验证 features 模块可加载
    try:
        module = _load_features_module()
        print(f"features 模块加载成功: {module.__name__}")
        assert hasattr(module, "compute_volume_profile")
        assert hasattr(module, "extract_nearest_nodes")
        assert hasattr(module, "VolumeProfileConfig")
        print("compute_volume_profile/extract_nearest_nodes/VolumeProfileConfig 可用 ✓")
    except FileNotFoundError as e:
        print(f"features 模块不可用（跳过）: {e}")

    # 验证 ABC 继承
    from app.strategy.runtime import StrategyRuntime
    assert issubclass(VolumeNodeMonitor, StrategyRuntime)
    print("VolumeNodeMonitor 继承 StrategyRuntime ✓")
    print("OK")
