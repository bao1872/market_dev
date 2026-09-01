"""Direct tests + parity proof for the shared math primitives (AUCTION-V3.2 §5).

Scope:
- ``domain/shared/hhi.py``   (raw_hhi / normalized_hhi)
- ``domain/shared/stdev.py`` (population_stdev)

Acceptance required by §5:
  * equal shares
  * dominant member
  * N=1
  * None
  * boundary floating point
  * invalid out-of-range raw HHI  -> must RAISE, never silently clamp
  * Review HHI before == after
  * Auction Amount HHI before == after

The two ``_before_*`` helpers below are FROZEN snapshots of the pre-migration
implementations. They are TEST-ONLY reference oracles: they are never imported
by production code and are not a second semantic owner.
"""

from __future__ import annotations

import math

import pytest

from app.domain.shared.hhi import normalized_hhi, raw_hhi
from app.domain.shared.stdev import population_stdev

_EPSILON = 1e-12


# --------------------------------------------------------------------------
# frozen BEFORE snapshots (test-only oracles)
# --------------------------------------------------------------------------
def _auction_amount_hhi_before(amounts: list[float]) -> tuple[float | None, float | None]:
    """FROZEN snapshot of ``scope_fact.py`` inline amount HHI (pre-migration).

    Mirrors the retired code path verbatim:

        raw = float(np.sum(elig_amt * elig_amt)) / (scope_total * scope_total)
        n   = int(elig_amt.size)
        norm = None if n <= 1 else (raw - 1.0 / n) / (1.0 - 1.0 / n)
    """
    scope_total = sum(amounts)
    if scope_total <= 0:
        return None, None
    raw = sum(a * a for a in amounts) / (scope_total * scope_total)
    n = len(amounts)
    if n <= 1:
        return raw, None
    return raw, (raw - 1.0 / n) / (1.0 - 1.0 / n)


def _review_stdev_before(values: list[float | None]) -> float | None:
    """FROZEN snapshot of ``scope_observation._stdev`` (pre-extraction)."""
    finite = sorted(v for v in values if v is not None and math.isfinite(v))
    n = len(finite)
    if n < 2:
        return None
    mean = sum(finite) / n
    var = sum((x - mean) ** 2 for x in finite) / n
    return var**0.5


# --------------------------------------------------------------------------
# raw HHI
# --------------------------------------------------------------------------
def test_raw_hhi_equal_shares() -> None:
    assert raw_hhi([0.5, 0.5]) == pytest.approx(0.5)


def test_raw_hhi_dominant_member() -> None:
    # 0.9^2 + 0.1^2 = 0.81 + 0.01
    assert raw_hhi([0.9, 0.1]) == pytest.approx(0.82)


def test_raw_hhi_sorted_for_determinism() -> None:
    # sorted() in the primitive removes FP non-associativity across orderings.
    assert raw_hhi([0.3, 0.2, 0.5]) == raw_hhi([0.5, 0.3, 0.2])


# --------------------------------------------------------------------------
# normalized HHI
# --------------------------------------------------------------------------
def test_normalized_hhi_equal_distribution_is_zero() -> None:
    # raw = 0.5, N = 2 -> floor = 0.5 -> (0.5 - 0.5) / 0.5 = 0.0
    assert normalized_hhi(raw_hhi([0.5, 0.5]), 2) == pytest.approx(0.0)


def test_normalized_hhi_dominant_member() -> None:
    # raw = 0.82, N = 2 -> (0.82 - 0.5) / 0.5 = 0.64
    assert normalized_hhi(raw_hhi([0.9, 0.1]), 2) == pytest.approx(0.64)


def test_normalized_hhi_n1_is_none() -> None:
    # single member -> no internal concentration space
    assert normalized_hhi(1.0, 1) is None


def test_normalized_hhi_none_input_is_none() -> None:
    assert normalized_hhi(None, 5) is None


def test_normalized_hhi_boundary_float_clamped_to_zero() -> None:
    # raw sits a hair BELOW the 1/N floor purely from FP rounding.
    # value = -2e-13, which is >= -_EPSILON -> clamped to exactly 0.0.
    assert normalized_hhi(0.4999999999999, 2) == 0.0


def test_normalized_hhi_out_of_range_below_zero_raises() -> None:
    # value = (0.4 - 0.5) / 0.5 = -0.2 -> genuinely illegal, MUST surface.
    with pytest.raises(ValueError, match="out of range"):
        normalized_hhi(0.4, 2)


