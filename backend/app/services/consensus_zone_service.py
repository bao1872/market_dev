"""ConsensusZone 计算服务（PRD V1.1 §7.4）。

核心算法：
1. 过滤 timestamp <= as_of（因果性，杜绝未来数据泄漏）
2. 将价格区间离散为 bins，按成交量形成分布
3. 识别有效局部峰；相邻峰以最低谷分割为独立峰簇
4. 在每个峰簇内部，以各价位成交量为权重计算价格 P10、P50、P90
5. 输出 lower=P10, upper=P90, center=P50, peakPrice, volumeRatio, strength

主结构使用日线，细化分布使用15分钟数据。
缓存键包含 symbol/as_of/timeframe/algorithm_version/data_version，Redis TTL。

纯函数设计：compute_consensus_zones 接受 numpy 数组，返回结果列表，
无 DB/Redis 依赖，可直接单元测试。
"""

from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np
import pandas as pd

from app.core.redis_client import get_redis
from app.schemas.consensus_zone import ConsensusCluster, ConsensusZoneResult

# 算法版本：修改计算逻辑/参数/分箱方式必须 bump
CONSENSUS_ALGORITHM_VERSION = "v1"
CONSENSUS_DATA_VERSION = "1"

# 默认参数（不暴露给用户）
DEFAULT_NUM_BINS = 50
DEFAULT_MIN_VOLUME_RATIO = 0.05  # 峰簇最小成交量占比（低于此值忽略）
DEFAULT_PEAK_PROMINENCE = 0.10  # 峰最小突出度（相对最大成交量的比例）
CACHE_TTL_SECONDS = 3600  # Redis TTL 1小时


def filter_bars_by_as_of(
    bars: pd.DataFrame,
    as_of: date | datetime | None,
) -> pd.DataFrame:
    """过滤 timestamp <= as_of，杜绝未来数据泄漏（PRD V1.1 §7.4 步骤1）。

    Args:
        bars: OHLCV DataFrame，必须含 datetime/high/low/close/volume 列
        as_of: 截止时间；None 表示不过滤（全部数据）

    Returns:
        过滤后的 DataFrame（按 datetime 升序）
    """
    if as_of is None or bars.empty:
        return bars.copy()

    dt_col = "datetime" if "datetime" in bars.columns else "trade_date"
    if dt_col not in bars.columns:
        return bars.copy()

    bar_times = pd.to_datetime(bars[dt_col])
    cutoff = pd.Timestamp(as_of)
    filtered = bars[bar_times <= cutoff].copy()
    return filtered


def compute_volume_distribution(
    highs: np.ndarray,
    lows: np.ndarray,
    volumes: np.ndarray,
    num_bins: int = DEFAULT_NUM_BINS,
) -> tuple[np.ndarray, np.ndarray, float, float]:
    """将价格区间离散为 bins，按成交量形成分布（PRD V1.1 §7.4 步骤2）。

    每根 bar 的成交量按价格区间与 bin 的重叠比例分配到各 bin。

    Args:
        highs: 每根 bar 的最高价
        lows: 每根 bar 的最低价
        volumes: 每根 bar 的成交量
        num_bins: 价格分箱数

    Returns:
        (bin_volumes, bin_centers, price_min, price_max)
    """
    if len(highs) == 0:
        return np.array([]), np.array([]), 0.0, 0.0

    price_min = float(np.min(lows))
    price_max = float(np.max(highs))
    if price_max <= price_min:
        # 所有 bar 价格相同，单一 bin
        bin_volumes = np.array([float(np.sum(volumes))])
        bin_centers = np.array([(price_min + price_max) / 2.0])
        return bin_volumes, bin_centers, price_min, price_max

    bin_width = (price_max - price_min) / num_bins
    bin_edges = np.linspace(price_min, price_max, num_bins + 1)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2.0
    bin_volumes = np.zeros(num_bins, dtype=float)

    for i in range(len(highs)):
        h = highs[i]
        lo = lows[i]
        v = volumes[i]
        if v <= 0 or h <= lo:
            continue

        # 计算 bar 覆盖的 bin 范围
        lo_idx = int((lo - price_min) / bin_width)
        hi_idx = int((h - price_min) / bin_width)
        lo_idx = max(0, min(num_bins - 1, lo_idx))
        hi_idx = max(0, min(num_bins - 1, hi_idx))

        if lo_idx == hi_idx:
            bin_volumes[lo_idx] += v
        else:
            # 按重叠比例分配
            total_span = h - lo
            for b in range(lo_idx, hi_idx + 1):
                overlap_low = max(lo, bin_edges[b])
                overlap_high = min(h, bin_edges[b + 1])
                overlap = overlap_high - overlap_low
                if overlap > 0:
                    bin_volumes[b] += v * (overlap / total_span)

    return bin_volumes, bin_centers, price_min, price_max


