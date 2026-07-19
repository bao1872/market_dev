"""复权因子纯函数 calculate_adjustment_factor_series 单元测试（CHANGE-20260719-001 §1.2）。

测试覆盖：
1. 无事件 / 空 corporate_actions → 全 1.0
2. 单事件（分红）→ Chanlunpro preclose 公式正确
3. 多事件累积（送股+转增+分红）
4. 数据缺失抛 AdjustmentFactorDataError（含 degraded_reason、missing_event_dates）
5. 空 raw_daily_bars → 空列表
6. 事件日早于 raw_daily_bars 最早日期 → 数据缺失
7. algorithm_version 传参不影响结果
8. close=0 跳过事件（不抛异常，因子为 1.0）
9. denominator=0 跳过事件
10. category 过滤（仅处理 category=1，其他 category 忽略）
11. NaN 字段视为 0
12. 事件因子为 1.0（preclose == close_{D-1}）时不累积
13. AdjustmentFactorDataError 多个缺失事件日
14. AdjustmentFactorDataError 自定义 degraded_reason
15. 纯函数无 IO 约束：源码不导入 sqlalchemy/pytdx/asyncio

约束：
- 纯单元测试（不连 DB / 网络 / pytdx）
- 使用合成 DataFrame 模拟 raw 日线 + xdxr 事件
- 000688 真实场景 Case：2026-04-23 close=40.97 + 2026-04-24 分红 1.3 → factor=0.996827
"""
from __future__ import annotations

import ast
import inspect
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.constants.factor_contract import FACTOR_ALGORITHM_VERSION
from app.services.adjustment_factor_calculator import (
    AdjustmentFactorDataError,
    calculate_adjustment_factor_series,
)

_CALCULATOR_FILE = (
    Path(__file__).parent.parent / "app" / "services" / "adjustment_factor_calculator.py"
)


# =============================================================================
# 合成数据 helpers
# =============================================================================


def _raw_df(
    dates: list[str], closes: list[float]
) -> pd.DataFrame:
    """构造 raw 日线 DataFrame（含 datetime/close 列）。"""
    return pd.DataFrame({
        "datetime": pd.to_datetime(dates),
        "close": closes,
    })


def _xdxr_df(events: list[dict]) -> pd.DataFrame:
    """构造 xdxr 公司行为 DataFrame（含必需列）。

    缺失字段填充为默认值（与生产路径一致）。
    """
    rows = []
    for e in events:
        rows.append({
            "date": pd.Timestamp(e["date"]),
            "category": e.get("category", 1),
            "fenhong": e.get("fenhong", 0.0),
            "songzhuangu": e.get("songzhuangu", 0.0),
            "peigu": e.get("peigu", 0.0),
            "peigujia": e.get("peigujia", 0.0),
        })
    return pd.DataFrame(rows, columns=[
        "date", "category", "fenhong", "songzhuangu", "peigu", "peigujia",
    ])


# =============================================================================
# 1. 无事件 / 空 corporate_actions → 全 1.0
# =============================================================================


class TestNoEvents:
    """无除权除息事件场景。"""

    def test_empty_corporate_actions(self):
        """空 xdxr DataFrame → 全 1.0。"""
        raw = _raw_df(["2026-06-16", "2026-06-17", "2026-06-18"], [10.0, 10.5, 11.0])
        xdxr = pd.DataFrame(columns=[
            "date", "category", "fenhong", "songzhuangu", "peigu", "peigujia",
        ])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        assert factors == [1.0, 1.0, 1.0]

    def test_none_corporate_actions(self):
        """corporate_actions=None → 全 1.0（向后兼容）。"""
        raw = _raw_df(["2026-06-16", "2026-06-17"], [10.0, 10.5])
        factors = calculate_adjustment_factor_series(raw, None)  # type: ignore[arg-type]
        assert factors == [1.0, 1.0]

    def test_no_category_1_events(self):
        """xdxr 有事件但 category != 1 → 全 1.0（category 过滤）。"""
        raw = _raw_df(["2026-06-16", "2026-06-17"], [10.0, 10.5])
        xdxr = _xdxr_df([{
            "date": "2026-06-17", "category": 2,  # 非除权除息
            "fenhong": 1.0,
        }])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        assert factors == [1.0, 1.0]


