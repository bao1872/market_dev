"""Modified-scope pure/unit tests for the Canonical Scope Observation Core (Round 1A + Correction).

Covers the 23 required contracts from `ref/prompt.md` §18 plus the 16 correction
regression contracts from §11.  No DB, no network.
"""

from __future__ import annotations

import json
import math
from datetime import date
from typing import Any

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.scope_observation import (
    MemberObservation,
    StructureEvent,
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
    volume_t: float | None = None,
    vol_ratio200: float | None = None,
    vol_pct20: float | None = None,
    vol_pct200: float | None = None,
    vol_zscore20: float | None = None,
    vol_zscore200: float | None = None,
    regime_strength: float | None = None,
    dsa_dir_bars: float | None = None,
    dsa_vwap_dev_pct: float | None = None,
    segment_bars: float | None = None,
    segment_change_pct: float | None = None,
    segment_slope: float | None = None,
    seg_vol_ratio: float | None = None,
    seg_amt_ratio: float | None = None,
    seg_vol_mean: float | None = None,
    seg_amt_mean_prev: float | None = None,
    segment_direction: float | None = None,
    structure_alignment_categorical: str | None = None,
    active_internal_ob_count: float | None = None,
    active_swing_ob_count: float | None = None,
    volatility_phase: float | None = None,
    momentum_direction_raw: float | None = None,
    momentum_change: float | None = None,
    sqzmom_delta: float | None = None,
    sqzmom_val: float | None = None,
    # CURRENT-ONLY canonical snapshot facts (REVIEW-V23-A-CORRECTION-3).
    release_volume_ratio: float | None = None,
    momentum_volume_relation: str | None = None,
    bb_position: float | None = None,
    bb_width: float | None = None,
    vwap_ret_total: float | None = None,
    trailing_top_pct: float | None = None,
    trailing_bottom_pct: float | None = None,
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
        volume_t=volume_t,
        vol_ratio200=vol_ratio200,
        vol_pct20=vol_pct20,
        vol_pct200=vol_pct200,
        vol_zscore20=vol_zscore20,
        vol_zscore200=vol_zscore200,
        regime_strength=regime_strength,
        dsa_dir_bars=dsa_dir_bars,
        dsa_vwap_dev_pct=dsa_vwap_dev_pct,
        segment_id=None,
        segment_direction=segment_direction,
        segment_bars=segment_bars,
        segment_change_pct=segment_change_pct,
        segment_slope=segment_slope,
        seg_vol_ratio=seg_vol_ratio,
        seg_amt_ratio=seg_amt_ratio,
        seg_vol_mean=seg_vol_mean,
        seg_amt_mean_prev=seg_amt_mean_prev,
        structure_alignment_categorical=structure_alignment_categorical,
        active_internal_ob_count=active_internal_ob_count,
        active_swing_ob_count=active_swing_ob_count,
        volatility_phase=volatility_phase,
        momentum_direction_raw=momentum_direction_raw,
        momentum_change=momentum_change,
        sqzmom_delta=sqzmom_delta,
        sqzmom_val=sqzmom_val,
        release_volume_ratio=release_volume_ratio,
        momentum_volume_relation=momentum_volume_relation,
        bb_position=bb_position,
        bb_width=bb_width,
        vwap_ret_total=vwap_ret_total,
        trailing_top_pct=trailing_top_pct,
        trailing_bottom_pct=trailing_bottom_pct,
    )