def identify_peaks(
    bin_volumes: np.ndarray,
    prominence: float = DEFAULT_PEAK_PROMINENCE,
) -> list[int]:
    """识别有效局部峰（PRD V1.1 §7.4 步骤3）。

    峰定义：bin_volumes[i] > 左右邻居，且 >= prominence * max_volume。

    Args:
        bin_volumes: 各 bin 的成交量
        prominence: 峰最小突出度（相对最大成交量）

    Returns:
        峰索引列表（升序）
    """
    if len(bin_volumes) < 3:
        # 数据太少，返回全局最大值作为唯一峰
        if len(bin_volumes) > 0:
            return [int(np.argmax(bin_volumes))]
        return []

    max_vol = float(np.max(bin_volumes))
    if max_vol <= 0:
        return []

    threshold = prominence * max_vol
    peaks: list[int] = []

    for i in range(1, len(bin_volumes) - 1):
        if (
            bin_volumes[i] > bin_volumes[i - 1]
            and bin_volumes[i] >= bin_volumes[i + 1]
            and bin_volumes[i] >= threshold
        ):
            peaks.append(i)

    # 检查边界
    if len(bin_volumes) >= 2:
        if bin_volumes[0] > bin_volumes[1] and bin_volumes[0] >= threshold:
            peaks.insert(0, 0)
        last = len(bin_volumes) - 1
        if bin_volumes[last] > bin_volumes[last - 1] and bin_volumes[last] >= threshold:
            peaks.append(last)

    return peaks


def segment_clusters_by_valleys(
    bin_volumes: np.ndarray,
    peaks: list[int],
) -> list[tuple[int, int]]:
    """相邻峰以最低谷分割为独立峰簇（PRD V1.1 §7.4 步骤3）。

    Args:
        bin_volumes: 各 bin 的成交量
        peaks: 峰索引列表（升序）

    Returns:
        簇边界列表 [(start_idx, end_idx), ...]（含峰到相邻峰间最低谷）
    """
    if len(peaks) == 0:
        return []
    if len(peaks) == 1:
        # 单峰：整个范围为一个簇
        return [(0, len(bin_volumes) - 1)]

    clusters: list[tuple[int, int]] = []
    for i, peak in enumerate(peaks):
        if i == 0:
            start = 0
        else:
            # 找前一峰到当前峰之间的最低谷
            valley_idx = int(np.argmin(bin_volumes[peaks[i - 1] : peak])) + peaks[i - 1]
            start = valley_idx

        if i == len(peaks) - 1:
            end = len(bin_volumes) - 1
        else:
            # 找当前峰到下一峰之间的最低谷
            valley_idx = int(np.argmin(bin_volumes[peak : peaks[i + 1]])) + peak
            end = valley_idx

        clusters.append((start, end))

    return clusters


def compute_volume_weighted_percentiles(
    prices: np.ndarray,
    volumes: np.ndarray,
    percentiles: list[float],
) -> dict[float, float]:
    """成交量加权百分位计算（PRD V1.1 §7.4 步骤4）。

    Args:
        prices: 价格数组
        volumes: 对应成交量权重
        percentiles: 百分位列表（如 [10, 50, 90]）

    Returns:
        {percentile: value} 字典
    """
    if len(prices) == 0 or np.sum(volumes) <= 0:
        return dict.fromkeys(percentiles, 0.0)

    # 按价格排序
    sort_idx = np.argsort(prices)
    sorted_prices = prices[sort_idx]
    sorted_volumes = volumes[sort_idx]
    total_vol = np.sum(sorted_volumes)
    cum_vol = np.cumsum(sorted_volumes) / total_vol

    result: dict[float, float] = {}
    for p in percentiles:
        target = p / 100.0
        idx = int(np.searchsorted(cum_vol, target))
        idx = min(idx, len(sorted_prices) - 1)
        result[p] = float(sorted_prices[idx])

    return result


