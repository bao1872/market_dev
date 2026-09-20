"""F1C rebuild orchestration — 真实 PostgreSQL contract（isolation + membership guard）。

真实 PostgreSQL 验证（PANJI_REMOTE_VERIFY_DB_TEST=1, plan=targeted-pg）：
1. read 阶段事务真的是 REPEATABLE READ + READ ONLY（在 injected prepare 内 SHOW 实测，
   不是源码字符串）；
2. membership guard：exact active board/version snapshot → PASS；membershipVersion 改变 → FAIL；
   active board set 改变（新增 / 缺失）→ FAIL；失败时绝不调用 writer；
3. guard ``LOCK TABLE ... IN SHARE MODE`` 在真实 PostgreSQL 上可执行。

不做多进程/线程 deadlock stress。
============================================================================
锁安全：使用 TestAsyncSessionLocal 短事务；orchestration 自身管理 read/guard/write 事务。
============================================================================
"""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, text

import app.services.market_dashboard_projection_service as proj
import app.services.market_dashboard_service as svc
from app.models.market_board import MarketBoard
from app.models.market_dashboard import (
    MarketDashboardMarketDaily,
    MarketDashboardScopeDaily,
)
from app.repositories.market_dashboard_projection_repository import ProjectionWriteResult
from app.services import market_dashboard_projection_rebuild_service as rebuild
from tests.conftest import TestAsyncSessionLocal

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------- helpers
def _minimal_context(board_ids, membership_versions) -> proj.ProjectionContext:  # noqa: ANN001
    return proj.ProjectionContext(
        projection_trade_date=date(2026, 9, 18),
        display_dates=[],
        stock_facts=svc._compute_stock_facts_long(None),
        membership_long=svc._build_membership_long({}),
        board_ids=list(board_ids),
        membership_versions=dict(membership_versions),
    )


async def _create_board(membership_version: str) -> UUID:
    async with TestAsyncSessionLocal() as session:
        board = MarketBoard(
            externalCode=f"mdf1c-{uuid4().hex[:12]}",
            name="proj-f1c-test",
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


def _patch_flow(monkeypatch, ctx, *, current_override=None):  # noqa: ANN001
    """替换 prepare/build/iter/writer；保留真实 guard 读取，除非显式 override。"""
    calls: list[int] = []

    async def _prepare(_session, _end_date):
        return ctx

    def _build(_ctx):
        return [{"trade_date": date(2026, 9, 18)}]

    def _iter(_ctx, **_kw):
        return iter(())

    async def _writer(*_a, **_k):  # noqa: ANN002, ANN003
        calls.append(1)
        return ProjectionWriteResult(
            projection_trade_date=date(2026, 9, 18),
            market_rows=1,
            scope_rows=len(ctx.membership_versions),
        )

    monkeypatch.setattr(proj, "prepare_projection_context", _prepare)
    monkeypatch.setattr(proj, "build_market_records", _build)
    monkeypatch.setattr(proj, "iter_scope_record_chunks", _iter)
    monkeypatch.setattr(rebuild, "replace_dashboard_projection", _writer)
    if current_override is not None:

        async def _cur(_session):
            return dict(current_override)

        monkeypatch.setattr(rebuild, "_current_membership_versions", _cur)
    return calls


# ---------------------------------------------------------------
# 1. read snapshot isolation（真实 SHOW）
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_rebuild_read_snapshot_isolation(monkeypatch) -> None:
    seen: dict[str, str] = {}
    ctx = _minimal_context([], {})

    # 顺序不可颠倒：先装通用 fake（build/iter/writer/guard），最后再覆盖 prepare；
    # 否则 _patch_flow 内的通用 _prepare 会把下面的真实 SHOW probe 覆盖掉。
    calls = _patch_flow(monkeypatch, ctx, current_override={})

    async def _prepare(session, _end_date):
        iso = (await session.execute(text("SHOW transaction_isolation"))).scalar()
        ro = (await session.execute(text("SHOW transaction_read_only"))).scalar()
        seen["iso"] = str(iso)
        seen["ro"] = str(ro)
        return ctx

    monkeypatch.setattr(proj, "prepare_projection_context", _prepare)

    result = await rebuild.rebuild_market_dashboard_projection(date(2026, 9, 18))
    assert seen == {"iso": "repeatable read", "ro": "on"}
    assert calls == [1]  # guard 通过后 delegate 给 writer
    assert isinstance(result, ProjectionWriteResult)


# ---------------------------------------------------------------
# 2. guard: exact snapshot passes
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_rebuild_guard_exact_snapshot_passes(monkeypatch) -> None:
    bid = await _create_board("mv-guard")
    try:
        async with TestAsyncSessionLocal() as session:
            current = await rebuild._current_membership_versions(session)
        assert bid in current
        ctx = _minimal_context(list(current.keys()), current)
        calls = _patch_flow(monkeypatch, ctx)  # 真实 guard 读取

        result = await rebuild.rebuild_market_dashboard_projection(date(2026, 9, 18))
        assert calls == [1]
        assert isinstance(result, ProjectionWriteResult)
    finally:
        await _cleanup([bid])


# ---------------------------------------------------------------
# 3. guard: membershipVersion changed → reject
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_rebuild_guard_membership_version_change_rejected(monkeypatch) -> None:
    bid = await _create_board("mv-guard")
    try:
        async with TestAsyncSessionLocal() as session:
            current = await rebuild._current_membership_versions(session)
        mutated = dict(current)
        mutated[bid] = "mv-CHANGED"
        ctx = _minimal_context(list(mutated.keys()), mutated)
        calls = _patch_flow(monkeypatch, ctx)

        with pytest.raises(rebuild.ProjectionInputChangedError):
            await rebuild.rebuild_market_dashboard_projection(date(2026, 9, 18))
        assert calls == []  # 绝不写 projection
    finally:
        await _cleanup([bid])


# ---------------------------------------------------------------
# 4. guard: active board set changed → reject
# ---------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["added", "missing"])
async def test_rebuild_guard_active_set_change_rejected(monkeypatch, mode: str) -> None:
    bid = await _create_board("mv-guard")
    try:
        async with TestAsyncSessionLocal() as session:
            current = await rebuild._current_membership_versions(session)
        if mode == "added":
            changed = dict(current)
            changed[uuid4()] = "mv-extra"
        else:
            changed = {k: v for k, v in current.items() if k != bid}
        ctx = _minimal_context(list(changed.keys()), changed)
        calls = _patch_flow(monkeypatch, ctx)

        with pytest.raises(rebuild.ProjectionInputChangedError):
            await rebuild.rebuild_market_dashboard_projection(date(2026, 9, 18))
        assert calls == []
    finally:
        await _cleanup([bid])


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
