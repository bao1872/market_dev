"""Analysis B — Historical Position Foundation (PRD §7.9, first layer).

Position = the current Scope Fact's historical percentile:

    Position(T) = percentile_rank( value_at_T, valid pre-T baseline )

Frozen contract (PRD §7.9 + task spec):

- historical baseline = ONLY valid primitive observations **strictly before T**;
- historical window = the latest 120 trading **observations** before T (a
  candidate window — we do NOT reach further back to fill 120 valid values;
  this is NOT 120 calendar days);
- minimum valid history = 60 observations; ``valid_count < 60`` -> unavailable;
- T itself is NEVER part of the baseline denominator;
- T+1 / any future value is NEVER consulted;
- missing / None / NaN / inf are filtered (baseline) or unavailable (current),
  never coerced to 0 and never a "latest" fallback;
- the percentile SSOT is ``scope_evidence.percentile_rank``
  (``below_or_equal / n * 100`` clamped to 0..100) — no second percentile
  implementation is introduced here.

Ownership boundary
------------------
Pure domain layer.  This module NEVER touches the database / AsyncSession /
persistence / reconstruction membership / API / orchestrator.  It consumes an
ordered historical canonical Scope Observation series (as produced by
``review_historical_scope_reconstruction_service``) and returns objective
Position facts.  Position is an objective fact — never high / low / strong /
weak / opportunity / risk / score / phase.

Primitive source
----------------
No payload paths are hard-coded here.  The 11 historical-ready primitives are
consumed exclusively through the shared ``OBSERVATION_PRIMITIVES`` registry
(the same single source of truth for path + extraction used by Analysis A / C).
``momentum.bb_position`` / ``momentum.bb_width`` are CURRENT-ONLY and are never
eligible for a historical Position.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from typing import Any

from app.domain.review.observation_primitives import (
    ObservationPrimitiveSpec,
    get_primitive,
)
from app.domain.review.scope_evidence import percentile_rank

# PRD §7.9 frozen Position contract.
POSITION_WINDOW_SIZE = 120
POSITION_MINIMUM_VALID_HISTORY = 60

# The 11 historical-ready primitives, in fixed order.  These are exactly the
# primitives whose historical values are available in the reconstruction series
# (benchmarked 116/120 valid days).  ``momentum.bb_position`` / ``momentum.bb_width``
# are deliberately absent: they are current-only facts with no FP-history series.
HISTORICAL_READY_PRIMITIVE_KEYS: tuple[str, ...] = (
    "equal_weight_return",
    "amount_weighted_return",
    "advance_ratio",
    "decline_ratio",
    "unchanged_ratio",
    "return_dispersion",
    "price_normalized_hhi",
    "amount_normalized_hhi",
    "trend.continuous.regime_strength",
    "participation.volume.ratio20",
    "participation.volume.ratio200",
)

_SPECS: dict[str, ObservationPrimitiveSpec] = {
    key: get_primitive(key) for key in HISTORICAL_READY_PRIMITIVE_KEYS
}


# ---------------------------------------------------------------------------
# Pure helpers (no IO, no mutation)
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    """Return ``value`` as a finite float, or None when non-finite / non-numeric."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _extract_value(spec: ObservationPrimitiveSpec, payload: dict[str, Any]) -> float | None:
    """Extract the comparable scalar for ``spec`` from the canonical L1 payload.

    ``spec.path`` walks the canonical payload and ``spec.extract`` fail-closes on
    missing / non-finite / distribution nodes, returning None (never 0).  Both
    come from the shared registry — no local path duplication.
    """
    node: Any = payload
    for key in spec.path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return spec.extract(node)


def _series_pair(item: Any) -> tuple[date, dict[str, Any]]:
    """Normalize one series item to ``(trade_date, canonical payload)``.

    Accepts the reconstruction series element shape (dict with ``trade_date`` as
    ISO string / ``observation`` as payload) as well as object form (attribute
    access).  ``trade_date`` is returned as a ``datetime.date``.
    """
    if isinstance(item, dict):
        td = item["trade_date"]
        payload = item["observation"]
    else:
        td = item.trade_date
        payload = item.observation
    if isinstance(td, str):
        td = date.fromisoformat(td)
    return td, payload


# ---------------------------------------------------------------------------
# Position fact (single T)
# ---------------------------------------------------------------------------


