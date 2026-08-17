"""Auction V2.1 canonical domain.

Code Alignment owner for:
- Member Facts (canonical prepared Auction member input)
- L1 Scope Facts (breadth / participation / joint / contribution / concentration)

This package is intentionally independent from the legacy AuctionAnchor
implementation and from the Review canonical domain. See PRD §24 (AU-24).
"""

from app.domain.auction.member_fact import (
    AuctionMemberFact,
    build_auction_member_facts,
)
from app.domain.auction.scope_fact import (
    AuctionL1ScopeFact,
    compute_auction_l1_scope_facts,
)

__all__ = [
    "AuctionMemberFact",
    "build_auction_member_facts",
    "AuctionL1ScopeFact",
    "compute_auction_l1_scope_facts",
]
