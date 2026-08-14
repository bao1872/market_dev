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
from typing import Any

import pytest

from app.domain.first_pyramid_semantics import Direction
from app.domain.review.analysis import cross_sectional as dom
from app.domain.review.analysis.cross_sectional import compute_cross_sectional
from app.domain.review.scope_observation import (
    MemberObservation,
    compute_scope_observation,
)
from app.services import review_cross_sectional_service as svc

pytestmark = pytest.mark.pure_unit

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
    # 5 valid peers (excluding current) so valid_peer_count == 5 >= minimum.
    peers = {
        "cur": current,
        "p1": _l1_payload(equal_weight_return=0.01),
        "p2": _l1_payload(equal_weight_return=0.02),
        "p3": _l1_payload(equal_weight_return=0.04),
        "p4": _l1_payload(equal_weight_return=0.05),
        "p5": _l1_payload(equal_weight_return=0.06),
    }
    out = compute_cross_sectional(current_payload=current, peer_payloads=peers, current_scope_key="cur")
    res = _result_by_field(out)
    ewr = res["equal_weight_return"]
    assert ewr["status"] == "ready"
    assert ewr["value"] == 0.03
    assert ewr["peer_count"] == 6
    # valid_peer_count EXCLUDES current (PRD §7.8.1 D) -> 5 other valid peers.
    assert ewr["valid_peer_count"] == 5
    # percentile denominator INCLUDES current (PRD §7.8.1 B "含自身参与"):
    # values [0.01,0.02,0.03(cur),0.04,0.05,0.06]; v=0.03 -> below=2, equal=1
    # -> (2 + 0.5) / 6 * 100 = 41.666...
    assert ewr["percentile"] == pytest.approx(41.66666666666667)


def test_percentile_rank_midpoint_boundary():
    # all peers equal to current -> below=0, equal=5 -> 0.5*5/5*100 = 50
    current = _l1_payload(equal_weight_return=0.02)
    peers = {f"p{i}": _l1_payload(equal_weight_return=0.02) for i in range(5)}
    peers["cur"] = current
    out = compute_cross_sectional(current_payload=current, peer_payloads=peers, current_scope_key="cur")
    ewr = _result_by_field(out)["equal_weight_return"]
    assert ewr["status"] == "ready"
    assert ewr["percentile"] == pytest.approx(50.0)
    assert ewr["valid_peer_count"] == 5  # 5 peers, current excluded


# ---------------------------------------------------------------------------
# 2. valid_peer_count filtering (unavailable peers excluded from denominator)
# ---------------------------------------------------------------------------


def test_valid_peer_count_excludes_unavailable_peers():
    current = _l1_payload(equal_weight_return=0.03)
    peers = {
        "cur": current,
        # 5 valid (so valid_peer_count excl current == 5 >= minimum)
        "p1": _l1_payload(equal_weight_return=0.01),
        "p2": _l1_payload(equal_weight_return=0.02),
        "p3": _l1_payload(equal_weight_return=0.04),
        "p4": _l1_payload(equal_weight_return=0.05),
        "p5": _l1_payload(equal_weight_return=0.06),
        # 3 unavailable: missing key / non-finite / wrong type
        "bad_missing": {"price": {}},  # equal_weight_return absent
        "bad_nan": _l1_payload(equal_weight_return=float("nan")),
        "bad_type": _l1_payload(equal_weight_return="x"),  # type: ignore[arg-type]
    }
    out = compute_cross_sectional(current_payload=current, peer_payloads=peers, current_scope_key="cur")
    ewr = _result_by_field(out)["equal_weight_return"]
    assert ewr["status"] == "ready"
    assert ewr["peer_count"] == 9
    # valid_peer_count excludes current -> 5 valid peers (3 bad excluded)
    assert ewr["valid_peer_count"] == 5
    # percentile denominator includes current: values [0.01,0.02,0.03(cur),0.04,0.05,0.06]
    # -> below=2, equal=1 -> (2 + 0.5) / 6 * 100 = 41.666...
    assert ewr["percentile"] == pytest.approx(41.66666666666667)


