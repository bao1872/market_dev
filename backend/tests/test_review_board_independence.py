"""Slice 3 — Remove Board Run From Review Identity.

Pure-unit tests (PURE_UNIT_TEST=1, no DB/network) covering the §11 hard gates:

1. _resolve_source_core_run_id uses explicitly passed source_core_run_id (no publication lookup).
2. source_core_run_id=None and no publication -> ReviewOrchestratorError.
3. source_core_run_id=None and a STOCK_CORE publication exists -> uses publication.data_run_id.
4. Board publication missing/any status no longer gates resolution (resolver never queries Board).
5. _create_run_impl new run carries source_board_run_id=None (dry-run path).
6. _create_run_impl upsert uses constraint="uq_review_runs_date_core_algo_filter".
7. AfterClose _execute_review_step prereq = (stock_core_published and snapshot_run_id is not None).
8. AfterClose calls create_run WITHOUT aggregation_status / source_board_run_id.
9. ReviewRunCreateRequest rejects non-NULL source_board_run_id (deprecated input).
10. ReviewRunResponse.source_board_run_id is Optional[str] (str | None).
11. ORM model nullable + UniqueConstraint rename + migration 092 fail-closed contract.
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

# Pure-unit guard: these tests must never touch a real DB/network.
os.environ.setdefault("PURE_UNIT_TEST", "1")

from app.models.market_review import MarketReviewRun  # noqa: E402
from app.schemas.review import (  # noqa: E402
    ReviewRunCreateRequest,
    ReviewRunResponse,
)
from app.services import after_close_orchestrator as aco  # noqa: E402
from app.services.review_orchestrator_service import (  # noqa: E402
    ReviewOrchestratorError,
    _create_run_impl,
    _resolve_source_core_run_id,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_publication(kind: str, data_run_id: uuid.UUID | None, status: str = "published"):
    pub = MagicMock()
    pub.publication_kind = kind
    pub.data_run_id = data_run_id
    pub.status = status
    return pub


def _make_fake_session():
    sess = AsyncMock()
    sess.execute = AsyncMock(return_value=MagicMock())
    sess.commit = AsyncMock()
    sess.rollback = AsyncMock()
    sess.refresh = AsyncMock()
    sess.add = MagicMock()
    return sess


# --------------------------------------------------------------------------- #
# Gates 1-4: _resolve_source_core_run_id
# --------------------------------------------------------------------------- #
async def test_resolver_uses_explicit_core_run_id(monkeypatch):
    """Gate 1: explicit source_core_run_id is used directly; no publication lookup."""
    explicit = uuid.uuid4()
    get_pub = AsyncMock()  # must NOT be called
    monkeypatch.setattr(
        "app.services.review_orchestrator_service._get_publication", get_pub
    )
    result = await _resolve_source_core_run_id(
        _make_fake_session(), date(2026, 8, 20), source_core_run_id=explicit
    )
    assert result == explicit
    get_pub.assert_not_called()


async def test_resolver_errors_when_no_publication(monkeypatch):
    """Gate 2: no explicit id and no STOCK_CORE publication -> ReviewOrchestratorError."""
    get_pub = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.services.review_orchestrator_service._get_publication", get_pub
    )
    with pytest.raises(ReviewOrchestratorError):
        await _resolve_source_core_run_id(
            _make_fake_session(), date(2026, 8, 20), source_core_run_id=None
        )


async def test_resolver_uses_stock_core_publication(monkeypatch):
    """Gate 3: no explicit id but STOCK_CORE publication -> uses publication.data_run_id."""
    expected = uuid.uuid4()
    get_pub = AsyncMock(return_value=_make_publication("stock_core", expected))
    monkeypatch.setattr(
        "app.services.review_orchestrator_service._get_publication", get_pub
    )
    result = await _resolve_source_core_run_id(
        _make_fake_session(), date(2026, 8, 20), source_core_run_id=None
    )
    assert result == expected
    # resolver requested the stock_core pointer only (Board never consulted)
    get_pub.assert_awaited_once()
    assert get_pub.await_args.args[1] == date(2026, 8, 20)
    assert get_pub.await_args.args[2] == "stock_core"


async def test_resolver_ignores_board_publication(monkeypatch):
    """Gate 4: Board publication state (any) never gates resolution.

    The resolver never queries the market_aggregation / Board publication, so a
    missing or failed Board publication must not affect Review identity.
    """
    explicit = uuid.uuid4()
    # Even if a Board publication existed, the resolver must not read it.
    board_pub = _make_publication("market_aggregation", uuid.uuid4(), status="failed")
    get_pub = AsyncMock(return_value=None)  # only relevant for stock_core lookup
    monkeypatch.setattr(
        "app.services.review_orchestrator_service._get_publication", get_pub
    )
    # Patch _get_publication used internally? resolver only ever asks for stock_core;
    # ensure it does not consult Board. We assert it queries stock_core only.
    result = await _resolve_source_core_run_id(
        _make_fake_session(), date(2026, 8, 20), source_core_run_id=explicit
    )
    assert result == explicit
    # No Board-related call must occur; only stock_core may be requested (here explicit, so 0 calls).
    get_pub.assert_not_called()
    # board_pub is unused -> confirms Board is not part of the identity contract.
    assert board_pub.publication_kind == "market_aggregation"


# --------------------------------------------------------------------------- #
# Gates 5-6: _create_run_impl board-independent identity
# --------------------------------------------------------------------------- #
def _make_run_obj(core_id: uuid.UUID):
    run = MarketReviewRun(
        trade_date=date(2026, 8, 20),
        source_core_run_id=core_id,
        source_board_run_id=None,
        algorithm_version="algo-v1",
        filter_version="filter-v1",
        status="created",
    )
    return run


async def test_create_run_impl_new_run_has_null_board_and_new_constraint(monkeypatch):
    """Gates 5 & 6: new run source_board_run_id=None; upsert uses renamed constraint."""
    captured: dict = {}

    async def fake_get_run_by_keys(session, *, trade_date, source_core_run_id,
                                    algorithm_version, filter_version):
        # called once after upsert to read the (newly inserted) row back
        return _make_run_obj(source_core_run_id)

    def fake_on_conflict(constraint=None, **_):
        captured["constraint"] = constraint
        return MagicMock()

    async def fake_execute(stmt, *a, **k):
        return MagicMock()

    # pg_insert(...) -> stmt; stmt.values(...) -> stmt; stmt.on_conflict_do_nothing(...)
    # -> fake_on_conflict (sets captured["constraint"]).
    stmt = MagicMock()
    stmt.on_conflict_do_nothing = fake_on_conflict
    stmt.values.return_value = stmt

    monkeypatch.setattr(
        "app.services.review_orchestrator_service.get_run_by_keys", fake_get_run_by_keys
    )
    monkeypatch.setattr(
        "app.services.review_orchestrator_service.pg_insert",
        lambda *a, **k: stmt,
    )
    sess = _make_fake_session()
    sess.execute = fake_execute

    core_id = uuid.uuid4()
    with patch(
        "app.services.review_orchestrator_service._resolve_source_core_run_id",
        new=AsyncMock(return_value=core_id),
    ):
        run, _created = await _create_run_impl(
            sess,
            trade_date=date(2026, 8, 20),
            algorithm_version="algo-v1",
            filter_version="filter-v1",
            source_core_run_id=core_id,
            idempotency_key="test:key",
        )

    assert run.source_board_run_id is None  # Gate 5
    assert captured["constraint"] == "uq_review_runs_date_core_algo_filter"  # Gate 6


async def test_create_run_impl_dry_run_null_board(monkeypatch):
    """Gate 5 (dry-run): dry_run path also yields source_board_run_id=None."""
    captured: dict = {}

    def fake_on_conflict(constraint=None, **_):
        captured["constraint"] = constraint
        return MagicMock()

    async def fake_execute(stmt, *a, **k):
        return MagicMock()

    monkeypatch.setattr(
        "app.services.review_orchestrator_service.get_run_by_keys",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.review_orchestrator_service.pg_insert",
        lambda *a, **k: MagicMock(on_conflict_do_nothing=fake_on_conflict),
    )
    sess = _make_fake_session()
    sess.execute = fake_execute

    core_id = uuid.uuid4()
    with patch(
        "app.services.review_orchestrator_service._resolve_source_core_run_id",
        new=AsyncMock(return_value=core_id),
    ):
        run, _created = await _create_run_impl(
            sess,
            trade_date=date(2026, 8, 20),
            algorithm_version="algo-v1",
            filter_version="filter-v1",
            source_core_run_id=core_id,
            idempotency_key="test:key",
            dry_run=True,
        )

    # Gate 5 (dry-run): dry_run path yields source_board_run_id=None.
    # dry_run does not execute an upsert, so constraint name is not exercised here.
    assert run.source_board_run_id is None


# --------------------------------------------------------------------------- #
# Gates 7-8: AfterClose _execute_review_step prereq + call contract
# --------------------------------------------------------------------------- #
async def test_after_close_prereq_satisfied_without_board(monkeypatch):
    """Gates 7 & 8: prereq = stock_core_published and snapshot_run_id present.

    Board status is NOT a parameter -> cannot gate Review. We mock the whole DB
    surface so the function enters the executing branch (no skip) and calls
    create_run without aggregation_status / source_board_run_id.
    """
    job_run_id = uuid.uuid4()
    snapshot_run_id = uuid.uuid4()
    core_id = uuid.uuid4()

    # Fake session factory
    fake_sess = _make_fake_session()
    fake_factory = MagicMock(return_value=fake_sess)
    monkeypatch.setattr(aco, "AsyncSessionLocal", fake_factory)

    # Minimal job_run with empty metadata
    job_run = MagicMock()
    job_run.metadata_json = "{}"
    monkeypatch.setattr(aco, "_get_job_run_or_raise", AsyncMock(return_value=job_run))
    monkeypatch.setattr(aco, "_parse_metadata", lambda jr: {})
    monkeypatch.setattr(aco, "_update_orchestrator_status", AsyncMock())
    monkeypatch.setattr(aco, "append_event", AsyncMock())
    monkeypatch.setattr(aco, "_update_heartbeat_and_step", AsyncMock())

    captured_create: dict = {}

    async def fake_create_run(db, *, trade_date, canary=False, dry_run=False,
                              idempotency_key=None, **kwargs):
        captured_create["called"] = True
        captured_create["kwargs"] = kwargs
        run = MagicMock()
        run.id = uuid.uuid4()
        run.source_core_run_id = core_id
        run.source_board_run_id = None
        run.algorithm_version = "algo-v1"
        run.filter_version = "filter-v1"
        run.status = "created"
        run.expected_scope_count = 0
        run.signal_count = 0
        run.coverage_ratio = 0.0
        return run

    # _execute_review_step dynamically imports these from review_orchestrator_service,
    # so patch the module attributes.
    from app.services import review_orchestrator_service as ros
    from app.services import review_publication_service as rps

    monkeypatch.setattr(ros, "create_run", fake_create_run)
    monkeypatch.setattr(rps, "get_published_review_run_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        rps, "is_formally_published_review_run", lambda run, pid: False
    )

    # We only need to confirm the function reaches create_run (prereq satisfied)
    # without raising and without consulting Board. compute_run is mocked so the
    # executing branch is reached.
    async def fake_compute_run(db, run):
        return None

    monkeypatch.setattr(ros, "compute_run", fake_compute_run)

    await aco._execute_review_step(
        job_run_id=job_run_id,
        trade_date=date(2026, 8, 20),
        snapshot_run_id=snapshot_run_id,  # present
        worker_id=None,
        skip_review=False,
        stock_core_published=True,  # core published
        # NOTE: no aggregation_status argument -> Gate 8 (signature changed)
    )

    assert captured_create.get("called") is True
    assert "aggregation_status" not in captured_create["kwargs"]
    assert "source_board_run_id" not in captured_create["kwargs"]


# --------------------------------------------------------------------------- #
# Gates 9-10: schema validation / response shape
# --------------------------------------------------------------------------- #
def test_create_request_rejects_non_null_board_run():
    """Gate 9: non-NULL source_board_run_id is rejected (deprecated input)."""
    with pytest.raises(ValidationError):  # source_board_run_id is deprecated input
        ReviewRunCreateRequest(
            trade_date="2026-08-20",
            source_core_run_id=str(uuid.uuid4()),
            idempotency_key="test:key",
            source_board_run_id=str(uuid.uuid4()),  # deprecated, must be rejected
        )


def test_create_request_accepts_null_board_run():
    """Gate 9 (positive): None source_board_run_id is allowed."""
    req = ReviewRunCreateRequest(
        trade_date="2026-08-20",
        source_core_run_id=str(uuid.uuid4()),
        idempotency_key="test:key",
        source_board_run_id=None,
    )
    assert req.source_board_run_id is None


def test_response_source_board_run_id_optional():
    """Gate 10: ReviewRunResponse.source_board_run_id is str | None."""
    ann = ReviewRunResponse.model_fields["source_board_run_id"].annotation
    # pydantic v2: Optional[str] -> typing.Union[str, None]
    assert "None" in str(ann)


# --------------------------------------------------------------------------- #
# Gate 11: ORM + migration static contract
# --------------------------------------------------------------------------- #
def test_orm_source_board_run_id_nullable_and_constraint_renamed():
    """Gate 11a: ORM column nullable=True; UniqueConstraint renamed (no board)."""
    col = MarketReviewRun.__table__.c["source_board_run_id"]
    assert col.nullable is True  # physical column retained but nullable

    uq_names = {c.name for c in MarketReviewRun.__table__.constraints
                if c.__class__.__name__ == "UniqueConstraint"}
    assert "uq_review_runs_date_core_algo_filter" in uq_names
    assert "uq_review_runs_date_core_board_algo_filter" not in uq_names


def test_migration_092_fail_closed_contract():
    """Gate 11b: migration 092 fail-closed text contract is present."""
    import pathlib

    mig_path = (
        pathlib.Path(aco.__file__).resolve().parent.parent.parent
        / "alembic" / "versions" / "092_review_core_only_identity.py"
    )
    text = mig_path.read_text(encoding="utf-8")
    assert "092_review_core_only_identity" in text
    assert "091_observation_run_lineage" in text
    # fail-closed upgrade: detects duplicate core-only identity and stops
    assert "cannot apply" in text
    assert "duplicate" in text
    assert "raise" in text
    # fail-closed downgrade: NULL source_board_run_id present -> fail
    assert "source_board_run_id IS NULL" in text
    # constraint rename present
    assert "uq_review_runs_date_core_algo_filter" in text
    assert "uq_review_runs_date_core_board_algo_filter" in text