# =============================================================================
# 2. 单事件（分红）→ Chanlunpro preclose 公式正确
# =============================================================================


class TestSingleEvent:
    """单除权除息事件场景（000688 真实 Case）。"""

    def test_dividend_000688_scenario(self):
        """000688 真实场景：2026-04-23 close=40.97 + 2026-04-24 分红 1.3 元/10 股。

        preclose = (40.97×10 - 1.3 + 0) / (10 + 0 + 0) = 408.4 / 10 = 40.84
        event_factor = 40.84 / 40.97 ≈ 0.996826939...
        """
        raw = _raw_df(["2026-04-23", "2026-04-24"], [40.97, 41.20])
        xdxr = _xdxr_df([{
            "date": "2026-04-24", "fenhong": 1.3,
        }])
        factors = calculate_adjustment_factor_series(raw, xdxr)

        # 04-23: 事件日在后 → factor = event_factor ≈ 0.996827
        # 04-24: 事件日当天，无后续事件 → factor = 1.0
        expected_factor = (40.97 * 10 - 1.3) / 10 / 40.97
        assert factors[0] == pytest.approx(expected_factor, abs=1e-10)
        assert factors[1] == pytest.approx(1.0, abs=1e-10)

    def test_event_day_factor_is_one(self):
        """事件日当天（无后续事件）adj_factor=1.0。"""
        raw = _raw_df(["2026-04-22", "2026-04-23", "2026-04-24"], [40.0, 40.97, 41.20])
        xdxr = _xdxr_df([{"date": "2026-04-24", "fenhong": 1.3}])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        assert factors[-1] == pytest.approx(1.0, abs=1e-10)

    def test_bars_after_event_are_unit(self):
        """事件日之后的 bar adj_factor=1.0（无后续事件）。"""
        raw = _raw_df(
            ["2026-04-23", "2026-04-24", "2026-04-25", "2026-04-28"],
            [40.97, 41.20, 41.50, 42.00],
        )
        xdxr = _xdxr_df([{"date": "2026-04-24", "fenhong": 1.3}])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        # 04-23 factor = event_factor（事件日在后）
        # 04-24/25/28 factor = 1.0（事件日当天及之后，无后续事件）
        assert factors[0] < 1.0
        assert factors[1] == pytest.approx(1.0, abs=1e-10)
        assert factors[2] == pytest.approx(1.0, abs=1e-10)
        assert factors[3] == pytest.approx(1.0, abs=1e-10)


# =============================================================================
# 3. 多事件累积（送股+转增+分红）
# =============================================================================


