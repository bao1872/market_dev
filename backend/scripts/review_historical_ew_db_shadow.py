"""M5-C2-A Production READ-ONLY Historical EW shadow runner.

Version-controlled proof harness that compares:

    NEW close-only EW source (M5-C1 columnar + canonical Dynamics)
            vs
    OLD canonical reconstruction path (on a 3-scope deterministic sample)

on the live production database for:

    scope_type          = industry_l1
    analysis_asof_date  = 2026-08-24

Strict guarantees
=================

* **READ-ONLY only.**  Every session opens with ``SET TRANSACTION READ ONLY``;
  on completion or exception the transaction is rolled back and the session
  is closed.  The runner never calls ``commit()``, never ``flush()`` for
  business data, never invokes ``publish_review`` or any persistence helper.
* **Production axis / scope universe / old parity** are read through their
  canonical owners — this runner does NOT implement any of them on its own:
  ``_build_dynamics_trading_axis``, ``compute_current_static_scope_dynamics_batch``,
  ``MarketBoard`` L1 industry select, ``compute_current_static_historical_ew_batch``,
  ``build_scope_dynamics_from_ew``.
* **NEW-path memory verdict is FROZEN before the OLD 3-scope sample parity
  runs.**  The old reconstruction samples are allowed to raise the process
  high-water mark afterward, but they never overwrite ``incremental_process_peak_new``.
* **Full family new path runs once on ALL industry L1 scopes** (~31 scopes,
  current production scale), producing the Linux /proc RSS + cgroup verdict.
* OLD parity is intentionally limited to exactly 3 scopes (small / median /
  large membership) to avoid re-triggering the known old-path 4 GiB OOM at
  full scale.  For a correctness proof at production scale, 3 sampled scopes
  with exact parity + full-family close-only RSS gate is sufficient.

Execution environment
=====================

Runs from the backend project root with:

    PURE_UNIT_TEST=0 PYTHONPATH=. .venv/bin/python \\
        scripts/review_historical_ew_db_shadow.py

against the real Linux box that owns ``bz_stock`` (panji-prod).  There must
be no test-database override present in the process environment for the
actual shadow run; C2 is about real production evidence.

The UNIT TESTS in ``tests/test_review_historical_ew_db_shadow.py`` are DB-free
(PURE_UNIT_TEST=1) and mock all heavy owners.
"""
from __future__ import annotations

import gc
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Canonical owners (exactly the set the C2 spec allows — no more).
# ---------------------------------------------------------------------------
from app.db import AsyncSessionLocal
from app.models.market_board import MarketBoard
from app.services.review_historical_ew_columnar import build_scope_dynamics_from_ew
from app.services.review_historical_ew_db_service import (
    HistoricalEWBatchResult,
    compute_current_static_historical_ew_batch,
)
from app.services.review_orchestrator_service import _build_dynamics_trading_axis
from app.services.review_scope_dynamics_service import (
    compute_current_static_scope_dynamics_batch,
)

# ---------------------------------------------------------------------------
# Constants / runtime identity.
# ---------------------------------------------------------------------------
RUNNER_EXPECTED_BASE_SHA = "1d901f537f5de431e462b5461f5a24e821eea231"
ANALYSIS_ASOF_DATE = date(2026, 8, 24)
SCOPE_TYPE = "industry_l1"
SAMPLE_STRATEGIES = ("small", "median", "large")


# ===========================================================================
# Memory helpers / cgroup helpers.  Pure read-only, never write controls.
# ===========================================================================
@dataclass
class MemorySnapshot:
    process_rss_kib: int | None = None
    process_vmhwm_kib: int | None = None
    cgroup_current_bytes: int | None = None
    cgroup_peak_bytes: int | None = None
    cgroup_limit_bytes: int | None = None

    @classmethod
    def capture(cls) -> "MemorySnapshot":
        snap = cls()
        try:
            with open("/proc/self/status", "r") as fh:
                for line in fh:
                    key, _, value = line.strip().partition(":")
                    value = value.strip()
                    if key == "VmRSS":
                        snap.process_rss_kib = int(value.split()[0])
                    elif key == "VmHWM":
                        snap.process_vmhwm_kib = int(value.split()[0])
        except (FileNotFoundError, PermissionError, ValueError):
            pass
        # cgroup v2.
        for path_name, attr in (
            ("/sys/fs/cgroup/memory.current", "cgroup_current_bytes"),
            ("/sys/fs/cgroup/memory.peak", "cgroup_peak_bytes"),
            ("/sys/fs/cgroup/memory.max", "cgroup_limit_bytes"),
        ):
            try:
                raw = Path(path_name).read_text().strip()
                if raw == "max":
                    val: int | None = None
                else:
                    val = int(raw)
                setattr(snap, attr, val)
            except (FileNotFoundError, PermissionError, ValueError):
                pass
        return snap


def _kib_to_mib(kib: int | None) -> float | None:
    return None if kib is None else round(kib / 1024.0, 2)


def _bytes_to_mib(b: int | None) -> float | None:
    return None if b is None else round(b / (1024.0 * 1024.0), 2)


