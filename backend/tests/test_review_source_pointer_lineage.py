"""AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01 — Review source lineage contract.

Pure-unit tests (mock AsyncSession, no DB/network):
    PURE_UNIT_TEST=1 python -m pytest tests/test_review_source_pointer_lineage.py -v

New contract for `_resolve_source_core_run_id`:

1. explicit source_core_run_id -> used directly, NO stock_core publication lookup,
   NO FactorPublication read at all (KPI-2/3: FactorPublication(kind=stock_core)
   reads = 0 in normal path).
2. source_core_run_id=None -> ReviewOrchestratorError (fail-closed).
   MUST NOT read stock_core pointer / FactorPublication(kind=stock_core).
3. resolver MUST NOT query PUBLICATION_KIND_STOCK_CORE / market_aggregation.
4. resolver MUST NOT session.get(BoardAnalysisRun).
5. validate_core_run: run row exists + trade_date==T + status==succeeded
   (compute-complete), no published_at / stock_core pointer check.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.models.stock_feature_snapshot_run import STATUS_SUCCEEDED
from app.services.review_orchestrator_service import (
    ReviewOrchestratorError,
    _resolve_source_core_run_id,
    validate_core_run,
)

pytestmark = pytest.mark.asyncio

TRADE_DATE = date(2026, 8, 5)


# =============================================================================
# Mock session: records publication kinds queried + BoardAnalysisRun lookups
# =============================================================================


class _FakeResult:
    def __init__(self, *, scalar: object = None) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self) -> object:
        return self._scalar


class _FakeSession:
    """Records publication kinds queried and BoardAnalysisRun lookups.

    Must NEVER be asked for stock_core / market_aggregation publication, and must
    NEVER load a BoardAnalysisRun.
    """

    def __init__(
        self,
        *,
        core_pointer: object = None,
        board_present: bool = False,
        board_status: str = "succeeded",
    ) -> None:
        self._core_pointer = core_pointer
        self._board_present = board_present
        self._board_status = board_status
        self.executed_kind: list[str] = []
        self.board_get_calls: int = 0

    async def execute(self, stmt):
        compiled = stmt.compile(compile_kwargs={"literal_binds": True}).string
        if "publication_kind" in compiled:
            # New contract: resolver must NEVER query any FactorPublication kind.
            raise AssertionError(
                f"Review source resolution MUST NOT query FactorPublication: {compiled}"
            )
        self.executed_kind.append("unknown:" + compiled[:80])
        return _FakeResult(scalar=None)

    async def get(self, model, ident):
        if model.__name__ == "BoardAnalysisRun":
            self.board_get_calls += 1
            raise AssertionError(
                "Review source resolution MUST NOT session.get(BoardAnalysisRun)"
            )
        return None


class _FakePointer:
    def __init__(self, data_run_id: uuid.UUID) -> None:
        self.data_run_id = data_run_id


def _make_pointer(data_run_id: uuid.UUID) -> _FakePointer:
    return _FakePointer(data_run_id)


# =============================================================================
# 1. explicit source_core_run_id -> used directly, no publication lookup
# =============================================================================


async def test_resolve_uses_explicit_core_run_id() -> None:
    explicit = uuid.uuid4()
    session = _FakeSession(core_pointer=None)  # even without publication
    resolved = await _resolve_source_core_run_id(
        session, TRADE_DATE, source_core_run_id=explicit,
    )
    assert resolved == explicit
    # explicit id short-circuits; no publication lookup at all
    assert session.executed_kind == []
    assert session.board_get_calls == 0


# =============================================================================
# 2. source_core_run_id=None -> fail-closed, no stock_core pointer read
# =============================================================================


async def test_resolve_requires_explicit_core_run_id() -> None:
    # Even when a stock_core pointer exists, None must NOT fall back to it.
    session = _FakeSession(core_pointer=_make_pointer(uuid.uuid4()))
    with pytest.raises(ReviewOrchestratorError) as exc:
        await _resolve_source_core_run_id(
            session, TRADE_DATE, source_core_run_id=None,
        )
    assert "source_core_run_id" in str(exc.value).lower()
    # Fail-closed: no publication query was ever issued.
    assert session.executed_kind == []
    assert session.board_get_calls == 0


async def test_resolve_none_without_pointer_rejected() -> None:
    session = _FakeSession(core_pointer=None)
    with pytest.raises(ReviewOrchestratorError):
        await _resolve_source_core_run_id(
            session, TRADE_DATE, source_core_run_id=None,
        )


# =============================================================================
# 3. resolver MUST NOT query stock_core / market_aggregation publication
# =============================================================================


async def test_resolve_forbids_publication_dependency() -> None:
    """Strongest proof: no FactorPublication query in normal resolution."""
    explicit = uuid.uuid4()
    session = _FakeSession(core_pointer=_make_pointer(uuid.uuid4()))
    await _resolve_source_core_run_id(
        session, TRADE_DATE, source_core_run_id=explicit,
    )
    assert all(
        "publication_kind" not in k for k in session.executed_kind
    ), "resolver queried FactorPublication"
    assert session.board_get_calls == 0


# =============================================================================
# 4. validate_core_run: direct StockFeatureSnapshotRun integrity (no pointer)
# =============================================================================


class _FakeCoreRun:
    def __init__(self, *, exists: bool = True, trade_date=TRADE_DATE,
                 status: str = STATUS_SUCCEEDED) -> None:
        self._exists = exists
        self.trade_date = trade_date
        self.status = status


async def test_validate_core_run_succeeded() -> None:
    core_id = uuid.uuid4()
    session = _FakeSession()
    session._resolve_target = _FakeCoreRun(exists=True, status=STATUS_SUCCEEDED)

    async def _get(model, ident):
        return session._resolve_target if ident == core_id else None

    session.get = _get  # type: ignore[assignment]
    run = await validate_core_run(
        session, core_run_id=core_id, trade_date=TRADE_DATE,
    )
    assert run is not None
    assert run.status == STATUS_SUCCEEDED


async def test_validate_core_run_missing() -> None:
    core_id = uuid.uuid4()
    session = _FakeSession()

    async def _get(model, ident):
        return None

    session.get = _get  # type: ignore[assignment]
    with pytest.raises(ReviewOrchestratorError):
        await validate_core_run(
            session, core_run_id=core_id, trade_date=TRADE_DATE,
        )


async def test_validate_core_run_trade_date_mismatch() -> None:
    core_id = uuid.uuid4()
    session = _FakeSession()
    session._resolve_target = _FakeCoreRun(
        exists=True, trade_date=date(2026, 8, 4),
    )

    async def _get(model, ident):
        return session._resolve_target

    session.get = _get  # type: ignore[assignment]
    with pytest.raises(ReviewOrchestratorError):
        await validate_core_run(
            session, core_run_id=core_id, trade_date=TRADE_DATE,
        )


async def test_validate_core_run_not_succeeded() -> None:
    core_id = uuid.uuid4()
    session = _FakeSession()
    session._resolve_target = _FakeCoreRun(exists=True, status="running")

    async def _get(model, ident):
        return session._resolve_target

    session.get = _get  # type: ignore[assignment]
    with pytest.raises(ReviewOrchestratorError):
        await validate_core_run(
            session, core_run_id=core_id, trade_date=TRADE_DATE,
        )
