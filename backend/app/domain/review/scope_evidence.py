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
State / Breadth D1 / D3 / D5 changes are continuous Objective Evidence; Diffusion
is not an independent canonical state / primitive (PRD §7, CHANGE-011 / 012).

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

# CORE Evidence primitives: explicit path mapping into the canonical L1 payload
# (4A §3).  No JSONPath / DSL — an explicit, closed mapping.  These are the
# scope-level numeric facts L1 already computes and persists; L2 now consumes
# all of them instead of only 6.  State/Breadth ratios cover the complete
# up/neutral/down distribution; Participation covers full volume/amount p25/p50/p75.
PRIMITIVE_PATHS: dict[str, tuple[str, ...]] = {
    # -------------------------------------------------
    # PRICE — Return Level / Distribution
    # -------------------------------------------------
    "price_return_mean": ("price", "return", "mean"),
    "price_return_median": ("price", "return", "median"),
    "price_return_p25": ("price", "return", "p25"),
    "price_return_p75": ("price", "return", "p75"),

    # -------------------------------------------------
    # PRICE — Breadth
    # -------------------------------------------------
    "price_advance_ratio": ("price", "breadth", "advance_ratio"),
    "price_decline_ratio": ("price", "breadth", "decline_ratio"),
    "price_unchanged_ratio": ("price", "breadth", "unchanged_ratio"),

    # -------------------------------------------------
    # PRICE — Concentration
    # -------------------------------------------------
    "price_raw_hhi": ("price", "concentration", "raw_hhi"),
    "price_normalized_hhi": ("price", "concentration", "normalized_hhi"),

    # -------------------------------------------------
    # PRICE → Amount Concentration
    # -------------------------------------------------
    "amount_raw_hhi": ("price", "amount", "concentration", "raw_hhi"),
    "amount_normalized_hhi": ("price", "amount", "concentration", "normalized_hhi"),

    # -------------------------------------------------
    # TREND — complete State/Breadth distribution
    # -------------------------------------------------
    "trend_up_ratio": ("trend", "state", "up_ratio"),
    "trend_neutral_ratio": ("trend", "state", "neutral_ratio"),
    "trend_down_ratio": ("trend", "state", "down_ratio"),

    # -------------------------------------------------
    # STRUCTURE — Swing State/Breadth
    # -------------------------------------------------
    "structure_swing_up_ratio": ("structure", "swing", "state", "up_ratio"),
    "structure_swing_neutral_ratio": ("structure", "swing", "state", "neutral_ratio"),
    "structure_swing_down_ratio": ("structure", "swing", "state", "down_ratio"),

    # -------------------------------------------------
    # STRUCTURE — Internal State/Breadth
    # -------------------------------------------------
    "structure_internal_up_ratio": ("structure", "internal", "state", "up_ratio"),
    "structure_internal_neutral_ratio": ("structure", "internal", "state", "neutral_ratio"),
    "structure_internal_down_ratio": ("structure", "internal", "state", "down_ratio"),

    # -------------------------------------------------
    # MOMENTUM — complete State/Breadth distribution
    # -------------------------------------------------
    "momentum_expanding_ratio": ("momentum", "state", "expanding_ratio"),
    "momentum_flat_ratio": ("momentum", "state", "flat_ratio"),
    "momentum_contracting_ratio": ("momentum", "state", "contracting_ratio"),

    # -------------------------------------------------
    # PARTICIPATION — Volume Distribution
    # -------------------------------------------------
    "participation_volume_p25": ("participation", "volume", "p25"),
    "participation_volume_p50": ("participation", "volume", "p50"),
    "participation_volume_p75": ("participation", "volume", "p75"),

    # -------------------------------------------------
    # PARTICIPATION — Amount Distribution
    # -------------------------------------------------
    "participation_amount_p25": ("participation", "amount", "p25"),
    "participation_amount_p50": ("participation", "amount", "p50"),
    "participation_amount_p75": ("participation", "amount", "p75"),
}

