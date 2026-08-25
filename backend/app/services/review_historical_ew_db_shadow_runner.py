"""M5-C2-A Production READ-ONLY Historical EW shadow runner — container side.

Canonical owner for the M5-C2-A shadow execution.  Lives under ``backend/app``
so the audited logic is always present inside the production backend container
(Live Mount only publishes ``backend/app`` + ``backend/alembic``).

The thin dev wrapper at ``backend/scripts/review_historical_ew_db_shadow.py``
re-exports this module's ``main()`` for host testing.

Public module entrypoints:

* :func:`main` — CLI entry; return an ``int`` exit-code (shell convention).
* :func:`run_shadow` — async entry returning the full ``ShadowReport`` plus
  boolean acceptance for higher-level orchestration.

The private comparators, sample-selection helper and snapshot helpers are
deliberately exported (``compare_ew_series_parity`` etc.) so unit tests can
exercise them against the REAL canonical dict contract without touching the
database.
"""
from __future__ import annotations

import gc
import json
import math
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Canonical owners — exactly the set sanctioned by the C2-A plan.
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
# Constants.
# ---------------------------------------------------------------------------
# Provenance only — this was the C1 BASE the runner was first branched from.
# It is NOT used for runtime identity enforcement.  See
# :func:`verify_runtime_sha_identity` for the real contract.
RUNNER_SOURCE_BASE_SHA: str = "1d901f537f5de431e462b5461f5a24e821eea231"

# Canonical plan constants for this shadow.
ANALYSIS_ASOF_DATE: date = date(2026, 8, 24)
SCOPE_TYPE: str = "industry_l1"
SAMPLE_STRATEGIES: tuple[str, ...] = ("small", "median", "large")
PRODUCTION_CGROUP_LIMIT_BYTES: int = 4 * 1024**3  # 4 GiB exact
RUNTIME_SHA_HEX_RE = re.compile(r"^[0-9a-fA-F]{40}$")
EXPECTED_RUNTIME_SHA_ENV: str = "M5C2_EXPECTED_RUNTIME_SHA"
REPORT_JSON_ENV: str = "M5C2_REPORT_JSON"

# Swap target for tests (DB-free).
SESSION_FACTORY: Any = AsyncSessionLocal


