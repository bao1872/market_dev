"""Golden contracts for First Pyramid canonical semantic consumption."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.domain.first_pyramid_semantics import (
    Direction,
    MomentumChange,
    MomentumDirection,
    RegimeDirection,
    SqueezeState,
    StructureAlignment,
    VolumeBadge,
)
from app.domain.review.attribution_engine import compute_instrument_contribution
from app.domain.review.metric_engine import _derive_multi_dim_improving_ratio
from app.services.board_analysis_service import compute_board_payload
from app.services.first_pyramid_semantic_adapter import FirstPyramidSemanticAdapter


@pytest.fixture(params=["canonical", "chinese", "english"])
def semantic_flat(request: pytest.FixtureRequest) -> dict[str, object]:
    common: dict[str, object] = {
        "fp_trend_strength": 0.8,
        "fp_dsa_vwap_dev_pct": 2.0,
        "fp_latest_bos_freshness": 0,
        "fp_latest_choch_freshness": 1,
        "fp_latest_ob_freshness": 2,
        "fp_active_ob_count": 2,
        "fp_sqzmom_value": 1.2,
        "fp_volume_ratio20": 1.8,
        "fp_volume_ratio200": 1.2,
        "fp_volume_percentile20": 85.0,
        "fp_volume_percentile200": 70.0,
        "fp_segment_change_pct": 2.0,
        "review_return_1d": 2.0,
        "review_amount_ratio20": 1.5,
        "review_previous_first_pyramid": {
            "fp_trend_direction": "sideways",
            "fp_swing_direction": "sideways",
            "fp_internal_direction": "down",
            "fp_momentum_direction": "flat",
            "fp_momentum_change": "flat",
        },
    }
    variants: dict[str, dict[str, object]] = {
        "canonical": {
            "fp_trend_direction": Direction.UP,
            "fp_swing_direction": Direction.UP,
            "fp_internal_direction": Direction.DOWN,
            "fp_structure_alignment": StructureAlignment.DIVERGENT,
            "fp_latest_bos_direction": Direction.UP,
            "fp_latest_choch_direction": Direction.DOWN,
            "fp_latest_ob_direction": RegimeDirection.UP,
            "fp_momentum_direction": MomentumDirection.EXPANDING,
            "fp_momentum_change": MomentumChange.ENHANCING,
            "fp_squeeze_state": SqueezeState.RELEASED,
            "fp_volume_badge": VolumeBadge.HIGH,
        },
        "chinese": {
            "fp_trend_direction": "上行",
            "fp_swing_direction": "上行",
            "fp_internal_direction": "下行",
            "fp_structure_alignment": "背离",
            "fp_latest_bos_direction": "上行",
            "fp_latest_choch_direction": "下行",
            "fp_latest_ob_direction": "上行",
            "fp_momentum_direction": "扩张",
            "fp_momentum_change": "增强",
            "fp_squeeze_state": "已释放",
            "fp_volume_badge": "放量",
        },
        "english": {
            "fp_trend_direction": "up",
            "fp_swing_direction": "bullish",
            "fp_internal_direction": "down",
            "fp_structure_alignment": "misaligned",
            "fp_latest_bos_direction": "up",
            "fp_latest_choch_direction": "bearish",
            "fp_latest_ob_direction": 1,
            "fp_momentum_direction": "up",
            "fp_momentum_change": "increasing",
            "fp_squeeze_state": "squeeze_off",
            "fp_volume_badge": "high",
        },
    }
    return {**common, **variants[str(request.param)]}


def test_adapter_normalizes_canonical_chinese_and_english(
    semantic_flat: dict[str, object],
) -> None:
    semantics = FirstPyramidSemanticAdapter(semantic_flat)
    assert semantics.trend is Direction.UP
    assert semantics.swing is Direction.UP
    assert semantics.internal is Direction.DOWN
    assert semantics.structure_alignment is StructureAlignment.DIVERGENT
    assert semantics.event_direction("fp_latest_bos_direction") is Direction.UP
    assert semantics.event_direction("fp_latest_choch_direction") is Direction.DOWN
    assert semantics.event_direction("fp_latest_ob_direction") is Direction.UP
    assert semantics.momentum_direction is MomentumDirection.EXPANDING
    assert semantics.momentum_change is MomentumChange.ENHANCING
    assert semantics.squeeze_state is SqueezeState.RELEASED
    assert semantics.volume_badge is VolumeBadge.HIGH


def test_board_and_review_consumers_have_semantic_parity(
    semantic_flat: dict[str, object],
) -> None:
    board = compute_board_payload([semantic_flat])
    assert board["trend_dist"] == {"up": 1, "down": 0, "neutral": 0}
    assert board["structure"]["swing_up"] == 1
    assert board["structure"]["alignment_misaligned"] == 1
    assert board["momentum"]["positive"] == 1
    assert board["momentum"]["enhancing"] == 1
    assert board["volume"]["high"] == 1
    assert _derive_multi_dim_improving_ratio([semantic_flat]) == 1.0

    metrics = {
        code: {"rawValue": 0.0, "value": 0.0}
        for code in ("P", "Q", "U", "C", "V")
    }
    contribution = compute_instrument_contribution(
        semantic_flat,
        metrics,
        parent_ready_count=1,
    )
    assert contribution["Q"] == 1.0
    assert contribution["U"] == 1.0


def test_producers_do_not_import_consumption_adapter() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    producers = (
        repo_root / "backend/app/strategy/selectors/dsa_selector.py",
        repo_root / "backend/app/strategy_assets/algorithms/features/sqzmom_lb.py",
        repo_root / "backend/app/strategy_assets/algorithms/features/smc_pine_core.py",
    )
    for path in producers:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert "app.services.first_pyramid_semantic_adapter" not in imports
