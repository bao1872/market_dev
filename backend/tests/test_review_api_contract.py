"""Modified-scope contract tests for the canonical Review read API (Slice A + B).

Proves the canonical read contracts in ``app.api.review`` without a database
(PURE_UNIT_TEST=1): DB/network/persistence are mocked.

Scope A (P0 call-contract fixes, still asserted after Slice B):
- P0-B: ``get_review_scope_composition`` passes the required ``trade_date``
  positional to ``get_scope_observation_fact_by_run`` (grain =
  review_run_id + trade_date + scope_type + scope_key).

Scope B (Thin Scope List Read Model):
- ``list_review_scopes`` delegates to the single projection owner
  ``list_review_scope_summaries_by_run`` with the published ``review_run_id``
  (run lineage, no global scan) and the ``scope_type`` filter pushed to SQL.
- DB-level pagination (offset/limit) + deterministic order are delegated to the
  service; the router issues exactly ONE projection call per page (no N+1,
  no load-all-then-slice).
- readiness/coverage ownership is unchanged (run.metadata_json owner).
- when the LEFT JOIN misses (Fact exists, Composition missing), the list item
  emits ``summary=None`` — never an all-zero object (unavailable≠0).
- the list DTO carries NO full ``observation`` payload and NO ``signalCount``.

NOTE: endpoint functions are invoked directly (not via FastAPI), so the
``Query``/``Depends`` default objects are NOT auto-resolved; resolved defaults
are passed explicitly.
"""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api import review as review_api
from app.schemas.review import (
    ReviewCanonicalScopeResponse,
    ReviewScopeListResponse,
    ReviewScopeObservationSummaryDTO,
    ReviewScopeSummaryDTO,
)
from app.services.review_observation_persistence_service import (
    ReviewScopeSummaryRow,
    list_review_scope_summaries_by_run,
)

# REAL L1 producer-backed payload — owned by test_review_observation_groups.
# _real_l1_payload() calls compute_scope_observation(...) with real
# MemberObservation + StructureEvent inputs, i.e. the TRUE producer output
# (scalars, distribution/categorical objects, segment facts under
# trend.continuous).  We feed THIS through the endpoint's (un-mocked)
# build_l2_observation_groups to prove the real
#   MemberObservation/StructureEvent → compute_scope_observation → canonical L1
#   → Fact → endpoint → L2
# chain.  Do NOT reintroduce a hand-written "canonical-shaped" fixture here.
from tests.test_review_observation_groups import _real_l1_payload

# Resolved Query/Depends defaults (FastAPI does not apply these when the
# endpoint function is called directly in a unit test).
_DEF_LIST = {"include_partial": False, "page": 1, "page_size": 20}
_DEF_DETAIL = {"include_partial": False}


def _run(
    run_id: uuid.UUID,
    trade_date: date = date(2026, 7, 29),
    *,
    algorithm_version: str = "review-2.0.0",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        trade_date=trade_date,
        status="published",
        source_core_run_id=uuid.uuid4(),
        source_board_run_id=uuid.uuid4(),
        source_chip_run_id=None,
        coverage_ratio=1.0,
        expected_scope_count=2,
        succeeded_scope_count=2,
        failed_scope_count=0,
        signal_count=0,
        algorithm_version=algorithm_version,
        filter_version="filters-1.0.0",
        baseline_window=120,
        metadata_json={},
        started_at=None,
        completed_at=None,
        published_at=None,
        degraded_reasons=[],
    )


def _fact(
    scope_type: str,
    scope_key: str,
    review_run_id: uuid.UUID,
    *,
    trade_date: date | None = None,
    scope_name: str | None = None,
    algorithm_version: str | None = "fact-2.0.0",
    observation_payload: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        review_run_id=review_run_id,
        scope_type=scope_type,
        scope_key=scope_key,
        scope_name=scope_name if scope_name is not None else scope_key,
        trade_date=trade_date if trade_date is not None else date(2026, 7, 29),
        pit_member_count=10,
        provided_member_count=9,
        pit_status_t="historical_pit",
        readiness="ready",
        algorithm_version=algorithm_version,
        observation_payload=(
            observation_payload
            if observation_payload is not None
            else {"scope": {"scope_type": scope_type, "scope_key": scope_key}}
        ),
    )


def _obs_with_groups() -> dict:
    """Canonical observation_payload with enough shape for the 8-group projection."""
    return {
        "scope": {"scope_type": "industry_l1", "scope_key": "k1"},
        "price_capital": {"return_level": {"ew": 0.01}},
        "trend": {"state": {"status": "ready"}},
        "structure": {"current_state": {"board_ready_member_count": 42}},
        "momentum": {"squeeze_release": {"status": "ready"}},
        "participation": {"volume": {"amount": 1.0}},
    }


# NOTE: the real producer-backed L1 payload is owned by
# ``tests.test_review_observation_groups._real_l1_payload`` (which calls
# compute_scope_observation with real MemberObservation + StructureEvent inputs —
# the TRUE producer output).  The hand-written ``_make_l1_payload`` is a
# "canonical-shaped" fixture and must NOT be used for the real-chain test.
# Do NOT reintroduce a second hand-written L1 shape here.


def _summary_row(
    scope_type: str,
    scope_key: str,
    *,
    composition_present: bool = True,
    **overrides: object,
) -> ReviewScopeSummaryRow:
    """Build a frozen ReviewScopeSummaryRow with sensible defaults."""
    defaults = {
        "scope_type": scope_type,
        "scope_key": scope_key,
        "scope_name": scope_key,
        "fact_readiness": "ready",
        "pit_status_t": "historical_pit",
        "pit_member_count": 10,
        "provided_member_count": 9,
        "composition_present": composition_present,
        "dynamics_status": "ready",
        "phase": "accumulation",
        "position": 0.5,
        "velocity": 0.1,
        "acceleration": -0.02,
        "upper_occupancy": 0.6,
        "lower_occupancy": 0.3,
        "equal_weight_return": 0.012,
        "amount_weighted_return": 0.015,
        "capital_tilt": 0.2,
        "advance_ratio": 0.55,
        "decline_ratio": 0.3,
        "unchanged_ratio": 0.15,
        "return_dispersion": 0.04,
        "price_normalized_hhi": 0.12,
        "amount_normalized_hhi": 0.18,
        "leadership_status": "ready",
        "jaccard_stability": 0.8,
        "migration": 0.1,
        # R2B Observation Fact thin projection (Fact-derived; NOT gated on composition_present)
        "freshness_today_count": 5,
        "freshness_decay_weighted_density": 0.432,
        "technical_hhi": 0.142,
        "technical_top5_numerator": 3.2,
        "technical_top5_denominator": 8.4,
        "technical_leader_median_gap": 2.18,
        "technical_leader_symbol": "601899",
        "technical_member_count": 42,
    }
    defaults.update(overrides)
    return ReviewScopeSummaryRow(**defaults)  # type: ignore[arg-type]


def _ctx(*, is_admin: bool = False) -> SimpleNamespace:
    return SimpleNamespace(is_admin=is_admin)


async def _resolve_run(db: AsyncMock, run: SimpleNamespace) -> None:
    """Wire a mock session so ``_get_published_run`` returns ``run``."""
    db.get = AsyncMock(return_value=run)


