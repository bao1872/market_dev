"""Objective Evidence Engine (Round 2A) — thin service layer.

Responsibilities only (prompt §5):
  - read the canonical L1 fact via ``get_scope_observation_fact``;
  - resolve exact D1/D3/D5 trading dates via the existing canonical calendar
    helper (``calendar_service.get_previous_trading_day_async``);
  - read historical facts (same scope, trade_date < T) and same-family peer
    facts (same trade_date, same scope_type);
  - call the pure evidence calculations in ``scope_evidence`` and return a dict.

It NEVER does Filter / Candidate / Discovery / Score, never writes anything, and
never reads legacy ``market_review_scope_snapshots`` / p/q/u/c/v payloads.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review import scope_evidence
from app.models.market_review import ReviewScopeObservationFact
from app.services import calendar_service
from app.services.review_observation_persistence_service import (
    get_scope_observation_fact,
    list_scope_observation_facts,
)

# Market has no peer cohort (prompt §14): it is excluded from peer context.
MARKET_HAS_NO_PEER = "market_has_no_peer_cohort"

# Same-family peer cohorts are exactly the activated persistence families.  The
# scope_type itself expresses the family cohort; no peer registry is built
# (prompt §14).  Market is excluded because no peer cohort is defined for it.
PEER_SCOPE_TYPES: frozenset[str] = frozenset(
    {"concept", "industry_l1", "industry_l2", "industry_l3"}
)


async def _nth_previous_trading_day(
    session: AsyncSession,
    ref_date: date,
    n: int,
) -> date | None:
    """Return the exact ``n``-th previous A-share trading day (n>=1).

    Iterates the existing canonical calendar helper.  Returns None if the chain
    cannot resolve (calendar gap).  No calendar-day subtraction, no nearest-fact
    fallback (prompt §9).
    """
    current: date | None = ref_date
    for _ in range(n):
        if current is None:
            return None
        current = await calendar_service.get_previous_trading_day_async(session, current)
    return current


def _scope_metadata(fact: ReviewScopeObservationFact) -> dict[str, Any]:
    return {
        "scope_type": fact.scope_type,
        "scope_key": fact.scope_key,
        "scope_name": fact.scope_name,
        "pit_member_count": fact.pit_member_count,
    }


def _extract_finite(payload: dict[str, Any], primitive: str) -> float | None:
    return scope_evidence.extract_primitive(payload, primitive)


async def compute_scope_evidence(
    session: AsyncSession,
    trade_date: date,
    scope_type: str,
    scope_key: str,
) -> dict[str, Any]:
    """Compute Objective Evidence for one scope/day from canonical L1 facts.

    Returns a dict shaped like prompt §19 (scope / trade_date / primitives) with
    per-context independent status.  Pure query-time derivation; nothing written.
    """
    fact = await get_scope_observation_fact(session, trade_date, scope_type, scope_key)
    if fact is None:
        raise LookupError(
            f"no canonical observation fact for {scope_type}/{scope_key} "
            f"{trade_date.isoformat()}"
        )

    # Exact D1/D3/D5 trading dates via canonical calendar.
    d_dates: dict[str, date | None] = {}
    for name, n in (("d1", 1), ("d3", 3), ("d5", 5)):
        d_dates[name] = await _nth_previous_trading_day(session, trade_date, n)

    # Reference facts at exact D1/D3/D5 dates (missing -> unavailable, no fallback).
    ref_facts: dict[str, tuple[date | None, ReviewScopeObservationFact | None]] = {}
    for name, ref_date in d_dates.items():
        ref_fact = None
        if ref_date is not None:
            ref_fact = await get_scope_observation_fact(
                session, ref_date, scope_type, scope_key
            )
        ref_facts[name] = (ref_date, ref_fact)

    # Historical facts: same scope, trade_date < T (current excluded upstream).
    hist_facts = await list_scope_observation_facts(
        session,
        scope_type=scope_type,
        scope_key=scope_key,
        to_date=trade_date - timedelta(days=1),
    )

    # Same-day same-family peers (current scope included when present).
    peer_facts = await list_scope_observation_facts(
        session, scope_type=scope_type, from_date=trade_date, to_date=trade_date
    )

    primitives: dict[str, Any] = {}
    for primitive in scope_evidence.PRIMITIVE_NAMES:
        primitives[primitive] = _compute_primitive(
            fact,
            ref_facts,
            hist_facts,
            peer_facts,
            scope_type,
            primitive,
        )

    return {
        "scope": _scope_metadata(fact),
        "trade_date": trade_date.isoformat(),
        "primitives": primitives,
    }


def _compute_primitive(
    fact: ReviewScopeObservationFact,
    ref_facts: dict[str, tuple[date | None, ReviewScopeObservationFact | None]],
    hist_facts: list[ReviewScopeObservationFact],
    peer_facts: list[ReviewScopeObservationFact],
    scope_type: str,
    primitive: str,
) -> dict[str, Any]:
    payload = fact.observation_payload
    current = _extract_finite(payload, primitive)

    out: dict[str, Any] = {}
    out["current"] = scope_evidence.build_current_context(current)

    for name, (ref_date, ref_fact) in ref_facts.items():
        ref_value = (
            _extract_finite(ref_fact.observation_payload, primitive)
            if ref_fact is not None
            else None
        )
        out[name] = scope_evidence.build_delta_context(current, ref_value, ref_date)

    # Historical sample: same primitive finite values over past facts.
    sample_values: list[float] = []
    dates: list[date] = []
    for h in hist_facts:
        v = _extract_finite(h.observation_payload, primitive)
        if v is not None:
            sample_values.append(v)
            dates.append(h.trade_date)
    start = min(dates) if dates else None
    end = max(dates) if dates else None
    out["historical"] = scope_evidence.build_historical_context(
        current, sample_values, start, end
    )

    # Peer cohort: same-day same-family finite values (current included).
    if scope_type not in PEER_SCOPE_TYPES:
        out["peer"] = {
            "status": "unavailable",
            "percentile": None,
            "peer_count": 0,
            "reason": MARKET_HAS_NO_PEER,
        }
    elif primitive == "price_raw_hhi":
        peer_values = [
            v
            for p in peer_facts
            if (v := _extract_finite(p.observation_payload, primitive)) is not None
        ]
        out["peer"] = scope_evidence.build_peer_context(
            current,
            peer_values,
            disabled_reason=scope_evidence.RAW_HHI_PEER_DISABLED_REASON,
        )
    else:
        peer_values = [
            v
            for p in peer_facts
            if (v := _extract_finite(p.observation_payload, primitive)) is not None
        ]
        out["peer"] = scope_evidence.build_peer_context(current, peer_values)

    return out
