"""Round 2.1 — Structure Alignment canonical key consumption (GAP-L1-STRUCTURE-
ALIGNMENT-KEY) tests.

These tests exercise the SINGLE formal production chain:

    previous_state_to_flat (member_fact.py)   -> emits fp_structure_alignment
    -> RawMemberFacts (observation_prep.py)
    -> build_member_observation (observation_prep.py)   [single member owner]
    -> compute_scope_observation (scope_observation.py) [single scope owner]

We do NOT re-implement any production algorithm here.  The only transparent oracle
is the canonical vocabulary mapping the system already owns:

    Up + Up   -> 共振 / aligned
    Up + Down -> 背离 / divergent

No DB, no network.  pure_unit marker.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.domain.review.member_fact import previous_state_to_flat
from app.domain.review.scope_observation import compute_scope_observation
from app.services.observation_prep import RawMemberFacts, build_member_observation

pytestmark = pytest.mark.pure_unit

TRADE = date(2026, 8, 5)


def _state(swing_bias: int, internal_bias: int) -> dict:
    # swing_bias / internal_bias drive the canonical structure_alignment derivation
    # inside previous_state_to_flat when the stored field is absent.
    return {
        "regime_value": 1,
        "swing_bias": swing_bias,
        "internal_bias": internal_bias,
        "sqzmom_val": 1.0,
    }


def _member_obs(member_id: str, swing_bias: int, internal_bias: int):
    flat = previous_state_to_flat(_state(swing_bias, internal_bias))
    raw = RawMemberFacts(member_id=member_id, flat_t=flat, close_t=10.0, amount_t=100.0)
    return build_member_observation(raw)


def _scope(obs_list):
    return compute_scope_observation(
        scope_type="concept",
        scope_key="k",
        trade_date=TRADE,
        pit_member_ids=[o.member_id for o in obs_list],
        members=obs_list,
    )


def test_align_01_swing_up_internal_up_aligned() -> None:
    """ALIGN-01: Swing Up + Internal Up -> member alignment canonical aligned/共振."""
    obs = _member_obs("m1", swing_bias=1, internal_bias=1)
    # Canonical member-level vocabulary emitted by previous_state_to_flat.
    assert obs.structure_alignment_categorical == "共振"
    # Scope maps to the canonical StructureAlignment.ALIGNED vocabulary.
    al = _scope([obs]).get("structure", {}).get("alignment", {})
    assert al.get("aligned_count") == 1
    assert al.get("divergent_count") == 0


def test_align_02_swing_up_internal_down_divergent() -> None:
    """ALIGN-02: Swing Up + Internal Down -> member alignment canonical divergent/背离."""
    obs = _member_obs("m2", swing_bias=1, internal_bias=-1)
    assert obs.structure_alignment_categorical == "背离"
    al = _scope([obs]).get("structure", {}).get("alignment", {})
    assert al.get("aligned_count") == 0
    assert al.get("divergent_count") == 1


def test_align_03_scope_alignment_denominator_gt_zero() -> None:
    """ALIGN-03: valid swing/internal members -> Scope Alignment denominator > 0."""
    members = [
        _member_obs("a", swing_bias=1, internal_bias=1),    # 共振
        _member_obs("b", swing_bias=1, internal_bias=-1),   # 背离
        _member_obs("c", swing_bias=-1, internal_bias=-1),  # 共振
    ]
    al = _scope(members).get("structure", {}).get("alignment", {})
    assert al.get("denominator") == 3
    assert al.get("aligned_count") == 2
    assert al.get("divergent_count") == 1


def test_align_04_alignment_source_unavailable_is_unavailable() -> None:
    """ALIGN-04: alignment source unavailable -> unavailable, never coerced to 0."""
    # A member with NO state -> previous_state_to_flat({}) emits fp_structure_alignment
    # as None -> the member-level categorical is None -> NOT counted, and must not
    # be forced to an "aligned=0 / divergent=0" fake (denominator stays 0).
    flat_empty = previous_state_to_flat({})
    raw = RawMemberFacts(member_id="n", flat_t=flat_empty, close_t=None, amount_t=None)
    member = build_member_observation(raw)
    assert member.structure_alignment_categorical is None
    al = _scope([member]).get("structure", {}).get("alignment", {})
    # Unavailable -> no fake counts; denominator is 0 (empty), not a false 0 signal.
    assert al.get("denominator") == 0
    assert al.get("aligned_count", 0) == 0
    assert al.get("divergent_count", 0) == 0


def test_align_mixed_members_only_valid_counted() -> None:
    """ALIGN-03b: valid + invalid members -> only valid contribute to denominator."""
    members = [
        _member_obs("a", swing_bias=1, internal_bias=1),  # valid -> 共振
        _member_obs("n", swing_bias=0, internal_bias=0),  # invalid -> no alignment
    ]
    al = _scope(members).get("structure", {}).get("alignment", {})
    assert al.get("denominator") == 1
    assert al.get("aligned_count") == 1
