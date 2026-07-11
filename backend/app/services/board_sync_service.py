"""Board Sync Service - qstock 概念/行业板块同步（PRD §7.5）。

V1.1 完整性门禁：
1. 先写暂存集合（内存）
2. 校验：空集合、目录数、成分数、异常降幅（>20%）、解析率
3. 全部通过后事务原子切换（集合差异 + 批量 upsert/delete）
4. 失败抛异常，由调用方控制事务回滚，保留上一成功版本

调度：每日收盘后或次日开盘前执行一次，单并发。
qstock 只存在于独立采集适配器，不成为用户请求链的运行时依赖。

纯函数设计：validate_staging_data 是纯函数，可直接单元测试。
sync_boards 是异步服务入口，编排完整流程，失败抛异常。
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
MIN_PARSE_RATE = 0.50  # 最低解析率：resolved_symbols / total_symbols

# 批量操作大小
BATCH_SIZE = 500


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

    Raises:
        StagingValidationError: 校验失败
    """
    if staging.board_count == 0:
        raise StagingValidationError("staging boards is empty")
    if staging.membership_count == 0:
        raise StagingValidationError("staging memberships is empty")
    if staging.board_count < MIN_BOARD_COUNT:
        raise StagingValidationError(
            f"staging board count {staging.board_count} < minimum {MIN_BOARD_COUNT}"
        )
    if staging.membership_count < MIN_MEMBERSHIP_COUNT:
        raise StagingValidationError(
            f"staging membership count {staging.membership_count} < minimum {MIN_MEMBERSHIP_COUNT}"
        )

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


async def _batch_delete(db: AsyncSession, model: Any, ids: list, batch_size: int = BATCH_SIZE) -> None:
    """分批删除，避免单次 SQL 过大。"""
    for i in range(0, len(ids), batch_size):
        chunk = ids[i : i + batch_size]
        await db.execute(delete(model).where(model.id.in_(chunk)))


async def sync_boards(
    db: AsyncSession,
    fetcher: BoardFetcher,
    instrument_resolver: Any | None = None,
) -> dict[str, Any]:
    """执行完整的板块同步流程（PRD §7.5）。

    流程：
    1. 拉取目录和成分到暂存集合
    2. 校验完整性（空集合/下限/降幅）
    3. 解析 symbol → instrument_id，校验解析率
    4. 集合差异 + 批量 upsert/delete（事务内，由调用方控制 commit）
    5. 失败抛异常，不修改现有数据

    Args:
        db: 异步数据库会话
        fetcher: qstock 数据拉取适配器
        instrument_resolver: 将 symbol 解析为 instrument_id 的函数

    Returns:
        同步结果摘要 {board_count, membership_count, status, ...}

    Raises:
        StagingValidationError: 暂存数据校验失败
        BoardSyncError: 解析率过低或写入失败
        QStockFetchError: qstock 拉取失败
    """
    # 1. 获取当前计数（用于异常降幅校验）
    prev_board_count, prev_membership_count = await get_current_counts(db)

    # 2. 拉取目录（fetcher 内部失败会抛异常，不返回空列表）
    boards_data = await fetcher.fetch_boards()
    if not boards_data:
        raise StagingValidationError("fetcher returned empty boards")

    # 3. 拉取成分到暂存集合
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

    # 4. 校验暂存数据
    validate_staging_data(staging, prev_board_count, prev_membership_count)

    # 5. 解析 symbol → instrument_id
    all_symbols = set()
    for syms in staging.memberships.values():
        all_symbols.update(syms)

    symbol_to_id: dict[str, UUID] = {}
    total_symbol_count = len(all_symbols)
    if instrument_resolver and all_symbols:
        symbol_to_id = await instrument_resolver(list(all_symbols))

    resolved_count = len(symbol_to_id)
    parse_rate = resolved_count / total_symbol_count if total_symbol_count > 0 else 0.0
    if parse_rate < MIN_PARSE_RATE:
        raise BoardSyncError(
            f"instrument parse rate {parse_rate:.1%} < minimum {MIN_PARSE_RATE:.0%} "
            f"({resolved_count}/{total_symbol_count} resolved)"
        )

    # 6. 查询现有 boards → 计算差异
    existing_boards_result = await db.execute(
        select(MarketBoard.id, MarketBoard.externalCode, MarketBoard.type, MarketBoard.name)
    )
    existing_board_map: dict[tuple[str, str], dict[str, Any]] = {}
    for row in existing_boards_result:
        existing_board_map[(row.externalCode, row.type)] = {"id": row.id, "name": row.name}

    new_board_keys = {(b["external_code"], b["type"]) for b in staging.boards}

    # board 差异
    boards_to_delete_ids = [
        v["id"] for k, v in existing_board_map.items() if k not in new_board_keys
    ]
    boards_to_insert = [
        b for b in staging.boards if (b["external_code"], b["type"]) not in existing_board_map
    ]
    boards_to_update = [
        (existing_board_map[k]["id"], b["name"])
        for b in staging.boards
        for k in [(b["external_code"], b["type"])]
        if k in existing_board_map and existing_board_map[k]["name"] != b["name"]
    ]

    # 7. 执行 board 变更（事务内，由调用方控制 commit/rollback）
    now = datetime.now(UTC)

    # 删除旧 boards（先删 memberships 避免 FK 违约，虽然 CASCADE 也会处理）
    if boards_to_delete_ids:
        await _batch_delete_mem_by_board(db, boards_to_delete_ids)
        await _batch_delete(db, MarketBoard, boards_to_delete_ids)

    # 插入新 boards
    new_board_id_map: dict[tuple[str, str], UUID] = {}
    for b in boards_to_insert:
        new_board = MarketBoard(
            externalCode=b["external_code"],
            name=b["name"],
            type=b["type"],
            updatedAt=now,
        )
        db.add(new_board)
        await db.flush()
        new_board_id_map[(b["external_code"], b["type"])] = new_board.id

    # 更新 board names
    for board_id, new_name in boards_to_update:
        from sqlalchemy import update as sa_update
        await db.execute(
            sa_update(MarketBoard)
            .where(MarketBoard.id == board_id)
            .values(name=new_name, updatedAt=now)
        )

    # 合并 board_key → id 映射（existing 保留 + 新插入）
    board_key_to_id: dict[tuple[str, str], UUID] = {}
    for k, v in existing_board_map.items():
        if k in new_board_keys:
            board_key_to_id[k] = v["id"]
    board_key_to_id.update(new_board_id_map)

    # 8. 查询现有 memberships → 计算差异
    kept_board_ids = list(board_key_to_id.values())
    existing_mem_keys: set[tuple] = set()
    if kept_board_ids:
        existing_mem_result = await db.execute(
            select(MarketBoardMembership.boardId, MarketBoardMembership.instrumentId)
            .where(MarketBoardMembership.boardId.in_(kept_board_ids))
        )
        for mem_row in existing_mem_result:
            existing_mem_keys.add((mem_row.boardId, mem_row.instrumentId))

    # 期望的 membership 集合
    desired_memberships: set[tuple] = set()
    for (ext_code, btype), symbols in staging.memberships.items():
        board_id = board_key_to_id.get((ext_code, btype))
        if board_id is None:
            continue
        for sym in symbols:
            instr_id = symbol_to_id.get(sym)
            if instr_id is not None:
                desired_memberships.add((board_id, instr_id))

    # membership 差异
    memberships_to_delete = existing_mem_keys - desired_memberships
    memberships_to_insert_keys = desired_memberships - existing_mem_keys

    # 批量删除（按 board_id 批次，避免 N+1）
    if memberships_to_delete:
        await _batch_delete_mem_by_keys(db, list(memberships_to_delete))

    # 批量插入
    memberships_inserted = 0
    keys_to_insert = list(memberships_to_insert_keys)
    for i in range(0, len(keys_to_insert), BATCH_SIZE):
        chunk = keys_to_insert[i : i + BATCH_SIZE]
        for board_id, instr_id in chunk:
            db.add(MarketBoardMembership(
                boardId=board_id,
                instrumentId=instr_id,
                updatedAt=now,
            ))
        await db.flush()
        memberships_inserted += len(chunk)

    logger.info(
        "board_sync diff: boards delete=%d insert=%d update=%d, memberships delete=%d insert=%d",
        len(boards_to_delete_ids), len(boards_to_insert), len(boards_to_update),
        len(memberships_to_delete), memberships_inserted,
    )

    return {
        "board_count": len(staging.boards),
        "membership_count": len(desired_memberships),
        "status": "succeeded",
        "boards_deleted": len(boards_to_delete_ids),
        "boards_inserted": len(boards_to_insert),
        "boards_updated": len(boards_to_update),
        "memberships_deleted": len(memberships_to_delete),
        "memberships_inserted": memberships_inserted,
        "parse_rate": round(parse_rate, 4),
    }


