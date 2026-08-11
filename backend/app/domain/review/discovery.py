"""Review V2 Discovery domain — consumes canonical Signal semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# =============================================================================
# Component helpers
# =============================================================================

def _find_component(components: list[dict] | None, name: str) -> dict | None:
    if not components or not isinstance(components, list):
        return None
    for c in components:
        if isinstance(c, dict) and c.get("name") == name:
            return c
    return None

def _comp_raw(components: list[dict] | None, name: str) -> float | None:
    c = _find_component(components, name)
    if c is None: return None
    v = c.get("rawValue")
    return float(v) if isinstance(v, (int, float)) else None

def _comp_norm(components: list[dict] | None, name: str) -> float | None:
    c = _find_component(components, name)
    if c is None: return None
    v = c.get("normalizedValue")
    return float(v) if isinstance(v, (int, float)) else None

def _extract_components(payload: dict | None) -> list[dict]:
    if payload is None: return []
    c = payload.get("components", [])
    return c if isinstance(c, list) else []

# =============================================================================
# State / Change / Anomaly
# =============================================================================

@dataclass
class MetricState:
    code: str; value: float | None = None
    history_percentile: float | None = None
    cross_section_percentile: float | None = None

@dataclass
class MetricChange:
    code: str; delta1d: float | None = None; delta5d: float | None = None

@dataclass
class ConcentrationState:
    hhi: float | None = None; top5_contribution: float | None = None
    leader_median_gap: float | None = None

@dataclass
class ConcentrationChange:
    direction: str | None = None; delta1d: float | None = None

@dataclass
class InternalStructure:
    trend_breadth: float | None = None; structure_breadth: float | None = None
    momentum_breadth: float | None = None
    structure_breakdown_diffusion: float | None = None
    synchronized_improvement: bool = False

@dataclass
class ScopeState:
    metrics: dict[str, MetricState] = field(default_factory=dict)
    concentration: ConcentrationState = field(default_factory=ConcentrationState)
    internal_structure: InternalStructure = field(default_factory=InternalStructure)

@dataclass
class ScopeChange:
    metrics: dict[str, MetricChange] = field(default_factory=dict)
    concentration: ConcentrationChange = field(default_factory=ConcentrationChange)

@dataclass
class ScopeAnomaly:
    self_historical: dict[str, float | None] = field(default_factory=dict)
    cross_sectional: dict[str, float | None] = field(default_factory=dict)

# =============================================================================
# Discovery
# =============================================================================

@dataclass
class Discovery:
    discovery_id: str; review_run_id: str; trade_date: str
    scope_type: str; scope_key: str; scope_name: str
    state: ScopeState; change: ScopeChange; anomaly: ScopeAnomaly
    key_evidence: list[str] = field(default_factory=list)
    supporting_signal_ids: list[str] = field(default_factory=list)
    related_scopes: list[dict[str, Any]] = field(default_factory=list)
    representative_instruments: list[dict[str, Any]] = field(default_factory=list)
    status: str = "new"; first_seen: str | None = None; duration: int = 0
    coverage: float = 0.0; ready_count: int = 0
    data_quality: dict[str, Any] = field(default_factory=dict)
    rank_key: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "discoveryId": self.discovery_id,
            "reviewRunId": self.review_run_id,
            "tradeDate": self.trade_date,
            "scope": {"type": self.scope_type, "key": self.scope_key, "name": self.scope_name},
            "state": _state_to_dict(self.state),
            "change": _change_to_dict(self.change),
            "anomaly": _anomaly_to_dict(self.anomaly),
            "keyEvidence": self.key_evidence,
            "supportingSignalIds": self.supporting_signal_ids,
            "relatedScopes": self.related_scopes,
            "representativeInstruments": self.representative_instruments,
            "lifecycle": {"status": self.status, "firstSeen": self.first_seen, "duration": self.duration},
            "dataQuality": {"coverage": self.coverage, "readyCount": self.ready_count, **self.data_quality},
            "rankKey": self.rank_key,
        }

# =============================================================================
# Projection
# =============================================================================

def project_state(p_payload, q_payload, u_payload, c_payload, v_payload) -> ScopeState:
    state = ScopeState()
    for code, payload in [("P", p_payload), ("Q", q_payload), ("U", u_payload), ("C", c_payload), ("V", v_payload)]:
        if payload is None: payload = {}
        state.metrics[code] = MetricState(code=code, value=payload.get("value"),
            history_percentile=payload.get("historyPercentile120d"),
            cross_section_percentile=payload.get("crossSectionPercentile"))
    c_comp = _extract_components(c_payload)
    state.concentration = ConcentrationState(hhi=_comp_raw(c_comp, "member_change_hhi"),
        top5_contribution=_comp_raw(c_comp, "top5_price_change_contribution"),
        leader_median_gap=_comp_raw(c_comp, "leader_median_diff"))
    q_comp = _extract_components(q_payload); u_comp = _extract_components(u_payload)
    state.internal_structure = InternalStructure(
        trend_breadth=_comp_norm(q_comp, "uptrend_member_ratio"),
        structure_breadth=_comp_norm(q_comp, "main_structure_up_ratio"),
        momentum_breadth=_comp_norm(u_comp, "multi_dim_improving_ratio"),
        structure_breakdown_diffusion=_comp_raw(q_comp, "structure_breakdown_diffusion"),
        synchronized_improvement=bool((_comp_raw(u_comp, "leader_follower_common_confirm_ratio") or 0) > 0.5))
    return state

def project_change(p_payload, q_payload, u_payload, c_payload, v_payload) -> ScopeChange:
    change = ScopeChange()
    for code, payload in [("P", p_payload), ("Q", q_payload), ("U", u_payload), ("C", c_payload), ("V", v_payload)]:
        if payload is None: payload = {}
        change.metrics[code] = MetricChange(code=code, delta1d=payload.get("delta1d"), delta5d=payload.get("delta5d"))
    c = c_payload or {}; c_delta = c.get("delta1d")
    if isinstance(c_delta, (int, float)):
        direction = None
        if c_delta > 1.0: direction = "rising"
        elif c_delta < -1.0: direction = "broadening" if (c.get("value", 0) or 0) > 50 else "narrowing"
        change.concentration = ConcentrationChange(direction=direction, delta1d=c_delta)
    return change

def project_anomaly(p_payload, q_payload, u_payload, c_payload, v_payload) -> ScopeAnomaly:
    anomaly = ScopeAnomaly()
    for code, payload in [("P", p_payload), ("Q", q_payload), ("U", u_payload), ("C", c_payload), ("V", v_payload)]:
        if payload is None: payload = {}
        anomaly.self_historical[code] = payload.get("historyPercentile120d")
        anomaly.cross_sectional[code] = payload.get("crossSectionPercentile")
    return anomaly

# =============================================================================
# Signal evidence classification — consumes canonical Signal semantics
# =============================================================================

# Filters whose evidence semantics are "change" (detecting delta/trend)
CHANGE_FILTERS = frozenset({
    "surface_weak_internal_improving",   # A2: Q/U delta1d historical percentile
    "high_level_slowing",                # B1: Q/U/V delta1d percentile
    "low_level_repair",                  # B2: Q/U delta1d percentile
    "volume_without_breadth",            # C1: V delta
    "breadth_without_volume",            # C2: U delta
    "synchronized_expansion",            # C3: U/V delta
})

# Filters whose evidence semantics are "anomaly" (extreme relative position)
ANOMALY_FILTERS = frozenset({
    "surface_strong_internal_weak",      # A1: P high, (P-Q) extreme
})

# Filters whose evidence semantics are primarily "state" (static position)
STATE_FILTERS = frozenset({
    "concentration_high",                # D4: static hhi/top5 threshold
})

# D1/D2/D3/D5: state migration / freshness / breadth / relative strength
# These are "change" evidence when they indicate directional movement
D_CHANGE_FILTERS = frozenset({
    "state_migration_positive",          # D1
    "event_freshness_high",             # D2
    "breadth_expansion",                # D3
    "relative_strength_strong",         # D5
})


def classify_signal_evidence(signal_type: str, filter_family: str) -> dict[str, bool]:
    """Classify a Signal's evidence semantics: state/change/anomaly.

    Consumes canonical signal_type and filter_family from versioned filter definitions.
    Returns {'is_state': bool, 'is_change': bool, 'is_anomaly': bool}.
    """
    result = {"is_state": False, "is_change": False, "is_anomaly": False}
    if signal_type in CHANGE_FILTERS or signal_type in D_CHANGE_FILTERS:
        result["is_change"] = True
    if signal_type in ANOMALY_FILTERS:
        result["is_anomaly"] = True
    if signal_type in STATE_FILTERS:
        result["is_state"] = True
    # Any signal that hits means state evidence exists
    if not any(result.values()):
        result["is_state"] = True
    return result


def is_discovery_eligible(signal_types: list[str], signal_families: list[str]) -> bool:
    """Discovery eligibility from canonical Signal evidence.

    Requires: Signal evidence AND (Change evidence OR Anomaly evidence).
    Pure state-only signals (e.g. D4 concentration_high) alone cannot create Discovery.
    """
    if not signal_types:
        return False
    has_change = False
    has_anomaly = False
    for st, sf in zip(signal_types, signal_families):
        cls = classify_signal_evidence(st, sf)
        if cls["is_change"]:
            has_change = True
        if cls["is_anomaly"]:
            has_anomaly = True
    return has_change or has_anomaly

# =============================================================================
# Identity — includes run_id for deterministic resolution
# =============================================================================

def make_discovery_id(run_id: str, scope_type: str, scope_key: str) -> str:
    import hashlib
    return hashlib.sha256(f"{run_id}:{scope_type}:{scope_key}".encode()).hexdigest()[:12]

# =============================================================================
# Builder
# =============================================================================

def build_discovery(
    run_id, trade_date, scope_type, scope_key, scope_name,
    p_payload, q_payload, u_payload, c_payload, v_payload,
    signal_ids=None, signal_types=None, signal_families=None,
    signal_statuses=None, signal_first_seens=None,
    coverage=0.0, ready_count=0,
) -> Discovery | None:
    signal_ids = signal_ids or []
    signal_types = signal_types or []
    signal_families = signal_families or []
    if not is_discovery_eligible(signal_types, signal_families):
        return None
    state = project_state(p_payload, q_payload, u_payload, c_payload, v_payload)
    change = project_change(p_payload, q_payload, u_payload, c_payload, v_payload)
    anomaly = project_anomaly(p_payload, q_payload, u_payload, c_payload, v_payload)
    lifecycle = _derive_lifecycle(signal_statuses, signal_first_seens)
    return Discovery(
        discovery_id=make_discovery_id(run_id, scope_type, scope_key),
        review_run_id=run_id, trade_date=trade_date,
        scope_type=scope_type, scope_key=scope_key, scope_name=scope_name,
        state=state, change=change, anomaly=anomaly,
        key_evidence=_build_key_evidence(signal_types, state, change, anomaly),
        supporting_signal_ids=signal_ids,
        status=lifecycle["status"], first_seen=lifecycle["first_seen"],
        duration=lifecycle["duration"],
        coverage=coverage, ready_count=ready_count,
        data_quality={"coverage": coverage, "readyCount": ready_count},
    )

def _derive_lifecycle(signal_statuses, signal_first_seens):
    statuses = signal_statuses or []
    first_seens = signal_first_seens or []
    status = "new"
    if "confirmed" in statuses: status = "confirmed"
    elif "weakened" in statuses: status = "weakened"
    elif "invalidated" in statuses: status = "invalidated"
    elif "transformed" in statuses: status = "transformed"
    elif "continuing" in statuses: status = "continuing"
    valid_dates = [d for d in first_seens if d]
    first_seen = min(valid_dates) if valid_dates else None
    duration = len(set(first_seens)) if first_seens else 0
    return {"status": status, "first_seen": first_seen, "duration": duration}

def _build_key_evidence(signal_types, state, change, anomaly):
    evidence = [f"signals:{len(signal_types)}"]
    evidence.extend(signal_types)
    for code, m in state.metrics.items():
        if m.history_percentile is not None:
            if m.history_percentile >= 80: evidence.append(f"{code}_high_state")
            elif m.history_percentile <= 20: evidence.append(f"{code}_low_state")
    if change.concentration.direction:
        evidence.append(f"concentration_{change.concentration.direction}")
    if state.internal_structure.synchronized_improvement:
        evidence.append("synchronized_improvement")
    return evidence

# =============================================================================
# Serialization
# =============================================================================

def _state_to_dict(state):
    return {"metrics": {code: {"value": m.value, "historyPercentile": m.history_percentile,
        "crossSectionPercentile": m.cross_section_percentile} for code, m in state.metrics.items()},
        "concentration": {"hhi": state.concentration.hhi, "top5Contribution": state.concentration.top5_contribution,
                          "leaderMedianGap": state.concentration.leader_median_gap},
        "internalStructure": {"trendBreadth": state.internal_structure.trend_breadth,
            "structureBreadth": state.internal_structure.structure_breadth,
            "momentumBreadth": state.internal_structure.momentum_breadth,
            "structureBreakdownDiffusion": state.internal_structure.structure_breakdown_diffusion,
            "synchronizedImprovement": state.internal_structure.synchronized_improvement}}

def _change_to_dict(change):
    return {"metrics": {code: {"delta1d": m.delta1d, "delta5d": m.delta5d} for code, m in change.metrics.items()},
            "concentration": {"direction": change.concentration.direction, "delta1d": change.concentration.delta1d}}

def _anomaly_to_dict(anomaly):
    return {"selfHistorical": anomaly.self_historical, "crossSectional": anomaly.cross_sectional}
