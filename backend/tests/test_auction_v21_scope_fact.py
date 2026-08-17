"""Targeted tests for Auction V2.1 canonical L1 Scope Fact owner.

Coverage required by AUCTION-V21-001 execution contract:
- deterministic L1 fact cases (§9/§10 semantics);
- scalar reference oracle vs vectorized kernel parity (fixed seed);
- micro-benchmark evidence (N = 12 / 40 / 151 / 3599 / 5000).

The scalar oracle is TEST-ONLY: it is never imported by production code and is
not a second semantic owner.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import pytest

from app.domain.auction.member_fact import (
    AuctionMemberFact,
    AuctionMemberFactConfig,
)
from app.domain.auction.scope_fact import (
    compute_auction_l1_scope_facts,
)

_NAN = float("nan")


def _mk(
    instrument_id: str,
    gap_pct=_NAN,
    auction_volume=_NAN,
    auction_amount=_NAN,
    gap_percentile=_NAN,
    volume_percentile=_NAN,
    amount_percentile=_NAN,
    current_gap_eligible=False,
    gap_history_eligible=False,
    current_volume_eligible=False,
    volume_history_eligible=False,
    current_amount_eligible=False,
    amount_history_eligible=False,
) -> AuctionMemberFact:
    return AuctionMemberFact(
        instrument_id=instrument_id,
        gap_pct=gap_pct,
        auction_volume=auction_volume,
        auction_amount=auction_amount,
        gap_percentile=gap_percentile,
        volume_percentile=volume_percentile,
        amount_percentile=amount_percentile,
        current_gap_eligible=current_gap_eligible,
        gap_history_eligible=gap_history_eligible,
        current_volume_eligible=current_volume_eligible,
        volume_history_eligible=volume_history_eligible,
        current_amount_eligible=current_amount_eligible,
        amount_history_eligible=amount_history_eligible,
    )


CFG = AuctionMemberFactConfig(
    positive_gap_percentile_threshold=90.0,
    negative_gap_percentile_threshold=10.0,
    volume_abnormal_percentile_threshold=90.0,
    amount_abnormal_percentile_threshold=90.0,
)


# ---------------------------------------------------------------------------
# TEST-ONLY SCALAR REFERENCE ORACLE (not imported by production)
# ---------------------------------------------------------------------------
@dataclass
class _ScalarFact:
    equal_weight_gap: float | None
    amount_weighted_gap: float | None
    positive_gap_breadth: float | None
    negative_gap_breadth: float | None
    positive_gap_abnormal_breadth: float | None
    negative_gap_abnormal_breadth: float | None
    total_auction_volume: float | None
    total_auction_amount: float | None
    volume_abnormal_breadth: float | None
    amount_abnormal_breadth: float | None
    auction_amount_market_contribution: float | None
    positive_joint_abnormal_breadth: float | None
    negative_joint_abnormal_breadth: float | None
    top1_amount_share: float | None
    top3_amount_share: float | None
    raw_hhi: float | None
    normalized_hhi: float | None
    amount_share_eligible_count: int


def _scalar_compute(scope_members: list[AuctionMemberFact], market_total_amount: float):
    """Plain Python scalar reference. Not a semantic owner."""
    cg = [m for m in scope_members if m.current_gap_eligible]
    gh = [m for m in scope_members if m.gap_history_eligible]
    cv = [m for m in scope_members if m.current_volume_eligible]
    vh = [m for m in scope_members if m.volume_history_eligible]
    ca = [m for m in scope_members if m.current_amount_eligible]
    ah = [m for m in scope_members if m.amount_history_eligible]
    joint = [m for m in scope_members if m.gap_history_eligible and m.amount_history_eligible]

    def br(num, den):
        return (num / den) if den > 0 else None

    ewg = (sum(m.gap_pct for m in cg) / len(cg)) if cg else None

    aw_cand = [m for m in cg if m.current_amount_eligible]
    aw_w = sum(m.auction_amount for m in aw_cand)
    awg = (
        sum(m.gap_pct * m.auction_amount for m in aw_cand) / aw_w if aw_w > 0 else None
    )

    pos_g = br(sum(1 for m in cg if m.gap_pct > 0), len(cg))
    neg_g = br(sum(1 for m in cg if m.gap_pct < 0), len(cg))

    pos_ab = br(
        sum(1 for m in gh if m.gap_percentile >= CFG.positive_gap_percentile_threshold),
        len(gh),
    )
    neg_ab = br(
        sum(1 for m in gh if m.gap_percentile <= CFG.negative_gap_percentile_threshold),
        len(gh),
    )

    tv = sum(m.auction_volume for m in cv) if cv else None
    ta = sum(m.auction_amount for m in ca) if ca else None

    vab = br(
        sum(1 for m in vh if m.volume_percentile >= CFG.volume_abnormal_percentile_threshold),
        len(vh),
    )
    aab = br(
        sum(1 for m in ah if m.amount_percentile >= CFG.amount_abnormal_percentile_threshold),
        len(ah),
    )

    contrib = (ta / market_total_amount) if (market_total_amount > 0 and ta is not None) else None

    pjab = br(
        sum(
            1
            for m in joint
            if m.gap_percentile >= CFG.positive_gap_percentile_threshold
            and m.amount_percentile >= CFG.amount_abnormal_percentile_threshold
        ),
        len(joint),
    )
    njab = br(
        sum(
            1
            for m in joint
            if m.gap_percentile <= CFG.negative_gap_percentile_threshold
            and m.amount_percentile >= CFG.amount_abnormal_percentile_threshold
        ),
        len(joint),
    )

    # concentration
    elig = [m for m in scope_members if m.current_amount_eligible]
    n = len(elig)
    if n == 0:
        top1 = top3 = rawh = normh = None
    else:
        amts = [m.auction_amount for m in elig]
        total = sum(amts)
        if total <= 0:
            top1 = top3 = rawh = normh = None
        else:
            top1 = max(amts) / total
            k = min(3, n)
            topk = sorted(amts, reverse=True)[:k]
            top3 = sum(topk) / total
            rawh = sum(a * a for a in amts) / (total * total)
            normh = (rawh - 1.0 / n) / (1.0 - 1.0 / n) if n > 1 else None

    return _ScalarFact(
        equal_weight_gap=ewg,
        amount_weighted_gap=awg,
        positive_gap_breadth=pos_g,
        negative_gap_breadth=neg_g,
        positive_gap_abnormal_breadth=pos_ab,
        negative_gap_abnormal_breadth=neg_ab,
        total_auction_volume=tv,
        total_auction_amount=ta,
        volume_abnormal_breadth=vab,
        amount_abnormal_breadth=aab,
        auction_amount_market_contribution=contrib,
        positive_joint_abnormal_breadth=pjab,
        negative_joint_abnormal_breadth=njab,
        top1_amount_share=top1,
        top3_amount_share=top3,
        raw_hhi=rawh,
        normalized_hhi=normh,
        amount_share_eligible_count=n,
    )


def _assert_close(a, b, key: str):
    if a is None or b is None:
        assert a is None and b is None, f"{key}: {a} vs {b}"
        return
    # relative tolerance: large-sum float reduction order differs between
    # vectorized (np.sum) and scalar (Python sum); absolute 1e-9 too tight.
    tol = 1e-6 * max(1.0, abs(a), abs(b))
    assert abs(a - b) < tol, f"{key}: {a} vs {b}"


# ---------------------------------------------------------------------------
# Deterministic cases
# ---------------------------------------------------------------------------
def _run_single(members, scope_members_idx, market_idx):
    scopes = [
        {"scope_id": "market", "scope_family": "market", "member_indices": market_idx},
        {"scope_id": "s1", "scope_family": "industry", "member_indices": scope_members_idx},
    ]
    res = compute_auction_l1_scope_facts(members, scopes, CFG)
    return res[1]  # s1


def test_01_ordinary_scope():
    members = [
        _mk("A", gap_pct=2.0, auction_amount=100.0, gap_percentile=60.0, amount_percentile=60.0,
            current_gap_eligible=True, gap_history_eligible=True, current_amount_eligible=True,
            amount_history_eligible=True),
        _mk("B", gap_pct=-1.0, auction_amount=50.0, gap_percentile=40.0, amount_percentile=40.0,
            current_gap_eligible=True, gap_history_eligible=True, current_amount_eligible=True,
            amount_history_eligible=True),
    ]
    r = _run_single(members, [0, 1], [0, 1])
    assert r.equal_weight_gap == 0.5
    assert r.positive_gap_breadth == 0.5
    assert r.negative_gap_breadth == 0.5
    assert r.total_auction_amount == 150.0


def test_02_positive_negative_zero_gap():
    members = [
        _mk("A", gap_pct=1.0, current_gap_eligible=True, gap_history_eligible=True),
        _mk("B", gap_pct=-1.0, current_gap_eligible=True, gap_history_eligible=True),
        _mk("C", gap_pct=0.0, current_gap_eligible=True, gap_history_eligible=True),
    ]
    r = _run_single(members, [0, 1, 2], [0, 1, 2])
    assert r.equal_weight_gap == 0.0
    assert r.positive_gap_breadth == 1 / 3
    assert r.negative_gap_breadth == 1 / 3
    assert r.equal_weight_gap_den == 3


def test_03_missing_current_gap():
    m = _mk("A", gap_pct=_NAN, current_gap_eligible=False, gap_history_eligible=True,
            gap_percentile=95.0, amount_history_eligible=True)
    r = _run_single([m], [0], [0])
    assert r.equal_weight_gap is None
    # but abnormal breadth uses history denominator
    assert r.positive_gap_abnormal_breadth == 1.0


def test_04_current_valid_history_not_ready():
    m = _mk("A", gap_pct=5.0, gap_percentile=95.0, current_gap_eligible=True,
            gap_history_eligible=False)
    r = _run_single([m], [0], [0])
    assert r.equal_weight_gap == 5.0
    assert r.positive_gap_abnormal_breadth is None  # history denom 0


def test_05_current_amount_valid_amount_history_not_ready():
    m = _mk("A", auction_amount=100.0, amount_percentile=95.0, current_amount_eligible=True,
            amount_history_eligible=False)
    r = _run_single([m], [0], [0])
    assert r.total_auction_amount == 100.0
    assert r.amount_abnormal_breadth is None


def test_06_current_volume_valid_volume_history_not_ready():
    m = _mk("A", auction_volume=1000.0, volume_percentile=95.0, current_volume_eligible=True,
            volume_history_eligible=False)
    r = _run_single([m], [0], [0])
    assert r.total_auction_volume == 1000.0
    assert r.volume_abnormal_breadth is None


def test_07_zero_auction_amount():
    m = _mk("A", gap_pct=2.0, auction_amount=0.0, gap_percentile=60.0, amount_percentile=60.0,
            current_gap_eligible=True, gap_history_eligible=True, current_amount_eligible=True,
            amount_history_eligible=True)
    r = _run_single([m], [0], [0])
    assert r.total_auction_amount == 0.0
    assert r.amount_weighted_gap is None  # zero weight denom -> None


def test_08_invalid_missing_amount():
    m = _mk("A", gap_pct=2.0, auction_amount=_NAN, gap_percentile=60.0, amount_percentile=_NAN,
            current_gap_eligible=True, gap_history_eligible=True, current_amount_eligible=False,
            amount_history_eligible=False)
    r = _run_single([m], [0], [0])
    assert r.total_auction_amount is None
    assert r.auction_amount_market_contribution is None
    assert r.amount_share_eligible_count == 0


def test_09_different_metric_denominators():
    members = [
        _mk("A", gap_pct=1.0, current_gap_eligible=True, gap_history_eligible=True),
        _mk("B", gap_pct=1.0, current_gap_eligible=True, gap_history_eligible=False),
        _mk("C", gap_pct=1.0, current_gap_eligible=False, gap_history_eligible=True),
    ]
    r = _run_single(members, [0, 1, 2], [0, 1, 2])
    assert r.equal_weight_gap_den == 2  # current_gap
    assert r.positive_gap_abnormal_breadth_den == 2  # gap_history


def test_10_positive_gap_abnormal():
    m = _mk("A", gap_pct=3.0, gap_percentile=95.0, current_gap_eligible=True,
            gap_history_eligible=True)
    r = _run_single([m], [0], [0])
    assert r.positive_gap_abnormal_breadth == 1.0
    assert r.negative_gap_abnormal_breadth == 0.0


def test_11_negative_gap_abnormal():
    m = _mk("A", gap_pct=-3.0, gap_percentile=5.0, current_gap_eligible=True,
            gap_history_eligible=True)
    r = _run_single([m], [0], [0])
    assert r.negative_gap_abnormal_breadth == 1.0
    assert r.positive_gap_abnormal_breadth == 0.0


def test_12_volume_abnormal():
    m = _mk("A", auction_volume=100.0, volume_percentile=95.0, current_volume_eligible=True,
            volume_history_eligible=True)
    r = _run_single([m], [0], [0])
    assert r.volume_abnormal_breadth == 1.0


def test_13_amount_abnormal():
    m = _mk("A", auction_amount=100.0, amount_percentile=95.0, current_amount_eligible=True,
            amount_history_eligible=True)
    r = _run_single([m], [0], [0])
    assert r.amount_abnormal_breadth == 1.0


def test_14_positive_joint_abnormal():
    m = _mk("A", gap_pct=3.0, auction_amount=100.0, gap_percentile=95.0, amount_percentile=95.0,
            current_gap_eligible=True, gap_history_eligible=True, current_amount_eligible=True,
            amount_history_eligible=True)
    r = _run_single([m], [0], [0])
    assert r.positive_joint_abnormal_breadth == 1.0


def test_15_negative_joint_abnormal():
    m = _mk("A", gap_pct=-3.0, auction_amount=100.0, gap_percentile=5.0, amount_percentile=95.0,
            current_gap_eligible=True, gap_history_eligible=True, current_amount_eligible=True,
            amount_history_eligible=True)
    r = _run_single([m], [0], [0])
    assert r.negative_joint_abnormal_breadth == 1.0


def test_16_ew_gap():
    members = [
        _mk("A", gap_pct=2.0, current_gap_eligible=True, gap_history_eligible=True),
        _mk("B", gap_pct=4.0, current_gap_eligible=True, gap_history_eligible=True),
        _mk("C", gap_pct=0.0, current_gap_eligible=True, gap_history_eligible=True),
    ]
    r = _run_single(members, [0, 1, 2], [0, 1, 2])
    assert r.equal_weight_gap == 2.0


def test_17_aw_gap():
    members = [
        _mk("A", gap_pct=2.0, auction_amount=100.0, current_gap_eligible=True,
            current_amount_eligible=True, gap_history_eligible=True, amount_history_eligible=True),
        _mk("B", gap_pct=4.0, auction_amount=300.0, current_gap_eligible=True,
            current_amount_eligible=True, gap_history_eligible=True, amount_history_eligible=True),
    ]
    r = _run_single(members, [0, 1], [0, 1])
    # (2*100 + 4*300) / 400 = 1400/400 = 3.5
    assert abs(r.amount_weighted_gap - 3.5) < 1e-9


def test_18_aw_zero_weight_denominator():
    members = [
        _mk("A", gap_pct=2.0, auction_amount=0.0, current_gap_eligible=True,
            current_amount_eligible=True),
    ]
    r = _run_single(members, [0], [0])
    assert r.amount_weighted_gap is None


def test_19_amount_contribution():
    members = [
        _mk("A", auction_amount=100.0, current_amount_eligible=True),
        _mk("B", auction_amount=300.0, current_amount_eligible=True),
    ]
    # scope = A only; market = A + B
    scopes = [
        {"scope_id": "market", "scope_family": "market", "member_indices": [0, 1]},
        {"scope_id": "s1", "scope_family": "industry", "member_indices": [0]},
    ]
    r = compute_auction_l1_scope_facts(members, scopes, CFG)[1]
    assert abs(r.auction_amount_market_contribution - 100 / 400) < 1e-9


def test_20_overlapping_concept_membership():
    members = [
        _mk("A", auction_amount=100.0, current_amount_eligible=True),
        _mk("B", auction_amount=100.0, current_amount_eligible=True),
    ]
    scopes = [
        {"scope_id": "market", "scope_family": "market", "member_indices": [0, 1]},
        {"scope_id": "c1", "scope_family": "concept", "member_indices": [0, 1]},
        {"scope_id": "c2", "scope_family": "concept", "member_indices": [0]},
    ]
    res = compute_auction_l1_scope_facts(members, scopes, CFG)
    c1 = res[1]
    c2 = res[2]
    # c1 = (100+100)/200 = 1.0; c2 = 100/200 = 0.5
    assert abs(c1.auction_amount_market_contribution - 1.0) < 1e-9
    assert abs(c2.auction_amount_market_contribution - 0.5) < 1e-9


def test_21_concept_contributions_total_gt_1():
    # Two overlapping concepts can each contribute > their disjoint share; sum may exceed 1
    members = [
        _mk("A", auction_amount=100.0, current_amount_eligible=True),
        _mk("B", auction_amount=100.0, current_amount_eligible=True),
    ]
    scopes = [
        {"scope_id": "market", "scope_family": "market", "member_indices": [0, 1]},
        {"scope_id": "c1", "scope_family": "concept", "member_indices": [0, 1]},
        {"scope_id": "c2", "scope_family": "concept", "member_indices": [0, 1]},
    ]
    res = compute_auction_l1_scope_facts(members, scopes, CFG)
    s = res[1].auction_amount_market_contribution + res[2].auction_amount_market_contribution
    assert s > 1.0  # allowed; not normalized


def test_22_top1():
    members = [
        _mk("A", auction_amount=100.0, current_amount_eligible=True),
        _mk("B", auction_amount=300.0, current_amount_eligible=True),
        _mk("C", auction_amount=100.0, current_amount_eligible=True),
    ]
    r = _run_single(members, [0, 1, 2], [0, 1, 2])
    assert abs(r.top1_amount_share - 0.6) < 1e-9  # 300/500


def test_23_top3_ge3():
    members = [
        _mk("A", auction_amount=100.0, current_amount_eligible=True),
        _mk("B", auction_amount=300.0, current_amount_eligible=True),
        _mk("C", auction_amount=100.0, current_amount_eligible=True),
        _mk("D", auction_amount=50.0, current_amount_eligible=True),
    ]
    r = _run_single(members, [0, 1, 2, 3], [0, 1, 2, 3])
    # top3 = (300+100+100)/550 = 500/550
    assert abs(r.top3_amount_share - 500 / 550) < 1e-9


def test_24_top3_lt3():
    members = [
        _mk("A", auction_amount=100.0, current_amount_eligible=True),
        _mk("B", auction_amount=300.0, current_amount_eligible=True),
    ]
    r = _run_single(members, [0, 1], [0, 1])
    # fewer than 3 -> all available: (100+300)/400 = 1.0
    assert abs(r.top3_amount_share - 1.0) < 1e-9


def test_25_hhi_equal():
    members = [
        _mk("A", auction_amount=100.0, current_amount_eligible=True),
        _mk("B", auction_amount=100.0, current_amount_eligible=True),
        _mk("C", auction_amount=100.0, current_amount_eligible=True),
    ]
    r = _run_single(members, [0, 1, 2], [0, 1, 2])
    # raw HHI = 3*(0.333..^2) = 1/3; normalized = 0
    assert abs(r.raw_hhi - 1 / 3) < 1e-9
    assert abs(r.normalized_hhi - 0.0) < 1e-9


def test_26_hhi_concentrated():
    members = [
        _mk("A", auction_amount=980.0, current_amount_eligible=True),
        _mk("B", auction_amount=10.0, current_amount_eligible=True),
        _mk("C", auction_amount=10.0, current_amount_eligible=True),
    ]
    r = _run_single(members, [0, 1, 2], [0, 1, 2])
    # raw = (980^2+10^2+10^2)/(1000^2) = (960400+100+100)/1e6 = 0.9606
    assert abs(r.raw_hhi - 0.9606) < 1e-9


def test_27_normalized_hhi():
    members = [
        _mk("A", auction_amount=100.0, current_amount_eligible=True),
        _mk("B", auction_amount=300.0, current_amount_eligible=True),
    ]
    r = _run_single(members, [0, 1], [0, 1])
    # raw = (1e4+9e4)/16e4 = 10/16 = 0.625; norm=(0.625-0.5)/(1-0.5)=0.25
    assert abs(r.raw_hhi - 0.625) < 1e-9
    assert abs(r.normalized_hhi - 0.25) < 1e-9


def test_28_n1_normalized_unavailable():
    m = _mk("A", auction_amount=100.0, current_amount_eligible=True)
    r = _run_single([m], [0], [0])
    assert r.raw_hhi == 1.0
    assert r.normalized_hhi is None


def test_29_zero_total_concentration_unavailable():
    members = [
        _mk("A", auction_amount=0.0, current_amount_eligible=True),
        _mk("B", auction_amount=0.0, current_amount_eligible=True),
    ]
    r = _run_single(members, [0, 1], [0, 1])
    assert r.top1_amount_share is None
    assert r.raw_hhi is None
    assert r.amount_share_eligible_count == 2  # real count preserved


def test_30_missing_not_zero():
    # missing amount (not eligible) must not count as zero amount
    members = [
        _mk("A", auction_amount=_NAN, current_amount_eligible=False),
        _mk("B", auction_amount=0.0, current_amount_eligible=True),
    ]
    r = _run_single(members, [0, 1], [0, 1])
    assert r.amount_share_eligible_count == 1
    assert r.total_auction_amount == 0.0  # only B with real zero


def test_31_historical_not_ready_vs_current_invalid():
    # different metric denom isolation proves the two are distinct states
    members = [
        _mk("A", gap_pct=5.0, gap_percentile=95.0, current_gap_eligible=True,
            gap_history_eligible=False),
        _mk("B", gap_pct=5.0, gap_percentile=95.0, current_gap_eligible=False,
            gap_history_eligible=True),
    ]
    r = _run_single(members, [0, 1], [0, 1])
    assert r.equal_weight_gap == 5.0  # only A
    assert r.positive_gap_abnormal_breadth == 1.0  # only B (history denom)


def test_32_deterministic_repeat():
    members = [
        _mk("A", gap_pct=2.0, auction_amount=100.0, gap_percentile=95.0, amount_percentile=95.0,
            current_gap_eligible=True, gap_history_eligible=True, current_amount_eligible=True,
            amount_history_eligible=True),
        _mk("B", gap_pct=-1.0, auction_amount=50.0, gap_percentile=5.0, amount_percentile=40.0,
            current_gap_eligible=True, gap_history_eligible=True, current_amount_eligible=True,
            amount_history_eligible=True),
    ]
    r1 = _run_single(members, [0, 1], [0, 1])
    r2 = _run_single(members, [0, 1], [0, 1])
    assert r1.equal_weight_gap == r2.equal_weight_gap
    assert r1.raw_hhi == r2.raw_hhi
    assert r1.amount_weighted_gap == r2.amount_weighted_gap


# ---------------------------------------------------------------------------
# Scalar-reference vs vectorized parity (fixed seed)
# ---------------------------------------------------------------------------
def _synthetic_members(n: int, seed: int) -> list[AuctionMemberFact]:
    rng = np.random.default_rng(seed)
    members = []
    for i in range(n):
        gap = float(rng.normal(0, 2))
        vol = float(rng.exponential(1e5))
        amt = float(rng.exponential(1e6))
        gp = float(rng.uniform(0, 100))
        vp = float(rng.uniform(0, 100))
        ap = float(rng.uniform(0, 100))
        members.append(_mk(
            instrument_id=f"S{i:06d}",
            gap_pct=gap, auction_volume=vol, auction_amount=amt,
            gap_percentile=gp, volume_percentile=vp, amount_percentile=ap,
            current_gap_eligible=bool(rng.random() < 0.9),
            gap_history_eligible=bool(rng.random() < 0.85),
            current_volume_eligible=bool(rng.random() < 0.9),
            volume_history_eligible=bool(rng.random() < 0.85),
            current_amount_eligible=bool(rng.random() < 0.9),
            amount_history_eligible=bool(rng.random() < 0.85),
        ))
    return members


def test_parity_vectorized_vs_scalar():
    n = 500
    members = _synthetic_members(n, seed=12345)
    # random scope partition into 8 scopes
    rng = np.random.default_rng(777)
    scope_ids = rng.integers(0, 8, size=n)
    scopes = [{"scope_id": "market", "scope_family": "market", "member_indices": list(range(n))}]
    for s in range(8):
        idx = np.nonzero(scope_ids == s)[0].tolist()
        scopes.append({"scope_id": f"s{s}", "scope_family": "industry", "member_indices": idx})

    res = compute_auction_l1_scope_facts(members, scopes, CFG)

    # market total amount
    market_total = sum(m.auction_amount for m in members if m.current_amount_eligible)

    for s in range(8):
        r = res[s + 1]
        sm = [members[i] for i in scopes[s + 1]["member_indices"]]
        ref = _scalar_compute(sm, market_total)
        _assert_close(r.equal_weight_gap, ref.equal_weight_gap, f"s{s}.ewg")
        _assert_close(r.amount_weighted_gap, ref.amount_weighted_gap, f"s{s}.awg")
        _assert_close(r.positive_gap_breadth, ref.positive_gap_breadth, f"s{s}.pgb")
        _assert_close(r.negative_gap_breadth, ref.negative_gap_breadth, f"s{s}.ngb")
        _assert_close(r.positive_gap_abnormal_breadth, ref.positive_gap_abnormal_breadth, f"s{s}.pgab")
        _assert_close(r.negative_gap_abnormal_breadth, ref.negative_gap_abnormal_breadth, f"s{s}.ngab")
        _assert_close(r.total_auction_volume, ref.total_auction_volume, f"s{s}.tv")
        _assert_close(r.total_auction_amount, ref.total_auction_amount, f"s{s}.ta")
        _assert_close(r.volume_abnormal_breadth, ref.volume_abnormal_breadth, f"s{s}.vab")
        _assert_close(r.amount_abnormal_breadth, ref.amount_abnormal_breadth, f"s{s}.aab")
        _assert_close(r.auction_amount_market_contribution, ref.auction_amount_market_contribution, f"s{s}.contrib")
        _assert_close(r.positive_joint_abnormal_breadth, ref.positive_joint_abnormal_breadth, f"s{s}.pjab")
        _assert_close(r.negative_joint_abnormal_breadth, ref.negative_joint_abnormal_breadth, f"s{s}.njab")
        _assert_close(r.top1_amount_share, ref.top1_amount_share, f"s{s}.top1")
        _assert_close(r.top3_amount_share, ref.top3_amount_share, f"s{s}.top3")
        _assert_close(r.raw_hhi, ref.raw_hhi, f"s{s}.rawhhi")
        _assert_close(r.normalized_hhi, ref.normalized_hhi, f"s{s}.normhhi")
        assert r.amount_share_eligible_count == ref.amount_share_eligible_count


# ---------------------------------------------------------------------------
# Micro-benchmark (calls production kernel only)
# ---------------------------------------------------------------------------
def _make_synthetic_scopes(n: int, s: int, seed: int):
    members = _synthetic_members(n, seed=seed)
    rng = np.random.default_rng(seed + 1)
    scope_ids = rng.integers(0, s, size=n)
    scopes = [{"scope_id": "market", "scope_family": "market", "member_indices": list(range(n))}]
    edges = n  # each member in market scope
    for sc in range(s):
        idx = np.nonzero(scope_ids == sc)[0].tolist()
        scopes.append({"scope_id": f"s{sc}", "scope_family": "industry", "member_indices": idx})
        edges += len(idx)
    return members, scopes, edges


@pytest.mark.benchmark
def test_micro_benchmark():
    scales = [12, 40, 151, 3599, 5000]
    n_scopes = 50
    print("\n=== AUCTION V2.1 L1 SCOPE FACT MICRO-BENCHMARK ===")
    print(f"{'N':>6} {'E':>8} {'S':>4} {'wall_ms':>10} {'cpu_ms':>10} {'peak_RSS_MiB':>13}")
    for n in scales:
        members, scopes, edges = _make_synthetic_scopes(n, n_scopes, seed=42)
        # warmup
        compute_auction_l1_scope_facts(members, scopes, CFG)
        t0 = time.perf_counter()
        res = compute_auction_l1_scope_facts(members, scopes, CFG)
        t1 = time.perf_counter()
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0  # MiB on macOS
        wall = (t1 - t0) * 1000.0
        # cpu time approximated via repeated short runs
        cpu_t0 = time.process_time()
        compute_auction_l1_scope_facts(members, scopes, CFG)
        cpu = (time.process_time() - cpu_t0) * 1000.0
        assert len(res) == n_scopes + 1
        print(f"{n:>6} {edges:>8} {n_scopes:>4} {wall:>10.3f} {cpu:>10.3f} {rss:>13.1f}")
    print("=== END BENCHMARK (REAL FULL-SCALE EDGE COUNT = NOT YET VERIFIED) ===")
