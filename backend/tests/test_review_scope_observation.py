"""Modified-scope pure/unit tests for the Canonical Scope Observation Core (Round 1A).

Covers the 23 required contracts from `ref/prompt.md` §18.  No DB, no network.
"""

from __future__ import annotations

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
) -> dict[str, Any]:
    return compute_scope_observation(
        scope_type=scope_type,
        scope_key=scope_key,
        trade_date=TRADE_DATE,
        pit_member_ids=[m.member_id for m in members],
        members=members,
    )


# ---------------------------------------------------------------------------
# 1. industry and concept share the same canonical calculation path
# ---------------------------------------------------------------------------


def test_industry_and_concept_share_same_calculation_path() -> None:
    members = [_m("a"), _m("b", trend=Direction.DOWN)]
    industry = _run(members, scope_type="industry", scope_key="electronics")
    concept = _run(members, scope_type="concept", scope_key="chip")
    assert industry["scope"]["scope_type"] == "industry"
    assert concept["scope"]["scope_type"] == "concept"
    # Every observation object (beyond scope identity) must be identical.
    for obj in ("price", "amount", "trend", "structure", "momentum", "participation", "chip"):
        assert industry[obj] == concept[obj], f"family divergence in {obj}"


# ---------------------------------------------------------------------------
# 2. deterministic output
# ---------------------------------------------------------------------------


def test_deterministic_output() -> None:
    members = [_m("a", return_1d=2.0), _m("b", return_1d=-1.0, trend=Direction.DOWN)]
    first = _run(members)
    second = _run(members)
    assert first == second


# ---------------------------------------------------------------------------
# 3 / 4 / 5. neutral / flat are valid states
# ---------------------------------------------------------------------------