# distribution-valued field: missing p50 makes the peer invalid
def test_distribution_field_excludes_peers_missing_p50():
    current = _l1_payload(vol_ratio20_p50=1.1)
    peers = {
        "cur": current,
        "p1": _l1_payload(vol_ratio20_p50=0.9),
        "p2": _l1_payload(vol_ratio20_p50=1.2),
        "p3": _l1_payload(vol_ratio20_p50=1.3),
        "p4": _l1_payload(vol_ratio20_p50=1.4),
        "p5": _l1_payload(vol_ratio20_p50=1.5),
        "bad": {"participation": {"volume": {"ratio20": {"p25": 0.8, "p75": 1.2}}}},  # no p50
    }
    out = compute_cross_sectional(current_payload=current, peer_payloads=peers, current_scope_key="cur")
    res = _result_by_field(out)
    r20 = res["participation.volume.ratio20"]
    assert r20["status"] == "ready"
    # valid_peer_count excludes current -> 5 valid (bad has no p50)
    assert r20["valid_peer_count"] == 5
    assert r20["value"] == 1.1
    # percentile denominator includes current: below=1 (0.9), equal=1 -> (1 + 0.5)/6*100 = 25.0
    assert r20["percentile"] == pytest.approx(25.0)
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
    out = compute_cross_sectional(current_payload=current, peer_payloads=peers, current_scope_key="cur")
    ewr = _result_by_field(out)["equal_weight_return"]
    assert ewr["status"] == "unavailable"
    assert ewr["reason"] == "INSUFFICIENT_PEER_SAMPLE"
    assert ewr["percentile"] is None
    assert ewr["valid_peer_count"] == 3  # 3 extra valid, current excluded
    assert ewr["value"] == 0.03


def test_no_peers_unavailable():
    current = _l1_payload()
    out = compute_cross_sectional(current_payload=current, peer_payloads={}, current_scope_key="cur")
    # empty cohort -> current not even present
    res = _result_by_field(out)
    assert res["equal_weight_return"]["status"] == "unavailable"
    assert res["equal_weight_return"]["reason"] == "NO_PEERS"
    # when cohort contains only the (valid) current, valid_peer_count=0 -> insufficient
    out2 = compute_cross_sectional(current_payload=current, peer_payloads={"cur": current}, current_scope_key="cur")
    assert _result_by_field(out2)["equal_weight_return"]["reason"] == "INSUFFICIENT_PEER_SAMPLE"


# ---------------------------------------------------------------------------
# 4. Unknown / current-field-unavailable -> fail closed
# ---------------------------------------------------------------------------


def test_current_field_unavailable_when_scalar_missing():
    current = _l1_payload()
    del current["price"]["equal_weight_return"]  # current scope lacks the field
    peers = {f"p{i}": _l1_payload() for i in range(5)}
    peers["cur"] = current
    out = compute_cross_sectional(current_payload=current, peer_payloads=peers, current_scope_key="cur")
    ewr = _result_by_field(out)["equal_weight_return"]
    assert ewr["status"] == "unavailable"
    assert ewr["reason"] == "CURRENT_FIELD_UNAVAILABLE"
    assert ewr["percentile"] is None
    # other valid fields still compute
    assert _result_by_field(out)["amount_weighted_return"]["status"] == "ready"


def test_only_core_fields_emitted():
    out = compute_cross_sectional(current_payload=_l1_payload(), peer_payloads={"c": _l1_payload()}, current_scope_key="c")
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
    out_a = compute_cross_sectional(current_payload=current, peer_payloads=peers, current_scope_key="cur")
    out_b = compute_cross_sectional(current_payload=current, peer_payloads=dict(peers), current_scope_key="cur")
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

    compute_cross_sectional(current_payload=current, peer_payloads=peers, current_scope_key="cur")

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
        _FakeFact(_l1_payload(equal_weight_return=0.06), "p5"),
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
    assert ewr["peer_count"] == 6
    # valid_peer_count excludes current -> 5 other valid peers
    assert ewr["valid_peer_count"] == 5
    # current 0.03 inside percentile denominator: below=2, equal=1 -> 41.666...
    assert ewr["percentile"] == pytest.approx(41.66666666666667)


