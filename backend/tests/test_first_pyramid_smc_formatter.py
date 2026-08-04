"""第一金字塔 SMC 事件唯一语义 formatter 合同测试（与前端 smcLabels.ts 矩阵一致）。

[QM-63 canonical 2026-08-04] format_smc_event / format_smc_order_block / get_smc_eq_label
是后端 SMC 事件中文文案唯一来源，禁止各模块重写文案。
"""
from __future__ import annotations

import pytest

from app.domain.first_pyramid_semantics import (
    SmcEventDirection,
    SmcStructureLevel,
    format_smc_event,
    format_smc_order_block,
    get_smc_eq_label,
    normalize_smc_direction,
    normalize_smc_structure_level,
)


# ---------------------------------------------------------------------------
# 方向 / 级别归一
# ---------------------------------------------------------------------------

def test_normalize_direction_accepts_canonical_and_legacy() -> None:
    assert normalize_smc_direction("bullish") is SmcEventDirection.BULLISH
    assert normalize_smc_direction("bearish") is SmcEventDirection.BEARISH
    # 历史兼容
    assert normalize_smc_direction("up") is SmcEventDirection.BULLISH
    assert normalize_smc_direction("down") is SmcEventDirection.BEARISH
    assert normalize_smc_direction(1) is SmcEventDirection.BULLISH
    assert normalize_smc_direction(-1) is SmcEventDirection.BEARISH


def test_normalize_direction_rejects_unknown_and_does_not_default_bearish() -> None:
    # 缺方向 / 未知值一律 None（不默认 bearish）
    assert normalize_smc_direction(None) is None
    assert normalize_smc_direction(0) is None
    assert normalize_smc_direction(True) is None  # bool 不归入方向
    assert normalize_smc_direction("sideways") is None
    assert normalize_smc_direction("unknown") is None


def test_normalize_structure_level_only_swing_internal() -> None:
    assert normalize_smc_structure_level("swing") is SmcStructureLevel.SWING
    assert normalize_smc_structure_level("internal") is SmcStructureLevel.INTERNAL
    # 其余一律 None（不默认 swing）
    assert normalize_smc_structure_level(None) is None
    assert normalize_smc_structure_level("minor") is None
    assert normalize_smc_structure_level("") is None


# ---------------------------------------------------------------------------
# BOS / CHoCH 八组合文案矩阵（与前端 smcLabels.ts 完全一致）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "event_type,level,direction,expected",
    [
        ("BOS", "swing", "bullish", "主要·多头突破"),
        ("BOS", "swing", "bearish", "主要·空头跌破"),
        ("BOS", "internal", "bullish", "短线·多头突破"),
        ("BOS", "internal", "bearish", "短线·空头跌破"),
        ("CHoCH", "swing", "bullish", "主要·转强拐点"),
        ("CHoCH", "swing", "bearish", "主要·转弱拐点"),
        ("CHoCH", "internal", "bullish", "短线·转强拐点"),
        ("CHoCH", "internal", "bearish", "短线·转弱拐点"),
    ],
)
def test_bos_choch_eight_combinations(
    event_type: str, level: str, direction: str, expected: str,
) -> None:
    semantic = format_smc_event(
        event_type=event_type, structure_level=level, direction=direction,
    )
    assert semantic.label == expected
    assert semantic.direction.value == direction
    assert semantic.structure_level.value == level
    assert semantic.inconsistent is False
    assert semantic.diagnostic is None


# ---------------------------------------------------------------------------
# 缺字段显式表达，不猜测
# ---------------------------------------------------------------------------

def test_missing_direction_shows_unknown_direction() -> None:
    semantic = format_smc_event(event_type="BOS", structure_level="swing", direction=None)
    assert semantic.label == "方向未知"
    assert semantic.direction is None
    assert semantic.structure_level is SmcStructureLevel.SWING


def test_missing_level_shows_unknown_level() -> None:
    semantic = format_smc_event(event_type="CHoCH", structure_level=None, direction="bullish")
    assert semantic.label == "级别未知"
    assert semantic.structure_level is None
    assert semantic.direction is SmcEventDirection.BULLISH


def test_missing_both_shows_unknown_structure() -> None:
    semantic = format_smc_event(event_type="BOS", structure_level=None, direction=None)
    assert semantic.label == "结构未知"


def test_unknown_event_type_shows_unknown_structure() -> None:
    semantic = format_smc_event(event_type="OB_CREATED", structure_level="swing", direction="bullish")
    assert semantic.label == "结构未知"
    assert semantic.structure_level is SmcStructureLevel.SWING


# ---------------------------------------------------------------------------
# Order Block 四组合
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "level,direction,expected",
    [
        ("swing", "bullish", "主要·多头承接区"),
        ("swing", "bearish", "主要·空头压制区"),
        ("internal", "bullish", "短线·多头承接区"),
        ("internal", "bearish", "短线·空头压制区"),
    ],
)
def test_order_block_four_combinations(level: str, direction: str, expected: str) -> None:
    semantic = format_smc_order_block(structure_level=level, direction=direction)
    assert semantic.label == expected
    assert semantic.direction.value == direction
    assert semantic.structure_level.value == level


def test_order_block_missing_direction_not_guessed() -> None:
    semantic = format_smc_order_block(structure_level="swing", direction=None)
    assert semantic.label == "方向未知"


# ---------------------------------------------------------------------------
# EQH / EQL（无结构级别，不虚构主要/短线）
# ---------------------------------------------------------------------------

def test_eq_labels() -> None:
    assert get_smc_eq_label("EQH") == "双顶压力"
    assert get_smc_eq_label("EQL") == "双底支撑"
    assert get_smc_eq_label("UNKNOWN") == "结构未知"


# ---------------------------------------------------------------------------
# 方向冲突：不静默择一，输出 diagnostic
# ---------------------------------------------------------------------------

def test_direction_bias_conflict_emits_diagnostic() -> None:
    # direction=bullish 但 bias=-1（bearish）→ 冲突
    semantic = format_smc_event(
        event_type="BOS", structure_level="swing",
        direction="bullish", bias=-1,
    )
    assert semantic.inconsistent is True
    assert semantic.diagnostic is not None
    # direction 优先，label 仍按 bullish 计算
    assert semantic.label == "主要·多头突破"
    assert semantic.direction is SmcEventDirection.BULLISH


def test_bias_derives_direction_when_direction_missing() -> None:
    semantic = format_smc_event(
        event_type="CHoCH", structure_level="internal", direction=None, bias=1,
    )
    assert semantic.direction is SmcEventDirection.BULLISH
    assert semantic.label == "短线·转强拐点"
    assert semantic.inconsistent is False


def test_to_dict_roundtrip() -> None:
    semantic = format_smc_event(event_type="BOS", structure_level="swing", direction="bullish")
    d = semantic.to_dict()
    assert d == {
        "label": "主要·多头突破",
        "direction": "bullish",
        "structureLevel": "swing",
        "arrow": "↑",
        "inconsistent": False,
        "diagnostic": None,
    }
