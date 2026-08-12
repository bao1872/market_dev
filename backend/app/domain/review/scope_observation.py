"""Canonical Scope Observation Core (PRD §7).

Pure, family-agnostic computation of objective Scope Observation facts from
already-prepared canonical member facts.  ``industry`` / ``concept`` (and any
future scope family) share exactly one calculation path: no ``scope_type``
branch is allowed in any calculation (PRD §7.8.2 / §7.8.3).

Scope-Family specificity (membership / metadata / peer cohort / readiness)
is a separate concern and is NOT handled here.

This module does NOT:
- query the database, resolve membership, load bars, or guess the canonical
  previous trading day (those are orchestration / data-preparation concerns);
- fall back to an earlier bar when the exact canonical T-1 is missing;
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
    data-preparation concern.  This Core only consumes the resolved facts below;
    a member whose exact T-1 is missing simply carries ``None`` / ``False`` for
    the corresponding T-1 fields and is excluded from the affected denominators.
    """

    member_id: str
    # PRICE — ``price_candidate`` = PIT ∩ valid FP ∩ close(T) available.
    price_candidate: bool
    # ``return_1d`` = close(T) / close(T-1) - 1 via exact canonical T-1.
    # ``None`` ⇔ exact T-1 close unavailable (never fall back to an earlier bar).
    return_1d: float | None
    # AMOUNT — independent universe, no T-1 requirement.
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


def _price_concentration(returns: Sequence[float]) -> dict[str, Any]:
    """abs-price-change share based raw HHI. Raw HHI is NOT cross-scope normalized."""
    abs_returns = [abs(r) for r in returns]
    total = sum(abs_returns)
    if total <= _EPSILON:
        return {"raw_hhi": None, "member_count": len(returns), "status": "zero_abs_return"}
    shares = [a / total for a in abs_returns]
    return {
        "raw_hhi": _raw_hhi(shares),
        "member_count": len(returns),
        "status": "ready",
    }


def _amount_concentration(amounts: Sequence[float]) -> dict[str, Any]:
    """amount-share based raw HHI over the independent amount universe."""
    total = sum(amounts)
    if total <= _EPSILON:
        return {"raw_hhi": None, "member_count": len(amounts), "status": "zero_amount"}
    shares = [a / total for a in amounts]
    return {
        "raw_hhi": _raw_hhi(shares),
        "member_count": len(amounts),
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
    transition key.  Membership add/remove is excluded at the boundary: an added
    member has no exact T-1 state and is therefore not in the denominator.
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


def compute_scope_observation(
    *,
    scope_type: str,
    scope_key: str,
    trade_date: date,
    pit_member_ids: Iterable[str],
    members: Iterable[MemberObservation],
) -> dict[str, Any]:
    """Compute objective Canonical Scope Observation facts (PRD §7).

    ``scope_type`` / ``scope_key`` only identify the scope; they never branch
    the calculation path.  ``pit_member_ids`` is the PIT member set (lineage /
    membership metadata); the provided ``members`` carry the canonical facts.
    """
    member_list = list(members)
    pit_ids = list(pit_member_ids)

    # PRICE universe — current value + exact canonical T-1.
    price_candidate_count = sum(1 for m in member_list if m.price_candidate)
    price_returns = [m.return_1d for m in member_list if m.return_1d is not None]
    price_valid_count = len(price_returns)
    missing_exact_t1_count = price_candidate_count - price_valid_count

    # AMOUNT universe — independent, no T-1 requirement.
    amounts = [m.amount for m in member_list if m.amount is not None]
    amount_valid_count = len(amounts)

    # Categorical axes — state denominators are axis-specific.
    trend_values = [m.trend for m in member_list if m.trend is not None]
    swing_values = [m.swing for m in member_list if m.swing is not None]
    internal_values = [m.internal for m in member_list if m.internal is not None]
    momentum_values = [m.momentum for m in member_list if m.momentum is not None]

    # Transition axes — exact T-1 -> T common-valid denominators.
    trend_transition = [
        (m.trend, m.t1_trend) for m in member_list
        if m.trend is not None and m.t1_trend is not None
    ]
    swing_transition = [
        (m.swing, m.t1_swing) for m in member_list
        if m.swing is not None and m.t1_swing is not None
    ]
    internal_transition = [
        (m.internal, m.t1_internal) for m in member_list
        if m.internal is not None and m.t1_internal is not None
    ]
    momentum_transition = [
        (m.momentum, m.t1_momentum) for m in member_list
        if m.momentum is not None and m.t1_momentum is not None
    ]

    # PARTICIPATION universe — threshold-free, no T-1 requirement.
    vol_ratios = [m.vol_ratio20 for m in member_list if m.vol_ratio20 is not None]
    amt_ratios = [m.amt_ratio20 for m in member_list if m.amt_ratio20 is not None]

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
            "pit_member_count": len(set(pit_ids)),
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
        },
        "amount": {
            "valid_count": amount_valid_count,
            "concentration": _amount_concentration(amounts),
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
