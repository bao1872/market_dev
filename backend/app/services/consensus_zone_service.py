"""ConsensusZone 计算服务（PRD V1.1 §7.4）。

核心算法（V2: 日线主结构 + 15m 细化）：
1. 过滤 timestamp <= as_of（因果性，杜绝未来数据泄漏）— 服务入口内部执行
2. 日线主结构：将日线价格区间离散为 bins，按成交量形成分布
3. 识别有效局部峰；相邻峰以最低谷分割为独立峰簇
4. 15m 细化：在每个日线峰簇价格区间内，用 15m bars 重算成交量加权 P10/P50/P90
5. 输出 lower=P10, upper=P90, center=P50, peakPrice, volumeRatio, strength
6. 重叠簇按固定规则合并（OVERLAP_RULE_VERSION）

V2 算法版本（CONSENSUS_ALGORITHM_VERSION="v2"）：
- 日线主结构：固定窗口日线（250根，DAILY_HISTORY_BARS），识别峰簇价格区间
- 15m 细化：固定窗口 15m（4000根，NODE_CLUSTER_LOW_BARS），在簇区间内重算百分位
- timeframe：固定 "1d"（主结构周期，不随显示周期变化）
- refinementTimeframe: "15m"（细化分布周期）
- data_version: 真实 bar hash（日线+15m 末根时间+数量），缓存键组成部分
- 显示周期切换（1d/15m/1h/1w/1mo）不改变 ConsensusZone 定义

缓存键包含 symbol/as_of/timeframe/algorithm_version/data_version，Redis TTL。

纯函数设计：底层函数接受 numpy 数组，无 DB/Redis 依赖，可直接单元测试。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

import numpy as np
import pandas as pd

from app.constants.indicator_contract import (
    DAILY_HISTORY_BARS,
    NODE_CLUSTER_LOW_BARS,
)
from app.core.redis_client import get_redis
from app.schemas.consensus_zone import ConsensusCluster, ConsensusZoneResult

# 算法版本：修改计算逻辑/参数/分箱方式必须 bump
CONSENSUS_ALGORITHM_VERSION = "v2"
# 重叠区合并规则版本：修改合并逻辑必须 bump
OVERLAP_RULE_VERSION = "v1"

# 默认参数（不暴露给用户）
DEFAULT_NUM_BINS = 50
DEFAULT_MIN_VOLUME_RATIO = 0.05  # 峰簇最小成交量占比（低于此值忽略）
DEFAULT_PEAK_PROMINENCE = 0.10  # 峰最小突出度（相对最大成交量的比例）
# 重叠合并阈值：两簇重叠宽度 / 较小簇宽度 > 此值则合并
OVERLAP_MERGE_THRESHOLD = 0.5
CACHE_TTL_SECONDS = 3600  # Redis TTL 1小时

# 固定输入窗口从 indicator_contract 导入（DAILY_HISTORY_BARS=250, NODE_CLUSTER_LOW_BARS=4000）
# 禁止本地重新定义受控字面量，由 test_no_duplicate_controlled_params 守门


def filter_bars_by_as_of(
    bars: pd.DataFrame,
    as_of: date | datetime | str | None,
) -> pd.DataFrame:
    """过滤 timestamp <= as_of，杜绝未来数据泄漏（PRD V1.1 §7.4 步骤1）。

    若 as_of 为 date 或 YYYY-MM-DD 字符串，按 Asia/Shanghai 当日结束
    （23:59:59 CST）处理，确保盘后计算时当日 15m 数据不被排除。
    若 as_of 为 datetime，按精确时间过滤。

    Args:
        bars: OHLCV DataFrame，必须含 datetime/high/low/close/volume 列
        as_of: 截止时间；date/str 按当日结束处理；None 表示不过滤

    Returns:
        过滤后的 DataFrame（按 datetime 升序）
    """
    if as_of is None or bars.empty:
        return bars.copy()

    dt_col = "datetime" if "datetime" in bars.columns else "trade_date"
    if dt_col not in bars.columns:
        return bars.copy()

    bar_times = pd.to_datetime(bars[dt_col])

    # date-only 或 YYYY-MM-DD 字符串：按当日结束（23:59:59）处理
    if isinstance(as_of, date) and not isinstance(as_of, datetime):
        cutoff = pd.Timestamp(as_of) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    elif isinstance(as_of, str):
        parsed = pd.to_datetime(as_of)
        # 若字符串只含日期部分（无时间），按当日结束处理
        if len(as_of) <= 10:
            cutoff = parsed + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        else:
            cutoff = parsed
    else:
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


def _format_bars_for_hash(bars: pd.DataFrame, prefix: str) -> str:
    """将 DataFrame 全量行格式化为稳定字符串（排序+标准化 OHLCV）。

    每行格式：time|open|high|low|close|volume（固定精度）。
    行按时间升序排列，相同数据（即使行序不同）产生相同字符串。
    """
    if bars.empty:
        return ""
    dt_col = "datetime" if "datetime" in bars.columns else (
        "trade_date" if "trade_date" in bars.columns else None
    )
    if dt_col is None:
        return f"{prefix}:{len(bars)}:notime"

    sorted_bars = bars.sort_values(dt_col)
    times = pd.to_datetime(sorted_bars[dt_col]).dt.strftime("%Y-%m-%dT%H:%M:%S")
    opens = sorted_bars["open"].round(4).astype(str)
    highs = sorted_bars["high"].round(4).astype(str)
    lows = sorted_bars["low"].round(4).astype(str)
    closes = sorted_bars["close"].round(4).astype(str)
    volumes = sorted_bars["volume"].round(2).astype(str)

    rows = (
        times + "|" + opens + "|" + highs + "|" + lows
        + "|" + closes + "|" + volumes
    )
    return f"{prefix}:{len(sorted_bars)}\n" + "\n".join(rows.tolist())


def compute_data_version(
    daily_bars: pd.DataFrame,
    bars_15min: pd.DataFrame | None,
) -> str:
    """计算动态 data_version（PRD V1.1 §7.4: 真实 bar 版本/hash）。

    对排序、标准化后的全部 1d 和 15m 时间戳 + OHLCV 行计算 SHA-256。
    不依赖 DataFrame 字符串表示（不稳定），不使用首尾摘要。
    每行格式：time|open|high|low|close|volume（固定精度）。

    稳定性保证：
    - 行按时间升序排列
    - 相同数据（即使行序不同）产生相同 hash
    - 任意行 OHLCV 变化都会改变 hash

    Args:
        daily_bars: 日线 DataFrame（已过滤+tail）
        bars_15min: 15m DataFrame（已过滤+tail，可为 None）

    Returns:
        8 字符 hash 字符串
    """
    parts: list[str] = []

    daily_str = _format_bars_for_hash(daily_bars, "d")
    if daily_str:
        parts.append(daily_str)

    if bars_15min is not None:
        m_str = _format_bars_for_hash(bars_15min, "m")
        if m_str:
            parts.append(m_str)

    if not parts:
        return "empty"
    raw = "\n---\n".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def merge_overlapping_clusters(
    clusters: list[ConsensusCluster],
    threshold: float = OVERLAP_MERGE_THRESHOLD,
) -> list[ConsensusCluster]:
    """重叠簇合并规则 v1（PRD V1.1 §7.4 步骤6: 重叠区按固定规则合并）。

    规则：两簇 [lower, upper] 重叠时，若重叠宽度 / 较小簇宽度 > threshold，
    则合并为一个大簇（lower=min, upper=max, center=成交量加权 center,
    volumeRatio=相加, strength=取较大值, peakPrice=取较高 volumeRatio 的峰）。

    Args:
        clusters: 已按 volumeRatio 降序排列的簇列表
        threshold: 合并阈值（默认 0.5）

    Returns:
        合并后的簇列表（仍按 volumeRatio 降序）
    """
    if len(clusters) <= 1:
        return clusters

    # 按 lower 升序排序便于检测相邻重叠
    sorted_clusters = sorted(clusters, key=lambda c: c.lower)
    merged: list[ConsensusCluster] = [sorted_clusters[0]]

    for current in sorted_clusters[1:]:
        prev = merged[-1]
        overlap_low = max(prev.lower, current.lower)
        overlap_high = min(prev.upper, current.upper)
        overlap_width = overlap_high - overlap_low

        if overlap_width <= 0:
            # 无重叠，直接添加
            merged.append(current)
            continue

        prev_width = prev.upper - prev.lower
        curr_width = current.upper - current.lower
        min_width = min(prev_width, curr_width)

        if min_width <= 0:
            merged.append(current)
            continue

        overlap_ratio = overlap_width / min_width
        if overlap_ratio > threshold:
            # 合并：取更大 volumeRatio 的 peakPrice，宽度取并集
            dominant = prev if prev.volumeRatio >= current.volumeRatio else current
            merged[-1] = ConsensusCluster(
                lower=min(prev.lower, current.lower),
                upper=max(prev.upper, current.upper),
                center=dominant.center,
                peakPrice=dominant.peakPrice,
                volumeRatio=round(prev.volumeRatio + current.volumeRatio, 4),
                strength=max(prev.strength, current.strength),
            )
        else:
            merged.append(current)

    # 重新按 volumeRatio 降序
    merged.sort(key=lambda c: c.volumeRatio, reverse=True)
    return merged


def refine_cluster_with_15m(
    cluster: ConsensusCluster,
    bars_15min: pd.DataFrame,
) -> ConsensusCluster:
    """用 15m bars 细化单个日线簇的 P10/P50/P90（PRD V1.1 §7.4: 15m 细化分布）。

    在日线簇价格区间 [cluster.lower, cluster.upper] 内，用 15m bars 的
    high/low/volume 重算成交量加权百分位，得到更精细的分布。

    Args:
        cluster: 日线主结构识别的簇
        bars_15min: 15m OHLCV DataFrame

    Returns:
        细化后的簇（若 15m 数据不足则返回原簇）
    """
    if bars_15min.empty:
        return cluster

    # 筛选与簇价格区间相交的 15m bars
    highs = bars_15min["high"].to_numpy(dtype=float)
    lows = bars_15min["low"].to_numpy(dtype=float)
    volumes = bars_15min["volume"].to_numpy(dtype=float)

    # bar 与簇区间相交：bar.low <= cluster.upper AND bar.high >= cluster.lower
    mask = (lows <= cluster.upper) & (highs >= cluster.lower)
    if not mask.any():
        return cluster

    cluster_highs = highs[mask]
    cluster_lows = lows[mask]
    cluster_volumes = volumes[mask]
    cluster_total_vol = float(np.sum(cluster_volumes))

    if cluster_total_vol <= 0 or len(cluster_highs) < 5:
        return cluster

    # 在簇区间内重新分箱计算分布
    bin_volumes, bin_centers, _, _ = compute_volume_distribution(
        cluster_highs, cluster_lows, cluster_volumes, DEFAULT_NUM_BINS
    )

    if len(bin_volumes) == 0 or float(np.sum(bin_volumes)) <= 0:
        return cluster

    # 用 15m 分布重算 P10/P50/P90
    pcts = compute_volume_weighted_percentiles(
        bin_centers, bin_volumes, [10, 50, 90]
    )
    peak_idx = int(np.argmax(bin_volumes))
    peak_price = float(bin_centers[peak_idx])

    return ConsensusCluster(
        lower=pcts[10],
        upper=pcts[90],
        center=pcts[50],
        peakPrice=peak_price,
        volumeRatio=cluster.volumeRatio,
        strength=cluster.strength,
    )


def compute_consensus_zones(
    daily_bars: pd.DataFrame,
    symbol: str,
    as_of: str,
    bars_15min: pd.DataFrame | None = None,
    num_bins: int = DEFAULT_NUM_BINS,
) -> ConsensusZoneResult:
    """纯函数：日线主结构 + 15m 细化计算 ConsensusZone（PRD V1.1 §7.4 V2）。

    完整流程：
    1. 日线分布 → 峰识别 → 谷分割 → 日线簇
    2. 若 bars_15min 可用，每簇用 15m 重算 P10/P50/P90（细化）
    3. 重叠簇按 OVERLAP_RULE_VERSION 合并

    Args:
        daily_bars: 日线 OHLCV DataFrame（含 datetime/high/low/close/volume）
        symbol: 股票代码
        as_of: 截止时间 ISO
        bars_15min: 15m OHLCV DataFrame（可选，None 表示仅日线主结构）
        num_bins: 价格分箱数

    Returns:
        ConsensusZoneResult
    """
    data_version = compute_data_version(daily_bars, bars_15min)
    refinement_tf: str | None = "15m" if (
        bars_15min is not None and not bars_15min.empty
    ) else None

    # 数据不足检查
    if daily_bars.empty or len(daily_bars) < 10:
        return ConsensusZoneResult(
            symbol=symbol,
            timeframe="1d",
            asOf=as_of,
            algorithmVersion=CONSENSUS_ALGORITHM_VERSION,
            dataVersion=data_version,
            overlapRuleVersion=OVERLAP_RULE_VERSION,
            refinementTimeframe=refinement_tf,
            clusters=[],
            totalVolume=0.0,
            binCount=0,
            priceMin=0.0,
            priceMax=0.0,
            isAvailable=False,
            unavailableReason="insufficient daily bars (need >= 10)",
        )

    highs = daily_bars["high"].to_numpy(dtype=float)
    lows = daily_bars["low"].to_numpy(dtype=float)
    volumes = daily_bars["volume"].to_numpy(dtype=float)
    total_volume = float(np.sum(volumes))
    price_min = float(np.min(lows))
    price_max = float(np.max(highs))

    if total_volume <= 0:
        return ConsensusZoneResult(
            symbol=symbol,
            timeframe="1d",
            asOf=as_of,
            algorithmVersion=CONSENSUS_ALGORITHM_VERSION,
            dataVersion=data_version,
            overlapRuleVersion=OVERLAP_RULE_VERSION,
            refinementTimeframe=refinement_tf,
            clusters=[],
            totalVolume=0.0,
            binCount=0,
            priceMin=price_min,
            priceMax=price_max,
            isAvailable=False,
            unavailableReason="zero total volume",
        )

    # 步骤2: 日线价格分箱 + 成交量分布
    bin_volumes, bin_centers, _, _ = compute_volume_distribution(
        highs, lows, volumes, num_bins
    )

    if len(bin_volumes) == 0:
        return ConsensusZoneResult(
            symbol=symbol,
            timeframe="1d",
            asOf=as_of,
            algorithmVersion=CONSENSUS_ALGORITHM_VERSION,
            dataVersion=data_version,
            overlapRuleVersion=OVERLAP_RULE_VERSION,
            refinementTimeframe=refinement_tf,
            clusters=[],
            totalVolume=total_volume,
            binCount=0,
            priceMin=price_min,
            priceMax=price_max,
            isAvailable=False,
            unavailableReason="empty volume distribution",
        )

    # 步骤3: 峰识别 + 谷分割（日线主结构）
    peaks = identify_peaks(bin_volumes)
    cluster_bounds = segment_clusters_by_valleys(bin_volumes, peaks)

    max_vol = float(np.max(bin_volumes))

    # 步骤4: 每簇日线加权百分位
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

    # 步骤5: 15m 细化（若可用）
    if bars_15min is not None and not bars_15min.empty:
        clusters = [
            refine_cluster_with_15m(c, bars_15min) for c in clusters
        ]

    # 步骤6: 重叠簇合并
    clusters = merge_overlapping_clusters(clusters)

    # 按成交量降序排列
    clusters.sort(key=lambda c: c.volumeRatio, reverse=True)

    # 细化后更新 price_min/max 以纳入所有簇边界
    if clusters:
        all_lowers = [c.lower for c in clusters]
        all_uppers = [c.upper for c in clusters]
        price_min = min(price_min, min(all_lowers))
        price_max = max(price_max, max(all_uppers))

    return ConsensusZoneResult(
        symbol=symbol,
        timeframe="1d",
        asOf=as_of,
        algorithmVersion=CONSENSUS_ALGORITHM_VERSION,
        dataVersion=data_version,
        overlapRuleVersion=OVERLAP_RULE_VERSION,
        refinementTimeframe=refinement_tf,
        clusters=clusters,
        totalVolume=total_volume,
        binCount=len(bin_volumes),
        priceMin=price_min,
        priceMax=price_max,
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
    data_version: str,
) -> ConsensusZoneResult | None:
    """从 Redis 获取缓存的 ConsensusZone 结果（需传入 data_version）。

    v1 缓存安全：缓存键含 algorithm_version="v2"，v1 键（"v1"）不会被读取。
    若旧 v1 JSON 被手动注入，Pydantic 反序列化会因缺少 dataVersion/
    overlapRuleVersion/refinementTimeframe/priceMin/priceMax 必填字段而
    抛 ValidationError，被 except 捕获后返回 None（视为缓存未命中，重算）。
    """
    try:
        redis = get_redis()
        key = _build_cache_key(
            symbol, as_of, timeframe,
            CONSENSUS_ALGORITHM_VERSION, data_version,
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
            result.algorithmVersion, result.dataVersion,
        )
        await redis.setex(key, ttl, result.model_dump_json())
    except Exception:
        pass  # 缓存失败不影响主流程


async def compute_and_cache_consensus_zone(
    daily_bars: pd.DataFrame,
    symbol: str,
    as_of: str | date | datetime,
    bars_15min: pd.DataFrame | None = None,
) -> ConsensusZoneResult:
    """计算 + 缓存 ConsensusZone（服务层入口，PRD V1.1 §7.4 V2）。

    因果性过滤 + 固定窗口 tail 顺序（C1 纠偏）：
    1. filter_bars_by_as_of — 过滤 timestamp <= as_of（杜绝未来数据）
    2. tail(DAILY_HISTORY_BARS) / tail(NODE_CLUSTER_LOW_BARS) — 截取固定窗口
    3. compute_data_version — 基于过滤+tail 后的数据计算稳定 hash
    4. 查缓存 → 计算 → 写缓存

    调用方应直接查询约 4000 根 15m（而非加载约 12000 根再截取），
    本函数仍会 filter + tail 作为安全保证。

    Args:
        daily_bars: 日线 OHLCV DataFrame
        symbol: 股票代码
        as_of: 截止时间（因果性锚点）
        bars_15min: 15m OHLCV DataFrame（可选）

    Returns:
        ConsensusZoneResult
    """
    as_of_str = as_of if isinstance(as_of, str) else as_of.isoformat()

    # 步骤1: 因果性过滤 — 先 filter timestamp <= as_of
    filtered_daily = filter_bars_by_as_of(daily_bars, as_of)
    filtered_15m: pd.DataFrame | None = None
    if bars_15min is not None and not bars_15min.empty:
        filtered_15m = filter_bars_by_as_of(bars_15min, as_of)

    # 步骤2: 固定窗口 tail — 从过滤后的数据截取最近 N 根
    if not filtered_daily.empty:
        filtered_daily = filtered_daily.tail(DAILY_HISTORY_BARS)
    if filtered_15m is not None and not filtered_15m.empty:
        filtered_15m = filtered_15m.tail(NODE_CLUSTER_LOW_BARS)

    # 步骤3: data_version 基于过滤+tail 后的最终数据计算（稳定 hash）
    data_version = compute_data_version(filtered_daily, filtered_15m)

    # 步骤4: 查缓存
    cached = await get_cached_consensus_zone(symbol, as_of_str, "1d", data_version)
    if cached is not None:
        return cached

    # 步骤5: 计算（V2: 日线主结构 + 15m 细化）
    result = compute_consensus_zones(
        filtered_daily, symbol, as_of_str, filtered_15m,
    )

    # 步骤6: 写缓存
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

    # 构造 15m bars（每日 16 根，30 天 = 480 根）
    dates_15m = pd.date_range("2026-06-01", periods=480, freq="15min")
    prices_15m = 10.0 + np.cumsum(np.random.randn(480) * 0.03)
    bars_15m = pd.DataFrame({
        "datetime": dates_15m,
        "open": prices_15m,
        "high": prices_15m + 0.05,
        "low": prices_15m - 0.05,
        "close": prices_15m,
        "volume": np.random.randint(100, 2000, 480).astype(float),
    })

    result = compute_consensus_zones(bars, "000001", "2026-06-30", bars_15m)
    print(f"algorithmVersion: {result.algorithmVersion}")
    print(f"dataVersion: {result.dataVersion}")
    print(f"overlapRuleVersion: {result.overlapRuleVersion}")
    print(f"refinementTimeframe: {result.refinementTimeframe}")
    print(f"isAvailable: {result.isAvailable}")
    print(f"clusters: {len(result.clusters)}")
    print(f"totalVolume: {result.totalVolume:.0f}")
    print(f"binCount: {result.binCount}")
    print(f"priceMin: {result.priceMin:.2f} priceMax: {result.priceMax:.2f}")
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
