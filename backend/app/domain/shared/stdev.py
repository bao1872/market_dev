"""Shared population-stdev primitive — 逐字提取自 Review scope_observation。

来源：``backend/app/domain/review/scope_observation.py`` 的 ``_stdev``
（AUCTION-V3.2 §4.3 EXTRACT_TO_SHARED）。语义：population stdev，
过滤 None / 非 finite，``sorted()`` 保确定性，``n < 2`` -> None。
本模块不得修改公式；Auction Gap Dispersion 复用同一 owner，禁止自造另一种 std。
"""
from __future__ import annotations

import math
from collections.abc import Sequence


def population_stdev(values: Sequence[float | None]) -> float | None:
    """Population stdev over a finite subsequence (used for Return Dispersion).

    Returns ``None`` when fewer than 2 finite values (no dispersion space).
    """
    finite = sorted(v for v in values if v is not None and math.isfinite(v))
    n = len(finite)
    if n < 2:
        return None
    mean = sum(finite) / n
    var = sum((x - mean) ** 2 for x in finite) / n
    return var ** 0.5
