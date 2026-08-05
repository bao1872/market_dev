"""[V2.1 EPIC-01] 领域 run 模型与 publication pointer 真实行为测试（共享开发库目标测试）。

覆盖：
- board_facts / chip_consensus / auction_anchor 三个领域 run 模型可持久化、可回读；
- × run-item 级联删除；
- publication pointer 唯一约束（scope_type, scope_key, trade_date, publication_kind）；
- publication 原子提交（同事务写入 run + pointer）；
- publication 回滚保留旧 pointer（不误覆盖）。

设计约束：共享开发库 bz_stock 含真实业务数据，本文件统一使用未来合成交易日 `_FUTURE`，
该日期在共享库中不存在真实数据，测试自插记录即为该日期唯一结果，保证断言确定性与幂等
（savepoint rollback，无残留）。

用法：
    PANJI_SHARED_DEV_DB_TEST=1 PANJI_SHARED_DEV_DB_TARGET=tests/test_domain_runs_publication_pg.py \
        APP_ENV=development backend/.venv/bin/python -m pytest \
        backend/tests/test_domain_runs_publication_pg.py -q -p no:cacheprovider
"""

from __future__ import annotations

import uuid
from datetime import date as _date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.factor_publication import (
    PUBLICATION_KIND_BOARD_FACTS,
    FactorPublication,
)

pytestmark = pytest.mark.shared_dev_db

# 未来合成交易日：共享开发库中不存在真实数据。
_FUTURE = _date(2099, 12, 31)


async def _add_board_facts_run(db: AsyncSession, trade_date=_FUTURE, **overrides):
    from app.models.board_facts_run import BoardFactsRun

    run = BoardFactsRun(
        trade_date=trade_date,
        run_mode=overrides.get("run_mode", "manual_current"),
        source=overrides.get("source", "pywencai"),
        status=overrides.get("status", "published"),
        readiness=overrides.get("readiness", "ready"),
        **{k: v for k, v in overrides.items() if k not in {"run_mode", "source", "status", "readiness"}},
    )
    db.add(run)
    await db.flush()
    return run


async def _add_publication(db: AsyncSession, *, data_run_id, trade_date=_FUTURE, **overrides):
    pub = FactorPublication(
        scope_type=overrides.get("scope_type", "market"),
        scope_key=overrides.get("scope_key", f"test-{uuid.uuid4().hex[:8]}"),
        trade_date=trade_date,
        publication_kind=overrides.get("publication_kind", PUBLICATION_KIND_BOARD_FACTS),
        algorithm_version=overrides.get("algorithm_version", "v1"),
        data_run_id=data_run_id,
        coverage_ratio=overrides.get("coverage_ratio", 1.0),
    )
    db.add(pub)
    await db.flush()
    return pub


@pytest.mark.asyncio
async def test_board_facts_run_persist_and_read(db_session: AsyncSession) -> None:
    """board_facts_run 可持久化并可回读。"""
    from app.models.board_facts_run import BoardFactsRun

    run = await _add_board_facts_run(db_session)
    run_id = run.id

    result = await db_session.execute(select(BoardFactsRun).where(BoardFactsRun.id == run_id))
    loaded = result.scalar_one()
    assert loaded.trade_date == _FUTURE
    assert loaded.source == "pywencai"
    assert loaded.status == "published"
    assert loaded.readiness == "ready"


