"""前复权计算服务（向量化实现）。

从 ref/交易/datasource/adj_factor.py 迁移，关键改进：
1. 解耦 DB 访问：原函数内部读 stock_adj_factor 表，迁移后接收 adj_factor_df 参数，
   由调用方（repository）负责提供全量复权因子，服务层纯计算，可独立测试。
2. 向量化：用 pd.merge_asof 替代 missing dates 的 for 循环；
   用 DataFrame.multiply 替代逐列 for 循环。
3. DRY：日线与分钟线前复权逻辑相同，核心实现只写一份（_apply_adj_factor_core），
   apply_adj_factor 与 apply_adj_factor_intraday 为薄封装。

前复权公式（CHANGE-20260717-002 引入 adjustment_as_of）：
- 旧（as_of=None，向后兼容）：qfq_price = raw_price × (bar.adj_factor / latest_adj)
- 新（as_of 指定）：qfq_price = raw_price × (factor(bar_date) / factor(as_of))
  其中 factor(as_of) 取截至 as_of 的最近交易日因子（ffill），
  禁止未来除权事件泄漏（as_of 之后的事件不影响历史 qfq 价）。

因子语义（与 bar_repository._calculate_adj_factor 的 Chanlunpro preclose 公式一致）：
- 最新日期 adj_factor = 1.0
- 除权前 bar 的 adj_factor = 累积 event_factor（10送10 时 event_factor=0.5）
- 例：10送10，除权前 raw close=20、adj_factor=0.5，除权后 raw close=10、adj_factor=1.0
  → qfq = 20 × (0.5/1.0) = 10（价格连续，除权前价格下调到除权后基准）

- volume/amount 不复权（仅 OHLC 调整）
- 缺失日期的 adj_factor 用最近前一交易日 ffill；仍缺失用 latest_adj 兜底

Inputs:
    bars_df: K线数据，index 为 DatetimeIndex（bar_time），含 open/high/low/close 列
    adj_factor_df: 复权因子，columns=[trade_date, adj_factor]，已按 trade_date 排序
    as_of: 复权锚点日期（None=最新，向后兼容；date=point-in-time 复权）

Outputs:
    前复权后的 DataFrame（OHLC 调整，volume 不变）

How to Run:
    python -m app.services.adj_factor    # 自测：小样本验证前复权计算
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

logger = logging.getLogger("adj_factor")

# 需要前复权的价格列
_PRICE_COLS = ["open", "high", "low", "close"]


def _compute_denominator_factor(
    adj: pd.DataFrame, as_of: date | None, label: str
) -> tuple[float, bool]:
    """计算复权分母因子（denominator_factor）。

    - as_of=None：返回 latest_adj（最新因子，向后兼容旧公式 ratio=bar.adj_factor/latest_adj）
    - as_of 指定：取截至 as_of 的最近交易日因子（ffill）；
      as_of 早于第一个因子日期时用第一个；晚于最后一个时用最后一个；
      因子序列为空时返回 1.0 并标记 degraded。

    Args:
        adj: 已排序去重的因子序列，columns=[trade_date, adj_factor]
        as_of: 复权锚点日期（None=最新）
        label: 日志标签

    Returns:
        (denominator_factor, degraded)：degraded=True 表示因子源异常，调用方应标记降级
    """
    if adj.empty:
        return 1.0, True

    if as_of is None:
        return float(adj["adj_factor"].iloc[-1]), False

    # point-in-time：取截至 as_of 的最近交易日因子（ffill）
    as_of_ts = pd.Timestamp(as_of)
    eligible = adj[adj["trade_date"] <= as_of_ts]
    if not eligible.empty:
        return float(eligible["adj_factor"].iloc[-1]), False

    # as_of 早于第一个因子日期：用第一个因子（as_of 时点该因子已是"当前最新"）
    first_factor = float(adj["adj_factor"].iloc[0])
    if first_factor == 0:
        logger.warning("[%s] as_of=%s 早于首因子且首因子=0，返回 1.0 degraded", label, as_of)
        return 1.0, True
    logger.debug("[%s] as_of=%s 早于首因子日期，用首因子 %.6f", label, as_of, first_factor)
    return first_factor, False


def _apply_adj_factor_core(
    bars_df: pd.DataFrame,
    adj_factor_df: pd.DataFrame,
    label: str,
    as_of: date | None = None,
) -> pd.DataFrame:
    """前复权核心实现（向量化，无 for 循环）。

    公式（CHANGE-20260717-002）：
    - as_of=None（向后兼容）：ratio = bar.adj_factor / latest_adj
    - as_of 指定：ratio = factor(bar_date) / factor(as_of)
      factor(as_of) 由 _compute_denominator_factor 计算（ffill，禁止未来事件泄漏）

    算法步骤：
    1. 准备 adj_factor 序列（按 trade_date 排序去重）
    2. 计算 denominator_factor（latest_adj 或 factor(as_of)）
    3. 按 trade_date 映射每根 bar 的 adj_factor（用 merge_asof 向前填充）
    4. 缺失 adj_factor 用 denominator_factor 兜底
    5. ratio = bar.adj_factor / denominator_factor
    6. OHLC *= ratio（volume 不变）

    Args:
        bars_df: K线数据，index 为 DatetimeIndex，含 OHLC 列
        adj_factor_df: 复权因子，columns=[trade_date, adj_factor]
        label: 日志标签（"前复权" 或 "前复权-分钟"）
        as_of: 复权锚点日期（None=最新，向后兼容；date=point-in-time 复权）

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

    # 计算分母因子（denominator_factor）：latest_adj 或 factor(as_of)
    denominator_factor, degraded = _compute_denominator_factor(adj, as_of, label)
    if denominator_factor == 0:
        logger.warning("[%s] denominator_factor=0（as_of=%s），跳过复权", label, as_of)
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
        ratio = merged["adj_factor"] / denominator_factor
    else:
        # 缺失的 adj_factor 用 denominator_factor 填充（ratio=1.0，等价不复权该 bar）
        missing_count = int(merged["_adj"].isna().sum())
        merged["_adj"] = merged["_adj"].fillna(denominator_factor)
        ratio = merged["_adj"] / denominator_factor

    # 向量化列乘法（替代原始 for col in ["open","high","low","close"] 循环）
    cols_to_adj = [c for c in _PRICE_COLS if c in merged.columns]
    merged[cols_to_adj] = merged[cols_to_adj].multiply(ratio, axis=0)

    # 清理临时列（保留 adj_factor 列，_df_to_responses 需要返回它）
    merged = merged.drop(
        columns=["_trade_date", "_adj_trade_date", "_adj"], errors="ignore"
    )

    high_after = float(merged["high"].max()) if "high" in merged.columns else float("nan")
    logger.info(
        "[%s] denominator=%.4f as_of=%s adj_records=%d missing_dates=%d degraded=%s high_max: %.2f -> %.2f",
        label, denominator_factor, as_of, len(adj), missing_count, degraded,
        high_before, high_after,
    )

    return merged


