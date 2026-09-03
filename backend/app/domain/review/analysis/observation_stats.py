"""Pure 20D rolling diagnostics helpers for Review scope history.

No DB, no future leakage. These are the *only* place the 20D rolling math lives;
the read-model owner (`review_scope_diagnostics_service`) calls them.

Contract (task spec / PRD §7.6 / §7.7.5):
- ``baseline(T)`` uses history strictly BEFORE T (excludes T itself).
- ``mean20[T] = mean(T-20 .. T-1)``; ``std20[T] = population std`` of the same window.
- ``zscore20[T] = (value[T] - mean20[T]) / std20[T]``; if ``std == 0`` OR the
  baseline has < 2 finite samples -> ``None`` (never a fake ``z = 0``).
- ``null / unavailable != 0``: missing values are EXCLUDED from the baseline,
  never coerced to 0, never forward-filled.
- ``percentile20`` is the empirical percentile rank of the current value within
  its own trailing window (self-inclusive), matching the L1 / cross-sectional
  convention ``(count(p < v) + 0.5 * count(p == v)) / N * 100``.
"""
from __future__ import annotations

import math
from collections.abc import Sequence


def safe_mean(values: Sequence[float | None]) -> float | None:
    """Mean of finite values; ``None`` when no finite value exists."""
    finite = [v for v in values if v is not None and math.isfinite(v)]
    if not finite:
        return None
    return sum(finite) / len(finite)


def safe_variance(values: Sequence[float | None]) -> float | None:
    """Population variance of finite values; ``None`` when < 2 finite values.

    [SLICE 4 / Price] Same owner + same population definition as ``safe_std`` so
    the two are mathematically consistent (``std == sqrt(variance)``). The
    frontend must NEVER derive variance as ``std ** 2`` — variance is a
    first-class backend fact.

    - null / non-finite are EXCLUDED (never coerced to 0);
    - population (``/ n``), not sample (``/ (n - 1)``);
    - ``n < 2`` -> ``None``.
    """
    finite = [v for v in values if v is not None and math.isfinite(v)]
    n = len(finite)
    if n < 2:
        return None
    m = sum(finite) / n
    return sum((x - m) ** 2 for x in finite) / n


def safe_std(values: Sequence[float | None]) -> float | None:
    """Population std of finite values; ``None`` when < 2 finite values."""
    finite = [v for v in values if v is not None and math.isfinite(v)]
    n = len(finite)
    if n < 2:
        return None
    m = sum(finite) / n
    var = sum((x - m) ** 2 for x in finite) / n
    return math.sqrt(var)


def zscore(
    value: float | None, mean: float | None, std: float | None
) -> float | None:
    """``(value - mean) / std``; ``None`` when value/std/mean missing or ``std == 0``."""
    if value is None or mean is None or std is None:
        return None
    if not math.isfinite(std) or std == 0:
        return None
    return (value - mean) / std


def empirical_percentile(
    value: float | None, samples: Sequence[float | None]
) -> float | None:
    """Percentile rank of ``value`` within ``samples`` (self-inclusive), ``[0, 100]``.

    Convention matches L1 / C1 cross-sectional:
    ``(count(p < v) + 0.5 * count(p == v)) / N * 100``.
    """
    if value is None or not math.isfinite(value):
        return None
    finite = [v for v in samples if v is not None and math.isfinite(v)]
    if not finite:
        return None
    less = sum(1 for v in finite if v < value)
    equal = sum(1 for v in finite if v == value)
    n = len(finite)
    return (less + 0.5 * equal) / n * 100.0


__all__ = ["safe_mean", "safe_std", "zscore", "empirical_percentile"]
