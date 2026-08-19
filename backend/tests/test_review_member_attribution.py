"""Tests for REVIEW-MEMBER-ATTRIBUTION-CLOSURE (pure domain, PRD §9.3 review).

Locks the deterministic decomposition + reconciliation contract:

- Direction member sum == canonical AW return.
- Capital Tilt member sum == canonical (AW - EW) over one unified universe, using
  exact 0-weight for members outside each canonical universe.
- Breadth member sets == canonical advance/decline/unchanged counts AND ratios.
- Concentration member ``weight^2`` sum == canonical raw HHI (price + amount).
- Leadership consumes canonical entrant/exit ids verbatim; retained = prev ∩
  current validated against retained_count.  A legitimately empty leader set is
  NOT a snapshot problem: the group is unavailable(empty_leader_set) but the
  member sets still reconcile.
- Availability: missing/NaN/negative never coerced to 0.
- Determinism: member order / set iteration never affects output.
- Raw-vs-display precision (A1-1): reconciliation sums RAW values with
  math.fsum; ranking sorts RAW values; rounding happens only at display time.

All payloads come from the REAL canonical producer
``compute_scope_observation`` — no hand-distorted fake payloads.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.analysis.leadership_migration import (
    AlignedLeadership,
    LeadershipSnapshot,
    compute_leadership_migration,
)
from app.domain.review.analysis.member_attribution import (
    RECONCILIATION_TOLERANCE,
    checksum,
    compute_member_attribution,
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
    price_candidate: bool = True,
) -> MemberObservation:
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


def _run(
    members: list[MemberObservation], *, scope_key: str = "electronics"
) -> dict[str, Any]:
    return compute_scope_observation(
        scope_type="industry",
        scope_key=scope_key,
        trade_date=TRADE_DATE,
        pit_member_ids=[m.member_id for m in members],
        members=members,
        event_coverage_member_ids=None,
    )


def _snapshot(
    member_ids_contribs: list[tuple[str, float]], direction: int
) -> LeadershipSnapshot:
    leader_set = tuple(
        AlignedLeadership(member_id=mid, contribution=contrib, aligned_score=contrib * direction)
        for mid, contrib in member_ids_contribs
    )
    return LeadershipSnapshot(
        trade_date=TRADE_DATE.isoformat(),
        status="ready",
        reason=None,
        direction=direction,
        rankable_count=len(leader_set),
        leader_set=leader_set,
    )


def _nominal_a1() -> dict[str, Any]:
    """The exact worked example from the plan: a=+5%/0.30, b=-4%/0.20, c=+2%/0.10, d=0/0.40.

    AW = 0.3*0.05 + 0.2*(-0.04) + 0.1*0.02 + 0.4*0.0 = 0.015 - 0.008 + 0.002 = 0.009.
    EW = 0.05 + (-0.04) + 0.02 + 0.0 = 0.03 / 4 = 0.0075.
    """
    members = [
        _m("a", return_1d=0.05, amount=30.0),
        _m("b", return_1d=-0.04, amount=20.0),
        _m("c", return_1d=0.02, amount=10.0),
        _m("d", return_1d=0.0, amount=40.0),
    ]
    observation = _run(members)
    return compute_member_attribution(members=members, observation=observation)


# ---------------------------------------------------------------------------
# Reconcilable worked example (A1) - all five gates PASS
# ---------------------------------------------------------------------------


def test_nominal_all_gates_pass() -> None:
    out = _nominal_a1()
    rec = out["reconciliation"]
    assert rec["violation_count"] == 0
    assert out["direction"]["sum_contribution"] == pytest.approx(0.009, abs=RECONCILIATION_TOLERANCE)
    assert out["direction"]["canonical_aw_return"] == pytest.approx(0.009, abs=RECONCILIATION_TOLERANCE)
    assert out["capital_tilt"]["sum_tilt_contribution"] == pytest.approx(0.0015, abs=RECONCILIATION_TOLERANCE)
    # canonical tilt = aw - ew = 0.009 - 0.0075
    for key in ("direction", "capital_tilt", "concentration_price", "concentration_amount", "breadth"):
        assert rec["checks"][key]["pass"] is True, key
    # leadership not provided -> skipped, not a violation
    assert "leadership" in rec["skipped"]
    assert out["leadership"]["status"] == "unavailable"
    assert out["leadership"]["reason"] == "not_provided"


def test_nominal_aw_tilt_numbers() -> None:
    out = _nominal_a1()
    # member a: aw_weight 0.30, contribution 0.30*0.05 = 0.015 (top positive)
    pos = out["direction"]["positive"]
    assert pos[0]["member_id"] == "a"
    assert pos[0]["contribution"] == pytest.approx(0.015, abs=RECONCILIATION_TOLERANCE)
    # member b is the only negative contributor
    neg = out["direction"]["negative"]
    assert [e["member_id"] for e in neg] == ["b"]
    assert neg[0]["contribution"] == pytest.approx(0.008 * -1.0, abs=RECONCILIATION_TOLERANCE)


# ---------------------------------------------------------------------------
# Breadth - member sets + ordering + ratios
# ---------------------------------------------------------------------------


def test_breadth_sets_match_and_order() -> None:
    out = _nominal_a1()
    breadth = out["breadth"]
    assert [e["member_id"] for e in breadth["advance"]] == ["a", "c"]  # return DESC
    assert [e["member_id"] for e in breadth["decline"]] == ["b"]
    assert [e["member_id"] for e in breadth["unchanged"]] == ["d"]
    ck = out["reconciliation"]["checks"]["breadth"]
    assert ck["pass"] is True
    assert ck["ratios_match"] is True


def test_breadth_zero_return_is_unchanged_not_unavailable() -> None:
    out = _nominal_a1()
    assert len(out["breadth"]["unchanged"]) == 1
    assert out["breadth"]["unavailable"] == []


# ---------------------------------------------------------------------------
# Direction ordering + deterministic tie-break
# ---------------------------------------------------------------------------


def test_direction_tie_break_member_id_asc() -> None:
    # a and b have identical contribution; member_id ASC should fix order.
    members = [_m("b", return_1d=0.02, amount=100.0), _m("a", return_1d=0.02, amount=100.0)]
    out = compute_member_attribution(members=members, observation=_run(members))
    pos = out["direction"]["positive"]
    assert [e["member_id"] for e in pos] == ["a", "b"]


# ---------------------------------------------------------------------------
# Concentration - member weight^2 sum == raw HHI
# ---------------------------------------------------------------------------


def test_concentration_price_and_amount_sum_to_raw_hhi() -> None:
    out = _nominal_a1()
    price = out["concentration"]["price"]
    amount = out["concentration"]["amount"]
    # price raw HHI == canonical payload raw_hhi
    assert price["sum_hhi"] == pytest.approx(price["canonical_raw_hhi"], abs=RECONCILIATION_TOLERANCE)
    assert amount["sum_hhi"] == pytest.approx(amount["canonical_raw_hhi"], abs=RECONCILIATION_TOLERANCE)
    # amount HHI share squares: 0.30^2 + 0.20^2 + 0.10^2 + 0.40^2 = 0.30
    assert amount["sum_hhi"] == pytest.approx(0.30, abs=RECONCILIATION_TOLERANCE)


def test_concentration_hhi_sorted_desc() -> None:
    out = _nominal_a1()
    hhis = [e["hhi_contribution"] for e in out["concentration"]["price"]["members"]]
    assert hhis == sorted(hhis, reverse=True)


# ---------------------------------------------------------------------------
# Capital Tilt - sign + sum == canonical (AW - EW)
# ---------------------------------------------------------------------------


def test_capital_tilt_universe_mismatch_still_reconciles() -> None:
    # Member 'p' has a price-valid return but NO amount -> it is NOT in the AW
    # universe.  Exact 0-weight makes the tilt total still == AW - EW (0 violations),
    # while 'p' is excluded from direction.
    members = [
        _m("a", return_1d=0.05, amount=100.0),
        _m("b", return_1d=-0.04, amount=100.0),
        _m("p", return_1d=0.10, amount=None, price_candidate=True),  # price-only
    ]
    observation = _run(members)
    out = compute_member_attribution(members=members, observation=observation)
    dir_ids = [e["member_id"] for e in out["direction"]["positive"] + out["direction"]["negative"]]
    assert "p" not in dir_ids  # no amount -> no direction contribution
    assert out["reconciliation"]["checks"]["capital_tilt"]["pass"] is True
    assert out["reconciliation"]["checks"]["direction"]["pass"] is True
    assert out["reconciliation"]["violation_count"] == 0


# ---------------------------------------------------------------------------
# Availability semantics (None never 0, zero is valid, ...)
# ---------------------------------------------------------------------------


def test_missing_return_is_none_not_zero() -> None:
    # 'a' has a valid amount but NO return; 'b'/'c' have returns + distinct amounts.
    # AW weights are renormalized over {b,c}: b=100/150, c=50/150.
    members = [
        _m("a", return_1d=None, amount=100.0),
        _m("b", return_1d=0.03, amount=100.0),
        _m("c", return_1d=-0.01, amount=50.0),
    ]
    out = compute_member_attribution(members=members, observation=_run(members))
    assert out["reconciliation"]["violation_count"] == 0
    # 'a' has no finite return -> excluded from price/aw/breadth, listed as unavailable.
    assert [e["member_id"] for e in out["breadth"]["unavailable"]] == ["a"]
    # 'b' has a strong positive tilt (overweight amount + up move).
    tilt_ids = [e["member_id"] for e in out["capital_tilt"]["positive"]]
    assert "b" in tilt_ids


def test_negative_amount_excluded_from_direction_not_zeroed() -> None:
    members = [_m("a", return_1d=0.03, amount=-5.0)]
    obs = _run(members)
    out = compute_member_attribution(members=members, observation=obs)
    # AW universe empty -> canonical AW unavailable; both sides unavailable -> PASS.
    assert out["reconciliation"]["checks"]["direction"]["resolved"] == "both_unavailable"
    assert out["reconciliation"]["violation_count"] == 0


def test_zero_amount_valid_zero_share() -> None:
    # 'a' has amount 0 (legal) with a real return -> aw_weight is real 0.0, never None.
    members = [_m("a", return_1d=0.03, amount=0.0), _m("b", return_1d=-0.01, amount=100.0)]
    out = compute_member_attribution(members=members, observation=_run(members))
    assert out["reconciliation"]["violation_count"] == 0
    assert out["reconciliation"]["checks"]["direction"]["pass"] is True
    a_ev = next(e for e in out["capital_tilt"]["negative"] if e["member_id"] == "a")
    assert a_ev["aw_weight"] == 0.0
    # tilt_a = (aw_w 0.0 - ew_w 0.5) * 0.03 == -0.015
    assert a_ev["tilt_contribution"] == pytest.approx(-0.015, abs=RECONCILIATION_TOLERANCE)


# ---------------------------------------------------------------------------
# Determinism - member order / set iteration independent
# ---------------------------------------------------------------------------


def test_determinism_same_and_shuffled_input() -> None:
    members = [
        _m("a", return_1d=0.05, amount=30.0),
        _m("b", return_1d=-0.04, amount=20.0),
        _m("c", return_1d=0.02, amount=10.0),
        _m("d", return_1d=0.0, amount=40.0),
    ]
    obs = _run(members)
    first = compute_member_attribution(members=members, observation=obs)
    # deterministic reconstruction with a client has no persistence: rerun
    second = compute_member_attribution(members=list(reversed(members)), observation=obs)
    assert first["determinism_checksum"] == second["determinism_checksum"]
    # The stored checksum is a stable fingerprint of the content EXCLUDING itself.
    assert (
        checksum({k: v for k, v in first.items() if k != "determinism_checksum"})
        == first["determinism_checksum"]
    )


def test_determinism_float_aggregation_order_independent() -> None:
    # Non-integer amounts/returns make float aggregation order-sensitive: the
    # abs-return total and the AW-amount total MUST be summed over a member_id
    # order, never the supplier order, or the checksum would differ.
    members = [
        _m("a", return_1d=0.03125, amount=123.45),
        _m("b", return_1d=-0.0125, amount=76.3),
        _m("c", return_1d=0.0, amount=0.0),
        _m("d", return_1d=0.009375, amount=500.12),
        _m("e", return_1d=-0.00625, amount=33.33),
    ]
    obs = _run(members)
    base = compute_member_attribution(members=members, observation=obs)
    for perm in (list(reversed(members)), [members[2], members[0], members[4], members[1], members[3]]):
        other = compute_member_attribution(members=perm, observation=obs)
        assert other["determinism_checksum"] == base["determinism_checksum"]
    assert base["reconciliation"]["violation_count"] == 0


# ---------------------------------------------------------------------------
# A1-2 Large-N golden + raw-value ranking (precision lock)
# ---------------------------------------------------------------------------


def test_large_n_4096_equal_members_reconcile() -> None:
    # A1-2 golden: with N=4096 equal members each weight is 1/4096 and each
    # contribution is tiny.  Rounding each to 6 decimals before summing would
    # collapse the HHI sum to 0 while canonical raw HHI = 1/4096 ~ 2.44e-4 — a
    # guaranteed FAIL on any round-then-sum implementation.  Raw math.fsum must
    # reproduce the canonical facts exactly.
    n = 4096
    members = [_m(f"m{i:05d}", return_1d=0.001, amount=1.0) for i in range(n)]
    out = compute_member_attribution(
        members=members, observation=_run(members, scope_key="large4096")
    )
    rec = out["reconciliation"]
    assert rec["violation_count"] == 0
    for key in ("direction", "capital_tilt", "concentration_price", "concentration_amount"):
        assert rec["checks"][key]["resolved"] == "matched", key
    # Direction: AW == each member return == 0.001.
    assert out["direction"]["sum_contribution"] == pytest.approx(0.001, abs=RECONCILIATION_TOLERANCE)
    # Tilt: AW == EW -> 0.
    assert out["capital_tilt"]["sum_tilt_contribution"] == pytest.approx(0.0, abs=RECONCILIATION_TOLERANCE)
    # HHI: sum of (1/4096)^2 over 4096 members == 1/4096.
    assert out["concentration"]["price"]["sum_hhi"] == pytest.approx(1 / 4096, abs=RECONCILIATION_TOLERANCE)
    assert out["concentration"]["amount"]["sum_hhi"] == pytest.approx(1 / 4096, abs=RECONCILIATION_TOLERANCE)
    # Each member's displayed hhi contribution ~5.96e-8 rounds to 0.000000, but
    # that display must never feed the reconciliation sum.
    assert out["concentration"]["price"]["members"][0]["hhi_contribution"] == 0.0
    # Large-N determinism: the displayed reconciliation abs_diff must be
    # order-stable even though the canonical amount_share owner sums in input
    # order (residue ~1e-16 << 6-decimal display resolution).
    rev = compute_member_attribution(
        members=list(reversed(members)), observation=_run(members, scope_key="large4096")
    )
    assert rev["determinism_checksum"] == out["determinism_checksum"]


def test_near_equal_raw_contributions_ranked_by_raw_not_rounded() -> None:
    # A1-2: two members whose RAW contributions differ only below the display
    # resolution (both round to 0.000000 at 6 decimals).  Ranking must use the
    # raw values, so the higher-raw member comes first even though its id sorts
    # later; a rounded-value ranking would tie them and put the smaller id first.
    # aw_weight is 0.5 for each; raw contribution = 0.5 * return.
    members = [
        _m("z", return_1d=0.00000005, amount=1.0),  # raw 2.5e-8
        _m("a", return_1d=0.00000004, amount=1.0),  # raw 2.0e-8
    ]
    out = compute_member_attribution(
        members=members, observation=_run(members, scope_key="near_equal")
    )
    assert out["reconciliation"]["violation_count"] == 0
    ids = [e["member_id"] for e in out["direction"]["positive"]]
    assert ids == ["z", "a"]
    # Both display as 0.000000 (below resolution) but ranking still separated them.
    assert all(e["contribution"] == 0.0 for e in out["direction"]["positive"])


# ---------------------------------------------------------------------------
# Leadership - consume canonical migration verbatim
# ---------------------------------------------------------------------------


def test_leadership_expansion_ready() -> None:
    current = [
        _m("a", return_1d=2.0, amount=100.0),
        _m("b", return_1d=1.0, amount=50.0),
        _m("c", return_1d=-0.5, amount=40.0),
    ]
    prev_snap = _snapshot([("a", 1.0), ("x", 0.5)], direction=1)  # prev leaders {a,x}
    curr_snap = _snapshot([("a", 1.0), ("b", 0.4)], direction=1)  # curr leaders {a,b}
    mig = compute_leadership_migration(previous_snapshot=prev_snap, current_snapshot=curr_snap)
    assert mig.status == "ready"
    obs = _run(current)
    out = compute_member_attribution(members=current, observation=obs, leadership_migration=mig)
    assert out["leadership"]["status"] == "ready"
    assert sorted(e["member_id"] for e in out["leadership"]["retained"]) == ["a"]
    assert sorted(e["member_id"] for e in out["leadership"]["entrants"]) == ["b"]
    assert sorted(e["member_id"] for e in out["leadership"]["exits"]) == ["x"]
    assert out["reconciliation"]["checks"]["leadership"]["pass"] is True
    assert out["reconciliation"]["violation_count"] == 0


def test_leadership_not_provided_is_skipped_not_failed() -> None:
    out = _nominal_a1()
    assert out["leadership"]["status"] == "unavailable"
    assert out["reconciliation"]["checks"]["leadership"]["pass"] is None
    assert out["reconciliation"]["violation_count"] == 0


def test_leadership_empty_leader_set_reconciles_sets() -> None:
    # A1-4: both snapshots ready but the previous leader set is legitimately
    # empty.  Canonical migration status = unavailable(empty_leader_set) with
    # set facts preserved.  Attribution mirrors the metric AND still reconciles
    # retained/entrant/exit — only unavailable_snapshot skips reconciliation.
    prev_snap = _snapshot([], direction=1)  # legitimately empty previous leaders
    curr_snap = _snapshot([("a", 1.0)], direction=1)
    mig = compute_leadership_migration(previous_snapshot=prev_snap, current_snapshot=curr_snap)
    assert mig.status == "unavailable" and mig.reason == "empty_leader_set"
    assert mig.entrant_ids == ("a",) and mig.retained_count == 0
    current = [_m("a", return_1d=2.0, amount=100.0)]
    out = compute_member_attribution(
        members=current, observation=_run(current), leadership_migration=mig
    )
    assert out["leadership"]["status"] == "unavailable"
    assert out["leadership"]["reason"] == "empty_leader_set"
    assert sorted(e["member_id"] for e in out["leadership"]["entrants"]) == ["a"]
    ck = out["reconciliation"]["checks"]["leadership"]
    assert ck["resolved"] == "matched" and ck["pass"] is True
    assert out["reconciliation"]["violation_count"] == 0


def test_leadership_unavailable_snapshot_skips_transition() -> None:
    # A1-4: one side's snapshot is unavailable -> transition cannot be verified,
    # reconciliation is skipped (never a false PASS), never a violation.
    prev_snap = _snapshot([("a", 1.0)], direction=1)
    curr_snap = LeadershipSnapshot(
        trade_date=TRADE_DATE.isoformat(),
        status="unavailable",
        reason="unavailable_snapshot",
        direction=None,
        rankable_count=None,
        leader_set=(),
    )
    mig = compute_leadership_migration(previous_snapshot=prev_snap, current_snapshot=curr_snap)
    assert mig.status == "unavailable" and mig.reason == "unavailable_snapshot"
    current = [_m("a", return_1d=2.0, amount=100.0)]
    out = compute_member_attribution(
        members=current, observation=_run(current), leadership_migration=mig
    )
    ck = out["reconciliation"]["checks"]["leadership"]
    assert ck["resolved"] == "skipped" and ck["pass"] is None
    assert out["reconciliation"]["violation_count"] == 0


# ---------------------------------------------------------------------------
# Frozen-style logic validation: 4 diverse scopes all reconcile
# ---------------------------------------------------------------------------


def test_frozen_style_four_scopes_reconcile() -> None:
    scenarios = [
        [_m("a", return_1d=3.5, amount=50.0), _m("b", return_1d=-1.2, amount=30.0), _m("c", return_1d=0.0, amount=20.0)],
        [_m("sx", return_1d=-2.0, amount=8.0), _m("sy", return_1d=-1.0, amount=92.0), _m("sz", return_1d=0.5, amount=0.0)],
        [_m("p", return_1d=0.01, amount=1.0), _m("q", return_1d=2.0, amount=99.0), _m("r", return_1d=1.0, amount=0.0)],
        [_m("m1", return_1d=-0.5, amount=40.0), _m("m2", return_1d=-0.5, amount=40.0), _m("m3", return_1d=-0.5, amount=20.0)],
    ]
    for i, members in enumerate(scenarios):
        out = compute_member_attribution(members=members, observation=_run(members, scope_key=f"s{i}"))
        assert out["reconciliation"]["violation_count"] == 0, f"scope s{i} violations"


# ---------------------------------------------------------------------------
# No interpretation leakage (prohibited axis)
# ---------------------------------------------------------------------------


def test_no_interpreretation_label_leakage() -> None:
    import json

    out = _nominal_a1()
    blob = json.dumps(out, default=str, sort_keys=True)
    for forbidden in (
        "Core-led",
        "Rotating",
        "Broadening",
        "Fragmenting",
        "structure_type",
        "confidence_score",
        "attribution_score",
        "member_score",
        "importance_score",
        "core_score",
        "role_",
    ):
        assert forbidden not in blob, forbidden