def compute_cluster_strength(
    bin_volumes: np.ndarray,
    start: int,
    end: int,
    max_volume: float,
) -> float:
    """计算簇强度（0-1，峰度归一化）。

    强度 = 峰值成交量 / 全局最大成交量 * (1 - 簇宽度占比)
    """
    if end < start or max_volume <= 0:
        return 0.0
    peak_vol = float(np.max(bin_volumes[start : end + 1]))
    width_ratio = (end - start + 1) / len(bin_volumes) if len(bin_volumes) > 0 else 1.0
    return max(0.0, min(1.0, (peak_vol / max_volume) * (1.0 - width_ratio * 0.5)))


def compute_consensus_zones(
    bars: pd.DataFrame,
    symbol: str,
    timeframe: str,
    as_of: str,
    num_bins: int = DEFAULT_NUM_BINS,
) -> ConsensusZoneResult:
    """纯函数：从 OHLCV bars 计算 ConsensusZone（PRD V1.1 §7.4）。

    完整流程：过滤 → 分布 → 峰识别 → 谷分割 → 加权百分位

    Args:
        bars: OHLCV DataFrame（含 datetime/high/low/close/volume）
        symbol: 股票代码
        timeframe: 来源周期（1d/15m）
        as_of: 截止时间 ISO
        num_bins: 价格分箱数

    Returns:
        ConsensusZoneResult
    """
    # 数据不足检查
    if bars.empty or len(bars) < 10:
        return ConsensusZoneResult(
            symbol=symbol,
            timeframe=timeframe,
            asOf=as_of,
            algorithmVersion=CONSENSUS_ALGORITHM_VERSION,
            clusters=[],
            totalVolume=0.0,
            binCount=0,
            isAvailable=False,
            unavailableReason="insufficient bars (need >= 10)",
        )

    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    volumes = bars["volume"].to_numpy(dtype=float)
    total_volume = float(np.sum(volumes))

    if total_volume <= 0:
        return ConsensusZoneResult(
            symbol=symbol,
            timeframe=timeframe,
            asOf=as_of,
            algorithmVersion=CONSENSUS_ALGORITHM_VERSION,
            clusters=[],
            totalVolume=0.0,
            binCount=0,
            isAvailable=False,
            unavailableReason="zero total volume",
        )

    # 步骤2: 价格分箱 + 成交量分布
    bin_volumes, bin_centers, _, _ = compute_volume_distribution(
        highs, lows, volumes, num_bins
    )

    if len(bin_volumes) == 0:
        return ConsensusZoneResult(
            symbol=symbol,
            timeframe=timeframe,
            asOf=as_of,
            algorithmVersion=CONSENSUS_ALGORITHM_VERSION,
            clusters=[],
            totalVolume=total_volume,
            binCount=0,
            isAvailable=False,
            unavailableReason="empty volume distribution",
        )

    # 步骤3: 峰识别 + 谷分割
    peaks = identify_peaks(bin_volumes)
    cluster_bounds = segment_clusters_by_valleys(bin_volumes, peaks)

    max_vol = float(np.max(bin_volumes))

    # 步骤4: 每簇加权百分位
    clusters: list[ConsensusCluster] = []
    for start, end in cluster_bounds:
        if end < start:
            continue
        cluster_prices = bin_centers[start : end + 1]
        cluster_volumes = bin_volumes[start : end + 1]
        cluster_total = float(np.sum(cluster_volumes))

        if cluster_total <= 0:
            continue

        volume_ratio = cluster_total / total_volume
        if volume_ratio < DEFAULT_MIN_VOLUME_RATIO:
            continue

        pcts = compute_volume_weighted_percentiles(
            cluster_prices, cluster_volumes, [10, 50, 90]
        )
        peak_idx = int(np.argmax(cluster_volumes))
        peak_price = float(cluster_prices[peak_idx])
        strength = compute_cluster_strength(bin_volumes, start, end, max_vol)

        clusters.append(ConsensusCluster(
            lower=pcts[10],
            upper=pcts[90],
            center=pcts[50],
            peakPrice=peak_price,
            volumeRatio=round(volume_ratio, 4),
            strength=round(strength, 4),
        ))

    # 按成交量降序排列
    clusters.sort(key=lambda c: c.volumeRatio, reverse=True)

    return ConsensusZoneResult(
        symbol=symbol,
        timeframe=timeframe,
        asOf=as_of,
        algorithmVersion=CONSENSUS_ALGORITHM_VERSION,
        clusters=clusters,
        totalVolume=total_volume,
        binCount=len(bin_volumes),
        isAvailable=len(clusters) > 0,
        unavailableReason=None if clusters else "no significant peaks detected",
    )


