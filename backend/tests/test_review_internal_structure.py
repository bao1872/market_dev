"""Focused tests for Analysis C Internal Structure Foundation (PRD §14).

The L1 payloads are produced by the REAL canonical producer
(``scope_observation.compute_scope_observation``) from ``MemberObservation``
inputs — no hand-written, shape-distorted fake payload.  This proves the
derived view consumes the canonical L1 shape verbatim.

Covered contract surface:

1. Breadth passthrough — advance/decline/unchanged/dispersion == canonical L1
2. Capital Tilt positive (AW - EW)
3. Capital Tilt negative
4. Capital Tilt unavailable -> None, never 0 (missing EW / missing AW)
5. Concentration passthrough — no recompute, equals L1 canonical HHI
6. No interpretation leakage — forbidden semantics absent from output
7. Deterministic output + no mutation of the input payload

No DB, no recomputation of L1 facts, no Leadership Migration.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.analysis.internal_structure import compute_internal_structure
from app.domain.review.scope_observation import (
    MemberObservation,
    compute_scope_observation,
)

pytestmark = pytest.mark.pure_unit

TRADE_DATE = date(2026, 8, 14)


def _m(
    mid: str,
    *,
    price_candidate: bool = True,
    return_1d: float | None = None,
    amount: float | None = None,
) -> MemberObservation:
    """Minimal MemberObservation builder (canonical required fields only)."""
    return MemberObservation(
        member_id=mid,
        price_candidate=price_candidate,
        return_1d=return_1d,
        amount=amount,
        trend=Direction.UP,
        swing=Direction.UP,
        internal=Direction.UP,
        momentum=MomentumDirection.EXPANDING,
    )


def _run(members: list[MemberObservation]) -> dict[str, Any]:
    """Produce a real canonical L1 observation payload (single source path)."""
    return compute_scope_observation(
        scope_type="industry",
        scope_key="electronics",
        trade_date=TRADE_DATE,
        pit_member_ids=[m.member_id for m in members],
        members=members,
    )


# ---------------------------------------------------------------------------
# Test 1 — Breadth passthrough == canonical L1
# ---------------------------------------------------------------------------


def test_breadth_passthrough_matches_l1() -> None:
    members = [
        _m("a", return_1d=2.0),
        _m("b", return_1d=-1.0),
        _m("c", return_1d=0.0),
        _m("d", return_1d=3.0),
    ]
    payload = _run(members)
    breadth = compute_internal_structure(payload)["breadth"]

    assert breadth["advance_ratio"] == payload["price"]["breadth"]["advance_ratio"]
    assert breadth["decline_ratio"] == payload["price"]["breadth"]["decline_ratio"]
    assert breadth["unchanged_ratio"] == payload["price"]["breadth"]["unchanged_ratio"]
    assert breadth["return_dispersion"] == payload["price"]["return_dispersion"]
    assert breadth["equal_weight_return"] == payload["price"]["equal_weight_return"]


# ---------------------------------------------------------------------------
# Test 2 / 3 — Capital Tilt sign
# ---------------------------------------------------------------------------


def test_capital_tilt_positive() -> None:
    # EW = mean(0.5, 1.5) = 1.0; AW = (0.5*0 + 1.5*100) / 100 = 1.5.
    members = [_m("a", return_1d=0.5, amount=0.0), _m("b", return_1d=1.5, amount=100.0)]
    tilt = compute_internal_structure(_run(members))["capital_tilt"]
    assert tilt["equal_weight_return"] == pytest.approx(1.0)
    assert tilt["amount_weighted_return"] == pytest.approx(1.5)
    assert tilt["capital_tilt"] == pytest.approx(0.5)


def test_capital_tilt_negative() -> None:
    # EW = mean(0.5, 1.5) = 1.0; AW = (0.5*90 + 1.5*10) / 100 = 0.6.
    members = [_m("a", return_1d=0.5, amount=90.0), _m("b", return_1d=1.5, amount=10.0)]
    tilt = compute_internal_structure(_run(members))["capital_tilt"]
    assert tilt["equal_weight_return"] == pytest.approx(1.0)
    assert tilt["amount_weighted_return"] == pytest.approx(0.6)
    assert tilt["capital_tilt"] == pytest.approx(-0.4)


# ---------------------------------------------------------------------------
# Test 4 — Capital Tilt unavailable -> None, never 0
# ---------------------------------------------------------------------------


def test_capital_tilt_unavailable_when_aw_missing() -> None:
    # Amount missing -> AW universe empty -> AW None, EW finite -> tilt None.
    members = [_m("a", return_1d=1.0, amount=None), _m("b", return_1d=2.0, amount=None)]
    tilt = compute_internal_structure(_run(members))["capital_tilt"]
    assert tilt["equal_weight_return"] == pytest.approx(1.5)
    assert tilt["amount_weighted_return"] is None
    assert tilt["capital_tilt"] is None


def test_capital_tilt_unavailable_when_ew_missing() -> None:
    # No price-valid member -> EW None, AW finite -> tilt None (not 0).
    members = [_m("a", price_candidate=False, return_1d=1.0, amount=100.0)]
    tilt = compute_internal_structure(_run(members))["capital_tilt"]
    assert tilt["equal_weight_return"] is None
    assert tilt["amount_weighted_return"] == pytest.approx(1.0)
    assert tilt["capital_tilt"] is None


# ---------------------------------------------------------------------------
# Test 5 — Concentration passthrough == canonical L1 (no recompute)
# ---------------------------------------------------------------------------


def test_concentration_passthrough_matches_l1() -> None:
    # Amount concentrated on "a" -> amount normalized HHI = 1.0; equal returns
    # -> price normalized HHI = 0.0 (the "资金集中、价格扩散" structure case).
    members = [
        _m("a", return_1d=1.0, amount=100.0),
        _m("b", return_1d=1.0, amount=0.0),
        _m("c", return_1d=1.0, amount=0.0),
    ]
    payload = _run(members)
    conc = compute_internal_structure(payload)["concentration"]

    assert conc["price_normalized_hhi"] == payload["price"]["concentration"]["normalized_hhi"]
    assert conc["amount_normalized_hhi"] == payload["price"]["amount"]["concentration"]["normalized_hhi"]
    assert conc["price_normalized_hhi"] == pytest.approx(0.0)
    assert conc["amount_normalized_hhi"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Test 6 — No interpretation leakage
# ---------------------------------------------------------------------------


def test_no_interpretation_leakage() -> None:
    members = [_m("a", return_1d=1.0), _m("b", return_1d=-1.0), _m("c", return_1d=0.0)]
    out = compute_internal_structure(_run(members))

    assert set(out.keys()) == {"breadth", "capital_tilt", "concentration"}
    forbidden = (
        "strong",
        "weak",
        "opportunity",
        "risk",
        "score",
        "phase",
        "structure_type",
        "leadership",
    )
    text = json.dumps(out).lower()
    for token in forbidden:
        assert token not in text, f"forbidden interpretation token leaked: {token}"


# ---------------------------------------------------------------------------
# Test 7 — Deterministic output + no input mutation
# ---------------------------------------------------------------------------


def test_deterministic_and_no_input_mutation() -> None:
    members = [_m("a", return_1d=1.0, amount=50.0), _m("b", return_1d=-1.0, amount=50.0)]
    payload = _run(members)
    snapshot = json.dumps(payload, sort_keys=True)

    first = compute_internal_structure(payload)
    second = compute_internal_structure(payload)

    assert first == second
    assert json.dumps(payload, sort_keys=True) == snapshot
