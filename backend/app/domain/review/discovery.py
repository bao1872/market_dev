"""Review V2 Discovery domain: State/Change/Anomaly projection and Discovery aggregation.

Discovery = user-level market finding, aggregated from atomic Signal evidence.
State/Change/Anomaly are projected from existing scope observations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# =============================================================================
# State / Change / Anomaly domain projection
# =============================================================================


@dataclass
class MetricState:
    """当前 metric 的状态投影。"""
    code: str  # P/Q/U/C/V
    value: float | None = None
    history_percentile: float | None = None
    cross_section_percentile: float | None = None


@dataclass
class MetricChange:
    """metric 的变化投影。"""
    code: str
    delta1d: float | None = None
    delta5d: float | None = None


@dataclass
class ConcentrationState:
    """集中度状态。"""
    hhi: float | None = None
    top5_contribution: float | None = None
    leader_median_gap: float | None = None


@dataclass
class ConcentrationChange:
    """集中度变化。"""
    direction: str | None = None  # "rising" / "broadening" / "narrowing" / None
    delta1d: float | None = None


@dataclass
class InternalStructure:
    """内部结构摘要。"""
    trend_breadth: float | None = None
    structure_breadth: float | None = None
    momentum_breadth: float | None = None
    structure_breakdown_diffusion: float | None = None
    synchronized_improvement: bool = False


@dataclass
class ScopeState:
    """Scope 当前状态投影。"""
    metrics: dict[str, MetricState] = field(default_factory=dict)
    concentration: ConcentrationState = field(default_factory=ConcentrationState)
    internal_structure: InternalStructure = field(default_factory=InternalStructure)


@dataclass
class ScopeChange:
    """Scope 变化投影。"""
    metrics: dict[str, MetricChange] = field(default_factory=dict)
    concentration: ConcentrationChange = field(default_factory=ConcentrationChange)


@dataclass
class ScopeAnomaly:
    """Scope 异常投影。"""
    self_historical: dict[str, float | None] = field(default_factory=dict)
    cross_sectional: dict[str, float | None] = field(default_factory=dict)


# =============================================================================
# Discovery domain
# =============================================================================


@dataclass
class Discovery:
    """V2 user-level market finding."""
    discovery_id: str
    review_run_id: str
    trade_date: str

    # Scope
    scope_type: str
    scope_key: str
    scope_name: str

    # State / Change / Anomaly
    state: ScopeState
    change: ScopeChange
    anomaly: ScopeAnomaly

    # Evidence
    key_evidence: list[str] = field(default_factory=list)
    supporting_signal_ids: list[str] = field(default_factory=list)

    # Related
    related_scopes: list[dict[str, Any]] = field(default_factory=list)
    representative_instruments: list[dict[str, Any]] = field(default_factory=list)

    # Lifecycle
    status: str = "new"
    first_seen: str | None = None
    duration: int = 0

    # Quality
    coverage: float = 0.0
    ready_count: int = 0
    data_quality: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "discoveryId": self.discovery_id,
            "reviewRunId": self.review_run_id,
            "tradeDate": self.trade_date,
            "scope": {
                "type": self.scope_type,
                "key": self.scope_key,
                "name": self.scope_name,
            },
            "state": _state_to_dict(self.state),
            "change": _change_to_dict(self.change),
            "anomaly": _anomaly_to_dict(self.anomaly),
            "keyEvidence": self.key_evidence,
            "supportingSignalIds": self.supporting_signal_ids,
            "relatedScopes": self.related_scopes,
            "representativeInstruments": self.representative_instruments,
            "lifecycle": {
                "status": self.status,
                "firstSeen": self.first_seen,
                "duration": self.duration,
            },
            "dataQuality": {
                "coverage": self.coverage,
                "readyCount": self.ready_count,
                **self.data_quality,
            },
        }


# =============================================================================
# Projection builders
# =============================================================================


def project_state(
    p_payload: dict | None,
    q_payload: dict | None,
    u_payload: dict | None,
    c_payload: dict | None,
    v_payload: dict | None,
) -> ScopeState:
    """从 snapshot payloads 投影 ScopeState。"""
    state = ScopeState()

    for code, payload in [("P", p_payload), ("Q", q_payload), ("U", u_payload),
                           ("C", c_payload), ("V", v_payload)]:
        if payload is None:
            payload = {}
        state.metrics[code] = MetricState(
            code=code,
            value=payload.get("value"),
            history_percentile=payload.get("historyPercentile120d"),
            cross_section_percentile=payload.get("crossSectionPercentile"),
        )

    # Concentration state from C components
    c = c_payload or {}
    components = c.get("components", {}) if isinstance(c.get("components"), dict) else {}
    state.concentration = ConcentrationState(
        hhi=_component_value(components, "member_change_hhi"),
        top5_contribution=_component_value(components, "top5_price_change_contribution"),
        leader_median_gap=_component_value(components, "leader_median_diff"),
    )

    # Internal structure from Q/U components
    q = q_payload or {}
    u = u_payload or {}
    q_components = q.get("components", {}) if isinstance(q.get("components"), dict) else {}
    u_components = u.get("components", {}) if isinstance(u.get("components"), dict) else {}
    state.internal_structure = InternalStructure(
        trend_breadth=_component_value(q_components, "uptrend_member_ratio"),
        structure_breadth=_component_value(q_components, "main_structure_up_ratio"),
        momentum_breadth=_component_value(u_components, "multi_dim_improving_ratio"),
        structure_breakdown_diffusion=_component_value(q_components, "structure_breakdown_diffusion"),
        synchronized_improvement=bool(
            _component_value(u_components, "leader_follower_common_confirm_ratio", 0) > 0.5
        ),
    )

    return state


def project_change(
    p_payload: dict | None,
    q_payload: dict | None,
    u_payload: dict | None,
    c_payload: dict | None,
    v_payload: dict | None,
) -> ScopeChange:
    """从 snapshot payloads 投影 ScopeChange。"""
    change = ScopeChange()

    for code, payload in [("P", p_payload), ("Q", q_payload), ("U", u_payload),
                           ("C", c_payload), ("V", v_payload)]:
        if payload is None:
            payload = {}
        change.metrics[code] = MetricChange(
            code=code,
            delta1d=payload.get("delta1d"),
            delta5d=payload.get("delta5d"),
        )

    # Concentration change from C delta
    c = c_payload or {}
    c_delta = c.get("delta1d")
    if isinstance(c_delta, (int, float)):
        if c_delta > 0.01:
            direction = "rising"
        elif c_delta < -0.01:
            direction = "broadening" if c.get("value", 0) > 0.5 else "narrowing"
        else:
            direction = None
        change.concentration = ConcentrationChange(direction=direction, delta1d=c_delta)

    return change


def project_anomaly(
    p_payload: dict | None,
    q_payload: dict | None,
    u_payload: dict | None,
    c_payload: dict | None,
    v_payload: dict | None,
) -> ScopeAnomaly:
    """从 snapshot payloads 投影 ScopeAnomaly。"""
    anomaly = ScopeAnomaly()

    for code, payload in [("P", p_payload), ("Q", q_payload), ("U", u_payload),
                           ("C", c_payload), ("V", v_payload)]:
        if payload is None:
            payload = {}
        anomaly.self_historical[code] = payload.get("historyPercentile120d")
        anomaly.cross_sectional[code] = payload.get("crossSectionPercentile")

    return anomaly


# =============================================================================
# Discovery eligibility
# =============================================================================


def is_discovery_eligible(
    state: ScopeState,
    change: ScopeChange,
    anomaly: ScopeAnomaly,
) -> bool:
    """判断 scope 是否有资格产生 Discovery。

    Discovery 成立条件（PRD70 §10.4）：
    原则上应至少包含 State + Change 或 State + Anomaly。
    仅有静态 State 不得产生 Discovery。
    """
    has_change = _has_meaningful_change(change)
    has_anomaly = _has_meaningful_anomaly(anomaly)
    has_state = _has_meaningful_state(state)
    return has_state and (has_change or has_anomaly)


def _has_meaningful_state(state: ScopeState) -> bool:
    for m in state.metrics.values():
        if m.value is not None:
            return True
    return False


def _has_meaningful_change(change: ScopeChange) -> bool:
    for m in change.metrics.values():
        if m.delta1d is not None and abs(m.delta1d) > 0.001:
            return True
    return False


def _has_meaningful_anomaly(anomaly: ScopeAnomaly) -> bool:
    for v in anomaly.self_historical.values():
        if v is not None and (v >= 80 or v <= 20):
            return True
    for v in anomaly.cross_sectional.values():
        if v is not None and (v >= 80 or v <= 20):
            return True
    return False


# =============================================================================
# Discovery identity
# =============================================================================


def make_discovery_id(run_id: str, scope_type: str, scope_key: str) -> str:
    """Deterministic Discovery logical identity from run + scope."""
    import hashlib
    raw = f"{run_id}:{scope_type}:{scope_key}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# =============================================================================
# Discovery builder
# =============================================================================


def build_discovery(
    run_id: str,
    trade_date: str,
    scope_type: str,
    scope_key: str,
    scope_name: str,
    p_payload: dict | None,
    q_payload: dict | None,
    u_payload: dict | None,
    c_payload: dict | None,
    v_payload: dict | None,
    signal_ids: list[str] | None = None,
    coverage: float = 0.0,
    ready_count: int = 0,
) -> Discovery | None:
    """从 scope snapshot + signals 构建 Discovery。

    返回 None 如果 scope 不满足 eligibility 条件。
    """
    state = project_state(p_payload, q_payload, u_payload, c_payload, v_payload)
    change = project_change(p_payload, q_payload, u_payload, c_payload, v_payload)
    anomaly = project_anomaly(p_payload, q_payload, u_payload, c_payload, v_payload)

    if not is_discovery_eligible(state, change, anomaly):
        return None

    return Discovery(
        discovery_id=make_discovery_id(run_id, scope_type, scope_key),
        review_run_id=run_id,
        trade_date=trade_date,
        scope_type=scope_type,
        scope_key=scope_key,
        scope_name=scope_name,
        state=state,
        change=change,
        anomaly=anomaly,
        key_evidence=_build_key_evidence(state, change, anomaly),
        supporting_signal_ids=signal_ids or [],
        coverage=coverage,
        ready_count=ready_count,
        data_quality={"coverage": coverage, "readyCount": ready_count},
    )


# =============================================================================
# Helpers
# =============================================================================


def _component_value(components: dict, name: str, default: Any = None) -> Any:
    item = components.get(name)
    if isinstance(item, dict):
        return item.get("value", default)
    return default


def _build_key_evidence(
    state: ScopeState,
    change: ScopeChange,
    anomaly: ScopeAnomaly,
) -> list[str]:
    evidence: list[str] = []
    # High/low state
    for code, m in state.metrics.items():
        if m.history_percentile is not None and m.history_percentile >= 80:
            evidence.append(f"{code}_high")
        elif m.history_percentile is not None and m.history_percentile <= 20:
            evidence.append(f"{code}_low")
    # Strong change
    for code, m in change.metrics.items():
        if m.delta1d is not None and abs(m.delta1d) > 0.03:
            direction = "up" if m.delta1d > 0 else "down"
            evidence.append(f"{code}_{direction}")
    # Anomaly
    for code, v in anomaly.self_historical.items():
        if v is not None and v >= 90:
            evidence.append(f"{code}_historical_extreme")
    return evidence


def _state_to_dict(state: ScopeState) -> dict:
    return {
        "metrics": {
            code: {
                "value": m.value,
                "historyPercentile": m.history_percentile,
                "crossSectionPercentile": m.cross_section_percentile,
            }
            for code, m in state.metrics.items()
        },
        "concentration": {
            "hhi": state.concentration.hhi,
            "top5Contribution": state.concentration.top5_contribution,
            "leaderMedianGap": state.concentration.leader_median_gap,
        },
        "internalStructure": {
            "trendBreadth": state.internal_structure.trend_breadth,
            "structureBreadth": state.internal_structure.structure_breadth,
            "momentumBreadth": state.internal_structure.momentum_breadth,
            "structureBreakdownDiffusion": state.internal_structure.structure_breakdown_diffusion,
            "synchronizedImprovement": state.internal_structure.synchronized_improvement,
        },
    }


def _change_to_dict(change: ScopeChange) -> dict:
    return {
        "metrics": {
            code: {"delta1d": m.delta1d, "delta5d": m.delta5d}
            for code, m in change.metrics.items()
        },
        "concentration": {
            "direction": change.concentration.direction,
            "delta1d": change.concentration.delta1d,
        },
    }


def _anomaly_to_dict(anomaly: ScopeAnomaly) -> dict:
    return {
        "selfHistorical": anomaly.self_historical,
        "crossSectional": anomaly.cross_sectional,
    }