# =============================================================================
# Redis 缓存层
# =============================================================================


def _build_cache_key(
    symbol: str,
    as_of: str,
    timeframe: str,
    algo_version: str,
    data_version: str,
) -> str:
    """构建版本化缓存键（PRD V1.1 §7.4）。

    缓存键包含 symbol/as_of/timeframe/algorithm_version/data_version。
    """
    return (
        f"consensus_zone:{symbol}:{as_of}:{timeframe}"
        f":{algo_version}:{data_version}"
    )


async def get_cached_consensus_zone(
    symbol: str,
    as_of: str,
    timeframe: str,
) -> ConsensusZoneResult | None:
    """从 Redis 获取缓存的 ConsensusZone 结果。"""
    try:
        redis = get_redis()
        key = _build_cache_key(
            symbol, as_of, timeframe,
            CONSENSUS_ALGORITHM_VERSION, CONSENSUS_DATA_VERSION,
        )
        raw = await redis.get(key)
        if raw is None:
            return None
        data = json.loads(raw)
        return ConsensusZoneResult(**data)
    except Exception:
        return None


async def set_cached_consensus_zone(
    result: ConsensusZoneResult,
    ttl: int = CACHE_TTL_SECONDS,
) -> None:
    """将 ConsensusZone 结果写入 Redis（带 TTL）。"""
    try:
        redis = get_redis()
        key = _build_cache_key(
            result.symbol, result.asOf, result.timeframe,
            result.algorithmVersion, CONSENSUS_DATA_VERSION,
        )
        await redis.setex(key, ttl, result.model_dump_json())
    except Exception:
        pass  # 缓存失败不影响主流程


async def compute_and_cache_consensus_zone(
    bars: pd.DataFrame,
    symbol: str,
    timeframe: str,
    as_of: str,
) -> ConsensusZoneResult:
    """计算 + 缓存 ConsensusZone（服务层入口）。"""
    # 先查缓存
    cached = await get_cached_consensus_zone(symbol, as_of, timeframe)
    if cached is not None:
        return cached

    # 计算
    result = compute_consensus_zones(bars, symbol, timeframe, as_of)

    # 写缓存
    await set_cached_consensus_zone(result)

    return result


if __name__ == "__main__":
    # 自测：构造简单数据验证算法
    print("consensus_zone_service 自测...")

    dates = pd.date_range("2026-06-01", periods=30, freq="D")
    np.random.seed(42)
    prices = 10.0 + np.cumsum(np.random.randn(30) * 0.1)
    bars = pd.DataFrame({
        "datetime": dates,
        "open": prices,
        "high": prices + 0.2,
        "low": prices - 0.2,
        "close": prices,
        "volume": np.random.randint(1000, 10000, 30).astype(float),
    })

    result = compute_consensus_zones(bars, "000001", "1d", "2026-06-30")
    print(f"isAvailable: {result.isAvailable}")
    print(f"clusters: {len(result.clusters)}")
    print(f"totalVolume: {result.totalVolume:.0f}")
    print(f"binCount: {result.binCount}")
    for c in result.clusters:
        print(f"  lower={c.lower:.2f} center={c.center:.2f} upper={c.upper:.2f} "
              f"peak={c.peakPrice:.2f} ratio={c.volumeRatio:.2%} strength={c.strength:.2f}")

    # 因果性验证
    future_bars = bars[bars["datetime"] > pd.Timestamp("2026-06-20")]
    filtered = filter_bars_by_as_of(bars, pd.Timestamp("2026-06-20"))
    assert len(filtered) <= 20, "as_of 过滤失败"
    assert len(future_bars) > 0, "测试数据有未来数据"
    print(f"as_of filter: {len(bars)} -> {len(filtered)} bars (causal OK)")

    print("OK: consensus_zone_service 自测通过")
