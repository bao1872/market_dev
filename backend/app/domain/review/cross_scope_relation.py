"""Review V2 Cross-Scope Relation — all 6 types executable."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RELATION_TYPES = frozenset({
    "THEME_LED", "INDUSTRY_LED", "BROAD_CONFIRMATION",
    "ISOLATED_THEME", "STYLE_LED", "CONFLICTING",
})


@dataclass
class CrossScopeRelation:
    source_scope: str
    target_scope: str
    relation_type: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceScopeId": self.source_scope,
            "targetScopeId": self.target_scope,
            "relationType": self.relation_type,
            "evidence": self.evidence,
        }


def compute_relations(
    discoveries: list[dict[str, Any]],
    scope_memberships: dict[str, set[str]] | None = None,
) -> list[CrossScopeRelation]:
    if len(discoveries) < 2:
        return []
    memberships = scope_memberships or {}
    relations: list[CrossScopeRelation] = []

    for i, d1 in enumerate(discoveries):
        for j, d2 in enumerate(discoveries):
            if i >= j:
                continue
            t1 = d1.get("scope", {}).get("type", "")
            t2 = d2.get("scope", {}).get("type", "")
            k1 = d1.get("scope", {}).get("key", "")
            k2 = d2.get("scope", {}).get("key", "")
            if not _is_supported_pair(t1, t2):
                continue
            m1 = memberships.get(k1, set())
            m2 = memberships.get(k2, set())
            overlap_ratio = len(m1 & m2) / max(len(m1 | m2), 1) if m1 and m2 else 0.0
            rel = _classify_relation(d1, d2, t1, t2, overlap_ratio)
            if rel:
                relations.append(rel)

    return relations


def _is_supported_pair(t1: str, t2: str) -> bool:
    supported = {
        ("concept", "industry_l1"), ("concept", "industry_l2"), ("concept", "industry_l3"),
        ("concept", "concept"), ("concept", "style"),
        ("industry_l1", "style"), ("industry_l2", "style"), ("industry_l3", "style"),
        ("industry_l1", "industry_l1"), ("industry_l2", "industry_l2"), ("industry_l3", "industry_l3"),
    }
    return (t1, t2) in supported or (t2, t1) in supported


def _classify_relation(d1, d2, t1, t2, overlap_ratio) -> CrossScopeRelation | None:
    d1_id = d1.get("discoveryId", "")
    d2_id = d2.get("discoveryId", "")
    q1_pct = _get_metric_history_pct(d1, "Q")
    q2_pct = _get_metric_history_pct(d2, "Q")
    u1_pct = _get_metric_history_pct(d1, "U")
    u2_pct = _get_metric_history_pct(d2, "U")

    # CONFLICTING
    if q1_pct is not None and q2_pct is not None:
        if (q1_pct >= 70 and q2_pct <= 30) or (q1_pct <= 30 and q2_pct >= 70):
            return CrossScopeRelation(d1_id, d2_id, "CONFLICTING",
                                      {"q1Percentile": q1_pct, "q2Percentile": q2_pct, "overlapRatio": overlap_ratio})

    # THEME_LED / INDUSTRY_LED
    concept, industry = _order_concept_industry(t1, t2, d1, d2)
    if concept and industry:
        c_q = _get_metric_history_pct(concept, "Q")
        i_q = _get_metric_history_pct(industry, "Q")
        if c_q is not None and i_q is not None:
            if c_q >= 70 and i_q <= 50:
                return CrossScopeRelation(concept.get("discoveryId", ""), industry.get("discoveryId", ""),
                                          "THEME_LED", {"conceptQ": c_q, "industryQ": i_q, "overlapRatio": overlap_ratio})
            if i_q >= 70 and c_q >= 60:
                return CrossScopeRelation(industry.get("discoveryId", ""), concept.get("discoveryId", ""),
                                          "INDUSTRY_LED", {"industryQ": i_q, "conceptQ": c_q, "overlapRatio": overlap_ratio})

    # STYLE_LED: style scope + multiple non-style confirmations
    style_d, other_d = _order_style(t1, t2, d1, d2)
    if style_d and other_d:
        s_q = _get_metric_history_pct(style_d, "Q")
        o_q = _get_metric_history_pct(other_d, "Q")
        if s_q is not None and s_q >= 60 and o_q is not None and o_q >= 60:
            return CrossScopeRelation(style_d.get("discoveryId", ""), other_d.get("discoveryId", ""),
                                      "STYLE_LED", {"styleQ": s_q, "otherQ": o_q, "overlapRatio": overlap_ratio})

    # BROAD_CONFIRMATION
    if q1_pct is not None and q2_pct is not None and q1_pct >= 70 and q2_pct >= 70:
        if u1_pct is not None and u2_pct is not None and u1_pct >= 60 and u2_pct >= 60:
            return CrossScopeRelation(d1_id, d2_id, "BROAD_CONFIRMATION",
                                      {"q1Percentile": q1_pct, "q2Percentile": q2_pct, "overlapRatio": overlap_ratio})

    # ISOLATED_THEME
    if concept and not industry:
        c_q = _get_metric_history_pct(concept, "Q")
        if c_q is not None and c_q >= 70 and overlap_ratio < 0.3:
            return CrossScopeRelation(concept.get("discoveryId", ""), "",
                                      "ISOLATED_THEME", {"conceptQ": c_q, "overlapRatio": overlap_ratio})

    return None


def _order_concept_industry(t1, t2, d1, d2):
    if t1.startswith("concept") and t2.startswith("industry"):
        return d1, d2
    if t2.startswith("concept") and t1.startswith("industry"):
        return d2, d1
    return None, None


def _order_style(t1, t2, d1, d2):
    if t1 == "style" and not t2.startswith("style"):
        return d1, d2
    if t2 == "style" and not t1.startswith("style"):
        return d2, d1
    return None, None


def _get_metric_history_pct(d: dict, code: str) -> float | None:
    return d.get("anomaly", {}).get("selfHistorical", {}).get(code)