class TestMultipleEventsCumulative:
    """多事件累积因子场景。"""

    def test_two_events_cumulative(self):
        """两个事件累积：早期 bar 因子 = event2 × event1。"""
        # bar1 (06-15) → 事件 06-20 (fenhong=1.0, close_{06-19}=10) 在后
        # bar2 (06-19) → 同上
        # bar3 (06-20) → 事件 06-25 (fenhong=1.0, close_{06-24}=12) 在后
        # bar4 (06-24) → 同上
        # bar5 (06-25) → 无后续事件 → 1.0
        raw = _raw_df(
            ["2026-06-15", "2026-06-19", "2026-06-20", "2026-06-24", "2026-06-25"],
            [10.0, 10.0, 12.0, 12.0, 13.0],
        )
        xdxr = _xdxr_df([
            {"date": "2026-06-20", "fenhong": 1.0},
            {"date": "2026-06-25", "fenhong": 1.0},
        ])
        factors = calculate_adjustment_factor_series(raw, xdxr)

        # event1 (06-20): preclose = (10×10 - 1)/10 = 9.9, factor1 = 9.9/10 = 0.99
        # event2 (06-25): preclose = (12×10 - 1)/10 = 11.9, factor2 = 11.9/12 ≈ 0.991667
        # 06-15, 06-19: 两个事件都在后 → factor = factor1 × factor2
        # 06-20, 06-24: 仅 06-25 事件在后 → factor = factor2
        # 06-25: 无后续事件 → factor = 1.0
        factor1 = (10.0 * 10 - 1.0) / 10 / 10.0
        factor2 = (12.0 * 10 - 1.0) / 10 / 12.0
        assert factors[0] == pytest.approx(factor1 * factor2, abs=1e-10)
        assert factors[1] == pytest.approx(factor1 * factor2, abs=1e-10)
        assert factors[2] == pytest.approx(factor2, abs=1e-10)
        assert factors[3] == pytest.approx(factor2, abs=1e-10)
        assert factors[4] == pytest.approx(1.0, abs=1e-10)

    def test_events_unsorted_in_xdxr(self):
        """xdxr 事件乱序输入 → 内部按 date 升序处理，结果正确。"""
        raw = _raw_df(
            ["2026-06-15", "2026-06-19", "2026-06-20", "2026-06-24", "2026-06-25"],
            [10.0, 10.0, 12.0, 12.0, 13.0],
        )
        xdxr_sorted = _xdxr_df([
            {"date": "2026-06-20", "fenhong": 1.0},
            {"date": "2026-06-25", "fenhong": 1.0},
        ])
        xdxr_shuffled = _xdxr_df([
            {"date": "2026-06-25", "fenhong": 1.0},
            {"date": "2026-06-20", "fenhong": 1.0},
        ])
        f_sorted = calculate_adjustment_factor_series(raw, xdxr_sorted)
        f_shuffled = calculate_adjustment_factor_series(raw, xdxr_shuffled)
        assert f_sorted == f_shuffled

    def test_songzhuangu_and_peigu(self):
        """送股 + 配股 + 分红混合事件。"""
        # close_{D-1}=20, fenhong=2, songzhuangu=5, peigu=2, peigujia=15
        # preclose = (20×10 - 2 + 2×15) / (10 + 2 + 5) = (200 - 2 + 30)/17 = 228/17 ≈ 13.411765
        # event_factor = preclose / 20 ≈ 0.670588
        raw = _raw_df(["2026-06-19", "2026-06-20"], [20.0, 14.0])
        xdxr = _xdxr_df([{
            "date": "2026-06-20", "fenhong": 2.0, "songzhuangu": 5.0,
            "peigu": 2.0, "peigujia": 15.0,
        }])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        preclose = (20.0 * 10 - 2.0 + 2.0 * 15.0) / (10 + 2.0 + 5.0)
        expected_factor = preclose / 20.0
        assert factors[0] == pytest.approx(expected_factor, abs=1e-10)
        assert factors[1] == pytest.approx(1.0, abs=1e-10)


# =============================================================================
# 4. 数据缺失抛 AdjustmentFactorDataError
# =============================================================================


