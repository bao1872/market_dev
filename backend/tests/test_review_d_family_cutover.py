"""Slice 4A4 — D2/D4 filter consumer cutover parity test (pure-unit).

Slice 4A4 cuts the D2 (event freshness) and D4 (concentration) filter inputs
from the legacy Board ``pyramid_v2`` payload over to the canonical Review
``scope_observation``.  D1 / D3 / D5 are intentionally left on ``pyramid_v2``.

Hard requirements locked by this file:

1. D2 reads ONLY ``scope_observation.freshness`` — no fallback to
   ``pyramid_v2.freshness``.
2. D4 reads ONLY
   ``scope_observation.structure.current_state.technical_state.concentration``
   (the technical-state concentration, NOT price/amount) — no fallback to
   ``pyramid_v2.concentration``.
3. When canonical is absent while legacy is present, D2/D4 must NOT fire
   (missing/unavailable semantics, no silent fallback).
4. D1 / D3 / D5 remain unchanged on ``pyramid_v2``.
5. Thresholds/score/filter name/match semantics are frozen — only the data
   *source* changes (same input value -> same D result).

Hard gate: the production D2/D4 evaluators MUST NOT read ``pyramid_v2`` at all
(asserted via source inspection, so a regression back to the legacy lookup turns
this suite red).
"""
from __future__ import annotations

import inspect

import pytest

from app.domain.review.filter_engine import _eval_d2_event_freshness_high, _eval_d4_concentration_high
from app.domain.review.filter_definitions import (
    FilterDefinition,
    FilterFamily,
    build_rank_key,
    compare_rank_keys,
)


# --------------------------------------------------------------------------- #
# Minimal FilterDefinition stubs (evaluator dispatch via filter_engine registry)
# --------------------------------------------------------------------------- #
def _filter(evaluator: str) -> FilterDefinition:
    """Build a bare FilterDefinition wired to a registered evaluator.

    FilterDefinition.evaluate() dispatches special evaluators through the
    set_evaluator registry in filter_engine (already imported above).
    """
    return FilterDefinition(
        signal_type="dummy",
        family=FilterFamily.D,
        description="slice 4a4 test",
        evaluator=evaluator,
    )


_FILTER_D2 = _filter("eval_d2_event_freshness_high")
_FILTER_D4 = _filter("eval_d4_concentration_high")
_FILTER_D1 = _filter("eval_d1_state_migration_positive")
_FILTER_D3 = _filter("eval_d3_breadth_expansion")
_FILTER_D5 = _filter("eval_d5_relative_strength_strong")


def _canonical_ctx(
    *,
    density: float | None = 0.45,
    today_count: float | None = 2,
    last_5d_count: float | None = 5,
    hhi: float | None = 0.15,
    top5_num: float | None = 0.5,
    top5_den: float | None = 1.0,
    leader_median_gap: float | None = 3.5,
    **obs_fields,
) -> dict:
    """Build a filter context with a canonical scope_observation section."""
    concentration: dict = {}
    if hhi is not None:
        concentration["hhi"] = hhi
    if top5_num is not None:
        concentration["top5_contribution"] = {
            "numerator": top5_num,
            "denominator": top5_den if top5_den is not None else 1.0,
        }
    if leader_median_gap is not None:
        concentration["leader_median_gap"] = leader_median_gap
    concentration["leader_symbol"] = "000001"

    freshness: dict = {}
    if density is not None:
        freshness["decay_weighted_density"] = density
    if today_count is not None:
        freshness["today_count"] = today_count
    if last_5d_count is not None:
        freshness["last_5d_count"] = last_5d_count
    # Slice 4A3 canonical freshness always carries the full 0..20 shape; inject
    # the extras used by D2's thresholds deterministically.
    freshness.setdefault("last_10d_count", 0)
    freshness.setdefault("last_20d_count", 0)

    obs = {
        "freshness": freshness,
        "structure": {
            "current_state": {
                "technical_state": {"concentration": concentration},
            },
        },
        **obs_fields,
    }
    return {
        "P": {"value": 50, "status": "ready", "components": []},
        "Q": {"value": 50, "status": "ready", "components": []},
        "U": {"value": 50, "status": "ready", "components": []},
        "C": {"value": 50, "status": "ready", "components": []},
        "V": {"value": 50, "status": "ready", "components": []},
        "coverage": 0.98,
        "scope_observation": obs,
    }