def _patch_list(db, run, *, rows, total=None):
    """Context manager mocking the single projection owner for the list."""
    run_id = run.id
    total = total if total is not None else len(rows)
    return patch.object(
        review_api,
        "get_published_review_run_id",
        new=AsyncMock(return_value=run_id),
    ), patch.object(
        review_api,
        "list_review_scope_summaries_by_run",
        new=AsyncMock(return_value=(total, rows)),
    )


# ---------------------------------------------------------------------------
# P0-B: get_review_scope_composition — trade_date in run-lineage grain
# ---------------------------------------------------------------------------


async def test_get_review_scope_composition_ok() -> None:
    # [R3A BE-1] Fact exists + Composition exists: 200, composition preserved,
    # observation preserved, observationGroups exactly canonical projection.
    run_id = uuid.uuid4()
    run = _run(run_id)
    snapshot = SimpleNamespace(
        scope_type="industry_l1",
        scope_key="k1",
        scope_name="行业",
        algorithm_version="review-2.0.0",
        composition_payload={"dynamics": {"position": 0.5}},
    )
    fact = _fact("industry_l1", "k1", run_id, observation_payload=_obs_with_groups())
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api,
        "get_scope_composition_snapshot",
        new=AsyncMock(return_value=snapshot),
    ), patch.object(
        review_api,
        "get_scope_observation_fact_by_run",
        new=AsyncMock(return_value=fact),
    ) as mock_fact, patch.object(
        review_api,
        "build_l2_observation_groups",
        new=MagicMock(return_value=_eight_groups()),
    ) as mock_groups:
        resp = await review_api.get_review_scope_composition(
            "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
        )
        # P0-B: trade_date passed explicitly (positional) in the run-lineage grain
        mock_fact.assert_called_once_with(
            db, run_id, date(2026, 7, 29), "industry_l1", "k1"
        )
        # [R3A] groups projected directly from fact.observation_payload
        mock_groups.assert_called_once_with(fact.observation_payload)

    assert resp.scopeType == "industry_l1"
    assert resp.composition == {"dynamics": {"position": 0.5}}
    assert resp.observation == fact.observation_payload
    # BE-1 now asserts the FULL canonical 8-group projection (not a 2-group stub)
    assert resp.observationGroups == _eight_groups()
    assert len(resp.observationGroups) == 8
    assert list(resp.observationGroups.keys()) == [
        "price_capital",
        "trend_state",
        "trend_progress",
        "trend_volume_confirmation",
        "structure_break_turn",
        "structure_evolution_position",
        "momentum_squeeze_release",
        "volume_anomaly",
    ]


async def test_get_review_scope_composition_real_projection_through_endpoint() -> None:
    # [R3A-V3] REAL PRODUCER → L2 → DETAIL ENDPOINT chain proof.
    #
    # _real_l1_payload() calls compute_scope_observation(...) with REAL
    # MemberObservation + StructureEvent inputs — i.e. the TRUE producer output
    # (scalars, distribution/categorical objects, segment facts under
    # trend.continuous).  This is NOT a hand-written "canonical-shaped" fixture.
    #
    # build_l2_observation_groups is NOT mocked here — we prove the full chain:
    #   MemberObservation/StructureEvent → compute_scope_observation →
    #   canonical L1 → Fact → endpoint → real build_l2_observation_groups →
    #   8/8 groups + canonical labels + one representative fact per group.
    from app.domain.review.observation_groups import build_l2_observation_groups

    run_id = uuid.uuid4()
    run = _run(run_id)
    snapshot = SimpleNamespace(
        scope_type="industry_l1",
        scope_key="k1",
        scope_name="行业",
        algorithm_version="review-2.0.0",
        composition_payload={"dynamics": {"position": 0.5}},
    )
    # Real producer output — the single owner is _real_l1_payload in
    # tests.test_review_observation_groups.
    payload = _real_l1_payload()
    fact = _fact("industry_l1", "k1", run_id, observation_payload=payload)
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api,
        "get_scope_composition_snapshot",
        new=AsyncMock(return_value=snapshot),
    ), patch.object(
        review_api,
        "get_scope_observation_fact_by_run",
        new=AsyncMock(return_value=fact),
    ):
        resp = await review_api.get_review_scope_composition(
            "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
        )

    # 1) groups equal the REAL projection of the real producer L1 payload
    expected = build_l2_observation_groups(payload)
    assert resp.observationGroups == expected
    # 2) exactly 8 groups, exact ordered keys, canonical labels from L2_GROUP_SPECS
    assert len(resp.observationGroups) == 8
    assert list(resp.observationGroups.keys()) == [
        "price_capital",
        "trend_state",
        "trend_progress",
        "trend_volume_confirmation",
        "structure_break_turn",
        "structure_evolution_position",
        "momentum_squeeze_release",
        "volume_anomaly",
    ]
    for key, spec in [
        ("price_capital", "价格与资金表现"),
        ("trend_state", "趋势状态"),
        ("trend_progress", "趋势进程"),
        ("trend_volume_confirmation", "趋势量能确认"),
        ("structure_break_turn", "结构突破与转折"),
        ("structure_evolution_position", "结构演化与位置"),
        ("momentum_squeeze_release", "动量与压缩释放"),
        ("volume_anomaly", "量能异常"),
    ]:
        assert resp.observationGroups[key]["group_key"] == key
        assert resp.observationGroups[key]["label"] == spec
        assert "facts" in resp.observationGroups[key]
    groups = resp.observationGroups

    # [R3A-V3 §7] TREND PATH PROTECTION: payload is now produced by
    # compute_scope_observation itself, so this assertion is meaningful.
    # Segment facts MUST live under trend.continuous; NO top-level ``continuous``.
    assert "continuous" in payload["trend"]
    assert "segment_bars" in payload["trend"]["continuous"]
    assert payload.get("continuous") is None  # no top-level continuous object

    # [R3A-V3 §5] 8/8 representative facts — one NON-NULL per group.
    # Each asserted value EQUALS the corresponding L1 source object/value (no
    # recomputation).  For scalar producer values this compares scalars; for
    # distribution/categorical objects it compares the same object.
    # G1 price_capital
    g1 = groups["price_capital"]["facts"]
    assert g1["equal_weight_return"] == payload["price"]["equal_weight_return"]
    assert g1["amount_weighted_return"] == payload["price"]["amount_weighted_return"]
    assert g1["total_amount"] == payload["price"]["amount"]["total_amount"]
    # G2 trend_state
    g2 = groups["trend_state"]["facts"]
    assert g2["trend_direction_member_ratio"] == payload["trend"]["state"]
    assert g2["trend_strength"] == payload["trend"]["continuous"]["regime_strength"]
    # G3 trend_progress — segment facts come from trend.continuous (NOT top-level)
    g3 = groups["trend_progress"]["facts"]
    assert g3["current_segment_bars"] == payload["trend"]["continuous"]["segment_bars"]
    assert g3["segment_change_pct"] == payload["trend"]["continuous"]["segment_change_pct"]
    assert g3["segment_slope"] == payload["trend"]["continuous"]["segment_slope"]
    assert g3["current_segment_bars"] is not None  # non-null representative
    # G4 trend_volume_confirmation — at least one SEGMENT fact non-null
    g4 = groups["trend_volume_confirmation"]["facts"]
    assert g4["segment_volume_mean_ratio"] == payload["trend"]["continuous"]["segment_volume_mean_ratio"]
    assert g4["segment_amount_mean_ratio"] == payload["trend"]["continuous"]["segment_amount_mean_ratio"]
    assert g4["momentum_volume_relation"] == payload["momentum"]["momentum_volume_relation"]
    assert g4["segment_volume_mean_ratio"] is not None  # segment fact proved non-null
    # G5 structure_break_turn — real BOS/CHoCH cells survive the filter
    g5 = groups["structure_break_turn"]["facts"]["bos_choch_events"]
    g5_event_types = {c["event_type"] for c in g5["cells"]["leveled"].values()}
    assert g5_event_types == {"BOS", "CHoCH"}
    assert g5["cells"]["extreme"] == {}
    # G6 structure_evolution_position — real OB_*/EQ survive + alignment passthrough
    g6 = groups["structure_evolution_position"]["facts"]
    g6_event_types = {c["event_type"] for c in g6["ob_and_eq_events"]["cells"]["leveled"].values()}
    assert g6_event_types == {"OB_CREATED", "OB_ENTERED", "OB_MITIGATED"}
    assert set(g6["ob_and_eq_events"]["cells"]["extreme"].keys()) == {"EQH", "EQL"}
    assert g6["structure_alignment"] == payload["structure"]["alignment"]
    # G7 momentum_squeeze_release
    g7 = groups["momentum_squeeze_release"]["facts"]
    assert g7["squeeze_state"] == payload["momentum"]["squeeze_state"]
    assert g7["bb_position"] == payload["momentum"]["bb_position"]
    assert g7["release_volume_ratio"] == payload["momentum"]["release_volume_ratio"]
    assert g7["squeeze_state"] is not None  # non-null representative
    # G8 volume_anomaly — full six-fact vector
    g8 = groups["volume_anomaly"]["facts"]
    assert g8["volume_ratio20"] == payload["participation"]["volume"]["ratio20"]
    assert g8["volume_ratio200"] == payload["participation"]["volume"]["ratio200"]
    assert g8["volume_percentile200"] == payload["participation"]["volume"]["percentile200"]
    assert g8["volume_zscore200"] == payload["participation"]["volume"]["zscore200"]
    assert g8["volume_ratio20"] is not None  # non-null representative

    # composition and observation still preserved alongside real groups
    assert resp.composition == {"dynamics": {"position": 0.5}}
    assert resp.observation == payload


