"""Historical Dynamics — Production Close-Only SQL Adapter (M5-C1).

This module is the **production DB-aware adapter** that feeds the M5-B1
columnar EW core (``review_historical_ew_columnar``) from three canonical
owners:

* ``list_recent_trading_days`` + ``_build_t1_map`` — exact calendar / T-1.
* ``resolve_current_memberships_batch`` — current-static membership (no PIT).
* ``BarDaily`` rows read via **SQL projection with exactly three columns** and
  a true ``session.stream(..., yield_per=...)`` iteration.

The result payload is intentionally **small** and transient-matrix-free: only
the scope-level equal-weight return series plus diagnostic source metrics are
retained.  No ObservationSeries, no ScopeDynamics, no MemberObservation —
those live in downstream composition and are deliberately deferred to later
slices (M5-C2 / D6 shadow).

Strict input / forbidden paths
==============================

This service MUST NOT:
* independently build an analysis axis of its own — ``trade_dates`` is the
  caller-supplied authoritative axis;
* call any heavy ``review_observation_prep_service`` reconstruction owners
  (``prepare_union_fact_context`` and friends);
* hydrate full ORM ``BarDaily`` objects for the bar stream;
* call ``reconstruct_scope_series_batch``.
"""
from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Canonical owners — the only production owners this adapter is allowed to
# reference.
# ---------------------------------------------------------------------------
from app.services.review_historical_ew_columnar import (
    build_required_bar_axis,
    build_scope_member_indices,
    compute_return_matrix,
    compute_scope_ew_matrix,
)
from app.services.review_historical_scope_reconstruction_service import (
    CurrentStaticMembership,
    resolve_current_memberships_batch,
)
from app.services.review_observation_prep_service import (
    _build_t1_map,
    _log_rss,
    list_recent_trading_days,
)
from app.models.bar import BarDaily


# ===========================================================================
# Result contract
# ===========================================================================
@dataclass(frozen=True)
class HistoricalEWScopeResult:
    """One scope's EW series aligned exactly to the requested ``trade_dates``."""

    scope_key: str
    scope_name: str
    member_count: int
    # Length == len(trade_dates).  ``None`` = unavailable (canonical NaN).
    ew_values: tuple[float | None, ...]


@dataclass(frozen=True)
class HistoricalEWSourceMetrics:
    """Transparent source-process diagnostics (used by shadow / audit / logs).

    Durations are wall-clock milliseconds via ``time.perf_counter()``.
    Memory markers are emitted to ``_log_rss`` side-channel as well; the
    struct stores only the aggregate dimensions required for shadow parity
    evidence / OOM post-mortems.
    """

    calendar_ms: int = 0
    membership_ms: int = 0
    bars_stream_ms: int = 0
    return_ms: int = 0
    ew_ms: int = 0
    total_ms: int = 0

    rows_streamed: int = 0
    finite_close_cells: int = 0
    unavailable_close_rows: int = 0
    duplicate_streamed_cells: int = 0
    missing_cells: int = 0

    R: int = 0
    M: int = 0
    S: int = 0
    scope_member_refs: int = 0
    close_matrix_mib: float = 0.0


@dataclass(frozen=True)
class HistoricalEWBatchResult:
    scope_type: str
    analysis_asof_date: date
    trade_dates: tuple[date, ...]
    scopes: tuple[HistoricalEWScopeResult, ...]
    metrics: HistoricalEWSourceMetrics
    # Deterministic scope ordering.
    scope_order: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "scope_order",
            tuple(s.scope_key for s in self.scopes),
        )


# ===========================================================================
# Internal helpers
# ===========================================================================
def _normalize_analysis_dates(
    trade_dates: Sequence[date],
    analysis_asof_date: date,
) -> list[date]:
    """Validate caller-supplied analysis axis.

    Fail-closed on:
      * empty axis;
      * non-unique or non-strictly-ascending dates;
      * any date strictly after ``analysis_asof_date``;
      * trailing date != ``analysis_asof_date``.
    """
    if not trade_dates:
        raise ValueError("trade_dates must be non-empty")
    normalized: list[date] = []
    prev: date | None = None
    for raw in trade_dates:
        if not isinstance(raw, date):
            raise TypeError(
                f"trade_dates elements must be date objects; got {type(raw).__name__}"
            )
        if raw > analysis_asof_date:
            raise ValueError(
                f"trade_dates contains a date later than analysis_asof_date: "
                f"{raw} > {analysis_asof_date}"
            )
        if prev is not None and not (prev < raw):
            raise ValueError(
                "trade_dates must be strictly ascending and strictly unique"
            )
        normalized.append(raw)
        prev = raw
    if normalized[-1] != analysis_asof_date:
        raise ValueError(
            f"trade_dates[-1] ({normalized[-1]}) must equal "
            f"analysis_asof_date ({analysis_asof_date})"
        )
    return normalized


