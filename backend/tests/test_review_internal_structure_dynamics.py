"""Stage 5B — Internal Structure Dynamics composition boundary tests (PRD §14).

Tests the thin composition owner ``compute_internal_structure_dynamics``:

    compute_internal_structure(payload)   -- Breadth / Capital Tilt / Concentration
    +
    leadership_migration (already-computed LeadershipMigrationFacts)
    =
    complete Internal Structure Dynamics

Boundary contract locked here:
  1. Four facts coexist (breadth / capital_tilt / concentration / leadership_migration).
  2. The first three are EXACTLY equal to the standalone foundation owner
     (semantic-equivalence gate, mismatch = 0).
  3. Leadership is passed through transparently (== input facts), never
     re-interpreted.
  4. Leadership unavailable does NOT make the other three unavailable (local
     availability).
  5. Deterministic + no mutation of payload or facts.
  6. No interpretation leakage (structure_type / stable / rotating / score / risk).

Composition must NOT recompute EW / AW / HHI / contribution / leader-set / Jaccard.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.analysis.internal_structure import (
    compute_internal_structure,
    compute_internal_structure_dynamics,
)
from app.domain.review.scope_observation import (
    MemberObservation,
    compute_scope_observation,
)

pytestmark = pytest.mark.pure_unit

TRADE_DATE = date(2026, 8, 14)


def _m(
    mid: str,
    *,
    return_1d: float | None = None,
    amount: float | None = None,
) -> MemberObservation:
    return MemberObservation(
        member_id=mid,
        price_candidate=True,
        return_1d=return_1d,
        amount=amount,
        trend=Direction.UP,
        swing=Direction.UP,
        internal=Direction.UP,
        momentum=MomentumDirection.EXPANDING,
    )


def _run(members: list[MemberObservation]) -> dict[str, Any]:
    return compute_scope_observation(
        scope_type="industry",
        scope_key="electronics",
        trade_date=TRADE_DATE,
        pit_member_ids=[m.member_id for m in members],
        members=members,
        event_coverage_member_ids=None,
    )


def _migration_facts(*, status: str = "ready") -> dict[str, Any]:
    """A minimal LeadershipMigrationFacts-shaped object for composition passthrough."""
    base: dict[str, Any] = {
        "trade_date": "2026-08-14",
        "status": status,
        "reason": None if status == "ready" else "unavailable_snapshot",
        "coverage": 0.50,
        "previous_direction": 1,
        "current_direction": 1,
        "previous_rankable_count": 3,
        "current_rankable_count": 3,
        "previous_leader_count": 2,
        "current_leader_count": 2,
        "retained_count": 2,
        "entrant_count": 0,
        "exit_count": 0,
        "previous_retention": 1.0,
        "jaccard_stability": 1.0,
        "migration": 0.0,
        "previous_leader_ids": ("a", "b"),
        "current_leader_ids": ("a", "b"),
        "entrant_ids": (),
        "exit_ids": (),
    }
    if status == "unavailable":
        base.update(
            previous_leader_count=None,
            current_leader_count=None,
            retained_count=None,
            entrant_count=None,
            exit_count=None,
            previous_retention=None,
            jaccard_stability=None,
            migration=None,
            previous_leader_ids=None,
            current_leader_ids=None,
            entrant_ids=None,
            exit_ids=None,
        )
    return base


def _ready_members() -> list[MemberObservation]:
    return [
        _m("a", return_1d=0.02, amount=50.0),
        _m("b", return_1d=0.01, amount=30.0),
        _m("c", return_1d=-0.005, amount=20.0),
    ]


# ---------------------------------------------------------------------------
# 1. Four facts coexist
# ---------------------------------------------------------------------------


def test_four_facts_coexist() -> None:
    out = compute_internal_structure_dynamics(_run(_ready_members()), _migration_facts())
    assert set(out.keys()) == {"breadth", "capital_tilt", "concentration", "leadership_migration"}


# ---------------------------------------------------------------------------
# 2. First three == standalone foundation (semantic equivalence, mismatch 0)
# ---------------------------------------------------------------------------


def test_foundation_equivalence_mismatch_zero() -> None:
    members = _ready_members()
    payload = _run(members)
    foundation = compute_internal_structure(payload)
    full = compute_internal_structure_dynamics(payload, _migration_facts())

    assert full["breadth"] == foundation["breadth"]
    assert full["capital_tilt"] == foundation["capital_tilt"]
    assert full["concentration"] == foundation["concentration"]


# ---------------------------------------------------------------------------
# 3. Leadership passed through transparently (no re-interpretation)
# ---------------------------------------------------------------------------


def test_leadership_passthrough_equal_to_input() -> None:
    facts = _migration_facts()
    full = compute_internal_structure_dynamics(_run(_ready_members()), facts)
    assert full["leadership_migration"] == facts


def test_leadership_not_reinterpreted() -> None:
    # Composition must not add any interpretation to the leadership facts.
    out = compute_internal_structure_dynamics(_run(_ready_members()), _migration_facts())
    lm = out["leadership_migration"]
    forbidden = ("structure_type", "stable", "rotating", "strong", "weak",
                 "risk", "opportunity", "score", "signal", "confidence")
    text = json.dumps(lm).lower()
    for token in forbidden:
        assert token not in text, f"leadership re-interpreted with {token!r}"


# ---------------------------------------------------------------------------
# 4. Local availability — leadership unavailable does not block the other three
# ---------------------------------------------------------------------------


def test_leadership_unavailable_does_not_block_foundation() -> None:
    members = _ready_members()
    payload = _run(members)
    unavail = _migration_facts(status="unavailable")
    full = compute_internal_structure_dynamics(payload, unavail)

    foundation = compute_internal_structure(payload)
    # Foundation facts stay ready/equal even though leadership is unavailable.
    assert full["breadth"] == foundation["breadth"]
    assert full["capital_tilt"] == foundation["capital_tilt"]
    assert full["concentration"] == foundation["concentration"]
    assert full["leadership_migration"]["status"] == "unavailable"
    assert full["leadership_migration"]["migration"] is None
    # The whole internal structure is NOT marked unavailable.
    assert "unavailable" not in {
        str(full["breadth"]["equal_weight_return"]),
        str(full["concentration"]["price_normalized_hhi"]),
    }


# ---------------------------------------------------------------------------
# 5. Deterministic + no mutation
# ---------------------------------------------------------------------------


def test_deterministic_and_no_mutation() -> None:
    members = _ready_members()
    payload = _run(members)
    facts = _migration_facts()
    payload_snap = json.dumps(payload, sort_keys=True)
    facts_snap = json.dumps(facts, sort_keys=True)

    first = compute_internal_structure_dynamics(payload, facts)
    second = compute_internal_structure_dynamics(payload, facts)

    assert first == second
    assert json.dumps(payload, sort_keys=True) == payload_snap
    assert json.dumps(facts, sort_keys=True) == facts_snap


# ---------------------------------------------------------------------------
# 6. No interpretation leakage in the full composition output
# ---------------------------------------------------------------------------


def test_composition_no_interpretation_leakage() -> None:
    out = compute_internal_structure_dynamics(_run(_ready_members()), _migration_facts())
    text = json.dumps(out).lower()
    forbidden = ("strong", "weak", "opportunity", "risk", "score",
                 "phase", "structure_type", "stable", "rotating")
    for token in forbidden:
        assert token not in text, f"composition leaked interpretation token {token!r}"
