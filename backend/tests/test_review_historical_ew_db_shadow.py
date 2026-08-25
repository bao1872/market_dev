"""M5-C2 shadow runner unit tests — DB-free (PURE_UNIT_TEST=1).

Coverage (8 gates A–H as required by PERF-OOM-M5C2-A):

A. deterministic small/median/large sample selection.
B. exact EW comparator catches availability mismatch.
C. exact EW comparator catches 1-bit / float value mismatch.
D. deep Dynamics comparator (NaN eq NaN, inf sign, dict/list strict, ndarray-reject).
E. read-only transaction SET TRANSACTION READ ONLY is issued for every session.
F. there is NO commit() / persistence writer path in the runner.
G. NEW-path memory verdict is frozen before the OLD 3-sample loop runs.
H. final evaluate_acceptance returns False and nonzero on any mismatch.
"""
from __future__ import annotations

import math
import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from scripts.review_historical_ew_db_shadow import (
    MemorySnapshot,
    SampleParityResult,
    SAMPLE_STRATEGIES,
    compare_ew_series_parity,
    deep_equal_dynamics,
    evaluate_acceptance,
    select_deterministic_sample_scopes,
)


# ===========================================================================
# Test A — deterministic 3-sample selection.
# ===========================================================================
class TestASampleSelectionDeterministic:
    def test_standard_dense_11_scopes_picks_0_mid_10(self) -> None:
        # 11 scopes (member_count 1..11) → small = idx 0 (mc=1),
        # median = idx 5 (mc=6), large = idx 10 (mc=11).
        scopes = [
            (str(uuid.UUID(int=1000 + i)), i)
            for i in range(1, 12)
        ]
        sel = select_deterministic_sample_scopes(scopes)
        assert list(sel.keys()) == ["small", "median", "large"]
        mcs = {k: scopes[[s for s, _ in scopes].index(v)][1]
               for k, v in sel.items()}
        assert mcs == {"small": 1, "median": 6, "large": 11}

    def test_ties_broken_by_scope_key(self) -> None:
        # All three scopes have identical member_count. The (mc, scope_key)
        # tuple tie-breaker forces a deterministic order by UUID string.
        ids = sorted([str(uuid.UUID(int=i)) for i in (1, 2, 3)])
        scopes = [(k, 42) for k in ids]
        sel = select_deterministic_sample_scopes(scopes)
        assert sel == {"small": ids[0], "median": ids[1], "large": ids[2]}

    def test_two_scopes_dedup_to_unique_plan(self) -> None:
        # Only 2 scopes: median selection may collide with large; the
        # selection owner walks forward to an unused key so all 3 returned
        # names still have entries (and uniqueness is preserved when possible).
        ids = [str(uuid.UUID(int=5)), str(uuid.UUID(int=7))]
        scopes = [(k, 10) if i == 0 else (k, 20) for i, k in enumerate(ids)]
        sel = select_deterministic_sample_scopes(scopes)
        keys = [sel["small"], sel["median"], sel["large"]]
        # All 3 slots filled (may reuse); the set of used keys must be a subset
        # of {id0, id1}.
        assert all(k in set(ids) for k in keys)
        # With len==2 we expect small=index0, median=index1 (2//2=1),
        # large=index1 → but large collides with median → next unused = id0.
        assert sel["small"] == ids[0]

    def test_zero_scopes_fail_closed(self) -> None:
        with pytest.raises(ValueError, match="zero scopes"):
            select_deterministic_sample_scopes([])


# ===========================================================================
# Tests B & C — exact EW comparator.
# ===========================================================================
def _fake_observation_series(
    axis: list[date], values: list[Any]
) -> Any:
    """Build an old-observation-series stand-in that exposes the public
    ``to_snapshots()`` shape used by the runner."""
    class _Obs:
        def to_snapshots(self_inner):
            out = []
            for td, v in zip(axis, values):
                out.append({
                    "trade_date": td.isoformat(),
                    "payload": {"price": {"equal_weight_return": v}},
                })
            return out
    return _Obs()


AXIS_3 = [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)]


