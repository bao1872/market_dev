"""Auction V2.1 canonical Member Fact contract.

This module defines the canonical *prepared* Auction member input. It does NOT
query the database, does NOT access the Review private calculator, and does NOT
recompute historical percentiles. It only expresses the already-prepared 9:25
source facts plus their metric-specific eligibility into a typed contract.

Key invariants (PRD §9 / §16 / §19 / AU-24-5):
- ``missing`` is distinguished from ``zero``. Production numerical kernel reads
  from contiguous columnar arrays; eligibility masks carry missing/invalid
  semantics, not a global valid flag.
- ``joint_eligible`` is derived at the canonical boundary from
  ``gap_history_eligible AND amount_history_eligible``. No global valid flag.
- Thresholds are explicit caller inputs; no hidden calibration constants.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["AuctionMemberFact", "AuctionMemberFactConfig", "build_auction_member_facts"]


@dataclass(frozen=True)
class AuctionMemberFactConfig:
    """Explicit, caller-provided thresholds.

    Production code must pass all thresholds explicitly. Calibration candidates
    (e.g. 90 / 10) must NOT become hidden production constants.
    """

    positive_gap_percentile_threshold: float
    negative_gap_percentile_threshold: float
    volume_abnormal_percentile_threshold: float
    amount_abnormal_percentile_threshold: float


@dataclass(frozen=True)
class AuctionMemberFact:
    """Single canonical prepared Auction member input.

    Percentile / value fields use ``float("nan")`` to mark *missing* (not
    measured / not available). Eligibility masks carry the *invalid* / *not
    ready* semantics. ``missing != zero`` and ``invalid != zero`` are both
    preserved through these two independent representations.

    Note: in the hot numerical path these fields are NOT iterated per-member;
    they are collected once into columnar arrays by
    :func:`build_auction_member_facts`.
    """

    instrument_id: str

    # Raw prepared values (NaN == missing / not available)
    gap_pct: float
    auction_volume: float
    auction_amount: float
    gap_percentile: float
    volume_percentile: float
    amount_percentile: float

    # metric-specific eligibility
    current_gap_eligible: bool
    gap_history_eligible: bool
    current_volume_eligible: bool
    volume_history_eligible: bool
    current_amount_eligible: bool
    amount_history_eligible: bool

    @property
    def joint_eligible(self) -> bool:
        # Canonical boundary derivation (AU-24 / PRD §11):
        # gap_history AND amount_history. No global valid flag.
        return self.gap_history_eligible and self.amount_history_eligible


def build_auction_member_facts(
    rows: list[AuctionMemberFact],
) -> dict[str, np.ndarray]:
    """Convert the member contract list into contiguous columnar arrays.

    Per-member dataclass objects are NOT constructed in the numerical hot loop.
    This single conversion is the only place member facts become arrays; the
    L1 kernel consumes these arrays directly.

    Returns a dict of columnar arrays keyed by field. Eligibility is packed as
    ``bool`` ndarrays; values as ``float64`` ndarrays (NaN carries missing).
    """
    n = len(rows)
    if n == 0:
        return {
            "instrument_id": np.empty(0, dtype=object),
            "gap_pct": np.empty(0, dtype=np.float64),
            "auction_volume": np.empty(0, dtype=np.float64),
            "auction_amount": np.empty(0, dtype=np.float64),
            "gap_percentile": np.empty(0, dtype=np.float64),
            "volume_percentile": np.empty(0, dtype=np.float64),
            "amount_percentile": np.empty(0, dtype=np.float64),
            "current_gap_eligible": np.empty(0, dtype=bool),
            "gap_history_eligible": np.empty(0, dtype=bool),
            "current_volume_eligible": np.empty(0, dtype=bool),
            "volume_history_eligible": np.empty(0, dtype=bool),
            "current_amount_eligible": np.empty(0, dtype=bool),
            "amount_history_eligible": np.empty(0, dtype=bool),
            "joint_eligible": np.empty(0, dtype=bool),
        }

    out: dict[str, np.ndarray] = {
        "instrument_id": np.array([r.instrument_id for r in rows], dtype=object),
        "gap_pct": np.array([r.gap_pct for r in rows], dtype=np.float64),
        "auction_volume": np.array([r.auction_volume for r in rows], dtype=np.float64),
        "auction_amount": np.array([r.auction_amount for r in rows], dtype=np.float64),
        "gap_percentile": np.array([r.gap_percentile for r in rows], dtype=np.float64),
        "volume_percentile": np.array([r.volume_percentile for r in rows], dtype=np.float64),
        "amount_percentile": np.array([r.amount_percentile for r in rows], dtype=np.float64),
        "current_gap_eligible": np.array(
            [r.current_gap_eligible for r in rows], dtype=bool
        ),
        "gap_history_eligible": np.array(
            [r.gap_history_eligible for r in rows], dtype=bool
        ),
        "current_volume_eligible": np.array(
            [r.current_volume_eligible for r in rows], dtype=bool
        ),
        "volume_history_eligible": np.array(
            [r.volume_history_eligible for r in rows], dtype=bool
        ),
        "current_amount_eligible": np.array(
            [r.current_amount_eligible for r in rows], dtype=bool
        ),
        "amount_history_eligible": np.array(
            [r.amount_history_eligible for r in rows], dtype=bool
        ),
        "joint_eligible": np.array([r.joint_eligible for r in rows], dtype=bool),
    }
    return out
