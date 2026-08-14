"""Pure-unit tests for the Observation Primitive Registry.

The registry owns only: canonical path, extraction rule, and availability
behavior.  It must NOT compute percentile / velocity / acceleration / persistence
/ regime / signal.  These tests lock in the extraction contract consumed by C1
and the future Analysis B/C modules.
"""
from __future__ import annotations

import pytest

from app.domain.review.observation_primitives import (
    OBSERVATION_PRIMITIVES,
    ObservationPrimitiveSpec,
    get_primitive,
    list_primitive_keys,
)

pytestmark = pytest.mark.pure_unit


def test_registry_contains_expected_keys():
    keys = set(list_primitive_keys())
    assert {
        "equal_weight_return",
        "amount_weighted_return",
        "trend.continuous.regime_strength",
        "participation.volume.ratio20",
        "participation.volume.ratio200",
        "momentum.bb_position",
        "momentum.bb_width",
    }.issubset(keys)


def test_direct_scalar_extraction():
    spec: ObservationPrimitiveSpec = get_primitive("equal_weight_return")
    assert spec.path == ("price", "equal_weight_return")
    assert spec.extract(0.012) == 0.012
    assert spec.extract(float("nan")) is None
    assert spec.extract("x") is None
    assert spec.extract(None) is None


def test_participation_uses_p50():
    spec = get_primitive("participation.volume.ratio20")
    node = {"p25": 1.0, "p50": 2.0, "p75": 3.0, "valid_count": 10}
    assert spec.extract(node) == 2.0
    # Missing / non-finite p50 -> unavailable.
    assert spec.extract({"p25": 1.0, "p50": None, "p75": 3.0}) is None
    assert spec.extract({"not_a_dist": 1}) is None


def test_momentum_uses_median_fallback_when_no_p50():
    """momentum distributions expose ``median`` (== p50); registry must read it."""
    spec = get_primitive("momentum.bb_position")
    node = {"median": 0.5, "p25": 0.3, "p75": 0.7, "valid_count": 8}
    assert spec.extract(node) == 0.5
    # p50 takes precedence if both present.
    assert spec.extract({"p50": 0.42, "median": 0.5}) == 0.42


def test_registry_is_single_source_for_c1_subset():
    """C1_CORE_FIELDS in cross_sectional must be the same specs from the registry."""
    from app.domain.review.analysis.cross_sectional import C1_CORE_FIELDS

    for spec in C1_CORE_FIELDS:
        assert OBSERVATION_PRIMITIVES[spec.key] is spec
        # Backward-compatible attribute aliases.
        assert spec.field_key == spec.key
        assert spec.l1_path == spec.path
