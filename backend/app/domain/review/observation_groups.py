"""Canonical L2 Observation Groups (v2.3, §7.7).

Ownership boundary (prompt §3 / §5 / §10):

    L1 persisted canonical facts
        -> pure deterministic L2 projection
        -> 8 Observation Groups

L2 is NOT a fact-computation layer.  It ONLY reorganises already-computed L1
facts into the 8 fixed market-logic groups.  It must not recompute, score,
rank, signal, derive opportunity/risk, or mutate the input payload.

Every L2 fact is a direct reference to an existing L1 source path.  The mapping
is fixed by ``L2_GROUP_SPECS`` and verified by contract tests.  The same L1
fact referenced by two groups (e.g. segment_volume_mean_ratio in Group 3 and
Group 4) keeps identical value + source path — no second computation.

This module performs only dict reads + filtering; no DB, no membership
resolution, no historical query, no First Pyramid / VolumeContext compute.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# L1 source-path reference contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationGroupFactRef:
    """A single L2 fact: stable business key + its L1 source path.

    ``key`` is the stable business key inside the group.  ``source_path`` is the
    tuple of L1 dict keys used to read the value (deep-read, never recomputed).
    """

    key: str
    source_path: tuple[str, ...]


@dataclass(frozen=True)
class ObservationGroupSpec:
    """Fixed contract for one of the 8 Observation Groups."""

    group_key: str
    label: str
    facts: tuple[ObservationGroupFactRef, ...]


# ---------------------------------------------------------------------------
# Fixed 8-group specification (PRD §7.7)
# ---------------------------------------------------------------------------

# Group 1 — 价格与资金表现
_PRICE_CAPITAL_FACTS = (
    ObservationGroupFactRef("equal_weight_return", ("price", "equal_weight_return")),
    ObservationGroupFactRef("amount_weighted_return", ("price", "amount_weighted_return")),
    ObservationGroupFactRef("total_volume", ("price", "total_volume")),
    ObservationGroupFactRef("total_amount", ("price", "amount", "total_amount")),
    ObservationGroupFactRef("price_hhi", ("price", "concentration")),
    ObservationGroupFactRef("amount_hhi", ("price", "amount", "concentration")),
)

# Group 2 — 趋势状态
_TREND_STATE_FACTS = (
    ObservationGroupFactRef("trend_direction_member_ratio", ("trend", "state")),
    ObservationGroupFactRef("trend_strength", ("trend", "continuous", "regime_strength")),
    ObservationGroupFactRef("dsa_vwap_dev_pct", ("trend", "continuous", "dsa_vwap_dev_pct")),
)

# Group 3 — 趋势进程
_TREND_PROGRESS_FACTS = (
    ObservationGroupFactRef("current_segment_bars", ("trend", "continuous", "segment_bars")),
    ObservationGroupFactRef("segment_change_pct", ("trend", "continuous", "segment_change_pct")),
    ObservationGroupFactRef("segment_slope", ("trend", "continuous", "segment_slope")),
    ObservationGroupFactRef(
        "segment_volume_mean_ratio", ("trend", "continuous", "segment_volume_mean_ratio")
    ),
    ObservationGroupFactRef(
        "segment_amount_mean_ratio", ("trend", "continuous", "segment_amount_mean_ratio")
    ),
    ObservationGroupFactRef("vwap_ret_total", ("trend", "continuous", "vwap_ret_total")),
)

# Group 4 — 趋势量能确认 (reuses segment volume/amount mean ratio from Group 3)
_TREND_VOLUME_CONFIRMATION_FACTS = (
    ObservationGroupFactRef(
        "segment_volume_mean_ratio", ("trend", "continuous", "segment_volume_mean_ratio")
    ),
    ObservationGroupFactRef(
        "segment_amount_mean_ratio", ("trend", "continuous", "segment_amount_mean_ratio")
    ),
    ObservationGroupFactRef(
        "momentum_volume_relation", ("momentum", "momentum_volume_relation")
    ),
)

# Group 5 — 结构突破与转折 (event-type projection only)
_STRUCTURE_BREAK_TURN_FACTS = (
    ObservationGroupFactRef("bos_choch_events", ("structure", "events")),
)

# Group 6 — 结构演化与位置 (event-type projection + alignment + trailing distances)
_STRUCTURE_EVOLUTION_POSITION_FACTS = (
    ObservationGroupFactRef("ob_and_eq_events", ("structure", "events")),
    ObservationGroupFactRef("structure_alignment", ("structure", "alignment")),
    ObservationGroupFactRef(
        "distance_to_trailing_top_pct", ("structure", "distance_to_trailing_top_pct")
    ),
    ObservationGroupFactRef(
        "distance_to_trailing_bottom_pct", ("structure", "distance_to_trailing_bottom_pct")
    ),
)

# Group 7 — 动量与压缩释放
_MOMENTUM_SQUEEZE_RELEASE_FACTS = (
    ObservationGroupFactRef("squeeze_state", ("momentum", "squeeze_state")),
    ObservationGroupFactRef("bb_position", ("momentum", "bb_position")),
    ObservationGroupFactRef("bb_width", ("momentum", "bb_width")),
    ObservationGroupFactRef("release_volume_ratio", ("momentum", "release_volume_ratio")),
)

# Group 8 — 量能异常 (full six-fact Volume vector)
_VOLUME_ANOMALY_FACTS = (
    ObservationGroupFactRef("volume_ratio20", ("participation", "volume", "ratio20")),
    ObservationGroupFactRef("volume_ratio200", ("participation", "volume", "ratio200")),
    ObservationGroupFactRef("volume_percentile20", ("participation", "volume", "percentile20")),
    ObservationGroupFactRef("volume_percentile200", ("participation", "volume", "percentile200")),
    ObservationGroupFactRef("volume_zscore20", ("participation", "volume", "zscore20")),
    ObservationGroupFactRef("volume_zscore200", ("participation", "volume", "zscore200")),
)

L2_GROUP_SPECS: tuple[ObservationGroupSpec, ...] = (
    ObservationGroupSpec("price_capital", "价格与资金表现", _PRICE_CAPITAL_FACTS),
    ObservationGroupSpec("trend_state", "趋势状态", _TREND_STATE_FACTS),
    ObservationGroupSpec("trend_progress", "趋势进程", _TREND_PROGRESS_FACTS),
    ObservationGroupSpec("trend_volume_confirmation", "趋势量能确认", _TREND_VOLUME_CONFIRMATION_FACTS),
    ObservationGroupSpec("structure_break_turn", "结构突破与转折", _STRUCTURE_BREAK_TURN_FACTS),
    ObservationGroupSpec(
        "structure_evolution_position", "结构演化与位置", _STRUCTURE_EVOLUTION_POSITION_FACTS
    ),
    ObservationGroupSpec("momentum_squeeze_release", "动量与压缩释放", _MOMENTUM_SQUEEZE_RELEASE_FACTS),
    ObservationGroupSpec("volume_anomaly", "量能异常", _VOLUME_ANOMALY_FACTS),
)

# Event types projected into each structure group (prompt §6).
_BREAK_TURN_EVENT_TYPES = frozenset({"BOS", "CHoCH"})
_EVOLUTION_EVENT_TYPES = frozenset({"OB_CREATED", "OB_ENTERED", "OB_MITIGATED", "EQH", "EQL"})

# L1 event cell keys preserved verbatim (no recompute / renormalize).
_EVENT_CELL_PRESERVE_KEYS = (
    "event_type",
    "direction",
    "structure_level",
    "member_count",
    "member_ratio",
    "event_count",
)

# Forbidden L2 product-semantics keys (prompt §5.F / §9 TEST 11).
_FORBIDDEN_L2_SEMANTICS = frozenset(
    {"score", "opportunity", "risk", "recommendation", "signal", "rank"}
)


# ---------------------------------------------------------------------------
# Pure projection helpers
# ---------------------------------------------------------------------------


def _deep_get(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Read a value from ``payload`` by L1 source path.

    Returns the L1 object as-is (preserving status / unavailable / valid_count /
    denominator semantics).  Missing path -> None (caller preserves L1 absence).
    """
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def project_event_cells_by_type(
    structure_events: dict[str, Any] | None,
    allowed_event_types: frozenset[str],
) -> dict[str, Any]:
    """Thin projection: filter existing structure-event cells by event type.

    Must NOT recompute denominator / member_count / ratio / direction /
    structure_level (prompt §6).  Cells are passed through verbatim; only the
    ``cells`` list is filtered to ``allowed_event_types``.  If the input has no
    cells, returns the input unchanged (empty subset, not a fabricated 0-fact).
    """
    if not isinstance(structure_events, dict):
        return {}
    cells = structure_events.get("cells")
    if not isinstance(cells, list):
        return deepcopy(structure_events) if structure_events else {}
    filtered = [
        cell for cell in cells if isinstance(cell, dict) and cell.get("event_type") in allowed_event_types
    ]
    return {**structure_events, "cells": filtered}


