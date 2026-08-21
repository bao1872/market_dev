"""REVIEW-DYNAMICS-COMPOSITION-CONTRACT — boundary adapter regression tests.

Root cause (P0_RUNTIME_WIRING):
- ``compute_current_static_scope_dynamics_batch`` returns a domain object with
  keys ``{scope, membership, observation_series, scope_dynamics, metrics}`` and
  NO top-level ``status``.
- ``compose_canonical_review_scope`` requires every required layer (including
  ``historical_dynamics`` for activated families) to carry a legal top-level
  ``status`` (ready / insufficient_history / unavailable_current) and raises
  ``ReviewCompositionError`` when it is absent.

Fix: ``_adapt_scope_dynamics_to_composition_layer`` is the single application
composition-boundary conversion that derives the status from the canonical
``scope_dynamics["dynamics_phase"]`` tail (target-date phase status), never by
re-deriving history sufficiency.

These tests cover producer-shape → adapter → composition (the missing chain).
Pure unit tests — no DB, no IO.

    PURE_UNIT_TEST=1 backend/.venv/bin/python -m pytest \\
        tests/test_review_dynamics_composition_adapter.py
"""
from __future__ import annotations

import pytest

from app.domain.review.canonical_composition import (
    STATUS_INSUFFICIENT,
    STATUS_READY,
    STATUS_UNAVAILABLE,
    ReviewCompositionError,
    compose_canonical_review_scope,
)
from app.domain.review.review_capability import resolve_scope_capability
from app.services.review_orchestrator_service import (
    _adapt_scope_dynamics_to_composition_layer,
)


# ---------------------------------------------------------------------------
# Helpers — the EXACT producer shape (no top-level status), NOT a hand wrapper
# ---------------------------------------------------------------------------
def _raw_dynamics(phase_status: str = "ready") -> dict:
    """Shape of one ``compute_current_static_scope_dynamics_batch`` item.

    Top-level keys: scope / membership / observation_series / scope_dynamics /
    metrics.  There is deliberately NO top-level ``status`` — that is the
    production producer contract.
    """
    return {
        "scope": {"scope_type": "concept", "scope_key": "k", "scope_name": "TEST"},
        "membership": {"member_count": 5},
        "observation_series": {"primitives": {}},
        "scope_dynamics": {
            "historical_dynamics": {
                "position": [{"trade_date": "2026-08-19", "value": 0.1, "status": phase_status}],
                "ema5": [{"trade_date": "2026-08-19", "value": 0.1, "status": phase_status}],
            },
            "dynamics_phase": [{"trade_date": "2026-08-19", "phase": None, "status": phase_status}],
        },
        "metrics": {"trade_date_count": 1},
    }


def _layers(phase_status: str = "ready"):
    ob = {"status": STATUS_READY}
    struct = {"status": STATUS_READY}
    lead = {"status": STATUS_READY}
    attr = {"status": STATUS_READY}
    return ob, struct, lead, attr


def _compose_with_raw_dynamics(phase_status: str = "ready"):
    ob, struct, lead, attr = _layers()
    raw = _raw_dynamics(phase_status)
    adapted = _adapt_scope_dynamics_to_composition_layer(raw)
    cap = resolve_scope_capability(scope_type="concept", scope_name="TEST")
    return compose_canonical_review_scope(
        scope_type="concept", scope_key="k", trade_date="2026-08-19", capability=cap,
        scope_observation=ob, historical_dynamics=adapted,
        internal_structure_facts=struct, leadership=lead, member_attribution=attr,
    ), adapted


# ---------------------------------------------------------------------------
# Gate 1 — adapter derives a legal status from the real producer shape
# ---------------------------------------------------------------------------
def test_adapter_adds_legal_status_from_real_dynamics_shape() -> None:
    for phase_status, expected in (
        ("ready", STATUS_READY),
        ("insufficient_history", STATUS_INSUFFICIENT),
        ("unavailable_current", STATUS_UNAVAILABLE),
    ):
        raw = _raw_dynamics(phase_status)
        adapted = _adapt_scope_dynamics_to_composition_layer(raw)
        # top-level status was added; raw payload keys preserved
        assert adapted["status"] == expected
        assert adapted["scope"] is raw["scope"]
        assert adapted["scope_dynamics"] is raw["scope_dynamics"]


def test_adapter_falls_back_unavailable_when_no_phase_evidence() -> None:
    raw = {
        "scope": {"scope_type": "concept", "scope_key": "k", "scope_name": "TEST"},
        "membership": {},
        "observation_series": {},
        "scope_dynamics": {},  # no dynamics_phase series
        "metrics": {},
    }
    adapted = _adapt_scope_dynamics_to_composition_layer(raw)
    assert adapted["status"] == STATUS_UNAVAILABLE


def test_adapter_is_idempotent_on_existing_valid_status() -> None:
    already = {"status": "ready", "extra": 1}
    adapted = _adapt_scope_dynamics_to_composition_layer(already)
    assert adapted is already  # unchanged object, not re-wrapped


# ---------------------------------------------------------------------------
# Gate 2 — raw dynamics (via adapter) + composition: previously raised, now PASS
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("phase_status,expected_readiness", [
    ("ready", STATUS_READY),
    ("insufficient_history", STATUS_INSUFFICIENT),
    ("unavailable_current", STATUS_UNAVAILABLE),
])
def test_raw_dynamics_through_adapter_composes(phase_status, expected_readiness) -> None:
    comp, _adapted = _compose_with_raw_dynamics(phase_status)
    assert comp["composition_readiness"] == expected_readiness


def test_raw_dynamics_without_adapter_raises() -> None:
    """The regression this fixes: raw producer shape (no top-level status) must
    raise ReviewCompositionError when passed directly, proving the adapter is the
    required boundary conversion."""
    ob, struct, lead, attr = _layers()
    raw = _raw_dynamics("ready")
    cap = resolve_scope_capability(scope_type="concept", scope_name="TEST")
    with pytest.raises(ReviewCompositionError):
        compose_canonical_review_scope(
            scope_type="concept", scope_key="k", trade_date="2026-08-19", capability=cap,
            scope_observation=ob, historical_dynamics=raw,  # NO status → raises
            internal_structure_facts=struct, leadership=lead, member_attribution=attr,
        )


def test_adapter_preserves_real_dynamics_entering_ready_composition() -> None:
    """Full chain: real producer shape → adapter → activated composition == ready,
    and the real scope_dynamics payload is retained in the composition layer."""
    comp, adapted = _compose_with_raw_dynamics("ready")
    assert comp["composition_readiness"] == STATUS_READY
    assert comp["historical_dynamics"]["scope_dynamics"] is adapted["scope_dynamics"]
    assert comp["historical_dynamics"]["status"] == STATUS_READY