def apply_adj_factor(
    bars_df: pd.DataFrame,
    adj_factor_df: pd.DataFrame,
    as_of: date | None = None,
) -> pd.DataFrame:
    """对日线/周线 K 线数据应用前复权转换。

    前复权公式：
    - as_of=None（向后兼容）：qfq = raw × (bar.adj_factor / latest_adj)
    - as_of 指定：qfq = raw × (factor(bar_date) / factor(as_of))

    Args:
        bars_df: 日线 K 线数据，index 为 DatetimeIndex（bar_time），含 OHLC 列
        adj_factor_df: 复权因子，columns=[trade_date, adj_factor]
        as_of: 复权锚点日期（None=最新；date=point-in-time，禁止未来事件泄漏）

    Returns:
        前复权后的 DataFrame（volume 不变，OHLC 调整）
    """
    return _apply_adj_factor_core(bars_df, adj_factor_df, label="前复权", as_of=as_of)


def apply_adj_factor_intraday(
    bars_df: pd.DataFrame,
    adj_factor_df: pd.DataFrame,
    as_of: date | None = None,
) -> pd.DataFrame:
    """对分钟级 K 线数据应用前复权转换（按交易日映射 adj_factor）。

    分钟数据的 trade_time 通过 normalize() 映射到交易日，
    再查找对应日期的 adj_factor 进行前复权转换。
    同一交易日内的所有分钟 bar 使用相同的 adj_factor。

    Args:
        bars_df: 分钟级 K 线数据，index 为 DatetimeIndex（trade_time），含 OHLC 列
        adj_factor_df: 复权因子，columns=[trade_date, adj_factor]
        as_of: 复权锚点日期（None=最新；date=point-in-time，禁止未来事件泄漏）

    Returns:
        前复权后的 DataFrame（volume 不变，OHLC 调整）
    """
    return _apply_adj_factor_core(
        bars_df, adj_factor_df, label="前复权-分钟", as_of=as_of
    )