# ---------------------------------------------------------------------------
# Transition primitives (4B §3/§4)
# ---------------------------------------------------------------------------
# L1 Transition = member exact canonical T-1 -> T categorical migration; the
# cross-scope primary expression is the transition RATIO (raw count is
# explanation/audit only, never a cross-scope primitive).  L1 uses SPARSE
# encoding: only actually-occurring non-identity migrations are stored as
# ``"<From>→<To>": {"count": ..., "ratio": ...}`` under a ``transition``
# container that also carries ``denominator`` (T & T-1 common valid members).
#
# Decoding rules (4B §2.4, frozen):
#   A. denominator > 0 and transition key ABSENT  -> ratio = 0.0 (zero members
#      made this migration; NOT "no data").
#   B. denominator <= 0 or denominator unavailable -> ratio = None (unavailable).
#
# Stable (identity) migrations Up→Up / Neutral→Neutral / Down→Down (and
# Momentum Expanding→Expanding / Flat→Flat / Contracting→Contracting) are NOT
# transition events and are intentionally absent from this spec.  Transition
# count is never a primitive.
TRANSITION_PRIMITIVE_SPECS: dict[
    str,
    tuple[tuple[str, ...], str],
] = {
    # TREND (Up / Neutral / Down)
    "trend_transition_up_to_neutral_ratio": (("trend", "transition"), "Up→Neutral"),
    "trend_transition_up_to_down_ratio": (("trend", "transition"), "Up→Down"),
    "trend_transition_neutral_to_up_ratio": (("trend", "transition"), "Neutral→Up"),
    "trend_transition_neutral_to_down_ratio": (("trend", "transition"), "Neutral→Down"),
    "trend_transition_down_to_up_ratio": (("trend", "transition"), "Down→Up"),
    "trend_transition_down_to_neutral_ratio": (("trend", "transition"), "Down→Neutral"),

    # STRUCTURE — SWING (Up / Neutral / Down)
    "structure_swing_transition_up_to_neutral_ratio": (("structure", "swing", "transition"), "Up→Neutral"),
    "structure_swing_transition_up_to_down_ratio": (("structure", "swing", "transition"), "Up→Down"),
    "structure_swing_transition_neutral_to_up_ratio": (("structure", "swing", "transition"), "Neutral→Up"),
    "structure_swing_transition_neutral_to_down_ratio": (("structure", "swing", "transition"), "Neutral→Down"),
    "structure_swing_transition_down_to_up_ratio": (("structure", "swing", "transition"), "Down→Up"),
    "structure_swing_transition_down_to_neutral_ratio": (("structure", "swing", "transition"), "Down→Neutral"),

    # STRUCTURE — INTERNAL (Up / Neutral / Down)
    "structure_internal_transition_up_to_neutral_ratio": (("structure", "internal", "transition"), "Up→Neutral"),
    "structure_internal_transition_up_to_down_ratio": (("structure", "internal", "transition"), "Up→Down"),
    "structure_internal_transition_neutral_to_up_ratio": (("structure", "internal", "transition"), "Neutral→Up"),
    "structure_internal_transition_neutral_to_down_ratio": (("structure", "internal", "transition"), "Neutral→Down"),
    "structure_internal_transition_down_to_up_ratio": (("structure", "internal", "transition"), "Down→Up"),
    "structure_internal_transition_down_to_neutral_ratio": (("structure", "internal", "transition"), "Down→Neutral"),

    # MOMENTUM (Expanding / Flat / Contracting)
    "momentum_transition_expanding_to_flat_ratio": (("momentum", "transition"), "Expanding→Flat"),
    "momentum_transition_expanding_to_contracting_ratio": (("momentum", "transition"), "Expanding→Contracting"),
    "momentum_transition_flat_to_expanding_ratio": (("momentum", "transition"), "Flat→Expanding"),
    "momentum_transition_flat_to_contracting_ratio": (("momentum", "transition"), "Flat→Contracting"),
    "momentum_transition_contracting_to_expanding_ratio": (("momentum", "transition"), "Contracting→Expanding"),
    "momentum_transition_contracting_to_flat_ratio": (("momentum", "transition"), "Contracting→Flat"),
}

