"""Review V2 Discovery domain tests — final production contract."""

from app.domain.review.discovery import (
    Discovery, ScopeState, ScopeChange, ScopeAnomaly,
    project_state, project_change, project_anomaly,
    is_discovery_eligible, build_discovery, make_discovery_id,
)
from app.domain.review.cross_scope_relation import (
    CrossScopeRelation, compute_relations, RELATION_TYPES,
)


def _make_metric_payload(value=60.0, delta1d=2.0, delta5d=5.0,
                         history_pct=70.0, cross_pct=65.0, components=None):
    return {"value": value, "delta1d": delta1d, "delta5d": delta5d,
            "historyPercentile120d": history_pct, "crossSectionPercentile": cross_pct,
            "components": components or [], "coverage": 0.95, "status": "ready"}


def _make_component(name, raw_value=0.5, norm_value=50.0):
    return {"name": name, "rawValue": raw_value, "normalizedValue": norm_value,
            "direction": "neutral", "status": "ready"}


def _sample_payloads():
    return (
        _make_metric_payload(60.0, 2.0),
        _make_metric_payload(50.0, 1.0, components=[
            _make_component("uptrend_member_ratio", 0.52, 52.0),
            _make_component("main_structure_up_ratio", 0.61, 61.0),
            _make_component("structure_breakdown_diffusion", 0.15, 15.0),
        ]),
        _make_metric_payload(70.0, 5.0, components=[
            _make_component("multi_dim_improving_ratio", 0.45, 45.0),
            _make_component("leader_follower_common_confirm_ratio", 0.55, 55.0),
        ]),
        _make_metric_payload(30.0, -1.0, components=[
            _make_component("member_change_hhi", 0.12, 12.0),
            _make_component("top5_price_change_contribution", 0.38, 38.0),
            _make_component("leader_median_diff", 0.05, 5.0),
        ]),
        _make_metric_payload(40.0, 3.0),
    )


class TestStateProjection:
    def test_project_state_real_components(self):
        p, q, u, c, v = _sample_payloads()
        state = project_state(p, q, u, c, v)
        assert state.metrics["P"].value == 60.0
        assert state.concentration.hhi == 0.12
        assert state.internal_structure.trend_breadth == 52.0
        assert state.internal_structure.synchronized_improvement is True

    def test_project_state_empty(self):
        state = project_state(None, None, None, None, None)
        assert state.metrics["P"].value is None


class TestChangeProjection:
    def test_project_change(self):
        p, q, u, c, v = _sample_payloads()
        change = project_change(p, q, u, c, v)
        assert change.metrics["P"].delta1d == 2.0
        assert change.concentration.direction is None

    def test_concentration_rising(self):
        c = _make_metric_payload(80.0, 5.0)
        change = project_change(None, None, None, c, None)
        assert change.concentration.direction == "rising"


class TestAnomalyProjection:
    def test_project_anomaly(self):
        p, q, u, c, v = _sample_payloads()
        anomaly = project_anomaly(p, q, u, c, v)
        assert anomaly.self_historical["P"] == 70.0


class TestDiscoveryEligibility:
    def test_no_signal_no_discovery(self):
        assert is_discovery_eligible([], ScopeState(), ScopeChange(), ScopeAnomaly()) is False

    def test_signal_without_state_no_discovery(self):
        assert is_discovery_eligible(["s1"], ScopeState(), ScopeChange(), ScopeAnomaly()) is False

    def test_signal_with_state_no_change_or_anomaly_no_discovery(self):
        state = ScopeState()
        state.metrics["P"] = type("m", (), {"value": 60.0})()
        assert is_discovery_eligible(["s1"], state, ScopeChange(), ScopeAnomaly()) is False

    def test_signal_state_change_eligible(self):
        state = ScopeState()
        state.metrics["P"] = type("m", (), {"value": 60.0})()
        change = ScopeChange()
        change.metrics["P"] = type("m", (), {"delta1d": 3.0})()
        assert is_discovery_eligible(["s1"], state, change, ScopeAnomaly()) is True

    def test_signal_state_anomaly_eligible(self):
        state = ScopeState()
        state.metrics["P"] = type("m", (), {"value": 60.0})()
        anomaly = ScopeAnomaly(self_historical={"P": 85.0})
        assert is_discovery_eligible(["s1"], state, ScopeChange(), anomaly) is True

    def test_static_d4_concentration_only_no_discovery(self):
        """D4 concentration_high 仅有 static State，无 Change/Anomaly → 不得产生 Discovery。"""
        c = _make_metric_payload(70.0, 0.0, history_pct=50.0, components=[
            _make_component("member_change_hhi", 0.15, 15.0),
            _make_component("top5_price_change_contribution", 0.42, 42.0),
        ])
        state = project_state(None, None, None, c, None)
        change = project_change(None, None, None, c, None)
        anomaly = project_anomaly(None, None, None, c, None)
        assert is_discovery_eligible(["s1"], state, change, anomaly) is False


class TestDiscoveryIdentity:
    def test_deterministic(self):
        id1 = make_discovery_id("run-1", "industry_l1", "electronics")
        id2 = make_discovery_id("run-1", "industry_l1", "electronics")
        assert id1 == id2 and len(id1) == 12

    def test_different_scope(self):
        assert make_discovery_id("r1", "industry_l1", "e") != make_discovery_id("r1", "concept", "c")


