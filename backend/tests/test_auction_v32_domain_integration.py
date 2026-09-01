"""Auction V3.2 domain-owner integration regression (LOWER LEVEL, not T3).

This file is deliberately NOT the T3 business-chain closure.  It wires the
domain owners directly, by hand, to catch regressions in each owner in
isolation.  The T3 closure lives in
``test_auction_v32_production_chain.py`` and calls the single production
preparation owner instead, so it cannot drift from what production actually
runs.

Kept because the hand-wired path still exercises owner combinations that the
preparation owner collapses into one call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.domain.auction.contribution import compute_contributions, reconcile
from app.domain.auction.coverage import compute_scan_coverage
from app.domain.auction.cross_sectional import compute_cross_sectional
from app.domain.auction.leadership import compute_leadership
from app.domain.auction.member_fact import AuctionMemberFactConfig
from app.domain.auction.member_fact_adapter import to_member_facts
from app.domain.auction.member_history import (
    compute_member_history_evidence,
    filter_strictly_pre_t,
)
from app.domain.auction.member_observation import build_member_observation
from app.domain.auction.membership_pit import (
    FAMILY_CONCEPT,
    FAMILY_INDUSTRY,
    MembershipEdge,
    resolve_scope_members,
)
from app.domain.auction.publication_read import (
    find_scope_result_by_key,
    read_published_scope_results,
    to_scope_detail,
    to_scope_list_items,
)
from app.domain.auction.scope_dynamics import compute_dynamics
from app.domain.auction.scope_fact import compute_auction_l1_scope_facts
from app.domain.auction.scope_payload import (
    build_scope_payload,
    canonical_scope_key,
)
from app.domain.auction.version import V32_ALGORITHM_VERSION
from app.services.auction_publication_service import (
    evaluate_auction_publication_gate,
)
from app.services.auction_scope_persistence_service import (
    build_scan_run_kwargs,
    build_scope_result_kwargs,
)

_T = date(2026, 8, 14)
_HISTORY_DAYS = 120

_CFG = AuctionMemberFactConfig(
    positive_gap_percentile_threshold=90.0,
    negative_gap_percentile_threshold=10.0,
    volume_abnormal_percentile_threshold=90.0,
    amount_abnormal_percentile_threshold=90.0,
)

_SCOPE_KEY = "IND_BANK"
_SCOPE_NAME = "银行"


@dataclass
class FakeScopeResultRow:
    scan_run_id: UUID
    trade_date: date
    scope_type: str
    scope_id: UUID | None
    scope_name: str | None
    payload: dict[str, Any]


def _quote(instrument_id: UUID, trade_date: date, price: float | None, prev_close: float, amount: float | None):
    """Stand-in for an AuctionFinalQuote row."""
    return {
        "instrument_id": instrument_id,
        "trade_date": trade_date,
        "final_price": price,
        "prev_close": prev_close,
        "amount": amount,
        "quality_status": "ok",
        "source": "mootdx" if trade_date == _T else "historical_backfill",
    }


def _observation(quote: dict[str, Any]):
    return build_member_observation(
        instrument_id=quote["instrument_id"],
        trade_date=quote["trade_date"],
        final_price=quote["final_price"],
        prev_close=quote["prev_close"],
        amount=quote["amount"],
        quality_status=quote["quality_status"],
        source=quote["source"],
    )


def _history_for(instrument_id: UUID) -> list[dict[str, Any]]:
    """HISTORY_DAYS rows strictly before T."""
    return [
        _quote(
            instrument_id,
            _T - timedelta(days=i + 1),
            1.0 + 0.01 * ((i % 7) - 3) / 10.0,
            1.0,
            1000.0 + i,
        )
        for i in range(_HISTORY_DAYS)
    ]


# ---------------------------------------------------------------------------
# Pre-T / future-leakage evidence
# ---------------------------------------------------------------------------
def test_history_is_strictly_pre_t() -> None:
    instrument = uuid4()
    history = _history_for(instrument)
    kept, dropped = filter_strictly_pre_t(
        [_observation(q) for q in history] + [_observation(_quote(instrument, _T, 1.0, 1.0, 1.0))],
        _T,
    )
    assert dropped == 1  # T itself is never part of its own baseline
    assert all(o.trade_date < _T for o in kept)


def test_member_history_baseline_excludes_t_and_future() -> None:
    instrument = uuid4()
    history = [_observation(q) for q in _history_for(instrument)]
    current = _observation(_quote(instrument, _T, 1.02, 1.0, 5000.0))
    polluted = history + [
        _observation(_quote(instrument, _T, 999.0, 1.0, 9e9)),
        _observation(_quote(instrument, _T + timedelta(days=1), 999.0, 1.0, 9e9)),
    ]
    evidence = compute_member_history_evidence(
        instrument_id=instrument,
        trade_date=_T,
        current=current,
        history=polluted,
    )
    assert evidence.dropped_future_or_same_day == 2
    assert evidence.gap_candidate_count == _HISTORY_DAYS


# ---------------------------------------------------------------------------
# The continuous chain
# ---------------------------------------------------------------------------
def _build_chain() -> dict[str, Any]:
    members = [uuid4() for _ in range(5)]
    gaps = [0.031, -0.012, 0.000, 0.008, 0.021]
    amounts = [4_000_000.0, 1_500_000.0, 800_000.0, 2_200_000.0, 3_000_000.0]

    current = [
        _observation(_quote(m, _T, 1.0 + g, 1.0, a))
        for m, g, a in zip(members, gaps, amounts, strict=False)
    ]

    # member history evidence — computed ONCE per instrument, reused by scopes
    evidence = {
        m: compute_member_history_evidence(
            instrument_id=m,
            trade_date=_T,
            current=obs,
            history=[_observation(q) for q in _history_for(m)],
        )
        for m, obs in zip(members, current, strict=False)
    }

    facts = to_member_facts(current, evidence)

    scopes = [
        {"scope_id": "s0", "scope_family": FAMILY_INDUSTRY, "member_indices": []},
        {"scope_id": _SCOPE_KEY, "scope_family": FAMILY_INDUSTRY, "member_indices": [0, 1, 2, 3, 4]},
    ]
    l1 = compute_auction_l1_scope_facts(facts, scopes, _CFG)
    scope_fact = l1[1]

    # historical dynamics over the SAME scope metric series
    dates = [_T - timedelta(days=i) for i in range(_HISTORY_DAYS, 0, -1)]
    series = [(d, 0.001 * (i % 11) - 0.005) for i, d in enumerate(dates)]
    dynamics = compute_dynamics(series)

    cross = compute_cross_sectional(
        [
            {"scope_key": _SCOPE_KEY, "equal_weight_gap": scope_fact.equal_weight_gap,
             "positive_gap_breadth": scope_fact.positive_gap_breadth,
             "amount_historical_position": 60.0, "amount_multiple": 1.2,
             "amount_abnormal_breadth": 0.2, "price_normalized_hhi": 0.3,
             "normalized_hhi": 0.3, "top3_amount_share": 0.6},
        ]
    )[_SCOPE_KEY]

    contributions = compute_contributions(
        trade_date=_T,
        members=current,
        ew_gap=scope_fact.equal_weight_gap,
        aw_gap=scope_fact.amount_weighted_gap,
        scope_total_amount=scope_fact.total_auction_amount,
    )
    leadership = compute_leadership(
        contributions=contributions.members,
        ew_gap=scope_fact.equal_weight_gap,
    )

    payload = build_scope_payload(
        algorithm_version=V32_ALGORITHM_VERSION,
        identity={"scope_key": _SCOPE_KEY, "scope_name": _SCOPE_NAME},
        repricing={
            "equal_weight_gap": scope_fact.equal_weight_gap,
            "amount_weighted_gap": scope_fact.amount_weighted_gap,
            "capital_tilt": scope_fact.capital_tilt,
            "positive_gap_breadth": scope_fact.positive_gap_breadth,
            "negative_gap_breadth": scope_fact.negative_gap_breadth,
            "unchanged_gap_breadth": scope_fact.unchanged_gap_breadth,
            "gap_dispersion": scope_fact.gap_dispersion,
            "price_normalized_hhi": scope_fact.price_normalized_hhi,
            "price_valid_count": scope_fact.equal_weight_gap_den,
        },
        historical_dynamics={
            "position": dynamics.latest().position if dynamics.latest() else None,
            "ema_fast": dynamics.latest().ema_fast if dynamics.latest() else None,
            "ema_slow": dynamics.latest().ema_slow if dynamics.latest() else None,
            "velocity": dynamics.latest().velocity if dynamics.latest() else None,
            "signal": dynamics.latest().signal if dynamics.latest() else None,
            "acceleration": dynamics.latest().acceleration if dynamics.latest() else None,
        },
        participation={
            "total_auction_amount": scope_fact.total_auction_amount,
            "amount_position": None,
            "amount_multiple": None,
            "amount_abnormal_breadth": None,
            "top1_amount_share": scope_fact.top1_amount_share,
            "top3_amount_share": scope_fact.top3_amount_share,
            "amount_normalized_hhi": scope_fact.normalized_hhi,
        },
        cross_sectional={
            "repricing": dict(cross.repricing),
            "breadth": dict(cross.breadth),
            "participation": dict(cross.participation),
            "concentration": dict(cross.concentration),
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
            "leadership_migration": leadership.migration,
            "retained": [str(x) for x in leadership.retained],
            "entrants": [str(x) for x in leadership.entrants],
            "exits": [str(x) for x in leadership.exits],
            "jaccard": leadership.jaccard,
        },
    )

    return {
        "members": members,
        "current": current,
        "facts": facts,
        "scope_fact": scope_fact,
        "contributions": contributions,
        "leadership": leadership,
        "payload": payload,
    }


def test_chain_runs_end_to_end() -> None:
    chain = _build_chain()
    assert chain["scope_fact"].equal_weight_gap_den == 5
    assert canonical_scope_key(chain["payload"]) == _SCOPE_KEY


def test_ew_contribution_sum_equals_ew_gap() -> None:
    chain = _build_chain()
    scope_fact = chain["scope_fact"]
    checks = reconcile(
        chain["contributions"],
        ew_gap=scope_fact.equal_weight_gap,
        aw_gap=scope_fact.amount_weighted_gap,
    )
    assert checks["ew_sum_matches_ew_gap"] is True


def test_aw_contribution_sum_equals_aw_gap() -> None:
    chain = _build_chain()
    scope_fact = chain["scope_fact"]
    checks = reconcile(
        chain["contributions"],
        ew_gap=scope_fact.equal_weight_gap,
        aw_gap=scope_fact.amount_weighted_gap,
    )
    assert checks["aw_sum_matches_aw_gap"] is True


def test_amount_share_sum_is_one() -> None:
    chain = _build_chain()
    scope_fact = chain["scope_fact"]
    checks = reconcile(
        chain["contributions"],
        ew_gap=scope_fact.equal_weight_gap,
        aw_gap=scope_fact.amount_weighted_gap,
    )
    assert checks["amount_share_sum_is_one"] is True


def test_scope_key_survives_persistence_and_read() -> None:
    """The canonical key must be identical before and after the round trip."""
    chain = _build_chain()
    run_id = uuid4()
    row = build_scope_result_kwargs(
        scan_run_id=run_id,
        trade_date=_T,
        scope_type=FAMILY_INDUSTRY,
        scope_id=uuid4(),
        scope_name=_SCOPE_NAME,
        payload=chain["payload"],
    )
    materialised = FakeScopeResultRow(
        scan_run_id=run_id,
        trade_date=_T,
        scope_type=FAMILY_INDUSTRY,
        scope_id=uuid4(),
        scope_name=_SCOPE_NAME,
        payload=row["payload"],
    )
    assert canonical_scope_key(materialised.payload) == _SCOPE_KEY

    found = find_scope_result_by_key([materialised], _SCOPE_KEY)
    assert found is not None
    # display name must not resolve
    assert find_scope_result_by_key([materialised], _SCOPE_NAME) is None


# ---------------------------------------------------------------------------
# visibility: publication gate + pointer
# ---------------------------------------------------------------------------
def _publication(trade_date: date, run_id: UUID, algorithm: str = V32_ALGORITHM_VERSION):
    from datetime import datetime

    @dataclass
    class _Pub:
        trade_date: date
        algorithm_version: str
        scan_run_id: UUID
        published_at: Any

    return _Pub(trade_date, algorithm, run_id, datetime(2026, 8, 14, 9, 40))


def test_failed_gate_means_no_visibility() -> None:
    """A run that fails the production gate must never be readable."""
    reasons = evaluate_auction_publication_gate(
        truth_status="verified",
        test_namespace="production",
        scan_status="succeeded",
        scan_coverage=0.98,
        capture_source="mootdx",  # raw single-family source
        capture_status="succeeded",
        scope_count=5,
    )
    assert reasons  # gate rejects -> no publication row -> invisible


def test_unpublished_newer_run_stays_invisible_in_the_chain() -> None:
    chain = _build_chain()
    run_a, run_b = uuid4(), uuid4()

    def _mk(run_id: UUID, name: str) -> FakeScopeResultRow:
        return FakeScopeResultRow(
            scan_run_id=run_id,
            trade_date=_T,
            scope_type=FAMILY_INDUSTRY,
            scope_id=uuid4(),
            scope_name=name,
            payload=chain["payload"],
        )

    publications = [_publication(_T, run_a)]
    results = [_mk(run_a, "已发布"), _mk(run_b, "未发布更新")]

    visible = read_published_scope_results(
        publications, results, trade_date=_T, family=FAMILY_INDUSTRY
    )
    assert len(visible) == 1
    assert visible[0].scope_name == "已发布"


def test_read_model_returns_complete_family() -> None:
    chain = _build_chain()
    run_id = uuid4()
    publications = [_publication(_T, run_id)]
    results = [
        FakeScopeResultRow(
            scan_run_id=run_id,
            trade_date=_T,
            scope_type=FAMILY_INDUSTRY,
            scope_id=uuid4(),
            scope_name=f"IND_{i:02d}",
            payload=chain["payload"],
        )
        for i in range(25)
    ]
    items = to_scope_list_items(
        read_published_scope_results(
            publications, results, trade_date=_T, family=FAMILY_INDUSTRY
        )
    )
    assert len(items) == 25  # complete family snapshot, no Top-N


def test_detail_exposes_five_groups_after_the_chain() -> None:
    chain = _build_chain()
    run_id = uuid4()
    row = FakeScopeResultRow(
        scan_run_id=run_id,
        trade_date=_T,
        scope_type=FAMILY_INDUSTRY,
        scope_id=uuid4(),
        scope_name=_SCOPE_NAME,
        payload=chain["payload"],
    )
    detail = to_scope_detail(row)
    for group in (
        "repricing",
        "historical_dynamics",
        "participation",
        "cross_sectional",
        "member_attribution",
        "diagnostics",
    ):
        assert group in detail
    assert detail["member_attribution"]["leadership_migration"] is not None


# ---------------------------------------------------------------------------
# KPI-5 legacy isolation: inputs come only from quote rows + PIT membership
# ---------------------------------------------------------------------------
def test_chain_inputs_are_quote_rows_and_pit_membership_only() -> None:
    chain = _build_chain()
    members = chain["members"]
    edges = [
        MembershipEdge(m, _SCOPE_KEY, _SCOPE_NAME, FAMILY_INDUSTRY, _T - timedelta(days=400), None)
        for m in members
    ]
    resolved = resolve_scope_members(edges, _T, family=FAMILY_INDUSTRY)
    assert set(resolved[_SCOPE_KEY]) == set(members)

    # the concept family is a separate cohort and must not appear here
    concept_edges = [
        MembershipEdge(m, "CPT_ROBOT", "机器人", FAMILY_CONCEPT, _T - timedelta(days=400), None)
        for m in members
    ]
    assert FAMILY_CONCEPT not in resolve_scope_members(edges, _T, family=FAMILY_CONCEPT)
    assert set(resolve_scope_members(concept_edges, _T, family=FAMILY_CONCEPT)["CPT_ROBOT"]) == set(members)


def test_scan_coverage_is_produced_from_observations_not_history() -> None:
    chain = _build_chain()
    coverage = compute_scan_coverage(chain["current"])
    assert coverage.eligible_count == 5
    assert coverage.valid_count == 5
    assert coverage.coverage_ratio == pytest.approx(1.0)

    kwargs = build_scan_run_kwargs(trade_date=_T, coverage=coverage)
    assert kwargs["coverage_ratio"] == coverage.coverage_ratio
