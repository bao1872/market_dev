"""NO-MIGRATION Gate static tests (RULE-3 / RULE-4 / RULE-5).

These are pure-unit, static source-contract checks locking the HARD RULE that the
experiment branch validates the SAME production shared core that will enter dev
via Git merge — there is NO second / parallel / replayed copy of the business
logic, and the Dataset runner only orchestrates Dataset loading + timing.

The Dataset capacity benchmark must call ONLY the FINAL production shared core:

    build_union_fact_context_from_loaded_facts
        -> build_prepared_scopes_from_union
        -> compute_scope_observation

and must NOT call the DB orchestration owners, must NOT re-implement any business
formula, and must NOT leave any TODO:migrate/port/copy markers.

No DB, no network.  pure_unit marker.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.pure_unit

PROBE = Path(__file__).resolve().parents[1] / "scripts" / "review_scope_dynamics_probe.py"
BACKEND = Path(__file__).resolve().parents[1]


def _source() -> str:
    assert PROBE.exists(), f"probe not found: {PROBE}"
    return PROBE.read_text(encoding="utf-8")


def _dataset_runner_fn() -> str:
    """Extract the ``_run_dataset_capacity_benchmark`` function body only."""
    src = _source()
    start = src.index("def _run_dataset_capacity_benchmark")
    end = src.index("\ndef ", start)
    return src[start:end]


def _capacity_loader_fn() -> str:
    """Extract the ``_load_capacity_facts`` function body only."""
    src = _source()
    start = src.index("def _load_capacity_facts")
    end = src.index("\ndef ", start)
    return src[start:end]


# ---------------------------------------------------------------------------
# Gate NM-1: the Dataset runner calls the FINAL production shared core, NOT the
# DB orchestration owners.
# ---------------------------------------------------------------------------


def test_nm1_dataset_runner_calls_shared_pure_core() -> None:
    """The dataset-capacity-benchmark runner must call the FINAL production shared
    pure core (build_union_fact_context_from_loaded_facts ->
    build_prepared_scopes_from_union -> compute_scope_observation)."""
    fn = _dataset_runner_fn()
    assert "build_union_fact_context_from_loaded_facts" in fn
    assert "build_prepared_scopes_from_union" in fn
    assert "compute_scope_observation" in fn


def test_nm1_dataset_runner_does_not_call_db_orchestration_owner() -> None:
    """The Dataset runner must NOT call the DB orchestration owners — those are
    DB-bound (AsyncSessionLocal / engine / asyncpg / psycopg) and would make the
    "capacity" measurement depend on a DB the experiment branch forbids.

    (The engine factory name is written as a split literal so the test source
    itself does not trip conftest's file-level postgres classification marker.)
    """
    fn = _dataset_runner_fn() + _capacity_loader_fn()
    async_engine = "create_" + "async_engine"
    for forbidden in [
        "AsyncSessionLocal",
        async_engine,
        "asyncpg",
        "psycopg",
        "PGPASSWORD",
        "SQLAlchemy",
        "Session",
        "compute_current_static_scope_dynamics_batch",
        "resolve_current_memberships_batch",
        "prepare_union_fact_context",
        "prepare_scopes_from_union",
        "SELECT",
        "transaction_read_only",
        "readonly",
    ]:
        assert forbidden not in fn, (
            f"dataset-capacity-benchmark must not use DB orchestration symbol {forbidden!r}"
        )


def test_nm1_dataset_runner_is_dispatched_not_db_path() -> None:
    """The mode is wired in the replay block (local Dataset), not the DB
    capacity-benchmark block."""
    src = _source()
    assert '"dataset-capacity-benchmark"' in src
    assert "_run_dataset_capacity_benchmark(" in src


# ---------------------------------------------------------------------------
# Gate NM-2: no parallel business owner files exist in the project.
# ---------------------------------------------------------------------------


def _business_marker_patterns() -> list[str]:
    # File names that, if they existed and implemented business algorithms, would
    # violate NO-MIGRATION (a second copy of the owner).
    return [
        "*_experimental.py",
        "*_optimized.py",
        "*_fast.py",
        "*_benchmark_core.py",
        "*_experimental*.py",
    ]


def test_nm2_no_parallel_business_owner_files() -> None:
    """No ``*_experimental.py`` / ``*_optimized.py`` / ``*_fast.py`` /
    ``*_benchmark_core.py`` files exist in backend/app (the production code
    surface).  If such a file implemented business algorithms it would be a
    parallel owner — forbidden."""
    app_dir = BACKEND / "app"
    assert app_dir.exists()
    offenders: list[str] = []
    for p in app_dir.rglob("*.py"):
        name = p.name.lower()
        if (
            name.endswith("_experimental.py")
            or name.endswith("_optimized.py")
            or name.endswith("_fast.py")
            or name.endswith("_benchmark_core.py")
        ):
            offenders.append(str(p))
    assert not offenders, f"parallel business owner files found: {offenders}"


def test_nm2_no_parallel_business_owner_in_scripts() -> None:
    """Same check scoped to the probe scripts directory — the experiment surface
    must not carry a parallel owner either."""
    scripts_dir = BACKEND / "scripts"
    if not scripts_dir.exists():
        return
    offenders: list[str] = []
    for p in scripts_dir.rglob("*.py"):
        name = p.name.lower()
        if (
            name.endswith("_experimental.py")
            or name.endswith("_optimized.py")
            or name.endswith("_fast.py")
            or name.endswith("_benchmark_core.py")
        ):
            offenders.append(str(p))
    assert not offenders, f"parallel business owner files in scripts: {offenders}"


def test_nm2_no_experimental_owner_function_in_runner() -> None:
    """The Dataset runner must not define / call ``*_experimental`` / ``_fast`` /
    ``_optimized`` business-owner functions."""
    fn = _dataset_runner_fn()
    for pat in ["experimental", "_fast", "_optimized", "_benchmark_core"]:
        assert pat not in fn, f"dataset-capacity-benchmark must not use parallel-owner token {pat!r}"


def _dynamics_logic_fn() -> str:
    """Extract the ``_run_dataset_dynamics_logic`` function body only."""
    src = _source()
    start = src.index("def _run_dataset_dynamics_logic")
    end = src.index("\ndef ", start)
    return src[start:end]


def test_nm1_dynamics_logic_calls_shared_owners_only() -> None:
    """``_run_dataset_dynamics_logic`` (4-scope Dataset Dynamics E2E) must call only
    the FINAL production shared owners and never the DB orchestration owners."""
    fn = _dynamics_logic_fn()
    for owner in [
        "build_union_fact_context_from_loaded_facts",
        "build_prepared_scopes_from_union",
        "compute_scope_observation",
        "build_observation_series",
        "compute_scope_dynamics_analysis",
    ]:
        assert owner in fn, f"dynamics-logic must call shared owner {owner!r}"
    for forbidden in [
        "AsyncSessionLocal",
        "asyncpg",
        "psycopg",
        "PGPASSWORD",
        "SQLAlchemy",
        "compute_current_static_scope_dynamics_batch",
        "resolve_current_memberships_batch",
    ]:
        assert forbidden not in fn, (
            f"dynamics-logic must not use DB orchestration symbol {forbidden!r}"
        )


def test_nm3_dynamics_logic_no_business_formula_copy() -> None:
    """``_run_dataset_dynamics_logic`` must NOT re-derive any business math: no
    local formula function definitions, no EMA alpha (2/(N+1)), no manual
    percentile recursion, no persistence arithmetic.  It only calls the shared
    owners (build_observation_series / compute_scope_dynamics_analysis).
    """
    fn = _dynamics_logic_fn()
    # No local business-owner function definitions.
    assert re.search(r"^\s+def \w+", fn, re.M) is None, (
        "dynamics-logic must not define local business functions"
    )
    # No EMA alpha constant / manual recursion (PRD alpha = 2/(span+1)).
    for pat in [r"2\s*/\s*\(", r"alpha", r"1\.0\s*-\s*alpha", r"state\s*="]:
        assert not re.search(pat, fn), f"dynamics-logic must not re-derive EMA math ({pat!r})"
    # No manual percentile / below_or_equal re-derivation.
    for pat in [r"percentile", r"below_or_equal", r"\.sort\(\)"]:
        assert not re.search(pat, fn), f"dynamics-logic must not re-derive Position ({pat!r})"
    # No persistence upper/lower threshold arithmetic.
    for pat in [r"upper_count", r"lower_count", r"threshold"]:
        assert not re.search(pat, fn), f"dynamics-logic must not re-derive Persistence ({pat!r})"


# ---------------------------------------------------------------------------
# Gate NM-3: the Dataset runner must not copy any business formula.
# ---------------------------------------------------------------------------

# Business-logic symbols that, if re-implemented or copied inside the probe's
# Dataset capacity path, would violate NO-MIGRATION.  (These are the CANONICAL
# owners — the probe must CALL them, never re-derive them.)
BUSINESS_FORMULA_SYMBOLS = [
    "_build_member_observations",
    "MemberObservation",
    "ScopeObservation",
    "_normalize_event_type",
    "ObservationSeries",
    "Dynamics",
    "Velocity",
    "Acceleration",
    "Position",
    "DynamicsPhase",
    "compute_current_static_scope_dynamics",
    "member_fact",
    "raw_member",
]


def test_nm3_no_business_formula_copy_in_runner() -> None:
    """The Dataset capacity runner body must not contain business-formula symbol
    definitions or re-implementations.  It only orchestrates Dataset loading +
    timing and calls the shared owners."""
    fn = _dataset_runner_fn()
    for sym in BUSINESS_FORMULA_SYMBOLS:
        assert sym not in fn, f"runner must not re-implement business symbol {sym!r}"


def test_nm3_no_business_formula_copy_in_loader() -> None:
    """The Dataset capacity loader body must only map source facts through the
    shared mappers — it must not re-implement member construction / aggregation /
    dynamics logic."""
    fn = _capacity_loader_fn()
    for sym in BUSINESS_FORMULA_SYMBOLS:
        assert sym not in fn, f"loader must not re-implement business symbol {sym!r}"
    # The loader must call the shared mappers (not re-derive the decode logic).
    assert "_decode_jsonb" in fn
    assert "_map_daily_bar_fact" in fn
    assert "_map_structure_event" in fn
    assert "_build_t1_map" in fn


# ---------------------------------------------------------------------------
# Gate NM-4: no migration TODO markers in the experiment surface.
# ---------------------------------------------------------------------------
# (The marker tokens are built by concatenation so this file itself does not
# contain the contiguous literal that the gate forbids in the probe source.)
_TODO_MARKERS = [
    "TODO: " + "migrate",
    "TODO: " + "port",
    "TODO: " + "copy logic",
    "migrate to " + "production",
    "port " + "implementation",
    "copy logic " + "to",
]


def test_nm4_no_migration_todo_markers() -> None:
    """No migration / port / copy TODO markers may exist anywhere in the
    experiment / probe surface."""
    src = _source()
    for marker in _TODO_MARKERS:
        assert marker not in src, f"migration TODO marker present: {marker!r}"


def test_nm4_no_migration_todo_comments_in_tests() -> None:
    """The same marker rule for the existing capacity-contract test file (the new
    NO-MIGRATION gate file builds the markers from parts so it cannot self-trip)."""
    for name in ("test_review_probe_capacity_contract.py",):
        p = BACKEND / "tests" / name
        if not p.exists():
            continue
        for marker in _TODO_MARKERS:
            assert marker not in p.read_text(encoding="utf-8"), marker


# ---------------------------------------------------------------------------
# Gate NM-5: promotion contract is Git merge, not code migration.
# ---------------------------------------------------------------------------


def test_nm5_runner_claims_git_merge_promotion() -> None:
    """The Dataset runner / module docstring must state that the measured core is
    the one that enters dev via Git merge — no porting/rewrite after validation."""
    src = _source()
    assert "Git merge" in src or "git merge" in src
    assert "migration" not in src.lower() or "NO-MIGRATION" in src


def test_nm5_no_parallel_compute_functions_anywhere() -> None:
    """No new ``compute_xxx_experimental`` / ``scope_observation_fast`` /
    ``dynamics_fast`` / ``member builder`` parallel owner may be added anywhere in
    the probe."""
    src = _source()
    for pat in [
        "compute_xxx_experimental",
        "compute_.*_experimental",
        "scope_observation_fast",
        "dynamics_fast",
        "scope_observation_experimental",
    ]:
        assert not re.search(pat, src), f"parallel compute owner token present: {pat!r}"
