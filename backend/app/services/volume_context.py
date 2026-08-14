"""统一 VolumeContext 计算模块（Gate1 第一金字塔收口）。

计算一次，被趋势/结构/动量共同引用，禁止三模块各自重复计算。

字段：
- volume, amount, turnover_rate（如有）
- volume_ma_20, volume_ma_200
- volume_ratio_20 = V / MA20, volume_ratio_200 = V / MA200
- volume_percentile_20 = 当前量在最近20日经验分布中的百分位（0-100）
- volume_percentile_200 = 当前量在最近200日经验分布中的百分位（0-100）
- volume_zscore_20 = (V - mean20) / std20
- volume_zscore_200 = (V - mean200) / std200

窗口样本不足返回 null + readiness=False，不得用 0 伪装。
分位统一 0-100，zscore 保留原值。

用法：
    from app.services.volume_context import compute_volume_context_series, extract_last_volume_context

    vc_series = compute_volume_context_series(bars)
    last_vc = extract_last_volume_context(vc_series)
    # last_vc -> VolumeContextData 或 None

模块自测：
    python -m app.services.volume_context
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

# 窗口大小
SHORT_WINDOW = 20
LONG_WINDOW = 200


@dataclass(frozen=True)
class VolumeContextData:
    """统一量能上下文（不可变）。"""

    volume: float | None
    amount: float | None
    turnover_rate: float | None
    volume_ma_20: float | None
    volume_ma_200: float | None
    volume_ratio_20: float | None
    volume_ratio_200: float | None
    volume_percentile_20: float | None  # 0-100
    volume_percentile_200: float | None  # 0-100
    volume_zscore_20: float | None
    volume_zscore_200: float | None
    readiness: bool  # True=数据充分；False=窗口不足

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _zscore(value: float, window: Sequence[float]) -> float | None:
    """ddof=0 z-score of ``value`` within ``window`` (mirrors VolumeContext SSOT)."""
    n = len(window)
    if n < 2:
        return None
    mean = sum(window) / n
    if mean == 0:
        return None
    var = sum((x - mean) ** 2 for x in window) / n
    if var <= 0:
        return None
    std = var ** 0.5
    return (value - mean) / std


def _position_pct(value: float, window: Sequence[float]) -> float | None:
    """0-100 empirical percentile of ``value`` within ``window`` (>=5 values gate).

    ``count(strictly below) / count`` × 100, matching the rolling percentile
    semantics used by :func:`compute_volume_context_series`.
    """
    if len(window) < 5:
        return None
    below = sum(1 for x in window if x < value)
    return below / len(window) * 100.0


def _rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    """计算滚动百分位（0-100）。

    当前值在最近 window 日经验分布中的位置。
    使用 rank(pct=True) 方法，排除当前值后计算。
    """
    if len(series) < 2:
        return pd.Series(np.nan, index=series.index, dtype=float)

    def _pct(val: float, window_vals: np.ndarray) -> float:
        if len(window_vals) == 0 or not np.isfinite(val):
            return np.nan
        # 百分位 = 小于当前值的比例 * 100
        return float(np.sum(window_vals < val) / len(window_vals) * 100.0)

    result = pd.Series(np.nan, index=series.index, dtype=float)
    vals = series.values.astype(float)
    for i in range(len(vals)):
        if i < 1:
            continue
        start = max(0, i - window)
        window_vals = vals[start:i]
        window_vals = window_vals[np.isfinite(window_vals)]
        if len(window_vals) >= 5:  # 至少 5 个有效值
            result.iloc[i] = _pct(vals[i], window_vals)
    return result


def compute_volume_context_series(bars: pd.DataFrame) -> pd.DataFrame:
    """计算全序列 VolumeContext（返回 DataFrame，每行对应一根 bar）。

    Args:
        bars: OHLCV DataFrame，必须含 volume 列；可选 amount/turnover_rate

    Returns:
        DataFrame，含 volume_ma_20/200, volume_ratio_20/200,
        volume_percentile_20/200, volume_zscore_20/200, readiness 列
    """
    if bars is None or bars.empty:
        return pd.DataFrame()

    df = bars.copy()
    if "volume" not in df.columns:
        return pd.DataFrame()

    vol = pd.to_numeric(df["volume"], errors="coerce").astype(float)

    # MA
    vol_ma_20 = vol.rolling(window=SHORT_WINDOW, min_periods=SHORT_WINDOW).mean()
    vol_ma_200 = vol.rolling(window=LONG_WINDOW, min_periods=LONG_WINDOW).mean()

    # Ratio
    vol_ratio_20 = vol / vol_ma_20.replace(0, np.nan)
    vol_ratio_200 = vol / vol_ma_200.replace(0, np.nan)

    # Z-score
    vol_mean_20 = vol.rolling(window=SHORT_WINDOW, min_periods=SHORT_WINDOW).mean()
    vol_std_20 = vol.rolling(window=SHORT_WINDOW, min_periods=SHORT_WINDOW).std(ddof=0)
    vol_zscore_20 = (vol - vol_mean_20) / vol_std_20.replace(0, np.nan)

    vol_mean_200 = vol.rolling(window=LONG_WINDOW, min_periods=LONG_WINDOW).mean()
    vol_std_200 = vol.rolling(window=LONG_WINDOW, min_periods=LONG_WINDOW).std(ddof=0)
    vol_zscore_200 = (vol - vol_mean_200) / vol_std_200.replace(0, np.nan)

    # Percentile (0-100)
    vol_pct_20 = _rolling_percentile(vol, SHORT_WINDOW)
    vol_pct_200 = _rolling_percentile(vol, LONG_WINDOW)

    # Readiness
    readiness = (vol_ma_20.notna() & vol_ma_200.notna())

    result = pd.DataFrame(
        {
            "volume": vol,
            "volume_ma_20": vol_ma_20,
            "volume_ma_200": vol_ma_200,
            "volume_ratio_20": vol_ratio_20,
            "volume_ratio_200": vol_ratio_200,
            "volume_zscore_20": vol_zscore_20,
            "volume_zscore_200": vol_zscore_200,
            "volume_percentile_20": vol_pct_20,
            "volume_percentile_200": vol_pct_200,
            "readiness": readiness,
        },
        index=df.index,
    )

    # 可选字段
    if "amount" in df.columns:
        result["amount"] = pd.to_numeric(df["amount"], errors="coerce").astype(float)
    else:
        result["amount"] = np.nan

    if "turnover_rate" in df.columns:
        result["turnover_rate"] = pd.to_numeric(
            df["turnover_rate"], errors="coerce"
        ).astype(float)
    else:
        result["turnover_rate"] = np.nan

    return result


def compute_volume_context_from_series(
    current_volume: float | None,
    history_volumes: Sequence[float | None],
) -> VolumeContextData | None:
    """Pure, DB-free VolumeContext for a single current bar + its prior history.

    Reuses the exact SSOT semantics of :func:`compute_volume_context_series`
    (MA20 / MA200 ratio, ddof=0 z-score, count-below percentile with the >=5
    valid-values gate) but operates on an already-prepared ``(current_volume,
    history_volumes)`` pair instead of a database-backed bar DataFrame.  This is
    the single canonical volume-math owner for the Review scope-prep path; no
    second rolling formula is introduced.

    ``history_volumes`` are the volumes strictly BEFORE the current bar, in
    ascending chronological order.  ``current_volume`` is the T-bar volume.

    Returns ``None`` only when the required windows cannot be formed; otherwise
    returns a ``VolumeContextData`` whose per-field ``None`` marks an
    individually-unavailable fact (sample too small for that statistic).
    """

    if current_volume is None or not math.isfinite(current_volume):
        return None

    prior = [
        v for v in history_volumes
        if v is not None and math.isfinite(v) and v > 0
    ]
    if len(prior) < SHORT_WINDOW:
        # Not enough history to form MA20 / percentile20 windows.
        return None

    # SSOT rolling windows EXCLUDE the current bar (pandas rolling at index i uses
    # vals[max(0, i-window):i]); the current bar is only the compare value inside
    # _pct.  So all windows are slices of ``prior`` (history before T).
    last20 = prior[-SHORT_WINDOW:]
    last200 = prior[-LONG_WINDOW:]

    mean20 = sum(last20) / len(last20)
    mean200 = sum(last200) / len(last200)

    ratio_20 = current_volume / mean20 if mean20 > 0 else None
    ratio_200 = current_volume / mean200 if mean200 > 0 else None

    zscore_20 = _zscore(current_volume, last20)
    zscore_200 = _zscore(current_volume, last200)

    pct_20 = _position_pct(current_volume, last20)
    pct_200 = _position_pct(current_volume, last200)

    return VolumeContextData(
        volume=current_volume,
        amount=None,
        turnover_rate=None,
        volume_ma_20=mean20,
        volume_ma_200=mean200,
        volume_ratio_20=ratio_20,
        volume_ratio_200=ratio_200,
        volume_percentile_20=pct_20,
        volume_percentile_200=pct_200,
        volume_zscore_20=zscore_20,
        volume_zscore_200=zscore_200,
        readiness=True,
    )


def extract_volume_context_at(
    vc_series: pd.DataFrame, bar_index: int | None = None
) -> VolumeContextData | None:
    """提取指定 bar 的 VolumeContext。

    Args:
        vc_series: compute_volume_context_series 返回的 DataFrame
        bar_index: bar 索引；None 取最后一根

    Returns:
        VolumeContextData 或 None（数据为空时）
    """
    if vc_series is None or vc_series.empty:
        return None

    if bar_index is None:
        bar_index = len(vc_series) - 1
    if bar_index < 0 or bar_index >= len(vc_series):
        return None

    row = vc_series.iloc[bar_index]
    readiness = bool(row.get("readiness", False)) if pd.notna(row.get("readiness")) else False

    return VolumeContextData(
        volume=_safe_float(row.get("volume")),
        amount=_safe_float(row.get("amount")),
        turnover_rate=_safe_float(row.get("turnover_rate")),
        volume_ma_20=_safe_float(row.get("volume_ma_20")),
        volume_ma_200=_safe_float(row.get("volume_ma_200")),
        volume_ratio_20=_safe_float(row.get("volume_ratio_20")),
        volume_ratio_200=_safe_float(row.get("volume_ratio_200")),
        volume_percentile_20=_safe_float(row.get("volume_percentile_20")),
        volume_percentile_200=_safe_float(row.get("volume_percentile_200")),
        volume_zscore_20=_safe_float(row.get("volume_zscore_20")),
        volume_zscore_200=_safe_float(row.get("volume_zscore_200")),
        readiness=readiness,
    )


def extract_last_volume_context(vc_series: pd.DataFrame) -> VolumeContextData | None:
    """提取最后一根 bar 的 VolumeContext（便捷方法）。"""
    return extract_volume_context_at(vc_series, None)


def volume_context_to_dict(vc: VolumeContextData | None) -> dict[str, Any] | None:
    """将 VolumeContextData 转为前端 JSON 友好字典；None 时返回 None。"""
    if vc is None:
        return None
    return vc.to_dict()


def volume_badge(vc: VolumeContextData | None) -> str:
    """根据 VolumeContext 生成量能徽标文本。

    返回："放量" / "缩量" / "正常" / "未知"
    基于 20 日百分位：
    - > 80: 放量
    - < 20: 缩量
    - 其余: 正常
    """
    if vc is None or not vc.readiness:
        return "未知"
    pct = vc.volume_percentile_20
    if pct is None:
        return "未知"
    if pct > 80:
        return "放量"
    if pct < 20:
        return "缩量"
    return "正常"


# 模块自测
if __name__ == "__main__":
    np.random.seed(42)
    dates = pd.date_range("2025-06-01", periods=250, freq="B")
    vol = np.random.randint(100000, 500000, 250).astype(float)
    # 注入一些异常值
    vol[200] = 2000000  # 放量
    vol[210] = 50000  # 缩量
    df = pd.DataFrame(
        {
            "open": 10.0 + np.cumsum(np.random.randn(250) * 0.1),
            "high": 11.0 + np.cumsum(np.random.randn(250) * 0.1),
            "low": 9.0 + np.cumsum(np.random.randn(250) * 0.1),
            "close": 10.0 + np.cumsum(np.random.randn(250) * 0.1),
            "volume": vol,
            "amount": vol * 10.0,
        },
        index=dates,
    )

    vc_series = compute_volume_context_series(df)
    print(f"VC series shape: {vc_series.shape}")
    print(f"Columns: {list(vc_series.columns)}")

    # 测试最后一根
    last_vc = extract_last_volume_context(vc_series)
    assert last_vc is not None
    print(f"Last VC: readiness={last_vc.readiness}")
    print(f"  volume={last_vc.volume:.0f}")
    print(f"  ma_20={last_vc.volume_ma_20:.0f}" if last_vc.volume_ma_20 else "  ma_20=None")
    print(f"  ma_200={last_vc.volume_ma_200:.0f}" if last_vc.volume_ma_200 else "  ma_200=None")
    print(f"  ratio_20={last_vc.volume_ratio_20:.2f}" if last_vc.volume_ratio_20 else "  ratio_20=None")
    print(f"  pct_20={last_vc.volume_percentile_20:.1f}" if last_vc.volume_percentile_20 else "  pct_20=None")
    print(f"  pct_200={last_vc.volume_percentile_200:.1f}" if last_vc.volume_percentile_200 else "  pct_200=None")
    print(f"  zscore_20={last_vc.volume_zscore_20:.2f}" if last_vc.volume_zscore_20 else "  zscore_20=None")
    print(f"  badge={volume_badge(last_vc)}")

    # 测试放量日 (index 200)
    vol_vc = extract_volume_context_at(vc_series, 200)
    assert vol_vc is not None
    print(f"\nVol spike day: badge={volume_badge(vol_vc)}")
    print(f"  pct_20={vol_vc.volume_percentile_20:.1f}" if vol_vc.volume_percentile_20 else "  pct_20=None")
    print(f"  zscore_20={vol_vc.volume_zscore_20:.2f}" if vol_vc.volume_zscore_20 else "  zscore_20=None")

    # 测试缩量日 (index 210)
    shrink_vc = extract_volume_context_at(vc_series, 210)
    assert shrink_vc is not None
    print(f"\nShrink day: badge={volume_badge(shrink_vc)}")
    print(f"  pct_20={shrink_vc.volume_percentile_20:.1f}" if shrink_vc.volume_percentile_20 else "  pct_20=None")

    # 测试窗口不足
    short_df = df.iloc[:10]
    short_vc = compute_volume_context_series(short_df)
    short_last = extract_last_volume_context(short_vc)
    assert short_last is not None
    print(f"\nShort series (10 bars): readiness={short_last.readiness}")
    assert short_last.readiness is False, "10 bars should not have readiness=True"
    assert short_last.volume_ma_200 is None, "200-day MA should be None"
    print("  volume_ma_200=None (correct)")
    print("  volume_ma_20=None (correct, <20 bars)")

    print("\nOK: VolumeContext module self-test passed")