# ---------------------------------------------------------------------------
# 8. Real L1 integration: compute_scope_observation() -> compute_cross_sectional()
# ---------------------------------------------------------------------------
#
# Verifies the contract end-to-end against the ACTUAL L1 producer (no DB, synthetic
# members). This is the chain the service will run in production:
#
#     compute_scope_observation(members=...)   -> L1 canonical payload
#         |
#         v
#     compute_cross_sectional(L1 payload, peer L1 payloads)
#
# It must exercise at least one scalar field, one distribution field, and one
# unavailable field.


def _member(
    member_id: str,
    *,
    return_1d: float | None,
    amount: float | None,
    vol_ratio20: float | None = None,
    vol_ratio200: float | None = None,
    regime_strength: float | None = None,
    momentum: Any = None,
) -> MemberObservation:
    """Build a synthetic member with the canonical facts C1 reads."""
    return MemberObservation(
        member_id=member_id,
        price_candidate=return_1d is not None,
        return_1d=return_1d,
        amount=amount,
        trend=Direction.UP if momentum is None else None,
        swing=None,
        internal=None,
        momentum=momentum,
        vol_ratio20=vol_ratio20,
        vol_ratio200=vol_ratio200,
        regime_strength=regime_strength,
    )


def _scope_payload(
    scope_key: str,
    returns: list[float],
    amounts: list[float],
    *,
    vol_ratio20: float | None = None,
    vol_ratio200: float | None = None,
    regime_strength: float | None = None,
    momentum: Any = None,
) -> dict:
    """Compute a real L1 payload for one scope from synthetic members."""
    members = [
        _member(f"{scope_key}_{i}", return_1d=r, amount=a, vol_ratio20=vol_ratio20,
                vol_ratio200=vol_ratio200, regime_strength=regime_strength, momentum=momentum)
        for i, (r, a) in enumerate(zip(returns, amounts, strict=True))
    ]
    return compute_scope_observation(
        scope_type="industry_l1",
        scope_key=scope_key,
        trade_date=date(2026, 8, 13),
        pit_member_ids=[m.member_id for m in members],
        members=members,
    )


