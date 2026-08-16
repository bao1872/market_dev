"""Analysis Foundation — Observation Series Builder (PRD §7.7.5).

Pure domain.  Builds a **date-complete** ``ObservationSeries`` from:

- a canonical trading-date axis (provided by the future application layer from
  the canonical A-share trading calendar — this Builder NEVER queries a
  calendar);
- the History Service sparse snapshot series (ACTUAL shape
  ``[{"trade_date", "readiness", "payload"}]``);
- the Observation Primitive Registry (single extraction owner).

Responsibility (frozen, PRD §7.7.5 + task spec)
------------------------------------------------
- **Date alignment**: every trading date produces exactly one ``PrimitivePoint``
  per primitive — a trading day with no persisted snapshot is preserved as an
  unavailable observation slot, it NEVER disappears from the timeline.
- **Gap preservation**: no drop / compress / forward-fill / zero-fill; a missing
  snapshot day is expressed at the ``PrimitivePoint`` layer as
  ``{"trade_date": ISO, "readiness": "unavailable", "value": None,
  "available": False}`` — never as a fake canonical L1 payload.
- **Primitive extraction**: the ONLY owner is ``OBSERVATION_PRIMITIVES`` /
  ``get_primitive``; no L1 path is hard-coded here.
- **available semantics**: ``available = value is a finite scalar``
  (the registry's ``extract`` returns finite-float-or-None) — independent of
  snapshot ``readiness``.  A ``partial`` snapshot can still carry available
  values; a ``ready`` snapshot can still carry ``value=None``.

NOT owned (frozen): percentile / Position / EMA / Velocity / Acceleration /
Persistence / Phase / Internal Structure / score / signal.  This module only
``align + extract``; it never invokes an analysis owner and never touches DB /
network / member loop / reconstruction.

Date validation is strict and fail-fast: trading dates strictly ascending,
unique, within ``[from_date, to_date]``; snapshot dates strictly ascending,
unique and every snapshot date must belong to the trading axis.  No silent sort,
no silent dedupe, no out-of-window tolerance.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from app.domain.review.observation_primitives import (
    ObservationPrimitiveSpec,
    get_primitive,
    list_primitive_keys,
)

# Builder-level readiness label for a trading observation that has no consumable
# canonical snapshot source.  This is NOT a new Historical Dynamics status
# vocabulary — the PRD forbids adding gap / missing / paused / stale there.  It
# only means "this trading-observation slot has no snapshot to consume".
READINESS_UNAVAILABLE = "unavailable"


def _to_date(value: Any, *, field: str) -> date:
    """Normalize ``value`` to a ``datetime.date`` (date / datetime / ISO str)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError(f"{field} must be a date / datetime / ISO string, got {type(value).__name__}")


def _require_strictly_ascending(dates: Sequence[date], *, field: str) -> None:
    """Fail fast on duplicate / descending dates (never silent sort / dedupe)."""
    for prev, cur in zip(dates, dates[1:], strict=False):
        if not prev < cur:
            raise ValueError(
                f"{field} must be strictly ascending and unique; "
                f"got {prev.isoformat()} -> {cur.isoformat()}"
            )


def _extract_from_payload(spec: ObservationPrimitiveSpec, payload: Any) -> float | None:
    """Walk ``spec.path`` into the payload and run the registry ``extract``.

    Fail-closed: a missing node (or a non-dict traversal step) yields None, then
    ``spec.extract`` applies the canonical scalar rule (finite float or None).
    No local extraction logic exists here — only the registry decides.
    """
    node: Any = payload
    for key in spec.path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return spec.extract(node)