# ===========================================================================
# Memory helpers / cgroup helpers — read only, never write to cgroup files.
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
# Runtime SHA identity (P1-3) — strict env + /app/RUNTIME_SHA contract.
# ===========================================================================
def _read_runtime_sha_from_repository() -> str | None:
    try:
        import subprocess  # local-only import

        top = Path(__file__).resolve().parents[3]
        out = subprocess.run(
            ["git", "-C", str(top), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        if out.returncode != 0:
            return None
        val = out.stdout.strip()[:40] or ""
        return val if RUNTIME_SHA_HEX_RE.fullmatch(val) else None
    except Exception:
        return None


def _read_runtime_sha_from_live_mount() -> str | None:
    """Priority 2: ``RUNTIME_SHA`` files next to backend/app (Live Mount)."""
    candidates = [
        Path("/app/RUNTIME_SHA"),  # priority 1 per spec
        Path(__file__).resolve().parents[2].joinpath("RUNTIME_SHA"),  # /backend/RUNTIME_SHA
        Path(__file__).resolve().parents[1].joinpath("RUNTIME_SHA"),  # /backend/app/RUNTIME_SHA
    ]
    for c in candidates:
        try:
            raw = c.read_text().strip()[:40]
            if RUNTIME_SHA_HEX_RE.fullmatch(raw):
                return raw
        except (FileNotFoundError, PermissionError):
            continue
    return None


def resolve_runtime_sha() -> str | None:
    """Return the best-effort runtime SHA, or ``None`` if unobtainable.

    Priority:
      1. ``/app/RUNTIME_SHA`` (one-shot container convention).
      2. Live-mount RUNTIME_SHA files adjacent to backend/app.
      3. ``git rev-parse HEAD`` when git is available (dev host).
    """
    # Order matters.  ``/app/RUNTIME_SHA`` is the authoritative target.
    try:
        raw = Path("/app/RUNTIME_SHA").read_text().strip()[:40]
        if RUNTIME_SHA_HEX_RE.fullmatch(raw):
            return raw
    except (FileNotFoundError, PermissionError):
        pass
    live = _read_runtime_sha_from_live_mount()
    if live is not None:
        return live
    return _read_runtime_sha_from_repository()


def verify_runtime_sha_identity(
    *,
    env_getter: Any = os.environ.get,
    runtime_sha_resolver: Any = resolve_runtime_sha,
) -> tuple[str | None, str | None, bool]:
    """Verify exact 40-char identity match.

    Returns ``(expected, actual, matches_exactly)``.  Any missing value or
    format violation yields ``matches_exactly=False`` (fail closed).
    """
    expected_raw = env_getter(EXPECTED_RUNTIME_SHA_ENV) if callable(env_getter) else None
    if not expected_raw or not RUNTIME_SHA_HEX_RE.fullmatch(str(expected_raw)):
        return (
            (expected_raw or None),
            runtime_sha_resolver() if callable(runtime_sha_resolver) else None,
            False,
        )
    expected = str(expected_raw).lower()
    actual = runtime_sha_resolver() if callable(runtime_sha_resolver) else None
    if actual is None or not RUNTIME_SHA_HEX_RE.fullmatch(str(actual)):
        return expected, actual, False
    return expected, str(actual).lower(), (expected == str(actual).lower())


# ===========================================================================
# Deterministic 3-sample selection (P1-5 now requires 3 UNIQUE keys).
# ===========================================================================
def select_deterministic_sample_scopes(
    scope_key_member_tuples: Iterable[tuple[str, int]],
) -> dict[str, str]:
    """Deterministically choose ``{small, median, large}`` → unique scope_key.

    Uses ``(member_count, scope_key)`` as the ordering key.  Fail closed when
    fewer than 3 scopes are provided (production S≥31 so this gate is
    informational but we want to guarantee uniqueness of the 3 returned
    slots even on edge data).
    """
    ordered = sorted(
        ((int(mc), str(sk)) for (sk, mc) in scope_key_member_tuples),
        key=lambda t: (t[0], t[1]),
    )
    if len(ordered) < len(SAMPLE_STRATEGIES):
        raise ValueError(
            "select_deterministic_sample_scopes: "
            f"require at least {len(SAMPLE_STRATEGIES)} scopes to yield "
            f"3 unique samples, got {len(ordered)}"
        )
    # 3 distinct indices guaranteed when len(ordered) >= 3 and indices
    # are 0 // 2 and -1.
    indices = [0, len(ordered) // 2, len(ordered) - 1]
    # Sanity: on len==3 the indices are exactly [0,1,2] — unique by
    # construction.  On len>3 (0, n//2, n-1) are also unique.
    assert len(set(indices)) == len(SAMPLE_STRATEGIES), (
        f"non-unique sample indices for len={len(ordered)}: {indices}"
    )
    out: dict[str, str] = {}
    for name, idx in zip(SAMPLE_STRATEGIES, indices):
        out[name] = ordered[idx][1]
    # Hard contract: 3 unique scope keys.
    picked = list(out.values())
    if len(set(picked)) != len(SAMPLE_STRATEGIES):
        raise ValueError(
            "select_deterministic_sample_scopes: 3-sample plan resolved to a "
            f"non-unique set: {picked}"
        )
    return out


# ===========================================================================
# Canonical OLD EW comparator — REAL dict contract.
# ===========================================================================
def validate_and_extract_ew_old_points(
    axis: Sequence[date],
    old_observation_series: Any,
) -> list[dict[str, Any]]:
    """Fail-closed structural validator for OLD canonical series.

    * Requires exact type ``dict`` at top level.
    * Requires key chain ``["primitives"]["equal_weight_return"]["points"]``.
    * ``points`` must be a ``list`` with length ``== len(axis)``.
    * Every point: ``trade_date`` is ISO string == ``axis[i]``; ``available``
      is a bool; ``value`` is consistent with availability
      (True → finite numeric; False → ``None``).
    * Any violation raises ``ValueError`` (never silently coerced).

    Returns the raw points list on success (caller runs the numeric parity).
    """
    if not isinstance(old_observation_series, dict):
        raise ValueError(
            "old_observation_series must be a dict (canonical "
            "build_observation_series() output); got type: "
            f"{type(old_observation_series).__name__}"
        )
    primitives = old_observation_series.get("primitives")
    if not isinstance(primitives, dict):
        raise ValueError(
            "old_observation_series missing 'primitives' dict key"
        )
    ewr = primitives.get("equal_weight_return")
    if not isinstance(ewr, dict):
        raise ValueError(
            "old_observation_series['primitives'] missing "
            "'equal_weight_return' dict key"
        )
    points = ewr.get("points")
    if not isinstance(points, list):
        raise ValueError(
            "old_observation_series equal_weight_return.points is not a list"
        )
    if len(points) != len(axis):
        raise ValueError(
            "old_observation_series points length mismatch: "
            f"{len(points)} != axis length {len(axis)}"
        )
    for i, (d, pt) in enumerate(zip(axis, points)):
        if not isinstance(pt, dict):
            raise ValueError(
                f"equal_weight_return.points[{i}] is not a dict"
            )
        td_raw = pt.get("trade_date")
        if not isinstance(td_raw, str):
            raise ValueError(
                f"equal_weight_return.points[{i}].trade_date is not string"
            )
        try:
            td_parsed = date.fromisoformat(td_raw[:10])
        except ValueError as exc:
            raise ValueError(
                f"equal_weight_return.points[{i}].trade_date "
                f"{td_raw!r} is not an ISO date"
            ) from exc
        if td_parsed != d:
            raise ValueError(
                f"equal_weight_return.points[{i}].trade_date mismatch: "
                f"{td_parsed.isoformat()} != axis[{i}] {d.isoformat()}"
            )
        avail = pt.get("available")
        if not isinstance(avail, bool):
            raise ValueError(
                f"equal_weight_return.points[{i}].available is not bool, "
                f"got {type(avail).__name__}"
            )
        value = pt.get("value", None)
        if avail:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"equal_weight_return.points[{i}] available=True but "
                    f"value is not a finite numeric: {value!r}"
                )
            if not math.isfinite(float(value)):
                raise ValueError(
                    f"equal_weight_return.points[{i}] available=True but "
                    f"value is non-finite: {value!r}"
                )
        else:
            if value is not None:
                raise ValueError(
                    f"equal_weight_return.points[{i}] available=False but "
                    f"value is not None: {value!r}"
                )
    return points


def compare_ew_series_parity(
    axis: Sequence[date],
    new_ew_values: Sequence[float | None],
    old_observation_series: Any,
) -> tuple[int, int]:
    """Compare NEW ew_values against OLD canonical ObservationSeries dict.

    First runs :func:`validate_and_extract_ew_old_points` which raises
    ``ValueError`` for any canonical shape violation (including the old
    fake ``to_snapshots`` object shape — an object will fail the
    ``isinstance(..., dict)`` gate at the top).

    Returns ``(availability_mismatches, value_mismatches)`` under the
    exact-float, zero-tolerance parity rule.
    """
    points = validate_and_extract_ew_old_points(axis, old_observation_series)
    avail_mismatch = 0
    value_mismatch = 0
    for t, d in enumerate(axis):
        new_val = new_ew_values[t] if t < len(new_ew_values) else None
        old_pt = points[t]
        # Availability normalisation.
        new_avail = new_val is not None and (
            isinstance(new_val, (int, float))
            and not isinstance(new_val, bool)
            and math.isfinite(float(new_val))
        )
        old_avail = bool(old_pt["available"])
        if new_avail != old_avail:
            avail_mismatch += 1
            continue
        if not new_avail and not old_avail:
            continue
        # Both finite.
        a = float(new_val)  # type: ignore[arg-type]
        b = float(old_pt["value"])
        if a != b:
            value_mismatch += 1
    return avail_mismatch, value_mismatch


# ===========================================================================
# Deep Dynamics comparator (kept; NDArray rejection moved to top).
# ===========================================================================
def deep_equal_dynamics(
    lhs: Any,
    rhs: Any,
    *,
    _path: str = "root",
) -> tuple[bool, str]:
    """Deterministic deep-equality for canonical scope_dynamics dicts.

    Same rules as C2-A: dict keys strict, list lengths strict, finite floats
    exact, ``NaN == NaN`` only, same-sign ``inf == same-sign inf`` only,
    ``ndarray`` retention hard-rejects.
    """
    # NDArray fast-fail first (the type check below would otherwise accept
    # list/ndarray by shape coercion and produce a misleading mismatch).
    if isinstance(lhs, np.ndarray) or isinstance(rhs, np.ndarray):
        return False, f"{_path} ndarray retention not allowed"
    if lhs is rhs:
        return True, ""
    if type(lhs) is not type(rhs):
        if isinstance(lhs, (list, tuple)) and isinstance(rhs, (list, tuple)):
            if len(lhs) != len(rhs):
                return False, f"{_path} length {len(lhs)} vs {len(rhs)}"
            for i, (x, y) in enumerate(zip(lhs, rhs)):
                ok, why = deep_equal_dynamics(x, y, _path=f"{_path}[{i}]")
                if not ok:
                    return False, why
            return True, ""
        return False, (
            f"{_path} type mismatch "
            f"{type(lhs).__name__} vs {type(rhs).__name__}"
        )
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
            extra = sorted(lhs_keys - rhs_keys)[:5]
            missing = sorted(rhs_keys - lhs_keys)[:5]
            return False, (
                f"{_path} keys differ — extra={extra!r} missing={missing!r}"
            )
        for k in sorted(lhs_keys):
            ok, why = deep_equal_dynamics(lhs[k], rhs[k], _path=f"{_path}.{k}")
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
    if hasattr(lhs, "__dataclass_fields__") and hasattr(rhs, "__dataclass_fields__"):
        return deep_equal_dynamics(asdict(lhs), asdict(rhs), _path=_path)
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
    runner_source_base_sha: str
    expected_runtime_sha: str | None
    actual_runtime_sha: str | None
    runtime_sha_exact_match: bool

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

    cgroup_limit_bytes: int | None
    cgroup_limit_mib: float | None
    production_cgroup_4g_confirmed: bool

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

    sample_plan_unique_3: bool
    sample_parity: list[dict[str, Any]]

    acceptance: dict[str, Any]
    final_status: str


# ===========================================================================
# READ-ONLY session factory (surface kept identical for tests).
# ===========================================================================
async def _open_readonly_session() -> tuple[AsyncSession, bool]:
    session: AsyncSession = SESSION_FACTORY()
    issued = False
    try:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        issued = True
    except Exception as exc:
        try:
            await session.rollback()
        except Exception:
            pass
        try:
            await session.close()
        except Exception:
            pass
        raise RuntimeError(
            "Failed to issue SET TRANSACTION READ ONLY for shadow session. "
            f"Inner: {type(exc).__name__}: {exc}"
        ) from exc
    return session, issued


# ===========================================================================
# Industry L1 universe SELECT exactly 2 columns, deterministic order.
# ===========================================================================
async def _fetch_industry_l1_universe(session: AsyncSession) -> list[tuple[str, str]]:
    stmt = select(MarketBoard.id, MarketBoard.name).where(
        MarketBoard.type == "industry",
        MarketBoard.hierarchyLevel == "L1",
        MarketBoard.isActive.is_(True),
    ).order_by(MarketBoard.id.asc())
    rows = (await session.execute(stmt)).all()
    return [(str(r[0]), str(r[1])) for r in rows]


# ===========================================================================
# Acceptance evaluation (standalone; unit tests exercise each gate directly).
# ===========================================================================
def evaluate_acceptance(
    *,
    read_only_confirmed: bool,
    runtime_sha_exact_match: bool,
    scopes_S: int,
    production_cgroup_4g_confirmed: bool,
    ew_lengths_all_equal_D: bool,
    all_dynamics_built: bool,
    sample_results: list[SampleParityResult],
    required_sample_names: Sequence[str],
    sample_plan_unique_3: bool,
    incremental_process_peak_new_mib: float | None,
    hard_peak_mib: float = 500.0,
) -> tuple[dict[str, Any], bool]:
    gates: dict[str, Any] = {}
    gates["read_only_confirmed"] = bool(read_only_confirmed)
    gates["runtime_sha_exact_match"] = bool(runtime_sha_exact_match)
    gates["production_cgroup_4g_confirmed"] = bool(production_cgroup_4g_confirmed)
    gates["S_ge_3"] = scopes_S >= 3
    gates["S_positive"] = scopes_S > 0
    gates["all_scope_ew_lengths_equal_D"] = bool(ew_lengths_all_equal_D)
    gates["all_scope_dynamics_outputs_built"] = bool(all_dynamics_built)
    gates["sample_plan_resolved_3_unique_keys"] = bool(sample_plan_unique_3)
    sample_complete = (
        len(sample_results) == len(required_sample_names)
        and len({r.scope_key for r in sample_results}) == len(required_sample_names)
        and [r.strategy for r in sample_results] == list(required_sample_names)
    )
    gates["3_deterministic_unique_samples_completed"] = sample_complete
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
            gates["runtime_sha_exact_match"],
            gates["production_cgroup_4g_confirmed"],
            gates["S_ge_3"],
            gates["S_positive"],
            gates["all_scope_ew_lengths_equal_D"],
            gates["all_scope_dynamics_outputs_built"],
            gates["sample_plan_resolved_3_unique_keys"],
            gates["3_deterministic_unique_samples_completed"],
            gates["sample_ew_availability_mismatch_eq_0"],
            gates["sample_ew_value_mismatch_eq_0"],
            gates["sample_dynamics_deep_equal_true"],
            gates["incremental_process_peak_new_lt_500_mib"],
        ]
    )
    return gates, all_pass


