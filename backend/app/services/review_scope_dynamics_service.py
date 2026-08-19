"""Review v2.3 — Analysis B Current-Static Scope Dynamics Application Composition.

The UNIQUE Application Composition Owner that wires the frozen layers:

    Current-Static Reconstruction
        (review_historical_scope_reconstruction_service.reconstruct_scope_series_batch)
            -> ObservationSeries (observation_series.build_observation_series)
            -> Scope Dynamics (scope_dynamics.compute_scope_dynamics_analysis)

It is NOT the Historical Source owner, NOT the ObservationSeries owner, NOT the
Position owner, NOT the Dynamics Phase owner.  This module never re-implements
membership resolution / batch reconstruction / primitive extraction / gap logic /
Position / EMA / Velocity / Acceleration / Persistence / Phase.  It only:

    validate input -> call reconstruction source -> adapt source shape ->
    build ObservationSeries -> call Scope Dynamics -> package provenance.

SINGLE entry point: :func:`compute_current_static_scope_dynamics_batch` (the
unique Dynamics composition implementation).  A single scope routes through the
SAME batch owner with a batch size of one — there is no second single-scope
composition path; the shared ``_compose_scope_dynamics_from_reconstruction`` is
the one composition implementation used by the batch.

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

import logging
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
    reconstruct_scope_series_batch,
)

logger = logging.getLogger(__name__)


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


async def compute_current_static_scope_dynamics_batch(
    db: AsyncSession,
    scope_type: str,
    scope_keys: Sequence[str],
    trade_dates: Sequence[date],
    *,
    analysis_asof_date: date,
    union_member_cap: int = 4096,
) -> list[dict[str, Any]]:
    """Compose current-static Scope Dynamics for a batch of scopes (shadow).

    VEC-1B: routes through ``reconstruct_scope_series_batch`` so the member x
    date window is loaded ONCE across all scopes that share a member (PERF-2)
    and the member observation is built once per unique member-day (VEC-1).
    The per-scope composition is EXACTLY the same helper as the single-scope
    path — there is no second Dynamics owner.

    The shared reconstruction is NOT disguised as a per-scope cost: each
    scope's ``metrics`` reports only its ``composition_ms``; the shared batch
    phase is reported once via ``batch_reconstruction_ms`` / ``batch_total_ms``
    on every result (identical across the batch, labelled ``batch_*``).

    SHADOW ONLY: never wired into API / orchestrator / persistence / frontend.
    """
    _validate_trade_dates(trade_dates, analysis_asof_date=analysis_asof_date)
    if not scope_keys:
        return []

    import time

    t_recon = time.perf_counter()
    reconstructions = await reconstruct_scope_series_batch(
        db,
        scope_type,
        list(scope_keys),
        list(trade_dates),
        asof_date=analysis_asof_date,
        union_member_cap=union_member_cap,
    )
    batch_reconstruction_ms = (time.perf_counter() - t_recon) * 1000.0

    results = [
        _compose_scope_dynamics_from_reconstruction(
            reconstruction,
            scope_type=scope_type,
            scope_key=reconstruction["scope"]["scope_key"],
            trade_dates=trade_dates,
            analysis_asof_date=analysis_asof_date,
        )
        for reconstruction in reconstructions
    ]
    batch_composition_ms = sum(
        r["metrics"].get("composition_ms", 0.0) for r in results
    )
    for r in results:
        r["metrics"]["batch_scope_count"] = len(results)
        r["metrics"]["batch_reconstruction_ms"] = batch_reconstruction_ms
        r["metrics"]["batch_composition_ms"] = batch_composition_ms
        r["metrics"]["batch_total_ms"] = batch_reconstruction_ms + batch_composition_ms
    return results


def _compose_scope_dynamics_from_reconstruction(
    reconstruction: dict[str, Any],
    *,
    scope_type: str,
    scope_key: str,
    trade_dates: Sequence[date],
    analysis_asof_date: date,
    reconstruction_ms: float = 0.0,
) -> dict[str, Any]:
    """Shared composition: reconstruction source -> ObservationSeries -> Dynamics.

    The single owner that turns one current-static reconstruction (either a
    single-scope series or one entry of a shared batch) into the shadow
    ObservationSeries + Scope Dynamics result.  Both application entry points
    delegate here so there is exactly one composition implementation.

    ``reconstruction_ms`` is the caller-measured time spent in the source phase
    (single-scope reconstruction, or 0.0 for a shared batch entry whose cost is
    reported once under ``batch_reconstruction_ms``).  ``total_ms`` is the
    end-to-end ``reconstruction_ms + composition_ms``.
    """
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
    import time

    composition_ms = observation_series_ms = dynamics_ms = 0.0
    t_comp = time.perf_counter()

    snapshot_series = [
        {
            "trade_date": row["trade_date"],
            "readiness": "ready",
            "payload": row["observation"],
        }
        for row in reconstruction["series"]
    ]

    t_series = time.perf_counter()
    observation_series = build_observation_series(
        scope_type=scope_type,
        scope_key=scope_key,
        from_date=trade_dates[0],
        to_date=trade_dates[-1],
        trading_dates=trade_dates,
        snapshot_series=snapshot_series,
        primitive_keys=[DYNAMICS_PHASE_PRIMITIVE_KEY],
    )
    observation_series_ms = (time.perf_counter() - t_series) * 1000.0

    t_dyn = time.perf_counter()
    scope_dynamics = compute_scope_dynamics_analysis(observation_series)
    dynamics_ms = (time.perf_counter() - t_dyn) * 1000.0
    composition_ms = (time.perf_counter() - t_comp) * 1000.0
    total_ms = reconstruction_ms + composition_ms

    _mem = reconstruction.get("membership") or {}
    _mem_count = _mem.get("member_count") if isinstance(_mem, dict) else len(_mem)
    _prep = reconstruction.get("prep_metrics") or {}
    logger.info(
        "[scope-dynamics] scope_type=%s scope_key=%s member_count=%s "
        "trade_date_count=%d vec_hit=%d vec_fallback=%d fallback_reasons=%s "
        "reconstruction_ms=%.1f observation_series_ms=%.1f dynamics_ms=%.1f "
        "composition_ms=%.1f total_ms=%.1f",
        scope_type, scope_key, _mem_count, len(trade_dates),
        _prep.get("vec_hit", 0), _prep.get("vec_fallback", 0),
        ",".join(_prep.get("fallback_reasons", [])) or "-",
        reconstruction_ms, observation_series_ms, dynamics_ms,
        composition_ms, total_ms,
    )

    return {
        "scope": reconstruction["scope"],
        "membership": reconstruction["membership"],
        "observation_series": observation_series,
        "scope_dynamics": scope_dynamics,
        "metrics": {
            "scope_count": 1,
            "member_count": _mem_count,
            "trade_date_count": len(trade_dates),
            "member_date_count": (_mem_count if isinstance(_mem_count, int) else 0)
            * len(trade_dates),
            "vec_hit": _prep.get("vec_hit", 0),
            "vec_fallback": _prep.get("vec_fallback", 0),
            "fallback_reasons": list(_prep.get("fallback_reasons", [])),
            "reconstruction_ms": reconstruction_ms,
            "observation_series_ms": observation_series_ms,
            "dynamics_ms": dynamics_ms,
            "composition_ms": composition_ms,
            "total_ms": total_ms,
        },
    }
