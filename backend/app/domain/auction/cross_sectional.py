"""Auction V3.2 cross-sectional structure positions (V3.2 §十一).

Same-family only:

    industry <-> industry
    concept  <-> concept

The two peer cohorts are NEVER merged into one ranking universe.

Four independent observation axes are produced, each as 0..100 position
evidence.  They are deliberately NOT collapsed into a composite score — a
single number would hide *which* kind of extremity a board exhibits.

The percentile math is not re-implemented here: everything delegates to the
shared :func:`app.domain.shared.historical_position.percentile_rank` owner.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.shared.historical_position import percentile_rank

__all__ = [
    "AXES",
    "CrossSectionalPositions",
    "axis_primary_metric",
    "axis_primary_positions",
    "compute_cross_sectional",
]

#: The four axes and the metric keys each one reads.  A metric may appear in
#: only one axis, so the axes stay independent.
AXES: dict[str, tuple[str, ...]] = {
    "repricing": ("equal_weight_gap", "amount_weighted_gap", "capital_tilt"),
    "breadth": (
        "positive_gap_breadth",
        "negative_gap_breadth",
        "unchanged_gap_breadth",
        "gap_dispersion",
    ),
    "participation": (
        "amount_historical_position",
        "amount_multiple",
        "amount_abnormal_breadth",
    ),
    "concentration": (
        "price_normalized_hhi",
        "normalized_hhi",
        "top3_amount_share",
    ),
}


@dataclass(frozen=True)
class CrossSectionalPositions:
    """Per-axis 0..100 positions for one scope, relative to its family peers."""

    repricing: dict[str, float | None]
    breadth: dict[str, float | None]
    participation: dict[str, float | None]
    concentration: dict[str, float | None]


def _collect(values_by_scope: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    out: list[float] = []
    for row in values_by_scope:
        v = row.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        out.append(float(v))
    return out


def axis_primary_metric(axis: str) -> str:
    """Return the representative metric for one cross-sectional axis.

    The rule is "the first metric declared for that axis" — a single,
    self-maintaining rule owned here.  The API must never pick a metric on its
    own, and the list DTO carries ONE position per axis.
    """
    return AXES[axis][0]


def axis_primary_positions(
    axis_positions: Mapping[str, Any],
) -> dict[str, float | None]:
    """Flatten axis -> metric -> position into axis -> primary position.

    Used for list display, where the UI shows one cross-sectional position per
    axis (涨跌 / 扩散 / 成交 / 集中度).  Missing stays ``None``, never 0.
    """
    flat: dict[str, float | None] = {}
    for axis in AXES:
        metrics = axis_positions.get(axis)
        if not isinstance(metrics, Mapping):
            flat[axis] = None
            continue
        flat[axis] = metrics.get(axis_primary_metric(axis))
    return flat


def compute_cross_sectional(
    values_by_scope: Sequence[Mapping[str, Any]],
) -> dict[str, CrossSectionalPositions]:
    """Compute same-family cross-sectional positions for every scope.

    Args:
        values_by_scope: one mapping per scope **within a single family**;
            each mapping must carry ``scope_key`` plus the metric keys listed in
            :data:`AXES`.  Missing metrics yield ``None`` (never 0) and are
            excluded from that metric's peer sample.

    Returns:
        ``scope_key -> CrossSectionalPositions``.
    """
    result: dict[str, CrossSectionalPositions] = {}
    for row in values_by_scope:
        scope_key = row["scope_key"]
        axes: dict[str, dict[str, float | None]] = {}
        for axis, keys in AXES.items():
            positions: dict[str, float | None] = {}
            for key in keys:
                value = row.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    positions[key] = None
                    continue
                peers = _collect(values_by_scope, key)
                positions[key] = percentile_rank(float(value), peers)
            axes[axis] = positions
        result[scope_key] = CrossSectionalPositions(
            repricing=axes["repricing"],
            breadth=axes["breadth"],
            participation=axes["participation"],
            concentration=axes["concentration"],
        )
    return result
