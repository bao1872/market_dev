"""Historical Dynamics — Columnar EW Source Prototype (M5-B1).

Lean pure-NumPy columnar core that produces Historical-Dynamics-specific
``equal_weight_return`` primitive series for a fixed set of scopes.

Contract goals
==============

* Close matrix carries ``required_bar_dates`` (analysis dates union their exact
  T-1 dates), so the first analysis observation's T-1 is never silently dropped
  even when it lies outside the analysis axis.
* Return validity applies four semantic gates:
  ``finite close_t & finite close_t1 & abs(close_t1) > 1e-12 & finite(raw)``.
  This matches the **effective member-return contribution** that actually
  reaches the EW reducer — ``compute_scope_observation`` further wraps each
  scalar ``return_1d`` with ``_finite_or_none``, so a non-finite raw return
  (e.g. overflow → ``inf``) is treated as unavailable downstream even when
  the raw ``compute_exact_return`` intermediate is not ``None``.  Parity
  against the scalar oracle is therefore defined on
  ``finite(scalar_raw)`` / valid value identity, not on raw intermediate
  identity.
* Scope membership uses sparse int32 index vectors only (no dense [S, M] matrix).
* The EW reducer reuses the frozen canonical ``_return_distribution`` to keep
  the sorted-mean accumulation order identical to the scalar owner.
* ``ObservationSeries`` and ``Dynamics Phase`` are delegated entirely to the
  existing canonical owners — no math is re-implemented here.

Pure-unit gate
==============

This module is DB-free.  All inputs are explicit in-memory collections /
NumPy arrays.  The production loader that feeds these arrays from SQL / frozen
datasets belongs to M5-B2 onwards and must not leak into this module.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

import numpy as np

# ---------------------------------------------------------------------------
# Canonical owners — the ONLY external math this module may reference.
# ---------------------------------------------------------------------------
from app.domain.review.scope_observation import _return_distribution
from app.services.observation_prep import compute_exact_return
from app.domain.review.analysis.observation_series import build_observation_series
from app.domain.review.analysis.scope_dynamics import compute_scope_dynamics_analysis


# Public markers for benchmarks / callers that want to layer an external
# "unavailable → None" converter on top of the float64 NaN contract used
# inside the NumPy pipeline.
_UNAVAILABLE = np.nan


def _to_date(obj: Any) -> date:
    """Normalise ``YYYY-MM-DD`` / ``date`` / ISO ``str`` / numpy scalars to ``date``.

    Mirrors the tolerance inside ``build_observation_series`` without importing
    the private helper, so pure-unit tests can pass any reasonable date shape.
    """
    if isinstance(obj, date):
        return obj
    if hasattr(obj, "date"):
        # datetime / pandas Timestamp / np.datetime64-as-object via item()
        d = getattr(obj, "date", None)
        if callable(d):
            try:
                out = d()
                if isinstance(out, date):
                    return out
            except Exception:
                pass
    s = str(obj)
    if len(s) >= 10:
        s = s[:10]
    return date.fromisoformat(s)


# ===========================================================================
# 1. Required bar axis
# ===========================================================================
def build_required_bar_axis(
    analysis_dates: Sequence[Any],
    t1_by_date: Mapping[Any, Any | None],
) -> tuple[list[date], np.ndarray, np.ndarray]:
    """Expand the analysis axis to include every required exact bar date.

    T1-calendar fail-closed contract
    ---------------------------------

    ``t1_by_date`` is treated as the authoritative T-1 predecessor map owned
    by the calendar adapter.  Two hard gates are enforced here so that a
    loader/calendar bug cannot silently degrade a valid analysis return
    into an unavailable observation:

    (1) *Complete map.*  Every analysis date MUST be a key in the input map,
        even when the exact T-1 is known-absent.  For "no exact T-1 available
        for this analysis date" the explicit sentinel is ``d: None``.  A
        missing key is NOT equivalent to an explicit ``None`` — it raises.

    (2) *Strictly lower T1.*  Every non-``None`` T-1 value must be strictly
        earlier than the analysis date it belongs to.  ``t1 >= d`` (same-day
        or future-looking) raises ``ValueError``.  This mirrors the canonical
        production ``_build_t1_map()`` owner which always returns the
        strictly-lower trading-day predecessor; no local derivation or
        correction is attempted here.

    Args:
        analysis_dates: strictly ascending trading date axis.  Elements are
            ``date`` or anything ``str(·)`` of which parses as ISO ``YYYY-MM-DD``.
        t1_by_date: for every analysis date the exact-calendar previous trading
            date, or ``None`` when no exact T-1 is available.  Keys MUST cover
            all analysis dates exactly (see contract above).

    Returns:
        ``(required_bar_dates, t_idx, t1_idx)`` where

        * ``required_bar_dates[R]`` is the sorted union of analysis dates with
          every non-``None`` exact T-1 date, **not** assumed to be ``R == D+1``.
        * ``t_idx[D]`` maps each analysis date to its row in the close matrix.
        * ``t1_idx[D]`` likewise maps to the exact T-1 row; ``-1`` when no
          exact T-1 is available.

    Raises:
        ValueError: analysis_dates not strictly ascending; or any analysis
            date is missing from ``t1_by_date``; or any non-``None`` T-1 is
            not strictly earlier than its analysis date.
    """
    d_dates = [_to_date(d) for d in analysis_dates]
    for i in range(1, len(d_dates)):
        if not (d_dates[i - 1] < d_dates[i]):
            raise ValueError("analysis_dates must be strictly ascending unique")

    # Normalise t1 map to date keys.
    t1_by_date_norm: dict[date, date | None] = {}
    for k, v in t1_by_date.items():
        t1_by_date_norm[_to_date(k)] = None if v is None else _to_date(v)

    # (1) Fail-closed: EVERY analysis date MUST be explicitly keyed.
    for d in d_dates:
        if d not in t1_by_date_norm:
            raise ValueError(
                f"t1_by_date is missing an explicit entry for analysis date {d}; "
                "use `d: None` for dates whose exact T-1 is known-absent.  "
                "A missing key is NOT equivalent to an explicit None."
            )
    # (2) Fail-closed: strictly lower T1 (no same-day, no future).
    for d in d_dates:
        t1 = t1_by_date_norm[d]
        if t1 is None:
            continue
        if not (t1 < d):
            raise ValueError(
                f"exact T-1 for analysis date {d} must be strictly earlier; "
                f"got T-1 = {t1}.  The calendar adapter is the T-1 owner; do "
                "not try to derive or correct the predecessor locally."
            )

    # Build required set.
    required = set(d_dates)
    for d in d_dates:
        t1 = t1_by_date_norm[d]
        if t1 is not None:
            required.add(t1)
    required_bar_dates = sorted(required)
    required_idx = {d: i for i, d in enumerate(required_bar_dates)}

    t_idx = np.zeros(len(d_dates), dtype=np.int32)
    t1_idx = np.full(len(d_dates), fill_value=-1, dtype=np.int32)
    for i, d in enumerate(d_dates):
        if d not in required_idx:  # pragma: no cover — defensive
            raise KeyError(f"analysis date missing from required_bar_dates: {d}")
        t_idx[i] = required_idx[d]
        t1 = t1_by_date_norm[d]
        if t1 is not None:
            t1_idx[i] = required_idx[t1]

    return required_bar_dates, t_idx, t1_idx


# ===========================================================================
# 2. Return matrix
# ===========================================================================
def compute_return_matrix(
    close: np.ndarray,
    t_idx: np.ndarray,
    t1_idx: np.ndarray,
) -> dict[str, np.ndarray]:
    """Compute the exact member-level return matrix with canonical semantics.

    Args:
        close: ``float64 [R, M]`` close matrix.  Missing / unavailable bar data
            **must** be represented as ``NaN`` (inherited from the DB / loader
            owner that treats non-finite inputs as unavailable).
        t_idx: int32 [D] analysis→bar row index.
        t1_idx: int32 [D] analysis→exact T-1 bar row index.  ``-1`` when the
            exact T-1 bar is not available.

    Returns:
        Dict with three arrays, all ``shape (D, M)``:

        * ``price_candidate`` (bool) — ``finite(close_t)``.
        * ``return_valid`` (bool) — all four gates passed (close_t finite,
          close_t1 finite, ``abs(close_t1) > 1e-12``, and the raw division
          result itself is finite).
        * ``return_1d`` (float64) — ``close_t / close_t1 - 1`` when valid,
          else ``NaN``.
    """
    if close.dtype != np.float64:
        close = close.astype(np.float64, copy=False)
    t_idx_i = np.asarray(t_idx, dtype=np.int32)
    t1_idx_i = np.asarray(t1_idx, dtype=np.int32)

    R, M = close.shape
    D = t_idx_i.shape[0]
    if t1_idx_i.shape[0] != D:
        raise ValueError("t_idx and t1_idx must have the same length")
    if t_idx_i.size and (int(t_idx_i.min()) < 0 or int(t_idx_i.max()) >= R):
        raise IndexError("t_idx contains entry out of close row range")
    # t1_idx allows -1; we guard on the positive subset.
    t1pos = t1_idx_i[t1_idx_i >= 0]
    if t1pos.size and (int(t1pos.max()) >= R):
        raise IndexError("t1_idx contains positive entry out of close row range")

    close_t = close[t_idx_i]  # [D, M]
    # Build close_t1 with NaN-filled unavailable (-1) rows.
    t1_safe = np.where(t1_idx_i >= 0, t1_idx_i, 0)  # valid indexing placeholder
    close_t1_full = close[t1_safe]  # [D, M]
    # For rows where t1_idx was -1, overwrite entire M-slice to NaN.
    missing_t1 = t1_idx_i < 0
    if missing_t1.any():
        close_t1_full[missing_t1] = np.nan

    price_candidate = np.isfinite(close_t)
    close_t1_finite = np.isfinite(close_t1_full)

    base_valid = price_candidate & close_t1_finite & (np.abs(close_t1_full) > 1e-12)

    # Compute raw return.  Suppress expected divide / invalid / overflow
    # warnings because all three outcomes are handled explicitly by the
    # validity mask below — overflow is an intentional "unavailable" path.
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        raw_return = close_t / close_t1_full - 1.0

    finite_raw = np.isfinite(raw_return)
    return_valid = base_valid & finite_raw

    return_1d = np.full((D, M), np.nan, dtype=np.float64)
    return_1d[return_valid] = raw_return[return_valid]

    return {
        "price_candidate": price_candidate,
        "return_valid": return_valid,
        "return_1d": return_1d,
    }


# ===========================================================================
# 3. Sparse scope membership
# ===========================================================================
def build_scope_member_indices(
    member_ids: Sequence[Any],
    memberships: Mapping[Any, Sequence[Any]],
) -> dict[Any, np.ndarray]:
    """Build deterministic sparse int32 member-column indices per scope.

    Fail-closed contract
    --------------------

    A scope membership that references a member **not** present in
    ``member_ids`` is treated as a composition error and raises
    ``ValueError``.  Silent dropping is intentionally forbidden because
    it would quietly change the EW denominator (and every downstream
    scope-level fact) without any alert — this is the classic
    "fast but subtly wrong" performance-path failure mode.

    Duplicate members within a single scope membership are
    deterministically deduplicated to the first occurrence (this matches
    the scalar scope-observation owner which deduplicates PIT members
    before building per-member observations).

    Args:
        member_ids: sequence of unique member identifiers in a deterministic
            order — this order defines the column index of the columnar cube.
        memberships: scope_key → ordered iterable of member_ids.

    Returns:
        ``{scope_key: np.ndarray[int32]}``.  A scope whose (deduped)
        membership list is genuinely empty after fail-closed resolution
        yields a zero-length int32 array.

    Raises:
        ValueError: any scope references a member id that is not present
            in ``member_ids``; or ``member_ids`` is not strictly unique.
    """
    member_to_col: dict[Any, int] = {}
    seen: set[Any] = set()
    for i, m in enumerate(member_ids):
        if m in seen:
            raise ValueError("member_ids must be strictly unique")
        seen.add(m)
        member_to_col[m] = i

    scope_idx: dict[Any, np.ndarray] = {}
    for scope_key, members in memberships.items():
        cols: list[int] = []
        used_scope: set[Any] = set()
        for m in members:
            if m in used_scope:
                continue
            used_scope.add(m)
            c = member_to_col.get(m)
            if c is None:
                raise ValueError(
                    f"scope {scope_key!r} references unknown member {m!r}; "
                    "membership must resolve against the same member_ids "
                    "used to build the columnar cube."
                )
            cols.append(c)
        scope_idx[scope_key] = np.asarray(cols, dtype=np.int32)
    return scope_idx


# ===========================================================================
# 4. Scope equal_weight_return matrix via canonical reducer
# ===========================================================================
def compute_scope_ew_matrix(
    return_1d: np.ndarray,
    scope_member_idx: Mapping[Any, np.ndarray],
) -> tuple[list[Any], np.ndarray]:
    """Compute scope×date equal_weight_return through the canonical reducer.

    For every ``(date, scope)`` pair we extract the finite member returns,
    then delegate the sorted-mean calculation to ``_return_distribution``
    so that floating-point accumulation order matches the scalar oracle.

    Args:
        return_1d: float64 [D, M] matrix with NaN for unavailable returns.
        scope_member_idx: scope_key → int32 column indices.

    Returns:
        ``(scope_keys, ew_matrix)`` where

        * ``scope_keys`` preserves the deterministic iteration order of the
          input ``scope_member_idx`` mapping.
        * ``ew_matrix`` is float64 [D, S] with NaN on unavailable (including
          empty-universe) cells.
    """
    r1d = np.asarray(return_1d, dtype=np.float64)
    if r1d.ndim != 2:
        raise ValueError("return_1d must be a 2D matrix [D, M]")
    D, _ = r1d.shape

    scope_keys = list(scope_member_idx.keys())
    S = len(scope_keys)
    ew_matrix = np.full((D, S), np.nan, dtype=np.float64)

    for s_idx, key in enumerate(scope_keys):
        cols = np.asarray(scope_member_idx[key], dtype=np.int32)
        if cols.size == 0:
            continue
        scope_returns = r1d[:, cols]  # [D, n_members]
        for t in range(D):
            row = scope_returns[t]
            # Strictly the same filter as scope_observation: only finite
            # return_1d values enter the distribution.  price_candidate was
            # already the precondition encoded inside return_valid so the
            # NaN mask on return_1d captures the full EW universe gate.
            values = row[np.isfinite(row)]
            if values.size == 0:
                continue
            mean = _return_distribution(values.tolist())["mean"]
            ew_matrix[t, s_idx] = float(mean)

    return scope_keys, ew_matrix


# ===========================================================================
# 5. Minimal snapshot → ObservationSeries → canonical Dynamics
# ===========================================================================
def build_scope_dynamics_from_ew(
    *,
    scope_type: str,
    scope_key: Any,
    analysis_dates: Sequence[Any],
    ew_values: Sequence[float | None],
) -> dict[str, Any]:
    """Build a one-scope dynamics chain through the canonical owners.

    This helper is intentionally per-scope (matches Dynamics' single-scope
    owner), keeping the S-batched loop in the caller.  Benchmark / parity
    harnesses call this once per scope and compare the returned structured
    dicts field-by-field.

    Args:
        scope_type: canonical scope type string forwarded to the builder.
        scope_key: canonical scope key.
        analysis_dates: strictly ascending unique trading dates.  Must match
            the ``D`` axis that produced ``ew_values``.
        ew_values: iterable of length ``D``.  ``None`` or non-finite floats
            are treated as unavailable and rendered as ``None`` inside the
            snapshot payload.

    Returns:
        ``{"observation_series": ..., "dynamics": ...}``.
    """
    d_dates = [_to_date(d) for d in analysis_dates]
    D = len(d_dates)
    values_list = list(ew_values)
    if len(values_list) != D:
        raise ValueError("ew_values length must match analysis_dates length")

    snapshots: list[dict[str, Any]] = []
    for d, raw_val in zip(d_dates, values_list):
        if raw_val is None:
            snap_val: float | None = None
        else:
            fv = float(raw_val)
            snap_val = fv if np.isfinite(fv) else None
        snapshots.append(
            {
                "trade_date": d.isoformat(),
                "readiness": "ready",
                "payload": {
                    "price": {
                        "equal_weight_return": snap_val,
                    }
                },
            }
        )

    observation_series = build_observation_series(
        scope_type=scope_type,
        scope_key=str(scope_key),
        from_date=d_dates[0],
        to_date=d_dates[-1],
        trading_dates=d_dates,
        snapshot_series=snapshots,
        primitive_keys=["equal_weight_return"],
    )
    dynamics = compute_scope_dynamics_analysis(observation_series)
    return {
        "observation_series": observation_series,
        "dynamics": dynamics,
    }


__all__ = [
    "build_required_bar_axis",
    "compute_return_matrix",
    "build_scope_member_indices",
    "compute_scope_ew_matrix",
    "build_scope_dynamics_from_ew",
    "compute_exact_return",
    "_return_distribution",
]