def _project_group_facts(
    group: ObservationGroupSpec,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Project one group's facts from the L1 payload (read-only)."""
    facts: dict[str, Any] = {}
    for ref in group.facts:
        if ref.key == "bos_choch_events":
            facts[ref.key] = project_event_cells_by_type(
                _deep_get(payload, ref.source_path), _BREAK_TURN_EVENT_TYPES
            )
        elif ref.key == "ob_and_eq_events":
            facts[ref.key] = project_event_cells_by_type(
                _deep_get(payload, ref.source_path), _EVOLUTION_EVENT_TYPES
            )
        else:
            facts[ref.key] = _deep_get(payload, ref.source_path)
    return facts


def build_l2_observation_groups(observation_payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministically project L1 canonical facts into 8 Observation Groups.

    Pure + deterministic + non-mutating (prompt §5).  The input payload is never
    modified; nested L1 objects are returned by reference (no copy) so identical
    L1 objects shared across groups remain the exact same object/value.

    Output shape::

        {
          "price_capital": {"group_key": ..., "label": ..., "facts": {...}},
          ...  # exactly 8, fixed order
        }
    """
    return {
        spec.group_key: {
            "group_key": spec.group_key,
            "label": spec.label,
            "facts": _project_group_facts(spec, observation_payload),
        }
        for spec in L2_GROUP_SPECS
    }


__all__ = [
    "ObservationGroupFactRef",
    "ObservationGroupSpec",
    "L2_GROUP_SPECS",
    "project_event_cells_by_type",
    "build_l2_observation_groups",
]