# Deterministic merge: 29 CORE scalar facts + 24 transition ratio facts = 53.
# This is the full set of internal numeric evidence extraction facts (NOT a
# "53 product indicators" UI structure, NOT a score).
PRIMITIVE_NAMES: tuple[str, ...] = (
    *PRIMITIVE_PATHS.keys(),
    *TRANSITION_PRIMITIVE_SPECS.keys(),
)

# raw HHI is not normalized by member count -> not cross-scope comparable
# (PRD §7.9.3, 4A §4).  Both price and amount raw_hhi share this rule; only
# CURRENT/D1/D3/D5/HISTORICAL_POSITION are allowed; PEER_POSITION must be
# disabled.  normalized_hhi is cross-scope comparable and is NOT listed here.
# Transition ratios are cross-scope comparable ratios (not raw counts) and are
# intentionally NOT listed here.
PEER_DISABLED_REASON_BY_PRIMITIVE: dict[str, str] = {
    "price_raw_hhi": "raw_hhi_not_cross_scope_comparable",
    "amount_raw_hhi": "raw_hhi_not_cross_scope_comparable",
}


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


def _extract_transition_ratio(
    observation_payload: dict[str, Any],
    container_path: tuple[str, ...],
    transition_key: str,
) -> float | None:
    """Decode a transition ratio from L1's sparse transition container (4B §5).

    Rules (frozen):
      - walk ``container_path``; missing container -> None (unavailable);
      - ``denominator`` absent / not finite / <= 0 -> None (unavailable);
      - transition_key absent but denominator > 0 -> 0.0 (zero members migrated);
      - transition_key present but malformed / ratio absent / non-finite -> None;
      - ratio outside [0, 1] -> None.

    L2 never recomputes ratio from count/denominator; it only interprets L1's
    canonical sparse encoding.
    """
    node: Any = observation_payload
    for key in container_path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    if not isinstance(node, dict):
        return None

    denominator = _finite_number(node.get("denominator"))
    if denominator is None or denominator <= 0:
        return None

    item = node.get(transition_key)
    if item is None:
        # Sparse encoding: legal transition with valid denominator but no stored
        # key means zero members made this migration.
        return 0.0
    if not isinstance(item, dict):
        return None

    ratio = _finite_number(item.get("ratio"))
    if ratio is None:
        return None
    if ratio < 0.0 or ratio > 1.0:
        return None
    return ratio


def extract_primitive(observation_payload: dict[str, Any], primitive: str) -> float | None:
    """Extract one primitive (CORE scalar or Transition ratio) from a canonical payload.

    Returns the finite numeric value, or None when the path is missing /
    non-numeric / boolean / non-finite / denominator invalid (-> unavailable).
    Sparse transition keys absent with a valid denominator decode to 0.0, not None.
    """
    scalar_path = PRIMITIVE_PATHS.get(primitive)
    if scalar_path is not None:
        node: Any = observation_payload
        for key in scalar_path:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return _finite_number(node)

    transition_spec = TRANSITION_PRIMITIVE_SPECS.get(primitive)
    if transition_spec is not None:
        container_path, transition_key = transition_spec
        return _extract_transition_ratio(
            observation_payload, container_path, transition_key
        )

    raise KeyError(f"unknown evidence primitive: {primitive}")


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

    Status precedence (Round 2A correction):
      A. current value is None -> ``unavailable`` (percentile=None), regardless of
         sample size; sample_count / history_start_date / history_end_date are kept;
      B. current available but sample_count < 60 -> ``insufficient_history``;
      C. current available and sample_count >= 60 -> ``ready``.
    """
    count = len(sample_values)
    if value is None:
        return {
            "status": "unavailable",
            "percentile": None,
            "sample_count": count,
            "history_start_date": history_start_date.isoformat() if history_start_date else None,
            "history_end_date": history_end_date.isoformat() if history_end_date else None,
        }
    if count < HISTORICAL_MIN_SAMPLE:
        return {
            "status": "insufficient_history",
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
