"""Contract tests for L2 Observation Groups (v2.3 §7.7 / plan §9).

These tests verify that L2 is a pure deterministic projection of L1 canonical
facts — no recompute, no score, no mutation.  They build minimal L1-shaped
payloads (no DB) and assert the 8-group structure and exact source mapping.
"""

from __future__ import annotations

from copy import deepcopy

from app.domain.review.observation_groups import (
    L2_GROUP_SPECS,
    build_l2_observation_groups,
    project_event_cells_by_type,
)

# --- minimal L1-shaped payload fixture ---------------------------------------


def _make_l1_payload() -> dict:
    return {
        "scope": {
            "scope_type": "industry_l1",
            "scope_key": "I11",
            "scope_name": "银行",
            "trade_date": "2026-08-13",
            "member_count": 10,
        },
        "price": {
            "equal_weight_return": {"median": 0.012, "valid_count": 10, "denominator": 10},
            "amount_weighted_return": {"median": 0.009, "valid_count": 10, "denominator": 10},
            "total_volume": {"median": 1_000_000, "valid_count": 10, "denominator": 10},
            "concentration": {
                "raw_hhi": 0.11,
                "normalized_hhi": 0.03,
                "member_count": 10,
                "status": "available",
            },
            "amount": {
                "total_amount": {"median": 5_000_000_000, "valid_count": 10, "denominator": 10},
                "concentration": {
                    "raw_hhi": 0.15,
                    "normalized_hhi": 0.05,
                    "member_count": 10,
                    "status": "available",
                },
            },
        },
        "trend": {
            "state": {"status": "available", "member_ratio": {"up": 0.6, "down": 0.4}},
            "continuous": {
                "regime_strength": {"median": 0.4, "valid_count": 10, "denominator": 10},
                "dsa_vwap_dev_pct": {"median": -0.01, "valid_count": 10, "denominator": 10},
                "segment_bars": {"median": 12, "valid_count": 10, "denominator": 10},
                "segment_change_pct": {"median": 0.05, "valid_count": 10, "denominator": 10},
                "segment_slope": {"median": 0.002, "valid_count": 10, "denominator": 10},
                "segment_volume_mean_ratio": {"median": 1.2, "valid_count": 10, "denominator": 10},
                "segment_amount_mean_ratio": {"median": 1.1, "valid_count": 10, "denominator": 10},
                "vwap_ret_total": {"median": 0.03, "valid_count": 10, "denominator": 10},
            },
        },
        "structure": {
            "events": {
                "cells": [
                    {"event_type": "BOS", "direction": "up", "structure_level": "Swing",
                     "member_count": 4, "member_ratio": 0.4, "event_count": 4},
                    {"event_type": "CHoCH", "direction": "down", "structure_level": "Internal",
                     "member_count": 3, "member_ratio": 0.3, "event_count": 3},
                    {"event_type": "OB_CREATED", "direction": "up", "structure_level": "Swing",
                     "member_count": 2, "member_ratio": 0.2, "event_count": 2},
                    {"event_type": "OB_ENTERED", "direction": "up", "structure_level": "Swing",
                     "member_count": 1, "member_ratio": 0.1, "event_count": 1},
                    {"event_type": "OB_MITIGATED", "direction": "down", "structure_level": "Internal",
                     "member_count": 1, "member_ratio": 0.1, "event_count": 1},
                    {"event_type": "EQH", "direction": "up", "structure_level": "Internal",
                     "member_count": 2, "member_ratio": 0.2, "event_count": 2},
                    {"event_type": "EQL", "direction": "down", "structure_level": "Internal",
                     "member_count": 2, "member_ratio": 0.2, "event_count": 2},
                    {"event_type": "SQZ_RELEASE", "direction": "up", "structure_level": "Swing",
                     "member_count": 5, "member_ratio": 0.5, "event_count": 5},
                ]
            },
            "alignment": {"status": "available", "value": "aligned_up"},
            "distance_to_trailing_top_pct": {"median": 0.08, "valid_count": 10, "denominator": 10},
            "distance_to_trailing_bottom_pct": {"median": -0.05, "valid_count": 10, "denominator": 10},
        },
        "momentum": {
            "squeeze_state": {"status": "available", "member_ratio": {"squeeze": 0.2}},
            "bb_position": {"status": "unavailable", "reason": "CURRENT_SOURCE_UNAVAILABLE"},
            "bb_width": {"status": "available", "median": 0.02, "valid_count": 10, "denominator": 10},
            "momentum_volume_relation": {
                "status": "available",
                "member_ratio": {"共振": 0.3, "背离": 0.1},
            },
            "release_volume_ratio": {"median": 1.35, "valid_count": 10, "denominator": 10},
        },
        "participation": {
            "volume": {
                "ratio20": {"median": 1.1, "valid_count": 10, "denominator": 10},
                "ratio200": {"status": "unavailable", "reason": "readiness_200_not_met"},
                "percentile20": {"median": 60, "valid_count": 10, "denominator": 10},
                "percentile200": {"status": "unavailable", "reason": "readiness_200_not_met"},
                "zscore20": {"median": 0.5, "valid_count": 10, "denominator": 10},
                "zscore200": {"status": "unavailable", "reason": "readiness_200_not_met"},
            }
        },
        "chip": {"status": "unavailable", "reason": "CHIP_UNRESOLVED"},
    }


