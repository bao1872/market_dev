"""Review V2 Discovery domain tests — Signal semantics, STYLE_LED, lifecycle."""

from app.domain.review.discovery import (
    Discovery, ScopeState, ScopeChange, ScopeAnomaly,
    classify_signal_evidence, is_discovery_eligible,
    build_discovery, make_discovery_id,
)
from app.domain.review.cross_scope_relation import compute_relations, RELATION_TYPES


def _make_metric_payload(value=60.0, delta1d=2.0, delta5d=5.0, history_pct=70.0, cross_pct=65.0, components=None):
    return {"value": value, "delta1d": delta1d, "delta5d": delta5d,
            "historyPercentile120d": history_pct, "crossSectionPercentile": cross_pct,
            "components": components or [], "coverage": 0.95, "status": "ready"}


class TestSignalEvidenceClassification:
    def test_a1_is_anomaly(self):
        cls = classify_signal_evidence("surface_strong_internal_weak", "A")
        assert cls["is_anomaly"] is True
        assert cls["is_change"] is False

    def test_a2_is_change(self):
        cls = classify_signal_evidence("surface_weak_internal_improving", "A")
        assert cls["is_change"] is True

    def test_b1_is_change(self):
        cls = classify_signal_evidence("high_level_slowing", "B")
        assert cls["is_change"] is True

    def test_d4_is_state_only(self):
        cls = classify_signal_evidence("concentration_high", "D")
        assert cls["is_state"] is True
        assert cls["is_change"] is False
        assert cls["is_anomaly"] is False

    def test_d1_is_change(self):
        cls = classify_signal_evidence("state_migration_positive", "D")
        assert cls["is_change"] is True


class TestDiscoveryEligibility:
    def test_d4_alone_no_discovery(self):
        """D4 concentration_high only → NO Discovery."""
        assert is_discovery_eligible(
            ["concentration_high"], ["D"]) is False

    def test_change_signal_eligible(self):
        assert is_discovery_eligible(
            ["low_level_repair"], ["B"]) is True

    def test_anomaly_signal_eligible(self):
        assert is_discovery_eligible(
            ["surface_strong_internal_weak"], ["A"]) is True

    def test_no_signal_no_discovery(self):
        assert is_discovery_eligible([], []) is False

    def test_d4_plus_change_signal_eligible(self):
        assert is_discovery_eligible(
            ["concentration_high", "low_level_repair"], ["D", "B"]) is True


class TestBuildDiscovery:
    def test_d4_only_no_discovery(self):
        p = _make_metric_payload(60.0, 0.0, history_pct=50.0)
        d = build_discovery("r1", "2026-08-11", "industry_l1", "e", "e",
                            p, None, None, None, None,
                            signal_ids=["s1"], signal_types=["concentration_high"],
                            signal_families=["D"])
        assert d is None

    def test_change_signal_creates_discovery(self):
        p = _make_metric_payload(60.0, 3.0, history_pct=50.0)
        d = build_discovery("r1", "2026-08-11", "industry_l1", "e", "e",
                            p, None, None, None, None,
                            signal_ids=["s1"], signal_types=["low_level_repair"],
                            signal_families=["B"])
        assert d is not None
        assert d.status == "new"

    def test_lifecycle_from_signals(self):
        p = _make_metric_payload(60.0, 3.0)
        d = build_discovery("r1", "2026-08-11", "industry_l1", "e", "e",
                            p, None, None, None, None,
                            signal_ids=["s1", "s2"],
                            signal_types=["low_level_repair", "breadth_expansion"],
                            signal_families=["B", "D"],
                            signal_statuses=["continuing", "confirmed"],
                            signal_first_seens=["2026-08-01", "2026-08-03"])
        assert d is not None
        assert d.status == "confirmed"
        assert d.first_seen == "2026-08-01"
        assert d.duration == 2  # distinct dates: 08-01, 08-03

    def test_market_discovery(self):
        p = _make_metric_payload(60.0, 3.0, history_pct=85.0, cross_pct=None)
        d = build_discovery("r1", "2026-08-11", "market", "market", "全市场",
                            p, None, None, None, None,
                            signal_ids=["s1"], signal_types=["low_level_repair"],
                            signal_families=["B"])
        assert d is not None
        assert d.scope_type == "market"


class TestCrossScopeStyleLed:
    def test_style_one_industry_no_style_led(self):
        style = {"discoveryId": "d1", "scope": {"type": "style", "key": "large_cap"},
                 "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 70.0}}}
        ind1 = {"discoveryId": "d2", "scope": {"type": "industry_l1", "key": "i1"},
                 "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 65.0}}}
        types = {r.relation_type for r in compute_relations([style, ind1])}
        assert "STYLE_LED" not in types

    def test_style_two_industries_style_led(self):
        style = {"discoveryId": "d1", "scope": {"type": "style", "key": "large_cap"},
                 "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 70.0}}}
        ind1 = {"discoveryId": "d2", "scope": {"type": "industry_l1", "key": "i1"},
                 "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 65.0}}}
        ind2 = {"discoveryId": "d3", "scope": {"type": "industry_l1", "key": "i2"},
                 "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 70.0}}}
        types = {r.relation_type for r in compute_relations([style, ind1, ind2])}
        assert "STYLE_LED" in types

    def test_style_conflicting_no_style_led(self):
        style = {"discoveryId": "d1", "scope": {"type": "style", "key": "large_cap"},
                 "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 70.0}}}
        ind1 = {"discoveryId": "d2", "scope": {"type": "industry_l1", "key": "i1"},
                 "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 80.0}}}
        ind2 = {"discoveryId": "d3", "scope": {"type": "industry_l1", "key": "i2"},
                 "state": {"metrics": {}}, "anomaly": {"selfHistorical": {"Q": 20.0}}}
        relations = compute_relations([style, ind1, ind2])
        types = {r.relation_type for r in relations}
        # ind2 Q=20 conflicts, so CONFLICTING should appear
        assert "CONFLICTING" in types or "STYLE_LED" not in types


class TestCrossScopeAllTypes:
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

    def test_all_six_declared(self):
        assert RELATION_TYPES == {"THEME_LED", "INDUSTRY_LED", "BROAD_CONFIRMATION",
                                   "ISOLATED_THEME", "STYLE_LED", "CONFLICTING"}


class TestDiscoveryIdentity:
    def test_deterministic(self):
        id1 = make_discovery_id("run-1", "industry_l1", "electronics")
        assert id1 == make_discovery_id("run-1", "industry_l1", "electronics")
        assert len(id1) == 12


class TestRanking:
    def test_rank_details_preserved(self):
        from app.services.review_discovery_service import rank_discoveries
        d = Discovery("d1", "r1", "2026-08-11", "industry_l1", "i1", "i1",
                      ScopeState(), ScopeChange(), ScopeAnomaly())
        ranked = rank_discoveries([d])
        _, details = ranked[0]
        for key in ["anomaly", "change", "evidenceConsistency", "coverage", "breadth"]:
            assert key in details, f"{key} missing"

    def test_rank_key_to_dict(self):
        d = Discovery("d1", "r1", "2026-08-11", "industry_l1", "i1", "i1",
                      ScopeState(), ScopeChange(), ScopeAnomaly())
        d.rank_key = {"anomaly": 32.0, "change": 5.0}
        dd = d.to_dict()
        assert dd["rankKey"] == d.rank_key