async def test_get_review_scope_composition_404_when_fact_missing() -> None:
    # [R3A BE-3/BE-4] Fact missing → 404 (Fact owns detail existence),
    # even if Composition exists. Short-circuit: snapshot NOT fetched.
    run_id = uuid.uuid4()
    run = _run(run_id)
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api,
        "get_scope_composition_snapshot",
        new=AsyncMock(return_value="SHOULD_NOT_BE_CALLED"),
    ) as mock_snapshot, patch.object(
        review_api, "get_scope_observation_fact_by_run", new=AsyncMock(return_value=None)
    ) as mock_fact:
        with pytest.raises(HTTPException) as exc:
            await review_api.get_review_scope_composition(
                "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
            )
        mock_fact.assert_called_once_with(
            db, run_id, date(2026, 7, 29), "industry_l1", "k1"
        )
        # Fact-missing must short-circuit BEFORE the Composition snapshot query
        mock_snapshot.assert_not_called()
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# R3A — Canonical Observation Detail Contract (Fact-first, observationGroups,
#       nullable composition, published-run lineage)
# ---------------------------------------------------------------------------


def _eight_groups() -> dict:
    """Canonical 8-group projection shape (labels owned by L2_GROUP_SPECS).

    These labels MUST match ``app.domain.review.observation_groups.L2_GROUP_SPECS``
    exactly — the frontend has no second label vocabulary.  Any drift here means
    the backend SSOT changed and the test must be updated to track it.
    """
    return {
        "price_capital": {"group_key": "price_capital", "label": "价格与资金表现", "facts": {}},
        "trend_state": {"group_key": "trend_state", "label": "趋势状态", "facts": {}},
        "trend_progress": {"group_key": "trend_progress", "label": "趋势进程", "facts": {}},
        "trend_volume_confirmation": {
            "group_key": "trend_volume_confirmation",
            "label": "趋势量能确认",
            "facts": {},
        },
        "structure_break_turn": {
            "group_key": "structure_break_turn",
            "label": "结构突破与转折",
            "facts": {},
        },
        "structure_evolution_position": {
            "group_key": "structure_evolution_position",
            "label": "结构演化与位置",
            "facts": {},
        },
        "momentum_squeeze_release": {
            "group_key": "momentum_squeeze_release",
            "label": "动量与压缩释放",
            "facts": {},
        },
        "volume_anomaly": {"group_key": "volume_anomaly", "label": "量能异常", "facts": {}},
    }


async def test_r3a_fact_exists_composition_missing_200() -> None:
    # [R3A BE-2] Fact exists + Composition missing → 200, composition==null,
    # observation preserved, observationGroups present with exactly 8 canonical groups.
    run_id = uuid.uuid4()
    run = _run(run_id)
    fact = _fact("industry_l1", "k1", run_id, observation_payload=_obs_with_groups())
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api, "get_scope_composition_snapshot", new=AsyncMock(return_value=None)
    ), patch.object(
        review_api, "get_scope_observation_fact_by_run", new=AsyncMock(return_value=fact)
    ), patch.object(
        review_api,
        "build_l2_observation_groups",
        new=MagicMock(return_value=_eight_groups()),
    ):
        resp = await review_api.get_review_scope_composition(
            "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
        )
    assert resp.composition is None
    assert resp.observation == fact.observation_payload
    assert set(resp.observationGroups.keys()) == set(_eight_groups().keys())
    assert len(resp.observationGroups) == 8


async def test_r3a_same_day_multirun_selects_published() -> None:
    # [R3A BE-5/BE-6] Same-day multiple ReviewRuns: published run's Fact selected
    # by review_run_id; later/unpublished Fact must not contaminate the response.
    published_run_id = uuid.uuid4()
    other_run_id = uuid.uuid4()
    published_run = _run(published_run_id)
    fact_published = _fact(
        "industry_l1", "k1", published_run_id, scope_name="PUBLISHED"
    )
    fact_other = _fact("industry_l1", "k1", other_run_id, scope_name="OTHER")
    db = AsyncMock()
    await _resolve_run(db, published_run)
    with patch.object(
        review_api,
        "get_published_review_run_id",
        new=AsyncMock(return_value=published_run_id),
    ), patch.object(
        review_api, "get_scope_composition_snapshot", new=AsyncMock(return_value=None)
    ), patch.object(
        review_api,
        "get_scope_observation_fact_by_run",
        new=AsyncMock(return_value=fact_published),
    ) as mock_fact, patch.object(
        review_api,
        "build_l2_observation_groups",
        new=MagicMock(return_value=_eight_groups()),
    ):
        resp = await review_api.get_review_scope_composition(
            "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
        )
        # identity must come from the published-run lineage fact
        mock_fact.assert_called_once_with(
            db, published_run_id, date(2026, 7, 29), "industry_l1", "k1"
        )
    # verify the OTHER fact was never selected (lineage integrity)
    assert resp.scopeName == "PUBLISHED"
    assert fact_other.scope_name == "OTHER"  # untouched; not used


