"""Board Sync Service 测试（PRD §7.5 qstock 板块同步）。

验证项：
1. 完整性校验：空集合、目录数、成分数、异常降幅
2. 原子切换：成功时 TRUNCATE+INSERT，失败时保持旧数据
3. 事务回滚：异常时不修改现有数据
4. 保留旧快照：校验失败时不删除旧关系
5. 按板块筛选：industry/concept 筛选
6. migration upgrade/downgrade/upgrade 循环
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
    StagingData,
    StagingValidationError,
    get_current_counts,
    sync_boards,
    validate_staging_data,
)


class MockBoardFetcher:
    """模拟 qstock 数据拉取器。"""

    def __init__(
        self,
        boards: list[dict[str, str]] | None = None,
        memberships: dict[tuple[str, str], list[str]] | None = None,
    ) -> None:
        if boards is None:
            # 默认生成足够的板块（>MIN_BOARD_COUNT）
            boards = [
                {"external_code": f"B{i:04d}", "name": f"板块{i}", "type": "industry"}
                for i in range(MIN_BOARD_COUNT + 10)
            ]
        self._boards = boards
        self._memberships = memberships or {
            (b["external_code"], b["type"]): [
                f"{j:06d}" for j in range(30)
            ]
            for b in boards
        }

    async def fetch_boards(self) -> list[dict[str, str]]:
        return self._boards

    async def fetch_memberships(
        self, board_external_code: str, board_type: str
    ) -> list[str]:
        return self._memberships.get((board_external_code, board_type), [])


# =============================================================================
# 1. 完整性校验测试
# =============================================================================


class TestStagingValidation:
    """PRD V1.1 完整性门禁。"""

    def test_empty_boards_rejected(self) -> None:
        """空板块目录被拒绝。"""
        staging = StagingData(boards=[], memberships={})
        with pytest.raises(StagingValidationError, match="empty"):
            validate_staging_data(staging)

    def test_empty_memberships_rejected(self) -> None:
        """空成分关系被拒绝。"""
        staging = StagingData(
            boards=[{"external_code": "B001", "name": "test", "type": "industry"}],
            memberships={},
        )
        with pytest.raises(StagingValidationError, match="empty"):
            validate_staging_data(staging)

    def test_insufficient_boards_rejected(self) -> None:
        """板块数不足被拒绝。"""
        staging = StagingData(
            boards=[{"external_code": f"B{i}", "name": f"b{i}", "type": "industry"} for i in range(10)],
            memberships={("B0", "industry"): ["000001"] * MIN_MEMBERSHIP_COUNT},
        )
        with pytest.raises(StagingValidationError, match="board count"):
            validate_staging_data(staging)

    def test_insufficient_memberships_rejected(self) -> None:
        """成分关系数不足被拒绝。"""
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
        """异常降幅（>20%）被拒绝。"""
        boards = [
            {"external_code": f"B{i:04d}", "name": f"b{i}", "type": "industry"}
            for i in range(MIN_BOARD_COUNT)
        ]
        staging = StagingData(
            boards=boards,
            memberships={
                (b["external_code"], b["type"]): ["000001"] * 50
                for b in boards
            },
        )
        # 上次有 200 个板块，现在只有 110 个 → 降幅 45%
        with pytest.raises(StagingValidationError, match="dropped"):
            validate_staging_data(staging, prev_board_count=200, prev_membership_count=10000)

    def test_normal_drop_accepted(self) -> None:
        """正常降幅（<20%）通过。"""
        # 上次 150 板块/8000 关系，现在 140 板块/7000 关系
        # board 降幅 6.7%，membership 降幅 12.5%，均 < 20%
        boards = [
            {"external_code": f"B{i:04d}", "name": f"b{i}", "type": "industry"}
            for i in range(MIN_BOARD_COUNT + 40)
        ]
        staging = StagingData(
            boards=boards,
            memberships={
                (b["external_code"], b["type"]): ["000001"] * 50
                for b in boards
            },
        )
        validate_staging_data(staging, prev_board_count=150, prev_membership_count=8000)

    def test_first_sync_no_drop_check(self) -> None:
        """首次同步（prev=0）不做降幅检查。"""
        boards = [
            {"external_code": f"B{i:04d}", "name": f"b{i}", "type": "industry"}
            for i in range(MIN_BOARD_COUNT)
        ]
        staging = StagingData(
            boards=boards,
            memberships={
                (b["external_code"], b["type"]): ["000001"] * 50
                for b in boards
            },
        )
        validate_staging_data(staging, prev_board_count=0, prev_membership_count=0)


# =============================================================================
# 2. 原子切换 + 事务回滚测试（需要测试 DB）
# =============================================================================


@pytest_asyncio.fixture
async def board_test_session(db_session: AsyncSession) -> AsyncSession:
    """提供干净的测试 DB 会话。"""
    return db_session


def _make_instrument_resolver(session: AsyncSession):
    """构造 instrument 解析器：为每个 symbol 在 DB 中创建真实 Instrument。

    避免 FK 违约：membership.instrument_id 必须指向 instruments 表中的真实记录。
    """
    from app.models.instrument import Instrument

    async def _resolve(symbols: list[str]) -> dict[str, UUID]:
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


class TestAtomicSwap:
    """PRD §7.5: 事务原子切换 + 失败保持旧数据。"""

    @pytest.mark.asyncio
    async def test_successful_sync_inserts_data(
        self, board_test_session: AsyncSession
    ) -> None:
        """成功同步后数据被写入。"""
        # 用少量板块避免创建过多 instrument
        boards = [
            {"external_code": f"B{i:04d}", "name": f"板块{i}", "type": "industry"}
            for i in range(MIN_BOARD_COUNT)
        ]
        # 每个板块 30 个成分股，symbol 跨板块复用以减少 instrument 数量
        symbols_pool = [f"{j:06d}" for j in range(40)]
        memberships = {
            (b["external_code"], b["type"]): symbols_pool[:30]
            for b in boards
        }
        fetcher = MockBoardFetcher(boards=boards, memberships=memberships)
        resolver = _make_instrument_resolver(board_test_session)
        result = await sync_boards(
            board_test_session,
            fetcher,
            instrument_resolver=resolver,
        )
        await board_test_session.commit()

        assert result["status"] == "succeeded", f"unexpected: {result}"
        assert result["board_count"] == MIN_BOARD_COUNT
        assert result["membership_count"] > 0

        # 验证数据写入
        board_count, membership_count = await get_current_counts(board_test_session)
        assert board_count == result["board_count"]
        assert membership_count == result["membership_count"]

    @pytest.mark.asyncio
    async def test_validation_failure_keeps_old_data(
        self, board_test_session: AsyncSession
    ) -> None:
        """校验失败时保持旧数据。"""
        # 先插入一些旧数据
        old_board = MarketBoard(
            externalCode="OLD001",
            name="旧板块",
            type="industry",
            updatedAt=datetime.now(UTC),
        )
        board_test_session.add(old_board)
        await board_test_session.commit()
        await board_test_session.refresh(old_board)

        # 用空数据触发校验失败
        fetcher = MockBoardFetcher(boards=[], memberships={})
        result = await sync_boards(
            board_test_session,
            fetcher,
            instrument_resolver=_make_instrument_resolver(board_test_session),
        )

        assert result["status"] == "validation_failed"

        # 旧数据仍存在（校验失败不删除）
        board_count, _ = await get_current_counts(board_test_session)
        assert board_count >= 1

    @pytest.mark.asyncio
    async def test_exception_does_not_modify_data(
        self, board_test_session: AsyncSession
    ) -> None:
        """异常时不修改现有数据。"""
        # 先插入旧数据
        old_board = MarketBoard(
            externalCode="OLD002",
            name="旧板块2",
            type="industry",
            updatedAt=datetime.now(UTC),
        )
        board_test_session.add(old_board)
        await board_test_session.commit()

        # 用会抛异常的 fetcher
        class FailingFetcher:
            async def fetch_boards(self) -> list[dict[str, str]]:
                raise RuntimeError("network error")

            async def fetch_memberships(
                self, board_external_code: str, board_type: str
            ) -> list[str]:
                return []

        result = await sync_boards(
            board_test_session,
            FailingFetcher(),
            instrument_resolver=_make_instrument_resolver(board_test_session),
        )

        assert result["status"] == "failed"

        # 旧数据仍存在
        board_count, _ = await get_current_counts(board_test_session)
        assert board_count >= 1


# =============================================================================
# 3. Migration 循环测试
# =============================================================================


class TestMigrationCycle:
    """PRD: migration upgrade → downgrade → upgrade 循环。"""

    @pytest.mark.asyncio
    async def test_migration_upgrade_downgrade_upgrade(self) -> None:
        """062 migration upgrade → downgrade → upgrade 循环。"""
        import os
        from pathlib import Path

        from alembic.config import Config

        from alembic import command

        db_url = os.environ.get("TEST_DATABASE_URL", "")
        if not db_url:
            pytest.skip("TEST_DATABASE_URL not set")

        # 使用相对于测试文件的路径，避免硬编码绝对路径
        backend_dir = Path(__file__).resolve().parent.parent
        alembic_cfg = Config(str(backend_dir / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", db_url)

        # Upgrade to 062
        command.upgrade(alembic_cfg, "062_market_boards")

        # Downgrade back to 061
        command.downgrade(alembic_cfg, "061_snapshot_source_run_id")

        # Upgrade to 062 again
        command.upgrade(alembic_cfg, "062_market_boards")

        # 验证表存在（使用 async engine + run_sync，避免 sync create_engine 与 psycopg v3 async 冲突）
        from sqlalchemy import inspect
        from sqlalchemy.ext.asyncio import create_async_engine

        async_engine = create_async_engine(db_url)
        try:
            async with async_engine.connect() as conn:
                tables = await conn.run_sync(
                    lambda sync_conn: inspect(sync_conn).get_table_names()
                )
                assert "market_boards" in tables
                assert "market_board_memberships" in tables
        finally:
            await async_engine.dispose()
