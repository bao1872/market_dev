"""特征构建与公式登记 — 基础归一化特征 + 时序派生。

不复制 ATR/BB/SQZMOM/swing/DSA 公式，只读取已回补研究矩阵列做派生。
公式集中登记在 FEATURE_FORMULAS，禁止散落字面量。
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger("regime_discovery.feature_builder")

# =============================================================================
# 公式集中登记
# =============================================================================

FEATURE_FORMULAS: dict[str, str] = {
    # 基础归一化特征 (11)
    "atr_pct": "causal_atr / close",
    "bb_percent_b": "causal_bb_percent_b (winsorize 0.5/99.5)",
    "bb_bandwidth_log": "log1p(max(causal_bb_bandwidth_pct, 0))",
    "sqzmom_atr": "causal_sqzmom_val / causal_atr",
    "sqzmom_delta_atr": "causal_sqzmom_delta_1 / causal_atr",
    "volume_ratio_log": "log1p(max(causal_volume_ratio_20, 0))",
    "volume_percentile_120": "causal_volume_percentile_120",
    "active_swing_position": "clip((close - causal_active_swing_low) / "
    "(causal_active_swing_high - causal_active_swing_low), 0, 1)",
    "developing_swing_position": "clip((close - causal_developing_swing_low) / "
    "(causal_developing_swing_high - causal_developing_swing_low), 0, 1)",
    "dsa_dir": "sign(causal_dsa_confirmed_direction) ∈ {-1, 0, 1}",
    "dsa_age_log": "log1p(causal_dsa_confirmed_age_bars)",
    # 派生时序特征 (6)
    "bb_percent_b_delta_5": "bb_percent_b[i] - bb_percent_b[i-5]",
    "bandwidth_delta_5": "bb_bandwidth_log[i] - bb_bandwidth_log[i-5]",
    "sqzmom_atr_delta_5": "sqzmom_atr[i] - sqzmom_atr[i-5]",
    "volume_percentile_delta_5": "volume_percentile_120[i] - volume_percentile_120[i-5]",
    "return_5d": "close[i] / close[i-5] - 1",
    "realized_vol_10d": "std(close[i-9:i] / close[i-10:i-1] - 1, ddof=1)",
}

# 聚类白名单（按顺序）
CLUSTERING_FEATURE_WHITELIST: list[str] = list(FEATURE_FORMULAS.keys())

# 方向类特征（不做 winsorize/scaler/PCA）
DIRECTION_FEATURES: set[str] = {"dsa_dir"}

# 禁止进入聚类 X 的列前缀
FORBIDDEN_PREFIXES = ("hindsight_", "label_", "amount")

# 运行时填充的排除原因
_excluded_reasons: dict[str, str] = {}


def get_excluded_reasons() -> dict[str, str]:
    """返回被排除字段及原因。"""
    return dict(_excluded_reasons)


def validate_no_leakage(columns: list[str]) -> None:
    """硬阻断 hindsight/label/amount 字段进入聚类 X。

    Args:
        columns: 待验证的列名列表

    Raises:
        ValueError: 如果发现禁止的列
    """
    for col in columns:
        for prefix in FORBIDDEN_PREFIXES:
            if col.startswith(prefix):
                raise ValueError(
                    f"禁止字段 '{col}' 进入聚类 X："
                    f"{prefix} 类字段不得作为聚类输入"
                )


def build_features(
    matrix_df: pd.DataFrame,
    close_df: pd.DataFrame,
) -> pd.DataFrame:
    """从研究矩阵 + close 价格构建聚类特征。

    Args:
        matrix_df: 研究矩阵行，含 causal/confirmed_delay 列
        close_df: bars_daily 的 close 价格 (instrument_id, trade_date, close)

    Returns:
        DataFrame 含 instrument_id, trade_date, symbol + 17 个聚类特征列
    """
    _excluded_reasons.clear()
    validate_no_leakage(list(matrix_df.columns))

    # 合并 close 价格
    close_df = close_df.copy()
    close_df["trade_date"] = pd.to_datetime(close_df["trade_date"])
    matrix_df = matrix_df.copy()
    matrix_df["trade_date"] = pd.to_datetime(matrix_df["trade_date"])

    df = matrix_df.merge(
        close_df[["instrument_id", "trade_date", "close"]],
        on=["instrument_id", "trade_date"],
        how="left",
    )

    if df["close"].isna().all():
        _excluded_reasons["close"] = "bars_daily 无匹配 close 价格"
        raise RuntimeError("无法从 bars_daily 获取 close 价格")

    close_missing_rate = df["close"].isna().mean()
    if close_missing_rate > 0.5:
        _excluded_reasons["close"] = (
            f"bars_daily close 缺失率 {close_missing_rate:.1%} 过高"
        )

    # 基础归一化特征
    df = _compute_base_normalized(df)

    # 时序派生特征
    df = _compute_temporal_derivatives(df)

    # 检查特征是否成功构造
    for feat in CLUSTERING_FEATURE_WHITELIST:
        if feat not in df.columns:
            _excluded_reasons[feat] = f"特征构造失败：列 {feat} 不存在"
        elif df[feat].isna().all():
            _excluded_reasons[feat] = "特征全为 NaN：源列可能缺失"

    return df


def _compute_base_normalized(df: pd.DataFrame) -> pd.DataFrame:
    """计算 11 个基础归一化特征。"""
    close = df["close"].to_numpy(dtype=np.float64)

    # atr_pct = causal_atr / close
    atr = df["causal_atr"].to_numpy(dtype=np.float64)
    df["atr_pct"] = np.where(close > 0, atr / close, np.nan).astype(np.float32)

    # bb_percent_b (直接使用现有字段)
    df["bb_percent_b"] = df["causal_bb_percent_b"].astype(np.float32)

    # bb_bandwidth_log = log1p(max(bandwidth_pct, 0))
    bw = df["causal_bb_bandwidth_pct"].to_numpy(dtype=np.float64)
    df["bb_bandwidth_log"] = np.log1p(np.maximum(bw, 0.0)).astype(np.float32)

    # sqzmom_atr = sqzmom_val / atr
    sqz = df["causal_sqzmom_val"].to_numpy(dtype=np.float64)
    df["sqzmom_atr"] = np.where(atr > 0, sqz / atr, np.nan).astype(np.float32)

    # sqzmom_delta_atr = sqzmom_delta_1 / atr
    sqz_d = df["causal_sqzmom_delta_1"].to_numpy(dtype=np.float64)
    df["sqzmom_delta_atr"] = np.where(atr > 0, sqz_d / atr, np.nan).astype(np.float32)

    # volume_ratio_log = log1p(max(volume_ratio_20, 0))
    vr = df["causal_volume_ratio_20"].to_numpy(dtype=np.float64)
    df["volume_ratio_log"] = np.log1p(np.maximum(vr, 0.0)).astype(np.float32)

    # volume_percentile_120 (直接使用)
    df["volume_percentile_120"] = df["causal_volume_percentile_120"].astype(np.float32)

    # active_swing_position = clip((close - low) / (high - low), 0, 1)
    ash = df["causal_active_swing_high"].to_numpy(dtype=np.float64)
    asl = df["causal_active_swing_low"].to_numpy(dtype=np.float64)
    denom_ash = ash - asl
    asp = np.where(denom_ash > 0, (close - asl) / denom_ash, 0.5)
    df["active_swing_position"] = np.clip(asp, 0.0, 1.0).astype(np.float32)

    # developing_swing_position
    dsh = df["causal_developing_swing_high"].to_numpy(dtype=np.float64)
    dsl = df["causal_developing_swing_low"].to_numpy(dtype=np.float64)
    denom_dsh = dsh - dsl
    dsp = np.where(denom_dsh > 0, (close - dsl) / denom_dsh, 0.5)
    df["developing_swing_position"] = np.clip(dsp, 0.0, 1.0).astype(np.float32)

    # dsa_dir = sign(causal_dsa_confirmed_direction)
    dsa_dir_raw = df["causal_dsa_confirmed_direction"]
    # direction 可能是 string "1"/"0"/"-1" 或 numeric
    dsa_dir_numeric = pd.to_numeric(dsa_dir_raw, errors="coerce")
    df["dsa_dir"] = np.sign(dsa_dir_numeric.to_numpy(dtype=np.float64)).astype(np.float32)

    # dsa_age_log = log1p(age_bars)
    age = pd.to_numeric(df["causal_dsa_confirmed_age_bars"], errors="coerce")
    df["dsa_age_log"] = np.log1p(np.maximum(age.to_numpy(dtype=np.float64), 0.0)).astype(
        np.float32
    )

    return df


def _compute_temporal_derivatives(df: pd.DataFrame) -> pd.DataFrame:
    """按 instrument 时间排序计算 6 个时序派生特征。

    使用 groupby().shift() 向量化计算，避免 per-instrument for 循环
    和 df.loc[idx, ...] 赋值（5177 个 instrument 的循环会产生大量
    中间 copy，导致 RSS 峰值过高）。
    """
    df = df.sort_values(["instrument_id", "trade_date"]).reset_index(drop=True)
    g = df.groupby("instrument_id")

    # delta_5 系列：当前值 - 5 行前值（per instrument）
    df["bb_percent_b_delta_5"] = (
        df["bb_percent_b"] - g["bb_percent_b"].shift(5)
    ).astype(np.float32)
    df["bandwidth_delta_5"] = (
        df["bb_bandwidth_log"] - g["bb_bandwidth_log"].shift(5)
    ).astype(np.float32)
    df["sqzmom_atr_delta_5"] = (
        df["sqzmom_atr"] - g["sqzmom_atr"].shift(5)
    ).astype(np.float32)
    df["volume_percentile_delta_5"] = (
        df["volume_percentile_120"] - g["volume_percentile_120"].shift(5)
    ).astype(np.float32)

    # return_5d = close[i] / close[i-5] - 1
    close_f64 = df["close"].astype(np.float64)
    df["return_5d"] = (close_f64 / g["close"].shift(5) - 1.0).astype(np.float32)

    # realized_vol_10d = std(daily_return, 10) per instrument
    daily_ret = close_f64 / g["close"].shift(1) - 1.0
    df["realized_vol_10d"] = (
        daily_ret.groupby(df["instrument_id"]).rolling(10, min_periods=10).std(ddof=1)
        .reset_index(level=0, drop=True)
    ).astype(np.float32)

    return df


def get_feature_summary() -> dict[str, Any]:
    """返回特征清单摘要，用于 manifest。"""
    return {
        "feature_count": len(CLUSTERING_FEATURE_WHITELIST),
        "features": CLUSTERING_FEATURE_WHITELIST,
        "formulas": FEATURE_FORMULAS,
        "direction_features": list(DIRECTION_FEATURES),
        "excluded": get_excluded_reasons(),
    }