class TestBCEwComparator:
    def test_B_catches_availability_mismatch(self) -> None:
        # NEW has value at [1]; OLD returns None there.
        new = [0.05, 0.10, None]
        old_vals = [0.05, None, None]
        a_m, v_m = compare_ew_series_parity(
            AXIS_3, new, _fake_observation_series(AXIS_3, old_vals)
        )
        assert a_m == 1
        assert v_m == 0

    def test_B_catches_reverse_availability_mismatch_old_finite_new_none(self) -> None:
        new = [0.05, None, -0.01]
        old_vals = [0.05, 0.10, -0.01]
        a_m, v_m = compare_ew_series_parity(
            AXIS_3, new, _fake_observation_series(AXIS_3, old_vals)
        )
        assert a_m == 1
        assert v_m == 0

    def test_B_both_unavailable_are_exact_parity(self) -> None:
        new = [None, None, 0.0]
        old_vals = [None, float("nan"), 0.0]
        a_m, v_m = compare_ew_series_parity(
            AXIS_3, new, _fake_observation_series(AXIS_3, old_vals)
        )
        # OLD value NaN is treated non-finite → semantically unavailable.
        assert a_m == 0
        assert v_m == 0

    def test_C_exact_bit_mismatch_counts_value_mismatch(self) -> None:
        # Use 1 ULP apart floats that are definitively distinct regardless of
        # Python decimal-representation folding (0.1 parses to a single value
        # in both literals, so we pick 2 truly distinct IEEE floats).
        a = 1.0
        b = float.fromhex("0x1.0000000000001p+0")  # 1 + 2**-52
        assert a != b
        new = [a]
        old_vals = [b]
        a_m, v_m = compare_ew_series_parity(
            AXIS_3[:1], new, _fake_observation_series(AXIS_3[:1], old_vals)
        )
        assert a_m == 0
        assert v_m == 1

    def test_C_exact_same_floats_pass(self) -> None:
        values = [math.pi / 4.0, -7.0 / 9.0, 1e-9]
        new = list(values)
        # exact same float representation — pass.
        a_m, v_m = compare_ew_series_parity(
            AXIS_3, new, _fake_observation_series(AXIS_3, list(values))
        )
        assert a_m == 0
        assert v_m == 0

    def test_C_nan_not_counted_as_value_mismatch_when_both_unavailable(self) -> None:
        # NEW has None (unavail) and OLD has NaN (non-finite unavail).
        new = [None]
        old_vals = [float("nan")]
        a_m, v_m = compare_ew_series_parity(
            AXIS_3[:1], new, _fake_observation_series(AXIS_3[:1], old_vals)
        )
        assert a_m == 0
        assert v_m == 0


# ===========================================================================
# Test D — deep dynamics comparator.
# ===========================================================================
class TestDDeepDynamicsComparator:
    def test_nan_equal_nan_only(self) -> None:
        ok, why = deep_equal_dynamics(float("nan"), float("nan"))
        assert ok, why
        ok, why = deep_equal_dynamics(float("nan"), 1.0)
        assert not ok and "NaN parity" in why

    def test_same_sign_inf_equal(self) -> None:
        assert deep_equal_dynamics(float("inf"), float("inf"))[0]
        assert deep_equal_dynamics(float("-inf"), float("-inf"))[0]
        assert not deep_equal_dynamics(float("inf"), float("-inf"))[0]
        assert not deep_equal_dynamics(float("inf"), 1e308)[0]

    def test_dict_key_sets_strict(self) -> None:
        ok, why = deep_equal_dynamics(
            {"a": 1, "b": 2.0}, {"a": 1, "c": 2.0}
        )
        assert not ok and "missing" in why

    def test_lists_length_and_order_enforced(self) -> None:
        assert deep_equal_dynamics([1, 2, 3.0], [1, 2, 3.0])[0]
        ok, why = deep_equal_dynamics([1, 2], [1, 2, 3])
        assert not ok and "length" in why
        ok, why = deep_equal_dynamics([1, 2, 3], [3, 2, 1])
        assert not ok and why  # must report a mismatch at the first differing idx
        # The returned message for a first-element scalar mismatch is
        # deterministic (it mentions index 0) — check index, not "list index".
        assert "root[0]" in why

    def test_nested_dict_and_list(self) -> None:
        left = {
            "phase": {"name": "reacceleration", "strength": 1},
            "position": [0.1, 0.2, float("nan")],
            "signal": None,
        }
        right = {
            "phase": {"name": "reacceleration", "strength": 1},
            "position": [0.1, 0.2, float("nan")],
            "signal": None,
        }
        assert deep_equal_dynamics(left, right)[0]
        # mutate 1 bit in float
        right["position"][0] = 0.1 + 1e-16
        ok, why = deep_equal_dynamics(left, right)
        assert not ok and "float value" in why

    def test_ndarray_is_rejected(self) -> None:
        # The C2 result contract forbids retaining ndarrays in Dynamics.
        # Any ndarray path must hard-fail the comparator.
        arr = np.array([1.0, 2.0, 3.0])
        ok, why = deep_equal_dynamics(
            {"data": arr}, {"data": arr.tolist()}
        )
        assert not ok and "ndarray retention not allowed" in why

    def test_tuple_vs_list_shape_coerced(self) -> None:
        # Acceptable per comparator: both represent iterable ordered containers.
        assert deep_equal_dynamics((1, 2.0), [1, 2.0])[0]
        ok, why = deep_equal_dynamics((1,), [1, 2])
        assert not ok and "length" in why


