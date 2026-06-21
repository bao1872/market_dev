"""行情数据写入校验。

在 _upsert_*_bars 写入 DB 前校验 DataFrame 数据质量，拦截非法数据，
避免 pytdx 偶发异常数据（OHLC 关系不成立、价格为 0/负数、时间倒序）污染 DB。

校验规则（5 条）：
1. OHLC 关系：high >= max(open, close, low) 且 low <= min(open, close, high)
2. 非负性：volume >= 0，amount >= 0，open/high/low/close > 0（价格为正）
3. 时间单调：datetime 列单调递增（同标的无倒序）
4. 空值检查：OHLCV 无 NaN
5. 异常价格：close > 0 且 close < 100000（A 股无百万级股价）

校验失败时（is_valid=False），调用方应跳过写入并记录 error 日志。

Inputs:
    df: pytdx 返回的 DataFrame，含 datetime/open/high/low/close/volume/amount 列
    symbol: 股票代码（用于错误上下文）
    period: 周期（d/15m/60m/w/m）

Outputs:
    ValidationResult: is_valid, errors, warning_count

How to Run:
    python -m app.services.bars_validator    # 自测：验证 5 条校验规则
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

logger = logging.getLogger("bars_validator")

# 错误详情最大保留条数（避免日志爆炸）
_MAX_ERRORS = 20

# A 股价格上限（无百万级股价）
_MAX_PRICE = 100000.0


@dataclass
class ValidationResult:
    """校验结果。

    Attributes:
        is_valid: True 时可写入 DB；False 时调用方应跳过写入
        errors: 错误详情列表（阻断写入），最多保留前 _MAX_ERRORS 条
        warning_count: 警告数量（不阻断写入）
    """

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warning_count: int = 0


def _add_error(errors: list[str], msg: str) -> None:
    """添加错误详情，超过 _MAX_ERRORS 时截断。"""
    if len(errors) < _MAX_ERRORS:
        errors.append(msg)


def validate_bars(df: pd.DataFrame, symbol: str, period: str) -> ValidationResult:
    """校验行情 DataFrame 数据质量。

    Args:
        df: 行情数据，含 datetime/open/high/low/close/volume/amount 列
        symbol: 股票代码（用于错误上下文）
        period: 周期（d/15m/60m/w/m）

    Returns:
        ValidationResult: is_valid=False 时调用方应跳过写入并记录错误
    """
    # 空 DataFrame 视为合法（无数据可校验）
    if df is None or df.empty:
        return ValidationResult(is_valid=True)

    errors: list[str] = []
    warning_count = 0
    ctx = f"symbol={symbol} period={period}"

    # ===== 规则 4：空值检查（OHLCV 无 NaN）=====
    required_cols = ["open", "high", "low", "close", "volume", "amount"]
    missing_cols = [c for c in required_cols if c not in df.columns]
    if missing_cols:
        _add_error(errors, f"[{ctx}] 缺少必要列: {missing_cols}")
        return ValidationResult(is_valid=False, errors=errors, warning_count=warning_count)

    # 向量化 NaN 检查
    nan_mask = df[required_cols].isna()
    if nan_mask.any().any():
        for col in required_cols:
            col_nan_count = int(nan_mask[col].sum())
            if col_nan_count > 0:
                _add_error(
                    errors,
                    f"[{ctx}] 列 {col} 有 {col_nan_count} 个 NaN",
                )

    # ===== 规则 1：OHLC 关系 =====
    # high >= max(open, close, low) 且 low <= min(open, close, high)
    ohlc = df[["open", "close", "low"]].copy()
    max_ocl = ohlc.max(axis=1)  # open, close, low 的最大值
    min_och = df[["open", "close", "high"]].min(axis=1)  # open, close, high 的最小值

    high_lt_max = df["high"] < max_ocl  # high < max(open, close, low) 违规
    low_gt_min = df["low"] > min_och  # low > min(open, close, high) 违规
    ohlc_violation = high_lt_max | low_gt_min

    if ohlc_violation.any():
        violation_count = int(ohlc_violation.sum())
        # 收集前几条违规详情
        for idx in df.index[ohlc_violation][:3]:
            row = df.loc[idx]
            _add_error(
                errors,
                f"[{ctx}] OHLC 关系不成立 @ {row.get('datetime', idx)}: "
                f"open={row['open']} high={row['high']} low={row['low']} close={row['close']}",
            )
        if violation_count > 3:
            _add_error(errors, f"[{ctx}] OHLC 关系违规共 {violation_count} 条（仅展示前 3 条）")

    # ===== 规则 2：非负性（volume >= 0, amount >= 0, 价格 > 0）=====
    # volume/amount 可以为 0（停牌），但价格必须 > 0
    volume_neg = df["volume"] < 0
    amount_neg = df["amount"] < 0
    price_nonpositive = (df[["open", "high", "low", "close"]] <= 0).any(axis=1)

    if volume_neg.any():
        _add_error(
            errors,
            f"[{ctx}] volume 有 {int(volume_neg.sum())} 个负值",
        )
    if amount_neg.any():
        _add_error(
            errors,
            f"[{ctx}] amount 有 {int(amount_neg.sum())} 个负值",
        )
    if price_nonpositive.any():
        violation_count = int(price_nonpositive.sum())
        for idx in df.index[price_nonpositive][:3]:
            row = df.loc[idx]
            _add_error(
                errors,
                f"[{ctx}] 价格非正 @ {row.get('datetime', idx)}: "
                f"open={row['open']} high={row['high']} low={row['low']} close={row['close']}",
            )
        if violation_count > 3:
            _add_error(errors, f"[{ctx}] 价格非正共 {violation_count} 条（仅展示前 3 条）")

    # ===== 规则 5：异常价格（close > 0 且 close < 100000）=====
    # 与规则 2 的 price_nonpositive 部分重叠，但额外检查上界
    close_too_high = df["close"] >= _MAX_PRICE
    if close_too_high.any():
        violation_count = int(close_too_high.sum())
        for idx in df.index[close_too_high][:3]:
            row = df.loc[idx]
            _add_error(
                errors,
                f"[{ctx}] close 超过上限 {_MAX_PRICE} @ {row.get('datetime', idx)}: "
                f"close={row['close']}",
            )
        if violation_count > 3:
            _add_error(errors, f"[{ctx}] close 超上限共 {violation_count} 条（仅展示前 3 条）")

    # ===== 规则 3：时间单调递增 =====
    if "datetime" in df.columns:
        dt_series = pd.to_datetime(df["datetime"], errors="coerce")
        if dt_series.isna().any():
            nan_dt_count = int(dt_series.isna().sum())
            _add_error(errors, f"[{ctx}] datetime 列有 {nan_dt_count} 个无效值")
        else:
            # 检查是否单调递增（允许相等）
            is_monotonic = dt_series.is_monotonic_increasing
            if not is_monotonic:
                # 找出倒序的位置
                diff = dt_series.diff()
                neg_diff_mask = diff < pd.Timedelta(0)
                violation_count = int(neg_diff_mask.sum())
                # 找第一个倒序点作为示例
                first_violation_idx = neg_diff_mask.idxmax() if violation_count > 0 else None
                if first_violation_idx is not None:
                    prev_idx = df.index[df.index.get_loc(first_violation_idx) - 1] \
                        if df.index.get_loc(first_violation_idx) > 0 else None
                    if prev_idx is not None:
                        _add_error(
                            errors,
                            f"[{ctx}] 时间倒序 @ prev={dt_series.loc[prev_idx]} "
                            f"curr={dt_series.loc[first_violation_idx]}",
                        )
                if violation_count > 1:
                    _add_error(
                        errors,
                        f"[{ctx}] 时间倒序共 {violation_count} 处",
                    )

    is_valid = len(errors) == 0
    if not is_valid:
        logger.warning(
            "行情数据校验失败 %s errors_count=%d errors=%s",
            ctx, len(errors), errors[:5],
        )

    return ValidationResult(
        is_valid=is_valid,
        errors=errors,
        warning_count=warning_count,
    )


if __name__ == "__main__":
    # 自测入口：验证 5 条校验规则（无副作用，不连 DB）
    print("===== Phase 5.1 bars_validator 自测 =====")

    # 1. 正常数据（应通过）
    normal_df = pd.DataFrame({
        "datetime": pd.date_range("2024-01-01", periods=5, freq="D"),
        "open": [10.0, 10.5, 10.2, 10.8, 10.6],
        "high": [10.8, 10.9, 10.5, 11.0, 10.9],
        "low": [9.8, 10.1, 10.0, 10.5, 10.4],
        "close": [10.5, 10.2, 10.4, 10.9, 10.7],
        "volume": [1000, 1200, 800, 1500, 1100],
        "amount": [10500.0, 12240.0, 8320.0, 16350.0, 11770.0],
    })
    result = validate_bars(normal_df, "000001", "d")
    assert result.is_valid, f"正常数据应通过校验，errors={result.errors}"
    assert len(result.errors) == 0, f"正常数据不应有错误，errors={result.errors}"
    print("✓ 规则 1-5 正常数据通过校验")

    # 2. OHLC 关系不成立（high < open）
    bad_ohlc_df = normal_df.copy()
    bad_ohlc_df.loc[0, "high"] = 9.5  # high < open(10.0)
    result = validate_bars(bad_ohlc_df, "000001", "d")
    assert not result.is_valid, "OHLC 关系不成立应失败"
    assert any("OHLC 关系不成立" in e for e in result.errors), \
        f"应包含 OHLC 关系错误，errors={result.errors}"
    print("✓ 规则 1 OHLC 关系校验拦截违规")

    # 3. 负 volume
    neg_vol_df = normal_df.copy()
    neg_vol_df.loc[0, "volume"] = -100
    result = validate_bars(neg_vol_df, "000001", "d")
    assert not result.is_valid, "负 volume 应失败"
    assert any("volume" in e and "负值" in e for e in result.errors), \
        f"应包含 volume 负值错误，errors={result.errors}"
    print("✓ 规则 2 非负性校验拦截负 volume")

    # 4. 价格为 0
    zero_price_df = normal_df.copy()
    zero_price_df.loc[0, "close"] = 0.0
    result = validate_bars(zero_price_df, "000001", "d")
    assert not result.is_valid, "价格为 0 应失败"
    assert any("价格非正" in e for e in result.errors), \
        f"应包含价格非正错误，errors={result.errors}"
    print("✓ 规则 2 非负性校验拦截零价格")

    # 5. 时间倒序
    reversed_df = normal_df.copy()
    reversed_df = reversed_df.iloc[::-1].reset_index(drop=True)
    result = validate_bars(reversed_df, "000001", "d")
    assert not result.is_valid, "时间倒序应失败"
    assert any("时间倒序" in e for e in result.errors), \
        f"应包含时间倒序错误，errors={result.errors}"
    print("✓ 规则 3 时间单调性校验拦截倒序")

    # 6. NaN 值
    nan_df = normal_df.copy()
    nan_df.loc[0, "close"] = float("nan")
    result = validate_bars(nan_df, "000001", "d")
    assert not result.is_valid, "NaN 值应失败"
    assert any("NaN" in e for e in result.errors), \
        f"应包含 NaN 错误，errors={result.errors}"
    print("✓ 规则 4 空值检查拦截 NaN")

    # 7. 异常高价
    high_price_df = normal_df.copy()
    high_price_df.loc[0, "close"] = 200000.0  # 超过 100000
    result = validate_bars(high_price_df, "000001", "d")
    assert not result.is_valid, "异常高价应失败"
    assert any("close 超过上限" in e for e in result.errors), \
        f"应包含 close 超上限错误，errors={result.errors}"
    print("✓ 规则 5 异常价格校验拦截超高价")

    # 8. 空 DataFrame（应通过）
    empty_df = pd.DataFrame()
    result = validate_bars(empty_df, "000001", "d")
    assert result.is_valid, "空 DataFrame 应通过校验"
    assert len(result.errors) == 0, "空 DataFrame 不应有错误"
    print("✓ 空 DataFrame 通过校验")

    # 9. 缺少必要列
    missing_col_df = pd.DataFrame({
        "datetime": pd.date_range("2024-01-01", periods=3, freq="D"),
        "open": [10.0, 10.5, 10.2],
        "high": [10.8, 10.9, 10.5],
        # 缺少 low, close, volume, amount
    })
    result = validate_bars(missing_col_df, "000001", "d")
    assert not result.is_valid, "缺少列应失败"
    assert any("缺少必要列" in e for e in result.errors), \
        f"应包含缺少列错误，errors={result.errors}"
    print("✓ 缺少必要列校验拦截")

    print("\n所有自测通过 ✓")
