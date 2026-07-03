"""prepare_node_cluster_bars 共享数据准备函数测试（TDD）。

测试 advice.md 第四节要求的共享函数：
- 输入：daily_bars、bars_15m、bars_minute 三个 DataFrame
- 处理：DatetimeIndex 排序 → 删除重复 index(keep=last) → 过滤未完成 Bar → tail(N)
- 输出：准备后的三个 DataFrame + 诊断元信息（实际根数/period/parameter_version）

测试场景：
1. 正常输入：daily=300/15m=1500/1m=10 → 输出 daily=250/15m=4000/1m=2
2. 重复 index：dedupe keep=last，确保使用最新一条
3. 超过 N 根：tail(N) 截断
4. 不足 N 根：返回实际根数，诊断字段记录真实根数（不静默改用其他参数）
5. DatetimeIndex 数据不依赖 date/datetime 列（仅用 index）
6. 诊断字段：input_daily_bars/input_15m_bars/input_minute_bars/primary_period/low_period/parameter_version

参考：
- /root/web_dev/advice.md 第四节
- /root/web_dev/backend/app/constants/indicator_contract.py
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from app.constants import indicator_contract as IC
from app.strategy_assets.algorithms.features.unified_volume_profile import (
    prepare_node_cluster_bars,
)

# ===== 工具函数 =====


def _make_bars(
    n: int,
    start: datetime,
    freq: str = "D",
    start_price: float = 10.0,
    seed: int = 42,
) -> pd.DataFrame:
    """生成合成的 OHLCV bars，index 为 DatetimeIndex。

    Args:
        n: 生成根数
        start: 起始时间
        freq: pandas 频率字符串（D/15min/1min）
        start_price: 起始价格
        seed: 随机种子

    Returns:
        DataFrame: index=DatetimeIndex, columns=open/high/low/close/volume/amount
    """
    np.random.seed(seed)
    dates = pd.date_range(start=start, periods=n, freq=freq)
    returns = np.random.uniform(-0.01, 0.01, size=n)
    close = start_price * np.cumprod(1 + returns)
    open_ = close * (1 + np.random.uniform(-0.005, 0.005, size=n))
    high = np.maximum(open_, close) * (1 + np.random.uniform(0, 0.005, size=n))
    low = np.minimum(open_, close) * (1 - np.random.uniform(0, 0.005, size=n))
    volume = np.random.randint(1_000_000, 10_000_000, size=n).astype(float)
    amount = volume * close

    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
        },
        index=dates,
    )


# ===== 测试场景 =====


def test_prepare_node_cluster_bars_normal_input():
    """场景1：正常输入截断到 250/4000/2。"""
    daily = _make_bars(300, datetime(2025, 1, 1), freq="D")
    bars_15m = _make_bars(IC.NODE_CLUSTER_LOW_BARS, datetime(2025, 1, 1), freq="15min")
    bars_1m = _make_bars(10, datetime(2025, 6, 18, 9, 30), freq="1min")

    result = prepare_node_cluster_bars(daily, bars_15m, bars_1m)

    assert len(result.daily) == IC.NODE_CLUSTER_PRIMARY_BARS  # 250
    assert len(result.bars_15m) == IC.NODE_CLUSTER_LOW_BARS  # 4000
    assert len(result.bars_minute) == IC.NODE_CLUSTER_MINUTE_BARS  # 2


def test_prepare_node_cluster_bars_dedup_keep_last():
    """场景2：重复 index 去重，保留最后一条。"""
    daily = _make_bars(250, datetime(2025, 1, 1), freq="D")
    # 复制最后一行并附加相同 index（模拟重复）
    last_row = daily.iloc[[-1]].copy()
    last_row["close"] = 999.99  # 标记为"最新"
    daily_dup = pd.concat([daily, last_row])

    result = prepare_node_cluster_bars(
        daily_dup, _make_bars(IC.NODE_CLUSTER_LOW_BARS, datetime(2025, 1, 1), freq="15min"),
        _make_bars(2, datetime(2025, 6, 18, 9, 30), freq="1min"),
    )

    assert len(result.daily) == 250
    # 最后一根 close 应为 999.99（keep=last 生效）
    assert result.daily["close"].iloc[-1] == pytest.approx(999.99)


def test_prepare_node_cluster_bars_truncate_over_limit():
    """场景3：超过 NODE_CLUSTER_LOW_BARS 根 15m 数据时只取最后 NODE_CLUSTER_LOW_BARS 根。"""
    bars_15m_extra = _make_bars(IC.NODE_CLUSTER_LOW_BARS + 500, datetime(2025, 1, 1), freq="15min")
    # 标记最后一根 close 为唯一值
    bars_15m_extra.iloc[-1, bars_15m_extra.columns.get_loc("close")] = 888.88

    result = prepare_node_cluster_bars(
        _make_bars(250, datetime(2025, 1, 1), freq="D"),
        bars_15m_extra,
        _make_bars(2, datetime(2025, 6, 18, 9, 30), freq="1min"),
    )

    assert len(result.bars_15m) == IC.NODE_CLUSTER_LOW_BARS
    # 最后一根 close 应为 888.88（tail 截断保留最新）
    assert result.bars_15m["close"].iloc[-1] == pytest.approx(888.88)


def test_prepare_node_cluster_bars_insufficient_records_actual_count():
    """场景4：数据不足时记录实际根数，不静默改用其他参数。"""
    daily_short = _make_bars(100, datetime(2025, 1, 1), freq="D")  # < 250
    bars_15m_short = _make_bars(500, datetime(2025, 1, 1), freq="15min")  # < 4000
    bars_1m_short = _make_bars(1, datetime(2025, 6, 18, 9, 30), freq="1min")  # < 2

    result = prepare_node_cluster_bars(daily_short, bars_15m_short, bars_1m_short)

    # 不足时仍返回实际根数（不抛异常、不静默替换参数）
    assert len(result.daily) == 100
    assert len(result.bars_15m) == 500
    assert len(result.bars_minute) == 1

    # 诊断字段必须记录实际根数
    meta = result.profile_meta
    assert meta["input_daily_bars"] == 100
    assert meta["input_15m_bars"] == 500
    assert meta["input_minute_bars"] == 1


def test_prepare_node_cluster_bars_no_date_column_required():
    """场景5：DataFrame 仅用 DatetimeIndex，不依赖 date/datetime 列。

    模拟 bar_repository 返回的 DataFrame：index=DatetimeIndex(trade_date/trade_time)，
    无 date/datetime 列。函数应正常工作，不抛 KeyError。
    """
    daily = _make_bars(250, datetime(2025, 1, 1), freq="D")
    bars_15m = _make_bars(IC.NODE_CLUSTER_LOW_BARS, datetime(2025, 1, 1), freq="15min")
    bars_1m = _make_bars(2, datetime(2025, 6, 18, 9, 30), freq="1min")

    # 确认无 date/datetime 列
    assert "date" not in daily.columns
    assert "datetime" not in daily.columns
    assert "date" not in bars_15m.columns
    assert "datetime" not in bars_15m.columns

    # 应正常执行，不抛 KeyError
    result = prepare_node_cluster_bars(daily, bars_15m, bars_1m)
    assert len(result.daily) == 250
    assert len(result.bars_15m) == IC.NODE_CLUSTER_LOW_BARS
    assert len(result.bars_minute) == 2


def test_prepare_node_cluster_bars_profile_meta_diagnostic_fields():
    """场景6：profile_meta 包含 6 个诊断字段。"""
    daily = _make_bars(250, datetime(2025, 1, 1), freq="D")
    bars_15m = _make_bars(IC.NODE_CLUSTER_LOW_BARS, datetime(2025, 1, 1), freq="15min")
    bars_1m = _make_bars(2, datetime(2025, 6, 18, 9, 30), freq="1min")

    result = prepare_node_cluster_bars(daily, bars_15m, bars_1m)

    meta = result.profile_meta
    required_keys = {
        "input_daily_bars",
        "input_15m_bars",
        "input_minute_bars",
        "primary_period",
        "low_period",
        "parameter_version",
    }
    assert required_keys.issubset(meta.keys()), f"缺少字段: {required_keys - meta.keys()}"

    # 验证字段值
    assert meta["input_daily_bars"] == 250
    assert meta["input_15m_bars"] == IC.NODE_CLUSTER_LOW_BARS
    assert meta["input_minute_bars"] == 2
    assert meta["primary_period"] == IC.NODE_CLUSTER_PRIMARY_PERIOD  # "1d"
    assert meta["low_period"] == IC.NODE_CLUSTER_LOW_PERIOD  # "15m"
    # parameter_version 应为字符串，标识当前参数版本
    assert isinstance(meta["parameter_version"], str)
    assert len(meta["parameter_version"]) > 0


def test_prepare_node_cluster_bars_sort_index_ascending():
    """场景7：DatetimeIndex 按升序排序后取 tail(N)。"""
    # 生成倒序的 daily（最新在前）
    daily = _make_bars(300, datetime(2025, 1, 1), freq="D")
    daily_reverse = daily.iloc[::-1]  # 倒序

    result = prepare_node_cluster_bars(
        daily_reverse,
        _make_bars(IC.NODE_CLUSTER_LOW_BARS, datetime(2025, 1, 1), freq="15min"),
        _make_bars(2, datetime(2025, 6, 18, 9, 30), freq="1min"),
    )

    assert len(result.daily) == 250
    # 升序后最后一根应为 daily 倒序前的第一根（最新）
    assert result.daily.index[-1] == daily.index[-1]


def test_prepare_node_cluster_bars_empty_inputs():
    """场景8：空 DataFrame 输入不抛异常，返回空结果与零值诊断字段。"""
    empty_df = pd.DataFrame(
        columns=["open", "high", "low", "close", "volume", "amount"]
    )

    result = prepare_node_cluster_bars(empty_df, empty_df, empty_df)

    assert len(result.daily) == 0
    assert len(result.bars_15m) == 0
    assert len(result.bars_minute) == 0

    meta = result.profile_meta
    assert meta["input_daily_bars"] == 0
    assert meta["input_15m_bars"] == 0
    assert meta["input_minute_bars"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
