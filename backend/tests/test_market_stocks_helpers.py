"""market_stocks_service 纯单元测试（不连接数据库）。

测试内容（CHANGE-20260729-009）：
1. _compute_factor_ready：各种输入场景的返回值
   - flat_fp=None + daily_bar_count 不同值 → INSUFFICIENT_DAILY_BARS / COMPUTE_FAILED / no_snapshot
   - flat_fp 存在但维度缺失 → trend_missing / structure_missing / momentum_missing
   - 全部维度就绪 → (True, None, None, None)
2. _build_chip_status_struct：各种 chip 状态的结构化输出
   - None → None
   - succeeded → status/reason_text 正确
   - skipped + M15_BARS_INSUFFICIENT → reason_code/actual_bars/required_bars 正确
   - failed → CHIP_ERROR
3. _MIN_DAILY_BARS_FOR_FACTOR / _CHIP_MIN_15M_BARS 常量值正确

运行方式：
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider tests/test_market_stocks_helpers.py -v
"""

from __future__ import annotations

from datetime import datetime, UTC
from types import SimpleNamespace

from app.services.market_stocks_service import (
    _CHIP_MIN_15M_BARS,
    _FP_MOMENTUM_KEYS,
    _FP_STRUCTURE_KEYS,
    _FP_TREND_KEYS,
    _MIN_DAILY_BARS_FOR_FACTOR,
    _build_chip_status_struct,
    _compute_factor_ready,
)


# ===== _compute_factor_ready 测试 =====


class TestComputeFactorReady_NoneFlatFp:
    """flat_fp=None 时的各种场景。"""

    def test_none_no_bar_count_returns_no_snapshot(self):
        """flat_fp=None + 无日线计数 → no_snapshot。"""
        ready, error, actual, required = _compute_factor_ready(None)
        assert ready is False
        assert error == "no_snapshot"
        assert actual is None
        assert required is None

    def test_none_zero_bars_returns_insufficient(self):
        """flat_fp=None + 0 日线 → INSUFFICIENT_DAILY_BARS。"""
        ready, error, actual, required = _compute_factor_ready(None, daily_bar_count=0)
        assert ready is False
        assert error == "INSUFFICIENT_DAILY_BARS"
        assert actual == 0
        assert required == _MIN_DAILY_BARS_FOR_FACTOR

    def test_none_below_threshold_returns_insufficient(self):
        """flat_fp=None + 日线 < 60 → INSUFFICIENT_DAILY_BARS。"""
        ready, error, actual, required = _compute_factor_ready(None, daily_bar_count=45)
        assert ready is False
        assert error == "INSUFFICIENT_DAILY_BARS"
        assert actual == 45
        assert required == 60

    def test_none_just_below_threshold_returns_insufficient(self):
        """flat_fp=None + 日线 = 59 → INSUFFICIENT_DAILY_BARS。"""
        ready, error, actual, required = _compute_factor_ready(None, daily_bar_count=59)
        assert ready is False
        assert error == "INSUFFICIENT_DAILY_BARS"
        assert actual == 59
        assert required == 60

    def test_none_at_threshold_returns_compute_failed(self):
        """flat_fp=None + 日线 = 60 → COMPUTE_FAILED（不是 INSUFFICIENT_DAILY_BARS）。"""
        ready, error, actual, required = _compute_factor_ready(None, daily_bar_count=60)
        assert ready is False
        assert error == "COMPUTE_FAILED"
        assert actual == 60
        assert required == 60

    def test_none_above_threshold_returns_compute_failed(self):
        """flat_fp=None + 日线 > 60 → COMPUTE_FAILED（有数据但计算仍失败）。"""
        ready, error, actual, required = _compute_factor_ready(None, daily_bar_count=200)
        assert ready is False
        assert error == "COMPUTE_FAILED"
        assert actual == 200
        assert required == 60


class TestComputeFactorReady_DimensionsMissing:
    """flat_fp 存在但维度缺失。"""

    def test_all_dimensions_present_returns_ready(self):
        """三维度均有权威字段 → factor_ready=True。"""
        flat = {
            "fp_trend_direction": "up",
            "fp_swing_direction": "bull",
            "fp_sqzmom_value": 0.5,
        }
        ready, error, actual, required = _compute_factor_ready(flat)
        assert ready is True
        assert error is None
        assert actual is None
        assert required is None

    def test_trend_missing(self):
        """趋势维度全 None → trend_missing。"""
        flat = {
            "fp_trend_direction": None,
            "fp_trend_bars": None,
            "fp_swing_direction": "bull",
            "fp_sqzmom_value": 0.5,
        }
        ready, error, actual, required = _compute_factor_ready(flat)
        assert ready is False
        assert error == "trend_missing"

    def test_structure_missing(self):
        """结构维度全 None → structure_missing。"""
        flat = {
            "fp_trend_direction": "up",
            "fp_swing_direction": None,
            "fp_structure_alignment": None,
            "fp_sqzmom_value": 0.5,
        }
        ready, error, actual, required = _compute_factor_ready(flat)
        assert ready is False
        assert error == "structure_missing"

    def test_momentum_missing(self):
        """动量维度全 None → momentum_missing。"""
        flat = {
            "fp_trend_direction": "up",
            "fp_swing_direction": "bull",
            "fp_sqzmom_value": None,
            "fp_momentum_direction": None,
            "fp_squeeze_state": None,
        }
        ready, error, actual, required = _compute_factor_ready(flat)
        assert ready is False
        assert error == "momentum_missing"

    def test_trend_partial_presence_counts_as_ready(self):
        """趋势维度有一个字段非空即视为就绪。"""
        flat = {
            "fp_trend_direction": None,
            "fp_trend_bars": 55,
            "fp_swing_direction": "bull",
            "fp_sqzmom_value": 0.5,
        }
        ready, error, _, _ = _compute_factor_ready(flat)
        assert ready is True