def _run(
    members: list[MemberObservation],
    *,
    scope_type: str = "industry",
    scope_key: str = "electronics",
    pit_member_ids: list[str] | None = None,
    pit_member_ids_t1: list[str] | None = None,
    events: list[StructureEvent] | None = None,
    coverage: list[str] | None = None,
) -> dict[str, Any]:
    ids = pit_member_ids if pit_member_ids is not None else [m.member_id for m in members]
    return compute_scope_observation(
        scope_type=scope_type,
        scope_key=scope_key,
        trade_date=TRADE_DATE,
        pit_member_ids=ids,
        pit_member_ids_t1=pit_member_ids_t1,
        members=members,
        events=events or [],
        event_coverage_member_ids=coverage,
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


def test_amount_contributions_are_supplier_order_independent() -> None:
    from app.domain.review.scope_observation import compute_member_amount_contributions

    members = [_m("c", amount=1e16), _m("a", amount=1.0), _m("b", amount=1.0)]
    forward = compute_member_amount_contributions(members)
    reverse = compute_member_amount_contributions(list(reversed(members)))
    assert forward == reverse
    assert [member.member_id for member in forward.members] == ["a", "b", "c"]


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
    # Amount participation remains a threshold-free distribution (PRD Participation).
    assert part["amount"]["valid_count"] == 3
    assert "p25" in part["amount"] and "p50" in part["amount"] and "p75" in part["amount"]
    assert "active_ratio" not in part["amount"]
    assert "expansion_ratio" not in part["amount"]
    # Volume participation is the §7.7 six-fact vector; each fact is its own
    # comparable-continuous distribution — no >1 / >1.5 threshold anywhere.
    # (Only ratio20 is populated by these members; the other five are empty here
    #  but the keys must all exist and carry no threshold fields.)
    for key in ("ratio20", "ratio200", "percentile20", "percentile200", "zscore20", "zscore200"):
        assert key in part["volume"]
        if key == "ratio20":
            assert part["volume"][key]["valid_count"] == 3
        assert "active_ratio" not in part["volume"][key]
        assert "expansion_ratio" not in part["volume"][key]


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
    assert part["volume"]["ratio20"]["valid_count"] == 1
    assert part["amount"]["valid_count"] == 1
    assert part["volume"]["ratio20"]["p50"] == pytest.approx(1.0)
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


def test_amount_hhi_two_member_unequal_distribution() -> None:
    # Single canonical truth for the amount 100/200 fixture used in PG tests.
    # shares = 1/3, 2/3 -> raw_hhi = 5/9, normalized = 1/9.
    out = _run([
        _m("a", amount=100.0),
        _m("b", amount=200.0),
    ])

    amount = out["price"]["amount"]
    concentration = amount["concentration"]

    assert amount["valid_count"] == 2
    assert amount["total_amount"] == pytest.approx(300.0)

    # shares = 1/3, 2/3
    assert concentration["raw_hhi"] == pytest.approx(5.0 / 9.0)

    # normalized = (5/9 - 1/2) / (1 - 1/2) = 1/9
    assert concentration["normalized_hhi"] == pytest.approx(1.0 / 9.0)

    assert concentration["member_count"] == 2
    assert concentration["status"] == "ready"


# ===========================================================================
# Wave 1A — L1 §7.2-§7.6 data-contract closure (REVIEW-V22-W1A-L1-CONTRACT-CLOSURE)
# ===========================================================================


def test_amount_weighted_return_joint_valid_universe() -> None:
    # Joint-valid universe: return finite AND amount finite >= 0.
    # Member c: valid return but amount is None -> excluded from AW (not price).
    # Member d: amount valid but return None -> excluded from AW (weight=0).
    # Weights renormalized INSIDE the joint universe, never the amount-HHI universe.
    members = [
        _m("a", return_1d=0.10, amount=100.0),
        _m("b", return_1d=0.20, amount=300.0),
        _m("c", return_1d=0.50, amount=None),
        _m("d", return_1d=None, amount=200.0),
    ]
    price = _run(members)["price"]
    # EW return uses price-valid universe (a, b, c): c has a finite return and is
    # price-candidate even though its amount is None -> it stays in EW, not AW.
    # (0.10 + 0.20 + 0.50) / 3
    assert price["equal_weight_return"] == pytest.approx(0.26666666666666666)
    # AW return: JOINT-valid universe (a, b only; c dropped for missing amount),
    # weights renormalized inside the joint universe: 100/(100+300), 300/(100+300).
    aw = price["amount_weighted_return"]
    assert aw == pytest.approx((0.10 * 100 + 0.20 * 300) / 400)
    assert price["amount_weighted_return_universe_count"] == 2


def test_amount_weighted_return_negative_amount_excluded() -> None:
    members = [
        _m("a", return_1d=0.10, amount=100.0),
        _m("b", return_1d=0.20, amount=-50.0),
    ]
    price = _run(members)["price"]
    # b's negative amount -> not in joint universe -> AW uses a only.
    assert price["amount_weighted_return"] == pytest.approx(0.10)
    assert price["amount_weighted_return_universe_count"] == 1


def test_total_volume_sum() -> None:
    members = [
        _m("a", volume_t=1000.0),
        _m("b", volume_t=2500.0),
        _m("c", volume_t=None),
    ]
    assert _run(members)["price"]["total_volume"] == pytest.approx(3500.0)


def test_return_dispersion_std() -> None:
    # returns 0.0, 0.02, -0.02 -> population std = sqrt((0.0004+0+0.0004)/3)
    members = [
        _m("a", return_1d=0.0),
        _m("b", return_1d=0.02),
        _m("c", return_1d=-0.02),
    ]
    disp = _run(members)["price"]["return_dispersion"]
    expected = (0.0004 / 3 + 0.0 / 3 + 0.0004 / 3) ** 0.5
    assert disp == pytest.approx(expected)


def test_return_dispersion_single_member_none() -> None:
    # No dispersion space with a single value.
    assert _run([_m("a", return_1d=0.01)])["price"]["return_dispersion"] is None


def test_trend_continuous_facts_median() -> None:
    members = [
        _m("a", regime_strength=0.2, segment_bars=5.0, dsa_vwap_dev_pct=-1.0,
           segment_change_pct=2.0, segment_slope=0.5, seg_vol_ratio=1.1,
           seg_amt_ratio=1.2, seg_vol_mean=100.0, seg_amt_mean_prev=90.0),
        _m("b", regime_strength=0.8, segment_bars=15.0, dsa_vwap_dev_pct=2.0,
           segment_change_pct=-3.0, segment_slope=-0.3, seg_vol_ratio=0.7,
           seg_amt_ratio=0.9, seg_vol_mean=80.0, seg_amt_mean_prev=110.0),
        _m("c", regime_strength=0.5, segment_bars=10.0, dsa_vwap_dev_pct=0.5,
           segment_change_pct=0.0, segment_slope=0.1, seg_vol_ratio=1.0,
           seg_amt_ratio=1.0, seg_vol_mean=95.0, seg_amt_mean_prev=95.0),
    ]
    cont = _run(members)["trend"]["continuous"]
    assert cont["regime_strength"] == pytest.approx(0.5)
    assert cont["segment_bars"] == pytest.approx(10.0)
    assert cont["dsa_vwap_dev_pct"] == pytest.approx(0.5)
    assert cont["segment_change_pct"] == pytest.approx(0.0)
    assert cont["segment_slope"] == pytest.approx(0.1)
    assert cont["segment_volume_mean_ratio"] == pytest.approx(1.0)
    # REVIEW-V23-A-CORRECTION-3: VWAP Return Total is a CURRENT-ONLY snapshot fact.
    # These members carry no snapshot value -> median is None, but there is NO
    # "upstream_unavailable_history_state" status key any more (the missing HISTORY
    # series must not be reported as a Current suppression reason).
    assert cont["vwap_ret_total"] is None
    assert "vwap_ret_total_status" not in cont


def test_trend_segment_direction_categorical() -> None:
    members = [
        _m("a", segment_direction=1.0),
        _m("b", segment_direction=-1.0),
        _m("c", segment_direction=0.0),
        _m("c2", segment_direction=1.0),
    ]
    seg = _run(members)["trend"]["segment_direction"]
    assert seg["denominator"] == 4
    assert seg["up_count"] == 2
    assert seg["down_count"] == 1
    assert seg["neutral_count"] == 1


def test_structure_alignment_and_active_ob_count() -> None:
    # 2026-08-13 CORRECTION: structure_alignment 使用 canonical categorical 语义
    # （First Pyramid 存储的 "aligned" / "divergent"），不再使用 numeric cast。
    members = [
        _m("a", structure_alignment_categorical="aligned", active_internal_ob_count=2.0, active_swing_ob_count=1.0),
        _m("b", structure_alignment_categorical="divergent", active_internal_ob_count=1.0, active_swing_ob_count=0.0),
        _m("c", structure_alignment_categorical="aligned", active_internal_ob_count=0.0, active_swing_ob_count=0.0),
    ]
    structure = _run(members)["structure"]
    align = structure["alignment"]
    # denominator = 带 canonical alignment 的成员数（categorical 无 neutral 类别）。
    assert align["denominator"] == 3
    assert align["aligned_count"] == 2
    assert align["divergent_count"] == 1
    assert align.get("neutral_count", 0) == 0
    # REVIEW-V23-A-CORRECTION-3: Active OB Count is FORMALLY REMOVED from the
    # canonical v2.3 payload -> the key must be ABSENT, not present-but-unavailable.
    assert "active_ob_count" not in structure
    # Distance-to-trailing are CURRENT-ONLY snapshot facts.  These members carry no
    # snapshot value -> unavailable because the SOURCE is missing for every member.
    assert structure["distance_to_trailing_top_pct"]["status"] == "unavailable"
    assert (
        "CURRENT_SOURCE_UNAVAILABLE"
        in structure["distance_to_trailing_top_pct"]["reason"]
    )
    assert structure["distance_to_trailing_bottom_pct"]["status"] == "unavailable"


def test_structure_events_member_dedupe_and_cells() -> None:
    # 2026-08-13 CORRECTION: cell key 使用独立的 Structure Level categorical
    # (Swing/Internal)，与 numeric price level 分离；price level 不再参与聚合。
    events = [
        StructureEvent("a", "BOS", direction="Up", level=10.0, internal=False),
        StructureEvent("a", "BOS", direction="Up", level=10.0, internal=False),  # same cell twice same day
        StructureEvent("b", "BOS", direction="Up", level=10.0, internal=False),
        StructureEvent("c", "OB_CREATED", direction="Down", level=8.0, internal=True),
        StructureEvent("d", "EQH"),
        StructureEvent("e", "EQL"),
        StructureEvent("a", "EQH"),  # extreme: no level, member_count dedup by member
    ]
    # Member "x" is NOT PIT(T) -> must be ignored.
    events.append(StructureEvent("x", "BOS", direction="Up", level=10.0, internal=False))
    structure = _run(
        [_m("a"), _m("b"), _m("c"), _m("d"), _m("e")],
        pit_member_ids=["a", "b", "c", "d", "e"],
        events=events,
        coverage=["a", "b", "c", "d", "e"],
    )["structure"]
    cells = structure["events"]["cells"]
    bos = cells["leveled"]["BOS_Up_Swing"]
    assert bos["event_count"] == 3  # a×2 + b×1
    assert bos["member_count"] == 2  # a, b dedup
    assert bos["member_ratio"] == pytest.approx(2 / 5)
    ob = cells["leveled"]["OB_CREATED_Down_Internal"]
    assert ob["event_count"] == 1 and ob["member_count"] == 1
    assert cells["extreme"]["EQH"]["member_count"] == 2  # d, a
    assert cells["extreme"]["EQH"]["member_ratio"] == pytest.approx(2 / 5)
    assert cells["extreme"]["EQL"]["member_count"] == 1


def test_structure_events_no_level_for_extremes() -> None:
    events = [
        StructureEvent("a", "EQH"),
        StructureEvent("b", "EQL"),
    ]
    cells = _run(
        [_m("a"), _m("b")], pit_member_ids=["a", "b"], events=events, coverage=["a", "b"]
    )["structure"]["events"]["cells"]
    assert "EQH" in cells["extreme"]
    assert cells["extreme"]["EQH"]["event_count"] == 1
    # Ensure extreme cells carry no direction/level payload.
    assert "direction" not in cells["extreme"]["EQH"]
    assert "level" not in cells["extreme"]["EQH"]


def test_release_volume_ratio_is_member_first_not_event_weighted() -> None:
    # REVIEW-V23-A-CORRECTION-3: PRD freezes Release Volume Ratio as MEMBER-FIRST —
    # at most ONE fact per member per day.  The Scope median is taken over the
    # canonical per-member snapshot fact, so a member firing several SQZ_RELEASE raw
    # events must NOT gain extra weight.
    #
    # Member a fires 3 release events, member b fires 1.  Under the old
    # event-weighted aggregation the median would be pulled toward a's values.
    events = [
        StructureEvent("a", "SQZ_RELEASE", release_volume_ratio=1.0),
        StructureEvent("a", "SQZ_RELEASE", release_volume_ratio=2.0),
        StructureEvent("a", "SQZ_RELEASE", release_volume_ratio=3.0),
        StructureEvent("b", "SQZ_RELEASE", release_volume_ratio=10.0),
    ]
    out = _run(
        [
            _m("a", release_volume_ratio=2.0),
            _m("b", release_volume_ratio=10.0),
        ],
        pit_member_ids=["a", "b"],
        events=events,
        coverage=["a", "b"],
    )
    rel = out["momentum"]["release_volume_ratio"]
    # Member-first: values = [2.0 (a), 10.0 (b)] -> median = 6.0.
    # Event-weighted would have been median([1,2,3,10]) = 2.5 -> must NOT happen.
    assert rel["median"] == pytest.approx(6.0)
    assert rel["valid_count"] == 2
    # The event stream is retained as EVIDENCE only: it must not carry a ratio.
    assert "release_volume_ratio" not in out["structure"]["events"]
    # SQZ_RELEASE still appears as an evidence cell, member-deduped (a counted once
    # despite firing 3 raw events).
    release_cell = out["structure"]["events"]["cells"]["extreme"]["SQZ_RELEASE"]
    assert release_cell["member_count"] == 2
    assert release_cell["event_count"] == 4


def test_release_volume_ratio_unavailable_without_current_source() -> None:
    # No member carries a consumable exact-T snapshot value -> the fact reports the
    # missing SOURCE, never a fabricated number and never an algorithm gap.
    rel = _run([_m("a"), _m("b")])["momentum"]["release_volume_ratio"]
    assert rel["status"] == "unavailable"
    assert "CURRENT_SOURCE_UNAVAILABLE" in rel["reason"]


def test_momentum_squeeze_state_and_relation() -> None:
    # 2026-08-13 CORRECTION: Squeeze State 直接继承 canonical fact（First Pyramid
    # volatility_phase / momentum_direction 字符串 token），Review 不再用 numeric phase 重推导。
    members = [
        _m("a", volatility_phase="released", momentum_direction_raw="up"),
        _m("b", volatility_phase="no_squeeze", momentum_direction_raw="down"),
        _m("c", volatility_phase="squeeze", momentum_direction_raw="up"),
    ]
    mom = _run(members)["momentum"]
    squeeze = mom["squeeze_state"]
    assert squeeze["denominator"] == 3
    assert squeeze["squeeze_release_count"] == 1  # a
    assert squeeze["squeeze_count"] == 1  # c
    assert squeeze["non_squeeze_count"] == 1  # b
    # REVIEW-V23-A-CORRECTION-3: BB position/width and Momentum/Volume Relation are
    # CURRENT-ONLY snapshot facts.  These members carry no snapshot value, so the
    # reason is a missing SOURCE — NOT ALGORITHM_MAPPING_REQUIRED (Review still does
    # not own the algorithm, but the algorithm is not what is missing).
    assert mom["bb_position"]["status"] == "unavailable"
    assert "CURRENT_SOURCE_UNAVAILABLE" in mom["bb_position"]["reason"]
    assert mom["bb_width"]["status"] == "unavailable"
    relation = mom["momentum_volume_relation"]
    assert relation["status"] == "unavailable"
    assert "CURRENT_SOURCE_UNAVAILABLE" in relation["reason"]
    assert "ALGORITHM_MAPPING_REQUIRED" not in relation["reason"]


def test_current_only_facts_served_from_snapshot_without_history() -> None:
    # REVIEW-V23-A-CORRECTION-3 / PRD v2.3: "Current ready + Historical missing" is a
    # LEGAL state.  A missing member-day history series must NOT suppress Current.
    members = [
        _m("a", release_volume_ratio=1.0, momentum_volume_relation="confirmation",
           bb_position=0.2, bb_width=0.05, vwap_ret_total=1.0,
           trailing_top_pct=-3.0, trailing_bottom_pct=7.0),
        _m("b", release_volume_ratio=3.0, momentum_volume_relation="divergence",
           bb_position=0.8, bb_width=0.15, vwap_ret_total=5.0,
           trailing_top_pct=-1.0, trailing_bottom_pct=9.0),
        _m("c", release_volume_ratio=2.0, momentum_volume_relation="confirmation",
           bb_position=0.5, bb_width=0.10, vwap_ret_total=3.0,
           trailing_top_pct=-2.0, trailing_bottom_pct=8.0),
    ]
    out = _run(members)

    mom = out["momentum"]
    assert mom["release_volume_ratio"]["median"] == pytest.approx(2.0)
    assert mom["release_volume_ratio"]["valid_count"] == 3
    assert mom["bb_position"]["median"] == pytest.approx(0.5)
    assert mom["bb_width"]["median"] == pytest.approx(0.10)

    # Momentum/Volume Relation is an OPEN categorical fact -> member-ratio.
    relation = mom["momentum_volume_relation"]
    assert relation["denominator"] == 3
    assert relation["confirmation_count"] == 2
    assert relation["divergence_count"] == 1
    assert relation["confirmation_ratio"] == pytest.approx(2 / 3)

    # VWAP Return Total is served from the snapshot (member-weighted median).
    assert out["trend"]["continuous"]["vwap_ret_total"] == pytest.approx(3.0)

    structure = out["structure"]
    assert structure["distance_to_trailing_top_pct"]["median"] == pytest.approx(-2.0)
    assert structure["distance_to_trailing_bottom_pct"]["median"] == pytest.approx(8.0)
    # Active OB Count stays formally removed regardless of Current availability.
    assert "active_ob_count" not in structure


def test_momentum_volume_relation_preserves_canonical_categories() -> None:
    # The canonical producer emits Chinese enum values ("共振"/"背离").  Review must
    # consume them VERBATIM: it does not own the category vocabulary, so it must not
    # translate, bucket, or silently drop an unrecognised category.
    members = [
        _m("a", momentum_volume_relation="共振"),
        _m("b", momentum_volume_relation="背离"),
        _m("c", momentum_volume_relation="共振"),
        _m("d"),  # no consumable Current value -> excluded from the denominator
    ]
    rel = _run(members)["momentum"]["momentum_volume_relation"]
    assert rel["denominator"] == 3
    assert rel["共振_count"] == 2
    assert rel["背离_count"] == 1
    assert rel["共振_ratio"] == pytest.approx(2 / 3)
    assert rel["背离_ratio"] == pytest.approx(1 / 3)


def test_volume_20_200_six_fact_medians() -> None:
    members = [
        _m("a", vol_ratio20=0.8, vol_ratio200=1.1, vol_pct20=20.0,
           vol_pct200=30.0, vol_zscore20=-1.0, vol_zscore200=-0.5),
        _m("b", vol_ratio20=1.2, vol_ratio200=0.9, vol_pct20=70.0,
           vol_pct200=40.0, vol_zscore20=0.5, vol_zscore200=0.2),
        _m("c", vol_ratio20=2.0, vol_ratio200=1.5, vol_pct20=90.0,
           vol_pct200=80.0, vol_zscore20=1.5, vol_zscore200=1.0),
    ]
    vol = _run(members)["participation"]["volume"]
    assert vol["ratio20"]["p50"] == pytest.approx(1.2)
    assert vol["ratio200"]["p50"] == pytest.approx(1.1)
    assert vol["percentile20"]["p50"] == pytest.approx(70.0)
    assert vol["percentile200"]["p50"] == pytest.approx(40.0)
    assert vol["zscore20"]["p50"] == pytest.approx(0.5)
    assert vol["zscore200"]["p50"] == pytest.approx(0.2)


def test_unavailable_is_not_zero() -> None:
    out = _run([_m("a")])
    # None of the "unavailable" markers should be coerced to 0.
    # REVIEW-V23-A-CORRECTION-2: turnover is removed from the v2.3 canonical
    # Scope payload entirely (no longer a target fact); it must not be present.
    assert "turnover" not in out["price"]
    assert out["trend"]["continuous"]["vwap_ret_total"] is None
    assert out["structure"]["distance_to_trailing_top_pct"]["status"] == "unavailable"
    assert out["momentum"]["bb_position"]["status"] == "unavailable"
    assert out["chip"]["status"] == "unavailable"


def test_persistence_section_keys_passthrough_no_recompute() -> None:
    # The persistence layer just stores the canonical payload; this test verifies
    # the Core output carries all top-level L1 sections without recomputation.
    out = _run([_m("a", trend=Direction.UP, swing=Direction.UP, internal=Direction.UP,
                   momentum=MomentumDirection.EXPANDING, return_1d=0.01, amount=100.0)])
    for section in ("price", "trend", "structure", "momentum", "participation", "chip", "scope"):
        assert section in out


# === REVIEW-V23-A-CORRECTION-2: contract tests for the four contract deviations ===


def test_momentum_volume_relation_unavailable_without_current_source() -> None:
    # REVIEW-V23-A-CORRECTION-3: Review MUST NOT synthesize a squeeze x momentum
    # cross.  With squeeze/momentum present but NO canonical relation value, the fact
    # reports the missing Current SOURCE (not an algorithm gap) and never a derived
    # category.
    members = [
        _m("a", volatility_phase="released", momentum_direction_raw="up"),
        _m("b", volatility_phase="squeeze", momentum_direction_raw="down"),
        _m("c", volatility_phase="no_squeeze", momentum_direction_raw="up"),
    ]
    rel = _run(members)["momentum"]["momentum_volume_relation"]
    assert rel["status"] == "unavailable"
    assert "CURRENT_SOURCE_UNAVAILABLE" in rel["reason"]
    # A synthesized "Release·Up" / "Squeeze·Down" cross must NOT appear anywhere.
    payload = json.dumps(_run(members))
    assert "Release·Up" not in payload
    assert "Squeeze·Down" not in payload


def test_release_volume_ratio_ignores_event_stream_entirely() -> None:
    # REVIEW-V23-A-CORRECTION-3: the raw event stream must have NO influence on the
    # Current Release Volume Ratio.  Here the events carry ratios that would produce
    # median 2.5 under the old event-weighted aggregation, while the canonical
    # per-member snapshot facts are absent -> the fact must report the missing SOURCE
    # rather than silently falling back to event values.
    events = [
        StructureEvent("a", "SQZ_RELEASE", release_volume_ratio=1.0),
        StructureEvent("a", "SQZ_RELEASE", release_volume_ratio=2.0),
        StructureEvent("a", "SQZ_RELEASE", release_volume_ratio=3.0),
        StructureEvent("b", "SQZ_RELEASE", release_volume_ratio=10.0),
    ]
    rel = _run(
        [_m("a"), _m("b")], pit_member_ids=["a", "b"], events=events, coverage=["a", "b"]
    )["momentum"]["release_volume_ratio"]
    assert rel["status"] == "unavailable"
    assert "CURRENT_SOURCE_UNAVAILABLE" in rel["reason"]
    assert "median" not in rel


def test_canonical_scope_payload_excludes_active_ob_count() -> None:
    # PRD v2.3 FORMALLY REMOVES Active OB Count from the canonical Scope payload.
    # REVIEW-V23-A-CORRECTION-3: "excludes" means the key is ABSENT — a present key
    # with status="unavailable" does NOT satisfy the contract (same rule as Turnover).
    out = _run([_m("a", active_internal_ob_count=2.0, active_swing_ob_count=1.0)])
    assert "active_ob_count" not in out["structure"]
    payload = json.dumps(out)
    assert "active_ob_count" not in payload


def test_canonical_scope_payload_excludes_turnover() -> None:
    # PRD v2.3 removes Turnover Rate from the canonical Scope payload entirely.
    out = _run([_m("a")])
    assert "turnover" not in out["price"]
    payload = json.dumps(out)
    assert '"turnover"' not in payload
