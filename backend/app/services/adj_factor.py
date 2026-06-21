"""前复权计算服务（向量化实现）。

从 ref/交易/datasource/adj_factor.py 迁移，关键改进：
1. 解耦 DB 访问：原函数内部读 stock_adj_factor 表，迁移后接收 adj_factor_df 参数，
   由调用方（repository）负责提供全量复权因子，服务层纯计算，可独立测试。
2. 向量化：用 pd.merge_asof 替代 missing dates 的 for 循环；
   用 DataFrame.multiply 替代逐列 for 循环。
3. DRY：日线与分钟线前复权逻辑相同，核心实现只写一份（_apply_adj_factor_core），
   apply_adj_factor 与 apply_adj_factor_intraday 为薄封装。

前复权公式：前复权价格 = 不复权价格 × (历史 adj_factor / 最新 adj_factor)
- volume/amount 不复权（仅 OHLC 调整）
- 缺失日期的 adj_factor 用最近前一交易日 ffill；仍缺失用 latest_adj 兜底

Inputs:
    bars_df: K线数据，index 为 DatetimeIndex（bar_time），含 open/high/low/close 列
    adj_factor_df: 复权因子，columns=[trade_date, adj_factor]，已按 trade_date 排序

Outputs:
    前复权后的 DataFrame（OHLC 调整，volume 不变）

How to Run:
    python -m app.services.adj_factor    # 自测：小样本验证前复权计算
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger("adj_factor")

# 需要前复权的价格列
_PRICE_COLS = ["open", "high", "low", "close"]


def _apply_adj_factor_core(
    bars_df: pd.DataFrame,
    adj_factor_df: pd.DataFrame,
    label: str,
) -> pd.DataFrame:
    """前复权核心实现（向量化，无 for 循环）。

    算法与原始 apply_adj_factor 一致：
    1. 取 latest_adj = adj_factor_df 最后一条（最新复权因子）
    2. 按 trade_date 映射每根 bar 的 adj_factor（用 merge_asof 向前填充）
    3. 缺失 adj_factor 用 latest_adj 兜底
    4. ratio = bar.adj_factor / latest_adj
    5. OHLC *= ratio（volume 不变）

    Args:
        bars_df: K线数据，index 为 DatetimeIndex，含 OHLC 列
        adj_factor_df: 复权因子，columns=[trade_date, adj_factor]
        label: 日志标签（"前复权" 或 "前复权-分钟"）

    Returns:
        前复权后的 DataFrame；输入为空或 adj_factor 为空时原样返回
    """
    if bars_df.empty:
        return bars_df
    if adj_factor_df is None or adj_factor_df.empty:
        logger.warning("[%s] adj_factor 为空，跳过复权", label)
        return bars_df

    df = bars_df.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    # 统一去掉时区，避免 merge_asof 时 bars_df(带时区) 与 adj_factor_df(不带时区) 类型不兼容
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    # 统一日期精度为微秒，避免 merge_asof 时 dtype('<M8[us]') 与 dtype('<M8[s]') 不兼容
    df.index = df.index.astype("datetime64[us]")

    # 准备 adj_factor：按 trade_date 排序去重（同一天取最后一条）
    adj = adj_factor_df[["trade_date", "adj_factor"]].copy()
    adj["trade_date"] = pd.to_datetime(adj["trade_date"])
    # 统一日期精度为微秒，避免 merge_asof 时 dtype('<M8[us]') 与 dtype('<M8[s]') 不兼容
    adj["trade_date"] = adj["trade_date"].astype("datetime64[us]")
    adj = adj.sort_values("trade_date").drop_duplicates("trade_date", keep="last")

    latest_adj = float(adj["adj_factor"].iloc[-1])
    if latest_adj == 0:
        logger.warning("[%s] latest_adj=0，跳过复权", label)
        return df

    high_before = float(df["high"].max()) if "high" in df.columns else float("nan")

    # 准备 bars 的 trade_date 列（normalize 到当天 00:00）
    # 重命名原 index 列避免与 adj 的 trade_date 列在 merge_asof 后冲突
    time_col = df.index.name or "index"
    df_reset = df.reset_index().rename(columns={time_col: "_bar_time"})
    df_reset["_trade_date"] = pd.to_datetime(df_reset["_bar_time"]).dt.normalize()
    df_reset = df_reset.sort_values("_trade_date")

    # merge_asof 向量化实现 ffill（替代原始 for 循环逐个查找 nearest）
    # 重命名 adj 的列避免与 bars 列冲突
    merged = pd.merge_asof(
        df_reset,
        adj.rename(columns={"adj_factor": "_adj", "trade_date": "_adj_trade_date"}),
        left_on="_trade_date",
        right_on="_adj_trade_date",
        direction="backward",
    )
    merged = merged.set_index("_bar_time")
    merged.index.name = time_col
    # merge_asof 前按 _trade_date 排序会打乱同一天内的时间顺序，需按 index 重新排序
    merged = merged.sort_index()

    # 优先使用 bars_df 中已有的 adj_factor 列（周线/月线从日线合成时 adj_factor 已正确）
    # 仅在 bars_df 无 adj_factor 列时使用 merge_asof 查找的结果
    if "adj_factor" in merged.columns:
        missing_count = 0
        ratio = merged["adj_factor"] / latest_adj
    else:
        # 缺失的 adj_factor 用 latest_adj 填充（与原始逻辑一致）
        missing_count = int(merged["_adj"].isna().sum())
        merged["_adj"] = merged["_adj"].fillna(latest_adj)
        ratio = merged["_adj"] / latest_adj

    # 向量化列乘法（替代原始 for col in ["open","high","low","close"] 循环）
    cols_to_adj = [c for c in _PRICE_COLS if c in merged.columns]
    merged[cols_to_adj] = merged[cols_to_adj].multiply(ratio, axis=0)

    # 清理临时列（保留 adj_factor 列，_df_to_responses 需要返回它）
    merged = merged.drop(
        columns=["_trade_date", "_adj_trade_date", "_adj"], errors="ignore"
    )

    high_after = float(merged["high"].max()) if "high" in merged.columns else float("nan")
    logger.info(
        "[%s] latest_adj=%.4f adj_records=%d missing_dates=%d high_max: %.2f -> %.2f",
        label, latest_adj, len(adj), missing_count, high_before, high_after,
    )

    return merged


def apply_adj_factor(
    bars_df: pd.DataFrame,
    adj_factor_df: pd.DataFrame,
) -> pd.DataFrame:
    """对日线/周线 K 线数据应用前复权转换。

    前复权公式：前复权价格 = 不复权价格 × (历史 adj_factor / 最新 adj_factor)

    Args:
        bars_df: 日线 K 线数据，index 为 DatetimeIndex（bar_time），含 OHLC 列
        adj_factor_df: 复权因子，columns=[trade_date, adj_factor]

    Returns:
        前复权后的 DataFrame（volume 不变，OHLC 调整）
    """
    return _apply_adj_factor_core(bars_df, adj_factor_df, label="前复权")


def apply_adj_factor_intraday(
    bars_df: pd.DataFrame,
    adj_factor_df: pd.DataFrame,
) -> pd.DataFrame:
    """对分钟级 K 线数据应用前复权转换（按交易日映射 adj_factor）。

    分钟数据的 trade_time 通过 normalize() 映射到交易日，
    再查找对应日期的 adj_factor 进行前复权转换。
    同一交易日内的所有分钟 bar 使用相同的 adj_factor。

    Args:
        bars_df: 分钟级 K 线数据，index 为 DatetimeIndex（trade_time），含 OHLC 列
        adj_factor_df: 复权因子，columns=[trade_date, adj_factor]

    Returns:
        前复权后的 DataFrame（volume 不变，OHLC 调整）
    """
    return _apply_adj_factor_core(bars_df, adj_factor_df, label="前复权-分钟")


def _build_sample_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """构造小样本数据用于自测（不复权价格 + 复权因子）。

    场景：某股票 3 个交易日，发生 1 次送转（adj_factor 变化）
    - 2026-06-16: adj=2.0（送转前）
    - 2026-06-17: adj=1.0（送转后，latest）
    - 2026-06-18: adj=1.0（latest）
    前复权后 06-16 的价格应 = 原价 × (2.0/1.0) = 原价 × 2
    """
    bars_data = {
        "open": [10.0, 5.0, 5.2],
        "high": [10.5, 5.5, 5.6],
        "low": [9.8, 4.8, 5.0],
        "close": [10.2, 5.2, 5.4],
        "volume": [100000, 200000, 150000],
    }
    dates = pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"])
    bars_df = pd.DataFrame(bars_data, index=dates)
    bars_df.index.name = "bar_time"

    adj_factor_df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"]),
        "adj_factor": [2.0, 1.0, 1.0],
    })
    return bars_df, adj_factor_df


if __name__ == "__main__":
    # 自测入口：用小样本验证前复权计算（无副作用，不写库表）
    logging.basicConfig(level=logging.INFO)

    bars_df, adj_df = _build_sample_data()
    print("=== 原始数据（不复权）===")
    print(bars_df)
    print("\n=== 复权因子 ===")
    print(adj_df)

    # 日线前复权
    qfq_df = apply_adj_factor(bars_df, adj_df)
    print("\n=== 日线前复权结果 ===")
    print(qfq_df)

    # 验证：06-16 的 close 应 = 10.2 × (2.0/1.0) = 20.4
    expected_close_0616 = 10.2 * (2.0 / 1.0)
    actual_close_0616 = float(qfq_df.loc[pd.Timestamp("2026-06-16"), "close"])
    print(f"\n06-16 close: expected={expected_close_0616}, actual={actual_close_0616}")
    assert abs(actual_close_0616 - expected_close_0616) < 1e-6, \
        f"日线前复权计算错误: expected={expected_close_0616}, actual={actual_close_0616}"

    # 验证：06-17/06-18 的 close 不变（adj=latest_adj=1.0，ratio=1.0）
    expected_close_0617 = 5.2
    actual_close_0617 = float(qfq_df.loc[pd.Timestamp("2026-06-17"), "close"])
    assert abs(actual_close_0617 - expected_close_0617) < 1e-6, \
        f"06-17 close 应不变: expected={expected_close_0617}, actual={actual_close_0617}"

    # 验证：volume 不变
    assert float(qfq_df.loc[pd.Timestamp("2026-06-16"), "volume"]) == 100000.0, \
        "volume 不应被复权调整"

    # 分钟线前复权（用相同样本，验证逻辑一致）
    minute_idx = pd.to_datetime([
        "2026-06-16 09:30", "2026-06-16 09:31",
        "2026-06-17 09:30", "2026-06-18 09:30",
    ])
    minute_bars = pd.DataFrame({
        "open": [10.0, 10.1, 5.0, 5.2],
        "high": [10.2, 10.3, 5.1, 5.3],
        "low": [9.9, 10.0, 4.9, 5.1],
        "close": [10.1, 10.2, 5.0, 5.2],
        "volume": [1000, 1200, 2000, 1500],
    }, index=minute_idx)
    minute_bars.index.name = "trade_time"

    qfq_minute = apply_adj_factor_intraday(minute_bars, adj_df)
    print("\n=== 分钟线前复权结果 ===")
    print(qfq_minute)

    # 验证：06-16 09:30 的 close 应 = 10.1 × 2.0 = 20.2
    expected_minute_close = 10.1 * 2.0
    actual_minute_close = float(qfq_minute.loc[minute_idx[0], "close"])
    print(f"\n06-16 09:30 close: expected={expected_minute_close}, actual={actual_minute_close}")
    assert abs(actual_minute_close - expected_minute_close) < 1e-6, \
        f"分钟前复权计算错误: expected={expected_minute_close}, actual={actual_minute_close}"

    # 验证空数据
    empty_df = apply_adj_factor(pd.DataFrame(), adj_df)
    assert empty_df.empty, "空输入应返回空"

    # 验证 adj_factor 为空
    no_adj = apply_adj_factor(bars_df, pd.DataFrame())
    pd.testing.assert_frame_equal(no_adj, bars_df)

    print("\n所有自测通过 ✓")