class TestAdjustmentFactorDataError:
    """数据缺口/缺失场景。

    [CHANGE-20260719-001 §1.3] 行为变更：
    - 事件日 <= earliest_bar_date：跳过事件（不影响任何 bar 因子），不抛异常
    - 事件日 > earliest_bar_date 但 prev_close 距事件日 > 14 天：抛 bars_daily_gap
    - 事件日 > earliest_bar_date 且 prev_close 完全缺失：抛 bars_daily_missing_data
      （实际不会发生，因为 earliest_bar_date 之后总有至少一个 bar 在事件日之前）
    """

    def test_gap_detection_raises(self):
        """数据缺口 → 抛 AdjustmentFactorDataError（degraded_reason="bars_daily_gap"）。

        事件日 2026-04-24 在 raw_df 缺口中（raw_df 有 2026-01-30 和 2026-06-29，
        缺口 > 14 天阈值）。纯函数检测到 prev_close（2026-01-30）距事件日 > 14 天
        → 抛 bars_daily_gap，防止用错误的 prev_close（26.80 而非 40.97）计算因子。
        """
        raw = _raw_df(["2026-01-30", "2026-06-29"], [26.80, 33.42])
        xdxr = _xdxr_df([{"date": "2026-04-24", "fenhong": 1.3}])
        with pytest.raises(AdjustmentFactorDataError) as exc_info:
            calculate_adjustment_factor_series(raw, xdxr)
        assert date(2026, 4, 24) in exc_info.value.missing_event_dates
        assert exc_info.value.degraded_reason == "bars_daily_gap"

    def test_event_on_earliest_bar_skipped(self):
        """事件日 == earliest_bar_date → 跳过事件（不抛异常，因子全 1.0）。

        事件日等于最早 bar 日期时，事件不影响任何 bar（无 bar 在事件日之前），
        应跳过而非抛异常。
        """
        raw = _raw_df(["2026-04-24"], [41.20])
        xdxr = _xdxr_df([{"date": "2026-04-24", "fenhong": 1.3}])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        assert factors == [1.0]

    def test_event_before_earliest_bar_skipped(self):
        """事件日 < earliest_bar_date → 跳过事件（不抛异常，因子全 1.0）。

        事件日早于最早 bar 日期时，事件不影响任何 bar（所有 bar 都在事件日之后），
        应跳过而非抛异常。这是 000688 误报修复的核心：1997-2001 年的旧事件
        不应阻止 2024+ 年数据的因子计算。
        """
        raw = _raw_df(["2026-06-16", "2026-06-17"], [10.0, 10.5])
        xdxr = _xdxr_df([{"date": "2026-06-15", "fenhong": 1.0}])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        assert factors == [1.0, 1.0]

    def test_multiple_old_events_skipped(self):
        """多个早于 earliest_bar 的事件全部跳过（不抛异常）。"""
        raw = _raw_df(["2026-06-22", "2026-06-23"], [11.0, 11.5])
        xdxr = _xdxr_df([
            {"date": "2026-06-15", "fenhong": 1.0},
            {"date": "2026-06-20", "fenhong": 1.0},
        ])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        assert factors == [1.0, 1.0]

    def test_mixed_old_and_valid_events(self):
        """混合事件：早于 earliest_bar 的跳过，之后的正常处理。"""
        # earliest_bar = 2026-06-16
        # 事件 2026-06-10（早于 earliest_bar）→ 跳过
        # 事件 2026-06-20（晚于 earliest_bar）→ 正常处理
        raw = _raw_df(
            ["2026-06-16", "2026-06-19", "2026-06-20"],
            [10.0, 10.0, 12.0],
        )
        xdxr = _xdxr_df([
            {"date": "2026-06-10", "fenhong": 5.0},  # 跳过
            {"date": "2026-06-20", "fenhong": 1.0},  # 处理
        ])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        # 06-10 事件被跳过，不影响因子
        # 06-20 事件：preclose = (10×10 - 1)/10 = 9.9, factor = 9.9/10 = 0.99
        # 06-16, 06-19: 事件日在后 → factor = 0.99
        # 06-20: 事件日当天 → factor = 1.0
        expected_factor = (10.0 * 10 - 1.0) / 10 / 10.0
        assert factors[0] == pytest.approx(expected_factor, abs=1e-10)
        assert factors[1] == pytest.approx(expected_factor, abs=1e-10)
        assert factors[2] == pytest.approx(1.0, abs=1e-10)

    def test_gap_within_threshold_not_raised(self):
        """prev_close 距事件日 <= 14 天（如长假）→ 不算缺口，正常计算。"""
        # 模拟春节缺口：2026-02-14（节前最后交易日）→ 2026-02-25（节后事件日）
        # 缺口 11 天 <= 14 天阈值 → 正常计算
        raw = _raw_df(["2026-02-14", "2026-02-25"], [10.0, 11.0])
        xdxr = _xdxr_df([{"date": "2026-02-25", "fenhong": 1.0}])
        # 不应抛异常
        factors = calculate_adjustment_factor_series(raw, xdxr)
        expected_factor = (10.0 * 10 - 1.0) / 10 / 10.0
        assert factors[0] == pytest.approx(expected_factor, abs=1e-10)
        assert factors[1] == pytest.approx(1.0, abs=1e-10)

    def test_custom_degraded_reason(self):
        """AdjustmentFactorDataError 支持自定义 degraded_reason。"""
        exc = AdjustmentFactorDataError(
            [date(2026, 4, 24)], degraded_reason="custom_reason_xyz",
        )
        assert exc.degraded_reason == "custom_reason_xyz"
        assert exc.missing_event_dates == [date(2026, 4, 24)]
        assert "custom_reason_xyz" in str(exc)

    def test_error_message_includes_dates(self):
        """异常消息包含缺失事件日 ISO 字符串。"""
        exc = AdjustmentFactorDataError([date(2026, 4, 24), date(2026, 6, 15)])
        msg = str(exc)
        assert "2026-04-24" in msg
        assert "2026-06-15" in msg
        assert "bars_daily_missing_data" in msg

    def test_error_message_truncates_many_dates(self):
        """缺失事件日 >5 个时消息含"共 N 个"提示。"""
        many_dates = [date(2026, 1, d) for d in range(1, 11)]  # 10 个日期
        exc = AdjustmentFactorDataError(many_dates)
        msg = str(exc)
        assert "共 10 个" in msg


