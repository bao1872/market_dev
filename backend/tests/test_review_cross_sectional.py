"""Focused tests for C1 Cross-sectional Analysis (v2.3 §7.8.1).

These tests cover ONLY the C1 contract surface:

1. percentile calculation
2. valid_peer_count filtering (unavailable peers excluded from denominator)
3. insufficient peers -> unavailable
4. unknown field -> fail closed  (implicit: only C1_CORE_FIELDS are emitted)
5. deterministic output
6. no mutation of inputs
7. service delegation to the domain projection

No DB, no recomputation, no L1 changes.  The domain is pure, so it is tested
directly; the service is tested by monkeypatching the persistence read-back.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.review.analysis import cross_sectional as dom
from app.domain.review.analysis.cross_sectional import compute_cross_sectional
from app.services import review_cross_sectional_service as svc

# ---------------------------------------------------------------------------
# L1 payload helpers (canonical shape, verified against scope_observation output)
# ---------------------------------------------------------------------------


def _l1_payload(
    *,
    equal_weight_return: float = 0.01,
    amount_weighted_return: float = 0.02,
    regime_strength: float = 0.4,
    vol_ratio20_p50: float = 1.1,
    vol_ratio200_p50: float = 1.3,
    bb_position_p50: float = 0.5,
    bb_width_p50: float = 0.02,
) -> dict:
    """Build a minimal valid L1 canonical payload.

    Scalar fields are floats; distribution fields carry a ``p50`` (the comparable
    scalar used by the domain) plus the canonical L1 envelope keys.
    """
    return {
        "price": {
            "equal_weight_return": equal_weight_return,
            "amount_weighted_return": amount_weighted_return,
        },
        "trend": {
            "continuous": {
                "regime_strength": regime_strength,
            }
        },
        "participation": {
            "volume": {
                "ratio20": {"p25": 0.9, "p50": vol_ratio20_p50, "p75": 1.3, "valid_count": 10},
                "ratio200": {"p25": 1.0, "p50": vol_ratio200_p50, "p75": 1.6, "valid_count": 10},
            }
        },
        "momentum": {
            "bb_position": {"median": bb_position_p50, "p25": 0.3, "p50": bb_position_p50, "p75": 0.7, "valid_count": 10},
            "bb_width": {"median": bb_width_p50, "p25": 0.01, "p50": bb_width_p50, "p75": 0.05, "valid_count": 10},
        },
    }


def _result_by_field(out: dict) -> dict[str, dict]:
    return {f["field"]: f for f in out["fields"]}


# ---------------------------------------------------------------------------
# 1. Percentile calculation
# ---------------------------------------------------------------------------


def test_percentile_calculation():
    current = _l1_payload(equal_weight_return=0.03)
    # 4 valid peers with equal_weight_return = 0.01, 0.02, 0.04, 0.05
    peers = {
        "cur": current,
        "p1": _l1_payload(equal_weight_return=0.01),
        "p2": _l1_payload(equal_weight_return=0.02),
        "p3": _l1_payload(equal_weight_return=0.04),
        "p4": _l1_payload(equal_weight_return=0.05),
    }
    out = compute_cross_sectional(current_payload=current, peer_payloads=peers)
    res = _result_by_field(out)
    ewr = res["equal_weight_return"]
    assert ewr["status"] == "ready"
    assert ewr["value"] == 0.03
    assert ewr["peer_count"] == 5
    assert ewr["valid_peer_count"] == 5
    # cohort includes the current scope itself (valid_peer_count == 5).
    # below=2 (0.01, 0.02), equal=1 (current 0.03) -> (2 + 0.5) / 5 * 100 = 50.0
    assert ewr["percentile"] == pytest.approx(50.0)


def test_percentile_rank_midpoint_boundary():
    # all peers equal to current -> below=0, equal=5 -> 0.5*5/5*100 = 50
    current = _l1_payload(equal_weight_return=0.02)
    peers = {f"p{i}": _l1_payload(equal_weight_return=0.02) for i in range(5)}
    peers["cur"] = current
    out = compute_cross_sectional(current_payload=current, peer_payloads=peers)
    ewr = _result_by_field(out)["equal_weight_return"]
    assert ewr["status"] == "ready"
    assert ewr["percentile"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# 2. valid_peer_count filtering (unavailable peers excluded from denominator)
# ---------------------------------------------------------------------------


def test_valid_peer_count_excludes_unavailable_peers():
    current = _l1_payload(equal_weight_return=0.03)
    peers = {
        "cur": current,
        # 4 valid
        "p1": _l1_payload(equal_weight_return=0.01),
        "p2": _l1_payload(equal_weight_return=0.02),
        "p3": _l1_payload(equal_weight_return=0.04),
        "p4": _l1_payload(equal_weight_return=0.05),
        # 3 unavailable: missing key / non-finite / wrong type
        "bad_missing": {"price": {}},  # equal_weight_return absent
        "bad_nan": _l1_payload(equal_weight_return=float("nan")),
        "bad_type": _l1_payload(equal_weight_return="x"),  # type: ignore[arg-type]
    }
    out = compute_cross_sectional(current_payload=current, peer_payloads=peers)
    ewr = _result_by_field(out)["equal_weight_return"]
    assert ewr["status"] == "ready"
    assert ewr["peer_count"] == 8
    assert ewr["valid_peer_count"] == 5  # current + 4 valid
    # current 0.03 inside cohort: below=2, equal=1 -> (2 + 0.5)/5*100 = 50.0
    assert ewr["percentile"] == pytest.approx(50.0)


# distribution-valued field: missing p50 makes the peer invalid
def test_distribution_field_excludes_peers_missing_p50():
    current = _l1_payload(vol_ratio20_p50=1.1)
    peers = {
        "cur": current,
        "p1": _l1_payload(vol_ratio20_p50=0.9),
        "p2": _l1_payload(vol_ratio20_p50=1.2),
        "p3": _l1_payload(vol_ratio20_p50=1.3),
        "p4": _l1_payload(vol_ratio20_p50=1.4),
        "bad": {"participation": {"volume": {"ratio20": {"p25": 0.8, "p75": 1.2}}}},  # no p50
    }
    out = compute_cross_sectional(current_payload=current, peer_payloads=peers)
    res = _result_by_field(out)
    r20 = res["participation.volume.ratio20"]
    assert r20["status"] == "ready"
    assert r20["valid_peer_count"] == 5
    assert r20["value"] == 1.1
    # current 1.1 inside cohort: below=1 (0.9), equal=1 -> (1 + 0.5)/5*100 = 30.0
    assert r20["percentile"] == pytest.approx(30.0)
    # momentum.bb_position still ready using same cohort p50 extractor
    assert res["momentum.bb_position"]["status"] == "ready"


# ---------------------------------------------------------------------------
# 3. Insufficient peers -> unavailable
# ---------------------------------------------------------------------------


def test_insufficient_valid_peers_unavailable():
    current = _l1_payload(equal_weight_return=0.03)
    # only 3 extra valid peers -> valid_peer_count == 4 (< 5) -> unavailable
    peers = {
        "cur": current,
        "p1": _l1_payload(equal_weight_return=0.01),
        "p2": _l1_payload(equal_weight_return=0.02),
        "p3": _l1_payload(equal_weight_return=0.04),
    }
    out = compute_cross_sectional(current_payload=current, peer_payloads=peers)
    ewr = _result_by_field(out)["equal_weight_return"]
    assert ewr["status"] == "unavailable"
    assert ewr["reason"] == "INSUFFICIENT_PEER_SAMPLE"
    assert ewr["percentile"] is None
    assert ewr["valid_peer_count"] == 4  # current + 3 extra
    assert ewr["value"] == 0.03


def test_no_peers_unavailable():
    current = _l1_payload()
    out = compute_cross_sectional(current_payload=current, peer_payloads={})
    # empty cohort -> current not even present
    res = _result_by_field(out)
    assert res["equal_weight_return"]["status"] == "unavailable"
    assert res["equal_weight_return"]["reason"] == "NO_PEERS"
    # when cohort contains only the (valid) current, peer_count=1 -> insufficient
    out2 = compute_cross_sectional(current_payload=current, peer_payloads={"cur": current})
    assert _result_by_field(out2)["equal_weight_return"]["reason"] == "INSUFFICIENT_PEER_SAMPLE"


# ---------------------------------------------------------------------------
# 4. Unknown / current-field-unavailable -> fail closed
# ---------------------------------------------------------------------------


def test_current_field_unavailable_when_scalar_missing():
    current = _l1_payload()
    del current["price"]["equal_weight_return"]  # current scope lacks the field
    peers = {f"p{i}": _l1_payload() for i in range(5)}
    peers["cur"] = current
    out = compute_cross_sectional(current_payload=current, peer_payloads=peers)
    ewr = _result_by_field(out)["equal_weight_return"]
    assert ewr["status"] == "unavailable"
    assert ewr["reason"] == "CURRENT_FIELD_UNAVAILABLE"
    assert ewr["percentile"] is None
    # other valid fields still compute
    assert _result_by_field(out)["amount_weighted_return"]["status"] == "ready"


def test_only_core_fields_emitted():
    out = compute_cross_sectional(current_payload=_l1_payload(), peer_payloads={"c": _l1_payload()})
    fields = [f["field"] for f in out["fields"]]
    assert fields == [spec.field_key for spec in dom.C1_CORE_FIELDS]
    # exactly the allowlisted set, nothing else
    assert len(fields) == 7


# ---------------------------------------------------------------------------
# 5. Deterministic output
# ---------------------------------------------------------------------------


def test_deterministic_output():
    current = _l1_payload(equal_weight_return=0.03)
    peers = {
        "cur": current,
        "p1": _l1_payload(equal_weight_return=0.01),
        "p2": _l1_payload(equal_weight_return=0.02),
        "p3": _l1_payload(equal_weight_return=0.04),
        "p4": _l1_payload(equal_weight_return=0.05),
    }
    out_a = compute_cross_sectional(current_payload=current, peer_payloads=peers)
    out_b = compute_cross_sectional(current_payload=current, peer_payloads=dict(peers))
    assert out_a == out_b


# ---------------------------------------------------------------------------
# 6. No mutation of inputs
# ---------------------------------------------------------------------------


def test_no_mutation_of_inputs():
    current = _l1_payload()
    peers = {
        "cur": current,
        "p1": _l1_payload(equal_weight_return=0.01),
        "p2": _l1_payload(equal_weight_return=0.02),
        "p3": _l1_payload(equal_weight_return=0.04),
        "p4": _l1_payload(equal_weight_return=0.05),
    }
    import copy

    current_snapshot = copy.deepcopy(current)
    peers_snapshot = copy.deepcopy(peers)

    compute_cross_sectional(current_payload=current, peer_payloads=peers)

    assert current == current_snapshot
    assert peers == peers_snapshot


# ---------------------------------------------------------------------------
# 7. Service delegation
# ---------------------------------------------------------------------------


class _FakeFact:
    def __init__(self, payload: dict, scope_key: str = "cur"):
        self.observation_payload = payload
        self.scope_key = scope_key


@pytest.mark.asyncio
async def test_service_returns_none_when_no_fact(monkeypatch):
    async def _no_fact(*a, **k):
        return None

    monkeypatch.setattr(svc, "get_scope_observation_fact", _no_fact)
    out = await svc.get_cross_sectional(None, date(2026, 8, 13), "industry_l1", "cur")
    assert out is None


@pytest.mark.asyncio
async def test_service_delegates_to_domain(monkeypatch):
    current = _l1_payload(equal_weight_return=0.03)
    peers = [
        _FakeFact(current, "cur"),
        _FakeFact(_l1_payload(equal_weight_return=0.01), "p1"),
        _FakeFact(_l1_payload(equal_weight_return=0.02), "p2"),
        _FakeFact(_l1_payload(equal_weight_return=0.04), "p3"),
        _FakeFact(_l1_payload(equal_weight_return=0.05), "p4"),
    ]

    async def _fake_current(*a, **k):
        return peers[0]

    async def _fake_list(*a, **k):
        return peers

    monkeypatch.setattr(svc, "get_scope_observation_fact", _fake_current)
    monkeypatch.setattr(svc, "list_scope_observation_facts", _fake_list)

    out = await svc.get_cross_sectional(None, date(2026, 8, 13), "industry_l1", "cur")
    assert out is not None
    ewr = _result_by_field(out)["equal_weight_return"]
    assert ewr["status"] == "ready"
    assert ewr["peer_count"] == 5
    assert ewr["valid_peer_count"] == 5
    # current 0.03 inside cohort: below=2, equal=1 -> (2 + 0.5)/5*100 = 50.0
    assert ewr["percentile"] == pytest.approx(50.0)
