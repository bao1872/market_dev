"""F1B projection persistence — 真实 PostgreSQL 原子替换 / rollback 合同。

真实 PostgreSQL 验证（PANJI_REMOTE_VERIFY_DB_TEST=1, plan=targeted-pg）：
A. 成功原子替换：旧 rows 全消失，market/scope == 新 rows，membership_version 正确
B. 失败 rollback：后一批 scope 违反 DB CHECK（above > valid）→ 整事务回滚，旧 snapshot 完整保留
C. 同日 rebuild 幂等：连续 replace 两次，行数/值不变、无重复
D. 拒绝 stale T：旧 T > 新 T 时拒绝，新 fresh session 读旧不变
E. 部分 generator rollback：expected rows 不足 → raise → rollback（旧 snapshot 保留）
F. membership_version mismatch rollback：mv 不符 → raise → rollback（旧 snapshot 保留）

============================================================================
锁安全约束（与 093/094/F0 contract 一致）：
conftest 的 db_session 是 savepoint 模式，测试期间持有外层未提交事务。因此本文件所有真实 PG
操作用 TestAsyncSessionLocal 短事务（open→execute→commit→close），writer 亦要求 fresh session。
禁止在 db_session 内执行本文件的 DDL/写操作。
============================================================================
"""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from app.domain.market_dashboard.breadth import WINDOWS
from app.models.market_board import MarketBoard
from app.models.market_dashboard import (
    MarketDashboardMarketDaily,
    MarketDashboardScopeDaily,
)
from app.repositories.market_dashboard_projection_repository import (
    replace_dashboard_projection,
)
from tests.conftest import TestAsyncSessionLocal

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------
# helpers
# ---------------------------------------------------------------
def _counts(
    *,
    member_count: int = 1,
    valid_return_count: int = 1,
    equal_weight_return: float | None = 0.0,
    valid: int = 1,
    above: int = 1,
) -> dict[str, object]:
    fields: dict[str, object] = {
        "member_count": member_count,
        "valid_return_count": valid_return_count,
        "equal_weight_return": equal_weight_return,
    }
    for k in WINDOWS:
        fields[f"ma{k}_valid_count"] = valid
        fields[f"ma{k}_above_count"] = above
    return fields


def _mkt(d: date, **kw: object) -> dict[str, object]:
    return {"trade_date": d, **_counts(**kw)}


def _scope(bid: UUID, d: date, mv: str, **kw: object) -> dict[str, object]:
    return {"board_id": bid, "trade_date": d, "membership_version": mv, **_counts(**kw)}


async def _create_board(membership_version: str) -> UUID:
    async with TestAsyncSessionLocal() as session:
        board = MarketBoard(
            externalCode=f"mdf1b-{uuid4().hex[:12]}",
            name="proj-f1b-test",
            type="industry",
            membershipVersion=membership_version,
        )
        session.add(board)
        await session.commit()
        return board.id


async def _cleanup(board_ids: list[UUID]) -> None:
    async with TestAsyncSessionLocal() as session:
        await session.execute(delete(MarketDashboardScopeDaily))
        await session.execute(delete(MarketDashboardMarketDaily))
        if board_ids:
            await session.execute(delete(MarketBoard).where(MarketBoard.id.in_(board_ids)))
        await session.commit()


async def _replace(market_records, scope_chunks, expected):  # noqa: ANN001
    async with TestAsyncSessionLocal() as session:
        return await replace_dashboard_projection(
            session,
            market_records=market_records,
            scope_chunks=scope_chunks,
            expected_membership_versions=expected,
        )


async def _read_projection():
    async with TestAsyncSessionLocal() as session:
        market = (
            (
                await session.execute(
                    select(MarketDashboardMarketDaily).order_by(
                        MarketDashboardMarketDaily.trade_date
                    )
                )
            )
            .scalars()
            .all()
        )
        scope = (
            (
                await session.execute(
                    select(MarketDashboardScopeDaily).order_by(
                        MarketDashboardScopeDaily.board_id,
                        MarketDashboardScopeDaily.trade_date,
                    )
                )
            )
            .scalars()
            .all()
        )
        await session.commit()
        return market, scope


# ---------------------------------------------------------------
# A. successful atomic replacement
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_atomic_replacement_success() -> None:
    t_old = date(2026, 9, 17)
    t_new = date(2026, 9, 18)
    b1 = await _create_board("mv-new")
    b2 = await _create_board("mv-new")
    try:
        await _replace(
            [_mkt(t_old, member_count=5, valid=5, above=3)],
            [[_scope(b1, t_old, "mv-old"), _scope(b2, t_old, "mv-old")]],
            {b1: "mv-old", b2: "mv-old"},
        )
        result = await _replace(
            [_mkt(t_new, member_count=7, valid=6, above=4)],
            [[_scope(b1, t_new, "mv-new"), _scope(b2, t_new, "mv-new")]],
            {b1: "mv-new", b2: "mv-new"},
        )
        assert result.projection_trade_date == t_new
        assert result.market_rows == 1
        assert result.scope_rows == 2

        market, scope = await _read_projection()
        assert [m.trade_date for m in market] == [t_new]  # 旧 T 已消失
        assert [m.member_count for m in market] == [7]
        assert {(s.board_id, s.trade_date) for s in scope} == {(b1, t_new), (b2, t_new)}
        assert {s.membership_version for s in scope} == {"mv-new"}
    finally:
        await _cleanup([b1, b2])