def test_normalized_hhi_out_of_range_above_one_raises() -> None:
    # value = (2.0 - 0.25) / 0.75 = 2.333... -> genuinely illegal.
    with pytest.raises(ValueError, match="out of range"):
        normalized_hhi(2.0, 4)


def test_normalized_hhi_never_silently_clamps_illegal_values() -> None:
    """Guard the exact §5 rule: no silent clamping of a real algorithmic error."""
    for bad_raw in (0.0, 0.1, 0.4, 1.5, 2.0):
        with pytest.raises(ValueError):
            normalized_hhi(bad_raw, 2)


def test_normalized_hhi_degenerate_floor_guard_unreachable_for_integer_n() -> None:
    """The ``1 - 1/N <= _EPSILON`` guard is DEFENSIVE: unreachable for integer N.

    ``N <= 1`` already short-circuits to None, and for every integer ``N >= 2``
    the denominator is ``1 - 1/N >= 0.5``, far above ``_EPSILON``.  Pinned so
    nobody later mistakes this guard for a reachable business branch, and so the
    None-returning contract stays limited to ``raw is None`` / ``N <= 1``.
    """
    for n in (2, 3, 10, 100, 10_000, 10**9):
        assert 1.0 - 1.0 / n > _EPSILON
    assert normalized_hhi(0.5, 1) is None
    assert normalized_hhi(None, 2) is None


# --------------------------------------------------------------------------
# population stdev
# --------------------------------------------------------------------------
def test_population_stdev_n_lt_2_is_none() -> None:
    assert population_stdev([]) is None
    assert population_stdev([1.0]) is None


def test_population_stdev_filters_non_finite() -> None:
    # None / NaN / inf are dropped, not coerced to 0 (Missing != Zero).
    assert population_stdev([1.0, None, 3.0]) == pytest.approx(1.0)
    assert population_stdev([1.0, float("nan"), 3.0]) == pytest.approx(1.0)
    assert population_stdev([1.0, float("inf"), 3.0]) == pytest.approx(1.0)


def test_population_stdev_is_population_not_sample() -> None:
    # [1,2,3,4]: population var = 1.25 -> 1.1180...; sample var = 1.6667 -> 1.2910...
    got = population_stdev([1.0, 2.0, 3.0, 4.0])
    assert got is not None
    assert got == pytest.approx(math.sqrt(1.25))
    assert got != pytest.approx(math.sqrt(5.0 / 3.0))


# --------------------------------------------------------------------------
# PARITY: Auction Amount HHI before == after
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "amounts",
    [
        [100.0, 50.0, 25.0],
        [10.0],  # N = 1
        [1.0, 1.0, 1.0, 1.0],  # perfectly equal
        [1000.0, 1.0, 1.0, 1.0],  # dominant member
        [0.5, 0.25, 0.125, 0.125],
        [7.0, 11.0, 13.0, 17.0, 19.0, 23.0],
    ],
)
def test_auction_amount_hhi_before_equals_after(amounts: list[float]) -> None:
    before_raw, before_norm = _auction_amount_hhi_before(amounts)

    total = sum(amounts)
    shares = [a / total for a in amounts]
    after_raw = raw_hhi(shares)
    after_norm = normalized_hhi(after_raw, len(amounts))

    assert before_raw == pytest.approx(after_raw, abs=1e-12)
    if before_norm is None:
        assert after_norm is None
    else:
        assert after_norm is not None
        assert before_norm == pytest.approx(after_norm, abs=1e-12)


def test_auction_amount_hhi_zero_total_unavailable_both_paths() -> None:
    # scope_total <= 0 -> explicit unavailable, never 0.
    assert _auction_amount_hhi_before([0.0, 0.0]) == (None, None)
    # shared path is only reached when total > 0; the guard itself is unchanged
    # in the production caller, so this pins the contract, not the call site.


# --------------------------------------------------------------------------
# PARITY: Review stdev before == after
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "values",
    [
        [1.0, 2.0, 3.0, 4.0],
        [0.023, -0.011, 0.005, 0.0],  # realistic ratio-scale gaps
        [1.0],  # N < 2
        [1.0, None, 3.0],  # missing filtered
        [5.0, 5.0, 5.0],  # zero dispersion
    ],
)
def test_review_stdev_before_equals_after(values: list[float | None]) -> None:
    before = _review_stdev_before(values)
    after = population_stdev(values)
    if before is None:
        assert after is None
    else:
        assert after is not None
        assert before == pytest.approx(after, abs=1e-12)
