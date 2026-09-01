"""Bridge: V3.2 Observation + History Evidence -> the existing ``AuctionMemberFact``.

The V3.2 chain is::

    AuctionMemberObservation   (current day, provenance-agnostic)
            +
    MemberHistoryEvidence      (strictly-pre-T percentiles)
            ↓
    AuctionMemberFact          (the EXISTING single scope-calculator input)
            ↓
    compute_auction_l1_scope_facts   (unchanged, single owner)

This module deliberately does NOT create a second member-fact owner and does
NOT rewrite the scope calculator.  It only adapts into the existing contract.

Ownership rules honoured here:
- ``gap_pct`` receives the ``gap_ratio`` value **as-is**.  PRD AU-04-1 defines
  ``gap = final_price / prev_close - 1`` (a RATIO, ``+2.30%`` -> ``0.023``), and
  the frozen scale audit confirmed the attribute carries ratio semantics; so no
  ``/100`` and no ``*100`` happens anywhere, ever.
- ``current_*_eligible`` comes from the CURRENT observation only.
- ``*_history_eligible`` comes from the HISTORY evidence only.
  The two are never derived from one another (current ready != history ready).
- Volume is demoted in V3.2: it is not a formal analysis axis.  The legacy
  compatibility fields are still populated (with unavailable values) so the
  existing contract stays intact, but V3.2 must not read Volume as truth.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np

from app.domain.auction.member_fact import AuctionMemberFact
from app.domain.auction.member_history import MemberHistoryEvidence
from app.domain.auction.member_observation import AuctionMemberObservation

__all__ = ["to_member_facts"]

_NAN = float("nan")


def _to_float(value: float | None) -> float:
    """Preserve Missing != Zero across the numpy boundary (None -> NaN)."""
    return _NAN if value is None else float(value)


def to_member_facts(
    observations: Sequence[AuctionMemberObservation],
    evidence_by_instrument: dict,
) -> list[AuctionMemberFact]:
    """Adapt observations + their history evidence into ``AuctionMemberFact``.

    ``evidence_by_instrument`` maps ``instrument_id -> MemberHistoryEvidence``.
    An instrument without evidence is adapted with history eligibility False
    (never assumed eligible).
    """
    facts: list[AuctionMemberFact] = []
    for obs in observations:
        ev: MemberHistoryEvidence | None = evidence_by_instrument.get(obs.instrument_id)
        gap_history_eligible = bool(ev.gap_history_eligible) if ev else False
        amount_history_eligible = bool(ev.amount_history_eligible) if ev else False

        facts.append(
            AuctionMemberFact(
                instrument_id=str(obs.instrument_id),
                gap_pct=_to_float(obs.gap_ratio),
                # Volume demoted in V3.2: kept for contract compatibility,
                # unavailable by construction, and NOT a formal analysis axis.
                auction_volume=_NAN,
                auction_amount=_to_float(obs.auction_amount),
                gap_percentile=_to_float(ev.gap_percentile) if ev else _NAN,
                volume_percentile=_NAN,
                amount_percentile=_to_float(ev.amount_percentile) if ev else _NAN,
                current_gap_eligible=obs.price_ready,
                gap_history_eligible=gap_history_eligible,
                current_volume_eligible=False,
                volume_history_eligible=False,
                current_amount_eligible=obs.amount_ready,
                amount_history_eligible=amount_history_eligible,
            )
        )
    return facts


def to_member_fact_dict(
    observations: Iterable[AuctionMemberObservation],
    evidence_by_instrument: dict,
) -> dict:
    """Convenience: adapted facts keyed by instrument_id (for bulk reuse)."""
    return {
        f.instrument_id: f
        for f in to_member_facts(list(observations), evidence_by_instrument)
    }


def nan() -> float:
    """Expose the canonical unavailable marker used across the adapter."""
    return np.nan
