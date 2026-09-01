"""Persistence / publication contract tests for Auction V3.2 (no PostgreSQL).

KPI-1: V3.2 must NOT create publication rows itself.  Publication is owned by
``auction_publication_service.publish_auction_analysis``, which re-reads
ScanRun / CaptureRun / ScopeResult and evaluates ``evaluate_auction_publication_gate``.

KPI-7: these tests therefore consume the PRODUCTION gate owner directly instead
of a locally invented builder.  A configuration that the real gate rejects must
be rejected here too — the previous "historical_backfill + single_source_unverified
build succeeds" assertion was a false green and is gone.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from app.domain.auction.coverage import compute_scan_coverage, compute_scope_coverage
from app.domain.auction.member_observation import build_member_observation
from app.domain.auction.scope_payload import (
    SCHEMA_VERSION,
    build_scope_payload,
    canonical_scope_key,
    parse_scope_payload,
)
from app.services.auction_publication_service import (
    MIN_FORMAL_COVERAGE,
    VERIFIED_AUCTION_SOURCE,
    AuctionPublicationGateError,
    evaluate_auction_publication_gate,
)
from app.services.auction_scope_persistence_service import (
    V32_ALGORITHM_VERSION,
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


def _obs(gap: float | None, amount: float | None) -> Any:
    return build_member_observation(
        instrument_id=uuid4(),
        trade_date=_T,
        final_price=None if gap is None else 1.0 + gap,
        prev_close=1.0,
        amount=amount,
        quality_status="ok",
        source="mootdx",
    )


def _payload() -> dict:
    return build_scope_payload(
        algorithm_version=V32_ALGORITHM_VERSION,
        identity={"scope_key": "IND_BANK", "scope_name": "银行"},
        **_EMPTY_GROUPS,
    )


def _scope_row(**payload_kwargs: Any) -> Any:
    payload = _payload()
    payload.update(payload_kwargs)
    return build_scope_result_kwargs(
        scan_run_id=uuid4(),
        trade_date=_T,
        scope_type="industry",
        scope_id=uuid4(),
        scope_name="银行",
        payload=payload,
    )


# ---------------------------------------------------------------------------
# publication gate — the production owner decides (KPI-7)
# ---------------------------------------------------------------------------
def _gate(**over: Any) -> list[str]:
    base = {
        "truth_status": "verified",
        "test_namespace": "production",
        "scan_status": "succeeded",
        "scan_coverage": 0.98,
        "capture_source": VERIFIED_AUCTION_SOURCE,
        "capture_status": "succeeded",
        "scope_count": 12,
    }
    base.update(over)
    return evaluate_auction_publication_gate(**base)


def test_gate_allows_the_fully_verified_combination() -> None:
    assert _gate() == []


def test_gate_rejects_raw_mootdx_source() -> None:
    """Raw single-family acquisition must never be publishable."""
    assert "capture_source_not_verified_consensus" in _gate(capture_source="mootdx")


def test_gate_rejects_historical_backfill_source() -> None:
    """History is an INPUT to dynamics, never a publishable source."""
    reasons = _gate(capture_source="historical_backfill")
    assert "capture_source_not_verified_consensus" in reasons


def test_gate_rejects_low_coverage() -> None:
    # exactly at the formal threshold is allowed...
    assert _gate(scan_coverage=MIN_FORMAL_COVERAGE) == []
    # ...anything below is rejected (0.94 < 0.95 must NOT pass)
    assert "scan_coverage_below_threshold" in _gate(scan_coverage=0.94)
    assert "scan_coverage_below_threshold" in _gate(scan_coverage=MIN_FORMAL_COVERAGE - 0.01)


def test_gate_rejects_zero_scope_count() -> None:
    assert "aggregation_missing" in _gate(scope_count=0)


def test_gate_rejects_failed_capture() -> None:
    assert "capture_not_succeeded" in _gate(capture_status="failed")


def test_gate_rejects_unverified_truth() -> None:
    assert "auction_truth_not_verified" in _gate(truth_status="single_source_unverified")


def test_gate_rejects_non_production_namespace() -> None:
    assert "canary_or_test_namespace" in _gate(test_namespace="historical_backfill")


def test_gate_rejects_failed_scan() -> None:
    assert "scan_not_succeeded" in _gate(scan_status="failed")


def test_historical_lane_cannot_be_published_at_all() -> None:
    """The historical lane fails MULTIPLE gate conditions, never zero."""
    reasons = _gate(
        capture_source="historical_backfill",
        test_namespace="historical_backfill",
        truth_status="single_source_unverified",
    )
    assert len(reasons) >= 3


def test_publication_gate_error_type_exists_for_callers() -> None:
    assert issubclass(AuctionPublicationGateError, ValueError)


# ---------------------------------------------------------------------------
# scan run / scope result preparation (still V3.2-owned, non-publication)
# ---------------------------------------------------------------------------
def test_scan_run_leaves_anchor_foreign_keys_null() -> None:
    kwargs = build_scan_run_kwargs(
        trade_date=_T, coverage=compute_scan_coverage([_obs(0.01, 100.0)])
    )
    assert kwargs["source_anchor_snapshot_id"] is None
    assert kwargs["source_anchor_publication_id"] is None


def test_scan_run_uses_v32_algorithm_version() -> None:
    kwargs = build_scan_run_kwargs(
        trade_date=_T, coverage=compute_scan_coverage([_obs(0.01, 100.0)])
    )
    assert kwargs["algorithm_version"] == V32_ALGORITHM_VERSION


def test_scan_run_coverage_projected_from_the_coverage_owner() -> None:
    cov = compute_scan_coverage([_obs(0.01, 100.0), _obs(None, None)])
    kwargs = build_scan_run_kwargs(trade_date=_T, coverage=cov)
    assert kwargs["coverage_ratio"] == cov.coverage_ratio
    assert kwargs["ready_count"] == cov.valid_count


def test_scope_result_accepts_valid_payload() -> None:
    row = _scope_row()
    assert row["payload"]["schema_version"] == SCHEMA_VERSION


def test_scope_result_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        parse_scope_payload({**_payload(), "schema_version": "nope"})


def test_scope_result_rejects_missing_identity() -> None:
    bad = _payload()
    del bad["identity"]
    with pytest.raises(ValueError, match="identity"):
        parse_scope_payload(bad)


def test_scope_result_does_not_populate_legacy_columns() -> None:
    row = _scope_row()
    for legacy in (
        "structure_breakout_count",
        "chip_cross_up_count",
        "status_label",
        "confidence_level",
    ):
        assert legacy not in row


# ---------------------------------------------------------------------------
# KPI-3 canonical scope identity
# ---------------------------------------------------------------------------
def test_payload_carries_scope_key_distinct_from_scope_name() -> None:
    payload = build_scope_payload(
        algorithm_version=V32_ALGORITHM_VERSION,
        identity={"scope_key": "CPT_ROBOT", "scope_name": "机器人"},
        **_EMPTY_GROUPS,
    )
    assert canonical_scope_key(payload) == "CPT_ROBOT"
    assert payload["identity"]["scope_name"] == "机器人"
    assert canonical_scope_key(payload) != payload["identity"]["scope_name"]


def test_payload_requires_a_non_empty_scope_key() -> None:
    with pytest.raises(ValueError, match="scope_key"):
        build_scope_payload(
            algorithm_version=V32_ALGORITHM_VERSION,
            identity={"scope_name": "机器人"},
            **_EMPTY_GROUPS,
        )


def test_canonical_scope_key_rejects_name_only_payload() -> None:
    with pytest.raises(ValueError, match="scope_key"):
        canonical_scope_key({"identity": {"scope_name": "机器人"}})


# ---------------------------------------------------------------------------
# coverage layering
# ---------------------------------------------------------------------------
def test_current_coverage_excludes_history_readiness() -> None:
    cov = compute_scan_coverage([_obs(0.01, 100.0)])
    assert cov.valid_count == 1
    assert cov.coverage_ratio == 1.0


def test_scope_coverage_is_a_different_fact_from_scan_coverage() -> None:
    universe = [_obs(0.01, 100.0), _obs(0.02, 200.0), _obs(None, None), _obs(None, None)]
    scan = compute_scan_coverage(universe)
    tight = compute_scope_coverage([_obs(0.01, 100.0), _obs(0.02, 200.0)])
    weak = compute_scope_coverage([_obs(None, None), _obs(None, None)])

    assert scan.coverage_ratio == 0.5
    assert tight.coverage_ratio == 1.0
    assert weak.coverage_ratio == 0.0
    assert tight.coverage_ratio != weak.coverage_ratio


def test_member_valid_on_either_formal_axis_counts() -> None:
    assert compute_scan_coverage([_obs(None, 100.0)]).valid_count == 1
    assert compute_scan_coverage([_obs(0.01, None)]).valid_count == 1
    assert compute_scan_coverage([_obs(None, None)]).valid_count == 0


def test_coverage_ratio_is_zero_not_none_when_empty() -> None:
    assert compute_scan_coverage([]).coverage_ratio == 0.0
    assert compute_scope_coverage([]).coverage_ratio == 0.0


# ---------------------------------------------------------------------------
# A2: parse_scope_payload must fail CLOSED on identity (before persistence)
# ---------------------------------------------------------------------------
def _valid_payload_copy() -> dict:
    return build_scope_payload(
        algorithm_version=V32_ALGORITHM_VERSION,
        identity={"scope_key": "CPT_ROBOT", "scope_name": "机器人"},
        **_EMPTY_GROUPS,
    )


def test_parser_rejects_empty_identity_mapping() -> None:
    bad = _valid_payload_copy()
    bad["identity"] = {}
    with pytest.raises(ValueError, match="scope_key"):
        parse_scope_payload(bad)


def test_parser_rejects_blank_scope_key() -> None:
    bad = _valid_payload_copy()
    bad["identity"] = {"scope_key": "   ", "scope_name": "机器人"}
    with pytest.raises(ValueError, match="scope_key"):
        parse_scope_payload(bad)


def test_parser_rejects_non_mapping_identity() -> None:
    bad = _valid_payload_copy()
    bad["identity"] = "CPT_ROBOT"
    with pytest.raises(ValueError, match="mapping"):
        parse_scope_payload(bad)


def test_parser_rejects_missing_scope_key() -> None:
    bad = _valid_payload_copy()
    bad["identity"] = {"scope_name": "机器人"}
    with pytest.raises(ValueError, match="scope_key"):
        parse_scope_payload(bad)


def test_parser_accepts_valid_identity() -> None:
    parsed = parse_scope_payload(_valid_payload_copy())
    assert canonical_scope_key(parsed) == "CPT_ROBOT"


def test_persistence_rejects_malformed_identity_before_write() -> None:
    """The failure must happen at persistence preparation, not at API read."""
    bad = _valid_payload_copy()
    bad["identity"] = {}
    with pytest.raises(ValueError):
        build_scope_result_kwargs(
            scan_run_id=uuid4(),
            trade_date=_T,
            scope_type="concept",
            scope_id=uuid4(),
            scope_name="机器人",
            payload=bad,
        )
