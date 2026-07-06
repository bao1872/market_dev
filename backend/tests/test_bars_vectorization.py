"""向量化性能与正确性测试。

测试内容：
1. _df_to_upsert_records 向量化构建 records（与 iterrows 结果一致）
2. _df_to_responses 向量化转换（与 iterrows 结果一致）
3. pd.to_numeric 转换（与 apply(lambda) 结果一致）
4. 性能对比（向量化耗时 < iterrows 耗时）

How to Run:
    pytest tests/test_bars_vectorization.py -v
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal

import pandas as pd
import pytest

from app.api.bars import _df_to_responses
from app.repositories.bar_repository import _df_to_upsert_records
from app.schemas.bar import BarResponse

TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")


# ============================================================
# 辅助函数
# ============================================================


def _build_raw_df(n: int = 800) -> pd.DataFrame:
    """构造模拟 pytdx 返回的 raw DataFrame。

    包含 datetime/open/high/low/close/volume/amount/adj_factor 列。
    """
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "datetime": dates,
        "open": [10.0 + i * 0.01 for i in range(n)],
        "high": [10.5 + i * 0.01 for i in range(n)],
        "low": [9.8 + i * 0.01 for i in range(n)],
        "close": [10.2 + i * 0.01 for i in range(n)],
        "volume": [100000 + i * 100 for i in range(n)],
        "amount": [1000000.0 + i * 1000 for i in range(n)],
        "adj_factor": [1.0 + i * 0.0001 for i in range(n)],
    })


def _build_query_df(n: int = 800) -> pd.DataFrame:
    """构造模拟 DB 查询返回的 DataFrame（index 为 DatetimeIndex）。"""
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": [10.0 + i * 0.01 for i in range(n)],
        "high": [10.5 + i * 0.01 for i in range(n)],
        "low": [9.8 + i * 0.01 for i in range(n)],
        "close": [10.2 + i * 0.01 for i in range(n)],
        "volume": [100000 + i * 100 for i in range(n)],
        "amount": [1000000.0 + i * 1000 for i in range(n)],
        "adj_factor": [1.0 + i * 0.0001 for i in range(n)],
    }, index=dates)


# ============================================================
# 1. _df_to_upsert_records 向量化测试
# ============================================================


def test_upsert_records_daily() -> None:
    """日线 records 构建：is_daily=True，volume_multiplier=1。"""
    df = _build_raw_df(5)
    records = _df_to_upsert_records(df, TEST_INSTRUMENT_ID, is_daily=True)

    assert len(records) == 5
    r0 = records[0]
    assert r0["instrument_id"] == TEST_INSTRUMENT_ID
    assert "trade_date" in r0
    assert "trade_time" not in r0
    assert isinstance(r0["trade_date"], type(df["datetime"].iloc[0].date()))
    assert r0["open"] == Decimal("10.0")
    assert r0["close"] == Decimal("10.2")
    assert r0["volume"] == Decimal("100000")
    assert r0["adj_factor"] == Decimal("1.0")


def test_upsert_records_minute() -> None:
    """分钟线 records 构建：is_daily=False，volume_multiplier=1。"""
    dates = pd.date_range("2026-06-16 09:30", periods=5, freq="15min")
    df = pd.DataFrame({
        "datetime": dates,
        "open": [10.0] * 5,
        "high": [10.5] * 5,
        "low": [9.8] * 5,
        "close": [10.2] * 5,
        "volume": [100000] * 5,
        "amount": [1000000.0] * 5,
        "adj_factor": [1.0] * 5,
    })
    records = _df_to_upsert_records(df, TEST_INSTRUMENT_ID, is_daily=False)

    assert len(records) == 5
    r0 = records[0]
    assert "trade_time" in r0
    assert "trade_date" not in r0


def test_upsert_records_weekly_volume_multiplier() -> None:
    """周线 records 构建：volume_multiplier=Decimal('100')。"""
    df = _build_raw_df(3)
    records = _df_to_upsert_records(
        df, TEST_INSTRUMENT_ID, is_daily=True, volume_multiplier=Decimal("100")
    )

    # volume 应乘以 100
    assert records[0]["volume"] == Decimal("100000") * Decimal("100")
    assert records[1]["volume"] == Decimal("100100") * Decimal("100")


def test_upsert_records_empty() -> None:
    """空 DataFrame 返回空列表。"""
    df = pd.DataFrame()
    records = _df_to_upsert_records(df, TEST_INSTRUMENT_ID, is_daily=True)
    assert records == []


def test_upsert_records_consistency_with_iterrows() -> None:
    """向量化构建结果与 iterrows 构建结果一致。"""
    df = _build_raw_df(100)
    records_vec = _df_to_upsert_records(df, TEST_INSTRUMENT_ID, is_daily=True)

    # 手动 iterrows 构建（模拟原逻辑）
    records_iter = []
    for _, row in df.iterrows():
        dt = pd.to_datetime(row["datetime"])
        records_iter.append({
            "instrument_id": TEST_INSTRUMENT_ID,
            "trade_date": dt.date(),
            "open": Decimal(str(row["open"])),
            "high": Decimal(str(row["high"])),
            "low": Decimal(str(row["low"])),
            "close": Decimal(str(row["close"])),
            "volume": Decimal(str(row["volume"])),
            "amount": Decimal(str(row["amount"])),
            "adj_factor": Decimal(str(row["adj_factor"])),
        })

    assert len(records_vec) == len(records_iter)
    for rv, ri in zip(records_vec, records_iter, strict=False):
        assert rv["instrument_id"] == ri["instrument_id"]
        assert rv["trade_date"] == ri["trade_date"]
        assert rv["open"] == ri["open"]
        assert rv["high"] == ri["high"]
        assert rv["low"] == ri["low"]
        assert rv["close"] == ri["close"]
        assert rv["volume"] == ri["volume"]
        assert rv["amount"] == ri["amount"]
        assert rv["adj_factor"] == ri["adj_factor"]


# ============================================================
# 2. _df_to_responses 向量化测试
# ============================================================


def test_df_to_responses_daily() -> None:
    """日线 response 转换：timeframe=1d，trade_date 有值，trade_time=None。"""
    df = _build_query_df(5)
    responses = _df_to_responses(df, TEST_INSTRUMENT_ID, "1d")

    assert len(responses) == 5
    r0 = responses[0]
    assert isinstance(r0, BarResponse)
    assert r0.instrument_id == TEST_INSTRUMENT_ID
    assert r0.trade_date is not None
    assert r0.trade_time is None
    assert r0.open == 10.0
    assert r0.close == 10.2


def test_df_to_responses_intraday() -> None:
    """15min response 转换：timeframe=15m，trade_time 有值，trade_date=None。"""
    dates = pd.date_range("2026-06-16 09:30", periods=5, freq="15min")
    df = pd.DataFrame({
        "open": [10.0] * 5,
        "high": [10.5] * 5,
        "low": [9.8] * 5,
        "close": [10.2] * 5,
        "volume": [100000] * 5,
        "amount": [1000000.0] * 5,
        "adj_factor": [1.0] * 5,
    }, index=dates)
    responses = _df_to_responses(df, TEST_INSTRUMENT_ID, "15m")

    assert len(responses) == 5
    r0 = responses[0]
    assert r0.trade_time is not None
    assert r0.trade_date is None


def test_df_to_responses_empty() -> None:
    """空 DataFrame 返回空列表。"""
    df = pd.DataFrame()
    responses = _df_to_responses(df, TEST_INSTRUMENT_ID, "1d")
    assert responses == []


def test_df_to_responses_consistency_with_iterrows() -> None:
    """向量化转换结果与 iterrows 转换结果一致。"""
    df = _build_query_df(100)
    responses_vec = _df_to_responses(df, TEST_INSTRUMENT_ID, "1d")

    # 手动 iterrows 构建（模拟原逻辑）
    responses_iter = []
    for idx, row in df.iterrows():
        ts = pd.to_datetime(idx)
        responses_iter.append(BarResponse(
            instrument_id=TEST_INSTRUMENT_ID,
            trade_date=ts.date(),
            trade_time=None,
            open=float(row["open"]) if pd.notna(row["open"]) else 0.0,
            high=float(row["high"]) if pd.notna(row["high"]) else 0.0,
            low=float(row["low"]) if pd.notna(row["low"]) else 0.0,
            close=float(row["close"]) if pd.notna(row["close"]) else 0.0,
            volume=float(row["volume"]) if pd.notna(row["volume"]) else 0.0,
            amount=float(row["amount"]) if pd.notna(row["amount"]) else 0.0,
            adj_factor=float(row["adj_factor"]) if pd.notna(row["adj_factor"]) else 1.0,
        ))

    assert len(responses_vec) == len(responses_iter)
    for rv, ri in zip(responses_vec, responses_iter, strict=False):
        assert rv.instrument_id == ri.instrument_id
        assert rv.trade_date == ri.trade_date
        assert rv.open == ri.open
        assert rv.high == ri.high
        assert rv.low == ri.low
        assert rv.close == ri.close
        assert rv.volume == ri.volume
        assert rv.amount == ri.amount
        assert rv.adj_factor == ri.adj_factor


# ============================================================
# 3. pd.to_numeric 转换一致性测试
# ============================================================


def test_to_numeric_consistency() -> None:
    """pd.to_numeric 与 apply(lambda) 转换结果一致。"""
    # 构造含 None 和 Decimal 的 Series
    from decimal import Decimal as Dec

    s = pd.Series([Dec("10.5"), None, Dec("9.8"), Dec("11.2"), None])

    # 向量化方式
    result_vec = pd.to_numeric(s, errors="coerce")

    # 原 apply(lambda) 方式
    result_iter = s.apply(lambda x: float(x) if x is not None else None)

    # pd.to_numeric 将 None 转为 NaN，apply(lambda) 将 None 转为 None
    # NaN != None，但 pd.isna() 均为 True
    for i in range(len(s)):
        if pd.isna(result_vec.iloc[i]):
            assert pd.isna(result_iter.iloc[i]) or result_iter.iloc[i] is None
        else:
            assert result_vec.iloc[i] == result_iter.iloc[i]


def test_to_numeric_with_floats() -> None:
    """纯 float Series 的 pd.to_numeric 转换。"""
    s = pd.Series([10.5, 9.8, 11.2, 10.0])
    result = pd.to_numeric(s, errors="coerce")
    assert list(result) == [10.5, 9.8, 11.2, 10.0]


# ============================================================
# 4. 性能对比测试
# ============================================================


def test_performance_upsert_records() -> None:
    """性能对比：向量化 _df_to_upsert_records 耗时 < iterrows 耗时。"""
    df = _build_raw_df(2000)

    # warmup：首次调用有 pandas/numpy JIT 编译开销，预热避免 CI runner 性能波动误判
    _df_to_upsert_records(df, TEST_INSTRUMENT_ID, is_daily=True)

    # 向量化
    start = time.perf_counter()
    _df_to_upsert_records(df, TEST_INSTRUMENT_ID, is_daily=True)
    vec_time = time.perf_counter() - start

    # iterrows（模拟原逻辑）
    start = time.perf_counter()
    records_iter = []
    for _, row in df.iterrows():
        dt = pd.to_datetime(row["datetime"])
        records_iter.append({
            "instrument_id": TEST_INSTRUMENT_ID,
            "trade_date": dt.date(),
            "open": Decimal(str(row["open"])),
            "high": Decimal(str(row["high"])),
            "low": Decimal(str(row["low"])),
            "close": Decimal(str(row["close"])),
            "volume": Decimal(str(row["volume"])),
            "amount": Decimal(str(row["amount"])),
            "adj_factor": Decimal(str(row["adj_factor"])),
        })
    iter_time = time.perf_counter() - start

    print(f"\n  _df_to_upsert_records (n=2000): 向量化={vec_time*1000:.2f}ms, iterrows={iter_time*1000:.2f}ms, 提升={iter_time/vec_time:.1f}x")
    assert vec_time < iter_time, f"向量化({vec_time:.4f}s)应快于 iterrows({iter_time:.4f}s)"


def test_performance_df_to_responses() -> None:
    """性能对比：向量化 _df_to_responses 耗时 < iterrows 耗时。"""
    df = _build_query_df(2000)

    # warmup：首次调用有 pandas/numpy JIT 编译开销，预热避免 CI runner 性能波动误判
    _df_to_responses(df, TEST_INSTRUMENT_ID, "1d")

    # 向量化
    start = time.perf_counter()
    _df_to_responses(df, TEST_INSTRUMENT_ID, "1d")
    vec_time = time.perf_counter() - start

    # iterrows（模拟原逻辑）
    start = time.perf_counter()
    responses_iter = []
    for idx, row in df.iterrows():
        ts = pd.to_datetime(idx)
        responses_iter.append(BarResponse(
            instrument_id=TEST_INSTRUMENT_ID,
            trade_date=ts.date(),
            trade_time=None,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            amount=float(row["amount"]),
            adj_factor=float(row["adj_factor"]),
        ))
    iter_time = time.perf_counter() - start

    print(f"\n  _df_to_responses (n=2000): 向量化={vec_time*1000:.2f}ms, iterrows={iter_time*1000:.2f}ms, 提升={iter_time/vec_time:.1f}x")
    assert vec_time < iter_time, f"向量化({vec_time:.4f}s)应快于 iterrows({iter_time:.4f}s)"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
