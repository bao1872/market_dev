"""Review V2 Discovery domain unit tests.

Tests for: State/Change/Anomaly projection, Discovery aggregation,
Cross-Scope Relation, global ranking, Discovery identity.
"""

from app.domain.review.discovery import (
    Discovery,
    ScopeState,
    ScopeChange,
    ScopeAnomaly,
    project_state,
    project_change,
    project_anomaly,
    is_discovery_eligible,
    build_discovery,
    make_discovery_id,
)
from app.domain.review.cross_scope_relation import (
    CrossScopeRelation,
    compute_relations,
    RELATION_TYPES,
)


# =============================================================================
# State / Change / Anomaly projection
# =============================================================================


def _sample_payloads():
    return (
        {"value": 0.6, "delta1d": 0.02, "delta5d": 0.05, "historyPercentile120d": 70.0,
         "crossSectionPercentile": 65.0},
        {"value": 0.5, "delta1d": 0.01, "delta5d": 0.03, "historyPercentile120d": 60.0,
         "crossSectionPercentile": 55.0,
         "components": {
             "uptrend_member_ratio": {"value": 0.52},
             "main_structure_up_ratio": {"value": 0.61},
             "structure_breakdown_diffusion": {"value": 0.15},
         }},
        {"value": 0.7, "delta1d": 0.05, "delta5d": 0.08, "historyPercentile120d": 80.0,
         "crossSectionPercentile": 75.0,
         "components": {
             "multi_dim_improving_ratio": {"value": 0.45},
             "leader_follower_common_confirm_ratio": {"value": 0.55},
         }},
        {"value": 0.3, "delta1d": -0.01, "delta5d": 0.02, "historyPercentile120d": 40.0,
         "crossSectionPercentile": 35.0,
         "components": {
             "member_change_hhi": {"value": 0.12},
             "top5_price_change_contribution": {"value": 0.38},
             "leader_median_diff": {"value": 0.05},
         }},
        {"value": 0.4, "delta1d": 0.03, "delta5d": 0.06, "historyPercentile120d": 55.0,
         "crossSectionPercentile": 50.0},
    )


class TestStateProjection:
    def test_project_state_all_metrics(self):
        p, q, u, c, v = _sample_payloads()
        state = project_state(p, q, u, c, v)
        assert len(state.metrics) == 5
        assert state.metrics["P"].value == 0.6
        assert state.metrics["Q"].history_percentile == 60.0
        assert state.concentration.hhi == 0.12
        assert state.concentration.top5_contribution == 0.38
        assert state.internal_structure.trend_breadth == 0.52
        assert state.internal_structure.structure_breadth == 0.61
        assert state.internal_structure.structure_breakdown_diffusion == 0.15
        assert state.internal_structure.synchronized_improvement is True

    def test_project_state_none_payloads(self):
        state = project_state(None, None, None, None, None)
        assert len(state.metrics) == 5
        assert state.metrics["P"].value is None


class TestChangeProjection:
    def test_project_change(self):
        p, q, u, c, v = _sample_payloads()
        change = project_change(p, q, u, c, v)
        assert change.metrics["P"].delta1d == 0.02
        assert change.metrics["Q"].delta5d == 0.03
        # C delta=-0.01, value=0.3 → not rising, not high enough for broadening → None
        assert change.concentration.delta1d == -0.01

    def test_concentration_rising(self):
        c = {"value": 0.8, "delta1d": 0.05}
        change = project_change(None, None, None, c, None)
        assert change.concentration.direction == "rising"


class TestAnomalyProjection:
    def test_project_anomaly(self):
        p, q, u, c, v = _sample_payloads()
        anomaly = project_anomaly(p, q, u, c, v)
        assert anomaly.self_historical["P"] == 70.0
        assert anomaly.cross_sectional["Q"] == 55.0


# =============================================================================
# Discovery eligibility
# =============================================================================


class TestDiscoveryEligibility:
    def test_state_only_not_eligible(self):
        state = ScopeState()
        state.metrics["P"] = type("m", (), {"value": 0.6})()
        change = ScopeChange()
        anomaly = ScopeAnomaly()
        assert is_discovery_eligible(state, change, anomaly) is False

    def test_state_plus_change_eligible(self):
        state = ScopeState()
        state.metrics["P"] = type("m", (), {"value": 0.6})()
        change = ScopeChange()
        change.metrics["P"] = type("m", (), {"delta1d": 0.05})()
        anomaly = ScopeAnomaly()
        assert is_discovery_eligible(state, change, anomaly) is True

    def test_state_plus_anomaly_eligible(self):
        state = ScopeState()
        state.metrics["P"] = type("m", (), {"value": 0.6})()
        change = ScopeChange()
        anomaly = ScopeAnomaly()
        anomaly.self_historical["P"] = 85.0
        assert is_discovery_eligible(state, change, anomaly) is True


