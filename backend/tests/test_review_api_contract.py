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
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import review as review_api
from app.schemas.review import (
    ReviewCanonicalScopeResponse,
    ReviewScopeListResponse,
    ReviewScopeSummaryDTO,
)
from app.services.review_observation_persistence_service import (
    ReviewScopeSummaryRow,
    list_review_scope_summaries_by_run,
)

# Resolved Query/Depends defaults (FastAPI does not apply these when the
# endpoint function is called directly in a unit test).
_DEF_LIST = {"include_partial": False, "page": 1, "page_size": 20}
_DEF_DETAIL = {"include_partial": False}


def _run(run_id: uuid.UUID, trade_date: date = date(2026, 7, 29)) -> SimpleNamespace:
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
        algorithm_version="review-2.0.0",
        filter_version="filters-1.0.0",
        baseline_window=120,
        metadata_json={},
        started_at=None,
        completed_at=None,
        published_at=None,
        degraded_reasons=[],
    )


def _fact(scope_type: str, scope_key: str, review_run_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        scope_type=scope_type,
        scope_key=scope_key,
        scope_name=scope_key,
        pit_member_count=10,
        provided_member_count=9,
        pit_status_t="historical_pit",
        readiness="ready",
        observation_payload={"scope": {"scope_type": scope_type, "scope_key": scope_key}},
    )


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
    run_id = uuid.uuid4()
    run = _run(run_id)
    snapshot = SimpleNamespace(
        scope_type="industry_l1",
        scope_key="k1",
        scope_name="行业",
        algorithm_version="review-2.0.0",
        composition_payload={"dynamics": {"position": 0.5}},
    )
    fact = _fact("industry_l1", "k1", run_id)
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
    ) as mock_fact:
        resp = await review_api.get_review_scope_composition(
            "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
        )
        # P0-B: trade_date passed explicitly (positional) in the run-lineage grain
        mock_fact.assert_called_once_with(
            db, run_id, date(2026, 7, 29), "industry_l1", "k1"
        )

    assert resp.scopeType == "industry_l1"
    assert resp.composition == {"dynamics": {"position": 0.5}}


async def test_get_review_scope_composition_404_when_missing() -> None:
    run_id = uuid.uuid4()
    run = _run(run_id)
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api, "get_scope_composition_snapshot", new=AsyncMock(return_value=None)
    ):
        with pytest.raises(HTTPException) as exc:
            await review_api.get_review_scope_composition(
                "2026-07-29", "industry_l1", "k1", db=db, ctx=_ctx(), **_DEF_DETAIL
            )
    assert exc.value.status_code == 404


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
    """Fact exists, Composition missing → summary=None (never all-zero)."""
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
    # no full observation payload leaked into the list DTO
    assert not hasattr(item, "observation")
    assert not hasattr(item, "signalCount")


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
    page_sql = None
    for stmt in captured:
        sql = str(stmt.compile(dialect=pg.dialect()))
        if "LIMIT" in sql:
            page_sql = sql
    assert page_sql is not None, "page statement must carry LIMIT"

    # DB-level pagination + LEFT OUTER JOIN on the 4-key lineage grain.
    assert "LEFT OUTER JOIN" in page_sql
    assert "OFFSET" in page_sql

    # Thin: never select the full observation payload.
    assert "observation_payload" not in page_sql, page_sql

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
