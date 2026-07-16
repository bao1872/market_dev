"""Board Sync Service - 问财板块原子快照同步（PRD §7.5 重构）。

V1.1 完整性门禁（绝对门禁 + 相对门禁）：
1. 接收 wencai_board_provider 构建的完整 BoardSnapshot（内存中全部 boards + memberships）
2. 绝对门禁：原始行≥5000、代码唯一率≥99.9%、行业≥200、概念≥300、关系≥60000、解析率≥95%
3. 相对门禁：股票/行业/概念/关系任一下降>20% 拒绝切换
4. 全部通过后单事务差异 upsert/delete（原子切换）
5. 失败 rollback 保留上一成功版本；成功时刷新有效 board 的 updated_at

board 同步是软失败：失败不覆盖旧数据、不阻断 DSA/快照/发布。
状态通过 Redis 记录（record_sync_status / get_sync_status），供 /market/boards API 读取。

纯函数设计：validate_snapshot 是纯函数，可直接单元测试。
sync_boards 是异步服务入口，编排完整流程，失败抛异常。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis_client import get_redis
from app.models.market_board import MarketBoard, MarketBoardMembership
from app.services.wencai_board_provider import BoardSnapshot

logger = logging.getLogger(__name__)

# =============================================================================
# 绝对门禁阈值（PROMPT §四.2）
# =============================================================================
MIN_RAW_ROWS = 5000  # 原始行数下限
MIN_CODE_UNIQUENESS_RATE = 0.999  # 代码唯一率 ≥ 99.9%
MIN_INDUSTRY_COUNT = 200  # 完整行业 ≥ 200
MIN_CONCEPT_COUNT = 300  # 概念 ≥ 300
MIN_RELATION_COUNT = 60000  # 总关系 ≥ 60000
MIN_PARSE_RATE = 0.95  # 有效 A 股解析率 ≥ 95%
MIN_INDUSTRY_COVERAGE = 0.99  # 已解析股票行业覆盖 ≥ 99%
MAX_CONCEPTS_PER_STOCK = 100  # 单股概念 ≤ 100

# 相对门禁阈值
MAX_DROP_PERCENT = 0.20  # 任一指标下降 >20% 拒绝切换

# 批量操作大小（500~1000）
BATCH_SIZE = 500

# Redis 状态键
_BOARD_SYNC_STATUS_KEY = "board_sync:status"
_BOARD_SYNC_STATUS_TTL = 7 * 24 * 3600  # 7 天


class BoardSyncError(Exception):
    """板块同步错误基类。"""


class StagingValidationError(BoardSyncError):
    """暂存数据校验失败（绝对门禁或相对门禁）。"""


class BoardSyncStatusError(BoardSyncError):
    """板块同步状态读写失败。"""


# =============================================================================
# 纯函数：绝对门禁校验
# =============================================================================


def validate_snapshot(
    snapshot: BoardSnapshot,
    *,
    prev_stock_count: int = 0,
    prev_industry_count: int = 0,
    prev_concept_count: int = 0,
    prev_relation_count: int = 0,
) -> dict[str, Any]:
    """校验快照完整性（绝对门禁 + 相对门禁）。

    绝对门禁：
    1. 原始行数 ≥ 5000
    2. 代码唯一率 ≥ 99.9%（unique_symbols / total_symbol_refs）
    3. 行业数 ≥ 200
    4. 概念数 ≥ 300
    5. 总关系数 ≥ 60000
    6. 概念数/股 ≤ 100（已在 provider 截断）

    相对门禁（prev > 0 时检查）：
    7. 股票/行业/概念/关系任一下降 > 20% 拒绝

    Args:
        snapshot: wencai_board_provider 构建的 BoardSnapshot
        prev_stock_count: 上次成功的股票数
        prev_industry_count: 上次成功的行业数
        prev_concept_count: 上次成功的概念数
        prev_relation_count: 上次成功的关系数

    Returns:
        校验统计 dict（供 metadata 记录）

    Raises:
        StagingValidationError: 校验失败
    """
    stats = _compute_snapshot_stats(snapshot)

    # 绝对门禁
    if stats["raw_rows"] < MIN_RAW_ROWS:
        raise StagingValidationError(
            f"raw rows {stats['raw_rows']} < minimum {MIN_RAW_ROWS}"
        )
    if stats["code_uniqueness_rate"] < MIN_CODE_UNIQUENESS_RATE:
        raise StagingValidationError(
            f"code uniqueness rate {stats['code_uniqueness_rate']:.4f} "
            f"< minimum {MIN_CODE_UNIQUENESS_RATE}"
        )
    if stats["industry_count"] < MIN_INDUSTRY_COUNT:
        raise StagingValidationError(
            f"industry count {stats['industry_count']} < minimum {MIN_INDUSTRY_COUNT}"
        )
    if stats["concept_count"] < MIN_CONCEPT_COUNT:
        raise StagingValidationError(
            f"concept count {stats['concept_count']} < minimum {MIN_CONCEPT_COUNT}"
        )
    if stats["relation_count"] < MIN_RELATION_COUNT:
        raise StagingValidationError(
            f"relation count {stats['relation_count']} < minimum {MIN_RELATION_COUNT}"
        )

    # 相对门禁
    if prev_stock_count > 0:
        drop = 1.0 - (stats["unique_stock_count"] / prev_stock_count)
        if drop > MAX_DROP_PERCENT:
            raise StagingValidationError(
                f"stock count dropped {drop:.1%} (from {prev_stock_count} "
                f"to {stats['unique_stock_count']}), exceeds {MAX_DROP_PERCENT:.0%}"
            )
    if prev_industry_count > 0:
        drop = 1.0 - (stats["industry_count"] / prev_industry_count)
        if drop > MAX_DROP_PERCENT:
            raise StagingValidationError(
                f"industry count dropped {drop:.1%} (from {prev_industry_count} "
                f"to {stats['industry_count']}), exceeds {MAX_DROP_PERCENT:.0%}"
            )
    if prev_concept_count > 0:
        drop = 1.0 - (stats["concept_count"] / prev_concept_count)
        if drop > MAX_DROP_PERCENT:
            raise StagingValidationError(
                f"concept count dropped {drop:.1%} (from {prev_concept_count} "
                f"to {stats['concept_count']}), exceeds {MAX_DROP_PERCENT:.0%}"
            )
    if prev_relation_count > 0:
        drop = 1.0 - (stats["relation_count"] / prev_relation_count)
        if drop > MAX_DROP_PERCENT:
            raise StagingValidationError(
                f"relation count dropped {drop:.1%} (from {prev_relation_count} "
                f"to {stats['relation_count']}), exceeds {MAX_DROP_PERCENT:.0%}"
            )

    return stats


def _compute_snapshot_stats(snapshot: BoardSnapshot) -> dict[str, Any]:
    """计算快照统计信息（纯函数）。"""
    industry_count = sum(1 for b in snapshot.boards if b["type"] == "industry")
    concept_count = sum(1 for b in snapshot.boards if b["type"] == "concept")

    # 统计唯一股票代码和总引用
    all_symbols: set[str] = set()
    total_symbol_refs = 0
    for symbols in snapshot.memberships.values():
        all_symbols.update(symbols)
        total_symbol_refs += len(symbols)

    unique_stock_count = len(all_symbols)
    # 代码唯一率：唯一股票数 / 原始行数（每行应为一个唯一股票，非 membership 引用率）
    code_uniqueness_rate = (
        unique_stock_count / snapshot.raw_rows if snapshot.raw_rows > 0 else 0.0
    )

    return {
        "raw_rows": snapshot.raw_rows,
        "industry_count": industry_count,
        "concept_count": concept_count,
        "board_count": snapshot.board_count,
        "relation_count": snapshot.membership_count,
        "unique_stock_count": unique_stock_count,
        "total_symbol_refs": total_symbol_refs,
        "code_uniqueness_rate": round(code_uniqueness_rate, 4),
        "unresolved_count": len(snapshot.unresolved_symbols),
    }


# =============================================================================
# 数据库查询：当前计数
# =============================================================================


async def get_current_counts(db: AsyncSession) -> tuple[int, int]:
    """获取当前板块数和成分关系数。"""
    board_count = await db.scalar(select(func.count()).select_from(MarketBoard))
    membership_count = await db.scalar(
        select(func.count()).select_from(MarketBoardMembership)
    )
    return (board_count or 0, membership_count or 0)


async def get_current_detailed_counts(db: AsyncSession) -> dict[str, int]:
    """获取当前板块详细计数（board/industry/concept/membership/stock）。"""
    board_count = await db.scalar(select(func.count()).select_from(MarketBoard)) or 0
    membership_count = await db.scalar(
        select(func.count()).select_from(MarketBoardMembership)
    ) or 0
    industry_count = await db.scalar(
        select(func.count()).select_from(MarketBoard).where(MarketBoard.type == "industry")
    ) or 0
    concept_count = await db.scalar(
        select(func.count()).select_from(MarketBoard).where(MarketBoard.type == "concept")
    ) or 0
    stock_count = await db.scalar(
        select(func.count(func.distinct(MarketBoardMembership.instrumentId)))
    ) or 0
    return {
        "board_count": board_count,
        "membership_count": membership_count,
        "industry_count": industry_count,
        "concept_count": concept_count,
        "stock_count": stock_count,
    }


# =============================================================================
# 原子同步主函数
# =============================================================================


async def sync_boards(
    db: AsyncSession,
    snapshot: BoardSnapshot,
    instrument_resolver: Any | None = None,
) -> dict[str, Any]:
    """执行完整的板块原子同步（PRD §7.5 重构）。

    流程：
    1. 获取当前计数（相对门禁用）
    2. 绝对门禁 + 相对门禁校验
    3. 批量解析 symbol → instrument_id（500~1000 批次）
    4. 单事务差异 upsert/delete（原子切换）
    5. 成功时刷新有效 board 的 updated_at

    Args:
        db: 异步数据库会话
        snapshot: wencai_board_provider 构建的完整 BoardSnapshot
        instrument_resolver: 将 symbol 解析为 instrument_id 的异步函数

    Returns:
        同步结果摘要 dict

    Raises:
        StagingValidationError: 门禁校验失败
        BoardSyncError: 解析率过低或写入失败
    """
    start_time = time.monotonic()

    # 1. 获取当前详细计数（相对门禁用）
    prev_counts = await get_current_detailed_counts(db)

    # 2. 门禁校验
    stats = validate_snapshot(
        snapshot,
        prev_stock_count=prev_counts["stock_count"],
        prev_industry_count=prev_counts["industry_count"],
        prev_concept_count=prev_counts["concept_count"],
        prev_relation_count=prev_counts["membership_count"],
    )

    # 3. 批量解析 symbol → instrument_id
    all_symbols = set()
    for symbols in snapshot.memberships.values():
        all_symbols.update(symbols)

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

    # 4. 单事务差异 upsert/delete
    result = await _atomic_switch(db, snapshot, symbol_to_id)

    duration_ms = int((time.monotonic() - start_time) * 1000)

    return {
        "status": "succeeded",
        "board_count": stats["board_count"],
        "industry_count": stats["industry_count"],
        "concept_count": stats["concept_count"],
        "membership_count": stats["relation_count"],
        "unique_stock_count": stats["unique_stock_count"],
        "raw_rows": stats["raw_rows"],
        "resolved": resolved_count,
        "unresolved": total_symbol_count - resolved_count,
        "parse_rate": round(parse_rate, 4),
        "duration_ms": duration_ms,
        **result,
    }


async def _atomic_switch(
    db: AsyncSession,
    snapshot: BoardSnapshot,
    symbol_to_id: dict[str, UUID],
) -> dict[str, Any]:
    """单事务差异 upsert/delete（原子切换）。

    任何异常由调用方的 rollback 保留旧数据。
    成功时刷新有效 board 的 updated_at（即使内容未变）。
    """
    now = datetime.now(UTC)

    # 查询现有 boards
    existing_boards_result = await db.execute(
        select(MarketBoard.id, MarketBoard.externalCode, MarketBoard.type, MarketBoard.name)
    )
    existing_board_map: dict[tuple[str, str], dict[str, Any]] = {}
    for row in existing_boards_result:
        existing_board_map[(row.externalCode, row.type)] = {"id": row.id, "name": row.name}

    new_board_keys = {(b["external_code"], b["type"]) for b in snapshot.boards}

    # board 差异
    boards_to_delete_ids = [
        v["id"] for k, v in existing_board_map.items() if k not in new_board_keys
    ]
    boards_to_insert = [
        b for b in snapshot.boards if (b["external_code"], b["type"]) not in existing_board_map
    ]
    boards_to_update = [
        (existing_board_map[k]["id"], b["name"])
        for b in snapshot.boards
        for k in [(b["external_code"], b["type"])]
        if k in existing_board_map and existing_board_map[k]["name"] != b["name"]
    ]
    # 即使 name 未变也需要刷新 updated_at 的 board ids
    boards_to_touch = [
        existing_board_map[k]["id"]
        for b in snapshot.boards
        for k in [(b["external_code"], b["type"])]
        if k in existing_board_map
    ]

    # 删除旧 boards（先删 memberships 避免 FK 违约）
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
        await db.execute(
            sa_update(MarketBoard)
            .where(MarketBoard.id == board_id)
            .values(name=new_name, updatedAt=now)
        )

    # 刷新已存在 board 的 updated_at（即使内容未变）
    if boards_to_touch:
        for i in range(0, len(boards_to_touch), BATCH_SIZE):
            chunk = boards_to_touch[i : i + BATCH_SIZE]
            await db.execute(
                sa_update(MarketBoard)
                .where(MarketBoard.id.in_(chunk))
                .values(updatedAt=now)
            )

    # 合并 board_key → id 映射
    board_key_to_id: dict[tuple[str, str], UUID] = {}
    for k, v in existing_board_map.items():
        if k in new_board_keys:
            board_key_to_id[k] = v["id"]
    board_key_to_id.update(new_board_id_map)

    # 查询现有 memberships
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
    for (ext_code, btype), symbols in snapshot.memberships.items():
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

    # 批量删除
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
        "board_sync diff: boards delete=%d insert=%d update=%d touch=%d, "
        "memberships delete=%d insert=%d",
        len(boards_to_delete_ids), len(boards_to_insert),
        len(boards_to_update), len(boards_to_touch),
        len(memberships_to_delete), memberships_inserted,
    )

    return {
        "boards_deleted": len(boards_to_delete_ids),
        "boards_inserted": len(boards_to_insert),
        "boards_updated": len(boards_to_update),
        "memberships_deleted": len(memberships_to_delete),
        "memberships_inserted": memberships_inserted,
    }


# =============================================================================
# 批量删除辅助
# =============================================================================


async def _batch_delete(db: AsyncSession, model: Any, ids: list, batch_size: int = BATCH_SIZE) -> None:
    """分批删除，避免单次 SQL 过大。"""
    for i in range(0, len(ids), batch_size):
        chunk = ids[i : i + batch_size]
        await db.execute(delete(model).where(model.id.in_(chunk)))


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
    """按 (board_id, instrument_id) 复合主键批量删除 memberships。"""
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


# =============================================================================
# Redis 状态跟踪（供 /market/boards API 读取）
# =============================================================================


async def record_sync_status(status: dict[str, Any]) -> None:
    """记录板块同步状态到 Redis（供 /market/boards API 读取）。

    Args:
        status: 同步状态 dict，包含：
            - status: "succeeded" | "failed" | "degraded"
            - source: "wencai"
            - completed_at: ISO 时间戳
            - raw_rows, resolved, unresolved, industry_count, concept_count,
              membership_count, duration_ms, error_code, reused_previous_snapshot
    """
    try:
        redis = await get_redis()
        status["completed_at"] = datetime.now(UTC).isoformat()
        await redis.set(
            _BOARD_SYNC_STATUS_KEY,
            json.dumps(status, ensure_ascii=False),
            ex=_BOARD_SYNC_STATUS_TTL,
        )
    except Exception as exc:
        logger.warning("[BoardSync] 记录同步状态到 Redis 失败: %s", exc)


async def get_sync_status() -> dict[str, Any] | None:
    """读取板块同步状态（从 Redis）。

    Returns:
        状态 dict 或 None（无记录时）
    """
    try:
        redis = await get_redis()
        raw = await redis.get(_BOARD_SYNC_STATUS_KEY)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("[BoardSync] 读取同步状态从 Redis 失败: %s", exc)
        return None


# =============================================================================
# 只读查询函数（供 API 和筛选使用）
# =============================================================================


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
