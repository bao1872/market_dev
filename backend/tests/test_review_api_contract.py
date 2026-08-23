"""Modified-scope contract tests for the canonical Review read API (Slice A).

Proves the two P0 call-contract fixes in ``app.api.review`` without a database:

- P0-A: ``list_review_scopes`` must pass ``review_run_id`` (run lineage) to
  ``list_scope_observation_facts_by_run`` and NOT pass an unsupported
  ``scope_type`` kwarg; the ``scope_type`` filter is applied lightly in the
  router after the run-lineage read.
- P0-B: ``get_review_scope_composition`` must pass the required ``trade_date``
  positional to ``get_scope_observation_fact_by_run`` (grain =
  review_run_id + trade_date + scope_type + scope_key), not fall back to a
  global ``WHERE trade_date=?`` scan.

NOTE: the endpoint functions are invoked directly (not through FastAPI), so the
``Query``/``Depends`` default objects are NOT auto-resolved. The call sites pass
the resolved default values explicitly (``include_partial=False``, ``page=1``,
``page_size=20``) so the real body runs.

Pure-unit mode (``PURE_UNIT_TEST=1``): DB/network/services are mocked.
Covers: published run, scope_type filter, pagination, detail lookup, 404,
include_partial authorization, same-day multi-run lineage.
"""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import review as review_api

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


def _ctx(*, is_admin: bool = False) -> SimpleNamespace:
    return SimpleNamespace(is_admin=is_admin)


async def _resolve_run(db: AsyncMock, run: SimpleNamespace) -> None:
    """Wire a mock session so ``_get_published_run`` returns ``run``."""
    db.get = AsyncMock(return_value=run)


# ---------------------------------------------------------------------------
# P0-A: list_review_scopes — run lineage + scope_type filter + pagination
# ---------------------------------------------------------------------------


async def test_list_review_scopes_published_and_scope_type_filter() -> None:
    run_id = uuid.uuid4()
    run = _run(run_id)
    facts = [_fact("industry_l1", "k1", run_id), _fact("concept", "c1", run_id)]
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api,
        "list_scope_observation_facts_by_run",
        new=AsyncMock(return_value=facts),
    ) as mock_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29", scope_type="industry_l1", db=db, ctx=_ctx(), **_DEF_LIST
        )
        # lineage: service queried by the published run id (no global scan)
        mock_list.assert_called_once_with(db, review_run_id=run_id)

    # scope_type filter applied in router, only industry_l1 remains
    assert resp.total == 1
    assert resp.items[0].scopeType == "industry_l1"


async def test_list_review_scopes_pagination() -> None:
    run_id = uuid.uuid4()
    run = _run(run_id)
    facts = [_fact("industry_l1", f"k{i}", run_id) for i in range(25)]
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api,
        "list_scope_observation_facts_by_run",
        new=AsyncMock(return_value=facts),
    ):
        resp = await review_api.list_review_scopes(
            "2026-07-29",
            page=2,
            page_size=10,
            db=db,
            ctx=_ctx(),
            include_partial=False,
            scope_type=None,
        )

    assert resp.total == 25
    assert len(resp.items) == 10
    assert resp.page == 2
    assert resp.has_more is True


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
    facts = [_fact("industry_l1", "k1", run_id)]
    db = AsyncMock()
    await _resolve_run(db, run)
    with patch.object(
        review_api, "get_published_review_run_id", new=AsyncMock(return_value=run_id)
    ), patch.object(
        review_api,
        "list_scope_observation_facts_by_run",
        new=AsyncMock(return_value=facts),
    ):
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
# same-day multi-run lineage: only the published run's facts are returned
# ---------------------------------------------------------------------------


async def test_same_day_multi_run_uses_published_run_lineage() -> None:
    published_run_id = uuid.uuid4()
    other_run_id = uuid.uuid4()
    published = _run(published_run_id)
    # A different same-day run must NOT leak into the published run's list.
    _leaked_fact = _fact("industry_l1", "leaked", other_run_id)
    owned_fact = _fact("industry_l1", "owned", published_run_id)
    db = AsyncMock()
    await _resolve_run(db, published)
    with patch.object(
        review_api,
        "get_published_review_run_id",
        new=AsyncMock(return_value=published_run_id),
    ), patch.object(
        review_api,
        "list_scope_observation_facts_by_run",
        new=AsyncMock(return_value=[owned_fact]),
    ) as mock_list:
        resp = await review_api.list_review_scopes(
            "2026-07-29", db=db, ctx=_ctx(), scope_type=None, **_DEF_LIST
        )
        # the service is invoked with the published run id, never a poll() scan
        mock_list.assert_called_once_with(db, review_run_id=published_run_id)

    assert resp.total == 1
    assert resp.items[0].scopeKey == "owned"