# =============================================================================
# 5. 空 raw_daily_bars → 空列表
# =============================================================================


class TestEmptyRawBars:
    """空 raw_daily_bars 场景。"""

    def test_empty_raw_with_events(self):
        """raw_daily_bars 为空但有事件 → 返回空列表（不抛异常）。"""
        raw = pd.DataFrame(columns=["datetime", "close"])
        xdxr = _xdxr_df([{"date": "2026-04-24", "fenhong": 1.3}])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        assert factors == []

    def test_empty_raw_empty_events(self):
        """raw 和 xdxr 都为空 → 返回空列表。"""
        raw = pd.DataFrame(columns=["datetime", "close"])
        xdxr = pd.DataFrame(columns=[
            "date", "category", "fenhong", "songzhuangu", "peigu", "peigujia",
        ])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        assert factors == []


# =============================================================================
# 6. algorithm_version 传参不影响结果
# =============================================================================


class TestAlgorithmVersion:
    """algorithm_version 参数行为。"""

    def test_default_version(self):
        """默认使用 FACTOR_ALGORITHM_VERSION。"""
        raw = _raw_df(["2026-04-23", "2026-04-24"], [40.97, 41.20])
        xdxr = _xdxr_df([{"date": "2026-04-24", "fenhong": 1.3}])
        # 不传 algorithm_version → 使用 FACTOR_ALGORITHM_VERSION
        factors_default = calculate_adjustment_factor_series(raw, xdxr)
        # 显式传 FACTOR_ALGORITHM_VERSION
        factors_explicit = calculate_adjustment_factor_series(
            raw, xdxr, algorithm_version=FACTOR_ALGORITHM_VERSION,
        )
        assert factors_default == factors_explicit

    def test_version_does_not_affect_result(self):
        """algorithm_version 仅用于日志，不影响计算结果。"""
        raw = _raw_df(["2026-04-23", "2026-04-24"], [40.97, 41.20])
        xdxr = _xdxr_df([{"date": "2026-04-24", "fenhong": 1.3}])
        f1 = calculate_adjustment_factor_series(raw, xdxr, algorithm_version="v1")
        f2 = calculate_adjustment_factor_series(raw, xdxr, algorithm_version="v2")
        assert f1 == f2


# =============================================================================
# 7. close=0 跳过事件（不抛异常，因子为 1.0）
# =============================================================================


