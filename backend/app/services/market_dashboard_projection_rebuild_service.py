"""Market Dashboard P1-F1C — rebuild orchestration（race-safe；无 scheduler/API）。

三 owner 继续分离：F1A=compute、F1B=persistence、F1C=lifecycle orchestration。

安全调用契约：
① READ / COMPUTE
   fresh read session；在业务 SELECT 之前 ``SET TRANSACTION ISOLATION LEVEL
   REPEATABLE READ, READ ONLY`` → ``prepare_projection_context``（instrument / trade-dates /
   bars / active boards / memberships / membershipVersion 全在同一 snapshot）→
   ``build_market_records`` → read transaction/session **完全关闭**（不与 write 混成一个事务）。
② MEMBERSHIP COMMIT GUARD
   fresh guard session；``LOCK TABLE market_boards, market_board_memberships IN SHARE MODE``
   阻止 board sync 的 DML 切换 membership；re-read active board → membershipVersion，
   与 F1A context **精确全等**比对；不一致 → :class:`ProjectionInputChangedError`，
   **绝不写 projection**（不是先 DELETE 再检查）。
③ WRITE
   guard 仍持锁时，独立 **fresh** write session → F1B ``replace_dashboard_projection``
   （F1B 要求 ``in_transaction() == False``，故不可复用 guard session）。

不做自动重试（本 checkpoint fail closed）；失败由更外层决定是否重跑。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import AsyncSessionLocal
from app.models.market_board import MarketBoard
from app.repositories.market_dashboard_projection_repository import (
    ProjectionWriteResult,
    replace_dashboard_projection,
)
from app.services import market_dashboard_projection_service as projection_service
from app.services.market_dashboard_index_facts import fetch_market_index_facts


class ProjectionInputChangedError(RuntimeError):
    """F1A snapshot 到 F1B commit 之间 membership 被切换（fail closed，不自动重试）。"""


# 读事务：同一 snapshot + 只读（必须在任何业务 SELECT 之前设置）。
_READ_SNAPSHOT_SQL = text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

# guard：短时间阻止 board / membership 的 DML（board sync 用 ROW EXCLUSIVE，与之冲突）；
# 普通 SELECT 仍可正常读取。不得用 ACCESS EXCLUSIVE / SHARE ROW EXCLUSIVE。
_GUARD_LOCK_SQL = text("LOCK TABLE market_boards, market_board_memberships IN SHARE MODE")


def _validate_context_invariants(context: projection_service.ProjectionContext) -> None:
    """orchestration 防御（不改 F1A）：board_ids 无重复且与 membership_versions 键集一致。"""
    board_ids = list(context.board_ids)
    if len(set(board_ids)) != len(board_ids):
        raise ValueError("projection context has duplicate board_ids")
    if set(board_ids) != set(context.membership_versions):
        raise ValueError("projection context board_ids and membership_versions keys mismatch")


async def _lock_membership_tables(session: AsyncSession) -> None:
    await session.execute(_GUARD_LOCK_SQL)


async def _current_membership_versions(session: AsyncSession) -> dict[UUID, str]:
    """当前 active board 的 (id -> membershipVersion)（guard 持锁后查询）。"""
    rows = (
        await session.execute(
            select(MarketBoard.id, MarketBoard.membershipVersion).where(
                MarketBoard.isActive.is_(True)
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _assert_membership_snapshot_current(
    session: AsyncSession,
    expected_versions: Mapping[UUID, str],
) -> None:
    """已持 guard SHARE lock 时必须调用；context 与 current 精确全等，否则 fail closed。"""
    current = await _current_membership_versions(session)
    expected = dict(expected_versions)
    if current == expected:
        return
    expected_ids = set(expected)
    current_ids = set(current)
    changed = {bid for bid in (expected_ids & current_ids) if expected[bid] != current[bid]}
    raise ProjectionInputChangedError(
        "board membership snapshot changed during projection rebuild: "
        f"expected_boards={len(expected_ids)} current_boards={len(current_ids)} "
        f"missing_boards={len(expected_ids - current_ids)} "
        f"added_boards={len(current_ids - expected_ids)} "
        f"version_changed_boards={len(changed)}"
    )


async def rebuild_market_dashboard_projection(
    end_date: date,
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> ProjectionWriteResult:
    """安全重建并原子发布 dashboard projection。

    Returns: F1B :class:`ProjectionWriteResult`。
    Raises: :class:`ProjectionInputChangedError`（membership 被切换）；上游 ValueError/RuntimeError。
    """
    # [PANJI-MARKET-OVERVIEW] 事务外拉取三大指数 + 880006（失败 → 抛异常 fail-closed 保留旧投影）
    index_facts = await fetch_market_index_facts(end_date)

    # ① READ / COMPUTE —— 独立 REPEATABLE READ + READ ONLY snapshot，随后完全关闭
    async with session_factory() as read_session:
        async with read_session.begin():
            await read_session.execute(_READ_SNAPSHOT_SQL)
            context = await projection_service.prepare_projection_context(read_session, end_date)
            market_records = projection_service.build_market_records(context, index_facts)

    # [PANJI-MARKET-OVERVIEW] fail-closed：头条投影日必须完整（三大指数 + 涨跌停 五字段缺一不可）；
    # 任一不可得 → 保留旧投影，绝不写残缺最新快照
    last_date = context.display_dates[-1]
    last = index_facts.get(last_date)
    if last is None or (
        last.sse_close is None
        or last.szse_close is None
        or last.chinext_close is None
        or last.limit_up_count is None
        or last.limit_down_count is None
    ):
        raise RuntimeError(
            f"market overview: 头条投影日 {last_date} 指数/涨跌停字段不完整 → fail-closed 保留旧投影"
        )

    _validate_context_invariants(context)

    # ② MEMBERSHIP COMMIT GUARD（SHARE lock 期间比对；不匹配绝不写 projection）
    async with session_factory() as guard_session:
        async with guard_session.begin():
            await _lock_membership_tables(guard_session)
            await _assert_membership_snapshot_current(guard_session, context.membership_versions)

            # ③ WRITE —— 独立 fresh session（F1B 要求无打开事务）；guard 仍持锁
            async with session_factory() as write_session:
                result = await replace_dashboard_projection(
                    write_session,
                    market_records=market_records,
                    scope_chunks=projection_service.iter_scope_record_chunks(context),
                    expected_membership_versions=context.membership_versions,
                )

    return result


if __name__ == "__main__":
    print("OK: market_dashboard_projection_rebuild_service")
