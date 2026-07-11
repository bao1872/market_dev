"""Board Sync Service - qstock 概念/行业板块同步（PRD §7.5）。

V1.1 完整性门禁：
1. 先写暂存集合（内存或临时表）
2. 校验：空集合、目录数、成分数、异常降幅（>20%）
3. 全部通过后事务原子切换（TRUNCATE + INSERT）
4. 失败保持上一成功版本，不删除旧关系

调度：每日收盘后或次日开盘前执行一次，单并发。
qstock 只存在于独立采集适配器，不成为用户请求链的运行时依赖。

纯函数设计：validate_staging_data 和 prepare_atomic_swap 是纯函数，
可直接单元测试。sync_boards 是异步服务入口，编排完整流程。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_board import MarketBoard, MarketBoardMembership

logger = logging.getLogger(__name__)

# 完整性校验阈值
MIN_BOARD_COUNT = 100  # A 股至少 100+ 行业/概念板块
MIN_MEMBERSHIP_COUNT = 3000  # 至少 3000 条成分关系
MAX_DROP_PERCENT = 0.20  # 异常降幅阈值：>20% 拒绝切换


class BoardSyncError(Exception):
    """板块同步错误基类。"""


class StagingValidationError(BoardSyncError):
    """暂存数据校验失败。"""


class BoardFetcher(Protocol):
    """qstock 数据拉取适配器协议。"""

    async def fetch_boards(self) -> list[dict[str, str]]:
        """拉取板块目录。返回 [{external_code, name, type}]。"""
        ...

    async def fetch_memberships(
        self, board_external_code: str, board_type: str
    ) -> list[str]:
        """拉取指定板块的成分股代码列表。返回 [symbol, ...]。"""
        ...


@dataclass
class StagingData:
    """暂存数据（内存中的完整快照）。"""

    boards: list[dict[str, str]] = field(default_factory=list)
    # (external_code, type) -> [symbol, ...]
    memberships: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    @property
    def board_count(self) -> int:
        return len(self.boards)

    @property
    def membership_count(self) -> int:
        return sum(len(v) for v in self.memberships.values())


def validate_staging_data(
    staging: StagingData,
    prev_board_count: int = 0,
    prev_membership_count: int = 0,
) -> None:
    """校验暂存数据完整性（PRD V1.1 完整性门禁）。

    校验项：
    1. 空集合检查
    2. 目录数下限检查
    3. 成分数下限检查
    4. 异常降幅检查（>20% 拒绝）

    Args:
        staging: 暂存数据
        prev_board_count: 上次成功板块数（0 表示首次）
        prev_membership_count: 上次成功成分关系数（0 表示首次）

    Raises:
        StagingValidationError: 校验失败
    """
    # 1. 空集合检查
    if staging.board_count == 0:
        raise StagingValidationError("staging boards is empty")
    if staging.membership_count == 0:
        raise StagingValidationError("staging memberships is empty")

    # 2. 目录数下限
    if staging.board_count < MIN_BOARD_COUNT:
        raise StagingValidationError(
            f"staging board count {staging.board_count} < minimum {MIN_BOARD_COUNT}"
        )

    # 3. 成分数下限
    if staging.membership_count < MIN_MEMBERSHIP_COUNT:
        raise StagingValidationError(
            f"staging membership count {staging.membership_count} < minimum {MIN_MEMBERSHIP_COUNT}"
        )

    # 4. 异常降幅检查（非首次）
    if prev_board_count > 0:
        board_drop = 1.0 - (staging.board_count / prev_board_count)
        if board_drop > MAX_DROP_PERCENT:
            raise StagingValidationError(
                f"board count dropped {board_drop:.1%} (from {prev_board_count} "
                f"to {staging.board_count}), exceeds {MAX_DROP_PERCENT:.0%} threshold"
            )

    if prev_membership_count > 0:
        membership_drop = 1.0 - (staging.membership_count / prev_membership_count)
        if membership_drop > MAX_DROP_PERCENT:
            raise StagingValidationError(
                f"membership count dropped {membership_drop:.1%} "
                f"(from {prev_membership_count} to {staging.membership_count}), "
                f"exceeds {MAX_DROP_PERCENT:.0%} threshold"
            )


async def get_current_counts(db: AsyncSession) -> tuple[int, int]:
    """获取当前板块数和成分关系数。"""
    board_count = await db.scalar(select(func.count()).select_from(MarketBoard))
    membership_count = await db.scalar(
        select(func.count()).select_from(MarketBoardMembership)
    )
    return (board_count or 0, membership_count or 0)


async def sync_boards(
    db: AsyncSession,
    fetcher: BoardFetcher,
    instrument_resolver: Any | None = None,
) -> dict[str, Any]:
    """执行完整的板块同步流程（PRD §7.5）。

    流程：
    1. 拉取目录和成分到暂存集合
    2. 校验完整性
    3. 事务原子切换（TRUNCATE + INSERT）
    4. 失败保持上一成功版本

    Args:
        db: 异步数据库会话
        fetcher: qstock 数据拉取适配器
        instrument_resolver: 将 symbol 解析为 instrument_id 的函数

    Returns:
        同步结果摘要 {board_count, membership_count, status, error}
    """
    result: dict[str, Any] = {
        "board_count": 0,
        "membership_count": 0,
        "status": "pending",
        "error": None,
    }

    try:
        # 1. 获取当前计数（用于异常降幅校验）
        prev_board_count, prev_membership_count = await get_current_counts(db)

        # 2. 拉取目录
        boards_data = await fetcher.fetch_boards()
        if not boards_data:
            raise StagingValidationError("fetcher returned empty boards")

        # 3. 拉取成分
        staging = StagingData()
        for board in boards_data:
            ext_code = board.get("external_code", "")
            name = board.get("name", "")
            btype = board.get("type", "")
            if not ext_code or not name or not btype:
                continue
            staging.boards.append({"external_code": ext_code, "name": name, "type": btype})
            symbols = await fetcher.fetch_memberships(ext_code, btype)
            staging.memberships[(ext_code, btype)] = symbols

        # 4. 校验
        validate_staging_data(staging, prev_board_count, prev_membership_count)

        # 5. 原子切换（事务内 TRUNCATE + INSERT）
        # 注意：不在此时 commit，由调用方控制事务
        # 先收集 instrument_id 映射
        all_symbols = set()
        for syms in staging.memberships.values():
            all_symbols.update(syms)

        symbol_to_id: dict[str, UUID] = {}
        if instrument_resolver and all_symbols:
            symbol_to_id = await instrument_resolver(list(all_symbols))

        # 删除旧数据
        await db.execute(delete(MarketBoardMembership))
        await db.execute(delete(MarketBoard))

        # 插入新数据
        board_key_to_id: dict[tuple[str, str], UUID] = {}
        now = datetime.now(UTC)
        for board_data in staging.boards:
            key = (board_data["external_code"], board_data["type"])
            new_board: MarketBoard = MarketBoard(
                externalCode=board_data["external_code"],
                name=board_data["name"],
                type=board_data["type"],
                updatedAt=now,
            )
            db.add(new_board)
            await db.flush()  # 获取 id
            board_key_to_id[key] = new_board.id

        membership_count = 0
        for (ext_code, btype), symbols in staging.memberships.items():
            board_id = board_key_to_id.get((ext_code, btype))
            if board_id is None:
                continue
            for sym in symbols:
                instr_id = symbol_to_id.get(sym)
                if instr_id is None:
                    continue
                db.add(MarketBoardMembership(
                    boardId=board_id,
                    instrumentId=instr_id,
                    updatedAt=now,
                ))
                membership_count += 1

        result["board_count"] = len(staging.boards)
        result["membership_count"] = membership_count
        result["status"] = "succeeded"

    except StagingValidationError as e:
        result["status"] = "validation_failed"
        result["error"] = str(e)
        logger.warning(f"Board sync validation failed: {e}")
        # 不修改现有数据
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        logger.error(f"Board sync failed: {e}", exc_info=True)

    return result


async def get_instrument_boards(
    db: AsyncSession,
    instrument_id: UUID,
    board_type: str | None = None,
) -> list[dict[str, str]]:
    """查询某只股票的板块归属（只读）。"""
    stmt = (
        select(MarketBoard.externalCode, MarketBoard.name, MarketBoard.type)
        .join(MarketBoardMembership, MarketBoardMembership.boardId == MarketBoard.id)
        .where(MarketBoardMembership.instrumentId == instrument_id)
    )
    if board_type is not None:
        stmt = stmt.where(MarketBoard.type == board_type)
    result = await db.execute(stmt)
    return [
        {"external_code": row[0], "name": row[1], "type": row[2]}
        for row in result
    ]


async def filter_instruments_by_board(
    db: AsyncSession,
    board_type: str,
    board_name: str | None = None,
    board_code: str | None = None,
) -> list[UUID]:
    """按板块筛选 instrument_id 列表（用于 industry/concept 筛选）。"""
    stmt = (
        select(MarketBoardMembership.instrumentId)
        .join(MarketBoard, MarketBoard.id == MarketBoardMembership.boardId)
        .where(MarketBoard.type == board_type)
    )
    if board_name is not None:
        stmt = stmt.where(MarketBoard.name == board_name)
    if board_code is not None:
        stmt = stmt.where(MarketBoard.externalCode == board_code)
    result = await db.execute(stmt)
    return [row[0] for row in result]
