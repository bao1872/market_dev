"""M5-B2 Frozen-dataset benchmark for M5-B1 Columnar EW core.

Scope
=====

Runs only on the frozen ``review-source-c5c686e-v1`` dataset against the
``industry_l1`` scope family using **only** the 5 allowed raw inputs.
Answers:

1. Does the columnar EW source reproduce the canonical scalar mathematical
   oracle at all 4 comparison layers?
2. What are the real RSS and CPU costs for this 31-scope × 120-day world?

Harness only — not a production loader.  See §FORBIDDEN_INPUTS; any loader,
prep, union context, or runtime code outside the M5-B1 columnar core and
the canonical ObservationSeries / Dynamics owners is strictly off-limits.

Run from inside the ``backend`` directory::

    PURE_UNIT_TEST=1 .venv/bin/python scripts/review_historical_ew_columnar_benchmark.py
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import resource
import statistics
import sys
import time
from bisect import bisect_left
from collections import OrderedDict
from datetime import date
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Memory sampler — prefer Linux /proc/self/status, macOS falls back via
# resource.getrusage ru_maxrss (peak only) + optional psutil.
# ---------------------------------------------------------------------------

_PROC_STATUS = "/proc/self/status"


def current_rss_kb() -> int | None:
    """Return current-process RSS in kB, or ``None`` when unavailable."""
    if os.path.exists(_PROC_STATUS):
        with open(_PROC_STATUS, "r") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1])
    # psutil fallback for macOS.
    try:
        import psutil  # type: ignore
    except Exception:  # pragma: no cover — platform dependent
        return None
    try:
        return int(psutil.Process().memory_info().rss / 1024)
    except Exception:  # pragma: no cover
        return None


def peak_rss_kb() -> int:
    """Best-effort peak RSS in kB using the process ru_maxrss facility."""
    rusage = resource.getrusage(resource.RUSAGE_SELF)
    kb = getattr(rusage, "ru_maxrss", 0)
    # On macOS ru_maxrss is in bytes; on Linux in kB.  Linux can also be read
    # from /proc/self/status VmHWM; cross-check that when present.
    if os.path.exists(_PROC_STATUS):
        try:
            with open(_PROC_STATUS, "r") as fh:
                for line in fh:
                    if line.startswith("VmHWM:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            return int(parts[1])
        except Exception:
            pass
    if sys.platform == "darwin":
        return int(kb / 1024)
    return int(kb)


def _to_date(x: Any) -> date:
    if isinstance(x, date):
        return x
    s = str(x)
    if len(s) >= 10:
        s = s[:10]
    return date.fromisoformat(s)


# ---------------------------------------------------------------------------
# Marker bookkeeping.
# ---------------------------------------------------------------------------

MARKERS: list[dict[str, Any]] = []
_TIMES: dict[str, float] = {}
BASELINE_RSS_KB: int | None = None


def marker(name: str) -> None:
    rss = current_rss_kb()
    peak = peak_rss_kb()
    t = time.perf_counter()
    MARKERS.append(
        {"name": name, "t_s": t, "rss_kb": rss, "peak_kb": peak}
    )
    print(
        f"[marker] {name:<32s} "
        f"rss={rss if rss is not None else '?':>7} kB  "
        f"peak={peak:>7} kB  ",
        flush=True,
    )


def timing_start(name: str) -> None:
    _TIMES[name + "_s"] = time.perf_counter()


def timing_stop(name: str) -> float:
    end = time.perf_counter()
    start = _TIMES.pop(name + "_s", None)
    if start is None:  # pragma: no cover
        raise KeyError(f"timing_start({name!r}) was not called")
    elapsed = end - start
    _TIMES[name + "_elapsed"] = elapsed
    print(f"[cpu]    {name:<32s} {elapsed:8.3f} s", flush=True)
    return elapsed


# ---------------------------------------------------------------------------
# 1. Dataset identity.
# ---------------------------------------------------------------------------

DEFAULT_DATASET_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".perfdata",
    "review",
    "review-source-c5c686e-v1",
)


def step1_dataset_identity(dataset_path: str) -> tuple[str, list[date]]:
    marker("benchmark-start")
    timing_start("identity")
    manifest_path = os.path.join(dataset_path, "manifest.json")
    with open(manifest_path, "r") as fh:
        manifest = json.load(fh)
    asof = _to_date(manifest["analysis_asof_date"])
    axis = [_to_date(s) for s in manifest["date_ranges"]["analysis_axis"]]
    if len(axis) != len(set(axis)):
        raise ValueError("manifest analysis_axis contains duplicate dates")
    for i in range(1, len(axis)):
        if not (axis[i - 1] < axis[i]):
            raise ValueError("manifest analysis_axis not strictly ascending")
    if axis[-1] != asof:
        raise ValueError(
            f"manifest last analysis date {axis[-1]} != "
            f"analysis_asof_date {asof}"
        )
    timing_stop("identity")
    marker("after-dataset-identity")
    header = {
        "dataset_path": dataset_path,
        "analysis_asof_date": asof.isoformat(),
        "analysis_date_count": len(axis),
        "first_date": axis[0].isoformat(),
        "last_date": axis[-1].isoformat(),
    }
    print("\n=== DATASET IDENTITY ===")
    for k, v in header.items():
        print(f"  {k}: {v}")
    return dataset_path, axis


# ---------------------------------------------------------------------------
# 2. Calendar + exact T1 map.
# ---------------------------------------------------------------------------

def step2_calendar(dataset_path: str, analysis_dates: list[date]):
    timing_start("calendar")
    cal_path = os.path.join(dataset_path, "raw", "trading_calendar.jsonl.gz")
    trading: list[date] = []
    with gzip.open(cal_path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("market") != "A":
                continue
            if not obj.get("is_trading_day", False):
                continue
            trading.append(_to_date(obj["trade_date"]))
    trading.sort()
    # Build exact T1: strictly-lower A-market predecessor for each d.
    t1_by_date: dict[date, date | None] = {}
    for d in analysis_dates:
        idx = bisect_left(trading, d)
        # idx points to d in trading[] if present; predecessor is idx-1.
        # Per _build_t1_map() semantics we index strictly-lower regardless
        # of whether d itself is a trading day (analysis dates already are).
        if idx <= 0:
            # No strict predecessor in the calendar at all.
            t1_by_date[d] = None
        else:
            cand = trading[idx - 1]
            # Extra sanity against weird calendars.
            if cand >= d:
                raise ValueError(
                    f"calendar T1 for {d} resolved to {cand} (not strictly "
                    f"lower).  Trading-calendar owner must be checked."
                )
            t1_by_date[d] = cand
    timing_stop("calendar")
    marker("after-calendar")
    t1_nones = sum(1 for v in t1_by_date.values() if v is None)
    non_nones = [(d, v) for d, v in t1_by_date.items() if v is not None]
    first_d, first_t1 = non_nones[0]
    last_d, last_t1 = non_nones[-1]
    print("\n=== CALENDAR / T1 ===")
    print(f"  A-market trading days in file: {len(trading)}")
    print(f"  explicit None T1 slots:        {t1_nones}")
    print(f"  first analysis / first T1:     {first_d} / {first_t1}")
    print(f"  last analysis / last T1:       {last_d} / {last_t1}")
    return t1_by_date, trading


# ---------------------------------------------------------------------------
# 3. Scope universe (active industry_l1).
# ---------------------------------------------------------------------------

def step3_scopes(dataset_path: str):
    timing_start("boards")
    path = os.path.join(dataset_path, "raw", "boards.jsonl.gz")
    # Frozen boards use type=="industry" + hierarchy_level=="L1" + is_active.
    # Also accept legacy "industry_l1" defensively; but fail closed when
    # the canonical convention disagrees.
    scopes: list[dict[str, Any]] = []
    with gzip.open(path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            b = json.loads(line)
            if not b.get("is_active", False):
                continue
            t = b.get("type")
            hl = b.get("hierarchy_level")
            if (t, hl) != ("industry", "L1"):
                continue
            scopes.append(b)
    # Deterministic iteration order by stable board id (UUID) ascending so
    # that scope[S] mapping is reproducible across runs.
    scopes.sort(key=lambda s: s["id"])
    timing_stop("boards")
    print("\n=== INDUSTRY_L1 SCOPES ===")
    print(f"  active industry_l1 scope count: {len(scopes)}")
    scope_ids = [s["id"] for s in scopes]
    scope_names = [s.get("name", s["id"]) for s in scopes]
    for sid, name in zip(scope_ids, scope_names):
        print(f"    - {sid}  {name}")
    return scopes, scope_ids


# ---------------------------------------------------------------------------
# 3b. Build memberships from snapshot.
# ---------------------------------------------------------------------------

def step3b_memberships(
    dataset_path: str, scope_ids: list[str]
):
    timing_start("memberships")
    path = os.path.join(dataset_path, "raw", "board_memberships_current_snapshot.jsonl.gz")
    board_set = set(scope_ids)
    # OrderedDict preserves first-seen order per scope to match scalar
    # dedup semantics (M5-B1 collapses duplicate members to first occurrence).
    memberships_raw: dict[str, "OrderedDict[Any, None]"] = {
        sid: OrderedDict() for sid in scope_ids
    }
    raw_selected_rows = 0
    raw_duplicate_rows = 0
    refs = 0
    with gzip.open(path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            bid = row["board_id"]
            if bid not in board_set:
                continue
            raw_selected_rows += 1
            inst = row["instrument_id"]
            bucket = memberships_raw[bid]
            if inst in bucket:
                # Ignore duplicates (same semantics as M5-B1's per-scope dedup).
                raw_duplicate_rows += 1
                continue
            bucket[inst] = None
            refs += 1
    # Convert to plain lists.
    memberships: dict[str, list[Any]] = {
        sid: list(od.keys()) for sid, od in memberships_raw.items()
    }
    counts = sorted(len(v) for v in memberships.values())
    def _pct(data, p):
        if not data:
            return 0
        k = (len(data) - 1) * p
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return data[f]
        return data[f] + (data[c] - data[f]) * (k - f)
    print(f"  min/p50/p95/max members:       "
          f"{counts[0] if counts else 0}/"
          f"{_pct(counts, 0.5):.0f}/"
          f"{_pct(counts, 0.95):.0f}/"
          f"{counts[-1] if counts else 0}")
    print(f"  raw selected membership rows:  {raw_selected_rows}")
    print(f"  raw duplicate membership rows: {raw_duplicate_rows}")
    print(f"  deduped membership refs:       {refs}")
    timing_stop("memberships")
    marker("after-membership")
    return memberships, refs, counts, raw_selected_rows, raw_duplicate_rows


# ---------------------------------------------------------------------------
# 4. Member universe + scope indices.
# ---------------------------------------------------------------------------

def step4_member_universe(memberships: dict[str, list[Any]]):
    timing_start("member_union")
    seen: set[Any] = set()
    order: list[Any] = []
    total_refs = 0
    for _sid, members in memberships.items():
        total_refs += len(members)
        for m in members:
            if m not in seen:
                seen.add(m)
                order.append(m)
    # Deterministic sort: the stable instrument UUID (strings).  We don't
    # depend on scope iteration order for union-member column indices.
    order.sort()
    member_ids = order
    M = len(member_ids)
    dup_ratio = total_refs / M if M else 0.0
    # Columnar membership → int32 sparse indices, fail-closed on unknown.
    from app.services.review_historical_ew_columnar import (
        build_scope_member_indices,
    )
    scope_member_idx = build_scope_member_indices(member_ids, memberships)
    timing_stop("member_union")
    print("\n=== MEMBER UNIVERSE ===")
    print(f"  union member count M:           {M}")
    print(f"  total scope-member refs:        {total_refs}")
    print(f"  duplication ratio (refs/M):     {dup_ratio:.2f}")
    return member_ids, scope_member_idx, total_refs


# ---------------------------------------------------------------------------
# 5. Close matrix loader (stream + fail-closed on conflicts).
# ---------------------------------------------------------------------------

def step5_close_matrix(
    dataset_path: str,
    required_bar_dates: list[date],
    member_ids: list[Any],
):
    timing_start("bars_scan")
    path = os.path.join(dataset_path, "raw", "bars_daily.jsonl.gz")
    R = len(required_bar_dates)
    M = len(member_ids)
    date_to_row = {d: i for i, d in enumerate(required_bar_dates)}
    member_to_col = {m: i for i, m in enumerate(member_ids)}
    date_set = set(required_bar_dates)
    member_set = set(member_ids)
    close = np.full((R, M), np.nan, dtype=np.float64)
    rows_scanned = 0
    rows_selected = 0
    finite_close_count = 0
    duplicate_exact = 0
    duplicate_conflict = 0
    selected_raw_unavailable_close_rows = 0

    # ------------------------------------------------------------------
    # Frozen-adapter integrity gate: deterministic sample of selected
    # raw bar records, sampled as first 100 + last 100 + hash-selected.
    # After streaming we verify each sampled raw record independently
    # against the matrix's (row, col) mapping.
    #
    # We do NOT construct a second full 639k-cell Python-dict oracle;
    # the sample is strictly for adapter-proof (row/col did not drift,
    # column/row ordering did not shift, matrix was written correctly
    # from the raw record inputs we actually ingested).
    #
    # Hash rule: sha256(date|inst|selected_ordinal) masked with a
    # 10-bit mask → accept ~1 in every 1024 selected rows.  On a
    # ~634k-selected frozen world this yields ~620 hash hits; combined
    # with first+last buffers gives ~820 total sampled checks.  This
    # is a deterministic adapter-proof, not a statistics sample.
    # ------------------------------------------------------------------
    SAMPLE_FIRST_N = 100
    SAMPLE_LAST_N = 100
    SAMPLE_HASH_MASK = (1 << 10) - 1  # ~1/1024 selected rows
    first_selected_samples: list[tuple[date, Any, Any]] = []
    last_selected_samples: list[tuple[date, Any, Any]] = []
    hash_selected_samples: list[tuple[date, Any, Any]] = []

    # Explicit unseen sentinel — None is a valid first-seen normalized
    # state ("unavailable close"), so `seen.get(key) is None` can NOT be
    # used to decide whether a cell has already been occupied.
    _UNSET: Any = object()
    # seen[(r,c)] stores the NORMALIZED first-occurrence close for every
    # selected (date, instrument):
    #   None                       → raw close was missing/None (unavailable)
    #   float (finite or nonfinite) → numeric raw close as float
    seen: dict[tuple[int, int], Any] = {}
    with gzip.open(path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows_scanned += 1
            row = json.loads(line)
            inst = row["instrument_id"]
            if inst not in member_set:
                continue
            td = _to_date(row["trade_date"])
            if td not in date_set:
                continue
            r = date_to_row[td]
            c = member_to_col[inst]
            raw = row.get("close")
            # --- Normalize raw close per §BAR DUPLICATE OWNER semantics.
            if raw is None:
                normalized: Any = None
            else:
                normalized = float(raw)
            key = (r, c)
            prior = seen.get(key, _UNSET)
            if prior is _UNSET:
                # FIRST occurrence → record normalized (including unavailable),
                # write matrix, count selected row.
                seen[key] = normalized
                rows_selected += 1
                if normalized is None:
                    selected_raw_unavailable_close_rows += 1
                    # matrix cell remains NaN (preallocated state).
                else:
                    fval = float(normalized)
                    if math.isfinite(fval):
                        close[r, c] = fval
                        finite_close_count += 1
                    else:
                        # non-finite numeric → still NaN in the matrix
                        # (matches compute_exact_return's availability gate).
                        pass
                # Sample item uses the SAME raw semantics as the bar-proof
                # oracle: None = unavailable; else numeric.
                sample_item: tuple[date, Any, Any] | None
                if normalized is None:
                    sample_item = (td, inst, None)
                else:
                    sample_item = (td, inst, normalized)
            else:
                # DUPLICATE — compare normalized vs prior.  Exact equality
                # is allowed (dataset has identical repeat rows); any
                # semantic difference is a conflict that MUST raise
                # (no last-write-wins, no silent paper-over).
                sample_item = None
                if normalized is None and prior is None:
                    duplicate_exact += 1
                elif normalized is None or prior is None:
                    # unavailable ↔ finite/nonfinite: semantic mismatch.
                    duplicate_conflict += 1
                else:
                    a = float(prior)
                    b = float(normalized)
                    # Two NaNs or ±same-sign infs: treat as exact repeat.
                    both_nan = math.isnan(a) and math.isnan(b)
                    both_inf = math.isinf(a) and math.isinf(b) and (a > 0) == (b > 0)
                    both_finite_eq = (
                        math.isfinite(a) and math.isfinite(b) and a == b
                    )
                    if both_nan or both_inf or both_finite_eq:
                        duplicate_exact += 1
                    else:
                        duplicate_conflict += 1
                if duplicate_conflict > 0:
                    raise ValueError(
                        f"duplicate conflicting bar close at (trade_date={td}, "
                        f"instrument_id={inst}); prior={prior!r} new={normalized!r}.  "
                        f"Dataset owner must be audited; last-write-wins is "
                        f"forbidden here."
                    )

            if sample_item is not None:
                sel_idx = rows_selected - 1
                if len(first_selected_samples) < SAMPLE_FIRST_N:
                    first_selected_samples.append(sample_item)
                # Rotating last-N buffer.
                if len(last_selected_samples) >= SAMPLE_LAST_N:
                    last_selected_samples.pop(0)
                last_selected_samples.append(sample_item)
                # Hash-selected stable subset.
                h = hashlib.sha256(
                    f"{td.isoformat()}\n{inst}\n{sel_idx}".encode("utf-8")
                ).digest()
                h_int = int.from_bytes(h[:4], "little")
                if (h_int & SAMPLE_HASH_MASK) == 0:
                    hash_selected_samples.append(sample_item)

    # Collapse to an ordered de-duplicated-by-tuple list (a given (d,m) may
    # appear both in first-N and in hash-set).
    merged_unique: "OrderedDict[tuple[date, Any, Any], None]" = OrderedDict()
    for s in first_selected_samples:
        merged_unique[s] = None
    for s in last_selected_samples:
        merged_unique[s] = None
    for s in hash_selected_samples:
        merged_unique[s] = None
    adapter_samples = list(merged_unique.keys())

    # --- Adapter proof: every sampled raw record must equal the matrix.
    adapter_bar_sample_mismatches = 0
    for (sd, sm, sraw) in adapter_samples:
        r = date_to_row[sd]
        c = member_to_col[sm]
        cell = close[r, c]
        if sraw is None:
            if not math.isnan(cell):
                adapter_bar_sample_mismatches += 1
        else:
            sraw_f = float(sraw)
            if math.isnan(sraw_f):
                if not math.isnan(cell):
                    adapter_bar_sample_mismatches += 1
            else:
                if not (math.isfinite(cell) and float(cell) == sraw_f):
                    adapter_bar_sample_mismatches += 1

    missing_cells = int(np.isnan(close).sum())
    matrix_bytes = close.nbytes
    # IMPORTANT: capture bars elapsed BEFORE using it to compute rows/s;
    # the previous bug computed rows_per_second against a dict key that
    # hadn't been written yet (yielding divide-by-near-zero → ~1e15).
    timing_stop("bars_scan")
    bars_elapsed = max(_TIMES.get("bars_scan_elapsed", 0.0), 1e-9)
    rows_per_sec = rows_scanned / bars_elapsed
    marker("after-close-matrix")
    print("\n=== CLOSE MATRIX ===")
    print(f"  rows scanned:                   {rows_scanned:,}")
    print(f"  rows selected:                  {rows_selected:,}")
    print(f"  rows scanned/s:                 {rows_per_sec:,.0f}")
    print(f"  finite close cells:             {finite_close_count:,}")
    print(f"  missing (NaN) cells:            {missing_cells:,}")
    print(f"  raw unavailable close rows:     {selected_raw_unavailable_close_rows}")
    print(f"  duplicate exact bars:           {duplicate_exact}")
    print(f"  duplicate conflict bars:        {duplicate_conflict}")
    print(f"  close[{R},{M}] size MiB:        {matrix_bytes / (1024*1024):.1f}")
    print("\n=== FROZEN ADAPTER INTEGRITY GATE ===")
    print(f"  bar sample size (first+last+hash): {len(adapter_samples)}")
    print(f"    first N kept:                    {len(first_selected_samples)}")
    print(f"    last N kept:                     {len(last_selected_samples)}")
    print(f"    hash hits kept:                  {len(hash_selected_samples)}")
    print(f"  adapter_bar_sample_mismatches:     {adapter_bar_sample_mismatches}")
    return close, {
        "rows_scanned": rows_scanned,
        "rows_selected": rows_selected,
        "rows_per_second": rows_per_sec,
        "finite_close_count": finite_close_count,
        "missing_cells": missing_cells,
        "selected_raw_unavailable_close_rows": selected_raw_unavailable_close_rows,
        "duplicate_exact": duplicate_exact,
        "duplicate_conflict": duplicate_conflict,
        "matrix_size_mib": matrix_bytes / (1024 * 1024),
        "adapter_bar_sample_size": len(adapter_samples),
        "adapter_bar_sample_first": len(first_selected_samples),
        "adapter_bar_sample_last": len(last_selected_samples),
        "adapter_bar_sample_hash": len(hash_selected_samples),
        "adapter_bar_sample_mismatches": adapter_bar_sample_mismatches,
    }


# ---------------------------------------------------------------------------
# 6+7. Columnar path (unchanged M5-B1 core) + scalar mathematical oracle.
# ---------------------------------------------------------------------------

def step6_columnar_path(close, t_idx, t1_idx, scope_member_idx):
    timing_start("return_matrix")
    from app.services.review_historical_ew_columnar import (
        compute_return_matrix,
        compute_scope_ew_matrix,
    )
    r_out = compute_return_matrix(close, t_idx, t1_idx)
    timing_stop("return_matrix")
    marker("after-return-matrix")

    timing_start("ew_columnar")
    scope_keys, ew_col = compute_scope_ew_matrix(
        r_out["return_1d"], scope_member_idx
    )
    timing_stop("ew_columnar")
    marker("after-ew-matrix")
    return r_out, scope_keys, ew_col


def step7_scalar_ew_oracle(
    close: np.ndarray,
    t_idx: np.ndarray,
    t1_idx: np.ndarray,
    scope_keys: list[Any],
    scope_member_idx: dict[Any, np.ndarray],
):
    """Scalar oracle built directly from the SAME close matrix.

    For every (scope × date) pair we compute the EW mean through the
    canonical ``_return_distribution`` reducer over finite
    ``compute_exact_return(close_t, close_t1)`` values.  This is the
    mathematical oracle used for parity; no MemberObservation is rebuilt.
    """
    timing_start("ew_scalar_oracle")
    from app.services.observation_prep import compute_exact_return
    from app.domain.review.scope_observation import _return_distribution

    D = t_idx.shape[0]
    S = len(scope_keys)
    ew_sc = np.full((D, S), np.nan, dtype=np.float64)

    # Also build per (D, M) effective semantics for Layer 1 parity later.
    # price_candidate = finite(close_t) on the analysis row for that member.
    # scalar_available = finite(scalar_raw).  scalar_value = scalar_raw or NaN.
    M = close.shape[1]
    scalar_price = np.zeros((D, M), dtype=bool)
    scalar_value = np.full((D, M), np.nan, dtype=np.float64)
    scalar_valid = np.zeros((D, M), dtype=bool)

    # Pre-extract T1 row index for fast access.
    t_idx_arr = np.asarray(t_idx, dtype=np.int32)
    t1_idx_arr = np.asarray(t1_idx, dtype=np.int32)

    for di in range(D):
        r_t = int(t_idx_arr[di])
        r_t1 = int(t1_idx_arr[di])
        close_t_row = close[r_t]
        close_t1_row = close[r_t1] if r_t1 >= 0 else None
        for m in range(M):
            ct = float(close_t_row[m])
            ct_finite = math.isfinite(ct)
            scalar_price[di, m] = ct_finite
            if close_t1_row is None:
                ct1 = None
            else:
                raw = float(close_t1_row[m])
                ct1 = raw if math.isfinite(raw) else None
            ct_clean = ct if ct_finite else None
            raw_ret = compute_exact_return(ct_clean, ct1)
            # Effective contribution gate (matches _finite_or_none wrap
            # that compute_scope_observation applies to scalar return_1d).
            if raw_ret is not None and math.isfinite(float(raw_ret)):
                scalar_value[di, m] = float(raw_ret)
                scalar_valid[di, m] = True
            else:
                # scalar_value stays NaN.
                pass

    # For each (scope, date) run the canonical reducer.
    for si, sk in enumerate(scope_keys):
        cols = np.asarray(scope_member_idx[sk], dtype=np.int32)
        if cols.size == 0:
            continue
        for di in range(D):
            vals: list[float] = []
            for mc in cols.tolist():
                v = float(scalar_value[di, mc])
                if math.isfinite(v):
                    vals.append(v)
            if not vals:
                continue
            mean = _return_distribution(vals)["mean"]
            ew_sc[di, si] = float(mean)

    timing_stop("ew_scalar_oracle")
    marker("after-scalar-ew-oracle")
    return ew_sc, scalar_price, scalar_value, scalar_valid


# ---------------------------------------------------------------------------
# 8–11. Parity layers.
# ---------------------------------------------------------------------------

def step8_layer1_parity(
    r_out: dict[str, np.ndarray],
    scalar_price: np.ndarray,
    scalar_value: np.ndarray,
    scalar_valid: np.ndarray,
) -> dict[str, int]:
    D, M = scalar_price.shape
    pairs = D * M
    # price_candidate: columnar bool mask == scalar finite(close_t).
    col_pc = np.asarray(r_out["price_candidate"], dtype=bool)
    col_valid = np.asarray(r_out["return_valid"], dtype=bool)
    col_value = np.asarray(r_out["return_1d"], dtype=np.float64)

    cand_mm = int(np.sum(col_pc != scalar_price))
    val_mm = int(np.sum(col_valid != scalar_valid))

    # Exact value parity on the intersection of both sides' valid cells.
    common_valid = col_valid & scalar_valid
    if np.any(common_valid):
        col_v = col_value[common_valid]
        scl_v = scalar_value[common_valid]
        value_mm = int(np.sum(col_v != scl_v))
    else:
        value_mm = 0
    # Additionally: cells where columnar claims invalid but scalar value has
    # a finite slot, OR vice versa, were already counted by val_mm.  As a
    # consistency check, on all scalar-valid cells, columnar must match.
    print("\n=== LAYER 1 — MEMBER PARITY ===")
    print(f"  (date, member) pairs:           {pairs:,}")
    print(f"  price_candidate mismatches:     {cand_mm}")
    print(f"  return_validity mismatches:     {val_mm}")
    print(f"  return_value mismatches (exact):{value_mm}")
    return {
        "member_pairs": pairs,
        "candidate_mismatches": cand_mm,
        "validity_mismatches": val_mm,
        "value_mismatches": value_mm,
    }


def step9_layer2_ew_parity(
    ew_col: np.ndarray, ew_sc: np.ndarray
) -> dict[str, int]:
    D, S = ew_col.shape
    pairs = D * S
    col_avail = np.isfinite(ew_col)
    sc_avail = np.isfinite(ew_sc)
    avail_mm = int(np.sum(col_avail != sc_avail))
    common = col_avail & sc_avail
    if np.any(common):
        val_mm = int(np.sum(ew_col[common] != ew_sc[common]))
    else:
        val_mm = 0
    cells_per_sec = pairs / max(
        _TIMES.get("ew_columnar_elapsed", 1e-9), 1e-9
    )
    print("\n=== LAYER 2 — EW PARITY ===")
    print(f"  (date, scope) pairs:            {pairs:,}")
    print(f"  EW availability mismatches:     {avail_mm}")
    print(f"  EW value mismatches (exact):    {val_mm}")
    print(f"  col scope-date cells/s:         {cells_per_sec:,.0f}")
    return {
        "scope_date_pairs": pairs,
        "availability_mismatches": avail_mm,
        "value_mismatches": val_mm,
        "scope_date_cells_per_second_col": cells_per_sec,
    }


def step10_11_obs_series_and_dynamics(
    analysis_dates,
    scope_keys,
    ew_col,
    ew_sc,
):
    """Layer 3 (ObservationSeries) + Layer 4 (Dynamics) parity.

    Because both sides are fed through the SAME canonical owners, the
    required parity is: when the EW primitive series are semantically
    identical, ObservationSeries and Dynamics must be struct-deep equal.
    """
    timing_start("obs_series_and_dynamics")
    from app.services.review_historical_ew_columnar import (
        build_scope_dynamics_from_ew,
    )

    obs_mismatches = 0
    dyn_mismatch_scopes: list[Any] = []

    D = len(analysis_dates)
    for si, sk in enumerate(scope_keys):
        ew_col_list: list[float | None] = [
            (None if np.isnan(ew_col[t, si]) else float(ew_col[t, si]))
            for t in range(D)
        ]
        ew_sc_list: list[float | None] = [
            (None if np.isnan(ew_sc[t, si]) else float(ew_sc[t, si]))
            for t in range(D)
        ]
        col_res = build_scope_dynamics_from_ew(
            scope_type="industry_l1",
            scope_key=sk,
            analysis_dates=analysis_dates,
            ew_values=ew_col_list,
        )
        sc_res = build_scope_dynamics_from_ew(
            scope_type="industry_l1",
            scope_key=sk,
            analysis_dates=analysis_dates,
            ew_values=ew_sc_list,
        )

        # --- Layer 3: ObservationSeries primitive-point equality.
        col_prim = col_res["observation_series"]["primitives"]["equal_weight_return"]
        sc_prim = sc_res["observation_series"]["primitives"]["equal_weight_return"]
        col_meta = (
            col_prim["key"],
            col_prim["l1_path"],
            col_res["observation_series"].get("scope_type"),
            col_res["observation_series"].get("scope_key"),
        )
        sc_meta = (
            sc_prim["key"],
            sc_prim["l1_path"],
            sc_res["observation_series"].get("scope_type"),
            sc_res["observation_series"].get("scope_key"),
        )
        if col_meta != sc_meta:
            obs_mismatches += 1
            continue
        if len(col_prim["points"]) != len(sc_prim["points"]):
            obs_mismatches += 1
            continue
        point_bad = False
        for pcol, psca in zip(col_prim["points"], sc_prim["points"]):
            if (
                pcol["trade_date"] != psca["trade_date"]
                or pcol["readiness"] != psca["readiness"]
                or pcol["available"] != psca["available"]
            ):
                point_bad = True
                break
            if pcol["available"]:
                if float(pcol["value"]) != float(psca["value"]):
                    point_bad = True
                    break
            else:
                if pcol["value"] is not None or psca["value"] is not None:
                    point_bad = True
                    break
        if point_bad:
            obs_mismatches += 1
            continue

        # --- Layer 4: Dynamics deep structural equality.
        if not _dyn_deep_equal(col_res["dynamics"], sc_res["dynamics"]):
            dyn_mismatch_scopes.append(sk)

    timing_stop("obs_series_and_dynamics")
    marker("after-dynamics")
    print("\n=== LAYER 3 — OBSERVATION SERIES ===")
    print(f"  scope count:                    {len(scope_keys)}")
    print(f"  ObservationSeries mismatches:   {obs_mismatches}")
    print("\n=== LAYER 4 — DYNAMICS ===")
    print(f"  Dynamics mismatch scopes:       {len(dyn_mismatch_scopes)}")
    if dyn_mismatch_scopes:
        print(f"    first 3 bad: {dyn_mismatch_scopes[:3]}")
    return {
        "scope_count": len(scope_keys),
        "observation_series_mismatches": obs_mismatches,
        "dynamics_mismatch_scope_count": len(dyn_mismatch_scopes),
        "dynamics_mismatch_scope_ids": [str(x) for x in dyn_mismatch_scopes[:10]],
    }


def _dyn_deep_equal(a: Any, b: Any) -> bool:
    if isinstance(a, dict):
        if not isinstance(b, dict):
            return False
        if a.keys() != b.keys():
            return False
        return all(_dyn_deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        if not isinstance(b, list):
            return False
        if len(a) != len(b):
            return False
        return all(_dyn_deep_equal(xa, xb) for xa, xb in zip(a, b))
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        if math.isinf(a) and math.isinf(b):
            return (a > 0) == (b > 0)
        return bool(a == b)
    return bool(a == b)


# ---------------------------------------------------------------------------
# 14. Final report + acceptance verdict.
# ---------------------------------------------------------------------------

def step14_final_report(
    identity: dict[str, Any],
    D: int,
    R: int,
    S: int,
    M: int,
    scope_member_refs: int,
    l1: dict[str, int],
    l2: dict[str, int],
    l3l4: dict[str, Any],
    memory: dict[str, Any],
    cpu: dict[str, Any],
    adapter: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    marker("benchmark-end")
    report = {
        "DATASET": identity,
        "DIMENSIONS": {
            "analysis_asof": identity["analysis_asof_date"],
            "D": D,
            "R": R,
            "S": S,
            "M": M,
            "scope_member_refs": scope_member_refs,
        },
        "PARITY": {
            "member_pairs": l1["member_pairs"],
            "member_candidate_mismatch": l1["candidate_mismatches"],
            "member_validity_mismatch": l1["validity_mismatches"],
            "member_value_mismatch": l1["value_mismatches"],
            "scope_date_pairs": l2["scope_date_pairs"],
            "ew_availability_mismatch": l2["availability_mismatches"],
            "ew_value_mismatch": l2["value_mismatches"],
            "observation_series_mismatches": l3l4["observation_series_mismatches"],
            "dynamics_mismatch_scopes": l3l4["dynamics_mismatch_scope_count"],
        },
        "ADAPTER_INTEGRITY": adapter,
        "MEMORY": memory,
        "CPU": cpu,
    }
    accept_all = (
        l1["candidate_mismatches"] == 0
        and l1["validity_mismatches"] == 0
        and l1["value_mismatches"] == 0
        and l2["availability_mismatches"] == 0
        and l2["value_mismatches"] == 0
        and l3l4["observation_series_mismatches"] == 0
        and l3l4["dynamics_mismatch_scope_count"] == 0
        and adapter["adapter_bar_sample_mismatches"] == 0
        and adapter["membership_refs_match"] is True
        and adapter["membership_unmapped_after_union"] == 0
    )
    rss_inc_mib = memory.get("incremental_peak_mib")
    rss_ok = rss_inc_mib is not None and rss_inc_mib < 500
    overall = accept_all and rss_ok

    print("\n" + "=" * 68)
    print("M5-B2 — FROZEN DATASET BENCHMARK FINAL SUMMARY")
    print("=" * 68)
    print("\nDATASET")
    for k, v in report["DIMENSIONS"].items():
        print(f"  {k:<24s}: {v}")
    print("\nPARITY")
    for k, v in report["PARITY"].items():
        print(f"  {k:<28s}: {v}")
    print("\nADAPTER INTEGRITY")
    for k, v in report["ADAPTER_INTEGRITY"].items():
        if isinstance(v, float):
            print(f"  {k:<32s}: {v:,.1f}")
        else:
            print(f"  {k:<32s}: {v}")
    print("\nMEMORY (MiB)")
    for k, v in memory.items():
        print(f"  {k:<28s}: {v}")
    print("\nCPU (s)")
    for k, v in cpu.items():
        if isinstance(v, float):
            print(f"  {k:<28s}: {v:8.3f}")
        else:
            print(f"  {k:<28s}: {v}")
    print()
    if overall:
        print("STATUS: M5-B2 BENCHMARK PASS — READY FOR GIT COMMIT REVIEW")
    else:
        print("STATUS: M5-B2 FAIL")
        if not (
            l1["candidate_mismatches"] == 0
            and l1["validity_mismatches"] == 0
            and l1["value_mismatches"] == 0
            and l2["availability_mismatches"] == 0
            and l2["value_mismatches"] == 0
            and l3l4["observation_series_mismatches"] == 0
            and l3l4["dynamics_mismatch_scope_count"] == 0
        ):
            print("  reason: parity mismatch; see PARITY section above.")
        elif (
            adapter["adapter_bar_sample_mismatches"] != 0
            or adapter["membership_refs_match"] is not True
            or adapter["membership_unmapped_after_union"] != 0
        ):
            print(
                "  reason: frozen adapter integrity gate FAILED; see "
                "ADAPTER INTEGRITY section above."
            )
        elif not rss_ok:
            print(
                f"  reason: incremental peak RSS {rss_inc_mib!r} MiB "
                f">= 500 MiB acceptance gate."
            )
    return bool(overall), report


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    global BASELINE_RSS_KB
    BASELINE_RSS_KB = current_rss_kb()
    dataset_path = argv[1] if len(argv) > 1 else DEFAULT_DATASET_PATH
    if not os.path.isdir(dataset_path):
        print(f"ERROR: dataset path not found: {dataset_path}", file=sys.stderr)
        return 2

    # 1. Identity.
    _ds_path, axis = step1_dataset_identity(dataset_path)
    identity = {
        "dataset_path": dataset_path,
        "analysis_asof_date": axis[-1].isoformat(),
        "analysis_date_count": len(axis),
        "first_date": axis[0].isoformat(),
        "last_date": axis[-1].isoformat(),
    }

    # 2. Calendar T1.
    t1_by_date, _trading = step2_calendar(dataset_path, axis)
    # Feed into M5-B1 axis builder — naturally validates (complete map + T1<T).
    from app.services.review_historical_ew_columnar import (
        build_required_bar_axis,
    )
    required_bar_dates, t_idx, t1_idx = build_required_bar_axis(
        axis, t1_by_date
    )
    R = len(required_bar_dates)
    print(f"  required_bar_dates R:           {R}")

    # 3. Scopes + memberships.
    scopes, scope_ids = step3_scopes(dataset_path)
    S = len(scope_ids)
    (
        memberships,
        _refs,
        _cnts,
        raw_selected_membership_rows,
        raw_duplicate_membership_rows,
    ) = step3b_memberships(dataset_path, scope_ids)
    # Re-key memberships dict by scope_ids (preserves deterministic order),
    # so compute_scope_ew_matrix iteration order matches scope_ids.
    memberships_ordered: "OrderedDict[Any, list[Any]]" = OrderedDict()
    raw_deduped_membership_refs = 0
    for sid in scope_ids:
        lst = memberships[sid]
        memberships_ordered[sid] = list(lst)
        raw_deduped_membership_refs += len(lst)

    # 4. Member union + sparse indices.
    member_ids, scope_member_idx, _ = step4_member_universe(memberships_ordered)
    M = len(member_ids)

    # --- Membership adapter integrity gate.
    # NOTE on "unmapped after union":
    #   member_ids is built as the deduped, sorted union of the exact
    #   membership lists we just loaded.  Therefore, by construction,
    #   every deduped membership id IS present in member_ids.  We still
    #   measure it explicitly so the report carries the evidence
    #   (instead of silently hardcoding 0) — but we also document
    #   that this is NOT an external instrument-master validation.
    #   The real fail-closed owner for "member id is actually a column"
    #   is build_scope_member_indices() in M5-B1 core.  (Instrument
    #   master data is intentionally outside M5-B2's allowed inputs.)
    member_to_col = {m: i for i, m in enumerate(member_ids)}
    membership_unmapped_after_union = 0
    for _sid, members in memberships_ordered.items():
        for m in members:
            if m not in member_to_col:
                membership_unmapped_after_union += 1
    # 4b. mapped scope-member refs == sum of scope_member_idx lengths.
    mapped_scope_member_refs = sum(
        len(v) for v in scope_member_idx.values()
    )
    print("\n=== MEMBERSHIP ADAPTER INTEGRITY GATE ===")
    print(f"  raw selected membership rows:   {raw_selected_membership_rows}")
    print(f"  raw duplicate membership rows:  {raw_duplicate_membership_rows}")
    print(f"  deduped membership refs:        {raw_deduped_membership_refs}")
    print(f"  mapped scope-member refs:       {mapped_scope_member_refs}")
    print(f"  unmapped after union:           {membership_unmapped_after_union}")
    print(
        "    (expected 0 by construction; real fail-closed owner is"
        " build_scope_member_indices; does NOT assert external instrument"
        " master validity — instruments are outside M5-B2 allowed inputs)"
    )
    membership_refs_match = (
        raw_deduped_membership_refs == mapped_scope_member_refs
    )
    if not membership_refs_match:
        print("  MATCH: NO (acceptance gate FAIL)")
    else:
        print("  MATCH: deduped refs == mapped refs  (OK)")
    if membership_unmapped_after_union != 0:
        print("  UNMAPPED AFTER UNION: non-zero (FAIL)")
    else:
        print("  UNMAPPED AFTER UNION: 0  (OK by construction)")

    # 5. Close matrix.
    close, bars_stats = step5_close_matrix(
        dataset_path, required_bar_dates, member_ids
    )

    # 6. Columnar path.
    r_out, scope_keys, ew_col = step6_columnar_path(
        close, t_idx, t1_idx, scope_member_idx
    )
    # Sanity: scope_keys iteration order == scope_ids ordering.
    if scope_keys != scope_ids:
        raise ValueError(
            "compute_scope_ew_matrix returned scope_key iteration order "
            "different from the input scope_member_idx mapping.  "
            "This would break L2+ parity alignment."
        )

    # 7. Scalar oracle.
    ew_sc, scalar_price, scalar_value, scalar_valid = step7_scalar_ew_oracle(
        close, t_idx, t1_idx, scope_keys, scope_member_idx
    )

    # 8. L1 parity.
    l1 = step8_layer1_parity(r_out, scalar_price, scalar_value, scalar_valid)

    # 9. L2 EW parity.
    l2 = step9_layer2_ew_parity(ew_col, ew_sc)

    # 10/11. ObservationSeries + Dynamics parity.
    marker("after-observation-series")
    l3l4 = step10_11_obs_series_and_dynamics(
        axis, scope_keys, ew_col, ew_sc
    )

    # 12/13. Memory + CPU summary derived from markers/timers.
    baseline_mib = (
        None if BASELINE_RSS_KB is None else BASELINE_RSS_KB / 1024.0
    )
    peak_mib = peak_rss_kb() / 1024.0
    inc_peak_mib = (
        None if baseline_mib is None else peak_mib - baseline_mib
    )
    final_cur = current_rss_kb()
    final_cur_mib = None if final_cur is None else final_cur / 1024.0
    memory = {
        "baseline_rss_mib": baseline_mib,
        "final_current_rss_mib": final_cur_mib,
        "peak_rss_mib": peak_mib,
        "incremental_peak_mib": (
            round(inc_peak_mib, 2) if inc_peak_mib is not None else None
        ),
    }
    cpu = {
        "identity_s": round(_TIMES.get("identity_elapsed", 0.0), 3),
        "calendar_s": round(_TIMES.get("calendar_elapsed", 0.0), 3),
        "boards_memberships_s": round(
            _TIMES.get("boards_elapsed", 0.0)
            + _TIMES.get("memberships_elapsed", 0.0)
            + _TIMES.get("member_union_elapsed", 0.0),
            3,
        ),
        "bars_scan_and_fill_s": round(_TIMES.get("bars_scan_elapsed", 0.0), 3),
        "return_matrix_s": round(_TIMES.get("return_matrix_elapsed", 0.0), 3),
        "ew_columnar_s": round(_TIMES.get("ew_columnar_elapsed", 0.0), 3),
        "scalar_ew_oracle_s": round(
            _TIMES.get("ew_scalar_oracle_elapsed", 0.0), 3
        ),
        "observation_series_and_dynamics_s": round(
            _TIMES.get("obs_series_and_dynamics_elapsed", 0.0), 3
        ),
        "total_wall_s": round(
            MARKERS[-1]["t_s"] - MARKERS[0]["t_s"]
            if MARKERS
            else 0.0,
            3,
        ),
        "bars_rows_per_second": round(bars_stats["rows_per_second"], 1),
        "scope_date_cells_per_second_columnar": round(
            l2["scope_date_cells_per_second_col"], 1
        ),
    }

    # 14. Final report + acceptance.
    adapter = {
        "bars_sample_total": bars_stats["adapter_bar_sample_size"],
        "bars_sample_first_n": bars_stats["adapter_bar_sample_first"],
        "bars_sample_last_n": bars_stats["adapter_bar_sample_last"],
        "bars_sample_hash_n": bars_stats["adapter_bar_sample_hash"],
        "adapter_bar_sample_mismatches": bars_stats[
            "adapter_bar_sample_mismatches"
        ],
        "selected_raw_unavailable_close_rows": bars_stats[
            "selected_raw_unavailable_close_rows"
        ],
        "bars_duplicate_exact": bars_stats["duplicate_exact"],
        "bars_duplicate_conflict": bars_stats["duplicate_conflict"],
        "membership_raw_selected_rows": raw_selected_membership_rows,
        "membership_raw_duplicate_rows": raw_duplicate_membership_rows,
        "membership_deduped_refs": raw_deduped_membership_refs,
        "membership_mapped_scope_member_refs": mapped_scope_member_refs,
        "membership_unmapped_after_union": membership_unmapped_after_union,
        "membership_unmapped_after_union_expected_zero": True,
        "membership_refs_match": bool(membership_refs_match),
    }
    ok, report = step14_final_report(
        identity=identity,
        D=len(axis),
        R=R,
        S=S,
        M=M,
        scope_member_refs=mapped_scope_member_refs,
        l1=l1,
        l2=l2,
        l3l4=l3l4,
        memory=memory,
        cpu=cpu,
        adapter=adapter,
    )
    # Also emit a JSON copy of the report for downstream tooling on stdout's
    # last line (parsable after the human summary banner has finished).
    try:
        print(f"\nREPORT_JSON_BEGIN\n{json.dumps(report, default=str)}\nREPORT_JSON_END")
    except Exception as e:  # pragma: no cover
        print(f"WARNING: failed to serialise report JSON: {e}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