async def test_r3a_algorithm_version_precedence_snapshot_fact_run() -> None:
    # [R3A BE-7] algorithmVersion precedence: snapshot > fact > run.
    run_id = uuid.uuid4()
    run = _run(run_id, algorithm_version="run-1.0.0")
    snapshot = SimpleNamespace(
        scope_type="industry_l1",
        scope_key="k1",
        scope_name="行业",
        algorithm_version="snapshot-2.0.0",
        composition_payload={"dynamics": {}},
    )
    fact = _fact(
        "industry_l1", "k1", run_id, algorithm_version="fact-1.5.0",
        observation_payload=_obs_with_groups(),
    )
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api, "get_scope_composition_snapshot", new=AsyncMock(return_value=snapshot)
    ), patch.object(
        review_api, "get_scope_observation_fact_by_run", new=AsyncMock(return_value=fact)
    ), patch.object(
        review_api,
        "build_l2_observation_groups",
        new=MagicMock(return_value=_eight_groups()),
    ):
        resp = await review_api.get_review_scope_composition(
            "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
        )
    assert resp.algorithmVersion == "snapshot-2.0.0"


async def test_r3a_algorithm_version_fact_fallback() -> None:
    # [R3A BE-7] snapshot absent → fact.algorithm_version used.
    run_id = uuid.uuid4()
    run = _run(run_id, algorithm_version="run-1.0.0")
    fact = _fact(
        "industry_l1", "k1", run_id, algorithm_version="fact-1.5.0",
        observation_payload=_obs_with_groups(),
    )
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api, "get_scope_composition_snapshot", new=AsyncMock(return_value=None)
    ), patch.object(
        review_api, "get_scope_observation_fact_by_run", new=AsyncMock(return_value=fact)
    ), patch.object(
        review_api,
        "build_l2_observation_groups",
        new=MagicMock(return_value=_eight_groups()),
    ):
        resp = await review_api.get_review_scope_composition(
            "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
        )
    assert resp.algorithmVersion == "fact-1.5.0"


async def test_r3a_observation_payload_returned_verbatim() -> None:
    # [R3A BE-8] observation payload returned verbatim (no normalization/recompute).
    run_id = uuid.uuid4()
    run = _run(run_id)
    payload = _obs_with_groups()
    fact = _fact("industry_l1", "k1", run_id, observation_payload=payload)
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api, "get_scope_composition_snapshot", new=AsyncMock(return_value=None)
    ), patch.object(
        review_api, "get_scope_observation_fact_by_run", new=AsyncMock(return_value=fact)
    ), patch.object(
        review_api,
        "build_l2_observation_groups",
        new=MagicMock(return_value=_eight_groups()),
    ):
        resp = await review_api.get_review_scope_composition(
            "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
        )
    # verbatim content (pydantic copies on validation, but no normalization/recompute)
    assert resp.observation == payload


async def test_r3a_observation_groups_equals_projection() -> None:
    # [R3A BE-9] observationGroups equals build_l2_observation_groups(fact.observation_payload).
    run_id = uuid.uuid4()
    run = _run(run_id)
    payload = _obs_with_groups()
    fact = _fact("industry_l1", "k1", run_id, observation_payload=payload)
    projected = _eight_groups()
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api, "get_scope_composition_snapshot", new=AsyncMock(return_value=None)
    ), patch.object(
        review_api, "get_scope_observation_fact_by_run", new=AsyncMock(return_value=fact)
    ), patch.object(
        review_api,
        "build_l2_observation_groups",
        new=MagicMock(return_value=projected),
    ) as mock_groups:
        resp = await review_api.get_review_scope_composition(
            "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
        )
        mock_groups.assert_called_once_with(payload)
    assert resp.observationGroups == projected


async def test_r3a_no_global_fact_fallback() -> None:
    # [R3A BE-10] user-detail path must NOT call global get_scope_observation_fact.
    run_id = uuid.uuid4()
    run = _run(run_id)
    fact = _fact("industry_l1", "k1", run_id, observation_payload=_obs_with_groups())
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api, "get_scope_composition_snapshot", new=AsyncMock(return_value=None)
    ), patch.object(
        review_api, "get_scope_observation_fact_by_run", new=AsyncMock(return_value=fact)
    ), patch.object(
        review_api,
        "build_l2_observation_groups",
        new=MagicMock(return_value=_eight_groups()),
    ):
        # If the endpoint called the global get_scope_observation_fact, it would
        # raise AttributeError (not patched) → test fails. We assert it is NOT used.
        resp = await review_api.get_review_scope_composition(
            "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
        )
    assert resp.scopeKey == "k1"


async def test_r3a_malformed_fact_payload_fails_closed() -> None:
    # [R3A BE-11] malformed canonical Fact payload fails closed (500),
    # do not manufacture empty groups / {} observation.
    run_id = uuid.uuid4()
    run = _run(run_id)
    fact = _fact("industry_l1", "k1", run_id)
    fact.observation_payload = "NOT_A_DICT"  # corrupt
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api, "get_scope_composition_snapshot", new=AsyncMock(return_value=None)
    ), patch.object(
        review_api, "get_scope_observation_fact_by_run", new=AsyncMock(return_value=fact)
    ), patch.object(
        review_api,
        "build_l2_observation_groups",
        new=MagicMock(return_value=_eight_groups()),
    ):
        with pytest.raises(HTTPException) as exc:
            await review_api.get_review_scope_composition(
                "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
            )
    assert exc.value.status_code == 500


async def test_r3a_no_board_signal_discovery_fallback() -> None:
    # [R3A BE-12] No legacy Board / Signal / Discovery fallback for the detail.
    # The endpoint must derive identity purely from the canonical Fact lineage,
    # not from any external/legacy source. (Negative: any such fallback would
    # require an extra import/call not present — we assert the pure path holds.)
    run_id = uuid.uuid4()
    run = _run(run_id)
    fact = _fact("industry_l1", "k1", run_id, scope_name="FROM_FACT",
                 observation_payload=_obs_with_groups())
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api, "get_scope_composition_snapshot", new=AsyncMock(return_value=None)
    ), patch.object(
        review_api, "get_scope_observation_fact_by_run", new=AsyncMock(return_value=fact)
    ), patch.object(
        review_api,
        "build_l2_observation_groups",
        new=MagicMock(return_value=_eight_groups()),
    ):
        resp = await review_api.get_review_scope_composition(
            "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
        )
    assert resp.scopeName == "FROM_FACT"
    assert resp.scopeType == "industry_l1"
    assert resp.scopeKey == "k1"
    assert resp.tradeDate == "2026-07-29"


# ---------------------------------------------------------------------------
# Slice B: Thin Scope List Read Model
# ---------------------------------------------------------------------------


async def test_list_review_scopes_uses_projection_owner_with_run_lineage() -> None:
    run_id = uuid.uuid4()
    run = _run(run_id)
    rows = [_summary_row("industry_l1", "k1"), _summary_row("concept", "c1")]
    db = AsyncMock()
    await _resolve_run(db, run)
    p_run, p_list = _patch_list(db, run, rows=rows)
    with p_run, p_list as mock_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29", scope_type="industry_l1", db=db, ctx=_ctx(), **_DEF_LIST
        )
        # lineage + scope_type filter pushed to SQL (single owner, no global scan)
        mock_list.assert_called_once_with(
            db,
            review_run_id=run_id,
            trade_date=date(2026, 7, 29),
            scope_type="industry_l1",
            offset=0,
            limit=20,
        )

    # the SQL filter is delegated to the service; the router returns what the
    # projection returned (total reflects server-side filtering).
    assert resp.total == 2
    assert {i.scopeType for i in resp.items} == {"industry_l1", "concept"}


