"""Contract tests for L2 Observation Groups (v2.3 §7.7 / plan §9).

These tests verify that L2 is a pure deterministic projection of L1 canonical
facts — no recompute, no score, no mutation.

Round 2 (Fix & Verify) correction: the structure-event fixtures no longer
hand-write a shape that differs from production.  The canonical L1 event shape
produced by ``scope_observation._aggregate_structure_events`` is::

    structure.events = {
        "cells": {
            "leveled": {"<cell_name>": {event_type, direction, structure_level,
                                        event_count, member_count, member_ratio}},
            "extreme": {"<event_type>": {event_count, member_count, member_ratio}},
        },
        "denominator": N,
    }

The dict fixture below mirrors that exactly, and the REAL-L1 tests at the bottom
drive ``compute_scope_observation(...)`` and feed its true output into
``build_l2_observation_groups(...)`` so the tested chain is
``real L1 Core -> L2 projection`` rather than ``fake fixture -> L2 helper``.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.observation_groups import (
    L2_GROUP_SPECS,
    build_l2_observation_groups,
    project_event_cells_by_type,
)
from app.domain.review.scope_observation import (
    MemberObservation,
    StructureEvent,
    compute_scope_observation,
)

# --- canonical-shaped L1 payload fixture -------------------------------------
#
# ``structure.events`` uses the EXACT production topology (cells.leveled /
# cells.extreme / denominator), including SQZ_RELEASE living in ``extreme``.


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
                "cells": {
                    "leveled": {
                        "BOS_up_Swing": {
                            "event_type": "BOS", "direction": "up", "structure_level": "Swing",
                            "event_count": 4, "member_count": 4, "member_ratio": 0.4,
                        },
                        "CHoCH_down_Internal": {
                            "event_type": "CHoCH", "direction": "down",
                            "structure_level": "Internal",
                            "event_count": 3, "member_count": 3, "member_ratio": 0.3,
                        },
                        "OB_CREATED_up_Swing": {
                            "event_type": "OB_CREATED", "direction": "up",
                            "structure_level": "Swing",
                            "event_count": 2, "member_count": 2, "member_ratio": 0.2,
                        },
                        "OB_ENTERED_up_Swing": {
                            "event_type": "OB_ENTERED", "direction": "up",
                            "structure_level": "Swing",
                            "event_count": 1, "member_count": 1, "member_ratio": 0.1,
                        },
                        "OB_MITIGATED_down_Internal": {
                            "event_type": "OB_MITIGATED", "direction": "down",
                            "structure_level": "Internal",
                            "event_count": 1, "member_count": 1, "member_ratio": 0.1,
                        },
                    },
                    "extreme": {
                        "EQH": {"event_count": 2, "member_count": 2, "member_ratio": 0.2},
                        "EQL": {"event_count": 2, "member_count": 2, "member_ratio": 0.2},
                        "SQZ_RELEASE": {
                            "event_count": 5, "member_count": 5, "member_ratio": 0.5,
                        },
                    },
                },
                "denominator": 10,
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


# --- TEST 5: structure event filtering (canonical topology) ------------------


def test_structure_event_filtering_by_group():
    payload = _make_l1_payload()
    l2 = build_l2_observation_groups(payload)

    g5 = l2["structure_break_turn"]["facts"]["bos_choch_events"]
    g6 = l2["structure_evolution_position"]["facts"]["ob_and_eq_events"]

    # topology preserved: cells.leveled / cells.extreme / denominator
    assert set(g5.keys()) == {"cells", "denominator"}
    assert set(g5["cells"].keys()) == {"leveled", "extreme"}
    assert set(g6["cells"].keys()) == {"leveled", "extreme"}

    # Group 5: only BOS / CHoCH in leveled, extreme empty
    assert {c["event_type"] for c in g5["cells"]["leveled"].values()} == {"BOS", "CHoCH"}
    assert g5["cells"]["extreme"] == {}

    # Group 6: only OB_* in leveled, only EQH / EQL in extreme
    assert {c["event_type"] for c in g6["cells"]["leveled"].values()} == {
        "OB_CREATED", "OB_ENTERED", "OB_MITIGATED",
    }
    assert set(g6["cells"]["extreme"].keys()) == {"EQH", "EQL"}

    # SQZ_RELEASE enters neither group
    assert "SQZ_RELEASE" not in g5["cells"]["extreme"]
    assert "SQZ_RELEASE" not in g6["cells"]["extreme"]
    assert "SQZ_RELEASE" not in g5["cells"]["leveled"]
    assert "SQZ_RELEASE" not in g6["cells"]["leveled"]

    # denominator copied verbatim from L1 (never recomputed from the subset)
    assert g5["denominator"] == payload["structure"]["events"]["denominator"] == 10
    assert g6["denominator"] == payload["structure"]["events"]["denominator"] == 10


def test_structure_event_cells_unchanged_field_by_field():
    """Every retained cell must be structurally identical to its L1 origin."""
    payload = _make_l1_payload()
    l1_events = payload["structure"]["events"]
    l2 = build_l2_observation_groups(payload)

    g5 = l2["structure_break_turn"]["facts"]["bos_choch_events"]
    g6 = l2["structure_evolution_position"]["facts"]["ob_and_eq_events"]

    leveled_checks = {
        "BOS_up_Swing": g5,
        "CHoCH_down_Internal": g5,
        "OB_CREATED_up_Swing": g6,
        "OB_ENTERED_up_Swing": g6,
        "OB_MITIGATED_down_Internal": g6,
    }
    for cell_name, group in leveled_checks.items():
        src = l1_events["cells"]["leveled"][cell_name]
        out = group["cells"]["leveled"][cell_name]
        for field in (
            "event_type", "direction", "structure_level",
            "event_count", "member_count", "member_ratio",
        ):
            assert out[field] == src[field], f"{cell_name}.{field} changed"
        # passed through by reference — no copy, no recompute
        assert out is src

    for event_type in ("EQH", "EQL"):
        src = l1_events["cells"]["extreme"][event_type]
        out = g6["cells"]["extreme"][event_type]
        for field in ("event_count", "member_count", "member_ratio"):
            assert out[field] == src[field], f"{event_type}.{field} changed"
        assert out is src


def test_structure_event_empty_when_no_matching_cells():
    payload = _make_l1_payload()
    # events contain only SQZ_RELEASE -> empty subset, denominator still verbatim
    payload["structure"]["events"] = {
        "cells": {
            "leveled": {},
            "extreme": {"SQZ_RELEASE": {"event_count": 5, "member_count": 5, "member_ratio": 0.5}},
        },
        "denominator": 10,
    }
    l2 = build_l2_observation_groups(payload)

    g5 = l2["structure_break_turn"]["facts"]["bos_choch_events"]
    g6 = l2["structure_evolution_position"]["facts"]["ob_and_eq_events"]
    assert g5["cells"] == {"leveled": {}, "extreme": {}}
    assert g6["cells"] == {"leveled": {}, "extreme": {}}
    assert g5["denominator"] == 10
    assert g6["denominator"] == 10


# --- TEST 6: Group 6 excludes Active OB -------------------------------------


def test_group_6_excludes_active_ob_count():
    payload = _make_l1_payload()
    # even if L1 mistakenly carried active_ob_count, L2 must not surface it as a key
    payload["structure"]["active_ob_count"] = {"status": "unavailable"}
    l2 = build_l2_observation_groups(payload)
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
    assert g7["release_volume_ratio"] is payload["momentum"]["release_volume_ratio"]


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
    events = {
        "cells": {
            "leveled": {
                "BOS_up_Swing": {"event_type": "BOS", "direction": "up",
                                 "structure_level": "Swing", "member_count": 1,
                                 "member_ratio": 0.1, "event_count": 1},
                "OB_CREATED_up_Swing": {"event_type": "OB_CREATED", "direction": "up",
                                        "structure_level": "Swing", "member_count": 2,
                                        "member_ratio": 0.2, "event_count": 2},
            },
            "extreme": {"EQH": {"member_count": 3, "member_ratio": 0.3, "event_count": 3}},
        },
        "denominator": 10,
    }
    out = project_event_cells_by_type(events, frozenset({"BOS"}))
    assert list(out["cells"]["leveled"].keys()) == ["BOS_up_Swing"]
    assert out["cells"]["extreme"] == {}
    # denominator preserved verbatim, not recomputed
    assert out["denominator"] == 10
    # input not mutated
    assert set(events["cells"]["leveled"].keys()) == {"BOS_up_Swing", "OB_CREATED_up_Swing"}
    assert events["cells"]["extreme"] == {
        "EQH": {"member_count": 3, "member_ratio": 0.3, "event_count": 3}
    }


def test_project_event_cells_by_type_passthrough_when_no_cells():
    events = {"status": "unavailable", "reason": "x"}
    out = project_event_cells_by_type(events, frozenset({"BOS"}))
    assert out["status"] == "unavailable"
    assert out["reason"] == "x"
    assert out["cells"] == {"leveled": {}, "extreme": {}}


# ===========================================================================
# REAL L1 -> L2 contract tests (Round 2)
#
# These drive the real canonical Core (``compute_scope_observation``) and feed
# its true output into the L2 projection.  No hand-written L1 shape.
# ===========================================================================

_REAL_TRADE_DATE = date(2026, 8, 13)


def _real_member(mid: str) -> MemberObservation:
    """A member carrying every fact needed by the 8 groups (real Core input)."""
    return MemberObservation(
        member_id=mid,
        price_candidate=True,
        return_1d=1.5,
        amount=1_000_000.0,
        trend=Direction.UP,
        swing=Direction.UP,
        internal=Direction.UP,
        momentum=MomentumDirection.EXPANDING,
        t1_trend=Direction.SIDEWAYS,
        t1_swing=Direction.UP,
        t1_internal=Direction.UP,
        t1_momentum=MomentumDirection.FLAT,
        vol_ratio20=1.2,
        amt_ratio20=1.1,
        volume_t=500_000.0,
        vol_ratio200=1.3,
        vol_pct20=70.0,
        vol_pct200=65.0,
        vol_zscore20=0.8,
        vol_zscore200=0.6,
        regime_strength=0.55,
        dsa_dir_bars=6.0,
        dsa_vwap_dev_pct=0.7,
        segment_id=None,
        segment_direction=1.0,
        segment_bars=9.0,
        segment_change_pct=4.2,
        segment_slope=0.35,
        seg_vol_ratio=1.15,
        seg_amt_ratio=1.05,
        seg_vol_mean=1.25,
        seg_amt_mean_prev=1.18,
        structure_alignment_categorical="aligned",
        active_internal_ob_count=2.0,
        active_swing_ob_count=1.0,
        volatility_phase=1.0,
        momentum_direction_raw=1.0,
        momentum_change=1.0,
        sqzmom_delta=0.4,
        sqzmom_val=0.9,
        release_volume_ratio=1.45,
        momentum_volume_relation="共振",
        bb_position=0.62,
        bb_width=0.048,
        vwap_ret_total=3.1,
        trailing_top_pct=-2.5,
        trailing_bottom_pct=8.4,
    )


def _real_events(member_ids: list[str]) -> list[StructureEvent]:
    """Cover all 8 canonical event types across real members."""
    m0, m1, m2 = member_ids[0], member_ids[1], member_ids[2]
    return [
        StructureEvent(member_id=m0, event_type="BOS", direction="up",
                       level=10.0, internal=False),
        StructureEvent(member_id=m1, event_type="CHoCH", direction="down",
                       level=9.5, internal=True),
        StructureEvent(member_id=m0, event_type="OB_CREATED", direction="up",
                       level=10.2, internal=False),
        StructureEvent(member_id=m1, event_type="OB_ENTERED", direction="up",
                       level=10.1, internal=True),
        StructureEvent(member_id=m2, event_type="OB_MITIGATED", direction="down",
                       level=9.9, internal=False),
        StructureEvent(member_id=m0, event_type="EQH"),
        StructureEvent(member_id=m2, event_type="EQL"),
        StructureEvent(member_id=m1, event_type="SQZ_RELEASE", release_volume_ratio=1.7),
    ]


def _real_l1_payload() -> dict:
    member_ids = ["000001", "000002", "000003", "000004"]
    members = [_real_member(mid) for mid in member_ids]
    return compute_scope_observation(
        scope_type="industry_l1",
        scope_key="I11",
        trade_date=_REAL_TRADE_DATE,
        pit_member_ids=member_ids,
        pit_member_ids_t1=member_ids,
        members=members,
        events=_real_events(member_ids),
        event_coverage_member_ids=member_ids,
    )


def test_real_l1_structure_events_project_into_groups_5_and_6():
    """real Core -> L2: Group 5 = BOS/CHoCH only, Group 6 = OB_*/EQ only."""
    l1 = _real_l1_payload()
    l1_events = l1["structure"]["events"]

    # sanity: the real Core really produced the canonical dict topology
    assert set(l1_events["cells"].keys()) == {"leveled", "extreme"}
    assert isinstance(l1_events["cells"]["leveled"], dict)
    assert isinstance(l1_events["cells"]["extreme"], dict)
    # all 8 event types present in real L1
    assert {c["event_type"] for c in l1_events["cells"]["leveled"].values()} == {
        "BOS", "CHoCH", "OB_CREATED", "OB_ENTERED", "OB_MITIGATED",
    }
    assert set(l1_events["cells"]["extreme"].keys()) == {"EQH", "EQL", "SQZ_RELEASE"}

    l2 = build_l2_observation_groups(l1)
    g5 = l2["structure_break_turn"]["facts"]["bos_choch_events"]
    g6 = l2["structure_evolution_position"]["facts"]["ob_and_eq_events"]

    # Group 5: leveled only BOS + CHoCH, extreme empty
    assert {c["event_type"] for c in g5["cells"]["leveled"].values()} == {"BOS", "CHoCH"}
    assert g5["cells"]["extreme"] == {}

    # Group 6: leveled only OB_*, extreme only EQH + EQL
    assert {c["event_type"] for c in g6["cells"]["leveled"].values()} == {
        "OB_CREATED", "OB_ENTERED", "OB_MITIGATED",
    }
    assert set(g6["cells"]["extreme"].keys()) == {"EQH", "EQL"}

    # SQZ_RELEASE absent from both
    for group in (g5, g6):
        assert "SQZ_RELEASE" not in group["cells"]["extreme"]
        assert all(
            c["event_type"] != "SQZ_RELEASE" for c in group["cells"]["leveled"].values()
        )

    # denominator verbatim from real L1
    assert g5["denominator"] == l1_events["denominator"]
    assert g6["denominator"] == l1_events["denominator"]


def test_real_l1_event_cell_values_unchanged_by_projection():
    """Every real L1 cell field survives projection bit-identically."""
    l1 = _real_l1_payload()
    l1_events = l1["structure"]["events"]
    l2 = build_l2_observation_groups(l1)

    g5 = l2["structure_break_turn"]["facts"]["bos_choch_events"]
    g6 = l2["structure_evolution_position"]["facts"]["ob_and_eq_events"]

    projected_leveled = {**g5["cells"]["leveled"], **g6["cells"]["leveled"]}
    for cell_name, out in projected_leveled.items():
        src = l1_events["cells"]["leveled"][cell_name]
        for field in (
            "event_type", "direction", "structure_level",
            "event_count", "member_count", "member_ratio",
        ):
            assert out[field] == src[field], f"{cell_name}.{field} changed"

    for event_type, out in g6["cells"]["extreme"].items():
        src = l1_events["cells"]["extreme"][event_type]
        for field in ("event_count", "member_count", "member_ratio"):
            assert out[field] == src[field], f"{event_type}.{field} changed"


def test_real_l1_topology_compatible_with_all_8_groups():
    """Every L2 source path must be readable from a REAL L1 output."""
    l1 = _real_l1_payload()
    l2 = build_l2_observation_groups(l1)

    assert len(l2) == 8
    assert list(l2.keys()) == [s.group_key for s in L2_GROUP_SPECS]

    # Groups 1/2/3/4/7/8: every declared source path resolves in the real payload.
    event_projection_keys = {"bos_choch_events", "ob_and_eq_events"}
    for spec in L2_GROUP_SPECS:
        facts = l2[spec.group_key]["facts"]
        assert set(facts.keys()) == {ref.key for ref in spec.facts}
        for ref in spec.facts:
            if ref.key in event_projection_keys:
                continue
            node = l1
            for part in ref.source_path:
                assert isinstance(node, dict), (
                    f"{spec.group_key}.{ref.key}: real L1 path {ref.source_path} broken at {part!r}"
                )
                assert part in node, (
                    f"{spec.group_key}.{ref.key}: real L1 has no path {ref.source_path}"
                )
                node = node[part]
            # projected value IS the real L1 object at that path
            assert facts[ref.key] is node


def test_real_l1_duplicate_fact_identical_across_groups():
    """Same L1 fact referenced by Group 3 and Group 4 stays the same object."""
    l1 = _real_l1_payload()
    l2 = build_l2_observation_groups(l1)

    g3 = l2["trend_progress"]["facts"]
    g4 = l2["trend_volume_confirmation"]["facts"]
    assert g4["segment_volume_mean_ratio"] is g3["segment_volume_mean_ratio"]
    assert g4["segment_amount_mean_ratio"] is g3["segment_amount_mean_ratio"]


def test_real_l1_projection_does_not_mutate_core_output():
    l1 = _real_l1_payload()
    before = deepcopy(l1)
    _ = build_l2_observation_groups(l1)
    assert l1 == before
