"""Service-level contract test for L2 Observation Group projection (plan §9 TEST 12).

The service only reads an already-persisted L1 fact and projects L2.  We test it
without a DB by monkeypatching the persistence read-back owner; this proves the
service delegates to ``build_l2_observation_groups`` on the canonical
``observation_payload`` and returns ``None`` when no fact exists.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.review.observation_groups import (
    L2_GROUP_SPECS,
    build_l2_observation_groups,
)
from app.services import review_observation_group_service as svc


def _l1_payload() -> dict:
    return {
        "scope": {"scope_type": "industry_l1", "scope_key": "I11", "trade_date": "2026-08-13"},
        "price": {
            "equal_weight_return": {"median": 0.01, "valid_count": 10, "denominator": 10},
            "amount_weighted_return": {"median": 0.01, "valid_count": 10, "denominator": 10},
            "total_volume": {"median": 1, "valid_count": 10, "denominator": 10},
            "concentration": {"raw_hhi": 0.1, "normalized_hhi": 0.02, "member_count": 10, "status": "available"},
            "amount": {
                "total_amount": {"median": 5, "valid_count": 10, "denominator": 10},
                "concentration": {"raw_hhi": 0.1, "normalized_hhi": 0.02, "member_count": 10, "status": "available"},
            },
        },
        "trend": {
            "state": {"status": "available", "member_ratio": {"up": 0.6}},
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
            # canonical L1 shape (cells.leveled / cells.extreme / denominator)
            "events": {
                "cells": {
                    "leveled": {
                        "BOS_up_Swing": {
                            "event_type": "BOS", "direction": "up", "structure_level": "Swing",
                            "event_count": 4, "member_count": 4, "member_ratio": 0.4,
                        },
                        "OB_CREATED_up_Swing": {
                            "event_type": "OB_CREATED", "direction": "up",
                            "structure_level": "Swing",
                            "event_count": 2, "member_count": 2, "member_ratio": 0.2,
                        },
                    },
                    "extreme": {
                        "EQH": {"event_count": 1, "member_count": 1, "member_ratio": 0.1},
                        "SQZ_RELEASE": {"event_count": 3, "member_count": 3, "member_ratio": 0.3},
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
            "momentum_volume_relation": {"status": "available", "member_ratio": {"共振": 0.3}},
            "release_volume_ratio": {"median": 1.35, "valid_count": 10, "denominator": 10},
        },
        "participation": {"volume": {
            "ratio20": {"median": 1.1, "valid_count": 10, "denominator": 10},
            "ratio200": {"status": "unavailable", "reason": "readiness_200_not_met"},
            "percentile20": {"median": 60, "valid_count": 10, "denominator": 10},
            "percentile200": {"status": "unavailable", "reason": "readiness_200_not_met"},
            "zscore20": {"median": 0.5, "valid_count": 10, "denominator": 10},
            "zscore200": {"status": "unavailable", "reason": "readiness_200_not_met"},
        }},
        "chip": {"status": "unavailable", "reason": "CHIP_UNRESOLVED"},
    }


class _FakeFact:
    def __init__(self, payload: dict):
        self.observation_payload = payload


@pytest.mark.asyncio
async def test_service_returns_none_when_no_fact(monkeypatch):
    async def _no_fact(*a, **k):
        return None

    monkeypatch.setattr(svc, "get_scope_observation_fact", _no_fact)
    out = await svc.get_scope_observation_groups(None, date(2026, 8, 13), "industry_l1", "I11")
    assert out is None


@pytest.mark.asyncio
async def test_service_projects_persisted_payload(monkeypatch):
    payload = _l1_payload()

    async def _fake_fact(*a, **k):
        return _FakeFact(payload)

    monkeypatch.setattr(svc, "get_scope_observation_fact", _fake_fact)
    out = await svc.get_scope_observation_groups(None, date(2026, 8, 13), "industry_l1", "I11")

    # deterministic projection equals the pure builder on the same payload
    assert out == build_l2_observation_groups(payload)
    # key invariants
    assert list(out.keys()) == [s.group_key for s in L2_GROUP_SPECS]
    assert out["price_capital"]["facts"]["equal_weight_return"] == payload["price"]["equal_weight_return"]
    assert out["volume_anomaly"]["facts"]["volume_ratio20"] == payload["participation"]["volume"]["ratio20"]

    # canonical event topology is projected (not passed through whole)
    g5 = out["structure_break_turn"]["facts"]["bos_choch_events"]
    g6 = out["structure_evolution_position"]["facts"]["ob_and_eq_events"]
    assert set(g5["cells"]["leveled"].keys()) == {"BOS_up_Swing"}
    assert g5["cells"]["extreme"] == {}
    assert set(g6["cells"]["leveled"].keys()) == {"OB_CREATED_up_Swing"}
    assert set(g6["cells"]["extreme"].keys()) == {"EQH"}
    assert g5["denominator"] == 10
