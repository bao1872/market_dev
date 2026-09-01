"""Auction V3.2 Member Contribution owner (V3.2 §十二).

Three transparent contributions, each derived from the SAME current member
facts and the SAME joint-valid denominators used by the Scope calculator — so
they reconcile against the Scope facts instead of being independent opinions:

- **EW contribution** = ``gap_i / price_valid_count``
  -> ``sum(EW contribution) == EW Gap``
- **Amount share** = ``amount_i / scope_total_amount``
  -> ``sum(Amount share) == 1``
- **AW contribution** = ``(amount_i / joint_total_amount) * gap_i``
  -> ``sum(AW contribution) == AW Gap``

Positive and negative contributions are both preserved: the point of this view
is to explain *why* a board looks the way it does, not to produce a top-gainer
list.  V3.2 explicitly rejects "只列涨幅最大的股票".

Missing != Zero: a member without a usable gap contributes ``None``, and its
denominator slot is *not* silently dropped from the reconciliation identity —
the caller must see the valid counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

__all__ = [
    "MemberContribution",
    "ContributionResult",
    "compute_contributions",
]

_RECON_TOLERANCE = 1e-9


@dataclass(frozen=True)
class MemberContribution:
    instrument_id: UUID
    trade_date: date
    gap_ratio: float | None
    auction_amount: float | None
    ew_contribution: float | None
    amount_share: float | None
    aw_contribution: float | None


@dataclass(frozen=True)
class ContributionResult:
    members: tuple[MemberContribution, ...]
    price_valid_count: int
    amount_valid_count: int
    joint_valid_count: int
    scope_total_amount: float | None
    joint_total_amount: float | None
    #: machine-checkable reconciliation (V3.2 §二十四)
    ew_sum: float | None
    aw_sum: float | None
    amount_share_sum: float | None
    positive_ew: tuple[MemberContribution, ...]
    negative_ew: tuple[MemberContribution, ...]
    positive_aw: tuple[MemberContribution, ...]
    negative_aw: tuple[MemberContribution, ...]
    top_amount: tuple[MemberContribution, ...]


def compute_contributions(
    *,
    trade_date: date,
    members: Any,
    ew_gap: float | None,
    aw_gap: float | None,
    scope_total_amount: float | None,
    top_amount_limit: int = 10,
) -> ContributionResult:
    """Compute the three contributions for one scope.

    Args:
        trade_date: the T being explained.
        members: iterable of objects exposing ``instrument_id``, ``gap_ratio``
            and ``auction_amount`` (i.e. :class:`AuctionMemberObservation`).
        ew_gap / aw_gap: the Scope facts to reconcile against (may be ``None``).
        scope_total_amount: the Scope Total Auction Amount (amount-share base).
        top_amount_limit: how many top-amount members to surface.
    """
    rows = list(members)

    price_valid = [m for m in rows if m.gap_ratio is not None]
    amount_valid = [m for m in rows if m.auction_amount is not None]
    joint_valid = [
        m for m in rows if m.gap_ratio is not None and m.auction_amount is not None
    ]

    price_count = len(price_valid)
    joint_total = sum(m.auction_amount for m in joint_valid) if joint_valid else None
    if joint_total is not None and joint_total <= 0:
        joint_total = None

    out: list[MemberContribution] = []
    for m in rows:
        ew = m.gap_ratio / price_count if (m.gap_ratio is not None and price_count) else None
        share = (
            m.auction_amount / scope_total_amount
            if (
                m.auction_amount is not None
                and scope_total_amount is not None
                and scope_total_amount > 0
            )
            else None
        )
        aw = (
            (m.auction_amount / joint_total) * m.gap_ratio
            if (joint_total is not None and m.gap_ratio is not None and m.auction_amount is not None)
            else None
        )
        out.append(
            MemberContribution(
                instrument_id=m.instrument_id,
                trade_date=trade_date,
                gap_ratio=m.gap_ratio,
                auction_amount=m.auction_amount,
                ew_contribution=ew,
                amount_share=share,
                aw_contribution=aw,
            )
        )

    def _sum(values: list[float | None]) -> float | None:
        present = [v for v in values if v is not None]
        return sum(present) if present else None

    ew_sum = _sum([c.ew_contribution for c in out])
    aw_sum = _sum([c.aw_contribution for c in out])
    share_sum = _sum([c.amount_share for c in out])

    positive_ew = tuple(c for c in out if c.ew_contribution is not None and c.ew_contribution > 0)
    negative_ew = tuple(c for c in out if c.ew_contribution is not None and c.ew_contribution < 0)
    positive_aw = tuple(c for c in out if c.aw_contribution is not None and c.aw_contribution > 0)
    negative_aw = tuple(c for c in out if c.aw_contribution is not None and c.aw_contribution < 0)
    top_amount = tuple(
        sorted(
            (c for c in out if c.auction_amount is not None),
            key=lambda c: (-c.auction_amount, str(c.instrument_id)),
        )[:top_amount_limit]
    )

    return ContributionResult(
        members=tuple(out),
        price_valid_count=price_count,
        amount_valid_count=len(amount_valid),
        joint_valid_count=len(joint_valid),
        scope_total_amount=scope_total_amount,
        joint_total_amount=joint_total,
        ew_sum=ew_sum,
        aw_sum=aw_sum,
        amount_share_sum=share_sum,
        positive_ew=positive_ew,
        negative_ew=negative_ew,
        positive_aw=positive_aw,
        negative_aw=negative_aw,
        top_amount=top_amount,
    )


def reconcile(
    result: ContributionResult,
    *,
    ew_gap: float | None,
    aw_gap: float | None,
    tolerance: float = _RECON_TOLERANCE,
) -> dict[str, bool]:
    """Machine-checkable reconciliation identities (V3.2 §二十四)."""
    def _close(a: float | None, b: float | None) -> bool:
        if a is None or b is None:
            return a is None and b is None
        return abs(a - b) <= tolerance

    return {
        "ew_sum_matches_ew_gap": _close(result.ew_sum, ew_gap),
        "aw_sum_matches_aw_gap": _close(result.aw_sum, aw_gap),
        "amount_share_sum_is_one": _close(result.amount_share_sum, 1.0),
    }