class TestEdgeCases:
    """边界场景：close=0、denominator=0、NaN 字段、事件因子为 1.0。"""

    def test_prev_close_zero_skips_event(self):
        """事件日前一交易日 close=0 → 跳过事件（不抛异常，不累积因子）。"""
        # close_{D-1}=0 → 数据异常，跳过事件
        raw = _raw_df(["2026-04-23", "2026-04-24"], [0.0, 41.20])
        xdxr = _xdxr_df([{"date": "2026-04-24", "fenhong": 1.3}])
        # 不应抛异常，但因子的累积跳过该事件 → 全 1.0
        factors = calculate_adjustment_factor_series(raw, xdxr)
        assert factors == [1.0, 1.0]

    def test_denominator_zero_skips_event(self):
        """denominator=0（peigu=-10+songzhuangu=0 等异常组合）→ 跳过事件。"""
        # denominator = 10 + peigu + songzhuangu = 10 + (-10) + 0 = 0 → 跳过
        raw = _raw_df(["2026-04-23", "2026-04-24"], [40.97, 41.20])
        xdxr = _xdxr_df([{
            "date": "2026-04-24", "fenhong": 1.3,
            "peigu": -10.0, "songzhuangu": 0.0,
        }])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        # denominator=0 跳过事件 → 全 1.0
        assert factors == [1.0, 1.0]

    def test_nan_fields_treated_as_zero(self):
        """xdxr 中 NaN 字段视为 0（与原 _calculate_adj_factor 一致）。"""
        raw = _raw_df(["2026-04-23", "2026-04-24"], [40.97, 41.20])
        # 显式构造含 NaN 的 xdxr
        xdxr = pd.DataFrame([{
            "date": pd.Timestamp("2026-04-24"),
            "category": 1,
            "fenhong": 1.3,
            "songzhuangu": float("nan"),
            "peigu": float("nan"),
            "peigujia": float("nan"),
        }])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        # NaN 视为 0 → 等价于仅分红 1.3 → 与 test_dividend_000688_scenario 相同
        expected_factor = (40.97 * 10 - 1.3) / 10 / 40.97
        assert factors[0] == pytest.approx(expected_factor, abs=1e-10)
        assert factors[1] == pytest.approx(1.0, abs=1e-10)

    def test_event_factor_unit_not_accumulated(self):
        """事件因子为 1.0（preclose == close_{D-1}）时不累积。

        构造 preclose == close_{D-1} 的事件：
        fenhong=0, peigu=0, songzhuangu=0 → preclose = (close×10)/10 = close → factor=1.0
        """
        raw = _raw_df(["2026-04-23", "2026-04-24"], [40.97, 41.20])
        xdxr = _xdxr_df([{
            "date": "2026-04-24",
            "fenhong": 0.0, "songzhuangu": 0.0,
            "peigu": 0.0, "peigujia": 0.0,
        }])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        # 事件因子 = 1.0 → 不累积 → 全 1.0
        assert factors == [1.0, 1.0]


# =============================================================================
# 8. 纯函数无 IO 约束（AST 守护）
# =============================================================================


class TestPureFunctionConstraint:
    """纯函数无 IO 约束：源码不导入 sqlalchemy/pytdx/asyncio。

    确保该模块可作为 Auditor（只读）和 Rebuild（持久化）共用的唯一算法实现，
    不会引入数据库/网络副作用。
    """

    def test_no_sqlalchemy_import(self):
        """源码不导入 sqlalchemy（不连 DB）。"""
        source = _CALCULATOR_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "sqlalchemy" not in alias.name, (
                        f"adjustment_factor_calculator.py 禁止导入 sqlalchemy: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or "sqlalchemy" not in node.module, (
                    f"adjustment_factor_calculator.py 禁止导入 sqlalchemy: {node.module}"
                )

    def test_no_pytdx_import(self):
        """源码不导入 pytdx（不调网络）。"""
        source = _CALCULATOR_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "pytdx" not in alias.name, (
                        f"adjustment_factor_calculator.py 禁止导入 pytdx: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or "pytdx" not in node.module, (
                    f"adjustment_factor_calculator.py 禁止导入 pytdx: {node.module}"
                )

    def test_no_asyncio_import(self):
        """源码不导入 asyncio（纯同步函数）。"""
        source = _CALCULATOR_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "asyncio" not in alias.name, (
                        f"adjustment_factor_calculator.py 禁止导入 asyncio: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module is None or "asyncio" not in node.module, (
                    f"adjustment_factor_calculator.py 禁止导入 asyncio: {node.module}"
                )

    def test_no_io_calls_in_calculate_function(self):
        """calculate_adjustment_factor_series 函数体内无 IO 调用。

        不允许 open()、read()、write()、requests、session.execute、cursor 等。
        """
        source = _CALCULATOR_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        # 找到 calculate_adjustment_factor_series 函数定义
        target_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "calculate_adjustment_factor_series":
                target_func = node
                break
        assert target_func is not None, "calculate_adjustment_factor_series 函数未找到"

        # 检查函数体内的调用：不允许 open/read/write/execute/request 等
        forbidden_calls = {"open", "read", "write", "execute", "request", "fetch", "query"}
        for node in ast.walk(target_func):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in forbidden_calls:
                    raise AssertionError(
                        f"calculate_adjustment_factor_series 禁止调用 IO 函数: {func.id}"
                    )
                if isinstance(func, ast.Attribute) and func.attr in forbidden_calls:
                    raise AssertionError(
                        f"calculate_adjustment_factor_series 禁止调用 IO 方法: {func.attr}"
                    )

    def test_calculate_function_signature_no_io_params(self):
        """calculate_adjustment_factor_series 不接受 IO 相关参数。

        不允许 session、adapter、min_date、use_raw_close、supplement_df 等参数。
        """
        sig = inspect.signature(calculate_adjustment_factor_series)
        forbidden_params = {"session", "adapter", "min_date", "use_raw_close", "supplement_df"}
        actual_params = set(sig.parameters.keys())
        overlap = actual_params & forbidden_params
        assert not overlap, (
            f"calculate_adjustment_factor_series 禁止接受 IO 参数: {overlap}"
        )

    def test_function_is_sync_not_async(self):
        """calculate_adjustment_factor_series 是同步函数（非 async）。"""
        assert not inspect.iscoroutinefunction(calculate_adjustment_factor_series), (
            "calculate_adjustment_factor_series 必须是同步函数（纯函数无 IO）"
        )


