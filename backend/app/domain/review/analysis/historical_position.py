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

Canonical input path (PRD §7.7.5 bridge)
----------------------------------------
The ONE canonical Position calculation path is
:func:`compute_position_series_from_primitive_series` — it consumes a formal
date-complete ``PrimitiveSeries`` (the Observation Series Builder's
``primitives[key]`` block) whose ``PrimitivePoint`` timeline already preserves
missing trading-observation slots.  :func:`compute_position_series` is only a
compatibility adapter over the legacy raw canonical L1 payload series: it
extracts values through the shared registry and delegates to the same core — so
the raw path and the Builder path share exactly ONE percentile / window math
owner.  No second percentile / 120-window / 60-minimum logic exists.

Primitive source
----------------
No payload paths are hard-coded here.  The 11 historical-ready primitives are
consumed exclusively through the shared ``OBSERVATION_PRIMITIVES`` registry
(the same single source of truth for path + extraction used by Analysis A / C).
``momentum.bb_position`` / ``momentum.bb_width`` are CURRENT-ONLY and are never
eligible for a historical Position.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app.domain.review.observation_primitives import (
    ObservationPrimitiveSpec,
    get_primitive,
)
from app.domain.shared.historical_position import (
    compute_historical_position as shared_compute_historical_position,
)
from app.domain.shared.historical_position import (
    finite_number,
)

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


_finite = finite_number  # NO_FORMULA_CHANGE：委托共享原语（AUCTION-V3.2 §十二）

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
    """Thin wrapper -> shared primitive (NO_FORMULA_CHANGE, AUCTION-V3.2 §十二)."""
    return shared_compute_historical_position(
        current_value,
        pre_t_values,
        window_size=window_size,
        minimum_valid_history=minimum_valid_history,
    )

# ---------------------------------------------------------------------------
# Position series (canonical: formal PrimitiveSeries input)
# ---------------------------------------------------------------------------


def _point_date(value: Any) -> date:
    """Normalize a PrimitivePoint ``trade_date`` (ISO string or ``date``)."""
    if isinstance(value, str):
        return date.fromisoformat(value)
    return value


def compute_position_series_from_primitive_series(
    primitive_series: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Compute the Position series from a formal PrimitiveSeries (PRD §7.7.5).

    This is the ONE canonical Position calculation path.  The input is the
    Observation Series Builder's ``primitives[key]`` block: a date-complete,
    availability-bearing ``PrimitivePoint`` timeline in which a missing
    trading-observation slot is already preserved (``available=False`` /
    ``value=None``).

    Contract (frozen, PRD §7.9 + §7.7.5):
        - ``primitive_series["key"]`` must be historical-ready, else ``KeyError``;
        - points must be strictly ascending & unique by ``trade_date`` (fail
          fast, never silent sort / dedupe);
        - ``available=True`` requires a finite scalar (None / NaN / inf / bool /
          non-numeric -> ``ValueError``); ``available=False`` requires
          ``value=None`` (a finite value -> ``ValueError``);
        - ``readiness`` NEVER overrides ``available`` — a ``partial`` snapshot
          with a finite value is valid Position input;
        - every point produces exactly one Position fact (never compressed);
          for index ``i`` the pre-T baseline is the latest ``POSITION_WINDOW_SIZE``
          point slots strictly before ``i`` — an unavailable slot stays in the
          candidate window and contributes ``None`` (never reached-over);
        - delegates the percentile / window math to the single-T owner
          ``compute_historical_position`` — no second implementation here.

    Returns:
        One Position fact per point, date-aligned: ``{"primitive_key",
        "trade_date", "value", "position", "history", "status"}``.
    """
    key = primitive_series["key"]
    if key not in _SPECS:
        raise KeyError(f"primitive not historical-ready: {key}")

    points = primitive_series["points"]
    dates = [_point_date(pt["trade_date"]) for pt in points]
    for prev, cur in zip(dates, dates[1:], strict=False):
        if not prev < cur:
            raise ValueError(
                "PrimitiveSeries points must be strictly ascending by trade_date; "
                f"got {prev.isoformat()} -> {cur.isoformat()}"
            )

    # Pre-bind the per-point current value and enforce the available/value
    # contract once (availability is decided by the Builder's registry extractor
    # only — readiness is deliberately NOT consulted here).
    values: list[float | None] = []
    for idx, pt in enumerate(points):
        if pt["available"]:
            finite = _finite(pt["value"])
            if finite is None:
                raise ValueError(
                    "available=True with non-finite / non-numeric value at "
                    f"{dates[idx].isoformat()}: {pt['value']!r}"
                )
        else:
            if pt["value"] is not None:
                raise ValueError(
                    "available=False with non-None value at "
                    f"{dates[idx].isoformat()}: {pt['value']!r}"
                )
            finite = None
        values.append(finite)

    out: list[dict[str, Any]] = []
    for i, (d, current_value) in enumerate(zip(dates, values, strict=True)):
        # pre-T baseline = latest POSITION_WINDOW_SIZE point slots before i.
        baseline = values[max(0, i - POSITION_WINDOW_SIZE) : i]
        fact = compute_historical_position(
            current_value,
            baseline,
            window_size=POSITION_WINDOW_SIZE,
            minimum_valid_history=POSITION_MINIMUM_VALID_HISTORY,
        )
        out.append({"primitive_key": key, "trade_date": d.isoformat(), **fact})
    return out


# ---------------------------------------------------------------------------
# Position series (legacy compatibility adapter over raw canonical payloads)
# ---------------------------------------------------------------------------


def compute_position_series(
    observation_series: Sequence[Any],
    primitive_key: str,
) -> list[dict[str, Any]]:
    """Compute the Position series for one primitive — compatibility adapter.

    NOT a second math owner.  This legacy raw-payload path first extracts each
    value through the shared registry (fail-closed None on missing / non-finite)
    into an equivalent date-complete PrimitiveSeries, then delegates to
    :func:`compute_position_series_from_primitive_series`.  The raw path and the
    Observation Series Builder path therefore share exactly ONE Position math
    owner (percentile / 120-window / 60-minimum / candidate_count).

    Args:
        observation_series: ordered historical canonical Scope Observation
            series, trade_date ASCENDING.  Each element is either a dict
            (``{"trade_date": ISO, "observation": payload}`` — the exact
            ``reconstruct_scope_series_batch`` series element) or an object exposing
            ``.trade_date`` / ``.observation``.
        primitive_key: one of ``HISTORICAL_READY_PRIMITIVE_KEYS``.

    Raises:
        KeyError: primitive not historical-ready (e.g. current-only).
        ValueError: series trade dates are not strictly ascending (fail fast —
            never silently re-sort and mask a caller bug).
    """
    if primitive_key not in _SPECS:
        raise KeyError(f"primitive not historical-ready: {primitive_key}")
    spec = _SPECS[primitive_key]
    points: list[dict[str, Any]] = []
    for item in observation_series:
        td, payload = _series_pair(item)
        value = _extract_value(spec, payload)
        points.append(
            {
                "trade_date": td.isoformat(),
                "readiness": "raw_adapter",
                "value": value,
                "available": value is not None,
            }
        )
    return compute_position_series_from_primitive_series(
        {"key": primitive_key, "l1_path": spec.path, "points": points}
    )


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
    "compute_position_series_from_primitive_series",
    "compute_position_series",
    "compute_historical_positions",
]
