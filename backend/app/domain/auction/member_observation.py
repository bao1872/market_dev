"""Auction V3.2 unified Member Auction Observation.

This module is the ONLY boundary that knows that Auction data can arrive from
two different lanes:

- historical lane -> ``auction_final_quotes`` rows with
  ``source == "historical_backfill"``;
- live lane -> ``auction_final_quotes`` rows written by
  ``auction_quote_capture_service`` (provider ``MootdxAuctionQuoteProvider``,
  legacy name; real transport is pytdx/tongdaxin).

Both lanes land on the SAME table with the SAME field names, so downstream
analysis never branches on provenance.  Everything after this module
(Scope computation, historical dynamics, cross-section, contribution,
leadership) consumes :class:`AuctionMemberObservation` only.

Frozen contracts (see PRD AU-04-1 / AU-04-5):
- ``gap_ratio = final_price / prev_close - 1``; ``+2.30%`` -> ``0.023``.
  Never ``/100``, never ``*100``.
- Missing != Zero: an unusable price yields ``gap_ratio = None`` and
  ``price_ready = False`` — never ``0.0``.
- ``amount`` is carried as-is; ``amount_ready`` is an independent flag from
  ``price_ready`` (no single global valid_count).

This module is pure: it touches no database, no network and no ORM session.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

__all__ = [
    "HISTORICAL_SOURCE",
    "AuctionMemberObservation",
    "compute_gap_ratio",
    "build_member_observation",
]

#: Source value used by the historical backfill writer.  Rows carrying this
#: source live in an isolated namespace and are NOT part of the live
#: ``verified_consensus`` truth chain.
HISTORICAL_SOURCE = "historical_backfill"

#: ``quality_status`` values that permit using a fact.  Anything else
#: (suspended / zero_volume / missing_field / api_error / ...) is unavailable,
#: not zero.
USABLE_QUALITY_STATUSES = frozenset({"ok"})

_REASON_MISSING_PRICE = "AUCTION_PRICE_UNAVAILABLE"
_REASON_NON_POSITIVE_PREV_CLOSE = "PREVIOUS_CLOSE_NON_POSITIVE"
_REASON_MISSING_PREV_CLOSE = "PREVIOUS_CLOSE_UNAVAILABLE"
_REASON_MISSING_AMOUNT = "AUCTION_AMOUNT_UNAVAILABLE"
_REASON_NEGATIVE_AMOUNT = "AUCTION_AMOUNT_NEGATIVE"
_REASON_QUALITY_NOT_USABLE = "QUALITY_STATUS_NOT_USABLE"


def _finite(value: object) -> float | None:
    """Coerce to a finite float; anything unusable becomes ``None`` (never 0)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def compute_gap_ratio(
    final_price: object,
    prev_close: object,
) -> float | None:
    """Canonical Auction Gap owner (PRD AU-04-1).

    ``gap_ratio = final_price / prev_close - 1`` — a RATIO, so ``+2.30%``
    is ``0.023``.  Returns ``None`` when either input is missing/non-finite or
    when ``prev_close <= 0`` (a zero or negative base has no defined ratio).
    """
    price = _finite(final_price)
    prev = _finite(prev_close)
    if price is None or prev is None or prev <= 0:
        return None
    return price / prev - 1.0


@dataclass(frozen=True)
class AuctionMemberObservation:
    """One instrument on one trade_date, in the unified V3.2 business shape.

    It deliberately contains NO board/industry/concept judgement, NO historical
    position and NO score/label: those belong to their own owners.
    """

    instrument_id: UUID
    trade_date: date
    gap_ratio: float | None
    auction_amount: float | None
    price_ready: bool
    amount_ready: bool
    reason_codes: tuple[str, ...]
    #: provenance lane, carried for diagnostics only — downstream logic must
    #: not branch on it.
    source: str


def build_member_observation(
    *,
    instrument_id: UUID,
    trade_date: date,
    final_price: object,
    prev_close: object,
    amount: object,
    quality_status: str | None = None,
    reason_codes: object = None,
    source: str = "",
) -> AuctionMemberObservation:
    """Build the unified observation from one ``auction_final_quotes`` row.

    ``quality_status`` gates the whole row: a row that the upstream classified
    as unusable yields neither a price fact nor an amount fact, regardless of
    whether the numeric columns happen to be populated.
    """
    codes: list[str] = []
    if isinstance(reason_codes, (list, tuple)):
        codes.extend(str(c) for c in reason_codes)

    quality_usable = quality_status in USABLE_QUALITY_STATUSES
    if not quality_usable:
        codes.append(_REASON_QUALITY_NOT_USABLE)

    raw_price = _finite(final_price)
    raw_prev = _finite(prev_close)
    raw_amount = _finite(amount)

    gap = compute_gap_ratio(raw_price, raw_prev)
    if raw_price is None:
        codes.append(_REASON_MISSING_PRICE)
    if raw_prev is None:
        codes.append(_REASON_MISSING_PREV_CLOSE)
    elif raw_prev <= 0:
        codes.append(_REASON_NON_POSITIVE_PREV_CLOSE)

    # Amount: present and non-negative.  A legitimately zero amount is allowed
    # and stays amount_ready — only a missing/negative amount is unavailable.
    if raw_amount is None:
        codes.append(_REASON_MISSING_AMOUNT)
        amount_ready = False
    elif raw_amount < 0:
        codes.append(_REASON_NEGATIVE_AMOUNT)
        amount_ready = False
    else:
        amount_ready = True

    price_ready = gap is not None

    if not quality_usable:
        # Upstream declared the row unusable: nothing derived from it may be
        # consumed as a fact.
        return AuctionMemberObservation(
            instrument_id=instrument_id,
            trade_date=trade_date,
            gap_ratio=None,
            auction_amount=None,
            price_ready=False,
            amount_ready=False,
            reason_codes=tuple(dict.fromkeys(codes)),
            source=source,
        )

    return AuctionMemberObservation(
        instrument_id=instrument_id,
        trade_date=trade_date,
        gap_ratio=gap,
        auction_amount=raw_amount,
        price_ready=price_ready,
        amount_ready=amount_ready,
        reason_codes=tuple(dict.fromkeys(codes)),
        source=source,
    )


def observation_from_quote_row(row: Mapping[str, Any]) -> AuctionMemberObservation:
    """Adapter for a raw ``auction_final_quotes`` mapping (either lane)."""
    return build_member_observation(
        instrument_id=row["instrument_id"],
        trade_date=row["trade_date"],
        final_price=row.get("final_price"),
        prev_close=row.get("prev_close"),
        amount=row.get("amount"),
        quality_status=row.get("quality_status"),
        reason_codes=row.get("reason_codes"),
        source=row.get("source", "") or "",
    )
