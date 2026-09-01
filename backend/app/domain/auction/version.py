"""Auction V3.2 algorithm-version owner — ONE machine definition.

Governance: "version binding" is one semantic with one machine owner.  The
writer, the publication read model, the API and the payload builder must all
import the value from here.  A second literal definition anywhere else is a
defect: if the writer moves to a new version and a reader keeps the old one,
results would be written successfully and then never be visible to the API.

This module deliberately owns ONLY the algorithm version.  The JSONB payload
schema version stays with the payload contract owner
(:mod:`app.domain.auction.scope_payload`) so the two concerns do not import
each other.
"""

from __future__ import annotations

__all__ = ["V32_ALGORITHM_VERSION"]

#: Stable identity of the V3.2 computation contract.  Used as
#: ``AuctionScanRun.algorithm_version`` and therefore as half of the
#: publication unique key ``(trade_date, algorithm_version)``.
V32_ALGORITHM_VERSION = "auction-v3.2"
