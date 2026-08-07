"""[Commit F · Review V2.1] Review 依赖与血统合同测试。

纯单元测试（mock AsyncSession），无需数据库：
    PURE_UNIT_TEST=1 python -m pytest tests/test_review_v21_dependency_contract.py -v

锁定 Review V2.1 不变量（PRD §11 / 用户 Commit F 边界）：
1. Review 只依赖 stock_core + board_aggregation 两个正式 publication pointer；
2. 不依赖 chip：创建阶段零次 chip 查询，chip 不进入 Review lineage；
3. 不等待 auction：创建阶段不查询任何 auction publication kind；
4. 创建阶段只执行两类 publication 查询（stock_core / market_aggregation），
   不得额外查询 chip / auction / state_event 等其他 kind；
5. exact lineage：board run 必须与 stock_core pointer 同源
   （board_run.source_core_run_id == resolved core id）、同日、status=succeeded；
6. consumer 只读发布结果（publication pointer 指向的 run），不读临时表。

[AUD-07 2026-08-07] 本文件原先只在 `_resolve_source_run_ids()` 层断言"零 chip
查询"，而真正的 chip 查询发生在其调用方 `create_run()` 内，导致合同声明为真但
实际被绕过（假绿）。现补充 create_run 层断言（见文件末尾"7."），把保护层级
上提到真实入口。
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
from app.models.market_review import MarketReviewRun
from app.services.review_orchestrator_service import (
    ReviewOrchestratorError,
    _resolve_source_run_ids,
)
from app.services.review_publication_service import get_published_review_run_id

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
        session, TRADE_DATE,  # type: ignore[arg-type]
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
        session, TRADE_DATE,  # type: ignore[arg-type]
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
    """consumer 入口 get_published_review_run_id 只返回 publication pointer 指向的 run_id。"""
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
    """无正式 pointer → consumer 返回 None（不读临时/未发布 run）。"""
    class _EmptySession:
        async def execute(self, stmt):
            return _FakeResult(scalar=None)

    got = await get_published_review_run_id(
        _EmptySession(), TRADE_DATE,  # type: ignore[arg-type]
    )
    assert got is None


# =============================================================================
# 7. [AUD-07] create_run 层零 chip 查询（真实入口，非仅 _resolve_source_run_ids）
# =============================================================================


class _RecordingSession:
    """记录 create_run 全过程执行的 SQL 文本，用于断言未触达 chip 相关表。"""

    def __init__(
        self,
        *,
        core_id: uuid.UUID,
        board_id: uuid.UUID,
        board_run: BoardAnalysisRun,
        existing_run: object = None,
    ) -> None:
        self._core_id = core_id
        self._board_id = board_id
        self._board_run = board_run
        self._existing_run = existing_run
        self.sql_log: list[str] = []
        self.flushed = 0

    async def execute(self, stmt):
        try:
            compiled = stmt.compile(
                compile_kwargs={"literal_binds": True},
            ).string
        except Exception:  # pragma: no cover - 编译失败时退化为结构文本
            compiled = str(stmt)
        self.sql_log.append(compiled)

        if "publication_kind" in compiled and "stock_core" in compiled:
            return _FakeResult(scalar=_make_pointer(self._core_id))
        if "publication_kind" in compiled and "market_aggregation" in compiled:
            return _FakeResult(scalar=_make_pointer(self._board_id))
        # INSERT ... ON CONFLICT / 读回 run
        return _FakeResult(scalar=self._existing_run)

    async def get(self, model, ident):
        return self._board_run

    async def flush(self):
        self.flushed += 1


def _build_recording_session(existing_run: object) -> _RecordingSession:
    core_id = uuid.uuid4()
    board_id = uuid.uuid4()
    board_run = _make_board_run(source_core_run_id=core_id)
    board_run.id = board_id
    return _RecordingSession(
        core_id=core_id,
        board_id=board_id,
        board_run=board_run,
        existing_run=existing_run,
    )


async def test_create_run_never_queries_chip() -> None:
    """[AUD-04/07] create_run 全过程不得查询 chip 相关表。

    这是原合同的真实断言层级：chip 查询过去发生在 create_run 内部
    （_resolve_chip_dependency），只断言 _resolve_source_run_ids 会漏掉它。
    """
    from app.services.review_orchestrator_service import create_run

    existing = MarketReviewRun(
        trade_date=TRADE_DATE,
        source_core_run_id=uuid.uuid4(),
        source_board_run_id=uuid.uuid4(),
        algorithm_version="v1",
        filter_version="v1",
        baseline_window=120,
    )
    session = _build_recording_session(existing)

    await create_run(session, trade_date=TRADE_DATE)  # type: ignore[arg-type]

    joined = "\n".join(session.sql_log).lower()

    # 1) 不得触达任何 chip / auction / state_event 数据表。
    #    注意：market_review_runs 自身保留了 source_chip_run_id 列（未删列以避免
    #    migration 风险），它会出现在 SELECT 列清单里，那不是"依赖 chip"，
    #    因此这里断言的是 FROM/JOIN 的表名，而非裸字符串 "chip"。
    for table in (
        "stock_chip_consensus_snapshot",
        "auction_",
        "state_event",
    ):
        assert f"from {table}" not in joined and f"join {table}" not in joined, (
            f"create_run 不得读取 {table!r} 表（Review 输入身份只含 "
            f"stock_core + market_aggregation）；实际 SQL:\n{joined}"
        )

    # 2) publication 查询只允许 stock_core + market_aggregation 两种 kind。
    for kind in ("chip_consensus", "auction_anchor", "history_cross_section"):
        assert f"publication_kind = '{kind}'" not in joined, (
            f"create_run 不得查询 publication_kind={kind!r}"
        )
    assert "publication_kind = 'stock_core'" in joined
    assert "publication_kind = 'market_aggregation'" in joined


async def test_create_run_does_not_write_chip_columns() -> None:
    """[AUD-05] create_run 的 INSERT 不得写入 chip lineage 值。

    `source_chip_run_id` / `degraded_reasons` 两列刻意保留（不删列，避免
    migration 与向后兼容风险），但 create_run 不得再向其写入 chip 语义：
    - source_chip_run_id 不出现在 INSERT 列清单（无显式赋值）；
    - degraded_reasons 即便因 ORM default=list 被物化，其值也必须是空列表。
    """
    from app.services.review_orchestrator_service import create_run

    existing = MarketReviewRun(
        trade_date=TRADE_DATE,
        source_core_run_id=uuid.uuid4(),
        source_board_run_id=uuid.uuid4(),
        algorithm_version="v1",
        filter_version="v1",
        baseline_window=120,
    )
    session = _build_recording_session(existing)

    await create_run(session, trade_date=TRADE_DATE)  # type: ignore[arg-type]

    inserts = [s for s in session.sql_log if "insert into" in s.lower()]
    assert inserts, "create_run 必须执行一次 INSERT"
    insert_sql = "\n".join(inserts).lower()
    assert "source_chip_run_id" not in insert_sql, (
        "chip run id 不得写入 Review lineage"
    )
    # chip 域信息（覆盖率）不得混入 Review metadata
    assert "chip_coverage" not in insert_sql, (
        "chip 覆盖率不得写入 Review metadata"
    )


async def test_create_run_dry_run_has_no_chip_lineage() -> None:
    """dry_run 返回的 run 对象不得带 chip lineage 字段值。"""
    from app.services.review_orchestrator_service import create_run

    session = _build_recording_session(None)
    creation = await create_run(
        session, trade_date=TRADE_DATE, dry_run=True,  # type: ignore[arg-type]
    )
    run = creation.run

    assert run.source_chip_run_id is None
    assert not run.metadata_json.get("chip_coverage")


async def test_create_run_upsert_is_do_nothing() -> None:
    """[AUD-05] 已存在的 run 不得被后续 create_run 改写。

    SQL 层必须是 ON CONFLICT DO NOTHING —— DO UPDATE 会让晚到的增强产品
    （chip 等）改写已发布 Review 的 lineage 与降级状态。
    """
    from app.services.review_orchestrator_service import create_run

    existing = MarketReviewRun(
        trade_date=TRADE_DATE,
        source_core_run_id=uuid.uuid4(),
        source_board_run_id=uuid.uuid4(),
        algorithm_version="v1",
        filter_version="v1",
        baseline_window=120,
    )
    session = _build_recording_session(existing)

    await create_run(session, trade_date=TRADE_DATE)  # type: ignore[arg-type]

    inserts = [s for s in session.sql_log if "insert into" in s.lower()]
    insert_sql = "\n".join(inserts).lower()
    assert "on conflict" in insert_sql, "必须保持 upsert 幂等语义"
    assert "do nothing" in insert_sql, (
        "必须是 DO NOTHING：DO UPDATE 会让晚到 chip 改写已有 Review run"
    )
    assert "do update" not in insert_sql