class TestBuildDiscovery:
    def test_with_signals(self):
        p, q, u, c, v = _sample_payloads()
        d = build_discovery("run-1", "2026-08-11", "industry_l1", "electronics", "电子",
                            p, q, u, c, v, signal_ids=["s1", "s2"], coverage=0.95, ready_count=30)
        assert d is not None
        assert d.scope_type == "industry_l1"
        assert len(d.supporting_signal_ids) == 2
        dd = d.to_dict()
        assert dd["scope"]["type"] == "industry_l1"
        assert dd["state"]["metrics"]["P"]["value"] == 60.0
        assert "rankKey" in dd

    def test_market_can_produce_discovery(self):
        p = _make_metric_payload(60.0, 3.0, history_pct=85.0, cross_pct=None)
        d = build_discovery("r1", "2026-08-11", "market", "market", "全市场",
                            p, None, None, None, None, signal_ids=["s1"])
        assert d is not None
        assert d.scope_type == "market"
        assert d.anomaly.cross_sectional.get("P") is None  # market has no peer cohort

    def test_market_no_signal_no_discovery(self):
        p = _make_metric_payload(60.0, 3.0)
        d = build_discovery("r1", "2026-08-11", "market", "market", "全市场",
                            p, None, None, None, None, signal_ids=[])
        assert d is None

    def test_static_state_only_no_discovery(self):
        p = _make_metric_payload(60.0, 0.0, history_pct=50.0)
        d = build_discovery("r1", "2026-08-11", "industry_l1", "e", "e",
                            p, None, None, None, None, signal_ids=["s1"])
        assert d is None

    def test_change_only_no_state_no_discovery(self):
        d = build_discovery("r1", "2026-08-11", "industry_l1", "e", "e",
                            None, None, None, None, None, signal_ids=["s1"])
        assert d is None


class TestCrossScopeRelation:
    def test_theme_led(self):
        concept = {"discoveryId": "d1", "scope": {"type": "concept", "key": "c1"},
                    "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 75.0}}}
        industry = {"discoveryId": "d2", "scope": {"type": "industry_l1", "key": "i1"},
                     "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 35.0}}}
        types = {r.relation_type for r in compute_relations([concept, industry])}
        assert "THEME_LED" in types

    def test_broad_confirmation(self):
        d1 = {"discoveryId": "d1", "scope": {"type": "industry_l1", "key": "i1"},
              "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 75.0, "U": 70.0}}}
        d2 = {"discoveryId": "d2", "scope": {"type": "industry_l1", "key": "i2"},
              "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 80.0, "U": 75.0}}}
        types = {r.relation_type for r in compute_relations([d1, d2])}
        assert "BROAD_CONFIRMATION" in types

    def test_conflicting(self):
        d1 = {"discoveryId": "d1", "scope": {"type": "industry_l1", "key": "i1"},
              "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 80.0}}}
        d2 = {"discoveryId": "d2", "scope": {"type": "industry_l1", "key": "i2"},
              "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 20.0}}}
        types = {r.relation_type for r in compute_relations([d1, d2])}
        assert "CONFLICTING" in types

    def test_style_led(self):
        style = {"discoveryId": "d1", "scope": {"type": "style", "key": "large_cap"},
                 "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 70.0}}}
        industry = {"discoveryId": "d2", "scope": {"type": "industry_l1", "key": "i1"},
                     "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 65.0}}}
        types = {r.relation_type for r in compute_relations([style, industry])}
        assert "STYLE_LED" in types

    def test_all_six_types_declared(self):
        expected = {"THEME_LED", "INDUSTRY_LED", "BROAD_CONFIRMATION",
                     "ISOLATED_THEME", "STYLE_LED", "CONFLICTING"}
        assert RELATION_TYPES == expected


class TestDiscoveryRanking:
    def test_rank_by_anomaly(self):
        from app.services.review_discovery_service import rank_discoveries
        d1 = Discovery("d1", "r1", "2026-08-11", "industry_l1", "i1", "i1",
                       ScopeState(), ScopeChange(), ScopeAnomaly(self_historical={"Q": 90.0}))
        d2 = Discovery("d2", "r1", "2026-08-11", "industry_l1", "i2", "i2",
                       ScopeState(), ScopeChange(), ScopeAnomaly(self_historical={"Q": 55.0}))
        ranked = rank_discoveries([d2, d1])
        assert ranked[0][0].discovery_id == "d1"

    def test_rank_details_preserved(self):
        from app.services.review_discovery_service import rank_discoveries
        d = Discovery("d1", "r1", "2026-08-11", "industry_l1", "i1", "i1",
                      ScopeState(), ScopeChange(), ScopeAnomaly())
        ranked = rank_discoveries([d])
        assert len(ranked) == 1
        _, details = ranked[0]
        for key in ["anomaly", "change", "evidenceConsistency", "coverage", "breadth"]:
            assert key in details, f"{key} missing"

    def test_rank_key_survives_to_dict(self):
        d = Discovery("d1", "r1", "2026-08-11", "industry_l1", "i1", "i1",
                      ScopeState(), ScopeChange(), ScopeAnomaly())
        d.rank_key = {"anomaly": 32.0, "change": 5.0, "evidenceConsistency": 3.0,
                       "crossScopeConfirmation": 0.0, "coverage": 9.5, "duration": 1.0, "breadth": 0.0}
        dd = d.to_dict()
        assert dd["rankKey"] == d.rank_key