# =============================================================================
# Discovery identity
# =============================================================================


class TestDiscoveryIdentity:
    def test_deterministic(self):
        id1 = make_discovery_id("run-1", "industry_l1", "electronics")
        id2 = make_discovery_id("run-1", "industry_l1", "electronics")
        assert id1 == id2
        assert len(id1) == 12

    def test_different_scope_different_id(self):
        id1 = make_discovery_id("run-1", "industry_l1", "electronics")
        id2 = make_discovery_id("run-1", "concept", "glass_substrate")
        assert id1 != id2


# =============================================================================
# Discovery builder
# =============================================================================


class TestBuildDiscovery:
    def test_build_with_eligible_scope(self):
        p, q, u, c, v = _sample_payloads()
        discovery = build_discovery(
            run_id="run-1", trade_date="2026-08-11",
            scope_type="industry_l1", scope_key="electronics",
            scope_name="电子",
            p_payload=p, q_payload=q, u_payload=u, c_payload=c, v_payload=v,
            signal_ids=["sig-1", "sig-2"],
            coverage=0.95, ready_count=30,
        )
        assert discovery is not None
        assert discovery.scope_type == "industry_l1"
        assert len(discovery.supporting_signal_ids) == 2
        assert len(discovery.key_evidence) > 0
        d = discovery.to_dict()
        assert d["scope"]["type"] == "industry_l1"
        assert d["state"]["metrics"]["P"]["value"] == 0.6
        assert d["lifecycle"]["status"] == "new"

    def test_build_state_only_no_discovery(self):
        p = {"value": 0.6}
        discovery = build_discovery(
            run_id="run-1", trade_date="2026-08-11",
            scope_type="industry_l1", scope_key="electronics",
            scope_name="电子",
            p_payload=p, q_payload=None, u_payload=None, c_payload=None, v_payload=None,
        )
        assert discovery is None


# =============================================================================
# Cross-Scope Relation
# =============================================================================


class TestCrossScopeRelation:
    def test_no_relations_for_single_discovery(self):
        d = {
            "discoveryId": "d1", "scope": {"type": "concept", "key": "c1"},
            "state": {"metrics": {}}, "anomaly": {"selfHistorical": {}},
        }
        relations = compute_relations([d])
        assert relations == []

    def test_theme_led(self):
        concept = {
            "discoveryId": "d1", "scope": {"type": "concept", "key": "c1"},
            "state": {"metrics": {"Q": {"value": 0.7}}},
            "anomaly": {"selfHistorical": {"Q": 75.0}},
        }
        industry = {
            "discoveryId": "d2", "scope": {"type": "industry_l1", "key": "i1"},
            "state": {"metrics": {"Q": {"value": 0.3}}},
            "anomaly": {"selfHistorical": {"Q": 35.0}},
        }
        relations = compute_relations([concept, industry])
        assert len(relations) >= 1
        types = {r.relation_type for r in relations}
        assert "THEME_LED" in types or "CONFLICTING" in types

    def test_broad_confirmation(self):
        d1 = {
            "discoveryId": "d1", "scope": {"type": "industry_l1", "key": "i1"},
            "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 75.0, "U": 70.0}},
        }
        d2 = {
            "discoveryId": "d2", "scope": {"type": "industry_l1", "key": "i2"},
            "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 80.0, "U": 75.0}},
        }
        relations = compute_relations([d1, d2])
        types = {r.relation_type for r in relations}
        assert "BROAD_CONFIRMATION" in types

    def test_all_relation_types_valid(self):
        for rt in RELATION_TYPES:
            assert rt in {"THEME_LED", "INDUSTRY_LED", "BROAD_CONFIRMATION",
                          "ISOLATED_THEME", "STYLE_LED", "CONFLICTING"}


# =============================================================================
# Global ranking
# =============================================================================


class TestDiscoveryRanking:
    def test_rank_empty(self):
        from app.services.review_discovery_service import rank_discoveries
        assert rank_discoveries([]) == []

    def test_rank_by_anomaly(self):
        from app.services.review_discovery_service import rank_discoveries
        d1 = Discovery(
            discovery_id="d1", review_run_id="r1", trade_date="2026-08-11",
            scope_type="industry_l1", scope_key="i1", scope_name="i1",
            state=ScopeState(), change=ScopeChange(),
            anomaly=ScopeAnomaly(self_historical={"Q": 90.0}),
        )
        d2 = Discovery(
            discovery_id="d2", review_run_id="r1", trade_date="2026-08-11",
            scope_type="industry_l1", scope_key="i2", scope_name="i2",
            state=ScopeState(), change=ScopeChange(),
            anomaly=ScopeAnomaly(self_historical={"Q": 55.0}),
        )
        ranked = rank_discoveries([d2, d1])
        assert ranked[0].discovery_id == "d1"  # higher anomaly first