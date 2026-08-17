"""Review v2.3 — Analysis B Current-Static Scope Dynamics Application Composition.

The UNIQUE Application Composition Owner that wires the frozen layers:

    Current-Static Reconstruction
        (review_historical_scope_reconstruction_service.reconstruct_scope_series)
            -> ObservationSeries (observation_series.build_observation_series)
            -> Scope Dynamics (scope_dynamics.compute_scope_dynamics_analysis)

It is NOT the Historical Source owner, NOT the ObservationSeries owner, NOT the
Position owner, NOT the Dynamics Phase owner.  This module never re-implements
membership resolution / batch reconstruction / primitive extraction / gap logic /
Position / EMA / Velocity / Acceleration / Persistence / Phase.  It only:

    validate input -> call reconstruction source -> adapt source shape ->
    build ObservationSeries -> call Scope Dynamics -> package provenance.

Source contract (frozen, PRD §7.9 / §7.15.2): CURRENT STATIC MEMBERSHIP x
historical member facts.  This service NEVER touches
``review_observation_history_service`` persisted PIT series.  After the source
returns, identity / membership-mode / as-of provenance is fail-fast validated;
any mismatch raises ``CurrentStaticDynamicsSourceContractError`` so a
historical-PIT source can never silently replace the current-static application
path.

This is a SHADOW application path: NO API / publication / orchestrator /
scheduler / materialization / cache / persistence / runtime activation.  It does
NOT query the trading calendar — ``trade_dates`` is the caller-provided canonical
A-share trading-date axis (non-empty, strictly ascending, unique, every date
<= analysis_asof_date; no silent sort / dedupe).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.analysis.observation_series import build_observation_series
from app.domain.review.analysis.scope_dynamics import (
    DYNAMICS_PHASE_PRIMITIVE_KEY,
    compute_scope_dynamics_analysis,
)
from app.services.review_historical_scope_reconstruction_service import (
    reconstruct_scope_series,
)


class CurrentStaticDynamicsSourceContractError(RuntimeError):
    """The reconstruction source did not honour the current-static contract."""


def _validate_trade_dates(
    trade_dates: Sequence[date],
    *,
    analysis_asof_date: date,
) -> None:
    """Fail fast on an invalid caller-provided trading-date axis.

    Enforces non-empty / unique / strictly ascending and every date
    ``<= analysis_asof_date``.  No silent sort, no silent dedupe.
    """
    if not trade_dates:
        raise ValueError("trade_dates must be non-empty")
    prev: date | None = None
    for d in trade_dates:
        if not isinstance(d, date):
            raise ValueError(f"trade_dates must contain datetime.date, got {type(d).__name__}")
        if d > analysis_asof_date:
            raise ValueError(
                f"trade_date {d.isoformat()} is after analysis_asof_date "
                f"{analysis_asof_date.isoformat()} (future relative to the analysis as-of)"
            )
        if prev is not None:
            if prev == d:
                raise ValueError(f"duplicate trade_date: {d.isoformat()}")
            if not prev < d:
                raise ValueError(
                    f"trade_dates must be strictly ascending, got "
                    f"{prev.isoformat()} -> {d.isoformat()}"
                )
        prev = d


def _guard_source_contract(
    reconstruction: dict[str, Any],
    *,
    scope_type: str,
    scope_key: str,
    analysis_asof_date: date,
) -> None:
    """Fail fast when the source did not honour the current-static contract."""
    scope = reconstruction.get("scope")
    if (
        scope is None
        or scope.get("scope_type") != scope_type
        or scope.get("scope_key") != scope_key
    ):
        raise CurrentStaticDynamicsSourceContractError(
            "reconstruction scope identity mismatch: "
            f"expected ({scope_type!r}, {scope_key!r}), got {scope!r}"
        )
    membership = reconstruction.get("membership")
    if membership is None or membership.get("mode") != "current_static":
        raise CurrentStaticDynamicsSourceContractError(
            f"reconstruction membership.mode must be 'current_static', got {membership!r}"
        )
    if membership.get("asof_date") != analysis_asof_date.isoformat():
        raise CurrentStaticDynamicsSourceContractError(
            "reconstruction membership.asof_date "
            f"{membership.get('asof_date')!r} != analysis_asof_date "
            f"{analysis_asof_date.isoformat()!r}"
        )


async def compute_current_static_scope_dynamics(
    db: AsyncSession,
    scope_type: str,
    scope_key: str,
    trade_dates: Sequence[date],
    *,
    analysis_asof_date: date,
) -> dict[str, Any]:
    """Compose the current-static Scope Dynamics application path (shadow).

    Args:
        db: AsyncSession (only passed through to the reconstruction source).
        scope_type / scope_key: identify the scope.
        trade_dates: caller-provided canonical A-share trading-date axis —
            non-empty / unique / strictly ascending / every date
            ``<= analysis_asof_date`` (fail fast otherwise).
        analysis_asof_date: current-static membership as-of date.

    Returns (internal shadow application result — NOT a public API schema):
        ``{"scope", "membership", "observation_series", "scope_dynamics"}``.
        ``scope`` / ``membership`` are passed through unchanged from the source
        owner; provenance is never re-derived here.
    """
    _validate_trade_dates(trade_dates, analysis_asof_date=analysis_asof_date)

    reconstruction = await reconstruct_scope_series(
        db,
        scope_type,
        scope_key,
        list(trade_dates),
        asof_date=analysis_asof_date,
    )
    _guard_source_contract(
        reconstruction,
        scope_type=scope_type,
        scope_key=scope_key,
        analysis_asof_date=analysis_asof_date,
    )

    # Adapt the reconstruction rows into Builder snapshots.  A row that really
    # exists is always "ready" — member-level availability (provided_member_count)
    # must NOT downgrade a Scope snapshot (PRD §7.15.2).  A caller-axis date with
    # no row is simply omitted: the Builder preserves it as an unavailable slot.
    snapshot_series = [
        {
            "trade_date": row["trade_date"],
            "readiness": "ready",
            "payload": row["observation"],
        }
        for row in reconstruction["series"]
    ]

    observation_series = build_observation_series(
        scope_type=scope_type,
        scope_key=scope_key,
        from_date=trade_dates[0],
        to_date=trade_dates[-1],
        trading_dates=trade_dates,
        snapshot_series=snapshot_series,
        primitive_keys=[DYNAMICS_PHASE_PRIMITIVE_KEY],
    )

    scope_dynamics = compute_scope_dynamics_analysis(observation_series)

    return {
        "scope": reconstruction["scope"],
        "membership": reconstruction["membership"],
        "observation_series": observation_series,
        "scope_dynamics": scope_dynamics,
    }
