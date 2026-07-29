"""[CHANGE-20260729-004 P0-2] chipStatus 结构化状态单元测试。

覆盖：
  1. ChipStatus schema 字段约束（state/reasonCode/reasonText/computedAt）
  2. _build_chip_status 从 ChipConsensusResult 构建 ChipStatus 的所有分支
  3. 深科技根因场景：M15_BARS_INSUFFICIENT（INPUT_CONTRACT_VIOLATION）

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_chip_status.py -v
"""
from __future__ import annotations

import pytest

from app.schemas.first_pyramid import (
    CHIP_STATUS_REASON_CODES,
    CHIP_STATUS_STATES,
    ChipConsensusResult,
    ChipStatus,
    DimensionResult,
)
from app.services.first_pyramid_service import _build_chip_status


class TestChipStatusSchema:
    """ChipStatus schema 约束。"""

    def test_state_required(self) -> None:
        with pytest.raises(ValueError, match="state"):
            ChipStatus()  # type: ignore[call-arg]

    def test_valid_states_accepted(self) -> None:
        for state in CHIP_STATUS_STATES:
            cs = ChipStatus(state=state)
            assert cs.state == state

    def test_reason_codes_documented(self) -> None:
        """所有声明的 reasonCode 都在文档集合中。"""
        expected = {
            "CHIP_JOB_PENDING",
            "CHIP_JOB_FAILED",
            "DAILY_BARS_INSUFFICIENT",
            "M15_BARS_INSUFFICIENT",
            "NO_VALID_PEAK",
            "CORE_RUN_MISMATCH",
            "STALE_RESULT",
        }
        assert expected <= CHIP_STATUS_REASON_CODES


class TestBuildChipStatus:
    """_build_chip_status 分支覆盖。"""

    def test_chip_none_returns_pending(self) -> None:
        """chip=None → pending / CHIP_JOB_PENDING。"""
        cs = _build_chip_status(None)
        assert cs.state == "pending"
        assert cs.reasonCode == "CHIP_JOB_PENDING"
        assert cs.reasonText is not None

    def test_chip_error_daily_bars(self) -> None:
        """error 含 insufficient_daily → DAILY_BARS_INSUFFICIENT。"""
        chip = ChipConsensusResult(
            chip=None,
            chipHash="h",
            dailyBarsCount=5,
            bars15mCount=0,
            error="INSUFFICIENT_DAILY_BARS",
        )
        cs = _build_chip_status(chip)
        assert cs.state == "unavailable"
        assert cs.reasonCode == "DAILY_BARS_INSUFFICIENT"
        assert "5" in (cs.reasonText or "")

    def test_chip_error_input_contract_violation(self) -> None:
        """error 含 INPUT_CONTRACT_VIOLATION → M15_BARS_INSUFFICIENT（深科技根因）。"""
        chip = ChipConsensusResult(
            chip=None,
            chipHash="h",
            dailyBarsCount=250,
            bars15mCount=338,
            error="INPUT_CONTRACT_VIOLATION",
        )
        cs = _build_chip_status(chip)
        assert cs.state == "unavailable"
        assert cs.reasonCode == "M15_BARS_INSUFFICIENT"
        assert "338" in (cs.reasonText or "")
        assert "4000" in (cs.reasonText or "")

    def test_chip_error_missing_15m(self) -> None:
        """error 含 MISSING_15M_BARS → M15_BARS_INSUFFICIENT。"""
        chip = ChipConsensusResult(
            chip=None,
            chipHash="h",
            dailyBarsCount=250,
            bars15mCount=0,
            error="MISSING_15M_BARS",
        )
        cs = _build_chip_status(chip)
        assert cs.state == "unavailable"
        assert cs.reasonCode == "M15_BARS_INSUFFICIENT"

    def test_chip_error_profile_empty(self) -> None:
        """error 含 PROFILE_EMPTY → NO_VALID_PEAK。"""
        chip = ChipConsensusResult(
            chip=None,
            chipHash="h",
            dailyBarsCount=250,
            bars15mCount=4000,
            error="PROFILE_EMPTY",
        )
        cs = _build_chip_status(chip)
        assert cs.state == "unavailable"
        assert cs.reasonCode == "NO_VALID_PEAK"

    def test_chip_error_unknown(self) -> None:
        """未知 error → failed / CHIP_JOB_FAILED。"""
        chip = ChipConsensusResult(
            chip=None,
            chipHash="h",
            dailyBarsCount=250,
            bars15mCount=4000,
            error="some weird exception",
        )
        cs = _build_chip_status(chip)
        assert cs.state == "failed"
        assert cs.reasonCode == "CHIP_JOB_FAILED"

    def test_chip_no_error_but_chip_none(self) -> None:
        """无 error 但 chip=None → NO_VALID_PEAK。"""
        chip = ChipConsensusResult(
            chip=None,
            chipHash="h",
            dailyBarsCount=250,
            bars15mCount=4000,
            error=None,
        )
        cs = _build_chip_status(chip)
        assert cs.state == "unavailable"
        assert cs.reasonCode == "NO_VALID_PEAK"

    def test_chip_available(self) -> None:
        """chip.available=True → ready。"""
        dim = DimensionResult(
            name="chip_consensus",
            available=True,
            continuousFactors={"poc_price": 29.36},
            events=[],
            statusText="价格在 POC 上方",
        )
        chip = ChipConsensusResult(
            chip=dim,
            chipHash="h",
            dailyBarsCount=250,
            bars15mCount=4000,
            error=None,
        )
        cs = _build_chip_status(chip)
        assert cs.state == "ready"
        assert cs.reasonCode is None

    def test_chip_not_available_no_error(self) -> None:
        """chip.available=False 且无 error → NO_VALID_PEAK 兜底。"""
        dim = DimensionResult(
            name="chip_consensus",
            available=False,
            continuousFactors={},
            events=[],
            statusText="不可用",
        )
        chip = ChipConsensusResult(
            chip=dim,
            chipHash="h",
            dailyBarsCount=250,
            bars15mCount=4000,
            error=None,
        )
        cs = _build_chip_status(chip)
        assert cs.state == "unavailable"
        assert cs.reasonCode == "NO_VALID_PEAK"