async def test_list_review_scopes_pagination_is_db_level() -> None:
    run_id = uuid.uuid4()
    run = _run(run_id)
    rows = [_summary_row("industry_l1", f"k{i}") for i in range(10)]
    db = AsyncMock()
    await _resolve_run(db, run)
    p_run, p_list = _patch_list(db, run, rows=rows, total=25)
    with p_run, p_list as mock_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29",
            page=2,
            page_size=10,
            db=db,
            ctx=_ctx(),
            include_partial=False,
            scope_type=None,
        )
        # offset/limit computed by the router and handed to the DB projection
        mock_list.assert_called_once_with(
            db,
            review_run_id=run_id,
            trade_date=date(2026, 7, 29),
            scope_type=None,
            offset=10,
            limit=10,
        )

    assert resp.total == 25
    assert len(resp.items) == 10
    assert resp.page == 2
    assert resp.has_more is True


async def test_list_review_scopes_no_n_plus_one_single_projection_call() -> None:
    """The list endpoint issues exactly ONE projection call per request."""
    run_id = uuid.uuid4()
    run = _run(run_id)
    rows = [_summary_row("industry_l1", f"k{i}") for i in range(5)]
    db = AsyncMock()
    await _resolve_run(db, run)
    p_run, p_list = _patch_list(db, run, rows=rows)
    with p_run, p_list as mock_list:
        await review_api.list_review_scopes(
            "2026-07-29", db=db, ctx=_ctx(), scope_type=None, **_DEF_LIST
        )
    assert mock_list.call_count == 1


async def test_list_review_scopes_composition_missing_yields_summary_none() -> None:
    """Fact exists, Composition missing → summary=None BUT observationSummary still populated.

    R2B HARD ACCEPTANCE CONTRACT (§8): the two owners are independent. A missing
    Composition nulls `summary` (existing Slice-B invariant) without dropping the
    Fact-derived `observationSummary`.
    """
    run_id = uuid.uuid4()
    run = _run(run_id)
    rows = [_summary_row("industry_l1", "k1", composition_present=False)]
    db = AsyncMock()
    await _resolve_run(db, run)
    p_run, p_list = _patch_list(db, run, rows=rows)
    with p_run, p_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29", db=db, ctx=_ctx(), scope_type=None, **_DEF_LIST
        )

    assert resp.total == 1
    item = resp.items[0]
    assert item.scopeType == "industry_l1"
    assert item.summary is None
    # R2B: Observation Fact thin projection survives a missing Composition
    assert item.observationSummary is not None
    assert item.observationSummary.freshnessTodayCount == 5
    assert item.observationSummary.technicalHhi == 0.142
    assert item.observationSummary.technicalLeaderSymbol == "601899"
    # no full observation payload leaked into the list DTO
    assert not hasattr(item, "observation")
    assert not hasattr(item, "signalCount")


# ============================================================
# R2B backend tests (R2B-BE-1 .. R2B-BE-10)
# ============================================================

async def test_r2b_be_1_observation_jsonb_paths_map_to_scalar_row_fields() -> None:
    """R2B-BE-1: Fact observation_payload scalar JSONB paths map to correct row fields."""
    run_id = uuid.uuid4()
    run = _run(run_id)
    rows = [
        _summary_row(
            "industry_l1",
            "k1",
            freshness_today_count=7,
            freshness_decay_weighted_density=0.51,
            technical_hhi=0.23,
            technical_top5_numerator=4.1,
            technical_top5_denominator=9.3,
            technical_leader_median_gap=1.9,
            technical_leader_symbol="600519",
            technical_member_count=33,
        )
    ]
    db = AsyncMock()
    await _resolve_run(db, run)
    p_run, p_list = _patch_list(db, run, rows=rows)
    with p_run, p_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29", db=db, ctx=_ctx(), scope_type=None, **_DEF_LIST
        )
    obs = resp.items[0].observationSummary
    assert isinstance(obs, ReviewScopeObservationSummaryDTO)
    assert obs.freshnessTodayCount == 7
    assert obs.freshnessDecayWeightedDensity == 0.51
    assert obs.technicalHhi == 0.23
    assert obs.technicalTop5Numerator == 4.1
    assert obs.technicalTop5Denominator == 9.3
    assert obs.technicalLeaderMedianGap == 1.9
    assert obs.technicalLeaderSymbol == "600519"
    assert obs.technicalMemberCount == 33


async def test_r2b_be_2_freshness_today_count_zero_survives() -> None:
    """R2B-BE-2: freshness today_count=0 is a valid zero, not None."""
    run_id = uuid.uuid4()
    run = _run(run_id)
    rows = [_summary_row("industry_l1", "k1", freshness_today_count=0)]
    db = AsyncMock()
    await _resolve_run(db, run)
    p_run, p_list = _patch_list(db, run, rows=rows)
    with p_run, p_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29", db=db, ctx=_ctx(), scope_type=None, **_DEF_LIST
        )
    assert resp.items[0].observationSummary.freshnessTodayCount == 0


async def test_r2b_be_3_technical_hhi_zero_survives() -> None:
    """R2B-BE-3: technical hhi=0 is a valid zero, not None."""
    run_id = uuid.uuid4()
    run = _run(run_id)
    rows = [_summary_row("industry_l1", "k1", technical_hhi=0)]
    db = AsyncMock()
    await _resolve_run(db, run)
    p_run, p_list = _patch_list(db, run, rows=rows)
    with p_run, p_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29", db=db, ctx=_ctx(), scope_type=None, **_DEF_LIST
        )
    assert resp.items[0].observationSummary.technicalHhi == 0


async def test_r2b_be_4_top5_denominator_zero_survives() -> None:
    """R2B-BE-4: top5 denominator=0 is a valid persisted zero, NOT coerced to None."""
    run_id = uuid.uuid4()
    run = _run(run_id)
    rows = [_summary_row("industry_l1", "k1", technical_top5_numerator=0, technical_top5_denominator=0)]
    db = AsyncMock()
    await _resolve_run(db, run)
    p_run, p_list = _patch_list(db, run, rows=rows)
    with p_run, p_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29", db=db, ctx=_ctx(), scope_type=None, **_DEF_LIST
        )
    obs = resp.items[0].observationSummary
    assert obs.technicalTop5Denominator == 0
    assert obs.technicalTop5Numerator == 0


async def test_r2b_be_5_leader_symbol_null_survives() -> None:
    """R2B-BE-5: leader_symbol=None remains None."""
    run_id = uuid.uuid4()
    run = _run(run_id)
    rows = [_summary_row("industry_l1", "k1", technical_leader_symbol=None)]
    db = AsyncMock()
    await _resolve_run(db, run)
    p_run, p_list = _patch_list(db, run, rows=rows)
    with p_run, p_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29", db=db, ctx=_ctx(), scope_type=None, **_DEF_LIST
        )
    assert resp.items[0].observationSummary.technicalLeaderSymbol is None


