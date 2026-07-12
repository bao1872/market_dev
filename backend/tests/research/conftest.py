"""共享 fixtures for regime_discovery 测试。

所有测试不连真实 DB，使用固定 seed 与构造的样本 DataFrame。
"""

# ruff: noqa: N802, N803, N806

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def seed() -> int:
    return 42


@pytest.fixture
def rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def _make_trade_dates(n_days: int = 30, start: date | None = None) -> list[date]:
    """生成连续交易日期（跳过周末）。"""
    if start is None:
        start = date(2026, 6, 2)  # 周二
    dates: list[date] = []
    cur = start
    while len(dates) < n_days:
        if cur.weekday() < 5:
            dates.append(cur)
        cur += timedelta(days=1)
    return dates


@pytest.fixture
def sample_matrix_df() -> pd.DataFrame:
    """构造样本研究矩阵 DataFrame（2 股票 × 20 日 = 40 行）。

    包含 FEATURE_MATRIX_COLUMNS 的所有列。
    """
    n_inst = 2
    n_days = 20
    trade_dates = _make_trade_dates(n_days)
    inst_ids = [uuid4() for _ in range(n_inst)]
    rows: list[dict] = []
    rng_local = np.random.default_rng(42)
    for inst_id in inst_ids:
        for td in trade_dates:
            row: dict = {
                "instrument_id": inst_id,
                "symbol": "000001" if inst_id == inst_ids[0] else "600000",
                "trade_date": td,
                # causal (16)
                "causal_atr": float(rng_local.normal(0.5, 0.1)),
                "causal_bb_percent_b": float(rng_local.normal(0.5, 0.2)),
                "causal_bb_bandwidth_pct": float(abs(rng_local.normal(0.1, 0.02))),
                "causal_sqzmom_val": float(rng_local.normal(0, 0.01)),
                "causal_sqzmom_delta_1": float(rng_local.normal(0, 0.005)),
                "causal_volume_ratio_20": float(abs(rng_local.normal(1.0, 0.3))),
                "causal_volume_percentile_120": float(rng_local.uniform(0, 1)),
                "causal_active_swing_dir": "1",
                "causal_active_swing_high": float(rng_local.normal(11, 0.5)),
                "causal_active_swing_low": float(rng_local.normal(9, 0.5)),
                "causal_developing_swing_dir": "0",
                "causal_developing_swing_high": float(rng_local.normal(10.5, 0.3)),
                "causal_developing_swing_low": float(rng_local.normal(9.5, 0.3)),
                "causal_dsa_confirmed_segment": int(rng_local.integers(0, 5)),
                "causal_dsa_confirmed_direction": str(int(rng_local.choice([-1, 0, 1]))),
                "causal_dsa_confirmed_age_bars": int(rng_local.integers(0, 20)),
                # confirmed_delay (4)
                "confirmed_delay_confirmed_swing_high": float(rng_local.normal(11, 0.5)),
                "confirmed_delay_confirmed_swing_low": float(rng_local.normal(9, 0.5)),
                "confirmed_delay_bars_since_confirmed_swing_high": int(rng_local.integers(0, 10)),
                "confirmed_delay_bars_since_confirmed_swing_low": int(rng_local.integers(0, 10)),
            }
            rows.append(row)
    return pd.DataFrame(rows)


@pytest.fixture
def sample_close_df(sample_matrix_df: pd.DataFrame) -> pd.DataFrame:
    """构造与 sample_matrix_df 匹配的 close 价格 DataFrame。"""
    rows: list[dict] = []
    rng_local = np.random.default_rng(43)
    for _idx, row in sample_matrix_df.iterrows():
        rows.append({
            "instrument_id": row["instrument_id"],
            "trade_date": row["trade_date"],
            "close": float(rng_local.normal(10, 0.3)),
        })
    return pd.DataFrame(rows)


@pytest.fixture
def sample_features_df(sample_matrix_df: pd.DataFrame, sample_close_df: pd.DataFrame) -> pd.DataFrame:
    """构造已 build_features 的样本 DataFrame（含 17 个聚类特征）。"""
    from app.research.regime_discovery.feature_builder import build_features
    return build_features(sample_matrix_df, sample_close_df)


@pytest.fixture
def sample_X(sample_features_df: pd.DataFrame) -> np.ndarray:
    """构造用于聚类的 float32 矩阵。"""
    from app.research.regime_discovery.feature_builder import CLUSTERING_FEATURE_WHITELIST
    df = sample_features_df[CLUSTERING_FEATURE_WHITELIST].apply(
        pd.to_numeric, errors="coerce"
    )
    df = df.replace([np.inf, -np.inf], np.nan).dropna()
    return df.to_numpy(dtype=np.float32)


@pytest.fixture
def sample_clusterable_X() -> np.ndarray:
    """构造 3 簇可分数据（300 行 × 2 维），用于聚类测试。"""
    rng = np.random.default_rng(42)
    n_per = 100
    a = rng.normal([0, 0], 0.3, (n_per, 2))
    b = rng.normal([5, 5], 0.3, (n_per, 2))
    c = rng.normal([10, 0], 0.3, (n_per, 2))
    return np.vstack([a, b, c]).astype(np.float32)