def _board_ctx(*, freshness=None, concentration=None, diffusion=None, rs=None) -> dict:
    """Build a filter context carrying only the legacy pyramid_v2 payload."""
    pv2: dict = {}
    if freshness is not None:
        pv2["freshness"] = freshness
    if concentration is not None:
        pv2["concentration"] = concentration
    if diffusion is not None:
        pv2["diffusion"] = diffusion
    if rs is not None:
        pv2["relative_strength"] = rs
    return {
        "P": {"value": 50, "status": "ready", "components": []},
        "Q": {"value": 50, "status": "ready", "components": []},
        "U": {"value": 50, "status": "ready", "components": []},
        "C": {"value": 50, "status": "ready", "components": []},
        "V": {"value": 50, "status": "ready", "components": []},
        "coverage": 0.98,
        "pyramid_v2": pv2,
    }


# --------------------------------------------------------------------------- #
# D2 — canonical freshness cutover
# --------------------------------------------------------------------------- #
def test_d2_only_canonical_freshness_works() -> None:
    ctx = _canonical_ctx(density=0.45, today_count=1, last_5d_count=0)
    ctx.pop("pyramid_v2", None)
    assert _FILTER_D2.evaluate(ctx) is True, "only-canonical freshness must fire D2"


def test_d2_uses_canonical_over_legacy_on_conflict() -> None:
    # canonical density=0.45 (fires) vs legacy density=0.05 (would not fire).
    ctx = _canonical_ctx(density=0.45, today_count=1, last_5d_count=0)
    ctx["pyramid_v2"] = {
        "freshness": {
            "decay_weighted_density": 0.05,
            "today_count": 0,
            "last_5d_count": 0,
        }
    }
    assert _FILTER_D2.evaluate(ctx) is True, "D2 must read canonical (0.45), not legacy (0.05)"


def test_d2_canonical_missing_does_not_fallback_to_legacy() -> None:
    ctx = _board_ctx(
        freshness={
            "decay_weighted_density": 0.8,
            "today_count": 5,
            "last_5d_count": 9,
        }
    )
    assert "scope_observation" not in ctx
    assert _FILTER_D2.evaluate(ctx) is False, "canonical absent -> D2 must NOT fall back to legacy"


def test_d2_canonical_absent_entirely() -> None:
    ctx = {"P": {}, "Q": {}, "U": {}, "C": {}, "V": {}, "coverage": 0.98}
    assert _FILTER_D2.evaluate(ctx) is False


def test_d2_threshold_frozen() -> None:
    # density exactly 0.3 with today_count>=1 fires (>=0.3 frozen).
    assert _FILTER_D2.evaluate(
        _canonical_ctx(density=0.3, today_count=1, last_5d_count=0)
    ) is True
    # density 0.299999 -> no fire (strict freeze, <0.3).
    assert _FILTER_D2.evaluate(
        _canonical_ctx(density=0.299999, today_count=1, last_5d_count=0)
    ) is False


# --------------------------------------------------------------------------- #
# D4 — canonical technical-state concentration cutover
# --------------------------------------------------------------------------- #
def test_d4_only_canonical_concentration_works() -> None:
    ctx = _canonical_ctx(hhi=0.15, top5_num=0.5, top5_den=1.0, leader_median_gap=3.5)
    ctx.pop("pyramid_v2", None)
    assert _FILTER_D4.evaluate(ctx) is True, "only-canonical concentration must fire D4"


def test_d4_uses_canonical_over_legacy_on_conflict() -> None:
    # canonical hhi=0.15 (fires) vs legacy hhi=0.01 (would not fire).
    ctx = _canonical_ctx(hhi=0.15, top5_num=0.5, top5_den=1.0, leader_median_gap=3.5)
    ctx["pyramid_v2"] = {
        "concentration": {
            "hhi": 0.01,
            "top5_contribution": {"numerator": 0.01, "denominator": 1.0},
            "leader_median_gap": 5.0,
        }
    }
    assert _FILTER_D4.evaluate(ctx) is True, "D4 must read canonical (0.15), not legacy (0.01)"


