"""PERF-PROBE-CLEANUP — probe capacity-benchmark contract.

These are static, pure-unit contract checks against the probe source:

  - the legacy A/B performance benchmark runtime path is GONE
    (_vec1_benchmark / _measure_all_scopes / _observations_close / legacy member
    build / legacy-vs-vec1 observation comparison / --benchmark-scopes /
    --sample-bar-members / measure-all-scopes / vec1-benchmark)
  - the only performance mode is ``capacity-benchmark``, which calls ONLY the
    optimized production owner ``compute_current_static_scope_dynamics_batch``
    and never the legacy / single / manual-union-prep functions
  - the read-only transaction guard uses ``SET TRANSACTION READ ONLY`` +
    ``SHOW transaction_read_only`` (fail-closed)

No DB, no network.  pure_unit marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.pure_unit

PROBE = Path(__file__).resolve().parents[1] / "scripts" / "review_scope_dynamics_probe.py"


def _source() -> str:
    assert PROBE.exists(), f"probe not found: {PROBE}"
    return PROBE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Legacy A/B performance benchmark removed
# ---------------------------------------------------------------------------

LEGACY_FORBIDDEN = [
    "_vec1_benchmark",
    "_measure_all_scopes",
    "_observations_close",
    "legacy_members",
    "legacy_member_build_ms",
    "legacy_prepared_count",
    "measure-all-scopes",
    "vec1-benchmark",
    "--benchmark-scopes",
    "--sample-bar-members",
    "async def _probe(",
    "def _probe(",
]


@pytest.mark.parametrize("sym", LEGACY_FORBIDDEN)
def test_legacy_ab_symbol_removed(sym: str) -> None:
    """PERF-PROBE-CLEANUP-FINAL: legacy A/B + single performance probe symbols are absent."""
    src = _source()
    assert sym not in src, f"legacy/single symbol still present: {sym!r}"


def test_capacity_mode_in_choices() -> None:
    """capacity-benchmark is the (only) performance mode in the CLI choices."""
    src = _source()
    assert '"capacity-benchmark"' in src
    # single performance mode is gone
    assert '"single"' not in src
    # legacy performance modes are gone
    assert '"vec1-benchmark"' not in src
    assert '"measure-all-scopes"' not in src


def test_single_mode_absent() -> None:
    """PERF-PROBE-CLEANUP-FINAL: the single performance probe is removed entirely.

    (The module filename ``review_scope_dynamics_probe`` legitimately contains
    ``_probe``; the check is on the function definition / call, not the bare
    substring.)
    """
    src = _source()
    assert "def _probe(" not in src
    assert "_probe(" not in src


# ---------------------------------------------------------------------------
# capacity-benchmark calls ONLY the optimized production owner
# ---------------------------------------------------------------------------


def _capacity_fn_source() -> str:
    src = _source()
    start = src.index("async def _capacity_benchmark")
    # take the whole function up to the next top-level def.
    end = src.index("\ndef ", start)
    return src[start:end]


def test_capacity_calls_batch_owner() -> None:
    """capacity-benchmark calls compute_current_static_scope_dynamics_batch."""
    assert "compute_current_static_scope_dynamics_batch" in _capacity_fn_source()


def test_capacity_does_not_call_legacy_or_single() -> None:
    """capacity-benchmark does NOT call the legacy / single / manual-union-prep
    functions (it delegates all computation to the batch production owner)."""
    fn = _capacity_fn_source()
    for forbidden in [
        "_build_member_observations",
        "prepare_union_fact_context",
        "prepare_scopes_from_union",
        "compute_scope_observation",
        "union_member_cap=",
    ]:
        assert forbidden not in fn, f"capacity-benchmark must not call {forbidden!r}"


def test_capacity_scope_family_filtering() -> None:
    """PERF-PROBE-CLEANUP-FINAL: membership / overlap ranking is restricted to the
    current scope_type family (candidate_board_ids), so industry / other-family
    membership never pollutes the concept-overlap sample."""
    fn = _capacity_fn_source()
    # it builds the candidate board id set and filters memberships by it
    assert "candidate_board_ids" in fn
    assert "not in candidate_board_ids" in fn
    assert "continue" in fn


def test_capacity_wording_shadow_path() -> None:
    """PERF-PROBE-CLEANUP-FINAL: capacity-benchmark is described as an optimized
    batch owner / shadow production-bound path, NOT a production-wired owner."""
    fn = _capacity_fn_source()
    assert "shadow" in fn
    assert "production-bound" in fn


def test_capacity_readonly_guard() -> None:
    """capacity-benchmark uses SET TRANSACTION READ ONLY + SHOW fail-closed."""
    fn = _capacity_fn_source()
    assert "SET TRANSACTION READ ONLY" in fn
    assert "SHOW transaction_read_only" in fn
    assert "if ro != \"on\":" in fn
    assert "rollback()" in fn


def test_capacity_passes_scope_count_and_history() -> None:
    """capacity-benchmark resolves scope_count + history into the batch call."""
    fn = _capacity_fn_source()
    assert "scope_count" in fn
    assert "history" in fn
    assert "scope_keys" in fn
    # union_member_cap is NOT passed — the batch owner's default is the only source.
    assert "union_member_cap=" not in fn