async def _batch_delete_mem_by_board(db: AsyncSession, board_ids: list, batch_size: int = BATCH_SIZE) -> None:
    """按 board_id 批量删除 memberships。"""
    for i in range(0, len(board_ids), batch_size):
        chunk = board_ids[i : i + batch_size]
        await db.execute(
            delete(MarketBoardMembership).where(MarketBoardMembership.boardId.in_(chunk))
        )


async def _batch_delete_mem_by_keys(
    db: AsyncSession, keys: list[tuple], batch_size: int = BATCH_SIZE
) -> None:
    """按 (board_id, instrument_id) 复合主键批量删除 memberships。

    使用 PostgreSQL tuple IN 语法，避免 N+1 删除查询。
    """
    from sqlalchemy import tuple_

    for i in range(0, len(keys), batch_size):
        chunk = keys[i : i + batch_size]
        await db.execute(
            delete(MarketBoardMembership).where(
                tuple_(
                    MarketBoardMembership.boardId,
                    MarketBoardMembership.instrumentId,
                ).in_(chunk)
            )
        )


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


async def get_instrument_boards_batch(
    db: AsyncSession,
    instrument_ids: list[UUID],
) -> dict[UUID, list[dict[str, str]]]:
    """批量查询多只股票的板块归属（只读，禁止 N+1）。

    Returns:
        {instrument_id: [{external_code, name, type}, ...]}
    """
    if not instrument_ids:
        return {}
    stmt = (
        select(
            MarketBoardMembership.instrumentId,
            MarketBoard.externalCode,
            MarketBoard.name,
            MarketBoard.type,
        )
        .join(MarketBoard, MarketBoard.id == MarketBoardMembership.boardId)
        .where(MarketBoardMembership.instrumentId.in_(instrument_ids))
    )
    result = await db.execute(stmt)
    mapping: dict[UUID, list[dict[str, str]]] = {}
    for row in result:
        inst_id = row.instrumentId if hasattr(row, "instrumentId") else row[0]
        mapping.setdefault(inst_id, []).append({
            "external_code": row.externalCode if hasattr(row, "externalCode") else row[1],
            "name": row.name if hasattr(row, "name") else row[2],
            "type": row.type if hasattr(row, "type") else row[3],
        })
    return mapping