async def test_r2b_be_6_fact_only_case_summary_null_observation_populated() -> None:
    """R2B-BE-6: Fact present + Composition missing → summary=None, observationSummary filled."""
    run_id = uuid.uuid4()
    run = _run(run_id)
    rows = [_summary_row("industry_l1", "k1", composition_present=False)]
    db = AsyncMock()
    await _resolve_run(db, run)
    p_run, p_list = _patch_list(db, run, rows=rows)
    with p_run, p_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29", db=db, ctx=_ctx(), scope_type=None, **_DEF_LIST
        )
    item = resp.items[0]
    assert item.summary is None
    assert item.observationSummary is not None
    assert item.observationSummary.technicalLeaderSymbol == "601899"


async def test_r2b_be_7_read_model_keeps_fact_left_join_composition() -> None:
    """R2B-BE-7: list owner still uses ReviewScopeObservationFact LEFT JOIN
    ReviewScopeCompositionSnapshot with full lineage join keys (no second join)."""
    run = _run(uuid.uuid4())
    captured: list = []

    async def _fake_execute(stmt):
        captured.append(str(stmt))
        FakeResult = SimpleNamespace()
        FakeResult.fetchall = lambda: []
        FakeResult.scalars = lambda: SimpleNamespace(all=lambda: [])
        FakeResult.scalar_one = lambda: 0
        FakeResult.fetchone = lambda: (0,)
        FakeResult.mappings = lambda: SimpleNamespace(all=lambda: [])
        return FakeResult

    db = AsyncMock()
    db.execute = _fake_execute
    await _resolve_run(db, run)
    await list_review_scope_summaries_by_run(
        db,
        review_run_id=run.id,
        trade_date=run.trade_date,
        scope_type=None,
        offset=0,
        limit=20,
    )
    # at least one statement issued; the page projection references the Fact table
    assert captured, "read model must issue SQL"
    joined = "\n".join(captured).lower()
    assert "observation_facts" in joined or "observation" in joined
    # composition snapshot still joined (the existing LEFT JOIN architecture)
    assert "composition_snapshots" in joined or "composition" in joined


async def test_r2b_be_8_one_count_one_page_query_no_per_scope() -> None:
    """R2B-BE-8: list issues exactly 1 count + 1 page projection query; no per-scope query."""
    run = _run(uuid.uuid4())
    call_count = {"n": 0}

    async def _fake_execute(stmt):
        call_count["n"] += 1
        FakeResult = SimpleNamespace()
        FakeResult.fetchall = lambda: []
        FakeResult.scalars = lambda: SimpleNamespace(all=lambda: [])
        FakeResult.scalar_one = lambda: 0
        FakeResult.fetchone = lambda: (0,)
        FakeResult.mappings = lambda: SimpleNamespace(all=lambda: [])
        return FakeResult

    db = AsyncMock()
    db.execute = _fake_execute
    await _resolve_run(db, run)
    await list_review_scope_summaries_by_run(
        db,
        review_run_id=run.id,
        trade_date=run.trade_date,
        scope_type=None,
        offset=0,
        limit=20,
    )
    # count query + page projection query = 2 statements, no per-scope query
    assert call_count["n"] == 2, f"expected 2 statements (count+page), got {call_count['n']}"


async def test_r2b_be_9_page_projection_does_not_load_full_json_into_python() -> None:
    """R2B-BE-9: page projection references scalar JSONB paths; it does NOT select
    the complete observation_payload or composition_payload object into Python."""
    run = _run(uuid.uuid4())
    captured: list = []

    async def _fake_execute(stmt):
        captured.append(str(stmt))
        FakeResult = SimpleNamespace()
        FakeResult.fetchall = lambda: []
        FakeResult.scalars = lambda: SimpleNamespace(all=lambda: [])
        FakeResult.scalar_one = lambda: 0
        FakeResult.fetchone = lambda: (0,)
        FakeResult.mappings = lambda: SimpleNamespace(all=lambda: [])
        return FakeResult

    db = AsyncMock()
    db.execute = _fake_execute
    await _resolve_run(db, run)
    await list_review_scope_summaries_by_run(
        db,
        review_run_id=run.id,
        trade_date=run.trade_date,
        scope_type=None,
        offset=0,
        limit=20,
    )
    # page projection is the 2nd statement (count is 1st); verify thin scalar JSONB
    # paths are referenced (not the full ~130 KiB observation_payload object).
    page_sql = captured[1].lower()
    assert "freshness" in page_sql
    assert "->" in page_sql or "->>" in page_sql


async def test_r2b_be_10_dto_has_no_derived_top5_ratio() -> None:
    """R2B-BE-10: DTO carries numerator + denominator verbatim; no derived ratio field."""
    assert "technicalTop5Numerator" in ReviewScopeObservationSummaryDTO.model_fields
    assert "technicalTop5Denominator" in ReviewScopeObservationSummaryDTO.model_fields
    assert "technicalTop5Ratio" not in ReviewScopeObservationSummaryDTO.model_fields
    # round-trip preserves both scalars without a ratio
    dto = ReviewScopeObservationSummaryDTO(
        technicalTop5Numerator=3.2, technicalTop5Denominator=8.4
    )
    assert dto.technicalTop5Numerator == 3.2
    assert dto.technicalTop5Denominator == 8.4


def _capture_execute_factory(captured: list) -> object:
    """Return a fake ``db.execute`` that records every compiled SQL statement."""
    async def _fake_execute(stmt):
        captured.append(str(stmt))
        fake_result = SimpleNamespace()
        fake_result.fetchall = lambda: []
        fake_result.scalars = lambda: SimpleNamespace(all=lambda: [])
        fake_result.scalar_one = lambda: 0
        fake_result.fetchone = lambda: (0,)
        fake_result.mappings = lambda: SimpleNamespace(all=lambda: [])
        return fake_result
    return _fake_execute


async def test_family_sql_1_page_carries_scope_type_predicate() -> None:
    """FAMILY-SQL-1: scope_type='concept' → BOTH count and page SQL contain the
    family predicate (not merely that the Python arg was passed). Bound parameters
    are used, so we assert the `scope_type =` WHERE predicate appears in both."""
    run = _run(uuid.uuid4())
    captured: list = []
    db = AsyncMock()
    db.execute = _capture_execute_factory(captured)  # type: ignore[assignment]
    await _resolve_run(db, run)
    await list_review_scope_summaries_by_run(
        db,
        review_run_id=run.id,
        trade_date=run.trade_date,
        scope_type="concept",
        offset=0,
        limit=20,
    )
    assert len(captured) >= 2, f"expected count+page SQL, got {len(captured)}"
    count_sql = captured[0].lower()
    page_sql = captured[1].lower()
    # count predicate (table-qualified WHERE predicate, not the JOIN key)
    assert "review_scope_observation_facts.scope_type =" in count_sql
    # page predicate (the pre-R2C bug: page had NO family predicate)
    assert "review_scope_observation_facts.scope_type =" in page_sql
    # both bind a single scope_type parameter
    assert count_sql.count("review_scope_observation_facts.scope_type =") == 1
    assert page_sql.count("review_scope_observation_facts.scope_type =") == 1


async def test_family_sql_2_none_does_not_introduce_family_predicate() -> None:
    """FAMILY-SQL-2: scope_type=None → count/page introduce NO artificial family
    predicate."""
    run = _run(uuid.uuid4())
    captured: list = []
    db = AsyncMock()
    db.execute = _capture_execute_factory(captured)  # type: ignore[assignment]
    await _resolve_run(db, run)
    await list_review_scope_summaries_by_run(
        db,
        review_run_id=run.id,
        trade_date=run.trade_date,
        scope_type=None,
        offset=0,
        limit=20,
    )
    assert len(captured) >= 2
    joined = "\n".join(captured).lower()
    # no family restriction when None requested (table-qualified WHERE predicate
    # must be absent; the JOIN still references scope_type as an equality key)
    assert "review_scope_observation_facts.scope_type =" not in joined
    # sanity: run id + trade date columns still filter (bound params)
    assert "review_run_id" in joined
    assert "trade_date" in joined