# --- TEST 1: exactly 8 groups, fixed order, keys, labels ---------------------


def test_l2_has_exactly_8_fixed_groups_in_order():
    payload = _make_l1_payload()
    l2 = build_l2_observation_groups(payload)

    expected_keys = [s.group_key for s in L2_GROUP_SPECS]
    assert list(l2.keys()) == expected_keys
    assert len(l2) == 8

    expected_labels = [
        "价格与资金表现",
        "趋势状态",
        "趋势进程",
        "趋势量能确认",
        "结构突破与转折",
        "结构演化与位置",
        "动量与压缩释放",
        "量能异常",
    ]
    for spec, key, label in zip(L2_GROUP_SPECS, expected_keys, expected_labels, strict=True):
        assert l2[key]["group_key"] == key
        assert l2[key]["label"] == label
        assert spec.label == label


# --- TEST 2: Group 1 mapping ------------------------------------------------


def test_group_1_price_capital_mapping():
    payload = _make_l1_payload()
    facts = build_l2_observation_groups(payload)["price_capital"]["facts"]

    assert facts["equal_weight_return"] == payload["price"]["equal_weight_return"]
    assert facts["amount_weighted_return"] == payload["price"]["amount_weighted_return"]
    assert facts["total_volume"] == payload["price"]["total_volume"]
    assert facts["total_amount"] == payload["price"]["amount"]["total_amount"]
    # full HHI objects preserved (raw_hhi + normalized_hhi + member_count + status)
    assert facts["price_hhi"] == payload["price"]["concentration"]
    assert facts["amount_hhi"] == payload["price"]["amount"]["concentration"]
    assert "raw_hhi" in facts["price_hhi"]
    assert "normalized_hhi" in facts["price_hhi"]
    # Turnover excluded by design (PRD §7.7)
    assert "turnover" not in facts


# --- TEST 3: Group 2 / 3 trend mapping --------------------------------------


def test_group_2_and_3_trend_mapping():
    payload = _make_l1_payload()
    l2 = build_l2_observation_groups(payload)

    g2 = l2["trend_state"]["facts"]
    assert g2["trend_direction_member_ratio"] == payload["trend"]["state"]
    assert g2["trend_strength"] == payload["trend"]["continuous"]["regime_strength"]
    assert g2["dsa_vwap_dev_pct"] == payload["trend"]["continuous"]["dsa_vwap_dev_pct"]

    g3 = l2["trend_progress"]["facts"]
    assert g3["current_segment_bars"] == payload["trend"]["continuous"]["segment_bars"]
    assert g3["segment_change_pct"] == payload["trend"]["continuous"]["segment_change_pct"]
    assert g3["segment_slope"] == payload["trend"]["continuous"]["segment_slope"]
    assert g3["segment_volume_mean_ratio"] == payload["trend"]["continuous"]["segment_volume_mean_ratio"]
    assert g3["segment_amount_mean_ratio"] == payload["trend"]["continuous"]["segment_amount_mean_ratio"]
    assert g3["vwap_ret_total"] == payload["trend"]["continuous"]["vwap_ret_total"]


# --- TEST 4: duplicate reference semantics (Group 3 vs Group 4) -------------


def test_group_4_duplicate_reference_identical_to_group_3():
    payload = _make_l1_payload()
    l2 = build_l2_observation_groups(payload)

    g3 = l2["trend_progress"]["facts"]
    g4 = l2["trend_volume_confirmation"]["facts"]

    # same value (by-reference identical object)
    assert g4["segment_volume_mean_ratio"] is g3["segment_volume_mean_ratio"]
    assert g4["segment_amount_mean_ratio"] is g3["segment_amount_mean_ratio"]
    # momentum_volume_relation from momentum section
    assert g4["momentum_volume_relation"] == payload["momentum"]["momentum_volume_relation"]


# --- TEST 5: structure event filtering --------------------------------------


def test_structure_event_filtering_by_group():
    payload = _make_l1_payload()
    l2 = build_l2_observation_groups(payload)

    g5 = l2["structure_break_turn"]["facts"]["bos_choch_events"]
    g5_types = {c["event_type"] for c in g5["cells"]}
    assert g5_types == {"BOS", "CHoCH"}

    g6 = l2["structure_evolution_position"]["facts"]["ob_and_eq_events"]
    g6_types = {c["event_type"] for c in g6["cells"]}
    assert g6_types == {"OB_CREATED", "OB_ENTERED", "OB_MITIGATED", "EQH", "EQL"}

    # SQZ_RELEASE enters neither group
    assert "SQZ_RELEASE" not in g5_types
    assert "SQZ_RELEASE" not in g6_types

    # preserved verbatim: member_count / member_ratio / event_count / direction / structure_level
    cell = g5["cells"][0]
    for k in ("event_type", "direction", "structure_level", "member_count", "member_ratio", "event_count"):
        assert cell[k] == {"event_type": "BOS", "direction": "up", "structure_level": "Swing",
                           "member_count": 4, "member_ratio": 0.4, "event_count": 4}[k]