def apply_adj_factor_with_as_of(
    bars_df: pd.DataFrame,
    adj_factor_df: pd.DataFrame,
    as_of: date,
    intraday: bool = False,
) -> pd.DataFrame:
    """显式 adjustment_as_of 的前复权（CHANGE-20260717-002）。

    公式：qfq_price = raw_price × factor(bar_date) / factor(as_of)
    factor(as_of) 取截至 as_of 的最近交易日因子（ffill），
    禁止未来除权事件泄漏。

    Args:
        bars_df: K 线数据，index 为 DatetimeIndex，含 OHLC 列
        adj_factor_df: 复权因子，columns=[trade_date, adj_factor]
        as_of: 复权锚点日期（必填）
        intraday: True 为分钟线（按交易日映射），False 为日线/周线/月线

    Returns:
        前复权后的 DataFrame（volume 不变，OHLC 调整）
    """
    label = "前复权-分钟-as_of" if intraday else "前复权-as_of"
    return _apply_adj_factor_core(bars_df, adj_factor_df, label=label, as_of=as_of)


def _build_sample_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """构造小样本数据用于自测（不复权价格 + 复权因子）。

    场景：10送10（每10股送10股），与 Chanlunpro preclose 公式一致
    - 2026-06-16: 除权前，raw close=20.0，adj_factor=0.5（累积 event_factor=0.5）
    - 2026-06-17: 除权日，raw close=10.0，adj_factor=1.0（latest，无后续事件）
    - 2026-06-18: 除权后，raw close=10.2，adj_factor=1.0（latest）

    Chanlunpro 推导（10送10）：
    - close_{D-1}=20（除权前收盘）
    - preclose=(20×10 - 0 + 0×0)/(10+0+10)=200/20=10
    - event_factor=preclose/close_{D-1}=10/20=0.5
    - 累积因子（除权前 bar）=1.0×0.5=0.5
    前复权（as_of=None，denominator=latest_adj=1.0）：
    - 06-16: qfq=20.0×(0.5/1.0)=10.0（除权前价格下调到除权后基准，价格连续）
    - 06-17: qfq=10.0×(1.0/1.0)=10.0
    - 06-18: qfq=10.2×(1.0/1.0)=10.2
    """
    bars_data = {
        "open": [20.0, 10.0, 10.1],
        "high": [20.5, 10.2, 10.3],
        "low": [19.8, 9.8, 10.0],
        "close": [20.0, 10.0, 10.2],
        "volume": [100000, 200000, 150000],
    }
    dates = pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"])
    bars_df = pd.DataFrame(bars_data, index=dates)
    bars_df.index.name = "bar_time"

    adj_factor_df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"]),
        "adj_factor": [0.5, 1.0, 1.0],
    })
    return bars_df, adj_factor_df


def _build_no_event_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """无公司行为场景：所有 adj_factor=1.0，qfq=raw。"""
    bars_df = pd.DataFrame({
        "open": [10.0, 10.2, 10.1],
        "high": [10.5, 10.6, 10.4],
        "low": [9.8, 10.0, 9.9],
        "close": [10.2, 10.4, 10.3],
        "volume": [100000, 120000, 110000],
    }, index=pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"]))
    bars_df.index.name = "bar_time"
    adj_factor_df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"]),
        "adj_factor": [1.0, 1.0, 1.0],
    })
    return bars_df, adj_factor_df


