"""Unit contract for run-item zero-output classification (§ Phase 3D).

Covers `_classify_history_zero_output` semantics:
- A: input bars < required (59) → INSUFFICIENT_HISTORY skip (not succeeded)
- B: input bars >= required with output → process (succeeded)
- C: no daily bars (empty bars handled by caller) → NO_DAILY_BARS
- D: input bars >= required but compute unexpectedly empty → COMPUTE_EMPTY_UNEXPECTED fail (not skip, not succeeded)
"""
import pytest

from app.services.first_pyramid_history_service import _classify_history_zero_output

REQUIRED = 60


class TestClassifyHistoryZeroOutput:
    def test_a_59_bars_insufficient_history_skip(self) -> None:
        """59 bars + empty daily_state → INSUFFICIENT_HISTORY skip。"""
        decision, reason = _classify_history_zero_output(
            n_input=59, daily_state_rows=[], required_bars=REQUIRED,
        )
        assert decision == "skip"
        assert "INSUFFICIENT_HISTORY" in reason
        assert "input_bars=59" in reason
        assert "required_bars=60" in reason

    def test_b_60_plus_bars_with_output_process(self) -> None:
        """60+ valid bars 且有 daily_state 输出 → process（正常 succeeded 路径）。"""
        decision, reason = _classify_history_zero_output(
            n_input=60, daily_state_rows=[{"d": 1}], required_bars=REQUIRED,
        )
        assert decision == "process"
        assert reason == ""

    def test_b2_250_bars_with_output_process(self) -> None:
        decision, reason = _classify_history_zero_output(
            n_input=250, daily_state_rows=[{}, {}], required_bars=REQUIRED,
        )
        assert decision == "process"
        assert reason == ""

    def test_c_empty_bars_is_no_daily_bars(self) -> None:
        """bars 为空由调用方跳过（NO_DAILY_BARS 类别），不在本 helper 判定。"""
        # 空 bars 情况：n_input=0 < required → INSUFFICIENT_HISTORY skip 已覆盖（0 bars）
        decision, reason = _classify_history_zero_output(
            n_input=0, daily_state_rows=[], required_bars=REQUIRED,
        )
        # 0 bars 也应 skip（而非 fail），但 reason 是 insufficient
        assert decision == "skip"
        assert "INSUFFICIENT_HISTORY" in reason

    def test_d_60_bars_empty_compute_fails_closed(self) -> None:
        """60 bars 但 compute 意外返回空 → COMPUTE_EMPTY_UNEXPECTED fail（不得 skip/succeeded）。"""
        decision, reason = _classify_history_zero_output(
            n_input=60, daily_state_rows=[], required_bars=REQUIRED,
            meta_error="DSA factor_per_bar 为空",
        )
        assert decision == "fail"
        assert "COMPUTE_EMPTY_UNEXPECTED" in reason
        assert "DSA factor_per_bar 为空" in reason

    def test_d2_250_bars_empty_compute_fails_closed(self) -> None:
        decision, reason = _classify_history_zero_output(
            n_input=250, daily_state_rows=[], required_bars=REQUIRED,
        )
        assert decision == "fail"
        assert "COMPUTE_EMPTY_UNEXPECTED" in reason

    def test_insufficient_not_confused_with_unexpected(self) -> None:
        """INSUFFICIENT_HISTORY 与 COMPUTE_EMPTY_UNEXPECTED 必须可区分。"""
        skip_dec, skip_reason = _classify_history_zero_output(
            n_input=59, daily_state_rows=[], required_bars=REQUIRED,
        )
        fail_dec, fail_reason = _classify_history_zero_output(
            n_input=60, daily_state_rows=[], required_bars=REQUIRED,
        )
        assert skip_dec == "skip"
        assert fail_dec == "fail"
        assert skip_reason != fail_reason
        assert "INSUFFICIENT_HISTORY" in skip_reason
        assert "COMPUTE_EMPTY_UNEXPECTED" in fail_reason