# ---------------------------------------------------------------
# B. failure rollback preserves old snapshot
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_failure_rollback_preserves_old_snapshot() -> None:
    t_old = date(2026, 9, 17)
    t_new = date(2026, 9, 18)
    b1 = await _create_board("mv-1")
    b2 = await _create_board("mv-1")
    expected = {b1: "mv-1", b2: "mv-1"}
    try:
        await _replace(
            [_mkt(t_old, member_count=5, valid=5, above=3)],
            [[_scope(b1, t_old, "mv-1"), _scope(b2, t_old, "mv-1")]],
            expected,
        )
        # chunk1 合法；chunk2 违反 DB CHECK（ma5_above_count=2 > ma5_valid_count=1）
        bad_chunk = [_scope(b2, t_new, "mv-1", valid=1, above=2)]
        with pytest.raises(Exception):  # noqa: B017 - DB CHECK 违反（IntegrityError）
            await _replace(
                [_mkt(t_new)],
                [[_scope(b1, t_new, "mv-1")], bad_chunk],
                expected,
            )

        market, scope = await _read_projection()
        assert [m.trade_date for m in market] == [t_old]  # 旧 snapshot 完整保留
        assert {(s.board_id, s.trade_date) for s in scope} == {(b1, t_old), (b2, t_old)}
    finally:
        await _cleanup([b1, b2])


# ---------------------------------------------------------------
# C. same-T rebuild idempotent
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_same_t_rebuild_idempotent() -> None:
    t = date(2026, 9, 18)
    b1 = await _create_board("mv-1")
    expected = {b1: "mv-1"}
    try:
        r1 = await _replace(
            [_mkt(t, member_count=3, valid=2, above=1)],
            [[_scope(b1, t, "mv-1", member_count=3, valid=2, above=1)]],
            expected,
        )
        r2 = await _replace(
            [_mkt(t, member_count=3, valid=2, above=1)],
            [[_scope(b1, t, "mv-1", member_count=3, valid=2, above=1)]],
            expected,
        )
        assert r1.market_rows == r2.market_rows == 1
        assert r1.scope_rows == r2.scope_rows == 1

        market, scope = await _read_projection()
        assert len(market) == 1 and len(scope) == 1  # 不增加、无重复
        assert market[0].member_count == 3
        assert scope[0].ma5_above_count == 1
    finally:
        await _cleanup([b1])


# ---------------------------------------------------------------
# D. reject stale T
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_reject_stale_trade_date() -> None:
    t_new = date(2026, 9, 18)
    t_stale = date(2026, 9, 17)
    b1 = await _create_board("mv-1")
    expected = {b1: "mv-1"}
    try:
        await _replace(
            [_mkt(t_new, member_count=9, valid=8, above=5)],
            [[_scope(b1, t_new, "mv-1", member_count=9, valid=8, above=5)]],
            expected,
        )
        with pytest.raises(ValueError):
            await _replace([_mkt(t_stale)], [[_scope(b1, t_stale, "mv-1")]], expected)

        market, scope = await _read_projection()
        assert [m.trade_date for m in market] == [t_new]
        assert market[0].member_count == 9
        assert {(s.board_id, s.trade_date) for s in scope} == {(b1, t_new)}
    finally:
        await _cleanup([b1])


# ---------------------------------------------------------------
# E. partial generator rollback
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_partial_generator_rollback() -> None:
    t_old = date(2026, 9, 17)
    t_new = date(2026, 9, 18)
    b1 = await _create_board("mv-1")
    b2 = await _create_board("mv-1")
    expected = {b1: "mv-1", b2: "mv-1"}
    try:
        await _replace(
            [_mkt(t_old)],
            [[_scope(b1, t_old, "mv-1"), _scope(b2, t_old, "mv-1")]],
            expected,
        )
        # expected = 2 boards × 2 dates = 4；generator 只给 3（缺 b2@t_new）→ 计数不足 → rollback
        with pytest.raises(ValueError):
            await _replace(
                [_mkt(t_old), _mkt(t_new)],
                [
                    [
                        _scope(b1, t_old, "mv-1"),
                        _scope(b2, t_old, "mv-1"),
                        _scope(b1, t_new, "mv-1"),
                    ]
                ],
                expected,
            )

        market, scope = await _read_projection()
        assert [m.trade_date for m in market] == [t_old]
        assert {(s.board_id, s.trade_date) for s in scope} == {(b1, t_old), (b2, t_old)}
    finally:
        await _cleanup([b1, b2])


# ---------------------------------------------------------------
# F. membership version mismatch rollback
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_membership_version_mismatch_rollback() -> None:
    t_old = date(2026, 9, 17)
    t_new = date(2026, 9, 18)
    b1 = await _create_board("mv-new")
    expected = {b1: "mv-new"}
    try:
        await _replace(
            [_mkt(t_old)],
            [[_scope(b1, t_old, "mv-new")]],
            expected,
        )
        with pytest.raises(ValueError):
            await _replace([_mkt(t_new)], [[_scope(b1, t_new, "mv-old")]], expected)

        market, scope = await _read_projection()
        assert [m.trade_date for m in market] == [t_old]
        assert {(s.board_id, s.trade_date) for s in scope} == {(b1, t_old)}
        assert scope[0].membership_version == "mv-new"
    finally:
        await _cleanup([b1])


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