# ===========================================================================
# Test E — READ ONLY transaction is issued on the session.
# ===========================================================================
class TestEReadOnlyTransactionIssued:
    @pytest.mark.asyncio
    async def test_session_executes_set_transaction_read_only(self) -> None:
        # Swap SESSION_FACTORY for a fake AsyncSession that records every
        # execute() call's SQL text and returns a compliant empty result.
        # SESSION_FACTORY in the runner is invoked SYNCHRONOUSLY (callable →
        # returns AsyncSession instance).  We must mirror that exact calling
        # convention, not return an awaitable.
        import scripts.review_historical_ew_db_shadow as shadow

        issued: list[str] = []

        class FakeResult:
            async def all(self): return []
            def scalars(self): return self

        class FakeSession:
            async def execute(self, stmt):
                # stmt is a TextClause; we compile with string context using
                # str() for simplicity.
                issued.append(str(stmt))
                return FakeResult()
            async def rollback(self): return
            async def close(self): return

        class FakeFactory:
            def __call__(self_inner):
                return FakeSession()

        original = shadow.SESSION_FACTORY
        shadow.SESSION_FACTORY = FakeFactory()
        try:
            session, issued_flag = await shadow._open_readonly_session()
            await session.close()
        finally:
            shadow.SESSION_FACTORY = original
        assert issued_flag is True
        # The captured SQL MUST include SET TRANSACTION READ ONLY.
        joined = "\n".join(issued).upper()
        assert "SET TRANSACTION READ ONLY" in joined


# ===========================================================================
# Test F — No commit or persistence helpers exist in the runner source text.
# ===========================================================================
class TestFNoWritePaths:
    def test_no_commit_save_publish_calls_in_runner_source(self) -> None:
        from pathlib import Path
        src = Path(__file__).resolve().parents[1].joinpath(
            "scripts", "review_historical_ew_db_shadow.py"
        ).read_text()
        # The runner must NEVER call these writers.
        forbidden_calls = [
            ".commit(",
            "publish_review(",
            "save_scope_observation_fact(",
            "save_scope_composition_snapshot(",
            "await session.flush(",
            "session.flush()",
        ]
        hits = [c for c in forbidden_calls if c in src]
        assert hits == [], f"write paths found in runner source: {hits}"

    def test_only_rollback_in_cleanup_blocks(self) -> None:
        from pathlib import Path
        src = Path(__file__).resolve().parents[1].joinpath(
            "scripts", "review_historical_ew_db_shadow.py"
        ).read_text()
        # Cleanup blocks call rollback; never commit.  Count is at least 2
        # (main session cleanup + per-sample session cleanup).
        assert src.count(".rollback()") >= 2
        assert src.count(".commit()") == 0