@pytest.mark.asyncio
async def test_board_facts_run_item_cascade(db_session: AsyncSession) -> None:
    """board_facts_run_items 随 run 级联删除。"""
    from app.models.board_facts_run import BoardFactsRunItem

    run = await _add_board_facts_run(db_session)
    item = BoardFactsRunItem(
        run_id=run.id,
        trade_date=_FUTURE,
        instrument_symbol="SH600000",
        resolved=True,
        industry_l1="银行",
    )
    db_session.add(item)
    await db_session.flush()

    await db_session.delete(run)
    await db_session.flush()

    result = await db_session.execute(
        select(BoardFactsRunItem).where(BoardFactsRunItem.run_id == run.id)
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_chip_consensus_run_persist_and_read(db_session: AsyncSession) -> None:
    """chip_consensus_run 可持久化并可回读。"""
    from app.models.chip_consensus_run import ChipConsensusRun

    run = ChipConsensusRun(
        trade_date=_FUTURE,
        source_core_run_id=uuid.uuid4(),
        algorithm_version="chip-v1",
        status="succeeded",
        expected_count=100,
        succeeded_count=100,
        coverage_ratio=1.0,
    )
    db_session.add(run)
    await db_session.flush()
    run_id = run.id

    result = await db_session.execute(
        select(ChipConsensusRun).where(ChipConsensusRun.id == run_id)
    )
    loaded = result.scalar_one()
    assert loaded.source_core_run_id == run.source_core_run_id
    assert loaded.coverage_ratio == 1.0


@pytest.mark.asyncio
async def test_auction_anchor_run_persist_and_read(db_session: AsyncSession) -> None:
    """auction_anchor_run 可持久化并可回读。"""
    from app.models.auction_anchor_run import AuctionAnchorRun

    run = AuctionAnchorRun(
        trade_date=_FUTURE,
        source_core_run_id=uuid.uuid4(),
        mode="structure_only",
        algorithm_version="auction-v1",
        status="succeeded",
        coverage_ratio=1.0,
    )
    db_session.add(run)
    await db_session.flush()
    run_id = run.id

    result = await db_session.execute(
        select(AuctionAnchorRun).where(AuctionAnchorRun.id == run_id)
    )
    loaded = result.scalar_one()
    assert loaded.mode == "structure_only"
    assert loaded.coverage_ratio == 1.0


@pytest.mark.asyncio
async def test_publication_pointer_unique(db_session: AsyncSession) -> None:
    """publication pointer 唯一约束：(scope_type, scope_key, trade_date, publication_kind)。"""
    from sqlalchemy.exc import IntegrityError

    run_a = await _add_board_facts_run(db_session)
    run_b = await _add_board_facts_run(db_session)

    scope_key = f"unique-{uuid.uuid4().hex[:8]}"
    await _add_publication(
        db_session,
        data_run_id=run_a.id,
        scope_key=scope_key,
        publication_kind=PUBLICATION_KIND_BOARD_FACTS,
    )

    # 同 (scope_type, scope_key, trade_date, publication_kind) 第二次插入必须失败
    duplicate = FactorPublication(
        scope_type="market",
        scope_key=scope_key,
        trade_date=_FUTURE,
        publication_kind=PUBLICATION_KIND_BOARD_FACTS,
        algorithm_version="v1",
        data_run_id=run_b.id,
        coverage_ratio=1.0,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_publication_atomic_commit(db_session: AsyncSession) -> None:
    """publication 原子提交：同一事务写 run + pointer，commit 后两者都可见。"""
    from app.models.board_facts_run import BoardFactsRun

    run = await _add_board_facts_run(db_session)
    scope_key = f"atomic-{uuid.uuid4().hex[:8]}"
    await _add_publication(
        db_session,
        data_run_id=run.id,
        scope_key=scope_key,
        publication_kind=PUBLICATION_KIND_BOARD_FACTS,
    )

    # db_session commit 仅提交 savepoint，不污染数据库；两者同事务可见
    await db_session.commit()

    pub_result = await db_session.execute(
        select(FactorPublication)
        .where(FactorPublication.scope_key == scope_key)
        .where(FactorPublication.publication_kind == PUBLICATION_KIND_BOARD_FACTS)
    )
    pub = pub_result.scalar_one()
    run_result = await db_session.execute(
        select(BoardFactsRun).where(BoardFactsRun.id == pub.data_run_id)
    )
    assert run_result.scalar_one() is not None


@pytest.mark.asyncio
async def test_publication_rollback_keeps_old_pointer(db_session: AsyncSession) -> None:
    """publication 回滚保留旧 pointer：新 pointer 写入失败时旧 pointer 不被覆盖。"""
    from sqlalchemy.exc import IntegrityError

    run_a = await _add_board_facts_run(db_session)
    run_b = await _add_board_facts_run(db_session)

    scope_key = f"rollback-{uuid.uuid4().hex[:8]}"
    old_pub = await _add_publication(
        db_session,
        data_run_id=run_a.id,
        scope_key=scope_key,
        publication_kind=PUBLICATION_KIND_BOARD_FACTS,
    )
    old_pub_id = old_pub.id

    # 模拟切换：先创建新 pointer（会违反唯一约束 → 抛错），回滚后旧 pointer 仍在
    duplicate = FactorPublication(
        scope_type="market",
        scope_key=scope_key,
        trade_date=_FUTURE,
        publication_kind=PUBLICATION_KIND_BOARD_FACTS,
        algorithm_version="v1",
        data_run_id=run_b.id,
        coverage_ratio=1.0,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    result = await db_session.execute(
        select(FactorPublication).where(FactorPublication.id == old_pub_id)
    )
    persisted = result.scalar_one()
    assert persisted is not None
    assert persisted.data_run_id == run_a.id
