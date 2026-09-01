"""Auction V3.2 production preparation owner — PURE orchestration.

This is the single place where the V3.2 fact chain is assembled.  Both the
production writer and the T3 business-chain test call THIS owner; neither may
re-wire the domain helpers by hand.

Naming: the module lives in ``domain/auction/`` (not ``services/``) because it
is pure — it only arranges already-loaded canonical inputs and delegates every
computation to its owner.

Strictly out of scope here: no DB reads, no session, no commit, no publication,
no provider calls.

Diagnostics carries machine counters so tests can prove, for example, that
member history is computed once per instrument and does not grow with concept
overlap.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from app.domain.auction.contribution import compute_contributions, reconcile
from app.domain.auction.coverage import compute_scan_coverage
from app.domain.auction.cross_sectional import compute_cross_sectional
from app.domain.auction.leadership import compute_leadership
from app.domain.auction.member_fact import AuctionMemberFactConfig
from app.domain.auction.member_fact_adapter import to_member_facts
from app.domain.auction.member_history import (
    MemberHistoryEvidence,
    compute_member_history_evidence,
    filter_strictly_pre_t,
)
from app.domain.auction.member_observation import AuctionMemberObservation
from app.domain.auction.membership_pit import (
    FAMILY_CONCEPT,
    FAMILY_INDUSTRY,
    MembershipEdge,
    resolve_scope_members,
)
from app.domain.auction.scope_dynamics import (
    compute_amount_participation,
    compute_dynamics,
)
from app.domain.auction.scope_fact import compute_auction_l1_scope_facts
from app.domain.auction.scope_history import build_scope_history_series
from app.domain.auction.scope_payload import build_scope_payload
from app.domain.auction.version import V32_ALGORITHM_VERSION

__all__ = [
    "FAMILIES",
    "V32PreparedScope",
    "V32PreparationResult",
    "MAX_HISTORY_SLOTS",
    "PREVIOUS_COMPUTED_EMPTY",
    "PREVIOUS_NONEMPTY",
    "PREVIOUS_UNAVAILABLE",
    "SLOT_STATUS_INSUFFICIENT_HISTORY",
    "SLOT_STATUS_OK",
    "SlotContract",
    "build_previous_leader_sets",
    "canonicalize_trade_slots",
    "prepare_v32_analysis",
]

FAMILIES = (FAMILY_INDUSTRY, FAMILY_CONCEPT)

#: Bounded history window.  Fewer pre-T slots is allowed ("insufficient
#: history"); filling up to this number by reaching back further is NOT.
MAX_HISTORY_SLOTS = 120

SLOT_STATUS_OK = "ok"
SLOT_STATUS_INSUFFICIENT_HISTORY = "insufficient_history"

#: previous-leader three-state contract (never collapse with ``or ()``)
PREVIOUS_UNAVAILABLE = "previous_unavailable"
PREVIOUS_COMPUTED_EMPTY = "previous_computed_empty"
PREVIOUS_NONEMPTY = "previous_nonempty"


@dataclass(frozen=True)
class SlotContract:
    """Canonical, fail-closed observation slots for one V3.2 run."""

    trade_dates: tuple[date, ...]
    pre_t_count: int
    status: str


def canonicalize_trade_slots(
    trade_dates: Sequence[date], trade_date: date
) -> SlotContract:
    """Validate and canonicalise the observation slots (D-120 ... T).

    Fail-closed rules:
      * T must be present;
      * no slot may be later than T (a future slot is never silently dropped);
      * duplicates and unordered input are canonicalised deterministically;
      * at most ``MAX_HISTORY_SLOTS`` pre-T slots, keeping the ones CLOSEST to
        T.  Fewer is fine and reported as ``insufficient_history`` — the caller
        must never reach back further just to top the window up.
    """
    unique = sorted(set(trade_dates))
    if not unique:
        raise ValueError("trade_dates is empty")

    future = [d for d in unique if d > trade_date]
    if future:
        raise ValueError(
            f"trade_dates contains slots after T={trade_date.isoformat()}: "
            f"{[d.isoformat() for d in future[:5]]}"
        )
    if trade_date not in unique:
        raise ValueError(
            f"trade_dates must contain T={trade_date.isoformat()}"
        )

    pre_t = [d for d in unique if d < trade_date]
    if len(pre_t) > MAX_HISTORY_SLOTS:
        pre_t = pre_t[-MAX_HISTORY_SLOTS:]

    status = SLOT_STATUS_OK if len(pre_t) >= MAX_HISTORY_SLOTS else SLOT_STATUS_INSUFFICIENT_HISTORY
    return SlotContract(
        trade_dates=tuple(pre_t) + (trade_date,),
        pre_t_count=len(pre_t),
        status=status,
    )


@dataclass(frozen=True)
class V32PreparedScope:
    """One fully-computed V3.2 scope, ready for persistence preparation."""

    family: str
    scope_key: str
    scope_name: str
    payload: dict[str, Any]
    reconciliation: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class V32PreparationResult:
    """Everything the writer needs, and nothing it must recompute."""

    trade_date: date
    coverage: Any
    scopes: tuple[V32PreparedScope, ...]
    diagnostics: dict[str, Any]


def _scope_names(edges: Sequence[MembershipEdge]) -> dict[str, str]:
    """scope_key -> display name, taken from the PIT edges themselves."""
    names: dict[str, str] = {}
    for edge in edges:
        if edge.scope_key not in names and getattr(edge, "scope_name", None):
            names[edge.scope_key] = edge.scope_name
    return names


def _family_table(series: Any, family: str) -> dict[date, dict[str, Any]]:
    return series.industry if family == FAMILY_INDUSTRY else series.concept


def _l1_by_scope(
    *,
    family: str,
    facts: list[Any],
    edges: Sequence[MembershipEdge],
    trade_date: date,
    config: AuctionMemberFactConfig,
    index_by_instrument: dict[str, int],
) -> dict[str, Any]:
    """Run the single L1 calculator for one family; keyed by scope_key."""
    membership = resolve_scope_members(edges, trade_date, family=family)
    scopes = [
        {
            "scope_id": key,
            "scope_family": family,
            "member_indices": [
                index_by_instrument[str(m)]
                for m in members
                if str(m) in index_by_instrument
            ],
        }
        for key, members in membership.items()
        if any(str(m) in index_by_instrument for m in members)
    ]
    if not scopes:
        return {}
    return {f.scope_id: f for f in compute_auction_l1_scope_facts(facts, scopes, config)}


def build_previous_leader_sets(
    *,
    previous_trade_date: date,
    observations_by_date: Mapping[date, Sequence[AuctionMemberObservation]],
    edges: Sequence[MembershipEdge],
    config: AuctionMemberFactConfig,
    evidence_by_date: Mapping[date, Mapping[UUID, MemberHistoryEvidence]] | None = None,
) -> dict[str, dict[str, frozenset[UUID]]]:
    """Leader sets for the PREVIOUS auction trading day, per family and scope.

    The previous auction day is an explicit input: it is a trading-calendar
    question, not a calendar-day subtraction, so the caller resolves it.
    """
    observations = list(observations_by_date.get(previous_trade_date, ()))
    if not observations:
        return {}

    evidence = (evidence_by_date or {}).get(previous_trade_date, {})
    facts = to_member_facts(observations, evidence)
    # AuctionMemberFact.instrument_id is a str while PIT edges carry UUID: normalise
    index_by_instrument = {str(f.instrument_id): i for i, f in enumerate(facts)}

    out: dict[str, dict[str, frozenset[UUID]]] = {}
    for family in FAMILIES:
        l1 = _l1_by_scope(
            family=family,
            facts=facts,
            edges=edges,
            trade_date=previous_trade_date,
            config=config,
            index_by_instrument=index_by_instrument,
        )
        leaders: dict[str, frozenset[UUID]] = {}
        membership = resolve_scope_members(edges, previous_trade_date, family=family)
        for key, fact in l1.items():
            # SAME adapter as today: only the previous PIT scope members may
            # contribute, otherwise an outsider could become a past leader.
            scope_member_ids = {str(m) for m in membership.get(key, ())}
            scope_observations = [
                o for o in observations if str(o.instrument_id) in scope_member_ids
            ]
            contributions = compute_contributions(
                trade_date=previous_trade_date,
                members=scope_observations,
                ew_gap=fact.equal_weight_gap,
                aw_gap=fact.amount_weighted_gap,
                scope_total_amount=fact.total_auction_amount,
            )
            leadership = compute_leadership(
                contributions=contributions.members,
                ew_gap=fact.equal_weight_gap,
            )
            leaders[key] = frozenset(leadership.leaders)
        out[family] = leaders
    return out


def prepare_v32_analysis(
    *,
    trade_date: date,
    trade_dates: Sequence[date],
    observations_by_date: Mapping[date, Sequence[AuctionMemberObservation]],
    edges: Sequence[MembershipEdge],
    config: AuctionMemberFactConfig,
    evidence_by_date: Mapping[date, Mapping[UUID, MemberHistoryEvidence]] | None = None,
    previous_leader_sets: Mapping[str, Mapping[str, frozenset[UUID]]] | None = None,
    algorithm_version: str = V32_ALGORITHM_VERSION,
) -> V32PreparationResult:
    """Assemble the complete V3.2 fact set for one trading day.

    ``trade_dates`` must be the ordered observation slots ``D-120 ... T``.
    Missing days keep their slot with unavailable values; this function never
    reaches back further to fill a gap.

    Args:
        trade_date: T.
        trade_dates: ordered slots, ending at T.
        observations_by_date: bulk-loaded observations per slot.
        edges: PIT membership edges covering the whole window.
        config: thresholds are explicit caller inputs.
        evidence_by_date: history evidence per date; when T is absent it is
            derived here ONCE per instrument (never per scope).
        previous_leader_sets: ``family -> scope_key -> leaders`` for the
            previous auction day.
        algorithm_version: fixed to the V3.2 owner; callers may not nominate
            another version.
    """
    if algorithm_version != V32_ALGORITHM_VERSION:
        raise ValueError(
            f"prepare_v32_analysis produces {V32_ALGORITHM_VERSION} only; "
            f"got {algorithm_version!r}"
        )

    # Time contract is enforced HERE, not left to the loader's good behaviour.
    slot = canonicalize_trade_slots(trade_dates, trade_date)
    trade_dates = slot.trade_dates

    current = list(observations_by_date.get(trade_date, ()))
    coverage = compute_scan_coverage(current)

    # --- member history evidence: once per instrument, never per scope -------
    member_history_computations = 0
    evidence_by_date = dict(evidence_by_date or {})
    if trade_date not in evidence_by_date:
        histories: dict[UUID, list[AuctionMemberObservation]] = {}
        for d in trade_dates:
            if d >= trade_date:
                continue  # baseline is strictly pre-T
            for obs in observations_by_date.get(d, ()):
                histories.setdefault(obs.instrument_id, []).append(obs)
        per_instrument: dict[UUID, MemberHistoryEvidence] = {}
        for obs in current:
            kept, _dropped = filter_strictly_pre_t(histories.get(obs.instrument_id, []), trade_date)
            per_instrument[obs.instrument_id] = compute_member_history_evidence(
                instrument_id=obs.instrument_id,
                trade_date=trade_date,
                current=obs,
                history=kept,
            )
            member_history_computations += 1
        evidence_by_date[trade_date] = per_instrument

    facts = to_member_facts(current, evidence_by_date.get(trade_date, {}))
    index_by_instrument = {f.instrument_id: i for i, f in enumerate(facts)}

    # --- scope history series through the single owner -----------------------
    series = build_scope_history_series(
        trade_dates=trade_dates,
        observations_by_date=dict(observations_by_date),
        edges=edges,
        config=config,
        evidence_by_date=evidence_by_date,
    )

    names = _scope_names(edges)
    previous_leader_sets = previous_leader_sets or {}

    # --- L1 per family, then dynamics / amount participation -----------------
    l1: dict[str, dict[str, Any]] = {}
    for family in FAMILIES:
        l1[family] = _l1_by_scope(
            family=family,
            facts=facts,
            edges=edges,
            trade_date=trade_date,
            config=config,
            index_by_instrument=index_by_instrument,
        )

    membership_by_family: dict[str, dict[str, Sequence[UUID]]] = {
        f: resolve_scope_members(edges, trade_date, family=f) for f in FAMILIES
    }
    cross_rows: dict[str, list[dict[str, Any]]] = {f: [] for f in FAMILIES}
    preliminary: list[tuple[str, Any, Any, Any]] = []  # family, fact, dynamics, participation

    for family in FAMILIES:
        table = _family_table(series, family)
        ordered_dates = sorted(table)
        for key, fact in l1[family].items():
            dynamics = compute_dynamics(series.ew_gap_series(family, key))

            amounts: list[tuple[date, float | None]] = []
            for d in ordered_dates:
                day_fact = table[d].get(key)
                amounts.append((d, day_fact.total_auction_amount if day_fact else None))
            participation = compute_amount_participation(amounts)

            preliminary.append((family, fact, dynamics, participation))
            cross_rows[family].append(
                {
                    "scope_key": key,
                    "equal_weight_gap": fact.equal_weight_gap,
                    "amount_weighted_gap": fact.amount_weighted_gap,
                    "capital_tilt": fact.capital_tilt,
                    "positive_gap_breadth": fact.positive_gap_breadth,
                    "negative_gap_breadth": fact.negative_gap_breadth,
                    "unchanged_gap_breadth": fact.unchanged_gap_breadth,
                    "gap_dispersion": fact.gap_dispersion,
                    "amount_historical_position": participation.get("amount_position"),
                    "amount_multiple": participation.get("amount_multiple"),
                    "amount_abnormal_breadth": fact.amount_abnormal_breadth,
                    "price_normalized_hhi": fact.price_normalized_hhi,
                    "normalized_hhi": fact.normalized_hhi,
                    "top3_amount_share": fact.top3_amount_share,
                }
            )

    # --- cross-section: one owner call per family, cohorts never merged ------
    cross_by_family = {
        family: compute_cross_sectional(rows)
        for family, rows in cross_rows.items()
        if rows
    }

    # --- contribution + leadership, then finalise payloads -------------------
    prepared: list[V32PreparedScope] = []
    for family, fact, dynamics, participation in preliminary:
        key = fact.scope_id
        latest = dynamics.latest()

        # contributions are scoped: only THIS scope's members may appear,
        # otherwise the sums cannot reconcile against the scope's own gaps.
        scope_member_ids = {str(m) for m in membership_by_family[family].get(key, ())}
        scope_observations = [
            o for o in current if str(o.instrument_id) in scope_member_ids
        ]
        contributions = compute_contributions(
            trade_date=trade_date,
            members=scope_observations,
            ew_gap=fact.equal_weight_gap,
            aw_gap=fact.amount_weighted_gap,
            scope_total_amount=fact.total_auction_amount,
        )
        checks = reconcile(
            contributions,
            ew_gap=fact.equal_weight_gap,
            aw_gap=fact.amount_weighted_gap,
        )
        prev_lookup = previous_leader_sets.get(family) or {}
        if key in prev_lookup:
            prev_for_scope = prev_lookup[key]
            previous_status = (
                PREVIOUS_NONEMPTY if prev_for_scope else PREVIOUS_COMPUTED_EMPTY
            )
        else:
            prev_for_scope = None
            previous_status = PREVIOUS_UNAVAILABLE
        leadership = compute_leadership(
            contributions=contributions.members,
            ew_gap=fact.equal_weight_gap,
            previous_leaders=prev_for_scope,
        )
        cross = (cross_by_family.get(family) or {}).get(key)

        payload = build_scope_payload(
            algorithm_version=algorithm_version,
            identity={"scope_key": key, "scope_name": names.get(key, key)},
            repricing={
                "equal_weight_gap": fact.equal_weight_gap,
                "amount_weighted_gap": fact.amount_weighted_gap,
                "capital_tilt": fact.capital_tilt,
                "positive_gap_breadth": fact.positive_gap_breadth,
                "negative_gap_breadth": fact.negative_gap_breadth,
                "unchanged_gap_breadth": fact.unchanged_gap_breadth,
                "gap_dispersion": fact.gap_dispersion,
                "price_normalized_hhi": fact.price_normalized_hhi,
                "price_valid_count": fact.equal_weight_gap_den,
            },
            historical_dynamics={
                "position": latest.position if latest else None,
                "ema_fast": latest.ema_fast if latest else None,
                "ema_slow": latest.ema_slow if latest else None,
                "velocity": latest.velocity if latest else None,
                "signal": latest.signal if latest else None,
                "acceleration": latest.acceleration if latest else None,
                "latest_trade_date": (
                    latest.trade_date.isoformat() if latest else None
                ),
            },
            participation={
                "total_auction_amount": fact.total_auction_amount,
                "amount_position": participation.get("amount_position"),
                "amount_multiple": participation.get("amount_multiple"),
                "amount_abnormal_breadth": fact.amount_abnormal_breadth,
                "top1_amount_share": fact.top1_amount_share,
                "top3_amount_share": fact.top3_amount_share,
                "amount_normalized_hhi": fact.normalized_hhi,
            },
            cross_sectional={
                "repricing": dict(cross.repricing) if cross else {},
                "breadth": dict(cross.breadth) if cross else {},
                "participation": dict(cross.participation) if cross else {},
                "concentration": dict(cross.concentration) if cross else {},
            },
            member_attribution={
                "members": [
                    {
                        "instrument_id": str(c.instrument_id),
                        "gap_ratio": c.gap_ratio,
                        "ew_contribution": c.ew_contribution,
                        "auction_amount": c.auction_amount,
                        "amount_share": c.amount_share,
                        "aw_contribution": c.aw_contribution,
                    }
                    for c in contributions.members
                ],
                "leaders": [str(x) for x in leadership.leaders],
            "leadership_migration": leadership.migration,
                "retained": (
                    None
                    if leadership.retained is None
                    else [str(x) for x in leadership.retained]
                ),
                "entrants": (
                    None
                    if leadership.entrants is None
                    else [str(x) for x in leadership.entrants]
                ),
                "exits": (
                    None
                    if leadership.exits is None
                    else [str(x) for x in leadership.exits]
                ),
                "jaccard": leadership.jaccard,
            },
            diagnostics={
                "family": family,
                "amount_position_status": participation.get("amount_position_status"),
                "history_valid_count": participation.get("history_valid_count"),
                "previous_leader_status": previous_status,
                "previous_leader_count": (
                    None if prev_for_scope is None else len(prev_for_scope)
                ),
            },
        )
        prepared.append(
            V32PreparedScope(
                family=family,
                scope_key=key,
                scope_name=names.get(key, key),
                payload=payload,
                reconciliation=dict(checks),
            )
        )

    diagnostics = {
        "trade_date": trade_date.isoformat(),
        "member_history_computations": member_history_computations,
        "unique_instruments": len({f.instrument_id for f in facts}),
        "scope_count": len(prepared),
        "industry_scope_count": sum(1 for s in prepared if s.family == FAMILY_INDUSTRY),
        "concept_scope_count": sum(1 for s in prepared if s.family == FAMILY_CONCEPT),
        "algorithm_version": algorithm_version,
        "slot_status": slot.status,
        "pre_t_slot_count": slot.pre_t_count,
        "max_history_slots": MAX_HISTORY_SLOTS,
        "coverage_eligible_count": coverage.eligible_count,
        "coverage_valid_count": coverage.valid_count,
        "coverage_ratio": coverage.coverage_ratio,
    }

    return V32PreparationResult(
        trade_date=trade_date,
        coverage=coverage,
        scopes=tuple(prepared),
        diagnostics=diagnostics,
    )