# ===========================================================================
# MAIN — actual shadow runner (executed either via ``__main__`` or CLI module).
# ===========================================================================
async def run_shadow(
    *,
    _sha_verifier: Any = verify_runtime_sha_identity,
) -> tuple[ShadowReport, bool]:
    """Execute the full production shadow and return ``(report, accepted)``.

    This is the library entrypoint used by host wrappers and container
    one-shot runners alike.  The caller is responsible for writing JSON /
    exit-code semantics.
    """
    t_total_start = time.perf_counter()

    # ------------------------------------------------------------------
    # STEP identity — strict runtime SHA first (fail closed).
    # ------------------------------------------------------------------
    expected_rt, actual_rt, rt_match = (
        _sha_verifier() if callable(_sha_verifier) else (None, None, False)
    )
    print("=" * 78)
    print("M5-C2-A  PRODUCTION READ-ONLY SHADOW RUNNER")
    print("=" * 78)
    print(f"RUNNER_SOURCE_BASE_SHA     : {RUNNER_SOURCE_BASE_SHA}")
    print(f"EXPECTED_RUNTIME_SHA (env) : {expected_rt or '(UNAVAILABLE / INVALID)'}")
    print(f"ACTUAL_RUNTIME_SHA         : {actual_rt or '(UNOBTAINABLE)'}")
    print(f"RUNTIME_SHA_EXACT_MATCH    : {'TRUE' if rt_match else 'FALSE'}")
    print(f"analysis_asof_date         : {ANALYSIS_ASOF_DATE.isoformat()}")
    print(f"scope_type                 : {SCOPE_TYPE}")

    # ------------------------------------------------------------------
    # STEP 1 — memory / cgroup baseline + 4 GiB limit check (P1-4).
    # ------------------------------------------------------------------
    mem_before = MemorySnapshot.capture()
    cgroup_limit_bytes = mem_before.cgroup_limit_bytes
    production_cgroup_4g_confirmed = bool(
        cgroup_limit_bytes is not None
        and cgroup_limit_bytes == PRODUCTION_CGROUP_LIMIT_BYTES
    )
    print("\n[STEP 1] MEMORY / CGROUP BASELINE")
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
    print(
        f"  cgroup_limit           = {cgroup_limit_bytes} bytes "
        f"({_bytes_to_mib(cgroup_limit_bytes)} MiB) "
        f"— 4GiB confirmed = {'YES' if production_cgroup_4g_confirmed else 'NO'}"
    )
    if not production_cgroup_4g_confirmed:
        print("  WARN: memory.max != 4 GiB → acceptance gate will FAIL.")

    # ------------------------------------------------------------------
    # STEP 2 — canonical production axis.
    # ------------------------------------------------------------------
    axis: list[date] | None = None
    session_main: AsyncSession | None = None
    scope_universe: list[tuple[str, str]] | None = None
    issued_readonly = False
    try:
        session_main, issued_readonly = await _open_readonly_session()
        print(f"\n[READONLY] DB_MODE = READ ONLY, writes_attempted = 0, "
              f"issued = {issued_readonly}")
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
            raise ValueError("Industry L1 active universe empty — fail closed.")
        scope_keys_ordered = [sk for sk, _nm in scope_universe]
        S = len(scope_keys_ordered)
        print("\n[STEP 3] INDUSTRY L1 UNIVERSE")
        print(f"  S = {S} scopes (deterministic id order); "
              f"S>=3 gate = {'OK' if S >= 3 else 'FAIL'}")

        # --------------------------------------------------------------
        # STEP 4 — FULL NEW PATH + FROZEN MEMORY VERDICT.
        # --------------------------------------------------------------
        mem_before_new = MemorySnapshot.capture()
        print(
            f"\n[STEP 4] NEW FULL FAMILY PATH — memory gate BEFORE — "
            f"rss={_kib_to_mib(mem_before_new.process_rss_kib)} MiB"
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
                new_scope_dynamics[sk] = {
                    "dynamics": None,
                    "observation_series": None,
                    "_error": f"{type(exc).__name__}: {exc}",
                }
        dynamics_total_ms = int((time.perf_counter() - t_dyn_start) * 1000)

        mem_after_new = MemorySnapshot.capture()
        peak_before_kib = mem_before.process_vmhwm_kib or mem_before.process_rss_kib or 0
        peak_after_new_kib = (
            mem_after_new.process_vmhwm_kib or mem_after_new.process_rss_kib or 0
        )
        incremental_peak_new_mib: float | None = (
            _kib_to_mib(peak_after_new_kib - peak_before_kib)
        )
        print(
            f"[STEP 4] NEW FULL FAMILY PATH — memory AFTER (FROZEN) — "
            f"rss={_kib_to_mib(mem_after_new.process_rss_kib)} MiB, "
            f"peak_process_after={_kib_to_mib(peak_after_new_kib)} MiB, "
            f"Δpeak_new_frozen={incremental_peak_new_mib} MiB"
        )
        src = new_result.metrics
        print(
            f"DIMENSIONS: S={src.S}, M={src.M}, R={src.R}, "
            f"scope_member_refs={src.scope_member_refs}"
        )
        print(
            f"CLOSE MATRIX: rows_streamed={src.rows_streamed}, "
            f"finite={src.finite_close_cells}, unavail={src.unavailable_close_rows}, "
            f"missing={src.missing_cells}, "
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
        # STEP 5 — Output sanity: EW cell counts & all-EW-unavailable scopes.
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
                if (
                    v is not None
                    and isinstance(v, (int, float))
                    and not isinstance(v, bool)
                    and math.isfinite(float(v))
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

        # --------------------------------------------------------------
        # STEP 6 — 3 deterministic unique scope samples OLD vs NEW parity.
        # --------------------------------------------------------------
        sample_results: list[SampleParityResult] = []
        sample_plan_unique_3 = False
        scope_key_to_member = [
            (sk, new_scopes_by_key[sk].member_count)
            for sk in scope_keys_ordered
            if sk in new_scopes_by_key
        ]
        try:
            sample_plan = select_deterministic_sample_scopes(scope_key_to_member)
            sample_plan_unique_3 = len(set(sample_plan.values())) == len(
                SAMPLE_STRATEGIES
            )
        except ValueError as exc:
            sample_plan = {}
            print(f"[STEP 6] sample plan aborted: {exc}")
        print(
            "\n[STEP 6] OLD-VS-NEW DETERMINISTIC 3-SAMPLE PARITY "
            f"(unique={sample_plan_unique_3})"
        )
        for strategy in SAMPLE_STRATEGIES:
            if strategy not in sample_plan:
                break
            sample_sk = sample_plan[strategy]
            sample_scope = new_scopes_by_key[sample_sk]
            print(
                f"  sample[{strategy:6s}] scope_key={sample_sk[:8]}.. "
                f"member_count={sample_scope.member_count}"
            )
            old_sess: AsyncSession | None = None
            old_issued = False
            mem_before_old = MemorySnapshot.capture()
            t_old_start = time.perf_counter()
            try:
                old_sess, old_issued = await _open_readonly_session()
                if not old_issued:
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
                if old_sess is not None:
                    try:
                        await old_sess.rollback()
                    except Exception:
                        pass
                    await old_sess.close()
            old_elapsed_ms = (time.perf_counter() - t_old_start) * 1000.0
            mem_after_old = MemorySnapshot.capture()
            if len(old_batch) != 1:
                sample_results.append(
                    SampleParityResult(
                        strategy=strategy, scope_key=sample_sk,
                        member_count=sample_scope.member_count,
                        ew_availability_mismatch=1, ew_value_mismatch=0,
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
            ew_a = 0
            ew_v = 0
            try:
                ew_a, ew_v = compare_ew_series_parity(
                    axis, list(sample_scope.ew_values), old_os
                )
            except ValueError as exc:
                ew_a = 1
                ew_v = 0
                sample_results.append(
                    SampleParityResult(
                        strategy=strategy, scope_key=sample_sk,
                        member_count=sample_scope.member_count,
                        ew_availability_mismatch=ew_a, ew_value_mismatch=ew_v,
                        dynamics_deep_equal=False,
                        dynamics_mismatch_path=(
                            f"old observation series canonical validation: {exc}"
                        ),
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
                continue
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
                    strategy=strategy, scope_key=sample_sk,
                    member_count=sample_scope.member_count,
                    ew_availability_mismatch=ew_a, ew_value_mismatch=ew_v,
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
        if session_main is not None:
            try:
                await session_main.rollback()
            except Exception:
                pass
            await session_main.close()
        print(f"\n[READONLY] main session closed; rollback only; commit() never called.")

    mem_after_old_all = MemorySnapshot.capture()
    src = new_result.metrics  # type: ignore[name-defined]

    # ------------------------------------------------------------------
    # STEP 7 + 8 — report assembly + acceptance verdict.
    # ------------------------------------------------------------------
    acceptance, passed = evaluate_acceptance(
        read_only_confirmed=issued_readonly,
        runtime_sha_exact_match=rt_match,
        scopes_S=S,
        production_cgroup_4g_confirmed=production_cgroup_4g_confirmed,
        ew_lengths_all_equal_D=all_ew_len_D,
        all_dynamics_built=all_dynamics_built,
        sample_results=sample_results,
        required_sample_names=SAMPLE_STRATEGIES,
        sample_plan_unique_3=sample_plan_unique_3,
        incremental_process_peak_new_mib=incremental_peak_new_mib,
    )
    status = "M5-C2 SHADOW PASS" if passed else "M5-C2 SHADOW FAIL"

    report = ShadowReport(
        runner_source_base_sha=RUNNER_SOURCE_BASE_SHA,
        expected_runtime_sha=expected_rt,
        actual_runtime_sha=actual_rt,
        runtime_sha_exact_match=rt_match,
        analysis_asof=ANALYSIS_ASOF_DATE.isoformat(),
        scope_type=SCOPE_TYPE,
        db_mode="READ ONLY",
        writes_attempted=0,
        session_set_read_only_issued=1 if issued_readonly else 0,
        axis_D=len(axis) if axis else 0,
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
        cgroup_limit_bytes=cgroup_limit_bytes,
        cgroup_limit_mib=_bytes_to_mib(cgroup_limit_bytes),
        production_cgroup_4g_confirmed=production_cgroup_4g_confirmed,
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
            "cgroup_limit_bytes": mem_before.cgroup_limit_bytes,
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
        sample_plan_unique_3=sample_plan_unique_3,
        sample_parity=[asdict(r) for r in sample_results],
        acceptance=acceptance,
        final_status=status,
    )

    total_ms = int((time.perf_counter() - t_total_start) * 1000)
    print("\n" + "=" * 78)
    print(status)
    print("=" * 78)
    print(
        f"DB_MODE                  : {report.db_mode}  (writes_attempted=0)"
    )
    print(
        f"IDENTITY                 : expected={(expected_rt or 'N/A')[:7]} "
        f"actual={(actual_rt or 'N/A')[:7]} exact_match={rt_match}"
    )
    print(
        f"DIMENSIONS               : D={report.axis_D} S={report.scopes_S} "
        f"M={report.members_M} R={report.bar_R}"
    )
    print(
        f"CGROUP                   : limit_bytes={report.cgroup_limit_bytes} "
        f"limit_mib={report.cgroup_limit_mib} 4GiB gate={production_cgroup_4g_confirmed}"
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
        f"(gate <500 MiB → "
        f"{'OK' if acceptance['incremental_process_peak_new_lt_500_mib'] else 'FAIL'})"
    )
    print(f"CPU (total runner)       : {total_ms} ms")
    print("\nACCEPTANCE GATES:")
    for k, v in acceptance.items():
        if isinstance(v, bool):
            print(f"  {k:<52s} : {'PASS' if v else 'FAIL'}")
        else:
            print(f"  {k:<52s} : {v!r}")

    # JSON output.
    payload = asdict(report)
    json_path = Path(os.environ.get(
        REPORT_JSON_ENV,
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

    return report, passed


def main() -> int:
    """CLI entrypoint.  Supports both ``scripts/`` wrapper and module mode."""
    import asyncio

    if os.environ.get("PURE_UNIT_TEST") in {"1", "true", "True", "yes"}:
        print(
            "M5-C2 shadow: PURE_UNIT_TEST=1 → refusing to run against any DB."
        )
        return 2
    _report, passed = asyncio.run(run_shadow())
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