# ===========================================================================
# Test G — NEW peak frozen before OLD samples loop.
# ===========================================================================
class TestGNewPeakFrozenBeforeOldSamples:
    def test_evaluate_acceptance_uses_incremental_new_not_post_old(self) -> None:
        # Contract: incremental_process_peak_new_mib is the FROZEN verdict,
        # regardless of what the OLD loop does afterwards.
        import inspect
        sig = inspect.signature(evaluate_acceptance)
        params = list(sig.parameters.keys())
        # Only ONE peak-denominated argument represents the NEW-path peak.
        # There is NO "old_samples_peak" or "after_old" peak parameter — that
        # value cannot overwrite the frozen verdict.
        new_peak_arg = "incremental_process_peak_new_mib"
        assert new_peak_arg in params
        # There must be NO "old_samples" / "post_old" / "after_old" peak arg.
        assert not any(
            ("old" in p.lower() or "post" in p.lower() or "after" in p.lower())
            and ("peak" in p.lower() or "rss" in p.lower() or "memory" in p.lower())
            for p in params
        )
        # The unrelated hard_peak_mib constant (500 MiB) is there for
        # flexibility — it is NOT a runtime measurement; confirm it is
        # literally the gate-constant name.
        assert set(params) & {
            "hard_peak_mib",
            new_peak_arg,
        } == {new_peak_arg, "hard_peak_mib"}

    def test_incremental_peak_300_passes_but_550_fails_hard_gate(self) -> None:
        gates, passed = evaluate_acceptance(
            read_only_confirmed=True,
            scopes_S=31,
            ew_lengths_all_equal_D=True,
            all_dynamics_built=True,
            sample_results=[
                SampleParityResult(
                    strategy=strategy, scope_key=str(uuid.UUID(int=i)),
                    member_count=10 + i, ew_availability_mismatch=0,
                    ew_value_mismatch=0, dynamics_deep_equal=True,
                    dynamics_mismatch_path="", old_path_elapsed_ms=100.0,
                    process_rss_before_old_mib=None,
                    process_rss_after_old_mib=None,
                )
                for i, strategy in enumerate(SAMPLE_STRATEGIES)
            ],
            required_sample_names=SAMPLE_STRATEGIES,
            incremental_process_peak_new_mib=300.0,
        )
        assert gates["incremental_process_peak_new_lt_500_mib"] is True
        assert passed
        gates2, passed2 = evaluate_acceptance(
            read_only_confirmed=True,
            scopes_S=31,
            ew_lengths_all_equal_D=True,
            all_dynamics_built=True,
            sample_results=[
                SampleParityResult(
                    strategy=strategy, scope_key=str(uuid.UUID(int=i)),
                    member_count=10 + i, ew_availability_mismatch=0,
                    ew_value_mismatch=0, dynamics_deep_equal=True,
                    dynamics_mismatch_path="", old_path_elapsed_ms=100.0,
                    process_rss_before_old_mib=None,
                    process_rss_after_old_mib=None,
                )
                for i, strategy in enumerate(SAMPLE_STRATEGIES)
            ],
            required_sample_names=SAMPLE_STRATEGIES,
            incremental_process_peak_new_mib=550.0,
        )
        assert gates2["incremental_process_peak_new_lt_500_mib"] is False
        assert passed2 is False


