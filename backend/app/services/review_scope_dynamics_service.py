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

This is an EXPERIMENT / NOT_RUNTIME application path: not yet wired into the
orchestrator (no API / publication / scheduler / materialization / cache /
persistence / runtime activation).  It does NOT query the trading calendar —
``trade_dates`` is the caller-provided canonical A-share trading-date axis
(non-empty, strictly ascending, unique, every date
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
from app.services.review_historical_ew_columnar import (
    build_scope_dynamics_from_ew,
)
from app.services.review_historical_ew_db_service import (
    compute_current_static_historical_ew_batch,
)
from app.services.review_historical_scope_reconstruction_service import (
    reconstruct_scope_series_batch,
)
from app.services.review_observation_prep_service import _log_rss

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
    historical_source: str = "reconstruction",
) -> list[dict[str, Any]]:
    """Compose current-static Scope Dynamics for a batch of scopes (not-runtime-yet).

    VEC-1B: routes through ``reconstruct_scope_series_batch`` so the member x
    date window is loaded ONCE across all scopes that share a member (PERF-2)
    and the member observation is built once per unique member-day (VEC-1).
    The per-scope composition is EXACTLY the same helper as the single-scope
    path — there is no second Dynamics owner.

    The shared reconstruction is NOT disguised as a per-scope cost: each
    scope's ``metrics`` reports only its ``composition_ms``; the shared batch
    phase is reported once via ``batch_reconstruction_ms`` / ``batch_total_ms``
    on every result (identical across the batch, labelled ``batch_*``).

    M5-D1 (integration-only, NO owner switch yet): a second internal historical
    source is available via ``historical_source="columnar_ew"`` —
    ``compute_current_static_historical_ew_batch`` (close-only SQL -> columnar
    EW) then ``build_scope_dynamics_from_ew`` (canonical ObservationSeries +
    ScopeDynamics).  ``historical_source`` defaults to ``"reconstruction"`` so
    production behaviour is unchanged; the new implementation is reachable only
    when the caller explicitly opts in.  The public result contract
    ``{scope, membership, observation_series, scope_dynamics, metrics}`` is
    identical for both sources.

    EXPERIMENT / NOT_RUNTIME: not wired into API / orchestrator / persistence / frontend yet.
    """
    if historical_source not in ("reconstruction", "columnar_ew"):
        raise ValueError(
            "historical_source must be 'reconstruction' (legacy owner) or "
            f"'columnar_ew' (M5-D1 new owner); got {historical_source!r}"
        )
    _validate_trade_dates(trade_dates, analysis_asof_date=analysis_asof_date)
    if not scope_keys:
        return []

    if historical_source == "columnar_ew":
        return await _compose_batch_from_ew_source(
            db,
            scope_type,
            list(scope_keys),
            list(trade_dates),
            analysis_asof_date=analysis_asof_date,
        )

    import time

    # M4 dynamics-batch attribution: the current code holds reconstructions AND
    # the composed results simultaneously until return (reconstructions is not
    # released before results are built).  RSS boundaries here split the
    # reconstruction load from the composition step.  Observability only.
    _log_rss(
        "dynamics-batch-recon-start",
        scope_type=scope_type,
        scope_count=len(scope_keys),
        trade_date_count=len(trade_dates),
    )
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
    _log_rss(
        "dynamics-batch-recon-end",
        scope_type=scope_type,
        scope_count=len(scope_keys),
        trade_date_count=len(trade_dates),
        reconstruction_count=len(reconstructions),
    )

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
    _log_rss(
        "dynamics-batch-compose-end",
        scope_type=scope_type,
        scope_count=len(scope_keys),
        trade_date_count=len(trade_dates),
        reconstruction_count=len(reconstructions),
        result_count=len(results),
    )
    batch_composition_ms = sum(
        r["metrics"].get("composition_ms", 0.0) for r in results
    )
    for r in results:
        r["metrics"]["batch_scope_count"] = len(results)
        r["metrics"]["batch_reconstruction_ms"] = batch_reconstruction_ms
        r["metrics"]["batch_composition_ms"] = batch_composition_ms
        r["metrics"]["batch_total_ms"] = batch_reconstruction_ms + batch_composition_ms
    return results