def _build_cash_dividend_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """现金分红场景：每10股派1元（fenhong=1）。

    Chanlunpro 推导（close_{D-1}=10，fenhong=1，songzhuangu=0，peigu=0）：
    - preclose=(10×10 - 1 + 0)/(10+0+0)=99/10=9.9
    - event_factor=9.9/10=0.99
    - 除权前 adj_factor=0.99，除权后 adj_factor=1.0
    qfq（as_of=None）= 10.0×(0.99/1.0)=9.9（除权前价格下调0.1元，价格连续）
    """
    bars_df = pd.DataFrame({
        "open": [10.0, 9.9, 9.95],
        "high": [10.2, 10.0, 10.05],
        "low": [9.8, 9.8, 9.85],
        "close": [10.0, 9.9, 9.95],
        "volume": [100000, 150000, 120000],
    }, index=pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"]))
    bars_df.index.name = "bar_time"
    adj_factor_df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"]),
        "adj_factor": [0.99, 1.0, 1.0],
    })
    return bars_df, adj_factor_df


def _build_rights_issue_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """配股场景：每10股配3股，配股价5元（peigu=3，peigujia=5）。

    Chanlunpro 推导（close_{D-1}=10，fenhong=0，songzhuangu=0，peigu=3，peigujia=5）：
    - preclose=(10×10 - 0 + 3×5)/(10+0+3)=115/13≈8.8462
    - event_factor=8.8462/10≈0.88462
    - 除权前 adj_factor≈0.88462，除权后 adj_factor=1.0
    qfq（as_of=None）= 10.0×(0.88462/1.0)≈8.8462（价格连续）
    """
    bars_df = pd.DataFrame({
        "open": [10.0, 8.85, 8.90],
        "high": [10.2, 8.95, 9.00],
        "low": [9.8, 8.80, 8.85],
        "close": [10.0, 8.85, 8.90],
        "volume": [100000, 130000, 125000],
    }, index=pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"]))
    bars_df.index.name = "bar_time"
    adj_factor_df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-06-16", "2026-06-17", "2026-06-18"]),
        "adj_factor": [0.88462, 1.0, 1.0],
    })
    return bars_df, adj_factor_df