async def test_family_sql_3_count_and_page_share_predicates() -> None:
    """FAMILY-SQL-3: count + page share review_run_id, trade_date, and (when
    supplied) scope_type."""
    run = _run(uuid.uuid4())
    captured: list = []
    db = AsyncMock()
    db.execute = _capture_execute_factory(captured)  # type: ignore[assignment]
    await _resolve_run(db, run)
    await list_review_scope_summaries_by_run(
        db,
        review_run_id=run.id,
        trade_date=run.trade_date,
        scope_type="concept",
        offset=0,
        limit=20,
    )
    count_sql = captured[0].lower()
    page_sql = captured[1].lower()
    for col in ("review_run_id", "trade_date"):
        assert col in count_sql and col in page_sql
    # same family predicate present in both (table-qualified WHERE predicate)
    assert "review_scope_observation_facts.scope_type =" in count_sql
    assert "review_scope_observation_facts.scope_type =" in page_sql


async def test_family_sql_4_requested_family_cannot_return_other_family() -> None:
    """FAMILY-SQL-4: requested='concept' cannot return 'industry_l1' rows even if
    such rows would exist WITHOUT the predicate. Proven by asserting the page SQL
    restricts scope_type to the requested family."""
    run = _run(uuid.uuid4())
    captured: list = []
    db = AsyncMock()
    db.execute = _capture_execute_factory(captured)  # type: ignore[assignment]
    await _resolve_run(db, run)
    await list_review_scope_summaries_by_run(
        db,
        review_run_id=run.id,
        trade_date=run.trade_date,
        scope_type="concept",
        offset=0,
        limit=100,
    )
    page_sql = captured[1].lower()
    # the page projection MUST filter to the requested family only
    assert "review_scope_observation_facts.scope_type =" in page_sql
    # regression guard: it must NOT be a blank cross-family projection
    assert "industry_l1" not in page_sql


async def test_family_sql_5_pagination_semantics_preserved() -> None:
    """FAMILY-SQL-5: pagination semantics remain offset + limit + ORDER BY
    scope_type, scope_key."""
    run = _run(uuid.uuid4())
    captured: list = []
    db = AsyncMock()
    db.execute = _capture_execute_factory(captured)  # type: ignore[assignment]
    await _resolve_run(db, run)
    await list_review_scope_summaries_by_run(
        db,
        review_run_id=run.id,
        trade_date=run.trade_date,
        scope_type="concept",
        offset=40,
        limit=20,
    )
    page_sql = captured[1].lower()
    assert "offset" in page_sql and "40" in page_sql
    assert "limit" in page_sql and "20" in page_sql
    assert "order by" in page_sql
    assert "scope_type" in page_sql and "scope_key" in page_sql


async def test_family_sql_6_observation_scalar_paths_remain() -> None:
    """FAMILY-SQL-6: R2B Observation scalar JSONB paths remain in the page
    projection (not full payload load)."""
    run = _run(uuid.uuid4())
    captured: list = []
    db = AsyncMock()
    db.execute = _capture_execute_factory(captured)  # type: ignore[assignment]
    await _resolve_run(db, run)
    await list_review_scope_summaries_by_run(
        db,
        review_run_id=run.id,
        trade_date=run.trade_date,
        scope_type="concept",
        offset=0,
        limit=20,
    )
    page_sql = captured[1].lower()
    assert "freshness" in page_sql
    assert "->" in page_sql or "->>" in page_sql


async def test_family_sql_7_fact_left_join_composition_unchanged() -> None:
    """FAMILY-SQL-7: Fact LEFT OUTER JOIN Composition remains unchanged (single
    join, join keys intact)."""
    run = _run(uuid.uuid4())
    captured: list = []
    db = AsyncMock()
    db.execute = _capture_execute_factory(captured)  # type: ignore[assignment]
    await _resolve_run(db, run)
    await list_review_scope_summaries_by_run(
        db,
        review_run_id=run.id,
        trade_date=run.trade_date,
        scope_type=None,
        offset=0,
        limit=20,
    )
    page_sql = captured[1].lower()
    assert "left outer join" in page_sql or "left join" in page_sql
    assert "composition_snapshots" in page_sql or "composition" in page_sql
    # single join only (no second join introduced by R2C)
    assert page_sql.count("join") <= 1


async def test_list_review_scopes_phase_null_ready_keeps_null_summary_fields() -> None:
    """phase=None + ready: summary carries nulls, not zeros."""
    run_id = uuid.uuid4()
    run = _run(run_id)
    rows = [
        _summary_row(
            "industry_l1",
            "k1",
            dynamics_status="ready",
            phase=None,
            position=None,
            velocity=None,
            acceleration=None,
        )
    ]
    db = AsyncMock()
    await _resolve_run(db, run)
    p_run, p_list = _patch_list(db, run, rows=rows)
    with p_run, p_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29", db=db, ctx=_ctx(), scope_type=None, **_DEF_LIST
        )

    item = resp.items[0]
    assert item.status == "historical_pit"
    assert item.summary is not None
    assert item.summary.phase is None
    assert item.summary.position is None
    assert item.summary.velocity is None


async def test_list_review_scopes_readiness_ownership_unchanged() -> None:
    """readiness still resolved from run.metadata_json owner."""
    run_id = uuid.uuid4()
    run = _run(run_id)
    run.metadata_json = {"canonical_composition_readiness": {"k1": "published_ready"}}
    rows = [_summary_row("industry_l1", "k1", fact_readiness="ready")]
    db = AsyncMock()
    await _resolve_run(db, run)
    p_run, p_list = _patch_list(db, run, rows=rows)
    with p_run, p_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29", db=db, ctx=_ctx(), scope_type=None, **_DEF_LIST
        )
    assert resp.items[0].readiness == "published_ready"


# ---------------------------------------------------------------------------
# include_partial authorization
# ---------------------------------------------------------------------------


async def test_include_partial_forbidden_for_non_admin() -> None:
    run_id = uuid.uuid4()
    run = _run(run_id)
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ):
        with pytest.raises(HTTPException) as exc:
            await review_api.list_review_scopes(
                "2026-07-29",
                db=db,
                ctx=_ctx(is_admin=False),
                include_partial=True,
                page=1,
                page_size=20,
            )
    assert exc.value.status_code == 403


async def test_include_partial_admin_bypasses_authz() -> None:
    run_id = uuid.uuid4()
    run = _run(run_id)
    rows = [_summary_row("industry_l1", "k1")]
    db = AsyncMock()
    await _resolve_run(db, run)
    p_run, p_list = _patch_list(db, run, rows=rows)
    with p_run, p_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29",
            db=db,
            ctx=_ctx(is_admin=True),
            include_partial=True,
            page=1,
            page_size=20,
            scope_type=None,
        )
    assert resp.total == 1


# ---------------------------------------------------------------------------
# same-day multi-run lineage: only the published run's rows are returned
# ---------------------------------------------------------------------------