def build_observation_series(
    *,
    scope_type: str,
    scope_key: str,
    from_date: Any,
    to_date: Any,
    trading_dates: Sequence[Any],
    snapshot_series: Sequence[Mapping[str, Any]],
    availability: Mapping[str, Any] | None = None,
    primitive_keys: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build a date-complete ``ObservationSeries`` (PRD §7.7.5).

    Args:
        scope_type / scope_key: identifying the scope.
        from_date / to_date: inclusive series query window bounds.
        trading_dates: canonical trading-date axis, strictly ascending, unique,
            every date within ``[from_date, to_date]``.
        snapshot_series: History Service ACTUAL shape
            ``[{"trade_date": ISO, "readiness": str, "payload": dict}]``,
            strictly ascending, unique, every snapshot date on the axis.
        availability: History Service availability metadata (spread into the
            output with three transparent source-coverage counts appended).
        primitive_keys: registered primitive keys to extract; ``None`` -> all
            registry primitives.  Unknown key -> KeyError (fail fast).

    Returns:
        ``{"scope_type", "scope_key", "query_window", "availability",
        "primitives"}`` following the frozen §7.7.5 shape.
    """
    from_d = _to_date(from_date, field="from_date")
    to_d = _to_date(to_date, field="to_date")
    if from_d > to_d:
        raise ValueError(
            f"from_date must be <= to_date, got {from_d.isoformat()} -> {to_d.isoformat()}"
        )

    # Trading-date axis validation.
    axis = [_to_date(d, field="trading_dates") for d in trading_dates]
    _require_strictly_ascending(axis, field="trading_dates")
    for d in axis:
        if not from_d <= d <= to_d:
            raise ValueError(
                f"trading_date {d.isoformat()} outside query window "
                f"[{from_d.isoformat()}, {to_d.isoformat()}]"
            )
    axis_set = set(axis)

    # Snapshot series validation (strictly ascending, unique, on the axis).
    snapshot_dates: list[date] = []
    snapshot_by_date: dict[date, Mapping[str, Any]] = {}
    for item in snapshot_series:
        sdate = _to_date(item["trade_date"], field="snapshot_series.trade_date")
        if sdate not in axis_set:
            raise ValueError(
                f"snapshot trade_date {sdate.isoformat()} is not on the trading-date axis"
            )
        if sdate in snapshot_by_date:
            raise ValueError(f"duplicate snapshot trade_date: {sdate.isoformat()}")
        snapshot_by_date[sdate] = item
        snapshot_dates.append(sdate)
    _require_strictly_ascending(snapshot_dates, field="snapshot_series.trade_date")

    # Primitive selection — the single registry owner decides paths / extraction.
    keys = list(primitive_keys) if primitive_keys is not None else list_primitive_keys()
    specs = [(key, get_primitive(key)) for key in keys]  # unknown key -> KeyError

    primitives: dict[str, Any] = {}
    for key, spec in specs:
        points: list[dict[str, Any]] = []
        for d in axis:
            snapshot = snapshot_by_date.get(d)
            if snapshot is None:
                # A legal trading observation with no consumable snapshot source.
                # Preserved as an unavailable slot — never dropped, never zero-filled.
                points.append(
                    {
                        "trade_date": d.isoformat(),
                        "readiness": READINESS_UNAVAILABLE,
                        "value": None,
                        "available": False,
                    }
                )
                continue
            value = _extract_from_payload(spec, snapshot["payload"])
            points.append(
                {
                    "trade_date": d.isoformat(),
                    "readiness": snapshot["readiness"],
                    "value": value,
                    # The registry ``extract`` contract guarantees a finite float,
                    # or None (missing / non-finite).  available therefore ==
                    # "the registry produced a finite scalar" — readiness is
                    # deliberately NOT consulted (PRD §7.7.5).
                    "available": value is not None,
                }
            )
        primitives[key] = {
            "key": key,
            "l1_path": spec.path,
            "points": points,
        }

    coverage: dict[str, Any] = dict(availability) if availability is not None else {}
    coverage.update(
        {
            "trading_observation_count": len(axis),
            "snapshot_count": len(snapshot_series),
            "missing_snapshot_count": len(axis) - len(snapshot_series),
        }
    )

    return {
        "scope_type": scope_type,
        "scope_key": scope_key,
        "query_window": {
            "from_date": from_d.isoformat(),
            "to_date": to_d.isoformat(),
        },
        "availability": coverage,
        "primitives": primitives,
    }


__all__ = [
    "READINESS_UNAVAILABLE",
    "build_observation_series",
]
