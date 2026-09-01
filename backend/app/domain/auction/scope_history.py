"""Auction V3.2 historical Scope series builder (one pass, one calculator).

Builds ``D-120 ... D-1, T`` Scope Facts for every industry and concept, using
the SAME single scope calculator (``compute_auction_l1_scope_facts``) that the
current-day path uses.  There is no second historical Scope calculator.

Performance contract (V3.2 §九):
- PIT membership for the whole date range is resolved ONCE
  (``resolve_scope_members_bulk``), never per scope / per day;
- member observations are already grouped by date by the caller (one bulk read);
- ``compute_auction_l1_scope_facts`` is invoked ONCE PER DATE with all of that
  day's scopes, so one instrument belonging to many concepts is aggregated from
  the same already-built columnar arrays;
- history evidence is only needed where historical abnormality is read (today);
  historical days need EW/AW/breadth only, so they are adapted without evidence
  and no percentile is recomputed for them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from app.domain.auction.member_fact import AuctionMemberFactConfig
from app.domain.auction.member_fact_adapter import to_member_facts
from app.domain.auction.member_history import MemberHistoryEvidence
from app.domain.auction.member_observation import AuctionMemberObservation
from app.domain.auction.membership_pit import (
    FAMILY_CONCEPT,
    FAMILY_INDUSTRY,
    MembershipEdge,
    resolve_scope_members_bulk,
)
from app.domain.auction.scope_fact import (
    AuctionL1ScopeFact,
    compute_auction_l1_scope_facts,
)

__all__ = ["ScopeHistorySeries", "build_scope_history_series"]


@dataclass(frozen=True)
class ScopeHistorySeries:
    """Scope Facts grouped by family, then by date, then by scope_key."""

    industry: dict[date, dict[str, AuctionL1ScopeFact]]
    concept: dict[date, dict[str, AuctionL1ScopeFact]]

    def ew_gap_series(self, family: str, scope_key: str) -> list[tuple[date, float | None]]:
        """Ordered ``(trade_date, equal_weight_gap)`` for one scope.

        Missing days keep a slot with ``None`` — never dropped, never zeroed.
        """
        table = self.industry if family == FAMILY_INDUSTRY else self.concept
        out: list[tuple[date, float | None]] = []
        for d in sorted(table):
            fact = table[d].get(scope_key)
            out.append((d, fact.equal_weight_gap if fact else None))
        return out


def _build_one_family(
    *,
    family: str,
    trade_dates: Sequence[date],
    observations_by_date: dict[date, Sequence[AuctionMemberObservation]],
    membership_by_date: dict[date, dict[str, tuple[UUID, ...]]],
    evidence_by_date: dict[date, dict[UUID, MemberHistoryEvidence]] | None,
    config: AuctionMemberFactConfig,
) -> dict[date, dict[str, AuctionL1ScopeFact]]:
    result: dict[date, dict[str, AuctionL1ScopeFact]] = {}
    for d in trade_dates:
        scopes_at_d = membership_by_date.get(d, {})
        observations = observations_by_date.get(d)
        if observations is None:
            result[d] = {}
            continue

        # index -> instrument, plus a reverse map, so scope edges can be
        # expressed as positional indices without rebuilding the fact list.
        facts = to_member_facts(
            list(observations),
            (evidence_by_date or {}).get(d, {}),
        )
        index_by_instrument: dict[str, int] = {
            fact.instrument_id: i for i, fact in enumerate(facts)
        }

        payload: list[dict[str, Any]] = []
        keys: list[str] = []
        for scope_key, members in sorted(scopes_at_d.items()):
            indices = [
                index_by_instrument[str(m)]
                for m in members
                if str(m) in index_by_instrument
            ]
            if not indices:
                continue
            payload.append(
                {
                    "scope_id": scope_key,
                    "scope_family": family,
                    "member_indices": indices,
                }
            )
            keys.append(scope_key)

        if not payload:
            result[d] = {}
            continue

        computed = compute_auction_l1_scope_facts(facts, payload, config)
        result[d] = {keys[i]: computed[i] for i in range(len(keys))}
    return result


def build_scope_history_series(
    *,
    trade_dates: Sequence[date],
    observations_by_date: dict[date, Sequence[AuctionMemberObservation]],
    edges: Sequence[MembershipEdge],
    config: AuctionMemberFactConfig,
    evidence_by_date: dict[date, dict[UUID, MemberHistoryEvidence]] | None = None,
) -> ScopeHistorySeries:
    """Build the industry + concept history series in one coordinated pass.

    Args:
        trade_dates: ordered target dates (typically T-120 ... T).
        observations_by_date: already-loaded member observations per date
            (the caller performs ONE bulk read; this function never queries).
        edges: full PIT membership edges (loaded once by the caller).
        config: thresholds are explicit caller inputs (no hidden constants).
        evidence_by_date: optional per-date history evidence; only required for
            dates whose abnormal breadth is read (normally just T).
    """
    dates = sorted(set(trade_dates))

    # ONE pass for membership of every date and both families.
    industry_membership = resolve_scope_members_bulk(
        edges, dates, family=FAMILY_INDUSTRY
    )
    concept_membership = resolve_scope_members_bulk(
        edges, dates, family=FAMILY_CONCEPT
    )

    return ScopeHistorySeries(
        industry=_build_one_family(
            family=FAMILY_INDUSTRY,
            trade_dates=dates,
            observations_by_date=observations_by_date,
            membership_by_date=industry_membership,
            evidence_by_date=evidence_by_date,
            config=config,
        ),
        concept=_build_one_family(
            family=FAMILY_CONCEPT,
            trade_dates=dates,
            observations_by_date=observations_by_date,
            membership_by_date=concept_membership,
            evidence_by_date=evidence_by_date,
            config=config,
        ),
    )