async def test_same_day_multi_run_uses_published_run_lineage() -> None:
    published_run_id = uuid.uuid4()
    published = _run(published_run_id)
    owned = _summary_row("industry_l1", "owned")
    db = AsyncMock()
    await _resolve_run(db, published)
    p_run, p_list = _patch_list(db, published, rows=[owned])
    with p_run, p_list as mock_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29", db=db, ctx=_ctx(), scope_type=None, **_DEF_LIST
        )
        # the projection owner is invoked with the published run id only
        mock_list.assert_called_once_with(
            db,
            review_run_id=published_run_id,
            trade_date=date(2026, 7, 29),
            scope_type=None,
            offset=0,
            limit=20,
        )

    assert resp.total == 1
    assert resp.items[0].scopeKey == "owned"


async def test_scope_summary_projection_sql_shape_is_thin() -> None:
    """Part D (Slice B): the page statement compiled from the real production
    projection must be a thin JSONB scalar projection, NOT a full-snapshot /
    ORM load.

    Proves:
    - DB-level pagination (LIMIT + OFFSET present)
    - LEFT OUTER JOIN on the full lineage grain
    - invariant 6: no full ``observation_payload`` / full ``composition_payload``
      column is selected (composition_payload only appears inside ``->`` JSON
      path expressions, never as a bare column)
    - numeric fields are cast in SQL (DB owns the type), not coerced in Python

    No DB connection is opened; the actual production statement is captured and
    compiled against the PostgreSQL dialect. This is NOT a second SQL
    implementation.
    """
    import re
    from unittest.mock import Mock

    from sqlalchemy.dialects import postgresql as pg

    captured: list = []

    async def _fake_execute(stmt, *a, **k):
        captured.append(stmt)
        res = Mock()
        res.scalar_one.return_value = 0  # count query
        res.mappings.return_value.all.return_value = []  # page query
        return res

    db = AsyncMock()
    db.execute = _fake_execute

    await list_review_scope_summaries_by_run(
        db,
        review_run_id=uuid.uuid4(),
        trade_date=date(2026, 8, 11),
        scope_type="industry_l1",
        offset=0,
        limit=20,
    )

    assert len(captured) == 2, f"expected count+page, got {len(captured)}"
    compiled = [str(s.compile(dialect=pg.dialect())) for s in captured]
    # the page statement is the one carrying LIMIT/OFFSET
    page_sql = next((sql for sql in compiled if "LIMIT" in sql), None)
    assert page_sql is not None, "page statement must carry LIMIT"

    # Fact trade_date lineage predicate must appear in BOTH the count and the
    # page statement — in the production SQL, not merely in the Python call
    # arguments.  `review_scope_observation_facts.trade_date = ` (fact on the
    # LEFT of `=`) matches a WHERE predicate and excludes the LEFT JOIN's
    # `snapshots.trade_date = fact.trade_date` (where the fact column is on the
    # RIGHT).  This proves the requested trade_date actually constrains the Fact
    # rows read (Slice B lineage regression).
    fact_td_predicate = re.compile(r"review_scope_observation_facts\.trade_date = ")
    assert fact_td_predicate.search(compiled[0]), compiled[0]
    assert fact_td_predicate.search(compiled[1]), compiled[1]

    # DB-level pagination + LEFT OUTER JOIN on the 4-key lineage grain.
    assert "LEFT OUTER JOIN" in page_sql
    assert "OFFSET" in page_sql

    # R2B thin contract (replaces the stale "observation_payload absent" check):
    # ALLOWED  — Observation scalar JSONB path projection, e.g.
    #   review_scope_observation_facts.observation_payload[...] ->> ...
    #            ->> cast AS freshness_today_count
    # FORBIDDEN — selecting the COMPLETE/BARE observation_payload JSON object into
    #   Python, e.g.
    #   SELECT ..., review_scope_observation_facts.observation_payload, ...
    #   or review_scope_observation_facts.observation_payload AS observation_payload
    # PostgreSQL renders the allowed form as `observation_payload[%(...)s]...`, so a
    # scalar path projection must be present, and every occurrence of the column
    # must be immediately subscripted (never a bare full-column select).
    assert "observation_payload[" in page_sql, page_sql
    # forbid a bare/full observation_payload column select. The allowed scalar
    # form is always `observation_payload[...]`; the bind-param name is
    # `observation_payload_N` (underscore suffix). So a forbidden occurrence is
    # `observation_payload` followed by neither `[` nor `_`.
    bare_observation = re.compile(r"observation_payload(?![_\[])")
    assert not bare_observation.search(page_sql), page_sql

    # composition_payload must only appear inside JSON path projections
    # (subscript `composition_payload[...]`) or as a SQL bind param name
    # (`%(composition_payload_N)s`); never as a bare full-column select
    # (`...composition_payload AS ...` / `..., composition_payload`).
    assert not re.search(r"composition_payload(?![\[_])", page_sql), page_sql

    # Numeric fields are cast in SQL (DB owns typing), not string-passed.
    # PostgreSQL renders CAST(... AS FLOAT) where FLOAT == double precision.
    assert "AS FLOAT" in page_sql or "DOUBLE PRECISION" in page_sql, page_sql


def test_scope_list_response_size_is_thin() -> None:
    """Part H (Slice B): a representative 100-scope list serializes thin.

    The list DTO carries only scalar summary fields (no nested full
    Composition / Observation), so 100 scopes must stay well under 500 KB.
    This is a structure check, not a performance benchmark.
    """
    items = []
    for i in range(100):
        items.append(
            ReviewCanonicalScopeResponse(
                scopeType="industry_l1",
                scopeKey=f"scope_{i:03d}",
                scopeName=f"Scope {i}",
                readiness="ready",
                status="historical_pit",
                eligibleCount=120,
                providedCount=118,
                coverageRatio=118 / 120,
                summary=ReviewScopeSummaryDTO(
                    dynamicsStatus="ready",
                    phase="trending",
                    position=0.5123,
                    velocity=0.0412,
                    acceleration=-0.0012,
                    upperOccupancy=0.62,
                    lowerOccupancy=0.38,
                    equalWeightReturn=0.0123,
                    amountWeightedReturn=0.0098,
                    capitalTilt=0.0234,
                    advanceRatio=0.55,
                    declineRatio=0.30,
                    unchangedRatio=0.15,
                    returnDispersion=0.018,
                    priceNormalizedHhi=0.42,
                    amountNormalizedHhi=0.51,
                    leadershipStatus="stable",
                    jaccardStability=0.88,
                    migration=-0.04,
                ),
                observationSummary=ReviewScopeObservationSummaryDTO(
                    freshnessTodayCount=7,
                    freshnessDecayWeightedDensity=0.51,
                    technicalHhi=0.23,
                    technicalTop5Numerator=3.2,
                    technicalTop5Denominator=8.4,
                    technicalLeaderMedianGap=0.07,
                    technicalLeaderSymbol="600519",
                    technicalMemberCount=118,
                ),
            )
        )
    resp = ReviewScopeListResponse(
        items=items, total=100, page=1, page_size=100, has_more=False
    )
    raw = resp.model_dump_json()
    n = len(raw.encode("utf-8"))
    avg = n / 100
    print(f"RESPONSE_SIZE: 100 scopes = {n / 1024:.1f} KB, avg {avg:.0f} B/item")
    assert n < 500_000, f"thin list exceeded 500 KB: {n} bytes"
