"""Slice 3R — Review core-only API contract tests (pure-unit).

Closes the two API blockers found in review of commit 453abf56:
  Blocker 1: admin create route must NOT pass source_board_run_id to
             create_run_with_result (would raise TypeError -> HTTP 500).
  Blocker 2: nullable Board lineage must serialize DB NULL -> JSON null,
             not the string "None"; historical UUID -> UUID string.

Covers user-specified gates A-H. No DB / network; PURE_UNIT_TEST=1 safe.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.review import ReviewRunCreateRequest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_fake_run(*, source_board_run_id: uuid.UUID | None) -> Any:
    """Minimal MarketReviewRun stand-in for serializer tests."""
    return type(
        "FakeRun",
        (),
        {
            "id": uuid.uuid4(),
            "trade_date": date(2026, 8, 20),
            "source_core_run_id": uuid.uuid4(),
            "source_board_run_id": source_board_run_id,
            "source_chip_run_id": None,
            "degraded_reasons": [],
            "algorithm_version": "v1",
            "filter_version": "v1",
            "baseline_window": 120,
            "status": "created",
            "expected_scope_count": 0,
            "succeeded_scope_count": 0,
            "failed_scope_count": 0,
            "signal_count": 0,
            "coverage_ratio": None,
            "started_at": None,
            "completed_at": None,
            "published_at": None,
            "metadata_json": {},
            "created_at": datetime(2026, 8, 20, 9, 0, 0),
            "updated_at": datetime(2026, 8, 20, 9, 0, 0),
        },
    )()


class _FakeDB:
    """Async no-op DB session stand-in (no real connection)."""

    async def commit(self) -> None:  # noqa: D401
        return None

    async def refresh(self, obj: Any) -> None:
        return None

    async def rollback(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# A / B / C — ReviewRunCreateRequest deprecated field contract
# --------------------------------------------------------------------------- #
def test_a_create_request_board_field_omitted_passes():
    """A: source_board_run_id omitted -> validation PASS."""
    req = ReviewRunCreateRequest(
        trade_date="2026-08-20",
        idempotency_key="idem-A",
        # source_board_run_id omitted
    )
    assert req.source_board_run_id is None


def test_b_create_request_board_field_null_passes():
    """B: source_board_run_id = null -> validation PASS."""
    req = ReviewRunCreateRequest(
        trade_date="2026-08-20",
        source_board_run_id=None,
        idempotency_key="idem-B",
    )
    assert req.source_board_run_id is None


def test_c_create_request_board_field_uuid_rejected():
    """C: source_board_run_id = UUID -> validation error (422)."""
    with pytest.raises(ValidationError):
        ReviewRunCreateRequest(
            trade_date="2026-08-20",
            source_board_run_id=str(uuid.uuid4()),
            idempotency_key="idem-C",
        )


# --------------------------------------------------------------------------- #
# D / E / F — admin create route contract (Blocker 1 + Blocker 2 admin side)
# --------------------------------------------------------------------------- #
async def test_d_admin_route_does_not_pass_source_board_run_id(monkeypatch):
    """D (regression gate): create_run_with_result called WITHOUT
    source_board_run_id kwarg even when request omits Board ID."""
    from app.api import admin_review as ar
    from app.services.review_orchestrator_service import ReviewRunCreation

    captured: dict[str, Any] = {}

    fake_run = _make_fake_run(source_board_run_id=None)

    async def _fake_create_run_with_result(session, **kwargs):
        captured.update(kwargs)
        return ReviewRunCreation(run=fake_run, created=True)

    async def _fake_compute(db, run, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(ar, "create_run_with_result", _fake_create_run_with_result)
    monkeypatch.setattr(ar, "compute_run", _fake_compute)
    monkeypatch.setattr(ar, "resume_run", _fake_compute)

    payload = ReviewRunCreateRequest(
        trade_date="2026-08-20",
        idempotency_key="idem-D",
        # no source_board_run_id
    )

    await ar.create_review_run(payload, _FakeDB(), _FakeDB())  # noqa: SLF001

    assert "source_board_run_id" not in captured, (
        "admin route must NOT pass source_board_run_id to create_run_with_result; "
        f"got kwargs={list(captured.keys())}"
    )


async def test_e_admin_response_null_board_lineage_is_none(monkeypatch):
    """E: run.source_board_run_id = None -> response.source_board_run_id is None
    (NOT the string 'None')."""
    from app.api import admin_review as ar
    from app.services.review_orchestrator_service import ReviewRunCreation

    null_run = _make_fake_run(source_board_run_id=None)

    async def _fake_create(session, **kwargs):
        return ReviewRunCreation(run=null_run, created=True)

    async def _fake_compute(db, run, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(ar, "create_run_with_result", _fake_create)
    monkeypatch.setattr(ar, "compute_run", _fake_compute)
    monkeypatch.setattr(ar, "resume_run", _fake_compute)

    payload = ReviewRunCreateRequest(
        trade_date="2026-08-20", idempotency_key="idem-E"
    )
    resp = await ar.create_review_run(payload, _FakeDB(), _FakeDB())  # noqa: SLF001
    assert resp.source_board_run_id is None
    assert resp.source_board_run_id != "None"


async def test_f_admin_response_historical_uuid_is_string(monkeypatch):
    """F: historical UUID board lineage -> serialized as UUID string."""
    from app.api import admin_review as ar
    from app.services.review_orchestrator_service import ReviewRunCreation

    hist_id = uuid.uuid4()
    hist_run = _make_fake_run(source_board_run_id=hist_id)

    async def _fake_create(session, **kwargs):
        return ReviewRunCreation(run=hist_run, created=False)

    async def _fake_compute(db, run, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(ar, "create_run_with_result", _fake_create)
    monkeypatch.setattr(ar, "compute_run", _fake_compute)
    monkeypatch.setattr(ar, "resume_run", _fake_compute)

    payload = ReviewRunCreateRequest(
        trade_date="2026-08-20", idempotency_key="idem-F"
    )
    resp = await ar.create_review_run(payload, _FakeDB(), _FakeDB())  # noqa: SLF001
    assert resp.source_board_run_id == str(hist_id)


# --------------------------------------------------------------------------- #
# G / H — public overview serializer (Blocker 2 public side)
# --------------------------------------------------------------------------- #
async def test_g_public_overview_null_board_lineage_is_none(monkeypatch):
    """G: new run (NULL board) -> sourceBoardRunId is None (NOT 'None')."""
    from app.api import review as rv
    from app.schemas.review import ReviewOverviewResponse

    null_run = _make_fake_run(source_board_run_id=None)

    async def _fake_get_published_run(db, td, include_partial=False):
        return null_run

    async def _fake_list_facts(db, from_date, to_date):
        return []

    monkeypatch.setattr(rv, "_get_published_run", _fake_get_published_run)
    monkeypatch.setattr(rv, "list_scope_observation_facts", _fake_list_facts)

    resp = await rv.get_review_overview(  # noqa: SLF001
        "2026-08-20", include_partial=False, db=object(), ctx=object()
    )
    assert isinstance(resp, ReviewOverviewResponse)
    assert resp.sourceBoardRunId is None
    assert resp.sourceBoardRunId != "None"


async def test_h_public_overview_historical_uuid_is_string(monkeypatch):
    """H: historical UUID board lineage -> sourceBoardRunId is UUID string."""
    from app.api import review as rv
    from app.schemas.review import ReviewOverviewResponse

    hist_id = uuid.uuid4()
    hist_run = _make_fake_run(source_board_run_id=hist_id)

    async def _fake_get_published_run(db, td, include_partial=False):
        return hist_run

    async def _fake_list_facts(db, from_date, to_date):
        return []

    monkeypatch.setattr(rv, "_get_published_run", _fake_get_published_run)
    monkeypatch.setattr(rv, "list_scope_observation_facts", _fake_list_facts)

    resp = await rv.get_review_overview(  # noqa: SLF001
        "2026-08-20", include_partial=False, db=object(), ctx=object()
    )
    assert isinstance(resp, ReviewOverviewResponse)
    assert resp.sourceBoardRunId == str(hist_id)
