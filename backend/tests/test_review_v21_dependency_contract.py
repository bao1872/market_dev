"""[Commit F · Review V2.1] Review 依赖与血统合同测试。

纯单元测试（mock AsyncSession），无需数据库：
    PURE_UNIT_TEST=1 python -m pytest tests/test_review_v21_dependency_contract.py -v

锁定 Review V2.1 不变量（PRD §11 / 用户 Commit F 边界）：
1. Review 只依赖 stock_core + board_aggregation 两个正式 publication pointer；
2. 不等待 chip：chip 缺失只降级（degraded_reasons），不阻塞 run 创建；
3. 不等待 auction：创建阶段不查询任何 auction publication kind；
4. 创建阶段只执行两类 publication 查询（stock_core / market_aggregation），
   不得额外查询 chip / auction / state_event 等其他 kind；
5. exact lineage：board run 必须与 stock_core pointer 同源
   （board_run.source_core_run_id == resolved core id）、同日、status=succeeded；
6. consumer 只读发布结果（publication pointer 指向的 run），不读临时表。
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.models.board_analysis_snapshot import BoardAnalysisRun
from app.models.factor_publication import (
    PUBLICATION_KIND_MARKET_AGGREGATION,
    PUBLICATION_KIND_STOCK_CORE,
)
from app.services.review_orchestrator_service import (
    ReviewOrchestratorError,
    _resolve_source_run_ids,
)

pytestmark = pytest.mark.asyncio

TRADE_DATE = date(2026, 8, 5)

# 创建阶段禁止查询的其他 publication kind（确保 Review 不等待 chip/auction）
FORBIDDEN_KINDS = ("chip", "auction", "state_event", "market_review", "history_cross_section")


# =============================================================================
# Mock 工具
# =============================================================================


class _FakeResult:
    def __init__(self, *, scalar: object = None) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self) -> object:
        return self._scalar

    def scalar(self) -> object:
        return self._scalar


def _make_pointer(data_run_id: uuid.UUID) -> AsyncMock:
    pub = AsyncMock()
    pub.data_run_id = data_run_id
    return pub


class _FakeSession:
    """记录执行的 SQL 语句与 publication kind，返回预置 pointer 序列。"""

    def __init__(
        self,
        *,
        core_pointer: object = None,
        board_pointer: object = None,
        board_run: BoardAnalysisRun | None = None,
    ) -> None:
        self._core_pointer = core_pointer
        self._board_pointer = board_pointer
        self._board_run = board_run
        self.executed_kind: list[str] = []
        self.get_calls: list[tuple[type, object]] = []

    async def execute(self, stmt):
        # 从 SQL 文本提取 publication_kind 字面量，用于断言查询种类
        compiled = stmt.compile(compile_kwargs={"literal_binds": True}).string
        if "publication_kind" in compiled and "stock_core" in compiled:
            self.executed_kind.append(PUBLICATION_KIND_STOCK_CORE)
            return _FakeResult(scalar=self._core_pointer)
        if "publication_kind" in compiled and "market_aggregation" in compiled:
            self.executed_kind.append(PUBLICATION_KIND_MARKET_AGGREGATION)
            return _FakeResult(scalar=self._board_pointer)
        self.executed_kind.append("unknown:" + compiled[:80])
        return _FakeResult(scalar=None)

    async def get(self, model, ident):
        self.get_calls.append((model, ident))
        return self._board_run


def _make_board_run(
    *,
    source_core_run_id: uuid.UUID,
    status: str = "succeeded",
) -> BoardAnalysisRun:
    return BoardAnalysisRun(
        id=uuid.uuid4(),
        trade_date=TRADE_DATE,
        source_core_run_id=source_core_run_id,
        status=status,
    )


# =============================================================================
# 1-3. 只依赖 stock_core + board_aggregation（不等待 chip / auction）
# =============================================================================


async def test_resolve_requires_stock_core_pointer() -> None:
    """board pointer 存在但 stock_core pointer 缺失 → 拒绝创建（不静默降级 core）。"""
    board_id = uuid.uuid4()
    session = _FakeSession(
        core_pointer=None,
        board_pointer=_make_pointer(board_id),
        board_run=_make_board_run(source_core_run_id=uuid.uuid4()),
    )
    with pytest.raises(ReviewOrchestratorError) as exc:
        await _resolve_source_run_ids(
            session, TRADE_DATE,
            source_core_run_id=None, source_board_run_id=None,
        )
    assert "stock_core pointer" in str(exc.value)


async def test_resolve_requires_board_pointer() -> None:
    """stock_core pointer 存在但 board_aggregation pointer 缺失 → 拒绝创建。"""
    core_id = uuid.uuid4()
    session = _FakeSession(
        core_pointer=_make_pointer(core_id),
        board_pointer=None,
        board_run=None,
    )
    with pytest.raises(ReviewOrchestratorError) as exc:
        await _resolve_source_run_ids(
            session, TRADE_DATE,
            source_core_run_id=None, source_board_run_id=None,
        )
    assert "board_analysis pointer" in str(exc.value)


async def test_resolve_queries_only_core_and_board_kinds() -> None:
    """创建阶段只查询 stock_core + market_aggregation，不得查询 chip/auction。"""
    core_id = uuid.uuid4()
    board_id = uuid.uuid4()
    session = _FakeSession(
        core_pointer=_make_pointer(core_id),
        board_pointer=_make_pointer(board_id),
        board_run=_make_board_run(source_core_run_id=core_id),
    )
    await _resolve_source_run_ids(
        session, TRADE_DATE,
        source_core_run_id=None, source_board_run_id=None,
    )

    assert set(session.executed_kind) == {
        PUBLICATION_KIND_STOCK_CORE,
        PUBLICATION_KIND_MARKET_AGGREGATION,
    }, f"Review 创建只能依赖 core+board，实际查询: {session.executed_kind}"
    for kind in FORBIDDEN_KINDS:
        assert kind not in session.executed_kind, (
            f"Review 创建不得查询 {kind}（不等待 chip/auction）"
        )


# =============================================================================
# 4-5. exact lineage：board 与 stock_core 必须同源、同日、succeeded
# =============================================================================


async def test_resolve_rejects_board_mismatched_source_core() -> None:
    """board run 的 source_core_run_id 与 stock_core pointer 不同源 → 拒绝。"""
    core_id = uuid.uuid4()
    board_id = uuid.uuid4()
    session = _FakeSession(
        core_pointer=_make_pointer(core_id),
        board_pointer=_make_pointer(board_id),
        board_run=_make_board_run(source_core_run_id=uuid.uuid4()),  # 不同源
    )
    with pytest.raises(ReviewOrchestratorError) as exc:
        await _resolve_source_run_ids(
            session, TRADE_DATE,
            source_core_run_id=None, source_board_run_id=None,
        )
    assert "不同源" in str(exc.value)


async def test_resolve_rejects_board_wrong_trade_date() -> None:
    """board run 的 trade_date 与 Review 不一致 → 拒绝。"""
    core_id = uuid.uuid4()
    board_id = uuid.uuid4()
    board_run = _make_board_run(source_core_run_id=core_id)
    board_run.trade_date = date(2026, 8, 4)  # 与 TRADE_DATE 不同
    session = _FakeSession(
        core_pointer=_make_pointer(core_id),
        board_pointer=_make_pointer(board_id),
        board_run=board_run,
    )
    with pytest.raises(ReviewOrchestratorError) as exc:
        await _resolve_source_run_ids(
            session, TRADE_DATE,
            source_core_run_id=None, source_board_run_id=None,
        )
    assert "trade_date" in str(exc.value)


async def test_resolve_rejects_board_not_succeeded() -> None:
    """board run 状态非 succeeded → 拒绝（不基于未完成 batch 出复盘）。"""
    core_id = uuid.uuid4()
    board_id = uuid.uuid4()
    session = _FakeSession(
        core_pointer=_make_pointer(core_id),
        board_pointer=_make_pointer(board_id),
        board_run=_make_board_run(
            source_core_run_id=core_id, status="failed",
        ),
    )
    with pytest.raises(ReviewOrchestratorError) as exc:
        await _resolve_source_run_ids(
            session, TRADE_DATE,
            source_core_run_id=None, source_board_run_id=None,
        )
    assert "非 ready" in str(exc.value)


async def test_resolve_success_exact_lineage() -> None:
    """全通过：返回 (core_id, board_id)，且 board 与 core 精确同源。"""
    core_id = uuid.uuid4()
    board_id = uuid.uuid4()
    board_run = _make_board_run(
        source_core_run_id=core_id, status="succeeded",
    )
    board_run.id = board_id
    session = _FakeSession(
        core_pointer=_make_pointer(core_id),
        board_pointer=_make_pointer(board_id),
        board_run=board_run,
    )
    resolved_core, resolved_board = await _resolve_source_run_ids(
        session, TRADE_DATE,
        source_core_run_id=None, source_board_run_id=None,
    )
    assert resolved_core == core_id
    assert resolved_board == board_id
    # 确认通过 session.get 读取 BoardAnalysisRun（exact lineage 校验的数据源）
    loaded = [m for m, _id in session.get_calls if m is BoardAnalysisRun]
    assert loaded, "必须通过 session.get 读取 BoardAnalysisRun 做 lineage 校验"


# =============================================================================
# 6. consumer 只读发布结果，不读临时表
# =============================================================================


async def test_consumer_reads_published_pointer_not_temp() -> None:
    """普通用户 _get_published_run 只返回 publication pointer 指向的 run。"""
    from app.api.review import _get_published_run

    pointer_run_id = uuid.uuid4()
    pointer = _make_pointer(pointer_run_id)
    published_run = AsyncMock()
    published_run.id = pointer_run_id

    class _ConsumerSession:
        async def execute(self, stmt):
            return _FakeResult(scalar=pointer)

        async def get(self, model, ident):
            assert ident == pointer_run_id
            return published_run

    got = await _get_published_run(
        _ConsumerSession(), TRADE_DATE, include_partial=False,
    )
    assert got is published_run
    assert got.id == pointer_run_id


async def test_consumer_404_when_no_pointer() -> None:
    """无正式 pointer → 普通用户 404（不读临时/未发布 run）。"""
    from fastapi import HTTPException

    from app.api.review import _get_published_run

    class _EmptySession:
        async def execute(self, stmt):
            return _FakeResult(scalar=None)

    with pytest.raises(HTTPException) as exc:
        await _get_published_run(
            _EmptySession(), TRADE_DATE, include_partial=False,
        )
    assert exc.value.status_code == 404
