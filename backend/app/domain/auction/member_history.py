"""Auction V3.2 Member Historical Evidence owner (PRD AU-05 / AU-06).

Responsibility (deliberately narrow — see the V3.2 ownership split):

- :mod:`app.domain.auction.member_observation` owns``gap_ratio`` / ``auction_amount``
  for **one** day (current observation);
- THIS module owns the *historical* evidence for one instrument at one T:
  gap percentile and amount percentile computed against that instrument's own
  strictly-pre-T history.

It does NOT own board/scope computation, historical dynamics, abnormal
thresholds or any score.  Those belong to their own owners.

Hard contracts (frozen):
- baseline is **strictly before T**: T itself never enters its own denominator,
  and any observation dated ``>= T`` is dropped (never silently used);
- no future data: same guard, and no reach-back beyond the candidate window to
  top up valid values;
- Missing != Zero: an unusable history yields ``*_history_eligible = False``
  and a ``None`` percentile — never 0;
- **current ready != history ready**: ``price_ready`` on the current
  observation says nothing about ``gap_history_eligible``.  The two are
  independent and must never be derived from one another.
- the evidence for one instrument is computed ONCE and reused by every scope
  that contains it (concept overlap must not recompute it).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.domain.auction.member_observation import AuctionMemberObservation
from app.domain.shared.historical_position import (
    POSITION_MINIMUM_VALID_HISTORY,
    POSITION_WINDOW_SIZE,
    compute_historical_position,
)

__all__ = [
    "MemberHistoryEvidence",
    "compute_member_history_evidence",
    "filter_strictly_pre_t",
]

_REASON_NO_CURRENT_GAP = "GAP_CURRENT_UNAVAILABLE"
_REASON_NO_CURRENT_AMOUNT = "AMOUNT_CURRENT_UNAVAILABLE"
_REASON_FUTURE_OR_SAME_DAY_DROPPED = "FUTURE_OR_SAME_DAY_OBSERVATION_DROPPED"
_REASON_GAP_INSUFFICIENT = "GAP_HISTORY_INSUFFICIENT"
_REASON_AMOUNT_INSUFFICIENT = "AMOUNT_HISTORY_INSUFFICIENT"


def filter_strictly_pre_t(
    history: Sequence[AuctionMemberObservation],
    trade_date: date,
) -> tuple[list[AuctionMemberObservation], int]:
    """Drop any observation dated ``>= trade_date`` (T and future are excluded).

    Returns ``(kept, dropped_count)``.  Dropping is never silent: the count is
    surfaced so callers can record it as evidence.
    """
    kept: list[AuctionMemberObservation] = []
    dropped = 0
    for obs in history:
        if obs.trade_date < trade_date:
            kept.append(obs)
        else:
            dropped += 1
    kept.sort(key=lambda o: o.trade_date)
    return kept, dropped


@dataclass(frozen=True)
class MemberHistoryEvidence:
    """Historical evidence for ONE instrument at ONE trade_date.

    ``*_history_eligible`` is true only when the corresponding percentile
    reached ``status == "ready"``, i.e. the pre-T window really produced the
    minimum number of valid observations.
    """

    instrument_id: UUID
    trade_date: date

    gap_percentile: float | None
    gap_position_status: str
    gap_valid_count: int
    gap_candidate_count: int

    amount_percentile: float | None
    amount_position_status: str
    amount_valid_count: int
    amount_candidate_count: int

    gap_history_eligible: bool
    amount_history_eligible: bool

    dropped_future_or_same_day: int
    reason_codes: tuple[str, ...]


def compute_member_history_evidence(
    *,
    instrument_id: UUID,
    trade_date: date,
    current: AuctionMemberObservation,
    history: Sequence[AuctionMemberObservation],
    window_size: int = POSITION_WINDOW_SIZE,
    minimum_valid_history: int = POSITION_MINIMUM_VALID_HISTORY,
) -> MemberHistoryEvidence:
    """Compute gap + amount historical evidence for one instrument at T.

    ``history`` may contain rows dated on/after T; they are dropped by
    :func:`filter_strictly_pre_t` and counted, so no future or same-day value
    can ever enter the baseline.
    """
    kept, dropped = filter_strictly_pre_t(history, trade_date)

    gap_baseline = [obs.gap_ratio for obs in kept]
    amount_baseline = [obs.auction_amount for obs in kept]

    gap_position = compute_historical_position(
        current.gap_ratio,
        gap_baseline,
        window_size=window_size,
        minimum_valid_history=minimum_valid_history,
    )
    amount_position = compute_historical_position(
        current.auction_amount,
        amount_baseline,
        window_size=window_size,
        minimum_valid_history=minimum_valid_history,
    )

    codes: list[str] = []
    if dropped:
        codes.append(_REASON_FUTURE_OR_SAME_DAY_DROPPED)
    if current.gap_ratio is None:
        codes.append(_REASON_NO_CURRENT_GAP)
    if current.auction_amount is None:
        codes.append(_REASON_NO_CURRENT_AMOUNT)
    if gap_position["status"] == "insufficient_history":
        codes.append(_REASON_GAP_INSUFFICIENT)
    if amount_position["status"] == "insufficient_history":
        codes.append(_REASON_AMOUNT_INSUFFICIENT)

    gap_history = gap_position["history"]
    amount_history = amount_position["history"]

    return MemberHistoryEvidence(
        instrument_id=instrument_id,
        trade_date=trade_date,
        gap_percentile=gap_position["position"],
        gap_position_status=gap_position["status"],
        gap_valid_count=gap_history["valid_count"],
        gap_candidate_count=gap_history["candidate_count"],
        amount_percentile=amount_position["position"],
        amount_position_status=amount_position["status"],
        amount_valid_count=amount_history["valid_count"],
        amount_candidate_count=amount_history["candidate_count"],
        gap_history_eligible=gap_position["status"] == "ready",
        amount_history_eligible=amount_position["status"] == "ready",
        dropped_future_or_same_day=dropped,
        reason_codes=tuple(dict.fromkeys(codes)),
    )
