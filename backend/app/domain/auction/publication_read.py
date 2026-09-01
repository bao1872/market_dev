"""Auction V3.2 publication read-model owner.

The formal read chain (V3.2 §三)::

    (trade_date, algorithm_version)
            -> AuctionAnalysisPublication   (visibility boundary)
            -> scan_run_id
            -> AuctionScopeResult
            -> V3.2 payload (schema_version validated)

Explicitly forbidden here:
- picking "the latest scan run";
- picking "the latest succeeded run";
- reading the newest ``AuctionScopeResult`` directly.

A run that is newer but NOT published is invisible.  That is the whole point of
the publication pointer, and it is what the tests in
``tests/test_auction_v32_api_contract.py`` pin.

Everything in this module is pure: it receives already-loaded rows and returns
selections/mappings.  It never recomputes EW/AW/Capital Tilt/HHI/Position/
Velocity/Contribution/Leadership — those are produced by their own owners and
only READ out of the payload here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, Protocol
from uuid import UUID

from app.domain.auction.scope_payload import parse_scope_payload

__all__ = [
    "V32_ALGORITHM_VERSION",
    "PublishedRun",
    "select_published_run",
    "read_published_scope_results",
    "ScopeListItem",
    "to_scope_list_items",
    "to_scope_detail",
]

V32_ALGORITHM_VERSION = "auction-v3.2"


class PublicationRow(Protocol):
    trade_date: date
    algorithm_version: str
    scan_run_id: UUID
    published_at: Any


class ScopeResultRow(Protocol):
    scan_run_id: UUID
    trade_date: date
    scope_type: str
    scope_id: UUID | None
    scope_name: str | None
    payload: Any


class PublishedRun:
    """The one run that is formally visible for a (date, algorithm) pair."""

    def __init__(self, scan_run_id: UUID, published_at: Any) -> None:
        self.scan_run_id = scan_run_id
        self.published_at = published_at


def select_published_run(
    publications: Sequence[PublicationRow],
    *,
    trade_date: date,
    algorithm_version: str = V32_ALGORITHM_VERSION,
) -> PublishedRun | None:
    """Select the publication for (trade_date, algorithm_version).

    Returns ``None`` when nothing is published — the caller must then treat the
    day as unavailable, never fall back to an unpublished run.
    """
    matches = [
        p
        for p in publications
        if p.trade_date == trade_date and p.algorithm_version == algorithm_version
    ]
    if not matches:
        return None
    latest = max(matches, key=lambda p: p.published_at)
    return PublishedRun(latest.scan_run_id, latest.published_at)


def read_published_scope_results(
    publications: Sequence[PublicationRow],
    scope_results: Sequence[ScopeResultRow],
    *,
    trade_date: date,
    family: str,
    algorithm_version: str = V32_ALGORITHM_VERSION,
) -> list[ScopeResultRow]:
    """Return the COMPLETE same-family snapshot for the published run.

    No slicing / no Top-N: the list supports local sorting and pagination in the
    UI.  An unpublished day yields an empty list.
    """
    run = select_published_run(
        publications, trade_date=trade_date, algorithm_version=algorithm_version
    )
    if run is None:
        return []
    return [
        row
        for row in scope_results
        if row.scan_run_id == run.scan_run_id
        and row.trade_date == trade_date
        and row.scope_type == family
    ]


class ScopeListItem:
    """Flat list row: values are READ from the payload, never recomputed."""

    __slots__ = (
        "scope_key",
        "scope_name",
        "equal_weight_gap",
        "amount_weighted_gap",
        "capital_tilt",
        "positive_gap_breadth",
        "negative_gap_breadth",
        "unchanged_gap_breadth",
        "gap_dispersion",
        "price_normalized_hhi",
        "ew_position",
        "ew_velocity",
        "ew_acceleration",
        "amount_historical_position",
        "amount_multiple",
        "amount_abnormal_breadth",
        "total_auction_amount",
        "normalized_hhi",
        "top3_amount_share",
        "cross_sectional",
        "leadership_migration",
        "price_valid_count",
    )

    def __init__(
        self,
        *,
        scope_key: str,
        scope_name: str | None,
        repricing: dict[str, Any],
        dynamics: dict[str, Any],
        participation: dict[str, Any],
        cross_sectional: dict[str, Any],
        attribution: dict[str, Any],
    ) -> None:
        self.scope_key = scope_key
        self.scope_name = scope_name
        self.equal_weight_gap = repricing.get("equal_weight_gap")
        self.amount_weighted_gap = repricing.get("amount_weighted_gap")
        self.capital_tilt = repricing.get("capital_tilt")
        self.positive_gap_breadth = repricing.get("positive_gap_breadth")
        self.negative_gap_breadth = repricing.get("negative_gap_breadth")
        self.unchanged_gap_breadth = repricing.get("unchanged_gap_breadth")
        self.gap_dispersion = repricing.get("gap_dispersion")
        self.price_normalized_hhi = repricing.get("price_normalized_hhi")
        self.ew_position = dynamics.get("position")
        self.ew_velocity = dynamics.get("velocity")
        self.ew_acceleration = dynamics.get("acceleration")
        self.amount_historical_position = participation.get("amount_position")
        self.amount_multiple = participation.get("amount_multiple")
        self.amount_abnormal_breadth = participation.get("amount_abnormal_breadth")
        self.total_auction_amount = participation.get("total_auction_amount")
        self.normalized_hhi = participation.get("amount_normalized_hhi")
        self.top3_amount_share = participation.get("top3_amount_share")
        self.cross_sectional = cross_sectional
        self.leadership_migration = attribution.get("leadership_migration")
        self.price_valid_count = repricing.get("price_valid_count") or 0


def to_scope_list_items(
    rows: Sequence[ScopeResultRow],
) -> list[ScopeListItem]:
    """Map published scope rows to list DTOs (read-only mapping)."""
    items: list[ScopeListItem] = []
    for row in rows:
        payload = parse_scope_payload(row.payload)
        items.append(
            ScopeListItem(
                scope_key=row.scope_name or str(row.scope_id),
                scope_name=row.scope_name,
                repricing=payload["repricing"],
                dynamics=payload["historical_dynamics"],
                participation=payload["participation"],
                cross_sectional=payload["cross_sectional"],
                attribution=payload["member_attribution"],
            )
        )
    return items


def to_scope_detail(row: ScopeResultRow) -> dict[str, Any]:
    """Return the five canonical groups plus diagnostics (read-only)."""
    payload = parse_scope_payload(row.payload)
    diagnostics = dict(payload.get("diagnostics") or {})
    # technical identifiers stay in diagnostics only
    diagnostics.setdefault("scope_id", str(row.scope_id))
    diagnostics.setdefault("scan_run_id", str(row.scan_run_id))
    return {
        "repricing": payload["repricing"],
        "historical_dynamics": payload["historical_dynamics"],
        "participation": payload["participation"],
        "cross_sectional": payload["cross_sectional"],
        "member_attribution": payload["member_attribution"],
        "diagnostics": diagnostics,
    }


def published_dates(
    publications: Sequence[PublicationRow],
    *,
    algorithm_version: str = V32_ALGORITHM_VERSION,
) -> list[date]:
    """Trade dates that have a FORMAL V3.2 publication (newest first).

    Historical-data dates are deliberately NOT included: having history does not
    mean a V3.2 page exists for that day (PRD AU-04-6).
    """
    dates = {
        p.trade_date for p in publications if p.algorithm_version == algorithm_version
    }
    return sorted(dates, reverse=True)
