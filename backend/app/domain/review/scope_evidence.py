"""Objective Evidence Engine (Round 2A) — pure calculation layer.

L2-A derives Objective Evidence from the canonical L1 facts
(``review_scope_observation_facts``).  It answers:

    "今天这些客观事实，和自己的过去相比、和今天同类 Scope 相比，
     处在什么位置、发生了什么变化？"

This module is PURE: it never touches the database, never reads legacy
``market_review_scope_snapshots`` / p/q/u/c/v payloads, never resolves trading
dates, and never mutates L1 facts.  It only owns the numeric/semantic-neutral
evidence math (current / delta / percentile / context status).

Explicit exclusions (prompt §1 / §29): no Opportunity / Risk / Strong / Weak /
Candidate / Filter / Discovery / Ranking / Score / Grade / Recommendation.
Diffusion remains PROVISIONAL; only raw breadth D1/D3/D5 deltas are exposed here
(prompt §18).

Percentile primitive (prompt §6 / §7): this module does NOT import or reuse
``scope_observation._percentile`` (a quantile-value calculator for P25/P50/P75),
and does NOT import legacy ``_normalize_component`` / P/Q/U/C/V score / weight /
direction.  ``percentile_rank`` is a new, neutral, pure helper: the current value
ranked inside a sample distribution, 0..100, without direction/weight/negative
inversion (extracting only the pure math semantic of the repo's existing
cross-sectional rank convention).
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any

# PRD §7.6: fewer than 60 valid historical samples -> insufficient_history.
HISTORICAL_MIN_SAMPLE = 60

# Phase-1 Evidence primitives: explicit path mapping into the canonical payload
# (prompt §10 / §14).  No JSONPath / DSL — an explicit, closed mapping.
PRIMITIVE_PATHS: dict[str, tuple[str, ...]] = {
    "price_return_mean": ("price", "return", "mean"),
    "price_advance_ratio": ("price", "breadth", "advance_ratio"),
    "trend_up_ratio": ("trend", "state", "up_ratio"),
    "momentum_expanding_ratio": ("momentum", "state", "expanding_ratio"),
    "participation_volume_p50": ("participation", "volume", "p50"),
    "price_raw_hhi": ("price", "concentration", "raw_hhi"),
}

# Phase-1 primitive order for deterministic iteration / output.
PRIMITIVE_NAMES: tuple[str, ...] = tuple(PRIMITIVE_PATHS)

# raw HHI is not normalized by member count -> not cross-scope comparable
# (PRD §7.9.3, prompt §15).  Only CURRENT/D1/D3/D5/HISTORICAL_POSITION are
# allowed; PEER_POSITION must be disabled.
RAW_HHI_PEER_DISABLED_REASON = "raw_hhi_not_cross_scope_comparable"


def _finite_number(value: Any) -> float | None:
    """Return a finite float for int/float, else None.

    Booleans are explicitly rejected: ``bool`` must never be treated as numeric
    evidence (prompt §11).  None / NaN / +-inf -> None (never coerced to 0).
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def percentile_rank(value: Any, samples: list[Any]) -> float | None:
    """Neutral percentile rank (0..100) of ``value`` within ``samples``.

    Rules (prompt §7):
      - filters None / NaN / inf from ``samples`` and ``value``;
      - empty (after filter) -> None (unavailable);
      - deterministic tie behavior (values equal to ``value`` all count);
      - output 0..100;
      - no direction, no weight, no negative inversion, no score normalization.

    Only the pure math semantic of the repo's cross-sectional rank convention is
    extracted: ``below_or_equal / n * 100``, clamped to [0, 100].
    """
    current = _finite_number(value)
    finite = [float(s) for s in samples if _finite_number(s) is not None]
    if current is None or not finite:
        return None
    below_or_equal = sum(1 for s in finite if s <= current)
    return _clamp_0_100(below_or_equal / len(finite) * 100.0)


def _clamp_0_100(value: float) -> float:
    return min(100.0, max(0.0, value))


def extract_primitive(observation_payload: dict[str, Any], primitive: str) -> float | None:
    """Extract one Phase-1 primitive from a canonical observation payload.

    Returns the finite numeric value, or None when the path is missing /
    non-numeric / boolean / non-finite (-> unavailable, never 0).
    """
    if primitive not in PRIMITIVE_PATHS:
        raise KeyError(f"unknown evidence primitive: {primitive}")
    node: Any = observation_payload
    for key in PRIMITIVE_PATHS[primitive]:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return _finite_number(node)


def compute_delta(current: float | None, reference: float | None) -> float | None:
    """``current - reference`` in the primitive's native unit (ratio stays 0..1).

    Returns None when either side is unavailable.  No domain re-interpretation:
    no "+0.07 = strong", no improving/deteriorating label (Round 2B).
    """
    if current is None or reference is None:
        return None
    return current - reference


# ---------------------------------------------------------------------------
# Context status builders (pure; called by the thin service after it assembles
# reference / historical / peer inputs).  Each context carries its OWN status.
# ---------------------------------------------------------------------------


def build_current_context(value: float | None) -> dict[str, Any]:
    if value is None:
        return {"status": "unavailable", "value": None}
    return {"status": "ready", "value": value}


def build_delta_context(
    current: float | None,
    reference_value: float | None,
    reference_date: date | None,
) -> dict[str, Any]:
    if current is None or reference_value is None or reference_date is None:
        return {
            "status": "unavailable",
            "reference_date": reference_date.isoformat() if reference_date else None,
            "reference_value": reference_value,
            "delta": None,
        }
    return {
        "status": "ready",
        "reference_date": reference_date.isoformat(),
        "reference_value": reference_value,
        "delta": compute_delta(current, reference_value),
    }


def build_historical_context(
    value: float | None,
    sample_values: list[float],
    history_start_date: date | None,
    history_end_date: date | None,
) -> dict[str, Any]:
    """Historical position: current value ranked inside its own past samples.

    ``sample_values`` are the already-extracted finite historical values
    (trade_date < T; current is excluded upstream).  The 60-sample PRD gate is
    enforced here; a short sample yields ``insufficient_history`` (never replaced
    by peer percentile).
    """
    count = len(sample_values)
    if value is None or count < HISTORICAL_MIN_SAMPLE:
        return {
            "status": "insufficient_history" if count < HISTORICAL_MIN_SAMPLE else "unavailable",
            "percentile": None,
            "sample_count": count,
            "history_start_date": history_start_date.isoformat() if history_start_date else None,
            "history_end_date": history_end_date.isoformat() if history_end_date else None,
        }
    pct = percentile_rank(value, sample_values)
    return {
        "status": "ready",
        "percentile": pct,
        "sample_count": count,
        "history_start_date": history_start_date.isoformat() if history_start_date else None,
        "history_end_date": history_end_date.isoformat() if history_end_date else None,
    }


def build_peer_context(
    value: float | None,
    peer_values: list[float],
    disabled_reason: str | None = None,
) -> dict[str, Any]:
    """Peer position: current value ranked inside same-day same-family cohort.

    ``peer_values`` include the current scope's value when available (prompt §16).
    ``disabled_reason`` (raw HHI) forces ``unavailable`` and suppresses the rank.
    """
    if disabled_reason is not None:
        return {
            "status": "unavailable",
            "percentile": None,
            "peer_count": len(peer_values),
            "reason": disabled_reason,
        }
    if value is None or not peer_values:
        return {"status": "unavailable", "percentile": None, "peer_count": len(peer_values)}
    pct = percentile_rank(value, peer_values)
    return {"status": "ready", "percentile": pct, "peer_count": len(peer_values)}
