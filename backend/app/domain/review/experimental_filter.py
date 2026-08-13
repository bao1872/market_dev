"""Experimental Filter (Round 2B) — PURE evaluation layer.

Shadow / exploration-stage evaluation of L2-A Objective Evidence.  It answers:

    "这个 Scope 的 Evidence 是否命中了一个值得进一步检查的实验 pattern?"

NOT recommendation / opportunity / Signal / Discovery / ranking / strength.

This module is PURE (prompt §4): it consumes the ``compute_scope_evidence`` dict
returned by ``scope_evidence_service``.  It NEVER reads the DB, never reads bars,
never recomputes Observation / Evidence, never writes anything.  It does NOT import
legacy ``P/Q/U/C/V`` payloads or ``filter_definitions`` / ``filter_engine``.

Explicit exclusions (prompt §5 / §14): no score / rank / grade / strength /
confidence / recommendation / opportunity / risk / bullish / bearish output.
Concentration (``price_raw_hhi``) is DEFERRED and never used (prompt §14).
Momentum / Return primitives are NOT used as mandatory conditions (prompt §15).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Phase-1 mandatory horizons (prompt §9).  D5 is optional supporting evidence.
MANDATORY_HORIZONS: tuple[str, ...] = ("d1", "d3")
OPTIONAL_HORIZONS: tuple[str, ...] = ("d5",)

# Phase-1 archetypes (prompt §10 / §11).
ARCHETYPE_BREADTH_EXPANSION = "BREADTH_EXPANSION"
ARCHETYPE_PARTICIPATION_CONFIRMATION = "PARTICIPATION_CONFIRMATION"

# Phase-1 activated scope types (prompt §17).  Market / Major Index / Style excluded.
ACTIVATED_SCOPE_TYPES: frozenset[str] = frozenset(
    {"concept", "industry_l1", "industry_l2", "industry_l3"}
)
EXCLUDED_SCOPE_TYPES: frozenset[str] = frozenset(
    {"market", "major_index", "style"}
)

# Canonical comparison operator used by V0 (prompt §8).  We use strict ``>`` so the
# V0 "current above anchor" boundary is the lowest interpretable: delta strictly
# greater than 0.  No 0.05 / 80 percentile / 1.2x unverified params are added.
V0_OPERATOR = "gt"
V0_BOUNDARY = 0.0

# Banned output keys (prompt §5 / test M).  CandidateResult must never carry these.
BANNED_CANDIDATE_KEYS: frozenset[str] = frozenset(
    {
        "score", "rank", "grade", "strength", "confidence", "recommendation",
        "opportunity", "risk", "bullish", "bearish",
    }
)


@dataclass(frozen=True)
class ExperimentCondition:
    """One typed condition in an archetype.

    ``primitive`` is a Phase-1 Evidence primitive name; ``horizon`` is one of
    d1/d3/d5; ``mandatory`` marks whether its unavailability makes the archetype
    NOT_EVALUABLE (vs. just dropping optional supporting evidence).
    """

    condition_id: str
    primitive: str
    horizon: str
    mandatory: bool


@dataclass(frozen=True)
class ExperimentArchetype:
    """A Phase-1 experiment archetype: an ordered set of typed conditions."""

    experiment_id: str
    conditions: tuple[ExperimentCondition, ...]


@dataclass(frozen=True)
class ExperimentConfig:
    """EXPERIMENT_CONFIG_V0 (prompt §7 / §8).

    Minimal typed config; threshold is pipeline / real-data experiment only.  NOT
    claimed optimal / canonical / production.  Uses delta > 0 (current above the
    historical anchor).  No legacy FilterDefinition binding.
    """

    version: str = "EXPERIMENT_CONFIG_V0"
    operator: str = V0_OPERATOR
    boundary: float = V0_BOUNDARY


# ---------------------------------------------------------------------------
# Phase-1 archetype definitions (prompt §10 / §11).  All conditions are
# delta-based (d1.delta / d3.delta) and read the explicit delta field only;
# never compare whole dicts or use reference_value as a delta proxy.
# ---------------------------------------------------------------------------

BREADTH_EXPANSION = ExperimentArchetype(
    experiment_id=ARCHETYPE_BREADTH_EXPANSION,
    conditions=(
        ExperimentCondition("trend_up_ratio_d1", "trend_up_ratio", "d1", True),
        ExperimentCondition("trend_up_ratio_d3", "trend_up_ratio", "d3", True),
        ExperimentCondition("price_advance_ratio_d1", "price_advance_ratio", "d1", True),
        ExperimentCondition("price_advance_ratio_d3", "price_advance_ratio", "d3", True),
        # D5 optional supporting evidence (prompt §10).
        ExperimentCondition("trend_up_ratio_d5", "trend_up_ratio", "d5", False),
        ExperimentCondition("price_advance_ratio_d5", "price_advance_ratio", "d5", False),
    ),
)

PARTICIPATION_CONFIRMATION = ExperimentArchetype(
    experiment_id=ARCHETYPE_PARTICIPATION_CONFIRMATION,
    conditions=(
        ExperimentCondition("participation_volume_p50_d1", "participation_volume_p50", "d1", True),
        ExperimentCondition("participation_volume_p50_d3", "participation_volume_p50", "d3", True),
        ExperimentCondition("price_advance_ratio_d1", "price_advance_ratio", "d1", True),
        ExperimentCondition("price_advance_ratio_d3", "price_advance_ratio", "d3", True),
        # trend_up_ratio optional confirmation (prompt §11).
        ExperimentCondition("trend_up_ratio_d1", "trend_up_ratio", "d1", False),
        ExperimentCondition("trend_up_ratio_d3", "trend_up_ratio", "d3", False),
        # D5 optional supporting evidence (prompt §11).
        ExperimentCondition("participation_volume_p50_d5", "participation_volume_p50", "d5", False),
        ExperimentCondition("price_advance_ratio_d5", "price_advance_ratio", "d5", False),
    ),
)

ALL_ARCHETYPES: tuple[ExperimentArchetype, ...] = (
    BREADTH_EXPANSION,
    PARTICIPATION_CONFIRMATION,
)


def get_archetype(experiment_id: str) -> ExperimentArchetype | None:
    for a in ALL_ARCHETYPES:
        if a.experiment_id == experiment_id:
            return a
    return None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _get_delta(evidence: dict[str, Any], primitive: str, horizon: str) -> float | None:
    """Return the explicit delta of ``primitive`` at ``horizon``, or None.

    Reads only ``primitives[primitive][horizon]["delta"]`` (prompt §9).  Never
    falls back to reference_value, never compares whole dicts.  None when the
    horizon context is missing / unavailable / non-finite (never coerced to 0).
    """
    prim = evidence.get("primitives", {}).get(primitive)
    if not isinstance(prim, dict):
        return None
    ctx = prim.get(horizon)
    if not isinstance(ctx, dict):
        return None
    if ctx.get("status") != "ready":
        return None
    delta = ctx.get("delta")
    if not isinstance(delta, (int, float)) or isinstance(delta, bool):
        return None
    return float(delta)


def _condition_status(
    evidence: dict[str, Any],
    condition: ExperimentCondition,
    config: ExperimentConfig,
) -> tuple[Literal["matched", "not_matched", "unavailable"], float | None]:
    """Evaluate one condition.

    Returns (status, delta).  ``unavailable`` when the primitive/horizon delta is
    not ready (mandatory or optional alike).  ``matched`` when delta satisfies the
    V0 boundary under the configured operator.  Never coerces missing to 0.
    """
    delta = _get_delta(evidence, condition.primitive, condition.horizon)
    if delta is None:
        return "unavailable", None
    if config.operator == "gt":
        satisfied = delta > config.boundary
    elif config.operator == "ge":
        satisfied = delta >= config.boundary
    else:
        raise ValueError(f"unsupported V0 operator: {config.operator}")
    return ("matched" if satisfied else "not_matched"), delta


def _is_scope_activated(scope_type: str) -> bool:
    return scope_type in ACTIVATED_SCOPE_TYPES


# ---------------------------------------------------------------------------
# Public evaluation
# ---------------------------------------------------------------------------


def evaluate_experiment(
    evidence: dict[str, Any],
    experiment_id: str,
    config: ExperimentConfig | None = None,
) -> dict[str, Any]:
    """Evaluate one archetype against one scope's Evidence dict.

    Returns a ``CandidateResult``-shaped dict (prompt §5).  Pure and deterministic.
    The input ``evidence`` dict is NOT mutated (test L).
    """
    if config is None:
        config = ExperimentConfig()

    scope = evidence.get("scope", {})
    scope_type = scope.get("scope_type")
    trade_date = evidence.get("trade_date")

    archetype = get_archetype(experiment_id)
    if archetype is None:
        raise KeyError(f"unknown experiment_id: {experiment_id}")

    # Scope activation gate (prompt §17): excluded types cannot be evaluated.
    if scope_type in EXCLUDED_SCOPE_TYPES or not _is_scope_activated(scope_type or ""):
        return {
            "scope": scope,
            "trade_date": trade_date,
            "experiment_id": experiment_id,
            "evaluation_status": "not_evaluable",
            "matched": False,
            "conditions": [],
            "supporting_evidence": {},
            "diagnostics": {
                "mandatory_missing": ["scope_type_not_activated"],
                "optional_missing": [],
                "reason": "scope_type_not_activated",
            },
        }

    conditions_out: list[dict[str, Any]] = []
    mandatory_missing: list[str] = []
    optional_missing: list[str] = []
    all_mandatory_matched = True

    for cond in archetype.conditions:
        status, delta = _condition_status(evidence, cond, config)
        conditions_out.append(
            {
                "condition_id": cond.condition_id,
                "primitive": cond.primitive,
                "horizon": cond.horizon,
                "mandatory": cond.mandatory,
                "status": status,
                "evidence": {"delta": delta},
            }
        )
        if status == "unavailable":
            if cond.mandatory:
                mandatory_missing.append(cond.condition_id)
                all_mandatory_matched = False
            else:
                optional_missing.append(cond.condition_id)
        elif status == "not_matched":
            if cond.mandatory:
                all_mandatory_matched = False

    # Three-state resolution (prompt §6).
    if mandatory_missing:
        evaluation_status: str = "not_evaluable"
        matched = False
    else:
        evaluation_status = "evaluable"
        matched = all_mandatory_matched

    # supporting_evidence: historical / peer context already present in Evidence,
    # surfaced verbatim (read-only, no recompute).  Never blocks evaluation (§12/§13).
    supporting_evidence: dict[str, Any] = {}
    for prim_name, prim in evidence.get("primitives", {}).items():
        if not isinstance(prim, dict):
            continue
        hist = prim.get("historical")
        if isinstance(hist, dict) and hist.get("status") == "ready":
            supporting_evidence.setdefault(prim_name, {})["historical_percentile"] = hist.get(
                "percentile"
            )
        peer = prim.get("peer")
        if isinstance(peer, dict) and peer.get("status") == "ready":
            supporting_evidence.setdefault(prim_name, {})["peer_percentile"] = peer.get("percentile")

    return {
        "scope": scope,
        "trade_date": trade_date,
        "experiment_id": experiment_id,
        "evaluation_status": evaluation_status,
        "matched": matched,
        "conditions": conditions_out,
        "supporting_evidence": supporting_evidence,
        "diagnostics": {
            "mandatory_missing": mandatory_missing,
            "optional_missing": optional_missing,
        },
    }


def evaluate_scope(
    evidence: dict[str, Any],
    config: ExperimentConfig | None = None,
) -> list[dict[str, Any]]:
    """Evaluate all Phase-1 archetypes for one scope's Evidence dict."""
    if config is None:
        config = ExperimentConfig()
    return [evaluate_experiment(evidence, a.experiment_id, config) for a in ALL_ARCHETYPES]


def candidate_result_has_banned_keys(result: dict[str, Any]) -> bool:
    """Return True if ``result`` (or nested) carries a banned output key (test M)."""
    flat = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                flat.add(str(k).lower())
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(result)
    return bool(flat & BANNED_CANDIDATE_KEYS)
