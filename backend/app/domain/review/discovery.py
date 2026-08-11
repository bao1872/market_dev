"""Review V2 Discovery domain: State/Change/Anomaly projection and Discovery aggregation.

Production metric payload contract:
- value: normalized metric value (0–100 scale)
- delta1d/delta5d: normalized metric value difference
- components: LIST of dicts with name, rawValue, normalizedValue, direction, status, etc.

Discovery = user-level market finding, aggregated from atomic Signal evidence.
State/Change/Anomaly are projected from existing scope observations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# =============================================================================
# Component lookup helper
# =============================================================================


def _find_component(components: list[dict] | None, name: str) -> dict | None:
    """从 production component list 中按 name 查找 component。"""
    if not components or not isinstance(components, list):
        return None
    for c in components:
        if isinstance(c, dict) and c.get("name") == name:
            return c
    return None


def _comp_raw(components: list[dict] | None, name: str) -> float | None:
    c = _find_component(components, name)
    if c is None:
        return None
    v = c.get("rawValue")
    return float(v) if isinstance(v, (int, float)) else None


def _comp_norm(components: list[dict] | None, name: str) -> float | None:
    c = _find_component(components, name)
    if c is None:
        return None
    v = c.get("normalizedValue")
    return float(v) if isinstance(v, (int, float)) else None


def _extract_components(payload: dict | None) -> list[dict]:
    if payload is None:
        return []
    c = payload.get("components", [])
    return c if isinstance(c, list) else []


# =============================================================================
# State / Change / Anomaly domain projection
# =============================================================================


@dataclass
class MetricState:
    code: str
    value: float | None = None
    history_percentile: float | None = None
    cross_section_percentile: float | None = None


@dataclass
class MetricChange:
    code: str
    delta1d: float | None = None
    delta5d: float | None = None


@dataclass
class ConcentrationState:
    hhi: float | None = None
    top5_contribution: float | None = None
    leader_median_gap: float | None = None


@dataclass
class ConcentrationChange:
    direction: str | None = None
    delta1d: float | None = None


@dataclass
class InternalStructure:
    trend_breadth: float | None = None
    structure_breadth: float | None = None
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
# Discovery domain
# =============================================================================


@dataclass
class Discovery:
    discovery_id: str
    review_run_id: str
    trade_date: str

    scope_type: str
    scope_key: str
    scope_name: str

    state: ScopeState
    change: ScopeChange
    anomaly: ScopeAnomaly

    key_evidence: list[str] = field(default_factory=list)
    supporting_signal_ids: list[str] = field(default_factory=list)

    related_scopes: list[dict[str, Any]] = field(default_factory=list)
    representative_instruments: list[dict[str, Any]] = field(default_factory=list)

    status: str = "new"
    first_seen: str | None = None
    duration: int = 0

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
# Projection builders — production component shape
# =============================================================================


def project_state(
    p_payload: dict | None,
    q_payload: dict | None,
    u_payload: dict | None,
    c_payload: dict | None,
    v_payload: dict | None,
) -> ScopeState:
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

    c_components = _extract_components(c_payload)
    state.concentration = ConcentrationState(
        hhi=_comp_raw(c_components, "member_change_hhi"),
        top5_contribution=_comp_raw(c_components, "top5_price_change_contribution"),
        leader_median_gap=_comp_raw(c_components, "leader_median_diff"),
    )

    q_components = _extract_components(q_payload)
    u_components = _extract_components(u_payload)
    state.internal_structure = InternalStructure(
        trend_breadth=_comp_norm(q_components, "uptrend_member_ratio"),
        structure_breadth=_comp_norm(q_components, "main_structure_up_ratio"),
        momentum_breadth=_comp_norm(u_components, "multi_dim_improving_ratio"),
        structure_breakdown_diffusion=_comp_raw(q_components, "structure_breakdown_diffusion"),
        synchronized_improvement=bool(
            (_comp_raw(u_components, "leader_follower_common_confirm_ratio") or 0) > 0.5
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

    c = c_payload or {}
    c_delta = c.get("delta1d")
    if isinstance(c_delta, (int, float)):
        direction = None
        if c_delta > 1.0:
            direction = "rising"
        elif c_delta < -1.0:
            c_val = c.get("value", 0)
            direction = "broadening" if (c_val or 0) > 50 else "narrowing"
        change.concentration = ConcentrationChange(direction=direction, delta1d=c_delta)

    return change


def project_anomaly(
    p_payload: dict | None,
    q_payload: dict | None,
    u_payload: dict | None,
    c_payload: dict | None,
    v_payload: dict | None,
) -> ScopeAnomaly:
    anomaly = ScopeAnomaly()
    for code, payload in [("P", p_payload), ("Q", q_payload), ("U", u_payload),
                           ("C", c_payload), ("V", v_payload)]:
        if payload is None:
            payload = {}
        anomaly.self_historical[code] = payload.get("historyPercentile120d")
        anomaly.cross_sectional[code] = payload.get("crossSectionPercentile")
    return anomaly


# =============================================================================
# Discovery eligibility — signal evidence based
# =============================================================================


def is_discovery_eligible(
    signal_ids: list[str],
    state: ScopeState,
    change: ScopeChange,
    anomaly: ScopeAnomaly,
) -> bool:
    """Discovery 必须有 supporting atomic evidence (Signal)。

    有 Signal 的 scope 才能成为 Discovery candidate。
    纯 State/Change/Anomaly 无 Signal 不产生 Discovery。
    """
    if not signal_ids:
        return False
    return True


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
    signal_ids = signal_ids or []

    state = project_state(p_payload, q_payload, u_payload, c_payload, v_payload)
    change = project_change(p_payload, q_payload, u_payload, c_payload, v_payload)
    anomaly = project_anomaly(p_payload, q_payload, u_payload, c_payload, v_payload)

    if not is_discovery_eligible(signal_ids, state, change, anomaly):
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
        key_evidence=_build_key_evidence(signal_ids, state, change, anomaly),
        supporting_signal_ids=signal_ids,
        coverage=coverage,
        ready_count=ready_count,
        data_quality={"coverage": coverage, "readyCount": ready_count},
    )


# =============================================================================
# Key evidence from signals + state/change/anomaly
# =============================================================================


def _build_key_evidence(
    signal_ids: list[str],
    state: ScopeState,
    change: ScopeChange,
    anomaly: ScopeAnomaly,
) -> list[str]:
    evidence: list[str] = []
    evidence.append(f"signals:{len(signal_ids)}")

    for code, m in state.metrics.items():
        if m.history_percentile is not None:
            if m.history_percentile >= 80:
                evidence.append(f"{code}_high_state")
            elif m.history_percentile <= 20:
                evidence.append(f"{code}_low_state")

    for code, m in change.metrics.items():
        if m.delta1d is not None and abs(m.delta1d) > 2.0:
            direction = "up" if m.delta1d > 0 else "down"
            evidence.append(f"{code}_{direction}")

    if change.concentration.direction:
        evidence.append(f"concentration_{change.concentration.direction}")

    if state.internal_structure.synchronized_improvement:
        evidence.append("synchronized_improvement")

    return evidence


# =============================================================================
# Serialization helpers
# =============================================================================


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
