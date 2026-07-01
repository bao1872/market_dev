"""统一 Volume Profile 计算模块（监控/图表/通知唯一真源）。

封装 ref/交易/app/monitoring.py 中 compute_volume_profile 的调用参数，
为后端所有业务链路提供单一入口。

参数固定为（与 monitoring.py 盘中实时监控逻辑一致）：
- VP_LOOKBACK=250
- VP_ROWS=100
- VP_VALUE_AREA_PCT=0.70
- VP_PEAK_DETECTION_PCT=0.05
- VP_NODE_THRESHOLD_PCT=0.01

用法：
    from app.strategy_assets.algorithms.features.unified_volume_profile import (
        UnifiedVolumeProfileResult,
        compute_unified_volume_profile,
    )
    result = compute_unified_volume_profile(
        bars_daily, profile_df=bars_15min, main_period="day"
    )
    state = result.state_for_price(current_price)

模块自测：
    python -m app.strategy_assets.algorithms.features.unified_volume_profile
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.constants.indicator_contract import (
    NODE_CLUSTER_LOW_BARS,
    NODE_CLUSTER_LOW_PERIOD,
    NODE_CLUSTER_MINUTE_BARS,
    NODE_CLUSTER_PRIMARY_BARS,
    NODE_CLUSTER_PRIMARY_PERIOD,
    VP_HIGHEST_N_NODES,
    VP_LOWEST_N_NODES,
    VP_NODE_THRESHOLD_PCT,
    VP_PEAK_DETECTION_PCT,
    VP_ROWS,
    VP_TROUGHS_DETECTION_PCT,
    VP_TROUGHS_SHOW,
    VP_VALUE_AREA_PCT,
)
from app.constants.indicator_contract import (
    NODE_CLUSTER_PRIMARY_BARS as VP_LOOKBACK,
)
from app.strategy_assets.algorithms.features.luxalgo_volume_profile_pytdx_15m_aligned import (
    VolumeProfileConfig,
    VolumeProfileResult,
    compute_volume_profile,
    extract_nearest_nodes,
)

# Volume Profile 标准参数（来自基线 indicator_contract，禁止本地硬编码第二套）


@dataclass
class UnifiedVolumeProfileResult:
    """统一 Volume Profile 计算结果包装。

    封装底层 LuxAlgo VolumeProfileResult，提供业务常用字段和按价格查询状态的方法。
    所有 node/peak 相关计算复用 features/ SSOT 函数，禁止在模块外部重复实现。
    """

    raw: VolumeProfileResult

    @property
    def poc_price(self) -> float:
        """POC（控制点）价格。"""
        return float(self.raw.poc_price) if pd.notna(self.raw.poc_price) else float("nan")

    @property
    def vah_price(self) -> float:
        """价值区域高位（Value Area High）价格。"""
        return float(self.raw.vah_price) if pd.notna(self.raw.vah_price) else float("nan")

    @property
    def val_price(self) -> float:
        """价值区域低位（Value Area Low）价格。"""
        return float(self.raw.val_price) if pd.notna(self.raw.val_price) else float("nan")

    @property
    def peak_rows(self) -> pd.DataFrame:
        """Peak Node 行（含 price_mid/bullish_volume/bearish_volume/total_volume/is_peak）。"""
        return self.raw.peak_df if self.raw.peak_df is not None else pd.DataFrame()

    @property
    def peak_df(self) -> pd.DataFrame | None:
        """peak_df 别名（直接暴露 raw.peak_df，保持与 VolumeProfileResult 接口兼容）。

        供 monitor_chart_renderer.render_monitoring_chart 等历史调用方直接访问，
        避免在共享模块外重复实现 peak 行的提取逻辑。
        """
        return self.raw.peak_df

    @property
    def all_peak_prices(self) -> list[float]:
        """所有 Peak Node 的 price_mid 列表（去重排序）。"""
        return self.raw.all_peak_prices

    @property
    def lowest_price(self) -> float:
        """VP 价格范围最低价。"""
        return float(self.raw.lowest_price)

    @property
    def highest_price(self) -> float:
        """VP 价格范围最高价。"""
        return float(self.raw.highest_price)

    @property
    def price_step(self) -> float:
        """VP 价格档位步长。"""
        return float(self.raw.price_step)

    @property
    def profile_df(self) -> pd.DataFrame:
        """完整 profile 行（含所有价格档位成交量与 bullish/bearish 拆分）。"""
        return self.raw.profile_df

    def position_0_1(self, current_price: float) -> float:
        """计算当前价在 VP 价格范围中的相对位置 [0, 1]。

        Args:
            current_price: 当前价格

        Returns:
            [0, 1] 之间的位置比例
        """
        price_range = self.highest_price - self.lowest_price
        if price_range > 0:
            return round(
                float(np.clip((float(current_price) - self.lowest_price) / price_range, 0.0, 1.0)),
                4,
            )
        return 0.5

    @staticmethod
    def _node_row_to_json(row: pd.Series | None) -> dict[str, float] | None:
        """将 peak/profile 行转换为标准 JSON 结构（price_mid/price_low/price_high）。"""
        if row is None:
            return None
        return {
            "price_mid": round(float(row["price_mid"]), 4),
            "price_low": round(float(row["price_low"]), 4),
            "price_high": round(float(row["price_high"]), 4),
        }

    def _lookup_node_by_price(self, price: float | None) -> dict[str, float] | None:
        """从 peak_df 按 price_mid 查找完整 Node JSON 结构。"""
        peak_df = self.raw.peak_df
        if price is None or peak_df is None or peak_df.empty:
            return None
        mask = np.isclose(peak_df["price_mid"].to_numpy(dtype=float), float(price), atol=1e-4)
        matched = peak_df[mask]
        if matched.empty:
            return None
        return self._node_row_to_json(matched.iloc[0])

    def nearest_nodes(self, current_price: float) -> dict[str, Any]:
        """获取参考价上方/下方最近的 Peak Node。

        Args:
            current_price: 参考价格

        Returns:
            {"upper_node": {...}|None, "lower_node": {...}|None}
        """
        nearest_info = extract_nearest_nodes(self.raw, current_price)
        return {
            "upper_node": self._lookup_node_by_price(nearest_info["nearest_above_node_price"]),
            "lower_node": self._lookup_node_by_price(nearest_info["nearest_below_node_price"]),
        }

    def touched_node(self, current_price: float) -> dict[str, float] | None:
        """向量化检测当前价触碰的 Peak Node（价格落在 [price_low, price_high] 内）。

        若触碰多个 Node，选择 price_mid 最接近 current_price 的那个。

        Args:
            current_price: 当前价格

        Returns:
            触碰 Node 的 JSON 结构，或 None
        """
        peak_df = self.raw.peak_df
        if peak_df is None or peak_df.empty:
            return None
        mask = (peak_df["price_low"] <= current_price) & (peak_df["price_high"] >= current_price)
        touched = peak_df[mask]
        if touched.empty:
            return None
        idx = (touched["price_mid"] - current_price).abs().idxmin()
        return self._node_row_to_json(touched.loc[idx])

    def poc_node(self) -> dict[str, float] | None:
        """从 VP 结果中查找 POC 对应的 profile 行（JSON 结构）。"""
        profile_df = self.raw.profile_df
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

    def state_for_price(self, current_price: float) -> dict[str, Any]:
        """计算当前价对应的完整监控状态字段。

        输出字段与 volume_node_monitor.yaml outputs 对齐：
        current_price/upper_node/lower_node/position_0_1/poc_price/last_touched_node

        Args:
            current_price: 当前价格

        Returns:
            状态字典
        """
        nodes = self.nearest_nodes(current_price)
        return {
            "current_price": round(float(current_price), 4),
            "upper_node": nodes["upper_node"],
            "lower_node": nodes["lower_node"],
            "position_0_1": self.position_0_1(current_price),
            "poc_price": self.poc_node(),
            "last_touched_node": self.touched_node(current_price),
        }


def _prepare_bars_for_vp(bars: pd.DataFrame) -> pd.DataFrame:
    """准备 OHLCV DataFrame 供 compute_volume_profile 使用。

    compute_volume_profile 内部调用 _normalize_columns，要求含 open/high/low/close/volume
    列及 datetime 列（或可由 index 重置得到）。

    Args:
        bars: OHLCV DataFrame，DatetimeIndex + open/high/low/close/volume 列

    Returns:
        含 datetime 列的 OHLCV DataFrame（副本）
    """
    df = bars.copy()
    if "datetime" not in df.columns:
        if isinstance(df.index, pd.DatetimeIndex):
            df["datetime"] = df.index
        else:
            df["datetime"] = pd.to_datetime(df.index, errors="coerce")
    return df


# ===== Node Cluster 行情准备函数（advice.md 第四节）=====
# 参数版本标识：当 indicator_contract 中任一 Node Cluster 参数变更时，应同步更新此字符串
_NODE_CLUSTER_PARAMETER_VERSION = "v1.1.0"


@dataclass
class NodeClusterBarsResult:
    """prepare_node_cluster_bars 的返回结果。

    Attributes:
        daily: 准备后的日线 bars（DatetimeIndex 升序、去重、tail(250)）
        bars_15m: 准备后的 15 分钟 bars（DatetimeIndex 升序、去重、tail(1200)）
        bars_minute: 准备后的 1 分钟 bars（DatetimeIndex 升序、去重、tail(2)）
        profile_meta: 诊断元信息，含输入根数/周期/参数版本
    """

    daily: pd.DataFrame
    bars_15m: pd.DataFrame
    bars_minute: pd.DataFrame
    profile_meta: dict[str, Any] = field(default_factory=dict)


def _dedupe_sort_tail(bars: pd.DataFrame, n: int) -> pd.DataFrame:
    """统一执行 DatetimeIndex 排序 → 去重(keep=last) → tail(n)。

    Args:
        bars: 输入 DataFrame，期望 index 为 DatetimeIndex
        n: 保留最近 n 根

    Returns:
        准备后的 DataFrame（升序、去重、tail(n)）；空输入返回空 DataFrame
    """
    if bars is None or bars.empty:
        return pd.DataFrame() if bars is None else bars.iloc[0:0]

    df = bars
    # 若 index 不是 DatetimeIndex，尝试转换
    if not isinstance(df.index, pd.DatetimeIndex):
        df = df.copy()
        df.index = pd.to_datetime(df.index, errors="coerce")
        # 转换失败的行（NaT）剔除
        df = df[~df.index.isna()]

    if df.empty:
        return df.iloc[0:0]

    # 删除重复 index（keep=last 保留最后一条）
    df = df[~df.index.duplicated(keep="last")]
    # 升序排序
    df = df.sort_index()
    # 取最近 n 根
    if n > 0 and len(df) > n:
        df = df.tail(n)
    return df


def prepare_node_cluster_bars(
    daily_bars: pd.DataFrame,
    bars_15m: pd.DataFrame,
    bars_minute: pd.DataFrame,
) -> NodeClusterBarsResult:
    """Node Cluster 行情共享准备函数（advice.md 第四节唯一真源）。

    统一执行：
    1. DatetimeIndex 排序（升序）
    2. 删除重复 index（keep=last）
    3. 过滤未完成 Bar（由调用方保证，本函数不再额外过滤）
    4. daily.tail(NODE_CLUSTER_PRIMARY_BARS) / 15m.tail(NODE_CLUSTER_LOW_BARS) / 1m.tail(NODE_CLUSTER_MINUTE_BARS)
    5. 数据不足时返回实际根数（不静默改用其他参数），诊断字段记录真实根数

    盘中 MonitorBatchService 和个股详情 IndicatorService 必须调用本函数，
    禁止各自处理 sort_values/drop_duplicates/tail。

    Args:
        daily_bars: 日线 OHLCV DataFrame（DatetimeIndex）
        bars_15m: 15 分钟 OHLCV DataFrame（DatetimeIndex）
        bars_minute: 1 分钟 OHLCV DataFrame（DatetimeIndex）

    Returns:
        NodeClusterBarsResult，含准备后的三个 DataFrame 与 profile_meta 诊断字段
    """
    daily_prepared = _dedupe_sort_tail(daily_bars, NODE_CLUSTER_PRIMARY_BARS)
    bars_15m_prepared = _dedupe_sort_tail(bars_15m, NODE_CLUSTER_LOW_BARS)
    bars_minute_prepared = _dedupe_sort_tail(bars_minute, NODE_CLUSTER_MINUTE_BARS)

    profile_meta: dict[str, Any] = {
        "input_daily_bars": int(len(daily_prepared)),
        "input_15m_bars": int(len(bars_15m_prepared)),
        "input_minute_bars": int(len(bars_minute_prepared)),
        "primary_period": NODE_CLUSTER_PRIMARY_PERIOD,  # "1d"
        "low_period": NODE_CLUSTER_LOW_PERIOD,  # "15m"
        "parameter_version": _NODE_CLUSTER_PARAMETER_VERSION,
    }

    return NodeClusterBarsResult(
        daily=daily_prepared,
        bars_15m=bars_15m_prepared,
        bars_minute=bars_minute_prepared,
        profile_meta=profile_meta,
    )


def compute_unified_volume_profile(
    df: pd.DataFrame,
    profile_df: pd.DataFrame | None = None,
    main_period: str = "day",
) -> UnifiedVolumeProfileResult:
    """计算统一 Volume Profile（所有业务链路唯一真源）。

    使用固定真源参数调用 LuxAlgo compute_volume_profile，返回 UnifiedVolumeProfileResult。
    主数据通常为日线 bars，profile_df 为低周期 bars（如 15m）用于成交量分配。

    Args:
        df: 主数据 OHLCV DataFrame
        profile_df: 低周期 bars DataFrame（供成交量分配），或 None
        main_period: 主数据周期（"day"/"1m" 等）

    Returns:
        UnifiedVolumeProfileResult
    """
    bars = _prepare_bars_for_vp(df)
    profile = (
        _prepare_bars_for_vp(profile_df)
        if profile_df is not None and not profile_df.empty
        else None
    )

    cfg = VolumeProfileConfig(
        peaks_show="peaks",
        profile_lookback_length=VP_LOOKBACK,
        profile_number_of_rows=VP_ROWS,
        peaks_detection_percent=VP_PEAK_DETECTION_PCT,
        value_area_threshold=VP_VALUE_AREA_PCT,
        troughs_show=VP_TROUGHS_SHOW,
        troughs_detection_percent=VP_TROUGHS_DETECTION_PCT,
        volume_node_threshold=VP_NODE_THRESHOLD_PCT,
        highest_n_volume_nodes=VP_HIGHEST_N_NODES,
        lowest_n_volume_nodes=VP_LOWEST_N_NODES,
    )

    raw = compute_volume_profile(
        bars,
        cfg,
        profile_df=profile,
        main_period=main_period,
    )
    return UnifiedVolumeProfileResult(raw=raw)


if __name__ == "__main__":
    # 自测入口：验证统一模块计算与状态查询（无副作用，不写库表）
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(42)
    n = 100
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, n)))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0.0, 0.005, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0.005, 0.02, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.005, 0.02, n))
    volume = rng.lognormal(mean=13, sigma=0.35, size=n).astype(int)
    dates = pd.date_range("2026-01-01", periods=n, freq="D")

    bars_daily = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )

    result = compute_unified_volume_profile(bars_daily, profile_df=None, main_period="day")
    assert isinstance(result, UnifiedVolumeProfileResult)
    assert isinstance(result.raw, VolumeProfileResult)
    print(f"POC={result.poc_price:.4f} VAH={result.vah_price:.4f} VAL={result.val_price:.4f} ✓")

    assert not result.profile_df.empty
    assert "price_mid" in result.profile_df.columns
    assert "bullish_volume" in result.profile_df.columns
    assert "bearish_volume" in result.profile_df.columns
    assert "total_volume" in result.profile_df.columns
    assert "is_peak" in result.profile_df.columns
    print("profile_df 字段完整 ✓")

    assert isinstance(result.peak_rows, pd.DataFrame)
    if not result.peak_rows.empty:
        assert "price_mid" in result.peak_rows.columns
        assert "bullish_volume" in result.peak_rows.columns
        assert "bearish_volume" in result.peak_rows.columns
        assert "total_volume" in result.peak_rows.columns
        assert "is_peak" in result.peak_rows.columns
    print(f"peak_rows count={len(result.peak_rows)} ✓")

    current_price = float(bars_daily["close"].iloc[-1])
    state = result.state_for_price(current_price)
    expected_keys = {
        "current_price", "upper_node", "lower_node",
        "position_0_1", "poc_price", "last_touched_node",
    }
    assert set(state.keys()) == expected_keys
    assert 0.0 <= state["position_0_1"] <= 1.0
    assert state["current_price"] == round(current_price, 4)
    print(f"state_for_price: position_0_1={state['position_0_1']} ✓")

    print("OK")