class TestComputeFactorReady_Constants:
    """验证常量值。"""

    def test_min_daily_bars_is_60(self):
        assert _MIN_DAILY_BARS_FOR_FACTOR == 60

    def test_trend_keys(self):
        assert _FP_TREND_KEYS == ("fp_trend_direction", "fp_trend_bars")

    def test_structure_keys(self):
        assert _FP_STRUCTURE_KEYS == ("fp_swing_direction", "fp_structure_alignment")

    def test_momentum_keys(self):
        assert _FP_MOMENTUM_KEYS == ("fp_sqzmom_value", "fp_momentum_direction", "fp_squeeze_state")


# ===== _build_chip_status_struct 测试 =====


def _make_chip_row(
    status: str,
    chip_payload: dict | None = None,
    error_message: str | None = None,
    created_at: datetime | None = None,
) -> SimpleNamespace:
    """构造模拟 chip row（NamedTuple 替代）。"""
    return SimpleNamespace(
        status=status,
        chip_payload=chip_payload,
        error_message=error_message,
        created_at=created_at,
    )


class TestBuildChipStatusStruct:
    """_build_chip_status_struct 各种场景。"""

    def test_none_returns_none(self):
        """chip_row=None → None。"""
        assert _build_chip_status_struct(None) is None

    def test_succeeded(self):
        """succeeded 状态 → status/reason_text 正确，required_bars=None。"""
        row = _make_chip_row("succeeded", chip_payload={"consensus": 0.8})
        result = _build_chip_status_struct(row)
        assert result is not None
        assert result["status"] == "succeeded"
        assert result["reason_text"] == "已计算"
        assert result["required_bars"] is None
        assert result["reason_code"] is None

    def test_skipped_m15_insufficient_from_payload(self):
        """skipped + payload.reason=M15_BARS_INSUFFICIENT → 结构化状态。"""
        row = _make_chip_row(
            "skipped",
            chip_payload={"reason": "M15_BARS_INSUFFICIENT", "actual_bars": 354},
            error_message="15m bars insufficient: 354 < 500",
        )
        result = _build_chip_status_struct(row)
        assert result is not None
        assert result["status"] == "skipped"
        assert result["reason_code"] == "M15_BARS_INSUFFICIENT"
        assert result["actual_bars"] == 354
        assert result["required_bars"] == _CHIP_MIN_15M_BARS
        assert "15 分钟数据不足" in result["reason_text"]
        assert "354" in result["reason_text"]

    def test_skipped_m15_insufficient_from_error_message(self):
        """skipped + 无 payload.reason 但 error_message 含 '15m' → 解析为 M15_BARS_INSUFFICIENT。"""
        row = _make_chip_row(
            "skipped",
            chip_payload=None,
            error_message="15m bars insufficient: 200 < 500",
        )
        result = _build_chip_status_struct(row)
        assert result is not None
        assert result["reason_code"] == "M15_BARS_INSUFFICIENT"
        assert result["required_bars"] == _CHIP_MIN_15M_BARS

    def test_skipped_other_reason(self):
        """skipped + 非 15m 原因 → 通用 skipped。"""
        row = _make_chip_row(
            "skipped",
            chip_payload={"reason": "OTHER_REASON"},
            error_message="some other reason",
        )
        result = _build_chip_status_struct(row)
        assert result is not None
        assert result["status"] == "skipped"
        assert result["reason_code"] == "OTHER_REASON"
        assert result["reason_text"] == "some other reason"

    def test_failed(self):
        """failed 状态 → CHIP_ERROR。"""
        row = _make_chip_row(
            "failed",
            chip_payload=None,
            error_message="compute error: division by zero",
        )
        result = _build_chip_status_struct(row)
        assert result is not None
        assert result["status"] == "failed"
        assert result["reason_code"] == "CHIP_ERROR"
        assert result["reason_text"] == "compute error: division by zero"
        assert result["required_bars"] == _CHIP_MIN_15M_BARS

    def test_failed_no_error_message(self):
        """failed + 无 error_message → 通用失败文本。"""
        row = _make_chip_row("failed", chip_payload=None, error_message=None)
        result = _build_chip_status_struct(row)
        assert result is not None
        assert result["status"] == "failed"
        assert result["reason_text"] == "计算失败"

    def test_created_at_conversion(self):
        """created_at 正确转换为 ISO 字符串。"""
        dt = datetime(2026, 7, 29, 15, 30, 0, tzinfo=UTC)
        row = _make_chip_row("succeeded", chip_payload={}, error_message=None, created_at=dt)
        result = _build_chip_status_struct(row)
        assert result is not None
        assert result["computed_at"] is not None
        # 应包含 2026-07-29 日期部分
        assert "2026-07-29" in result["computed_at"]

    def test_no_created_at(self):
        """created_at=None → computed_at=None。"""
        row = _make_chip_row("succeeded", chip_payload={}, error_message=None, created_at=None)
        result = _build_chip_status_struct(row)
        assert result is not None
        assert result["computed_at"] is None


class TestChipMinBarsConstant:
    """验证 chip 门槛常量。"""

    def test_chip_min_15m_bars_is_500(self):
        assert _CHIP_MIN_15M_BARS == 500