# =============================================================================
# 9. 确定性（同输入同输出）
# =============================================================================


class TestDeterminism:
    """纯函数确定性：相同输入 → 相同输出。"""

    def test_same_input_same_output(self):
        """相同输入两次调用结果一致。"""
        raw = _raw_df(["2026-04-23", "2026-04-24"], [40.97, 41.20])
        xdxr = _xdxr_df([{"date": "2026-04-24", "fenhong": 1.3}])
        f1 = calculate_adjustment_factor_series(raw, xdxr)
        f2 = calculate_adjustment_factor_series(raw, xdxr)
        assert f1 == f2

    def test_factors_length_matches_raw(self):
        """返回因子列表长度与 raw_daily_bars 行数一致。"""
        raw = _raw_df(
            ["2026-04-23", "2026-04-24", "2026-04-25", "2026-04-28"],
            [40.97, 41.20, 41.50, 42.00],
        )
        xdxr = _xdxr_df([{"date": "2026-04-24", "fenhong": 1.3}])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        assert len(factors) == len(raw)


# =============================================================================
# 10. 603538 bug 模式回归（stored 全 1.0 但 expected 有非 1.0）
# =============================================================================


class Test603538BugPattern:
    """603538 bug 模式：stored 全 1.0 但 expected 有非 1.0。

    本纯函数应正确计算 expected，让 Auditor 能发现 stored 全 1.0 的错误。
    """

    def test_expected_factor_non_unit_with_event(self):
        """有事件时 expected 因子应非 1.0（让 Auditor 能检测到 stored 全 1.0 错误）。"""
        raw = _raw_df(["2026-04-23", "2026-04-24"], [40.97, 41.20])
        xdxr = _xdxr_df([{"date": "2026-04-24", "fenhong": 1.3}])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        # 04-23 因子应非 1.0（约 0.996827）
        assert abs(factors[0] - 1.0) > 1e-6, (
            f"有事件时 expected 因子应非 1.0，实际 factors[0]={factors[0]}"
        )

    def test_no_event_returns_all_unit(self):
        """无事件时 expected 因子全 1.0（与 stored 全 1.0 一致）。"""
        raw = _raw_df(["2026-06-16", "2026-06-17", "2026-06-18"], [10.0, 10.5, 11.0])
        xdxr = pd.DataFrame(columns=[
            "date", "category", "fenhong", "songzhuangu", "peigu", "peigujia",
        ])
        factors = calculate_adjustment_factor_series(raw, xdxr)
        assert all(abs(f - 1.0) < 1e-10 for f in factors)
