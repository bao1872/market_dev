"""Slice 3 — Review core-only source lineage contract.

Pure-unit tests (mock AsyncSession, no DB/network):
    PURE_UNIT_TEST=1 python -m pytest tests/test_review_source_pointer_lineage.py -v

Slice 3 removed the Board Analysis dependency from Review identity. Review
source resolution now depends ONLY on stock_core (or an explicitly supplied
source_core_run_id). The market_aggregation pointer and BoardAnalysisRun are no
longer part of Review identity.

This file locks the NEW contract for `_resolve_source_core_run_id`:

1. stock_core pointer present  -> returns pointer.data_run_id
2. stock_core pointer missing  -> ReviewOrchestratorError
3. explicit source_core_run_id -> used directly, NO market_aggregation lookup
4. market_aggregation pointer missing + stock_core present -> SUCCESS
   (most important inverse regression guard for Slice 3)
5. Board run present in any status (failed/partial/succeeded) does NOT affect
   resolver behavior
6. resolver MUST NOT:
     - query PUBLICATION_KIND_MARKET_AGGREGATION
     - session.get(BoardAnalysisRun, ...)
7. publication query kind set is exactly {stock_core}
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.models.board_analysis_snapshot import BoardAnalysisRun
from app.models.factor_publication import PUBLICATION_KIND_STOCK_CORE
from app.services.review_orchestrator_service import (
    ReviewOrchestratorError,
    _resolve_source_core_run_id,
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


def _make_pointer(data_run_id: uuid.UUID) -> AsyncMock:
    pub = AsyncMock()
    pub.data_run_id = data_run_id
    pub.status = "published"
    return pub


class _FakeSession:
    """Records publication kinds queried and BoardAnalysisRun lookups."""

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
            # Slice 3: resolver must only ever ask for stock_core.
            assert "market_aggregation" not in compiled, (
                "Review source resolution MUST NOT query "
                "PUBLICATION_KIND_MARKET_AGGREGATION"
            )
            assert "stock_core" in compiled, (
                f"unexpected publication_kind query: {compiled}"
            )
            self.executed_kind.append(PUBLICATION_KIND_STOCK_CORE)
            return _FakeResult(scalar=self._core_pointer)
        self.executed_kind.append("unknown:" + compiled[:80])
        return _FakeResult(scalar=None)

    async def get(self, model, ident):
        if model is BoardAnalysisRun:
            self.board_get_calls += 1
            raise AssertionError(
                "Review source resolution MUST NOT session.get(BoardAnalysisRun)"
            )
        return None


# =============================================================================
# 1. stock_core present -> use pointer.data_run_id
# =============================================================================


async def test_resolve_uses_stock_core_pointer() -> None:
    core_id = uuid.uuid4()
    session = _FakeSession(core_pointer=_make_pointer(core_id))
    resolved = await _resolve_source_core_run_id(
        session, TRADE_DATE, source_core_run_id=None,
    )
    assert resolved == core_id
    assert PUBLICATION_KIND_STOCK_CORE in session.executed_kind
    assert session.board_get_calls == 0


# =============================================================================
# 2. stock_core missing -> reject
# =============================================================================


async def test_resolve_requires_stock_core_pointer() -> None:
    session = _FakeSession(core_pointer=None)
    with pytest.raises(ReviewOrchestratorError) as exc:
        await _resolve_source_core_run_id(
            session, TRADE_DATE, source_core_run_id=None,
        )
    assert "stock_core" in str(exc.value).lower()


# =============================================================================
# 3. explicit source_core_run_id -> used directly, no publication lookup
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
# 4. market_aggregation missing + stock_core present -> SUCCESS
#    (core inverse regression guard for Slice 3)
# =============================================================================


async def test_resolve_succeeds_without_market_aggregation() -> None:
    core_id = uuid.uuid4()
    # No board pointer, no BoardAnalysisRun referenced anywhere.
    session = _FakeSession(core_pointer=_make_pointer(core_id))
    resolved = await _resolve_source_core_run_id(
        session, TRADE_DATE, source_core_run_id=None,
    )
    assert resolved == core_id
    # The only publication kind ever queried is stock_core.
    assert session.executed_kind == [PUBLICATION_KIND_STOCK_CORE]
    assert session.board_get_calls == 0


# =============================================================================
# 5. Board run in any status does NOT affect resolver
# =============================================================================


@pytest.mark.parametrize("board_status", ["failed", "partial", "succeeded", "skipped"])
async def test_resolve_ignores_board_run_status(board_status: str) -> None:
    core_id = uuid.uuid4()
    session = _FakeSession(
        core_pointer=_make_pointer(core_id),
        board_present=True,
        board_status=board_status,
    )
    resolved = await _resolve_source_core_run_id(
        session, TRADE_DATE, source_core_run_id=None,
    )
    assert resolved == core_id
    assert session.board_get_calls == 0


# =============================================================================
# 6. resolver MUST NOT query market_aggregation or BoardAnalysisRun
# =============================================================================


async def test_resolve_forbids_board_dependency_graph() -> None:
    """Strongest Slice 3 proof: Board is NOT in the dependency graph."""
    core_id = uuid.uuid4()
    session = _FakeSession(core_pointer=_make_pointer(core_id))
    await _resolve_source_core_run_id(
        session, TRADE_DATE, source_core_run_id=None,
    )
    # No market_aggregation publication query
    assert all(
        "market_aggregation" not in k for k in session.executed_kind
    ), "resolver queried market_aggregation publication"
    # No BoardAnalysisRun entity load
    assert session.board_get_calls == 0
    # Exactly one publication kind queried: stock_core
    assert set(session.executed_kind) == {PUBLICATION_KIND_STOCK_CORE}
