"""Modified-scope pure/unit tests for the Canonical Scope Observation Core (Round 1A + Correction).

Covers the 23 required contracts from `ref/prompt.md` §18 plus the 16 correction
regression contracts from §11.  No DB, no network.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.scope_observation import (
    MemberObservation,
    compute_scope_observation,
)

TRADE_DATE = date(2026, 8, 10)


def _m(
    mid: str,
    *,
    price_candidate: bool = True,
    return_1d: float | None = 1.0,
    amount: float | None = 100.0,
    trend: Direction | None = Direction.UP,
    swing: Direction | None = Direction.UP,
    internal: Direction | None = Direction.UP,
    momentum: MomentumDirection | None = MomentumDirection.EXPANDING,
    t1_trend: Direction | None = None,
    t1_swing: Direction | None = None,
    t1_internal: Direction | None = None,
    t1_momentum: MomentumDirection | None = None,
    vol_ratio20: float | None = 1.0,
    amt_ratio20: float | None = 1.0,
) -> MemberObservation:
    return MemberObservation(
        member_id=mid,
        price_candidate=price_candidate,
        return_1d=return_1d,
        amount=amount,
        trend=trend,
        swing=swing,
        internal=internal,
        momentum=momentum,
        t1_trend=t1_trend,
        t1_swing=t1_swing,
        t1_internal=t1_internal,
        t1_momentum=t1_momentum,
        vol_ratio20=vol_ratio20,
        amt_ratio20=amt_ratio20,
    )


def _run(
    members: list[MemberObservation],
    *,
    scope_type: str = "industry",
    scope_key: str = "electronics",
    pit_member_ids: list[str] | None = None,
    pit_member_ids_t1: list[str] | None = None,
) -> dict[str, Any]:
    ids = pit_member_ids if pit_member_ids is not None else [m.member_id for m in members]
    return compute_scope_observation(
        scope_type=scope_type,
        scope_key=scope_key,
        trade_date=TRADE_DATE,
        pit_member_ids=ids,
        pit_member_ids_t1=pit_member_ids_t1,
        members=members,
    )


# ---------------------------------------------------------------------------
# §18.1 — industry and concept share the same canonical calculation path
# ---------------------------------------------------------------------------


def test_industry_and_concept_share_same_calculation_path() -> None:
    members = [_m("a"), _m("b", trend=Direction.DOWN)]
    industry = _run(members, scope_type="industry", scope_key="electronics")
    concept = _run(members, scope_type="concept", scope_key="chip")
    assert industry["scope"]["scope_type"] == "industry"
    assert concept["scope"]["scope_type"] == "concept"
    for obj in ("price", "trend", "structure", "momentum", "participation", "chip"):
        assert industry[obj] == concept[obj], f"family divergence in {obj}"
    assert "amount" not in industry
    assert "amount" in industry["price"]


# ---------------------------------------------------------------------------
# §18.2 — deterministic output
# ---------------------------------------------------------------------------


def test_deterministic_output() -> None:
    members = [_m("a", return_1d=2.0), _m("b", return_1d=-1.0, trend=Direction.DOWN)]
    assert _run(members) == _run(members)


# ---------------------------------------------------------------------------
# §18.3/4/5 — neutral / flat are valid states
# ---------------------------------------------------------------------------


def test_trend_neutral_is_valid() -> None:
    state = _run([_m("a", trend=Direction.SIDEWAYS)])["trend"]["state"]
    assert state["denominator"] == 1
    assert state["neutral_count"] == 1
    assert state["neutral_ratio"] == pytest.approx(1.0)


def test_structure_neutral_is_valid() -> None:
    out = _run([_m("a", swing=Direction.SIDEWAYS, internal=Direction.SIDEWAYS)])
    for axis in ("swing", "internal"):
        state = out["structure"][axis]["state"]
        assert state["denominator"] == 1
        assert state["neutral_count"] == 1
        assert state["neutral_ratio"] == pytest.approx(1.0)


def test_momentum_flat_is_valid() -> None:
    state = _run([_m("a", momentum=MomentumDirection.FLAT)])["momentum"]["state"]
    assert state["denominator"] == 1
    assert state["flat_count"] == 1
    assert state["flat_ratio"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# §18.6/7 — categorical counts sum denominator, ratios sum ~= 1
# ---------------------------------------------------------------------------


def _assert_counts_sum_denominator(state: dict[str, Any]) -> None:
    count_keys = [k for k in state if k.endswith("_count")]
    assert sum(state[k] for k in count_keys) == state["denominator"]


def _assert_ratios_sum_one(state: dict[str, Any]) -> None:
    ratio_keys = [k for k in state if k.endswith("_ratio")]
    total = sum(state[k] for k in ratio_keys if state[k] is not None)
    assert total == pytest.approx(1.0, abs=1e-9)


def test_categorical_counts_sum_denominator_and_ratios_sum_one() -> None:
    members = [
        _m("a", trend=Direction.UP, swing=Direction.UP, internal=Direction.UP,
           momentum=MomentumDirection.EXPANDING),
        _m("b", trend=Direction.SIDEWAYS, swing=Direction.DOWN, internal=Direction.SIDEWAYS,
           momentum=MomentumDirection.FLAT),
        _m("c", trend=Direction.DOWN, swing=Direction.SIDEWAYS, internal=Direction.DOWN,
           momentum=MomentumDirection.CONTRACTING),
        _m("d", trend=Direction.UP, swing=Direction.UP, internal=Direction.UP,
           momentum=MomentumDirection.EXPANDING),
    ]
    out = _run(members)
    axes = [
        out["trend"]["state"],
        out["structure"]["swing"]["state"],
        out["structure"]["internal"]["state"],
        out["momentum"]["state"],
    ]
    for state in axes:
        _assert_counts_sum_denominator(state)
        _assert_ratios_sum_one(state)


# ---------------------------------------------------------------------------
# §18.8/10 — missing exact T-1 excluded from price denominator, no fallback
# ---------------------------------------------------------------------------


def test_missing_exact_t1_not_in_price_denominator_and_no_fallback() -> None:
    members = [
        _m("a", return_1d=2.0),
        _m("b", price_candidate=True, return_1d=None),
        _m("c", price_candidate=False, return_1d=None),
    ]
    price = _run(members)["price"]
    assert price["candidate_count"] == 2
    assert price["valid_count"] == 1
    assert price["missing_exact_t1_count"] == 1
    assert price["return"]["valid_count"] == 1
    assert price["return"]["mean"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# §18.9 — missing exact T-1 excluded from transition denominator
# ---------------------------------------------------------------------------


def test_missing_exact_t1_not_in_transition_denominator() -> None:
    members = [
        _m("a", trend=Direction.UP, t1_trend=Direction.SIDEWAYS),
        _m("b", trend=Direction.UP, t1_trend=None),
    ]
    transition = _run(members, pit_member_ids_t1=["a"])["trend"]["transition"]
    assert transition["denominator"] == 1
    assert transition["Neutral→Up"]["count"] == 1


# ---------------------------------------------------------------------------
# §18.11 — one member missing T-1 does not make the whole scope unavailable
# ---------------------------------------------------------------------------


def test_missing_t1_does_not_make_scope_unavailable() -> None:
    members = [
        _m("a", return_1d=2.0, trend=Direction.UP),
        _m("b", return_1d=None, trend=Direction.DOWN, t1_trend=None),
    ]
    out = _run(members)
    assert out["price"]["valid_count"] == 1
    assert out["price"]["return"]["mean"] == pytest.approx(2.0)
    assert out["trend"]["state"]["denominator"] == 2


# ---------------------------------------------------------------------------
# §18.12/13 — membership add / remove are not transitions
# ---------------------------------------------------------------------------


def test_added_member_not_counted_as_transition() -> None:
    members = [
        _m("a", trend=Direction.UP, t1_trend=Direction.SIDEWAYS),
        _m("b", trend=Direction.UP, t1_trend=None),
        _m("c", trend=Direction.DOWN, t1_trend=Direction.UP),
    ]
    transition = _run(members, pit_member_ids_t1=["a", "c"])["trend"]["transition"]
    assert transition["denominator"] == 2
    assert transition["Neutral→Up"]["count"] == 1
    assert transition["Up→Down"]["count"] == 1


def test_removed_member_not_counted_as_transition() -> None:
    members = [_m("a", trend=Direction.UP, t1_trend=Direction.SIDEWAYS)]
    transition = _run(members, pit_member_ids_t1=["a"])["trend"]["transition"]
    assert transition["denominator"] == 1
    assert transition["Neutral→Up"]["count"] == 1
    assert sum(
        v["count"] for k, v in transition.items() if k != "denominator"
    ) == 1


# ---------------------------------------------------------------------------
# §18.14 — price universe separate from amount universe
# ---------------------------------------------------------------------------


def test_price_and_amount_universes_are_separate() -> None:
    members = [
        _m("a", return_1d=1.0, amount=100.0),
        _m("b", return_1d=None, amount=50.0),
        _m("c", price_candidate=True, return_1d=None, amount=None),
    ]
    out = _run(members)
    assert out["price"]["valid_count"] == 1
    assert out["price"]["amount"]["valid_count"] == 2


# ---------------------------------------------------------------------------
# §18.15 — advance + decline + unchanged == price denominator
# ---------------------------------------------------------------------------


def test_price_breadth_counts_sum_denominator() -> None:
    members = [
        _m("a", return_1d=2.0),
        _m("b", return_1d=-1.0),
        _m("c", return_1d=0.0),
        _m("d", return_1d=3.0),
        _m("e", return_1d=None),
    ]
    breadth = _run(members)["price"]["breadth"]
    assert breadth["denominator"] == 4
    assert (
        breadth["advance_count"] + breadth["decline_count"] + breadth["unchanged_count"]
        == breadth["denominator"]
    )
    assert breadth["advance_count"] == 2
    assert breadth["decline_count"] == 1
    assert breadth["unchanged_count"] == 1
    assert (
        breadth["advance_ratio"] + breadth["decline_ratio"] + breadth["unchanged_ratio"]
        == pytest.approx(1.0)
    )


# ---------------------------------------------------------------------------
# §18.16/17 — abs price shares sum ~= 1; raw price HHI present with member_count
# ---------------------------------------------------------------------------


def test_abs_price_shares_sum_one_and_raw_hhi_present() -> None:
    members = [_m("a", return_1d=1.0), _m("b", return_1d=2.0), _m("c", return_1d=3.0)]
    conc = _run(members)["price"]["concentration"]
    expected = (1 / 6) ** 2 + (2 / 6) ** 2 + (3 / 6) ** 2
    assert conc["raw_hhi"] == pytest.approx(expected)
    assert conc["member_count"] == 3
    assert conc["status"] == "ready"


def test_raw_price_hhi_requires_abs_return_sum_positive() -> None:
    conc = _run([_m("a", return_1d=0.0), _m("b", return_1d=0.0)])["price"]["concentration"]
    assert conc["raw_hhi"] is None
    assert conc["member_count"] == 2
    assert conc["status"] == "zero_abs_return"


# ---------------------------------------------------------------------------
# §18.18/19 — amount shares sum ~= 1; raw amount HHI consistent with amount-valid
# ---------------------------------------------------------------------------


def test_amount_shares_sum_one_and_raw_hhi_consistent() -> None:
    members = [
        _m("a", amount=100.0),
        _m("b", amount=300.0),
        _m("c", amount=200.0),
        _m("d", return_1d=None, amount=0.0),
    ]
    out = _run(members)
    price_amount = out["price"]["amount"]
    conc = price_amount["concentration"]
    assert price_amount["valid_count"] == 4
    assert price_amount["total_amount"] == pytest.approx(600.0)
    assert conc["member_count"] == 4

    # Single canonical owner: amount_share vector drives BOTH the shares AND the HHI.
    from app.domain.review.scope_observation import compute_member_amount_contributions

    facts = compute_member_amount_contributions(members)
    assert facts.valid_count == 4
    assert facts.total_amount == pytest.approx(600.0)
    by_id = {m.member_id: m for m in facts.members}
    assert by_id["a"].amount_share == pytest.approx(1 / 6)
    assert by_id["b"].amount_share == pytest.approx(1 / 2)
    assert by_id["c"].amount_share == pytest.approx(1 / 3)
    assert by_id["d"].amount_share == pytest.approx(0.0)
    share_sum = sum(m.amount_share for m in facts.members if m.amount_share is not None)
    assert share_sum == pytest.approx(1.0)

    # amount HHI must equal sum of squared shares from the same owner (no 2nd formula).
    expected = (1 / 6) ** 2 + (1 / 2) ** 2 + (1 / 3) ** 2 + 0.0
    assert conc["raw_hhi"] == pytest.approx(expected)
    assert conc["normalized_hhi"] == pytest.approx((expected - 0.25) / 0.75)


# ---------------------------------------------------------------------------
# §18.20 — Participation is a distribution with no >1 / >1.5 canonical threshold
# ---------------------------------------------------------------------------


def test_participation_is_threshold_free_distribution() -> None:
    members = [
        _m("a", vol_ratio20=0.8, amt_ratio20=1.1),
        _m("b", vol_ratio20=1.2, amt_ratio20=1.6),
        _m("c", vol_ratio20=2.0, amt_ratio20=0.5),
    ]
    part = _run(members)["participation"]
    for key in ("volume", "amount"):
        assert part[key]["valid_count"] == 3
        assert "p25" in part[key] and "p50" in part[key] and "p75" in part[key]
        assert "active_ratio" not in part[key]
        assert "expansion_ratio" not in part[key]


# ---------------------------------------------------------------------------
# §18.21/22 — Core does not generate P/Q/U/C/V or call legacy normalization
# ---------------------------------------------------------------------------


def test_core_does_not_generate_pqucv_or_normalization() -> None:
    out = _run([_m("a"), _m("b", trend=Direction.DOWN)])
    for obj in ("price", "trend", "structure", "momentum", "participation"):
        assert not any(k in out[obj] for k in ("P", "Q", "U", "C", "V", "value", "score")), obj
    assert "amount" not in out
    flat = repr(out)
    for forbidden in ("normalizedValue", "historyPercentile120d", "crossSectionPercentile"):
        assert forbidden not in flat


# ---------------------------------------------------------------------------
# §18.23 — Chip unavailable does not block other outputs
# ---------------------------------------------------------------------------


def test_chip_unavailable_does_not_block_other_outputs() -> None:
    out = _run([_m("a", return_1d=1.0, trend=Direction.UP)])
    assert out["chip"]["status"] == "unavailable"
    assert out["price"]["valid_count"] == 1
    assert out["trend"]["state"]["denominator"] == 1


# ===========================================================================
# Correction regression tests (§11)
# ===========================================================================


def test_non_pit_current_member_rejected() -> None:
    members = [_m("a"), _m("rogue")]
    with pytest.raises(ValueError, match="not in the PIT\\(T\\)"):
        _run(members, pit_member_ids=["a"])


def test_duplicate_member_id_rejected() -> None:
    members = [_m("a"), _m("a")]
    with pytest.raises(ValueError, match="duplicate member_id"):
        _run(members, pit_member_ids=["a"])


def test_candidate_false_return_non_null_excluded() -> None:
    members = [
        _m("a", price_candidate=True, return_1d=1.0),
        _m("b", price_candidate=False, return_1d=99.0),  # must be fully excluded
    ]
    price = _run(members)["price"]
    assert price["candidate_count"] == 1
    assert price["valid_count"] == 1
    assert price["return"]["valid_count"] == 1
    assert price["return"]["mean"] == pytest.approx(1.0)
    assert price["breadth"]["denominator"] == 1
    assert price["concentration"]["member_count"] == 1


def test_valid_count_never_exceeds_candidate_count() -> None:
    members = [
        _m("a", price_candidate=True, return_1d=1.0),
        _m("b", price_candidate=True, return_1d=None),
        _m("c", price_candidate=False, return_1d=5.0),
    ]
    price = _run(members)["price"]
    assert price["valid_count"] <= price["candidate_count"]


def test_missing_exact_t1_count_never_negative() -> None:
    members = [
        _m("a", price_candidate=True, return_1d=1.0),
        _m("b", price_candidate=True, return_1d=None),
        _m("c", price_candidate=False, return_1d=5.0),
    ]
    price = _run(members)["price"]
    assert price["candidate_count"] == 2
    assert price["valid_count"] == 1
    assert price["missing_exact_t1_count"] == 1
    assert price["missing_exact_t1_count"] >= 0


def test_t_added_member_with_valid_t1_state_excluded_from_transition() -> None:
    # "b" is newly added at T (not in PIT(T-1)) but carries a valid t1 state.
    members = [
        _m("a", trend=Direction.UP, t1_trend=Direction.SIDEWAYS),
        _m("b", trend=Direction.DOWN, t1_trend=Direction.UP),
    ]
    transition = _run(members, pit_member_ids_t1=["a"])["trend"]["transition"]
    assert transition["denominator"] == 1
    assert transition["Neutral→Up"]["count"] == 1
    # b's Up->Down must NOT appear: b is not in PIT(T-1).
    assert "Up→Down" not in transition


def test_common_pit_member_included_in_transition() -> None:
    members = [
        _m("a", trend=Direction.UP, t1_trend=Direction.SIDEWAYS),
        _m("b", trend=Direction.DOWN, t1_trend=Direction.UP),
    ]
    transition = _run(members, pit_member_ids_t1=["a", "b"])["trend"]["transition"]
    assert transition["denominator"] == 2
    assert transition["Neutral→Up"]["count"] == 1
    assert transition["Up→Down"]["count"] == 1


def test_removed_member_excluded_from_transition() -> None:
    # "gone" is in PIT(T-1) but removed at T: not part of current facts, so it
    # can never appear in any current transition.
    members = [_m("a", trend=Direction.UP, t1_trend=Direction.SIDEWAYS)]
    transition = _run(
        members,
        pit_member_ids_t1=["a", "gone"],
    )["trend"]["transition"]
    assert transition["denominator"] == 1
    assert sum(v["count"] for k, v in transition.items() if k != "denominator") == 1


def test_newly_added_member_with_exact_t1_price_enters_price() -> None:
    # "b" added at T (not in PIT(T-1)) but has an exact T-1 price bar:
    # it still enters PRICE return/breadth (price does not require T-1 membership).
    members = [
        _m("a", return_1d=1.0),
        _m("b", return_1d=3.0),
    ]
    price = _run(members, pit_member_ids_t1=["a"])["price"]
    assert price["valid_count"] == 2
    assert price["return"]["valid_count"] == 2
    assert price["return"]["mean"] == pytest.approx(2.0)


def test_nan_return_excluded() -> None:
    members = [
        _m("a", return_1d=1.0),
        _m("b", return_1d=math.nan),
    ]
    price = _run(members)["price"]
    assert price["valid_count"] == 1
    assert price["return"]["valid_count"] == 1
    assert price["return"]["mean"] == pytest.approx(1.0)


def test_inf_return_excluded() -> None:
    members = [
        _m("a", return_1d=1.0),
        _m("b", return_1d=math.inf),
    ]
    price = _run(members)["price"]
    assert price["valid_count"] == 1
    assert price["return"]["mean"] == pytest.approx(1.0)


def test_nan_amount_excluded() -> None:
    members = [
        _m("a", amount=100.0),
        _m("b", amount=math.nan),
    ]
    out = _run(members)
    assert out["price"]["amount"]["valid_count"] == 1
    assert out["price"]["amount"]["concentration"]["member_count"] == 1


def test_negative_amount_excluded() -> None:
    members = [
        _m("a", amount=100.0),
        _m("b", amount=-50.0),
    ]
    out = _run(members)
    assert out["price"]["amount"]["valid_count"] == 1
    assert out["price"]["amount"]["concentration"]["member_count"] == 1


def test_inf_amount_excluded() -> None:
    members = [
        _m("a", amount=100.0),
        _m("b", amount=math.inf),
    ]
    out = _run(members)
    assert out["price"]["amount"]["valid_count"] == 1
    assert out["price"]["amount"]["concentration"]["member_count"] == 1


def test_zero_amount_is_valid() -> None:
    members = [
        _m("a", amount=100.0),
        _m("b", amount=0.0),
    ]
    out = _run(members)
    assert out["price"]["amount"]["valid_count"] == 2
    conc = out["price"]["amount"]["concentration"]
    assert conc["member_count"] == 2
    assert conc["status"] == "ready"


def test_nan_participation_excluded() -> None:
    members = [
        _m("a", vol_ratio20=1.0, amt_ratio20=1.0),
        _m("b", vol_ratio20=math.nan, amt_ratio20=math.inf),
    ]
    part = _run(members)["participation"]
    assert part["volume"]["valid_count"] == 1
    assert part["amount"]["valid_count"] == 1
    assert part["volume"]["p50"] == pytest.approx(1.0)
    assert part["amount"]["p50"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# §18.21 — normalized HHI boundaries (ACCEPTED CONTRACT, CHANGE-012)
# ---------------------------------------------------------------------------


def test_normalized_hhi_equal_distribution_is_zero() -> None:
    # price: equal abs returns -> raw=0.25, normalized=0
    out = _run([_m("a", return_1d=1.0), _m("b", return_1d=1.0), _m("c", return_1d=1.0), _m("d", return_1d=1.0)])
    pc = out["price"]["concentration"]
    assert pc["raw_hhi"] == pytest.approx(0.25)
    assert pc["normalized_hhi"] == pytest.approx(0.0)
    # amount: equal amounts -> raw=0.25, normalized=0 (same owner)
    amt = out["price"]["amount"]["concentration"]
    assert amt["normalized_hhi"] == pytest.approx(0.0)


def test_normalized_hhi_concentrated_distribution_is_one() -> None:
    # price: one dominant abs return -> raw=1, normalized=1
    out = _run([_m("a", return_1d=10.0), _m("b", return_1d=0.0), _m("c", return_1d=0.0), _m("d", return_1d=0.0)])
    pc = out["price"]["concentration"]
    assert pc["raw_hhi"] == pytest.approx(1.0)
    assert pc["normalized_hhi"] == pytest.approx(1.0)
    # amount: one dominant amount -> raw=1, normalized=1 (same owner)
    out2 = _run([_m("a", return_1d=1.0, amount=100.0), _m("b", return_1d=1.0, amount=0.0), _m("c", return_1d=1.0, amount=0.0), _m("d", return_1d=1.0, amount=0.0)])
    amt = out2["price"]["amount"]["concentration"]
    assert amt["raw_hhi"] == pytest.approx(1.0)
    assert amt["normalized_hhi"] == pytest.approx(1.0)


def test_normalized_hhi_single_member_none() -> None:
    out = _run([_m("a", return_1d=1.0)])
    pc = out["price"]["concentration"]
    assert pc["raw_hhi"] == pytest.approx(1.0)
    assert pc["normalized_hhi"] is None
    assert pc["status"] == "insufficient_member_count"

    amt = out["price"]["amount"]["concentration"]
    assert amt["raw_hhi"] == pytest.approx(1.0)
    assert amt["normalized_hhi"] is None
    assert amt["status"] == "insufficient_member_count"


def test_zero_total_price_normalized_none() -> None:
    out = _run([_m("a", return_1d=0.0), _m("b", return_1d=0.0)])
    pc = out["price"]["concentration"]
    assert pc["raw_hhi"] is None
    assert pc["normalized_hhi"] is None
    assert pc["status"] == "zero_abs_return"


def test_zero_total_amount_normalized_none() -> None:
    out = _run([_m("a", return_1d=1.0, amount=0.0), _m("b", return_1d=1.0, amount=0.0)])
    amt = out["price"]["amount"]
    assert amt["total_amount"] == pytest.approx(0.0)
    conc = amt["concentration"]
    assert conc["raw_hhi"] is None
    assert conc["normalized_hhi"] is None
    assert conc["status"] == "zero_amount"
    # total=0 -> every valid member amount_share is None (verified via owner)
    from app.domain.review.scope_observation import compute_member_amount_contributions

    facts = compute_member_amount_contributions([_m("a", amount=0.0), _m("b", amount=0.0)])
    assert facts.total_amount == pytest.approx(0.0)
    assert all(m.amount_share is None for m in facts.members)