def test_d4_canonical_missing_does_not_fallback_to_legacy() -> None:
    ctx = _board_ctx(
        concentration={
            "hhi": 0.5,
            "top5_contribution": {"numerator": 0.8, "denominator": 1.0},
            "leader_median_gap": 5.0,
        }
    )
    assert "scope_observation" not in ctx
    assert _FILTER_D4.evaluate(ctx) is False, "canonical absent -> D4 must NOT fall back to legacy"


def test_d4_ignores_price_amount_concentration() -> None:
    # only price/amount concentration present in canonical -> not technical-state.
    ctx = _canonical_ctx(hhi=None, top5_num=None, leader_median_gap=None)
    ctx["scope_observation"]["price"] = {"concentration": {"hhi": 0.9}}
    ctx["scope_observation"]["amount"] = {"concentration": {"hhi": 0.9}}
    assert _FILTER_D4.evaluate(ctx) is False


def test_d4_threshold_frozen() -> None:
    # hhi exactly 0.1 + gap>0 fires (>=0.1 frozen).
    assert _FILTER_D4.evaluate(
        _canonical_ctx(hhi=0.1, top5_num=0.0, top5_den=1.0, leader_median_gap=1.0)
    ) is True
    # hhi 0.999999 (just below) -> no fire.
    assert _FILTER_D4.evaluate(
        _canonical_ctx(hhi=0.0999999, top5_num=0.0, top5_den=1.0, leader_median_gap=1.0)
    ) is False


# --------------------------------------------------------------------------- #
# D1 / D3 / D5 regression — remain on legacy pyramid_v2
# --------------------------------------------------------------------------- #
def test_d1_unchanged_reads_legacy_diffusion() -> None:
    ctx = _board_ctx(
        diffusion={
            "positive_migration_count": 8,
            "negative_migration_count": 3,
            "positive_ratio": {"numerator": 8, "denominator": 11},
        }
    )
    assert _FILTER_D1.evaluate(ctx) is True
    # canonical-only (no legacy) must NOT fire D1.
    assert _FILTER_D1.evaluate(_canonical_ctx()) is False


def test_d3_unchanged_reads_legacy_diffusion() -> None:
    ctx = _board_ctx(
        diffusion={
            "participation_coverage": {"numerator": 15, "denominator": 40},
            "total_migration_count": 11,
        }
    )
    assert _FILTER_D3.evaluate(ctx) is True
    assert _FILTER_D3.evaluate(_canonical_ctx()) is False


def test_d5_unchanged_reads_legacy_relative_strength() -> None:
    ctx = _board_ctx(
        rs={
            "vs_market": {"ratio": 1.25, "label": "strong", "diff": 0.15},
            "equal_weight_diff": 0.15,
        }
    )
    assert _FILTER_D5.evaluate(ctx) is True
    assert _FILTER_D5.evaluate(_canonical_ctx()) is False


# --------------------------------------------------------------------------- #
# Production source gate — D2/D4 MUST NOT read pyramid_v2
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "evaluator",
    [_eval_d2_event_freshness_high, _eval_d4_concentration_high],
)
def test_d2_d4_production_source_has_no_pyramid_v2_lookup(evaluator) -> None:
    src = inspect.getsource(evaluator)
    # Strip the docstring so a mention inside documentation does not trip the gate.
    if '"""' in src:
        src = src.split('"""', 2)[2]
    # Must not read the legacy payload either via the helper or direct index.
    assert "_get_pyramid_v2(" not in src, (
        f"D2/D4 production source must NOT read pyramid_v2: {evaluator.__name__}"
    )
    assert '["pyramid_v2"]' not in src and '.get("pyramid_v2")' not in src, (
        f"D2/D4 production source must NOT read pyramid_v2: {evaluator.__name__}"
    )
    assert "_get_scope_observation(" in src, (
        f"D2/D4 production source must read canonical scope_observation: "
        f"{evaluator.__name__}"
    )