def _require_calendar_suffix(
    trade_dates: Sequence[date],
    trading_days_sorted_asc: Sequence[date],
) -> None:
    """Require the trailing ``len(trade_dates)`` trading days to match exactly.

    This is the production-canonical axis sanity gate: if the caller supplied
    a non-contiguous trading-day axis (e.g. weekend dates embedded) the
    adapter fails closed rather than silently extending / dropping.
    """
    D = len(trade_dates)
    if len(trading_days_sorted_asc) < D:
        raise ValueError(
            f"list_recent_trading_days returned only "
            f"{len(trading_days_sorted_asc)} days; need >= {D}"
        )
    suffix = trading_days_sorted_asc[-D:]
    if list(suffix) != list(trade_dates):
        raise ValueError(
            "trade_dates axis is not the trailing canonical A-market trading-day "
            f"window; suffix differs from caller-supplied axis starting at the "
            f"first position where suffix[i]={suffix[0]} != trade_dates[0]="
            f"{trade_dates[0]}"
        )


def _convert_db_close(value: Any) -> tuple[float, bool]:
    """Convert a DB close value to ``(float_value, is_finite)``.

    ``None`` / non-finite numeric → the ``is_finite=False`` branch writes
    ``NaN`` to the close matrix; this makes the downstream ``close_t`` /
    ``close_t1`` availability gates in ``compute_return_matrix`` identical
    to the "bar genuinely absent" path.
    """
    if value is None:
        return math.nan, False
    # Handles Decimal / int / float / numpy.
    try:
        fv = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"non-numeric close value: {value!r}") from exc
    if math.isfinite(fv):
        return fv, True
    return math.nan, False


async def _stream_close_matrix(
    session: AsyncSession,
    *,
    member_ids: Sequence[uuid.UUID],
    member_to_col: Mapping[uuid.UUID, int],
    required_bar_dates: Sequence[date],
    date_to_row: Mapping[date, int],
    stream_yield_per: int,
) -> tuple[np.ndarray, dict[str, int]]:
    """Stream close rows into a preallocated ``float64 [R, M]`` matrix.

    Uses the **exact 3-column** projection specified in M5-C1:
    ``BarDaily.instrument_id``, ``BarDaily.trade_date``, ``BarDaily.close``.

    Iteration uses ``await session.stream(...)`` with server-side
    ``yield_per`` — no ``.all()`` / ``list(...)`` / full scalar materialisation
    of the 3-column row set is ever performed.

    Duplicate cell write → fail closed.  This is stricter than the PK-only
    guarantee because the DB adapter might still emit a repeat row on an
    unexpected join / union; treating that as an adapter/data fault avoids a
    silent "last-write-wins" drift against the M5-B2 frozen-oracle contract.
    """
    R = len(required_bar_dates)
    M = len(member_ids)
    close = np.full((R, M), np.nan, dtype=np.float64)
    written = np.zeros((R, M), dtype=bool)

    rows_streamed = 0
    finite_close_cells = 0
    unavailable_close_rows = 0
    duplicate_streamed_cells = 0

    cols = (
        BarDaily.instrument_id,
        BarDaily.trade_date,
        BarDaily.close,
    )
    stmt = (
        select(*cols)
        .where(
            BarDaily.instrument_id.in_(list(member_ids)),
            BarDaily.trade_date.in_(list(required_bar_dates)),
        )
        .execution_options(yield_per=max(1, int(stream_yield_per)))
    )

    member_set = set(member_ids)
    date_set = set(required_bar_dates)

    result = await session.stream(stmt)
    async for row in result:
        rows_streamed += 1
        # Row is a 3-tuple of the projected columns in declared order.
        instrument_id, trade_date, raw_close = row  # type: ignore[misc]
        # The SQL WHERE already restricted membership; these two are belt-
        # and-braces so a broken query plan can't silently write off-grid.
        if instrument_id not in member_set:
            raise ValueError(
                f"adapter streamed instrument_id={instrument_id!r} outside the "
                "supplied member_ids"
            )
        if trade_date not in date_set:
            raise ValueError(
                f"adapter streamed trade_date={trade_date!r} outside the "
                "required_bar_dates set"
            )
        r = date_to_row[trade_date]  # type: ignore[index]
        c = member_to_col[instrument_id]  # type: ignore[index]
        if written[r, c]:
            duplicate_streamed_cells += 1
            raise ValueError(
                f"duplicate streamed cell (trade_date={trade_date}, "
                f"instrument_id={instrument_id}); adapter/PK violation.  "
                "Last-write-wins is forbidden here."
            )
        written[r, c] = True
        fv, finite = _convert_db_close(raw_close)
        if finite:
            close[r, c] = fv
            finite_close_cells += 1
        else:
            # NaN already preallocated; track unavailable evidence.
            unavailable_close_rows += 1

    total_cells = R * M
    missing_cells = total_cells - finite_close_cells - unavailable_close_rows
    mib = float(R * M * 8) / (1024.0 * 1024.0)
    stats = {
        "rows_streamed": rows_streamed,
        "finite_close_cells": finite_close_cells,
        "unavailable_close_rows": unavailable_close_rows,
        "duplicate_streamed_cells": duplicate_streamed_cells,
        "missing_cells": missing_cells,
        "R": R,
        "M": M,
        "close_matrix_mib": mib,
    }
    return close, stats