def compute_historical_position(
    current_value: Any,
    pre_t_values: Sequence[Any],
    *,
    window_size: int = POSITION_WINDOW_SIZE,
    minimum_valid_history: int = POSITION_MINIMUM_VALID_HISTORY,
) -> dict[str, Any]:
    """Compute one objective Historical Position fact for a single T.

    Args:
        current_value: the primitive value at T (may be None / NaN / inf).
        pre_t_values: primitive values at observations STRICTLY BEFORE T.  Only
            the latest ``window_size`` candidates are used; we never reach past
            that window to accumulate more valid values.
        window_size: candidate window length (default 120 observations).
        minimum_valid_history: minimum valid pre-T observations for a position
            (default 60).  ``valid_count < minimum_valid_history`` -> unavailable.

    Returns (transparent fact, deterministic, non-mutating):
        ``{"value", "position", "history": {window_size, minimum_valid_history,
        candidate_count, valid_count}, "status"}`` where ``status`` is
        ``"ready"`` | ``"insufficient_history"`` | ``"unavailable_current"``.
        ``position`` is ``None`` (never 0) unless ``status == "ready"``.
    """
    candidates = list(pre_t_values)[-window_size:]
    valid = [v for v in candidates if _finite(v) is not None]
    value = _finite(current_value)
    if value is None:
        status = "unavailable_current"
    elif len(valid) < minimum_valid_history:
        status = "insufficient_history"
    else:
        status = "ready"
    position = percentile_rank(value, valid) if status == "ready" else None
    return {
        "value": value,
        "position": position,
        "history": {
            "window_size": window_size,
            "minimum_valid_history": minimum_valid_history,
            "candidate_count": len(candidates),
            "valid_count": len(valid),
        },
        "status": status,
    }


# ---------------------------------------------------------------------------
# Position series (single primitive over the canonical observation series)
# ---------------------------------------------------------------------------


def compute_position_series(
    observation_series: Sequence[Any],
    primitive_key: str,
) -> list[dict[str, Any]]:
    """Compute the Position series for one primitive over a canonical series.

    Args:
        observation_series: ordered historical canonical Scope Observation
            series, trade_date ASCENDING.  Each element is either a dict
            (``{"trade_date": ISO, "observation": payload}`` — the exact
            ``reconstruct_scope_series`` output element) or an object exposing
            ``.trade_date`` / ``.observation``.
        primitive_key: one of ``HISTORICAL_READY_PRIMITIVE_KEYS``.

    Raises:
        KeyError: primitive not historical-ready (e.g. current-only).
        ValueError: series trade dates are not strictly ascending (fail fast —
            never silently re-sort and mask a caller bug).

    For index ``i`` the pre-T baseline is ``series[max(0, i-120):i]`` — it NEVER
    includes ``i`` (no future leakage; no ``series[:i+1]`` window).
    """
    if primitive_key not in _SPECS:
        raise KeyError(f"primitive not historical-ready: {primitive_key}")
    spec = _SPECS[primitive_key]
    pairs = [_series_pair(item) for item in observation_series]
    for prev, cur in zip(pairs, pairs[1:], strict=False):
        if not prev[0] < cur[0]:
            raise ValueError(
                "observation_series must be strictly ascending by trade_date; "
                f"got {prev[0]} -> {cur[0]}"
            )
    out: list[dict[str, Any]] = []
    for i, (td, payload) in enumerate(pairs):
        value_t = _extract_value(spec, payload)
        baseline = [
            _extract_value(spec, p)
            for _, p in pairs[max(0, i - POSITION_WINDOW_SIZE):i]
        ]
        fact = compute_historical_position(
            value_t,
            baseline,
            window_size=POSITION_WINDOW_SIZE,
            minimum_valid_history=POSITION_MINIMUM_VALID_HISTORY,
        )
        out.append(
            {
                "primitive_key": primitive_key,
                "trade_date": td.isoformat(),
                **fact,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Multi-primitive foundation (thin loop only — no framework)
# ---------------------------------------------------------------------------


def compute_historical_positions(
    observation_series: Sequence[Any],
    primitive_keys: Sequence[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Compute Position series for the eligible primitives.

    ``primitive_keys`` defaults to all 11 historical-ready primitives.  This is
    only a thin loop over :func:`compute_position_series` — no velocity / signal
    / acceleration / persistence is introduced here.
    """
    keys = (
        list(primitive_keys)
        if primitive_keys is not None
        else list(HISTORICAL_READY_PRIMITIVE_KEYS)
    )
    return {key: compute_position_series(observation_series, key) for key in keys}


__all__ = [
    "POSITION_WINDOW_SIZE",
    "POSITION_MINIMUM_VALID_HISTORY",
    "HISTORICAL_READY_PRIMITIVE_KEYS",
    "compute_historical_position",
    "compute_position_series",
    "compute_historical_positions",
]