if __name__ == "__main__":
    # 自测入口：验证前复权计算（无副作用，不写库表）
    # 覆盖：10送10 / 现金分红 / 配股 / 无事件 / 事件日缺前收 / 因子源失败 / as_of 无未来泄漏
    import inspect
    from datetime import date as _date

    logging.basicConfig(level=logging.INFO)

    def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
        return abs(a - b) < tol

    # ===== 1. 10送10场景（_build_sample_data）=====
    print("=== 测试1: 10送10 ===")
    bars_df, adj_df = _build_sample_data()
    qfq_df = apply_adj_factor(bars_df, adj_df)
    # 06-16: qfq=20.0×(0.5/1.0)=10.0
    assert _approx(float(qfq_df.loc[pd.Timestamp("2026-06-16"), "close"]), 10.0), \
        f"10送10 06-16 qfq close 应=10.0, got {qfq_df.loc[pd.Timestamp('2026-06-16'), 'close']}"
    # 06-17/06-18 不变
    assert _approx(float(qfq_df.loc[pd.Timestamp("2026-06-17"), "close"]), 10.0)
    assert _approx(float(qfq_df.loc[pd.Timestamp("2026-06-18"), "close"]), 10.2)
    # volume 不变
    assert float(qfq_df.loc[pd.Timestamp("2026-06-16"), "volume"]) == 100000.0
    print("10送10 qfq 价格连续 10.0→10.0→10.2 ✓")

    # ===== 2. 现金分红场景 =====
    print("=== 测试2: 现金分红（每10股派1元）===")
    cd_bars, cd_adj = _build_cash_dividend_data()
    cd_qfq = apply_adj_factor(cd_bars, cd_adj)
    # 06-16: qfq=10.0×(0.99/1.0)=9.9
    assert _approx(float(cd_qfq.loc[pd.Timestamp("2026-06-16"), "close"]), 9.9), \
        f"现金分红 06-16 qfq close 应=9.9, got {cd_qfq.loc[pd.Timestamp('2026-06-16'), 'close']}"
    print("现金分红 06-16 qfq=9.9（下调0.1元）✓")

    # ===== 3. 配股场景 =====
    print("=== 测试3: 配股（10配3，配股价5）===")
    ri_bars, ri_adj = _build_rights_issue_data()
    ri_qfq = apply_adj_factor(ri_bars, ri_adj)
    # 06-16: qfq=10.0×(0.88462/1.0)≈8.8462
    assert _approx(float(ri_qfq.loc[pd.Timestamp("2026-06-16"), "close"]), 8.8462, 1e-4), \
        f"配股 06-16 qfq close 应≈8.8462, got {ri_qfq.loc[pd.Timestamp('2026-06-16'), 'close']}"
    print("配股 06-16 qfq≈8.8462 ✓")

    # ===== 4. 无事件场景 =====
    print("=== 测试4: 无公司行为 ===")
    ne_bars, ne_adj = _build_no_event_data()
    ne_qfq = apply_adj_factor(ne_bars, ne_adj)
    # 所有 factor=1.0，qfq=raw
    for d in ["2026-06-16", "2026-06-17", "2026-06-18"]:
        raw_close = float(ne_bars.loc[pd.Timestamp(d), "close"])
        qfq_close = float(ne_qfq.loc[pd.Timestamp(d), "close"])
        assert _approx(raw_close, qfq_close), \
            f"无事件 {d} qfq 应=raw: raw={raw_close}, qfq={qfq_close}"
    print("无事件 qfq=raw ✓")

    # ===== 5. 事件日缺前收（as_of 早于首因子日期）=====
    print("=== 测试5: 事件日缺前收（as_of 早于首因子）===")
    # as_of=2026-06-10（早于首因子 06-16），应用首因子 0.5 作为 denominator
    early_qfq = apply_adj_factor(bars_df, adj_df, as_of=_date(2026, 6, 10))
    # denominator=首因子=0.5，ratio=bar.adj_factor/0.5
    # 06-16: ratio=0.5/0.5=1.0 → qfq=20.0×1.0=20.0（不调整，因为从除权前视角看，除权是未来事件）
    # 06-17: ratio=1.0/0.5=2.0 → qfq=10.0×2.0=20.0（除权后价格上调到除权前基准）
    assert _approx(float(early_qfq.loc[pd.Timestamp("2026-06-16"), "close"]), 20.0), \
        f"as_of=06-10 06-16 qfq 应=20.0（不调整）, got {early_qfq.loc[pd.Timestamp('2026-06-16'), 'close']}"
    assert _approx(float(early_qfq.loc[pd.Timestamp("2026-06-17"), "close"]), 20.0), \
        f"as_of=06-10 06-17 qfq 应=20.0（上调到除权前基准）, got {early_qfq.loc[pd.Timestamp('2026-06-17'), 'close']}"
    print("as_of=06-10（除权前视角）：价格统一到20.0基准，无未来泄漏 ✓")

    # ===== 6. 因子源失败（空 adj_factor_df）=====
    print("=== 测试6: 因子源失败 ===")
    empty_adj = pd.DataFrame(columns=["trade_date", "adj_factor"])
    fail_qfq = apply_adj_factor(bars_df, empty_adj)
    # 因子源失败：原样返回（调用方应标记 degraded，不伪装成功）
    pd.testing.assert_frame_equal(fail_qfq, bars_df)
    print("因子源失败：原样返回，不伪装 ✓")

    # ===== 7. adjustment_as_of 无未来事件泄漏 =====
    print("=== 测试7: as_of 无未来泄漏 ===")
    # as_of=2026-06-16（除权前一天）：06-16 的 qfq 应=raw（除权是未来事件，不应影响）
    as_of_0616 = apply_adj_factor_with_as_of(
        bars_df, adj_df, as_of=_date(2026, 6, 16), intraday=False
    )
    # denominator=factor(06-16)=0.5
    # 06-16: ratio=0.5/0.5=1.0 → qfq=20.0（不调整，除权是未来事件）
    assert _approx(float(as_of_0616.loc[pd.Timestamp("2026-06-16"), "close"]), 20.0), \
        f"as_of=06-16 06-16 qfq 应=20.0（无未来泄漏）, got {as_of_0616.loc[pd.Timestamp('2026-06-16'), 'close']}"
    print("as_of=06-16：06-16 qfq=20.0=raw，未来除权事件未泄漏 ✓")

    # as_of=2026-06-18（最新）：06-16 qfq=10.0（除权已发生，价格下调）
    as_of_0618 = apply_adj_factor_with_as_of(
        bars_df, adj_df, as_of=_date(2026, 6, 18), intraday=False
    )
    assert _approx(float(as_of_0618.loc[pd.Timestamp("2026-06-16"), "close"]), 10.0), \
        f"as_of=06-18 06-16 qfq 应=10.0, got {as_of_0618.loc[pd.Timestamp('2026-06-16'), 'close']}"
    print("as_of=06-18：06-16 qfq=10.0（除权后视角）✓")

    # ===== 8. 分钟线前复权（按交易日映射同一权威因子）=====
    print("=== 测试8: 分钟线按交易日映射 ===")
    minute_idx = pd.to_datetime([
        "2026-06-16 09:30", "2026-06-16 09:31",
        "2026-06-17 09:30", "2026-06-18 09:30",
    ])
    minute_bars = pd.DataFrame({
        "open": [20.0, 20.1, 10.0, 10.1],
        "high": [20.2, 20.3, 10.1, 10.2],
        "low": [19.9, 20.0, 9.9, 10.0],
        "close": [20.0, 20.1, 10.0, 10.1],
        "volume": [1000, 1200, 2000, 1500],
    }, index=minute_idx)
    minute_bars.index.name = "trade_time"

    qfq_minute = apply_adj_factor_intraday(minute_bars, adj_df)
    # 06-16 09:30: qfq=20.0×(0.5/1.0)=10.0
    assert _approx(float(qfq_minute.loc[minute_idx[0], "close"]), 10.0), \
        f"分钟 06-16 09:30 qfq 应=10.0, got {qfq_minute.loc[minute_idx[0], 'close']}"
    # 06-17 09:30: qfq=10.0×(1.0/1.0)=10.0
    assert _approx(float(qfq_minute.loc[minute_idx[2], "close"]), 10.0)
    print("分钟线按交易日映射同一权威因子 ✓")

    # ===== 9. 空数据 =====
    print("=== 测试9: 空数据 ===")
    empty_df = apply_adj_factor(pd.DataFrame(), adj_df)
    assert empty_df.empty, "空输入应返回空"
    no_adj = apply_adj_factor(bars_df, pd.DataFrame())
    pd.testing.assert_frame_equal(no_adj, bars_df)
    print("空数据/空因子处理 ✓")

    # ===== 10. 函数签名校验（向后兼容）=====
    print("=== 测试10: 向后兼容签名 ===")
    sig = inspect.signature(apply_adj_factor)
    params = list(sig.parameters.keys())
    assert params == ["bars_df", "adj_factor_df", "as_of"], \
        f"apply_adj_factor 参数应为 [bars_df, adj_factor_df, as_of], got {params}"
    sig_intraday = inspect.signature(apply_adj_factor_intraday)
    params_intraday = list(sig_intraday.parameters.keys())
    assert params_intraday == ["bars_df", "adj_factor_df", "as_of"], \
        f"apply_adj_factor_intraday 参数应为 [bars_df, adj_factor_df, as_of], got {params_intraday}"
    sig_asof = inspect.signature(apply_adj_factor_with_as_of)
    params_asof = list(sig_asof.parameters.keys())
    assert params_asof == ["bars_df", "adj_factor_df", "as_of", "intraday"], \
        f"apply_adj_factor_with_as_of 参数应为 [bars_df, adj_factor_df, as_of, intraday], got {params_asof}"
    # as_of 默认 None（向后兼容）
    assert inspect.signature(apply_adj_factor).parameters["as_of"].default is None
    print("函数签名向后兼容 ✓")

    print("\n所有自测通过 ✓")
