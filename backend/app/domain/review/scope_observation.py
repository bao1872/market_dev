"""Canonical Scope Observation Core (PRD §7).

Pure, family-agnostic computation of objective Scope Observation facts from
already-prepared canonical member facts.  ``industry`` / ``concept`` (and any
future scope family) share exactly one calculation path: no ``scope_type``
branch is allowed in any calculation (PRD §7.8.2 / §7.8.3).

Scope-Family specificity (membership / metadata / peer cohort / readiness)
is a separate concern and is NOT handled here.

PIT enforcement
---------------
- Every current fact is consumed only from members whose ``member_id`` belongs
  to ``pit_member_ids`` (PIT(T)).  A member outside PIT(T) is rejected with a
  ``ValueError`` (fail-fast), never silently mixed in.
- A duplicate ``member_id`` in ``members`` is rejected with a ``ValueError``,
  otherwise denominators / HHI / breadth would be double counted.
- Transitions additionally require membership in BOTH PIT(T) and PIT(T-1)
  (``pit_member_ids_t1``).  T-1 categorical-state existence is never used as a
  proxy for previous Scope membership.  Price return does NOT require PIT(T-1):
  it only needs PIT(T) + a price candidate(T) + an exact T-1 bar return.

Exact-T1 boundary
-----------------
- The Core does NOT query dates / bars.  ``return_1d`` and ``t1_*`` states are
  already-prepared exact-T1 facts; their provenance is owned by the Round 1B
  data-preparation owner.  The Core never falls back to an earlier bar.

This module does NOT:
- query the database, resolve membership, load bars, or guess the canonical
  previous trading day;
- reuse legacy P/Q/U/C/V scores, ``_normalize_component``, historyPercentile120d,
  crossSectionPercentile, or any 0-100 score.

Every ratio / distribution fact carries an explicit denominator / valid_count,
and categorical states (neutral / flat) are valid, never invalid.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.domain.first_pyramid_semantics import Direction, MomentumDirection

_EPSILON = 1e-12

# Categorical state label mapping for Transition output (single canonical path).
_STATE_LABELS: dict[Any, str] = {
    Direction.UP: "Up",
    Direction.SIDEWAYS: "Neutral",
    Direction.DOWN: "Down",
    MomentumDirection.EXPANDING: "Expanding",
    MomentumDirection.FLAT: "Flat",
    MomentumDirection.CONTRACTING: "Contracting",
}


@dataclass(frozen=True)
class MemberObservation:
    """One member's already-prepared canonical facts for the target trade date.

    The exact canonical T-1 resolution (which bar is T-1) is an orchestration /
    data-preparation concern owned by the Round 1B data-preparation layer.  This
    Core only consumes the resolved facts below; a member whose exact T-1 is
    missing simply carries ``None`` / ``False`` for the corresponding T-1 fields
    and is excluded from the affected denominators.  The Core never falls back.

    ``price_candidate`` = PIT(T) ∩ valid FP ∩ close(T) available.  A member with
    ``price_candidate=False`` is excluded from the price universe even if a
    ``return_1d`` happens to be present (it must not enter price denominator).

    Numeric validity: ``return_1d`` / ``amount`` / ``vol_ratio20`` /
    ``amt_ratio20`` must be finite (NaN / ±inf are unavailable).  ``amount``
    must additionally be non-negative; zero amount is valid.
    """

    member_id: str
    # PRICE — candidate(T) flag.
    price_candidate: bool
    # ``return_1d`` = close(T) / close(T-1) - 1 via exact canonical T-1.
    # ``None``/non-finite ⇔ exact T-1 unavailable (never fall back).
    return_1d: float | None
    # AMOUNT — independent universe, no T-1 requirement.  Must be finite & >= 0.
    amount: float | None
    # Current categorical states (canonical, already normalized at the boundary).
    trend: Direction | None
    swing: Direction | None
    internal: Direction | None
    momentum: MomentumDirection | None
    # Exact canonical T-1 categorical states (None = exact T-1 missing).
    t1_trend: Direction | None = None
    t1_swing: Direction | None = None
    t1_internal: Direction | None = None
    t1_momentum: MomentumDirection | None = None
    # PARTICIPATION — threshold-free distribution descriptors.
    vol_ratio20: float | None = None
    amt_ratio20: float | None = None


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _percentile(sorted_values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation percentile of an ascending sorted sequence (0 <= q <= 1)."""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    position = q * (n - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return sorted_values[lower]
    frac = position - lower
    return sorted_values[lower] * (1.0 - frac) + sorted_values[upper] * frac


def _finite_or_none(value: float | None) -> float | None:
    """Return ``value`` if it is a finite number, else ``None`` (unavailable)."""
    if value is None or not math.isfinite(value):
        return None
    return value


def _return_distribution(returns: Sequence[float]) -> dict[str, Any]:
    """Return Level + Return Distribution over the price-valid universe.

    ``mean`` and ``median`` are distinct facts; ``median`` and ``p50`` are the
    same fact, so only ``median`` is exposed (PRD §7.2).  p25/p75 (and p10/p90)
    describe the same distribution object, not separate dimensions.
    """
    if not returns:
        return {
            "mean": None,
            "median": None,
            "p25": None,
            "p75": None,
            "p10": None,
            "p90": None,
            "valid_count": 0,
        }
    mean = sum(returns) / len(returns)
    ordered = sorted(returns)
    return {
        "mean": mean,
        "median": _percentile(ordered, 0.5),
        "p25": _percentile(ordered, 0.25),
        "p75": _percentile(ordered, 0.75),
        "p10": _percentile(ordered, 0.10),
        "p90": _percentile(ordered, 0.90),
        "valid_count": len(returns),
    }


def _price_breadth(returns: Sequence[float], denominator: int) -> dict[str, Any]:
    """Threshold-free price breadth. advance+decline+unchanged == denominator."""
    advance = sum(1 for r in returns if r > 0)
    decline = sum(1 for r in returns if r < 0)
    unchanged = sum(1 for r in returns if r == 0)
    return {
        "advance_count": advance,
        "decline_count": decline,
        "unchanged_count": unchanged,
        "advance_ratio": _safe_ratio(advance, denominator),
        "decline_ratio": _safe_ratio(decline, denominator),
        "unchanged_ratio": _safe_ratio(unchanged, denominator),
        "denominator": denominator,
    }


def _raw_hhi(shares: Sequence[float]) -> float:
    return sum(share * share for share in shares)


def _normalized_hhi(
    raw_hhi: float | None,
    member_count: int,
) -> float | None:
    """Member-count-normalized HHI (ACCEPTED CONTRACT, PRD §7.2).

    ``normalized_hhi = (raw_hhi - 1/N) / (1 - 1/N)``, N = member_count > 1.

    Equal distribution -> 0; single-member-dominant -> 1; removes the mechanical
    lower bound that raw HHI imposes on a larger N.  Boundaries (frozen):
    - ``raw_hhi is None`` -> None (unavailable upstream);
    - ``member_count <= 1`` -> None (no internal concentration space, denominator 0);
    - ``1 - 1/N <= _EPSILON`` -> None (numerical degenerate floor);
    - ``raw_hhi`` out of [0, 1] after extraction -> ``ValueError`` (never silent).
    Only floating-point rounding near the endpoints is clamped to [0, 1]; a
    genuinely out-of-range value is a real error and must surface.
    """
    if raw_hhi is None:
        return None

    if member_count <= 1:
        return None

    floor = 1.0 / member_count
    denominator = 1.0 - floor

    if denominator <= _EPSILON:
        return None

    value = (raw_hhi - floor) / denominator

    # Only float-rounding near endpoints; must NOT mask an algorithmic error.
    if value < 0.0 and value >= -_EPSILON:
        value = 0.0
    elif value > 1.0 and value <= 1.0 + _EPSILON:
        value = 1.0

    if value < 0.0 or value > 1.0:
        raise ValueError(
            f"normalized HHI out of range: "
            f"raw_hhi={raw_hhi}, member_count={member_count}, value={value}"
        )

    return value


def _price_concentration(returns: Sequence[float]) -> dict[str, Any]:
    """abs-price-change share based raw + normalized HHI over the price universe.

    A zero-return member is still a valid price-concentration universe member, so
    ``member_count = len(returns)`` (all price-valid members), NOT the
    positive-return count.
    """
    abs_returns = [abs(r) for r in returns]
    member_count = len(returns)
    total = sum(abs_returns)

    if total <= _EPSILON:
        return {
            "raw_hhi": None,
            "normalized_hhi": None,
            "member_count": member_count,
            "status": "zero_abs_return",
        }

    shares = [value / total for value in abs_returns]
    raw_hhi = _raw_hhi(shares)

    if member_count <= 1:
        return {
            "raw_hhi": raw_hhi,
            "normalized_hhi": None,
            "member_count": member_count,
            "status": "insufficient_member_count",
        }

    return {
        "raw_hhi": raw_hhi,
        "normalized_hhi": _normalized_hhi(raw_hhi, member_count),
        "member_count": member_count,
        "status": "ready",
    }


@dataclass(frozen=True)
class MemberAmountContribution:
    """Member-level canonical amount contribution evidence (L1 fact, PRD §7.2).

    ``amount_share`` is scope-relative (member amount / scope valid amount total);
    it is NOT an instrument-level fact and is intentionally NOT stored on
    ``ReviewMemberFact``.  The complete member vector is not persisted into the
    scope observation payload (physical persistence = IMPLEMENTATION DESIGN REQUIRED).
    """

    member_id: str
    amount: float
    amount_share: float | None


@dataclass(frozen=True)
class AmountContributionFacts:
    """Scope aggregate of member amount contributions (single canonical owner)."""

    valid_count: int
    total_amount: float
    members: tuple[MemberAmountContribution, ...]


def compute_member_amount_contributions(
    members: Sequence[MemberObservation],
) -> AmountContributionFacts:
    """Single canonical owner of L1 member-level amount contribution (PRD §7.2).

    Rules:
    - amount None / NaN / inf / negative -> unavailable, excluded;
    - amount == 0 is a legal member (contributes 0 share when total > 0);
    - total_amount > 0 -> every valid member's ``amount_share`` sums ~= 1;
    - total_amount == 0 -> every valid member's ``amount_share`` is None;
    - no ranking / TopN / strong-weak; no DB write; no legacy attribution.
    """
    valid: list[tuple[str, float]] = []

    for member in members:
        amount = _finite_or_none(member.amount)
        if amount is None or amount < 0.0:
            continue
        valid.append((member.member_id, amount))

    total_amount = sum(amount for _, amount in valid)

    if total_amount <= _EPSILON:
        contributions = tuple(
            MemberAmountContribution(
                member_id=member_id,
                amount=amount,
                amount_share=None,
            )
            for member_id, amount in valid
        )
    else:
        contributions = tuple(
            MemberAmountContribution(
                member_id=member_id,
                amount=amount,
                amount_share=amount / total_amount,
            )
            for member_id, amount in valid
        )

    return AmountContributionFacts(
        valid_count=len(valid),
        total_amount=total_amount,
        members=contributions,
    )


def _amount_concentration(
    contribution_facts: AmountContributionFacts,
) -> dict[str, Any]:
    """amount-share based raw + normalized HHI reusing the single canonical shares.

    Must NOT recompute shares from raw amounts: ``amount_share`` and the amount HHI
    come from the same ``compute_member_amount_contributions`` owner (no second
    share formula).
    """
    member_count = contribution_facts.valid_count

    shares = [
        item.amount_share
        for item in contribution_facts.members
        if item.amount_share is not None
    ]

    if contribution_facts.total_amount <= _EPSILON:
        return {
            "raw_hhi": None,
            "normalized_hhi": None,
            "member_count": member_count,
            "status": "zero_amount",
        }

    raw_hhi = _raw_hhi(shares)

    if member_count <= 1:
        return {
            "raw_hhi": raw_hhi,
            "normalized_hhi": None,
            "member_count": member_count,
            "status": "insufficient_member_count",
        }

    return {
        "raw_hhi": raw_hhi,
        "normalized_hhi": _normalized_hhi(raw_hhi, member_count),
        "member_count": member_count,
        "status": "ready",
    }


def _categorical_state_distribution(values: Sequence[Any], labels: dict[Any, str]) -> dict[str, Any]:
    """State + Breadth for a categorical axis. neutral/flat are valid states."""
    counts = dict.fromkeys(labels.values(), 0)
    for value in values:
        label = labels.get(value)
        if label is not None:
            counts[label] += 1
    denominator = len(values)
    return {
        **{f"{label.lower()}_count": count for label, count in counts.items()},
        **{f"{label.lower()}_ratio": _safe_ratio(count, denominator) for label, count in counts.items()},
        "denominator": denominator,
    }


def _transition_distribution(
    current: Sequence[Any],
    previous: Sequence[Any],
    labels: dict[Any, str],
) -> dict[str, Any]:
    """exact T-1 -> T state migration counts/ratios over the common-valid denominator.

    A member that is stable (same state) is inside the denominator but yields no
    transition key.  Membership add/remove is handled at the boundary via
    ``pit_member_ids_t1``: only members in PIT(T) ∩ PIT(T-1) are passed in here.
    """
    denominator = len(current)
    transitions: dict[tuple[str, str], int] = {}
    for cur, prev in zip(current, previous, strict=True):
        cur_label = labels.get(cur)
        prev_label = labels.get(prev)
        if cur_label is None or prev_label is None or cur_label == prev_label:
            continue
        key = (prev_label, cur_label)
        transitions[key] = transitions.get(key, 0) + 1
    out: dict[str, Any] = {}
    for (prev_label, cur_label), count in transitions.items():
        out[f"{prev_label}→{cur_label}"] = {
            "count": count,
            "ratio": _safe_ratio(count, denominator),
        }
    out["denominator"] = denominator
    return out


def _participation_distribution(values: Sequence[float]) -> dict[str, Any]:
    """Threshold-free distribution descriptors (P25/P50/P75) of a participation ratio."""
    if not values:
        return {"p25": None, "p50": None, "p75": None, "valid_count": 0}
    ordered = sorted(values)
    return {
        "p25": _percentile(ordered, 0.25),
        "p50": _percentile(ordered, 0.50),
        "p75": _percentile(ordered, 0.75),
        "valid_count": len(values),
    }


def _reject_if_invalid_members(
    member_list: list[MemberObservation],
    pit_set: set[str],
) -> None:
    """Boundary validation: every member belongs to PIT(T), no duplicates."""
    seen: set[str] = set()
    for member in member_list:
        if member.member_id in seen:
            raise ValueError(f"duplicate member_id in members: {member.member_id}")
        seen.add(member.member_id)
        if member.member_id not in pit_set:
            raise ValueError(
                f"member {member.member_id} is not in the PIT(T) member set"
            )


def compute_scope_observation(
    *,
    scope_type: str,
    scope_key: str,
    trade_date: date,
    pit_member_ids: Iterable[str],
    pit_member_ids_t1: Iterable[str] | None = None,
    members: Iterable[MemberObservation],
) -> dict[str, Any]:
    """Compute objective Canonical Scope Observation facts (PRD §7).

    ``scope_type`` / ``scope_key`` only identify the scope; they never branch
    the calculation path.  ``pit_member_ids`` is PIT(T); ``pit_member_ids_t1``
    is the previous PIT member set (used only for Transition).  The provided
    ``members`` carry the current canonical facts and must all belong to PIT(T).
    """
    member_list = list(members)
    pit_set = set(pit_member_ids)
    t1_set = set(pit_member_ids_t1) if pit_member_ids_t1 is not None else set()
    _reject_if_invalid_members(member_list, pit_set)

    # PRICE universe — PIT(T) ∩ price candidate(T) ∩ finite exact-T1 return.
    price_candidate_count = sum(1 for m in member_list if m.price_candidate)
    price_returns = [
        finite
        for m in member_list
        if m.price_candidate and (finite := _finite_or_none(m.return_1d)) is not None
    ]
    price_valid_count = len(price_returns)
    # candidate_count >= valid_count always; difference is never negative.
    missing_exact_t1_count = price_candidate_count - price_valid_count

    # AMOUNT universe — single canonical owner of member-level amount contribution.
    # amount_share AND amount HHI both derive from this owner (no second share formula).
    amount_contribution = compute_member_amount_contributions(member_list)

    # Categorical axes — state denominators are axis-specific (no T-1 needed).
    trend_values = [m.trend for m in member_list if m.trend is not None]
    swing_values = [m.swing for m in member_list if m.swing is not None]
    internal_values = [m.internal for m in member_list if m.internal is not None]
    momentum_values = [m.momentum for m in member_list if m.momentum is not None]

    # Transition axes — PIT(T) ∩ PIT(T-1) ∩ valid state(T) ∩ valid state(T-1).
    trend_transition = [
        (m.trend, m.t1_trend) for m in member_list
        if m.member_id in t1_set and m.trend is not None and m.t1_trend is not None
    ]
    swing_transition = [
        (m.swing, m.t1_swing) for m in member_list
        if m.member_id in t1_set and m.swing is not None and m.t1_swing is not None
    ]
    internal_transition = [
        (m.internal, m.t1_internal) for m in member_list
        if m.member_id in t1_set and m.internal is not None and m.t1_internal is not None
    ]
    momentum_transition = [
        (m.momentum, m.t1_momentum) for m in member_list
        if m.member_id in t1_set and m.momentum is not None and m.t1_momentum is not None
    ]

    # PARTICIPATION universe — threshold-free, finite only, no T-1 requirement.
    vol_ratios = [
        finite for m in member_list
        if (finite := _finite_or_none(m.vol_ratio20)) is not None
    ]
    amt_ratios = [
        finite for m in member_list
        if (finite := _finite_or_none(m.amt_ratio20)) is not None
    ]

    direction_labels = {
        Direction.UP: _STATE_LABELS[Direction.UP],
        Direction.SIDEWAYS: _STATE_LABELS[Direction.SIDEWAYS],
        Direction.DOWN: _STATE_LABELS[Direction.DOWN],
    }
    momentum_labels = {
        MomentumDirection.EXPANDING: _STATE_LABELS[MomentumDirection.EXPANDING],
        MomentumDirection.FLAT: _STATE_LABELS[MomentumDirection.FLAT],
        MomentumDirection.CONTRACTING: _STATE_LABELS[MomentumDirection.CONTRACTING],
    }

    return {
        "scope": {
            "scope_type": scope_type,
            "scope_key": scope_key,
            "trade_date": trade_date.isoformat(),
            "pit_member_count": len(pit_set),
            "pit_member_count_t1": len(t1_set),
            "provided_member_count": len(member_list),
        },
        "price": {
            "candidate_count": price_candidate_count,
            "valid_count": price_valid_count,
            "missing_exact_t1_count": missing_exact_t1_count,
            "return": _return_distribution(price_returns),
            "breadth": _price_breadth(price_returns, price_valid_count),
            "concentration": _price_concentration(price_returns),
            "signed_contribution": {"status": "prd_clarification_required"},
            "amount": {
                "valid_count": amount_contribution.valid_count,
                "total_amount": amount_contribution.total_amount,
                "concentration": _amount_concentration(amount_contribution),
            },
        },
        "trend": {
            "state": _categorical_state_distribution(trend_values, direction_labels),
            "transition": _transition_distribution(
                [c for c, _ in trend_transition],
                [p for _, p in trend_transition],
                direction_labels,
            ),
        },
        "structure": {
            "swing": {
                "state": _categorical_state_distribution(swing_values, direction_labels),
                "transition": _transition_distribution(
                    [c for c, _ in swing_transition],
                    [p for _, p in swing_transition],
                    direction_labels,
                ),
            },
            "internal": {
                "state": _categorical_state_distribution(internal_values, direction_labels),
                "transition": _transition_distribution(
                    [c for c, _ in internal_transition],
                    [p for _, p in internal_transition],
                    direction_labels,
                ),
            },
        },
        "momentum": {
            "state": _categorical_state_distribution(momentum_values, momentum_labels),
            "transition": _transition_distribution(
                [c for c, _ in momentum_transition],
                [p for _, p in momentum_transition],
                momentum_labels,
            ),
        },
        "participation": {
            "volume": _participation_distribution(vol_ratios),
            "amount": _participation_distribution(amt_ratios),
        },
        "chip": {"status": "unavailable"},
    }
