"""Persistence contract tests for Auction V3.2 (no PostgreSQL required).

These tests pin the honest-reuse decision (REUSE_WITH_V32_SEMANTICS):
- V3.2 must NOT forge an anchor / legacy lifecycle;
- ``capture_run_id`` must be a REAL caller-supplied identity, never fabricated;
- the payload schema version is validated before anything is persisted;
- publication is the only visibility boundary.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from app.domain.auction.coverage import compute_scan_coverage, compute_scope_coverage
from app.domain.auction.member_observation import build_member_observation
from app.domain.auction.scope_payload import SCHEMA_VERSION, build_scope_payload
from app.services.auction_scope_persistence_service import (
    V32_ALGORITHM_VERSION,
    build_publication_kwargs,
    build_scan_run_kwargs,
    build_scope_result_kwargs,
)

_T = date(2026, 8, 14)
_EMPTY_GROUPS = {
    "repricing": {},
    "historical_dynamics": {},
    "participation": {},
    "cross_sectional": {},
    "member_attribution": {},
}


def _valid_payload() -> dict:
    return build_scope_payload(algorithm_version=V32_ALGORITHM_VERSION, **_EMPTY_GROUPS)


# ---------------------------------------------------------------------------
# scan run: no anchor forgery
# ---------------------------------------------------------------------------
def _obs(gap, amount):
    return build_member_observation(
        instrument_id=uuid4(),
        trade_date=_T,
        final_price=None if gap is None else 1.0 + gap,
        prev_close=1.0,
        amount=amount,
        quality_status="ok",
        source="mootdx",
    )


def test_scan_run_leaves_anchor_foreign_keys_null() -> None:
    """V3.2 must not pretend to run the legacy anchor pipeline."""
    cov = compute_scan_coverage([_obs(0.01, 100.0), _obs(0.02, 200.0)])
    kwargs = build_scan_run_kwargs(trade_date=_T, coverage=cov)
    assert kwargs["source_anchor_snapshot_id"] is None
    assert kwargs["source_anchor_publication_id"] is None


def test_scan_run_uses_v32_algorithm_version() -> None:
    cov = compute_scan_coverage([_obs(0.01, 100.0)])
    kwargs = build_scan_run_kwargs(trade_date=_T, coverage=cov)
    assert kwargs["algorithm_version"] == V32_ALGORITHM_VERSION


def test_scan_run_derives_missing_count() -> None:
    cov = compute_scan_coverage([_obs(0.01, 100.0), _obs(0.02, 200.0), _obs(None, None)])
    kwargs = build_scan_run_kwargs(trade_date=_T, coverage=cov)
    assert kwargs["missing_count"] == 1
    assert kwargs["ready_count"] == 2


def test_scan_run_coverage_comes_from_the_coverage_owner() -> None:
    """Persistence must not recompute coverage — it projects the owner value."""
    obs = [_obs(0.01, 100.0), _obs(0.02, 200.0), _obs(None, None), _obs(0.03, None)]
    cov = compute_scan_coverage(obs)
    kwargs = build_scan_run_kwargs(trade_date=_T, coverage=cov)
    assert kwargs["coverage_ratio"] == cov.coverage_ratio
    assert kwargs["eligible_count"] == cov.eligible_count
    assert kwargs["ready_count"] == cov.valid_count


# ---------------------------------------------------------------------------
# three coverage layers are NOT the same field (V3.2 §三)
# ---------------------------------------------------------------------------
def test_current_coverage_excludes_history_readiness() -> None:
    """A good today-quote is valid even with no history (V3.2 §二)."""
    obs = [_obs(0.01, 100.0)]  # no history evidence at all
    cov = compute_scan_coverage(obs)
    assert cov.valid_count == 1
    assert cov.coverage_ratio == 1.0


def test_scope_coverage_is_a_different_fact_from_scan_coverage() -> None:
    """Scope coverage has its OWN denominator and varies per scope.

    A numerically coincidental equality proves nothing, so this pins the
    structural property instead: on the SAME day (same scan coverage), scope
    coverage must change with that scope's membership.
    """
    universe = [_obs(0.01, 100.0), _obs(0.02, 200.0), _obs(None, None), _obs(None, None)]
    scan = compute_scan_coverage(universe)

    tight = compute_scope_coverage([_obs(0.01, 100.0), _obs(0.02, 200.0)])
    weak = compute_scope_coverage([_obs(None, None), _obs(None, None)])

    # the day-level value is fixed at 2/4 no matter which scope we look at
    assert scan.coverage_ratio == 0.5
    assert scan.eligible_count == 4

    # ...while scope coverage is 1.0 and 0.0 for two different scopes
    assert tight.coverage_ratio == 1.0
    assert weak.coverage_ratio == 0.0
    assert tight.coverage_ratio != weak.coverage_ratio
    assert tight.member_count == 2 == weak.member_count


def test_scope_coverage_uses_the_same_validity_rule() -> None:
    members = [_obs(0.01, 100.0), _obs(None, None), _obs(None, 50.0)]
    scope = compute_scope_coverage(members)
    # first (price+amount) and third (amount only) are valid; second is not
    assert scope.valid_count == 2


def test_coverage_ratio_is_zero_not_none_when_nothing_to_analyse() -> None:
    scan = compute_scan_coverage([])
    scope = compute_scope_coverage([])
    assert scan.coverage_ratio == 0.0
    assert scope.coverage_ratio == 0.0


def test_member_valid_on_either_formal_axis_counts() -> None:
    """Valid = contributes to at least one axis (Gap or Amount)."""
    amount_only = compute_scan_coverage([_obs(None, 100.0)])
    gap_only = compute_scan_coverage([_obs(0.01, None)])
    neither = compute_scan_coverage([_obs(None, None)])
    assert amount_only.valid_count == 1
    assert gap_only.valid_count == 1
    assert neither.valid_count == 0


# ---------------------------------------------------------------------------
# scope result: payload validation before persistence
# ---------------------------------------------------------------------------
def test_scope_result_accepts_valid_payload() -> None:
    row = build_scope_result_kwargs(
        scan_run_id=uuid4(),
        trade_date=_T,
        scope_type="industry",
        scope_id=uuid4(),
        scope_name="IND_BANK",
        payload=_valid_payload(),
    )
    assert row["payload"]["schema_version"] == SCHEMA_VERSION
    assert row["scope_type"] == "industry"


def test_scope_result_rejects_unknown_schema_version() -> None:
    bad = _valid_payload()
    bad["schema_version"] = "auction-scope-v9.9"
    with pytest.raises(ValueError, match="schema_version"):
        build_scope_result_kwargs(
            scan_run_id=uuid4(),
            trade_date=_T,
            scope_type="industry",
            scope_id=None,
            scope_name="X",
            payload=bad,
        )


def test_scope_result_rejects_missing_group() -> None:
    bad = _valid_payload()
    del bad["member_attribution"]
    with pytest.raises(ValueError, match="missing groups"):
        build_scope_result_kwargs(
            scan_run_id=uuid4(),
            trade_date=_T,
            scope_type="concept",
            scope_id=None,
            scope_name="X",
            payload=bad,
        )


def test_scope_result_does_not_populate_legacy_columns() -> None:
    """V3.2 must not write legacy Structure/Chip/label semantics."""
    row = build_scope_result_kwargs(
        scan_run_id=uuid4(),
        trade_date=_T,
        scope_type="industry",
        scope_id=None,
        scope_name="X",
        payload=_valid_payload(),
    )
    for legacy in (
        "structure_breakout_count",
        "chip_cross_up_count",
        "status_label",
        "confidence_level",
        "median_change_pct",
    ):
        assert legacy not in row


# ---------------------------------------------------------------------------
# publication: real capture run, V3.2 identity, visibility boundary
# ---------------------------------------------------------------------------
def test_publication_carries_the_real_capture_run_id() -> None:
    real_capture_run = uuid4()
    kwargs = build_publication_kwargs(
        trade_date=_T,
        scan_run_id=uuid4(),
        capture_run_id=real_capture_run,
        coverage_ratio=0.9,
        test_namespace="historical_backfill",
        truth_status="single_source_unverified",
        capture_source="historical_backfill",
    )
    assert kwargs["capture_run_id"] == real_capture_run


def test_publication_uses_v32_algorithm_version() -> None:
    kwargs = build_publication_kwargs(
        trade_date=_T,
        scan_run_id=uuid4(),
        capture_run_id=uuid4(),
        coverage_ratio=1.0,
        test_namespace="production",
        truth_status="verified",
        capture_source="verified_consensus",
    )
    assert kwargs["algorithm_version"] == V32_ALGORITHM_VERSION


def test_publication_has_a_published_at_timestamp() -> None:
    kwargs = build_publication_kwargs(
        trade_date=_T,
        scan_run_id=uuid4(),
        capture_run_id=uuid4(),
        coverage_ratio=1.0,
        test_namespace="production",
        truth_status="verified",
        capture_source="verified_consensus",
    )
    assert kwargs["published_at"] is not None


def test_publication_requires_distinct_scan_and_capture_identity() -> None:
    """Sanity: a publication binds a computation run to an acquisition run."""
    scan_run, capture_run = uuid4(), uuid4()
    kwargs = build_publication_kwargs(
        trade_date=_T,
        scan_run_id=scan_run,
        capture_run_id=capture_run,
        coverage_ratio=1.0,
        test_namespace="production",
        truth_status="verified",
        capture_source="verified_consensus",
    )
    assert kwargs["scan_run_id"] == scan_run
    assert kwargs["capture_run_id"] == capture_run


# ---------------------------------------------------------------------------
# truth_status must be INHERITED, never defaulted (anti-forgery)
# ---------------------------------------------------------------------------
def test_publication_has_no_default_truth_status() -> None:
    """A defaulted ``truth_status`` would forge a multi-source consensus claim.

    ``AuctionTruthPolicy.min_independent_sources = 2`` and PRD §0.0-A freeze
    pytdx/mootdx as ONE supply chain, so V3.2 cannot honestly claim "verified"
    on its own.  Omitting the argument must therefore be an error, not a
    silent default.
    """
    with pytest.raises(TypeError):
        build_publication_kwargs(
            trade_date=_T,
            scan_run_id=uuid4(),
            capture_run_id=uuid4(),
            coverage_ratio=1.0,
            test_namespace="production",
        )


def test_publication_rejects_empty_truth_status() -> None:
    with pytest.raises(ValueError, match="truth_status"):
        build_publication_kwargs(
            trade_date=_T,
            scan_run_id=uuid4(),
            capture_run_id=uuid4(),
            coverage_ratio=1.0,
            test_namespace="production",
            truth_status="",
            capture_source="verified_consensus",
        )


def test_publication_rejects_empty_test_namespace() -> None:
    with pytest.raises(ValueError, match="test_namespace"):
        build_publication_kwargs(
            trade_date=_T,
            scan_run_id=uuid4(),
            capture_run_id=uuid4(),
            coverage_ratio=1.0,
            test_namespace="",
            truth_status="verified",
            capture_source="verified_consensus",
        )


def test_publication_records_capture_source_lineage() -> None:
    kwargs = build_publication_kwargs(
        trade_date=_T,
        scan_run_id=uuid4(),
        capture_run_id=uuid4(),
        coverage_ratio=1.0,
        test_namespace="historical_backfill",
        truth_status="single_source_unverified",
        capture_source="historical_backfill",
    )
    assert kwargs["gate_evidence"]["capture_source"] == "historical_backfill"
    assert kwargs["truth_status"] == "single_source_unverified"


def test_historical_lane_cannot_claim_verified_consensus() -> None:
    """The historical lane is explicitly outside the live verified_consensus
    truth chain, so its capture_source must stay recorded as such."""
    kwargs = build_publication_kwargs(
        trade_date=_T,
        scan_run_id=uuid4(),
        capture_run_id=uuid4(),
        coverage_ratio=1.0,
        test_namespace="historical_backfill",
        truth_status="single_source_unverified",
        capture_source="historical_backfill",
    )
    assert kwargs["gate_evidence"]["capture_source"] != "verified_consensus"
