"""Board Sync Service 测试（PRD §7.5 重构：问财原子快照同步）。

验证项：
1. 绝对门禁：原始行数、代码唯一率、行业数、概念数、关系数、解析率
2. 相对门禁：股票/行业/概念/关系下降 >20% 拒绝
3. 原子切换：成功时正确 upsert/delete，失败时 rollback 保留旧数据
4. 幂等性：重复同步相同数据不产生重复
5. Redis 状态记录

注：真实问财拉取测试不进入 CI，只在部署后执行一次。
DB 集成测试通过 mock validate_snapshot 绕过绝对门禁（需 5000+ 股票），
门禁逻辑由纯函数测试覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_board import MarketBoard, MarketBoardMembership
from app.services.board_sync_service import (
    MIN_CONCEPT_COUNT,
    MIN_INDUSTRY_COUNT,
    MIN_RAW_ROWS,
    MIN_RELATION_COUNT,
    BoardSyncError,
    StagingValidationError,
    get_current_counts,
    get_current_detailed_counts,
    sync_boards,
    validate_snapshot,
)
from app.services.wencai_board_provider import BoardSnapshot

# =============================================================================
# 辅助函数：构造测试用 BoardSnapshot
# =============================================================================


def _make_valid_snapshot(
    num_stocks: int = 5500,
    concepts_per_stock: int = 12,
    num_industries: int = 257,
    num_concepts: int = 388,
) -> BoardSnapshot:
    """构造能通过绝对门禁的 BoardSnapshot。

    默认参数接近生产基线：5537股、257行业、388概念、69737概念关系。
    raw_rows = num_stocks，每股唯一 → code_uniqueness_rate = 1.0
    """
    boards: list[dict[str, str]] = []
    memberships: dict[tuple[str, str], list[str]] = {}

    # 生成行业 boards
    for i in range(num_industries):
        name = f"行业{i}-子类{i % 10}"
        ext_code = f"wc:i:industry_{i:04d}"
        boards.append({"external_code": ext_code, "name": name, "type": "industry"})
        memberships[(ext_code, "industry")] = []

    # 生成概念 boards
    for i in range(num_concepts):
        ext_code = f"wc:c:concept_{i:04d}"
        boards.append({"external_code": ext_code, "name": f"概念{i}", "type": "concept"})
        memberships[(ext_code, "concept")] = []

    # 生成股票及其板块归属
    for stock_idx in range(num_stocks):
        symbol = f"{600000 + stock_idx:06d}"

        # 每股分配一个行业（轮询）
        industry_idx = stock_idx % num_industries
        industry_key = (f"wc:i:industry_{industry_idx:04d}", "industry")
        memberships[industry_key].append(symbol)

        # 每股分配多个概念（轮询）
        for c in range(concepts_per_stock):
            concept_idx = (stock_idx * concepts_per_stock + c) % num_concepts
            concept_key = (f"wc:c:concept_{concept_idx:04d}", "concept")
            memberships[concept_key].append(symbol)

    return BoardSnapshot(
        boards=boards,
        memberships=memberships,
        raw_rows=num_stocks,
        unresolved_symbols=[],
    )


def _make_small_snapshot(
    num_stocks: int = 50,
    num_industries: int = MIN_INDUSTRY_COUNT,
    num_concepts: int = MIN_CONCEPT_COUNT,
) -> BoardSnapshot:
    """构造小规模 snapshot（用于 DB 测试，mock validate_snapshot 后使用）。

    raw_rows 设为 num_stocks，但通过 mock 绕过绝对门禁。
    """
    boards: list[dict[str, str]] = []
    memberships: dict[tuple[str, str], list[str]] = {}

    for i in range(num_industries):
        ext_code = f"wc:i:small_ind_{i:04d}"
        boards.append({"external_code": ext_code, "name": f"行业{i}", "type": "industry"})
        memberships[(ext_code, "industry")] = []

    for i in range(num_concepts):
        ext_code = f"wc:c:small_con_{i:04d}"
        boards.append({"external_code": ext_code, "name": f"概念{i}", "type": "concept"})
        memberships[(ext_code, "concept")] = []

    for stock_idx in range(num_stocks):
        symbol = f"{600000 + stock_idx:06d}"
        industry_idx = stock_idx % num_industries
        memberships[(f"wc:i:small_ind_{industry_idx:04d}", "industry")].append(symbol)
        for c in range(3):
            concept_idx = (stock_idx * 3 + c) % num_concepts
            memberships[(f"wc:c:small_con_{concept_idx:04d}", "concept")].append(symbol)

    return BoardSnapshot(
        boards=boards,
        memberships=memberships,
        raw_rows=num_stocks,
        unresolved_symbols=[],
    )


def _mock_stats(snapshot: BoardSnapshot) -> dict:
    """构造 mock stats（绕过绝对门禁，用于 DB 测试）。"""
    industry_count = sum(1 for b in snapshot.boards if b["type"] == "industry")
    concept_count = sum(1 for b in snapshot.boards if b["type"] == "concept")
    all_symbols = set()
    for symbols in snapshot.memberships.values():
        all_symbols.update(symbols)
    return {
        "raw_rows": max(snapshot.raw_rows, MIN_RAW_ROWS),
        "industry_count": industry_count,
        "concept_count": concept_count,
        "board_count": snapshot.board_count,
        "relation_count": snapshot.membership_count,
        "unique_stock_count": len(all_symbols),
        "total_symbol_refs": snapshot.membership_count,
        "code_uniqueness_rate": 1.0,
        "unresolved_count": 0,
    }


# =============================================================================
# 1. 绝对门禁测试（纯函数）
# =============================================================================


class TestValidateSnapshotAbsolute:
    """绝对门禁校验测试。"""

    def test_valid_snapshot_passes(self) -> None:
        snapshot = _make_valid_snapshot()
        stats = validate_snapshot(snapshot)
        assert stats["raw_rows"] >= MIN_RAW_ROWS
        assert stats["industry_count"] >= MIN_INDUSTRY_COUNT
        assert stats["concept_count"] >= MIN_CONCEPT_COUNT
        assert stats["relation_count"] >= MIN_RELATION_COUNT
        assert stats["code_uniqueness_rate"] >= 0.999

    def test_raw_rows_below_minimum_rejected(self) -> None:
        """raw_rows < 5000 拒绝。"""
        snapshot = BoardSnapshot(
            boards=[{"external_code": "wc:i:b0", "name": "b", "type": "industry"}],
            memberships={("wc:i:b0", "industry"): ["000001"]},
            raw_rows=MIN_RAW_ROWS - 1,
        )
        with pytest.raises(StagingValidationError, match="raw rows"):
            validate_snapshot(snapshot)

    def test_industry_below_minimum_rejected(self) -> None:
        """行业数 < 200 拒绝。"""
        snapshot = _make_valid_snapshot(num_industries=MIN_INDUSTRY_COUNT - 1)
        with pytest.raises(StagingValidationError, match="industry count"):
            validate_snapshot(snapshot)

    def test_concept_below_minimum_rejected(self) -> None:
        """概念数 < 300 拒绝。"""
        snapshot = _make_valid_snapshot(num_concepts=MIN_CONCEPT_COUNT - 1)
        with pytest.raises(StagingValidationError, match="concept count"):
            validate_snapshot(snapshot)

    def test_relation_below_minimum_rejected(self) -> None:
        """关系数 < 60000 拒绝（其它门禁均通过）。"""
        # 5000 唯一股票 → code_uniqueness_rate=1.0；200行业+300概念通过；
        # 每股仅 1 行业 + 1 概念 → 10000 关系 < 60000
        num_stocks = MIN_RAW_ROWS
        boards: list[dict[str, str]] = []
        memberships: dict[tuple[str, str], list[str]] = {}
        for i in range(MIN_INDUSTRY_COUNT):
            ext = f"wc:i:b{i:04d}"
            boards.append({"external_code": ext, "name": f"b{i}", "type": "industry"})
            memberships[(ext, "industry")] = []
        for i in range(MIN_CONCEPT_COUNT):
            ext = f"wc:c:b{i:04d}"
            boards.append({"external_code": ext, "name": f"c{i}", "type": "concept"})
            memberships[(ext, "concept")] = []
        for s_idx in range(num_stocks):
            sym = f"{s_idx:06d}"
            memberships[(f"wc:i:b{s_idx % MIN_INDUSTRY_COUNT:04d}", "industry")].append(sym)
            memberships[(f"wc:c:b{s_idx % MIN_CONCEPT_COUNT:04d}", "concept")].append(sym)
        snapshot = BoardSnapshot(
            boards=boards,
            memberships=memberships,
            raw_rows=num_stocks,
        )
        with pytest.raises(StagingValidationError, match="relation count"):
            validate_snapshot(snapshot)


# =============================================================================
# 2. 相对门禁测试（纯函数）
# =============================================================================


class TestValidateSnapshotRelative:
    """相对门禁校验测试。"""

    def test_normal_drop_accepted(self) -> None:
        """下降 ≤20% 接受。"""
        snapshot = _make_valid_snapshot(num_stocks=5000)
        validate_snapshot(
            snapshot,
            prev_stock_count=6000,
            prev_industry_count=300,
            prev_concept_count=450,
            prev_relation_count=80000,
        )

    def test_stock_drop_over_20_percent_rejected(self) -> None:
        """股票数下降 >20% 拒绝（snapshot 通过绝对门禁）。"""
        # 默认 5500 股票通过绝对门禁；prev=8000 → drop=31.25% > 20%
        snapshot = _make_valid_snapshot(num_stocks=5500)
        with pytest.raises(StagingValidationError, match="stock count dropped"):
            validate_snapshot(snapshot, prev_stock_count=8000)

    def test_industry_drop_over_20_percent_rejected(self) -> None:
        snapshot = _make_valid_snapshot(num_industries=200)
        with pytest.raises(StagingValidationError, match="industry count dropped"):
            validate_snapshot(snapshot, prev_industry_count=300)

    def test_concept_drop_over_20_percent_rejected(self) -> None:
        snapshot = _make_valid_snapshot(num_concepts=300)
        with pytest.raises(StagingValidationError, match="concept count dropped"):
            validate_snapshot(snapshot, prev_concept_count=400)

    def test_first_sync_no_drop_check(self) -> None:
        """首次同步（prev=0）不检查相对门禁。"""
        snapshot = _make_valid_snapshot()
        validate_snapshot(
            snapshot,
            prev_stock_count=0,
            prev_industry_count=0,
            prev_concept_count=0,
            prev_relation_count=0,
        )


# =============================================================================
# 3. 原子切换测试（需要 DB，mock validate_snapshot 绕过绝对门禁）
# =============================================================================


def _make_instrument_resolver(session: AsyncSession, fail: bool = False):
    """构造 instrument 解析器：先查已存在，缺失的才创建（幂等）。

    fail=True 时返回空映射，触发解析率过低错误。
    """
    from sqlalchemy import select

    async def _resolve(symbols: list[str]) -> dict[str, UUID]:
        if fail:
            return {}
        # 先查已存在的 instrument（幂等：重复同步不重复创建）
        result = await session.execute(
            select(Instrument.id, Instrument.symbol).where(Instrument.symbol.in_(symbols))
        )
        mapping: dict[str, UUID] = {row.symbol: row.id for row in result}
        # 只为缺失的 symbol 创建新 Instrument
        for sym in symbols:
            if sym not in mapping:
                instr = Instrument(
                    symbol=sym,
                    name=f"测试-{sym}",
                    market="SH",
                    status="active",
                )
                session.add(instr)
                await session.flush()
                mapping[sym] = instr.id
        return mapping

    return _resolve


@pytest_asyncio.fixture
async def old_board_with_data(db_session: AsyncSession) -> MarketBoard:
    """插入旧板块+成分关系，用于验证失败时旧数据保留。"""
    instr = Instrument(symbol="600000", name="测试-旧", market="SH", status="active")
    db_session.add(instr)
    await db_session.flush()

    board = MarketBoard(
        externalCode="wc:i:old_001",
        name="旧板块",
        type="industry",
        updatedAt=datetime.now(UTC),
    )
    db_session.add(board)
    await db_session.flush()

    mem = MarketBoardMembership(
        boardId=board.id,
        instrumentId=instr.id,
        updatedAt=datetime.now(UTC),
    )
    db_session.add(mem)
    await db_session.commit()
    await db_session.refresh(board)
    return board


class TestAtomicSwitch:
    """原子切换测试（mock validate_snapshot 绕过绝对门禁）。"""

    @pytest.mark.asyncio
    async def test_successful_sync_inserts_data(self, db_session: AsyncSession) -> None:
        """成功同步后数据正确写入。"""
        snapshot = _make_small_snapshot(num_stocks=50)
        resolver = _make_instrument_resolver(db_session)

        with patch("app.services.board_sync_service.validate_snapshot", return_value=_mock_stats(snapshot)):
            with patch("app.services.board_sync_service.get_current_detailed_counts", return_value={
                "board_count": 0, "membership_count": 0,
                "industry_count": 0, "concept_count": 0, "stock_count": 0,
            }):
                result = await sync_boards(db_session, snapshot, instrument_resolver=resolver)
        await db_session.commit()

        assert result["status"] == "succeeded"
        assert result["industry_count"] >= MIN_INDUSTRY_COUNT
        assert result["concept_count"] >= MIN_CONCEPT_COUNT
        assert result["membership_count"] > 0

        board_count, membership_count = await get_current_counts(db_session)
        assert board_count == result["board_count"]
        assert membership_count == result["membership_count"]

    @pytest.mark.asyncio
    async def test_validation_failure_preserves_old_data(
        self, db_session: AsyncSession, old_board_with_data: MarketBoard
    ) -> None:
        """校验失败时旧数据完全不变（不 mock，让真实 validate_snapshot 拒绝）。"""
        snapshot = _make_small_snapshot(num_stocks=10)
        with pytest.raises(StagingValidationError):
            await sync_boards(db_session, snapshot)

        board_count, mem_count = await get_current_counts(db_session)
        assert board_count >= 1
        assert mem_count >= 1

    @pytest.mark.asyncio
    async def test_low_parse_rate_raises_error(
        self, db_session: AsyncSession, old_board_with_data: MarketBoard
    ) -> None:
        """解析率过低抛 BoardSyncError。"""
        snapshot = _make_small_snapshot(num_stocks=50)
        resolver = _make_instrument_resolver(db_session, fail=True)

        with patch("app.services.board_sync_service.validate_snapshot", return_value=_mock_stats(snapshot)):
            with patch("app.services.board_sync_service.get_current_detailed_counts", return_value={
                "board_count": 0, "membership_count": 0,
                "industry_count": 0, "concept_count": 0, "stock_count": 0,
            }):
                with pytest.raises(BoardSyncError, match="parse rate"):
                    await sync_boards(db_session, snapshot, instrument_resolver=resolver)

        # 旧数据仍存在
        board_count, mem_count = await get_current_counts(db_session)
        assert board_count >= 1
        assert mem_count >= 1

    @pytest.mark.asyncio
    async def test_differential_sync_idempotent(self, db_session: AsyncSession) -> None:
        """重复同步相同数据不产生重复（幂等）。"""
        snapshot = _make_small_snapshot(num_stocks=50)
        resolver = _make_instrument_resolver(db_session)

        mock_stats = _mock_stats(snapshot)
        mock_counts = {
            "board_count": 0, "membership_count": 0,
            "industry_count": 0, "concept_count": 0, "stock_count": 0,
        }

        with patch("app.services.board_sync_service.validate_snapshot", return_value=mock_stats):
            with patch("app.services.board_sync_service.get_current_detailed_counts", return_value=mock_counts):
                await sync_boards(db_session, snapshot, instrument_resolver=resolver)
                await db_session.commit()

        board_count_1, mem_count_1 = await get_current_counts(db_session)

        # 第二次同步（相同数据，prev_counts 非零）
        mock_counts_2 = {
            "board_count": board_count_1, "membership_count": mem_count_1,
            "industry_count": mock_stats["industry_count"],
            "concept_count": mock_stats["concept_count"],
            "stock_count": 50,
        }
        with patch("app.services.board_sync_service.validate_snapshot", return_value=mock_stats):
            with patch("app.services.board_sync_service.get_current_detailed_counts", return_value=mock_counts_2):
                await sync_boards(db_session, snapshot, instrument_resolver=resolver)
                await db_session.commit()

        board_count_2, mem_count_2 = await get_current_counts(db_session)

        assert board_count_1 == board_count_2
        assert mem_count_1 == mem_count_2

    @pytest.mark.asyncio
    async def test_differential_sync_updates_name(
        self, db_session: AsyncSession, old_board_with_data: MarketBoard
    ) -> None:
        """差异同步：旧 board name 更新。"""
        snapshot = _make_small_snapshot(num_stocks=50)
        # 显式添加与 fixture 同 external_code 的 board，name 改为"旧板块-更新"
        snapshot.boards.append({
            "external_code": "wc:i:old_001",
            "name": "旧板块-更新",
            "type": "industry",
        })
        snapshot.memberships[("wc:i:old_001", "industry")] = ["600000"]

        resolver = _make_instrument_resolver(db_session)

        with patch("app.services.board_sync_service.validate_snapshot", return_value=_mock_stats(snapshot)):
            with patch("app.services.board_sync_service.get_current_detailed_counts", return_value={
                "board_count": 1, "membership_count": 1,
                "industry_count": 1, "concept_count": 0, "stock_count": 1,
            }):
                result = await sync_boards(db_session, snapshot, instrument_resolver=resolver)
                await db_session.commit()

        assert result["boards_updated"] >= 1  # old_001 name 更新

    @pytest.mark.asyncio
    async def test_sync_boards_returns_source_wencai(
        self, db_session: AsyncSession
    ) -> None:
        """CHANGE-20260716-007：sync_boards 返回 dict 应包含 source=wencai。

        防止手工调用 record_sync_status(result) 时丢失 source 字段。
        """
        snapshot = _make_small_snapshot(num_stocks=20)
        resolver = _make_instrument_resolver(db_session)

        with patch("app.services.board_sync_service.validate_snapshot", return_value=_mock_stats(snapshot)):
            with patch("app.services.board_sync_service.get_current_detailed_counts", return_value={
                "board_count": 0, "membership_count": 0,
                "industry_count": 0, "concept_count": 0, "stock_count": 0,
            }):
                result = await sync_boards(db_session, snapshot, instrument_resolver=resolver)
        await db_session.commit()

        # source 字段必须存在且为 wencai（即使手工调用 record_sync_status 也能带上）
        assert result["source"] == "wencai"
        assert result["status"] == "succeeded"


# =============================================================================
# 4. 详细计数测试
# =============================================================================


class TestGetCurrentDetailedCounts:
    """get_current_detailed_counts 测试。"""

    @pytest.mark.asyncio
    async def test_empty_db_returns_zeros(self, db_session: AsyncSession) -> None:
        counts = await get_current_detailed_counts(db_session)
        assert counts["board_count"] == 0
        assert counts["membership_count"] == 0
        assert counts["industry_count"] == 0
        assert counts["concept_count"] == 0
        assert counts["stock_count"] == 0
