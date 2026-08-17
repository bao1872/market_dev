"""Auction V2.1 canonical L1 Scope Fact owner (vectorized).

Single production numerical owner for all L1 scope facts: breadth, participation,
joint breadth, amount contribution, concentration, EW/AW gap.

This module is independent from:
- legacy AuctionAnchor implementation (auction_anchor_service.py);
- the Review canonical domain (app.domain.review.*).

Compute shape contract (PRD §9/§10/AU-24-4): columnar arrays + boolean masks
+ batch/vector reductions (``np.bincount`` / masks / ``np.partition``). No
scope×member Python nested numerical loop in the hot path.

Semantic contract (PRD §9/§10/§11/§16/AU-24-5): metric-specific eligibility
denominators, missing != zero, historical-not-ready != current-invalid, concept
overlap (contribution allowed > 100%, not normalized), amount/volume
directionless.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.domain.auction.member_fact import (
    AuctionMemberFact,
    AuctionMemberFactConfig,
    build_auction_member_facts,
)

__all__ = ["AuctionL1ScopeFact", "compute_auction_l1_scope_facts"]

_NAN = float("nan")


@dataclass
class AuctionL1ScopeFact:
    """Canonical L1 scope fact for one scope.

    Every ratio/breadth records numerator_count + denominator_count + ratio so
    the denominator is independently verifiable (PRD AU-24-7). Ratios that are
    unavailable record ``ratio=None`` (never 0 masquerading unavailable).
    """

    # scope identity (kept outside the numerical hot path)
    scope_id: str
    scope_family: str  # "market" | "industry" | "concept" (or equivalent)

    # --- A. PRICE ---
    equal_weight_gap: float | None = None
    equal_weight_gap_num: int = 0
    equal_weight_gap_den: int = 0

    amount_weighted_gap: float | None = None
    amount_weighted_gap_num: float = 0.0
    amount_weighted_gap_den: float = 0.0

    positive_gap_breadth: float | None = None
    positive_gap_breadth_num: int = 0
    positive_gap_breadth_den: int = 0

    negative_gap_breadth: float | None = None
    negative_gap_breadth_num: int = 0
    negative_gap_breadth_den: int = 0

    positive_gap_abnormal_breadth: float | None = None
    positive_gap_abnormal_breadth_num: int = 0
    positive_gap_abnormal_breadth_den: int = 0

    negative_gap_abnormal_breadth: float | None = None
    negative_gap_abnormal_breadth_num: int = 0
    negative_gap_abnormal_breadth_den: int = 0

    # --- B. PARTICIPATION ---
    total_auction_volume: float | None = None
    total_auction_volume_den: int = 0

    total_auction_amount: float | None = None
    total_auction_amount_den: int = 0

    volume_abnormal_breadth: float | None = None
    volume_abnormal_breadth_num: int = 0
    volume_abnormal_breadth_den: int = 0

    amount_abnormal_breadth: float | None = None
    amount_abnormal_breadth_num: int = 0
    amount_abnormal_breadth_den: int = 0

    auction_amount_market_contribution: float | None = None
    auction_amount_market_contribution_num: float = 0.0
    auction_amount_market_contribution_den: float = 0.0

    # --- C. JOINT ---
    positive_joint_abnormal_breadth: float | None = None
    positive_joint_abnormal_breadth_num: int = 0
    positive_joint_abnormal_breadth_den: int = 0

    negative_joint_abnormal_breadth: float | None = None
    negative_joint_abnormal_breadth_num: int = 0
    negative_joint_abnormal_breadth_den: int = 0

    # --- CONCENTRATION (amount-share universe) ---
    top1_amount_share: float | None = None
    top3_amount_share: float | None = None
    raw_hhi: float | None = None
    normalized_hhi: float | None = None
    amount_share_eligible_count: int = 0

    @staticmethod
    def _ratio(num: int, den: int) -> float | None:
        if den <= 0:
            return None
        return num / den


def _safe_ratio(num: float, den: float) -> float | None:
    if den is None or den <= 0:
        return None
    return num / den


@dataclass
class _ScopeMeta:
    scope_id: str
    scope_family: str


def compute_auction_l1_scope_facts(
    members: list[AuctionMemberFact],
    scopes: list[dict],
    config: AuctionMemberFactConfig,
    market_scope_id: str = "market",
) -> list[AuctionL1ScopeFact]:
    """Canonical V2.1 L1 Scope Fact owner.

    Parameters
    ----------
    members:
        Prepared canonical member facts (already have values + eligibility).
    scopes:
        List of scope descriptors. Each must contain:
          - ``scope_id`` (str)
          - ``scope_family`` (str: market / industry / concept / ...)
          - ``member_indices`` (list[int]) — edge representation into ``members``
    config:
        Explicit caller-provided thresholds.
    market_scope_id:
        Identifier whose scope is treated as the *Market* universe denominator for
        amount contribution. Must be present in ``scopes`` (or a market scope with
        that id). Concept overlap does NOT change the market denominator.

    Returns
    -------
    list[AuctionL1ScopeFact]
        One result per scope, in input order.
    """
    # ---- ONE-TIME conversion to columnar arrays (no per-member hot loop) ----
    arr = build_auction_member_facts(members)
    gap = arr["gap_pct"]
    volume = arr["auction_volume"]
    amount = arr["auction_amount"]
    gap_pct = arr["gap_percentile"]
    vol_pct = arr["volume_percentile"]
    amt_pct = arr["amount_percentile"]

    cur_gap = arr["current_gap_eligible"]
    gap_hist = arr["gap_history_eligible"]
    cur_vol = arr["current_volume_eligible"]
    vol_hist = arr["volume_history_eligible"]
    cur_amt = arr["current_amount_eligible"]
    amt_hist = arr["amount_history_eligible"]
    joint = arr["joint_eligible"]

    # Precomputed per-member boolean signal masks (computed ONCE, reused across
    # scopes — never recomputed per scope and never per member in a Python loop).
    gap_pos = gap > 0.0
    gap_neg = gap < 0.0
    # gap == 0.0 enters current_gap denominator (via cg_mask) but no sign numerator
    gap_abn_pos = gap_pct >= config.positive_gap_percentile_threshold
    gap_abn_neg = gap_pct <= config.negative_gap_percentile_threshold
    vol_abn = vol_pct >= config.volume_abnormal_percentile_threshold
    amt_abn = amt_pct >= config.amount_abnormal_percentile_threshold

    # Market denominator for amount contribution (concept overlap invariant):
    # built from the market scope's current_amount_eligible members only.
    market_amount_den = 0.0
    for sc in scopes:
        if sc["scope_id"] == market_scope_id:
            midx = np.asarray(sc["member_indices"], dtype=np.int64)
            if midx.size:
                m_amt = amount[midx]
                m_cur = cur_amt[midx]
                market_amount_den = float(np.sum(m_amt[m_cur]))
            break

    results: list[AuctionL1ScopeFact] = []

    # Small OUTER scope loop is allowed (AU-24-4). Inside, all reductions are
    # vectorized via boolean masks + np.bincount-style indexing over edges.
    for sc in scopes:
        meta = _ScopeMeta(scope_id=sc["scope_id"], scope_family=sc["scope_family"])
        midx = np.asarray(sc["member_indices"], dtype=np.int64)

        res = AuctionL1ScopeFact(scope_id=meta.scope_id, scope_family=meta.scope_family)

        if midx.size == 0:
            results.append(res)
            continue

        # Sliced columnar views for this scope's edges (no list reconstruction)
        s_gap = gap[midx]
        s_vol = volume[midx]
        s_amt = amount[midx]
        s_cur_gap = cur_gap[midx]
        s_gap_hist = gap_hist[midx]
        s_cur_vol = cur_vol[midx]
        s_vol_hist = vol_hist[midx]
        s_cur_amt = cur_amt[midx]
        s_amt_hist = amt_hist[midx]
        s_joint = joint[midx]

        s_gap_pos = gap_pos[midx]
        s_gap_neg = gap_neg[midx]
        s_gap_abn_pos = gap_abn_pos[midx]
        s_gap_abn_neg = gap_abn_neg[midx]
        s_vol_abn = vol_abn[midx]
        s_amt_abn = amt_abn[midx]

        # ---------------- A. PRICE ----------------
        cg_den = int(np.count_nonzero(s_cur_gap))
        if cg_den > 0:
            cg_mask = s_cur_gap
            res.equal_weight_gap_den = cg_den
            res.equal_weight_gap_num = int(np.count_nonzero(cg_mask))
            res.equal_weight_gap = float(np.sum(s_gap[cg_mask])) / cg_den

            pos_num = int(np.count_nonzero(s_gap_pos & cg_mask))
            res.positive_gap_breadth_num = pos_num
            res.positive_gap_breadth_den = cg_den
            res.positive_gap_breadth = pos_num / cg_den

            neg_num = int(np.count_nonzero(s_gap_neg & cg_mask))
            res.negative_gap_breadth_num = neg_num
            res.negative_gap_breadth_den = cg_den
            res.negative_gap_breadth = neg_num / cg_den
        else:
            res.equal_weight_gap_den = 0
            res.equal_weight_gap_num = 0
            res.equal_weight_gap = None
            res.positive_gap_breadth = None
            res.negative_gap_breadth = None

        # amount-weighted gap (candidate: current_gap AND current_amount)
        aw_mask = s_cur_gap & s_cur_amt
        aw_wsum = float(np.sum(s_amt[aw_mask]))
        if aw_wsum > 0:
            res.amount_weighted_gap_den = aw_wsum
            res.amount_weighted_gap_num = float(np.sum(s_gap[aw_mask] * s_amt[aw_mask]))
            res.amount_weighted_gap = res.amount_weighted_gap_num / aw_wsum
        else:
            res.amount_weighted_gap = None  # not 0

        # gap abnormal breadth (history denominator)
        gh_den = int(np.count_nonzero(s_gap_hist))
        if gh_den > 0:
            res.positive_gap_abnormal_breadth_den = gh_den
            res.positive_gap_abnormal_breadth_num = int(
                np.count_nonzero(s_gap_abn_pos & s_gap_hist)
            )
            res.positive_gap_abnormal_breadth = (
                res.positive_gap_abnormal_breadth_num / gh_den
            )

            res.negative_gap_abnormal_breadth_den = gh_den
            res.negative_gap_abnormal_breadth_num = int(
                np.count_nonzero(s_gap_abn_neg & s_gap_hist)
            )
            res.negative_gap_abnormal_breadth = (
                res.negative_gap_abnormal_breadth_num / gh_den
            )
        else:
            res.positive_gap_abnormal_breadth = None
            res.negative_gap_abnormal_breadth = None

        # ---------------- B. PARTICIPATION ----------------
        cv_den = int(np.count_nonzero(s_cur_vol))
        if cv_den > 0:
            res.total_auction_volume_den = cv_den
            res.total_auction_volume = float(np.sum(s_vol[s_cur_vol]))
        else:
            res.total_auction_volume = None

        ca_den = int(np.count_nonzero(s_cur_amt))
        if ca_den > 0:
            res.total_auction_amount_den = ca_den
            res.total_auction_amount = float(np.sum(s_amt[s_cur_amt]))
        else:
            res.total_auction_amount = None

        vh_den = int(np.count_nonzero(s_vol_hist))
        if vh_den > 0:
            res.volume_abnormal_breadth_den = vh_den
            res.volume_abnormal_breadth_num = int(
                np.count_nonzero(s_vol_abn & s_vol_hist)
            )
            res.volume_abnormal_breadth = res.volume_abnormal_breadth_num / vh_den
        else:
            res.volume_abnormal_breadth = None

        ah_den = int(np.count_nonzero(s_amt_hist))
        if ah_den > 0:
            res.amount_abnormal_breadth_den = ah_den
            res.amount_abnormal_breadth_num = int(
                np.count_nonzero(s_amt_abn & s_amt_hist)
            )
            res.amount_abnormal_breadth = res.amount_abnormal_breadth_num / ah_den
        else:
            res.amount_abnormal_breadth = None

        # amount contribution (scope total / market total; no normalization)
        if market_amount_den > 0 and ca_den > 0:
            scope_total_amt = res.total_auction_amount
            assert scope_total_amt is not None  # guaranteed by ca_den > 0
            res.auction_amount_market_contribution_den = market_amount_den
            res.auction_amount_market_contribution_num = float(scope_total_amt)
            res.auction_amount_market_contribution = float(scope_total_amt) / market_amount_den
        else:
            res.auction_amount_market_contribution = None

        # ---------------- C. JOINT ----------------
        j_den = int(np.count_nonzero(s_joint))
        if j_den > 0:
            res.positive_joint_abnormal_breadth_den = j_den
            res.positive_joint_abnormal_breadth_num = int(
                np.count_nonzero(s_gap_abn_pos & s_amt_abn & s_joint)
            )
            res.positive_joint_abnormal_breadth = (
                res.positive_joint_abnormal_breadth_num / j_den
            )

            res.negative_joint_abnormal_breadth_den = j_den
            res.negative_joint_abnormal_breadth_num = int(
                np.count_nonzero(s_gap_abn_neg & s_amt_abn & s_joint)
            )
            res.negative_joint_abnormal_breadth = (
                res.negative_joint_abnormal_breadth_num / j_den
            )
        else:
            res.positive_joint_abnormal_breadth = None
            res.negative_joint_abnormal_breadth = None

        # ---------------- CONCENTRATION (amount-share universe) ----------------
        # amount share universe = current_amount_eligible members only
        amt_elig_mask = s_cur_amt
        amt_elig_idx = np.nonzero(amt_elig_mask)[0]
        res.amount_share_eligible_count = int(amt_elig_idx.size)
        if amt_elig_idx.size > 0:
            elig_amt = s_amt[amt_elig_idx]
            scope_total = float(np.sum(elig_amt))
            if scope_total <= 0:
                # zero/negative total -> unavailable, but real count preserved
                res.top1_amount_share = None
                res.top3_amount_share = None
                res.raw_hhi = None
                res.normalized_hhi = None
            else:
                # Top1
                max_amt = float(np.max(elig_amt))
                res.top1_amount_share = max_amt / scope_total

                # Top3 (use all available if fewer than 3)
                k = min(3, elig_amt.size)
                topk = np.partition(elig_amt, -k)[-k:]
                res.top3_amount_share = float(np.sum(topk)) / scope_total

                # raw HHI via sum(amount^2) / (sum amount)^2 (no share objects)
                res.raw_hhi = float(np.sum(elig_amt * elig_amt)) / (scope_total * scope_total)

                n = int(elig_amt.size)
                if n <= 1:
                    res.normalized_hhi = None  # N<=1 unavailable
                else:
                    res.normalized_hhi = (res.raw_hhi - 1.0 / n) / (1.0 - 1.0 / n)
        else:
            res.top1_amount_share = None
            res.top3_amount_share = None
            res.raw_hhi = None
            res.normalized_hhi = None

        results.append(res)

    return results