def test_real_l1_chain_scalar_and_distribution_fields():
    # Six industry_l1 scopes (cur + 5 peers), each with 5 members, so
    # valid_peer_count (excluding current) == 5 >= minimum.
    cur = _scope_payload(
        "cur",
        returns=[0.03, 0.02, 0.01, 0.04, 0.05],
        amounts=[1e9, 1.1e9, 9e8, 1.2e9, 1.3e9],
        vol_ratio20=1.2,
        vol_ratio200=1.4,
        regime_strength=0.6,
    )
    p1 = _scope_payload(
        "p1",
        returns=[0.01, 0.00, -0.02, 0.015, 0.005],
        amounts=[8e8, 9e8, 7e8, 1e9, 9.5e8],
        vol_ratio20=0.9,
        vol_ratio200=1.0,
        regime_strength=0.3,
    )
    p2 = _scope_payload(
        "p2",
        returns=[0.04, 0.05, 0.03, 0.06, 0.02],
        amounts=[2e9, 1.9e9, 2.1e9, 1.8e9, 2.2e9],
        vol_ratio20=1.5,
        vol_ratio200=1.7,
        regime_strength=0.8,
    )
    p3 = _scope_payload(
        "p3",
        returns=[0.02, 0.015, 0.025, 0.01, 0.03],
        amounts=[1.2e9, 1.1e9, 1.3e9, 1.0e9, 1.25e9],
        vol_ratio20=1.1,
        vol_ratio200=1.2,
        regime_strength=0.5,
    )
    p4 = _scope_payload(
        "p4",
        returns=[0.00, -0.01, 0.005, 0.01, -0.005],
        amounts=[7e8, 8e8, 6e8, 9e8, 7.5e8],
        vol_ratio20=0.8,
        vol_ratio200=0.9,
        regime_strength=0.2,
    )
    p5 = _scope_payload(
        "p5",
        returns=[0.05, 0.06, 0.04, 0.055, 0.045],
        amounts=[2.5e9, 2.4e9, 2.6e9, 2.3e9, 2.55e9],
        vol_ratio20=1.6,
        vol_ratio200=1.8,
        regime_strength=0.9,
    )

    peers = {"cur": cur, "p1": p1, "p2": p2, "p3": p3, "p4": p4, "p5": p5}
    out = compute_cross_sectional(current_payload=cur, peer_payloads=peers, current_scope_key="cur")
    res = _result_by_field(out)

    # --- scalar field (price.equal_weight_return) ---
    ewr = res["equal_weight_return"]
    assert ewr["status"] == "ready"
    assert ewr["peer_count"] == 6
    assert ewr["valid_peer_count"] == 5  # 5 peers, current excluded
    assert ewr["value"] is not None
    # 'cur' (0.03) is mid-pack among 6 scopes -> percentile strictly inside (0,100)
    assert 0.0 < ewr["percentile"] < 100.0

    # --- distribution field (participation.volume.ratio20 -> p50) ---
    r20 = res["participation.volume.ratio20"]
    assert r20["status"] == "ready"
    assert r20["value"] is not None  # p50 extracted from the L1 distribution
    assert r20["valid_peer_count"] == 5
    assert 0.0 <= r20["percentile"] <= 100.0

    # --- scalar trend field also derived from real L1 ---
    rs = res["trend.continuous.regime_strength"]
    assert rs["status"] == "ready"
    assert rs["value"] == pytest.approx(0.6)


def test_real_l1_chain_unavailable_field():
    # Current scope lacks valid price members -> equal_weight_return unavailable.
    # 6 scopes total (cur + 5 peers) so the peer-sample gate passes and the
    # current-scope field gap surfaces as CURRENT_FIELD_UNAVAILABLE (not masked
    # by INSUFFICIENT_PEER_SAMPLE).
    cur = _scope_payload(
        "cur",
        returns=[None, None, None],  # no valid price members
        amounts=[1e9, 1e9, 1e9],
        vol_ratio20=1.2,
        vol_ratio200=1.4,
        regime_strength=0.6,  # members carry regime_strength -> trend field ready
    )
    p1 = _scope_payload("p1", returns=[0.01, 0.02, 0.03], amounts=[1e9, 1e9, 1e9])
    p2 = _scope_payload("p2", returns=[0.04, 0.05, 0.06], amounts=[1e9, 1e9, 1e9])
    p3 = _scope_payload("p3", returns=[0.02, 0.015, 0.025], amounts=[1e9, 1e9, 1e9])
    p4 = _scope_payload("p4", returns=[0.00, -0.01, 0.005], amounts=[1e9, 1e9, 1e9])
    p5 = _scope_payload("p5", returns=[0.05, 0.06, 0.04], amounts=[1e9, 1e9, 1e9])

    peers = {"cur": cur, "p1": p1, "p2": p2, "p3": p3, "p4": p4, "p5": p5}
    out = compute_cross_sectional(current_payload=cur, peer_payloads=peers, current_scope_key="cur")
    res = _result_by_field(out)

    # scalar field that the current scope cannot produce -> CURRENT_FIELD_UNAVAILABLE
    # (fail-closed: the current scope's gap is reported directly; peer counting is
    # short-circuited when the current field is missing).
    ewr = res["equal_weight_return"]
    assert ewr["status"] == "unavailable"
    assert ewr["reason"] == "CURRENT_FIELD_UNAVAILABLE"
    assert ewr["percentile"] is None