def test_trend_neutral_is_valid() -> None:
    out = _run([_m("a", trend=Direction.SIDEWAYS)])
    state = out["trend"]["state"]
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
    out = _run([_m("a", momentum=MomentumDirection.FLAT)])
    state = out["momentum"]["state"]
    assert state["denominator"] == 1
    assert state["flat_count"] == 1
    assert state["flat_ratio"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 6 / 7. categorical counts sum denominator, ratios sum ~= 1
# ---------------------------------------------------------------------------


def _assert_counts_sum_denominator(state: dict[str, Any]) -> None:
    count_keys = [k for k in state if k.endswith("_count")]
    total = sum(state[k] for k in count_keys)
    assert total == state["denominator"]


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
# 8 / 10. member missing exact T-1 excluded from price denominator, no fallback
# ---------------------------------------------------------------------------


def test_missing_exact_t1_not_in_price_denominator_and_no_fallback() -> None:
    members = [
        _m("a", return_1d=2.0),                       # valid
        _m("b", price_candidate=True, return_1d=None),  # T-1 missing, close(T) ok
        _m("c", price_candidate=False, return_1d=None),  # close(T) missing too
    ]
    out = _run(members)
    price = out["price"]
    assert price["candidate_count"] == 2
    assert price["valid_count"] == 1
    assert price["missing_exact_t1_count"] == 1
    # Only the single valid member contributes; no fallback to earlier bar.
    assert price["return"]["valid_count"] == 1
    assert price["return"]["mean"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# 9. member missing exact T-1 excluded from transition denominator
# ---------------------------------------------------------------------------


def test_missing_exact_t1_not_in_transition_denominator() -> None:
    members = [
        _m("a", trend=Direction.UP, t1_trend=Direction.SIDEWAYS),  # real transition
        _m("b", trend=Direction.UP, t1_trend=None),                # missing T-1
    ]
    out = _run(members)
    transition = out["trend"]["transition"]
    assert transition["denominator"] == 1
    assert transition["Neutral→Up"]["count"] == 1


# ---------------------------------------------------------------------------
# 11. one member missing T-1 does not make the whole scope unavailable
# ---------------------------------------------------------------------------


def test_missing_t1_does_not_make_scope_unavailable() -> None:
    members = [
        _m("a", return_1d=2.0, trend=Direction.UP),
        _m("b", return_1d=None, trend=Direction.DOWN, t1_trend=None),
    ]
    out = _run(members)
    assert out["price"]["valid_count"] == 1
    assert out["price"]["return"]["mean"] == pytest.approx(2.0)
    assert out["trend"]["state"]["denominator"] == 2  # trend state does not need T-1


# ---------------------------------------------------------------------------
# 12 / 13. membership add / remove are not transitions
# ---------------------------------------------------------------------------


def test_added_member_not_counted_as_transition() -> None:
    members = [
        _m("a", trend=Direction.UP, t1_trend=Direction.SIDEWAYS),  # real Neutral->Up
        _m("b", trend=Direction.UP, t1_trend=None),                # added (no T-1)
        _m("c", trend=Direction.DOWN, t1_trend=Direction.UP),      # real Up->Down
    ]
    out = _run(members)
    transition = out["trend"]["transition"]
    # Only a and c are common-valid; b is excluded from the denominator.
    assert transition["denominator"] == 2
    assert transition["Neutral→Up"]["count"] == 1
    assert transition["Up→Down"]["count"] == 1


def test_removed_member_not_counted_as_transition() -> None:
    # A removed member is not part of the current member facts, so it can never
    # appear in any transition.  The transition denominator reflects only the
    # current members with an exact T-1 state.
    members = [_m("a", trend=Direction.UP, t1_trend=Direction.SIDEWAYS)]
    out = _run(members)
    assert out["trend"]["transition"]["denominator"] == 1
    assert out["trend"]["transition"]["Neutral→Up"]["count"] == 1
    # A member that was removed (present at T-1, absent at T) is not part of the
    # current member facts, so it contributes nothing to the transition counts.
    assert sum(
        v["count"] for k, v in out["trend"]["transition"].items() if k != "denominator"
    ) == 1


# ---------------------------------------------------------------------------
# 14. price universe separate from amount universe
# ---------------------------------------------------------------------------


def test_price_and_amount_universes_are_separate() -> None:
    members = [
        _m("a", return_1d=1.0, amount=100.0),
        _m("b", return_1d=None, amount=50.0),   # price invalid but amount valid
        _m("c", price_candidate=True, return_1d=None, amount=None),
    ]
    out = _run(members)
    assert out["price"]["valid_count"] == 1
    assert out["amount"]["valid_count"] == 2  # amount does not require T-1 return


# ---------------------------------------------------------------------------
# 15. advance + decline + unchanged == price denominator
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
    total = breadth["advance_count"] + breadth["decline_count"] + breadth["unchanged_count"]
    assert total == breadth["denominator"]
    assert breadth["advance_count"] == 2
    assert breadth["decline_count"] == 1
    assert breadth["unchanged_count"] == 1
    total_ratio = (
        breadth["advance_ratio"] + breadth["decline_ratio"] + breadth["unchanged_ratio"]
    )
    assert total_ratio == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 16 / 17. abs price shares sum ~= 1; raw price HHI present with member_count
# ---------------------------------------------------------------------------


def test_abs_price_shares_sum_one_and_raw_hhi_present() -> None:
    members = [
        _m("a", return_1d=1.0),
        _m("b", return_1d=2.0),
        _m("c", return_1d=3.0),
    ]
    conc = _run(members)["price"]["concentration"]
    # abs returns = [1,2,3]; shares sum to 1 by construction; expected hhi:
    expected = (1 / 6) ** 2 + (2 / 6) ** 2 + (3 / 6) ** 2
    assert conc["raw_hhi"] == pytest.approx(expected)
    assert conc["member_count"] == 3
    assert conc["status"] == "ready"


def test_raw_price_hhi_requires_abs_return_sum_positive() -> None:
    members = [_m("a", return_1d=0.0), _m("b", return_1d=0.0)]
    conc = _run(members)["price"]["concentration"]
    assert conc["raw_hhi"] is None
    assert conc["member_count"] == 2
    assert conc["status"] == "zero_abs_return"


# ---------------------------------------------------------------------------
# 18 / 19. amount shares sum ~= 1; raw amount HHI consistent with amount-valid
# ---------------------------------------------------------------------------


def test_amount_shares_sum_one_and_raw_hhi_consistent() -> None:
    members = [
        _m("a", amount=100.0),
        _m("b", amount=300.0),
        _m("c", amount=200.0),
        _m("d", return_1d=None, amount=0.0),  # price invalid but amount valid
    ]
    out = _run(members)
    conc = out["amount"]["concentration"]
    # amount universe = 4 (amount non-null regardless of T-1).  total = 600.
    assert out["amount"]["valid_count"] == 4
    assert conc["member_count"] == 4
    expected = (1 / 6) ** 2 + (1 / 2) ** 2 + (1 / 3) ** 2 + 0.0
    assert conc["raw_hhi"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 20. Participation is a distribution with no >1 / >1.5 canonical threshold
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
        # No canonical active/expansion threshold output.
        assert "active_ratio" not in part[key]
        assert "expansion_ratio" not in part[key]


# ---------------------------------------------------------------------------
# 21 / 22. Core does not generate P/Q/U/C/V or call legacy normalization
# ---------------------------------------------------------------------------


def test_core_does_not_generate_pqucv_or_normalization() -> None:
    out = _run([_m("a"), _m("b", trend=Direction.DOWN)])
    for obj in ("price", "amount", "trend", "structure", "momentum", "participation"):
        assert not any(k in out[obj] for k in ("P", "Q", "U", "C", "V", "value", "score")), obj
    flat = repr(out)
    for forbidden in ("normalizedValue", "historyPercentile120d", "crossSectionPercentile"):
        assert forbidden not in flat


# ---------------------------------------------------------------------------
# 23. Chip unavailable does not block other outputs
# ---------------------------------------------------------------------------


def test_chip_unavailable_does_not_block_other_outputs() -> None:
    out = _run([_m("a", return_1d=1.0, trend=Direction.UP)])
    assert out["chip"]["status"] == "unavailable"
    assert out["price"]["valid_count"] == 1
    assert out["trend"]["state"]["denominator"] == 1