# ===========================================================================
# Test H — acceptance returns nonzero on any mismatch (shell semantic).
# ===========================================================================
class TestHAcceptanceNonzeroOnMismatch:
    def _ok_samples(self) -> list[SampleParityResult]:
        return [
            SampleParityResult(
                strategy=s, scope_key=str(uuid.UUID(int=i)),
                member_count=10 + i, ew_availability_mismatch=0,
                ew_value_mismatch=0, dynamics_deep_equal=True,
                dynamics_mismatch_path="", old_path_elapsed_ms=100.0,
                process_rss_before_old_mib=None,
                process_rss_after_old_mib=None,
            )
            for i, s in enumerate(SAMPLE_STRATEGIES)
        ]

    def test_all_passing_returns_pass(self) -> None:
        _, passed = evaluate_acceptance(
            read_only_confirmed=True,
            scopes_S=31,
            ew_lengths_all_equal_D=True,
            all_dynamics_built=True,
            sample_results=self._ok_samples(),
            required_sample_names=SAMPLE_STRATEGIES,
            incremental_process_peak_new_mib=150.0,
        )
        assert passed

    def test_read_only_false_fail(self) -> None:
        _, p = evaluate_acceptance(
            read_only_confirmed=False, scopes_S=31,
            ew_lengths_all_equal_D=True, all_dynamics_built=True,
            sample_results=self._ok_samples(),
            required_sample_names=SAMPLE_STRATEGIES,
            incremental_process_peak_new_mib=150.0,
        )
        assert not p

    def test_S_zero_fail(self) -> None:
        _, p = evaluate_acceptance(
            read_only_confirmed=True, scopes_S=0,
            ew_lengths_all_equal_D=True, all_dynamics_built=True,
            sample_results=[],
            required_sample_names=SAMPLE_STRATEGIES,
            incremental_process_peak_new_mib=150.0,
        )
        assert not p

    def test_ew_len_not_D_fail(self) -> None:
        _, p = evaluate_acceptance(
            read_only_confirmed=True, scopes_S=31,
            ew_lengths_all_equal_D=False, all_dynamics_built=True,
            sample_results=self._ok_samples(),
            required_sample_names=SAMPLE_STRATEGIES,
            incremental_process_peak_new_mib=150.0,
        )
        assert not p

    def test_dynamics_not_built_fail(self) -> None:
        _, p = evaluate_acceptance(
            read_only_confirmed=True, scopes_S=31,
            ew_lengths_all_equal_D=True, all_dynamics_built=False,
            sample_results=self._ok_samples(),
            required_sample_names=SAMPLE_STRATEGIES,
            incremental_process_peak_new_mib=150.0,
        )
        assert not p

    def test_sample_ew_avail_mismatch_fail(self) -> None:
        samples = self._ok_samples()
        samples[1] = SampleParityResult(
            strategy=samples[1].strategy, scope_key=samples[1].scope_key,
            member_count=samples[1].member_count,
            ew_availability_mismatch=1, ew_value_mismatch=0,
            dynamics_deep_equal=True, dynamics_mismatch_path="",
            old_path_elapsed_ms=1.0,
            process_rss_before_old_mib=None,
            process_rss_after_old_mib=None,
        )
        _, p = evaluate_acceptance(
            read_only_confirmed=True, scopes_S=31,
            ew_lengths_all_equal_D=True, all_dynamics_built=True,
            sample_results=samples,
            required_sample_names=SAMPLE_STRATEGIES,
            incremental_process_peak_new_mib=150.0,
        )
        assert not p

    def test_sample_ew_value_mismatch_fail(self) -> None:
        samples = self._ok_samples()
        samples[0] = SampleParityResult(
            strategy=samples[0].strategy, scope_key=samples[0].scope_key,
            member_count=samples[0].member_count,
            ew_availability_mismatch=0, ew_value_mismatch=1,
            dynamics_deep_equal=True, dynamics_mismatch_path="",
            old_path_elapsed_ms=1.0,
            process_rss_before_old_mib=None,
            process_rss_after_old_mib=None,
        )
        _, p = evaluate_acceptance(
            read_only_confirmed=True, scopes_S=31,
            ew_lengths_all_equal_D=True, all_dynamics_built=True,
            sample_results=samples,
            required_sample_names=SAMPLE_STRATEGIES,
            incremental_process_peak_new_mib=150.0,
        )
        assert not p

    def test_sample_dynamics_not_equal_fail(self) -> None:
        samples = self._ok_samples()
        samples[2] = SampleParityResult(
            strategy=samples[2].strategy, scope_key=samples[2].scope_key,
            member_count=samples[2].member_count,
            ew_availability_mismatch=0, ew_value_mismatch=0,
            dynamics_deep_equal=False,
            dynamics_mismatch_path="root.phase",
            old_path_elapsed_ms=1.0,
            process_rss_before_old_mib=None,
            process_rss_after_old_mib=None,
        )
        _, p = evaluate_acceptance(
            read_only_confirmed=True, scopes_S=31,
            ew_lengths_all_equal_D=True, all_dynamics_built=True,
            sample_results=samples,
            required_sample_names=SAMPLE_STRATEGIES,
            incremental_process_peak_new_mib=150.0,
        )
        assert not p

    def test_sample_count_mismatch_fail(self) -> None:
        # Only 2 samples completed instead of 3.
        _, p = evaluate_acceptance(
            read_only_confirmed=True, scopes_S=31,
            ew_lengths_all_equal_D=True, all_dynamics_built=True,
            sample_results=self._ok_samples()[:2],
            required_sample_names=SAMPLE_STRATEGIES,
            incremental_process_peak_new_mib=150.0,
        )
        assert not p

    def test_incremental_peak_over_500_fail(self) -> None:
        _, p = evaluate_acceptance(
            read_only_confirmed=True, scopes_S=31,
            ew_lengths_all_equal_D=True, all_dynamics_built=True,
            sample_results=self._ok_samples(),
            required_sample_names=SAMPLE_STRATEGIES,
            incremental_process_peak_new_mib=None,
        )
        assert not p  # None → fail closed
        _, p = evaluate_acceptance(
            read_only_confirmed=True, scopes_S=31,
            ew_lengths_all_equal_D=True, all_dynamics_built=True,
            sample_results=self._ok_samples(),
            required_sample_names=SAMPLE_STRATEGIES,
            incremental_process_peak_new_mib=500.0,  # strict lt
        )
        assert not p