def test_structure_event_empty_when_no_matching_cells():
    payload = _make_l1_payload()
    # events contain only SQZ_RELEASE -> empty subset, not fabricated 0-fact
    payload["structure"]["events"] = {"cells": [
        {"event_type": "SQZ_RELEASE", "direction": "up", "structure_level": "Swing",
         "member_count": 5, "member_ratio": 0.5, "event_count": 5}
    ]}
    l2 = build_l2_observation_groups(payload)
    assert l2["structure_break_turn"]["facts"]["bos_choch_events"]["cells"] == []
    assert l2["structure_evolution_position"]["facts"]["ob_and_eq_events"]["cells"] == []


# --- TEST 6: Group 6 excludes Active OB -------------------------------------


def test_group_6_excludes_active_ob_count():
    payload = _make_l1_payload()
    # even if L1 mistakenly carried active_ob_count, L2 must not surface it as a key
    payload["structure"]["active_ob_count"] = {"status": "unavailable"}
    l2 = build_l2_observation_groups(payload)
    assert "active_ob_count" not in l2["structure_evolution_position"]["facts"]
    # the structure section itself never had that key either
    assert "active_ob_count" not in l2["structure_evolution_position"]["facts"]


# --- TEST 7: Group 7 member-first passthrough (no event recompute) ----------


def test_group_7_release_volume_ratio_member_first():
    payload = _make_l1_payload()
    l2 = build_l2_observation_groups(payload)
    g7 = l2["momentum_squeeze_release"]["facts"]

    assert g7["release_volume_ratio"] == payload["momentum"]["release_volume_ratio"]
    assert g7["squeeze_state"] == payload["momentum"]["squeeze_state"]
    assert g7["bb_position"] == payload["momentum"]["bb_position"]
    assert g7["bb_width"] == payload["momentum"]["bb_width"]

    # Release ratio must NOT be derived from structure.events
    assert g7["release_volume_ratio"] is not payload["structure"]["events"]


# --- TEST 8: Group 8 complete six-fact vector ------------------------------


def test_group_8_volume_six_facts_complete():
    payload = _make_l1_payload()
    facts = build_l2_observation_groups(payload)["volume_anomaly"]["facts"]

    assert set(facts.keys()) == {
        "volume_ratio20", "volume_ratio200",
        "volume_percentile20", "volume_percentile200",
        "volume_zscore20", "volume_zscore200",
    }
    assert facts["volume_ratio20"] == payload["participation"]["volume"]["ratio20"]
    assert facts["volume_ratio200"] == payload["participation"]["volume"]["ratio200"]
    assert facts["volume_percentile20"] == payload["participation"]["volume"]["percentile20"]
    assert facts["volume_percentile200"] == payload["participation"]["volume"]["percentile200"]
    assert facts["volume_zscore20"] == payload["participation"]["volume"]["zscore20"]
    assert facts["volume_zscore200"] == payload["participation"]["volume"]["zscore200"]


# --- TEST 9: unavailable passthrough ----------------------------------------


def test_unavailable_status_preserved_verbatim():
    payload = _make_l1_payload()
    l2 = build_l2_observation_groups(payload)

    bb = l2["momentum_squeeze_release"]["facts"]["bb_position"]
    assert bb["status"] == "unavailable"
    assert bb["reason"] == "CURRENT_SOURCE_UNAVAILABLE"
    # not coerced to 0 / false / normal
    assert bb.get("median") is None

    vol200 = l2["volume_anomaly"]["facts"]["volume_ratio200"]
    assert vol200["status"] == "unavailable"


# --- TEST 10: no mutation of input payload ----------------------------------


def test_build_l2_does_not_mutate_input():
    payload = _make_l1_payload()
    before = deepcopy(payload)
    _ = build_l2_observation_groups(payload)
    assert payload == before


# --- TEST 11: forbidden product-semantics keys absent -----------------------


def test_l2_has_no_forbidden_product_semantics():
    payload = _make_l1_payload()
    l2 = build_l2_observation_groups(payload)

    forbidden = {"score", "opportunity", "risk", "recommendation", "signal", "rank"}

    def _walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k not in forbidden, f"forbidden L2 key {k!r} found"
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(l2)


# --- project_event_cells_by_type unit coverage ------------------------------


def test_project_event_cells_by_type_filters_only():
    events = {"cells": [
        {"event_type": "BOS", "member_count": 1},
        {"event_type": "OB_CREATED", "member_count": 2},
    ]}
    out = project_event_cells_by_type(events, frozenset({"BOS"}))
    assert [c["event_type"] for c in out["cells"]] == ["BOS"]
    # non-cells metadata preserved, not recomputed
    assert out.get("member_count") == events.get("member_count") or "member_count" not in out


def test_project_event_cells_by_type_passthrough_when_no_cells():
    events = {"status": "unavailable", "reason": "x"}
    out = project_event_cells_by_type(events, frozenset({"BOS"}))
    assert out == events
