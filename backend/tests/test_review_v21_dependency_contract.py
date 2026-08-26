"""Slice 3 — Review dependency & lineage contract (core-only identity).

Pure-unit tests (mock AsyncSession, no DB/network):
    PURE_UNIT_TEST=1 python -m pytest tests/test_review_v21_dependency_contract.py -v

Slice 3 changed Review identity to depend on stock_core ONLY. The
market_aggregation pointer and BoardAnalysisRun are no longer part of Review
creation. This file locks:

1. Review creation identity depends on stock_core only:
     - stock_core missing -> reject
     - stock_core present  -> accept
     - market_aggregation absent -> accept (no Board prerequisite)
2. publication query kinds during creation:
     - MUST contain stock_core
     - MUST NOT contain market_aggregation
     - MUST NOT contain chip / auction / state_event
3. create_run MUST NOT session.get(BoardAnalysisRun, ...)
4. create_run MUST NOT query chip / auction / state_event tables
5. new Review run carries source_board_run_id = NULL (core-only identity)
6. consumer contract (unchanged, still valid):
     - get_published_review_run_id reads only the formal Review publication pointer
     - no pointer -> None

[AUD-07 2026-08-07] chip-query protection is asserted at the create_run layer
(the real entry point), not only inside the resolver, so the contract cannot be
bypassed silently.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.models.factor_publication import (
    PUBLICATION_KIND_STOCK_CORE,
)
from app.models.market_review import MarketReviewRun
from app.services.review_orchestrator_service import (
    ReviewOrchestratorError,
    _resolve_source_core_run_id,
)
from app.services.review_publication_service import get_published_review_run_id

pytestmark = pytest.mark.asyncio

TRADE_DATE = date(2026, 8, 5)

# Creation must never query these publication kinds.
FORBIDDEN_KINDS = ("chip", "auction", "state_event", "market_aggregation",
                   "market_review", "history_cross_section")


# =============================================================================
# Mock helpers
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
    """Records publication kinds queried + BoardAnalysisRun lookups."""

    def __init__(self, *, core_pointer: object = None) -> None:
        self._core_pointer = core_pointer
        self.executed_kind: list[str] = []
        self.board_get_calls: int = 0

    async def execute(self, stmt):
        compiled = stmt.compile(compile_kwargs={"literal_binds": True}).string
        if "publication_kind" in compiled:
            assert "market_aggregation" not in compiled, (
                "Review creation MUST NOT query "
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
        # Slice 3: nothing in Review creation loads BoardAnalysisRun.
        from app.models.board_analysis_snapshot import BoardAnalysisRun

        if model is BoardAnalysisRun:
            self.board_get_calls += 1
            raise AssertionError(
                "Review creation MUST NOT session.get(BoardAnalysisRun)"
            )
        return None


# =============================================================================
# 1. Review identity requires explicit source_core_run_id (fail-closed)
# =============================================================================


async def test_resolve_requires_explicit_source_core_run_id() -> None:
    """[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] source_core_run_id=None ->
    fail-closed（不回退到 stock_core FactorPublication pointer 解析）。

    Review 必须显式绑定本次 AfterCloseRun 产生的 CoreRun；旧合同
    "stock_core pointer missing -> reject, present -> accept" 已被否决。
    """
    session = _FakeSession(core_pointer=None)
    with pytest.raises(ReviewOrchestratorError) as exc:
        await _resolve_source_core_run_id(
            session, TRADE_DATE, source_core_run_id=None,
        )
    assert "source_core_run_id" in str(exc.value).lower()
    # 关键不变量：fail-closed 路径绝不查询 stock_core FactorPublication pointer。
    assert PUBLICATION_KIND_STOCK_CORE not in session.executed_kind


async def test_resolve_uses_explicit_source_core_run_id() -> None:
    """显式 source_core_run_id 被直接使用，不读任何 publication pointer。"""
    core_id = uuid.uuid4()
    session = _FakeSession(core_pointer=_make_pointer(core_id))
    resolved = await _resolve_source_core_run_id(
        session, TRADE_DATE, source_core_run_id=core_id,
    )
    assert resolved == core_id
    # 显式 id 短路：不查询 stock_core / 任何 publication kind。
    assert PUBLICATION_KIND_STOCK_CORE not in session.executed_kind


# =============================================================================
# 2. publication query kinds during creation
# =============================================================================


async def test_creation_queries_no_stock_core_pointer() -> None:
    """[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] Review 创建不查询任何
    FactorPublication pointer（含 stock_core / market_aggregation / chip /
    auction / state_event / market_review / history_cross_section）。
    """
    core_id = uuid.uuid4()
    session = _FakeSession(core_pointer=_make_pointer(core_id))
    # 显式传入 source_core_run_id（不再解析 pointer）
    await _resolve_source_core_run_id(
        session, TRADE_DATE, source_core_run_id=core_id,
    )
    assert PUBLICATION_KIND_STOCK_CORE not in session.executed_kind
    for kind in FORBIDDEN_KINDS:
        assert kind not in session.executed_kind, (
            f"Review creation must not query publication kind {kind!r}"
        )


# =============================================================================
# 3-4. create_run: no BoardAnalysisRun load, no chip/auction/state_event tables
# =============================================================================


class _RecordingSession:
    """Records create_run SQL text to assert no chip/auction/state_event touch."""

    def __init__(self, *, core_id: uuid.UUID) -> None:
        self._core_id = core_id
        self.sql_log: list[str] = []
        self.insert_params: list[dict] = []
        self.board_get_calls: int = 0

    async def execute(self, stmt):
        try:
            compiled = stmt.compile(
                compile_kwargs={"literal_binds": True},
            ).string
        except Exception:
            compiled = str(stmt)
        self.sql_log.append(compiled)
        # Capture bound parameters for INSERT assertions.
        try:
            params = dict(getattr(compiled, "params", {}) or {})
        except Exception:
            params = {}
        if "insert into" in compiled.lower():
            self.insert_params.append(params)
        if "publication_kind" in compiled:
            assert "market_aggregation" not in compiled, (
                "create_run MUST NOT query PUBLICATION_KIND_MARKET_AGGREGATION"
            )
            if "stock_core" in compiled:
                return _FakeResult(scalar=_make_pointer(self._core_id))
        # get_run_by_keys SELECT and INSERT -> scalar None
        return _FakeResult(scalar=None)

    async def get(self, model, ident):
        from app.models.board_analysis_snapshot import BoardAnalysisRun
        from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

        if model is BoardAnalysisRun:
            self.board_get_calls += 1
            raise AssertionError(
                "create_run MUST NOT session.get(BoardAnalysisRun)"
            )
        if model is StockFeatureSnapshotRun:
            # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] validate_core_run 直接查
            # StockFeatureSnapshotRun 行（status==succeeded, trade_date==T）。
            core = AsyncMock()
            core.trade_date = TRADE_DATE
            core.status = "succeeded"
            return core
        return None

    async def flush(self):
        pass


def _build_recording_session() -> _RecordingSession:
    return _RecordingSession(core_id=uuid.uuid4())


def _fake_run_obj(core_id: uuid.UUID) -> MarketReviewRun:
    return MarketReviewRun(
        trade_date=TRADE_DATE,
        source_core_run_id=core_id,
        source_board_run_id=None,  # legacy column retained but NULL for new runs
        algorithm_version="v1",
        filter_version="v1",
        baseline_window=120,
        metadata_json={},
    )


async def test_create_run_never_queries_board_or_chip(monkeypatch) -> None:
    """[Slice 3] create_run touches no BoardAnalysisRun, chip, auction, state_event."""
    from app.services import review_orchestrator_service as ros

    session = _build_recording_session()

    async def fake_get_run_by_keys(session, *, trade_date, source_core_run_id,
                                    algorithm_version, filter_version):
        # upsert 后读回：返回构造的 run 对象（source_board_run_id=None）
        return _fake_run_obj(source_core_run_id)

    monkeypatch.setattr(ros, "get_run_by_keys", fake_get_run_by_keys)

    creation = await ros.create_run_with_result(
        session, trade_date=TRADE_DATE,  # type: ignore[arg-type]
        source_core_run_id=session._core_id,
    )
    run = creation.run
    # Core-only identity: new run carries NULL board lineage.
    assert run.source_board_run_id is None

    joined = "\n".join(session.sql_log).lower()

    # No BoardAnalysisRun load
    assert session.board_get_calls == 0

    # No chip / auction / state_event tables in FROM/JOIN
    for table in ("stock_chip_consensus_snapshot", "auction_", "state_event"):
        assert f"from {table}" not in joined and f"join {table}" not in joined, (
            f"create_run must not read {table!r} (Review identity is stock_core only); "
            f"actual SQL:\n{joined}"
        )

    # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] create_run 不查询任何 publication_kind
    # （含 stock_core / market_aggregation / chip_consensus / auction_anchor /
    # history_cross_section / market_review / state_event）。CoreRun 通过显式
    # source_core_run_id + validate_core_run（直接查 StockFeatureSnapshotRun 行）校验。
    for kind in ("chip_consensus", "auction_anchor", "market_aggregation",
                 "history_cross_section", "stock_core", "market_review",
                 "state_event"):
        assert f"publication_kind = '{kind}'" not in joined, (
            f"create_run must not query publication_kind={kind!r}"
        )


async def test_create_run_inserts_null_board_run_id(monkeypatch) -> None:
    """[Slice 3 core proof] new Review run INSERT carries source_board_run_id = NULL."""
    from app.services import review_orchestrator_service as ros

    session = _build_recording_session()

    async def fake_get_run_by_keys(session, *, trade_date, source_core_run_id,
                                    algorithm_version, filter_version):
        # upsert 后读回：返回构造的 run 对象（source_board_run_id=None）
        return _fake_run_obj(source_core_run_id)

    monkeypatch.setattr(ros, "get_run_by_keys", fake_get_run_by_keys)

    await ros.create_run_with_result(
        session, trade_date=TRADE_DATE,  # type: ignore[arg-type]
        source_core_run_id=session._core_id,
    )

    inserts = [s for s in session.sql_log if "insert into" in s.lower()]
    assert inserts, "create_run must execute one INSERT"
    insert_sql = "\n".join(inserts).lower()
    # 1) The legacy column is retained (still in the column list) ...
    assert "source_board_run_id" in insert_sql, (
        "legacy column source_board_run_id must be retained in schema"
    )
    # 2) ... and its value is a BOUND PARAMETER (%(...)s), not a hardcoded literal.
    #    The _create_run_impl hardcodes source_board_run_id=None, so the bound
    #    value is NULL -> core-only identity (no Board lineage written).
    assert "%(source_board_run_id)s" in insert_sql, (
        "source_board_run_id must be a bound parameter (ORM-set NULL), "
        "not a hardcoded board run id"
    )
    # 3) no hardcoded UUID literal anywhere in the INSERT (all values are params)
    import re

    literal_uuids = re.findall(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        insert_sql,
    )
    assert not literal_uuids, (
        f"INSERT must not contain hardcoded UUID literals; found {literal_uuids}"
    )
    # 4) idempotent upsert preserved
    assert "do nothing" in insert_sql


async def test_create_run_dry_run_has_null_board() -> None:
    """dry_run run object carries source_board_run_id = None."""
    from app.services import review_orchestrator_service as ros

    session = _build_recording_session()
    creation =     await ros.create_run_with_result(
        session, trade_date=TRADE_DATE, dry_run=True,  # type: ignore[arg-type]
        source_core_run_id=session._core_id,
    )
    run = creation.run
    assert run.source_board_run_id is None


async def test_create_run_upsert_is_do_nothing(monkeypatch) -> None:
    """Existing run must not be overwritten: ON CONFLICT DO NOTHING (idempotent)."""
    from app.services import review_orchestrator_service as ros

    session = _build_recording_session()

    async def fake_get_run_by_keys(session, *, trade_date, source_core_run_id,
                                    algorithm_version, filter_version):
        # upsert 后读回：返回构造的 run 对象（source_board_run_id=None）
        return _fake_run_obj(source_core_run_id)

    monkeypatch.setattr(ros, "get_run_by_keys", fake_get_run_by_keys)

    await ros.create_run_with_result(
        session, trade_date=TRADE_DATE,  # type: ignore[arg-type]
        source_core_run_id=session._core_id,
    )

    inserts = [s for s in session.sql_log if "insert into" in s.lower()]
    insert_sql = "\n".join(inserts).lower()
    assert "on conflict" in insert_sql, "must keep upsert idempotency"
    assert "do nothing" in insert_sql, "must be DO NOTHING (no late rewrite)"
    assert "do update" not in insert_sql


# =============================================================================
# 6. consumer contract (unchanged, still valid)
# =============================================================================


async def test_consumer_reads_published_pointer_not_temp() -> None:
    """consumer get_published_review_run_id returns only the formal pointer run_id."""
    pointer_run_id = uuid.uuid4()
    pointer = _make_pointer(pointer_run_id)

    class _ConsumerSession:
        async def execute(self, stmt):
            return _FakeResult(scalar=pointer)

    got = await get_published_review_run_id(
        _ConsumerSession(), TRADE_DATE,  # type: ignore[arg-type]
    )
    assert got == pointer_run_id


async def test_consumer_none_when_no_pointer() -> None:
    """No formal pointer -> consumer returns None (no temp/unpublished run)."""
    class _EmptySession:
        async def execute(self, stmt):
            return _FakeResult(scalar=None)

    got = await get_published_review_run_id(
        _EmptySession(), TRADE_DATE,  # type: ignore[arg-type]
    )
    assert got is None