async def _compose_batch_from_ew_source(
    db: AsyncSession,
    scope_type: str,
    scope_keys: list[str],
    trade_dates: list[date],
    *,
    analysis_asof_date: date,
) -> list[dict[str, Any]]:
    """M5-D1: columnar_ew historical source -> canonical Dynamics composition.

    The ONLY new-source batch composition owner.  Routes through:

        compute_current_static_historical_ew_batch        (close-only SQL -> EW)
            -> build_scope_dynamics_from_ew               (canonical owners)

    then re-packages into the SAME public contract as the legacy
    reconstruction path:  ``{scope, membership, observation_series,
    scope_dynamics, metrics}``.

    Fail-closed guarantees (deliberate, mirror the C2/C3 contract):

    * scope iteration of the EW batch MUST match the caller-supplied scope
      order exactly (no reorder, no missing / surplus scope);
    * every returned EW series MUST be exactly ``len(trade_dates)`` long and
      ``None`` (missing EW) stays ``None`` -> Builder unavailable — it is never
      coerced to zero;
    * a scope absent from the EW batch result raises, never skips.

    Metrics parity: per-scope keys mirror the reconstruction result.  The
    shared source cost is reported once via ``batch_ew_source_ms`` (the legacy
    reconstruction counterpart is ``batch_reconstruction_ms``, which we set to
    0 — no reconstruction ran).  Phase-level ``observation_series_ms`` /
    ``dynamics_ms`` are 0.0 here: ``build_scope_dynamics_from_ew`` is a single
    canonical-owner call that builds both, so only the true end-to-end
    ``composition_ms`` is measured per scope (see comment below).
    """
    import time

    _log_rss(
        "dynamics-batch-ew-start",
        scope_type=scope_type,
        scope_count=len(scope_keys),
        trade_date_count=len(trade_dates),
    )
    t_ew_src = time.perf_counter()
    ew_batch = await compute_current_static_historical_ew_batch(
        db,
        scope_type,
        scope_keys,
        trade_dates,
        analysis_asof_date=analysis_asof_date,
    )
    batch_ew_source_ms = (time.perf_counter() - t_ew_src) * 1000.0
    _log_rss(
        "dynamics-batch-ew-end",
        scope_type=scope_type,
        scope_count=len(scope_keys),
        trade_date_count=len(trade_dates),
        result_count=len(ew_batch.scopes),
    )

    scopes_by_key = {s.scope_key: s for s in ew_batch.scopes}
    if list(scopes_by_key) != scope_keys:
        raise RuntimeError(
            "columnar_ew batch scope ordering mismatch: EW batch returned "
            f"order {list(scopes_by_key)} != caller order {scope_keys}"
        )

    results: list[dict[str, Any]] = []
    for sk in scope_keys:
        scope_res = scopes_by_key[sk]
        if len(scope_res.ew_values) != len(trade_dates):
            raise ValueError(
                f"columnar_ew scope {sk} EW series length "
                f"{len(scope_res.ew_values)} != trade_dates length "
                f"{len(trade_dates)}"
            )
        t_build = time.perf_counter()
        built = build_scope_dynamics_from_ew(
            scope_type=scope_type,
            scope_key=sk,
            analysis_dates=trade_dates,
            ew_values=list(scope_res.ew_values),
        )
        # ``build_scope_dynamics_from_ew`` internally calls the canonical
        # ObservationSeries builder + ScopeDynamics analysis as ONE owner; we
        # measure the true end-to-end per-scope compose time here.
        ew_compose_ms = (time.perf_counter() - t_build) * 1000.0
        results.append(
            _package_scope_dynamics_from_ew(
                built=built,
                scope_type=scope_type,
                scope_key=sk,
                member_count=scope_res.member_count,
                scope_name=scope_res.scope_name,
                trade_dates=trade_dates,
                analysis_asof_date=analysis_asof_date,
                ew_source_composition_ms=ew_compose_ms,
            )
        )
        _log_rss(
            "dynamics-batch-ew-composed",
            scope_type=scope_type,
            result_count=len(results),
        )

    batch_composition_ms = sum(
        r["metrics"].get("composition_ms", 0.0) for r in results
    )
    for r in results:
        r["metrics"]["batch_scope_count"] = len(results)
        r["metrics"]["batch_reconstruction_ms"] = 0.0
        r["metrics"]["batch_ew_source_ms"] = batch_ew_source_ms
        r["metrics"]["batch_composition_ms"] = batch_composition_ms
        r["metrics"]["batch_total_ms"] = batch_ew_source_ms + batch_composition_ms
    return results


def _package_scope_dynamics_from_ew(
    *,
    built: dict[str, Any],
    scope_type: str,
    scope_key: str,
    member_count: int,
    scope_name: str,
    trade_dates: Sequence[date],
    analysis_asof_date: date,
    ew_source_composition_ms: float,
) -> dict[str, Any]:
    """Package one built EW->Dynamics chain into the canonical public result.

    Result keys / shapes are identical to
    :func:`_compose_scope_dynamics_from_reconstruction` so the orchestrator and
    composition boundary are source-agnostic.  ``membership`` mirrors the
    reconstruction provenance layout (mode / asof_date / member_count).
    """
    observation_series = built.get("observation_series")
    scope_dynamics = built.get("dynamics")
    return {
        "scope": {
            "scope_type": scope_type,
            "scope_key": scope_key,
        },
        "membership": {
            "mode": "current_static",
            "asof_date": analysis_asof_date.isoformat(),
            "member_count": member_count,
        },
        "observation_series": observation_series,
        "scope_dynamics": scope_dynamics,
        "metrics": {
            "scope_count": 1,
            "member_count": member_count,
            "scope_name": scope_name,
            "trade_date_count": len(trade_dates),
            "member_date_count": member_count * len(trade_dates),
            "vec_hit": 0,
            "vec_fallback": 0,
            "fallback_reasons": [],
            "historical_source": "columnar_ew",
            "reconstruction_ms": 0.0,
            "observation_series_ms": 0.0,
            "dynamics_ms": 0.0,
            "composition_ms": ew_source_composition_ms,
            "total_ms": ew_source_composition_ms,
        },
    }


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
    single-scope series or one entry of a shared batch) into the NOT_RUNTIME
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