# ===========================================================================
# Public top-level entrypoint
# ===========================================================================
async def compute_current_static_historical_ew_batch(
    session: AsyncSession,
    scope_type: str,
    scope_keys: Sequence[str],
    trade_dates: Sequence[date],
    *,
    analysis_asof_date: date,
    stream_yield_per: int = 4096,
) -> HistoricalEWBatchResult:
    """Produce EW-series output for requested scopes via the canonical 3-owner chain.

    Data-flow summary
    -----------------
    (1) ``trade_dates`` axis validated + calendar suffix match via
        ``list_recent_trading_days(session, asof, len(trade_dates)+1)`` and
        canonical ``_build_t1_map``.
    (2) ``build_required_bar_axis`` to build the analysis-union-T1 index map.
    (3) ``resolve_current_memberships_batch`` → deterministic union
        ``member_ids`` (UUID-sorted) → ``build_scope_member_indices``.
    (4) ``_stream_close_matrix`` → 3-column SQL projection, true
        ``session.stream(yield_per=...)``, preallocated ``close[R, M]``.
    (5) ``compute_return_matrix`` + ``compute_scope_ew_matrix`` unchanged
        from M5-B1.
    (6) Fold EW matrix into per-scope ``HistoricalEWScopeResult`` tuples,
        release matrices, return small payload only.

    Returns:
        A ``HistoricalEWBatchResult`` that does NOT retain the close /
        return / membership matrices.  Only scope-level EW values and the
        source metrics are kept.
    """
    t_start = time.perf_counter()
    _log_rss("m5c-ew-start")

    # ------------------------------------------------------------------
    # 1. Input axis + canonical calendar suffix match + T1 map
    # ------------------------------------------------------------------
    if not scope_keys:
        raise ValueError("scope_keys must be non-empty")
    t_cal = time.perf_counter()
    trade_dates_norm = _normalize_analysis_dates(trade_dates, analysis_asof_date)
    D = len(trade_dates_norm)
    recent_days_desc = await list_recent_trading_days(
        session,
        end_date=analysis_asof_date,
        n=D + 1,
    )
    # ``list_recent_trading_days`` returns DESC ordered; ascending suffix.
    recent_days_asc = sorted(recent_days_desc)
    _require_calendar_suffix(trade_dates_norm, recent_days_asc)
    t1_by_date = _build_t1_map(trade_dates_norm, recent_days_asc)
    required_bar_dates, t_idx, t1_idx = build_required_bar_axis(
        trade_dates_norm,
        t1_by_date,
    )
    calendar_ms = int((time.perf_counter() - t_cal) * 1000)
    _log_rss(
        "m5c-ew-calendar",
        trade_date_count=len(trade_dates_norm),
        bar_rows=len(required_bar_dates),
    )

    # ------------------------------------------------------------------
    # 2. Current-static membership batch + deterministic union member_ids
    # ------------------------------------------------------------------
    t_mem = time.perf_counter()
    scope_keys_list = [str(k) for k in scope_keys]
    # Fail-closed: scope order in output matches the caller's order exactly.
    if len(scope_keys_list) != len(set(scope_keys_list)):
        raise ValueError("scope_keys must be strictly unique")
    memberships_raw: dict[str, CurrentStaticMembership] = (
        await resolve_current_memberships_batch(
            session,
            scope_type,
            scope_keys_list,
            asof_date=analysis_asof_date,
        )
    )
    # Preserve deterministic UUID ordering for member universe columns.
    union_set: set[uuid.UUID] = set()
    memberships_for_idx: dict[str, tuple[uuid.UUID, ...]] = {}
    scope_member_refs = 0
    for k in scope_keys_list:
        if k not in memberships_raw:
            raise ValueError(
                f"resolve_current_memberships_batch did not return scope_key={k}"
            )
        members = memberships_raw[k].member_ids
        memberships_for_idx[k] = members
        union_set.update(members)
        scope_member_refs += len(members)
    member_ids = sorted(union_set)  # UUIDs are stably comparable.
    member_to_col = {m: i for i, m in enumerate(member_ids)}
    scope_member_idx = build_scope_member_indices(member_ids, memberships_for_idx)
    membership_ms = int((time.perf_counter() - t_mem) * 1000)
    S = len(scope_keys_list)
    M = len(member_ids)
    _log_rss(
        "m5c-ew-membership-loaded",
        union_member_count=M,
        scope_count=S,
        member_refs_t=scope_member_refs,
    )

    # ------------------------------------------------------------------
    # 3. True SQL streaming: 3-column projection + yield_per + row loop.
    # ------------------------------------------------------------------
    t_bars = time.perf_counter()
    date_to_row = {d: i for i, d in enumerate(required_bar_dates)}
    close, bar_stats = await _stream_close_matrix(
        session,
        member_ids=member_ids,
        member_to_col=member_to_col,
        required_bar_dates=required_bar_dates,
        date_to_row=date_to_row,
        stream_yield_per=stream_yield_per,
    )
    bars_stream_ms = int((time.perf_counter() - t_bars) * 1000)
    _log_rss(
        "m5c-ew-close-loaded",
        bar_rows=bar_stats["rows_streamed"],
        union_member_count=M,
    )

    # ------------------------------------------------------------------
    # 4. M5-B1 columnar compute: return matrix → scope EW matrix.
    # ------------------------------------------------------------------
    t_ret = time.perf_counter()
    ret_bundle = compute_return_matrix(close, t_idx, t1_idx)
    return_1d = ret_bundle["return_1d"]
    # Explicitly drop the intermediate arrays as soon as they're no longer
    # needed so their memory is released before scope EW iteration.
    ret_bundle.pop("price_candidate", None)
    ret_bundle.pop("return_valid", None)
    return_ms = int((time.perf_counter() - t_ret) * 1000)
    _log_rss("m5c-ew-return-computed", union_member_count=M)

    t_ew = time.perf_counter()
    # Ordering: ``build_scope_member_indices`` preserves the insertion order
    # of ``memberships_for_idx``, which we iterated in caller-given
    # ``scope_keys_list`` order.  So scope_keys_out == scope_keys_list.
    scope_keys_out, ew_matrix = compute_scope_ew_matrix(
        return_1d, scope_member_idx
    )
    if list(scope_keys_out) != scope_keys_list:
        raise RuntimeError(
            "compute_scope_ew_matrix returned a scope iteration order that "
            "does not match the caller-supplied scope_keys deterministic order; "
            "build_scope_member_indices contract violation."
        )
    # Release return matrix explicitly before folding final results.
    del return_1d
    ew_ms = int((time.perf_counter() - t_ew) * 1000)
    _log_rss(
        "m5c-ew-scope-ew-computed",
        union_member_count=M,
        scope_count=S,
    )

    # ------------------------------------------------------------------
    # 5. Fold per-scope result + discard all transient arrays.
    # ------------------------------------------------------------------
    scope_results: list[HistoricalEWScopeResult] = []
    for s_idx, key in enumerate(scope_keys_list):
        col = ew_matrix[:, s_idx]
        values: list[float | None] = [None] * D
        for t in range(D):
            v = col[t]
            if np.isfinite(v):
                values[t] = float(v)
            else:
                values[t] = None
        membership = memberships_raw[key]
        scope_results.append(
            HistoricalEWScopeResult(
                scope_key=key,
                scope_name=membership.scope_name,
                member_count=membership.member_count,
                ew_values=tuple(values),
            )
        )
    # Explicit release of the only matrix we still hold a reference to.
    del ew_matrix
    del close

    total_ms = int((time.perf_counter() - t_start) * 1000)
    metrics = HistoricalEWSourceMetrics(
        calendar_ms=calendar_ms,
        membership_ms=membership_ms,
        bars_stream_ms=bars_stream_ms,
        return_ms=return_ms,
        ew_ms=ew_ms,
        total_ms=total_ms,
        rows_streamed=bar_stats["rows_streamed"],
        finite_close_cells=bar_stats["finite_close_cells"],
        unavailable_close_rows=bar_stats["unavailable_close_rows"],
        duplicate_streamed_cells=bar_stats["duplicate_streamed_cells"],
        missing_cells=bar_stats["missing_cells"],
        R=bar_stats["R"],
        M=bar_stats["M"],
        S=S,
        scope_member_refs=scope_member_refs,
        close_matrix_mib=bar_stats["close_matrix_mib"],
    )
    _log_rss("m5c-ew-end", scope_count=S)

    return HistoricalEWBatchResult(
        scope_type=scope_type,
        analysis_asof_date=analysis_asof_date,
        trade_dates=tuple(trade_dates_norm),
        scopes=tuple(scope_results),
        metrics=metrics,
    )


__all__ = [
    "HistoricalEWBatchResult",
    "HistoricalEWScopeResult",
    "HistoricalEWSourceMetrics",
    "compute_current_static_historical_ew_batch",
]