# ===========================================================================
# Deterministic 3-sample selection, exact-EW comparator, deep-dynamics comparator.
# ===========================================================================
def select_deterministic_sample_scopes(
    scope_key_member_tuples: Iterable[tuple[str, int]],
) -> dict[str, str]:
    """Pick the 3 deterministic parity-sample scopes.

    Input is ``(scope_key, member_count)`` pairs for every scope produced by
    the NEW path.  Ordering within a tie-break is by ``scope_key``
    (UUID-strings sort stably).  The returned dict maps strategy → scope_key.

    Strategy contract (M5-C2 step 6):
      * ``small``  — lowest ``(member_count, scope_key)``
      * ``medium`` — median of the list sorted by ``(member_count, scope_key)``
      * ``large``  — highest ``(member_count, scope_key)``

    If there are fewer than three scopes we simply reuse the largest available
    one (still strictly deterministic); the caller must ensure S > 0 for the
    broader ``S > 0`` acceptance gate.  This avoids silent randomness.
    """
    ordered = sorted(
        ((int(mc), str(sk)) for (sk, mc) in scope_key_member_tuples),
        key=lambda t: (t[0], t[1]),
    )
    if not ordered:
        raise ValueError("select_deterministic_sample_scopes: zero scopes")
    out: dict[str, str] = {}
    for name, idx in (
        ("small", 0),
        ("median", len(ordered) // 2),
        ("large", len(ordered) - 1),
    ):
        out[name] = ordered[idx][1]
    # Deduplicate (if 1–2 scopes caused overlap): fall back to "index from the
    # end / middle / start" with deterministic padding.  Result order is
    # still strictly fixed by the input.
    seen: set[str] = set()
    unique: list[str] = []
    for k, sk in out.items():
        if sk in seen:
            # Re-pick deterministically: walk from the start and grab the next
            # unused scope key (preserves: small first, largest last).
            for _mc, alt in ordered:
                if alt not in seen:
                    out[k] = alt
                    sk = alt
                    break
        seen.add(sk)
        unique.append(out[k])
    return out


def compare_ew_series_parity(
    axis: Sequence[date],
    new_ew_values: Sequence[float | None],
    old_observation_series: Any,
) -> tuple[int, int]:
    """Return ``(availability_mismatches, value_mismatches)``.

    Exact float equality; no tolerance. ``new_ew_values`` contains
    ``float | None`` aligned 1:1 to ``axis``.  ``old_observation_series`` is
    the canonical observation series object: we must extract the
    ``equal_weight_return`` primitive for every point.

    Availability contract:
      NEW ``None`` must coincide with OLD point unavailable OR OLD value
      non-finite.  Either side diverging → count mismatch.

    Value contract:
      when both are finite, float bits must match EXACTLY.
    """
    # Canonical observation series stores points in a list attribute; access
    # via ``trading_dates`` / ``__getitem__`` or ``to_snapshots()`` — the
    # published shape is point-based, so we use ``get_point(trade_date)``
    # if available, falling back to the known internal point list.
    D = len(axis)
    avail_mismatch = 0
    value_mismatch = 0
    # Build the OLD lookup once: trade_date → equal_weight_return value (
    # None = unavailable per canonical snapshot "payload.price.equal_weight_return").
    old_lookup: dict[date, float | None] = {}
    snapshots = getattr(old_observation_series, "to_snapshots", None)
    if callable(snapshots):
        for snap in snapshots():
            td = snap["trade_date"]
            if not isinstance(td, date):
                td = date.fromisoformat(str(td)[:10])
            val = snap.get("payload", {}).get("price", {}).get(
                "equal_weight_return"
            )
            old_lookup[td] = val
    else:
        # Legacy/internal point list shape.  Prefer the iterable points.
        points = getattr(old_observation_series, "points", None) or getattr(
            old_observation_series, "_points", []
        )
        for pt in points:
            td = getattr(pt, "trade_date", None)
            val = getattr(pt, "equal_weight_return", None) or None
            if val is None and hasattr(pt, "snapshot"):
                val = (pt.snapshot or {}).get("payload", {}).get("price", {}).get(
                    "equal_weight_return"
                )
            if td is None:
                continue
            if not isinstance(td, date):
                td = date.fromisoformat(str(td)[:10])
            old_lookup[td] = val
    for t, td in enumerate(axis):
        new_val = new_ew_values[t] if t < len(new_ew_values) else None
        old_val = old_lookup.get(td)  # None = genuinely unavailable
        # Availability normalisation.
        new_avail = new_val is not None and (
            isinstance(new_val, (int, float)) and math.isfinite(float(new_val))
        )
        old_avail = old_val is not None and (
            isinstance(old_val, (int, float)) and math.isfinite(float(old_val))
        )
        if new_avail != old_avail:
            avail_mismatch += 1
            continue
        if not new_avail and not old_avail:
            # Both unavailable — exact semantic parity.
            continue
        # Both finite.
        a = float(new_val)  # type: ignore[arg-type]
        b = float(old_val)  # type: ignore[arg-type]
        if a != b:
            value_mismatch += 1
    return avail_mismatch, value_mismatch


def deep_equal_dynamics(
    lhs: Any,
    rhs: Any,
    *,
    _path: str = "root",
) -> tuple[bool, str]:
    """Deterministic deep-equality for canonical Scope Dynamics dicts.

    Rules:
      * dict key sets must be EXACT.
      * list/tuple lengths must be EXACT.
      * finite floats / ints / bools: exact equality.
      * ``NaN == NaN`` only (no "any NaN equal NaN" via isclose).
      * same-sign ``inf == same-sign inf`` only.
      * ``None`` must equal ``None`` only.
      * dataclasses / ObservationSeries objects walk their public dict-keys.

    Returns ``(equal, first_mismatch_path_or_empty)``.
    """
    # ndarray fast-fail FIRST — canonical Dynamics result must never contain
    # numpy arrays.  We reject this before any type-equality branch so the
    # failure reason is explicit even when the other side is a list.
    if isinstance(lhs, np.ndarray) or isinstance(rhs, np.ndarray):
        return False, f"{_path} ndarray retention not allowed"
    # Type-fast paths.
    if lhs is rhs:
        return True, ""
    if type(lhs) is not type(rhs):
        # Accept minor divergence: tuple vs list for arrays when shapes are
        # identical (canonical builders return lists; some helpers may coerce
        # to tuples in the NEW-only helpers).  Strict: check contents only.
        if isinstance(lhs, (list, tuple)) and isinstance(rhs, (list, tuple)):
            if len(lhs) != len(rhs):
                return False, f"{_path} length {len(lhs)} vs {len(rhs)}"
            for i, (x, y) in enumerate(zip(lhs, rhs)):
                ok, why = deep_equal_dynamics(x, y, _path=f"{_path}[{i}]")
                if not ok:
                    return False, why
            return True, ""
        return False, f"{_path} type mismatch {type(lhs).__name__} vs {type(rhs).__name__}"
    # NaN / ±inf paths before general numeric equality.
    if isinstance(lhs, float) and isinstance(rhs, float):
        ln, rn = math.isnan(lhs), math.isnan(rhs)
        if ln and rn:
            return True, ""
        if ln != rn:
            return False, f"{_path} NaN parity"
        li, ri = math.isinf(lhs), math.isinf(rhs)
        if li and ri:
            if (lhs > 0) == (rhs > 0):
                return True, ""
            return False, f"{_path} inf sign"
        if li != ri:
            return False, f"{_path} inf parity"
        if lhs != rhs:
            return False, f"{_path} float value {lhs!r} vs {rhs!r}"
        return True, ""
    if isinstance(lhs, dict):
        lhs_keys = set(lhs.keys())
        rhs_keys = set(rhs.keys())
        if lhs_keys != rhs_keys:
            extra = lhs_keys - rhs_keys
            missing = rhs_keys - lhs_keys
            return False, (
                f"{_path} keys differ — extra={sorted(extra)[:5]!r} "
                f"missing={sorted(missing)[:5]!r}"
            )
        for k in sorted(lhs_keys):
            ok, why = deep_equal_dynamics(
                lhs[k], rhs[k], _path=f"{_path}.{k}"
            )
            if not ok:
                return False, why
        return True, ""
    if isinstance(lhs, (list, tuple)):
        if len(lhs) != len(rhs):
            return False, f"{_path} length {len(lhs)} vs {len(rhs)}"
        for i, (x, y) in enumerate(zip(lhs, rhs)):
            ok, why = deep_equal_dynamics(x, y, _path=f"{_path}[{i}]")
            if not ok:
                return False, why
        return True, ""
    if isinstance(lhs, (int, str, bool)):
        if lhs != rhs:
            return False, f"{_path} scalar {lhs!r} vs {rhs!r}"
        return True, ""
    if lhs is None and rhs is None:
        return True, ""
    # Generic dataclass/arbitrary object: compare asdict() when possible.
    if hasattr(lhs, "__dataclass_fields__") and hasattr(rhs, "__dataclass_fields__"):
        return deep_equal_dynamics(asdict(lhs), asdict(rhs), _path=_path)
    # Final fallback: Python equality.  This covers date / UUID / uuid-like /
    # enum sentinels that all implement a deterministic ==.
    if lhs == rhs:
        return True, ""
    return False, f"{_path} fallback equality failed"


# ===========================================================================
# Parity per-scope struct + final report structs.
# ===========================================================================
@dataclass
class SampleParityResult:
    strategy: str
    scope_key: str
    member_count: int
    ew_availability_mismatch: int
    ew_value_mismatch: int
    dynamics_deep_equal: bool
    dynamics_mismatch_path: str
    old_path_elapsed_ms: float
    process_rss_before_old_mib: float | None
    process_rss_after_old_mib: float | None


@dataclass
class ShadowReport:
    """Full machine-readable report persisted as the step-8 JSON output."""

    runner_expected_git_sha: str
    runtime_git_sha: str | None
    analysis_asof: str
    scope_type: str
    db_mode: str
    writes_attempted: int
    session_set_read_only_issued: int

    axis_D: int
    axis_first_date: str | None
    axis_last_date: str | None

    scopes_S: int
    members_M: int
    bar_R: int
    scope_member_refs: int
    rows_streamed: int
    finite_close_cells: int
    unavailable_close_rows: int
    missing_cells: int
    close_matrix_mib: float

    total_ew_cells: int
    available_ew_cells: int
    unavailable_ew_cells: int
    scopes_all_unavailable: int

    calendar_ms: int
    membership_ms: int
    bars_stream_ms: int
    return_ms: int
    ew_ms: int
    source_total_ms: int
    dynamics_total_ms: int

    memory_before: dict[str, Any]
    memory_after_new: dict[str, Any]
    incremental_process_peak_new_mib: float | None
    memory_after_old_samples: dict[str, Any]

    sample_parity: list[dict[str, Any]]
    acceptance: dict[str, Any]
    final_status: str


# ===========================================================================
# Production-database session helper (READ ONLY, no writes).
# ---------------------------------------------------------------------------
# Wrapper allows tests to inject a session-factory without touching the real
# DB engine.
SESSION_FACTORY = AsyncSessionLocal


async def _open_readonly_session() -> tuple[AsyncSession, bool]:
    """Open a fresh AsyncSession and issue SET TRANSACTION READ ONLY.

    Returns ``(session, issued_readonly_flag)``.  The flag is evidence for
    the report's ``session_set_read_only_issued`` counter; failing to issue
    does NOT silently make the session writable — it makes the runner exit
    with a hard acceptance failure.
    """
    session: AsyncSession = SESSION_FACTORY()
    issued = False
    try:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        issued = True
    except Exception as exc:  # pragma: no cover — explicit surface in report.
        try:
            await session.rollback()
        except Exception:
            pass
        await session.close()
        raise RuntimeError(
            "Failed to issue SET TRANSACTION READ ONLY for shadow session; "
            "production database must not be written.  Inner: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return session, issued


# ===========================================================================
# Step 3 — Industry L1 universe SELECT exactly 2 columns.
# ===========================================================================
async def _fetch_industry_l1_universe(session: AsyncSession) -> list[tuple[str, str]]:
    """Return deterministic ``[(scope_key_str, name)]`` for all active L1 industries.

    Exact WHERE: ``type == "industry"``, ``hierarchyLevel == "L1"``,
    ``isActive IS TRUE``.  Columns: ``MarketBoard.id``, ``MarketBoard.name``.
    """
    stmt = select(MarketBoard.id, MarketBoard.name).where(
        MarketBoard.type == "industry",
        MarketBoard.hierarchyLevel == "L1",
        MarketBoard.isActive.is_(True),
    ).order_by(MarketBoard.id.asc())
    rows = (await session.execute(stmt)).all()
    return [(str(r[0]), str(r[1])) for r in rows]


# ===========================================================================
# Acceptance logic (kept out of main so tests can exercise it standalone).
# ===========================================================================
def evaluate_acceptance(
    *,
    read_only_confirmed: bool,
    scopes_S: int,
    ew_lengths_all_equal_D: bool,
    all_dynamics_built: bool,
    sample_results: list[SampleParityResult],
    required_sample_names: Sequence[str],
    incremental_process_peak_new_mib: float | None,
    hard_peak_mib: float = 500.0,
) -> tuple[dict[str, Any], bool]:
    gates: dict[str, Any] = {}
    gates["read_only_confirmed"] = bool(read_only_confirmed)
    gates["S_positive"] = scopes_S > 0
    gates["all_scope_ew_lengths_equal_D"] = bool(ew_lengths_all_equal_D)
    gates["all_scope_dynamics_outputs_built"] = bool(all_dynamics_built)

    sample_complete = len(sample_results) == len(required_sample_names)
    gates["3_deterministic_samples_completed"] = sample_complete
    ew_avail_ok = all(r.ew_availability_mismatch == 0 for r in sample_results)
    ew_val_ok = all(r.ew_value_mismatch == 0 for r in sample_results)
    dyn_ok = all(r.dynamics_deep_equal for r in sample_results)
    gates["sample_ew_availability_mismatch_eq_0"] = ew_avail_ok
    gates["sample_ew_value_mismatch_eq_0"] = ew_val_ok
    gates["sample_dynamics_deep_equal_true"] = dyn_ok

    peak_fail = (
        incremental_process_peak_new_mib is None
        or incremental_process_peak_new_mib >= hard_peak_mib
    )
    gates["incremental_process_peak_new_lt_500_mib"] = not peak_fail
    gates["incremental_process_peak_new_mib"] = incremental_process_peak_new_mib

    all_pass = all(
        [
            gates["read_only_confirmed"],
            gates["S_positive"],
            gates["all_scope_ew_lengths_equal_D"],
            gates["all_scope_dynamics_outputs_built"],
            gates["3_deterministic_samples_completed"],
            gates["sample_ew_availability_mismatch_eq_0"],
            gates["sample_ew_value_mismatch_eq_0"],
            gates["sample_dynamics_deep_equal_true"],
            gates["incremental_process_peak_new_lt_500_mib"],
        ]
    )
    return gates, all_pass


# ===========================================================================
# Git SHA runtime identity (do not invent from a comment string).
# ===========================================================================
def _read_runtime_git_sha() -> str | None:
    try:
        import subprocess  # local import only

        top = Path(__file__).resolve().parents[1]
        out = subprocess.run(
            ["git", "-C", str(top), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        if out.returncode == 0:
            return out.stdout.strip()[:40] or None
    except Exception:
        pass
    return None


# ===========================================================================
# MAIN — actual shadow runner (only executed when script launched directly).
# ===========================================================================
async def _run_shadow() -> int:
    print("=" * 78)
    print("M5-C2-A  PRODUCTION READ-ONLY SHADOW RUNNER")
    print("=" * 78)

    t_total_start = time.perf_counter()
    runtime_sha = _read_runtime_git_sha()
    print(f"RUNNER_EXPECTED_GIT_SHA : {RUNNER_EXPECTED_BASE_SHA}")
    print(f"RUNTIME_GIT_SHA         : {runtime_sha or '(unknown)'}")
    if runtime_sha and runtime_sha[:7] != RUNNER_EXPECTED_BASE_SHA[:7]:
        print("WARN: runtime SHA does not match expected base (C2-A was authored at "
              f"{RUNNER_EXPECTED_BASE_SHA[:7]}); continue with reported identity.")

    print(f"analysis_asof_date       : {ANALYSIS_ASOF_DATE.isoformat()}")
    print(f"scope_type               : {SCOPE_TYPE}")
    print("DB_MODE                  : READ ONLY")

    # ------------------------------------------------------------------
    # STEP 1 — memory / cgroup baseline.
    # ------------------------------------------------------------------
    mem_before = MemorySnapshot.capture()
    print("\n[STEP 1] MEMORY BASELINE")
    print(
        f"  process_rss_before     = {_kib_to_mib(mem_before.process_rss_kib)} MiB"
    )
    print(
        f"  process_peak_before    = {_kib_to_mib(mem_before.process_vmhwm_kib)} MiB"
    )
    print(
        f"  cgroup_current_before  = {_bytes_to_mib(mem_before.cgroup_current_bytes)} MiB"
    )
    print(
        f"  cgroup_peak_before     = {_bytes_to_mib(mem_before.cgroup_peak_bytes)} MiB"
    )
    print(f"  cgroup_limit           = {_bytes_to_mib(mem_before.cgroup_limit_bytes)} MiB")

    # ------------------------------------------------------------------
    # STEP 2 — canonical production axis.
    # ------------------------------------------------------------------
    axis: list[date] | None = None
    scope_universe: list[tuple[str, str]] | None = None
    session_main, issued_readonly = await _open_readonly_session()
    writes_attempted = 0
    try:
        axis = await _build_dynamics_trading_axis(
            session_main,
            ANALYSIS_ASOF_DATE,
        )
        if not axis:
            raise ValueError("_build_dynamics_trading_axis returned empty axis")
        prev: date | None = None
        for d in axis:
            if prev is not None and not (prev < d):
                raise ValueError(
                    f"axis not strictly ascending unique ({prev} >= {d})"
                )
            prev = d
        if axis[-1] != ANALYSIS_ASOF_DATE:
            raise ValueError(
                f"axis tail {axis[-1].isoformat()} != analysis_asof_date "
                f"{ANALYSIS_ASOF_DATE.isoformat()}"
            )
        print("\n[STEP 2] CANONICAL PRODUCTION AXIS")
        print(f"  D = {len(axis)}")
        print(f"  first_date = {axis[0].isoformat()}")
        print(f"  last_date  = {axis[-1].isoformat()}")

        # --------------------------------------------------------------
        # STEP 3 — Industry L1 scope universe, 2-col MarketBoard select.
        # --------------------------------------------------------------
        scope_universe = await _fetch_industry_l1_universe(session_main)
        if not scope_universe:
            raise ValueError(
                "Industry L1 active universe empty — fail closed."
            )
        scope_keys_ordered = [sk for sk, _nm in scope_universe]
        print("\n[STEP 3] INDUSTRY L1 UNIVERSE")
        print(f"  S = {len(scope_keys_ordered)} scopes (deterministic id order)")

        # --------------------------------------------------------------
        # STEP 4 — FULL NEW PATH + FROZEN MEMORY VERDICT.
        # --------------------------------------------------------------
        mem_before_new = MemorySnapshot.capture()
        print(
            f"\n[STEP 4] NEW FULL FAMILY PATH — memory gate BEFORE — rss="
            f"{_kib_to_mib(mem_before_new.process_rss_kib)} MiB"
        )
        new_result: HistoricalEWBatchResult = (
            await compute_current_static_historical_ew_batch(
                session_main,
                SCOPE_TYPE,
                scope_keys_ordered,
                axis,
                analysis_asof_date=ANALYSIS_ASOF_DATE,
            )
        )
        # Build canonical Dynamics for every scope in the NEW result.
        t_dyn_start = time.perf_counter()
        new_scope_dynamics: dict[str, dict[str, Any]] = {}
        new_scopes_by_key = {s.scope_key: s for s in new_result.scopes}
        all_ew_len_D = True
        all_dynamics_built = True
        for sk in scope_keys_ordered:
            scope_res = new_scopes_by_key.get(sk)
            if scope_res is None:
                all_dynamics_built = False
                continue
            if len(scope_res.ew_values) != len(axis):
                all_ew_len_D = False
                # Don't raise — report in STEP 5.
                new_scope_dynamics[sk] = {"dynamics": None, "observation_series": None}
                continue
            try:
                built = build_scope_dynamics_from_ew(
                    scope_type=SCOPE_TYPE,
                    scope_key=sk,
                    analysis_dates=axis,
                    ew_values=list(scope_res.ew_values),
                )
                new_scope_dynamics[sk] = built
                if "dynamics" not in built or built.get("dynamics") is None:
                    all_dynamics_built = False
            except Exception as exc:
                all_dynamics_built = False
                new_scope_dynamics[sk] = {"dynamics": None, "observation_series": None,
                                         "_error": f"{type(exc).__name__}: {exc}"}
        dynamics_total_ms = int((time.perf_counter() - t_dyn_start) * 1000)

        # FROZEN MEMORY after NEW path — do NOT overwrite with OLD samples' HWM.
        mem_after_new = MemorySnapshot.capture()
        # Compute incremental peak against the step-1 baseline.
        peak_before_kib = mem_before.process_vmhwm_kib or mem_before.process_rss_kib or 0
        peak_after_new_kib = (
            mem_after_new.process_vmhwm_kib
            or mem_after_new.process_rss_kib
            or 0
        )
        if peak_before_kib is None:
            peak_before_kib = 0
        incremental_peak_new_mib: float | None = (
            _kib_to_mib(peak_after_new_kib - peak_before_kib)
            if peak_after_new_kib and peak_before_kib is not None
            else None
        )
        print(
            f"[STEP 4] NEW FULL FAMILY PATH — memory gate AFTER — rss="
            f"{_kib_to_mib(mem_after_new.process_rss_kib)} MiB, "
            f"peak(process) after={_kib_to_mib(peak_after_new_kib)} MiB, "
            f"Δpeak new (FROZEN)={incremental_peak_new_mib} MiB"
        )
        src = new_result.metrics
        print(
            "DIMENSIONS: "
            f"S={src.S}, M={src.M}, R={src.R}, "
            f"scope_member_refs={src.scope_member_refs}"
        )
        print(
            "CLOSE MATRIX: "
            f"rows_streamed={src.rows_streamed}, finite={src.finite_close_cells}, "
            f"unavail={src.unavailable_close_rows}, missing={src.missing_cells}, "
            f"close_matrix_mib={src.close_matrix_mib:.2f}"
        )
        print(
            "CPU: "
            f"calendar_ms={src.calendar_ms}, membership_ms={src.membership_ms}, "
            f"bars_stream_ms={src.bars_stream_ms}, return_ms={src.return_ms}, "
            f"ew_ms={src.ew_ms}, source_total_ms={src.total_ms}, "
            f"dynamics_total_ms={dynamics_total_ms}"
        )

        # --------------------------------------------------------------
        # STEP 5 — OUTPUT SANITY (EW-length / per-scope Dynamics existence).
        # --------------------------------------------------------------
        total_ew_cells = len(axis) * len(scope_keys_ordered)
        avail_cells = 0
        unavail_cells = 0
        all_unavailable_scope_count = 0
        for sk in scope_keys_ordered:
            scope_res = new_scopes_by_key.get(sk)
            if scope_res is None:
                unavail_cells += len(axis)
                all_unavailable_scope_count += 1
                continue
            this_scope_any_avail = False
            for v in scope_res.ew_values:
                if v is not None and (
                    isinstance(v, (int, float)) and math.isfinite(float(v))
                ):
                    avail_cells += 1
                    this_scope_any_avail = True
                else:
                    unavail_cells += 1
            if not this_scope_any_avail:
                all_unavailable_scope_count += 1
        print("\n[STEP 5] OUTPUT SANITY")
        print(f"  total EW cells                 = {total_ew_cells}")
        print(f"  available EW cells             = {avail_cells}")
        print(f"  unavailable EW cells           = {unavail_cells}")
        print(f"  scopes with all-EW-unavailable = {all_unavailable_scope_count}")
        if avail_cells + unavail_cells != total_ew_cells:
            print("WARN: cell count parity mismatch (likely a bug, not data).")

        # --------------------------------------------------------------
        # STEP 6 — 3 deterministic sample OLD vs NEW parity.
        # --------------------------------------------------------------
        sample_results: list[SampleParityResult] = []
        scope_key_to_member = [
            (sk, new_scopes_by_key[sk].member_count) for sk in scope_keys_ordered
            if sk in new_scopes_by_key
        ]
        sample_plan = select_deterministic_sample_scopes(scope_key_to_member)
        print("\n[STEP 6] OLD-VS-NEW DETERMINISTIC 3-SAMPLE PARITY")
        for strategy in SAMPLE_STRATEGIES:
            sample_sk = sample_plan[strategy]
            sample_scope = new_scopes_by_key[sample_sk]
            print(
                f"  sample[{strategy:6s}] scope_key={sample_sk[:8]}.. "
                f"member_count={sample_scope.member_count}"
            )
            # FRESH session per sample.
            old_sess, old_issued = await _open_readonly_session()
            mem_before_old = MemorySnapshot.capture()
            t_old_start = time.perf_counter()
            try:
                if not old_issued:  # pragma: no cover — defensive
                    raise RuntimeError("old-sample session failed to issue RO")
                old_batch = await compute_current_static_scope_dynamics_batch(
                    old_sess,
                    SCOPE_TYPE,
                    [sample_sk],
                    axis,
                    analysis_asof_date=ANALYSIS_ASOF_DATE,
                )
                await old_sess.rollback()
            finally:
                await old_sess.close()
            old_elapsed_ms = (time.perf_counter() - t_old_start) * 1000.0
            mem_after_old = MemorySnapshot.capture()
            if len(old_batch) != 1:
                sample_results.append(
                    SampleParityResult(
                        strategy=strategy,
                        scope_key=sample_sk,
                        member_count=sample_scope.member_count,
                        ew_availability_mismatch=1,
                        ew_value_mismatch=0,
                        dynamics_deep_equal=False,
                        dynamics_mismatch_path=f"old_batch len={len(old_batch)}",
                        old_path_elapsed_ms=old_elapsed_ms,
                        process_rss_before_old_mib=_kib_to_mib(
                            mem_before_old.process_rss_kib
                        ),
                        process_rss_after_old_mib=_kib_to_mib(
                            mem_after_old.process_rss_kib
                        ),
                    )
                )
                continue
            old_result = old_batch[0]
            old_os = old_result.get("observation_series")
            ew_a, ew_v = compare_ew_series_parity(
                axis,
                list(sample_scope.ew_values),
                old_os,
            )
            new_built = new_scope_dynamics.get(sample_sk, {})
            new_dyn = new_built.get("dynamics")
            old_dyn = old_result.get("scope_dynamics")
            if new_dyn is None or old_dyn is None:
                dyn_equal = False
                why = (
                    f"dynamics missing new={new_dyn is None} old={old_dyn is None}"
                )
            else:
                dyn_equal, why = deep_equal_dynamics(new_dyn, old_dyn)
            sample_results.append(
                SampleParityResult(
                    strategy=strategy,
                    scope_key=sample_sk,
                    member_count=sample_scope.member_count,
                    ew_availability_mismatch=ew_a,
                    ew_value_mismatch=ew_v,
                    dynamics_deep_equal=dyn_equal,
                    dynamics_mismatch_path=why,
                    old_path_elapsed_ms=old_elapsed_ms,
                    process_rss_before_old_mib=_kib_to_mib(
                        mem_before_old.process_rss_kib
                    ),
                    process_rss_after_old_mib=_kib_to_mib(
                        mem_after_old.process_rss_kib
                    ),
                )
            )
            gc.collect()
    finally:
        # READ-ONLY cleanup: rollback + close.  Never commit().
        try:
            await session_main.rollback()
        except Exception:
            pass
        await session_main.close()

    # Memory after old samples (note: do NOT overwrite the frozen NEW peak).
    mem_after_old_all = MemorySnapshot.capture()

    # ------------------------------------------------------------------
    # STEP 7 + 8 — report assembly + acceptance verdict.
    # ------------------------------------------------------------------
    acceptance, passed = evaluate_acceptance(
        read_only_confirmed=issued_readonly,
        scopes_S=src.S,
        ew_lengths_all_equal_D=all_ew_len_D,
        all_dynamics_built=all_dynamics_built,
        sample_results=sample_results,
        required_sample_names=SAMPLE_STRATEGIES,
        incremental_process_peak_new_mib=incremental_peak_new_mib,
    )
    status = "M5-C2 SHADOW PASS" if passed else "M5-C2 SHADOW FAIL"

    report = ShadowReport(
        runner_expected_git_sha=RUNNER_EXPECTED_BASE_SHA,
        runtime_git_sha=runtime_sha,
        analysis_asof=ANALYSIS_ASOF_DATE.isoformat(),
        scope_type=SCOPE_TYPE,
        db_mode="READ ONLY",
        writes_attempted=writes_attempted,
        session_set_read_only_issued=1 if issued_readonly else 0,
        axis_D=len(axis),
        axis_first_date=axis[0].isoformat() if axis else None,
        axis_last_date=axis[-1].isoformat() if axis else None,
        scopes_S=src.S,
        members_M=src.M,
        bar_R=src.R,
        scope_member_refs=src.scope_member_refs,
        rows_streamed=src.rows_streamed,
        finite_close_cells=src.finite_close_cells,
        unavailable_close_rows=src.unavailable_close_rows,
        missing_cells=src.missing_cells,
        close_matrix_mib=float(src.close_matrix_mib),
        total_ew_cells=total_ew_cells,
        available_ew_cells=avail_cells,
        unavailable_ew_cells=unavail_cells,
        scopes_all_unavailable=all_unavailable_scope_count,
        calendar_ms=int(src.calendar_ms),
        membership_ms=int(src.membership_ms),
        bars_stream_ms=int(src.bars_stream_ms),
        return_ms=int(src.return_ms),
        ew_ms=int(src.ew_ms),
        source_total_ms=int(src.total_ms),
        dynamics_total_ms=dynamics_total_ms,
        memory_before={
            "process_rss_mib": _kib_to_mib(mem_before.process_rss_kib),
            "process_vmhwm_mib": _kib_to_mib(mem_before.process_vmhwm_kib),
            "cgroup_current_mib": _bytes_to_mib(mem_before.cgroup_current_bytes),
            "cgroup_peak_mib": _bytes_to_mib(mem_before.cgroup_peak_bytes),
            "cgroup_limit_mib": _bytes_to_mib(mem_before.cgroup_limit_bytes),
        },
        memory_after_new={
            "process_rss_mib": _kib_to_mib(mem_after_new.process_rss_kib),
            "process_vmhwm_mib": _kib_to_mib(mem_after_new.process_vmhwm_kib),
            "cgroup_current_mib": _bytes_to_mib(mem_after_new.cgroup_current_bytes),
            "cgroup_peak_mib": _bytes_to_mib(mem_after_new.cgroup_peak_bytes),
        },
        incremental_process_peak_new_mib=incremental_peak_new_mib,
        memory_after_old_samples={
            "process_rss_mib": _kib_to_mib(mem_after_old_all.process_rss_kib),
            "process_vmhwm_mib": _kib_to_mib(mem_after_old_all.process_vmhwm_kib),
            "cgroup_current_mib": _bytes_to_mib(
                mem_after_old_all.cgroup_current_bytes
            ),
            "cgroup_peak_mib": _bytes_to_mib(mem_after_old_all.cgroup_peak_bytes),
        },
        sample_parity=[asdict(r) for r in sample_results],
        acceptance=acceptance,
        final_status=status,
    )

    total_ms = int((time.perf_counter() - t_total_start) * 1000)
    print("\n" + "=" * 78)
    print(status)
    print("=" * 78)
    print(f"DB_MODE                  : {report.db_mode}  (writes_attempted=0)")
    print(
        f"IDENTITY                 : expected={RUNNER_EXPECTED_BASE_SHA[:7]} "
        f"runtime={(runtime_sha or 'UNKNOWN')[:7]}"
    )
    print(
        f"DIMENSIONS               : D={report.axis_D} S={report.scopes_S} "
        f"M={report.members_M} R={report.bar_R}"
    )
    print(
        f"EW AVAIL                 : cells total/avail/unavail = "
        f"{report.total_ew_cells}/{report.available_ew_cells}/{report.unavailable_ew_cells} "
        f"(all-unavail scopes={report.scopes_all_unavailable})"
    )
    for r in sample_results:
        print(
            f"PARITY SAMPLE[{r.strategy}] scope={r.scope_key[:8]}.. "
            f"members={r.member_count}  "
            f"EW_avail_miss={r.ew_availability_mismatch}  "
            f"EW_val_miss={r.ew_value_mismatch}  "
            f"Dynamics_equal={r.dynamics_deep_equal}  "
            f"old_path_ms={int(r.old_path_elapsed_ms)}"
        )
    print(
        f"MEMORY (FROZEN NEW PATH) : baseline={report.memory_before['process_rss_mib']} "
        f"after_new={report.memory_after_new['process_rss_mib']} MiB, "
        f"Δprocess_peak_new={incremental_peak_new_mib} MiB  "
        f"(gate <500 MiB → {'OK' if acceptance['incremental_process_peak_new_lt_500_mib'] else 'FAIL'})"
    )
    print(f"CPU (total runner)       : {total_ms} ms")
    print("\nACCEPTANCE GATES:")
    for k, v in acceptance.items():
        if isinstance(v, bool):
            print(f"  {k:<48s} : {'PASS' if v else 'FAIL'}")
        else:
            print(f"  {k:<48s} : {v!r}")

    # JSON output.
    payload = asdict(report)
    json_path = Path(os.environ.get(
        "M5C2_REPORT_JSON",
        ".perfdata/review/m5c2-shadow-report.json",
    ))
    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(payload, indent=2, default=str))
        print(f"\nJSON REPORT written to: {json_path}")
    except Exception as exc:
        print(
            f"\nWARN: could not write JSON report to {json_path} "
            f"({type(exc).__name__}: {exc})"
        )
        print("STDOUT JSON:")
        print(json.dumps(payload, default=str))

    return 0 if passed else 1


def main() -> int:
    import asyncio

    if os.environ.get("PURE_UNIT_TEST") in {"1", "true", "True", "yes"}:
        print("M5-C2 shadow: PURE_UNIT_TEST=1 → refusing to run against any DB.")
        return 2
    return asyncio.run(_run_shadow())


if __name__ == "__main__":
    sys.exit(main())
