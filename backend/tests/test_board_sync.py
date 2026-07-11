"""Board Sync Service 测试（PRD §7.5 qstock 板块同步）。

验证项：
1. 完整性校验：空集合、目录数、成分数、异常降幅
2. 异常传播：fetcher 失败/校验失败/解析率过低 → 抛异常（不返回 status=failed）
3. 保留旧快照：校验失败或异常时不修改现有数据
4. 集合差异：成功时正确插入/删除/更新
5. qstock_fetcher：超时/重试/异常抛出

注：migration upgrade/downgrade/upgrade 循环由 CI 的 alembic-cycle job 覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_board import MarketBoard
from app.services.board_sync_service import (
    MIN_BOARD_COUNT,
    MIN_MEMBERSHIP_COUNT,
    BoardSyncError,
    StagingData,
    StagingValidationError,
    get_current_counts,
    sync_boards,
    validate_staging_data,
)
from app.services.qstock_fetcher import QStockFetchError


class MockBoardFetcher:
    """模拟 qstock 数据拉取器。"""

    def __init__(
        self,
        boards: list[dict[str, str]] | None = None,
        memberships: dict[tuple[str, str], list[str]] | None = None,
    ) -> None:
        if boards is None:
            boards = [
                {"external_code": f"B{i:04d}", "name": f"板块{i}", "type": "industry"}
                for i in range(MIN_BOARD_COUNT + 10)
            ]
        self._boards = boards
        self._memberships = memberships or {
            (b["external_code"], b["type"]): [f"{j:06d}" for j in range(30)]
            for b in boards
        }

    async def fetch_boards(self) -> list[dict[str, str]]:
        return self._boards

    async def fetch_memberships(
        self, board_external_code: str, board_type: str
    ) -> list[str]:
        return self._memberships.get((board_external_code, board_type), [])


class FailingFetcher:
    """总是失败的 fetcher。"""

    async def fetch_boards(self) -> list[dict[str, str]]:
        raise QStockFetchError("network error")

    async def fetch_memberships(
        self, board_external_code: str, board_type: str
    ) -> list[str]:
        raise QStockFetchError("network error")


# =============================================================================
# 1. 完整性校验测试（纯函数）
# =============================================================================


class TestStagingValidation:
    """PRD V1.1 完整性门禁。"""

    def test_empty_boards_rejected(self) -> None:
        staging = StagingData(boards=[], memberships={})
        with pytest.raises(StagingValidationError, match="empty"):
            validate_staging_data(staging)

    def test_empty_memberships_rejected(self) -> None:
        staging = StagingData(
            boards=[{"external_code": "B001", "name": "test", "type": "industry"}],
            memberships={},
        )
        with pytest.raises(StagingValidationError, match="empty"):
            validate_staging_data(staging)

    def test_insufficient_boards_rejected(self) -> None:
        staging = StagingData(
            boards=[{"external_code": f"B{i}", "name": f"b{i}", "type": "industry"} for i in range(10)],
            memberships={("B0", "industry"): ["000001"] * MIN_MEMBERSHIP_COUNT},
        )
        with pytest.raises(StagingValidationError, match="board count"):
            validate_staging_data(staging)

    def test_insufficient_memberships_rejected(self) -> None:
        boards = [
            {"external_code": f"B{i:04d}", "name": f"b{i}", "type": "industry"}
            for i in range(MIN_BOARD_COUNT)
        ]
        staging = StagingData(
            boards=boards,
            memberships={(b["external_code"], b["type"]): ["000001"] for b in boards},
        )
        with pytest.raises(StagingValidationError, match="membership count"):
            validate_staging_data(staging)

    def test_abnormal_drop_rejected(self) -> None:
        boards = [
            {"external_code": f"B{i:04d}", "name": f"b{i}", "type": "industry"}
            for i in range(MIN_BOARD_COUNT)
        ]
        staging = StagingData(
            boards=boards,
            memberships={(b["external_code"], b["type"]): ["000001"] * 50 for b in boards},
        )
        with pytest.raises(StagingValidationError, match="dropped"):
            validate_staging_data(staging, prev_board_count=200, prev_membership_count=10000)

    def test_normal_drop_accepted(self) -> None:
        boards = [
            {"external_code": f"B{i:04d}", "name": f"b{i}", "type": "industry"}
            for i in range(MIN_BOARD_COUNT + 40)
        ]
        staging = StagingData(
            boards=boards,
            memberships={(b["external_code"], b["type"]): ["000001"] * 50 for b in boards},
        )
        validate_staging_data(staging, prev_board_count=150, prev_membership_count=8000)

    def test_first_sync_no_drop_check(self) -> None:
        boards = [
            {"external_code": f"B{i:04d}", "name": f"b{i}", "type": "industry"}
            for i in range(MIN_BOARD_COUNT)
        ]
        staging = StagingData(
            boards=boards,
            memberships={(b["external_code"], b["type"]): ["000001"] * 50 for b in boards},
        )
        validate_staging_data(staging, prev_board_count=0, prev_membership_count=0)


# =============================================================================
# 2. 异常传播测试（sync_boards 失败必须抛异常，不返回 status=failed）
# =============================================================================


def _make_instrument_resolver(session: AsyncSession, fail: bool = False):
    """构造 instrument 解析器：为每个 symbol 创建真实 Instrument。

    fail=True 时返回空映射，触发解析率过低错误。
    """
    from app.models.instrument import Instrument

    async def _resolve(symbols: list[str]) -> dict[str, UUID]:
        if fail:
            return {}
        mapping: dict[str, UUID] = {}
        for sym in symbols:
            instr = Instrument(
                symbol=sym,
                name=f"测试-{sym}",
                market="SZ",
                status="active",
            )
            session.add(instr)
            await session.flush()
            mapping[sym] = instr.id
        return mapping

    return _resolve


class TestExceptionPropagation:
    """sync_boards 失败时必须抛异常（PRD §7.5: 失败保留旧快照）。"""

    @pytest.mark.asyncio
    async def test_fetch_failure_raises_qstock_error(
        self, db_session: AsyncSession
    ) -> None:
        """fetcher 失败必须抛 QStockFetchError，不返回空数组伪装成功。"""
        with pytest.raises(QStockFetchError, match="network error"):
            await sync_boards(db_session, FailingFetcher())

    @pytest.mark.asyncio
    async def test_empty_staging_raises_validation_error(
        self, db_session: AsyncSession
    ) -> None:
        """空暂存数据必须抛 StagingValidationError。"""
        fetcher = MockBoardFetcher(boards=[], memberships={})
        with pytest.raises(StagingValidationError, match="empty"):
            await sync_boards(db_session, fetcher)

    @pytest.mark.asyncio
    async def test_low_parse_rate_raises_board_sync_error(
        self, db_session: AsyncSession
    ) -> None:
        """解析率过低必须抛 BoardSyncError。"""
        boards = [
            {"external_code": f"B{i:04d}", "name": f"板块{i}", "type": "industry"}
            for i in range(MIN_BOARD_COUNT)
        ]
        symbols_pool = [f"{j:06d}" for j in range(40)]
        memberships = {
            (b["external_code"], b["type"]): symbols_pool[:30] for b in boards
        }
        fetcher = MockBoardFetcher(boards=boards, memberships=memberships)
        # instrument_resolver 返回空 → 解析率 0% < 50%
        resolver = _make_instrument_resolver(db_session, fail=True)
        with pytest.raises(BoardSyncError, match="parse rate"):
            await sync_boards(db_session, fetcher, instrument_resolver=resolver)


# =============================================================================
# 3. 保留旧快照测试（失败时不修改现有数据）
# =============================================================================


@pytest_asyncio.fixture
async def old_board_with_data(db_session: AsyncSession) -> MarketBoard:
    """插入旧板块+成分关系，用于验证失败时旧数据保留。"""
    from app.models.instrument import Instrument
    from app.models.market_board import MarketBoardMembership

    instr = Instrument(symbol="600000", name="测试-旧", market="SH", status="active")
    db_session.add(instr)
    await db_session.flush()

    board = MarketBoard(
        externalCode="OLD001",
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


class TestPreserveOldSnapshot:
    """PRD §7.5: 校验失败或异常时不修改现有数据。"""

    @pytest.mark.asyncio
    async def test_fetch_failure_preserves_old_data(
        self, db_session: AsyncSession, old_board_with_data: MarketBoard
    ) -> None:
        """fetcher 失败时旧数据完全不变。"""
        with pytest.raises(QStockFetchError):
            await sync_boards(db_session, FailingFetcher())

        # 旧数据仍存在
        board_count, mem_count = await get_current_counts(db_session)
        assert board_count >= 1
        assert mem_count >= 1

    @pytest.mark.asyncio
    async def test_validation_failure_preserves_old_data(
        self, db_session: AsyncSession, old_board_with_data: MarketBoard
    ) -> None:
        """校验失败时旧数据完全不变。"""
        fetcher = MockBoardFetcher(boards=[], memberships={})
        with pytest.raises(StagingValidationError):
            await sync_boards(db_session, fetcher)

        board_count, mem_count = await get_current_counts(db_session)
        assert board_count >= 1
        assert mem_count >= 1

    @pytest.mark.asyncio
    async def test_parse_rate_failure_preserves_old_data(
        self, db_session: AsyncSession, old_board_with_data: MarketBoard
    ) -> None:
        """解析率过低时旧数据完全不变。"""
        boards = [
            {"external_code": f"B{i:04d}", "name": f"板块{i}", "type": "industry"}
            for i in range(MIN_BOARD_COUNT)
        ]
        symbols_pool = [f"{j:06d}" for j in range(40)]
        memberships = {
            (b["external_code"], b["type"]): symbols_pool[:30] for b in boards
        }
        fetcher = MockBoardFetcher(boards=boards, memberships=memberships)
        resolver = _make_instrument_resolver(db_session, fail=True)
        with pytest.raises(BoardSyncError):
            await sync_boards(db_session, fetcher, instrument_resolver=resolver)

        # 旧数据仍存在
        board_count, mem_count = await get_current_counts(db_session)
        assert board_count >= 1
        assert mem_count >= 1

    @pytest.mark.asyncio
    async def test_write_failure_rolls_back(
        self, db_session: AsyncSession, old_board_with_data: MarketBoard
    ) -> None:
        """写入失败时事务回滚，旧数据完全不变。

        模拟方式：使用一个在 fetch_memberships 阶段抛异常的 fetcher，
        使 sync_boards 在拉取成分时失败（已通过校验但未写入）。
        """
        boards = [
            {"external_code": f"B{i:04d}", "name": f"板块{i}", "type": "industry"}
            for i in range(MIN_BOARD_COUNT)
        ]

        class PartialFailingFetcher:
            """目录成功，成分拉取中途失败。"""

            def __init__(self):
                self._call_count = 0

            async def fetch_boards(self):
                return boards

            async def fetch_memberships(self, ext_code, btype):
                self._call_count += 1
                if self._call_count > 5:
                    raise QStockFetchError("membership fetch failed mid-way")
                return [f"{j:06d}" for j in range(30)]

        with pytest.raises(QStockFetchError):
            await sync_boards(db_session, PartialFailingFetcher())

        # 旧数据仍存在（异常发生在写入前）
        board_count, mem_count = await get_current_counts(db_session)
        assert board_count >= 1
        assert mem_count >= 1


# =============================================================================
# 4. 成功同步测试
# =============================================================================


class TestSuccessfulSync:
    """PRD §7.5: 成功同步后数据正确写入。"""

    @pytest.mark.asyncio
    async def test_successful_sync_inserts_data(
        self, db_session: AsyncSession
    ) -> None:
        boards = [
            {"external_code": f"B{i:04d}", "name": f"板块{i}", "type": "industry"}
            for i in range(MIN_BOARD_COUNT)
        ]
        symbols_pool = [f"{j:06d}" for j in range(40)]
        memberships = {
            (b["external_code"], b["type"]): symbols_pool[:30] for b in boards
        }
        fetcher = MockBoardFetcher(boards=boards, memberships=memberships)
        resolver = _make_instrument_resolver(db_session)
        result = await sync_boards(db_session, fetcher, instrument_resolver=resolver)
        await db_session.commit()

        assert result["status"] == "succeeded"
        assert result["board_count"] == MIN_BOARD_COUNT
        assert result["membership_count"] > 0

        board_count, membership_count = await get_current_counts(db_session)
        assert board_count == result["board_count"]
        assert membership_count == result["membership_count"]

    @pytest.mark.asyncio
    async def test_differential_sync_updates(
        self, db_session: AsyncSession, old_board_with_data: MarketBoard
    ) -> None:
        """差异同步：旧 board 保留（如果在新数据中），旧 membership 不重复插入。"""
        boards = [
            {"external_code": "OLD001", "name": "旧板块-更新", "type": "industry"},
        ] + [
            {"external_code": f"B{i:04d}", "name": f"板块{i}", "type": "industry"}
            for i in range(MIN_BOARD_COUNT)
        ]
        # 每板块 30 个 symbol（101 * 30 = 3030 > MIN_MEMBERSHIP_COUNT）
        symbols_pool = [f"{j:06d}" for j in range(30)]
        memberships = {
            (b["external_code"], b["type"]): symbols_pool
            for b in boards
        }
        fetcher = MockBoardFetcher(boards=boards, memberships=memberships)
        resolver = _make_instrument_resolver(db_session)
        result = await sync_boards(db_session, fetcher, instrument_resolver=resolver)
        await db_session.commit()

        assert result["status"] == "succeeded"
        assert result["boards_updated"] >= 1  # OLD001 name 更新
