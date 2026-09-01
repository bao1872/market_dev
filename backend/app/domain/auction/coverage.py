"""Auction V3.2 coverage owners — three SEPARATE meanings, never one field.

The repo has three distinct coverage concepts.  Conflating them is a real
correctness bug, so each is named and computed separately here:

1. **Capture coverage** — did the market-data acquisition succeed?
   Owner: ``AuctionQuoteCaptureRun.coverage`` (written by the capture service).
   NOT computed here.

2. **Scan coverage** — of the instruments that should be analysed today, how
   many formed a valid CURRENT auction fact?
   Owner: :func:`compute_scan_coverage` below; persisted on
   ``AuctionScanRun.coverage_ratio``.  Publication only READS this value.

3. **Scope coverage** — within ONE industry/concept, how many of its own
   members are valid?
   Owner: :func:`compute_scope_coverage`.  Lives in the scope payload; it is
   per-scope and must never be reused as the day-level scan coverage.

Hard rule (V3.2 §二): **Current Coverage != Historical Readiness.**
A stock with a perfectly good today-quote that happens to have fewer than 60
days of history is a VALID current fact; the short history only makes
``Position`` / ``Velocity`` / ``Acceleration`` unavailable.  History readiness
is therefore deliberately excluded from scan coverage.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.auction.member_observation import AuctionMemberObservation

__all__ = [
    "ScanCoverage",
    "ScopeCoverage",
    "compute_scan_coverage",
    "compute_scope_coverage",
]


@dataclass(frozen=True)
class ScanCoverage:
    """Day-level coverage over the instruments that should be analysed.

    ``coverage_ratio = valid_count / eligible_count`` (``0.0`` when there is
    nothing to analyse — never ``None`` masquerading as unavailable, and never
    computed anywhere else).
    """

    eligible_count: int
    valid_count: int
    price_ready_count: int
    amount_ready_count: int
    both_ready_count: int
    missing_count: int
    coverage_ratio: float
    missing_reasons: tuple[str, ...]

    def as_scan_run_fields(self) -> dict[str, Any]:
        """Project into the ``AuctionScanRun`` columns that own these values."""
        return {
            "eligible_count": self.eligible_count,
            "ready_count": self.valid_count,
            "coverage_ratio": self.coverage_ratio,
            "missing_count": self.missing_count,
            "missing_reasons": {
                "price_ready_count": self.price_ready_count,
                "amount_ready_count": self.amount_ready_count,
                "both_ready_count": self.both_ready_count,
                "codes": list(self.missing_reasons),
            },
        }


@dataclass(frozen=True)
class ScopeCoverage:
    """Per-scope member coverage — a DIFFERENT fact from scan coverage."""

    member_count: int
    valid_count: int
    coverage_ratio: float


def compute_scan_coverage(
    observations: Sequence[AuctionMemberObservation],
) -> ScanCoverage:
    """Compute day-level coverage from CURRENT observations only.

    A member is valid when it can contribute to at least one formal V3.2 axis
    (``price_ready`` = Gap, ``amount_ready`` = Auction Amount).  History
    readiness is intentionally NOT consulted.
    """
    eligible = len(observations)
    price_ready = sum(1 for o in observations if o.price_ready)
    amount_ready = sum(1 for o in observations if o.amount_ready)
    both_ready = sum(1 for o in observations if o.price_ready and o.amount_ready)
    valid = sum(1 for o in observations if o.price_ready or o.amount_ready)

    reasons: list[str] = []
    for o in observations:
        if not o.price_ready and not o.amount_ready:
            reasons.extend(o.reason_codes)

    ratio = (valid / eligible) if eligible else 0.0
    return ScanCoverage(
        eligible_count=eligible,
        valid_count=valid,
        price_ready_count=price_ready,
        amount_ready_count=amount_ready,
        both_ready_count=both_ready,
        missing_count=max(eligible - valid, 0),
        coverage_ratio=ratio,
        missing_reasons=tuple(sorted(set(reasons))),
    )


def compute_scope_coverage(
    members: Sequence[AuctionMemberObservation],
) -> ScopeCoverage:
    """Per-scope coverage using the SAME validity rule as scan coverage.

    Note that the denominator is this scope's own member count, which is why
    the result is a different fact from the day-level ``ScanCoverage``.
    """
    total = len(members)
    valid = sum(1 for m in members if m.price_ready or m.amount_ready)
    return ScopeCoverage(
        member_count=total,
        valid_count=valid,
        coverage_ratio=(valid / total) if total else 0.0,
    )
