"""Market Dashboard P1-F1B — projection 原子持久化（095 两张 projection 表）。

唯一职责：把 F1A 计算出的 records **原子替换**进 ``market_dashboard_market_daily`` /
``market_dashboard_scope_daily``。不做 compute、不带 scheduler/API。

事务合同（不可破坏）：
- writer 拥有自己的事务：要求 **fresh session**（无打开事务），内部 ``async with session.begin()``。
  调用方不得把 writer 混进已有业务 transaction。
- 事务内先 ``LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE``：serialize 并发 writer，
  同时不阻塞普通 ``SELECT`` reader（禁止 ``ACCESS EXCLUSIVE`` / ``TRUNCATE``）。
- stale-T guard：旧 ``MAX(trade_date)`` 若 > 新 T 则拒绝，防止较旧 rebuild 覆盖较新 projection。
- whole snapshot replacement：``DELETE`` old（先 scope 后 market）→ ``INSERT`` market →
  ``INSERT`` scope chunks。**非 UPSERT**（失效 board / membership 变化须自然消失）。
- scope 逐行校验 board_id / trade_date / membership_version；全部消费后校验
  ``scope_rows == len(boards) × len(dates)``（F1A generator 被截断即在此发现）。
- 只有**一个 commit boundary**；任何一步失败整个事务 rollback，旧 projection 完整保留。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Table, delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_dashboard import (
    MarketDashboardMarketDaily,
    MarketDashboardScopeDaily,
)
from app.services.market_dashboard_service import HISTORY_TRADE_DAYS

# 单次 write batch 上限：禁止一条 SQL 塞 ~19 万行，也禁止 ORM object × 19 万。
PERSIST_BATCH_SIZE = 1000

# serialize writers，同时不阻塞普通 SELECT reader（禁止 ACCESS EXCLUSIVE）。
_WRITE_LOCK_SQL = text(
    "LOCK TABLE market_dashboard_market_daily, market_dashboard_scope_daily "
    "IN SHARE ROW EXCLUSIVE MODE"
)


@dataclass(frozen=True)
class ProjectionWriteResult:
    projection_trade_date: date
    market_rows: int
    scope_rows: int


def _validate_market_dates(market_dates: Sequence[date]) -> None:
    for d in market_dates:
        if not isinstance(d, date) or isinstance(d, datetime):
            raise ValueError(f"market record trade_date must be datetime.date, got {d!r}")
    if len(set(market_dates)) != len(market_dates):
        raise ValueError("duplicate market record trade_date")
    if len(market_dates) > HISTORY_TRADE_DAYS:
        raise ValueError(
            f"market_records exceed HISTORY_TRADE_DAYS={HISTORY_TRADE_DAYS}: {len(market_dates)}"
        )


async def _bulk_insert(
    session: AsyncSession,
    table: Table,
    rows: Iterable[Mapping[str, Any]],
) -> int:
    """SQLAlchemy Core bulk insert，按 ``PERSIST_BATCH_SIZE`` 分批（同一事务内）。"""
    total = 0
    batch: list[Mapping[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= PERSIST_BATCH_SIZE:
            await session.execute(insert(table), batch)
            total += len(batch)
            batch = []
    if batch:
        await session.execute(insert(table), batch)
        total += len(batch)
    return total


def _validate_scope_record(
    record: Mapping[str, Any],
    *,
    board_ids: set[UUID],
    dates: set[date],
    membership_versions: Mapping[UUID, str],
) -> None:
    """persistence 侧二次守卫：board / date / membership_version 必须与期望一致。"""
    board_id = record["board_id"]
    trade_date = record["trade_date"]
    if board_id not in board_ids:
        raise ValueError(f"scope record has unexpected board_id {board_id}")
    if trade_date not in dates:
        raise ValueError(f"scope record has unexpected trade_date {trade_date}")
    if record["membership_version"] != membership_versions[board_id]:
        raise ValueError(
            f"membership_version mismatch for board {board_id}: "
            f"{record['membership_version']!r} != {membership_versions[board_id]!r}"
        )


async def replace_dashboard_projection(
    session: AsyncSession,
    *,
    market_records: Sequence[Mapping[str, Any]],
    scope_chunks: Iterable[Sequence[Mapping[str, Any]]],
    expected_membership_versions: Mapping[UUID, str],
) -> ProjectionWriteResult:
    """原子替换 dashboard projection（整个 snapshot，单事务）。

    要求 ``session`` 为 fresh session（无打开事务）；成功返回写入行数摘要。
    """
    if session.in_transaction():
        raise RuntimeError(
            "replace_dashboard_projection requires a fresh session "
            "(session already has an open transaction)"
        )
    if not market_records:
        raise ValueError("market_records must be non-empty")

    market_dates = [r["trade_date"] for r in market_records]
    _validate_market_dates(market_dates)
    new_t = max(market_dates)
    market_date_set = set(market_dates)
    board_ids = set(expected_membership_versions)
    expected_scope_rows = len(board_ids) * len(market_dates)

    async with session.begin():
        # 1) serialize writers（不阻塞普通 SELECT reader）
        await session.execute(_WRITE_LOCK_SQL)

        # 2) stale-T guard：较旧 rebuild 不得覆盖较新 projection（同日允许，幂等）
        existing_t = (
            await session.execute(select(func.max(MarketDashboardMarketDaily.trade_date)))
        ).scalar()
        if existing_t is not None and existing_t > new_t:
            raise ValueError(
                "refusing to replace projection: existing max trade_date "
                f"{existing_t} is newer than incoming {new_t}"
            )

        # 3) DELETE old（whole snapshot replacement；先 scope 后 market）
        await session.execute(delete(MarketDashboardScopeDaily))
        await session.execute(delete(MarketDashboardMarketDaily))

        # 4) INSERT market（F1A 已保证每个 display date 一行）
        market_rows = await _bulk_insert(
            session, MarketDashboardMarketDaily.__table__, market_records
        )

        # 5) INSERT scope chunks（逐行守卫 + 分批；generator 截断 / 乱 mv 在此拒绝）
        scope_rows = 0
        for chunk in scope_chunks:
            chunk_rows = list(chunk)
            for record in chunk_rows:
                _validate_scope_record(
                    record,
                    board_ids=board_ids,
                    dates=market_date_set,
                    membership_versions=expected_membership_versions,
                )
            scope_rows += await _bulk_insert(
                session, MarketDashboardScopeDaily.__table__, chunk_rows
            )

        # 6) 完整性校验（缺失行 → 计数不足；重复行 → PK 冲突；extra board/date → 守卫）
        if scope_rows != expected_scope_rows:
            raise ValueError(
                "scope completeness violated: wrote "
                f"{scope_rows}, expected {expected_scope_rows} "
                f"({len(board_ids)} boards x {len(market_dates)} dates)"
            )

    return ProjectionWriteResult(
        projection_trade_date=new_t,
        market_rows=market_rows,
        scope_rows=scope_rows,
    )


if __name__ == "__main__":
    print(f"PERSIST_BATCH_SIZE={PERSIST_BATCH_SIZE}")
    print(f"HISTORY_TRADE_DAYS={HISTORY_TRADE_DAYS}")
    print("OK")
